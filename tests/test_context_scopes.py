"""Native context protocols retained across independently supplied fragments."""

import ast
import asyncio
import inspect
import math
import sys
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from partial_python._contexts import Scope, compile_binding


class Manager:
    def __init__(self, *, value=None, result=False, exit_error=None):
        self.value, self.result, self.exit_error = value, result, exit_error
        self.calls = []

    def __enter__(self):
        self.calls.append(("enter", asyncio.current_task()))
        return self.value

    async def __aenter__(self):
        return self.__enter__()

    def __exit__(self, kind, error, traceback):
        self.calls.append(("exit", asyncio.current_task(), kind, error, traceback, sys.exception()))
        if self.exit_error is not None:
            raise self.exit_error
        return self.result

    async def __aexit__(self, *details):
        return self.__exit__(*details)


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_scope_keeps_calling_task_and_exits_exactly_once(asynchronous):
    owner = asyncio.current_task()
    manager = Manager(value=object())
    scope, value = await Scope.open(manager, asynchronous)
    assert value is manager.value
    assert [call[0] for call in manager.calls] == ["enter"]
    assert await scope.close() is None
    assert await scope.close() is None
    assert [call[0] for call in manager.calls] == ["enter", "exit"]
    assert all(call[1] is owner for call in manager.calls)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("suppressed", [False, True])
@pytest.mark.parametrize("error_type", [ValueError, StopIteration, StopAsyncIteration, GeneratorExit, asyncio.CancelledError])
async def test_close_preserves_control_exceptions_and_active_exception(asynchronous, suppressed, error_type):
    error = error_type("body")
    try:
        raise error
    except error_type:
        original_traceback = error.__traceback__
    manager = Manager(result=suppressed)
    scope, _ = await Scope.open(manager, asynchronous)
    assert await scope.close(error) is (None if suppressed else error)
    call = manager.calls[-1]
    assert call[2] is error_type and call[3] is error and call[4] is original_traceback and call[5] is error
    assert error.__traceback__ is original_traceback


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_normal_exit_never_tests_truthiness_of_exit_result(asynchronous):
    class UnusableTruth:
        def __bool__(self):
            raise AssertionError("Native normal exit ignores its return value")

    scope, _ = await Scope.open(Manager(result=UnusableTruth()), asynchronous)
    assert await scope.close() is None


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_exit_failure_replaces_body_failure_and_preserves_chain(asynchronous):
    original, replacement = ValueError("body"), RuntimeError("cleanup")
    manager = Manager(exit_error=replacement)
    scope, _ = await Scope.open(manager, asynchronous)
    assert await scope.close(original) is replacement
    assert replacement.__context__ is original
    assert manager.calls[-1][-1] is original
    assert await scope.close() is None
    assert [call[0] for call in manager.calls] == ["enter", "exit"]


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_assignment_failure_is_given_to_entered_manager(asynchronous):
    manager = Manager(value=(1,), result=True)
    scope, value = await Scope.open(manager, asynchronous)
    try:
        _first, _second = value
    except ValueError as error:
        assert await scope.close(error) is None
    assert manager.calls[-1][2] is ValueError


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_native_special_lookup_ignores_instance_attributes(asynchronous):
    def wrong(*args):
        raise AssertionError("Instance attributes must not override the protocol")

    manager = Manager()
    manager.__enter__ = manager.__aenter__ = manager.__exit__ = manager.__aexit__ = wrong
    # Avoid the test fixture's explicit calls through overridden instance methods.
    if asynchronous:

        class AsyncManager(Manager):
            async def __aenter__(self):
                return Manager.__enter__(self)

            async def __aexit__(self, *details):
                return Manager.__exit__(self, *details)

        manager = AsyncManager()
        manager.__aenter__ = manager.__aexit__ = wrong
    scope, _ = await Scope.open(manager, asynchronous)
    assert await scope.close() is None
    assert [call[0] for call in manager.calls] == ["enter", "exit"]


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_native_special_descriptors_bind_the_manager_instance(asynchronous):
    calls = []

    class Method:
        def __init__(self, name):
            self.name = name

        def __get__(self, instance, owner):
            assert instance is not None
            calls.append((self.name, instance))
            if asynchronous:

                async def method(*args):
                    return instance if self.name == "enter" else False

                return method
            return lambda *args: instance if self.name == "enter" else False

    class DescriptorManager:
        __enter__ = __aenter__ = Method("enter")
        __exit__ = __aexit__ = Method("exit")

    manager = DescriptorManager()
    scope, value = await Scope.open(manager, asynchronous)
    assert value is manager
    assert await scope.close() is None
    actual = calls.copy()
    calls.clear()
    if asynchronous:
        async with manager:
            pass
    else:
        with manager:
            pass
    assert actual == calls and len(calls) == 2


