"""
A full export leaves a transfer package beside its two files, and the package
holds what they hold.

Run against the site's own books, so the package is built from a real export
rather than from fixtures.
"""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from tally_connector.api import export as export_api
from tally_connector.tally import package


class TestAFullExportIsPackaged(IntegrationTestCase):
	def setUp(self):
		self.company = frappe.get_all("Company", pluck="name")[0]
		result = export_api.export_all(
			from_date=None,
			to_date=None,
			company=self.company,
			push_to_tally_flag=False,
			background=False,
		)
		self.log = result.get("log")
		self.result = result

	def tearDown(self):
		if self.log:
			frappe.delete_doc("Tally Export Log", self.log, force=True, ignore_permissions=True)
			frappe.db.commit()

	def _content(self, url):
		return frappe.get_doc("File", {"file_url": url}).get_content()

	def test_the_package_verifies_and_carries_every_voucher(self):
		self.assertTrue(self.result.get("package_url"), self.result.get("error"))
		zipped = self._content(self.result["package_url"])
		self.assertEqual(package.verify(zipped), [])

		import io
		import json
		import zipfile

		with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
			manifest = json.loads(archive.read(package.MANIFEST))
			masters = archive.read(manifest["files"][0]["name"])

		vouchers = self._content(self.result["vouchers_url"])
		if isinstance(vouchers, str):
			vouchers = vouchers.encode("utf-8")
		self.assertEqual(manifest["totals"]["vouchers"], vouchers.count(b"<TALLYMESSAGE"))
		loose = self._content(self.result["masters_url"])
		self.assertEqual(masters, loose if isinstance(loose, bytes) else loose.encode("utf-8"))
		self.assertEqual(manifest["erpnext_company"], self.company)

	def test_the_status_of_a_finished_export_names_the_package(self):
		status = export_api.export_status(self.log)
		self.assertEqual(status["package_url"], self.result["package_url"])
		self.assertNotEqual(status["vouchers_url"], status["package_url"])
