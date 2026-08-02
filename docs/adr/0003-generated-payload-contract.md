# ADR 0003: The payload contract is generated from the rules, never written by hand

## Status

Accepted.

## Context

Problems one, two and three on the list are all the same problem wearing
different clothes: a human decides what data a decision needs, that decision
lives somewhere other than the rules, and it drifts.

Once it has drifted, both directions hurt. Fields nobody reads keep being sent,
because removing one is risky and nobody can prove it is unread. Fields somebody
needs are missing, so a rule gets rewritten around what happens to be available,
which is where the messy workarounds for vertical slices come from.

## Decision

The set of fields is not an input. It is an output.

`Projection` is computed by running every rule's expressions through the
evaluator with a resolver that returns `UNKNOWN` for everything. The resulting
set of fact paths is the payload contract. It is generated per ruleset, per
client, and per effective date.

Collection traversal is part of the path grammar. `flight.roster[*].rank` is a
path, so a vertical slice is expressible in the contract and batchable by the
resolver.

Static analysis deliberately does **not** short circuit, so the contract covers
every branch. Evaluation does short circuit, so the actual read set at run time
is usually much smaller. Both numbers are reported on every decision.

## Consequences

Good:

* The contract cannot drift, because there is nowhere for it to drift to.
* It is a diffable artefact. Regenerate it in CI and a change to the integration
  surface shows up in review.
* Per client contracts fall out for free, so a small tenant does not pay for a
  large tenant's fields.
* It is deliverable before any engine migration. Phase two of the migration
  playbook shrinks the payload while the existing engine is still deciding
  everything, which is what funds the rest of the work.

Bad:

* Dynamic fact access defeats it. If a rule reaches a path that static analysis
  did not predict, the contract handed to the caller was wrong. Canon raises
  `UnplannedFactError` in strict mode rather than continuing quietly. That is
  the correct behaviour and it is also a hard failure in production if a rule
  slips through, so strict mode belongs in CI and the decision about production
  is the operator's.
* The contract is a worst case. A ruleset with many rarely applicable rules will
  advertise a larger surface than it typically uses. The `unread_paths` figure on
  each decision exists to make that visible, and per client scoping is the lever
  for reducing it.
* The calling application has to be willing to consume a generated field mask.
  That is a real integration change, and it is the one piece of this that cannot
  be done inside the rules team.
