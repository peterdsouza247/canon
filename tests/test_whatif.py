"""Proposals and what-if replay."""

from __future__ import annotations

from pathlib import Path

import pytest

from canon import Engine, ShadowCase, load_mapping
from canon.errors import RuleSetError
from canon.proposal import Proposal
from canon.whatif import (DERIVED_CHANGED, NEWLY_ALLOWED, NEWLY_BLOCKED,
                          UNCHANGED, WhatIf)

ROOT = Path(__file__).resolve().parents[1]


BASE = {
    "ruleset": "demo",
    "version": "1",
    "derived": {"limit": {"combine": "min"}},
    "rules": [
        {"id": "D-BASE", "version": "1", "priority": 10,
         "set": {"limit": "limits.base"}},
        {"id": "D-SECTORS", "version": "1", "priority": 11,
         "when": "duty.sectors > 2",
         "set": {"limit": "limits.base - 0.5 * (duty.sectors - 2)"}},
        {"id": "CHECK", "version": "1", "priority": 20,
         "reads": ["derived.limit"],
         "when": "duty.hours > derived.limit",
         "emit": {"code": "OVER_LIMIT", "severity": "hard"}},
        {"id": "REST", "version": "1", "priority": 21,
         "when": "crew.rest < 12",
         "emit": {"code": "REST_SHORT", "severity": "hard"}},
        {"id": "DORMANT", "version": "1", "priority": 30,
         "when": "crew.rest < 0",
         "emit": {"code": "IMPOSSIBLE", "severity": "hard"}},
    ],
}


def case(case_id: str, hours: float, sectors: int, rest: float) -> ShadowCase:
    return ShadowCase(
        id=case_id,
        facts={
            "limits": {"base": 13.0},
            "duty": {"hours": hours, "sectors": sectors},
            "crew": {"rest": rest},
        },
        key={"crew_id": case_id},
    )


CORPUS = [
    case("c1", 10.0, 2, 14.0),   # nothing fires
    case("c2", 12.0, 4, 14.0),   # baseline limit 12.0, candidate 11.5
    case("c3", 14.0, 2, 14.0),   # over in both
    case("c4", 10.0, 2, 11.0),   # rest short in both
    case("c5", 11.6, 4, 14.0),   # baseline fine, candidate over
]


# --------------------------------------------------------------------------
# Proposals
# --------------------------------------------------------------------------


def test_proposal_modifies_only_what_it_names():
    base = load_mapping(BASE)
    proposal = Proposal.from_mapping({
        "proposal": "steeper", "against": "demo", "version": "2",
        "modify": [{"id": "D-SECTORS", "version": "2",
                    "set": {"limit": "limits.base - 0.75 * (duty.sectors - 2)"}}],
    })
    candidate = proposal.apply(base)

    assert candidate.version == "2"
    assert len(candidate) == len(base)
    assert candidate.by_id["D-SECTORS"].content_hash != base.by_id["D-SECTORS"].content_hash
    for rule_id in ("D-BASE", "CHECK", "REST", "DORMANT"):
        assert candidate.by_id[rule_id].content_hash == base.by_id[rule_id].content_hash


def test_proposal_does_not_mutate_the_baseline():
    base = load_mapping(BASE)
    before = base.content_hash
    Proposal.from_mapping({
        "proposal": "p", "modify": [{"id": "REST", "when": "crew.rest < 13"}],
    }).apply(base)
    assert base.content_hash == before


def test_proposal_can_add_and_remove():
    base = load_mapping(BASE)
    candidate = Proposal.from_mapping({
        "proposal": "p",
        "add": [{"id": "NEW", "version": "1", "when": "crew.rest > 100",
                 "emit": {"code": "NEW_CODE"}}],
        "remove": ["DORMANT"],
    }).apply(base)
    assert "NEW" in candidate.by_id
    assert "DORMANT" not in candidate.by_id


def test_proposal_against_the_wrong_ruleset_is_refused():
    with pytest.raises(RuleSetError):
        Proposal.from_mapping({"proposal": "p", "against": "something_else"}) \
            .apply(load_mapping(BASE))


def test_modifying_a_rule_that_does_not_exist_is_refused():
    with pytest.raises(RuleSetError) as info:
        Proposal.from_mapping({"proposal": "p",
                               "modify": [{"id": "NOPE", "priority": 1}]}) \
            .apply(load_mapping(BASE))
    assert "not in ruleset" in str(info.value)


def test_a_proposal_that_introduces_a_cycle_is_refused_at_apply_time():
    """A proposal is validated like any other ruleset, so a change that closes a
    loop in the dependency graph is refused before it can be replayed, let alone
    shipped."""
    with pytest.raises(RuleSetError) as info:
        Proposal.from_mapping({
            "proposal": "cyclic",
            "modify": [{"id": "D-BASE", "reads": ["derived.other"],
                        "set": {"limit": "derived.other + 1"}}],
            "add": [{"id": "N", "version": "1", "reads": ["derived.limit"],
                     "set": {"other": "derived.limit"}}],
        }).apply(load_mapping(BASE))
    assert "cycle" in str(info.value)


def test_a_proposal_cannot_quietly_introduce_undeclared_coupling():
    with pytest.raises(RuleSetError) as info:
        Proposal.from_mapping({
            "proposal": "sneaky",
            "modify": [{"id": "CHECK", "reads": None}],
        }).apply(load_mapping(BASE))
    assert "declare" in str(info.value)


