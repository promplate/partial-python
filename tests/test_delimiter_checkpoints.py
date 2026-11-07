"""Lexer-context replay must preserve original syntax, state, and source ranges."""

import ast
import sys

import pytest
from _assertions import assert_partition, count_work

import partial_python.parser as parser_module
from partial_python import Parser

PEP701 = pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 701 requires Python 3.12")
TSTRINGS = pytest.mark.skipif(sys.version_info < (3, 14), reason="template strings require Python 3.14")

PROGRAMS = [
    "value = (\n    {'key': [\n        (1,\n         2),\n    ]},\n)\nnext()\n",
    "value = [{(\n    1,\n    2): [3,\n         4]}]\nnext()\n",
    "if condition:\n    data = [\n        1,\n        2,\n    ]\n    another()\nelse:\n    fallback()\nnext()\n",
    "for item in items:\n    data = {\n        'key': [item,\n                item],\n    }\n    another()\nelse:\n    fallback()\nnext()\n",
    "def make():\n    data = (\n        1,\n        2,\n    )\n    return data\nnext()\n",
    "value = [\n    1,\n] \\\n+ other\nnext()\n",
    "value = [\n    # brackets ] ) } and quotes ' \" and a backslash \\\n    1,\n]\nnext()\n",
    "value = [\n    '''fake ]\nelse:\n(\n''',\n    1,\n]\nnext()\n",
    "value = [\n    r'''fake ]\nelse:\n(\n''',\n    1,\n]\nnext()\n",
    "value = [\n    b'''fake ]\nelse:\n(\n''',\n    1,\n]\nnext()\n",
    "value = [\n    'one\\\ntwo',\n    1,\n]\nnext()\n",
    'value = [\n    f"{value:{width}.{precision}f}",\n    1,\n]\nnext()\n',
    "value = [\n    f\"{f'{name}'}\",\n    1,\n]\nnext()\n",
    'value = [\n    rf"{name!r} {{fake ]}}",\n    1,\n]\nnext()\n',
    'value = [\n    f"""{(\n        name\n    )}\nfake ]\n""",\n    1,\n]\nnext()\n',
    "变量 = [\n    '🙂',\n    {'é': [\n        1,\n    ]},\n]\n下一步()\n",
    "def first(\n    value: list[\n        int\n    ] = [1,\n         2],\n):\n    return value\nnext()\n",
    "if condition:\n    def first(\n        value: list[\n            int\n        ],\n    ):\n        return value\n    another()\nnext()\n",
    "if (\n    condition\n):\n    first()\nelse:\n    second()\nnext()\n",
    "value = [\n    1,\n]; another()\nnext()\n",
    "value = (\n    'first'\n    'second'\n)\nnext()\n",
    pytest.param('value = [\n    f"{mapping["key"]}",\n    1,\n]\nnext()\n', marks=PEP701, id="same-quote-fstring-expression"),
    pytest.param('value = [\n    f"""{name  # fake } ]\n    }""",\n    1,\n]\nnext()\n', marks=PEP701, id="comment-in-fstring-expression"),
    pytest.param("value = [\n    f\"{'\\n'.join(names)}\",\n    1,\n]\nnext()\n", marks=PEP701, id="backslash-in-fstring-expression"),
    pytest.param("def first[T: (\n    int,\n    str,\n)](value: T):\n    return value\nnext()\n", marks=PEP701, id="type-parameter-container"),
    pytest.param('value = [\n    t"""{(\n        name\n    )}\nfake ]\n""",\n    1,\n]\nnext()\n', marks=TSTRINGS, id="multiline-template-expression"),
    pytest.param("value = [\n    t\"{f'{name}'}\",\n    1,\n]\nnext()\n", marks=TSTRINGS, id="fstring-in-template"),
    pytest.param("value = [\n    f\"{t'{name}'}\",\n    1,\n]\nnext()\n", marks=TSTRINGS, id="template-in-fstring"),
]


