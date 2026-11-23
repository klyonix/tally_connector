"""
The dashboard and the server agree about what exists.

This is the guard for the failure nobody sees until they press something: a page
that renders perfectly and whose every button answers 404, because a method name
in a Vue file is a string and nothing checks it. The SPA is not built in CI and
its calls are never type-checked against Python, so this sweep is the only thing
standing between a rename here and a dead button there.

It reads the built source rather than the bundle, so it holds whether or not
anybody has run `yarn build`.
"""

import os
import re

import frappe
from frappe.tests import IntegrationTestCase

from tally_connector.api.dashboard import AREAS

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND = os.path.join(os.path.dirname(APP), "frontend", "src")

CALL_PATTERN = re.compile(r"tally_connector\.api\.[a-z_]+\.[a-z_]+")

# `toISOString()` cut down to its date half. The call itself is fine - a full
# ISO timestamp is the right way to record an instant.
TRUNCATED_ISO = re.compile(r"toISOString\(\)\s*\.\s*(split|slice|substring|substr)")


def _sources():
    for root, _dirs, files in os.walk(FRONTEND):
        for name in files:
            if name.endswith((".vue", ".ts", ".js")):
                yield os.path.join(root, name)


def _called_methods():
    """Every server method the front end names.

    The ten per-area endpoints are built by interpolation - `export_${row.key}` -
    so they cannot be found by reading strings. They are added from the area
    table the server itself declares, which is the same list the UI iterates, so
    an area added on one side and not the other fails here rather than on screen.
    """
    names = set()
    for path in _sources():
        with open(path, encoding="utf-8") as fh:
            for match in CALL_PATTERN.findall(fh.read()):
                # `tally_connector.api.export.export_` is the interpolation's
                # prefix, not a method.
                if not match.endswith("."):
                    names.add(match)
    names.discard("tally_connector.api.export.export_")
    names |= {f"tally_connector.api.export.export_{key}" for key, *_rest in AREAS}
    return sorted(names)


class TestEveryCallResolves(IntegrationTestCase):
    def test_the_sweep_finds_the_calls_at_all(self):
        """A sweep that matches nothing passes every assertion below it."""
        names = _called_methods()
        self.assertGreater(len(names), 15, f"only found {names}")
        self.assertIn("tally_connector.api.jobs.running", names)

    def test_every_method_the_dashboard_calls_exists(self):
        missing = []
        for name in _called_methods():
            try:
                frappe.get_attr(name)
            except Exception:
                missing.append(name)
        self.assertEqual(missing, [], "the dashboard calls methods that do not exist")

    def test_every_method_the_dashboard_calls_is_whitelisted(self):
        """A method that exists and is not whitelisted is a 403 on a button that
        looks exactly like a working one."""
        not_whitelisted = []
        for name in _called_methods():
            try:
                fn = frappe.get_attr(name)
            except Exception:
                continue
            if name not in frappe.whitelisted and getattr(fn, "__func__", fn) not in frappe.whitelisted:
                not_whitelisted.append(name)
        self.assertEqual(not_whitelisted, [])


class TestEveryRouteHasAPage(IntegrationTestCase):
    def test_each_route_names_a_file_that_exists(self):
        """A lazy import of a missing page is a blank screen at run time and a
        clean build - Vite resolves it only when somebody navigates there."""
        with open(os.path.join(FRONTEND, "router.ts"), encoding="utf-8") as fh:
            router = fh.read()
        pages = re.findall(r"import\('@/pages/([A-Za-z]+\.vue)'\)", router)
        self.assertGreaterEqual(len(pages), 8, f"only found {pages}")
        for page in pages:
            with self.subTest(page=page):
                self.assertTrue(
                    os.path.exists(os.path.join(FRONTEND, "pages", page)),
                    f"router points at pages/{page}, which does not exist",
                )

    def test_every_sidebar_destination_is_a_declared_route(self):
        """A sidebar entry pointing at a route name the router does not declare
        throws inside `router.resolve` during render, which takes the whole shell
        down rather than just that link."""
        with open(os.path.join(FRONTEND, "router.ts"), encoding="utf-8") as fh:
            declared = set(re.findall(r"name: '([A-Za-z]+)'", fh.read()))
        with open(os.path.join(FRONTEND, "App.vue"), encoding="utf-8") as fh:
            wanted = set(re.findall(r"\{ name: '([A-Za-z]+)' \}", fh.read()))
        self.assertTrue(wanted, "the sidebar sweep found no destinations")
        self.assertEqual(sorted(wanted - declared), [])


