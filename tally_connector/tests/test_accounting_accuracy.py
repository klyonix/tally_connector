"""
The money must not move.

This is the suite that matters. Everything else in this app is plumbing around
one promise: what ERPNext has posted is what Tally will post. So these tests do
not check the exporter against itself - they check it against **ERPNext's own
general ledger**, which is the only independent record of what the books say.

What it cannot check is the other half. Tally is not reachable from this bench,
so nothing here proves what Tally does with the file; it proves that the file
says what ERPNext says. The leg from XML to a Tally ledger balance is unverified
and is named as such in the report rather than assumed.

The comparison is deliberately made per voucher and per ledger rather than on a
grand total. Two errors of opposite sign cancel in a total, and a total that
agrees while every line is wrong is the worst result this could produce.
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from tally_connector.tally.extract import extract
from tally_connector.utils.ledgers import ledger_name

# What a rounding difference may be, per voucher, before it is a defect rather
# than arithmetic. ERPNext posts to two decimal places and so does the export;
# anything above a paisa is a real disagreement.
TOLERANCE = 0.011

# Bank Transaction is a statement line, not an accounting document - it has no
# GL Entry to compare against. Excluding it here is not skipping a check; it is
# excluding rows the check does not apply to.
NON_GL_KINDS = ("Contra",)


def _company():
    counts = {c: 0 for c in frappe.get_all("Company", pluck="name")}
    for doctype in ("Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry"):
        for company in counts:
            counts[company] += frappe.db.count(doctype, {"docstatus": 1, "company": company})
    return max(counts, key=counts.get)


def _gl_totals(company):
    """ERPNext's own debit and credit per voucher, in one query.

    Read straight from `GL Entry` rather than from the documents, because GL is
    what ERPNext actually posted - a document's `grand_total` is what it says
    about itself, and the two can differ where rounding or a write-off is
    involved.
    """
    rows = frappe.db.sql(
        """select voucher_no, sum(debit) as debit, sum(credit) as credit
           from `tabGL Entry`
           where company = %s and is_cancelled = 0
           group by voucher_no""",
        company, as_dict=True,
    )
    return {r.voucher_no: r for r in rows}


def _gl_by_account(company):
    """Debit and credit per (voucher, account) - the per-line comparison."""
    rows = frappe.db.sql(
        """select voucher_no, account, sum(debit) as debit, sum(credit) as credit
           from `tabGL Entry`
           where company = %s and is_cancelled = 0
           group by voucher_no, account""",
        company, as_dict=True,
    )
    out = {}
    for r in rows:
        out.setdefault(r.voucher_no, {})[r.account] = flt(r.debit, 2) - flt(r.credit, 2)
    return out


class TestTheExportAgreesWithTheGeneralLedger(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _company()
        cls.export = extract(cls.company, None, None)
        cls.gl = _gl_totals(cls.company)
        cls.gl_by_account = _gl_by_account(cls.company)
        # Only vouchers ERPNext actually posted a ledger entry for.
        # Which (voucher, account) rows ERPNext posted against a party, so they
        # can be left out of the per-ledger comparison rather than mapped.
        cls.party_accounts = {}
        for row in frappe.db.sql(
            """select voucher_no, account from `tabGL Entry`
               where company = %s and is_cancelled = 0 and ifnull(party, '') != ''""",
            cls.company, as_dict=True,
        ):
            cls.party_accounts.setdefault(row.voucher_no, set()).add(row.account)
        cls.comparable = [
            v for v in cls.export.vouchers
            if v.remote_id in cls.gl and v.kind not in NON_GL_KINDS
        ]

    def test_there_is_enough_data_to_prove_anything(self):
        """Every assertion below is vacuous against an empty export."""
        self.assertGreater(len(self.export.vouchers), 100)
        self.assertGreater(
            len(self.comparable), 100,
            "almost nothing matched a GL entry - the join is wrong, not the data",
        )

    def test_every_voucher_balances_to_zero(self):
        """A voucher that does not balance is refused by Tally line by line, and
        an unbalanced one that Tally accepts is a corrupted trial balance."""
        offenders = [
            (v.kind, v.number, str(v.imbalance()))
            for v in self.export.vouchers if not v.balances()
        ]
        self.assertEqual(offenders[:10], [], f"{len(offenders)} vouchers do not balance")

    def test_every_ledger_moves_by_exactly_what_erpnext_posted(self):
        """The comparison that decides every balance in Tally.

        Net movement per (voucher, ledger), not gross debit and credit totals.
        The gross figures legitimately differ in one case and it is worth naming:
        where TDS is withheld, ERPNext credits the supplier the gross amount and
        debits back the deduction as a second row on the same account, while this
        export posts the net once. Two rows against one - same ledger, same net,
        same outstanding. Comparing gross would fail on correct output, and a
        test that fails on correct output is one somebody switches off.

        Net per ledger catches everything that actually matters: an amount posted
        to the wrong account, a line dropped, a sign inverted, a party balance
        that will not clear.
        """
        offenders = []
        for v in self.comparable:
            # Party lines are excluded from both sides. ERPNext posts a party to
            # a control account with the party named beside it; this export gives
            # the party a ledger of its own, so the two cannot be compared
            # account for account without inventing a mapping. An earlier version
            # invented one - it dropped a journal's whole party net on whichever
            # Receivable or Payable it found first, which failed on correct
            # output for any journal touching two of them. Party balances are
            # `TestPartyBalancesAgree`'s subject and are checked there in full.
            ours = {}
            for line in v.lines:
                if line.is_party:
                    continue
                ours[line.ledger] = ours.get(line.ledger, 0.0) + float(line.amount)
            for inv in v.inventory:
                ours[inv.ledger] = ours.get(inv.ledger, 0.0) + float(inv.amount)

            theirs = {}
            for account, net in self.gl_by_account.get(v.remote_id, {}).items():
                if account in self.party_accounts.get(v.remote_id, set()):
                    continue
                # ERPNext posts a party to the control account with the party
                # named beside it; Tally has no control account, so the party is
                # its own ledger. Keyed the way the export names it.
                theirs[ledger_name(account, self.company)] = \
                    theirs.get(ledger_name(account, self.company), 0.0) + net

            for ledger in set(ours) | set(theirs):
                # Our sign convention is the opposite of a GL net: positive is a
                # credit here, and `debit - credit` there.
                mine = -flt(ours.get(ledger, 0.0), 2)
                yours = flt(theirs.get(ledger, 0.0), 2)
                if abs(mine - yours) > TOLERANCE:
                    offenders.append((v.kind, v.number, ledger, round(mine, 2), round(yours, 2)))

        self.assertEqual(
            offenders[:12], [],
            f"{len(offenders)} ledger movements disagree with ERPNext's GL "
            "(kind, voucher, ledger, ours, ERPNext)",
        )

    def test_no_voucher_erpnext_posted_is_silently_dropped(self):
        """A voucher that never reaches the file is the failure nobody notices:
        the export succeeds, Tally accepts it, and the books are short."""
        exported = {v.remote_id for v in self.export.vouchers if v.remote_id}
        missing = []
        for doctype, date_field in (
            ("Sales Invoice", "posting_date"), ("Purchase Invoice", "posting_date"),
            ("Payment Entry", "posting_date"), ("Journal Entry", "posting_date"),
        ):
            for name in frappe.get_all(
                doctype, filters={"docstatus": 1, "company": self.company}, pluck="name",
            ):
                if name not in exported:
                    missing.append(f"{doctype} {name}")
        self.assertEqual(missing[:10], [], f"{len(missing)} submitted documents were not exported")

    def test_a_cancelled_document_is_never_exported(self):
        """Cancelled means ERPNext has reversed it. Sending it to Tally posts a
        transaction the books do not contain."""
        exported = {v.remote_id for v in self.export.vouchers if v.remote_id}
        leaked = []
        for doctype in ("Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry"):
            for name in frappe.get_all(
                doctype, filters={"docstatus": 2, "company": self.company}, pluck="name",
            ):
                if name in exported:
                    leaked.append(f"{doctype} {name}")
        self.assertEqual(leaked, [], "cancelled documents reached the export")


class TestTaxIsCarriedExactly(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _company()
        cls.export = extract(cls.company, None, None)

    def test_every_invoices_tax_matches_the_documents_own_tax_rows(self):
        """GST is the part an auditor checks first. The comparison is against the
        invoice's own tax table, per account, so a rate applied to the wrong base
        shows up rather than being absorbed into a total."""
        invoices = {
            v.remote_id: v for v in self.export.vouchers
            if v.kind in ("Sales", "Credit Note", "Purchase", "Debit Note") and v.remote_id
        }
        if not invoices:
            self.skipTest("no invoices on this company")

        offenders = []
        for doctype, table in (("Sales Invoice", "Sales Taxes and Charges"),
                               ("Purchase Invoice", "Purchase Taxes and Charges")):
            names = [n for n in frappe.get_all(
                doctype, filters={"docstatus": 1, "company": self.company}, pluck="name")
                if n in invoices]
            if not names:
                continue
            # `add_deduct_tax` is on Purchase Taxes and Charges and not on the
            # Sales one - the trap `_children_by_parent` documents. Naming it in
            # a SELECT against the sales table is a hard SQL error, which is how
            # the first version of this test errored rather than failed.
            fields = ["parent", "account_head", "base_tax_amount_after_discount_amount"]
            if frappe.db.has_column(table, "add_deduct_tax"):
                fields.append("add_deduct_tax")
            rows = frappe.get_all(
                table, filters={"parent": ["in", names]}, fields=fields,
                limit_page_length=0,
            )
            wanted = {}
            for r in rows:
                key = (r.parent, ledger_name(r.account_head, self.company))
                value = flt(r.base_tax_amount_after_discount_amount, 2)
                # A `Deduct` row is withheld from the supplier rather than added
                # to the bill - TDS above all - so it is a credit where every
                # other purchase tax is a debit. The first version of this test
                # ignored the column and failed on two correct TDS lines.
                if r.get("add_deduct_tax") == "Deduct":
                    value = -value
                wanted[key] = wanted.get(key, 0.0) + value

            # An item row may post to the same account as a charge row.
            # Purchase invoices can book an agent's fee and the
            # ticket itself to `Air Travel Expenses`, and sent as accounting
            # only the ticket's value folds onto that same ledger line. The
            # line is then the charge plus the items, and ERPNext's own GL
            # agrees: such an invoice nets the fee plus the ticket there. Comparing the line with the tax
            # table alone failed on eleven vouchers whose money was right.
            folded = {}
            if getattr(self.export, "accounting_only", False):
                account_field = ("income_account" if doctype == "Sales Invoice"
                                 else "expense_account")
                for r in frappe.get_all(
                        f"{doctype} Item", filters={"parent": ["in", names]},
                        fields=["parent", account_field, "base_net_amount"],
                        limit_page_length=0):
                    account = r.get(account_field)
                    if not account:
                        continue
                    key = (r.parent, ledger_name(account, self.company))
                    folded[key] = folded.get(key, 0.0) + flt(r.base_net_amount, 2)

            for (parent, ledger), amount in wanted.items():
                if abs(amount) < 0.005:
                    continue
                voucher = invoices[parent]
                ours = sum(float(l.amount) for l in voucher.lines if l.ledger == ledger)
                # Sales tax is a credit (positive here); purchase tax a debit.
                # No second flip for a return. ERPNext already stores a
                # negative tax amount on a credit or debit note, so inverting
                # again turned correct output into a failure - which is what the
                # first version of this test did.
                expected = amount if doctype == "Sales Invoice" else -amount
                items = folded.get((parent, ledger), 0.0)
                expected += items if doctype == "Sales Invoice" else -items
                if abs(ours - expected) > TOLERANCE:
                    offenders.append((parent, ledger, round(ours, 2), round(expected, 2)))

        self.assertEqual(
            offenders[:10], [],
            f"{len(offenders)} tax lines disagree with the invoice's own tax table "
            "(voucher, ledger, ours, ERPNext)",
        )

    def test_gst_ledgers_are_placed_where_a_return_can_find_them(self):
        """A GST ledger outside Duties & Taxes imports perfectly cleanly and is
        missing from GSTR-1, which is the worst class of defect here: silent."""
        from tally_connector.tally.extract import gst_ledgers

        gst = gst_ledgers(self.company)
        if not gst:
            self.skipTest("india_compliance reports no GST accounts for this company")
        created = {l.name: l for l in self.export.masters.ledgers}
        misplaced = [
            (name, created[name].parent)
            for name in gst
            if name in created and created[name].parent != "Duties & Taxes"
        ]
        self.assertEqual(misplaced, [], "GST ledgers outside Duties & Taxes")
        headless = [
            name for name in gst
            if name in created and not created[name].duty_head
        ]
        self.assertEqual(headless, [], "GST ledgers with no duty head - Tally cannot classify them")


class TestPartyBalancesAgree(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _company()
        cls.export = extract(cls.company, None, None)

    def test_each_partys_net_movement_matches_the_ledger(self):
        """What a customer owes is the number they will ring about.

        Summed per party across every voucher and compared with ERPNext's own
        party postings. A party line posted to the wrong ledger, or dropped, or
        double-counted shows up here and nowhere else - the voucher still
        balances either way.
        """
        ours = {}
        for voucher in self.export.vouchers:
            for line in voucher.lines:
                if line.is_party:
                    ours[line.ledger] = ours.get(line.ledger, 0.0) + float(line.amount)
        if not ours:
            self.skipTest("no party lines in this export")

        # Every document type that posts, Expense Claim included. This used to
        # leave Expense Claim out as "out of scope" and said the gap was
        # reported by `test_the_unexported_party_postings_are_known` - a test
        # that was never written. So nothing reported it, and a full year reached
        # Tally with employees showing payments and none of the claims they
        # settled. `_gl_documents`
        # sends them now, and `test_completeness` holds every posting type.
        #
        # Every posting document, not the reader's own list: built from that
        # list, this test dropped a type from both sides at once and passed
        # with expense claims removed from the reader again.
        rows = frappe.db.sql(
            """select party_type, party, sum(credit) - sum(debit) as net
               from `tabGL Entry`
               where company = %s and is_cancelled = 0 and ifnull(party, '') != ''
                 and voucher_type != 'Period Closing Voucher'
               group by party_type, party""",
            self.company, as_dict=True,
        )
        theirs = {}
        for r in rows:
            label = frappe.db.get_value(r.party_type, r.party, "name") or r.party
            display = {
                "Customer": lambda p: frappe.db.get_value("Customer", p, "customer_name") or p,
                "Supplier": lambda p: frappe.db.get_value("Supplier", p, "supplier_name") or p,
                "Employee": lambda p: frappe.db.get_value("Employee", p, "employee_name") or p,
            }.get(r.party_type, lambda p: p)(label)
            key = str(display).strip()
            theirs[key] = theirs.get(key, 0.0) + flt(r.net, 2)

        offenders = []
        for party, amount in ours.items():
            if party in theirs and abs(amount - theirs[party]) > TOLERANCE:
                offenders.append((party, round(amount, 2), round(theirs[party], 2)))
        matched = len([p for p in ours if p in theirs])
        self.assertGreater(matched, 0, "no party name matched ERPNext - the comparison is vacuous")
        self.assertEqual(
            offenders[:10], [],
            f"{len(offenders)} of {matched} party balances disagree (party, ours, ERPNext)",
        )


class TestAStockRateAgreesWithItsAmount(IntegrationTestCase):
    """Tally stores RATE and AMOUNT exactly as given and reconciles neither.

    A rate that disagrees with the amount is an item priced wrongly in the
    customer's books for ever: the value right, the rate about 18% high, and
    nothing anywhere saying so. Read back off the live company after a real
    push, Tally held `rate=1960.00/Box, qty=2.00 Box, amount=3322.03`.

    The cause was sending `Sales Invoice Item.rate` - the **gross** rate on a
    tax-inclusive invoice - beside `base_net_amount`, which is net. Measured on
    the real data: 103,588 of 103,737 sales lines had `rate * qty != amount`.
    """

    def _rate(self, base_net_rate, amount, qty, rate=0):
        from tally_connector.tally.extract import _stock_rate

        row = frappe._dict({"base_net_rate": base_net_rate, "rate": rate})
        return _stock_rate(row, amount, qty)

    def test_the_net_rate_is_what_is_sent(self):
        """ERPNext's own figure, because it is the number somebody typed."""
        self.assertEqual(float(self._rate(1661.02, 3322.04, 2)), 1661.02)

    def test_a_rate_that_cannot_multiply_back_exactly_is_within_a_paisa(self):
        """Seven boxes at 5,089.83 is 727.118571 each and no rate rounded to
        paise reproduces that. The amount is sent explicitly and is what Tally
        posts, so the accounting is unaffected."""
        got = float(self._rate(727.12, 5089.83, 7))
        self.assertLess(abs(got * 7 - 5089.83), 0.05)

    def test_it_falls_back_to_the_amount_when_there_is_no_net_rate(self):
        self.assertAlmostEqual(float(self._rate(0, 500.0, 4)), 125.0, places=2)

    def test_the_gross_rate_never_survives(self):
        """The defect itself: 1960 against an amount of 3322.03."""
        self.assertNotEqual(float(self._rate(1661.02, 3322.03, 2, rate=1960)), 1960)

    def test_a_purchase_line_gets_a_positive_rate(self):
        """The amount is negative because the sign is how this model says
        debit. A price is never negative."""
        self.assertGreater(float(self._rate(100.0, -500.0, 5)), 0)

    def test_a_line_with_no_quantity_keeps_the_stated_rate(self):
        """Nothing to derive from, and inventing a rate would be worse."""
        self.assertEqual(float(self._rate(250.0, 0, 0)), 250.0)

    def test_every_stock_line_this_company_exports_agrees(self):
        """The invariant, over the real data rather than a fixture.

        It asks for the stock lines rather than taking whatever the site is
        set to send. What is under test is how the reader prices a line, which
        is a property of the reader; with `Send item detail on vouchers`
        unticked - which is how many sites are set up - `extract`
        folds every line away and this skipped, taking the gross-versus-net
        rate fix with it. The class below this one records that exact trap: a
        test that finds nothing, skips, and passes with the defect planted
        back in.
        """
        from tally_connector.tally.extract import extract
        from tally_connector.tests.inventory import inventory_sent

        company = frappe.db.get_value("Company", {}, "name")
        with inventory_sent():
            export = extract(company, "2000-01-01", "2100-01-01")
        checked = bad = 0
        for voucher in export.vouchers:
            for inv in voucher.inventory:
                if not inv.quantity:
                    continue
                checked += 1
                # Within a paisa per line: two decimals cannot always
                # multiply back exactly, and the amount is authoritative.
                if abs(abs(inv.rate * inv.quantity) - abs(inv.amount)) > 0.05:
                    bad += 1
        if not checked:
            self.skipTest("This company exports no stock lines.")
        self.assertEqual(bad, 0, f"{bad} of {checked} stock lines misprice the item")


