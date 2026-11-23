"""
Only a submitted document goes to Tally, and a withdrawn one is reported.

Two halves of one rule: only a submitted record moves to Tally.

**Nothing but `docstatus = 1` is ever sent.** A draft is not accounting yet and
a cancelled document is accounting withdrawn; neither belongs in somebody's
books. This was already true of all six voucher sources and nothing asserted
it, so it is asserted here.

**A document already in Tally that is later withdrawn is reported, not
cancelled.** A file that cancelled such vouchers was built and removed the same
day: it stops accounting somebody may be relying on, and nothing
here has been proved against a real Tally. What remains is the knowing - the
connector says Tally holds a voucher ERPNext no longer stands behind, and a
person settles it in Tally where they can see what it affects. Before this,
nothing said anything at all and a trial balance was how you found out.

Two ways a document stops being current, and ERPNext spells them differently.

**Cancelled** is `docstatus = 2`: the document remains and its accounting is
withdrawn.

**Amended** is a *different document*. ERPNext cancels the original and creates
a successor naming it in `amended_from`, so the original is cancelled too and
the successor has a name of its own - and therefore a GUID of its own. The two
are told apart because they need different words: "cancelled" and "amended, now
SINV-0002" are different things to read in a report.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from tally_connector.utils import sync_ledger as ledger

TALLY = "Demo Traders Pvt Ltd"


def _sent(doctype, name, company, voucher_number=None):
    row = ledger.note_attempt(doctype, name, company, TALLY,
                              voucher_type="Sales",
                              voucher_number=voucher_number or name)
    ledger.note_outcome(row, ledger.CREATED)
    return row


def _a_sellable_item():
    """An item this site will actually accept on a Sales Invoice.

    `{"is_stock_item": 0}` on its own takes whatever non-stock item the site
    holds first, and on this bench that is one with *Is Sales Item* off, which
    ERPNext refuses outright: "not marked as sales item". Nine tests in this
    file failed on site data rather than on the code, and on a bench whose
    first item happened to be sellable they would have passed.

    It asks for a sellable one and builds one when there is none, so a clean
    bench and a bench with years of real items both work.
    """
    name = frappe.db.get_value(
        "Item", {"is_stock_item": 0, "is_sales_item": 1, "disabled": 0}, "name")
    if name:
        return name
    item = frappe.new_doc("Item")
    item.item_code = item.item_name = "Tally Connector Test Service"
    item.item_group = (frappe.db.get_value("Item Group", {"is_group": 0}, "name")
                       or frappe.db.get_value("Item Group", {}, "name"))
    item.stock_uom = frappe.db.get_value("UOM", {}, "name") or "Nos"
    item.is_stock_item = 0
    item.is_sales_item = 1
    item.flags.ignore_permissions = True
    item.flags.ignore_mandatory = True
    item.insert()
    return item.name


def _a_customer():
    """An enabled customer. A disabled one is refused on submit."""
    return (frappe.db.get_value("Customer", {"disabled": 0}, "name")
            or frappe.db.get_value("Customer", {}, "name"))


class TestFindingWhatTallyStillHolds(FrappeTestCase):

    def setUp(self):
        self.company = frappe.db.get_value("Company", {}, "name")
        frappe.db.delete(ledger.DOCTYPE, {"tally_company": TALLY})

    def _invoice(self, submit=True, cancel=False):
        """A real Sales Invoice, because the detection reads `docstatus` and a
        stand-in cannot have one."""
        item = _a_sellable_item()
        customer = _a_customer()
        doc = frappe.new_doc("Sales Invoice")
        doc.company = self.company
        doc.customer = customer
        doc.due_date = frappe.utils.add_days(frappe.utils.today(), 30)
        doc.append("items", {"item_code": item, "qty": 1, "rate": 100})
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.insert()
        if submit:
            doc.submit()
        if cancel:
            doc.cancel()
        return doc

    def test_a_live_document_is_not_stale(self):
        doc = self._invoice()
        _sent("Sales Invoice", doc.name, self.company)
        self.assertEqual(ledger.stale(self.company, TALLY), [])

    def test_a_cancelled_document_is_found(self):
        doc = self._invoice(cancel=True)
        _sent("Sales Invoice", doc.name, self.company)
        rows = ledger.stale(self.company, TALLY)
        self.assertEqual([r[0].source_name for r in rows], [doc.name])
        self.assertEqual(rows[0][1], "cancelled")

    def test_an_amended_document_is_found_and_names_its_successor(self):
        doc = self._invoice(cancel=True)
        _sent("Sales Invoice", doc.name, self.company)
        amended = frappe.copy_doc(doc)
        amended.amended_from = doc.name
        amended.docstatus = 0
        amended.flags.ignore_permissions = True
        amended.insert()

        rows = ledger.stale(self.company, TALLY)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "amended")
        self.assertEqual(rows[0][2], amended.name)

    def test_a_document_that_was_never_sent_is_not_stale(self):
        self._invoice(cancel=True)
        self.assertEqual(ledger.stale(self.company, TALLY), [])

    def test_detection_records_the_disagreement_on_the_record(self):
        """So it shows in a list somebody already reads, not only in a report
        they have to ask for."""
        doc = self._invoice(cancel=True)
        row = _sent("Sales Invoice", doc.name, self.company)
        ledger.mark_stale(ledger.stale(self.company, TALLY))
        row.reload()
        self.assertEqual(row.outcome, ledger.STALE)
        self.assertIn("still holds", row.message)

    def test_marking_twice_changes_nothing(self):
        doc = self._invoice(cancel=True)
        _sent("Sales Invoice", doc.name, self.company)
        rows = ledger.stale(self.company, TALLY)
        self.assertEqual(ledger.mark_stale(rows), 1)
        self.assertEqual(ledger.mark_stale(ledger.stale(self.company, TALLY)), 0)

    def test_a_stale_voucher_is_still_counted_as_in_tally(self):
        """It has not gone anywhere. Dropping it out of `IN_TALLY` would make
        the next export send a second copy of a voucher Tally already has."""
        self.assertIn(ledger.STALE, ledger.IN_TALLY)


class TestOnlySubmittedRecordsAreSent(FrappeTestCase):
    """Every voucher source filters on `docstatus = 1`. That was already true
    and nothing asserted it, so a source added later could quietly send drafts.

    Asserted twice over: once by reading the sources, so a new one is caught
    the day it is written, and once by building a draft and a cancelled invoice
    and checking neither reaches the export."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = frappe.db.get_value("Company", {}, "name")

    def test_every_voucher_source_filters_on_submitted(self):
        import re

        from tally_connector.tally import extract as module

        source = open(module.__file__).read()
        filters = re.findall(r"_date_filter\(\{([^}]*)\}", source)
        self.assertTrue(filters, "no voucher source filters were found")
        for text in filters:
            with self.subTest(filters=text.strip()[:60]):
                self.assertIn('"docstatus": 1', text)

    def _invoice(self, submit=True, cancel=False):
        item = _a_sellable_item()
        doc = frappe.new_doc("Sales Invoice")
        doc.company = self.company
        doc.customer = _a_customer()
        doc.due_date = frappe.utils.add_days(frappe.utils.today(), 30)
        doc.append("items", {"item_code": item, "qty": 1, "rate": 100})
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.insert()
        if submit:
            doc.submit()
        if cancel:
            doc.cancel()
        return doc

    def _exported(self, day):
        from tally_connector.tally.extract import extract_vouchers

        return {v.remote_id for v in
                extract_vouchers("sales_invoices", self.company, day, day)}

    def test_a_draft_is_not_exported(self):
        doc = self._invoice(submit=False)
        self.assertNotIn(doc.name, self._exported(doc.posting_date))

    def test_a_submitted_invoice_is_exported(self):
        """The control. Without it the other two prove only that nothing is."""
        doc = self._invoice()
        self.assertIn(doc.name, self._exported(doc.posting_date))

    def test_a_cancelled_invoice_is_not_exported(self):
        doc = self._invoice(cancel=True)
        self.assertNotIn(doc.name, self._exported(doc.posting_date))


