"""
What Tally will refuse, decided before Tally sees it.

Pure: takes a model, returns findings. No frappe, no network, no exceptions -
a validator that throws stops at the first problem, and the whole point is to
hand somebody the list.

Each check exists because Tally enforces it, and carries the message Tally gives
when it slips through, so a finding here and a line in `tally.imp` are legibly
the same thing. Severity is about blast radius, not about how wrong something
looks: Tally aborts an import at the first line it cannot parse and reports only
that line, so one ABORTS finding costs the whole file - and then every voucher
that depended on those masters fails for a different-looking reason.
"""

from __future__ import annotations

import datetime
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from tally_connector.tally.extract_rules import tally_key
from tally_connector.tally.model import (
    REGISTRATION_TYPES, RESERVED_VOUCHER_TYPES, Export, money,
)

# The whole file is lost at this line.
ABORTS = "aborts"
# Tally refuses this record.
REJECTS = "rejects"
# Tally accepts it and the books are wrong.
WRONG = "wrong"

SEVERITY_ORDER = {ABORTS: 0, REJECTS: 1, WRONG: 2}

# A number Tally can read. `--1234.50` is what a purchase return produced, and
# Tally does not parse it - it stops.
NUMBER = re.compile(r"^-?\d+(\.\d{1,4})?$")

# Letters, digits, space, hyphen, underscore, full stop, per-cent. `/` is Tally's
# compound-unit separator, so a symbol holding one is read as a compound unit.
ILLEGAL_IN_UNIT = re.compile(r"[^A-Za-z0-9 \-_.%]")

CONTROL_CHARS = re.compile(r"[\x00-\x1f]")

# Tally's own reserved groups for money. A Journal voucher does not accept a
# ledger under any of them unless `Allow Cash Accounts in Journals` is switched
# on in F12, and that is off by default.
CASH_BANK_GROUPS = frozenset({
    "Bank Accounts", "Bank OD A/c", "Bank OCC A/c", "Cash-in-Hand",
})
JOURNAL_KINDS = frozenset({"Journal"})


@dataclass(frozen=True)
class Finding:
    severity: str
    check: str
    subject: str
    detail: str
    tally_says: str = ""

    def __str__(self):
        return f"[{self.severity}] {self.check} · {self.subject}: {self.detail}"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    checked: dict = field(default_factory=dict)

    def add(self, *args, **kwargs):
        self.findings.append(Finding(*args, **kwargs))

    @property
    def ok(self) -> bool:
        return not self.findings

    @property
    def blocking(self) -> list[Finding]:
        """Findings that cost you the import rather than the accuracy."""
        return [f for f in self.findings if f.severity in (ABORTS, REJECTS)]

    def by_severity(self) -> dict:
        out = {}
        for f in sorted(self.findings, key=lambda f: SEVERITY_ORDER[f.severity]):
            out.setdefault(f.severity, []).append(f)
        return out

    def counts(self) -> dict:
        return dict(Counter(f.severity for f in self.findings))

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "counts": self.counts(),
            "checked": self.checked,
            "findings": [
                {"severity": f.severity, "check": f.check, "subject": f.subject,
                 "detail": f.detail, "tally_says": f.tally_says}
                for f in sorted(self.findings, key=lambda f: SEVERITY_ORDER[f.severity])
            ],
        }


@dataclass
class TargetCompany:
    """What the destination Tally company actually is.

    Two checks cannot be answered from ERPNext alone - whether a date falls
    inside the company's open books, and whether a voucher type exists there.
    Both are read-only questions Tally will answer over its own XML gateway.
    Where nothing is known, the checks that need it are skipped and say so,
    rather than guessing and reporting a problem that is not one.
    """

    name: str = ""
    books_from: datetime.date | None = None
    period_to: datetime.date | None = None
    voucher_types: set[str] = field(default_factory=set)
    # name -> True | False | None, where None means Tally did not say.
    duplicate_guard: dict = field(default_factory=dict)
    known: bool = False
    # What the destination already holds, from `tally/snapshot.py`, or None.
    #
    # The closure checks need it. They ask whether a master a voucher names is
    # created by this export - and once the export stopped sending masters the
    # destination already has, that question stopped being the right one. On a
    # real company it reported 2,986 missing ledgers, 1,515 missing cost
    # centres and 279 missing units, every one of them sitting in Tally.
    snapshot: object | None = None

    def holds(self, kind: str, name: str):
        """Whether the destination already has this master.

        `None` where Tally was not asked or did not say, which the callers
        treat as "cannot help" rather than as "no" - the difference between a
        check that stays quiet and one that invents thousands of problems.
        """
        snapshot = self.snapshot
        if snapshot is None or not snapshot.known(kind):
            return None
        return snapshot.holds(kind, name) is not None


def _satisfied(target, kind, name, created):
    """Whether a master a voucher names will be there when the voucher lands.

    Two ways it can be: this export creates it, or the destination already has
    it. Only the first was ever checked, which was right while the export sent
    every master and wrong the moment it stopped sending the ones Tally has.
    """
    if name in created:
        return True
    return bool(target and target.holds(kind, name))


def _check_amounts_parse(export: Export, report: Report):
    """A value Tally cannot read stops the import at that line."""
    for voucher in export.vouchers:
        for line in voucher.lines:
            if not NUMBER.match(f"{line.amount:.2f}"):
                report.add(ABORTS, "amount", f"{voucher.number} / {line.ledger}",
                           f"{line.amount!r} is not a number Tally can read",
                           "(import aborts at the line)")
        for inv in voucher.inventory:
            if not NUMBER.match(f"{inv.amount:.2f}"):
                report.add(ABORTS, "amount", f"{voucher.number} / {inv.item}",
                           f"{inv.amount!r} is not a number Tally can read",
                           "(import aborts at the line)")


