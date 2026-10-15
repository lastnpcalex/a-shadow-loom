"""Queue status is reconciled in place; no provider or active host involved."""
import asyncio
import json

import pytest
import uvicorn

import database as db
from event_router import SubscriptionRouter
from tests.test_fullstack_queue_isolation import _eventually, _free_port

playwright = pytest.importorskip("playwright.async_api")


@pytest.fixture
async def queue_browser(tmp_database, tmp_path, monkeypatch, isolate_model_discovery):
    import server
    isolate_model_discovery()

    generation_tasks = {}
    monkeypatch.setattr(server, "_active_generations", generation_tasks)
    monkeypatch.setattr(server, "_generation_snapshots", {})
    monkeypatch.setattr(server, "_active_websockets", {})
    monkeypatch.setattr(server, "_event_router", SubscriptionRouter())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    conv = await db.create_conversation("Queue presentation fixture", mode="codex", project_dir=str(workspace))
    user = await db.add_message(conv["id"], "user", "First fixture prompt")
    assistant = await db.add_message(conv["id"], "assistant", "\n\n".join(["Provider partial"] * 60), parent_id=user["id"])
    queued = await db.add_message(conv["id"], "user", "Queued fixture prompt", parent_id=assistant["id"])
    await db.enqueue_generation(queued["id"], conv["id"], assistant["id"])
    port = _free_port()
    fixture_server = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, lifespan="off", log_level="warning", ws="websockets-sansio"))
    task = asyncio.create_task(fixture_server.serve())
    try:
        await _eventually(lambda: fixture_server.started)
        async with playwright.async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                await page.route("**/api/local/all-models", lambda r: r.fulfill(json={"models": []}))
                await page.route("**/api/health", lambda r: r.fulfill(json={"status": "mock"}))
                # Start through the real router and finish initial socket/cursor
                # synchronization before injecting the component's mock stream.
                # Manual loadConversation during init races a second DOM paint.
                await page.goto(f"http://127.0.0.1:{port}/?conversation={conv['id']}&branch={queued['id']}&view=chat", wait_until="domcontentloaded")
                await page.wait_for_function("State.ws?.readyState === WebSocket.OPEN && !State.ws._needsSync && !State._restoringView && !!_queuedGeneration && document.querySelector('#loading-overlay').classList.contains('hidden')")
                await page.locator(".queued-message .queue-edit-btn").wait_for()
                yield page, conv, assistant, queued
                assert not errors, errors
            finally:
                await browser.close()
    finally:
        # A failed assertion must not leave a provider task writing into the
        # next test's SQLite connection. These tasks belong only to this fixture.
        try:
            pending = [t for t in generation_tasks.values() if isinstance(t, asyncio.Task) and not t.done()]
            for generation in pending:
                generation.cancel()
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=10)
        finally:
            fixture_server.should_exit = True
            await asyncio.wait_for(task, timeout=10)


async def pause_queue(page, conv, assistant):
    await db.pause_queued_generations(conv["id"], wait_for_draft_id=assistant["id"])
    await page.evaluate("handleWSMessage({type: 'queue_changed'})")
    await page.wait_for_function("_queuedGeneration?.queue_status === 'paused'")


async def test_send_after_lost_stream_snapshot_keeps_linear_parent(queue_browser, monkeypatch):
    from unittest.mock import AsyncMock
    import context_manager
    page, conv, assistant, old_queue = queue_browser
    monkeypatch.setattr(context_manager, "update_rolling_summary", AsyncMock())
    await db.delete_branch(old_queue["id"])
    # Removing the fixture's old queue advances its cursor revision. Refresh
    # that bookkeeping before simulating loss of the stream snapshot.
    await page.evaluate("async () => _storeLoomCursor(await API.get(`/api/conversations/${State.currentConvId}/cursor/${encodeURIComponent(_loomClientInstanceId)}`))")
    await db.try_acquire_generation_lease(conv["id"], "browser-queue-lease", user_msg_id=assistant["parent_id"])
    await db.bind_generation_lease("browser-queue-lease", draft_msg_id=assistant["id"])
    await page.evaluate("""({promptId}) => {
        // Reconnection can leave the pre-stream user visible while the
        // provider's durable lane is still occupied. Do not trust the DOM
        // to select a topology parent for an automatic queued continuation.
        State.messages = State.messages.filter(m => m.id === promptId);
        // A lost snapshot also lacks the active child in the cached tree.
        // Keeping it there asks the current composer for an explicit branch.
        State.treeData = State.treeData.filter(m => m.id === promptId);
        _queuedGeneration = null;
        State.isStreaming = false;
        streamingDiv?.remove(); streamingDiv = null;
        renderMessages();
    }""", {"promptId": assistant["parent_id"]})
    await page.locator('#user-input').fill('New queued continuation fixture')
    await page.evaluate('sendMessage()')
    queued = await db.list_queued_generations(conv["id"])
    assert len(queued) == 1
    message = await db.get_message(queued[0]["user_msg_id"])
    assert message["parent_id"] == assistant["id"]
    branch = await db.get_branch_to_root(message["id"])
    assert [m["id"] for m in branch] == [assistant["parent_id"], assistant["id"], message["id"]]


