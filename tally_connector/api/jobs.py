"""
What a long-running export or import is doing, while it is doing it.

The dashboard used to poll a log whose only states were Pending and an outcome,
so a job in flight and a job that had never started looked identical - and the
only honest thing the screen could say was "Running in the background...". This
module is the spine that replaces that: a worker reports its stage and its real
counts as it goes, the log stores them, and a realtime event carries them to
whoever is watching.

Two rules the design turns on:

* **Nothing here interpolates.** A percentage is derived from a count the worker
  actually reached. A stage with no countable work reports its stage and leaves
  the number where it was, rather than easing a bar towards a number nobody
  measured. A progress bar that moves when nothing is happening is worse than no
  bar, because it is believed.

* **The log is the truth and the socket is an optimisation.** Every field the
  screen reads is committed to `Tally Export Log` before it is published, so a
  browser that missed the event - reconnected, opened later, a second person
  entirely - gets the same answer by asking. That is what makes the polling
  fallback a fallback rather than a second implementation.
"""

import frappe
from frappe.utils import cint, flt, now_datetime, time_diff_in_seconds

from tally_connector.api.export import EXPORT_ROLES, _require

# The event both applications listen on. One name for both directions: a client
# watching a log does not care whether the work is going out or coming in.
EVENT = "tally_connector:job"

# Every stage a job can report, in the order they happen. The client draws this
# list, so a stage added here appears in the UI without a second edit - and one
# that is never reached is drawn as pending rather than skipped, which is the
# distinction somebody reading a stuck job needs.
# The masters are generated **after** the vouchers, which looks wrong beside the
# fact that they must be *imported* first - and both are correct. Closure is only
# possible in this order: the extract collects every ledger the vouchers actually
# name, then builds the masters, then adds anything referenced that no master
# creates. A ledger a voucher names and nothing creates is a state that ordering
# makes unreachable. The written files still go masters-first; this is the order
# the work happens in, and the stage list is about the work.
#
# The first version of this list was written in import order, and the ordering
# test below caught it - which is the whole reason a declared sequence is worth
# asserting against rather than trusting.
EXPORT_STAGES = (
    "Queued",
    "Preparing",
    # There is no early validation stage and there never was: an export is
    # checked against Tally's rules at the end, over the bytes it actually
    # wrote, which is "Final Validation" below. "Validating" sat here unreached
    # by any code path, and once the panel started ticking only what really ran
    # it showed as a permanent grey dot - reading as a step that was skipped.
    # A declared stage nothing enters is the same defect as a ticked stage
    # nothing ran, in the other direction.
    "Processing Sales",
    "Processing Purchases",
    "Processing Payments",
    "Processing Journals",
    "Processing Bank Lines",
    "Processing Other Documents",
    "Generating Masters",
    "Generating XML",
    "Sending to Tally",
    "Final Validation",
    "Completed",
)

IMPORT_STAGES = (
    "Queued",
    "Preparing",
    "Reading File",
    "Validating",
    "Importing Masters",
    "Importing Vouchers",
    "Final Validation",
    "Completed",
)

TERMINAL = ("Success", "Failed", "Partial", "Cancelled")

# How long a job may go without writing anything before it is reported as
# stalled rather than running.
#
# A job whose worker never picked it up - no worker on the queue, a worker
# restarted, a queue that was never started - sits at `Pending` for ever, and
# the header counted it as running for ever with it. Two of those on a real
# dashboard is what prompted this: "2 Syncs Running · 0%", both queued, neither
# going anywhere, and nothing on the screen able to say so.
#
# Generous on purpose. The reporter writes on every per-cent, so a live export
# is never quiet for this long; anything that is has stopped.
STALE_AFTER_SECONDS = 10 * 60


