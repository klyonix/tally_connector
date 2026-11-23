"""
One answer to "may this go to Tally?", assembled from everything that knows.

The certification is deliberately a separate object from the validation report.
The validator answers "is this file well formed for Tally"; the snapshot and
resolution answer "does this destination already hold these masters"; the
connection answers "is Tally there and is it the right company". A person
pressing Export needs the three together, with one verdict at the bottom.

**BLOCKED means no production XML.** That is the whole point of the object: a
verdict nothing acts on is a report, and this connector already had one of
those.

The section names and the shape follow the brief so the output can be read
against it directly.

Pure: no frappe, no network. The caller gathers the inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tally_connector.tally.snapshot import company_matches
from tally_connector.tally.validate import ABORTS, REJECTS

PASS, FAIL, UNKNOWN, SKIPPED = "PASS", "FAIL", "UNKNOWN", "SKIPPED"
# Tally accepts it and the books are wrong. Real, and not a reason to refuse
# the file - a taxable item with no HSN imports perfectly and then drops the
# voucher into Uncertain Transactions. Calling that PASS would be dishonest and
# calling it FAIL means nothing ever ships, so it is its own word.
WARN = "WARN"

READY = "READY FOR TALLY"
BLOCKED = "BLOCKED - FIX REQUIRED"

# A section that is UNKNOWN does not block. Tally being switched off has never
# been a reason to refuse an offline export, and making it one would break the
# workflow this connector exists to support.
BLOCKING_RESULTS = (FAIL,)


@dataclass
class Section:
    name: str
    result: str
    detail: str = ""
    counts: dict = field(default_factory=dict)

    @property
    def blocks(self) -> bool:
        return self.result in BLOCKING_RESULTS


@dataclass
class Certification:
    company: str = ""
    destination: str = ""
    sections: list[Section] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, name, result, detail="", counts=None):
        self.sections.append(Section(name, result, detail, counts or {}))

    @property
    def blocking(self) -> list[Section]:
        return [s for s in self.sections if s.blocks]

    @property
    def verdict(self) -> str:
        return BLOCKED if (self.blocking or self.errors) else READY

    @property
    def ready(self) -> bool:
        return self.verdict == READY

    def to_dict(self) -> dict:
        return {
            "company": self.company,
            "destination": self.destination,
            "verdict": self.verdict,
            "ready": self.ready,
            "sections": [
                {"name": s.name, "result": s.result, "detail": s.detail,
                 "counts": s.counts}
                for s in self.sections
            ],
            "errors": self.errors,
            "warnings": self.warnings,
        }

    def as_text(self) -> str:
        """The report as the brief writes it, for a log or a paste into a ticket."""
        lines = ["TALLY EXPORT CERTIFICATION",
                 f"Destination Company: {self.destination or '(not set)'}",
                 f"ERPNext Company: {self.company or '(not set)'}", ""]
        for section in self.sections:
            lines.append(f"{section.name}: {section.result}")
            if section.counts:
                for key, value in section.counts.items():
                    lines.append(f"    {key}: {value}")
            if section.detail:
                lines.append(f"    {section.detail}")
        lines.append("")
        lines.append(f"Errors: {len(self.errors)}")
        for message in self.errors[:20]:
            lines.append(f"    {message}")
        lines.append(f"Warnings: {len(self.warnings)}")
        for message in self.warnings[:20]:
            lines.append(f"    {message}")
        lines.append("")
        # Named explicitly, because a section can fail on a finding this counts
        # as a warning - a taxable item with no HSN does not stop the import
        # and does put the voucher into Uncertain Transactions - and "Errors: 0"
        # above "BLOCKED" reads as a contradiction unless the reason is said.
        if self.blocking:
            lines.append("Blocked by: "
                         + ", ".join(s.name for s in self.blocking))
        warned = [s.name for s in self.sections if s.result == WARN]
        if warned:
            lines.append("Accepted with warnings: " + ", ".join(warned))
        lines.append(f"Overall: {self.verdict}")
        return "\n".join(lines)


def _severity_counts(report, checks):
    hits = [f for f in report.findings if f.check in checks]
    return hits


def _verdict(hits):
    """A section fails on what Tally would refuse, and warns on the rest.

    The validator has said since it was written that `ABORTS` and `REJECTS` cost
    you the import while `WRONG` costs you the accuracy, and the certification
    used to fail a section on any finding at all - which made it stricter than
    the validator it reads. On a real company that would have blocked a clean
    291-voucher push over six notes about voucher types not preventing
    duplicates, which the sync ledger already prevents.
    """
    if any(f.severity in (ABORTS, REJECTS) for f in hits):
        return FAIL
    return WARN if hits else PASS


def _file(cert, hits):
    """Blocking findings are errors; the rest are warnings."""
    for f in hits:
        line = f"{f.subject}: {f.detail}"
        if f.severity in (ABORTS, REJECTS):
            cert.errors.append(line)
        else:
            cert.warnings.append(line)


def certify(*, erpnext_company, tally_company, report, resolution=None,
            snapshot=None, reachable=None, counts=None) -> Certification:
    """Everything known about this export, as one verdict.

    `report` is the validator's. `resolution` and `snapshot` are absent when
    Tally was not asked, and their sections then read UNKNOWN rather than PASS -
    an unasked question is not a passed one.
    """
    cert = Certification(company=erpnext_company, destination=tally_company)
    counts = counts or {}

    # Connectivity and company mapping.
    if reachable is None:
        cert.add("Connectivity", UNKNOWN, "Tally was not asked.")
        cert.add("Company Mapping", UNKNOWN, "Tally was not asked.")
    elif not reachable:
        cert.add("Connectivity", UNKNOWN,
                 "Tally could not be reached. An offline export is unaffected; "
                 "a push cannot be made.")
        cert.add("Company Mapping", UNKNOWN, "Tally could not be reached.")
    else:
        cert.add("Connectivity", PASS)
        ok, why = company_matches(snapshot, tally_company) if snapshot else (True, "")
        if ok and snapshot and snapshot.companies is None:
            cert.add("Company Mapping", UNKNOWN,
                     "Tally did not list its companies, so the name could not "
                     "be checked. A mismatch imports nothing and reports success.")
        elif ok:
            cert.add("Company Mapping", PASS)
        else:
            cert.add("Company Mapping", FAIL, why)
            cert.errors.append(why)

    # Voucher types and Tally's own structures.
    kinds = _severity_counts(report, {"voucher type"})
    cert.add("Voucher Types", _verdict(kinds),
             "; ".join(f.detail for f in kinds[:3]))
    _file(cert, kinds)

    reserved = _severity_counts(report, {"reserved name", "ledger parent",
                                         "item parent", "group tree"})
    cert.add("System Masters", _verdict(reserved),
             "; ".join(f.detail for f in reserved[:3]))
    _file(cert, reserved)

    # Master resolution, per kind.
    def resolution_section(label, kinds_wanted, check_names):
        findings = _severity_counts(report, check_names)
        row = {}
        if resolution is not None:
            for kind in kinds_wanted:
                summary = resolution.summary().get(kind, {})
                for action, n in summary.items():
                    row[action.title()] = row.get(action.title(), 0) + n
        conflicts = ([d for d in resolution.conflicts if d.kind in kinds_wanted]
                     if resolution is not None else [])
        row["Unresolved"] = len(findings)
        blocking = [f for f in findings if f.severity in (ABORTS, REJECTS)]
        if blocking or conflicts:
            result = FAIL
        elif resolution is None:
            result = UNKNOWN
        else:
            result = WARN if findings else PASS
        detail = "; ".join(
            [d.reason for d in conflicts[:2]] + [f.detail for f in findings[:2]])
        if resolution is None and not findings:
            detail = detail or "Tally was not asked what it already holds."
        cert.add(label, result, detail, row)
        cert.errors.extend(f"{d.name}: {d.reason}" for d in conflicts)
        _file(cert, findings)

    resolution_section("UOM", ["units"], {"unit", "unit symbol"})
    resolution_section("Ledgers", ["ledgers", "groups"], {"closure", "name collision"})
    resolution_section("Parties", ["ledgers"], {"party"})
    resolution_section("Stock Items", ["stock_items", "stock_groups"], {"item parent"})

    # GST.
    gst = _severity_counts(report, {"gst ledger", "hsn", "registration type", "state"})
    cert.add("GST", _verdict(gst), "; ".join(f.detail for f in gst[:3]))
    _file(cert, gst)

    # Transactions.
    txn = _severity_counts(report, {"date", "invoice view", "journal cash",
                                    "cost centre", "godown"})
    cert.add("Transaction Validation", _verdict(txn),
             "; ".join(f.detail for f in txn[:3]))
    _file(cert, txn)

    balance = _severity_counts(report, {"balance", "amount"})
    cert.add("Debit/Credit Validation", _verdict(balance),
             "; ".join(f.detail for f in balance[:3]))
    _file(cert, balance)

    bills = _severity_counts(report, {"bill-wise"})
    cert.add("Bill Allocation Validation", _verdict(bills),
             "; ".join(f.detail for f in bills[:3]))
    _file(cert, bills)

    dupes = _severity_counts(report, {"duplicate", "re-import"})
    cert.add("Duplicate Validation", _verdict(dupes),
             "; ".join(f.detail for f in dupes[:3]))
    _file(cert, dupes)

    # Anything blocking the validator found that no section above claimed.
    claimed = {"voucher type", "reserved name", "ledger parent", "item parent",
               "group tree", "unit", "unit symbol", "closure", "name collision",
               "party", "gst ledger", "hsn", "registration type", "state",
               "date", "invoice view", "journal cash", "cost centre", "godown",
               "balance", "amount", "bill-wise", "duplicate", "re-import"}
    leftover = [f for f in report.blocking if f.check not in claimed]
    if leftover:
        cert.add("Other", FAIL,
                 "; ".join(f"{f.check}: {f.detail}" for f in leftover[:3]))
        cert.errors.extend(f"{f.subject}: {f.detail}" for f in leftover)

    if counts:
        cert.add("Volume", PASS, "", {k: v for k, v in counts.items()
                                      if isinstance(v, int)})
    return cert
