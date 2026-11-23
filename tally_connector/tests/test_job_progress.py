"""
A background job has to be able to say where it is.

The dashboard used to poll a log whose only states were Pending and an outcome,
so the truest thing it could put on screen was "Running in the background...".
These tests hold the replacement to the one property that makes a progress bar
worth drawing: **every number on it is a count something actually reached.**

That is why there is a test here for a bar that does *not* move. A stage with no
rows to count - rendering a model already in memory - must leave the number
where the last counted stage left it, rather than easing it upwards over elapsed
time. A bar that moves while nothing is happening is worse than no bar, because
somebody believes it.
"""

import frappe
from frappe.tests import IntegrationTestCase

from tally_connector.api import dashboard
from tally_connector.api import settings as tally_settings
from tally_connector.api.export import _create_log, run_full_export
from tally_connector.api.jobs import EXPORT_STAGES, IMPORT_STAGES, JobReporter, running, status


def _company():
	"""The busiest company - a progress test on an empty one proves nothing."""
	counts = {c: 0 for c in frappe.get_all("Company", pluck="name")}
	for doctype in ("Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry"):
		for company in counts:
			counts[company] += frappe.db.count(doctype, {"docstatus": 1, "company": company})
	return max(counts, key=counts.get)


class _Recorder(JobReporter):
	"""A reporter that keeps every value it wrote, and still writes it.

	Subclassed rather than monkeypatched so what runs is the real class. An
	override that replaced `_commit` outright would prove the test's own code
	reports correctly and say nothing about this one.
	"""

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.writes = []

	def _commit(self, values):
		self.writes.append(dict(values))
		super()._commit(values)

	@property
	def stages_seen(self):
		out = []
		for v in self.writes:
			stage = v.get("stage")
			if stage and (not out or out[-1] != stage):
				out.append(stage)
		return out


