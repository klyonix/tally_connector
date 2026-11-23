"""
tally_connector/api/export.py

Whitelisted methods behind the Tally Connector dashboard and the per-doctype
buttons.

Three things changed here and each was a defect rather than a preference:

* **Every method was open to any signed-in user.** `@frappe.whitelist()` without
  a role check admits anybody with a session, and every generator reads through
  `frappe.get_all`, which ignores permissions by design. On the bench this was
  audited against, ten customer-portal Website Users - people for whom
  `frappe.has_permission("Sales Invoice", "read")` correctly answers False -
  could call `export_all` and receive the full chart of accounts, every invoice
  with amounts, and every party's address and GSTIN. Every method now names the
  roles that may call it, and the company is checked against the caller's own
  permissions rather than defaulted from whatever the database returns first.

* **`export_all` produced one envelope declaring "All Masters" and filled it
  with vouchers.** It now produces the two documents Tally actually imports,
  masters first.

* **`export_single_document` exported the whole day.** It passed the document's
  posting date as both ends of a range, so pressing "Export to Tally" on one
  invoice pushed every invoice sharing that date - while the confirmation dialog
  named a single document.

The ten near-identical per-doctype endpoints are now one table. They had drifted
already: four of them returned the generated XML on the push path and six did
not, for no reason anybody chose.
"""

import os

import frappe
from frappe import _
from frappe.utils import now_datetime, today

from tally_connector.tally.extract import extract
from tally_connector.tally.validate import validate
from tally_connector.utils.connection import (
	TallyConnector,
	push_masters,
	push_to_tally,
	push_vouchers,
)
from tally_connector.utils.exporter import (
	EXCEL,
	EXTENSION,
	SINGLE_DOCUMENT_SECTIONS,
	XML,
	document_parts,
	is_master_section,
	master_section,
	render_document,
	render_section,
	unresolved_ledgers,
	voucher_section,
	write_full_export,
)

# Who may pull the books out of this system. `Accounts User` is included because
# running the export is a clerical act; changing settings and importing back
# into ERPNext are not, and carry the narrower tuple below.
EXPORT_ROLES = ("System Manager", "Accounts Manager", "Accounts User")
ADMIN_ROLES = ("System Manager", "Accounts Manager")


def _read_text(path: str) -> str:
	"""A file this module wrote itself, as text."""
	# nosemgrep: frappe-security-file-traversal - a path this code made, not one from a request
	with open(path, encoding="utf-8") as fh:
		return fh.read()


def _require(roles):
	"""Refuse a caller who holds none of `roles`."""
	if frappe.session.user == "Administrator":
		return
	if not set(roles) & set(frappe.get_roles()):
		frappe.throw(
			_("Not permitted. This action needs one of: {0}.").format(", ".join(roles)),
			frappe.PermissionError,
		)


def _require_sync_enabled():
	"""The master switch, checked before anything is generated or queued.

	One place to stop the connector - during a Tally upgrade, a data cleanup, a
	month-end close - without editing a schedule or removing a role. Refusing
	here rather than in the UI means a scheduled run and a stray API call are
	stopped by the same rule the button obeys.
	"""
	if not frappe.db.get_single_value("Tally Settings", "sync_enabled"):
		frappe.throw(
			_(
				"Tally sync is switched off. Turn it back on from the switch at the "
				"top of the Tally Connector, or in Settings."
			),
			title=_("Sync is off"),
		)


def _resolve_company(company=None):
	"""The company to export, checked against the caller.

	Defaulting to `frappe.db.get_value("Company", {}, "name")` - which is what
	every endpoint did - picks whichever row the database returns first. On a
	site with two companies that silently exported one company's books into the
	other's Tally, and did so without the caller ever naming a company.
	"""
	company = company or frappe.defaults.get_user_default("Company")
	if not company:
		allowed = frappe.get_list("Company", pluck="name", limit=2)
		if len(allowed) == 1:
			company = allowed[0]
		elif not allowed:
			frappe.throw(_("There is no company to export. Set one up in ERPNext first."))
	if not company:
		frappe.throw(_("Choose a company to export. This site has more than one."))
	if not frappe.db.exists("Company", company):
		frappe.throw(_("No company called {0}.").format(company))
	if not frappe.has_permission("Company", doc=company):
		frappe.throw(_("Not permitted to export {0}.").format(company), frappe.PermissionError)
	return company


def _truthy(value):
	"""`frappe.parse_json("false")` returns the string "false", which is true."""
	if isinstance(value, str):
		return value.strip().lower() in ("1", "true", "yes", "on")
	return bool(value)


# ─────────────────────────────────────────────────────────────────────────────
# Export log
# ─────────────────────────────────────────────────────────────────────────────


