"""Independent child evidence, shared recovery, and ownership negatives."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import database as db
import child_context
import context_runtime
import server
from agent_supervisor import AgentSupervisor
from continuity_engine import Checkpoint, SummaryResult, fingerprint
from replay_budget import PayloadBudget


async def recorded_child(tmp_path, harness="codex", model=None):
    conv = await db.create_conversation("Synthetic child recovery", project_dir=str(tmp_path))
    parent = await db.add_message(conv["id"], "user", "PARENT_SECRET_NOT_IN_CHILD")
    run = await db.create_agent_run("recovered-child", conv["id"], "Finish ORCHID-17 review; preserve key.md",
                                    requested_harness=harness,
                                    requested_model=model or {"openrouter": "openrouter:z-ai/glm-5.3-flash",
                                                     "umans": "umans:claude-sonnet-4-6"}.get(harness),
                                    parent_message_id=parent["id"], project_dir=str(tmp_path))
    await db.append_agent_event(run["run_id"], "tool_start", {"id": "saved", "name": "write_file"})
    await db.append_agent_event(run["run_id"], "tool_result", {"id": "saved", "output": "key.md SAVED_ONCE"})
    await db.append_agent_event(run["run_id"], "text_delta", {"text": "KEEP_KEY_PRIVATE " + "old detail " * 8000})
    await db.append_agent_event(run["run_id"], "tool_start", {"id": "uncertain", "name": "publish"})
    await db.append_agent_event(run["run_id"], "agent_paused", {"status": "paused"})
    await db.update_agent_run(run["run_id"], status="paused", provider_session_id="unconfirmed-native")
    return conv, run


@pytest.mark.parametrize("harness,model", [
    *((h, None) for h in ["codex", "gemini", "goose", "claude", "local", "openrouter", "umans", "dream-claude"]),
    ("goose", "goose:llama:synthetic-model"),
    ("goose", "goose:auto:llama:synthetic-model"),
    ("goose", "goose:auto:local:synthetic-model"),
])
async def test_child_recovery_all_launch_lanes_keep_owned_evidence(tmp_database, tmp_path, monkeypatch, harness, model):
    conv, run = await recorded_child(tmp_path, harness, model)
    calls, summaries, retrievals = [], [], []
    class Summary:
        identity = "synthetic-local"
        def __init__(self, *a): pass
        async def summarize(self, text, max_tokens):
            summaries.append(text)
            assert "PARENT_SECRET_NOT_IN_CHILD" not in text
            assert "SAVED_ONCE" in text and "uncertain" in text
            return SummaryResult("KEEP_KEY_PRIVATE. key.md SAVED_ONCE. publish outcome uncertain; reconcile first.", self.identity)
    class Counter:
        def __init__(self, *a): pass
        async def count(self, payload): return len(payload["messages"][0]["content"]) // 3
        async def close(self): pass
    import local_context_guard
    monkeypatch.setattr(local_context_guard, "LocalContextGuard", Counter)
    monkeypatch.setattr(context_runtime, "ConfiguredLocalSummarizer", Summary)
    monkeypatch.setattr(context_runtime, "assess_payload", AsyncMock(return_value=PayloadBudget(
        16000, "synthetic capacity", 0, 0, 1000, 0, 1000, 1000)))
    async def launch(prompt, *args, **kwargs):
        calls.append((prompt, kwargs))
        token = prompt.split("context_scope:", 1)[1].split()[0]
        listing = await child_context.retrieve(token, run["run_id"])
        for item in listing["messages"]:
            retrievals.append(await child_context.retrieve(token, run["run_id"], message_id=item["id"]))
        async def events():
            yield {"type": "session_info", "session_id": "recovered-native"}
            yield {"type": "result", "result_text": "ORCHID-17 reviewed; existing key preserved"}
        return SimpleNamespace(pid=None, returncode=0), events()
    for adapter, method in ((server.codex_client, "run_codex"), (server.gemini_client, "run_gemini"),
                            (server.goose_client, "run_goose"), (server.claude_client, "run_claude")):
        monkeypatch.setattr(adapter, method, launch)
    original = await db.list_agent_events(run["run_id"])
    resumed = await db.request_agent_resume(run["run_id"])
    assert resumed["provider_session_id"] is None  # unconfirmed interrupted state is not native-resume evidence
    supervisor = AgentSupervisor(server._launch_agent_run)
    await supervisor._run_one(resumed)
    saved = await db.get_agent_run(run["run_id"])
    assert saved["status"] == "completed", saved["error"]
    assert len(calls) == 1 and summaries
    if model is not None:
        assert context_runtime.assess_payload.await_args.kwargs["backend"] == "llama"
    prompt, kwargs = calls[0]
    assert len(prompt) < 8000
    assert "PARENT_SECRET_NOT_IN_CHILD" not in prompt
    assert "SAVED_ONCE" in prompt and "uncertain" in prompt
    assert "do not restart completed work" in prompt
    assert kwargs["permission_scope"] == "agent:recovered-child"
    if harness in {"goose", "claude", "local", "openrouter", "umans", "dream-claude"}:
        assert kwargs["context_replay_history"] in prompt
        assert "Explicitly resumed child task" not in kwargs["context_replay_history"]
    assert any("SAVED_ONCE" in r["record_fragment"] for r in retrievals)
    assert (await db.list_agent_events(run["run_id"]))[:len(original)] == original
    connection = await db.get_db()
    assert len(await connection.execute_fetchall("SELECT id FROM messages WHERE conversation_id=?", (conv["id"],))) == 1
    scopes = await (await db.get_db()).execute_fetchall("SELECT * FROM agent_context_scopes")
    assert len(scopes) == 1
    info = next(e["payload"] for e in await db.list_agent_events(run["run_id"]) if e["event_kind"] == "agent_launch_info")
    assert info["context_plan"]["evidence_kind"] == "child_events"
    assert "covered_message_ids" not in info["context_plan"]


async def test_child_scope_highwater_and_sibling_parent_denial(tmp_database, tmp_path):
    conv, run = await recorded_child(tmp_path)
    sibling = await db.create_agent_run("sibling", conv["id"], "SIBLING_PRIVATE")
    evidence, highwater = await child_context.snapshot(run["run_id"])
    note = await child_context.notice(run["run_id"], evidence, highwater)
    token = note.split("context_scope:", 1)[1].strip()
    later = await db.append_agent_event(run["run_id"], "text_delta", {"text": "outside frozen prefix"})
    with pytest.raises(PermissionError):
        await child_context.retrieve(token, sibling["run_id"])
    from context_checkpoints import retrieve
    with pytest.raises(PermissionError):
        await retrieve(token, conv["id"])
    with pytest.raises(PermissionError):
        await child_context.retrieve(token, run["run_id"], message_id=later["sequence"])
    cp = Checkpoint(conv["id"], fingerprint(evidence.messages), tuple(m["id"] for m in evidence.messages),
                    "child summary", "synthetic-local")
    store = child_context.ChildCheckpointStore(run["run_id"])
    await store.save(cp)  # later appended events do not invalidate frozen prefix
    assert await store.find(conv["id"], cp.fingerprint, cp.summary_route) == cp
    assert await child_context.ChildCheckpointStore(sibling["run_id"]).find(conv["id"], cp.fingerprint, cp.summary_route) is None
    async with db.write_transaction() as connection:
        await connection.execute("UPDATE agent_runs SET task='edited' WHERE run_id=?", (run["run_id"],))
    with pytest.raises(PermissionError, match="edited"):
        await child_context.retrieve(token, run["run_id"])
    with pytest.raises(ValueError, match="changed"):
        await store.save(cp)


async def test_child_summary_failure_launches_nothing_and_keeps_evidence(tmp_database, tmp_path, monkeypatch):
    conv, run = await recorded_child(tmp_path)
    launch = AsyncMock(side_effect=AssertionError("must not launch"))
    monkeypatch.setattr(server.codex_client, "run_codex", launch)
    monkeypatch.setattr(context_runtime, "assess_payload", AsyncMock(side_effect=ValueError("backend unavailable")))
    before = await db.list_agent_events(run["run_id"])
    await AgentSupervisor(server._launch_agent_run)._run_one(await db.request_agent_resume(run["run_id"]))
    saved = await db.get_agent_run(run["run_id"])
    assert saved["status"] == "failed" and "backend unavailable" in saved["error"]
    launch.assert_not_called()
    assert (await db.list_agent_events(run["run_id"]))[:len(before)] == before


async def test_cancel_during_child_preparation_never_launches_provider(tmp_database, tmp_path, monkeypatch):
    conv, run = await recorded_child(tmp_path)
    entered = asyncio.Event()
    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(context_runtime, "assess_payload", blocked)
    launch = AsyncMock(side_effect=AssertionError("must not launch"))
    monkeypatch.setattr(server.codex_client, "run_codex", launch)
    task = asyncio.create_task(AgentSupervisor(server._launch_agent_run)._run_one(await db.request_agent_resume(run["run_id"])))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.wait_for(task, 2)
        assert (await db.get_agent_run(run["run_id"]))["status"] == "cancelled"
        launch.assert_not_called()
        rows = await (await db.get_db()).execute_fetchall("SELECT owner_id FROM agent_context_scopes")
        import context_scope_lifetime
        assert rows and not context_scope_lifetime.active(rows[0]["owner_id"])
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
