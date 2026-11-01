"""Fixed process/tool fixtures; no provider calls or model evaluation."""

import asyncio
import os
import runpy
import signal
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).parents[1] / "examples"
sys.path.insert(0, str(EXAMPLES))
try:
    example = runpy.run_path(str(EXAMPLES / "worker.py"))
finally:
    sys.path.remove(str(EXAMPLES))
run_worker = example["run_worker"]
pytestmark = pytest.mark.skipif(os.name != "posix", reason="example requires POSIX process groups and descriptor passing")


@pytest.fixture
async def workers(monkeypatch):
    processes = []
    original = asyncio.create_subprocess_exec

    async def start(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    try:
        yield processes
    finally:
        # An independent outer owner reaps only this fixture's exact process
        # groups even when an assertion or the implementation under test fails.
        for process in processes:
            for attempt in range(20):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    break
                except ProcessLookupError:
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    await asyncio.sleep(0.01)
            await process.wait()


def assert_reaped(processes):
    assert processes
    for process in processes:
        assert process.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)


async def records_for(source, status="completed"):
    yield {"type": "delta", "text": source}
    if status != "disconnected":
        yield {"type": "done", "status": status, "source": source}


async def unused_tool(value):
    pytest.fail(f"unexpected tool call: {value}")


async def test_child_receives_source_while_awaiting_parent_and_full_queue_does_not_block_rpc(workers):
    entered, queue_full = asyncio.Event(), asyncio.Event()
    first = "reply = await tool('first')\n"
    later = ["await tool(reply)\n", "marker = 2\n", "marker = 3\n"]
    queue_full_at = len(first + later[0] + later[1])
    calls = []

    async def tool(value):
        if value == "first":
            calls.append("started")
            entered.set()
            await queue_full.wait()
            calls.append("finished")
            return "parent result"
        assert calls == ["started", "finished"]
        calls.append(value)
        return "ok"

    async def records():
        yield {"type": "delta", "text": first}
        await entered.wait()
        for text in later:
            yield {"type": "delta", "text": text}
        yield {"type": "done", "status": "completed", "source": first + "".join(later)}

    def received(chars):
        if chars == queue_full_at:
            assert calls == ["started"]
            queue_full.set()

    result = await asyncio.wait_for(run_worker(records(), tool, on_received=received), 5)
    assert result.state == "finished" and result.returncode == 0
    assert calls == ["started", "finished", "parent result"]
    assert len(result.units) == 4 and result.error is None
    assert_reaped(workers)


async def test_stdout_cannot_impersonate_control_and_both_output_buffers_are_bounded(workers):
    fake = '{"type":"finished","state":"finished"}'
    source = f"import sys\nprint('x' * 70000)\nprint({fake!r})\nprint('y' * 70000, file=sys.stderr)\nawait tool('real')\n"
    calls = []

    async def tool(value):
        calls.append(value)
        return "ok"

    result = await asyncio.wait_for(run_worker(records_for(source), tool), 5)
    assert result.state == "finished" and calls == ["real"]
    assert fake in result.stdout
    assert len(result.stdout.encode()) <= example["MAX_OUTPUT_BYTES"]
    assert len(result.stderr.encode()) <= example["MAX_OUTPUT_BYTES"]
    assert result.stderr.endswith("y\n")
    assert_reaped(workers)


@pytest.mark.parametrize("status", ["truncated", "cancelled", "disconnected"])
async def test_failed_provider_status_never_seals_final_compound(status, workers):
    entered = asyncio.Event()
    prefix = "await tool('early')\n"
    suite = "for value in (1, 2):\n    await tool(str(value))\n"
    calls = []

    async def tool(value):
        calls.append(value)
        entered.set()
        return "ok"

    async def records():
        yield {"type": "delta", "text": prefix}
        await entered.wait()
        yield {"type": "delta", "text": suite}
        if status != "disconnected":
            yield {"type": "done", "status": status, "source": prefix + suite}

    result = await asyncio.wait_for(run_worker(records(), tool), 5)
    assert result.state == "failed" and calls == ["early"]
    assert result.pending == suite
    assert ("SourceProtocolError" if status == "disconnected" else "SourceAborted") in result.error
    assert_reaped(workers)


async def test_parent_tool_exception_fails_unit_without_starting_later_units(workers):
    calls = []

    async def tool(value):
        calls.append(value)
        raise ValueError("fixture failure")

    result = await asyncio.wait_for(run_worker(records_for("await tool('bad')\nawait tool('later')\n"), tool), 5)
    assert result.state == "failed" and calls == ["bad"]
    assert result.failed_unit == "await tool('bad')\n" and result.units == ()
    assert "ValueError: fixture failure" in result.error
    assert_reaped(workers)


