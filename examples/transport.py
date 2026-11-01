"""Example-only NDJSON transport for partial-python; no model evaluation is performed.

Run: uv run examples/transport.py
A delta record carries raw Python source. Only a completed done record whose
source exactly matches the received deltas declares EOF to incremental_exec.
One connection carries one logical source stream; there is no retry or replay.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from time import monotonic

from partial_python import Execution, incremental_exec

MAX_RECORD_BYTES = 1 << 20
MAX_SOURCE_CHARS = 1 << 16


class SourceProtocolError(ValueError):
    """Malformed, truncated, or inconsistent source transport."""


class SourceAborted(RuntimeError):
    def __init__(self, status: str):
        self.status = status
        super().__init__(f"source ended with status {status!r}; pending Python was not finalized")


def record_bytes(record: dict) -> bytes:
    return (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")


async def source_deltas(reader: asyncio.StreamReader) -> AsyncIterator[str]:
    """Validate this example protocol before exposing text or successful EOF.

    The caller supplies a StreamReader with limit=MAX_RECORD_BYTES. Returning
    normally is the adapter's sole authority to finalize the source stream.
    """
    parts = []
    size = 0
    while True:
        try:
            line = await reader.readline()
        except ValueError as error:
            raise SourceProtocolError("transport record exceeded the reader limit") from error
        if not line:
            raise SourceProtocolError("connection ended without a completed done record")
        if not line.endswith(b"\n"):
            raise SourceProtocolError("connection ended inside a transport record")
        if len(line) > MAX_RECORD_BYTES:
            raise SourceProtocolError("transport record is too large")
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise SourceProtocolError("transport record is not valid UTF-8 JSON") from error
        if not isinstance(record, dict):
            raise SourceProtocolError("transport record must be an object")
        if record.get("type") == "delta":
            text = record.get("text")
            if not isinstance(text, str):
                raise SourceProtocolError("delta text must be a string")
            size += len(text)
            if size > MAX_SOURCE_CHARS:
                raise SourceProtocolError("source exceeded the example's size limit")
            if text:
                parts.append(text)
                yield text
        elif record.get("type") == "done":
            status = record.get("status")
            if status in ("truncated", "cancelled", "failed"):
                raise SourceAborted(status)
            if status != "completed":
                raise SourceProtocolError("done status must explicitly describe successful completion or failure")
            if not isinstance(record.get("source"), str) or record["source"] != "".join(parts):
                raise SourceProtocolError("final source does not match the received text deltas")
            return
        else:
            raise SourceProtocolError("unknown transport record type")


async def execute_connection(host: str, port: int, namespace: dict) -> AsyncIterator[Execution]:
    """Own a socket for one source stream and close it when consumption stops."""
    reader, writer = await asyncio.open_connection(host, port, limit=MAX_RECORD_BYTES)
    execution = incremental_exec(source_deltas(reader), namespace)
    try:
        async for event in execution:
            yield event
    finally:
        try:
            await execution.aclose()
        finally:
            writer.close()
            await writer.wait_closed()


async def run_scenario(status: str):
    started_at = monotonic()
    first_ran = asyncio.Event()
    terminal_sent = False
    calls = []
    terminal = None
    prefix = "await tool('early')\n"
    suite = "for value in (1, 2):\n    await tool(value)\n"
    source = prefix + suite
    server_finished = asyncio.get_running_loop().create_future()

    async def tool(value):
        if value == "early":
            assert not terminal_sent
            first_ran.set()
        else:
            assert terminal_sent and status == "completed"
        calls.append(value)
        print(f"{status:9} {monotonic() - started_at:5.3f}s tool({value!r})")

    async def serve(reader, writer):
        nonlocal terminal_sent
        try:
            writer.write(record_bytes({"type": "delta", "text": prefix}))
            await writer.drain()
            # A handshake proves execution precedes final source delivery, without timing guesses.
            await asyncio.wait_for(first_ran.wait(), 2)
            wire = record_bytes({"type": "delta", "text": suite})
            for offset in range(0, len(wire), 3):
                writer.write(wire[offset : offset + 3])
                await writer.drain()
                await asyncio.sleep(0)
            if status != "disconnected":
                terminal_sent = True
                writer.write(record_bytes({"type": "done", "status": status, "source": source}))
                await writer.drain()
        except BaseException as error:  # noqa: BLE001 - the owning scenario must observe handler failure or cancellation.
            if not server_finished.done():
                server_finished.set_exception(error)
        finally:
            writer.close()
            await writer.wait_closed()
            if not server_finished.done():
                server_finished.set_result(None)

    server = await asyncio.start_server(serve, "127.0.0.1", 0, limit=MAX_RECORD_BYTES)
    async with server:
        port = server.sockets[0].getsockname()[1]
        try:
            async for _ in execute_connection("127.0.0.1", port, {"tool": tool}):
                pass
        except (SourceAborted, SourceProtocolError) as error:
            terminal = error
            print(f"{status:9} {monotonic() - started_at:5.3f}s {error}")
        await asyncio.wait_for(server_finished, 2)
    assert calls == (["early", 1, 2] if status == "completed" else ["early"])
    assert (terminal is None) == (status == "completed")
    return calls


async def main():
    for status in ("completed", "truncated", "cancelled", "disconnected"):
        await run_scenario(status)
    print("Verified loopback transport: early execution and explicit completion; failed streams never flush the final suite.")


if __name__ == "__main__":
    asyncio.run(main())
