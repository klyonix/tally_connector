"""
What happens when something is wrong.

An accounting integration is judged on its failures more than its successes: a
push that half-worked, or an error nobody can act on, costs more than one that
refused cleanly. Every case here is provoked deliberately and the assertion is
always the same shape - the system refused, said why in words a person can act
on, and left nothing behind.
"""

import frappe
from frappe.tests import IntegrationTestCase

from tally_connector.api import export as export_api
from tally_connector.api import import_data


def _company():
	return frappe.get_all("Company", pluck="name")[0]


class TestTallyIsNotThere(IntegrationTestCase):
	"""The most common failure in the field: the machine is off, or the port is
	not open, or somebody typed the host wrong."""

	def setUp(self):
		self.settings = frappe.get_single("Tally Settings")
		self.host, self.port = self.settings.tally_host, self.settings.tally_port

	def tearDown(self):
		frappe.db.set_value(
			"Tally Settings", "Tally Settings", {"tally_host": self.host, "tally_port": self.port}
		)
		frappe.db.commit()

	def _set(self, host, port):
		frappe.db.set_value("Tally Settings", "Tally Settings", {"tally_host": host, "tally_port": port})
		frappe.db.commit()
		frappe.clear_document_cache("Tally Settings", "Tally Settings")

	def test_a_refused_connection_is_reported_rather_than_raised(self):
		"""Port 9 is discard - it refuses immediately, so this tests the handling
		rather than the timeout."""
		self._set("127.0.0.1", 9)
		result = export_api.test_tally_connection()
		self.assertFalse(result["success"])
		self.assertTrue(result["response"], "a failure with no explanation is not actionable")

	def test_a_host_that_does_not_resolve_is_reported_rather_than_raised(self):
		self._set("tally.invalid.example", 9000)
		result = export_api.test_tally_connection()
		self.assertFalse(result["success"])
		self.assertTrue(result["response"])

	def test_pushing_to_a_dead_tally_does_not_lose_the_export(self):
		"""The files are written and attached before anything is sent, so a push
		that cannot reach Tally still leaves a downloadable export rather than an
		hour of generation thrown away."""
		self._set("127.0.0.1", 9)
		result = export_api.export_all(
			from_date=None,
			to_date=None,
			company=_company(),
			push_to_tally_flag=True,
			background=False,
		)
		log = result.get("log")
		try:
			self.assertTrue(log, "no log was written at all")
			files = frappe.get_all(
				"File",
				filters={"attached_to_doctype": "Tally Export Log", "attached_to_name": log},
				pluck="file_name",
			)
			self.assertEqual(len(files), 2, "the generated files were not kept")
			status = frappe.db.get_value("Tally Export Log", log, "status")
			self.assertIn(status, ("Success", "Partial", "Failed"))
		finally:
			if log:
				frappe.delete_doc("Tally Export Log", log, force=True, ignore_permissions=True)
				frappe.db.commit()


