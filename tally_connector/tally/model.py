"""
The normalised model every other layer speaks.

Small enough to write by hand in a fixture, complete enough that rendering needs
nothing else. Two decisions carry most of the weight:

**One signed amount per line, in base currency.** Tally's convention is that a
negative amount is a debit and a positive one is a credit, and that a voucher's
amounts sum to zero. The generators this replaces stored the sign in three
different ways - a `-` prefixed onto a formatted string here, an
`ISDEEMEDPOSITIVE` flag there, an already-negative ERPNext field somewhere else -
and each of the eleven call sites had to get the combination right. Two of them
did not: a purchase return produced `--1234.50`, which Tally cannot parse at
all, and a foreign-currency journal put 61.79 EUR against 6,977.85 INR in one
voucher. Here the sign lives in one place and `ISDEEMEDPOSITIVE` is derived at
render time, so neither defect is expressible.

**Nothing is validated on construction.** An unbalanced voucher must be
*representable*, or the validator has nothing to report and the failure moves
back to Tally's event log where it started. `imbalance()` answers the question;
deciding what to do about it belongs to the validator.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal

# Tally voucher types this connector emits. The value is the name Tally knows,
# which is also the default `VOUCHERTYPENAME` - a company may rename them, which
# is what the voucher-type settings are for.
SALES = "Sales"
PURCHASE = "Purchase"
RECEIPT = "Receipt"
PAYMENT = "Payment"
JOURNAL = "Journal"
CONTRA = "Contra"
CREDIT_NOTE = "Credit Note"
DEBIT_NOTE = "Debit Note"

VOUCHER_KINDS = (
    SALES, PURCHASE, RECEIPT, PAYMENT, JOURNAL, CONTRA, CREDIT_NOTE, DEBIT_NOTE,
)

# Vouchers Tally treats as invoices: they carry stock and must declare the
# invoice view, or Tally answers "Can't mix Stock Items and Accounting Entries".
INVOICE_KINDS = (SALES, PURCHASE, CREDIT_NOTE, DEBIT_NOTE)

# Every voucher type Tally creates in a new company. A voucher naming anything
# else is naming a master somebody made by hand in the destination company, and
# this export creates no voucher types - so it is the same class of reference as
# a group, a unit or a currency, and fails the same way.
RESERVED_VOUCHER_TYPES = frozenset({
    "Contra", "Payment", "Receipt", "Journal", "Sales", "Purchase",
    "Credit Note", "Debit Note", "Sales Order", "Purchase Order",
    "Delivery Note", "Receipt Note", "Rejections In", "Rejections Out",
    "Stock Journal", "Physical Stock", "Memorandum", "Reversing Journal",
    "Payroll", "Attendance", "Material In", "Material Out",
    "Job Work In Order", "Job Work Out Order",
})

# The words Tally writes for a party's GST registration. Counted off 1,160
# voucher tags and 5,974 ledger tags in two real Tally exports: `Regular` and
# `Unregistered/Consumer` are almost all of it, and `Unregistered/Consumer` is
# **one token** rather than two - which is also exactly the list on the Read Me
# sheet of Tally's own Excel voucher template. Two independent sources, one
# answer. `Consumer` and `Unregistered` as separate words are what this used to
# send, and neither appears anywhere in 300 MB of Tally's own output.
REGISTRATION_TYPES = ("Regular", "Composition", "Unregistered/Consumer",
                      "Unknown")

# Bill-wise reference types, spelled as Tally spells them.
NEW_REF = "New Ref"
AGAINST_REF = "Agst Ref"
ADVANCE = "Advance"
ON_ACCOUNT = "On Account"


def money(value) -> Decimal:
    """Two decimal places, half-up, as an exact quantity.

    Decimal rather than float because these are summed to test whether a voucher
    balances, and 0.1 + 0.2 is a poor basis for telling an accountant their books
    do not add up.
    """
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _date(value) -> datetime.date:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value)[:10])


@dataclass(frozen=True)
class BillRef:
    """A bill-wise reference on a party line.

    A party ledger created with `ISBILLWISEON` obliges its vouchers to carry
    these. Without them Tally accepts the voucher and posts the whole amount to
    *On Account*, so the books look right and the ageing is useless - which is
    the state 470 of this bench's vouchers were in.
    """

    name: str
    kind: str = NEW_REF
    amount: Decimal = Decimal("0.00")
    due_date: datetime.date | None = None


@dataclass(frozen=True)
class CostCentreAllocation:
    """One cost centre's share of a ledger line."""

    name: str
    amount: Decimal = Decimal("0.00")



