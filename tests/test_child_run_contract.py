"""Child execution/permission contracts; no live providers or host lifecycle."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import database as db
import server
from agent_supervisor import AgentSupervisor, AgentTeardownError
from event_router import SubscriptionRouter


@pytest.mark.parametrize("events,expected", [
    ([{"type": "error", "error": "connection lost"}, {"type": "result"}], "failed"),
    ([{"type": "result", "is_error": True}], "failed"),
    ([{"type": "result", "stop_reason": "incomplete"}], "failed"),
    ([{"type": "result", "stop_reason": "interrupted"}], "failed"),
    ([{"type": "text_delta", "text": "partial only"}], "failed"),
    ([{"type": "result", "text": "completed answer"}], "completed"),
    ([{"type": "tool_result", "is_error": True}, {"type": "result", "text": "recovered"}], "completed"),
])
async def test_goose_child_identity_and_terminal_outcome(tmp_path, monkeypatch, events, expected):
    conv = await db.create_conversation("Child fixture", mode="goose", project_dir=str(tmp_path))
    captured = {}

    async def launch(prompt, **kwargs):
        captured.update(kwargs)

        async def stream():
            # This native request is not the bridge's actionable UUID.
            yield {"type": "permission_request", "request_id": 42}
            for event in events:
                yield event

        return SimpleNamespace(pid=None, returncode=0), stream()

    monkeypatch.setattr(server.goose_client, "run_goose", launch)
    run = await db.create_agent_run("child-fixture", conv["id"], "bounded task", requested_harness="goose")
    supervisor = AgentSupervisor(launcher=server._launch_agent_run)
    await supervisor._run_one(run)
    saved = await db.get_agent_run(run["run_id"])
    assert captured["conv_id"] == conv["id"]
    assert captured["permission_scope"] == "agent:child-fixture"
    assert saved["status"] == expected
    assert bool(saved["error"]) == (expected == "failed")
    if events[-1].get("text") and expected == "completed":
        assert saved["result"]["text"] == events[-1]["text"]
    history = await db.list_agent_events(run["run_id"])
    assert not any(e["event_kind"] == "permission_request" for e in history)
    assert history[-1]["event_kind"] == f"agent_{expected}"


@pytest.mark.parametrize("harness,adapter,method", [
    ("codex", "codex_client", "cancel_codex"),
    ("gemini", "gemini_client", "cancel_gemini"),
    ("goose", "goose_client", "cancel_goose"),
    ("claude", "claude_client", "cancel_claude"),
    ("local", "claude_client", "cancel_claude"),
    ("openrouter", "claude_client", "cancel_claude"),
    ("umans", "claude_client", "cancel_claude"),
    ("dream-claude", "claude_client", "cancel_claude"),
])
async def test_child_uses_adapter_cleanup_and_waits(harness, adapter, method, monkeypatch):
    release = asyncio.Event()
    wait_entered = asyncio.Event()
    proc = SimpleNamespace(pid=None, returncode=None)

    async def wait():
        wait_entered.set()
        await release.wait()
        proc.returncode = 0

    proc.wait = wait
    cancel = AsyncMock()
    if getattr(server, adapter) is None:
        monkeypatch.setattr(server, adapter, SimpleNamespace(**{method: cancel}))
    else:
        monkeypatch.setattr(getattr(server, adapter), method, cancel)
    cleanup = asyncio.create_task(server._teardown_child_provider(proc, harness))
    try:
        await asyncio.wait_for(wait_entered.wait(), 2)
        assert not cleanup.done()
        cancel.assert_awaited_once_with(proc)
        release.set()
        await asyncio.wait_for(cleanup, 2)
    finally:
        release.set()
        await asyncio.gather(cleanup, return_exceptions=True)


async def test_surviving_descendants_are_not_reported_as_stopped(monkeypatch):
    import psutil
    child = object()
    proc = SimpleNamespace(pid=987654321, returncode=None)
    monkeypatch.setattr(psutil, "Process", lambda _pid: SimpleNamespace(children=lambda **_: [child]))
    monkeypatch.setattr(psutil, "wait_procs", lambda *a, **k: ([], [child]))

    async def cancel(_proc):
        proc.returncode = 0

    monkeypatch.setattr(server.goose_client, "cancel_goose", cancel)
    with pytest.raises(AgentTeardownError, match="descendants"):
        await server._teardown_child_provider(proc, "goose")


async def test_unverified_teardown_retains_scope(monkeypatch):
    conv = await db.create_conversation("Child scope fixture")
    first = await db.create_agent_run("stubborn-child", conv["id"], "first", project_dir="fixture")
    second = await db.create_agent_run("waiting-child", conv["id"], "second", project_dir="fixture")
    launched = []

    async def launcher(run, emit):
        launched.append(run["run_id"])
        await db.update_agent_run(run["run_id"], pid=987654321)
        raise AgentTeardownError("exit unconfirmed")

    supervisor = AgentSupervisor(launcher=launcher)
    await supervisor._run_one(first)
    saved = await db.get_agent_run(first["run_id"])
    assert saved["status"] == "cancel_requested"
    assert saved["pid"] == 987654321
    assert saved["completed_at"] is None
    assert (await supervisor.cancel(first["run_id"]))["status"] == "cancel_requested"
    # Run just the scheduler (not restart recovery) against the occupied scope.
    dispatcher = asyncio.create_task(supervisor._dispatch_loop())
    try:
        await asyncio.sleep(0.1)
        assert launched == [first["run_id"]]
        assert (await db.get_agent_run(second["run_id"]))["status"] == "queued"
    finally:
        dispatcher.cancel()
        await asyncio.gather(dispatcher, return_exceptions=True)


async def test_repeated_cancel_waits_for_cleanup_and_retires_approvals(tmp_path, monkeypatch):
    conv = await db.create_conversation("Child cancel fixture", mode="goose", project_dir=str(tmp_path))
    run = await db.create_agent_run("cancel-child", conv["id"], "task", requested_harness="goose")
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    proc = SimpleNamespace(pid=987654321, returncode=None)

    async def launch(*args, **kwargs):
        async def stream():
            started.set()
            await asyncio.Event().wait()
            yield {}
        return proc, stream()

    async def cleanup(*args):
        cleaning.set()
        await release.wait()
        proc.returncode = 0

    monkeypatch.setattr(server.goose_client, "run_goose", launch)
    monkeypatch.setattr(server, "_teardown_child_provider", cleanup)
    monkeypatch.setattr(server, "_pending_hook_permissions", {
        "fixture-approval": {"conv_id": conv["id"], "permission_scope": "agent:cancel-child",
                             "event": asyncio.Event(), "response": None},
    })
    supervisor = AgentSupervisor(launcher=server._launch_agent_run)
    task = asyncio.create_task(supervisor._run_one(run))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert (await db.get_agent_run(run["run_id"]))["pid"] == proc.pid
        release.set()
        await asyncio.wait_for(task, 2)
        saved = await db.get_agent_run(run["run_id"])
        assert saved["status"] == "cancelled"
        assert saved["pid"] is None
        pending = server._pending_hook_permissions["fixture-approval"]
        assert pending["event"].is_set()
        assert pending["response"]["decision_source"] == "loom_lifecycle"
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_child_permission_waiter_exists_before_delivery_and_recovers(monkeypatch):
    conv = await db.create_conversation("Child approval fixture")
    await db.update_conversation_fields(conv["id"], permission_profile="strict")
    other = await db.create_conversation("Other approval fixture")
    run = await db.create_agent_run("approval-child", conv["id"], "task", client_instance_id="old-page")
    await db.update_agent_run(run["run_id"], status="running")
    monkeypatch.setattr(server, "_pending_hook_permissions", {})
    monkeypatch.setattr(server, "_event_router", SubscriptionRouter())
    monkeypatch.setattr(server, "_active_websockets", {})

    class Socket:
        _client_instance_id = "new-page"
        def __init__(self):
            self.events = []

        async def send_json(self, event):
            self.events.append(event)
            pending = server._pending_hook_permissions[event["request_id"]]
            # Simulate answering immediately, before send_json returns.
            pending["response"] = {"allow": False, "feedback": "Use the fixture only", "decision_source": "user"}
            pending["event"].set()

    socket = Socket()
    server._active_websockets[conv["id"]] = {socket}
    server._event_router.register(socket, conv["id"])
    server._event_router.subscribe(socket, "agent:approval-child")
    assert await server._permission_socket_can_recover(socket, conv["id"], "agent:approval-child", None)
    assert not await server._permission_socket_can_recover(socket, other["id"], "agent:approval-child", None)
    result = await asyncio.wait_for(server.handle_cc_permission({
        "loom_conv_id": conv["id"], "agent_run_id": run["run_id"],
        "tool_name": "SensitiveRead", "tool_input": {"file_path": "fixture.txt"},
    }), 3)
    assert result["allow"] is False
    assert result["feedback"] == "Use the fixture only"
    assert result["decision_source"] == "user"
    assert socket.events[0]["run_id"] == "agent:approval-child"
    invalid = await server.handle_cc_permission({
        "loom_conv_id": other["id"], "agent_run_id": run["run_id"], "tool_name": "SensitiveRead",
    })
    assert invalid["decision_source"] == "loom_lifecycle"


@pytest.mark.parametrize("alive", [True, False])
async def test_restart_retains_only_unverified_process_scopes(monkeypatch, alive):
    import agent_supervisor
    conv = await db.create_conversation("Restart fixture")
    run = await db.create_agent_run("restart-fixture", conv["id"], "task")
    await db.update_agent_run(run["run_id"], status="running", pid=987654321)
    monkeypatch.setattr(agent_supervisor.psutil, "pid_exists", lambda pid: alive)
    supervisor = AgentSupervisor()
    await supervisor.start()
    try:
        current = await db.get_agent_run(run["run_id"])
        assert current["status"] == ("cancel_requested" if alive else "paused")
        assert current["pid"] == (987654321 if alive else None)
    finally:
        await supervisor.stop()


async def test_child_response_authority_and_first_decision_wins(monkeypatch):
    import json
    from starlette.websockets import WebSocketDisconnect
    conv = await db.create_conversation("Approval owner")
    other = await db.create_conversation("Other conversation")
    run = await db.create_agent_run("owned-child", conv["id"], "task", client_instance_id="owner")
    pending = {
        "event": asyncio.Event(), "response": None, "conv_id": conv["id"],
        "permission_scope": "agent:owned-child", "run_id": "agent:owned-child",
        "client_instance_id": "owner", "tool_name": "FixtureReview", "tool_input": {},
    }
    monkeypatch.setattr(server, "_pending_hook_permissions", {"fixture-request": pending})
    monkeypatch.setattr(server, "_event_router", SubscriptionRouter())
    monkeypatch.setattr(server, "_active_websockets", {})

    class Socket:
        def __init__(self, identity, allow):
            self._client_instance_id = identity
            self.events = []
            self.requests = [{"action": "permission_response", "request_id": "fixture-request", "allow": allow}]

        async def send_json(self, data):
            self.events.append(data)

        async def receive_text(self):
            if self.requests:
                return json.dumps(self.requests.pop())
            raise WebSocketDisconnect()

    owner = Socket("owner", True)
    stranger = Socket("owner", False)
    observer = Socket("observer", False)
    server._active_websockets[conv["id"]] = {owner, observer}
    for ws in [owner, observer]:
        server._event_router.register(ws, conv["id"])
        server._event_router.subscribe(ws, "agent:owned-child")
    server._event_router.register(stranger, other["id"])
    await server._continue_ws_chat(stranger, other["id"], "owner")
    assert pending["response"] is None
    await server._continue_ws_chat(observer, conv["id"], "observer")
    assert pending["response"] is None
    await server._continue_ws_chat(owner, conv["id"], "owner")
    assert pending["response"]["allow"] is True
    owner.requests = [{"action": "permission_response", "request_id": "fixture-request", "allow": False}]
    await server._continue_ws_chat(owner, conv["id"], "owner")
    assert pending["response"]["allow"] is True


async def test_child_state_and_terminal_event_are_atomic_and_delivery_is_not_outcome():
    conv = await db.create_conversation("Atomic child fixture")
    run = await db.create_agent_run("atomic-child", conv["id"], "task")
    with pytest.raises(ValueError, match="lifecycle field"):
        await db.append_agent_event(run["run_id"], "agent_completed", {}, run_state={"invalid_field": True})
    assert await db.list_agent_events(run["run_id"]) == []
    assert (await db.get_agent_run(run["run_id"]))["status"] == "queued"

    async def launcher(_run, emit):
        return {"text": "done"}

    seen = []
    async def sink(_run, event):
        if event["event_kind"] == "agent_completed":
            seen.append(((await db.get_agent_run(run["run_id"]))["status"],
                         (await db.list_agent_events(run["run_id"]))[-1]["event_kind"]))
        raise ConnectionError("fixture disconnected browser")

    await AgentSupervisor(launcher=launcher, event_sink=sink)._run_one(run)
    assert (await db.get_agent_run(run["run_id"]))["status"] == "completed"
    assert seen == [("completed", "agent_completed")]
