"""Whether a voucher goes out carrying its items, and where that is decided.

A site may want accounting only, and there was no way to say so: `without_stock` existed, was tested, and had no
caller in any production path - the previous migration folded its vouchers
from a throwaway script. So the choice could not be made from the app, and a
push would have sent inventory whatever anybody had decided.

These hold the setting to one chokepoint. `extract_vouchers` and `extract`
between them are every path that produces a voucher (the per-area buttons, the
single document button, the masters slice and the full export), so folding
there is what makes the downloaded file, the pushed batches and what validate()
reads the same vouchers.

Pure: no site, no Tally. `_settings` is stood in for, because what is under
test is the decision rather than where the field is stored.
"""

from __future__ import annotations

import datetime
import unittest
from decimal import Decimal

from frappe.tests import IntegrationTestCase, UnitTestCase

from tally_connector.tally import extract as ex
from tally_connector.tally import model
from tally_connector.tally.validate import validate


class _Settings(dict):
	"""Enough of a Single to answer `.get` and attribute reads, including the
	case this is really about: the field being absent altogether."""

	def get(self, key, default=None):
		return super().get(key, default)

	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError:
			raise AttributeError(key) from None


def _sale(ledger="Sales"):
	v = model.Voucher(kind="Sales", date=datetime.date(2026, 3, 31), number="SINV-0001", party="Acme")
	v.lines.append(model.LedgerLine(ledger="Acme", amount=Decimal("-480.00"), is_party=True))
	v.lines.append(model.LedgerLine(ledger="Output Tax SGST", amount=Decimal("36.61")))
	v.lines.append(model.LedgerLine(ledger="Output Tax CGST", amount=Decimal("36.61")))
	v.inventory.append(
		model.InventoryLine(
			item="Support Service",
			ledger=ledger,
			quantity=Decimal("1"),
			rate=Decimal("406.78"),
			amount=Decimal("406.78"),
			unit="Nos",
		)
	)
	return v


class TestWhatTheSettingReads(UnitTestCase):
	def test_an_absent_field_reads_as_sending(self):
		"""A site that has not migrated must not silently change what it sends."""
		self.assertTrue(ex.sending_inventory(_Settings()))

	def test_ticked_sends(self):
		self.assertTrue(ex.sending_inventory(_Settings(send_voucher_inventory=1)))

	def test_unticked_does_not(self):
		self.assertFalse(ex.sending_inventory(_Settings(send_voucher_inventory=0)))


class TestTheFold(UnitTestCase):
	def test_off_drops_the_items_and_keeps_the_money(self):
		out = ex._as_configured([_sale()], _Settings(send_voucher_inventory=0))
		self.assertEqual(out[0].inventory, [])
		self.assertEqual(out[0].imbalance(), Decimal("0"))
		self.assertEqual(next(l.amount for l in out[0].lines if l.ledger == "Sales"), Decimal("406.78"))

	def test_off_takes_it_out_of_invoice_mode(self):
		"""The reason this shape imports: no LEDGERENTRIES, no AFFECTSSTOCK check."""
		self.assertTrue(_sale().is_invoice)
		out = ex._as_configured([_sale()], _Settings(send_voucher_inventory=0))
		self.assertFalse(out[0].is_invoice)

	def test_on_leaves_the_items_alone(self):
		out = ex._as_configured([_sale()], _Settings(send_voucher_inventory=1))
		self.assertEqual(len(out[0].inventory), 1)
		self.assertTrue(out[0].is_invoice)

	def test_a_stock_line_with_no_ledger_is_left_whole(self):
		"""Nowhere to fold to, so it fails loudly rather than losing the money."""
		out = ex._as_configured([_sale(ledger="")], _Settings(send_voucher_inventory=0))
		self.assertEqual(len(out[0].inventory), 1)


