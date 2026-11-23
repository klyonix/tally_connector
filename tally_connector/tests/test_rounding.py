"""Shifting a voucher by a paisa so Tally will take it, without losing money.

Tally computes an invoice's value from the stock lines and its own GST
arithmetic and refuses a party line that does not match, while never saying
what figure it wanted. The connector adopts Tally's total and books the
difference to the rounding ledger, accepting that the cost is each
customer's outstanding differing from ERPNext by up to a paisa per invoice.

What these hold is that the cost is exactly that and no more: the money is
conserved, the shift is bounded, and the original is always tried first.

Pure: no site, no Tally.
"""

from __future__ import annotations

import datetime
import unittest
from decimal import Decimal

from tally_connector.tally import model
from tally_connector.tally.rounding import MAX_PAISE, attempts


def _sale(party_amount="-210.00", with_bill=True):
	v = model.Voucher(kind="Sales", date=datetime.date(2026, 3, 31), number="STG-1", party="Acme")
	party = model.LedgerLine(ledger="Acme", amount=Decimal(party_amount), is_party=True)
	if with_bill:
		party.bill_refs.append(model.BillRef(name="STG-1", amount=Decimal(party_amount)))
	v.lines.append(party)
	v.lines.append(model.LedgerLine(ledger="Output Tax SGST", amount=Decimal("16.02")))
	v.lines.append(model.LedgerLine(ledger="Output Tax CGST", amount=Decimal("16.02")))
	v.inventory.append(
		model.InventoryLine(
			item="Grout",
			ledger="Sales",
			quantity=Decimal("3"),
			rate=Decimal("59.32"),
			amount=Decimal("177.96"),
			unit="KG",
		)
	)
	return v


class TestTheOriginalIsTriedFirst(unittest.TestCase):
	def test_the_first_attempt_is_the_voucher_itself(self):
		"""A voucher Tally would have taken anyway is never adjusted."""
		v = _sale()
		first, paise = next(iter(attempts(v)))
		self.assertIs(first, v)
		self.assertEqual(paise, Decimal("0"))


class TestMoneyIsConserved(unittest.TestCase):
	def test_every_variant_still_balances(self):
		for variant, _paise in attempts(_sale()):
			with self.subTest(paise=_paise):
				self.assertEqual(variant.imbalance(), Decimal("0"))

	def test_revenue_and_tax_are_untouched(self):
		for variant, paise in attempts(_sale()):
			if not paise:
				continue
			with self.subTest(paise=paise):
				self.assertEqual(
					[l.amount for l in variant.lines if "Tax" in l.ledger],
					[Decimal("16.02"), Decimal("16.02")],
				)
				self.assertEqual([i.amount for i in variant.inventory], [Decimal("177.96")])

	def test_what_leaves_the_party_arrives_at_the_rounding_ledger(self):
		for variant, paise in attempts(_sale()):
			if not paise:
				continue
			with self.subTest(paise=paise):
				party = next(l for l in variant.lines if l.is_party)
				rounding = next(l for l in variant.lines if l.ledger == "Rounded Off")
				self.assertEqual(party.amount + rounding.amount, Decimal("-210.00"))


class TestTheShiftIsBounded(unittest.TestCase):
	"""A voucher wrong for any other reason is refused rather than bent until
	it fits."""

	def test_no_variant_moves_more_than_two_paise(self):
		for _variant, paise in attempts(_sale()):
			with self.subTest(paise=paise):
				self.assertLessEqual(abs(paise) * 100, MAX_PAISE)

	def test_there_are_four_variants_and_no_more(self):
		self.assertEqual(len(list(attempts(_sale()))), 5)


class TestTheBillFollowsTheLine(unittest.TestCase):
	"""A bill reference that disagrees with its line sends the amount to On
	Account, and the ageing stops meaning anything."""

	def test_the_bill_reference_matches_the_party_line(self):
		for variant, paise in attempts(_sale()):
			if not paise:
				continue
			with self.subTest(paise=paise):
				party = next(l for l in variant.lines if l.is_party)
				self.assertEqual(sum(r.amount for r in party.bill_refs), party.amount)


