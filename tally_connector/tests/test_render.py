from frappe.tests import UnitTestCase

"""
The offline suite. No site, no database, no Tally.

Runnable two ways, and both matter:

    python -m unittest tally_connector.tests.test_render     # no frappe at all
    bench --site <site> run-tests --app tally_connector      # alongside the rest

Nothing here imports `frappe`. That is the property that makes it possible to
change a Tally mapping, run the tests in a second, and know what the XML became -
instead of deploying to a live site and reading Tally's event log afterwards.
"""

import datetime
import json
import os
import unittest
from decimal import Decimal

from lxml import etree

from tally_connector.tally import golden
from tally_connector.tally.model import (
	LedgerLine,
	Voucher,
	export_from_dict,
	money,
)
from tally_connector.tally.render import render, render_vouchers


class TestGoldenFiles(UnitTestCase):
	"""Every scenario renders exactly what was committed.

	A diff here is either a defect or an intended change; either way somebody
	reads it before it reaches a customer's books.
	"""

	def test_every_scenario_matches_its_golden_file(self):
		failures = golden.check()
		self.assertEqual(
			failures,
			[],
			"Rendered XML differs from the golden files. If the change is "
			"intended: python -m tally_connector.tally.golden --write, then "
			"read the diff before committing it.",
		)

	def test_there_are_scenarios_to_check(self):
		"""A golden suite that found no fixtures passes and proves nothing."""
		names = list(golden.scenarios())
		self.assertGreaterEqual(len(names), 10, f"only {len(names)} scenarios found")

	def test_rendering_is_deterministic(self):
		"""Same input, same bytes - twice. Otherwise the golden files are noise."""
		for name in golden.scenarios():
			with self.subTest(scenario=name):
				self.assertEqual(golden.rendered(name), golden.rendered(name))


class TestSignConvention(UnitTestCase):
	"""Tally reads the sign off the amount; ISDEEMEDPOSITIVE flags the debit side.

	Storing both separately is how they came to disagree, and how a purchase
	return produced `--1234.50` - a value Tally cannot parse, which aborted the
	whole import.
	"""

	def _voucher(self, amount):
		return Voucher(
			kind="Journal",
			number="X",
			date=datetime.date(2026, 4, 1),
			lines=[LedgerLine("A", money(amount)), LedgerLine("B", -money(amount))],
		)

	def test_no_amount_is_ever_double_negative(self):
		for name in golden.scenarios():
			with self.subTest(scenario=name):
				for _f, xml in golden.rendered(name).items():
					for el in etree.fromstring(xml.encode("utf-8")).iter("AMOUNT"):
						self.assertRegex(
							el.text or "0",
							r"^-?\d+\.\d{2}$",
							f"{el.text!r} is not a number Tally can read",
						)

	def test_debit_is_negative_and_flagged(self):
		xml = render_vouchers("Co", [self._voucher("100.00")])
		entries = etree.fromstring(xml.encode("utf-8")).findall(".//ALLLEDGERENTRIES.LIST")
		by_ledger = {e.findtext("LEDGERNAME"): e for e in entries}
		self.assertEqual(by_ledger["B"].findtext("AMOUNT"), "-100.00")
		self.assertEqual(by_ledger["B"].findtext("ISDEEMEDPOSITIVE"), "Yes")
		self.assertEqual(by_ledger["A"].findtext("AMOUNT"), "100.00")
		self.assertEqual(by_ledger["A"].findtext("ISDEEMEDPOSITIVE"), "No")


class TestVoucherBalance(UnitTestCase):
	"""The check Tally applies hardest: amounts must sum to zero."""

	def test_every_scenario_balances_except_the_one_that_should_not(self):
		for name in golden.scenarios():
			expected_bad = name == "unbalanced_voucher"
			with self.subTest(scenario=name):
				for voucher in golden.load(name).vouchers:
					if expected_bad:
						self.assertFalse(voucher.balances())
					else:
						self.assertTrue(
							voucher.balances(),
							f"{voucher.number} is out by {voucher.imbalance()} - "
							"Tally answers 'Total Debit and Credit amounts do not match'",
						)

	def test_an_unbalanced_voucher_is_representable(self):
		"""It must be, or the validator has nothing to report and the failure
		moves back to Tally's event log."""
		export = golden.load("unbalanced_voucher")
		self.assertEqual(export.vouchers[0].imbalance(), Decimal("-6916.06"))

	def test_inventory_counts_once(self):
		"""An inventory line carries its own accounting allocation. Counting the
		line and the allocation would double every invoice."""
		export = golden.load("sale_domestic_gst")
		self.assertTrue(export.vouchers[0].balances())


