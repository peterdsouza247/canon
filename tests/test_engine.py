"""Evaluation, rule isolation, projections and traces."""

from __future__ import annotations

import pytest

from canon import Engine, RuleSet, load_mapping
from canon.errors import (RuleSetError, UndeclaredDependencyError,
                          UnplannedFactError)
from canon.facts import FactRequest, Projection


# --------------------------------------------------------------------------
# The example ruleset
# --------------------------------------------------------------------------


def test_ruleset_loads_and_stratifies(ftl_ruleset):
    assert len(ftl_ruleset) >= 15
    assert len(ftl_ruleset.strata) >= 2
    derivations = {rule.id for rule in ftl_ruleset.strata[0]}
    assert {"FTL-001", "FTL-002", "FTL-003"} <= derivations
    later = {rule.id for stratum in ftl_ruleset.strata[1:] for rule in stratum}
    assert "FTL-010" in later


def test_clean_scenario_passes(engine, scenarios):
    case = scenarios["legal"]
    decision = engine.evaluate(case["facts"], key=case["key"],
                               client=case["client"], as_of=case["as_of"])
    assert decision.ok, decision.render(verbose=True)
    assert decision.findings == []


def test_fdp_breach_is_detected_with_the_right_limit(engine, scenarios):
    case = scenarios["fdp_breach"]
    decision = engine.evaluate(case["facts"], key=case["key"],
                               client=case["client"], as_of=case["as_of"])
    assert not decision.ok
    assert "FTL_FDP_EXCEEDED" in decision.codes()
    # 13 base, minus 1.5 for three sectors past the second, minus 2 for being
    # unacclimatised; the most restrictive of the three wins.
    assert decision.derived["max_fdp_hours"] == pytest.approx(11.0)
    assert decision.derived["augmentation_credit"] == pytest.approx(0.0)


def test_vertical_slice_rule_reads_the_whole_roster(engine, scenarios):
    case = scenarios["fdp_breach"]
    decision = engine.evaluate(case["facts"], client=case["client"],
                               as_of=case["as_of"])
    assert "INEXPERIENCED_PILOT_PAIRING" in decision.codes()
    trace = decision.trace_for("CREW-002")
    assert "flight.roster[*].hours_on_type" in trace.reads
    assert "flight.roster[*].rank" in trace.reads


def test_composition_scenario_raises_the_roster_findings(engine, scenarios):
    case = scenarios["composition"]
    decision = engine.evaluate(case["facts"], client=case["client"],
                               as_of=case["as_of"])
    codes = decision.codes()
    assert "CABIN_CREW_BELOW_MINIMUM" in codes
    assert "LINE_TRAINING_WITHOUT_LTC" in codes
    assert "NO_LOCAL_LANGUAGE_SPEAKER" in codes


def test_augmentation_extends_rather_than_restricts(engine, scenarios):
    case = scenarios["qualifications"]
    decision = engine.evaluate(case["facts"], client=case["client"],
                               as_of=case["as_of"])
    assert decision.derived["augmentation_credit"] == pytest.approx(4.0)
    assert "FTL_FDP_EXCEEDED" not in decision.codes()


# --------------------------------------------------------------------------
# Multi tenancy and effective dating
# --------------------------------------------------------------------------


def test_tenant_rule_only_applies_to_its_client(engine, scenarios):
    case = scenarios["tenant_margin"]
    for_b = engine.evaluate(case["facts"], client="AIRLINE_B",
                            as_of=case["as_of"])
    for_a = engine.evaluate(case["facts"], client="AIRLINE_A",
                            as_of=case["as_of"])
    assert "REST_BELOW_OPERATOR_MARGIN" in for_b.codes()
    assert "REST_BELOW_OPERATOR_MARGIN" not in for_a.codes()
    assert for_b.ok and for_a.ok  # soft findings do not block


def test_projection_is_smaller_for_a_client_with_fewer_rules(ftl_ruleset):
    for_a = ftl_ruleset.projection_for("AIRLINE_A", None)
    for_b = ftl_ruleset.projection_for("AIRLINE_B", None)
    assert "crew.hotel_rest_grade" in for_b.paths
    assert "crew.hotel_rest_grade" not in for_a.paths
    assert for_b.leaf_count() > for_a.leaf_count()


