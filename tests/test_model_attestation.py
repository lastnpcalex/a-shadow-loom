import json
from pathlib import Path

import pytest


def test_claude_code_records_harness_then_provider_model_evidence():
    import claude_client

    state = {
        "requested_model": "sonnet",
        "launch_model": "sonnet",
        "model_provider": "anthropic",
    }
    init = claude_client._process_event(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "claude-session",
            "model": "claude-sonnet-4-7",
        },
        state,
    )[0]
    provider = claude_client._process_event(
        {
            "type": "stream_event",
            "event": {
                "type": "message_start",
                "message": {"model": "claude-sonnet-4-7"},
            },
        },
        state,
    )[0]

    assert init["model_attestation"]["status"] == "verified"
    assert init["model_attestation"]["harness"] == "Claude Code"
    assert init["model_attestation"]["verification_level"] == "harness"
    assert provider["model_attestation"]["verification_level"] == "provider_response"
    assert provider["model_attestation"]["effective_model"] == "claude-sonnet-4-7"


def test_antigravity_attestation_distinguishes_launch_from_runtime():
    import gemini_client

    configured = gemini_client._agy_model_attestation(
        "gemini:gemini-3.7-flash-high",
        "gemini-3.7-flash-high",
        None,
        source="agy_cli_launch_arguments",
    )
    verified = gemini_client._agy_model_attestation(
        "gemini:gemini-3.7-flash-high",
        "gemini-3.7-flash-high",
        "gemini-3.7-flash-high",
        source="antigravity_stream_init",
    )

    assert configured["status"] == "configured"
    assert verified["status"] == "verified"
    assert verified["harness"] == "Antigravity (agy)"
    assert verified["model_provider"] == "google"


def test_goose_does_not_mislabel_process_configuration_as_verification():
    import goose_client

    configured = goose_client._goose_model_attestation(
        "goose:openrouter:z-ai/glm-5.2"
    )
    verified = goose_client._goose_model_attestation(
        "goose:openrouter:z-ai/glm-5.2",
        "openrouter:z-ai/glm-5.2",
        session_id="goose-session",
    )

    assert configured["status"] == "configured"
    assert configured["effective_model"] is None
    assert configured["source"] == "goose_process_environment"
    assert verified["status"] == "verified"
    assert verified["effective_model"] == "openrouter:z-ai/glm-5.2"


def test_direct_completion_attestation_uses_response_model():
    import llama_client

    verified = llama_client._completion_model_attestation(
        "Qwen3.6-27B.gguf",
        "Qwen3.6-27B.gguf",
        "Qwen3.6-27B.gguf",
        provider="llama-server",
        source="openai_compatible_stream_chunk",
    )
    mismatch = llama_client._completion_model_attestation(
        "openrouter:z-ai/glm-5.2",
        "z-ai/glm-5.2",
        "openai/gpt-5.6-luna",
        provider="openrouter",
        source="openrouter_stream_chunk",
    )

    assert verified["status"] == "verified"
    assert verified["harness"] == "Llama Server"
    assert mismatch["status"] == "mismatch"


@pytest.mark.asyncio
async def test_server_persists_and_broadcasts_turn_model_attestation(
    tmp_database, monkeypatch
):
    import database as db
    import server

    sent = []

    async def fake_ws_send(conv_id, payload):
        sent.append((conv_id, payload))

    monkeypatch.setattr(server, "_ws_send", fake_ws_send)
    conv = await db.create_conversation("Model identity", mode="claude")
    parent = await db.add_message(conv["id"], "user", "hello")
    draft = await db.add_message(
        conv["id"], "assistant", "draft", parent_id=parent["id"]
    )
    attestation = {
        "status": "verified",
        "harness": "Claude Code",
        "requested_model": "sonnet",
        "launch_model": "sonnet",
        "effective_model": "claude-sonnet-4-7",
        "model_provider": "anthropic",
        "source": "anthropic_compatible_message_start",
        "verification_level": "provider_response",
    }

    await server._record_turn_model_attestation(
        conv["id"], draft["id"], parent["id"], attestation
    )

    saved = await db.get_message(draft["id"])
    assert json.loads(saved["model_attestation"]) == attestation
    assert sent[-1][1]["type"] == "model_attestation"
    assert sent[-1][1]["attestation"]["harness"] == "Claude Code"


@pytest.mark.asyncio
async def test_visible_attestation_includes_verified_destination_preparation(
    tmp_database, monkeypatch
):
    import asyncio
    import database as db
    import server

    async def fake_ws_send(*_args):
        return None

    monkeypatch.setattr(server, "_ws_send", fake_ws_send)
    conv = await db.create_conversation("Preparation identity", mode="goose")
    parent = await db.add_message(conv["id"], "user", "continue")
    draft = await db.add_message(conv["id"], "assistant", "draft", parent_id=parent["id"])
    task = asyncio.current_task()
    old_trace = getattr(task, "_context_trace", None)
    task._context_trace = {
        "operation_id": "prep-1",
        "preparation_calls": [{
            "route": {"harness": "goose", "model": "glm-5.3-flash", "backend": "openrouter"},
            "attested_model": "glm-5.3-flash",
            "accepted": True,
        }],
    }
    try:
        recorded = await server._record_turn_model_attestation(
            conv["id"], draft["id"], parent["id"], {
                "status": "verified", "harness": "Goose ACP",
                "requested_model": "glm-5.3-flash", "launch_model": "glm-5.3-flash",
                "effective_model": "glm-5.3-flash", "source": "goose_acp_session_model_state",
            }
        )
    finally:
        if old_trace is None:
            delattr(task, "_context_trace")
        else:
            task._context_trace = old_trace

    assert recorded["status"] == "verified"
    assert recorded["preparation"]["status"] == "verified"
    assert recorded["preparation"]["calls"][0]["backend"] == "openrouter"


