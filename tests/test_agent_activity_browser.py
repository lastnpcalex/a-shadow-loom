"""Real Chromium child-panel regression; disposable DB, random port, no provider."""
import asyncio
import contextlib
import json
import time

import pytest
import uvicorn

import database as db
from agent_events import ChildEventWriter
from event_router import SubscriptionRouter
from tests.test_fullstack_queue_isolation import _eventually, _free_port
from tests.lifecycle_metrics import browser_delta, browser_sample, observe

playwright = pytest.importorskip("playwright.async_api")


@pytest.fixture
async def activity_browser(tmp_database, tmp_path, monkeypatch, isolate_model_discovery):
    import server
    isolate_model_discovery()
    monkeypatch.setattr(server, '_CASUAL_CHAT_ROOT', tmp_path / 'ExoHome')
    monkeypatch.setattr(server, "_active_generations", {})
    monkeypatch.setattr(server, "_generation_snapshots", {})
    monkeypatch.setattr(server, "_active_websockets", {})
    monkeypatch.setattr(server, "_event_router", SubscriptionRouter())
    conv = await db.create_conversation("Activity fixture", mode="codex", project_dir=str(tmp_path))
    user = await db.add_message(conv["id"], "user", "Keep my branch and edit")
    assistant = await db.add_message(conv["id"], "assistant", "Saved answer", parent_id=user["id"], content_blocks=json.dumps([
        {"type": "tool_use", "name": "Read", "input": "fixture.txt", "result": "saved tool context"},
        {"type": "text", "text": "\n\n".join(f"Saved paragraph {n}" for n in range(60))},
    ]))
    runs = []
    for i in range(4):
        run = await db.create_agent_run(f"activity-{i}", conv["id"], f"Independent task {i}",
                                        requested_harness="codex", parent_message_id=user["id"])
        await db.update_agent_run(run["run_id"], status="running", started_at=time.time())
        runs.append(run)
    port = _free_port()
    uv_server = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port,
                                             ws="websockets-sansio",
                                             log_level="warning", lifespan="off"))
    task = asyncio.create_task(uv_server.serve())
    await _eventually(lambda: uv_server.started)
    try:
        async with playwright.async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            await page.goto(f"http://127.0.0.1:{port}", wait_until="domcontentloaded")
            await page.wait_for_function("typeof loadConversation === 'function' && typeof State !== 'undefined'")
            await page.wait_for_function("!document.body.classList.contains('app-booting')")
            yield page, conv, user, assistant, runs
            await browser.close()
            assert not errors, errors
    finally:
        # Only this fixture's random-port server; never the active Loom host.
        uv_server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


async def open_conversation(page, conv, assistant):
    await page.evaluate("id => loadConversation(id)", conv["id"])
    await page.wait_for_function("State.ws?.readyState === WebSocket.OPEN && !_agentRefreshJob")


async def test_failed_child_resume_updates_without_refresh_or_cursor_move(activity_browser, monkeypatch):
    import server
    from agent_supervisor import AgentSupervisor
    page, conv, user, assistant, runs = activity_browser
    run = runs[0]
    await db.update_agent_run(run["run_id"], pid=123456789)
    monkeypatch.setattr(server, "_agent_supervisor", AgentSupervisor())  # no dispatcher/provider
    await open_conversation(page, conv, assistant)
    await page.locator("#btn-agent-manager").click()
    card = page.locator('.agent-run-card').filter(has_text='Independent task 0')
    await card.locator(':scope > summary').click()
    before = await page.evaluate("JSON.stringify({branch:State.activeBranchId,conv:State.currentConvId,cursor:State.loomCursor})")
    snapshot_ready, snapshot_release = asyncio.Event(), asyncio.Event()
    async def stale_snapshot(route):
        response = await route.fetch()
        snapshot_ready.set()
        await snapshot_release.wait()
        await route.fulfill(response=response)
    await page.route("**/api/conversations/*/agent-runs", stale_snapshot)
    await page.evaluate("() => { void refreshAgentRuns(); }")
    await asyncio.wait_for(snapshot_ready.wait(), 10)
    await db.update_agent_run(run["run_id"], pid=None)  # provider teardown precedes terminal emission
    terminal = await db.append_agent_event(run["run_id"], "agent_failed", {"status":"failed", "error":"lost tool reply", "pid":None},
                                           run_state={"status":"failed", "error":"lost tool reply"})
    await server._publish_agent_event(run, terminal)
    resume = card.locator('[data-agent-resume]')
    await resume.wait_for(state='visible')
    snapshot_release.set()
    await page.wait_for_function("!_agentRefreshJob")
    await page.unroute("**/api/conversations/*/agent-runs", stale_snapshot)
    assert await resume.is_visible()  # stale running PID must not hide recovery again
    await resume.click()
    await page.wait_for_function("State.agentRuns.find(r => r.run_id === 'activity-0').status === 'queued'")
    assert not await resume.is_visible()
    assert (await db.get_agent_run(run["run_id"]))["status"] == "queued"
    assert before == await page.evaluate("JSON.stringify({branch:State.activeBranchId,conv:State.currentConvId,cursor:State.loomCursor})")


