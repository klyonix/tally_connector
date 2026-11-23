"""
One verdict, and it means something.

The connector already had a report nothing acted on. The point of the
certification is that `BLOCKED` stops production XML being produced, and that
the reasons are legible to whoever has to clear them.

Two rules are easy to get backwards and are held here.

**An unasked question is not a passed one.** Where Tally was not asked, the
sections that depend on it read UNKNOWN rather than PASS.

**UNKNOWN does not block.** Tally being switched off has never been a reason to
refuse an offline export, and making it one would break the workflow this
connector exists for. Only FAIL blocks.

Pure: no site, no network.
"""

from __future__ import annotations

from frappe.tests import IntegrationTestCase, UnitTestCase

from tally_connector.tally.certify import (
	BLOCKED,
	FAIL,
	PASS,
	READY,
	UNKNOWN,
	WARN,
	certify,
)
from tally_connector.tally.snapshot import TallySnapshot, parse_companies
from tally_connector.tally.validate import ABORTS, WRONG, Finding, Report

COMPANIES = b"""<ENVELOPE><COMPANY NAME="Demo Traders Pvt Ltd"/></ENVELOPE>"""


def _report(*findings):
	report = Report()
	for severity, check, subject, detail in findings:
		report.add(severity, check, subject, detail)
	return report


def _cert(report=None, **kwargs):
	return certify(
		erpnext_company="Books Ltd", tally_company="Demo Traders Pvt Ltd", report=report or Report(), **kwargs
	)


def _section(cert, name):
	return next(s for s in cert.sections if s.name == name)


class TestTheVerdictMeansSomething(UnitTestCase):
	def test_a_clean_export_is_ready(self):
		cert = _cert()
		self.assertEqual(cert.verdict, READY)
		self.assertTrue(cert.ready)

	def test_a_blocking_finding_blocks(self):
		cert = _cert(_report((ABORTS, "reserved name", "Stock In Hand", "names a group Tally owns")))
		self.assertEqual(cert.verdict, BLOCKED)
		self.assertFalse(cert.ready)
		self.assertEqual(_section(cert, "System Masters").result, FAIL)

	def test_the_verdict_names_what_blocked_it(self):
		"""`Errors: 0` above `BLOCKED` reads as a contradiction unless the
		section is named."""
		cert = _cert(_report((ABORTS, "reserved name", "Stock In Hand", "names a group Tally owns")))
		self.assertIn("Blocked by: System Masters", cert.as_text())
		self.assertIn(BLOCKED, cert.as_text())

	def test_a_finding_tally_accepts_warns_rather_than_blocking(self):
		"""A taxable item with no HSN imports perfectly and then drops the
		voucher into Uncertain Transactions. Real, and not a reason to refuse
		the file - blocking on it would mean nothing ever ships, and calling it
		PASS would be dishonest, so it is its own word."""
		cert = _cert(_report((WRONG, "hsn", "Widget", "taxable stock item carries no HSN or SAC code")))
		self.assertEqual(_section(cert, "GST").result, WARN)
		self.assertEqual(cert.verdict, READY)
		self.assertIn("Accepted with warnings: GST", cert.as_text())
		self.assertTrue(cert.warnings)

	def test_a_warning_never_lands_in_the_errors(self):
		cert = _cert(_report((WRONG, "hsn", "Widget", "no HSN")))
		self.assertEqual(cert.errors, [])

	def test_the_text_carries_every_section_the_brief_asks_for(self):
		text = _cert().as_text()
		for name in (
			"Connectivity",
			"Company Mapping",
			"Voucher Types",
			"System Masters",
			"UOM",
			"Ledgers",
			"Parties",
			"Stock Items",
			"GST",
			"Transaction Validation",
			"Debit/Credit Validation",
			"Bill Allocation Validation",
			"Duplicate Validation",
			"Overall",
		):
			with self.subTest(section=name):
				self.assertIn(name, text)

	def test_a_blocking_finding_no_section_claims_is_still_reported(self):
		"""A check added later must not become invisible by not being listed."""
		cert = _cert(_report((ABORTS, "something new", "X", "a new check")))
		self.assertEqual(_section(cert, "Other").result, FAIL)
		self.assertEqual(cert.verdict, BLOCKED)


class TestAnUnaskedQuestionIsNotAPassedOne(UnitTestCase):
	def test_without_tally_the_dependent_sections_are_unknown(self):
		cert = _cert()
		for name in ("Connectivity", "Company Mapping", "UOM", "Ledgers", "Parties", "Stock Items"):
			with self.subTest(section=name):
				self.assertEqual(_section(cert, name).result, UNKNOWN)

	def test_unknown_does_not_block(self):
		"""Tally switched off has never stopped an offline export."""
		self.assertEqual(_cert().verdict, READY)

	def test_a_tally_that_cannot_be_reached_is_unknown_and_not_failed(self):
		cert = _cert(reachable=False)
		self.assertEqual(_section(cert, "Connectivity").result, UNKNOWN)
		self.assertEqual(cert.verdict, READY)


class TestTheCompanyWeWouldWriteTo(IntegrationTestCase):
	def test_a_matching_company_passes(self):
		snap = TallySnapshot(companies=parse_companies(COMPANIES))
		cert = _cert(reachable=True, snapshot=snap)
		self.assertEqual(_section(cert, "Company Mapping").result, PASS)

	def test_a_company_tally_has_not_got_blocks(self):
		"""A mismatch imports nothing and used to report success."""
		snap = TallySnapshot(companies=["Some Other Company"])
		cert = certify(
			erpnext_company="Books Ltd",
			tally_company="Demo Traders Pvt Ltd",
			report=Report(),
			reachable=True,
			snapshot=snap,
		)
		self.assertEqual(_section(cert, "Company Mapping").result, FAIL)
		self.assertEqual(cert.verdict, BLOCKED)

	def test_a_tally_that_did_not_list_its_companies_is_unknown(self):
		cert = _cert(reachable=True, snapshot=TallySnapshot())
		self.assertEqual(_section(cert, "Company Mapping").result, UNKNOWN)
		self.assertEqual(cert.verdict, READY)


class TestResolutionReachesTheReport(UnitTestCase):
	def setUp(self):
		from tally_connector.tally import model
		from tally_connector.tally.resolve import resolve

		masters = model.Masters()
		masters.ledgers = [model.Ledger(name="Acme Ltd", parent="Sundry Creditors")]
		self.snapshot = TallySnapshot(
			ledgers={"acmeltd": type("M", (), {"name": "Acme Ltd", "parent": "Sundry Debtors"})()},
			groups={},
			units={},
			stock_groups={},
			stock_items={},
			godowns={},
			cost_centres={},
			companies=["Demo Traders Pvt Ltd"],
		)
		self.resolution = resolve(masters, self.snapshot)

	def test_a_master_conflict_blocks_and_explains(self):
		cert = certify(
			erpnext_company="Books Ltd",
			tally_company="Demo Traders Pvt Ltd",
			report=Report(),
			resolution=self.resolution,
			snapshot=self.snapshot,
			reachable=True,
		)
		self.assertEqual(_section(cert, "Ledgers").result, FAIL)
		self.assertEqual(cert.verdict, BLOCKED)
		self.assertTrue(any("Sundry Debtors" in e for e in cert.errors))

	def test_the_counts_say_what_would_happen_to_each_master(self):
		cert = certify(
			erpnext_company="Books Ltd",
			tally_company="Demo Traders Pvt Ltd",
			report=Report(),
			resolution=self.resolution,
			snapshot=self.snapshot,
			reachable=True,
		)
		self.assertIn("Conflict", _section(cert, "Ledgers").counts)
