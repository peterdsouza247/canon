"""The expression language, and the static analysis that rides on it."""

from __future__ import annotations

import pytest

from canon.errors import ExpressionError, UnsafeExpressionError
from canon.expr import Expression, MappingResolver, compile_expression

ROOTS = ["crew", "flight", "duty", "limits"]


def paths(source: str) -> list[str]:
    return Expression(source).analyse(ROOTS)


def value(source: str, data: dict):
    resolver = MappingResolver(data)
    return Expression(source).evaluate(resolver)


# --------------------------------------------------------------------------
# Safety
# --------------------------------------------------------------------------


@pytest.mark.parametrize("source", [
    "__import__('os').system('ls')",
    "lambda x: x",
    "crew.__class__",
    "(x := 1)",
    "f'{crew.base}'",
    "crew.base.__dict__",
    "[x for x in range(3)]",
])
def test_unsafe_syntax_is_refused(source):
    with pytest.raises((UnsafeExpressionError, ExpressionError)):
        Expression(source).analyse(ROOTS)


def test_unknown_function_is_refused():
    with pytest.raises(ExpressionError):
        Expression("eval('1')").analyse(ROOTS)


def test_unknown_root_is_refused():
    with pytest.raises(ExpressionError):
        Expression("payroll.salary > 10").analyse(ROOTS)


def test_syntax_error_names_the_expression():
    with pytest.raises(ExpressionError) as info:
        Expression("crew.base ==")
    assert "syntax error" in str(info.value)


# --------------------------------------------------------------------------
# Static analysis
# --------------------------------------------------------------------------


def test_scalar_paths_are_discovered():
    assert paths("crew.seniority_years > 5") == ["crew.seniority_years"]


def test_only_leaves_are_planned():
    found = paths("crew.rest_hours_before_duty < limits.min_rest_hours")
    assert found == ["crew.rest_hours_before_duty", "limits.min_rest_hours"]


def test_comprehension_target_is_not_mistaken_for_unsafe_syntax():
    """Regression. The loop variable in ``for m in flight.roster`` is a Name in
    Store context, and ``ast.walk`` yields the context object as a node in its
    own right. Leaving Store off the allow list made every collection rule fail
    to compile, which is to say every rule that needed a vertical slice."""
    Expression("any(m.rank == 'CP' for m in flight.roster)").analyse(ROOTS)
    Expression("[m.rank for m in flight.roster]").analyse(ROOTS)
    Expression("{m.rank for m in flight.roster}").analyse(ROOTS)
    Expression("sum(m.hours_on_type for m in flight.roster)").analyse(ROOTS)


def test_comprehension_yields_a_vertical_slice():
    found = paths("any(m.rank == 'CP' for m in flight.roster)")
    assert "flight.roster" in found
    assert "flight.roster[*].rank" in found


def test_comprehension_filter_paths_are_planned_too():
    found = paths(
        "sum(1 for m in flight.roster if m.rank in ['CP','FO'] "
        "and m.hours_on_type < 100)")
    assert "flight.roster[*].rank" in found
    assert "flight.roster[*].hours_on_type" in found


def test_both_branches_of_a_conditional_are_planned():
    found = paths("crew.base if duty.acclimatised else crew.home_base")
    assert "crew.base" in found
    assert "crew.home_base" in found
    assert "duty.acclimatised" in found


def test_static_analysis_does_not_short_circuit():
    """A guard that fails on its first term at run time still declares the
    fields its later terms would have needed, otherwise the payload contract
    would depend on the data."""
    found = paths("duty.sectors > 99 and crew.hours_last_28d > 10")
    assert found == ["crew.hours_last_28d", "duty.sectors"]


def test_nested_attribute_paths():
    assert paths("crew.address.city == 'LHR'") == ["crew.address.city"]


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


def test_arithmetic_and_comparison():
    data = {"duty": {"sectors": 5}, "limits": {"max_fdp_hours_base": 13.0}}
    assert value("limits.max_fdp_hours_base - 0.5 * (duty.sectors - 2)", data) == 11.5


def test_hours_between():
    data = {"duty": {"start_utc": "2026-08-14T05:00:00Z",
                     "end_utc": "2026-08-14T19:30:00Z"}}
    assert value("hours_between(duty.start_utc, duty.end_utc)", data) == 14.5


def test_membership_on_a_missing_list_is_false_not_an_error():
    assert value("'ETOPS' in crew.qualifications", {"crew": {}}) is False


def test_comprehension_over_a_roster():
    data = {"flight": {"roster": [
        {"rank": "CP", "hours_on_type": 4000},
        {"rank": "FO", "hours_on_type": 60},
        {"rank": "FO", "hours_on_type": 80},
        {"rank": "CC", "hours_on_type": 10},
    ]}}
    assert value(
        "sum(1 for m in flight.roster if m.rank in ['CP','FO'] "
        "and m.hours_on_type < 100)", data) == 2


def test_reads_are_recorded_with_values():
    resolver = MappingResolver({"crew": {"seniority_years": 14}})
    Expression("crew.seniority_years > 5").evaluate(resolver)
    assert resolver.reads["crew.seniority_years"].value == 14


def test_short_circuit_avoids_the_second_read():
    resolver = MappingResolver({"duty": {"sectors": 1},
                                "crew": {"hours_last_28d": 90}})
    Expression("duty.sectors > 99 and crew.hours_last_28d > 10").evaluate(resolver)
    assert "duty.sectors" in resolver.reads
    assert "crew.hours_last_28d" not in resolver.reads


def test_compile_cache_returns_the_same_object():
    first = compile_expression("crew.base == 'LHR'", "R1")
    second = compile_expression("crew.base == 'LHR'", "R1")
    assert first is second