async def test_cpu_bound_child_ignoring_sigterm_is_hard_killed_and_reaped(workers, monkeypatch):
    buffers = []
    original = run_worker.__globals__["drain_output"]

    async def observe(reader, buffer):
        buffers.append(buffer)
        await original(reader, buffer)

    monkeypatch.setitem(run_worker.__globals__, "drain_output", observe)
    # The marker comes from inside the compound before its inner busy loop.
    # It proves the timeout killed executing code, not merely slow startup.
    source = "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True:\n    print('CPU_ENTERED', flush=True)\n    while True: pass\n"
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(run_worker(records_for(source), unused_tool, timeout=1), 5)
    assert any(b"CPU_ENTERED" in buffer for buffer in buffers)
    assert workers[0].returncode == -signal.SIGKILL
    assert_reaped(workers)


async def test_caller_cancellation_closes_source_tool_and_worker(workers):
    entered, source_closed, tool_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def records():
        try:
            yield {"type": "delta", "text": "await tool('waiting')\n"}
            await asyncio.Event().wait()
        finally:
            source_closed.set()

    async def tool(value):
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            tool_closed.set()

    task = asyncio.create_task(run_worker(records(), tool))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert source_closed.is_set() and tool_closed.is_set()
        assert_reaped(workers)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("origin", ["source", "tool"])
async def test_independent_child_task_cancellation_reaches_host_owner(origin, workers):
    entered = asyncio.Event()

    async def tool(value):
        entered.set()
        if origin == "tool":
            raise asyncio.CancelledError
        await asyncio.Event().wait()

    async def records():
        yield {"type": "delta", "text": "await tool('waiting')\n"}
        await entered.wait()
        if origin == "source":
            raise asyncio.CancelledError
        await asyncio.Event().wait()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run_worker(records(), tool), 5)
    assert_reaped(workers)


async def test_unexpected_worker_exit_is_not_successful_completion(workers):
    with pytest.raises(ExceptionGroup) as caught:
        await asyncio.wait_for(run_worker(records_for("import os\nos._exit(7)\n"), unused_tool), 5)
    assert any(isinstance(error, example["WorkerError"]) for error in caught.value.exceptions)
    assert workers[0].returncode == 7
    assert_reaped(workers)


async def test_inconsistent_final_source_is_rejected_before_pending_unit_runs(workers):
    async def records():
        yield {"type": "delta", "text": "await tool('pending')"}
        yield {"type": "done", "status": "completed", "source": "rewritten"}

    result = await asyncio.wait_for(run_worker(records(), unused_tool), 5)
    assert result.state == "failed" and result.units == ()
    assert "does not match" in result.error
    assert_reaped(workers)


async def test_second_cancellation_during_cleanup_cannot_detach_process_owner(workers, monkeypatch):
    entered, cleanup_entered, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = run_worker.__globals__["reap_process"]

    async def reap(process):
        cleanup_entered.set()
        await release_cleanup.wait()
        await original(process)

    async def tool(value):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setitem(run_worker.__globals__, "reap_process", reap)
    task = asyncio.create_task(run_worker(records_for("await tool('waiting')\n"), tool))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.wait_for(cleanup_entered.wait(), 2)
        task.cancel()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert_reaped(workers)
    finally:
        release_cleanup.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_process_group_cleanup_removes_descendant_after_worker_leader_exits(workers, monkeypatch):
    buffers = []
    original = run_worker.__globals__["drain_output"]

    async def observe(reader, buffer):
        buffers.append(buffer)
        await original(reader, buffer)

    monkeypatch.setitem(run_worker.__globals__, "drain_output", observe)
    source = "import subprocess, sys, os\nchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\nprint(f'DESCENDANT:{child.pid}', flush=True)\nos._exit(0)\n"
    with pytest.raises(ExceptionGroup):
        await asyncio.wait_for(run_worker(records_for(source), unused_tool), 5)
    output = b"".join(buffers).decode()
    child_pid = int(output.split("DESCENDANT:", 1)[1].splitlines()[0])
    # The descendant is reparented when its leader exits; allow the OS reaper
    # to remove its zombie before asserting that the whole owned group is gone.
    async with asyncio.timeout(2):
        while True:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.01)
    assert_reaped(workers)


async def test_source_cannot_swallow_cancellation_and_report_normal_eof(workers):
    entered = asyncio.Event()

    async def tool(value):
        entered.set()
        await asyncio.Event().wait()

    async def records():
        yield {"type": "delta", "text": "await tool('waiting')\n"}
        await entered.wait()
        asyncio.current_task().cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            return

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run_worker(records(), tool), 5)
    assert_reaped(workers)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
async def test_invalid_deadline_is_rejected_before_acquiring_process(timeout, workers):
    with pytest.raises(ValueError, match="finite and positive"):
        await run_worker(records_for("pass\n"), unused_tool, timeout=timeout)
    assert workers == []


