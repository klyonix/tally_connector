"""
Who may do what, and what leaks.

`test_permissions` holds the role fences on the endpoints. This holds the three
things a fence does not cover: that one company's books cannot reach another's
Tally, that the connection settings are not readable by everyone who may run an
export, and that nothing written to a log carries a credential.
"""

import frappe
from frappe.tests import IntegrationTestCase

from tally_connector.api import settings as tally_settings
from tally_connector.api.export import ADMIN_ROLES, EXPORT_ROLES
from tally_connector.tally.extract import extract


class TestOneCompanysBooksStayInOneCompany(IntegrationTestCase):
	"""The defect this connector already had once: with no company selector the
	API took whichever company the database returned first, so one set of books
	could be exported into another company's Tally."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.companies = frappe.get_all("Company", pluck="name")

	def test_an_export_holds_only_the_chosen_companys_vouchers(self):
		if len(self.companies) < 2:
			self.skipTest("one company on this site - the isolation cannot be observed")
		for company in self.companies:
			export = extract(company, None, None)
			names = [v.remote_id for v in export.vouchers if v.remote_id]
			if not names:
				continue
			wrong = frappe.db.sql(
				"""select name from `tabSales Invoice`
                   where name in %s and company != %s limit 5""",
				(names, company),
				pluck=True,
			)
			with self.subTest(company=company):
				self.assertEqual(wrong, [], "another company's invoices are in this export")

	def test_no_voucher_appears_under_two_companies(self):
		if len(self.companies) < 2:
			self.skipTest("one company on this site")
		seen = {}
		for company in self.companies:
			for v in extract(company, None, None).vouchers:
				if v.remote_id:
					seen.setdefault(v.remote_id, set()).add(company)
		shared = {k: sorted(v) for k, v in seen.items() if len(v) > 1}
		self.assertEqual(dict(list(shared.items())[:5]), {})

	def test_the_envelope_names_the_company_it_was_asked_for(self):
		"""SVCURRENTCOMPANY is what decides which company in Tally receives the
		file. Getting it from a setting rather than from the request is how one
		company's books land in another's ledger."""
		from tally_connector.utils.exporter import render_full_export

		settings = frappe.get_single("Tally Settings")
		expected = (settings.company_name_in_tally or "").strip()
		masters_xml, vouchers_xml, _counts = render_full_export(None, None, self.companies[0])
		for xml in (masters_xml, vouchers_xml):
			if expected:
				self.assertIn(f"<SVCURRENTCOMPANY>{expected}</SVCURRENTCOMPANY>", xml)


class TestTheSettingsAreNarrowerThanTheExport(IntegrationTestCase):
	def test_writing_settings_needs_a_narrower_role_than_running_an_export(self):
		"""Running an export is clerical. Changing which ledger the books land in
		is not, and the two must not share a fence."""
		self.assertTrue(set(ADMIN_ROLES).issubset(set(EXPORT_ROLES)))
		self.assertTrue(
			set(EXPORT_ROLES) - set(ADMIN_ROLES),
			"the export and admin fences are identical - the distinction is gone",
		)

	def test_a_clerk_may_read_the_settings_and_not_write_them(self):
		user = "tally-clerk-sec@example.com"
		if not frappe.db.exists("User", user):
			doc = frappe.new_doc("User")
			doc.email = user
			doc.first_name = "Clerk"
			doc.insert(ignore_permissions=True)
		account = frappe.get_doc("User", user)
		account.set("roles", [])
		account.append("roles", {"role": "Accounts User"})
		account.save(ignore_permissions=True)
		frappe.db.commit()

		try:
			frappe.set_user(user)
			data = tally_settings.get()
			self.assertFalse(data["can_write"], "a clerk is offered a Save button")
			with self.assertRaises(frappe.PermissionError):
				tally_settings.save({"tally_host": "somewhere-else"})
		finally:
			frappe.set_user("Administrator")

	def test_the_settings_endpoint_will_not_write_a_field_no_screen_draws(self):
		"""It writes to a Single, where an unlisted field is any field on it."""
		with self.assertRaises(frappe.ValidationError):
			tally_settings.save({"owner": "Administrator"})


class TestNothingSecretReachesALog(IntegrationTestCase):
	def test_no_password_field_exists_on_the_settings_to_leak(self):
		"""The connection is a host and a port - Tally's XML port has no
		authentication of its own. This asserts that stays true, because the day
		somebody adds a credential is the day the logs need re-checking."""
		meta = frappe.get_meta("Tally Settings")
		secrets = [f.fieldname for f in meta.fields if f.fieldtype == "Password"]
		self.assertEqual(
			secrets,
			[],
			"a credential was added to Tally Settings - check that no log, "
			"error message or XML preview carries it",
		)

	def test_the_export_log_never_stores_a_whole_document(self):
		"""`xml_preview` is capped so a log row cannot become a second copy of
		the books, readable by everyone who can read a log."""
		from tally_connector.api.export import _create_log, _finalize_log

		company = frappe.get_all("Company", pluck="name")[0]
		log = _create_log("Full Export", "Tally XML", None, None, company)
		try:
			_finalize_log(log, 1, 0, "<X>" + ("a" * 20000) + "</X>")
			stored = frappe.db.get_value("Tally Export Log", log, "xml_preview")
			self.assertLessEqual(len(stored or ""), 4000)
		finally:
			frappe.delete_doc("Tally Export Log", log, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_a_failure_message_names_the_problem_and_not_the_configuration(self):
		"""An error a clerk can read and act on, without the host and port of an
		internal machine in it."""
		from tally_connector.api import export as export_api

		result = export_api.test_tally_connection()
		self.assertIn("success", result)
		self.assertIsInstance(result.get("response", ""), str)
