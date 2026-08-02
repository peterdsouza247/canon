"""Canon: a stateless, self explaining rules engine for crew rostering.

Canon exists to answer seven questions that long lived business rules
deployments tend to answer badly:

1. What data does this decision actually need?      -> ``Projection``
2. How do we stop sending more than that?           -> generated, not hand held
3. How do we ask for crew *on a flight*?            -> collection fact paths
4. How do we stop rules depending on each other?    -> declared reads, strata
5. How do we stop paying for the engine?            -> this is a Python library
6. Which rule caused this?                          -> ``Decision.explain``
7. Which deployment broke it?                       -> ``DeployLedger.blame``

The public surface is small on purpose. Load a ruleset, build an engine,
evaluate. Everything else is inspection of what just happened.

    from canon import load_yaml, Engine

    ruleset = load_yaml("rules/ftl.yaml")
    engine = Engine(ruleset)
    decision = engine.evaluate(facts, client="AIRLINE_A")
    print(decision.render(verbose=True))
"""

from __future__ import annotations

__version__ = "0.1.0"

from .dsl import RuleSetBuilder, compile_python_rule
from .engine import Engine, evaluate
from .errors import (CanonError, ExpressionError, MigrationError,
                     RuleDefinitionError, RuleSetError, TamperError,
                     UndeclaredDependencyError, UnplannedFactError,
                     UnsafeExpressionError)
from .expr import Expression, compile_expression
from .facts import FactRequest, FactStore, Projection, dict_source
from .loaders import (load_decision_table, load_decision_table_text,
                      load_directory, load_mapping, load_yaml, load_yaml_text)
from .proposal import Proposal, apply_proposal, load_proposal, load_proposal_text
from .registry import (DeployLedger, DeployRecord, Manifest, RuleEntry,
                       diff_manifests, issue_receipt, merkle_root,
                       verify_receipt)
from .rules import Emission, Finding, Rule, RuleSet
from .session import Delta, Session, apply_changes, diff_facts
from .shadow import ShadowCase, ShadowReport, ShadowRunner, load_cases_jsonl
from .trace import Decision, RuleTrace
from .whatif import Flip, ImpactReport, WhatIf, replay

__all__ = [
    "__version__",
    # authoring
    "load_yaml", "load_yaml_text", "load_mapping", "load_directory",
    "load_decision_table", "load_decision_table_text",
    "RuleSetBuilder", "compile_python_rule",
    # model
    "Rule", "RuleSet", "Emission", "Finding", "Expression", "compile_expression",
    # execution
    "Engine", "evaluate", "Decision", "RuleTrace",
    "Projection", "FactStore", "FactRequest", "dict_source",
    # interactive editing
    "Session", "Delta", "apply_changes", "diff_facts",
    # governance
    "Manifest", "RuleEntry", "DeployLedger", "DeployRecord", "merkle_root",
    "diff_manifests", "issue_receipt", "verify_receipt",
    # migration and change management
    "ShadowRunner", "ShadowCase", "ShadowReport", "load_cases_jsonl",
    "Proposal", "load_proposal", "load_proposal_text", "apply_proposal",
    "WhatIf", "ImpactReport", "Flip", "replay",
    # errors
    "CanonError", "ExpressionError", "UnsafeExpressionError",
    "RuleDefinitionError", "RuleSetError", "UndeclaredDependencyError",
    "UnplannedFactError", "TamperError", "MigrationError",
]
