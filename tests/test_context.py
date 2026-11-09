"""Compiler context and original source locations."""

import pickle
import sys
from types import SimpleNamespace

import pytest

from partial_python import MODULE, Executor, UnsupportedSyntaxError


def execute_native(source, namespace=None):
    namespace = namespace if namespace is not None else {}
    exec(compile(source, "native_context.py", "exec", dont_inherit=True), namespace)  # noqa: S102
    return namespace


async def execute(source, namespace=None):
    executor = Executor(namespace, filename="generated_context.py")
    for character in source:
        await executor.feed(character)
    await executor.finish()
    return executor


@pytest.mark.parametrize(
    "prologue",
    [
        '"doc"; from __future__ import annotations\n',
        '("doc" "string"); from __future__ import annotations\n',
        '# comment\n\f"doc"\nfrom __future__ import annotations\n',
        "from __future__ import annotations; from __future__ import generator_stop\n",
    ],
)
async def test_docstring_and_future_semicolon_units_match_native_namespace(prologue):
    source = prologue + 'first: Missing = 1; second: OtherMissing = 2\n"ordinary later string"\n'
    native = execute_native(source, {"__doc__": "existing"})
    executor = await execute(source, {"__doc__": "existing"})
    assert executor.namespace["__doc__"] == native["__doc__"]
    assert executor.namespace["__annotations__"] == native["__annotations__"]
    assert executor.namespace["first"] == native["first"]
    assert executor.namespace["second"] == native["second"]


@pytest.mark.parametrize("prefix", ["value = 1; ", "pass; ", "b'bytes'; ", "0; "])
async def test_noninitial_string_in_semicolon_unit_does_not_replace_docstring(prefix):
    source = '"original doc"\n' + prefix + '"ordinary"\n"also ordinary"; value = 2\n'
    native = execute_native(source)
    executor = await execute(source)
    assert executor.namespace["__doc__"] == native["__doc__"] == "original doc"
    assert executor.namespace["value"] == 2


@pytest.mark.parametrize("prefix", ['"doc"; "ordinary"\n', 'b"not a docstring"\n', '"doc"; value = 1\n'])
async def test_nonprologue_units_reject_later_future_import(prefix):
    executor = Executor()
    await executor.feed(prefix)
    with pytest.raises(SyntaxError):
        await executor.feed("from __future__ import annotations\n")


async def test_function_and_class_scopes_preserve_global_and_annotation_behavior():
    source = """outside = 1
class Box:
    global outside
    outside = 2
    value: int = 3
    def update(self):
        global outside
        local: int = 4
        outside = local
        return local
box = Box()
result = box.update()
"""
    native = execute_native(source)
    executor = await execute(source)
    assert executor.namespace["outside"] == native["outside"] == 4
    assert executor.namespace["result"] == native["result"] == 4
    assert executor.namespace["Box"].__annotations__ == native["Box"].__annotations__ == {"value": int}


async def test_nested_function_nonlocal_preserves_its_complete_scope():
    source = """def outer():
    value: int = 1
    def update():
        nonlocal value
        value += 1
        return value
    return update
update = outer()
first = update()
second = update()
"""
    native = execute_native(source)
    executor = await execute(source)
    assert (executor.namespace["first"], executor.namespace["second"]) == (native["first"], native["second"]) == (2, 3)


@pytest.mark.parametrize("source", ["if False:\n    global x\n", "try:\n    pass\nfinally:\n    global x\n", "for _ in ():\n    global x\n"])
async def test_module_global_policy_applies_through_control_flow(source):
    executor = Executor()
    with pytest.raises(UnsupportedSyntaxError, match="global"):
        await executor.feed(source)
        await executor.finish()


@pytest.mark.skipif(sys.version_info < (3, 14), reason="Deferred module annotations are specific to Python 3.14+")
@pytest.mark.parametrize("source", ["if False:\n    x: int = 1\n", "try:\n    pass\nfinally:\n    x: int = 1\n", "for _ in ():\n    x: int = 1\n"])
async def test_annotation_policy_applies_through_module_control_flow(source):
    executor = Executor()
    with pytest.raises(UnsupportedSyntaxError, match="annotations"):
        await executor.feed(source)
        await executor.finish()


