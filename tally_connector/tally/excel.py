"""
The same export, as the workbooks Tally's own Excel import reads.

Tally ships sample workbooks - *All Accounting Masters* and *Accounting
Vouchers* - and each carries a **Read Me** sheet listing every column it will
accept, with the permitted values for the ones it validates. Those sheets are
the specification this module is written against, and they are the reason this
path is safer than the XML one: a column name is documented, where an XML
element had to be inferred from files and refusals.

Three rules come straight off the Read Me and shape everything below.

**The header row is the contract.** *"Ensure to enter Column Headers exactly as
mentioned under the List of Fields"*, and *"Delete unwanted columns"* - so a
column may be left out but never renamed, and nothing may be invented. Every
header in `MASTER_SHEETS` and `VOUCHER_COLUMNS` is copied from a Read Me sheet.

**Sign is a word, not a minus.** The model carries a signed amount, negative for
a debit. Excel carries the magnitude in one column and `Dr` or `Cr` in the next.

**A voucher is a run of rows.** The first row of the run carries the voucher's
own fields and every later row leaves them blank, which is how Tally knows the
run has not ended. Each ledger posting, bill reference, cost centre allocation
and stock line takes a row of the run.

Pure, like the rest of `tally/`: model in, workbook bytes out. No frappe.
"""

from __future__ import annotations

import datetime
import io
from decimal import Decimal

from openpyxl import Workbook

from tally_connector.tally.model import (
    INVOICE_KINDS,
    REGISTRATION_TYPES,
    Export,
    Masters,
    money,
)

# Tally's own sheet names, in the order its template puts them.
GROUP_SHEET = "Group"
LEDGER_SHEET = "Ledger"
COST_CATEGORY_SHEET = "Cost Category"
COST_CENTRE_SHEET = "Cost Centre"
VOUCHER_SHEET = "Accounting Voucher"

DR, CR = "Dr", "Cr"

# `Change Mode` on the voucher sheet, which is what the XML says with ISINVOICE
# and the voucher's view. Its permitted values are on the Read Me.
AS_VOUCHER = "As Voucher"
ITEM_INVOICE = "Item Invoice"
ACCOUNTING_INVOICE = "Accounting Invoice"

# The voucher sheet's list, which turned out to be the same four words Tally
# writes into its own XML - so the extract now produces them and this is a
# membership check rather than a second mapping. Two mappings for one fact is
# how the two exporters would come to disagree about a party.
VOUCHER_REGISTRATIONS = REGISTRATION_TYPES

MASTER_SHEETS = {
    LEDGER_SHEET: (
        "Name", "Group Name",
        "Ledger - Opening Balance", "Ledger Opening Balance - Dr/Cr",
        "Maintain balances bill-by-bill",
        "Cost centres are applicable",
        "Inventory values are affected",
        "Type of Duty/Tax", "Tax Type (GST)",
        "Mailing Name", "Address", "State", "Country", "Pincode",
        "GST Registration Type", "GST Registration - GSTIN/UIN",
    ),
    GROUP_SHEET: ("Name", "Parent Group Name"),
    COST_CATEGORY_SHEET: ("Name",),
    COST_CENTRE_SHEET: ("Name",),
}

VOUCHER_COLUMNS = (
    "Voucher Date", "Voucher Type Name", "Voucher Number", "Reference No.",
    "Voucher Narration", "Change Mode",
    "Buyer/Supplier - Address", "Buyer/Supplier - State",
    "Buyer/Supplier - Country", "Buyer/Supplier - Pincode",
    "Buyer/Supplier - GST Registration Type", "Buyer/Supplier - GSTIN/UIN",
    "Buyer/Supplier - Place of Supply",
    "Ledger Name", "Ledger Amount", "Ledger Amount Dr/Cr",
    "Bill Type of Ref", "Bill Name", "Bill Amount", "Bill Amount - Dr/Cr",
    "Bill Due Dt or Credit Days",
    "Cost Allocation for - Cost Centre", "Cost Allocation for - Amount",
    "Item Name", "Billed Quantity", "Quantity UOM",
    "Item Rate", "Item Rate per", "Item Amount",
    "Bank Allocations - Transaction Type", "Bank Allocations - Inst Date",
    "Bank Allocations - Amount",
)


