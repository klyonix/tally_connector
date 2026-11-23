"""
The snapshot and the sync ledger are actually called, and actually change the file.

This file exists because of a defect in the audit's own findings list. P0-11 was
"destination discovery exists and is never used on the push path" - the probing
code had been written and only the advisory path called it. Adding two more
modules that nothing calls would have repeated exactly that, so what is checked
here is the wiring rather than the modules.

Each test asserts a difference in what comes out, not that a function was
reached. A call that happens and changes nothing is the same defect wearing a
mock.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from lxml import etree

from tally_connector.tally import model
from tally_connector.tally.render import render_masters
from tally_connector.tally.resolve import resolve
from tally_connector.tally.snapshot import TallySnapshot
from tally_connector.utils import exporter, sync_ledger as ledger


def _masters():
    masters = model.Masters()
    masters.ledgers = [
        model.Ledger(name="Acme Ltd", parent="Sundry Debtors"),
        model.Ledger(name="Brand New Customer", parent="Sundry Debtors"),
    ]
    masters.units = [model.Unit(symbol="Nos"), model.Unit(symbol="KG")]
    return masters


def _snapshot():
    def held(name, parent):
        return type("M", (), {"name": name, "parent": parent})()
    return TallySnapshot(
        ledgers={"acmeltd": held("Acme Ltd", "Sundry Debtors")},
        units={"nos": held("Nos", "")},
        groups={}, stock_groups={}, stock_items={}, godowns={},
        cost_centres={}, companies=["Demo Traders Pvt Ltd"])


def _names(xml, element):
    root = etree.fromstring(xml.encode("utf-8"))
    return [el.get("NAME") for el in root.iter(element)]


class TestTheSnapshotChangesTheMastersFile(FrappeTestCase):

    def test_without_a_resolution_everything_is_created_as_before(self):
        xml = render_masters("Co", _masters())
        self.assertEqual(sorted(_names(xml, "LEDGER")),
                         ["Acme Ltd", "Brand New Customer"])
        self.assertEqual(sorted(_names(xml, "UNIT")), ["KG", "Nos"])

    def test_a_master_tally_holds_is_left_out_of_the_file(self):
        """The fix for `Created: 9,626  Altered: 9,694` against a live chart."""
        xml = render_masters("Co", _masters(), resolution=resolve(_masters(),
                                                                 _snapshot()))
        self.assertEqual(_names(xml, "LEDGER"), ["Brand New Customer"])
        self.assertEqual(_names(xml, "UNIT"), ["KG"])

    def test_what_is_sent_is_still_sent_as_a_create(self):
        xml = render_masters("Co", _masters(), resolution=resolve(_masters(),
                                                                  _snapshot()))
        root = etree.fromstring(xml.encode("utf-8"))
        for el in root.iter("LEDGER"):
            self.assertEqual(el.get("ACTION"), "Create")

    def test_a_tally_that_said_nothing_sends_everything(self):
        """Unknown falls back to the old behaviour rather than to an empty
        company, which would mean creating everything *and* believing it."""
        xml = render_masters("Co", _masters(),
                             resolution=resolve(_masters(), TallySnapshot()))
        self.assertEqual(sorted(_names(xml, "LEDGER")),
                         ["Acme Ltd", "Brand New Customer"])


class TestTheExporterAsksTheDestination(FrappeTestCase):

    def test_it_asks_and_uses_the_answer(self):
        """`in_test` is patched off deliberately: the guard exists so an
        ordinary test run does not depend on somebody's Tally being switched
        on, and this test is the one that wants a destination, so it supplies
        one of its own."""
        with patch.object(exporter, "_settings",
                          return_value=frappe._dict({"sync_enabled": 1})), \
             patch("tally_connector.utils.connection.TallyConnector.snapshot",
                   return_value=_snapshot()):
            resolution = exporter.destination_resolution(_masters(), ask=True)
        self.assertIsNotNone(resolution)
        self.assertEqual(len(resolution.by_action("reuse")), 2)

    def test_a_tally_that_is_not_there_does_not_break_the_export(self):
        with patch.object(exporter, "_settings",
                          return_value=frappe._dict({"sync_enabled": 1})), \
             patch("tally_connector.utils.connection.TallyConnector.snapshot",
                   side_effect=OSError("connection refused")):
            self.assertIsNone(exporter.destination_resolution(_masters(), ask=True))

    def test_sync_being_off_does_not_ask_at_all(self):
        with patch.object(exporter, "_settings",
                          return_value=frappe._dict({"sync_enabled": 0})), \
             patch("tally_connector.utils.connection.TallyConnector.snapshot") as probe:
            self.assertIsNone(exporter.destination_resolution(_masters(), ask=True))
        probe.assert_not_called()


def _voucher(name):
    voucher = model.Voucher(kind="Sales", date=datetime.date(2026, 4, 8),
                            number=name, remote_id=name, party="Acme Ltd")
    voucher.lines.append(model.LedgerLine(ledger="Acme Ltd",
                                          amount=Decimal("-100"), is_party=True))
    voucher.lines.append(model.LedgerLine(ledger="Sales", amount=Decimal("100")))
    return voucher


class TestTheExporterAsksTheSyncLedger(FrappeTestCase):

    def setUp(self):
        self.company = frappe.db.get_value("Company", {}, "name")
        self.tally = "Demo Traders Pvt Ltd"
        for name in ("SINV-W-1", "SINV-W-2"):
            frappe.db.delete(ledger.DOCTYPE, {"source_name": name})

    def test_nothing_sent_yet_means_everything_goes(self):
        vouchers = [_voucher("SINV-W-1"), _voucher("SINV-W-2")]
        to_send, skipped = exporter.unsent_vouchers(vouchers, self.company,
                                                    self.tally)
        self.assertEqual(len(to_send), 2)
        self.assertEqual(skipped, [])

    def test_a_voucher_already_in_tally_is_left_out_of_the_file(self):
        row = ledger.note_attempt("Sales Invoice", "SINV-W-1", self.company,
                                  self.tally, voucher_number="SINV-W-1")
        ledger.note_outcome(row, ledger.CREATED)
        vouchers = [_voucher("SINV-W-1"), _voucher("SINV-W-2")]
        to_send, skipped = exporter.unsent_vouchers(vouchers, self.company,
                                                    self.tally)
        self.assertEqual([v.number for v in to_send], ["SINV-W-2"])
        self.assertEqual(len(skipped), 1)
        self.assertIn("already in Tally", skipped[0][1])

    def test_an_unknown_result_is_also_left_out_and_explained(self):
        row = ledger.note_attempt("Sales Invoice", "SINV-W-1", self.company,
                                  self.tally, voucher_number="SINV-W-1")
        ledger.note_outcome(row, ledger.UNKNOWN, "answer could not be read")
        to_send, skipped = exporter.unsent_vouchers(
            [_voucher("SINV-W-1")], self.company, self.tally)
        self.assertEqual(to_send, [])
        self.assertIn("Check Tally", skipped[0][1])

    def test_a_sync_ledger_that_cannot_be_read_sends_everything(self):
        """Failing closed here would stop exports working at all, which is a
        worse failure than the one it guards against."""
        with patch("tally_connector.utils.sync_ledger.unsent",
                   side_effect=RuntimeError("table missing")):
            to_send, skipped = exporter.unsent_vouchers(
                [_voucher("SINV-W-1")], self.company, self.tally)
        self.assertEqual(len(to_send), 1)
        self.assertEqual(skipped, [])

    def test_the_outcome_is_recorded_for_what_was_sent(self):
        vouchers = [_voucher("SINV-W-1"), _voucher("SINV-W-2")]
        result = ledger.record_sent(
            vouchers, self.company, self.tally,
            {"success": True, "reached": True, "unknown": False, "created": 2,
             "altered": 0, "errors": [], "last_voucher_id": "42"})
        self.assertEqual(result["outcome"], ledger.CREATED)
        self.assertEqual(result["records"], 2)
        again, skipped = exporter.unsent_vouchers(vouchers, self.company,
                                                  self.tally)
        self.assertEqual(again, [], "a second export sent them again")


class TestATestRunNeverAsksALiveTally(FrappeTestCase):
    """Seven modules went red the first time a real Tally was connected, and
    every one of them was right: with a destination reachable the export sends
    only the masters Tally has not got, so an assertion about the file became
    an assertion about that Tally. A test run must not depend on whether
    somebody's Tally is switched on."""

    def test_the_probe_is_skipped_in_tests(self):
        with patch("tally_connector.utils.connection.TallyConnector.snapshot") as probe:
            self.assertIsNone(exporter.destination_resolution(_masters()))
        probe.assert_not_called()
