"""
Extract, render, write. The only impure thing this module does is read the
settings and open files; everything about Tally lives in `tally/render.py` and
everything about ERPNext in `tally/extract/`.

It exists because those two must not know about each other's side of the wall,
and something has to hold them together for the dashboard and the scheduler.
Nothing here decides what an amount is or what Tally calls a voucher.
"""

import frappe

from tally_connector.tally.extract import extract, extract_vouchers
from tally_connector.tally.model import Masters
from tally_connector.tally.render import (
	DUP_COMBINE,
	DUP_MODIFY,
	DUP_NONE,
	render_masters,
	render_vouchers,
)


def _settings():
	return frappe.get_single("Tally Settings")


def _import_options(settings):
	"""How a re-import behaves, as two settings rather than two defaults.

	Tally has no upsert. `Create` adds a second voucher when it already holds
	one; `Alter` fails on a voucher Tally has never seen. No single mode is
	right for both, so both are somebody's decision on the settings screen and
	neither is chosen here.
	"""
	duplicates = {
		"Leave it alone": DUP_NONE,
		"Replace it": DUP_MODIFY,
		"Combine balances": DUP_COMBINE,
	}.get(settings.get("import_duplicate_behaviour") or "", DUP_NONE)
	return duplicates, (settings.get("voucher_import_action") or "Create")


def ledgers_refusing_cost_centres(ask=None):
	"""Ledgers the destination will not accept a cost centre allocation on.

	Empty when Tally was not asked, which is the behaviour that existed before
	the destination was ever consulted.
	"""
	if ask is None:
		ask = not bool(frappe.flags.get("in_test"))
	if not ask or not _settings().get("sync_enabled"):
		return frozenset()
	try:
		from tally_connector.utils.connection import TallyConnector

		snapshot = TallyConnector().snapshot()
		return frozenset(snapshot.ledgers_refusing_cost_centres())
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Tally Connector: cost centre flags")
		return frozenset()


def send_guid(settings=None):
	"""Whether vouchers carry a GUID. Off until proven against a real Tally."""
	return bool((settings or _settings()).get("send_voucher_guid"))


def _referenced(export):
	"""Every ledger name the vouchers name, from every place one can appear."""
	names = set()
	for voucher in export.vouchers:
		names |= {line.ledger for line in voucher.lines}
		names |= {inv.ledger for inv in voucher.inventory}
		if voucher.party:
			names.add(voucher.party)
	return names


# ─────────────────────────────────────────────────────────────────────────────
# One area at a time - the dashboard's per-area buttons
# ─────────────────────────────────────────────────────────────────────────────

# What each masters button sends. Groups go with every one of them because a
# masters file is imported on its own: a ledger whose group Tally does not hold
# is refused, and a group Tally already has is the cheap case the duplicate
# behaviour above is there to handle. Stock items carry the units for the same
# reason - an item naming a unit that does not exist is refused, and the button
# would look like it had worked.
_MASTER_SECTIONS = {
	"chart_of_accounts": ("groups", "ledgers", "cost_centres", "godowns"),
	"parties": ("groups", "parties"),
	"uoms": ("units",),
	"stock_items": ("units", "stock_groups", "stock_items"),
}

# A party ledger is one sitting under a party group. That is how the extract
# places them and how Tally tells them apart, so it is not a second opinion
# about what a party is.
_PARTY_GROUPS = ("Sundry Debtors", "Sundry Creditors")


def _slice(masters: Masters, parts) -> Masters:
	party = [l for l in masters.ledgers if l.parent in _PARTY_GROUPS]
	other = [l for l in masters.ledgers if l.parent not in _PARTY_GROUPS]
	out = Masters()
	if "groups" in parts:
		out.groups = list(masters.groups)
	if "ledgers" in parts:
		out.ledgers = other
	if "parties" in parts:
		out.ledgers = out.ledgers + party
	if "units" in parts:
		out.units = list(masters.units)
	if "stock_groups" in parts:
		out.stock_groups = list(masters.stock_groups)
	if "stock_items" in parts:
		out.stock_items = list(masters.stock_items)
	if "cost_centres" in parts:
		out.cost_centres = list(masters.cost_centres)
	if "godowns" in parts:
		out.godowns = list(masters.godowns)
	return out


