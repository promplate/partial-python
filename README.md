# partial-python

Execute append-only Python while the source is still arriving. Python 3.11+, no runtime dependencies. Experimental API; not yet published.

`Executor` and `incremental_exec` require a running `asyncio` event loop. With AnyIO, use its asyncio backend. `Parser` and `complete` are synchronous and need no event loop.

```python
from partial_python import incremental_exec

async for event in incremental_exec(model_deltas, namespace={"foo": foo, "bar": bar}):
    print(event.unit.source)
```

Given `await foo(1)\nawait bar(2)`, `foo` runs when its newline arrives. `bar` waits for a newline or successful end of stream. Every executed character comes from the original source. Display completion is a separate API.

## Explicit input and completion

```python
from partial_python import Executor

executor = Executor({"foo": foo, "bar": bar, "baz": baz})
await executor.feed("await foo(1)\n")  # Executes now.
await executor.feed("await bar(2)")  # Still extendable; waits.
await executor.feed("\nawait baz(3)")  # Executes bar, buffers baz.
await executor.finish()  # Actual EOF permits baz.
```

`feed()` and `finish()` return a `tuple[Execution, ...]` containing the units completed by that call. `executor.state` is `"open"`, `"finished"`, `"failed"`, or `"aborted"`.

`finish()` means the source successfully completed. A network disconnect, cancelled request, or model token limit is not successful EOF. For an idle executor, `abort()` retains pending source for diagnosis without executing it. It cannot interrupt an active operation: cancel and await that operation’s owning task first. If an event stream is paused at a yielded event, close it with `aclose()` or `aclosing` in its owning task/context. Never turn these failures into `finish()`.

For async iterators, normal exhaustion means successful EOF; adapters **must raise** for truncation or transport failure. [The loopback transport example](examples/transport.py) demonstrates explicit `done(status, source)` validation. [Bounded concurrent reception](examples/overlap.py) shows how a host can keep receiving while a tool awaits. Final-source equality is a consistency check; it cannot undo effects already performed.

## Inspect a stream and its failures

```python
from contextlib import aclosing
from partial_python import Executor

executor = Executor({"foo": foo})
try:
    async with aclosing(executor.stream(model_deltas)) as events:
        async for event in events:
            print(event.index, event.unit.start, event.unit.end)
except Exception:
    print(executor.error)  # Recorded failure; retain its exception chain.
    print(executor.events)  # Units that completed successfully.
    print(executor.failed_unit)  # Attempted unit; effects may be partial or complete.
    print(executor.unexecuted)  # Sealed units never started.
    print(executor.parser.pending)  # Source not yet sealed.
    raise
```

