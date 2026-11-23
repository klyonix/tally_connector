"""
Masters sent a batch at a time, in the order Tally has to read them.

A single masters push on this engagement was 79MB and a live TallyPrime
answered `Software Exception c0000005 (Memory Access Violation)` rather than
refusing it. Sending 6,449 ledgers and 6,172 stock items in one file is not a
size question a bigger timeout fixes.

Two things have to hold at once, and they pull against each other. Batching
wants a flat list to slice; Tally wants every parent kind finished before the
child kind starts. So this batches **within** a kind and never across two, and
the order of the kinds is held against the renderer's own order rather than
written down twice.

Pure: the transport is replaced, so no Tally is needed and none is touched.
"""

from __future__ import annotations

import re
import unittest
from unittest.mock import patch

from tally_connector.tally import model
from tally_connector.utils import connection
from tally_connector.utils.connection import MASTER_ORDER, push_masters


def _masters(ledgers=0, items=0, groups=0):
    m = model.Masters()
    for i in range(groups):
        m.groups.append(model.Group(name=f"Group {i}", parent="Primary"))
    for i in range(ledgers):
        m.ledgers.append(model.Ledger(name=f"Party {i}", parent="Sundry Debtors"))
    for i in range(items):
        m.stock_items.append(model.StockItem(name=f"Item {i}", base_unit="Nos"))
    return m


def _ok(**over):
    out = {"success": True, "created": 1, "altered": 0, "ignored": 0,
           "unknown": False, "errors": []}
    out.update(over)
    return out


class TestTheOrderCannotDrift(unittest.TestCase):

    def test_master_order_is_the_order_the_renderer_writes(self):
        """Two copies of one ordering is how they come to disagree, and the
        one that matters is the renderer's, because that is the file Tally
        reads. So this reads the renderer rather than restating it."""
        import inspect

        from tally_connector.tally import render

        source = inspect.getsource(render.render_masters)
        seen = []
        for match in re.finditer(r"for \w+ in masters\.(\w+)", source):
            kind = match.group(1)
            if kind not in seen:
                seen.append(kind)
        self.assertEqual(
            seen, list(MASTER_ORDER),
            "push_masters would batch kinds in an order the renderer does not "
            "write them in, so a child could be sent before its parent")

    def test_every_kind_in_the_order_is_a_real_field(self):
        m = model.Masters()
        for kind in MASTER_ORDER:
            with self.subTest(kind=kind):
                self.assertIsInstance(getattr(m, kind), list)


class TestBatching(unittest.TestCase):

    def _push(self, masters, results=None, batch=250):
        """Send, with the transport replaced by a list of canned answers."""
        calls = []
        answers = list(results or [])

        def fake(xml, log=None):
            calls.append(xml)
            return answers.pop(0) if answers else _ok()

        with patch.object(connection, "push_to_tally", side_effect=fake):
            summary = push_masters("Co", masters, batch=batch)
        return summary, calls

    def test_it_goes_in_more_than_one_file(self):
        summary, calls = self._push(_masters(ledgers=600), batch=250)
        self.assertEqual(len(calls), 3)
        self.assertEqual(summary["sent"], 600)
        self.assertTrue(summary["success"])

    def test_no_batch_carries_two_kinds(self):
        """The whole reason this does not slice a flat list."""
        _summary, calls = self._push(_masters(groups=2, ledgers=300, items=300),
                                     batch=250)
        for xml in calls:
            with self.subTest(size=len(xml)):
                kinds = {t for t in ("<GROUP ", "<LEDGER ", "<STOCKITEM ")
                         if t in xml}
                self.assertEqual(len(kinds), 1,
                                 f"one file carries {kinds}")

    def test_the_kinds_arrive_in_the_renderers_order(self):
        _summary, calls = self._push(_masters(groups=2, ledgers=300, items=300),
                                     batch=250)
        order = []
        for xml in calls:
            for tag, kind in (("<GROUP ", "groups"), ("<STOCKITEM ", "stock_items"),
                              ("<LEDGER ", "ledgers")):
                if tag in xml and (not order or order[-1] != kind):
                    order.append(kind)
        self.assertEqual(order, ["groups", "stock_items", "ledgers"])

    def test_a_batch_says_which_kind_and_which_records_it_carried(self):
        summary, _calls = self._push(_masters(ledgers=300), batch=250)
        first = summary["batches"][0]
        self.assertEqual(first["kind"], "ledgers")
        self.assertEqual(first["masters"], 250)
        self.assertEqual(first["first"], "Party 0")
        self.assertEqual(first["last"], "Party 249")
        self.assertGreater(first["bytes"], 0)


