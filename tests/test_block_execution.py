"""Public streaming-block behavior: source delivery must never replay effects."""

import asyncio
import sys
from types import ModuleType

import pytest

from partial_python import ALL, ASYNC_WITH, IF, MODULE, NONE, WITH, Executor, incremental_exec
from partial_python.preview import Allow


class Scope:
    def __init__(self, effects, name="scope", suppress=()):
        self.effects = effects
        self.name = name
        self.suppress = suppress
        self.exits = []
        self.tasks = []

    def __enter__(self):
        self.tasks.append(asyncio.current_task())
        self.effects.append(("enter", self.name))
        return self.name

    def __exit__(self, error_type, error, traceback):
        self.tasks.append(asyncio.current_task())
        self.exits.append(error)
        self.effects.append(("exit", self.name))
        return isinstance(error, self.suppress)


class AsyncScope(Scope):
    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, error_type, error, traceback):
        return self.__exit__(error_type, error, traceback)


@pytest.mark.parametrize("options", [{}, {"stream_blocks": MODULE}])
async def test_default_and_explicit_module_keep_existing_top_level_boundaries(options):
    effects = []
    executor = Executor({"effects": effects}, **options)
    first = await executor.feed("effects.append('first')\n")
    assert len(first) == 1 and effects == ["first"]
    assert await executor.feed("for item in ['loop']:\n    effects.append(item)\n") == ()
    assert effects == ["first"]
    last = await executor.finish()
    assert len(last) == 1 and effects == ["first", "loop"]
    assert [event.index for event in executor.events] == [0, 1]


async def test_none_buffers_and_executes_one_module_in_the_caller_namespace():
    effects = []
    namespace = {"effects": effects, "value": 40}
    executor = Executor(namespace, stream_blocks=NONE)
    source = "global value\nvalue += 2\neffects.append(value)\n"
    for character in source:
        assert await executor.feed(character) == ()
    assert effects == [] and namespace["value"] == 40 and executor.events == ()
    events = await executor.finish()
    assert executor.namespace is namespace
    assert effects == [42] and namespace["value"] == 42
    assert len(events) == 1 and events[0].unit.source == source
    assert executor.events == events and executor.state == "finished"


async def test_none_compiles_all_source_before_any_effect():
    effects = []
    executor = Executor({"effects": effects}, stream_blocks=NONE)
    assert await executor.feed("effects.append('never')\nglobal effects\n") == ()
    with pytest.raises(SyntaxError):
        await executor.finish()
    assert effects == [] and executor.events == () and executor.state == "failed"


async def test_none_preserves_module_future_and_docstring_semantics():
    namespace = {}
    executor = Executor(namespace, stream_blocks=NONE)
    source = '"whole module"\nfrom __future__ import annotations\nvalue: Missing = 3\n"later expression"\n'
    for line in source.splitlines(keepends=True):
        assert await executor.feed(line) == ()
    assert namespace == {}
    await executor.finish()
    assert namespace["__doc__"] == "whole module"
    assert namespace["__annotations__"] == {"value": "Missing"}
    assert namespace["value"] == 3


@pytest.mark.skipif(sys.version_info < (3, 14), reason="deferred module annotations require Python 3.14")
async def test_none_preserves_deferred_module_annotations_without_future_import():
    import annotationlib

    actual, expected = ModuleType("actual"), ModuleType("expected")
    source = "first: int = 1\nsecond: str = 'two'\n"
    exec(compile(source, "native_module.py", "exec", dont_inherit=True), expected.__dict__)  # noqa: S102 - compare against native whole-module semantics.
    executor = Executor(actual.__dict__, stream_blocks=NONE)
    for line in source.splitlines(keepends=True):
        assert await executor.feed(line) == ()
    await executor.finish()
    assert annotationlib.get_annotations(actual) == annotationlib.get_annotations(expected) == {"first": int, "second": str}
    assert actual.first == 1 and actual.second == "two"


async def test_none_abort_never_executes_valid_buffered_source():
    effects = []
    executor = Executor({"effects": effects}, stream_blocks=NONE)
    await executor.feed("effects.append('never')\n")
    executor.abort()
    assert executor.state == "aborted" and executor.error is None and effects == []
    with pytest.raises(RuntimeError):
        await executor.finish()
    assert effects == []


