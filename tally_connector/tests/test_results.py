"""
Asking Tally for a result per record, and reading the answer safely.

The TDL itself has not been run on a TallyPrime by this project, so these tests
hold what can be held without one: the request carries every record unchanged
under the same company, the answer is read only when it names every record
sent, and anything less falls back to reading the answer the way it is read
without asking.

Pure: no site.
"""

from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from lxml import etree

from tally_connector.tally import golden, results
from tally_connector.tally.render import render_masters, render_vouchers
from tally_connector.utils import connection
from tally_connector.utils.connection import ANSWERED, TallyConnector

COMPANY = "Demo Traders Pvt Ltd"
_PLAIN = etree.XMLParser(remove_blank_text=True)


def _vouchers_xml():
	vouchers = [v for name in golden.scenarios() for v in golden.load(name).vouchers]
	return render_vouchers(COMPANY, vouchers), vouchers


def _masters_xml():
	return render_masters(COMPANY, golden.load("sale_domestic_gst").masters)


def _messages(xml):
	root = etree.fromstring(xml.encode("utf-8"), _PLAIN)
	return [etree.tostring(m) for m in root.iter("TALLYMESSAGE")]


def _answer(rows):
	"""A response carrying the report, one RESULT per (key, error, action)."""
	body = "".join(
		f"<{results.RESULT_TAG}><KIND>Voucher</KIND><KEY>{key}</KEY><ACTION>{action}</ACTION>"
		f"<MASTERID>{i + 40}</MASTERID><GUID>g-{i}</GUID><ERROR>{error}</ERROR></{results.RESULT_TAG}>"
		for i, (key, error, action) in enumerate(rows)
	)
	return f"<ENVELOPE><BODY><DATA><{results.RESULTS_TAG}>{body}</{results.RESULTS_TAG}></DATA></BODY></ENVELOPE>"


class TestTheRequest(unittest.TestCase):
	def test_every_voucher_goes_unchanged(self):
		xml, _vouchers = _vouchers_xml()
		self.assertEqual(_messages(results.with_results(xml)), _messages(xml))

	def test_it_is_the_versioned_import_for_the_same_file_and_company(self):
		xml, _vouchers = _vouchers_xml()
		root = etree.fromstring(results.with_results(xml).encode("utf-8"))
		self.assertEqual(root.findtext("HEADER/VERSION"), "1")
		self.assertEqual(root.findtext("HEADER/TALLYREQUEST"), "Import")
		self.assertEqual(root.findtext("HEADER/TYPE"), "Data")
		self.assertEqual(root.findtext("HEADER/ID"), "Vouchers")
		self.assertEqual(root.findtext("BODY/DESC/STATICVARIABLES/SVCURRENTCOMPANY"), COMPANY)

	def test_the_tdl_switches_the_report_on_for_that_import_file(self):
		xml, _vouchers = _vouchers_xml()
		root = etree.fromstring(results.with_results(xml).encode("utf-8"))
		tdl = root.find("BODY/DESC/TDL/TDLMESSAGE")
		base = tdl.find("IMPORTFILE[@NAME='Vouchers']")
		self.assertEqual(base.get("ISMODIFY"), "Yes")
		self.assertIn(results.OPTION, base.findtext("ADD"))
		option = tdl.find(f"IMPORTFILE[@NAME='{results.OPTION}']")
		self.assertEqual(option.findtext("RESPONSEREPORT"), results.REPORT)
		self.assertEqual(tdl.find(f"FORM[@NAME='{results.REPORT}']").findtext("XMLTAG"), results.RESULTS_TAG)

	def test_masters_ask_about_the_masters_import(self):
		masters = _masters_xml()
		root = etree.fromstring(results.with_results(masters).encode("utf-8"))
		self.assertEqual(root.findtext("HEADER/ID"), "All Masters")
		self.assertEqual(_messages(results.with_results(masters)), _messages(masters))

	def test_something_that_is_not_an_import_is_refused(self):
		with self.assertRaises(ValueError):
			results.with_results("<ENVELOPE><HEADER/></ENVELOPE>")

	def test_the_keys_are_voucher_numbers_and_master_names(self):
		xml, vouchers = _vouchers_xml()
		self.assertEqual(results.keys_in(xml), [v.number for v in vouchers])
		masters = golden.load("sale_domestic_gst").masters
		self.assertIn(masters.ledgers[0].name, results.keys_in(_masters_xml()))
		self.assertTrue(all(results.keys_in(_masters_xml())))


