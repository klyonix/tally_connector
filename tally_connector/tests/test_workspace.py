"""
The Desk workspace, and the button on it that opens the connector.

Two failures this guards against, and neither shows up as an error anywhere:

* **A shortcut that exists as a record and is drawn nowhere.** The Desk renders
  `Workspace.content`, not the `shortcuts` table - a shortcut absent from the
  layout is a button somebody added, committed and migrated, and which does not
  appear. The reverse is just as quiet: a block naming a shortcut that no longer
  exists renders an empty slot.

* **A button the people who need it cannot see.** `Workspace.roles` gates the
  whole workspace, and `Accounts User` - which is in `EXPORT_ROLES`, the whole
  clerical point of this app - was not on it.
"""

import json

import frappe
from frappe.tests import IntegrationTestCase

from tally_connector.api.export import EXPORT_ROLES

WORKSPACE = "Tally Connector"
APP_ROUTE = "/tally-connector"


def _workspace():
	return frappe.get_doc("Workspace", WORKSPACE)


def _blocks(doc):
	return [
		block.get("data", {}).get("shortcut_name")
		for block in json.loads(doc.content)
		if block.get("type") == "shortcut"
	]


class TestTheWorkspaceOpensTheConnector(IntegrationTestCase):
	def test_there_is_a_shortcut_to_the_application(self):
		shortcut = next((s for s in _workspace().shortcuts if s.type == "URL" and s.url == APP_ROUTE), None)
		self.assertIsNotNone(
			shortcut,
			f"nothing on the workspace opens {APP_ROUTE} - the Desk has no way into the app",
		)
		self.assertTrue(shortcut.label, "the shortcut has no label to click")

	def test_the_shortcut_points_at_a_route_this_app_actually_serves(self):
		"""A shortcut to a route no rule answers is a button that opens a 404."""
		from tally_connector import hooks

		served = {rule["from_route"] for rule in hooks.website_route_rules}
		self.assertIn(APP_ROUTE, served)

	def test_every_shortcut_is_drawn_and_every_block_has_one(self):
		doc = _workspace()
		labels = [s.label for s in doc.shortcuts]
		blocks = _blocks(doc)
		self.assertTrue(blocks, "the layout draws no shortcuts at all")
		self.assertEqual(
			sorted(set(labels) - set(blocks)),
			[],
			"these shortcuts exist as records and appear nowhere on the page",
		)
		self.assertEqual(
			sorted(set(blocks) - set(labels)),
			[],
			"the layout names shortcuts that do not exist, which renders an empty slot",
		)

	def test_everyone_who_may_use_the_connector_can_see_the_workspace(self):
		"""`EXPORT_ROLES` is who the endpoints admit. A role that may run an
		export and cannot see the page holding the button is a gap between two
		lists that nothing else compares."""
		roles = {r.role for r in _workspace().roles}
		self.assertEqual(sorted(set(EXPORT_ROLES) - roles), [])


class TestTheDeskFiltersItCorrectly(IntegrationTestCase):
	"""The behaviour, as the Desk itself computes it.

	Asserting the record says the right thing is not the same as asserting the
	Desk serves it: shortcuts go through `is_item_allowed`, which drops the ones
	the caller holds no permission on. This is what a clerk actually receives.
	"""

	def setUp(self):
		self.user = "tally-clerk@example.com"
		if not frappe.db.exists("User", self.user):
			doc = frappe.new_doc("User")
			doc.email = self.user
			doc.first_name = "Tally"
			doc.last_name = "Clerk"
			doc.insert(ignore_permissions=True)
		user = frappe.get_doc("User", self.user)
		user.set("roles", [])
		user.append("roles", {"role": "Accounts User"})
		user.save(ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")

	def _shortcuts_for(self, user):
		from frappe.desk.desktop import get_desktop_page

		frappe.set_user(user)
		frappe.clear_cache(user=user)
		page = get_desktop_page(json.dumps({"name": WORKSPACE, "public": 1}))
		return {s["label"]: s for s in page["shortcuts"]["items"]}

	def test_a_clerk_is_served_the_button(self):
		shortcuts = self._shortcuts_for(self.user)
		self.assertIn("Open Tally Connector", shortcuts)
		self.assertEqual(shortcuts["Open Tally Connector"]["url"], APP_ROUTE)

	def test_a_clerk_is_not_served_the_settings_shortcut(self):
		"""They hold no read on `Tally Settings`, so the Desk drops it. This is
		why adding the role to the workspace widens nothing: what each person
		sees is still decided per shortcut."""
		self.assertNotIn("Tally Settings", self._shortcuts_for(self.user))

	def test_the_logs_they_can_read_are_still_served(self):
		"""The filter has to be doing real work in both directions - a test that
		only shows things disappearing passes against a page serving nothing."""
		shortcuts = self._shortcuts_for(self.user)
		self.assertIn("Tally Export Log", shortcuts)
