"""
tally_connector/api/sync.py
Scheduled tasks for automatic Tally sync.
"""

import frappe
from frappe.utils import now_datetime, today


def auto_sync_to_tally():
    """Hourly scheduler - only runs if auto sync is enabled."""
    settings = frappe.get_single("Tally Settings")
    if not settings.enable_auto_sync:
        return

    if settings.auto_sync_interval == "Hourly":
        _run_sync()
    elif settings.auto_sync_interval == "Twice a Day":
        if settings.last_sync_at:
            from frappe.utils import time_diff_in_hours

            if time_diff_in_hours(now_datetime(), settings.last_sync_at) < 11:
                return
        _run_sync()


def daily_full_sync():
    """Daily full sync."""
    settings = frappe.get_single("Tally Settings")
    if settings.enable_auto_sync and settings.auto_sync_interval == "Daily":
        _run_sync()


def _companies_to_sync(settings):
    """Which ERPNext companies this scheduled run may push.

    One, or none. It used to be every `Company` on the site, while the Tally
    company name comes from a single settings field - so a site with three
    companies pushed all three sets of books into one company's Tally, and the
    second and third arrived as somebody else's transactions. The docstring
    below this one already claimed that had been fixed; it had not.

    An unmapped site pushes nothing and says so, rather than guessing.
    """
    named = (settings.get("erpnext_company") or "").strip()
    if named and frappe.db.exists("Company", named):
        return [named]
    # A site with exactly one company has no ambiguity to resolve, so it is
    # allowed to go unconfigured. Anything else must be said out loud.
    only = frappe.get_all("Company", pluck="name", limit=2)
    return only if len(only) == 1 else []


def _run_sync():
    """Push today's data to Tally, for the one company that is mapped to it.

    Two changes from the original. It runs for the mapped company rather than
    every company on the site, because the Tally company name is a single
    setting and pushing several companies into it put one customer's books into
    another's Tally; and a successful run is no longer written to the **error**
    log - `frappe.log_error` was being used to record success, so the error log
    filled with entries that were not errors and a real failure was buried among
    them. `Tally Export Log` already records every run, which is where somebody
    looks.
    """
    from tally_connector.api.export import export_all

    settings = frappe.get_single("Tally Settings")
    from_date = to_date = str(today())
    companies = _companies_to_sync(settings)
    if not companies:
        frappe.log_error(
            "Auto sync did nothing: this site has more than one company and "
            "none is mapped to the Tally company. Set 'ERPNext Company' on "
            "Tally Settings.",
            "Tally Connector: Auto Sync")
        return

    try:
        for company in companies:
            result = export_all(
                from_date=from_date,
                to_date=to_date,
                export_format="Tally XML",
                push_to_tally_flag=True,
                company=company,
            )
            if not result.get("success"):
                frappe.log_error(
                    f"Auto sync failed for {company}: {result.get('error')}",
                    "Tally Connector: Auto Sync",
                )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Tally Connector: Auto Sync Failed")
    finally:
        settings.last_sync_at = now_datetime()
        settings.save(ignore_permissions=True)
        frappe.db.commit()


@frappe.whitelist()
def has_app_permission():
    """Whether the Tally Connector tile shows on the apps screen."""
    return frappe.has_permission("Tally Settings", "read")
