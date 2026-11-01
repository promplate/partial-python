"""POSIX-only CodeAct host fixture: one source stream, one fixed async tool.

Run: uv run examples/worker.py
Source records reuse transport.py. The worker owns bounded reception from
its source socket; a separate RPC socket prevents full-source-queue deadlocks.
stdout/stderr are bounded diagnostics, never the protocol. This is process
isolation for scheduling and termination, not a security sandbox or model eval.
Cleanup targets the owned process group. Descendants that create another session
can escape it; their inherited output pipes cannot delay this host indefinitely.
"""

import asyncio
import contextlib
import json
import math
import os
import signal
import socket
import sys
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from partial_python import Executor

if __package__:
    from .overlap import execute_buffered
    from .transport import MAX_RECORD_BYTES, closing_writer, record_bytes, source_deltas
else:
    from overlap import execute_buffered
    from transport import MAX_RECORD_BYTES, closing_writer, record_bytes, source_deltas

MAX_RECORDS = 128
MAX_OUTPUT_BYTES = 1 << 16
OUTPUT_DRAIN_GRACE = 0.1


class WorkerError(RuntimeError):
    """Worker exited unexpectedly or violated the example protocol."""


@dataclass(frozen=True)
class WorkerResult:
    """Parser ledger only: pending/unexecuted exclude text still in the receive queue.

    Inspect both state (execution) and returncode (worker teardown); a later
    process-exit failure does not erase already completed execution events.
    """

    state: str
    units: tuple[str, ...]
    unexecuted: tuple[str, ...]
    pending: str
    failed_unit: str | None
    error: str | None
    stdout: str
    stderr: str
    pid: int
    returncode: int


async def read_record(reader):
    try:
        line = await reader.readline()
        if not line or not line.endswith(b"\n") or len(line) > MAX_RECORD_BYTES:
            raise WorkerError("worker control channel closed or exceeded its frame limit")
        record = json.loads(line)
    except (ValueError, UnicodeError) as error:
        raise WorkerError("invalid worker control record") from error
    if not isinstance(record, dict):
        raise WorkerError("worker control record must be an object")
    return record


def validate_summary(record):
    required = {"state", "units", "unexecuted", "pending", "failed_unit", "error"}
    if (
        not required <= record.keys()
        or record["state"] not in ("finished", "failed", "aborted")
        or any(not isinstance(record[key], list) or any(not isinstance(unit, str) for unit in record[key]) for key in ("units", "unexecuted"))
        or not isinstance(record["pending"], str)
        or any(record[key] is not None and not isinstance(record[key], str) for key in ("failed_unit", "error"))
    ):
        raise WorkerError("invalid terminal worker record")


def write_record(writer, record):
    wire = record_bytes(record)
    if len(wire) > MAX_RECORD_BYTES:
        raise WorkerError("worker control record exceeded its frame limit")
    writer.write(wire)


async def drain_output(reader, buffer):
    while chunk := await reader.read(4096):
        buffer.extend(chunk)
        del buffer[:-MAX_OUTPUT_BYTES]


async def output_pipe():
    read_fd, write_fd = os.pipe()
    pipe = os.fdopen(read_fd, "rb", buffering=0)
    reader = asyncio.StreamReader()
    try:
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), pipe)
    except BaseException:
        pipe.close()
        os.close(write_fd)
        raise
    return reader, transport, write_fd


async def signal_group(pid, sig):
    for attempt in range(10):
        try:
            os.killpg(pid, sig)
            return
        except ProcessLookupError:
            return
        except PermissionError:
            # XNU skips zombie members, then returns EPERM if no live member
            # was signalled. Let the OS finish reaping; persistent denial still
            # raises. See apple-oss-distributions/xnu bsd/kern/kern_sig.c killpg1.
            if sys.platform != "darwin" or attempt == 9:
                raise
            await asyncio.sleep(0.01)


async def reap_process(process):
    # A completed worker may still be unwinding asyncio.run. A bounded grace
    # period avoids killing ordinary cleanup; a CPU loop cannot block the host.
    try:
        await asyncio.wait_for(process.wait(), 0.1)
    except TimeoutError:
        await signal_group(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 0.1)
        except TimeoutError:
            pass
    finally:
        # Kill the owned group even when its leader has exited: descendants may
        # otherwise retain output descriptors and prevent the drainers finishing.
        await signal_group(process.pid, signal.SIGKILL)
        await process.wait()


