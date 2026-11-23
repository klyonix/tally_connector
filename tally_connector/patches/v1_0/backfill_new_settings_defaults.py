"""
Give a field added to `Tally Settings` its declared default on a site that
already has the Single.

Frappe writes a Single's defaults when the document is first created and does
not backfill them when a field is added later - the row simply has no entry for
it, and reading it answers None. For a Check that means the new switch reads as
**off**.

That is not theoretical. `include_stock_groups` was read by the combined export
long before it existed as a field:

    settings.get("include_stock_groups", 1)

`Document.get` treats a positional second argument as the default, so with no
such field the call answered 1 and stock groups were exported. Adding the field
- so the switch could actually be seen and used - made the same call answer the
stored value, which was nothing, and stock groups silently stopped being
exported. Seven groups became zero on the site this was found on, and nothing
failed: an export that omits a section looks exactly like an export of a site
that has none.

So the backfill is narrow and idempotent: a field named here that has no stored
value gets the default the DocType declares, and a field somebody has
deliberately switched off is left alone.
"""

import frappe

# Fields added after the DocType shipped. A field is listed here once, when it
# is added; it stays listed so a site that has not migrated yet still gets it.
NEW_FIELDS = (
    "include_stock_groups",
    "employee_ledger_group",
    "suspense_ledger",
    "contra_voucher_type",
    "credit_note_voucher_type",
    "debit_note_voucher_type",
    "include_godowns",
    "import_duplicate_behaviour",
    "voucher_import_action",
    # The master switch. Its declared default is 1, and a Single that already
    # existed reads it as 0 - which would leave every site that upgrades with
    # syncing silently switched off, and no clue why an export refuses.
    "sync_enabled",
    # Whether a voucher carries its items. Declared default 1, and measured on
    # a site that already had the Single: it read 0 before anything was set,
    # so without this every upgrading site would silently start folding its
    # invoices to accounting entries and Tally's stock summary would go empty
    # with nothing to say why.
    "send_voucher_inventory",
)


def execute():
    if not frappe.db.exists("DocType", "Tally Settings"):
        return

    meta = frappe.get_meta("Tally Settings")
    stored = dict(
        frappe.db.sql(
            "select field, value from tabSingles where doctype = 'Tally Settings'"
        )
    )

    # No row at all means the Single has never been opened, and Frappe will
    # write every default the first time it is. Nothing to repair.
    if not stored:
        return

    for fieldname in NEW_FIELDS:
        field = meta.get_field(fieldname)
        if not field or field.default is None:
            continue
        if stored.get(fieldname) not in (None, ""):
            continue
        frappe.db.set_value(
            "Tally Settings", "Tally Settings", fieldname, field.default,
            update_modified=False,
        )

    frappe.db.commit()
    frappe.clear_cache(doctype="Tally Settings")