class TestNothingCancelsInTally(FrappeTestCase):
    """Removed the day it was built. The rule it
    leaves behind: this connector adds accounting to Tally and never withdraws
    it, because withdrawing stops something somebody may be relying on and a
    person doing it in Tally can see what that is."""

    def test_the_voucher_actions_are_create_and_alter_only(self):
        from tally_connector.tally import render

        self.assertEqual(set(render.VOUCHER_ACTIONS), {"Create", "Alter"})

    def test_no_cancellation_file_can_be_rendered(self):
        from tally_connector.tally import render

        self.assertFalse(hasattr(render, "render_cancellations"))

    def test_no_endpoint_cancels_anything(self):
        from tally_connector.api import export

        self.assertFalse(hasattr(export, "cancel_stale_in_tally"))

    def test_the_outcome_vocabulary_has_no_cancelled(self):
        self.assertNotIn("Cancelled", ledger.OUTCOMES)
        options = frappe.get_meta(ledger.DOCTYPE).get_field("outcome").options
        self.assertNotIn("Cancelled", options.split("\n"))

    def test_reporting_it_still_works(self):
        """Removing the acting must not remove the knowing."""
        from tally_connector.api.export import stale_in_tally

        out = stale_in_tally()
        self.assertTrue(out["success"])
        self.assertIn("count", out)