async def test_child_bursts_preserve_dom_editor_scroll_and_hidden_panel(activity_browser, record_property):
    import server
    page, conv, user, assistant, runs = activity_browser
    frames = []
    page.on("websocket", lambda ws: ws.on("framereceived", lambda payload: frames.append(payload)))
    load_started = time.perf_counter()
    await open_conversation(page, conv, assistant)
    screen_load_ms = (time.perf_counter() - load_started) * 1000
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Performance.enable")
    await page.locator("#btn-agent-manager").click()
    await page.wait_for_selector(".agent-run-card")
    writers = [ChildEventWriter(run, server._publish_agent_event, server._publish_agent_events) for run in runs]
    try:
        for _ in range(80):
            await writers[0].emit("text_delta", {"text": "initial line\n"})
        await writers[0].flush()
        await page.wait_for_function("State.agentRuns.find(r => r.run_id === 'activity-0')._lastSequence === 80 && _agentRenderTimer == null")
        await page.evaluate("id => editMessage(id)", user["id"])
        # Finish the existing editor's deferred auto-size before measuring;
        # scroll instantly so CSS smooth-scrolling isn't the change under test.
        await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        await page.evaluate("""() => {
            const card = document.querySelector('[data-run-id="activity-0"]');
            card.open = true;
            const tool = document.querySelector('.tool-block'); tool.classList.add('expanded');
            const editor = document.querySelector('.edit-message-input');
            editor.value = 'unsaved user edit'; editor.focus(); editor.setSelectionRange(3, 8);
            const output = card.querySelector('.agent-output'); output.scrollTop = 20;
            const list = document.getElementById('agent-activity-list'); list.scrollTop = 15;
            const messages = document.getElementById('messages-container'); messages.scrollTo({top:80, behavior:'instant'});
            window.activityBefore = {card, tool, editor, output, list, messages,
                cursor: JSON.stringify(State.loomCursor), branch: State.activeBranchId,
                msgScroll: messages.scrollTop, listScroll: list.scrollTop, outputScroll: output.scrollTop};
            window.childPaints = 0; const render = renderAgentActivity;
            renderAgentActivity = () => { window.childPaints++; return render(); };
        }""")
        async def burst(writer):
            for _ in range(200):
                await writer.emit("text_delta", {"text": "next\n"})
            await writer.flush()
        before = await browser_sample(cdp)
        burst_started = time.perf_counter()
        await asyncio.gather(*(burst(writer) for writer in writers))
        await page.wait_for_function("State.agentRuns.every(r => r._lastSequence >= 200) && _agentRenderTimer == null")
        burst_ms = (time.perf_counter() - burst_started) * 1000
        resource_metrics = browser_delta(before, await browser_sample(cdp))
        preserved = await page.evaluate("""() => {
            const b = activityBefore;
            return {card: b.card === document.querySelector('[data-run-id="activity-0"]'),
                open: b.card.open, tool: b.tool.isConnected && b.tool.classList.contains('expanded'),
                editor: b.editor.isConnected && b.editor.value === 'unsaved user edit',
                focus: document.activeElement === b.editor, caret: [b.editor.selectionStart, b.editor.selectionEnd],
                cursor: JSON.stringify(State.loomCursor) === b.cursor, branch: State.activeBranchId === b.branch,
                msgScroll: b.messages.scrollTop === b.msgScroll, listScroll: Math.abs(b.list.scrollTop - b.listScroll) < 2,
                outputScroll: b.output.scrollTop === b.outputScroll, paints: childPaints};
        }""")
        assert all(value is True for key, value in preserved.items() if key not in {"caret", "paints"}), json.dumps(preserved)
        assert preserved["caret"] == [3, 8]
        assert preserved["paints"] < 30, preserved
        batches = [json.loads(frame) for frame in frames if json.loads(frame).get("type") == "agent_event_batch"]
        assert len(batches) < 30
        assert sum(len(frame["events"]) for frame in batches) >= 800

        # Closed and background panels must not repaint, but state still lands.
        await page.evaluate("""() => {
            _closeAgentManager(); window.panelMutations = 0;
            window.panelObserver = new MutationObserver(rows => panelMutations += rows.length);
            panelObserver.observe(document.getElementById('agent-activity-list'), {childList:true, subtree:true, characterData:true});
        }""")
        await burst(writers[0])
        await page.wait_for_function("State.agentRuns.find(r => r.run_id === 'activity-0')._lastSequence === 480 && _agentRenderTimer == null")
        assert await page.evaluate("panelMutations") == 0
        # Emulate the visibility signal deterministically in headless Chromium.
        await page.evaluate("""() => {
            window.fixtureHidden = true;
            Object.defineProperty(document, 'hidden', {configurable:true, get:() => fixtureHidden});
            window.hiddenPaints = childPaints;
        }""")
        hidden_before = await browser_sample(cdp)
        await burst(writers[0])
        await page.wait_for_function("State.agentRuns.find(r => r.run_id === 'activity-0')._lastSequence === 680")
        # Include a bounded idle interval so timer cost isn't hidden by a short burst.
        await page.wait_for_timeout(1000)
        hidden_metrics = browser_delta(hidden_before, await browser_sample(cdp))
        # Calling the renderer's visibility guard is not a paint. Observe the
        # panel itself so an early return cannot count as hidden DOM work.
        assert await page.evaluate("panelMutations") == 0
        await page.evaluate("""() => { fixtureHidden = false; document.dispatchEvent(new Event('visibilitychange')); }""")
        await page.locator("#btn-agent-manager").click()
        assert await page.locator('[data-run-id="activity-0"] .agent-output').text_content() == await page.evaluate("State.agentRuns.find(r => r.run_id === 'activity-0').last_output")
        assert await page.evaluate("JSON.stringify(State.loomCursor) === activityBefore.cursor")
        # A real socket reconnect is independent of the visibility simulation:
        # preserve the active editor and expanded chat tool, not just our panel.
        reconnect_started = time.perf_counter()
        await page.evaluate("() => { window.previousSocket = State.ws; State.ws.close(); }")
        await page.wait_for_function("State.ws !== previousSocket && State.ws?.readyState === WebSocket.OPEN && !State.ws._needsSync && !_agentRefreshJob")
        assert await page.evaluate("activityBefore.editor.isConnected && activityBefore.editor.value === 'unsaved user edit'")
        assert await page.evaluate("activityBefore.tool.isConnected && activityBefore.tool.classList.contains('expanded')")
        assert await page.evaluate("activityBefore.card === document.querySelector('[data-run-id=\"activity-0\"]') && activityBefore.card.open")
        assert await page.evaluate("JSON.stringify(State.loomCursor) === activityBefore.cursor")
        observe(record_property, "child_burst", **resource_metrics,
                burst_events=800, burst_batches=len(batches), burst_paints=preserved["paints"], burst_ms=burst_ms,
                hidden_task_ms=hidden_metrics["task_ms"], hidden_wall_ms=hidden_metrics["wall_ms"],
                reconnect_ms=(time.perf_counter() - reconnect_started) * 1000, screen_load_ms=screen_load_ms,
                raytracer_canvas=await page.locator('#blackhole-container canvas').count())
    finally:
        for writer in writers:
            await writer.close()


