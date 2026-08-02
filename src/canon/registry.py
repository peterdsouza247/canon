"""Rule identity, deployment manifests, and tamper evident history.

The seventh recurring problem is that it is hard to identify which deployment
broke a rule. That question is unanswerable as long as the unit of deployment is
a file and the unit of meaning is a rule. Canon gives every rule a
content hash, records the exact set of hashes that went out with each release,
and chains those records together.

Once that exists, three previously hard questions become lookups:

* which version of rule X was live on 3 March
* which deployment changed rule X
* was this decision produced by the ruleset we think it was

The chain is a hash linked append only log with an optional HMAC over each
entry. That is deliberately modest: it detects tampering by anyone who does not
hold the signing key, and it detects accidental corruption by anyone at all. It
is not a distributed ledger and does not pretend to be. For a regulated audit
trail the signing key lives in an HSM or KMS and the chain head is published
somewhere the rules team cannot rewrite.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .errors import TamperError
from .rules import RuleSet, canonical_json, content_hash
from .trace import Decision

__all__ = [
    "merkle_root", "RuleEntry", "Manifest", "DeployRecord", "DeployLedger",
    "diff_manifests", "issue_receipt", "verify_receipt",
]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def merkle_root(hashes: Sequence[str]) -> str:
    """Root of a binary Merkle tree over sorted leaf hashes.

    Sorting makes the root independent of file layout, so moving a rule between
    files does not change the root. An odd node is paired with itself, which is
    the common convention and is safe here because the leaves are sorted and
    de-duplicated by rule id upstream.
    """
    if not hashes:
        return _sha("canon:empty")
    layer = [_sha("leaf:" + h) for h in sorted(hashes)]
    while len(layer) > 1:
        nxt: list[str] = []
        for index in range(0, len(layer), 2):
            left = layer[index]
            right = layer[index + 1] if index + 1 < len(layer) else left
            nxt.append(_sha("node:" + left + right))
        layer = nxt
    return layer[0]


@dataclass(frozen=True)
class RuleEntry:
    rule_id: str
    version: str
    hash: str
    title: str = ""
    clients: tuple[str, ...] = ("*",)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "version": self.version,
            "hash": self.hash,
            "title": self.title,
            "clients": list(self.clients),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RuleEntry":
        return cls(
            rule_id=raw["rule_id"],
            version=raw["version"],
            hash=raw["hash"],
            title=raw.get("title", ""),
            clients=tuple(raw.get("clients", ("*",))),
        )


@dataclass
class Manifest:
    """The exact contents of one ruleset build."""

    ruleset_id: str
    ruleset_version: str
    entries: list[RuleEntry] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    source_revision: str = ""
    notes: str = ""

    @classmethod
    def of(cls, ruleset: RuleSet, *, source_revision: str = "",
           notes: str = "", created_at: str | None = None) -> "Manifest":
        entries = [
            RuleEntry(rule_id=rule.id, version=rule.version,
                      hash=rule.content_hash, title=rule.title,
                      clients=rule.clients)
            for rule in sorted(ruleset.rules, key=lambda r: r.id)
        ]
        return cls(
            ruleset_id=ruleset.id,
            ruleset_version=ruleset.version,
            entries=entries,
            created_at=created_at or _now(),
            source_revision=source_revision,
            notes=notes,
        )

    @property
    def root(self) -> str:
        return merkle_root([entry.hash for entry in self.entries])

    def hash_of(self, rule_id: str) -> str | None:
        for entry in self.entries:
            if entry.rule_id == rule_id:
                return entry.hash
        return None

    def by_id(self) -> dict[str, RuleEntry]:
        return {entry.rule_id: entry for entry in self.entries}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ruleset_id": self.ruleset_id,
            "ruleset_version": self.ruleset_version,
            "created_at": self.created_at,
            "source_revision": self.source_revision,
            "notes": self.notes,
            "merkle_root": self.root,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Manifest":
        manifest = cls(
            ruleset_id=raw["ruleset_id"],
            ruleset_version=raw["ruleset_version"],
            entries=[RuleEntry.from_dict(e) for e in raw.get("entries", [])],
            created_at=raw.get("created_at", ""),
            source_revision=raw.get("source_revision", ""),
            notes=raw.get("notes", ""),
        )
        declared = raw.get("merkle_root")
        if declared and declared != manifest.root:
            raise TamperError(
                f"manifest for {manifest.ruleset_id} declares root "
                f"{declared[:12]} but its entries hash to {manifest.root[:12]}")
        return manifest

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2),
                              encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Manifest":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def diff_manifests(before: Manifest, after: Manifest) -> dict[str, Any]:
    """What changed between two builds, at rule granularity."""
    old = before.by_id()
    new = after.by_id()
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = []
    for rule_id in sorted(set(old) & set(new)):
        if old[rule_id].hash != new[rule_id].hash:
            changed.append({
                "rule_id": rule_id,
                "from_version": old[rule_id].version,
                "to_version": new[rule_id].version,
                "from_hash": old[rule_id].hash[:12],
                "to_hash": new[rule_id].hash[:12],
                "version_bumped": old[rule_id].version != new[rule_id].version,
            })
    return {
        "from": {"version": before.ruleset_version, "root": before.root[:12]},
        "to": {"version": after.ruleset_version, "root": after.root[:12]},
        "added": [new[r].to_dict() for r in added],
        "removed": [old[r].to_dict() for r in removed],
        "changed": changed,
        "unchanged": len(set(old) & set(new)) - len(changed),
        "silent_changes": [c for c in changed if not c["version_bumped"]],
    }


# --------------------------------------------------------------------------
# Deployment ledger
# --------------------------------------------------------------------------


@dataclass
class DeployRecord:
    seq: int
    environment: str
    manifest: Manifest
    deployed_at: str = field(default_factory=_now)
    deployed_by: str = ""
    client: str = "*"
    prev_hash: str = ""
    entry_hash: str = ""
    signature: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "environment": self.environment,
            "client": self.client,
            "deployed_at": self.deployed_at,
            "deployed_by": self.deployed_by,
            "prev_hash": self.prev_hash,
            "manifest": self.manifest.to_dict(),
        }

    def compute_hash(self) -> str:
        return content_hash(self.payload())

    def to_dict(self) -> dict[str, Any]:
        out = self.payload()
        out["entry_hash"] = self.entry_hash
        out["signature"] = self.signature
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DeployRecord":
        return cls(
            seq=raw["seq"],
            environment=raw["environment"],
            manifest=Manifest.from_dict(raw["manifest"]),
            deployed_at=raw.get("deployed_at", ""),
            deployed_by=raw.get("deployed_by", ""),
            client=raw.get("client", "*"),
            prev_hash=raw.get("prev_hash", ""),
            entry_hash=raw.get("entry_hash", ""),
            signature=raw.get("signature", ""),
        )


class DeployLedger:
    """Append only, hash chained record of what went where and when."""

    GENESIS = "0" * 64

    def __init__(self, records: Iterable[DeployRecord] = (),
                 secret: bytes | None = None) -> None:
        self.records: list[DeployRecord] = list(records)
        self.secret = secret

    # -- writing ----------------------------------------------------------

    def append(self, manifest: Manifest, *, environment: str,
               deployed_by: str = "", client: str = "*",
               deployed_at: str | None = None) -> DeployRecord:
        prev = self.records[-1].entry_hash if self.records else self.GENESIS
        record = DeployRecord(
            seq=len(self.records) + 1,
            environment=environment,
            manifest=manifest,
            deployed_at=deployed_at or _now(),
            deployed_by=deployed_by,
            client=client,
            prev_hash=prev,
        )
        record.entry_hash = record.compute_hash()
        if self.secret is not None:
            record.signature = hmac.new(
                self.secret, record.entry_hash.encode("utf-8"),
                hashlib.sha256).hexdigest()
        self.records.append(record)
        return record

    # -- verification -----------------------------------------------------

    def verify(self) -> list[str]:
        """Return a list of problems. An empty list means the chain is intact."""
        problems: list[str] = []
        expected_prev = self.GENESIS
        for index, record in enumerate(self.records):
            if record.seq != index + 1:
                problems.append(
                    f"entry {index + 1}: sequence number is {record.seq}")
            if record.prev_hash != expected_prev:
                problems.append(
                    f"entry {record.seq}: prev_hash {record.prev_hash[:12]} "
                    f"does not match the previous entry {expected_prev[:12]}. "
                    f"An entry has been inserted, removed or reordered.")
            recomputed = record.compute_hash()
            if record.entry_hash != recomputed:
                problems.append(
                    f"entry {record.seq}: contents were edited after the fact "
                    f"(stored {record.entry_hash[:12]}, "
                    f"recomputed {recomputed[:12]})")
            if self.secret is not None:
                expected_sig = hmac.new(
                    self.secret, record.entry_hash.encode("utf-8"),
                    hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected_sig, record.signature or ""):
                    problems.append(
                        f"entry {record.seq}: signature does not verify")
            expected_prev = record.entry_hash
        return problems

    def assert_intact(self) -> None:
        problems = self.verify()
        if problems:
            raise TamperError("deploy ledger failed verification:\n  " +
                              "\n  ".join(problems))

    # -- questions people actually ask ------------------------------------

    def history(self, environment: str | None = None) -> list[DeployRecord]:
        return [r for r in self.records
                if environment is None or r.environment == environment]

    def blame(self, rule_id: str, environment: str | None = None) -> list[dict[str, Any]]:
        """Every deployment in which this rule's content changed.

        This is the answer to "in which deployment did rule X break". Run it,
        take the last entry before the bug reports started, and you have the
        release, the timestamp, the person, and the exact hash that went out.
        """
        out: list[dict[str, Any]] = []
        previous: str | None = None
        seen_before = False
        for record in self.history(environment):
            current = record.manifest.hash_of(rule_id)
            if current is None:
                if seen_before:
                    out.append({
                        "seq": record.seq,
                        "environment": record.environment,
                        "deployed_at": record.deployed_at,
                        "deployed_by": record.deployed_by,
                        "change": "removed",
                        "from_hash": (previous or "")[:12],
                        "to_hash": None,
                        "ruleset_version": record.manifest.ruleset_version,
                    })
                    seen_before = False
                    previous = None
                continue
            if current != previous:
                out.append({
                    "seq": record.seq,
                    "environment": record.environment,
                    "deployed_at": record.deployed_at,
                    "deployed_by": record.deployed_by,
                    "change": "added" if previous is None else "modified",
                    "from_hash": (previous or "")[:12] or None,
                    "to_hash": current[:12],
                    "version": (record.manifest.by_id()[rule_id].version),
                    "ruleset_version": record.manifest.ruleset_version,
                })
                previous = current
            seen_before = True
        return out

    def live_at(self, when: str, environment: str) -> Manifest | None:
        """Which manifest was live in an environment at a given instant."""
        chosen: Manifest | None = None
        for record in self.history(environment):
            if record.deployed_at <= when:
                chosen = record.manifest
            else:
                break
        return chosen

    def find_by_root(self, root: str) -> list[DeployRecord]:
        return [r for r in self.records if r.manifest.root.startswith(root)]

    # -- persistence ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "canon.deploy-ledger/1",
            "head": self.records[-1].entry_hash if self.records else self.GENESIS,
            "records": [r.to_dict() for r in self.records],
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2),
                              encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path, secret: bytes | None = None) -> "DeployLedger":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        ledger = cls([DeployRecord.from_dict(r) for r in raw.get("records", [])],
                     secret=secret)
        declared_head = raw.get("head")
        actual_head = (ledger.records[-1].entry_hash
                       if ledger.records else cls.GENESIS)
        if declared_head and declared_head != actual_head:
            raise TamperError(
                f"ledger head {declared_head[:12]} does not match the last "
                f"entry {actual_head[:12]}")
        return ledger


# --------------------------------------------------------------------------
# Decision receipts
# --------------------------------------------------------------------------


def issue_receipt(decision: Decision, manifest: Manifest,
                  secret: bytes | None = None,
                  issued_at: str | None = None) -> dict[str, Any]:
    """Bind a decision to the exact ruleset that produced it.

    A roster published in March and challenged in September is only defensible
    if you can show which rules were in force and what data they saw. The
    receipt stores digests rather than the data itself, so it is small enough to
    keep forever and carries no personal data on its own.
    """
    body = {
        "format": "canon.receipt/1",
        "issued_at": issued_at or _now(),
        "ruleset_id": decision.ruleset_id,
        "ruleset_version": decision.ruleset_version,
        "manifest_root": manifest.root,
        "key": dict(decision.key),
        "client": decision.client,
        "as_of": decision.as_of.isoformat() if decision.as_of else None,
        "input_digest": decision.input_digest,
        "output_digest": decision.output_digest,
        "rules_fired": decision.rules_fired(),
        "verdict": "pass" if decision.ok else "fail",
    }
    body["receipt_hash"] = content_hash(body)
    if secret is not None:
        body["signature"] = hmac.new(
            secret, body["receipt_hash"].encode("utf-8"),
            hashlib.sha256).hexdigest()
    return body


def verify_receipt(receipt: Mapping[str, Any], *,
                   decision: Decision | None = None,
                   manifest: Manifest | None = None,
                   secret: bytes | None = None) -> list[str]:
    """Check a receipt. Returns a list of problems; empty means it holds."""
    problems: list[str] = []
    body = {k: v for k, v in receipt.items()
            if k not in ("receipt_hash", "signature")}
    recomputed = content_hash(body)
    if receipt.get("receipt_hash") != recomputed:
        problems.append("receipt body does not match its hash")
    if secret is not None:
        expected = hmac.new(secret, str(receipt.get("receipt_hash", "")).encode(),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, str(receipt.get("signature", ""))):
            problems.append("receipt signature does not verify")
    if manifest is not None and receipt.get("manifest_root") != manifest.root:
        problems.append(
            f"receipt names manifest root "
            f"{str(receipt.get('manifest_root'))[:12]} but the manifest "
            f"supplied hashes to {manifest.root[:12]}")
    if decision is not None:
        if receipt.get("input_digest") != decision.input_digest:
            problems.append("replayed inputs differ from the recorded inputs")
        if receipt.get("output_digest") != decision.output_digest:
            problems.append(
                "replaying the decision produced a different result, so either "
                "the ruleset changed or the engine is not deterministic")
    return problems
