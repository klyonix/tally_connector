"""
Rules about Tally's grammar that both the impure and the pure halves need.

`tally/extract.py` is the one module that imports frappe, and `tally/validate.py`
must never import it - the whole layering rests on the validator running against
a JSON fixture with no site. A constant they both depend on cannot live in
either, so it lives here: no imports, nothing to go wrong.
"""

# Tally's UNIT `NAME` is the symbol. It refused `Square Meter` and `Parts Per
# Million` with "BAD UNIT NAME" and accepted `Nos` and `BAG`; what the refusals
# have and the acceptances lack is a space, and length.
UNIT_SYMBOL_MAX = 8


def tally_key(name):
	"""A name as Tally compares it: case and every non-alphanumeric ignored.

	Read off a real refusal rather than a document. A masters file sent a ledger
	called `Stock In Hand`, and Tally stopped with *"Improper change of Group
	Name for Ledger 'Stock-in-Hand'!"* - naming its own reserved group, spelled
	with hyphens. So to Tally those are one name, and a check that compares
	case alone is comparing something Tally does not.
	"""
	return "".join(c for c in (name or "").lower() if c.isalnum())
