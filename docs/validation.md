# Validation record

These checks verify parser, execution and host-protocol behavior. Constructed programs are engineering fixtures, not model evaluations. Real-source corpus files are read and compiled to ASTs, never imported or executed.

## Reproduce the checks

```sh
uv run pytest -q
uv run mypy
uv run ruff check .
uv run ruff format --check .
uv run --isolated --python 3.11 pytest -q
uv run --isolated --python 3.15 pytest -q
PARTIAL_PYTHON_GRAMMAR_EXAMPLES=10000 uv run pytest -q tests/test_generated_grammar.py --hypothesis-seed=827173 --hypothesis-show-statistics
uv run python tools/check_corpus.py --limit 0 --chunk-max 512 --char-limit 2048
uv build
```

The full 831-case suite passed with 781 passed/50 skipped on 3.11, 794/37 on 3.12 and 3.13, 817/14 on 3.14, and all 831 passed on 3.15; syntax introduced later than the host version is skipped explicitly. The generated-grammar stress run was requested with 10,000 examples on each of 3.11, 3.14 and 3.15, seed 827173, with no failures. It combines nested control flow, optional clauses, strings, decorators, joining, indentation, line endings and transport partitions. Every generated program must compile successfully before testing; invalid generated input is a test failure, never silently discarded.

## Real-source conformance

Local runs on 2 November 2026 used bulk delivery, seeded random partitions and character delivery for files up to the configured threshold. Every intermediate emitted AST must be a prefix of the whole-source AST; original slices, source cursors, retained tails and final unit grouping are also checked.

| Corpus | Valid files passed | Source characters | Partitions |
| --- | ---: | ---: | ---: |
| Python 3.11.18 standard library | 732 | 12,190,338 | 1,641 |
| Python 3.14.10 standard library | 653 | 11,799,837 | 1,453 |
| Python 3.15.2 standard library, including its installed tests | 2,055 | 39,867,355 | 4,754 |
| Local AnyIO sources | 43 | 498,527 | 99 |
| Local HTTPX sources | 23 | 284,351 | 52 |
| Local gh-mcp sources | 4 | 17,704 | 10 |
| Local HMR sources | 21 | 83,086 | 57 |

There were no parser errors or invariant mismatches among valid files. The 3.15 tree additionally contained three deliberately invalid encoding fixtures and two whole-source syntax-error fixtures; they were reported separately, not counted as parser successes. Versioned standard-library rows overlap in module identity; they are not distinct projects. Third-party/local runs used maximum random chunks of 37 characters and a 4,096-character threshold for character delivery. Installed source versions can change, so the corpus runner prints interpreter/root configuration and hashes any failing source for reproduction.

The oracle deliberately uses public CPython compilation independently of the library's private parser helpers. Sharing those helpers would hide bugs in the implementation being checked.

## Performance evidence and limits

Measurements are local development observations, not cross-machine promises or model latency results:

- A 1 MiB unfinished single-line literal, appended in 64-character chunks and read once at the end, fell from roughly 125 ms to 1.4 ms after deferred buffer joining.
- Roughly 1 MiB of ordinary multiline literal text, delivered in 256-character chunks and finalized, parsed in about 23 ms after quote retry gating.
- The 601,384-character standard-library `pydoc_data/topics.py` corpus check fell from about 35.5 seconds before container checkpoints to about 0.3 seconds with container checkpoints and string retry gating. The entire 3.14 corpus fell from about 48 seconds to 9–10 seconds.

Work-count regression tests accompany timing observations. No general linear-time guarantee is made. A valid long literal containing repeated escaped candidate closing delimiters can still force repeated full rescans. Reading the entire growing `pending` value after every tiny chunk also requires repeated materialization. Hosts should bound source size and execution lifetime.

## End-to-end scope

- `streaming_tools.py`: early call, complete compound holdback, sequential awaits and explicit EOF.
- `transport.py`: real loopback TCP, fragmented UTF-8/JSON records, exact final-source consistency, truncation and disconnect behavior.
- `overlap.py`: handshake-proven reception during an active await, bounded queue backpressure, cancellation propagation and source cleanup.
- `worker.py`: actual child processes, independent source/RPC channels, persistent per-stream namespace, bounded output capture, hostile CPU fixtures, repeated cancellation, acquisition failures and exact process/group cleanup. Escaped descendants are cleaned separately by the test fixture; the example itself contains only its owned group.

These fixtures use real parser/executor/protocol code and controlled local tool functions. They do not measure an LLM or claim production CodeAct integration. Packaging smoke checks have also imported the built wheel with isolated Python 3.11 and 3.15 environments; repeat them after release-affecting edits.
