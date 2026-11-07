# CodeAct integration

The examples and tests here are deterministic protocol fixtures, not real model evaluations or evidence of improved online behavior.

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

Execution uses sequential compilation units sharing globals. See the [semantic contract](semantics.md#execution-is-a-sequence-of-compilation-units) for future-import, docstring, global and annotation policies, and observable differences from whole-module compilation.

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