async def test_none_source_failure_does_not_execute_buffered_code():
    effects, closed = [], []
    failure = OSError("transport broke")

    async def chunks():
        try:
            yield "effects.append('never')\n"
            raise failure
        finally:
            closed.append(True)

    executor = Executor({"effects": effects}, stream_blocks=NONE)
    with pytest.raises(OSError) as caught:
        async for _ in executor.stream(chunks()):
            pytest.fail("a failed whole-module source cannot complete an execution")
    assert caught.value is failure and executor.error is failure
    assert effects == [] and closed == [True] and executor.state == "failed"


async def test_none_cancellation_during_finish_stops_the_module():
    effects = []
    entered = asyncio.Event()

    async def wait():
        entered.set()
        await asyncio.Event().wait()

    executor = Executor({"effects": effects, "wait": wait}, stream_blocks=NONE)
    await executor.feed("effects.append('before')\nawait wait()\neffects.append('never')\n")
    assert not entered.is_set() and effects == []
    task = asyncio.create_task(executor.finish())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert effects == ["before"] and executor.state == "failed" and executor.events == ()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("invalid", [0, 1, False, True, None, "MODULE", Allow.NONE, Allow.ALL])
def test_stream_blocks_rejects_values_from_other_option_domains(invalid):
    with pytest.raises(TypeError, match="Block|stream_blocks"):
        Executor(stream_blocks=invalid)


@pytest.mark.parametrize("first,second,selected,conditions", [(True, True, "if", ["if"]), (False, True, "elif", ["if", "elif"]), (False, False, "else", ["if", "elif"])])
async def test_if_branches_cross_chunks_without_reevaluating_conditions(first, second, selected, conditions):
    effects, evaluated = [], []

    def condition(label):
        evaluated.append(label)
        return first if label == "if" else second

    executor = Executor({"effects": effects, "condition": condition}, stream_blocks=MODULE | IF)
    chunks = ["if condition('if'):\n    effects.append('if')\n", "elif condition('elif'):\n    effects.append('elif')\n", "else:\n    effects.append('else')\n"]
    for index, chunk in enumerate(chunks):
        assert await executor.feed(chunk) == ()
        assert effects == ([selected] if ["if", "elif", "else"].index(selected) <= index else [])
    assert evaluated == conditions and executor.events == ()
    events = await executor.finish()
    assert len(events) == 1 and events[0].unit.source == "".join(chunks)
    assert evaluated == conditions and effects == [selected]


async def test_if_condition_and_body_remain_once_only_at_character_boundaries():
    effects = []

    def condition():
        effects.append("condition")
        return True

    executor = Executor({"effects": effects, "condition": condition}, stream_blocks=ALL)
    for character in "if condition():\n    effects.append('body')\n":
        assert await executor.feed(character) == ()
    assert effects == ["condition", "body"] and executor.events == ()
    for character in "else:\n    effects.append('never')\n":
        assert await executor.feed(character) == ()
    assert len(await executor.finish()) == 1
    assert effects == ["condition", "body"]


@pytest.mark.parametrize("scope_type,keyword,flag", [(Scope, "with", WITH), (AsyncScope, "async with", ASYNC_WITH)])
async def test_context_header_and_stable_body_execute_once_before_its_event(scope_type, keyword, flag):
    effects, made = [], []
    scope = scope_type(effects)
    owner = asyncio.current_task()

    def make():
        made.append(True)
        return scope

    executor = Executor({"effects": effects, "make": make}, stream_blocks=MODULE | flag)
    first = f"{keyword} make() as value:\n    effects.append(value)\n"
    assert await executor.feed(first) == ()
    assert made == [True] and effects == [("enter", "scope"), "scope"]
    assert scope.exits == [] and executor.events == ()
    assert await executor.feed("    effects.append('second')\n") == ()
    assert effects == [("enter", "scope"), "scope", "second"]
    events = await executor.feed("effects.append('after')\n")
    assert len(events) == 2 and scope.exits == [None]
    assert effects == [("enter", "scope"), "scope", "second", ("exit", "scope"), "after"]
    assert scope.tasks == [owner, owner] and made == [True]
    assert await executor.finish() == ()


