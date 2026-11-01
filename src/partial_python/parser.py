"""Conservative boundaries for append-only Python source."""

import __future__

import ast
import io
import re
import tokenize
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Literal, cast


@dataclass(frozen=True, slots=True)
class CodeUnit:
    """An exact source slice; offsets count Unicode characters, lines are one-based."""

    source: str
    start: int
    end: int
    start_line: int
    end_line: int


_COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.TryStar, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Match)
_CLAUSES = frozenset({"elif", "else", "except", "finally"})
_LINE_END = re.compile(r"[\r\n]")
_FUTURES = {name: getattr(__future__, name).compiler_flag for name in __future__.all_feature_names}
_TRIVIA = frozenset({tokenize.COMMENT, tokenize.NL, tokenize.ENCODING})


def _tree(source: str, filename: str, flags: int = 0, start_line: int = 1) -> ast.Module:
    try:
        return cast(ast.Module, compile(source, filename, "exec", flags=flags | ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT, dont_inherit=True))
    except SyntaxError:
        if start_line == 1:
            raise
        # Only the error path pays for leading lines. Let CPython construct both
        # serialized locations and messages that themselves mention a line number.
        return cast(ast.Module, compile("\n" * (start_line - 1) + source, filename, "exec", flags=flags | ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT, dont_inherit=True))


def _source_line(source: str, line: int) -> str:
    # str.splitlines also splits formfeeds and Unicode separators, unlike Python.
    start = 0
    for _ in range(max(0, line - 1)):
        match = _LINE_END.search(source, start)
        if match is None:
            return ""
        start = match.end() + (match[0] == "\r" and source[match.end() : match.end() + 1] == "\n")
    match = _LINE_END.search(source, start)
    if match is None:
        return source[start:]
    end = match.end() + (match[0] == "\r" and source[match.end() : match.end() + 1] == "\n")
    return source[start:end]


def _line_end(source: str, start: int) -> int:
    match = _LINE_END.search(source, start)
    if match is None:
        return len(source)
    # One lookahead character resolves a bare CR versus the first half of CRLF.
    return min(match.end() + (match[0] == "\r"), len(source))


def _normalize(source: str) -> tuple[str, Sequence[int]]:
    if "\r" not in source:
        return source, range(len(source) + 1)
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


_STRING_STARTS = frozenset(getattr(tokenize, name) for name in ("FSTRING_START", "TSTRING_START") if hasattr(tokenize, name))
_STRING_ENDS = frozenset(getattr(tokenize, name) for name in ("FSTRING_END", "TSTRING_END") if hasattr(tokenize, name))


@dataclass(frozen=True, slots=True)
class _Boundary:
    offset: int
    kind: Literal["successor", "newline", "resume", "continuation", "started", "string"]
    delimiters: tuple[str, ...] = ()
    logical_root: bool = True
    quote: str = ""


def _quote_delimiter(token_text: str) -> str:
    marker = token_text.lstrip("rRuUbBfFtT")
    return marker[:3] if marker[:3] in ("'''", '"""') else marker[:1]


def _unfinished_quote(source: str, last_end: tuple[int, int]) -> tuple[str, tuple[int, int]] | None:
    eof = (source.count("\n") + 1, len(source.rsplit("\n", 1)[-1]))
    # Ask the host tokenizer to identify an opaque STRING crossing actual EOF.
    # Four quotes also close a trailing backslash. Never reinterpret an already
    # emitted token (e.g. r'' becoming r''' merely because of the probe suffix).
    for quote in ("'", '"'):
        try:
            for token in tokenize.generate_tokens(io.StringIO(source + quote * 4).readline):
                if token.type == tokenize.STRING and last_end <= token.start < eof < token.end:
                    return _quote_delimiter(token.string), token.start
        except (tokenize.TokenError, IndentationError, SyntaxError, UnicodeError):
            pass
    return None