class TestReadingTheAnswer(unittest.TestCase):
	def test_results_are_read(self):
		rows = results.parse(_answer([("SINV-1", "", "Create"), ("SINV-2", "Ledger 'X' does not exist", "")]))
		self.assertEqual([r["key"] for r in rows], ["SINV-1", "SINV-2"])
		self.assertEqual(rows[0]["master_id"], "40")
		self.assertTrue(results.landed(rows[0]))
		self.assertFalse(results.landed(rows[1]))

	def test_an_answer_without_the_report_is_none_not_empty(self):
		self.assertIsNone(results.parse("<RESPONSE><CREATED>2</CREATED></RESPONSE>"))
		self.assertIsNone(results.parse(""))

	def test_only_a_complete_answer_is_trusted(self):
		rows = results.parse(_answer([("SINV-1", "", "Create")]))
		self.assertIsNone(results.complete(rows, ["SINV-1", "SINV-2"]))
		self.assertEqual(set(results.complete(rows, ["SINV-1"])), {"sinv-1"})

	def test_keys_match_the_way_tally_matches_names(self):
		rows = results.parse(_answer([("Duties  and Taxes", "", "Alter")]))
		matched = results.complete(rows, ["duties and taxes"])
		self.assertIsNotNone(matched)
		self.assertTrue(results.altered(matched["duties and taxes"]))


def _connector():
	conn = TallyConnector.__new__(TallyConnector)
	conn.host, conn.port, conn.company = "127.0.0.1", 9000, COMPANY
	conn.base_url = "http://127.0.0.1:9000"
	conn.timeout, conn.probe_timeout, conn.connect_timeout = 1, 1, 1
	return conn


class TestThePushReadsWhatTallySaid(unittest.TestCase):
	def _push(self, answer, xml):
		conn = _connector()
		sent = []

		def send(body, timeout=None):
			sent.append(body)
			return ANSWERED, answer

		with (
			patch.object(connection, "TallyConnector", return_value=conn),
			patch.object(conn, "send", side_effect=send),
		):
			result = connection.push_to_tally(xml, per_record=True)
		return result, sent

	def test_a_full_answer_is_a_result_per_voucher(self):
		xml, vouchers = _vouchers_xml()
		result, sent = self._push(_answer([(v.number, "", "Create") for v in vouchers]), xml)
		self.assertIn(results.REPORT, sent[0], "the request asked for the report")
		self.assertTrue(result["success"])
		self.assertEqual(result["created"], len(vouchers))
		self.assertEqual(len(result["records"]), len(vouchers))

	def test_a_refusal_is_named_with_tallys_words(self):
		xml, vouchers = _vouchers_xml()
		rows = [(v.number, "", "Create") for v in vouchers]
		rows[2] = (vouchers[2].number, "Ledger 'Office Rent' does not exist", "")
		result, _sent = self._push(_answer(rows), xml)
		self.assertFalse(result["success"])
		self.assertFalse(result["unknown"])
		self.assertEqual(result["errors"], [f"{vouchers[2].number}: Ledger 'Office Rent' does not exist"])

	def test_an_ordinary_answer_is_read_the_ordinary_way(self):
		xml, vouchers = _vouchers_xml()
		answer = (
			f"<RESPONSE><CREATED>{len(vouchers)}</CREATED><ALTERED>0</ALTERED><ERRORS>0</ERRORS></RESPONSE>"
		)
		result, _sent = self._push(answer, xml)
		self.assertIsNone(result["records"])
		self.assertTrue(result["success"])

	def test_a_partial_answer_is_not_trusted(self):
		xml, vouchers = _vouchers_xml()
		result, _sent = self._push(_answer([(vouchers[0].number, "", "Create")]), xml)
		self.assertIsNone(result["records"])
		self.assertTrue(result["unknown"], "with neither a full list nor totals, nobody knows")

	def test_off_sends_the_ordinary_import(self):
		xml, _vouchers = _vouchers_xml()
		conn = _connector()
		with (
			patch.object(connection, "TallyConnector", return_value=conn),
			patch.object(
				conn, "send", return_value=(ANSWERED, "<RESPONSE><CREATED>1</CREATED></RESPONSE>")
			) as send,
		):
			connection.push_to_tally(xml, per_record=False)
		self.assertEqual(send.call_args[0][0], xml)


