"""Task scopes and execution acknowledgement across streamed units."""

import asyncio
import sys
from contextlib import asynccontextmanager
from contextvars import ContextVar

import pytest

from partial_python import Executor


async def numbers():
    for number in range(3):
        yield number


async def double(value):
    return value * 2


async def allowed(value):
    return value % 2 == 0


async def test_stream_units_share_the_callers_task_and_context():
    marker = ContextVar("stream_context", default="host")
    owner, observed = asyncio.current_task(), []

    async def inspect_context():
        await asyncio.sleep(0)
        observed.append((asyncio.current_task(), marker.get()))

    async def chunks():
        yield "token = marker.set('stream')\n"
        assert marker.get() == "stream" and asyncio.current_task() is owner
        yield "await inspect_context()\n"
        yield "marker.reset(token)\n"

    executor = Executor({"marker": marker, "inspect_context": inspect_context})
    async for _ in executor.stream(chunks()):
        assert asyncio.current_task() is owner
    assert observed == [(owner, "stream")]
    assert marker.get() == "host" and executor.state == "finished"


async def execute(source, namespace, chunk_size):
    executor = Executor(namespace)
    for start in range(0, len(source), chunk_size):
        await executor.feed(source[start : start + chunk_size])
    await executor.finish()
    assert "".join(event.unit.source for event in executor.events) == source
    return executor


@pytest.mark.parametrize("chunk_size", [1, 7, 10000])
async def test_await_assignment_retains_module_scope_and_all_binding_names(chunk_size):
    async def amount():
        return 2

    async def get_callback():
        return lambda: 7

    source = "count = 10\ncount = count + await amount()\n_callback = await get_callback()\n_result = _callback()\n"
    executor = await execute(source, {"amount": amount, "get_callback": get_callback}, chunk_size)
    # An async-function wrapper would make count local and can lose callable or underscore bindings.
    assert executor.namespace["count"] == 12
    assert callable(executor.namespace["_callback"])
    assert executor.namespace["_result"] == 7


@pytest.mark.parametrize("chunk_size", [1, 7, 10000])
async def test_async_comprehensions_keep_global_lookup_walrus_and_iteration_scope(chunk_size):
    source = """offset = 3
values = [offset + item for item in range(3)]
selected = [await double(item) async for item in numbers() if await allowed(item)]
mapping = {item: await double(item) async for item in numbers()}
total = 0
running = [(total := total + item) async for item in numbers()]
leaked = 'item' in globals()
"""
    executor = await execute(source, {"numbers": numbers, "double": double, "allowed": allowed}, chunk_size)
    assert executor.namespace["values"] == [3, 4, 5]
    assert executor.namespace["selected"] == [0, 4]
    assert executor.namespace["mapping"] == {0: 0, 1: 2, 2: 4}
    assert executor.namespace["running"] == [0, 1, 3]
    assert executor.namespace["total"] == 3
    assert executor.namespace["leaked"] is False


@pytest.mark.parametrize("chunk_size", [1, 7, 10000])
async def test_async_generator_expression_reads_namespace_when_consumed(chunk_size):
    source = "factor = 2\nstream = (factor * await double(item) async for item in numbers())\nfactor = 5\nresult = [item async for item in stream]\n"
    executor = await execute(source, {"numbers": numbers, "double": double}, chunk_size)
    assert executor.namespace["result"] == [0, 10, 20]
    assert "item" not in executor.namespace


async def test_async_with_and_async_for_else_wait_for_real_eof():
    calls = []

    @asynccontextmanager
    async def manager():
        calls.append("enter")
        try:
            yield "tool"
        finally:
            calls.append("exit")

    source = """async with manager() as marker:
    async for item in numbers():
        calls.append((marker, await double(item)))
    else:
        calls.append('else')
"""
    executor = Executor({"manager": manager, "numbers": numbers, "double": double, "calls": calls})
    for character in source:
        assert await executor.feed(character) == ()
    assert calls == []
    events = await executor.finish()
    assert [event.unit.source for event in events] == [source]
    assert calls == ["enter", ("tool", 0), ("tool", 2), ("tool", 4), "else", "exit"]


async def test_awaited_decorator_stays_with_the_complete_definition():
    calls = []

    async def choose():
        calls.append("choose")

        def decorate(function):
            calls.append("decorate")
            return function

        return decorate

    async def amount():
        calls.append("amount")
        return 7

    definition = "@(await choose())\nasync def work():\n    return await amount()\n"
    executor = Executor({"choose": choose, "amount": amount})
    for character in definition:
        assert await executor.feed(character) == ()
    assert calls == []
    events = await executor.feed("result = await work()\n")
    assert len(events) == 2
    assert events[0].unit.source == definition
    assert calls == ["choose", "decorate", "amount"]
    assert executor.namespace["result"] == 7
    await executor.finish()


@pytest.mark.parametrize("chunk_size", [1, 7, 10000])
async def test_exception_group_await_handlers_keep_cleanup_and_source_order(chunk_size):
    calls = []

    async def fail():
        raise ExceptionGroup("tools", [ValueError("first"), TypeError("second")])

    async def record(kind, group):
        calls.append((kind, len(group.exceptions)))

    source = """try:
    await fail()
except* ValueError as errors:
    await record('value', errors)
except* TypeError as errors:
    await record('type', errors)
finally:
    calls.append('finally')
calls.append(('done', 'errors' in globals()))
"""
    executor = await execute(source, {"fail": fail, "record": record, "calls": calls}, chunk_size)
    assert calls == [("value", 1), ("type", 1), "finally", ("done", False)]
    assert "errors" not in executor.namespace


@pytest.mark.skipif(sys.version_info < (3, 12), reason="Same-quote f-string expressions require Python 3.12+")
async def test_pep701_await_inside_same_quote_fstring_executes_once_at_boundary():
    calls = []

    async def lookup(key):
        calls.append(key)
        return 42

    source = 'result = f"value={await lookup("key")}"'
    executor = Executor({"lookup": lookup})
    for character in source:
        assert await executor.feed(character) == ()
    assert calls == []
    await executor.feed("\n")
    assert calls == ["key"]
    assert executor.namespace["result"] == "value=42"
    await executor.finish()


@pytest.mark.skipif(sys.version_info < (3, 14), reason="Template strings require Python 3.14+")
async def test_template_await_interpolation_retains_native_value_and_format_spec():
    calls = []

    async def amount():
        calls.append("amount")
        return 7

    source = 'template = t"answer={await amount():04d}"'
    executor = Executor({"amount": amount})
    for character in source:
        assert await executor.feed(character) == ()
    assert calls == []
    await executor.finish()
    assert calls == ["amount"]
    template = executor.namespace["template"]
    assert template.strings == ("answer=", "")
    assert template.interpolations[0].value == 7
    assert template.interpolations[0].format_spec == "04d"
