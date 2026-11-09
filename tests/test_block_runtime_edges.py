"""Control-flow and resource boundaries of opened streaming blocks."""

import ast
import asyncio
import inspect
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar

import pytest

from partial_python import Block, Executor


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("error_type", [asyncio.CancelledError, GeneratorExit, KeyboardInterrupt, SystemExit])
async def test_explicit_body_baseexception_can_be_suppressed_without_task_cancellation(asynchronous, error_type):
    calls = []

    @contextmanager
    def sync_manager():
        try:
            yield
        except error_type:
            calls.append("suppressed")

    @asynccontextmanager
    async def async_manager():
        with sync_manager():
            yield

    keyword, manager = ("async with", async_manager) if asynchronous else ("with", sync_manager)
    executor = Executor({"manager": manager, "error_type": error_type, "calls": calls}, stream_blocks=Block.ALL)
    cancellations = asyncio.current_task().cancelling()
    await executor.feed(f"{keyword} manager():\n    raise error_type()\n")
    await executor.feed("    calls.append('skipped')\ncalls.append('after')\n")
    await executor.finish()
    assert calls == ["suppressed", "after"]
    assert executor.state == "finished" and asyncio.current_task().cancelling() == cancellations


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_suppressed_nested_if_cannot_revive_elif_or_else(asynchronous):
    calls = []

    @contextmanager
    def sync_manager():
        try:
            yield
        except ZeroDivisionError:
            calls.append("suppressed")

    @asynccontextmanager
    async def async_manager():
        with sync_manager():
            yield

    keyword, manager = ("async with", async_manager) if asynchronous else ("with", sync_manager)
    executor = Executor({"manager": manager, "calls": calls}, stream_blocks=Block.ALL)
    lines = [
        "if True:\n",
        f"    {keyword} manager():\n",
        "        if True:\n",
        "            1 / 0\n",
        "        elif calls.append('elif condition'):\n",
        "            calls.append('elif')\n",
        "        else:\n",
        "            calls.append('else')\n",
        "        calls.append('with tail')\n",
        "    calls.append('outer resumed')\n",
        "else:\n",
        "    calls.append('outer else')\n",
        "calls.append('after')\n",
    ]
    for line in lines:
        await executor.feed(line)
    await executor.finish()
    assert calls == ["suppressed", "outer resumed", "after"]


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_assignment_failure_unwinds_multiple_managers_in_native_order(asynchronous):
    calls = []

    @contextmanager
    def sync_manager(name):
        calls.append((name, "enter"))
        try:
            yield (1,)
        except ValueError:
            calls.append((name, "suppressed"))
        finally:
            calls.append((name, "exit"))

    @asynccontextmanager
    async def async_manager(name):
        with sync_manager(name) as value:
            yield value

    keyword, manager = ("async with", async_manager) if asynchronous else ("with", sync_manager)
    executor = Executor({"manager": manager, "calls": calls}, stream_blocks=Block.ALL)
    await executor.feed(f"{keyword} manager('first'), manager('second') as (left, right):\n")
    assert calls == [("first", "enter"), ("second", "enter"), ("second", "suppressed"), ("second", "exit"), ("first", "exit")]
    await executor.feed("    calls.append('skipped')\ncalls.append('after')\n")
    await executor.finish()
    assert calls[-1] == "after" and "skipped" not in calls
    assert "left" not in executor.namespace and "right" not in executor.namespace


async def test_foreign_task_cannot_resume_finish_or_close_context_owned_by_caller():
    marker = ContextVar("block_owner", default="host")
    calls = []
    owner = asyncio.current_task()

    @asynccontextmanager
    async def manager():
        token = marker.set("body")
        calls.append(("enter", asyncio.current_task()))
        try:
            yield
        finally:
            calls.append(("exit", asyncio.current_task()))
            marker.reset(token)

    executor = Executor({"manager": manager, "calls": calls, "marker": marker}, stream_blocks=Block.ALL)
    await executor.feed("async with manager():\n")
    assert marker.get() == "body"

    async def foreign():
        for operation in (lambda: executor.feed("    calls.append('wrong')\n"), executor.finish, executor.aclose):
            with pytest.raises(RuntimeError, match="owning task"):
                await operation()

    await asyncio.create_task(foreign())
    assert executor.state == "open" and marker.get() == "body"
    assert calls == [("enter", owner)]
    await executor.feed("    calls.append(marker.get())\n")
    await executor.finish()
    assert calls == [("enter", owner), "body", ("exit", owner)]
    assert marker.get() == "host"