@dataclass
class LedgerLine:
    """One accounting posting. `amount` is signed: negative debit, positive credit."""

    ledger: str
    amount: Decimal
    is_party: bool = False
    bill_refs: list[BillRef] = field(default_factory=list)
    cost_centres: list[CostCentre] = field(default_factory=list)
    # Set only where the line is a bank posting Tally should mark as cleared.
    cleared_on: datetime.date | None = None
    instrument: str | None = None

    @property
    def is_debit(self) -> bool:
        return self.amount < 0


@dataclass
class InventoryLine:
    """One stock line, with the accounting allocation it posts to."""

    item: str
    quantity: Decimal
    unit: str
    rate: Decimal
    amount: Decimal          # signed, same convention as LedgerLine
    ledger: str              # the income or expense ledger this allocates to
    godown: str | None = None
    batch: str | None = None
    # The cost centre on the ERPNext item row. Fetched by the readers all
    # along and thrown away here, which only showed once the line could be
    # folded onto a ledger line that had its own allocations: the amount grew
    # and the allocations did not, so they no longer totalled the line, which
    # is a cost-centre report Tally accepts and gets wrong.
    cost_centres: list[CostCentreAllocation] = field(default_factory=list)


# A fixed namespace, so a voucher's GUID depends only on the ERPNext document
# and not on when, where or by whom the export ran.
GUID_NAMESPACE = uuid.UUID("3f2a6f4c-5c7b-5a1e-9d3f-7b2c4e8a1d60")


def voucher_guid(remote_id):
    """A stable Tally GUID for an ERPNext document.

    Derived rather than stored, because the whole worth of it is that the same
    document exports to the same identifier every time. TallyPrime's "Overwrite
    voucher when a voucher with same GUID exists" reads this to tell a
    re-import from a second set of vouchers, and a fresh value each run would
    duplicate them exactly as having none does.

    Keyed on the document alone. An ERPNext name is unique across the site, and
    keying on the company as well would move every GUID the day somebody
    corrects the company's name in Tally - which is the one thing this must
    survive.
    """
    remote_id = (remote_id or "").strip()
    return str(uuid.uuid5(GUID_NAMESPACE, remote_id)) if remote_id else ""


@dataclass
class Address:
    lines: list[str] = field(default_factory=list)
    state: str = ""
    country: str = ""
    pincode: str = ""
    gstin: str = ""
    registration_type: str = ""


@dataclass
class Voucher:
    kind: str
    number: str
    date: datetime.date
    narration: str = ""
    party: str | None = None
    party_address: Address | None = None
    lines: list[LedgerLine] = field(default_factory=list)
    inventory: list[InventoryLine] = field(default_factory=list)
    # The ERPNext document this came from. Tally correlates on it, which is what
    # makes a second export an update rather than a duplicate.
    remote_id: str | None = None
    # Tally's own identifier. Left empty in the ordinary case and derived from
    # `remote_id` when the voucher is written, so it cannot be forgotten; set
    # explicitly only where something already knows Tally's own value.
    guid: str = ""

    @property
    def tally_guid(self) -> str:
        return self.guid or voucher_guid(self.remote_id)
    reference: str = ""
    # Whether Tally should read this as an invoice, which is what makes stock
    # lines legal on it. It is normally decided by the voucher type's name - but
    # that name is a setting, so a company whose sales vouchers are called
    # "Tax Invoice" would have had every stock invoice refused with
    # *"Can't mix Stock Items and Accounting Entries"*. `extract` sets it from
    # the ERPNext document, `read` from what the file says, and `None` means
    # neither knew and the name is all there is to go on.
    is_invoice_kind: bool | None = None
    # The ERPNext doctype a voucher was built from, where its kind does not
    # say. A Journal built from an Expense Claim is not a Journal Entry, and the
    # sync ledger keys on the doctype, so recording it as one would put a row
    # under a document that does not exist.
    source_doctype: str | None = None

    @property
    def is_invoice(self) -> bool:
        by_kind = (self.kind in INVOICE_KINDS if self.is_invoice_kind is None
                   else self.is_invoice_kind)
        return by_kind and bool(self.inventory)

    def imbalance(self) -> Decimal:
        """How far from zero this voucher's postings sum.

        Inventory lines carry their own accounting allocation, so they count once
        - through the allocation, not through both.
        """
        total = sum((line.amount for line in self.lines), Decimal("0.00"))
        total += sum((inv.amount for inv in self.inventory), Decimal("0.00"))
        return money(total)

    def balances(self) -> bool:
        return self.imbalance() == Decimal("0.00")


