# Related implementations

Streaming execution already has direct precedents. This prototype focuses on an append-only Python boundary contract, original-source provenance, top-level await, and explicit terminal state. The comparisons below describe inspected versions; they are not claims about every version or benchmark results for this project. External implementations were inspected without installing or executing them, and no implementation code was copied.

## The partial family

The family suggests keeping provisional display separate from committed effects:

| Reference | Useful idea and boundary |
| --- | --- |
| [`partial-json-parser` API](https://github.com/promplate/partial-json-parser/blob/e0aa5e2ec52ed81c2c5d7692d5fe81d01c4b0bc6/src/partial_json_parser/core/api.py#L11-L26) | Separates value parsing from string completion. [`Allow`](https://github.com/promplate/partial-json-parser/blob/e0aa5e2ec52ed81c2c5d7692d5fe81d01c4b0bc6/src/partial_json_parser/core/options.py#L4-L21) controls incomplete values; [`fix`](https://github.com/promplate/partial-json-parser/blob/e0aa5e2ec52ed81c2c5d7692d5fe81d01c4b0bc6/src/partial_json_parser/core/complete.py#L12-L28) separates retained text and synthetic suffix. This motivates `complete()` and its display-only permissions. |
| [`partial-tsx`](https://github.com/MindLab-Research/macaron-genui-demo/blob/e960b17a460e7ddf7f03045c2a67aabbf5f79f67/lib/partial-tsx/src/partial.ts#L1454-L1480) | Tracks original-source provenance; [continuation handling](https://github.com/MindLab-Research/macaron-genui-demo/blob/e960b17a460e7ddf7f03045c2a67aabbf5f79f67/lib/partial-tsx/src/partial.ts#L1900-L1938) retains pending control structures. Display exceptions for JSX or function shells do not authorize Python effects. |
| [`partial-react`](https://github.com/MindLab-Research/macaron-genui-demo/blob/e960b17a460e7ddf7f03045c2a67aabbf5f79f67/lib/partial-react/src/runtime.ts#L309-L413) | Separates scheduling, compilation and committed rendering. A renderer can coalesce updates; an executor must preserve accepted source order and must not replay effects. |
| [`partial-vue`](https://github.com/MindLab-Research/macaron-genui-demo/blob/e960b17a460e7ddf7f03045c2a67aabbf5f79f67/lib/partial-vue/src/partial.ts#L137-L172) | Structural directives wait for a real closing quote: a temporarily valid expression can still change. Python statements likewise need actual boundary evidence. |
| [`partial-svelte`](https://github.com/MindLab-Research/macaron-genui-demo/blob/e960b17a460e7ddf7f03045c2a67aabbf5f79f67/lib/partial-svelte/README.md#L34-L39) | Documents limitations of state preservation through internal compiler-generated APIs. The Python parser delegates final syntax acceptance to the host compiler. |
| [`partial-solidjs`](https://github.com/MindLab-Research/macaron-genui-demo/blob/e960b17a460e7ddf7f03045c2a67aabbf5f79f67/lib/partial-solidjs/README.md#L26-L44) | Preserves visible output and reactive state across updates. Such restoration cannot undo a Python operation's external effects. |

The inspected JSON revision is `e0aa5e2ec52ed81c2c5d7692d5fe81d01c4b0bc6`, with an [MIT license](https://github.com/promplate/partial-json-parser/blob/e0aa5e2ec52ed81c2c5d7692d5fe81d01c4b0bc6/LICENSE). Renderer references are pinned to GenUI revision `e960b17a460e7ddf7f03045c2a67aabbf5f79f67`; this audit does not establish a standalone license for those in-repository packages. [Preview design](preview.md) describes the resulting API in more detail.

## EAGER / StreamExec

[Executing as You Generate](https://arxiv.org/html/2604.00491v2), arXiv `2604.00491v2` (22 June 2026), describes AST boundary detection with lookahead, persistent execution, dynamic batching, gating and optional early error interruption. This is directly related prior art. Batching is useful when interpreter invocation has meaningful overhead; its benefits need measurement for a particular host.

The inspected [public artifact](https://doi.org/10.6084/m9.figshare.31869469.v1) is an earlier version, published 27 March 2026: [`EAGER-main.zip`](https://ndownloader.figshare.com/files/63180436), MD5 `f441473c12c18fd106eb80926059ec75`. It has no Git commit metadata. The archive's `LICENSE:1–21` is MIT, copyright 2026 EAGER Authors; the [deposit metadata](https://api.figshare.com/v2/articles/31869469) separately labels the deposit CC BY 4.0. Paper and artifact dates differ, so implementation differences below should not be read as an audit of the later paper's implementation.

Useful source locations within that archive:

| Location | Inspected behavior |
| --- | --- |
| `eager/parser.py:117–172,289–344,397–433` | Uses `code.compile_command` and `ast.parse` on pending physical lines. Compounds wait for a later parsed statement or finalization. It reparses the growing pending source, and receives lookahead through complete LF-terminated lines. |
| `eager/parser.py:446–473`; `eager/consumer.py:303–324` | Invalid/incomplete EOF becomes a chunk carrying `parse_error`; the consumer records and excludes such chunks from executable batches. |
| `eager/consumer.py:62–133,479–516` | Coalesces available chunks, with optional size/delay limits and halt/continue error policies. |
| `eager/executors.py:17–46,50–85,152–207` | Shared namespaces in local threads or a persistent subprocess. The subprocess has its own timeout/kill mechanism. Plain compilation does not enable top-level await or retain future flags across subprocess requests. |
| `eager/pipeline.py:41,94–125` | Disables rewriting by default, gathers producer/consumer, then runs a separate full-program baseline: this entry point is an experiment harness. |
| `eager/parser.py:26,48–50`; `eager/rewriters.py:101–117,139–173` | Direct parser construction instead enables an if-chain rewriter by default. It injects bookkeeping globals and fixed-indentation source. |

Our contract deliberately avoids that rewriting, retains native exceptions, and makes EOF, abort and failure explicit. The examined artifact's runtime-error halt marks later chunks skipped while generation continues; its timeout path cancels both tasks (`clean_benchmark/runtime.py:310–351`). The later paper describes additional gating and interruption behavior that should not be inferred from these older paths.

Static inspection also highlights why source reconstruction alone cannot prove general module equivalence: compiler flags, module docstrings and compilation context survive differently across separate `exec` calls. See [our semantic limits](semantics.md). The archive contains benchmark runners and an overhead experiment (`scripts/test_exec_overhead.py`); no dedicated parser unit/property suite was found. None of its reported benchmark results are results for partial-python.

## Playbooks

[Playbooks](https://github.com/playbooks-ai/playbooks/tree/1f6ee0cc760f245aa3af262b0fd0e6d0d70060f8) contains a direct streaming Python executor integrated with agent call frames. Inspected revision: `1f6ee0cc760f245aa3af262b0fd0e6d0d70060f8`; [MIT license](https://github.com/playbooks-ai/playbooks/blob/1f6ee0cc760f245aa3af262b0fd0e6d0d70060f8/LICENSE), copyright 2024 Playbooks AI.

- [`CodeBuffer`](https://github.com/playbooks-ai/playbooks/blob/1f6ee0cc760f245aa3af262b0fd0e6d0d70060f8/src/playbooks/execution/incremental_code_buffer.py#L38-L115) finds a parsable prefix using physical newlines and indentation. Its markdown stripping and `$variable` preprocessing serve that host's language, rather than exact Python-source preservation.
- [`add_chunk`](https://github.com/playbooks-ai/playbooks/blob/1f6ee0cc760f245aa3af262b0fd0e6d0d70060f8/src/playbooks/execution/streaming_python_executor.py#L106-L192) executes parsed statements and records errors. [`Await handling`](https://github.com/playbooks-ai/playbooks/blob/1f6ee0cc760f245aa3af262b0fd0e6d0d70060f8/src/playbooks/execution/streaming_python_executor.py#L321-L383) wraps a statement in an async function, then copies selected locals into the agent frame. Partial-python instead uses native top-level-await compilation and one namespace.
- [`Tests`](https://github.com/playbooks-ai/playbooks/blob/1f6ee0cc760f245aa3af262b0fd0e6d0d70060f8/tests/unit/execution/test_streaming_variable_capture.py#L110-L177) exercise variable persistence and mixed sync/async work. Host-specific [safety-filter tests](https://github.com/playbooks-ai/playbooks/blob/1f6ee0cc760f245aa3af262b0fd0e6d0d70060f8/tests/unit/execution/test_streaming_executor_safety_filters.py#L68-L115) intentionally permit skipping some source statements, unlike this project's execution contract.

Static counterexamples help define our regression requirements: `else:` must not close and emit the preceding `if`; a final suite needs explicit EOF handling; `x = x + await foo()` must keep module scope; and successful statements before a later failure need individual provenance. At the inspected revision, `CodeBuffer:95–102` can select the prefix before `else`, [`finalize`](https://github.com/playbooks-ai/playbooks/blob/1f6ee0cc760f245aa3af262b0fd0e6d0d70060f8/src/playbooks/execution/streaming_python_executor.py#L388-L419) only retries the normal buffer path, and the prefix journal updates after the entire parsed statement loop succeeds. These are source-derived observations, not execution results from that project.

## Partial expression parsing

vLLM's [`make_valid_python`](https://github.com/vllm-project/vllm/blob/b8c04dd3b455affd8d91f0ab436b815491c9af38/vllm/tool_parsers/utils.py#L982-L1077) completes streaming tool-call expressions and returns completed text plus an added suffix, or `None`. The inspected revision is `b8c04dd3b455affd8d91f0ab436b815491c9af38`; the file [declares Apache-2.0](https://github.com/vllm-project/vllm/blob/b8c04dd3b455affd8d91f0ab436b815491c9af38/vllm/tool_parsers/utils.py#L1-L2). It can strip trailing whitespace/comma and synthesize delimiters; it also rejects synthesized sets that could really be incomplete dictionaries. This is useful preview prior art, with a different contract from executing committed original Python.

Python's [`codeop`](https://docs.python.org/3/library/codeop.html) supplies complete/incomplete/error classification and a stateful compiler that remembers future directives. That is useful machinery, but temporary syntactic completeness does not establish append stability. [`Parso`](https://parso.readthedocs.io/en/latest/docs/usage.html) supplies error recovery and optional incremental reparsing; recovering a syntax tree likewise does not authorize irreversible execution.

## Package name

On 2 November 2026, the [PyPI JSON endpoint](https://pypi.org/pypi/partial-python/json) and [simple index endpoint](https://pypi.org/simple/partial-python/) returned not found. No published package was found at that normalized name during this check. This does not reserve the name or guarantee that PyPI will accept registration. The prototype retains the requested distribution name `partial-python` and import name `partial_python`.
