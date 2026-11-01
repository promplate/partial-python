"""Example-only bounded reception while sequential Python execution is awaiting.

Run: uv run examples/overlap.py
This is a deterministic loopback protocol fixture, not a model evaluation.
The core stays pull-based; this host explicitly owns a producer task and a
bounded queue. TaskGroup preserves observed producer failures while cancelling
execution. A full queue also pauses reading later provider status records.
"""

import asyncio
import sys
from collections.abc import AsyncIterable, Callable

from partial_python import Execution, Executor


async def execute_buffered(chunks: AsyncIterable[str], executor: Executor, *, capacity: int = 2, on_buffered: Callable[[str, int], None] | None = None) -> tuple[Execution, ...]:
    """Own reception and execution together; one queued item is one source delta.

    At most capacity deltas are queued, plus one producer-local delta waiting to
    enter the queue. Transport/frame limits still bound individual delta sizes.
    Only normal, validated source exhaustion sends the completion sentinel.
    """
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
        raise ValueError("capacity must be a positive integer")
    queue = asyncio.Queue(capacity)
    eof = object()
    events = []
    owner = asyncio.current_task()
    consuming = True

    async def produce():
        task = asyncio.current_task()
        iterator = aiter(chunks)
        try:
            async for delta in iterator:
                if task is not None and task.cancelling():
                    raise asyncio.CancelledError
                await queue.put(delta)
                if on_buffered is not None:
                    on_buffered(delta, queue.qsize())
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError
        await queue.put(eof)

    def producer_done(task):
        # TaskGroup ignores child cancellation; propagate an independently
        # cancelled source without re-cancelling the owner during its cleanup.
        if task.cancelled() and consuming and owner is not None:
            owner.cancel()

    async def queued_source():
        while True:
            item = await queue.get()
            if item is eof:
                return
            yield item

    async def consume():
        async for event in executor.stream(queued_source()):
            events.append(event)

    # An observed producer failure cancels the running unit. Consumption
    # stays in the owning task so a cancelled tool also closes reception.
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(produce()).add_done_callback(producer_done)
        try:
            await consume()
        finally:
            consuming = False
    return tuple(events)


async def run_scenario(status: str):
    # This sibling owns the example wire protocol; do not implement it twice.
    if __package__:
        from .transport import MAX_RECORD_BYTES, SourceAborted, SourceProtocolError, close_writer, record_bytes, source_deltas
    else:
        from transport import MAX_RECORD_BYTES, SourceAborted, SourceProtocolError, close_writer, record_bytes, source_deltas

    entered = asyncio.Event()
    later_buffered = asyncio.Event()
    server_finished = asyncio.get_running_loop().create_future()
    calls = []
    overlapped = False
    final_sent = False
    first = "await tool('early')\n"
    later = "await tool('later')\n"
    executor = Executor()

    async def tool(value):
        if value == "early":
            assert not final_sent
            calls.append("early started")
            entered.set()
            if status == "completed":
                await later_buffered.wait()
                calls.append("early finished")
            else:
                await asyncio.Event().wait()
        else:
            assert calls == ["early started", "early finished"]
            calls.append("later")

    def buffered(text, depth):
        nonlocal overlapped
        assert depth <= 2
        if text == later:
            assert entered.is_set() and calls == ["early started"]
            overlapped = True
            later_buffered.set()
            print(f"{status}: later source ingested while first tool is still awaiting")

    async def serve(reader, writer):
        nonlocal final_sent
        try:
            try:
                writer.write(record_bytes({"type": "delta", "text": first}))
                await writer.drain()
                await asyncio.wait_for(entered.wait(), 2)
                writer.write(record_bytes({"type": "delta", "text": later}))
                if status != "disconnected":
                    final_sent = True
                    writer.write(record_bytes({"type": "done", "status": status, "source": first + later}))
                await writer.drain()
            finally:
                await close_writer(writer, sys.exception())
        except BaseException as error:  # noqa: BLE001 - the scenario must observe callback and cleanup failures.
            if not server_finished.done():
                server_finished.set_exception(error)
        else:
            if not server_finished.done():
                server_finished.set_result(None)

    executor.namespace["tool"] = tool
    server = await asyncio.start_server(serve, "127.0.0.1", 0, limit=MAX_RECORD_BYTES)
    terminal = None
    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1], limit=MAX_RECORD_BYTES)
        try:
            try:
                await execute_buffered(source_deltas(reader), executor, on_buffered=buffered)
            except* (SourceAborted, SourceProtocolError) as errors:
                terminal = errors
        finally:
            await close_writer(writer, sys.exception())
        await asyncio.wait_for(server_finished, 2)
    assert overlapped
    if status == "completed":
        assert terminal is None and calls == ["early started", "early finished", "later"]
        assert executor.state == "finished"
    else:
        assert terminal is not None and calls == ["early started"]
        assert executor.state == "failed"
        assert isinstance(executor.error, asyncio.CancelledError)
    print(f"{status}: {calls}; executor={executor.state}")


async def main():
    for status in ("completed", "truncated", "cancelled", "disconnected"):
        await run_scenario(status)
    print("Verified bounded concurrent reception, sequential execution, and cancellation on producer failure.")


if __name__ == "__main__":
    asyncio.run(main())
