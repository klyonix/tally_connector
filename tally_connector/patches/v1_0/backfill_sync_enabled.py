"""
Switch syncing on for a site that already had `Tally Settings`.

A `Check` field declared with `default: 1` is written when the Single is first
created and never afterwards, so a site upgrading into this field reads it as 0.
That would leave every existing installation with syncing silently off and an
export refusing for a reason nobody could see - the switch would be doing its
job, against a value nobody chose.

Separate from `backfill_new_settings_defaults` because that patch has already
run everywhere; a patch does not run twice, so a field added to its list after
the fact is never applied.
"""

import frappe


def execute():
    if not frappe.db.exists("DocType", "Tally Settings"):
        return

    singles = frappe.qb.DocType("Singles")
    stored = (
        frappe.qb.from_(singles)
        .select(singles.value)
        .where((singles.doctype == "Tally Settings") & (singles.field == "sync_enabled"))
    ).run()
    # Only where the site has no opinion. Somebody who has deliberately switched
    # syncing off must not have it switched back on by an upgrade.
    if stored:
        return

    frappe.db.set_single_value("Tally Settings", "sync_enabled", 1)