def destination_resolution(masters, ask=None):
	"""What the destination already holds, if it will say. None if it will not.

	Asking costs one round of small read-only requests and saves sending
	masters Tally already has - which is not a saving but a correctness fix: a
	real import answered `Created: 9,626  Altered: 9,694` against a customer's
	live chart, because nothing had asked.

	Never raises and never blocks. A Tally that is switched off leaves this
	None, and `render_masters` then behaves exactly as it always did.
	"""
	settings = _settings()
	if not settings.get("sync_enabled"):
		return None
	if ask is None:
		# A test run must not depend on whether somebody's Tally happens to be
		# switched on. With a live one reachable the masters this export sends
		# are the ones Tally has not got - correct, and it makes every
		# assertion about the file's contents an assertion about that Tally.
		# Seven modules went red the first time a real Tally was connected.
		# A test that wants a destination passes `ask=True` and supplies one.
		ask = not bool(frappe.flags.get("in_test"))
	if not ask:
		return None
	try:
		from tally_connector.tally.resolve import resolve
		from tally_connector.utils.connection import TallyConnector

		snapshot = TallyConnector().snapshot()
		if not snapshot.anything_known():
			return None
		return resolve(masters, snapshot)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Tally Connector: destination snapshot")
		return None


def unsent_vouchers(vouchers, erpnext_company, tally_company):
	"""Vouchers this destination has not already been sent.

	Left out of the file rather than sent and deduplicated afterwards by a
	TallyPrime setting this app cannot see. Never raises: a sync ledger that
	cannot be read leaves every voucher in, which is the behaviour that existed
	before it did.
	"""
	try:
		from tally_connector.utils.sync_ledger import unsent

		return unsent(vouchers, erpnext_company, tally_company)
	except ImportError:
		return list(vouchers), []
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Tally Connector: sync ledger")
		return list(vouchers), []


def voucher_section(section, from_date=None, to_date=None, company=None):
	"""The same vouchers `render_section` would render, unrendered.

	The counterpart of `master_section`, and it exists for the same reason: a
	batch cannot be cut out of a finished string without cutting a record in
	half. `connection.push_vouchers` renders each batch itself.

	It returns the ERPNext company beside the Tally one because the sync ledger
	keys on the first and Tally on the second, and confusing them is how a
	ledger of what has been sent comes to match nothing.
	"""
	company = company or frappe.defaults.get_user_default("Company")
	settings = _settings()
	_duplicates, action = _import_options(settings)
	name = (settings.company_name_in_tally or "").strip() or company
	vouchers = extract_vouchers(section, company, from_date, to_date)
	vouchers, skipped = unsent_vouchers(vouchers, company, name)
	return {
		"tally_company": name,
		"erpnext_company": company,
		"vouchers": vouchers,
		"action": action,
		"with_guid": send_guid(settings),
		"no_cost_centres": ledgers_refusing_cost_centres(),
		"already_sent": len(skipped),
	}


def document_parts(doctype, docname, posting_date, company):
	"""One document's voucher and everything needed to push it.

	The counterpart of `render_document`, unrendered, so the single-document
	button can go through `push_vouchers` and be recorded. It uses the same
	`only` filter that render_document does - the readers filter on date first
	and the name second - rather than extracting the day and picking the
	voucher out afterwards, which matched nothing and quietly reported the
	document as already sent.
	"""
	settings = _settings()
	_duplicates, action = _import_options(settings)
	name = (settings.company_name_in_tally or "").strip() or company
	vouchers = extract_vouchers(
		SINGLE_DOCUMENT_SECTIONS[doctype], company, posting_date, posting_date, only=docname
	)
	vouchers, skipped = unsent_vouchers(vouchers, company, name)
	return {
		"tally_company": name,
		"erpnext_company": company,
		"vouchers": vouchers,
		"action": action,
		"with_guid": send_guid(settings),
		"no_cost_centres": ledgers_refusing_cost_centres(),
		"already_sent": len(skipped),
	}


def is_master_section(section) -> bool:
	"""Whether this section is masters rather than vouchers.

	A predicate rather than the mapping itself, so callers outside this module
	do not reach into a private name to ask one yes-or-no question.
	"""
	return section in _MASTER_SECTIONS


