"""
Every master a file references but does not create.

Three real imports died on this one shape. A group, then a stock group, then a
currency: each time the export named something Tally owns, or something nothing
creates, and Tally stopped the file at that line and reported only that line.
This sweeps the rest of the family rather than waiting for a fourth.

What it found, on real data:

  * `PARTYTAXREGISTRATIONTYPE` and `GSTREGISTRATIONTYPE` carried `Overseas`,
    which is ERPNext's word. Tally validates that field against five of its own
    and `Overseas` is not one of them - nor are `Regular-SEZ`, `Deemed Exports
    - Refund`, `UIN Holders` or `Tax Deductor`, all of which the old table
    could produce.
  * `STATENAME` carried `California` and `North Carolina` on an Indian company,
    whose state list holds Indian states.
  * A voucher type is a master too, and the eight this connector emits are
    free-text settings - so a company calling its sales vouchers *Tax Invoice*
    was naming a master Tally may not have, and silently losing the invoice
    view with it.

The parents nothing had ever checked - a ledger's group and a stock item's
group - came back clean on this bench, which is the answer worth having and not
a reason to leave them unchecked.
"""

from typing import ClassVar

import frappe
from frappe.tests import IntegrationTestCase

from tally_connector.tally.extract import (
	_address,
	_gst_registration,
	_in_india,
	extract_masters,
)
from tally_connector.tally.model import REGISTRATION_TYPES
from tally_connector.tally.render import render_masters
from tally_connector.tally.validate import validate

# The complete GST Category list ERPNext and india_compliance offer, plus the
# two answers a field can give that are not on it.
GST_CATEGORIES = (
	"Registered Regular",
	"Registered Composition",
	"Unregistered",
	"SEZ",
	"Overseas",
	"Consumer",
	"Deemed Export",
	"UIN Holders",
	"Tax Deductor",
	"Tax Collector",
	"Input Service Distributor",
	"",
	None,
)

REFERENCE_CHECKS = (
	"ledger parent",
	"item parent",
	"registration type",
	"state",
	"group parent",
	"group cycle",
	"master parent",
	"master order",
	"currency",
	"unit",
	"closure",
)


def _companies():
	return frappe.get_all("Company", pluck="name")


class TestRegistrationTypeIsTallysWord(IntegrationTestCase):
	def test_every_gst_category_maps_onto_one_tally_accepts(self):
		"""Tally validates this field. ERPNext's list is longer than Tally's."""
		for category in GST_CATEGORIES:
			with self.subTest(category=category):
				self.assertIn(_gst_registration(category), REGISTRATION_TYPES)

	def test_the_site_has_no_category_this_table_has_never_seen(self):
		"""A category added to ERPNext falls through to `Regular`, which is a
		guess. This says whether the site is currently making it."""
		meta = frappe.get_meta("Address")
		field = meta.get_field("gst_category")
		if not field or not field.options:
			self.skipTest("india_compliance is not installed")
		options = [o for o in field.options.split("\n") if o.strip()]
		self.assertEqual(sorted(set(options) - set(GST_CATEGORIES)), [])

	def test_the_words_that_used_to_reach_tally_no_longer_do(self):
		"""The exact five values the old table produced."""
		for category in ("Overseas", "SEZ", "Deemed Export", "UIN Holders", "Tax Deductor"):
			with self.subTest(category=category):
				self.assertEqual(_gst_registration(category), "Regular")


