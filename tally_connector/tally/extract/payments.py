"""
Payments and receipts, and the bank statement lines no entry has booked.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr, flt

from tally_connector.tally.extract.invoices import BILLABLE, _bill_refs, _rounding_line
from tally_connector.tally.extract.naming import _party_name, _round_off_ledger, bill_reference_name
from tally_connector.tally.extract.source import _children_by_parent, _date_filter, _settings, _strip_company
from tally_connector.tally.model import (
	LedgerLine,
	Voucher,
	money,
)


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