class JobReporter:
    """Writes a worker's progress where the dashboard can see it.

    Deliberately tolerant: a reporter that throws takes the export down with it,
    and losing the books because the progress bar failed is the wrong trade. A
    write that fails is logged and swallowed, and the job carries on - the final
    state is written on a path of its own.
    """

    def __init__(self, doctype, name, stages):
        self.doctype = doctype
        self.name = name
        self.stages = list(stages)
        self.processed = 0
        self.total = 0
        self.warnings = 0
        self.stage = "Queued"
        # Which stages this run actually entered. The screen used to tick every
        # stage before the current one, and since the last stage is "Completed"
        # that meant a finished download-only export showed a green tick beside
        # "Sending to Tally" - work that never happened. A progress display that
        # claims work is worse than one that omits it.
        self.reached = []
        self._last_published = None

    # ── the worker's side ───────────────────────────────────────────────────

    def start(self, total=0):
        self.total = cint(total)
        self.stage = "Preparing"
        self.reached = ["Queued", "Preparing"]
        self._write(status="Running", stage="Preparing", started_at=now_datetime())

    def at(self, stage, processed=None, total=None, warnings=None):
        """Move to a stage, optionally with new counts.

        `processed` is what the worker has actually got through. Passing None
        leaves it where it was, which is the honest thing to do in a stage that
        counts nothing - generating XML from a model already in memory has no
        rows to tick.
        """
        self.stage = stage
        if stage not in self.reached:
            self.reached.append(stage)
        if processed is not None:
            self.processed = cint(processed)
        if total is not None:
            self.total = cint(total)
        if warnings is not None:
            self.warnings = cint(warnings)
        self._write(stage=stage)

    def tick(self, n=1):
        """One more record done.

        Publishes at most once per per-cent, so eleven thousand invoices are a
        hundred events rather than eleven thousand. The count itself is exact;
        it is the *reporting* that is sampled, which is the right way round.
        """
        self.processed += n
        pct = self._percent()
        if pct != self._last_published:
            self._write()

    def warn(self, n=1):
        self.warnings += n

    def summarise(self, findings):
        """What the warnings actually were, stored beside how many there were."""
        try:
            frappe.db.set_value(self.doctype, self.name, "validation_summary",
                                frappe.as_json(findings or []))
            frappe.db.commit()
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Tally Connector: findings summary")

    def finish(self, status, error=None, records=None, failed=0):
        """The last word on this job, written whatever happened before it."""
        if status in ("Success", "Partial") and "Completed" not in self.reached:
            self.reached.append("Completed")
        values = {
            "status": status,
            "stage": "Completed" if status in ("Success", "Partial") else status,
            "stages_reached": ", ".join(self.reached),
            "progress": 100 if status in ("Success", "Partial") else self._percent(),
            "processed": self.processed,
            "total_records": self.total,
            "warnings": self.warnings,
            "finished_at": now_datetime(),
            "records_failed": cint(failed),
        }
        if records is not None:
            field = "records_exported" if self.doctype == "Tally Export Log" else "records_imported"
            values[field] = cint(records)
        if error:
            values["error_log"] = str(error)[:4000]
        self._commit(values)
        self._publish(final=True)

    # ── plumbing ────────────────────────────────────────────────────────────

    def _percent(self):
        if self.total <= 0:
            # A stage index is a real measure of where the job is when there is
            # nothing to count, and it never runs backwards.
            try:
                return int(self.stages.index(self.stage) / max(len(self.stages) - 1, 1) * 100)
            except ValueError:
                return 0
        return min(int(self.processed / self.total * 100), 99)

    def _write(self, **extra):
        values = {
            "stage": self.stage,
            "stages_reached": ", ".join(self.reached),
            "progress": self._percent(),
            "processed": self.processed,
            "total_records": self.total,
            "warnings": self.warnings,
        }
        values.update(extra)
        self._commit(values)
        self._publish()

    def _commit(self, values):
        try:
            # `modified` moves on purpose: it is this job's heartbeat. Without
            # it there is no way to tell a long export that is working from one
            # whose worker died an hour ago, and the dashboard counted both as
            # running. No Version rows are written - neither log tracks changes.
            frappe.db.set_value(self.doctype, self.name, values)
            frappe.db.commit()
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Tally Connector: progress write")

    def _publish(self, final=False):
        self._last_published = self._percent()
        try:
            frappe.publish_realtime(
                EVENT, self.payload(), doctype=self.doctype, docname=self.name,
            )
        except Exception:
            # A site with no socketio still has to export. The polling fallback
            # is what makes that acceptable rather than a silent degradation.
            if final:
                frappe.log_error(frappe.get_traceback(), "Tally Connector: progress publish")

    def payload(self):
        return {
            "name": self.name,
            "doctype": self.doctype,
            "kind": "export" if self.doctype == "Tally Export Log" else "import",
            "stage": self.stage,
            "progress": self._percent(),
            "processed": self.processed,
            "total": self.total,
            "warnings": self.warnings,
        }


def _stalled(doc):
    """Has this job gone quiet for longer than a running job ever would?

    Measured from the last time it wrote anything - `modified`, which every
    progress write now moves - rather than from when it started, so a long
    export that is genuinely working is never called stalled.
    """
    if doc.status in TERMINAL:
        return False
    last = doc.get("modified") or doc.get("started_at")
    if not last:
        return False
    try:
        return time_diff_in_seconds(now_datetime(), last) > STALE_AFTER_SECONDS
    except Exception:
        return False