def _check_names(export: Export, report: Report):
    """Empty or control-character names abort; illegal unit symbols abort."""
    for unit in export.masters.units:
        if not unit.symbol.strip():
            report.add(ABORTS, "unit", "(blank)", "a unit with no symbol", "BAD UNIT NAME")
        elif ILLEGAL_IN_UNIT.search(unit.symbol):
            bad = "".join(sorted(set(ILLEGAL_IN_UNIT.findall(unit.symbol))))
            report.add(ABORTS, "unit", unit.symbol,
                       f"symbol contains {bad!r}; `/` is Tally's compound-unit separator",
                       "BAD UNIT NAME")

    named = (
        [("group", g.name) for g in export.masters.groups]
        + [("ledger", l.name) for l in export.masters.ledgers]
        + [("stock item", i.name) for i in export.masters.stock_items]
        + [("stock group", g.name) for g in export.masters.stock_groups]
    )
    for kind, name in named:
        if not str(name).strip():
            report.add(ABORTS, "name", f"({kind})", "a master with no name")
        elif CONTROL_CHARS.search(str(name)):
            report.add(ABORTS, "name", name, "name contains a control character")
        elif not tally_key(name):
            # A name of nothing but punctuation is not a name to Tally. It
            # matches masters on `tally_key` - case and every non-alphanumeric
            # ignored - so `....` reduces to the empty string and there is no
            # identity left to file the master under.
            #
            # Found on a live push: a customer in ERPNext is called `....`, and
            # Tally answered `0 errors and 1 exceptions` without naming it, so
            # the batch aborted and the rest of the file was lost. `strip()`
            # cannot see this, because those four dots are not whitespace.
            #
            # **How badly this matters depends on whether anything uses it.**
            # Such a master cannot exist in Tally at all, so the renderer
            # leaves it out and there is nothing for Tally to refuse - which
            # makes an unused one a WRONG rather than a blocker, reported by
            # name so nobody has to discover it later. One that a voucher
            # references is a different thing: the accounting genuinely cannot
            # cross, and sending the voucher would post it to a ledger that is
            # not there. That stays blocking.
            used = name in _referenced(export)
            report.add(
                ABORTS if used else WRONG, "name", name,
                f"a {kind} whose name is nothing but punctuation, which is the "
                "empty name once Tally has matched it"
                + (", and a voucher posts to it" if used else
                   ", and nothing references it, so it is left out of the file"),
                "(exception, unnamed)")


def _referenced(export: Export) -> set:
    """Every master name a voucher actually names."""
    out = set()
    for voucher in export.vouchers:
        out.update(l.ledger for l in voucher.lines)
        out.update(i.ledger for i in voucher.inventory)
        out.update(i.item for i in voucher.inventory)
        if voucher.party:
            out.add(voucher.party)
    return out


def _check_names_are_distinct(export: Export, report: Report):
    """Two ledgers whose names differ only by whitespace are two ledgers.

    A supplier on the books this was written against had a name ending in a
    trailing space nobody can see. The
    parties master created it verbatim while every voucher referenced the
    stripped form, so a second ledger was created to satisfy closure and Tally
    would have carried the same supplier twice, invoices on one and payments on
    the other.

    Nothing fails on import, which is what makes it worth a check: two ledgers
    with the same visible name is a reconciliation nobody can finish.
    """
    for kind, items in (("ledger", export.masters.ledgers),
                        ("stock item", export.masters.stock_items)):
        by_stripped = {}
        for item in items:
            by_stripped.setdefault(str(item.name).strip(), []).append(item.name)
        for stripped, names in by_stripped.items():
            if len(names) > 1:
                report.add(WRONG, "name collision", stripped,
                           f"{len(names)} {kind}s differ only by whitespace: "
                           f"{[repr(n) for n in names]}",
                           "(Tally carries them as separate records)")
            elif names[0] != stripped:
                report.add(WRONG, "name collision", stripped,
                           f"{kind} name has leading or trailing whitespace: {names[0]!r}")


def _check_units_are_consistent(export: Export, report: Report,
                                target: TargetCompany = None):
    """A stock item naming a unit that will not be there is a refused item."""
    symbols = {u.symbol for u in export.masters.units}
    for item in export.masters.stock_items:
        if item.base_unit and not _satisfied(target, "units", item.base_unit, symbols):
            report.add(REJECTS, "unit", item.name,
                       f"BASEUNITS is {item.base_unit!r}, which no unit master "
                       "creates and the destination has not got",
                       "BAD UNIT NAME")
    for voucher in export.vouchers:
        for inv in voucher.inventory:
            if inv.unit and not _satisfied(target, "units", inv.unit, symbols):
                report.add(REJECTS, "unit", f"{voucher.number} / {inv.item}",
                           f"line uses unit {inv.unit!r}, which no unit master "
                           "creates and the destination has not got",
                           "BAD UNIT NAME")


