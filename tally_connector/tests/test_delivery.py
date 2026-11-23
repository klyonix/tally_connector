"""
What became of a request decides whether it may be sent again.

A request that never left this server cannot be in Tally, so trying it again is
safe. A request Tally received and never answered may be in Tally already - a
large import that outlasts the timeout looks exactly like that from here - so
sending it again is how vouchers come to be posted twice. The connector used to
report both as "could not reach Tally", the sync ledger recorded the second as
Failed, and Failed is resendable.

The transport cases run against real sockets on this machine: a closed port, a
server that reads the request and never answers, and one that answers "busy".
No Tally is needed and none is touched.

Pure: no site.
"""

from __future__ import annotations

import socket
import threading
import time
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from tally_connector.utils import connection
from tally_connector.utils.connection import ANSWERED, LOST, NEVER_LEFT, TallyConnector


def _connector(port, timeout=1.0):
	"""A connector pointed at this machine, built without reading settings."""
	conn = TallyConnector.__new__(TallyConnector)
	conn.host, conn.port, conn.company = "127.0.0.1", port, "Demo Traders Pvt Ltd"
	conn.base_url = f"http://127.0.0.1:{port}"
	conn.timeout, conn.probe_timeout, conn.connect_timeout = timeout, timeout, timeout
	return conn


def _closed_port():
	"""A port nothing is listening on."""
	with socket.socket() as s:
		s.bind(("127.0.0.1", 0))
		return s.getsockname()[1]


class _Server:
	"""Accepts connections, reads the request, then does what it is told."""

	def __init__(self, reply=None):
		self.reply = reply
		self.requests = 0
		self.sock = socket.socket()
		self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		self.sock.bind(("127.0.0.1", 0))
		self.sock.listen(5)
		self.port = self.sock.getsockname()[1]
		self._stop = False
		self.thread = threading.Thread(target=self._serve, daemon=True)
		self.thread.start()

	def _serve(self):
		self.sock.settimeout(0.2)
		while not self._stop:
			try:
				client, _ = self.sock.accept()
			except OSError:
				continue
			with client:
				client.settimeout(2)
				try:
					client.recv(65536)
				except OSError:
					continue
				self.requests += 1
				if self.reply is None:
					# Tally is "still importing": hold the connection open
					# until the caller gives up.
					time.sleep(1.5)
					continue
				body = self.reply.encode("utf-8")
				client.sendall(
					b"HTTP/1.1 200 OK\r\nContent-Type: text/xml\r\n"
					+ f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
					+ body
				)

	def close(self):
		self._stop = True
		self.sock.close()
		self.thread.join(timeout=2)


ANSWER = (
	"<RESPONSE><CREATED>2</CREATED><ALTERED>0</ALTERED><IGNORED>0</IGNORED>"
	"<ERRORS>0</ERRORS><EXCEPTIONS>0</EXCEPTIONS><LASTVCHID>41</LASTVCHID></RESPONSE>"
)
BUSY = "<RESPONSE>Error 6010: Request cannot be served, please try later</RESPONSE>"


class TestWhatBecameOfTheRequest(unittest.TestCase):
	def test_a_closed_port_never_left(self):
		state, text = _connector(_closed_port()).send("<ENVELOPE/>")
		self.assertEqual(state, NEVER_LEFT)
		self.assertIn("Cannot connect", text)

	def test_a_name_that_does_not_resolve_never_left(self):
		conn = _connector(9000)
		conn.base_url = "http://tally.invalid:9000"
		state, _text = conn.send("<ENVELOPE/>")
		self.assertEqual(state, NEVER_LEFT)

	def test_a_request_received_and_not_answered_is_lost_not_unreached(self):
		server = _Server(reply=None)
		try:
			state, text = _connector(server.port, timeout=0.5).send("<ENVELOPE/>")
		finally:
			server.close()
		self.assertEqual(state, LOST)
		self.assertIn("may still be importing", text)
		self.assertEqual(server.requests, 1, "the request did arrive, which is the point")

	def test_an_answer_is_answered(self):
		server = _Server(reply=ANSWER)
		try:
			state, text = _connector(server.port).send("<ENVELOPE/>")
		finally:
			server.close()
		self.assertEqual(state, ANSWERED)
		self.assertIn("<CREATED>2</CREATED>", text)


class TestOnlyWhatCannotBeInTallyIsTriedAgain(unittest.TestCase):
	def setUp(self):
		patcher = patch.object(connection, "SEND_PAUSES", (0, 0))
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_a_request_that_never_left_is_tried_again(self):
		state, _text, tries = connection._deliver(_connector(_closed_port()), "<ENVELOPE/>")
		self.assertEqual(state, NEVER_LEFT)
		self.assertEqual(tries, connection.SEND_TRIES)

	def test_a_lost_reply_is_never_sent_again(self):
		server = _Server(reply=None)
		try:
			state, _text, tries = connection._deliver(_connector(server.port, timeout=0.5), "<ENVELOPE/>")
		finally:
			server.close()
		self.assertEqual(state, LOST)
		self.assertEqual(tries, 1)
		self.assertEqual(server.requests, 1, "a second copy of a delivered import is the duplicate")

	def test_busy_is_tried_again(self):
		server = _Server(reply=BUSY)
		try:
			state, _text, tries = connection._deliver(_connector(server.port), "<ENVELOPE/>")
		finally:
			server.close()
		self.assertEqual(state, ANSWERED)
		self.assertEqual(tries, connection.SEND_TRIES)
		self.assertEqual(server.requests, connection.SEND_TRIES)

	def test_an_answer_is_not_tried_again(self):
		server = _Server(reply=ANSWER)
		try:
			_state, _text, tries = connection._deliver(_connector(server.port), "<ENVELOPE/>")
		finally:
			server.close()
		self.assertEqual(tries, 1)


