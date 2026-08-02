# ADR 0002: Rule to rule dependencies must be declared, and are refused if cyclic

## Status

Accepted.

## Context

The brief describes rules that depend on other rules, authored by people who are
not in step with the people building the integration layer. The failure mode is
familiar: evaluation order becomes load bearing, nobody knows which orderings
are safe, and a reordering that looks cosmetic changes an outcome months later.

There were three options.

1. **Forbid rule to rule dependencies entirely.** Purest. Also unusable: flight
   time limitation rules genuinely derive a limit and then test against it, and
   forcing every consumer to recompute the derivation duplicates the hardest
   arithmetic in the ruleset.
2. **Allow them implicitly**, with the engine inferring order. Convenient. The
   coupling stays invisible in the source, which is the current problem.
3. **Allow them, require declaration, verify the declaration against the syntax
   tree, and refuse cycles.**

## Decision

Option three, with both directions enforced.

* A rule that reads `derived.x` must list it under `reads`. Reading without
  declaring is a load error.
* A rule that declares `derived.x` and does not read it is *also* a load error.
  Stale declarations make the graph lie, and a graph that lies is worse than no
  graph.
* The dependency graph is topologically sorted into strata. Rules in a stratum
  are independent by construction.
* A cycle refuses to load, naming the rules involved.

## Consequences

Good:

* "Rules run in isolation" becomes a property the loader proves rather than a
  convention people remember. Nothing in a stratum can observe anything else in
  that stratum.
* Intra stratum ordering is irrelevant, so a stratum is parallelisable and
  reordering a file is safe.
* The coupling is visible in the source that a reviewer reads.
* `Decision.explain` can walk the dependency chain backwards, because the chain
  is real data.

Bad:

* More ceremony per rule. An author who adds a term to a condition may now also
  have to add a line to `reads`, and will get a load error if they forget.
  This is deliberate friction and it will be complained about.
* The Python front end's `auto_reads=True` weakens the visibility half of the
  benefit, since the declaration no longer appears in the source. It is the
  default because it is what people expect from Python, and
  `docs/authoring-comparison.md` recommends turning it off. That tension is
  unresolved and stated rather than hidden.
* A genuinely cyclic requirement has no expression in Canon. If one appears, it
  is a modelling problem, but the author will experience it as the tool being
  obstructive.
