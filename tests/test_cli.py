"""The command line interface.

These are mostly regression tests. The commands in the README and in the CI
workflow are the ones people actually type, so they are the ones worth pinning
down.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from canon.cli import build_parser, main

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "examples" / "rules" / "ftl.yaml"
TABLE = ROOT / "examples" / "rules" / "qualifications.csv"
PROPOSAL = ROOT / "examples" / "proposals" / "2026-09-fatigue-package.yaml"


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [
    ["validate", "rules.yaml", "--json"],
    ["plan", "rules.yaml", "--json"],
    ["diff", "before.json", "after.json", "--json"],
    ["whatif", "rules.yaml", "--json"],
    ["manifest", "rules.yaml", "--json"],
    ["ledger", "verify", "--json"],
])
def test_json_is_accepted_after_the_subcommand(argv):
    """Regression. --json used to live on the top level parser, and argparse
    hands everything after the subcommand name to the subparser, so
    `canon plan rules.yaml --json` exited 2 with 'unrecognized arguments'.
    That is exactly how it is written in the README and in CI."""
    args = build_parser().parse_args(argv)
    assert args.json is True


def test_positional_arguments_survive_the_shared_flag():
    args = build_parser().parse_args(["plan", "rules.yaml", "--client", "A"])
    assert args.rules == "rules.yaml"
    assert args.client == "A"
    assert args.json is False


# --------------------------------------------------------------------------
# End to end, on the example ruleset
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _needs_yaml():
    pytest.importorskip("yaml")


def test_every_example_ruleset_loads():
    """Regression. Two rules in ftl.yaml used a YAML folded block whose
    continuation lines were indented further than the first line. YAML keeps
    the line break in that case, so the expression arrived at the parser cut in
    half and the whole ruleset refused to load. Nothing but actually loading
    every example file catches that."""
    from canon import load_decision_table, load_yaml

    for path in sorted((ROOT / "examples").rglob("*")):
        if path.suffix.lower() in (".yaml", ".yml") and "proposals" not in path.parts:
            assert len(load_yaml(path)) > 0, path
        elif path.suffix.lower() == ".csv":
            assert len(load_decision_table(path)) > 0, path


def test_no_compiled_expression_was_cut_in_half():
    """A guard whose source contains a bare line break outside brackets is the
    signature of the folding bug, and it parses only by accident when the break
    happens to fall inside a call."""
    from canon import load_yaml

    ruleset = load_yaml(RULES)
    for rule in ruleset.rules:
        for expression in rule.expressions():
            depth = 0
            for character in expression.source:
                if character in "([{":
                    depth += 1
                elif character in ")]}":
                    depth -= 1
                elif character == "\n":
                    assert depth > 0, (
                        f"{rule.id}: line break outside brackets in "
                        f"{expression.source!r}")


def test_validate_runs(capsys):
    assert main(["validate", str(RULES)]) == 0
    assert "crew_rostering" in capsys.readouterr().out


def test_plan_json_is_parseable(capsys):
    """This is the exact command the CI contract job runs."""
    assert main(["plan", str(RULES), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ruleset"] == "crew_rostering"
    assert payload["paths"], "the contract cannot be empty"
    # Leaf count is at most the number of paths: a collection contributes a
    # path of its own but no leaf, because its leaves are its element fields.
    assert 0 < payload["field_count"] <= len(payload["paths"])
    assert any("[*]" in path for path in payload["paths"])


def test_plan_narrows_for_a_client(capsys):
    main(["plan", str(RULES), "--client", "AIRLINE_A", "--json"])
    for_a = json.loads(capsys.readouterr().out)
    main(["plan", str(RULES), "--client", "AIRLINE_B", "--json"])
    for_b = json.loads(capsys.readouterr().out)
    assert for_b["field_count"] > for_a["field_count"]


def test_decision_table_loads_through_the_cli(capsys):
    assert main(["validate", str(TABLE)]) == 0


def test_manifest_writes_a_file(tmp_path, capsys):
    out = tmp_path / "manifest.json"
    assert main(["manifest", str(RULES), "--out", str(out)]) == 0
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["ruleset_id"] == "crew_rostering"
    assert len(manifest["merkle_root"]) == 64


def test_whatif_without_cases_reports_the_diff_and_says_so(capsys):
    assert main(["whatif", str(RULES), "--proposal", str(PROPOSAL)]) == 0
    out = capsys.readouterr().out
    assert "CREW-006" in out
    assert "unknown" in out


def test_run_returns_two_when_the_assignment_is_illegal(tmp_path, capsys):
    facts = ROOT / "examples" / "data" / "fdp_breach.json"
    assert main(["run", str(RULES), "--facts", str(facts),
                 "--client", "AIRLINE_A", "--as-of", "2026-08-14"]) == 2
    assert "FTL_FDP_EXCEEDED" in capsys.readouterr().out


def test_explain_names_the_rule(capsys):
    facts = ROOT / "examples" / "data" / "fdp_breach.json"
    assert main(["explain", str(RULES), "--facts", str(facts),
                 "--code", "FTL_FDP_EXCEEDED", "--as-of", "2026-08-14"]) == 0
    out = capsys.readouterr().out
    assert "FTL-010" in out
    assert "FTL-002" in out  # the upstream rule that moved the limit
