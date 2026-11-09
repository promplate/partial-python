# API and transport integration

## Public entry points

Start with `Executor.feed()` / `finish()` / `aclose()` when integrating a model response. They keep source reception and the provider's completion status explicit. The async iterator APIs adapt sources that already validate successful completion.

| Entry point | Input | Output | Purpose |
| --- | --- | --- | --- |
| `Executor.feed(delta)` / `finish()` | Append-only raw Python deltas / explicit EOF | Tuple of completed `Execution` events | Own provider status handling explicitly. |
| `await Executor.aclose()` | No arguments; executor must be idle | `None`; awaits open context cleanup | Abandon pending source without declaring EOF. |
| `Executor.abort()` | No arguments; executor must be idle with no open context | `None`; retains diagnostic state | Synchronous abandonment when no context needs cleanup. |
| `await incremental_exec(deltas)` | A validated async text iterator | The namespace used, after successful execution | Convenience wrapper when per-unit events are unnecessary. |
| `Executor.stream(deltas)` | The same validated iterator | Completed events with retained executor diagnostics | Consume events as each unit completes. |
| `Parser.feed(delta)` / `finish()` | Append-only raw Python deltas / explicit EOF | Immutable original-source `CodeUnit` ranges | Integrate with another execution runtime. |
| `complete(source, allow=...)` | Current full source snapshot | `Completion` with original prefix, synthetic suffix, withheld source and committed offset | Display an explicitly provisional preview. Never feed its generated suffix to execution. |

A validated iterator ends normally only after authoritative successful generation. Truncation, failure, or a missing completion signal must raise; normal exhaustion authorizes `finish()`.

`feed` always means a new delta. It must not accept whole accumulated snapshots implicitly: guessing whether repeated text is a retransmission would make legitimate repeated Python indistinguishable from duplication. A snapshot adapter should independently enforce `new.startswith(previous)` and emit only the new suffix; a changed prefix is terminal. A reconnect adapter needs its own durable replay/offset policy and must not feed an already executed prefix again.

`stream_blocks` selects where stable original statements may execute before their enclosing block closes. It never repairs source or invents an execution boundary. Preview permissions remain separate: `Allow` values cannot be passed as execution options.

## Namespace and results

`Executor(namespace=None, *, filename="<partial-python>", stream_blocks=MODULE)` uses a supplied dictionary as-is. Without one, it creates a namespace with `__name__ = "__main__"`. Include that binding explicitly when a supplied namespace should behave as a script. No module is registered in `sys.modules`.

`await incremental_exec(validated_deltas, namespace=None, *, filename="<partial-python>", stream_blocks=MODULE)` follows the same namespace rules and returns that exact dictionary on success. It executes while source arrives; the await finishes only after execution and source cleanup complete. Keep an `Executor` when you need diagnostics after failure; use its `stream()` when you also need events as each unit completes.

Each `Execution` records its index, original completed top-level `unit`, and monotonic `started_at` / `completed_at` times. Streaming a body can cause effects before any event exists; its enclosing top-level unit emits only on successful completion. It does not capture expression results or stdout. Assign results in source (`result = await foo()`) and read them from `executor.namespace`.

