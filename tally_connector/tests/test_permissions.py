"""
Who may call these endpoints.

Every whitelisted method in this app carried no role check at all, and every
generator reads through `frappe.get_all`, which ignores permissions by design.
So a customer-portal Website User - an account for which
`frappe.has_permission("Sales Invoice", "read")` correctly answers False - could
call `export_all` and receive the chart of accounts, every invoice with amounts
and every party's address and GSTIN. The import endpoint was the same in the
other direction: it creates Customers, Suppliers and Items and writes clearance
dates onto submitted documents, all under `ignore_permissions=True`.

The sweep is over the module rather than a list, so an endpoint added later
without a fence fails this rather than shipping quietly.
"""

import inspect

import frappe
from frappe.tests import IntegrationTestCase

from tally_connector.api import export as export_api
from tally_connector.api import import_data as import_api

# `has_app_permission` decides whether a tile is drawn and reads nothing.
UNFENCED_BY_DESIGN = {"has_app_permission"}


def _whitelisted(module):
	"""The whitelisted callables this module exposes.

	`frappe.whitelist` adds the function to the global `frappe.whitelisted`
	**set**; it sets no attribute on the function. The first version of this
	swept for `getattr(fn, "whitelisted", False)`, matched nothing at all, and
	passed happily with an endpoint's role check deleted - a sweep that finds
	nothing asserts nothing. The count is asserted below before anything else
	is, for that reason.
	"""
	return {name: fn for name, fn in vars(module).items() if callable(fn) and fn in frappe.whitelisted}


class TestEndpointsAreFenced(IntegrationTestCase):
	def setUp(self):
		self.website_user = self._a_website_user()

	def _a_website_user(self):
		existing = frappe.get_all(
			"User", filters={"user_type": "Website User", "enabled": 1}, limit=1, pluck="name"
		)
		if existing:
			return existing[0]
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": "tally-connector-fence@example.com",
				"first_name": "Fence",
				"user_type": "Website User",
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
		return user.name

	def test_the_sweep_finds_the_endpoints_at_all(self):
		"""Asserted before what is asserted about them."""
		found = _whitelisted(export_api)
		self.assertGreaterEqual(
			len(found),
			12,
			f"The whitelist sweep found {len(found)} endpoints in api.export; it should "
			"find every export method. A sweep that finds nothing proves nothing.",
		)
		self.assertIn("export_all", found)
		self.assertIn("export_sales_invoices", found)

	def test_every_export_endpoint_names_the_roles_it_needs(self):
		"""Read the source rather than call each one: a fence that is not there
		is the failure, and calling every endpoint as a stranger would create
		export logs on a live site."""
		for name, fn in _whitelisted(export_api).items():
			if name in UNFENCED_BY_DESIGN:
				continue
			with self.subTest(endpoint=name):
				self.assertIn(
					"_require(",
					inspect.getsource(fn),
					f"tally_connector.api.export.{name} is whitelisted with no role check.",
				)

	def test_the_import_endpoint_is_fenced_more_narrowly_than_the_export(self):
		self.assertIn("_require(ADMIN_ROLES)", inspect.getsource(import_api.process_tally_xml))
		self.assertNotIn("Accounts User", export_api.ADMIN_ROLES)

	def test_a_website_user_is_refused_by_the_role_fence(self):
		"""The behaviour, and *which* refusal fired.

		Asserting only that a `PermissionError` is raised is not enough, and the
		first version of this proved it: with the role check deleted the call
		still refused, because `_resolve_company` will not let a Website User at
		a Company either - so the test passed on the wrong refusal. The two say
		different things, and only one of them is the fence this file is about,
		so the message is what is asserted.
		"""
		before = frappe.db.count("Tally Export Log")
		frappe.set_user(self.website_user)
		try:
			self.assertFalse(
				frappe.has_permission("Sales Invoice", "read"),
				"This fixture is only meaningful for an account that cannot read invoices.",
			)
			for label, call in (
				("export_all", lambda: export_api.export_all("2000-01-01", "2100-01-01")),
				(
					"export_sales_invoices",
					lambda: export_api.export_sales_invoices("2000-01-01", "2100-01-01"),
				),
				(
					"export_single_document",
					lambda: export_api.export_single_document("Sales Invoice", "does-not-matter"),
				),
				("process_tally_xml", lambda: import_api.process_tally_xml("<ENVELOPE/>")),
			):
				with self.subTest(endpoint=label):
					with self.assertRaises(frappe.PermissionError) as caught:
						call()
					self.assertIn(
						"This action needs one of",
						str(caught.exception),
						f"{label} refused, but not because of the role fence - so with the "
						"fence removed this would still pass.",
					)
		finally:
			frappe.set_user("Administrator")

		self.assertEqual(
			frappe.db.count("Tally Export Log"),
			before,
			"A refused caller got as far as opening an export log.",
		)

	def test_the_dashboard_page_refuses_the_same_account(self):
		from tally_connector.www.tally_connector import get_context

		frappe.set_user(self.website_user)
		try:
			with self.assertRaises(frappe.PermissionError):
				get_context(frappe._dict())
		finally:
			frappe.set_user("Administrator")

	def test_a_signed_out_visitor_is_sent_to_the_login(self):
		from tally_connector.www.tally_connector import get_context

		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.Redirect):
				get_context(frappe._dict())
		finally:
			frappe.set_user("Administrator")

	def test_a_company_the_caller_may_not_read_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			export_api._resolve_company("No Such Company Ltd")
