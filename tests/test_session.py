"""Interactive editing: incremental re-evaluation, and proving it is correct."""

from __future__ import annotations

import pytest

from canon import Engine, load_mapping
# Imported from the defining module rather than the package. A missing
# re-export in canon/__init__.py should fail one small test that says so, not
# break collection of this module and abort the whole run.
from canon.session import Session, apply_changes, diff_facts

SPEC = {
    "ruleset": "editing",
    "version": "1",
    "derived": {"limit": {"combine": "min"}},
    "rules": [
        {"id": "D-BASE", "version": "1", "priority": 10,
         "set": {"limit": "limits.base"}},
        {"id": "D-SECTORS", "version": "1", "priority": 11,
         "when": "duty.sectors > 2",
         "set": {"limit": "limits.base - 0.5 * (duty.sectors - 2)"}},
        {"id": "FDP", "version": "1", "priority": 20,
         "reads": ["derived.limit"],
         "when": "duty.hours > derived.limit",
         "emit": {"code": "FDP_EXCEEDED", "severity": "hard",
                  "message": "duty of {h}h over the {l}h limit",
                  "detail": {"h": "duty.hours", "l": "derived.limit"}}},
        {"id": "REST", "version": "1", "priority": 21,
         "when": "crew.rest < 12",
         "emit": {"code": "REST_SHORT", "severity": "hard"}},
        {"id": "CABIN", "version": "1", "priority": 30,
         "when": "sum(1 for m in flight.roster if m.rank in ['SCC','CC']) < 3",
         "emit": {"code": "CABIN_SHORT", "severity": "hard"}},
        {"id": "GREEN", "version": "1", "priority": 31,
         "when": "sum(1 for m in flight.roster if m.hours_on_type < 100) > 1",
         "emit": {"code": "TOO_GREEN", "severity": "hard"}},
    ],
}


def facts() -> dict:
    return {
        "limits": {"base": 13.0},
        "duty": {"hours": 10.0, "sectors": 2},
        "crew": {"rest": 14.0},
        "flight": {"roster": [
            {"rank": "CP", "hours_on_type": 4000},
            {"rank": "FO", "hours_on_type": 900},
            {"rank": "SCC", "hours_on_type": 2000},
            {"rank": "CC", "hours_on_type": 800},
            {"rank": "CC", "hours_on_type": 400},
        ]},
    }


@pytest.fixture
def session() -> Session:
    return Session(Engine(load_mapping(SPEC), strict_facts=False), facts())


# --------------------------------------------------------------------------
# Editing and diffing facts
# --------------------------------------------------------------------------


def test_dotted_paths_and_subtrees_both_work():
    updated = apply_changes(facts(), {"duty.hours": 15.0,
                                      "crew": {"rest": 9.0}})
    assert updated["duty"]["hours"] == 15.0
    assert updated["crew"]["rest"] == 9.0
    assert updated["duty"]["sectors"] == 2, "a merge must not drop siblings"


def test_editing_an_element_of_a_collection():
    updated = apply_changes(facts(), {"flight.roster.1.hours_on_type": 40})
    assert updated["flight"]["roster"][1]["hours_on_type"] == 40


def test_the_original_facts_are_not_mutated():
    original = facts()
    apply_changes(original, {"duty.hours": 99.0})
    assert original["duty"]["hours"] == 10.0


def test_diff_uses_the_notation_the_rules_use():
    before = facts()
    after = apply_changes(before, {"flight.roster.1.hours_on_type": 40})
    assert diff_facts(before, after) == {"flight.roster[*].hours_on_type"}


def test_a_collection_changing_length_is_reported_coarsely():
    before = facts()
    after = apply_changes(before, {"flight.roster": before["flight"]["roster"][:3]})
    assert diff_facts(before, after) == {"flight.roster"}


# --------------------------------------------------------------------------
# Incremental evaluation
# --------------------------------------------------------------------------


def test_an_edit_that_breaks_something_says_what_and_why(session):
    delta = session.apply({"duty.hours": 15.0})
    assert delta.ok_before and not delta.ok_after
    assert [f["code"] for f in delta.newly_raised] == ["FDP_EXCEEDED"]
    assert delta.newly_raised[0]["because"]["paths"] == ["duty.hours"]


def test_an_edit_that_fixes_something_is_reported_too(session):
    session.apply({"crew.rest": 9.0})
    delta = session.apply({"crew.rest": 13.0})
    assert [f["code"] for f in delta.no_longer_raised] == ["REST_SHORT"]
    assert delta.ok_after


