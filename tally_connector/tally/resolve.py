"""
What to do about each master: reuse it, create it, alter it, or stop.

Tally has no upsert, and treating it as though it had is the root cause behind
most of this connector's history. `ACTION="Create"` on a master Tally already
holds is not a no-op: a real import answered `Created: 9,626  Altered: 9,694`,
which is nine and a half thousand of a customer's own masters rewritten.

So each master gets a decision, made here against the destination snapshot and
nowhere else:

  REUSE    - the destination has it. Emit nothing. This is the common case and
             the one that was missing.
  CREATE   - the destination has not got it. Emit it.
  ALTER    - the destination has it and ours differs in a way this connector
             owns. Emit it as an alteration, and only where policy allows.
  CONFLICT - the destination has it and the difference is not ours to settle.
             Stop and say so.
  SYSTEM   - Tally's own. Never redefined, whatever anything else says.
  UNKNOWN  - Tally did not answer about this master type. Fall back to the old
             behaviour and say that is what is happening.

**UNKNOWN is deliberately not CREATE.** An unanswered probe used to be
indistinguishable from an empty company, and reading it as empty is how a chart
of accounts gets overwritten by a connector that could not see it.

**The only difference this connector claims to own is the parent.** Everything
else a Tally master carries - its opening balance, its GST registration, its
credit period, whatever an accountant has set by hand - is theirs. A difference
there is a CONFLICT to report, not an alteration to make. That is a deliberately
narrow claim, and widening it is a decision somebody should have to take.

Pure: no frappe, no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tally_connector.tally.extract_rules import tally_key
from tally_connector.utils.ledgers import is_reserved_group

REUSE, CREATE, ALTER, CONFLICT, SYSTEM, UNKNOWN = (
	"reuse",
	"create",
	"alter",
	"conflict",
	"system",
	"unknown",
)

# What a decision means for the file: whether the master is emitted, and how.
EMITS = {CREATE: "Create", ALTER: "Alter", UNKNOWN: "Create"}

# Which snapshot collection answers for which part of the export.
KIND_FOR = {
	"groups": "groups",
	"ledgers": "ledgers",
	"units": "units",
	"stock_groups": "stock_groups",
	"stock_items": "stock_items",
	"godowns": "godowns",
	"cost_centres": "cost_centres",
}

# Policy: what this connector is allowed to alter on a master that exists.
# Off by default, because the safe answer to "the destination differs" is to
# leave the destination alone and say so.
ALTER_NOTHING = "nothing"
ALTER_PARENT = "parent"
ALTER_POLICIES = (ALTER_NOTHING, ALTER_PARENT)


@dataclass
class Decision:
	kind: str  # which collection: ledgers, units, ...
	name: str  # the master's name as we would send it
	action: str  # REUSE / CREATE / ALTER / CONFLICT / SYSTEM / UNKNOWN
	reason: str = ""
	theirs: str = ""  # what the destination holds, where that matters
	ours: str = ""  # what we would have sent

	@property
	def emit(self) -> str:
		"""The ACTION attribute for this master, or "" to send nothing."""
		return EMITS.get(self.action, "")


@dataclass
class Resolution:
	decisions: list[Decision] = field(default_factory=list)

	def add(self, *args, **kwargs):
		self.decisions.append(Decision(*args, **kwargs))

	def by_action(self, action) -> list[Decision]:
		return [d for d in self.decisions if d.action == action]

	@property
	def conflicts(self) -> list[Decision]:
		return self.by_action(CONFLICT)

	def emitted(self, kind: str) -> dict[str, str]:
		"""{name: ACTION} for the masters of this kind that go in the file."""
		return {d.name: d.emit for d in self.decisions if d.kind == kind and d.emit}

	def counts(self) -> dict:
		out = {}
		for d in self.decisions:
			out[d.action] = out.get(d.action, 0) + 1
		return out

	def summary(self) -> dict:
		"""Per kind, what happens to it. The certification report reads this."""
		out: dict[str, dict] = {}
		for d in self.decisions:
			row = out.setdefault(d.kind, {})
			row[d.action] = row.get(d.action, 0) + 1
		return out


def _named(masters, attr="name"):
	for master in masters or ():
		name = (getattr(master, attr, "") or "").strip()
		if name:
			yield name, master


def resolve(masters, snapshot, alter_policy=ALTER_NOTHING) -> Resolution:
	"""One decision per master in the export, against what the destination has.

	`masters` is the `Masters` the extract produced; `snapshot` is what the
	destination answered. Nothing here writes, renders or talks to anything.
	"""
	out = Resolution()

	def decide(kind, name, parent, is_group_name=False):
		# Tally's own structures are mapping targets and are never redefined,
		# whatever the snapshot says - this is checked before the snapshot is
		# consulted, so it holds even when Tally did not answer.
		if is_group_name and is_reserved_group(name):
			out.add(
				kind,
				name,
				SYSTEM,
				reason="Tally owns this group. It is a mapping target, not something to create.",
			)
			return
		if not snapshot.known(kind):
			out.add(
				kind,
				name,
				UNKNOWN,
				reason=f"Tally did not answer about {kind.replace('_', ' ')}, "
				"so this is sent as a Create as it always was.",
			)
			return
		held = snapshot.holds(kind, name)
		if held is None:
			out.add(kind, name, CREATE)
			return
		theirs = (held.parent or "").strip()
		ours = (parent or "").strip()
		if not ours or tally_key(theirs) == tally_key(ours):
			out.add(kind, name, REUSE, theirs=held.name)
			return
		# The destination has it somewhere else. Re-parenting an existing
		# master is what Tally refused as "Improper change of Group Name", and
		# it is a decision about somebody's chart rather than a detail.
		if alter_policy == ALTER_PARENT:
			out.add(
				kind,
				name,
				ALTER,
				theirs=theirs,
				ours=ours,
				reason="moved to the group this export says it belongs to",
			)
			return
		out.add(
			kind,
			name,
			CONFLICT,
			theirs=theirs,
			ours=ours,
			reason=(
				f"Tally holds this under {theirs!r} and this export "
				f"would put it under {ours!r}. Re-parenting a master "
				"somebody else maintains is not this connector's to "
				"decide."
			),
		)

	for name, group in _named(getattr(masters, "groups", None)):
		decide("groups", name, getattr(group, "parent", ""), is_group_name=True)
	for name, ledger in _named(getattr(masters, "ledgers", None)):
		decide("ledgers", name, getattr(ledger, "parent", ""))
	for name, _unit in _named(getattr(masters, "units", None), attr="symbol"):
		decide("units", name, "")
	for name, sg in _named(getattr(masters, "stock_groups", None)):
		decide("stock_groups", name, getattr(sg, "parent", ""))
	for name, item in _named(getattr(masters, "stock_items", None)):
		decide("stock_items", name, getattr(item, "parent", ""))
	for name, godown in _named(getattr(masters, "godowns", None)):
		decide("godowns", name, getattr(godown, "parent", ""))
	for name, centre in _named(getattr(masters, "cost_centres", None)):
		decide("cost_centres", name, getattr(centre, "parent", ""))
	return out


def findings(resolution: Resolution):
	"""Resolution conflicts as validator findings, so one report carries both.

	Returned as plain tuples rather than `Finding` objects, so this module
	stays free of the validator and can be read on its own.
	"""
	for decision in resolution.conflicts:
		yield ("master conflict", decision.name, decision.reason)
