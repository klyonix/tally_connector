"""
Writing a Tally company into ERPNext: the chart, the parties, the balances.

The plan in `tally/plan.py` decides everything; this does it, and does nothing
the plan did not say. That split is what makes the dry run trustworthy - the
screen somebody approves is built from the same object the importer follows.

Three decisions are worth stating here rather than leaving to be read out of the
code.

**Only an empty company.** A company that already has ledger entries is one
where these balances would be added to somebody else's, and no import should be
the thing that discovers that. The refusal is checked before the first write.

**Profit and loss does not become an opening balance.** A cutover carries the
balance sheet; the income and expense of the year that ended is history, and its
net belongs in reserves. So the two sides are told apart and the net is posted
once, to an equity account named after Tally's own, with the figure reported
rather than folded in quietly.

**Parties are parties, not accounts.** Tally keeps a ledger per customer;
ERPNext keeps one receivable account and names the party on the posting, which
is what makes its ageing and statements work. So a ledger under Sundry Debtors
becomes a Customer and its balance a posting with that Customer on it.
"""

from __future__ import annotations

from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import flt

from tally_connector.tally.plan import erp_key

# How many rows go in one opening Journal Entry, balancing row included.
#
# The ceiling is ERPNext's, not ours: `Journal Entry.submit` hands anything
# over a hundred rows to a background worker instead of posting it. On a bench
# with the scheduler paused that worker never runs, so the import reports
# fourteen entries made and the ledgers stay empty - which is exactly what the
# first end-to-end run did. Ninety-nine rows plus the balancing row is a
# hundred, and a hundred submits in the request.
OPENING_CHUNK = 99

# What Tally calls the ledger its own profit and loss lands in.
PROFIT_AND_LOSS = "Profit & Loss A/c"

GST_CATEGORY = {
	"regular": "Registered Regular",
	"composition": "Registered Composition",
	"unregistered/consumer": "Unregistered",
	"unregistered": "Unregistered",
	"consumer": "Unregistered",
	"unknown": "Unregistered",
}


def company_is_empty(company):
	"""Whether this company has any accounting history of its own yet."""
	return not frappe.db.exists("GL Entry", {"company": company, "is_cancelled": 0})


def _roots(company):
	rows = frappe.get_all(
		"Account",
		filters={"company": company, "parent_account": ("in", ["", None])},
		fields=["name", "root_type"],
	)
	return {r.root_type: r.name for r in rows}


def _existing(company):
	rows = frappe.get_all(
		"Account", filters={"company": company}, fields=["name", "account_name", "is_group"]
	)
	return {erp_key(r.account_name): r for r in rows}


def _promote_to_group(name):
	"""Make an existing leaf account a group, so Tally's children can hang off it.

	ERPNext's own default chart carries several names Tally uses for groups -
	`Unsecured Loans` is a leaf there and a group with four ledgers under it in
	a real Tally company. Reusing the account and then parenting to it is
	refused by ERPNext ("Parent account ... can not be a ledger"), so it is
	converted first. Only ever in an empty company, so there is no posting to
	invalidate; the account type goes with the conversion because ERPNext
	refuses a typed account as a group, and a group's type is inherited by its
	children anyway.
	"""
	doc = frappe.get_doc("Account", name)
	if doc.is_group:
		return
	doc.is_group = 1
	doc.account_type = None
	doc.flags.ignore_permissions = True
	doc.save()


