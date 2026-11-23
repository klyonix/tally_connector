"""
The master switch, and jobs that stop reporting.

Both come from one report: a dashboard reading "2 Syncs Running · 0%" about two
exports whose worker had never picked them up. Nothing was running; the header
had no way to know that, and no way to say it.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from tally_connector.api import settings as tally_settings
from tally_connector.api.export import _create_log, export_all
from tally_connector.api.jobs import (
	EXPORT_STAGES,
	STALE_AFTER_SECONDS,
	JobReporter,
	cancel,
	notifications,
	running,
	status,
)


def _company():
	return frappe.get_all("Company", pluck="name")[0]


class TestTheMasterSwitch(IntegrationTestCase):
	"""These tests move a site-wide switch, which needs care.

	`set_sync_enabled` commits, and a commit inside an `IntegrationTestCase`
	escapes the rollback that would otherwise undo it. Capturing the ambient
	value in `setUp` and restoring it in `tearDown` looked right and was a
	ratchet: once anything left the switch off, every later `setUp` captured
	**off** and dutifully restored it, so the bench stayed off and the next full
	run failed on a test asserting the default. Which is what happened.

	So the state is forced rather than inherited: on before each test, on after
	the class whatever the tests did. Nothing here reads what the site happened
	to be set to.
	"""

	def setUp(self):
		tally_settings.set_sync_enabled(1)

	@classmethod
	def tearDownClass(cls):
		tally_settings.set_sync_enabled(1)
		frappe.db.commit()
		super().tearDownClass()

	def test_the_backfill_patch_switches_an_upgraded_site_on(self):
		"""A `Check` default is written when a Single is first created and never
		again, so a site upgrading into this field reads 0 - every existing
		installation would come up with syncing silently off.

		The patch is exercised directly, against a site with no stored value,
		rather than by reading whatever this bench happens to hold. The first
		version of this test asserted the ambient value, which tests the bench.
		"""
		from tally_connector.patches.v1_0.backfill_sync_enabled import execute

		def stored():
			"""The row itself, which is what the patch keys on.

			Not `get_single_value`: for an absent Single value that answers the
			fieldtype's own default - 0 for a Check - rather than None. Which is
			the whole reason this patch exists and the whole reason it cannot
			ask that question either.
			"""
			return frappe.db.sql(
				"""select value from tabSingles
                   where doctype = 'Tally Settings' and field = 'sync_enabled'"""
			)

		frappe.db.sql(
			"""delete from tabSingles
               where doctype = 'Tally Settings' and field = 'sync_enabled'"""
		)
		frappe.db.commit()
		frappe.clear_document_cache("Tally Settings", "Tally Settings")
		self.assertEqual(stored(), (), "the fixture did not reach the state the patch is for")

		execute()
		frappe.clear_document_cache("Tally Settings", "Tally Settings")
		self.assertTrue(stored(), "the patch wrote nothing")
		self.assertTrue(frappe.db.get_single_value("Tally Settings", "sync_enabled"))

	def test_the_patch_never_overrides_a_deliberate_choice(self):
		"""Somebody who has switched syncing off must not have it switched back
		on by an upgrade."""
		from tally_connector.patches.v1_0.backfill_sync_enabled import execute

		tally_settings.set_sync_enabled(0)
		execute()
		frappe.clear_document_cache("Tally Settings", "Tally Settings")
		self.assertFalse(frappe.db.get_single_value("Tally Settings", "sync_enabled"))

	def test_switching_it_off_refuses_an_export(self):
		tally_settings.set_sync_enabled(0)
		with self.assertRaises(frappe.ValidationError) as caught:
			export_all(from_date=None, to_date=None, company=_company())
		self.assertIn("switched off", str(caught.exception))

	def test_switching_it_off_refuses_a_single_area_export(self):
		"""Every door, not just the big one. A switch that stops the full export
		and leaves ten buttons working is not a switch."""
		from tally_connector.api.export import export_chart_of_accounts

		tally_settings.set_sync_enabled(0)
		with self.assertRaises(frappe.ValidationError):
			export_chart_of_accounts(company=_company())

	def test_switching_it_back_on_lets_an_export_run(self):
		"""The other direction, so a test that passes against a switch stuck off
		is not possible."""
		tally_settings.set_sync_enabled(1)
		result = export_all(
			from_date="1900-01-01", to_date="1900-01-02", company=_company(), background=False
		)
		try:
			self.assertTrue(result["success"], result.get("error"))
		finally:
			if result.get("log"):
				frappe.delete_doc("Tally Export Log", result["log"], force=True, ignore_permissions=True)
				frappe.db.commit()

	def test_the_switch_is_reported_to_the_screen(self):
		tally_settings.set_sync_enabled(0)
		from tally_connector.api.dashboard import companies_with_defaults

		self.assertFalse(companies_with_defaults()["sync_enabled"])


class TestAJobThatStoppedReporting(IntegrationTestCase):
	def setUp(self):
		self.log = _create_log("Full Export", "Tally XML", None, None, _company())

	def tearDown(self):
		if frappe.db.exists("Tally Export Log", self.log):
			frappe.delete_doc("Tally Export Log", self.log, force=True, ignore_permissions=True)
			frappe.db.commit()

	def _go_quiet(self):
		"""Age the job's heartbeat past the threshold."""
		old = add_to_date(now_datetime(), seconds=-(STALE_AFTER_SECONDS + 60))
		frappe.db.set_value("Tally Export Log", self.log, "modified", old, update_modified=False)
		frappe.db.commit()

	def test_a_job_still_reporting_is_not_stalled(self):
		reporter = JobReporter("Tally Export Log", self.log, EXPORT_STAGES)
		reporter.start(total=100)
		reporter.at("Processing Sales", processed=10)
		row = status(self.log)
		self.assertTrue(row["running"])
		self.assertFalse(row["stalled"], "a job that just wrote is being called stalled")

	def test_a_job_that_has_gone_quiet_is_reported_as_stalled(self):
		"""The reported case: queued, never picked up, counted as running for
		ever."""
		JobReporter("Tally Export Log", self.log, EXPORT_STAGES).start(total=100)
		self._go_quiet()
		row = next(r for r in running() if r["name"] == self.log)
		self.assertTrue(row["running"], "it has not finished, so it is still in flight")
		self.assertTrue(row["stalled"], "a job silent for ten minutes is not running")

	def test_a_stalled_job_can_be_cancelled(self):
		JobReporter("Tally Export Log", self.log, EXPORT_STAGES).start(total=100)
		self._go_quiet()
		row = cancel(self.log, "export")
		self.assertEqual(row["status"], "Cancelled")
		self.assertFalse(row["running"])
		self.assertNotIn(self.log, [r["name"] for r in running()])

	def test_a_live_job_cannot_be_cancelled(self):
		"""A button that can kill a running export is a way to lose an hour of
		work by mis-clicking."""
		reporter = JobReporter("Tally Export Log", self.log, EXPORT_STAGES)
		reporter.start(total=100)
		reporter.at("Processing Sales", processed=10)
		with self.assertRaises(frappe.ValidationError) as caught:
			cancel(self.log, "export")
		self.assertIn("still reporting progress", str(caught.exception))

	def test_cancelling_keeps_the_log_and_says_who_did_it(self):
		"""Nothing is deleted. What somebody asks a week later is why it stopped."""
		JobReporter("Tally Export Log", self.log, EXPORT_STAGES).start(total=100)
		self._go_quiet()
		cancel(self.log, "export")
		doc = frappe.get_doc("Tally Export Log", self.log)
		self.assertEqual(doc.status, "Cancelled")
		self.assertIn(frappe.session.user, doc.error_log)
		self.assertTrue(doc.finished_at)


