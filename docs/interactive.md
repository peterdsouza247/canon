# Interactive editing, and why not RETE

The application lets a planner make incremental changes and see the effect on
legality immediately. That is RETE's home ground, so this document does two
things: shows how Canon does it, and makes the case for choosing Canon anyway
without pretending the trade is free.

```bash
python tools/interactive_demo.py --verify
```

---

## How it works

A `Session` holds the facts and the last decision. The engine stays stateless:
every call is still a pure function of the facts handed to it.

```python
from canon import Engine, Session, load_yaml

session = Session(Engine(load_yaml("rules/ftl.yaml")), facts,
                  client="AIRLINE_A", as_of="2026-08-14")

delta = session.apply({"duty.end_utc": "2026-08-14T17:45:00Z"})
delta.render()
# now failing  [hard] FTL_FDP_EXCEEDED
#              Flight duty period of 12.5h exceeds the permitted 11h for this duty
#              because duty.end_utc, duty.sectors via FTL-002
#   3 rules re-evaluated, 15 reused (83% avoided), 210us
```

When an edit arrives the session diffs the fact documents, producing a set of
changed paths in the same notation the rules use (`duty.end_utc`,
`flight.roster[*].hours_on_type`). Then every rule whose **actual read set** from
the previous evaluation is untouched replays its previous verdict. Everything
else is re-evaluated, in stratum order. If a derived value moves, its readers are
invalidated before their stratum is reached.

## Why the read set is the right key, and why it is exact

The invalidation key is the set of fact paths each rule *actually read* last
time, not the set it might read. That sounds too aggressive and is not, for a
specific reason.

Evaluation is pure and deterministic. There is no `now()`, no random, no mutable
state, no side effects; the language subset makes those unrepresentable rather
than merely discouraged. So if every value a rule read is unchanged, re-running
it follows an identical control path and reaches an identical conclusion.

The interesting case is short circuiting. A guard `a and b` where `a` was false
never read `b`. If `b` changes and `a` does not, the guard still stops at `a`,
so the verdict cannot have moved and skipping the rule is not merely safe, it is
provably correct. The `test_session.py` suite pins this down with a ten edit
sequence that checks the incremental result against a full evaluation after every
single step.

The pleasing part is that we did not build any of this for incrementality. The
trace exists to answer "which rule did this and what did it look at", which was
the sixth recurring problem. It turns out to be exactly the invalidation structure
an incremental engine needs, so the two cannot drift apart.

---

## Where Canon is worse than RETE here

Say it first.

**Granularity.** RETE invalidates partial matches inside a rule. We invalidate
whole rules. If one rule contains an expensive condition and a cheap one, and
only the cheap one's inputs changed, RETE reuses the expensive part and we do
not.

That matters less than it sounds, because Canon rules are small by construction:
one condition, one outcome, no `else`, no loops, no local variables. The
constraint that makes a rule reviewable is the same constraint that makes rule
level invalidation adequate. But it is a real difference, and on a ruleset
authored as a few dozen enormous rules it would hurt.

**Broad edits.** Changing the effective date, the operator, or a value most rules
read invalidates most of the ruleset, and we fall back to a full evaluation. The
session does that automatically past a threshold, because below it the
bookkeeping costs more than it saves.

**Cross entity cascades.** Editing one duty changes a crew member's cumulative
hours, which affects their other duties. Canon does not model how facts derive
from other facts; the caller has to tell us what changed. That is real work.
It is also exactly the same work in a RETE deployment, where those derived facts
have to be retracted and re-asserted, but it is not work we remove.

---

## Where Canon is better, for this workflow specifically

### 1. It tells the planner what their edit caused, not just what is wrong now

A matching network gives you the new set of violations. The planner already knows
things are wrong; what they need is which of their edits did it.

Every finding in a `Delta` carries the fact paths behind it and the chain of
rules it travelled through. When the duty period rule starts firing, the planner
is told the cause is the sector count, via the rule that reduces the limit,
even though the duty period rule never reads the sector count. That indirection
is precisely the thing a human cannot work out for themselves, and the
dependency graph already knows it.