# ─────────────────────────────────────────────────────────────────────────────
# Masters
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Group:
    name: str
    parent: str


# Tally's own duty heads. A GST ledger that does not declare one is a duty
# ledger as far as Tally is concerned, and its GST reports stay empty.
CGST, SGST, IGST, CESS = "CGST", "SGST", "IGST", "Cess"
DUTY_HEADS = (CGST, SGST, IGST, CESS)

# Taxability, as Tally spells it on a stock item.
TAXABLE, EXEMPT, NIL_RATED, NON_GST = "Taxable", "Exempt", "Nil Rated", "Non-GST"


@dataclass
class Ledger:
    name: str
    parent: str
    # Empty means "the company's own base currency", which is what Tally uses
    # when a ledger declares none - and what every amount in this export is.
    # Naming a currency here only makes sense once something also creates it.
    currency: str = ""
    bill_wise: bool = False
    affects_stock: bool = False
    address: Address | None = None
    opening_balance: Decimal | None = None
    # "GST" for a GST ledger, "Others" for any other duty ledger, empty
    # otherwise. Tally reads its GST returns off this and the duty head, not off
    # the ledger's name.
    tax_type: str = ""
    duty_head: str = ""
    # Tally refuses a cost centre allocation on a ledger that has not got cost
    # centres switched on, so the flag travels with the ledger rather than being
    # inferred at render time from whether an allocation happens to be present.
    cost_centres_on: bool = False


@dataclass
class Unit:
    """A unit of measure.

    `symbol` is what Tally calls the unit and what every reference to it must
    use; `formal_name` is the long form. They are separate because Tally's symbol
    grammar is narrow - `/` is its compound-unit separator, so a symbol holding
    one is read as a compound unit and refused, taking the whole import with it.
    """

    symbol: str
    formal_name: str = ""
    decimal_places: int = 2


# Tally files every cost centre under a cost category, and ships exactly one.
PRIMARY_COST_CATEGORY = "Primary Cost Category"


@dataclass
class CostCentre:
    """A Tally cost centre master.

    Distinct from `CostCentre` allocations on a line: this is the master that
    has to exist before a line may name it.
    """

    name: str
    parent: str = ""
    category: str = PRIMARY_COST_CATEGORY


@dataclass
class Godown:
    name: str
    parent: str = ""


@dataclass
class StockGroup:
    name: str
    parent: str = ""


@dataclass
class StockItem:
    name: str
    parent: str = ""
    base_unit: str = ""
    description: str = ""
    hsn_code: str = ""
    gst_applicable: bool = True
    type_of_supply: str = "Goods"
    taxability: str = TAXABLE
    # The integrated rate, as a percentage. Tally holds a rate per duty head,
    # and for an ordinary Indian item the central and state rates are each half
    # of this one - which is how india_compliance models it too. `None` means
    # ERPNext has no rate for this item, which is a different thing from nought.
    gst_rate: Decimal | None = None


@dataclass
class Masters:
    """Everything that must exist in Tally before a voucher references it."""

    # Currency masters this export creates, which is none today: every amount
    # is in the company's base currency, so no ledger declares one. The field
    # is real rather than assumed because `_check_currency` reads it - it used
    # to reach for it through a `getattr` default, so the check was written
    # against something that did not exist and nobody could tell.
    currencies: list[str] = field(default_factory=list)
    units: list[Unit] = field(default_factory=list)
    stock_groups: list[StockGroup] = field(default_factory=list)
    stock_items: list[StockItem] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    ledgers: list[Ledger] = field(default_factory=list)
    cost_centres: list[CostCentre] = field(default_factory=list)
    godowns: list[Godown] = field(default_factory=list)
    # The date a ledger's GST registration, mailing details and GST rates take
    # effect from. Tally keeps all three as dated collections, and every one of
    # the 10,969 filled blocks in a real company's export opens with it - where
    # this export had sent none. It must not be later than any voucher, or the
    # voucher sees no registration at all; earlier than the books is fine, and
    # the real file does exactly that.
    applicable_from: datetime.date | None = None

    def ledger_names(self) -> set[str]:
        return {l.name for l in self.ledgers} | {g.name for g in self.groups}

    def count(self) -> int:
        """How many objects a masters import would create.

        Cost centres and godowns are counted like everything else: they are
        TALLYMESSAGE entries Tally either accepts or refuses, and a count that
        quietly omitted two kinds would make a partial import look complete.
        """
        return sum(len(x) for x in (self.units, self.stock_groups, self.stock_items,
                                    self.groups, self.ledgers, self.cost_centres,
                                    self.godowns))


