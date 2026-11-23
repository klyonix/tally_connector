"""
Signs, currency and the naming authority - against real ERPNext data.

These are integration tests on purpose. The offline suite proves the renderer
obeys Tally's sign convention; this proves the *extraction* hands it the right
numbers, which is where all four defects actually were:

  * a return exported as a Sales voucher with a minus prefixed onto an already
    negative string, producing `--1234.50`, which Tally cannot parse at all;
  * account-currency figures inside a base-currency voucher, putting 3,276.00
    USD against 286,813.80 INR;
  * a grand total ERPNext rounded independently of its own lines, leaving a
    voucher out by a paisa;
  * a stock item renamed after its invoices were raised, so the voucher and the
    master disagree about what it is called.

The property under test is the one Tally enforces: every voucher balances, every
amount parses, and every name a voucher uses is a name a master creates.
"""

import re

import frappe
from frappe.tests import IntegrationTestCase
from lxml import etree

from tally_connector.tally import extract
from tally_connector.tally.extract import stock_item_name
from tally_connector.tally.read import malformed_amounts, read_export
from tally_connector.tally.validate import ABORTS, REJECTS, validate
from tally_connector.utils.exporter import render_document, write_full_export

NUMBER = re.compile(r"^-?\d+\.\d{2}$")


def _companies():
	return frappe.get_all("Company", pluck="name")


def _export(company):
	"""The whole export, including vouchers already sent.

	`write_full_export` leaves out what the sync ledger says has reached Tally,
	which is right for a person exporting and wrong for a test of how a voucher
	renders. Once a site's year was sent, every voucher these
	tests look for was recorded as sent, and three of them failed looking for a
	payment and two journals that the export had correctly dropped.
	"""
	import os
	import tempfile
	from unittest.mock import patch

	with (
		tempfile.TemporaryDirectory() as tmp,
		patch(
			"tally_connector.utils.exporter.unsent_vouchers",
			side_effect=lambda vouchers, *a, **k: (list(vouchers), []),
		),
	):
		m, v = os.path.join(tmp, "m.xml"), os.path.join(tmp, "v.xml")
		write_full_export(None, None, company, m, v)
		return open(m, encoding="utf-8").read(), open(v, encoding="utf-8").read()


class TestNothingBlocksTheImport(IntegrationTestCase):
	"""The whole point of stage 3: no finding that costs the file."""

	def test_no_company_produces_a_blocking_finding(self):
		for company in _companies():
			masters, vouchers = _export(company)
			report = validate(read_export(masters, vouchers))
			blocking = [f for f in report.blocking]
			with self.subTest(company=company):
				self.assertEqual(
					[str(f) for f in blocking],
					[],
					"these would stop or be refused by Tally",
				)

	def test_no_amount_is_unparseable(self):
		for company in _companies():
			masters, vouchers = _export(company)
			with self.subTest(company=company):
				self.assertEqual(malformed_amounts(masters), [])
				self.assertEqual(malformed_amounts(vouchers), [])

	def test_every_amount_is_a_plain_signed_number(self):
		"""`--1234.50` passes a naive float() check in some locales and is not
		a number Tally reads. The shape is asserted, not just parseability."""
		for company in _companies():
			_m, vouchers = _export(company)
			for el in etree.fromstring(vouchers.encode("utf-8")).iter("AMOUNT"):
				with self.subTest(company=company):
					self.assertRegex(el.text or "0.00", NUMBER)