@pytest.mark.parametrize("scope_type,keyword", [(Scope, "with"), (AsyncScope, "async with")])
async def test_multiple_context_managers_enter_once_and_exit_in_reverse_order(scope_type, keyword):
    effects = []
    outer, inner = scope_type(effects, "outer"), scope_type(effects, "inner")
    executor = Executor({"effects": effects, "outer": outer, "inner": inner}, stream_blocks=ALL)
    assert await executor.feed(f"{keyword} outer as first, inner as second:\n    effects.append((first, second))\n") == ()
    assert effects == [("enter", "outer"), ("enter", "inner"), ("outer", "inner")]
    assert len(await executor.finish()) == 1
    assert effects[-2:] == [("exit", "inner"), ("exit", "outer")]
    assert outer.exits == inner.exits == [None]


@pytest.mark.parametrize("scope_type,keyword", [(Scope, "with"), (AsyncScope, "async with")])
async def test_suppressed_body_failure_skips_remaining_body_but_not_following_unit(scope_type, keyword):
    effects = []
    failure = ValueError("body failed")
    scope = scope_type(effects, suppress=(ValueError,))
    executor = Executor({"effects": effects, "scope": scope, "failure": failure}, stream_blocks=ALL)
    assert await executor.feed(f"{keyword} scope:\n    effects.append('before')\n    raise failure\n") == ()
    assert effects == [("enter", "scope"), "before", ("exit", "scope")]
    assert scope.exits == [failure] and executor.state == "open"
    assert await executor.feed("    effects.append('skipped')\n") == ()
    events = await executor.feed("effects.append('after')\n")
    assert len(events) == 2 and effects[-1] == "after" and "skipped" not in effects
    assert scope.exits == [failure]
    await executor.finish()


@pytest.mark.parametrize("scope_type,keyword", [(Scope, "with"), (AsyncScope, "async with")])
async def test_unsuppressed_body_failure_closes_context_without_replaying_prefix(scope_type, keyword):
    effects = []
    failure = ValueError("body failed")
    scope = scope_type(effects)
    executor = Executor({"effects": effects, "scope": scope, "failure": failure}, stream_blocks=ALL)
    with pytest.raises(ValueError) as caught:
        await executor.feed(f"{keyword} scope:\n    effects.append('before')\n    raise failure\n    effects.append('never')\n")
    assert caught.value is failure and executor.error is failure and executor.state == "failed"
    assert effects == [("enter", "scope"), "before", ("exit", "scope")]
    assert scope.exits == [failure] and executor.events == ()
    with pytest.raises(RuntimeError):
        await executor.finish()
    await executor.aclose()
    assert scope.exits == [failure]


@pytest.mark.parametrize("scope_type,keyword", [(Scope, "with"), (AsyncScope, "async with")])
async def test_transport_failure_closes_open_context_and_cannot_be_suppressed_as_eof(scope_type, keyword):
    effects, closed = [], []
    scope = scope_type(effects, suppress=(BaseException,))
    failure = OSError("source disconnected")

    async def chunks():
        try:
            yield f"{keyword} scope:\n    effects.append('before')\n"
            assert effects == [("enter", "scope"), "before"]
            raise failure
        finally:
            closed.append(True)

    executor = Executor({"effects": effects, "scope": scope}, stream_blocks=ALL)
    with pytest.raises(OSError) as caught:
        async for _ in executor.stream(chunks()):
            pytest.fail("the incomplete context cannot emit a completed unit")
    assert caught.value is failure and executor.error is failure and executor.state == "failed"
    assert effects == [("enter", "scope"), "before", ("exit", "scope")]
    assert len(scope.exits) == 1 and closed == [True]