async def test_contextvar_token_is_reset_in_its_original_context():
    marker = ContextVar("scope_marker", default="host")

    @asynccontextmanager
    async def manager():
        token = marker.set("body")
        try:
            yield
        finally:
            marker.reset(token)

    scope, _ = await Scope.open(manager(), True)
    assert marker.get() == "body"
    await asyncio.sleep(0)
    assert await scope.close() is None
    assert marker.get() == "host"


async def test_taskgroup_can_span_fragments_in_its_calling_task():
    calls = []
    scope, group = await Scope.open(asyncio.TaskGroup(), True)

    async def child():
        await asyncio.sleep(0)
        calls.append("done")

    task = group.create_task(child())
    assert await scope.close() is None
    assert task.done() and calls == ["done"]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("error_type", [ValueError, StopIteration, StopAsyncIteration, GeneratorExit, asyncio.CancelledError])
async def test_enter_failure_propagates_without_calling_exit(asynchronous, error_type):
    original = error_type("enter")

    class FailingEnter(Manager):
        def __enter__(self):
            raise original

    manager = FailingEnter()
    scope, error = await Scope.open(manager, asynchronous)
    assert scope is None
    if asynchronous and error_type is StopIteration:
        assert isinstance(error, RuntimeError) and error.__cause__ is original
    else:
        assert error is original
    assert manager.calls == []


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("error_type", [StopIteration, StopAsyncIteration, GeneratorExit, asyncio.CancelledError])
async def test_exit_control_exception_is_not_consumed(asynchronous, error_type):
    original, replacement = ValueError("body"), error_type("exit")
    scope, _ = await Scope.open(Manager(exit_error=replacement), asynchronous)
    error = await scope.close(original)
    if asynchronous and error_type is StopIteration:
        assert isinstance(error, RuntimeError) and error.__cause__ is replacement
    else:
        assert error is replacement
    assert replacement.__context__ is original
    assert await scope.close() is None


async def test_unrelated_caller_exception_does_not_replace_original_body_chain():
    original = ValueError("body")
    manager = Manager()
    scope, _ = await Scope.open(manager, False)
    try:
        raise OSError("host")
    except OSError:
        assert await scope.close(original) is original
    assert original.__context__ is None
    assert manager.calls[-1][-1] is original


async def test_cancellation_during_exit_is_propagated_without_replaying_cleanup():
    started, calls = asyncio.Event(), []

    class WaitingExit:
        async def __aenter__(self):
            pass

        async def __aexit__(self, *details):
            calls.append("exit")
            started.set()
            await asyncio.Event().wait()

    async def owner():
        scope, _ = await Scope.open(WaitingExit(), True)
        try:
            error = await scope.close()
            if error is not None:
                raise error
        finally:
            assert await scope.close() is None

    task = asyncio.create_task(owner())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["exit"]