async def test_paginated_replay_does_not_lose_live_tail_or_resurrect_completed_run(activity_browser):
    import server
    page, conv, user, assistant, runs = activity_browser
    run = runs[0]
    await db.append_agent_events(run["run_id"], [
        {"event_kind": "agent_started", "payload": {"task": run["task"]}},
        *[{"event_kind": "text_delta", "payload": {"text": f"{i},"}} for i in range(1200)],
    ])
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []
    async def delay_first_page(route):
        requests.append(route.request.url)
        if "activity-0/events?after_sequence=0&" in route.request.url and not entered.is_set():
            entered.set()
            await release.wait()
        await route.continue_()
    await page.route("**/api/agent-runs/*/events?*", delay_first_page)
    await page.evaluate("id => loadConversation(id)", conv["id"])
    await asyncio.wait_for(entered.wait(), timeout=10)
    await page.wait_for_function("_subscribedRunIds.has('agent:activity-0')")
    terminal = await db.append_agent_event(run["run_id"], "agent_completed", {"status":"completed", "result":{"text":"complete"}},
                                           run_state={"status":"completed", "result":{"text":"complete"}})
    await server._publish_agent_event(run, terminal)
    await page.wait_for_function("State.agentRuns.find(r => r.run_id === 'activity-0').status === 'completed'")
    release.set()
    await page.wait_for_function("State.agentRuns.find(r => r.run_id === 'activity-0')._lastSequence === 1202 && !_agentRefreshJob")
    result = await page.evaluate("""() => { const r = State.agentRuns.find(r => r.run_id === 'activity-0');
        return {status:r.status, text:r.last_output, pending:r._pendingEvents?.size || 0}; }""")
    assert result == {"status":"completed", "text":"".join(f"{i}," for i in range(1200))[-1600:], "pending":0}
    assert len(requests) == 3, requests
    assert "after_sequence=500" in requests[1] and "after_sequence=1000" in requests[2]
    await page.evaluate("() => Promise.all([refreshAgentRuns(), refreshAgentRuns()])")
    assert len(requests) == 3  # No fetch from zero, no duplicate replay.
    await server._publish_agent_event(run, terminal)
    assert await page.evaluate("State.agentRuns.find(r => r.run_id === 'activity-0').status") == "completed"

    # An HTTP list captured before completion must not overwrite the newer
    # terminal state delivered on the socket while that response was delayed.
    snapshot_ready, snapshot_release = asyncio.Event(), asyncio.Event()
    async def stale_snapshot(route):
        response = await route.fetch()
        snapshot_ready.set()
        await snapshot_release.wait()
        await route.fulfill(response=response)
    await page.route("**/api/conversations/*/agent-runs", stale_snapshot)
    await page.evaluate("() => { void refreshAgentRuns(); }")
    await asyncio.wait_for(snapshot_ready.wait(), timeout=10)
    other = runs[1]
    terminal = await db.append_agent_event(other["run_id"], "agent_completed", {"status":"completed"},
                                           run_state={"status":"completed"})
    await server._publish_agent_event(other, terminal)
    await page.wait_for_function("State.agentRuns.find(r => r.run_id === 'activity-1').status === 'completed'")
    snapshot_release.set()
    await page.wait_for_function("!_agentRefreshJob")
    assert await page.evaluate("State.agentRuns.find(r => r.run_id === 'activity-1').status") == "completed"

    # A delayed terminal from a previous attempt cannot dismiss the permission
    # of a newer attempt that reused the durable child ID.
    started = await db.append_agent_event(other["run_id"], "agent_started", {}, run_state={"status":"running"})
    await server._publish_agent_event(other, started)
    await page.wait_for_function("State.agentRuns.find(r => r.run_id === 'activity-1').status === 'running'")
    await page.evaluate("convId => handleWSMessage({scope:'run', run_id:'agent:activity-1', conv_id:convId, type:'permission_request', request_id:'fixture-new-attempt', tool_name:'FixtureReview'})", conv["id"])
    await server._publish_agent_event(other, terminal)
    await page.evaluate("() => new Promise(resolve => setTimeout(resolve, 100))")
    assert await page.evaluate("State.agentRuns.find(r => r.run_id === 'activity-1')._permissionPending")