class TestEveryVoucherPathGoesThroughIt(IntegrationTestCase):
	"""The wiring, not the fold. Removing either call site fails here."""

	def setUp(self):
		self._sections = dict(ex.VOUCHER_SECTIONS)
		self._sources = ex.VOUCHER_SOURCES
		self._settings = ex._settings
		self._masters = ex.extract_masters
		ex.VOUCHER_SECTIONS["sales_invoices"] = (lambda company, f, t, only=None, tick=None: [_sale()], {})
		ex.VOUCHER_SOURCES = (
			("sales_invoices", "include_sales_invoice", lambda c, f, t, tick=None: [_sale()]),
		)
		ex._settings = lambda: _Settings(send_voucher_inventory=0, company_name_in_tally="DEMO TRADERS")
		ex.extract_masters = lambda *a, **k: model.Masters()

	def tearDown(self):
		ex.VOUCHER_SECTIONS.clear()
		ex.VOUCHER_SECTIONS.update(self._sections)
		ex.VOUCHER_SOURCES = self._sources
		ex._settings = self._settings
		ex.extract_masters = self._masters

	def test_the_per_area_and_single_document_path_folds(self):
		"""`extract_vouchers` is what voucher_section, document_parts and
		render_section all call."""
		out = ex.extract_vouchers("sales_invoices", "Co", None, None)
		self.assertEqual(out[0].inventory, [])

	def test_the_full_export_folds(self):
		self.assertEqual(ex.extract("Co", None, None).vouchers[0].inventory, [])

	def test_a_folded_export_marks_no_ledger_as_affecting_inventory(self):
		"""Marking reads the vouchers, so it must run after the fold. Marked,
		every ledger would go out with AFFECTSSTOCK Yes for no reason."""
		export = ex.extract("Co", None, None)
		self.assertEqual([l.name for l in export.masters.ledgers if getattr(l, "affects_stock", False)], [])

	def test_the_folded_ledger_is_still_created(self):
		"""Closure: the stock line's ledger arrives as an ordinary line and
		nothing else creates it."""
		export = ex.extract("Co", None, None)
		self.assertIn("Sales", export.masters.ledger_names())


class TestTheLossIsOnTheRecord(IntegrationTestCase):
	"""accounting.py says the loss must be said out loud rather than dropped
	quietly, and until now nothing said it anywhere."""

	def setUp(self):
		self._sources = ex.VOUCHER_SOURCES
		self._settings = ex._settings
		self._masters = ex.extract_masters
		ex.VOUCHER_SOURCES = (
			("sales_invoices", "include_sales_invoice", lambda c, f, t, tick=None: [_sale(), _sale()]),
		)
		ex.extract_masters = lambda *a, **k: model.Masters()

	def tearDown(self):
		ex.VOUCHER_SOURCES = self._sources
		ex._settings = self._settings
		ex.extract_masters = self._masters

	def _export(self, **settings):
		ex._settings = lambda: _Settings(company_name_in_tally="DEMO TRADERS", **settings)
		return ex.extract("Co", None, None)

	def test_a_folded_export_says_so_and_counts_what_went(self):
		export = self._export(send_voucher_inventory=0)
		self.assertTrue(export.accounting_only)
		self.assertEqual(export.stock_lines_folded, 2)

	def test_an_ordinary_export_claims_nothing(self):
		export = self._export(send_voucher_inventory=1)
		self.assertFalse(export.accounting_only)
		self.assertEqual(export.stock_lines_folded, 0)

	def test_an_empty_stock_summary_is_told_apart_from_nothing_sold(self):
		"""Both leave the inventory empty, and they are not the same fact."""
		self.assertTrue(self._export(send_voucher_inventory=0).accounting_only)
		ex.VOUCHER_SOURCES = (("sales_invoices", "include_sales_invoice", lambda c, f, t, tick=None: []),)
		self.assertFalse(self._export(send_voucher_inventory=1).accounting_only)

	def test_a_line_that_could_not_be_folded_is_not_counted_as_folded(self):
		ex.VOUCHER_SOURCES = (
			("sales_invoices", "include_sales_invoice", lambda c, f, t, tick=None: [_sale(ledger="")]),
		)
		export = self._export(send_voucher_inventory=0)
		self.assertEqual(export.stock_lines_folded, 0)

	def test_the_report_carries_it(self):
		checked = validate(self._export(send_voucher_inventory=0)).checked
		self.assertTrue(checked["accounting_only"])
		self.assertEqual(checked["stock_lines_folded"], 2)

	def test_folding_raises_no_finding(self):
		"""No money moved, so it is not a defect and must not read as one."""
		report = validate(self._export(send_voucher_inventory=0))
		self.assertEqual([f.check for f in report.findings if f.check == "hsn"], [])
		self.assertEqual(report.blocking, [])