class TestStoppingAtTheFirstRefusal(unittest.TestCase):
    """Tally aborts a file at the first record it refuses and loses the rest,
    so the batch that failed is partly imported and every later batch would
    land on a company whose masters are already wrong. Carrying on turns one
    clear failure into a guess about what is where."""

    def _push(self, masters, results, batch=250):
        answers = list(results)
        calls = []

        def fake(xml, log=None):
            calls.append(xml)
            return answers.pop(0) if answers else _ok()

        with patch.object(connection, "push_to_tally", side_effect=fake):
            return push_masters("Co", masters, batch=batch), calls

    def test_it_stops_rather_than_sending_the_rest(self):
        summary, calls = self._push(
            _masters(ledgers=1000),
            [_ok(), {"success": False, "errors": ["Ledger 'X' does not exist!"],
                     "unknown": False}],
            batch=250)
        self.assertFalse(summary["success"])
        self.assertEqual(summary["stopped_at"], 2)
        self.assertEqual(len(calls), 2, "it kept sending after a refusal")

    def test_it_says_what_did_land_before_it_stopped(self):
        """Somebody has to know what is in Tally, not only that it broke."""
        summary, _calls = self._push(
            _masters(ledgers=1000),
            [_ok(created=250), {"success": False, "errors": ["boom"]}],
            batch=250)
        self.assertEqual(summary["sent"], 250)
        self.assertEqual(summary["created"], 250)
        self.assertEqual(summary["errors"], ["boom"])

    def test_an_unknown_answer_stops_it_too(self):
        """A Tally that fell over has not said no, and carrying on into one
        that may be half dead is how the last run ended."""
        summary, calls = self._push(
            _masters(ledgers=1000),
            [{"success": False, "unknown": True, "errors": []}],
            batch=250)
        self.assertTrue(summary["unknown"])
        self.assertFalse(summary["success"])
        self.assertEqual(len(calls), 1)


class TestNotPostingAnEmptyFile(unittest.TestCase):
    """The resolution drops every master the destination already holds, so a
    batch of 250 can legitimately come out empty. Posting it is a round trip
    that can only fail, on a company that has already fallen over once."""

    def test_a_batch_the_resolution_empties_is_not_sent(self):
        from tally_connector.tally.resolve import REUSE, Decision, Resolution

        masters = _masters(ledgers=300)
        decisions = [Decision(kind="ledgers", name=l.name, action=REUSE,
                              reason="already there")
                     for l in masters.ledgers]
        resolution = Resolution(decisions=decisions)
        calls = []

        def fake(xml, log=None):
            calls.append(xml)
            return _ok()

        with patch.object(connection, "push_to_tally", side_effect=fake):
            summary = push_masters("Co", masters, resolution=resolution,
                                   batch=250)
        self.assertEqual(calls, [], "posted a file with no master in it")
        self.assertEqual(summary["skipped_empty"], 2)
        self.assertTrue(summary["success"])

    def test_nothing_at_all_is_a_success_and_not_a_failure(self):
        summary, calls = TestBatching()._push(model.Masters())
        self.assertTrue(summary["success"])
        self.assertEqual(calls, [])


