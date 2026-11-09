"""Internal lexical events for explicitly enabled streaming suites.

Header probes recover AST fields only. Their dummy bodies never become events,
code units, or executable code; every source span points into the original input.
"""

import ast
import io
import re
import tokenize
from dataclasses import dataclass, field
from typing import Literal

from .blocks import Block
from .parser import _COMPOUND, _FUTURES, CodeUnit, _candidates, _has_unescaped_closer, _line_end, _normalize, _quote_window, _tree


@dataclass(frozen=True, slots=True)
class BlockEvent:
    kind: Literal["enter", "branch", "statement", "exit"]
    unit: CodeUnit
    depth: int
    node: ast.Module | ast.If | ast.With | ast.AsyncWith | None = None


@dataclass(slots=True)
class _Frame:
    node: ast.If | ast.With | ast.AsyncWith
    unit: CodeUnit
    indent: str
    width: int
    alternate: int
    body_width: int | None = None
    body_alternate: int | None = None
    body_indent: str | None = None
    seen_body: bool = False
    seen_else: bool = False
    inline: bool = False
    parts: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Line:
    start: int
    end: int | None
    tokens: tuple[tokenize.TokenInfo, ...]
    indent: str
    stable: bool
    row_shift: int
    token_start: int

    @property
    def word(self) -> str:
        return self.tokens[0].string


_TRIVIA = frozenset({tokenize.COMMENT, tokenize.NL, tokenize.ENCODING, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER})
_CLAUSES = ("elif", "else", "except", "finally")
_COMPOUND_WORDS = frozenset({"if", "for", "while", "with", "try", "def", "class", "match", "async", "@"})


