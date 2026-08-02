"""Canon's restricted expression language.

The whole of Canon rests on one idea: *the same code path that evaluates an
expression also tells you what data that expression needs.*

We parse expressions with Python's own ``ast`` module, reject everything that
is not on an explicit allow list, and then walk the tree with a small
interpreter. The interpreter never touches facts directly. It asks a
``Resolver`` for them. Swap in a ``StaticResolver`` and the identical walk
produces the set of fact paths the expression would read, without any data
being present.

That is what solves the payload problem. The projection sent over the wire is
derived from the rules themselves, so it cannot drift away from what the rules
actually use, and nobody has to maintain a hand written list of fields.

Collection access uses ordinary Python comprehension syntax::

    any(m.rank == 'CP' for m in flight.roster)

which statically yields the path ``flight.roster[*].rank``. That is the
vertical slice, expressed as data the engine understands rather than as a
special case bolted onto the side.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Iterable as _AbcIterable
from collections.abc import Mapping as _AbcMapping
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .errors import ExpressionError, UnsafeExpressionError

__all__ = [
    "Expression",
    "Resolver",
    "StaticResolver",
    "Read",
    "UNKNOWN",
    "FUNCTIONS",
    "compile_expression",
]


# --------------------------------------------------------------------------
# The unknown sentinel used during static analysis
# --------------------------------------------------------------------------


class _Unknown:
    """Stands in for any value during static analysis.

    Every operator in the interpreter returns ``UNKNOWN`` when one of its
    operands is ``UNKNOWN``, so a static walk never raises on type mismatches
    and always reaches every branch.
    """

    _instance: "_Unknown | None" = None

    def __new__(cls) -> "_Unknown":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNKNOWN"

    def __bool__(self) -> bool:  # pragma: no cover - defensive
        return False

    def __iter__(self) -> Iterator[Any]:
        return iter(())


UNKNOWN = _Unknown()


def _unknown(*values: Any) -> bool:
    return any(v is UNKNOWN for v in values)


# --------------------------------------------------------------------------
# Lazy handles
# --------------------------------------------------------------------------


class Ref:
    """A not yet resolved reference to a fact path rooted at a namespace.

    ``crew`` is a Ref. ``crew.base`` is a Ref. Nothing is fetched until the
    reference is used in a value position, which is what keeps the engine from
    pulling data that a short circuiting guard made unnecessary.
    """

    __slots__ = ("path",)

    def __init__(self, path: str) -> None:
        self.path = path

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Ref({self.path!r})"


class Elem:
    """A value already in hand, carrying the fact path it came from.

    Produced by iterating a collection. The value is present, but we still
    defer *recording* the read until the value is consumed, so the trace
    reflects what a rule genuinely looked at.
    """

    __slots__ = ("obj", "path")

    def __init__(self, obj: Any, path: str) -> None:
        self.obj = obj
        self.path = path

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Elem({self.path!r})"


class Read:
    """One recorded access to a fact path."""

    __slots__ = ("path", "kind", "value")

    def __init__(self, path: str, kind: str, value: Any) -> None:
        self.path = path
        self.kind = kind  # "scalar" or "collection"
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Read({self.path!r}, {self.kind})"


# --------------------------------------------------------------------------
# Resolvers
# --------------------------------------------------------------------------


class Resolver:
    """Base resolver. Subclasses supply the data; this class records reads."""

    def __init__(self, roots: Iterable[str]) -> None:
        self.roots = frozenset(roots)
        self.reads: dict[str, Read] = {}

    # -- fetching ---------------------------------------------------------

    def fetch(self, path: str) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def fetch_collection(self, path: str) -> Sequence[Any]:  # pragma: no cover
        raise NotImplementedError

    # -- recording --------------------------------------------------------

    def record(self, path: str, kind: str, value: Any) -> None:
        existing = self.reads.get(path)
        if existing is None:
            self.reads[path] = Read(path, kind, value)
        elif existing.kind == "scalar" and kind == "collection":
            existing.kind = "collection"

    def read_paths(self) -> list[str]:
        return sorted(self.reads)


class StaticResolver(Resolver):
    """Resolver used for planning. Returns UNKNOWN for everything.

    Because it hands back a single symbolic element for every collection, one
    static walk visits the body of a comprehension exactly once and learns the
    element fields the rule needs.
    """

    static = True

    def fetch(self, path: str) -> Any:
        return UNKNOWN

    def fetch_collection(self, path: str) -> Sequence[Any]:
        return (UNKNOWN,)


class MappingResolver(Resolver):
    """Simple resolver over a nested dict. Used in tests and in the CLI."""

    static = False

    def __init__(self, data: Mapping[str, Any]) -> None:
        super().__init__(data.keys())
        self.data = data

    def fetch(self, path: str) -> Any:
        cur: Any = self.data
        for seg in path.split("."):
            cur = _raw_get(cur, seg)
            if cur is None:
                return None
        return cur

    def fetch_collection(self, path: str) -> Sequence[Any]:
        value = self.fetch(path)
        if value is None:
            return ()
        if isinstance(value, (str, bytes)) or not isinstance(value, _AbcIterable):
            raise ExpressionError(
                f"fact path {path!r} was iterated but is not a collection",
                path=path,
            )
        return list(value)


def _raw_get(obj: Any, name: str) -> Any:
    """Read one field from a fact object.

    Facts arrive as dicts from JSON payloads, or as objects from an adapter.
    Canon treats both the same so that a caller is never forced to reshape
    their domain model just to talk to the rules engine.
    """
    if obj is UNKNOWN or obj is None:
        return UNKNOWN if obj is UNKNOWN else None
    if isinstance(obj, _AbcMapping):
        return obj.get(name)
    return getattr(obj, name, None)


# --------------------------------------------------------------------------
# Built in functions
# --------------------------------------------------------------------------


def _to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError as exc:
            raise ExpressionError(f"not an ISO 8601 timestamp: {value!r}") from exc
    raise ExpressionError(f"cannot interpret {value!r} as a timestamp")


def _naive(dt: datetime) -> datetime:
    """Drop the tzinfo after normalising, so mixed inputs still subtract."""
    if dt.tzinfo is not None:
        return dt.astimezone(tz=None).replace(tzinfo=None)
    return dt


def hours_between(start: Any, end: Any) -> Any:
    if _unknown(start, end):
        return UNKNOWN
    delta = _naive(_to_datetime(end)) - _naive(_to_datetime(start))
    return delta.total_seconds() / 3600.0


def days_between(start: Any, end: Any) -> Any:
    if _unknown(start, end):
        return UNKNOWN
    return hours_between(start, end) / 24.0


def minutes_between(start: Any, end: Any) -> Any:
    if _unknown(start, end):
        return UNKNOWN
    return hours_between(start, end) * 60.0


def overlaps(a_start: Any, a_end: Any, b_start: Any, b_end: Any) -> Any:
    if _unknown(a_start, a_end, b_start, b_end):
        return UNKNOWN
    return (_naive(_to_datetime(a_start)) < _naive(_to_datetime(b_end))
            and _naive(_to_datetime(b_start)) < _naive(_to_datetime(a_end)))


def add_hours(moment: Any, hours: Any) -> Any:
    if _unknown(moment, hours):
        return UNKNOWN
    return _to_datetime(moment) + timedelta(hours=float(hours))


def coalesce(*values: Any) -> Any:
    for value in values:
        if value is UNKNOWN:
            return UNKNOWN
        if value is not None:
            return value
    return None


def _guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a plain function so it propagates UNKNOWN instead of raising."""

    def wrapper(*args: Any) -> Any:
        if _unknown(*args):
            return UNKNOWN
        return fn(*args)

    wrapper.__name__ = getattr(fn, "__name__", "fn")
    return wrapper


