"""Exact-draft liveness recovery and persisted provider measurements."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import database as db
import server
from event_router import SubscriptionRouter


@pytest.mark.parametrize("state", ["completed", "interrupted", "cancelled", "failed", None])
async def test_probe_recovers_only_persisted_terminal_on_cursor_branch(monkeypatch, state):
    conv = await db.create_conversation("Probe fixture")
    user = await db.add_message(conv["id"], "user", "fixture")
    draft = await db.add_message(conv["id"], "assistant", "retained", parent_id=user["id"])
    queued = await db.add_message(conv["id"], "user", "queued", parent_id=draft["id"])
    await db.update_message_content(draft["id"], generation_ms=2500, turn_lifecycle_state=state)
    cursor = {"leaf_message_id": queued["id"], "thread_id": "fixture-thread"}
    monkeypatch.setattr(db, "get_client_loom_cursor", AsyncMock(return_value=cursor))
    # Another run remains active; it is never completion authority for ours.
    monkeypatch.setattr(server, "_active_generations", {(conv["id"], user["id"], 8): SimpleNamespace(done=lambda: False)})
    monkeypatch.setattr(server, "_generation_snapshots", {})
    sock = SimpleNamespace(_client_instance_id="page", send_json=AsyncMock())
    probe = {"run_id": "gen:7", "draft_msg_id": draft["id"]}
    await server._probe_followed_generation(sock, conv["id"], probe)
    event = sock.send_json.call_args.args[0]
    assert event["type"] == "generation_terminal"
    assert event["run_id"] == "gen:7" and event["message"]["id"] == draft["id"]
    assert cursor == {"leaf_message_id": queued["id"], "thread_id": "fixture-thread"}
    sock.send_json.reset_mock()
    cursor["leaf_message_id"] = user["id"]  # Moved away: no private content replay.
    await server._probe_followed_generation(sock, conv["id"], probe)
    await server._probe_followed_generation(sock, conv["id"] + 1, probe)
    await server._probe_followed_generation(sock, conv["id"], {**probe, "run_id": "gen:invalid"})
    sock.send_json.assert_not_called()


async def test_probe_does_not_invent_completion_or_reuse_another_draft(monkeypatch):
    conv = await db.create_conversation("Probe active fixture")
    draft = await db.add_message(conv["id"], "assistant", "partial")
    sock = SimpleNamespace(_client_instance_id="page", send_json=AsyncMock())
    monkeypatch.setattr(server, "_active_generations", {})
    monkeypatch.setattr(server, "_generation_snapshots", {})
    await server._probe_followed_generation(sock, conv["id"], {"run_id": "gen:7", "draft_msg_id": draft["id"]})
    sock.send_json.assert_not_called()  # No terminal evidence, even with an empty registry.

    key = (conv["id"], None, 7)
    monkeypatch.setattr(server, "_active_generations", {key: SimpleNamespace(done=lambda: False)})
    monkeypatch.setattr(server, "_generation_snapshots", {key: {"draft_msg_id": draft["id"]}})
    allowed = AsyncMock(return_value=True)
    snapshot = AsyncMock()
    monkeypatch.setattr(server, "_websocket_can_subscribe_run", allowed)
    monkeypatch.setattr(server, "_send_active_gen_state", snapshot)
    router = SubscriptionRouter()
    class Socket:
        pass
    sock = Socket()
    router.register(sock, conv["id"])
    monkeypatch.setattr(server, "_event_router", router)
    await server._probe_followed_generation(sock, conv["id"], {"run_id": "gen:7", "draft_msg_id": draft["id"]})
    assert router.subscriptions(sock) == {"gen:7"}
    snapshot.assert_awaited_once()
    await server._probe_followed_generation(sock, conv["id"], {"run_id": "gen:7", "draft_msg_id": draft["id"]})
    snapshot.assert_awaited_once()  # No whole-history polling on a healthy stream.


@pytest.mark.parametrize("first", ["text", "thinking", "none"])
async def test_weave_first_output_and_usage_survive_finalization(monkeypatch, first):
    conv = await db.create_conversation("Local telemetry fixture", mode="weave")
    await db.update_conversation_fields(conv["id"], system_only=1, system_prompt="Fixture")
    conv = await db.get_conversation(conv["id"])
    user = await db.add_message(conv["id"], "user", "fixture")
    sent = []
    async def send(cid, payload):
        sent.append(payload)
    async def stream(*args, **kwargs):
        yield {"type": "thinking_start"}
        yield {"type": "thinking_delta", "text": ""}
        yield ""
        assert not any(e["type"] == "first_token" for e in sent)
        await asyncio.sleep(.015)
        if first != "none":
            yield {"type": "thinking_delta", "text": "reasoning"} if first == "thinking" else "answer"
            measured = next(e["elapsed_ms"] for e in sent if e["type"] == "first_token")
            assert measured >= 10
            await asyncio.sleep(.015)
            yield " final"
        yield {"type": "usage", "input_tokens": 1234, "output_tokens": 56}
    monkeypatch.setattr(server, "stream_chat", stream)
    monkeypatch.setattr(server, "_ws_send", send)
    monkeypatch.setattr("context_manager.update_rolling_summary", AsyncMock())
    await server._handle_weave_generation(None, conv["id"], conv, {"action": "generate", "parent_id": user["id"]})
    children = await db.get_children(user["id"])
    first_events = [e for e in sent if e["type"] == "first_token"]
    if first == "none":
        assert first_events == []
    else:
        assert len(first_events) == 1
        message = next(m for m in children if m["role"] == "assistant")
        assert message["time_to_first_token_ms"] == first_events[0]["elapsed_ms"]
        assert message["generation_ms"] >= message["time_to_first_token_ms"]
        assert message["turn_input_tokens"] == 1234 and message["turn_output_tokens"] == 56


async def test_first_response_is_persisted_once_and_survives_interrupt(monkeypatch):
    conv = await db.create_conversation("Timing fixture")
    draft = await db.add_message(conv["id"], "assistant", "partial")
    monkeypatch.setattr(server, "_ws_send", AsyncMock())
    first_ms = await server._record_first_response(conv["id"], draft["id"], None, time.time() - 1, None)
    assert await server._record_first_response(conv["id"], draft["id"], None, time.time() - 9, first_ms) == first_ms
    await db.update_message_content(draft["id"], turn_input_tokens=1200, turn_output_tokens=20)
    monkeypatch.setattr(server, "_generation_stop_disposition", lambda: "interrupt")
    await server._finalize_user_stopped_generation(conv["id"], draft["id"], partial_text="partial", generation_ms=2000)
    message = await db.get_message(draft["id"])
    assert message["time_to_first_token_ms"] == first_ms
    assert message["turn_input_tokens"] == 1200 and message["turn_output_tokens"] == 20


async def test_nonstreaming_ooda_records_response_arrival_without_changing_generation(monkeypatch):
    conv = await db.create_conversation("OODA telemetry fixture", mode="weave")
    user = await db.add_message(conv["id"], "user", "fixture")
    async def sync(*args, **kwargs):
        await asyncio.sleep(.015)
        return "<ooda><observe>Fixture only</observe><orient>No tools needed</orient><decide>Answer</decide></ooda>\nFixture answer", {"input_tokens": 100, "output_tokens": 20}
    monkeypatch.setattr(server, "sync_chat", sync)
    monkeypatch.setattr(server, "_ws_send", AsyncMock())
    monkeypatch.setattr("context_manager.update_rolling_summary", AsyncMock())
    await server._handle_ooda_generation(None, conv["id"], conv, {"action": "generate", "parent_id": user["id"]})
    message = next(m for m in await db.get_children(user["id"]) if m["role"] == "assistant")
    assert message["content"] == "Fixture answer"
    assert message["time_to_first_token_ms"] >= 10
    assert message["generation_ms"] >= message["time_to_first_token_ms"]
    assert message["turn_input_tokens"] == 100


@pytest.mark.parametrize("harness", ["codex", "goose"])
async def test_interrupted_adapter_keeps_already_reported_tokens(monkeypatch, tmp_path, harness):
    conv = await db.create_conversation("Interrupted telemetry fixture", mode=harness, project_dir=str(tmp_path))
    model = "codex-gpt-5.6-sol" if harness == "codex" else "goose:openrouter:z-ai/glm-5.3-flash"
    # Adapter-only test: scheduling/leases are tested separately.
    await db.update_conversation_fields(conv["id"], cc_model=model, parallel_agents_enabled=True)
    user = await db.add_message(conv["id"], "user", "bounded fixture")
    class Proc:
        pid = 999991
        returncode = 0
        async def wait(self):
            return 0
    async def launch(*args, **kwargs):
        async def events():
            yield {"type": "session_info", "session_id": "fixture-session"}
            yield {"type": "text_delta", "text": "partial result"}
            yield {"type": "usage", "input_tokens": 1234, "output_tokens": 56}
            raise asyncio.CancelledError()
        return Proc(), events()
    monkeypatch.setattr(server, "_ws_send", AsyncMock())
    monkeypatch.setattr(server, "_generation_stop_disposition", lambda: "interrupt")
    monkeypatch.setattr(server.config, "enable_goose", True)
    adapter = server.codex_client if harness == "codex" else server.goose_client
    monkeypatch.setattr(adapter, "run_" + harness, launch)
    monkeypatch.setattr(adapter, "cancel_" + harness, AsyncMock())
    handler = server._handle_claude_generation if harness == "codex" else server._handle_goose_generation
    with pytest.raises(asyncio.CancelledError):
        await handler(None, conv["id"], await db.get_conversation(conv["id"]), {"action": "generate", "parent_id": user["id"]})
    message = next(m for m in await db.get_children(user["id"]) if m["role"] == "assistant")
    assert message["turn_lifecycle_state"] == "interrupted"
    assert message["turn_input_tokens"] == 1234 and message["turn_output_tokens"] == 56
    assert message["time_to_first_token_ms"] is not None
