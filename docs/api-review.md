# API review and transport prototype

Status: design review with a validated example-only protocol. The core now exposes the reviewed `Executor.stream()` lifecycle; no vendor adapter is included. The transport tests are deterministic engineering fixtures, not model evaluations.

## Recommended public shape

Keep the small entry points below with separate contracts:

| Entry point | Input | Output | Purpose |
| --- | --- | --- | --- |
| `complete(source, allow=...)` | Current full source snapshot | `Completion` with original prefix, synthetic suffix, withheld source and committed offset | Display an explicitly provisional preview. Never feed its generated suffix to execution. |
| `Parser.feed(delta)` / `finish()` | Append-only raw Python deltas / explicit EOF | Immutable original-source `CodeUnit` ranges | Integrate with another execution runtime. |
| `Executor.feed(delta)` / `finish()` / `abort()` | Same source protocol | Completed `Execution` events and inspectable state | Own provider status handling explicitly. |
| `Executor.stream(deltas)` | A validated async text iterator | Completed events with retained executor diagnostics | Stateful pull-based consumption. |
| `incremental_exec(deltas)` | The same iterator | The same events and exceptions | Convenience wrapper returning an executor stream directly. |

`feed` always means a new delta. It must not accept whole accumulated snapshots implicitly: guessing whether repeated text is a retransmission would make legitimate repeated Python indistinguishable from duplication. A snapshot adapter should independently enforce `new.startswith(previous)` and emit only the new suffix; a changed prefix is terminal. A reconnect adapter needs its own durable replay/offset policy and must not feed an already executed prefix again.

The execution rule should not gain an eager mode that closes an unfinished loop or call. Configuration belongs to preview detail, caller namespace, diagnostics, host resource limits, and transport ownership. Waiting longer is safe; inventing an execution boundary is not a tuning option.

## Lifecycle and diagnostics

`finish()` declares authoritative successful source completion. It can release a final call without a newline or an entire compound suite. It is not a generic cleanup method. `abort()` abandons the stream while preserving evidence; `finally: await finish()` is therefore the wrong integration pattern.

The current diagnostic model is useful when its distinctions are explicit:

