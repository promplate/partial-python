# What “safe” means

`partial-python` separates a syntactic promise from an execution model. A statement becoming parseable at the current end of a buffer is insufficient evidence to execute it.

## The append-only contract

Let `P` be the source received so far and `C` the source already committed. `C` is an exact, contiguous prefix of `P`: no inserted quotes, brackets, `pass`, indentation, or newlines. For every suffix `S` for which `P + S` is valid under the stream's Python grammar, the committed top-level statements remain an unchanged prefix of its statement AST. Source locations may be translated when comparing standalone slices.

This promise concerns the structure of statements, not every observable property of compiling their surrounding module. Invalid later source cannot retract effects that already happened. An append-only producer cannot revise an earlier token; edits require a new stream.

`feed()` means more characters may arrive. Only `finish()` declares actual EOF. A dropped connection, cancellation, length limit, or failed model response must abort the stream instead of calling `finish()`. A normal async-iterator end is therefore appropriate only when the adapter knows the source completed successfully.

## Boundaries

| Source received | Reason to wait |
| --- | --- |
| `foo()` | Can become `foo() + bar()` before the logical newline. |
| `foo() # comment` | The physical line is still open. |
| `x = (foo()\n` | A physical newline inside brackets is not a logical newline. |
| `x = foo() \\\n` | Explicit line joining keeps the expression open. |
| A `for` suite ending with a newline | More body statements or an `else` clause may follow. |
| A complete `if` and its first suite | `elif` or `else` may follow. |
| A complete `try`/`except` | Another handler, `else`, or `finally` may follow. |
| A complete `def`, `class`, `with`, or `match` | The indented body may continue. |
| `@decorate` | Decorators belong to the following definition. |
| A compound followed by the character `e` | The token may still become `else`, `elif`, or `except`. |

A real logical newline seals a top-level simple statement. All semicolon-separated statements on that line are one execution unit. A compound waits for a real, unambiguous top-level successor or explicit EOF. Blank lines and comments do not close suites. One-line compound suites obey the same rule because their continuation clauses can still arrive later.

The standard tokenizer creates virtual `NEWLINE` and `DEDENT` tokens at EOF. A parser repeatedly run against a growing buffer therefore reports apparent closures that are not stream boundaries. Likewise, `codeop.compile_command(..., symbol="exec")` accepts a loop whose body can still grow. Neither result alone proves append stability. Python's [lexical rules](https://docs.python.org/3.14/reference/lexical_analysis.html#indentation) and [compound grammar](https://docs.python.org/3.14/reference/compound_stmts.html) explain these distinctions.

All three Python newline conventions are accepted. CRLF remains one physical newline even when transport chunks split between CR and LF. CodeUnit offsets refer to original Unicode characters, not UTF-8 bytes or normalized source. The host interpreter defines the grammar; a Python 3.11 host does not gain Python 3.14 syntax merely because the package also supports that host version.

## Execution is a sequence of compilation units

Execution shares one globals dictionary and runs units sequentially, including top-level `await`. Every compound stays in a single unit, so loops, exception handlers, context managers, function bodies, and class bodies retain their ordinary internal control flow. There is no implicit task parallelism.

It is impossible to guarantee arbitrary whole-module equivalence while executing before EOF. These executable counterexamples matter even when lexical boundaries are correct:

```python
seen = "__annotate__" in globals()
x: int
```

On Python 3.14, compiling this as one module installs its deferred annotation function before evaluating `seen`; splitting it into units does not. Separately compiling `x: int` and `y: str` also replaces the first module annotation function, losing the earlier annotation. The executor rejects module-scope annotated assignments on Python 3.14+ unless string annotations were explicitly enabled with `from __future__ import annotations`. This includes annotations inside module-level control flow. Function and class annotations remain inside their complete containing definition and do not need this restriction. The restriction avoids silent data loss; it does not retroactively promise whole-module equivalence.