def _create_log(export_type, export_format, from_date=None, to_date=None, company=None):
	log = frappe.new_doc("Tally Export Log")
	log.export_type = export_type
	log.export_format = export_format
	log.from_date = from_date
	log.to_date = to_date
	log.status = "Pending"
	log.exported_at = now_datetime()
	log.company = company
	log.insert(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit - the log outlives a failed export
	return log.name


def _finalize_log(log_name, records_exported, records_failed, xml_preview=None):
	log = frappe.get_doc("Tally Export Log", log_name)
	log.records_exported = records_exported
	log.records_failed = records_failed
	if xml_preview:
		log.xml_preview = xml_preview[:4000]
	if log.status == "Pending":
		log.status = "Success" if records_failed == 0 else "Partial"
	log.save(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit - the log outlives a failed export


def _fail_log(log_name, error):
	try:
		log = frappe.get_doc("Tally Export Log", log_name)
		log.status = "Failed"
		log.error_log = str(error)[:4000]
		log.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit - the log outlives a failed export
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Tally Connector: could not record failure")


# ─────────────────────────────────────────────────────────────────────────────
# Connection test
# ─────────────────────────────────────────────────────────────────────────────


@frappe.whitelist(methods=["GET", "POST"])
def test_tally_connection():
	"""Test connectivity to the Tally Prime XML server."""
	_require(EXPORT_ROLES)
	try:
		conn = TallyConnector()
		success, response = conn.test_connection()
		return {"success": success, "response": response[:500] if response else ""}
	except Exception as e:
		return {"success": False, "response": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Individual exports - one table, not ten copies
# ─────────────────────────────────────────────────────────────────────────────

_EXPORTS = {
	"chart_of_accounts": "Chart of Accounts",
	"parties": "Parties",
	"uoms": "UOMs",
	"stock_items": "Stock Items",
	"sales_invoices": "Sales Invoice",
	"purchase_invoices": "Purchase Invoice",
	"payment_in": "Payment In",
	"payment_out": "Payment Out",
	"journal_entries": "Journal Entry",
	"bank_transactions": "Bank Transaction",
}


def _would_be_refused(from_date, to_date, company):
	"""The blocking findings for this company and period, as one sentence.

	Empty when there are none. Deliberately re-extracts rather than validating
	the rendered section: a section is a slice of an export, and closure -
	whether a voucher names a ledger no master creates - is a question about
	the whole of it. Validating the slice alone would answer a narrower
	question and call it a pass.
	"""
	try:
		export = extract(company, from_date, to_date)
		# With the destination, always. Without it every master Tally already
		# holds reads as missing, and this function decides whether to send -
		# so a connected Tally full of the right masters refused its own data,
		# 7,581 findings deep, every one of them false.
		report = validate(export, target_company())
		blocking = report.blocking
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Tally Connector: preflight")
		# A validator that cannot run is not a validator that passed.
		return (
			"The export could not be checked before sending, so nothing "
			"was sent. The error log has the detail."
		)
	if not blocking:
		return ""
	reason = "; ".join(f"{f.check}: {f.subject} - {f.detail}" for f in blocking[:5])
	return (
		f"Not sent to Tally: {len(blocking)} blocking "
		f"finding{'s' if len(blocking) != 1 else ''} would make Tally refuse "
		f"this import and lose the rest of the file. {reason}"
	)


def _run_export(key, from_date, to_date, push, company):
	_require_sync_enabled()
	label = _EXPORTS[key]
	company = _resolve_company(company)
	log_name = _create_log(label, "Tally XML", from_date, to_date, company)
	try:
		xml_str, count = render_section(key, from_date, to_date, company)
		_finalize_log(log_name, count, 0, xml_str)
		out = {"success": True, "records": count, "log": log_name, "company": company, "xml": xml_str}
		if push:
			# Validated before it is sent, which this path never did. Ten
			# endpoints and the Desk button on an invoice all arrive here, and
			# all of them used to push whatever rendered - so the one check
			# that knows Tally would refuse the file was skipped on every path
			# except the full export, where it was advisory anyway.
			refusal = _would_be_refused(from_date, to_date, company)
			if refusal:
				out["success"] = False
				out["error"] = refusal
				_fail_log(log_name, refusal)
				return out
			# Masters go a batch at a time and vouchers already did. One
			# 79MB file is what a live TallyPrime answered a memory access
			# violation to, and a bigger timeout does not make it smaller.
			if is_master_section(key):
				name, masters, duplicates, resolution, _n = master_section(key, from_date, to_date, company)
				# `name_refusals` because Tally will not say what it
				# refused - a real batch answered "0 errors and 1 exceptions"
				# and named nothing, which leaves whoever reads this with a
				# batch of 250 and no idea which record to look at. It sends
				# the failing batch again one at a time and puts every refused
				# master in the result by name. Masters can be treated this way
				# and vouchers cannot: a master Tally would not take is simply
				# absent, where a voucher half-imported is accounting nobody
				# can reconcile.
				out["tally"] = push_masters(
					name, masters, duplicates, resolution, log_name, name_refusals=True
				)
			else:
				# Vouchers batch too, and for the same reason masters do: one
				# file is what a live TallyPrime answered a memory access
				# violation to. `push_vouchers` was written for this and had
				# no caller anywhere, so every voucher push has gone as a
				# single document - and nothing was ever written to the sync
				# ledger, which is what stops a retry posting the whole lot
				# into the customer's books a second time.
				part = voucher_section(key, from_date, to_date, company)
				out["tally"] = push_vouchers(
					part["tally_company"],
					part["vouchers"],
					part["action"],
					log_name,
					no_cost_centres=part["no_cost_centres"],
					with_guid=part["with_guid"],
					erpnext_company=part["erpnext_company"],
				)
			if not out["tally"].get("success"):
				out["success"] = False
				refused = out["tally"].get("refused") or []
				if refused:
					# Named, so say the names. Everything else went in.
					shown = ", ".join(repr(r["name"]) for r in refused[:10])
					more = f" and {len(refused) - 10} more" if len(refused) > 10 else ""
					out["error"] = (
						f"Tally refused {len(refused)} of these and took the "
						f"rest: {shown}{more}. They are listed in full under "
						"`tally.refused`."
					)
				else:
					out["error"] = (
						"Tally's answer could not be read, so whether this "
						"arrived is unknown. Check Tally.imp before sending "
						"again."
						if out["tally"].get("unknown")
						else "Tally refused this. " + "; ".join(out["tally"].get("errors") or [])[:400]
					)
		return out
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), f"Tally Connector: {label} export")
		_fail_log(log_name, e)
		return {"success": False, "error": str(e), "log": log_name}


@frappe.whitelist(methods=["POST"])
def export_chart_of_accounts(
	from_date: str | None = None,
	to_date: str | None = None,
	push_to_tally_flag: bool = False,
	company: str | None = None,
):
	_require(EXPORT_ROLES)
	_require_sync_enabled()
	return _run_export("chart_of_accounts", from_date, to_date, _truthy(push_to_tally_flag), company)


@frappe.whitelist(methods=["POST"])
def export_parties(
	from_date: str | None = None,
	to_date: str | None = None,
	push_to_tally_flag: bool = False,
	company: str | None = None,
):
	_require(EXPORT_ROLES)
	_require_sync_enabled()
	return _run_export("parties", from_date, to_date, _truthy(push_to_tally_flag), company)


@frappe.whitelist(methods=["POST"])
def export_uoms(
	from_date: str | None = None,
	to_date: str | None = None,
	push_to_tally_flag: bool = False,
	company: str | None = None,
):
	_require(EXPORT_ROLES)
	_require_sync_enabled()
	return _run_export("uoms", from_date, to_date, _truthy(push_to_tally_flag), company)


@frappe.whitelist(methods=["POST"])
def export_stock_items(
	from_date: str | None = None,
	to_date: str | None = None,
	push_to_tally_flag: bool = False,
	company: str | None = None,
):
	_require(EXPORT_ROLES)
	_require_sync_enabled()
	return _run_export("stock_items", from_date, to_date, _truthy(push_to_tally_flag), company)


@frappe.whitelist(methods=["POST"])
def export_sales_invoices(
	from_date: str | None = None,
	to_date: str | None = None,
	push_to_tally_flag: bool = False,
	company: str | None = None,
):
	_require(EXPORT_ROLES)
	_require_sync_enabled()
	return _run_export("sales_invoices", from_date, to_date, _truthy(push_to_tally_flag), company)


@frappe.whitelist(methods=["POST"])
def export_purchase_invoices(
	from_date: str | None = None,
	to_date: str | None = None,
	push_to_tally_flag: bool = False,
	company: str | None = None,
):
	_require(EXPORT_ROLES)
	_require_sync_enabled()
	return _run_export("purchase_invoices", from_date, to_date, _truthy(push_to_tally_flag), company)


@frappe.whitelist(methods=["POST"])
def export_payment_in(
	from_date: str | None = None,
	to_date: str | None = None,
	push_to_tally_flag: bool = False,
	company: str | None = None,
):
	_require(EXPORT_ROLES)
	_require_sync_enabled()
	return _run_export("payment_in", from_date, to_date, _truthy(push_to_tally_flag), company)


@frappe.whitelist(methods=["POST"])
def export_payment_out(
	from_date: str | None = None,
	to_date: str | None = None,
	push_to_tally_flag: bool = False,
	company: str | None = None,
):
	_require(EXPORT_ROLES)
	_require_sync_enabled()
	return _run_export("payment_out", from_date, to_date, _truthy(push_to_tally_flag), company)


@frappe.whitelist(methods=["POST"])
def export_journal_entries(
	from_date: str | None = None,
	to_date: str | None = None,
	push_to_tally_flag: bool = False,
	company: str | None = None,
):
	_require(EXPORT_ROLES)
	_require_sync_enabled()
	return _run_export("journal_entries", from_date, to_date, _truthy(push_to_tally_flag), company)


@frappe.whitelist(methods=["POST"])
def export_bank_transactions(
	from_date: str | None = None,
	to_date: str | None = None,
	push_to_tally_flag: bool = False,
	company: str | None = None,
):
	_require(EXPORT_ROLES)
	_require_sync_enabled()
	return _run_export("bank_transactions", from_date, to_date, _truthy(push_to_tally_flag), company)


# ─────────────────────────────────────────────────────────────────────────────
# Full export
# ─────────────────────────────────────────────────────────────────────────────

# How many documents make this worth handing to a worker rather than answering
# in the request. A year of a real ledger is tens of thousands; the request that
# prompted this carried eleven thousand sales invoices and died on a 504.
BACKGROUND_THRESHOLD = 2000


def _attach_xml(log_name, filename, path):
	"""Store a generated document as a private File against the export log.

	The XML no longer travels in the HTTP response. Two documents totalling tens
	of megabytes, JSON-encoded into a reply the browser then has to hold, is a
	gateway timeout before it is a download - which is exactly what a year of
	invoices produced.
	"""
	# nosemgrep: frappe-security-file-traversal - a path this code made, not one from a request
	with open(path, "rb") as fh:
		content = fh.read()
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": filename,
			"content": content,
			"is_private": 1,
			"attached_to_doctype": "Tally Export Log",
			"attached_to_name": log_name,
		}
	)
	file_doc.insert(ignore_permissions=True)
	return file_doc.file_url


