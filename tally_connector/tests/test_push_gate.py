"""
Nothing reaches Tally that this connector already knows Tally will refuse,
and nothing is called a success on the strength of the HTTP call returning.

Both were true until 20 September and both are on the record as having cost
real imports.

**The gate.** `run_full_export` pushed at one line and consulted the validator's
blocking findings twenty lines later, so a file carrying an ABORTS finding was
sent in full. Since TallyPrime stops at the first record it cannot accept, the
rest of the file went with it. The ten per-area endpoints and the Desk button on
an invoice never validated at all.

**The verdict.** A push was successful when `requests.post` returned. An answer
that could not be parsed was swallowed and read as a clean import of nothing;
`finish("Success")` then wrote over whatever the push had concluded.

These are integration tests because the gate lives in the API layer, but no
HTTP happens: the transport is replaced.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from tally_connector.api import export as api
from tally_connector.tally.validate import ABORTS, Finding
from tally_connector.utils import connection
from tally_connector.utils.connection import TallyConnector


def _ok(_xml, _log=None):
	return {
		"success": True,
		"reached": True,
		"unknown": False,
		"created": 3,
		"altered": 0,
		"ignored": 0,
		"errors": [],
		"last_voucher_id": "7",
		"response": "<RESPONSE><CREATED>3</CREATED></RESPONSE>",
	}


class _Recorder:
	"""Stands in for the transport and remembers whether it was used."""

	def __init__(self, outcome=None):
		self.calls = []
		self.outcome = outcome or _ok

	def __call__(self, xml, log=None):
		self.calls.append(xml)
		return self.outcome(xml, log)


class TestNothingKnownBadIsSent(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = frappe.db.get_value("Company", {}, "name")

	def _run(self, push=None, validate=None):
		recorder = _Recorder()
		# The destination is stubbed to an empty target here on purpose. These
		# cases are about the gate's behaviour, not about whatever this bench's
		# data happens to contain - with a real target they fail on genuine
		# findings of the site's own, which is the gate working and tells us
		# nothing about the gate.
		from tally_connector.tally.validate import TargetCompany

		patches = [
			patch.object(connection, "push_to_tally", recorder),
			patch.object(api, "target_company", return_value=TargetCompany(name="Co")),
		]
		if validate is not None:
			patches.append(patch.object(api, "validate", validate))
		for p in patches:
			p.start()
		try:
			out = api._run_export("chart_of_accounts", None, None, True, self.company)
		finally:
			for p in reversed(patches):
				p.stop()
		return out, recorder

	def test_a_clean_export_is_sent(self):
		"""The control. Without it the other two prove only that nothing works.

		Masters go a batch at a time now, so the count is one *or more* rather
		than exactly one - pinning it would make this fail the day the fixture
		chart grows past a batch, which says nothing about the gate.
		"""
		out, recorder = self._run()
		self.assertGreaterEqual(len(recorder.calls), 1, out.get("error"))
		self.assertTrue(out["success"])

	def test_an_aborting_finding_stops_the_push(self):
		def refuses(export, target=None):
			report = (
				api.validate.__wrapped__(export)
				if hasattr(api.validate, "__wrapped__")
				else _real_validate(export)
			)
			report.findings.append(
				Finding(
					ABORTS, "unit symbol", "Square Meter", "a space in a unit symbol aborts the whole file"
				)
			)
			return report

		out, recorder = self._run(validate=refuses)
		self.assertEqual(recorder.calls, [], "a known-bad file was sent to Tally")
		self.assertFalse(out["success"])
		self.assertIn("Square Meter", out["error"])
		self.assertIn("blocking", out["error"])

	def test_a_validator_that_raises_stops_the_push(self):
		"""A validator that cannot run is not a validator that passed."""

		def explodes(export, target=None):
			raise RuntimeError("validator exploded")

		out, recorder = self._run(validate=explodes)
		self.assertEqual(recorder.calls, [])
		self.assertFalse(out["success"])
		self.assertIn("could not be checked", out["error"])

	def test_a_push_tally_refused_is_not_a_success(self):
		def refused(_xml, _log=None):
			return {
				"success": False,
				"reached": True,
				"unknown": False,
				"created": 0,
				"altered": 0,
				"ignored": 0,
				"errors": ["Ledger 'ACME TRADERS 9000000000' does not exist!"],
				"last_voucher_id": "",
				"response": "<RESPONSE/>",
			}

		recorder = _Recorder(refused)
		with (
			patch.object(
				api,
				"target_company",
				return_value=__import__(
					"tally_connector.tally.validate", fromlist=["TargetCompany"]
				).TargetCompany(name="Co"),
			),
			patch.object(connection, "push_to_tally", recorder),
		):
			out = api._run_export("chart_of_accounts", None, None, True, self.company)
		self.assertFalse(out["success"])
		# A readable refusal is bisected now, so the count is no longer one -
		# what replaces it is stronger. Tally refuses and names nothing, so the
		# masters it would not take are sent again singly and come back **by
		# name**, which is what somebody actually needs. A count of requests
		# was never the property worth holding; knowing which record failed is.
		refused_names = [r["name"] for r in out["tally"]["refused"]]
		self.assertTrue(refused_names, "a refusal that named nothing at all")
		self.assertEqual(len(refused_names), out["records"])
		self.assertIn("Tally refused", out["error"])
		self.assertIn(repr(refused_names[0]), out["error"])

	def test_an_answer_that_could_not_be_read_is_not_a_success_either(self):
		"""And it is not a failure. Nobody knows, and saying `Failed` invites
		the retry that duplicates vouchers Tally may already hold."""

		def unreadable(_xml, _log=None):
			return {
				"success": False,
				"reached": True,
				"unknown": True,
				"created": 0,
				"altered": 0,
				"ignored": 0,
				"errors": ["Tally's answer could not be read as XML"],
				"last_voucher_id": "",
				"response": "<html>500</html>",
			}

		with (
			patch.object(
				api,
				"target_company",
				return_value=__import__(
					"tally_connector.tally.validate", fromlist=["TargetCompany"]
				).TargetCompany(name="Co"),
			),
			patch.object(connection, "push_to_tally", _Recorder(unreadable)),
		):
			out = api._run_export("chart_of_accounts", None, None, True, self.company)
		self.assertFalse(out["success"])
		self.assertIn("unknown", out["error"])


_real_validate = api.validate


class TestTallysAnswerIsReadHonestly(IntegrationTestCase):
	"""`parse_tally_response` used to swallow every exception, so anything that
	was not XML came back as `created=0, altered=0, errors=[]` - which the
	caller recorded as a clean import."""

	def setUp(self):
		self.conn = TallyConnector()

	def test_a_normal_answer_is_read(self):
		parsed = self.conn.parse_tally_response(
			"<RESPONSE><CREATED>5</CREATED><ALTERED>2</ALTERED>"
			"<ERRORS>0</ERRORS><LASTVCHID>119</LASTVCHID></RESPONSE>"
		)
		self.assertTrue(parsed["parsed"])
		self.assertEqual((parsed["created"], parsed["altered"]), (5, 2))
		self.assertEqual(parsed["last_voucher_id"], "119")
		self.assertEqual(parsed["errors"], [])

	def test_line_errors_are_collected(self):
		parsed = self.conn.parse_tally_response(
			"<RESPONSE><CREATED>0</CREATED><ERRORS>1</ERRORS>"
			"<LINEERROR>Ledger 'Sales' does not exist!</LINEERROR></RESPONSE>"
		)
		self.assertEqual(parsed["errors"], ["Ledger 'Sales' does not exist!"])

	def test_an_error_count_without_line_bodies_is_still_an_error(self):
		"""The count is authoritative. A run reporting errors and naming none
		used to look perfect."""
		parsed = self.conn.parse_tally_response("<RESPONSE><CREATED>4</CREATED><ERRORS>3</ERRORS></RESPONSE>")
		self.assertTrue(parsed["errors"])
		self.assertIn("3 errors", parsed["errors"][0])

	def test_exceptions_count_too(self):
		parsed = self.conn.parse_tally_response(
			"<RESPONSE><CREATED>1</CREATED><EXCEPTIONS>2</EXCEPTIONS></RESPONSE>"
		)
		self.assertTrue(parsed["errors"])

	def test_an_html_error_page_is_not_a_clean_import(self):
		parsed = self.conn.parse_tally_response("<html><body>500 Internal Server Error</body></html>")
		self.assertFalse(parsed["parsed"])
		self.assertTrue(parsed["errors"])

	def test_a_truncated_body_is_not_a_clean_import(self):
		parsed = self.conn.parse_tally_response("<RESPONSE><CREATED>5</CREA")
		self.assertFalse(parsed["parsed"])
		self.assertTrue(parsed["errors"])

	def test_an_empty_body_is_not_a_clean_import(self):
		for body in ("", "   ", None):
			with self.subTest(body=body):
				parsed = self.conn.parse_tally_response(body)
				self.assertFalse(parsed["parsed"])
				self.assertTrue(parsed["errors"])


class TestPushingOneDocumentByHand(IntegrationTestCase):
	"""The button somebody presses every day, rather than once at migration.

	It pushed straight through `push_to_tally`, which meant two things: it
	skipped the validation gate every other path runs, and it wrote nothing to
	the sync ledger - so pressing it twice posted the document to Tally twice,
	with nothing anywhere saying it had already gone. Duplicate protection
	matters most on the path used by hand.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = frappe.db.get_value("Company", {}, "name")

	def _an_invoice(self):
		"""One that has not already been sent.

		The first version of this took whichever invoice came first, and on
		this bench that one is already in the sync ledger from an earlier
		push - so the endpoint correctly reported "already sent" and the test
		read it as a broken code path. The fixture was wrong, not the code.
		"""
		sent = set(
			frappe.get_all(
				"Tally Sync Record", filters={"erpnext_company": self.company}, pluck="source_name"
			)
		)
		for name in frappe.get_all(
			"Sales Invoice", filters={"docstatus": 1, "company": self.company}, pluck="name", limit=50
		):
			if name not in sent:
				return name
		self.skipTest("Every sales invoice on this site has already been sent.")

	def test_a_refused_export_is_not_sent(self):
		"""The gate this path did not run."""
		from tally_connector.api import export as api

		recorder = _Recorder()
		with (
			patch.object(connection, "push_to_tally", recorder),
			patch.object(api, "_would_be_refused", return_value="nope, blocked"),
		):
			out = api.export_single_document("Sales Invoice", self._an_invoice(), push_to_tally_flag=True)
		self.assertFalse(out["success"])
		self.assertEqual(recorder.calls, [], "a blocked document was sent anyway")

	def test_the_push_goes_through_the_recorded_path(self):
		"""Not "did anything reach Tally" but "did it go the way that records".

		Asserting only that a second press sends nothing passes for the wrong
		reason when the push bypasses `push_vouchers` altogether - both presses
		then miss the seam being watched. Planted exactly that, and it sailed
		through until this asserted the positive.
		"""
		from tally_connector.api import export as api

		ok = {
			"success": True,
			"reached": True,
			"created": 1,
			"altered": 0,
			"ignored": 0,
			"unknown": False,
			"errors": [],
			"sent": 1,
		}
		with (
			patch.object(api, "push_vouchers", return_value=ok) as recorded,
			patch.object(api, "_would_be_refused", return_value=""),
		):
			out = api.export_single_document("Sales Invoice", self._an_invoice(), push_to_tally_flag=True)
		self.assertTrue(out["success"], out.get("error"))
		self.assertEqual(recorded.call_count, 1, "the push did not go through the path that records it")
		self.assertEqual(
			recorded.call_args.kwargs.get("erpnext_company"),
			self.company,
			"recorded against no company is a row nothing ever matches",
		)


