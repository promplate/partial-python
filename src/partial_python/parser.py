"""Conservative boundaries for append-only Python source."""

import __future__

import ast
import io
import re
import tokenize
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Statement:
    """An exact source slice; offsets count Unicode characters, lines are one-based."""

    source: str
    start: int
    end: int
    start_line: int
    end_line: int


_COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.TryStar, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Match)
_CLAUSES = frozenset({"elif", "else", "except", "finally"})
_LINE_END = re.compile(r"[\r\n]")
_TRIVIA = frozenset({tokenize.COMMENT, tokenize.NL, tokenize.ENCODING})


def _tree(source: str, filename: str, flags: int = 0, start_line: int = 1) -> ast.Module:
    try:
        return compile(source, filename, "exec", flags=flags | ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT, dont_inherit=True)
    except SyntaxError as error:
        if error.lineno is not None:
            error.lineno += start_line - 1
        if error.end_lineno is not None:
            error.end_lineno += start_line - 1
        raise


def _line_end(source: str, start: int) -> int:
    match = _LINE_END.search(source, start)
    if match is None:
        return len(source)
    # One lookahead character resolves a bare CR versus the first half of CRLF.
    return min(match.end() + (match[0] == "\r"), len(source))


def _normalize(source: str):
    normalized = []
    offsets = [0]
    index = 0
    while index < len(source):
        character = source[index]
        if character == "\r":
            if index + 1 == len(source):
                break  # CR may be half of CRLF; do not commit it before the next chunk.
            index += 2 if source[index + 1] == "\n" else 1
            normalized.append("\n")
        else:
            index += 1
            normalized.append(character)
        offsets.append(index)
    return "".join(normalized), offsets


def _candidates(source: str):
    source, original_offsets = _normalize(source)
    starts = [0]
    starts.extend(index + 1 for index, character in enumerate(source) if character == "\n")

    def offset(position):
        row, column = position
        return starts[row - 1] + column if row <= len(starts) else len(source)

    indent = 0
    logical_start = True
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            kind = token.type
            if kind == tokenize.INDENT:
                indent += 1
            elif kind == tokenize.DEDENT:
                indent -= 1
            elif kind == tokenize.NEWLINE:
                # tokenize invents NEWLINE and DEDENT at EOF. Neither closes a live stream.
                if token.string and indent == 0:
                    yield original_offsets[offset(token.end)], False
                if token.string:
                    yield original_offsets[offset(token.end)], None
                    return
                logical_start = True
            elif kind == tokenize.NL and logical_start and token.string:
                # A blank/comment-only physical line has no lexical state to carry.
                yield original_offsets[offset(token.end)], None
                return
            elif kind not in _TRIVIA and kind != tokenize.ENDMARKER:
                if logical_start and indent == 0:
                    # A partial NAME such as `e` may still become `else`; wait for a delimiter.
                    stable = offset(token.end) < len(source) or kind == tokenize.OP or (kind == tokenize.NAME and not any(clause.startswith(token.string) for clause in _CLAUSES))
                    if stable and token.string not in _CLAUSES:
                        yield original_offsets[offset(token.start)], True
                logical_start = False
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Valid earlier tokens remain useful even when the unfinished suffix is not tokenizable.
        return


class Parser:
    """Emit only syntactically sealed source, never guessed closures.

    ``finish`` declares actual EOF. A failed finish closes the parser, because a
    transport EOF cannot subsequently be reinterpreted as another text chunk.
    """

    def __init__(self, *, filename: str = "<partial-python>"):
        self.filename = filename
        self._pending = ""
        self._offset = 0
        self._line = 1
        self._closed = False
        self._flags = 0
        self._scan = 0
        self._checked = 0
        self._checked_successor = False

    @property
    def pending(self) -> str:
        return self._pending

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def committed(self) -> int:
        return self._offset

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("parser is closed")

    def _statement(self, source: str) -> Statement:
        statement = Statement(source, self._offset, self._offset + len(source), self._line, self._line + source.count("\n") + source.count("\r") - source.count("\r\n"))
        self._offset = statement.end
        self._line = statement.end_line
        return statement

    def feed(self, delta: str) -> tuple[Statement, ...]:
        self._ensure_open()
        if not isinstance(delta, str):
            raise TypeError("delta must be str")
        self._pending += delta
        result = []
        consumed = 0
        scan = self._scan
        window_end = _line_end(self._pending, scan)
        while scan < len(self._pending):
            checkpoint = None
            for relative_end, successor in _candidates(self._pending[scan:window_end]):
                end = scan + relative_end
                if successor is None:
                    checkpoint = end
                    break
                if end <= consumed or end < self._checked or (end == self._checked and successor == self._checked_successor):
                    continue
                self._checked = end
                self._checked_successor = successor
                source = self._pending[consumed:end]
                try:
                    tree = _tree(source, self.filename, self._flags)
                except (SyntaxError, ValueError):
                    continue
                if not tree.body or (not successor and isinstance(tree.body[-1], _COMPOUND)):
                    continue
                for node in tree.body:
                    if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                        for name in node.names:
                            feature = getattr(__future__, name.name, None)
                            if isinstance(feature, __future__._Feature):
                                self._flags |= feature.compiler_flag
                result.append(self._statement(source))
                consumed = end
            if checkpoint is None or checkpoint <= scan:
                if window_end == len(self._pending):
                    break
                window_end = _line_end(self._pending, window_end)
            else:
                scan = checkpoint
                window_end = _line_end(self._pending, scan)
        # Restart tokenization only after a real logical line; never inside a
        # bracket, multiline string, or explicit continuation. Prior suite
        # indentation is validated when compiling the original whole statement.
        self._scan = max(scan - consumed, 0)
        self._checked = max(self._checked - consumed, 0)
        self._pending = self._pending[consumed:]
        return tuple(result)

    def finish(self) -> tuple[Statement, ...]:
        self._ensure_open()
        self._closed = True
        # Parse even a trivia-only tail: an unterminated comment string is harmless,
        # but a stray backslash or bracket must never disappear silently.
        _tree(self._pending, self.filename, self._flags, self._line)
        if not self._pending:
            return ()
        statement = self._statement(self._pending)
        self._pending = ""
        return (statement,)
