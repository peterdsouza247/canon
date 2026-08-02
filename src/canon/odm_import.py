"""Importer for IBM ODM business action language.

This is an accelerator, not a magic wand, and it is written to be honest about
which it is. ODM verbalisation is domain specific by design: the phrase "the
duty hours of 'the crew member'" only means something because somebody mapped it
to a getter in a BOM. No importer can recover that mapping from the rule text
alone, so this one takes the mapping as input and refuses to guess when it is
missing.

What it does automate is the tedious and error prone part: turning several
hundred structurally similar conditions into expressions, preserving rule names
and priorities, and producing a report of everything it would not convert. In
practice that is most of the work, and the residue is the part a human should
have been looking at anyway.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import MigrationError
from .loaders import load_mapping
from .rules import RuleSet

__all__ = ["Verbalisation", "ImportResult", "parse_bal", "import_bal_file"]


# --------------------------------------------------------------------------
# Verbalisation mapping
# --------------------------------------------------------------------------


@dataclass
class Verbalisation:
    """The BOM knowledge an importer cannot infer.

    ``objects`` maps an ODM variable phrase to a Canon fact root, for example
    ``"the crew member" -> "crew"``. ``attributes`` optionally overrides the
    default snake case conversion of an attribute phrase, for the cases where
    the verbalisation and the field name genuinely differ.
    """

    objects: dict[str, str] = field(default_factory=dict)
    attributes: dict[str, str] = field(default_factory=dict)
    codes: dict[str, str] = field(default_factory=dict)
    result_object: str = "the result"

    @classmethod
    def load(cls, path: str | Path) -> "Verbalisation":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            objects=raw.get("objects", {}),
            attributes=raw.get("attributes", {}),
            codes=raw.get("codes", {}),
            result_object=raw.get("result_object", "the result"),
        )

    def root_for(self, phrase: str) -> str:
        key = phrase.strip().strip("'\"").lower()
        if key in self.objects:
            return self.objects[key]
        stripped = key[4:] if key.startswith("the ") else key
        if stripped in self.objects:
            return self.objects[stripped]
        raise MigrationError(
            f"no fact root mapped for the ODM object {phrase!r}. Add it to the "
            f"verbalisation file, for example {{\"objects\": {{\"{stripped}\": "
            f"\"crew\"}}}}. Guessing here would silently change what a rule "
            f"reads, so the importer will not do it.")

    def field_for(self, phrase: str) -> str:
        key = phrase.strip().lower()
        if key in self.attributes:
            return self.attributes[key]
        return re.sub(r"[^a-z0-9]+", "_", key).strip("_")


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------


@dataclass
class ImportResult:
    ruleset: dict[str, Any]
    converted: list[str] = field(default_factory=list)
    needs_review: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        total = len(self.converted) + len(self.needs_review)
        return len(self.converted) / total if total else 0.0

    def to_ruleset(self) -> RuleSet:
        return load_mapping(self.ruleset, source="<odm-import>")

    def render(self) -> str:
        lines = [
            f"converted {len(self.converted)} rules, "
            f"{len(self.needs_review)} need review "
            f"({self.coverage:.0%} automatic)",
        ]
        if self.needs_review:
            lines.append("")
            lines.append("needs review")
            for item in self.needs_review:
                lines.append(f"  {item['rule']}: {item['reason']}")
                if item.get("text"):
                    lines.append(f"      {item['text']}")
        if self.warnings:
            lines.append("")
            lines.append("warnings")
            for warning in self.warnings:
                lines.append(f"  {warning}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Grammar
# --------------------------------------------------------------------------

# Ordered longest first so that "is at least" wins over "is".
_COMPARATORS: list[tuple[str, str]] = [
    ("is not one of", "not in"),
    ("is greater than or equal to", ">="),
    ("is less than or equal to", "<="),
    ("is not equal to", "!="),
    ("does not contain", "not contains"),
    ("is more than", ">"),
    ("is later than", ">"),
    ("is earlier than", "<"),
    ("is less than", "<"),
    ("is at least", ">="),
    ("is at most", "<="),
    ("is one of", "in"),
    ("is not", "!="),
    ("contains", "contains"),
    ("is", "=="),
]

_RULE_BLOCK = re.compile(
    r"rule\s+(?P<name>[\w.\-]+)\s*\{(?P<body>.*?)\n\}", re.DOTALL)
_WHEN_BLOCK = re.compile(r"when\s*\{(?P<body>.*?)\}\s*then", re.DOTALL)
_THEN_BLOCK = re.compile(r"then\s*\{(?P<body>.*?)\}\s*$", re.DOTALL)
_PRIORITY = re.compile(r"priority\s*=\s*(?P<value>[\w\-]+)")
_ATTR_OF = re.compile(r"the\s+(?P<attr>[\w \-]+?)\s+of\s+(?P<obj>'[^']+'|\"[^\"]+\")")
_SET_ACTION = re.compile(
    r"set\s+the\s+(?P<attr>[\w \-]+?)\s+of\s+(?P<obj>'[^']+'|\"[^\"]+\")\s+to\s+(?P<value>.+)",
    re.IGNORECASE)
_ADD_ACTION = re.compile(
    r"add\s+(?:(?P<sev>error|warning|info|violation)\s+)?(?P<msg>\"[^\"]*\"|'[^']*')"
    r"(?:\s+to\s+(?P<obj>'[^']+'|\"[^\"]+\"))?", re.IGNORECASE)
_SET_LIST = re.compile(r"\{(?P<items>[^}]*)\}")

_SEVERITY_MAP = {
    "error": "hard", "violation": "hard", "warning": "soft",
    "info": "info", None: "hard",
}


def _split_clauses(text: str) -> list[str]:
    """Split a when block into clauses on ';' and leading 'and'/'or'.

    ODM allows 'or' at the top level. Canon supports it too, but mixing 'and'
    and 'or' without brackets is ambiguous in either language, so the importer
    refuses the mixed case rather than picking a precedence for you.
    """
    parts = [p.strip() for p in text.split(";") if p.strip()]
    return parts


def _clause_to_expression(clause: str, verbal: Verbalisation) -> tuple[str, str]:
    """Convert one ODM condition to a Canon expression. Returns (expr, joiner)."""
    joiner = "and"
    working = clause.strip()
    lowered = working.lower()
    if lowered.startswith("and "):
        working = working[4:].strip()
    elif lowered.startswith("or "):
        joiner = "or"
        working = working[3:].strip()

    match = _ATTR_OF.search(working)
    if not match:
        raise MigrationError(
            f"could not find a \"the <attribute> of '<object>'\" phrase in: "
            f"{clause.strip()!r}")
    root = verbal.root_for(match.group("obj"))
    field_name = verbal.field_for(match.group("attr"))
    path = f"{root}.{field_name}"

    remainder = working[match.end():].strip()
    for phrase, operator in _COMPARATORS:
        if remainder.lower().startswith(phrase):
            value_text = remainder[len(phrase):].strip().rstrip(";").strip()
            return _build(path, operator, value_text), joiner

    if remainder.lower() in ("is true", "is present", ""):
        return f"{path} == True", joiner
    if remainder.lower() in ("is false", "is absent"):
        return f"{path} == False", joiner
    raise MigrationError(
        f"unrecognised comparison in: {clause.strip()!r}. Supported phrases "
        f"are {[p for p, _ in _COMPARATORS]}.")


def _build(path: str, operator: str, value_text: str) -> str:
    value = _literal(value_text)
    if operator == "contains":
        return f"{value} in {path}"
    if operator == "not contains":
        return f"{value} not in {path}"
    return f"{path} {operator} {value}"


def _literal(text: str) -> str:
    text = text.strip().rstrip(";").strip()
    set_match = _SET_LIST.search(text)
    if set_match:
        items = [i.strip() for i in set_match.group("items").split(",") if i.strip()]
        return "[" + ", ".join(_literal(i) for i in items) + "]"
    if text.lower() in ("true", "false"):
        return text.capitalize()
    if text.lower() in ("null", "none"):
        return "None"
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return text
    if (text.startswith('"') and text.endswith('"')) or \
            (text.startswith("'") and text.endswith("'")):
        return json.dumps(text[1:-1])
    # A bare word is most often an enum constant in ODM.
    return json.dumps(text)


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def parse_bal(text: str, verbal: Verbalisation, *,
              ruleset_id: str = "imported",
              version: str = "1") -> ImportResult:
    """Parse an ODM BAL export into a Canon ruleset mapping."""
    result = ImportResult(ruleset={
        "ruleset": ruleset_id,
        "version": version,
        "description": "Imported from IBM ODM business action language. "
                       "Every rule below needs a human read before it goes live.",
        "rules": [],
    })

    blocks = list(_RULE_BLOCK.finditer(text))
    if not blocks:
        raise MigrationError(
            "no 'rule <name> { ... }' blocks found. This importer reads the BAL "
            "text export, not the .brl XML. Export from Rule Designer with "
            "'Business Action Language' selected.")

    for block in blocks:
        name = block.group("name")
        body = block.group("body")
        try:
            rule = _convert_block(name, body, verbal)
        except MigrationError as exc:
            result.needs_review.append({
                "rule": name,
                "reason": str(exc),
                "text": " ".join(body.split())[:220],
            })
            continue
        result.ruleset["rules"].append(rule)
        result.converted.append(name)

    if not result.ruleset["rules"]:
        result.warnings.append(
            "nothing converted automatically; check the verbalisation mapping")

    derived = {
        name for rule in result.ruleset["rules"]
        for name in (rule.get("set") or {})
    }
    if derived:
        result.warnings.append(
            f"derived facts {sorted(derived)} were created from ODM 'set' "
            f"actions. Check whether more than one rule writes each of them, "
            f"and declare a combine policy if so.")
    return result


def _convert_block(name: str, body: str, verbal: Verbalisation) -> dict[str, Any]:
    when_match = _WHEN_BLOCK.search(body)
    then_match = _THEN_BLOCK.search(body)
    if then_match is None:
        raise MigrationError("no 'then' block found")

    when_source: str | None = None
    if when_match:
        clauses = _split_clauses(when_match.group("body"))
        converted: list[tuple[str, str]] = [
            _clause_to_expression(clause, verbal) for clause in clauses]
        joiners = {joiner for _, joiner in converted[1:]}
        if len(joiners) > 1:
            raise MigrationError(
                "the condition mixes 'and' with 'or' at the top level; "
                "bracket it by hand so the precedence is explicit")
        joiner = joiners.pop() if joiners else "and"
        when_source = f" {joiner} ".join(expr for expr, _ in converted)

    rule: dict[str, Any] = {
        "id": name,
        "version": "1",
        "title": name.replace("_", " ").replace(".", " ").strip(),
        "tags": ["imported", "odm"],
    }
    priority = _PRIORITY.search(body)
    if priority:
        try:
            rule["priority"] = int(priority.group("value"))
        except ValueError:
            rule["priority"] = 100
    if when_source:
        rule["when"] = when_source

    emitted = False
    sets: dict[str, str] = {}
    for statement in _split_clauses(then_match.group("body")):
        statement = statement.strip()
        if not statement:
            continue
        add = _ADD_ACTION.match(statement)
        if add:
            if emitted:
                raise MigrationError(
                    "the action block raises more than one finding; split it "
                    "into one Canon rule per finding")
            message = add.group("msg")[1:-1]
            code = verbal.codes.get(message) or _code_from_message(message)
            rule["emit"] = {
                "code": code,
                "severity": _SEVERITY_MAP.get(
                    (add.group("sev") or "").lower() or None, "hard"),
                "message": message,
            }
            emitted = True
            continue
        assignment = _SET_ACTION.match(statement)
        if assignment:
            root = verbal.root_for(assignment.group("obj"))
            field_name = verbal.field_for(assignment.group("attr"))
            if root == verbal.objects.get(
                    verbal.result_object.replace("the ", ""), "result"):
                target = field_name
            else:
                target = f"{root}_{field_name}"
            sets[target] = _set_value(assignment.group("value"), verbal)
            continue
        raise MigrationError(f"unrecognised action: {statement!r}")

    if sets:
        rule["set"] = sets
    if not emitted and not sets:
        raise MigrationError("the action block has no effect Canon can express")
    return rule


def _set_value(text: str, verbal: Verbalisation) -> str:
    match = _ATTR_OF.search(text)
    if match:
        root = verbal.root_for(match.group("obj"))
        return f"{root}.{verbal.field_for(match.group('attr'))}"
    return _literal(text)


def _code_from_message(message: str) -> str:
    words = re.sub(r"[^A-Za-z0-9 ]+", " ", message).split()
    return "_".join(word.upper() for word in words[:6]) or "IMPORTED_FINDING"


def import_bal_file(path: str | Path, verbalisation: str | Path | Verbalisation,
                    *, ruleset_id: str | None = None,
                    version: str = "1") -> ImportResult:
    path = Path(path)
    verbal = (verbalisation if isinstance(verbalisation, Verbalisation)
              else Verbalisation.load(verbalisation))
    return parse_bal(path.read_text(encoding="utf-8"), verbal,
                     ruleset_id=ruleset_id or path.stem, version=version)
