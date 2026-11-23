"""
Six places where the code did not do what it said, and nothing said so.

None of these failed a test or raised anything. That is what they have in
common and why they are together: each was a check that could not fire, a
lookup that could not match, or an argument that was accepted and ignored. A
defect that throws gets fixed the day it ships; these were all still here after
ten failed imports.

Pure where it can be. The two that read the site are marked.
"""

from __future__ import annotations


import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from tally_connector.tally import model
from tally_connector.tally.extract import unit_name, units_in_use
from tally_connector.tally.extract_rules import UNIT_SYMBOL_MAX, tally_key
from tally_connector.tally.validate import ABORTS, validate
from tally_connector.utils.ledgers import TALLY_RESERVED_GROUPS


class TestEveryFindingCanReachItsRecord(UnitTestCase):
    """Five of the nine link keys were a different spelling of the check they
    were meant to match - `duplicates` against `duplicate`, `bill_wise` against
    `bill-wise`, `dates` against `date`, and so on. Those findings silently
    linked to nothing, which looks exactly like a subject that did not resolve.
    """

    def test_every_link_key_is_a_check_the_validator_emits(self):
        import re

        from tally_connector.api.export import _LINKABLE
        from tally_connector.tally import validate as module

        source = open(module.__file__).read()
        emitted = set(re.findall(r'report\.add\(\s*\w+\s*,\s*"([^"]+)"', source))
        self.assertTrue(emitted, "no check names were found to compare against")
        unknown = sorted(set(_LINKABLE) - emitted)
        self.assertEqual(
            unknown, [],
            f"these link keys match no check the validator emits: {unknown}. "
            f"It emits {sorted(emitted)}")


class TestAValidatorThatCanActuallyFire(UnitTestCase):
    """`_check_currency` read `masters.currencies` through a `getattr` default,
    and `Masters` had no such field - so the check was written against
    something that did not exist and behaved as though everything collided."""

    def test_masters_really_carries_the_list_the_check_reads(self):
        self.assertIsInstance(model.Masters().currencies, list)

    def test_a_ledger_naming_a_currency_nothing_creates_is_refused(self):
        export = model.Export(company="Co")
        export.masters.ledgers = [
            model.Ledger(name="Acme Ltd", parent="Sundry Debtors", currency="USD")]
        findings = [f for f in validate(export).findings if f.check == "currency"]
        self.assertTrue(findings)
        self.assertEqual(findings[0].severity, ABORTS)

    def test_a_currency_the_export_creates_is_accepted(self):
        """The half that could never have worked: with no list on the model,
        nothing could ever be in it."""
        export = model.Export(company="Co")
        export.masters.currencies = ["USD"]
        export.masters.ledgers = [
            model.Ledger(name="Acme Ltd", parent="Sundry Debtors", currency="USD")]
        self.assertEqual(
            [f for f in validate(export).findings if f.check == "currency"], [])


class TestTheReaderSeesWhatTheWriterWrote(UnitTestCase):
    """The validator reads a model re-parsed from the written bytes, so a field
    the reader discards is invisible to every check that reads it. `GUID` was
    discarded, so the finding about a missing GUID could not fire on the
    preflight path whatever the file carried."""

    def test_a_guid_survives_the_round_trip_through_the_reader(self):
        import datetime
        from decimal import Decimal

        from tally_connector.tally.read import read_export
        from tally_connector.tally.render import render_masters, render_vouchers

        voucher = model.Voucher(kind="Sales", date=datetime.date(2026, 4, 8),
                                number="SINV-1", remote_id="SINV-1")
        voucher.lines.append(model.LedgerLine(ledger="Cash", amount=Decimal("-1")))
        voucher.lines.append(model.LedgerLine(ledger="Sales", amount=Decimal("1")))
        masters = model.Masters()
        masters.ledgers = [model.Ledger(name="Cash", parent="Cash-in-Hand"),
                           model.Ledger(name="Sales", parent="Sales Accounts")]

        read = read_export(render_masters("Co", masters),
                           render_vouchers("Co", [voucher], with_guid=True))
        self.assertEqual(read.vouchers[0].guid, voucher.tally_guid)
        self.assertTrue(read.vouchers[0].guid)


class TestTheUnitFallbackRespectsWhatIsTaken(IntegrationTestCase):
    """Reads the site. The fallback for a name that is not a UOM record skipped
    collision resolution, so it could mint a symbol a registered UOM already
    owned - and `extract_masters` then dropped the duplicate silently, leaving
    one symbol meaning two units."""

    def test_the_fallback_is_a_symbol_tally_accepts(self):
        for odd in ("///", "%%%", "   x   "):
            with self.subTest(uom=odd):
                symbol = unit_name(odd)
                if symbol:
                    self.assertNotRegex(symbol, r"[\s/]")
                    self.assertLessEqual(len(symbol), UNIT_SYMBOL_MAX)

    def test_it_does_not_take_a_symbol_a_registered_unit_owns(self):
        from tally_connector.tally.extract import _unit_symbols

        taken = set(_unit_symbols().values())
        self.assertNotIn(unit_name("///"), taken)


