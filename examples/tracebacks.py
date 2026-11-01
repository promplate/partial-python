"""Retain original source in a host-owned file for ordinary Python tracebacks.

Run: uv run examples/tracebacks.py
Use a fresh filename for each source stream. Keep its file alive while functions
or tracebacks from that stream can still be used; finishing execution is not the
end of the file's lifetime. This UTF-8 recipe requires an absent or UTF-8 source
encoding cookie: compile(str) ignores cookies, but traceback readers honor them.
This fixture does not register a global source cache.
"""

import asyncio
import tempfile
import traceback
from contextlib import aclosing
from pathlib import Path

from partial_python import Executor


async def record_and_execute(executor, source_file, chunks):
    """The host owns the file/namespace and must close its acquired input iterator.

    This helper consumes the input but does not close it; use caller-owned
    ``aclosing`` when the iterator provides ``aclose``.
    """
    task = asyncio.current_task()
    cancellations = task.cancelling() if task is not None else 0
    try:
        async for delta in chunks:
            if task is not None and task.cancelling() > cancellations:
                raise asyncio.CancelledError
            # Publish the original characters before code can fail or be inspected.
            # flush(), not fsync(), is enough for another reader in this process.
            source_file.write(delta)
            source_file.flush()
            await executor.feed(delta)
        # A source may suppress cancellation and return; that is not valid EOF.
        if task is not None and task.cancelling() > cancellations:
            raise asyncio.CancelledError
    except BaseException:
        executor.abort()
        raise
    else:
        await executor.finish()  # Only normal source exhaustion authorizes EOF.


async def main():
    source = "label = '你好'\r\nasync def fail_later():\r\n    await pause()\r\n    raise RuntimeError(label)\r\n"
    awaited = []

    async def pause():
        await asyncio.sleep(0)
        awaited.append(True)

    async def chunks():
        for offset in range(0, len(source), 7):
            yield source[offset : offset + 7]

    with tempfile.TemporaryDirectory(prefix="partial-python-source-") as directory:
        filename = Path(directory).resolve() / "response.py"
        executor = Executor({"__name__": "__main__", "pause": pause}, filename=str(filename))
        with filename.open("w", encoding="utf-8", newline="") as source_file:
            async with aclosing(chunks()) as deltas:
                await record_and_execute(executor, source_file, deltas)
        # The writer and executor have finished, but the host still owns the file.
        try:
            await executor.namespace["fail_later"]()
        except RuntimeError as error:
            frames = [frame for frame in traceback.extract_tb(error.__traceback__) if frame.filename == str(filename)]
            rendered = "".join(traceback.format_exception(error))
            assert [(frame.lineno, frame.line) for frame in frames] == [(4, "raise RuntimeError(label)")]
            assert awaited == [True]
            print(rendered, end="")
            # Capture/render diagnostics before leaving the owning file scope.
            return {"source": source, "stored": filename.read_bytes(), "frames": frames, "traceback": rendered, "state": executor.state}
        raise AssertionError("the fixture function should fail after its await")


if __name__ == "__main__":
    asyncio.run(main())
