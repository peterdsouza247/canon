# ADR 0004: Content hashes, manifests, and a hash chained deploy ledger

## Status

Accepted. Tamper evidence added because regulated environments need it.

## Context

Two of the seven recurring problems are archaeology: which rule caused this, and which
deployment broke it. Neither is answerable while the unit of deployment is a
file and the unit of meaning is a rule. Git tells you which file changed. It
does not tell you that the rule which now misbehaves is the one whose threshold
moved, or which environment received it first.

Then the separate, stronger requirement: the record of what was deployed should
be hard to alter after the fact. In a regulated context "we think this is what
was live in March" is not an answer.

## Decision

Three layers.

1. **Content hash per rule.** SHA-256 over a canonical JSON form of the rule's
   *semantics* only: id, version, guard, emission, sets, declared reads, client
   scope, effective dates, priority. Titles, descriptions, owners and tags are
   excluded.
2. **Manifest per build.** The sorted list of rule hashes plus a Merkle root
   over them. Sorted leaves mean the root does not change when a rule moves
   between files.
3. **Hash chained ledger per environment.** Each deploy record contains the
   previous record's hash, and its own hash covers that link. Optionally each
   entry hash is signed with HMAC-SHA256.

## Consequences

Good:

* `blame(rule_id)` returns only the deployments where that rule's content
  actually changed, with date, environment and person. Question seven, one
  command.
* `live_at(when, env)` answers what was in force on a date.
* Excluding documentation from the hash means deployment diffs stay small enough
  that people keep reading them. `diff_manifests` surfaces `silent_changes`,
  rules whose content moved while their version did not, which is nearly always
  a mistake and is invisible in a normal review of a large YAML file.
* Insertion, deletion, reordering and after the fact editing are all detected
  without a key. Rewriting the chain wholesale is detected with one.
* Decision receipts bind an outcome to a manifest root, so a challenged roster
  can be tied to the exact rules that produced it.

Bad:

* **HMAC is symmetric.** Anybody who can verify can also forge. For an internal
  audit trail this is proportionate; for a regulator facing one it is not, and
  the fix is Ed25519 signatures with the private key in a KMS. The code is
  structured so this is a swap in one function, but it has not been swapped.
* **The chain protects order and content, not existence.** Nothing here stops
  somebody deleting the ledger file. The mitigation is operational: publish the
  head hash somewhere the rules team cannot write.
* Excluding titles from the hash means a rule whose *description* is corrected
  to say the opposite of what the rule does will not show up in a manifest diff.
  Reviewing prose remains a human job.
* Version fields are advisory. Canon can tell you a rule changed without a
  version bump; it cannot stop you.