def test_example_proposal_applies_to_the_example_ruleset(ftl_ruleset):
    from canon import load_proposal

    proposal = load_proposal(ROOT / "examples" / "proposals"
                             / "2026-09-fatigue-package.yaml")
    candidate = proposal.apply(ftl_ruleset)
    assert len(candidate) == len(ftl_ruleset) + 1
    assert "CREW-006" in candidate.by_id
    assert candidate.by_id["FTL-040"].priority == 26
    assert candidate.by_id["FTL-010"].content_hash == \
        ftl_ruleset.by_id["FTL-010"].content_hash


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------


def _steeper():
    base = load_mapping(BASE)
    candidate = Proposal.from_mapping({
        "proposal": "steeper", "version": "2",
        "modify": [
            {"id": "D-SECTORS", "version": "2",
             "set": {"limit": "limits.base - 0.75 * (duty.sectors - 2)"}},
            {"id": "REST", "priority": 25},
        ],
    }).apply(base)
    return base, candidate


def test_compare_returns_the_flip_and_both_decisions():
    """Regression. ``compare`` returned a five element tuple after a bad edit,
    and nothing noticed until ``run`` tried to unpack it. It is a NamedTuple
    now, so the shape is checked where it is built rather than where it is
    consumed."""
    base, candidate = _steeper()
    replayed = WhatIf(base, candidate).compare(CORPUS[4])
    assert replayed.flip.case_id == "c5"
    assert replayed.before is not None
    assert replayed.after is not None
    assert len(replayed) == 3
    flip, before, after = replayed
    assert flip is replayed.flip


def test_identical_rulesets_move_nothing():
    base = load_mapping(BASE)
    report = WhatIf(base, load_mapping(BASE)).run(CORPUS)
    assert report.flip_rate == 0.0
    assert all(flip.kind == UNCHANGED for flip in report.results)


def test_a_tightening_blocks_cases_that_used_to_pass():
    base, candidate = _steeper()
    report = WhatIf(base, candidate).run(CORPUS)
    moved = {flip.case_id: flip for flip in report.flips}
    assert "c5" in moved
    assert moved["c5"].kind == NEWLY_BLOCKED
    assert moved["c5"].added == ["OVER_LIMIT"]
    assert "c1" not in moved
    assert "c4" not in moved


def test_a_relaxation_shows_as_newly_allowed():
    base = load_mapping(BASE)
    candidate = Proposal.from_mapping({
        "proposal": "relax",
        "modify": [{"id": "REST", "when": "crew.rest < 10"}],
    }).apply(base)
    report = WhatIf(base, candidate).run(CORPUS)
    moved = {flip.case_id: flip for flip in report.flips}
    assert moved["c4"].kind == NEWLY_ALLOWED
    assert moved["c4"].removed == ["REST_SHORT"]


def test_a_derived_value_moving_without_a_finding_is_reported():
    base, candidate = _steeper()
    report = WhatIf(base, candidate).run([case("only-derived", 8.0, 5, 14.0)])
    flip = report.results[0]
    assert flip.kind == DERIVED_CHANGED
    assert flip.derived_moved["limit"]["before"] == pytest.approx(11.5)
    assert flip.derived_moved["limit"]["after"] == pytest.approx(10.75)


def test_blame_reaches_through_the_derived_namespace():
    """CHECK did not change. D-SECTORS did, and CHECK reads what it writes."""
    base, candidate = _steeper()
    report = WhatIf(base, candidate).run(CORPUS)
    flip = next(f for f in report.flips if f.case_id == "c5")
    entry = next(e for e in flip.responsible if e["code"] == "OVER_LIMIT")
    assert entry["rule_id"] == "CHECK"
    assert entry["rule_changed"] is False
    assert entry["via"] == ["D-SECTORS"]


def test_blame_names_the_rule_directly_when_it_changed():
    base = load_mapping(BASE)
    candidate = Proposal.from_mapping({
        "proposal": "p", "modify": [{"id": "REST", "when": "crew.rest < 15"}],
    }).apply(base)
    report = WhatIf(base, candidate).run(CORPUS)
    flip = next(f for f in report.flips if f.case_id == "c1")
    entry = flip.responsible[0]
    assert entry["rule_id"] == "REST"
    assert entry["rule_changed"] is True
    assert entry["via"] == []


def test_a_change_that_moves_nothing_is_reported_as_inert():
    """The priority edit to REST changes its content hash and nothing else.
    That is the evidence a reviewer wants and cannot currently get."""
    base, candidate = _steeper()
    report = WhatIf(base, candidate).run(CORPUS)
    assert "REST" in report.changed_rule_ids()
    assert "REST" in report.inert_changes()
    assert "D-SECTORS" not in report.inert_changes()


def test_never_fired_finds_dead_rules():
    base, candidate = _steeper()
    report = WhatIf(base, candidate).run(CORPUS)
    assert "DORMANT" in report.never_fired()
    assert "CHECK" not in report.never_fired()
    assert report.coverage()["rules"] == 5


def test_report_counts_and_serialises():
    base, candidate = _steeper()
    report = WhatIf(base, candidate).run(CORPUS)
    payload = report.to_dict()
    assert payload["total"] == len(CORPUS)
    assert payload["flipped"] == len(report.flips)
    assert payload["unchanged"] + payload["flipped"] == payload["total"]
    assert "OVER_LIMIT" in payload["by_code"]
    assert isinstance(report.render(), str)


def test_by_code_separates_direction():
    base = load_mapping(BASE)
    candidate = Proposal.from_mapping({
        "proposal": "p", "modify": [{"id": "REST", "when": "crew.rest < 15"}],
    }).apply(base)
    report = WhatIf(base, candidate).run(CORPUS)
    assert report.by_code()["REST_SHORT"]["newly_raised"] > 0
    assert report.by_code()["REST_SHORT"]["no_longer_raised"] == 0
