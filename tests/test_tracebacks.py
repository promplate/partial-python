"""Source-file lifetime and original traceback locations."""

import asyncio
import traceback

import pytest

from examples import tracebacks as example
from examples.tracebacks import record_and_execute
from partial_python import Executor


async def test_later_function_keeps_real_source_after_executor_and_writer_finish():
    result = await example.main()
    assert result["state"] == "finished"
    assert result["stored"] == result["source"].encode("utf-8")
    assert [(frame.lineno, frame.name, frame.line) for frame in result["frames"]] == [(4, "fail_later", "raise RuntimeError(label)")]
    assert "RuntimeError: 你好" in result["traceback"]


async def test_top_level_error_after_await_has_original_unicode_crlf_source(tmp_path):
    filename = tmp_path / "response.py"
    prefix = "label = '你好'\r\nawait pause()\r\n"
    tail = "raise RuntimeError('错误')\r\n"
    awaited = []

    async def pause():
        assert filename.read_bytes() == prefix.encode("utf-8")
        await asyncio.sleep(0)
        awaited.append(True)

    async def chunks():
        yield prefix[:-1]  # Split CRLF across deltas without normalizing the file.
        yield prefix[-1:]
        assert awaited == [True]
        yield tail

    executor = Executor({"pause": pause}, filename=str(filename))
    with filename.open("w", encoding="utf-8", newline="") as source_file, pytest.raises(RuntimeError, match="错误") as caught:
        await record_and_execute(executor, source_file, chunks())
    frames = [frame for frame in traceback.extract_tb(caught.value.__traceback__) if frame.filename == str(filename)]
    assert [(frame.lineno, frame.line) for frame in frames] == [(3, "raise RuntimeError('错误')")]
    assert filename.read_bytes() == (prefix + tail).encode("utf-8")
    assert executor.state == "failed" and not executor.parser_state.closed


@pytest.mark.parametrize("swallowed_cancellation", [False, True])
async def test_input_failure_aborts_instead_of_finishing_pending_suite(tmp_path, swallowed_cancellation):
    filename = tmp_path / "response.py"
    prefix, suite = "calls.append('early')\r\n", "for value in (1, 2):\r\n    calls.append(value)\r\n"
    error = OSError("fixture disconnected")
    calls = []

    async def chunks():
        yield prefix
        yield suite
        if swallowed_cancellation:
            asyncio.current_task().cancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                return
        raise error

    executor = Executor({"calls": calls}, filename=str(filename))
    with filename.open("w", encoding="utf-8", newline="") as source_file, pytest.raises(asyncio.CancelledError if swallowed_cancellation else OSError) as caught:
        await record_and_execute(executor, source_file, chunks())
    if not swallowed_cancellation:
        assert caught.value is error
    assert calls == ["early"]
    assert executor.state == "aborted" and not executor.parser_state.closed
    assert executor.parser_state.pending == suite
    assert filename.read_bytes() == (prefix + suite).encode("utf-8")