async def test_taskgroup_spans_fragments_without_changing_parent_task():
    owner, gate, calls = asyncio.current_task(), asyncio.Event(), []

    async def child():
        await gate.wait()
        calls.append("child")

    executor = Executor({"asyncio": asyncio, "owner": owner, "gate": gate, "child": child, "calls": calls}, stream_blocks=Block.ALL)
    await executor.feed("async with asyncio.TaskGroup() as group:\n    task = group.create_task(child())\n")
    task = executor.namespace["task"]
    assert not task.done()
    await executor.feed("    assert asyncio.current_task() is owner\n    gate.set()\n")
    await executor.feed("calls.append('after')\n")
    await executor.finish()
    assert task.done() and calls == ["child", "after"]


async def test_source_failure_preserves_falsey_cleanup_exception():
    class CleanupFailure(Exception):
        def __bool__(self):
            return False

    replacement = CleanupFailure("cleanup")
    original = OSError("source")

    @contextmanager
    def manager():
        try:
            yield
        finally:
            raise replacement

    async def source():
        yield "with manager():\n    pass\n"
        raise original

    executor = Executor({"manager": manager}, stream_blocks=Block.ALL)
    with pytest.raises(CleanupFailure) as caught:
        async for _ in executor.stream(source()):
            pass
    assert caught.value is replacement and executor.error is replacement
    assert replacement.__context__ is original and executor.state == "failed"


async def test_timeout_scope_covers_waiting_for_source_and_restores_cancellation_count():
    owner = asyncio.current_task()
    cancellations = owner.cancelling()

    async def source():
        yield "async with asyncio.timeout(0.01):\n    pass\n"
        await asyncio.Event().wait()
        yield "unreachable = True\n"

    executor = Executor({"asyncio": asyncio}, stream_blocks=Block.ALL)
    with pytest.raises(TimeoutError):
        async for _ in executor.stream(source()):
            pass
    assert executor.state == "failed" and owner.cancelling() == cancellations
    assert "unreachable" not in executor.namespace


async def test_aclose_cancellation_cannot_be_swallowed_by_async_exit():
    entered, calls = asyncio.Event(), []
    holder = []

    class Manager:
        async def __aenter__(self):
            pass

        async def __aexit__(self, *details):
            calls.append("exit")
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                calls.append("swallowed cancellation")
                return True

    async def owner():
        executor = Executor({"manager": Manager()}, stream_blocks=Block.ALL)
        holder.append(executor)
        await executor.feed("async with manager:\n    pass\n")
        await executor.aclose()

    task = asyncio.create_task(owner())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["exit", "swallowed cancellation"]
    assert holder[0].state == "failed" and isinstance(holder[0].error, asyncio.CancelledError)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("source_failure", [False, True])
