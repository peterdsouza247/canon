"""Shadow running: prove the new engine matches the old one before you trust it.

No airline is going to swap the engine that decides whether a crew member may
legally operate a flight on the strength of a green test suite. The only
argument that works is empirical: here are three months of real transactions,
here is where we agreed, here is every case where we did not, and here is the
rule responsible for each disagreement.

That is what this module produces. It runs Canon beside the incumbent, compares
the outcomes, and does one thing beyond a plain diff: it attributes divergence
to rules. A rule that fires on eighty per cent of diverging cases and two per
cent of agreeing cases is almost certainly the problem, and the report says so
rather than leaving somebody to work it out from a spreadsheet.
"""

from __future__ import annotations

import json
import random
import statistics
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .engine import Engine
from .trace import Decision

__all__ = ["ShadowCase", "Comparison", "ShadowReport", "ShadowRunner",
           "load_cases_jsonl"]

MATCH = "match"
VALUE_MISMATCH = "value_mismatch"
MISSING_IN_CANON = "missing_in_canon"
EXTRA_IN_CANON = "extra_in_canon"
BOTH_DIFFER = "both_differ"
CANON_ERROR = "canon_error"
LEGACY_ERROR = "legacy_error"

DIVERGENT = (VALUE_MISMATCH, MISSING_IN_CANON, EXTRA_IN_CANON, BOTH_DIFFER,
             CANON_ERROR, LEGACY_ERROR)


@dataclass
class ShadowCase:
    """One captured production transaction."""

    id: str
    facts: Mapping[str, Any]
    key: Mapping[str, Any] = field(default_factory=dict)
    client: str | None = None
    as_of: date | str | None = None
    legacy_codes: Sequence[str] | None = None
    legacy_micros: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ShadowCase":
        return cls(
            id=str(raw.get("id") or raw.get("case_id") or ""),
            facts=raw.get("facts") or {},
            key=raw.get("key") or {},
            client=raw.get("client"),
            as_of=raw.get("as_of"),
            legacy_codes=raw.get("legacy_codes"),
            legacy_micros=raw.get("legacy_micros"),
            metadata=raw.get("metadata") or {},
        )


@dataclass
class Comparison:
    case_id: str
    status: str
    canon_codes: list[str] = field(default_factory=list)
    legacy_codes: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    canon_micros: float = 0.0
    legacy_micros: float = 0.0
    canon_rules_fired: list[str] = field(default_factory=list)
    error: str | None = None
    decision: Decision | None = None

    @property
    def diverged(self) -> bool:
        return self.status != MATCH

    def to_dict(self, include_decision: bool = False) -> dict[str, Any]:
        out = {
            "case_id": self.case_id,
            "status": self.status,
            "canon_codes": self.canon_codes,
            "legacy_codes": self.legacy_codes,
            "missing_in_canon": self.missing,
            "extra_in_canon": self.extra,
            "canon_micros": round(self.canon_micros, 1),
            "legacy_micros": round(self.legacy_micros, 1),
            "rules_fired": self.canon_rules_fired,
            "error": self.error,
        }
        if include_decision and self.decision is not None:
            out["decision"] = self.decision.to_dict()
        return out