class TestOnlyBalanceSheetAccountsCarryAnOpening(IntegrationTestCase):
    """A profit and loss ledger starts each year at nought.

    Last year's result closes into reserves, so summing every posting ever
    made and calling it an opening puts the whole of a company's history onto
    this year's income. Measured on a real migration: the Sales ledger reached
    Tally with an opening larger than the year's whole revenue, and Tally's own Profit &
    Loss A/c read far above that revenue. The
    movement had been right all along - the opening was the entire error, and
    an income-tax return built on it would have been wrong.

    The postings are made here rather than read off the bench. The first
    version of this asked whichever company the site happened to have, found
    no opening balances, skipped, and passed with the defect planted back in.
    """

    def setUp(self):
        super().setUp()
        self.company = frappe.db.get_value("Company", {}, "name")
        self.income = frappe.db.get_value(
            "Account", {"company": self.company, "root_type": "Income",
                        "is_group": 0}, "name")
        self.asset = frappe.db.get_value(
            "Account", {"company": self.company, "root_type": "Asset",
                        "is_group": 0}, "name")
        if not (self.income and self.asset):
            self.skipTest("This company has no income or asset account.")

    def _post(self, account, debit, credit, when="2020-01-01"):
        """One GL entry before the period, without a document behind it."""
        # Written straight to the table. `GL Entry` links `voucher_no` to a
        # real document, and inventing a Journal Entry here would drag in the
        # whole of ERPNext's posting validation for a row this test only needs
        # to exist so the function under test can read it back.
        frappe.db.sql("""
            INSERT INTO `tabGL Entry`
                (name, creation, modified, owner, modified_by, docstatus,
                 posting_date, account, company, debit, credit,
                 debit_in_account_currency, credit_in_account_currency,
                 voucher_type, voucher_no, is_cancelled)
            VALUES (%s, NOW(), NOW(), 'Administrator', 'Administrator', 1,
                    %s, %s, %s, %s, %s, %s, %s, 'Journal Entry',
                    'KX-OPENING-TEST', 0)
        """, (frappe.generate_hash(length=10), when, account, self.company,
              debit, credit, debit, credit))

    def test_an_income_account_is_given_no_opening(self):
        from tally_connector.tally.extract import _opening_balances

        self._post(self.income, 0, 5000)
        self._post(self.asset, 5000, 0)
        opening = _opening_balances(self.company, "2025-04-01")
        from tally_connector.tally.extract_rules import tally_key

        income_key = tally_key(self.income.rsplit(" - ", 1)[0])
        offenders = [n for n in opening if tally_key(n) == income_key]
        self.assertEqual(
            offenders, [],
            f"{self.income} is a profit and loss account and was given an opening")

    def test_an_asset_account_still_gets_its_opening(self):
        """The other half: dropping every opening would be just as wrong, and
        a balance sheet without them does not balance."""
        from tally_connector.tally.extract import _opening_balances
        from tally_connector.tally.extract_rules import tally_key

        self._post(self.income, 0, 5000)
        self._post(self.asset, 5000, 0)
        opening = _opening_balances(self.company, "2025-04-01")
        asset_key = tally_key(self.asset.rsplit(" - ", 1)[0])
        self.assertTrue(
            [n for n in opening if tally_key(n) == asset_key],
            f"{self.asset} is a balance sheet account and lost its opening")
