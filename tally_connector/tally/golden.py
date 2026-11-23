"""
Fixtures in, golden XML out - and the tool that regenerates them.

Run offline, with no site and no database:

    python -m tally_connector.tally.golden --check     # what the tests do
    python -m tally_connector.tally.golden --write     # accept current output

`--write` is how an intended change to the renderer is recorded: regenerate, read
the diff, commit it. The diff is the review. An unintended change shows up the
same way, which is the point.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from tally_connector.tally.model import export_from_dict
from tally_connector.tally.render import render

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.join(HERE, "..", "tests")
FIXTURES = os.path.normpath(os.path.join(TESTS, "fixtures"))
GOLDEN = os.path.normpath(os.path.join(TESTS, "golden"))


def scenarios():
	for name in sorted(os.listdir(FIXTURES)):
		if name.endswith(".json"):
			yield name[:-5]


def load(name):
	# nosemgrep: frappe-security-file-traversal - a fixed test fixture path
	with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as fh:
		return export_from_dict(json.load(fh))


def rendered(name):
	"""The two documents this scenario produces, keyed by golden file name."""
	masters, vouchers = render(load(name))
	return {f"{name}.masters.xml": masters, f"{name}.vouchers.xml": vouchers}


def golden_path(filename):
	return os.path.join(GOLDEN, filename)


def check():
	"""Returns a list of (filename, reason) for everything that does not match."""
	failures = []
	for name in scenarios():
		for filename, produced in rendered(name).items():
			path = golden_path(filename)
			if not os.path.exists(path):
				failures.append((filename, "no golden file"))
				continue
			# nosemgrep: frappe-security-file-traversal - a fixed test fixture path
			with open(path, encoding="utf-8") as fh:
				expected = fh.read()
			if expected != produced:
				failures.append((filename, "differs from golden"))
	return failures


def write():
	os.makedirs(GOLDEN, exist_ok=True)
	written = []
	for name in scenarios():
		for filename, produced in rendered(name).items():
			# nosemgrep: frappe-security-file-traversal - a fixed test fixture path
			with open(golden_path(filename), "w", encoding="utf-8") as fh:
				fh.write(produced)
			written.append(filename)
	return written


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--write", action="store_true", help="overwrite the golden files with current output")
	parser.add_argument(
		"--check", action="store_true", help="compare current output against the golden files"
	)
	args = parser.parse_args(argv)

	if args.write:
		for filename in write():
			print(f"  wrote {filename}")
		return 0

	failures = check()
	for filename, reason in failures:
		print(f"  {filename}: {reason}")
	print(f"{len(list(scenarios()))} scenarios, {len(failures)} mismatches")
	return 1 if failures else 0


if __name__ == "__main__":
	sys.exit(main())
