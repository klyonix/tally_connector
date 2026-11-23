"""
Knowing what the destination already holds, and deciding what to do about it.

The defect these exist for is on the record: a real masters import answered
`Created: 9,626  Altered: 9,694`. Nine and a half thousand masters an accountant
maintains, overwritten, because every master carried a hardcoded
`ACTION="Create"` and nothing had asked what was there.

The distinction the whole design turns on is **unknown is not empty**. A
collection Tally did not answer must not read as a company with nothing in it,
because that is the reading under which a connector overwrites a chart of
accounts it could not see.

Pure: no site, no database, no network.
"""

from __future__ import annotations

from frappe.tests import IntegrationTestCase, UnitTestCase

from tally_connector.tally import model
from tally_connector.tally.resolve import (
	ALTER,
	ALTER_PARENT,
	CONFLICT,
	CREATE,
	REUSE,
	SYSTEM,
	UNKNOWN,
	resolve,
)
from tally_connector.tally.snapshot import (
	TallySnapshot,
	company_matches,
	parse_companies,
	parse_masters,
)

LEDGERS_XML = b"""<ENVELOPE><BODY><DATA><COLLECTION>
<LEDGER NAME="Acme Ltd"><PARENT>Sundry Debtors</PARENT><OPENINGBALANCE>-100</OPENINGBALANCE></LEDGER>
<LEDGER NAME="BANK CHARGES"><PARENT>Indirect Expenses</PARENT></LEDGER>
<LEDGER NAME="Cash"><PARENT>Cash-in-Hand</PARENT></LEDGER>
</COLLECTION></DATA></BODY></ENVELOPE>"""

UNITS_XML = b"""<ENVELOPE><BODY><DATA><COLLECTION>
<UNIT NAME="Nos"><ISSIMPLEUNIT>Yes</ISSIMPLEUNIT></UNIT>
<UNIT NAME="BAG"></UNIT>
</COLLECTION></DATA></BODY></ENVELOPE>"""

COMPANIES_XML = b"""<ENVELOPE><BODY><DATA>
<COMPANY NAME="Demo Traders Pvt Ltd"></COMPANY>
<COMPANY NAME="Second Company"></COMPANY>
</DATA></BODY></ENVELOPE>"""


class TestReadingWhatTallyHolds(IntegrationTestCase):
	def test_ledgers_are_read_with_their_parents(self):
		held = parse_masters(LEDGERS_XML, "LEDGER")
		self.assertEqual(len(held), 3)
		self.assertEqual(held["acmeltd"].name, "Acme Ltd")
		self.assertEqual(held["acmeltd"].parent, "Sundry Debtors")

	def test_names_are_keyed_the_way_tally_matches_them(self):
		"""Case and punctuation ignored, which is how Tally decided
		`Stock In Hand` and `Stock-in-Hand` were one master."""
		snap = TallySnapshot(ledgers=parse_masters(LEDGERS_XML, "LEDGER"))
		for spelling in ("BANK CHARGES", "Bank Charges", "bank-charges"):
			with self.subTest(spelling=spelling):
				self.assertIsNotNone(snap.holds("ledgers", spelling))

	def test_an_answer_that_cannot_be_read_is_none_and_not_empty(self):
		self.assertIsNone(parse_masters(b"\x00 not xml at all", "LEDGER"))

	def test_an_answer_with_no_records_is_empty_and_not_none(self):
		"""A genuinely empty company is a different fact from a silent one."""
		held = parse_masters(b"<ENVELOPE><BODY/></ENVELOPE>", "LEDGER")
		self.assertEqual(held, {})

	def test_unknown_and_empty_are_distinguishable_on_the_snapshot(self):
		self.assertFalse(TallySnapshot().known("ledgers"))
		self.assertTrue(TallySnapshot(ledgers={}).known("ledgers"))

	def test_the_company_list_is_read(self):
		self.assertEqual(parse_companies(COMPANIES_XML), ["Demo Traders Pvt Ltd", "Second Company"])


