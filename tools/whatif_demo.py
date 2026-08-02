"""One command what-if demo.

Generates a synthetic capture file if one is not already present, applies the
example proposal to the example ruleset, replays every case against both, and
prints the impact report.

    python tools/whatif_demo.py
    python tools/whatif_demo.py --cases 2000 --json report.json

What to look for in the output, in this order:

1. **inert changes.** FTL-040 changed its priority. Priority orders findings in
   the output and never affects logic, so it should move nothing. The report
   proves it rather than asserting it.
2. **attribution.** FTL-010 will be responsible for a pile of newly raised
   findings, and FTL-010 did not change. The report says "via FTL-002", because
   the dependency graph knows that FTL-002 computes the limit FTL-010 tests
   against. That relationship is the one that is invisible today.
3. **the new rule.** CREW-006 reads like a modest tightening. Look at what
   fraction of the corpus it blocks before deciding it is modest.
4. **never fired.** Rules that did not fire once across the corpus. Either dead
   weight or quietly broken, and both are worth knowing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

RULES = ROOT / "examples" / "rules" / "ftl.yaml"
PROPOSAL = ROOT / "examples" / "proposals" / "2026-09-fatigue-package.yaml"
CASES = ROOT / "examples" / "shadow" / "cases.jsonl"


def ensure_cases(count: int) -> None:
    if CASES.exists():
        return
    print(f"no capture file at {CASES.relative_to(ROOT)}, generating {count} cases")
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "make_shadow_cases.py"),
         "-n", str(count)],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, default=600,
                        help="how many cases to generate if none exist")
    parser.add_argument("--limit", type=int, help="replay only the first N")
    parser.add_argument("--json", dest="json_out",
                        help="also write the full report as JSON")
    args = parser.parse_args()

    ensure_cases(args.cases)

    from canon import load_yaml, load_cases_jsonl, load_proposal
    from canon.whatif import WhatIf

    baseline = load_yaml(RULES)
    proposal = load_proposal(PROPOSAL)
    candidate = proposal.apply(baseline)

    print(proposal.render())
    print()
    print(f"baseline  {baseline.id} v{baseline.version}  "
          f"{len(baseline)} rules  {baseline.content_hash[:12]}")
    print(f"candidate {candidate.id} v{candidate.version}  "
          f"{len(candidate)} rules  {candidate.content_hash[:12]}")
    print()

    cases = load_cases_jsonl(CASES)
    if args.limit:
        cases = cases[:args.limit]

    report = WhatIf(baseline, candidate).run(cases)
    print(report.render())

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report.to_dict(sample_limit=25), indent=2, default=str),
            encoding="utf-8")
        print()
        print(f"full report written to {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
