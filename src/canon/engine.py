"""The evaluation engine.

The engine is a pure function of (ruleset, facts, client, date). It holds no
state between calls, caches nothing that could vary per tenant, and mutates
nothing it was given. Two calls with the same inputs produce byte identical
output, including the trace, which is what makes shadow running meaningful and
what lets a decision receipt mean something.

Isolation is structural. A rule is handed a scoped resolver, evaluated, and its
results collected. It cannot reach another rule's scope because there is no
shared mutable context to reach into. The only channel between rules is the
``derived`` namespace, and access to that is declared, graph checked, and
stratified.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Mapping as _AbcMapping
from dataclasses import replace
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from .errors import CanonError
from .expr import Resolver
from .facts import FactSource, FactStore, Projection, dict_source
from .rules import (COMBINERS, DERIVED_ROOT, Finding, Rule, RuleSet,
                    render_message)
from .trace import Decision, RuleTrace

__all__ = ["Engine", "evaluate", "ReuseCache"]


def _touched(path: str, dirty: Iterable[str]) -> bool:
    """Is this fact path affected by anything in the dirty set?

    Prefix aware, so marking ``flight.roster`` dirty also invalidates
    ``flight.roster[*].rank``. Marking a parent is always sound; marking a child
    is precise. The diff produces whichever it can justify.
    """
    for entry in dirty:
        if path == entry or path.startswith(entry + ".") \
                or path.startswith(entry + "[*]"):
            return True
    return False


class ReuseCache:
    """Lets an interactive session skip rules whose inputs have not moved.

    The invalidation key is the set of fact paths a rule *actually read* last
    time, not the set it might read. That is the correct key, and it is exact
    rather than conservative, for a reason worth stating.

    Evaluation is pure and deterministic. If every value a rule read is
    unchanged, re-running it follows an identical control path and reaches an
    identical conclusion. A guard that short circuited on its first term never
    read the later terms, so a change to those terms cannot alter its verdict.
    Skipping it is not an optimisation with a risk attached; it is a
    consequence of the evaluator having no state and no side effects.

    Which means the trace, built for debugging, turns out to be exactly the
    invalidation structure an incremental engine needs. We did not have to build
    a second one.
    """

    def __init__(self, traces: Mapping[str, RuleTrace],
                 dirty: Iterable[str],
                 previous_derived: Mapping[str, Any] | None = None) -> None:
        self.traces = dict(traces)
        self.dirty: set[str] = set(dirty)
        self.previous_derived = dict(previous_derived or {})
        self.reused: list[str] = []
        self.recomputed: list[str] = []

    def usable(self, rule_id: str) -> RuleTrace | None:
        trace = self.traces.get(rule_id)
        if trace is None or trace.error:
            return None
        for path in trace.reads:
            if _touched(path, self.dirty):
                return None
        return trace

    def note_derived(self, name: str, value: Any) -> None:
        """A derived value that moved dirties every rule that reads it."""
        if name not in self.previous_derived or self.previous_derived[name] != value:
            self.dirty.add(f"{DERIVED_ROOT}.{name}")


class _ScopedResolver(Resolver):
    """Per rule view over the shared fact store.

    Reads are recorded twice: once against the rule, which is what makes the
    trace useful, and once against the store, which is what makes the payload
    accounting useful. Fetching still happens once per root for the whole
    transaction.
    """

    static = False

    def __init__(self, store: FactStore) -> None:
        # The store's root set is complete before any rule runs, because the
        # engine injects the derived namespace up front, so a plain snapshot is
        # correct and avoids a descriptor dance.
        super().__init__(store.roots)
        self.store = store

    def fetch(self, path: str) -> Any:
        return self.store.fetch(path)

    def fetch_collection(self, path: str) -> Sequence[Any]:
        return self.store.fetch_collection(path)

    def record(self, path: str, kind: str, value: Any) -> None:
        super().record(path, kind, value)
        self.store.record(path, kind, value)


class Engine:
    """Stateless evaluator for one ruleset."""

    def __init__(self, ruleset: RuleSet, *, strict_facts: bool = True,
                 on_rule_error: str = "record") -> None:
        if on_rule_error not in ("record", "raise"):
            raise CanonError("on_rule_error must be 'record' or 'raise'")
        self.ruleset = ruleset
        self.strict_facts = strict_facts
        self.on_rule_error = on_rule_error

    # -- planning ---------------------------------------------------------

    def projection(self, client: str | None = None,
                   as_of: date | None = None) -> Projection:
        return self.ruleset.projection_for(client, as_of)

    def plan(self, client: str | None = None,
             as_of: date | None = None) -> dict[str, Any]:
        """The integration contract: what this ruleset needs and who needs it."""
        applicable = self.ruleset.applicable(client, as_of)
        projection = self.ruleset.projection_for(client, as_of)
        by_path: dict[str, list[str]] = defaultdict(list)
        for rule in applicable:
            for path in self.ruleset.rule_paths[rule.id]:
                by_path[path].append(rule.id)
        return {
            "ruleset": self.ruleset.id,
            "version": self.ruleset.version,
            "client": client,
            "as_of": as_of.isoformat() if as_of else None,
            "rules_applicable": len(applicable),
            "rules_total": len(self.ruleset),
            "strata": len(self.ruleset.strata),
            "projection": projection.to_dict(),
            "paths": projection.paths,
            "field_count": projection.leaf_count(),
            "requested_by": {path: sorted(rules)
                             for path, rules in sorted(by_path.items())},
        }

    # -- evaluation -------------------------------------------------------

    def evaluate(self, sources: Mapping[str, FactSource] | Mapping[str, Any],
                 *, key: Mapping[str, Any] | None = None,
                 client: str | None = None,
                 as_of: date | None = None,
                 capture_values: bool = True,
                 reuse: "ReuseCache | None" = None) -> Decision:
        """Run the ruleset once.

        ``sources`` may be a mapping of root name to callable, or a plain nested
        document, which is wrapped automatically. The document form is what you
        want for tests, replays and the CLI; the callable form is what you want
        in production, because it is the form that lets Canon avoid fetching
        data no rule ended up needing.
        """
        started = time.perf_counter_ns()
        rs = self.ruleset
        as_of_date = _as_date(as_of)

        callables = {name: value for name, value in sources.items()
                     if callable(value)}
        if len(callables) != len(sources):
            callables = dict_source(sources)  # type: ignore[arg-type]

        projection = rs.projection_for(client, as_of_date)
        store = FactStore(projection, callables, key or {},
                          strict=self.strict_facts)

        derived_values: dict[str, Any] = {}
        derived_pending: dict[str, list[Any]] = defaultdict(list)
        store.set_root(DERIVED_ROOT, derived_values)

        traces: list[RuleTrace] = []
        findings: list[Finding] = []
        errors: list[str] = []

        for stratum_index, stratum in enumerate(rs.strata):
            wrote_this_stratum = False
            for rule in stratum:
                trace = self._run_rule(rule, stratum_index, store, client,
                                       as_of_date, findings, derived_pending,
                                       errors, capture_values, reuse)
                traces.append(trace)
                if trace.sets:
                    wrote_this_stratum = True
            if wrote_this_stratum:
                self._combine(derived_pending, derived_values, reuse)

        # Findings are ordered by severity then rule priority so that the most
        # consequential reason appears first in any user interface.
        findings.sort(key=lambda f: (
            -_SEVERITY_RANK.get(f.severity, 0),
            rs.by_id[f.rule_id].priority if f.rule_id in rs.by_id else 0,
            f.rule_id,
        ))

        decision = Decision(
            ruleset_id=rs.id,
            ruleset_version=rs.version,
            ruleset_hash=rs.content_hash,
            client=client,
            as_of=as_of_date,
            key=dict(key or {}),
            findings=findings,
            derived=dict(derived_values),
            traces=traces,
            fact_stats=store.stats(),
            inputs=store.read_document() if capture_values else {},
            errors=errors,
            micros=(time.perf_counter_ns() - started) / 1000.0,
        )
        return decision

    # -- internals --------------------------------------------------------

    def _run_rule(self, rule: Rule, stratum_index: int, store: FactStore,
                  client: str | None, as_of: date | None,
                  findings: list[Finding],
                  derived_pending: dict[str, list[Any]],
                  errors: list[str], capture_values: bool,
                  reuse: "ReuseCache | None" = None) -> RuleTrace:
        if reuse is not None:
            cached = reuse.usable(rule.id)
            if cached is not None:
                reuse.reused.append(rule.id)
                return self._replay(cached, findings, derived_pending)
            reuse.recomputed.append(rule.id)

        trace = RuleTrace(
            rule_id=rule.id,
            rule_version=rule.version,
            rule_hash=rule.content_hash,
            stratum=stratum_index,
            guard_source=rule.when.source if rule.when else None,
        )
        applicable, reason = rule.applies_to(client, as_of)
        if not applicable:
            trace.considered = False
            trace.skip_reason = reason
            return trace

        scope = _ScopedResolver(store)
        started = time.perf_counter_ns()
        try:
            guard = True
            if rule.when is not None:
                guard = rule.when.evaluate(scope)
                trace.guard_result = _plain(guard)
            if guard is True or (guard is not False and guard):
                trace.fired = True
                if rule.emit is not None:
                    detail = {name: _plain(expression.evaluate(scope))
                              for name, expression in rule.emit.detail.items()}
                    finding = Finding(
                        code=rule.emit.code,
                        severity=rule.emit.severity,
                        message=render_message(rule.emit.message, detail),
                        rule_id=rule.id,
                        rule_version=rule.version,
                        detail=detail,
                    )
                    findings.append(finding)
                    trace.emitted = finding.to_dict()
                    trace.finding = finding
                for name, expression in rule.sets.items():
                    value = _plain(expression.evaluate(scope))
                    derived_pending[name].append(value)
                    trace.sets[name] = value
        except Exception as exc:  # noqa: BLE001 - deliberately broad
            message = f"{rule.id}: {type(exc).__name__}: {exc}"
            trace.error = message
            errors.append(message)
            if self.on_rule_error == "raise":
                raise
        finally:
            trace.micros = (time.perf_counter_ns() - started) / 1000.0
            if capture_values:
                trace.reads = {path: _plain(read.value)
                               for path, read in sorted(scope.reads.items())}
            else:
                trace.reads = {path: None for path in sorted(scope.reads)}
        return trace

    def _replay(self, cached: RuleTrace, findings: list[Finding],
                derived_pending: dict[str, list[Any]]) -> RuleTrace:
        """Reuse a previous verdict verbatim, including its trace."""
        replayed = replace(cached, reused=True, micros=0.0)
        if replayed.finding is not None:
            findings.append(replayed.finding)
        for name, value in replayed.sets.items():
            derived_pending[name].append(value)
        return replayed

    def _combine(self, pending: Mapping[str, list[Any]],
                 target: dict[str, Any],
                 reuse: "ReuseCache | None" = None) -> None:
        for name, values in pending.items():
            if not values:
                continue
            policy = self.ruleset.derived_policy.get(
                name, "error" if len(values) > 1 else "first")
            combiner = COMBINERS[policy]
            usable = [v for v in values if v is not None]
            if not usable and policy in ("min", "max", "sum"):
                target[name] = None
                continue
            target[name] = combiner(name, values if policy in
                                    ("all", "and", "or", "first", "last")
                                    else usable)
            if reuse is not None:
                # A derived value that moved dirties its readers. Producers
                # always sit in an earlier stratum than consumers, so marking
                # it here reaches every consumer before it is evaluated.
                reuse.note_derived(name, target[name])


_SEVERITY_RANK = {"info": 0, "advisory": 1, "soft": 2, "hard": 3}


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value.strip()[:10])
    raise CanonError(f"as_of must be a date, got {value!r}")


def _plain(value: Any) -> Any:
    """Convert engine values into something JSON can carry."""
    from .expr import UNKNOWN

    if value is UNKNOWN:
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, _AbcMapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def evaluate(ruleset: RuleSet, sources: Mapping[str, Any], **kwargs: Any) -> Decision:
    """Convenience wrapper for one off evaluation."""
    return Engine(ruleset).evaluate(sources, **kwargs)
