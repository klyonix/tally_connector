from typing import ClassVar

"""
The validator, offline. No site, no database, no Tally.

Every check gets a fixture that is deliberately wrong in exactly one way, and
the test asserts that check fires on it - and that the good scenarios stay
clean. A validator nobody has seen fail is a validator nobody should trust.
"""

import datetime
import json
import os
import unittest

from tally_connector.tally import golden
from tally_connector.tally.model import export_from_dict
from tally_connector.tally.read import malformed_amounts, read_export
from tally_connector.tally.render import render
from tally_connector.tally.validate import (
	ABORTS,
	REJECTS,
	WRONG,
	TargetCompany,
	validate,
)

BROKEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "broken")


def load_broken(name):
	with open(os.path.join(BROKEN, name + ".json"), encoding="utf-8") as fh:
		return export_from_dict(json.load(fh))


def checks_fired(report):
	return {f.check for f in report.findings}


class TestGoodScenariosAreClean(unittest.TestCase):
	def test_every_scenario_validates_except_the_one_that_should_not(self):
		for name in golden.scenarios():
			report = validate(golden.load(name))
			with self.subTest(scenario=name):
				if name == "unbalanced_voucher":
					self.assertIn("balance", checks_fired(report))
				else:
					self.assertTrue(
						report.ok,
						"\n".join(str(f) for f in report.findings),
					)

	def test_a_clean_report_says_what_it_checked(self):
		report = validate(golden.load("sale_domestic_gst"))
		self.assertTrue(report.ok)
		self.assertEqual(report.checked["vouchers"], 1)
		self.assertFalse(report.checked["target_company_known"])


class TestEveryCheckFires(unittest.TestCase):
	"""One deliberately broken fixture per check."""

	CASES: ClassVar[dict] = {
		"unbalanced": ("balance", REJECTS),
		"missing_ledger": ("closure", REJECTS),
		"missing_stock_item": ("closure", REJECTS),
		"illegal_unit_symbol": ("unit", ABORTS),
		"item_unit_not_created": ("unit", REJECTS),
		"stock_on_a_journal": ("invoice view", REJECTS),
		"no_bill_reference": ("bill-wise", WRONG),
		"bill_refs_do_not_total": ("bill-wise", WRONG),
		"no_party_flag": ("party", WRONG),
		"unknown_voucher_kind": ("voucher type", REJECTS),
		"blank_ledger_name": ("name", ABORTS),
		"duplicate_voucher_number": ("duplicate", WRONG),
		"vouchers_sharing_one_guid": ("re-import", WRONG),
		"journal_holding_a_bank_ledger": ("journal cash", REJECTS),
		"name_differs_only_by_whitespace": ("name collision", WRONG),
		"gst_ledger_outside_duties": ("gst ledger", WRONG),
		"gst_ledger_without_duty_head": ("gst ledger", WRONG),
		"taxable_item_without_hsn": ("hsn", WRONG),
		"cost_centre_without_a_master": ("cost centre", REJECTS),
		"cost_centre_allocations_do_not_total": ("cost centre", WRONG),
		"cost_centre_on_a_ledger_that_refuses_them": ("cost centre", REJECTS),
		"ledger_group_not_created": ("ledger parent", ABORTS),
		"stock_group_not_created": ("item parent", ABORTS),
		"registration_type_tally_refuses": ("registration type", REJECTS),
		"state_outside_india": ("state", REJECTS),
		"unit_symbol_with_a_space": ("unit symbol", ABORTS),
		"ledger_named_like_a_reserved_group": ("reserved name", ABORTS),
		"ledger_that_does_not_affect_stock": ("stock ledger", REJECTS),
	}

	def test_each_broken_fixture_is_caught_by_its_check(self):
		for name, (check, severity) in self.CASES.items():
			report = validate(load_broken(name))
			with self.subTest(fixture=name):
				hits = [f for f in report.findings if f.check == check]
				self.assertTrue(hits, f"{name}: no {check!r} finding. Got: {checks_fired(report) or 'none'}")
				self.assertIn(severity, {f.severity for f in hits})

	def test_there_is_a_fixture_for_every_check_that_can_have_one(self):
		"""A check with no broken fixture is a check nobody has seen fail."""
		covered = {check for check, _s in self.CASES.values()} | {"amount", "date"}
		every = set()
		for name in os.listdir(BROKEN):
			if name.endswith(".json"):
				every |= checks_fired(validate(load_broken(name[:-5])))
		self.assertEqual(sorted(every - covered), [])


