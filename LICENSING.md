# Licensing

Canon is published under the [Business Source License 1.1](LICENSE). This page
is the plain English version. Where the two disagree, the LICENSE file wins.

## The short version

| What you want to do | What it costs |
|---|---|
| Read the source, clone it, fork it, modify it | Free |
| Run it on your laptop, in CI, in a test environment | Free |
| Measure your payloads with `Projection.select` | Free |
| Import an existing ruleset and see what converts | Free |
| Shadow run it against your current engine, on production data and production infrastructure | Free |
| Benchmark it, replay proposed rule changes against real traffic | Free |
| Personal projects, academic research, teaching | Free |
| **Use its output to make, approve, publish or record a real decision** | **Commercial licence** |
| **Ship it inside a product or service you sell** | **Commercial licence** |
| Anything at all, from 2 August 2030 | Free, under Apache-2.0 |

## Why the line is drawn there

The free grant is deliberately shaped around the
[migration playbook](docs/migration-playbook.md). Every phase up to and
including shadow running produces evidence and produces no decisions, so all of
it is free. You can measure your payload sizes, convert your existing rules, run
Canon beside your current engine on a month of real traffic, and find out
exactly how much it disagrees, without paying anything or asking anyone.

The licence only becomes relevant at the point where Canon starts deciding
things, which is also the point at which it starts being worth something to you.

This is not a trap and there is nothing to uninstall if you decide against it.
If the evidence says the current engine is fine, you have lost nothing but the
time, and you will have a measurement you did not have before.

## The change date

On 2 August 2030 this version becomes available under the Apache License 2.0,
with no restrictions at all. That date is fixed and cannot be moved back.

The licence applies separately to each released version, so a version published
later may carry a later change date. Nothing already released is ever withdrawn.

## Buying a commercial licence

Email **peterdsouza.personal@gmail.com**.

Expect it to be priced against what the licence you are replacing costs, not
against a per seat table. Migration work is quoted separately and in stages, and
each stage ends in a number you can check rather than a claim you have to
believe.

## Contributions

Canon does not currently accept outside contributions. See
[CONTRIBUTING.md](CONTRIBUTING.md) for why, and what to do instead.

## Not open source, and saying so

The Business Source License is source available, not open source. It does not
meet the Open Source Definition, and calling it open source would be wrong.
Everything is readable, everything is forkable, and every version eventually
becomes genuinely open. Until its change date, production use is restricted.

## No warranty

Canon is provided as is, with no warranty of any kind. It is not certified for
operational use, it has not been assessed against any airworthiness or safety
standard, and the example rules in this repository are illustrative rather than
regulatory. Nothing here should be relied on to determine whether anybody may
legally operate an aircraft.
