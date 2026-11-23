"""One row per ERPNext document per Tally company: what was sent, what happened.

Nothing recorded this before. `Tally Export Log` is job-level - it has no field
naming a source document - so the connector could not answer the question every
duplicate depends on: has this invoice already been sent, and what did Tally say?

Deleting one would erase the only record that a document reached a customer's
books, so nobody may.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class TallySyncRecord(Document):
	def on_trash(self):
		frappe.throw(
			_(
				"A sync record is the only evidence that this document reached "
				"Tally. It is not deleted; re-sync the document instead."
			)
		)


# The natural key of the ledger: one document, one destination. Held by the
# database rather than by a read before the insert, because two pushes running
# at once (the hourly sync and a person) both read "no record" and both insert.
# `source_name` leads so the bulk read by document name can use it too.
NATURAL_KEY = ("source_name", "source_doctype", "erpnext_company", "tally_company")
UNIQUE_KEY = "unique_source_destination"


def on_doctype_update():
	frappe.db.add_unique("Tally Sync Record", list(NATURAL_KEY), constraint_name=UNIQUE_KEY)
