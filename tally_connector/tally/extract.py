"""
ERPNext -> the normalised model. The only module in this package that imports
frappe, and the only one that ever should.

Everything downstream of here - mapping, validation, rendering - is pure, so it
can be exercised against a JSON fixture in milliseconds with no site. That split
is the whole reason this layer exists as its own file rather than as more code
inside the generators.

What it does *not* do is decide anything about Tally. The voucher kind, the sign
convention, the ledger naming and the group placement are all decisions this
module makes because they depend on ERPNext data - but each one is a single
named function, and the renderer downstream simply writes down what it is given.
"""

from __future__ import annotations

import datetime
import html
import re

import frappe
from frappe.utils import cstr, flt, getdate
from frappe.utils.caching import request_cache

from tally_connector.tally.accounting import without_stock
from tally_connector.tally.model import (
	ADVANCE,
	AGAINST_REF,
	NEW_REF,
	ON_ACCOUNT,
	PRIMARY_COST_CATEGORY,
	Address,
	BillRef,
	CostCentre,
	CostCentreAllocation,
	Export,
	Godown,
	Group,
	InventoryLine,
	Ledger,
	LedgerLine,
	Masters,
	StockGroup,
	StockItem,
	Unit,
	Voucher,
	money,
)
from tally_connector.utils.ledgers import (
	GROUP_BANK,
	GROUP_CASH,
	GROUP_CREDITORS,
	GROUP_DEBTORS,
	GROUP_DUTIES,
	GROUP_ROUNDING,
	GROUP_SUSPENSE,
	TALLY_PRIMARY,
	group_name,
	is_reserved_group,
	ledger_name,
	party_ledger_name,
	reserved_group,
)

ROUNDING_TOLERANCE = 1.0

# Which ERPNext documents become a bill Tally can be settled against. A payment
# may reference an Expense Claim or an Employee Advance; those are not vouchers
# this connector exports, so there is no bill in Tally to allocate to and the
# amount goes On Account rather than against a reference that does not exist.
BILLABLE = ("Sales Invoice", "Purchase Invoice")


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


def _bill_refs(line_amount, allocations, opening=None, due_date=None):
	"""Bill references that account for the whole line.

	Tally posts to *On Account* whatever a bill reference does not claim, so the
	references have to add up to the line or the party's ageing quietly drifts
	from the ledger balance. Anything unallocated is therefore named as On
	Account rather than left off.

	`allocations` are (bill name, positive amount); the sign is taken from the
	line, so a receipt and the invoice it settles carry opposite signs against
	the same bill name, which is what Tally expects.
	"""
	line_amount = flt(line_amount, 2)
	sign = -1 if line_amount < 0 else 1
	refs, claimed = [], 0.0

	if opening:
		refs.append(BillRef(name=opening, kind=NEW_REF, amount=money(line_amount), due_date=due_date))
		return refs

	for name, amount in allocations:
		amount = abs(flt(amount, 2))
		if not amount or not name:
			continue
		refs.append(BillRef(name=name, kind=AGAINST_REF, amount=money(sign * amount)))
		claimed += amount

	remainder = flt(abs(line_amount) - claimed, 2)
	if remainder > 0.005:
		refs.append(BillRef(name="", kind=ON_ACCOUNT, amount=money(sign * remainder)))
	return refs


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


def _books_begin(company):
	"""When this company's registrations take effect, for Tally's dated lists.

	The start of its earliest ERPNext fiscal year: early enough that no voucher
	predates it, which is the direction that matters - a voucher dated before
	a registration's APPLICABLEFROM sees no registration at all. A real
	company's export sets it earlier than its own books begin, so early is safe.
	"""
	try:
		rows = frappe.get_all(
			"Fiscal Year",
			fields=["year_start_date"],
			filters=[["Fiscal Year Company", "company", "=", company]],
			order_by="year_start_date asc",
			limit=1,
		)
		if not rows:
			rows = frappe.get_all(
				"Fiscal Year", fields=["year_start_date"], order_by="year_start_date asc", limit=1
			)
		if rows and rows[0].year_start_date:
			return getdate(rows[0].year_start_date)
	except Exception:
		# A missing child table on an odd bench is not a reason to lose the
		# masters file; the renderer has a floor of its own.
		pass
	return None


def _opening_balances(company, as_of):
	"""Every ledger's balance the day before this export's first voucher.

	Without these a Tally company built from our masters starts every ledger at
	nought, so its trial balance is only whatever the exported vouchers happen
	to contain. The whole of a company's history before `from_date` lived
	nowhere in this export until now.

	Two things decide where a balance goes.

	**Signs are Tally's**, as everywhere else here: negative is a debit. ERPNext
	keeps debit and credit in separate columns, so the balance is credit less
	debit and the subtraction is the conversion.

	**A party's balance belongs to the party**, not to the receivable account.
	Tally keeps a ledger per customer and ERPNext keeps one control account with
	the party named on each posting - the same difference the importer meets
	from the other side. Sending both would count the money twice, and the
	control account is the copy to drop, because no voucher this export writes
	ever posts to it.
	"""
	if not as_of:
		return {}
	# **Only balance sheet accounts carry an opening.** A profit and loss
	# ledger starts each year at nought, because last year's result closes
	# into reserves - so summing every posting ever made and calling it an
	# opening puts the whole of the company's history onto this year's income.
	#
	# Measured on a real migration: the Sales ledger arrived in Tally with an
	# opening larger than the year's whole revenue, and Tally's Profit &
	# Loss A/c read far above that revenue. The
	# movement was right the whole time; the opening was the whole of the
	# error. An income-tax return built on that would have been wrong.
	from frappe.query_builder.functions import Sum

	gl, account = frappe.qb.DocType("GL Entry"), frappe.qb.DocType("Account")
	rows = (
		frappe.qb.from_(gl)
		.join(account)
		.on(account.name == gl.account)
		.select(gl.account, gl.party_type, gl.party, (Sum(gl.credit) - Sum(gl.debit)).as_("balance"))
		.where(
			(gl.company == company)
			& (gl.is_cancelled == 0)
			& (gl.posting_date < as_of)
			& (account.root_type.notin(["Income", "Expense"]) | gl.party.isnotnull())
		)
		.groupby(gl.account, gl.party_type, gl.party)
	).run(as_dict=True)

	opening = {}
	for row in rows:
		balance = money(row.balance or 0)
		if not balance:
			continue
		if row.party:
			name = _party_name(row.party_type, row.party)
		else:
			name = ledger_name(row.account, company)
		if name:
			opening[name] = opening.get(name, money(0)) + balance
	return opening


