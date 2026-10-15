"""Real picker DOM and stale-host compatibility. Disposable server, no providers."""
import asyncio
import json

import pytest
import uvicorn
import database as db

from tests.test_model_registry import canary_registry, CANARY
from tests.test_fullstack_queue_isolation import _eventually, _free_port

playwright = pytest.importorskip("playwright.async_api")


@pytest.mark.parametrize("old_host", [False, True])
async def test_picker_registry_and_preserved_selections(tmp_database, canary_registry, old_host):
    import server
    port = _free_port()
    fixture_server = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off", ws="websockets-sansio"))
    task = asyncio.create_task(fixture_server.serve())
    await _eventually(lambda: fixture_server.started)
    try:
        async with playwright.async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            # Avoid all local sidecar probes; registry UI is the feature under test.
            await page.route("**/api/local/all-models", lambda r: r.fulfill(json={"models": [{"name": "Fixture.gguf", "backend": "llama", "loaded": True}]}))
            await page.route("**/api/health", lambda r: r.fulfill(json={"status": "mock"}))
            if old_host:
                await page.route("**/api/model-registry.js", lambda r: r.fulfill(status=404, body="Not found"))
                legacy = [{"group": g["group"], "models": [{"value": m["value"], "label": m["label"]} for m in g["models"]]} for g in server._visible_cc_models()]
                await page.route("**/api/cc-models", lambda r: r.fulfill(json=legacy))
            await page.goto(f"http://127.0.0.1:{port}", wait_until="domcontentloaded")
            await page.wait_for_function("!document.body.classList.contains('app-booting')")
            selector = "openrouter:" + CANARY["id"]
            await page.wait_for_function("selector => [...document.querySelectorAll('#cc-model option')].some(o => o.value === selector)", arg=selector)
            await page.evaluate("selector => populateCCModelDropdowns(selector)", selector)
            await page.wait_for_function("[...document.querySelector('#cc-model').options].some(o => o.value === 'Fixture.gguf')")
            chosen = await page.evaluate("""() => ['cc-model-inline','cc-model','cfg-cc-model'].map(id => {
                const s = document.getElementById(id);
                return {value:s.value, options:[...s.options].map(o=>o.value)};
            })""")
            for select in chosen:
                assert select["value"] == selector
                assert "goose:openrouter:" + CANARY["id"] in select["options"]
                assert "goose:auto:openrouter:" + CANARY["id"] in select["options"]
                assert "Fixture.gguf" in select["options"]
            assert await page.evaluate("_agentKindForModel('fixture-vendor/registry-canary', {mode:'goose'})") == "openrouter"
            # Discovery failure must never silently select a different model.
            await page.route("**/api/cc-models", lambda r: r.fulfill(status=503, json={"error": "temporary fixture failure"}))
            await page.evaluate("() => populateCCModelDropdowns('openrouter:missing/saved-selection')")
            assert await page.eval_on_selector("#cc-model", "s => s.value") == "openrouter:missing/saved-selection"
            assert await page.eval_on_selector("#cc-model-inline", "s => s.value") == "openrouter:missing/saved-selection"
            assert await page.eval_on_selector("#cc-model", "s => s.selectedOptions[0].textContent") == "openrouter:missing/saved-selection (not in discovered list)"
            assert await page.eval_on_selector("#cc-model", "s => s.selectedOptions[0].parentElement.label") == "Saved selection"
            assert not errors, errors
            await browser.close()
    finally:
        # This is only the random-port fixture object, never either Loom host.
        fixture_server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


