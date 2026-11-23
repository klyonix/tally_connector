"""
What this export writes, read back by this app's own importer.

The two directions were built months apart against the same system, and until
both existed in one repository nothing compared them. Doing so is what found
the three gaps these tests cover: the export never sent an opening balance, so
a Tally company built from it started every ledger at nought; it sent no GUID,
so Tally had nothing to recognise a re-import by; and it sent no GST rate, so
an item arrived carrying an HSN code, the word `Taxable` and no rate at all.

Reading our own output with our own reader is not a proof that Tally accepts
it - only a real import is that. It is a proof that the two halves of this app
agree about what the file means, which is the part that was never checked.

Pure: no site, no database.
"""

from __future__ import annotations

import datetime
import unittest
from decimal import Decimal

from tally_connector.tally import model
from tally_connector.tally.books import iter_vouchers, read_groups_and_ledgers
from tally_connector.tally.render import render_masters, render_vouchers


def _masters(**ledger_fields):
    masters = model.Masters(applicable_from=datetime.date(2017, 4, 1))
    masters.groups.append(model.Group(name="Sundry Debtors",
                                      parent="Current Assets"))
    masters.ledgers.append(model.Ledger(name="Acme Ltd", parent="Sundry Debtors",
                                        **ledger_fields))
    return masters


def _read_ledger(masters, name="Acme Ltd"):
    xml = render_masters("Demo Traders", masters).encode("utf-8")
    _groups, ledgers, _removed = read_groups_and_ledgers(xml)
    return ledgers.get(name)


class TestOpeningBalanceSurvivesTheRoundTrip(unittest.TestCase):
    """Tally's sign, both ways: negative is a debit."""

    def test_a_debit_balance_reads_back_as_the_same_debit(self):
        read = _read_ledger(_masters(opening_balance=Decimal("-12345.67")))
        self.assertEqual(read.opening, Decimal("-12345.67"))

    def test_a_credit_balance_reads_back_as_the_same_credit(self):
        read = _read_ledger(_masters(opening_balance=Decimal("8900.00")))
        self.assertEqual(read.opening, Decimal("8900.00"))

    def test_a_ledger_with_no_opening_sends_none(self):
        """Nought and nothing say the same thing, and the shorter file imports."""
        xml = render_masters("Demo Traders", _masters())
        self.assertNotIn("<OPENINGBALANCE>", xml)

    def test_a_zero_opening_is_not_sent_either(self):
        xml = render_masters("Demo Traders", _masters(opening_balance=Decimal("0")))
        self.assertNotIn("<OPENINGBALANCE>", xml)


class TestGstRateReachesTheItem(unittest.TestCase):
    """`GSTDETAILS.LIST > STATEWISEDETAILS.LIST > RATEDETAILS.LIST`.

    Without it the import succeeds and the voucher lands in Tally's Uncertain
    Transactions, out of GSTR-1 until somebody fixes it by hand - which is the
    worst shape of wrong, because nothing says so until filing.
    """

    def _item_xml(self, **fields):
        masters = model.Masters(applicable_from=datetime.date(2017, 4, 1))
        masters.stock_items.append(model.StockItem(
            name="Widget", base_unit="Nos", hsn_code="8471", **fields))
        return render_masters("Demo Traders", masters)

    def test_the_integrated_rate_is_whole_and_the_two_halves_are_halves(self):
        xml = self._item_xml(gst_rate=Decimal("18"))
        for head, rate in (("Central Tax", "9.00"), ("State Tax", "9.00"),
                           ("Integrated Tax", "18.00")):
            with self.subTest(duty_head=head):
                self.assertIn(f"<GSTRATEDUTYHEAD>{head}</GSTRATEDUTYHEAD>", xml)
        self.assertEqual(xml.count("<GSTRATE>9.00</GSTRATE>"), 2)
        self.assertEqual(xml.count("<GSTRATE>18.00</GSTRATE>"), 1)

    def test_an_odd_rate_still_halves_to_two_decimals(self):
        """5% is 2.5 each side, which is a real GST rate and not a round number."""
        xml = self._item_xml(gst_rate=Decimal("5"))
        self.assertEqual(xml.count("<GSTRATE>2.50</GSTRATE>"), 2)

    def test_the_rate_sits_inside_the_state_row_inside_the_gst_details(self):
        xml = self._item_xml(gst_rate=Decimal("12"))
        gst = xml[xml.index("<GSTDETAILS.LIST>"):xml.index("</GSTDETAILS.LIST>")]
        self.assertIn("<STATEWISEDETAILS.LIST>", gst)
        self.assertIn("<STATENAME>Any</STATENAME>", gst)
        self.assertIn("<RATEDETAILS.LIST>", gst)

    def test_an_item_erpnext_has_no_rate_for_sends_none(self):
        """No rate and a rate of nought are different claims about an item."""
        xml = self._item_xml()
        self.assertNotIn("<RATEDETAILS.LIST>", xml)
        self.assertIn("<TAXABILITY>", xml)


class TestVoucherGuidSurvivesTheRoundTrip(unittest.TestCase):

    def _voucher(self, remote_id="SINV-0001"):
        voucher = model.Voucher(kind="Sales", date=datetime.date(2026, 4, 8),
                                number="SINV-0001", party="Acme Ltd",
                                remote_id=remote_id)
        voucher.lines.append(model.LedgerLine(ledger="Acme Ltd",
                                              amount=Decimal("-118"), is_party=True))
        voucher.lines.append(model.LedgerLine(ledger="Sales", amount=Decimal("118")))
        return voucher

    def _read(self, voucher, with_guid=True):
        xml = render_vouchers("Demo Traders", [voucher],
                              with_guid=with_guid).encode("utf-8")
        return next(iter_vouchers(xml))

    def test_the_guid_written_is_the_guid_read(self):
        voucher = self._voucher()
        self.assertEqual(self._read(voucher).guid, voucher.tally_guid)

    def test_the_same_document_exports_to_the_same_guid(self):
        """The whole worth of it. A fresh value each run duplicates exactly as
        having none does."""
        self.assertEqual(self._read(self._voucher()).guid,
                         self._read(self._voucher()).guid)

    def test_two_documents_get_two_guids(self):
        self.assertNotEqual(self._read(self._voucher("SINV-0001")).guid,
                            self._read(self._voucher("SINV-0002")).guid)

    def test_a_guid_tally_already_knows_is_kept_rather_than_derived(self):
        voucher = self._voucher()
        voucher.guid = "01ae33cc-833c-479e-913c-c5079b964709-0000a46e"
        self.assertEqual(self._read(voucher).guid, voucher.guid)

    def test_a_voucher_naming_no_document_carries_no_guid(self):
        """Nothing to derive from, and an invented one would be worse than none:
        it would change every run and duplicate anyway."""
        xml = render_vouchers("Demo Traders", [self._voucher(remote_id=None)],
                              with_guid=True)
        self.assertNotIn("<GUID>", xml)

    def test_no_guid_is_sent_unless_it_is_asked_for(self):
        """Off by default since 20 September. A live TallyPrime crashed with
        `Software Exception c0000005 (Memory Access Violation)` importing
        vouchers that carried one, and ours is a bare UUID where Tally's own
        carry an eight-hex suffix after it. Not yet bisected to this field, so
        it is off rather than removed - and the sync ledger prevents duplicates
        without Tally's help anyway."""
        xml = render_vouchers("Demo Traders", [self._voucher()])
        self.assertNotIn("<GUID>", xml)
