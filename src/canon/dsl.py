"""The Python authoring front end.

Rules written here look like ordinary Python functions, but they are never
called. The decorator reads the function's source, parses it, and compiles it
into the same ``Rule`` objects a YAML file produces. Nothing from the enclosing
module leaks into evaluation, so a Python authored rule is exactly as isolated,
as analysable and as hashable as a YAML authored one.

The cost of the ergonomics is a narrow accepted shape. A rule body is a
docstring, then either one ``if`` statement or a sequence of ``emit`` and
``set_`` calls. No loops, no local variables, no early returns, no ``else``.
Anything outside that shape is refused with a message that says what to do
instead, usually "write two rules". That refusal is the feature: it is what
keeps a rule small enough to reason about and cheap enough to hash.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Any, Callable, Iterable, Mapping

from .errors import RuleDefinitionError
from .expr import compile_expression
from .rules import Emission, Rule, RuleSet, _as_date

__all__ = ["RuleSetBuilder", "compile_python_rule", "emit", "set_", "facts"]


def emit(code: str, **kwargs: Any) -> None:
    """Marker for the compiler. Importable so that editors and linters are
    happy; calling it means a rule function was executed, which never happens."""
    raise RuntimeError(
        "emit() is compiled, not executed. A Canon rule function is never "
        "called at run time.")


def set_(*args: Any, **kwargs: Any) -> None:
    """Marker for the compiler. See :func:`emit`."""
    raise RuntimeError(
        "set_() is compiled, not executed. A Canon rule function is never "
        "called at run time.")


class _FactsStub:
    """Typing convenience for rule authors. Never used at run time."""

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - stub
        return self


facts = _FactsStub()


class _StripFactArg(ast.NodeTransformer):
    """Rewrite ``f.crew.base`` into ``crew.base``.

    The accessor argument exists so that editors can autocomplete and so that
    the function reads like Python. It has no meaning in the compiled rule, and
    removing it here means the stored expression is identical to the one a YAML
    author would have written, which keeps content hashes comparable across
    front ends.
    """

    def __init__(self, arg_name: str) -> None:
        self.arg_name = arg_name

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.value, ast.Name) and node.value.id == self.arg_name:
            return ast.copy_location(ast.Name(id=node.attr, ctx=ast.Load()), node)
        return node


def _unparse(node: ast.AST) -> str:
    return ast.unparse(node).strip()


def _literal(node: ast.AST, what: str, rule_id: str) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    raise RuleDefinitionError(
        f"rule {rule_id!r}: {what} must be a literal, not an expression. "
        f"Detail values may be expressions; the code, severity and message "
        f"cannot, because they are what a support engineer greps for.")


def _statements(body: list[ast.stmt]) -> list[ast.stmt]:
    if body and isinstance(body[0], ast.Expr) and \
            isinstance(body[0].value, ast.Constant) and \
            isinstance(body[0].value.value, str):
        return body[1:]
    return body


def compile_python_rule(fn: Callable[..., Any], *, id: str,
                        version: str = "1",
                        title: str = "",
                        clients: Iterable[str] = ("*",),
                        reads: Iterable[str] | None = None,
                        effective_from: Any = None,
                        effective_to: Any = None,
                        priority: int = 100,
                        tags: Iterable[str] = (),
                        owner: str = "") -> Rule:
    try:
        source = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError) as exc:  # pragma: no cover - exotic environments
        raise RuleDefinitionError(
            f"rule {id!r}: could not read the source of {fn!r}. Python authored "
            f"rules must live in a real file, not in an interactive session."
        ) from exc

    module = ast.parse(source)
    fndef = module.body[0]
    if not isinstance(fndef, ast.FunctionDef):
        raise RuleDefinitionError(f"rule {id!r}: expected a plain function")
    fndef.decorator_list = []

    args = fndef.args
    if len(args.args) != 1 or args.vararg or args.kwarg or args.kwonlyargs:
        raise RuleDefinitionError(
            f"rule {id!r}: a rule function takes exactly one argument, the fact "
            f"accessor, conventionally named 'f'")
    arg_name = args.args[0].arg

    fndef = _StripFactArg(arg_name).visit(fndef)
    ast.fix_missing_locations(fndef)

    docstring = ast.get_docstring(fndef) or ""
    body = _statements(fndef.body)
    if not body:
        raise RuleDefinitionError(f"rule {id!r}: the function body is empty")

    when_src: str | None = None
    actions: list[ast.stmt] = body

    if len(body) == 1 and isinstance(body[0], ast.If):
        branch = body[0]
        if branch.orelse:
            raise RuleDefinitionError(
                f"rule {id!r}: 'else' and 'elif' are not supported. Write one "
                f"rule per outcome. Two small rules can be versioned, hashed, "
                f"traced and switched off independently; one branching rule "
                f"cannot.")
        when_src = _unparse(branch.test)
        actions = _statements(branch.body)
    elif any(isinstance(stmt, ast.If) for stmt in body):
        raise RuleDefinitionError(
            f"rule {id!r}: an 'if' must be the whole body, not one statement "
            f"among several")

    emission: Emission | None = None
    sets: dict[str, Any] = {}

    for stmt in actions:
        if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
            raise RuleDefinitionError(
                f"rule {id!r}: only 'emit(...)' and 'set_(...)' calls are "
                f"allowed in a rule body, found {type(stmt).__name__}")
        call = stmt.value
        if not isinstance(call.func, ast.Name):
            raise RuleDefinitionError(
                f"rule {id!r}: only 'emit' and 'set_' may be called directly")
        name = call.func.id
        if name == "emit":
            if emission is not None:
                raise RuleDefinitionError(
                    f"rule {id!r}: a rule emits at most one finding. Two "
                    f"findings mean two rules.")
            emission = _build_emission(call, id)
        elif name == "set_":
            sets.update(_build_sets(call, id))
        else:
            raise RuleDefinitionError(
                f"rule {id!r}: unknown action {name!r}; expected 'emit' or 'set_'")

    compiled_when = compile_expression(when_src, id) if when_src else None
    compiled_sets = {key: compile_expression(value, id)
                     for key, value in sets.items()}

    rule = Rule(
        id=id,
        version=version,
        title=title or (docstring.splitlines()[0] if docstring else ""),
        description=docstring,
        when=compiled_when,
        emit=emission,
        sets=compiled_sets,
        reads=tuple(reads) if reads is not None else (),
        clients=tuple(clients),
        effective_from=_as_date(effective_from, f"{id}.effective_from"),
        effective_to=_as_date(effective_to, f"{id}.effective_to"),
        priority=priority,
        tags=tuple(tags),
        owner=owner,
        authoring="python",
        source_ref=_source_ref(fn),
    )
    return rule


def _build_emission(call: ast.Call, rule_id: str) -> Emission:
    if not call.args:
        raise RuleDefinitionError(
            f"rule {rule_id!r}: emit() needs a finding code as its first argument")
    code = _literal(call.args[0], "the finding code", rule_id)
    severity = "hard"
    message = ""
    detail: dict[str, Any] = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            raise RuleDefinitionError(
                f"rule {rule_id!r}: '**kwargs' is not permitted in emit()")
        if keyword.arg == "severity":
            severity = _literal(keyword.value, "severity", rule_id)
        elif keyword.arg == "message":
            message = _literal(keyword.value, "message", rule_id)
        else:
            detail[keyword.arg] = _unparse(keyword.value)
    return Emission(
        code=str(code),
        severity=str(severity),
        message=str(message),
        detail={key: compile_expression(value, rule_id)
                for key, value in detail.items()},
    )


def _build_sets(call: ast.Call, rule_id: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if call.args:
        if len(call.args) != 2:
            raise RuleDefinitionError(
                f"rule {rule_id!r}: set_() takes a name and a value, or keyword "
                f"arguments")
        name = _literal(call.args[0], "the derived fact name", rule_id)
        out[str(name)] = _unparse(call.args[1])
    for keyword in call.keywords:
        if keyword.arg is None:
            raise RuleDefinitionError(
                f"rule {rule_id!r}: '**kwargs' is not permitted in set_()")
        out[keyword.arg] = _unparse(keyword.value)
    if not out:
        raise RuleDefinitionError(f"rule {rule_id!r}: set_() set nothing")
    return out


def _source_ref(fn: Callable[..., Any]) -> str:
    try:
        path = inspect.getsourcefile(fn) or "<unknown>"
        line = fn.__code__.co_firstlineno
        return f"{path}:{line}"
    except Exception:  # pragma: no cover - defensive
        return getattr(fn, "__qualname__", "<unknown>")


# --------------------------------------------------------------------------
# Builder
# --------------------------------------------------------------------------


class RuleSetBuilder:
    """Collects Python authored rules into a ruleset.

    ``auto_reads`` is the one behavioural difference between this front end and
    YAML. Because the compiler already has the syntax tree, it can work out
    which derived facts a rule consumes and fill in the declaration. That is
    convenient, and it is also a small loss: the coupling is no longer visible
    in the source a reviewer reads. Teams that care more about the review than
    the convenience should pass ``auto_reads=False``.
    """

    def __init__(self, id: str, *, version: str = "1", description: str = "",
                 auto_reads: bool = True) -> None:
        self.id = id
        self.version = version
        self.description = description
        self.auto_reads = auto_reads
        self._rules: list[Rule] = []
        self._policy: dict[str, str] = {}

    def combine(self, derived_name: str, policy: str) -> "RuleSetBuilder":
        self._policy[derived_name] = policy
        return self

    def rule(self, id: str, **meta: Any) -> Callable[[Callable[..., Any]],
                                                     Callable[..., Any]]:
        def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._rules.append(compile_python_rule(fn, id=id, **meta))
            return fn

        return decorate

    def add(self, rule: Rule) -> "RuleSetBuilder":
        self._rules.append(rule)
        return self

    def build(self) -> RuleSet:
        rules = self._rules
        if self.auto_reads:
            roots = _roots_of(rules)
            rules = [
                rule if rule.reads else _with_reads(rule, roots)
                for rule in rules
            ]
        return RuleSet(
            id=self.id,
            rules=rules,
            version=self.version,
            derived_policy=self._policy,
            description=self.description,
        )


def _roots_of(rules: Iterable[Rule]) -> list[str]:
    from .rules import _infer_roots

    return sorted(_infer_roots(list(rules)))


def _with_reads(rule: Rule, roots: list[str]) -> Rule:
    from dataclasses import replace

    discovered = tuple(rule.derived_reads(roots))
    if not discovered:
        return rule
    return replace(rule, reads=discovered)
