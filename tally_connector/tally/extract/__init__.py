"""
ERPNext -> the normalised model. The only module in this package that imports
frappe, and the only one that ever should.

Everything downstream of here - mapping, validation, rendering - is pure, so it
can be exercised against a JSON fixture in milliseconds with no site. That split
is the whole reason this layer exists as its own file rather than as more code
inside the generators.

What it does *not* do is decide anything about Tally. The voucher kind, the sign
convention, the ledger naming and the group placement are all decisions this
module makes because they depend on ERPNext data - but each one is a single
named function, and the renderer downstream simply writes down what it is given.

The readers live in the modules beside this one; every name they define is
imported here as well, so `tally_connector.tally.extract` reads as it always
has.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr

from tally_connector.tally.accounting import without_stock
from tally_connector.tally.extract.invoices import (
	BILLABLE,
	ROUNDING_TOLERANCE,
	_bill_refs,
	_cost_centres,
	_mark_ledgers_that_carry_stock,
	_posted_total,
	_purchase,
	_rounding_line,
	_sales,
	_stock_rate,
)
from tally_connector.tally.extract.journals import (
	GL_DOCUMENT_TYPES,
	_gl_documents,
	_journals,
)
from tally_connector.tally.extract.masters import (
	_books_begin,
	_item_gst_rates,
	_opening_balances,
	extract_masters,
)
from tally_connector.tally.extract.naming import (
	_UNIT_SAFE,
	_UNIT_WORDS,
	MONEY_GROUPS,
	_group_for,
	_item_names,
	_lands_in_money,
	_party_name,
	_party_names,
	_round_off_ledger,
	_unit_sanitise,
	_unit_shorten,
	_unit_symbols,
	bill_reference_name,
	cash_and_bank,
	cost_centre_name,
	gst_accounts,
	gst_ledgers,
	stock_item_name,
	unit_name,
	units_in_use,
	voucher_kind_for,
)
from tally_connector.tally.extract.payments import (
	_bank_transactions,
	_payments,
)
from tally_connector.tally.extract.source import (
	_ADDRESS_FIELDS,
	_GST_REGISTRATION,
	_address,
	_addresses_for,
	_children_by_parent,
	_chunks,
	_date_filter,
	_gst_registration,
	_in_india,
	_parents_first,
	_plain_text,
	_settings,
	_strip_company,
)
from tally_connector.tally.extract_rules import UNIT_SYMBOL_MAX
from tally_connector.tally.model import Export, Ledger, Voucher
from tally_connector.utils.ledgers import GROUP_CREDITORS, GROUP_SUSPENSE

VOUCHER_SOURCES = (
	("sales_invoices", "include_sales_invoice", _sales),
	("purchase_invoices", "include_purchase_invoice", _purchase),
	("payment_entries", "include_payment_entry", _payments),
	("journal_entries", "include_journal_entry", _journals),
	("bank_transactions", "include_bank_transaction", _bank_transactions),
	# No settings field yet, so `settings.get(..., 1)` answers on. Adding one
	# later must go through `backfill_new_settings_defaults`, or upgrading
	# sites lose these documents silently.
	("other_documents", "include_other_documents", _gl_documents),
)


# What a reporting caller calls each source while it runs, and which ERPNext
# DocType and date column to count for its denominator. Kept beside the sources
# so a sixth cannot be added with no way to report it.
SOURCE_STAGES = {
	"sales_invoices": ("Processing Sales", "Sales Invoice", "posting_date"),
	"purchase_invoices": ("Processing Purchases", "Purchase Invoice", "posting_date"),
	"payment_entries": ("Processing Payments", "Payment Entry", "posting_date"),
	"journal_entries": ("Processing Journals", "Journal Entry", "posting_date"),
	"bank_transactions": ("Processing Bank Lines", "Bank Transaction", "date"),
	"other_documents": ("Processing Other Documents", "GL Entry", "posting_date"),
}


def count_vouchers(company, from_date=None, to_date=None, settings=None):
	"""How many vouchers this range holds, per source and in total.

	Counted rather than guessed, because it is the denominator of a progress bar
	somebody will believe. It obeys the same `include_*` settings the export
	does - a denominator that counts work the export will skip makes a bar that
	stops at eighty per cent and stays there.
	"""
	settings = settings or _settings()
	per_source, total = {}, 0
	for key, flag, _fn in VOUCHER_SOURCES:
		if not settings.get(flag, 1):
			per_source[key] = 0
			continue
		_stage, doctype, date_field = SOURCE_STAGES[key]
		filters = _date_filter({"docstatus": 1, "company": company}, date_field, from_date, to_date)
		try:
			if key == "other_documents":
				# Documents, not GL rows: one voucher each.
				filters.update({"is_cancelled": 0, "voucher_type": ["in", GL_DOCUMENT_TYPES]})
				n = len(
					frappe.get_all(
						doctype, filters=filters, fields=["voucher_no"], distinct=True, limit_page_length=0
					)
				)
			else:
				n = frappe.db.count(doctype, filters)
		except Exception:
			n = 0
		per_source[key] = n
		total += n
	return per_source, total


# The dashboard's per-area buttons, keyed the way the buttons are labelled.
# They name **ERPNext** areas rather than Tally kinds, which is the distinction
# that matters: "Sales Invoices" means every voucher the sales reader produced,
# credit notes among them, because that is what somebody pressing it is asking
# for. Keying it on the Tally kind instead would silently drop the credit notes
# into a button nobody would think to press.
VOUCHER_SECTIONS = {
	"sales_invoices": (_sales, {}),
	"purchase_invoices": (_purchase, {}),
	"payment_in": (_payments, {"payment_type": "Receive"}),
	"payment_out": (_payments, {"payment_type": "Pay"}),
	# Not a button; the name the single-document export and the run counts use.
	"payment_entries": (_payments, {}),
	"journal_entries": (_journals, {}),
	"bank_transactions": (_bank_transactions, {}),
	"other_documents": (_gl_documents, {}),
}


def sending_inventory(settings=None) -> bool:
	"""Whether a voucher goes out carrying its items.

	`None` reads as on, because a missing field must not quietly change what a
	customer's books receive. That guard is not what protects an upgrading
	site, though, and saying so here is the point: `Tally Settings` is a
	Single, and a Check with no row in `tabSingles` was **measured** reading 0
	rather than None, which is folding switched on by accident. What protects
	it is `patches.v1_0.backfill_new_settings_defaults`, which gives the field
	its declared 1 on a site that already has the Single.
	"""
	settings = settings if settings is not None else _settings()
	value = settings.get("send_voucher_inventory")
	return True if value is None else bool(value)


def _as_configured(vouchers, settings=None):
	"""The vouchers as the destination company is set up to take them.

	The one place the accounting-only choice is applied, so the file somebody
	downloads, the batches somebody pushes and the export validate() reads are
	the same vouchers. Folding at each push site instead is how one door comes
	to disagree with another about what was sent.

	Folding also takes the voucher out of invoice mode, because `is_invoice` is
	derived from the inventory being non-empty. That is the point rather than a
	side effect: an accounting voucher is sent with ALLLEDGERENTRIES.LIST and
	is never asked whether its ledgers affect inventory, which is the pair of
	conditions a live TallyPrime refused roughly 43% of invoices over.
	"""
	if sending_inventory(settings):
		return list(vouchers)
	return [without_stock(voucher) for voucher in vouchers]


def extract_vouchers(section, company, from_date=None, to_date=None, only=None) -> list[Voucher]:
	"""One area's vouchers, for the per-area buttons.

	Deliberately does **not** consult the `include_*` settings the full export
	obeys. Those answer "what goes in the nightly export"; a person pressing
	Sales Invoices has just answered that question by pressing it, and a button
	that silently exported nothing because of a setting on another screen is
	worse than one that exports what it says.
	"""
	fn, extra = VOUCHER_SECTIONS[section]
	return _as_configured(fn(company, from_date, to_date, only=only, **extra))


def extract(company, from_date=None, to_date=None, reporter=None) -> Export:
	"""The whole export as a model, with closure guaranteed.

	Vouchers first, then masters, then anything the vouchers referenced that no
	master creates. That ordering is what makes closure possible rather than
	hoped for: a ledger a voucher names and nothing creates is a state this
	cannot return.

	`reporter` is optional and is the only thing in this module that knows a
	background job exists. It is called with a stage as each source begins and
	ticked once per voucher actually built, so what a progress bar shows is a
	count this loop reached rather than a fraction of elapsed time.
	"""
	settings = _settings()
	# `Export.company` is the company **as Tally knows it**, which is what goes
	# into SVCURRENTCOMPANY and must match Tally exactly. Resolving it belongs
	# here rather than in the renderer: the renderer is pure and cannot read a
	# setting, and that is the point of the split.
	export = Export(company=cstr(settings.company_name_in_tally or "").strip() or company)

	for key, flag, fn in VOUCHER_SOURCES:
		if not settings.get(flag, 1):
			continue
		if reporter:
			reporter.at(SOURCE_STAGES[key][0])
		export.vouchers.extend(fn(company, from_date, to_date, tick=(reporter.tick if reporter else None)))

	# Before the closure walk and before the stock marking, both of which read
	# the vouchers: folded, a stock line's ledger arrives as an ordinary ledger
	# line and is still created, and no ledger is marked as affecting
	# inventory, because no voucher carries any.
	carried = sum(len(voucher.inventory) for voucher in export.vouchers)
	export.vouchers = _as_configured(export.vouchers, settings)
	if not sending_inventory(settings):
		export.accounting_only = True
		# Measured across the fold rather than taken from the count before it,
		# so a stock line with no ledger - left whole on purpose, because
		# dropping it would take its money - is not counted as folded.
		export.stock_lines_folded = carried - sum(len(voucher.inventory) for voucher in export.vouchers)

	# Which ledgers carry a cost centre is known only once the vouchers exist,
	# which is why the masters are built second.
	cost_centre_ledgers = {
		line.ledger for voucher in export.vouchers for line in voucher.lines if line.cost_centres
	}
	if reporter:
		reporter.at("Generating Masters")
	export.masters = extract_masters(company, from_date, to_date, cost_centre_ledgers)

	# Folded, no voucher names a stock item, a stock group or a unit, so
	# creating them would put records in the customer's Tally that nothing
	# uses - over a hundred items on one year's books. It sits
	# here beside the fold because it is the same decision, and the readers
	# keep one shape. `validate`'s closure check still proves nothing
	# references what is not created, which is what makes this safe rather
	# than merely tidy.
	if not sending_inventory(settings):
		export.masters.stock_items = []
		export.masters.stock_groups = []
		export.masters.units = []

	created = export.masters.ledger_names()
	referenced = {}
	for voucher in export.vouchers:
		for line in voucher.lines:
			referenced.setdefault(line.ledger, _group_for(line, voucher))
		for inv in voucher.inventory:
			referenced.setdefault(inv.ledger, None)
		if voucher.party:
			referenced.setdefault(voucher.party, GROUP_CREDITORS)

	for name, group in sorted(referenced.items()):
		if name and name not in created:
			export.masters.ledgers.append(
				Ledger(name=name, parent=group or GROUP_SUSPENSE, cost_centres_on=name in cost_centre_ledgers)
			)

	_mark_ledgers_that_carry_stock(export)
	return export
