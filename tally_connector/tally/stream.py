"""
A Tally export, opened as something a strict XML parser can read, in pieces.

Tally's own exports are not well-formed XML, and a real one taught the two
things this module exists for.

**Tally writes characters XML 1.0 forbids.** It prefixes its system values with
`&#4;` - `&#4; Not Applicable`, `&#4; Unknown` - 50,193 times in one company's
masters, and a transactions file carried 1,159 raw control characters besides.
A strict parser stops at the first, at line 49. Python's own `ElementTree`,
which is what a first attempt at an importer reaches for, cannot read the file
at all.

**Parsing leniently instead corrupts it, silently.** lxml's `recover=True` reads
the whole file - and after the first bad reference it drops later entities, so
`Duties &amp; Taxes` came out as `Duties  Taxes`. Sixty-seven ledgers named by
vouchers then failed to match their own masters, where the true number is ten.
A parser that invents a mismatch is worse than one that stops.

So the forbidden characters are removed from the text **before** a strict parse,
and nothing else is touched. It streams: a year of vouchers is hundreds of
megabytes as UTF-16, and 6 MB as a zip, which is how it arrives - Frappe refuses
an upload over 25 MB, and the zip is read without being unpacked to disk.

Pure: no frappe.
"""

from __future__ import annotations

import codecs
import io
import re
import zipfile
from collections import Counter

from lxml import etree

# Numeric character references to code points XML 1.0 does not allow: C0
# controls other than tab, line feed and carriage return. Decimal and hex.
FORBIDDEN_REFERENCE = re.compile(
	r"&#(?:[xX]0*(?:[0-8bBcCeEfF]|1[0-9a-fA-F])|0*(?:[0-8]|1[124-9]|2[0-9]|3[01]));"
)
# The same code points, written raw.
FORBIDDEN_CHARACTER = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

CHUNK = 1 << 20


class CleanStream(io.RawIOBase):
	"""UTF-16 or UTF-8 in, UTF-8 out, forbidden characters removed.

	`removed` counts what was taken out, so a caller can say how much of a file
	was cleaned rather than leave it to be wondered about.
	"""

	def __init__(self, binary):
		head = binary.read(2)
		rest = binary
		self._pending = head
		encoding = "utf-16" if head in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
		self._decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
		self._raw = rest
		self._out = b""
		self._tail = ""
		self._done = False
		self.removed = Counter()

	def readable(self):
		return True

	def _fill(self, want):
		while len(self._out) < want and not self._done:
			chunk = self._pending + self._raw.read(CHUNK)
			self._pending = b""
			last = not chunk
			text = self._tail + self._decoder.decode(chunk, final=last)
			self._tail = ""
			# A reference split across two chunks would be missed, so anything
			# from an unfinished `&` onwards waits for the next one.
			cut = text.rfind("&")
			if not last and cut != -1 and ";" not in text[cut:]:
				text, self._tail = text[:cut], text[cut:]
			refs = FORBIDDEN_REFERENCE.findall(text)
			chars = FORBIDDEN_CHARACTER.findall(text)
			if refs:
				self.removed["references"] += len(refs)
				text = FORBIDDEN_REFERENCE.sub("", text)
			if chars:
				self.removed["characters"] += len(chars)
				text = FORBIDDEN_CHARACTER.sub("", text)
			self._out += text.encode("utf-8")
			self._done = last

	def readinto(self, buffer):
		self._fill(len(buffer))
		n = min(len(buffer), len(self._out))
		buffer[:n] = self._out[:n]
		self._out = self._out[n:]
		return n


def open_export(source):
	"""A clean stream over a Tally export: a path, a file object, or bytes.

	A zip is read from its first `.xml` member, so the file nobody can upload
	at 166 MB arrives at 6 MB.
	"""
	if isinstance(source, (bytes, bytearray)):
		source = io.BytesIO(source)
	elif isinstance(source, str):
		# nosemgrep: frappe-security-file-traversal - a path this code made, not one from a request
		source = open(source, "rb")

	head = source.read(4)
	source.seek(0)
	if head.startswith(b"PK\x03\x04"):
		archive = zipfile.ZipFile(source)
		members = [m for m in archive.namelist() if m.lower().endswith(".xml")]
		if not members:
			raise ValueError("That zip has no XML file in it.")
		return CleanStream(archive.open(members[0]))
	return CleanStream(source)


def iter_elements(stream, tags):
	"""Each finished element with one of these tags, strictly parsed.

	The caller reads what it needs and the element is cleared afterwards, so a
	file of any size is held one object at a time.
	"""
	for _event, element in etree.iterparse(stream, tag=tags, huge_tree=True):
		yield element
		element.clear()
		parent = element.getparent()
		if parent is not None:
			while element.getprevious() is not None:
				del parent[0]
