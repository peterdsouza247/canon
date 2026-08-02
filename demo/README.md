# The demo

A single page, no build step, no server. Open `index.html` in a browser and it
works, including from `file://`.

Independent project, not affiliated with or derived from any employer's systems.
Every rule, operator, flight, aircraft and person on the page is invented, and
the duty and rest limits are shaped like published regulation rather than copied
from it.

## Two audiences, one page

The page opens in **Plain English** and there is a toggle in the top right for
**Technical**. The switch changes the wording and nothing else: the same engine
runs, the same numbers appear, the same rules are evaluated. The hint next to the
toggle says so, because a reader who suspects the "simple" version is also the
dishonest version will not trust either.

Plain mode adds three things beyond softer wording:

* a worked story of one decision (a captain, a flight, a refusal, and the
  question nobody can answer six months later) before any interactive panel,
* a live plain-language summary under each panel, computed from the run rather
  than written in advance, so it cannot drift from what the panel shows,
* a glossary of the twelve terms the page cannot avoid.

Technical mode keeps the precise vocabulary: projection, static analysis,
strata, Merkle root, lift, flip rate. Nothing is removed in either direction, so
a mixed audience can read the same page together and argue about the same
numbers.

## What is real here

Everything on the page is computed in the browser when you load it.

* `engine.js` is a JavaScript port of the ideas in `src/canon`: lazy fact
  references that record what they are asked for, one walk that both plans and
  evaluates, declared rule dependencies sorted into strata, and a full trace.
* `rules.js` mirrors `examples/rules/ftl.yaml` and
  `examples/rules/qualifications.csv`, carrying both the executable form and the
  source text so the page can show you the rule it is actually running.
* The payload contract, the byte comparison, the trace, the shadow report, the
  what-if replay and every hash on the page are computed live. `sha256` is
  implemented in `engine.js`, so the Merkle roots and the deploy chain are
  genuine.
* The what-if section builds a candidate ruleset in the browser from the same
  four edits as `examples/proposals/2026-09-fatigue-package.yaml`, replays both
  rulesets over generated traffic, and attributes every moved decision.

Nothing is a stored screenshot and no figure is hard coded.

## The one honest difference from the Python engine

Python parses rule expressions from source text, so boolean composition can be
written with ordinary `and` and `or` and the static walk can visit both sides.
JavaScript cannot intercept those operators, so the port asks rule authors to
write `$.all(...)` and `$.any(...)` over thunks instead. The semantics are the
same, including short circuiting at run time and full branch coverage when
planning. It is a notation difference, not a behavioural one.

## Publishing to GitHub Pages

`.github/workflows/pages.yml` uploads this folder as the Pages artefact on every
push to `main`. The workflow passes `enablement: true` to `configure-pages`, so
it turns Pages on for the repository itself the first time it runs. If that step
still fails with "Get Pages site failed", turn it on by hand: repository
Settings, Pages, Source set to **GitHub Actions**.

To serve it locally instead:

```bash
python -m http.server -d demo 8000
```

Then open http://localhost:8000.
