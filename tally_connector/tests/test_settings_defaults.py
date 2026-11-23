"""
That a switch added to `Tally Settings` is on when it is meant to be.

`Tally Settings` is a Single. Frappe writes its defaults when the document is
first created and does not backfill them when a field is added later, so a Check
added to an existing site reads as **off** - and a section of the export
disappears with nothing reporting it.

That happened. The combined export read `settings.get("include_stock_groups", 1)`
for months while no such field existed; `Document.get` takes a positional second
argument as the default, so it answered 1. Adding the field so the switch could
be seen made it answer the stored value, which was nothing, and the seven stock
groups this site exports became zero on the next migrate. Nothing failed, which
is the problem: an export missing a section is indistinguishable from a site
that has none.
"""

import frappe
from frappe.tests import IntegrationTestCase

from tally_connector.patches.v1_0.backfill_new_settings_defaults import NEW_FIELDS, execute


def _stored():
	return dict(frappe.db.sql("select field, value from tabSingles where doctype = 'Tally Settings'"))


class TestSettingsDefaults(IntegrationTestCase):
	def test_no_include_switch_is_silently_unset(self):
		"""The property that matters, over every section switch rather than the
		one that broke."""
		meta = frappe.get_meta("Tally Settings")
		stored = _stored()
		unset = [
			f.fieldname
			for f in meta.fields
			if f.fieldname.startswith("include_") and stored.get(f.fieldname) in (None, "")
		]
		self.assertEqual(
			unset,
			[],
			f"These export sections are switched off because nothing ever wrote their "
			f"default, not because anybody chose to: {unset}",
		)

	def test_the_patch_restores_a_default_and_leaves_a_choice_alone(self):
		"""Both halves. Restoring every default would be a patch that overrides
		somebody deliberately switching a section off.

		The restore in `finally` commits, and that is not belt-and-braces. A
		test normally leans on `IntegrationTestCase` rolling the transaction
		back, and the subject here is a patch that calls `frappe.db.commit()` -
		so the writes under test survive the rollback while an uncommitted
		restore does not. The first version of this left
		`include_stock_groups` switched off on the site it ran against, which
		silently dropped a section from every subsequent export: the exact
		failure the test exists to catch, caused by the test.
		"""
		field = "include_stock_groups"
		original = frappe.db.get_single_value("Tally Settings", field)
		try:
			# Unset, as a site that has just gained the field looks.
			frappe.db.sql("delete from tabSingles where doctype = 'Tally Settings' and field = %s", field)
			frappe.clear_cache(doctype="Tally Settings")
			execute()
			self.assertEqual(
				str(frappe.db.get_single_value("Tally Settings", field)),
				"1",
				"The patch did not give the new switch its declared default.",
			)

			# Deliberately off. The patch must not turn it back on.
			frappe.db.set_value("Tally Settings", "Tally Settings", field, 0, update_modified=False)
			frappe.clear_cache(doctype="Tally Settings")
			execute()
			self.assertEqual(
				str(frappe.db.get_single_value("Tally Settings", field)),
				"0",
				"The patch overrode a section somebody had switched off.",
			)
		finally:
			frappe.db.set_value(
				"Tally Settings",
				"Tally Settings",
				field,
				1 if original in (None, "") else original,
				update_modified=False,
			)
			frappe.db.commit()
			frappe.clear_cache(doctype="Tally Settings")

	def test_every_new_field_named_by_the_patch_exists(self):
		"""A name renamed away leaves the patch silently doing nothing."""
		meta = frappe.get_meta("Tally Settings")
		for fieldname in NEW_FIELDS:
			with self.subTest(field=fieldname):
				self.assertIsNotNone(
					meta.get_field(fieldname),
					f"The backfill patch names {fieldname}, which is not on the DocType.",
				)
