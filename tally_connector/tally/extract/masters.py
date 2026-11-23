"""
The masters file: groups, ledgers, parties, stock and units, with opening
balances.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr, getdate

from tally_connector.tally.extract.naming import (
	_lands_in_money,
	_party_name,
	cash_and_bank,
	cost_centre_name,
	gst_accounts,
	gst_ledgers,
	stock_item_name,
	unit_name,
	units_in_use,
)
from tally_connector.tally.extract.source import (
	_address,
	_addresses_for,
	_parents_first,
	_plain_text,
	_settings,
)
from tally_connector.tally.model import (
	PRIMARY_COST_CATEGORY,
	Address,
	CostCentre,
	Godown,
	Group,
	Ledger,
	Masters,
	StockGroup,
	StockItem,
	Unit,
	money,
)
from tally_connector.utils.ledgers import (
	GROUP_CREDITORS,
	GROUP_DEBTORS,
	GROUP_DUTIES,
	TALLY_PRIMARY,
	group_name,
	is_reserved_group,
	ledger_name,
	party_ledger_name,
)


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
