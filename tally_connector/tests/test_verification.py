"""
What ERPNext sent, what Tally holds, and which documents are missing by name.

Tally's answer to an import says how many vouchers it created and, when it
feels like it, a reason or two - but never **which** ones failed. On a real run
against a live TallyPrime it reported `Created: 179, Errors: 108` and named
none of the 108. The only way to find them was a trial balance, which is the
worst way to learn that a third of a sync did not land.

So the connector asks Tally afterwards what is actually in its Day Book and
lines that up against what the export would send. The answer is a list of
ERPNext document names.

**One limit, and it is Tally's.** A voucher type set to automatic numbering
makes Tally assign its own number and discard ours, and such a voucher cannot
be matched back by number. Those are reported separately rather than counted as
missing, because calling them missing sends somebody looking for vouchers that
are sitting there under another name.

Pure where it can be; the endpoint test replaces the transport.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from tally_connector.utils.connection import TallyConnector

DAYBOOK = """<ENVELOPE><BODY><DATA><COLLECTION>
 <VOUCHER VCHTYPE="Sales">
  <DATE>20260408</DATE><VOUCHERNUMBER>SINV-0001</VOUCHERNUMBER>
 </VOUCHER>
 <VOUCHER VCHTYPE="Receipt">
  <DATE>20260409</DATE><VOUCHERNUMBER>1</VOUCHERNUMBER>
 </VOUCHER>
</COLLECTION></DATA></BODY></ENVELOPE>"""

# What Tally answers a request it does not recognise. It is a well-formed
# envelope about the company and holds no collection at all, which is what the
# old Day Book request got back every single time.
NOT_A_COLLECTION = """<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS>
</HEADER><BODY><IMPORTDATA><REQUESTDESC><REPORTNAME>All Masters</REPORTNAME>
</REQUESTDESC><REQUESTDATA><TALLYMESSAGE><COMPANY><REMOTECMPINFO.LIST>
<NAME>030d486c</NAME><REMOTECMPNAME>Demo Traders Pvt Ltd</REMOTECMPNAME>
</REMOTECMPINFO.LIST></COMPANY></TALLYMESSAGE></REQUESTDATA></IMPORTDATA>
</BODY></ENVELOPE>"""

# Tally puts one empty voucher in every collection, including over a period
# with nothing in it.
EMPTY_PERIOD = """<ENVELOPE><BODY><DATA><COLLECTION>
 <VOUCHER><GUID></GUID></VOUCHER>