def master_section(section, from_date=None, to_date=None, company=None):
	"""The same masters `render_section` would render, unrendered.

	A masters file on this engagement is 79MB and a live TallyPrime answered a
	memory access violation rather than refusing it, so the push sends them a
	batch at a time - and a batch cannot be cut out of a finished string
	without cutting a record in half. This hands back the parts instead, and
	`connection.push_masters` renders each batch itself.

	It runs the same extract and the same resolution as `render_section`, so
	the file somebody downloads and the batches somebody sends are built from
	one answer rather than two.
	"""
	company = company or frappe.defaults.get_user_default("Company")
	settings = _settings()
	duplicates, _action = _import_options(settings)
	export = extract(company, from_date, to_date)
	masters = _slice(export.masters, _MASTER_SECTIONS[section])
	return (export.company, masters, duplicates, destination_resolution(masters), masters.count())


def render_section(section, from_date=None, to_date=None, company=None):
	"""One area of the export as XML, plus how many objects are in it.

	A masters section runs the **whole** extract rather than the masters alone,
	and that is deliberate at a cost. Two facts on a ledger - whether it carries
	cost centres, and whether anything references it that no master creates -
	are knowable only once the vouchers exist. Building the masters on their own
	would give a chart of accounts subtly different from the one the full export
	sends, and one door disagreeing with another about the same ledger is the
	defect this whole layer was written to remove.
	"""
	company = company or frappe.defaults.get_user_default("Company")
	settings = _settings()
	duplicates, action = _import_options(settings)

	if section in _MASTER_SECTIONS:
		export = extract(company, from_date, to_date)
		masters = _slice(export.masters, _MASTER_SECTIONS[section])
		resolution = destination_resolution(masters)
		return (render_masters(export.company, masters, duplicates, resolution), masters.count())

	vouchers = extract_vouchers(section, company, from_date, to_date)
	name = (settings.company_name_in_tally or "").strip() or company
	vouchers, _skipped = unsent_vouchers(vouchers, company, name)
	return (
		render_vouchers(name, vouchers, action, send_guid(settings), ledgers_refusing_cost_centres()),
		len(vouchers),
	)


# Which ERPNext document types can be sent one at a time, and which reader
# produces them. `Bank Transaction` is absent because it has no single-document
# button - it is reconciliation data rather than something somebody opens and
# presses Export on.
SINGLE_DOCUMENT_SECTIONS = {
	"Sales Invoice": "sales_invoices",
	"Purchase Invoice": "purchase_invoices",
	"Payment Entry": "payment_entries",
	"Journal Entry": "journal_entries",
}


def render_document(doctype, docname, posting_date, company):
	"""Exactly one document as a voucher file, and nothing else.

	The date is passed as both ends of the range **and** the name is passed as
	`only`, because the readers filter on date first and the name second. The
	date alone used to be the whole filter, which sent every document sharing
	that date while the button's own confirmation named one.
	"""
	settings = _settings()
	_, action = _import_options(settings)
	vouchers = extract_vouchers(
		SINGLE_DOCUMENT_SECTIONS[doctype], company, posting_date, posting_date, only=docname
	)
	name = (settings.company_name_in_tally or "").strip() or company
	return (
		render_vouchers(name, vouchers, action, send_guid(settings), ledgers_refusing_cost_centres()),
		len(vouchers),
	)


# ─────────────────────────────────────────────────────────────────────────────
# The whole thing
# ─────────────────────────────────────────────────────────────────────────────


def _counts(export):
	"""What was exported, by the names the dashboard's cards already use."""
	counts = {
		"sales_invoices": sum(1 for v in export.vouchers if v.kind in ("Sales", "Credit Note")),
		"purchase_invoices": sum(1 for v in export.vouchers if v.kind in ("Purchase", "Debit Note")),
		"payment_entries": sum(1 for v in export.vouchers if v.kind in ("Receipt", "Payment", "Contra")),
		"journal_entries": sum(1 for v in export.vouchers if v.kind == "Journal"),
		"bank_transactions": 0,
		"uoms": len(export.masters.units),
		"stock_groups": len(export.masters.stock_groups),
		"stock_items": len(export.masters.stock_items),
		"chart_of_accounts": len(export.masters.groups) + len(export.masters.ledgers),
		"parties": 0,
		"required_ledgers": 0,
	}
	counts["masters"] = export.masters.count()
	counts["vouchers"] = len(export.vouchers)
	counts["total"] = counts["masters"] + counts["vouchers"]
	counts["unresolved_ledgers"] = sorted(n for n in _referenced(export) - export.masters.ledger_names() if n)
	return counts


