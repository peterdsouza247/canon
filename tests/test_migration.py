"""Authoring front ends, the ODM importer and the shadow harness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from canon import Engine, ShadowCase, ShadowRunner, load_mapping
from canon.errors import MigrationError, RuleDefinitionError
from canon.loaders import load_decision_table_text
from canon.odm_import import Verbalisation, parse_bal

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# Decision tables
# --------------------------------------------------------------------------


TABLE = """id,version,when crew.rank ==,when crew.hours_on_type <,when crew.qualifications not contains,then code,then severity,then message
T1,1,CP,100,,LOW_TIME_CAPTAIN,hard,Captain below the minimum hours on type
T2,1,,,ETOPS,ETOPS_NOT_QUALIFIED,hard,Not ETOPS qualified
"""


def test_decision_table_compiles_cells_into_conditions():
    ruleset = load_decision_table_text(TABLE, source="table.csv")
    assert len(ruleset) == 2
    assert ruleset.by_id["T1"].when.source == \
        "crew.rank == \"CP\" and crew.hours_on_type < 100"
    assert ruleset.by_id["T2"].when.source == '"ETOPS" not in crew.qualifications'


def test_decision_table_evaluates():
    ruleset = load_decision_table_text(TABLE, source="table.csv")
    decision = Engine(ruleset).evaluate({"crew": {
        "rank": "CP", "hours_on_type": 40, "qualifications": ["A320"]}})
    assert sorted(decision.codes()) == ["ETOPS_NOT_QUALIFIED", "LOW_TIME_CAPTAIN"]


def test_example_decision_table_loads(quals_ruleset):
    assert {"QUAL-001", "QUAL-005"} <= set(quals_ruleset.by_id)
    projection = quals_ruleset.projection
    assert "crew.qualifications" in projection.paths
    assert "flight.aircraft_type" in projection.paths


def test_qualification_table_catches_the_expected_failures(quals_ruleset,
                                                           scenarios):
    case = scenarios["qualifications"]
    decision = Engine(quals_ruleset).evaluate(case["facts"],
                                              client=case["client"])
    codes = set(decision.codes())
    assert "ETOPS_NOT_QUALIFIED" in codes
    assert "NOT_TYPE_RATED" in codes      # holds A320, flight is a B787
    assert "MEDICAL_EXPIRED" in codes     # expires 2026-08-10, departs 08-14
    assert "LINE_CHECK_LAPSED" in codes


# --------------------------------------------------------------------------
# The Python front end
# --------------------------------------------------------------------------


def test_python_and_yaml_produce_the_same_rule():
    from canon.dsl import RuleSetBuilder, emit  # noqa: F401

    builder = RuleSetBuilder("py", auto_reads=False)

    @builder.rule("FTL-020", version="3", priority=21)
    def minimum_rest(f):
        """Rest before report must meet the minimum."""
        if f.crew.rest_hours_before_duty < f.limits.min_rest_hours:
            emit("REST_INSUFFICIENT",
                 severity="hard",
                 message="Rest of {actual}h before report falls short of the "
                         "{required}h minimum",
                 actual=f.crew.rest_hours_before_duty,
                 required=f.limits.min_rest_hours)

    from_python = builder.build().by_id["FTL-020"]

    from_yaml = load_mapping({
        "ruleset": "y",
        "rules": [{
            "id": "FTL-020", "version": "3", "priority": 21,
            "when": "crew.rest_hours_before_duty < limits.min_rest_hours",
            "emit": {
                "code": "REST_INSUFFICIENT", "severity": "hard",
                "message": "Rest of {actual}h before report falls short of "
                           "the {required}h minimum",
                "detail": {"actual": "crew.rest_hours_before_duty",
                           "required": "limits.min_rest_hours"},
            },
        }],
    }).by_id["FTL-020"]

    assert from_python.content_hash == from_yaml.content_hash


def test_python_front_end_discovers_derived_reads():
    from canon.dsl import RuleSetBuilder, emit, set_  # noqa: F401

    builder = RuleSetBuilder("py")

    @builder.rule("A", version="1")
    def produce(f):
        """Set a limit."""
        set_(limit=f.limits.base)

    @builder.rule("B", version="1")
    def consume(f):
        """Check against the limit."""
        if f.duty.hours > f.derived.limit:
            emit("OVER")

    ruleset = builder.build()
    assert ruleset.by_id["B"].reads == ("derived.limit",)


def test_else_is_refused_with_an_explanation():
    from canon.dsl import RuleSetBuilder, emit  # noqa: F401

    builder = RuleSetBuilder("py")

    with pytest.raises(RuleDefinitionError) as info:
        @builder.rule("A", version="1")
        def branching(f):
            """Two outcomes."""
            if f.crew.rank == "CP":
                emit("IS_CAPTAIN")
            else:
                emit("NOT_CAPTAIN")

    assert "one rule per outcome" in str(info.value)


def test_loops_are_refused():
    from canon.dsl import RuleSetBuilder, emit  # noqa: F401

    builder = RuleSetBuilder("py")

    with pytest.raises(RuleDefinitionError):
        @builder.rule("A", version="1")
        def looping(f):
            """Not allowed."""
            for member in f.flight.roster:
                emit("SOMETHING")


# --------------------------------------------------------------------------
# The ODM importer
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def verbalisation() -> Verbalisation:
    return Verbalisation.load(ROOT / "examples" / "odm" / "verbalisation.json")


@pytest.fixture(scope="module")
def bal_text() -> str:
    return (ROOT / "examples" / "odm" / "legacy_ftl.bal").read_text(encoding="utf-8")


def test_importer_converts_the_simple_rules(bal_text, verbalisation):
    result = parse_bal(bal_text, verbalisation, ruleset_id="imported")
    assert "FTL_010_MaxDutyPeriod" in result.converted
    assert "FTL_020_MinimumRest" in result.converted
    assert "QUAL_002_Etops" in result.converted


def test_importer_refuses_a_collection_quantifier(bal_text, verbalisation):
    result = parse_bal(bal_text, verbalisation)
    refused = {item["rule"] for item in result.needs_review}
    assert "CREW_002_InexperiencedPairing" in refused


def test_importer_refuses_mixed_precedence(bal_text, verbalisation):
    result = parse_bal(bal_text, verbalisation)
    refused = {item["rule"]: item["reason"] for item in result.needs_review}
    assert "FTL_045_MixedPrecedence" in refused
    assert "bracket" in refused["FTL_045_MixedPrecedence"]


def test_imported_rules_are_a_loadable_ruleset(bal_text, verbalisation):
    result = parse_bal(bal_text, verbalisation, ruleset_id="imported")
    ruleset = result.to_ruleset()
    assert len(ruleset) == len(result.converted)
    decision = Engine(ruleset).evaluate({
        "duty": {"duty_hours": 14},
        "crew": {"rest_hours_before_duty": 8, "hours_last_28d": 44,
                 "qualifications": ["A320"]},
        "flight": {"is_etops": True},
    })
    assert "FTL_FDP_EXCEEDED" in decision.codes()
    assert "REST_INSUFFICIENT" in decision.codes()
    assert "ETOPS_NOT_QUALIFIED" in decision.codes()


def test_importer_refuses_an_unmapped_object(verbalisation):
    text = """