@pytest.mark.parametrize('size', [(1280, 720), (390, 844)])
async def test_dock_approval_preserves_large_inputs_note_and_cursor(activity_browser, monkeypatch, tmp_path, size):
    import server
    page, conv, user, assistant, runs = activity_browser
    await page.set_viewport_size({'width': size[0], 'height': size[1]})
    await db.update_conversation_fields(conv["id"], permission_profile="strict")
    monkeypatch.setattr(server, "_pending_hook_permissions", {})
    await open_conversation(page, conv, assistant)
    before = await page.evaluate("JSON.stringify({cursor:State.loomCursor, branch:State.activeBranchId})")
    request = asyncio.create_task(server.handle_cc_permission({
        "loom_conv_id": conv["id"], "agent_run_id": runs[0]["run_id"],
        "tool_name": "SensitiveRead", "tool_input": {"file_path": "fixture.txt", "detail": "large input " * 3000},
    }))
    try:
        await page.locator('#agent-dock-attention').wait_for(state='visible')
        assert not await page.locator('#agent-manager-panel').is_visible()
        await page.locator('#agent-dock-attention').click()
        bounds = await page.locator('#agent-manager-panel').bounding_box()
        assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= size[0] + 1
        card = page.locator('[data-run-id="activity-0"]')
        await card.locator('.agent-request-arguments > summary').click()
        assert len(await card.locator('.agent-request-arguments pre').text_content()) > 30000
        await card.locator('.permission-feedback-input').fill('Use a local fixture')
        await page.evaluate('renderAgentActivity()')
        assert await card.locator('.permission-feedback-input').input_value() == 'Use a local fixture'
        await page.screenshot(path=str(tmp_path / 'agent-dock.png'))
        await card.locator('[data-action="deny"]').click()
        result = await asyncio.wait_for(request, 5)
        assert result['allow'] is False and result['feedback'] == 'Use a local fixture'
        await page.locator('#agent-dock-attention').wait_for(state='hidden')
        assert before == await page.evaluate("JSON.stringify({cursor:State.loomCursor, branch:State.activeBranchId})")
        assert not await page.locator('#notif-dropdown').is_visible()
    finally:
        if not request.done(): request.cancel()
        await asyncio.gather(request, return_exceptions=True)
