"""
The package carries exactly what the single files held, in the order Tally
needs, and can prove it.

Built from the golden scenarios, so the vouchers are what the renderer really
writes: twelve of them across April, May and June 2026.

Pure: no site.
"""

from __future__ import annotations

import io
import json
import unittest
import zipfile

from lxml import etree

from tally_connector.tally import golden, package
from tally_connector.tally.render import render_masters, render_vouchers

COMPANY = "Demo Traders Pvt Ltd"


def _export():
	vouchers, masters = [], None
	for name in golden.scenarios():
		export = golden.load(name)
		vouchers.extend(export.vouchers)
		masters = masters or export.masters
	return (
		render_masters(COMPANY, masters).encode("utf-8"),
		render_vouchers(COMPANY, vouchers).encode("utf-8"),
	)


# Indentation is not content. Canonical XML would say so too, but it refuses
# the relative namespace URI Tally's own `xmlns:UDF="TallyUDF"` uses.
_PLAIN = etree.XMLParser(remove_blank_text=True)


def _messages(xml: bytes):
	"""Each TALLYMESSAGE with its indentation removed, so formatting cannot hide a change."""
	root = etree.fromstring(xml, _PLAIN)
	return sorted(etree.tostring(m) for m in root.iter("TALLYMESSAGE"))


def _header(xml: bytes):
	root = etree.fromstring(xml, _PLAIN)
	return etree.tostring(root.find("HEADER")), etree.tostring(root.find("BODY/IMPORTDATA/REQUESTDESC"))


META = {
	"erpnext_company": "Demo Traders",
	"tally_company": COMPANY,
	"from_date": "2026-04-01",
	"to_date": "2026-06-30",
}


class TestSplittingByMonth(unittest.TestCase):
	def setUp(self):
		self.masters, self.vouchers = _export()

	def test_every_voucher_is_in_exactly_one_file_unchanged(self):
		parts = package.split_vouchers(self.vouchers)
		moved = sorted(m for part in parts for m in _messages(part["content"]))
		self.assertEqual(moved, _messages(self.vouchers))

	def test_one_file_per_month_in_date_order(self):
		parts = package.split_vouchers(self.vouchers)
		self.assertEqual([p["period"] for p in parts], ["2026-04", "2026-05", "2026-06"])
		for part in parts:
			self.assertTrue(part["first"].startswith(part["period"]))
			self.assertTrue(part["last"].startswith(part["period"]))

	def test_every_file_keeps_the_request_header(self):
		original = _header(self.vouchers)
		for part in package.split_vouchers(self.vouchers):
			self.assertEqual(_header(part["content"]), original)

	def test_a_large_month_is_split_into_parts(self):
		parts = package.split_vouchers(self.vouchers, limit=2)
		april = [p for p in parts if p["period"] == "2026-04"]
		self.assertGreater(len(april), 1)
		self.assertEqual([p["part"] for p in april], list(range(1, len(april) + 1)))
		self.assertTrue(all(p["records"] <= 2 for p in parts))

	def test_a_file_that_is_not_a_voucher_import_is_refused(self):
		with self.assertRaisesRegex(ValueError, "REQUESTDATA"):
			package.split_vouchers(b"<ENVELOPE><HEADER/></ENVELOPE>")