class TestNamingWhatTallyRefused(unittest.TestCase):
    """Tally will not say what it refused.

    A real batch answered *"0 errors and 1 exceptions"* and named nothing, so
    the only way to learn which record it was is to send the batch again one
    master at a time. It turned out to be a customer called `....` - four dots,
    which is the empty name once Tally has matched it.

    The master that stands in for it here is an ordinary name Tally happens to
    refuse, deliberately: that defect is now caught before the file is built,
    so using `....` would test the renderer's omission rather than this. What
    is under test is the naming, whatever the cause.
    """

    def _push(self, masters, bad_names, batch=50):
        calls = []

        def fake(xml, log=None):
            calls.append(xml)
            if any(f"NAME=\"{n}\"" in xml or f"<NAME>{n}</NAME>" in xml
                   for n in bad_names):
                return {"success": False, "unknown": False, "created": 0,
                        "altered": 0, "ignored": 0,
                        "errors": ["Tally reported 0 errors and 1 exceptions "
                                   "and named 0 of them."]}
            return _ok(created=xml.count("<LEDGER "))

        with patch.object(connection, "push_to_tally", side_effect=fake):
            summary = push_masters("Co", masters, batch=batch,
                                   name_refusals=True)
        return summary, calls

    def test_the_refused_master_is_named(self):
        masters = _masters(ledgers=50)
        summary, _calls = self._push(masters, ["Party 7"])
        self.assertEqual([r["name"] for r in summary["refused"]], ["Party 7"])
        self.assertEqual(summary["refused"][0]["kind"], "ledgers")
        self.assertIn("exceptions", summary["refused"][0]["error"])

    def test_the_rest_of_that_batch_still_goes(self):
        """The 49 good ones in a batch of 50 are not collateral."""
        masters = _masters(ledgers=50)
        summary, _calls = self._push(masters, ["Party 7"])
        self.assertEqual(summary["sent"], 49)

    def test_the_batches_after_it_still_go(self):
        masters = _masters(ledgers=150)
        summary, _calls = self._push(masters, ["Party 7"], batch=50)
        self.assertEqual(summary["sent"], 149)

    def test_it_is_still_not_a_success(self):
        """Something was refused. Reporting a clean run would put the missing
        party's vouchers on somebody else's desk to puzzle over."""
        masters = _masters(ledgers=50)
        summary, _calls = self._push(masters, ["Party 7"])
        self.assertFalse(summary["success"])

    def test_without_the_flag_it_stops_as_before(self):
        """The default is unchanged: stop, and say which batch."""
        masters = _masters(ledgers=150)
        calls = []

        def fake(xml, log=None):
            calls.append(xml)
            return {"success": False, "unknown": False, "errors": ["no"]}

        with patch.object(connection, "push_to_tally", side_effect=fake):
            summary = push_masters("Co", masters, batch=50)
        self.assertEqual(summary["stopped_at"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(summary["refused"], [])


class TestAnUnreadableAnswerIsNeverBisected(unittest.TestCase):
    """`unknown` means Tally fell over or said something that could not be
    read. Sending it another 250 requests one at a time is how a wounded Tally
    is finished off, which is the failure this batching exists to avoid.

    Found by a test rather than by reading: the first version of
    `name_refusals` bisected on any failure, and the push gate's unknown-answer
    case turned one request into 178.
    """

    def _push(self, results):
        """The first answer is the one under test; everything after it is
        clean, so a later batch cannot be what sets `stopped_at`. The first
        version of this let an unknown default leak into batch two and the
        control failed for a reason that was nothing to do with its subject."""
        calls = []
        answers = list(results)

        def fake(xml, log=None):
            calls.append(xml)
            return answers.pop(0) if answers else _ok()

        with patch.object(connection, "push_to_tally", side_effect=fake):
            return push_masters("Co", _masters(ledgers=150), batch=50,
                                name_refusals=True), calls

    def test_it_stops_on_an_unreadable_answer(self):
        summary, calls = self._push([{"success": False, "unknown": True,
                                      "errors": ["not XML"]}])
        self.assertEqual(len(calls), 1, "it kept talking to a Tally that fell over")
        self.assertEqual(summary["stopped_at"], 1)
        self.assertTrue(summary["unknown"])
        self.assertEqual(summary["refused"], [])

    def test_a_readable_refusal_is_still_bisected(self):
        """The control: the two answers must not be treated the same."""
        summary, calls = self._push([{"success": False, "unknown": False,
                                      "errors": ["1 exception"]}])
        self.assertGreater(len(calls), 1)
        self.assertIsNone(summary["stopped_at"])


class TestSentMeansSent(unittest.TestCase):
    """`sent` used to be the chunk's length, which is records considered.

    The renderer drops what the destination already holds and what Tally could
    not name, so a batch of 50 can put 49 in the file - and a summary saying 50
    is the kind of number somebody reconciles against Tally and cannot make
    balance.
    """

    def _sent(self, masters, resolution=None):
        with patch.object(connection, "push_to_tally",
                          side_effect=lambda xml, log=None: _ok()):
            return push_masters("Co", masters, resolution=resolution,
                                batch=50)["sent"]

    def test_a_master_tally_cannot_name_is_not_counted_as_sent(self):
        masters = _masters(ledgers=50)
        masters.ledgers[3].name = "...."
        self.assertEqual(self._sent(masters), 49)

    def test_a_master_the_destination_holds_is_not_counted_either(self):
        from tally_connector.tally.resolve import CREATE, REUSE, Decision, Resolution

        masters = _masters(ledgers=50)
        decisions = [
            Decision(kind="ledgers", name=l.name,
                     action=REUSE if i < 10 else CREATE, reason="")
            for i, l in enumerate(masters.ledgers)]
        self.assertEqual(self._sent(masters, Resolution(decisions=decisions)), 40)

    def test_an_ordinary_batch_still_counts_all_of_it(self):
        self.assertEqual(self._sent(_masters(ledgers=50)), 50)


class TestCarryingOnPastARefusedBatch(unittest.TestCase):
    """A run that stops at the first bad batch leaves the rest of a year's
    books in ERPNext.

    Carrying on is only safe because Tally is asked what it holds. Tally
    aborts a file at the record it refused and keeps what it read before that,
    so part of the batch is in the books - and sending the whole batch again
    would post those a second time, which is the one failure that cannot be
    undone from here.
    """

    def _voucher(self, number):
        import datetime
        from decimal import Decimal

        v = model.Voucher(kind="Sales", date=datetime.date(2026, 4, 8),
                          number=number, party="Acme", remote_id=number)
        v.lines.append(model.LedgerLine(ledger="Acme", amount=Decimal("-1"),
                                        is_party=True))
        v.lines.append(model.LedgerLine(ledger="Sales", amount=Decimal("1")))
        return v

    def _run(self, holds, keep_going=True):
        rows = [self._voucher("V%d" % i) for i in range(6)]
        answers = [{"success": False, "reached": True, "unknown": False,
                    "created": 0, "altered": 0, "errors": ["1 error"]},
                   {"success": True, "reached": True, "unknown": False,
                    "created": 3, "altered": 0, "errors": []}]
        calls = []

        def fake(xml, log=None):
            calls.append(xml)
            return answers[min(len(calls) - 1, len(answers) - 1)]

        with patch.object(connection, "push_to_tally", side_effect=fake), \
             patch.object(connection, "_split_by_what_tally_holds",
                          return_value=holds), \
             patch.object(connection, "_record_batch", lambda *a, **k: None):
            out = connection.push_vouchers(
                "Co", rows, batch=3, keep_going=keep_going,
                period=("2026-04-01", "2026-04-30"), erpnext_company="X")
        return out, calls

    def test_it_sends_the_batches_after_a_refusal(self):
        rows = [self._voucher("V%d" % i) for i in range(3)]
        out, calls = self._run((rows[:2], rows[2:]))
        self.assertEqual(len(calls), 2, "it stopped instead of carrying on")
        self.assertIsNone(out["stopped_at"])

    def test_what_landed_in_the_bad_batch_is_counted_as_sent(self):
        rows = [self._voucher("V%d" % i) for i in range(3)]
        out, _calls = self._run((rows[:2], rows[2:]))
        self.assertEqual(out["created"], 2 + 3)

    def test_what_was_refused_is_named(self):
        rows = [self._voucher("V%d" % i) for i in range(3)]
        out, _calls = self._run((rows[:2], rows[2:]))
        self.assertEqual([r["number"] for r in out["refused"]], ["V2"])
        self.assertFalse(out["success"], "a refusal is never a clean run")

    def test_a_tally_that_did_not_answer_stops_the_run(self):
        """Reading a failed probe as "none of them landed" would send the
        whole batch again and duplicate whatever did."""
        out, calls = self._run((None, None))
        self.assertEqual(len(calls), 1)
        self.assertEqual(out["stopped_at"], 1)

    def test_without_keep_going_it_stops_as_before(self):
        rows = [self._voucher("V%d" % i) for i in range(3)]
        out, calls = self._run((rows[:2], rows[2:]), keep_going=False)
        self.assertEqual(len(calls), 1)
        self.assertEqual(out["stopped_at"], 1)


class TestTheReconcileReadStaysSmall(unittest.TestCase):
    """The read after a refused batch asks about that batch's own dates.

    Asking for the whole period meant a read that grew with every voucher that
    landed, run after every refused batch. On a real run - 43% refused - that
    is hundreds of full-year Day Book reads, each bigger than the last, until
    one took longer than the timeout and stopped a push that was working. The
    batch spans a day or two and that read stays the same size all night.
    """

    def test_it_asks_about_the_batch_not_the_whole_period(self):
        import datetime

        rows = []
        for day in (8, 9):
            v = model.Voucher(kind="Sales", date=datetime.date(2026, 4, day),
                              number="V%d" % day)
            rows.append(v)
        asked = {}

        class FakeConn:
            def vouchers_in_period(self, a, b):
                asked["window"] = (a, b)
                return []

        with patch.object(connection, "TallyConnector", FakeConn):
            connection._split_by_what_tally_holds(
                rows, "Co", ("2025-04-01", "2026-03-31"))
        self.assertEqual(asked["window"], ("2026-04-08", "2026-04-09"))

    def test_a_batch_with_no_dates_falls_back_to_the_period(self):
        asked = {}

        class FakeConn:
            def vouchers_in_period(self, a, b):
                asked["window"] = (a, b)
                return []

        v = model.Voucher(kind="Sales", date=None, number="V1")
        with patch.object(connection, "TallyConnector", FakeConn):
            connection._split_by_what_tally_holds(
                [v], "Co", ("2025-04-01", "2026-03-31"))
        self.assertEqual(asked["window"], ("2025-04-01", "2026-03-31"))


class TestTheProbeIsAskedMoreThanOnce(unittest.TestCase):
    """One probe that timed out used to stop the whole run.

    Tally is busy in the moment after it has refused a file, and on a push
    with a 43% refusal rate this probe is reached constantly. Three real runs
    stopped at batches 32, 1 and 2 for exactly this, leaving 24,000 vouchers
    in ERPNext. Unknown after several tries still stops, because it still
    means nobody knows what landed.
    """

    def _probe(self, answers):
        import datetime

        calls = {"n": 0}

        class FakeConn:
            def vouchers_in_period(self, a, b):
                answer = answers[min(calls["n"], len(answers) - 1)]
                calls["n"] += 1
                return answer

        v = model.Voucher(kind="Sales", date=datetime.date(2026, 4, 8),
                          number="V1")
        with patch.object(connection, "TallyConnector", FakeConn), \
             patch.object(connection, "PROBE_PAUSE", 0):
            got = connection._split_by_what_tally_holds(
                [v], "Co", ("2025-04-01", "2026-03-31"))
        return got, calls["n"]

    def test_a_probe_that_answers_late_still_answers(self):
        (landed, refused), calls = self._probe([None, None, [{"number": "V1"}]])
        self.assertEqual(calls, 3)
        self.assertEqual([v.number for v in landed], ["V1"])
        self.assertEqual(refused, [])

    def test_it_gives_up_after_the_last_try(self):
        (landed, refused), calls = self._probe([None])
        self.assertEqual(calls, connection.PROBE_TRIES)
        self.assertIsNone(landed)
        self.assertIsNone(refused)

    def test_a_first_answer_costs_no_extra_calls(self):
        (landed, _r), calls = self._probe([[]])
        self.assertEqual(calls, 1)
        self.assertEqual(landed, [])
