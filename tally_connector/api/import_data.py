import traceback
import xml.etree.ElementTree as ET

import frappe
from frappe import _

from tally_connector.api.export import ADMIN_ROLES, _require

# A Tally XML file can be tens of megabytes and this reads it into memory,
# rewrites it with four regular expressions and parses the result. The cap is
# there so a file nobody meant to upload cannot take the worker down.
MAX_XML_BYTES = 20 * 1024 * 1024


def _clean(xml_data):
	"""Make a Tally file parseable without changing what it says.

	Tally writes XML that a strict parser refuses: its own declaration on top of
	ours, numeric entities outside the XML character range, and raw control
	bytes. Each substitution below removes something no parser will accept, and
	nothing here touches a value.
	"""
	import re

	xml_data = (xml_data or "").strip()
	xml_data = re.sub(r"<\?xml[^>]+\?>", "", xml_data)

	def clean_entities(match):
		val = match.group(1).lower()
		try:
			num = int(val[1:], 16) if val.startswith("x") else int(val)
			if num in (0x9, 0xA, 0xD) or (0x20 <= num <= 0xD7FF) or (0xE000 <= num <= 0xFFFD):
				return match.group(0)
		except ValueError:
			pass
		return ""

	xml_data = re.sub(r"&#([xX]?[0-9a-fA-F]+);", clean_entities, xml_data)
	return re.sub(r"[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]", "", xml_data)