class TestSignsFollowTheNumber(IntegrationTestCase):
	def test_the_debit_flag_never_disagrees_with_the_amount(self):
		"""ISDEEMEDPOSITIVE is a restatement of the sign, not a second fact."""
		for company in _companies():
			_m, vouchers = _export(company)
			root = etree.fromstring(vouchers.encode("utf-8"))
			for tag in ("LEDGERENTRIES.LIST", "ALLLEDGERENTRIES.LIST", "ACCOUNTINGALLOCATIONS.LIST"):
				for el in root.iter(tag):
					amount = float(el.findtext("AMOUNT") or 0)
					flag = el.findtext("ISDEEMEDPOSITIVE")
					if not amount:
						continue
					with self.subTest(company=company, ledger=el.findtext("LEDGERNAME")):
						self.assertEqual(flag, "Yes" if amount < 0 else "No")

	def test_a_return_is_a_credit_or_debit_note(self):
		returns = {}
		for doctype, kind in (("Sales Invoice", "Credit Note"), ("Purchase Invoice", "Debit Note")):
			names = frappe.get_all(doctype, filters={"docstatus": 1, "is_return": 1}, pluck="name", limit=1)
			if not names:
				continue
			doc = frappe.db.get_value(doctype, names[0], ["company", "posting_date"], as_dict=True)
			xml, count = render_document(doctype, names[0], str(doc.posting_date), doc.company)
			self.assertEqual(count, 1)
			root = etree.fromstring(xml.encode("utf-8"))
			returns[doctype] = root.findtext(".//VOUCHERTYPENAME")
			with self.subTest(doctype=doctype):
				self.assertEqual(returns[doctype], kind)
		if not returns:
			self.skipTest("No returns on this site.")

	def test_the_return_mapping_holds_even_where_the_data_does_not(self):
		"""The test above can only assert what this bench happens to hold - four
		purchase returns and no sales returns - so a sales-side regression would
		skip rather than fail. The mapping itself is asserted here, and the
		offline suite carries a fixture for each direction.
		"""
		settings = frappe.get_single("Tally Settings")
		self.assertEqual(settings.get("credit_note_voucher_type") or "Credit Note", "Credit Note")
		self.assertEqual(settings.get("debit_note_voucher_type") or "Debit Note", "Debit Note")
		source = __import__("inspect").getsource(extract._sales)
		self.assertIn("is_return", source, "the sales reader no longer branches on is_return")


class TestBaseCurrency(IntegrationTestCase):
	def test_a_multi_currency_voucher_balances(self):
		"""`paid_amount` is in the account's currency; mixing it with a
		base-currency figure put 3,276.00 USD against 286,813.80 INR."""
		rows = frappe.db.sql(
			"""select name, company from `tabPayment Entry`
               where docstatus = 1 and paid_from_account_currency
                     != paid_to_account_currency limit 1""",
			as_dict=True,
		)
		if not rows:
			self.skipTest("No multi-currency payment on this site.")
		masters, vouchers = _export(rows[0].company)
		export = read_export(masters, vouchers)
		matching = [v for v in export.vouchers if v.remote_id == rows[0].name]
		self.assertTrue(matching, f"{rows[0].name} is not in the export")
		self.assertTrue(matching[0].balances(), f"out by {matching[0].imbalance()}")

	def test_a_multi_currency_journal_balances(self):
		rows = frappe.db.sql(
			"""select ja.parent, je.company from `tabJournal Entry Account` ja
               join `tabJournal Entry` je on je.name = ja.parent
               where je.docstatus = 1 and ja.exchange_rate != 1 limit 1""",
			as_dict=True,
		)
		if not rows:
			self.skipTest("No multi-currency journal on this site.")
		masters, vouchers = _export(rows[0].company)
		export = read_export(masters, vouchers)
		matching = [v for v in export.vouchers if v.remote_id == rows[0].parent]
		self.assertTrue(matching)
		self.assertTrue(matching[0].balances(), f"out by {matching[0].imbalance()}")


class TestNamingAuthority(IntegrationTestCase):
	def test_a_voucher_never_names_a_stock_item_the_master_does_not_create(self):
		for company in _companies():
			masters, vouchers = _export(company)
			export = read_export(masters, vouchers)
			created = {i.name for i in export.masters.stock_items}
			used = {i.item for v in export.vouchers for i in v.inventory}
			with self.subTest(company=company):
				self.assertEqual(sorted(used - created), [])

	def test_the_name_comes_from_the_code_not_the_invoice_line(self):
		"""An invoice line carries the item name as it was when the invoice was
		raised. Renaming the Item is what made the two disagree."""
		rows = frappe.db.sql(
			"""select item_code, item_name from `tabSales Invoice Item`
               where item_code != item_name limit 1""",
			as_dict=True,
		)
		if not rows:
			self.skipTest("No invoice line whose item has been renamed.")
		current = frappe.db.get_value("Item", rows[0].item_code, "item_name")
		self.assertEqual(stock_item_name(rows[0].item_code, rows[0].item_name), current)


class TestVoucherNumbersAreUnique(IntegrationTestCase):
	def test_no_two_vouchers_share_a_number(self):
		"""A supplier's bill number is not unique - two purchase invoices from
		one supplier shared a bill number - and re-importing a shared number
		creates a second voucher."""
		for company in _companies():
			masters, vouchers = _export(company)
			export = read_export(masters, vouchers)
			seen = {}
			for v in export.vouchers:
				seen.setdefault((v.kind, v.number), []).append(v.remote_id)
			duplicates = {k: v for k, v in seen.items() if len(v) > 1}
			with self.subTest(company=company):
				self.assertEqual(duplicates, {})


