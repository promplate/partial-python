"""A deterministic streaming protocol demo, not a real model evaluation.

Run with: uv run examples/streaming_tools.py
Replace chunks() with an async iterator over decoded model text deltas. That
adapter must raise on truncation or transport failure; only normal exhaustion
means the source is complete.
"""

import asyncio
from time import monotonic

from partial_python import incremental_exec


async def main():
    started = monotonic()
    received = ""
    source_finished = False
    calls = []
    source = "await foo(1)\nfor value in (2, 3):\n    await bar(value)\nawait baz(4)"

    def log(message):
        print(f"{monotonic() - started:5.2f}s  {message}")

    async def foo(value):
        assert not source_finished and received != source
        calls.append(("foo", value))
        log(f"foo({value}) runs while later code is still arriving")
        await asyncio.sleep(0.03)

    async def bar(value):
        # The loop belongs to one commit; the real successor seals its entire body.
        assert "    await bar(value)\nawait " in received
        calls.append(("bar", value))
        log(f"bar({value}) runs after the entire loop is sealed")
        await asyncio.sleep(0.03)

    async def baz(value):
        assert source_finished  # No final newline: the final call waits for EOF.
        calls.append(("baz", value))
        log(f"baz({value}) runs after normal EOF")

    async def chunks():
        nonlocal received, source_finished
        for offset in range(0, len(source), 7):
            await asyncio.sleep(0.02)
            delta = source[offset : offset + 7]
            received += delta
            log(f"receive {delta!r}")
            yield delta
        source_finished = True
        log("source finished normally")

    async for event in incremental_exec(chunks(), {"foo": foo, "bar": bar, "baz": baz}):
        log(f"completed statement {event.index}: characters {event.unit.start}:{event.unit.end}")
    assert calls == [("foo", 1), ("bar", 2), ("bar", 3), ("baz", 4)]
    log("verified: early execution, ordered awaits, deferred loop, explicit EOF")


if __name__ == "__main__":
    asyncio.run(main())