def render_full_export(from_date=None, to_date=None, company=None):
	"""The whole export as two strings, for a caller that wants it in hand.

	`write_full_export` is the path for anything large - it never holds both
	documents at once, which is the difference that matters at eleven thousand
	invoices.
	"""
	company = company or frappe.defaults.get_user_default("Company")
	settings = _settings()
	duplicates, action = _import_options(settings)
	export = extract(company, from_date, to_date)
	return (
		render_masters(export.company, export.masters, duplicates),
		render_vouchers(export.company, export.vouchers, action),
		_counts(export),
	)


XML, EXCEL = "Tally XML", "Excel"

# What the two files are called, per format. The numbers are not decoration:
# masters must be imported first, and a name that sorts is the cheapest way to
# say so to somebody looking at a download folder.
EXTENSION = {XML: "xml", EXCEL: "xlsx"}


def write_full_export(from_date, to_date, company, masters_path, vouchers_path, reporter=None, fmt=XML):
	"""Write the complete export to two files: ERPNext -> extract -> render.

	Two files rather than one because masters must land in Tally before the
	vouchers that name them; a voucher naming a ledger Tally does not hold is
	refused.

	`fmt` picks the renderer and nothing else. Both read the same `Export`, so
	the two formats cannot disagree about what the books say - which is the
	whole reason the model exists between the extract and the writers.

	`reporter` is optional. When one is passed the extract ticks it per voucher
	and names each stage as it begins, so a caller running this in a background
	worker can say what is happening without this function knowing anything
	about jobs.
	"""
	company = company or frappe.defaults.get_user_default("Company")
	settings = _settings()
	duplicates, action = _import_options(settings)

	export = extract(company, from_date, to_date, reporter=reporter)

	# Rendering is one pass over a model already in memory, so it is a stage
	# rather than a count. The reporter deliberately leaves the number where the
	# extract left it instead of easing it upwards over work with no rows.
	if reporter:
		reporter.at("Generating XML")

	if fmt == EXCEL:
		from tally_connector.tally import excel

		# nosemgrep: frappe-security-file-traversal - a path this code made, not one from a request
		with open(vouchers_path, "wb") as fh:
			fh.write(excel.render_vouchers(export.vouchers))
		# nosemgrep: frappe-security-file-traversal - a path this code made, not one from a request
		with open(masters_path, "wb") as fh:
			fh.write(excel.render_masters(export.masters))
	else:
		# The destination is asked once, here, and both files follow from the
		# answer: masters Tally already holds are left out, and so are vouchers
		# this company has already been sent. With Tally unreachable both fall
		# back to sending everything, which is what this always did.
		resolution = destination_resolution(export.masters)
		vouchers, skipped = unsent_vouchers(export.vouchers, company, export.company)
		# nosemgrep: frappe-security-file-traversal - a path this code made, not one from a request
		with open(vouchers_path, "w", encoding="utf-8") as fh:
			fh.write(
				render_vouchers(
					export.company, vouchers, action, send_guid(settings), ledgers_refusing_cost_centres()
				)
			)
		# nosemgrep: frappe-security-file-traversal - a path this code made, not one from a request
		with open(masters_path, "w", encoding="utf-8") as fh:
			fh.write(render_masters(export.company, export.masters, duplicates, resolution))
		counts = _counts(export)
		counts["vouchers_already_sent"] = len(skipped)
		if resolution is not None:
			counts["masters_already_in_tally"] = len(resolution.by_action("reuse"))
			counts["master_conflicts"] = len(resolution.conflicts)
		return counts

	return _counts(export)


def unresolved_ledgers(from_date=None, to_date=None, company=None):
	"""Ledgers the vouchers reference that the masters would not create.

	Zero is the only acceptable answer and the extract guarantees it by
	construction; this is the check rather than the fix, and what the dashboard
	reports before anybody imports anything.
	"""
	company = company or frappe.defaults.get_user_default("Company")
	export = extract(company, from_date, to_date)
	return sorted(n for n in _referenced(export) - export.masters.ledger_names() if n)