### 2. Preview without committing

`session.preview(changes)` returns the full delta and changes nothing. That is
the hover state: show the consequence before the planner lets go of the mouse.

There is no working memory to snapshot and no network to unwind, because the
engine never mutated anything. In a RETE deployment the same feature means
asserting facts, reading the agenda, and retracting carefully, and getting the
retraction wrong leaves the network subtly wrong in a way nobody notices until
later.

### 3. Speculative scoring, in parallel, for free

The real planning question is rarely "is this legal". It is "which of these
twelve crew members can take this duty".

```python
session.score({member["id"]: {"crew": crew_record(member)} for member in candidates})
```

Each variant is independent and mutates nothing, so this parallelises with no
interference. One working memory makes it a sequence of assert and retract
cycles.

### 4. The incremental answer is auditable

```python
assert session.verify() == []
```

`verify` re-evaluates from scratch and asserts the incremental result matches.
Run it after every edit in development, on a sample of sessions in production,
and in CI over a recorded edit sequence.

A stale matching network is a class of bug you cannot audit away. Here the
incremental path and the full path are the same code over a deterministic
evaluator, so equivalence is checkable at any moment, cheaply, on real data. For
a system that decides whether somebody may legally operate an aircraft, being
able to prove the fast path agrees with the slow path is worth more than the
speed.

### 5. Every intermediate state is explainable and receiptable

The session keeps a delta per edit. A published roster can be traced back through
the edits that produced it, each with a decision receipt binding it to the exact
ruleset in force. RETE has no memory of how it reached the current state.

### 6. The edit loop moves far less data

Interactive editing is the workload where payload size hurts most, because it
happens on every drag rather than once per batch. The generated projection means
the session ships only the fields the rules read, and after the first evaluation
an edit ships only the diff.

---

## What that costs, measured

`tools/interactive_demo.py` runs eight edits against the example ruleset and
reports the fraction of rule evaluations skipped and the per edit latency. Run it
on your own ruleset and edit traces before believing any of this. The number that
decides it is **work avoided**: how many rule evaluations an average edit skips.

If work avoided is high, rule level invalidation is enough and the rest of this
argument stands. If it is low, your rules read a small number of very widely used
facts, and the honest answer is that you want either finer granularity or a
different design.

---

## How to run the bake-off

If someone is choosing between this and a RETE engine for an editing workflow,
these are the measurements that decide it, in order.

1. **End to end edit latency**, from keystroke to updated panel, on a real
   ruleset and a real payload. Not matching time. The payload difference is
   likely to dominate and neither vendor's benchmark will show it.
2. **Work avoided per edit**, from a recorded session of real planner actions.
   Ask both engines what fraction of work an average edit skips.
3. **Can you prove the incremental answer is right?** Ask for the equivalent of
   `session.verify()`. This is the question most likely to separate the two, and
   it is the one an airline's safety case actually turns on.
4. **What does the planner see when something breaks?** Ask each engine to
   produce the cause of a violation, attributed to the edit and the rule chain,
   not just the violation.
5. **What happens when a rule changes?** Ask which deployment introduced the
   current behaviour of one named rule. `canon ledger blame` answers in one
   command.

Points 3, 4 and 5 are not performance questions, and they are where the decision
should be made. Performance is the entry ticket; provenance is the product.

---

## API

| | |
|---|---|
| `Session(engine, facts, key=, client=, as_of=)` | Open a session. Evaluates once. |
| `session.apply(changes)` | Commit an edit, return a `Delta`. |
| `session.preview(changes)` | Same, without committing. |
| `session.score({name: changes})` | Preview several candidates. |
| `session.verify()` | Check the incremental result against a full one. |
| `session.stats()` | Work avoided, p50 and p95 per edit. |
| `session.decision` | The current `Decision`, with its full trace. |

`changes` accepts dotted paths (`duty.end_utc`, `flight.roster.1.rank`) or top
level subtrees to merge. Both forms occur in a real interface: a form field sends
a path, a drag and drop sends a subtree.
