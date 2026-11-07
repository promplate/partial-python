"""Adversarial display completions and source preservation."""

import ast
import sys

import pytest

from partial_python.preview import Allow, complete


def assert_display(source, result):
    assert result.prefix + result.withheld == source
    assert result.source == result.prefix + result.suffix
    compile(result.source, "<preview-edge>", "exec", flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT, dont_inherit=True)


@pytest.mark.parametrize(
    "source",
    [
        "value = not'hello",
        "value = 'first' if ready else'second",
        "def f():\n    return'hello",
        "def f():\n    yield'hello",
    ],
)
def test_keyword_adjacent_literals_are_not_mistaken_for_string_prefixes(source):
    result = complete(source, Allow.STRING)
    assert result.source == source + "'"
    assert_display(source, result)


@pytest.mark.parametrize(
    "source,suffix",
    [
        ('value = "🙂你好', '"'),
        ('value = R"raw\\u12', '"'),
        ('value = BR"raw\\x1', '"'),
        ('value = rB"raw\\N{', '"'),
        ('value = u"\\u1234', '"'),
        ('value = b"\\x41', '"'),
        ('value = """a\\""', '""'),
        ('value = """a\\\\""', '"'),
        ('value = r"""a\\""', '""'),
        ('value = r"""a\\\\""', '"'),
        ('value = "line\\\ncontinued', '"'),
        ('value = r"line\\\ncontinued', '"'),
        ('value = "# not a comment ] ) }', '"'),
        ('# \' " ] }\nvalue = "hello', '"'),
    ],
)
def test_literal_closure_preserves_escapes_and_lexical_context(source, suffix):
    result = complete(source, Allow.STRING)
    assert result.source == source + suffix
    assert_display(source, result)


@pytest.mark.parametrize(
    "source",
    [
        'value = "\\x1',
        'value = "\\u123',
        'value = "\\U0000000',
        'value = "\\N{SNOWMAN',
        'value = b"\\x1',
        'value = b"你好',
        'value = "actual\nnewline',
        'value = r"actual\rnewline',
        'value = r"odd\\',
        'value = br"odd\\',
        'value = """odd\\',
        'value = "unterminated\nnext = 2\n',
        "value = [1\x00",
        "value = 1\x00\nafter = 2\n",
    ],
)
def test_invalid_or_incomplete_literal_content_stays_visible_as_withheld(source):
    result = complete(source, Allow.ALL)
    assert result.source == ""
    assert result.withheld == source
    assert result.diagnostic is not None
    assert_display(source, result)


@pytest.mark.parametrize(
    "source,suffix",
    [
        ("value = [1, # comment\r2,", "]"),
        ("value = [1, # comment\r\n2,", "]"),
        ("value = [1, # comment\r", "]"),
        ("value = [1, # comment", "\n]"),
        ("if ready:\r    for item in items:", "\n        pass\n"),
        ("if ready:\r\n    for item in items:", "\n        pass\n"),
        ("if ready:\n\tif enabled:", "\n\t    pass\n"),
        ('if ready: # quote " and [', "\n    pass\n"),
    ],
)
def test_comments_line_endings_and_suite_indentation(source, suffix):
    result = complete(source, Allow.BRACKET | Allow.SUITE)
    assert result.source == source + suffix
    assert_display(source, result)


@pytest.mark.parametrize(
    "source",
    [
        '发送("你好"',
        'first = 1\r发送("你好"',
        'first = 1\r\n发送("你好"',
        "(factory()(",
        "value = [send(1",
        'value = [send("hello',
        "value = object.method(1,",
    ],
)
def test_synthetic_closures_never_choose_call_arity(source):
    result = complete(source, Allow.ALL)
    assert result.withheld
    assert "发送" not in result.source
    assert "factory" not in result.source
    assert "send" not in result.source
    assert "method" not in result.source
    assert_display(source, result)


@pytest.mark.parametrize(
    "source,suffix",
    [
        ('value = [发送("你好")', "]"),
        ('发送("你好")\rvalue = [2', "]"),
        ('发送("你好")\r\nvalue = [2', "]"),
        ("value = (factory()", ")"),
    ],
)
def test_closures_outside_already_closed_calls_do_not_withhold_the_container(source, suffix):
    result = complete(source, Allow.BRACKET)
    assert result.source == source + suffix
    assert_display(source, result)


@pytest.mark.parametrize("prefix", ["f", "F", "fr", "rf", "Fr", "rF", "t", "T", "tr", "rt"])
def test_nested_string_grammars_are_never_repaired(prefix):
    source = f'value = {prefix}"unfinished {{name'
    result = complete(source, Allow.ALL)
    assert result.source == ""
    assert result.withheld == source
    assert_display(source, result)


def test_bad_text_type_does_not_decode_bytes_implicitly():
    with pytest.raises(TypeError, match="source must be str"):
        complete(b"value = 1\n", Allow.ALL)


@pytest.mark.parametrize("kind", ["f", pytest.param("t", marks=pytest.mark.skipif(sys.version_info < (3, 14), reason="template strings require Python 3.14"))])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("prefix_template", ["label = {literal}\n", "from __future__ import annotations\nlabel = {literal}\n", "if ready:\n    label = {literal}\n"])
@pytest.mark.parametrize("tail,suffix", [("values = ['hel", "']"), ("values = [1,", "]"), ("if ready:\n    for item in items:", "\n        pass\n")])
def test_committed_nested_strings_do_not_block_independent_tail_repairs(kind, newline, prefix_template, tail, suffix):
    prefix = prefix_template.format(literal=kind + '"{1}"').replace("\n", newline)
    source = prefix + tail.replace("\n", newline)
    result = complete(source, Allow.ALL)
    assert result.source == source + suffix and result.prefix == source
    assert result.committed == len(prefix) and result.withheld == ""
    assert result.diagnostic is not None and result.provisional
    assert_display(source, result)


@pytest.mark.parametrize("source", ['label = f"{1}"; values = [1,', 'if ready:\n    label = f"{1}"\n    values = [1,'])
def test_nested_strings_in_the_same_pending_unit_still_block_repairs(source):
    result = complete(source, Allow.ALL)
    assert result.source == "" and result.committed == 0
    assert result.withheld == source and not result.suffix
    assert_display(source, result)


@pytest.mark.parametrize("tail", ["send(['hel", "broken = ]\nvalues = ['hel", 'values = f"unfinished {name'])
def test_committed_string_history_does_not_bypass_tail_restrictions(tail):
    prefix = 'label = f"{1}"\n'
    source = prefix + tail
    result = complete(source, Allow.ALL)
    assert result.source == prefix and result.committed == len(prefix)
    assert result.withheld == tail and not result.suffix
    assert_display(source, result)


def test_comments_after_committed_nested_strings_keep_their_own_lexical_context():
    source = 'label = f"{1}"\r\n# f"ignored"\r\nvalues = [1, # comment'
    result = complete(source, Allow.BRACKET)
    assert result.source == source + "\n]"
    assert_display(source, result)