@dataclass
class Export:
    """A whole export: the company it is for, its masters and its vouchers."""

    company: str
    masters: Masters = field(default_factory=Masters)
    vouchers: list[Voucher] = field(default_factory=list)

    # Whether the vouchers were folded to accounting entries only, and how
    # many stock lines that dropped. Recorded rather than inferred from the
    # inventory being empty, because "nothing was sold" and "the items were
    # folded away" are different facts and a report that cannot tell them
    # apart is a report somebody has to go and check by hand.
    accounting_only: bool = False
    stock_lines_folded: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# Plain-data round trip
#
# Fixtures are JSON so a scenario can be written, read and reviewed by hand
# without a database anywhere near it.
# ─────────────────────────────────────────────────────────────────────────────

def to_jsonable(obj):
    """Model -> plain JSON types, with Decimal and date as strings."""
    def convert(v):
        if isinstance(v, Decimal):
            return f"{v:.2f}"
        if isinstance(v, datetime.date):
            return v.isoformat()
        if isinstance(v, dict):
            return {k: convert(x) for k, x in v.items() if x is not None and x != [] and x != ""}
        if isinstance(v, (list, tuple)):
            return [convert(x) for x in v]
        return v

    return convert(asdict(obj))


_DECIMALS = {"amount", "rate", "quantity", "opening_balance", "gst_rate"}
_DATES = {"date", "due_date", "cleared_on"}


def _hydrate(cls, data):
    if data is None:
        return None
    kwargs = {}
    for name, f in cls.__dataclass_fields__.items():
        if name not in data:
            continue
        raw = data[name]
        if name in _DECIMALS and raw is not None:
            kwargs[name] = money(raw)
        elif name in _DATES and raw is not None:
            kwargs[name] = _date(raw)
        else:
            kwargs[name] = raw
    return cls(**kwargs)


def export_from_dict(data) -> Export:
    """Rebuild an Export from the plain data a fixture holds."""
    m = data.get("masters", {})
    masters = Masters(
        units=[_hydrate(Unit, u) for u in m.get("units", [])],
        stock_groups=[_hydrate(StockGroup, g) for g in m.get("stock_groups", [])],
        stock_items=[_hydrate(StockItem, i) for i in m.get("stock_items", [])],
        groups=[_hydrate(Group, g) for g in m.get("groups", [])],
        cost_centres=[_hydrate(CostCentre, c) for c in m.get("cost_centres", [])],
        godowns=[_hydrate(Godown, g) for g in m.get("godowns", [])],
        applicable_from=_date(m["applicable_from"]) if m.get("applicable_from") else None,
        ledgers=[
            Ledger(**{**_hydrate(Ledger, l).__dict__,
                      "address": _hydrate(Address, l.get("address"))})
            for l in m.get("ledgers", [])
        ],
    )
    vouchers = []
    for v in data.get("vouchers", []):
        voucher = _hydrate(Voucher, {k: x for k, x in v.items()
                                     if k not in ("lines", "inventory", "party_address")})
        voucher.party_address = _hydrate(Address, v.get("party_address"))
        voucher.lines = [
            LedgerLine(**{**_hydrate(LedgerLine, l).__dict__,
                          "bill_refs": [_hydrate(BillRef, b) for b in l.get("bill_refs", [])],
                          "cost_centres": [_hydrate(CostCentreAllocation, c)
                                           for c in l.get("cost_centres", [])]})
            for l in v.get("lines", [])
        ]
        voucher.inventory = [_hydrate(InventoryLine, i) for i in v.get("inventory", [])]
        vouchers.append(voucher)
    return Export(company=data["company"], masters=masters, vouchers=vouchers)
