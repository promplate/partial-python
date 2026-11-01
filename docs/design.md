# Design decisions

Status: working prototype under adversarial review. The API is unpublished and may change.

## Contract before implementation

The product has three layers: a syntactic commit frontier, an effectful executor, and a provisional display completer. Keeping them separate is essential: a parser accepting a prefix at EOF does not prove future append-only text cannot extend it, and a renderable repaired string must never authorize side effects.

An emitted `CodeUnit` is an exact original-source slice whose AST cannot be extended by a valid continuation. This is syntactic stability, not equivalence to compiling an entire future module or a promise of runtime isolation. See [the semantic contract](semantics.md) for counterexamples and explicit restrictions.

## Why Python and the standard library

The execution host's `tokenize` supplies lexical candidate boundaries; its compiler supplies the authoritative grammar and scope checks. An AST is sufficient because original source slices retain formatting and comments. A full CST is useful for editing, but it does not prove an append-only commit boundary. A Rust/TypeScript backend would add grammar/version synchronization and packaging costs before a measured need exists.

`tokenize` is not trusted as a recovery oracle: it is specified for valid Python and synthesizes EOF tokens. We ignore synthetic line endings, retain invalid/incomplete tails, and validate candidate source with CPython. Adversarial source prefixes and a Python version matrix are part of the implementation contract.

## Public layers

- `Parser.feed` / `finish` produce original `CodeUnit` slices; `pending` retains the uncommitted suffix.
- `Executor.feed` / `finish` are explicit writes for protocols with separate completion statuses.
- `Executor.stream` consumes an async iterator with per-unit backpressure and persistent diagnostics. `incremental_exec` constructs an executor and returns that iterator directly.
- `complete` returns `Completion(prefix, suffix, withheld, committed, diagnostic)`. The default is a stable prefix; nonzero `Allow` flags enable provisional display. Only the display completer adds characters.

Names describe the actual unit of work: one `CodeUnit` can contain a semicolon chain or a whole compound statement. `Execution.unit` identifies that slice; events denote successful completion, not atomic effects.

## Boundary rules

Simple statements need a real logical NEWLINE or declared EOF. Entire compound statements wait for a distinct top-level successor or EOF, including one-line suites. Optional continuation clauses remain attached. Blank lines and comments never close a suite. Decorators travel with their definition.

A partial name such as `e` or exact `else` at temporary EOF is still ambiguous. A name that cannot grow into a continuation clause, such as `next`, already proves a successor. Known operator, number and string tokens establish the same boundary, even when the literal itself may still grow. Actual f/t-string start tokens and host-proven opaque string openers also establish successors before their contents finish. Unresolved lexical errors remain conservative. A successor keeps its entire logical-line prefix, including formfeeds and explicit joins, so original columns and physical lines remain intact. For a prefix spanning explicit joins, a native AST probe with a placeholder `pass` verifies that it is truly at module indentation; Python 3.11’s pure-Python tokenizer can disagree with the compiler here. The placeholder never enters a `CodeUnit` or execution.

## Incremental scanning

The scanner retains an uncommitted source window, lexical checkpoint, and the last candidate/reason checked. It resumes after actual logical newlines. Inside containers, a completed physical newline can also provide a checkpoint: exact opener kinds and the original logical line's indentation restore lexical context for the next probe. A synthetic opener prefix is used only by `tokenize`; it never enters a `CodeUnit` or compilation. Strings, mismatched delimiters, lexical errors and backslash joins cannot create those checkpoints. Compilation of the entire original unit validates the complete suite.

Physical-line windows avoid scanning the entire remaining buffer for every line in one batch. When a logical line spans physical lines, probe windows grow geometrically; the previous unfinished probe position persists across feed calls. A candidate's reason is part of its cache key: a compound held at newline must be reconsidered when a successor later proves that same offset stable.

Once a line's first token resolves the preceding boundary, a chunk without CR/LF cannot close that logical line. Such chunks accumulate without copying the growing source; scanning or reading `pending` joins them on demand. This removes repeated parsing of large one-line values/calls without maintaining a second Python lexer. A trailing CR always gets lookahead because it may be half of CRLF. Normalized scanning offsets map back to exact original characters.

For a proven unfinished string, a retry gate skips fragments containing no possible closing delimiter. The host tokenizer supplies f/t start tokens; a tokenizer-only quote suffix probe identifies opaque STRING tokens crossing actual EOF without reinterpreting any emitted token. Probe suffixes never enter compilation or execution. A two-character overlap and the preceding backslash-run parity reject escaped candidates while retaining overlapping closers. This necessary condition applies to ordinary/raw/bytes and f/t strings: a real outer closer cannot begin after an odd backslash run, and leaving a replacement field introduces a non-backslash `}`. It does not parse nested expressions or format specifications; a possible closer always triggers authoritative original-source tokenization. Frequent unescaped candidates in nested grammars can still cause repeated rescans. Growing multiline suites and repeatedly reading the full growing `pending` value can also require repeated copying, so this is not a blanket linear-time claim. Structural work-count tests prevent the avoidable cubic/batch-quadratic regressions; future changes should follow representative measurements, not a custom lexer by default.

## Execution lifecycle

A logical response owns one executor and namespace. Units run sequentially; source reception uses pull backpressure. The core creates no reception task. An optional host adapter can overlap reception and execution with explicit queue and task ownership; `examples/overlap.py` verifies that arrangement. No detached tasks are created or cancelled at unit boundaries. A host may own/cancel tasks at the end of the whole logical response.

Failure and cancellation are terminal. The journal separates completed units, the failed attempted unit, sealed units never started, and the unsealed suffix. Parser diagnostics are immutable snapshots so a caller cannot accidentally consume source around the execution journal. Accepted code is never automatically replayed.

`stream` owns its acquired iterator and attempts to await its `aclose()` on every exit when provided. Its write guard stays active across yielded events. Each accepted chunk reaches a cooperative checkpoint even if it is empty or has no sealed unit; an immediately returning buffered source cannot starve cancellation by endlessly supplying unfinished code. Cancellation checks surround each current task's receive/execution/cleanup boundary; historical cancellation counts alone do not poison a fresh operation. Ordinary cleanup errors do not mask an existing execution error. New cancellation or process-level exceptions during cleanup remain observable failures. CPU-bound or cancellation-defeating code requires a process boundary.

## Compiler context

Future flags persist across units with stream-prologue checks. Only the initial string can become a module docstring. Module `global` declarations are rejected. On Python 3.14+, module annotated assignments require source-explicit future annotations so separate lazy annotation functions cannot overwrite one another. Function/class scopes remain intact. Policy traversal skips expressions and uses an explicit stack, avoiding artificial recursion limits on native-compilable expressions.

## Validation still in progress

- Repeated external `/simplify` and `/code-review` rounds; one configured route returned an authentication error and does not count as completed review.
- Full version matrix after each material scanner/executor change, package artifact smoke tests, and broad real-source partition conformance.
- Resource bounds and remaining large multiline input behavior.
- Real model evaluation or CodeAct product integration would require complete real session context. Current fixtures, TCP protocol checks, and stdlib corpora are engineering verification only.
