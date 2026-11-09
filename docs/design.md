# Design decisions

## Contract before implementation

The product has three layers: a syntactic commit frontier, an effectful executor, and a provisional display completer. Keeping them separate is essential: a parser accepting a prefix at EOF does not prove future append-only text cannot extend it, and a renderable repaired string must never authorize side effects.

Each `CodeUnit` returned by `Parser` is an exact source slice whose AST cannot be extended by a valid continuation. This is syntactic stability, not equivalence to compiling an entire future module or a promise of runtime isolation. See [the semantic contract](semantics.md) for counterexamples and explicit restrictions.

## Why Python and the standard library

The execution host's `tokenize` supplies lexical candidate boundaries; its compiler supplies the authoritative grammar and scope checks. An AST is sufficient because original source slices retain formatting and comments. A full CST is useful for editing, but it does not prove an append-only commit boundary. A Rust/TypeScript backend would add grammar/version synchronization and packaging costs before a measured need exists.

`tokenize` is not trusted as a recovery oracle: it is specified for valid Python and synthesizes EOF tokens. We ignore synthetic line endings, retain invalid/incomplete tails, and validate candidate source with CPython. Adversarial source prefixes and a Python version matrix are part of the implementation contract.

## Boundary rules

The [semantic contract](semantics.md#boundaries) defines when a complete top-level unit is stable. `stream_blocks` also permits stable headers and body statements to execute before their enclosing unit closes. It never authorizes repaired source; completion belongs to [display preview](preview.md).

A partial name such as `e` or exact `else` at temporary EOF is still ambiguous. A name that cannot grow into a continuation clause, such as `next`, already proves a successor. Known operator, number and string tokens establish the same boundary, even when the literal itself may still grow. Actual f/t-string start tokens and host-proven opaque string openers also establish successors before their contents finish. Unresolved lexical errors remain conservative. A successor keeps its entire logical-line prefix, including formfeeds and explicit joins, so original columns and physical lines remain intact. For a prefix spanning explicit joins, a native AST probe with a placeholder `pass` verifies that it is truly at module indentation; Python 3.11’s pure-Python tokenizer can disagree with the compiler here. The placeholder never enters a `CodeUnit` or execution.

## Incremental scanning

The scanner retains an uncommitted source window, lexical checkpoint, and the last candidate/reason checked. It resumes after actual logical newlines. Inside containers, a completed physical newline can also provide a checkpoint: exact opener kinds and the original logical line's indentation restore lexical context for the next probe. A synthetic opener prefix is used only by `tokenize`; it never enters a `CodeUnit` or compilation. Strings, mismatched delimiters, lexical errors and backslash joins cannot create those checkpoints. Compilation of the entire original unit validates the complete suite.

Physical-line windows avoid scanning the entire remaining buffer for every line in one batch. When a logical line spans physical lines, probe windows grow geometrically; the previous unfinished probe position persists across feed calls. A candidate's reason is part of its cache key: a compound held at newline must be reconsidered when a successor later proves that same offset stable.

Once a line's first token resolves the preceding boundary, a chunk without CR/LF cannot close that logical line. Such chunks accumulate without copying the growing source; scanning or reading `pending` joins them on demand. This removes repeated parsing of large one-line values/calls without maintaining a second Python lexer. A trailing CR always gets lookahead because it may be half of CRLF. Normalized scanning offsets map back to exact original characters.

For a proven unfinished string, a retry gate skips fragments containing no possible closing delimiter. The host tokenizer supplies f/t start tokens; a tokenizer-only quote suffix probe identifies opaque STRING tokens crossing actual EOF without reinterpreting any emitted token. Probe suffixes never enter compilation or execution. A two-character overlap and the preceding backslash-run parity reject escaped candidates while retaining overlapping closers. This necessary condition applies to ordinary/raw/bytes and f/t strings: a real outer closer cannot begin after an odd backslash run, and leaving a replacement field introduces a non-backslash `}`. It does not parse nested expressions or format specifications; a possible closer always triggers authoritative original-source tokenization. Frequent unescaped candidates in nested grammars can still cause repeated rescans. Growing multiline suites and repeatedly reading the full growing `pending` value can also require repeated copying, so this is not a blanket linear-time claim. Structural work-count tests prevent the avoidable cubic/batch-quadratic regressions; future changes should follow representative measurements, not a custom lexer by default.

## Execution lifecycle

A logical response owns one executor and namespace. Units run sequentially; source reception uses pull backpressure. The core creates no reception task. An optional host adapter can overlap reception and execution with explicit queue and task ownership; `examples/overlap.py` verifies that arrangement. No detached tasks are created or cancelled at unit boundaries. A host may own/cancel tasks at the end of the whole logical response.

Unhandled failures and cancellation are terminal. Events acknowledge completed top-level units; an open block can already have effects while its source remains pending. Failure ranges can overlap that pending source, so diagnostics are not resume offsets. Parser snapshots are immutable, and accepted code is never automatically replayed.

`stream` owns its acquired iterator and attempts to await its `aclose()` on every exit when provided. Its write guard stays active across yielded events. Each accepted chunk reaches a cooperative checkpoint even if it is empty or has no sealed unit; an immediately returning buffered source cannot starve cancellation by endlessly supplying unfinished code. Cancellation checks surround each current task's receive/execution/cleanup boundary; historical cancellation counts alone do not poison a fresh operation. Ordinary cleanup errors do not mask an existing execution error. New cancellation or process-level exceptions during cleanup remain observable failures. CPU-bound or cancellation-defeating code requires a process boundary.

## Compiler context

With `MODULE` enabled, future flags persist across units with stream-prologue checks. Only the initial string can become a module docstring. Module `global` declarations are rejected. On Python 3.14+, module annotated assignments require source-explicit future annotations so separate lazy annotation functions cannot overwrite one another. Function/class scopes remain intact. Policy traversal skips expressions and uses an explicit stack, avoiding artificial recursion limits on native-compilable expressions. Without `MODULE`, the entire source is compiled once at EOF with native module semantics.

## Validation and remaining scope

The [validation guide](validation.md) covers interpreter versions, generated grammar, exact AST-position conformance on real source, package installs, and host examples. Shared mutable AST caches would add ownership and mutation rules; optimize this path only when measurements justify that cost.

The core deliberately leaves source-size limits, hard execution deadlines and process isolation to its host. Large multiline inputs and frequent unescaped quote candidates remain performance limits, not reasons to weaken the commit boundary.
