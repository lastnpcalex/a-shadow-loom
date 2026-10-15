"""Bounded child traffic, durable ordering, teardown drain and exact routing."""
import asyncio
from contextlib import asynccontextmanager

import pytest

import database as db
from agent_events import ChildEventWriter
from event_router import SubscriptionRouter


async def make_run(name="batch-child"):
    conv = await db.create_conversation("Child batching fixture")
    return await db.create_agent_run(name, conv["id"], "bounded fixture")


async def test_thousand_deltas_keep_payloads_with_seventeen_transactions(monkeypatch):
    run = await make_run()
    original = db.write_transaction
    transactions = []

    @asynccontextmanager
    async def counted():
        transactions.append(1)
        async with original() as connection:
            yield connection

    monkeypatch.setattr(db, "write_transaction", counted)
    delivered = []
    async def batch_sink(_run, events):
        delivered.append(events)
    async def sink(_run, event):
        delivered.append([event])
    writer = ChildEventWriter(run, sink, batch_sink)
    writer.interval = 10
    payload = {"text": ""}
    for index in range(1000):
        payload["text"] = f"{index},"
        await writer.emit("text_delta", payload)
    await writer.emit("agent_completed", {"status": "completed"}, run_state={"status": "completed"})
    await writer.close()
    history = await db.list_agent_events(run["run_id"], limit=5000)
    assert len(transactions) == 17  # 16 output batches + atomic terminal state.
    assert len(delivered) == 17
    assert len(history) == 1001
    assert [e["sequence"] for e in history] == list(range(1, 1002))
    assert len({e["event_id"] for e in history}) == 1001
    assert "".join(e["payload"]["text"] for e in history[:-1]) == "".join(f"{i}," for i in range(1000))
    assert [e["event_id"] for group in delivered for e in group] == [e["event_id"] for e in history]
    assert (await db.get_agent_run(run["run_id"]))["status"] == "completed"
    assert (await db.list_agent_runs(run["conversation_id"]))[0]["event_sequence"] == 1001


async def test_partial_batch_flushes_on_timer_without_another_provider_event():
    run = await make_run()
    ready = asyncio.Event()
    async def sink(_run, event):
        ready.set()
    writer = ChildEventWriter(run, sink)
    try:
        await writer.emit("thinking_delta", {"text": "working"})
        await asyncio.wait_for(ready.wait(), timeout=2)
        assert len(await db.list_agent_events(run["run_id"])) == 1
    finally:
        await writer.close()


@pytest.mark.parametrize("kind", ["permission_request", "permission_resolved", "tool_start", "error", "agent_cancelled", "agent_paused"])
async def test_control_flushes_pending_output_immediately(kind):
    run = await make_run()
    writer = ChildEventWriter(run)
    writer.interval = 10
    try:
        await writer.emit("text_delta", {"text": "partial"})
        await writer.emit(kind, {"request_id": "fixture-request"})
        assert [e["event_kind"] for e in await db.list_agent_events(run["run_id"])] == ["text_delta", kind]
        assert not writer.pending
    finally:
        await writer.close()


async def test_byte_bound_flushes_without_waiting_for_event_count():
    run = await make_run()
    writer = ChildEventWriter(run)
    writer.interval = 10
    await writer.emit("tool_input_delta", {"text": "x" * writer.max_bytes})
    assert len(await db.list_agent_events(run["run_id"])) == 1
    assert not writer.pending
    await writer.close()


async def test_cancellation_joins_commit_before_terminal_event(monkeypatch):
    run = await make_run()
    original = db.append_agent_events
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(run_id, items, **kwargs):
        if items[0]["event_kind"] == "text_delta":
            entered.set()
            await release.wait()
        return await original(run_id, items, **kwargs)

    monkeypatch.setattr(db, "append_agent_events", blocked)
    writer = ChildEventWriter(run)
    writer.interval = 10
    await writer.emit("text_delta", {"text": "retained"})
    flush = asyncio.create_task(writer.flush())
    await entered.wait()
    flush.cancel()
    with pytest.raises(asyncio.CancelledError):
        await flush
    terminal = asyncio.create_task(writer.emit("agent_cancelled", {}, run_state={"status": "cancelled"}))
    await asyncio.sleep(0)
    assert not terminal.done()
    release.set()
    await terminal
    await writer.close()
    assert [e["event_kind"] for e in await db.list_agent_events(run["run_id"])] == ["text_delta", "agent_cancelled"]
    assert (await db.get_agent_run(run["run_id"]))["status"] == "cancelled"
    assert writer._flush_task.done() and writer._timer.done()


async def test_socket_failure_does_not_erase_persisted_output():
    run = await make_run()
    async def broken(*_):
        raise ConnectionError("fixture socket closed")
    writer = ChildEventWriter(run, broken, broken)
    await writer.emit("text_delta", {"text": "a"})
    await writer.emit("text_delta", {"text": "b"})
    await writer.emit("agent_completed", {}, run_state={"status": "completed"})
    await writer.close()
    assert len(await db.list_agent_events(run["run_id"])) == 3
    assert (await db.get_agent_run(run["run_id"]))["status"] == "completed"


async def test_batch_transaction_rolls_back_all_events_on_serialization_error():
    run = await make_run()
    with pytest.raises(TypeError):
        await db.append_agent_events(run["run_id"], [
            {"event_kind": "text_delta", "payload": {"text": "first"}},
            {"event_kind": "text_delta", "payload": {"invalid": object()}},
        ], run_state={"status": "completed"})
    assert await db.list_agent_events(run["run_id"]) == []
    assert (await db.get_agent_run(run["run_id"]))["status"] == "queued"


async def test_concurrent_batches_allocate_unique_contiguous_sequences():
    run = await make_run()
    await asyncio.gather(*(db.append_agent_events(run["run_id"], [
        {"event_kind": "text_delta", "payload": {"text": str(i)}} for i in range(25)
    ]) for _ in range(4)))
    assert [e["sequence"] for e in await db.list_agent_events(run["run_id"])] == list(range(1, 101))


async def test_batch_transport_is_exact_run_scoped_and_legacy_compatible(monkeypatch):
    import server
    run = await make_run()
    class Socket:
        def __init__(self, batches=False):
            self._agent_event_batches = batches
            self.frames = []
        async def send_json(self, frame):
            self.frames.append(frame)
    modern, legacy, stranger = Socket(True), Socket(), Socket(True)
    router = SubscriptionRouter()
    monkeypatch.setattr(server, "_event_router", router)
    monkeypatch.setattr(server, "_active_websockets", {run["conversation_id"]: {modern, legacy, stranger}})
    for socket in (modern, legacy, stranger):
        router.register(socket, run["conversation_id"])
    for socket in (modern, legacy):
        router.subscribe(socket, f"agent:{run['run_id']}")
    events = await db.append_agent_events(run["run_id"], [
        {"event_kind": "text_delta", "payload": {"text": str(i)}} for i in range(12)
    ])
    await server._publish_agent_events(run, events)
    assert len(modern.frames) == 1
    assert modern.frames[0]["type"] == "agent_event_batch"
    assert modern.frames[0]["event"]["kind"] == "agent_event_batch"
    assert modern.frames[0]["events"] == legacy.frames
    assert len(legacy.frames) == 12
    assert not stranger.frames
