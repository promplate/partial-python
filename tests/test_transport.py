"""Transport completion, framing, and socket cleanup."""

import asyncio

import pytest

from examples import overlap
from examples import transport as example
from examples.transport import SourceAborted, SourceProtocolError, execute_connection, record_bytes, source_deltas
from partial_python import MODULE, Executor, incremental_exec


def reader_for(*records):
    reader = asyncio.StreamReader(limit=example.MAX_RECORD_BYTES)
    for record in records:
        reader.feed_data(record if isinstance(record, bytes) else record_bytes(record))
    reader.feed_eof()
    return reader


@pytest.mark.parametrize("status", ["completed", "truncated", "cancelled", "disconnected"])
async def test_loopback_starts_tool_before_final_source_and_respects_status(status):
    calls = await asyncio.wait_for(example.run_scenario(status), 5)
    assert calls == (["early", 1, 2] if status == "completed" else ["early"])


async def test_successful_terminal_record_seals_final_call_without_newline():
    source = "calls.append(1)"
    reader = reader_for({"type": "delta", "text": source}, {"type": "done", "status": "completed", "source": source})
    calls = []
    events = [event async for event in Executor({"calls": calls}).stream(source_deltas(reader))]
    assert calls == [1]
    assert len(events) == 1


@pytest.mark.parametrize("status", ["truncated", "cancelled", "failed"])
async def test_aborted_terminal_status_never_seals_pending_compound(status):
    source = "calls.append('early')\nfor value in (1, 2):\n    calls.append(value)\n"
    reader = reader_for({"type": "delta", "text": source}, {"type": "done", "status": status})
    calls = []
    with pytest.raises(SourceAborted) as captured:
        await incremental_exec(source_deltas(reader), {"calls": calls})
    assert captured.value.status == status
    assert calls == ["early"]


async def test_bare_socket_eof_is_failure_even_when_pending_source_is_valid():
    reader = reader_for({"type": "delta", "text": "calls.append(1)"})
    calls = []
    with pytest.raises(SourceProtocolError, match="without a completed"):
        await incremental_exec(source_deltas(reader), {"calls": calls})
    assert calls == []


async def test_final_source_rewrite_rejected_before_sealing_pending_suite():
    source = "calls.append('early')\nif True:\n    calls.append('pending')\n"
    reader = reader_for({"type": "delta", "text": source}, {"type": "done", "status": "completed", "source": source + "# rewritten"})
    calls = []
    with pytest.raises(SourceProtocolError, match="does not match"):
        await incremental_exec(source_deltas(reader), {"calls": calls}, stream_blocks=MODULE)
    # A final consistency check cannot roll back the previously committed call.
    assert calls == ["early"]


@pytest.mark.parametrize(
    "record",
    [b"not json\n", b"\xff\n", b"[]\n", {"type": "unknown"}, {"type": "delta", "text": 42}, {"type": "done"}, {"type": "done", "status": "completed"}, {"type": "done", "status": "length"}],
)
async def test_invalid_records_never_become_source_or_successful_eof(record):
    with pytest.raises(SourceProtocolError):
        async for _ in source_deltas(reader_for(record)):
            pytest.fail("invalid record became a source delta")


async def test_partial_wire_record_does_not_become_normal_eof():
    reader = reader_for(b'{"type": "done", "status": "completed", "source": ""}')
    with pytest.raises(SourceProtocolError, match="inside a transport record"):
        async for _ in source_deltas(reader):
            pass


async def test_unicode_and_escaped_python_text_survive_fragmented_bytes():
    source = 'message = "你好\\nworld"\n'
    wire = record_bytes({"type": "delta", "text": source}) + record_bytes({"type": "done", "status": "completed", "source": source})
    reader = asyncio.StreamReader(limit=example.MAX_RECORD_BYTES)

    async def produce():
        for value in wire:
            reader.feed_data(bytes([value]))
            await asyncio.sleep(0)
        reader.feed_eof()

    producer = asyncio.create_task(produce())
    try:
        assert [delta async for delta in source_deltas(reader)] == [source]
        await producer
    finally:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)


async def test_frame_reader_limit_is_reported_as_protocol_error():
    reader = asyncio.StreamReader(limit=8)
    reader.feed_data(b'{"type": "delta", "text": "long"}\n')
    reader.feed_eof()
    with pytest.raises(SourceProtocolError, match="reader limit"):
        async for _ in source_deltas(reader):
            pass


async def test_source_size_limit_applies_before_yielding_new_text():
    reader = reader_for({"type": "delta", "text": "x" * (example.MAX_SOURCE_CHARS + 1)})
    with pytest.raises(SourceProtocolError, match="size limit"):
        async for _ in source_deltas(reader):
            pytest.fail("oversized source was emitted")


async def test_valid_empty_stream_requires_explicit_completion():
    reader = reader_for({"type": "done", "status": "completed", "source": ""})
    assert [delta async for delta in source_deltas(reader)] == []


