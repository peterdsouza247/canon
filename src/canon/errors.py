"""Canon error hierarchy.

Every error carries enough context to point an engineer at a specific rule, a
specific expression, and a specific fact path. Debuggability is a design goal
rather than an afterthought, so Canon never raises a bare ValueError.
"""

from __future__ import annotations


class CanonError(Exception):
    """Base class for everything Canon raises."""


class ExpressionError(CanonError):
    """Raised when an expression cannot be compiled or evaluated."""

    def __init__(self, message: str, *, source: str | None = None,
                 rule_id: str | None = None, path: str | None = None):
        self.message = message
        self.source = source
        self.rule_id = rule_id
        self.path = path
        parts = [message]
        if rule_id:
            parts.append(f"rule={rule_id}")
        if source:
            parts.append(f"expression={source!r}")
        if path:
            parts.append(f"fact_path={path}")
        super().__init__("  ".join(parts))


class UnsafeExpressionError(ExpressionError):
    """The expression used syntax that is deliberately not supported.

    Canon's expression language is a restricted subset of Python. Anything that
    could reach the host process (imports, lambdas, walrus assignment, f-string
    evaluation, dunder attribute access) is rejected at compile time rather than
    discovered at run time.
    """


class RuleDefinitionError(CanonError):
    """A rule is structurally invalid, for example it neither emits nor sets."""


class RuleSetError(CanonError):
    """The ruleset as a whole is invalid: a dependency cycle, an undeclared read
    of a derived fact, or two rules writing the same derived fact with no
    declared combine policy."""


class UndeclaredDependencyError(RuleSetError):
    """A rule read a derived fact it did not declare in ``reads``.

    This error is what makes rule isolation real. Without it, rules quietly grow
    dependencies on one another and evaluation order becomes load bearing.
    """


class UnplannedFactError(CanonError):
    """A rule read a fact path that static analysis did not predict.

    In strict mode this is fatal, because an unplanned read means the payload
    projection handed to the calling application was incomplete. In permissive
    mode it is recorded on the trace as a planning miss.
    """


class FactResolutionError(CanonError):
    """A fact source failed, or returned something unusable."""


class TamperError(CanonError):
    """A manifest, ledger entry, or decision receipt failed verification."""


class MigrationError(CanonError):
    """The ODM importer met a construct it will not silently guess at."""