class TestTheAreasMatchTheSidebar(IntegrationTestCase):
    def test_every_area_the_server_declares_is_reachable_from_the_sidebar(self):
        """The sidebar's Masters and Transactions sections and the server's area
        table are one list said twice. Two lists is how one grows an entry the
        other does not have."""
        with open(os.path.join(FRONTEND, "App.vue"), encoding="utf-8") as fh:
            linked = set(re.findall(r"area\('([a-z_]+)'\)", fh.read()))
        declared = {key for key, *_rest in AREAS}
        self.assertEqual(sorted(declared - linked), [], "areas with no sidebar entry")
        self.assertEqual(sorted(linked - declared), [], "sidebar entries with no area")


class TestDatesAreLocal(IntegrationTestCase):
    """A date field that is off by one is worse than one that is empty.

    `new Date(...).toISOString().split('T')[0]` answers with the **UTC** date. At
    +05:30 that is the previous day until half past five in the morning, and it
    is why the range opened on 31 March when the code asked for 1 April - the
    first day of the financial year, rendered as the last day of the one before.
    Nothing failed; the form simply came up wrong, every time, for everybody.

    The rule is checkable, so it is checked: no date string in this application
    is derived from `toISOString`.
    """

    def test_no_date_is_derived_from_toisostring(self):
        """Only the *truncation* is the defect, not the call.

        The first version of this matched `toISOString` anywhere and failed on
        two honest uses: a full ISO timestamp stored as a marker for when the
        connection was last tested, which is never rendered as a calendar date,
        and this docstring. A guard that fails on correct code is one somebody
        deletes, so it matches what actually goes wrong - cutting an ISO string
        down to its date half - and it skips comments.
        """
        offenders = []
        for path in _sources():
            with open(path, encoding="utf-8") as fh:
                for number, line in enumerate(fh, 1):
                    stripped = line.strip()
                    if stripped.startswith(("*", "//", "/*")):
                        continue
                    if TRUNCATED_ISO.search(line):
                        offenders.append(f"{os.path.relpath(path, FRONTEND)}:{number}")
        self.assertEqual(
            offenders, [],
            "these cut a date out of an ISO string, which is UTC - use localDate()",
        )

    def test_the_range_opens_on_the_current_month(self):
        """The default somebody sees before they touch anything."""
        with open(os.path.join(FRONTEND, "stores", "app.ts"), encoding="utf-8") as fh:
            store = fh.read()
        self.assertIn("fromDate: monthStart()", store)
        self.assertIn("toDate: monthEnd()", store)


class TestEveryFindingHasWords(IntegrationTestCase):
    """A finding the screen has no words for renders its raw check name.

    The validator's names use spaces - `gst ledger`, `name collision`,
    `bill-wise` - and the first version of the screen's label table guessed at
    underscored ones. Eight of nineteen matched nothing, so those findings fell
    through to the internal string. It reads *plausibly*, which is exactly why it
    survived: nobody looks at "gst ledger" and thinks the label is missing.

    Two directions, because either alone leaves a hole. A check with no label
    shows an internal name to an accounts clerk; a label for a check that no
    longer exists is dead code that reads as coverage.
    """

    def _validator_checks(self):
        import ast

        path = os.path.join(APP, "tally", "validate.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        checks = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add" and len(node.args) >= 2):
                arg = node.args[1]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    checks.add(arg.value)
        return checks

    def _screen_labels(self):
        path = os.path.join(FRONTEND, "components", "FindingsTable.vue")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        block = re.search(r"const CHECK_LABELS[^{]*\{(.*?)\n\}", source, re.S).group(1)
        # Underscores and digits are in the class deliberately. Without them the
        # sweep could not see a key like `cost_centre` - which is the exact
        # spelling that was wrong here - so the stale-label check was blind to
        # the one mistake it exists to catch. Found by planting it: the plant
        # passed.
        return set(re.findall(r"^\s*'?([A-Za-z][A-Za-z0-9 _\-]*?)'?\s*:", block, re.M))

    def test_the_sweep_finds_both_sides(self):
        """Two empty sets agree perfectly and prove nothing."""
        self.assertGreater(len(self._validator_checks()), 12)
        self.assertGreater(len(self._screen_labels()), 12)

    def test_every_check_the_validator_emits_has_words(self):
        missing = sorted(self._validator_checks() - self._screen_labels())
        self.assertEqual(
            missing, [],
            "these checks would show their internal name to whoever reads the report",
        )

    def test_no_label_describes_a_check_that_cannot_happen(self):
        stale = sorted(self._screen_labels() - self._validator_checks())
        self.assertEqual(stale, [], "these labels describe checks the validator never emits")
