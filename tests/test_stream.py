"""Direct stateful-stream protocol tests; deterministic engineering fixtures only."""

import asyncio

import pytest

from partial_python import Executor


async def chunks_of(*chunks):
    for chunk in chunks:
        yield chunk


async def test_stateful_stream_reports_success_and_retains_events():
    executor = Executor()
    events = [event async for event in executor.stream(chunks_of("first = 1\n", "second = first + 1"))]
    assert executor.state == "finished"
    assert executor.events == tuple(events)
    assert [event.index for event in events] == [0, 1]
    assert executor.namespace["second"] == 2
    assert executor.error is None
    assert executor.failed_unit is None
    assert executor.unexecuted == ()
    assert executor.parser.closed is True
    assert executor.parser.pending == ""


async def test_stream_can_continue_previously_fed_partial_source():
    executor = Executor()
    await executor.feed("answer =")
    events = [event async for event in executor.stream(chunks_of(" 42\n"))]
    assert len(events) == 1
    assert events[0].unit.source == "answer = 42\n"
    assert executor.namespace["answer"] == 42
    assert executor.state == "finished"


async def test_stream_runtime_error_preserves_full_source_accounting():
    source = "before = 1\n1 / 0\nnever = 2\nincomplete ="
    executor = Executor()
    observed = []
    with pytest.raises(ZeroDivisionError) as captured:
        async for event in executor.stream(chunks_of(source)):
            observed.append(event)
    assert executor.state == "failed"
    assert executor.error is captured.value
    assert executor.events == tuple(observed)
    assert executor.failed_unit.source == "1 / 0\n"
    assert [statement.source for statement in executor.unexecuted] == ["never = 2\n"]
    assert executor.parser.pending == "incomplete ="
    reconstructed = "".join(event.unit.source for event in observed) + executor.failed_unit.source + "".join(statement.source for statement in executor.unexecuted) + executor.parser.pending
    assert reconstructed == source
    assert "never" not in executor.namespace


async def test_early_close_keeps_queue_and_abandons_pending_source():
    closed = []

    async def source():
        try:
            yield "first = 1\nsecond = 2\nincomplete ="
        finally:
            closed.append(True)

    executor = Executor()
    stream = executor.stream(source())
    await anext(stream)
    await stream.aclose()
    assert executor.state == "aborted"
    assert executor.error is None
    assert executor.failed_unit is None
    assert [statement.source for statement in executor.unexecuted] == ["second = 2\n"]
    assert executor.parser.pending == "incomplete ="
    assert "second" not in executor.namespace
    assert closed == [True]
    with pytest.raises(RuntimeError, match="aborted"):
        await executor.finish()


async def test_stream_owns_executor_while_suspended_at_event_yield():
    executor = Executor()
    stream = executor.stream(chunks_of("first = 1\nsecond = 2\n"))
    try:
        await anext(stream)
        with pytest.raises(RuntimeError, match="active"):
            await executor.feed("injected = 3\n")
        with pytest.raises(RuntimeError, match="active"):
            await executor.finish()
        with pytest.raises(RuntimeError, match="active"):
            executor.abort()
        competing = executor.stream(chunks_of("injected = 4\n"))
        try:
            with pytest.raises(RuntimeError, match="active"):
                await anext(competing)
        finally:
            await competing.aclose()
        assert executor.error is None
        assert executor.state == "open"
        assert (await anext(stream)).index == 1
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert executor.state == "finished"
        assert "injected" not in executor.namespace
    finally:
        await stream.aclose()


async def test_constructing_or_closing_unstarted_stream_does_not_own_executor():
    acquired = []

    class Source:
        def __aiter__(self):
            acquired.append(True)
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    executor = Executor()
    stream = executor.stream(Source())
    assert acquired == []
    await executor.feed("value = 1\n")
    await stream.aclose()
    assert acquired == []
    assert executor.state == "open"
    assert executor.error is None
    await executor.finish()
    assert executor.namespace["value"] == 1


async def test_unstarted_stream_checks_executor_state_when_first_consumed():
    executor = Executor()
    stream = executor.stream(chunks_of("never = 1\n"))
    await executor.finish()
    with pytest.raises(RuntimeError, match="finished"):
        await anext(stream)
    assert executor.state == "finished"
    assert executor.error is None
    assert "never" not in executor.namespace


async def test_source_failure_records_error_without_finalizing_pending_suite():
    closed = []
    failure = ConnectionError("provider disconnected")

    async def source():
        try:
            yield "before = 1\nif True:\n    never = 2\n"
            raise failure
        finally:
            closed.append(True)

    executor = Executor()
    with pytest.raises(ConnectionError) as captured:
        async for _ in executor.stream(source()):
            pass
    assert captured.value is failure
    assert executor.error is failure
    assert executor.failed_unit is None
    assert executor.state == "failed"
    assert executor.namespace["before"] == 1
    assert "never" not in executor.namespace
    assert executor.parser.pending == "if True:\n    never = 2\n"
    assert closed == [True]


async def test_source_iterator_acquisition_failure_releases_executor_guard():
    failure = ValueError("cannot acquire source")

    class Source:
        def __aiter__(self):
            raise failure

    executor = Executor()
    with pytest.raises(ValueError) as captured:
        await anext(executor.stream(Source()))
    assert captured.value is failure
    assert executor.error is failure
    assert executor.state == "failed"
    with pytest.raises(RuntimeError, match="failed"):
        await executor.feed("never = 1\n")


async def test_source_reentrancy_is_rejected_without_poisoning_stream():
    executor = Executor()
    rejections = []

    async def source():
        with pytest.raises(RuntimeError, match="active"):
            await executor.feed("injected = 1\n")
        rejections.append(True)
        yield "value = 42\n"

    events = [event async for event in executor.stream(source())]
    assert rejections == [True]
    assert len(events) == 1
    assert executor.namespace["value"] == 42
    assert "injected" not in executor.namespace
    assert executor.error is None