class TestBusyMeansNothingWasImported(unittest.TestCase):
	def test_tallys_busy_words_are_busy(self):
		self.assertTrue(connection._busy(BUSY))
		self.assertTrue(connection._busy("<RESPONSE>Error 6612 unable to establish session</RESPONSE>"))

	def test_an_answer_with_import_counts_is_never_busy(self):
		"""The words inside an import result - a narration, a line error -
		must not turn an import into a retry, which would duplicate it."""
		self.assertFalse(
			connection._busy(
				"<RESPONSE><CREATED>3</CREATED><LINEERROR>please try later</LINEERROR></RESPONSE>"
			)
		)

	def test_an_ordinary_answer_is_not_busy(self):
		self.assertFalse(connection._busy(ANSWER))
		self.assertFalse(connection._busy(""))


def _result(**kw):
	out = {
		"success": False,
		"reached": True,
		"unknown": False,
		"created": 0,
		"altered": 0,
		"ignored": 0,
		"errors": [],
		"last_voucher_id": "",
		"response": "",
	}
	out.update(kw)
	return out


class TestTheLedgerReadsTheRightState(unittest.TestCase):
	"""What `push_to_tally` concludes, and so what the sync ledger records."""

	def _push(self, state, text):
		conn = _connector(9000)
		with (
			patch.object(connection, "TallyConnector", return_value=conn),
			patch.object(connection, "SEND_PAUSES", (0, 0)),
			patch.object(conn, "send", return_value=(state, text)),
		):
			return connection.push_to_tally("<ENVELOPE><TALLYMESSAGE/></ENVELOPE>")

	def test_a_lost_reply_is_unknown_and_so_not_resendable(self):
		from tally_connector.utils.sync_ledger import RESENDABLE, UNKNOWN, outcome_from_push

		result = self._push(LOST, "Tally was sent the request and did not answer within 60s.")
		self.assertTrue(result["unknown"])
		self.assertFalse(result["success"])
		outcome, _message = outcome_from_push(result)
		self.assertEqual(outcome, UNKNOWN)
		self.assertNotIn(outcome, RESENDABLE)

	def test_a_request_that_never_left_is_failed_and_so_resendable(self):
		from tally_connector.utils.sync_ledger import FAILED, outcome_from_push

		result = self._push(NEVER_LEFT, "Cannot connect to Tally.")
		self.assertFalse(result["reached"])
		self.assertFalse(result["unknown"])
		self.assertEqual(outcome_from_push(result)[0], FAILED)
		self.assertEqual(result["tries"], connection.SEND_TRIES)

	def test_busy_after_every_try_is_a_refusal_not_an_unknown(self):
		from tally_connector.utils.sync_ledger import FAILED, outcome_from_push

		result = self._push(ANSWERED, BUSY)
		self.assertFalse(result["unknown"])
		self.assertEqual(outcome_from_push(result)[0], FAILED)
		self.assertIn("nothing was imported", result["errors"][0])


def _voucher(number):
	return SimpleNamespace(number=number, date=date(2026, 4, 1), kind="Sales", remote_id=number)


class TestALostReplyIsReadBack(unittest.TestCase):
	"""A batch whose answer was lost is asked about, not given up on."""

	def _push(self, vouchers, held, batch=2):
		results = iter(
			[_result(unknown=True, errors=["did not answer"])] + [_result(success=True, created=2)] * 5
		)
		calls = []

		def push(xml, _log=None):
			calls.append(xml)
			return next(results)

		def split(chunk, _company, _period):
			if held is None:
				return None, None
			return [v for v in chunk if v.number in held], [v for v in chunk if v.number not in held]

		with (
			patch.object(connection, "push_to_tally", push),
			patch.object(connection, "_split_by_what_tally_holds", split),
			patch("tally_connector.tally.render.render_vouchers", return_value="<ENVELOPE/>"),
		):
			summary = connection.push_vouchers("Demo Traders Pvt Ltd", vouchers, batch=batch)
		return summary, calls

	def test_a_batch_tally_turns_out_to_hold_is_counted_and_the_run_goes_on(self):
		vouchers = [_voucher(n) for n in ("SINV-1", "SINV-2", "SINV-3", "SINV-4")]
		summary, calls = self._push(vouchers, held={"SINV-1", "SINV-2"})
		self.assertTrue(summary["success"])
		self.assertEqual(summary["read_back"], 2)
		self.assertEqual(summary["sent"], 4)
		self.assertEqual(len(calls), 2, "nothing was sent twice")

	def test_part_of_a_batch_still_unknown_stops_the_run_and_is_not_resent(self):
		vouchers = [_voucher(n) for n in ("SINV-1", "SINV-2", "SINV-3", "SINV-4")]
		summary, calls = self._push(vouchers, held={"SINV-1"})
		self.assertFalse(summary["success"])
		self.assertTrue(summary["unknown"])
		self.assertEqual(summary["read_back"], 1)
		self.assertEqual(summary["stopped_at"], 1)
		self.assertEqual(len(calls), 1)

	def test_a_tally_that_cannot_be_asked_leaves_the_batch_unknown(self):
		vouchers = [_voucher(n) for n in ("SINV-1", "SINV-2")]
		summary, calls = self._push(vouchers, held=None)
		self.assertTrue(summary["unknown"])
		self.assertEqual(summary["read_back"], 0)
		self.assertEqual(len(calls), 1)


if __name__ == "__main__":
	unittest.main()