def _width(indent: str, tab: int) -> int:
    column = 0
    for char in indent:
        if char == "\t":
            column = (column // tab + 1) * tab
        elif char == "\f":
            column = 0
        else:
            column += 1
    return column


def _lines(source: str, prefix: str, eof: bool, *, first_only: bool = False) -> tuple[_Line, ...]:
    # CPython 3.12's tokenizer can raise SystemError for NUL. It can never be
    # repaired by appending; leave the original span for CPython's EOF diagnostic.
    if "\0" in source:
        return ()
    normalized, offsets = _normalize(source + "\n" if eof and source.endswith("\r") else source)
    if eof and source.endswith("\r"):
        offsets = [min(offset, len(source)) for offset in offsets]
    starts = [0, *(index + 1 for index, char in enumerate(normalized) if char == "\n")]
    rows = prefix.count("\n")
    result: list[_Line] = []
    tokens: list[tokenize.TokenInfo] = []
    indentation = [""]
    current_start = 0
    current_indent = ""

    def offset(position: tuple[int, int]) -> int:
        row, column = position
        row -= rows
        index = starts[row - 1] + column if row <= len(starts) else len(normalized)
        return offsets[min(index, len(offsets) - 1)]

    def finish(end: int | None) -> None:
        if not tokens:
            return
        first = tokens[0]
        start = current_start
        indent = current_indent
        leading = source[start : offset(first.start)]
        joined = list(re.finditer(r"([ \t\f]*)\\(?:\r\n|\r|\n)", leading))
        if joined and joined[0].start() == 0:
            # Python 3.11's tokenizer omits indentation after explicit joins.
            # CPython uses the first nonempty indentation in this physical chain.
            tail = leading[joined[-1].end() :]
            if not tail.strip(" \t\f"):
                indent = next((match[1] for match in joined if _width(match[1], 8)), tail)
        # An unfinished name can still grow into else/elif. Lexical punctuation,
        # a complete physical line, or a following character seals its identity.
        stable = end is not None or offset(first.end) < len(source) or first.type in {tokenize.OP, tokenize.NUMBER, tokenize.STRING}
        stable = stable or (first.type == tokenize.NAME and not any(clause.startswith(first.string) for clause in _CLAUSES))
        result.append(_Line(start, end, tuple(tokens), indent, stable, rows, offset(first.start)))
        tokens.clear()

    try:
        for token in tokenize.generate_tokens(io.StringIO(prefix + normalized).readline):
            if token.type == tokenize.INDENT:
                indentation.append(token.string)
            elif token.type == tokenize.DEDENT and len(indentation) > 1:
                indentation.pop()
            if token.start[0] <= rows:
                continue
            if token.type == tokenize.NEWLINE:
                finish(offset(token.end) if token.string or eof else None)
                if first_only and result:
                    break
                if token.string:
                    current_start = offset(token.end)
            elif token.type == tokenize.NL and not tokens:
                current_start = offset(token.end)
            elif token.type not in _TRIVIA:
                if not tokens:
                    current_indent = indentation[-1]
                tokens.append(token)
    except (tokenize.TokenError, IndentationError, SyntaxError, UnicodeError):
        pass
    finish(None)
    return tuple(result)


class BlockParser:
    """Internal parser; complete public CodeUnits remain the responsibility of Parser."""

    def __init__(self, *, filename: str = "<partial-python>", stream_blocks: Block = Block.MODULE):
        self.filename = filename
        self.blocks = stream_blocks
        self._chunks: list[str] = []
        self._pending_chunks: list[str] = []
        self._pending = ""
        self._offset = 0
        self._committed = 0
        self._line = 1
        self._closed = False
        self._frames: list[_Frame] = []
        self._flags = 0
        self._reset_scan()

    @property
    def pending(self) -> str:
        return "".join(self._chunks)[self._committed :]

    @property
    def committed(self) -> int:
        return self._committed

    @property
    def closed(self) -> bool:
        return self._closed

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("parser is closed")

    def _reset_scan(self) -> None:
        self._scan = 0
        self._retry_full_window = False
        self._line_started = False
        self._delimiters: tuple[str, ...] = ()
        self._logical_root = True
        self._await_quote = ""
        self._quote_tail = ""
        self._quote_tail_escaped = False
        self._opaque_width: int | None = None

    def _ready(self) -> bool:
        # Reuse Parser's lexical checkpoints only as a wakeup gate. Full original
        # spans still pass _lines/_parse before any executable event is produced.
        scan = self._scan
        window_end = len(self._pending) if self._retry_full_window else _line_end(self._pending, scan)
        self._retry_full_window = False
        while scan < len(self._pending):
            checkpoint = None
            ready = False
            window = self._pending[scan:window_end]
            if "\0" in window:
                return False
            for boundary in _candidates(window, self._delimiters, self._logical_root):
                kind = boundary.kind
                if kind == "string":
                    if window_end == len(self._pending):
                        self._await_quote = boundary.quote
                        self._quote_tail, self._quote_tail_escaped = _quote_window(self._pending, False)
                    continue
                if kind == "started":
                    self._line_started = True
                    if not self._delimiters and (self._frames or self._opaque_width is not None):
                        lines = _lines(window, self._lexical_prefix(), False, first_only=True)
                        if lines:
                            first = lines[0]
                            width = _width(first.indent, 8)
                            edge = self._opaque_width if self._opaque_width is not None else self._frames[-1].width
                            if width <= edge:
                                self._line_started = first.stable
                                ready = first.stable and (self._opaque_width is None or width < edge or first.word not in _CLAUSES)
                    continue
                if kind in {"resume", "continuation"}:
                    self._line_started = kind == "continuation"
                    self._delimiters = boundary.delimiters
                    self._logical_root = boundary.logical_root
                    checkpoint = scan + boundary.offset
                    ready |= kind == "resume" and self._opaque_width is None
                    break
            if checkpoint is not None and checkpoint > scan:
                scan = checkpoint
                self._scan = scan
                if ready:
                    return True
                window_end = _line_end(self._pending, scan)
            else:
                if ready:
                    self._scan = scan
                    return True
                if window_end == len(self._pending):
                    self._scan = scan
                    self._retry_full_window = True
                    # A comment cannot become a statement before its newline.
                    self._line_started |= not self._delimiters and window.lstrip(" \t\f").startswith("#")
                    break
                window_end = min(len(self._pending), scan + max(1, window_end - scan) * 2)
        return False

    def _prefix(self) -> str:
        return "".join(frame.indent + "if True:\n" for frame in self._frames)

    def _lexical_prefix(self) -> str:
        # Existing body indentation must be present before an explicit join:
        # tokenize otherwise loses it when the next physical line starts at zero.
        return "".join(frame.indent + "if True:\n" + (frame.body_indent + "pass\n" if frame.body_indent is not None else "") for frame in self._frames)

    def _parse(self, source: str) -> ast.Module:
        prefix = self._prefix()
        tree = _tree(prefix + source, self.filename, self._flags)
        body = tree.body
        for _ in self._frames:
            node = body[0]
            assert isinstance(node, ast.If), "lexical scope prefix changed"
            body = node.body
        result = ast.Module(body=body, type_ignores=[])
        ast.increment_lineno(result, self._line - 1 - prefix.count("\n"))
        return result

    def _unit(self, end: int) -> CodeUnit:
        source = self._pending[:end]
        last_line = self._line + source.count("\n") + source.count("\r") - source.count("\r\n")
        return CodeUnit(source, self._offset, self._offset + end, self._line, last_line)

    def _consume(self, end: int) -> CodeUnit:
        unit = self._unit(end)
        for frame in self._frames:
            frame.parts.append(unit.source)
        self._offset = unit.end
        self._line = unit.end_line
        self._pending = self._pending[end:]
        self._reset_scan()
        return unit

    def _statement(self, end: int, tree: ast.Module) -> BlockEvent:
        if self._frames and tree.body:
            self._frames[-1].seen_body = True
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                for name in node.names:
                    self._flags |= _FUTURES.get(name.name, 0)
        unit = self._consume(end)
        if not self._frames:
            self._committed = unit.end
        return BlockEvent("statement", unit, len(self._frames), node=tree)

    def _header(self, line: _Line, branch: bool = False) -> tuple[ast.If | ast.With | ast.AsyncWith, list[ast.stmt], int] | None:
        if line.end is None or line.word not in {"if", "with", "async", "elif", "else"}:
            return None
        source = self._pending[: line.end]
        if branch:
            # Equal-width replacements preserve the original expression columns.
            start = line.token_start
            source = source[:start] + ("if  " if line.word == "elif" else "if 1") + source[start + 4 :]
        multiline = line.tokens[-1].string == ":"
        try:
            tree = self._parse(source + line.indent + "    pass\n" if multiline else source)
        except (SyntaxError, ValueError, UnicodeError):
            return None
        if len(tree.body) != 1 or not isinstance(tree.body[0], (ast.If, ast.With, ast.AsyncWith)):
            return None
        node = tree.body[0]
        body = [] if multiline else node.body
        node.body = []
        if isinstance(node, ast.If):
            node.orelse = []
        if multiline:
            colon = line.tokens[-1]
            end = line.end
        else:
            first = body[0]
            before = [
                token
                for token in line.tokens
                if token.string == ":" and (self._line + token.start[0] - line.row_shift - 1, len(token.line[: token.start[1]].encode())) < (first.lineno, first.col_offset)
            ]
            if not before:
                return None
            colon = before[-1]
            normalized, offsets = _normalize(self._pending)
            row = colon.end[0] - line.row_shift
            starts = [0, *(index + 1 for index, char in enumerate(normalized) if char == "\n")]
            end = offsets[starts[row - 1] + colon.end[1]]
        # A probe pass is never exposed, even through the header's end range.
        node.end_lineno = self._line + colon.end[0] - line.row_shift - 1
        node.end_col_offset = len(colon.line[: colon.end[1]].encode())
        return node, body, end

    def _enabled(self, node: ast.If | ast.With | ast.AsyncWith) -> bool:
        flag = Block.IF if isinstance(node, ast.If) else Block.ASYNC_WITH if isinstance(node, ast.AsyncWith) else Block.WITH
        return bool(self.blocks & flag)

    def _exit(self) -> BlockEvent:
        frame = self._frames.pop()
        self._reset_scan()
        unit = CodeUnit("".join(frame.parts), frame.unit.start, self._offset, frame.unit.start_line, self._line)
        if not self._frames:
            self._committed = unit.end
        return BlockEvent("exit", unit, len(self._frames))

    def _first_line(self, eof: bool) -> tuple[_Line, ...]:
        end = _line_end(self._pending, 0)
        while True:
            lines = _lines(self._pending[:end], self._lexical_prefix(), eof and end == len(self._pending), first_only=True)
            if (lines and lines[0].end is not None) or end == len(self._pending):
                return lines
            end = min(len(self._pending), max(end * 2, 1))

    def _drain(self, eof: bool = False) -> tuple[BlockEvent, ...]:
        events: list[BlockEvent] = []
        while self._pending:
            lines = self._first_line(eof)
            if not lines:
                if eof:
                    events.append(self._statement(len(self._pending), ast.Module(body=[], type_ignores=[])))
                break
            first = lines[0]
            width, alternate = _width(first.indent, 8), _width(first.indent, 1)
            if self._frames:
                frame = self._frames[-1]
                if width <= frame.width:
                    if width == frame.width and alternate != frame.alternate:
                        break
                    if not frame.seen_body:
                        break
                    is_branch = isinstance(frame.node, ast.If) and width == frame.width and first.word in {"elif", "else"}
                    if width == frame.width and isinstance(frame.node, ast.If) and not first.stable:
                        break
                    if is_branch:
                        if first.end is None or frame.seen_else:
                            break
                        saved = self._frames.pop()
                        header = self._header(first, branch=True)
                        self._frames.append(saved)
                        if header is None:
                            break
                        node, body, end = header
                        frame.seen_else = first.word == "else"
                        frame.seen_body = False
                        frame.body_width = frame.body_alternate = None
                        frame.body_indent = None
                        frame.inline = bool(body)
                        events.append(BlockEvent("branch", self._consume(end), len(self._frames) - 1, node if first.word == "elif" else None))
                        if body:
                            events.append(self._statement(first.end - end, ast.Module(body=body, type_ignores=[])))
                        continue
                    if first.tokens[0].type == tokenize.ERRORTOKEN:
                        break
                    # Leading comments belong to the suite that just ended. A
                    # backslash join is not trivia and stays with its successor.
                    if first.start:
                        try:
                            trivia = self._parse(self._pending[: first.start])
                        except (SyntaxError, ValueError, UnicodeError):
                            trivia = None
                        if trivia is not None and not trivia.body:
                            events.append(self._statement(first.start, trivia))
                            continue
                    events.append(self._exit())
                    continue
                if frame.inline:
                    break
                if frame.body_width is None:
                    if alternate <= frame.alternate:
                        break
                    frame.body_width, frame.body_alternate = width, alternate
                    frame.body_indent = first.indent
                if width != frame.body_width or alternate != frame.body_alternate:
                    break
            elif width:
                break
            header = self._header(first)
            if header is not None and self._enabled(header[0]):
                node, body, end = header
                if self._frames:
                    self._frames[-1].seen_body = True
                unit = self._consume(end)
                events.append(BlockEvent("enter", unit, len(self._frames), node))
                self._frames.append(_Frame(node, unit, first.indent, width, alternate, inline=bool(body), parts=[unit.source]))
                assert first.end is not None
                if body:
                    events.append(self._statement(first.end - end, ast.Module(body=body, type_ignores=[])))
                continue
            if first.end is not None:
                try:
                    tree = self._parse(self._pending[: first.end])
                except (SyntaxError, ValueError, UnicodeError):
                    tree = None
                if tree is not None and not any(isinstance(node, _COMPOUND) for node in tree.body):
                    events.append(self._statement(first.end, tree))
                    continue
            compound = first.word in _COMPOUND_WORDS
            if compound:
                self._opaque_width = width
                lines = _lines(self._pending, self._lexical_prefix(), eof)
                end = None
                opaque_tree = None
                for successor in lines[1:]:
                    successor_width = _width(successor.indent, 8)
                    if successor_width > width or (successor_width == width and (not successor.stable or successor.word in _CLAUSES)):
                        continue
                    try:
                        tree = self._parse(self._pending[: successor.start])
                    except (SyntaxError, ValueError, UnicodeError):
                        continue
                    if tree.body:
                        end = successor.start
                        opaque_tree = tree
                        break
                if end is not None and opaque_tree is not None:
                    events.append(self._statement(end, opaque_tree))
                    continue
            if eof:
                tree = self._parse(self._pending)
                events.append(self._statement(len(self._pending), tree))
                continue
            break
        if eof:
            if self._pending:
                raise SyntaxError("could not seal the remaining streaming suite", (self.filename, self._line, 1, self._pending.split("\n", 1)[0]))
            while self._frames:
                events.append(self._exit())
        return tuple(events)

    def feed(self, delta: str) -> tuple[BlockEvent, ...]:
        self._ensure_open()
        if not isinstance(delta, str):
            raise TypeError("delta must be str")
        resolves_cr = self._pending.endswith("\r")
        if delta:
            self._chunks.append(delta)
            self._pending_chunks.append(delta)
        if self._await_quote:
            window = self._quote_tail + delta
            if not _has_unescaped_closer(window, self._await_quote, self._quote_tail_escaped):
                self._quote_tail, self._quote_tail_escaped = _quote_window(window, self._quote_tail_escaped)
                return ()
            self._await_quote = ""
        if self._line_started and not resolves_cr and "\n" not in delta and "\r" not in delta:
            return ()
        self._pending += "".join(self._pending_chunks)
        self._pending_chunks.clear()
        return self._drain() if self._ready() else ()

    def finish(self) -> tuple[BlockEvent, ...]:
        self._ensure_open()
        self._closed = True
        # Only actual EOF validates the entire original program and authorizes
        # closure of remaining suites. No probe participates in this validation.
        self._pending += "".join(self._pending_chunks)
        self._pending_chunks.clear()
        _tree("".join(self._chunks), self.filename, self._flags)
        return self._drain(True)