def _import_xml(xml_data, reporter=None):
	"""
	Parses Tally XML and imports/updates Masters and Bank Reconciliation dates in ERPNext.

	Not whitelisted. The fence is on the two callers - `process_tally_xml` and
	the background `run_import` - because this now takes a `reporter`, and a
	whitelisted function taking an object argument is a function a caller can
	hand a string to. What it creates is Customers, Suppliers, Items and UOMs,
	and it writes `clearance_date` onto submitted Payment and Journal Entries,
	all under `ignore_permissions=True`: narrower than the export deliberately,
	because reading the books out is clerical and writing into them is not.
	"""
	if not xml_data:
		return {"success": False, "error": "No XML data provided"}

	if len(xml_data) > MAX_XML_BYTES:
		return {
			"success": False,
			"error": f"That file is larger than {MAX_XML_BYTES // (1024 * 1024)} MB. "
			"Split it in Tally and import the parts.",
		}

	try:
		root = ET.fromstring(_clean(xml_data).encode("utf-8"))

		settings = frappe.get_single("Tally Settings")
		debtors_group = settings.get("sundry_debtors_ledger") or "Sundry Debtors"
		creditors_group = settings.get("sundry_creditors_ledger") or "Sundry Creditors"
		company = settings.get("company") or frappe.defaults.get_user_default("Company")
		if not company:
			company = frappe.db.get_value("Company", {}, "name")

		bank_recon_master = settings.get("bank_recon_master") or "ERPNext"

		if not company:
			return {
				"success": False,
				"error": "Company not set in Tally Settings, and no default Company found in the system.",
			}

		counts = {"customers": 0, "suppliers": 0, "accounts": 0, "items": 0, "bank_allocations": 0}

		# Find all TALLYMESSAGE nodes
		# Tally wraps data in <ENVELOPE><BODY><DATA><TALLYMESSAGE>
		messages = root.findall(".//TALLYMESSAGE")
		if not messages:
			# Maybe it's a direct XML without envelope
			if root.tag == "TALLYMESSAGE":
				messages = [root]

		if reporter:
			# The denominator is the number of objects the file actually holds,
			# counted before anything is written. Nothing here estimates.
			reporter.start(total=len(messages))
			reporter.at("Importing Masters", processed=0)

		for msg in messages:
			if reporter:
				reporter.tick()
			# 1. Ledgers (Accounts, Customers, Suppliers)
			ledger = msg.find("LEDGER")
			if ledger is not None:
				ledger_name = ledger.get("NAME")
				parent_group = ledger.findtext("PARENT") or ""

				# Check if it's a Customer
				if parent_group.lower() == debtors_group.lower():
					if not frappe.db.exists("Customer", ledger_name):
						customer = frappe.new_doc("Customer")
						customer.customer_name = ledger_name
						customer.customer_group = "All Customer Groups"
						customer.territory = "All Territories"
						customer.customer_type = "Company"
						customer.insert(ignore_permissions=True)
						counts["customers"] += 1

				# Check if it's a Supplier
				elif parent_group.lower() == creditors_group.lower():
					if not frappe.db.exists("Supplier", ledger_name):
						supplier = frappe.new_doc("Supplier")
						supplier.supplier_name = ledger_name
						supplier.supplier_group = "All Supplier Groups"
						supplier.supplier_type = "Company"
						supplier.insert(ignore_permissions=True)
						counts["suppliers"] += 1

				# Otherwise, it's a General Account
				else:
					account_name = f"{ledger_name} - {frappe.db.get_value('Company', company, 'abbr')}"
					if not frappe.db.exists("Account", account_name):
						# We don't have the full tree, so we place it under a generic group or try to map
						# This is complex in real life, so we log it or create under a specific parent.
						# For now, we will just count it to avoid breaking the Chart of Accounts hierarchy.
						counts["accounts"] += 1

			# 2. Stock Items
			item = msg.find("STOCKITEM")
			if item is not None:
				item_name = item.get("NAME")
				uom = item.findtext("BASEUNITS") or "Nos"

				if not frappe.db.exists("Item", item_name):
					# Create UOM if not exists
					if uom and not frappe.db.exists("UOM", uom):
						frappe.get_doc({"doctype": "UOM", "uom_name": uom}).insert(ignore_permissions=True)

					new_item = frappe.new_doc("Item")
					new_item.item_code = item_name
					new_item.item_name = item_name
					new_item.item_group = "All Item Groups"
					new_item.stock_uom = uom
					new_item.is_stock_item = 1
					new_item.insert(ignore_permissions=True)
					counts["items"] += 1

			# 3. Bank Reconciliation Dates (Phase 3)
			voucher = msg.find("VOUCHER")
			if voucher is not None and bank_recon_master == "Tally":
				vch_number = voucher.findtext("VOUCHERNUMBER")
				if vch_number:
					bank_allocs = voucher.findall(".//BANKALLOCATIONS.LIST")
					for alloc in bank_allocs:
						clearance_date_str = alloc.findtext("BANKERSDATE")
						if clearance_date_str and len(clearance_date_str) >= 8:
							try:
								c_date = f"{clearance_date_str[:4]}-{clearance_date_str[4:6]}-{clearance_date_str[6:8]}"

								# Try matching Payment Entry first
								if frappe.db.exists("Payment Entry", vch_number):
									doc = frappe.get_doc("Payment Entry", vch_number)
									if not doc.clearance_date or str(doc.clearance_date) != c_date:
										doc.db_set("clearance_date", c_date)
										counts["bank_allocations"] += 1
								# Try Journal Entry
								elif frappe.db.exists("Journal Entry", vch_number):
									doc = frappe.get_doc("Journal Entry", vch_number)
									if not doc.clearance_date or str(doc.clearance_date) != c_date:
										doc.db_set("clearance_date", c_date)
										counts["bank_allocations"] += 1
							except Exception as e:
								frappe.logger().error(f"Failed to reconcile voucher {vch_number}: {e!s}")

		frappe.db.commit()  # nosemgrep: frappe-manual-commit - an import is committed as it goes

		# A reporter owns a log of its own and writes the outcome to it. Writing
		# a second one here would put two rows in the history for one import,
		# which is worse than none - a count somebody has to reconcile.
		if not reporter:
			_write_import_log("Tally XML", "Success", counts)

		msg_parts = []
		if counts["customers"]:
			msg_parts.append(f"{counts['customers']} Customers")
		if counts["suppliers"]:
			msg_parts.append(f"{counts['suppliers']} Suppliers")
		if counts["items"]:
			msg_parts.append(f"{counts['items']} Items")
		if counts["bank_allocations"]:
			msg_parts.append(f"{counts['bank_allocations']} Bank Reconciliations")

		message = (
			("Created/Updated: " + ", ".join(msg_parts))
			if msg_parts
			else "Parsed XML, but no new records were created or updated."
		)
		return {"success": True, "message": message, "counts": counts}

	except Exception as e:
		frappe.log_error(title="Tally XML Import Error", message=traceback.format_exc())
		if not reporter:
			_write_import_log("Tally XML", "Failed", None, error=str(e))
		return {"success": False, "error": f"Error parsing XML: {e!s}"}