def _item_gst_rates(company):
	"""Each item's integrated GST rate, read off its own tax template.

	ERPNext keeps the rate on an `Item Tax Template`, a row per tax account, and
	which account is which half of GST is `india_compliance`'s answer rather
	than anything recoverable from the account's name. The integrated rate is
	the one Tally wants; where a template carries only the two halves they are
	added, because CGST plus SGST is the integrated rate by construction.

	Empty where india_compliance is not installed, so a bench without it simply
	sends no rates rather than guessing at them.
	"""
	heads = gst_accounts(company)
	if not heads:
		return {}
	templates = {}
	for row in frappe.get_all(
		"Item Tax Template Detail", fields=["parent", "tax_type", "tax_rate"], limit_page_length=0
	):
		head = heads.get(row.tax_type)
		if not head:
			continue
		found = templates.setdefault(row.parent, {})
		# A template names the same head once for output and once for input at
		# the same rate; the larger is still that rate.
		found[head] = max(found.get(head, money(0)), money(row.tax_rate or 0))

	rates = {}
	for parent, found in templates.items():
		rate = found.get("IGST") or (found.get("CGST", money(0)) + found.get("SGST", money(0)))
		if rate:
			rates[parent] = rate

	out = {}
	for row in frappe.get_all("Item Tax", fields=["parent", "item_tax_template"], limit_page_length=0):
		rate = rates.get(row.item_tax_template)
		if rate is not None and row.parent not in out:
			out[row.parent] = rate
	return out


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


def cost_centre_name(name, company):
	"""A cost centre's Tally name: its own, without the company suffix.

	ERPNext names a cost centre `Main - KTC` exactly as it names an account, and
	a suffix that means nothing in Tally would appear on every allocation.
	"""
	return ledger_name(name, company)


def _cost_centres(amount, centre, company):
	"""The whole line against one cost centre, or nothing.

	ERPNext carries one cost centre per row, so an allocation is either the
	entire line or absent - splitting is a Tally concept with no ERPNext source,
	and inventing a split would be inventing an analysis nobody made. Tally
	requires the allocations to total the line, which one allocation trivially
	does.
	"""
	if not centre:
		return []
	return [CostCentreAllocation(name=cost_centre_name(centre, company), amount=money(amount))]


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


def _posted_total(doc, rounding_ledger):
	"""What ERPNext actually posted to the party, which is not always the grand total.

	Where rounding is enabled and the total is not already whole, ERPNext debits
	the customer the **rounded** figure and books the difference to Round Off.
	Exporting the grand total instead put a different amount on the party than
	the books hold: on this bench 78 of 287 invoices, each out by up to fifty
	paise. It is small and it does not stay small - the bill reference carries
	the same wrong figure, so a receipt for the rounded amount never clears the
	bill and the customer's ledger in Tally keeps a residue for ever.

	The rounded figure is used only when a round-off ledger exists to take the
	difference. Without one there is nothing to balance the voucher against, and
	an unbalanced voucher is refused line by line, which is worse than the paisa.
	"""
	grand = flt(doc.get("base_grand_total"), 2)
	rounded = flt(doc.get("base_rounded_total"), 2)
	if doc.get("disable_rounded_total") or not rounded or not rounding_ledger:
		return grand
	return rounded


def _rounding_line(residual, ledger):
	"""A paisa of ERPNext rounding, and only a paisa.

	A larger residual is an exchange difference or a mapping error; posting that
	to round-off turns a voucher Tally refuses into books nobody can reconcile,
	so it is left in place for the validator to report.
	"""
	residual = flt(residual, 2)
	if not residual or abs(residual) > ROUNDING_TOLERANCE or not ledger:
		return None
	return LedgerLine(ledger=ledger, amount=money(-residual))


# ─────────────────────────────────────────────────────────────────────────────
# Masters
# ─────────────────────────────────────────────────────────────────────────────