class TestAnUpgradingSiteKeepsSendingItems(UnitTestCase):
	"""The guard in `sending_inventory` is not what protects an existing site,
	and this is the test that says so.

	`Tally Settings` is a Single. A Check added later has no row in
	`tabSingles`, and on a real site it was measured reading **0** rather
	than None before anything was set - so the None guard never fires and
	every upgrading site would have started folding its invoices by accident,
	with Tally's stock summary going empty and nothing to say why.

	What protects it is the backfill patch, so the field being listed there is
	the thing worth holding.
	"""

	def test_the_backfill_patch_names_the_field(self):
		from tally_connector.patches.v1_0.backfill_new_settings_defaults import (
			NEW_FIELDS,
		)

		self.assertIn("send_voucher_inventory", NEW_FIELDS)


class TestNoStockMastersAreCreatedForNothing(IntegrationTestCase):
	"""136 item masters, 7 groups and 6 units that no voucher names is 149
	records put into a customer's Tally for nothing."""

	def setUp(self):
		self._sources = ex.VOUCHER_SOURCES
		self._settings = ex._settings
		self._masters = ex.extract_masters
		ex.VOUCHER_SOURCES = (
			("sales_invoices", "include_sales_invoice", lambda c, f, t, tick=None: [_sale()]),
		)

		def masters(*a, **k):
			m = model.Masters()
			m.stock_items.append(model.StockItem(name="Support Service", parent="Services", base_unit="Nos"))
			m.stock_groups.append(model.StockGroup(name="Services"))
			m.units.append(model.Unit(symbol="Nos", formal_name="Numbers"))
			# A real ledger, with the two facts only `extract_masters` knows:
			# where it sits and what it opened at. Without one the closure
			# walk rebuilds the ledger list from what the vouchers reference,
			# so clearing the ledgers looks harmless and is not - it would
			# drop every parent and every opening balance on the site.
			m.ledgers.append(
				model.Ledger(name="Sales", parent="Sales Accounts", opening_balance=Decimal("-1234.00"))
			)
			return m

		ex.extract_masters = masters

	def tearDown(self):
		ex.VOUCHER_SOURCES = self._sources
		ex._settings = self._settings
		ex.extract_masters = self._masters

	def _masters_of(self, **settings):
		ex._settings = lambda: _Settings(company_name_in_tally="DEMO TRADERS", **settings)
		return ex.extract("Co", None, None).masters

	def test_sending_items_keeps_the_stock_masters(self):
		m = self._masters_of(send_voucher_inventory=1)
		self.assertEqual(len(m.stock_items), 1)
		self.assertEqual(len(m.stock_groups), 1)
		self.assertEqual(len(m.units), 1)

	def test_folding_drops_them(self):
		m = self._masters_of(send_voucher_inventory=0)
		self.assertEqual((m.stock_items, m.stock_groups, m.units), ([], [], []))

	def test_the_ledgers_are_untouched(self):
		"""Only the stock masters go. A chart of accounts is still a chart of
		accounts, with its parents and its opening balances."""
		folded = self._masters_of(send_voucher_inventory=0)
		sales = next(l for l in folded.ledgers if l.name == "Sales")
		self.assertEqual(sales.parent, "Sales Accounts")
		self.assertEqual(sales.opening_balance, Decimal("-1234.00"))
		self.assertEqual(
			sorted(l.name for l in folded.ledgers),
			sorted(l.name for l in self._masters_of(send_voucher_inventory=1).ledgers),
		)


if __name__ == "__main__":
	unittest.main()
