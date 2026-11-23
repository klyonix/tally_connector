"""
The names Tally will know things by: parties, items, units, cost centres,
GST ledgers, cash and bank, and the kind of voucher each document becomes.
"""

from __future__ import annotations

import re

import frappe
from frappe.utils import cstr, flt
from frappe.utils.caching import request_cache

from tally_connector.tally.extract.source import _strip_company
from tally_connector.tally.extract_rules import UNIT_SYMBOL_MAX
from tally_connector.tally.model import (
	LedgerLine,
	Voucher,
)
from tally_connector.utils.ledgers import (
	GROUP_BANK,
	GROUP_CASH,
	GROUP_CREDITORS,
	GROUP_DEBTORS,
	GROUP_DUTIES,
	GROUP_ROUNDING,
	GROUP_SUSPENSE,
	ledger_name,
	party_ledger_name,
	reserved_group,
)


def bill_reference_name(doctype, name, bill_no=None):
	"""The string both sides of a bill reference use.

	The ERPNext document name, not the supplier's own bill number. Tally matches
	bills by this string, so the invoice that opens the bill and the payment that
	settles it have to agree - and a payment references the ERPNext name, which
	is what it has. The supplier's number is not unique either: two purchase
	invoices can carry the same bill number, and using it would merge two
	different bills into one outstanding.

	The supplier's number still reaches Tally, on the voucher's REFERENCE field,
	which is where somebody reconciling against a physical bill will look.
	"""
	return cstr(name)


def gst_ledgers(company):
	"""Which tax ledgers are GST, and which duty head each carries.

	Asked of `india_compliance`, which owns the answer, rather than inferred
	from the account's name. It knows all four sets - output, input and both
	reverse-charge - and which of CGST, SGST, IGST or Cess an account is, none
	of which is reliably recoverable from a string.

	Returns {} where india_compliance is not installed, so this works on a bench
	without it and the ledgers are simply not classified.
	"""
	return {ledger_name(account, company): head for account, head in gst_accounts(company).items()}


@request_cache
def gst_accounts(company):
	"""The same answer keyed on the ERPNext account rather than the Tally ledger.

	Which is what reading an `Item Tax Template` needs: its rows name accounts,
	and the rate on each only means something once you know whether that account
	is CGST, SGST, IGST or Cess.
	"""
	mapping = {}
	try:
		from india_compliance.gst_india.utils import get_gst_accounts_by_type

		heads = {
			"cgst_account": "CGST",
			"sgst_account": "SGST",
			"igst_account": "IGST",
			"cess_account": "Cess",
			"cess_non_advol_account": "Cess",
		}
		for account_type in ("Output", "Input", "Sales Reverse Charge", "Purchase Reverse Charge"):
			try:
				accounts = get_gst_accounts_by_type(company, account_type, throw=False)
			except Exception:
				continue
			for field, duty_head in heads.items():
				account = (accounts or {}).get(field)
				if account:
					mapping[account] = duty_head
	except ImportError:
		pass
	return mapping


@request_cache
def _party_names():
	"""Party id -> the ledger name Tally knows it by, for all three party types.

	Resolved from the party's **id**, which does not change, rather than from the
	name a document snapshotted when it was raised. ERPNext copies
	`customer_name` onto the invoice at the time, so renaming a customer leaves
	every historical document carrying the old spelling while the master creates
	the new one - two ledgers in Tally for one customer, with the outstanding
	split between them and a bill under one name that a receipt under the other
	can never settle. Found by comparing the export against ERPNext's own ledger:
	one renamed customer had a single invoice and a single receipt still under
	the old spelling, and their balance came out 1,500 adrift.

	This is the same defect `stock_item_name` was written for, and the same fix:
	resolve from the thing that does not change, and keep the snapshot only as a
	fallback for a party that has since been deleted.
	"""
	names = {}
	for doctype, field in (
		("Customer", "customer_name"),
		("Supplier", "supplier_name"),
		("Employee", "employee_name"),
	):
		for row in frappe.get_all(doctype, fields=["name", field], limit_page_length=0):
			names[(doctype, row.name)] = party_ledger_name(row.get(field) or row.name)
	return names


def _party_name(party_type, party_id, fallback=""):
	"""One party, one ledger name, wherever it is referenced.

	The fallback is the document's own snapshot, which is all there is for a
	party that has since been deleted - at least the voucher stays readable.
	"""
	if party_type and party_id:
		known = _party_names().get((party_type, party_id))
		if known:
			return known
	return party_ledger_name(fallback or party_id or "")