def test_unrelated_rules_are_not_re_evaluated(session):
    """Editing rest cannot move the cabin crew count, and the engine knows it
    without being told, because CABIN never read anything that changed."""
    delta = session.apply({"crew.rest": 9.0})
    assert "REST" in delta.rules_recomputed
    assert "CABIN" not in delta.rules_recomputed
    assert "GREEN" not in delta.rules_recomputed
    assert delta.rules_reused >= 4


def test_a_knock_on_through_a_derived_value_is_attributed(session):
    """FDP did not read duty.sectors. D-SECTORS did, and FDP reads the limit
    D-SECTORS writes. The planner is told the sector count is the cause."""
    delta = session.apply({"duty.hours": 12.0, "duty.sectors": 5})
    assert [f["code"] for f in delta.newly_raised] == ["FDP_EXCEEDED"]
    because = delta.newly_raised[0]["because"]
    assert "duty.sectors" in because["paths"]
    assert "D-SECTORS" in because["via_rules"]
    assert delta.derived_moved["limit"]["before"] == pytest.approx(13.0)
    assert delta.derived_moved["limit"]["after"] == pytest.approx(11.5)


def test_a_derived_change_invalidates_its_readers(session):
    """The dirty set has to travel through the derived namespace, otherwise a
    reader keeps a stale verdict."""
    session.apply({"duty.hours": 12.0})
    delta = session.apply({"duty.sectors": 6})
    assert "FDP" in delta.rules_recomputed
    assert session.verify() == []


def test_editing_a_crew_member_only_touches_roster_rules(session):
    delta = session.apply({"flight.roster.1.hours_on_type": 40})
    assert "GREEN" in delta.rules_recomputed
    assert "REST" not in delta.rules_recomputed
    assert "FDP" not in delta.rules_recomputed


def test_an_edit_that_changes_nothing_costs_nothing(session):
    delta = session.apply({"duty.hours": 10.0})
    assert delta.changed_paths == []
    assert not delta.moved
    assert delta.rules_recomputed == []


# --------------------------------------------------------------------------
# The property that matters: incremental agrees with from scratch
# --------------------------------------------------------------------------


EDITS = [
    {"duty.hours": 15.0},
    {"duty.sectors": 5},
    {"crew.rest": 9.0},
    {"flight.roster.1.hours_on_type": 40},
    {"flight.roster.4.hours_on_type": 60},
    {"duty.hours": 8.0},
    {"crew.rest": 20.0},
    {"duty.sectors": 1},
    {"limits.base": 9.0},
    {"flight.roster.2.rank": "CP"},
]


def test_every_edit_agrees_with_a_full_evaluation(session):
    """The incremental path and the from scratch path are the same code over a
    deterministic evaluator, so they can be checked against each other after
    every single edit. This is the audit a stale matching network cannot give
    you."""
    for edit in EDITS:
        session.apply(edit)
        assert session.verify() == [], f"diverged after {edit}"


def test_a_long_edit_sequence_lands_where_a_cold_run_would(session):
    for edit in EDITS:
        session.apply(edit)
    cold = Engine(load_mapping(SPEC), strict_facts=False).evaluate(session.facts)
    assert sorted(session.decision.codes()) == sorted(cold.codes())
    assert session.decision.derived == cold.derived


def test_most_work_is_avoided_across_a_session(session):
    for edit in EDITS:
        session.apply(edit)
    stats = session.stats()
    assert stats["edits"] == len(EDITS)
    assert stats["work_avoided"] > 0.4, stats


# --------------------------------------------------------------------------
# Preview and speculative scoring
# --------------------------------------------------------------------------


def test_preview_does_not_commit(session):
    before = session.decision.output_digest
    delta = session.preview({"duty.hours": 15.0})
    assert delta.newly_raised
    assert session.decision.output_digest == before
    assert session.facts["duty"]["hours"] == 10.0


def test_score_ranks_candidate_edits(session):
    results = session.score({
        "keep": {},
        "longer": {"duty.hours": 15.0},
        "much_longer": {"duty.hours": 20.0},
        "short_rest": {"crew.rest": 8.0},
    })
    assert results["keep"].ok_after
    assert not results["longer"].ok_after
    assert not results["short_rest"].ok_after
    # nothing was committed by any of them
    assert session.decision.ok
    assert session.verify() == []