def _count(iterable: Any) -> Any:
    if iterable is UNKNOWN:
        return UNKNOWN
    return sum(1 for _ in iterable)


def _min_or(iterable: Any, default: Any) -> Any:
    if iterable is UNKNOWN:
        return UNKNOWN
    values = [v for v in iterable if v is not None]
    return min(values) if values else default


def _max_or(iterable: Any, default: Any) -> Any:
    if iterable is UNKNOWN:
        return UNKNOWN
    values = [v for v in iterable if v is not None]
    return max(values) if values else default


def _safe_any(iterable: Any) -> Any:
    if iterable is UNKNOWN:
        return UNKNOWN
    saw_unknown = False
    for value in iterable:
        if value is UNKNOWN:
            saw_unknown = True
        elif value:
            return True
    return UNKNOWN if saw_unknown else False


def _safe_all(iterable: Any) -> Any:
    if iterable is UNKNOWN:
        return UNKNOWN
    saw_unknown = False
    for value in iterable:
        if value is UNKNOWN:
            saw_unknown = True
        elif not value:
            return False
    return UNKNOWN if saw_unknown else True


def _safe_sum(iterable: Any) -> Any:
    if iterable is UNKNOWN:
        return UNKNOWN
    total = 0
    for value in iterable:
        if value is UNKNOWN:
            return UNKNOWN
        total += value or 0
    return total


