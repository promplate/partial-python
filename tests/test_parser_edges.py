"""Regressions for eager release and ambiguous tokenizer end-of-buffer signals."""

import ast
import importlib.util
import random
from pathlib import Path

import pytest

from partial_python import Parser

ONE_LINE_ENDINGS = [
    "if condition: first()\n",
    "if condition: first()\nelse: second()\n",
    "if condition:\n    first()\nelif other: second()\n",
    "if condition:\n    first()\nelse: second()\n",
    "for item in items: visit(item)\n",
    "for item in items:\n    visit(item)\nelse: complete()\n",
    "while condition: first()\n",
    "while condition:\n    first()\nelse: complete()\n",
    "try: first()\nexcept Exception: second()\n",
    "try:\n    first()\nexcept Exception:\n    second()\nelse: complete()\n",
    "try:\n    first()\nfinally: cleanup()\n",
    "try: first()\nexcept* ValueError: second()\n",
    "with manager: first()\n",
    "async with manager: await first()\n",
    "async for item in items: await visit(item)\n",
    "def first(): return 1\n",
    "async def first(): return await call()\n",
    "class Example: value = 1\n",
    "@decorate\ndef first(): return 1\n",
    "match value:\n    case _: first()\n",
]


@pytest.mark.parametrize("source", ONE_LINE_ENDINGS)
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_new_successor_releases_compound_previously_held_at_same_offset(source, newline):
    source = source.replace("\n", newline)
    parser = Parser()
    assert parser.feed(source) == ()
    # A cached AST at the preceding NEWLINE must be reconsidered when that same
    # offset is now supported by a top-level successor. Waiting for next() to
    # close would delay effects even though the original compound is sealed.
    statements = parser.feed("next(")
    assert [statement.source for statement in statements] == [source]
    assert parser.pending == "next("
    assert parser.committed == len(source)


@pytest.mark.parametrize("source", ONE_LINE_ENDINGS)
def test_bulk_input_releases_closed_compound_before_unfinished_next_call(source):
    parser = Parser()
    assert [statement.source for statement in parser.feed(source + "next(")] == [source]
    assert parser.pending == "next("


def test_cached_compound_still_waits_for_continuation_clause():
    parser = Parser()
    source = "if condition: first()\n"
    assert parser.feed(source) == ()
    for character in "else: second()\n":
        assert parser.feed(character) == ()
    assert [statement.source for statement in parser.feed("last(")] == [source + "else: second()\n"]


def test_cached_empty_prefix_does_not_skip_first_real_statement():
    parser = Parser()
    prefix = "# initial comment\n\n# another\n"
    assert parser.feed(prefix) == ()
    for character in "value = (1 + 2)":
        assert parser.feed(character) == ()
    assert [statement.source for statement in parser.feed("\n")] == [prefix + "value = (1 + 2)\n"]
    assert parser.pending == ""


@pytest.mark.parametrize("successor", ["n", "x", "awa", "as", "fr", "exx", "elsew", "finally_", "(", "[", "{", "@", "+", "-", "..."])
def test_definite_successor_does_not_need_token_delimiter(successor):
    parser = Parser()
    source = "if condition: first()\n"
    assert parser.feed(source) == ()
    assert [statement.source for statement in parser.feed(successor)] == [source]
    assert parser.pending == successor


@pytest.mark.parametrize("successor", ["e", "el", "eli", "elif", "els", "else", "ex", "exce", "except", "f", "fi", "final", "finally"])
def test_possible_continuation_keyword_waits_for_disambiguation(successor):
    parser = Parser()
    source = "try: first()\nexcept Exception: second()\n"
    assert parser.feed(source) == ()
    assert parser.feed(successor) == ()
    assert parser.pending == source + successor


@pytest.mark.parametrize(
    "tail",
    [
        "value = 'unterminated",
        'value = "unterminated',
        "value = '''unterminated\n",
        "value = f'{",
        "value = r'unterminated",
        "value = b'unterminated",
        "value = (1,\n",
        "value = 1 \\\n",
        "@decorator(\n",
        "if True:\n    pass\n  bad_indent\n",
        "value = \0\n",
    ],
)
def test_invalid_tail_preserves_preceding_exact_source(tail):
    parser = Parser()
    prefix = "first()\n"
    assert [statement.source for statement in parser.feed(prefix + tail)] == [prefix]
    assert parser.pending == tail
    assert parser.committed == len(prefix)
    with pytest.raises((SyntaxError, ValueError)):
        parser.finish()
    assert parser.pending == tail


def test_unfinished_backslash_string_becomes_valid_without_losing_earlier_source():
    parser = Parser()
    assert [statement.source for statement in parser.feed("first()\nvalue = 'one\\\n")] == ["first()\n"]
    assert [statement.source for statement in parser.feed("two'\n")] == ["value = 'one\\\ntwo'\n"]


@pytest.mark.parametrize("tail", ["\ud800", "value = '\udfff'\n", "# invalid source character \ud800\n"])
@pytest.mark.parametrize("separate_chunk", [False, True])
def test_unencodable_tail_does_not_lose_already_committed_prefix(tail, separate_chunk):
    parser = Parser()
    prefix = "first()\n"
    if separate_chunk:
        statements = parser.feed(prefix)
        assert parser.feed(tail) == ()
    else:
        statements = parser.feed(prefix + tail)
    assert [statement.source for statement in statements] == [prefix]
    assert parser.committed == len(prefix)
    assert parser.pending == tail
    with pytest.raises(UnicodeError):
        parser.finish()
    assert parser.closed
    assert parser.pending == tail


def test_ascii_escape_for_surrogate_is_valid_source():
    source = "value = '\\ud800'\n"
    parser = Parser()
    assert [statement.source for statement in parser.feed(source)] == [source]
    assert parser.finish() == ()


@pytest.mark.parametrize("module", ["contextlib", "functools", "dataclasses", "argparse", "inspect"])
def test_real_stdlib_corpus_retains_statement_asts(module):
    spec = importlib.util.find_spec(module)
    if not spec or not spec.origin or spec.origin in {"frozen", "built-in"}:
        pytest.skip(f"stdlib source unavailable for {module}")
    source = Path(spec.origin).read_text(encoding="utf-8")
    expected = [ast.dump(node, include_attributes=False) for node in ast.parse(source).body]
    rng = random.Random(32171)
    parser = Parser()
    statements = []
    offset = 0
    while offset < len(source):
        size = rng.randint(64, 4096)
        statements.extend(parser.feed(source[offset : offset + size]))
        offset += size
    statements.extend(parser.finish())
    assert "".join(statement.source for statement in statements) == source
    actual = [ast.dump(node, include_attributes=False) for statement in statements for node in ast.parse(statement.source).body]
    assert actual == expected