class TestTallyMatchesNamesItsOwnWay(unittest.TestCase):
	"""Case and punctuation ignored, read off a real refusal: a ledger sent as
	`Stock In Hand` was refused as *"Improper change of Group Name for Ledger
	'Stock-in-Hand'!"*."""

	def test_the_two_spellings_tally_confused_are_one_key(self):
		from tally_connector.tally.extract_rules import tally_key

		self.assertEqual(tally_key("Stock In Hand"), tally_key("Stock-in-Hand"))
		self.assertEqual(tally_key("Cash In Hand"), tally_key("Cash-in-Hand"))
		self.assertNotEqual(tally_key("Stock In Hand - KTC"), tally_key("Stock-in-Hand"))

	def test_both_collisions_in_the_standard_chart_are_recognised(self):
		from tally_connector.utils.ledgers import reserved_group

		self.assertEqual(reserved_group("Stock In Hand"), "Stock-in-Hand")
		self.assertEqual(reserved_group("Cash In Hand"), "Cash-in-Hand")
		self.assertEqual(reserved_group("Stock Assets"), "")


class TestTargetCompanyChecks(unittest.TestCase):
	"""Dates and voucher types cannot be answered from ERPNext alone.

	Where nothing is known about the destination the checks are skipped, rather
	than guessing and reporting a problem that is not one.
	"""

	def test_dates_are_not_checked_when_the_target_is_unknown(self):
		report = validate(load_broken("date_before_books"))
		self.assertNotIn("date", checks_fired(report))
		self.assertFalse(report.checked["date_and_voucher_type_checked"])

	def test_a_date_before_the_books_begin_is_caught(self):
		target = TargetCompany(
			name="Demo Traders Pvt Ltd",
			known=True,
			books_from=datetime.date(2025, 4, 1),
			period_to=datetime.date(2027, 3, 31),
		)
		report = validate(load_broken("date_before_books"), target)
		hits = [f for f in report.findings if f.check == "date"]
		self.assertTrue(hits)
		self.assertEqual(hits[0].tally_says, "Date is out of range.")

	def test_a_date_after_the_companys_last_data_is_not_refused(self):
		"""`ENDINGAT` is where the data stops, not a ceiling on an import.

		Measured against a live TallyPrime: a new company reported `ENDINGAT`
		as 20250401 and accepted a voucher dated 2026-03-31, `created=1`. Read
		as a ceiling it refused every voucher of a full year, and `_would_be_refused`
		reads these findings, so the whole migration was blocked by a company
		that would have taken every one.
		"""
		export = golden.load("sale_domestic_gst")
		latest = max(v.date for v in export.vouchers)
		target = TargetCompany(
			name="Demo",
			known=True,
			books_from=latest - datetime.timedelta(days=365),
			period_to=latest - datetime.timedelta(days=1),
		)
		report = validate(export, target)
		self.assertEqual([f for f in report.findings if f.check == "date"], [])

	def test_a_voucher_type_the_company_lacks_is_caught(self):
		target = TargetCompany(name="Demo", known=True, voucher_types={"Purchase"})
		report = validate(golden.load("sale_domestic_gst"), target)
		hits = [f for f in report.findings if f.check == "voucher type"]
		self.assertTrue(hits)
		self.assertIn("does not exist", hits[0].tally_says)

	def test_a_voucher_type_tally_does_not_reserve_is_caught_with_no_probe(self):
		"""The eight kinds are settings, not constants.

		A company whose sales vouchers are called *Tax Invoice* types that into
		Tally Settings, and this export creates no voucher types - so the name
		has to already exist in the destination company. Nothing on this side
		can see whether it does, which is exactly why it is reported.
		"""
		export = golden.load("sale_domestic_gst")
		for voucher in export.vouchers:
			voucher.kind = "Tax Invoice"
		report = validate(export)
		hits = [f for f in report.findings if f.check == "voucher type"]
		self.assertTrue(hits)
		self.assertIn("reserves", hits[0].detail)

	def test_a_renamed_sales_voucher_still_carries_the_invoice_view(self):
		"""The name is not what makes a voucher an invoice.

		`is_invoice` used to be read off the voucher type's name, so renaming
		the sales voucher type in Settings dropped `ISINVOICE` from every stock
		invoice and Tally answered *"Can't mix Stock Items and Accounting
		Entries"*. The flag travels with the voucher now.
		"""
		export = golden.load("sale_domestic_gst")
		for voucher in export.vouchers:
			voucher.kind, voucher.is_invoice_kind = "Tax Invoice", True
		self.assertTrue(all(v.is_invoice for v in export.vouchers if v.inventory))
		self.assertNotIn("invoice view", checks_fired(validate(export)))

	def test_a_renamed_type_tally_holds_is_not_reported(self):
		"""Where Tally has been asked, its own list is the answer."""
		export = golden.load("sale_domestic_gst")
		for voucher in export.vouchers:
			voucher.kind, voucher.is_invoice_kind = "Tax Invoice", True
		target = TargetCompany(
			name="Demo",
			known=True,
			voucher_types={"Tax Invoice"},
			books_from=datetime.date(2020, 4, 1),
			period_to=datetime.date(2030, 3, 31),
		)
		self.assertNotIn("voucher type", checks_fired(validate(export, target)))

	def test_a_known_company_with_the_right_types_stays_clean(self):
		target = TargetCompany(
			name="Demo",
			known=True,
			voucher_types={"Sales", "Purchase"},
			books_from=datetime.date(2020, 4, 1),
			period_to=datetime.date(2030, 3, 31),
		)
		self.assertTrue(validate(golden.load("sale_domestic_gst"), target).ok)