def ensure_chart(company, plan, tick=None):
	"""Every Tally group and ledger as an ERPNext account. Returns the mapping.

	An account of the same name already in the company is used rather than a
	second one made beside it, so running this twice changes nothing.
	"""
	roots = _roots(company)
	have = _existing(company)
	mapping, created = {}, 0

	for entry in plan.accounts:
		if tick:
			tick()
		found = have.get(erp_key(entry.account_name))
		if found:
			if entry.is_group and not found.is_group:
				_promote_to_group(found.name)
				found.is_group = 1
			mapping[entry.name] = found.name
			continue

		parent = mapping.get(entry.parent) or (
			have[erp_key(entry.parent)].name if erp_key(entry.parent) in have else None
		)
		if not parent:
			parent = roots.get(entry.root_type) or roots.get("Liability")
		elif not frappe.db.get_value("Account", parent, "is_group"):
			# A plan entry whose parent the plan itself never declared a group:
			# the same promotion, reached from the child rather than the parent.
			_promote_to_group(parent)

		doc = frappe.new_doc("Account")
		doc.account_name = entry.account_name.strip()
		doc.company = company
		doc.parent_account = parent
		doc.is_group = 1 if entry.is_group else 0
		doc.root_type = entry.root_type
		# A group carries the type so its children inherit it; a leaf that
		# Tally never typed is left untyped rather than guessed at.
		if entry.account_type:
			doc.account_type = entry.account_type
		doc.flags.ignore_permissions = True
		doc.flags.ignore_mandatory = True
		doc.insert()
		have[erp_key(doc.account_name)] = frappe._dict(
			{"name": doc.name, "account_name": doc.account_name, "is_group": doc.is_group}
		)
		mapping[entry.name] = doc.name
		created += 1

	return mapping, created


def _address_for(party_kind, party, plan_party, company):
	if not (plan_party.address or plan_party.gstin or plan_party.state):
		return
	doc = frappe.new_doc("Address")
	doc.address_title = party[:100]
	doc.address_type = "Billing"
	lines = list(plan_party.address) or [plan_party.state or "-"]
	doc.address_line1 = lines[0][:240]
	if len(lines) > 1:
		doc.address_line2 = " ".join(lines[1:])[:240]
	doc.city = (lines[-1] if len(lines) > 1 else plan_party.state or "")[:100]
	doc.state = plan_party.state
	doc.country = plan_party.country or "India"
	doc.pincode = plan_party.pincode
	if plan_party.gstin and frappe.get_meta("Address").has_field("gstin"):
		doc.gstin = plan_party.gstin
	doc.append("links", {"link_doctype": party_kind, "link_name": party})
	doc.flags.ignore_permissions = True
	doc.flags.ignore_mandatory = True
	doc.insert()


def ensure_parties(plan, company, with_addresses=True, tick=None, commit_every=200):
	"""Every Tally party ledger as a Customer or a Supplier.

	Returns what was made and, beside it, what each Tally ledger is called in
	ERPNext - which is not always the same string. A Customer is named by a
	primary key on a case-insensitive collation, so Tally's `NORTHGATE
	SUPPLIES` reuses an existing `Northgate Supplies` rather
	than making a second one, and ERPNext appends a number where a name is
	taken. Everything downstream names the party by this mapping, because
	reconciling against the Tally spelling reads as a balance that never
	arrived when it is sitting there under the other one.
	"""
	made = {"Customer": 0, "Supplier": 0}
	mapping = {}
	seen = 0
	gst_field = frappe.get_meta("Customer").has_field("gst_category")

	for party in plan.parties:
		seen += 1
		if tick:
			tick()
		name = party.name.strip()
		found = frappe.db.exists(party.kind, name)
		if found:
			mapping[party.name] = found
			continue
		doc = frappe.new_doc(party.kind)
		if party.kind == "Customer":
			doc.customer_name = name
			doc.customer_type = "Company"
			doc.customer_group = (
				frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or "All Customer Groups"
			)
			doc.territory = frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories"
		else:
			doc.supplier_name = name
			doc.supplier_group = (
				frappe.db.get_value("Supplier Group", {"is_group": 0}, "name") or "All Supplier Groups"
			)
		if gst_field:
			doc.gst_category = GST_CATEGORY.get((party.registration or "").strip().lower(), "Unregistered")
		if party.pan and doc.meta.has_field("pan"):
			doc.pan = party.pan
		doc.flags.ignore_permissions = True
		doc.flags.ignore_mandatory = True
		doc.insert()
		made[party.kind] += 1
		mapping[party.name] = doc.name
		if with_addresses:
			try:
				_address_for(party.kind, doc.name, party, company)
			except Exception:
				# An address nobody can parse is not a reason to lose the party.
				frappe.log_error(frappe.get_traceback(), "Tally Connector: party address")
		if commit_every and seen % commit_every == 0:
			frappe.db.commit()  # nosemgrep: frappe-manual-commit - a long import commits in batches

	return made, mapping


