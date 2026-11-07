"""Bound avoidable rescans without promising linear streaming of a giant token."""

import pytest
from _assertions import count_work

from partial_python import Parser

REGIONS = [
    ("value = [\n", "    123,\n", "]\n"),
    ("value = '''\n", "a line of literal text\n", "'''\n"),
    ('value = f"""\n', "a line of literal text\n", '{name}"""\n'),
]


def measured_parse(monkeypatch, opening, line, closing, count, *, bulk):
    source = opening + line * count + closing
    with count_work(monkeypatch) as work:
        parser = Parser()
        if bulk:
            statements = parser.feed(source)
        else:
            assert parser.feed(opening) == ()
            for _ in range(count):
                assert parser.feed(line) == ()
            statements = parser.feed(closing)
        assert [statement.source for statement in statements] == [source]
        assert parser.pending == ""
        assert parser.finish() == ()
    return len(source), work["normalized"]


@pytest.mark.parametrize("opening,line,closing", REGIONS)
def test_open_multiline_region_does_not_rescan_every_old_window_on_each_feed(monkeypatch, opening, line, closing):
    _, small_work = measured_parse(monkeypatch, opening, line, closing, 64, bulk=False)
    _, large_work = measured_parse(monkeypatch, opening, line, closing, 128, bulk=False)
    # tokenize cannot resume halfway through a string. Re-reading the open
    # region per new chunk can be quadratic, but replaying every old physical
    # line window again per chunk introduces an unnecessary cubic factor.
    assert large_work <= small_work * 5


@pytest.mark.parametrize("opening,line,closing", REGIONS)
def test_bulk_multiline_region_uses_geometrically_growing_probe_windows(monkeypatch, opening, line, closing):
    length, work = measured_parse(monkeypatch, opening, line, closing, 128, bulk=True)
    # A single already-available region needs no repeated per-line history
    # reconstruction. Doubling probes bounds the sum of examined prefixes.
    assert work < length * 8


@pytest.mark.parametrize("opening,closing", [("value = '", "'\n"), ("consume('", "')\n"), ("value = [", "]\n"), ("[", "]\n")])
@pytest.mark.parametrize("chunk_size", [1, 16, 256])
def test_started_physical_line_is_not_retokenized_without_a_newline(monkeypatch, opening, closing, chunk_size):
    def measured(size):
        source = opening + ("1," * size if "[" in opening else "a" * size) + closing
        with count_work(monkeypatch) as work:
            parser = Parser()
            statements = []
            for offset in range(0, len(source), chunk_size):
                statements.extend(parser.feed(source[offset : offset + chunk_size]))
            statements.extend(parser.finish())
        assert [statement.source for statement in statements] == [source]
        return len(source), work["normalized"]

    _, small_work = measured(1024)
    length, large_work = measured(4096)
    # After the line's first token is settled, appending ordinary characters
    # cannot produce the real logical NEWLINE required for a new statement.
    assert large_work <= small_work * 5
    assert large_work < length * 4


@pytest.mark.parametrize("clause", ["else", "elif", "except", "finally"])
def test_unterminated_clause_name_still_probes_new_non_newline_characters(clause):
    parser = Parser()
    source = "if condition: first()\n"
    assert parser.feed(source) == ()
    assert parser.feed(clause) == ()
    # The apparent clause name can still become an ordinary identifier. A
    # no-newline fast path must not hide this newly certain top-level boundary.
    assert [statement.source for statement in parser.feed("where")] == [source]
    assert parser.pending == clause + "where"


def test_no_newline_fast_path_still_resolves_a_previously_buffered_cr():
    parser = Parser()
    assert parser.feed("first()\r") == ()
    assert [statement.source for statement in parser.feed("next(")] == ["first()\r"]
    assert parser.pending == "next("


def test_indented_long_line_does_not_flush_its_parent():
    parser = Parser()
    prefix = "if condition:\n    value = '"
    assert parser.feed(prefix) == ()
    for _ in range(100):
        assert parser.feed("a" * 16) == ()
    assert parser.feed("'\n") == ()
    source = prefix + "a" * 1600 + "'\n"
    assert [statement.source for statement in parser.feed("next(")] == [source]


@pytest.mark.parametrize("inspect_pending", [False, True])
@pytest.mark.parametrize(
    "opening,middle,closing",
    [
        ("value = '\u4f60", "\u597d" * 100, "'\n"),
        ('consume("', "a" * 100, '")\r\n'),
        ("value = [", "1," * 100, "]\rnext()\n"),
        ("if condition:\n    value = '", "a" * 100, "'\nnext()\n"),
    ],
)
def test_buffered_fragments_preserve_source_with_optional_diagnostic_reads(opening, middle, closing, inspect_pending):
    parser = Parser()
    units = list(parser.feed(opening))
    accepted = opening
    for character in middle:
        units.extend(parser.feed(character))
        accepted += character
        if inspect_pending:
            assert "".join(unit.source for unit in units) + parser.pending == accepted
    units.extend(parser.feed(closing))
    units.extend(parser.finish())
    assert "".join(unit.source for unit in units) == opening + middle + closing
    assert parser.pending == ""


def test_finish_materializes_buffered_invalid_tail_without_losing_it():
    parser = Parser()
    assert parser.feed("value = '") == ()
    for _ in range(100):
        assert parser.feed("abc") == ()
    with pytest.raises(SyntaxError):
        parser.finish()
    assert parser.closed
    assert parser.pending == "value = '" + "abc" * 100
