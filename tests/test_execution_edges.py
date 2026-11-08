"""Adversarial execution and terminal-state behavior."""

import asyncio

import pytest

from partial_python import Executor, UnsupportedSyntaxError, incremental_exec


async def test_closing_stream_closes_its_source_iterator():
    closed = []

    async def chunks():
        try:
            yield "first = 1\n"
            yield "second = 2\n"
        finally:
            closed.append(True)

    source = chunks()
    stream = Executor().stream(source)
    try:
        await anext(stream)
        await stream.aclose()
        assert closed == [True]
    finally:
        await stream.aclose()
        await source.aclose()


async def test_execution_failure_closes_source_transport():
    closed = []

    async def chunks():
        try:
            yield "1 / 0\n"
            yield "should_not_arrive = True\n"
        finally:
            closed.append(True)

    source = chunks()
    stream = Executor().stream(source)
    try:
        with pytest.raises(ZeroDivisionError):
            await anext(stream)
        assert closed == [True]
    finally:
        await stream.aclose()
        await source.aclose()


async def test_cancellation_during_execution_closes_source_transport():
    closed = []
    entered = asyncio.Event()

    async def wait():
        entered.set()
        await asyncio.Event().wait()

    async def chunks():
        try:
            yield "await wait()\n"
            yield "unreachable = True\n"
        finally:
            closed.append(True)

    source = chunks()
    stream = Executor({"wait": wait}).stream(source)
    task = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == [True]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await stream.aclose()
        await source.aclose()


async def test_caught_cancellation_still_stops_later_statements():
    entered = asyncio.Event()
    calls = []

    async def wait():
        entered.set()
        await asyncio.Event().wait()

    executor = Executor({"wait": wait, "asyncio": asyncio, "calls": calls})
    source = "try:\n    await wait()\nexcept asyncio.CancelledError:\n    calls.append('caught')\ncalls.append('after')\n"
    task = asyncio.create_task(executor.feed(source))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == ["caught"]
        assert executor.state == "failed"
        assert executor.failed_unit is not None
        assert executor.events == ()
        with pytest.raises(RuntimeError):
            await executor.finish()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancellation_requested_by_sync_statement_stops_the_next_unit():
    calls = []
    executor = Executor({"asyncio": asyncio, "calls": calls})
    task = asyncio.create_task(executor.feed("asyncio.current_task().cancel()\ncalls.append('after cancellation')\n"))
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == []
    assert executor.state == "failed"


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
async def test_eof_syntax_error_reports_original_stream_location(newline):
    executor = Executor(filename="generated_stream.py")
    await executor.feed(f"first = 1{newline}second = 2{newline}")
    await executor.feed(f"broken ={newline}")
    with pytest.raises(SyntaxError) as captured:
        await executor.finish()
    assert captured.value.filename == "generated_stream.py"
    assert captured.value.lineno == 3
    assert captured.value.end_lineno == 3
    assert captured.value.text.strip() == "broken ="
    assert executor.state == "failed"


@pytest.mark.parametrize("source, exception", [("global first\n", UnsupportedSyntaxError), ("from __future__ import annotations\n", SyntaxError)])
async def test_policy_errors_include_original_stream_location(source, exception):
    executor = Executor(filename="generated_stream.py")
    await executor.feed("first = 1\nsecond = 2\n")
    with pytest.raises(exception) as captured:
        await executor.feed(source)
    assert captured.value.filename == "generated_stream.py"
    assert captured.value.lineno == 3
    assert captured.value.text == source
    assert executor.failed_unit.source == source


async def test_backpressure_does_not_pull_next_chunk_before_current_events():
    pulls = []
    calls = []

    async def chunks():
        pulls.append(1)
        yield "calls.append(1)\ncalls.append(2)\n"
        pulls.append(2)
        yield "calls.append(3)\n"

    source = chunks()
    stream = Executor({"calls": calls}).stream(source)
    try:
        assert (await anext(stream)).index == 0
        assert pulls == [1]
        assert calls == [1]
        assert (await anext(stream)).index == 1
        assert pulls == [1]
        assert calls == [1, 2]
        assert (await anext(stream)).index == 2
        assert pulls == [1, 2]
        assert calls == [1, 2, 3]
    finally:
        await stream.aclose()
        await source.aclose()


