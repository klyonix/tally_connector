"""
What a second import does.

Tally has no upsert, and the honest consequence is that no single mode is right
for both a first import and a re-import:

  * `Create` always creates. Importing the same file twice makes every voucher
    twice, and the only thing that stops it is the *target's* configuration -
    the voucher type numbered Manually with Prevent Duplicates set.
  * `Alter` addresses a voucher that already exists, by voucher number, and
    fails on one Tally has never seen.

So the mode is a deliberate choice rather than a default nobody sees, the
masters carry an explicit duplicate behaviour instead of relying on whatever the
target happens to be set to, and `check_target_company` reports whether the
destination would catch a repeat.
"""

import unittest

from lxml import etree

from tally_connector.tally import golden
from tally_connector.tally.render import (
	ALTER,
	CREATE,
	DUP_COMBINE,
	DUP_IGNORE,
	DUP_MODIFY,
	render_masters,
	render_vouchers,
)
from tally_connector.tally.validate import TargetCompany, validate


class TestDuplicateBehaviour(unittest.TestCase):
	def test_masters_declare_nothing_about_duplicates_by_default(self):
		"""The reverse of what this asserted until 20 September.

		The claim was that leaving the directive out lets the target's own
		setting rewrite a chart somebody has corrected by hand. True in
		principle; what was actually being sent was `@@DUPIGNORECOMBINE`, which
		exists in no Tally artefact, and a real import carrying it altered
		9,694 of a customer's own masters. An instruction Tally does not
		recognise is not protection."""
		masters = render_masters("Co", golden.load("sale_domestic_gst").masters)
		self.assertIsNone(etree.fromstring(masters.encode("utf-8")).findtext(".//IMPORTDUPS"))

	def test_each_behaviour_somebody_chooses_reaches_the_envelope(self):
		for behaviour in (DUP_MODIFY, DUP_COMBINE):
			with self.subTest(behaviour=behaviour):
				xml = render_masters("Co", golden.load("sale_domestic_gst").masters, behaviour)
				self.assertEqual(etree.fromstring(xml.encode("utf-8")).findtext(".//IMPORTDUPS"), behaviour)

	def test_the_behaviour_sits_beside_the_company_not_instead_of_it(self):
		xml = render_masters("Demo Traders Pvt Ltd", golden.load("sale_domestic_gst").masters, DUP_MODIFY)
		root = etree.fromstring(xml.encode("utf-8"))
		self.assertEqual(root.findtext(".//SVCURRENTCOMPANY"), "Demo Traders Pvt Ltd")
		self.assertEqual(root.findtext(".//IMPORTDUPS"), DUP_MODIFY)


class TestVoucherAction(unittest.TestCase):
	def _vouchers(self, action):
		xml = render_vouchers("Co", golden.load("sale_domestic_gst").vouchers, action)
		return etree.fromstring(xml.encode("utf-8"))

	def test_create_is_the_default_and_addresses_nothing(self):
		voucher = next(self._vouchers(CREATE).iter("VOUCHER"))
		self.assertEqual(voucher.get("ACTION"), CREATE)
		self.assertIsNone(voucher.get("TAGNAME"))
		self.assertIsNone(voucher.get("TAGVALUE"))

	def test_alter_addresses_the_voucher_by_its_number(self):
		"""An alteration that does not say which voucher it alters is not one."""
		voucher = next(self._vouchers(ALTER).iter("VOUCHER"))
		self.assertEqual(voucher.get("ACTION"), ALTER)
		self.assertEqual(voucher.get("TAGNAME"), "Voucher Number")
		self.assertEqual(voucher.get("TAGVALUE"), voucher.findtext("VOUCHERNUMBER"))

	def test_the_correlation_key_survives_both_modes(self):
		"""The voucher number ties a Tally voucher back to its ERPNext document,
		whichever way the file was imported.

		It used to be `REMOTEID` as well. That was removed on a wrong guess
		about why the vouchers were refused - they were refused because the
		masters had never finished - and a later working import carried an
		ERPNext name in REMOTEID without complaint. It is not needed: the voucher
		number is the same document name.
		"""
		for action in (CREATE, ALTER):
			with self.subTest(action=action):
				voucher = next(self._vouchers(action).iter("VOUCHER"))
				self.assertEqual(voucher.findtext("VOUCHERNUMBER"), "SINV-0001")

	def test_no_voucher_writes_a_remoteid(self):
		"""Not because Tally refuses one - a working import carried one - but
		because correlation is the voucher number, and two keys for one fact is
		how they come to disagree. This holds the decision in place."""
		for name in golden.scenarios():
			for action in (CREATE, ALTER):
				xml = render_vouchers(golden.load(name).company, golden.load(name).vouchers, action)
				root = etree.fromstring(xml.encode("utf-8"))
				with self.subTest(scenario=name, action=action):
					self.assertEqual(
						[v.get("REMOTEID") for v in root.iter("VOUCHER") if v.get("REMOTEID")], []
					)

	def test_every_voucher_carries_a_stable_correlation_key(self):
		for name in golden.scenarios():
			export = golden.load(name)
			for voucher in export.vouchers:
				if not voucher.remote_id:
					continue
				with self.subTest(scenario=name, voucher=voucher.number):
					# The number is the document name, so what an Alter
					# addresses and what the model correlates on are one value.
					self.assertEqual(voucher.remote_id, voucher.number)