async def run_worker(records: AsyncIterable[dict], tool: Callable[[str], Awaitable[str]], *, timeout: float = 3, on_received: Callable[[int], None] | None = None) -> WorkerResult:
    """Run one source stream; caller tools and record sources must cooperate with cancellation.

    Only an explicit completed done record seals source. Timeout covers worker
    startup after process creation, source delivery, and execution; cleanup adds
    short, bounded process/output grace periods. Owned tasks and the worker are
    reaped; descendants outside its process group are outside this example's
    termination scope. Receipts acknowledge buffering, not parser acceptance.
    """
    if os.name != "posix":
        raise NotImplementedError("this example uses POSIX descriptor passing and process groups")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    process = None
    endpoints, writers, drainers, output_transports, output_fds = [], [], [], [], []
    stdout, stderr = bytearray(), bytearray()
    try:
        source_parent, source_child = socket.socketpair()
        endpoints.extend((source_parent, source_child))
        control_parent, control_child = socket.socketpair()
        endpoints.extend((control_parent, control_child))
        for buffer in (stdout, stderr):
            reader, transport, write_fd = await output_pipe()
            output_transports.append(transport)
            output_fds.append(write_fd)
            drainers.append(asyncio.create_task(drain_output(reader, buffer)))
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                str(Path(__file__).resolve()),
                "--worker",
                str(source_child.fileno()),
                str(control_child.fileno()),
                pass_fds=(source_child.fileno(), control_child.fileno()),
                start_new_session=True,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=output_fds[0],
                stderr=output_fds[1],
            )
        finally:
            source_child.close()
            control_child.close()
            while output_fds:
                os.close(output_fds.pop())
        async with asyncio.timeout(timeout):
            _, source_writer = await asyncio.open_connection(sock=source_parent, limit=MAX_RECORD_BYTES)
            writers.append(source_writer)
            control_reader, control_writer = await asyncio.open_connection(sock=control_parent, limit=MAX_RECORD_BYTES)
            writers.append(control_writer)

            async def send_source():
                iterator = aiter(records)
                try:
                    count = 0
                    async for record in iterator:
                        if asyncio.current_task().cancelling():
                            raise asyncio.CancelledError
                        count += 1
                        if count > MAX_RECORDS:
                            raise WorkerError("example source record limit exceeded")
                        write_record(source_writer, record)
                        await source_writer.drain()
                    if asyncio.current_task().cancelling():
                        raise asyncio.CancelledError
                    source_writer.write_eof()
                finally:
                    close = getattr(iterator, "aclose", None)
                    if close is not None:
                        await close()

            async def call_tool(record):
                try:
                    value = await tool(record["value"])
                    if not isinstance(value, str):
                        raise TypeError("the example tool must return a string")
                    reply = {"type": "result", "id": record["id"], "value": value}
                except Exception as error:  # noqa: BLE001 - errors cross a process boundary as text, never pickle.
                    reply = {"type": "error", "id": record["id"], "message": f"{type(error).__name__}: {error}"[:4096]}
                if asyncio.current_task().cancelling():
                    raise asyncio.CancelledError
                write_record(control_writer, reply)
                await control_writer.drain()

            owner = asyncio.current_task()
            active = True

            def propagate_cancellation(task):
                if task.cancelled() and active:
                    owner.cancel()

            async with asyncio.TaskGroup() as tasks:
                source_task = tasks.create_task(send_source())
                source_task.add_done_callback(propagate_cancellation)
                calls = set()
                last_id = 0
                try:
                    while True:
                        record = await read_record(control_reader)
                        kind = record.get("type")
                        if kind == "call":
                            if type(record.get("id")) is not int or record["id"] != last_id + 1 or not isinstance(record.get("value"), str):
                                raise WorkerError("invalid fixed-tool request")
                            last_id += 1
                            task = tasks.create_task(call_tool(record))
                            calls.add(task)
                            task.add_done_callback(calls.discard)
                            task.add_done_callback(propagate_cancellation)
                        elif kind == "received":
                            if type(record.get("chars")) is not int or record["chars"] < 0:
                                raise WorkerError("invalid source receipt")
                            if on_received is not None:
                                on_received(record["chars"])
                        elif kind == "finished":
                            validate_summary(record)
                            summary = record
                            break
                        else:
                            raise WorkerError("unknown worker control record")
                finally:
                    # Receiving final status closes the dispatch gate before any
                    # cleanup await; user-created tasks cannot extend this run.
                    active = False
                    source_task.cancel()
                    for task in calls:
                        task.cancel()
    finally:

        async def cleanup():
            for writer in writers:
                writer.transport.abort()
            for endpoint in endpoints:
                endpoint.close()
            while output_fds:
                os.close(output_fds.pop())
            try:
                if process is not None:
                    await reap_process(process)
            finally:
                # An escaped descendant can hold inherited write ends open.
                # Output is best-effort after this grace; closing our public
                # read transports also releases descriptors, not just tasks.
                if drainers:
                    await asyncio.wait(drainers, timeout=OUTPUT_DRAIN_GRACE)
                for transport in output_transports:
                    transport.close()
                for drainer in drainers:
                    drainer.cancel()
                await asyncio.gather(*drainers, return_exceptions=True)
                for writer in writers:
                    with contextlib.suppress(OSError):
                        await writer.wait_closed()

        # A second cancellation must not detach the only owner of the process.
        cleanup_task = asyncio.create_task(cleanup())
        interrupted = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                interrupted = True
        await cleanup_task
        if interrupted:
            raise asyncio.CancelledError
    return WorkerResult(
        summary["state"],
        tuple(summary["units"]),
        tuple(summary["unexecuted"]),
        summary["pending"],
        summary["failed_unit"],
        summary["error"],
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
        process.pid,
        process.returncode,
    )


