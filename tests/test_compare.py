import ast
import asyncio
import json

import pytest

from examples import compare as demo


@pytest.mark.parametrize(
    ("scenario", "status", "incremental_effects", "batch_effects"),
    [
        ("calls", "completed", ["读取数据", "转换格式", "写入结果"], ["读取数据", "转换格式", "写入结果"]),
        ("loop", "completed", ["开始", "A", "B", "循环完成", "结束"], ["开始", "A", "B", "循环完成", "结束"]),
        ("syntax", "error", ["已准备"], []),
        ("truncated", "aborted", ["开始"], []),
    ],
)
async def test_real_execution_and_shared_source(scenario, status, incremental_effects, batch_effects):
    events = []
    result = await demo.compare(scenario, events.append, chunk_size=1, chunk_delay=0, tool_delay=0)
    source = demo.SCENARIOS[scenario]["source"]
    source_end = next(index for index, event in enumerate(events) if event["type"] == "source_end")
    received = ""
    for index, event in enumerate(events):
        if event["type"] == "source":
            received += event["delta"]
        assert event["received"] == len(received)
        if event["type"] == "unit_completed":
            assert event["source"] == source[event["start"] : event["end"]]
            assert event["end"] <= event["committed"] <= event["received"]
        if event["type"] == "tool_started" and event["lane"] == "batch":
            assert index > source_end
            assert event["committed"] == len(source)
    assert received == result["source"] == source
    assert [event["t"] for event in events] == sorted(event["t"] for event in events)
    assert events[-1]["type"] == "done" and events[-1]["result"] == result
    assert json.loads(json.dumps(events)) == events
    for lane, expected in [("incremental", incremental_effects), ("batch", batch_effects)]:
        assert result["lanes"][lane]["status"] == status
        assert result["lanes"][lane]["effects"] == expected
        effects = [event for event in events if event["type"] == "effect" and event["lane"] == lane]
        assert [event["label"] for event in effects] == expected
        assert [event["effects"] for event in effects] == [expected[:index] for index in range(1, len(expected) + 1)]
        assert result["lanes"][lane]["first_effect"] == (effects[0]["t"] if effects else None)
    if scenario in ("syntax", "truncated"):
        assert result["lanes"]["batch"]["committed"] == 0
        assert result["lanes"]["incremental"]["committed"] == source.index("\n") + 1
    else:
        assert all(lane["committed"] == len(source) for lane in result["lanes"].values())
    if scenario == "truncated":
        # The bytes already form valid Python: only the missing successful EOF prevents loop execution.
        compile(source, "<truncated-fixture>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT, dont_inherit=True)
        assert result["source_status"] == "truncated"
    if scenario == "loop":
        successor = source.index('await step("结束")')
        loop_calls = [event for event in events if event["type"] == "tool_started" and event["lane"] == "incremental" and event["label"] in ("A", "B", "循环完成")]
        assert len(loop_calls) == 3
        assert all(event["received"] > successor and event["committed"] >= successor for event in loop_calls)
        units = [event for event in events if event["type"] == "unit_completed" and event["lane"] == "incremental"]
        assert len(units) == 3 and units[1]["end"] == successor


async def test_reception_continues_while_a_tool_is_awaiting(monkeypatch):
    entered = asyncio.Event()
    source_done = asyncio.Event()
    events = []
    received = 0
    first_line_end = demo.SCENARIOS["calls"]["source"].index("\n") + 1
    sleep = asyncio.sleep

    async def paced_sleep(delay):
        if delay == 0.123:
            # Rendezvous after the first complete line, without relying on scheduler timing.
            if received >= first_line_end:
                await entered.wait()
            await sleep(0)
        elif delay == 0.456:
            await source_done.wait()
        else:
            await sleep(delay)

    def emit(event):
        nonlocal received
        events.append(event)
        received = event["received"]
        if event["type"] == "tool_started" and event["lane"] == "incremental":
            entered.set()
        elif event["type"] == "source_end":
            assert entered.is_set()
            assert not any(item["type"] == "effect" for item in events)
            source_done.set()

    monkeypatch.setattr(demo.asyncio, "sleep", paced_sleep)
    result = await asyncio.wait_for(demo.compare("calls", emit, chunk_size=1, chunk_delay=0.123, tool_delay=0.456), 2)
    assert result["lanes"]["incremental"]["effects"] == result["lanes"]["batch"]["effects"] == ["读取数据", "转换格式", "写入结果"]


async def test_cancellation_awaits_all_owned_tasks():
    entered = asyncio.Event()
    events = []

    def emit(event):
        events.append(event)
        if event["type"] == "tool_started":
            entered.set()

    task = asyncio.create_task(demo.compare("calls", emit, chunk_size=1, chunk_delay=0, tool_delay=3600))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not any(item.get_name().startswith("partial-python-demo:") for item in asyncio.all_tasks())
        assert not any(event["type"] in ("effect", "done") for event in events)
        assert [event["status"] for event in events if event["type"] == "lane_end"] == ["cancelled", "cancelled"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
