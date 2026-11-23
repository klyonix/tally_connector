"""
Asking Tally what became of each record it was sent.

Tally answers an import with totals - created, altered, errors - and a line of
text per refusal that names no record. So when a batch of 200 vouchers comes
back with one error, nothing in the answer says which voucher it was, and the
connector has to ask Tally afterwards what it holds.

TallyPrime lets an import carry a **response report**: a small TDL definition,
sent inline with the request, that is added to the import file definition and
runs on Tally's import events. This module writes one. After each object Tally
imports, it records the object's kind, its name or voucher number, the import
action Tally took and the error if Tally refused it, and for a voucher it also
records the master id and GUID Tally gave it. The report then prints one RESULT
per object, which `parse` reads back.

**Unverified against a live Tally.** The mechanism is documented by Tally, and
an open-source library uses it, but this TDL has not been run on a TallyPrime by
this project. It is behind the `per_record_results` setting, off by default, and
everything that reads its answer falls back to today's behaviour when the
answer is not there:

- `parse` returns None unless the response carries the report, so an answer
  without it is read the way every answer is read now;
- `complete` trusts the results only when they name every record that was
  sent. A partial list is a list somebody could act on wrongly, so it is
  treated as no list at all.

The request this builds is Tally's versioned XML form (VERSION 1, TALLYREQUEST
Import, TYPE Data), which is the form that carries inline TDL. The records in it
are moved from the ordinary import untouched.

Pure: no frappe.
"""

from __future__ import annotations

from lxml import etree

REPORT = "KX Import Results"
OPTION = "KX Import Results"
RESULTS_TAG = "KXRESULTS"
RESULT_TAG = "KXRESULT"

# Tally's own list variable, reused rather than declared, because inline TDL
# sent with a request has no system section to declare one in. Its members are
# strings, which is all this stores. The mapping is ours.
LIST = "CCTotalMisMatch"
MEMBERS = {
	"kind": "VTMark",
	"key": "CCLedName",
	"action": "CCCatName",
	"master_id": "VchMID",
	"guid": "VchDate",
	"error": "VchNumber",
}

BEGIN = "KX Import Results Begin"
RECORD = "KX Import Results Record"


def _sub(parent, tag, text=None, **attrib):
	el = etree.SubElement(parent, tag, **attrib)
	if text is not None:
		el.text = text
	return el


def _tdl(import_file: str):
	"""The TDL message: the option, the events, the report and its collection."""
	message = etree.Element("TDLMESSAGE")

	# Switch our option on for the import file this request uses.
	base = _sub(message, "IMPORTFILE", NAME=import_file, ISMODIFY="Yes")
	_sub(base, "ADD", f"Option : {OPTION} : Yes")

	option = _sub(message, "IMPORTFILE", NAME=OPTION, ISOPTION="Yes")
	_sub(option, "RESPONSEREPORT", REPORT)
	_sub(option, "ADD", f"On : Start Import : Yes : Call : {BEGIN}")
	_sub(option, "ADD", f"On : After Import Object : Yes : Call : {RECORD}")

	begin = _sub(message, "FUNCTION", NAME=BEGIN)
	_sub(begin, "ACTION", f"01 : LISTDELETE : {LIST}")

	record = _sub(message, "FUNCTION", NAME=RECORD)
	for declaration in (
		"KXKind : String : $$Type",
		'KXKey : String : if $$Type = "Voucher" then $VoucherNumber else $Name',
		"KXAction : String : $$ImportAction",
		"KXError : String : $$LastImportError",
	):
		_sub(record, "VARIABLE", declaration)
	actions = [
		f"01 : LISTADD : {LIST} : ##KXKey : ##KXKind : {MEMBERS['kind']}",
		f"02 : LISTADD : {LIST} : ##KXKey : ##KXKey : {MEMBERS['key']}",
		f"03 : LISTADD : {LIST} : ##KXKey : ##KXAction : {MEMBERS['action']}",
		f"04 : LISTADD : {LIST} : ##KXKey : ##KXError : {MEMBERS['error']}",
		# A voucher Tally took is looked up so its master id and GUID can be
		# reported. The number is checked after the lookup, because the last
		# voucher Tally created is not this one when this one was an
		# alteration, and a wrong id is worse than none.
		'05 : IF : ##KXKind = "Voucher" AND $$IsEmpty:##KXError',
		"06 : SET OBJECT : (Voucher, $$LastCreatedVchId).",
		"07 : IF : $VoucherNumber = ##KXKey",
		f"08 : LISTADD : {LIST} : ##KXKey : $MasterId : {MEMBERS['master_id']}",
		f"09 : LISTADD : {LIST} : ##KXKey : $GUID : {MEMBERS['guid']}",
		"10 : ENDIF",
		"11 : ENDIF",
	]
	for action in actions:
		_sub(record, "ACTION", action)

	report = _sub(message, "REPORT", NAME=REPORT)
	_sub(report, "FORMS", REPORT)
	form = _sub(message, "FORM", NAME=REPORT)
	_sub(form, "PARTS", REPORT)
	_sub(form, "XMLTAG", RESULTS_TAG)
	part = _sub(message, "PART", NAME=REPORT)
	_sub(part, "LINES", REPORT)
	_sub(part, "REPEAT", f"{REPORT} : {REPORT}")
	_sub(part, "SCROLLED", "Vertical")
	line = _sub(message, "LINE", NAME=REPORT)
	_sub(line, "FIELDS", ", ".join(f"{REPORT} {name}" for name in MEMBERS))
	_sub(line, "XMLTAG", RESULT_TAG)
	for name, member in MEMBERS.items():
		field = _sub(message, "FIELD", NAME=f"{REPORT} {name}")
		_sub(field, "SET", f"${member}")
		_sub(field, "XMLTAG", name.upper().replace("_", ""))
	collection = _sub(message, "COLLECTION", NAME=REPORT)
	_sub(collection, "DATASOURCE", f"Variable : {LIST}")
	return message


