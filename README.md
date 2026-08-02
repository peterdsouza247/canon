# Canon

A stateless rules engine for crew rostering legality, written in Python.

Live demo: **https://peterdsouza247.github.io/canon/**
(the demo runs the engine in your browser, no server involved)

> **Independent project.** Canon is not affiliated with, endorsed by, or derived
> from any employer's systems. It was written from public sources: published
> flight time limitation regulation, vendor documentation, and the general
> literature on business rules management systems. Every rule, fact document,
> operator, flight number, aircraft and person in this repository is invented.
> The numbers are shaped like real regulation and are not a copy of it, and
> nothing here should be flown on.

> **Licence.** [Business Source License 1.1](LICENSE). Source available, not
> open source. Free to read, fork, modify, test, benchmark and shadow run
> against your current engine on real production traffic. A commercial licence
> is required to use its output to make, approve or record a real decision, or
> to ship it inside something you sell. Becomes Apache-2.0 on 2 August 2030.
> Plain English version and how to buy: [LICENSING.md](LICENSING.md).

---

## Why

Long lived business rules deployments tend to accumulate the same set of
problems. They are well documented in the BRMS literature and anyone who has
maintained one for a decade will recognise them. Canon is built around that list
rather than around a general theory of rules engines.

| # | The recurring problem | What Canon does about it |
|---|---|---|
| 1 | Payloads are large and growing, and it shows in the latency | The payload is a **projection derived by static analysis of the rules**, so it contains what the rules read and nothing else |
| 2 | The design makes it hard to send only what is needed | Nobody maintains the field list. It is generated, and it cannot drift from the rules because it is computed from them |
| 3 | Data arrives as horizontal slices; some rules need vertical ones | Collection traversal is first class. `any(m.rank == 'CP' for m in flight.roster)` yields the fact path `flight.roster[*].rank`, which the resolver batches |
| 4 | Rules depend on other rules; authors and integrators drift apart | Rule to rule coupling must be **declared**, is checked against the syntax tree, is refused if it forms a cycle, and is sorted into strata that provably cannot see one another |
| 5 | Commercial rules engines are expensive to licence | Canon is a Python library with no runtime dependencies outside the standard library |
| 6 | Finding the rule behind a bug is hard | Every decision carries a trace: which rules were considered, why each was or was not, what each read, and what each concluded. `decision.explain(code)` walks it backwards |
| 7 | Finding the deployment that broke a rule is hard | Every rule has a content hash. Every release is a manifest of hashes. Every deployment is an entry in a hash chained ledger. `canon ledger blame --rule FTL-010` prints the release, the date and the person |

And one more that regulated environments need: **tamper evidence**. Deploy
records are hash chained and optionally signed, and every decision can be issued
a receipt binding the outcome to the exact ruleset that produced it.

---

## The idea in one screen

```python
from canon import load_yaml, Engine

ruleset = load_yaml("examples/rules/ftl.yaml")
engine  = Engine(ruleset)

# What does this ruleset need on the wire, for this client, on this date?
engine.plan(client="AIRLINE_A", as_of=date(2026, 8, 14))["paths"]
# ['crew.duty_hours_last_7d', 'crew.hours_last_28d', ...,
#  'flight.roster[*].hours_on_type', 'flight.roster[*].rank', ...]

decision = engine.evaluate(facts, client="AIRLINE_A", as_of="2026-08-14")

decision.ok                      # False
decision.codes()                 # ['FTL_FDP_EXCEEDED', 'INEXPERIENCED_PILOT_PAIRING', ...]
decision.derived["max_fdp_hours"]  # 11.0
decision.explain("FTL_FDP_EXCEEDED")
# [FTL-010, then the rules that produced the limit it compared against]
```

The list of fact paths is not written by hand anywhere. It is the output of
walking the rules' syntax trees with a resolver that returns `UNKNOWN` for
everything. The same walk, with a real resolver, is the evaluator. One code
path, so the contract and the behaviour cannot disagree.

---

## Install and run

Python 3.10 or newer. PyYAML only if you want to author rules in YAML.

```bash
git clone https://github.com/peterdsouza247/canon.git
cd canon
pip install -e ".[yaml]"

canon validate examples/rules/ftl.yaml
canon plan     examples/rules/ftl.yaml --client AIRLINE_A
canon run      examples/rules/ftl.yaml --facts examples/data/fdp_breach.json --trace
canon explain  examples/rules/ftl.yaml --facts examples/data/fdp_breach.json \
               --code FTL_FDP_EXCEEDED
```

Governance:

