"""Sending a voucher as accounting only, with the stock lines folded in.

The migration exists so an audit team can file an income-tax return from
Tally. A return is built from the profit and loss and the balance sheet -
revenue, tax, party balances - and none of that needs 9,483 item movements to
cross. Closing stock is one figure.

What this does is move each stock line's value onto the ledger that line was
already posting to, and drop the stock. **No money moves**: the item's amount
was always allocated to an income or expense ledger through
`ACCOUNTINGALLOCATIONS`, and that is the same ledger it lands on here. The
party, the taxes and the totals are untouched, and the voucher balances after
exactly as it did before.

What is lost is the quantity and the item name, and that is a real loss rather
than a technicality - it is said out loud here, reported per voucher by the
caller, and it is why this is off unless somebody asks for it.

Two things make it the right shape for this engagement rather than a
concession. Tally refuses roughly 43% of invoice-mode vouchers on this company
for a reason that has not been found, and an accounting voucher has imported
reliably all along. And the customer's own Tally company for the previous year
contains 4,682 vouchers with **no stock line on any of them** - an empty
inventory list and the revenue on a ledger - so this is the shape their books
were already kept in.

Pure: no frappe, no network.
"""

from __future__ import annotations

import copy

from tally_connector.tally.model import (
	CostCentreAllocation,
	LedgerLine,
	money,
)


def without_stock(voucher):
	"""The same voucher as accounting entries only.

	Returns a copy. A voucher carrying no stock is returned unchanged, so this
	is safe to run over everything.
	"""
	if not voucher.inventory:
		return voucher

	# A stock line with no ledger has nowhere to be folded to, and dropping it
	# would take its money with it - the voucher would balance afterwards, so
	# nothing anywhere would say a rupee had gone. Such a voucher is left
	# exactly as it is and fails loudly instead. Found by a test that asserted
	# the loss would be loud and discovered it was silent.
	if any(not line.ledger for line in voucher.inventory):
		return voucher

	out = copy.deepcopy(voucher)
	folded: dict[str, object] = {}
	centres: dict[str, dict[str, object]] = {}
	for line in out.inventory:
		folded[line.ledger] = money(folded.get(line.ledger, 0) + line.amount)
		_gather(centres.setdefault(line.ledger, {}), line.cost_centres)
	out.inventory = []

	# Onto the ledger the stock line already posted to, so the accounting is
	# the same postings it always was. An existing line on that ledger is
	# added to rather than duplicated, because two lines on one ledger in one
	# voucher is a reconciliation somebody has to do by hand later.
	#
	# The cost-centre allocations move with the amount, and that is not a
	# nicety. Tally requires a line's allocations to total the line, and
	# merging an amount into a line while leaving its allocations alone breaks
	# exactly that: eleven purchase invoices in one year's books came out with
	# an expense line allocated a fraction of its own amount, which Tally
	# takes and reports wrongly. The item row's cost centre was
	# being read from ERPNext and thrown away.
	for ledger, amount in folded.items():
		if amount == 0:
			continue
		existing = next((l for l in out.lines if l.ledger == ledger and not l.is_party), None)
		if existing is None:
			out.lines.append(
				LedgerLine(ledger=ledger, amount=amount, cost_centres=_allocations(centres.get(ledger, {})))
			)
		else:
			from dataclasses import replace

			at = out.lines.index(existing)
			merged = {}
			_gather(merged, existing.cost_centres)
			_gather(merged, _allocations(centres.get(ledger, {})))
			out.lines[at] = replace(
				existing, amount=money(existing.amount + amount), cost_centres=_allocations(merged)
			)
	return out


def _gather(into: dict, allocations):
	"""Add allocations into a running total per cost centre."""
	for allocation in allocations:
		into[allocation.name] = money(into.get(allocation.name, 0) + allocation.amount)


def _allocations(totals: dict):
	"""Back to allocation rows, dropping any that cancelled to nothing.

	Where one side of a merge carries a cost centre and the other does not,
	the result does not total the line and `validate`'s cost-centre check says
	so. That is the right outcome rather than something to paper over: the
	unallocated part is unallocated in ERPNext too, and inventing a centre for
	it would be inventing an analysis nobody made.
	"""
	return [CostCentreAllocation(name=name, amount=amount) for name, amount in totals.items() if amount != 0]


def what_is_lost(voucher) -> dict:
	"""What sending this voucher as accounting only does not carry.

	So the caller can report it per voucher rather than leaving somebody to
	notice that Tally's stock summary is empty.
	"""
	return {
		"number": getattr(voucher, "number", ""),
		"document": getattr(voucher, "remote_id", "") or getattr(voucher, "number", ""),
		"stock_lines": len(voucher.inventory),
		"items": sorted({str(i.item) for i in voucher.inventory}),
	}
