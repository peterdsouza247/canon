"""The rule intermediate representation, and the ruleset that validates it.

Everything Canon can execute is a ``Rule``. YAML files, Python functions and
decision tables are three front ends onto this one shape, which is why they can
be compared honestly: they differ in ergonomics, not in semantics.

Two design choices here do most of the work.

**Rules cannot see each other by accident.** A rule reads facts, and it may read
*derived* values produced by other rules, but only if it names them in
``reads``. The ruleset builds a dependency graph from those declarations,
refuses cycles, and sorts rules into strata. Rules inside a stratum are provably
independent of one another, so evaluation order within a stratum cannot change
an outcome. That is what makes "run rules in isolation" a property of the system
rather than a discipline people have to remember.

**Conflicts are declared, not discovered.** If two rules write the same derived
value, the ruleset will not load unless the author has said how to combine them.
For flight time limitations that is nearly always ``min``, because the most
restrictive limit wins, and stating it once beats encoding it in rule ordering.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

from .errors import RuleDefinitionError, RuleSetError, UndeclaredDependencyError
from .expr import Expression, compile_expression
from .facts import Projection

__all__ = [
    "Emission", "Rule", "RuleSet", "Finding", "COMBINERS",
    "canonical_json", "content_hash",
]

DERIVED_ROOT = "derived"
_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

SEVERITIES = ("info", "advisory", "soft", "hard")


# --------------------------------------------------------------------------
# Combine policies for derived values
# --------------------------------------------------------------------------

def _combine_error(name: str, values: list[Any]) -> Any:
    raise RuleSetError(
        f"derived fact {name!r} was written by more than one rule and no "
        f"combine policy was declared. Add it under 'derived:' in the ruleset, "
        f"for example {{{name}: {{combine: min}}}}."
    )


COMBINERS = {
    "error": _combine_error,
    "min": lambda name, values: min(v for v in values if v is not None),
    "max": lambda name, values: max(v for v in values if v is not None),
    "sum": lambda name, values: sum(v for v in values if v is not None),
    "all": lambda name, values: list(values),
    "and": lambda name, values: all(values),
    "or": lambda name, values: any(values),
    "first": lambda name, values: values[0],
    "last": lambda name, values: values[-1],
}


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Emission:
    """The template a rule uses to produce a finding."""

    code: str
    severity: str = "hard"
    message: str = ""
    detail: Mapping[str, Expression] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise RuleDefinitionError(
                f"unknown severity {self.severity!r}; expected one of {SEVERITIES}")

    def to_canonical(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "detail": {k: v.source for k, v in sorted(self.detail.items())},
        }


@dataclass(frozen=True)
class Finding:
    """One outcome produced by one rule on one transaction."""

    code: str
    severity: str
    message: str
    rule_id: str
    rule_version: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "detail": dict(self.detail),
        }


def render_message(template: str, detail: Mapping[str, Any]) -> str:
    """Substitute ``{name}`` placeholders.

    Deliberately not ``str.format``. Format strings permit attribute traversal,
    which would let a rule author reach into the runtime from what looks like a
    harmless message.
    """

    def sub(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key not in detail:
            return match.group(0)
        value = detail[key]
        if isinstance(value, float):
            return f"{value:g}"
        return str(value)

    return _PLACEHOLDER.sub(sub, template or "")


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


def _as_date(value: Any, label: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError as exc:
            raise RuleDefinitionError(
                f"{label} must be an ISO date, got {value!r}") from exc
    raise RuleDefinitionError(f"{label} must be an ISO date, got {value!r}")


@dataclass(frozen=True)
class Rule:
    id: str
    version: str = "1"
    title: str = ""
    description: str = ""
    when: Expression | None = None
    emit: Emission | None = None
    sets: Mapping[str, Expression] = field(default_factory=dict)
    reads: tuple[str, ...] = ()
    clients: tuple[str, ...] = ("*",)
    effective_from: date | None = None
    effective_to: date | None = None
    priority: int = 100
    tags: tuple[str, ...] = ()
    owner: str = ""
    authoring: str = "yaml"
    source_ref: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            raise RuleDefinitionError("a rule needs an id")
        if self.emit is None and not self.sets:
            raise RuleDefinitionError(
                f"rule {self.id!r} neither emits a finding nor sets a derived "
                f"value, so it can have no effect")
        for name in self.reads:
            if not name.startswith(DERIVED_ROOT + "."):
                raise RuleDefinitionError(
                    f"rule {self.id!r} declares read {name!r}; declared reads "
                    f"name derived facts only, as 'derived.something'. Plain "
                    f"facts are discovered automatically.")
        if self.effective_from and self.effective_to \
                and self.effective_from > self.effective_to:
            raise RuleDefinitionError(
                f"rule {self.id!r} has effective_from after effective_to")

    # -- expression inventory ---------------------------------------------

    def expressions(self) -> list[Expression]:
        out: list[Expression] = []
        if self.when is not None:
            out.append(self.when)
        if self.emit is not None:
            out.extend(self.emit.detail.values())
        out.extend(self.sets.values())
        return out

    def analyse(self, roots: Iterable[str]) -> list[str]:
        """All fact paths this rule can read, derived facts included."""
        all_roots = set(roots) | {DERIVED_ROOT}
        found: set[str] = set()
        for expression in self.expressions():
            found.update(expression.analyse(all_roots))
        return sorted(found)

    def fact_paths(self, roots: Iterable[str]) -> list[str]:
        return [p for p in self.analyse(roots)
                if not p.startswith(DERIVED_ROOT + ".")]

    def derived_reads(self, roots: Iterable[str]) -> list[str]:
        return [p for p in self.analyse(roots)
                if p.startswith(DERIVED_ROOT + ".")]

    @property
    def produces(self) -> tuple[str, ...]:
        return tuple(sorted(self.sets))

    # -- applicability ----------------------------------------------------

    def applies_to(self, client: str | None, as_of: date | None) -> tuple[bool, str]:
        if client is not None and "*" not in self.clients \
                and client not in self.clients:
            return False, f"not enabled for client {client!r}"
        if as_of is not None:
            if self.effective_from and as_of < self.effective_from:
                return False, f"not effective until {self.effective_from.isoformat()}"
            if self.effective_to and as_of > self.effective_to:
                return False, f"expired on {self.effective_to.isoformat()}"
        return True, "applicable"

    # -- identity ---------------------------------------------------------

    def to_canonical(self) -> dict[str, Any]:
        """The semantic content of the rule.

        Titles, descriptions, owners and tags are excluded on purpose. Editing a
        comment must not change a rule's identity, otherwise every deployment
        diff is noise and nobody reads them.
        """
        return {
            "id": self.id,
            "version": self.version,
            "when": self.when.source if self.when else None,
            "emit": self.emit.to_canonical() if self.emit else None,
            "sets": {k: v.source for k, v in sorted(self.sets.items())},
            "reads": sorted(self.reads),
            "clients": sorted(self.clients),
            "effective_from": self.effective_from.isoformat() if self.effective_from else None,
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "priority": self.priority,
        }

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_canonical())

    @property
    def short_hash(self) -> str:
        return self.content_hash[:12]


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def content_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# RuleSets
# --------------------------------------------------------------------------


class RuleSet:
    """An immutable, validated collection of rules.

    Construction is where the expensive, useful work happens: static analysis of
    every expression, dependency graph construction, cycle detection, conflict
    detection, and projection computation. All of it happens once at load time
    and never again, so evaluation is a pure function over frozen data.
    """

    def __init__(self, id: str, rules: Sequence[Rule], *,
                 version: str = "1",
                 roots: Iterable[str] = (),
                 derived_policy: Mapping[str, str] | None = None,
                 description: str = "") -> None:
        self.id = id
        self.version = version
        self.description = description
        self.rules: tuple[Rule, ...] = tuple(rules)
        self.derived_policy = dict(derived_policy or {})

        seen: dict[str, Rule] = {}
        for rule in self.rules:
            if rule.id in seen:
                raise RuleSetError(
                    f"duplicate rule id {rule.id!r} "
                    f"(in {seen[rule.id].source_ref or 'unknown'} "
                    f"and {rule.source_ref or 'unknown'})")
            seen[rule.id] = rule
        self.by_id = seen

        declared_roots = set(roots)
        if not declared_roots:
            declared_roots = _infer_roots(self.rules)
        self.roots: tuple[str, ...] = tuple(sorted(declared_roots))

        self._analyse()

    # -- validation and analysis ------------------------------------------

    def _analyse(self) -> None:
        self.rule_paths: dict[str, list[str]] = {}
        self.rule_derived_reads: dict[str, list[str]] = {}
        producers: dict[str, list[str]] = defaultdict(list)
        all_paths: set[str] = set()

        for rule in self.rules:
            paths = rule.fact_paths(self.roots)
            derived = rule.derived_reads(self.roots)
            self.rule_paths[rule.id] = paths
            self.rule_derived_reads[rule.id] = derived
            all_paths.update(paths)

            declared = set(rule.reads)
            actual = set(derived)
            undeclared = actual - declared
            if undeclared:
                raise UndeclaredDependencyError(
                    f"rule {rule.id!r} reads {sorted(undeclared)} but does not "
                    f"declare them. Add them under 'reads:'. Canon requires the "
                    f"declaration so that rule to rule coupling is visible in "
                    f"the source and enforceable by the graph."
                )
            unused = declared - actual
            if unused:
                raise UndeclaredDependencyError(
                    f"rule {rule.id!r} declares reads {sorted(unused)} that it "
                    f"never uses. Stale declarations make the dependency graph "
                    f"lie, so they are rejected."
                )
            for name in rule.produces:
                producers[f"{DERIVED_ROOT}.{name}"].append(rule.id)

        self.producers = dict(producers)
        self.projection = Projection(all_paths)

        # Every derived fact that is read must be produced by something.
        for rule in self.rules:
            for name in self.rule_derived_reads[rule.id]:
                if name not in producers:
                    raise RuleSetError(
                        f"rule {rule.id!r} reads {name!r} but no rule in "
                        f"ruleset {self.id!r} produces it")

        # Conflicting writers need a declared policy.
        for name, writers in producers.items():
            short = name.split(".", 1)[1]
            if len(writers) > 1 and short not in self.derived_policy:
                raise RuleSetError(
                    f"derived fact {short!r} is written by {sorted(writers)}. "
                    f"Declare how to combine them, for example:\n"
                    f"  derived:\n    {short}: {{combine: min}}"
                )
            policy = self.derived_policy.get(short, "error")
            if policy not in COMBINERS:
                raise RuleSetError(
                    f"unknown combine policy {policy!r} for {short!r}; "
                    f"available: {sorted(COMBINERS)}")

        self.strata = self._build_strata()

    def _build_strata(self) -> tuple[tuple[Rule, ...], ...]:
        depends_on: dict[str, set[str]] = {r.id: set() for r in self.rules}
        for rule in self.rules:
            for name in self.rule_derived_reads[rule.id]:
                for producer in self.producers.get(name, ()):
                    if producer != rule.id:
                        depends_on[rule.id].add(producer)

        remaining = dict(depends_on)
        resolved: set[str] = set()
        strata: list[tuple[Rule, ...]] = []

        while remaining:
            ready = [rid for rid, deps in remaining.items() if deps <= resolved]
            if not ready:
                cycle = sorted(remaining)
                raise RuleSetError(
                    f"dependency cycle between rules {cycle}. Canon will not "
                    f"load a ruleset whose rules depend on one another in a "
                    f"loop, because there is no order in which all of them are "
                    f"correct."
                )
            layer = sorted((self.by_id[rid] for rid in ready),
                           key=lambda r: (r.priority, r.id))
            strata.append(tuple(layer))
            resolved.update(ready)
            for rid in ready:
                remaining.pop(rid)

        return tuple(strata)

    # -- selection --------------------------------------------------------

    def applicable(self, client: str | None = None,
                   as_of: date | None = None) -> list[Rule]:
        return [r for r in self.rules if r.applies_to(client, as_of)[0]]

    def projection_for(self, client: str | None = None,
                       as_of: date | None = None) -> Projection:
        """The payload contract for one client on one date.

        Multi tenant deployments differ, sometimes a lot. Generating the
        projection per client means a small client is not made to ship the
        fields that only a large client's rules need.
        """
        paths: set[str] = set()
        for rule in self.applicable(client, as_of):
            paths.update(self.rule_paths[rule.id])
        return Projection(paths)

    def subset(self, rule_ids: Iterable[str]) -> "RuleSet":
        wanted = set(rule_ids)
        return RuleSet(
            self.id,
            [r for r in self.rules if r.id in wanted],
            version=self.version,
            roots=self.roots,
            derived_policy=self.derived_policy,
            description=self.description,
        )

    # -- identity ---------------------------------------------------------

    def to_canonical(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "roots": list(self.roots),
            "derived_policy": dict(sorted(self.derived_policy.items())),
            "rules": [r.to_canonical() for r in
                      sorted(self.rules, key=lambda r: r.id)],
        }

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_canonical())

    def clients(self) -> list[str]:
        found: set[str] = set()
        for rule in self.rules:
            found.update(rule.clients)
        return sorted(found)

    def __len__(self) -> int:
        return len(self.rules)

    def __iter__(self):
        return iter(self.rules)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"RuleSet({self.id!r}, {len(self.rules)} rules, "
                f"{len(self.strata)} strata, "
                f"{self.projection.leaf_count()} planned fields)")


def _infer_roots(rules: Sequence[Rule]) -> set[str]:
    """Discover fact roots by parsing rather than by asking.

    Roots are the top level names an expression mentions. We collect the
    candidate names from the syntax tree so that a ruleset does not have to
    repeat, in a header, what its own rules already say.
    """
    import ast as _ast

    found: set[str] = set()
    for rule in rules:
        for expression in rule.expressions():
            bound: set[str] = set()
            for node in _ast.walk(expression.tree):
                if isinstance(node, _ast.comprehension) and isinstance(
                        node.target, _ast.Name):
                    bound.add(node.target.id)
            for node in _ast.walk(expression.tree):
                if isinstance(node, _ast.Name) and node.id not in bound:
                    found.add(node.id)
    from .expr import FUNCTIONS

    return {name for name in found
            if name not in FUNCTIONS and name != DERIVED_ROOT}