async def test_generator_supports_iterables_without_aclose():
    class Chunks:
        def __init__(self):
            self.items = iter(["x = 1\n", "y = x + 1\n"])

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.items)
            except StopIteration:
                raise StopAsyncIteration from None

    namespace = {}
    events = [event async for event in Executor(namespace).stream(Chunks())]
    assert len(events) == 2
    assert namespace["y"] == 2


async def test_future_flags_affect_later_parser_and_compiler_units():
    executor = Executor()
    await executor.feed("from __future__ import barry_as_FLUFL\n")
    await executor.feed("different = 1 <> 2\n")
    await executor.finish()
    assert executor.namespace["different"] is True


async def test_namespace_mutation_is_retained_without_replacing_shared_dict():
    namespace = {"keep": 42}
    executor = Executor(namespace)
    await executor.feed("globals().pop('keep')\n")
    await executor.feed("fresh = 43\n")
    assert executor.namespace is namespace
    assert "keep" not in namespace
    assert namespace["fresh"] == 43


async def test_empty_and_comment_only_chunks_preserve_docstring_prologue():
    executor = Executor()
    await executor.feed("")
    await executor.feed("# comment\n\n")
    await executor.feed('"module docstring"\n')
    await executor.feed("from __future__ import annotations\n")
    await executor.feed("value: Unknown = 1\n")
    await executor.finish()
    assert executor.namespace["__doc__"] == "module docstring"
    assert executor.namespace["__annotations__"] == {"value": "Unknown"}


async def test_cancellation_while_receiving_source_cannot_be_swallowed_into_execution():
    entered = asyncio.Event()
    calls = []

    async def chunks():
        try:
            entered.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            yield "calls.append('after cancellation')\n"

    source = chunks()
    stream = Executor({"calls": calls}).stream(source)
    task = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await stream.aclose()
        await source.aclose()


async def test_historical_cancellation_count_does_not_poison_new_execution():
    async def worker():
        task = asyncio.current_task()
        task.cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        assert task.cancelling() == 1
        executor = Executor()
        await executor.feed("value = 42\n")
        await executor.finish()
        return executor.namespace["value"]

    assert await asyncio.create_task(worker()) == 42


async def test_source_swallowing_cancellation_into_eof_cannot_seal_pending_compound():
    entered = asyncio.Event()
    calls = []

    async def chunks():
        yield "if True:\n    calls.append('unsealed')\n"
        try:
            entered.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    source = chunks()
    stream = Executor({"calls": calls}).stream(source)
    task = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await stream.aclose()
        await source.aclose()


async def test_parser_inspection_returns_immutable_independent_snapshot():
    executor = Executor()
    await executor.feed("first = 1")
    snapshot = executor.parser_state
    assert snapshot.pending == "first = 1"
    assert snapshot.committed == 0
    assert snapshot.closed is False
    assert not hasattr(snapshot, "feed")
    assert not hasattr(snapshot, "finish")
    with pytest.raises((AttributeError, TypeError)):
        snapshot.pending = "injected = True\n"
    with pytest.raises((AttributeError, TypeError)):
        executor.parser_state = snapshot
    await executor.feed("\n")
    assert snapshot.pending == "first = 1"
    assert executor.parser_state.pending == ""
    assert executor.parser_state.committed == len("first = 1\n")
    assert executor.namespace["first"] == 1
    await executor.finish()
    assert executor.parser_state.closed is True
    assert snapshot.closed is False


async def test_failed_stream_preserves_all_accepted_source_for_diagnostics():
    source = "before = 1\n1 / 0\nnever = 2\nincomplete ="
    executor = Executor()
    with pytest.raises(ZeroDivisionError) as captured:
        await executor.feed(source)
    assert executor.error is captured.value
    assert executor.failed_unit.source == "1 / 0\n"
    assert isinstance(executor.queued, tuple)
    assert [statement.source for statement in executor.queued] == ["never = 2\n"]
    assert executor.parser_state.pending == "incomplete ="
    reconstructed = "".join(event.unit.source for event in executor.events) + executor.failed_unit.source + "".join(statement.source for statement in executor.queued) + executor.parser_state.pending
    assert reconstructed == source
    assert "never" not in executor.namespace
    with pytest.raises(RuntimeError):
        await executor.feed("invalid retry\n")
    assert executor.error is captured.value
    assert [statement.source for statement in executor.queued] == ["never = 2\n"]


