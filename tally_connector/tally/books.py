"""
A Tally company's books, read out of its own export.

The other direction from `extract.py`: this reads what Tally wrote rather than
writing what Tally reads. It produces plain records and nothing else - no
frappe, no ERPNext vocabulary - so the mapping in `plan.py` can be argued with
and tested against a fixture with no site at all.

Two things here come from a real export rather than from documentation.

**A group says its own nature**, in `ISREVENUE` and `ISDEEMEDPOSITIVE`. Revenue
and deemed-positive is an expense, revenue and not is an income, and on the
balance sheet deemed-positive is an asset and not is a liability. Every one of
the fifteen primary groups in a real company agrees, so a group somebody
invented is classified as correctly as a reserved one and nothing is guessed.

**A voucher can be in the file and not in the books.** Tally exports cancelled
and optional vouchers with everything else, and neither posts. Importing them
would put entries in ERPNext that the company's own trial balance does not have.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from tally_connector.tally.stream import iter_elements, open_export

ASSET, LIABILITY, EQUITY, INCOME, EXPENSE = ("Asset", "Liability", "Equity", "Income", "Expense")

# Tally keeps capital on the liabilities side; ERPNext gives it a root of its
# own. The only primary group whose nature its own flags do not express.
EQUITY_GROUPS = frozenset({"capital account"})


def _text(el, tag, default=""):
	value = el.findtext(tag)
	return (value or default).strip()


def _yes(el, tag):
	return _text(el, tag).lower() == "yes"


def _amount(raw) -> Decimal:
	"""A Tally amount. Negative is a debit, which is Tally's own convention."""
	raw = (raw or "").strip().replace(",", "")
	if not raw:
		return Decimal("0")
	try:
		return Decimal(raw)
	except InvalidOperation:
		return Decimal("0")


def _date(raw):
	raw = (raw or "").strip()
	try:
		return datetime.date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))
	except (ValueError, IndexError):
		return None


@dataclass
class TallyGroup:
	name: str
	parent: str = ""
	revenue: bool = False
	deemed_positive: bool = False

	@property
	def nature(self) -> str:
		if self.name.strip().lower() in EQUITY_GROUPS:
			return EQUITY
		if self.revenue:
			return EXPENSE if self.deemed_positive else INCOME
		return ASSET if self.deemed_positive else LIABILITY


@dataclass
class TallyLedger:
	name: str
	parent: str = ""
	opening: Decimal = Decimal("0")
	bill_wise: bool = False
	gstin: str = ""
	registration: str = ""
	state: str = ""
	country: str = ""
	pincode: str = ""
	address: list[str] = field(default_factory=list)
	mailing_name: str = ""
	pan: str = ""
	email: str = ""
	phone: str = ""
	tax_type: str = ""
	duty_head: str = ""


@dataclass
class BillRef:
	name: str
	kind: str
	amount: Decimal


@dataclass
class TallyPosting:
	ledger: str
	amount: Decimal
	is_party: bool = False
	bills: list[BillRef] = field(default_factory=list)
	cost_centres: list[tuple[str, Decimal]] = field(default_factory=list)
	cleared_on: datetime.date | None = None
	instrument: str = ""

	@property
	def is_debit(self) -> bool:
		return self.amount < 0


@dataclass
class TallyItemLine:
	item: str
	quantity: Decimal
	unit: str
	rate: Decimal
	amount: Decimal
	ledger: str = ""
	godown: str = ""
	hsn: str = ""


@dataclass
class TallyVoucher:
	guid: str
	kind: str
	date: datetime.date | None
	number: str = ""
	reference: str = ""
	reference_date: datetime.date | None = None
	narration: str = ""
	party: str = ""
	is_invoice: bool = False
	postings: list[TallyPosting] = field(default_factory=list)
	items: list[TallyItemLine] = field(default_factory=list)

	def total(self) -> Decimal:
		return sum((p.amount for p in self.postings), Decimal("0"))

	def balances(self) -> bool:
		return self.total() == 0


def read_groups_and_ledgers(source):
	"""Every group and ledger in a masters export, in the order Tally wrote."""
	groups: dict[str, TallyGroup] = {}
	ledgers: dict[str, TallyLedger] = {}
	stream = open_export(source)
	for el in iter_elements(stream, ("GROUP", "LEDGER")):
		name = (el.get("NAME") or _text(el, "NAME")).strip()
		if not name:
			continue
		if el.tag == "GROUP":
			groups[name] = TallyGroup(
				name=name,
				parent=_text(el, "PARENT"),
				revenue=_yes(el, "ISREVENUE"),
				deemed_positive=_yes(el, "ISDEEMEDPOSITIVE"),
			)
			continue
		gst = el.find("LEDGSTREGDETAILS.LIST")
		mailing = el.find("LEDMAILINGDETAILS.LIST")
		ledgers[name] = TallyLedger(
			name=name,
			parent=_text(el, "PARENT"),
			opening=_amount(el.findtext("OPENINGBALANCE")),
			bill_wise=_yes(el, "ISBILLWISEON"),
			gstin=(_text(gst, "GSTIN") if gst is not None else "") or _text(el, "PARTYGSTIN"),
			registration=(_text(gst, "GSTREGISTRATIONTYPE") if gst is not None else "")
			or _text(el, "GSTREGISTRATIONTYPE"),
			state=(_text(mailing, "STATE") if mailing is not None else "")
			or (_text(gst, "PLACEOFSUPPLY") if gst is not None else "")
			or _text(el, "PRIORSTATENAME"),
			country=(_text(mailing, "COUNTRY") if mailing is not None else "")
			or _text(el, "COUNTRYOFRESIDENCE"),
			pincode=_text(mailing, "PINCODE") if mailing is not None else "",
			address=[a.text.strip() for a in el.iter("ADDRESS") if (a.text or "").strip()],
			mailing_name=(_text(mailing, "MAILINGNAME") if mailing is not None else "")
			or _text(el, "MAILINGNAME"),
			pan=_text(el, "INCOMETAXNUMBER"),
			email=_text(el, "EMAIL"),
			phone=_text(el, "LEDGERPHONE") or _text(el, "LEDGERMOBILE"),
			tax_type=_text(el, "TAXTYPE"),
			duty_head=_text(el, "GSTDUTYHEAD"),
		)
	return groups, ledgers, stream.removed