class TestInvoiceView(UnitTestCase):
	"""A voucher carrying stock must declare the invoice view, or Tally answers
	'Can't mix Stock Items and Accounting Entries'."""

	def test_stock_vouchers_declare_the_invoice_view(self):
		for name in golden.scenarios():
			for _f, xml in golden.rendered(name).items():
				for v in etree.fromstring(xml.encode("utf-8")).iter("VOUCHER"):
					has_stock = v.find("ALLINVENTORYENTRIES.LIST") is not None
					with self.subTest(scenario=name, voucher=v.findtext("VOUCHERNUMBER")):
						if has_stock:
							self.assertEqual(v.findtext("ISINVOICE"), "Yes")
							self.assertEqual(v.findtext("PERSISTEDVIEW"), "Invoice Voucher View")
						else:
							self.assertIsNone(v.find("ISINVOICE"))

	def test_the_ledger_tag_follows_the_voucher_mode(self):
		"""Two modes, two tags, and mixing them is how every invoice failed.

		The accounting half was measured against a live TallyPrime - the same
		voucher, changing nothing but the tag:

		    accounting, ALLLEDGERENTRIES.LIST   created=1
		    accounting, LEDGERENTRIES.LIST      created=0, EXCEPTIONS=1

		and that exception is a memory access violation, so Tally falls over
		rather than refusing and the gateway answers a count with no LINEERROR.

		That measurement was then applied to invoices as well, which it never
		covered, and every invoice-mode voucher failed afterwards. TallyPrime's
		own export of the destination company settles it - 4,682 vouchers it
		wrote itself, split with no exceptions:

		    invoice    (ISINVOICE=Yes)   1,141   LEDGERENTRIES.LIST
		    accounting                   3,541   ALLLEDGERENTRIES.LIST

		So this asserts the split rather than either tag alone. A voucher
		carrying both tags, or the wrong one for its mode, is the defect.
		"""
		seen = {"invoice": 0, "accounting": 0}
		for name in golden.scenarios():
			for _f, xml in golden.rendered(name).items():
				for v in etree.fromstring(xml.encode("utf-8")).iter("VOUCHER"):
					invoice = v.findtext("ISINVOICE") == "Yes"
					wanted = "LEDGERENTRIES.LIST" if invoice else "ALLLEDGERENTRIES.LIST"
					other = "ALLLEDGERENTRIES.LIST" if invoice else "LEDGERENTRIES.LIST"
					seen["invoice" if invoice else "accounting"] += 1
					with self.subTest(scenario=name, voucher=v.findtext("VOUCHERNUMBER")):
						self.assertTrue(v.findall(wanted), f"a voucher with lines must carry {wanted}")
						self.assertEqual(v.findall(other), [], f"{other} is the other mode's tag")

		# A split asserted over one mode is the mistake being corrected here,
		# so the scenarios have to cover both for this test to mean anything.
		self.assertTrue(seen["invoice"], "no invoice voucher in the scenarios")
		self.assertTrue(seen["accounting"], "no accounting voucher either")


class TestBillWise(UnitTestCase):
	"""A ledger with ISBILLWISEON obliges its vouchers to carry bill references,
	or the amount posts to On Account and the ageing is useless."""

	def test_party_lines_are_flagged_and_referenced(self):
		for name in golden.scenarios():
			export = golden.load(name)
			bill_wise = {l.name for l in export.masters.ledgers if l.bill_wise}
			for voucher in export.vouchers:
				for line in voucher.lines:
					if line.ledger in bill_wise:
						with self.subTest(scenario=name, voucher=voucher.number):
							self.assertTrue(line.is_party)
							self.assertTrue(line.bill_refs)

	def test_bill_references_sum_to_the_party_line(self):
		for name in golden.scenarios():
			for voucher in golden.load(name).vouchers:
				for line in voucher.lines:
					if line.bill_refs:
						with self.subTest(scenario=name, voucher=voucher.number):
							self.assertEqual(
								money(sum(r.amount for r in line.bill_refs)),
								money(line.amount),
								"bill references must account for the whole line",
							)

	def test_the_party_flag_reaches_the_xml(self):
		xml = golden.rendered("sale_domestic_gst")["sale_domestic_gst.vouchers.xml"]
		root = etree.fromstring(xml.encode("utf-8"))
		# Either tag: which one a voucher carries is its mode's business and
		# is asserted in TestInvoiceView. Naming one here made this fail when
		# the mode changed, which is a test reading the tag rather than the
		# flag it is about.
		party = [
			e
			for e in root.iter()
			if e.tag in ("ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST")
			and e.findtext("ISPARTYLEDGER") == "Yes"
		]
		self.assertEqual(len(party), 1)
		self.assertEqual(party[0].findtext("BILLALLOCATIONS.LIST/BILLTYPE"), "New Ref")