def _row(doc, kind):
    """One job as the dashboard reads it, from either log."""
    started, finished = doc.get("started_at"), doc.get("finished_at")
    duration = None
    if started and finished:
        duration = flt(time_diff_in_seconds(finished, started), 1)
    records = doc.get("records_exported" if kind == "export" else "records_imported")
    return {
        "name": doc.name,
        "kind": kind,
        "doctype": "Tally Export Log" if kind == "export" else "Tally Import Log",
        "stages": list(EXPORT_STAGES if kind == "export" else IMPORT_STAGES),
        "title": doc.get("export_type") or doc.get("import_type") or "",
        "company": doc.get("company") or "",
        "status": doc.status,
        "stage": doc.get("stage") or ("Completed" if doc.status in TERMINAL else "Queued"),
        # What actually ran. Empty for a log written before this existed, and the
        # screen falls back to ticking by position there rather than showing a
        # job with no stages at all.
        "reached": [s.strip() for s in (doc.get("stages_reached") or "").split(",") if s.strip()],
        "progress": cint(doc.get("progress")),
        "processed": cint(doc.get("processed")),
        "total": cint(doc.get("total_records")),
        "warnings": cint(doc.get("warnings")),
        "records": cint(records),
        "failed": cint(doc.get("records_failed")),
        "from_date": str(doc.get("from_date") or "") or None,
        "to_date": str(doc.get("to_date") or "") or None,
        "started_at": str(started) if started else None,
        "finished_at": str(finished) if finished else None,
        "duration": duration,
        "error": doc.get("error_log") or "",
        # What the warnings were, not only how many. A number on its own tells
        # somebody there is a problem and nothing about it.
        "findings": frappe.parse_json(doc.get("validation_summary") or "[]"),
        "running": doc.status not in TERMINAL,
        # Not finished, and not moving either. The screen shows these apart from
        # live work rather than counting them as running, and offers to cancel
        # them - because the only thing worse than a stuck job is a dashboard
        # insisting it is fine.
        "stalled": _stalled(doc),
        "last_seen": str(doc.get("modified")) if doc.get("modified") else None,
    }


_LIST_FIELDS = {
    "Tally Export Log": [
        "name", "export_type", "company", "status", "stage", "stages_reached",
        "progress", "processed",
        "total_records", "warnings", "validation_summary",
        "records_exported", "records_failed", "from_date",
        "to_date", "started_at", "finished_at", "error_log", "creation", "modified",
    ],
    "Tally Import Log": [
        "name", "import_type", "company", "status", "stage", "stages_reached",
        "progress", "processed",
        "total_records", "warnings", "records_imported", "records_failed",
        "started_at", "finished_at", "error_log", "creation", "modified",
    ],
}


@frappe.whitelist(methods=["GET", "POST"])
def running():
    """Every job still in flight, for the header indicator.

    This is how a browser learns about a job it did not start - a colleague's
    export, or its own from before a reload. The socket carries the fine-grained
    updates once a job is known; discovering one is this.
    """
    _require(EXPORT_ROLES)
    out = []
    for doctype, kind in (("Tally Export Log", "export"), ("Tally Import Log", "import")):
        for doc in frappe.get_all(
            doctype, filters={"status": ["in", ["Pending", "Running"]]},
            fields=_LIST_FIELDS[doctype], order_by="creation desc", limit=20,
        ):
            out.append(_row(doc, kind))
    return sorted(out, key=lambda r: r["started_at"] or "", reverse=True)


@frappe.whitelist(methods=["GET", "POST"])
def status(name, kind="export"):
    """One job, by name. The polling fallback and the history detail read this."""
    _require(EXPORT_ROLES)
    doctype = "Tally Export Log" if kind == "export" else "Tally Import Log"
    doc = frappe.get_doc(doctype, name)
    row = _row(doc, kind)
    row["files"] = _files(doctype, name)
    return row


def _files(doctype, name):
    return frappe.get_all(
        "File",
        filters={"attached_to_doctype": doctype, "attached_to_name": name},
        fields=["file_name", "file_url", "file_size"], order_by="file_name asc",
    )


