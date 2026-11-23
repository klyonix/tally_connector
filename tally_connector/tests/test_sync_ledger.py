"""
Push the same document once, twice, five times. There must be one voucher.

This was P0 and it was unanswerable: nothing recorded that a document had been
sent, so every duplicate-prevention question came down to a TallyPrime setting
this app cannot see, cannot set and never checked.

The case that matters most is not the double press. It is the one where **the
request reached Tally, Tally imported it, and the answer was lost** - a timeout
on a payload the code itself says can legitimately take a minute. Tally holds
the vouchers. `Failed` invites the retry that duplicates them; `Created` loses
them. It is its own outcome, and the only correct action is for a person to
look.

Integration tests, because the ledger is a DocType. No HTTP happens.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from tally_connector.utils import sync_ledger as ledger

ERP = None

DOC = ("Sales Invoice", "SINV-TEST-0001")
TALLY = "Demo Traders Pvt Ltd"


def _company():
    """A real company, because the record links to one. Read rather than
    created: these tests are about the ledger, not about companies."""
    return frappe.db.get_value("Company", {}, "name")


def _push(created=1, altered=0, success=True, reached=True, unknown=False,
          errors=None):
    return {"success": success, "reached": reached, "unknown": unknown,
            "created": created, "altered": altered, "ignored": 0,
            "errors": errors or [], "last_voucher_id": "119",
            "response": "<RESPONSE/>"}


class TestOneDocumentOneVoucher(FrappeTestCase):

    def setUp(self):
        global ERP
        ERP = _company()
        frappe.db.delete(ledger.DOCTYPE, {"source_name": DOC[1]})

    def _send(self, payload="<VOUCHER/>", result=None):
        """One push, recorded the way the export path records it."""
        skip, why = ledger.already_sent(*DOC, ERP, TALLY, payload=payload)
        if skip:
            return None, why
        row = ledger.note_attempt(*DOC, ERP, TALLY, voucher_type="Sales",
                                  voucher_number=DOC[1], payload=payload)
        outcome, message = ledger.outcome_from_push(result or _push())
        ledger.note_outcome(row, outcome, message,
                            (result or _push()).get("last_voucher_id"))
        return row, ""

    def test_sending_once_records_it_as_created(self):
        row, _ = self._send()
        self.assertEqual(row.outcome, ledger.CREATED)
        self.assertEqual(row.tally_voucher_id, "119")

    def test_sending_twice_sends_once(self):
        self._send()
        row, why = self._send()
        self.assertIsNone(row, "the second send was not prevented")
        self.assertIn("already in Tally", why)

    def test_sending_five_times_leaves_one_record_and_one_send(self):
        sent = 0
        for _ in range(5):
            row, _ = self._send()
            sent += 1 if row else 0
        self.assertEqual(sent, 1)
        self.assertEqual(
            frappe.db.count(ledger.DOCTYPE, {"source_name": DOC[1]}), 1)

    def test_the_same_document_to_a_second_tally_company_is_a_second_send(self):
        """Two destinations really are two sends, and one row cannot say so."""
        self._send()
        skip, _ = ledger.already_sent(*DOC, ERP, "Another Tally Company")
        self.assertFalse(skip)


class TestTheAnswerNobodyGot(FrappeTestCase):
    """A request that reached Tally whose answer was lost."""

    def setUp(self):
        global ERP
        ERP = _company()
        frappe.db.delete(ledger.DOCTYPE, {"source_name": DOC[1]})

    def test_an_unreadable_answer_is_its_own_outcome(self):
        outcome, _ = ledger.outcome_from_push(
            _push(created=0, success=False, unknown=True,
                  errors=["could not be read as XML"]))
        self.assertEqual(outcome, ledger.UNKNOWN)

    def test_it_is_not_recorded_as_failed(self):
        """`Failed` is resendable, and resending is the one action guaranteed
        to be wrong if the first attempt landed."""
        self.assertIn(ledger.FAILED, ledger.RESENDABLE)
        self.assertNotIn(ledger.UNKNOWN, ledger.RESENDABLE)

    def test_an_unknown_result_stops_the_next_send_and_says_why(self):
        row = ledger.note_attempt(*DOC, ERP, TALLY, voucher_number=DOC[1])
        ledger.note_outcome(row, ledger.UNKNOWN, "answer could not be read")
        skip, why = ledger.already_sent(*DOC, ERP, TALLY)
        self.assertTrue(skip)
        self.assertIn("unknown", why)
        self.assertIn("Check Tally", why)

    def test_a_genuine_failure_may_be_sent_again(self):
        row = ledger.note_attempt(*DOC, ERP, TALLY, voucher_number=DOC[1])
        ledger.note_outcome(row, ledger.FAILED, "Tally could not be reached")
        skip, _ = ledger.already_sent(*DOC, ERP, TALLY)
        self.assertFalse(skip)

    def test_the_attempt_is_recorded_before_the_send_not_after(self):
        """A row written only after a reply never exists for the request that
        got no reply, which is precisely the case that needs one."""
        row = ledger.note_attempt(*DOC, ERP, TALLY, voucher_number=DOC[1])
        self.assertEqual(row.outcome, ledger.PENDING)
        self.assertEqual(row.attempts, 1)
        self.assertTrue(row.last_attempt_at)


class TestAnAmendmentIsNotADuplicate(FrappeTestCase):

    def setUp(self):
        global ERP
        ERP = _company()
        frappe.db.delete(ledger.DOCTYPE, {"source_name": DOC[1]})

    def test_an_unchanged_document_is_not_sent_again(self):
        row = ledger.note_attempt(*DOC, ERP, TALLY, payload="<VOUCHER>100</VOUCHER>")
        ledger.note_outcome(row, ledger.CREATED)
        skip, _ = ledger.already_sent(*DOC, ERP, TALLY,
                                      payload="<VOUCHER>100</VOUCHER>")
        self.assertTrue(skip)

    def test_a_changed_document_is_sent_and_named_as_a_change(self):
        row = ledger.note_attempt(*DOC, ERP, TALLY, payload="<VOUCHER>100</VOUCHER>")
        ledger.note_outcome(row, ledger.CREATED)
        skip, why = ledger.already_sent(*DOC, ERP, TALLY,
                                        payload="<VOUCHER>250</VOUCHER>")
        self.assertFalse(skip)
        self.assertIn("changed since", why)

    def test_the_hash_is_over_the_voucher_and_is_stable(self):
        self.assertEqual(ledger.payload_hash("<VOUCHER/>"),
                         ledger.payload_hash("<VOUCHER/>"))
        self.assertNotEqual(ledger.payload_hash("<VOUCHER>1</VOUCHER>"),
                            ledger.payload_hash("<VOUCHER>2</VOUCHER>"))


class TestTheOutcomeVocabulary(FrappeTestCase):

    def setUp(self):
        global ERP
        ERP = _company()
        frappe.db.delete(ledger.DOCTYPE, {"source_name": DOC[1]})

    def test_tallys_three_states_map_onto_outcomes(self):
        cases = [
            (_push(created=1), ledger.CREATED),
            (_push(created=0, altered=1), ledger.ALTERED),
            (_push(created=0, altered=0), ledger.EXISTING),
            (_push(success=False, reached=False), ledger.FAILED),
            (_push(success=False, unknown=True), ledger.UNKNOWN),
        ]
        for result, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(ledger.outcome_from_push(result)[0], expected)

    def test_every_outcome_is_one_the_doctype_offers(self):
        options = frappe.get_meta(ledger.DOCTYPE).get_field("outcome").options
        offered = {o.strip() for o in options.split("\n") if o.strip()}
        self.assertEqual(set(ledger.OUTCOMES), offered)

    def test_an_outcome_the_vocabulary_does_not_have_is_refused(self):
        row = ledger.note_attempt(*DOC, ERP, TALLY)
        with self.assertRaises(frappe.ValidationError):
            ledger.note_outcome(row, "Probably Fine")

    def test_a_sync_record_cannot_be_deleted(self):
        """It is the only evidence a document reached a customer's books."""
        row = ledger.note_attempt(*DOC, ERP, TALLY)
        with self.assertRaises(frappe.ValidationError):
            row.delete()