def _check_narration_length(export: Export, report: Report):
    """A narration longer than Tally holds.

    Measured off TallyPrime's own export: 514 narrations, longest 120, median
    also 120. This connector sent up to 1,456, and a live Tally answered
    `Software Exception c0000005 (Memory Access Violation)` importing vouchers.

    The renderer truncates rather than dropping the voucher, so this is a note
    about information that did not survive rather than a refusal - but it is
    said out loud, because text that vanishes with nothing recording it is the
    kind of loss nobody finds.
    """
    from tally_connector.tally.render import NARRATION_MAX

    for voucher in export.vouchers:
        length = len(voucher.narration or "")
        if length > NARRATION_MAX:
            report.add(WRONG, "narration", voucher.number,
                       f"its narration is {length} characters and Tally holds "
                       f"{NARRATION_MAX}, so it is sent cut short",
                       "(Tally's own narrations are never longer)")


def _check_balance(export: Export, report: Report):
    """The check Tally applies hardest."""
    for voucher in export.vouchers:
        out = voucher.imbalance()
        if out != Decimal("0.00"):
            report.add(REJECTS, "balance", voucher.number,
                       f"debits and credits differ by {out}",
                       "Total Debit and Credit amounts do not match")


def _check_closure(export: Export, report: Report, target: TargetCompany = None):
    """Every ledger a voucher names must exist when the voucher lands.

    Either this export creates it or the destination already has it. It used to
    be the first alone, which was right while every master was sent and became
    wrong the moment the export stopped sending the ones Tally already holds:
    on a real company that reported 2,986 ledgers missing, every one of them
    sitting in Tally.
    """
    created = export.masters.ledger_names()
    for voucher in export.vouchers:
        referenced = {l.ledger for l in voucher.lines} | {i.ledger for i in voucher.inventory}
        if voucher.party:
            referenced.add(voucher.party)
        for name in sorted(referenced):
            if _satisfied(target, "ledgers", name, created):
                continue
            report.add(REJECTS, "closure", voucher.number,
                       f"references ledger {name!r}, which no master creates and "
                       "the destination has not got",
                       f"Ledger '{name}' does not exist!")

    items = {i.name for i in export.masters.stock_items}
    for voucher in export.vouchers:
        for inv in voucher.inventory:
            if not _satisfied(target, "stock_items", inv.item, items):
                report.add(REJECTS, "closure", voucher.number,
                           f"references stock item {inv.item!r}, which no master "
                           "creates and the destination has not got",
                           f"Stock Item '{inv.item}' does not exist!")


def _check_invoice_view(export: Export, report: Report):
    for voucher in export.vouchers:
        if voucher.inventory and not voucher.is_invoice:
            report.add(REJECTS, "invoice view", voucher.number,
                       f"a {voucher.kind} voucher carries {len(voucher.inventory)} stock lines",
                       "Can't mix Stock Items and Accounting Entries")


def _check_voucher_kinds(export: Export, report: Report, target: TargetCompany):
    """A voucher type is a master like any other, and this export creates none.

    The eight kinds this connector emits are settings, not constants - somebody
    whose company calls its sales vouchers *Tax Invoice* types that into Tally
    Settings and every sales voucher carries it. Tally reserves twenty-odd
    voucher types in every company; anything outside that set was made by hand
    in the destination company, and nothing on this side can see whether it was.

    Where Tally has been asked, its own list is the answer and this says nothing
    it cannot support. Where it has not, an unreserved name is reported, because
    the alternative is finding out from *"Voucher Type 'Tax Invoice' does not
    exist."* with the import already stopped.
    """
    for voucher in export.vouchers:
        kind = (voucher.kind or "").strip()
        if target.known and target.voucher_types:
            if kind not in target.voucher_types:
                report.add(REJECTS, "voucher type", voucher.number,
                           f"{kind!r} does not exist in "
                           f"{target.name or 'the target company'}",
                           f"Voucher Type '{kind}' does not exist.")
        elif kind not in RESERVED_VOUCHER_TYPES:
            report.add(REJECTS, "voucher type", voucher.number,
                       f"{kind!r} is not one of the voucher types Tally reserves, and "
                       "this export creates none - it has to already exist in the "
                       "destination company",
                       f"Voucher Type '{kind}' does not exist.")


def _check_reimport_would_duplicate(export: Export, report: Report,
                                    target: TargetCompany):
    """Whether a second import of this file would make a second set of vouchers.

    Tally has no upsert: `ACTION="Create"` always creates. Three things can stop
    a repeat, and this export now carries one of them itself.

    **The GUID**, which every voucher here gets, derived from the ERPNext
    document so it is the same on every export of it. TallyPrime's import
    carries "Overwrite voucher when a voucher with same GUID exists"; with that
    on, a second import replaces rather than duplicates. This used to say the
    duplication "cannot be prevented from here", which was wrong - it named
    only the destination's own numbering setting, and never the identifier the
    file could have been carrying all along.

    **The voucher type** numbered Manually with Prevent Duplicates set, which is
    a setting inside the target company and why this check needs the target.

    **`ACTION="Alter"`**, which this export can send instead.

    So a missing GUID is the finding, and the target's own numbering is
    reported beside it as the fallback it now is.
    """
    # `tally_guid` derives one from the ERPNext document, so this is empty
    # unless a voucher records no document at all - which is what makes it
    # worth reporting rather than a restatement of the model.
    missing = [v for v in export.vouchers if not v.tally_guid]
    if missing:
        report.add(WRONG, "re-import", "voucher guid",
                   f"{len(missing)} vouchers carry no GUID, so Tally has "
                   "nothing to recognise them by and a second import would "
                   "create them again",
                   "(the export mints one per ERPNext document)")
    seen = {}
    for voucher in export.vouchers:
        if voucher.tally_guid:
            seen.setdefault(voucher.tally_guid, []).append(voucher.number)
    for guid, numbers in seen.items():
        if len(numbers) > 1:
            report.add(WRONG, "re-import", numbers[0],
                       f"{len(numbers)} vouchers share one GUID, so Tally would "
                       "read them as one voucher altered repeatedly",
                       f"(vouchers {', '.join(str(n) for n in numbers[:4])})")

    if not target.known or not target.duplicate_guard:
        return
    unguarded = sorted({
        v.kind for v in export.vouchers
        if target.duplicate_guard.get(v.kind) is False
    })
    for kind in unguarded:
        count = sum(1 for v in export.vouchers if v.kind == kind)
        report.add(WRONG, "re-import", kind,
                   f"{count} vouchers of a type that does not prevent duplicates, "
                   "so a second import relies on the GUID being honoured",
                   "(switch on \"Overwrite voucher when a voucher with same GUID "
                   "exists\", or set the voucher type to Manual numbering with "
                   "Prevent Duplicates)")


