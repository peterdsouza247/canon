"""Fact planning and lazy, batched fact resolution.

Three of the recurring problems are really one problem:

* payloads are large and growing (1),
* the design makes it hard to send only what is needed (2),
* data arrives in horizontal slices when rules want vertical ones (3).

They collapse into a single question: *who decides what data a decision needs?*
Today a human does, in a hand maintained mapping layer, and that mapping drifts
away from the rules. Canon makes the rules decide. ``Projection`` is computed
by static analysis of the ruleset, so it is always exactly the set of fields the
rules can read, and it names collection traversals explicitly, which is what
makes a vertical slice ordinary rather than a workaround.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping as _AbcMapping
from collections.abc import Sequence as _AbcSequence
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .errors import FactResolutionError, UnplannedFactError
from .expr import Resolver, _raw_get

__all__ = [
    "PathNode",
    "Projection",
    "FactRequest",
    "FactSource",
    "FactStore",
    "dict_source",
]


# --------------------------------------------------------------------------
# Projection: the machine readable payload contract
# --------------------------------------------------------------------------


@dataclass
class PathNode:
    """One node in the projected shape of the fact document."""

    name: str
    is_collection: bool = False
    children: dict[str, "PathNode"] = field(default_factory=dict)

    def child(self, name: str, is_collection: bool = False) -> "PathNode":
        node = self.children.get(name)
        if node is None:
            node = PathNode(name)
            self.children[name] = node
        if is_collection:
            node.is_collection = True
        return node

    def to_dict(self) -> Any:
        if not self.children:
            return "*" if self.is_collection else "leaf"
        body = {name: child.to_dict() for name, child in sorted(self.children.items())}
        return {"__each__": body} if self.is_collection else body

    def leaf_count(self) -> int:
        if not self.children:
            return 1
        return sum(child.leaf_count() for child in self.children.values())


def _split(path: str) -> list[tuple[str, bool]]:
    """Split ``a.b[*].c`` into ``[('a', False), ('b', True), ('c', False)]``."""
    parts: list[tuple[str, bool]] = []
    for raw in path.split("."):
        collection = False
        name = raw
        while name.endswith("[*]"):
            collection = True
            name = name[:-3]
        if not name:
            raise FactResolutionError(f"malformed fact path {path!r}")
        parts.append((name, collection))
    return parts


class Projection:
    """The set of fact paths a ruleset can read, as a tree.

    This object is the artefact you hand to the calling application. It is
    stable, diffable, and generated, so the integration layer stops being a
    place where knowledge is duplicated by hand.
    """

    def __init__(self, paths: Iterable[str] = ()) -> None:
        self.roots: dict[str, PathNode] = {}
        self._paths: set[str] = set()
        for path in paths:
            self.add(path)

    # -- construction -----------------------------------------------------

    def add(self, path: str) -> None:
        segments = _split(path)
        self._paths.add(path)
        root_name, root_collection = segments[0]
        node = self.roots.get(root_name)
        if node is None:
            node = PathNode(root_name)
            self.roots[root_name] = node
        if root_collection:
            node.is_collection = True
        for name, is_collection in segments[1:]:
            node = node.child(name, is_collection)

    @classmethod
    def from_paths(cls, paths: Iterable[str]) -> "Projection":
        return cls(paths)

    def merge(self, other: "Projection") -> "Projection":
        return Projection(self._paths | other._paths)

    # -- inspection -------------------------------------------------------

    @property
    def paths(self) -> list[str]:
        return sorted(self._paths)

    @property
    def root_names(self) -> list[str]:
        return sorted(self.roots)

    def covers(self, path: str) -> bool:
        """True if ``path`` or a prefix of it was planned.

        A read of ``crew.address`` is covered by a planned ``crew.address``, and
        also by a planned ``crew.address.city``, because fetching the child
        implies the parent was present in the payload.
        """
        if path in self._paths:
            return True
        prefix = path + "."
        return any(p.startswith(prefix) for p in self._paths)

    def leaf_count(self) -> int:
        return sum(node.leaf_count() for node in self.roots.values())

    def to_dict(self) -> dict[str, Any]:
        return {name: node.to_dict() for name, node in sorted(self.roots.items())}

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Projection({self.leaf_count()} leaves across {len(self.roots)} roots)"

    # -- trimming ---------------------------------------------------------

    def select(self, document: Mapping[str, Any]) -> dict[str, Any]:
        """Trim a full fact document down to the projected shape.

        Useful in two places. A sidecar deployment can trim inbound payloads to
        prove how much of the current payload is dead weight, and the shadow
        harness uses it to replay captured production payloads without paying
        to move the unused parts around.
        """
        out: dict[str, Any] = {}
        for name, node in self.roots.items():
            if name not in document:
                continue
            trimmed = _select_node(node, document[name])
            if trimmed is not None:
                out[name] = trimmed
        return out


def _select_node(node: PathNode, value: Any) -> Any:
    if value is None:
        return None
    if node.is_collection:
        if not isinstance(value, _AbcSequence) or isinstance(value, (str, bytes)):
            return value
        return [_select_leafish(node, item) for item in value]
    return _select_leafish(node, value)


def _select_leafish(node: PathNode, value: Any) -> Any:
    if not node.children:
        return value
    out: dict[str, Any] = {}
    for name, child in node.children.items():
        raw = _raw_get(value, name)
        if raw is None:
            continue
        out[name] = _select_node(child, raw)
    return out


# --------------------------------------------------------------------------
# Fact sources
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FactRequest:
    """What the engine asks a source for.

    ``paths`` is the precise list of leaves the rules can read under this root,
    so a SQL backed source can build a narrow SELECT and an HTTP backed source
    can send a field mask. The source is never asked for a whole aggregate.
    """

    root: str
    paths: tuple[str, ...]
    projection: Projection
    key: Mapping[str, Any]

    def relative_paths(self) -> tuple[str, ...]:
        cut = len(self.root) + 1
        return tuple(p[cut:] for p in self.paths if len(p) > cut)


FactSource = Callable[[FactRequest], Any]


def dict_source(document: Mapping[str, Any]) -> dict[str, FactSource]:
    """Build one source per top level key of an in memory document.

    This is the source used by tests, the CLI, and the shadow replayer. Real
    deployments register sources that hit a crew database, a flight schedule
    service, or the calling application over gRPC.
    """

    def make(root: str) -> FactSource:
        def source(request: FactRequest) -> Any:
            return document.get(root)

        return source

    return {root: make(root) for root in document}


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------


class FactStore(Resolver):
    """Lazy, batched, single fetch per root, with full read accounting.

    Laziness matters more than it sounds. A ruleset of two hundred rules has a
    planned projection covering all two hundred, but on any given transaction
    most guards fail on their first term. Roots whose facts are never touched
    are never fetched, so the effective payload for a typical request is far
    smaller than the worst case contract.
    """

    static = False

    def __init__(self, projection: Projection, sources: Mapping[str, FactSource],
                 key: Mapping[str, Any] | None = None, *,
                 strict: bool = True,
                 seed: Mapping[str, Any] | None = None) -> None:
        super().__init__(set(projection.root_names) | set(sources) | set(seed or {}))
        self.projection = projection
        self.sources = dict(sources)
        self.key = dict(key or {})
        self.strict = strict
        self._loaded: dict[str, Any] = dict(seed or {})
        self._preloaded_roots = set(self._loaded)
        self.unplanned: list[str] = []
        self.fetch_count = 0
        self.fetch_nanos = 0
        self.fetched_roots: list[str] = []

    # -- root loading -----------------------------------------------------

    def _root_of(self, path: str) -> str:
        return path.split(".", 1)[0].replace("[*]", "")

    def _load_root(self, root: str) -> Any:
        if root in self._loaded:
            return self._loaded[root]
        source = self.sources.get(root)
        if source is None:
            raise FactResolutionError(
                f"no fact source registered for root {root!r}; "
                f"registered roots are {sorted(self.sources)}")
        paths = tuple(p for p in self.projection.paths
                      if self._root_of(p) == root)
        request = FactRequest(
            root=root,
            paths=paths,
            projection=Projection(paths),
            key=self.key,
        )
        started = time.perf_counter_ns()
        try:
            value = source(request)
        except Exception as exc:  # noqa: BLE001 - surfaced with context
            raise FactResolutionError(
                f"fact source for root {root!r} failed: {exc}") from exc
        self.fetch_nanos += time.perf_counter_ns() - started
        self.fetch_count += 1
        self.fetched_roots.append(root)
        self._loaded[root] = value
        return value

    def set_root(self, root: str, value: Any) -> None:
        """Inject a root directly. The engine uses this for derived facts."""
        self._loaded[root] = value
        self.roots = self.roots | {root}

    # -- Resolver interface ----------------------------------------------

    def _check_planned(self, path: str) -> None:
        if self.projection.covers(path):
            return
        if path.split(".", 1)[0] == "derived":
            return
        self.unplanned.append(path)
        if self.strict:
            raise UnplannedFactError(
                f"rule read unplanned fact path {path!r}. The projection handed "
                f"to the caller would not have contained it. Either the rule "
                f"uses dynamic access that static analysis cannot see, or the "
                f"projection is stale."
            )

    def fetch(self, path: str) -> Any:
        self._check_planned(path)
        segments = path.split(".")
        cur = self._load_root(segments[0])
        for seg in segments[1:]:
            if cur is None:
                return None
            cur = _raw_get(cur, seg)
        return cur

    def fetch_collection(self, path: str) -> Sequence[Any]:
        value = self.fetch(path)
        if value is None:
            return ()
        if isinstance(value, (str, bytes)):
            raise FactResolutionError(
                f"fact path {path!r} is a string but a rule iterated it")
        if isinstance(value, _AbcMapping):
            return list(value.values())
        try:
            return list(value)
        except TypeError as exc:
            raise FactResolutionError(
                f"fact path {path!r} is not iterable: {value!r}") from exc

    # -- accounting -------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        planned = self.projection.paths
        # Derived facts are outputs of other rules, not payload, so they are
        # excluded from the accounting that the payload contract is judged on.
        read = [p for p in self.read_paths() if not p.startswith("derived.")]
        unread = [p for p in planned if p not in self.reads]
        return {
            "planned_paths": len(planned),
            "read_paths": len(read),
            "unread_paths": len(unread),
            "unread": unread,
            "unplanned": list(self.unplanned),
            "roots_planned": len(self.projection.roots),
            "roots_fetched": len(self.fetched_roots),
            "fetches": self.fetch_count,
            "fetch_micros": round(self.fetch_nanos / 1000.0, 1),
        }

    def read_document(self) -> dict[str, Any]:
        """The values actually consumed, keyed by path. This is what goes on
        the trace and what makes a decision reproducible from the record."""
        return {path: _jsonable(read.value)
                for path, read in sorted(self.reads.items())
                if read.kind == "scalar" and not path.startswith("derived.")}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, _AbcMapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)