def extract_masters(company, from_date=None, to_date=None, cost_centre_ledgers=frozenset()) -> Masters:
	"""The masters, with cost centres enabled on the ledgers that need them.

	Which ledgers those are is taken from the vouchers rather than guessed from
	the account's root type. The guess was wrong: ERPNext puts a cost centre on
	a TDS or TCS line and on a party line in a journal, none of which is income
	or expense, and Tally refuses an allocation on a ledger that has not got
	cost centres switched on. Deriving the flag from what actually carries an
	allocation cannot be wrong in that direction.
	"""
	settings = _settings()
	masters = Masters(applicable_from=_books_begin(company))

	whole = set(
		frappe.get_all("UOM", filters={"must_be_whole_number": 1}, pluck="name", limit_page_length=0) or []
	)
	for uom in sorted(units_in_use(company, from_date, to_date)):
		symbol = unit_name(uom)
		if symbol and symbol not in {u.symbol for u in masters.units}:
			masters.units.append(
				Unit(
					symbol=symbol,
					# Always, now that the symbol is a short form rather than the
					# name itself. `Nos` on its own says nothing in a unit list;
					# `Nos` with `Numbers` beside it is what Tally shows.
					formal_name=cstr(uom).strip(),
					# ERPNext already knows which units cannot be fractional, and a
					# unit that allows two decimals where it should allow none lets
					# Tally accept half a licence.
					decimal_places=0 if uom in whole else 2,
				)
			)

	item_groups = frappe.get_all(
		"Item Group", fields=["name", "item_group_name", "parent_item_group"], limit_page_length=0
	)
	group_display = {g.name: (g.item_group_name or g.name) for g in item_groups}
	for row in _parents_first(item_groups, "name", "parent_item_group"):
		masters.stock_groups.append(
			StockGroup(
				name=group_display[row.name],
				parent=group_display.get(row.parent_item_group, row.parent_item_group or ""),
			)
		)

	item_rates = _item_gst_rates(company)
	item_fields = ["name", "item_name", "item_group", "stock_uom", "description", "is_stock_item"]
	for optional in ("gst_hsn_code", "is_nil_exempt", "is_non_gst"):
		if frappe.db.has_column("Item", optional):
			item_fields.append(optional)
	# One Tally stock item per ERPNext Item. Nothing is merged.
	#
	# It briefly was: two items sharing an `item_name` were emitted once with a
	# note saying so, because Tally keys on the name and would have merged them
	# anyway. The seven pairs that produced turned out to be seven genuine pairs
	# of products, so the names are made distinct instead, in `stock_item_name`, and both items go.
	for item in frappe.get_all("Item", fields=item_fields, order_by="name asc", limit_page_length=0):
		name = stock_item_name(item.name, item.item_name)
		if not name:
			continue
		masters.stock_items.append(
			StockItem(
				name=name,
				parent=item.item_group or "",
				base_unit=unit_name(item.stock_uom) or "Nos",
				description=_plain_text(item.description),
				hsn_code=cstr(item.get("gst_hsn_code") or ""),
				type_of_supply="Goods" if item.is_stock_item else "Services",
				taxability=(
					"Non-GST"
					if item.get("is_non_gst")
					else "Nil Rated"
					if item.get("is_nil_exempt")
					else "Taxable"
				),
				gst_rate=item_rates.get(item.name),
			)
		)

	accounts = frappe.get_all(
		"Account",
		filters={"company": company},
		fields=[
			"name",
			"account_name",
			"root_type",
			"parent_account",
			"account_currency",
			"account_type",
			"is_group",
		],
		limit_page_length=0,
	)
	tree = {a.name: a for a in accounts}
	money_groups = cash_and_bank(company)

	def is_root(acc):
		"""An ERPNext account with nothing above it - `Application of Funds
		(Assets)`, `Income`, `Expenses`. Tally's equivalent layer is `Primary`,
		which it owns and which no import creates."""
		parent = acc.get("parent_account")
		return not parent or parent not in tree

	def parent_of(acc):
		"""Where this account sits in **Tally's** tree, which is not ERPNext's.

		ERPNext keeps a root layer above everything - the two sides of the
		balance sheet, income and expenses - and Tally expresses the same idea
		with its own reserved primary groups. Recreating ERPNext's roots inside
		one of those made a cycle on the first real import: the root was created
		under `Current Assets`, and ERPNext's own `Current Assets` was then
		created under the root. Tally refused the file at that line.

		So a child of a root is mapped onto Tally's primary group for its own
		root type, and the root itself is never emitted.
		"""
		parent = acc.get("parent_account")
		if parent and parent in tree and not is_root(tree[parent]):
			return group_name(parent, company)
		return TALLY_PRIMARY.get(acc.get("root_type"), "Indirect Expenses")

	def depth(acc):
		d, cur = 0, acc.get("parent_account")
		while cur and cur in tree and d < 50:
			d, cur = d + 1, tree[cur].get("parent_account")
		return d

	for acc in sorted((a for a in accounts if a.is_group), key=depth):
		name = group_name(acc.name, company)
		# Two kinds of group are deliberately not sent, and both would abort the
		# import rather than being merged away.
		#
		# An ERPNext **root** has no counterpart in Tally: its children are
		# mapped onto Tally's own primary groups above, so the root itself would
		# be a container with nothing needing it.
		#
		# A group Tally **reserves** already exists, in a place Tally owns. A
		# `GROUP` master naming one with a different parent is a re-parenting,
		# and Tally stops the file at that line - which is exactly how the first
		# real import failed.
		if is_root(acc) or is_reserved_group(name):
			continue
		masters.groups.append(Group(name=name, parent=parent_of(acc)))

	gst = gst_ledgers(company)
	group_parents = {g.name: g.parent for g in masters.groups}
	for acc in (a for a in accounts if not a.is_group):
		name = ledger_name(acc.name, company)
		duty_head = gst.get(name)
		if duty_head:
			# A GST ledger belongs under Tally's own Duties & Taxes group, for
			# the same reason a bank ledger belongs under Bank Accounts: the
			# reports read from the group and the duty head, not the name.
			parent = GROUP_DUTIES
		else:
			parent = money_groups.get(name)
			if not parent:
				parent = parent_of(acc)
				# Tally's cash and bank groups mean money: a ledger under them
				# is refused on a Journal and offered for bank reconciliation.
				# So they hold exactly what ERPNext types Cash or Bank, which
				# `money_groups` has already placed, and nothing else. A real
				# chart filed `Bad Debts` under ERPNext's `Cash In Hand` group;
				# Tally reads that group as its own `Cash-in-Hand`, so 140
				# write-off journals became journals holding a cash ledger.
				# Placed under the primary group for its root type instead, it
				# stays the asset or expense ERPNext's books say it is.
				if _lands_in_money(parent, group_parents):
					parent = TALLY_PRIMARY.get(acc.get("root_type"), "Indirect Expenses")
		masters.ledgers.append(
			Ledger(
				name=name,
				parent=parent,
				# **Not** the ERPNext account's currency, deliberately.
				#
				# Every amount this export sends is in the company's base currency -
				# `base_grand_total`, `base_net_amount`, `base_paid_amount` - which
				# is the whole point of the note at the top of `model.py`. Declaring
				# a ledger as USD and then posting INR into it is wrong even where
				# Tally has a USD currency, because Tally would read those figures
				# as dollars.
				#
				# It is also what took down the third real import: *"Line 2951:
				# Currency 'USD' does not exist!"*. A Tally company holds only the
				# currencies somebody defined in it, and this export creates none.
				# Saying nothing lets Tally use its own base currency, which is by
				# definition the right one for base-currency figures. True
				# multi-currency needs currency masters and exchange rates, and is a
				# feature rather than a repair.
				currency="",
				tax_type="GST" if duty_head else ("Others" if parent == GROUP_DUTIES else ""),
				duty_head=duty_head or "",
				cost_centres_on=name in cost_centre_ledgers,
			)
		)

	centres = frappe.get_all(
		"Cost Center",
		filters={"company": company},
		fields=["name", "cost_center_name", "parent_cost_center", "is_group"],
		limit_page_length=0,
	)
	for row in _parents_first(centres, "name", "parent_cost_center"):
		masters.cost_centres.append(
			CostCentre(
				name=cost_centre_name(row.name, company),
				parent=(cost_centre_name(row.parent_cost_center, company) if row.parent_cost_center else ""),
				category=PRIMARY_COST_CATEGORY,
			)
		)

	# Godowns are behind a switch, and off by default. Sending one to a company
	# that has not got multiple godowns enabled is a refusal, while omitting it
	# is always safe - Tally puts the stock in its primary location. Turning a
	# working import into a failed one to carry a warehouse name is the wrong
	# trade, so this is opt-in for a site that actually tracks locations.
	if settings.get("include_godowns"):
		warehouses = frappe.get_all(
			"Warehouse",
			filters={"company": company},
			fields=["name", "warehouse_name", "parent_warehouse", "is_group"],
			limit_page_length=0,
		)
		for row in _parents_first(warehouses, "name", "parent_warehouse"):
			masters.godowns.append(
				Godown(
					name=ledger_name(row.name, company),
					parent=(ledger_name(row.parent_warehouse, company) if row.parent_warehouse else ""),
				)
			)

	debtors = settings.sundry_debtors_ledger or GROUP_DEBTORS
	creditors = settings.sundry_creditors_ledger or GROUP_CREDITORS
	employees = settings.get("employee_ledger_group") or creditors
	seen = {l.name for l in masters.ledgers}

	def add_party(party_type, party_name, display, tax_id, group, addresses):
		if not display or display in seen:
			return
		seen.add(display)
		masters.ledgers.append(
			Ledger(
				name=display,
				parent=group,
				bill_wise=True,
				cost_centres_on=display in cost_centre_ledgers,
				address=_address(addresses.get(party_name))
				or (Address(gstin=cstr(tax_id)) if tax_id else None),
			)
		)

	customers = frappe.get_all("Customer", fields=["name", "customer_name", "tax_id"], limit_page_length=0)
	addresses = _addresses_for("Customer", {c.name for c in customers})
	for c in customers:
		add_party("Customer", c.name, party_ledger_name(c.customer_name), c.tax_id, debtors, addresses)

	suppliers = frappe.get_all("Supplier", fields=["name", "supplier_name", "tax_id"], limit_page_length=0)
	addresses = _addresses_for("Supplier", {s.name for s in suppliers})
	for s in suppliers:
		add_party("Supplier", s.name, party_ledger_name(s.supplier_name), s.tax_id, creditors, addresses)

	for e in frappe.get_all("Employee", fields=["name", "employee_name"], limit_page_length=0):
		add_party("Employee", e.name, party_ledger_name(e.employee_name), None, employees, {})

	# Applied last, once every ledger a balance could belong to exists, so a
	# party's opening lands on the party rather than being dropped for want of
	# the ledger that carries it.
	opening = _opening_balances(company, from_date)
	if opening:
		for ledger in masters.ledgers:
			balance = opening.get(ledger.name)
			if balance:
				ledger.opening_balance = balance

	return masters