| Field | Meaning |
| --- | --- |
| `events` | Units that completed successfully; immutable snapshots. |
| `failed_unit` | The attempted unit whose completion was not acknowledged; partial or complete effects may exist. A parser EOF error has no fabricated attempted unit. |
| `unexecuted` | Accepted, sealed units that have not started. They are retained for diagnostics, not automatic retry. |
| `parser.pending` | Source still awaiting a grammatical boundary. |
| `parser.committed` | Number of source characters accepted by the parser; this is not an execution watermark. |
| `error` | The recorded terminal failure; rejected later calls must not replace it. Native generator control exceptions may be wrapped when propagated; see [exception semantics](semantics.md#failures-cancellation-and-effects). |
| `state` | Open, successfully finished, failed, or explicitly aborted. |

Successful events plus the failed range, queued ranges and pending tail preserve the accepted source after a mid-chunk failure. Syntax errors retain the original filename and line numbers. Cooperative cancellation is terminal, including when a producer or statement catches `CancelledError` without clearing the new cancellation request. This is not a hard deadline for code that loops forever or deliberately removes cancellation state.

Catching `CancelledError` can let a unit run its remaining body, assign its result, and return normally. Without `uncancel()`, the outstanding new cancellation still causes `failed_unit` and no success event. The event journal is an acknowledgement record, not a rollback log or proof that an unacknowledged unit had no effects.

Source ranges use half-open character offsets and one-based cursor lines. A final newline advances `end_line`; without it, `end_line` stays on the final physical line. EOF may emit a trivia-only `CodeUnit`, so event counts are neither AST statement counts nor tool-call counts.

### Stateful streaming is now implemented

`Executor.stream(chunks) -> AsyncGenerator[Execution, None]` gives streaming callers access to the executor's diagnostics after failure. `incremental_exec()` is a plain convenience function returning that stream directly, so it does not add another async-generator cleanup boundary or duplicate the execution lifecycle.

The stream reserves the executor from its first consumption through exhaustion or explicit close, including source waits and suspended event yields. Interleaved `feed`, `finish`, `abort`, or another stream are rejected. Merely constructing an unstarted stream does not acquire the source or reserve the executor. It can continue a prefix already accepted through `feed`.

The method maintains the same queue/error journal as direct calls and attempts to close its acquired source iterator on exit. Normal validated source exhaustion permits `finish`; early consumer close abandons pending source and retains the unexecuted queue. Runtime failure, invalid EOF, or source failure retain their original exception and source diagnostics. Ordinary cleanup errors must not replace an existing execution failure. New cancellation or process-level interrupts during cleanup propagate with the original failure in Python’s exception chain and leave a failed terminal state.

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
    inspect_failure(executor.error, executor.failed_unit, executor.unexecuted, executor.parser.pending)
    raise
```

Exceptions from `display_completed()` belong to the caller; `executor.error` records source, execution and cleanup failures inside the executor. `aclosing` also closes the stream when the consumer raises or stops early.

Completed events plus raised exceptions are enough for the first API. Host UIs can report a logical stream as running while awaiting a completion; a future started-event API should follow a real integration need, with a clear distinction between compilation, dispatch and completion. Do not emit a failed event and then also raise the same failure without a reason.

## Minimal transport adapter

`examples/transport.py` demonstrates two NDJSON records over a single loopback TCP connection:

```json
{"type": "delta", "text": "await tool('early')\n"}
{"type": "done", "status": "completed", "source": "await tool('early')\n"}
```

The adapter yields only `text` from delta records. A completed done record must contain the exact concatenation of those deltas. It then ends the text iterator normally, permitting `incremental_exec()` to finalize Python. The terminal record ends this logical stream; the example does not multiplex further streams on the connection.

`truncated`, `cancelled`, and `failed` terminal statuses raise `SourceAborted`. A closed socket without a completed done record, a partial wire record, invalid JSON/UTF-8, wrong field types, unknown status, or different final source raises `SourceProtocolError`. None of these paths turns an unfinished suite into a completed program. An explicit completed record is required even for an empty source stream.

Exact final source equality is a consistency check, not authorization or rollback. A mismatch discovered at the end cannot undo a previously executed statement. Hosts must select the intended tool call, validate its identity and permissions before feeding source, and keep nested tool authorization intact. The small example intentionally has no start-record handshake, sequence numbers, reconnects, hashes or provider dependencies because one ordered connection already owns one known source stream.

The source reader bounds each wire record, and the example bounds total source characters because it retains text for final equality. These are example host limits, not universal library limits. They fail rather than forcing a flush. The socket and server are closed in the same run; no background daemon remains.

### Raw source versus streamed JSON tool arguments

The parser accepts Python characters, not a JSON envelope or markdown. A raw custom-tool input delta is the smallest integration. With a JSON tool schema such as `{"code": "..."}`, a separate decoder must emit only irrevocably decoded characters from the `code` string. It must retain incomplete escape sequences and surrogate pairs and reject changes to an already emitted prefix. Do not execute a repaired partial-JSON value, strip code fences speculatively, or feed a provider's final full source again after its deltas.

The example's JSON is record framing: each record is decoded completely before its Python string is emitted. It is not an implementation of partial JSON argument decoding.

## Receive/execution concurrency

The default should remain pull-based: obtaining the next completed event permits execution to continue and eventually requests more source. It naturally applies backpressure and has no hidden producer task. The TCP reader and operating system may buffer incoming bytes; that does not mean Python execution is concurrent or reordered.

For a host that needs reception while an awaitable tool is running, use an explicit bounded producer queue with separate successful-completion, producer-failure and cancellation signals. Queue exhaustion alone is not EOF. If failure is observed while a tool is running, the host must decide whether to let that already-started unit finish or cancel it; either choice must prevent later queued units from starting. Preserve FIFO order, do not automatically retry side effects, and close both source and worker lifetimes. A bounded queue is an adapter concern until a real integration demonstrates the right public abstraction.

## Evidence from the prototype

The loopback server sends the first closed await statement and waits for the tool itself to signal execution before it transmits the remainder. This proves early execution without relying on sleep durations. It then fragments the loop's record into three-byte writes. Only a completed terminal status releases the final loop; truncated, canceled, and disconnected cases retain only the earlier side effect.

`tests/test_transport.py` also checks final-source mismatch, no-newline final calls, bare EOF, partial records, malformed frames, Unicode/escape preservation across byte fragments, record/source limits, empty completion, and early consumer close. Closing the event iterator closes the socket and leaves the rest of a multi-statement chunk unexecuted.

Run the prototype with `uv run examples/transport.py` and its tests with `uv run --group dev pytest tests/test_transport.py`. No model endpoint, credential, or fabricated model-quality result is involved.