def test_future_rule_is_dormant_until_its_effective_date(ftl_ruleset):
    from datetime import date

    before = ftl_ruleset.projection_for(None, date(2026, 8, 14))
    after = ftl_ruleset.projection_for(None, date(2026, 9, 15))
    assert "duty.encroaches_wocl" not in before.paths
    assert "duty.encroaches_wocl" in after.paths


def test_skip_reason_is_recorded_on_the_trace(engine, scenarios):
    case = scenarios["legal"]
    decision = engine.evaluate(case["facts"], client="AIRLINE_A",
                               as_of=case["as_of"])
    trace = decision.trace_for("FTL-090")
    assert trace.considered is False
    assert "AIRLINE_A" in trace.skip_reason


# --------------------------------------------------------------------------
# Payload accounting
# --------------------------------------------------------------------------


def test_only_planned_fields_are_read(engine, scenarios):
    case = scenarios["legal"]
    decision = engine.evaluate(case["facts"], client=case["client"],
                               as_of=case["as_of"])
    stats = decision.fact_stats
    assert stats["unplanned"] == []
    assert stats["read_paths"] <= stats["planned_paths"]
    assert stats["planned_paths"] > 0


def test_projection_trims_a_full_payload(ftl_ruleset, scenarios):
    full = scenarios["legal"]["facts"]
    trimmed = ftl_ruleset.projection.select(full)
    assert "seniority_years" not in trimmed["crew"]
    assert "rest_hours_before_duty" in trimmed["crew"]
    assert "languages" in trimmed["flight"]["roster"][0]
    assert "id" not in trimmed["flight"]["roster"][0]


def test_one_fetch_per_root(engine, scenarios):
    """Laziness must not turn into a fetch per field."""
    calls: dict[str, int] = {}

    def make(root: str, payload):
        def source(request: FactRequest):
            calls[root] = calls.get(root, 0) + 1
            return payload

        return source

    facts = scenarios["legal"]["facts"]
    sources = {root: make(root, payload) for root, payload in facts.items()}
    decision = engine.evaluate(sources, client="AIRLINE_A", as_of="2026-08-14")
    assert decision.ok
    assert all(count == 1 for count in calls.values()), calls


def test_unused_roots_are_never_fetched(ftl_ruleset, scenarios):
    """A guard that fails early should cost nothing downstream."""
    ruleset = ftl_ruleset.subset(["FTL-020"])
    engine = Engine(ruleset)
    touched: list[str] = []

    def make(root: str, payload):
        def source(request: FactRequest):
            touched.append(root)
            return payload

        return source

    facts = scenarios["legal"]["facts"]
    sources = {root: make(root, payload) for root, payload in facts.items()}
    engine.evaluate(sources)
    assert set(touched) == {"crew", "limits"}
    assert "flight" not in touched


# --------------------------------------------------------------------------
# Isolation
# --------------------------------------------------------------------------


UNDECLARED = {
    "ruleset": "bad",
    "rules": [
        {"id": "A", "set": {"x": "1"}},
        {"id": "B", "when": "derived.x > 0",
         "emit": {"code": "B_FIRED"}},
    ],
}

STALE_DECLARATION = {
    "ruleset": "bad",
    "rules": [
        {"id": "A", "set": {"x": "1"}},
        {"id": "B", "reads": ["derived.x"], "when": "1 > 0",
         "emit": {"code": "B_FIRED"}},
    ],
}

CYCLE = {
    "ruleset": "bad",
    "rules": [
        {"id": "A", "reads": ["derived.y"], "set": {"x": "derived.y + 1"}},
        {"id": "B", "reads": ["derived.x"], "set": {"y": "derived.x + 1"}},
    ],
}

CONFLICT = {
    "ruleset": "bad",
    "rules": [
        {"id": "A", "set": {"x": "1"}},
        {"id": "B", "set": {"x": "2"}},
    ],
}

