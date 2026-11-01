"""Boundary tests assert source stability, never just parseability at temporary EOF."""

import ast
from itertools import pairwise

import pytest

from partial_python import Parser

PROGRAMS = [
    "await foo(1)\nawait bar(2)\nawait baz(3)",
    "x = 1\ny = 2\n",
    "f(); g(); h()\n",
    "if condition:\n    first()\n    second()\nelif other:\n    third()\nelse:\n    fourth()\nlast()\n",
    "for x in xs:\n    first(x)\n    second(x)\nelse:\n    exhausted()\nlast()\n",
    "while condition:\n    first()\nelse:\n    done()\nlast()\n",
    "try:\n    first()\nexcept ValueError:\n    second()\nexcept TypeError:\n    third()\nelse:\n    fourth()\nfinally:\n    cleanup()\nlast()\n",
    "try:\n    first()\nfinally:\n    cleanup()\nlast()\n",
    "try:\n    first()\nexcept* ValueError:\n    second()\nfinally:\n    cleanup()\nlast()\n",
    "with first() as x, second() as y:\n    use(x, y)\nlast()\n",
    "async with first() as x:\n    await use(x)\nlast()\n",
    "async for x in xs:\n    await use(x)\nelse:\n    exhausted()\nlast()\n",
    "@first\n@second(x)\ndef f():\n    return 1\nf()\n",
    "@decorate\nasync def f():\n    await call()\nawait f()\n",
    "@decorate\nclass C:\n    def f(self):\n        return 1\nC()\n",
    "match value:\n    case [x, *xs] if x:\n        first()\n    case _:\n        second()\nlast()\n",
    "if outer:\n    if inner:\n        first()\n    else:\n        second()\nelse:\n    third()\nlast()\n",
    "if condition: first(); second()\nelse: third()\nlast()\n",
    "for x in xs: use(x)\nelse: exhausted()\nlast()\n",
    "value = (\n    first()\n    + second()\n)\nlast()\n",
    "value = first() \\\n    + second()\nlast()\n",
    "value = '''first\nsecond\nthird'''\nlast()\n",
    'value = f"hello {name!r:>10}"\nlast()\n',
    'value = rf"hello {name}"\nlast()\n',
    "value = 'first' 'second'\nlast()\n",
    "# lead\n\nfirst() # trailing\n# middle\nsecond()\n# tail",
    "if condition:\n\tfirst()\nlast()\n",
    "if condition:\n    first()\n\fsecond()\n",
    "变量 = 'é🙂'\n另一个 = 变量\n",
    "first()\r\nsecond()\r\n",
    "first()\rsecond()\r",
    "if condition:\r\n    first()\r\nelse:\r\n    second()\r\nlast()\r\n",
    "if condition:\r    first()\relse:\r    second()\rlast()\r",
    "first()\r\nsecond()\rthird()\n",
    "# comment only\n",
    "\n\n",
    "",
]


def parse_chunks(chunks):
    parser = Parser()
    emitted = []
    for chunk in chunks:
        emitted.extend(parser.feed(chunk))
    emitted.extend(parser.finish())
    return emitted


def ast_body(source):
    return [ast.dump(node, include_attributes=False) for node in ast.parse(source).body]


@pytest.mark.parametrize("source", PROGRAMS)
def test_every_two_chunk_split_preserves_statements(source):
    expected = parse_chunks([source])
    for split in range(len(source) + 1):
        actual = parse_chunks([source[:split], source[split:]])
        assert actual == expected, f"partition at {split}: {source!r}"


@pytest.mark.parametrize("source", PROGRAMS)
def test_character_stream_preserves_source_and_ast(source):
    statements = parse_chunks(source)
    assert "".join(statement.source for statement in statements) == source
    assert [node for statement in statements for node in ast_body(statement.source)] == ast_body(source)
    for previous, current in pairwise(statements):
        assert previous.end == current.start
        assert previous.end_line == current.start_line
    if statements:
        assert statements[0].start == 0
        assert statements[-1].end == len(source)


@pytest.mark.parametrize("source", ["first()", "first() # comment", "value = 'closed'", "value = 12", "first(); second()"])
def test_simple_statement_needs_real_logical_newline(source):
    parser = Parser()
    assert parser.feed(source) == ()
    assert parser.pending == source
    assert [s.source for s in parser.feed("\n")] == [source + "\n"]


