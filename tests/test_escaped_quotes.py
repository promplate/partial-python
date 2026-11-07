"""Escaped delimiters may skip retries, but never change a source boundary."""

import ast
import sys

import pytest
from _assertions import count_work

from partial_python import Parser

OPAQUE_PREFIXES = ["", "r", "R", "u", "U", "b", "B", "br", "bR", "Br", "BR", "rb", "rB", "Rb", "RB"]
NEWLINES = ["\n", "\r\n", "\r"]


def nodes(source):
    tree = compile(source, "<escaped-quotes>", "exec", flags=ast.PyCF_ONLY_AST, dont_inherit=True)
    return [ast.dump(node, include_attributes=False) for node in tree.body]


def compare_chunks(source, chunks, read_pending=True):
    expected = nodes(source)
    parser = Parser()
    reference = Parser()
    emitted = []
    actual = []
    received = committed = 0
    line = 1

    def inspect(units):
        nonlocal committed, line
        for unit in units:
            assert unit.start == committed
            assert unit.end == unit.start + len(unit.source) <= received
            assert unit.source == source[unit.start : unit.end]
            assert unit.start_line == line
            line += unit.source.count("\n") + unit.source.count("\r") - unit.source.count("\r\n")
            assert unit.end_line == line
            committed = unit.end
            emitted.append(unit.source)
            actual.extend(nodes(unit.source))
            assert actual == expected[: len(actual)]

    for index, chunk in enumerate(chunks):
        received += len(chunk)
        # Compare latency as well as final ASTs. The reference still performs
        # original-source parsing; it simply cannot skip a string retry.
        reference._await_quote = ""
        units = parser.feed(chunk)
        assert units == reference.feed(chunk), f"chunk {index}: {chunk!r}"
        inspect(units)
        assert parser.committed == reference.committed == committed
        if read_pending:
            assert parser.pending == reference.pending == source[committed:received]
    units = parser.finish()
    assert units == reference.finish()
    inspect(units)
    assert actual == expected
    assert "".join(emitted) == source
    assert parser.pending == reference.pending == ""
    return emitted


@pytest.mark.parametrize("prefix", OPAQUE_PREFIXES)
@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("read_pending", [False, True])
def test_long_odd_and_even_backslash_runs_preserve_each_emission(prefix, quote, read_pending):
    opening = "value = " + prefix + quote * 3 + "first\n"
    decoys = ["\\" * count + quote * 3 + " still inside\n" for count in (1, 3, 31, 65)]
    # Each slash and quote arrives separately, including runs longer than the
    # retained two-character window. Even runs must permit the real closer.
    for even in (0, 2, 32, 64):
        closing = "\\" * even + quote * 3 + "\nnext_value = 1\n"
        source = opening + "".join(decoys) + closing
        assert compare_chunks(source, [opening, *decoys, closing], read_pending) == compare_chunks(source, source, read_pending)


@pytest.mark.parametrize("prefix", OPAQUE_PREFIXES)
@pytest.mark.parametrize("quote", ["'", '"'])
def test_overlapping_four_quotes_close_after_a_cross_chunk_odd_run(prefix, quote):
    opening = "value = " + prefix + quote * 3 + "first\n"
    for count in (1, 3, 31, 65):
        slash_run = "\\" * count
        chunks = [opening, *slash_run, quote, quote, quote, quote + "\n", "next_value = 1\n"]
        source = "".join(chunks)
        assert compare_chunks(source, chunks) == [opening + slash_run + quote * 4 + "\n", "next_value = 1\n"]


@pytest.mark.parametrize("prefix", ["", "r", "u"])
@pytest.mark.parametrize("quote", ["'", '"'])
def test_overlapping_quote_runs_can_close_then_start_an_adjacent_literal(prefix, quote):
    opening = "value = " + prefix + quote * 3 + "first\n" + "\\" * 31
    # After the escaped first quote and the real triple closer, the remainder
    # can open a short literal, finish an empty one, or open another triple.
    for count, suffix in [(5, "tail" + quote + "\n"), (6, "\n"), (7, "tail\n" + quote * 3 + "\n")]:
        source = opening + quote * count + suffix + "next_value = 1\n"
        assert compare_chunks(source, [opening, *(quote * count), suffix, "next_value = 1\n"]) == compare_chunks(source, source)


@pytest.mark.parametrize("prefix", OPAQUE_PREFIXES)
@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("newline", NEWLINES)
def test_escaped_physical_newline_does_not_escape_next_quote(prefix, quote, newline):
    opening = "value = " + prefix + quote * 3 + "first"
    source = opening + "\\" * 33 + newline + quote * 3 + newline + "next_value = 1" + newline
    assert compare_chunks(source, source) == compare_chunks(source, [source])


@pytest.mark.parametrize("prefix", OPAQUE_PREFIXES)
@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("newline", NEWLINES)
def test_single_quoted_escaped_newline_and_inner_quote(prefix, quote, newline):
    # On hosts where an unfinished short string has no trustworthy STRING
    # witness, the existing conservative fallback must remain equivalent too.
    source = "value = " + prefix + quote + "first\\" + newline + "last\\" + quote + " inner" + quote + newline + "next_value = 1" + newline
    assert compare_chunks(source, source) == compare_chunks(source, [source])


