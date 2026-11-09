# partial-python

Execute append-only Python while source is still arriving. Python 3.11+, no runtime dependencies. Experimental API.

```sh
uv add 'partial-python==0.1.0a2'
```

## Input and completion

Use an `Executor` for each logical response. Feed source as it arrives, then explicitly finish or abort according to the provider's completion status.

```python
from partial_python import Executor

executor = Executor({"foo": foo, "bar": bar, "baz": baz})
await executor.feed("await foo(1)\n")  # Executes now.
await executor.feed("await bar(2)")  # Waits: an operator or attribute access may follow.
await executor.feed("\nawait baz(3)")  # Executes bar, buffers baz.
await executor.finish()  # Successful EOF permits baz.
```

**Units execute in source order, at most once each.** New input never reruns earlier units. A loop still performs its ordinary iterations, but its whole body must be sealed before execution starts.

`feed()` accepts new text deltas, not accumulated snapshots. `feed()` and `finish()` return completed `Execution` events. The executor retains `events`, `failed_unit`, sealed but unstarted `queued` units, and an immutable `parser_state` snapshot containing the unsealed `pending` tail. Read results from `executor.namespace`.

`finish()` declares successful source completion. On disconnects, token limits, cancellation, or failed responses, use `abort()` for an idle executor; cancel and await an active operation first. Failure is terminal: earlier effects remain, later units do not start, and nothing is retried. [Lifecycle and integration](docs/api.md).

## Async text iterators

When your adapter already supplies a validated async text iterator, `incremental_exec()` is a convenience wrapper. `validated_deltas` must yield raw source and end normally **only after successful generation**. Its adapter must raise on truncation, failure, or a missing completion signal.

```python
from partial_python import incremental_exec

namespace = await incremental_exec(validated_deltas, {"foo": foo, "bar": bar})
```

Execution starts while source is arriving; the await completes after execution and returns the namespace used. To consume events as each unit completes, use `Executor.stream()` and retain the executor for diagnostics. Close a partially consumed event iterator with `aclosing` in its owning task/context. [Streaming adapter](docs/api.md#stateful-streaming).

## Stable boundaries

| Input | Execution boundary |
| --- | --- |
| Top-level simple statement | A real logical newline or successful EOF. |
| `foo()` at temporary EOF | Wait; it can still become `foo() + bar()`. |
| `for`, `if`, `try`, `def`, and other compounds | Hold the entire statement until an unambiguous top-level successor or successful EOF. |
| `else`, `elif`, `except`, `finally`, or an ambiguous prefix such as `e` | Keep clauses attached; wait for ambiguous prefixes to resolve. |
| Blank lines and comments | Preserve them; they do not close a suite. |
| Decorators, brackets, line joins, and strings | Keep their complete logical statement together. |

`Parser.feed()` / `finish()` expose the same boundaries as immutable `CodeUnit` source slices without executing them. `pending` retains the uncommitted suffix. Every unit preserves original characters and source positions; no repaired text is executed. [Precise semantic contract](docs/semantics.md).

## Display completion

```python
from partial_python import Allow, complete

assert complete("ready = 1\nvalue = ['hel").source == "ready = 1\n"
preview = complete("value = ['hel", Allow.STRING | Allow.BRACKET)
assert preview.source == "value = ['hel']"
assert preview.provisional
```

The default returns only the stable prefix. `Allow` flags opt into provisional display; `prefix`, `suffix`, and `withheld` distinguish original text from synthetic text. A preview never authorizes execution, even when its suffix is empty. [Preview options and limits](docs/preview.md).

## Execution scope

Units share one namespace and support native top-level `await`. Execution requires an asyncio event loop; use the asyncio backend with AnyIO. `Parser` and `complete` are synchronous.

This uses sequential compilation units, with [explicit differences from whole-module execution](docs/semantics.md#execution-is-a-sequence-of-compilation-units). Future imports remain in the prologue, only the initial string sets the module docstring, and module-scope `global` declarations are rejected. On Python 3.14+, module annotations require an explicit `from __future__ import annotations`.

In-process execution provides no sandbox or rollback. Hosts own permissions, background tasks, source limits, and hard deadlines. The [integration examples](docs/api.md#host-examples) demonstrate transport completion, concurrent reception, and traceback source lifetime.

## Development

```sh
uv sync
uv run pytest
uv run basedpyright
uv run ruff check .
uv run ruff format --check .
uv run examples/streaming_tools.py
uv build
```

[API and transport](docs/api.md) · [Design](docs/design.md) · [Validation](docs/validation.md) · [Related implementations](docs/prior-art.md)
