"""
The migration's acceptance test: the extract layer captures the same facts the
generators did.

Byte comparison was the plan and is the wrong instrument. The renderer
deliberately emits more than the generators - the invoice view, ISPARTYLEDGER,
unit decimal places, and the allocations that arrive in later stages - so
comparing bytes would only restate that the renderer is not the generator.

The comparison that means something is one level up: extract the model from
ERPNext, read a second model back out of the generators' XML, and assert they
agree on every fact the XML can carry. If they do, the swap changed how the
export is produced and not what it says. What the *rendered* XML adds is then a
short, enumerable list rather than an unexplained diff.

This found a real defect on its first run. A supplier whose name ended in a
trailing space was created verbatim by
the parties master while every voucher referenced the stripped form, so the
closure step created a second ledger and Tally would have carried one supplier
twice, invoices on one and payments on the other.
"""

import os
import tempfile

import frappe
from frappe.tests import IntegrationTestCase

from tally_connector.tally.extract import extract
from tally_connector.tally.model import AGAINST_REF, NEW_REF, ON_ACCOUNT, money
from tally_connector.tally.extract import _party_names
from tally_connector.tally.read import read_export


def _companies():
    return frappe.get_all("Company", pluck="name")


def _key(voucher):
    return voucher.remote_id or (voucher.kind, voucher.number)


# `TestExtractMatchesTheGenerators` stood here until the generators were removed
# with them. It asserted that the model this extract produces equals the model
# read back from the generators' XML, which is what made the switch reviewable.
# It cannot outlive its subject: with one path there is nothing to compare. What
# guards the same ground now is `tests/golden/` - the rendered XML of thirteen
# scenarios, byte for byte - plus the validator, which reads the emitted XML back
# and checks it against Tally's rules rather than against a previous
# implementation of ours.


class TestPartyLinesPostToTheParty(IntegrationTestCase):
    """A journal line carrying a party posts to the party ledger.

    ERPNext books it against a control account - Creditors, Debtors, Payroll
    Payable - with the party named alongside. Tally has no such ledger: Sundry
    Creditors is a *group* with the parties under it. Posting the control
    account created a lump nobody can age and left 654 party lines invisible in
    the parties' own balances.
    """

    def test_every_line_erpnext_gave_a_party_posts_to_that_party(self):
        """The precise claim, and no more than it.

        A line ERPNext booked against a control account *with no party* is a
        genuine posting to that account - three Payroll Payable accruals on this
        bench are exactly that, and they belong on the control ledger in Tally
        too. What must not happen is a line that names a party landing on the
        control account, because then the party's balance never moves.
        """
        for company in _companies():
            rows = frappe.db.sql("""select ja.parent, ja.party_type, ja.party
                from `tabJournal Entry Account` ja
                join `tabJournal Entry` je on je.name = ja.parent
                where je.docstatus = 1 and je.company = %s
                      and ifnull(ja.party, '') != ''""", company, as_dict=True)
            if not rows:
                continue
            export = extract(company, None, None)
            by_id = {v.remote_id: v for v in export.vouchers}
            names = _party_names()
            for row in rows:
                voucher = by_id.get(row.parent)
                if not voucher:
                    continue
                expected = names.get((row.party_type, row.party))
                with self.subTest(company=company, voucher=row.parent, party=row.party):
                    self.assertIn(expected, [l.ledger for l in voucher.lines],
                                  "the party ERPNext named is not a ledger on this voucher")

    def test_a_journal_party_line_is_flagged_and_carries_a_reference(self):
        rows = frappe.db.sql("""select ja.parent, je.company from `tabJournal Entry Account` ja
            join `tabJournal Entry` je on je.name = ja.parent
            where je.docstatus = 1 and ifnull(ja.party, '') != '' limit 1""", as_dict=True)
        if not rows:
            self.skipTest("No journal line carries a party on this site.")
        export = extract(rows[0].company, None, None)
        match = [v for v in export.vouchers if v.remote_id == rows[0].parent]
        self.assertTrue(match)
        party_lines = [l for l in match[0].lines if l.is_party]
        self.assertTrue(party_lines, "the party line is not flagged")
        for line in party_lines:
            self.assertTrue(line.bill_refs, "a flagged party line carries no bill reference")