class TestTheCompanyWeAreAboutToWriteTo(IntegrationTestCase):
	"""`SVCURRENTCOMPANY` must match exactly, and a mismatch imports nothing
	while reporting success - which is why it is checked rather than hoped."""

	def setUp(self):
		self.snap = TallySnapshot(companies=parse_companies(COMPANIES_XML))

	def test_a_company_tally_has_is_accepted(self):
		ok, _ = company_matches(self.snap, "Demo Traders Pvt Ltd")
		self.assertTrue(ok)

	def test_a_company_tally_has_not_got_is_refused_and_says_what_it_has(self):
		ok, why = company_matches(self.snap, "Demo Traders")
		self.assertFalse(ok)
		self.assertIn("Demo Traders Pvt Ltd", why)

	def test_no_company_configured_is_refused(self):
		ok, why = company_matches(self.snap, "")
		self.assertFalse(ok)
		self.assertIn("No company name", why)

	def test_a_tally_that_did_not_list_its_companies_does_not_block(self):
		"""Never asking has always been allowed. What must not happen is
		treating an answer that came back and did not name the company as
		though it had."""
		ok, _ = company_matches(TallySnapshot(), "Anything")
		self.assertTrue(ok)


def _masters(**parts):
	masters = model.Masters()
	for name, value in parts.items():
		setattr(masters, name, value)
	return masters


class TestDecidingWhatToDoWithEachMaster(UnitTestCase):
	def setUp(self):
		self.snap = TallySnapshot(
			ledgers=parse_masters(LEDGERS_XML, "LEDGER"),
			units=parse_masters(UNITS_XML, "UNIT"),
			groups={},
			stock_groups={},
			stock_items={},
			godowns={},
			cost_centres={},
		)

	def _action(self, resolution, name):
		return next(d.action for d in resolution.decisions if d.name == name)

	def test_a_ledger_tally_already_holds_is_reused_not_recreated(self):
		masters = _masters(ledgers=[model.Ledger(name="Acme Ltd", parent="Sundry Debtors")])
		self.assertEqual(self._action(resolve(masters, self.snap), "Acme Ltd"), REUSE)

	def test_a_reused_master_is_not_emitted_at_all(self):
		masters = _masters(ledgers=[model.Ledger(name="Acme Ltd", parent="Sundry Debtors")])
		self.assertEqual(resolve(masters, self.snap).emitted("ledgers"), {})

	def test_a_ledger_tally_has_not_got_is_created(self):
		masters = _masters(ledgers=[model.Ledger(name="New Customer", parent="Sundry Debtors")])
		resolution = resolve(masters, self.snap)
		self.assertEqual(self._action(resolution, "New Customer"), CREATE)
		self.assertEqual(resolution.emitted("ledgers"), {"New Customer": "Create"})

	def test_a_unit_tally_already_holds_is_reused(self):
		"""`Unit 'Nos' already exists!` is a real Tally error, and one refused
		master aborts the whole file."""
		masters = _masters(units=[model.Unit(symbol="Nos")])
		self.assertEqual(self._action(resolve(masters, self.snap), "Nos"), REUSE)

	def test_a_master_in_a_different_group_is_a_conflict_not_an_alteration(self):
		"""Re-parenting somebody else's master is what Tally refused as
		'Improper change of Group Name', and it is a decision about their chart
		rather than a detail to fix in passing."""
		masters = _masters(ledgers=[model.Ledger(name="Acme Ltd", parent="Sundry Creditors")])
		resolution = resolve(masters, self.snap)
		self.assertEqual(self._action(resolution, "Acme Ltd"), CONFLICT)
		self.assertEqual(resolution.emitted("ledgers"), {})
		self.assertTrue(resolution.conflicts)
		self.assertIn("Sundry Debtors", resolution.conflicts[0].theirs)

	def test_a_conflict_becomes_an_alteration_only_when_policy_says_so(self):
		masters = _masters(ledgers=[model.Ledger(name="Acme Ltd", parent="Sundry Creditors")])
		resolution = resolve(masters, self.snap, alter_policy=ALTER_PARENT)
		self.assertEqual(self._action(resolution, "Acme Ltd"), ALTER)
		self.assertEqual(resolution.emitted("ledgers"), {"Acme Ltd": "Alter"})

	def test_a_tally_system_group_is_never_redefined(self):
		masters = _masters(groups=[model.Group(name="Current Assets", parent="Primary")])
		resolution = resolve(masters, self.snap)
		self.assertEqual(self._action(resolution, "Current Assets"), SYSTEM)
		self.assertEqual(resolution.emitted("groups"), {})

	def test_a_system_group_is_protected_even_when_tally_said_nothing(self):
		"""Checked before the snapshot is consulted, so it holds with Tally
		switched off."""
		masters = _masters(groups=[model.Group(name="Sundry Debtors", parent="Current Assets")])
		resolution = resolve(masters, TallySnapshot())
		self.assertEqual(self._action(resolution, "Sundry Debtors"), SYSTEM)