@pytest.mark.parametrize("source", PROGRAMS)
def test_delimiter_context_replay_preserves_every_partition(source):
    expected = assert_partition(source, [source])
    assert assert_partition(source, source) == expected
    for split in range(len(source) + 1):
        assert assert_partition(source, [source[:split], source[split:]]) == expected


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_internal_checkpoint_preserves_original_indentation(newline):
    source = "if condition:\n\tdata = [\n\t    1,\n\t]\n\tanother()\nelse:\n\tfallback()\n".replace("\n", newline)
    parser = Parser()
    for character in source:
        assert parser.feed(character) == ()
    assert [unit.source for unit in parser.feed("next(")] == [source]
    assert parser.pending == "next("


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
def test_unicode_container_offsets_across_newline_partitions(newline):
    source = "变量 = [\n    '🙂',\n    {'é': [\n        1,\n    ]},\n]\n下一步()\n".replace("\n", newline)
    expected = assert_partition(source, [source])
    for split in range(len(source) + 1):
        assert assert_partition(source, [source[:split], source[split:]]) == expected


def test_closing_delimiter_before_explicit_continuation_is_not_saved_early():
    parser = Parser()
    assert parser.feed("value = [\n") == ()
    assert parser.feed("    1,\n") == ()
    assert parser.feed("] \\\n") == ()
    source = "value = [\n    1,\n] \\\n+ other\n"
    assert [unit.source for unit in parser.feed("+ other\n")] == [source]


def test_new_openers_and_closers_after_checkpoint_are_not_replayed_twice():
    chunks = ["value = [\n", "(", "1", ",", "\n", "2", ",\n", ")", ",\n", "]", "\n"]
    source = "".join(chunks)
    assert assert_partition(source, chunks) == assert_partition(source, [source])


@pytest.mark.parametrize("tail", ["value = [\n    1)\nnext()\n", "value = ({\n    'x': 1]\nnext()\n", "value = [\n    1,\n"])
def test_bad_delimiter_context_never_discards_error_or_exposes_synthetic_locations(tail):
    source = "first()\n" + tail
    parser = Parser(filename="original.py")
    assert [unit.source for unit in parser.feed(source)] == ["first()\n"]
    assert parser.pending == tail
    with pytest.raises(SyntaxError) as expected:
        compile(source, "original.py", "exec", flags=ast.PyCF_ONLY_AST, dont_inherit=True)
    with pytest.raises(SyntaxError) as actual:
        parser.finish()
    for attribute in ("lineno", "offset", "end_lineno", "end_offset", "text", "filename"):
        assert getattr(actual.value, attribute) == getattr(expected.value, attribute)


def test_synthetic_delimiters_never_reach_the_ast_compiler(monkeypatch):
    source = "value = [\n    {'key': (\n        1,\n        2,\n    )},\n]\n"
    original_tree = parser_module._tree
    seen = []

    def tree(text, *args, **kwargs):
        if text:
            assert source.startswith(text)
            seen.append(text)
        return original_tree(text, *args, **kwargs)

    monkeypatch.setattr(parser_module, "_tree", tree)
    parser = Parser()
    units = []
    for character in source:
        units.extend(parser.feed(character))
    units.extend(parser.finish())
    assert [unit.source for unit in units] == [source]
    assert seen[-1] == source


@pytest.mark.parametrize("chunk_size", [1, 16, 1_000_000])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_long_data_containers_have_linear_normalization_and_tokenizer_work(monkeypatch, chunk_size, newline):
    def measure(count):
        source = newline.join(["data = {", *["    'key': [1, 2, 3],"] * count, "}", ""])
        with count_work(monkeypatch) as work:
            parser = Parser()
            units = []
            for offset in range(0, len(source), chunk_size):
                units.extend(parser.feed(source[offset : offset + chunk_size]))
            units.extend(parser.finish())
        assert [unit.source for unit in units] == [source]
        return len(source), work

    _, small = measure(64)
    length, large = measure(256)
    for operation in ("normalized", "tokenized"):
        assert large[operation] <= small[operation] * 5
        assert large[operation] < length * 8
