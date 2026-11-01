"""Runtime coverage for Python 3.15 lazy imports across compilation units.

Only authored temporary constant modules are imported; stdlib corpus code is
never executed by these tests.
"""

import sys
import types
from uuid import uuid4

import pytest

from partial_python import Executor, UnsupportedSyntaxError

pytestmark = pytest.mark.skipif(sys.version_info < (3, 15), reason="explicit lazy imports require Python 3.15")


@pytest.fixture
def lazy_target(tmp_path, monkeypatch):
    name = "_partial_python_lazy_" + uuid4().hex
    (tmp_path / (name + ".py")).write_text("VALUE = 41\nOTHER = 1\nTYPE = int\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        yield name
    finally:
        sys.modules.pop(name, None)
        # Keep the interpreter's other pending imports untouched.
        for entry in tuple(sys.lazy_modules):
            if isinstance(entry, str) and (entry == name or entry.startswith(name + ".")):
                sys.lazy_modules.discard(entry)


@pytest.mark.parametrize("named_namespace", [False, True])
async def test_lazy_import_resolves_in_a_later_unit_and_preserves_module_identity(lazy_target, named_namespace):
    namespace = {"__name__": "incremental_lazy_test"} if named_namespace else {}
    executor = Executor(namespace)
    events = await executor.feed(f"lazy import {lazy_target} as target\n")
    assert len(events) == 1
    assert lazy_target not in sys.modules
    assert type(namespace["target"]) is types.LazyImportType
    await executor.feed("answer = target.VALUE\n")
    assert namespace["answer"] == 41
    assert namespace["target"] is sys.modules[lazy_target]
    await executor.feed(f"import {lazy_target} as eager\nsame = eager is target\n")
    assert namespace["same"] is True
    await executor.finish()


async def test_lazy_from_names_resolve_independently_across_units(lazy_target):
    namespace = {}
    executor = Executor(namespace)
    await executor.feed(f"lazy from {lazy_target} import VALUE as first, OTHER as second\n")
    assert lazy_target not in sys.modules
    assert type(namespace["first"]) is types.LazyImportType
    assert type(namespace["second"]) is types.LazyImportType
    await executor.feed("answer = first\n")
    assert namespace["answer"] == 41
    assert type(namespace["second"]) is types.LazyImportType
    await executor.feed("answer += second\n")
    assert namespace["answer"] == 42
    await executor.finish()


async def test_later_function_code_resolves_lazy_shared_globals(lazy_target):
    namespace = {}
    executor = Executor(namespace)
    await executor.feed(f"lazy import {lazy_target} as target\n")
    await executor.feed("def get_value():\n    return target.VALUE\nready = True\n")
    assert lazy_target not in sys.modules
    assert namespace["get_value"].__globals__ is namespace
    await executor.feed("answer = get_value()\n")
    assert namespace["answer"] == 41
    await executor.finish()


@pytest.mark.parametrize("future_annotations", [False, True])
async def test_function_annotations_preserve_lazy_import_semantics(lazy_target, future_annotations):
    namespace = {}
    executor = Executor(namespace)
    if future_annotations:
        await executor.feed("from __future__ import annotations\n")
    await executor.feed(f"lazy import {lazy_target} as target\n")
    await executor.feed("def identity(value: target.TYPE):\n    return value\nready = True\n")
    assert lazy_target not in sys.modules
    await executor.feed("annotations = identity.__annotations__\n")
    if future_annotations:
        assert namespace["annotations"] == {"value": "target.TYPE"}
        assert lazy_target not in sys.modules
    else:
        assert namespace["annotations"] == {"value": int}
        assert lazy_target in sys.modules
    await executor.feed("answer = target.VALUE\n")
    assert namespace["answer"] == 41
    await executor.finish()


async def test_type_alias_reifies_lazy_import_only_when_its_value_is_requested(lazy_target):
    namespace = {}
    executor = Executor(namespace)
    await executor.feed(f"lazy import {lazy_target} as target\n")
    await executor.feed("type Alias = target.TYPE\n")
    assert lazy_target not in sys.modules
    await executor.feed("resolved = Alias.__value__\n")
    assert namespace["resolved"] is int
    assert lazy_target in sys.modules
    await executor.finish()


async def test_lazy_compatibility_configuration_is_read_from_shared_runtime_globals(lazy_target):
    namespace = {}
    executor = Executor(namespace)
    await executor.feed(f"__lazy_modules__ = [{lazy_target!r}]\n")
    await executor.feed(f"import {lazy_target} as target\n")
    assert lazy_target not in sys.modules
    assert type(namespace["target"]) is types.LazyImportType
    await executor.feed("__lazy_modules__ = []\n")
    assert lazy_target not in sys.modules
    await executor.feed("answer = target.VALUE\n")
    assert namespace["answer"] == 41
    await executor.finish()


async def test_lazy_import_in_module_control_flow_remains_native_python(lazy_target):
    namespace = {}
    executor = Executor(namespace)
    assert await executor.feed(f"if True:\n    lazy import {lazy_target} as target\n") == ()
    await executor.feed("ready = True\n")
    assert lazy_target not in sys.modules
    await executor.feed("answer = target.VALUE\n")
    assert namespace["answer"] == 41
    await executor.finish()


@pytest.mark.parametrize(
    "source",
    [
        "lazy from __future__ import annotations\n",
        "lazy from math import *\n",
        "def bad():\n    lazy import math\n",
        "class Bad:\n    lazy import math\n",
        "try:\n    lazy import math\nexcept Exception:\n    pass\n",
    ],
)
async def test_invalid_lazy_import_forms_keep_native_compiler_restrictions(source):
    with pytest.raises(SyntaxError):
        compile(source, "native.py", "exec", dont_inherit=True)
    executor = Executor()
    with pytest.raises(SyntaxError) as error:
        await executor.feed(source)
        await executor.finish()
    assert not isinstance(error.value, UnsupportedSyntaxError)
    assert executor.state == "failed"
