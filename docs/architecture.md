# Architecture

## The one idea

Canon has a single structural idea, and everything else is a consequence of it:

> The code that evaluates a rule is the same code that tells you what data the
> rule needs.

`expr.py` contains a small interpreter that walks a validated syntax tree. It
never reads a fact directly; it asks a `Resolver`. Give it a resolver backed by
real data and it evaluates. Give it a `StaticResolver`, which returns a sentinel
called `UNKNOWN` for every path and one symbolic element for every collection,
and the identical walk records the fact paths the expression could read, with no
data present at all.

Everything follows.

* The payload contract is `Projection`, built from those paths. Nobody writes it.
* It cannot drift from the rules, because it is computed from the rules.
* A vertical slice is not a special case. `flight.roster[*].rank` is a path like
  any other, and the resolver knows to fetch it as one batched collection read.
* The trace is the same recording, taken during a real evaluation, so "what the
  rule looked at" is measured rather than reconstructed.

## Layers

```
                    load time                          request time
                    ---------                          ------------
  YAML ─┐
  Python├─► Rule IR ─► RuleSet validation ─► Projection ─► FactStore ─► Engine ─► Decision
  Table ─┘             cycles, conflicts,     (contract)   (lazy,        (pure)    (+ trace,
                       undeclared reads,                    batched)               receipt)
                       strata
```

Load time is where the expensive work happens: parsing, validating, static
analysis, graph construction. It happens once per process. Request time is a
walk over frozen data.

### `expr.py` — the restricted language

A subset of the Python grammar, enforced by an allow list of AST node types
checked at compile time. Rejected outright: imports, lambdas, walrus, f-strings,
starred arguments, keyword arguments, comprehensions with more than one `for`,
any attribute or name beginning with an underscore.

Two details are load bearing.

**`UNKNOWN` propagates.** Every operator returns `UNKNOWN` if an operand is
`UNKNOWN`. That is what lets a static walk reach every branch without type
errors.

**Static analysis does not short circuit; evaluation does.** During planning,
both sides of an `and` are visited, so the contract covers every branch the
engine could take. At run time the first false term ends the guard, so a rule
whose first condition fails costs one field read and no fetch of anything else.
The projection is therefore the worst case and the actual read set is usually
much smaller. Both numbers appear on the decision.

**Collections use comprehension syntax.** `any(m.rank == 'CP' for m in
flight.roster)` is ordinary Python that a rule author can read, and it is
simultaneously a machine readable statement that this rule needs the `rank` of
every crew member on the flight and nothing else about them. That is problem
three solved by notation rather than by workaround.

### `facts.py` — projections and lazy resolution

A `Projection` is a tree of fact paths with collection nodes marked. It
serialises to JSON, so it is the artefact you hand the Java team, and it diffs
cleanly, so a change to the contract is visible in review.

`FactStore` fetches at most once per root per transaction, on first touch. A
`FactSource` receives a `FactRequest` carrying the exact leaf paths under its
root, so a SQL source can build a narrow projection and an HTTP source can send
a field mask. Roots that no rule ended up touching are never fetched at all.

`Projection.select(document)` trims a full payload down to the projected shape.
That is how you measure, on real traffic, how much of today's payload is dead
weight, before changing anything.

### `rules.py` — the IR and the validator

A `Rule` is a guard, an optional emission, and an optional set of derived
values. Rules communicate through one namespace, `derived`, and only by
declaration.

`RuleSet` construction refuses to complete if:

* a rule reads a derived fact it did not declare in `reads`,
* a rule declares a read it does not use,
* a derived fact is read but nothing produces it,
* two rules write the same derived fact with no combine policy,
* the dependency graph contains a cycle.

Rules are then sorted into **strata** by Kahn's algorithm. Every rule in a
stratum is independent of every other rule in that stratum, so intra stratum
ordering cannot change an outcome, and a stratum is trivially parallelisable.
This is the concrete answer to "transactions should be stateless and rules
should run in isolation": isolation is a property the loader proves, not a
convention the team maintains.

