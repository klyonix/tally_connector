"""
The group tree Tally is asked to build.

Written after the first real import failed. Tally aborted the whole masters file
at line 45 with *"Group 'Application of Funds (Assets)' is already a child of
this!"*, and the cause was a cycle this export created:

    Application of Funds (Assets)  ->  parent: Current Assets
    Current Assets                 ->  parent: Application of Funds (Assets)

ERPNext keeps a root layer above everything - the two sides of the balance
sheet, income, expenses - and Tally expresses the same idea with reserved
primary groups it owns. Recreating ERPNext's roots *inside* one of those made
each the other's ancestor. The validator had no opinion about the group tree at
all, so the file reported `would_import: True` and was sent.

Two rules come out of it, and both are tested against this bench's real chart
rather than a fixture: a reserved group is never created, and the tree is a
tree.
"""

import frappe
from frappe.tests import IntegrationTestCase

from tally_connector.tally.extract import extract_masters
from tally_connector.tally.model import Export, Group, Masters
from tally_connector.tally.validate import validate
from tally_connector.utils.ledgers import TALLY_PRIMARY, is_reserved_group


def _companies():
    return frappe.get_all("Company", pluck="name")


class TestTheTreeWeSend(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.trees = {c: {g.name: g.parent for g in extract_masters(c).groups}
                     for c in _companies()}

    def test_there_are_groups_to_check(self):
        """Every assertion below is vacuous against an empty tree."""
        self.assertTrue(self.trees)
        self.assertTrue(any(len(t) > 3 for t in self.trees.values()))

    def test_no_group_tally_reserves_is_ever_created(self):
        """Tally owns these and owns where they sit. A `GROUP` master naming one
        with a different parent is a re-parenting, and Tally stops the file at
        that line rather than ignoring it."""
        for company, tree in self.trees.items():
            offenders = sorted(n for n in tree if is_reserved_group(n))
            with self.subTest(company=company):
                self.assertEqual(offenders, [])

    def test_no_group_is_its_own_ancestor(self):
        """The defect itself, stated directly."""
        for company, tree in self.trees.items():
            for name in tree:
                seen, cur, steps = {name}, tree.get(name), 0
                while cur and steps < 60:
                    with self.subTest(company=company, group=name):
                        self.assertNotIn(cur, seen, f"{name} leads back to {cur}")
                    seen.add(cur)
                    cur, steps = tree.get(cur), steps + 1

    def test_every_parent_is_created_here_or_reserved_by_tally(self):
        """A parent that is neither is a group landing nowhere."""
        for company, tree in self.trees.items():
            dangling = sorted({
                p for p in tree.values()
                if p and p not in tree and not is_reserved_group(p)
            })
            with self.subTest(company=company):
                self.assertEqual(dangling, [])

    def test_no_group_is_left_without_a_parent(self):
        """Tally puts a group with no parent at the top of the tree, beside its
        own reserved ones, which is not where a customer's ledger group goes."""
        for company, tree in self.trees.items():
            with self.subTest(company=company):
                self.assertEqual(sorted(n for n, p in tree.items() if not p), [])

    def test_an_erpnext_root_is_not_sent_at_all(self):
        """It has no counterpart: its children are mapped onto Tally's own
        primary groups, so the root would be a container nothing needs."""
        for company in _companies():
            roots = frappe.get_all(
                "Account",
                filters={"company": company, "is_group": 1, "parent_account": ["in", ["", None]]},
                pluck="account_name",
            )
            if not roots:
                continue
            with self.subTest(company=company):
                for root in roots:
                    self.assertNotIn(root, self.trees[company])

    def test_a_child_of_a_root_sits_under_tallys_primary_group_for_its_type(self):
        """The mapping that replaces the root layer. Checked against a group
        that has one, so the test is not vacuous on a flat chart."""
        for company in _companies():
            rows = frappe.get_all(
                "Account",
                filters={"company": company, "is_group": 1},
                fields=["name", "account_name", "parent_account", "root_type"],
                limit_page_length=0,
            )
            by_name = {r.name: r for r in rows}
            children = [
                r for r in rows
                if r.parent_account and r.parent_account in by_name
                and not by_name[r.parent_account].parent_account
                and not is_reserved_group(r.account_name)
            ]
            if not children:
                continue
            tree = self.trees[company]
            for row in children[:5]:
                with self.subTest(company=company, group=row.account_name):
                    if row.account_name in tree:
                        self.assertEqual(tree[row.account_name],
                                         TALLY_PRIMARY.get(row.root_type, "Indirect Expenses"))


class TestTheValidatorWouldHaveCaughtIt(IntegrationTestCase):
    """The check that did not exist when the real import failed."""

    def _export(self, groups):
        return Export(company="Probe Co",
                      masters=Masters(groups=[Group(name=n, parent=p) for n, p in groups]))

    def test_the_exact_cycle_from_the_failed_import_is_blocking(self):
        report = validate(self._export([
            ("Application of Funds (Assets)", "Current Assets"),
            ("Current Assets", "Application of Funds (Assets)"),
        ]))
        cycles = [f for f in report.findings if f.check == "group cycle"]
        self.assertTrue(cycles, "the cycle that aborted a real import is not reported")
        self.assertTrue(report.blocking, "a cycle must stop the export, not warn about it")

    def test_a_group_pointing_at_itself_is_caught(self):
        report = validate(self._export([("Odd One", "Odd One")]))
        self.assertTrue([f for f in report.findings if f.check == "group cycle"])

    def test_a_parent_that_nothing_creates_is_caught(self):
        report = validate(self._export([("Stock Assets", "Nowhere At All")]))
        found = [f for f in report.findings if f.check == "group parent"]
        self.assertTrue(found)
        self.assertIn("Nowhere At All", found[0].detail)

    def test_a_reserved_parent_is_not_reported(self):
        """Tally has these; naming one as a parent is correct and must stay
        silent, or the report fills with noise nobody can act on."""
        report = validate(self._export([("Stock Assets", "Current Assets")]))
        self.assertEqual([f for f in report.findings if f.check == "group parent"], [])

    def test_a_sound_tree_produces_nothing(self):
        report = validate(self._export([
            ("Trade Receivables", "Current Assets"),
            ("Retail Debtors", "Trade Receivables"),
        ]))
        noise = [f for f in report.findings if f.check in ("group cycle", "group parent")]
        self.assertEqual(noise, [])


class TestMastersArriveInOrder(IntegrationTestCase):
    """Tally creates masters in the order it reads them.

    The second real import died on this: *"Line 335: Stock Group 'All Item
    Groups' does not exist!"*. It did exist - forty lines further down. ERPNext's
    root item group was emitted after six of its own children, because
    `frappe.get_all` returns rows in the database's order and not the tree's.

    The account groups had been depth-sorted since they were written. The stock
    groups, cost centres and godowns had not, and nothing compared them.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.masters = {c: extract_masters(c) for c in _companies()}

    def _out_of_order(self, rows):
        position = {}
        for index, row in enumerate(rows):
            position.setdefault(row.name, index)
        return [r.name for i, r in enumerate(rows)
                if getattr(r, "parent", "") and position.get(r.parent, -1) > i]

    def test_there_are_masters_to_check(self):
        self.assertTrue(any(len(m.stock_groups) > 1 for m in self.masters.values()))

    def test_no_stock_group_arrives_before_its_parent(self):
        for company, m in self.masters.items():
            with self.subTest(company=company):
                self.assertEqual(self._out_of_order(m.stock_groups), [])

    def test_no_cost_centre_arrives_before_its_parent(self):
        for company, m in self.masters.items():
            with self.subTest(company=company):
                self.assertEqual(self._out_of_order(m.cost_centres), [])

    def test_no_godown_arrives_before_its_parent(self):
        for company, m in self.masters.items():
            with self.subTest(company=company):
                self.assertEqual(self._out_of_order(m.godowns), [])

    def test_every_stock_group_parent_is_created(self):
        """Tally reserves no stock group, so a parent that is not in the file is
        a parent that does not exist."""
        for company, m in self.masters.items():
            names = {g.name for g in m.stock_groups}
            dangling = sorted({g.parent for g in m.stock_groups if g.parent and g.parent not in names})
            with self.subTest(company=company):
                self.assertEqual(dangling, [])

    def test_an_item_can_still_sit_in_the_root_group(self):
        """The root is kept, unlike the account roots. An `Item` can sit directly
        in `All Item Groups` and a voucher can allocate to the root cost centre;
        dropping them would leave those pointing at nothing."""
        for company, m in self.masters.items():
            roots = [g.name for g in m.stock_groups if not g.parent]
            with self.subTest(company=company):
                self.assertTrue(roots, "every stock group has a parent - the root was dropped")


class TestTheValidatorCatchesTheOrdering(IntegrationTestCase):
    def _export(self, groups):
        from tally_connector.tally.model import StockGroup

        return Export(company="Probe Co",
                      masters=Masters(stock_groups=[StockGroup(name=n, parent=p)
                                                    for n, p in groups]))

    def test_a_child_before_its_parent_is_blocking(self):
        report = validate(self._export([("Fixed Asset", "All Item Groups"),
                                        ("All Item Groups", "")]))
        found = [f for f in report.findings if f.check == "master order"]
        self.assertTrue(found, "the ordering that aborted a real import is not reported")
        self.assertTrue(report.blocking)
        self.assertIn("All Item Groups", found[0].detail)

    def test_a_parent_that_is_never_created_is_blocking(self):
        report = validate(self._export([("Fixed Asset", "Nowhere")]))
        self.assertTrue([f for f in report.findings if f.check == "master parent"])

    def test_the_right_order_is_silent(self):
        report = validate(self._export([("All Item Groups", ""),
                                        ("Fixed Asset", "All Item Groups")]))
        noise = [f for f in report.findings if f.check in ("master order", "master parent")]
        self.assertEqual(noise, [])


class TestNoLedgerNamesACurrencyTallyLacks(IntegrationTestCase):
    """A Tally company holds the currencies somebody defined in it and no others.

    The third real import died on *"Line 2951: Currency 'USD' does not exist!"*.
    A ledger naming a currency is a reference to a master exactly like a group or
    a unit, and this export creates no currency masters.

    The import failure is the lesser half. **Every amount this export sends is in
    the company's base currency** - `base_grand_total`, `base_net_amount`,
    `base_paid_amount` - which is the rule at the top of `model.py`. Declaring a
    ledger as USD and posting INR into it is wrong even where Tally has a USD
    currency, because Tally reads those figures as dollars.
    """

    def test_no_ledger_in_a_real_export_names_a_currency(self):
        for company in _companies():
            named = [l.name for l in extract_masters(company).ledgers if l.currency]
            with self.subTest(company=company):
                self.assertEqual(named[:5], [])

    def test_the_check_is_not_vacuous(self):
        for company in _companies():
            ledgers = extract_masters(company).ledgers
            if len(ledgers) > 5:
                return
        self.skipTest("too few ledgers on this bench to prove anything")

    def test_a_foreign_currency_account_still_produces_a_ledger(self):
        """It is not skipped - it is posted in base currency, which is the
        currency of the figures that reach it."""
        for company in _companies():
            foreign = frappe.get_all(
                "Account",
                filters={"company": company, "is_group": 0,
                         "account_currency": ["not in", ["INR", "", None]]},
                pluck="name", limit=1)
            if not foreign:
                continue
            from tally_connector.utils.ledgers import ledger_name

            names = {l.name for l in extract_masters(company).ledgers}
            with self.subTest(company=company):
                self.assertIn(ledger_name(foreign[0], company), names)
            return
        self.skipTest("no foreign-currency account on this bench")

    def test_the_validator_refuses_a_currency_nothing_creates(self):
        from tally_connector.tally.model import Ledger

        export = Export(company="Probe Co", masters=Masters(
            ledgers=[Ledger(name="Debtors USD", parent="Sundry Debtors", currency="USD")]))
        report = validate(export)
        found = [f for f in report.findings if f.check == "currency"]
        self.assertTrue(found, "a currency nothing creates is not reported")
        self.assertTrue(report.blocking, "Tally stops the file on this, so it must block")
        self.assertIn("USD", found[0].tally_says)

    def test_a_ledger_naming_no_currency_is_silent(self):
        from tally_connector.tally.model import Ledger

        export = Export(company="Probe Co", masters=Masters(
            ledgers=[Ledger(name="Cash", parent="Cash-in-Hand")]))
        self.assertEqual([f for f in validate(export).findings if f.check == "currency"], [])

    def test_the_rendered_file_carries_no_currency_element(self):
        """The half a reader can check: what actually reaches Tally."""
        from tally_connector.utils.exporter import render_full_export

        masters_xml, _v, _c = render_full_export(None, None, _companies()[0])
        self.assertNotIn("<CURRENCYNAME>", masters_xml)