class TestVoucherTypeMapping(IntegrationTestCase):
	"""Tally's taxonomy is about what a voucher touches, not what ERPNext called it.

	Its Journal is for adjustments and excludes cash and bank ledgers unless
	`Allow Cash Accounts in Journals` is switched on in F12, which is off on a
	fresh company. ERPNext's six journal types were all collapsed into Journal
	and every bank statement line went the same way, so 520 of 1,222 vouchers -
	43% - were Journals holding a bank ledger.
	"""

	def test_no_journal_holds_a_cash_or_bank_ledger(self):
		from tally_connector.tally.extract import cash_and_bank

		for company in _companies():
			money = set(cash_and_bank(company))
			if not money:
				continue
			_m, vouchers = _export(company)
			root = etree.fromstring(vouchers.encode("utf-8"))
			offenders = []
			for v in root.iter("VOUCHER"):
				if v.findtext("VOUCHERTYPENAME") != "Journal":
					continue
				for el in v.iter("LEDGERNAME"):
					if el.text in money:
						offenders.append((v.findtext("VOUCHERNUMBER"), el.text))
						break
			with self.subTest(company=company):
				self.assertEqual(offenders[:5], [], f"{len(offenders)} such vouchers")

	def test_money_ledgers_sit_under_tallys_reserved_groups(self):
		"""Tally only offers a ledger for bank reconciliation, accepts it on a
		Contra, or excludes it from a Journal if it sits under one of its own
		money groups - whatever ERPNext's chart says."""
		from tally_connector.tally.extract import cash_and_bank
		from tally_connector.utils.ledgers import CASH_BANK_GROUPS

		for company in _companies():
			money = set(cash_and_bank(company))
			if not money:
				continue
			masters, _v = _export(company)
			root = etree.fromstring(masters.encode("utf-8"))
			for el in root.iter("LEDGER"):
				if el.get("NAME") in money:
					with self.subTest(company=company, ledger=el.get("NAME")):
						self.assertIn(el.findtext("PARENT"), CASH_BANK_GROUPS)

	def test_an_erpnext_contra_entry_becomes_a_tally_contra(self):
		rows = frappe.get_all(
			"Journal Entry",
			filters={"docstatus": 1, "voucher_type": "Contra Entry"},
			fields=["name", "company"],
			limit=1,
		)
		if not rows:
			self.skipTest("No contra entry on this site.")
		_m, vouchers = _export(rows[0].company)
		root = etree.fromstring(vouchers.encode("utf-8"))
		match = [v for v in root.iter("VOUCHER") if v.findtext("VOUCHERNUMBER") == rows[0].name]
		self.assertTrue(match)
		self.assertEqual(match[0].findtext("VOUCHERTYPENAME"), "Contra")

	def test_a_bank_statement_line_is_a_receipt_or_a_payment(self):
		"""It always touches a bank ledger, so it can never be a Journal.

		Only a line with something still unallocated is sent: a reconciled one
		is already in the books through the payment or journal it matched.
		"""
		for company in _companies():
			names = frappe.get_all(
				"Bank Transaction",
				filters={"docstatus": 1, "company": company, "unallocated_amount": [">", 0]},
				pluck="name",
				limit=1,
			)
			if not names:
				continue
			_m, vouchers = _export(company)
			root = etree.fromstring(vouchers.encode("utf-8"))
			match = [v for v in root.iter("VOUCHER") if v.findtext("VOUCHERNUMBER") == names[0]]
			with self.subTest(company=company):
				self.assertTrue(match)
				self.assertIn(match[0].findtext("VOUCHERTYPENAME"), ("Receipt", "Payment"))

	def test_the_rule_is_driven_by_the_ledgers_not_the_erpnext_label(self):
		from tally_connector.tally.extract import voucher_kind_for

		settings = frappe.get_single("Tally Settings")
		money = {"HDFC Bank": "Bank Accounts", "Cash": "Cash-in-Hand"}
		# money only, both sides
		self.assertEqual(voucher_kind_for([("HDFC Bank", -100), ("Cash", 100)], money, settings), "Contra")
		# money in
		self.assertEqual(voucher_kind_for([("HDFC Bank", -100), ("Sales", 100)], money, settings), "Receipt")
		# money out
		self.assertEqual(voucher_kind_for([("HDFC Bank", 100), ("Rent", -100)], money, settings), "Payment")
		# no money at all
		self.assertEqual(voucher_kind_for([("Rent", -100), ("Outstanding", 100)], money, settings), "Journal")
