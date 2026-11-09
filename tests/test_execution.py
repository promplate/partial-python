"""Ordered execution of stable source units."""

import asyncio
import sys
import traceback
from contextvars import ContextVar

import pytest

from partial_python import MODULE, Executor, UnsupportedSyntaxError, incremental_exec


async def test_executes_closed_await_before_receiving_more_source():
    calls = []

    async def tool(value):
        calls.append(value)

    namespace = {"tool": tool}
    executor = Executor(namespace)
    assert await executor.feed("await tool(1)") == ()
    assert calls == []
    events = await executor.feed("\n")
    assert calls == [1]
    assert events[0].unit.source == "await tool(1)\n"
    assert events[0].index == 0
    assert events[0].completed_at >= events[0].started_at
    assert executor.namespace is namespace
    assert executor.events == events
    assert executor.state == "open"
    await executor.feed("await tool(2)\n")
    await executor.finish()
    assert calls == [1, 2]
    assert executor.state == "finished"


async def test_entire_loop_waits_for_successor_including_else():
    calls = []
    executor = Executor({"calls": calls})
    await executor.feed("for value in range(2):\n    calls.append(value)\n")
    await executor.feed("\n# more output may follow\n")
    assert calls == []
    await executor.feed("else:\n    calls.append('else')\n")
    assert calls == []
    await executor.feed("calls.append('after')\n")
    assert calls == [0, 1, "else", "after"]
    assert len(executor.events) == 2
    await executor.finish()


async def test_normal_eof_seals_compound_but_abort_does_not():
    for finish in (True, False):
        calls = []
        executor = Executor({"calls": calls}, stream_blocks=MODULE)
        await executor.feed("if True:\n    calls.append(1)\n")
        assert calls == []
        if finish:
            await executor.finish()
            assert calls == [1]
        else:
            executor.abort()
            assert executor.state == "aborted"
            assert calls == []
            with pytest.raises(RuntimeError):
                await executor.finish()