def _check_dates(export: Export, report: Report, target: TargetCompany):
    """A voucher dated before the books begin is refused by Tally.

    **A voucher dated after `ENDINGAT` is not, and this used to refuse it.**
    Measured against a live TallyPrime: a new company reported `BOOKSFROM`,
    `STARTINGFROM` and `ENDINGAT` all as 20250401, and a voucher dated
    2026-03-31 was sent on its own and came back `created=1`. So `ENDINGAT` is the last date the company holds data for, not
    a ceiling on what may be imported, and Tally moves it as data arrives.

    The cost of reading it as a ceiling was the whole migration: every voucher
    of a full year's books was reported as rejected,
    and `_would_be_refused` reads these findings to decide whether to send, so
    a company that would have taken all of them refused the lot. That is the
    shape of the 7,581 findings this file's `target_company` docstring already
    records, reached a second time by a different route.

    `books_from` stays, because a date before the books begin genuinely is out
    of range for Tally. It is kept as REJECTS rather than measured, and said so
    here, because nothing on this engagement has had a voucher that early to
    test it with.
    """
    if not target.known or not target.books_from:
        return
    for voucher in export.vouchers:
        if voucher.date < target.books_from:
            report.add(REJECTS, "date", voucher.number,
                       f"dated {voucher.date}, before books begin on {target.books_from}",
                       "Date is out of range.")


def _check_bill_wise(export: Export, report: Report):
    """A ledger with ISBILLWISEON obliges its vouchers to carry references.

    Tally accepts the voucher either way. Without them the amount posts to
    *On Account* and the party's ageing is unusable, which is why this is WRONG
    rather than REJECTS - nothing fails, and the books are not right.
    """
    bill_wise = {l.name for l in export.masters.ledgers if l.bill_wise}
    for voucher in export.vouchers:
        for line in voucher.lines:
            if line.ledger not in bill_wise:
                continue
            if not line.bill_refs:
                report.add(WRONG, "bill-wise", f"{voucher.number} / {line.ledger}",
                           "party ledger tracks bills but the line carries no reference",
                           "(posts to On Account)")
                continue
            total = money(sum(r.amount for r in line.bill_refs))
            if total != money(line.amount):
                report.add(WRONG, "bill-wise", f"{voucher.number} / {line.ledger}",
                           f"references total {total}, line is {money(line.amount)}",
                           "(the difference posts to On Account)")


def _check_party_flag(export: Export, report: Report):
    for voucher in export.vouchers:
        if not voucher.party:
            continue
        flagged = [l for l in voucher.lines if l.is_party]
        if not flagged:
            report.add(WRONG, "party", voucher.number,
                       f"names party {voucher.party!r} but no line is marked as the party ledger")
        elif len(flagged) > 1:
            report.add(WRONG, "party", voucher.number,
                       f"{len(flagged)} lines are marked as the party ledger")


def _money_ledgers(export: Export) -> set:
    """Ledgers Tally treats as cash or bank, walking the group tree upwards.

    A ledger may sit under a sub-group of Bank Accounts, so matching the
    immediate parent alone would miss it.
    """
    from tally_connector.utils.ledgers import reserved_group

    parents = {g.name: g.parent for g in export.masters.groups}

    def is_money(group, depth=0):
        while group and depth < 25:
            # By Tally's matching: a parent spelled `Cash In Hand` is Tally's
            # `Cash-in-Hand`, and a check comparing exact strings missed 140
            # journals Tally would have refused.
            if (reserved_group(group) or group) in CASH_BANK_GROUPS:
                return True
            group = parents.get(group)
            depth += 1
        return False

    return {l.name for l in export.masters.ledgers if is_money(l.parent)}


def _check_journal_cash(export: Export, report: Report):
    """A Journal holding a cash or bank ledger.

    Tally's Journal is for adjustments and excludes money accounts; moving money
    is what Contra, Receipt and Payment are for. 520 of the 1,222 vouchers this
    connector exported were Journals holding a bank ledger, because ERPNext's six
    journal types were all collapsed into one and every bank statement line went
    the same way.

    Reported as a refusal because the F12 option that permits it is off on a
    fresh company, which is the case worth defaulting to.
    """
    money = _money_ledgers(export)
    if not money:
        return
    for voucher in export.vouchers:
        if voucher.kind not in JOURNAL_KINDS:
            continue
        for line in voucher.lines:
            if line.ledger in money:
                report.add(REJECTS, "journal cash", voucher.number,
                           f"a Journal cannot hold the cash or bank ledger "
                           f"{line.ledger!r}; this belongs on a Contra, Receipt "
                           "or Payment",
                           "(refused unless F12 'Allow Cash Accounts in Journals' is on)")
                break


