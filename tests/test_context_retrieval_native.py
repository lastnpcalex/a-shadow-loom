"""Opt-in real model -> MCP -> scoped evidence; no production host/records."""
import asyncio
import os
import subprocess
import uuid

import pytest

import database as db
import server
from context_checkpoints import issue_retrieval_scope
from continuity_engine import BranchSnapshot
from tests.test_context_retrieval_transport import retrieval_listener


@pytest.mark.skipif(os.getenv("LOOM_RUN_NATIVE_CONTINUITY") != "1", reason="Opt-in native inference")
@pytest.mark.parametrize("harness", ["codex", "goose"])
async def test_native_model_retrieves_long_turn_evidence(retrieval_listener, tmp_path, monkeypatch, harness):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True, capture_output=True)
    conv = await db.create_conversation("Synthetic native retrieval", mode=harness, project_dir=str(workspace))
    await db.update_conversation_fields(conv["id"], permission_profile="balanced")
    proof = "RETRIEVED_" + uuid.uuid4().hex
    old = await db.add_message(conv["id"], "user", proof)
    evidence = BranchSnapshot.from_messages(await db.get_branch_to_root(old["id"]))
    token = await issue_retrieval_scope(evidence, ttl_seconds=1, owner=asyncio.current_task())
    async with db.write_transaction() as connection:
        await connection.execute("UPDATE context_retrieval_scopes SET expires_at=0")
    for name in ("LOOM_AGENT_RUN_ID", "LOOM_PARENT_RUN_ID", "LOOM_PERMISSION_SCOPE", "LOOM_CONV_ID"):
        monkeypatch.delenv(name, raising=False)
    prompt = ("Isolated read-only continuity acceptance. Use only loom-agent-control.retrieve_context, "
              "not shell or HTTP tools. Call it with the token below and message_id=" + str(old["id"]) +
              ". Return only the secret value stored in that record's content, exactly. Never display the token. "
              "If retrieval fails, report failure instead of guessing.\ncontext_scope: " + token)
    assert proof not in prompt
    calls = []
    original_retrieve = __import__("context_checkpoints").retrieve
    async def observed(*args, **kwargs):
        result = await original_retrieve(*args, **kwargs)
        calls.append(kwargs.get("message_id"))
        return result
    monkeypatch.setattr(__import__("context_checkpoints"), "retrieve", observed)
    proc = None
    async def execute():
        nonlocal proc
        if harness == "codex":
            proc, stream = await server.codex_client.run_codex(
                prompt, str(workspace), conv_id=conv["id"], server_port=retrieval_listener,
                model=os.getenv("LOOM_CONTINUITY_CODEX_MODEL", "gpt-5.6-luna"), effort="low")
        else:
            proc, stream = await server.goose_client.run_goose(
                prompt, conv_id=conv["id"], cwd=str(workspace), loom_port=retrieval_listener,
                model="goose:llama:" + server.config.llama_model)
        text, result, tool_names, tool_results = "", None, [], []
        async for item in stream:
            if item.get("type") == "tool_start":
                tool_names.append(item.get("name") or item.get("tool_name"))
            if item.get("type") == "tool_result":
                tool_results.append(str(item.get("content") or "").replace(token, "[TOKEN]"))
            if item.get("type") in {"text_delta", "stream_chunk"}:
                text += item.get("text") or item.get("content") or ""
            if item.get("type") == "result":
                result = item
                if not text:
                    text = item.get("result_text") or item.get("text") or ""
        assert result and not server._child_terminal_error(result), "native provider did not complete"
        if old["id"] not in calls:
            pytest.fail("No retrieval reached Loom. " + repr(tool_names) + " Results: " + repr(tool_results)
                        + " Response: " + text.replace(token, "[TOKEN]"))
        assert proof in text, "model did not recover the unknown evidence"
        assert token not in text, "model exposed the bearer token"
    try:
        await asyncio.wait_for(execute(), 150)
    finally:
        if proc is not None:
            await server._teardown_child_provider(proc, harness)