@pytest.mark.parametrize(
    "source",
    [
        "for x in xs:\n    first()\n",
        "if condition:\n    first()\n",
        "while condition:\n    first()\n",
        "with manager:\n    first()\n",
        "def first():\n    pass\n",
        "class C:\n    pass\n",
        "match value:\n    case _:\n        first()\n",
        "try:\n    first()\nexcept Exception:\n    second()\n",
        "if condition: first()\n",
        "for x in xs: first()\n",
    ],
)
def test_compound_waits_through_blank_lines_and_comments(source):
    parser = Parser()
    assert parser.feed(source) == ()
    assert parser.feed("\n# comment\n    # another\n") == ()
    statements = parser.feed("n")  # No continuation clause can begin with n.
    assert parser.feed("ext(") == ()
    assert len(statements) == 1
    assert statements[0].source == source + "\n# comment\n    # another\n"
    assert parser.pending == "next("


@pytest.mark.parametrize(
    "prefix, clause",
    [
        ("if condition:\n    first()\n", "elif"),
        ("if condition:\n    first()\n", "else"),
        ("for x in xs:\n    first()\n", "else"),
        ("while condition:\n    first()\n", "else"),
        ("try:\n    first()\nexcept ValueError:\n    second()\n", "except"),
        ("try:\n    first()\nexcept ValueError:\n    second()\n", "else"),
        ("try:\n    first()\nexcept ValueError:\n    second()\n", "finally"),
    ],
)
def test_partial_continuation_keywords_never_seal_previous_compound(prefix, clause):
    parser = Parser()
    assert parser.feed(prefix) == ()
    for character in clause:
        assert parser.feed(character) == ()
    assert parser.feed(" ") == ()


@pytest.mark.parametrize("source", ["@decorate\n", "@decorate(\n", "@first\n@second\n", "@decorate\ndef f():\n    pass\n"])
def test_decorators_remain_attached_to_definition(source):
    parser = Parser()
    assert parser.feed(source) == ()


@pytest.mark.parametrize("source", ["call(\n", "value = 'open", "value = '''open\n", "value = 1 \\\n", "if condition:\n", "try:\n    first()\n", "value =\n", "value = \0\n"])
def test_invalid_or_incomplete_eof_never_repairs_source(source):
    parser = Parser()
    assert parser.feed(source) == ()
    with pytest.raises((SyntaxError, ValueError)):
        parser.finish()
    assert parser.closed
    with pytest.raises(RuntimeError):
        parser.feed("later()\n")


def test_finish_explicitly_seals_compound_without_dedent():
    source = "for x in xs:\n    first(x)\n    second(x)"
    parser = Parser()
    assert parser.feed(source) == ()
    assert [s.source for s in parser.finish()] == [source]
    assert parser.closed
    assert parser.pending == ""


def test_invalid_suffix_does_not_erase_sealed_prefix():
    parser = Parser()
    assert [s.source for s in parser.feed("first()\nvalue = (\n")] == ["first()\n"]
    assert parser.pending == "value = (\n"
    with pytest.raises(SyntaxError):
        parser.finish()
    assert parser.committed == len("first()\n")


@pytest.mark.parametrize("separator", ["\n", "\r\n", "\r"])
def test_offsets_count_characters_and_physical_lines(separator):
    source = f"变量 = '🙂'{separator}另一个 = 变量{separator}"
    first, second = parse_chunks([source])
    assert first.start == 0
    assert first.end == len(f"变量 = '🙂'{separator}")
    assert (first.start_line, first.end_line) == (1, 2)
    assert (second.start_line, second.end_line) == (2, 3)
    assert source[first.start : first.end] == first.source
    assert source[second.start : second.end] == second.source


def test_lifecycle_and_input_type():
    parser = Parser()
    with pytest.raises(TypeError):
        parser.feed(b"first()\n")
    assert parser.feed("") == ()
    assert parser.finish() == ()
    with pytest.raises(RuntimeError):
        parser.finish()
    with pytest.raises(RuntimeError):
        parser.feed("")
