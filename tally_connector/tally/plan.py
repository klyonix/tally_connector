"""
What a Tally company becomes in ERPNext, decided before anything is written.

Pure, and that is the point: the whole mapping can be read, argued with and
tested against a fixture with no site, and the same object is both the dry run
somebody approves and the instructions the importer follows. A dry run that is
produced by different code from the import is a dry run that can be wrong about
it.

The two models differ in one way that shapes everything else. **Tally keeps a
ledger per customer; ERPNext keeps one receivable account and a party on each
posting.** So a ledger under Sundry Debtors becomes a Customer rather than an
account, and its balance is a posting to the company's receivable account with
that Customer named - which is what makes ERPNext's own ageing and statements
work at all.

Signs are Tally's throughout: negative is a debit. Nothing here converts to
ERPNext's debit and credit columns; the importer does that at the last moment,
so a mistake in it cannot hide inside the plan.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from tally_connector.tally.books import (
    LIABILITY,
)
from tally_connector.tally.extract_rules import tally_key

BLOCKING, WARNING, NOTE = "blocking", "warning", "note"

# Tally's reserved groups that tell ERPNext what kind of account this is.
# Matched the way Tally matches names, so `Cash In Hand` and `Cash-in-Hand` are
# one group - the distinction that cost a masters import its ninth thousandth
# record in the other direction.
ACCOUNT_TYPES = {
    tally_key("Bank Accounts"): "Bank",
    tally_key("Bank OD A/c"): "Bank",
    tally_key("Bank OCC A/c"): "Bank",
    tally_key("Cash-in-Hand"): "Cash",
    tally_key("Duties & Taxes"): "Tax",
    tally_key("Stock-in-Hand"): "Stock",
    tally_key("Fixed Assets"): "Fixed Asset",
    tally_key("Sundry Debtors"): "Receivable",
    tally_key("Sundry Creditors"): "Payable",
}
DEBTORS, CREDITORS = tally_key("Sundry Debtors"), tally_key("Sundry Creditors")

# Where a ledger goes when its own group is not in the file. Tally's own
# parking group, so it reads as what it is rather than as somebody's guess.
SUSPENSE = "Suspense A/c"


def erp_key(name):
    """A name as ERPNext's database compares it.

    An Account, a Customer and a Supplier are each named by a primary key on a
    case-insensitive collation, so `BANK CHARGES` and `Bank Charges` are one
    record whatever Tally thinks - and two Tally ledgers differing only in case
    or in the spaces between words cannot both be imported. That is worth
    refusing before the import rather than discovering on the duplicate.
    """
    return " ".join(name.split()).casefold()


@dataclass
class AccountPlan:
    name: str
    parent: str
    is_group: bool
    root_type: str
    account_type: str = ""
    # Set only where ERPNext cannot hold the Tally name as it stands. The Tally
    # name stays in `name`, because that is what the balances and the postings
    # are keyed on; this is what the account is called once it is there.
    erp_name: str = ""

    @property
    def account_name(self) -> str:
        return self.erp_name or self.name


@dataclass
class PartyPlan:
    kind: str
    name: str
    group: str = ""
    gstin: str = ""
    registration: str = ""
    state: str = ""
    country: str = ""
    pincode: str = ""
    address: list[str] = field(default_factory=list)
    pan: str = ""
    email: str = ""
    phone: str = ""


@dataclass
class Finding:
    severity: str
    check: str
    subject: str
    detail: str


@dataclass
class ImportPlan:
    accounts: list[AccountPlan] = field(default_factory=list)
    parties: list[PartyPlan] = field(default_factory=list)
    party_kind: dict[str, str] = field(default_factory=dict)
    opening: dict[str, Decimal] = field(default_factory=dict)
    movement: dict[str, Decimal] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    vouchers: int = 0
    postings: int = 0
    unposted: int = 0
    by_kind: Counter = field(default_factory=Counter)
    first_date = None
    last_date = None
    removed: Counter = field(default_factory=Counter)

    def closing(self) -> dict[str, Decimal]:
        """Each ledger's balance at the end of the period, Tally's signs."""
        out = dict(self.opening)
        for name, amount in self.movement.items():
            out[name] = out.get(name, Decimal("0")) + amount
        return {k: v for k, v in out.items() if v != 0}

    def trial_balance(self) -> Decimal:
        return sum(self.closing().values(), Decimal("0"))

    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == BLOCKING]

    def add(self, severity, check, subject, detail):
        self.findings.append(Finding(severity, check, subject, detail))


def _ancestry(name, groups, limit=50):
    """A group and every group above it, nearest first."""
    seen, out = set(), []
    current = groups.get(name)
    while current and current.name not in seen and len(out) < limit:
        seen.add(current.name)
        out.append(current)
        current = groups.get(current.parent) if current.parent else None
    return out


def _nature(name, groups) -> str:
    """The root type of a group, from the primary group above it.

    Read off the group's own flags rather than a table of names, so a group a
    company invented is classified as well as one Tally reserves.
    """
    line = _ancestry(name, groups)
    return line[-1].nature if line else LIABILITY


def _account_type(name, groups) -> str:
    for group in _ancestry(name, groups):
        kind = ACCOUNT_TYPES.get(tally_key(group.name))
        if kind:
            return kind
    return ""


def party_kind(parent, groups) -> str:
    """Customer, Supplier, or neither, from the groups above a ledger."""
    for group in _ancestry(parent, groups):
        key = tally_key(group.name)
        if key == DEBTORS:
            return "Customer"
        if key == CREDITORS:
            return "Supplier"
    return ""


def _depth(name, groups):
    return len(_ancestry(name, groups))


def _resolve_account_names(plan) -> None:
    """Give every account a name ERPNext can actually hold.

    Tally's ordinary way of recording a group's own direct postings is a ledger
    sitting inside a group of the same name - nine of them in one real company,
    `Rates & Taxes` inside `Rates & Taxes`. ERPNext names an account by a
    primary key on a case-insensitive collation, so the two cannot both exist,
    and a group there carries no balance of its own, so they cannot be merged
    either: the ledger's figure would have nowhere to sit.

    So the group keeps the name and the ledger is renamed, deterministically.
    The Tally name is untouched, because the balances and the postings are keyed
    on it; only what ERPNext calls the account changes, and the plan says so.
    """
    sharing = {}
    for entry in plan.accounts:
        sharing.setdefault(erp_key(entry.name), []).append(entry)
    taken = set(sharing)

    for entries in list(sharing.values()):
        if len(entries) < 2:
            continue
        # A group keeps its name; it is the one with children hanging off it.
        entries.sort(key=lambda e: (not e.is_group,))
        for entry in entries[1:]:
            candidate, n = f"{entry.name} (ledger)", 1
            while erp_key(candidate) in taken:
                n += 1
                candidate = f"{entry.name} (ledger {n})"
            entry.erp_name = candidate
            taken.add(erp_key(candidate))
            plan.add(WARNING, "name collision", entry.name,
                     f"Tally has a group and a ledger of this name, and ERPNext "
                     f"cannot hold both, so the ledger is imported as "
                     f"{candidate!r}")


def build_plan(groups, ledgers, vouchers=(), skip_unposted_count=0) -> ImportPlan:
    """The whole mapping, and everything wrong with it, in one object."""
    plan = ImportPlan()
    plan.unposted = skip_unposted_count

    # Groups, parents before children, so a chart can be built in this order.
    for group in sorted(groups.values(), key=lambda g: (_depth(g.name, groups), g.name)):
        if group.parent and group.parent not in groups:
            plan.add(WARNING, "group parent", group.name,
                     f"its parent {group.parent!r} is not in this file, so it is "
                     f"placed at the top of {_nature(group.name, groups)}")
        plan.accounts.append(AccountPlan(
            name=group.name,
            parent=group.parent if group.parent in groups else "",
            is_group=True,
            root_type=_nature(group.name, groups),
            account_type=_account_type(group.name, groups),
        ))

    for ledger in ledgers.values():
        kind = party_kind(ledger.parent, groups)
        if ledger.opening:
            plan.opening[ledger.name] = ledger.opening
        if kind:
            plan.party_kind[ledger.name] = kind
            plan.parties.append(PartyPlan(
                kind=kind, name=ledger.name, group=ledger.parent,
                gstin=ledger.gstin, registration=ledger.registration,
                state=ledger.state, country=ledger.country, pincode=ledger.pincode,
                address=list(ledger.address), pan=ledger.pan,
                email=ledger.email, phone=ledger.phone,
            ))
            continue
        if ledger.parent and ledger.parent not in groups:
            plan.add(WARNING, "ledger group", ledger.name,
                     f"its group {ledger.parent!r} is not in this file, so it is "
                     f"placed under {SUSPENSE}")
        kind = _account_type(ledger.parent, groups)
        if kind == "Stock":
            # ERPNext will not let a Journal Entry move a stock account: its
            # balance is meant to come from stock transactions, and an opening
            # one from a Stock Reconciliation. This import carries the closing
            # position and no inventory, so the figure is brought in as an
            # ordinary asset and the account is left untyped - saying so here,
            # because whoever turns on perpetual inventory later needs to know
            # this is not the account stock will post to.
            kind = ""
            plan.add(WARNING, "stock account", ledger.name,
                     "Tally holds stock here, and ERPNext only moves a stock "
                     "account through stock transactions, so the balance is "
                     "imported as an ordinary asset and the account is not "
                     "marked as the stock account")
        plan.accounts.append(AccountPlan(
            name=ledger.name,
            parent=ledger.parent if ledger.parent in groups else SUSPENSE,
            is_group=False,
            root_type=_nature(ledger.parent, groups) if ledger.parent in groups else LIABILITY,
            account_type=kind,
        ))

    # Sixteen ledgers in a real company are named with a leading or trailing
    # space, and their postings name them trimmed - which is why a ledger is
    # matched trimmed. Two records ERPNext cannot tell apart would be one
    # record holding both balances, so they are refused rather than merged.
    _resolve_account_names(plan)

    # Each ERPNext table is its own namespace, so an account and a customer of
    # the same name are two records and not a collision. A party is never
    # renamed: it is somebody's actual business name, and two that ERPNext
    # cannot tell apart is a decision for whoever keeps the books.
    for kind in ("Customer", "Supplier"):
        names = [p.name for p in plan.parties if p.kind == kind]
        counts = Counter(erp_key(n) for n in names)
        reported = set()
        for name in names:
            key = erp_key(name)
            if counts[key] > 1 and key not in reported:
                reported.add(key)
                plan.add(BLOCKING, "name collision", name.strip(),
                         f"{counts[key]} Tally ledgers differ only in case or in "
                         f"the spaces between words, so ERPNext would hold one "
                         f"{kind.lower()} carrying all their balances")

    known = {name.strip() for name in ledgers}
    unknown = Counter()
    for voucher in vouchers:
        plan.vouchers += 1
        plan.by_kind[voucher.kind] += 1
        if voucher.date:
            if plan.first_date is None or voucher.date < plan.first_date:
                plan.first_date = voucher.date
            if plan.last_date is None or voucher.date > plan.last_date:
                plan.last_date = voucher.date
        else:
            plan.add(BLOCKING, "voucher date", voucher.number or voucher.guid,
                     "this voucher has no date Tally could read")
        if not voucher.balances():
            plan.add(BLOCKING, "voucher balance", voucher.number or voucher.guid,
                     f"its postings total {voucher.total()} rather than nought")
        for posting in voucher.postings:
            plan.postings += 1
            plan.movement[posting.ledger] = (
                plan.movement.get(posting.ledger, Decimal("0")) + posting.amount)
            if posting.ledger not in known:
                unknown[posting.ledger] += 1

    for name, count in unknown.items():
        plan.add(BLOCKING, "unknown ledger", name,
                 f"{count} posting{'s' if count != 1 else ''} name this ledger and "
                 "the masters file does not have it")

    if plan.trial_balance() != 0:
        plan.add(BLOCKING, "trial balance", "the whole company",
                 f"balances total {plan.trial_balance()} rather than nought")
    return plan
