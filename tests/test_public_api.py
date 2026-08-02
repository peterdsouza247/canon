"""The public surface is a promise, so it gets a test.

This exists because of a specific failure. ``tests/test_session.py`` imported
``Session`` from the ``canon`` package, the re-export was missing from
``__init__.py``, and pytest aborted the entire run at collection with a single
ImportError. One missing line hid the state of every other test.

A missing export should fail one small, clearly named test instead.
"""

from __future__ import annotations

import importlib

import pytest

import canon

# The entry points the README, the docs and the CLI all assume are importable
# straight from the package.
DOCUMENTED = [
    "Engine", "evaluate", "Decision", "RuleTrace",
    "Rule", "RuleSet", "Emission", "Finding",
    "Expression", "compile_expression",
    "Projection", "FactStore", "FactRequest", "dict_source",
    "Session", "Delta", "apply_changes", "diff_facts",
    "Manifest", "DeployLedger", "merkle_root", "diff_manifests",
    "issue_receipt", "verify_receipt",
    "ShadowRunner", "ShadowCase", "ShadowReport", "load_cases_jsonl",
    "Proposal", "load_proposal", "apply_proposal",
    "WhatIf", "ImpactReport", "Flip", "replay",
    "load_yaml", "load_mapping", "load_directory", "load_decision_table",
    "RuleSetBuilder", "compile_python_rule",
    "CanonError", "ExpressionError", "RuleSetError",
]

MODULES = [
    "canon.cli", "canon.dsl", "canon.engine", "canon.errors", "canon.expr",
    "canon.facts", "canon.loaders", "canon.odm_import", "canon.proposal",
    "canon.registry", "canon.rules", "canon.session", "canon.shadow",
    "canon.trace", "canon.whatif",
]


def test_everything_in_all_actually_exists():
    missing = [name for name in canon.__all__ if not hasattr(canon, name)]
    assert missing == [], f"canon.__all__ promises names it does not have: {missing}"


def test_the_documented_entry_points_are_exported():
    missing = [name for name in DOCUMENTED if not hasattr(canon, name)]
    assert missing == [], (
        f"canon/__init__.py is missing re-exports: {missing}. Any test or "
        f"caller importing one of these from the package will fail, and if it "
        f"is a test it will fail at collection time and take the run with it."
    )


def test_documented_entry_points_are_also_declared():
    """``__all__`` is what ``from canon import *`` gives and what tooling
    reads. An export that works by accident is not an export."""
    undeclared = [name for name in DOCUMENTED if name not in canon.__all__]
    assert undeclared == [], f"exported but not declared in __all__: {undeclared}"


@pytest.mark.parametrize("module_name", MODULES)
def test_each_module_delivers_what_it_declares(module_name):
    module = importlib.import_module(module_name)
    missing = [name for name in getattr(module, "__all__", ())
               if not hasattr(module, name)]
    assert missing == [], f"{module_name}.__all__ promises {missing}"


def test_every_module_imports_cleanly():
    """Catches a circular import or a syntax error in a module nothing else
    happens to touch."""
    for module_name in MODULES:
        importlib.import_module(module_name)