def _estimate(from_date, to_date, company):
	"""Roughly how many vouchers this range holds, for the background decision."""
	total = 0
	for doctype, date_field in (
		("Sales Invoice", "posting_date"),
		("Purchase Invoice", "posting_date"),
		("Payment Entry", "posting_date"),
		("Journal Entry", "posting_date"),
		("Bank Transaction", "date"),
	):
		filters = {"docstatus": 1, "company": company}
		if from_date and to_date:
			filters[date_field] = ["between", [from_date, to_date]]
		try:
			total += frappe.db.count(doctype, filters)
		except Exception:
			pass
	return total


def _group_findings(findings, limit=40):
	"""The validator's findings, grouped the way somebody reads them.

	One row per check rather than one per finding: 465 party lines missing a
	bill reference is a single thing to fix, and a list of 465 is a list nobody
	reads. Stored on the log so the panel can say **what** the warnings were -
	a count on its own tells somebody there is a problem and nothing about it,
	which is what a "2 warnings" with no way to see them amounted to.

	Capped, because this is a field on a row and not a report. The count is the
	true total either way; only the examples are limited.
	"""
	groups = {}
	for f in findings:
		key = (f.severity, f.check)
		group = groups.setdefault(
			key,
			{
				"severity": f.severity,
				"check": f.check,
				"count": 0,
				"subject": f.subject,
				"detail": f.detail,
				"tally_says": getattr(f, "tally_says", "") or "",
			},
		)
		group["count"] += 1
	rank = {"aborts": 0, "rejects": 1, "wrong": 2}
	ordered = sorted(groups.values(), key=lambda g: (rank.get(g["severity"], 9), -g["count"]))
	return ordered[:limit]