def test_hidden_different_preparation_model_cannot_render_green():
    import server

    task = type("TaskEvidence", (), {})()
    task._context_trace = {
        "operation_id": "prep-local",
        "preparation_calls": [{
            "route": {"harness": "local", "model": "qwen-local", "backend": "llama"},
            "attested_model": "qwen-local",
            "accepted": True,
        }],
    }
    result = server._attach_preparation_provenance({
        "status": "verified", "harness": "Goose ACP",
        "requested_model": "glm-5.3-flash", "launch_model": "glm-5.3-flash",
        "effective_model": "glm-5.3-flash",
    }, task=task)

    assert result["status"] == "unverified"
    assert result["preparation"]["calls"][0]["effective_model"] == "qwen-local"
    assert "not verified as the selected destination model" in result["identity_warning"]


def test_rejected_preparation_attempt_cannot_hide_behind_accepted_retry():
    import server

    task = type("TaskEvidence", (), {})()
    task._context_trace = {
        "operation_id": "prep-retry",
        "preparation_calls": [{
            "route": {"harness": "goose", "model": "glm-5.3-flash"},
            "attested_model": "glm-5.3-flash", "accepted": True,
        }],
        "preparation_attempts": [
            {
                "route": {"harness": "local", "model": "qwen-local", "backend": "llama"},
                "attested_model": "qwen-local", "accepted": False,
            },
            {
                "route": {"harness": "goose", "model": "glm-5.3-flash"},
                "attested_model": "glm-5.3-flash", "accepted": True,
            },
        ],
    }
    result = server._attach_preparation_provenance({
        "status": "verified", "harness": "Goose ACP",
        "requested_model": "glm-5.3-flash", "effective_model": "glm-5.3-flash",
    }, task=task)

    assert result["status"] == "unverified"
    assert result["preparation"]["call_count"] == 2
    assert result["preparation"]["calls"][0]["effective_model"] == "qwen-local"


def test_model_attestation_ui_labels_harness_and_evidence_level():
    source = (Path(__file__).resolve().parent.parent / "static" / "chat.js").read_text(
        encoding="utf-8"
    )

    assert "configured" in source
    assert "['Harness', att.harness]" in source
    assert "['Verification', att.verification_level]" in source
    assert "${escapeHtml(harness)} · ${escapeHtml(model)}" in source
    assert "Goose process configuration" in source
    assert "['Preparation', preparationLabel]" in source
    assert "Preparation ${index + 1}" in source


def test_legacy_codex_attestation_recovers_harness_from_evidence_source():
    import server

    normalized = server._normalize_turn_model_attestation(
        {
            "status": "verified",
            "requested_model": "codex-gpt-5.6-sol",
            "launch_model": "gpt-5.6-sol",
            "effective_model": "gpt-5.6-sol",
            "model_provider": "openai",
            "source": "codex_app_server_thread_response",
        }
    )

    assert normalized["harness"] == "Codex app-server"
    assert normalized["verification_level"] == "harness"

    source = (Path(__file__).resolve().parent.parent / "static" / "chat.js").read_text(
        encoding="utf-8"
    )
    assert "codex_app_server_thread_response: ['Codex app-server', 'harness']" in source
    assert "sourceDefaults?.[0] || _harnessLabelForModel(model)" in source


def test_gpt6_is_recognized_as_codex_across_backend_and_frontend():
    import server

    assert server.is_codex_model("gpt-6-astra")
    assert not server.is_codex_model("gpt-oss-120b")

    from tests.js_model_registry import evaluate
    assert evaluate("[_isCodexValue('gpt-6-astra'), _isCodexValue('gpt-oss-120b'), _agentKindForModel('gpt-6-astra', {mode:'goose'})]") == [True, False, "codex"]


def test_streaming_draft_and_attestation_survive_live_repaints():
    source = (Path(__file__).resolve().parent.parent / "static" / "chat.js").read_text(
        encoding="utf-8"
    )

    assert "function _ensureLiveStreamDraft(data = {})" in source
    assert "_appendLiveDraftText(data.content)" in source
    assert "_appendLiveDraftThinking(data.content)" in source
    assert "_mergeLiveStreamDraftIntoState();" in source
    assert "streamingMsg.session_continuity" in source
    assert "draft.model_attestation = JSON.stringify(attestation)" in source
    assert "['Session continuity', continuityLabel]" in source
    assert "continuityReason === 'provider_boundary' ? 'harness handoff'" in source
    assert "continuityReason === 'model_boundary' ? 'model handoff'" in source


def test_all_generation_families_persist_model_attestations():
    source = (Path(__file__).resolve().parent.parent / "server.py").read_text(
        encoding="utf-8"
    )

    import inspect
    import server
    # Check each remaining implementation, not a count inflated by retired handlers.
    for name in ("_handle_claude_generation", "_handle_goose_generation",
                 "_handle_ooda_generation", "_handle_weave_generation"):
        assert "_record_turn_model_attestation(" in inspect.getsource(getattr(server, name)), name