class TestAStateOutsideIndia(IntegrationTestCase):
	RAW: ClassVar[dict] = {
		"address_line1": "1200 Bayshore Parkway",
		"city": "Mountain View",
		"pincode": "94043",
		"state": "California",
		"country": "United States",
	}

	def test_the_state_leaves_the_field_and_stays_in_the_address(self):
		address = _address(dict(self.RAW))
		self.assertEqual(address.state, "", "Tally reads STATENAME against an Indian company's own list")
		self.assertIn("California", address.lines, "the postal address must still say where it is")

	def test_an_indian_address_is_untouched(self):
		raw = dict(self.RAW, state="Tamil Nadu", country="India")
		self.assertEqual(_address(raw).state, "Tamil Nadu")

	def test_a_blank_country_counts_as_india(self):
		"""Most addresses on an Indian company never had the country filled in,
		and dropping their state would take it off most of the file."""
		self.assertTrue(_in_india(""))
		self.assertTrue(_in_india(None))
		raw = dict(self.RAW, state="Kerala", country="")
		self.assertEqual(_address(raw).state, "Kerala")

	def test_no_state_field_reaches_the_xml_for_a_us_party(self):
		from tally_connector.tally.model import Ledger, Masters

		masters = Masters(
			ledgers=[
				Ledger(name="Bayshore Systems Inc", parent="Sundry Debtors", address=_address(dict(self.RAW)))
			]
		)
		xml = render_masters("Demo Traders Pvt Ltd", masters)
		self.assertNotIn("California</PRIORSTATENAME>", xml)
		self.assertNotIn("<PLACEOFSUPPLY>California", xml)
		self.assertIn("California", xml, "still in the mailing address")


