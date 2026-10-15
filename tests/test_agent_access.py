"""Reservation, disclosure, transport and scheduler negative controls."""
import asyncio
import json
from pathlib import Path

import pytest
import database as db
import agent_access as access
from agent_supervisor import AgentSupervisor


def run(root, reads=(), writes=(), sensitive=False):
    return {"project_dir": str(root), "access": access.validate(root, {
        "read": list(reads), "write": list(writes), "sensitive": sensitive})}


def test_deck_conflicts_and_reader_writer_boundary(tmp_path):
    a = run(tmp_path, writes=["a.pptx"])
    b = run(tmp_path, writes=["b.pptx"])
    reader = run(tmp_path, reads=["a.pptx"])
    assert not access.conflicts(a, b)
    assert access.conflicts(a, reader)
    assert not access.conflicts(reader, reader)
    assert access.conflicts(a, run(tmp_path, writes=["."]))
    assert not access.conflicts(a, run(tmp_path / "other", writes=["a.pptx"]))
    assert access.conflicts(a, {"project_dir": str(tmp_path), "path_scope": []})


def test_nesting_and_invalid_scopes(tmp_path):
    parent = run(tmp_path, reads=["inputs"], writes=["output"], sensitive=True)
    assert access.narrows(parent, run(tmp_path, reads=["inputs/a"], writes=["output/a"], sensitive=True))
    assert not access.narrows(parent, run(tmp_path, writes=["inputs/a"], sensitive=True))
    assert not access.narrows(parent, run(tmp_path, writes=["output/a"]))
    for invalid in ({"write": ["../escape"]}, {"read": "x"}, {"sensitive": "false"}, {"typo": []}):
        with pytest.raises(ValueError):
            access.validate(tmp_path, invalid)


def test_sensitive_projection_is_allowlisted_and_release_only(tmp_path):
    private = {**run(tmp_path, sensitive=True), "run_id": "x", "status": "completed",
               "task": "secret task", "result": {"text": "secret text", "artifacts": ["secret path"]},
               "error": "secret error", "future_field": "secret"}
    assert "secret" not in json.dumps(access.public_run(private))
    assert access.public_run(private, human=True) == private
    private["released_result"] = "approved summary"
    assert access.public_run(private)["result"] == {"text": "approved summary"}
    assert "secret" not in json.dumps(access.public_run(private))


def test_structured_tools_fail_closed(tmp_path):
    reader = run(tmp_path, reads=["input"])
    assert not access.tool_error(reader, "Read", {"file_path": "input/a"}, "read")
    assert access.tool_error(reader, "Write", {"file_path": "input/a"}, "workspace_write")
    assert access.tool_error(reader, "Bash", {"command": "anything"}, "execute")
    writer = run(tmp_path, writes=["out"])
    assert not access.tool_error(writer, "Write", {"file_path": "out/a"}, "workspace_write")
    assert access.tool_error(writer, "Write", {"file_path": "elsewhere"}, "workspace_write")
    assert access.tool_error(writer, "Write", {"file_path": "../outside"}, "workspace_write")
    private = run(tmp_path, writes=["out"], sensitive=True)
    assert access.tool_error(private, "Bash", {}, "execute")
    assert access.tool_error(private, "WebFetch", {}, "network")


async def test_independent_local_decks_parallel_and_competing_writer_waits(tmp_database, tmp_path):
    conv = await db.create_conversation("Decks", mode="local")
    started = []
    release = asyncio.Event()
    async def launch(row, emit):
        started.append(row["task"])
        (tmp_path / row['access']['write'][0]).write_text(row['task'])
        await release.wait()
        return {"text": "done"}
    supervisor = AgentSupervisor(launcher=launch)
    supervisor.max_local_concurrent = 2
    await supervisor.start()
    try:
        for name, target in (("a", "a.pptx"), ("b", "b.pptx"), ("conflict", "a.pptx")):
            row = await supervisor.spawn(conv["id"], name, harness="local", project_dir=str(tmp_path),
                                         access={"write": [target]})
        for _ in range(100):
            if len(started) == 2: break
            await asyncio.sleep(.02)
        assert set(started) == {"a", "b"}
        assert (tmp_path / 'a.pptx').read_text() == 'a'
        assert (tmp_path / 'b.pptx').read_text() == 'b'
        waiting = await db.get_agent_run(row["run_id"])
        assert waiting["status"] == "queued" and waiting["queue_reason"] == "workspace_conflict"
        events = await db.list_agent_events(row['run_id'])
        assert any(event['event_kind'] == 'agent_waiting' for event in events)
        release.set()
        for _ in range(100):
            if "conflict" in started: break
            await asyncio.sleep(.02)
        assert "conflict" in started
    finally:
        release.set()
        await supervisor.stop()


async def test_sensitive_routes_never_return_raw_record(tmp_database, tmp_path):
    import server
    conv = await db.create_conversation("Private", mode="local")
    private = await db.create_agent_run("private", conv["id"], "secret task", project_dir=str(tmp_path),
                                         access={"read": ["."], "write": [], "sensitive": True})
    await db.update_agent_run(private["run_id"], result={"text": "secret result"}, error="secret error")
    assert "secret" not in json.dumps(await server.api_get_agent_run("private"))
    assert "secret" not in json.dumps(await server.api_list_agent_runs(conv["id"]))
    with pytest.raises(server.HTTPException) as refused:
        await server.api_list_agent_events("private")
    assert refused.value.status_code == 403