# ─────────────────────────────────────────────────────────────────────────────
# Vouchers
#
# Each of these returns model objects rather than XML. The sign convention is
# the model's - one signed base-currency amount per line, negative for a debit -
# so a return, a foreign-currency payment and a rounding difference are all just
# numbers here rather than three different string constructions.
# ─────────────────────────────────────────────────────────────────────────────


def _sales(company, from_date, to_date, only=None, tick=None):
	settings = _settings()
	filters = _date_filter({"docstatus": 1, "company": company}, "posting_date", from_date, to_date)
	if only:
		filters["name"] = only
	invoices = frappe.get_all(
		"Sales Invoice",
		filters=filters,
		fields=[
			"name",
			"customer",
			"customer_name",
			"posting_date",
			"base_grand_total",
			"base_rounded_total",
			"disable_rounded_total",
			"remarks",
			"is_return",
			"return_against",
			"due_date",
		],
		limit_page_length=0,
	)
	names = [i.name for i in invoices]
	items = _children_by_parent(
		"Sales Invoice Item",
		names,
		[
			"item_name",
			"item_code",
			"rate",
			"base_net_rate",
			"uom",
			"base_net_amount",
			"qty",
			"income_account",
			"cost_center",
			"warehouse",
		],
	)
	taxes = _children_by_parent(
		"Sales Taxes and Charges",
		names,
		[
			"account_head",
			"base_tax_amount_after_discount_amount",
			"base_tax_amount",
			"add_deduct_tax",
			"cost_center",
		],
	)
	addresses = _addresses_for("Customer", {i.customer for i in invoices if i.customer})
	rounding = _round_off_ledger(company)
	godowns = bool(settings.get("include_godowns"))
	out = []

	for inv in invoices:
		kind = (
			(settings.get("credit_note_voucher_type") or "Credit Note")
			if inv.is_return
			else (settings.sales_voucher_type or "Sales")
		)
		party_amount = -_posted_total(inv, rounding)
		# A credit note settles the bill it was raised against; an invoice opens
		# a new one. Without either, Tally posts the amount On Account and the
		# customer's ageing stops meaning anything.
		if inv.is_return and inv.return_against:
			refs = _bill_refs(
				party_amount, [(bill_reference_name("Sales Invoice", inv.return_against), abs(party_amount))]
			)
		else:
			refs = _bill_refs(
				party_amount,
				[],
				opening=bill_reference_name("Sales Invoice", inv.name),
				due_date=inv.due_date,
			)
		lines = [
			LedgerLine(
				ledger=_party_name("Customer", inv.customer, inv.customer_name),
				amount=money(party_amount),
				is_party=True,
				bill_refs=refs,
			)
		]

		inventory, items_total = [], 0.0
		for row in items.get(inv.name, []):
			amount = flt(row.base_net_amount, 2)
			inventory.append(
				InventoryLine(
					item=stock_item_name(row.item_code, row.item_name),
					quantity=money(row.qty),
					unit=unit_name(row.uom),
					rate=_stock_rate(row, amount, row.qty),
					amount=money(amount),
					ledger=(
						_strip_company(row.income_account, company)
						if row.get("income_account")
						else (settings.sales_ledger or "Sales")
					),
					godown=(
						ledger_name(row.warehouse, company) if godowns and row.get("warehouse") else None
					),
					cost_centres=_cost_centres(amount, row.get("cost_center"), company),
				)
			)
			items_total += amount

		taxes_total = 0.0
		for tax in taxes.get(inv.name, []):
			value = flt(tax.base_tax_amount_after_discount_amount) or flt(tax.base_tax_amount)
			if not value:
				continue
			# The account keeps its own name. Renaming anything with "TDS" in
			# it to one settings value split the balance: `TDS Payable` came
			# from the chart of accounts while the vouchers referenced a
			# `TDS-Deductible` that exists nowhere in ERPNext, so Tally carried
			# both. `TCS Receivable` was left alone by the same rule, which is
			# how inconsistent a name match is.
			ledger = _strip_company(tax.account_head, company)
			lines.append(
				LedgerLine(
					ledger=ledger,
					amount=money(value),
					cost_centres=_cost_centres(value, tax.get("cost_center"), company),
				)
			)
			taxes_total += value

		extra = _rounding_line(party_amount + items_total + taxes_total, rounding)
		if extra:
			lines.append(extra)

		if tick:
			tick()
		out.append(
			Voucher(
				kind=kind,
				number=inv.name,
				date=inv.posting_date,
				remote_id=inv.name,
				narration=cstr(inv.remarks or f"Sales Invoice {inv.name}"),
				party=_party_name("Customer", inv.customer, inv.customer_name),
				party_address=_address(addresses.get(inv.customer)),
				lines=lines,
				inventory=inventory,
				is_invoice_kind=True,
			)
		)
	return out


