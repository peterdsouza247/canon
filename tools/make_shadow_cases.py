"""Generate a synthetic capture file for the shadow harness.

In a real migration this file is produced by a tap on the production request
path: every payload the calling application sends to the existing engine, plus
the codes that engine returned. Here it is generated, and the stand in is
deliberately not quite right, in three ways typical of a long lived ruleset:

* it applies a flat thirteen hour duty limit and never reduces for sectors,
* it never implements the line training supervision check at all,
* it raises a cumulative hours finding one hour early, from an off by one that
  has been in production long enough that nobody questions it.

The point of the exercise is that the shadow report finds all three and
attributes each to the right rule, without anybody having read the Java.

    python tools/make_shadow_cases.py --out examples/shadow/cases.jsonl -n 400
"""

from __future__ import annotations

import argparse
import copy
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SCENARIOS = ROOT / "examples" / "data" / "scenarios.json"


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")


def perturb(facts: dict, rng: random.Random) -> dict:
    out = copy.deepcopy(facts)
    duty = out["duty"]
    crew = out["crew"]
    flight = out["flight"]

    duty["sectors"] = rng.choice([1, 2, 2, 3, 4, 5, 6])
    duty["acclimatised"] = rng.random() > 0.25
    duty["is_augmented"] = rng.random() > 0.85
    duty["additional_crew"] = rng.choice([0, 0, 1, 2]) if duty["is_augmented"] else 0

    start = _parse(duty["start_utc"]) + timedelta(minutes=rng.randrange(-180, 181, 15))
    length = rng.choice([7.5, 9.0, 10.5, 11.0, 12.0, 13.0, 14.0, 15.5])
    duty["start_utc"] = _iso(start)
    duty["end_utc"] = _iso(start + timedelta(hours=length))

    crew["rest_hours_before_duty"] = round(rng.uniform(9.0, 20.0), 1)
    crew["hours_last_28d"] = round(rng.uniform(30.0, 99.0), 1)
    crew["hours_last_365d"] = round(rng.uniform(400.0, 950.0), 1)
    crew["duty_hours_last_7d"] = round(rng.uniform(15.0, 65.0), 1)
    crew["standby_hours_before_report"] = rng.choice([0.0, 0.0, 0.0, 2.0, 4.0])

    departure = start + timedelta(minutes=60)
    block = rng.uniform(1.5, 9.0)
    flight["departure_utc"] = _iso(departure)
    flight["arrival_utc"] = _iso(departure + timedelta(hours=block))
    flight["departure_date"] = departure.strftime("%Y-%m-%d")

    roster = out["flight"]["roster"]
    for member in roster:
        if member["rank"] in ("CP", "FO"):
            member["hours_on_type"] = rng.choice([45, 70, 95, 120, 400, 900, 3200])
        if rng.random() > 0.93:
            member["is_under_line_training"] = True
    return out


# --------------------------------------------------------------------------
# The stand in for the incumbent engine
# --------------------------------------------------------------------------


def legacy_decision(facts: dict) -> list[str]:
    """A plausible, slightly wrong reimplementation of the incumbent."""
    codes: list[str] = []
    duty = facts["duty"]
    crew = facts["crew"]
    flight = facts["flight"]
    limits = facts["limits"]

    hours = (_parse(duty["end_utc"]) - _parse(duty["start_utc"])).total_seconds() / 3600.0

    # Divergence 1: flat limit, no sector reduction, no unacclimatised penalty.
    limit = limits["max_fdp_hours_base"]
    if duty.get("is_augmented") and duty.get("additional_crew", 0) >= 1:
        limit += 6.0 if duty["additional_crew"] >= 2 else 4.0
    if hours > limit:
        codes.append("FTL_FDP_EXCEEDED")

    if crew["rest_hours_before_duty"] < limits["min_rest_hours"]:
        codes.append("REST_INSUFFICIENT")

    block = (_parse(flight["arrival_utc"]) - _parse(flight["departure_utc"])
             ).total_seconds() / 3600.0
    # Divergence 2: off by one on the cumulative check.
    if crew["hours_last_28d"] + block > limits["max_hours_28d"] - 1:
        codes.append("BLOCK_HOURS_28D_EXCEEDED")

    if crew["hours_last_365d"] > limits["max_hours_365d"]:
        codes.append("BLOCK_HOURS_365D_EXCEEDED")
    if crew["duty_hours_last_7d"] > limits["max_duty_7d"]:
        codes.append("DUTY_HOURS_7D_EXCEEDED")

    cabin = sum(1 for m in flight["roster"] if m["rank"] in ("SCC", "CC"))
    if cabin < limits["min_cabin_crew"]:
        codes.append("CABIN_CREW_BELOW_MINIMUM")

    inexperienced = sum(1 for m in flight["roster"]
                        if m["rank"] in ("CP", "FO") and m["hours_on_type"] < 100)
    if inexperienced > 1:
        codes.append("INEXPERIENCED_PILOT_PAIRING")

    # Divergence 3: LINE_TRAINING_WITHOUT_LTC is simply not implemented.

    quals = crew.get("qualifications") or []
    if flight["aircraft_type"] not in quals:
        codes.append("NOT_TYPE_RATED")
    if flight.get("is_etops") and "ETOPS" not in quals:
        codes.append("ETOPS_NOT_QUALIFIED")
    if flight.get("is_lowvis_expected") and "LVO" not in quals:
        codes.append("LVO_NOT_QUALIFIED")
    if flight.get("destination_category") == "C" and "CAT_C" not in quals:
        codes.append("CAT_C_NOT_QUALIFIED")
    if crew.get("medical_expiry", "9999") < flight["departure_date"]:
        codes.append("MEDICAL_EXPIRED")
    if "LINE_CHECK_CURRENT" not in quals:
        codes.append("LINE_CHECK_LAPSED")

    return sorted(set(codes))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "examples" / "shadow" / "cases.jsonl"))
    parser.add_argument("-n", type=int, default=400)
    parser.add_argument("--seed", type=int, default=20260801)
    args = parser.parse_args()

    scenarios = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    bases = [value["facts"] for key, value in scenarios.items()
             if not key.startswith("_")]
    rng = random.Random(args.seed)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        handle.write("# Synthetic capture. One JSON object per line.\n")
        for index in range(args.n):
            facts = perturb(rng.choice(bases), rng)
            handle.write(json.dumps({
                "id": f"case-{index + 1:05d}",
                "client": "AIRLINE_A",
                "as_of": "2026-08-14",
                "key": {"crew_id": facts["crew"]["id"],
                        "flight_id": facts["flight"]["number"]},
                "facts": facts,
                "legacy_codes": legacy_decision(facts),
                "legacy_micros": round(rng.uniform(2800.0, 9500.0), 1),
            }) + "\n")

    print(f"wrote {args.n} cases to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
