# Validation

Run the checks from a checkout:

```sh
uv run pytest -q
uv run basedpyright
uv run ruff check .
uv run ruff format --check .
uv run --isolated --python 3.11 pytest -q
uv run --isolated --python 3.15 pytest -q
PARTIAL_PYTHON_GRAMMAR_EXAMPLES=10000 uv run pytest -q tests/test_generated_grammar.py --hypothesis-seed=827176 --hypothesis-show-statistics
uv run tools/check_corpus.py --limit 0 --chunk-max 512 --char-limit 2048
uv build
```

Test each supported interpreter when changing grammar-sensitive behavior; newer syntax is explicitly skipped on older hosts. Generated programs must compile successfully before partition testing. An invalid generated program is a fixture failure, never silently discarded.

## Parser conformance

Tests exercise character delivery, every two-chunk split, seeded random partitions, and bulk input. They compare original source slices, character offsets, physical line positions, pending tails, final grouping, and each emitted AST prefix against the whole-source AST.

`tools/check_corpus.py` applies the same invariants to real source, using the host standard library by default or `--root PATH` for another tree. It compiles to ASTs only; corpus code is never imported or executed. The oracle calls native compilation independently of private parser helpers and compares AST positions as well as shape. Invalid encodings and whole-source syntax failures are reported separately from parser failures. Output records interpreter/root configuration, partition seeds, and failing source hashes for reproduction.

Corpus decoding uses `tokenize.open`, which normalizes newlines. Separate fixtures preserve original CRLF and CR across transport partitions. The corpus oracle and retry-disabled differential tests remain independent of the shared test assertions.

## Execution and host integration

Execution tests cover ordering, terminal failure/cancellation, compiler context, source diagnostics, and iterator/task ownership. Block tests compare native execution across source partitions and cover default `ALL`, flag exclusions, early effects without replay, exception suppression, and cleanup. Gated sources and real `asyncio.timeout()` verify effects and context lifetimes before EOF. The [host examples](api.md#host-examples) have tests for real loopback sockets, bounded reception, task cleanup, and persistent traceback source. These controlled fixtures verify behavior; they are not model evaluations or evidence of improved CodeAct performance.

## Performance and packaging

Work-count regressions cover deferred buffer joining, geometric probe windows, delimiter checkpoints, and escape-aware string retry gates. No general linear-time guarantee is made: growing multiline suites, frequent unescaped candidate closers, malformed input, or repeatedly reading the entire growing `pending` value can still incur repeated work. Benchmark representative inputs before changing those optimizations; do not weaken commit boundaries to meet a timing target.

Before release, build and install both wheel and sdist in isolated environments outside the checkout. Verify installed source against the checkout, `py.typed`, zero runtime dependencies, preview behavior, early awaits, selected block streaming, compound holdback, and failure cleanup on the oldest and newest supported interpreters.