class TestASendIsActuallyRecorded(FrappeTestCase):
    """`record_sent` was written, tested, and called from nowhere.

    So nothing was ever written to the sync ledger, `unsent_vouchers` went on
    returning every voucher for ever, and a second run would have posted all of
    them into the customer's books a second time. That is the one failure this
    connector must not have, and it was one retry away - the same shape as
    `Ledger.affects_stock`, which was declared, read by the renderer, and set
    by nothing.
    """

    def _voucher(self, number="SINV-9001"):
        import datetime
        from decimal import Decimal

        from tally_connector.tally import model

        v = model.Voucher(kind="Sales", date=datetime.date(2026, 4, 8),
                          number=number, party="Acme", remote_id=number)
        v.lines.append(model.LedgerLine(ledger="Acme", amount=Decimal("-1"),
                                        is_party=True))
        v.lines.append(model.LedgerLine(ledger="Sales", amount=Decimal("1")))
        return v

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A real Company, because the sync record Links to one - a fixture
        # naming a company that does not exist fails the link check, which is
        # the ledger refusing to record against nothing.
        cls.real_company = frappe.db.get_value("Company", {}, "name")

    def _push(self, vouchers, result, erpnext_company="__real__"):
        if erpnext_company == "__real__":
            erpnext_company = self.real_company
        from unittest.mock import patch

        from tally_connector.utils import connection

        with patch.object(connection, "push_to_tally", return_value=result):
            return connection.push_vouchers(
                "Tally Co", vouchers, batch=50,
                erpnext_company=erpnext_company)

    def _rows(self, number):
        return frappe.get_all("Tally Sync Record",
                              filters={"source_name": number},
                              fields=["name", "outcome"])

    def test_a_successful_batch_is_written_to_the_ledger(self):
        number = "SINV-REC-1"
        self._push([self._voucher(number)],
                   {"success": True, "created": 1, "altered": 0,
                    "unknown": False, "errors": [], "last_voucher_id": "7"})
        self.assertTrue(self._rows(number), "nothing recorded, so a retry duplicates")

    def test_a_failed_batch_is_recorded_too(self):
        """Tally keeps what it read before the record it refused, so some of
        that batch is in the books. A retry that sends it again as though
        nothing happened is how duplicates get made."""
        number = "SINV-REC-2"
        self._push([self._voucher(number)],
                   {"success": False, "created": 0, "altered": 0,
                    "unknown": False, "errors": ["no"], "last_voucher_id": ""})
        self.assertTrue(self._rows(number))

    def test_without_an_erpnext_company_nothing_is_written(self):
        """Rows under a blank company are rows `unsent_vouchers` never matches,
        which is worse than none: they look like a record and are not."""
        number = "SINV-REC-3"
        self._push([self._voucher(number)],
                   {"success": True, "created": 1, "altered": 0,
                    "unknown": False, "errors": []}, erpnext_company=None)
        self.assertEqual(self._rows(number), [])

    def test_a_ledger_write_that_fails_does_not_fail_the_push(self):
        """The push reached Tally whatever happened here, and reporting it as
        failed is the reading under which somebody retries and duplicates."""
        from unittest.mock import patch

        from tally_connector.utils import connection

        with patch.object(connection, "push_to_tally",
                          return_value={"success": True, "created": 1,
                                        "altered": 0, "unknown": False,
                                        "errors": []}), \
             patch("tally_connector.utils.sync_ledger.record_sent",
                   side_effect=RuntimeError("ledger down")):
            out = connection.push_vouchers("Tally Co", [self._voucher("SINV-REC-4")],
                                           batch=50,
                                           erpnext_company=self.real_company)
        self.assertTrue(out["success"])
        self.assertEqual(out["created"], 1)