@pytest.mark.skipif(sys.version_info < (3, 14), reason="Deferred module metadata is specific to Python 3.14+")
@pytest.mark.parametrize("source", ["(value): int\n", "box.value: int = 9\n", "values[0]: int = 9\n", "if True:\n    box.value: int = 9\n"])
async def test_nonsimple_annotations_are_guarded_before_mutating_native_module_metadata(source):
    namespace = execute_native("prior: int = 7\n", {"box": SimpleNamespace(), "values": [0]})
    annotate = namespace["__annotate__"]
    assert annotate(1) == {"prior": int}
    executor = Executor(namespace)
    with pytest.raises(UnsupportedSyntaxError, match="annotations"):
        await executor.feed(source)
        await executor.finish()
    assert annotate(1) == {"prior": int}
    assert namespace["values"] == [0] and not hasattr(namespace["box"], "value")
    # Native non-simple annotations have no new __annotate__, but reset the
    # conditional-entry set read by the existing function. A simple-target-only
    # guard would silently lose these annotations in a caller-owned namespace.
    execute_native(source, namespace)
    assert namespace["__annotate__"] is annotate and annotate(1) == {}


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
async def test_original_unit_line_counts_and_multiline_string_values(newline):
    source = newline.join(['doc = """first', "second", 'third"""', "result = doc.splitlines()", ""])
    native = execute_native(source)
    executor = await execute(source)
    assert executor.namespace["doc"] == native["doc"]
    assert executor.namespace["result"] == ["first", "second", "third"]
    assert [event.unit.start_line for event in executor.events] == [1, 4]
    assert [event.unit.end_line for event in executor.events] == [4, 5]
    assert "".join(event.unit.source for event in executor.events) == source


@pytest.mark.parametrize("source", ["\fglobal π\n", "# comment\fmarker\nglobal π\n", 'π = "\u2028"; global x\n', "# comment\u0085marker\nglobal π\n"])
async def test_policy_error_source_lines_follow_python_newlines_only(source):
    executor = Executor(filename="generated_context.py")
    await executor.feed("prior = 1\n")
    with pytest.raises(UnsupportedSyntaxError) as captured:
        await executor.feed(source)
        await executor.finish()
    error = captured.value
    normalized_lines = source.split("\n")
    global_line_index = next(index for index, line in enumerate(normalized_lines) if "global" in line)
    expected_line = normalized_lines[global_line_index] + "\n"
    assert error.text == expected_line
    assert error.filename == "generated_context.py"
    assert error.lineno == global_line_index + 2
    assert error.offset == expected_line.index("global") + 1
    assert executor.failed_unit.source == source


@pytest.mark.parametrize("source", ["broken =\n", "if True:\npass\n", 'triple = """unterminated'])
async def test_shifted_parser_errors_keep_serializable_location_consistent(source):
    executor = Executor(filename="generated_context.py")
    await executor.feed("first = 1\nsecond = 2\n")
    await executor.feed(source)
    with pytest.raises(SyntaxError) as captured:
        await executor.finish()
    error = captured.value
    with pytest.raises(SyntaxError) as native_error:
        compile("first = 1\nsecond = 2\n" + source, "generated_context.py", "exec", dont_inherit=True)
    assert error.msg == native_error.value.msg
    restored = pickle.loads(pickle.dumps(error))
    assert restored.filename == error.filename
    assert restored.lineno == error.lineno
    assert restored.end_lineno == error.end_lineno
    assert restored.offset == error.offset
    assert restored.end_offset == error.end_offset
    assert restored.text == error.text


@pytest.mark.parametrize("source, line", [("return 42\n", 2), ("continue\n", 2), ("def repeat(x, x):\n    pass\n", 2), ("class Box:\n    nonlocal missing\n", 3)])
async def test_scope_compile_errors_retain_original_location(source, line):
    executor = Executor(filename="generated_context.py")
    await executor.feed("prior = 1\n")
    with pytest.raises(SyntaxError) as captured:
        await executor.feed(source)
        await executor.finish()
    assert captured.value.filename == "generated_context.py"
    assert captured.value.lineno == line
    assert captured.value.end_lineno == line
    assert executor.failed_unit.source == source
    assert executor.error is captured.value


@pytest.mark.skipif(sys.version_info < (3, 12), reason="Type statements and type parameter syntax require Python 3.12+")
async def test_type_alias_can_resolve_name_defined_by_later_complete_unit():
    source = "type Alias = Later\nclass Later:\n    pass\nresolved = Alias.__value__\n"
    native = execute_native(source)
    executor = await execute(source)
    assert executor.namespace["resolved"] is executor.namespace["Later"]
    assert native["resolved"] is native["Later"]


