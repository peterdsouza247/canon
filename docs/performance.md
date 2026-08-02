# Performance at scale, and the RETE question

> The commercial engines use RETE networks. How does a straightforward
> evaluator compete with that at scale?

The short answer: do not compete at RETE's game, because our workload does not
have RETE's shape. Take the half of RETE that does apply, which is cheaper than
it looks, and then be honest about where the time actually goes.

The longer answer follows, with the measurements you should take before
believing any of it.

---

## What RETE actually buys

RETE earns its keep two ways.

**Sharing.** Many rules test the same conditions. RETE compiles them into a
network so a condition is evaluated once and its result feeds every rule that
needs it. The alpha network discriminates single facts; the beta network joins
across facts and caches partial matches.

**Incrementality.** Working memory changes a little, and the network propagates
only the delta. Re-deciding after a small change is much cheaper than deciding
from scratch.

Now hold that against a crew rostering legality check:

| | RETE's assumption | Our workload |
|---|---|---|
| State | Long lived working memory | Stateless by requirement, fresh facts per transaction |
| Change | Small deltas, re-evaluated often | Whole new transaction each time |
| Joins | Cross products across many fact types | One crew member, one duty, one flight, one roster |
| Amortisation | Network built once, used many times | Nothing to amortise within a transaction |

Incrementality is worth nothing to us: there is no working memory to update, and
statelessness is not an implementation detail we chose, it is problem four on the
brief. The beta network is worth little: our joins are one collection deep, the
roster, and a comprehension over eight crew members is not a cross product.

**Sharing is worth a lot, and it does not require RETE.** The alpha network is,
stripped of ceremony, an index from discriminating conditions to the rules that
care about them. We can build that from the static analysis we already do, at
load time, with no working memory and no network.

---

## Measure first

`tools/benchmark.py` builds synthetic rulesets shaped like a real estate (mostly
threshold checks, some gated on aircraft type, a few consuming derived limits, a
few needing the whole roster) and runs real transactions through the real engine.

```bash
python tools/benchmark.py --rules 100 500 2000 10000 --transactions 300
```

It reports load time, p50/p95/p99 evaluation time, how many rules fired, how many
fields were read, and the cost of trace capture. It also reports, statically, how
many rules a discriminator index would leave to evaluate. **Read that last table
first.** If a ruleset of 2,000 collapses to 40 candidates on the median
transaction, indexing is the whole answer and everything below it is noise.

Do the same on your real ruleset before optimising anything. The synthetic mix is
a guess at your estate; your estate is not a guess.

---

## The ladder, in order of payoff

### 1. A discriminator index. The alpha network, without the network.

Static analysis already hands us every rule's guard as a syntax tree. Extract the
conditions that test a fact path against a literal at the top level of a
conjunction: `flight.aircraft_type == 'A320'`, `crew.rank in ['CP','FO']`,
`flight.destination_category == 'C'`. Build an index at load time from
`(path, value)` to the rules that want it. At request time, look up the facts and
evaluate only the candidates, plus an always-consider set for rules with no
indexable discriminator.

This is exactly where most of RETE's practical benefit comes from on large
rulesets, and it costs a few hundred lines rather than an engine rewrite.

* **Expected win.** Large and highly ruleset dependent. Qualification matrices
  discriminate hard on type ratings and aerodrome categories, which is why they
  are the rules that grow into the thousands. Flight time limitation rules
  discriminate badly, and there are only dozens of them. That asymmetry is
  convenient: the rules that are numerous are the ones that index well.
* **Soundness.** The index over-approximates. A rule with no usable
  discriminator is always considered, and a rule whose discriminator matches is
  always considered. It can never cause a rule to be missed. That is a property
  you can state, not just hope for.
* **How to prove it.** Run the indexed engine and the plain engine over the
  shadow corpus with `WhatIf` and require zero flips. We already built that
  harness for rule changes; it validates engine changes just as well, because
  the engine is deterministic. That is a real advantage over a matching network:
  you can diff the optimisation against the reference on a month of real traffic
  and get an exact answer.

### 2. Per-transaction sub-expression memoisation

Evaluation is pure and side-effect free. `hours_between(duty.start_utc,
duty.end_utc)` appears in four rules in the example ruleset and is recomputed
four times. Cache within a transaction, keyed by the expression's content hash
plus the values of the fact paths it reads, both of which we already have.

* **Expected win.** Modest per transaction, maybe 1.2 to 2x, and larger the more
  the ruleset shares subexpressions.
* **Soundness.** Free, because purity is enforced by the language subset. There
  is no `now()` and no mutable state to invalidate the cache.

### 3. A batch API. This is the one that matters for rostering.

The real question is never "is this assignment legal". It is "which of these 200
crew members can legally take this duty", asked for every open duty in the
month. Today that is 200 separate `evaluate` calls that each re-fetch and
re-derive everything about the flight.

`evaluate_many(shared_facts, varying_facts)` would fetch the shared roots once,
compute every subexpression that depends only on shared facts once, and vary only
what actually varies.

* **Expected win.** Close to the ratio of shared work to total work, which for
  candidate scoring is most of it. This is the largest available win and it is a
  design change rather than a micro-optimisation.
