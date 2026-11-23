"""
Reading ERPNext: settings, child tables in bulk, addresses, dates and text.
"""

from __future__ import annotations

import html
import re

import frappe
from frappe.utils import cstr

from tally_connector.tally.model import (
	Address,
)
from tally_connector.utils.ledgers import (
	ledger_name,
)


def _settings():
	return frappe.get_single("Tally Settings")


def _address(raw) -> Address | None:
	if not raw:
		return None
	lines = [l for l in (raw.get("address_line1"), raw.get("address_line2")) if l]
	city, pin = raw.get("city", ""), raw.get("pincode", "")
	if city and pin:
		lines.append(f"{city} - {pin}")
	elif city or pin:
		lines.append(city or pin)
	if not any([lines, raw.get("state"), raw.get("gstin")]):
		return None
	state, country = raw.get("state", ""), raw.get("country", "")
	if state and not _in_india(country):
		# Tally's state list on an Indian company holds Indian states, and
		# `STATENAME`, `PLACEOFSUPPLY` and `PRIORSTATENAME` are all read against
		# it - so `California` on a US customer is a reference to a master the
		# company has not got. The state is kept as a line of the postal
		# address, where it is text nobody validates, and the structural fields
		# are left out.
		lines.append(state)
		state = ""
	return Address(
		lines=lines,
		state=state,
		country=country,
		pincode=pin,
		gstin=raw.get("gstin", ""),
		registration_type=_gst_registration(raw.get("gst_category", "")),
	)


def _in_india(country):
	"""Whether an address is one Tally's Indian state and GST fields apply to.

	A blank country counts: every ERPNext address on an Indian company that
	nobody filled in is an Indian one, and dropping the state of all of them
	would take the state off most of the file.
	"""
	return (country or "").strip().lower() in ("", "india")


# ERPNext's GST Category, mapped onto the five words Tally accepts. Tally
# validates this field against its own list, so `Overseas`, `Regular-SEZ`,
# `Deemed Exports - Refund`, `UIN Holders` and `Tax Deductor` - which the first
# version of this table sent through unchanged - are values it refuses.
#
# The four that fold onto `Regular` all hold a registration number, which is
# what Tally means by Regular; the distinctions they lose are ones Tally keeps
# in fields this connector does not write. `Overseas` folds there too rather
# than onto the unregistered word, because the block it sits in is emitted only
# where there is a GSTIN, and a GSTIN beside "Unregistered" is a contradiction.
#
# `Unregistered/Consumer` is one token and not two. It is what Tally writes in
# 808 of 1,160 real voucher tags, and what its own Excel template lists.
_GST_REGISTRATION = {
	"Registered Regular": "Regular",
	"Registered Composition": "Composition",
	"Consumer": "Unregistered/Consumer",
	"Unregistered": "Unregistered/Consumer",
	"SEZ": "Regular",
	"Deemed Export": "Regular",
	"UIN Holders": "Regular",
	"Tax Deductor": "Regular",
	"Tax Collector": "Regular",
	"Input Service Distributor": "Regular",
	"Overseas": "Regular",
}


def _gst_registration(category):
	return _GST_REGISTRATION.get((category or "").strip(), "Regular")


def _date_filter(filters, field, from_date, to_date):
	if from_date and to_date:
		filters[field] = ["between", [from_date, to_date]]
	elif from_date:
		filters[field] = [">=", from_date]
	elif to_date:
		filters[field] = ["<=", to_date]
	return filters


def _parents_first(rows, key, parent_key):
	"""Rows ordered so a parent always arrives before its children.

	Tally creates masters in the order it reads them and refuses one whose
	parent it has not seen yet - *"Stock Group 'All Item Groups' does not
	exist!"* is what a child arriving first looks like, and it aborts the file
	at that line. The account groups have been depth-sorted since they were
	written; the stock groups, cost centres and godowns were not, and the order
	`frappe.get_all` happens to return is not the tree's.

	Unlike the account roots, an ERPNext root **is** kept here: an Item can sit
	directly in `All Item Groups` and a voucher can allocate to the root cost
	centre, so dropping it would leave those references pointing at nothing.
	"""
	by_name = {r.get(key): r for r in rows}

	def depth(row):
		d, cur = 0, row.get(parent_key)
		while cur and cur in by_name and d < 50:
			d, cur = d + 1, by_name[cur].get(parent_key)
		return d

	return sorted(rows, key=depth)