class TestAwkwardShapes(unittest.TestCase):
	def test_a_credit_notes_party_keeps_its_sign(self):
		v = _sale(party_amount="210.00")
		v.kind = "Credit Note"
		for variant, paise in attempts(v):
			if not paise:
				continue
			with self.subTest(paise=paise):
				party = next(l for l in variant.lines if l.is_party)
				self.assertGreater(party.amount, 0)

	def test_a_voucher_with_no_party_offers_no_variants(self):
		v = model.Voucher(kind="Journal", date=datetime.date(2026, 3, 31), number="JV-1")
		v.lines.append(model.LedgerLine(ledger="A", amount=Decimal("-1")))
		v.lines.append(model.LedgerLine(ledger="B", amount=Decimal("1")))
		self.assertEqual(len(list(attempts(v))), 1)

	def test_an_existing_rounding_line_is_adjusted_not_duplicated(self):
		# Built rather than mutated: the lines are frozen, which is the point.
		v = _sale(party_amount="-209.99")
		v.lines.append(model.LedgerLine(ledger="Rounded Off", amount=Decimal("-0.01")))
		for variant, paise in attempts(v):
			if not paise:
				continue
			with self.subTest(paise=paise):
				self.assertLessEqual(len([l for l in variant.lines if l.ledger == "Rounded Off"]), 1)

	def test_the_original_voucher_is_never_mutated(self):
		v = _sale()
		before = [(l.ledger, l.amount) for l in v.lines]
		list(attempts(v))
		self.assertEqual([(l.ledger, l.amount) for l in v.lines], before)


class TestThePushUsesIt(unittest.TestCase):
	"""Wired into the push, and only when asked for."""

	def _run(self, accepts_at, round_to_tally=True):
		from unittest.mock import patch

		from tally_connector.utils import connection

		rows = [_sale()]
		seen = {"n": 0}

		def fake(xml, log=None):
			seen["n"] += 1
			ok = seen["n"] == accepts_at
			return {
				"success": ok,
				"reached": True,
				"unknown": False,
				"created": 1 if ok else 0,
				"altered": 0,
				"errors": [] if ok else ["1 error"],
			}

		with (
			patch.object(connection, "push_to_tally", side_effect=fake),
			patch.object(connection, "_split_by_what_tally_holds", return_value=([], rows)),
			patch.object(connection, "_record_batch", lambda *a, **k: None),
		):
			return connection.push_vouchers(
				"Co",
				rows,
				batch=10,
				keep_going=True,
				period=("2026-03-01", "2026-03-31"),
				erpnext_company="X",
				round_to_tally=round_to_tally,
			), seen["n"]

	def test_a_refusal_the_first_paisa_fixes_is_sent_and_named(self):
		out, calls = self._run(accepts_at=2)
		self.assertEqual(out["created"], 1)
		self.assertEqual(out["refused"], [])
		self.assertEqual([r["number"] for r in out["rounded"]], ["STG-1"])
		self.assertEqual(out["rounded"][0]["paise"], "0.01")
		self.assertEqual(calls, 2, "the original, then one variant")

	def test_a_voucher_no_paisa_fixes_is_still_refused(self):
		"""Bounded: wrong for another reason means refused, not bent."""
		out, calls = self._run(accepts_at=99)
		self.assertEqual(out["created"], 0)
		self.assertEqual([r["number"] for r in out["refused"]], ["STG-1"])
		self.assertEqual(out["rounded"], [])
		self.assertEqual(calls, 5, "the original and all four variants")

	def test_it_does_nothing_unless_asked(self):
		out, calls = self._run(accepts_at=2, round_to_tally=False)
		self.assertEqual(out["rounded"], [])
		self.assertEqual([r["number"] for r in out["refused"]], ["STG-1"])
		self.assertEqual(calls, 1, "it retried without being asked to")