@frappe.whitelist(methods=["GET", "POST"])
def history(company=None, kind=None, status_filter=None, limit=50, start=0):
    """Every export and import, newest first - the Sync History page.

    Both logs in one list because that is the question somebody actually has:
    "what has this connector done", not "what has it done in one direction".
    """
    _require(EXPORT_ROLES)
    out = []
    wanted = ("export", "import") if kind in (None, "", "all") else (kind,)
    for doctype, this_kind in (("Tally Export Log", "export"), ("Tally Import Log", "import")):
        if this_kind not in wanted:
            continue
        filters = {}
        if company:
            filters["company"] = company
        if status_filter and status_filter != "all":
            filters["status"] = status_filter
        for doc in frappe.get_all(
            doctype, filters=filters, fields=_LIST_FIELDS[doctype],
            order_by="creation desc", limit=cint(limit) + cint(start),
        ):
            row = _row(doc, this_kind)
            row["creation"] = str(doc.creation)
            out.append(row)
    out.sort(key=lambda r: r["creation"], reverse=True)
    return out[cint(start): cint(start) + cint(limit)]


@frappe.whitelist(methods=["GET", "POST"])
def detail(name, kind="export"):
    """A job with everything the history page's expanded row shows."""
    _require(EXPORT_ROLES)
    row = status(name, kind)
    row["tally_response"] = frappe.db.get_value(row["doctype"], name, "tally_response") \
        if row["doctype"] == "Tally Export Log" else None
    return row


@frappe.whitelist(methods=["POST"])
def cancel(name, kind="export", reason=None):
    """Stop counting a job that is not going anywhere.

    Only a job that has already gone quiet can be cancelled - the threshold is
    the same one that marks it stalled - because a button that can kill a
    running export is a way to lose an hour of work by mis-clicking. Nothing is
    deleted: the log stays, saying it was cancelled and by whom, which is what
    somebody asks about a week later.

    This does not reach into the queue. A worker that later picks the job up
    will run it and write its own outcome over this; the point here is that a
    dashboard stops claiming work is in progress when it is not.
    """
    _require(EXPORT_ROLES)
    doctype = "Tally Export Log" if kind == "export" else "Tally Import Log"
    doc = frappe.get_doc(doctype, name)
    if doc.status in TERMINAL:
        return _row(doc, kind)
    if not _stalled(doc):
        frappe.throw(
            "This job is still reporting progress. Wait for it to finish, or for "
            f"{STALE_AFTER_SECONDS // 60} minutes of silence, before cancelling it."
        )
    note = (reason or "").strip() or "Cancelled from the dashboard after it stopped reporting."
    frappe.db.set_value(doctype, name, {
        "status": "Cancelled",
        "stage": "Cancelled",
        "finished_at": now_datetime(),
        "error_log": f"{note} (by {frappe.session.user})",
    })
    frappe.db.commit()
    return _row(frappe.get_doc(doctype, name), kind)


@frappe.whitelist(methods=["GET", "POST"])
def notifications(since=None, limit=20):
    """What has happened that somebody would want to know about.

    Built from the job logs rather than from Frappe's `Notification Log`,
    because everything worth telling somebody here is a job outcome and a second
    store would be a second thing that can disagree with the first. A job that
    failed, finished with warnings, or stalled is news; a clean success is
    listed but is not the reason the bell lights up.

    `since` is the caller's own last-seen timestamp. Unread is counted against
    it rather than stored per user, so nothing here has to be written to mark a
    thing read - which is the whole of the state this needs.
    """
    _require(EXPORT_ROLES)
    rows = []
    for doctype, kind in (("Tally Export Log", "export"), ("Tally Import Log", "import")):
        for doc in frappe.get_all(
            doctype, fields=_LIST_FIELDS[doctype],
            order_by="creation desc", limit=cint(limit),
        ):
            row = _row(doc, kind)
            when = row["finished_at"] or str(doc.get("modified") or doc.creation)
            if row["stalled"]:
                tone, text = "warning", "stopped reporting and may need cancelling"
            elif row["status"] == "Failed":
                tone, text = "error", row["error"][:160] or "failed"
            elif row["status"] == "Partial":
                tone, text = "warning", (row["error"][:160]
                                         or f"finished with {row['warnings']} warnings")
            elif row["status"] in ("Pending", "Running"):
                tone, text = "info", f"{row['stage'].lower()} - {row['progress']}%"
            else:
                tone, text = "success", f"{row['records']:,} objects written"
            rows.append({
                "name": row["name"], "kind": kind, "title": row["title"],
                "company": row["company"], "status": row["status"],
                "stalled": row["stalled"], "tone": tone, "text": text, "at": when,
            })

    rows.sort(key=lambda r: r["at"] or "", reverse=True)
    rows = rows[:cint(limit)]
    # A clean success is listed and does not ring the bell. Somebody who wants
    # to know every export finished has the history page.
    unread = sum(
        1 for r in rows
        if r["tone"] in ("error", "warning") and (not since or (r["at"] or "") > since)
    )
    return {"items": rows, "unread": unread, "now": str(now_datetime())}
