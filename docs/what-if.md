# What-if replay

Shadow running answers "does the new engine agree with the old one". That is a
migration question, and once the migration is done it stops being interesting.

What-if replay answers the question that never stops being interesting: **if we
make this change, what happens to real decisions?**

Today that gets answered with judgement. Someone reads the diff, thinks about it,
says it looks contained. Then a roster build produces four hundred new hard
findings on a Tuesday morning, and the change is reverted by people who still do
not know which of the four edits caused it.

## Run the demo

Three commands from a clean checkout. Python 3.10 or newer.

```bash
pip install -e ".[dev]"

# 1. Generate a synthetic capture file. In a real deployment this is a tap on
#    the production request path; here it is 600 perturbed transactions.
python tools/make_shadow_cases.py -n 600

# 2. Replay the example proposal against every one of them.
canon whatif examples/rules/ftl.yaml \
      --proposal examples/proposals/2026-09-fatigue-package.yaml \
      --cases examples/shadow/cases.jsonl
```

Or the same thing in one command, which generates the capture file if it is
missing:

```bash
python tools/whatif_demo.py
```

Useful variations:

```bash
# see what the proposal changes, without claiming to know what it does
canon whatif examples/rules/ftl.yaml \
      --proposal examples/proposals/2026-09-fatigue-package.yaml

# machine readable, for a pull request comment
canon whatif examples/rules/ftl.yaml \
      --proposal examples/proposals/2026-09-fatigue-package.yaml \
      --cases examples/shadow/cases.jsonl --json --out report.json

# fail CI if a change moves more than two per cent of decisions
canon whatif examples/rules/ftl.yaml \
      --proposal examples/proposals/2026-09-fatigue-package.yaml \
      --cases examples/shadow/cases.jsonl --max-flip-rate 0.02

# compare two whole rulesets instead of applying an overlay
canon whatif rules/current --candidate rules/proposed --cases capture/week-32.jsonl
```

The browser demo has the same thing, live, in the "What-if" section.

## What to read, in this order

**1. Inert changes.** Rules whose content hash changed and which moved nothing
anywhere in the corpus. In the example, `FTL-040` only has its priority edited.
Priority orders findings in the output and never affects logic, so it should be
inert, and the report proves it rather than asserting it. This list is the
evidence that a change is safe, and it is the thing nobody can currently produce.

**2. Attribution.** `FTL-010` will be responsible for a pile of newly raised
`FTL_FDP_EXCEEDED` findings, and `FTL-010` did not change. What changed is
`FTL-002`, which computes the limit `FTL-010` tests against. The report says
`unchanged, reached via FTL-002`, because the dependency graph already knows the
relationship. That indirection is exactly the thing that is invisible in a normal
diff review, and it is why declared dependencies were worth the ceremony.

**3. The new rule.** `CREW-006` reads like a modest tightening: commanders should
hold 500 hours on type. Look at what fraction of the corpus it blocks before
anybody signs it off. In the synthetic corpus it is a large number, which is the
point of running the replay at all.

**4. Never fired.** Rules that did not fire once across the whole corpus, in
either ruleset. Either dead weight or a rule that has quietly stopped matching
anything. Both are worth a conversation and neither is visible today. This falls
out for free because every decision already carries a trace.

## Proposals

A proposal names only what moves. Copying the whole ruleset to a second file and
asking people to spot the differences throws that away, and it is how a change
gets approved that nobody actually read.

```yaml
proposal: 2026-09-fatigue-package
against: crew_rostering
version: "2026.09.0-rc1"

modify:
  - id: FTL-002
    version: "4"
    set:
      max_fdp_hours: "max(9.0, limits.max_fdp_hours_base - 0.75 * (duty.sectors - 2))"
  - id: FTL-040
    priority: 26

add:
  - id: CREW-006
    version: "1"
    when: "sum(1 for m in flight.roster if m.rank == 'CP' and m.hours_on_type < 500) > 0"
    emit:
      code: COMMANDER_BELOW_EXPERIENCE_MINIMUM
      severity: hard

remove: []
```

Keys given as `null` under `modify` are deleted from the rule. Applying a
proposal rebuilds the candidate through the ordinary loader, so it gets the
ordinary validation: a proposal that introduces a cycle, an undeclared
dependency or an unpoliced write conflict fails at apply time rather than in
production.

```python
from canon import load_yaml, load_proposal, load_cases_jsonl
from canon.whatif import WhatIf

baseline  = load_yaml("examples/rules/ftl.yaml")
candidate = load_proposal("examples/proposals/2026-09-fatigue-package.yaml").apply(baseline)

report = WhatIf(baseline, candidate).run(load_cases_jsonl("capture/week-32.jsonl"))
print(report.render())
report.inert_changes()   # ['FTL-040']
report.never_fired()     # rules nothing in a month of traffic touched
```

## How classification works

For each captured transaction the replay runs both rulesets and compares.

| Kind | Meaning |
|---|---|
| `unchanged` | Same findings, same derived values. Most cases, on most changes. |
| `newly_blocked` | Passed before, fails now. The one that generates support tickets. |
| `newly_allowed` | Failed before, passes now. The one that generates regulatory questions. |
| `findings_changed` | Verdict is the same but the reasons differ. Easy to miss and worth seeing. |
| `derived_changed` | No finding moved, but a derived limit did. A near miss: this change did not bite today and will on a slightly different roster. |
| `errored` | A rule raised on this case. Recorded, not fatal to the run. |

Attribution finds the trace that emitted each added or removed code, then asks
whether that rule is itself in the diff. If it is not, it walks the derived
dependency graph backwards to the changed rule that reached it.

## Limits, stated

The replay is only as good as the corpus. A change that bites on an edge case
your capture does not contain will show as inert, and "inert on this corpus" is
not "inert". Say the corpus size and period whenever you quote a flip rate.

Attribution assumes a finding's cause is the rule that emitted it or something
upstream of it in the derived graph. If a change alters which rules are
*applicable*, by moving a client scope or an effective date, the emitting rule
may be unchanged and have no changed upstream. That case shows as
`cause not in the diff`, which is honest rather than helpful. Look at the
manifest diff directly when you see it.

The replay runs both rulesets over every case, so it costs twice a normal
evaluation. On a few hundred rules and a few thousand cases that is seconds. On
a month of high volume traffic, sample first with `--limit`.
