"""Submitted turns survive navigation on a disposable server, without providers."""
import asyncio
import json

import pytest

import database as db
import server
from tests.test_fullstack_queue_isolation import _ControlledProvider
from tests.test_lifecycle_handoff_browser import lifecycle_browser, playwright


@pytest.mark.parametrize("destination", ["new_thread", "sibling", "tree"])
async def test_late_send_ack_keeps_the_navigated_view(lifecycle_browser, monkeypatch, destination):
    page, conv, client, workspace = lifecycle_browser
    controller = _ControlledProvider(teardown_delay=0)
    monkeypatch.setattr(server, "_handle_generation", controller.handle)
    if destination == "sibling":
        root = await db.add_message(conv["id"], "user", "Original root")
        leaf = await db.add_message(conv["id"], "assistant", "Original leaf", parent_id=root["id"])
        sibling = await db.add_message(conv["id"], "assistant", "Sibling leaf", parent_id=root["id"])
        await page.evaluate("id => switchToBranch(id, id, {exact: true})", leaf["id"])
    ready, release = asyncio.Event(), asyncio.Event()

    async def hold_ack(route):
        response = await route.fetch()
        ready.set()
        await release.wait()
        await route.fulfill(response=response)

    await page.route(f"**/api/conversations/{conv['id']}/messages", hold_ack)
    await page.locator("#user-input").fill("Submitted before navigation")
    await page.evaluate("window.sendTask = sendMessage(); void 0")
    try:
        await asyncio.wait_for(ready.wait(), 10)
        await controller.wait_for_runs(1)
        await page.wait_for_function("id => State._followingDraftMsgId === id", arg=controller.runs[0].draft_id)
        await page.evaluate("async () => { if (State._cursorSyncPromise) await State._cursorSyncPromise; }")
        if destination == "new_thread":
            await page.evaluate("id => loadConversation(id, {newThread: true})", conv["id"])
        elif destination == "sibling":
            await page.evaluate("id => switchToBranch(id, id, {exact: true})", sibling["id"])
        else:
            await page.evaluate("switchView('tree')")
        await page.locator("#user-input").fill("Draft on destination")
        before = await page.evaluate("JSON.stringify({cursor: State.loomCursor, ids: State.messages.map(m => m.id), view: State.currentView})")
    finally:
        release.set()
    await page.evaluate("sendTask")
    assert await page.evaluate("JSON.stringify({cursor: State.loomCursor, ids: State.messages.map(m => m.id), view: State.currentView})") == before
    assert await page.locator("#user-input").input_value() == "Draft on destination"
    if destination != "tree":
        assert not await page.locator("#messages .streaming").count()
    run = controller.runs[0]
    assert (await db.get_message(run.parent_id))["content"] == "Submitted before navigation"
    controller.runs[0].release.set()
    await asyncio.wait_for(run.exited.wait(), 10)
    # This is the same content-free completion notice emitted by real adapters.
    await server._ws_broadcast_all({"type": "branch_landed", "conv_id": conv["id"], "message_id": run.draft_id})
    await page.wait_for_function("id => _notifications.some(n => n.id === id && n.branchActivity)", arg=run.draft_id)
    assert await page.evaluate("State.loomCursor.thread_id") == json.loads(before)["cursor"]["thread_id"]