**Combine policies** deserve a note. In flight time limitations several
regulations cap the same quantity and the most restrictive wins. Expressing that
as `min` once, in the ruleset header, is better than encoding it in evaluation
order where a reordering silently changes the answer.

**Content hashing** covers semantics only: id, version, guard, emission, sets,
declared reads, client scope, effective dates, priority. Titles, descriptions,
owners and tags are excluded, so editing a comment does not churn every
deployment diff and reviewers keep reading them.

### `engine.py` — evaluation

A pure function. Filter by client and date, compute the projection, build the
store, walk the strata, combine derived values at each stratum boundary,
assemble the decision.

Each rule gets a `_ScopedResolver`: a per rule view that records reads against
the rule for the trace and against the store for the payload accounting, while
fetching through the shared store. There is no shared mutable context for a rule
to reach into, which is why isolation holds at run time and not only on paper.

Rule errors are recorded on the trace and collected on the decision rather than
aborting the transaction, because one broken rule should not take a roster build
down. `on_rule_error="raise"` is available for tests.

### `trace.py` — provenance

`RuleTrace` records, per rule: whether it was considered and why not if it was
not, the guard source and its result, every path read with its value, what was
emitted, what was set, any error, and elapsed microseconds.

`Decision.explain(code)` finds the rule that emitted a finding and then walks
backwards through the derived values it read to the rules that produced them.
`Decision.why(name)` lists every contributor to a derived value.

`Decision.input_digest` hashes exactly the data the rules read, not the whole
payload. Two requests that differ only in fields nothing looked at have the same
digest, which makes result caching sound rather than approximate.

### `registry.py` — identity over time

Rule content hash → manifest of hashes with a Merkle root → hash chained deploy
ledger. Sorted leaves mean the root is independent of file layout, so moving a
rule between files does not churn the root.

`DeployLedger.blame(rule_id)` walks the chain and returns only the deployments
in which that rule's hash actually changed. `live_at(when, env)` answers what was
in force on a date. `verify()` detects insertion, deletion, reordering and
after the fact editing; with a signing key it also detects a rewrite by anyone
who does not hold the key.

`issue_receipt` binds `(input digest, output digest, manifest root)` and signs
it. A roster published in March and challenged in September is defensible if you
can show which rules were in force and what they saw. The receipt carries
digests rather than data, so it is small enough to keep indefinitely and holds
no personal data by itself.

### `shadow.py` — the migration argument

Runs Canon beside the incumbent on captured traffic and classifies each case as
match, missing, extra, both differ, or error. Beyond the diff it attributes
divergence to rules by lift: how much more often a rule fires on diverging cases
than on agreeing ones. A rule that fires on most failures and almost no
successes is the suspect, and in a ruleset of hundreds that ranking is the
difference between an afternoon and a fortnight.

## Performance shape

Canon is a straightforward per rule evaluator. There is no RETE network and no
incremental matching.

The relevant costs for a per assignment legality check:

* **Parsing and validation**: load time only, once per process.
* **Guard evaluation**: linear in applicable rules, but most guards fail on
  their first term, which is a single field read.
* **Fact fetching**: at most one call per root per transaction, and only for
  roots something actually touched. This is where the win against a large
  payload lives, and it is a network effect rather than a CPU one.
* **Trace capture**: proportional to reads. Set `capture_values=False` to record
  path names without values when running at volume.

For a ruleset in the tens of thousands with heavily shared subconditions, a
matching network would beat this. That is a real limit and it is stated in the
README rather than buried here.

## What would need building for production

Honest list.

* A service wrapper. Canon is a library; a gRPC or HTTP sidecar next to the Java
  application is a small piece of work but it is not written.
* Real fact sources against the crew and schedule systems, with connection
  pooling and timeouts.
* A rule authoring interface for non engineers, most likely over the decision
  table front end.
* Key management for signing, and somewhere to publish the ledger head.
* Load testing against production shaped payloads. The claims in this repository
  about payload reduction are structural, and they should be measured on your
  traffic with `Projection.select` before anyone quotes a number.