def _model_for(from_date, to_date, company):
	"""The same `Export` the writers were handed, for a format the validator
	cannot read back off disk."""
	from tally_connector.tally.extract import extract

	return extract(company, from_date, to_date)


def run_full_export(log_name, from_date, to_date, company, push=False, export_format=XML):
	"""Generate the two documents and attach them to the log.

	Runs in the request for a small range and in a background worker for a large
	one; either way this is the single place the work happens, so the two paths
	cannot drift.

	It reports as it goes. Every stage below is one this function actually
	reaches, and every count comes from the extract ticking a real voucher - so
	a bar that is not moving means work that is not moving, which is the only
	useful thing a progress bar can tell somebody.
	"""
	import tempfile

	from tally_connector.api.jobs import EXPORT_STAGES, JobReporter
	from tally_connector.tally.extract import count_vouchers

	reporter = JobReporter("Tally Export Log", log_name, EXPORT_STAGES)
	try:
		_per_source, total = count_vouchers(company, from_date, to_date)
		reporter.start(total=total)

		with tempfile.TemporaryDirectory() as tmp:
			tag = company.replace(" ", "_")
			ext = EXTENSION.get(export_format, "xml")
			masters_path = os.path.join(tmp, f"01_masters.{ext}")
			vouchers_path = os.path.join(tmp, f"02_vouchers.{ext}")
			counts = write_full_export(
				from_date, to_date, company, masters_path, vouchers_path, reporter=reporter, fmt=export_format
			)

			reporter.at("Final Validation")
			unresolved = counts.get("unresolved_ledgers", [])

			# The file is read back and held against Tally's rules before
			# anybody is told it worked.
			#
			# Tally aborts an import at the first line it cannot parse and
			# reports only that line - one bad unit symbol took all 744 masters
			# of the audited import with it. An export that reports Success and
			# then dies at line 48 is the most expensive thing this can do: the
			# failure surfaces in Tally's event log an hour later, naming
			# nothing that leads back here.
			#
			# So the same validator the Validate button runs is run over the
			# bytes actually written, and a blocking finding makes the job
			# Partial with the reason on it. The files are still kept - they are
			# what somebody needs in order to see what is wrong.
			blocking, findings = [], []
			try:
				from tally_connector.tally.read import read_export
				from tally_connector.tally.validate import validate

				if export_format == EXCEL:
					# Reading the written bytes back is an XML-only check, and
					# it is the renderer this has ever caught a fault in. The
					# workbook is written from the same `Export` every check
					# here reads, so the checks all still run - what is not
					# covered is a fault in the workbook writer itself, and that
					# is said rather than implied.
					verdict = validate(_model_for(from_date, to_date, company))
				else:
					verdict = validate(
						read_export(
							_read_text(masters_path),
							_read_text(vouchers_path),
						)
					)
				blocking = [
					{"check": f.check, "subject": f.subject, "detail": f.detail} for f in verdict.blocking
				]
				findings = _group_findings(verdict.findings)
				# The count is the findings, and only the findings. It used to
				# be the findings **plus** the unresolved ledgers, and
				# `_check_closure` already raises one finding per unresolved
				# ledger - so every one of them was counted twice, and a job
				# with three missing ledgers reported six warnings.
				reporter.warnings = len(verdict.findings)
			except Exception:
				# A validator that throws must not lose an export that worked.
				frappe.log_error(frappe.get_traceback(), "Tally Connector: final validation")

			masters_url = _attach_xml(log_name, f"01_masters_{tag}.{ext}", masters_path)
			vouchers_url = _attach_xml(log_name, f"02_vouchers_{tag}.{ext}", vouchers_path)

			result = {
				"success": True,
				"log": log_name,
				"company": company,
				"format": export_format,
				"records": counts["total"],
				"counts": {k: v for k, v in counts.items() if k != "unresolved_ledgers"},
				"unresolved_ledgers": unresolved,
				"blocking": blocking,
				"masters_url": masters_url,
				"vouchers_url": vouchers_url,
			}

			if push and export_format == EXCEL:
				# Tally's gateway takes XML. A workbook is a file somebody
				# imports by hand, so a push is refused in words rather than
				# silently ignored, which would report a send that never left.
				result["success"] = False
				result["error"] = (
					"Excel is a file you import in Tally. "
					"Push to Tally sends XML over the gateway - "
					"export as Tally XML to use it."
				)
				reporter.finish("Failed", error=result["error"], records=counts["total"])
				return result

			# Nothing is sent while the validator says Tally would refuse it.
			#
			# The files are still written and attached, because they are what
			# somebody needs in order to see what is wrong. That reasoning was
			# right and was applied to the wrong side of the push: an export
			# carrying an ABORTS finding used to be sent to Tally in full, and
			# since Tally stops at the first record it cannot accept, the rest
			# of the file was lost with it.
			if push and blocking:
				reason = "; ".join(f"{b['check']}: {b['subject']} - {b['detail']}" for b in blocking[:5])
				result["success"] = False
				result["error"] = (
					f"Not sent to Tally: {len(blocking)} blocking "
					f"finding{'s' if len(blocking) != 1 else ''} would make Tally "
					f"refuse this import and lose the rest of the file. {reason}"
				)
				reporter.summarise(findings)
				reporter.finish("Partial", records=counts["total"], error=result["error"])
				frappe.db.commit()  # nosemgrep: frappe-manual-commit - the polling screen reads this log
				return result

			if push:
				# Masters first, and stop if they fail: vouchers sent into a
				# company without the ledgers are refused line by line.
				reporter.at("Sending to Tally")
				masters_xml = _read_text(masters_path)
				master_result = push_to_tally(masters_xml, log_name)
				result["tally_masters"] = master_result
				del masters_xml
				if not master_result.get("success"):
					result["success"] = False
					if master_result.get("unknown"):
						result["error"] = (
							"Tally's answer to the masters could not be read, so "
							"whether they arrived is unknown and the vouchers "
							"were not sent. Check Tally.imp before sending again."
						)
					else:
						result["error"] = (
							"The masters were refused by Tally, so the vouchers "
							"were not sent. " + "; ".join(master_result.get("errors") or [])[:400]
						)
					reporter.summarise(findings)
					reporter.finish(
						"Partial" if master_result.get("unknown") else "Failed",
						error=result["error"],
						records=counts["total"],
					)
					frappe.db.commit()  # nosemgrep: frappe-manual-commit - the polling screen reads this log
					return result
				vouchers_xml = _read_text(vouchers_path)
				voucher_result = push_to_tally(vouchers_xml, log_name)
				result["tally_vouchers"] = voucher_result
				del vouchers_xml

			reporter.summarise(findings)

			# A job that pushed is judged on what Tally said. `finish` used to
			# write "Success" straight over the push's own verdict, so a
			# vouchers push Tally rejected line by line was reported as a
			# completed export.
			pushed = result.get("tally_vouchers") if push else None
			if blocking:
				reason = "; ".join(f"{b['check']}: {b['subject']} - {b['detail']}" for b in blocking[:5])
				reporter.finish(
					"Partial",
					records=counts["total"],
					error=(
						f"Tally would refuse this import - {len(blocking)} blocking "
						f"finding{'s' if len(blocking) != 1 else ''}. {reason}"
					),
				)
			elif pushed is not None and not pushed.get("success"):
				result["success"] = False
				result["error"] = (
					"Tally's answer to the vouchers could not be read, so whether "
					"they arrived is unknown. Check Tally.imp before sending again."
					if pushed.get("unknown")
					else "Tally refused some or all of the vouchers. "
					+ "; ".join(pushed.get("errors") or [])[:400]
				)
				reporter.finish(
					"Partial" if (pushed.get("unknown") or pushed.get("created")) else "Failed",
					error=result["error"],
					records=counts["total"],
				)
			else:
				reporter.finish("Partial" if unresolved else "Success", records=counts["total"])
			frappe.db.commit()  # nosemgrep: frappe-manual-commit - the polling screen reads this log
			return result

	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Tally Connector: Full export")
		# The reporter writes the failure rather than `_fail_log`, so a job that
		# dies mid-run stops reporting a stage it is no longer in. A log left
		# saying "Processing Sales" for ever is how a dead job passes for a slow
		# one.
		reporter.finish("Failed", error=e)
		return {"success": False, "error": str(e), "log": log_name}