async def test_live_queue_excludes_current_statement_and_preserves_later_units():
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait():
        entered.set()
        await release.wait()

    executor = Executor({"wait": wait})
    task = asyncio.create_task(executor.feed("before = 1\nawait wait()\nafter = 2\n"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert [statement.source for statement in executor.queued] == ["after = 2\n"]
        assert [event.unit.source for event in executor.events] == ["before = 1\n"]
        assert executor.failed_unit is None
        assert executor.error is None
        release.set()
        await task
        assert executor.queued == ()
        assert executor.error is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_compile_failure_keeps_failed_unit_separate_from_queue():
    executor = Executor()
    with pytest.raises(SyntaxError) as captured:
        await executor.feed("return 1\nnever = 2\n")
    assert executor.error is captured.value
    assert executor.failed_unit.source == "return 1\n"
    assert [statement.source for statement in executor.queued] == ["never = 2\n"]
    assert executor.events == ()
    assert "never" not in executor.namespace


async def test_incomplete_tail_error_is_retained_without_fabricated_attempt():
    executor = Executor()
    await executor.feed("before = 1\nincomplete =")
    with pytest.raises(SyntaxError) as captured:
        await executor.finish()
    assert executor.error is captured.value
    assert executor.failed_unit is None
    assert executor.queued == ()
    assert executor.parser_state.pending == "incomplete ="
    assert len(executor.events) == 1


async def test_cancellation_records_error_and_preserves_queued_source():
    entered = asyncio.Event()

    async def wait():
        entered.set()
        await asyncio.Event().wait()

    executor = Executor({"wait": wait})
    task = asyncio.create_task(executor.feed("await wait()\nnever = 2\n"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as captured:
            await task
        assert executor.error is captured.value
        assert executor.failed_unit.source == "await wait()\n"
        assert [statement.source for statement in executor.queued] == ["never = 2\n"]
        assert executor.events == ()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_success_and_explicit_abort_do_not_invent_errors():
    executor = Executor()
    assert executor.error is None
    assert executor.queued == ()
    await executor.feed("value = 1\n")
    await executor.finish()
    assert executor.error is None
    assert executor.queued == ()
    aborted = Executor()
    await aborted.feed("if True:\n    value = 1\n")
    aborted.abort()
    assert aborted.error is None
    assert aborted.failed_unit is None
    assert aborted.queued == ()
    assert aborted.parser_state.pending == "if True:\n    value = 1\n"


async def test_source_cleanup_error_does_not_mask_execution_error():
    async def chunks():
        try:
            yield "1 / 0\n"
        finally:
            raise OSError("source transport close failed")

    source = chunks()
    stream = Executor().stream(source)
    try:
        with pytest.raises(ZeroDivisionError):
            await anext(stream)
    finally:
        await stream.aclose()
        await source.aclose()


async def test_source_cleanup_error_does_not_mask_cancellation():
    entered = asyncio.Event()

    async def wait():
        entered.set()
        await asyncio.Event().wait()

    async def chunks():
        try:
            yield "await wait()\n"
        finally:
            raise OSError("source transport close failed")

    source = chunks()
    stream = Executor({"wait": wait}).stream(source)
    task = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await stream.aclose()
        await source.aclose()


async def test_cleanup_failure_without_primary_error_remains_visible():
    class Chunks:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self):
            raise OSError("source transport close failed")

    with pytest.raises(OSError, match="source transport close failed"):
        await incremental_exec(Chunks())


async def test_source_cancellation_guard_tracks_task_that_resumes_iterator():
    entered = asyncio.Event()
    calls = []

    async def chunks():
        yield "calls.append('first')\n"
        try:
            entered.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            yield "calls.append('after cancellation')\n"

    source = chunks()
    stream = Executor({"calls": calls}).stream(source)
    first = await asyncio.create_task(anext(stream))
    assert first.index == 0
    task = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == ["first"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await stream.aclose()
        await source.aclose()
