"""Folding stock into the ledger it already posted to.

The migration exists so an audit team can file an income-tax return from
Tally, and a return is built from the profit and loss and the balance sheet.
Sending accounting entries only is therefore a supported choice.

What these hold is that nothing accounting-relevant changes: the same money
reaches the same ledgers, the voucher still balances, and what *is* lost - the
quantity and the item - is reported rather than dropped quietly.

Pure: no site, no Tally.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from tally_connector.tally import model
from tally_connector.tally.accounting import what_is_lost, without_stock
from frappe.tests import UnitTestCase


def _sale():
    v = model.Voucher(kind="Sales", date=datetime.date(2026, 3, 31),
                      number="STG-1", party="Acme")
    v.lines.append(model.LedgerLine(ledger="Acme", amount=Decimal("-480.00"),
                                    is_party=True))
    v.lines.append(model.LedgerLine(ledger="Output Tax SGST", amount=Decimal("36.61")))
    v.lines.append(model.LedgerLine(ledger="Output Tax CGST", amount=Decimal("36.61")))
    v.inventory.append(model.InventoryLine(
        item="Widget", ledger="Sales", quantity=Decimal("15"),
        rate=Decimal("27.12"), amount=Decimal("406.78"), unit="Nos"))
    return v


class TestTheAccountingIsUnchanged(UnitTestCase):

    def test_it_still_balances(self):
        self.assertEqual(without_stock(_sale()).imbalance(), Decimal("0"))

    def test_the_money_lands_on_the_ledger_the_stock_posted_to(self):
        out = without_stock(_sale())
        sales = next(l for l in out.lines if l.ledger == "Sales")
        self.assertEqual(sales.amount, Decimal("406.78"))

    def test_the_party_and_the_taxes_are_untouched(self):
        out = without_stock(_sale())
        self.assertEqual(
            [(l.ledger, l.amount) for l in out.lines if l.is_party or "Tax" in l.ledger],
            [("Acme", Decimal("-480.00")),
             ("Output Tax SGST", Decimal("36.61")),
             ("Output Tax CGST", Decimal("36.61"))])

    def test_the_voucher_stops_being_an_invoice(self):
        """Which is the point: an accounting voucher is what Tally takes."""
        self.assertTrue(_sale().is_invoice)
        self.assertFalse(without_stock(_sale()).is_invoice)

    def test_several_lines_on_one_ledger_become_one(self):
        v = _sale()
        v.inventory.append(model.InventoryLine(
            item="OTHER", ledger="Sales", quantity=Decimal("1"),
            rate=Decimal("10"), amount=Decimal("10.00"), unit="Nos"))
        v.lines[0] = model.LedgerLine(ledger="Acme", amount=Decimal("-490.00"),
                                      is_party=True)
        out = without_stock(v)
        sales = [l for l in out.lines if l.ledger == "Sales"]
        self.assertEqual(len(sales), 1, "two lines on one ledger to reconcile by hand")
        self.assertEqual(sales[0].amount, Decimal("416.78"))
        self.assertEqual(out.imbalance(), Decimal("0"))

    def test_an_existing_line_on_that_ledger_is_added_to(self):
        v = _sale()
        v.lines.append(model.LedgerLine(ledger="Sales", amount=Decimal("100.00")))
        v.lines[0] = model.LedgerLine(ledger="Acme", amount=Decimal("-580.00"),
                                      is_party=True)
        out = without_stock(v)
        sales = [l for l in out.lines if l.ledger == "Sales"]
        self.assertEqual(len(sales), 1)
        self.assertEqual(sales[0].amount, Decimal("506.78"))


class TestWhatIsLostIsSaid(UnitTestCase):
    """A quantity that stops crossing is a real loss, not a technicality."""

    def test_it_names_the_items_and_counts_the_lines(self):
        lost = what_is_lost(_sale())
        self.assertEqual(lost["stock_lines"], 1)
        self.assertEqual(lost["items"], ["Widget"])
        self.assertEqual(lost["number"], "STG-1")


class TestItIsSafeOverEverything(UnitTestCase):

    def test_a_voucher_with_no_stock_is_returned_unchanged(self):
        v = model.Voucher(kind="Payment", date=datetime.date(2026, 3, 31),
                          number="PAY-1")
        v.lines.append(model.LedgerLine(ledger="Cash", amount=Decimal("-1")))
        v.lines.append(model.LedgerLine(ledger="Acme", amount=Decimal("1")))
        self.assertIs(without_stock(v), v)

    def test_the_original_is_never_mutated(self):
        v = _sale()
        without_stock(v)
        self.assertEqual(len(v.inventory), 1)
        self.assertTrue(v.is_invoice)

    def test_a_stock_line_with_no_ledger_is_never_folded(self):
        """It has nowhere to be folded to, and dropping it takes its money -
        the voucher balances afterwards, so nothing anywhere would say a rupee
        had gone. This test asserted the loss would be loud and found it was
        silent, which is how the guard came to exist."""
        v = _sale()
        v.inventory.append(model.InventoryLine(
            item="ORPHAN", ledger="", quantity=Decimal("1"),
            rate=Decimal("5"), amount=Decimal("5.00"), unit="Nos"))
        out = without_stock(v)
        self.assertIs(out, v, "it folded a line it could not account for")
        self.assertEqual(len(out.inventory), 2)

class TestTheCostCentresMoveWithTheMoney(UnitTestCase):
    """Tally requires a line's allocations to total the line.

    This is the shape purchase invoices can have: a charge row and
    an item row on the same expense ledger, each with its own cost centre. The
    fold merged the amounts and left the charge row's allocations alone, so the
    line read one figure with a fraction of it allocated. Tally takes that and reports it
    wrongly, which is the one severity this connector calls `wrong`.
    """

    def _air_travel(self):
        v = model.Voucher(kind="Purchase", date=datetime.date(2026, 3, 31),
                          number="PINV-0001", party="A Travel Agent",
                          is_invoice_kind=True)
        v.lines.append(model.LedgerLine(ledger="A Travel Agent",
                                        amount=Decimal("10000.00"), is_party=True))
        # the charge row, with its own allocation
        v.lines.append(model.LedgerLine(
            ledger="Air Travel Expenses", amount=Decimal("-1500.00"),
            cost_centres=[model.CostCentreAllocation(name="Main",
                                                     amount=Decimal("-1500.00"))]))
        # the item row, on the same ledger, with the cost centre ERPNext holds
        v.inventory.append(model.InventoryLine(
            item="Air Ticket", ledger="Air Travel Expenses",
            quantity=Decimal("1"), rate=Decimal("8500.00"),
            amount=Decimal("-8500.00"), unit="Nos",
            cost_centres=[model.CostCentreAllocation(name="Main",
                                                     amount=Decimal("-8500.00"))]))
        return v

    def _line(self, voucher, ledger="Air Travel Expenses"):
        return next(l for l in voucher.lines if l.ledger == ledger)

    def test_the_allocations_total_the_merged_line(self):
        line = self._line(without_stock(self._air_travel()))
        self.assertEqual(line.amount, Decimal("-10000.00"))
        self.assertEqual(sum(c.amount for c in line.cost_centres), line.amount)

    def test_one_centre_on_both_sides_stays_one_allocation(self):
        """Two rows naming the same centre is one allocation, not two."""
        line = self._line(without_stock(self._air_travel()))
        self.assertEqual([c.name for c in line.cost_centres], ["Main"])

    def test_two_centres_are_both_carried(self):
        v = self._air_travel()
        v.inventory[0].cost_centres = [
            model.CostCentreAllocation(name="Delivery", amount=Decimal("-8500.00"))]
        line = self._line(without_stock(v))
        self.assertEqual(sorted(c.name for c in line.cost_centres),
                         ["Delivery", "Main"])
        self.assertEqual(sum(c.amount for c in line.cost_centres), line.amount)

    def test_a_new_line_carries_the_items_own_centre(self):
        """Nothing to merge into: the folded line still names its centre."""
        v = self._air_travel()
        v.lines = [l for l in v.lines if l.ledger != "Air Travel Expenses"]
        v.lines[0].amount = Decimal("8500.00")
        line = self._line(without_stock(v))
        self.assertEqual(sum(c.amount for c in line.cost_centres), line.amount)

    def test_an_item_row_with_no_centre_leaves_the_charges_alone(self):
        """ERPNext genuinely has no centre for it, so the shortfall is real and
        validate is right to say so. What must not happen is inventing one."""
        v = self._air_travel()
        v.inventory[0].cost_centres = []
        line = self._line(without_stock(v))
        self.assertEqual(sum(c.amount for c in line.cost_centres),
                         Decimal("-1500.00"))
        self.assertEqual(line.amount, Decimal("-10000.00"))