def cost_centre_name(name, company):
	"""A cost centre's Tally name: its own, without the company suffix.

	ERPNext names a cost centre `Main - KTC` exactly as it names an account, and
	a suffix that means nothing in Tally would appear on every allocation.
	"""
	return ledger_name(name, company)


def _group_for(line: LedgerLine, voucher: Voucher):
	"""Where a ledger with no ERPNext account behind it should sit."""
	if line.is_party:
		return GROUP_DEBTORS if voucher.kind in ("Sales", "Credit Note", "Receipt") else GROUP_CREDITORS
	upper = line.ledger.upper()
	if "TDS" in upper or "GST" in upper or "TAX" in upper:
		return GROUP_DUTIES
	if "SUSPENSE" in upper:
		return GROUP_SUSPENSE
	if "ROUND" in upper:
		return GROUP_ROUNDING
	return None


_UNIT_SAFE = re.compile(r"[^A-Za-z0-9 \-_.%]")


# Tally's UNIT `NAME` is the **symbol**, not the long name, and Tally refused
# `Square Meter` and `Parts Per Million` with *"BAD UNIT NAME"* while accepting
# `Nos` and `BAG`. It stops the masters file at that line - so the ledgers after
# it were never created, and every voucher then failed on a ledger that does not
# exist. One refused unit, and four rounds spent looking at vouchers.
#
# Tally documents no rule for what makes a symbol bad. What both refusals have
# and both acceptances lack is a space, and length. So this produces a symbol
# with neither, which satisfies either rule without having to know which one it
# is; the full ERPNext name still travels as the unit's formal name, so nothing
# is lost in Tally's own unit list.

# How an accountant writes the long words, so a symbol that has to be short
# still reads as what it is. Without it `Cubic Meter` came out as `CM2`, which
# reads as square centimetres - wrong on a tiles business, whose whole trade is
# square and cubic measure.
_UNIT_WORDS = {
	"square": "Sq",
	"cubic": "Cu",
	"meter": "Mtr",
	"metre": "Mtr",
	"foot": "Ft",
	"feet": "Ft",
	"inch": "In",
	"yard": "Yd",
	"centimeter": "Cm",
	"centimetre": "Cm",
	"millimeter": "Mm",
	"millimetre": "Mm",
	"kilometer": "Km",
	"kilometre": "Km",
	"kilogram": "Kg",
	"gram": "Gm",
	"litre": "Ltr",
	"liter": "Ltr",
	"millilitre": "Ml",
	"milliliter": "Ml",
	"tonne": "Ton",
	"dozen": "Dz",
	"packet": "Pkt",
	"bundle": "Bdl",
	"carton": "Ctn",
	"piece": "Pc",
	"pieces": "Pcs",
	"number": "No",
	"numbers": "Nos",
}


def _unit_shorten(safe):
	"""A symbol at most `UNIT_SYMBOL_MAX` characters with no space in it.

	Four steps, each tried only if the one before is still too long: the name
	joined up (`Meter` stays `Meter`); its words abbreviated the way an
	accountant writes them (`Square Meter` is `SqMtr`, `Cubic Meter` is
	`CuMtr`); its initials (`Parts Per Million` is `PPM`); and last, cut short.
	"""
	words = safe.split()
	joined = "".join(words)
	if len(joined) <= UNIT_SYMBOL_MAX:
		return joined
	abbreviated = "".join(_UNIT_WORDS.get(w.lower(), w) for w in words)
	if len(abbreviated) <= UNIT_SYMBOL_MAX:
		return abbreviated
	if len(words) > 1:
		initials = "".join(w[0] for w in words).upper()
		if len(initials) <= UNIT_SYMBOL_MAX:
			return initials
	return abbreviated[:UNIT_SYMBOL_MAX]


def _unit_sanitise(uom):
	name = cstr(uom or "").strip()
	if not name:
		return ""
	safe = _UNIT_SAFE.sub(" ", name)
	# Collapse what the substitution leaves behind. Replacing with a space and
	# collapsing gives "Pint, Dry (US)" -> "Pint Dry US"; replacing with a
	# hyphen gave "Pint- Dry -US", which is worse to read for no gain.
	return re.sub(r"\s+", " ", safe).strip(" -")