async def test_navigation_during_slash_lookup_keeps_the_submitted_payload(lifecycle_browser, monkeypatch):
    page, conv, client, workspace = lifecycle_browser
    controller = _ControlledProvider(teardown_delay=0)
    monkeypatch.setattr(server, "_handle_generation", controller.handle)
    await page.evaluate("""() => {
        _loadSkills = () => new Promise(resolve => window.releaseSkills = resolve);
        _translateSlashCommand = () => ({prompt: 'Captured slash prompt'});
        State.pendingReply = {label: 'Original quote', text: 'Quoted before send'};
        State.pendingImages = [{path: 'original.txt', is_image: false}];
        document.getElementById('describe-context').value = 'Original description';
    }""")
    before = await page.evaluate("({cursor: State.loomCursor, model: document.getElementById('cc-model-inline').value})")
    captured = []

    async def capture(route):
        captured.append(route.request.post_data_json)
        await route.fulfill(status=503, json={"detail": "isolated payload capture"})

    await page.route("**/api/conversations/*/messages", capture)
    await page.locator("#user-input").fill("/fixture")
    await page.evaluate("window.sendTask = sendMessage(); void 0")
    await page.wait_for_function("typeof releaseSkills === 'function'")
    other = await db.create_conversation("Destination", mode="codex", project_dir=str(workspace))
    await page.evaluate("id => loadConversation(id, {newThread: true})", other["id"])
    await page.evaluate("""() => {
        State.pendingImages = [{path: 'destination.txt', is_image: false}];
        State.pendingReply = {label: 'Destination', text: 'Do not send this'};
        document.getElementById('describe-context').value = 'Destination description';
        document.getElementById('cc-model-inline').value = 'gpt-6.1-sol';
        releaseSkills([]);
    }""")
    await page.evaluate("sendTask")
    assert len(captured) == 1
    payload = captured[0]
    assert payload["loom_thread_id"] == before["cursor"]["thread_id"]
    assert payload["image_path"] == ["original.txt"]
    assert payload["describe_context"] == "Original description"
    assert payload["cc_model"] == before["model"]
    assert "Original quote" in payload["content"] and "Do not send this" not in payload["content"]
    assert await page.evaluate("State.currentConvId") == other["id"]


async def test_legacy_send_ack_launches_on_original_conversation_socket(lifecycle_browser, monkeypatch):
    page, conv, client, workspace = lifecycle_browser
    controller = _ControlledProvider(teardown_delay=0)
    launched = []

    async def controlled(websocket, conv_id, data):
        launched.append((conv_id, dict(data)))
        await controller.handle(websocket, conv_id, data)

    monkeypatch.setattr(server, "_handle_generation", controlled)
    ready, release = asyncio.Event(), asyncio.Event()

    async def legacy_ack(route):
        payload = route.request.post_data_json
        payload["launch_generation"] = False
        response = await route.fetch(post_data=json.dumps(payload))
        ready.set()
        await release.wait()
        data = await response.json()
        data.pop("generation_ids", None)
        await route.fulfill(response=response, json=data)

    await page.route(f"**/api/conversations/{conv['id']}/messages", legacy_ack)
    original_cursor = await page.evaluate("State.loomCursor")
    original_model = await page.locator("#cc-model-inline").input_value()
    await page.locator("#user-input").fill("Legacy persisted send")
    await page.evaluate("window.sendTask = sendMessage(); void 0")
    try:
        await asyncio.wait_for(ready.wait(), 10)
        other = await db.create_conversation("Destination", mode="codex", project_dir=str(workspace))
        await page.evaluate("id => loadConversation(id, {newThread: true})", other["id"])
        await page.locator("#user-input").fill("Destination draft")
        before = await page.evaluate("JSON.stringify({conv: State.currentConvId, cursor: State.loomCursor, ids: State.messages.map(m => m.id)})")
    finally:
        release.set()
    await page.evaluate("sendTask")
    await controller.wait_for_runs(1)
    assert len(launched) == 1 and launched[0][0] == conv["id"]
    packet = launched[0][1]
    assert packet["loom_thread_id"] == original_cursor["thread_id"]
    assert packet["cc_model"] == original_model
    assert (await db.get_message(controller.runs[0].parent_id))["content"] == "Legacy persisted send"
    assert await page.evaluate("JSON.stringify({conv: State.currentConvId, cursor: State.loomCursor, ids: State.messages.map(m => m.id)})") == before
    assert await page.locator("#user-input").input_value() == "Destination draft"
    controller.runs[0].release.set()
    await asyncio.wait_for(controller.runs[0].exited.wait(), 10)
    await page.wait_for_function("id => _notifications.some(n => n.id === id && n.branchActivity)", arg=controller.runs[0].draft_id)
    assert await page.evaluate("State.currentConvId") == other["id"]


