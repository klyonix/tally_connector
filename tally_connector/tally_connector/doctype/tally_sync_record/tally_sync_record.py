"""One row per ERPNext document per Tally company: what was sent, what happened.

Nothing recorded this before. `Tally Export Log` is job-level - it has no field
naming a source document - so the connector could not answer the question every
duplicate depends on: has this invoice already been sent, and what did Tally say?

Deleting one would erase the only record that a document reached a customer's
books, so nobody may.
"""

import frappe
from frappe.model.document import Document


class TallySyncRecord(Document):
    def on_trash(self):
        frappe.throw(
            "A sync record is the only evidence that this document reached "
            "Tally. It is not deleted; re-sync the document instead.")