@request_cache
def _unit_symbols():
	"""ERPNext UOM name -> the symbol Tally will know it by, collisions resolved.

	Built once per request over every UOM on the site rather than over the ones
	in use, so the answer for a given UOM does not change with the date range -
	a unit named one way in the masters and another in a voucher is the defect
	this whole area exists to prevent.

	Two UOMs shortening to one symbol would be one unit in Tally holding both
	their quantities. That is the stock-item collision, on units, and it is
	resolved the same way: by making the second one different.
	"""
	out, taken = {}, {}
	for name in sorted(frappe.get_all("UOM", pluck="name", limit_page_length=0) or []):
		safe = _unit_sanitise(name)
		if not safe:
			continue
		symbol = _unit_shorten(safe)
		if taken.get(symbol, name) != name:
			stem, n = symbol[: UNIT_SYMBOL_MAX - 1], 2
			while taken.get(f"{stem}{n}", name) != name:
				n += 1
			symbol = f"{stem}{n}"
		taken[symbol] = name
		out[name] = symbol
	return out


def unit_name(uom):
	"""A unit symbol Tally will accept, derived from the ERPNext UOM name.

	Anything outside letters, digits, space, hyphen, underscore, full stop and
	per-cent goes; what is left is shortened to a symbol with no space in it,
	because that is what Tally refused.

	The same function names the unit on the unit master, on a stock item's
	BASEUNITS and in an inventory line's rate and quantity, so the three cannot
	disagree - which is the other half of what makes an import succeed.
	"""
	name = cstr(uom or "").strip()
	if not name:
		return ""
	symbols = _unit_symbols()
	known = symbols.get(name)
	if known:
		return known
	# A row naming a UOM that is not a UOM record. Shortened the same way, and
	# then held against the symbols the registered units already took.
	#
	# It used to skip that second half, so an unregistered `Numbers` could
	# shorten to a symbol a real UOM already owned. `extract_masters` then
	# dropped the duplicate silently, and the item and the unit master ended up
	# naming the same symbol for two different units - a wrong answer rather
	# than a refused file, which is the worse kind.
	symbol = _unit_shorten(_unit_sanitise(name)) or "Unit"
	taken = set(symbols.values())
	if symbol in taken:
		stem, n = symbol[: UNIT_SYMBOL_MAX - 1], 2
		while f"{stem}{n}" in taken:
			n += 1
		symbol = f"{stem}{n}"
	return symbol


def units_in_use(company=None, from_date=None, to_date=None):
	"""Only the units something actually refers to.

	ERPNext ships 245 units and a real company uses a handful. Sending all of
	them puts 245 rows of noise into somebody's Tally, and every one is another
	chance to hit a name Tally refuses.

	It took all three arguments and used none of them, while the docstring said
	"the invoice lines in range" - so it read every invoice line on the site.
	On a site with several companies that put one company's units into another
	company's Tally, which is the same class of fault as the scheduler pushing
	every company's books into one destination.

	**Every item's own unit is still included whatever the range**, because
	`extract_masters` exports every `Item` and a stock item whose `BASEUNITS`
	no unit master creates is an import Tally refuses. Narrowing that half
	would trade noise for a broken file.
	"""
	used = set(frappe.get_all("Item", pluck="stock_uom", limit_page_length=0) or [])
	for child, parent in (
		("Sales Invoice Item", "Sales Invoice"),
		("Purchase Invoice Item", "Purchase Invoice"),
	):
		c, p = frappe.qb.DocType(child), frappe.qb.DocType(parent)
		query = (
			frappe.qb.from_(c)
			.join(p)
			.on(p.name == c.parent)
			.select(c.uom)
			.distinct()
			.where(c.parenttype == parent)
		)
		if company:
			query = query.where(p.company == company)
		if from_date:
			query = query.where(p.posting_date >= from_date)
		if to_date:
			query = query.where(p.posting_date <= to_date)
		try:
			used |= {uom for uom in query.run(pluck=True) if uom}
		except Exception:
			# An odd bench without the column is not a reason to lose the
			# masters; the items' own units are already in.
			pass
	return {u for u in used if u}


def _round_off_ledger(company):
	"""ERPNext's own round-off account for this company, if it has one."""
	account = frappe.get_cached_value("Company", company, "round_off_account")
	return _strip_company(account, company) if account else ""