class TestEscapingAndStructure(UnitTestCase):
	def test_ampersands_and_brackets_survive(self):
		xml = golden.rendered("party_needing_escaping")["party_needing_escaping.vouchers.xml"]
		self.assertIn("&amp;", xml)
		self.assertNotIn("<as agreed>", xml)
		root = etree.fromstring(xml.encode("utf-8"))
		self.assertEqual(root.findtext(".//PARTYLEDGERNAME"), "A & B Enterprises")
		self.assertIn("<as agreed>", root.findtext(".//NARRATION"))

	def test_one_tallymessage_per_object(self):
		for name in golden.scenarios():
			for filename, xml in golden.rendered(name).items():
				root = etree.fromstring(xml.encode("utf-8"))
				for msg in root.iter("TALLYMESSAGE"):
					with self.subTest(document=filename):
						self.assertEqual(len(msg), 1)

	def test_dates_are_yyyymmdd(self):
		for name in golden.scenarios():
			for _f, xml in golden.rendered(name).items():
				for el in etree.fromstring(xml.encode("utf-8")).iter("DATE"):
					self.assertRegex(el.text, r"^\d{8}$")

	def test_each_document_asks_for_its_own_import(self):
		"""`All Masters`, then `Vouchers` - Tally's documented import
		definitions, and what a working import of 16,261 vouchers used. An
		export file's envelope says `All Masters` on vouchers too, and is not
		evidence about the import request; reading it as such cost a day."""
		masters, vouchers = render(golden.load("sale_domestic_gst"))
		self.assertEqual(etree.fromstring(masters.encode("utf-8")).findtext(".//REPORTNAME"), "All Masters")
		self.assertEqual(etree.fromstring(vouchers.encode("utf-8")).findtext(".//REPORTNAME"), "Vouchers")
		self.assertEqual(etree.fromstring(masters.encode("utf-8")).findall(".//VOUCHER"), [])

	def test_every_dated_list_opens_with_its_date(self):
		"""10,969 of 10,969 filled blocks in a real company's whole-year export
		open with APPLICABLEFROM. This export sent none, on 353 blocks, and the
		masters had never got far enough past the units for Tally to say so."""
		import re

		for name in golden.scenarios():
			masters, _v = render(golden.load(name))
			for tag in (
				"LEDGSTREGDETAILS.LIST",
				"LEDMAILINGDETAILS.LIST",
				"GSTDETAILS.LIST",
				"HSNDETAILS.LIST",
			):
				blocks = re.findall(rf"<{re.escape(tag)}>\s*<([A-Z]+)>", masters)
				with self.subTest(scenario=name, tag=tag):
					self.assertEqual(sorted(set(blocks) - {"APPLICABLEFROM"}), [])

	def test_hsn_is_in_its_own_list_and_says_it_is_given_here(self):
		"""HSNCODE appears nowhere inside GSTDETAILS in a real TallyPrime
		export; HSN lives in HSNDETAILS.LIST. Without the source saying the
		code is given here, Tally uses the group's."""
		masters, _v = render(golden.load("sale_domestic_gst"))
		root = etree.fromstring(masters.encode("utf-8"))
		item = next(root.iter("STOCKITEM"))
		self.assertIsNone(item.find("GSTDETAILS.LIST/HSNCODE"))
		self.assertEqual(item.findtext("HSNDETAILS.LIST/HSNCODE"), "84819090")
		self.assertEqual(item.findtext("HSNDETAILS.LIST/SRCOFHSNDETAILS"), "Specify Details Here")

	def test_a_voucher_names_the_registration_tag_tally_uses(self):
		"""`PARTYTAXREGISTRATIONTYPE` appears nought times in 300 MB of Tally's
		own export - two files, one of them a real company's whole year - and
		`GSTREGISTRATIONTYPE` appears 1,160 times at voucher level. This was a
		field of ours on Tally's object, like the `ADDRESS.LIST` beside it."""
		_m, vouchers = render(golden.load("sale_domestic_gst"))
		self.assertIn("<GSTREGISTRATIONTYPE>", vouchers)
		self.assertNotIn("PARTYTAXREGISTRATIONTYPE", vouchers)

	def test_a_voucher_carries_the_buyer_address_and_not_a_ledger_address(self):
		"""`ADDRESS.LIST` is a field of Tally's Ledger and appears nought times
		in its transactions export; `BASICBUYERADDRESS` appears 64 times, in
		exactly this shape."""
		_m, vouchers = render(golden.load("sale_domestic_gst"))
		self.assertIn('<BASICBUYERADDRESS.LIST TYPE="String">', vouchers)
		voucher = vouchers.split("<VOUCHER ", 1)[1]
		self.assertNotIn("<ADDRESS.LIST", voucher)

	def test_a_voucher_declares_its_view_the_way_tally_writes_it(self):
		"""`OBJVIEW` is an attribute on VOUCHER in Tally's own export, beside
		VCHTYPE and ACTION, and was a child element here."""
		_m, vouchers = render(golden.load("sale_domestic_gst"))
		root = etree.fromstring(vouchers.encode("utf-8"))
		voucher = next(root.iter("VOUCHER"))
		self.assertEqual(voucher.get("OBJVIEW"), "Invoice Voucher View")
		self.assertIsNone(voucher.find("OBJVIEW"))