```bash
canon manifest examples/rules/ftl.yaml --out build/manifest.json --revision "$(git rev-parse HEAD)"
canon deploy   build/manifest.json --env prod --by "$USER" --secret "$CANON_KEY"
canon ledger   verify --secret "$CANON_KEY"
canon ledger   blame --rule FTL-010
canon diff     build/manifest-2026-07.json build/manifest-2026-08.json
```

Migration:

```bash
python tools/make_shadow_cases.py -n 2000
canon shadow      examples/rules --cases examples/shadow/cases.jsonl --threshold 0.995
canon import-odm  examples/odm/legacy_ftl.bal \
                  --verbalisation examples/odm/verbalisation.json --out imported.yaml
```

Interactive editing. A planner drags a duty and the legality panel keeps up,
because only the rules that read something that changed are re-evaluated:

```python
from canon import Engine, Session, load_yaml

session = Session(Engine(load_yaml("rules/ftl.yaml")), facts, client="AIRLINE_A")
delta = session.apply({"duty.end_utc": "2026-08-14T17:45:00Z"})

delta.newly_raised[0]["because"]   # {'paths': ['duty.sectors', 'duty.end_utc'],
                                   #  'via_rules': ['FTL-002']}
session.preview({"crew": other_crew_member})   # commits nothing
session.verify()                               # [] means incremental == full
```

The invalidation key is each rule's *actual read set* from its trace, which
makes it exact rather than conservative, and it came free with the provenance
machinery. `python tools/interactive_demo.py --verify` walks a realistic edit
sequence. The comparison against RETE for this workflow, including where RETE
still wins, is in [docs/interactive.md](docs/interactive.md).

Change management. Replay a proposed rule change against captured traffic and
find out what it does before shipping it:

```bash
python tools/whatif_demo.py          # generates a capture file and replays

canon whatif examples/rules/ftl.yaml \
      --proposal examples/proposals/2026-09-fatigue-package.yaml \
      --cases examples/shadow/cases.jsonl \
      --max-flip-rate 0.02
```

The report says how many decisions moved, which findings moved and in which
direction, and which rule is responsible for each. Crucially it reaches through
the derived namespace: when `FTL-010` starts firing and `FTL-010` did not change,
the report says `unchanged, reached via FTL-002`. It also lists rules that
changed and moved nothing, which is the evidence that a change is safe, and rules
that never fired at all, which is usually a longer list than anyone expects.
Details in [docs/what-if.md](docs/what-if.md).

---

## Authoring

Three front ends, one intermediate representation, identical semantics, identical
content hashes for identical meaning. Pick per team, or mix inside one ruleset.
The full comparison, including where each one hurts, is in
[docs/authoring-comparison.md](docs/authoring-comparison.md).

**YAML** — reviewable by people who do not write code, explicit about coupling.

```yaml
- id: FTL-010
  version: "5"
  reads: [derived.max_fdp_hours, derived.augmentation_credit]
  when: >
    hours_between(duty.start_utc, duty.end_utc)
    > derived.max_fdp_hours + derived.augmentation_credit
  emit:
    code: FTL_FDP_EXCEEDED
    severity: hard
    message: "Flight duty period of {actual_hours}h exceeds the permitted {limit_hours}h"
    detail:
      actual_hours: round(hours_between(duty.start_utc, duty.end_utc), 2)
      limit_hours: round(derived.max_fdp_hours + derived.augmentation_credit, 2)
```

**Python** — best tooling, compiled from the syntax tree, never executed.

```python
@builder.rule("FTL-010", version="5", priority=20)
def fdp_within_limit(f):
    """Flight duty period must not exceed the permitted maximum."""
    if hours_between(f.duty.start_utc, f.duty.end_utc) > \
            f.derived.max_fdp_hours + f.derived.augmentation_credit:
        emit("FTL_FDP_EXCEEDED", severity="hard",
             message="Flight duty period of {actual_hours}h exceeds the permitted {limit_hours}h",
             actual_hours=round(hours_between(f.duty.start_utc, f.duty.end_utc), 2),
             limit_hours=round(f.derived.max_fdp_hours + f.derived.augmentation_credit, 2))
```

**Decision table** — closest to how ODM business users already think. Cells hold
values, headers hold paths and operators.

```csv
id,when crew.rank ==,when crew.hours_on_type <,then code,then severity
QUAL-010,CP,100,LOW_TIME_CAPTAIN,hard
```

---

## What is deliberately not supported

A rules engine is as much about what it refuses as what it allows.

* **No `else`, no loops, no local variables in a rule.** One rule, one
  condition, one outcome. Two outcomes are two rules, and two rules can be
  versioned, hashed, traced and disabled independently.
