"""
Has this document been sent to Tally, and what happened to it?

Nothing could answer that before. `Tally Export Log` is job-level and carries no
field naming a source document, so every duplicate-prevention question was
unanswerable: pushing the same invoice twice produced two vouchers, an hourly
auto-sync re-pushed the same day up to twenty-four times, and a request that
timed out after Tally had imported it was indistinguishable from one that never
arrived.

One row per (ERPNext doctype, document, ERPNext company, Tally company). Two
Tally companies are two rows, because a document sent to both really has been
sent twice.

**The outcome vocabulary is the point**, and `Unknown Result` is the member that
matters. It means the request reached Tally and its answer could not be read.
Tally may hold the voucher. Calling that `Failed` invites the retry that
duplicates it, and calling it `Created` loses it. It is its own state, it is
never retried blindly, and clearing it means looking in Tally.

**The payload hash is what makes a re-run cheap and safe.** A document whose
payload has not changed since a `Created` is not sent again. A document whose
payload *has* changed is a different question - an amendment - and is reported
rather than quietly re-created.
"""

from __future__ import annotations

import hashlib

import frappe
from frappe import _
from frappe.utils import now_datetime

PENDING = "Pending"
CREATED = "Created"
ALTERED = "Altered"
EXISTING = "Existing"
CONFLICT = "Conflict"
FAILED = "Failed"
UNKNOWN = "Unknown Result"
# ERPNext has cancelled or amended the document and Tally still holds the
# voucher. A statement about a disagreement, not about an action taken - this
# connector does not cancel in Tally, because only a submitted document is ever
# sent there and withdrawing one is a person's decision made in Tally.
STALE = "Stale"

OUTCOMES = (PENDING, CREATED, ALTERED, EXISTING, STALE, CONFLICT, FAILED,
            UNKNOWN)

# Outcomes that mean Tally holds this document's accounting.
IN_TALLY = (CREATED, ALTERED, EXISTING, STALE)
# Outcomes safe to send again without asking Tally anything first.
#
# **`PENDING` is deliberately not among them.** A pending row means this
# connector began sending the document and never learnt what became of it, and
# resending that is how 191 purchase vouchers came to be posted two and three
# times into a customer's books. The reasoning that put it here was that a
# voucher which never reached Tally must be retried - true, and it assumed the
# ledger could tell "never sent" from "sent, outcome unknown". It cannot: both
# look like Pending.
#
# So the two errors are not symmetrical and this picks the recoverable one. A
# voucher left out is visible in the reconciliation by name and can be sent
# with one press. A voucher posted twice overstates an expense, and this
# connector cannot delete from Tally to undo it.
RESENDABLE = (FAILED, CONFLICT)

# What a person has to look at rather than what a retry will fix.
NEEDS_A_PERSON = (PENDING, UNKNOWN)

DOCTYPE = "Tally Sync Record"


def payload_hash(xml: str) -> str:
    """What was sent, as a short stable digest.

    Over the voucher's own XML rather than the whole file, so one voucher
    changing does not make every other voucher look changed.
    """
    return hashlib.sha256((xml or "").encode("utf-8")).hexdigest()[:32]


def _filters(source_doctype, source_name, erpnext_company, tally_company):
    return {
        "source_doctype": source_doctype,
        "source_name": source_name,
        "erpnext_company": erpnext_company or "",
        "tally_company": tally_company or "",
    }


def record_for(source_doctype, source_name, erpnext_company, tally_company):
    """The existing record for this document and destination, or None."""
    name = frappe.db.get_value(
        DOCTYPE, _filters(source_doctype, source_name, erpnext_company,
                          tally_company), "name")
    return frappe.get_doc(DOCTYPE, name) if name else None


def note_attempt(source_doctype, source_name, erpnext_company, tally_company,
                 *, voucher_type="", voucher_number="", tally_guid="",
                 payload=""):
    """Record that we are about to send this, before it is sent.

    Written first on purpose. A row created only after a reply never exists for
    the request that got no reply, which is exactly the case that needs one.
    """
    key = (source_doctype, source_name, erpnext_company, tally_company)
    doc = record_for(*key)
    if doc:
        return _attempt(doc, voucher_type, voucher_number, tally_guid, payload)

    doc = frappe.new_doc(DOCTYPE)
    doc.update(_filters(*key))
    # Another push may insert this record between the read above and the
    # insert below. The unique key refuses the second insert, and the record
    # the other push wrote is the one to update.
    frappe.db.savepoint("tally_sync_record")
    try:
        return _attempt(doc, voucher_type, voucher_number, tally_guid, payload)
    except frappe.UniqueValidationError:
        frappe.db.rollback(save_point="tally_sync_record")
        frappe.clear_last_message()
        return _attempt(record_for(*key), voucher_type, voucher_number,
                        tally_guid, payload)


