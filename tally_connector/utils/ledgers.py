"""
tally_connector/utils/ledgers.py

Canonical ledger naming, and the registry that guarantees master/voucher closure.

Two bugs lived here before this module existed, and both silently produced XML
that Tally rejected line by line:

1. The Chart of Accounts export named a ledger after `Account.account_name`
   while every voucher referenced `Account.name` minus the company suffix.
   For any account carrying an account number those are different strings -
   `50100012345 - Demo Bank - DT` was exported as "Demo Bank" and referenced
   on every voucher as "50100012345 - Demo Bank".

2. Several ledgers were referenced by vouchers and created by nothing at all:
   the hardcoded "Suspense Account", the `sales_ledger`/`purchase_ledger`/
   `tds_deductible_ledger` settings defaults, and every Employee named as the
   party on a Payment Entry (the parties export covers Customers and Suppliers
   only).

Together those accounted for 1,489 of 3,996 voucher lines on the bench this was
measured against - 37%. Tally refuses a voucher line whose ledger does not
exist, so more than a third of the books would not have imported.

The fix is not a longer list of special cases. It is closure: every generator
records the ledgers it references into a `LedgerRegistry`, and the master export
is built from the accounts *plus whatever the registry still wants*. A ledger
referenced by a voucher and created by nothing is then impossible by
construction rather than by vigilance, and `tests/test_ledger_closure.py` fails
the build if the two sides ever drift again.
"""

import frappe
from frappe.utils import cstr
from frappe.utils.caching import request_cache

from tally_connector.tally.extract_rules import tally_key

# Tally's own primary groups. A ledger has to hang off one of these, or off a
# group that eventually does.
# The groups every Tally company already has, and which no import may create.
#
# Tally ships these and owns their place in the tree. Sending a `GROUP` master
# for one of them with a different parent is not a duplicate to be ignored - it
# is a re-parenting Tally refuses, and it aborts the whole file at that line.
# That is what happened on the first real import: ERPNext's own root account
# `Application of Funds (Assets)` was created under Tally's reserved
# `Current Assets`, and ERPNext's `Current Assets` was then created under
# `Application of Funds (Assets)` - a cycle, which Tally reported as
# *"Group 'Application of Funds (Assets)' is already a child of this!"*.
#
# Compared case-insensitively through `is_reserved_group`, because Tally does
# not distinguish `Duties & Taxes` from `DUTIES & TAXES`.
TALLY_RESERVED_GROUPS = frozenset(
	{
		# Primary
		"Branch / Divisions",
		"Capital Account",
		"Current Assets",
		"Current Liabilities",
		"Direct Expenses",
		"Direct Incomes",
		"Fixed Assets",
		"Indirect Expenses",
		"Indirect Incomes",
		"Investments",
		"Loans (Liability)",
		"Misc. Expenses (ASSET)",
		"Purchase Accounts",
		"Sales Accounts",
		"Suspense A/c",
		# Sub-groups
		"Bank Accounts",
		"Bank OCC A/c",
		"Bank OD A/c",
		"Cash-in-Hand",
		"Deposits (Asset)",
		"Duties & Taxes",
		"Loans & Advances (Asset)",
		"Provisions",
		"Reserves & Surplus",
		"Secured Loans",
		"Stock-in-Hand",
		"Sundry Creditors",
		"Sundry Debtors",
		"Unsecured Loans",
	}
)

_RESERVED_BY_KEY = {tally_key(name): name for name in TALLY_RESERVED_GROUPS}


def reserved_group(name):
	"""Tally's own spelling of the reserved group this name is, or "".

	Matched the way Tally matches, not by case alone: `Cash In Hand` is Tally's
	`Cash-in-Hand` and `Stock In Hand` is its `Stock-in-Hand`, which is what
	stopped a real masters file at its 9,627th record.
	"""
	return _RESERVED_BY_KEY.get(tally_key(name), "")


def is_reserved_group(name):
	"""Whether Tally already owns a group of this name."""
	return bool(reserved_group(name))


TALLY_PRIMARY = {
	"Asset": "Current Assets",
	"Liability": "Current Liabilities",
	"Income": "Sales Accounts",
	"Expense": "Indirect Expenses",
	"Equity": "Capital Account",
}

# Where a ledger goes when nothing else says. Each is a real Tally group.
GROUP_SUSPENSE = "Suspense A/c"
GROUP_BANK = "Bank Accounts"
GROUP_BANK_OD = "Bank OD A/c"
GROUP_CASH = "Cash-in-Hand"

# Tally's own reserved groups for money. A ledger has to sit under one of these
# for Tally to treat it as cash or bank at all - bank reconciliation only offers
# ledgers under a bank group, and the Contra voucher only accepts these.
#
# It also decides which vouchers a ledger may appear on: Tally's Journal
# excludes cash and bank ledgers unless `Allow Cash Accounts in Journals` is
# switched on in F12, and that is off by default.
CASH_BANK_GROUPS = frozenset({GROUP_BANK, GROUP_BANK_OD, GROUP_CASH, "Bank OCC A/c"})
GROUP_ROUNDING = "Indirect Expenses"
GROUP_DUTIES = "Duties & Taxes"
GROUP_SALES = "Sales Accounts"
GROUP_PURCHASE = "Purchase Accounts"
GROUP_DEBTORS = "Sundry Debtors"
GROUP_CREDITORS = "Sundry Creditors"


