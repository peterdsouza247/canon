"""Proposals: a rule change expressed as a diff, not as a duplicated ruleset.

When a regulator publishes a fatigue package, or an operator wants to tighten a
margin, the thing under discussion is three or four specific changes. Copying
the whole ruleset to a second file and asking people to spot the differences
throws that away, and it is how a change gets approved that nobody actually
read.

A proposal names only what moves::

    proposal: 2026-09-fatigue-package
    against: crew_rostering
    modify:
      - id: FTL-002
        version: "4"
        set:
          max_fdp_hours: "max(9.0, limits.max_fdp_hours_base - 0.75 * (duty.sectors - 2))"
    add:
      - id: CREW-006
        ...
    remove: [FTL-095]

Applying it produces a real ``RuleSet`` that goes through the same validation as
any other, so a proposal cannot introduce a cycle, an undeclared dependency or
an unresolved write conflict and have it discovered later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import RuleSetError
from .loaders import dump_yaml_dict, load_mapping
from .rules import RuleSet

__all__ = ["Proposal", "load_proposal", "load_proposal_text", "apply_proposal"]

_TOP_KEYS = {"proposal", "against", "version", "description", "author",
             "modify", "add", "remove", "derived", "effective_from"}


@dataclass
class Proposal:
    """A named set of changes to apply to an existing ruleset."""

    id: str
    against: str = ""
    version: str = ""
    description: str = ""
    author: str = ""
    modify: list[dict[str, Any]] = field(default_factory=list)
    add: list[dict[str, Any]] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    derived: dict[str, Any] = field(default_factory=dict)

    # -- construction -----------------------------------------------------

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *,
                     source: str = "<mapping>") -> "Proposal":
        unknown = set(raw) - _TOP_KEYS
        if unknown:
            raise RuleSetError(
                f"{source}: unknown keys in proposal {sorted(unknown)}. "
                f"Expected some of {sorted(_TOP_KEYS)}.")
        identifier = raw.get("proposal") or raw.get("id")
        if not identifier:
            raise RuleSetError(f"{source}: a proposal needs a 'proposal' name")

        modify = list(raw.get("modify") or [])
        add = list(raw.get("add") or [])
        remove = list(raw.get("remove") or [])
        for entry in modify + add:
            if not isinstance(entry, dict) or not entry.get("id"):
                raise RuleSetError(
                    f"{source}: every entry under 'modify' and 'add' needs an id")

        return cls(
            id=str(identifier),
            against=str(raw.get("against", "")),
            version=str(raw.get("version", "")),
            description=str(raw.get("description", "")),
            author=str(raw.get("author", "")),
            modify=[dict(entry) for entry in modify],
            add=[dict(entry) for entry in add],
            remove=[str(rule_id) for rule_id in remove],
            derived=dict(raw.get("derived") or {}),
        )

    # -- application ------------------------------------------------------

    def apply(self, base: RuleSet) -> RuleSet:
        """Return a new ruleset with this proposal applied.

        The base ruleset is not touched. The candidate is rebuilt through the
        ordinary loader, so it gets the ordinary validation: a proposal that
        introduces a cycle, an undeclared read or an unpoliced write conflict
        fails here rather than in production.
        """
        if self.against and self.against != base.id:
            raise RuleSetError(
                f"proposal {self.id!r} is written against ruleset "
                f"{self.against!r} but was applied to {base.id!r}")

        document = dump_yaml_dict(base)
        by_id = {rule["id"]: rule for rule in document["rules"]}

        for rule_id in self.remove:
            if rule_id not in by_id:
                raise RuleSetError(
                    f"proposal {self.id!r} removes rule {rule_id!r}, which is "
                    f"not in ruleset {base.id!r}")
            by_id.pop(rule_id)

        for entry in self.modify:
            rule_id = entry["id"]
            if rule_id not in by_id:
                raise RuleSetError(
                    f"proposal {self.id!r} modifies rule {rule_id!r}, which is "
                    f"not in ruleset {base.id!r}. Use 'add' for a new rule.")
            merged = dict(by_id[rule_id])
            for key, value in entry.items():
                if key == "id":
                    continue
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            by_id[rule_id] = merged

        for entry in self.add:
            rule_id = entry["id"]
            if rule_id in by_id:
                raise RuleSetError(
                    f"proposal {self.id!r} adds rule {rule_id!r}, which already "
                    f"exists in ruleset {base.id!r}. Use 'modify' instead.")
            by_id[rule_id] = dict(entry)

        document["rules"] = [by_id[rule_id] for rule_id in
                             sorted(by_id, key=_ordering(document))]
        if self.version:
            document["version"] = self.version
        if self.derived:
            existing = document.get("derived") or {}
            for name, spec in self.derived.items():
                existing[name] = spec if isinstance(spec, dict) else {"combine": spec}
            document["derived"] = existing

        return load_mapping(document, source=f"<proposal:{self.id}>")

    # -- description ------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "proposal": self.id,
            "against": self.against,
            "version": self.version,
            "modifies": [entry["id"] for entry in self.modify],
            "adds": [entry["id"] for entry in self.add],
            "removes": list(self.remove),
        }

    def render(self) -> str:
        lines = [f"proposal {self.id}" + (f" v{self.version}" if self.version else "")]
        if self.description:
            lines.append("  " + " ".join(self.description.split()))
        for entry in self.modify:
            fields = sorted(k for k in entry if k != "id")
            lines.append(f"  modify {entry['id']:<12} changes {fields}")
        for entry in self.add:
            lines.append(f"  add    {entry['id']:<12} {entry.get('title', '')}")
        for rule_id in self.remove:
            lines.append(f"  remove {rule_id}")
        return "\n".join(lines)


def _ordering(document: Mapping[str, Any]):
    """Keep the original file order, with added rules at the end."""
    original = {rule["id"]: index for index, rule in enumerate(document["rules"])}

    def key(rule_id: str) -> tuple[int, str]:
        return (original.get(rule_id, 10 ** 6), rule_id)

    return key


def load_proposal_text(text: str, *, source: str = "<text>") -> Proposal:
    stripped = text.lstrip()
    if stripped.startswith("{"):
        return Proposal.from_mapping(json.loads(text), source=source)
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuleSetError(
            "PyYAML is needed to read a .yaml proposal. Install it with "
            "'pip install pyyaml', or write the proposal as JSON."
        ) from exc
    return Proposal.from_mapping(yaml.safe_load(text) or {}, source=source)


def load_proposal(path: str | Path) -> Proposal:
    path = Path(path)
    return load_proposal_text(path.read_text(encoding="utf-8"), source=str(path))


def apply_proposal(base: RuleSet, proposal: Proposal | str | Path) -> RuleSet:
    if not isinstance(proposal, Proposal):
        proposal = load_proposal(proposal)
    return proposal.apply(base)