class TestBillAllocations(IntegrationTestCase):
    """Tally posts to On Account whatever a bill reference does not claim, so
    the references have to add up to the line or the ageing drifts from the
    ledger balance."""

    def test_every_party_line_carries_references_totalling_the_line(self):
        for company in _companies():
            export = extract(company, None, None)
            for voucher in export.vouchers:
                for line in voucher.lines:
                    if not line.is_party:
                        continue
                    with self.subTest(company=company, voucher=voucher.number):
                        self.assertTrue(line.bill_refs)
                        self.assertEqual(
                            money(sum(r.amount for r in line.bill_refs)),
                            money(line.amount))

    def test_an_invoice_opens_a_bill_and_a_payment_settles_it(self):
        rows = frappe.db.sql("""select p.name, p.company, r.reference_name
            from `tabPayment Entry Reference` r
            join `tabPayment Entry` p on p.name = r.parent
            where p.docstatus = 1 and r.reference_doctype = 'Sales Invoice' limit 1""",
            as_dict=True)
        if not rows:
            self.skipTest("No payment allocated against a sales invoice.")
        export = extract(rows[0].company, None, None)
        by_id = {v.remote_id: v for v in export.vouchers}

        invoice = by_id.get(rows[0].reference_name)
        payment = by_id.get(rows[0].name)
        self.assertTrue(invoice and payment)

        opened = [r for l in invoice.lines for r in l.bill_refs if r.kind == NEW_REF]
        self.assertEqual([r.name for r in opened], [rows[0].reference_name],
                         "the invoice does not open a bill under its own name")
        settled = [r for l in payment.lines for r in l.bill_refs
                   if r.kind == AGAINST_REF and r.name == rows[0].reference_name]
        self.assertTrue(settled, "the payment does not settle the bill the invoice opened")
        # Opposite signs against one bill name is what closes it in Tally.
        self.assertLess(float(opened[0].amount) * float(settled[0].amount), 0)

    def test_a_reference_tally_has_no_bill_for_goes_on_account(self):
        """A payment may reference an Expense Claim or an Employee Advance.
        Neither is a voucher this connector exports, so there is no bill in Tally
        to allocate against and the amount is named On Account rather than
        pointed at a reference that does not exist."""
        rows = frappe.db.sql("""select p.name, p.company from `tabPayment Entry Reference` r
            join `tabPayment Entry` p on p.name = r.parent
            where p.docstatus = 1 and r.reference_doctype not in
                  ('Sales Invoice', 'Purchase Invoice') limit 1""", as_dict=True)
        if not rows:
            self.skipTest("No payment references a non-invoice document.")
        export = extract(rows[0].company, None, None)
        match = [v for v in export.vouchers if v.remote_id == rows[0].name]
        self.assertTrue(match)
        kinds = {r.kind for l in match[0].lines for r in l.bill_refs}
        self.assertIn(ON_ACCOUNT, kinds)


class TestTheExtractedModelValidates(IntegrationTestCase):
    """Run the validator over the model itself, not only over rendered XML.

    Cheaper and stricter than restating each rule here, and it means a check
    added in a later stage starts guarding the extract on the same day.
    """

    def test_nothing_blocks_the_import(self):
        from tally_connector.tally.validate import validate

        for company in _companies():
            report = validate(extract(company, None, None))
            with self.subTest(company=company):
                self.assertEqual([str(f) for f in report.blocking], [])

    def test_every_voucher_with_a_party_flags_exactly_one_line(self):
        """A party line that is not flagged is the difference between Tally
        tracking a bill and posting the amount to On Account."""
        from tally_connector.tally.validate import validate

        for company in _companies():
            report = validate(extract(company, None, None))
            party = [str(f) for f in report.findings if f.check == "party"]
            with self.subTest(company=company):
                self.assertEqual(party[:5], [])


class TestNoNameCollidesOnceStripped(IntegrationTestCase):
    """Whitespace at the ends of a Tally ledger name is invisible and never
    meaningful. Two ledgers that differ only by it are two ledgers in Tally."""

    def test_no_ledger_name_carries_stray_whitespace(self):
        for company in _companies():
            export = extract(company, None, None)
            offenders = [l.name for l in export.masters.ledgers
                         if l.name != str(l.name).strip()]
            with self.subTest(company=company):
                self.assertEqual(offenders, [])

    def test_no_two_ledgers_collide_once_stripped(self):
        for company in _companies():
            export = extract(company, None, None)
            seen = {}
            for l in export.masters.ledgers:
                seen.setdefault(str(l.name).strip(), []).append(l.name)
            with self.subTest(company=company):
                self.assertEqual({k: v for k, v in seen.items() if len(v) > 1}, {})