@request_cache
def company_abbr(company):
	"""The abbreviation ERPNext appends to this company's account names.

	Memoised for the life of the request. `ledger_name` is called for every
	ledger reference on every voucher - several times per document - and each
	call reached `get_cached_value`. Cheap but not free, and it was the one
	lookup left in the hot path that grew with the size of the export.
	"""
	if not company:
		return ""
	return cstr(frappe.get_cached_value("Company", company, "abbr") or "")


def party_ledger_name(name):
	"""A party's ledger name, stripped.

	A supplier's name can end in a trailing space nobody can see. The parties master created it verbatim while
	every voucher referenced the stripped form, so the closure step created a
	*second* ledger and Tally would have carried the same supplier twice, with
	the invoices on one and the payments on the other.

	Whitespace at the ends of a Tally ledger name is invisible and never
	meaningful, so it is removed once, here, and both sides agree by
	construction.
	"""
	return cstr(name or "").strip()


def group_name(account, company=None):
	"""What an ERPNext group account is called in Tally.

	Its own name without the company suffix - and where that is one of Tally's
	reserved groups, Tally's spelling of it. ERPNext's `Cash In Hand` group
	means exactly Tally's `Cash-in-Hand`, so it is not created; anything under
	it is placed under Tally's group by the name Tally gave it.
	"""
	stripped = _without_suffix(account, company)
	return reserved_group(stripped) or stripped


def ledger_name(account, company=None):
	"""The one name a ledger is known by, on both sides of the export.

	Where the name without its suffix is one of Tally's reserved **groups**, the
	suffix stays. ERPNext's standard chart has a ledger called `Stock In Hand`,
	Tally has a group called `Stock-in-Hand`, and Tally reads those as one name
	- so the ledger was a request to turn Tally's group into a ledger under
	somewhere else, and the masters file stopped there with *"Improper change
	of Group Name"*. `Stock In Hand - KTC` is ERPNext's own name for it, so it
	is still recognisable, and Tally does not confuse it with anything. Every
	voucher reaches this function too, so they name it the same way.

	A group is named by `group_name`, never by this: a group spelled like one
	of Tally's is Tally's group and must not be kept apart from it.
	"""
	stripped = _without_suffix(account, company)
	if stripped and is_reserved_group(stripped):
		return cstr(account or "").strip()
	return stripped


def _without_suffix(account, company=None):
	"""`Account.name` minus the company suffix - so
	`50100012345 - Demo Bank - DT` becomes
	`50100012345 - Demo Bank`, which is what the vouchers already used and
	what the masters now create.

	The abbreviation is matched exactly where the company is known, because a
	blind `rsplit(" - ", 1)` mangles an account whose own name contains the
	separator - such as `Vehicle Loan - Model EV - DT`,
	which the blind split happens to get right and would not on a company whose
	abbreviation were "EV".
	"""
	account = cstr(account or "").strip()
	if not account:
		return ""

	abbr = company_abbr(company) if company else ""
	if abbr and account.endswith(f" - {abbr}"):
		return account[: -len(f" - {abbr}")].strip()

	# Company not passed, or the suffix is some other company's. Fall back to
	# any abbreviation actually in use on this site before guessing blindly.
	for known in _known_abbrs():
		if account.endswith(f" - {known}"):
			return account[: -len(f" - {known}")].strip()

	return account


@request_cache
def _known_abbrs():
	"""Every abbreviation in use, memoised per request for the same reason.

	A process-wide cache was wrong here as well as slow: it outlived the request
	and would have answered a second site with the first site's companies.
	"""
	return [cstr(r.abbr) for r in frappe.get_all("Company", fields=["abbr"]) if r.abbr]


class LedgerRegistry:
	"""Every ledger name a voucher referenced, and where it should live.

	Voucher generators call `ref()` instead of writing a LEDGERNAME directly.
	The master export then asks `required()` for the names it must create, so
	the two cannot disagree.
	"""

	def __init__(self, company=None):
		self.company = company
		self._refs = {}

	def ref(self, name, parent=None):
		"""Record a reference and return the name, so this reads inline."""
		name = cstr(name or "").strip()
		if not name:
			return name
		# First non-empty parent hint wins. A later caller with no opinion must
		# not erase an earlier caller's.
		if name not in self._refs or (parent and not self._refs[name]):
			self._refs[name] = parent or self._refs.get(name)
		return name

	def account_ref(self, account, parent=None):
		"""Record a reference to an ERPNext Account by its canonical name."""
		return self.ref(ledger_name(account, self.company), parent)

	def required(self):
		"""name -> parent group hint (may be None)."""
		return dict(self._refs)

	def names(self):
		return set(self._refs)

	def merge(self, other):
		for name, parent in other.required().items():
			self.ref(name, parent)
		return self
