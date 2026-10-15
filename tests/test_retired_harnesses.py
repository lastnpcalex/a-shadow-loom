"""Retiring Hermes removes execution, not historical data or Dream's backend."""
import inspect
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import database as db
import model_registry
import server


@pytest.mark.parametrize("mode", ["hermes", "dream", "prometheus"])
async def test_retired_modes_reject_creation_sends_and_launches(client, monkeypatch, mode):
    response = await client.post("/api/conversations", json={"mode": mode})
    assert response.status_code == 410
    conv = await db.create_conversation("Historical fixture", mode=mode)
    user = await db.add_message(conv["id"], "user", "retained prompt")
    launch = AsyncMock(side_effect=AssertionError("No retired provider may launch"))
    monkeypatch.setattr(server, "_handle_weave_generation", launch)
    monkeypatch.setattr(server, "_handle_claude_generation", launch)
    monkeypatch.setattr(server, "_handle_goose_generation", launch)
    response = await client.post(f"/api/conversations/{conv['id']}/messages", json={
        "content": "must not be inserted", "parent_id": user["id"], "launch_generation": True})
    assert response.status_code == 410
    for entry in (server._launch_generation_task_admitted, server._handle_generation):
        with pytest.raises(HTTPException) as caught:
            await entry(None, conv["id"], {"parent_id": user["id"]})
        assert caught.value.status_code == 410
    assert not await db.get_live_generation_lease(conv["id"])
    launch.assert_not_called()
    assert (await client.get(f"/api/conversations/{conv['id']}")).status_code == 200


@pytest.mark.parametrize("selector", ["hermes:custom:fixture", "Hermes", "PROMETHEUS:fixture"])
async def test_retired_selectors_cannot_be_smuggled_into_supported_routes(client, selector):
    assert model_registry.resolve(selector)["retired"] is True
    response = await client.post("/api/conversations", json={"mode": "claude", "cc_model": selector})
    assert response.status_code == 410
    conv = await db.create_conversation("Fixture", mode="goose")
    response = await client.put(f"/api/conversations/{conv['id']}", json={"cc_model": selector})
    assert response.status_code == 410
    with pytest.raises(ValueError, match="retired"):
        server._resolve_agent_target({"requested_harness": "codex", "requested_model": selector}, conv)


@pytest.mark.parametrize("harness", ["hermes", "dream", "prometheus"])
def test_retired_children_cannot_launch(harness):
    with pytest.raises(ValueError, match="retired"):
        server._resolve_agent_target({"requested_harness": harness}, {"mode": "goose"})


async def test_history_survives_explicit_migration_to_goose(client):
    conv = await db.create_conversation("Historical fixture", mode="hermes")
    user = await db.add_message(conv["id"], "user", "retained prompt")
    attestation = {"source": "hermes_acp_set_model_ack", "status": "verified", "effective_model": "fixture"}
    reply = await db.add_message(conv["id"], "assistant", "retained answer", parent_id=user["id"])
    await db.update_message_content(reply["id"], cc_session_id="historical-native-id", cc_session_mode="hermes",
                                    model_attestation=json.dumps(attestation), content_blocks='[{"type":"tool_use","name":"Read"}]')
    before = await db.get_message(reply["id"])
    response = await client.put(f"/api/conversations/{conv['id']}", json={"cc_model": "goose:openrouter:z-ai/glm-5.3-flash"})
    assert response.status_code == 200
    assert response.json()["mode"] == "goose"
    assert await db.get_message(reply["id"]) == before
    assert server._normalize_turn_model_attestation(attestation)["harness"] == "Hermes ACP"
    from provider_contract import select_resume_session
    decision = select_resume_session(await db.get_branch_to_root(reply["id"]), "goose")
    assert decision.session_id is None  # Never resume a foreign native session.


async def test_old_queue_pauses_without_retry_loop(client, monkeypatch):
    conv = await db.create_conversation("Retired queue fixture", mode="hermes")
    user = await db.add_message(conv["id"], "user", "prompt")
    reply = await db.add_message(conv["id"], "assistant", "completed", parent_id=user["id"])
    queued = await db.add_message(conv["id"], "user", "queued", parent_id=reply["id"])
    await db.enqueue_generation(queued["id"], conv["id"], reply["id"])
    launch = AsyncMock(side_effect=AssertionError("Retired queue must not launch"))
    monkeypatch.setattr(server, "_launch_generation_task", launch)
    monkeypatch.setattr(server, "_ws_send", AsyncMock())
    await server._dispatch_ready_queued_generations(conv["id"])
    await server._dispatch_ready_queued_generations(conv["id"])
    launch.assert_not_called()
    assert (await db.list_queued_generations(conv["id"]))[0]["status"] == "paused"
    response = await client.post(f"/api/conversations/{conv['id']}/generation-queue/{queued['id']}/resume")
    assert response.status_code == 410
    assert (await db.get_message(queued["id"]))["content"] == "queued"


def test_dream_goose_and_weave_survive_retirement():
    import goose_client
    for harness, model in (("claude", "dream:fixture"), ("goose", "goose:dream:fixture"), ("weave", "fixture.gguf")):
        assert model_registry.retirement_reason(harness, model) is None
    assert server._mode_for_cc_model("dream:fixture") == "claude"
    assert "GOOSE_PROVIDER__HOST" in inspect.getsource(goose_client)
    assert not hasattr(server, "hermes_client")
    assert not any("/hermes/" in route.path for route in server.app.routes)


async def test_retired_controls_show_notice_without_migrating():
    playwright = pytest.importorskip("playwright.async_api")
    root = Path(__file__).resolve().parents[1]
    source = (root / "static/app.js").read_text(encoding="utf-8")
    controls = source[source.index("function _retiredHarnessMode("):source.index("// Treat both alias", source.index("function _retiredHarnessMode("))]
    html = (root / "static/index.html").read_text(encoding="utf-8")
    # DOM-only browser fixture: no HTTP server, host lifecycle, or provider.
    import re
    html = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", "", html)
    async with playwright.async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html)
            await page.locator("#input-area").evaluate("el => el.classList.remove('hidden')")
            await page.add_style_tag(content=".hidden { display: none !important; }")
            await page.add_script_tag(content="let _inlineCCSelectionRevision = 0; window.populateCCModelDropdowns = () => { throw Error('Unexpected migration'); };" + controls)
            for mode in ("hermes", "dream", "prometheus"):
                await page.evaluate("mode => updateInlineCCControls({mode, cc_model: 'sonnet'})", mode)
                assert await page.locator("#retired-harness-notice").is_visible()
                assert not await page.locator("#cc-inline-controls").is_visible()
            assert await page.locator('[data-value="hermes"], [data-value="dream"]').count() == 0
            assert await page.locator("#btn-settings").count() == 1
        finally:
            await browser.close()
