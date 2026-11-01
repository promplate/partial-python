# Review decisions

The requested external route uses two persistent `m clmimo` sessions: one for executor/preview/host behavior, one for parser boundaries. External reviewers inspect local source without editing or publishing. Findings below combine those reviews with independent implementation and counterexample checks. Every accepted behavior change is validated locally before commitment.

A separate `m clkm` attempt failed authentication and supplies no review evidence. Two initial review attempts exhausted their output allowance and supply no completed-review evidence. The parser attempt failed to resolve its intended range in this repository without a remote; that same review later resumed with explicit commits and completed. A separately launched, overlapping explicit-range retry repeatedly hit its output limit and was stopped; it is not counted as completed. A manual pass labeled “simplify” was explicitly excluded; subsequent rounds invoked the actual `/simplify` and `/code-review` tools.

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
| Cleanup diagnostics themselves could fail. | Attach ordinary secondary errors best-effort without replacing the primary; retain native exception chains when a new cancellation or process-level exception wins. |
| `GeneratorExit` from executed code was confused with event-consumer closure. | Track suspension at the event yield; internal failures remain recorded and terminal. |
| Empty or unsealed buffered chunks could starve cancellation. | Add a cooperative checkpoint when an accepted stream chunk produces no executable unit. |
| Non-simple module annotations can alter existing deferred metadata on 3.14+. | Keep the guard for every module-scope annotated assignment, not only simple targets. |
| Preview reparsed historical source under incompatible future flags. | Validate only the pending tail under current flags; keep original diagnostic coordinates and never reinterpret committed history. |
| A committed f/t string blocked an unrelated display repair. | Scan suffix repairs only inside the pending unit; nested f/t grammar remains conservative. |
| Formfeeds and standalone joins were assigned to a predecessor unit. | Keep the entire successor logical-line prefix and compare exact AST positions in conformance oracles. |
| Python 3.11 tokenization can disagree with native indentation after a join. | Use a native, non-executed prefix probe; lexical whitespace error tokens cannot prove a successor. |
| Socket close failures replaced tool errors or stranded server completion. | Preserve primary errors, propagate interrupted cleanup, and include callback close failures in the owning completion future. |
| Ambient exception state misclassified a successful socket scope. | Capture failures inside a writer-owning context manager; an unrelated caller `except` cannot hide a close failure. Reuse the same policy for worker terminal-frame writes. |
| Host ownership instructions were ambiguous. | Distinguish idle abort from cancellation of active operations; show explicit source-iterator closing and source-file lifetime/encoding constraints. |

## Findings and suggestions not applied

- **Shared mutable AST cache:** measured reparse cost did not justify ownership, mutation and retained-memory complexity. Public `CodeUnit` stays a small immutable source value.
- **Remove per-unit `sleep(0)`:** it delivers pending cancellation and permits other tasks to run between units; removing it changes behavior.
- **Add `executor=` to the convenience helper:** `Executor.stream` already provides persistent diagnostics without a duplicate entry point.
- **Extract tiny failure/line helpers or another draining async generator:** these would add indirection or another generator lifetime to small visible rules.
- **Reuse parser internals in the corpus oracle:** independent validation is intentional.
- **Claim linear rescanning for all valid source:** rejected after a valid escaped-quote counterexample; the limitation remains documented.
- **Treat source repair as execution permission:** preview remains separate and cannot relax the executor's original-source contract.
- **First-row indentation failure:** the proposed Python 3.11 counterexample was unverified and did not reproduce. Native tokenization emits `INDENT` for the ordinary indented first line, keeping the function intact. The leading-whitespace error-token case involves explicit joins, where the later physical row receives the native prefix probe.
- **Ignore every lexical error token:** on Python 3.11, a partial ordinary quote can already prove a successor even when its body resembles `else`; blanket filtering delays valid early execution. Ignore only lexical whitespace, and retain native prefix validation.

## Actual external iterations

Completed rows refer to actual tool-invoked `/code-review` and four-dimension `/simplify` rounds. Delivery receipts, pending agents and manual summaries do not count as completion. Reviewers with an unavailable Python transport reported static-only scope; the local validation record supplies the independent runtime evidence.

| Scope | Result and disposition |
| --- | --- |
| Initial parser/executor design and implementation | Completed; lifecycle, compiler-context and performance findings drove the focused fixes above. |
| Executor cleanup/cancellation (`46915a1`, `7a96bc9`) | Completed. The alleged loss of a primary error was retracted after checking Python's native exception context chain. |
| Parser boundaries (`17e656f..446acab`) | Completed. Adopted shared escape-aware string retry logic after native-lexer inspection and differential validation. |
| Internal failure versus consumer closure (`eec76ad`) | Completed. The stale-error finding was excluded by the terminal-state entry guard; added its invariant as a comment. Kept native `athrow` behavior. |
| Shared quote retry gate (`446acab..d9ab7c0`) | Completed. No behavior defect; corrected stale terminology in a comment. |
| Ordered preview grammar (`ac4e4d5`, `5ccee45`) | Completed. Late-future validation was a pre-existing AST-only limitation enforced by Executor; rejected merging helpers whose coordinate and hot-path contracts differ. |
| Logical-line prefixes and native indentation (`033929f`, `02fe02e`) | Four simplify dimensions completed: removed unused coordinates, added short-quote regressions, and rejected blanket lexical-error filtering after differential checks. The original code-review resumed with the explicit `d9ab7c0..02fe02e` range and completed. Its sole, explicitly unverified first-row indentation finding was disproved on native Python 3.11. The overlapping `d9ab7c0..c73f959` retry was stopped after repeated output limits and supplies no additional verdict. |
| Host socket cleanup (`ed48fd6`) | Completed. Independent reproduction promoted a supposedly latent ambient-exception concern into a real recipe fix; also fixed the worker terminal-write counterpart. Kept guards around secondary diagnostics and completion futures. |
| Scope-owned socket cleanup (`98a0daa`, `e26759d`) | Completed. No correctness findings or useful structural simplification remained. Clarified the shutdown exception policy and why the caught exception needs a separate binding. |

No confirmed finding remains open. Completion here records the returned review and independent validation; it does not turn failed or stopped duplicate attempts into clean reviews.
