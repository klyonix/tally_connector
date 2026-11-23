"""
The file Tally is actually asked to read.

Tally aborts an import at the first line it cannot parse and reports only that
line, so a single bad character anywhere costs the whole file - the audited
import died on `Line 48: BAD UNIT NAME` and took all 744 masters with it. Every
check here is against XML generated from this bench's real data rather than a
fixture, because the characters that break an import are the ones a person typed
into a customer name three years ago.

The offline suite already proves these properties against constructed scenarios.
This proves them against the data that actually exists.
"""

import re
import unicodedata
from xml.etree import ElementTree

import frappe
from frappe.tests import IntegrationTestCase

from tally_connector.utils.exporter import render_full_export

# Everything XML 1.0 forbids outright. A file holding one of these is refused by
# any parser, Tally's included, however well-formed the rest of it is.
ILLEGAL = re.compile(r"[^\x09\x0A\x0D\x20-퟿-�\U00010000-\U0010FFFF]")

# What Tally reads as a number: a plain decimal, optionally negative. Anything in
# scientific notation, or carrying a separator, is not a number to Tally.
AMOUNT = re.compile(r"^-?\d+(\.\d{1,4})?$")
DATE = re.compile(r"^\d{8}$")


def _company():
    counts = {c: 0 for c in frappe.get_all("Company", pluck="name")}
    for doctype in ("Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry"):
        for company in counts:
            counts[company] += frappe.db.count(doctype, {"docstatus": 1, "company": company})
    return max(counts, key=counts.get)