def _party_account(company, kind):
	field = "default_receivable_account" if kind == "Customer" else "default_payable_account"
	account = frappe.db.get_value("Company", company, field)
	if account:
		return account
	want = "Receivable" if kind == "Customer" else "Payable"
	return frappe.db.get_value("Account", {"company": company, "account_type": want, "is_group": 0}, "name")


def _equity_account(company, accounts, plan):
	"""Where the period's profit or loss lands."""
	if PROFIT_AND_LOSS in accounts:
		return accounts[PROFIT_AND_LOSS]
	roots = _roots(company)
	existing = frappe.db.get_value("Account", {"company": company, "account_name": PROFIT_AND_LOSS}, "name")
	if existing:
		return existing
	doc = frappe.new_doc("Account")
	doc.account_name = PROFIT_AND_LOSS
	doc.company = company
	doc.parent_account = roots.get("Equity") or roots.get("Liability")
	doc.root_type = "Equity"
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def _temporary_account(company):
	return frappe.db.get_value(
		"Account", {"company": company, "account_type": "Temporary", "is_group": 0}, "name"
	)


def opening_rows(company, plan, accounts, parties=None):
	"""The opening posting for every balance, ERPNext's debits and credits.

	Tally's sign is kept until here and converted once: negative is a debit.
	Income and expense do not appear; their net is one row of its own.
	"""
	rows, profit_and_loss = [], Decimal("0")
	parties = parties or {}
	root_of = {a.name: a.root_type for a in plan.accounts}

	for name, balance in sorted(plan.closing().items()):
		kind = plan.party_kind.get(name)
		if kind:
			account = _party_account(company, kind)
			rows.append(
				{
					"account": account,
					"party_type": kind,
					"party": parties.get(name, name.strip()),
					"amount": balance,
				}
			)
			continue
		if root_of.get(name) in ("Income", "Expense"):
			profit_and_loss += balance
			continue
		account = accounts.get(name)
		if not account:
			continue
		rows.append({"account": account, "amount": balance})

	if profit_and_loss:
		rows.append(
			{
				"account": _equity_account(company, accounts, plan),
				"amount": profit_and_loss,
				"profit_and_loss": True,
			}
		)
	return rows


