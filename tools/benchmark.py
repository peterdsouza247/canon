"""Measure the engine before optimising it.

The question this exists to answer is "does Canon hold up against a RETE engine
at scale", and the only honest way to answer it is with numbers from your own
ruleset. This harness produces the baseline: synthetic rulesets of a chosen
size, real transactions through the real engine, and a breakdown of where the
time actually goes.

It also measures, statically, how much a discriminator index would help. That
is the first optimisation on the list in docs/performance.md and it is the one
worth doing first, so it is worth knowing the number before writing any of it.

    python tools/benchmark.py
    python tools/benchmark.py --rules 100 500 2000 10000 --transactions 300
    python tools/benchmark.py --rules 2000 --json bench.json

Nothing here touches the library. It builds rulesets through the ordinary
loader and calls the ordinary engine, so the numbers are the numbers.
"""

from __future__ import annotations

import argparse
import ast
import json
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TYPES = [f"T{i}" for i in range(20)]
RANKS = ["CP", "FO", "SCC", "CC"]
DERIVED = 5
CREW_FIELDS = 50


# --------------------------------------------------------------------------
# Synthetic rulesets, shaped like a real estate
# --------------------------------------------------------------------------


def build_ruleset(n_rules: int):
    """A ruleset with the mix a rostering estate actually has.

    Roughly: six in ten plain threshold checks, two in ten gated on an aircraft
    type or similar discriminator, one in ten consuming a derived limit, one in
    ten needing a vertical slice over the roster.
    """
    from canon import load_mapping

    rules: list[dict[str, Any]] = []
    for k in range(DERIVED):
        rules.append({
            "id": f"D{k}", "version": "1", "priority": 10,
            "set": {f"limit_{k}": f"limits.base_{k}"},
        })

    for i in range(max(0, n_rules - DERIVED)):
        kind = i % 10
        rule: dict[str, Any] = {
            "id": f"R{i:06d}", "version": "1", "priority": 100,
            "emit": {"code": f"C{i:06d}", "severity": "soft"},
        }
        if kind < 6:
            rule["when"] = f"crew.f{i % CREW_FIELDS} > {60 + i % 60}"
        elif kind < 8:
            rule["when"] = (f"flight.aircraft_type == '{TYPES[i % len(TYPES)]}' "
                            f"and crew.f{i % CREW_FIELDS} > {40 + i % 40}")
        elif kind < 9:
            k = i % DERIVED
            rule["reads"] = [f"derived.limit_{k}"]
            rule["when"] = f"duty.hours > derived.limit_{k}"
        else:
            rule["when"] = (f"sum(1 for m in flight.roster "
                            f"if m.rank == 'CP' and m.hours_on_type < "
                            f"{100 + i % 400}) > 0")
        rules.append(rule)

    return load_mapping({"ruleset": "bench", "version": "1", "rules": rules})


def build_facts(rng: random.Random, roster_size: int = 8) -> dict[str, Any]:
    return {
        "limits": {f"base_{k}": 10.0 + k for k in range(DERIVED)},
        "crew": {f"f{i}": round(rng.uniform(0.0, 120.0), 1)
                 for i in range(CREW_FIELDS)},
        "duty": {"hours": round(rng.uniform(6.0, 16.0), 1)},
        "flight": {
            "aircraft_type": rng.choice(TYPES),
            "roster": [
                {"rank": rng.choice(RANKS),
                 "hours_on_type": rng.randrange(30, 4000)}
                for _ in range(roster_size)
            ],
        },
    }


# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------


def percentiles(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    def at(fraction: float) -> float:
        return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]
    return {
        "mean_us": round(statistics.fmean(ordered), 1),
        "p50_us": round(at(0.50), 1),
        "p95_us": round(at(0.95), 1),
        "p99_us": round(at(0.99), 1),
        "max_us": round(ordered[-1], 1),
    }


def time_engine(ruleset, transactions: int, seed: int,
                capture_values: bool) -> dict[str, Any]:
    from canon import Engine

    engine = Engine(ruleset, strict_facts=False)
    rng = random.Random(seed)
    cases = [build_facts(rng) for _ in range(transactions)]

    # One warm run so expression caches and imports are not in the sample.
    engine.evaluate(cases[0], capture_values=capture_values)

    samples: list[float] = []
    fired: list[int] = []
    reads: list[int] = []
    for facts in cases:
        started = time.perf_counter_ns()
        decision = engine.evaluate(facts, capture_values=capture_values)
        samples.append((time.perf_counter_ns() - started) / 1000.0)
        fired.append(len(decision.rules_fired()))
        reads.append(decision.fact_stats["read_paths"])

    out = percentiles(samples)
    out["rules"] = len(ruleset)
    out["rules_per_second"] = round(
        len(ruleset) / (out["p50_us"] / 1_000_000.0)) if out["p50_us"] else 0
    out["fired_median"] = statistics.median(fired)
    out["fields_read_median"] = statistics.median(reads)
    out["capture_values"] = capture_values
    return out


