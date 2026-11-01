"""Conformance checks across transport partitions and real stdlib source files."""

import ast
import importlib.util
import random
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from partial_python import Parser

SMALL_PROGRAMS = [
    "await first(1)\nawait second(2)\nawait third(3)",
    "if x:\n    first()\nelif y:\n    second()\nelse:\n    third()\nlast()\n",
    "try:\n    first()\nexcept* ValueError:\n    second()\nfinally:\n    third()\nlast()\n",
    "@decorator(\n    value\n)\nasync def f():\n    await first()\nawait f()\n",
    "match value:\n    case {'name': name}:\n        first(name)\n    case _:\n        second()\nlast()\n",
    "x = f'{first()} {second():>10}'\ny = '''one\ntwo\nthree'''\n",
    "# leading\r\n变量 = '🙂'\r\n# middle\rnext(变量)\n# tail",
    "for x in xs:\n    if x:\n        first()\n    else:\n        second()\nelse:\n    third()\nlast()\n",
]

CHECKPOINT_PROGRAMS = [
    "if ready:\n    pass\n \fπ = '🙂'; value = π\n",
    "if ready:\n    pass\n\\\n\fvalue = 1\n",
    "if condition:\n\tfirst()\n\tif other:\n\t\tsecond()\n\tthird()\nlast()\n",
    "if condition:\n    first()\n\n # comment\n\t# another\n    second()\nlast()\n",
    "if condition:\n    first()\n\felse:\n    second()\nlast()\n",
    "if condition:\n    \\\nfirst()\nlast()\n",
    "if condition:\n    first()\n\\\nlast()\n",
    "if condition:\n    first(\n0,\n# comment\n1\n    )\n    second()\nlast()\n",
    "if condition:\n    text = '''first\nelse:\nlast()\n'''\n    second()\nlast()\n",
    'if condition:\n    text = f"""first\n{(\n    1 + 2\n)}\nelse:\n"""\n    second()\nlast()\n',
    "if condition:\n    first()\nelse:\n    second()\n@decorate(\nargument\n)\ndef f():\n    pass\nlast()\n",
    "if condition:\r\n\tfirst()\r\n\tif other:\r\n\t\tsecond()\r\n\tthird()\r\nlast()\r\n",
    "if condition:\r    first()\r\felse:\r    second()\rlast()\r",
]


def bodies(source, start_line=1):
    tree = ast.parse(source)
    ast.increment_lineno(tree, start_line - 1)
    return [ast.dump(node, include_attributes=True) for node in tree.body]


def assert_prefixes(source, chunks):
    parser = Parser()
    expected = bodies(source)
    emitted = []
    nodes = []
    received = ""
    for chunk in chunks:
        received += chunk
        for statement in parser.feed(chunk):
            emitted.append(statement)
            nodes.extend(bodies(statement.source, statement.start_line))
        committed = "".join(statement.source for statement in emitted)
        assert received == committed + parser.pending
        assert nodes == expected[: len(nodes)]
        assert parser.committed == len(committed)
    emitted.extend(parser.finish())
    assert "".join(statement.source for statement in emitted) == source
    assert [node for statement in emitted for node in bodies(statement.source, statement.start_line)] == expected
    return emitted


@given(source=st.sampled_from(SMALL_PROGRAMS), sizes=st.lists(st.integers(min_value=0, max_value=20), min_size=0, max_size=70))
@settings(max_examples=120, deadline=None)
def test_arbitrary_transport_partitions_preserve_every_emitted_ast_prefix(source, sizes):
    chunks = []
    offset = 0
    for size in sizes:
        chunks.append(source[offset : offset + size])
        offset += size
        if offset >= len(source):
            break
    chunks.append(source[offset:])
    assert assert_prefixes(source, chunks) == assert_prefixes(source, [source])


@pytest.mark.parametrize("source", CHECKPOINT_PROGRAMS)
def test_logical_line_checkpoints_preserve_indentation_and_continuations(source):
    assert assert_prefixes(source, source) == assert_prefixes(source, [source])
    for split in range(len(source) + 1):
        assert assert_prefixes(source, [source[:split], source[split:]]) == assert_prefixes(source, [source])


@pytest.mark.parametrize("module", ["abc", "keyword", "stat"])
def test_standard_library_sources_preserve_ast_under_random_partitions(module):
    spec = importlib.util.find_spec(module)
    path = Path(sys.base_prefix) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / f"{module}.py"
    if spec and spec.origin and spec.origin not in {"frozen", "built-in"}:
        path = Path(spec.origin)
    if not path.is_file():
        pytest.skip(f"stdlib source unavailable for {module}")
    source = path.read_text(encoding="utf-8")
    rng = random.Random(17429)
    chunks = []
    offset = 0
    while offset < len(source):
        size = rng.randint(1, 512)
        chunks.append(source[offset : offset + size])
        offset += size
    assert assert_prefixes(source, chunks) == assert_prefixes(source, [source])


def test_parser_persists_future_grammar_across_units():
    parser = Parser()
    directive = "from __future__ import barry_as_FLUFL\n"
    comparison = "different = 1 <> 2\n"
    assert [statement.source for statement in parser.feed(directive)] == [directive]
    assert [statement.source for statement in parser.feed(comparison)] == [comparison]
    assert parser.finish() == ()


def test_future_grammar_is_independent_of_transport_partition():
    directive = "from __future__ import barry_as_FLUFL\n"
    comparison = "different = 1 <> 2\n"
    source = directive + comparison
    for split in range(len(source) + 1):
        parser = Parser()
        statements = [*parser.feed(source[:split]), *parser.feed(source[split:]), *parser.finish()]
        assert [statement.source for statement in statements] == [directive, comparison]


@pytest.mark.skipif(sys.version_info < (3, 12), reason="type statements require Python 3.12")
def test_type_alias_and_generic_definition_follow_host_grammar():
    source = "type Vector[T] = list[T]\ndef first[T](items: Vector[T]) -> T:\n    return items[0]\nfirst([1])\n"
    assert assert_prefixes(source, source) == assert_prefixes(source, [source])


@pytest.mark.skipif(sys.version_info < (3, 14), reason="template strings require Python 3.14")
def test_template_strings_do_not_expose_nested_newlines():
    source = 'value = t"""Hello {\n    name\n}!"""\nnext(value)\n'
    assert assert_prefixes(source, source) == assert_prefixes(source, [source])