def load_opening(company, plan, as_on, accounts, parties=None, tick=None, commit=True):
	"""The balances, as submitted opening Journal Entries.

	Split into entries of `OPENING_CHUNK` rows, each balanced through Temporary
	Opening. Across all of them those balancing rows total nought, because the
	company's own trial balance does - so Temporary Opening is left at nil and
	every other account holds exactly what Tally said.
	"""
	rows = opening_rows(company, plan, accounts, parties)
	temporary = _temporary_account(company)
	if not temporary:
		frappe.throw(
			_(
				"This company has no Temporary Opening account, which is "
				"where each batch of opening balances is balanced through."
			)
		)

	made, profit = [], Decimal("0")
	for start in range(0, len(rows), OPENING_CHUNK):
		chunk = rows[start : start + OPENING_CHUNK]
		entry = frappe.new_doc("Journal Entry")
		entry.voucher_type = "Opening Entry"
		entry.is_opening = "Yes"
		entry.company = company
		entry.posting_date = as_on
		entry.user_remark = (
			f"Opening balances from Tally, {start // OPENING_CHUNK + 1} of "
			f"{(len(rows) - 1) // OPENING_CHUNK + 1}"
		)
		# What the rows actually carry once rounded to the currency, rather
		# than the exact sum: the balancing row has to offset what was posted,
		# and a hundredth lost on each of ninety-nine rows is a hundredth
		# ERPNext will refuse the whole entry over.
		posted = Decimal("0")
		for row in chunk:
			amount = Decimal(str(row["amount"]))
			if row.get("profit_and_loss"):
				profit = amount
			value = Decimal(str(flt(abs(amount), 2)))
			line = {"account": row["account"]}
			if amount < 0:
				line["debit_in_account_currency"] = float(value)
				posted -= value
			else:
				line["credit_in_account_currency"] = float(value)
				posted += value
			if row.get("party"):
				line["party_type"] = row["party_type"]
				line["party"] = row["party"]
			entry.append("accounts", line)
			if tick:
				tick()
		if posted:
			# The rows net to a credit, so the row that balances them is a
			# debit, and the other way about.
			entry.append(
				"accounts",
				{
					"account": temporary,
					"debit_in_account_currency": float(posted) if posted > 0 else 0,
					"credit_in_account_currency": float(-posted) if posted < 0 else 0,
				},
			)
		entry.flags.ignore_permissions = True
		entry.insert()
		entry.submit()
		if entry.docstatus != 1:
			# ERPNext queued it rather than posting it, so the balances are not
			# in the ledgers whatever this function would otherwise report.
			# Better to stop here than to hand back a count that means nothing.
			frappe.throw(
				_(
					"Opening entry {0} was queued for a background worker "
					"rather than posted, so its {1} balances are not in "
					"the ledgers. That happens above a hundred rows an entry."
				).format(entry.name, len(chunk))
			)
		made.append(entry.name)
		if commit:
			frappe.db.commit()  # nosemgrep: frappe-manual-commit - a long import commits in batches

	return {"entries": made, "rows": len(rows), "profit_and_loss": profit}


def reconcile(company, plan, as_on, accounts, parties=None, profit_and_loss=Decimal("0")):
	"""ERPNext's balances against Tally's, one ledger at a time.

	The proof the import worked, rather than the claim. Party balances are
	compared per party, because that is where ERPNext keeps them.

	One account is expected to differ and it is not a fault. Tally keeps the
	result brought forward in `Profit & Loss A/c` and works the current
	period's out live from the income and expense ledgers; ERPNext has been
	given the net as a posting, so that account holds both figures. So what is
	checked there is Tally's own balance plus the net that was posted.
	"""
	wanted, mismatches = plan.closing(), []
	parties = parties or {}
	if profit_and_loss:
		equity = _equity_account(company, accounts, plan)
		for name, mapped in accounts.items():
			if mapped == equity:
				wanted[name] = wanted.get(name, Decimal("0")) + profit_and_loss
				break
		else:
			wanted[equity] = profit_and_loss
	from frappe.query_builder.functions import Sum

	gl = frappe.qb.DocType("GL Entry")
	rows = (
		frappe.qb.from_(gl)
		.select(gl.account, gl.party_type, gl.party, (Sum(gl.credit) - Sum(gl.debit)).as_("balance"))
		.where((gl.company == company) & (gl.is_cancelled == 0) & (gl.posting_date <= as_on))
		.groupby(gl.account, gl.party_type, gl.party)
	).run(as_dict=True)

	got = {}
	for row in rows:
		key = row.party if row.party else row.account
		got[key] = got.get(key, Decimal("0")) + Decimal(str(row.balance or 0))

	root_of = {a.name: a.root_type for a in plan.accounts}

	for name, balance in wanted.items():
		if root_of.get(name) in ("Income", "Expense"):
			# Netted into reserves at a cutover, so there is no ledger of its
			# own to compare; the net is checked separately below.
			continue
		if plan.party_kind.get(name):
			found = got.get(parties.get(name, name.strip()))
		else:
			found = got.get(accounts.get(name, name))
		found = Decimal(str(found if found is not None else 0))
		if abs(found - balance) > Decimal("0.01"):
			mismatches.append(
				{
					"ledger": name,
					"tally": str(balance),
					"erpnext": str(found),
					"difference": str(found - balance),
				}
			)
	return mismatches