def _purchase(company, from_date, to_date, only=None, tick=None):
	settings = _settings()
	filters = _date_filter({"docstatus": 1, "company": company}, "posting_date", from_date, to_date)
	if only:
		filters["name"] = only
	invoices = frappe.get_all(
		"Purchase Invoice",
		filters=filters,
		fields=[
			"name",
			"supplier",
			"supplier_name",
			"posting_date",
			"base_grand_total",
			"base_rounded_total",
			"disable_rounded_total",
			"bill_no",
			"remarks",
			"is_return",
			"return_against",
			"due_date",
		],
		limit_page_length=0,
	)
	names = [i.name for i in invoices]
	items = _children_by_parent(
		"Purchase Invoice Item",
		names,
		[
			"item_name",
			"item_code",
			"rate",
			"base_net_rate",
			"uom",
			"base_net_amount",
			"qty",
			"expense_account",
			"cost_center",
			"warehouse",
		],
	)
	taxes = _children_by_parent(
		"Purchase Taxes and Charges",
		names,
		[
			"account_head",
			"base_tax_amount_after_discount_amount",
			"base_tax_amount",
			"add_deduct_tax",
			"cost_center",
		],
	)
	addresses = _addresses_for("Supplier", {i.supplier for i in invoices if i.supplier})
	rounding = _round_off_ledger(company)
	godowns = bool(settings.get("include_godowns"))
	out = []

	for inv in invoices:
		kind = (
			(settings.get("debit_note_voucher_type") or "Debit Note")
			if inv.is_return
			else (settings.purchase_voucher_type or "Purchase")
		)
		party_amount = _posted_total(inv, rounding)
		if inv.is_return and inv.return_against:
			refs = _bill_refs(
				party_amount,
				[(bill_reference_name("Purchase Invoice", inv.return_against), abs(party_amount))],
			)
		else:
			refs = _bill_refs(
				party_amount,
				[],
				opening=bill_reference_name("Purchase Invoice", inv.name),
				due_date=inv.due_date,
			)
		lines = [
			LedgerLine(
				ledger=_party_name("Supplier", inv.supplier, inv.supplier_name),
				amount=money(party_amount),
				is_party=True,
				bill_refs=refs,
			)
		]

		inventory, items_total = [], 0.0
		for row in items.get(inv.name, []):
			amount = -flt(row.base_net_amount, 2)
			inventory.append(
				InventoryLine(
					item=stock_item_name(row.item_code, row.item_name),
					quantity=money(row.qty),
					unit=unit_name(row.uom),
					rate=_stock_rate(row, amount, row.qty),
					amount=money(amount),
					ledger=(
						_strip_company(row.expense_account, company)
						if row.get("expense_account")
						else (settings.purchase_ledger or "Purchase")
					),
					godown=(
						ledger_name(row.warehouse, company) if godowns and row.get("warehouse") else None
					),
					cost_centres=_cost_centres(amount, row.get("cost_center"), company),
				)
			)
			items_total += amount

		taxes_total = 0.0
		for tax in taxes.get(inv.name, []):
			value = flt(tax.base_tax_amount_after_discount_amount) or flt(tax.base_tax_amount)
			if not value:
				continue
			# The account keeps its own name. Renaming anything with "TDS" in
			# it to one settings value split the balance: `TDS Payable` came
			# from the chart of accounts while the vouchers referenced a
			# `TDS-Deductible` that exists nowhere in ERPNext, so Tally carried
			# both. `TCS Receivable` was left alone by the same rule, which is
			# how inconsistent a name match is.
			ledger = _strip_company(tax.account_head, company)
			amount = value if tax.get("add_deduct_tax") == "Deduct" else -value
			lines.append(
				LedgerLine(
					ledger=ledger,
					amount=money(amount),
					cost_centres=_cost_centres(amount, tax.get("cost_center"), company),
				)
			)
			taxes_total += amount

		extra = _rounding_line(party_amount + items_total + taxes_total, rounding)
		if extra:
			lines.append(extra)

		if tick:
			tick()
		out.append(
			Voucher(
				kind=kind,
				number=inv.name,
				date=inv.posting_date,
				remote_id=inv.name,
				reference=cstr(inv.bill_no or ""),
				narration=cstr(inv.remarks or f"Purchase Invoice {inv.name}"),
				party=_party_name("Supplier", inv.supplier, inv.supplier_name),
				party_address=_address(addresses.get(inv.supplier)),
				lines=lines,
				inventory=inventory,
				is_invoice_kind=True,
			)
		)
	return out


def _payments(company, from_date, to_date, payment_type=None, only=None, tick=None):
	settings = _settings()
	filters = _date_filter({"docstatus": 1, "company": company}, "posting_date", from_date, to_date)
	if payment_type:
		filters["payment_type"] = payment_type
	if only:
		filters["name"] = only
	payments = frappe.get_all(
		"Payment Entry",
		filters=filters,
		fields=[
			"name",
			"payment_type",
			"party_type",
			"party",
			"party_name",
			"posting_date",
			"base_paid_amount",
			"base_received_amount",
			"paid_from",
			"paid_to",
			"remarks",
			"reference_no",
			"clearance_date",
		],
		limit_page_length=0,
	)
	deductions = _children_by_parent(
		"Payment Entry Deduction", [p.name for p in payments], ["account", "amount"]
	)
	# 455 of these exist on this bench and none reached Tally, so every payment
	# posted On Account and no invoice was ever shown as settled.
	references = _children_by_parent(
		"Payment Entry Reference",
		[p.name for p in payments],
		["reference_doctype", "reference_name", "allocated_amount"],
	)
	rounding = _round_off_ledger(company)
	out = []

	for pay in payments:
		receive = pay.payment_type == "Receive"
		if receive:
			kind = settings.receipt_voucher_type or "Receipt"
		elif pay.payment_type == "Pay":
			kind = settings.payment_voucher_type or "Payment"
		else:
			kind = settings.get("contra_voucher_type") or "Contra"

		bank_base = flt(pay.base_received_amount if receive else pay.base_paid_amount, 2)
		party_base = flt(pay.base_paid_amount if receive else pay.base_received_amount, 2)
		rows = deductions.get(pay.name, [])
		deducted = sum(flt(d.amount, 2) for d in rows)
		bank_ledger = _strip_company(pay.paid_to if receive else pay.paid_from, company)

		allocations = [
			(bill_reference_name(r.reference_doctype, r.reference_name), r.allocated_amount)
			for r in references.get(pay.name, [])
			if r.reference_doctype in BILLABLE and r.reference_name
		]

		lines = []
		if receive:
			lines.append(
				LedgerLine(ledger=bank_ledger, amount=money(-bank_base), cleared_on=pay.clearance_date)
			)
			lines += [
				LedgerLine(ledger=_strip_company(d.account, company), amount=money(-flt(d.amount, 2)))
				for d in rows
			]
			lines.append(
				LedgerLine(
					ledger=_party_name(pay.party_type, pay.party, pay.party_name),
					amount=money(party_base),
					is_party=bool(pay.party_name),
					bill_refs=_bill_refs(party_base, allocations) if pay.party_name else [],
				)
			)
			residual = -bank_base + party_base - deducted
		else:
			lines.append(
				LedgerLine(
					ledger=_party_name(pay.party_type, pay.party, pay.party_name),
					amount=money(-party_base),
					is_party=bool(pay.party_name),
					bill_refs=_bill_refs(-party_base, allocations) if pay.party_name else [],
				)
			)
			lines += [
				LedgerLine(ledger=_strip_company(d.account, company), amount=money(flt(d.amount, 2)))
				for d in rows
			]
			lines.append(
				LedgerLine(ledger=bank_ledger, amount=money(bank_base), cleared_on=pay.clearance_date)
			)
			residual = -party_base + bank_base + deducted

		extra = _rounding_line(residual, rounding)
		if extra:
			lines.append(extra)

		if tick:
			tick()
		out.append(
			Voucher(
				kind=kind,
				number=pay.name,
				date=pay.posting_date,
				remote_id=pay.name,
				reference=cstr(pay.reference_no or ""),
				narration=cstr(pay.remarks or f"Payment {pay.name}"),
				party=_party_name(pay.party_type, pay.party, pay.party_name) or None,
				lines=[l for l in lines if l.ledger],
			)
		)
	return out