@frappe.whitelist(methods=["POST"])
def export_all(
	from_date: str | None = None,
	to_date: str | None = None,
	export_format: str = "Tally XML",
	push_to_tally_flag: bool = False,
	company: str | None = None,
	background: bool | None = None,
):
	"""Everything, as two files: masters then vouchers.

	The documents are attached to the export log and returned as URLs rather
	than as XML in the response body. Above `BACKGROUND_THRESHOLD` vouchers the
	work is handed to a worker and the caller polls `export_status`, because no
	amount of optimisation makes a year of a large ledger fit inside one HTTP
	request.
	"""
	_require(EXPORT_ROLES)
	_require_sync_enabled()
	push = _truthy(push_to_tally_flag)
	company = _resolve_company(company)
	log_name = _create_log("Full Export", export_format, from_date, to_date, company)

	estimate = _estimate(from_date, to_date, company)
	if background is None:
		background = estimate > BACKGROUND_THRESHOLD
	else:
		background = _truthy(background)

	if background:
		job = frappe.enqueue(
			"tally_connector.api.export.run_full_export",
			queue="long",
			timeout=3600,
			log_name=log_name,
			from_date=from_date,
			to_date=to_date,
			company=company,
			push=push,
			export_format=export_format,
		)
		frappe.db.set_value(
			"Tally Export Log",
			log_name,
			{
				"job_id": getattr(job, "id", None),
				"status": "Pending",
				"stage": "Queued",
				"total_records": estimate,
				"started_at": now_datetime(),
			},
			update_modified=False,
		)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit - the queued job reads this row
		return {
			"success": True,
			"queued": True,
			"log": log_name,
			"company": company,
			"estimate": estimate,
			"message": f"About {estimate:,} vouchers. Running in the background.",
		}

	return run_full_export(log_name, from_date, to_date, company, push, export_format)