def stock_item_name(code, fallback=""):
	"""The one name a stock item is known by, on the master and on every voucher.

	An invoice line carries the item's name *as it was when the invoice was
	raised*. The stock item master carries the name the Item has now. Rename an
	item and the two diverge silently: a voucher references the old name
	while the master creates the new one, and
	Tally answers `Stock Item does not exist`.

	So the name is resolved from the item code - the thing that does not change -
	in both places. The line's own name is the fallback for an item that has
	since been deleted, which at least keeps the voucher readable.

	**Where two items share an `item_name`, both fall back to their code.**
	Tally identifies a stock item by its name and nothing else, so two items
	named alike are one item in Tally holding both their stock - a white
	variant of a product counted as the grey one. Two items are two items, and
	nothing is merged.

	The item code is the only thing ERPNext holds that tells them apart, and on
	many charts it is the descriptive name somebody typed - `COVER HEAVY DUTY
	WHITE (36X36)`. It is used for **every** item in a colliding
	group rather than for all but one, so which item keeps its name is not a
	silent choice; in practice the item whose code and name already agree is
	unchanged, and only the one carrying a copied name moves.
	"""
	if not code:
		return cstr(fallback).strip()
	return _item_names().get(code) or cstr(fallback).strip() or cstr(code).strip()


@request_cache
def _item_names():
	"""Item code -> the name Tally will know it by, with collisions resolved.

	Built once per request, because it has to answer the same way for the master
	and for every voucher line - a name resolved one way in one place and another
	way in the other is the defect this whole function exists to prevent.
	"""
	rows = frappe.get_all("Item", fields=["name", "item_name"], order_by="name asc", limit_page_length=0)
	# Stripped for the same reason party names are: whitespace at the ends of a
	# Tally name is invisible and never meaningful, and two names differing only
	# by it are two stock items. An item name with a trailing space is common.
	display = {r.name: cstr(r.item_name or r.name).strip() for r in rows}

	shared = {}
	for code, name in display.items():
		shared.setdefault(name, []).append(code)

	for codes in shared.values():
		if len(codes) > 1:
			for code in codes:
				display[code] = cstr(code).strip()
	return display


@request_cache
def cash_and_bank(company):
	"""The ledgers Tally will treat as money, keyed to their Tally group.

	ERPNext puts bank accounts wherever the chart happens to put them - on this
	bench under Current Assets, Cash In Hand, Bank Accounts and Wallets. Tally
	does not accept that: a ledger has to sit under one of its own reserved
	groups for bank reconciliation to offer it, for a Contra to accept it, and
	for it to be excluded from a Journal.
	"""
	rows = frappe.get_all(
		"Account",
		filters={"company": company, "account_type": ["in", ["Bank", "Cash"]]},
		fields=["name", "account_type"],
		limit_page_length=0,
	)
	return {
		ledger_name(r.name, company): (GROUP_BANK if r.account_type == "Bank" else GROUP_CASH) for r in rows
	}


# Tally's reserved groups that mean money.
MONEY_GROUPS = frozenset({GROUP_CASH, GROUP_BANK, "Bank OD A/c", "Bank OCC A/c"})


def _lands_in_money(group, group_parents):
	"""Whether a group, or a group above it, is one of Tally's money groups.

	Compared by Tally's own matching, since a created group spelled like one of
	Tally's is Tally's.
	"""
	steps = 0
	while group and steps < 50:
		if (reserved_group(group) or group) in MONEY_GROUPS:
			return True
		group, steps = group_parents.get(group), steps + 1
	return False


def voucher_kind_for(postings, money, settings, erpnext_type=None):
	"""Which Tally voucher a set of postings belongs on.

	Tally's taxonomy is about what the voucher touches, not what ERPNext called
	it. Money moving only between cash and bank accounts is a Contra; money
	coming in is a Receipt; money going out is a Payment; anything not touching
	money at all is a Journal.

	This matters because Tally's Journal **excludes cash and bank ledgers** -
	`Allow Cash Accounts in Journals` is off by default - and 520 of the 1,222
	vouchers this app exported were Journals holding a bank ledger. Every one of
	them would have been refused.

	`postings` is [(ledger, signed amount)]; negative is a debit, so money coming
	in is a negative total on the cash side.
	"""
	direct = {
		"Credit Note": settings.get("credit_note_voucher_type") or "Credit Note",
		"Debit Note": settings.get("debit_note_voucher_type") or "Debit Note",
	}
	if erpnext_type in direct:
		return direct[erpnext_type]

	touching = [(l, a) for l, a in postings if l in money]
	if not touching:
		return settings.journal_voucher_type or "Journal"
	if len(touching) == len([p for p in postings if p[0]]):
		return settings.get("contra_voucher_type") or "Contra"
	total = sum(flt(a, 2) for _l, a in touching)
	if total < 0:
		return settings.receipt_voucher_type or "Receipt"
	return settings.payment_voucher_type or "Payment"