def _postings(voucher):
	"""Every ledger posting on a voucher, from either list Tally uses.

	An invoice writes `LEDGERENTRIES.LIST` and an accounting voucher
	`ALLLEDGERENTRIES.LIST`; a stock line's own allocation is a posting too.
	"""
	out = []
	for el in voucher.iter("LEDGERENTRIES.LIST", "ALLLEDGERENTRIES.LIST"):
		ledger = _text(el, "LEDGERNAME")
		if not ledger:
			continue
		bills = [
			BillRef(name=_text(b, "NAME"), kind=_text(b, "BILLTYPE"), amount=_amount(b.findtext("AMOUNT")))
			for b in el.findall("BILLALLOCATIONS.LIST")
			if _text(b, "NAME")
		]
		centres = [
			(_text(c, "NAME"), _amount(c.findtext("AMOUNT")))
			for c in el.findall("COSTCENTREALLOCATIONS.LIST")
			if _text(c, "NAME")
		]
		bank = el.find("BANKALLOCATIONS.LIST")
		out.append(
			TallyPosting(
				ledger=ledger,
				amount=_amount(el.findtext("AMOUNT")),
				is_party=_yes(el, "ISPARTYLEDGER"),
				bills=bills,
				cost_centres=centres,
				cleared_on=_date(bank.findtext("BANKERSDATE")) if bank is not None else None,
				instrument=_text(bank, "TRANSACTIONTYPE") if bank is not None else "",
			)
		)
	return out


def _split_quantity(raw):
	parts = (raw or "").strip().split()
	if not parts:
		return Decimal("0"), ""
	return _amount(parts[0]), " ".join(parts[1:])


def _split_rate(raw):
	raw = (raw or "").strip()
	if "/" in raw:
		value, unit = raw.split("/", 1)
		return _amount(value), unit.strip()
	return _amount(raw), ""


def _items(voucher):
	out = []
	for el in voucher.iter("ALLINVENTORYENTRIES.LIST"):
		item = _text(el, "STOCKITEMNAME")
		if not item:
			continue
		quantity, unit = _split_quantity(el.findtext("BILLEDQTY") or el.findtext("ACTUALQTY"))
		rate, rate_unit = _split_rate(el.findtext("RATE"))
		allocation = el.find("ACCOUNTINGALLOCATIONS.LIST")
		batch = el.find("BATCHALLOCATIONS.LIST")
		hsn = el.find("HSNDETAILS.LIST")
		out.append(
			TallyItemLine(
				item=item,
				quantity=quantity,
				unit=unit or rate_unit,
				rate=rate,
				amount=_amount(el.findtext("AMOUNT")),
				ledger=_text(allocation, "LEDGERNAME") if allocation is not None else "",
				godown=_text(batch, "GODOWNNAME") if batch is not None else "",
				hsn=(_text(hsn, "HSNCODE") if hsn is not None else "") or _text(el, "HSNCODE"),
			)
		)
	return out


def iter_vouchers(source, skip_unposted=True):
	"""Each voucher in a transactions export.

	Cancelled, optional and deleted vouchers are dropped by default: Tally
	exports them and posts none of them, so importing them would put entries in
	ERPNext that Tally's own trial balance does not have.
	"""
	stream = open_export(source)
	for el in iter_elements(stream, ("VOUCHER",)):
		if skip_unposted and (_yes(el, "ISCANCELLED") or _yes(el, "ISOPTIONAL") or _yes(el, "ISDELETED")):
			continue
		yield TallyVoucher(
			guid=_text(el, "GUID") or (el.get("REMOTEID") or ""),
			kind=(el.get("VCHTYPE") or _text(el, "VOUCHERTYPENAME")).strip(),
			date=_date(el.findtext("DATE")),
			number=_text(el, "VOUCHERNUMBER"),
			reference=_text(el, "REFERENCE"),
			reference_date=_date(el.findtext("REFERENCEDATE")),
			narration=_text(el, "NARRATION"),
			party=_text(el, "PARTYLEDGERNAME") or _text(el, "PARTYNAME"),
			is_invoice=_yes(el, "ISINVOICE"),
			postings=_postings(el),
			items=_items(el),
		)
	# The caller may want to know what was cleaned out of the file it just read.
	iter_vouchers.last_removed = stream.removed