class TestTheGateAsksTheDestination(IntegrationTestCase):
	"""A connected Tally full of the right masters refused its own data.

	`_would_be_refused` validated with no target, so every master already in
	Tally read as one that exists nowhere. On the real company that produced
	**7,581 blocking findings** - 4,798 ledgers, 1,365 units, 1,418 cost
	centres - naming masters Tally was holding at that moment, and the gate
	reads exactly these findings to decide whether to send. A report nobody
	can act on is a nuisance; a gate built on one stops the migration.
	"""

	def test_the_gate_passes_the_destination_to_the_validator(self):
		from tally_connector.api import export as api

		company = frappe.db.get_value("Company", {}, "name")
		seen = {}

		def spy(export, target=None):
			seen["target"] = target
			from tally_connector.tally.validate import Report

			return Report()

		with patch.object(api, "validate", spy):
			api._would_be_refused(None, None, company)
		self.assertIn("target", seen, "the validator was never called")
		self.assertIsNotNone(seen["target"], "the gate judged the export without asking what Tally holds")

	def test_asking_tally_is_the_default(self):
		"""It defaulted to not asking, which is the wrong way round when the
		whole point of a connected Tally is to be asked."""
		import inspect

		from tally_connector.api import export as api

		default = inspect.signature(api.target_company).parameters["ask_tally"].default
		self.assertTrue(default, "target_company still defaults to not asking")