DUTY_HEADS = frozenset({"CGST", "SGST", "IGST", "Cess"})
DUTIES_GROUP = "Duties & Taxes"


def _check_gst_ledgers(export: Export, report: Report):
    """A GST ledger Tally can actually report on.

    Tally builds its GST returns from ledgers that sit under Duties & Taxes and
    declare a duty head. A ledger with the right postings and neither is an
    ordinary duty ledger as far as the returns are concerned - the money is
    right and GSTR-1 is empty, which is the worst way for this to be wrong
    because nothing anywhere says so.
    """
    for ledger in export.masters.ledgers:
        if ledger.tax_type != "GST":
            continue
        if ledger.parent != DUTIES_GROUP:
            report.add(WRONG, "gst ledger", ledger.name,
                       f"a GST ledger under {ledger.parent!r} rather than {DUTIES_GROUP!r}",
                       "(kept out of the GST returns)")
        if ledger.duty_head not in DUTY_HEADS:
            report.add(WRONG, "gst ledger", ledger.name,
                       f"declares no duty head; expected one of {sorted(DUTY_HEADS)}",
                       "(kept out of the GST returns)")


def _check_hsn(export: Export, report: Report):
    """A taxable stock item with no HSN code.

    HSN is mandatory on a GST invoice, and Tally's HSN summary is built from the
    item master. This is a gap in the ERPNext data rather than a defect in the
    export, which is why it is reported rather than filled in with a guess: an
    invented HSN code is a filing problem, not a fix.
    """
    for item in export.masters.stock_items:
        if not item.gst_applicable or item.taxability != "Taxable":
            continue
        if not str(item.hsn_code).strip():
            report.add(WRONG, "hsn", item.name,
                       "taxable stock item carries no HSN or SAC code",
                       "(missing from the HSN summary)")


def _check_cost_centres(export: Export, report: Report,
                        target: TargetCompany = None):
    """Allocations Tally will accept.

    Three rules, and each is a refusal rather than a nicety. A cost centre a
    voucher names has to exist. A ledger has to have cost centres switched on
    before it may carry an allocation at all. And the allocations on a line have
    to total the line, because Tally treats a shortfall as an unallocated
    balance and the analysis stops reconciling to the ledger.
    """
    created = {c.name for c in export.masters.cost_centres}
    enabled = {l.name for l in export.masters.ledgers if l.cost_centres_on}
    ledgers = {l.name for l in export.masters.ledgers}

    for voucher in export.vouchers:
        for line in voucher.lines:
            if not line.cost_centres:
                continue
            for allocation in line.cost_centres:
                if not _satisfied(target, "cost_centres", allocation.name, created):
                    report.add(REJECTS, "cost centre", voucher.number,
                               f"names cost centre {allocation.name!r}, which no "
                               "master creates and the destination has not got",
                               f"Cost Centre '{allocation.name}' does not exist!")
            if line.ledger in ledgers and line.ledger not in enabled:  # noqa: E501
                report.add(REJECTS, "cost centre", f"{voucher.number} / {line.ledger}",
                           "allocation on a ledger that has cost centres switched off",
                           "(refused: the ledger does not accept cost centres)")
            total = money(sum(a.amount for a in line.cost_centres))
            if total != money(line.amount):
                report.add(WRONG, "cost centre", f"{voucher.number} / {line.ledger}",
                           f"allocations total {total}, line is {money(line.amount)}",
                           "(the difference is left unallocated)")


def _check_godowns(export: Export, report: Report, target: TargetCompany = None):
    """A godown a stock line names has to exist, like any other master."""
    created = {g.name for g in export.masters.godowns}
    for voucher in export.vouchers:
        for line in voucher.inventory:
            if line.godown and not _satisfied(target, "godowns", line.godown, created):
                report.add(REJECTS, "godown", voucher.number,
                           f"names godown {line.godown!r}, which no master creates "
                           "and the destination has not got",
                           f"Godown '{line.godown}' does not exist!")


def _check_destination_maintains_inventory(export: Export, report: Report,
                                           target: TargetCompany = None):
    """Stock going into a company that keeps no stock.

    `_check_closure` cannot see this. It asks whether each stock item exists -
    and this export creates every one of them, so closure passes and Tally
    still refuses the voucher, because whether a company maintains inventory
    at all is a company setting rather than a master.

    The destination on this engagement is exactly that shape. TallyPrime's own
    export of it holds 6,029 ledgers, 59 groups and **no** stock items, stock
    groups, units or godowns, and of the 4,682 vouchers Tally wrote itself not
    one carries a stock line: the 1,141 invoices all have an empty
    ALLINVENTORYENTRIES.LIST and put the revenue on a rate-bearing ledger.
    ERPNext wants to send it 9,483 stock items.

    This warns rather than blocks, and the distinction is deliberate. A
    company with inventory switched on but nothing in it yet looks identical
    from out here, and refusing that one would be inventing a problem. What is
    certain is that somebody has to choose, so the finding names both roads:
    turn inventory on in Tally, or send these as accounting invoices.
    """
    if target is None or target.snapshot is None:
        return
    snapshot = target.snapshot
    inventory_kinds = ("stock_items", "stock_groups", "godowns", "units")
    # Unknown is not empty. If Tally did not answer about all of them, it has
    # not said inventory is off, and guessing is how a connector comes to
    # refuse a company it could not see.
    if not all(snapshot.known(kind) for kind in inventory_kinds):
        return
    if any(getattr(snapshot, kind) for kind in inventory_kinds):
        return
    if not snapshot.known("ledgers") or not snapshot.ledgers:
        # An empty company says nothing either way.
        return

    carrying = [v.number for v in export.vouchers if v.inventory]
    if not carrying:
        return
    report.add(WRONG, "destination inventory", target.name or "the destination",
               f"holds {len(snapshot.ledgers)} ledgers and no stock item, stock "
               f"group, unit or godown at all, which is what a company that "
               f"does not maintain inventory looks like - and this export "
               f"sends {len(export.masters.stock_items)} stock items on "
               f"{len(carrying)} vouchers. Either switch inventory on in that "
               f"company, or send these as accounting invoices with the "
               f"revenue on a ledger line",
               "Can't mix Stock Items and Accounting Entries")