def _safe_len(value: Any) -> Any:
    if value is UNKNOWN:
        return UNKNOWN
    return len(list(value)) if not hasattr(value, "__len__") else len(value)


FUNCTIONS: dict[str, Callable[..., Any]] = {
    # aggregation
    "any": _safe_any,
    "all": _safe_all,
    "sum": _safe_sum,
    "len": _safe_len,
    "count": _count,
    "min_or": _min_or,
    "max_or": _max_or,
    "min": _guarded(min),
    "max": _guarded(max),
    # numbers
    "abs": _guarded(abs),
    "round": _guarded(round),
    "floor_div": _guarded(lambda a, b: a // b),
    # time
    "hours_between": hours_between,
    "minutes_between": minutes_between,
    "days_between": days_between,
    "overlaps": overlaps,
    "add_hours": add_hours,
    # text
    "lower": _guarded(lambda s: str(s).lower()),
    "upper": _guarded(lambda s: str(s).upper()),
    "starts_with": _guarded(lambda s, p: str(s).startswith(p)),
    "contains": _guarded(lambda hay, needle: needle in hay),
    # misc
    "coalesce": coalesce,
    "is_null": lambda v: UNKNOWN if v is UNKNOWN else v is None,
    "default": lambda v, d: UNKNOWN if v is UNKNOWN else (d if v is None else v),
}


# --------------------------------------------------------------------------
# Operator tables
# --------------------------------------------------------------------------

_BINOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_CMPOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}

_ALLOWED_NODES: tuple[type, ...] = (
    ast.Expression, ast.Constant, ast.Name, ast.Attribute, ast.Subscript,
    ast.Tuple, ast.List, ast.Set, ast.Dict, ast.UnaryOp, ast.BinOp,
    ast.BoolOp, ast.Compare, ast.IfExp, ast.Call, ast.GeneratorExp,
    ast.ListComp, ast.SetComp, ast.comprehension,
    # Expression contexts. ``ast.walk`` yields these as nodes in their own
    # right, so they have to be on the list even though they carry no meaning
    # of their own. ``Store`` appears on a comprehension target: the ``m`` in
    # ``for m in flight.roster`` is a Name in Store context. Omitting it makes
    # every collection rule fail to compile, which is exactly what it did.
    ast.Load, ast.Store,
    ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd,
) + tuple(_BINOPS) + tuple(_CMPOPS)


# --------------------------------------------------------------------------
# The interpreter
# --------------------------------------------------------------------------


