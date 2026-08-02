# Three ways to write the same rule

You asked to see the options compared rather than pick one blind. Here is one
real rule, `FTL-020`, written three ways. All three compile to the same `Rule`
object and produce the **same content hash**, which is the point: the choice is
about who writes and reviews rules, not about what the engine does.

## The rule

> Rest before report must meet the minimum. If it does not, raise a hard finding
> naming the actual rest and the required rest.

### YAML

```yaml
- id: FTL-020
  version: "3"
  priority: 21
  title: Minimum rest before duty
  when: crew.rest_hours_before_duty < limits.min_rest_hours
  emit:
    code: REST_INSUFFICIENT
    severity: hard
    message: "Rest of {actual}h before report falls short of the {required}h minimum"
    detail:
      actual: crew.rest_hours_before_duty
      required: limits.min_rest_hours
```

### Python

```python
@builder.rule("FTL-020", version="3", priority=21)
def minimum_rest(f):
    """Rest before report must meet the minimum."""
    if f.crew.rest_hours_before_duty < f.limits.min_rest_hours:
        emit("REST_INSUFFICIENT",
             severity="hard",
             message="Rest of {actual}h before report falls short of the "
                     "{required}h minimum",
             actual=f.crew.rest_hours_before_duty,
             required=f.limits.min_rest_hours)
```

### Decision table

```csv
id,version,priority,when crew.rest_hours_before_duty <,then code,then severity,then message
FTL-020,3,21,=limits.min_rest_hours,REST_INSUFFICIENT,hard,Rest before report falls short of the minimum
```

`tests/test_migration.py::test_python_and_yaml_produce_the_same_rule` asserts
the YAML and Python forms hash identically.

## Where each one hurts

| | YAML | Python | Decision table |
|---|---|---|---|
| Readable by a non engineer | Mostly. Conditions are still expressions | No | Yes, this is its whole reason to exist |
| Editable by a non engineer | With care and a schema aware editor | No | Yes |
| Editor support, jump to definition, refactoring | None | Full | None |
| Diff quality in review | Good | Good | Poor once the table is wide |
| Expresses one condition per rule | Yes | Yes | Yes, one rule per row |
| Expresses many similar rules cheaply | Poor, lots of repetition | Moderate | Excellent, that is what a table is |
| Expresses an unusual condition | Yes | Yes | Only via the `=expression` escape hatch |
| Coupling between rules is visible in the source | Yes, `reads:` is mandatory | No by default, discovered from the syntax tree | Yes, via the `reads` column |
| Risk of a typo silently disabling a condition | Low, unknown keys are refused | Low, it will not compile | **Higher**, an empty cell means "no condition" and looks like a blank |
| Merge conflicts on a busy ruleset | Moderate | Moderate | Bad, CSV rows conflict badly |

## The one real semantic difference

`RuleSetBuilder(auto_reads=True)`, the default for the Python front end,
discovers which derived facts a rule consumes from its syntax tree and fills in
the `reads` declaration for you.

That is convenient and it costs something. In YAML, a reviewer looking at
`FTL-010` sees:

```yaml
reads:
  - derived.max_fdp_hours
  - derived.augmentation_credit
```

and knows immediately that this rule depends on other rules. In Python with
`auto_reads=True` that line is absent from the source; the coupling is real,
enforced and visible in the compiled artefact and on the trace, but not in the
file the reviewer is reading.

Given that undeclared coupling between rules is one of the seven recurring
problems, the recommendation is `auto_reads=False` for any team that adopts the
Python front end. The convenience is not worth reintroducing the thing you were
trying to fix.

## Recommendation

Not one front end. Two, split by the shape of the rule.

**Decision tables for the qualification and eligibility matrices.** These are
the rules that are numerous, structurally identical, and change when the
business changes rather than when the regulation changes. They are also the ones
non engineers most want to see and edit. `examples/rules/qualifications.csv` is
this shape: six rules, one row each, no expression anywhere except two escape
hatches.

**YAML for the flight time limitation and composition rules.** These are fewer,
individually intricate, carry real arithmetic, and depend on one another. They
need the `reads` declaration to be explicit and they need to be readable as
prose in review. `examples/rules/ftl.yaml` is this shape.

**Python for the rules that need it, and for tests.** Some conditions really do
want an IDE. Keep the option, set `auto_reads=False`, and expect it to stay a
minority of the estate.

Mixing is supported: `load_directory` merges YAML and CSV files into one
validated ruleset, and `RuleSetBuilder.add` takes rules from any source.

## A note on what a decision table cannot do

The escape hatch matters more than it looks. A cell beginning with `=` is an
expression, not a value, and once a table has several of those it has stopped
being a decision table and become a spreadsheet full of code with none of the
review affordances of code. Watch the ratio. If a table's `=` cells climb past
roughly one in ten, those rows want moving to YAML.