@frappe.whitelist(methods=["GET", "POST"])
def export_status(log: str):
	"""Where a background export has got to, and the files if it has finished."""
	_require(EXPORT_ROLES)
	doc = frappe.get_doc("Tally Export Log", log)
	files = frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Tally Export Log", "attached_to_name": log},
		fields=["file_name", "file_url"],
		order_by="file_name asc",
	)
	return {
		"success": True,
		"log": log,
		"status": doc.status,
		"records": doc.records_exported,
		"error": doc.error_log,
		"masters_url": next((f.file_url for f in files if "masters" in f.file_name), None),
		"vouchers_url": next((f.file_url for f in files if "vouchers" in f.file_name), None),
	}


def target_company(ask_tally=True):
	"""What we know about the destination Tally company.

	Asking is read-only and **on by default**, which it was not. It used to
	default to not asking, so the validator judged every export against only
	the masters that one file creates - and a master already in Tally read as
	one that exists nowhere. On a real company that produced **7,581 findings
	naming ledgers, units and cost centres Tally was holding at the time**:
	4,798 ledgers, 1,365 units, 1,418 cost centres, every one of them present.

	That is not a noisy report, it is a blocked migration. `_would_be_refused`
	reads exactly these findings to decide whether to send, so a connected
	Tally full of the right masters refused its own data.

	A Tally that is unreachable still leaves the checks that need it skipped
	and says so, rather than failing the export - the offline harness exists
	precisely so Tally does not have to be there. What changed is which way
	round the default sits: ask when there is something to ask.
	"""
	from tally_connector.tally.validate import TargetCompany
	from tally_connector.utils.connection import TallyConnector

	settings = frappe.get_single("Tally Settings")
	target = TargetCompany(name=(settings.company_name_in_tally or "").strip())
	if not _truthy(ask_tally):
		return target
	try:
		conn = TallyConnector()
		reachable, _response = conn.test_connection()
		if not reachable:
			return target
		snapshot = conn.snapshot()
		target.snapshot = snapshot if snapshot.anything_known() else None
		target.duplicate_guard = conn.voucher_type_details() or {}
		target.voucher_types = set(target.duplicate_guard)
		target.books_from, target.period_to = conn.company_period()
		target.known = bool(target.voucher_types or target.books_from)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Tally Connector: company probe")
	return target


# ─────────────────────────────────────────────────────────────────────────────
# Findings, linked back to the record somebody has to fix
# ─────────────────────────────────────────────────────────────────────────────

# Which checks name an ERPNext document in their `subject`, and which DocTypes
# that subject could be. Deliberately not exhaustive: a ledger name has had the
# company abbreviation stripped off it, so it is *not* an `Account` name and a
# link built from it would send somebody to a 404. A finding with no link is a
# finding with no link - it says nothing false.
# The keys are the `check` names the validator emits, and five of the nine used
# to be a different spelling of them - `duplicates`, `bill_wise`, `dates`,
# `journal_cash`, `invoice_view` against the validator's `duplicate`,
# `bill-wise`, `date`, `journal cash`, `invoice view`. Those five findings never
# linked to anything, silently, because a key that matches nothing looks exactly
# like a subject that resolves to nothing. `test_validate` holds the two sides
# together now so a renamed check cannot go quiet again.
_VOUCHER_DOCTYPES = ("Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry")
_LINKABLE = {
	"balance": _VOUCHER_DOCTYPES,
	"duplicate": _VOUCHER_DOCTYPES,
	"bill-wise": _VOUCHER_DOCTYPES,
	"party": _VOUCHER_DOCTYPES,
	"date": _VOUCHER_DOCTYPES,
	"journal cash": ("Journal Entry",),
	"invoice view": ("Sales Invoice", "Purchase Invoice"),
	"hsn": ("Item",),
	"unit": ("UOM",),
}

_SLUG = {
	"Sales Invoice": "sales-invoice",
	"Purchase Invoice": "purchase-invoice",
	"Payment Entry": "payment-entry",
	"Journal Entry": "journal-entry",
	"Item": "item",
	"UOM": "uom",
}


def _add_links(findings):
	"""Point each finding at the ERPNext record behind it, where there is one.

	Resolved in bulk rather than per finding: a report can carry hundreds, and
	one `exists` call each would make the check that exists to be fast the
	slowest thing on the page. An unresolved subject is left without a link.
	"""
	wanted = {}
	for f in findings:
		subject = (f.get("subject") or "").strip()
		if subject and f.get("check") in _LINKABLE:
			wanted.setdefault(f["check"], set()).add(subject)

	found = {}
	for check, subjects in wanted.items():
		for doctype in _LINKABLE[check]:
			unresolved = [s for s in subjects if (check, s) not in found]
			if not unresolved:
				break
			try:
				rows = frappe.get_all(
					doctype, filters={"name": ["in", unresolved]}, pluck="name", limit_page_length=0
				)
			except Exception:
				continue
			for name in rows:
				found[(check, name)] = f"/app/{_SLUG[doctype]}/{name}"

	for f in findings:
		link = found.get((f.get("check"), (f.get("subject") or "").strip()))
		if link:
			f["link"] = link
	return findings


