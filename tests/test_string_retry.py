"""Retry gates may defer work only while a real string closer is still missing."""

import ast
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import partial_python.parser as parser_module
from partial_python import Parser

TSTRINGS = pytest.mark.skipif(sys.version_info < (3, 14), reason="template strings require Python 3.14")
PEP701 = pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 701 requires Python 3.12")
PREFIXES = ["", "r", "R", "b", "u", "br", "Rb", "bR", "rb", "f", "F", "fr", "rf", pytest.param("t", marks=TSTRINGS), pytest.param("rt", marks=TSTRINGS)]


def ast_nodes(source):
    module = compile(source, "<string-retry>", "exec", flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT, dont_inherit=True)
    return [ast.dump(node, include_attributes=False) for node in module.body]


def check_stream(source, chunks):
    expected = ast_nodes(source)
    parser = Parser()
    units = []
    actual = []
    received = committed = 0
    line = 1

    def inspect(batch):
        nonlocal committed, line
        for unit in batch:
            assert unit.start == committed
            assert unit.end == unit.start + len(unit.source) <= received
            assert unit.source == source[unit.start : unit.end]
            assert unit.start_line == line
            line += unit.source.count("\n") + unit.source.count("\r") - unit.source.count("\r\n")
            assert unit.end_line == line
            committed = unit.end
            actual.extend(ast_nodes(unit.source))
            assert actual == expected[: len(actual)]
            units.append(unit)

    for chunk in chunks:
        received += len(chunk)
        inspect(parser.feed(chunk))
        assert parser.pending == source[committed:received]
    inspect(parser.finish())
    assert committed == len(source)
    assert actual == expected
    return units


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("quote", ["'", '"'])
def test_complete_empty_string_must_not_be_reinterpreted_by_a_probe(prefix, quote):
    parser = Parser()
    start = "value = [" + prefix + quote * 2
    assert parser.feed(start) == ()
    # Four synthetic quotes could turn the existing empty string into an empty
    # triple string. Original emitted tokens forbid using that as gate evidence.
    assert [unit.source for unit in parser.feed("]\n")] == [start + "]\n"]


@pytest.mark.parametrize("prefix", ["r", "b", "u", "f", "t", "fr", "rb"])
def test_name_at_temporary_eof_does_not_require_a_future_quote(prefix):
    parser = Parser()
    start = "value = [" + prefix
    assert parser.feed(start) == ()
    assert [unit.source for unit in parser.feed("]\n")] == [start + "]\n"]


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("quote", ["'", '"'])
def test_real_third_quote_can_change_an_empty_token_into_an_open_triple_string(prefix, quote):
    parser = Parser()
    start = "value = [" + prefix + quote * 2
    assert parser.feed(start) == ()
    assert parser.feed(quote + "first line\n") == ()
    assert parser.feed("another line\n") == ()
    source = start + quote + "first line\nanother line\n" + quote * 3 + "]\n"
    assert [unit.source for unit in parser.feed(quote * 3 + "]\n")] == [source]
    ast_nodes(source)


@pytest.mark.parametrize("prefix", ["", "r", "b", "br"])
@pytest.mark.parametrize("quote", ["'", '"'])
def test_escaped_closer_is_only_a_retry_hint(prefix, quote):
    parser = Parser()
    opening = "value = " + prefix + quote * 3 + "first\n"
    decoy = "\\" + quote * 3 + " still inside\n"
    assert parser.feed(opening) == ()
    assert parser.feed(decoy) == ()
    assert parser.feed("last line\n") == ()
    source = opening + decoy + "last line\n" + quote * 3 + "\n"
    assert [unit.source for unit in parser.feed(quote * 3 + "\n")] == [source]
    ast_nodes(source)


@pytest.mark.parametrize("prefix", ["", "r", "b", "br"])
@pytest.mark.parametrize("quote", ["'", '"'])
def test_four_quotes_close_after_an_odd_trailing_backslash(prefix, quote):
    parser = Parser()
    opening = "value = " + prefix + quote * 3 + "first\nlast\\"
    assert parser.feed(opening) == ()
    assert parser.feed(quote * 3) == ()
    source = opening + quote * 4 + "\n"
    assert [unit.source for unit in parser.feed(quote + "\n")] == [source]
    ast_nodes(source)


