"""Compare the same simulated source stream with real incremental and whole-module execution.

Run: uv run examples/compare.py [calls|loop|syntax|truncated]
The source is a fixed fixture; tool effects and elapsed times come from live Python execution.
Import compare() to consume the same events in a browser running Pyodide.
"""

import argparse
import ast
import asyncio
import inspect
from collections.abc import Callable
from contextlib import aclosing
from math import isfinite
from time import perf_counter

from partial_python import Executor

SCENARIOS = {
    "calls": {
        "name": "三个 await",
        "description": "完整的语句立即执行；最后一行没有换行，等待成功 EOF。",
        "source": 'await step("读取数据")\nawait step("转换格式")\nawait step("写入结果")',
        "truncated": False,
    },
    "loop": {
        "name": "for / else 的边界",
        "description": "整个 for / else 保留到下一条顶层语句出现，循环内部不会被提前执行。",
        "source": 'await step("开始")\nfor item in ["A", "B"]:\n    await step(item)\nelse:\n    await step("循环完成")\nawait step("结束")',
        "truncated": False,
    },
    "syntax": {
        "name": "末尾语法错误",
        "description": "源流正常结束，但末尾语法不完整。增量执行已有的效果保留；整段编译失败，没有效果。",
        "source": 'await step("已准备")\nif True:\n',
        "truncated": False,
    },
    "truncated": {
        "name": "源流被截断",
        "description": "传输中断不等于成功 EOF：保留已经完成的效果，未闭合的循环不能执行。",
        "source": 'await step("开始")\nfor item in ["A", "B"]:\n    await step(item)\n',
        "truncated": True,
    },
}


class SourceTruncated(RuntimeError):
    """The fixture ended without authorizing an EOF flush."""


async def compare(scenario: str, emit: Callable[[dict], None], *, chunk_size: int = 5, chunk_delay: float = 0.10, tool_delay: float = 0.4) -> dict:
    """Emit JSON-compatible events; all offsets count Python characters, not bytes.

    Each event has type, t (elapsed seconds), and received (source characters).
    Source arrival runs independently of execution. Each lane has its own namespace
    and effects; successful EOF alone permits batch compilation and final flushing.
    Cancellation propagates after all three owned tasks have stopped.
    """
    fixture = SCENARIOS[scenario]
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 1:
        raise ValueError("chunk_size must be a positive integer")
    if any(not isfinite(delay) or delay < 0 for delay in (chunk_delay, tool_delay)):
        raise ValueError("delays must be finite and non-negative")
    began = perf_counter()
    received = ""
    source_status = "generating"
    source_done = asyncio.Event()
    # These finite fixtures cannot fill unbounded memory; a live transport needs its own buffering policy.
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    lanes = {lane: {"effects": [], "first_effect": None} for lane in ("incremental", "batch")}
    batch_committed = 0

    def notify(kind, **fields):
        event = {"type": kind, "t": perf_counter() - began, "received": len(received), **fields}
        emit(event)
        return event

    def committed(lane):
        return executor.parser_state.committed if lane == "incremental" else batch_committed

    def finish_lane(lane, status, error=None):
        state = lanes[lane]
        state.update(status=status, committed=committed(lane), finished_at=perf_counter() - began, error=error)
        notify("lane_end", lane=lane, **{**state, "effects": list(state["effects"])})

    def namespace(lane):
        async def step(label):
            notify("tool_started", lane=lane, label=label, committed=committed(lane))
            await asyncio.sleep(tool_delay)
            state = lanes[lane]
            state["effects"].append(label)
            event = notify("effect", lane=lane, label=label, effects=list(state["effects"]), committed=committed(lane))
            if state["first_effect"] is None:
                state["first_effect"] = event["t"]

        return {"__name__": "__main__", "step": step}

    executor = Executor(namespace("incremental"))
    batch_namespace = namespace("batch")

    async def produce():
        nonlocal received, source_status
        source = fixture["source"]
        for offset in range(0, len(source), chunk_size):
            await asyncio.sleep(chunk_delay)
            delta = source[offset : offset + chunk_size]
            received += delta
            notify("source", delta=delta)
            queue.put_nowait(delta)
        source_status = "truncated" if fixture["truncated"] else "completed"
        notify("source_end", status=source_status)
        source_done.set()
        queue.put_nowait(None)

    async def queued_source():
        while (delta := await queue.get()) is not None:
            yield delta
        if fixture["truncated"]:
            raise SourceTruncated("Source stream was truncated; EOF execution was not authorized")

    async def incremental():
        try:
            async with aclosing(executor.stream(queued_source())) as events:
                async for event in events:
                    unit = event.unit
                    notify("unit_completed", lane="incremental", index=event.index, start=unit.start, end=unit.end, source=unit.source, committed=committed("incremental"))
        except asyncio.CancelledError:
            finish_lane("incremental", "cancelled")
            raise
        except SourceTruncated as error:
            finish_lane("incremental", "aborted", str(error))
        except Exception as error:  # noqa: BLE001 - report execution errors without stopping the other lane.
            finish_lane("incremental", "error", f"{type(error).__name__}: {error}")
        else:
            finish_lane("incremental", "completed")

    async def batch():
        nonlocal batch_committed
        try:
            await source_done.wait()
            if source_status == "truncated":
                finish_lane("batch", "aborted", "Source stream was truncated; batch execution was not authorized")
                return
            # Compile the whole exact buffer once. Feeding it to Executor would hide the semantic difference on syntax errors.
            code = compile(received, "<batch>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT, dont_inherit=True)
            batch_committed = len(received)
            result = eval(code, batch_namespace, batch_namespace)
            if inspect.isawaitable(result):
                await result
            notify("unit_completed", lane="batch", index=0, start=0, end=len(received), source=received, committed=batch_committed)
        except asyncio.CancelledError:
            finish_lane("batch", "cancelled")
            raise
        except Exception as error:  # noqa: BLE001 - report execution errors without stopping the other lane.
            finish_lane("batch", "error", f"{type(error).__name__}: {error}")
        else:
            finish_lane("batch", "completed")

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(produce(), name="partial-python-demo:source")
        tasks.create_task(incremental(), name="partial-python-demo:incremental")
        tasks.create_task(batch(), name="partial-python-demo:batch")
    result = {"scenario": scenario, "source_status": source_status, "received": len(received), "source": received, "lanes": lanes}
    notify("done", result=result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", nargs="?", choices=SCENARIOS, default="calls")
    args = parser.parse_args()

    def show(event):
        prefix = f"{event['t']:5.2f}s {event.get('lane', 'source'):>11}"
        match event["type"]:
            case "source":
                print(f"{prefix} + {event['delta']!r}", flush=True)
            case "source_end":
                print(f"{prefix} {event['status']}", flush=True)
            case "tool_started":
                print(f"{prefix} started {event['label']}", flush=True)
            case "effect":
                print(f"{prefix} effect  {event['label']}", flush=True)
            case "lane_end":
                print(f"{prefix} {event['status']}: {event['effects']}", flush=True)
                if event["error"]:
                    print(f"             {event['error']}", flush=True)

    print(SCENARIOS[args.scenario]["description"])
    asyncio.run(compare(args.scenario, show))


if __name__ == "__main__":
    main()
