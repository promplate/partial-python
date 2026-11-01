"""Deterministic transport fixtures; no model calls, replays, or quality claims."""

import asyncio
import runpy
from pathlib import Path

import pytest

from partial_python import incremental_exec

example = runpy.run_path(str(Path(__file__).parents[1] / "examples" / "transport.py"))
source_deltas = example["source_deltas"]
record_bytes = example["record_bytes"]
execute_connection = example["execute_connection"]
SourceProtocolError = example["SourceProtocolError"]
SourceAborted = example["SourceAborted"]


def reader_for(*records):
    reader = asyncio.StreamReader(limit=example["MAX_RECORD_BYTES"])
    for record in records:
        reader.feed_data(record if isinstance(record, bytes) else record_bytes(record))
    reader.feed_eof()
    return reader


@pytest.mark.parametrize("status", ["completed", "truncated", "cancelled", "disconnected"])
async def test_loopback_starts_tool_before_final_source_and_respects_status(status):
    calls = await asyncio.wait_for(example["run_scenario"](status), 5)
    assert calls == (["early", 1, 2] if status == "completed" else ["early"])


async def test_successful_terminal_record_seals_final_call_without_newline():
    source = "calls.append(1)"
    reader = reader_for({"type": "delta", "text": source}, {"type": "done", "status": "completed", "source": source})
    calls = []
    events = [event async for event in incremental_exec(source_deltas(reader), {"calls": calls})]
    assert calls == [1]
    assert len(events) == 1


@pytest.mark.parametrize("status", ["truncated", "cancelled", "failed"])
async def test_aborted_terminal_status_never_seals_pending_compound(status):
    source = "calls.append('early')\nfor value in (1, 2):\n    calls.append(value)\n"
    reader = reader_for({"type": "delta", "text": source}, {"type": "done", "status": status})
    calls = []
    with pytest.raises(SourceAborted) as captured:
        async for _ in incremental_exec(source_deltas(reader), {"calls": calls}):
            pass
    assert captured.value.status == status
    assert calls == ["early"]


async def test_bare_socket_eof_is_failure_even_when_pending_source_is_valid():
    reader = reader_for({"type": "delta", "text": "calls.append(1)"})
    calls = []
    with pytest.raises(SourceProtocolError, match="without a completed"):
        async for _ in incremental_exec(source_deltas(reader), {"calls": calls}):
            pass
    assert calls == []


async def test_final_source_rewrite_rejected_before_sealing_pending_suite():
    source = "calls.append('early')\nif True:\n    calls.append('pending')\n"
    reader = reader_for({"type": "delta", "text": source}, {"type": "done", "status": "completed", "source": source + "# rewritten"})
    calls = []
    with pytest.raises(SourceProtocolError, match="does not match"):
        async for _ in incremental_exec(source_deltas(reader), {"calls": calls}):
            pass
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
    reader = asyncio.StreamReader(limit=example["MAX_RECORD_BYTES"])

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
    reader = reader_for({"type": "delta", "text": "x" * (example["MAX_SOURCE_CHARS"] + 1)})
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
