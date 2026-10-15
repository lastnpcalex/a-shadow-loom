"""Opt-in remaining native pairs. Short-context acceptance, not overflow proof."""
import asyncio
import json
import os
import re
import subprocess

import pytest

import database as db
import server
from event_router import SubscriptionRouter
from tests.test_context_retrieval_transport import retrieval_listener


@pytest.mark.skipif(os.getenv("LOOM_RUN_NATIVE_CONTINUITY") != "1", reason="Opt-in native inference")
@pytest.mark.parametrize("source,target", [
    ("codex", "goose"), ("codex", "gemini"), ("goose", "gemini"),
    ("local", "codex"), ("local", "goose"), ("local", "gemini"),
])
async def test_native_pair_roundtrip_and_both_resumes(retrieval_listener, tmp_path, monkeypatch, source, target, record_property):
    workspace = tmp_path / "disposable-pair"
    workspace.mkdir()
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True, capture_output=True)
    for name in ("_active_generations", "_generation_snapshots", "_active_websockets",
                 "_pending_hook_permissions", "_permission_rules"):
        monkeypatch.setattr(server, name, {})
    monkeypatch.setattr(server, "_event_router", SubscriptionRouter())
    monkeypatch.setattr(server.config, "enable_goose", True)
    models = {"codex": os.getenv("LOOM_CONTINUITY_CODEX_MODEL", "codex-gpt-5.6-luna"),
              "goose": "goose:llama:" + server.config.llama_model,
              "local": server.config.llama_model,
              "gemini": os.getenv("LOOM_CONTINUITY_GEMINI_MODEL", "Gemini 3.8 Flash (Low)")}
    conv = await db.create_conversation("Synthetic native pair", mode=source, project_dir=str(workspace))
    await db.update_conversation_fields(conv["id"], parallel_agents_enabled=1, permission_profile="balanced")
    concise = " Return exactly one line: the project identifier, a space, and its status. No explanation or tools."
    steps = [(source, "Remember project identifier ORCHID-17 with status pending." + concise, False, "pending"),
             (source, "Correct its status to reviewed." + concise, True, "reviewed"),
             (target, "Continue the remembered project." + concise, False, "reviewed"),
             (target, "Report the same project's latest state." + concise, True, "reviewed"),
             (source, "Return to the remembered project." + concise, False, "reviewed"),
             (source, "Report the same project's latest state." + concise, True, "reviewed")]
    parent = None
    denied = []
    for index, (harness, prompt, resumed, expected) in enumerate(steps):
        await db.update_conversation_fields(conv["id"], mode=harness, cc_model=models[harness],
                                            local_model=server.config.llama_model, cc_effort="low")
        current = await db.add_message(conv["id"], "user", prompt, parent_id=parent)
        handler = ({"goose": server._handle_goose_generation, "local": server._handle_local_generation}
                   .get(harness, server._handle_claude_generation))
        async def deny_unexpected_permissions():
            # Simulate a test user declining unexpected mutations, never
            # auto-approve writes merely to make an unattended test pass.
            while True:
                for request_id, pending in list(server._pending_hook_permissions.items()):
                    if pending.get("conv_id") == conv["id"] and pending.get("response") is None:
                        if await server._answer_pending_permission(request_id, {
                            "allow": False, "feedback": "Read-only acceptance: no persistent files or memory writes. "
                            "Keep the project identifier/status in this conversation and return the requested one line.",
                        }):
                            denied.append({"step": index, "harness": harness})
                await asyncio.sleep(.025)
        denier = asyncio.create_task(deny_unexpected_permissions())
        try:
            await asyncio.wait_for(handler(None, conv["id"], await db.get_conversation(conv["id"]),
                                           {"action": "generate", "parent_id": current["id"], "cc_model": models[harness]}), 150)
        finally:
            denier.cancel()
            await asyncio.gather(denier, return_exceptions=True)
            record_property("unexpected_permissions_denied", json.dumps(denied))
        leaf = await db.get_active_leaf(conv["id"])
        assert leaf["role"] == "assistant" and leaf["parent_id"] == current["id"]
        assert leaf["turn_lifecycle_state"] not in {"failed", "partial_failed", "cancelled"}, f"{harness} step {index} failed"
        assert re.fullmatch(r"ORCHID-17\s+" + expected + r"\.?", leaf["content"].strip(), re.I), f"{harness} lost task state or gave a jarring/refusal response"
        assert leaf["cc_session_id"]
        attestation = leaf.get("model_attestation") or {}
        if isinstance(attestation, str):
            attestation = json.loads(attestation)
        assert attestation.get("session_continuity") == ("forked" if resumed else "rebuilt"), (harness, index, attestation)
        parent = leaf["id"]