def _check_stock_ledgers_affect_inventory(export: Export, report: Report,
                                          target: TargetCompany = None):
    """A ledger an invoice allocates stock to must affect inventory.

    Tally refuses such a voucher when the ledger's *Inventory values are
    affected* is off, and refuses it in the worst way available: `0 errors and
    1 exceptions`, naming nothing, with the rest of the file lost behind it.
    In accounting mode it never checks - it drops the stock and reports success
    - so this only ever appears on invoices, and it appeared on every one of
    them. Every invoice-mode voucher on this engagement failed for this and
    nothing could say why.

    Either this export sets the flag, or the destination already has it on. A
    ledger this file creates carries `affects_stock` from the extract; one
    Tally already holds keeps whatever the destination has, which is how a
    chart that looks complete goes on producing vouchers Tally will not take.
    """
    carrying = {}
    for voucher in export.vouchers:
        for inv in voucher.inventory:
            if inv.ledger:
                carrying.setdefault(inv.ledger, voucher.number)
    if not carrying:
        return

    ours = {l.name: l.affects_stock for l in export.masters.ledgers}
    for name, voucher_number in sorted(carrying.items()):
        if ours.get(name):
            continue
        held = None
        if target is not None and target.snapshot is not None:
            held = target.snapshot.carries_stock(name)
        if held:
            continue
        if name not in ours and held is None:
            # Tally was not asked, or did not say, and this file does not
            # create the ledger either. Refusing here would invent a problem
            # on an offline run; the closure check already covers a ledger
            # that exists nowhere.
            continue
        report.add(REJECTS, "stock ledger", name,
                   f"carries stock on {voucher_number} and does not affect "
                   "inventory, which Tally refuses without saying so",
                   "(exception, unnamed)")


def _check_duplicates(export: Export, report: Report):
    """Re-importing the same voucher number creates a second voucher."""
    for label, values in (
        ("voucher number", [(v.kind, v.number) for v in export.vouchers]),
        ("remote id", [v.remote_id for v in export.vouchers if v.remote_id]),
    ):
        for value, count in Counter(values).items():
            if count > 1:
                subject = value if isinstance(value, str) else " / ".join(value)
                report.add(WRONG, "duplicate", subject,
                           f"{label} appears {count} times in one export")

    for label, names in (
        ("ledger", [l.name for l in export.masters.ledgers]),
        ("unit", [u.symbol for u in export.masters.units]),
        ("stock item", [i.name for i in export.masters.stock_items]),
    ):
        for name, count in Counter(names).items():
            if count > 1:
                report.add(WRONG, "duplicate", name, f"{label} master defined {count} times")


def _check_group_tree(export: Export, report: Report):
    """The group tree has to be a tree.

    Tally walks a group's parents to place it, so a cycle is not a slow import -
    it is a refusal at that line and the whole file with it. The first real
    import died on exactly this: ERPNext's root `Application of Funds (Assets)`
    was created under Tally's reserved `Current Assets`, and ERPNext's own
    `Current Assets` under that root, so each was the other's parent.

    Also reported: a parent no master creates and Tally does not reserve. Tally
    has its own twenty-odd groups and a name outside that set has to come from
    this file, or the group lands nowhere.
    """
    from tally_connector.utils.ledgers import is_reserved_group

    parents = {g.name: g.parent for g in export.masters.groups}

    for name, parent in parents.items():
        if not parent or is_reserved_group(parent) or parent in parents:
            continue
        report.add(ABORTS, "group parent", name,
                   f"its parent {parent!r} is neither created here nor a group Tally "
                   "reserves, so this group lands nowhere",
                   "(Tally stops the import at this line)")

    for name in parents:
        seen, cur, steps = {name}, parents.get(name), 0
        while cur and steps < 60:
            if cur in seen:
                report.add(ABORTS, "group cycle", name,
                           f"its parents lead back to {cur!r}, so each is the other's "
                           "ancestor",
                           "(Group is already a child of this!)")
                break
            seen.add(cur)
            cur, steps = parents.get(cur), steps + 1


