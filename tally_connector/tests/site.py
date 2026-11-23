"""Getting a fresh test site ready, the way ERPNext's own apps do.

A site made for CI has ERPNext installed and its setup wizard never run, so it
has no company, chart of accounts, fiscal year or units of measure, and nearly
every test here reads one of those. This runs the wizard once with a test
company. A site that already has a company is left as it is.
"""

import frappe
from frappe.utils import now_datetime


def before_tests():
	frappe.clear_cache()
	if frappe.get_all("Company", limit=1):
		return

	from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

	year = now_datetime().year
	setup_complete(
		{
			"currency": "INR",
			"full_name": "Test User",
			"company_name": "_Test Tally Company",
			"timezone": "Asia/Kolkata",
			"company_abbr": "_TTC",
			"industry": "Services",
			"country": "India",
			"fy_start_date": f"{year}-04-01",
			"fy_end_date": f"{year + 1}-03-31",
			"language": "english",
			"company_tagline": "Testing",
			"email": "test@example.com",
			"password": "test",
			"chart_of_accounts": "Standard",
		}
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit - the test run starts from this setup