class TestThisBenchNamesNoMasterItDoesNotCreate(IntegrationTestCase):
	"""The sweep itself, over every company on this site."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.reports = {c: validate(_export(c)) for c in _companies()}

	def test_there_is_something_to_check(self):
		self.assertTrue(self.reports)
		self.assertTrue(any(r.checked["ledgers"] > 5 for r in self.reports.values()))

	def test_no_reference_to_a_master_nothing_creates(self):
		for company, report in self.reports.items():
			offenders = sorted(
				f"{f.check}: {f.subject}" for f in report.findings if f.check in REFERENCE_CHECKS
			)
			with self.subTest(company=company):
				self.assertEqual(offenders[:10], [])


def _export(company):
	from tally_connector.tally.model import Export

	return Export(company=company, masters=extract_masters(company))


class TestAnERPNextNameTallyReadsAsItsOwnGroup(IntegrationTestCase):
	"""ERPNext's standard chart has a group `Cash In Hand` and a ledger
	`Stock In Hand`; Tally has groups `Cash-in-Hand` and `Stock-in-Hand`, and
	matches names ignoring case and punctuation. The ledger stopped a real
	masters file at its 9,627th record."""

	def setUp(self):
		super().setUp()
		self.company = _companies()[0]
		self.abbr = frappe.get_cached_value("Company", self.company, "abbr")

	def test_a_group_named_like_tallys_becomes_tallys(self):
		from tally_connector.utils.ledgers import group_name

		self.assertEqual(group_name(f"Cash In Hand - {self.abbr}", self.company), "Cash-in-Hand")

	def test_a_ledger_named_like_tallys_group_keeps_its_suffix(self):
		"""ERPNext's own name for it, so it is still recognisable, and every
		voucher reaches the same function."""
		from tally_connector.utils.ledgers import ledger_name

		self.assertEqual(
			ledger_name(f"Stock In Hand - {self.abbr}", self.company), f"Stock In Hand - {self.abbr}"
		)
		self.assertEqual(ledger_name(f"Stock Adjustment - {self.abbr}", self.company), "Stock Adjustment")

	def test_this_bench_sends_no_master_tally_reads_as_its_own_group(self):
		from tally_connector.utils.ledgers import reserved_group

		for company in _companies():
			masters = extract_masters(company)
			offenders = sorted(r.name for r in (*masters.groups, *masters.ledgers) if reserved_group(r.name))
			with self.subTest(company=company):
				self.assertEqual(offenders, [])

	def test_nothing_is_left_under_a_group_that_is_not_sent(self):
		"""`Cash In Hand` is no longer created, so what was under it must be
		placed under Tally's `Cash-in-Hand` by Tally's spelling."""
		for company in _companies():
			masters = extract_masters(company)
			created = {g.name for g in masters.groups}
			from tally_connector.utils.ledgers import is_reserved_group

			orphans = sorted(
				f"{r.name} -> {r.parent}"
				for r in (*masters.groups, *masters.ledgers)
				if r.parent and r.parent not in created and not is_reserved_group(r.parent)
			)
			with self.subTest(company=company):
				self.assertEqual(orphans, [])


class TestTallysMoneyGroupsHoldOnlyMoney(IntegrationTestCase):
	"""Tally's cash and bank groups hold exactly what ERPNext types Cash or Bank.

	A real chart filed `Bad Debts`, an ordinary account, under ERPNext's
	`Cash In Hand` group. Tally reads that group as its own `Cash-in-Hand`, so
	the ledger became a cash ledger in Tally, and 140 write-off journals against
	it were journals Tally refuses. This builds the same chart here.
	"""

	@classmethod
	def setUpClass(cls):
		"""Once for the class: the rollback is per class, so an account made in
		`setUp` is still there for the second test and the insert collides."""
		super().setUpClass()
		from tally_connector.utils.ledgers import group_name

		company = _companies()[0]
		cls.company = company
		cash_group = next(
			(
				a.name
				for a in frappe.get_all(
					"Account", filters={"company": company, "is_group": 1}, fields=["name"]
				)
				if group_name(a.name, company) == "Cash-in-Hand"
			),
			None,
		)
		cls.account = None
		if cash_group:
			cls.account = (
				frappe.get_doc(
					{
						"doctype": "Account",
						"account_name": "Bad Debts Written Off Test",
						"parent_account": cash_group,
						"company": company,
						"is_group": 0,
						"account_type": "",
					}
				)
				.insert(ignore_permissions=True)
				.name
			)

	def setUp(self):
		if not self.account:
			self.skipTest("This chart has no group Tally reads as Cash-in-Hand.")
		from tally_connector.tally.extract import cash_and_bank

		# Frappe's request cache keys each entry by the function it wraps.
		frappe.local.request_cache.pop(cash_and_bank.__wrapped__, None)

	def _ledger(self):
		from tally_connector.utils.ledgers import ledger_name

		name = ledger_name(self.account, self.company)
		return next(l for l in extract_masters(self.company).ledgers if l.name == name)

	def test_an_ordinary_account_under_cash_in_hand_is_not_put_in_tallys_cash_group(self):
		from tally_connector.utils.ledgers import TALLY_PRIMARY

		ledger = self._ledger()
		self.assertNotEqual(ledger.parent, "Cash-in-Hand")
		self.assertEqual(
			ledger.parent, TALLY_PRIMARY["Asset"], "it is an asset in ERPNext's books and must stay one"
		)

	def test_every_ledger_in_a_money_group_is_money_in_erpnext(self):
		from tally_connector.tally.extract import _lands_in_money, cash_and_bank

		masters = extract_masters(self.company)
		parents = {g.name: g.parent for g in masters.groups}
		money = cash_and_bank(self.company)
		strays = sorted(
			l.name for l in masters.ledgers if _lands_in_money(l.parent, parents) and l.name not in money
		)
		self.assertEqual(strays, [])

	def test_a_write_off_journal_against_it_is_not_refused(self):
		from tally_connector.tally.model import Export, LedgerLine, Voucher
		from tally_connector.tally.model import money as m
		from tally_connector.tally.validate import validate

		masters = extract_masters(self.company)
		ledger = self._ledger()
		other = next(
			l.name for l in masters.ledgers if l.parent == "Sundry Debtors" or l.parent == "Indirect Expenses"
		)
		import datetime

		journal = Voucher(
			kind="Journal",
			number="JV-WRITE-OFF",
			date=datetime.date(2026, 3, 31),
			lines=[LedgerLine(ledger=ledger.name, amount=m(-100)), LedgerLine(ledger=other, amount=m(100))],
		)
		report = validate(Export(company=self.company, masters=masters, vouchers=[journal]))
		self.assertNotIn("journal cash", {f.check for f in report.findings})