class _Interpreter:
    def __init__(self, resolver: Resolver, rule_id: str | None, source: str) -> None:
        self.r = resolver
        self.static = bool(getattr(resolver, "static", False))
        self.rule_id = rule_id
        self.source = source

    # -- helpers ----------------------------------------------------------

    def _fail(self, message: str) -> ExpressionError:
        return ExpressionError(message, source=self.source, rule_id=self.rule_id)

    def value(self, node_value: Any) -> Any:
        """Collapse a lazy handle into a concrete value, recording the read."""
        if isinstance(node_value, Ref):
            fetched = self.r.fetch(node_value.path)
            self.r.record(node_value.path, "scalar", fetched)
            return fetched
        if isinstance(node_value, Elem):
            self.r.record(node_value.path, "scalar", node_value.obj)
            return node_value.obj
        return node_value

    def iterate(self, handle: Any) -> list[Any]:
        if isinstance(handle, Ref):
            items = self.r.fetch_collection(handle.path)
            self.r.record(handle.path, "collection", items)
            base = handle.path + "[*]"
            return [Elem(item, base) for item in items]
        if isinstance(handle, Elem):
            self.r.record(handle.path, "collection", handle.obj)
            base = handle.path + "[*]"
            if handle.obj is UNKNOWN:
                return [Elem(UNKNOWN, base)]
            return [Elem(item, base) for item in (handle.obj or ())]
        if handle is UNKNOWN:
            return [UNKNOWN]
        if handle is None:
            return []
        if isinstance(handle, (str, bytes)):
            raise self._fail("refusing to iterate a string")
        return list(handle)

    # -- dispatch ---------------------------------------------------------

    def eval(self, node: ast.AST, env: dict[str, Any]) -> Any:
        method = getattr(self, "n_" + type(node).__name__, None)
        if method is None:
            raise UnsafeExpressionError(
                f"{type(node).__name__} is not permitted in a Canon expression",
                source=self.source, rule_id=self.rule_id,
            )
        return method(node, env)

    # -- leaves -----------------------------------------------------------

    def n_Expression(self, node: ast.Expression, env: dict[str, Any]) -> Any:
        return self.value(self.eval(node.body, env))

    def n_Constant(self, node: ast.Constant, env: dict[str, Any]) -> Any:
        return node.value

    def n_Name(self, node: ast.Name, env: dict[str, Any]) -> Any:
        if node.id in env:
            return env[node.id]
        if node.id in self.r.roots:
            return Ref(node.id)
        raise self._fail(
            f"unknown name {node.id!r}; expected one of "
            f"{sorted(self.r.roots) or ['<no roots declared>']} or a loop variable"
        )

    def n_Attribute(self, node: ast.Attribute, env: dict[str, Any]) -> Any:
        if node.attr.startswith("_"):
            raise UnsafeExpressionError(
                f"attribute {node.attr!r} is not accessible",
                source=self.source, rule_id=self.rule_id,
            )
        target = self.eval(node.value, env)
        return self._attr(target, node.attr)

    def _attr(self, target: Any, name: str) -> Any:
        if isinstance(target, Ref):
            return Ref(f"{target.path}.{name}")
        if isinstance(target, Elem):
            return Elem(_raw_get(target.obj, name), f"{target.path}.{name}")
        if target is UNKNOWN:
            return UNKNOWN
        return _raw_get(target, name)

    def n_Subscript(self, node: ast.Subscript, env: dict[str, Any]) -> Any:
        target = self.eval(node.value, env)
        key = self.value(self.eval(node.slice, env))
        if isinstance(target, Ref):
            if isinstance(key, str):
                return Ref(f"{target.path}.{key}")
            items = self.iterate(target)
            return self._index(items, key)
        if isinstance(target, Elem):
            if isinstance(key, str):
                return Elem(_raw_get(target.obj, key), f"{target.path}.{key}")
            items = self.iterate(target)
            return self._index(items, key)
        if target is UNKNOWN or key is UNKNOWN:
            return UNKNOWN
        if target is None:
            return None
        try:
            return target[key]
        except (KeyError, IndexError, TypeError):
            return None

    def _index(self, items: list[Any], key: Any) -> Any:
        if self.static:
            return items[0] if items else UNKNOWN
        if not isinstance(key, int):
            raise self._fail(f"collection index must be an integer, got {key!r}")
        if -len(items) <= key < len(items):
            return items[key]
        return None

    # -- containers -------------------------------------------------------

    def n_Tuple(self, node: ast.Tuple, env: dict[str, Any]) -> Any:
        return tuple(self.value(self.eval(e, env)) for e in node.elts)

    def n_List(self, node: ast.List, env: dict[str, Any]) -> Any:
        return [self.value(self.eval(e, env)) for e in node.elts]

    def n_Set(self, node: ast.Set, env: dict[str, Any]) -> Any:
        return {self.value(self.eval(e, env)) for e in node.elts}

    def n_Dict(self, node: ast.Dict, env: dict[str, Any]) -> Any:
        out: dict[Any, Any] = {}
        for key_node, value_node in zip(node.keys, node.values):
            if key_node is None:
                raise UnsafeExpressionError(
                    "dict unpacking is not permitted",
                    source=self.source, rule_id=self.rule_id,
                )
            out[self.value(self.eval(key_node, env))] = self.value(
                self.eval(value_node, env))
        return out

    # -- operators --------------------------------------------------------

    def n_UnaryOp(self, node: ast.UnaryOp, env: dict[str, Any]) -> Any:
        operand = self.value(self.eval(node.operand, env))
        if operand is UNKNOWN:
            return UNKNOWN
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
        raise UnsafeExpressionError(
            f"unary operator {type(node.op).__name__} is not permitted",
            source=self.source, rule_id=self.rule_id,
        )

    def n_BinOp(self, node: ast.BinOp, env: dict[str, Any]) -> Any:
        left = self.value(self.eval(node.left, env))
        right = self.value(self.eval(node.right, env))
        if _unknown(left, right):
            return UNKNOWN
        fn = _BINOPS.get(type(node.op))
        if fn is None:
            raise UnsafeExpressionError(
                f"operator {type(node.op).__name__} is not permitted",
                source=self.source, rule_id=self.rule_id,
            )
        if left is None or right is None:
            return None
        try:
            return fn(left, right)
        except ZeroDivisionError:
            raise self._fail("division by zero") from None

    def n_BoolOp(self, node: ast.BoolOp, env: dict[str, Any]) -> Any:
        is_and = isinstance(node.op, ast.And)
        if self.static:
            # No short circuiting during planning: every operand must be
            # visited so the projection covers every branch the engine could
            # take at run time.
            results = [self.value(self.eval(v, env)) for v in node.values]
            if any(r is UNKNOWN for r in results):
                return UNKNOWN
            return all(results) if is_and else any(results)
        result: Any = True if is_and else False
        for operand in node.values:
            result = self.value(self.eval(operand, env))
            if result is UNKNOWN:
                return UNKNOWN
            if is_and and not result:
                return False
            if not is_and and result:
                return True
        return bool(result) if not is_and else True

    def n_Compare(self, node: ast.Compare, env: dict[str, Any]) -> Any:
        left = self.value(self.eval(node.left, env))
        for op, comparator_node in zip(node.ops, node.comparators):
            right = self.value(self.eval(comparator_node, env))
            fn = _CMPOPS.get(type(op))
            if fn is None:
                raise UnsafeExpressionError(
                    f"comparison {type(op).__name__} is not permitted",
                    source=self.source, rule_id=self.rule_id,
                )
            if _unknown(left, right):
                return UNKNOWN
            if isinstance(op, (ast.Is, ast.IsNot)):
                if right is not None and not isinstance(right, bool):
                    raise UnsafeExpressionError(
                        "'is' may only be compared against None, True or False",
                        source=self.source, rule_id=self.rule_id,
                    )
            elif isinstance(op, (ast.In, ast.NotIn)):
                if right is None:
                    return False if isinstance(op, ast.In) else True
            elif left is None or right is None:
                # Null propagates rather than sorting before every number.
                return None
            try:
                outcome = fn(left, right)
            except TypeError as exc:
                raise self._fail(
                    f"cannot compare {left!r} with {right!r}") from exc
            if not outcome:
                return False
            left = right
        return True

    def n_IfExp(self, node: ast.IfExp, env: dict[str, Any]) -> Any:
        if self.static:
            self.value(self.eval(node.test, env))
            self.value(self.eval(node.body, env))
            self.value(self.eval(node.orelse, env))
            return UNKNOWN
        test = self.value(self.eval(node.test, env))
        if test is UNKNOWN:
            return UNKNOWN
        branch = node.body if test else node.orelse
        return self.value(self.eval(branch, env))

    # -- calls ------------------------------------------------------------

    def n_Call(self, node: ast.Call, env: dict[str, Any]) -> Any:
        if not isinstance(node.func, ast.Name):
            raise UnsafeExpressionError(
                "only plain named functions may be called",
                source=self.source, rule_id=self.rule_id,
            )
        name = node.func.id
        fn = FUNCTIONS.get(name)
        if fn is None:
            raise self._fail(
                f"unknown function {name!r}; available: {sorted(FUNCTIONS)}")
        if node.keywords:
            raise UnsafeExpressionError(
                "keyword arguments are not permitted",
                source=self.source, rule_id=self.rule_id,
            )
        args = []
        for arg_node in node.args:
            if isinstance(arg_node, ast.Starred):
                raise UnsafeExpressionError(
                    "argument unpacking is not permitted",
                    source=self.source, rule_id=self.rule_id,
                )
            evaluated = self.eval(arg_node, env)
            if isinstance(evaluated, (Ref, Elem)) and name in _ITERATING:
                items = self.iterate(evaluated)
                if name in _SIZE_ONLY:
                    args.append(items)
                else:
                    args.append([self.value(item) for item in items])
            else:
                args.append(self.value(evaluated))
        return fn(*args)

    # -- comprehensions ---------------------------------------------------

    def _comprehension(self, node: Any, env: dict[str, Any]) -> list[Any]:
        if len(node.generators) != 1:
            raise UnsafeExpressionError(
                "a comprehension may have exactly one 'for' clause; nest "
                "expressions instead so the data dependency stays legible",
                source=self.source, rule_id=self.rule_id,
            )
        gen = node.generators[0]
        if gen.is_async:
            raise UnsafeExpressionError(
                "async comprehensions are not permitted",
                source=self.source, rule_id=self.rule_id,
            )
        if not isinstance(gen.target, ast.Name):
            raise UnsafeExpressionError(
                "the loop variable must be a plain name",
                source=self.source, rule_id=self.rule_id,
            )
        items = self.iterate(self.eval(gen.iter, env))
        out: list[Any] = []
        for item in items:
            scoped = dict(env)
            scoped[gen.target.id] = item
            keep = True
            for condition in gen.ifs:
                verdict = self.value(self.eval(condition, scoped))
                if self.static:
                    continue
                if verdict is UNKNOWN or not verdict:
                    keep = False
                    break
            if keep:
                out.append(self.value(self.eval(node.elt, scoped)))
        return out

    def n_GeneratorExp(self, node: ast.GeneratorExp, env: dict[str, Any]) -> Any:
        return self._comprehension(node, env)

    def n_ListComp(self, node: ast.ListComp, env: dict[str, Any]) -> Any:
        return self._comprehension(node, env)

    def n_SetComp(self, node: ast.SetComp, env: dict[str, Any]) -> Any:
        return set(self._comprehension(node, env))


