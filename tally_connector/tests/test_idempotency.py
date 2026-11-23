"""
Running the same export twice.

Tally has no upsert. `Create` adds a second voucher where it already holds one;
`Alter` fails on a voucher it has never seen. Neither is right for both, so what
protects the books is a stable correlation key on every object plus a duplicate
behaviour somebody chose - and this suite holds both.

What it does **not** prove is what Tally then does with the file. That needs a
Tally, and the report says so rather than implying otherwise.
"""

from xml.etree import ElementTree

import frappe
from frappe.tests import IntegrationTestCase

from tally_connector.tally.render import DUP_COMBINE, DUP_IGNORE, DUP_MODIFY
from tally_connector.utils.exporter import render_full_export


def _company():
	counts = {c: 0 for c in frappe.get_all("Company", pluck="name")}
	for doctype in ("Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry"):
		for company in counts:
			counts[company] += frappe.db.count(doctype, {"docstatus": 1, "company": company})
	return max(counts, key=counts.get)


class TestRepeatingAnExportChangesNothing(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = _company()
		cls.first = render_full_export(None, None, cls.company)
		cls.second = render_full_export(None, None, cls.company)

	def test_two_runs_produce_the_same_bytes(self):
		"""Anything that differs between two runs of unchanged data - a timestamp,
		a set iterated in hash order, a dict without a sort - is a diff nobody can
		review and a re-import Tally cannot match against what it already holds.
		"""
		self.assertEqual(len(self.first[0]), len(self.second[0]), "masters differ in length")
		self.assertEqual(self.first[0], self.second[0], "the masters file is not reproducible")
		self.assertEqual(self.first[1], self.second[1], "the vouchers file is not reproducible")

	def test_the_counts_are_the_same(self):
		self.assertEqual(self.first[2]["masters"], self.second[2]["masters"])
		self.assertEqual(self.first[2]["vouchers"], self.second[2]["vouchers"])

	def test_every_voucher_carries_a_correlation_key(self):
		"""The voucher number is what ties a Tally voucher to its ERPNext
		document, and what an `Alter` addresses one by. A voucher without it can
		only ever be a duplicate.

		It used to be `REMOTEID` too, removed on a wrong guess about a refusal
		whose real cause was a masters file that never finished.
		"""
		root = ElementTree.fromstring(self.first[1].encode("utf-8"))
		vouchers = root.findall(".//VOUCHER")
		self.assertGreater(len(vouchers), 100)
		missing = [v.get("VCHTYPE") for v in vouchers if not (v.findtext("VOUCHERNUMBER") or "").strip()]
		self.assertEqual(missing[:10], [])

	def test_no_two_vouchers_share_a_correlation_key(self):
		"""Two vouchers with one key is one of them overwriting the other on a
		re-import, or two copies - depending on the mode, and neither is wanted."""
		root = ElementTree.fromstring(self.first[1].encode("utf-8"))
		keys = [v.findtext("VOUCHERNUMBER") for v in root.findall(".//VOUCHER")]
		duplicates = sorted({k for k in keys if keys.count(k) > 1})
		self.assertEqual(duplicates[:5], [])

	def test_the_key_is_the_erpnext_document_name(self):
		"""Stable across exports, across benches and across a rename of anything
		else. A key derived from a position in the file would not be."""
		names = set(frappe.get_all("Sales Invoice", pluck="name"))
		names |= set(frappe.get_all("Purchase Invoice", pluck="name"))
		names |= set(frappe.get_all("Payment Entry", pluck="name"))
		names |= set(frappe.get_all("Journal Entry", pluck="name"))
		root = ElementTree.fromstring(self.first[1].encode("utf-8"))
		sample = root.findall(".//VOUCHER")[:20]
		self.assertTrue(sample)
		for v in sample:
			number = v.findtext("VOUCHERNUMBER")
			with self.subTest(voucher=number):
				self.assertIn(number, names)

	def test_no_voucher_writes_a_remoteid(self):
		"""Correlation is the voucher number. REMOTEID is not refused by Tally -
		a working import carried one - it is simply a second key for one fact."""
		root = ElementTree.fromstring(self.first[1].encode("utf-8"))
		claimed = [v.get("REMOTEID") for v in root.findall(".//VOUCHER") if v.get("REMOTEID")]
		self.assertEqual(claimed[:5], [])

	def test_the_masters_declare_nothing_about_duplicates_by_default(self):
		"""This asserted the opposite until 20 September, on the reasoning that
		leaving it out lets Tally decide for itself.

		What it actually declared was `@@DUPIGNORECOMBINE`, a token found in no
		TallyPrime export, no Tally sample and no working implementation - only
		in four files, all of them this connector's own output. A real import
		carrying it answered `Created: 9,626  Altered: 9,694` against a
		customer's live company, so what it bought was nine and a half thousand
		of somebody's own masters overwritten.

		Sending nothing is the only configuration with evidence behind it: the
		implementation that imported 16,261 vouchers sends no IMPORTDUPS."""
		self.assertNotIn("IMPORTDUPS", self.first[0])

	def test_each_duplicate_behaviour_reaches_the_file(self):
		"""A behaviour somebody chooses on purpose still has to reach Tally."""
		from tally_connector.tally.extract import extract
		from tally_connector.tally.render import render_masters

		export = extract(self.company, None, None)
		for behaviour in (DUP_MODIFY, DUP_COMBINE):
			xml = render_masters(export.company, export.masters, behaviour)
			root = ElementTree.fromstring(xml.encode("utf-8"))
			with self.subTest(behaviour=behaviour):
				self.assertEqual(root.findtext(".//IMPORTDUPS"), behaviour)
				self.assertRegex(behaviour, r"^@@DUP[A-Z]+$")

	def test_the_default_sends_no_directive_at_all(self):
		from tally_connector.tally.extract import extract
		from tally_connector.tally.render import render_masters

		export = extract(self.company, None, None)
		xml = render_masters(export.company, export.masters, DUP_IGNORE)
		self.assertIsNone(ElementTree.fromstring(xml.encode("utf-8")).findtext(".//IMPORTDUPS"))


class TestRunningTheJobTwice(IntegrationTestCase):
	def test_a_second_export_writes_a_second_log_and_not_a_second_set_of_files(self):
		"""Two people pressing Export is ordinary. Each run is its own log with
		its own attachments; neither overwrites the other's evidence, which is
		what an audit asks for."""
		from tally_connector.api.export import _create_log

		first = _create_log("Full Export", "Tally XML", None, None, _company())
		second = _create_log("Full Export", "Tally XML", None, None, _company())
		try:
			self.assertNotEqual(first, second)
		finally:
			for name in (first, second):
				frappe.delete_doc("Tally Export Log", name, force=True, ignore_permissions=True)
			frappe.db.commit()
