import ast

import pytest

from partial_python.preview import Allow, complete


def assert_provenance(original, result):
    assert original == result.prefix + result.withheld
    assert result.source == result.prefix + result.suffix
    assert 0 <= result.committed <= len(result.prefix)
    compile(result.source, "<preview-test>", "exec", flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT, dont_inherit=True)


def test_default_only_displays_committed_source():
    original = "await foo(1)\nfor x in items:\n    await bar(x)\n"
    result = complete(original)
    assert result.source == "await foo(1)\n"
    assert result.withheld == "for x in items:\n    await bar(x)\n"
    assert not result.provisional
    assert_provenance(original, result)


def test_valid_unsealed_tail_requires_explicit_option():
    assert complete("await foo(1)").source == ""
    result = complete("await foo(1)", Allow.TAIL)
    assert result.source == "await foo(1)"
    assert result.suffix == ""
    assert result.committed == 0
    assert result.provisional


@pytest.mark.parametrize(
    "source,suffix,allow",
    [
        ("value = 'hello", "'", Allow.STRING),
        ('value = r"hello', '"', Allow.STRING),
        ('value = b"hello', '"', Allow.STRING),
        ('value = """', '"""', Allow.STRING),
        ('value = """hello"', '""', Allow.STRING),
        ('value = """hello""', '"', Allow.STRING),
        ("value = [1, 2,", "]", Allow.BRACKET),
        ('value = {"key": [1,', "]}", Allow.BRACKET),
        ('value = ["hello', '"]', Allow.STRING | Allow.BRACKET),
        ("value = [1, # note", "\n]", Allow.BRACKET),
        ("if ready:", "\n    pass\n", Allow.SUITE),
        ("if ready:\n    # comment", "\n    pass\n", Allow.SUITE),
        ("if ready:\n    for item in items:", "\n        pass\n", Allow.SUITE),
        ("@decorator\ndef function():", "\n    pass\n", Allow.SUITE),
    ],
)
def test_explicit_closures(source, suffix, allow):
    result = complete(source, allow)
    assert result.prefix == source
    assert result.suffix == suffix
    assert result.withheld == ""
    assert result.provisional
    assert_provenance(source, result)


@pytest.mark.parametrize(
    "source",
    [
        "foo(",
        "foo(1",
        "foo([1",
        'await foo("hello',
        "items[foo(1",
        'value = f"hello',
        'value = f"{name',
        'value = t"hello',
        'value = r"hello\\',
        'value = "\\u12',
        "value = 1 +",
        "value = [1 +",
        'value = {"key":',
        "value = [1)",
    ],
)
def test_does_not_infer_operands_calls_or_unsupported_literals(source):
    result = complete(source, Allow.ALL)
    assert result.source == ""
    assert result.withheld == source
    assert_provenance(source, result)


def test_flags_do_not_implicitly_enable_other_repairs():
    assert complete('value = ["hello', Allow.STRING).source == ""
    assert complete('value = ["hello', Allow.BRACKET).source == ""
    assert complete("if ready:", Allow.BRACKET).source == ""


@pytest.mark.parametrize("allow", [Allow(value) for value in range(1, int(Allow.ALL) + 1)])
def test_any_preview_policy_retains_an_already_valid_tail(allow):
    result = complete("value = 1", allow)
    assert result.source == "value = 1"
    assert result.suffix == ""
    assert result.withheld == ""
    assert result.provisional
    assert_provenance("value = 1", result)


@pytest.mark.parametrize(
    "partial,closed,allow",
    [
        ("value = 'hello", "value = 'hello'", Allow.STRING),
        ('value = """hello', 'value = """hello"""', Allow.STRING),
        ("value = [1, 2", "value = [1, 2]", Allow.BRACKET),
        ('value = {"items": [1', 'value = {"items": [1]}', Allow.BRACKET),
        ('value = ["hello', 'value = ["hello"]', Allow.STRING | Allow.BRACKET),
        ("if ready:", "if ready:\n    pass\n", Allow.SUITE),
        ("if ready:\n    for item in items:", "if ready:\n    for item in items:\n        pass\n", Allow.SUITE),
    ],
)
def test_preview_does_not_disappear_when_real_closers_arrive(partial, closed, allow):
    before = complete(partial, allow)
    after = complete(closed, allow)
    assert before.source == closed
    assert before.suffix
    assert after.source == closed
    assert after.suffix == ""
    assert after.withheld == ""
    assert after.provisional
    assert_provenance(partial, before)
    assert_provenance(closed, after)


def test_invalid_interior_is_preserved_and_explained():
    source = "before = 1\nbroken = ]\nafter = 2\n"
    result = complete(source, Allow.ALL)
    assert result.source == "before = 1\n"
    assert result.withheld == "broken = ]\nafter = 2\n"
    assert isinstance(result.diagnostic, SyntaxError)
    assert_provenance(source, result)


@pytest.mark.parametrize(
    "source",
    [
        'values = {"你好": [1, 2, "three"]}\nawait foo(values)\n',
        "for item in items:\n    await send(item)\nelse:\n    await finish()\nnext_item = 3\n",
        'text = r"raw\\path"\r\ntext2 = """multi\nline"""\r\n',
        'message = f"result: {call(name)!r:>10}"\nvalue = [1, 2]\n',
        "try:\n    await foo()\nexcept ValueError:\n    pass\nfinally:\n    cleanup()\n",
        "@decorator(1)\nasync def foo(x):\n    return await bar(x)\n",
    ],
)
def test_every_character_prefix_and_policy_has_valid_provenance(source):
    for end in range(len(source) + 1):
        for flags in range(int(Allow.ALL) + 1):
            original = source[:end]
            assert_provenance(original, complete(original, Allow(flags)))


def test_rejects_bad_inputs():
    with pytest.raises(TypeError, match="source must be str"):
        complete(None)
    with pytest.raises(ValueError, match="unknown preview flags"):
        complete("x=1", 16)