rule X {
	when {
		the salary of 'the payroll record' is more than 10 ;
	} then {
		add error "Nope" to 'the result' ;
	}
}
"""
    result = parse_bal(text, verbalisation)
    assert result.converted == []
    assert "no fact root mapped" in result.needs_review[0]["reason"]


def test_importer_coverage_is_reported(bal_text, verbalisation):
    result = parse_bal(bal_text, verbalisation)
    assert 0.0 < result.coverage < 1.0


# --------------------------------------------------------------------------
# Shadow running
# --------------------------------------------------------------------------


SHADOW_RULES = {
    "ruleset": "shadow_demo",
    "rules": [
        {"id": "R_FDP", "when": "duty.hours > 13", "emit": {"code": "FDP"}},
        {"id": "R_REST", "when": "crew.rest < 12", "emit": {"code": "REST"}},
        {"id": "R_NEW", "when": "crew.training == True",
         "emit": {"code": "TRAINING"}},
    ],
}


def _case(case_id: str, hours: float, rest: float, training: bool,
          legacy: list[str]) -> ShadowCase:
    return ShadowCase(
        id=case_id,
        facts={"duty": {"hours": hours},
               "crew": {"rest": rest, "training": training}},
        legacy_codes=legacy,
        legacy_micros=5000.0,
    )


def test_shadow_classifies_agreement_and_divergence():
    runner = ShadowRunner(Engine(load_mapping(SHADOW_RULES)))
    report = runner.run([
        _case("a", 12, 14, False, []),                     # match
        _case("b", 14, 14, False, ["FDP"]),                # match
        _case("c", 12, 10, False, ["FDP", "REST"]),        # legacy over-fires
        _case("d", 12, 14, True, []),                      # canon has a new rule
    ])
    assert report.total == 4
    assert report.matched == 2
    by_status = report.by_status()
    assert by_status.get("missing_in_canon") == 1
    assert by_status.get("extra_in_canon") == 1


def test_shadow_attributes_divergence_to_the_responsible_rule():
    runner = ShadowRunner(Engine(load_mapping(SHADOW_RULES)))
    cases = [_case(f"ok-{i}", 12, 14, False, []) for i in range(20)]
    cases += [_case(f"bad-{i}", 12, 14, True, []) for i in range(5)]
    report = runner.run(cases)
    top = report.suspects()[0]
    assert top["rule_id"] == "R_NEW"
    assert top["only_in_divergent"] is True


def test_shadow_code_mapping_absorbs_a_rename():
    runner = ShadowRunner(Engine(load_mapping(SHADOW_RULES)),
                          code_map={"LEGACY_FDP_CODE": "FDP"})
    report = runner.run([_case("a", 14, 14, False, ["LEGACY_FDP_CODE"])])
    assert report.matched == 1


def test_shadow_ignores_codes_on_request():
    runner = ShadowRunner(Engine(load_mapping(SHADOW_RULES)),
                          ignore_codes=["TRAINING"])
    report = runner.run([_case("a", 12, 14, True, [])])
    assert report.matched == 1


def test_shadow_report_serialises():
    runner = ShadowRunner(Engine(load_mapping(SHADOW_RULES)))
    report = runner.run([_case("a", 14, 10, True, ["FDP"])])
    payload = json.loads(json.dumps(report.to_dict(), default=str))
    assert payload["total"] == 1
    assert payload["agreement"] == 0.0