def _voucher(number):
	return SimpleNamespace(number=number, date=date(2026, 4, 1), kind="Sales", remote_id=number)


class TestVouchersUseTheResultPerRecord(unittest.TestCase):
	def _push(self, vouchers, errors, keep_going=False):
		records = {
			results.normalise(v.number): {
				"kind": "Voucher",
				"key": v.number,
				"action": "Create",
				"master_id": "",
				"guid": "",
				"error": errors.get(v.number, ""),
			}
			for v in vouchers
		}
		answer = {
			"success": not errors,
			"reached": True,
			"unknown": False,
			"created": len(vouchers) - len(errors),
			"altered": 0,
			"errors": [f"{k}: {e}" for k, e in errors.items()],
			"records": records,
		}

		def probe(*_args):
			raise AssertionError("Tally named every voucher; there was nothing to ask it")

		with (
			patch.object(connection, "push_to_tally", return_value=answer),
			patch.object(connection, "_split_by_what_tally_holds", probe),
			patch("tally_connector.tally.render.render_vouchers", return_value="<ENVELOPE/>"),
		):
			return connection.push_vouchers(
				COMPANY,
				vouchers,
				batch=len(vouchers),
				keep_going=keep_going,
				period=("2026-04-01", "2026-04-30"),
			)

	def test_a_refused_voucher_is_named_with_its_error_and_not_probed(self):
		vouchers = [_voucher(f"SINV-{i}") for i in range(1, 4)]
		summary = self._push(vouchers, {"SINV-2": "Voucher totals do not match"})
		self.assertFalse(summary["success"])
		self.assertEqual(summary["sent"], 2)
		self.assertEqual([r["number"] for r in summary["refused"]], ["SINV-2"])
		self.assertEqual(summary["refused"][0]["error"], "Voucher totals do not match")
		self.assertEqual(summary["stopped_at"], 1)

	def test_keeping_going_needs_no_probe_either(self):
		vouchers = [_voucher(f"SINV-{i}") for i in range(1, 4)]
		summary = self._push(vouchers, {"SINV-3": "Ledger 'X' does not exist"}, keep_going=True)
		self.assertIsNone(summary["stopped_at"])
		self.assertEqual(summary["sent"], 2)
		self.assertEqual(summary["refused"][0]["error"], "Ledger 'X' does not exist")

	def test_a_clean_batch_is_all_sent(self):
		vouchers = [_voucher(f"SINV-{i}") for i in range(1, 4)]
		summary = self._push(vouchers, {})
		self.assertTrue(summary["success"])
		self.assertEqual(summary["sent"], 3)


class TestMastersUseTheResultPerRecord(unittest.TestCase):
	def test_refused_masters_are_named_without_sending_them_one_at_a_time(self):
		from tally_connector.tally import model

		masters = model.Masters()
		masters.ledgers = [
			model.Ledger(name=name, parent="Sundry Debtors")
			for name in ("Acme Industries", "Brightline Retail")
		]
		records = {
			results.normalise("Acme Industries"): {"key": "Acme Industries", "error": "", "action": "Create"},
			results.normalise("Brightline Retail"): {
				"key": "Brightline Retail",
				"error": "Group 'Sundry Debtors' does not exist",
				"action": "",
			},
		}
		calls = []

		def push(xml, _log=None):
			calls.append(xml)
			return {
				"success": False,
				"reached": True,
				"unknown": False,
				"created": 1,
				"altered": 0,
				"errors": ["Brightline Retail: Group 'Sundry Debtors' does not exist"],
				"records": records,
			}

		with patch.object(connection, "push_to_tally", push):
			summary = connection.push_masters(COMPANY, masters, name_refusals=True)
		self.assertEqual(len(calls), 1, "no master was sent again on its own")
		self.assertEqual([r["name"] for r in summary["refused"]], ["Brightline Retail"])
		self.assertIn("does not exist", summary["refused"][0]["error"])
		self.assertEqual(summary["sent"], 1)


if __name__ == "__main__":
	unittest.main()