* **No non-determinism.** There is no `now()`. The current date is a fact
  supplied by the caller, so a decision can be replayed years later.
* **No implicit rule ordering.** Priority orders findings in the output. It
  never affects logic. If a rule needs another rule's result it says so.
* **No arbitrary Python at run time.** Expressions are an allow listed subset of
  the Python grammar, walked by an interpreter. There is no `eval`.
* **No mutable engine state.** The engine holds a frozen ruleset and nothing
  else. Two identical calls return byte identical output including the trace.

---

## Repository layout

```
src/canon/          the library
  expr.py           restricted expression language and static analysis
  facts.py          projections, lazy batched fact resolution
  rules.py          rule IR, ruleset validation, strata, content hashing
  engine.py         stateless evaluation
  trace.py          provenance and explanation
  loaders.py        YAML and decision table front ends
  dsl.py            Python front end
  registry.py       manifests, merkle roots, deploy ledger, receipts
  session.py        interactive editing, incremental re-evaluation, previews
  shadow.py         parallel run and divergence attribution
  proposal.py       a rule change as a reviewable overlay, not a duplicate file
  whatif.py         replay a change against captured traffic and attribute flips
  odm_import.py     IBM ODM business action language importer
  cli.py            command line interface
examples/           a worked crew rostering ruleset and fact documents
tools/              capture generator for the shadow harness
demo/               the GitHub Pages demo, a single self contained page
docs/               architecture, authoring comparison, migration playbook, ADRs
tests/              pytest suite
```

---

## Licence

[Business Source License 1.1](LICENSE), becoming Apache-2.0 on 2 August 2030.

Free, with no need to ask: reading, forking, modifying, running locally or in
CI, measuring your payloads, importing an existing ruleset, benchmarking, and
shadow running against your current engine on real production traffic and real
production infrastructure. Every phase of the
[migration playbook](docs/migration-playbook.md) up to and including the shadow
run is free, because every one of those phases produces evidence and no
decisions.

Requires a commercial licence: using Canon's output to make, approve, publish or
record a real decision, and shipping Canon inside a product or service you sell.

Plain English summary, the reasoning behind where the line sits, and how to buy:
**[LICENSING.md](LICENSING.md)**. Commercial enquiries:
peterdsouza.personal@gmail.com.

Canon is source available, not open source, and the difference is worth stating
rather than blurring. Outside code contributions are not currently accepted;
[CONTRIBUTING.md](CONTRIBUTING.md) explains why and what is useful instead.

---

## Status and honesty

This is a working design and a working implementation of the core. It is not
a certified airworthiness tool and the flight time limitation numbers in
`examples/` are illustrative rather than regulatory.

**Verification status.** The test suite was written alongside the implementation
on a machine with no working Python sandbox, so the first CI run was also the
first execution. It failed, and the two causes are worth recording because both
are the kind of bug that only shows up when you actually run the thing:

* `ast.Store` was missing from the expression allow list. A comprehension's loop
  variable is a `Name` in `Store` context, and `ast.walk` yields the context
  object as a node in its own right, so every rule that needed a vertical slice
  refused to compile. Fixed, with a named regression test.
* `--json` was declared on the top level argument parser. argparse hands
  everything after the subcommand to the subparser, so `canon plan rules.yaml
  --json`, which is how it appears in this README and in CI, exited 2. It is now
  a shared parent parser, with a regression test per subcommand.

Run `pytest -q` yourself before trusting the rest. CI publishes the pytest
output to the run summary so a failure can be read without downloading anything.

Known gaps, stated plainly:

* The ODM importer handles the common BAL shapes. Collection quantifiers and
  mixed `and`/`or` precedence are refused rather than guessed at, so expect a
  meaningful manual residue on a real estate of rules.
* There is no incremental or RETE style matching. Canon evaluates every
  applicable rule's guard. For a per assignment legality check over a few
  hundred rules that is the right trade. For a ruleset in the tens of thousands
  with heavy shared subconditions it would need revisiting.
  [docs/performance.md](docs/performance.md) works through what to do about
  that, and `python tools/benchmark.py` produces the baseline. The short version:
  RETE's incrementality is worth nothing to a stateless engine, its sharing is
  worth a lot and is available as a load time index without the network, and the
  latency budget is probably dominated by payload size rather than matching.
* Signing uses HMAC from the standard library. A regulated deployment should
  put the key in a KMS or HSM and publish the chain head somewhere the rules
  team cannot rewrite.

See [docs/architecture.md](docs/architecture.md) for the reasoning, and
[docs/adr](docs/adr) for the decisions that could reasonably have gone the
other way.