</COLLECTION></DATA></BODY></ENVELOPE>"""


class TestReadingWhatTallyHolds(IntegrationTestCase):

    def _read(self, answer, ok=True):
        conn = TallyConnector()
        with patch.object(conn, "_post_xml", return_value=(ok, answer)):
            return conn.vouchers_in_period("2026-04-01", "2026-08-31")

    def test_the_day_book_is_read_into_rows(self):
        rows = self._read(DAYBOOK)
        self.assertEqual([r["number"] for r in rows], ["SINV-0001", "1"])
        self.assertEqual(rows[0]["kind"], "Sales")
        self.assertEqual(rows[0]["date"], "20260408")

    def test_a_tally_that_did_not_answer_is_unknown_and_not_empty(self):
        """`Tally has nothing` and `Tally did not say` lead to opposite
        conclusions, so they must not come back as the same value."""
        self.assertIsNone(self._read("", ok=False))
        self.assertIsNone(self._read("<RESPONSE><LINEERROR>no such report"
                                     "</LINEERROR></RESPONSE>"))

    def test_an_empty_collection_is_empty_and_not_unknown(self):
        """Tally answered, and what it said was `nothing here`. That is a
        fact, and must not read as a failed probe."""
        rows = self._read("<ENVELOPE><BODY><DATA><COLLECTION/>"
                          "</DATA></BODY></ENVELOPE>")
        self.assertEqual(rows, [])

    def test_an_envelope_with_no_collection_is_unknown(self):
        """Tally did not answer the question that was asked. Reading it as an
        empty company is what made every sent document look missing."""
        self.assertIsNone(self._read("<ENVELOPE><BODY><DATA/></BODY></ENVELOPE>"))

    def test_an_unreadable_answer_is_unknown(self):
        self.assertIsNone(self._read("<html>500</html>" * 3))

    def test_the_answer_tally_gives_a_request_it_does_not_know_is_unknown(self):
        """The defect this cost. The old request asked for a "Day Book"
        report, Tally answered with a company-info envelope, and that parsed
        cleanly and held no VOUCHER - so it read as *this company holds no
        vouchers at all*. Every document sent would have been reported
        missing. It has to be unknown, because Tally never answered the
        question."""
        self.assertIsNone(self._read(NOT_A_COLLECTION))

    def test_tallys_own_empty_voucher_is_not_counted_as_one(self):
        """A collection over a period with nothing in it still carries one
        blank voucher. Counting it turns an empty company into one holding a
        document, which is the same error in the other direction."""
        self.assertEqual(self._read(EMPTY_PERIOD), [])


class TestNamingWhatIsMissing(IntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = frappe.db.get_value("Company", {}, "name")

    def _verify(self, held):
        from tally_connector.api import export as api

        with patch.object(TallyConnector, "vouchers_in_period",
                          return_value=held):
            return api.verify_in_tally("2026-04-01", "2026-08-31", self.company)

    def test_nothing_in_tally_means_everything_is_missing(self):
        out = self._verify([])
        self.assertTrue(out["success"])
        self.assertEqual(out["missing"], out["sent"])
        self.assertTrue(out["rows"], "no missing document was named")
        self.assertIn("document", out["rows"][0])

    def test_a_voucher_tally_holds_is_not_reported_missing(self):
        from tally_connector.tally.extract import extract

        export = extract(self.company, "2026-04-01", "2026-08-31")
        if not export.vouchers:
            self.skipTest("This company has no vouchers in the period.")
        first = export.vouchers[0].number
        out = self._verify([{"number": first, "kind": "Sales", "date": ""}])
        named = {r["voucher_number"] for r in out["rows"]}
        self.assertNotIn(first, named)
        self.assertEqual(out["missing"], out["sent"] - 1)

    def test_a_number_tally_assigned_itself_is_not_called_missing(self):
        """It is sitting there under another name, and sending somebody to
        look for it by ours wastes their afternoon."""
        out = self._verify([{"number": "1", "kind": "Receipt", "date": ""},
                            {"number": "2", "kind": "Receipt", "date": ""}])
        self.assertEqual(out["auto_numbered_in_tally"], 2)

    def test_a_tally_that_did_not_answer_reports_nothing_missing(self):
        """Reporting 931 missing documents because Tally was switched off is
        worse than reporting nothing at all."""
        out = self._verify(None)
        self.assertFalse(out["success"])
        self.assertIn("unknown", out["error"])
        self.assertNotIn("missing", out)


class TestNarrationFitsWhatTallyHolds(UnitTestCase):
    """Measured off TallyPrime's own export of a real company: 514 narrations,
    the longest 120 characters and the median also 120, which is what
    truncation looks like from outside. This connector was sending up to 1,456,
    and a live Tally answered `Software Exception c0000005 (Memory Access
    Violation)` while importing vouchers."""

    def test_a_long_narration_is_cut_to_what_tally_takes(self):
        from tally_connector.tally.render import NARRATION_MAX, _narration

        cut = _narration("x" * 500)
        self.assertEqual(len(cut), NARRATION_MAX)
        self.assertTrue(cut.endswith("..."))

    def test_a_short_narration_is_left_alone(self):
        from tally_connector.tally.render import _narration

        self.assertEqual(_narration("Paid in full"), "Paid in full")

    def test_the_cut_is_visible_rather_than_silent(self):
        """Text that stops mid-sentence with nothing marking it is a loss
        nobody finds."""
        from tally_connector.tally.render import _narration

        self.assertIn("...", _narration("y" * 300))

    def test_the_validator_says_which_vouchers_lost_text(self):
        import datetime
        from decimal import Decimal

        from tally_connector.tally import model
        from tally_connector.tally.validate import validate

        export = model.Export(company="Co")
        voucher = model.Voucher(kind="Journal", date=datetime.date(2026, 4, 8),
                                number="JV-1", narration="z" * 400)
        voucher.lines.append(model.LedgerLine(ledger="A", amount=Decimal("-1")))
        voucher.lines.append(model.LedgerLine(ledger="B", amount=Decimal("1")))
        export.vouchers.append(voucher)
        hits = [f for f in validate(export).findings if f.check == "narration"]
        self.assertTrue(hits)
        self.assertIn("400", hits[0].detail)
