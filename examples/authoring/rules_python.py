"""The same rules as ``examples/rules/ftl.yaml``, written in Python.

Run this file to see the ruleset validate and to print the payload contract it
generates. Nothing here is executed by the engine: the decorator reads the
source of each function, compiles it into the same ``Rule`` object the YAML
loader produces, and throws the function away.

    python examples/authoring/rules_python.py
"""

from __future__ import annotations

from canon.dsl import RuleSetBuilder, emit, set_

builder = RuleSetBuilder(
    "crew_rostering_python",
    version="2026.08.1",
    description="Python authored equivalent of the YAML example ruleset.",
)
builder.combine("max_fdp_hours", "min")
builder.combine("augmentation_credit", "max")


# --------------------------------------------------------------------------
# Stratum 0: derive the applicable limits
# --------------------------------------------------------------------------


@builder.rule("FTL-001", version="4", priority=10, tags=["ftl", "derivation"])
def baseline_fdp(f):
    """Baseline flight duty period for an acclimatised crew member."""
    set_(max_fdp_hours=f.limits.max_fdp_hours_base)


@builder.rule("FTL-002", version="3", priority=11, tags=["ftl", "derivation"])
def sector_reduction(f):
    """Each sector past the second costs half an hour, floored at nine."""
    if f.duty.sectors > 2:
        set_(max_fdp_hours=max(
            9.0, f.limits.max_fdp_hours_base - 0.5 * (f.duty.sectors - 2)))


@builder.rule("FTL-003", version="2", priority=12, tags=["ftl", "derivation"])
def unacclimatised_reduction(f):
    """An unacclimatised crew member loses two hours."""
    if f.duty.acclimatised == False:  # noqa: E712 - compiled, not evaluated
        set_(max_fdp_hours=f.limits.max_fdp_hours_base - 2.0)


@builder.rule("FTL-004", version="1", priority=13, tags=["ftl", "derivation"])
def no_augmentation_by_default(f):
    """Floor for the augmentation credit."""
    set_(augmentation_credit=0.0)


@builder.rule("FTL-005", version="2", priority=14, tags=["ftl", "derivation"])
def augmented_extension(f):
    """An extra pilot buys four hours, two extra pilots buy six."""
    if f.duty.is_augmented == True and f.duty.additional_crew >= 1:  # noqa: E712
        set_(augmentation_credit=6.0 if f.duty.additional_crew >= 2 else 4.0)


# --------------------------------------------------------------------------
# Stratum 1: test against the derived limits
# --------------------------------------------------------------------------


@builder.rule("FTL-010", version="5", priority=20, tags=["ftl", "legality"])
def fdp_within_limit(f):
    """Flight duty period must not exceed the permitted maximum.

    Note that ``reads`` is not written out here. The builder discovers the
    dependency on the two derived values from the syntax tree and fills it in.
    That is the convenience the Python front end buys, and the visibility it
    costs. Pass ``auto_reads=False`` to the builder to get the YAML behaviour.
    """
    if hours_between(f.duty.start_utc, f.duty.end_utc) > \
            f.derived.max_fdp_hours + f.derived.augmentation_credit:
        emit("FTL_FDP_EXCEEDED",
             severity="hard",
             message="Flight duty period of {actual_hours}h exceeds the "
                     "permitted {limit_hours}h for this duty",
             actual_hours=round(
                 hours_between(f.duty.start_utc, f.duty.end_utc), 2),
             limit_hours=round(
                 f.derived.max_fdp_hours + f.derived.augmentation_credit, 2),
             sectors=f.duty.sectors,
             acclimatised=f.duty.acclimatised)


@builder.rule("FTL-020", version="3", priority=21, tags=["ftl", "rest"])
def minimum_rest(f):
    """Rest before report must meet the minimum."""
    if f.crew.rest_hours_before_duty < f.limits.min_rest_hours:
        emit("REST_INSUFFICIENT",
             severity="hard",
             message="Rest of {actual}h before report falls short of the "
                     "{required}h minimum",
             actual=f.crew.rest_hours_before_duty,
             required=f.limits.min_rest_hours)


@builder.rule("CREW-002", version="2", priority=31, tags=["composition"])
def inexperienced_pairing(f):
    """Two pilots under a hundred hours on type must not be paired.

    This is the vertical slice case. The comprehension is ordinary Python and
    it is also the thing that tells the planner to fetch rank and hours on type
    for every crew member on the flight, and nothing else about them.
    """
    if sum(1 for m in f.flight.roster
           if m.rank in ['CP', 'FO'] and m.hours_on_type < 100) > 1:
        emit("INEXPERIENCED_PILOT_PAIRING",
             severity="hard",
             message="{count} pilots on this flight have under 100 hours on type",
             count=sum(1 for m in f.flight.roster
                       if m.rank in ['CP', 'FO'] and m.hours_on_type < 100))


@builder.rule("CREW-003", version="2", priority=32, tags=["composition"])
def line_training_supervision(f):
    """Line training needs a line training captain on board."""
    if any(m.is_under_line_training for m in f.flight.roster) and \
            not any(m.is_line_training_captain for m in f.flight.roster):
        emit("LINE_TRAINING_WITHOUT_LTC",
             severity="hard",
             message="A crew member is under line training with no line "
                     "training captain on board")


ruleset = builder.build()


# ``hours_between`` is a Canon builtin. It is referenced above so that the
# compiler can see the call; it is never resolved in this module's namespace.
def hours_between(*_args):  # pragma: no cover - never called
    raise RuntimeError("Canon builtins are compiled, not executed")


if __name__ == "__main__":
    from canon import Engine

    print(repr(ruleset))
    print()
    for index, stratum in enumerate(ruleset.strata):
        print(f"stratum {index}: {', '.join(rule.id for rule in stratum)}")
    print()
    print("payload contract")
    print(ruleset.projection.to_json())
    print()
    for rule in ruleset.rules:
        if rule.reads:
            print(f"{rule.id} declares reads {list(rule.reads)} "
                  f"(discovered automatically)")
    print()
    print("content hashes are front end independent, so a rule moved from "
          "YAML to Python keeps its identity as long as its meaning is "
          "unchanged:")
    for rule in ruleset.rules[:3]:
        print(f"  {rule.id:<10} {rule.short_hash}")