* **Note.** This is the thing RETE gets for free from working memory. We would
  get it by restructuring the call, which also keeps every decision individually
  traceable, which working memory does not.

### 4. Compile expressions to closures instead of walking the tree

Turn each validated syntax tree into a tree of Python closures once at load time.
A closure call is much cheaper than a visitor dispatch plus an `isinstance`
ladder.

* **Expected win.** Typically 3 to 10x on expression evaluation itself.
* **Cost.** It breaks the "one code path evaluates and plans" property that the
  whole design rests on. Mitigation: keep the interpreter as the reference, drive
  static analysis from the syntax tree as now, and add a differential test that
  runs both over the shadow corpus and requires identical traces. Do not do this
  before 1 to 3, and do not do it without the differential test.
* **What not to do.** Generating Python source and `compile()`ing it would be
  faster still and reintroduces the eval-shaped machinery that ADR 0001 rejected.
  Not worth it.

### 5. Reorder guard terms by measured selectivity

`and` short circuits, so the order of terms decides how much work a failing guard
does. Every decision already records which terms were evaluated and what they
returned, so production traces can tell us which term fails most often and which
is cheapest. Reorder at load time.

* **Expected win.** Small but free at run time, and it compounds with indexing.
* **Watch out.** Reordering changes which fields get read, so it changes the
  trace and the "actually read" accounting. It also interacts with null
  propagation: `a and b` where `a` is missing does not necessarily give the same
  answer as `b and a`. Restrict reordering to terms that cannot return null, or
  accept the semantics change deliberately and write it down.

### 6. Parallelism, honestly

Rules in a stratum are provably independent, so a stratum is embarrassingly
parallel. Python's GIL means threads will not help for CPU bound evaluation.
Processes will, for batch scoring, where the work per unit is large enough to pay
for the handoff. Per transaction, parallelism is not the answer; latency at that
scale is dominated by fact fetching, which is IO and already concurrent-friendly.

### 7. Turn off what you are not using

`capture_values=False` records the paths read without their values. The benchmark
reports both so you can see what provenance costs. In a dark launch you want full
capture; in a batch optimiser inner loop you do not.

---

## The strategic point: it is probably not the CPU

Before any of the above, look at the shape of the latency budget for one
decision:

* serialise the payload in the calling application,
* move it across the network,
* deserialise it,
* match and evaluate,
* return the result.

Canon's projection removes most of the first three by not sending fields no rule
reads. If that is a large fraction of the payload, as the demo suggests, the
saving there dwarfs any plausible difference in matching speed. An engine that
matches in 200 microseconds instead of 2 milliseconds has not helped you if it is
still waiting 30 milliseconds for a payload that is forty times bigger than it
needs to be.

So the competitive claim is not "we match faster than RETE". It is:

1. we move far less data, which is where the time is,
2. we can prove any optimisation is behaviour preserving on real traffic, which a
   matching network cannot easily do,
3. and we can take RETE's genuinely useful half, discrimination, without taking
   the rest.

Lead with the measurement, not the architecture.

---

## Where RETE genuinely wins, and we should say so

* **Incrementality at sub-rule granularity.** RETE invalidates partial matches
  inside a rule; Canon invalidates whole rules. See
  [interactive.md](interactive.md): incremental re-evaluation is implemented, the
  invalidation key is each rule's actual read set from the trace, and it is exact
  rather than conservative. What RETE still does better is reusing part of a rule
  when only part of its input moved. Canon rules are small by construction, which
  narrows that gap, but it does not close it.
* **Deep joins.** Rules that correlate many facts, cross product style, are what
  the beta network is for. Our comprehensions are nested loops and will lose
  badly if the rules ever need that shape. They currently do not.
* **Very large rulesets with heavy shared non-discriminating conditions.**
  Indexing handles equality, not expensive shared predicates. If your estate is
  20,000 rules that all compute the same expensive thing three ways, RETE's
  sharing wins and memoisation only partly closes the gap.

If we hit those, the answer is not to build a worse RETE. It is to say so.

## And where neither is the right tool

Building a whole month's roster is an optimisation problem, not a rules problem.
Neither Canon nor ODM should be inside that inner loop; a constraint solver
should, with the rules engine used to validate candidate solutions rather than to
search. Being clear about that boundary is worth more than a benchmark.

---

## If Python is the limit

The rule IR, the projection, the content hashes and the manifests are all
language independent. If, after the ladder, per-transaction cost is still the
binding constraint, the same IR can drive a Rust or Java evaluator with the
Python implementation kept as the reference, and the differential harness proving
the two agree over a month of traffic. That is a real option precisely because
the semantics live in the IR rather than in the interpreter.

That is a long way off, and proposing it before measuring would be the wrong
instinct.

---

## Sequencing

1. Run `tools/benchmark.py` on the real ruleset. Get the baseline and the index
   study. **Do not skip to step 2.**
2. Build the discriminator index. Validate with `WhatIf` at zero flips over the
   shadow corpus.
3. Add the batch API if candidate scoring is on the critical path, which it
   probably is.
4. Memoise subexpressions.
5. Only then consider closure compilation, and only with a differential test.

Every step on that list is measurable and reversible, and every one of them can
be proven behaviour preserving against real traffic before it ships. That
property, rather than raw speed, is the thing worth defending.