@pytest.mark.parametrize("streaming,missing_parent", [(True, False), (False, False), (True, True)])
async def test_queue_status_updates_without_replacing_rows(queue_browser, streaming, missing_parent):
    page, conv, assistant, queued = queue_browser
    await page.evaluate("""({streaming, missingParent, assistantId, queuedId}) => {
        window.queuePaintTrace = [];
        const render = renderMessages;
        window.renderMessages = function(...args) {
            queuePaintTrace.push(new Error('queue repaint').stack);
            return render.apply(this, args);
        };
        const handle = handleWSMessage;
        window.handleWSMessage = function(data) {
            queuePaintTrace.push('event: ' + data.type);
            return handle(data);
        };
        State.isStreaming = streaming;
        const parent = document.querySelector(`.message[data-msg-id="${assistantId}"]`);
        if (missingParent) parent.remove(); // e.g. outside the rendered window
        window.queueRow = document.querySelector(`.message[data-msg-id="${queuedId}"]`);
        window.queueBar = queueRow.querySelector('.queue-actions');
        window.queueContent = queueRow.querySelector('.message-content');
        queueBar.querySelector('.queue-edit-btn').focus();
        window.queueFocus = document.activeElement;
        window.queueCursor = JSON.stringify(State.loomCursor);
        const scroller = document.getElementById('messages-container');
        // Measure a settled viewport, not the start of CSS smooth scrolling.
        scroller.style.scrollBehavior = 'auto';
        scroller.scrollTop = 100;
        window.queueScroll = scroller.scrollTop;
    }""", {"streaming": streaming, "missingParent": missing_parent, "assistantId": assistant["id"], "queuedId": queued["id"]})
    # Force the formerly timing-dependent background reload. Streaming must
    # not make an unchanged chain replace its focused queue controls.
    await page.evaluate("loadMessages(State.currentConvId)")
    await pause_queue(page, conv, assistant)
    await playwright.expect(page.locator(".queued-message .queue-status-label")).to_have_text("paused", timeout=2500)
    await playwright.expect(page.locator(".queue-resume-btn")).to_be_visible()
    # Identical updates must not replace focused buttons or duplicate Resume.
    await page.evaluate("_refreshQueuedGeneration()")
    preserved = await page.evaluate("""() => ({
        row: queueRow.isConnected, bar: queueBar.isConnected, content: queueContent.isConnected,
        focus: document.activeElement === queueFocus,
        cursor: JSON.stringify(State.loomCursor) === queueCursor,
        scroll: document.getElementById('messages-container').scrollTop,
        previousScroll: queueScroll,
        paused: queueRow.classList.contains('paused-queued-message'),
        resumeCount: queueRow.querySelectorAll('.queue-resume-btn').length,
        repaintTrace: queuePaintTrace,
    })""")
    assert all(preserved[key] for key in ("row", "bar", "content", "focus", "cursor", "paused")), json.dumps(preserved)
    assert preserved["scroll"] == preserved["previousScroll"], preserved
    assert preserved["resumeCount"] == 1, preserved
    await db.resume_queued_generation(queued["id"])
    await page.evaluate("_refreshQueuedGeneration()")
    assert await page.locator(".queue-resume-btn").count() == 0
    assert await page.locator(".queue-status-label").inner_text() == "queued"
    assert not await page.evaluate("queueRow.classList.contains('paused-queued-message')")
    await db.delete_queued_generation(queued["id"])
    await page.evaluate("_refreshQueuedGeneration()")
    assert await page.evaluate("queueRow.isConnected && queueContent.isConnected")
    assert await page.locator(".queue-actions").count() == 0
    assert not await page.evaluate("queueRow.classList.contains('queued-message')")


@pytest.mark.parametrize("finish", ["save", "cancel"])
async def test_queue_status_preserves_editor_and_restores_current_actions(queue_browser, finish):
    page, conv, assistant, queued = queue_browser
    await page.evaluate("State.isStreaming = true")
    await page.locator(".queue-edit-btn").click()
    await page.locator(".edit-message-input").fill("Unsaved queue revision")
    await page.evaluate("""() => {
        window.queueEditor = document.querySelector('.edit-message-input');
        queueEditor.setSelectionRange(2, 8);
        window.queueCursor = JSON.stringify(State.loomCursor);
    }""")
    await pause_queue(page, conv, assistant)
    assert await page.evaluate("""() => (
        queueEditor.isConnected && document.activeElement === queueEditor
        && queueEditor.value === 'Unsaved queue revision'
        && queueEditor.selectionStart === 2 && queueEditor.selectionEnd === 8
        && JSON.stringify(State.loomCursor) === queueCursor
        && !document.querySelector('.queued-message .queue-actions')
    )""")
    await page.locator(f".edit-{finish}").click()
    await playwright.expect(page.locator(".queue-status-label")).to_have_text("paused", timeout=2500)
    await playwright.expect(page.locator(".queue-resume-btn")).to_be_visible()
    saved = await db.get_message(queued["id"])
    assert saved["content"] == ("Unsaved queue revision" if finish == "save" else "Queued fixture prompt")
    assert (await db.list_queued_generations(conv["id"]))[0]["status"] == "paused"
    assert await page.evaluate("JSON.stringify(State.loomCursor) === queueCursor")