class TestTheExportRendersFromTheModel(IntegrationTestCase):
    """The shipping path is the one the offline suite exercises."""

    def test_the_shipping_path_goes_through_the_model(self):
        """Stated as identity, not as a substring of somebody's source.

        The first version searched `write_full_export`'s source for an import
        line. That said nothing about what the function calls and broke the
        moment the import moved to the top of its module - which is exactly what
        happened. What actually matters is that the objects the exporter reaches
        for are the same extract and the same renderer the golden files and the
        validator run against: one path, not two that can drift.
        """
        from tally_connector.tally import extract as extract_module
        from tally_connector.tally import render as render_module
        from tally_connector.utils import exporter

        self.assertIs(exporter.extract, extract_module.extract)
        self.assertIs(exporter.render_vouchers, render_module.render_vouchers)
        self.assertIs(exporter.render_masters, render_module.render_masters)

    def test_the_rendered_export_carries_what_the_audit_found_missing(self):
        """The XML differences are deliberate, so they are named rather than
        left as an unexplained diff against the old output."""
        from lxml import etree

        from tally_connector.tests.inventory import inventory_sent

        company = _companies()[0]
        with tempfile.TemporaryDirectory() as tmp, inventory_sent():
            m, v = os.path.join(tmp, "m.xml"), os.path.join(tmp, "v.xml")
            from tally_connector.utils.exporter import write_full_export
            write_full_export(None, None, company, m, v)
            masters = etree.parse(m).getroot()
            vouchers = etree.parse(v).getroot()

        self.assertTrue(vouchers.findall(".//ISPARTYLEDGER"), "party lines are not flagged")
        self.assertTrue(vouchers.findall(".//PERSISTEDVIEW"), "no voucher declares its view")
        self.assertTrue(masters.findall(".//DECIMALPLACES"), "units carry no decimal places")
        for v in vouchers.iter("VOUCHER"):
            if v.find("ALLINVENTORYENTRIES.LIST") is not None:
                self.assertEqual(v.findtext("ISINVOICE"), "Yes")
                break


class TestCostCentresAndGodowns(IntegrationTestCase):
    """Tally refuses an allocation on a ledger that has not got cost centres
    switched on, and refuses a cost centre or godown no master creates."""

    def test_every_allocated_ledger_has_cost_centres_enabled(self):
        """The flag follows the data, not the account's root type.

        The root-type guess was wrong on this bench: ERPNext puts a cost centre
        on TDS and TCS lines and on a journal's party line, none of which is
        income or expense, and every one of those would have been refused.
        """
        for company in _companies():
            export = extract(company, None, None)
            enabled = {l.name for l in export.masters.ledgers if l.cost_centres_on}
            allocated = {l.ledger for v in export.vouchers for l in v.lines if l.cost_centres}
            with self.subTest(company=company):
                self.assertEqual(sorted(allocated - enabled), [])

    def test_every_cost_centre_named_has_a_master(self):
        for company in _companies():
            export = extract(company, None, None)
            created = {c.name for c in export.masters.cost_centres}
            named = {a.name for v in export.vouchers for l in v.lines
                     for a in l.cost_centres}
            with self.subTest(company=company):
                self.assertTrue(created, "no cost centre masters exported at all")
                self.assertEqual(sorted(named - created), [])

    def test_allocations_total_their_line(self):
        """Tally treats a shortfall as unallocated, so the analysis stops
        reconciling to the ledger."""
        for company in _companies():
            export = extract(company, None, None)
            for voucher in export.vouchers:
                for line in voucher.lines:
                    if not line.cost_centres:
                        continue
                    with self.subTest(company=company, voucher=voucher.number):
                        self.assertEqual(
                            money(sum(a.amount for a in line.cost_centres)),
                            money(line.amount))

    def test_a_cost_centre_keeps_its_own_name(self):
        """ERPNext suffixes it with the company abbreviation exactly as it does
        an account, and that suffix means nothing in Tally."""
        for company in _companies():
            export = extract(company, None, None)
            abbr = frappe.get_cached_value("Company", company, "abbr")
            for centre in export.masters.cost_centres:
                with self.subTest(company=company, centre=centre.name):
                    self.assertFalse(centre.name.endswith(f" - {abbr}"))

    def test_godowns_are_off_by_default_and_nothing_names_one(self):
        """Sending a godown to a company without multiple godowns enabled is a
        refusal, while omitting it is always safe - Tally uses its primary
        location. The switch is opt-in for a site that tracks locations."""
        settings = frappe.get_single("Tally Settings")
        if settings.get("include_godowns"):
            self.skipTest("Godowns are switched on for this site.")
        for company in _companies():
            export = extract(company, None, None)
            named = {i.godown for v in export.vouchers for i in v.inventory if i.godown}
            with self.subTest(company=company):
                self.assertEqual(export.masters.godowns, [])
                self.assertEqual(named, set())

    def test_a_whole_number_unit_allows_no_decimals(self):
        """ERPNext knows which units cannot be fractional; a unit that allows two
        decimals where it should allow none lets Tally accept half a licence."""
        whole = set(frappe.get_all("UOM", filters={"must_be_whole_number": 1},
                                   pluck="name"))
        if not whole:
            self.skipTest("No whole-number UOM on this site.")
        from tally_connector.tally.extract import unit_name

        expected = {unit_name(u) for u in whole}
        for company in _companies():
            export = extract(company, None, None)
            for unit in export.masters.units:
                if unit.symbol in expected:
                    with self.subTest(company=company, unit=unit.symbol):
                        self.assertEqual(unit.decimal_places, 0)