# Functions that consume an iterable rather than a scalar. When one of these
# receives a bare fact reference we iterate it instead of resolving it, so
# ``len(flight.roster)`` records a collection read rather than a scalar one.
_ITERATING = frozenset({"any", "all", "sum", "len", "count", "min", "max",
                        "min_or", "max_or"})

# For these, only the size of the collection matters, so the elements are left
# unresolved and the trace records a single collection read rather than one
# scalar read per element.
_SIZE_ONLY = frozenset({"len", "count"})


# --------------------------------------------------------------------------
# Public expression object
# --------------------------------------------------------------------------


def _validate(tree: ast.AST, source: str, rule_id: str | None) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise UnsafeExpressionError(
                f"{type(node).__name__} is not permitted in a Canon expression",
                source=source, rule_id=rule_id,
            )
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise UnsafeExpressionError(
                f"attribute {node.attr!r} is not accessible",
                source=source, rule_id=rule_id,
            )
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise UnsafeExpressionError(
                f"name {node.id!r} is not accessible",
                source=source, rule_id=rule_id,
            )


class Expression:
    """A compiled, validated Canon expression."""

    __slots__ = ("source", "tree", "rule_id")

    def __init__(self, source: str, rule_id: str | None = None) -> None:
        if not isinstance(source, str):
            raise ExpressionError(
                f"expression must be a string, got {type(source).__name__}",
                rule_id=rule_id)
        self.source = source.strip()
        self.rule_id = rule_id
        if not self.source:
            raise ExpressionError("empty expression", rule_id=rule_id)
        try:
            self.tree = ast.parse(self.source, mode="eval")
        except SyntaxError as exc:
            message = f"syntax error: {exc.msg}"
            if "\n" in self.source:
                # Almost always YAML folding rather than a typo. In a folded
                # block (>), a line indented further than the first line keeps
                # its line break instead of being folded into a space, which
                # silently turns one expression into two.
                message += (
                    ". The expression spans more than one line. If it came from "
                    "a YAML folded block, align every line to the same "
                    "indentation as the first one, otherwise the deeper lines "
                    "keep their line breaks and the expression is cut in half"
                )
            raise ExpressionError(
                message, source=self.source, rule_id=rule_id
            ) from exc
        _validate(self.tree, self.source, rule_id)

    # -- evaluation -------------------------------------------------------

    def evaluate(self, resolver: Resolver,
                 env: Mapping[str, Any] | None = None) -> Any:
        interpreter = _Interpreter(resolver, self.rule_id, self.source)
        result = interpreter.eval(self.tree, dict(env or {}))
        return interpreter.value(result)

    def analyse(self, roots: Iterable[str]) -> list[str]:
        """Return the fact paths this expression can read, without any data."""
        resolver = StaticResolver(roots)
        self.evaluate(resolver)
        return resolver.read_paths()

    # -- niceties ---------------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Expression({self.source!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Expression) and other.source == self.source

    def __hash__(self) -> int:
        return hash(self.source)


_CACHE: dict[tuple[str, str | None], Expression] = {}


def compile_expression(source: str, rule_id: str | None = None) -> Expression:
    """Compile with a process wide cache.

    Rulesets are immutable and expressions repeat across rules, so caching by
    source text keeps repeated parsing out of the hot path without introducing
    any per request state.
    """
    key = (source.strip() if isinstance(source, str) else source, rule_id)
    cached = _CACHE.get(key)
    if cached is None:
        cached = Expression(source, rule_id)
        _CACHE[key] = cached
    return cached