def describe_error(error):
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(describe_error(child) for child in error.exceptions)[:4096]
    return f"{type(error).__name__}: {error}"[:4096]


async def worker_main(source_fd, control_fd):
    source_reader, source_writer = await asyncio.open_connection(sock=socket.socket(fileno=source_fd), limit=MAX_RECORD_BYTES)
    control_reader, control_writer = await asyncio.open_connection(sock=socket.socket(fileno=control_fd), limit=MAX_RECORD_BYTES)
    owner = asyncio.current_task()
    tool_lock = asyncio.Lock()
    call_id = 0

    async def remote_tool(value):
        nonlocal call_id
        try:
            async with tool_lock:
                call_id += 1
                write_record(control_writer, {"type": "call", "id": call_id, "value": value})
                await control_writer.drain()
                reply = await read_record(control_reader)
                if reply.get("id") != call_id:
                    raise WorkerError("tool response does not match its request")
                if reply.get("type") == "error":
                    raise WorkerError(reply["message"])
                if reply.get("type") != "result" or not isinstance(reply.get("value"), str):
                    raise WorkerError("invalid tool result")
                return reply["value"]
        except asyncio.CancelledError:
            # This deliberately narrow host has no per-call cancellation/retry
            # protocol: cancelling any remote call terminates the whole run.
            if asyncio.current_task() is not owner:
                owner.cancel()
            raise

    received = 0

    def source_received(text, depth):
        nonlocal received
        received += len(text)
        # The host caps total records, so these tiny receipt frames are bounded.
        write_record(control_writer, {"type": "received", "chars": received})

    executor = Executor({"__name__": "__main__", "tool": remote_tool}, filename="<partial-python-worker>")
    error_text = None
    try:
        await execute_buffered(source_deltas(source_reader), executor, capacity=2, on_buffered=source_received)
    except BaseException as error:  # noqa: BLE001 - even cancellation must report a terminal worker state.
        error_text = describe_error(error)
    async with closing_writer(control_writer):
        try:
            write_record(
                control_writer,
                {
                    "type": "finished",
                    "state": executor.state,
                    "units": [event.unit.source for event in executor.events],
                    "unexecuted": [unit.source for unit in executor.unexecuted],
                    "pending": executor.parser.pending,
                    "failed_unit": executor.failed_unit.source if executor.failed_unit else None,
                    "error": error_text,
                },
            )
            await control_writer.drain()
        finally:
            source_writer.transport.abort()


async def main():
    first = "reply = await tool('first')\n"
    later = "print('ordinary stdout'); await tool(reply)\n"
    entered, received_later = asyncio.Event(), asyncio.Event()
    calls = []

    async def tool(value):
        calls.append(value)
        if value == "first":
            entered.set()
            await received_later.wait()
        return "parent result"

    async def records():
        yield {"type": "delta", "text": first}
        await entered.wait()
        yield {"type": "delta", "text": later}
        yield {"type": "done", "status": "completed", "source": first + later}

    result = await run_worker(records(), tool, on_received=lambda chars: received_later.set() if chars == len(first + later) else None)
    assert result.state == "finished" and calls == ["first", "parent result"]
    print(f"Worker {result.pid} exited {result.returncode}; calls={calls}; stdout={result.stdout.strip()!r}")
    print("Verified cross-process tool await, concurrent source reception, and ordered execution; this is a protocol fixture.")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        asyncio.run(worker_main(int(sys.argv[2]), int(sys.argv[3])))
    else:
        asyncio.run(main())