@pytest.mark.parametrize("scope_type,keyword", [(Scope, "with"), (AsyncScope, "async with")])
async def test_open_context_requires_aclose_and_cleanup_does_not_finish_pending_source(scope_type, keyword):
    effects = []
    scope = scope_type(effects)
    executor = Executor({"effects": effects, "scope": scope}, stream_blocks=ALL)
    await executor.feed(f"{keyword} scope:\n    effects.append('before')\n")
    await executor.feed("    effects.append('unfinished'")
    with pytest.raises(RuntimeError, match="aclose"):
        executor.abort()
    assert executor.state == "open" and scope.exits == []
    await executor.aclose()
    assert executor.state == "aborted" and executor.error is None and executor.events == ()
    assert effects == [("enter", "scope"), "before", ("exit", "scope")]
    await executor.aclose()
    assert len(scope.exits) == 1


async def test_aclose_waits_for_async_cleanup_in_the_owning_task():
    effects = []
    cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()

    class GatedScope(AsyncScope):
        async def __aexit__(self, error_type, error, traceback):
            cleanup_entered.set()
            await cleanup_release.wait()
            return await super().__aexit__(error_type, error, traceback)

    scope = GatedScope(effects)

    async def worker():
        executor = Executor({"scope": scope}, stream_blocks=ALL)
        await executor.feed("async with scope:\n    pass\n")
        await executor.aclose()
        return executor

    task = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(cleanup_entered.wait(), 1)
        assert not task.done() and scope.exits == []
        cleanup_release.set()
        executor = await task
        assert executor.state == "aborted" and scope.tasks == [task, task]
        assert effects == [("enter", "scope"), ("exit", "scope")]
    finally:
        cleanup_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("scope_type,keyword", [(Scope, "with"), (AsyncScope, "async with")])