Reuse the namespace with a new executor for another logical model response. Bindings persist; compiler flags do not. Each new stream must repeat its required future imports. See [compilation-unit semantics](semantics.md#execution-is-a-sequence-of-compilation-units).

## Block selection

`Block` is a `Flag`, not an integer mask. Its members have identical aliases in `partial_python` and `partial_python.blocks`; use either `Block.IF` or `IF`.

| Option | Effect |
| --- | --- |
| `NONE` | Buffer the whole module and compile it once at successful EOF. |
| `MODULE` | Execute complete top-level units as they arrive; this is the default. |
| `IF` | Stream stable statements in the selected `if` / `elif` / `else` suite. Each reached condition is evaluated once. |
| `WITH` | Enter synchronous contexts once, stream their stable bodies, and exit when the block closes. |
| `ASYNC_WITH` | The same behavior with awaited context entry and exit. |
| `ALL` | Exactly `MODULE | IF | WITH | ASYNC_WITH`; no unimplemented block kinds. |

`stream_blocks` replaces the default selection. `MODULE | IF` enables top-level and conditional streaming; `IF` alone does not. Streaming also requires enabled ancestors: `MODULE | IF` cannot stream an `if` nested inside an unselected `with` or a loop. Every selection without `MODULE` buffers and compiles the whole module at EOF, preserving native module-scope `global` declarations and Python 3.14 deferred annotations. Whole-module mode emits one `Execution` even for empty input, which still initializes a missing `__builtins__` binding as native `exec("")` does. `stream_blocks` rejects integers, booleans, and preview `Allow` values. The executor exposes the selected value as its read-only `stream_blocks` property.

```python
from partial_python import IF, MODULE, Executor

effects = []
executor = Executor({"effects": effects}, stream_blocks=MODULE | IF)
assert await executor.feed("if True:\n    effects.append('early')\n") == ()
assert effects == ["early"]  # Body effects precede the completed top-level event.
assert len(await executor.finish()) == 1
```

`ALL` is opt-in because open contexts span source reception. A real `asyncio.timeout(...)` can expire while the executor awaits the next source chunk; the default `MODULE` has not entered that context yet. Locks, context variables, and transactions likewise retain their lifetime until the block closes. `for`, `while`, `try`, `match`, function and class definitions still wait for their complete statement.

## Lifecycle and diagnostics

`finish()` declares authoritative successful source completion. It can release a final call without a newline or an entire compound suite. It is not a generic cleanup method. `await executor.aclose()` abandons pending source and awaits open context exits; `abort()` provides synchronous abandonment only when no context is open. Neither declares EOF. Cancel and await an active operation before closing the executor; `finally: await finish()` is the wrong integration pattern.

Operations spanning an open streaming block must run in the task that opened it, including `finish()` and `aclose()`. Ordinary body exceptions follow native context-manager suppression: suppression skips the rest of that with-body, then execution can resume after it. Source failure, invalid EOF, and cancellation remain terminal even when a manager returns a suppression value. An exit method can still raise a replacement exception, as with native Python contexts. A successful explicit `aclose()` leaves state `"aborted"` and no invented error.

The current diagnostic model is useful when its distinctions are explicit:

| Field | Meaning |
| --- | --- |
| `events` | Top-level units that completed successfully; immutable snapshots, excluding unfinished blocks with early effects. |
| `failed_unit` | The attempted original-source range whose completion was not acknowledged; it may be a streaming block header or an inner body statement. Partial or complete effects may exist. A parser EOF error has no fabricated attempted unit. |
| `queued` | Accepted, sealed units that have not started. Excludes the unsealed tail; retained for diagnostics, not automatic retry. |
| `parser_state` | Immutable `ParserState` snapshot of `pending`, `committed`, and `closed`; read the property again for current values. |
| `parser_state.pending` | Source still awaiting a grammatical boundary. |
| `parser_state.committed` | Number of source characters sealed by the parser; this is not an execution watermark. |
| `parser_state.closed` | Whether the parser received EOF, including invalid EOF; use executor `state` for execution lifecycle. |
| `error` | The recorded terminal failure; rejected later calls must not replace it. Native generator control exceptions may be wrapped when propagated; see [exception semantics](semantics.md#failures-cancellation-and-effects). |
| `state` | `ExecutorState`: `"open"`, `"finished"`, `"failed"`, or `"aborted"`. |

A `CodeUnit` represents an exact source span; the API or field returning it determines whether that span is a completed top-level unit. With the default `MODULE`, successful events plus the failed range, queued ranges and pending tail preserve the accepted source after a mid-chunk failure. With block streaming, an unsealed pending range may already have effects, and inner failure ranges can overlap it; these fields are diagnostics, not a resumable execution offset. Syntax errors retain the original filename and line numbers. Cooperative cancellation is terminal, including when a producer or statement catches `CancelledError` without clearing the new cancellation request. This is not a hard deadline for code that loops forever or deliberately removes cancellation state.

Catching `CancelledError` can let a unit run its remaining body, assign its result, and return normally. Without `uncancel()`, the outstanding new cancellation still causes `failed_unit` and no success event. The event journal is an acknowledgement record, not a rollback log or proof that an unacknowledged unit had no effects.

Source ranges use half-open character offsets and one-based cursor lines. A final newline advances `end_line`; without it, `end_line` stays on the final physical line. EOF may emit a trivia-only `CodeUnit`, so event counts are neither AST statement counts nor tool-call counts.

### Stateful streaming

`Executor.stream(chunks) -> AsyncGenerator[Execution, None]` gives streaming callers per-unit events and access to diagnostics after failure. `incremental_exec()` consumes the same stream in the calling task, handling closure internally without a second execution state machine.

The stream reserves the executor from its first consumption through exhaustion or explicit close, including source waits and suspended event yields. Interleaved `feed`, `finish`, `abort`, `aclose`, or another stream are rejected. Merely constructing an unstarted stream does not acquire the source or reserve the executor. It can continue a prefix already accepted through `feed`.

The method maintains the same queue/error journal as direct calls and attempts to close its acquired source iterator on exit. Normal validated source exhaustion permits `finish`; early consumer close abandons pending source and retains the unexecuted queue. Runtime failure, invalid EOF, or source failure retain their exception and source diagnostics, including native exception replacement by an open context exit. Ordinary source-iterator cleanup errors must not replace an existing execution failure. New cancellation or process-level interrupts during cleanup propagate with the original failure in Python’s exception chain and leave a failed terminal state.

An async generator that yields while processing `aclose()` violates Python's close protocol and raises `RuntimeError`. With no earlier failure, the executor records that error and enters `failed`; after an execution failure, an ordinary cleanup exception is attached to the original error as a best-effort note. Broken exception formatting cannot replace that primary failure; a new cancellation or process-level interrupt still propagates. The library does not suppress the violation or promise that a broken iterator has finished releasing its resources.

```python
from contextlib import aclosing
from partial_python import Executor

executor = Executor(namespace)
try:
    async with aclosing(executor.stream(validated_deltas)) as events:
        async for event in events:
            display_completed(event)
except Exception:
    inspect_failure(executor.error, executor.failed_unit, executor.queued, executor.parser_state.pending)
    raise
```

Exceptions from `display_completed()` belong to the caller; `executor.error` records source, execution and cleanup failures inside the executor. `aclosing` also closes the stream when the consumer raises or stops early.

## Minimal transport adapter

`examples/transport.py` demonstrates two NDJSON records over a single loopback TCP connection:

```json
{"type": "delta", "text": "await tool('early')\n"}
{"type": "done", "status": "completed", "source": "await tool('early')\n"}
```

The adapter yields only `text` from delta records. A completed done record must contain the exact concatenation of those deltas. It then ends the text iterator normally, permitting the executor to finalize Python. The terminal record ends this logical stream; the example does not multiplex further streams on the connection.

`truncated`, `cancelled`, and `failed` terminal statuses raise `SourceAborted`. A closed socket without a completed done record, a partial wire record, invalid JSON/UTF-8, wrong field types, unknown status, or different final source raises `SourceProtocolError`. None of these paths turns an unfinished suite into a completed program. An explicit completed record is required even for an empty source stream.

Exact final source equality is a consistency check, not authorization or rollback. A mismatch discovered at the end cannot undo a previously executed statement. Hosts must select the intended tool call, validate its identity and permissions before feeding source, and keep nested tool authorization intact. The small example intentionally has no start-record handshake, sequence numbers, reconnects, hashes or provider dependencies because one ordered connection already owns one known source stream.

The source reader bounds each wire record, and the example bounds total source characters because it retains text for final equality. These are example host limits, not universal library limits. They fail rather than forcing a flush. The socket and server are closed in the same run; no background daemon remains.

### Raw source versus streamed JSON tool arguments

The parser accepts Python characters, not a JSON envelope or markdown. A raw custom-tool input delta is the smallest integration. With a JSON tool schema such as `{"code": "..."}`, a separate decoder must emit only irrevocably decoded characters from the `code` string. It must retain incomplete escape sequences and surrogate pairs and reject changes to an already emitted prefix. Do not execute a repaired partial-JSON value, strip code fences speculatively, or feed a provider's final full source again after its deltas.

The example's JSON is record framing: each record is decoded completely before its Python string is emitted. It is not an implementation of partial JSON argument decoding.

## Task ownership

The standalone executor does not own tasks created by user code. `asyncio.create_task()` must survive parser flushes so a later statement can await the resulting task. Closing or failing a stream does not imply canceling every task in the process: that would also kill unrelated application work. Applications needing scoped cleanup should supply their own task registry or task group around a logical model call. Hard timeouts for CPU-bound code require a process boundary; canceling an asyncio task only works when execution cooperates.

Units execute in the calling task's context, with no hidden task per unit. A single consuming task therefore preserves `ContextVar` changes and tokens across units, which matters for output routing. A shared globals dictionary does not transfer task-local context between separate calls to `asyncio.create_task(anext(events))`; moving an open streaming block to another task is rejected. Consume the logical stream in one task and place `asyncio.timeout(...)` around that scope when its context must persist; a source iterator's own context-manager tokens may also require that lifetime.

For notebook integration, retain a single logical cell's output/history lifecycle while executing its sealed statements. Calling `run_cell_async()` separately for each statement changes `_`, `Out[n]`, final-expression display, transform semantics, and task cleanup. A raw-Python execution backend is the initial default; IPython `%`, `!`, and `%%` forms need their own cell-level contract.

## Receive/execution concurrency

The default is pull-based: obtaining the next completed event permits execution to continue and eventually requests more source. It naturally applies backpressure and has no hidden producer task. The TCP reader and operating system may buffer incoming bytes; that does not mean Python execution is concurrent or reordered.

For a host that needs reception while an awaitable tool is running, use an explicit bounded producer queue with separate successful-completion, producer-failure and cancellation signals. Queue exhaustion alone is not EOF. If failure is observed while a tool is running, the host must decide whether to let that already-started unit finish or cancel it; either choice must prevent later queued units from starting. Preserve FIFO order, do not automatically retry side effects, and close both source and owning task lifetimes. A bounded queue is an adapter concern until a real integration demonstrates the right public abstraction.

## Host examples

Run an example from a checkout with `uv run examples/<name>.py`. They are not installed as public package modules; a checkout can import them as `examples.transport`, `examples.overlap`, and `examples.tracebacks`.

| Example | Host responsibility |
| --- | --- |
| [streaming_tools](../examples/streaming_tools.py) | Show early execution, whole-compound holdback, ordered awaits, and explicit EOF. |
| [compare](../examples/compare.py) | Compare live incremental and whole-module execution on the same simulated source stream: `calls`, `loop`, `syntax`, or `truncated`. |
| [transport](../examples/transport.py) | Validate completion over a real loopback TCP connection with fragmented records. |
| [overlap](../examples/overlap.py) | Receive through a bounded queue while a tool awaits; propagate source failure and cancellation. |
| [tracebacks](../examples/tracebacks.py) | Write original source before execution and retain the file while generated functions or tracebacks remain in use. |

The traceback recipe writes UTF-8 without newline translation; it requires an absent or UTF-8 source encoding cookie, because traceback readers honor cookies even though `compile(str)` does not.