def _journals(company, from_date, to_date, only=None, tick=None):
	settings = _settings()
	filters = _date_filter({"docstatus": 1, "company": company}, "posting_date", from_date, to_date)
	if only:
		filters["name"] = only
	journals = frappe.get_all(
		"Journal Entry",
		filters=filters,
		fields=["name", "posting_date", "user_remark", "voucher_type"],
		limit_page_length=0,
	)
	accounts = _children_by_parent(
		"Journal Entry Account",
		[j.name for j in journals],
		[
			"account",
			"debit",
			"credit",
			"party_type",
			"party",
			"reference_type",
			"reference_name",
			"cost_center",
		],
	)
	parties = _party_names()
	money_ledgers = cash_and_bank(company)
	rounding = _round_off_ledger(company)
	out = []

	for jv in journals:
		lines, postings = [], []
		for row in accounts.get(jv.name, []):
			amount = flt(row.credit, 2) - flt(row.debit, 2)
			party = parties.get((row.party_type, row.party)) if row.party else None
			# A journal line carrying a party posts to the *party ledger*, not to
			# ERPNext's control account. Tally has no "Creditors" ledger - Sundry
			# Creditors is a group with the parties under it - so posting the
			# control account created a lump nobody can age, and left every one
			# of those parties' outstanding untouched.
			ledger = party or _strip_company(row.account, company)
			allocations = []
			if party and row.reference_type in BILLABLE and row.reference_name:
				allocations = [(bill_reference_name(row.reference_type, row.reference_name), abs(amount))]
			lines.append(
				LedgerLine(
					ledger=ledger,
					amount=money(amount),
					is_party=bool(party),
					bill_refs=_bill_refs(amount, allocations) if party else [],
					cost_centres=_cost_centres(amount, row.get("cost_center"), company),
				)
			)
			postings.append((ledger, amount))

		extra = _rounding_line(sum(a for _l, a in postings), rounding)
		if extra:
			lines.append(extra)
			postings.append((extra.ledger, float(extra.amount)))
		if tick:
			tick()
		out.append(
			Voucher(
				kind=voucher_kind_for(postings, money_ledgers, settings, jv.voucher_type),
				number=jv.name,
				date=jv.posting_date,
				remote_id=jv.name,
				narration=cstr(jv.user_remark or f"Journal Entry {jv.name}"),
				lines=lines,
			)
		)
	return out


def _bank_transactions(company, from_date, to_date, only=None, tick=None):
	"""The part of a bank statement line ERPNext has not booked, to Suspense.

	**Only the unallocated part.** A statement line reconciled to a Payment
	Entry or a Journal Entry is already in the books through that entry, and
	both are sent. This used to send every line in full: in one year's books
	every statement line was reconciled, to payments and to journals, so the
	bank moved twice in Tally, once through each payment and once more against
	Suspense. The bank ledger read high by exactly what Suspense held, where ERPNext
	has never posted to Suspense at all. Every other ledger matched, which is
	why only a comparison against ERPNext's GL could see it.
	"""
	settings = _settings()
	filters = _date_filter({"docstatus": 1, "company": company}, "date", from_date, to_date)
	filters["unallocated_amount"] = [">", 0]
	if only:
		filters["name"] = only
	rows = frappe.get_all(
		"Bank Transaction",
		filters=filters,
		fields=[
			"name",
			"date",
			"bank_account",
			"deposit",
			"withdrawal",
			"description",
			"reference_number",
			"unallocated_amount",
		],
		limit_page_length=0,
	)
	banks = (
		{
			r.name: r.account
			for r in frappe.get_all(
				"Bank Account",
				filters={"name": ["in", list({t.bank_account for t in rows if t.bank_account})]},
				fields=["name", "account"],
				limit_page_length=0,
			)
		}
		if rows
		else {}
	)
	suspense = settings.get("suspense_ledger") or "Suspense Account"
	out = []

	for txn in rows:
		deposit = flt(txn.deposit) > 0
		amount = flt(txn.unallocated_amount, 2)
		if amount <= 0:
			continue
		account = banks.get(txn.bank_account)
		bank_ledger = _strip_company(account, company) if account else txn.bank_account
		# A statement line always touches a bank ledger, so it is never a
		# Journal - Tally's Journal will not accept one.
		kind = (
			(settings.receipt_voucher_type or "Receipt")
			if deposit
			else (settings.payment_voucher_type or "Payment")
		)
		lines = (
			[
				LedgerLine(ledger=bank_ledger, amount=money(-amount)),
				LedgerLine(ledger=suspense, amount=money(amount)),
			]
			if deposit
			else [
				LedgerLine(ledger=suspense, amount=money(-amount)),
				LedgerLine(ledger=bank_ledger, amount=money(amount)),
			]
		)
		if tick:
			tick()
		out.append(
			Voucher(
				kind=kind,
				number=txn.name,
				date=txn.date,
				remote_id=txn.name,
				reference=cstr(txn.reference_number or ""),
				narration=cstr(txn.description or f"Bank Transaction {txn.name}"),
				lines=lines,
			)
		)
	return out


# ERPNext documents that post to the ledger and that no reader above reads.
# Found on a full year's books after every voucher was in Tally and every one
# of them matched: the Expense Claims, Stock Entries and Purchase Receipts had
# never crossed, so the year's profit in Tally was overstated by their expense
# and employees showed payments with no claim behind them. The check that read clean compared Tally
# with what was sent, which cannot see what was never sent.
GL_DOCUMENT_TYPES = ("Expense Claim", "Stock Entry", "Purchase Receipt")