def _date(value):
    """Tally's Excel import reads a real date cell, not a formatted string."""
    if isinstance(value, datetime.datetime):
        return value.date()
    return value


def _amount(value):
    """The magnitude. The sign lives in the neighbouring Dr/Cr column."""
    return float(abs(money(value)))


def _side(value) -> str:
    """Negative is a debit, which is the convention the whole model uses."""
    return DR if Decimal(str(value)) < 0 else CR


def _quantity(value):
    return float(money(value)) if value is not None else None


def _change_mode(voucher) -> str:
    if voucher.is_invoice:
        return ITEM_INVOICE
    if voucher.kind in INVOICE_KINDS:
        # An invoice with no stock on it. Tally's own export of exactly this
        # case says Accounting Invoice, and As Voucher would drop the party
        # from the invoice view.
        return ACCOUNTING_INVOICE
    return AS_VOUCHER


def _registration(address) -> str:
    """What the extract already decided, checked rather than re-decided.

    Anything outside the list is `Unknown`, which is Tally's own word for a
    party whose registration nobody has said - not a guess at `Regular`.
    """
    if not address or not address.gstin:
        return ""
    kind = (address.registration_type or "").strip()
    return kind if kind in VOUCHER_REGISTRATIONS else "Unknown"


def _sheet(book, title, headers):
    ws = book.create_sheet(title)
    ws.append(list(headers))
    return ws


def _row(columns, values):
    """A row in column order, with every column not named left empty."""
    return [values.get(name) for name in columns]


# ─────────────────────────────────────────────────────────────────────────────
# Masters
# ─────────────────────────────────────────────────────────────────────────────

def build_masters(masters: Masters) -> Workbook:
    book = Workbook()
    book.remove(book.active)

    ledgers = _sheet(book, LEDGER_SHEET, MASTER_SHEETS[LEDGER_SHEET])
    for ledger in masters.ledgers:
        address = ledger.address
        opening = ledger.opening_balance
        ledgers.append(_row(MASTER_SHEETS[LEDGER_SHEET], {
            "Name": ledger.name,
            "Group Name": ledger.parent,
            "Ledger - Opening Balance": (_amount(opening) if opening else None),
            "Ledger Opening Balance - Dr/Cr": (_side(opening) if opening else None),
            "Maintain balances bill-by-bill": _yes_no(ledger.bill_wise),
            "Cost centres are applicable": _yes_no(ledger.cost_centres_on),
            "Inventory values are affected": _yes_no(ledger.affects_stock),
            "Type of Duty/Tax": ledger.tax_type or None,
            "Tax Type (GST)": _tax_type(ledger),
            "Mailing Name": ledger.name if address else None,
            "Address": ", ".join(address.lines) if address else None,
            "State": address.state if address else None,
            "Country": address.country if address else None,
            "Pincode": address.pincode if address else None,
            "GST Registration Type": (address.registration_type
                                      if address and address.gstin else None),
            "GST Registration - GSTIN/UIN": address.gstin if address else None,
        }))

    groups = _sheet(book, GROUP_SHEET, MASTER_SHEETS[GROUP_SHEET])
    for group in masters.groups:
        groups.append([group.name, group.parent])

    # One category, because the export allocates to cost centres and never to a
    # category, and Tally files an unallocated centre under Primary Cost
    # Category. The sheet exists so the centres below have somewhere to sit.
    categories = _sheet(book, COST_CATEGORY_SHEET, MASTER_SHEETS[COST_CATEGORY_SHEET])
    if masters.cost_centres:
        categories.append(["Primary Cost Category"])

    centres = _sheet(book, COST_CENTRE_SHEET, MASTER_SHEETS[COST_CENTRE_SHEET])
    for centre in masters.cost_centres:
        centres.append([centre.name])

    return book


def _yes_no(flag) -> str:
    return "Yes" if flag else "No"


