"""
Names Tally will actually accept.

Tally aborts an import at the first line it cannot parse, and reports only that
line. So one unusable name does not cost you one record - it costs the whole
file, and then every voucher afterwards fails for want of the masters. A real
import died on `Line 48: BAD UNIT NAME`, took all 744 masters with it, and the
voucher file that followed reported a missing ledger which was simply never
created.

The unit symbol is where this bites, because `/` is how Tally writes a compound
unit (`Box of 10 Nos`) and ERPNext ships 245 units as standard of which 51
contain `/` or other punctuation: Meter/Second, Btu/Hour, Gram/Litre,
"Pint, Dry (US)".
"""

import re

import frappe
from frappe.tests import IntegrationTestCase
from lxml import etree

from tally_connector.tally.extract import unit_name, units_in_use
from tally_connector.tally.extract_rules import UNIT_SYMBOL_MAX
from tally_connector.tests.inventory import inventory_sent
from tally_connector.utils.exporter import render_section

# Letters, digits, space, hyphen, underscore, full stop, per-cent. Anything else
# is what Tally has been observed to refuse or to reinterpret.
ILLEGAL_IN_UNIT = re.compile(r"[^A-Za-z0-9 \-_.%]")


def _company():
	return frappe.get_all("Company", pluck="name")[0]


class TestTallyAcceptsNames(IntegrationTestCase):
	def test_no_exported_unit_carries_a_character_tally_refuses(self):
		with inventory_sent():
			xml, _n = render_section("uoms", None, None, _company())
		names = [e.get("NAME") for e in etree.fromstring(xml.encode("utf-8")).iter("UNIT")]
		self.assertTrue(names, "No units exported - the check would be vacuous.")
		for name in names:
			with self.subTest(unit=name):
				self.assertIsNone(
					ILLEGAL_IN_UNIT.search(name),
					f"Unit {name!r} would abort the whole masters import at its line.",
				)

	def test_every_unit_a_stock_item_names_is_exported(self):
		"""The half that a character check alone would miss.

		Sanitising the symbol on the unit master and not on the item's BASEUNITS
		trades a refused import for a silently wrong one.
		"""
		company = _company()
		with inventory_sent():
			units_xml, _u = render_section("uoms", None, None, company)
			items_xml, item_count = render_section("stock_items", None, None, company)
		if not item_count:
			self.skipTest("No items on this site.")

		exported = {e.get("NAME") for e in etree.fromstring(units_xml.encode("utf-8")).iter("UNIT")}
		used = {e.text for e in etree.fromstring(items_xml.encode("utf-8")).iter("BASEUNITS") if e.text}
		self.assertEqual(
			sorted(used - exported),
			[],
			"These stock items name a unit the unit master does not create.",
		)

	def test_the_slash_that_caused_it_is_removed(self):
		"""`/` is Tally's compound-unit separator, so a symbol holding one is
		read as a compound unit and refused.

		This asserted `Meter Second` for as long as it existed - a symbol with
		a space in it, which is exactly what a real Tally then refused as
		*"BAD UNIT NAME"* on `Square Meter`. The slash rule was right and the
		expected value was the next defect.
		"""
		for uom in ("Meter/Second", "Pint, Dry (US)"):
			symbol = unit_name(uom)
			with self.subTest(uom=uom):
				self.assertNotIn("/", symbol)
				self.assertNotIn(" ", symbol)
				self.assertLessEqual(len(symbol), UNIT_SYMBOL_MAX)

	def test_the_units_tally_refused_are_now_symbols_it_takes(self):
		"""The two a real Tally refused, and the two it accepted."""
		from tally_connector.tally.extract import _unit_sanitise, _unit_shorten

		self.assertEqual(_unit_shorten(_unit_sanitise("Square Meter")), "SqMtr")
		self.assertEqual(_unit_shorten(_unit_sanitise("Parts Per Million")), "PPM")
		# Not `CM2`, which is what the first version produced and which reads
		# as square centimetres.
		self.assertEqual(_unit_shorten(_unit_sanitise("Cubic Meter")), "CuMtr")
		self.assertEqual(_unit_shorten(_unit_sanitise("Square Foot")), "SqFt")
		self.assertEqual(_unit_shorten(_unit_sanitise("Nos")), "Nos")
		self.assertEqual(_unit_shorten(_unit_sanitise("BAG")), "BAG")

	def test_no_two_uoms_share_a_symbol(self):
		"""Two UOMs shortening to one symbol would be one unit in Tally holding
		both their quantities - the stock-item collision, on units."""
		from tally_connector.tally.extract import _unit_symbols

		symbols = list(_unit_symbols().values())
		self.assertTrue(symbols)
		duplicates = sorted({s for s in symbols if symbols.count(s) > 1})
		self.assertEqual(duplicates, [])

	def test_every_symbol_on_this_site_is_one_tally_takes(self):
		from tally_connector.tally.extract import _unit_symbols

		bad = sorted(s for s in _unit_symbols().values() if " " in s or len(s) > UNIT_SYMBOL_MAX or "/" in s)
		self.assertEqual(bad, [])

	def test_a_unit_that_is_only_punctuation_still_gets_a_name(self):
		"""An empty symbol is refused too, so the fallback must not be blank.

		Asserted as a property rather than as the literal `Unit`. The fallback
		now avoids a symbol a registered UOM already took, so on a site that
		has one called `Unit` the answer is `Unit2` - which is the fix working,
		not a regression, and an equality assertion here would have read it as
		one."""
		fallback = unit_name("///")
		self.assertTrue(fallback)
		self.assertTrue(fallback.startswith("Unit"))
		self.assertNotRegex(fallback, r"[\s/]")
		self.assertEqual(unit_name("   "), "")

	def test_the_fallback_never_takes_a_symbol_a_real_unit_owns(self):
		"""A silently dropped duplicate meant an item and a unit master naming
		one symbol for two different units."""
		from tally_connector.tally.extract import _unit_symbols

		taken = set(_unit_symbols().values())
		for odd in ("///", "***", "@@@"):
			with self.subTest(uom=odd):
				self.assertNotIn(unit_name(odd), taken)

	def test_only_units_in_use_are_exported(self):
		"""ERPNext ships 245 units; a company uses a handful. Sending all of them
		is noise in somebody's ledger and 245 chances to hit a name Tally
		refuses."""
		total = frappe.db.count("UOM")
		used = units_in_use()
		if total <= 20:
			self.skipTest("This site does not carry the standard UOM list.")
		self.assertLess(
			len(used),
			total,
			f"All {total} units are being exported; only those in use should be.",
		)
		_xml, exported = render_section("uoms", None, None, _company())
		self.assertLessEqual(exported, len(used))
