"""
The workbooks, offline. No site, no database, no Tally.

Tally's sample workbooks - *All Accounting Masters* and *Accounting Vouchers* -
each carry a Read Me sheet listing every column it accepts and the permitted
values for the ones it validates. Those sheets are the specification, and the
constants asserted here are copied from them: *"Ensure to enter Column Headers
exactly as mentioned under the List of Fields"*.

That is the reason this path exists beside the XML one. An XML element had to be
inferred from files and refusals, and four vouchers imports were lost to a word
in the envelope. A column name is written down.
"""

import datetime
import io
import unittest

import openpyxl

from tally_connector.tally import excel, golden
from tally_connector.tally.model import INVOICE_KINDS

# Copied from the Read Me sheets, and the point of copying them is that a
# rename here has to be a deliberate edit rather than a refactor.
LEDGER_HEADERS = (
    "Name", "Group Name", "Ledger - Opening Balance",
    "Ledger Opening Balance - Dr/Cr", "Maintain balances bill-by-bill",
    "Cost centres are applicable", "Inventory values are affected",
    "Type of Duty/Tax", "Tax Type (GST)", "Mailing Name", "Address", "State",
    "Country", "Pincode", "GST Registration Type",
    "GST Registration - GSTIN/UIN",
)
GROUP_HEADERS = ("Name", "Parent Group Name")
VOUCHER_HEADERS = (
    "Voucher Date", "Voucher Type Name", "Voucher Number", "Reference No.",
    "Voucher Narration", "Change Mode", "Buyer/Supplier - Address",
    "Buyer/Supplier - State", "Buyer/Supplier - Country",
    "Buyer/Supplier - Pincode", "Buyer/Supplier - GST Registration Type",
    "Buyer/Supplier - GSTIN/UIN", "Buyer/Supplier - Place of Supply",
    "Ledger Name", "Ledger Amount", "Ledger Amount Dr/Cr", "Bill Type of Ref",
    "Bill Name", "Bill Amount", "Bill Amount - Dr/Cr",
    "Bill Due Dt or Credit Days", "Cost Allocation for - Cost Centre",
    "Cost Allocation for - Amount", "Item Name", "Billed Quantity",
    "Quantity UOM", "Item Rate", "Item Rate per", "Item Amount",
    "Bank Allocations - Transaction Type", "Bank Allocations - Inst Date",
    "Bank Allocations - Amount",
)


def _book(blob):
    return openpyxl.load_workbook(io.BytesIO(blob))


def _rows(sheet):
    """Every data row as a dict of the columns it actually filled in."""
    headers = [c.value for c in sheet[1]]
    out = []
    for row in sheet.iter_rows(min_row=2, values_only=True):
        out.append({h: v for h, v in zip(headers, row) if v is not None})
    return out


class TestTheHeaderRowIsTheContract(unittest.TestCase):
    """Tally matches on the column header, so a rename is a silent data loss."""

    def setUp(self):
        export = golden.load("sale_domestic_gst")
        self.masters = _book(excel.render_masters(export.masters))
        self.vouchers = _book(excel.render_vouchers(export.vouchers))

    def test_the_masters_workbook_has_tallys_four_sheets(self):
        self.assertEqual(self.masters.sheetnames,
                         ["Ledger", "Group", "Cost Category", "Cost Centre"])

    def test_the_vouchers_workbook_has_tallys_one_sheet(self):
        self.assertEqual(self.vouchers.sheetnames, ["Accounting Voucher"])

    def test_every_header_is_one_tally_lists(self):
        for sheet, expected in (("Ledger", LEDGER_HEADERS),
                                ("Group", GROUP_HEADERS)):
            with self.subTest(sheet=sheet):
                self.assertEqual(
                    tuple(c.value for c in self.masters[sheet][1]), expected)
        self.assertEqual(
            tuple(c.value for c in self.vouchers["Accounting Voucher"][1]),
            VOUCHER_HEADERS)

    def test_the_name_only_sheets_are_named_columns_too(self):
        for sheet in ("Cost Category", "Cost Centre"):
            with self.subTest(sheet=sheet):
                self.assertEqual([c.value for c in self.masters[sheet][1]], ["Name"])