def _candidates(source: str, delimiters: tuple[str, ...], logical_root: bool) -> Iterator[_Boundary]:
    source, original_offsets = _normalize(source)
    starts = [0]
    starts.extend(index + 1 for index, character in enumerate(source) if character == "\n")
    prefix = "".join(delimiters) + "\n" if delimiters else ""
    row_shift = bool(prefix)

    def offset(position: tuple[int, int]) -> int:
        row, column = position
        row -= row_shift
        return starts[row - 1] + column if row <= len(starts) else len(source)

    # This synthetic prefix restores lexical bracket context only. It never
    # enters a CodeUnit or the compiler. Checkpoints carry exact opener kinds
    # and the ORIGINAL logical line's indentation, not the prefix's column zero.
    stack = list(delimiters)
    string_quotes: list[str] = []
    last_end = (1, 0)
    lexical_error = False
    logical_start = not delimiters
    try:
        for token in tokenize.generate_tokens(io.StringIO(prefix + source).readline):
            if token.start[0] <= row_shift:
                continue
            kind = token.type
            if token.string and kind not in {tokenize.DEDENT, tokenize.ENDMARKER}:
                last_end = token.end
            if kind in _STRING_STARTS:
                string_quotes.append(_quote_delimiter(token.string))
            elif kind in _STRING_ENDS:
                string_quotes.pop()
            elif kind == tokenize.ERRORTOKEN:
                lexical_error = True
            elif kind == tokenize.OP:
                if token.string in "([{":
                    stack.append(token.string)
                elif token.string in ")]}" and (not stack or stack.pop() != {")": "(", "]": "[", "}": "{"}[token.string]):
                    return
            if kind == tokenize.INDENT:
                logical_root = False
            elif kind == tokenize.DEDENT:
                continue
            elif kind == tokenize.NEWLINE:
                # tokenize invents NEWLINE and DEDENT at EOF. Neither closes a live stream.
                if token.string and logical_root:
                    yield _Boundary(original_offsets[offset(token.end)], "newline")
                if token.string:
                    yield _Boundary(original_offsets[offset(token.end)], "resume")
                    return
                logical_start = True
            elif kind == tokenize.NL and token.string:
                if logical_start and not stack:
                    yield _Boundary(original_offsets[offset(token.end)], "resume")
                    return
                if stack and not string_quotes and not lexical_error:
                    # Never checkpoint f/t-string lexical state or a partial
                    # token. Persist only this completed NL snapshot, not any
                    # transient delimiter changes observed at temporary EOF.
                    yield _Boundary(original_offsets[offset(token.end)], "continuation", tuple(stack), logical_root)
                    return
            elif kind not in _TRIVIA and kind != tokenize.ENDMARKER:
                if logical_start and logical_root:
                    stable = (
                        offset(token.end) < len(source) or kind == tokenize.OP or kind in _STRING_STARTS or (kind == tokenize.NAME and not any(clause.startswith(token.string) for clause in _CLAUSES))
                    )
                    if stable and token.string not in _CLAUSES:
                        yield _Boundary(original_offsets[offset(token.start)], "successor")
                    if stable:
                        yield _Boundary(original_offsets[offset(token.start)], "started")
                elif logical_start:
                    yield _Boundary(original_offsets[offset(token.start)], "started")
                logical_start = False
    except (tokenize.TokenError, IndentationError, SyntaxError, UnicodeError):
        if string_quotes:
            yield _Boundary(0, "string", quote=string_quotes[0])
        elif opening := _unfinished_quote(prefix + source, last_end):
            quote, position = opening
            if logical_start:
                start = original_offsets[offset(position)]
                # A host-proven string opener already excludes continuation
                # clauses; its unfinished contents need not delay a predecessor.
                if logical_root:
                    yield _Boundary(start, "successor")
                yield _Boundary(start, "started")
            yield _Boundary(0, "string", quote=quote)
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
        self._chunks: list[str] = []
        self._await_quote = ""
        self._quote_tail = ""
        self._offset = 0
        self._line = 1
        self._closed = False
        self._flags = 0
        self._scan = 0
        self._examined = 0
        self._line_started = False
        self._delimiters: tuple[str, ...] = ()
        self._logical_root = True
        self._checked = 0
        self._checked_successor = False

    @property
    def pending(self) -> str:
        if self._chunks:
            self._pending += "".join(self._chunks)
            self._chunks.clear()
        return self._pending

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def committed(self) -> int:
        return self._offset

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("parser is closed")

    def _statement(self, source: str) -> CodeUnit:
        statement = CodeUnit(source, self._offset, self._offset + len(source), self._line, self._line + source.count("\n") + source.count("\r") - source.count("\r\n"))
        self._offset = statement.end
        self._line = statement.end_line
        return statement

    def feed(self, delta: str) -> tuple[CodeUnit, ...]:
        self._ensure_open()
        if not isinstance(delta, str):
            raise TypeError("delta must be str")
        if self._await_quote and self._await_quote not in self._quote_tail + delta:
            if delta:
                self._chunks.append(delta)
                self._quote_tail = (self._quote_tail + delta)[-2:]
            return ()
        self._await_quote = ""
        resolves_cr = (self._chunks[-1] if self._chunks else self._pending).endswith("\r")
        # Once the first token is unambiguous, only a physical line ending can
        # produce NEWLINE. Skip growing operands/literals until that can happen.
        if self._line_started and not resolves_cr and "\n" not in delta and "\r" not in delta:
            if delta:
                # Join only when scanning or reading diagnostics; copying the
                # growing operand on every small delta is otherwise quadratic.
                self._chunks.append(delta)
            return ()
        self._pending = self.pending + delta
        result = []
        consumed = 0
        scan = self._scan
        window_end = len(self._pending) if self._examined > scan else _line_end(self._pending, scan)
        examined = scan
        while scan < len(self._pending):
            checkpoint = None
            for boundary in _candidates(self._pending[scan:window_end], self._delimiters, self._logical_root):
                end = scan + boundary.offset
                kind = boundary.kind
                if kind == "string":
                    if window_end == len(self._pending):
                        self._await_quote = boundary.quote
                        self._quote_tail = self._pending[-2:]
                    continue
                if kind == "started":
                    self._line_started = True
                    continue
                if kind in {"resume", "continuation"}:
                    self._line_started = kind == "continuation"
                    self._delimiters = boundary.delimiters
                    self._logical_root = boundary.logical_root
                    checkpoint = end
                    break
                successor = kind == "successor"
                if end <= consumed or end < self._checked or (end == self._checked and successor == self._checked_successor):
                    continue
                self._checked = end
                self._checked_successor = successor
                source = self._pending[consumed:end]
                try:
                    tree = _tree(source, self.filename, self._flags)
                except (SyntaxError, ValueError, UnicodeError):
                    continue
                if not tree.body or (not successor and isinstance(tree.body[-1], _COMPOUND)):
                    continue
                for node in tree.body:
                    if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                        for name in node.names:
                            self._flags |= _FUTURES.get(name.name, 0)
                result.append(self._statement(source))
                consumed = end
            if checkpoint is None or checkpoint <= scan:
                examined = window_end
                if window_end == len(self._pending):
                    break
                # Exponential windows avoid revisiting every prior physical line
                # of an unfinished list/string in one large transport chunk.
                window_end = min(len(self._pending), scan + max(1, window_end - scan) * 2)
            else:
                scan = checkpoint
                examined = scan
                window_end = _line_end(self._pending, scan)
        # Lexer checkpoints retain only reconstructible state. Original-source
        # compilation still validates the complete suite and every source unit.
        self._scan = max(scan - consumed, 0)
        self._examined = max(examined - consumed, 0)
        self._checked = max(self._checked - consumed, 0)
        self._pending = self._pending[consumed:]
        return tuple(result)

    def finish(self) -> tuple[CodeUnit, ...]:
        self._ensure_open()
        self._closed = True
        # Parse even a trivia-only tail: an unterminated comment string is harmless,
        # but a stray backslash or bracket must never disappear silently.
        _tree(self.pending, self.filename, self._flags, self._line)
        if not self._pending:
            return ()
        statement = self._statement(self._pending)
        self._pending = ""
        return (statement,)
