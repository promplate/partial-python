"""Generated-language engineering checks; source is compiled, never executed.

Set PARTIAL_PYTHON_GRAMMAR_EXAMPLES to increase the explicit stress-run budget.
"""

import ast
import os
import sys
from itertools import pairwise

from hypothesis import given, settings
from hypothesis import strategies as st

from partial_python import Parser

EXAMPLES = int(os.environ.get("PARTIAL_PYTHON_GRAMMAR_EXAMPLES", "150"))
if EXAMPLES < 1:
    raise ValueError("PARTIAL_PYTHON_GRAMMAR_EXAMPLES must be positive")

SIMPLE = [
    "value = 1\n",
    "e = 1\n",
    "elsewhere = 2\n",
    "finally_value = 3\n",
    "excepted()\n",
    "first(); second()\n",
    "pass\n",
    "await call(1)\n",
    "变量 = '🙂é'\n",
    "value = (\n    1\n    + 2\n)\n",
    "value = 1 \\\n+ 2\n",
    "value = [\n    {'key': (1,\n             2)},\n]\n",
    "value = [\n    1,\n] \\\n+ other\n",
    "text = '''first\nline with ' and \" and [ ]\nlast'''\n",
    "text = r'''first\nraw \\ path and [ ]\nlast'''\n",
    "text = b'''first\nASCII words and [ ]\nlast'''\n",
    "'''standalone\nsecond line'''\n",
    "text = 'one\\\ntwo'\n",
    'text = f"{name!r:>{width}}"\n',
    'text = f"""first\n{(\n    value\n)}\nlast"""\n',
    "def identity(value):\n    return value\n",
    "@decorate\nclass Box:\n    def value(self):\n        return 1\n",
]
if sys.version_info >= (3, 12):
    SIMPLE += [
        'text = f"{mapping["key"]}"\n',
        'text = f"""{value  # fake } ]\n}"""\n',
        "async def identity[T](value: T) -> T:\n    return value\n",
        "type Vector[T] = list[T]\n",
    ]
if sys.version_info >= (3, 14):
    SIMPLE += ['text = t"""first\n{name}\nlast"""\n', "text = t\"{f'{name}'}\"\n"]

TRIVIA = ["", "\n", "# note: [] ''' \\\n", "    # indented comment\n\n", "\t# tabbed comment\n"]


def indent(source):
    # Only LF is a physical newline at construction time. str.splitlines would
    # also treat formfeeds or Unicode separators as source line boundaries.
    return "\n".join("    " + line if line else "" for line in source.split("\n"))


@st.composite
def compound(draw, children):
    kind = draw(st.sampled_from(["if", "for", "while", "with", "async with", "async for", "try", "try-finally", "except*", "match", "function"]))
    body = draw(children)
    body = draw(st.sampled_from(TRIVIA)) + body
    if kind == "if":
        source = "if condition:\n" + indent(body)
        if draw(st.booleans()):
            source += "elif other:\n" + indent(draw(children))
        if draw(st.booleans()):
            source += "else:\n" + indent(draw(children))
        return source
    if kind in {"for", "while", "async for"}:
        header = "while condition" if kind == "while" else kind + " item in items"
        source = header + ":\n" + indent(body)
        if draw(st.booleans()):
            source += "else:\n" + indent(draw(children))
        return source
    if kind in {"with", "async with"}:
        return kind + " manager() as value:\n" + indent(body)
    if kind == "try-finally":
        return "try:\n" + indent(body) + "finally:\n" + indent(draw(children))
    if kind in {"try", "except*"}:
        handler = "except*" if kind == "except*" else "except"
        source = "try:\n" + indent(body) + handler + " ValueError:\n" + indent(draw(children))
        if draw(st.booleans()):
            source += handler + " TypeError:\n" + indent(draw(children))
        if draw(st.booleans()):
            source += "else:\n" + indent(draw(children))
        if draw(st.booleans()):
            source += "finally:\n" + indent(draw(children))
        return source
    if kind == "match":
        return "match value:\n    case {'key': item}:\n" + indent(indent(body)) + "    case _:\n" + indent(indent(draw(children)))
    decorators = draw(st.sampled_from(["", "@decorate\n", "@decorate\n@configured(option)\n"]))
    # An async function can contain every generated child, including top-level
    # await/async-for forms. Fixed regular functions/classes above own their body.
    return decorators + "async def generated():\n" + indent(body)


STATEMENT = st.recursive(st.sampled_from(SIMPLE), lambda children: compound(children), max_leaves=12)


@st.composite
def programs(draw):
    statements = draw(st.lists(STATEMENT, min_size=1, max_size=4))
    source = "".join(draw(st.sampled_from(TRIVIA)) + draw(st.sampled_from(["", "", "\f"])) + statement for statement in statements)
    source += draw(st.sampled_from(TRIVIA))
    if draw(st.booleans()):
        lines = source.split("\n")
        for index, line in enumerate(lines):
            spaces = len(line) - len(line.lstrip(" "))
            lines[index] = "\t" * (spaces // 4) + line[spaces // 4 * 4 :]
        source = "\n".join(lines)
    if draw(st.booleans()):
        source = source.removesuffix("\n")
    return source.replace("\n", draw(st.sampled_from(["\n", "\r\n", "\r"])))


def ast_nodes(source):
    module = compile(source, "<generated-grammar>", "exec", flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT, dont_inherit=True)
    return [ast.dump(node, include_attributes=False) for node in module.body]


def inspect_partition(source, expected, chunks):
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
        assert parser.committed == committed
        assert parser.pending == source[committed:received]
    inspect(parser.finish())
    assert committed == len(source)
    assert parser.pending == ""
    assert parser.closed
    assert actual == expected
    return units


@given(source=programs(), data=st.data())
@settings(max_examples=EXAMPLES, deadline=None)
def test_generated_valid_grammar_preserves_source_ast_prefixes_and_unit_partitions(source, data):
    # Full compilation also checks scope restrictions in generated combinations.
    # Failure here is a generator defect, not a reason to discard the example.
    compile(source, "<generated-grammar>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT, dont_inherit=True)
    expected = ast_nodes(source)
    bulk = inspect_partition(source, expected, [source])
    cuts = sorted([0, *data.draw(st.lists(st.integers(min_value=0, max_value=len(source)), max_size=60), label="transport cuts"), len(source)])
    chunks = [source[start:end] for start, end in pairwise(cuts)]
    assert inspect_partition(source, expected, chunks) == bulk
    assert inspect_partition(source, expected, source) == bulk
