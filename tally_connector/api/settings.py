"""
Reading and writing `Tally Settings` from the dashboard.

The screen used to be a link into the Desk, which is a different application
with a different shell - so configuring the connector meant leaving it. This is
the same settings document, exposed field by field rather than as a blob: an
endpoint that took whatever the client sent would be a way to write any field on
a Single, including ones no screen draws.
"""

import frappe
from frappe import _

from tally_connector.api.export import ADMIN_ROLES, EXPORT_ROLES, _require

# Every field the settings screen may write, grouped the way the screen groups
# them. The tuple is the fence: `save` refuses anything outside it, so adding a
# control to the UI is a deliberate edit here rather than a side effect.
GROUPS = {
    "connection": ("sync_enabled", "tally_host", "tally_port", "company_name_in_tally"),
    "export": ("default_export_format", "export_from_date",
               "import_duplicate_behaviour", "voucher_import_action"),
    "include": ("include_sales_invoice", "include_purchase_invoice",
                "include_payment_entry", "include_journal_entry",
                "include_bank_transaction", "include_chart_of_accounts",
                "include_parties", "include_stock_items", "include_stock_groups",
                "include_godowns", "include_uoms"),
    "vouchers": ("sales_voucher_type", "purchase_voucher_type", "receipt_voucher_type",
                 "payment_voucher_type", "journal_voucher_type", "contra_voucher_type",
                 "credit_note_voucher_type", "debit_note_voucher_type"),
    "ledgers": ("sales_ledger", "purchase_ledger", "tds_deductible_ledger",
                "sundry_debtors_ledger", "sundry_creditors_ledger",
                "employee_ledger_group", "suspense_ledger",
                "gst_output_ledger", "gst_input_ledger"),
    "sync": ("enable_auto_sync", "auto_sync_interval", "bank_recon_master"),
}

WRITABLE = tuple(f for fields in GROUPS.values() for f in fields)


@frappe.whitelist(methods=["GET", "POST"])
def get():
    """The settings as the screen draws them, plus the options each Select has.

    The options travel with the values so the screen never hard-codes a list
    that the DocType could change underneath it - which is how a dropdown comes
    to offer a value the server refuses.
    """
    _require(EXPORT_ROLES)
    doc = frappe.get_single("Tally Settings")
    meta = frappe.get_meta("Tally Settings")

    values, fields = {}, {}
    for name in WRITABLE:
        field = meta.get_field(name)
        if not field:
            continue
        values[name] = doc.get(name)
        fields[name] = {
            "label": field.label or name.replace("_", " ").title(),
            "type": field.fieldtype,
            "options": [o for o in (field.options or "").split("\n") if o]
            if field.fieldtype == "Select" else None,
            "description": field.description or "",
        }
    return {
        "values": values,
        "fields": fields,
        "groups": {k: list(v) for k, v in GROUPS.items()},
        "can_write": bool(set(ADMIN_ROLES) & set(frappe.get_roles()))
        or frappe.session.user == "Administrator",
        "last_sync_at": str(doc.last_sync_at) if doc.last_sync_at else None,
    }


@frappe.whitelist(methods=["POST"])
def save(values: dict | str):
    """Write the settings somebody changed, and only those.

    Fenced to the narrower role tuple: running an export is clerical, and
    changing which ledger the books land in is not. Same reasoning as the import
    endpoint, which has carried that distinction since it was fenced.
    """
    _require(ADMIN_ROLES)
    if isinstance(values, str):
        values = frappe.parse_json(values)

    unknown = sorted(set(values) - set(WRITABLE))
    if unknown:
        frappe.throw(
            _("These are not settings this screen can change: {0}").format(", ".join(unknown))
        )

    doc = frappe.get_single("Tally Settings")
    for name, value in values.items():
        doc.set(name, value)
    doc.save(ignore_permissions=True)
    return get()


@frappe.whitelist(methods=["POST"])
def set_sync_enabled(enabled: bool | str):
    """The master switch, from the top bar.

    Its own endpoint rather than a `save` of one field, because this is fenced
    to the export roles and not the admin ones: stopping a sync in a hurry is
    something the person watching it go wrong must be able to do. Changing where
    the books land stays narrower.
    """
    _require(EXPORT_ROLES)
    value = 1 if str(enabled).lower() in ("1", "true", "yes") else 0
    frappe.db.set_single_value("Tally Settings", "sync_enabled", value)
    frappe.clear_document_cache("Tally Settings", "Tally Settings")
    return {"sync_enabled": bool(value)}