class TestUnknownIsNotEmpty(UnitTestCase):
	"""The reading under which a connector overwrites a chart it cannot see."""

	def test_an_unanswered_collection_falls_back_to_create_and_says_so(self):
		masters = _masters(ledgers=[model.Ledger(name="Acme Ltd", parent="Sundry Debtors")])
		resolution = resolve(masters, TallySnapshot())
		decision = resolution.decisions[0]
		self.assertEqual(decision.action, UNKNOWN)
		self.assertIn("did not answer", decision.reason)
		self.assertEqual(decision.emit, "Create")

	def test_an_empty_answer_creates_without_the_caveat(self):
		masters = _masters(ledgers=[model.Ledger(name="Acme Ltd", parent="Sundry Debtors")])
		resolution = resolve(masters, TallySnapshot(ledgers={}))
		self.assertEqual(resolution.decisions[0].action, CREATE)

	def test_the_two_are_never_the_same_decision(self):
		masters = _masters(ledgers=[model.Ledger(name="X", parent="Y")])
		self.assertNotEqual(
			resolve(masters, TallySnapshot()).decisions[0].action,
			resolve(masters, TallySnapshot(ledgers={})).decisions[0].action,
		)


class TestStockGoingIntoACompanyThatKeepsNone(UnitTestCase):
	"""The destination on this engagement keeps no stock, and closure cannot
	see it.

	`_check_closure` asks whether each stock item will exist, and this export
	creates every one of them, so it passes - while Tally still refuses the
	voucher, because whether a company maintains inventory is a company
	setting and not a master.

	Measured off TallyPrime's own export of that company: 6,029 ledgers, 59
	groups, and no stock item, stock group, unit or godown at all; of the
	4,682 vouchers Tally wrote itself not one carries a stock line, and all
	1,141 invoices have an empty ALLINVENTORYENTRIES.LIST with the revenue on
	a rate-bearing ledger.
	"""

	def _export(self, with_stock=True):
		import datetime
		from decimal import Decimal

		export = model.Export(company="Co")
		export.masters.stock_items.append(model.StockItem(name="Widget", base_unit="Nos"))
		voucher = model.Voucher(
			kind="Sales", date=datetime.date(2026, 4, 8), number="SINV-1", party="Acme Ltd"
		)
		voucher.lines.append(model.LedgerLine(ledger="Acme Ltd", amount=Decimal("-118"), is_party=True))
		voucher.lines.append(model.LedgerLine(ledger="Sales", amount=Decimal("118")))
		if with_stock:
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
		export.vouchers.append(voucher)
		return export

	def _target(self, snapshot):
		from tally_connector.tally.validate import TargetCompany

		return TargetCompany(name="Demo Traders Pvt Ltd", known=True, snapshot=snapshot)

	def _accounting_only(self):
		"""A company with ledgers and nothing whatever on the stock side."""
		return TallySnapshot(
			company="Demo Traders Pvt Ltd",
			ledgers={"acme ltd": object()},
			groups={},
			stock_items={},
			stock_groups={},
			godowns={},
			units={},
		)

	def _hits(self, export, snapshot):
		from tally_connector.tally.validate import validate

		report = validate(export, target=self._target(snapshot))
		return [f for f in report.findings if f.check == "destination inventory"]

	def test_stock_into_an_accounting_only_company_is_reported(self):
		hits = self._hits(self._export(), self._accounting_only())
		self.assertTrue(hits, "sending stock into a company that keeps none")
		self.assertIn("accounting invoices", hits[0].detail)

	def test_it_names_both_roads_rather_than_choosing_one(self):
		"""Turning inventory on is a keystroke in Tally and is somebody's
		decision, not this connector's."""
		hits = self._hits(self._export(), self._accounting_only())
		self.assertIn("switch inventory on", hits[0].detail)

	def test_an_export_carrying_no_stock_is_not_reported(self):
		self.assertEqual(self._hits(self._export(with_stock=False), self._accounting_only()), [])

	def test_a_company_that_does_keep_stock_is_not_reported(self):
		snapshot = self._accounting_only()
		snapshot.stock_items = {"widget": object()}
		self.assertEqual(self._hits(self._export(), snapshot), [])

	def test_a_tally_that_did_not_say_is_not_read_as_a_company_with_none(self):
		"""Unknown is not empty, which is the rule this whole module exists
		for. A probe that failed must not be read as permission to refuse."""
		snapshot = self._accounting_only()
		snapshot.stock_items = None
		self.assertEqual(self._hits(self._export(), snapshot), [])

	def test_a_wholly_empty_company_says_nothing_either_way(self):
		"""A company with no ledgers either is new, not accounting-only."""
		snapshot = self._accounting_only()
		snapshot.ledgers = {}
		self.assertEqual(self._hits(self._export(), snapshot), [])

	def test_it_does_not_block_the_push(self):
		"""A company with inventory on and nothing in it yet looks identical
		from out here, and refusing that one would invent a problem."""
		from tally_connector.tally.validate import validate

		report = validate(self._export(), target=self._target(self._accounting_only()))
		self.assertEqual([f for f in report.blocking if f.check == "destination inventory"], [])