def time_load(n_rules: int, repeats: int = 3) -> dict[str, Any]:
    """Load time is paid once per process, but it is not free and it grows."""
    samples = []
    for _ in range(repeats):
        started = time.perf_counter_ns()
        build_ruleset(n_rules)
        samples.append((time.perf_counter_ns() - started) / 1_000_000.0)
    return {"rules": n_rules, "load_ms": round(min(samples), 1)}


# --------------------------------------------------------------------------
# How much would a discriminator index help?
# --------------------------------------------------------------------------


def _path_of(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _conjuncts(node: ast.AST):
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        for value in node.values:
            yield from _conjuncts(value)
    else:
        yield node


def discriminators(rule) -> list[tuple[str, Any]]:
    """Equality tests against a literal, at the top level of a conjunction.

    These are the conditions an index can key on. Everything else is left in
    the always-consider set, so the index over-approximates and can never make
    the engine miss a rule.
    """
    if rule.when is None:
        return []
    found: list[tuple[str, Any]] = []
    for node in _conjuncts(rule.when.tree.body):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], ast.Eq):
            continue
        path = _path_of(node.left)
        comparator = node.comparators[0]
        if path and isinstance(comparator, ast.Constant):
            found.append((path, comparator.value))
    return found


def fact_value(facts: dict[str, Any], path: str) -> Any:
    cursor: Any = facts
    for segment in path.split("."):
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(segment)
    return cursor


def index_study(ruleset, transactions: int, seed: int) -> dict[str, Any]:
    index: dict[tuple[str, Any], set[str]] = {}
    always: set[str] = set()
    for rule in ruleset.rules:
        found = discriminators(rule)
        if not found:
            always.add(rule.id)
            continue
        # One key is enough to be sound: a rule is a candidate if any of its
        # discriminators matches. Using the most selective one would be better.
        path, value = found[0]
        index.setdefault((path, value), set()).add(rule.id)

    rng = random.Random(seed)
    candidate_counts = []
    for _ in range(transactions):
        facts = build_facts(rng)
        candidates = set(always)
        for (path, value), rule_ids in index.items():
            if fact_value(facts, path) == value:
                candidates |= rule_ids
        candidate_counts.append(len(candidates))

    total = len(ruleset)
    median = statistics.median(candidate_counts)
    return {
        "rules": total,
        "indexable": total - len(always),
        "always_considered": len(always),
        "candidates_median": median,
        "candidates_p95": sorted(candidate_counts)[
            min(len(candidate_counts) - 1, int(len(candidate_counts) * 0.95))],
        "reduction": round(1 - (median / total), 4) if total else 0.0,
    }


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rules", type=int, nargs="+",
                        default=[100, 500, 2000])
    parser.add_argument("--transactions", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20260802)
    parser.add_argument("--json", dest="json_out")
    args = parser.parse_args()

    report: dict[str, Any] = {"transactions": args.transactions, "sizes": []}

    print(f"canon benchmark, {args.transactions} transactions per size")
    print()
    header = (f"{'rules':>7}  {'load ms':>8}  {'p50 us':>8}  {'p95 us':>8}  "
              f"{'p99 us':>8}  {'no trace':>9}  {'fired':>6}  {'reads':>6}")
    print(header)
    print("-" * len(header))

    for size in args.rules:
        ruleset = build_ruleset(size)
        load = time_load(size)
        traced = time_engine(ruleset, args.transactions, args.seed, True)
        untraced = time_engine(ruleset, args.transactions, args.seed, False)
        study = index_study(ruleset, min(args.transactions, 100), args.seed)

        print(f"{size:>7}  {load['load_ms']:>8}  {traced['p50_us']:>8}  "
              f"{traced['p95_us']:>8}  {traced['p99_us']:>8}  "
              f"{untraced['p50_us']:>9}  {traced['fired_median']:>6}  "
              f"{traced['fields_read_median']:>6}")

        report["sizes"].append({
            "rules": size, "load": load, "traced": traced,
            "untraced": untraced, "index_study": study,
        })

    print()
    print("what a discriminator index would leave to evaluate")
    print(f"{'rules':>7}  {'indexable':>10}  {'always':>7}  "
          f"{'candidates p50':>15}  {'reduction':>10}")
    for entry in report["sizes"]:
        study = entry["index_study"]
        print(f"{study['rules']:>7}  {study['indexable']:>10}  "
              f"{study['always_considered']:>7}  "
              f"{study['candidates_median']:>15}  "
              f"{study['reduction']:>9.1%}")

    print()
    print("Read the last table first. If the reduction is large, indexing is "
          "the optimisation to do, and it is the same trick RETE's alpha "
          "network uses. If it is small, your rules do not discriminate on "
          "equality and the answer is elsewhere.")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2, default=str),
                                       encoding="utf-8")
        print(f"\nwritten to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