MISSING_PRODUCER = {
    "ruleset": "bad",
    "rules": [
        {"id": "A", "reads": ["derived.nope"], "when": "derived.nope > 0",
         "emit": {"code": "A"}},
    ],
}


def test_undeclared_derived_read_is_refused():
    with pytest.raises(UndeclaredDependencyError):
        load_mapping(UNDECLARED)


def test_stale_declaration_is_refused():
    with pytest.raises(UndeclaredDependencyError):
        load_mapping(STALE_DECLARATION)


def test_cycle_is_refused():
    with pytest.raises(RuleSetError) as info:
        load_mapping(CYCLE)
    assert "cycle" in str(info.value)


def test_write_conflict_needs_a_policy():
    with pytest.raises(RuleSetError) as info:
        load_mapping(CONFLICT)
    assert "combine" in str(info.value)


def test_conflict_is_fine_once_declared():
    spec = dict(CONFLICT, derived={"x": {"combine": "min"}})
    ruleset = load_mapping(spec)
    decision = Engine(ruleset).evaluate({})
    assert decision.derived["x"] == 1


def test_missing_producer_is_refused():
    with pytest.raises(RuleSetError):
        load_mapping(MISSING_PRODUCER)


def test_rules_in_a_stratum_cannot_see_each_other(ftl_ruleset):
    """Structural, not behavioural: nothing in stratum 0 reads anything
    stratum 0 writes, which is what makes intra stratum order irrelevant."""
    for stratum in ftl_ruleset.strata:
        written = {name for rule in stratum for name in rule.produces}
        read = {path.split(".", 1)[1]
                for rule in stratum
                for path in ftl_ruleset.rule_derived_reads[rule.id]}
        assert not (written & read)


# --------------------------------------------------------------------------
# Determinism and traces
# --------------------------------------------------------------------------


def test_evaluation_is_deterministic(engine, scenarios):
    case = scenarios["fdp_breach"]
    first = engine.evaluate(case["facts"], client=case["client"],
                            as_of=case["as_of"])
    second = engine.evaluate(case["facts"], client=case["client"],
                             as_of=case["as_of"])
    assert first.output_digest == second.output_digest
    assert first.input_digest == second.input_digest


def test_explain_returns_the_emitting_rule_and_its_upstream(engine, scenarios):
    case = scenarios["fdp_breach"]
    decision = engine.evaluate(case["facts"], client=case["client"],
                               as_of=case["as_of"])
    chain = decision.explain("FTL_FDP_EXCEEDED")
    assert chain
    assert chain[0]["rule_id"] == "FTL-010"
    upstream = {entry["rule_id"] for entry in chain[1:]}
    assert {"FTL-002", "FTL-003"} <= upstream


def test_why_lists_every_contributor_to_a_derived_value(engine, scenarios):
    case = scenarios["fdp_breach"]
    decision = engine.evaluate(case["facts"], client=case["client"],
                               as_of=case["as_of"])
    contributors = {entry["rule_id"] for entry in decision.why("max_fdp_hours")}
    assert {"FTL-001", "FTL-002", "FTL-003"} <= contributors


def test_findings_are_ordered_by_severity(engine, scenarios):
    case = scenarios["fdp_breach"]
    decision = engine.evaluate(case["facts"], client=case["client"],
                               as_of=case["as_of"])
    severities = [f.severity for f in decision.findings]
    assert severities == sorted(severities,
                                key=lambda s: -{"hard": 3, "soft": 2,
                                                "advisory": 1, "info": 0}[s])


def test_projection_coverage_is_prefix_aware():
    projection = Projection(["crew.address.city"])
    assert projection.covers("crew.address.city")
    assert projection.covers("crew.address")
    assert not projection.covers("crew.rank")


def test_a_simple_ruleset_round_trips():
    spec = {
        "ruleset": "dynamic",
        "rules": [{"id": "A", "when": "crew.base == 'LHR'",
                   "emit": {"code": "A"}}],
    }
    decision = Engine(load_mapping(spec)).evaluate({"crew": {"base": "LHR"}})
    assert decision.codes() == ["A"]
    assert decision.fact_stats["unplanned"] == []
