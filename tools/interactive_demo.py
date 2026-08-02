"""What an editing session looks like, and what it costs.

A planner opens a roster, makes eight edits, and expects the legality panel to
keep up. This walks that sequence against the example ruleset and prints what
each edit did, which rules had to be re-evaluated, and how much work was skipped.

    python tools/interactive_demo.py
    python tools/interactive_demo.py --verify   # check every edit against a
                                                # full evaluation

The number to watch is "work avoided". It is the fraction of rule evaluations
that were skipped because nothing the rule read had changed. That is the same
benefit a RETE network gives, obtained from the trace we were already keeping.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

RULES = ROOT / "examples" / "rules" / "ftl.yaml"
SCENARIOS = ROOT / "examples" / "data" / "scenarios.json"

# A plausible sequence of planner actions on one assignment.
EDITS: list[tuple[str, dict]] = [
    ("extend the duty by two hours",
     {"duty.end_utc": "2026-08-14T17:45:00Z"}),
    ("add two more sectors",
     {"duty.sectors": 4}),
    ("mark the crew member as unacclimatised",
     {"duty.acclimatised": False}),
    ("swap in a low hours first officer",
     {"flight.roster.1.hours_on_type": 60}),
    ("and a second one",
     {"flight.roster.2.hours_on_type": 80}),
    ("shorten the rest before report",
     {"crew.rest_hours_before_duty": 10.5}),
    ("put the rest back",
     {"crew.rest_hours_before_duty": 14.0}),
    ("pull the duty back to where it started",
     {"duty.end_utc": "2026-08-14T15:45:00Z", "duty.sectors": 2,
      "duty.acclimatised": True}),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="legal")
    parser.add_argument("--verify", action="store_true",
                        help="check every edit against a full evaluation")
    parser.add_argument("--json", dest="json_out")
    args = parser.parse_args()

    from canon import Engine, Session, load_yaml

    scenarios = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    scenario = scenarios[args.scenario]
    engine = Engine(load_yaml(RULES), strict_facts=False)

    session = Session(engine, scenario["facts"],
                      client=scenario["client"], as_of=scenario["as_of"],
                      key=scenario["key"])

    print(f"opened {scenario['label']}")
    print(f"  {len(engine.ruleset)} rules, "
          f"{'legal' if session.decision.ok else 'not legal'} to begin with")
    print()

    records = []
    divergences: list[str] = []
    for label, changes in EDITS:
        delta = session.apply(changes)
        print(f"* {label}")
        for line in delta.render().splitlines():
            print(f"    {line}")
        if args.verify:
            problems = session.verify()
            if problems:
                divergences.extend(f"{label}: {p}" for p in problems)
            print(f"    verified against a full evaluation: "
                  f"{'agrees' if not problems else problems}")
        print()
        records.append({"label": label, "delta": delta.to_dict()})

    stats = session.stats()
    print("across the session")
    print(f"  {stats['edits']} edits")
    print(f"  {stats['rules_recomputed']} rule evaluations, "
          f"{stats['rules_reused']} skipped")
    print(f"  {stats['work_avoided']:.0%} of the work avoided")
    print(f"  p50 {stats['p50_micros']}us, p95 {stats['p95_micros']}us per edit")
    if stats["full_evaluations"]:
        print(f"  {stats['full_evaluations']} edits were broad enough that a "
              f"full evaluation was cheaper")

    print()
    print("Every one of those edits can be checked against a from scratch "
          "evaluation with Session.verify(), because the incremental path and "
          "the full path are the same code. Run again with --verify.")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"edits": records, "stats": stats}, indent=2, default=str),
            encoding="utf-8")
        print(f"\nwritten to {args.json_out}")

    if divergences:
        # The whole editing argument rests on the incremental path agreeing
        # with the full one. If it ever does not, that is a failure, not a note.
        print("\nINCREMENTAL RESULT DIVERGED FROM A FULL EVALUATION",
              file=sys.stderr)
        for problem in divergences:
            print(f"  {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