async def test_multiple_cleanup_failures_keep_the_complete_exception_chain(nested, source_failure):
    primary, inner_error, outer_error = ValueError("body or source"), LookupError("inner exit"), RuntimeError("outer exit")
    calls = []

    @contextmanager
    def manager(name, error):
        try:
            yield
        finally:
            calls.append(name)
            raise error

    header = "with manager('outer', outer_error):\n    with manager('inner', inner_error):\n" if nested else "with manager('outer', outer_error), manager('inner', inner_error):\n"
    body_indent = "        " if nested else "    "
    executor = Executor({"manager": manager, "outer_error": outer_error, "inner_error": inner_error, "primary": primary}, stream_blocks=Block.ALL)
    with pytest.raises(RuntimeError) as caught:
        if source_failure:

            async def source():
                yield header + body_indent + "pass\n"
                raise primary

            async for _ in executor.stream(source()):
                pass
        else:
            await executor.feed(header + body_indent + "raise primary\n")
    assert caught.value is outer_error and executor.error is outer_error
    assert outer_error.__context__ is inner_error
    assert inner_error.__context__ is primary
    assert calls == ["inner", "outer"]


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("body_failure", [False, True])
async def test_cancellation_during_unwind_never_commits_outer_context_or_runs_following_code(nested, body_failure):
    waiting, calls, holder = asyncio.Event(), [], []

    class Inner:
        async def __aenter__(self):
            pass

        async def __aexit__(self, *details):
            waiting.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                calls.append("inner swallowed cancellation")
                return True

    class Outer:
        async def __aenter__(self):
            pass

        async def __aexit__(self, kind, error, traceback):
            calls.append(("outer", kind))
            return True

    async def owner():
        executor = Executor({"outer": Outer(), "inner": Inner(), "calls": calls}, stream_blocks=Block.ALL)
        holder.append(executor)
        header = "async with outer:\n    async with inner:\n" if nested else "async with outer, inner:\n"
        body = "raise ValueError('body')" if body_failure else "pass"
        await executor.feed(header + ("        " if nested else "    ") + body + "\ncalls.append('after')\n")

    task = asyncio.create_task(owner())
    await asyncio.wait_for(waiting.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["inner swallowed cancellation", ("outer", asyncio.CancelledError)]
    assert holder[0].state == "failed" and isinstance(holder[0].error, asyncio.CancelledError)


@pytest.mark.parametrize("flags", [Block.MODULE | Block.WITH, Block.ALL])
@pytest.mark.parametrize(
    "fragment",
    [
        "raise StopIteration('body')",
        "raise StopIteration('body'); await noop()",
        "stop(); await noop()",
        "if True: stop(); await noop()",
        "with Manager('enter'):\n    pass",
        "with Manager('exit'):\n    pass",
        "with Manager('enter'):\n    await noop()",
        "with Manager('exit'):\n    await noop()",
        "with Manager('none') as target.value:\n    pass",
        "if stop():\n    await noop()",
        "next(iter(())); await noop()",
        "if next(iter(())): await noop()",
        "with Manager('none') as mapping[next(iter(()))]:\n    pass",
        "with Manager('none') as (await target_mapping())[next(iter(()))]:\n    pass",
    ],
)
async def test_streamed_with_preserves_native_synchronous_stopiteration_suppression(flags, fragment):
    def namespace():
        calls = []

        class Manager:
            def __init__(self, phase):
                self.phase = phase

            def __enter__(self):
                calls.append("enter")
                if self.phase == "enter":
                    raise StopIteration("enter")
                return object()

            def __exit__(self, *details):
                calls.append("exit")
                if self.phase == "exit":
                    raise StopIteration("exit")

        class Target:
            def __setattr__(self, name, value):
                calls.append("assignment")
                raise StopIteration("assignment")

        def stop():
            calls.append("stop")
            raise StopIteration("sync function")

        async def noop():
            calls.append("await")

        async def target_mapping():
            calls.append("target await")
            return {}

        return {"calls": calls, "Manager": Manager, "target": Target(), "mapping": {}, "target_mapping": target_mapping, "stop": stop, "noop": noop, "suppress": suppress}

    source = "with suppress(StopIteration):\n" + "".join("    " + line + "\n" for line in fragment.splitlines()) + "    calls.append('inside after')\ncalls.append('after')\n"
    native, streamed = namespace(), namespace()
    result = eval(compile(source, "native.py", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT), native, native)
    if inspect.isawaitable(result):
        await result
    executor = Executor(streamed, stream_blocks=flags)
    await executor.feed(source)
    await executor.finish()
    assert streamed["calls"] == native["calls"]
    assert streamed["calls"][-1] == "after" and "inside after" not in streamed["calls"]
    assert executor.state == "finished"


@pytest.mark.parametrize(
    "fragment",
    [
        "await stop()",
        "async with Manager('enter'):\n    pass",
        "async with Manager('exit'):\n    pass",
        "raise RuntimeError('manual') from StopIteration(); await stop()",
        "next(generator_stop()); await stop()",
        "wrapped(); await stop()",
    ],
)
async def test_real_user_coroutine_generator_or_explicit_runtimeerror_is_never_unwrapped(fragment):
    async def stop():
        raise StopIteration("user coroutine")

    class Manager:
        def __init__(self, phase):
            self.phase = phase

        async def __aenter__(self):
            if self.phase == "enter":
                raise StopIteration("async enter")

        async def __aexit__(self, *details):
            if self.phase == "exit":
                raise StopIteration("async exit")

    def generator_stop():
        raise StopIteration("user generator")  # noqa: PLR1708 - exercise the native PEP 479 failure, not normal exhaustion.
        yield

    class UserRuntimeError(RuntimeError):
        pass

    def wrapped():
        try:
            raise StopIteration("explicit cause")
        except StopIteration as error:
            raise UserRuntimeError("custom wrapper") from error

    source = "with suppress(StopIteration):\n" + "".join("    " + line + "\n" for line in fragment.splitlines()) + "after = True\n"
    native = {"suppress": suppress, "stop": stop, "Manager": Manager, "generator_stop": generator_stop, "wrapped": wrapped}
    with pytest.raises(RuntimeError) as native_error:
        result = eval(compile(source, "native.py", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT), native, native)
        if inspect.isawaitable(result):
            await result
    streamed = {"suppress": suppress, "stop": stop, "Manager": Manager, "generator_stop": generator_stop, "wrapped": wrapped}
    executor = Executor(streamed, stream_blocks=Block.ALL)
    with pytest.raises(RuntimeError) as streamed_error:
        await executor.feed(source)
        await executor.finish()
    assert isinstance(native_error.value.__cause__, StopIteration)
    assert isinstance(streamed_error.value.__cause__, StopIteration)
    assert type(streamed_error.value) is type(native_error.value)
    assert str(streamed_error.value) == str(native_error.value)
    assert "after" not in streamed and executor.state == "failed"


async def test_exception_instance_remains_an_ordinary_expression_value():
    namespace = {"error": ValueError("a value")}
    executor = Executor(namespace, stream_blocks=Block.ALL)
    await executor.feed("if error:\n    selected = error\n")
    await executor.finish()
    assert namespace["selected"] is namespace["error"]