A stream owns its executor until exhausted or closed. It attempts to close its acquired source iterator on every exit path. Use `aclosing` when a consumer might break early. `feed`, `finish`, `abort`, and another stream cannot intervene while a stream owns the executor; reentrant calls fail immediately. Construction of an iterator alone does not start execution. Keep iteration and closing in one task/context when source or executed code uses `ContextVar` tokens; `asyncio.timeout()` can bound that whole scope. [Task ownership](docs/codeact.md#task-ownership).

Exceptions and cancellation are terminal. Previous side effects remain, and no unit is replayed. `events` contains earlier successes even when one `feed()` call later raises. `failed_unit` is absent for input/transport errors that occur outside a unit. `parser` is an immutable diagnostic snapshot, not an input interface.

A unit can finish its body and produce all its effects after catching `CancelledError`. If it leaves the new cancellation request outstanding, the executor still raises cancellation, records `failed_unit`, and emits no success event. Missing events do not prove that effects are absent. A source that violates the close protocol by yielding during `aclose()` raises `RuntimeError`; closing failures are reported, not suppressed.

Each `Execution` records a monotonic start/completion time and the original `CodeUnit`; it does not capture expression values or stdout. Assign results in source (`result = await foo()`) and read them from `executor.namespace`. Reuse the same namespace with a new executor for another logical model response. This shares bindings, not compiler flags; each new stream must repeat any required future imports. With no namespace argument, the executor starts with `__name__ = "__main__"`. A supplied dictionary is used as-is; include `"__name__": "__main__"` when it should behave as a script. No module is registered in `sys.modules`. [Retaining a source file](examples/tracebacks.py) gives ordinary tracebacks their original source lines, including later calls to functions defined by the stream.

## What can be committed?

| Input boundary | Behavior |
| --- | --- |
| Simple statement plus real logical newline | Emit immediately. |
| A call ending in `)` at temporary EOF | Wait: an operator or attribute access can still follow. |
| `for`, `if`, `while`, `try`, `with`, `match`, `def`, `class` | Hold the entire statement until a distinct top-level successor or successful EOF. |
| `else`, `elif`, `except`, `finally` | Keep with the originating compound statement. |
| An ambiguous new name, such as `e` or `f` | Wait while it could still become a continuation clause. |
| Blank lines or comments | Preserve them; they do not seal a suite. |
| Decorators, bracket continuations, backslashes, strings | Keep together with their complete logical statement. |

`Parser.feed()` and `Parser.finish()` return `tuple[CodeUnit, ...]` without executing code:

```python
from partial_python import Parser

parser = Parser()
units = parser.feed("await foo(1)\nawait bar(")
assert units[0].source == "await foo(1)\n"
assert parser.pending == "await bar("
assert parser.feed("2)") == ()
assert parser.finish()[0].source == "await bar(2)"  # Successful EOF, no invented source.
```

`pending` is the uncommitted suffix. A `CodeUnit` is a compilation slice, not an atomic transaction or necessarily one AST statement: a semicolon chain stays together, and EOF may produce a trivia-only unit.

`start/end` are half-open Unicode-character offsets into original source. Lines are one-based cursor positions: a final newline places `end_line` on the following line; without one, it remains on the last line. CRLF, CR, comments, and whitespace are retained exactly.

## Display completion

```python
from partial_python import Allow, complete

stable = complete("ready = 1\nvalue = ['hel")
assert stable.source == "ready = 1\n"

preview = complete("value = ['hel", Allow.STRING | Allow.BRACKET)
assert preview.source == "value = ['hel']"
assert preview.provisional
assert preview.prefix + preview.withheld == "value = ['hel"
```

The default `Allow.NONE` returns only the stable prefix. Any nonzero policy also keeps already valid but unsealed source visible. `TAIL` permits that visibility without repairs; `STRING`, `BRACKET`, and `SUITE` opt into plain-literal endings, container/grouping closers, and empty-suite `pass` respectively. Calls needing invented closers/arguments and incomplete f/t strings remain withheld. No operand, dictionary value, or error inside the source is invented away.

`Completion.prefix`, `suffix`, and `withheld` distinguish original text from synthetic display text. A preview is never an execution authorization—even with an empty suffix. [Preview details](docs/preview.md).

## Semantic scope

Execution uses sequential interactive compilation units sharing one namespace, with top-level await enabled. It cannot be equivalent to compiling an unknown future module in one call: whole-module validation, frame introspection, constant identity, and compiler directives can differ. Future imports must remain in the prologue, only the initial string can set the module docstring, and module-scope `global` declarations are rejected.

On Python 3.14+, module-scope annotated assignments require an explicit `from __future__ import annotations`, because independent native annotation code objects would overwrite prior deferred annotations. Function/class annotations stay inside their complete scopes. [Precise contract and counterexamples](docs/semantics.md).

This is in-process Python, not a sandbox or transaction. Awaited operations cooperate with cancellation; CPU-bound code blocks the event loop, and code that suppresses/undoes cancellation cannot be forcibly stopped here. Background tasks survive unit boundaries and belong to the host's logical execution scope. Permission checks, hard timeouts, and process isolation belong in that host. [The POSIX worker example](examples/worker.py) demonstrates cross-process tool results and bounded cleanup. It terminates its own process group; it is not a sandbox and does not contain descendants that deliberately escape that group. From a repository checkout, use `from examples.worker import run_worker`; these examples are not installed as public package modules.

## Development

```sh
uv sync
uv run pytest
uv run basedpyright
uv run ruff check .
uv run python examples/streaming_tools.py
uv run python examples/transport.py
uv run python examples/overlap.py
uv run python examples/worker.py  # POSIX host prototype
uv build
```

Tests include every-character and random partition checks, AST-prefix invariants, real stdlib source, cancellation/transport failures, and deterministic work-count regressions. Fixtures and demos are engineering verification, not model evaluation evidence. [API and transport](docs/api.md) · [Design](docs/design.md) · [CodeAct integration](docs/codeact.md) · [Related implementations](docs/prior-art.md).