def certify_export(company, report, counts=None, ask_tally=False):
	"""One verdict over the validator, the destination and the connection.

	Asking Tally is optional and stays optional: an unasked question reads as
	UNKNOWN rather than PASS, and UNKNOWN does not block - Tally being switched
	off has never been a reason to refuse an offline export, and making it one
	would break the workflow this connector exists for.
	"""
	from tally_connector.tally.certify import certify

	snapshot = resolution = None
	reachable = None
	if _truthy(ask_tally):
		try:
			conn = TallyConnector()
			snapshot = conn.snapshot()
			reachable = snapshot.anything_known() or bool(snapshot.companies)
			if snapshot.anything_known():
				from tally_connector.tally.resolve import resolve

				export = extract(company, None, None)
				resolution = resolve(export.masters, snapshot)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Tally Connector: destination snapshot")
			reachable = False

	settings = frappe.get_single("Tally Settings")
	return certify(
		erpnext_company=company,
		tally_company=(settings.company_name_in_tally or "").strip(),
		report=report,
		resolution=resolution,
		snapshot=snapshot,
		reachable=reachable,
		counts={k: v for k, v in (counts or {}).items() if isinstance(v, int)},
	)


@frappe.whitelist(methods=["GET", "POST"])
def preflight(
	from_date: str | None = None,
	to_date: str | None = None,
	company: str | None = None,
	ask_tally: bool = False,
):
	"""A dry run: generate the export, validate it, throw the files away.

	Nothing is written to Tally and nothing is attached to a log. The point is
	to answer "would this import?" before anybody finds out from Tally's event
	log, which is where every defect in this connector has been found so far.

	The export still runs through the original generators, which do not know the
	normalised model - so the XML they produce is read back into the model and
	validated there. One validator, whichever side the data came from.
	"""
	_require(EXPORT_ROLES)
	import tempfile

	from tally_connector.tally.read import malformed_amounts, read_export
	from tally_connector.tally.validate import validate

	company = _resolve_company(company)
	with tempfile.TemporaryDirectory() as tmp:
		masters_path = os.path.join(tmp, "m.xml")
		vouchers_path = os.path.join(tmp, "v.xml")
		counts = write_full_export(from_date, to_date, company, masters_path, vouchers_path)
		masters_xml = _read_text(masters_path)
		vouchers_xml = _read_text(vouchers_path)

	report = validate(read_export(masters_xml, vouchers_xml), target_company(ask_tally))

	# The reader coerces a value it cannot parse so the rest of the file can
	# still be checked; by then the evidence is gone, so it is collected here.
	for raw in malformed_amounts(masters_xml) + malformed_amounts(vouchers_xml):
		report.add(
			"aborts",
			"amount",
			raw,
			"not a number Tally can read - the import stops at this line",
			"(import aborts at the line)",
		)

	payload = report.to_dict()
	_add_links(payload.get("findings", []))

	certification = certify_export(company, report, counts, ask_tally)

	return {
		"success": True,
		"company": company,
		"counts": {k: v for k, v in counts.items() if k != "unresolved_ledgers"},
		"unresolved_ledgers": counts.get("unresolved_ledgers", []),
		"validation": payload,
		"certification": certification.to_dict(),
		"certification_text": certification.as_text(),
		# Kept for the existing screens, and now the same answer the
		# certification gives rather than a second opinion beside it.
		"would_import": certification.ready,
	}


@frappe.whitelist(methods=["GET", "POST"])
def verify_in_tally(from_date: str | None = None, to_date: str | None = None, company: str | None = None):
	"""What ERPNext sent, what Tally holds, and what is missing, by name.

	Tally's answer to an import says how many vouchers it created and, when it
	feels like it, a reason or two - but never **which** ones failed. On a real
	run it reported `Created: 179, Errors: 108` and named none of the 108, so
	the only way to find them was a trial balance.

	This asks Tally afterwards what is actually in its Day Book and lines that
	up against what this export would send, so the answer is a list of ERPNext
	documents rather than a count.

	One honest limit, and it is Tally's: a voucher type set to automatic
	numbering makes Tally assign its own number and discard ours, and such a
	voucher cannot be matched back by number. Those are reported separately
	rather than counted as missing, because calling them missing would send
	somebody looking for vouchers that are sitting there under another name.
	"""
	_require(EXPORT_ROLES)
	company = _resolve_company(company)
	settings = frappe.get_single("Tally Settings")
	conn = TallyConnector()

	export = extract(company, from_date, to_date)
	sent = {(v.number or "").strip(): v for v in export.vouchers if v.number}

	held = conn.vouchers_in_period(from_date or "1900-01-01", to_date or "2099-12-31")
	if held is None:
		return {
			"success": False,
			"company": company,
			"error": (
				"Tally did not answer, so what it holds is unknown. "
				"Nothing is reported as missing on a guess."
			),
		}

	numbers = {row["number"] for row in held if row["number"]}
	# A number Tally assigned itself, which ours can never match.
	renumbered = [
		row for row in held if row["number"] and row["number"] not in sent and row["number"].isdigit()
	]
	missing = [v for number, v in sorted(sent.items()) if number not in numbers]

	by_kind = {}
	for voucher in missing:
		by_kind[voucher.kind] = by_kind.get(voucher.kind, 0) + 1

	return {
		"success": True,
		"company": company,
		"tally_company": (settings.company_name_in_tally or "").strip(),
		"from_date": from_date,
		"to_date": to_date,
		"sent": len(sent),
		"in_tally": len(held),
		"missing": len(missing),
		"missing_by_kind": by_kind,
		"auto_numbered_in_tally": len(renumbered),
		"rows": [
			{
				"document": v.remote_id or v.number,
				"voucher_number": v.number,
				"kind": v.kind,
				"date": str(v.date) if v.date else "",
			}
			for v in missing[:500]
		],
	}