@pytest.mark.parametrize("prefix", ["", "r", "u"])
@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("newline", NEWLINES)
def test_unicode_is_not_a_backslash_or_quote_and_cr_splits_preserve_offsets(prefix, quote, newline):
    opening = "变量 = " + prefix + quote * 3 + "🙂" + newline
    source = opening + "\\" * 31 + quote * 3 + "é" + newline + "＼" + quote * 3 + newline + "next_value = 1" + newline
    assert compare_chunks(source, source) == compare_chunks(source, [source])


@pytest.mark.parametrize("prefix", ["", "r", "u"])
def test_unicode_escape_decoding_cannot_create_a_lexical_closer(prefix):
    opening = "value = " + prefix + '"""first\n'
    body = r"\u0022\u0022\u0022\N{QUOTATION MARK}" + "\n"
    source = opening + body + '"""\nnext_value = 1\n'
    assert compare_chunks(source, [opening, *body, '"""\n', "next_value = 1\n"]) == compare_chunks(source, source)


@pytest.mark.parametrize("prefix", ["f", "F", "fr", "rf", pytest.param("t", marks=pytest.mark.skipif(sys.version_info < (3, 14), reason="template strings require Python 3.14"))])
@pytest.mark.parametrize("quote", ["'", '"'])
def test_interpolated_strings_skip_escaped_closers_without_delaying_real_boundaries(prefix, quote):
    opening = "value = " + prefix + quote * 3 + "{name!r:>{width}} first\n"
    body = "\\" * 31 + quote * 3 + " still inside\n"
    closing = "\\" * 3 + quote * 4 + "\nnext_value = 1\n"
    source = opening + body + closing
    assert compare_chunks(source, [opening, *body, *closing]) == compare_chunks(source, source)


@pytest.mark.parametrize("newline", NEWLINES)
@pytest.mark.parametrize("read_pending", [False, True])
def test_skipped_escaped_closers_preserve_native_finish_diagnostics(newline, read_pending):
    source = "ready = 1" + newline + "value = r'''first" + newline + ("\\" * 31 + "''' still inside" + newline) * 3
    parser = Parser(filename="original.py")
    for received, character in enumerate(source, 1):
        parser.feed(character)
        if read_pending:
            assert parser.pending == source[parser.committed : received]
    with pytest.raises(SyntaxError) as native:
        compile(source, "original.py", "exec", flags=ast.PyCF_ONLY_AST, dont_inherit=True)
    with pytest.raises(SyntaxError) as actual:
        parser.finish()
    assert actual.value.args == native.value.args
    assert parser.closed
    assert parser.pending == source[parser.committed :]


@pytest.mark.parametrize("prefix", ["", "r", "b", "br", "u", "f", "rf", pytest.param("t", marks=pytest.mark.skipif(sys.version_info < (3, 14), reason="template strings require Python 3.14"))])
@pytest.mark.parametrize("quote", ["'", '"'])
def test_repeated_escaped_triple_quotes_have_linear_scanner_work(monkeypatch, prefix, quote):
    def measure(count):
        opening = "value = " + prefix + quote * 3 + "first\n"
        line = "\\" * 33 + quote * 3 + " remains literal text\n"
        source = opening + line * count + quote * 3 + "\n"
        nodes(source)
        with count_work(monkeypatch) as work:
            parser = Parser()
            assert parser.feed(opening) == ()
            for _ in range(count):
                assert parser.feed(line) == ()
            assert [unit.source for unit in parser.feed(quote * 3 + "\n")] == [source]
        return len(source), work

    _, small = measure(64)
    length, large = measure(256)
    for operation in ("normalized", "tokenized"):
        assert large[operation] <= small[operation] * 5
        assert large[operation] < length * 12


@pytest.mark.parametrize("prefix", ["f", "rf", pytest.param("t", marks=pytest.mark.skipif(sys.version_info < (3, 14), reason="template strings require Python 3.14"))])
@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("newline", NEWLINES)
def test_escaped_delimiters_inside_format_spec_do_not_hide_the_outer_closer(prefix, quote, newline):
    triple = quote * 3
    source = "value = " + prefix + triple + "{name:" + "\\" + triple + "literal}tail" + triple + "\nnext_value = 1\n"
    source = source.replace("\n", newline)
    assert compare_chunks(source, source) == compare_chunks(source, [source])


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 701 expression grammar requires Python 3.12")
@pytest.mark.parametrize("prefix", ["f", "rf", pytest.param("t", marks=pytest.mark.skipif(sys.version_info < (3, 14), reason="template strings require Python 3.14"))])
@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("newline", NEWLINES)
def test_escaped_candidates_in_nested_strings_and_comments_preserve_every_emission(prefix, quote, newline):
    triple = quote * 3
    fixtures = [
        "value = " + prefix + triple + "{(\n    1 # \\" + triple + " comment\n)} \\" + triple + " literal\n" + triple + "\n",
        "value = " + prefix + triple + "{r" + triple + "inside \\" + triple + " decoy\ninner" + triple + "} outer \\" + triple + " decoy\n" + triple + "\n",
        "value = " + prefix + quote + "{(\n    1 # \\" + quote + " comment\n)} \\" + quote + " literal" + quote + "\n",
    ]
    for literal in fixtures:
        source = (literal + "next_value = 1\n").replace("\n", newline)
        expected = compare_chunks(source, source)
        for cut in range(len(source) + 1):
            assert compare_chunks(source, [source[:cut], source[cut:]]) == expected
