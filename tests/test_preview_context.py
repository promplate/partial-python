"""Preview follows ordered parser grammar, not one retroactive module compile."""

import ast
import warnings

import pytest

from partial_python import Allow, Parser, complete

BARRY = "from __future__ import barry_as_FLUFL\n"


@pytest.mark.parametrize("allow", [Allow.NONE, Allow.TAIL, Allow.ALL])
def test_committed_barry_source_uses_the_flags_active_for_each_unit(allow):
    source = BARRY + "different = 1 <> 2\n"
    result = complete(source, allow)
    assert result.source == source and result.committed == len(source)
    assert not result.withheld and not result.suffix and not result.provisional
    assert result.diagnostic is None


@pytest.mark.parametrize("allow", [Allow.NONE, Allow.TAIL, Allow.ALL])
def test_unsealed_barry_tail_uses_committed_future_flags(allow):
    tail = "different = 1 <> 2"
    result = complete(BARRY + tail, allow)
    assert result.committed == len(BARRY) and result.diagnostic is None
    assert result.source == BARRY + (tail if allow else "")
    assert result.withheld == ("" if allow else tail)
    assert result.provisional == bool(allow)


@pytest.mark.parametrize("allow", [Allow.NONE, Allow.TAIL, Allow.ALL])
def test_invalid_operator_after_barry_reports_original_line(allow):
    tail = "different = 1 != 2\n"
    result = complete(BARRY + tail, allow)
    assert result.source == BARRY and result.withheld == tail
    assert isinstance(result.diagnostic, SyntaxError)
    assert result.diagnostic.lineno == 2 and result.diagnostic.text == tail
    assert result.diagnostic.filename == "<partial-python-preview>"


@pytest.mark.parametrize("allow", [Allow.NONE, Allow.TAIL, Allow.ALL])
def test_later_future_does_not_reinterpret_earlier_inequality(allow):
    source = "before = 1 != 2\n" + BARRY + "after = 1 <> 2\n"
    result = complete(source, allow)
    assert result.source == source and result.committed == len(source)
    assert result.diagnostic is None and not result.provisional
    # Preview intentionally adds no Executor prologue validation: this is the
    # same ordered AST grammar that already accepted these source ranges.
    parser = Parser()
    assert "".join(unit.source for unit in parser.feed(source)) == source


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_bracket_repair_uses_future_flags_and_global_diagnostic_lines(newline):
    prefix = BARRY.replace("\n", newline)
    tail = "values = [1 <> 2"
    result = complete(prefix + tail, Allow.BRACKET)
    assert result.prefix == prefix + tail and result.suffix == "]"
    assert result.committed == len(prefix) and result.withheld == ""
    assert isinstance(result.diagnostic, SyntaxError) and result.diagnostic.lineno == 2


@pytest.mark.parametrize("prefix", [BARRY, "ready = 1\n", "if True:\n    pass\n\f"])
def test_call_closure_guard_uses_tail_columns_with_original_line_numbers(prefix):
    tail = "send('你好'"
    result = complete(prefix + tail, Allow.ALL)
    committed = prefix.rstrip("\f")
    assert result.source == committed and result.withheld == prefix[len(committed) :] + tail
    assert not result.suffix


def test_data_closer_after_complete_call_is_still_allowed():
    source = BARRY + "values = [send(1 <> 2)"
    result = complete(source, Allow.BRACKET)
    assert result.source == source + "]" and result.suffix == "]"
    assert result.withheld == ""


def test_line_specific_warning_error_uses_original_snapshot_line():
    source = "ready = 1\n" + r"value = '\q'"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        warnings.filterwarnings("error", message=".*invalid escape sequence", category=Warning, lineno=2)
        result = complete(source, Allow.TAIL)
    assert result.source == "ready = 1\n" and result.withheld == r"value = '\q'"
    assert isinstance(result.diagnostic, SyntaxError)
    assert result.diagnostic.lineno == 2


@pytest.mark.parametrize("allow", [Allow.NONE, Allow.ALL])
def test_unicode_diagnostic_retains_full_original_input_and_offsets(allow):
    source = "label = '你好'\n" + BARRY + "broken = '\ud800"
    with pytest.raises(UnicodeEncodeError) as caught:
        compile(source, "<native-preview>", "exec", flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT, dont_inherit=True)
    result = complete(source, allow)
    assert isinstance(result.diagnostic, UnicodeEncodeError)
    assert result.diagnostic.object == source
    assert result.diagnostic.args == caught.value.args
    assert result.withheld == "broken = '\ud800"
