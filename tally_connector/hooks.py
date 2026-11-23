app_name = "tally_connector"
app_title = "Tally Connector"
app_publisher = "KlyONIX Tech"
app_description = "ERPNext to Tally Prime XML Export & Sync"
app_email = "rk@klyonix.com"
app_license = "AGPLv3"
app_version = "1.1.0"

required_apps = ["erpnext"]

before_tests = "tally_connector.tests.site.before_tests"

# ──────────────────────────────────────────────
# Desk Home Screen
# ──────────────────────────────────────────────
add_to_apps_screen = [
	{
		"name": "tally_connector",
		# No `logo` key: the tile falls back to a default. The original pointed
		# at /assets/.../images/tally-logo.png, which has never existed in this
		# tree - a dead asset path is worse than no icon. Add one under
		# tally_connector/public/images/ and put the key back.
		"title": "Tally Connector",
		"route": "/tally-connector",
		"has_permission": "tally_connector.api.sync.has_app_permission",
	}
]

# ──────────────────────────────────────────────
# Scheduled tasks
# ──────────────────────────────────────────────
scheduler_events = {
	"hourly": ["tally_connector.api.sync.auto_sync_to_tally"],
	"daily": ["tally_connector.api.sync.daily_full_sync"],
}

# ──────────────────────────────────────────────
# Whitelisted API methods
# ──────────────────────────────────────────────
# (all frappe.whitelist decorated functions are auto-registered)

# ──────────────────────────────────────────────
# DocType JS overrides
# ──────────────────────────────────────────────
doctype_js = {
	"Sales Invoice": "tally_connector/doctype/overrides/sales_invoice_tally.js",
	"Purchase Invoice": "tally_connector/doctype/overrides/purchase_invoice_tally.js",
	"Payment Entry": "tally_connector/doctype/overrides/payment_entry_tally.js",
	"Journal Entry": "tally_connector/doctype/overrides/journal_entry_tally.js",
}

# NOTE: app_include_js / app_include_css are intentionally omitted.
# Those keys require a compiled bundle under assets/tally_connector/
# which needs an esbuild entry point. Since this app has no custom
# bundle, removing them prevents the ERR_INVALID_ARG_TYPE build error.

website_route_rules = [
	{"from_route": "/tally-connector/<path:app_path>", "to_route": "tally_connector"},
	{"from_route": "/tally-connector", "to_route": "tally_connector"},
]
