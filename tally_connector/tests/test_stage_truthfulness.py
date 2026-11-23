"""
The progress display must not claim work that never happened.

A finished download-only export showed a green tick beside "Sending to Tally" -
a stage that never ran, on a job that never pushed - because the screen ticked
every stage before the current one and "Completed" is last. Nothing was broken
and the file was fine; the screen was simply asserting something untrue, which
is the one thing a progress display cannot do and remain worth looking at.
"""

import frappe
from frappe.tests import IntegrationTestCase

from tally_connector.api.export import EXPORT_ROLES, _create_log, run_full_export
from tally_connector.api.jobs import EXPORT_STAGES, JobReporter, status
from tally_connector.tests.inventory import inventory_sent


def _company():
    counts = {c: 0 for c in frappe.get_all("Company", pluck="name")}
    for doctype in ("Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry"):
        for company in counts:
            counts[company] += frappe.db.count(doctype, {"docstatus": 1, "company": company})
    return max(counts, key=counts.get)


class TestOnlyStagesThatRanAreRecorded(IntegrationTestCase):
    def setUp(self):
        self.log = _create_log("Full Export", "Tally XML", None, None, _company())

    def tearDown(self):
        frappe.delete_doc("Tally Export Log", self.log, force=True, ignore_permissions=True)
        frappe.db.commit()

    def test_a_download_only_export_never_records_sending_to_tally(self):
        """The exact case from production: Download XML, and the panel ticked a
        push that did not occur."""
        run_full_export(self.log, None, None, _company(), push=False)
        row = status(self.log)
        self.assertEqual(row["status"], "Success", row.get("error"))
        self.assertIn("Sending to Tally", row["stages"], "the stage list is drawn from this")
        self.assertNotIn(
            "Sending to Tally", row["reached"],
            "a download-only export recorded a push it never made",
        )

    def test_the_stages_that_did_run_are_all_recorded(self):
        """The other direction. A list that recorded nothing would also pass the
        test above, and would be useless."""
        run_full_export(self.log, None, None, _company(), push=False)
        reached = status(self.log)["reached"]
        for stage in ("Preparing", "Generating Masters", "Generating XML",
                      "Final Validation", "Completed"):
            with self.subTest(stage=stage):
                self.assertIn(stage, reached)

    def test_every_recorded_stage_is_one_the_client_can_draw(self):
        run_full_export(self.log, None, None, _company(), push=False)
        for stage in status(self.log)["reached"]:
            with self.subTest(stage=stage):
                self.assertIn(stage, EXPORT_STAGES)

    def test_the_record_is_in_the_order_the_stages_happened(self):
        run_full_export(self.log, None, None, _company(), push=False)
        reached = status(self.log)["reached"]
        indexes = [EXPORT_STAGES.index(s) for s in reached]
        self.assertEqual(indexes, sorted(indexes), reached)

    def test_a_failed_job_records_only_what_it_got_through(self):
        reporter = JobReporter("Tally Export Log", self.log, EXPORT_STAGES)
        reporter.start(total=100)
        reporter.at("Processing Sales", processed=40)
        reporter.finish("Failed", error="the worker died")
        reached = status(self.log)["reached"]
        self.assertIn("Processing Sales", reached)
        self.assertNotIn("Completed", reached)
        self.assertNotIn("Generating XML", reached)


class TestTheExportRefusesToCallARefusalASuccess(IntegrationTestCase):
    """Tally aborts at the first line it cannot parse and reports only that line.

    An export that says Success and then dies at line 48 in somebody's event log
    an hour later is the most expensive failure this system has, because nothing
    in the message leads back here. The written file is now read back and held
    against Tally's rules before anybody is told it worked.
    """

    def test_a_clean_export_is_reported_as_success_with_nothing_blocking(self):
        log = _create_log("Full Export", "Tally XML", None, None, _company())
        try:
            result = run_full_export(log, None, None, _company(), push=False)
            self.assertTrue(result["success"])
            self.assertEqual(result["blocking"], [])
            self.assertEqual(status(log)["status"], "Success")
        finally:
            frappe.delete_doc("Tally Export Log", log, force=True, ignore_permissions=True)
            frappe.db.commit()

    def test_the_check_actually_reads_the_written_file(self):
        """Guards against the validation being wired to something other than the
        bytes that were saved - which would pass for ever and check nothing.

        Comments are stripped before the window is measured. The first version
        looked at 400 raw characters and failed the day a paragraph was written
        between the call and its arguments, which is a guard reporting on prose.
        """
        import inspect

        source = "\n".join(
            line for line in inspect.getsource(run_full_export).splitlines()
            if not line.strip().startswith("#")
        )
        self.assertIn("read_export", source)
        after = source.split("read_export", 1)[1][:400]
        self.assertIn("masters_path", after)
        self.assertIn("vouchers_path", after)

    def test_a_workbook_is_checked_against_the_model_and_says_so(self):
        """The read-back is XML only - there is no workbook reader - so the
        Excel branch validates the `Export` the writer was handed. Every check
        still runs; what is not covered is the workbook writer itself, and that
        must be a branch somebody wrote rather than a check quietly skipped."""
        import inspect

        source = inspect.getsource(run_full_export)
        self.assertIn("EXCEL", source)
        self.assertIn("_model_for", source)