class TestSeverity(unittest.TestCase):
	def test_blocking_findings_are_separated_from_wrong_books(self):
		report = validate(load_broken("no_bill_reference"))
		self.assertFalse(report.ok)
		self.assertEqual(report.blocking, [], "a missing bill reference imports fine; the books are wrong")

	def test_an_illegal_unit_aborts_rather_than_merely_rejects(self):
		"""One bad unit costs the whole masters file, not one record."""
		report = validate(load_broken("illegal_unit_symbol"))
		self.assertIn(ABORTS, {f.severity for f in report.findings if f.check == "unit"})

	def test_the_report_serialises(self):
		data = validate(load_broken("unbalanced")).to_dict()
		self.assertFalse(data["ok"])
		self.assertIn(REJECTS, data["counts"])
		json.dumps(data)


class TestReadBackFromXml(unittest.TestCase):
	"""The validator's second way in: XML that already exists.

	This is what lets stage 2 check the live export before stage 3 has bridged
	the model into it - the generators do not know the model, but their output
	is Tally XML and Tally XML reads into it.
	"""

	def test_a_rendered_export_reads_back_and_validates_clean(self):
		for name in golden.scenarios():
			if name == "unbalanced_voucher":
				continue
			masters, vouchers = render(golden.load(name))
			with self.subTest(scenario=name):
				report = validate(read_export(masters, vouchers))
				self.assertTrue(report.ok, "\n".join(str(f) for f in report.findings))

	def test_the_round_trip_keeps_what_the_checks_need(self):
		original = golden.load("sale_domestic_gst")
		masters, vouchers = render(original)
		back = read_export(masters, vouchers)

		self.assertEqual(back.company, original.company)
		self.assertEqual(len(back.vouchers), len(original.vouchers))
		a, b = original.vouchers[0], back.vouchers[0]
		self.assertEqual((a.kind, a.number, a.date, a.party), (b.kind, b.number, b.date, b.party))
		self.assertEqual([l.amount for l in a.lines], [l.amount for l in b.lines])
		self.assertEqual([l.is_party for l in a.lines], [l.is_party for l in b.lines])
		self.assertEqual(a.imbalance(), b.imbalance())
		self.assertEqual({u.symbol for u in original.masters.units}, {u.symbol for u in back.masters.units})
		self.assertEqual(
			{l.name for l in original.masters.ledgers if l.bill_wise},
			{l.name for l in back.masters.ledgers if l.bill_wise},
		)

	def test_a_malformed_amount_is_reported_rather_than_swallowed(self):
		"""`--1234.50` is the shape a return produced. The reader coerces it so the rest of the
		file can still be checked; this is how the evidence survives."""
		_masters, vouchers = render(golden.load("sale_domestic_gst"))
		broken_xml = vouchers.replace("<AMOUNT>-11800.00</AMOUNT>", "<AMOUNT>--11800.00</AMOUNT>", 1)
		self.assertEqual(malformed_amounts(broken_xml), ["--11800.00"])
		self.assertEqual(malformed_amounts(vouchers), [])

	def test_reading_does_not_care_which_exporter_wrote_the_file(self):
		"""No dependence on our own element ordering or attributes."""
		masters, vouchers = render(golden.load("contra_bank_transfer"))
		# A file Tally wrote carries a REMOTEID of its own and orders its
		# elements differently. Neither is anything the reader leans on.
		tallys = vouchers.replace("<VOUCHER VCHTYPE", '<VOUCHER REMOTEID="4f2a-0000d1c8" VCHTYPE')
		back = read_export(masters, tallys)
		self.assertEqual(back.vouchers[0].kind, "Contra")
		self.assertTrue(back.vouchers[0].balances())


