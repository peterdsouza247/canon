"""Manifests, the deploy ledger, blame, tamper evidence and receipts."""

from __future__ import annotations

import json

import pytest

from canon import (DeployLedger, Engine, Manifest, diff_manifests,
                   issue_receipt, load_mapping, merkle_root, verify_receipt)
from canon.errors import TamperError

SECRET = b"canon-test-key"


def _spec(threshold: int, version: str = "1") -> dict:
    return {
        "ruleset": "demo",
        "version": version,
        "rules": [
            {"id": "R1", "version": "1", "when": "crew.hours > 10",
             "emit": {"code": "OVER"}},
            {"id": "R2", "version": version, "when": f"crew.hours > {threshold}",
             "emit": {"code": "WAY_OVER"}},
        ],
    }


# --------------------------------------------------------------------------
# Hashing
# --------------------------------------------------------------------------


def test_merkle_root_is_order_independent():
    hashes = ["aa", "bb", "cc", "dd", "ee"]
    assert merkle_root(hashes) == merkle_root(list(reversed(hashes)))


def test_merkle_root_changes_when_a_leaf_changes():
    assert merkle_root(["aa", "bb"]) != merkle_root(["aa", "bc"])


def test_cosmetic_edits_do_not_change_a_rule_hash():
    plain = load_mapping(_spec(20))
    documented = load_mapping({
        "ruleset": "demo", "version": "1",
        "rules": [
            dict(_spec(20)["rules"][0], title="A title",
                 description="A long description", owner="someone@example.com",
                 tags=["a", "b"]),
            _spec(20)["rules"][1],
        ],
    })
    assert plain.by_id["R1"].content_hash == documented.by_id["R1"].content_hash


def test_changing_a_threshold_changes_the_hash():
    assert (load_mapping(_spec(20)).by_id["R2"].content_hash
            != load_mapping(_spec(21)).by_id["R2"].content_hash)


# --------------------------------------------------------------------------
# Manifests
# --------------------------------------------------------------------------


def test_manifest_round_trips(tmp_path):
    manifest = Manifest.of(load_mapping(_spec(20)))
    path = tmp_path / "manifest.json"
    manifest.save(path)
    reloaded = Manifest.load(path)
    assert reloaded.root == manifest.root
    assert len(reloaded.entries) == 2


def test_edited_manifest_is_detected(tmp_path):
    manifest = Manifest.of(load_mapping(_spec(20)))
    path = tmp_path / "manifest.json"
    manifest.save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["entries"][0]["hash"] = "0" * 64
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(TamperError):
        Manifest.load(path)


def test_diff_flags_a_content_change_without_a_version_bump():
    before = Manifest.of(load_mapping(_spec(20, version="1")))
    after = Manifest.of(load_mapping(_spec(25, version="1")))
    result = diff_manifests(before, after)
    assert len(result["changed"]) == 1
    assert result["changed"][0]["rule_id"] == "R2"
    assert result["silent_changes"], "a content change with no version bump " \
                                     "is exactly what a reviewer needs told"


# --------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------


def _ledger() -> DeployLedger:
    ledger = DeployLedger(secret=SECRET)
    ledger.append(Manifest.of(load_mapping(_spec(20, version="1")),
                              created_at="2026-05-01T09:00:00+00:00"),
                  environment="prod", deployed_by="alice",
                  deployed_at="2026-05-01T09:05:00+00:00")
    ledger.append(Manifest.of(load_mapping(_spec(20, version="2")),
                              created_at="2026-06-01T09:00:00+00:00"),
                  environment="prod", deployed_by="bob",
                  deployed_at="2026-06-01T09:05:00+00:00")
    ledger.append(Manifest.of(load_mapping(_spec(25, version="3")),
                              created_at="2026-07-01T09:00:00+00:00"),
                  environment="prod", deployed_by="carol",
                  deployed_at="2026-07-01T09:05:00+00:00")
    return ledger


def test_a_fresh_ledger_verifies():
    assert _ledger().verify() == []


