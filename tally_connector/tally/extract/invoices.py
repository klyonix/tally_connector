"""
Sales and purchase invoices, with their bill references, rounding and rates.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr, flt

from tally_connector.tally.extract.naming import (
	_party_name,
	_round_off_ledger,
	bill_reference_name,
	cost_centre_name,
	stock_item_name,
	unit_name,
)
from tally_connector.tally.extract.source import (
	_address,
	_addresses_for,
	_children_by_parent,
	_date_filter,
	_settings,
	_strip_company,
)
from tally_connector.tally.model import (
	AGAINST_REF,
	NEW_REF,
	ON_ACCOUNT,
	BillRef,
	CostCentreAllocation,
	Export,
	InventoryLine,
	LedgerLine,
	Voucher,
	money,
)
from tally_connector.utils.ledgers import (
	ledger_name,
)

ROUNDING_TOLERANCE = 1.0


# Which ERPNext documents become a bill Tally can be settled against. A payment
# may reference an Expense Claim or an Employee Advance; those are not vouchers
# this connector exports, so there is no bill in Tally to allocate to and the
# amount goes On Account rather than against a reference that does not exist.
BILLABLE = ("Sales Invoice", "Purchase Invoice")


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