def _plain_text(value):
	"""ERPNext rich text, as text Tally can show.

	`Item.description` is a Text Editor field, so what it holds is HTML. Copied
	straight through it reaches Tally as a literal `<div><p>...</p></div>` in the
	item's description - measured on a real chart of 9,423 items, 93 of them
	carried markup and 37 more carried an entity. Nothing refuses the import; it
	just reads as rubbish on every affected item for ever.

	Deliberately not a parser. A block tag becomes a space so two paragraphs do
	not run into one word, everything else is dropped, entities are resolved, and
	whitespace is collapsed. Anything cleverer would be a second opinion about
	HTML in a module whose job is reading ERPNext.
	"""
	text = cstr(value or "")
	if not text:
		return ""
	if "<" in text:
		text = re.sub(r"(?i)<\s*(br|/p|/div|/li|/tr|/h[1-6])\s*[^>]*>", " ", text)
		text = re.sub(r"<[^>]+>", "", text)
	if "&" in text:
		text = html.unescape(text)
	return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# ERPNext reads and naming authorities.
#
# These came here when the XML generators were removed, rather than being left
# behind in a module named after the thing that went. Every one of them is a
# read of ERPNext or a decision about ERPNext data, which is what this module is
# for. Nothing downstream of here sees them.
# ---------------------------------------------------------------------------


def _chunks(seq, size=500):
	"""`IN (...)` clauses are chunked rather than handed 11,000 names at once."""
	seq = list(seq)
	for i in range(0, len(seq), size):
		yield seq[i : i + size]


def _children_by_parent(doctype, parents, fields):
	"""All child rows for these parents, grouped by parent, in `idx` order.

	`idx` ordering matters: it is the order the lines appear on the document,
	and therefore the order they appear on the voucher. `get_doc` gave that for
	free and a bare `get_all` does not.

	A field the doctype does not have is dropped rather than selected. Callers
	ask for a superset on purpose - `add_deduct_tax` is on Purchase Taxes and
	Charges and not on the Sales one - and `doc.get()` answered None for those
	without complaining, where naming them in a SELECT is a hard SQL error. A
	dropped field reads back as None, which is what the calling code already
	handles.
	"""
	out = {}
	if not parents:
		return out
	present = [f for f in fields if frappe.db.has_column(doctype, f)]
	for chunk in _chunks(parents):
		rows = frappe.get_all(
			doctype,
			filters={"parent": ["in", chunk]},
			fields=["parent", "idx", *present],
			order_by="parent asc, idx asc",
			limit_page_length=0,
		)
		for row in rows:
			out.setdefault(row.parent, []).append(row)
	return out


_ADDRESS_FIELDS = ("address_line1", "address_line2", "city", "state", "country", "pincode")


def _addresses_for(party_type, names):
	"""The primary address of many parties, in one query per chunk.

	The ordering matches `frappe.db.get_value`, which is what this replaces:
	that defaults to `order_by="creation"`, and Frappe's query builder reads a
	bare column name as DESC - so it returns the party's **newest** address.
	Determined by running both against parties holding four addresses and
	comparing, not by reading the query builder, and held byte-for-byte by
	`tests/test_bulk_reads.py`.

	Whether newest is the right address for a customer's billing details is a
	separate question - ERPNext carries `is_primary_address` and neither this
	nor the code it replaces looks at it. Changing that would change what
	reaches Tally, so it stays a decision rather than a side effect of making
	the export faster.

	`gstin` and `gst_category` come from india_compliance and are selected only
	where the columns exist, so this works on a bench without it.
	"""
	fields = list(_ADDRESS_FIELDS)
	for extra in ("gstin", "gst_category"):
		if frappe.db.has_column("Address", extra):
			fields.append(extra)

	empty = {k: "" for k in (*_ADDRESS_FIELDS, "gstin", "gst_category")}
	out = {}
	for chunk in _chunks(names):
		rows = frappe.get_all(
			"Dynamic Link",
			filters={"link_doctype": party_type, "link_name": ["in", chunk], "parenttype": "Address"},
			fields=["link_name", "parent"],
			order_by="link_name asc, creation desc, name desc",
			limit_page_length=0,
		)
		first = {}
		for row in rows:
			first.setdefault(row.link_name, row.parent)
		if not first:
			continue
		addresses = {
			a.name: a
			for a in frappe.get_all(
				"Address",
				filters={"name": ["in", list(set(first.values()))]},
				fields=["name", *fields],
				limit_page_length=0,
			)
		}
		for party, address_name in first.items():
			a = addresses.get(address_name)
			if not a:
				continue
			out[party] = {**empty, **{k: cstr(a.get(k) or "") for k in fields}}
	return out


def _strip_company(account_name, company=None):
	"""Kept as the name the rest of this module already calls.

	The implementation now lives in `utils/ledgers.ledger_name`, which matches
	the company abbreviation exactly instead of splitting on the last " - " it
	can find, and - far more importantly - is the same function the Chart of
	Accounts export uses to name the ledger it creates. That those two were
	different was the defect: 806 voucher lines referenced a ledger spelled one
	way while the masters created it spelled another.
	"""
	return ledger_name(account_name, company)