class TestSignBecomesAWord(unittest.TestCase):
    """The model carries a signed amount. Excel carries the magnitude and a
    side, so a minus reaching a cell is a debit posted as a credit."""

    def setUp(self):
        export = golden.load("sale_domestic_gst")
        self.rows = _rows(_book(excel.render_vouchers(export.vouchers))["Accounting Voucher"])

    NUMERIC = ("Ledger Amount", "Bill Amount", "Item Amount", "Item Rate",
               "Billed Quantity", "Cost Allocation for - Amount",
               "Bank Allocations - Amount")

    def test_no_amount_cell_is_negative(self):
        for row in self.rows:
            for column in self.NUMERIC:
                if column in row:
                    with self.subTest(column=column):
                        self.assertGreaterEqual(row[column], 0)

    def test_a_debit_says_dr_and_a_credit_says_cr(self):
        party = [r for r in self.rows if r.get("Ledger Name") == "Acme Industries"][0]
        self.assertEqual(party["Ledger Amount Dr/Cr"], "Dr")
        self.assertEqual(party["Ledger Amount"], 11800)
        sales = [r for r in self.rows if r.get("Ledger Name") == "Sales"][0]
        self.assertEqual(sales["Ledger Amount Dr/Cr"], "Cr")

    def test_the_run_of_rows_still_balances(self):
        def signed(row, amount, side):
            value = row.get(amount)
            return 0 if value is None else (-value if row.get(side) == "Dr" else value)

        total = sum(signed(r, "Ledger Amount", "Ledger Amount Dr/Cr") for r in self.rows)
        self.assertEqual(round(total, 2), 0.0)


class TestAVoucherIsARunOfRows(unittest.TestCase):
    """The first row of the run carries the voucher's own fields and every later
    row leaves them blank, which is how Tally knows the run has not ended."""

    def setUp(self):
        export = golden.load("receipt_two_bills")
        self.rows = _rows(_book(excel.render_vouchers(export.vouchers))["Accounting Voucher"])

    def test_only_the_first_row_names_the_voucher(self):
        heads = [i for i, r in enumerate(self.rows) if "Voucher Number" in r]
        self.assertEqual(heads, [0], "a second header row starts a second voucher")

    def test_a_second_bill_reference_takes_a_row_of_its_own(self):
        refs = [r for r in self.rows if "Bill Name" in r]
        self.assertEqual(len(refs), 2)
        # The second row carries the reference and not the ledger again, which
        # would post the amount twice.
        self.assertNotIn("Ledger Amount", refs[1])