def _attempt(doc, voucher_type, voucher_number, tally_guid, payload):
    doc.voucher_type = voucher_type or doc.voucher_type
    doc.voucher_number = voucher_number or doc.voucher_number
    doc.tally_guid = tally_guid or doc.tally_guid
    doc.payload_hash = payload_hash(payload) if payload else doc.payload_hash
    doc.outcome = doc.outcome or PENDING
    doc.attempts = (doc.attempts or 0) + 1
    doc.last_attempt_at = now_datetime()
    doc.flags.ignore_permissions = True
    doc.save()
    return doc


def note_outcome(doc, outcome, message="", tally_voucher_id=""):
    """What Tally said. `doc` is what `note_attempt` returned."""
    if outcome not in OUTCOMES:
        frappe.throw(_("{0} is not a sync outcome.").format(repr(outcome)))
    doc.outcome = outcome
    doc.message = (message or "")[:1000]
    if tally_voucher_id:
        doc.tally_voucher_id = tally_voucher_id
    doc.flags.ignore_permissions = True
    doc.save()
    return doc


def already_sent(source_doctype, source_name, erpnext_company, tally_company,
                 payload=""):
    """Whether sending this again would duplicate what Tally already holds.

    Returns (skip, why). Three answers rather than two, because the middle one
    is the whole reason this module exists.

    - Tally holds it and nothing has changed -> skip.
    - Tally holds it and the payload has changed -> do not skip, and say so:
      this is an amendment, and creating a second voucher for it is wrong in a
      different way from creating a duplicate.
    - The last attempt ended Unknown -> **skip**, and say why. Somebody has to
      look in Tally. Sending again is the one action guaranteed to be wrong if
      the first one landed.
    """
    doc = record_for(source_doctype, source_name, erpnext_company, tally_company)
    if not doc:
        return False, ""
    if doc.outcome == UNKNOWN:
        return True, (
            f"{source_name} was sent once and Tally's answer could not be read, "
            "so whether it arrived is unknown. Check Tally for voucher "
            f"{doc.voucher_number or source_name!r} before sending it again; "
            "sending now would duplicate it if the first one landed.")
    if doc.outcome in IN_TALLY:
        if payload and doc.payload_hash and doc.payload_hash != payload_hash(payload):
            return False, (
                f"{source_name} is already in Tally and has changed since. It "
                "is sent as an alteration rather than a second voucher.")
        return True, f"{source_name} is already in Tally ({doc.outcome})."
    return False, ""


def outcome_from_push(result) -> tuple[str, str]:
    """Tally's reply, as one of the outcomes above.

    The three states `push_to_tally` distinguishes map straight onto this, which
    is why it distinguishes them.
    """
    if result.get("unknown"):
        return UNKNOWN, "; ".join(result.get("errors") or [])
    if not result.get("reached"):
        return FAILED, result.get("response") or "Tally could not be reached."
    if not result.get("success"):
        return FAILED, "; ".join(result.get("errors") or [])
    if result.get("created"):
        return CREATED, ""
    if result.get("altered"):
        return ALTERED, ""
    return EXISTING, "Tally accepted the request and reported nothing written."


def summary(erpnext_company=None, tally_company=None) -> dict:
    """Counts per outcome, for the certification report."""
    filters = {}
    if erpnext_company:
        filters["erpnext_company"] = erpnext_company
    if tally_company:
        filters["tally_company"] = tally_company
    rows = frappe.get_all(DOCTYPE, filters=filters, fields=["outcome", "count(*) as n"],
                          group_by="outcome")
    return {r["outcome"]: r["n"] for r in rows}