class TestTheGeneratedFilesAreSound(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = _company()
        cls.masters_xml, cls.vouchers_xml, cls.counts = render_full_export(None, None, cls.company)
        cls.masters = ElementTree.fromstring(cls.masters_xml.encode("utf-8"))
        cls.vouchers = ElementTree.fromstring(cls.vouchers_xml.encode("utf-8"))

    def test_there_is_enough_in_the_files_to_prove_anything(self):
        self.assertGreater(self.counts["masters"], 50)
        self.assertGreater(self.counts["vouchers"], 100)

    def test_both_files_parse(self):
        """Well-formedness, stated first because nothing else matters without it."""
        self.assertEqual(self.masters.tag, "ENVELOPE")
        self.assertEqual(self.vouchers.tag, "ENVELOPE")

    def test_no_illegal_character_reaches_the_file(self):
        """A control byte in a customer's name aborts the import at that line and
        takes everything after it. It is invisible in every screen that shows the
        name, which is why it survives to here."""
        for label, xml in (("masters", self.masters_xml), ("vouchers", self.vouchers_xml)):
            found = ILLEGAL.findall(xml)
            with self.subTest(document=label):
                self.assertEqual(
                    [f"U+{ord(c):04X}" for c in found[:5]], [],
                    f"{len(found)} characters XML forbids are in the {label} file",
                )

    def test_markup_characters_are_escaped(self):
        """`M/S A & B` is a real supplier name and a bare `&` ends the import.

        Proved by parsing rather than by reading the bytes: a parser that got
        this far has already accepted every entity in the file, so the check is
        that a name holding markup came back out intact.
        """
        risky = [name for name in
                 (el.get("NAME") for el in self.masters.iter() if el.get("NAME"))
                 if any(c in name for c in "&<>\"'")]
        if not risky:
            self.skipTest("no master on this bench has a markup character in its name")
        # It parsed, so the escaping held. Assert the value survived unmangled.
        for name in risky[:20]:
            with self.subTest(name=name):
                self.assertNotIn("&amp;", name, "double-escaped: the name carries a literal entity")
                self.assertNotIn("&lt;", name)

    def test_every_amount_is_a_number_tally_can_read(self):
        """Scientific notation is what a float does to a very small residual, and
        Tally stops at the line rather than reading it as zero."""
        bad = []
        for tag in ("AMOUNT", "RATE", "BILLEDQTY", "ACTUALQTY"):
            for el in self.vouchers.iter(tag):
                text = (el.text or "").strip()
                if not text:
                    continue
                # RATE and quantities carry a unit: "1000.00/Nos", "10.00 Nos".
                head = text.split("/")[0].split(" ")[0]
                if not AMOUNT.match(head):
                    bad.append(f"{tag}={text!r}")
        self.assertEqual(bad[:10], [], f"{len(bad)} values are not numbers Tally can read")

    def test_no_amount_carries_more_precision_than_the_books(self):
        """Two decimal places, because that is what ERPNext posted. A third is a
        float artefact and puts a rounding difference into somebody's ledger."""
        bad = [
            (el.text or "").strip()
            for el in self.vouchers.iter("AMOUNT")
            if (el.text or "").strip() and "." in el.text
            and len(el.text.strip().split(".")[1]) > 2
        ]
        self.assertEqual(bad[:10], [])

    def test_every_date_is_the_format_tally_parses(self):
        bad = []
        for tag in ("DATE", "EFFECTIVEDATE", "BILLDATE"):
            for el in self.vouchers.iter(tag):
                text = (el.text or "").strip()
                if text and not DATE.match(text):
                    bad.append(f"{tag}={text!r}")
        self.assertEqual(bad[:10], [])

    def test_a_negative_amount_is_written_as_a_negative(self):
        """Tally reads the sign as the side of the entry. A minus rendered as
        parentheses, or dropped, reverses a posting silently."""
        amounts = [(el.text or "").strip() for el in self.vouchers.iter("AMOUNT")]
        negatives = [a for a in amounts if a.startswith("-")]
        self.assertTrue(negatives, "no debit in the whole file - the sign is being lost")
        for a in amounts:
            with self.subTest(amount=a):
                self.assertNotIn("(", a)
                self.assertNotIn(",", a)

    def test_no_element_is_empty_where_tally_needs_a_value(self):
        """An empty LEDGERNAME is a line posted to nothing. Tally refuses it, and
        an export of eleven thousand vouchers refused for one blank is the whole
        file."""
        for tag in ("LEDGERNAME", "VOUCHERTYPENAME", "VOUCHERNUMBER", "DATE"):
            blanks = [el for el in self.vouchers.iter(tag) if not (el.text or "").strip()]
            with self.subTest(tag=tag):
                self.assertEqual(len(blanks), 0, f"{len(blanks)} blank <{tag}>")

    def test_every_ledger_a_voucher_names_is_created_by_the_masters(self):
        """Closure, against real data. Every unmatched name is a line Tally
        refuses, and the masters file is the only thing that can create it."""
        created = {
            el.get("NAME") for el in self.masters.iter()
            if el.tag in ("LEDGER", "GROUP") and el.get("NAME")
        }
        referenced = {
            (el.text or "").strip() for el in self.vouchers.iter("LEDGERNAME")
            if (el.text or "").strip()
        }
        referenced |= {
            (el.text or "").strip() for el in self.vouchers.iter("PARTYLEDGERNAME")
            if (el.text or "").strip()
        }
        self.assertEqual(sorted(referenced - created)[:10], [])

    def test_every_stock_item_and_unit_a_voucher_names_is_created(self):
        items = {el.get("NAME") for el in self.masters.iter("STOCKITEM") if el.get("NAME")}
        units = {el.get("NAME") for el in self.masters.iter("UNIT") if el.get("NAME")}
        used_items = {(el.text or "").strip() for el in self.vouchers.iter("STOCKITEMNAME")}
        used_items.discard("")
        self.assertEqual(sorted(used_items - items)[:10], [])

        used_units = set()
        for tag in ("ACTUALQTY", "BILLEDQTY"):
            for el in self.vouchers.iter(tag):
                text = (el.text or "").strip()
                if " " in text:
                    used_units.add(text.split(" ", 1)[1].strip())
        for el in self.vouchers.iter("RATE"):
            text = (el.text or "").strip()
            if "/" in text:
                used_units.add(text.split("/", 1)[1].strip())
        used_units.discard("")
        self.assertEqual(sorted(used_units - units)[:10], [])

    def test_no_two_masters_share_a_name(self):
        """Two ledgers with one name is one ledger in Tally holding both
        balances, and no error anywhere."""
        for tag in ("LEDGER", "GROUP", "STOCKITEM", "UNIT"):
            names = [el.get("NAME") for el in self.masters.iter(tag) if el.get("NAME")]
            duplicates = sorted({n for n in names if names.count(n) > 1})
            with self.subTest(tag=tag):
                self.assertEqual(duplicates[:5], [])

    def test_no_name_differs_from_another_only_by_whitespace(self):
        """Whitespace at the end of a Tally name is invisible and never
        meaningful, and two names differing only by it are two ledgers."""
        names = [el.get("NAME") for el in self.masters.iter() if el.get("NAME")]
        stripped = {}
        for name in names:
            stripped.setdefault(name.strip(), set()).add(name)
        collisions = {k: sorted(v) for k, v in stripped.items() if len(v) > 1}
        self.assertEqual(collisions, {})

    def test_no_name_carries_a_character_tally_reserves(self):
        """Tally reads `/` as its compound-unit separator, so a unit symbol
        holding one is refused. The audited import died on exactly that."""
        bad = [el.get("NAME") for el in self.masters.iter("UNIT")
               if el.get("NAME") and "/" in el.get("NAME")]
        self.assertEqual(bad, [])

    def test_a_long_narration_does_not_break_the_file(self):
        """Bank statement narrations run to hundreds of characters and arrive
        from a bank rather than from anybody who checked them."""
        longest = max(
            ((el.text or "") for el in self.vouchers.iter("NARRATION")),
            key=len, default="",
        )
        if not longest:
            self.skipTest("no narration in this export")
        self.assertNotRegex(longest, ILLEGAL.pattern)
        # Normalised so a decomposed accent cannot arrive as a stray combining
        # mark against a name Tally already holds in composed form.
        self.assertEqual(longest, unicodedata.normalize("NFC", longest))

    def test_each_file_asks_for_its_own_import(self):
        """`All Masters` for masters and `Vouchers` for vouchers, which is what
        Tally documents and what a working import of 16,261 vouchers used.

        For two days this asserted `All Masters` on both, after the envelope of
        a Tally *export* was read as evidence about the import request and
        `Vouchers` was said to import nothing. It was wrong; the vouchers
        imports were failing because the masters had never finished.
        """
        self.assertEqual(self.masters.findtext(".//REPORTNAME"), "All Masters")
        self.assertEqual(self.vouchers.findtext(".//REPORTNAME"), "Vouchers")

    def test_the_two_files_still_hold_different_things(self):
        """What separates them is the order they are imported in, not the
        request - so nothing may ride in the wrong envelope."""
        self.assertEqual(self.masters.findall(".//VOUCHER"), [])
        self.assertTrue(self.vouchers.findall(".//VOUCHER"))


class TestRichTextDoesNotReachTally(IntegrationTestCase):
    """ERPNext's `Item.description` is a Text Editor field, so it holds HTML.

    Found on a real chart of 9,423 items: 93 carried markup and 37 more carried
    an entity, all of which reached the file verbatim and would have shown in
    Tally as a literal `<div><p>...</p></div>`. Nothing refuses the import - it
    simply reads as rubbish on every affected item, permanently.
    """

    def test_the_converter_removes_markup_and_keeps_the_words(self):
        from tally_connector.tally.extract import _plain_text

        cases = [
            ("<div><p>WIDGET 600X300 GLOSS (8 PCS)</p></div>",
             "WIDGET 600X300 GLOSS (8 PCS)"),
            ("plain text, untouched", "plain text, untouched"),
            ("", ""),
            (None, ""),
            # Two blocks must not run into one word.
            ("<p>FIRST</p><p>SECOND</p>", "FIRST SECOND"),
            ("<p>A</p><br><p>B</p>", "A B"),
            # Entities resolve, and an ampersand survives as an ampersand - the
            # renderer escapes it again on the way out.
            ("M/S A &amp; B", "M/S A & B"),
            ("&nbsp;spaced&nbsp;out&nbsp;", "spaced out"),
            # A bare comparison is not markup.
            ("width < 600mm", "width < 600mm"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(_plain_text(raw), expected)

    def test_no_description_in_a_real_export_carries_markup(self):
        import re as _re

        from tally_connector.utils.exporter import render_full_export

        masters_xml, _v, _c = render_full_export(None, None, _company())
        root = ElementTree.fromstring(masters_xml.encode("utf-8"))
        tag = _re.compile(r"<[a-zA-Z/][^>]*>")
        offenders = [
            (el.text or "")[:80] for el in root.iter("DESCRIPTION")
            if tag.search(el.text or "")
        ]
        self.assertEqual(offenders[:5], [])


class TestTwoItemsStayTwoItems(IntegrationTestCase):
    """Tally identifies a stock item by its name and nothing else.

    So two ERPNext items sharing an `item_name` are one item in Tally holding
    both their stock: a white variant counted as the grey one, and an 8-piece
    pack as an 18-piece. Two items are two items, and nothing is merged.

    The fix is not to stop emitting both - emitting two masters with one name
    leaves Tally to merge them anyway, and silently. It is to make the names
    distinct, from the item code, which is the only thing ERPNext holds that
    tells them apart.
    """

    def _codes(self):
        return [f"KX-TALLY-PROBE-{n}" for n in ("A", "B")]

    def setUp(self):
        self.shared = "KX Tally Probe Item"
        for code in self._codes():
            if frappe.db.exists("Item", code):
                continue
            doc = frappe.new_doc("Item")
            doc.item_code = code
            # The defect being reproduced: two codes, one name.
            doc.item_name = self.shared
            doc.item_group = frappe.get_all("Item Group", limit=1, pluck="name")[0]
            doc.stock_uom = "Nos"
            doc.is_stock_item = 0
            # `india_compliance` demands an HSN code from its own controller
            # while the Custom Field says `reqd = 0`, so reading the meta cannot
            # see it. Taken from the site rather than invented, because it is a
            # Link and a made-up code does not exist.
            if frappe.db.has_column("Item", "gst_hsn_code"):
                hsn = frappe.db.get_value("GST HSN Code", {}, "name")
                if hsn:
                    doc.gst_hsn_code = hsn
            doc.insert(ignore_permissions=True)
        frappe.db.commit()
        frappe.local._tally_connector_item_names = None

    def tearDown(self):
        for code in self._codes():
            if frappe.db.exists("Item", code):
                frappe.delete_doc("Item", code, force=True, ignore_permissions=True)
        frappe.db.commit()
        frappe.local._tally_connector_item_names = None

    def test_two_items_sharing_a_name_get_two_names(self):
        from tally_connector.tally.extract import stock_item_name

        a, b = (stock_item_name(code) for code in self._codes())
        self.assertNotEqual(a, b, "two items resolved to one Tally name")
        self.assertEqual({a, b}, set(self._codes()),
                         "a colliding pair should fall back to its item codes")

    def test_an_item_with_a_name_of_its_own_keeps_it(self):
        """The fallback applies to the collision and to nothing else. Renaming
        every item to its code would rename thousands for the sake of seven."""
        from tally_connector.tally.extract import stock_item_name

        # An item whose code and name actually differ, so the assertion has
        # something to say. Picking the first item and skipping when they agree
        # made this test skip itself on most benches.
        row = next(
            (r for r in frappe.get_all("Item", fields=["name", "item_name"],
                                       limit_page_length=0)
             if r.item_name and r.item_name.strip() != r.name
             and r.name not in self._codes()),
            None,
        )
        if not row:
            self.skipTest("every item on this bench is named after its own code")
        self.assertEqual(stock_item_name(row.name), row.item_name.strip())

    def test_both_items_reach_the_masters_and_neither_is_merged(self):
        from tally_connector.tally.extract import extract_masters

        names = [i.name for i in extract_masters(_company()).stock_items]
        for code in self._codes():
            with self.subTest(code=code):
                self.assertIn(code, names)
        self.assertNotIn(self.shared, names,
                         "the shared name should not be used by either item")

    def test_the_master_and_a_voucher_line_agree_on_the_name(self):
        """The two are resolved by one function for exactly this reason: a name
        decided one way on the master and another on a voucher is what Tally
        answers `Stock Item does not exist` to."""
        from tally_connector.tally.extract import extract_masters, stock_item_name

        names = {i.name for i in extract_masters(_company()).stock_items}
        for code in self._codes():
            with self.subTest(code=code):
                self.assertIn(stock_item_name(code, "a stale line name"), names)
