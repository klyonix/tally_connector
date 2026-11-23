"""
The export must not make queries per document.

Every voucher reader used to call `frappe.get_doc` once per document purely to
reach its child tables, and the invoice readers looked up the party's address
once per document on top. Measured on a small bench that was 18.5
database round trips per sales invoice and 12 per purchase invoice.

At a hundred documents nobody notices. At eleven thousand - a year of a real
ledger - it is a 504, and far worse in production than a local measurement
suggests, because there the database is across a network rather than on the same
machine.

So the property under test is not "it is fast", which depends on the machine and
on how much data this bench happens to hold. It is that the number of queries
does not grow with the number of documents. A ceiling that a per-document
regression blows through immediately is a better guard than a timing assertion
that goes flaky on a loaded laptop.
"""

import frappe
from frappe.tests import IntegrationTestCase

from tally_connector.tally import extract as ex
from tally_connector.utils.exporter import render_section

# The ceiling is fixed cost plus a small allowance per document, not a flat
# number: a generator legitimately makes a handful of setup queries whatever the
# volume, and on a company holding five rows a flat ceiling says nothing. The
# allowance is far below the 18.5 and 12 per document this replaced, so a
# reintroduced per-row read fails immediately at any realistic size.
FIXED_ALLOWANCE = 30
PER_DOCUMENT_ALLOWANCE = 0.5

COUNTED = ("sql", "sql_list", "get_value", "get_values", "get_all", "get_list", "count")


class _CountingDB:
    """Counts database round trips without changing behaviour."""

    def __init__(self, db):
        self._db = db
        self.n = 0

    def __getattr__(self, name):
        attr = getattr(self._db, name)
        if name in COUNTED and callable(attr):
            def counted(*args, **kwargs):
                self.n += 1
                return attr(*args, **kwargs)
            return counted
        return attr


def _companies():
    """Busiest first: a generator proves nothing on a company with no rows."""
    counts = {c: 0 for c in frappe.get_all("Company", pluck="name")}
    for doctype in ("Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry"):
        for company in counts:
            counts[company] += frappe.db.count(doctype, {"docstatus": 1, "company": company})
    return sorted(counts, key=counts.get, reverse=True)


class TestBulkReads(IntegrationTestCase):
    def _count(self, section, company, from_date=None, to_date=None):
        # Warm the metadata caches first. The first read in a process loads
        # DocType metadata - custom fields, property setters, the meta itself -
        # which is a couple of hundred queries attributed to whichever section
        # happened to run first. Measured cold, 90 documents look like 137
        # queries and 1 document like 68, which reads exactly like a per-row
        # read and is nothing of the kind.
        render_section(section, from_date, to_date, company)
        real = frappe.local.db
        counter = _CountingDB(real)
        frappe.local.db = counter
        try:
            _xml, n = render_section(section, from_date, to_date, company)
        finally:
            frappe.local.db = real
        return counter.n, n

    def test_no_section_queries_per_document(self):
        for section in ("sales_invoices", "purchase_invoices", "payment_in",
                        "payment_out", "journal_entries", "bank_transactions"):
            with self.subTest(section=section):
                queries = records = 0
                for company in _companies():
                    queries, records = self._count(section, company)
                    if records:
                        break
                if not records:
                    self.skipTest(f"No company on this site has rows for {section}.")
                ceiling = FIXED_ALLOWANCE + PER_DOCUMENT_ALLOWANCE * records
                self.assertLessEqual(
                    queries, ceiling,
                    f"{section} made {queries} queries for {records} documents "
                    f"({queries / records:.1f} each, ceiling {ceiling:.0f}). The query "
                    "count must not grow with the number of documents - something is "
                    "reading per row again.",
                )

    def test_the_count_does_not_grow_with_the_number_of_documents(self):
        """The property stated directly: more documents, the same queries.

        A ceiling alone can be satisfied by a bench that happens to hold little
        data. This compares two ranges of the same generator, which is what "does
        not scale" actually means.
        """
        company = _companies()[0]
        rows = frappe.get_all(
            "Sales Invoice", filters={"docstatus": 1, "company": company},
            fields=["posting_date"], order_by="posting_date asc",
        )
        if len(rows) < 4:
            self.skipTest("Too few sales invoices here to compare two ranges.")
        first = str(rows[0].posting_date)
        mid = str(rows[len(rows) // 2].posting_date)
        last = str(rows[-1].posting_date)

        small_q, small_n = self._count(
            "sales_invoices", company, from_date=first, to_date=mid)
        big_q, big_n = self._count(
            "sales_invoices", company, from_date=first, to_date=last)

        self.assertGreater(big_n, small_n, "Both ranges hold the same documents.")
        self.assertLessEqual(
            big_q, small_q + PER_DOCUMENT_ALLOWANCE * (big_n - small_n),
            f"{small_n} documents took {small_q} queries and {big_n} took {big_q}. "
            "The query count is growing with the data.",
        )

    def test_the_counter_actually_counts(self):
        """A ceiling test is worthless if the counter is blind."""
        real = frappe.local.db
        counter = _CountingDB(real)
        frappe.local.db = counter
        try:
            frappe.db.get_value("Company", _companies()[0], "name")
            frappe.db.count("Company")
        finally:
            frappe.local.db = real
        self.assertGreaterEqual(counter.n, 2)

    def test_the_address_chosen_matches_the_lookup_it_replaced(self):
        """Bulk-reading addresses must not change which one a party gets.

        `frappe.db.get_value` defaults to `order_by="creation"` and Frappe's
        query builder reads a bare column as DESC, so it returns the party's
        newest address. Getting this backwards changed the state, the GSTIN and
        the mailing address on real invoices - caught by diffing the generated
        XML, which is why that diff is part of the routine now.
        """
        parties = frappe.db.sql(
            """select link_name from `tabDynamic Link`
               where link_doctype = 'Customer' and parenttype = 'Address'
               group by link_name having count(*) > 1 limit 5""",
            pluck=True,
        )
        if not parties:
            self.skipTest("No customer on this site has more than one address.")
        cache = ex._addresses_for("Customer", parties)
        for party in parties:
            with self.subTest(party=party):
                expected_name = frappe.db.get_value(
                    "Dynamic Link",
                    {"link_doctype": "Customer", "link_name": party, "parenttype": "Address"},
                    "parent",
                )
                expected = frappe.get_cached_doc("Address", expected_name)
                got = cache.get(party) or {}
                self.assertEqual(got.get("city", ""), frappe.utils.cstr(expected.city or ""))
                self.assertEqual(got.get("pincode", ""), frappe.utils.cstr(expected.pincode or ""))

    def test_a_missing_column_is_dropped_rather_than_selected(self):
        """`add_deduct_tax` is on Purchase Taxes and Charges and not on the Sales
        one. `doc.get()` answered None for it; naming it in a SELECT is a hard
        SQL error, which is how the first version of this failed."""
        rows = ex._children_by_parent(
            "Sales Taxes and Charges",
            frappe.get_all("Sales Invoice", filters={"docstatus": 1}, limit=5, pluck="name"),
            ["account_head", "add_deduct_tax", "not_a_real_column_at_all"],
        )
        for lines in rows.values():
            for line in lines:
                self.assertIsNone(line.get("not_a_real_column_at_all"))