class TestAStockGroupTallyAlreadyHolds(UnitTestCase):
	"""The second run, which is where this would have gone wrong.

	The first push sends the stock groups and the items together, so a check
	reading the file alone is satisfied. The push after it sends neither,
	because `resolve` reuses what the destination has - and every item would
	then abort over a group sitting in Tally. Same defect as the ledger
	closure, which reported 2,986 missing ledgers that were all present.
	"""

	def _export(self, send_groups):
		export = model.Export(company="Co")
		if send_groups:
			export.masters.stock_groups.append(model.StockGroup(name="Tiles"))
		export.masters.stock_items.append(model.StockItem(name="Widget", parent="Tiles", base_unit="Nos"))
		return export

	def _hits(self, export, snapshot=None):
		from tally_connector.tally.validate import TargetCompany, validate

		target = None if snapshot is None else TargetCompany(name="Co", known=True, snapshot=snapshot)
		report = validate(export, target=target)
		return [f for f in report.findings if f.check == "item parent"]

	def test_a_group_the_destination_holds_is_not_called_missing(self):
		snapshot = TallySnapshot(stock_groups={"tiles": object()})
		self.assertEqual(self._hits(self._export(send_groups=False), snapshot), [])

	def test_a_group_nobody_has_is_still_reported(self):
		snapshot = TallySnapshot(stock_groups={})
		hits = self._hits(self._export(send_groups=False), snapshot)
		self.assertTrue(hits, "an item under a group that exists nowhere")
		self.assertIn("Tiles", hits[0].detail)

	def test_a_group_this_file_creates_is_enough_on_its_own(self):
		self.assertEqual(self._hits(self._export(send_groups=True)), [])

	def test_without_a_snapshot_it_reads_the_file_as_it_always_did(self):
		"""No Tally to ask is the offline harness, which must keep working."""
		hits = self._hits(self._export(send_groups=False))
		self.assertTrue(hits)