class TestUnitsInUseHonoursItsArguments(IntegrationTestCase):
    """Reads the site. It took three arguments and used none, while its own
    docstring said "the invoice lines in range" - so on a site with several
    companies it put one company's units into another company's Tally."""

    def _only_from_invoices(self):
        """A UOM an invoice line names and no item does.

        Derived rather than hardcoded, and it is what makes these tests able to
        fail at all: a unit that is on some item is in the answer whatever the
        filters do, so only this kind can show the filter working. The first
        version of this test compared lengths with `<=`, which held just as
        well with the filter deleted - the plant passed, which is what planting
        is for."""
        items = {u for u in frappe.get_all("Item", pluck="stock_uom",
                                           limit_page_length=0) if u}
        rows = frappe.db.sql("""SELECT DISTINCT c.uom FROM `tabSales Invoice Item` c
                                JOIN `tabSales Invoice` p ON p.name = c.parent""")
        only = {r[0] for r in rows if r[0]} - items
        if not only:
            self.skipTest("No UOM on this site is unique to an invoice line, "
                          "so the company and date filters cannot be observed.")
        return sorted(only)[0]

    def test_a_company_with_no_invoices_gets_no_invoice_only_units(self):
        unit = self._only_from_invoices()
        self.assertIn(unit, units_in_use())
        self.assertNotIn(unit, units_in_use(company="No Such Company Ltd"))

    def test_a_date_range_holding_no_invoices_excludes_them_too(self):
        unit = self._only_from_invoices()
        company = frappe.db.get_value("Company", {}, "name")
        self.assertNotIn(unit, units_in_use(company, "1900-01-01", "1900-01-02"))

    def test_one_companys_units_do_not_reach_anothers_tally(self):
        """The fault this had: on a site with several companies it read every
        invoice line on it."""
        companies = frappe.get_all("Company", pluck="name", limit_page_length=0)
        if len(companies) < 2:
            self.skipTest("This site has one company.")
        answers = {c: units_in_use(c) for c in companies}
        self.assertTrue(any(a != b for a in answers.values()
                            for b in answers.values()),
                        "every company got the same units, so nothing narrowed")

    def test_every_item_unit_survives_any_range(self):
        company = frappe.db.get_value("Company", {}, "name")
        item_units = {u for u in frappe.get_all(
            "Item", pluck="stock_uom", limit_page_length=0) if u}
        self.assertTrue(item_units <= units_in_use(company, "1900-01-01",
                                                   "1900-01-02"))


class TestTheReservedGroupListMatchesTally(UnitTestCase):
    """Verified against TallyPrime's own declaration.

    A real company's masters export carries `RESERVEDNAME` on the groups Tally
    owns: 28 of its 59 groups. Every one of them is in this list, and the list
    holds one more (`Bank OCC A/c`) that this particular company has not got.
    A superset is the right side to be on.

    **There is no equivalent list for stock groups, godowns, cost categories or
    units**, and none is invented here. No evidence for one exists in any file
    this project has - the company whose export was read uses no inventory and
    no cost centres - and inventing names is what put `@@DUPIGNORECOMBINE` into
    every masters file for a fortnight. What protects those namespaces is the
    destination snapshot, which asks rather than assumes.
    """

    TALLY_DECLARES = (
        "Branch / Divisions", "Capital Account", "Current Assets",
        "Current Liabilities", "Direct Expenses", "Direct Incomes",
        "Fixed Assets", "Indirect Expenses", "Indirect Incomes", "Investments",
        "Loans (Liability)", "Misc. Expenses (ASSET)", "Purchase Accounts",
        "Sales Accounts", "Suspense A/c", "Bank Accounts", "Bank OD A/c",
        "Cash-in-Hand", "Deposits (Asset)", "Duties & Taxes",
        "Loans & Advances (Asset)", "Provisions", "Reserves & Surplus",
        "Secured Loans", "Stock-in-Hand", "Sundry Creditors", "Sundry Debtors",
        "Unsecured Loans",
    )

    def test_every_group_tally_reserves_is_protected(self):
        ours = {tally_key(n) for n in TALLY_RESERVED_GROUPS}
        missing = sorted(n for n in self.TALLY_DECLARES
                         if tally_key(n) not in ours)
        self.assertEqual(missing, [])

    def test_the_list_is_a_superset_and_knows_it(self):
        self.assertGreaterEqual(len(TALLY_RESERVED_GROUPS),
                                len(self.TALLY_DECLARES))
