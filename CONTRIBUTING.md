# Contributing

**Canon is not accepting code contributions at the moment.** Pull requests will
be closed unread, and that is not a comment on the quality of the work.

## Why

Canon is dual licensed: [Business Source License](LICENSE) publicly, commercial
licence privately. Offering a commercial licence requires holding all of the
copyright in the work. The moment a contribution from somebody else is merged,
part of the codebase belongs to them, and their copy is licensed under the BSL
with no right to sublicense it commercially. At that point the commercial
licence can no longer be offered honestly.

The usual remedy is a contributor licence agreement, which asks contributors to
assign or broadly licence their copyright. That is a reasonable thing to ask for
an established project with a legal team behind it. For a project this small it
is friction with no payoff yet, and asking people to sign paperwork for a two
line fix is worse than not taking the fix.

If Canon reaches the point where a CLA is worth the trouble, this file will say
so and outside contributions will open.

## What is genuinely useful instead

**Bug reports.** Especially anything where the engine is wrong rather than
merely slow. A failing case is worth more than a patch here, because a case can
be turned into a regression test without any copyright question arising.

**Measurements.** If you run `tools/benchmark.py` or the payload measurement on
a real ruleset, the numbers are interesting and none of the claims in this
repository are worth much without them.

**Disagreement with the design.** The decision records in [docs/adr](docs/adr)
each list what the decision costs as well as what it buys. If one of those trade
offs is wrong, saying so is more valuable than working around it.

**Forks.** The licence permits them. Fork freely, and if you build something
better, say so.

## Reporting something

Open an issue. For anything that looks like a security problem, email
peterdsouza.personal@gmail.com rather than filing publicly.