@frappe.whitelist(methods=["GET", "POST"])
def stale_in_tally(company: str | None = None):
	"""Vouchers Tally holds that ERPNext has cancelled or amended.

	Read-only, and read-only is the whole design. Only a submitted document is
	ever sent to Tally, so a cancelled one never goes; what this answers is the
	other direction - Tally holding a voucher for a document ERPNext has since
	withdrawn. The connector reports it and a person settles it in Tally, where
	they can see what the voucher affects.
	"""
	_require(EXPORT_ROLES)
	from tally_connector.utils.sync_ledger import mark_stale, stale

	company = _resolve_company(company)
	settings = frappe.get_single("Tally Settings")
	tally_company = (settings.company_name_in_tally or "").strip()
	rows = stale(company, tally_company)
	mark_stale(rows)
	return {
		"success": True,
		"company": company,
		"tally_company": tally_company,
		"count": len(rows),
		"rows": [
			{
				"document": row.source_name,
				"doctype": row.source_doctype,
				"voucher_type": row.voucher_type,
				"voucher_number": row.voucher_number,
				"reason": reason,
				"replaced_by": successor,
			}
			for row, reason, successor in rows[:500]
		],
	}


@frappe.whitelist(methods=["GET", "POST"])
def check_target_company():
	"""Ask the destination Tally what it is, read-only.

	Separate from exporting on purpose: it is the only part of validation that
	needs Tally to be switched on.
	"""
	_require(EXPORT_ROLES)
	target = target_company(ask_tally=True)
	return {
		"success": True,
		"reachable": target.known,
		"company": target.name,
		"books_from": str(target.books_from) if target.books_from else None,
		"period_to": str(target.period_to) if target.period_to else None,
		"voucher_types": sorted(target.voucher_types),
		# None means Tally answered without saying, and the check is skipped.
		"prevents_duplicates": {k: v for k, v in target.duplicate_guard.items() if v is not None},
	}


# ─────────────────────────────────────────────────────────────────────────────
# Single document
# ─────────────────────────────────────────────────────────────────────────────


@frappe.whitelist(methods=["POST"])
def export_single_document(doctype: str, docname: str, push_to_tally_flag: bool = False):
	"""Export exactly the document named, and nothing else.

	It used to pass `posting_date` as both ends of a date range, so this
	exported every document of that type sharing the date while the button's own
	confirmation named one. On a day with twelve invoices that is eleven
	vouchers nobody asked to send, and Tally has no undo.
	"""
	_require(EXPORT_ROLES)
	if doctype not in SINGLE_DOCUMENT_SECTIONS:
		frappe.throw(_("{0} cannot be exported to Tally on its own.").format(_(doctype)))
	if not frappe.has_permission(doctype, "read", doc=docname):
		frappe.throw(_("Not permitted to read {0} {1}.").format(_(doctype), docname), frappe.PermissionError)

	doc = frappe.get_doc(doctype, docname)
	if doc.docstatus != 1:
		frappe.throw(_("Only a submitted document can be exported to Tally."))
	company = _resolve_company(getattr(doc, "company", None))

	xml_str, count = render_document(doctype, doc.name, str(doc.posting_date), company)
	if not count:
		frappe.throw(
			_("{0} {1} produced no voucher. Check that it belongs to {2}.").format(
				_(doctype), docname, company
			)
		)

	log_name = _create_log(doctype, "Tally XML", str(doc.posting_date), str(doc.posting_date), company)
	_finalize_log(log_name, count, 0, xml_str)

	out = {"success": True, "records": count, "log": log_name, "company": company, "xml": xml_str}
	if _truthy(push_to_tally_flag):
		# The same gate the bulk export runs. This path skipped it, so the one
		# button somebody presses every day was the one that sent whatever
		# rendered - and a voucher Tally refuses loses the rest of its file.
		refusal = _would_be_refused(str(doc.posting_date), str(doc.posting_date), company)
		if refusal:
			out["success"] = False
			out["error"] = refusal
			_fail_log(log_name, refusal)
			return out

		# Through `push_vouchers`, so the send is recorded. Pushed directly,
		# this wrote nothing to the sync ledger: pressing the button twice
		# posted the document to Tally twice, with nothing anywhere saying it
		# had already gone. That matters most here, because this is the path
		# somebody uses by hand rather than once at migration.
		part = document_parts(doctype, doc.name, str(doc.posting_date), company)
		mine = part["vouchers"]
		if not mine:
			out["tally"] = {
				"success": True,
				"reached": True,
				"created": 0,
				"altered": 0,
				"sent": 0,
				"errors": [],
				"already_sent": True,
			}
			out["error"] = f"{doctype} {docname} has already been sent to Tally, so nothing was sent again."
			return out
		out["tally"] = push_vouchers(
			part["tally_company"],
			mine,
			part["action"],
			log_name,
			no_cost_centres=part["no_cost_centres"],
			with_guid=part["with_guid"],
			erpnext_company=part["erpnext_company"],
		)
		if not out["tally"].get("success"):
			out["success"] = False
			out["error"] = "Tally refused this. " + "; ".join(out["tally"].get("errors") or [])[:400]
	return out


@frappe.whitelist(methods=["GET", "POST"])
def companies():
	"""Companies this caller may export, for the dashboard's picker."""
	_require(EXPORT_ROLES)
	return frappe.get_list("Company", fields=["name", "abbr"], order_by="name")