class TestReimportWarning(unittest.TestCase):
	"""Half the answer is in the file and half is in the target.

	It used to be all target: the check reported the destination's numbering
	setting and said the duplication could not be prevented from here. It can.
	Every voucher carries a GUID derived from its ERPNext document, and
	TallyPrime's "Overwrite voucher when a voucher with same GUID exists" reads
	it - so what the file itself must get right is now checked here too.
	"""

	def test_nothing_is_claimed_without_a_target(self):
		report = validate(golden.load("sale_domestic_gst"))
		self.assertEqual([f for f in report.findings if f.check == "re-import"], [])

	def test_a_voucher_naming_no_document_is_reported(self):
		"""With nothing to derive a GUID from, Tally has nothing to match on."""
		export = golden.load("sale_domestic_gst")
		for voucher in export.vouchers:
			voucher.remote_id = None
			voucher.guid = ""
		hits = [f for f in validate(export).findings if f.check == "re-import"]
		self.assertTrue(hits)
		self.assertIn("GUID", hits[0].detail)

	def test_two_vouchers_never_share_one_guid(self):
		export = golden.load("sale_domestic_gst")
		guids = [v.tally_guid for v in export.vouchers]
		self.assertTrue(all(guids))
		self.assertEqual(len(set(guids)), len(guids))

	def test_the_guid_is_the_same_every_export(self):
		"""A fresh value each run duplicates exactly as having none does."""
		first = [v.tally_guid for v in golden.load("sale_domestic_gst").vouchers]
		second = [v.tally_guid for v in golden.load("sale_domestic_gst").vouchers]
		self.assertEqual(first, second)

	def test_a_type_that_does_not_prevent_duplicates_is_reported(self):
		target = TargetCompany(
			name="Demo", known=True, voucher_types={"Sales"}, duplicate_guard={"Sales": False}
		)
		report = validate(golden.load("sale_domestic_gst"), target)
		hits = [f for f in report.findings if f.check == "re-import"]
		self.assertTrue(hits)
		self.assertIn("GUID", hits[0].detail)

	def test_a_guarded_type_is_not_reported(self):
		target = TargetCompany(
			name="Demo", known=True, voucher_types={"Sales"}, duplicate_guard={"Sales": True}
		)
		report = validate(golden.load("sale_domestic_gst"), target)
		self.assertEqual([f for f in report.findings if f.check == "re-import"], [])

	def test_an_unanswered_type_is_not_guessed_at(self):
		"""Tally answering without saying is not the same as answering no."""
		target = TargetCompany(
			name="Demo", known=True, voucher_types={"Sales"}, duplicate_guard={"Sales": None}
		)
		report = validate(golden.load("sale_domestic_gst"), target)
		self.assertEqual([f for f in report.findings if f.check == "re-import"], [])


if __name__ == "__main__":
	unittest.main()