@dataclass
class ShadowReport:
    comparisons: list[Comparison] = field(default_factory=list)

    # -- headline numbers -------------------------------------------------

    @property
    def total(self) -> int:
        return len(self.comparisons)

    @property
    def matched(self) -> int:
        return sum(1 for c in self.comparisons if c.status == MATCH)

    @property
    def agreement(self) -> float:
        return (self.matched / self.total) if self.total else 0.0

    def by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for comparison in self.comparisons:
            counts[comparison.status] = counts.get(comparison.status, 0) + 1
        return dict(sorted(counts.items()))

    def by_code(self) -> dict[str, dict[str, int]]:
        """Divergence counted per finding code, in both directions."""
        out: dict[str, dict[str, int]] = {}
        for comparison in self.comparisons:
            for code in comparison.missing:
                out.setdefault(code, {"missing_in_canon": 0, "extra_in_canon": 0})
                out[code]["missing_in_canon"] += 1
            for code in comparison.extra:
                out.setdefault(code, {"missing_in_canon": 0, "extra_in_canon": 0})
                out[code]["extra_in_canon"] += 1
        return dict(sorted(out.items(),
                           key=lambda kv: -(kv[1]["missing_in_canon"]
                                            + kv[1]["extra_in_canon"])))

    def suspects(self, limit: int = 10) -> list[dict[str, Any]]:
        """Rules ranked by how much more often they fire on diverging cases.

        A rule that fires everywhere is not evidence. A rule that fires on most
        of the failures and almost none of the successes is. Lift is the ratio
        of those two rates, which is crude but reliably points at the right
        handful of rules in a ruleset of hundreds.
        """
        diverged = [c for c in self.comparisons if c.diverged]
        agreed = [c for c in self.comparisons if not c.diverged]
        if not diverged:
            return []
        rules: set[str] = set()
        for comparison in self.comparisons:
            rules.update(comparison.canon_rules_fired)

        rows: list[dict[str, Any]] = []
        for rule_id in rules:
            in_bad = sum(1 for c in diverged if rule_id in c.canon_rules_fired)
            in_good = sum(1 for c in agreed if rule_id in c.canon_rules_fired)
            bad_rate = in_bad / len(diverged)
            good_rate = (in_good / len(agreed)) if agreed else 0.0
            lift = bad_rate / good_rate if good_rate else (
                float("inf") if bad_rate else 0.0)
            rows.append({
                "rule_id": rule_id,
                "fired_in_divergent": in_bad,
                "fired_in_matching": in_good,
                "divergent_rate": round(bad_rate, 4),
                "matching_rate": round(good_rate, 4),
                "lift": None if lift == float("inf") else round(lift, 2),
                "only_in_divergent": lift == float("inf"),
            })
        rows.sort(key=lambda r: (r["only_in_divergent"], r["lift"] or 0,
                                 r["fired_in_divergent"]), reverse=True)
        return rows[:limit]

    def latency(self) -> dict[str, Any]:
        canon = [c.canon_micros for c in self.comparisons if c.canon_micros]
        legacy = [c.legacy_micros for c in self.comparisons if c.legacy_micros]

        def summarise(values: list[float]) -> dict[str, float] | None:
            if not values:
                return None
            ordered = sorted(values)
            return {
                "count": len(ordered),
                "mean_micros": round(statistics.fmean(ordered), 1),
                "p50_micros": round(ordered[len(ordered) // 2], 1),
                "p95_micros": round(ordered[min(len(ordered) - 1,
                                                int(len(ordered) * 0.95))], 1),
                "max_micros": round(ordered[-1], 1),
            }

        return {"canon": summarise(canon), "legacy": summarise(legacy)}

    def divergent_cases(self, limit: int = 25) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self.comparisons if c.diverged][:limit]

    def to_dict(self, sample: int = 25) -> dict[str, Any]:
        return {
            "total": self.total,
            "matched": self.matched,
            "agreement": round(self.agreement, 6),
            "by_status": self.by_status(),
            "by_code": self.by_code(),
            "suspects": self.suspects(),
            "latency": self.latency(),
            "divergent_sample": self.divergent_cases(sample),
        }

    def render(self) -> str:
        lines = [
            f"shadow run: {self.total} cases, {self.matched} matched "
            f"({self.agreement:.4%} agreement)",
            "",
            "status breakdown",
        ]
        for status, count in self.by_status().items():
            lines.append(f"  {status:<20} {count}")
        codes = self.by_code()
        if codes:
            lines.append("")
            lines.append("divergence by finding code")
            for code, counts in list(codes.items())[:15]:
                lines.append(
                    f"  {code:<32} missing {counts['missing_in_canon']:<6} "
                    f"extra {counts['extra_in_canon']}")
        suspects = self.suspects()
        if suspects:
            lines.append("")
            lines.append("most likely rules behind the divergence")
            for row in suspects:
                lift = "only in failures" if row["only_in_divergent"] \
                    else f"lift {row['lift']}"
                lines.append(
                    f"  {row['rule_id']:<20} fired in {row['fired_in_divergent']} "
                    f"divergent / {row['fired_in_matching']} matching  ({lift})")
        latency = self.latency()
        if latency["canon"]:
            lines.append("")
            canon = latency["canon"]
            lines.append(
                f"canon latency  p50 {canon['p50_micros']}us  "
                f"p95 {canon['p95_micros']}us")
            if latency["legacy"]:
                legacy = latency["legacy"]
                lines.append(
                    f"legacy latency p50 {legacy['p50_micros']}us  "
                    f"p95 {legacy['p95_micros']}us")
        return "\n".join(lines)


class ShadowRunner:
    """Runs Canon against a legacy decision function and compares outcomes."""

    def __init__(self, engine: Engine,
                 legacy: Callable[[ShadowCase], Any] | None = None, *,
                 code_map: Mapping[str, str] | None = None,
                 ignore_codes: Iterable[str] = (),
                 sample_rate: float = 1.0,
                 seed: int = 20260801) -> None:
        self.engine = engine
        self.legacy = legacy
        self.code_map = dict(code_map or {})
        self.ignore = set(ignore_codes)
        self.sample_rate = sample_rate
        self._random = random.Random(seed)

    # -- normalisation ----------------------------------------------------

    def _normalise(self, codes: Iterable[str]) -> list[str]:
        mapped = [self.code_map.get(code, code) for code in codes]
        return sorted({code for code in mapped if code not in self.ignore})

    def _legacy_codes(self, case: ShadowCase) -> tuple[list[str], float, str | None]:
        if case.legacy_codes is not None:
            return (self._normalise(case.legacy_codes),
                    case.legacy_micros or 0.0, None)
        if self.legacy is None:
            return [], 0.0, "no legacy result available for this case"
        started = time.perf_counter_ns()
        try:
            result = self.legacy(case)
        except Exception as exc:  # noqa: BLE001 - the legacy side is not ours
            return [], 0.0, f"{type(exc).__name__}: {exc}"
        elapsed = (time.perf_counter_ns() - started) / 1000.0
        if isinstance(result, dict):
            codes = result.get("codes") or []
        else:
            codes = list(result or [])
        return self._normalise(codes), elapsed, None

    # -- running ----------------------------------------------------------

    def compare(self, case: ShadowCase, keep_decision: bool = False) -> Comparison:
        legacy_codes, legacy_micros, legacy_error = self._legacy_codes(case)

        decision = None
        canon_error = None
        canon_codes: list[str] = []
        rules_fired: list[str] = []
        canon_micros = 0.0
        try:
            decision = self.engine.evaluate(
                dict(case.facts), key=case.key, client=case.client,
                as_of=case.as_of)
            canon_codes = self._normalise(decision.codes())
            rules_fired = decision.rules_fired()
            canon_micros = decision.micros
            if decision.errors:
                canon_error = "; ".join(decision.errors)
        except Exception as exc:  # noqa: BLE001 - report rather than abort a run
            canon_error = f"{type(exc).__name__}: {exc}"

        if canon_error:
            status = CANON_ERROR
        elif legacy_error:
            status = LEGACY_ERROR
        else:
            missing = [c for c in legacy_codes if c not in canon_codes]
            extra = [c for c in canon_codes if c not in legacy_codes]
            if not missing and not extra:
                status = MATCH
            elif missing and extra:
                status = BOTH_DIFFER
            elif missing:
                status = MISSING_IN_CANON
            else:
                status = EXTRA_IN_CANON
            return Comparison(
                case_id=case.id, status=status,
                canon_codes=canon_codes, legacy_codes=legacy_codes,
                missing=missing, extra=extra,
                canon_micros=canon_micros, legacy_micros=legacy_micros,
                canon_rules_fired=rules_fired,
                decision=decision if keep_decision else None,
            )

        return Comparison(
            case_id=case.id, status=status,
            canon_codes=canon_codes, legacy_codes=legacy_codes,
            missing=[c for c in legacy_codes if c not in canon_codes],
            extra=[c for c in canon_codes if c not in legacy_codes],
            canon_micros=canon_micros, legacy_micros=legacy_micros,
            canon_rules_fired=rules_fired,
            error=canon_error or legacy_error,
            decision=decision if keep_decision else None,
        )

    def run(self, cases: Iterable[ShadowCase],
            keep_decisions: bool = False) -> ShadowReport:
        report = ShadowReport()
        for case in cases:
            if self.sample_rate < 1.0 and self._random.random() > self.sample_rate:
                continue
            report.comparisons.append(self.compare(case, keep_decisions))
        return report


def load_cases_jsonl(path: str | Path) -> list[ShadowCase]:
    """Read captured transactions, one JSON object per line.

    JSON Lines because capture files get large, arrive incrementally, and want
    to be greppable. A day of traffic streams through this without being held in
    memory twice.
    """
    cases: list[ShadowCase] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            raw = json.loads(line)
            if not raw.get("id"):
                raw["id"] = f"line-{line_number}"
            cases.append(ShadowCase.from_dict(raw))
    return cases
