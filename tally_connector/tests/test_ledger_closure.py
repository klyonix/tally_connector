"""
Closure between the vouchers and the masters.

This is the guard for the defect that made the export unusable: 1,489 of 3,996
voucher lines - 37% - referenced a ledger that no master created, so Tally
refused better than a third of the books. Three separate causes, none of them
visible without importing into Tally and reading the LINEERROR list:

* the Chart of Accounts named a ledger `Account.account_name` while every
  voucher referenced `Account.name` minus the company suffix;
* group accounts were excluded from the export while settings pointed at one;
* the suspense, TDS, sales and purchase ledgers and every Employee party were
  referenced and created by nothing.

A list of the twenty names would be a test of this bench. The property is what
matters: whatever a voucher references, a master creates it.
"""

import frappe
from frappe.tests import IntegrationTestCase

from tally_connector.utils.ledgers import LedgerRegistry, ledger_name
from tally_connector.utils.exporter import (
    render_full_export,
    render_section,
    unresolved_ledgers,
)


def _ledgers_named_by(vouchers_xml):
    """Every ledger name the voucher file references, from both places one can appear.

    Parsed out of the emitted XML rather than asked of a helper. The first
    version of this test asked a helper what the export *would* name things and
    passed with the defect planted - two answers to one question, which is the
    shape of the original bug.
    """
    from lxml import etree

    root = etree.fromstring(vouchers_xml.encode("utf-8"))
    names = {el.text for el in root.iter("LEDGERNAME") if el.text}
    names |= {el.text for el in root.iter("PARTYLEDGERNAME") if el.text}
    return {n.strip() for n in names if n and n.strip()}


def _companies():
    return frappe.get_all("Company", pluck="name")


class TestLedgerClosure(IntegrationTestCase):
    def test_every_company_exports_with_no_unresolved_ledger(self):
        """The property, over whatever data this site happens to hold."""
        companies = _companies()
        self.assertTrue(companies, "No Company on this site - the check would be vacuous.")
        for company in companies:
            with self.subTest(company=company):
                missing = unresolved_ledgers(None, None, company)
                self.assertEqual(
                    missing, [],
                    f"{company}: vouchers reference {len(missing)} ledgers no master creates. "
                    f"Tally refuses each of those lines. {missing[:10]}",
                )

    def test_the_check_can_actually_fail(self):
        """A closure test that cannot fail is worse than none.

        The registry is fed a name nothing will create, and the comparison the
        real check performs must report it.
        """
        company = _companies()[0]
        masters_xml, _vouchers, _counts = render_full_export(None, None, company)
        from lxml import etree

        created = {
            el.get("NAME")
            for el in etree.fromstring(masters_xml.encode("utf-8")).iter()
            if el.tag in ("LEDGER", "GROUP") and el.get("NAME")
        }
        registry = LedgerRegistry(company)
        registry.ref("A Ledger Nobody Will Ever Create", "Suspense A/c")
        self.assertEqual(
            sorted(registry.names() - created), ["A Ledger Nobody Will Ever Create"]
        )

    def test_a_voucher_ledger_is_named_the_way_the_masters_name_it(self):
        """The naming half, stated directly.

        `50100012345 - Demo Bank - DT` was exported as "Demo Bank" and
        referenced on every voucher as "50100012345 - Demo Bank". One
        function answers both questions now.
        """
        company = _companies()[0]
        abbr = frappe.get_cached_value("Company", company, "abbr")
        numbered = frappe.get_all(
            "Account",
            filters={"company": company, "is_group": 0, "account_number": ["!=", ""]},
            fields=["name", "account_name"],
            limit=1,
        )
        if not numbered:
            self.skipTest("No numbered account on this site.")
        acc = numbered[0]
        canonical = ledger_name(acc.name, company)
        self.assertNotEqual(
            canonical, acc.account_name,
            "This fixture is meant to be an account whose two spellings differ.",
        )
        self.assertFalse(canonical.endswith(f" - {abbr}"))
        self.assertTrue(acc.name.startswith(canonical))

    def test_an_account_is_created_by_the_chart_of_accounts_and_not_the_fallback(self):
        """The naming defect, caught where it actually bites.

        The closure step is a safety net, and a net that catches everything hides
        what fell. With the Chart of Accounts naming ledgers `account_name`
        again, closure still holds - the fallback simply creates the missing
        spellings - and Tally ends up with the bank account under two names with
        the balance split between them, which is worse than a refused line
        because nothing reports it.

        So: any ledger a voucher references that *is* an ERPNext Account must
        come from the Chart of Accounts export. The fallback is for the ones
        that are not - the suspense ledger, the TDS ledger, a renamed party.
        """
        for company in _companies():
            with self.subTest(company=company):
                self._assert_accounts_come_from_the_coa(company)

    def _assert_accounts_come_from_the_coa(self, company):
        from lxml import etree

        _masters, vouchers_xml, _counts = render_full_export(None, None, company)
        referenced = _ledgers_named_by(vouchers_xml)

        coa_xml, _n = render_section("chart_of_accounts", None, None, company)
        coa = {
            el.get("NAME")
            for el in etree.fromstring(coa_xml.encode("utf-8")).iter("LEDGER")
            if el.get("NAME")
        }
        accounts = {
            ledger_name(a, company)
            for a in frappe.get_all(
                "Account", filters={"company": company, "is_group": 0}, pluck="name"
            )
        }
        referenced_accounts = referenced & accounts
        self.assertTrue(referenced_accounts, "No voucher referenced an account - check the fixture.")
        self.assertEqual(
            sorted(referenced_accounts - coa), [],
            "These ledgers are real accounts referenced by vouchers, yet the Chart of "
            "Accounts export does not create them under that name - so the fallback "
            "creates a duplicate and the balance splits across two ledgers in Tally.",
        )

    def test_every_referenced_ledger_carries_a_parent(self):
        """A ledger with no parent lands wherever Tally decides, which is nowhere useful."""
        company = _companies()[0]
        masters_xml, _v, _c = render_full_export(None, None, company)
        from lxml import etree

        for el in etree.fromstring(masters_xml.encode("utf-8")).iter("LEDGER"):
            with self.subTest(ledger=el.get("NAME")):
                self.assertTrue((el.findtext("PARENT") or "").strip(),
                                f"{el.get('NAME')} would be created with no parent group.")
