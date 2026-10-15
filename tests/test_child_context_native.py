"""Opt-in native explicit child recovery through the real supervisor/MCP path."""
import asyncio
import os
import subprocess
import uuid

import pytest

import database as db
import child_context
import server
from agent_supervisor import AgentSupervisor
from tests.test_context_retrieval_transport import retrieval_listener


@pytest.mark.skipif(os.getenv("LOOM_RUN_NATIVE_CONTINUITY") != "1", reason="Opt-in native inference")
@pytest.mark.parametrize("harness", ["codex", "goose"])
async def test_native_paused_child_recovers_own_evidence(retrieval_listener, tmp_path, monkeypatch, harness):
    workspace = tmp_path / "disposable-child"
    workspace.mkdir()
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True, capture_output=True)
    conv = await db.create_conversation("Synthetic native child recovery", project_dir=str(workspace))
    await db.update_conversation_fields(conv["id"], permission_profile="balanced")
    parent = await db.add_message(conv["id"], "user", "PARENT_CONTENT_MUST_NOT_BE_INHERITED")
    for key in ("LOOM_AGENT_RUN_ID", "LOOM_PARENT_RUN_ID", "LOOM_PERMISSION_SCOPE", "LOOM_CONV_ID"):
        monkeypatch.delenv(key, raising=False)
    model = (os.getenv("LOOM_CONTINUITY_CODEX_MODEL", "gpt-5.6-luna") if harness == "codex"
             else "goose:llama:" + server.config.llama_model)
    run = await db.create_agent_run("native-recover", conv["id"],
        "Read-only recovery acceptance. Use ONLY loom-agent-control.retrieve_context with the context_scope "
        "provided by Loom. First fetch the index, then read record 2 (message_id=2). "
        "Report the RECOVERED_ proof from its earlier output and the tool's saved status. "
        "Never repeat the saved action, run shell, read files, spawn children, or reveal the scope token.",
        requested_harness=harness, requested_model=model, project_dir=str(workspace), parent_message_id=parent["id"])
    proof = "RECOVERED_" + uuid.uuid4().hex
    await db.append_agent_event(run["run_id"], "tool_result", {"tool_id": "saved", "content": proof + " SAVED_ONCE"})
    await db.append_agent_event(run["run_id"], "agent_paused", {"status": "paused"})
    await db.update_agent_run(run["run_id"], status="paused", provider_session_id="not-a-verified-terminal-session")
    before = await db.list_agent_events(run["run_id"])
    calls = []
    original = child_context.retrieve
    async def observed(token, run_id, **kwargs):
        result = await original(token, run_id, **kwargs)
        calls.append((run_id, kwargs.get("message_id")))
        return result
    monkeypatch.setattr(child_context, "retrieve", observed)
    resumed = await db.request_agent_resume(run["run_id"])
    await asyncio.wait_for(AgentSupervisor(server._launch_agent_run)._run_one(resumed), 180)
    saved = await db.get_agent_run(run["run_id"])
    assert saved["status"] == "completed", saved["error"]
    assert (run["run_id"], 2) in calls, "native child did not retrieve its own exact evidence"
    assert proof in saved["result"]["text"] and "SAVED_ONCE" in saved["result"]["text"]
    assert saved["pid"] is None and saved["provider_session_id"] != "not-a-verified-terminal-session"
    assert (await db.list_agent_events(run["run_id"]))[:len(before)] == before
    rows = await (await db.get_db()).execute_fetchall("SELECT id FROM messages WHERE conversation_id=?", (conv["id"],))
    assert len(rows) == 1