async def test_stale_manual_launch_keeps_packet_when_view_socket_is_replaced(lifecycle_browser, monkeypatch):
    page, conv, client, workspace = lifecycle_browser
    controller = _ControlledProvider(teardown_delay=0)
    launched = []

    async def controlled(websocket, conv_id, data):
        launched.append((conv_id, dict(data)))
        await controller.handle(websocket, conv_id, data)

    monkeypatch.setattr(server, "_handle_generation", controlled)
    root = await db.add_message(conv["id"], "user", "Persisted Run target")
    await page.evaluate("id => loadConversation(id, {initialLeafId: %d})" % root["id"], conv["id"])
    await page.wait_for_function("State.ws?.readyState === WebSocket.OPEN")
    original_cursor = await page.evaluate("State.loomCursor")
    original_model = await page.locator("#cc-model-inline").input_value()
    other = await db.create_conversation("Destination", mode="codex", project_dir=str(workspace))
    await page.evaluate("""args => {
        State.ws._lastActivity = 0;
        window.triggerResult = _triggerParallelGenerate(1, args.parent);
        window.navigateTask = loadConversation(args.other, {newThread: true});
    }""", {"parent": root["id"], "other": other["id"]})
    await page.evaluate("navigateTask")
    await controller.wait_for_runs(1)
    # The acknowledged launch socket accepts the captured packet independently
    # of the old viewport socket; later assertions verify its exact route.
    assert await page.evaluate("triggerResult") is True
    assert len(launched) == 1 and launched[0][0] == conv["id"]
    assert launched[0][1]["parent_id"] == root["id"]
    assert launched[0][1]["loom_thread_id"] == original_cursor["thread_id"]
    assert launched[0][1]["cc_model"] == original_model
    assert await page.evaluate("State.currentConvId") == other["id"]
    assert await page.evaluate("State.messages.length") == 0
    assert not await page.locator("#messages .streaming").count()
    controller.runs[0].release.set()
    await asyncio.wait_for(controller.runs[0].exited.wait(), 10)
    await page.wait_for_function("id => _notifications.some(n => n.id === id && n.branchActivity)", arg=controller.runs[0].draft_id)
    assert await page.evaluate("_pendingGenerates.size") == 0


async def test_visible_stale_launch_reconstructs_once_and_lands(lifecycle_browser, monkeypatch):
    page, conv, client, workspace = lifecycle_browser
    controller = _ControlledProvider(teardown_delay=0)
    monkeypatch.setattr(server, "_handle_generation", controller.handle)
    logs = []
    page.on("console", lambda msg: logs.append(msg.text))
    root = await db.add_message(conv["id"], "user", "Visible Run target")
    await page.evaluate("id => loadConversation(id, {initialLeafId: %d})" % root["id"], conv["id"])
    await page.wait_for_function("State.ws?.readyState === WebSocket.OPEN")
    await page.evaluate("id => { State.ws._lastActivity = 0; _triggerParallelGenerate(1, id); }", root["id"])
    await controller.wait_for_runs(1)
    run = controller.runs[0]
    await page.wait_for_function("text => State._liveStreamDraft?.content === text && !State._reconstructing", arg=f"provider partial {run.gen_id}")
    assert await page.evaluate("_browserTabState") == "looming"
    assert await page.locator("#messages .streaming").count() == 1
    assert len(controller.runs) == 1
    await page.evaluate("""() => {
        const packet = {type: 'stream_chunk', event_id: 'navigation-duplicate-envelope',
            conv_id: State.currentConvId, run_id: `gen:${State._followingGenId}`,
            gen_id: State._followingGenId, loom_thread_id: State.loomCursor.thread_id,
            client_instance_id: _loomClientInstanceId, content: '|transport-copy|'};
        _handleSocketMessage(packet);
        _handleSocketMessage({...packet});
    }""")
    await page.wait_for_function("text => State._liveStreamDraft?.content === text", arg=f"provider partial {run.gen_id}|transport-copy|")
    run.release.set()
    await asyncio.wait_for(run.exited.wait(), 10)
    try:
        await page.wait_for_function("text => !State.isStreaming && State.messages.some(m => m.content === text)", arg=f"controlled run {run.gen_id} complete", timeout=10000)
    except playwright.TimeoutError as exc:
        ui = await page.evaluate("""() => ({cursor: State.loomCursor, messages: State.messages,
            streaming: State.isStreaming, gen: State._followingGenId, draft: State._followingDraftMsgId,
            reconstructing: State._reconstructing, live: State._liveStreamDraft,
            pending: _pendingLaunch, subscribed: [..._subscribedRunIds], terminals: [..._recentTerminalRuns]})""")
        raise AssertionError(f"Visible reconnect failed: {ui}; logs={logs}") from exc
    assert await page.evaluate("State.loomCursor.leaf_message_id") == run.draft_id
    assert await page.evaluate("_browserTabState") == "done"
    assert await page.locator("#messages .streaming").count() == 0


