# Migration playbook

Replacing the engine that decides whether a crew member may legally operate a
flight is not a technical problem with a political wrapper. It is a political
problem with a technical wrapper. This playbook is written accordingly: every
phase produces evidence somebody else can check, and no phase requires anyone to
believe a claim.

It is written generically, for any team moving off a commercial business rules
management system. The order matters: phases one and two deliver value while the
existing engine is still in charge, which is what buys the time to do the rest
properly.

---

## Phase 0 — Measure, change nothing

**Goal: a number, agreed with the people who own the current system.**

1. Tap the request path and capture payloads to JSON Lines. Sample if volume
   requires it, but capture the full payload, not a summary.
2. Write the fact paths for a handful of representative rules by hand and build
   a `Projection` from them.
3. Run `Projection.select(payload)` over the capture and compare serialised
   sizes.

Deliverable: "on N thousand real transactions, the rules we sampled read X per
cent of the bytes we send". If that number is not compelling, stop. Everything
downstream rests on it.

Nothing has been deployed and nothing has changed.

---

## Phase 1 — Import, do not run

**Goal: the current rules, in a form you can search, hash and diff.**

```bash
canon import-odm exports/ruleset.bal \
      --verbalisation config/verbalisation.json \
      --out imported/ruleset.yaml
```

Expect a residue. On the worked example the importer converts the plain
comparison rules and refuses two: an ODM collection quantifier and a condition
that mixes `and` with `or` without brackets. Both refusals are correct. Guessing
at precedence in a legality rule is how somebody gets rostered illegally and
nobody finds out for six months.

Build the verbalisation file first. It is the BOM knowledge no importer can
recover from rule text, and getting it wrong silently changes what a rule reads,
so the importer refuses to guess.

Deliverable: a ruleset that loads, a coverage figure, and an explicit list of
rules a human must convert. That list is itself useful. It is usually the same
list of rules nobody understands any more.

---

## Phase 2 — Contract first, engine later

**Goal: shrink the payload without replacing anything.**

This is the phase that pays for the project.

```bash
canon plan imported/ruleset.yaml --client AIRLINE_A --json > contracts/airline_a.json
```

The projection is a machine readable field mask. The Java integration layer can
consume it directly and stop sending fields no rule reads. The existing engine is
still in charge; the rules have not changed; the payload gets smaller and the
latency follows.

Two things fall out that are worth having on their own:

* **Per client contracts.** A client whose rules do not use a field never pays
  to send it. In the example ruleset, `crew.hotel_rest_grade` appears only in
  `AIRLINE_B`'s contract because only `AIRLINE_B` has a rule that reads it.
* **A regression test for the integration layer.** Regenerate the contract in
  CI. If it changes, either a rule changed or somebody broke something.

Deliverable: measured latency improvement, in production, with the old engine
still running.

---

## Phase 3 — Shadow

**Goal: an agreement rate, per rule, on real traffic.**

```bash
canon shadow imported/ruleset.yaml --cases capture/week-32.jsonl \
             --out reports/week-32.json --threshold 0.999
```

Read the report in this order.

1. **Agreement rate.** The headline. Expect it to be poor at first and expect
   that to be informative.
2. **Divergence by finding code.** Tells you whether Canon is over firing or
   under firing, which are very different problems.
3. **Suspect rules.** Ranked by lift: how much more often a rule fires on
   diverging cases than on agreeing ones. A rule that fires on most of the
   failures and almost none of the successes is your culprit. In a ruleset of
   several hundred, this ranking is the difference between an afternoon and a
   fortnight.

The synthetic example in `tools/make_shadow_cases.py` plants three divergences
that are typical of a long lived ruleset:

* a flat duty limit that never reduces for sectors, because the reduction was
  added to the spec and never to the code,
* a cumulative check that fires an hour early, from an off by one old enough
  that nobody questions it,
* a rule that exists in the specification and was never implemented at all.

The shadow report finds all three and names the responsible rule for each,
without anybody having read the Java.

**Expect to find that the incumbent is wrong.** This is the phase where somebody
discovers a rule has been firing incorrectly in production for years. Plan for
that conversation before it happens, because the instinct will be to make Canon
reproduce the bug, and sometimes that is even the right call for a release or
two. Record it as a rule with an explicit `effective_to` date rather than as a
quiet compatibility hack.

Deliverable: a divergence report per week of traffic, and a shrinking list.

---

## Phase 4 — Dark launch

**Goal: production load, no production consequence.**

Run Canon in process, or as a sidecar, on live traffic. Discard the result and
record the comparison. This is the same comparison as phase three, but on live
data with live latency, which surfaces the things capture files never do: cache
behaviour, source timeouts, tail latency.

Turn on receipts here, not later. Every dark launched decision gets a receipt
binding the outcome to the manifest root that produced it. When the migration is
questioned in eighteen months, that record is the answer.

Deliverable: agreement rate and latency distribution under production load.

---

## Phase 5 — Cut over by rule, not by system

**Goal: never a big bang.**

Canon supports client scoping and effective dating on every rule, so the cut
over is a routing decision, taken one family of rules at a time:

1. Route one finding code to Canon and keep everything else where it is.
   Qualification checks are the usual first choice: numerous, self contained,
   low blast radius.
2. Keep shadowing the codes the old engine still owns.
3. Move the next family when the previous one has been quiet for a fortnight.
4. The flight time limitation rules go last, because they are the ones with real
   arithmetic and real consequences.

Every step is reversible by routing, not by redeployment.

Deliverable: a shrinking commercial footprint and a licence renewal conversation
with leverage.

---

## Phase 6 — Governance from day one of production

Not a phase so much as a habit, and it should start before cut over rather than
after.

```bash
# in CI, on every merge to main
canon validate rules/
canon manifest rules/ --out build/manifest.json --revision "$GIT_SHA"
canon diff build/manifest-previous.json build/manifest.json
```

The diff is the review artefact. Pay attention to `silent_changes`: rules whose
content changed while their version did not. That is almost always a mistake,
and it is invisible in a normal code review of a large YAML file.

```bash
# at deploy time
canon deploy build/manifest.json --env prod --by "$DEPLOYER" --secret "$CANON_KEY"
canon ledger verify --secret "$CANON_KEY"
```

And on every proposed rule change, before it reaches review:

```bash
canon whatif rules/ --proposal proposals/2026-09-fatigue-package.yaml \
      --cases capture/week-32.jsonl --max-flip-rate 0.02
```

This is the habit that outlives the migration. Shadow running stops being
interesting once the cut over is done; what-if replay does not, because "what
does this change actually do" is asked every week forever. Post the report on the
pull request. Two lines of it do most of the work: the rules that changed and
moved nothing, and the rules that moved decisions without themselves changing.
See [what-if.md](what-if.md).

Then, when the incident arrives:

```bash
canon ledger blame --rule FTL-010
#  #12  2026-06-14T08:31:00+00:00  prod  modified  a41c9f3e0b12 -> 77bd0e51a9cc  by r.patel
```

That line is question seven answered: the release, the date, the person, the
exact content that went out.

---

## What to say when someone asks about the cost

The licence saving is the easy half of the argument and the weaker half. The
stronger half:

* the payload reduction is measured in phase zero, before any commitment,
* the shadow report is evidence about the incumbent that you did not have before
  and would want even if the migration were cancelled,
* rule provenance and the deploy ledger reduce time to diagnose, which is the
  cost that actually hurts and never appears on a licence renewal.

Do not lead with the licence fee. Lead with the fact that nobody can currently
answer "which rule did this and when did it change", and that after phase six
anybody can, in one command.
