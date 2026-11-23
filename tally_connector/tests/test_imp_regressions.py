"""
One test per failure TallyPrime actually reported, from the customer's own logs.

`tally.imp` and `tally (1).imp` record 57 import runs between them. They are
**mixed logs**: they carry imports from this connector, from a different app,
and from the customer importing Tally's own exports into their own companies.
Attributing all of it here would be wrong, so each class below names the file
that caused it.

The rule these exist for: a failure that cost a round trip to a customer never
comes back silently. Each test fails if its defect is reinstated.

Not covered here, deliberately, because they are not ours:
  - `The Closing Balance date is Out of Range for Stock in Hand ledger` - the
    customer splitting their own Tally data, five runs against `Master.xml`.
  - `Unit 'Nos' already exists!` and a blank `BAD UNIT NAME` - the other app's
    `tally_masters_*.xml`.
  - `Ledger 'Sales - ABCD' does not exist!` - the other app, exporting account
    names with the company abbreviation while its masters created them without.

Pure: no site, no database.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from frappe.tests import UnitTestCase

from tally_connector.tally import model
from tally_connector.tally.extract_rules import UNIT_SYMBOL_MAX
from tally_connector.tally.render import render_masters
from tally_connector.tally.validate import ABORTS, REJECTS, validate
from tally_connector.utils.ledgers import is_reserved_group, reserved_group


def _export(**parts):
	export = model.Export(company="Demo Traders")
	for name, value in parts.items():
		setattr(export.masters, name, value)
	return export


def _checks(export):
	return {f.check for f in validate(export).findings}


class TestUnitSymbolTallyRefuses(UnitTestCase):
	"""`ERROR: Unit: Square Meter - BAD UNIT NAME`, and the same for
	`Parts Per Million`. Our `masters_*4e3c1f.xml` and `01_masters_*c5129b.xml`.

	A space in a unit symbol is refused, and `/` is worse: it is Tally's
	compound-unit separator, so `Meter/Second` reads as a compound unit.
	One refused unit aborted the whole masters file and took every ledger
	after it, which is what made the vouchers fail for missing ledgers.
	"""

	def test_a_symbol_with_a_space_is_refused_before_it_is_sent(self):
		export = _export(units=[model.Unit(symbol="Square Meter")])
		findings = [f for f in validate(export).findings if f.check == "unit symbol"]
		self.assertTrue(findings)
		self.assertEqual({f.severity for f in findings}, {ABORTS})

	def test_the_sanitiser_gives_square_meter_a_symbol_tally_accepts(self):
		from tally_connector.tally.extract import _unit_sanitise, _unit_shorten

		for name in ("Square Meter", "Parts Per Million", "Meter/Second"):
			symbol = _unit_shorten(_unit_sanitise(name))
			with self.subTest(uom=name):
				self.assertNotRegex(symbol, r"[\s/]")
				self.assertTrue(symbol)
				self.assertLessEqual(len(symbol), UNIT_SYMBOL_MAX)

	def test_a_blank_symbol_is_refused(self):
		"""The other app sent one and Tally answered `BAD UNIT NAME` with no
		name after it. Ours cannot, and this is what holds that."""
		export = _export(units=[model.Unit(symbol="")])
		self.assertIn("unit", _checks(export))


class TestTallySystemGroupIsNeverRedefined(UnitTestCase):
	"""`ERROR: Group: Current Assets - Group 'Application of Funds (Assets)' is
	already a child of this!`. Our `01_masters_*f7cce0.xml`.

	We emitted a GROUP master for one of Tally's own primary groups and gave it
	a parent, which is a re-parenting of a structure Tally owns.
	"""

	def test_every_tally_primary_group_is_recognised_as_reserved(self):
		for name in (
			"Current Assets",
			"Current Liabilities",
			"Sales Accounts",
			"Purchase Accounts",
			"Duties & Taxes",
			"Sundry Debtors",
			"Sundry Creditors",
			"Bank Accounts",
			"Cash-in-Hand",
			"Stock-in-Hand",
			"Capital Account",
			"Suspense A/c",
		):
			with self.subTest(group=name):
				self.assertTrue(is_reserved_group(name))

	def test_a_group_master_naming_a_system_group_is_refused(self):
		export = _export(groups=[model.Group(name="Current Assets", parent="Application of Funds (Assets)")])
		findings = [f for f in validate(export).findings if f.check == "reserved name"]
		self.assertTrue(findings)
		self.assertEqual({f.severity for f in findings}, {ABORTS})

	def test_a_system_group_is_a_valid_parent_and_not_a_missing_one(self):
		"""A mapping target, which is the whole point of protecting them."""
		export = _export(ledgers=[model.Ledger(name="Acme Ltd", parent="Sundry Debtors")])
		self.assertNotIn("ledger parent", _checks(export))


class TestNameMatchingIgnoresCaseAndPunctuation(UnitTestCase):
	"""`ERROR: Ledger: Stock In Hand - Improper change of Group Name for Ledger
	'Stock-in-Hand'!`. Our `01_masters_*ce78a3.xml`.

	Tally matches master names ignoring case and punctuation, so ERPNext's
	ledger `Stock In Hand` reads as a request to turn Tally's reserved *group*
	`Stock-in-Hand` into a ledger.
	"""

	def test_the_two_spellings_tally_confused_are_one_name(self):
		self.assertEqual(reserved_group("Stock In Hand"), "Stock-in-Hand")
		self.assertEqual(reserved_group("CASH IN HAND"), "Cash-in-Hand")
		self.assertEqual(reserved_group("duties and taxes"), "")

	def test_a_ledger_named_like_a_system_group_is_refused(self):
		export = _export(ledgers=[model.Ledger(name="Stock In Hand", parent="Current Assets")])
		findings = [f for f in validate(export).findings if f.check == "reserved name"]
		self.assertTrue(findings)
		self.assertEqual({f.severity for f in findings}, {ABORTS})

	def test_the_company_suffix_keeps_such_a_ledger_distinct(self):
		from unittest.mock import patch

		from tally_connector.utils import ledgers

		# The abbreviation is the company's, so it is given rather than read
		# from whatever companies the test site holds.
		with patch.object(ledgers, "company_abbr", return_value="DT"):
			self.assertEqual(ledgers.ledger_name("Stock In Hand - DT", "Demo Traders"), "Stock In Hand - DT")
			self.assertEqual(ledgers.ledger_name("Bad Debts - DT", "Demo Traders"), "Bad Debts")


class TestAVoucherNeverNamesAnUncreatedLedger(UnitTestCase):
	"""`ERROR: Voucher ... Ledger 'ACME TRADERS 9000000000' does not exist!`.
	Our `02_vouchers_*0b4119.xml`.

	Downstream of the masters file aborting: the ledgers after the failing line
	were never created. The check that catches it directly is closure.
	"""

	def test_closure_refuses_a_voucher_naming_a_ledger_no_master_creates(self):
		export = _export(ledgers=[model.Ledger(name="Cash", parent="Cash-in-Hand")])
		voucher = model.Voucher(
			kind="Sales", date=datetime.date(2026, 4, 8), number="SINV-1", party="ACME TRADERS 9000000000"
		)
		voucher.lines.append(
			model.LedgerLine(ledger="ACME TRADERS 9000000000", amount=Decimal("-100"), is_party=True)
		)
		voucher.lines.append(model.LedgerLine(ledger="Cash", amount=Decimal("100")))
		export.vouchers.append(voucher)
		findings = [f for f in validate(export).findings if f.check == "closure"]
		self.assertTrue(findings)
		self.assertEqual({f.severity for f in findings}, {REJECTS})


class TestTheInventedDuplicateToken(UnitTestCase):
	"""`@@DUPIGNORECOMBINE` appeared in no Tally artefact, only in our own four
	output files, and a real import answered `Created: 9,626  Altered: 9,694` -
	nine and a half thousand of a customer's own masters overwritten.

	The default now sends no directive, which is what the implementation that
	successfully imported 16,261 vouchers does.
	"""

	def test_the_default_masters_envelope_carries_no_duplicate_directive(self):
		xml = render_masters("Demo Traders", model.Masters())
		self.assertNotIn("IMPORTDUPS", xml)

	def test_the_invented_token_is_no_longer_a_value_we_can_send(self):
		"""It survives in a comment, which is where the history belongs. What
		must not survive is any path that puts it in a file."""
		from tally_connector.tally import render

		self.assertNotIn("@@DUPIGNORECOMBINE", render.DUPLICATE_BEHAVIOURS)
		self.assertEqual(render.DUP_IGNORE, "")
		self.assertEqual(render.DUP_NONE, "")

	def test_an_explicit_choice_is_still_sent(self):
		from tally_connector.tally.render import DUP_MODIFY

		xml = render_masters("Demo Traders", model.Masters(), DUP_MODIFY)
		self.assertIn("<IMPORTDUPS>@@DUPMODIFY</IMPORTDUPS>", xml)

	def test_every_token_we_do_send_has_tallys_shape(self):
		from tally_connector.tally.render import DUPLICATE_BEHAVIOURS

		for token in DUPLICATE_BEHAVIOURS:
			if token:
				with self.subTest(token=token):
					self.assertRegex(token, r"^@@DUP[A-Z]+$")


class TestOneBadRecordLosesTheWholeFile(UnitTestCase):
	"""The reason every one of these mattered enough to cost a round trip.

	TallyPrime stops at the first record it cannot accept, so a single refused
	unit takes every master after it - which is why ten attempts produced ten
	logs each ending `Errors: 1`, one new defect at a time.
	"""

	def test_an_aborting_finding_is_reported_as_losing_the_file(self):
		export = _export(units=[model.Unit(symbol="Square Meter")])
		aborts = [f for f in validate(export).findings if f.severity == ABORTS]
		self.assertTrue(aborts, "a refused unit must be an aborting finding")

	def test_the_severity_vocabulary_still_distinguishes_the_three_outcomes(self):
		from tally_connector.tally.validate import SEVERITY_ORDER, WRONG

		self.assertEqual(sorted(SEVERITY_ORDER, key=SEVERITY_ORDER.get), [ABORTS, REJECTS, WRONG])


class TestTheCustomersOwnUnitsWouldNowPass(UnitTestCase):
	"""The eight symbols our last real export actually sent, from
	`01_masters_*ce78a3.xml`: BAG Box KG Nos SET SQ.FT SQFT SqMtr.
	"""

	SENT = ("BAG", "Box", "KG", "Nos", "SET", "SQ.FT", "SQFT", "SqMtr")

	def test_none_of_them_carries_a_character_tally_refuses(self):
		for symbol in self.SENT:
			with self.subTest(symbol=symbol):
				self.assertNotRegex(symbol, r"[\s/]")
				self.assertLessEqual(len(symbol), UNIT_SYMBOL_MAX)

	def test_the_validator_passes_them(self):
		export = _export(units=[model.Unit(symbol=s) for s in self.SENT])
		self.assertNotIn("unit symbol", _checks(export))
		self.assertNotIn("name", _checks(export))


class TestANameThatIsOnlyPunctuation(UnitTestCase):
	"""`....` is a real customer on a real site, and it cost a live push.

	Tally matches masters on `tally_key` - case and every non-alphanumeric
	ignored - so four dots reduce to the empty string and there is no identity
	left to file the master under. Tally answered `0 errors and 1 exceptions`
	and named nothing, so the batch aborted and everything after it in the file
	was lost; the record was found by sending the batch again one at a time.

	`strip()` cannot see this, which is why the existing blank-name check let
	it through: those four dots are not whitespace.
	"""

	def _report(self, name):
		from tally_connector.tally import model
		from tally_connector.tally.validate import validate

		export = model.Export(company="Co")
		export.masters.ledgers.append(model.Ledger(name=name, parent="Sundry Debtors"))
		return validate(export)

	def _hits(self, name):
		return [f for f in self._report(name).findings if f.check == "name"]

	def test_four_dots_is_reported(self):
		hits = self._hits("....")
		self.assertTrue(hits, "the name that actually broke a live push")
		self.assertIn("punctuation", hits[0].detail)

	def test_an_unused_one_is_not_a_blocker(self):
		"""It is left out of the file, so there is nothing for Tally to
		refuse - and blocking would hold a whole export hostage to a junk
		record nothing posts to, which is what it did on a live site."""
		self.assertEqual([f for f in self._report("....").blocking if f.check == "name"], [])
		self.assertIn("left out of the file", self._hits("....")[0].detail)

	def test_the_renderer_actually_leaves_it_out(self):
		"""Or the finding would be describing something that is not true."""
		from tally_connector.tally import model
		from tally_connector.tally.render import render_masters

		masters = model.Masters()
		masters.ledgers.append(model.Ledger(name="....", parent="Sundry Debtors"))
		masters.ledgers.append(model.Ledger(name="Acme", parent="Sundry Debtors"))
		xml = render_masters("Co", masters)
		self.assertNotIn("....", xml)
		self.assertIn("Acme", xml)

	def test_one_a_voucher_posts_to_still_blocks(self):
		"""Then the accounting genuinely cannot cross, and sending the voucher
		would post it to a ledger that is not there."""
		import datetime
		from decimal import Decimal

		from tally_connector.tally import model
		from tally_connector.tally.validate import validate

		export = model.Export(company="Co")
		export.masters.ledgers.append(model.Ledger(name="....", parent="Sundry Debtors"))
		voucher = model.Voucher(kind="Sales", date=datetime.date(2026, 4, 8), number="SINV-1", party="....")
		voucher.lines.append(model.LedgerLine(ledger="....", amount=Decimal("-1"), is_party=True))
		voucher.lines.append(model.LedgerLine(ledger="Sales", amount=Decimal("1")))
		export.vouchers.append(voucher)
		blocking = [f for f in validate(export).blocking if f.check == "name"]
		self.assertTrue(blocking, "a voucher posting to a ledger Tally cannot hold")
		self.assertIn("a voucher posts to it", blocking[0].detail)

	def test_other_punctuation_only_names_go_the_same_way(self):
		for name in ("---", "///", "&&", ". . .", "()"):
			with self.subTest(name=name):
				self.assertTrue(self._hits(name))

	def test_a_name_with_one_letter_in_it_is_fine(self):
		"""The rule is about having no identity at all, not about punctuation
		being unwelcome - plenty of real parties carry dots and ampersands."""
		for name in ("A.", "K S P v & Co", "M/S. R.K.", "3M"):
			with self.subTest(name=name):
				self.assertEqual(self._hits(name), [])


class TestALedgerThatCarriesStockMustAffectInventory(UnitTestCase):
	"""Every invoice-mode voucher on this engagement failed for this.

	Tally refuses an invoice whose ACCOUNTINGALLOCATIONS names a ledger with
	*Inventory values are affected* set to No, and refuses it in the worst way
	available: `0 errors and 1 exceptions`, naming nothing, with the rest of
	the file lost behind it. In accounting mode Tally never checks - it drops
	the stock lines and reports success - so it could only ever show up on
	invoices.

	Proved on a live TallyPrime by altering that one flag on one ledger and
	re-sending the same voucher: refused, then created. `Ledger.affects_stock`
	had existed since the model was written and the renderer had always read
	it; nothing ever set it, so every ledger went out as No.
	"""

	def _export(self, affects):
		import datetime
		from decimal import Decimal

		from tally_connector.tally import model

		export = model.Export(company="Co")
		export.masters.ledgers.append(
			model.Ledger(name="Sales", parent="Direct Income", affects_stock=affects)
		)
		export.masters.ledgers.append(model.Ledger(name="Acme", parent="Sundry Debtors"))
		export.masters.stock_items.append(model.StockItem(name="Widget", base_unit="Nos"))
		voucher = model.Voucher(kind="Sales", date=datetime.date(2026, 4, 8), number="SINV-1", party="Acme")
		voucher.inventory.append(
			model.InventoryLine(
				item="Widget",
				ledger="Sales",
				quantity=Decimal("1"),
				rate=Decimal("100"),
				amount=Decimal("100"),
				unit="Nos",
			)
		)
		voucher.lines.append(model.LedgerLine(ledger="Acme", amount=Decimal("-100"), is_party=True))
		export.vouchers.append(voucher)
		return export

	def _hits(self, export, target=None):
		from tally_connector.tally.validate import validate

		return [f for f in validate(export, target=target).findings if f.check == "stock ledger"]

	def test_a_ledger_that_does_not_affect_inventory_is_refused(self):
		hits = self._hits(self._export(affects=False))
		self.assertTrue(hits, "the defect that failed every invoice")
		self.assertIn("Sales", hits[0].subject)

	def test_it_blocks_rather_than_warns(self):
		"""Tally loses the rest of the file behind it."""
		from tally_connector.tally.validate import validate

		report = validate(self._export(affects=False))
		self.assertTrue([f for f in report.blocking if f.check == "stock ledger"])

	def test_setting_the_flag_clears_it(self):
		self.assertEqual(self._hits(self._export(affects=True)), [])

	def test_the_renderer_sends_the_flag(self):
		from tally_connector.tally.render import render_masters

		export = self._export(affects=True)
		xml = render_masters("Co", export.masters)
		block = xml[xml.index('<LEDGER NAME="Sales"') :]
		block = block[: block.index("</LEDGER>")]
		self.assertIn("<AFFECTSSTOCK>Yes</AFFECTSSTOCK>", block)

	def test_a_ledger_the_destination_already_flags_is_accepted(self):
		"""It is Tally's answer that matters, not ours - a reused master keeps
		the destination's setting."""
		from tally_connector.tally.snapshot import TallyMaster, TallySnapshot
		from tally_connector.tally.validate import TargetCompany

		snapshot = TallySnapshot(
			ledgers={
				"sales": TallyMaster(name="Sales", parent="Direct Income", extra={"AFFECTSSTOCK": "Yes"})
			}
		)
		target = TargetCompany(name="Co", known=True, snapshot=snapshot)
		export = self._export(affects=False)
		export.masters.ledgers = [l for l in export.masters.ledgers if l.name != "Sales"]
		self.assertEqual(self._hits(export, target), [])

	def test_a_destination_that_has_it_off_is_still_refused(self):
		from tally_connector.tally.snapshot import TallyMaster, TallySnapshot
		from tally_connector.tally.validate import TargetCompany

		snapshot = TallySnapshot(
			ledgers={"sales": TallyMaster(name="Sales", parent="Direct Income", extra={"AFFECTSSTOCK": "No"})}
		)
		target = TargetCompany(name="Co", known=True, snapshot=snapshot)
		export = self._export(affects=False)
		export.masters.ledgers = [l for l in export.masters.ledgers if l.name != "Sales"]
		self.assertTrue(self._hits(export, target))