async def test_cancellation_during_body_closes_context_even_when_it_suppresses_cancellation(scope_type, keyword):
    effects = []
    entered = asyncio.Event()
    scope = scope_type(effects, suppress=(asyncio.CancelledError,))

    async def wait():
        entered.set()
        await asyncio.Event().wait()

    executor = Executor({"effects": effects, "scope": scope, "wait": wait}, stream_blocks=ALL)
    task = asyncio.create_task(executor.feed(f"{keyword} scope:\n    await wait()\n    effects.append('never')\n"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert executor.state == "failed" and executor.events == ()
        assert effects == [("enter", "scope"), ("exit", "scope")]
        assert len(scope.exits) == 1 and scope.tasks == [task, task]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("scope_type,keyword", [(Scope, "with"), (AsyncScope, "async with")])
async def test_cancellation_while_receiving_source_closes_the_open_context(scope_type, keyword):
    effects, closed = [], []
    waiting = asyncio.Event()
    scope = scope_type(effects)

    async def chunks():
        try:
            yield f"{keyword} scope:\n    effects.append('before')\n"
            waiting.set()
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    executor = Executor({"effects": effects, "scope": scope}, stream_blocks=ALL)

    async def consume():
        async for _ in executor.stream(chunks()):
            pytest.fail("an unclosed context cannot emit an execution")

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        assert effects == [("enter", "scope"), "before"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert executor.state == "failed" and closed == [True]
        assert effects == [("enter", "scope"), "before", ("exit", "scope")]
        assert len(scope.exits) == 1 and scope.tasks == [task, task]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("scope_type,keyword", [(Scope, "with"), (AsyncScope, "async with")])
async def test_invalid_eof_closes_open_context_without_committing_the_unit(scope_type, keyword):
    effects = []
    scope = scope_type(effects)
    executor = Executor({"effects": effects, "scope": scope}, stream_blocks=ALL)
    await executor.feed(f"{keyword} scope:\n    effects.append('before')\n")
    await executor.feed("    unfinished =")
    with pytest.raises(SyntaxError) as caught:
        await executor.finish()
    assert executor.error is caught.value and executor.state == "failed"
    assert effects == [("enter", "scope"), "before", ("exit", "scope")]
    assert len(scope.exits) == 1 and executor.events == ()


async def test_incremental_exec_forwards_block_selection_and_preserves_namespace_identity():
    effects = []
    namespace = {"effects": effects}

    async def chunks():
        yield "if True:\n    effects.append('early')\n"
        assert effects == ["early"]
        yield "    effects.append('later')\n"

    assert await incremental_exec(chunks(), namespace, stream_blocks=MODULE | IF) is namespace
    assert effects == ["early", "later"]


async def test_incremental_exec_none_waits_for_normal_source_exhaustion():
    effects = []

    async def chunks():
        yield "effects.append('first')\n"
        assert effects == []
        yield "effects.append('second')\n"
        assert effects == []

    await incremental_exec(chunks(), {"effects": effects}, stream_blocks=NONE)
    assert effects == ["first", "second"]


@pytest.mark.parametrize("invalid", [1, True, Allow.ALL])
async def test_incremental_exec_rejects_wrong_options_before_pulling_source(invalid):
    pulled = []

    async def chunks():
        pulled.append(True)
        yield "pass\n"

    source = chunks()
    try:
        with pytest.raises(TypeError, match="Block|stream_blocks"):
            await incremental_exec(source, stream_blocks=invalid)
        assert pulled == []
    finally:
        await source.aclose()


@pytest.mark.parametrize("options", [{}, {"stream_blocks": ALL}], ids=["module-default", "all"])
async def test_real_asyncio_timeout_includes_source_wait_only_when_context_streaming_is_selected(options):
    effects, closed = [], []
    waiting, release = asyncio.Event(), asyncio.Event()
    timeout = asyncio.timeout(None)

    async def chunks():
        try:
            yield "async with timeout:\n    effects.append('body')\n"
            waiting.set()
            await release.wait()
        finally:
            closed.append(True)

    executor = Executor({"timeout": timeout, "effects": effects}, **options)

    async def consume():
        return [event async for event in executor.stream(chunks())]

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        assert executor.events == ()
        deadline = asyncio.get_running_loop().time() - 1
        if options:
            assert effects == ["body"]
            # An explicit expired deadline cancels the owner at its next await;
            # there is no timing race or sleep-based guess about source progress.
            timeout.reschedule(deadline)
            with pytest.raises(TimeoutError) as caught:
                await task
            assert executor.error is caught.value and executor.state == "failed"
            assert timeout.expired() and executor.events == ()
        else:
            assert effects == []
            with pytest.raises(RuntimeError, match="has not been entered"):
                timeout.reschedule(deadline)
            release.set()
            assert len(await task) == 1
            assert effects == ["body"] and executor.state == "finished"
            assert not timeout.expired()
        assert closed == [True]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("fail", [False, True])
async def test_queued_snapshot_excludes_started_compound_during_await_and_after_failure(fail):
    effects = []
    entered, release = asyncio.Event(), asyncio.Event()
    failure = ValueError("body failed after waiting")
    scope = Scope(effects)

    async def wait():
        entered.set()
        await release.wait()
        if fail:
            raise failure

    executor = Executor({"effects": effects, "scope": scope, "wait": wait}, stream_blocks=ALL)
    following = "effects.append('following')\n"
    source = "with scope:\n    effects.append('before')\n    await wait()\n    effects.append('body-end')\n" + following
    task = asyncio.create_task(executor.feed(source))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        snapshot = executor.queued
        assert [unit.source for unit in snapshot] == [following]
        assert executor.events == () and effects == [("enter", "scope"), "before"]
        release.set()
        if fail:
            with pytest.raises(ValueError) as caught:
                await task
            assert caught.value is failure and executor.state == "failed"
            assert executor.queued == snapshot
            assert effects == [("enter", "scope"), "before", ("exit", "scope")]
        else:
            assert len(await task) == 2
            assert executor.queued == ()
            assert effects == [("enter", "scope"), "before", "body-end", ("exit", "scope"), "following"]
        assert [unit.source for unit in snapshot] == [following]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_none_empty_module_matches_native_namespace_initialization_and_emits_one_event():
    native, namespace = {}, {}
    exec("", native)  # noqa: S102 - native empty-module initialization is the comparison oracle.
    executor = Executor(namespace, stream_blocks=NONE)
    assert await executor.feed("") == ()
    assert namespace == {}
    events = await executor.finish()
    assert "__builtins__" in namespace and namespace == native
    assert len(events) == 1 and events[0].unit.source == ""
    assert executor.events == events and executor.state == "finished"