def test_lean_oracle():
    oracle = json.loads((Path(__file__).resolve().parents[1] / "formal/agent_access/cases.json").read_text())
    for section, function in (("conflicts", access.conflict_flags), ("disclosure", access.may_disclose),
                              ("admission", access.admitted), ("inheritance", access.inherits)):
        for flags, expected in oracle[section]:
            assert function(*flags) == expected


async def test_sensitive_admission_and_descendants_fail_closed(tmp_database, tmp_path):
    conv = await db.create_conversation("Private", mode="codex")
    supervisor = AgentSupervisor()
    with pytest.raises(ValueError, match="local inference"):
        await supervisor.spawn(conv["id"], "private", harness="codex", project_dir=str(tmp_path),
                               access={"read": ["."], "sensitive": True})
    parent = await supervisor.spawn(conv["id"], "private", harness="local", project_dir=str(tmp_path),
                                    access={"read": ["inputs"], "write": ["output"], "sensitive": True})
    await db.update_agent_run(parent["run_id"], status="running")
    with pytest.raises(ValueError):
        await supervisor.spawn(conv["id"], "cloud descendant", parent_run_id=parent["run_id"], harness="codex")
    with pytest.raises(ValueError):
        await supervisor.spawn(conv["id"], "broader descendant", parent_run_id=parent["run_id"],
                               access={"read": ["."], "sensitive": True})
    child = await supervisor.spawn(conv["id"], "narrow descendant", parent_run_id=parent["run_id"],
                                   access={"read": ["inputs/a"], "sensitive": True})
    assert child["access"]["sensitive"] and not child["access"]["write"]


async def test_release_requires_human_and_never_releases_raw_text(tmp_database, tmp_path, monkeypatch):
    import server
    from starlette.requests import Request
    conv = await db.create_conversation("Private", mode="local")
    await db.create_agent_run("release", conv["id"], "private task", project_dir=str(tmp_path),
                              access={"read": [], "write": [], "sensitive": True})
    await db.update_agent_run("release", status="completed", result={"text": "raw secret"})
    request = Request({"type": "http", "headers": []})
    monkeypatch.setattr(server.loom_auth, "credential", lambda scope: None)
    with pytest.raises(server.HTTPException) as refused:
        await server.api_release_agent_result("release", {"text": "approved"}, request)
    assert refused.value.status_code == 403
    monkeypatch.setattr(server.loom_auth, "credential", lambda scope: "browser")
    assert (await server.api_release_agent_result("release", {"text": "approved"}, request))["released"]
    projected = await server.api_get_agent_run("release")
    assert projected["result"] == {"text": "approved"} and "raw secret" not in json.dumps(projected)


async def test_sensitive_events_do_not_leak_to_unpaired_socket(tmp_database, tmp_path, monkeypatch):
    import server
    from event_router import SubscriptionRouter
    conv = await db.create_conversation("Private", mode="local")
    private = await db.create_agent_run("private-events", conv["id"], "secret", project_dir=str(tmp_path),
                                         access={"read": [], "write": [], "sensitive": True})
    class Socket:
        scope = {}
        def __init__(self): self.frames = []
        async def send_json(self, frame): self.frames.append(frame)
    socket = Socket()
    router = SubscriptionRouter()
    router.register(socket, conv["id"])
    router.subscribe(socket, "agent:private-events")
    monkeypatch.setattr(server, "_event_router", router)
    monkeypatch.setattr(server, "_active_websockets", {conv["id"]: {socket}})
    monkeypatch.setattr(server.loom_auth, "credential", lambda scope: None)
    event = await db.append_agent_event("private-events", "agent_completed", {"status": "completed", "result": {"text": "secret"}})
    await server._publish_agent_event(private, event)
    assert socket.frames and "secret" not in json.dumps(socket.frames)
    assert socket.frames[0]["status"] == "completed"
    event = await db.append_agent_event("private-events", "status", {"status": "secret diagnostic", "queue_reason": "secret queue"})
    await server._publish_agent_event(private, event)
    assert "secret" not in json.dumps(socket.frames)


async def test_sensitive_native_launcher_disables_escape_tools(tmp_database, tmp_path, monkeypatch):
    import server
    from unittest.mock import AsyncMock
    import child_context
    conv = await db.create_conversation("Private launch", mode="local", project_dir=str(tmp_path))
    private = await db.create_agent_run("private-launch", conv["id"], "inspect input", project_dir=str(tmp_path),
                                         requested_harness="local", access={"read": ["."], "write": [], "sensitive": True})
    captured = {}
    async def launch(prompt, cwd, **kwargs):
        captured.update(kwargs)
        async def events():
            yield {"type": "result", "text": "private result"}
        return None, events()
    monkeypatch.setattr(server.claude_client, "run_claude", launch)
    monkeypatch.setattr(child_context, "prepare", AsyncMock(side_effect=lambda row, prompt, **kw: prompt))
    result = await server._launch_agent_run(private, AsyncMock())
    assert result["text"] == "private result" and captured["use_llama"]
    assert {"Agent", "Task", "Bash", "PowerShell", "WebFetch"} <= set(captured["extra_disallowed_tools"])
