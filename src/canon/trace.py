"""Provenance. Every decision explains itself.

Two of the seven recurring problems are debugging problems: finding the rule
behind a bug, and finding the deployment that broke it. Canon answers the first
here and the second in ``registry.py``, and the two are joined by the rule
content hash that appears in both.

A trace is not a log line. It is a structured record of every rule that was
considered, why it was or was not considered, what data it looked at, what it
concluded, and how long it took. It serialises to JSON, so it can be attached to
a support ticket and replayed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from .rules import Finding, content_hash

__all__ = ["RuleTrace", "Decision"]

_SEVERITY_ORDER = {"info": 0, "advisory": 1, "soft": 2, "hard": 3}


@dataclass
class RuleTrace:
    """What happened to one rule during one transaction."""

    rule_id: str
    rule_version: str
    rule_hash: str
    stratum: int
    considered: bool = True
    skip_reason: str | None = None
    guard_source: str | None = None
    guard_result: Any = None
    fired: bool = False
    reads: dict[str, Any] = field(default_factory=dict)
    emitted: dict[str, Any] | None = None
    sets: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    micros: float = 0.0
    # Set when an interactive session reused this rule's previous verdict
    # because nothing it read had changed. The finding object is carried so the
    # verdict can be replayed without re-evaluating anything.
    reused: bool = False
    finding: Finding | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "rule_hash": self.rule_hash[:12],
            "stratum": self.stratum,
            "considered": self.considered,
            "skip_reason": self.skip_reason,
            "guard": self.guard_source,
            "guard_result": self.guard_result,
            "fired": self.fired,
            "reads": self.reads,
            "emitted": self.emitted,
            "sets": self.sets,
            "error": self.error,
            "reused": self.reused,
            "micros": round(self.micros, 1),
        }

    def summary(self) -> str:
        if not self.considered:
            return f"skipped   {self.rule_id:<16} {self.skip_reason}"
        if self.error:
            return f"ERROR     {self.rule_id:<16} {self.error}"
        verdict = "FIRED" if self.fired else "no"
        return (f"{verdict:<9} {self.rule_id:<16} guard={self.guard_result!r} "
                f"reads={len(self.reads)} {self.micros:.0f}us")


@dataclass
class Decision:
    """The complete, self describing result of one stateless evaluation."""

    ruleset_id: str
    ruleset_version: str
    ruleset_hash: str
    client: str | None = None
    as_of: date | None = None
    key: Mapping[str, Any] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    derived: dict[str, Any] = field(default_factory=dict)
    traces: list[RuleTrace] = field(default_factory=list)
    fact_stats: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    micros: float = 0.0
    receipt: dict[str, Any] | None = None

    # -- verdicts ---------------------------------------------------------

    @property
    def ok(self) -> bool:
        """True when nothing hard blocking was raised.

        Soft findings are deliberately not failures. A rostering system needs to
        distinguish "illegal" from "undesirable", and collapsing the two is how
        planners end up overriding the engine wholesale.
        """
        return not any(f.severity == "hard" for f in self.findings) and not self.errors

    @property
    def severity(self) -> str:
        if not self.findings:
            return "info"
        return max((f.severity for f in self.findings),
                   key=lambda s: _SEVERITY_ORDER.get(s, 0))

    def codes(self) -> list[str]:
        return [f.code for f in self.findings]

    def rules_fired(self) -> list[str]:
        return [t.rule_id for t in self.traces if t.fired]

    def rules_considered(self) -> list[str]:
        return [t.rule_id for t in self.traces if t.considered]

    # -- explanation ------------------------------------------------------

    def trace_for(self, rule_id: str) -> RuleTrace | None:
        for trace in self.traces:
            if trace.rule_id == rule_id:
                return trace
        return None

    def explain(self, code: str) -> list[dict[str, Any]]:
        """Why did finding ``code`` appear?

        Returns the rule that emitted it, followed by the transitive chain of
        rules that produced the derived values it depended on. This is the
        answer to "which rule caused this", and it is computed from the run
        itself rather than reconstructed from source by hand.
        """
        out: list[dict[str, Any]] = []
        for trace in self.traces:
            if trace.emitted and trace.emitted.get("code") == code:
                out.append(trace.to_dict())
                out.extend(self._upstream(trace, set()))
        return out

    def why(self, derived_name: str) -> list[dict[str, Any]]:
        """Which rules contributed to a derived value, in evaluation order."""
        key = derived_name.split(".")[-1]
        return [t.to_dict() for t in self.traces if key in t.sets]

    def _upstream(self, trace: RuleTrace, seen: set[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        wanted = {p.split(".", 1)[1] for p in trace.reads
                  if p.startswith("derived.")}
        for other in self.traces:
            if other.rule_id in seen or other.rule_id == trace.rule_id:
                continue
            if wanted & set(other.sets):
                seen.add(other.rule_id)
                out.append(other.to_dict())
                out.extend(self._upstream(other, seen))
        return out

    def hot_rules(self, limit: int = 10) -> list[tuple[str, float]]:
        ranked = sorted(((t.rule_id, t.micros) for t in self.traces),
                        key=lambda pair: pair[1], reverse=True)
        return ranked[:limit]

    # -- serialisation ----------------------------------------------------

    @property
    def input_digest(self) -> str:
        """Hash of exactly the data the rules read.

        Not the whole payload. Two requests that differ only in fields no rule
        looked at produce the same digest, which makes caching and duplicate
        detection sound rather than approximate.
        """
        return content_hash(self.inputs)

    @property
    def output_digest(self) -> str:
        return content_hash({
            "findings": [f.to_dict() for f in self.findings],
            "derived": self.derived,
        })

    def to_dict(self, include_traces: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ruleset": {
                "id": self.ruleset_id,
                "version": self.ruleset_version,
                "hash": self.ruleset_hash[:12],
            },
            "client": self.client,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "key": dict(self.key),
            "ok": self.ok,
            "severity": self.severity,
            "findings": [f.to_dict() for f in self.findings],
            "derived": self.derived,
            "errors": list(self.errors),
            "facts": self.fact_stats,
            "digests": {
                "input": self.input_digest[:16],
                "output": self.output_digest[:16],
            },
            "micros": round(self.micros, 1),
        }
        if include_traces:
            payload["trace"] = [t.to_dict() for t in self.traces]
        if self.receipt:
            payload["receipt"] = self.receipt
        return payload

    # -- human output -----------------------------------------------------

    def render(self, verbose: bool = False) -> str:
        lines = [
            f"ruleset {self.ruleset_id} v{self.ruleset_version} "
            f"({self.ruleset_hash[:12]})",
            f"client={self.client or '-'} as_of={self.as_of or '-'} "
            f"key={dict(self.key)}",
            f"verdict: {'PASS' if self.ok else 'FAIL'}  severity={self.severity}  "
            f"{self.micros:.0f}us",
            "",
        ]
        if self.findings:
            lines.append("findings")
            for finding in self.findings:
                lines.append(f"  [{finding.severity:<8}] {finding.code:<28} "
                             f"{finding.message}")
                lines.append(f"  {'':<11} from {finding.rule_id} "
                             f"v{finding.rule_version}")
        else:
            lines.append("findings: none")
        if self.derived:
            lines.append("")
            lines.append("derived")
            for name, value in sorted(self.derived.items()):
                lines.append(f"  {name} = {value!r}")
        stats = self.fact_stats
        if stats:
            lines.append("")
            lines.append(
                f"facts: {stats.get('read_paths')} of "
                f"{stats.get('planned_paths')} planned fields actually read, "
                f"{stats.get('roots_fetched')} of {stats.get('roots_planned')} "
                f"roots fetched")
        if verbose:
            lines.append("")
            lines.append("trace")
            for trace in self.traces:
                lines.append("  " + trace.summary())
        if self.errors:
            lines.append("")
            lines.append("errors")
            for message in self.errors:
                lines.append("  " + message)
        return "\n".join(lines)