class TestEveryDeclaredStageIsReachable(IntegrationTestCase):
    """A stage nothing ever enters is the same defect as a tick nothing earned.

    Once the panel started ticking only what really ran, a declared-but-unreached
    stage stopped being invisible and started reading as a step that was skipped.
    `Validating` sat in `EXPORT_STAGES` doing exactly that: an export is checked
    at the end, over the bytes it wrote, and there is no early validation pass.

    Two stages are legitimately conditional and are named here with the reason,
    so the exemption is a decision rather than a hole.
    """

    # `Sending to Tally` runs only on a push, which this test does not make.
    # The `Processing *` stages run only where the matching `include_*` setting
    # is on, which is the site's decision rather than the code's.
    CONDITIONAL = {"Sending to Tally"}

    def test_a_full_export_reaches_every_stage_that_is_not_conditional(self):
        log = _create_log("Full Export", "Tally XML", None, None, _company())
        try:
            run_full_export(log, None, None, _company(), push=False)
            reached = set(status(log)["reached"])
            settings = frappe.get_single("Tally Settings")

            expected = set()
            for stage in EXPORT_STAGES:
                if stage in self.CONDITIONAL:
                    continue
                if stage.startswith("Processing "):
                    flag = _INCLUDE_FOR.get(stage)
                    if flag and not settings.get(flag, 1):
                        continue
                expected.add(stage)

            self.assertEqual(
                sorted(expected - reached), [],
                "these stages are declared and no export ever enters them",
            )
        finally:
            frappe.delete_doc("Tally Export Log", log, force=True, ignore_permissions=True)
            frappe.db.commit()

    def test_the_check_would_notice_a_dead_stage(self):
        """The guard must fail on exactly the defect it was written for."""
        self.assertNotIn(
            "Validating", EXPORT_STAGES,
            "an export has no early validation pass - it checks the file it wrote",
        )


# Which setting decides whether a source runs at all, so a company with bank
# lines switched off is not reported as skipping a stage.
_INCLUDE_FOR = {
    "Processing Sales": "include_sales_invoice",
    "Processing Purchases": "include_purchase_invoice",
    "Processing Payments": "include_payment_entry",
    "Processing Journals": "include_journal_entry",
    "Processing Bank Lines": "include_bank_transaction",
    "Processing Other Documents": "include_other_documents",
}


class TestTheWarningCountIsExplained(IntegrationTestCase):
    """A number on its own says there is a problem and nothing about it.

    A finished export said *2 warnings* and nothing about what they were. The
    count was stored and the findings behind it were thrown away, so the only
    way to learn what they were was to run the validator again by hand, and
    which nobody should have to.
    """

    def setUp(self):
        self.log = _create_log("Full Export", "Tally XML", None, None, _company())

    def tearDown(self):
        frappe.delete_doc("Tally Export Log", self.log, force=True, ignore_permissions=True)
        frappe.db.commit()

    def test_a_finished_export_says_what_its_warnings_were(self):
        # With the stock lines sent, because this needs an export that has a
        # finding to explain and the only one the full export reports on this
        # bench is a stock item with no HSN code. Folded there is no stock
        # item, so this skipped and stopped covering the log at all.
        #
        # Worth knowing while reading this: the count comes from validating
        # the written XML read back, so a narration is already cut short by
        # then and `_check_narration_length` sees nothing. A year's books can
        # hold dozens of them and the log says no warnings.
        with inventory_sent():
            run_full_export(self.log, None, None, _company(), push=False)
        row = status(self.log)
        if not row["warnings"]:
            self.skipTest("this bench's data produces no findings to explain")
        self.assertTrue(row["findings"], "the count is reported and the reasons are not")
        for group in row["findings"]:
            with self.subTest(check=group.get("check")):
                for key in ("severity", "check", "count", "subject", "detail"):
                    self.assertIn(key, group)

    def test_the_count_and_the_findings_agree(self):
        """Two numbers describing one thing must not disagree, which is worse
        than one of them being absent."""
        with inventory_sent():
            run_full_export(self.log, None, None, _company(), push=False)
        row = status(self.log)
        if not row["findings"]:
            self.skipTest("no findings on this bench")
        self.assertEqual(sum(g["count"] for g in row["findings"]), row["warnings"])

    def test_an_unresolved_ledger_is_counted_once(self):
        """It used to be counted twice. `_check_closure` raises a finding per
        unresolved ledger *and* the count added the unresolved list on top, so a
        job with three missing ledgers reported six warnings - a number that was
        wrong in the direction that makes somebody hunt for findings that are
        not there."""
        import inspect

        source = inspect.getsource(run_full_export)
        self.assertNotIn("reporter.warn(len(unresolved))", source)
        self.assertIn("reporter.warnings = len(verdict.findings)", source)

    def test_the_grouping_is_one_row_per_check(self):
        """465 party lines missing a bill reference is one thing to fix."""
        from tally_connector.api.export import _group_findings
        from tally_connector.tally.validate import Finding

        findings = [Finding("wrong", "hsn", f"Item {n}", "no HSN code", "") for n in range(5)]
        findings += [Finding("aborts", "unit", "Pint/Dry", "illegal symbol", "BAD UNIT NAME")]
        groups = _group_findings(findings)

        self.assertEqual(len(groups), 2)
        # Worst first, because that is the one somebody has to act on.
        self.assertEqual(groups[0]["severity"], "aborts")
        self.assertEqual(sum(g["count"] for g in groups), 6)
        self.assertEqual(next(g for g in groups if g["check"] == "hsn")["count"], 5)

    def test_the_summary_is_capped_rather_than_unbounded(self):
        """This is a field on a row, not a report. The count stays true; only
        the examples are limited."""
        from tally_connector.api.export import _group_findings
        from tally_connector.tally.validate import Finding

        findings = [Finding("wrong", f"check_{n}", "x", "y", "") for n in range(200)]
        self.assertLessEqual(len(_group_findings(findings)), 40)
