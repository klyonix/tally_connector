"""
What the Overview and the Export page read.

Nothing here writes anything or talks to Tally unless it is asked to. The
connection state in particular is reported from what is **known** rather than
probed on every page load: opening a dashboard should not hang for the length of
a TCP timeout because a machine in an accounts office is switched off.
"""

import frappe
from frappe.utils import cint, now_datetime, time_diff_in_seconds

from tally_connector.api.export import EXPORT_ROLES, _require, _resolve_company

# The ten areas the export is divided into, in the order they must reach Tally.
# Masters first is not a preference: a voucher naming a ledger Tally has not yet
# seen is refused, so the order this list is written in is the order somebody
# has to work through.
AREAS = (
    ("uoms", "Units", "masters", "Unit symbols every stock item refers to"),
    ("stock_items", "Stock Items", "masters", "Items with HSN and GST details"),
    ("chart_of_accounts", "Chart of Accounts", "masters", "Groups and ledgers"),
    ("parties", "Parties", "masters", "Customers, suppliers and employees"),
    ("sales_invoices", "Sales", "transactions", "Sales and credit note vouchers"),
    ("purchase_invoices", "Purchases", "transactions", "Purchase and debit note vouchers"),
    ("payment_in", "Receipts", "transactions", "Money in"),
    ("payment_out", "Payments", "transactions", "Money out"),
    ("journal_entries", "Journals", "transactions", "Journal vouchers"),
    ("bank_transactions", "Bank Transactions", "transactions", "Statement lines"),
)

# What each area is called in `Tally Export Log.export_type`, so "when was this
# last exported" can be answered per area rather than only for the whole run.
LOG_TYPE = {
    "uoms": "UOMs",
    "stock_items": "Stock Items",
    "chart_of_accounts": "Chart of Accounts",
    "parties": "Parties",
    "sales_invoices": "Sales Invoice",
    "purchase_invoices": "Purchase Invoice",
    "payment_in": "Payment Entry",
    "payment_out": "Payment Entry",
    "journal_entries": "Journal Entry",
    "bank_transactions": "Bank Transaction",
}


def _master_counts(company):
    """How much master data exists on the ERPNext side.

    Counted from ERPNext rather than by running the extract: this is drawn on a
    page somebody opens, and building the whole model to put a number in a table
    cell would make the page cost what the export costs.
    """
    def count(doctype, filters=None):
        try:
            return frappe.db.count(doctype, filters or {})
        except Exception:
            return 0

    return {
        "uoms": count("UOM"),
        "stock_items": count("Item", {"disabled": 0}),
        "chart_of_accounts": count("Account", {"company": company}),
        "parties": count("Customer") + count("Supplier"),
    }


def _last_exports():
    """The most recent finished export per area, keyed the way AREAS is."""
    out = {}
    rows = frappe.get_all(
        "Tally Export Log",
        filters={"status": ["in", ["Success", "Partial"]]},
        fields=["name", "export_type", "status", "records_exported", "finished_at",
                "exported_at", "creation"],
        order_by="creation desc", limit=200,
    )
    for row in rows:
        for key, log_type in LOG_TYPE.items():
            if row.export_type in (log_type, "Full Export") and key not in out:
                out[key] = {
                    "log": row.name,
                    "at": str(row.finished_at or row.exported_at or row.creation),
                    "status": row.status,
                    "records": cint(row.records_exported),
                    "whole_run": row.export_type == "Full Export",
                }
    return out


@frappe.whitelist()
def areas(company=None, from_date=None, to_date=None):
    """One row per area for the Export page's two tables.

    Replaces ten cards with two tables on purpose: a card can hold a title and
    two buttons, and the questions somebody actually has here - how much is
    there, when did it last go, did it work - are columns.
    """
    _require(EXPORT_ROLES)
    from tally_connector.tally.extract import count_vouchers

    company = _resolve_company(company)
    settings = frappe.get_single("Tally Settings")
    per_source, total = count_vouchers(company, from_date, to_date, settings)
    masters = _master_counts(company)
    last = _last_exports()

    # `payment_in` and `payment_out` are one ERPNext read split two ways, so the
    # count is reported against the pair rather than invented twice.
    payments = per_source.get("payment_entries", 0)

    rows = []
    for key, label, group, description in AREAS:
        if group == "masters":
            count, dated = masters.get(key, 0), False
        elif key in ("payment_in", "payment_out"):
            count, dated = payments, True
        else:
            count, dated = per_source.get(key, 0), True
        rows.append({
            "key": key, "label": label, "group": group, "description": description,
            "count": count,
            # Whether the date range applies. A unit does not have a posting
            # date, and a table that showed a range beside one would be lying
            # about what the number means.
            "dated": dated,
            "included": bool(settings.get(_INCLUDE_FLAG.get(key), 1)) if key in _INCLUDE_FLAG else True,
            "last": last.get(key),
        })
    return {"company": company, "rows": rows, "total_vouchers": total}