class TestClosure(UnitTestCase):
	"""Every ledger a voucher names must be created by a master.

	Held offline now as well as against the site: this is what Tally reports as
	`Ledger '...' does not exist`, and it is the failure that follows any aborted
	masters import.
	"""

	def test_no_scenario_references_a_ledger_it_does_not_create(self):
		for name in golden.scenarios():
			export = golden.load(name)
			created = export.masters.ledger_names()
			referenced = set()
			for voucher in export.vouchers:
				referenced |= {l.ledger for l in voucher.lines}
				referenced |= {i.ledger for i in voucher.inventory}
				if voucher.party:
					referenced.add(voucher.party)
			with self.subTest(scenario=name):
				self.assertEqual(sorted(referenced - created), [])


class TestOffline(UnitTestCase):
	def test_only_the_extract_layer_imports_frappe(self):
		"""The property the whole layering exists for.

		`extract.py` is the one module that reads ERPNext, and it is named here
		rather than pattern-matched, so a second impure module fails this test
		instead of quietly widening the exception. Everything else in the package
		stays runnable with no site, which is what keeps the offline suite
		offline.

		Asserted by reading the sources rather than by checking `sys.modules`,
		because `bench run-tests` has frappe imported already and the check would
		pass for the wrong reason - and offline is exactly the case bench cannot
		exercise.
		"""
		here = os.path.dirname(os.path.abspath(__file__))
		package = os.path.normpath(os.path.join(here, "..", "tally"))
		allowed = {"extract.py"}
		offenders = []
		for name in sorted(os.listdir(package)):
			if not name.endswith(".py") or name in allowed:
				continue
			with open(os.path.join(package, name), encoding="utf-8") as fh:
				for lineno, line in enumerate(fh, 1):
					if line.strip().startswith(("import frappe", "from frappe")):
						offenders.append(f"{name}:{lineno}")
		self.assertEqual(
			offenders,
			[],
			"The pure layer imports frappe, so it can no longer be tested offline.",
		)

	def test_the_one_impure_module_is_still_impure(self):
		"""An allow-list that names a module which has stopped importing frappe
		is an exemption nobody notices is stale."""
		here = os.path.dirname(os.path.abspath(__file__))
		extract = os.path.normpath(os.path.join(here, "..", "tally", "extract.py"))
		self.assertTrue(os.path.exists(extract))
		with open(extract, encoding="utf-8") as fh:
			self.assertIn("import frappe", fh.read())


if __name__ == "__main__":
	unittest.main()