async def test_closing_network_consumer_closes_socket_without_executing_queue():
    disconnected = asyncio.Event()
    calls = []
    handlers = set()

    async def serve(reader, writer):
        try:
            writer.write(record_bytes({"type": "delta", "text": "calls.append(1)\ncalls.append(2)\n"}))
            await writer.drain()
            if await reader.read() == b"":
                disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()

    def accept(reader, writer):
        task = asyncio.create_task(serve(reader, writer))
        handlers.add(task)
        task.add_done_callback(handlers.discard)

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    try:
        async with server:
            stream = execute_connection("127.0.0.1", server.sockets[0].getsockname()[1], {"calls": calls})
            try:
                await anext(stream)
                assert calls == [1]
            finally:
                await stream.aclose()
            await asyncio.wait_for(disconnected.wait(), 1)
            assert calls == [1]
    finally:
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)


async def test_empty_deltas_do_not_accumulate_unbounded_source_history():
    reader = reader_for(*[{"type": "delta", "text": ""}] * 1000, {"type": "done", "status": "completed", "source": ""})
    assert [delta async for delta in source_deltas(reader)] == []


class CloseFailureWriter:
    def __init__(self, error):
        self.error = error
        self.closed = False

    def close(self):
        self.closed = True

    async def wait_closed(self):
        raise self.error


@pytest.mark.parametrize("failed", [False, True])
async def test_socket_close_oserror_preserves_only_existing_tool_failure(monkeypatch, failed):
    primary, closing = ValueError("tool failed"), OSError("socket close failed")
    writer = CloseFailureWriter(closing)
    source = "await tool()\n"
    reader = reader_for({"type": "delta", "text": source}, {"type": "done", "status": "completed", "source": source})
    calls = []

    async def tool():
        calls.append("tool")
        if failed:
            raise primary

    async def connect(*args, **kwargs):
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", connect)
    with pytest.raises(ValueError if failed else OSError) as caught:
        async for _ in execute_connection("fixture", 0, {"tool": tool}):
            pass
    assert caught.value is (primary if failed else closing)
    assert writer.closed and calls == ["tool"]
    if failed:
        assert any("socket close failed" in note for note in primary.__notes__)


class CleanupInterrupt(BaseException):
    pass


@pytest.mark.parametrize("error_type", [asyncio.CancelledError, CleanupInterrupt])
@pytest.mark.parametrize("failed", [False, True])
async def test_socket_close_new_cancellation_or_interrupt_propagates(monkeypatch, error_type, failed):
    primary, closing = ValueError("tool failed"), error_type("close interrupted")
    writer = CloseFailureWriter(closing)
    source = "raise failure\n" if failed else ""
    reader = reader_for({"type": "delta", "text": source}, {"type": "done", "status": "completed", "source": source})

    async def connect(*args, **kwargs):
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", connect)
    with pytest.raises(error_type) as caught:
        async for _ in execute_connection("fixture", 0, {"failure": primary}):
            pass
    assert caught.value is closing and writer.closed
    if failed:
        assert closing.__context__ is primary


async def test_consumer_close_reports_socket_failure_without_executing_later_units(monkeypatch):
    closing = OSError("socket close failed")
    writer = CloseFailureWriter(closing)
    reader = reader_for({"type": "delta", "text": "calls.append(1)\ncalls.append(2)\n"})
    calls = []

    async def connect(*args, **kwargs):
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", connect)
    stream = execute_connection("fixture", 0, {"calls": calls})
    await anext(stream)
    with pytest.raises(OSError) as caught:
        await stream.aclose()
    assert caught.value is closing and writer.closed and calls == [1]


@pytest.mark.parametrize("scenario", ["transport", "overlap"])
async def test_server_close_failure_reaches_scenario_instead_of_timing_out(monkeypatch, scenario):
    run_scenario = example.run_scenario if scenario == "transport" else overlap.run_scenario
    start_server = asyncio.start_server
    closing = OSError("server socket close failed")

    class ServerWriter:
        def __init__(self, writer):
            self.writer = writer

        def __getattr__(self, name):
            return getattr(self.writer, name)

        async def wait_closed(self):
            await self.writer.wait_closed()
            raise closing

    async def start(callback, *args, **kwargs):
        async def connected(reader, writer):
            await callback(reader, ServerWriter(writer))

        return await start_server(connected, *args, **kwargs)

    monkeypatch.setattr(asyncio, "start_server", start)
    with pytest.raises(OSError) as caught:
        await asyncio.wait_for(run_scenario("completed"), 3)
    assert caught.value is closing


@pytest.mark.parametrize("failed", [False, True])
async def test_socket_close_never_uses_callers_handled_exception(monkeypatch, failed):
    unrelated, primary, closing = ValueError("caller already handled this"), ValueError("this tool failed"), OSError("socket close failed")
    writer = CloseFailureWriter(closing)
    source = "raise failure\n" if failed else ""
    reader = reader_for({"type": "delta", "text": source}, {"type": "done", "status": "completed", "source": source})

    async def connect(*args, **kwargs):
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", connect)
    try:
        raise unrelated
    except ValueError:
        with pytest.raises(ValueError if failed else OSError) as caught:
            async for _ in execute_connection("fixture", 0, {"failure": primary}):
                pass
    assert caught.value is (primary if failed else closing)
    assert not getattr(unrelated, "__notes__", ())
    if failed:
        assert any("socket close failed" in note for note in primary.__notes__)
