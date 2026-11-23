"""Getting a fresh test site ready, the way ERPNext's own apps do.

A site made for CI has ERPNext installed and its setup wizard never run, so it
has no company, chart of accounts, fiscal year or units of measure, and nearly
every test here reads one of those. This runs the wizard once with a test
company and gives it a small year of demo books, so the tests that refuse to
pass on an empty export have something to export. A site that already has a
company is left as it is.
"""

import frappe
from frappe.utils import add_days, getdate, now_datetime, today

COMPANY = "_Test Tally Company"
CUSTOMERS = ("Acme Industries", "A & B Enterprises", "Brightline Retail")
SUPPLIERS = ("Northgate Supplies", "Harbour Logistics")


def before_tests():
	frappe.clear_cache()
	if frappe.get_all("Company", limit=1):
		return

	from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

	fy_start = _financial_year_start()
	setup_complete(
		{
			"currency": "INR",
			"full_name": "Test User",
			"company_name": COMPANY,
			"timezone": "Asia/Kolkata",
			"company_abbr": "_TTC",
			"industry": "Services",
			"country": "India",
			"fy_start_date": str(fy_start),
			"fy_end_date": str(add_days(fy_start.replace(year=fy_start.year + 1), -1)),
			"language": "english",
			"company_tagline": "Testing",
			"email": "test@example.com",
			"password": "test",
			"chart_of_accounts": "Standard",
		}
	)
	_demo_books(COMPANY, fy_start)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit - the test run starts from this setup


def _financial_year_start():
	"""The 1 April that opens the current Indian financial year."""
	now = now_datetime().date()
	return getdate(f"{now.year if now.month >= 4 else now.year - 1}-04-01")


def _demo_books(company, fy_start):
	"""A small, varied year: sales with a few credit notes, purchases, receipts,
	payments and journals, spread from the start of the year to today."""
	_parties()
	_item("Widget", stock=True)
	_item("Support Service", stock=False)
	_item("Office Supplies", stock=False, sales=False)

	days = max(1, min((getdate(today()) - fy_start).days, 210))

	def day(i, of):
		return add_days(fy_start, i * days // of)

	sales = []
	for i in range(70):
		sales.append(
			_sales_invoice(
				company,
				CUSTOMERS[i % len(CUSTOMERS)],
				day(i, 70),
				[("Widget", 1 + i % 4, 250 + 10 * (i % 7)), ("Support Service", 1, 1200)][: 1 + i % 2],
			)
		)
	for original in sales[:3]:
		_credit_note(original)
	for i in range(30):
		_purchase_invoice(company, SUPPLIERS[i % len(SUPPLIERS)], day(i, 30), 400 + 25 * (i % 5), i)
	for invoice in sales[:20]:
		_payment("Sales Invoice", invoice)
	purchases = frappe.get_all(
		"Purchase Invoice", filters={"company": company, "docstatus": 1}, pluck="name", limit=10
	)
	for invoice in purchases:
		_payment("Purchase Invoice", invoice)
	for i in range(10):
		_journal(company, day(i, 10), 500 + 50 * i)


def _leaf(doctype):
	return frappe.db.get_value(doctype, {"is_group": 0}, "name") or frappe.db.get_value(doctype, {}, "name")


def _parties():
	for name in CUSTOMERS:
		if not frappe.db.exists("Customer", name):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": name,
					"customer_group": _leaf("Customer Group"),
					"territory": _leaf("Territory"),
				}
			).insert(ignore_permissions=True)
	for name in SUPPLIERS:
		if not frappe.db.exists("Supplier", name):
			frappe.get_doc(
				{"doctype": "Supplier", "supplier_name": name, "supplier_group": _leaf("Supplier Group")}
			).insert(ignore_permissions=True)


def _item(code, stock, sales=True):
	if frappe.db.exists("Item", code):
		return
	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": _leaf("Item Group"),
			"stock_uom": "Nos",
			"is_stock_item": int(stock),
			"is_sales_item": int(sales),
			"is_purchase_item": 1,
		}
	)
	if frappe.get_meta("Item").has_field("gst_hsn_code"):
		item.gst_hsn_code = "84713010" if stock else "998313"
	item.insert(ignore_permissions=True)


def _sales_invoice(company, customer, posting_date, lines):
	doc = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"company": company,
			"customer": customer,
			"set_posting_time": 1,
			"posting_date": posting_date,
			"due_date": add_days(posting_date, 30),
			"items": [{"item_code": code, "qty": qty, "rate": rate} for code, qty, rate in lines],
		}
	)
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc.name


def _credit_note(original):
	from erpnext.controllers.sales_and_purchase_return import make_return_doc

	doc = make_return_doc("Sales Invoice", original)
	doc.insert(ignore_permissions=True)
	doc.submit()


def _purchase_invoice(company, supplier, posting_date, rate, i):
	doc = frappe.get_doc(
		{
			"doctype": "Purchase Invoice",
			"company": company,
			"supplier": supplier,
			"bill_no": f"BILL-{i + 1:04d}",
			"bill_date": posting_date,
			"set_posting_time": 1,
			"posting_date": posting_date,
			"due_date": add_days(posting_date, 30),
			"items": [{"item_code": "Office Supplies", "qty": 1 + i % 3, "rate": rate}],
		}
	)
	doc.insert(ignore_permissions=True)
	doc.submit()


def _payment(doctype, invoice):
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	posting_date = frappe.db.get_value(doctype, invoice, "posting_date")
	doc = get_payment_entry(doctype, invoice)
	doc.posting_date = add_days(posting_date, 5)
	doc.reference_no = f"REF-{invoice}"
	doc.reference_date = doc.posting_date
	doc.insert(ignore_permissions=True)
	doc.submit()


def _journal(company, posting_date, amount):
	expense = frappe.db.get_value(
		"Account", {"company": company, "root_type": "Expense", "is_group": 0}, "name"
	)
	cash = frappe.get_cached_value("Company", company, "default_cash_account")
	cost_centre = frappe.get_cached_value("Company", company, "cost_center")
	doc = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"company": company,
			"posting_date": posting_date,
			"accounts": [
				{"account": expense, "debit_in_account_currency": amount, "cost_center": cost_centre},
				{"account": cash, "credit_in_account_currency": amount},
			],
		}
	)
	doc.insert(ignore_permissions=True)
	doc.submit()
