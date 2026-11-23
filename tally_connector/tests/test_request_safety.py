"""What a request may do: by which HTTP method, to which file, and how often.

Three holes closed together. A whitelisted method with no `methods=` accepts
GET, which Frappe does not check for a CSRF token, and several of these commit
their own writes, so a link on another site could change where the books are
sent. The import read any File it was given a URL for. And the sync ledger was
read and then inserted with nothing to stop two pushes both inserting.
"""

import importlib
import inspect
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from tally_connector.patches.v1_1.merge_duplicate_sync_records import keep_of
from tally_connector.utils import sync_ledger

API_MODULES = ("dashboard", "export", "import_data", "jobs", "settings", "sync")

# Every method that writes, imports, pushes to Tally or queues a job.
WRITES = {
	"export.export_chart_of_accounts", "export.export_parties", "export.export_uoms",
	"export.export_stock_items", "export.export_sales_invoices",
	"export.export_purchase_invoices", "export.export_payment_in",
	"export.export_payment_out", "export.export_journal_entries",
	"export.export_bank_transactions", "export.export_all",
	"export.export_single_document", "import_data.process_tally_xml",
	"import_data.start_import", "jobs.cancel", "settings.save",
	"settings.set_sync_enabled",
}


def _whitelisted():
	for module_name in API_MODULES:
		module = importlib.import_module(f"tally_connector.api.{module_name}")
		for name, fn in inspect.getmembers(module, inspect.isfunction):
			if fn.__module__ == module.__name__ and fn in frappe.whitelisted:
				yield f"{module_name}.{name}", fn


class TestHttpMethods(UnitTestCase):
	def test_every_method_names_its_http_methods(self):
		found = dict(_whitelisted())
		self.assertGreaterEqual(len(found), 35)
		for name, fn in found.items():
			# Frappe's default, when `methods=` is left out, is all four.
			methods = frappe.allowed_http_methods_for_whitelisted_func[fn]
			self.assertNotEqual(sorted(methods), ["DELETE", "GET", "POST", "PUT"], name)

	def test_nothing_that_writes_accepts_get(self):
		found = dict(_whitelisted())
		self.assertEqual(WRITES - set(found), set(), "a write method was renamed")
		for name in WRITES:
			methods = frappe.allowed_http_methods_for_whitelisted_func[found[name]]
			self.assertEqual(methods, ["POST"], name)

	def test_a_method_not_listed_as_a_write_writes_nothing(self):
		"""A read that starts committing must be moved to WRITES."""
		for name, fn in _whitelisted():
			if name in WRITES:
				continue
			source = inspect.getsource(fn)
			for call in ("db.commit", ".insert(", ".save(", "enqueue(", "set_value("):
				self.assertNotIn(call, source, f"{name} calls {call}")


class TestImportReadsOnlyPermittedFiles(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.files = []

	def tearDown(self):
		frappe.set_user("Administrator")
		for doc in self.files:
			frappe.delete_doc("File", doc.name, ignore_permissions=True, force=True)
		super().tearDown()

	def _private_file(self, owner):
		frappe.set_user(owner)
		doc = frappe.get_doc({
			"doctype": "File", "file_name": f"tally-{frappe.generate_hash(length=8)}.xml",
			"content": b"<ENVELOPE/>", "is_private": 1,
		}).insert(ignore_permissions=True)
		frappe.set_user("Administrator")
		self.files.append(doc)
		return doc

	def _accounts_manager(self):
		email = "tally-import-reader@example.com"
		if not frappe.db.exists("User", email):
			frappe.get_doc({"doctype": "User", "email": email, "first_name": "Import",
			                "send_welcome_email": 0}).insert(ignore_permissions=True)
		user = frappe.get_doc("User", email)
		user.add_roles("Accounts Manager")
		return email

	def test_somebody_elses_private_file_is_refused(self):
		from tally_connector.api.import_data import _read_file

		other = self._private_file("Administrator")
		frappe.set_user(self._accounts_manager())
		with self.assertRaises(frappe.PermissionError):
			_read_file(other.file_url)

	def test_the_callers_own_upload_is_read(self):
		from tally_connector.api.import_data import _read_file

		user = self._accounts_manager()
		own = self._private_file(user)
		frappe.set_user(user)
		doc, content = _read_file(own.file_url)
		self.assertEqual(doc.name, own.name)
		self.assertEqual(content, "<ENVELOPE/>")

	def test_a_filter_in_place_of_a_url_is_refused(self):
		from tally_connector.api.import_data import _read_file

		self._private_file("Administrator")
		with self.assertRaises(frappe.ValidationError):
			_read_file(["like", "%"])


class TestOneLedgerRecordPerDocument(IntegrationTestCase):
	KEY = ("Sales Invoice", "SINV-RACE-0001", "", "Demo Traders Pvt Ltd")

	def test_the_database_refuses_a_second_record(self):
		sync_ledger.note_attempt(*self.KEY)
		second = frappe.new_doc(sync_ledger.DOCTYPE)
		second.update(sync_ledger._filters(*self.KEY))
		second.flags.ignore_permissions = True
		with self.assertRaises(frappe.UniqueValidationError):
			second.insert()

	def test_an_insert_that_loses_the_race_updates_the_winner(self):
		"""The other push's record exists, but this push read before it did."""
		first = sync_ledger.note_attempt(*self.KEY)
		real = sync_ledger.record_for
		calls = []

		def stale_first_read(*args):
			calls.append(args)
			return None if len(calls) == 1 else real(*args)

		with patch.object(sync_ledger, "record_for", stale_first_read):
			doc = sync_ledger.note_attempt(*self.KEY)

		self.assertEqual(doc.name, first.name)
		self.assertEqual(doc.attempts, 2)
		self.assertEqual(frappe.db.count(sync_ledger.DOCTYPE,
		                                 sync_ledger._filters(*self.KEY)), 1)


class TestMergingDuplicates(UnitTestCase):
	def _row(self, name, outcome, modified):
		return SimpleNamespace(name=name, outcome=outcome, modified=modified)

	def test_a_record_saying_tally_holds_it_wins(self):
		rows = [self._row("a", "Failed", 3), self._row("b", "Created", 1),
		        self._row("c", "Pending", 2)]
		self.assertEqual(keep_of(rows).name, "b")

	def test_a_record_waiting_on_a_person_beats_a_failure(self):
		rows = [self._row("a", "Failed", 3), self._row("b", "Unknown Result", 1)]
		self.assertEqual(keep_of(rows).name, "b")

	def test_otherwise_the_newest(self):
		rows = [self._row("a", "Failed", 1), self._row("b", "Conflict", 2)]
		self.assertEqual(keep_of(rows).name, "b")
