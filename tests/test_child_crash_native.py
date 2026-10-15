"""Real provider death after a committed tool effect, before acknowledgement.

Kills only processes descended from the exact test-launched provider handle.
Never starts/stops/restarts a production Loom server or changes its records.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import psutil
import pytest
from fastapi import Body, HTTPException

import database as db
import server
from agent_supervisor import AgentSupervisor
from tests.test_context_retrieval_transport import retrieval_listener


pytestmark = pytest.mark.skipif(os.getenv("LOOM_RUN_NATIVE_CONTINUITY") != "1", reason="Opt-in native inference/process fault")


@pytest.mark.parametrize("source,target", [("codex", "goose"), ("goose", "codex")])
async def test_mid_tool_crash_then_cross_harness_reconciliation(retrieval_listener, tmp_path, monkeypatch, source, target):
    workspace = tmp_path / "fault-workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True, capture_output=True)
    nonce, proof = uuid.uuid4().hex, "RECEIPT_" + uuid.uuid4().hex
    committed, release = asyncio.Event(), asyncio.Event()
    receipt = workspace / "synthetic-receipt.json"
    counts = {"record": 0, "inspect": 0}
    phase = ["initial"]
    models = {"codex": os.getenv("LOOM_CONTINUITY_CODEX_MODEL", "gpt-5.6-luna"),
              "goose": "goose:llama:" + server.config.llama_model}
    conv = await db.create_conversation("Synthetic interrupted tool", project_dir=str(workspace))
    await db.update_conversation_fields(conv["id"], permission_profile="balanced")
    for name in ("LOOM_AGENT_RUN_ID", "LOOM_PARENT_RUN_ID", "LOOM_PERMISSION_SCOPE", "LOOM_CONV_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(server, "_pending_hook_permissions", {})
    path = "/_test_child_effect_" + nonce
    async def effect(data: dict = Body(...)):
        if data.get("nonce") != nonce or data.get("operation") not in counts:
            raise HTTPException(403)
        operation = data["operation"]
        counts[operation] += 1
        if operation == "record":
            # Count every invocation even if the file already exists: this is
            # a deliberately non-idempotent effect, not a dedup shortcut.
            with receipt.open("w", encoding="utf-8") as out:
                json.dump({"proof": proof, "commits": counts["record"]}, out)
                out.flush()
                os.fsync(out.fileno())
            committed.set()
            if phase[0] == "initial":
                await release.wait()  # no receipt reaches the first provider
        return json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else {"status": "not_recorded"}
    server.app.add_api_route(path, effect, methods=["POST"])
    route = server.app.routes[-1]
    certs = Path(server.__file__).parent / "certs"
    scheme = "https" if all((certs / p).is_file() for p in ("cert.pem", "key.pem")) else "http"
    spec = {"command": sys.executable, "args": [str(Path(__file__).parent / "fixtures" / "child_fault_mcp.py"),
              f"{scheme}://127.0.0.1:{retrieval_listener}{path}"]}
    codex_mcp, goose_mcp = server.codex_client._thread_mcp_servers, server.goose_client._loom_mcp_servers
    monkeypatch.setattr(server.codex_client, "_thread_mcp_servers", lambda *a, **k: {**codex_mcp(*a, **k), "fault-fixture": spec})
    monkeypatch.setattr(server.goose_client, "_loom_mcp_servers", lambda *a, **k: [*goose_mcp(*a, **k), {"name": "fault-fixture", **spec, "env": []}])
    processes = []
    native_calls = {}
    approval_frames = []
    decode = json.loads
    def capture_wire(value, *args, **kwargs):
        parsed = decode(value, *args, **kwargs)
        if isinstance(parsed, dict) and parsed.get("method") == "mcpServer/elicitation/request":
            approval_frames.append(parsed)
        return parsed
    monkeypatch.setattr(json, "loads", capture_wire)
    remember = server.goose_client.remember_tool_call
    def observe(state, call):
        remember(state, call)
        native_calls.update(state.get("permission_tool_calls", {}))
    monkeypatch.setattr(server.goose_client, "remember_tool_call", observe)
    for harness in ("codex", "goose"):
        adapter = getattr(server, harness + "_client")
        original = getattr(adapter, "run_" + harness)
        async def launch(*args, _original=original, **kwargs):
            proc, events = await _original(*args, **kwargs)
            processes.append(proc)
            return proc, events
        monkeypatch.setattr(adapter, "run_" + harness, launch)
    async def approvals():
        while True:
            for request_id, pending in list(server._pending_hook_permissions.items()):
                if pending.get("conv_id") != conv["id"] or pending.get("response") is not None:
                    continue
                args = pending.get("tool_input") or {}
                name = pending.get("tool_name") or ""
                identity = native_calls.get((args.get("acp_identity") or {}).get("tool_call_id"), {}).get("_meta", {}).get("goose", {}).get("toolCall", {})
                exact = name in {"mcp__fault-fixture__record_action", "mcp__fault-fixture__inspect_action"} or (
                    identity.get("extensionName") == "fault-fixture" and identity.get("toolName") in {
                        "record_action", "inspect_action", "fault-fixture__record_action", "fault-fixture__inspect_action"})
                operation = args.get("operation") or ("inspect" if "inspect_action" in (name + str(identity)) else "record")
                # Permit the exact synthetic operation even on recovery, so
                # an accidental duplicate is counted and FAILS the test.
                allowed = exact and args.get("nonce") == nonce and operation in {"inspect", "record"}
                await server._answer_pending_permission(request_id, {"allow": bool(allowed),
                    "feedback": "Use inspect_action to reconcile the existing receipt; no repeated writes or unrelated tools." if not allowed else ""})
            await asyncio.sleep(.025)
    run = await db.create_agent_run("fault-child", conv["id"],
        "Complete one synthetic action using only fault-fixture MCP tools. "
        f"Use nonce {nonce}. Call record_action at most once (operation=record). "
        "If its outcome is unknown, use inspect_action (operation=inspect) before considering any repeated action. "
        "Return the authoritative RECEIPT_ value and commit count. Do not use shell, files, memory, or delegation.",
        requested_harness=source, requested_model=models[source], project_dir=str(workspace))
    supervisor = AgentSupervisor(server._launch_agent_run)
    answering = asyncio.create_task(approvals())
    active = asyncio.create_task(supervisor._run_one(run))
    owned = []
    try:
        observed_commit = asyncio.create_task(committed.wait())
        try:
            await asyncio.wait_for(asyncio.wait({observed_commit, active}, return_when=asyncio.FIRST_COMPLETED), 150)
            assert committed.is_set(), "No committed fixture action; MCP approval frames: " + json.dumps(approval_frames)
        finally:
            observed_commit.cancel()
            await asyncio.gather(observed_commit, return_exceptions=True)
        assert not active.done()
        proc = processes[0]
        root = psutil.Process(proc.pid)
        owned = [root, *root.children(recursive=True)]
        # Fault injection: only this native test process tree, never a name/
        # port search or a live host handle. Store creation identity in psutil.
        for item in owned:
            try:
                item.kill()
            except psutil.NoSuchProcess:
                pass
        await asyncio.wait_for(active, 30)
        _, alive = await asyncio.to_thread(psutil.wait_procs, owned, timeout=5)
        assert not alive
        failed = await db.get_agent_run(run["run_id"])
        assert failed["status"] == "failed" and failed["pid"] is None
        old_events = await db.list_agent_events(run["run_id"], limit=5000)
        assert any(e["event_kind"] == "tool_start" for e in old_events)
        assert proof not in json.dumps(old_events), "first provider received the supposedly lost acknowledgement"
        assert counts["record"] == 1
        phase[0] = "recovery"
        await db.update_agent_run(run["run_id"], requested_harness=target, requested_model=models[target])
        resumed = await supervisor.resume(run["run_id"])
        assert resumed["status"] == "queued"
        active = asyncio.create_task(supervisor._run_one(resumed))
        await asyncio.wait_for(active, 180)
        saved = await db.get_agent_run(run["run_id"])
        assert saved["status"] == "completed", saved["error"]
        assert counts["inspect"] >= 1 and counts["record"] == 1
        assert proof in saved["result"]["text"]
        assert (await db.list_agent_events(run["run_id"], limit=5000))[:len(old_events)] == old_events
        assert saved["pid"] is None and len(processes) == 2
    finally:
        release.set()
        answering.cancel()
        if not active.done():
            active.cancel()
        await asyncio.gather(answering, active, return_exceptions=True)
        server.app.routes.remove(route)
        for proc in processes:
            if proc.returncode is None:
                await server._teardown_child_provider(proc, source if proc is processes[0] else target)
