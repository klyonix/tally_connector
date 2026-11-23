"""
What the destination Tally company already holds.

The connector used to send every master as `ACTION="Create"` and let Tally work
out the rest. A real import answered `Created: 9,626  Altered: 9,694` - nine and
a half thousand masters an accountant maintains, overwritten by ours, because
nothing had asked what was there.

This is the asking. It is the input to `resolve.py`, which decides per master
whether to reuse, create, alter or stop.

**Unknown is not empty, and the distinction is the whole design.** A collection
Tally did not answer leaves that master type `None`, and a `None` means "do not
guess" rather than "create everything". Reading a failed probe as an empty
company is how a connector comes to overwrite a chart of accounts it could not
see. Every accessor here is written so that the unknown case is the one that
refuses.

**Names are matched the way Tally matches them** - ignoring case and the
punctuation between words - because that is how Tally decided `Stock In Hand`
and `Stock-in-Hand` were the same master, which cost a real import.

Pure: no frappe, no network. `connection.py` does the talking and hands the
bytes here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tally_connector.tally.extract_rules import tally_key

# The master types this asks about, as (attribute, Tally collection id, Tally
# type name, the fields worth having). Kept in one table so adding a type is
# one row rather than a new method.
COLLECTIONS = (
	("groups", "KXGroups", "Group", ("Name", "Parent", "IsRevenue", "IsDeemedPositive")),
	(
		"ledgers",
		"KXLedgers",
		"Ledger",
		("Name", "Parent", "OpeningBalance", "IsCostCentresOn", "IsBillWiseOn", "AffectsStock"),
	),
	("units", "KXUnits", "Unit", ("Name", "OriginalName", "BaseUnits", "IsSimpleUnit")),
	("stock_groups", "KXStockGroups", "StockGroup", ("Name", "Parent")),
	("stock_items", "KXStockItems", "StockItem", ("Name", "Parent", "BaseUnits")),
	("godowns", "KXGodowns", "Godown", ("Name", "Parent")),
	("cost_centres", "KXCostCentres", "CostCentre", ("Name", "Parent", "Category")),
	("currencies", "KXCurrencies", "Currency", ("Name", "OriginalSymbol")),
)

# The element Tally wraps each record in, per collection type.
ELEMENT = {
	"Group": "GROUP",
	"Ledger": "LEDGER",
	"Unit": "UNIT",
	"StockGroup": "STOCKGROUP",
	"StockItem": "STOCKITEM",
	"Godown": "GODOWN",
	"CostCentre": "COSTCENTRE",
	"Currency": "CURRENCY",
}


@dataclass
class TallyMaster:
	"""One master the destination already holds."""

	name: str
	parent: str = ""
	extra: dict = field(default_factory=dict)


@dataclass
class TallySnapshot:
	"""Everything the destination holds, or `None` where it did not say.

	`company` is the name Tally reports for the open company, which is what
	`SVCURRENTCOMPANY` has to match exactly - a mismatch there is a silent
	no-op that used to be reported as a completed export.
	"""

	company: str = ""
	companies: list[str] | None = None
	groups: dict[str, TallyMaster] | None = None
	ledgers: dict[str, TallyMaster] | None = None
	units: dict[str, TallyMaster] | None = None
	stock_groups: dict[str, TallyMaster] | None = None
	stock_items: dict[str, TallyMaster] | None = None
	godowns: dict[str, TallyMaster] | None = None
	cost_centres: dict[str, TallyMaster] | None = None
	currencies: dict[str, TallyMaster] | None = None
	voucher_types: dict[str, object] | None = None
	# Why each unanswered collection is unanswered, for the report.
	unavailable: dict[str, str] = field(default_factory=dict)

	def known(self, kind: str) -> bool:
		"""Whether Tally answered about this master type at all."""
		return getattr(self, kind, None) is not None

	def holds(self, kind: str, name: str) -> TallyMaster | None:
		"""The master of that name the destination holds, or None.

		None where Tally did not say as well as where it said no, which is why
		every caller asks `known()` first. Collapsing the two is the defect
		this module exists to prevent.
		"""
		held = getattr(self, kind, None)
		if not held:
			return None
		return held.get(tally_key(name))

	def refuses_cost_centres(self, name: str):
		"""Whether this ledger in the destination will refuse an allocation.

		`None` where Tally was not asked or did not say. Tally refuses a cost
		centre allocation on a ledger whose own setting is off, and a reused
		master keeps the destination's setting rather than ours - which is how
		a change that stopped sending masters Tally already holds turned into
		vouchers Tally would not take. On a real company 313 of 357 ledgers
		have it off.
		"""
		held = self.holds("ledgers", name)
		if held is None:
			return None
		flag = (held.extra.get("ISCOSTCENTRESON") or "").strip().lower()
		if not flag:
			return None
		return flag != "yes"

	def carries_stock(self, name: str):
		"""Whether this ledger in the destination may carry stock.

		`None` where Tally was not asked or did not say. Tally refuses an
		invoice whose ACCOUNTINGALLOCATIONS names a ledger with *Inventory
		values are affected* off, and says nothing about which - so a reused
		master keeping the destination's setting is how a chart that looks
		complete produces vouchers Tally will not take. Proved on a live
		company: the same voucher, refused, then created, with only this flag
		altered on the one ledger.
		"""
		held = self.holds("ledgers", name)
		if held is None:
			return None
		flag = (held.extra.get("AFFECTSSTOCK") or "").strip().lower()
		if not flag:
			return None
		return flag == "yes"

	def ledgers_refusing_cost_centres(self) -> set:
		"""Every ledger the destination will not accept an allocation on."""
		out = set()
		for held in (self.ledgers or {}).values():
			flag = (held.extra.get("ISCOSTCENTRESON") or "").strip().lower()
			if flag and flag != "yes":
				out.add(held.name)
		return out

	def anything_known(self) -> bool:
		return any(self.known(kind) for kind, *_ in COLLECTIONS)

	def summary(self) -> dict:
		"""Counts per master type, or None where unknown. For the report."""
		out = {}
		for kind, *_ in COLLECTIONS:
			held = getattr(self, kind, None)
			out[kind] = None if held is None else len(held)
		out["voucher_types"] = None if self.voucher_types is None else len(self.voucher_types)
		return out


def parse_masters(xml_bytes, element) -> dict[str, TallyMaster] | None:
	"""Tally's answer to one collection, keyed the way Tally matches names.

	Returns None when the answer cannot be read at all, which the caller must
	keep as unknown. An answer that parses and holds no records of this type is
	a genuine empty, and comes back as `{}`.
	"""
	try:
		from lxml import etree

		root = etree.fromstring(xml_bytes, etree.XMLParser(recover=True))
	except Exception:
		return None
	if root is None:
		return None

	out: dict[str, TallyMaster] = {}
	for el in root.iter(element):
		name = (el.get("NAME") or el.findtext("NAME") or "").strip()
		if not name:
			continue
		key = tally_key(name)
		if key in out:
			continue
		extra = {}
		for child in el:
			text = (child.text or "").strip()
			if text and child.tag not in ("NAME", "PARENT"):
				extra.setdefault(child.tag, text)
		out[key] = TallyMaster(
			name=name,
			parent=(el.findtext("PARENT") or "").strip(),
			extra=extra,
		)
	return out


def parse_companies(xml_bytes) -> list[str] | None:
	"""The companies Tally has, from a `List of Companies` export.

	`test_connection` has fetched this since the connector was written and
	threw the body away, so nothing ever checked that the company named in
	settings is one Tally actually has.
	"""
	try:
		from lxml import etree

		root = etree.fromstring(xml_bytes, etree.XMLParser(recover=True))
	except Exception:
		return None
	if root is None:
		return None
	names = []
	for el in root.iter("COMPANY"):
		name = (el.get("NAME") or el.findtext("NAME") or el.findtext("COMPANYNAME") or "").strip()
		if name and name not in names:
			names.append(name)
	if not names:
		# Some builds answer a bare list of names rather than COMPANY elements.
		for el in root.iter("NAME"):
			name = (el.text or "").strip()
			if name and name not in names:
				names.append(name)
	return names or None


def company_matches(snapshot: TallySnapshot, wanted: str) -> tuple[bool, str]:
	"""Whether the company we are about to write to is one Tally has.

	Returns (ok, explanation). Unknown is ok: a Tally that did not answer must
	not block an export that would otherwise work, because the offline harness
	has always been allowed to run without Tally at all. What must not happen
	is treating an answer that *did* come back and did not name the company as
	though it had.
	"""
	wanted = (wanted or "").strip()
	if not wanted:
		return False, "No company name is set for Tally in settings."
	if snapshot.companies is None:
		return True, ""
	if any(tally_key(name) == tally_key(wanted) for name in snapshot.companies):
		return True, ""
	return False, (
		f"Tally has no company called {wanted!r}. It has "
		f"{', '.join(repr(c) for c in snapshot.companies[:5])}. "
		"SVCURRENTCOMPANY must match exactly, and a mismatch imports nothing "
		"while reporting success."
	)
