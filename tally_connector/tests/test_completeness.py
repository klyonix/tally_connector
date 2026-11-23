"""Every ERPNext document that posts to the ledger reaches Tally.

A full year went to Tally, and every ledger in Tally matched what had been
sent, to the paisa. The year's profit was still wrong: Expense Claims, Stock
Entries and Purchase Receipts post to the ledger and no reader read them. Comparing Tally
with what was sent cannot see what was never sent, so the comparison has to
start from ERPNext's own ledger.
"""

import datetime
from decimal import Decimal

import frappe
from frappe.tests import IntegrationTestCase

from tally_connector.tally.extract import GL_DOCUMENT_TYPES, _gl_documents
from tally_connector.utils.ledgers import ledger_name
from tally_connector.utils.sync_ledger import _doctype_for

# Read by a reader of their own.
READERS = {"Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry"}

# Posts to the ledger and is deliberately not sent, with the reason.
NOT_SENT = {
    "Period Closing Voucher":
        "Tally closes its own year; sending ERPNext's closing entry as well "
        "would close it twice.",
}


class TestEveryPostingDocumentIsRead(IntegrationTestCase):

    def test_every_document_type_that_posts_is_read(self):
        """A new kind of posting document - payroll, assets, an exchange
        revaluation - fails here the day it first posts, instead of being found
        as a wrong profit in somebody's Tally."""
        posting = set(frappe.get_all("GL Entry", filters={"is_cancelled": 0},
                                     pluck="voucher_type", distinct=True))
        read = READERS | set(GL_DOCUMENT_TYPES) | set(NOT_SENT)
        self.assertEqual(
            sorted(posting - read), [],
            "These post to ERPNext's ledger and nothing sends them to Tally.")


class TestOtherDocumentsCarryTheirOwnLedger(IntegrationTestCase):

    def setUp(self):
        super().setUp()
        row = frappe.db.sql("""
            SELECT company FROM `tabGL Entry`
             WHERE is_cancelled = 0 AND voucher_type IN %s
             LIMIT 1""", (GL_DOCUMENT_TYPES,))
        if not row:
            self.skipTest("no expense claim, stock entry or purchase receipt here")
        self.company = row[0][0]
        self.vouchers = _gl_documents(self.company, None, None)

    def test_one_voucher_per_document(self):
        documents = frappe.db.sql("""
            SELECT COUNT(DISTINCT voucher_no) FROM `tabGL Entry`
             WHERE is_cancelled = 0 AND company = %s AND voucher_type IN %s""",
                                  (self.company, GL_DOCUMENT_TYPES))[0][0]
        self.assertEqual(len(self.vouchers), documents)

    def test_each_balances(self):
        self.assertEqual([v.number for v in self.vouchers if v.imbalance() != 0], [])

    def test_the_profit_effect_is_erpnexts(self):
        """Against the GL's own income and expense, not against the reader's
        own arithmetic, so a ledger mapped to the wrong account shows."""
        expected = frappe.db.sql("""
            SELECT COALESCE(SUM(g.credit - g.debit), 0)
              FROM `tabGL Entry` g JOIN `tabAccount` a ON a.name = g.account
             WHERE g.is_cancelled = 0 AND g.company = %s
               AND g.voucher_type IN %s
               AND a.root_type IN ('Income', 'Expense')""",
                                 (self.company, GL_DOCUMENT_TYPES))[0][0]
        roots = {ledger_name(a.name, self.company): a.root_type
                 for a in frappe.get_all("Account", filters={"company": self.company},
                                         fields=["name", "root_type"])}
        got = sum((Decimal(str(l.amount)) for v in self.vouchers for l in v.lines
                   if roots.get(l.ledger) in ("Income", "Expense")), Decimal("0"))
        self.assertEqual(got, Decimal(str(expected)).quantize(Decimal("0.01")))

    def test_each_names_its_document(self):
        """The voucher number is Tally's own where numbering is automatic, so
        the narration is what ties a journal back to its document."""
        for v in self.vouchers[:50]:
            with self.subTest(number=v.number):
                self.assertTrue(v.narration.startswith(f"{v.source_doctype} {v.number}"))


class TestTheSyncLedgerRecordsTheRealDocument(IntegrationTestCase):

    def test_a_journal_from_an_expense_claim_is_recorded_as_one(self):
        from tally_connector.tally import model

        v = model.Voucher(kind="Journal", number="HR-EXP-2025-00001",
                          date=datetime.date(2025, 4, 1),
                          source_doctype="Expense Claim")
        self.assertEqual(_doctype_for(v), "Expense Claim")

    def test_a_journal_entry_is_still_a_journal_entry(self):
        from tally_connector.tally import model

        v = model.Voucher(kind="Journal", number="ACC-JV-1",
                          date=datetime.date(2025, 4, 1))
        self.assertEqual(_doctype_for(v), "Journal Entry")


class TestABankLineAlreadyBookedIsNotSentAgain(IntegrationTestCase):
    """A reconciled statement line is in the books through its payment or
    journal. Sending it as well moved the bank twice in Tally."""

    def test_only_the_unallocated_part_is_sent(self):
        from tally_connector.tally.extract import _bank_transactions

        rows = frappe.get_all("Bank Transaction", filters={"docstatus": 1},
                              fields=["company", "unallocated_amount"],
                              limit_page_length=0)
        if not rows:
            self.skipTest("no bank transactions here")
        for company in {r.company for r in rows}:
            expected = sum(flt_(r.unallocated_amount) for r in rows
                           if r.company == company and flt_(r.unallocated_amount) > 0)
            sent = sum(abs(Decimal(str(l.amount)))
                       for v in _bank_transactions(company, None, None)
                       for l in v.lines if l.amount > 0)
            with self.subTest(company=company):
                self.assertEqual(sent, Decimal(str(expected)).quantize(Decimal("0.01")))


def flt_(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))