@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_closer_split_at_every_boundary_preserves_unicode_and_physical_lines(quote, newline):
    source = "变量 = " + quote * 3 + "🙂" + newline + "é" + newline + quote * 3 + newline + "next()" + newline
    expected = check_stream(source, [source])
    assert check_stream(source, source) == expected
    for split in range(len(source) + 1):
        assert check_stream(source, [source[:split], source[split:]]) == expected


def test_host_proven_standalone_triple_opener_releases_prior_compound():
    parser = Parser()
    compound = "if condition: first()\n"
    assert parser.feed(compound) == ()
    assert [unit.source for unit in parser.feed("'''first\n")] == [compound]
    assert parser.feed("last\n") == ()
    literal = "'''first\nlast\n''' "
    assert parser.feed("''' ") == ()
    assert parser.pending == literal
    assert [unit.source for unit in parser.feed("\n")] == [literal + "\n"]


@pytest.mark.parametrize("prefix", ["", "r", "b", "br"])
def test_host_proven_indented_triple_opener_keeps_parent_buffered(prefix):
    parser = Parser()
    opening = "if condition:\n    " + prefix + "'''first\n"
    assert parser.feed(opening) == ()
    assert parser.feed("last\n") == ()
    assert parser.feed("'''\n") == ()
    assert parser.feed("    another()\n") == ()
    source = opening + "last\n'''\n    another()\n"
    assert [unit.source for unit in parser.feed("next(")] == [source]
    assert parser.pending == "next("


@pytest.mark.parametrize("prefix", [pytest.param("f", marks=PEP701), pytest.param("t", marks=TSTRINGS)])
def test_real_interpolated_string_start_seals_predecessor_before_its_body(prefix):
    parser = Parser()
    compound = "if condition: first()\n"
    assert parser.feed(compound) == ()
    assert [unit.source for unit in parser.feed(prefix + '"""')] == [compound]
    assert parser.feed("body\n" * 100) == ()
    literal = prefix + '"""' + "body\n" * 100 + '"""\n'
    assert [unit.source for unit in parser.feed('"""\n')] == [literal]


INTERPOLATED = [
    pytest.param(['value = f"""', "{", '"""inner"""', "}", "\nouter\n", '"""\n'], marks=PEP701, id="nested-plain-triple-same-quotes"),
    ['value = f"""', "{f'''", "{name}", "'''}", "\nouter\n", '"""\n'],
    pytest.param(['value = f"', "{(\n", 'mapping["key"]', "\n)}", '"\n'], marks=PEP701, id="multiline-single-quoted-fstring"),
    pytest.param(['value = f"""', "{name # fake } and quotes '\"\n", "}", "\nouter\n", '"""\n'], marks=PEP701, id="comment-quotes-inside-field"),
    pytest.param(['value = t"""', "{f'''", "{name}", "'''}", "\nouter\n", '"""\n'], marks=TSTRINGS, id="nested-fstring-in-template"),
    pytest.param(['value = f"""', "{t'''", "{name}", "'''}", "\nouter\n", '"""\n'], marks=TSTRINGS, id="nested-template-in-fstring"),
]


@pytest.mark.parametrize("chunks", INTERPOLATED)
def test_nested_interpolation_and_candidate_closers_are_always_retokenized(chunks):
    source = "".join(chunks)
    expected = check_stream(source, [source])
    assert check_stream(source, chunks) == expected
    assert check_stream(source, source) == expected


@pytest.mark.parametrize("source", ["value = '''first\nlast\n", "value = r'''first\nlast\n", 'value = f"""first\n{name}\n'])
def test_finish_validates_original_unclosed_string_despite_retry_gate(source):
    parser = Parser(filename="original.py")
    for line in source.splitlines(keepends=True):
        assert parser.feed(line) == ()
    with pytest.raises(SyntaxError) as expected:
        compile(source, "original.py", "exec", flags=ast.PyCF_ONLY_AST, dont_inherit=True)
    with pytest.raises(SyntaxError) as actual:
        parser.finish()
    assert actual.value.args == expected.value.args
    assert parser.pending == source


