"""Opt-in short native Goose/local <-> Codex continuity, fully isolated Loom DB/port.

This tests native handoff and subsequent resume, NOT forced context exhaustion.
No live Loom lifecycle endpoint, process, or user conversation is used.
"""
import asyncio
import json
import os
from pathlib import Path

import pytest
import uvicorn

import database as db
import server
from event_router import SubscriptionRouter
from tests.test_fullstack_queue_isolation import _free_port, _eventually


@pytest.mark.skipif(os.getenv("LOOM_RUN_NATIVE_CONTINUITY") != "1", reason="Opt-in native model usage")
@pytest.mark.parametrize("manual,overflow", [(False, False), (True, False), (False, True)],
                         ids=["roundtrip", "manual-checkpoint", "local-overflow"])
async def test_native_goose_codex_roundtrip_and_followups(tmp_database, tmp_path, monkeypatch, manual, overflow, record_property):
    workspace = tmp_path / "disposable-workspace"
    workspace.mkdir()
    for name in ("_active_generations", "_generation_snapshots", "_active_websockets",
                 "_pending_hook_permissions", "_permission_rules"):
        monkeypatch.setattr(server, name, {})
    monkeypatch.setattr(server, "_event_router", SubscriptionRouter())
    port = _free_port()
    monkeypatch.setattr(server.config, "port", port)
    monkeypatch.setattr(server.config, "enable_goose", True)
    certs = Path(server.__file__).parent / "certs"
    tls = all((certs / name).is_file() for name in ("cert.pem", "key.pem"))
    ssl_args = {"ssl_certfile": str(certs / "cert.pem"), "ssl_keyfile": str(certs / "key.pem")} if tls else {}
    listener = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port,
                                             ws="websockets-sansio",
                                             lifespan="off", log_level="warning",
                                             timeout_graceful_shutdown=3, **ssl_args))
    serving = asyncio.create_task(listener.serve())
    await _eventually(lambda: listener.started)
    conv = await db.create_conversation("Synthetic native continuity", mode="goose", project_dir=str(workspace))
    await db.update_conversation_fields(conv["id"], parallel_agents_enabled=1)
    goose_model = "goose:llama:" + server.config.llama_model
    codex_model = os.getenv("LOOM_CONTINUITY_CODEX_MODEL", "codex-gpt-5.6-luna")
    steps = [
        ("goose", goose_model, "Remember synthetic project code ORCHID-17 and status pending. Reply only ORCHID-17 pending. Do not call tools.", False),
        ("goose", goose_model, "For the same code, change status to reviewed. Reply with code and status only. Do not call tools.", True),
        ("codex", codex_model, "Continue this task. State its code and current status only. Do not call tools.", False),
        ("goose", goose_model, "Continue this task. State its code and current status only. Do not call tools.", False),
        ("goose", goose_model, "Again state its code and current status only. Do not call tools.", True),
    ]
    if manual:
        steps.insert(2, ("goose", goose_model,
                        "After the checkpoint, state the project code and current status only. Do not call tools.", False))
    measurements = []
    if overflow:
        from local_context_guard import LocalContextGuard
        probe = LocalContextGuard(server.config.llama_host_url())
        try:
            capacity = await probe.capacity(server.config.llama_model)
        finally:
            await probe.close()
        # Actual live capacity/tokenizer, not a fake model cap or server restart.
        # Short repeated tokens keep the byte estimate below exact admission;
        # two individually fitting turns together force native context pressure.
        steps = [
            ('goose', goose_model, 'Synthetic historical filler; no actions: ' + ' x' * int(capacity * .52)
             + '\nRemember ORCHID-17 pending. Reply only ORCHID-17 pending. Do not call tools.', False),
            ('goose', goose_model, 'More synthetic reference material; no actions: ' + ' y' * int(capacity * .40)
             + '\nChange the previously recorded project status to reviewed. Reply with its code and status only. Do not call tools.', True),
            ('goose', goose_model, 'State the project code and latest status only. Do not call tools.', True),
        ]
        original_prepare = LocalContextGuard.prepare
        async def measured(guard, payload, *, anthropic=False):
            before = await guard.count(payload, anthropic)
            cap = await guard.capacity(payload.get('model'))
            reserve = int(payload.get('max_completion_tokens', payload.get('max_tokens', 8192)) or 8192)
            limit = cap - reserve - max(256, cap // 10)
            from replay_budget import ReplayBudgetExceeded
            try:
                prepared = await original_prepare(guard, payload, anthropic=anthropic)
            except ReplayBudgetExceeded:
                measurements.append(dict(before=before, after=None, limit=limit, capacity=cap, rejected=True))
                raise
            after = await guard.count(prepared, anthropic)
            measurements.append(dict(before=before, after=after, limit=limit, capacity=cap))
            assert after <= limit
            return prepared
        monkeypatch.setattr(LocalContextGuard, 'prepare', measured)
    parent = None
    try:
        for index, (harness, model, text, resumed) in enumerate(steps):
            await db.update_conversation_fields(conv["id"], mode=harness, cc_model=model, cc_effort="low")
            current = await db.add_message(conv["id"], "user", text, parent_id=parent)
            handler = server._handle_goose_generation if harness == "goose" else server._handle_claude_generation
            # Only this fixture-owned handler/provider is cancelled on timeout.
            await asyncio.wait_for(handler(None, conv["id"], await db.get_conversation(conv["id"]),
                                           {"action": "generate", "parent_id": current["id"], "cc_model": model}), 300 if overflow else 180)
            leaf = await db.get_active_leaf(conv["id"])
            assert leaf["role"] == "assistant" and leaf["parent_id"] == current["id"]
            assert leaf["turn_lifecycle_state"] not in {"failed", "partial_failed", "cancelled"}, "native turn failed"
            assert "ORCHID-17" in leaf["content"]
            assert ("pending" if index == 0 else "reviewed") in leaf["content"].lower()
            assert leaf["cc_session_id"]
            attestation = leaf.get("model_attestation") or {}
            if isinstance(attestation, str):
                attestation = json.loads(attestation)
            if overflow and index > 0 and attestation.get("session_continuity") == "rebuilt":
                assert attestation.get("session_continuity_reason") == "context_exhausted_at_recorded_boundary"
            else:
                assert attestation.get("session_continuity") == ("forked" if resumed else "rebuilt")
            parent = leaf["id"]
            if manual and index == 1:
                cursor = await db.select_loom_cursor(conv["id"], "native-checkpoint-page",
                                                      leaf["loom_thread_id"], parent)
                await db.update_conversation_fields(conv["id"], parallel_agents_enabled=0)
                _, task = await server._launch_generation_task(None, conv["id"], {
                    "action": "compact", "parent_id": parent,
                    "client_instance_id": "native-checkpoint-page",
                    "loom_thread_id": cursor["thread_id"], "cursor_revision": cursor["revision"],
                }, "native-checkpoint-page")
                await asyncio.wait_for(task, 180)
                connection = await db.get_db()
                assert await connection.execute_fetchall("SELECT * FROM context_manual_boundaries")
                assert await db.get_client_loom_cursor(conv["id"], "native-checkpoint-page") == cursor
                await db.update_conversation_fields(conv["id"], parallel_agents_enabled=1)
        if overflow:
            record_property('native_local_admission_counts', json.dumps(measurements))
            assert any(m['before'] > m['limit'] for m in measurements), 'Fixture did not actually exercise overflow'
            assert all(m['after'] <= m['limit'] for m in measurements if m['after'] is not None)
    finally:
        # Exact handle created above; never discovers or signals live hosts.
        listener.should_exit = True
        await asyncio.wait_for(serving, 10)