async def test_branch_picker_uses_recorded_model_and_keeps_explicit_choice(tmp_database):
    import server

    conv = await db.create_conversation("Model picker branches", mode="claude")
    await db.update_conversation_fields(conv["id"], cc_model="sonnet")
    root = await db.add_message(conv["id"], "user", "Start")
    codex = await db.add_message(conv["id"], "assistant", "Codex reply", parent_id=root["id"])
    codex_followup = await db.add_message(conv["id"], "user", "Continue", parent_id=codex["id"])
    sonnet = await db.add_message(conv["id"], "assistant", "Sonnet reply", parent_id=root["id"])
    legacy_codex = await db.add_message(conv["id"], "assistant", "Older Codex reply", parent_id=root["id"])
    await db.update_message_content(
        codex["id"], cc_model_used="gpt-5.6-sol",
        model_attestation=json.dumps({"requested_model": "codex-gpt-5.6-sol"}),
    )
    await db.update_message_content(sonnet["id"], cc_model_used="sonnet")
    await db.update_message_content(legacy_codex["id"], cc_model_used="gpt-5.6-sol")

    port = _free_port()
    fixture_server = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off", ws="websockets-sansio"))
    task = asyncio.create_task(fixture_server.serve())
    await _eventually(lambda: fixture_server.started)
    try:
        async with playwright.async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.route("**/api/local/all-models", lambda r: r.fulfill(json={"models": []}))
            await page.route("**/api/health", lambda r: r.fulfill(json={"status": "mock"}))
            # Reproduce the refreshed Anthropic order that previously made Opus
            # the browser's incidental first option before conversation loading.
            await page.route("**/api/cc-models", lambda r: r.fulfill(json=[
                {"group": "Anthropic — Auto", "models": [
                    {"value": "opus", "label": "Opus"},
                    {"value": "sonnet", "label": "Sonnet"},
                ]},
                {"group": "ChatGPT Codex", "models": [
                    {"value": "codex-gpt-5.6-sol", "label": "Codex Sol"},
                ]},
            ]))
            await page.goto(
                f"http://127.0.0.1:{port}/?conversation={conv['id']}&branch={codex_followup['id']}&view=chat",
                wait_until="domcontentloaded",
            )
            await page.wait_for_function("document.querySelector('#cc-model-inline')?.value === 'codex-gpt-5.6-sol'")
            assert await page.evaluate("State.currentConv.cc_model") == "sonnet"
            assert await page.evaluate("""() => {
                const payload = {}; _attachCCSettings(payload); return payload.cc_model;
            }""") == "codex-gpt-5.6-sol"
            assert await page.evaluate("""() => {
                const cursor = State.loomCursor;
                State.loomCursor = {...cursor, leaf_message_id: null};
                const payload = {}; _attachCCSettings(payload);
                State.loomCursor = cursor;
                return payload.cc_model;
            }""") == "codex-gpt-5.6-sol"

            # Merely opening and saving other settings must not replace the
            # conversation model with a different branch's suggestion.
            await page.evaluate("""() => {
                window._originalModelTestPut = API.put;
                window._modelTestWrites = [];
                API.put = async (url, payload) => {
                    _modelTestWrites.push({url, payload});
                    if (url === '/api/config') return State.config;
                    if (url.startsWith('/api/conversations/')) return State.currentConv;
                    return {};
                };
            }""")
            await page.click("#btn-settings")
            await page.wait_for_function("document.querySelector('#cfg-cc-model')?.dataset.initialModel === 'codex-gpt-5.6-sol'")
            assert await page.eval_on_selector("#cfg-cc-model", "s => s.value") == "codex-gpt-5.6-sol"
            await page.click("#btn-save-settings")
            saved_models = await page.evaluate("""() => {
                API.put = _originalModelTestPut;
                return _modelTestWrites
                    .filter(w => w.url.startsWith('/api/conversations/'))
                    .map(w => w.payload.cc_model);
            }""")
            assert saved_models == [None]

            # An intentional change stays selected while the tab refreshes its
            # controls, but navigating to another branch restores that branch.
            await page.select_option("#cc-model-inline", "opus")
            await page.wait_for_function("document.querySelector('#cc-model-inline')?.value === 'opus'")
            await page.evaluate("() => updateInlineCCControls(State.currentConv)")
            assert await page.eval_on_selector("#cc-model-inline", "s => s.value") == "opus"
            await page.evaluate("id => switchToBranch(id, id, {exact: true})", sonnet["id"])
            await page.wait_for_function("document.querySelector('#cc-model-inline')?.value === 'sonnet'")
            assert await page.evaluate("""() => {
                const payload = {}; _attachCCSettings(payload); return payload.cc_model;
            }""") == "sonnet"
            await page.evaluate("id => switchToBranch(id, id, {exact: true})", legacy_codex["id"])
            await page.wait_for_function("document.querySelector('#cc-model-inline')?.value === 'codex-gpt-5.6-sol'")
            # A missing Goose selector must never be silently changed to the
            # similarly named OpenRouter route from a different harness.
            unmatched_goose = await page.evaluate("""() => {
                _ccModelsCache = [{group: 'OpenRouter', models: [
                    {value: 'openrouter:fixture/model', label: 'Fixture'}
                ]}];
                EXOModelRegistry.mergeGroups(_ccModelsCache);
                _renderCCModelDropdowns('goose:openrouter:fixture/model', [], null);
                return document.querySelector('#cc-model-inline').value;
            }""")
            assert unmatched_goose == "goose:openrouter:fixture/model"
            # A similar version is not the same model. Keep the exact legacy
            # selector visible instead of silently upgrading it on refresh.
            unmatched_codex = await page.evaluate("""() => {
                _ccModelsCache = [{group: 'ChatGPT Codex', models: [
                    {value: 'codex-gpt-5.6-sol-preview', label: 'Preview'}
                ]}];
                EXOModelRegistry.mergeGroups(_ccModelsCache);
                _renderCCModelDropdowns('gpt-5.6-sol', [], null);
                return document.querySelector('#cc-model-inline').value;
            }""")
            assert unmatched_codex == "gpt-5.6-sol"
            assert not errors, errors
            await browser.close()
    finally:
        fixture_server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
