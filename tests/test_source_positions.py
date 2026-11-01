"""Logical-line prefixes belong to the statement whose positions they establish."""

import ast

import pytest

from partial_python import Executor, Parser

PREFIX = "if True:\n    pass\n"
ROOT_PREFIXES = ["\f", " \t\f", "\\\n", "\\\n\\\n", "\\\n\f", "\\\n \t\f"]


def positioned_nodes(source, start_line=1):
    tree = ast.parse(source)
    ast.increment_lineno(tree, start_line - 1)
    return [ast.dump(node, include_attributes=True) for node in tree.body]


@pytest.mark.parametrize("leading", ROOT_PREFIXES)
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("tail,ending", [("next(", ")\n"), ('"""open\n', '"""\n'), ("1", "e3\n")])
def test_successor_keeps_its_logical_line_prefix_and_releases_prior_compound(leading, newline, tail, ending):
    prefix, leading, tail, ending = (value.replace("\n", newline) for value in (PREFIX, leading, tail, ending))
    source = prefix + leading + tail + ending
    expected = positioned_nodes(source)
    for chunks in ([prefix + leading + tail], [prefix, leading, tail], list(prefix + leading + tail)):
        parser = Parser()
        units = []
        for chunk in chunks:
            units.extend(parser.feed(chunk))
        assert [unit.source for unit in units] == [prefix]
        assert parser.pending == leading + tail and parser.committed == len(prefix)
        units.extend(parser.feed(ending))
        units.extend(parser.finish())
        assert [unit.source for unit in units] == [prefix, leading + tail + ending]
        assert [node for unit in units for node in positioned_nodes(unit.source, unit.start_line)] == expected


@pytest.mark.parametrize("leading", ["    \\\n", "\\\n    ", "\f    "])
@pytest.mark.parametrize("body", ["next()\n", "1e3\n", '"""body\n"""\n', 'f"""body\n"""\n'])
def test_indentation_on_first_physical_line_keeps_a_joined_successor_inside_the_suite(leading, body):
    source = PREFIX + leading + body
    parser = Parser()
    for character in source:
        assert parser.feed(character) == ()
    assert [unit.source for unit in parser.finish()] == [source]
    assert len(ast.parse(source).body[0].body) == 2


@pytest.mark.parametrize("leading", ["\f", " \t\f", "\\\n", "\\\n\f"])
def test_logical_line_prefixes_do_not_disambiguate_continuation_clause_names(leading):
    parser = Parser()
    source = PREFIX + leading + "else:\n    pass\n"
    for character in source:
        assert parser.feed(character) == ()
    assert [unit.source for unit in parser.finish()] == [source]
    assert len(ast.parse(source).body[0].orelse) == 1


@pytest.mark.parametrize("leading", ROOT_PREFIXES)
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
async def test_compiler_error_columns_retain_the_original_successor_prefix(leading, newline):
    prefix, leading = (value.replace("\n", newline) for value in (PREFIX, leading))
    tail = leading + "return 42" + newline
    source = prefix + tail
    with pytest.raises(SyntaxError) as native:
        compile(source, "original.py", "exec", dont_inherit=True)
    executor = Executor(filename="original.py")
    with pytest.raises(SyntaxError) as actual:
        await executor.feed(source)
        await executor.finish()
    assert executor.failed_unit.source == tail
    assert (actual.value.lineno, actual.value.offset, actual.value.end_lineno, actual.value.end_offset) == (native.value.lineno, native.value.offset, native.value.end_lineno, native.value.end_offset)
    assert actual.value.text.endswith("return 42" + newline)
