# Review decisions

Review is ongoing. The requested external route is a persistent `m clmimo` session. A separate `m clkm` attempt returned an authentication error and supplied no review evidence. External reviewers only inspect local source; they do not edit files or post to GitHub.

## Findings incorporated

| Finding | Decision |
| --- | --- |
| Accepted units disappeared when an earlier unit failed. | Retain a distinct unexecuted queue, failed unit, completed journal and pending suffix. |
| Convenience streaming bypassed ownership/state handling. | One stateful `Executor.stream` implementation; the convenience function returns it directly. |
| Code-object bits overlap obsolete future flags. | Derive requested flags from each unit's actual future imports, never later queued units. |
| Compiler error coordinates and serialized exceptions disagreed. | Preserve original source text, character columns and native error messages/arguments. |
| A cancelled body may return normally after producing effects. | Treat a new outstanding cancellation as terminal; document that a missing success event does not imply absent effects. |
| A source can fail while closing. | Surface close failure; retain a primary execution exception with a note for ordinary secondary errors. |
| Repeated scanning/copying penalized growing source. | Verified lexical checkpoints, deferred joining and quote retry gates, with independent adversarial tests. |
| Retry state stored an unnecessary coordinate. | Replace it with a boolean indicating whether the next probe should span the unfinished window. |
| Bounded reception could strand a task after independent child cancellation. | Keep execution in the owning task and explicitly forward independent producer cancellation. |
| Empty deltas accumulated history without consuming the character limit. | Discard empty transport deltas before retaining source history. |
| Descendants could hold output pipes open beyond the worker timeout. | Parent-owned output transports, bounded draining and explicit closure; process waiting is independent of pipe EOF on 3.11 too. |
| Partial resource acquisition and malformed worker summaries were unchecked. | Close already acquired resources, validate terminal fields and preserve the unexecuted journal across the process boundary. |

## Suggestions deliberately not applied

- **Shared mutable AST cache:** measured reparse cost did not justify ownership, mutation and retained-memory complexity. Public `CodeUnit` stays a small immutable source value.
- **Remove per-unit `sleep(0)`:** it delivers pending cancellation and permits other tasks to run between units; removing it changes behavior.
- **Add `executor=` to the convenience helper:** `Executor.stream` already provides persistent diagnostics without a duplicate entry point.
- **Extract tiny failure/line helpers or another draining async generator:** these would add indirection or another generator lifetime to small visible rules.
- **Reuse parser internals in the corpus oracle:** independent validation is intentional.
- **Claim linear rescanning for all valid source:** rejected after a valid escaped-quote counterexample; the limitation remains documented.
- **Treat source repair as execution permission:** preview remains separate and cannot relax the executor's original-source contract.

The first design review and multiple independent counterexample passes are complete. Actual `/simplify` dimensions have returned findings and clean results; the outstanding `/code-review`/remaining dimensions and a fresh post-change iteration must complete before review is considered finished.
