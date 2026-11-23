"""Reconciling ERPNext's total with the one Tally computes for itself.

Tally works an invoice's value out from the stock lines and its own GST
arithmetic, and refuses the voucher when the party line does not match what it
arrived at. ERPNext rounds each tax component; Tally does not round the same
way, and on a small invoice the two differ by a paisa: 177.96 of goods at 18%
is 32.0328 of tax, which ERPNext books as 16.02 + 16.02 = 32.04 and Tally will
not accept against a party line of 210.00.

**Tally never says what figure it wanted.** Its answer is `1 errors` and
nothing else, so the difference cannot be read off a reply, and predicting it
does not survive contact with the data - the formula that fits a refused
invoice contradicts one that was accepted. What is left is to try: shift the
party line by a paisa or two, book the difference to the rounding ledger, and
keep whichever Tally takes.

That is a deliberate and bounded thing, not a nudge until something sticks:

- the shift is never more than `MAX_PAISE`, so a voucher that is wrong for any
  other reason is refused rather than bent until it fits;
- the money is conserved. The stock lines, the tax lines and the revenue are
  untouched, and what moves off the party goes to the rounding ledger where an
  accountant can see it;
- every adjusted voucher is reported by name, because a silent adjustment to a
  customer's balance is exactly what nobody would find later.

The cost, accepted on purpose, is each customer's outstanding differing from
ERPNext by up to a paisa per invoice.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from decimal import Decimal

from tally_connector.tally.model import LedgerLine, money

# One paisa, then two, each way. Beyond that the voucher is wrong about
# something else and pretending otherwise would put the difference somewhere an
# accountant has to chase.
MAX_PAISE = 2
DELTAS = tuple(Decimal(p) / 100 for p in (1, -1, 2, -2))

ROUNDING_LEDGER = "Rounded Off"


def attempts(voucher, ledger=ROUNDING_LEDGER):
    """The voucher, then each rounded variant Tally might accept instead.

    The original comes first, so a voucher Tally would have taken anyway is
    never adjusted. Yields (voucher, paise) with `paise` zero for the original.
    """
    yield voucher, Decimal("0")
    if not _party_line(voucher):
        return
    for delta in DELTAS:
        variant = _shifted(voucher, delta, ledger)
        if variant is not None:
            yield variant, delta


def _party_line(voucher):
    for line in voucher.lines:
        if line.is_party:
            return line
    return None


def _shifted(voucher, delta, ledger):
    """A copy whose party line moves by `delta`, with the rounding ledger
    taking the other side so the voucher still balances.

    The lines are frozen dataclasses, which is why this rebuilds them through
    `replace` rather than assigning - and that is the right constraint: a
    variant must never be able to reach back and alter the voucher it came
    from, because the original is what gets sent when Tally accepts it.
    """
    out = copy.deepcopy(voucher)
    party = _party_line(out)
    if party is None:
        return None

    # The party sits on the debit side of a sale as a negative amount, so
    # moving it towards zero is what Tally's smaller total asks for. The sign
    # is read rather than assumed: a credit note's party is positive.
    before = party.amount
    after = money(before + (delta if before >= 0 else -delta))
    moved = money(before - after)

    refs = list(party.bill_refs)
    if refs:
        # A bill reference that disagrees with its line sends the amount to On
        # Account and the ageing stops meaning anything.
        last = refs[-1]
        refs[-1] = replace(last, amount=money(last.amount + (after - before)))

    index = out.lines.index(party)
    out.lines[index] = replace(party, amount=after, bill_refs=refs)

    rounding = next((l for l in out.lines
                     if l.ledger == ledger and not l.is_party), None)
    if rounding is None:
        out.lines.append(LedgerLine(ledger=ledger, amount=moved))
    else:
        total = money(rounding.amount + moved)
        at = out.lines.index(rounding)
        if total == 0:
            out.lines.pop(at)
        else:
            out.lines[at] = replace(rounding, amount=total)
    return out
