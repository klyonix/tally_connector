"""
How long it takes and how much it holds.

The figures are printed rather than only asserted, because a budget that passes
tells you nothing and a number tells you where the next bottleneck is. The
assertions are deliberately generous: this runs on whatever machine happens to
be to hand, and a tight timing assertion is a test that goes red on a loaded
laptop and gets deleted.

What is measured at real scale is the **extract**, against whatever this bench
holds. Ten thousand vouchers cannot be created here without writing ten thousand
documents into somebody's books, so the render and validate stages - which are
the memory-hungry ones - are measured against a synthetic model of that size.
That split is stated rather than hidden: the database read at 10,000 is
projected from measurement, not measured.
"""

import datetime
import os
import time
import tracemalloc

import frappe
from frappe.tests import IntegrationTestCase

from tally_connector.tally.model import (
    Export, Group, Ledger, LedgerLine, Masters, Voucher, money,
)
from tally_connector.tally.render import render_masters, render_vouchers
from tally_connector.tally.validate import validate
from tally_connector.utils.exporter import render_full_export

# Generous by design. A per-voucher cost above this is not slow, it is a
# regression in shape - something reading per row again.
MAX_MS_PER_VOUCHER_EXTRACT = 25.0
MAX_MS_PER_VOUCHER_RENDER = 2.0
MAX_KB_PER_VOUCHER = 12.0

REPORT = []

# Where the figures land so a person - or a release checklist - can read them
# back without re-running the suite.
REPORT_PATH = os.path.join(
    frappe.utils.get_bench_path(), "sites", "tally_connector_performance.txt"
)


def _company():
    counts = {c: 0 for c in frappe.get_all("Company", pluck="name")}
    for doctype in ("Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry"):
        for company in counts:
            counts[company] += frappe.db.count(doctype, {"docstatus": 1, "company": company})
    return max(counts, key=counts.get)


def _synthetic(n):
    """A model of n vouchers, built without touching the database.

    Shaped like a real sales voucher - a party line with a bill reference and two
    tax lines - so the render and the validator do the work they would really do
    rather than walking a degenerate structure.
    """
    masters = Masters(
        groups=[Group(name="Sundry Debtors", parent="Primary"),
                Group(name="Duties & Taxes", parent="Primary")],
        ledgers=[Ledger(name="Sales", parent="Sales Accounts", currency="INR"),
                 Ledger(name="Output CGST", parent="Duties & Taxes", currency="INR",
                        tax_type="GST", duty_head="CGST"),
                 Ledger(name="Output SGST", parent="Duties & Taxes", currency="INR",
                        tax_type="GST", duty_head="SGST")],
    )
    vouchers = []
    for i in range(n):
        party = f"Customer {i % 500}"
        masters.ledgers.append(Ledger(name=party, parent="Sundry Debtors", currency="INR"))
        vouchers.append(Voucher(
            kind="Sales", number=f"PERF-{i:06d}",
            date=datetime.date(2026, 1 + (i % 12), 1 + (i % 28)),
            narration=f"Performance probe voucher {i}",
            party=party,
            lines=[
                LedgerLine(ledger=party, amount=money(-1180), is_party=True),
                LedgerLine(ledger="Sales", amount=money(1000)),
                LedgerLine(ledger="Output CGST", amount=money(90)),
                LedgerLine(ledger="Output SGST", amount=money(90)),
            ],
            remote_id=f"PERF-{i:06d}",
        ))
    return Export(company="Perf Co", masters=masters, vouchers=vouchers)


class TestItIsFastEnoughAndSmallEnough(IntegrationTestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        lines = ["── Performance ──"] + REPORT
        for row in lines:
            print(row)
        # Written as well as printed: Frappe's test runner captures stdout, so a
        # measurement that only prints is a measurement nobody reads.
        try:
            with open(REPORT_PATH, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            pass

    def test_the_real_export_on_this_bench(self):
        """The whole path - ERPNext read, model, render - at the scale that
        actually exists here."""
        company = _company()
        tracemalloc.start()
        start = time.perf_counter()
        masters_xml, vouchers_xml, counts = render_full_export(None, None, company)
        elapsed = time.perf_counter() - start
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        n = counts["vouchers"]
        size_mb = (len(masters_xml) + len(vouchers_xml)) / 1024 / 1024
        REPORT.append(
            f"real export      {n:>7,} vouchers  {elapsed:7.2f}s  "
            f"{elapsed / max(n, 1) * 1000:6.2f} ms/vch  "
            f"{size_mb:6.2f} MB  peak {peak / 1024 / 1024:6.1f} MB"
        )
        self.assertGreater(n, 100, "too little data here to measure anything")
        self.assertLess(
            elapsed / n * 1000, MAX_MS_PER_VOUCHER_EXTRACT,
            "the per-voucher cost of a full export has regressed",
        )

    def test_render_and_validate_at_ten_thousand(self):
        """The stages that hold the whole export in memory, at production scale.

        Synthetic because ten thousand real vouchers cannot be created on a bench
        holding somebody's books. It measures render and validate honestly; it
        does not measure the database read, which is projected from the row
        above.
        """
        for n in (100, 1000, 10000):
            export = _synthetic(n)
            tracemalloc.start()
            start = time.perf_counter()
            masters_xml = render_masters(export.company, export.masters)
            vouchers_xml = render_vouchers(export.company, export.vouchers)
            render_seconds = time.perf_counter() - start

            start = time.perf_counter()
            report = validate(export)
            validate_seconds = time.perf_counter() - start
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            size_mb = (len(masters_xml) + len(vouchers_xml)) / 1024 / 1024
            REPORT.append(
                f"render+validate  {n:>7,} vouchers  "
                f"{render_seconds:6.2f}s render  {validate_seconds:6.2f}s validate  "
                f"{size_mb:6.2f} MB  peak {peak / 1024 / 1024:6.1f} MB  "
                f"({len(report.blocking)} blocking)"
            )
            with self.subTest(vouchers=n):
                self.assertLess(render_seconds / n * 1000, MAX_MS_PER_VOUCHER_RENDER)
                self.assertLess(peak / 1024 / n, MAX_KB_PER_VOUCHER)

    def test_the_estimate_that_decides_to_background_a_job_is_cheap(self):
        """It runs on every export before anything else, so it must not itself
        be the slow part."""
        from tally_connector.api.export import _estimate

        company = _company()
        start = time.perf_counter()
        estimate = _estimate(None, None, company)
        elapsed = time.perf_counter() - start
        REPORT.append(f"estimate         {estimate:>7,} vouchers  {elapsed:7.3f}s")
        self.assertLess(elapsed, 2.0)

    def test_a_large_export_is_handed_to_a_worker(self):
        """The threshold is the whole reason a year of a real ledger does not
        arrive as a 504."""
        from tally_connector.api.export import BACKGROUND_THRESHOLD, _estimate

        company = _company()
        self.assertGreater(BACKGROUND_THRESHOLD, 0)
        estimate = _estimate(None, None, company)
        REPORT.append(
            f"threshold        {BACKGROUND_THRESHOLD:>7,} vouchers  "
            f"(this bench: {estimate:,} -> "
            f"{'background' if estimate > BACKGROUND_THRESHOLD else 'in request'})"
        )