@pytest.mark.parametrize(
    "target",
    [
        "bound",
        "box.value",
        "box.items[index()]",
        "mapping[key()]",
        "mapping[tuple(sorted(locals()))]",
        "mapping[globals() is locals()]",
        "(await fetch()).value",
        "mapping[await async_key()]",
        "mapping[1e1000 - 1e1000]",
    ],
)
async def test_binding_matches_native_assignment_without_namespace_temporaries(target):
    value = object()

    def namespace():
        log = []
        box = SimpleNamespace(items=[None, None])

        def index():
            log.append("index")
            return 1

        def key():
            log.append("key")
            return "item"

        async def fetch():
            log.append("fetch")
            return box

        async def async_key():
            log.append("async_key")
            return "item"

        return {"manager": Manager(value=value), "box": box, "mapping": {}, "index": index, "key": key, "fetch": fetch, "async_key": async_key, "log": log}

    source = f"with manager as {target}:\n    pass\n"
    tree = ast.parse(source)
    code, index = compile_binding(tree.body[0].items[0].optional_vars, "binding.py", ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    constants = list(code.co_consts)
    constants[index] = value
    binding = code.replace(co_consts=tuple(constants))
    actual, expected = namespace(), namespace()
    for compiled, scope in [(binding, actual), (compile(source, "native.py", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT), expected)]:
        result = eval(compiled, scope, scope)
        if inspect.isawaitable(result):
            await result
    assert actual.keys() == expected.keys()
    assert actual["log"] == expected["log"]
    assert vars(actual["box"]) == vars(expected["box"])
    assert list(actual["mapping"].values()) == list(expected["mapping"].values())
    if "1e1000" not in target:
        assert actual["mapping"] == expected["mapping"]
    else:
        assert all(isinstance(key, float) and math.isnan(key) for key in actual["mapping"])
    if "bound" in actual:
        assert actual["bound"] is value


@pytest.mark.parametrize("value", [[1, 2, 3, 4], [1], {"not": "a literal"}, lambda: 1, ValueError("value")])
async def test_binding_accepts_arbitrary_objects_and_preserves_unpacking(value):
    target = ast.parse("first, *middle, last = None").body[0].targets[0]
    code, index = compile_binding(target, "binding.py", 0)
    constants = list(code.co_consts)
    constants[index] = value
    namespace = {}
    if not isinstance(value, list) or len(value) < 2:
        expected_error = ValueError if isinstance(value, (dict, list)) else TypeError
        with pytest.raises(expected_error):
            eval(code.replace(co_consts=tuple(constants)), namespace, namespace)
    else:
        eval(code.replace(co_consts=tuple(constants)), namespace, namespace)
        assert (namespace["first"], namespace["middle"], namespace["last"]) == (1, [2, 3], 4)
    assert set(namespace) <= {"__builtins__", "first", "middle", "last"}


async def test_bound_value_is_not_inspected_or_computed_as_a_user_expression():
    class Opaque:
        def __repr__(self):
            raise AssertionError("binding must not render the object")

        def __eq__(self, other):
            raise AssertionError("binding must not compare the object")

        def __hash__(self):
            raise AssertionError("binding must not hash the object")

    value = Opaque()
    target = ast.parse("bound = None").body[0].targets[0]
    code, index = compile_binding(target, "binding.py", 0)
    constants = list(code.co_consts)
    constants[index] = value
    namespace = {}
    eval(code.replace(co_consts=tuple(constants)), namespace, namespace)
    assert namespace["bound"] is value
    assert set(namespace) == {"__builtins__", "bound"}


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_nested_scope_cleanup_chain_survives_unrelated_handled_exception(asynchronous):
    primary, inner_error, outer_error = ValueError("body"), LookupError("inner exit"), RuntimeError("outer exit")
    outer, _ = await Scope.open(Manager(exit_error=outer_error), asynchronous)
    inner, _ = await Scope.open(Manager(exit_error=inner_error), asynchronous)
    try:
        raise primary
    except ValueError:
        assert await inner.close(primary) is inner_error
        assert await outer.close(inner_error) is outer_error
    assert outer_error.__context__ is inner_error
    assert inner_error.__context__ is primary
