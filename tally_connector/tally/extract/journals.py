"""
Journal entries, and the documents read straight from the general ledger.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr, flt

from tally_connector.tally.extract.invoices import BILLABLE, _bill_refs, _cost_centres, _rounding_line
from tally_connector.tally.extract.naming import (
	_party_name,
	_party_names,
	_round_off_ledger,
	bill_reference_name,
	cash_and_bank,
	voucher_kind_for,
)
from tally_connector.tally.extract.source import _children_by_parent, _date_filter, _settings, _strip_company
from tally_connector.tally.model import (
	LedgerLine,
	Voucher,
	money,
)


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
