"""Bound avoidable rescans without promising linear streaming of a giant token."""

import pytest

import partial_python.parser as parser_module
from partial_python import Parser

REGIONS = [
    ("value = [\n", "    123,\n", "]\n"),
    ("value = '''\n", "a line of literal text\n", "'''\n"),
    ('value = f"""\n', "a line of literal text\n", '{name}"""\n'),
]


def measured_parse(monkeypatch, opening, line, closing, count, *, bulk):
    source = opening + line * count + closing
    normalized = 0
    original_normalize = parser_module._normalize

    def normalize(text):
        nonlocal normalized
        normalized += len(text)
        return original_normalize(text)

    with monkeypatch.context() as patch:
        patch.setattr(parser_module, "_normalize", normalize)
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
    return len(source), normalized


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
    original_normalize = parser_module._normalize

    def measured(size):
        source = opening + ("1," * size if "[" in opening else "a" * size) + closing
        work = 0

        def normalize(text):
            nonlocal work
            work += len(text)
            return original_normalize(text)

        with monkeypatch.context() as patch:
            patch.setattr(parser_module, "_normalize", normalize)
            parser = Parser()
            statements = []
            for offset in range(0, len(source), chunk_size):
                statements.extend(parser.feed(source[offset : offset + chunk_size]))
            statements.extend(parser.finish())
        assert [statement.source for statement in statements] == [source]
        return len(source), work

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