class TestExportReportsWhereItIs(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = _company()
		cls.log = _create_log("Full Export", "Tally XML", None, None, cls.company)
		cls.reporter = _Recorder("Tally Export Log", cls.log, EXPORT_STAGES)
		import tally_connector.api.jobs as jobs_module

		original = jobs_module.JobReporter
		jobs_module.JobReporter = lambda *a, **k: cls.reporter
		try:
			cls.result = run_full_export(cls.log, None, None, cls.company, push=False)
		finally:
			jobs_module.JobReporter = original

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("Tally Export Log", cls.log, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def test_the_export_actually_ran(self):
		"""Everything below is vacuous against an export that did nothing."""
		self.assertTrue(self.result.get("success"), self.result.get("error"))
		self.assertGreater(self.result["counts"]["vouchers"], 10)

	def test_every_stage_reported_is_one_the_client_can_draw(self):
		"""The screen draws `EXPORT_STAGES`. A stage outside it renders as
		nothing at all."""
		for stage in self.reporter.stages_seen:
			with self.subTest(stage=stage):
				self.assertIn(stage, EXPORT_STAGES)

	def test_the_stages_arrive_in_the_order_they_are_declared(self):
		"""A stage list that goes backwards reads as a job that restarted."""
		indexes = [EXPORT_STAGES.index(s) for s in self.reporter.stages_seen]
		self.assertEqual(indexes, sorted(indexes), self.reporter.stages_seen)

	def test_the_work_stages_are_reached(self):
		"""Not just Preparing and Completed: the point is knowing which part of a
		long export is running."""
		seen = set(self.reporter.stages_seen)
		self.assertIn("Preparing", seen)
		self.assertIn("Generating Masters", seen)
		self.assertIn("Generating XML", seen)
		self.assertTrue(
			{"Processing Sales", "Processing Purchases", "Processing Payments"} & seen,
			f"no voucher source reported a stage: {sorted(seen)}",
		)

	def test_progress_never_runs_backwards(self):
		values = [w.get("progress", 0) for w in self.reporter.writes if "progress" in w]
		self.assertEqual(values, sorted(values))

	def test_the_count_is_the_vouchers_that_were_built(self):
		"""The heart of it. `processed` is a tick per voucher the extract actually
		constructed, so the final figure equals the number of vouchers in the
		export - not an estimate, and not a fraction of elapsed time."""
		self.assertEqual(self.reporter.processed, self.result["counts"]["vouchers"])

	def test_the_bar_does_not_move_while_the_xml_is_written(self):
		"""Rendering has no rows to count, so it reports its stage and leaves the
		number alone. This is the test that fails if somebody ever replaces a
		real count with an eased animation."""
		writes = self.reporter.writes
		rendering = [i for i, w in enumerate(writes) if w.get("stage") == "Generating XML"]
		self.assertTrue(rendering, "the render stage was never reported")
		first = rendering[0]
		self.assertEqual(writes[first]["processed"], writes[first - 1]["processed"])

	def test_the_log_holds_what_the_socket_carried(self):
		"""The socket is an optimisation; the log is the answer. A browser that
		missed every event must be able to ask and get the same thing."""
		row = status(self.log)
		self.assertEqual(row["status"], "Success")
		self.assertEqual(row["stage"], "Completed")
		self.assertEqual(row["progress"], 100)
		self.assertEqual(row["processed"], self.reporter.processed)
		self.assertFalse(row["running"])
		self.assertEqual(len(row["files"]), 2)


class TestTheReporterOnItsOwn(IntegrationTestCase):
	def setUp(self):
		self.log = _create_log("Full Export", "Tally XML", None, None, _company())

	def tearDown(self):
		frappe.delete_doc("Tally Export Log", self.log, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_a_stage_with_nothing_to_count_still_places_the_job(self):
		"""With no denominator the stage index is the measure - a real position in
		a declared sequence, which is the honest answer when there are no rows."""
		r = JobReporter("Tally Import Log", self.log, IMPORT_STAGES)
		r.total = 0
		r.stage = "Queued"
		self.assertEqual(r._percent(), 0)
		r.stage = "Completed"
		self.assertEqual(r._percent(), 100)
		r.stage = "Validating"
		self.assertLess(r._percent(), 100)
		self.assertGreater(r._percent(), 0)

	def test_a_counted_stage_never_reports_finished(self):
		"""99 is the ceiling while work is counted, because 100 beside a job that
		is still running is the one number nobody forgives."""
		r = JobReporter("Tally Export Log", self.log, EXPORT_STAGES)
		r.total = 10
		r.processed = 10
		self.assertEqual(r._percent(), 99)

	def test_ticking_is_exact_even_though_reporting_is_sampled(self):
		"""Eleven thousand invoices are a hundred events rather than eleven
		thousand - and the count itself is still every one of them."""
		r = JobReporter("Tally Export Log", self.log, EXPORT_STAGES)
		r.start(total=1000)
		for _ in range(1000):
			r.tick()
		self.assertEqual(r.processed, 1000)
		self.assertEqual(r._percent(), 99)

	def test_a_failure_stops_reporting_a_stage_it_is_no_longer_in(self):
		"""A log left saying "Processing Sales" for ever is how a dead job passes
		for a slow one."""
		r = JobReporter("Tally Export Log", self.log, EXPORT_STAGES)
		r.start(total=100)
		r.at("Processing Sales", processed=40)
		r.finish("Failed", error="the connection went")
		row = status(self.log)
		self.assertEqual(row["status"], "Failed")
		self.assertEqual(row["stage"], "Failed")
		self.assertFalse(row["running"])
		self.assertIn("the connection went", row["error"])

	def test_a_running_job_is_discoverable_by_a_browser_that_did_not_start_it(self):
		"""This is how a second person - or the same person after a reload - sees
		the job at all. Without it the indicator only works in the tab that
		pressed the button."""
		r = JobReporter("Tally Export Log", self.log, EXPORT_STAGES)
		r.start(total=100)
		r.at("Processing Sales", processed=25)
		row = next((x for x in running() if x["name"] == self.log), None)
		self.assertIsNotNone(row, "a running job was not discoverable")
		self.assertTrue(row["running"])
		self.assertEqual(row["stage"], "Processing Sales")
		self.assertEqual(row["processed"], 25)

	def test_a_finished_job_leaves_the_running_list(self):
		r = JobReporter("Tally Export Log", self.log, EXPORT_STAGES)
		r.start(total=100)
		r.finish("Success", records=100)
		self.assertNotIn(self.log, [x["name"] for x in running()])

	def test_a_progress_write_that_fails_does_not_take_the_export_with_it(self):
		"""Losing the books because the progress bar failed is the wrong trade."""
		r = JobReporter("Tally Export Log", "a-log-that-does-not-exist", EXPORT_STAGES)
		r.start(total=10)
		r.tick()
		r.finish("Success", records=1)


class TestTheScreensCanBeDrawn(IntegrationTestCase):
	def test_every_area_has_a_row_and_masters_are_not_dated(self):
		"""A date range beside a unit count would be lying about what the number
		means - a UOM has no posting date."""
		rows = dashboard.areas(_company(), None, None)["rows"]
		self.assertEqual(len(rows), len(dashboard.AREAS))
		by_key = {r["key"]: r for r in rows}
		self.assertFalse(by_key["uoms"]["dated"])
		self.assertFalse(by_key["chart_of_accounts"]["dated"])
		self.assertTrue(by_key["sales_invoices"]["dated"])

	def test_the_overview_answers_in_one_request(self):
		data = dashboard.overview(_company())
		for key in ("company", "host", "summary", "recent", "auto_sync"):
			self.assertIn(key, data)

	def test_the_overview_never_opens_a_socket_to_tally(self):
		"""Opening a dashboard must not hang for the length of a TCP timeout
		because a PC in an accounts office is switched off."""
		import inspect

		source = inspect.getsource(dashboard.overview)
		self.assertNotIn("TallyConnector", source)
		self.assertNotIn("test_connection", source)

	def test_settings_refuse_a_field_no_screen_draws(self):
		"""The signature is not the fence here - the allow-list is - because this
		writes to a Single, where an unlisted field is any field on it."""
		with self.assertRaises(frappe.ValidationError) as caught:
			tally_settings.save({"tally_host": "localhost", "owner": "Administrator"})
		self.assertIn("owner", str(caught.exception))

	def test_settings_carry_the_options_rather_than_the_screen_inventing_them(self):
		data = tally_settings.get()
		self.assertIn("default_export_format", data["fields"])
		self.assertIn("Tally XML", data["fields"]["default_export_format"]["options"])
