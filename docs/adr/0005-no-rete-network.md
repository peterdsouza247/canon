# ADR 0005: No RETE network, no incremental matching

## Status

Accepted, with a stated limit. See [performance.md](../performance.md) for what
to do when the limit starts to bind, and `tools/benchmark.py` for the numbers
that should trigger it.

## Context

ODM, Drools and most of the commercial rules engine lineage use RETE or a
descendant. RETE builds a network of shared subconditions and propagates fact
changes through it, so repeated evaluation over a slowly changing working memory
is cheap. It is the right algorithm for a large ruleset over a long lived
session.

Canon's workload is not that. A crew rostering legality check is:

* stateless by requirement, which is a common constraint in this domain,
* a single transaction against a fresh set of facts,
* a few hundred rules, most of whose guards fail on their first comparison.

RETE's advantage is amortised across repeated evaluation over shared state.
There is no shared state here, by design, and the network construction cost
would be paid on every transaction.

## Decision

Evaluate every applicable rule's guard directly, in stratum order, short
circuiting on the first false term.

## Consequences

Good:

* The engine is roughly two hundred lines and can be read in a sitting. That
  matters more than it sounds for a component whose failures are legal rather
  than merely inconvenient.
* Statelessness is trivial rather than carefully maintained. Two identical calls
  produce byte identical output including the trace, which is what makes shadow
  running and decision receipts meaningful.
* The trace is exact. A network that shares subconditions between rules cannot
  easily say which rule read what; a direct walk can, and does.
* Laziness in the fact layer, which is where the real cost lives, works cleanly.
  A guard that fails early costs one field read and no fetch.

Bad:

* **This does not scale to very large rulesets with heavy shared
  subconditions.** In the tens of thousands, with many rules sharing expensive
  conditions, a matching network wins and Canon would need revisiting. Stated in
  the README rather than buried here.
* Repeated evaluation over the same facts, for example scoring a hundred
  candidate crew members against one flight, recomputes shared conditions each
  time. The mitigation available today is that the fact layer fetches once per
  root per transaction, so the repetition is CPU rather than network. A future
  optimisation would memoise sub-expression results keyed by the input digest,
  which is possible precisely because evaluation is pure. It is not implemented.
* No agenda, no conflict resolution strategy, no `retract`. Rules that want to
  build on one another do so through the declared `derived` namespace and
  nothing else. This is a deliberate reduction in expressive power and some
  ODM patterns have no direct translation.
