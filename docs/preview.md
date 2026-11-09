# Display completion

`partial_python.complete()` is a separate, display-only API. It never executes source and never configures `Parser` or `Executor`. Execution uses only stable original source, including statements inside selected open blocks.

```python
from partial_python import Allow, complete

preview = complete('await foo(1)\nvalues = ["hel', Allow.STRING | Allow.BRACKET)
assert preview.prefix == 'await foo(1)\nvalues = ["hel'
assert preview.suffix == '"]'
assert preview.withheld == ""
assert preview.committed == len("await foo(1)\n")
assert preview.provisional
```

The immutable `Completion` preserves provenance:

- `prefix + withheld` is exactly the original input.
- `source` is `prefix + suffix`.
- `committed` is the number of original characters that passed the streaming parser's boundary rules.
- `provisional` says whether display includes characters beyond that boundary or synthetic text.
- `diagnostic` is the original `SyntaxError` or `UnicodeError` raised while parsing the input, or `None`. During a stream a syntax diagnostic can mean incomplete, not necessarily irreparable. A lone surrogate cannot be encoded as Python source; its original Unicode error is retained while the malformed tail stays in `withheld`.

A preview without a synthetic suffix can still be provisional. For example, `await foo(1)` is valid source but can still grow into `await foo(1) + other()`. It remains uncommitted until its real logical newline or declared EOF.

## Options

The default `Allow.NONE` returns only the committed prefix and retains the rest as `withheld`. Any nonzero policy enables provisional display and retains a valid pending tail under the parser's current grammar. Additional flags allow specific repairs:

| Flag | Display behavior |
| --- | --- |
| `TAIL` | Enable provisional display of already valid source, with no synthetic repairs. |
| `STRING` | Close ordinary, raw, bytes and triple-quoted string literals. |
| `BRACKET` | Close data-container and grouping delimiters. |
| `SUITE` | Append an indented `pass` for an empty final suite. |
| `ALL` | Enable all four display permissions. |

These flags authorize different repairs. An open string inside a list requires both `STRING` and `BRACKET`. Complete source remains visible under either flag: a preview must not disappear when its real closing quote or bracket arrives. `TAIL` is useful when no repairs are wanted; combining it with another flag adds no behavior.

```python
assert complete("value = 'hello", Allow.STRING).source == "value = 'hello'"
assert complete("value = 'hello'", Allow.STRING).source == "value = 'hello'"
```

Both previews remain provisional until a real streaming boundary arrives. A synthetic `pass` is likewise replaced by the author's real suite without treating that display update as an execution commitment.

Preview follows `Parser`'s ordered AST grammar, with top-level await enabled. Each committed unit was checked under the future flags active at that point; only the pending tail and proposed suffix are checked under the flags established by those units. A later future import never reinterprets earlier accepted source. The combined display is therefore not promised to parse as one module in a single compiler call; Python 3.11's `barry_as_FLUFL` handling is one such difference. Preview adds no executor prologue or scope checks.

Syntax diagnostics retain their original snapshot line numbers. Encoding diagnostics retain the full original input and character offsets. These checks do not imply successful runtime execution, a stable final value, sandboxing or reversible effects.

## Conservative boundaries

The completer only appends suffixes to the entire input, or returns the parser-committed exact prefix. It does not rewrite interior tokens or search for a shorter slice that happens to parse. An invalid interior stays in `withheld` with a diagnostic, including everything after it; later valid-looking code is never spliced around it.

No operands, dictionary values, identifiers, call arguments or exception handlers are invented. An incomplete call stays withheld even when closing its parenthesis would parse: `send(1` might still acquire more arguments. Fully written calls may be displayed under any nonzero policy, but are still not execution commitments.

F-strings and template strings have nested grammars and are never repaired. A complete source containing them can still be displayed under any nonzero policy. A previously committed f-string or template string does not block an independent pending tail. If an f/t string remains inside the uncommitted unit, even when that literal itself is complete, the suffix scanner still conservatively declines repairs. Incomplete escape sequences and odd trailing backslashes remain withheld. A complete compound body may be displayed under a nonzero policy; its execution boundary is unchanged.

`SUITE` is deliberately a preview permission. `for item in items:` can display with a synthetic `pass`, but the real loop remains entirely pending in the execution parser. No API automatically converts a `Completion` object into an execution unit.

`complete()` is a stateless snapshot operation. For streaming execution use `Executor.feed()` or `Executor.stream()`, and inspect completed events through `Execution.unit`. Calling the snapshot completer on every growing prefix necessarily rereads prior source; benchmark that separately from the incremental runtime. Display completions do not belong in `executor.queued` or its completed-event journal.

## References and validation

See [the partial family](prior-art.md#the-partial-family) for pinned references and the distinction between provisional display and committed effects.

Tests cover every character prefix of representative Unicode, CRLF, nested-suite, decorator, async and f-string sources under every flag combination, plus explicit repair/hold cases.
