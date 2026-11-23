"""Merge sync records that name the same document and destination.

Runs before the model sync adds the unique key on that pair, which would fail
on a site still holding duplicates. The record kept is the one that says the
most about Tally: one saying Tally holds the voucher, then one waiting on a
person (Pending, Unknown Result), then the newest. Attempts are added together.
"""

import frappe

IN_TALLY = ("Created", "Altered", "Existing", "Stale")
NEEDS_A_PERSON = ("Pending", "Unknown Result")
KEY = ("source_name", "source_doctype", "erpnext_company", "tally_company")


def keep_of(rows):
	"""The record to keep out of records naming one document and destination."""
	return max(rows, key=lambda r: (_rank(r), r.modified))


def _rank(row):
	if row.outcome in IN_TALLY:
		return 2
	if row.outcome in NEEDS_A_PERSON:
		return 1
	return 0


def execute():
	if not frappe.db.table_exists("Tally Sync Record"):
		return

	record = frappe.qb.DocType("Tally Sync Record")
	count = frappe.query_builder.functions.Count(record.name)
	groups = (
		frappe.qb.from_(record)
		.select(*[record[f] for f in KEY])
		.groupby(*[record[f] for f in KEY])
		.having(count > 1)
		.run(as_dict=True)
	)
	for group in groups:
		rows = frappe.get_all(
			"Tally Sync Record",
			filters={f: group[f] for f in KEY},
			fields=["name", "outcome", "attempts", "modified"],
			order_by="modified desc",
		)
		keep = keep_of(rows)
		attempts = sum(r.attempts or 0 for r in rows)
		frappe.db.set_value("Tally Sync Record", keep.name, "attempts", attempts, update_modified=False)
		for row in rows:
			if row.name != keep.name:
				frappe.db.delete("Tally Sync Record", {"name": row.name})
