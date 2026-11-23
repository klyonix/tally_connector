"""
tally_connector/utils/connection.py

Connects to Tally Prime's built-in XML server (default port 9000).
Tally must be open with a company loaded and XML server enabled in
Gateway of Tally → F12 Configuration → Advanced Configuration → Enable XML Server.
"""

import time

import frappe
import requests
from frappe.utils import now_datetime
from frappe.utils.caching import request_cache


class TallyConnector:
	"""HTTP client that speaks Tally Prime's XML protocol."""

	def __init__(self):
		settings = frappe.get_single("Tally Settings")
		self.host = settings.tally_host or "localhost"
		self.port = settings.tally_port or 9000
		self.company = settings.company_name_in_tally
		self.base_url = f"http://{self.host}:{self.port}"

		# Two timeouts, because the two things this client does have nothing in
		# common. Pushing a year of vouchers is Tally parsing tens of megabytes
		# and can legitimately take a minute. *Asking whether Tally is there* is
		# a handshake with a machine on the same LAN, and waiting a minute for
		# the answer means the Test Connection button spins for a minute
		# whenever the accounts PC is off - which is most evenings, and which is
		# exactly what a person presses that button to find out.
		self.timeout = 60  # a push
		self.probe_timeout = 5  # a question

	# ──────────────────────────────────────────────────────────────
	# Core HTTP method
	# ──────────────────────────────────────────────────────────────

	def _post_xml(self, xml_string, timeout=None):
		"""POST raw XML to Tally and return (success, response_text)."""
		headers = {
			"Content-Type": "text/xml; charset=utf-8",
		}
		timeout = timeout or self.timeout
		try:
			resp = requests.post(
				self.base_url,
				data=xml_string.encode("utf-8"),
				headers=headers,
				timeout=timeout,
			)
			resp.raise_for_status()
			return True, resp.text
		except requests.exceptions.ConnectionError:
			return False, (
				f"Cannot connect to Tally at {self.base_url}. "
				"Make sure Tally Prime is open and XML Server is enabled. "
				"If ERPNext is in the cloud and Tally is on your local LAN, direct connection won't work. "
				"In that case, please uncheck 'Push directly to Tally' and use the 'Download XML' option instead."
			)
		except requests.exceptions.Timeout:
			return False, (
				f"Tally did not answer within {timeout}s at {self.base_url}. "
				"Check that Tally Prime is open and its XML Server is enabled on "
				"that port."
			)
		except requests.exceptions.HTTPError as e:
			return False, f"HTTP error from Tally: {e}"
		except Exception as e:
			return False, str(e)

	# ──────────────────────────────────────────────────────────────
	# Connectivity test
	# ──────────────────────────────────────────────────────────────

	def test_connection(self):
		"""Whether Tally is there, and which companies it has.

		This used to ask for a report called `List of Companies`, and a real
		TallyPrime answers
		*"<LINEERROR>Could not find Report 'List of Companies'!</LINEERROR>"* -
		with HTTP 200, so the check reported the connection good and the body
		was discarded anyway. Two faults covering for each other: a request
		Tally cannot serve, and a caller that would not have noticed.

		It asks for the Company collection instead, which the same Tally
		answers with `<COMPANY NAME="..." >` and the books' own dates. That is
		what makes `SVCURRENTCOMPANY` checkable, and a mismatch there imports
		nothing while reporting success.
		"""
		success, response = self._post_xml(
			self._collection("Cmps", "Company", ["Name", "BooksFrom", "StartingFrom", "EndingAt"]),
			timeout=self.probe_timeout,
		)
		# Tally answers a request it cannot serve with HTTP 200 and a
		# LINEERROR, so the body decides, not the status line.
		if success and "<LINEERROR>" in (response or ""):
			return False, response
		return success, response

	# ──────────────────────────────────────────────────────────────
	# Import XML data into Tally
	# ──────────────────────────────────────────────────────────────

	def push_xml(self, xml_string):
		"""Push a TALLYMESSAGE XML string directly to Tally Prime."""
		return self._post_xml(xml_string)

	# ──────────────────────────────────────────────────────────────
	# Parse Tally response
	# ──────────────────────────────────────────────────────────────

	def parse_tally_response(self, response_text):
		"""What Tally says it did, or an explanation of why that is unknown.

		Tally answers an import with a summary - CREATED, ALTERED, IGNORED,
		ERRORS, EXCEPTIONS - and a LINEERROR per refused line.

		**An answer this cannot parse is a failure, not a success.** It used to
		be swallowed, so an HTML error page, a truncated body or a proxy's
		reply all read as `created=0, altered=0, errors=[]`, which the caller
		then recorded as a clean import. That is the worst reading available:
		it is indistinguishable from a real import of nothing, and somebody
		believes their books are in Tally when nothing arrived.

		**ERRORS is read as well as LINEERROR.** The count is the authoritative
		field; a run that reports errors without line bodies used to look
		perfect.
		"""
		result = {
			"created": 0,
			"altered": 0,
			"ignored": 0,
			"error_count": 0,
			"exceptions": 0,
			"last_voucher_id": "",
			"errors": [],
			"parsed": False,
			"raw": response_text,
		}
		if not (response_text or "").strip():
			result["errors"].append("Tally returned an empty body.")
			return result
		try:
			from lxml import etree

			root = etree.fromstring(response_text.encode("utf-8"))
		except Exception as exc:
			result["errors"].append(
				f"Tally's answer could not be read as XML ({exc}). "
				f"First 200 characters: {response_text[:200]!r}"
			)
			return result

		def total(tag):
			out = 0
			for elem in root.iter(tag):
				try:
					out += int((elem.text or "0").strip() or 0)
				except ValueError:
					continue
			return out

		# Well-formed is not the same as ours. An HTML error page parses
		# perfectly as XML and would otherwise read as a clean import of
		# nothing, which is the worst answer available.
		known = (
			"CREATED",
			"ALTERED",
			"IGNORED",
			"ERRORS",
			"EXCEPTIONS",
			"LINEERROR",
			"DELETED",
			"COMBINED",
			"LASTVCHID",
		)
		if not any(next(root.iter(tag), None) is not None for tag in known):
			result["errors"].append(
				"Tally's answer carried none of the fields an import reply has. "
				f"First 200 characters: {response_text[:200]!r}"
			)
			return result

		result["parsed"] = True
		result["created"] = total("CREATED")
		result["altered"] = total("ALTERED")
		result["ignored"] = total("IGNORED")
		result["error_count"] = total("ERRORS")
		result["exceptions"] = total("EXCEPTIONS")
		for elem in root.iter("LASTVCHID"):
			result["last_voucher_id"] = (elem.text or "").strip()
		for elem in root.iter("LINEERROR"):
			text = (elem.text or "").strip()
			if text:
				result["errors"].append(text)
		# The count is authoritative. A run that reports errors and no line
		# bodies is still a run with errors.
		missing = result["error_count"] + result["exceptions"] - len(result["errors"])
		if missing > 0:
			result["errors"].append(
				f"Tally reported {result['error_count']} errors and "
				f"{result['exceptions']} exceptions and named "
				f"{len(result['errors'])} of them. Read Tally.imp in the Tally "
				"folder for the rest."
			)
		return result

	# ──────────────────────────────────────────────────────────────
	# Read-only questions about the destination company
	#
	# Two validator checks cannot be answered from ERPNext alone: whether a
	# voucher's date falls inside the company's open books, and whether a
	# voucher type exists there. Tally answers both over the same gateway, as
	# `Export Data` requests that change nothing.
	#
	# Deliberately separate from exporting. Asking is optional, the answers only
	# ever add checks, and a Tally that is unreachable leaves those checks
	# skipped rather than failing the export - which matters because the whole
	# point of the offline harness is not needing Tally to be there.
	# ──────────────────────────────────────────────────────────────

	def _collection(self, collection_id, type_name, fetch):
		fields = "".join(f"<NATIVEMETHOD>{f}</NATIVEMETHOD>" for f in fetch)
		return f"""<ENVELOPE>
<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>{collection_id}</ID></HEADER>
<BODY><DESC><STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVCURRENTCOMPANY>{self.company or ""}</SVCURRENTCOMPANY>
</STATICVARIABLES>
<TDL><TDLMESSAGE><COLLECTION NAME="{collection_id}" ISMODIFY="No" ISFIXED="No">
<TYPE>{type_name}</TYPE>{fields}
</COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""

	def snapshot(self):
		"""What the destination company already holds, asked once a request.

		An export asks once and several sections may follow it; probing a Tally
		that is not there costs a timeout every time, and probing one that is
		costs ten round trips. Keyed on the host and port as well as the
		company, so a changed address is asked afresh.
		"""
		return _snapshot(self.host, self.port, self.company or "")

	def _take_snapshot(self):
		"""What the destination company already holds, as far as it will say.

		One read per master type. Each failure leaves that type **unknown**
		rather than empty, because `resolve.py` treats the two completely
		differently: unknown falls back to the old create-everything behaviour
		and says so, while empty means the destination genuinely has nothing
		and everything must be created.

		Never raises. A Tally that is switched off produces a snapshot that
		knows nothing, which is exactly what the offline harness needs.
		"""
		from tally_connector.tally.snapshot import (
			COLLECTIONS,
			ELEMENT,
			TallySnapshot,
			parse_companies,
			parse_masters,
		)

		snap = TallySnapshot(company=self.company or "")

		ok, response = self.test_connection()
		if ok:
			snap.companies = parse_companies((response or "").encode("utf-8"))
			if snap.companies is None:
				snap.unavailable["companies"] = "Tally's company list could not be read."
		else:
			# One question answers all of them. Asking the other nine
			# collections of a Tally that is not there costs a timeout each,
			# which turned an export on a site with no Tally from under a
			# second into most of a minute.
			snap.unavailable["companies"] = response or "Tally did not answer."
			for kind, *_ in COLLECTIONS:
				snap.unavailable[kind] = "Tally could not be reached."
			snap.unavailable["voucher_types"] = "Tally could not be reached."
			return snap

		for kind, collection_id, type_name, fields in COLLECTIONS:
			try:
				got, body = self._post_xml(
					self._collection(collection_id, type_name, fields), timeout=self.probe_timeout
				)
			except Exception as exc:
				snap.unavailable[kind] = str(exc)
				continue
			if not got:
				snap.unavailable[kind] = body or "Tally did not answer."
				continue
			parsed = parse_masters((body or "").encode("utf-8"), ELEMENT[type_name])
			if parsed is None:
				snap.unavailable[kind] = "Tally's answer could not be read."
				continue
			setattr(snap, kind, parsed)

		details = self.voucher_type_details()
		if details is None:
			snap.unavailable["voucher_types"] = "Tally did not answer."
		else:
			snap.voucher_types = details
		return snap

	def vouchers_in_period(self, from_date, to_date):
		"""The vouchers Tally holds in a period, asked as a collection.

		This is what makes a sync checkable. Tally's reply to an import says
		how many it created and, when it feels like it, why some failed - but
		never *which*. Asking afterwards says what is actually there, so the
		connector can name the documents that did not arrive instead of
		leaving somebody to find out from a trial balance.

		**It asked for the "Day Book" report until 20 September and never
		worked.** Against a live TallyPrime that request comes back as a
		company-info envelope - Tally's answer to a request it does not
		recognise - which parsed cleanly, held no VOUCHER element, and so read
		as *this company has no vouchers at all*. A confident empty from a
		question Tally never answered is the worst shape this can fail in: it
		is the reading under which every document sent is reported missing.
		The collection below is the shape Tally's own collection requests use
		elsewhere in this file, and it was measured returning real vouchers
		from the same company on the same connection.

		Returns a list of {number, kind, date, guid}, or None if Tally did not
		answer - unknown rather than empty, because "Tally has nothing" and
		"Tally did not say" lead to opposite conclusions.
		"""
		from lxml import etree

		def stamp(value):
			value = (value or "").strip()
			return value.replace("-", "") if value else ""

		xml = f"""<ENVELOPE>