def _gl_documents(company, from_date, to_date, only=None, tick=None):
	"""One Journal per document, built from that document's own GL entries.

	From the ledger rather than from the document, because these doctypes post
	through ERPNext's own rules - an expense claim through the employee's payable
	and any advance it consumed, a stock entry through stock and stock received
	but not billed - and the GL is where those rules have already been applied.
	The amounts are therefore ERPNext's own, to the paisa.

	A party line posts to the party's ledger On Account, the same way the
	payments that settled these claims were sent: only sales and purchase
	invoices are bill-wise here, so naming a bill on one side and not the other
	would leave an open reference that never clears.
	"""
	settings = _settings()
	filters = _date_filter(
		{"docstatus": 1, "is_cancelled": 0, "company": company}, "posting_date", from_date, to_date
	)
	filters["voucher_type"] = ["in", GL_DOCUMENT_TYPES]
	if only:
		filters["voucher_no"] = only
	rows = frappe.get_all(
		"GL Entry",
		filters=filters,
		fields=[
			"voucher_type",
			"voucher_no",
			"posting_date",
			"account",
			"party_type",
			"party",
			"debit",
			"credit",
			"cost_center",
			"remarks",
		],
		order_by="posting_date asc, voucher_no asc, name asc",
		limit_page_length=0,
	)
	money_ledgers = cash_and_bank(company)
	rounding = _round_off_ledger(company)

	documents = {}
	for row in rows:
		documents.setdefault((row.voucher_type, row.voucher_no), []).append(row)

	out = []
	for (doctype, name), entries in documents.items():
		merged = {}
		for row in entries:
			party = _party_name(row.party_type, row.party, row.party) if row.party else None
			ledger = party or _strip_company(row.account, company)
			key = (ledger, bool(party), row.cost_center or "")
			merged[key] = merged.get(key, 0.0) + flt(row.credit, 2) - flt(row.debit, 2)

		lines, postings = [], []
		for (ledger, is_party, centre), amount in merged.items():
			amount = flt(amount, 2)
			if not amount:
				continue
			lines.append(
				LedgerLine(
					ledger=ledger,
					amount=money(amount),
					is_party=is_party,
					bill_refs=_bill_refs(amount, []) if is_party else [],
					cost_centres=_cost_centres(amount, centre, company),
				)
			)
			postings.append((ledger, amount))
		if not lines:
			continue
		extra = _rounding_line(sum(a for _l, a in postings), rounding)
		if extra:
			lines.append(extra)
			postings.append((extra.ledger, float(extra.amount)))

		remark = cstr(entries[0].remarks or "").strip()
		narration = f"{doctype} {name}"
		if remark and remark.lower() not in ("no remarks", name.lower()):
			narration = f"{narration} - {remark}"
		if tick:
			tick()
		out.append(
			Voucher(
				kind=voucher_kind_for(postings, money_ledgers, settings, None),
				number=name,
				date=entries[0].posting_date,
				remote_id=name,
				narration=narration,
				lines=lines,
				source_doctype=doctype,
			)
		)
	return out


VOUCHER_SOURCES = (
	("sales_invoices", "include_sales_invoice", _sales),
	("purchase_invoices", "include_purchase_invoice", _purchase),
	("payment_entries", "include_payment_entry", _payments),
	("journal_entries", "include_journal_entry", _journals),
	("bank_transactions", "include_bank_transaction", _bank_transactions),
	# No settings field yet, so `settings.get(..., 1)` answers on. Adding one
	# later must go through `backfill_new_settings_defaults`, or upgrading
	# sites lose these documents silently.
	("other_documents", "include_other_documents", _gl_documents),
)

# What a reporting caller calls each source while it runs, and which ERPNext
# DocType and date column to count for its denominator. Kept beside the sources
# so a sixth cannot be added with no way to report it.
SOURCE_STAGES = {
	"sales_invoices": ("Processing Sales", "Sales Invoice", "posting_date"),
	"purchase_invoices": ("Processing Purchases", "Purchase Invoice", "posting_date"),
	"payment_entries": ("Processing Payments", "Payment Entry", "posting_date"),
	"journal_entries": ("Processing Journals", "Journal Entry", "posting_date"),
	"bank_transactions": ("Processing Bank Lines", "Bank Transaction", "date"),
	"other_documents": ("Processing Other Documents", "GL Entry", "posting_date"),
}


def count_vouchers(company, from_date=None, to_date=None, settings=None):
	"""How many vouchers this range holds, per source and in total.

	Counted rather than guessed, because it is the denominator of a progress bar
	somebody will believe. It obeys the same `include_*` settings the export
	does - a denominator that counts work the export will skip makes a bar that
	stops at eighty per cent and stays there.
	"""
	settings = settings or _settings()
	per_source, total = {}, 0
	for key, flag, _fn in VOUCHER_SOURCES:
		if not settings.get(flag, 1):
			per_source[key] = 0
			continue
		_stage, doctype, date_field = SOURCE_STAGES[key]
		filters = _date_filter({"docstatus": 1, "company": company}, date_field, from_date, to_date)
		try:
			if key == "other_documents":
				# Documents, not GL rows: one voucher each.
				filters.update({"is_cancelled": 0, "voucher_type": ["in", GL_DOCUMENT_TYPES]})
				n = len(
					frappe.get_all(
						doctype, filters=filters, fields=["voucher_no"], distinct=True, limit_page_length=0
					)
				)
			else:
				n = frappe.db.count(doctype, filters)
		except Exception:
			n = 0
		per_source[key] = n
		total += n
	return per_source, total


# The dashboard's per-area buttons, keyed the way the buttons are labelled.
# They name **ERPNext** areas rather than Tally kinds, which is the distinction
# that matters: "Sales Invoices" means every voucher the sales reader produced,
# credit notes among them, because that is what somebody pressing it is asking
# for. Keying it on the Tally kind instead would silently drop the credit notes
# into a button nobody would think to press.
VOUCHER_SECTIONS = {
	"sales_invoices": (_sales, {}),
	"purchase_invoices": (_purchase, {}),
	"payment_in": (_payments, {"payment_type": "Receive"}),
	"payment_out": (_payments, {"payment_type": "Pay"}),
	# Not a button; the name the single-document export and the run counts use.
	"payment_entries": (_payments, {}),
	"journal_entries": (_journals, {}),
	"bank_transactions": (_bank_transactions, {}),
	"other_documents": (_gl_documents, {}),
}


def sending_inventory(settings=None) -> bool:
	"""Whether a voucher goes out carrying its items.

	`None` reads as on, because a missing field must not quietly change what a
	customer's books receive. That guard is not what protects an upgrading
	site, though, and saying so here is the point: `Tally Settings` is a
	Single, and a Check with no row in `tabSingles` was **measured** reading 0
	rather than None, which is folding switched on by accident. What protects
	it is `patches.v1_0.backfill_new_settings_defaults`, which gives the field
	its declared 1 on a site that already has the Single.
	"""
	settings = settings if settings is not None else _settings()
	value = settings.get("send_voucher_inventory")
	return True if value is None else bool(value)


def _as_configured(vouchers, settings=None):
	"""The vouchers as the destination company is set up to take them.

	The one place the accounting-only choice is applied, so the file somebody
	downloads, the batches somebody pushes and the export validate() reads are
	the same vouchers. Folding at each push site instead is how one door comes
	to disagree with another about what was sent.

	Folding also takes the voucher out of invoice mode, because `is_invoice` is
	derived from the inventory being non-empty. That is the point rather than a
	side effect: an accounting voucher is sent with ALLLEDGERENTRIES.LIST and
	is never asked whether its ledgers affect inventory, which is the pair of
	conditions a live TallyPrime refused roughly 43% of invoices over.
	"""
	if sending_inventory(settings):
		return list(vouchers)
	return [without_stock(voucher) for voucher in vouchers]