def _check_masters_arrive_in_order(export: Export, report: Report):
    """A master must not name a parent that comes later in the file.

    Tally creates masters in the order it reads them, so a child arriving before
    its parent is refused - *"Stock Group 'All Item Groups' does not exist!"* -
    and the file stops there. That is the second real import this took down, and
    nothing here was looking: the account groups had been depth-sorted since
    they were written, and the stock groups, cost centres and godowns had not.

    A parent that appears nowhere in the file is `_check_group_tree`'s business
    for accounts; for these three it is the same defect and is reported here,
    because Tally reserves no stock group, cost centre or godown to fall back
    on.
    """
    for label, rows in (("stock group", export.masters.stock_groups),
                        ("cost centre", export.masters.cost_centres),
                        ("godown", export.masters.godowns)):
        position = {}
        for index, row in enumerate(rows):
            position.setdefault(row.name, index)
        for index, row in enumerate(rows):
            parent = getattr(row, "parent", "")
            if not parent:
                continue
            at = position.get(parent)
            if at is None:
                report.add(ABORTS, "master parent", row.name,
                           f"its {label} parent {parent!r} is never created",
                           "(does not exist!)")
            elif at > index:
                report.add(ABORTS, "master order", row.name,
                           f"its {label} parent {parent!r} is created later in the file, "
                           "and Tally reads the file in order",
                           "(does not exist!)")


def _check_currency(export: Export, report: Report):
    """A ledger may name a currency only if something creates it.

    A Tally company holds the currencies somebody defined in it and no others -
    an Indian company out of the box has one. Naming `USD` on a ledger is a
    reference to a master, exactly like a group or a unit, and Tally answers
    *"Currency 'USD' does not exist!"* and stops the file. That is how the third
    real import died.

    This export creates no currency masters and sends every amount in the
    company's base currency, so the ordinary answer is that a ledger names none
    and `Masters.currencies` is empty. The check is written against that list
    rather than against "is it empty", so it keeps working the day currency
    masters are added - which it could not do while it reached for the list
    through a `getattr` default, because the list was not there at all.
    """
    created = {c.strip().lower() for c in export.masters.currencies or []}
    for ledger in export.masters.ledgers:
        name = (ledger.currency or "").strip()
        if not name or name.lower() in created:
            continue
        report.add(ABORTS, "currency", ledger.name,
                   f"it names the currency {name!r}, which nothing in this export "
                   "creates - a Tally company holds only the currencies defined in "
                   "it, and this cannot know which those are",
                   f"(Currency '{name}' does not exist!)")


def _every_address(export: Export):
    """Every address the export renders, with what to call it in a finding."""
    for ledger in export.masters.ledgers:
        if ledger.address:
            yield ledger.name, ledger.address
    for voucher in export.vouchers:
        if voucher.party_address:
            yield voucher.number, voucher.party_address


def _check_reserved_names(export: Export, report: Report):
    """A master Tally reads as one of its own reserved groups stops the file.

    Tally compares names ignoring case and punctuation, so `Stock In Hand` is its
    `Stock-in-Hand`. Sent as a ledger, it is a request to turn Tally's group into
    a ledger somewhere else, and a real masters file stopped at its 9,627th
    record: *"Improper change of Group Name for Ledger 'Stock-in-Hand'!"*. Sent
    as a group, it is at best a silent alter of Tally's own group - which is how
    `Cash In Hand` got through, by having the same parent - and at worst a
    re-parenting Tally refuses.
    """
    from tally_connector.utils.ledgers import reserved_group

    for kind, rows in (("group", export.masters.groups),
                       ("ledger", export.masters.ledgers)):
        for row in rows:
            tallys = reserved_group(row.name)
            if not tallys:
                continue
            report.add(ABORTS, "reserved name", row.name,
                       f"a {kind} Tally reads as its own reserved group "
                       f"{tallys!r}, because Tally ignores case and punctuation "
                       "when it matches names",
                       f"Improper change of Group Name for Ledger '{tallys}'!")


def _check_unit_symbols(export: Export, report: Report):
    """A unit symbol Tally will refuse costs the whole masters file.

    *"ERROR: Unit: Square Meter / BAD UNIT NAME"* stopped a real import at
    `Created: 17`, and `Parts Per Million` stopped an earlier one at
    `Created: 1`. Everything after that line - 335 ledgers among them - never
    reached Tally, so every voucher then failed on a ledger that does not exist
    and four rounds were spent reading voucher XML. The masters had never
    finished; the summary said `Created: 17` and that was read as done.

    Tally documents no rule. `Nos` and `BAG` were accepted, `Square Meter` and
    `Parts Per Million` were not; what the refusals have and the acceptances
    lack is a space, and length. Both are checked, because being wrong about
    which of the two it is costs another round.
    """
    from tally_connector.tally.extract_rules import UNIT_SYMBOL_MAX

    for unit in export.masters.units:
        symbol = unit.symbol or ""
        if " " in symbol:
            report.add(ABORTS, "unit symbol", symbol,
                       "a unit symbol with a space in it was refused by a real "
                       "Tally, and the file stops at that line",
                       "BAD UNIT NAME")
        elif len(symbol) > UNIT_SYMBOL_MAX:
            report.add(ABORTS, "unit symbol", symbol,
                       f"{len(symbol)} characters; Tally refused a 12-character "
                       "symbol and the file stops at that line",
                       "BAD UNIT NAME")


def _check_ledger_parents(export: Export, report: Report):
    """A ledger's group must be one this file creates or one Tally reserves.

    Same defect as `_check_group_tree`'s, one level down and never looked at:
    the group tree was checked against itself while the ledgers hanging off it
    were not. A ledger naming a group nothing creates lands nowhere, and Tally
    stops rather than inventing the group.
    """
    from tally_connector.utils.ledgers import is_reserved_group

    groups = {g.name for g in export.masters.groups}
    for ledger in export.masters.ledgers:
        parent = (ledger.parent or "").strip()
        if not parent or parent in groups or is_reserved_group(parent):
            continue
        report.add(ABORTS, "ledger parent", ledger.name,
                   f"its group {parent!r} is neither created here nor a group Tally "
                   "reserves",
                   f"Group '{parent}' does not exist!")


