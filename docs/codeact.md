# CodeAct integration research

This note records current local source inspected on 2026-11-02. The existing Codex checkout is a dirty `py-codeact-poc` development branch; none of those files were changed for this project. The examples and tests here are deterministic protocol fixtures, not real model evaluations or evidence of improved online behavior.

## Recommended execution lifecycle

A model tool call is one logical execution stream. The parser emits stable original statements within that stream; emitting a statement does not create a new notebook cell, close the tool dispatch gate, or cancel background tasks.

1. Accept append-only decoded text deltas and retain source positions. A candidate AST is not executable merely because an error-recovering parser could construct it.
2. Commit a source range only after its syntax is sealed. Keep `committed`, `started`, and `completed` distinct: acceptance does not imply that its effects happened.
3. Execute committed statements sequentially against one namespace. Await each statement before starting its successor. Preserve all original compound scopes as one unit.
4. On execution failure or cancellation, terminate this execution stream. Earlier effects remain; queued statements must not execute, and uncertain or failed statements must not be retried automatically.
5. Normal transport completion explicitly seals pending source. A dropped connection, provider truncation, canceled reader, or consumer closing the event iterator must abandon the tail instead of treating it as EOF.
6. Keep a completed-event journal with sequence numbers and original source ranges. A completed final tool call must not cause the full accumulated code to execute again.

`Executor.feed()` provides explicit control; `Executor.stream()` and `incremental_exec()` provide pull-based backpressure and yield one completed unit at a time. They can stop between units delivered in the same text chunk. [The bounded reception example](../examples/overlap.py) overlaps network ingestion with awaited execution while retaining that order. Its owning task group propagates producer failures and cancellation; a full queue also delays reading later transport status records.

### Task ownership

The standalone executor does not own tasks created by user code. `asyncio.create_task()` must survive parser flushes so a later statement can await the resulting task. Closing or failing a stream does not imply canceling every task in the process: that would also kill unrelated application work. Applications needing scoped cleanup should supply their own task registry or task group around a logical model call. Hard timeouts for CPU-bound code require a process boundary; canceling an asyncio task only works when execution cooperates.

Units execute in the calling task's context, with no hidden task per unit. A single consuming task therefore preserves `ContextVar` changes and tokens across units, which matters for output routing. A shared globals dictionary does not transfer task-local context between separate calls to `asyncio.create_task(anext(events))`. Consume the logical stream in one task and place `asyncio.timeout(...)` around that scope when its context must persist; a source iterator's own context-manager tokens may also require that lifetime.

For notebook integration, retain a single logical cell's output/history lifecycle while executing its sealed statements. Calling `run_cell_async()` separately for each statement changes `_`, `Out[n]`, final-expression display, transform semantics, and task cleanup. A raw-Python execution backend is the initial default; IPython `%`, `!`, and `%%` forms need their own cell-level contract.

## Compile-unit differences

The contract is sequential interactive compilation units sharing globals, not equivalence to compiling an unknown entire module. Whole-file syntax errors, directives, annotations, scheduling, introspection, and compilation-unit metadata can distinguish the two. No syntax mechanism can roll back an already performed external side effect.

- Carry explicit `__future__` flags forward; reject late future imports after the stream prologue.
- Only the first source string can become the module docstring. Subsequent standalone strings must not reset `__doc__` just because they start another code object.
- Reject module-scope `global` declarations; a later directive can otherwise affect earlier compilation. Declarations inside a function stay within that complete function's scope.
- On Python 3.14+, reject module-scope annotated assignments unless the source explicitly enables `from __future__ import annotations`. This also includes annotations nested in module-level `if`, `for`, or `try` suites; complete function and class scopes are separate.

The annotation policy comes from a direct Python 3.14.10 experiment. Executing `x: int = 1` and `y: str = "s"` as two code objects replaces namespace `__annotate__`, whose result then contains only `y`; compiling both in one module returns both annotations. With the explicit future flag, separate units merge the string annotations in `__annotations__`. Silently injecting that flag would change the caller's annotation semantics, so the executor requires the caller's explicit choice instead. This policy avoids lost annotations; it does not claim whole-module reflection equivalence.

## Exact local reference map

All paths below are absolute. Line ranges refer to the inspected files, not a released package.