class TestNotifications(IntegrationTestCase):
	def test_a_clean_success_does_not_ring_the_bell(self):
		"""A bell that rings for every successful export is a bell people stop
		looking at."""
		log = _create_log("Full Export", "Tally XML", None, None, _company())
		try:
			JobReporter("Tally Export Log", log, EXPORT_STAGES).finish("Success", records=10)
			data = notifications(since=None)
			mine = next(i for i in data["items"] if i["name"] == log)
			self.assertEqual(mine["tone"], "success")
			self.assertNotIn(mine["tone"], ("error", "warning"))
		finally:
			frappe.delete_doc("Tally Export Log", log, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_a_failure_is_unread_until_it_has_been_seen(self):
		log = _create_log("Full Export", "Tally XML", None, None, _company())
		try:
			JobReporter("Tally Export Log", log, EXPORT_STAGES).finish(
				"Failed", error="Tally refused the masters"
			)
			before = notifications(since=None)
			self.assertGreater(before["unread"], 0)
			mine = next(i for i in before["items"] if i["name"] == log)
			self.assertEqual(mine["tone"], "error")
			self.assertIn("refused", mine["text"])

			# Seen: counted from the caller's own timestamp, so nothing has to be
			# written to mark a thing read.
			after = notifications(since=before["now"])
			self.assertEqual([i for i in after["items"] if i["name"] == log and i["at"] > before["now"]], [])
		finally:
			frappe.delete_doc("Tally Export Log", log, force=True, ignore_permissions=True)
			frappe.db.commit()