<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>KXVouchers</ID></HEADER>
<BODY><DESC><STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVFROMDATE TYPE="Date">{stamp(str(from_date))}</SVFROMDATE>
<SVTODATE TYPE="Date">{stamp(str(to_date))}</SVTODATE>
<SVCURRENTCOMPANY>{self.company or ""}</SVCURRENTCOMPANY>
</STATICVARIABLES>
<TDL><TDLMESSAGE><COLLECTION NAME="KXVouchers" ISMODIFY="No">
<TYPE>Voucher</TYPE>
<FETCH>Date,VoucherTypeName,VoucherNumber,Guid</FETCH>
</COLLECTION></TDLMESSAGE></TDL>
</DESC></BODY></ENVELOPE>"""
		ok, body = self._post_xml(xml, timeout=self.timeout)
		if not ok or "<LINEERROR>" in (body or ""):
			return None
		try:
			root = etree.fromstring((body or "").encode("utf-8"), etree.XMLParser(recover=True))
		except Exception:
			return None
		if root is None:
			return None
		# Well-formed is not the same as ours. An HTML error page parses
		# perfectly and would otherwise read as a company with nothing in it,
		# which is the answer that makes the connector report every document
		# as missing. So does the company-info envelope Tally returns for a
		# request it does not recognise - which is exactly what the old Day
		# Book request got, every time, for as long as it existed.
		if root.tag != "ENVELOPE" and next(root.iter("ENVELOPE"), None) is None:
			return None
		if next(root.iter("COLLECTION"), None) is None:
			return None
		out = []
		for el in root.iter("VOUCHER"):
			number = (el.findtext("VOUCHERNUMBER") or "").strip()
			kind = (el.get("VCHTYPE") or el.findtext("VOUCHERTYPENAME") or "").strip()
			# Tally puts an empty voucher in every collection, including one
			# over a period with nothing in it. Counting it would turn a
			# genuinely empty company into one holding a document.
			if not number and not kind:
				continue
			out.append(
				{
					"number": number,
					"kind": kind,
					"date": (el.findtext("DATE") or "").strip(),
					"guid": (el.findtext("GUID") or "").strip(),
				}
			)
		return out

	def voucher_types(self):
		"""Names of the voucher types the target company has."""
		details = self.voucher_type_details()
		return set(details) if details else None

	def voucher_type_details(self):
		"""Voucher types, and whether each would catch a duplicate.

		A re-import is the case this answers. Tally has no upsert - an ordinary
		import creates a second voucher - and this is one of the two things that
		stop it: the voucher type numbered Manually with Prevent Duplicates set.
		That is a setting inside the target company, so it can only be asked
		for, never assumed. The other is the GUID the export now puts on every
		voucher, which does not depend on the destination at all.

		Returns {name: True | False | None}, where None means Tally answered
		without saying. The field names below are best-effort: they have not
		been checked against a live Tally, so an unrecognised answer reads as
		unknown and the check that uses it is skipped rather than guessing.
		"""
		ok, response = self._post_xml(
			self._collection(
				"VchTypes",
				"VoucherType",
				["Name", "NumberingMethod", "PreventDuplicates", "IsPreventDuplicate"],
			),
			timeout=self.probe_timeout,
		)
		if not ok:
			return None
		try:
			from lxml import etree

			root = etree.fromstring(response.encode("utf-8"), etree.XMLParser(recover=True))
			out = {}
			for el in root.iter("VOUCHERTYPE"):
				name = el.get("NAME") or (el.findtext("NAME") or "").strip()
				if not name:
					continue
				raw = (el.findtext("PREVENTDUPLICATES") or el.findtext("ISPREVENTDUPLICATE") or "").strip()
				out[name] = {"yes": True, "no": False}.get(raw.lower())
			if not out:
				out = {el.text.strip(): None for el in root.iter("NAME") if el.text and el.text.strip()}
			return out or None
		except Exception:
			return None

	def company_period(self):
		"""(books_from, period_to) for the target company, or (None, None)."""
		ok, response = self._post_xml(
			self._collection("Cmps", "Company", ["Name", "BooksFrom", "StartingFrom", "EndingAt"]),
			timeout=self.probe_timeout,
		)
		if not ok:
			return None, None
		try:
			import datetime

			from lxml import etree

			root = etree.fromstring(response.encode("utf-8"), etree.XMLParser(recover=True))

			def first(tag):
				for el in root.iter(tag):
					if el.text and el.text.strip():
						return el.text.strip()
				return None

			def parse(raw):
				if not raw:
					return None
				raw = raw.strip()
				if len(raw) == 8 and raw.isdigit():
					return datetime.date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
				for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
					try:
						return datetime.datetime.strptime(raw, fmt).date()
					except ValueError:
						continue
				return None

			return parse(first("BOOKSFROM") or first("STARTINGFROM")), parse(first("ENDINGAT"))
		except Exception:
			return None, None


# ──────────────────────────────────────────────────────────────────────────────
# Standalone helper used by the API layer
# ──────────────────────────────────────────────────────────────────────────────

# How many vouchers go in one request.
#
# Not a guess: a real TallyPrime raised a memory overflow on a single file of
# this connector's output. Tally holds the whole request in
# memory while it parses, so the ceiling is Tally's rather than the network's,
# and one file of several thousand vouchers is past it.
#
# Smaller batches also bound the damage. Tally stops at the first record it
# cannot accept and loses the rest of the file, so a bad voucher costs its own
# batch instead of everything after it.
# Masters go in far smaller batches than vouchers, and the reason is the size
# of the file rather than the count of records: a single masters push on this
# engagement was 79MB, and a live TallyPrime answered `Software Exception
# c0000005 (Memory Access Violation)` rather than refusing it.
MASTER_BATCH = 250

# The order Tally has to read them in. A child names its parent, so the parent's
# kind has to be finished before the child's kind starts - which is why this
# batches *within* a kind and never across two. Slicing a flat list of every
# master would put a stock item in the same file as the stock group it needs,
# or worse, in an earlier one.
MASTER_ORDER = ("groups", "units", "cost_centres", "godowns", "stock_groups", "stock_items", "ledgers")


def push_masters(
	company,
	masters,
	duplicates="",
	resolution=None,
	export_log_name=None,
	batch=MASTER_BATCH,
	tick=None,
	name_refusals=False,
):
	"""Send masters to Tally a batch at a time, and stop at the first failure.

	The same contract as `push_vouchers` and for the same reason: Tally aborts
	a file at the first record it refuses and loses the rest, so a run that
	carries on turns one clear failure into a guess about what is where. What
	differs is the ordering. Vouchers are independent of each other; masters
	are not, so each kind is finished before the next begins and a batch never
	spans two kinds.

	`resolution` is passed through to the renderer, which drops any master the
	destination already holds - so a batch of 250 can legitimately send nothing
	at all, and such a batch is skipped rather than posted. An empty file is a
	round trip that can only fail.

	`name_refusals` changes what happens at a refusal, and it exists because
	Tally will not say what it refused. A real batch answered *"0 errors and 1
	exceptions"* and named nothing at all, so the only way to learn which
	record it was is to send that batch again one master at a time. With it on,
	the failing batch is re-sent singly, every master Tally refuses is recorded
	**by name**, and the run carries on with the batches after it.

	Carrying on is safe here in a way it is not otherwise, and the difference
	is knowing: a stopped run leaves "something in that batch failed and the
	rest never went", which is a guess about what is where. This leaves an
	exact list. Nothing is dropped quietly - a refused master is in the summary
	with its name and Tally's words, because a party missing from Tally is a
	voucher that fails later with no clue why. The run still reports
	`success: False` when anything was refused.
	"""
	from tally_connector.tally.model import Masters
	from tally_connector.tally.render import render_masters

	summary = {
		"success": True,
		"sent": 0,
		"created": 0,
		"altered": 0,
		"ignored": 0,
		"batches": [],
		"errors": [],
		"stopped_at": None,
		"unknown": False,
		"skipped_empty": 0,
		"refused": [],
	}

	number = 0
	for kind in MASTER_ORDER:
		rows = list(getattr(masters, kind, ()) or ())
		for start in range(0, len(rows), batch):
			chunk = rows[start : start + batch]
			one = Masters(applicable_from=masters.applicable_from)
			setattr(one, kind, chunk)
			xml = render_masters(company, one, duplicates, resolution)

			# The resolution can empty a batch completely. Posting nothing is a
			# round trip with no upside and a real downside, since Tally has
			# already fallen over once on this company.
			carried = _masters_in(xml)
			if not carried:
				summary["skipped_empty"] += 1
				if tick:
					tick(len(chunk))
				continue

			number += 1
			result = push_to_tally(xml, export_log_name)
			summary["batches"].append(
				{
					"batch": number,
					"kind": kind,
					"masters": len(chunk),
					"first": getattr(chunk[0], "name", ""),
					"last": getattr(chunk[-1], "name", ""),
					"bytes": len(xml),
					"created": result.get("created", 0),
					"altered": result.get("altered", 0),
					"ignored": result.get("ignored", 0),
					"success": bool(result.get("success")),
					"unknown": bool(result.get("unknown")),
					"errors": result.get("errors") or [],
				}
			)
			if tick:
				tick(len(chunk))
			if result.get("success"):
				summary["sent"] += carried
				summary["created"] += result.get("created", 0)
				summary["altered"] += result.get("altered", 0)
				summary["ignored"] += result.get("ignored", 0)
				continue

			summary["success"] = False
			summary["unknown"] = bool(result.get("unknown"))
			summary["errors"] = result.get("errors") or []
			# Bisecting is for a refusal, never for an answer that could not
			# be read. An `unknown` means Tally fell over or said something
			# unreadable, and sending it another 250 requests one at a time is
			# how a wounded Tally is finished off - which is the failure this
			# whole batching exists to avoid. Stop, as before.
			if not name_refusals or result.get("unknown"):
				summary["stopped_at"] = number
				return summary

			# Tally named nothing, so ask it about each one on its own.
			for row in chunk:
				alone = Masters(applicable_from=masters.applicable_from)
				setattr(alone, kind, [row])
				lone = render_masters(company, alone, duplicates, resolution)
				if not _masters_in(lone):
					continue
				answer = push_to_tally(lone, export_log_name)
				if answer.get("success"):
					summary["sent"] += 1
					summary["created"] += answer.get("created", 0)
					summary["altered"] += answer.get("altered", 0)
					summary["ignored"] += answer.get("ignored", 0)
					continue
				summary["refused"].append(
					{
						"kind": kind,
						"name": getattr(row, "name", ""),
						"parent": getattr(row, "parent", ""),
						"unknown": bool(answer.get("unknown")),
						"error": "; ".join(answer.get("errors") or [])[:400],
					}
				)

	return summary


def _masters_in(xml) -> int:
	"""How many masters this file actually creates or alters.

	Counted off the message element the renderer writes per master rather than
	off the length of the string, because an envelope with no masters in it is
	still several hundred bytes of header - and rather than off the chunk,
	because the renderer drops what the destination already holds and what
	Tally could not name. Counting the chunk made `sent` a number of records
	considered rather than records sent, which is not what anybody reads it as.
	"""
	return xml.count("<TALLYMESSAGE")


VOUCHER_BATCH = 200

# How hard to try before concluding Tally did not answer a probe.
PROBE_TRIES = 3
PROBE_PAUSE = 2


def push_vouchers(
	company,
	vouchers,
	action="Create",
	export_log_name=None,
	batch=VOUCHER_BATCH,
	tick=None,
	no_cost_centres=frozenset(),
	with_guid=False,
	erpnext_company=None,
	keep_going=False,
	period=None,
	round_to_tally=False,
):
	"""Send vouchers to Tally a batch at a time, and stop at the first failure.

	Returns a summary over every batch, plus the batches themselves so a caller
	can say which vouchers are in Tally and which never left. **Stopping
	matters more than finishing**: Tally aborts a file at the first record it
	refuses, so the batch that failed is partly imported and every later batch
	would land on a company whose masters or dates are already wrong. Carrying
	on turns one clear failure into a guess about what is where.

	**Each batch is written to the sync ledger as it lands.** `record_sent`
	existed, was tested, and had no caller anywhere - so nothing was ever
	recorded, `unsent_vouchers` went on returning every voucher for ever, and a
	second run would have posted all of them into the customer's books a second
	time. That is the one failure this connector must not have, and it was one
	retry away. It is written per batch rather than at the end because a run
	that stops at batch nine must still know that batches one to eight are in
	Tally.

	`erpnext_company` is what the sync ledger keys on. Without it there is
	nothing to record against, so the recording is skipped and says so rather
	than writing rows under a blank company that `unsent_vouchers` would never
	match.
	"""
	from tally_connector.tally.render import render_vouchers
	from tally_connector.tally.rounding import attempts

	vouchers = list(vouchers)
	summary = {
		"success": True,
		"sent": 0,
		"created": 0,
		"altered": 0,
		"batches": [],
		"errors": [],
		"stopped_at": None,
		"unknown": False,
		"refused": [],
		"rounded": [],
	}
	if not vouchers:
		return summary

	for start in range(0, len(vouchers), batch):
		chunk = vouchers[start : start + batch]
		number = start // batch + 1
		xml = render_vouchers(company, chunk, action, with_guid, no_cost_centres)
		# Recorded as pending before the request leaves, so a run that dies
		# between sending and hearing back leaves rows saying "we began this"
		# rather than no rows at all. Pending is not resendable, which is what
		# stops the next run posting them a second time.
		_note_pending(chunk, erpnext_company, company)
		result = push_to_tally(xml, export_log_name)
		summary["batches"].append(
			{
				"batch": number,
				"vouchers": len(chunk),
				"first": getattr(chunk[0], "number", ""),
				"last": getattr(chunk[-1], "number", ""),
				"created": result.get("created", 0),
				"altered": result.get("altered", 0),
				"success": bool(result.get("success")),
				"unknown": bool(result.get("unknown")),
				"errors": result.get("errors") or [],
			}
		)
		if tick:
			tick(len(chunk))
		if result.get("success"):
			summary["sent"] += len(chunk)
			summary["created"] += result.get("created", 0)
			summary["altered"] += result.get("altered", 0)
			_record_batch(chunk, erpnext_company, company, result)
			continue

		summary["success"] = False
		summary["unknown"] = bool(result.get("unknown"))
		summary["errors"] = result.get("errors") or []

		# An answer that could not be read always stops the run. Tally may
		# have fallen over, and carrying on into one that is half dead is how
		# the worst of this went wrong before.
		if result.get("unknown") or not keep_going or not period:
			summary["stopped_at"] = number
			_record_batch(chunk, erpnext_company, company, result)
			break

		# Tally aborts a file at the record it refused and keeps what it read
		# before that, so part of this batch is in the books. Which part is
		# not a guess to make: ask Tally, record what landed as landed and the
		# rest as refused, and carry on with the batches after it. Without the
		# asking this cannot continue at all, because a retry would post the
		# ones that did land a second time.
		landed, refused = _split_by_what_tally_holds(chunk, company, period)
		if landed is None:
			summary["stopped_at"] = number
			_record_batch(chunk, erpnext_company, company, result)
			break
		if landed:
			summary["sent"] += len(landed)
			summary["created"] += len(landed)
			_record_batch(
				landed,
				erpnext_company,
				company,
				{
					"success": True,
					"reached": True,
					"created": len(landed),
					"altered": 0,
					"unknown": False,
					"errors": [],
				},
			)
		if refused and round_to_tally:
			# Tally computes the invoice's value itself and refuses a party
			# line that does not match, without ever saying what it wanted.
			# The connector adopts Tally's figure and books
			# the difference to the rounding ledger, so each refusal is tried
			# again a paisa either way. Bounded, balanced, and every voucher
			# that took an adjustment is named.
			still, adjusted = [], []
			for voucher in refused:
				taken = None
				for variant, paise in attempts(voucher):
					if not paise:
						continue  # the original has already been refused
					lone = render_vouchers(company, [variant], action, with_guid, no_cost_centres)
					answer = push_to_tally(lone, export_log_name)
					if answer.get("success"):
						taken = (variant, paise)
						break
					if answer.get("unknown"):
						break
				if taken is None:
					still.append(voucher)
					continue
				variant, paise = taken
				adjusted.append(variant)
				summary["sent"] += 1
				summary["created"] += 1
				summary["rounded"].append(
					{
						"number": getattr(voucher, "number", ""),
						"document": getattr(voucher, "remote_id", "") or getattr(voucher, "number", ""),
						"paise": str(paise),
					}
				)
			if adjusted:
				_record_batch(
					adjusted,
					erpnext_company,
					company,
					{
						"success": True,
						"reached": True,
						"created": len(adjusted),
						"altered": 0,
						"unknown": False,
						"errors": [],
					},
				)
			refused = still

		if refused:
			_record_batch(refused, erpnext_company, company, result)
			summary["refused"].extend(
				{
					"number": getattr(v, "number", ""),
					"kind": getattr(v, "kind", ""),
					"document": getattr(v, "remote_id", "") or getattr(v, "number", ""),
					"batch": number,
				}
				for v in refused
			)

	return summary


def _split_by_what_tally_holds(chunk, tally_company, period):
	"""Of this batch, which vouchers are in Tally and which are not.

	Returns (landed, refused), or (None, None) when Tally did not answer -
	unknown rather than empty, because reading a failed probe as "none of them
	landed" would send the whole batch again and duplicate whatever did.

	**It asks about this batch's own dates, not the whole period.** Asking for
	the year meant a read that grew with every voucher that landed, run after
	every refused batch - on a run with a 43% refusal rate that is hundreds of
	full-year Day Book reads, each bigger than the last, until one takes longer
	than the timeout and stops a push that was otherwise working. A batch spans
	a day or two, and that read stays the same size all night.

	Matching is by voucher number, which is Tally's own answer about what it
	holds. A voucher type numbering automatically discards ours and cannot be
	matched back, so on such a type this returns unknown rather than guessing.
	"""
	dates = sorted(str(getattr(v, "date", "")) for v in chunk if getattr(v, "date", None))
	window = (dates[0], dates[-1]) if dates else period

	# Asked more than once before giving up. Tally is busy in the moment after
	# it has refused a file, and a single probe that times out there used to
	# stop the whole run - on a push with a 43% refusal rate this probe is
	# reached constantly, so one hiccup ended everything. Three runs stopped at
	# batches 32, 1 and 2 for exactly this, with 24,000 vouchers still in
	# ERPNext. Unknown after several tries still stops, because it still means
	# nobody knows what landed.
	held = None
	for attempt in range(PROBE_TRIES):
		try:
			held = TallyConnector().vouchers_in_period(*window)
		except Exception:
			held = None
		if held is not None:
			break
		if attempt + 1 < PROBE_TRIES:
			time.sleep(PROBE_PAUSE)
	if held is None:
		return None, None
	have = {row.get("number") for row in held if row.get("number")}
	landed = [v for v in chunk if getattr(v, "number", "") in have]
	refused = [v for v in chunk if getattr(v, "number", "") not in have]
	return landed, refused


def _note_pending(chunk, erpnext_company, tally_company):
	"""Write the attempt before the push, and never fail the push over it."""
	if not erpnext_company:
		return
	try:
		import frappe

		from tally_connector.utils.sync_ledger import note_pending

		note_pending(chunk, erpnext_company, tally_company)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit - a voucher is on record before it is sent
	except Exception:
		try:
			import frappe

			frappe.log_error(frappe.get_traceback(), "Tally Connector: sync ledger pending")
		except Exception:
			pass


def _record_batch(chunk, erpnext_company, tally_company, result):
	"""Write one batch's outcome to the sync ledger, and never fail the push.

	A push that reached Tally has reached it whatever happens here, so a
	bookkeeping error must not be reported as a failed send - that is the
	reading under which somebody retries and duplicates. It is logged instead.
	"""
	if not erpnext_company:
		return
	try:
		import frappe

		from tally_connector.utils.sync_ledger import record_sent

		record_sent(chunk, erpnext_company, tally_company, result)
		# Committed per batch, because the whole point of recording per batch
		# is surviving the run that does not finish. A push of 26,657 vouchers
		# was stopped part way and the ledger had written nothing: the rows
		# were there in the transaction and went with it, leaving 2,503
		# vouchers in Tally that nothing knew about. A record that only exists
		# if the process exits cleanly is not a record of what was sent.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit - what was sent is on record even if the process dies
	except Exception:
		try:
			import frappe

			frappe.log_error(frappe.get_traceback(), "Tally Connector: sync ledger write")
		except Exception:
			pass


def push_to_tally(xml_string, export_log_name=None):
	"""Send XML to Tally and record what Tally actually said about it.

	Returns `success`, `reached`, `unknown`, `created`, `altered`, `ignored`,
	`last_voucher_id`, `errors` and the raw `response`. `success` means Tally
	wrote what was sent; `unknown` means the request reached Tally and its
	answer could not be read, which is the one state that must never be retried
	blindly, because Tally may hold the vouchers already.
	"""
	conn = TallyConnector()
	reached, response = conn.push_xml(xml_string)

	parsed = (
		conn.parse_tally_response(response)
		if reached
		else {
			"created": 0,
			"altered": 0,
			"ignored": 0,
			"error_count": 0,
			"exceptions": 0,
			"last_voucher_id": "",
			"parsed": False,
			"errors": [response],
			"raw": response,
		}
	)

	# Three different things, told apart because they need different answers.
	#
	# `reached` - the transport worked. It says nothing about the data.
	# `parsed`  - Tally's answer was XML we could read. An answer we could not
	#             read leaves what happened genuinely unknown, and a timeout is
	#             the case that matters: Tally may have imported everything.
	# `wrote`   - Tally says it created or altered something.
	#
	# A push is a success only when all three hold and nothing was refused.
	# This used to be `success = the HTTP call returned`, which reported an
	# import Tally rejected line by line as a completed export.
	errors = list(parsed.get("errors") or [])
	wrote = parsed.get("created", 0) + parsed.get("altered", 0)
	unknown = reached and not parsed.get("parsed")
	success = bool(reached and parsed.get("parsed") and not errors)

	if success and not wrote and xml_string.count("<TALLYMESSAGE") > 0:
		# Tally answered cleanly and wrote nothing. The usual cause is
		# SVCURRENTCOMPANY not matching a company Tally has open, which is a
		# silent no-op that used to be reported as a completed export.
		success = False
		errors.append(
			"Tally accepted the request and created or altered nothing. The "
			"company name in settings most likely does not match a company "
			"open in Tally."
		)

	result = {
		"success": success,
		"reached": reached,
		"unknown": unknown,
		"response": response,
		"created": parsed.get("created", 0),
		"altered": parsed.get("altered", 0),
		"ignored": parsed.get("ignored", 0),
		"last_voucher_id": parsed.get("last_voucher_id", ""),
		"errors": errors,
	}

	if export_log_name:
		try:
			log = frappe.get_doc("Tally Export Log", export_log_name)
			# Appended rather than assigned: a full export pushes twice against
			# one log, and the masters answer used to be overwritten by the
			# vouchers answer, so the half that failed was the half nobody
			# could read afterwards.
			previous = log.tally_response or ""
			log.tally_response = (previous + ("\n\n" if previous else "") + (response or ""))[-20000:]
			if success:
				log.status = "Success"
			elif unknown:
				# Not a failure. Nobody knows, and saying "Failed" invites the
				# retry that duplicates the vouchers Tally may already hold.
				log.status = "Partial"
				log.error_log = "\n".join(errors)[:4000]
			elif reached:
				log.status = "Partial" if wrote else "Failed"
				log.error_log = "\n".join(errors)[:4000]
			else:
				log.status = "Failed"
				log.error_log = (response or "")[:4000]
			log.save(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit - the log outlives a failed push
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Tally Connector: Export Log update failed")

	return result


@request_cache
def _snapshot(host, port, company):
	"""One snapshot per destination per request."""
	conn = TallyConnector()
	conn.host, conn.port, conn.company = host, port, company
	conn.base_url = f"http://{host}:{port}"
	return conn._take_snapshot()