class TestTheLedgerSurvivesARunThatStops(FrappeTestCase):
    """A push of 26,657 vouchers was stopped part way and the ledger had
    written nothing.

    `record_sent` was being called per batch, correctly, and every row went
    with the transaction when the process was killed - leaving 2,503 vouchers
    in Tally that nothing knew about, which on the next run would have been
    sent again. Recording per batch is only worth doing if it is committed per
    batch: a record that exists only when the process exits cleanly is not a
    record of what was sent.
    """

    def test_each_batch_is_committed_as_it_lands(self):
        from unittest.mock import patch

        from tally_connector.utils import connection

        seen = []
        with patch.object(connection, "push_to_tally",
                          return_value={"success": True, "reached": True,
                                        "created": 1, "altered": 0,
                                        "unknown": False, "errors": []}), \
             patch("frappe.db.commit", side_effect=lambda: seen.append(1)):
            connection.push_vouchers(
                "Tally Co", [self._voucher("SINV-COMMIT-1")], batch=50,
                erpnext_company=frappe.db.get_value("Company", {}, "name"))
        self.assertTrue(seen, "the batch was recorded and never committed")

    def _voucher(self, number):
        import datetime
        from decimal import Decimal

        from tally_connector.tally import model

        v = model.Voucher(kind="Sales", date=datetime.date(2026, 4, 8),
                          number=number, party="Acme", remote_id=number)
        v.lines.append(model.LedgerLine(ledger="Acme", amount=Decimal("-1"),
                                        is_party=True))
        v.lines.append(model.LedgerLine(ledger="Sales", amount=Decimal("1")))
        return v


