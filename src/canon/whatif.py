"""What-if replay: change a rule, replay a month, count what moves.

Shadow running answers "does the new engine agree with the old one". This
answers a different and, day to day, more useful question: **if we make this
change, what happens to real decisions?**

Today that question gets answered with judgement. Someone reads the diff, thinks
about it, and says it looks contained. Then a roster build produces four hundred
new hard findings on a Tuesday morning and the change gets reverted by people who
still do not know which of the three edits caused it.

The replay turns that into a number, and then into a name. For every captured
transaction it runs the baseline ruleset and the candidate ruleset, classifies
the difference, and attributes it: which rule emitted the new finding, whether
that rule itself changed, and if it did not, which upstream rule change reached
it through the derived namespace.

Two of the outputs matter as much as the flip count.

**Inert changes.** Rules whose content changed but which moved no decision
anywhere in the corpus. That is the evidence that a change is safe, and it is
the thing nobody can currently produce.

**Never fired.** Rules that did not fire once across the whole corpus, in either
ruleset. On a long lived estate that list is usually longer than anyone expects,
and every entry is either dead weight or a rule that has quietly stopped working.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple, Sequence

from .engine import Engine
from .registry import Manifest, diff_manifests
from .rules import RuleSet
from .shadow import ShadowCase
from .trace import Decision

__all__ = ["Flip", "ImpactReport", "Replay", "WhatIf", "replay"]

UNCHANGED = "unchanged"
NEWLY_BLOCKED = "newly_blocked"
NEWLY_ALLOWED = "newly_allowed"
FINDINGS_CHANGED = "findings_changed"
DERIVED_CHANGED = "derived_changed"
ERRORED = "errored"

FLIP_KINDS = (NEWLY_BLOCKED, NEWLY_ALLOWED, FINDINGS_CHANGED, DERIVED_CHANGED,
              ERRORED)


# --------------------------------------------------------------------------
# One case
# --------------------------------------------------------------------------


@dataclass
class Flip:
    """One transaction whose outcome moved."""

    case_id: str
    kind: str
    before_codes: list[str] = field(default_factory=list)
    after_codes: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    before_ok: bool = True
    after_ok: bool = True
    before_severity: str = "info"
    after_severity: str = "info"
    derived_moved: dict[str, dict[str, Any]] = field(default_factory=dict)
    responsible: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    key: Mapping[str, Any] = field(default_factory=dict)

    @property
    def moved(self) -> bool:
        return self.kind != UNCHANGED

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "key": dict(self.key),
            "before": {"codes": self.before_codes, "ok": self.before_ok,
                       "severity": self.before_severity},
            "after": {"codes": self.after_codes, "ok": self.after_ok,
                      "severity": self.after_severity},
            "added": self.added,
            "removed": self.removed,
            "derived_moved": self.derived_moved,
            "responsible": self.responsible,
            "error": self.error,
        }

    def summary(self) -> str:
        blame = ", ".join(
            entry["rule_id"] + ("" if entry["rule_changed"]
                                else " (via " + "/".join(entry["via"]) + ")")
            for entry in self.responsible) or "unattributed"
        movement = []
        if self.added:
            movement.append("+" + ",".join(self.added))
        if self.removed:
            movement.append("-" + ",".join(self.removed))
        if self.derived_moved and not movement:
            movement.append("derived only")
        return (f"{self.case_id:<16} {self.kind:<17} "
                f"{' '.join(movement):<52} {blame}")


class Replay(NamedTuple):
    """One case replayed against both rulesets.

    Named rather than a bare tuple on purpose. A bare tuple let a bad edit turn
    ``return flip, before, after`` into a five element return, and nothing
    complained until the call site tried to unpack it.
    """

    flip: Flip
    before: Decision | None
    after: Decision | None


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


@dataclass
class ImpactReport:
    baseline_id: str = ""
    baseline_version: str = ""
    candidate_id: str = ""
    candidate_version: str = ""
    rule_diff: dict[str, Any] = field(default_factory=dict)
    results: list[Flip] = field(default_factory=list)
    fired_baseline: dict[str, int] = field(default_factory=dict)
    fired_candidate: dict[str, int] = field(default_factory=dict)
    all_rule_ids: list[str] = field(default_factory=list)

    # -- headline ---------------------------------------------------------

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def flips(self) -> list[Flip]:
        return [r for r in self.results if r.moved]

    @property
    def unchanged(self) -> int:
        return self.total - len(self.flips)

    @property
    def flip_rate(self) -> float:
        return (len(self.flips) / self.total) if self.total else 0.0

    def by_kind(self) -> dict[str, int]:
        counts = {kind: 0 for kind in FLIP_KINDS}
        for flip in self.flips:
            counts[flip.kind] = counts.get(flip.kind, 0) + 1
        return {kind: count for kind, count in counts.items() if count}

    def by_code(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for flip in self.flips:
            for code in flip.added:
                out.setdefault(code, {"newly_raised": 0, "no_longer_raised": 0})
                out[code]["newly_raised"] += 1
            for code in flip.removed:
                out.setdefault(code, {"newly_raised": 0, "no_longer_raised": 0})
                out[code]["no_longer_raised"] += 1
        return dict(sorted(out.items(),
                           key=lambda kv: -(kv[1]["newly_raised"]
                                            + kv[1]["no_longer_raised"])))

    def by_rule(self) -> dict[str, dict[str, Any]]:
        """Every rule implicated in a flip, and whether it is the cause."""
        out: dict[str, dict[str, Any]] = {}
        for flip in self.flips:
            for entry in flip.responsible:
                rule_id = entry["rule_id"]
                row = out.setdefault(rule_id, {
                    "raised": 0, "withdrew": 0, "rule_changed": entry["rule_changed"],
                    "via": set(),
                })
                if entry["direction"] == "added":
                    row["raised"] += 1
                else:
                    row["withdrew"] += 1
                row["via"].update(entry["via"])
        for row in out.values():
            row["via"] = sorted(row["via"])
        return dict(sorted(out.items(),
                           key=lambda kv: -(kv[1]["raised"] + kv[1]["withdrew"])))

    # -- the two lists worth having ---------------------------------------

    def changed_rule_ids(self) -> list[str]:
        diff = self.rule_diff
        return sorted(
            [entry["rule_id"] for entry in diff.get("changed", [])]
            + [entry["rule_id"] for entry in diff.get("added", [])]
            + [entry["rule_id"] for entry in diff.get("removed", [])]
        )

    def inert_changes(self) -> list[str]:
        """Rules that changed but moved nothing across the whole corpus.

        Read this list before the flip list. A change that appears here has
        evidence behind it, which is a stronger position than a change nobody
        can say anything about.
        """
        implicated: set[str] = set()
        for flip in self.flips:
            for entry in flip.responsible:
                implicated.add(entry["rule_id"])
                implicated.update(entry["via"])
        return [rule_id for rule_id in self.changed_rule_ids()
                if rule_id not in implicated]

    def never_fired(self) -> list[str]:
        """Rules that never fired in either run.

        Either dead weight, or a rule that has quietly stopped matching
        anything. Both are worth a conversation, and neither is visible today.
        """
        return [rule_id for rule_id in self.all_rule_ids
                if not self.fired_baseline.get(rule_id)
                and not self.fired_candidate.get(rule_id)]

    def coverage(self) -> dict[str, Any]:
        fired = [rule_id for rule_id in self.all_rule_ids
                 if self.fired_baseline.get(rule_id)
                 or self.fired_candidate.get(rule_id)]
        return {
            "rules": len(self.all_rule_ids),
            "fired_at_least_once": len(fired),
            "never_fired": self.never_fired(),
        }

    # -- output -----------------------------------------------------------

    def samples(self, kind: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        chosen = [f for f in self.flips if kind is None or f.kind == kind]
        return [f.to_dict() for f in chosen[:limit]]

    def to_dict(self, sample_limit: int = 10) -> dict[str, Any]:
        return {
            "baseline": {"id": self.baseline_id, "version": self.baseline_version},
            "candidate": {"id": self.candidate_id, "version": self.candidate_version},
            "rule_diff": self.rule_diff,
            "total": self.total,
            "unchanged": self.unchanged,
            "flipped": len(self.flips),
            "flip_rate": round(self.flip_rate, 6),
            "by_kind": self.by_kind(),
            "by_code": self.by_code(),
            "by_rule": self.by_rule(),
            "changed_rules": self.changed_rule_ids(),
            "inert_changes": self.inert_changes(),
            "coverage": self.coverage(),
            "samples": self.samples(limit=sample_limit),
        }

    def render(self, sample_limit: int = 8) -> str:
        diff = self.rule_diff
        lines = [
            f"what-if: {self.baseline_id} v{self.baseline_version} "
            f"-> v{self.candidate_version}",
            f"  rules added {len(diff.get('added', []))}, "
            f"removed {len(diff.get('removed', []))}, "
            f"changed {len(diff.get('changed', []))}, "
            f"unchanged {diff.get('unchanged', 0)}",
            "",
            f"replayed {self.total} transactions",
            f"  unchanged   {self.unchanged}",
            f"  moved       {len(self.flips)}  ({self.flip_rate:.2%})",
        ]
        for kind, count in self.by_kind().items():
            lines.append(f"    {kind:<18} {count}")

        codes = self.by_code()
        if codes:
            lines.append("")
            lines.append("findings that moved")
            for code, counts in list(codes.items())[:15]:
                lines.append(
                    f"  {code:<34} newly raised {counts['newly_raised']:<6} "
                    f"no longer raised {counts['no_longer_raised']}")

        rules = self.by_rule()
        if rules:
            lines.append("")
            lines.append("attributed to")
            for rule_id, row in list(rules.items())[:15]:
                cause = "changed in this proposal" if row["rule_changed"] \
                    else ("unchanged, reached via " + "/".join(row["via"])
                          if row["via"] else "unchanged, cause not in the diff")
                lines.append(
                    f"  {rule_id:<14} raised {row['raised']:<6} "
                    f"withdrew {row['withdrew']:<6} {cause}")

        inert = self.inert_changes()
        lines.append("")
        if inert:
            lines.append("changed but moved nothing in this corpus")
            for rule_id in inert:
                lines.append(f"  {rule_id}")
        else:
            lines.append("every changed rule moved at least one decision")

        never = self.never_fired()
        if never:
            lines.append("")
            lines.append(f"never fired in {self.total} transactions "
                         f"({len(never)} of {len(self.all_rule_ids)} rules)")
            for rule_id in never:
                lines.append(f"  {rule_id}")

        if self.flips:
            lines.append("")
            lines.append("sample of moved decisions")
            for flip in self.flips[:sample_limit]:
                lines.append("  " + flip.summary())

        return "\n".join(lines)


# --------------------------------------------------------------------------
# The replayer
# --------------------------------------------------------------------------


class WhatIf:
    """Replays captured transactions against two rulesets and diffs them."""

    def __init__(self, baseline: RuleSet, candidate: RuleSet, *,
                 strict_facts: bool = False,
                 compare_derived: bool = True) -> None:
        self.baseline = baseline
        self.candidate = candidate
        self.compare_derived = compare_derived
        self.baseline_engine = Engine(baseline, strict_facts=strict_facts)
        self.candidate_engine = Engine(candidate, strict_facts=strict_facts)
        self.diff = diff_manifests(Manifest.of(baseline), Manifest.of(candidate))
        self._changed = set(
            [entry["rule_id"] for entry in self.diff["changed"]]
            + [entry["rule_id"] for entry in self.diff["added"]]
            + [entry["rule_id"] for entry in self.diff["removed"]]
        )

    # -- attribution ------------------------------------------------------

    def _upstream_changed(self, ruleset: RuleSet, rule_id: str) -> list[str]:
        """Changed rules reachable from ``rule_id`` through derived facts.

        This is the answer to "FTL-010 started firing but FTL-010 did not
        change". It did not; the rule that computes the limit it compares
        against did, and the dependency graph already knows that.
        """
        found: list[str] = []
        seen: set[str] = {rule_id}
        queue: list[str] = [rule_id]
        while queue:
            current = queue.pop()
            for path in ruleset.rule_derived_reads.get(current, ()):
                for producer in ruleset.producers.get(path, ()):
                    if producer in seen:
                        continue
                    seen.add(producer)
                    if producer in self._changed:
                        found.append(producer)
                    queue.append(producer)
        return sorted(found)

    def _blame(self, decision: Decision, ruleset: RuleSet, code: str,
               direction: str) -> dict[str, Any] | None:
        for trace in decision.traces:
            if trace.emitted and trace.emitted.get("code") == code:
                changed = trace.rule_id in self._changed
                return {
                    "code": code,
                    "direction": direction,
                    "rule_id": trace.rule_id,
                    "rule_changed": changed,
                    "via": [] if changed else self._upstream_changed(ruleset, trace.rule_id),
                }
        return None

    # -- one case ---------------------------------------------------------

    def compare(self, case: ShadowCase) -> Replay:
        """Replay one case. Returns the classification and both decisions."""
        flip = Flip(case_id=case.id, kind=UNCHANGED, key=dict(case.key))
        try:
            before = self.baseline_engine.evaluate(
                dict(case.facts), key=case.key, client=case.client, as_of=case.as_of)
            after = self.candidate_engine.evaluate(
                dict(case.facts), key=case.key, client=case.client, as_of=case.as_of)
        except Exception as exc:  # noqa: BLE001 - a bad case must not stop a run
            flip.kind = ERRORED
            flip.error = f"{type(exc).__name__}: {exc}"
            return Replay(flip, None, None)

        flip.before_codes = sorted(before.codes())
        flip.after_codes = sorted(after.codes())
        flip.before_ok = before.ok
        flip.after_ok = after.ok
        flip.before_severity = before.severity
        flip.after_severity = after.severity
        flip.added = [c for c in flip.after_codes if c not in flip.before_codes]
        flip.removed = [c for c in flip.before_codes if c not in flip.after_codes]

        if self.compare_derived:
            names = set(before.derived) | set(after.derived)
            for name in sorted(names):
                old = before.derived.get(name)
                new = after.derived.get(name)
                if old != new:
                    flip.derived_moved[name] = {"before": old, "after": new}

        for code in flip.added:
            entry = self._blame(after, self.candidate, code, "added")
            if entry:
                flip.responsible.append(entry)
        for code in flip.removed:
            entry = self._blame(before, self.baseline, code, "removed")
            if entry:
                flip.responsible.append(entry)

        if flip.added or flip.removed:
            if before.ok and not after.ok:
                flip.kind = NEWLY_BLOCKED
            elif not before.ok and after.ok:
                flip.kind = NEWLY_ALLOWED
            else:
                flip.kind = FINDINGS_CHANGED
        elif flip.derived_moved:
            flip.kind = DERIVED_CHANGED

        return Replay(flip, before, after)

    # -- the run ----------------------------------------------------------

    def run(self, cases: Iterable[ShadowCase]) -> ImpactReport:
        report = ImpactReport(
            baseline_id=self.baseline.id,
            baseline_version=self.baseline.version,
            candidate_id=self.candidate.id,
            candidate_version=self.candidate.version,
            rule_diff=self.diff,
        )
        fired_before: dict[str, int] = defaultdict(int)
        fired_after: dict[str, int] = defaultdict(int)

        for case in cases:
            replayed = self.compare(case)
            report.results.append(replayed.flip)
            if replayed.before is None or replayed.after is None:
                continue
            for trace in replayed.before.traces:
                if trace.fired:
                    fired_before[trace.rule_id] += 1
            for trace in replayed.after.traces:
                if trace.fired:
                    fired_after[trace.rule_id] += 1

        report.fired_baseline = dict(fired_before)
        report.fired_candidate = dict(fired_after)
        report.all_rule_ids = sorted(
            {rule.id for rule in self.baseline.rules}
            | {rule.id for rule in self.candidate.rules})
        return report


def replay(baseline: RuleSet, candidate: RuleSet,
           cases: Iterable[ShadowCase], **kwargs: Any) -> ImpactReport:
    """Convenience wrapper."""
    return WhatIf(baseline, candidate, **kwargs).run(cases)
