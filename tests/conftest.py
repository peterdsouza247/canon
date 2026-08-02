from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

EXAMPLES = ROOT / "examples"


@pytest.fixture(scope="session")
def scenarios() -> dict:
    raw = json.loads((EXAMPLES / "data" / "scenarios.json").read_text(encoding="utf-8"))
    return {key: value for key, value in raw.items() if not key.startswith("_")}


@pytest.fixture(scope="session")
def ftl_ruleset():
    yaml = pytest.importorskip("yaml")  # noqa: F841
    from canon import load_yaml

    return load_yaml(EXAMPLES / "rules" / "ftl.yaml")


@pytest.fixture(scope="session")
def quals_ruleset():
    from canon import load_decision_table

    return load_decision_table(EXAMPLES / "rules" / "qualifications.csv")


@pytest.fixture
def engine(ftl_ruleset):
    from canon import Engine

    return Engine(ftl_ruleset)