async def test_cleanup_failure_records_terminal_failure_after_code_success():
    cleanup = OSError("source close failed")

    class Source:
        def __init__(self):
            self.sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return "value = 1\n"

        async def aclose(self):
            raise cleanup

    executor = Executor()
    with pytest.raises(OSError) as captured:
        async for _ in executor.stream(Source()):
            pass
    assert captured.value is cleanup
    assert executor.error is cleanup
    assert executor.state == "failed"
    assert executor.failed_unit is None
    assert executor.namespace["value"] == 1
    assert len(executor.events) == 1


async def test_cleanup_failure_keeps_primary_execution_error_in_diagnostics():
    async def source():
        try:
            yield "1 / 0\n"
        finally:
            raise OSError("source close failed")

    executor = Executor()
    with pytest.raises(ZeroDivisionError) as captured:
        await anext(executor.stream(source()))
    assert executor.error is captured.value
    assert executor.state == "failed"
    assert executor.failed_unit.source == "1 / 0\n"
    assert any("source close failed" in note for note in captured.value.__notes__)


@pytest.mark.parametrize("broken", ["repr", "add_note", "notes"])
async def test_secondary_diagnostics_cannot_replace_primary_execution_failure(broken):
    primary = ZeroDivisionError("original failure")
    cleanup = OSError("source close failed")

    class BadDetail:
        def __repr__(self):
            raise ValueError("detail repr failed")

    if broken == "repr":
        cleanup = OSError(BadDetail())
    elif broken == "add_note":
        primary.add_note = None
    else:
        primary.__notes__ = object()

    async def source():
        try:
            yield "raise primary\nnever = 1\n"
        finally:
            raise cleanup

    executor = Executor({"primary": primary})
    with pytest.raises(ZeroDivisionError) as captured:
        await anext(executor.stream(source()))
    assert captured.value is executor.error is primary
    assert executor.state == "failed" and executor.failed_unit.source == "raise primary\n"
    assert [unit.source for unit in executor.unexecuted] == ["never = 1\n"]
    if broken == "add_note":
        assert any("source close failed" in note for note in primary.__notes__)


@pytest.mark.parametrize("exception", [asyncio.CancelledError, KeyboardInterrupt, SystemExit])
async def test_interrupt_while_formatting_secondary_diagnostics_remains_terminal(exception):
    interruption = exception("interrupted diagnostics")

    class BadDetail:
        def __repr__(self):
            raise interruption

    async def source():
        try:
            yield "1 / 0\n"
        finally:
            raise OSError(BadDetail())

    executor = Executor()
    with pytest.raises(exception) as captured:
        await anext(executor.stream(source()))
    assert captured.value is executor.error is interruption
    assert executor.state == "failed"
    with pytest.raises(RuntimeError, match="executor is failed"):
        await executor.finish()


async def test_stream_cancellation_records_failed_unit_and_preserves_queue():
    entered = asyncio.Event()

    async def wait():
        entered.set()
        await asyncio.Event().wait()

    executor = Executor({"wait": wait})
    stream = executor.stream(chunks_of("await wait()\nnever = 2\n"))
    task = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as captured:
            await task
        assert executor.error is captured.value
        assert executor.state == "failed"
        assert executor.failed_unit.source == "await wait()\n"
        assert [statement.source for statement in executor.unexecuted] == ["never = 2\n"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await stream.aclose()


@pytest.mark.parametrize("first, later", [("", ""), ("for item in []:\n", "    pass\n")])
async def test_buffered_unsealed_input_yields_to_cancellation_before_requesting_more(first, later):
    pulled, closed = [], []

    async def source():
        try:
            for index in range(8):
                if index == 0:
                    asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
                pulled.append(index)
                yield first if index == 0 else later
        finally:
            closed.append(True)

    executor = Executor()

    async def consume():
        return [event async for event in executor.stream(source())]

    task = asyncio.create_task(consume())
    with pytest.raises(asyncio.CancelledError) as captured:
        await task
    assert pulled == [0] and closed == [True]
    assert executor.state == "failed" and executor.error is captured.value
    assert executor.failed_unit is None and executor.events == executor.unexecuted == ()
    assert executor.parser.pending == first and not executor.parser.closed


async def test_cancellation_during_source_cleanup_cannot_report_success():
    closing = asyncio.Event()

    class Source:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self):
            closing.set()
            await asyncio.Event().wait()

    executor = Executor()
    stream = executor.stream(Source())
    task = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(closing.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as captured:
            await task
        assert executor.error is captured.value
        assert executor.state == "failed"
        assert executor.failed_unit is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await stream.aclose()


async def test_cleanup_attribute_failure_is_recorded_as_terminal_error():
    cleanup = OSError("cannot access source closer")

    class Source:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        @property
        def aclose(self):
            raise cleanup

    executor = Executor()
    with pytest.raises(OSError) as captured:
        async for _ in executor.stream(Source()):
            pass
    assert captured.value is cleanup
    assert executor.error is cleanup
    assert executor.state == "failed"


async def test_cleanup_swallowing_new_cancellation_cannot_report_success():
    closing = asyncio.Event()

    class Source:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self):
            closing.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return

    executor = Executor()
    stream = executor.stream(Source())
    task = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(closing.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as captured:
            await task
        assert executor.error is captured.value
        assert executor.state == "failed"
        assert executor.failed_unit is None
        with pytest.raises(RuntimeError, match="failed"):
            await executor.feed("never = 1\n")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await stream.aclose()
