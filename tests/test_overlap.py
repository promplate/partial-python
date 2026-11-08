"""Bounded reception, task ownership, and cancellation."""

import asyncio

import pytest

from examples import overlap as example
from examples.overlap import execute_buffered
from partial_python import Executor


@pytest.mark.parametrize("status", ["completed", "truncated", "cancelled", "disconnected"])
async def test_loopback_receives_later_source_while_first_tool_awaits(status):
    await asyncio.wait_for(example.run_scenario(status), 5)


async def test_bounded_reception_pauses_producer_and_preserves_execution_order():
    entered = asyncio.Event()
    release = asyncio.Event()
    producer_blocked = asyncio.Event()
    closed = asyncio.Event()
    received = []
    buffered = []
    calls = []

    async def first():
        calls.append("started")
        entered.set()
        await release.wait()
        calls.append("finished")

    async def source():
        try:
            yield "await first()\n"
            await entered.wait()
            for value in (1, 2, 3):
                received.append(value)
                if value == 2:
                    producer_blocked.set()
                yield f"calls.append({value})\n"
        finally:
            closed.set()

    executor = Executor({"first": first, "calls": calls})
    task = asyncio.create_task(execute_buffered(source(), executor, capacity=1, on_buffered=lambda text, depth: buffered.append((text, depth))))
    try:
        # The second later delta is producer-local, blocked behind the one queued delta.
        await asyncio.wait_for(producer_blocked.wait(), 1)
        assert received == [1, 2]
        assert calls == ["started"]
        assert [text for text, _ in buffered] == ["await first()\n", "calls.append(1)\n"]
        assert all(depth <= 1 for _, depth in buffered)
        release.set()
        events = await asyncio.wait_for(task, 1)
        assert calls == ["started", "finished", 1, 2, 3]
        assert len(events) == 4 and executor.state == "finished"
        assert closed.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("same_chunk", [False, True])
async def test_producer_failure_cancels_active_unit_and_never_starts_later_units(same_chunk):
    entered = asyncio.Event()
    closed = asyncio.Event()
    calls = []
    error = OSError("provider disconnected")

    async def first():
        calls.append("started")
        entered.set()
        await asyncio.Event().wait()
        calls.append("finished")

    async def source():
        try:
            yield "await first()\n" + ("calls.append('later')\n" if same_chunk else "")
            await entered.wait()
            if not same_chunk:
                yield "calls.append('later')\n"
            raise error
        finally:
            closed.set()

    executor = Executor({"first": first, "calls": calls})
    with pytest.raises(ExceptionGroup) as caught:
        await asyncio.wait_for(execute_buffered(source(), executor), 1)
    assert caught.value.exceptions == (error,)
    assert calls == ["started"] and closed.is_set()
    assert executor.state == "failed" and isinstance(executor.error, asyncio.CancelledError)
    assert executor.failed_unit.source == "await first()\n"
    assert [unit.source for unit in executor.queued] == (["calls.append('later')\n"] if same_chunk else [])


@pytest.mark.parametrize("blocked_on", ["queue", "receive"])
async def test_execution_failure_closes_producer(blocked_on):
    entered = asyncio.Event()
    buffered_later = asyncio.Event()
    closed = asyncio.Event()
    error = RuntimeError("tool failed")
    calls = []

    async def first():
        entered.set()
        await buffered_later.wait()
        raise error

    async def source():
        try:
            yield "await first()\n"
            await entered.wait()
            yield "calls.append(1)\n"
            if blocked_on == "receive":
                await asyncio.Event().wait()
            yield "calls.append(2)\n"
            calls.append("producer resumed")
        finally:
            closed.set()

    def buffered(text, depth):
        if text == "calls.append(1)\n":
            buffered_later.set()

    executor = Executor({"first": first, "calls": calls})
    with pytest.raises(ExceptionGroup) as caught:
        await asyncio.wait_for(execute_buffered(source(), executor, capacity=1, on_buffered=buffered), 1)
    assert caught.value.exceptions == (error,)
    assert calls == [] and closed.is_set()
    assert executor.error is error and executor.state == "failed"


async def test_local_cancellation_closes_source_and_does_not_finish_pending_suite():
    entered = asyncio.Event()
    closed = asyncio.Event()
    calls = []

    async def first():
        entered.set()
        await asyncio.Event().wait()

    async def source():
        try:
            yield "await first()\nif True:\n    calls.append('pending')\n"
            await asyncio.Event().wait()
        finally:
            closed.set()

    executor = Executor({"first": first, "calls": calls})
    task = asyncio.create_task(execute_buffered(source(), executor))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == [] and closed.is_set()
    assert executor.state == "failed" and isinstance(executor.error, asyncio.CancelledError)
    assert executor.parser_state.pending == "if True:\n    calls.append('pending')\n"


@pytest.mark.parametrize("capacity", [0, -1, True, 1.5])
async def test_invalid_capacity_does_not_consume_source_or_own_executor(capacity):
    entered = False

    async def source():
        nonlocal entered
        entered = True
        yield "pass\n"

    executor = Executor()
    with pytest.raises(ValueError, match="positive integer"):
        await execute_buffered(source(), executor, capacity=capacity)
    assert not entered and executor.state == "open"


async def test_cancelled_tool_closes_waiting_producer_and_propagates_cancellation():
    reading = asyncio.Event()
    closed = asyncio.Event()

    async def tool():
        await reading.wait()
        raise asyncio.CancelledError

    async def source():
        try:
            yield "await tool()\n"
            reading.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    executor = Executor({"tool": tool})
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execute_buffered(source(), executor), 1)
    assert closed.is_set() and executor.state == "failed"
    assert isinstance(executor.error, asyncio.CancelledError)


@pytest.mark.parametrize("swallowed", [False, True])
async def test_cancelled_source_cancels_active_tool_instead_of_becoming_eof(swallowed):
    entered = asyncio.Event()
    closed = asyncio.Event()
    calls = []

    async def tool():
        entered.set()
        await asyncio.Event().wait()

    async def source():
        try:
            yield "await tool()\nif True:\n    calls.append('pending')\n"
            await entered.wait()
            if swallowed:
                asyncio.current_task().cancel()
                try:
                    await asyncio.sleep(0)
                except asyncio.CancelledError:
                    return
            raise asyncio.CancelledError
        finally:
            closed.set()

    executor = Executor({"tool": tool, "calls": calls})
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execute_buffered(source(), executor), 1)
    assert closed.is_set() and calls == []
    assert executor.state == "failed" and isinstance(executor.error, asyncio.CancelledError)
    assert executor.parser_state.pending == "if True:\n    calls.append('pending')\n"


async def test_busy_executor_rejection_does_not_abort_another_owners_operation():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def tool():
        entered.set()
        await release.wait()

    async def source():
        yield "pass\n"

    executor = Executor({"tool": tool})
    running = asyncio.create_task(executor.feed("await tool()\n"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(ExceptionGroup) as caught:
            await execute_buffered(source(), executor)
        assert len(caught.value.exceptions) == 1
        assert isinstance(caught.value.exceptions[0], RuntimeError)
        assert "active operation" in str(caught.value.exceptions[0])
        assert executor.state == "open" and not running.done()
        release.set()
        await running
        await executor.finish()
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