class TestValuesTallyValidates(unittest.TestCase):
    """Four columns are checked against a list on the Read Me sheet."""

    def test_change_mode_is_one_of_tallys_three(self):
        for name in golden.scenarios():
            export = golden.load(name)
            rows = _rows(_book(excel.render_vouchers(export.vouchers))["Accounting Voucher"])
            for row in rows:
                if "Change Mode" not in row:
                    continue
                with self.subTest(scenario=name):
                    self.assertIn(row["Change Mode"],
                                  (excel.AS_VOUCHER, excel.ITEM_INVOICE,
                                   excel.ACCOUNTING_INVOICE))

    def test_an_invoice_with_stock_is_an_item_invoice(self):
        export = golden.load("sale_domestic_gst")
        rows = _rows(_book(excel.render_vouchers(export.vouchers))["Accounting Voucher"])
        self.assertEqual(rows[0]["Change Mode"], excel.ITEM_INVOICE)

    def test_an_accounting_voucher_is_as_voucher(self):
        export = golden.load("contra_bank_transfer")
        rows = _rows(_book(excel.render_vouchers(export.vouchers))["Accounting Voucher"])
        self.assertEqual(rows[0]["Change Mode"], excel.AS_VOUCHER)

    def test_an_invoice_with_no_stock_is_an_accounting_invoice(self):
        """Tally's own export of exactly this case says so, and `As Voucher`
        would drop the party from the invoice view."""
        export = golden.load("sale_domestic_gst")
        for voucher in export.vouchers:
            voucher.inventory = []
        rows = _rows(_book(excel.render_vouchers(export.vouchers))["Accounting Voucher"])
        self.assertIn(export.vouchers[0].kind, INVOICE_KINDS)
        self.assertEqual(rows[0]["Change Mode"], excel.ACCOUNTING_INVOICE)

    def test_a_registration_outside_tallys_list_becomes_unknown(self):
        """ERPNext's GST Category is folded onto Tally's words by the extract,
        which both exporters read - so this checks rather than re-decides, and
        anything outside the list is Tally's own `Unknown` rather than a guess
        at `Regular`."""
        for given, expected in (("Regular", "Regular"),
                                ("Composition", "Composition"),
                                ("Unregistered/Consumer", "Unregistered/Consumer"),
                                ("Unknown", "Unknown"),
                                ("Regular - SEZ", "Unknown"),
                                ("Overseas", "Unknown"),
                                ("", "Unknown")):
            address = golden.load("sale_domestic_gst").vouchers[0].party_address
            address.registration_type = given
            with self.subTest(given=given):
                self.assertEqual(excel._registration(address), expected)
                self.assertIn(excel._registration(address),
                              excel.VOUCHER_REGISTRATIONS)

    def test_nothing_is_claimed_where_there_is_no_gstin(self):
        address = golden.load("sale_domestic_gst").vouchers[0].party_address
        address.gstin = ""
        self.assertEqual(excel._registration(address), "")

    def test_a_gst_ledger_says_sgst_the_way_this_sheet_spells_it(self):
        """`SGST/UTGST` here, `SGST` in the XML. Same duty head, two spellings,
        and the sheet's is the one on its Read Me."""
        export = golden.load("sale_domestic_gst")
        rows = _rows(_book(excel.render_masters(export.masters))["Ledger"])
        heads = {r["Name"]: r.get("Tax Type (GST)") for r in rows}
        self.assertEqual(heads["Output SGST"], "SGST/UTGST")
        self.assertEqual(heads["Output CGST"], "CGST")


class TestEveryScenarioRenders(unittest.TestCase):
    def test_both_workbooks_open_for_every_scenario(self):
        for name in golden.scenarios():
            export = golden.load(name)
            with self.subTest(scenario=name):
                masters, vouchers = excel.render(export)
                self.assertEqual(_book(masters).sheetnames[0], "Ledger")
                sheet = _book(vouchers)["Accounting Voucher"]
                self.assertGreaterEqual(sheet.max_row, 2, "no rows written")

    def test_a_date_is_a_date_and_not_a_string(self):
        """A text cell where Tally wants a date is a row it will not read."""
        export = golden.load("sale_domestic_gst")
        sheet = _book(excel.render_vouchers(export.vouchers))["Accounting Voucher"]
        value = _rows(sheet)[0]["Voucher Date"]
        self.assertIsInstance(value, (datetime.date, datetime.datetime))

    def test_every_ledger_a_voucher_names_is_on_the_ledger_sheet(self):
        """The same closure the XML path checks, across the two workbooks."""
        for name in golden.scenarios():
            export = golden.load(name)
            created = {r["Name"] for r in
                       _rows(_book(excel.render_masters(export.masters))["Ledger"])}
            rows = _rows(_book(excel.render_vouchers(export.vouchers))["Accounting Voucher"])
            named = {r["Ledger Name"] for r in rows if "Ledger Name" in r}
            with self.subTest(scenario=name):
                self.assertEqual(sorted(named - created), [])


if __name__ == "__main__":
    unittest.main()