def unsent(vouchers, erpnext_company, tally_company, source_doctype=None):
    """The vouchers that still need sending, and why the rest do not.

    Returns (to_send, skipped) where `skipped` is a list of (voucher, reason).
    This is where idempotency actually bites: a voucher Tally already holds is
    left out of the file rather than sent again and deduplicated afterwards by
    a TallyPrime setting nobody here can see.

    Filtering before the push rather than recording after it is deliberate. One
    file produces one aggregate answer - `Created: n` - which cannot say *which*
    vouchers it counted, so a per-voucher outcome derived from it would be a
    guess. What goes in the file is exactly what we intend to create, and the
    answer is then about that set.
    """
    vouchers = list(vouchers)
    wanted = {}
    for voucher in vouchers:
        name = getattr(voucher, "remote_id", None) or getattr(voucher, "number", "")
        if name:
            wanted[(source_doctype or _doctype_for(voucher), name)] = None

    # One read for the whole file, not one per voucher. An export of ten
    # thousand vouchers asking ten thousand times is the shape of slowness that
    # only shows up on a customer's data, and `test_bulk_reads` exists to catch
    # exactly this - it caught this.
    known = {}
    if wanted:
        rows = frappe.get_all(
            DOCTYPE,
            filters={
                "source_name": ["in", sorted({n for _d, n in wanted})],
                "erpnext_company": erpnext_company or "",
                "tally_company": tally_company or "",
            },
            fields=["source_doctype", "source_name", "outcome",
                    "voucher_number", "payload_hash"],
            limit_page_length=0)
        for row in rows:
            known[(row.source_doctype, row.source_name)] = row

    to_send, skipped = [], []
    for voucher in vouchers:
        name = getattr(voucher, "remote_id", None) or getattr(voucher, "number", "")
        if not name:
            to_send.append(voucher)
            continue
        row = known.get((source_doctype or _doctype_for(voucher), name))
        if row is None:
            to_send.append(voucher)
        elif row.outcome == UNKNOWN:
            skipped.append((voucher, (
                f"{name} was sent once and Tally's answer could not be read, "
                "so whether it arrived is unknown. Check Tally for voucher "
                f"{row.voucher_number or name!r} before sending it again; "
                "sending now would duplicate it if the first one landed.")))
        elif row.outcome in IN_TALLY:
            skipped.append((voucher, f"{name} is already in Tally ({row.outcome})."))
        elif row.outcome == PENDING:
            # Sending began and no outcome was ever recorded, so whether Tally
            # took it is not known. Resending on that basis posted 191
            # purchase vouchers two and three times into a customer's books,
            # and nothing here can delete from Tally to undo it.
            skipped.append((voucher, (
                f"{name} was sent and no outcome was recorded, so whether it "
                "arrived is unknown. Check Tally for voucher "
                f"{row.voucher_number or name!r}; sending now would duplicate "
                "it if the first one landed.")))
        elif row.outcome in RESENDABLE:
            to_send.append(voucher)
        else:
            # An outcome this function has not been taught about. Leaving it
            # out is the recoverable mistake: a voucher missing from Tally is
            # named in the reconciliation, a voucher posted twice is not.
            skipped.append((voucher, (
                f"{name} carries the outcome {row.outcome!r}, which this "
                "reader does not know how to act on.")))
    return to_send, skipped


# Tally's voucher kinds back to the ERPNext doctype that produced them. Only
# used to key the sync record, so an unrecognised kind keys on the kind itself
# rather than guessing at a doctype that may not exist.
_DOCTYPE_BY_KIND = {
    "Sales": "Sales Invoice",
    "Credit Note": "Sales Invoice",
    "Purchase": "Purchase Invoice",
    "Debit Note": "Purchase Invoice",
    "Receipt": "Payment Entry",
    "Payment": "Payment Entry",
    "Contra": "Payment Entry",
    "Journal": "Journal Entry",
}


def _doctype_for(voucher):
    named = getattr(voucher, "source_doctype", None)
    if named:
        return named
    return _DOCTYPE_BY_KIND.get(getattr(voucher, "kind", ""), "Tally Voucher")


def note_pending(vouchers, erpnext_company, tally_company):
    """Record that these are about to be sent, before they are sent.

    This is what makes the ledger safe against a reply that never comes. The
    row exists from the moment the request leaves, so a run that dies mid-batch
    leaves `Pending` rows rather than no rows at all - and `Pending` is not
    resendable, so nothing is posted twice on the strength of a gap.

    `note_attempt` has said "written first on purpose" since it was written.
    Nothing called it first.
    """
    written = []
    for voucher in vouchers:
        name = getattr(voucher, "remote_id", None) or getattr(voucher, "number", "")
        if not name:
            continue
        written.append(note_attempt(
            _doctype_for(voucher), name, erpnext_company, tally_company,
            voucher_type=getattr(voucher, "kind", ""),
            voucher_number=getattr(voucher, "number", ""),
            tally_guid=getattr(voucher, "tally_guid", "")).name)
    return written