async def test_awaits_execute_in_source_order():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def first():
        calls.append("first started")
        entered.set()
        await release.wait()
        calls.append("first completed")

    async def second():
        calls.append("second")

    executor = Executor({"first": first, "second": second})
    task = asyncio.create_task(executor.feed("await first()\nawait second()\n"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert calls == ["first started"]
        release.set()
        events = await asyncio.wait_for(task, 1)
        assert calls == ["first started", "first completed", "second"]
        assert [event.index for event in events] == [0, 1]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_background_task_survives_flush_until_explicitly_awaited():
    entered, release = asyncio.Event(), asyncio.Event()

    async def work():
        entered.set()
        await release.wait()
        return 42

    executor = Executor({"asyncio": asyncio, "work": work})
    await executor.feed("task = asyncio.create_task(work())\n")
    task = executor.namespace["task"]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not task.done()
        await executor.feed("unrelated = 1\n")
        assert not task.done()
        release.set()
        await executor.feed("answer = await task\n")
        assert executor.namespace["answer"] == 42
        await executor.finish()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_runtime_failure_preserves_effects_and_never_replays():
    calls = []
    executor = Executor({"calls": calls})
    with pytest.raises(ZeroDivisionError):
        await executor.feed("calls.append(1)\n1 / 0\ncalls.append(2)\n")
    assert calls == [1]
    assert len(executor.events) == 1
    assert executor.failed_unit.source == "1 / 0\n"
    assert executor.state == "failed"
    with pytest.raises(RuntimeError):
        await executor.feed("calls.append(3)\n")
    with pytest.raises(RuntimeError):
        await executor.finish()
    assert calls == [1]


async def test_cancellation_is_terminal_and_retains_prior_effects():
    entered = asyncio.Event()
    calls = []

    async def wait():
        entered.set()
        await asyncio.Event().wait()

    executor = Executor({"calls": calls, "wait": wait})
    task = asyncio.create_task(executor.feed("calls.append(1)\nawait wait()\ncalls.append(2)\n"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == [1]
        assert executor.state == "failed"
        assert executor.failed_unit.source == "await wait()\n"
        assert len(executor.events) == 1
        with pytest.raises(RuntimeError):
            await executor.finish()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_concurrent_feed_is_rejected_without_accepting_source():
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait():
        entered.set()
        await release.wait()

    executor = Executor({"wait": wait})
    task = asyncio.create_task(executor.feed("await wait()\n"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(RuntimeError, match="active"):
            await executor.feed("unaccepted = True\n")
        with pytest.raises(RuntimeError, match="active"):
            await executor.finish()
        release.set()
        await task
        assert "unaccepted" not in executor.namespace
        assert executor.parser_state.pending == ""
        await executor.finish()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_recursive_feed_is_rejected_without_poisoning_outer_execution():
    executor = Executor()
    failures = []

    async def recurse():
        with pytest.raises(RuntimeError, match="active"):
            await executor.feed("nested = 1\n")
        failures.append("rejected")

    executor.namespace["recurse"] = recurse
    await executor.feed("await recurse()\n")
    assert failures == ["rejected"]
    assert "nested" not in executor.namespace
    await executor.finish()


async def test_future_annotations_carry_across_compilation_units():
    executor = Executor()
    await executor.feed("from __future__ import annotations\n")
    await executor.feed("x: MissingType = 1\n")
    await executor.feed("y: OtherMissingType = 2\n")
    await executor.finish()
    assert executor.namespace["__annotations__"] == {"x": "MissingType", "y": "OtherMissingType"}


async def test_future_annotations_in_same_logical_line_apply_before_scope_guard():
    executor = Executor()
    await executor.feed("from __future__ import annotations; x: Missing = 1\n")
    await executor.finish()
    assert executor.namespace["__annotations__"] == {"x": "Missing"}


async def test_late_future_import_is_rejected():
    executor = Executor()
    await executor.feed("x = 1\n")
    with pytest.raises(SyntaxError):
        await executor.feed("from __future__ import annotations\n")
    assert executor.namespace["x"] == 1


async def test_only_first_string_is_module_docstring():
    executor = Executor()
    await executor.feed('"first docstring"\n')
    await executor.feed('"ordinary later string"\n')
    await executor.feed('"another ordinary string"\n')
    await executor.finish()
    assert executor.namespace["__doc__"] == "first docstring"


async def test_string_after_non_docstring_cannot_create_module_docstring():
    executor = Executor({"__doc__": "existing namespace doc"})
    await executor.feed("x = 1\n")
    await executor.feed('"ordinary string"\n')
    await executor.finish()
    assert executor.namespace["__doc__"] == "existing namespace doc"


async def test_docstring_allows_future_prologue_but_second_string_closes_it():
    executor = Executor()
    await executor.feed('# preface\n"doc"\n')
    await executor.feed("from __future__ import annotations\n")
    await executor.feed('"ordinary string"\n')
    with pytest.raises(SyntaxError):
        await executor.feed("from __future__ import generator_stop\n")


@pytest.mark.skipif(sys.version_info < (3, 14), reason="Deferred module annotations were introduced in Python 3.14")
@pytest.mark.parametrize("source", ["x: int = 1\n", "if True:\n    x: int = 1\n"])
async def test_deferred_module_annotations_require_explicit_future_policy(source):
    executor = Executor()
    with pytest.raises(UnsupportedSyntaxError, match="annotations"):
        await executor.feed(source)
        await executor.finish()
    assert "x" not in executor.namespace


async def test_class_and_function_annotations_keep_their_own_scope():
    executor = Executor()
    await executor.feed("class Box:\n    value: int = 1\n")
    await executor.feed("def identity(value: int) -> int:\n    return value\n")
    await executor.finish()
    assert executor.namespace["Box"].__annotations__ == {"value": int}
    assert executor.namespace["identity"].__annotations__ == {"value": int, "return": int}


async def test_module_global_rejected_but_function_global_allowed():
    executor = Executor()
    await executor.feed("value = 1\n")
    await executor.feed("def update():\n    global value\n    value = 2\n")
    await executor.feed("update()\n")
    assert executor.namespace["value"] == 2
    with pytest.raises(UnsupportedSyntaxError, match="global"):
        await executor.feed("global value\n")


async def test_runtime_traceback_retains_original_line_numbers():
    executor = Executor(filename="generated_actions.py")
    await executor.feed("x = 1\n")
    await executor.feed("y = 2\n")
    with pytest.raises(ZeroDivisionError) as captured:
        await executor.feed("x / 0\n")
    frame = next(frame for frame in reversed(traceback.extract_tb(captured.value.__traceback__)) if frame.filename == "generated_actions.py")
    assert frame.lineno == 3


async def test_compile_error_cannot_execute_its_unit():
    calls = []
    executor = Executor({"calls": calls})
    with pytest.raises(SyntaxError):
        await executor.feed("calls.append(1); return 3\n")
    assert calls == []
    assert executor.state == "failed"


async def test_invalid_eof_never_repairs_or_executes_tail():
    calls = []
    executor = Executor({"calls": calls})
    await executor.feed("calls.append(1)\ncalls.append(")
    with pytest.raises(SyntaxError):
        await executor.finish()
    assert calls == [1]
    assert executor.state == "failed"


async def test_stream_runs_before_requesting_next_delta():
    calls = []

    async def chunks():
        yield "calls.append(1)\n"
        assert calls == [1]
        yield "calls.append(2)"

    events = [event async for event in Executor({"calls": calls}).stream(chunks())]
    assert calls == [1, 2]
    assert len(events) == 2


async def test_closing_event_iterator_leaves_same_chunk_remainder_queued():
    calls = []

    async def chunks():
        yield "calls.append(1)\ncalls.append(2)\n"

    stream = Executor({"calls": calls}).stream(chunks())
    first = await anext(stream)
    assert first.index == 0
    assert calls == [1]
    await stream.aclose()
    assert calls == [1]


async def test_transport_failure_does_not_finalize_pending_compound():
    calls, closed = [], []
    failure = ConnectionError("truncated model response")

    async def chunks():
        try:
            yield "calls.append(1)\nif True:\n    calls.append(2)\n"
            raise failure
        finally:
            closed.append(True)

    with pytest.raises(ConnectionError) as captured:
        await incremental_exec(chunks(), {"calls": calls}, stream_blocks=MODULE)
    assert captured.value is failure
    assert calls == [1] and closed == [True]


async def test_deep_expression_does_not_recurse_through_module_policy_visitor():
    source = "value = " + " + ".join(["1"] * 600) + "\n"
    expected = {}
    exec(compile(source, "<whole-unit>", "exec"), expected)  # noqa: S102 - fixed arithmetic fixture checks native compilation equivalence.
    executor = Executor()
    await executor.feed(source)
    assert executor.namespace["value"] == expected["value"] == 600


async def test_owned_namespace_has_script_identity_without_registering_a_module():
    main = sys.modules["__main__"]
    executor = Executor()
    await executor.feed("if __name__ == '__main__':\n    ran = True\nclass Created:\n    pass\ndef created():\n    pass\n")
    await executor.finish()
    assert executor.namespace["ran"] is True
    assert executor.namespace["Created"].__module__ == "__main__"
    assert executor.namespace["created"].__module__ == "__main__"
    assert sys.modules["__main__"] is main


@pytest.mark.parametrize("namespace", [{}, {"__name__": "provided"}])
async def test_supplied_namespace_identity_is_not_replaced_or_seeded(namespace):
    expected = namespace.copy()
    executor = Executor(namespace)
    assert executor.namespace is namespace
    assert namespace == expected
    await executor.feed("value = 1\n")
    assert namespace.get("__name__") in (None, "provided")


async def test_incremental_exec_runs_early_in_order_and_returns_supplied_namespace():
    calls = []
    namespace = {"calls": calls, "__name__": "provided"}

    async def chunks():
        yield "calls.append('first')\n"
        assert calls == ["first"]
        yield "calls.append('second')\nvalue = 42"

    result = await incremental_exec(chunks(), namespace)
    assert result is namespace
    assert result["value"] == 42 and result["__name__"] == "provided"
    assert calls == ["first", "second"]


async def test_incremental_exec_returns_independent_default_execution_namespaces():
    async def chunks():
        yield "identity = globals()\nvalue = 42"

    first = await incremental_exec(chunks())
    second = await incremental_exec(chunks())
    assert first is first["identity"] and second is second["identity"]
    assert first is not second
    assert first["__name__"] == second["__name__"] == "__main__"
    assert first["value"] == second["value"] == 42


async def test_incremental_exec_preserves_caller_task_context_across_source_and_units():
    context = ContextVar("incremental_exec_context", default="outside")
    owner = asyncio.current_task()
    namespace = {"context": context, "asyncio": asyncio, "owner": owner}

    async def chunks():
        assert asyncio.current_task() is owner
        yield "token = context.set('inside')\n"
        assert context.get() == "inside"
        yield "assert asyncio.current_task() is owner\ncontext.reset(token)"

    await incremental_exec(chunks(), namespace)
    assert context.get() == "outside"


async def test_incremental_exec_runtime_failure_preserves_effects_and_closes_source():
    calls, closed = [], []
    failure = ValueError("tool failed")

    async def chunks():
        try:
            yield "calls.append('once')\nraise failure\ncalls.append('queued')\n"
            pytest.fail("source must not be resumed after execution fails")
        finally:
            closed.append(True)

    with pytest.raises(ValueError) as captured:
        await incremental_exec(chunks(), {"calls": calls, "failure": failure}, filename="helper_stream.py")
    assert captured.value is failure
    assert calls == ["once"] and closed == [True]
    frame = next(frame for frame in reversed(traceback.extract_tb(captured.value.__traceback__)) if frame.filename == "helper_stream.py")
    assert frame.lineno == 2


async def test_incremental_exec_cancellation_joins_cleanup_without_running_later_units():
    calls, closed = [], []
    entered = asyncio.Event()

    async def wait():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            calls.append("cleanup")

    async def chunks():
        try:
            yield "calls.append('once')\nawait wait()\ncalls.append('queued')\n"
            pytest.fail("cancelled execution must not request more source")
        finally:
            closed.append(True)

    task = asyncio.create_task(incremental_exec(chunks(), {"calls": calls, "wait": wait}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert calls == ["once"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == ["once", "cleanup"] and closed == [True]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