def _tax_type(ledger):
    """Tally spells the duty head SGST/UTGST on this sheet, and SGST in XML."""
    if not ledger.duty_head:
        return None
    return "SGST/UTGST" if ledger.duty_head == "SGST" else ledger.duty_head


# ─────────────────────────────────────────────────────────────────────────────
# Vouchers
# ─────────────────────────────────────────────────────────────────────────────

def _voucher_rows(voucher):
    """One voucher as its run of rows.

    The first row carries the voucher's own fields; every later row leaves them
    blank, which is how Tally reads a run as one voucher rather than as several.
    """
    address = voucher.party_address
    head = {
        "Voucher Date": _date(voucher.date),
        "Voucher Type Name": voucher.kind,
        "Voucher Number": voucher.number,
        "Reference No.": voucher.reference or None,
        "Voucher Narration": voucher.narration or None,
        "Change Mode": _change_mode(voucher),
    }
    if address:
        head.update({
            "Buyer/Supplier - Address": ", ".join(address.lines) or None,
            "Buyer/Supplier - State": address.state or None,
            "Buyer/Supplier - Country": address.country or None,
            "Buyer/Supplier - Pincode": address.pincode or None,
            "Buyer/Supplier - GSTIN/UIN": address.gstin or None,
            "Buyer/Supplier - GST Registration Type": _registration(address) or None,
            "Buyer/Supplier - Place of Supply": address.state or None,
        })

    rows = []

    for line in voucher.inventory:
        rows.append({
            "Item Name": line.item,
            "Billed Quantity": _quantity(line.quantity),
            "Quantity UOM": line.unit or None,
            "Item Rate": _amount(line.rate),
            "Item Rate per": line.unit or None,
            "Item Amount": _amount(line.amount),
            "Ledger Name": line.ledger,
            "Ledger Amount": _amount(line.amount),
            "Ledger Amount Dr/Cr": _side(line.amount),
        })

    for line in voucher.lines:
        base = {
            "Ledger Name": line.ledger,
            "Ledger Amount": _amount(line.amount),
            "Ledger Amount Dr/Cr": _side(line.amount),
        }
        if line.cleared_on:
            base.update({
                "Bank Allocations - Transaction Type": line.instrument or "Cheque/DD",
                "Bank Allocations - Inst Date": _date(line.cleared_on),
                "Bank Allocations - Amount": _amount(line.amount),
            })
        # A bill reference and a cost centre allocation are each a row of their
        # own beneath the ledger, so a posting split across three bills is three
        # rows rather than three values crammed into one cell.
        extras = []
        for ref in line.bill_refs:
            extras.append({
                "Bill Type of Ref": ref.kind,
                "Bill Name": ref.name,
                "Bill Amount": _amount(ref.amount),
                "Bill Amount - Dr/Cr": _side(ref.amount),
                "Bill Due Dt or Credit Days": _date(ref.due_date) if ref.due_date else None,
            })
        for centre in line.cost_centres:
            extras.append({
                "Cost Allocation for - Cost Centre": centre.name,
                "Cost Allocation for - Amount": _amount(centre.amount),
            })
        if extras:
            first, rest = extras[0], extras[1:]
            rows.append({**base, **first})
            rows.extend(rest)
        else:
            rows.append(base)

    if not rows:
        rows.append({})
    rows[0] = {**head, **rows[0]}
    return rows


def build_vouchers(vouchers) -> Workbook:
    book = Workbook()
    book.remove(book.active)
    sheet = _sheet(book, VOUCHER_SHEET, VOUCHER_COLUMNS)
    for voucher in vouchers:
        for row in _voucher_rows(voucher):
            sheet.append(_row(VOUCHER_COLUMNS, row))
    return book


# ─────────────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────────────

def _bytes(book) -> bytes:
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def render_masters(masters: Masters) -> bytes:
    return _bytes(build_masters(masters))


def render_vouchers(vouchers) -> bytes:
    return _bytes(build_vouchers(vouchers))


def render(export: Export) -> tuple[bytes, bytes]:
    """The two workbooks, in the order they must be imported."""
    return render_masters(export.masters), render_vouchers(export.vouchers)