# Which settings switch decides whether an area is part of the complete export.
# Shown in the table so a zero that is a setting reads differently from a zero
# that is an empty ledger.
_INCLUDE_FLAG = {
    "uoms": "include_uoms",
    "stock_items": "include_stock_items",
    "chart_of_accounts": "include_chart_of_accounts",
    "parties": "include_parties",
    "sales_invoices": "include_sales_invoice",
    "purchase_invoices": "include_purchase_invoice",
    "payment_in": "include_payment_entry",
    "payment_out": "include_payment_entry",
    "journal_entries": "include_journal_entry",
    "bank_transactions": "include_bank_transaction",
}


@frappe.whitelist()
def overview(company=None):
    """The Overview page in one request.

    One call rather than six because this is the page somebody lands on, and six
    round trips is six chances to render a screen half full of spinners.
    """
    _require(EXPORT_ROLES)
    company = _resolve_company(company)
    settings = frappe.get_single("Tally Settings")

    recent = frappe.get_all(
        "Tally Export Log",
        fields=["name", "export_type", "company", "status", "records_exported",
                "records_failed", "warnings", "started_at", "finished_at", "creation"],
        order_by="creation desc", limit=100,
    )
    imports = frappe.get_all(
        "Tally Import Log",
        fields=["name", "import_type", "company", "status", "records_imported",
                "records_failed", "started_at", "finished_at", "creation"],
        order_by="creation desc", limit=50,
    )

    summary = {"success": 0, "failed": 0, "partial": 0, "running": 0}
    for row in recent + imports:
        if row.status in ("Pending", "Running"):
            summary["running"] += 1
        elif row.status == "Success":
            summary["success"] += 1
        elif row.status == "Partial":
            summary["partial"] += 1
        elif row.status == "Failed":
            summary["failed"] += 1

    last_ok = next((r for r in recent if r.status in ("Success", "Partial")), None)
    duration = None
    if last_ok and last_ok.started_at and last_ok.finished_at:
        duration = time_diff_in_seconds(last_ok.finished_at, last_ok.started_at)

    return {
        "company": company,
        "tally_company": (settings.company_name_in_tally or "").strip(),
        "host": f"{settings.tally_host or 'localhost'}:{settings.tally_port or 9000}",
        # Reported, never probed. Asking Tally is a button, because a dashboard
        # that opens a socket to an office PC on every page load is a dashboard
        # that hangs whenever that PC is off.
        "last_sync_at": str(settings.last_sync_at) if settings.last_sync_at else None,
        "sync_enabled": bool(settings.get("sync_enabled")),
        "auto_sync": bool(settings.enable_auto_sync),
        "auto_sync_interval": settings.auto_sync_interval or "",
        "summary": summary,
        "last_export": {
            "name": last_ok.name, "type": last_ok.export_type,
            "company": last_ok.company, "status": last_ok.status,
            "records": cint(last_ok.records_exported),
            "failed": cint(last_ok.records_failed),
            "at": str(last_ok.finished_at or last_ok.creation),
            "duration": duration,
        } if last_ok else None,
        "recent": [{
            "name": r.name, "kind": "export", "title": r.export_type,
            "company": r.company, "status": r.status,
            "records": cint(r.records_exported), "failed": cint(r.records_failed),
            "at": str(r.finished_at or r.creation),
        } for r in recent[:6]],
    }


@frappe.whitelist()
def companies_with_defaults():
    """What the shell needs before it can draw anything: the companies, which one
    to open on, and who is signed in.

    The user comes from here rather than from `window.frappe.boot`, which this
    application does not have: the generated host page writes each key of the
    template's `boot` dict onto `window`, and nothing sets one - so the avatar
    fell back to a letter of the word "Account" on every session.
    """
    _require(EXPORT_ROLES)
    rows = frappe.get_list("Company", fields=["name", "abbr"], order_by="name")
    default = frappe.defaults.get_user_default("Company")
    if default not in [r.name for r in rows]:
        default = rows[0].name if rows else None
    return {
        "companies": rows,
        "default": default,
        "sync_enabled": bool(
            frappe.db.get_single_value("Tally Settings", "sync_enabled")
        ),
        "user": {
            "name": frappe.session.user,
            "full_name": frappe.utils.get_fullname(frappe.session.user),
        },
    }