@pytest.mark.parametrize("prefix", ["", "r", "b", "f", pytest.param("t", marks=TSTRINGS)])
@pytest.mark.parametrize("quote", ["'", '"'])
def test_multiline_prose_with_nonclosing_quotes_has_linear_scanner_work(monkeypatch, prefix, quote):
    original_normalize = parser_module._normalize
    original_tokens = parser_module.tokenize.generate_tokens

    def measure(count):
        opening = "value = " + prefix + quote * 3 + "\n"
        line = "A line with 'single' and \"double\" quotes, [brackets], and plain text.\n"
        source = opening + line * count + quote * 3 + "\n"
        work = {"normalized": 0, "tokenized": 0}

        def normalize(text):
            work["normalized"] += len(text)
            return original_normalize(text)

        def tokens(readline):
            def read():
                text = readline()
                work["tokenized"] += len(text)
                return text

            return original_tokens(read)

        with monkeypatch.context() as patch:
            patch.setattr(parser_module, "_normalize", normalize)
            patch.setattr(parser_module.tokenize, "generate_tokens", tokens)
            parser = Parser()
            assert parser.feed(opening) == ()
            for _ in range(count):
                assert parser.feed(line) == ()
            assert [unit.source for unit in parser.feed(quote * 3 + "\n")] == [source]
        return len(source), work

    _, small = measure(64)
    length, large = measure(256)
    for operation in small:
        assert large[operation] <= small[operation] * 5
        assert large[operation] < length * 8


def differential_sources():
    sources = [
        "value = '''first\nlast\n'''\nnext()\n",
        "value = r'''first\nlast\\''''\nnext()\n",
        'value = b"""first\nlast\n"""\nnext()\n',
        "items = [\n'''one\ntwo''']\r\nnext = 1\n",
        "if condition: first()\n'''one\ntwo''' \nnext()\n",
        "value = [r'']\nnext()\n",
        "value = [r'''first\nlast\n''']\nnext()\n",
        "变量 = '''🙂\ré\r'''\rnext()\r",
        "if condition:\n    value = '''first\nlast\n'''\n    another()\nnext()\n",
    ]
    for parameter in INTERPOLATED:
        chunks = parameter.values[0] if hasattr(parameter, "values") else parameter
        source = "".join(chunks)
        try:
            ast_nodes(source)
        except SyntaxError:
            continue  # The same fixtures already have explicit host-version guards above.
        sources.append(source)
    return sources


def assert_gate_matches_unskipped_scanner(chunks, read_pending):
    parser = Parser()
    reference = Parser()
    for index, chunk in enumerate(chunks):
        # Disable only the quote retry gate. All real parsing and boundary
        # logic stays identical, so a mismatch identifies changed latency or
        # source handling caused by skipped scans rather than another backend.
        reference._await_quote = ""
        assert parser.feed(chunk) == reference.feed(chunk), f"chunk {index}: {chunk!r}"
        assert parser.committed == reference.committed
        assert parser.closed == reference.closed
        if read_pending:
            assert parser.pending == reference.pending
    assert parser.finish() == reference.finish()
    assert parser.committed == reference.committed
    assert parser.pending == reference.pending == ""


@pytest.mark.parametrize("read_pending", [False, True])
@given(source=st.sampled_from(differential_sources()), sizes=st.lists(st.integers(min_value=0, max_value=24), min_size=0, max_size=100))
@settings(max_examples=120, deadline=None)
def test_retry_gate_preserves_every_emission_of_unskipped_scanner(source, sizes, read_pending):
    chunks = []
    offset = 0
    for size in sizes:
        chunks.append(source[offset : offset + size])
        offset += size
        if offset >= len(source):
            break
    chunks.append(source[offset:])
    assert_gate_matches_unskipped_scanner(chunks, read_pending)


@pytest.mark.parametrize("read_pending", [False, True])
def test_matching_quote_then_buffered_bracket_and_split_crlf_matches_unskipped_scanner(read_pending):
    chunks = ["items = [\n'''one\n", "two'''", "]", "\r", "\nnext = 1\n"]
    assert_gate_matches_unskipped_scanner(chunks, read_pending)
