# ADR 0001: A restricted subset of Python, interpreted, not a new DSL

## Status

Accepted.

## Context

Rules need conditions. There were three plausible options.

1. **A purpose built grammar**, parsed with a parser generator. Total control
   over syntax and semantics. Everybody has to learn it, the tooling is ours to
   build, and error messages are ours to get right.
2. **Real Python, executed.** Free tooling, free familiarity, and an execution
   model that can reach anything the host process can reach. Impossible to
   analyse statically once anyone uses dynamic access, and impossible to hash
   meaningfully once anyone closes over module state.
3. **A restricted subset of the Python grammar, parsed with `ast` and walked by
   our own interpreter.**

## Decision

Option three.

Expressions are parsed with `ast.parse(source, mode="eval")`, validated against
an allow list of node types, and evaluated by an interpreter in `expr.py`. There
is no `eval` and no `exec` anywhere in the engine.

## Consequences

Good:

* Rule authors already know the syntax. `any(m.rank == 'CP' for m in
  flight.roster)` needs no explanation.
* Editors, formatters and syntax highlighting work on the Python front end for
  free.
* The interpreter is the natural place to hang the read recording that the whole
  static analysis story depends on. There is exactly one walk, so the payload
  contract cannot disagree with the behaviour.
* Anything dangerous is refused at compile time, with a message naming the rule
  and the expression.

Bad:

* We own the interpreter, including its performance. A tree walk is slower than
  compiled Python. For guards that are two or three comparisons this does not
  matter; for arithmetic heavy expressions in a hot loop it eventually would.
* The subset is a promise. Every time somebody asks for a language feature we
  have to decide whether it can be analysed statically, and often the answer is
  no. That is a support burden, and it is also the mechanism that keeps rules
  small enough to reason about.
* Error messages are ours to write. They are currently good; keeping them good
  requires attention.

## Rejected alternative worth noting

Compiling expressions to Python bytecode with a restricted globals dictionary
would be considerably faster. It was rejected because a restricted globals
dictionary is a weak sandbox, and because the compiled form cannot be walked
symbolically, which would mean maintaining a second analyser alongside the
evaluator. Two analysers that must agree is exactly the failure mode this
project exists to remove.