| Concern | Source and lines | Finding |
| --- | --- | --- |
| Persistent IPython namespace | `/Users/muspi-merol/Desktop/macaron/dsh-py-codeact/py/kernel.py:758-795` | Each shell has an `InteractiveShell(user_ns=...)`; shells share a process, imports and event loop. ContextVar routing isolates output and display hooks. |
| Display/history | `/Users/muspi-merol/Desktop/macaron/dsh-py-codeact/py/kernel.py:812-816` | `_` and `Out[n]` persist; final-expression results are captured without a duplicate echo. |
| Await/error/cancellation | `/Users/muspi-merol/Desktop/macaron/dsh-py-codeact/py/kernel.py:868-904` | `transform_cell` and `run_cell_async(store_history=True)` distinguish preprocessing and execution errors. IPython catches cancellation and reports it through `ExecutionResult`. |
| Tool futures | `/Users/muspi-merol/Desktop/macaron/dsh-py-codeact/py/kernel.py:257-299` | A request owns one Future; pending unsent work is canceled, and late/duplicate results are ignored. Already-dispatched effects cannot be recalled. |
| Busy guard and tool rebinding | `/Users/muspi-merol/Desktop/macaron/dsh-py-codeact/py/kernel.py:1027-1092` | Reject an overlapping cell before changing the tool catalog. CPU-bound code blocks the in-band interrupt handler. |
| Host interrupt escalation | `/Users/muspi-merol/Desktop/macaron/dsh-py-codeact/lib/kernel.js:359-393` | Check abort again after startup, then in-band interrupt, then hard process kill after a grace period. |
| Codex per-cell task cleanup | `/Users/muspi-merol/Desktop/codex/scripts/py-codeact/host.py:68-100` | Every completed cell cancels detached tasks; cleanup exceeding two seconds exits the worker. This is incompatible with using one existing host cell per streamed statement. |
| Kernel subprocess lifecycle | `/Users/muspi-merol/Desktop/codex/scripts/py-codeact/host.py:172-220,226-280` | One subprocess per Codex session; finish closes tool dispatch before notifying observers. Termination kills the process group and loses variables. |
| Existing execute protocol | `/Users/muspi-merol/Desktop/codex/scripts/py-codeact/host.py:370-420` | `session/execute` receives full source and admits one active cell. Yield/wait observes execution; it is not source streaming. |
| Rust execution request | `/Users/muspi-merol/Desktop/codex/codex-rs/core/src/tools/code_mode/execute_handler.rs:38-99,128-163` | Full source enters `ExecuteRequest`; dispatch and cell tracing assume one complete logical tool call. |
| Custom tool text deltas | `/Users/muspi-merol/Desktop/codex/codex-rs/codex-api/src/sse/responses.rs:399-407` | Custom tool input deltas already become `ToolCallInputDelta`. |
| JSON arguments deltas | `/Users/muspi-merol/Desktop/codex/codex-rs/codex-api/src/sse/responses.rs:538-549` | Function-call arguments deltas are currently ignored. Raw custom Python input is the smaller initial integration. |
| Delta consumption | `/Users/muspi-merol/Desktop/codex/codex-rs/core/src/session/turn.rs:3179-3196,3411-3427` | Current deltas feed UI diff consumers, not an executor. |
| Dispatch authorization | `/Users/muspi-merol/Desktop/codex/codex-rs/core/src/session/turn.rs:3011-3023,3083-3094,3129` | Serving assessment and invocation dispatch occur at `OutputItemDone`. Production streaming integration must preserve the authorization path while introducing per-statement dispatch; the library prototype does not bypass it. |
| PoC environment restrictions | `/Users/muspi-merol/Desktop/codex/codex-rs/core/src/tools/code_mode/python.rs:79-106` | Current Python backend accepts only its local full-access environment. |
| Existing source-stream shape | `/Users/muspi-merol/Desktop/promplate/demo/src/utils/openai_api.py:23-30` | `AsyncIterable[str]` is already the text-delta interface used by the demo's SSE layer. |

These are reference ranges for navigation, not a claim that those integrations have been implemented.

## Parser alternatives

The initial implementation uses the running Python interpreter's grammar. A CST library can help preserve formatting or recover display structure, but cannot alone prove that later input will not extend a statement.

| Option | Relevant documented behavior | Role |
| --- | --- | --- |
| CPython `compile`/AST | AST parsing does not perform every scope check; top-level await has an explicit compiler flag. | Authoritative validation for the execution interpreter. |
| `tokenize` | On invalid Python its behavior is documented as undefined; synthetic EOF tokens also exist. | Candidate boundary discovery, checked by compilation and conformance tests. |
| `codeop` | Distinguishes complete/incomplete/error input and can retain future flags; completeness is not append stability. | Reference implementation for interactive compiler behavior. |
| Parso | Error recovery and round-trip trees; `diff_cache` mutates cached trees and is documented experimental. | Potential display/diagnostic backend. |
| Tree-sitter | True incremental parse via old tree/edit APIs, with error/missing nodes. | Potential performance backend after measurements; keep CPython validation. |
| LibCST | Full formatting-preserving CST; `PartialParserConfig` means partly specified configuration, not incomplete input support. | Refactoring tooling, not an execution boundary oracle. |
| Ruff/Rust | Error recovery is available; identifying incomplete input is a separately discussed feature. | Future accelerator if profiling justifies another grammar/binding dependency. |

Primary references:

- [Python AST/compiler flags](https://docs.python.org/3.14/library/ast.html#compiler-flags)
- [Python tokenize contract](https://docs.python.org/3.14/library/tokenize.html)
- [Python codeop](https://docs.python.org/3/library/codeop.html)
- [Parso parser and diff cache](https://parso.readthedocs.io/en/latest/docs/usage.html)
- [Tree-sitter Python parser](https://tree-sitter.github.io/py-tree-sitter/classes/tree_sitter.Parser.html)
- [LibCST parsing and PartialParserConfig](https://libcst.readthedocs.io/en/latest/parser.html)
- [Ruff parser design](https://astral.sh/blog/ruff-v0.4.0)
- [Ruff incomplete-input discussion](https://github.com/astral-sh/ruff/issues/17639)

## Prototype validation

`tests/test_execution.py` exercises early side effects, compound holdback, sequential await, background-task survival across flush, failure/cancellation terminality, rejected overlapping writes, future flags, docstrings, annotation scopes, source line numbers, transport errors, and early event-consumer close. `examples/streaming_tools.py` prints text arrival and execution timings with assertions: the first call runs before later source arrives, the entire loop waits for a successor, and the final call without a newline waits for EOF.

A real model evaluation still needs complete real online sessions and genuine tool results. These deterministic fixtures establish implementation behavior only.