def with_results(xml: str) -> str:
	"""This import, as a request that also asks Tally for a result per record.

	`xml` is an ordinary import as the renderer writes it. The company, the
	duplicate handling and every record are carried over unchanged; only the
	envelope around them changes.
	"""
	root = etree.fromstring(xml.encode("utf-8"), etree.XMLParser(huge_tree=True))
	desc = root.find("BODY/IMPORTDATA/REQUESTDESC")
	data = root.find("BODY/IMPORTDATA/REQUESTDATA")
	if desc is None or data is None:
		raise ValueError("That is not a Tally import: it has no REQUESTDESC and REQUESTDATA.")
	import_file = (desc.findtext("REPORTNAME") or "").strip()

	envelope = etree.Element("ENVELOPE")
	header = _sub(envelope, "HEADER")
	_sub(header, "VERSION", "1")
	_sub(header, "TALLYREQUEST", "Import")
	_sub(header, "TYPE", "Data")
	_sub(header, "ID", import_file)
	body = _sub(envelope, "BODY")
	new_desc = _sub(body, "DESC")
	static = desc.find("STATICVARIABLES")
	if static is not None:
		new_desc.append(static)
	tdl = _sub(new_desc, "TDL")
	tdl.append(_tdl(import_file))
	new_data = _sub(body, "DATA")
	for message in list(data):
		new_data.append(message)
	return etree.tostring(envelope, pretty_print=True, xml_declaration=True, encoding="UTF-8").decode("utf-8")


def parse(response: str) -> list[dict] | None:
	"""Tally's result per record, or None when the answer does not carry them.

	Each result is {"kind", "key", "action", "master_id", "guid", "error"}.
	None is not an empty list: an answer without the report is one this
	module knows nothing about, and the caller reads it the ordinary way.
	"""
	if not response or RESULTS_TAG not in response:
		return None
	try:
		root = etree.fromstring(response.encode("utf-8"), etree.XMLParser(recover=True, huge_tree=True))
	except etree.XMLSyntaxError:
		return None
	if root is None:
		return None
	block = root if root.tag == RESULTS_TAG else next(root.iter(RESULTS_TAG), None)
	if block is None:
		return None
	out = []
	for el in block.iter(RESULT_TAG):
		row = {name: (el.findtext(name.upper().replace("_", "")) or "").strip() for name in MEMBERS}
		if not row["key"]:
			continue
		out.append(row)
	return out


def normalise(value) -> str:
	"""A key the way Tally compares names: case and spacing aside."""
	return " ".join((value or "").split()).casefold()


def complete(results: list[dict] | None, keys) -> dict | None:
	"""The results by record, if and only if they account for every record sent.

	Returns {normalised key: result}, or None. Tally compares names without
	regard to case, so the keys are compared the same way.
	"""
	if results is None:
		return None
	by_key = {normalise(row["key"]): row for row in results}
	wanted = {normalise(k) for k in keys if k}
	if not wanted or not wanted <= set(by_key):
		return None
	return {k: by_key[k] for k in wanted}


def landed(result: dict) -> bool:
	"""Whether Tally took this record."""
	return not result.get("error")


def altered(result: dict) -> bool:
	"""Whether Tally altered an existing record rather than creating one."""
	return "alter" in (result.get("action") or "").casefold()


def keys_in(xml: str) -> list[str]:
	"""The name or voucher number of every record in an ordinary import."""
	root = etree.fromstring(xml.encode("utf-8"), etree.XMLParser(huge_tree=True))
	keys = []
	for message in root.iter("TALLYMESSAGE"):
		for record in message:
			if record.tag == "VOUCHER":
				keys.append((record.findtext("VOUCHERNUMBER") or "").strip())
			else:
				keys.append((record.get("NAME") or record.findtext("NAME") or "").strip())
	return keys