@pytest.mark.parametrize("ending", ["finished", "timeout"])
async def test_escaped_descendant_holding_output_cannot_block_worker_cleanup(ending, workers):
    escaped = []
    entered = asyncio.Event()

    async def tool(value):
        escaped.append(int(value))
        entered.set()
        return "ready"

    # This separate-session process intentionally escapes host group ownership.
    # It has a finite fail-safe lifetime and its exact PID is cleaned below.
    source = "import subprocess, sys\nchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'], start_new_session=True)\nawait tool(str(child.pid))\n"
    if ending == "timeout":
        source += "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: pass\n"
    task = asyncio.create_task(run_worker(records_for(source), tool, timeout=0.5))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        done, _ = await asyncio.wait([task], timeout=1.25)
        assert done, "worker cleanup waited for an escaped descendant's pipe EOF"
        if ending == "timeout":
            with pytest.raises(TimeoutError):
                await task
            assert workers[0].returncode == -signal.SIGKILL
        else:
            assert (await task).state == "finished"
        # The host returned while the escaped process still owns its write ends.
        os.kill(escaped[0], 0)
        assert_reaped(workers)
    finally:
        for pid in escaped:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for pid in escaped:
            async with asyncio.timeout(2):
                while True:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    await asyncio.sleep(0.01)


async def test_second_socketpair_failure_closes_first_acquisition(workers, monkeypatch):
    socket_module = run_worker.__globals__["socket"]
    original = socket_module.socketpair
    acquired = []

    def acquire():
        if acquired:
            raise OSError("second socketpair failed")
        pair = original()
        acquired.extend(pair)
        return pair

    monkeypatch.setattr(socket_module, "socketpair", acquire)
    with pytest.raises(OSError, match="second socketpair"):
        await run_worker(records_for("pass\n"), unused_tool)
    assert workers == [] and len(acquired) == 2
    assert all(endpoint.fileno() == -1 for endpoint in acquired)


async def test_second_output_pipe_failure_closes_first_acquisition(workers, monkeypatch):
    original = run_worker.__globals__["output_pipe"]
    acquired = []

    async def acquire():
        if acquired:
            raise OSError("second output pipe failed")
        item = await original()
        acquired.append(item)
        return item

    monkeypatch.setitem(run_worker.__globals__, "output_pipe", acquire)
    with pytest.raises(OSError, match="second output pipe"):
        await run_worker(records_for("pass\n"), unused_tool)
    assert workers == []
    _, transport, write_fd = acquired[0]
    assert transport.is_closing()
    with pytest.raises(OSError):
        os.fstat(write_fd)


async def test_failure_result_retains_all_source_accepted_by_parser(workers):
    source = "completed = 1\nawait tool('bad')\nnever = 2\nif True:\n    await tool('pending')\n"

    async def tool(value):
        raise ValueError("fixture failure")

    result = await run_worker(records_for(source), tool)
    assert result.units == ("completed = 1\n",)
    assert result.failed_unit == "await tool('bad')\n"
    assert result.unexecuted == ("never = 2\n",)
    assert result.pending == "if True:\n    await tool('pending')\n"
    assert "".join((*result.units, result.failed_unit, *result.unexecuted, result.pending)) == source
    assert_reaped(workers)


async def test_buffering_receipt_does_not_claim_parser_accepted_queued_text(workers):
    entered, buffered = asyncio.Event(), asyncio.Event()
    first, later = "await tool('bad')\n", "never_accepted = 2\n"

    async def tool(value):
        entered.set()
        await buffered.wait()
        raise ValueError("fixture failure")

    async def records():
        yield {"type": "delta", "text": first}
        await entered.wait()
        yield {"type": "delta", "text": later}
        yield {"type": "done", "status": "completed", "source": first + later}

    result = await run_worker(records(), tool, on_received=lambda chars: buffered.set() if chars == len(first + later) else None)
    assert buffered.is_set() and result.failed_unit == first
    assert result.units == result.unexecuted == () and result.pending == ""
    assert_reaped(workers)


@pytest.mark.parametrize("patch", [{"state": "unknown"}, {"units": "abc"}, {"units": [3]}, {"unexecuted": "abc"}, {"pending": None}, {"failed_unit": 3}, {"error": []}, None])
async def test_malformed_terminal_record_raises_protocol_error(patch, workers, monkeypatch):
    record = {"type": "finished", "state": "finished", "units": [], "unexecuted": [], "pending": "", "failed_unit": None, "error": None}
    if patch is None:
        del record["units"]
    else:
        record.update(patch)

    async def malformed(reader):
        return record

    monkeypatch.setitem(run_worker.__globals__, "read_record", malformed)
    with pytest.raises(ExceptionGroup) as caught:
        await run_worker(records_for("pass\n"), unused_tool)
    assert len(caught.value.exceptions) == 1
    assert isinstance(caught.value.exceptions[0], example["WorkerError"])
    assert "invalid terminal" in str(caught.value.exceptions[0])
    assert_reaped(workers)


async def test_nonzero_teardown_exit_keeps_completed_execution_journal(workers):
    calls = []

    async def tool(value):
        calls.append(value)
        return "ok"

    source = "import atexit, os\natexit.register(os._exit, 7)\nawait tool('effect')\n"
    result = await run_worker(records_for(source), tool)
    assert result.state == "finished" and result.returncode == 7
    assert calls == ["effect"] and "".join(result.units) == source
    assert result.error is result.failed_unit is None and result.unexecuted == ()
    assert_reaped(workers)
