"""
One file that carries an export to the PC running Tally.

An export is two files that have to be imported in a fixed order, and a year of
vouchers is tens of megabytes of XML. Moving that by email, chat or a USB stick
loses the order, invites a half-copied file, and leaves nobody able to say
afterwards whether what was imported is what was exported.

The package answers all three:

- **Small.** XML compresses to a fraction of its size, so a year of vouchers
  travels as a single attachment.
- **Ordered.** The masters file comes first and the vouchers follow in date
  order, one file per month, each named with its place in the sequence. A month
  larger than `MAX_VOUCHERS_PER_FILE` is split into parts, because Tally holds a
  whole file in memory while it imports and a very large one is how a live
  TallyPrime came to answer with a memory access violation.
- **Checkable.** `manifest.json` lists every file with its size, its SHA-256
  and how many records it carries, and `verify` holds a package against its own
  manifest. A second import of the same package can be told from an import of a
  different export by the package id, and a file damaged in transit by its hash.

TallyPrime does not import from a zip, so the package is extracted first; the
README inside says so, in the order the files go.

Splitting moves vouchers and never re-renders them. Each voucher's TALLYMESSAGE
is taken from the single file as it was written and put into its month's file
under the same request header, so the package holds exactly what the single file
held and was validated as.

Pure: no frappe.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import uuid
import zipfile
from collections import defaultdict

from lxml import etree

PACKAGE_FORMAT = "tally_connector.package"
PACKAGE_VERSION = 1

MANIFEST = "manifest.json"
README = "README.txt"

# A file Tally can take in one go. Pushes go in batches of 200; a file imported
# by hand can be larger, and this keeps it well under what has failed before.
MAX_VOUCHERS_PER_FILE = 5000

XML, EXCEL = "Tally XML", "Excel"

UNDATED = "undated"


def _sha256(data: bytes) -> str:
	return hashlib.sha256(data).hexdigest()


def _month(message) -> str:
	"""The month a voucher belongs to, as YYYY-MM, read off its own date."""
	voucher = message.find("VOUCHER")
	raw = (voucher.findtext("DATE") if voucher is not None else "") or ""
	raw = raw.strip()
	if len(raw) == 8 and raw.isdigit():
		return f"{raw[:4]}-{raw[4:6]}"
	return UNDATED


def split_vouchers(xml: bytes, limit: int = MAX_VOUCHERS_PER_FILE) -> list[dict]:
	"""The voucher file as one file per month, in date order.

	Returns a list of {"period", "part", "records", "first", "last",
	"content"}. Every message is moved as parsed and every file keeps the
	original request header, so a voucher reads in Tally exactly as it did in
	the single file.
	"""
	parser = etree.XMLParser(remove_blank_text=False, huge_tree=True)
	root = etree.fromstring(xml, parser)
	requestdata = root.find("BODY/IMPORTDATA/REQUESTDATA")
	if requestdata is None:
		raise ValueError("That is not a Tally voucher import: it has no REQUESTDATA.")

	messages = list(requestdata)
	by_month = defaultdict(list)
	for message in messages:
		by_month[_month(message)].append(message)
	for message in messages:
		requestdata.remove(message)

	files = []
	for period in sorted(by_month, key=lambda p: (p == UNDATED, p)):
		group = by_month[period]
		parts = [group[i : i + limit] for i in range(0, len(group), limit)] or [[]]
		for number, chunk in enumerate(parts, start=1):
			requestdata[:] = chunk
			content = etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
			files.append(
				{
					"period": period,
					"part": number if len(parts) > 1 else 0,
					"records": len(chunk),
					"first": _first_last(chunk)[0],
					"last": _first_last(chunk)[1],
					"content": content,
				}
			)
	return files


def _first_last(messages):
	dates = sorted(
		(m.find("VOUCHER").findtext("DATE") or "").strip()
		for m in messages
		if m.find("VOUCHER") is not None and (m.find("VOUCHER").findtext("DATE") or "").strip()
	)
	if not dates:
		return "", ""
	return _iso(dates[0]), _iso(dates[-1])


def _iso(raw):
	return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if len(raw) == 8 and raw.isdigit() else raw


def _count(xml: bytes) -> int:
	return xml.count(b"<TALLYMESSAGE")


def _voucher_name(order, period, part, ext):
	stem = f"{order:02d}_vouchers_{period}"
	if part:
		stem += f"_part{part}"
	return f"{stem}.{ext}"


def build(masters: bytes, vouchers: bytes, meta: dict, fmt: str = XML) -> tuple[bytes, dict]:
	"""The package as zip bytes, and its manifest.

	`meta` carries what the manifest says about the export: the ERPNext and
	Tally company, the period, the app version. Nothing in it is read back here.

	An Excel export is packaged as its two workbooks, unsplit: a workbook is a
	run of rows the renderer writes in one piece, and splitting it would mean a
	second Excel writer that could disagree with the first.
	"""
	ext = "xlsx" if fmt == EXCEL else "xml"
	entries = [
		{
			"name": f"01_masters.{ext}",
			"kind": "masters",
			"order": 1,
			"content": masters,
			"records": _count(masters) if fmt != EXCEL else None,
		}
	]
	if fmt == EXCEL:
		entries.append(
			{
				"name": f"02_vouchers.{ext}",
				"kind": "vouchers",
				"order": 2,
				"content": vouchers,
				"records": None,
			}
		)
	elif _count(vouchers):
		for order, part in enumerate(split_vouchers(vouchers), start=2):
			entries.append(
				{
					"name": _voucher_name(order, part["period"], part["part"], ext),
					"kind": "vouchers",
					"order": order,
					"period": part["period"],
					"from": part["first"],
					"to": part["last"],
					"content": part["content"],
					"records": part["records"],
				}
			)

	manifest = {
		"format": PACKAGE_FORMAT,
		"version": PACKAGE_VERSION,
		"id": str(uuid.uuid4()),
		"created_at": datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat(),
		"export_format": fmt,
		**{k: v for k, v in meta.items() if v not in (None, "")},
		"files": [
			{
				**{k: v for k, v in entry.items() if k != "content" and v is not None},
				"bytes": len(entry["content"]),
				"sha256": _sha256(entry["content"]),
			}
			for entry in entries
		],
	}
	if fmt != EXCEL:
		manifest["totals"] = {
			"masters": entries[0]["records"],
			"vouchers": sum(e["records"] or 0 for e in entries[1:]),
		}

	buffer = io.BytesIO()
	with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
		archive.writestr(README, readme(manifest))
		archive.writestr(MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False))
		for entry in entries:
			archive.writestr(entry["name"], entry["content"])
	return buffer.getvalue(), manifest


def readme(manifest: dict) -> str:
	"""The instructions that travel with the files."""
	files = manifest["files"]
	lines = [
		"Tally import package",
		"====================",
		"",
		f"Package id:  {manifest['id']}",
		f"Created:     {manifest['created_at']}",
	]
	if manifest.get("tally_company"):
		lines.append(f"Company:     {manifest['tally_company']}")
	if manifest.get("from_date") or manifest.get("to_date"):
		lines.append(f"Period:      {manifest.get('from_date', '')} to {manifest.get('to_date', '')}")
	lines += [
		"",
		"TallyPrime does not import from a zip. Extract this package first, then",
		"import the files into the company named above, one at a time and in this",
		"order. A voucher that names a ledger, unit or item Tally does not yet hold",
		"is refused, which is why the masters come first.",
		"",
	]
	for entry in files:
		what = "Masters" if entry["kind"] == "masters" else "Transactions"
		detail = []
		if entry.get("records") is not None:
			detail.append(f"{entry['records']} records")
		if entry.get("from"):
			detail.append(f"{entry['from']} to {entry['to']}")
		suffix = f"  ({', '.join(detail)})" if detail else ""
		lines.append(f"  {entry['order']:>2}. {entry['name']}  - Import > {what}{suffix}")
	lines += [
		"",
		"After each file, check Tally's import summary: a file is finished only",
		"when it reports no errors. Stop at the first file with errors and fix",
		"that before importing the next.",
		"",
		"Importing a file twice creates its vouchers twice unless Tally's",
		'"Overwrite voucher when a voucher with same GUID exists" is set to Yes.',
		"",
		f"{MANIFEST} lists every file with its SHA-256. To check a file on Windows:",
		"  certutil -hashfile <file name> SHA256",
		"",
	]
	return "\n".join(lines)


def verify(package: bytes) -> list[str]:
	"""What is wrong with a package, held against its own manifest.

	An empty list means every file the manifest names is present, the size and
	hash of each match, and nothing is in the package that the manifest does not
	account for.
	"""
	problems = []
	try:
		archive = zipfile.ZipFile(io.BytesIO(package))
	except zipfile.BadZipFile:
		return ["This is not a zip file, or it is damaged."]
	with archive:
		names = set(archive.namelist())
		if MANIFEST not in names:
			return [f"The package has no {MANIFEST}."]
		try:
			manifest = json.loads(archive.read(MANIFEST))
		except ValueError:
			return [f"{MANIFEST} is not valid JSON."]
		if manifest.get("format") != PACKAGE_FORMAT:
			problems.append(f"{MANIFEST} does not describe a Tally import package.")
		listed = set()
		for entry in manifest.get("files", []):
			name = entry.get("name", "")
			listed.add(name)
			if name not in names:
				problems.append(f"{name} is listed in the manifest and missing from the package.")
				continue
			content = archive.read(name)
			if len(content) != entry.get("bytes"):
				problems.append(f"{name} is {len(content)} bytes; the manifest says {entry.get('bytes')}.")
			if _sha256(content) != entry.get("sha256"):
				problems.append(f"{name} does not match its SHA-256 in the manifest.")
		for extra in sorted(names - listed - {MANIFEST, README}):
			problems.append(f"{extra} is in the package and not in the manifest.")
	return problems
