"""Clarify cancellation acknowledgement and source-cleanup diagnostic contracts."""

import asyncio

import pytest

from partial_python import Executor


async def test_cancelled_unit_can_fully_return_and_assign_without_success_event():
    entered = asyncio.Event()
    effects = []

    async def work():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        effects.append("body completed")
        return 42

    executor = Executor({"work": work})
    task = asyncio.create_task(executor.feed("result = await work()\nnever = 1\n"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as captured:
            await task
        assert effects == ["body completed"]
        assert executor.namespace["result"] == 42
        assert executor.events == ()
        assert executor.failed_unit.source == "result = await work()\n"
        assert executor.error is captured.value
        assert executor.state == "failed"
        assert [unit.source for unit in executor.queued] == ["never = 1\n"]
        assert "never" not in executor.namespace
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_source_yielding_during_close_is_failed_cleanup_not_more_input():
    closed = []

    async def source():
        try:
            yield "first = 1\nsecond = 2\n"
        except GeneratorExit:
            yield "injected_during_close = True\n"
        finally:
            closed.append(True)

    source_iterator = source()
    executor = Executor()
    stream = executor.stream(source_iterator)
    try:
        await anext(stream)
        with pytest.raises(RuntimeError, match="ignored GeneratorExit") as captured:
            await stream.aclose()
        assert executor.state == "failed"
        assert executor.error is captured.value
        assert executor.failed_unit is None
        assert len(executor.events) == 1
        assert [unit.source for unit in executor.queued] == ["second = 2\n"]
        assert "second" not in executor.namespace
        assert "injected_during_close" not in executor.namespace
        assert closed == []
    finally:
        await stream.aclose()
        # This deliberately invalid fixture is left at its illegal yield. Advance
        # only the fixture to its finalizer; a second aclose is not reliable here.
        if source_iterator.ag_frame is not None:
            with pytest.raises(StopAsyncIteration):
                await anext(source_iterator)
    assert closed == [True]


@pytest.mark.parametrize("mode", ["feed", "stream"])
@pytest.mark.parametrize("exception", [StopIteration, StopAsyncIteration])
async def test_unit_iteration_signals_cannot_be_mistaken_for_source_exhaustion(mode, exception):
    primary = exception("unit failed")
    executor = Executor({"primary": primary})
    code = "raise primary\nnever = 1\n"

    async def source():
        yield code

    expected = StopAsyncIteration if mode == "feed" and exception is StopAsyncIteration else RuntimeError
    with pytest.raises(expected) as captured:
        if mode == "feed":
            await executor.feed(code)
        else:
            await anext(executor.stream(source()))
    assert executor.state == "failed" and executor.events == ()
    assert executor.failed_unit.source == "raise primary\n"
    assert [unit.source for unit in executor.queued] == ["never = 1\n"]
    assert "never" not in executor.namespace
    if exception is StopAsyncIteration:
        assert executor.error is primary
        assert (captured.value is primary) if mode == "feed" else (captured.value.__cause__ is primary)
    else:
        assert executor.error is captured.value and captured.value.__cause__ is primary


async def test_native_aclose_may_consume_generator_exit_but_diagnostics_record_failure():
    primary = GeneratorExit("source cleanup failed")

    class Source:
        def __aiter__(self):
            return self

        async def __anext__(self):
            return "first = 1\nnever = 2\n"

        async def aclose(self):
            raise primary

    executor = Executor()
    stream = executor.stream(Source())
    await anext(stream)
    # Python's async-generator close protocol consumes GeneratorExit, including
    # one raised by a source closer. The recorded terminal state still matters.
    assert await stream.aclose() is None
    assert executor.state == "failed" and executor.error is primary
    assert executor.failed_unit is None and len(executor.events) == 1
    assert [unit.source for unit in executor.queued] == ["never = 2\n"]