def test_blame_finds_the_deployment_that_changed_a_rule():
    changes = _ledger().blame("R2")
    assert [entry["seq"] for entry in changes] == [1, 2, 3]
    assert changes[0]["change"] == "added"
    assert changes[-1]["deployed_by"] == "carol"


def test_blame_ignores_deployments_that_left_the_rule_alone():
    changes = _ledger().blame("R1")
    assert len(changes) == 1
    assert changes[0]["seq"] == 1


def test_reordering_the_ledger_is_detected():
    ledger = _ledger()
    ledger.records[1], ledger.records[2] = ledger.records[2], ledger.records[1]
    problems = ledger.verify()
    assert problems
    assert any("prev_hash" in problem or "sequence" in problem
               for problem in problems)


def test_editing_a_past_entry_is_detected():
    ledger = _ledger()
    ledger.records[0].deployed_by = "not-alice"
    problems = ledger.verify()
    assert any("edited after the fact" in problem for problem in problems)


def test_deleting_an_entry_is_detected():
    ledger = _ledger()
    del ledger.records[1]
    assert ledger.verify()


def test_forging_an_entry_without_the_key_is_detected():
    """Rewriting history and recomputing the hashes still fails, because the
    signature is over the entry hash and the forger does not hold the key."""
    honest = _ledger()
    forged = DeployLedger(secret=b"the-wrong-key")
    for record in honest.records[:2]:
        forged.append(record.manifest, environment=record.environment,
                      deployed_by=record.deployed_by,
                      deployed_at=record.deployed_at)
    checked = DeployLedger(forged.records, secret=SECRET)
    assert any("signature" in problem for problem in checked.verify())


def test_ledger_persists(tmp_path):
    path = tmp_path / "ledger.json"
    _ledger().save(path)
    reloaded = DeployLedger.load(path, secret=SECRET)
    assert reloaded.verify() == []
    assert len(reloaded.records) == 3


def test_live_at_answers_what_was_deployed_on_a_date():
    manifest = _ledger().live_at("2026-06-15T00:00:00+00:00", "prod")
    assert manifest is not None
    assert manifest.ruleset_version == "2"


# --------------------------------------------------------------------------
# Receipts
# --------------------------------------------------------------------------


def test_receipt_verifies_against_a_replay():
    ruleset = load_mapping(_spec(20))
    manifest = Manifest.of(ruleset)
    engine = Engine(ruleset)
    facts = {"crew": {"hours": 30}}
    decision = engine.evaluate(facts, key={"crew_id": "C1"})
    receipt = issue_receipt(decision, manifest, SECRET)

    replayed = engine.evaluate(facts, key={"crew_id": "C1"})
    assert verify_receipt(receipt, decision=replayed, manifest=manifest,
                          secret=SECRET) == []


def test_receipt_fails_when_the_ruleset_moved_on():
    ruleset = load_mapping(_spec(20))
    manifest = Manifest.of(ruleset)
    decision = Engine(ruleset).evaluate({"crew": {"hours": 22}})
    receipt = issue_receipt(decision, manifest, SECRET)

    newer = Manifest.of(load_mapping(_spec(25)))
    problems = verify_receipt(receipt, manifest=newer, secret=SECRET)
    assert any("manifest root" in problem for problem in problems)


def test_receipt_fails_when_the_outcome_changed():
    old_rules = load_mapping(_spec(20))
    new_rules = load_mapping(_spec(25))
    manifest = Manifest.of(old_rules)
    facts = {"crew": {"hours": 22}}
    decision = Engine(old_rules).evaluate(facts)
    receipt = issue_receipt(decision, manifest, SECRET)

    replayed = Engine(new_rules).evaluate(facts)
    problems = verify_receipt(receipt, decision=replayed, secret=SECRET)
    assert any("different result" in problem for problem in problems)


def test_tampering_with_a_receipt_body_is_detected():
    ruleset = load_mapping(_spec(20))
    manifest = Manifest.of(ruleset)
    decision = Engine(ruleset).evaluate({"crew": {"hours": 30}})
    receipt = dict(issue_receipt(decision, manifest, SECRET))
    receipt["verdict"] = "pass"
    problems = verify_receipt(receipt, secret=SECRET)
    assert problems