@pytest.mark.skipif(sys.version_info < (3, 12), reason="Type parameter syntax requires Python 3.12+")
async def test_generic_function_and_class_keep_their_own_annotation_scopes():
    source = """def identity[T](value: T) -> T:
    return value
class Box[T]:
    value: T
result = identity(42)
"""
    executor = await execute(source)
    identity = executor.namespace["identity"]
    box = executor.namespace["Box"]
    assert identity.__type_params__[0].__name__ == "T"
    assert identity.__annotations__ == {"value": identity.__type_params__[0], "return": identity.__type_params__[0]}
    assert box.__annotations__ == {"value": box.__type_params__[0]}
    assert executor.namespace["result"] == 42


@pytest.mark.parametrize(
    "source, line_index, fragment",
    [
        ("π = 1; return 2\n", 0, "return 2"),
        ("π = 1; continue\n", 0, "continue"),
        ('π = "你好"; break\n', 0, "break"),
        ("def f(π, π):\n    pass\n", 0, "π"),
        ("λ = lambda π, π: None\n", 0, "π"),
        ("f(π=1, π=2)\n", 0, "π=2"),
        ("class C:\n    π = 1; nonlocal missing\n", 1, "nonlocal missing"),
    ],
)
async def test_ast_compile_error_carets_use_characters_after_source_attachment(source, line_index, fragment):
    executor = Executor(filename="unicode_context.py")
    await executor.feed("prior = 1\n")
    with pytest.raises(SyntaxError) as captured:
        await executor.feed(source)
        await executor.finish()
    error = captured.value
    physical_line = source.split("\n")[line_index] + "\n"
    start = physical_line.rindex(fragment) + 1
    assert error.text == physical_line
    assert error.lineno == error.end_lineno == line_index + 2
    assert error.offset == start
    assert error.end_offset == start + len(fragment)
    assert error.text[error.offset - 1 : error.end_offset - 1] == fragment
    restored = pickle.loads(pickle.dumps(error))
    assert (restored.offset, restored.end_offset) == (error.offset, error.end_offset)


async def test_multiline_ast_error_converts_each_endpoint_on_its_own_source_line():
    source = 'π = 1; return (\n    "你好")\n'
    executor = Executor(filename="unicode_context.py")
    await executor.feed("prior = 1\n")
    with pytest.raises(SyntaxError) as captured:
        await executor.feed(source)
    error = captured.value
    first, last, _ = source.split("\n")
    assert error.text == first + "\n"
    assert error.lineno == 2
    assert error.end_lineno == 3
    assert error.offset == first.index("return") + 1
    assert error.end_offset == len(last) + 1


@pytest.mark.skipif(sys.version_info < (3, 12), reason="Type parameter syntax requires Python 3.12+")
async def test_duplicate_unicode_type_parameter_reports_character_range():
    source = "def f[π, π]():\n    pass\n"
    executor = Executor(filename="unicode_context.py")
    with pytest.raises(SyntaxError) as captured:
        await executor.feed(source)
        await executor.finish()
    first = source.split("\n")[0]
    assert captured.value.text == first + "\n"
    assert captured.value.offset == first.rindex("π") + 1
    assert captured.value.end_offset == first.rindex("π") + 2


async def test_deep_compound_keeps_all_nested_effects_until_successor():
    depth = 70
    lines = ["    " * level + "if True:\n" for level in range(depth)]
    lines.append("    " * depth + "calls.append('leaf')\n")
    source = "".join(lines) + "calls.append('after')\n"
    native_calls = []
    execute_native(source, {"calls": native_calls})
    calls = []
    executor = Executor({"calls": calls}, stream_blocks=MODULE)
    for line in lines:
        await executor.feed(line)
        assert calls == []
    await executor.feed("calls.append('after')\n")
    await executor.finish()
    assert calls == native_calls == ["leaf", "after"]
    assert len(executor.events) == 2


async def test_deep_nested_function_scope_preserves_names_and_first_line():
    depth = 35
    lines = ["prior = 1\n"]
    lines.extend("    " * level + f"def f{level}():\n" for level in range(depth))
    lines.append("    " * depth + "return 42\n")
    lines.extend("    " * (level + 1) + f"return f{level + 1}()\n" for level in reversed(range(depth - 1)))
    lines.append("answer = f0()\n")
    source = "".join(lines)
    native = execute_native(source)
    executor = Executor()
    for line in lines:
        await executor.feed(line)
    await executor.finish()
    assert executor.namespace["answer"] == native["answer"] == 42
    assert executor.namespace["f0"].__code__.co_firstlineno == native["f0"].__code__.co_firstlineno == 2
