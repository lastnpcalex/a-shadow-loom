"""Real Goose tool/continuation with a boundary-injected overflow terminal.

This tests native continuation from real tool evidence, NOT genuine capacity
exhaustion (the separate local-overflow test covers measured admission).
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from fastapi import Body, HTTPException

import database as db
import server
from tests.test_context_retrieval_transport import retrieval_listener


@pytest.mark.skipif(os.getenv("LOOM_RUN_NATIVE_CONTINUITY") != "1", reason="Opt-in native inference")
async def test_native_tool_outcome_survives_top_level_recovery(retrieval_listener, tmp_path, monkeypatch):
    workspace = tmp_path / "native-boundary"
    workspace.mkdir()
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True, capture_output=True)
    nonce, proof = uuid.uuid4().hex, "SAVED_" + uuid.uuid4().hex
    counts, launches = [], []
    conv = await db.create_conversation("Synthetic native tool boundary", mode="goose", project_dir=str(workspace))
    await db.update_conversation_fields(conv["id"], parallel_agents_enabled=1, permission_profile="balanced")
    monkeypatch.setattr(server, "_pending_hook_permissions", {})
    for key in ("LOOM_AGENT_RUN_ID", "LOOM_PARENT_RUN_ID", "LOOM_PERMISSION_SCOPE", "LOOM_CONV_ID"):
        monkeypatch.delenv(key, raising=False)
    path = "/_test_boundary_" + nonce
    async def effect(data: dict = Body(...)):
        if data.get("nonce") != nonce:
            raise HTTPException(403)
        if data.get("operation") == "record":
            counts.append("mutation")
        return {"receipt":proof, "writes":len(counts)} if counts else {"status":"not_recorded"}
    server.app.add_api_route(path, effect, methods=["POST"])
    route = server.app.routes[-1]
    certs = Path(server.__file__).parent / "certs"
    scheme = "https" if all((certs / p).is_file() for p in ("cert.pem", "key.pem")) else "http"
    spec = {"name":"fault-fixture", "command":sys.executable, "env":[],
            "args":[str(Path(__file__).parent / "fixtures/child_fault_mcp.py"), f"{scheme}://127.0.0.1:{retrieval_listener}{path}"]}
    native_mcp = server.goose_client._loom_mcp_servers
    monkeypatch.setattr(server.goose_client, "_loom_mcp_servers", lambda *a, **k: [*native_mcp(*a, **k), spec])
    native = server.goose_client.run_goose
    async def launch(*args, **kwargs):
        proc, stream = await native(*args, **kwargs)
        launches.append(proc)
        if len(launches) != 1:
            return proc, stream
        async def faulted():
            try:
                async for event in stream:
                    yield event
                    if event.get("type") == "tool_result" and proof in str(event.get("content")):
                        # Deliberately injected terminal, not a fabricated native
                        # tokenizer measurement. Stop only this fixture's provider.
                        await server._teardown_child_provider(proc, "goose")
                        yield {"type":"error", "message":"context window exceeded"}
                        yield {"type":"result", "is_error":True, "error":"context window exceeded"}
                        return
            finally:
                await stream.aclose()
        return proc, faulted()
    monkeypatch.setattr(server.goose_client, "run_goose", launch)
    async def approvals():
        while True:
            for rid, pending in list(server._pending_hook_permissions.items()):
                if pending.get("conv_id") != conv["id"] or pending.get("response") is not None:
                    continue
                args = pending.get("tool_input") or {}
                # Only this synthetic capability, including a duplicate if the
                # model attempts one: count it rather than masking it by denial.
                allowed = args.get("nonce") == nonce and args.get("operation") in {None, "record", "inspect"}
                await server._answer_pending_permission(rid, {"allow":allowed,
                    "feedback":"Use only the specified fixture MCP tools." if not allowed else ""})
            await asyncio.sleep(.025)
    answering = asyncio.create_task(approvals())
    model = "goose:llama:" + server.config.llama_model
    user = await db.add_message(conv["id"], "user",
        f"Call fault-fixture record_action once with nonce {nonce} and operation record. "
        "Never repeat an action with a saved receipt. Return only its SAVED_ receipt and writes count. "
        "No shell, filesystem tools or delegation. If a receipt is already in the retained evidence, report it.")
    try:
        await asyncio.wait_for(server._handle_goose_generation(None, conv["id"], await db.get_conversation(conv["id"]),
            {"action":"generate", "parent_id":user["id"], "cc_model":model}), 240)
        leaf = await db.get_active_leaf(conv["id"])
        assert len(launches) == 2 and counts == ["mutation"], leaf["content"]
        assert leaf["parent_id"] == user["id"]
        assert leaf["turn_lifecycle_state"] not in {"failed", "partial_failed", "cancelled"}
        assert proof in leaf["content"]
        assert any(b.get("outcome_recorded") for b in json.loads(leaf["content_blocks"]))
        assert all(p.returncode is not None for p in launches)
    finally:
        answering.cancel()
        await asyncio.gather(answering, return_exceptions=True)
        server.app.routes.remove(route)
        for proc in launches:
            if proc.returncode is None:
                await server._teardown_child_provider(proc, "goose")