async def test_send_ack_subscribes_when_launch_has_no_origin_socket(lifecycle_browser, monkeypatch):
    page, conv, client, workspace = lifecycle_browser
    controller = _ControlledProvider(teardown_delay=0)
    monkeypatch.setattr(server, "_handle_generation", controller.handle)
    monkeypatch.setattr(server, "_find_client_websocket", lambda *args: None)
    await page.locator("#user-input").fill("Launch without an origin socket")
    await page.evaluate("sendMessage()")
    await controller.wait_for_runs(1)
    run = controller.runs[0]
    await page.wait_for_function("text => State._liveStreamDraft?.content === text && !State._reconstructing", arg=f"provider partial {run.gen_id}")
    assert await page.locator("#messages .streaming").count() == 1
    run.release.set()
    await asyncio.wait_for(run.exited.wait(), 10)
    await page.wait_for_function("text => !State.isStreaming && State.messages.some(m => m.content === text)", arg=f"controlled run {run.gen_id} complete")
    assert await page.evaluate("State.loomCursor.leaf_message_id") == run.draft_id


async def test_exact_subscription_cannot_attach_other_thread_or_window(lifecycle_browser):
    page, conv, client, workspace = lifecycle_browser
    result = await page.evaluate("""() => {
        const thread = State.loomCursor.thread_id;
        _subscribedRunIds.add('gen:known-old-thread');
        const base = {type: 'generation_preparing', conv_id: State.currentConvId,
            run_id: 'gen:foreign', gen_id: 'foreign', loom_thread_id: thread,
            client_instance_id: 'another-window', parent_id: State.loomCursor.leaf_message_id};
        const foreign = _generationWindowOwnership(base);
        const oldThread = _generationWindowOwnership({...base, run_id: 'gen:known-old-thread',
            client_instance_id: _loomClientInstanceId, loom_thread_id: 'another-thread'});
        const otherConversation = _generationWindowOwnership({...base,
            run_id: 'gen:known-old-thread', conv_id: State.currentConvId + 100,
            client_instance_id: _loomClientInstanceId});
        handleWSMessage(base);
        handleWSMessage({...base, type: 'stream_start', draft_msg_id: 99999});
        return {foreign, oldThread, otherConversation, streaming: State.isStreaming,
            followed: State._followingGenId, shells: document.querySelectorAll('#messages .streaming').length};
    }""")
    assert result == {"foreign": False, "oldThread": False, "otherConversation": False,
                      "streaming": False, "followed": None, "shells": 0}


async def test_completion_received_during_snapshot_sync_is_painted(lifecycle_browser, monkeypatch):
    page, conv, client, workspace = lifecycle_browser
    controller = _ControlledProvider(teardown_delay=0)
    monkeypatch.setattr(server, "_handle_generation", controller.handle)
    await page.locator("#user-input").fill("Complete during snapshot sync")
    await page.evaluate("sendMessage()")
    await controller.wait_for_runs(1)
    run = controller.runs[0]
    await page.wait_for_function("id => State._followingDraftMsgId === id && !State._reconstructing", arg=run.draft_id)
    ready, release = asyncio.Event(), asyncio.Event()

    async def hold_snapshot_tree(route):
        response = await route.fetch()
        ready.set()
        await release.wait()
        await route.fulfill(response=response)

    # Hold the first snapshot read. A concurrent cursor revision can correctly
    # discard that read before the later branch request is ever issued.
    await page.route(f"**/api/conversations/{conv['id']}/tree", hold_snapshot_tree)
    await page.evaluate("State.ws.send(JSON.stringify({action: 'request_snapshot'}))")
    try:
        await asyncio.wait_for(ready.wait(), 10)
        run.release.set()
        await asyncio.wait_for(run.exited.wait(), 10)
        await page.wait_for_function("State._reconstructing && _queuedStreamEventsDuringReconstruct.some(e => e.type === 'stream_end')")
    finally:
        release.set()
    await page.wait_for_function("text => !State.isStreaming && State.messages.some(m => m.content === text)", arg=f"controlled run {run.gen_id} complete")
    await page.wait_for_function("_browserTabState === 'done'")
    assert await page.evaluate("State.loomCursor.leaf_message_id") == run.draft_id
    assert await page.locator("#messages .streaming").count() == 0
