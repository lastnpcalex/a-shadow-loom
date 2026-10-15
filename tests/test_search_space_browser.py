"""Real Search Space DOM against an isolated Loom API; no model/network calls."""

import asyncio
import json

import pytest
import uvicorn
from playwright.async_api import expect

import database as db
from search_space import SearchIndex, SearchOperator
from tests.test_fullstack_queue_isolation import _eventually, _free_port
from tests.test_search_history import FixtureEncoder


playwright = pytest.importorskip("playwright.async_api")


async def test_search_space_classification_route_and_cited_result(tmp_database, tmp_path, monkeypatch):
    import server

    workspace = tmp_path / "cloud-workspace"
    workspace.mkdir()
    conversation = await db.create_conversation("Phoenix project", mode="codex", project_dir=str(workspace))
    message = await db.add_message(conversation["id"], "assistant", "The Phoenix migration landed and validation passed.")
    monkeypatch.setattr(server, "_search_index", SearchIndex(tmp_path / "machine-state" / "search.sqlite3", embeddings=FixtureEncoder()))
    response_ready = asyncio.Event()

    async def fake_complete(self, messages, route):
        assert route == "cloud"
        await response_ready.wait()
        if "Initial search results" in messages[-1]["content"]:
            return json.dumps({"tool":"read_conversation_context", "arguments":{"message_id":message["id"]}})
        return json.dumps({"answer":"Phoenix was completed [S1]."})

    monkeypatch.setattr(SearchOperator, "_complete", fake_complete)
    port = _free_port()
    fixture = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, lifespan="off", log_level="warning", ws="websockets-sansio"))
    task = asyncio.create_task(fixture.serve())
    await _eventually(lambda: fixture.started)
    try:
        async with playwright.async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.route("**/api/local/all-models", lambda route: route.fulfill(json={"models": []}))
            await page.route("**/api/health", lambda route: route.fulfill(json={"status": "mock"}))
            await page.goto(f"http://127.0.0.1:{port}/search", wait_until="domcontentloaded")
            await expect(page.locator('#view-search')).to_be_visible()
            row = page.locator(".search-space-workspace")
            await expect(row).to_have_count(1)
            await row.locator("select").select_option("cloud")
            await expect(row.locator('input[type="checkbox"]')).to_be_enabled()
            await row.locator('input[type="checkbox"]').check()
            await expect(page.locator("#search-space-route")).to_contain_text("GLM 5.3 Flash")
            await page.locator("#search-space-question").fill("Where did Phoenix land?")
            await page.locator("#btn-search-space-run").click()
            await expect(page.locator('.search-space-pending .search-space-user')).to_have_text('Where did Phoenix land?')
            await expect(page.locator('#search-space-question')).to_have_value('')
            await expect(page.locator('.search-space-pending .clotho-spinner')).to_be_visible()
            first_frame = await page.locator('.search-space-pending .clotho-spinner').text_content()
            await page.wait_for_function("first => document.querySelector('.search-space-pending .clotho-spinner').textContent !== first", arg=first_frame)
            await page.clock.install()
            await page.clock.fast_forward(16000)
            await expect(page.locator('.search-space-waiting-text')).to_contain_text('Still looming')
            await page.clock.fast_forward(30000)
            await expect(page.locator('.search-space-waiting-text')).to_contain_text('taking longer')
            await expect(page.locator('#btn-search-space-run')).to_be_disabled()
            response_ready.set()
            await expect(page.locator(".search-space-reply")).to_contain_text("completed [S1]")
            await expect(page.locator('.search-space-pending')).to_have_count(0)
            await expect(page.locator('#search-space-question')).to_be_enabled()
            await expect(page.locator(".search-space-source")).to_have_count(1)
            await expect(page.locator(".search-space-source")).to_contain_text("Phoenix migration landed")
            href = await page.locator(".search-space-source").get_attribute("href")
            assert href == f"http://127.0.0.1:{port}/?conversation={conversation['id']}&branch={message['id']}&view=chat"
            await page.locator("#search-space-question").fill("The validation one")
            await page.locator("#search-space-question").press("Enter")
            await expect(page.locator('#search-space-question')).to_have_value('The validation one\n')
            await expect(page.locator('.search-space-pending')).to_have_count(0)
            await expect(page.locator('.search-space-reply')).to_have_count(1)
            await page.locator("#search-space-question").press("Shift+Enter")
            await expect(page.locator(".search-space-reply")).to_have_count(2)
            await page.get_by_role("button", name="New search").click()
            await expect(page.locator(".search-space-turn")).to_have_count(0)
            await page.locator("#search-space-question").fill("Where did Phoenix land?")
            await page.locator("#btn-search-space-run").click()
            await expect(page.locator(".search-space-reply")).to_have_count(1)
            await page.reload(wait_until="domcontentloaded")
            await expect(page.locator('#view-search')).to_be_visible()
            await expect(page.locator(".search-space-turn")).to_have_count(0)
            await expect(page.locator('#search-space-workspaces input[type="checkbox"]')).to_be_checked()
            await page.route('**/api/search-space/sessions/*/messages', lambda route: route.fulfill(status=503, json={'detail': 'Search unavailable'}))
            await page.locator('#search-space-question').fill('Retry this question\nwith detail')
            await page.locator('#search-space-question').press('Shift+Enter')
            await expect(page.locator('.search-space-waiting')).to_contain_text('Search failed')
            await expect(page.locator('#search-space-question')).to_have_value('Retry this question\nwith detail')
            await expect(page.locator('#search-space-question')).to_be_enabled()
            await expect(page.locator('.clotho-spinner')).to_have_count(0)
            assert not errors, errors
            await browser.close()
    finally:
        fixture.should_exit = True
        await asyncio.wait_for(task, timeout=10)