class TestThePackage(unittest.TestCase):
	def setUp(self):
		self.masters, self.vouchers = _export()
		self.zipped, self.manifest = package.build(self.masters, self.vouchers, META)
		self.archive = zipfile.ZipFile(io.BytesIO(self.zipped))

	def tearDown(self):
		self.archive.close()

	def test_masters_come_first_and_files_sort_in_import_order(self):
		names = [f["name"] for f in self.manifest["files"]]
		self.assertEqual(names[0], "01_masters.xml")
		self.assertEqual(names, sorted(names))
		self.assertEqual(
			names[1:],
			["02_vouchers_2026-04.xml", "03_vouchers_2026-05.xml", "04_vouchers_2026-06.xml"],
		)

	def test_the_manifest_accounts_for_every_record(self):
		self.assertEqual(self.manifest["totals"]["vouchers"], self.vouchers.count(b"<TALLYMESSAGE"))
		self.assertEqual(self.manifest["totals"]["masters"], self.masters.count(b"<TALLYMESSAGE"))
		self.assertEqual(self.manifest["tally_company"], COMPANY)

	def test_the_manifest_in_the_zip_is_the_one_returned(self):
		self.assertEqual(json.loads(self.archive.read(package.MANIFEST)), self.manifest)

	def test_the_masters_file_is_the_one_exported(self):
		self.assertEqual(self.archive.read("01_masters.xml"), self.masters)

	def test_it_is_compressed(self):
		self.assertLess(len(self.zipped), len(self.masters) + len(self.vouchers))

	def test_the_readme_names_every_file_in_order(self):
		text = self.archive.read(package.README).decode("utf-8")
		positions = [text.index(f["name"]) for f in self.manifest["files"]]
		self.assertEqual(positions, sorted(positions))
		self.assertIn("does not import from a zip", text)
		self.assertIn(self.manifest["id"], text)

	def test_each_package_has_its_own_id(self):
		_zipped, again = package.build(self.masters, self.vouchers, META)
		self.assertNotEqual(again["id"], self.manifest["id"])

	def test_an_export_with_no_vouchers_carries_only_masters(self):
		empty = render_vouchers(COMPANY, []).encode("utf-8")
		_zipped, manifest = package.build(self.masters, empty, META)
		self.assertEqual([f["kind"] for f in manifest["files"]], ["masters"])
		self.assertEqual(manifest["totals"]["vouchers"], 0)

	def test_an_excel_export_is_packaged_whole(self):
		_zipped, manifest = package.build(b"masters-workbook", b"vouchers-workbook", META, fmt=package.EXCEL)
		self.assertEqual([f["name"] for f in manifest["files"]], ["01_masters.xlsx", "02_vouchers.xlsx"])
		self.assertNotIn("totals", manifest)


class TestVerifying(unittest.TestCase):
	def setUp(self):
		masters, vouchers = _export()
		self.zipped, self.manifest = package.build(masters, vouchers, META)

	def _rewrite(self, change):
		"""The same package with one member changed, removed or added."""
		source = zipfile.ZipFile(io.BytesIO(self.zipped))
		out = io.BytesIO()
		with zipfile.ZipFile(out, "w") as target:
			for name in source.namelist():
				content = change(name, source.read(name))
				if content is not None:
					target.writestr(name, content)
			if change("__extra__", None) is not None:
				target.writestr("stray.xml", b"<x/>")
		return out.getvalue()

	def test_a_package_as_built_verifies(self):
		self.assertEqual(package.verify(self.zipped), [])

	def test_a_damaged_file_is_named(self):
		damaged = self._rewrite(
			lambda name, data: data.replace(b"<DATE>", b"<DATE >", 1)
			if name == "02_vouchers_2026-04.xml"
			else data
		)
		problems = package.verify(damaged)
		self.assertTrue(any("02_vouchers_2026-04.xml" in p for p in problems), problems)

	def test_a_missing_file_is_named(self):
		missing = self._rewrite(lambda name, data: None if name == "03_vouchers_2026-05.xml" else data)
		self.assertTrue(
			any("03_vouchers_2026-05.xml" in p and "missing" in p for p in package.verify(missing))
		)

	def test_a_file_the_manifest_does_not_list_is_named(self):
		extra = self._rewrite(lambda name, data: b"" if name == "__extra__" else data)
		self.assertTrue(any("stray.xml" in p for p in package.verify(extra)))

	def test_something_that_is_not_a_zip_is_refused_in_words(self):
		self.assertEqual(package.verify(b"not a zip"), ["This is not a zip file, or it is damaged."])


if __name__ == "__main__":
	unittest.main()