class TestBadInputIsRefused(IntegrationTestCase):
	def _file(self, content, name="probe.xml"):
		doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": name,
				"content": content,
				"is_private": 1,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return doc

	def test_a_file_that_is_not_xml_is_refused_before_anything_is_written(self):
		"""The old screen's only answer to this was a stack trace, after it had
		already created half the customers in the file."""
		doc = self._file("this is not xml at all <<<")
		try:
			result = import_data.validate_file(doc.file_url)
			self.assertFalse(result["success"])
			self.assertIn("XML", result["error"])
		finally:
			frappe.delete_doc("File", doc.name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_a_file_that_parses_but_holds_nothing_says_so(self):
		"""An empty file imports perfectly and creates nothing, which reads as
		success unless somebody says otherwise."""
		doc = self._file("<ENVELOPE><BODY></BODY></ENVELOPE>")
		try:
			result = import_data.validate_file(doc.file_url)
			self.assertTrue(result["success"])
			self.assertTrue(result["empty"])
		finally:
			frappe.delete_doc("File", doc.name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_a_file_that_does_not_exist_is_refused_in_words(self):
		with self.assertRaises(frappe.ValidationError):
			import_data.validate_file("/private/files/nothing-here-at-all.xml")

	def test_a_control_character_does_not_stop_the_reader(self):
		"""Tally itself writes files holding characters XML forbids. Refusing
		them outright would refuse real exports, so they are cleaned - and the
		cleaning is the same function the importer uses, not a second one."""
		raw = "<ENVELOPE><BODY><TALLYMESSAGE><LEDGER NAME='A\x1ab'/></TALLYMESSAGE></BODY></ENVELOPE>"
		doc = self._file(raw)
		try:
			result = import_data.validate_file(doc.file_url)
			self.assertTrue(result["success"], result.get("error"))
			self.assertEqual(result["objects"], 1)
		finally:
			frappe.delete_doc("File", doc.name, force=True, ignore_permissions=True)
			frappe.db.commit()


class TestMissingConfigurationRefusesByName(IntegrationTestCase):
	def test_an_export_for_a_company_that_does_not_exist_is_refused(self):
		with self.assertRaises(Exception) as caught:
			export_api._resolve_company("No Such Company At All")
		self.assertIn("No Such Company At All", str(caught.exception))

	def test_a_company_with_no_data_produces_an_empty_export_not_an_error(self):
		"""A range holding nothing is an ordinary answer. Failing on it would
		make the first export of a new company look broken."""
		from tally_connector.tally.extract import extract

		export = extract(_company(), "1900-01-01", "1900-01-02")
		self.assertEqual(export.vouchers, [])
		# The masters are not dated, so they are still there - which is correct
		# and is why an empty voucher file is not an empty export.
		self.assertGreater(len(export.masters.ledgers), 0)


class TestAFailedJobSaysSo(IntegrationTestCase):
	def test_a_worker_that_dies_leaves_a_log_that_reads_as_failed(self):
		"""A log left saying "Processing Sales" for ever is how a dead job passes
		for a slow one - the single most misleading state this system can be in."""
		from tally_connector.api.export import _create_log
		from tally_connector.api.jobs import EXPORT_STAGES, JobReporter, running, status

		log = _create_log("Full Export", "Tally XML", None, None, _company())
		try:
			reporter = JobReporter("Tally Export Log", log, EXPORT_STAGES)
			reporter.start(total=500)
			reporter.at("Processing Sales", processed=120)
			self.assertIn(log, [r["name"] for r in running()])

			reporter.finish("Failed", error="the worker was restarted")
			row = status(log)
			self.assertEqual(row["status"], "Failed")
			self.assertFalse(row["running"])
			self.assertNotIn(log, [r["name"] for r in running()])
			self.assertIn("restarted", row["error"])
			# The counts it reached are kept rather than reset, because how far
			# it got is the first question somebody asks about a failed run.
			self.assertEqual(row["processed"], 120)
		finally:
			frappe.delete_doc("Tally Export Log", log, force=True, ignore_permissions=True)
			frappe.db.commit()


class TestAskingTallyDoesNotHang(IntegrationTestCase):
	"""A question and a transfer are not the same wait.

	Pushing a year of vouchers is Tally parsing tens of megabytes and may
	legitimately take a minute. Asking whether Tally is *there* is a handshake
	with a machine on the same LAN. Sharing one 60-second timeout meant the Test
	Connection button spun for a full minute whenever the accounts PC was off -
	which is most evenings, and which is exactly what somebody presses that
	button to find out. Measured, not assumed: this suite took 61 seconds before
	the split and under two after it.
	"""

	def test_the_probe_timeout_is_short_and_the_push_timeout_is_not(self):
		from tally_connector.utils.connection import TallyConnector

		conn = TallyConnector()
		self.assertLessEqual(conn.probe_timeout, 10, "asking Tally should not hang a screen")
		self.assertGreaterEqual(conn.timeout, 30, "a large push needs room to finish")
		self.assertLess(conn.probe_timeout, conn.timeout)

	def test_a_connection_test_against_a_black_hole_returns_quickly(self):
		"""198.51.100.0/24 is reserved for documentation and routes nowhere, so
		the packet is dropped rather than refused - which is the case that
		actually waits for a timeout."""
		import time

		settings = frappe.get_single("Tally Settings")
		host, port = settings.tally_host, settings.tally_port
		frappe.db.set_value(
			"Tally Settings", "Tally Settings", {"tally_host": "198.51.100.7", "tally_port": 9000}
		)
		frappe.db.commit()
		frappe.clear_document_cache("Tally Settings", "Tally Settings")
		try:
			start = time.perf_counter()
			result = export_api.test_tally_connection()
			elapsed = time.perf_counter() - start
			self.assertFalse(result["success"])
			self.assertLess(elapsed, 15, f"the connection test took {elapsed:.0f}s")
		finally:
			frappe.db.set_value("Tally Settings", "Tally Settings", {"tally_host": host, "tally_port": port})
			frappe.db.commit()
			frappe.clear_document_cache("Tally Settings", "Tally Settings")
