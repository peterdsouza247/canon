"""Interactive editing: incremental re-evaluation for a planner at a screen.

A planner drags a duty, swaps a crew member, extends a sector. They expect the
legality panel to update immediately, and they expect it to tell them what their
edit just broke. This is the workload RETE is famous for, so it deserves a
straight answer rather than a defence of the architecture.

**The short version.** Canon does incremental re-evaluation at rule granularity,
and the invalidation key is the set of fact paths each rule *actually read* last
time, which the trace already records. If nothing a rule read has changed, its
verdict is unchanged. That is not a heuristic. Evaluation is pure and
deterministic, so identical inputs mean an identical control path and an
identical conclusion. A guard that short circuited on its first term never read
the later terms, so changes to those terms cannot move it.

Which means the provenance machinery, built to answer "which rule did this",
turns out to be exactly the invalidation structure an incremental engine needs.
We did not have to build a second one, and the two cannot drift apart.

**Where this differs from RETE, in both directions.**

Coarser: RETE invalidates partial matches inside a rule; we invalidate whole
rules. That matters less than it sounds here, because Canon rules are small by
construction. One condition, one outcome, no ``else``. The constraint that makes
rules reviewable is the same constraint that makes rule level invalidation
adequate.

Better: the incremental path and the from scratch path are the same code.
``Session.verify()`` re-evaluates everything and asserts the incremental answer
matches, and you can call it on every edit in development and on a sample in
production. A stale RETE network is a class of bug you cannot audit away.

Better: because nothing is mutated, ``preview`` and ``score`` can evaluate
candidate edits without committing them, in parallel, with no interference. A
planner comparing twelve crew members for one slot is the normal case, and one
working memory makes that awkward.

Better: the planner does not only want to know what is illegal now. They want to
know what their edit caused. Every finding in a ``Delta`` carries the fact paths
behind it and the chain of rules it travelled through.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .engine import Engine, ReuseCache, _touched
from .rules import Finding
from .trace import Decision, RuleTrace

__all__ = ["Session", "Delta", "diff_facts", "apply_changes"]


# --------------------------------------------------------------------------
# Fact editing and diffing
# --------------------------------------------------------------------------


def _assign(document: Any, path: str, value: Any) -> None:
    segments = path.split(".")
    cursor = document
    for segment in segments[:-1]:
        if isinstance(cursor, list):
            cursor = cursor[int(segment)]
        else:
            cursor = cursor.setdefault(segment, {})
    last = segments[-1]
    if isinstance(cursor, list):
        cursor[int(last)] = value
    else:
        cursor[last] = value


def _merge(base: dict[str, Any], changes: Mapping[str, Any]) -> None:
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def apply_changes(facts: Mapping[str, Any],
                  changes: Mapping[str, Any]) -> dict[str, Any]:
    """Return a new fact document with ``changes`` applied.

    Keys may be dotted paths (``duty.end_utc``, ``flight.roster.1.rank``) or
    top level names whose value is a partial document to merge. Both forms show
    up in a real editor: a form field sends a path, a drag and drop sends a
    subtree.
    """
    updated = copy.deepcopy(dict(facts))
    for key, value in changes.items():
        if "." in key:
            _assign(updated, key, value)
        elif isinstance(value, dict) and isinstance(updated.get(key), dict):
            _merge(updated[key], value)
        else:
            updated[key] = value
    return updated


def diff_facts(old: Any, new: Any, prefix: str = "",
               out: set[str] | None = None) -> set[str]:
    """Fact paths that differ, in the same notation the rules use.

    Element level changes inside a collection are reported as
    ``flight.roster[*].rank`` so they line up with what a rule recorded reading.
    A change to the collection's length is reported as ``flight.roster``, which
    is deliberately coarser: adding a crew member can affect any rule that looks
    at any member.
    """
    found = set() if out is None else out
    if isinstance(old, dict) and isinstance(new, dict):
        for key in set(old) | set(new):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in old or key not in new:
                found.add(child)
            else:
                diff_facts(old[key], new[key], child, found)
    elif isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            found.add(prefix)
        else:
            for before, after in zip(old, new):
                diff_facts(before, after, prefix + "[*]", found)
    elif old != new:
        found.add(prefix)
    return found


# --------------------------------------------------------------------------
# The result of one edit
# --------------------------------------------------------------------------


@dataclass
class Delta:
    """What one edit did, and why."""

    changed_paths: list[str] = field(default_factory=list)
    newly_raised: list[dict[str, Any]] = field(default_factory=list)
    no_longer_raised: list[dict[str, Any]] = field(default_factory=list)
    still_raised: list[str] = field(default_factory=list)
    derived_moved: dict[str, dict[str, Any]] = field(default_factory=dict)
    ok_before: bool = True
    ok_after: bool = True
    rules_recomputed: list[str] = field(default_factory=list)
    rules_reused: int = 0
    full_evaluation: bool = False
    micros: float = 0.0

    @property
    def moved(self) -> bool:
        return bool(self.newly_raised or self.no_longer_raised
                    or self.derived_moved)

    @property
    def work_avoided(self) -> float:
        total = len(self.rules_recomputed) + self.rules_reused
        return (self.rules_reused / total) if total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed_paths": self.changed_paths,
            "newly_raised": self.newly_raised,
            "no_longer_raised": self.no_longer_raised,
            "still_raised": self.still_raised,
            "derived_moved": self.derived_moved,
            "ok_before": self.ok_before,
            "ok_after": self.ok_after,
            "rules_recomputed": self.rules_recomputed,
            "rules_reused": self.rules_reused,
            "work_avoided": round(self.work_avoided, 4),
            "full_evaluation": self.full_evaluation,
            "micros": round(self.micros, 1),
        }

    def render(self) -> str:
        lines = [f"changed {', '.join(self.changed_paths) or 'nothing'}"]
        if not self.moved:
            lines.append("  no change to legality")
        for entry in self.newly_raised:
            because = entry["because"]
            cause = ", ".join(because["paths"]) or "a knock on effect"
            if because["via_rules"]:
                cause += " via " + ", ".join(because["via_rules"])
            lines.append(f"  now failing  [{entry['severity']}] {entry['code']}")
            lines.append(f"               {entry['message']}")
            lines.append(f"               because {cause}")
        for entry in self.no_longer_raised:
            lines.append(f"  now clear    {entry['code']}")
        for name, movement in self.derived_moved.items():
            lines.append(f"  {name}: {movement['before']} -> {movement['after']}")
        lines.append(
            f"  {len(self.rules_recomputed)} rules re-evaluated, "
            f"{self.rules_reused} reused ({self.work_avoided:.0%} avoided), "
            f"{self.micros:.0f}us"
            + ("  [full evaluation]" if self.full_evaluation else ""))
        return "\n".join(lines)


# --------------------------------------------------------------------------
# The session
# --------------------------------------------------------------------------


class Session:
    """A live editing session over one set of facts.

    The session holds the facts and the last decision. The engine stays
    stateless: every call is still a pure function of the facts it is handed.
    That is what lets ``preview`` and ``score`` explore alternatives without a
    snapshot or an undo, and what lets ``verify`` check the incremental answer
    against a full one at any moment.
    """

    def __init__(self, engine: Engine, facts: Mapping[str, Any], *,
                 key: Mapping[str, Any] | None = None,
                 client: str | None = None,
                 as_of: Any = None,
                 full_evaluation_threshold: float = 0.6) -> None:
        self.engine = engine
        self.key = dict(key or {})
        self.client = client
        self.as_of = as_of
        # Past this fraction of rules invalidated, the bookkeeping costs more
        # than it saves and we simply evaluate everything.
        self.threshold = full_evaluation_threshold
        self.facts: dict[str, Any] = copy.deepcopy(dict(facts))
        self.decision: Decision = self._evaluate(self.facts, None)
        self.edits: list[Delta] = []

    # -- internals --------------------------------------------------------

    def _evaluate(self, facts: Mapping[str, Any],
                  reuse: ReuseCache | None) -> Decision:
        return self.engine.evaluate(
            facts, key=self.key, client=self.client, as_of=self.as_of,
            reuse=reuse)

    def _traces_by_id(self) -> dict[str, RuleTrace]:
        return {trace.rule_id: trace for trace in self.decision.traces}

    def _because(self, trace: RuleTrace, decision: Decision,
                 changed: Iterable[str]) -> dict[str, Any]:
        """Which of the edited fact paths is behind this finding.

        Direct if the rule read one of them. Otherwise the edit reached the rule
        through a derived value, and we name both the path and the rule that
        recomputed it. That indirection is the thing a planner cannot work out
        for themselves, and the dependency graph already knows it.
        """
        changed = list(changed)
        paths = [p for p in trace.reads if _touched(p, changed)]
        via: list[str] = []

        # Follow the derived namespace outward. A rule that reads a limit is
        # affected by whatever moved that limit, and the producers graph already
        # knows who that is. One hop covers the shapes that occur in practice;
        # deeper chains are reported by their first hop rather than guessed at.
        wanted = {p.split(".", 1)[1] for p in trace.reads
                  if p.startswith("derived.")}
        for name in sorted(wanted):
            for producer_id in self.engine.ruleset.producers.get(
                    f"derived.{name}", ()):
                other = decision.trace_for(producer_id)
                if other is None:
                    continue
                contributed = [p for p in other.reads if _touched(p, changed)]
                if contributed:
                    via.append(producer_id)
                    paths.extend(contributed)

        return {"paths": sorted(set(paths)), "via_rules": sorted(set(via))}

    def _delta(self, before: Decision, after: Decision,
               changed: list[str], reuse: ReuseCache | None,
               micros: float) -> Delta:
        before_codes = {f.code: f for f in before.findings}
        after_codes = {f.code: f for f in after.findings}

        def describe(finding: Finding, decision: Decision) -> dict[str, Any]:
            trace = decision.trace_for(finding.rule_id)
            return {
                "code": finding.code,
                "severity": finding.severity,
                "message": finding.message,
                "rule_id": finding.rule_id,
                "detail": dict(finding.detail),
                "because": (self._because(trace, decision, changed)
                            if trace else {"paths": [], "via_rules": []}),
            }

        derived_moved: dict[str, dict[str, Any]] = {}
        for name in set(before.derived) | set(after.derived):
            old = before.derived.get(name)
            new = after.derived.get(name)
            if old != new:
                derived_moved[name] = {"before": old, "after": new}

        return Delta(
            changed_paths=sorted(changed),
            newly_raised=[describe(f, after) for code, f in after_codes.items()
                          if code not in before_codes],
            no_longer_raised=[describe(f, before) for code, f in before_codes.items()
                              if code not in after_codes],
            still_raised=sorted(set(before_codes) & set(after_codes)),
            derived_moved=derived_moved,
            ok_before=before.ok,
            ok_after=after.ok,
            rules_recomputed=list(reuse.recomputed) if reuse
                             else [t.rule_id for t in after.traces],
            rules_reused=len(reuse.reused) if reuse else 0,
            full_evaluation=reuse is None,
            micros=micros,
        )

    def _run(self, changes: Mapping[str, Any]) -> tuple[dict[str, Any], Decision, Delta]:
        started = time.perf_counter_ns()
        new_facts = apply_changes(self.facts, changes)
        changed = sorted(diff_facts(self.facts, new_facts))

        if not changed:
            return new_facts, self.decision, Delta(
                changed_paths=[], ok_before=self.decision.ok,
                ok_after=self.decision.ok,
                rules_reused=len(self.decision.traces),
                micros=(time.perf_counter_ns() - started) / 1000.0)

        traces = self._traces_by_id()
        reuse: ReuseCache | None = ReuseCache(
            traces, changed, self.decision.derived)

        # If the edit reaches most of the ruleset there is nothing to save.
        would_reuse = sum(1 for rule_id in traces if reuse.usable(rule_id))
        if traces and (would_reuse / len(traces)) < (1.0 - self.threshold):
            reuse = None

        after = self._evaluate(new_facts, reuse)
        micros = (time.perf_counter_ns() - started) / 1000.0
        return new_facts, after, self._delta(self.decision, after, changed,
                                             reuse, micros)

    # -- the public surface ----------------------------------------------

    def apply(self, changes: Mapping[str, Any]) -> Delta:
        """Commit an edit and return what it did."""
        new_facts, decision, delta = self._run(changes)
        self.facts = new_facts
        self.decision = decision
        self.edits.append(delta)
        return delta

    def preview(self, changes: Mapping[str, Any]) -> Delta:
        """What would this edit do? Nothing is committed.

        This is the hover state in a planning interface: show the consequence
        before the planner lets go of the mouse. It costs one incremental pass
        and leaves no trace behind, because there is no working memory to undo.
        """
        _, _, delta = self._run(changes)
        return delta

    def score(self, variants: Mapping[str, Mapping[str, Any]]) -> dict[str, Delta]:
        """Preview several candidate edits against the current state.

        The normal planning question is not "is this legal" but "which of these
        twelve crew members can take this duty". Each variant is independent, so
        this is embarrassingly parallel and safe to farm out; it is sequential
        here only because the sequential version is fast enough to demonstrate.
        """
        return {name: self.preview(changes) for name, changes in variants.items()}

    def verify(self) -> list[str]:
        """Re-evaluate from scratch and check the incremental answer.

        Returns a list of problems; empty means the incremental path agrees with
        the full one. Call it on every edit in development and on a sample in
        production. This is the audit a matching network cannot easily offer,
        and it is only possible because the two paths are the same code over the
        same deterministic evaluator.
        """
        fresh = self._evaluate(self.facts, None)
        problems: list[str] = []
        if fresh.output_digest != self.decision.output_digest:
            problems.append(
                f"incremental result diverged from a full evaluation: "
                f"{sorted(self.decision.codes())} against {sorted(fresh.codes())}")
        for name in set(fresh.derived) | set(self.decision.derived):
            if fresh.derived.get(name) != self.decision.derived.get(name):
                problems.append(
                    f"derived.{name} is {self.decision.derived.get(name)!r} "
                    f"incrementally and {fresh.derived.get(name)!r} from scratch")
        return problems

    def reset(self, facts: Mapping[str, Any] | None = None) -> Decision:
        self.facts = copy.deepcopy(dict(facts if facts is not None else self.facts))
        self.decision = self._evaluate(self.facts, None)
        self.edits.clear()
        return self.decision

    # -- reporting --------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        if not self.edits:
            return {"edits": 0}
        reused = sum(e.rules_reused for e in self.edits)
        recomputed = sum(len(e.rules_recomputed) for e in self.edits)
        timings = sorted(e.micros for e in self.edits)
        return {
            "edits": len(self.edits),
            "rules_reused": reused,
            "rules_recomputed": recomputed,
            "work_avoided": round(reused / (reused + recomputed), 4)
                            if (reused + recomputed) else 0.0,
            "p50_micros": round(timings[len(timings) // 2], 1),
            "p95_micros": round(timings[min(len(timings) - 1,
                                            int(len(timings) * 0.95))], 1),
            "full_evaluations": sum(1 for e in self.edits if e.full_evaluation),
        }