class TestOffline(unittest.TestCase):
	def test_only_the_extract_layer_imports_frappe(self):
		"""The property the whole layering exists for.

		`tally/extract/` is the one package that reads ERPNext, and it is named
		here rather than pattern-matched, so a second impure module fails this
		test instead of quietly widening the exception. Everything else in the
		package stays runnable with no site, which is what keeps the offline
		suite offline.

		Asserted by reading the sources rather than by checking `sys.modules`,
		because `bench run-tests` has frappe imported already and the check would
		pass for the wrong reason - and offline is exactly the case bench cannot
		exercise. The walk goes into subpackages, so a reader added in a new
		folder is checked too.
		"""
		here = os.path.dirname(os.path.abspath(__file__))
		package = os.path.normpath(os.path.join(here, "..", "tally"))
		allowed = os.path.join(package, "extract")
		offenders = []
		for root, _dirs, files in os.walk(package):
			if root == allowed or root.startswith(allowed + os.sep):
				continue
			for name in sorted(files):
				if not name.endswith(".py"):
					continue
				path = os.path.join(root, name)
				with open(path, encoding="utf-8") as fh:
					for lineno, line in enumerate(fh, 1):
						if line.strip().startswith(("import frappe", "from frappe")):
							offenders.append(f"{os.path.relpath(path, package)}:{lineno}")
		self.assertEqual(
			offenders,
			[],
			"The pure layer imports frappe, so it can no longer be tested offline.",
		)

	def test_the_one_impure_module_is_still_impure(self):
		"""An allow-list that names a package which has stopped importing frappe
		is an exemption nobody notices is stale."""
		here = os.path.dirname(os.path.abspath(__file__))
		extract = os.path.normpath(os.path.join(here, "..", "tally", "extract"))
		self.assertTrue(os.path.isdir(extract))
		sources = [
			open(os.path.join(extract, name), encoding="utf-8").read()
			for name in os.listdir(extract)
			if name.endswith(".py")
		]
		self.assertTrue(any("import frappe" in source for source in sources))


if __name__ == "__main__":
	unittest.main()
