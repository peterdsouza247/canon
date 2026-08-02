"""Front ends: YAML rule files and decision tables.

The Python front end lives in ``dsl.py``. All three compile to the same
``Rule`` objects, so a team can mix them inside one ruleset and the engine, the
trace, the hashes and the projection behave identically. That matters for the
migration story: you do not have to pick an authoring style before you know
which one your rule authors get on with.
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .errors import RuleDefinitionError, RuleSetError
from .expr import compile_expression
from .rules import Emission, Rule, RuleSet, _as_date

__all__ = [
    "load_yaml", "load_yaml_text", "load_mapping",
    "load_decision_table", "load_decision_table_text",
    "load_directory", "dump_yaml_dict",
]


def _require_yaml():
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuleSetError(
            "PyYAML is needed to read .yaml rule files. Install it with "
            "'pip install pyyaml', or use the JSON form, which needs nothing."
        ) from exc
    return yaml


# --------------------------------------------------------------------------
# YAML / JSON / mapping
# --------------------------------------------------------------------------


def load_mapping(data: Mapping[str, Any], *, source: str = "<mapping>") -> RuleSet:
    if "rules" not in data:
        raise RuleSetError(f"{source}: no 'rules' key")
    ruleset_id = data.get("ruleset") or data.get("id")
    if not ruleset_id:
        raise RuleSetError(f"{source}: no 'ruleset' name")

    derived_policy: dict[str, str] = {}
    for name, spec in (data.get("derived") or {}).items():
        if isinstance(spec, str):
            derived_policy[name] = spec
        elif isinstance(spec, Mapping):
            derived_policy[name] = spec.get("combine", "error")
        else:
            raise RuleSetError(
                f"{source}: derived policy for {name!r} must be a string or a "
                f"mapping with a 'combine' key")

    rules = [_rule_from_mapping(raw, source, index)
             for index, raw in enumerate(data["rules"], start=1)]

    return RuleSet(
        id=str(ruleset_id),
        rules=rules,
        version=str(data.get("version", "1")),
        roots=data.get("roots") or (),
        derived_policy=derived_policy,
        description=data.get("description", ""),
    )


def _rule_from_mapping(raw: Mapping[str, Any], source: str, index: int) -> Rule:
    if not isinstance(raw, Mapping):
        raise RuleDefinitionError(f"{source}: rule #{index} is not a mapping")
    rule_id = raw.get("id")
    if not rule_id:
        raise RuleDefinitionError(f"{source}: rule #{index} has no id")
    where = f"{source}#{rule_id}"

    unknown = set(raw) - _RULE_KEYS
    if unknown:
        raise RuleDefinitionError(
            f"{where}: unknown keys {sorted(unknown)}. Canon rejects unknown "
            f"keys so that a typo in a rule file fails at load rather than "
            f"silently disabling a condition.")

    when_src = raw.get("when")
    when = compile_expression(when_src, rule_id) if when_src else None

    emit_raw = raw.get("emit")
    emission = None
    if emit_raw:
        if not isinstance(emit_raw, Mapping) or "code" not in emit_raw:
            raise RuleDefinitionError(f"{where}: 'emit' needs at least a 'code'")
        detail = {name: compile_expression(str(expr), rule_id)
                  for name, expr in (emit_raw.get("detail") or {}).items()}
        emission = Emission(
            code=str(emit_raw["code"]),
            severity=str(emit_raw.get("severity", "hard")),
            message=str(emit_raw.get("message", "")),
            detail=detail,
        )

    sets = {name: compile_expression(str(expr), rule_id)
            for name, expr in (raw.get("set") or {}).items()}

    clients = raw.get("clients") or ["*"]
    if isinstance(clients, str):
        clients = [clients]

    tags = raw.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]

    reads = raw.get("reads") or []
    if isinstance(reads, str):
        reads = [reads]

    return Rule(
        id=str(rule_id),
        version=str(raw.get("version", "1")),
        title=str(raw.get("title", "")),
        description=str(raw.get("description", "")),
        when=when,
        emit=emission,
        sets=sets,
        reads=tuple(str(r) for r in reads),
        clients=tuple(str(c) for c in clients),
        effective_from=_as_date(raw.get("effective_from"), f"{where}.effective_from"),
        effective_to=_as_date(raw.get("effective_to"), f"{where}.effective_to"),
        priority=int(raw.get("priority", 100)),
        tags=tuple(str(t) for t in tags),
        owner=str(raw.get("owner", "")),
        authoring="yaml",
        source_ref=where,
    )


_RULE_KEYS = {
    "id", "version", "title", "description", "when", "emit", "set", "reads",
    "clients", "effective_from", "effective_to", "priority", "tags", "owner",
}


def load_yaml_text(text: str, *, source: str = "<text>") -> RuleSet:
    yaml = _require_yaml()
    return load_mapping(yaml.safe_load(text) or {}, source=source)


def load_yaml(path: str | Path) -> RuleSet:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return load_mapping(json.loads(text), source=str(path))
    return load_yaml_text(text, source=str(path))


def load_directory(directory: str | Path, *, ruleset_id: str | None = None,
                   version: str = "1") -> RuleSet:
    """Merge every rule file in a directory into one ruleset.

    Large rulesets belong in many small files organised by regulation or by
    client. Merging at load time keeps the file layout a matter of taste while
    the validation stays global, which is the only level at which cycles and
    write conflicts can actually be detected.
    """
    directory = Path(directory)
    parts: list[RuleSet] = []
    for path in sorted(directory.rglob("*")):
        if path.suffix.lower() in (".yaml", ".yml", ".json"):
            parts.append(load_yaml(path))
        elif path.suffix.lower() == ".csv":
            parts.append(load_decision_table(path))
    if not parts:
        raise RuleSetError(f"no rule files found under {directory}")

    merged_rules: list[Rule] = []
    policy: dict[str, str] = {}
    roots: set[str] = set()
    for part in parts:
        merged_rules.extend(part.rules)
        policy.update(part.derived_policy)
        roots.update(part.roots)
    return RuleSet(
        id=ruleset_id or parts[0].id,
        rules=merged_rules,
        version=version,
        roots=roots,
        derived_policy=policy,
    )


def dump_yaml_dict(ruleset: RuleSet) -> dict[str, Any]:
    """Render a ruleset back to the mapping form. Used by the ODM importer."""
    out: dict[str, Any] = {
        "ruleset": ruleset.id,
        "version": ruleset.version,
    }
    if ruleset.description:
        out["description"] = ruleset.description
    if ruleset.derived_policy:
        out["derived"] = {name: {"combine": policy}
                          for name, policy in sorted(ruleset.derived_policy.items())}
    rules: list[dict[str, Any]] = []
    for rule in ruleset.rules:
        entry: dict[str, Any] = {"id": rule.id, "version": rule.version}
        if rule.title:
            entry["title"] = rule.title
        if rule.description:
            entry["description"] = rule.description
        if rule.priority != 100:
            entry["priority"] = rule.priority
        if rule.clients != ("*",):
            entry["clients"] = list(rule.clients)
        if rule.reads:
            entry["reads"] = list(rule.reads)
        if rule.effective_from:
            entry["effective_from"] = rule.effective_from.isoformat()
        if rule.effective_to:
            entry["effective_to"] = rule.effective_to.isoformat()
        if rule.when is not None:
            entry["when"] = rule.when.source
        if rule.emit is not None:
            emit: dict[str, Any] = {
                "code": rule.emit.code,
                "severity": rule.emit.severity,
            }
            if rule.emit.message:
                emit["message"] = rule.emit.message
            if rule.emit.detail:
                emit["detail"] = {k: v.source
                                  for k, v in sorted(rule.emit.detail.items())}
            entry["emit"] = emit
        if rule.sets:
            entry["set"] = {k: v.source for k, v in sorted(rule.sets.items())}
        if rule.tags:
            entry["tags"] = list(rule.tags)
        rules.append(entry)
    out["rules"] = rules
    return out


# --------------------------------------------------------------------------
# Decision tables
# --------------------------------------------------------------------------

_OPS = {"==", "!=", ">", ">=", "<", "<=", "in", "not in", "contains",
        "not contains"}
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


def _cell_to_expression(path: str, op: str, cell: str) -> str:
    """Turn one table cell into a condition.

    Cells hold values, not code, which is the entire point of a decision table.
    An author who needs code can still write it by prefixing the cell with '='
    and that escape hatch is visible in the table, so a reviewer can see at a
    glance which rows stopped being data.
    """
    value = cell.strip()
    if value.startswith("="):
        literal = value[1:].strip()
    elif _NUMBER.match(value):
        literal = value
    elif value.lower() in ("true", "false"):
        literal = value.capitalize()
    elif value.lower() in ("null", "none"):
        literal = "None"
    elif "," in value and op in ("in", "not in"):
        parts = [p.strip() for p in value.split(",") if p.strip()]
        literal = "[" + ", ".join(
            p if _NUMBER.match(p) else json.dumps(p) for p in parts) + "]"
    else:
        literal = json.dumps(value)

    if op == "contains":
        return f"{literal} in {path}"
    if op == "not contains":
        return f"{literal} not in {path}"
    return f"{path} {op} {literal}"


def load_decision_table_text(text: str, *, source: str = "<csv>",
                             ruleset_id: str | None = None,
                             version: str = "1",
                             derived_policy: Mapping[str, str] | None = None,
                             ) -> RuleSet:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise RuleSetError(f"{source}: empty decision table")
    headers = [h.strip() for h in reader.fieldnames]

    conditions: list[tuple[str, str, str]] = []  # (header, path, op)
    for header in headers:
        if header.lower().startswith("when "):
            body = header[5:].strip()
            op = "=="
            for candidate in sorted(_OPS, key=len, reverse=True):
                if body.endswith(" " + candidate):
                    op = candidate
                    body = body[: -(len(candidate) + 1)].strip()
                    break
            conditions.append((header, body, op))

    rules: list[Rule] = []
    for row_number, row in enumerate(reader, start=2):
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        if not any(row.values()):
            continue
        rule_id = row.get("id") or f"{Path(source).stem}-{row_number:03d}"
        where = f"{source}:{row_number}"

        clauses = [_cell_to_expression(path, op, row[header])
                   for header, path, op in conditions
                   if row.get(header)]
        when = compile_expression(" and ".join(clauses), rule_id) if clauses else None

        code = row.get("then code") or row.get("code")
        emission = None
        if code:
            detail = {
                key.split(" ", 2)[-1]: compile_expression(value, rule_id)
                for key, value in row.items()
                if key.lower().startswith("then detail ") and value
            }
            emission = Emission(
                code=code,
                severity=row.get("then severity") or row.get("severity") or "hard",
                message=row.get("then message") or row.get("message") or "",
                detail=detail,
            )

        sets = {
            key.split(" ", 2)[-1]: compile_expression(value, rule_id)
            for key, value in row.items()
            if key.lower().startswith("then set ") and value
        }

        reads = tuple(r.strip() for r in (row.get("reads") or "").split(";") if r.strip())
        clients = tuple(c.strip() for c in (row.get("clients") or "*").split(";") if c.strip())

        rules.append(Rule(
            id=rule_id,
            version=row.get("version") or "1",
            title=row.get("title", ""),
            when=when,
            emit=emission,
            sets=sets,
            reads=reads,
            clients=clients or ("*",),
            effective_from=_as_date(row.get("effective_from") or None,
                                    f"{where}.effective_from"),
            effective_to=_as_date(row.get("effective_to") or None,
                                  f"{where}.effective_to"),
            priority=int(row.get("priority") or 100),
            authoring="table",
            source_ref=where,
        ))

    if not rules:
        raise RuleSetError(f"{source}: decision table produced no rules")

    return RuleSet(
        id=ruleset_id or Path(source).stem,
        rules=rules,
        version=version,
        derived_policy=derived_policy or {},
    )


def load_decision_table(path: str | Path, **kwargs: Any) -> RuleSet:
    path = Path(path)
    return load_decision_table_text(
        path.read_text(encoding="utf-8"), source=str(path), **kwargs)