def record_sent(vouchers, erpnext_company, tally_company, result):
    """What happened to the vouchers that were in the file.

    One aggregate answer covers the whole file, so every voucher in it gets the
    same outcome. That is honest: Tally does not say which of them it counted,
    and inventing a per-voucher verdict would be a guess dressed as a record.
    """
    outcome, message = outcome_from_push(result)
    written = []
    for voucher in vouchers:
        name = getattr(voucher, "remote_id", None) or getattr(voucher, "number", "")
        if not name:
            continue
        row = note_attempt(
            _doctype_for(voucher), name, erpnext_company, tally_company,
            voucher_type=getattr(voucher, "kind", ""),
            voucher_number=getattr(voucher, "number", ""),
            tally_guid=getattr(voucher, "tally_guid", ""))
        note_outcome(row, outcome, message, result.get("last_voucher_id", ""))
        written.append(row.name)
    return {"outcome": outcome, "records": len(written)}


def stale(erpnext_company=None, tally_company=None):
    """Documents Tally still holds that ERPNext no longer stands behind.

    Two ways a document stops being current, and ERPNext expresses them
    differently.

    **Cancelled** is `docstatus = 2`. The document is still there and its
    accounting is withdrawn.

    **Amended** is a *different document* - ERPNext cancels the original and
    creates a successor naming it in `amended_from`. So the original is
    cancelled too, and the successor is a document this has never seen, with a
    name of its own and therefore a GUID of its own. That is why an amendment
    is a cancellation plus a creation here rather than an alteration: altering
    the original's voucher would leave a voucher named after a document ERPNext
    has withdrawn, carrying content from one it has not.

    Nothing here writes to Tally, and nothing anywhere does about this: the
    connector reports the disagreement and a person settles it in Tally, where
    they can see what the voucher affects.
    """
    filters = {"outcome": ["in", list(IN_TALLY)]}
    if erpnext_company:
        filters["erpnext_company"] = erpnext_company
    if tally_company:
        filters["tally_company"] = tally_company
    rows = frappe.get_all(
        DOCTYPE, filters=filters,
        fields=["name", "source_doctype", "source_name", "voucher_type",
                "voucher_number", "tally_guid", "outcome"],
        limit_page_length=0)

    # One query per doctype rather than one per row.
    by_doctype = {}
    for row in rows:
        by_doctype.setdefault(row.source_doctype, []).append(row)

    out = []
    for doctype, group in by_doctype.items():
        names = [r.source_name for r in group]
        try:
            live = {d.name: d for d in frappe.get_all(
                doctype, filters={"name": ["in", names]},
                fields=["name", "docstatus"], limit_page_length=0)}
            successors = {}
            if frappe.get_meta(doctype).has_field("amended_from"):
                for d in frappe.get_all(
                        doctype, filters={"amended_from": ["in", names]},
                        fields=["name", "amended_from"], limit_page_length=0):
                    successors[d.amended_from] = d.name
        except Exception:
            # A doctype this bench has not got is not a reason to lose the rest.
            continue
        for row in group:
            doc = live.get(row.source_name)
            if doc is None:
                out.append((row, "deleted", ""))
            elif doc.docstatus == 2:
                out.append((row, "amended" if row.source_name in successors
                            else "cancelled", successors.get(row.source_name, "")))
    return out


def mark_stale(rows):
    """Record the disagreement on the sync record, without acting on it.

    So it is visible in a list somebody already reads rather than only in a
    report they have to ask for. `rows` is what `stale()` returned.
    """
    marked = 0
    for row, reason, successor in rows:
        doc = frappe.get_doc(DOCTYPE, row.name)
        if doc.outcome == STALE:
            continue
        doc.outcome = STALE
        doc.message = (
            f"ERPNext has {reason} {row.source_name}"
            + (f", replaced by {successor}" if successor else "")
            + ". Tally still holds the voucher.")[:1000]
        doc.flags.ignore_permissions = True
        doc.save()
        marked += 1
    return marked
