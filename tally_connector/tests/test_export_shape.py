"""
The shape of what Tally is asked to import.

Tally routes an import by REPORTNAME and treats one TALLYMESSAGE as one object.
The combined export declared "All Masters" and then put 636 master elements and
1,222 VOUCHER elements into a single TALLYMESSAGE, which is neither a master
import nor a voucher import.
"""

import frappe
from frappe.tests import IntegrationTestCase
from lxml import etree

from tally_connector.api import export as export_api
from tally_connector.utils.exporter import (
    render_document,
    render_full_export,
    render_section,
)

MASTER_TAGS = {"LEDGER", "GROUP", "UNIT", "STOCKITEM", "STOCKGROUP"}


def _company():
    return frappe.get_all("Company", pluck="name")[0]


class TestExportShape(IntegrationTestCase):
    def setUp(self):
        self.company = _company()
        self.masters, self.vouchers, self.counts = render_full_export(
            None, None, self.company
        )
        self.mroot = etree.fromstring(self.masters.encode("utf-8"))
        self.vroot = etree.fromstring(self.vouchers.encode("utf-8"))

    def test_masters_and_vouchers_are_two_documents(self):
        """Two files, two import requests, and masters must land first."""
        self.assertEqual(self.mroot.findtext(".//REPORTNAME"), "All Masters")
        self.assertEqual(self.vroot.findtext(".//REPORTNAME"), "Vouchers")

    def test_no_voucher_rides_in_the_masters_envelope(self):
        self.assertEqual(self.mroot.findall(".//VOUCHER"), [])

    def test_no_master_rides_in_the_vouchers_envelope(self):
        for tag in MASTER_TAGS:
            self.assertEqual(self.vroot.findall(f".//{tag}"), [], f"{tag} is in the voucher file.")

    def test_one_tallymessage_per_object(self):
        for root, label in ((self.mroot, "masters"), (self.vroot, "vouchers")):
            messages = root.findall(".//TALLYMESSAGE")
            for msg in messages:
                with self.subTest(document=label):
                    self.assertEqual(
                        len(msg), 1,
                        f"A TALLYMESSAGE in the {label} carries {len(msg)} objects; Tally reads one.",
                    )

    def test_the_envelope_names_the_company_as_tally_knows_it(self):
        """`company_name_in_tally` was read once in the connector and used in no
        envelope, so every import named the ERPNext company instead."""
        settings = frappe.get_single("Tally Settings")
        expected = (settings.company_name_in_tally or "").strip() or self.company
        self.assertEqual(self.mroot.findtext(".//SVCURRENTCOMPANY"), expected)
        self.assertEqual(self.vroot.findtext(".//SVCURRENTCOMPANY"), expected)

    def test_a_single_document_export_is_a_single_document(self):
        """It passed the posting date as both ends of a range, so this pushed
        every document sharing the date while the dialog named one."""
        busiest = frappe.db.sql(
            """select posting_date, count(*) c from `tabSales Invoice`
               where docstatus = 1 and company = %s
               group by posting_date order by c desc limit 1""",
            self.company, as_dict=True,
        )
        if not busiest or busiest[0].c < 2:
            self.skipTest("No date on this site carries two submitted sales invoices.")
        day = str(busiest[0].posting_date)
        one = frappe.get_all(
            "Sales Invoice",
            filters={"docstatus": 1, "company": self.company, "posting_date": day},
            limit=1, pluck="name",
        )[0]

        _all_xml, all_count = render_section("sales_invoices", day, day, self.company)
        one_xml, one_count = render_document("Sales Invoice", one, day, self.company)

        self.assertGreater(all_count, 1)
        self.assertEqual(one_count, 1)
        self.assertEqual(
            [e.text for e in etree.fromstring(one_xml.encode("utf-8")).iter("VOUCHERNUMBER")],
            [one],
        )

    def test_bank_transactions_are_scoped_to_the_company(self):
        """This filter alone among the five carried no company, so every bank
        line on the site went into whichever Tally company was named."""
        companies = frappe.get_all("Company", pluck="name")
        if len(companies) < 2:
            self.skipTest("One company on this site - the scoping cannot be observed.")
        total = frappe.db.count("Bank Transaction", {"docstatus": 1})
        per_company = 0
        for company in companies:
            _xml, count = render_section("bank_transactions", None, None, company)
            per_company += count
            self.assertLessEqual(count, total)
        self.assertLessEqual(per_company, total,
                             "A bank line was exported under more than one company.")

    def test_export_all_reports_its_own_closure(self):
        result = export_api.preflight(None, None, self.company)
        self.assertEqual(result["unresolved_ledgers"], [])
        self.assertEqual(
            result["counts"]["total"],
            result["counts"]["masters"] + result["counts"]["vouchers"],
        )