The default guard also includes attribute, subscript and parenthesized targets. Although these do not create a new `__annotate__` function, CPython 3.14/3.15 still [resets the module's conditional-annotation set](https://github.com/python/cpython/blob/v3.14.10/Python/codegen.c#L871-L878), which an existing annotation function [reads when returning entries](https://github.com/python/cpython/blob/v3.14.10/Python/codegen.c#L775-L787). For example, native `exec("x: int", namespace)` followed by native `exec("(y): str", namespace)` leaves the old function present but changes its result from `{"x": int}` to `{}`. Explicit future annotations select string-annotation semantics for this stream; they do not promise to preserve arbitrary preexisting deferred-annotation machinery in a supplied namespace.

```python
a = (1000, 2000)
b = (1000, 2000)
same = a is b
```

Whole-module constant pooling can produce different object identity from separately compiled units. Frame inspection, code-object constants, tracing, and compilation audit events can also observe the unit boundaries. Programs that depend on those observations must use ordinary whole-module execution.

Other deliberate rules address common accidental differences:

- Only the first statement can supply the stream's module docstring; later standalone strings must not overwrite `__doc__` merely because they begin a new unit.
- Future imports must remain in the initial prologue and their compiler flags persist across units. The library must not inherit its own implementation module's future flags accidentally.
- Module-scope `global` declarations are rejected. In ordinary files their validity depends on earlier uses throughout the module; the interactive prompt relaxes this rule. Declarations inside a complete function or class remain ordinary Python.
- A parseable AST still needs compilation before execution. AST-only parsing does not perform all scope checks: for example, standalone `return 42` parses but cannot execute.

Compiler warnings can be emitted by speculative AST probes as well as final compilation. AST-stage warnings may repeat and use unit-relative line numbers, so line-specific warning filters are not equivalent to whole-file compilation. Broad warning-as-error policy remains effective. The library does not change process-global warning filters; suppressing or replaying them would introduce concurrency and exception-semantics differences.

These distinctions follow the documented [future/global rules](https://docs.python.org/3.14/reference/simple_stmts.html#future), [interactive compiler state](https://docs.python.org/3.14/library/codeop.html), and [AST compilation caveat](https://docs.python.org/3.14/library/ast.html#ast.parse).

## Failures, cancellation, and effects

A completed event means that execution of its unit returned normally. Its timestamps and original source range identify that unit. If compilation, execution, or awaiting raises, the executor becomes terminal. Ordinary Python exception and coroutine rules apply. Earlier effects remain. The failing unit may already have performed effects before it raised; cancellation has the same limitation. No implicit retry is safe.

An ordinary source-cleanup error does not replace an execution failure already in flight; it is attached as a best-effort exception note. A new cancellation or process-level exception during cleanup does propagate and becomes `executor.error`; Python’s native `__context__` chain retains the earlier failure. Hosts should retain or render the full exception chain, not only the terminal message.

Iterator control exceptions keep Python’s native behavior. `StopIteration` escaping a coroutine becomes `RuntimeError`; `StopAsyncIteration` raised by executed code becomes `RuntimeError` at the event-stream boundary rather than successful source exhaustion. Its original instance remains in `executor.error` and the propagated exception’s `__cause__`. Conversely, Python’s [`agen.aclose()` protocol](https://docs.python.org/3.14/reference/expressions.html#agen.aclose) consumes a propagated `GeneratorExit`, even one originating in a source closer; the executor still records that cleanup failure. A `GeneratorExit` from source acquisition, reading, or executed code is a failure, distinct from a consumer closing at an event yield.

The executor rejects overlapping or recursive writes instead of silently reordering them. Backpressure preserves execution order but can delay reading additional model tokens while a tool call runs. An adapter that needs concurrent network ingestion can buffer text separately with an explicit limit; executing source still remains sequential.

This is language-boundary safety, not a sandbox, rollback mechanism, or distributed exactly-once protocol. An external worker can enforce isolation and resource limits independently of these parsing rules.

See the [validation guide](validation.md) for conformance checks and their scope.