def extract_vouchers(section, company, from_date=None, to_date=None, only=None) -> list[Voucher]:
	"""One area's vouchers, for the per-area buttons.

	Deliberately does **not** consult the `include_*` settings the full export
	obeys. Those answer "what goes in the nightly export"; a person pressing
	Sales Invoices has just answered that question by pressing it, and a button
	that silently exported nothing because of a setting on another screen is
	worse than one that exports what it says.
	"""
	fn, extra = VOUCHER_SECTIONS[section]
	return _as_configured(fn(company, from_date, to_date, only=only, **extra))


def extract(company, from_date=None, to_date=None, reporter=None) -> Export:
	"""The whole export as a model, with closure guaranteed.

	Vouchers first, then masters, then anything the vouchers referenced that no
	master creates. That ordering is what makes closure possible rather than
	hoped for: a ledger a voucher names and nothing creates is a state this
	cannot return.

	`reporter` is optional and is the only thing in this module that knows a
	background job exists. It is called with a stage as each source begins and
	ticked once per voucher actually built, so what a progress bar shows is a
	count this loop reached rather than a fraction of elapsed time.
	"""
	settings = _settings()
	# `Export.company` is the company **as Tally knows it**, which is what goes
	# into SVCURRENTCOMPANY and must match Tally exactly. Resolving it belongs
	# here rather than in the renderer: the renderer is pure and cannot read a
	# setting, and that is the point of the split.
	export = Export(company=cstr(settings.company_name_in_tally or "").strip() or company)

	for key, flag, fn in VOUCHER_SOURCES:
		if not settings.get(flag, 1):
			continue
		if reporter:
			reporter.at(SOURCE_STAGES[key][0])
		export.vouchers.extend(fn(company, from_date, to_date, tick=(reporter.tick if reporter else None)))

	# Before the closure walk and before the stock marking, both of which read
	# the vouchers: folded, a stock line's ledger arrives as an ordinary ledger
	# line and is still created, and no ledger is marked as affecting
	# inventory, because no voucher carries any.
	carried = sum(len(voucher.inventory) for voucher in export.vouchers)
	export.vouchers = _as_configured(export.vouchers, settings)
	if not sending_inventory(settings):
		export.accounting_only = True
		# Measured across the fold rather than taken from the count before it,
		# so a stock line with no ledger - left whole on purpose, because
		# dropping it would take its money - is not counted as folded.
		export.stock_lines_folded = carried - sum(len(voucher.inventory) for voucher in export.vouchers)

	# Which ledgers carry a cost centre is known only once the vouchers exist,
	# which is why the masters are built second.
	cost_centre_ledgers = {
		line.ledger for voucher in export.vouchers for line in voucher.lines if line.cost_centres
	}
	if reporter:
		reporter.at("Generating Masters")
	export.masters = extract_masters(company, from_date, to_date, cost_centre_ledgers)

	# Folded, no voucher names a stock item, a stock group or a unit, so
	# creating them would put records in the customer's Tally that nothing
	# uses - over a hundred items on one year's books. It sits
	# here beside the fold because it is the same decision, and the readers
	# keep one shape. `validate`'s closure check still proves nothing
	# references what is not created, which is what makes this safe rather
	# than merely tidy.
	if not sending_inventory(settings):
		export.masters.stock_items = []
		export.masters.stock_groups = []
		export.masters.units = []

	created = export.masters.ledger_names()
	referenced = {}
	for voucher in export.vouchers:
		for line in voucher.lines:
			referenced.setdefault(line.ledger, _group_for(line, voucher))
		for inv in voucher.inventory:
			referenced.setdefault(inv.ledger, None)
		if voucher.party:
			referenced.setdefault(voucher.party, GROUP_CREDITORS)

	for name, group in sorted(referenced.items()):
		if name and name not in created:
			export.masters.ledgers.append(
				Ledger(name=name, parent=group or GROUP_SUSPENSE, cost_centres_on=name in cost_centre_ledgers)
			)

	_mark_ledgers_that_carry_stock(export)
	return export


def _mark_ledgers_that_carry_stock(export: Export):
	"""A ledger an invoice allocates stock to must affect inventory.

	Tally refuses an invoice whose ACCOUNTINGALLOCATIONS names a ledger with
	*Inventory values are affected* set to No. It refuses it without saying so:
	the answer is `0 errors and 1 exceptions`, naming nothing, and the rest of
	the file is lost behind it. In accounting mode Tally never makes the check
	- it drops the stock lines instead and reports success - so this could only
	ever show up on invoices, and it showed up on all of them.

	`Ledger.affects_stock` has existed since the model was written, the
	renderer has always read it, and **nothing ever set it**, so every ledger
	went out as No. Proved on a live TallyPrime by altering that one flag on
	one ledger and re-sending the same voucher: refused, then created.

	Which ledgers these are is knowable only once the vouchers exist, which is
	why this runs here rather than in `extract_masters` - the same reason the
	cost-centre flag is worked out at this point.
	"""
	carrying = {inv.ledger for voucher in export.vouchers for inv in voucher.inventory if inv.ledger}
	for ledger in export.masters.ledgers:
		if ledger.name in carrying:
			ledger.affects_stock = True


def _stock_rate(row, amount, quantity):
	"""The unit rate to send, which must agree with the amount.

	Tally stores RATE and AMOUNT exactly as given and reconciles neither, so a
	rate that disagrees with the amount is an item priced wrongly in the
	customer's books for ever - the value right, the rate about 18% high, and
	nothing anywhere saying so. Read back off the live company after a real
	push: `rate=1960.00/Box, qty=2.00 Box, amount=3322.03`.

	The cause was sending `rate`, which on a tax-inclusive invoice is the
	**gross** rate, beside `base_net_amount`, which is net. Measured on the
	real data: 103,588 of 103,737 sales lines had `rate * qty != amount`, and
	405 of 19,823 purchase lines. `base_net_rate` is the field that pairs with
	`base_net_amount`, and it is what this sends.

	**A two-decimal rate cannot always reproduce the amount, and that is
	arithmetic rather than a defect.** Seven boxes at 5,089.83 is 727.118571
	each; no rate rounded to paise multiplies back exactly, so those lines are
	out by a paisa. The amount is what Tally posts and it is sent explicitly,
	so the accounting is unaffected; the rate is the item's price and is as
	close as two decimals allow. Deriving it from the amount instead gives the
	same rounded figure, so nothing is gained by preferring one over the other
	- ERPNext's own field wins, because it is the number somebody typed.

	A line with no quantity has nothing to check against and keeps the stated
	net rate. Magnitudes throughout: a purchase amount is negative because the
	sign is how this model says debit, and a price is never negative.
	"""
	rate = abs(flt(row.get("base_net_rate") or 0))
	if rate:
		return money(rate)
	qty = abs(flt(quantity or 0))
	if qty:
		return money(abs(flt(amount)) / qty)
	return money(abs(flt(row.rate or 0)))


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
from tally_connector.tally.extract_rules import UNIT_SYMBOL_MAX

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