class TestAPendingVoucherIsNeverResent(FrappeTestCase):
    """191 purchase vouchers were posted two and three times into a
    customer's books, and this is the rule that makes that impossible.

    The ledger was written only after a reply. So a run that sent a batch and
    never learnt its fate left no rows, the next run saw nothing recorded, and
    sent them again - and because this connector cannot delete from Tally,
    there was no undoing it.

    Two changes, and they only work together: the attempt is recorded before
    the request leaves, and `Pending` is not resendable. Either alone leaves
    the hole open.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = frappe.db.get_value("Company", {}, "name")

    def _voucher(self, number):
        import datetime
        from decimal import Decimal

        from tally_connector.tally import model

        v = model.Voucher(kind="Sales", date=datetime.date(2026, 4, 8),
                          number=number, party="Acme", remote_id=number)
        v.lines.append(model.LedgerLine(ledger="Acme", amount=Decimal("-1"),
                                        is_party=True))
        v.lines.append(model.LedgerLine(ledger="Sales", amount=Decimal("1")))
        return v

    def test_pending_is_not_in_the_resendable_set(self):
        from tally_connector.utils import sync_ledger

        self.assertNotIn(sync_ledger.PENDING, sync_ledger.RESENDABLE)
        self.assertIn(sync_ledger.PENDING, sync_ledger.NEEDS_A_PERSON)

    def test_a_voucher_recorded_pending_is_not_offered_again(self):
        from tally_connector.utils.sync_ledger import note_pending, unsent

        v = self._voucher("SINV-PEND-1")
        note_pending([v], self.company, "Tally Co")
        todo, skipped = unsent([v], self.company, "Tally Co")
        self.assertEqual(todo, [], "a voucher already in flight was offered again")
        self.assertEqual(len(skipped), 1)

    def test_the_attempt_is_recorded_before_the_push(self):
        """Not after. A row written only on a reply never exists for the
        request that got no reply, which is the case that needs one."""
        from unittest.mock import patch

        from tally_connector.utils import connection

        order = []
        with patch.object(connection, "_note_pending",
                          side_effect=lambda *a, **k: order.append("pending")), \
             patch.object(connection, "push_to_tally",
                          side_effect=lambda *a, **k: order.append("push") or {
                              "success": True, "reached": True, "created": 1,
                              "altered": 0, "unknown": False, "errors": []}), \
             patch.object(connection, "_record_batch",
                          side_effect=lambda *a, **k: order.append("record")):
            connection.push_vouchers("Tally Co", [self._voucher("SINV-ORD-1")],
                                     batch=10, erpnext_company=self.company)
        self.assertEqual(order, ["pending", "push", "record"])

    def test_a_failed_voucher_is_still_offered_again(self):
        """The recoverable half has to keep working: a genuine refusal is
        meant to be retried once whatever caused it is fixed."""
        from tally_connector.utils.sync_ledger import (
            FAILED, note_attempt, note_outcome, unsent)

        v = self._voucher("SINV-FAIL-1")
        row = note_attempt("Sales Invoice", "SINV-FAIL-1", self.company,
                           "Tally Co")
        note_outcome(row, FAILED, "refused")
        todo, _skipped = unsent([v], self.company, "Tally Co")
        self.assertEqual([x.number for x in todo], ["SINV-FAIL-1"])
