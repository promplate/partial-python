"""Inspectable display completions. No generated text is an execution boundary."""

import ast
from dataclasses import dataclass
from enum import IntFlag, auto
from typing import cast

from .parser import Parser


class Allow(IntFlag):
    """Opt into provisional display; these options never affect Parser or Executor."""

    NONE = 0
    TAIL = auto()
    STRING = auto()
    BRACKET = auto()
    SUITE = auto()
    ALL = TAIL | STRING | BRACKET | SUITE


@dataclass(frozen=True, slots=True)
class Completion:
    """``prefix + withheld`` is the input; ``prefix + suffix`` is display source.

    Only ``committed`` characters have passed Parser's streaming boundary rules.
    Even a preview with no synthetic suffix may contain an uncommitted statement.
    ``diagnostic`` describes the original input, and can simply mean incomplete.
    """

    prefix: str
    suffix: str
    withheld: str
    committed: int
    diagnostic: SyntaxError | None = None

    @property
    def source(self) -> str:
        return self.prefix + self.suffix

    @property
    def provisional(self) -> bool:
        return bool(self.suffix) or len(self.prefix) > self.committed


def _parse(source: str) -> ast.Module:
    return cast(ast.Module, compile(source, "<partial-python-preview>", "exec", flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT, dont_inherit=True))


def _closures(source: str, allow: Allow) -> str | None:
    # This scanner only proposes suffixes. CPython validates every proposal below;
    # unsupported syntax stays pending instead of being rewritten into other code.
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    stack: list[str] = []
    index = 0
    last = -1
    suffix = ""
    while index < len(source):
        char = source[index]
        if char == "#":
            end = source.find("\n", index)
            if end < 0:
                if stack:
                    suffix = "\n"
                break
            index = end + 1
            continue
        if char in "\"'":
            prefix_start = index
            while prefix_start and (source[prefix_start - 1].isalnum() or source[prefix_start - 1] == "_"):
                prefix_start -= 1
            prefix = source[prefix_start:index].lower()
            if prefix in {"f", "fr", "rf", "t", "tr", "rt"}:
                return None  # f/t strings have nested grammar; do not guess their braces or format specs.
            quote = char * (3 if source.startswith(char * 3, index) else 1)
            cursor = index + len(quote)
            closed = False
            partial_closer = 0
            while cursor < len(source):
                if source[cursor] == "\\":
                    cursor += 2
                elif source.startswith(quote, cursor):
                    cursor += len(quote)
                    closed = True
                    break
                elif len(quote) == 3 and len(source) - cursor < 3 and source[cursor:] in {char, char * 2}:
                    partial_closer = len(source) - cursor
                    cursor = len(source)
                elif len(quote) == 1 and source[cursor] in "\r\n":
                    return None
                else:
                    cursor += 1
            if not closed:
                if not allow & Allow.STRING or cursor > len(source):
                    return None
                # One or two quote characters at the end of a triple string are
                # the start of its closer; append only the missing characters.
                suffix = quote[partial_closer:]
                break
            index = cursor
            last = index - 1
            continue
        if char in "([{":
            stack.append(char)
        elif char in ")]}":
            if not stack or stack.pop() != {")": "(", "]": "[", "}": "{"}[char]:
                return None
        elif char == "\\" and index + 1 >= len(source):
            # Explicit line continuations need real source; do not invent an operand.
            return None
        if not char.isspace():
            last = index
        index += 1
    if stack:
        if not allow & Allow.BRACKET:
            return None
        suffix += "".join({"(": ")", "[": "]", "{": "}"}[char] for char in reversed(stack))
    if not suffix and last >= 0 and source[last] == ":" and allow & Allow.SUITE:
        header = source[source.rfind("\n", 0, last) + 1 : last]
        indent = header[: len(header) - len(header.lstrip(" \t"))]
        suffix = "\n" + indent + "    pass\n"
    return suffix or None


def complete(source: str, allow: Allow = Allow.NONE) -> Completion:
    """Complete a preview without altering or deleting interior source.

    Defaults to the exact committed prefix. Opt-in completion is display-only:
    STRING closes plain literals, BRACKET closes data/grouping delimiters, SUITE
    adds ``pass`` to an empty final suite, and TAIL includes already valid but
    unsealed source. Calls needing synthetic closers/arguments stay withheld.
    """
    if not isinstance(source, str):
        raise TypeError("source must be str")
    allow = Allow(allow)
    if int(allow) & ~int(Allow.ALL):
        raise ValueError("unknown preview flags")
    parser = Parser()
    parser.feed(source)
    committed = parser.committed
    diagnostic = None
    try:
        _parse(source)
    except SyntaxError as error:
        diagnostic = error
    else:
        if allow:
            return Completion(source, "", "", committed)
    suffix = _closures(source, allow) if allow else None
    if suffix:
        try:
            tree = _parse(source + suffix)
        except (SyntaxError, ValueError):
            pass
        else:
            # Closing a call would guess its final argument list, even when the
            # existing arguments happen to parse. Keep the entire pending unit.
            normalized = source.replace("\r\n", "\n").replace("\r", "\n")
            boundary = (normalized.count("\n") + 1, len(normalized.rsplit("\n", 1)[-1].encode()))
            closes_call = any(isinstance(node, ast.Call) and (node.end_lineno, node.end_col_offset) > boundary for node in ast.walk(tree))
            if not closes_call:
                return Completion(source, suffix, "", committed, diagnostic)
    prefix = source[:committed]
    _parse(prefix)
    return Completion(prefix, "", source[committed:], committed, diagnostic)
