"""Generated parser inputs exercise source preservation, not model behavior."""

import ast

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from partial_python import Allow, Parser, complete

pytestmark = [pytest.mark.filterwarnings("ignore::SyntaxWarning"), pytest.mark.filterwarnings("ignore::DeprecationWarning")]

_EDGE_CHARACTERS = "abcXYZ019_ ' ' \"\\#()[]{}:=,+-*/;.\n\r\t\f\v\x00\x01\x85\u2028\u0301🙂你好" + "\ud800\udfff"
_CHARACTERS = st.one_of(st.sampled_from(_EDGE_CHARACTERS), st.characters(exclude_categories=("Cs",)))
_TOKENS = [
    "name",
    "not",
    "return",
    "await",
    "if",
    "else",
    "for",
    "in",
    "try",
    "except",
    "finally",
    "pass",
    "# comment",
    "'",
    '"',
    "'''",
    '"""',
    "r",
    "b",
    "f",
    "t",
    "\\",
    "\\u",
    "\\N{",
    "(",
    ")",
    "[",
    "]",
    "{",
    "}",
    ":",
    "=",
    ",",
    ";",
    "\n",
    "\r",
    "\r\n",
    "\t",
    " ",
    "🙂",
    "\x00",
    "\ud800",
]
_ARBITRARY_SOURCE = st.one_of(st.text(_CHARACTERS, max_size=120), st.lists(st.sampled_from(_TOKENS), max_size=24).map("".join))


def assert_completion(source, result):
    assert result.prefix + result.withheld == source
    assert result.source == result.prefix + result.suffix
    assert 0 <= result.committed <= len(result.prefix) <= len(source)
    assert source[: result.committed] == result.prefix[: result.committed]
    compile(result.source, "<preview-property>", "exec", flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT, dont_inherit=True)


@settings(max_examples=350, deadline=None)
@example(source="\ud800")
@example(source="ready = 1\nvalue = '\udfff")
@example(source="# comment\ud800\rvalue = [1")
@example(source="value = [1, # comment\r2,")
@example(source="value = '\\u12")
@example(source='value = r"""escaped\\""')
@given(source=_ARBITRARY_SOURCE)
def test_arbitrary_source_preserves_provenance_under_every_policy(source):
    parser = Parser()
    parser.feed(source)
    for policy in range(int(Allow.ALL) + 1):
        result = complete(source, Allow(policy))
        assert_completion(source, result)
        assert result.committed == parser.committed
        if not policy:
            assert result.source == source[: parser.committed]
            assert result.withheld == parser.pending
            assert not result.suffix
            assert not result.provisional


@st.composite
def literal_programs(draw):
    values = draw(st.lists(st.text(_CHARACTERS, max_size=18), max_size=5))
    literal = repr(values)
    wrapper = draw(st.sampled_from(["value = {}", "value = {{'items': {}}}", "value = ({})", "if enabled:\n    value = {}", "def make():\n    return{}", "value = {} # brackets [{{( in comment"]))
    return wrapper.format(literal)


@settings(max_examples=150, deadline=None)
@given(source=literal_programs(), data=st.data())
def test_partial_literal_programs_remain_parseable_without_losing_source(source, data):
    end = data.draw(st.integers(min_value=0, max_value=len(source)), label="prefix end")
    partial = source[:end]
    for policy in range(int(Allow.ALL) + 1):
        assert_completion(partial, complete(partial, Allow(policy)))
    # A completed literal remains visible when its real closer replaces the
    # synthetic one; flags permit partialness, not disappearance on completion.
    for policy in range(1, int(Allow.ALL) + 1):
        result = complete(source, Allow(policy))
        assert result.source == source
        assert result.prefix == source
        assert not result.suffix
        assert not result.withheld


@settings(max_examples=100, deadline=None)
@given(name=st.sampled_from(["f", "F", "fr", "rF", "t", "T", "tr", "rT"]), payload=st.text(alphabet="abcXYZ09 _🙂你好", max_size=40), policy=st.integers(min_value=1, max_value=int(Allow.ALL)))
def test_nested_literal_grammar_is_validated_by_the_host_but_never_guessed(name, payload, policy):
    partial = f"value = {name}'{payload} {{value!r}}"
    result = complete(partial, Allow(policy))
    assert result.source == ""
    assert result.withheld == partial
    assert not result.suffix
    assert_completion(partial, result)

    closed = partial + "'"
    try:
        compile(closed, "<host-grammar>", "exec", flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT, dont_inherit=True)
    except SyntaxError:
        assert complete(closed, Allow(policy)).source == ""
    else:
        assert complete(closed, Allow(policy)).source == closed