def _write_import_log(import_type, status, counts, error=None):
	"""Record the import.

	`Tally Import Log` shipped with the app, was granted to three roles, and was
	written by nothing - so every import left no trace at all, which is the one
	thing an accounts team needs when a figure turns out wrong.
	"""
	try:
		log = frappe.new_doc("Tally Import Log")
		log.import_type = import_type
		log.status = status
		log.imported_at = frappe.utils.now_datetime()
		if counts:
			log.records_imported = sum(counts.values())
		log.records_failed = 0 if status == "Success" else 1
		if error:
			log.error_log = str(error)[:4000]
		log.insert(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit - the log outlives a failed import
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Tally Connector: could not write import log")


# ─────────────────────────────────────────────────────────────────────────────
# The two doors onto the parser
# ─────────────────────────────────────────────────────────────────────────────


@frappe.whitelist(methods=["POST"])
def process_tally_xml(xml_data: str):
	"""Import a file the caller has in hand. Kept for a small file and for the
	Desk buttons; anything sizeable goes through `start_import` instead."""
	_require(ADMIN_ROLES)
	return _import_xml(xml_data)


def _read_file(file_url):
	"""The bytes behind an uploaded File, as text.

	Read through the `File` document rather than by joining a path onto the
	site directory, and only after the File's own permission check:
	`get_content` checks nothing, so without it any private file on the site
	could be read by naming its URL. A `file_url` that is not a plain string
	is refused, because a filter passed in its place would match some other
	file.
	"""
	if not isinstance(file_url, str) or not file_url.strip():
		frappe.throw(_("Choose a Tally XML file to import."))
	name = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if not name:
		frappe.throw(_("That file could not be found. Upload it again."))
	doc = frappe.get_doc("File", name)
	doc.check_permission("read")
	content = doc.get_content()
	if isinstance(content, bytes):
		content = content.decode("utf-8", errors="replace")
	return doc, content


@frappe.whitelist(methods=["GET", "POST"])
def validate_file(file_url: str):
	"""Does this file parse, and what is in it - before anything is written.

	Separate from importing on purpose. The old screen's only answer to a file
	Tally would not have written was a stack trace after it had already created
	half the customers in it.
	"""
	_require(ADMIN_ROLES)
	doc, content = _read_file(file_url)

	if len(content) > MAX_XML_BYTES:
		return {
			"success": False,
			"error": f"That file is larger than {MAX_XML_BYTES // (1024 * 1024)} MB. "
			"Split it in Tally and import the parts.",
		}
	try:
		cleaned = _clean(content)
		root = ET.fromstring(cleaned.encode("utf-8"))
	except Exception as e:
		return {
			"success": False,
			"file_name": doc.file_name,
			"error": f"This is not XML Tally would have written: {e}",
		}

	messages = root.findall(".//TALLYMESSAGE") or ([root] if root.tag == "TALLYMESSAGE" else [])
	kinds = {}
	for msg in messages:
		for child in msg:
			kinds[child.tag] = kinds.get(child.tag, 0) + 1

	return {
		"success": True,
		"file_name": doc.file_name,
		"file_url": file_url,
		"size": len(content),
		"objects": len(messages),
		"kinds": sorted(({"tag": k, "count": v} for k, v in kinds.items()), key=lambda r: -r["count"]),
		"report_name": root.findtext(".//REPORTNAME") or "",
		# An empty file parses perfectly well and imports nothing, which reads
		# as success unless somebody says so here.
		"empty": not messages,
	}


@frappe.whitelist(methods=["POST"])
def start_import(file_url: str, company: str | None = None):
	"""Hand an uploaded file to a worker and answer with a log to watch.

	Always in the background, whatever the size. An import writes to the ledger,
	so the one thing that must never happen is a browser giving up on a request
	that is still creating customers - which is what the synchronous path did on
	anything large.
	"""
	_require(ADMIN_ROLES)
	from tally_connector.api.export import _require_sync_enabled

	_require_sync_enabled()
	doc, _content = _read_file(file_url)

	log = frappe.new_doc("Tally Import Log")
	log.import_type = "Tally XML"
	log.status = "Pending"
	log.stage = "Queued"
	log.file_name = doc.file_name
	log.file_attachment = file_url
	log.company = company or frappe.defaults.get_user_default("Company")
	log.imported_at = frappe.utils.now_datetime()
	log.started_at = frappe.utils.now_datetime()
	log.insert(ignore_permissions=True)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit - the queued job reads this row

	job = frappe.enqueue(
		"tally_connector.api.import_data.run_import",
		queue="long",
		timeout=3600,
		log_name=log.name,
		file_url=file_url,
	)
	frappe.db.set_value(
		"Tally Import Log", log.name, {"job_id": getattr(job, "id", None)}, update_modified=False
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit - the queued job reads this row
	return {"success": True, "log": log.name, "kind": "import", "queued": True}


def run_import(log_name, file_url):
	"""The worker. Reports as it goes and writes the outcome to its own log."""
	from tally_connector.api.jobs import IMPORT_STAGES, JobReporter

	reporter = JobReporter("Tally Import Log", log_name, IMPORT_STAGES)
	try:
		reporter.start()
		reporter.at("Reading File")
		_doc, content = _read_file(file_url)

		reporter.at("Validating")
		if len(content) > MAX_XML_BYTES:
			raise ValueError(
				f"That file is larger than {MAX_XML_BYTES // (1024 * 1024)} MB. "
				"Split it in Tally and import the parts."
			)

		result = _import_xml(content, reporter=reporter)
		if not result.get("success"):
			reporter.finish("Failed", error=result.get("error"))
			return result

		counts = result.get("counts") or {}
		reporter.at("Final Validation")
		reporter.finish("Success", records=sum(counts.values()))
		return result
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Tally Connector: import")
		reporter.finish("Failed", error=e)
		return {"success": False, "error": str(e)}