def _check_stock_item_parents(export: Export, report: Report,
                              target: TargetCompany = None):
    """A stock item's group must exist when the item lands.

    Either this file creates it or the destination already has it. Tally
    reserves no stock group but `Primary`, which is also what a blank parent
    means, so every other name has to come from one of those two places.

    It used to be the file alone, which was right while every master was sent
    and becomes wrong on the second run: once the destination holds the stock
    groups, `resolve` stops resending them, and a check that only knows the
    file would abort every single item over a group sitting in Tally. That is
    the same defect the ledger closure had, where it reported 2,986 missing
    ledgers that were all present, so it is fixed the same way rather than
    waiting to be found again.
    """
    groups = {g.name for g in export.masters.stock_groups} | {"Primary"}
    for item in export.masters.stock_items:
        parent = (item.parent or "").strip()
        if not parent or _satisfied(target, "stock_groups", parent, groups):
            continue
        report.add(ABORTS, "item parent", item.name,
                   f"its stock group {parent!r} is never created and the "
                   "destination has not got it",
                   f"Stock Group '{parent}' does not exist!")


def _check_registration_type(export: Export, report: Report):
    """Tally validates the GST registration type against five words.

    ERPNext's GST Category is a longer list, and the connector used to send it
    through unchanged - so `Overseas` reached a party's
    `PARTYTAXREGISTRATIONTYPE` and `Regular-SEZ`, `Deemed Exports - Refund`,
    `UIN Holders` and `Tax Deductor` would have too. It is only rendered beside
    a GSTIN, which is where the check looks.
    """
    for subject, address in _every_address(export):
        kind = (address.registration_type or "").strip()
        if not address.gstin or not kind or kind in REGISTRATION_TYPES:
            continue
        report.add(REJECTS, "registration type", subject,
                   f"{kind!r} is not a registration Tally writes "
                   f"({', '.join(REGISTRATION_TYPES)})",
                   "(Invalid GST registration type)")


def _check_state_is_indian(export: Export, report: Report):
    """A state may be named only on an address Tally will place in India.

    `STATENAME`, `PLACEOFSUPPLY` and `PRIORSTATENAME` are read against the state
    list of an Indian company, which holds Indian states - so `California` on a
    US customer is a reference to a master the company has not got. A non-India
    address carries its state as a line of the postal address instead, where
    nothing validates it.
    """
    for subject, address in _every_address(export):
        state = (address.state or "").strip()
        country = (address.country or "").strip()
        if not state or country.lower() in ("", "india"):
            continue
        report.add(REJECTS, "state", subject,
                   f"it names the state {state!r} beside the country {country!r}, and "
                   "Tally reads a state against the list an Indian company holds",
                   f"(State '{state}' does not exist!)")


# Checks that read the destination as well as the export. They take the target
# and work without it: absent one, `_satisfied` falls back to "only what this
# export creates counts", which is what they all did before.
DESTINATION_CHECKS = (_check_cost_centres, _check_godowns,
                      _check_stock_ledgers_affect_inventory,
                      _check_stock_item_parents,
                      _check_destination_maintains_inventory,
                      _check_units_are_consistent, _check_closure)

CHECKS = (
    _check_amounts_parse,
    _check_cost_centres,
    _check_godowns,
    _check_stock_ledgers_affect_inventory,
    _check_destination_maintains_inventory,
    _check_gst_ledgers,
    _check_hsn,
    _check_names_are_distinct,
    _check_journal_cash,
    _check_names,
    _check_units_are_consistent,
    _check_balance,
    _check_narration_length,
    _check_closure,
    _check_invoice_view,
    _check_bill_wise,
    _check_party_flag,
    _check_duplicates,
    _check_group_tree,
    _check_masters_arrive_in_order,
    _check_currency,
    _check_ledger_parents,
    _check_stock_item_parents,
    _check_registration_type,
    _check_state_is_indian,
    _check_unit_symbols,
    _check_reserved_names,
)

TARGET_CHECKS = (_check_voucher_kinds, _check_dates,
                 _check_reimport_would_duplicate)


def validate(export: Export, target: TargetCompany | None = None) -> Report:
    """Every check, over the whole export. Never raises."""
    target = target or TargetCompany()
    report = Report()
    for check in CHECKS:
        if check in DESTINATION_CHECKS:
            check(export, report, target)
        else:
            check(export, report)
    for check in TARGET_CHECKS:
        check(export, report, target)

    report.checked = {
        "company": export.company,
        "vouchers": len(export.vouchers),
        "ledgers": len(export.masters.ledgers),
        "units": len(export.masters.units),
        "stock_items": len(export.masters.stock_items),
        # Not a finding. None of the three severities fits: the file imports,
        # no record is refused and the books are right, because every item's
        # value is on the ledger it always posted to. What is true is that
        # Tally's stock summary will be empty, and that belongs in the facts
        # about this export rather than dressed up as a defect.
        "accounting_only": export.accounting_only,
        "stock_lines_folded": export.stock_lines_folded,
        "target_company_known": target.known,
        "destination_masters_known": bool(
            target.snapshot is not None and target.snapshot.anything_known()),
        "date_and_voucher_type_checked": bool(
            target.known and (target.books_from or target.voucher_types)
        ),
    }
    return report
