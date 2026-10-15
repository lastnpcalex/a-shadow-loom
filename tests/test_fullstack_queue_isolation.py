"""Full-stack browser verification for Loom's single-agent queue invariant.

This suite intentionally crosses the boundaries that the fast ASGI tests mock:
real HTTP/WebSocket transport, independent browser contexts, reconnects, the
one-second durable-queue scheduler, and delayed provider teardown.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import socket
import threading
import time
from dataclasses import dataclass, field

import httpx
import pytest
import uvicorn

import database as db
from event_router import SubscriptionRouter
from tests.lifecycle_metrics import arm_turn_probe, install_turn_probe, observe, turn_metrics


playwright = pytest.importorskip("playwright.async_api")


async def _eventually(predicate, *, timeout: float = 10.0, interval: float = 0.05):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = predicate()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not satisfied before timeout")
        await asyncio.sleep(interval)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class _RunRecord:
    gen_id: int
    parent_id: int
    draft_id: int
    started_at: float
    release: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_requested: asyncio.Event = field(default_factory=asyncio.Event)
    exited: asyncio.Event = field(default_factory=asyncio.Event)
    exited_at: float | None = None


class _ControlledProvider:
    """Provider double whose cancellation takes longer than one queue tick."""

    def __init__(self, *, teardown_delay: float = 1.6):
        self.teardown_delay = teardown_delay
        self.runs: list[_RunRecord] = []
        self.active_count = 0
        self.max_active_count = 0

    async def wait_for_runs(self, count: int, *, timeout: float = 10.0):
        return await _eventually(
            lambda: self.runs if len(self.runs) >= count else None,
            timeout=timeout,
        )

    async def handle(self, websocket, conv_id: int, data: dict):
        import server

        task = asyncio.current_task()
        gen_key = getattr(task, "_gen_key")
        parent_id = int(data["parent_id"])
        draft = await server._create_generation_draft(
            conv_id,
            await db.get_conversation(conv_id),
            parent_id,
        )
        partial_blocks = [
            {
                "type": "tool_use",
                "name": "Read",
                "tool_id": f"tool-{gen_key[2]}",
                "input": '{"path":"example.txt"}',
                "result": "example contents",
            },
            {"type": "text", "text": f"provider partial {gen_key[2]}"},
        ]
        await db.update_message_content(
            draft["id"],
            content=f"provider partial {gen_key[2]}",
            content_blocks=json.dumps(partial_blocks),
            cc_session_id=f"controlled-session-{gen_key[2]}",
            cc_session_mode="codex",
        )
        record = _RunRecord(
            gen_id=int(gen_key[2]),
            parent_id=parent_id,
            draft_id=int(draft["id"]),
            started_at=time.monotonic(),
        )
        self.runs.append(record)
        self.active_count += 1
        self.max_active_count = max(self.max_active_count, self.active_count)

        server._update_gen_snapshot(
            gen_key,
            full_text=f"provider partial {gen_key[2]}",
            content_blocks=partial_blocks,
            started_at=time.time(),
            draft_msg_id=draft["id"],
            parent_id=parent_id,
            cc_model="controlled-provider",
            mode="codex",
            client_instance_id=data.get("client_instance_id"),
            loom_thread_id=draft.get("loom_thread_id"),
        )
        await db.register_active_generation(
            draft["id"],
            conv_id,
            900000 + int(gen_key[2]),
            mode="codex",
            loom_thread_id=draft.get("loom_thread_id"),
        )
        await server._ws_send(
            conv_id,
            {
                "type": "stream_start",
                "parent_id": parent_id,
                "draft_msg_id": draft["id"],
                "cc_model": "controlled-provider",
            },
        )
        await server._ws_send(
            conv_id,
            {
                "type": "stream_chunk",
                "content": f"provider partial {gen_key[2]}",
            },
        )

        try:
            await record.release.wait()
            content = f"controlled run {record.gen_id} complete"
            await db.update_message_content(draft["id"], content=content)
            await server._ws_send(
                conv_id,
                {
                    "type": "stream_end",
                    "message": await db.get_message(draft["id"]),
                },
            )
        except asyncio.CancelledError:
            record.cancel_requested.set()
            # A real provider process tree does not disappear at task.cancel().
            # Keep the provider alive beyond the scheduler's one-second tick.
            await asyncio.sleep(self.teardown_delay)
            await server._finalize_user_stopped_generation(
                conv_id,
                draft["id"],
                partial_text=f"provider partial {gen_key[2]}",
                content_blocks=partial_blocks,
                session_id=f"controlled-session-{gen_key[2]}",
                session_mode="codex",
                model_used="controlled-provider",
            )
            raise
        finally:
            self.active_count -= 1
            record.exited_at = time.monotonic()
            record.exited.set()
            server._active_generations.pop(gen_key, None)
            server._generation_snapshots.pop(gen_key, None)
            await db.unregister_active_generation(draft["id"])
            await server._send_generation_idle_if_quiescent(conv_id)


async def test_real_browser_queue_waits_for_provider_exit_and_survives_reconnect(
    tmp_database, tmp_path, monkeypatch, record_property, isolate_model_discovery
):
    """A queued run cannot overlap its predecessor in a non-parallel lane."""
    import server

    isolate_model_discovery()
    controller = _ControlledProvider()
    monkeypatch.setattr(server, "_handle_generation", controller.handle)
    server._active_generations.clear()
    server._generation_snapshots.clear()
    server._active_websockets.clear()
    monkeypatch.setattr(server, "_event_router", SubscriptionRouter())

    port = _free_port()
    uv_config = uvicorn.Config(
        server.app,
        ws="websockets-sansio",
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="off",
    )
    uv_server = uvicorn.Server(uv_config)
    server_task = asyncio.create_task(uv_server.serve())
    scheduler_task = asyncio.create_task(server._queued_generation_scheduler_loop())
    base_url = f"http://127.0.0.1:{port}"

    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=5) as client:
            await _eventually(
                lambda: uv_server.started,
                timeout=10,
            )
            response = await client.post(
                "/api/conversations",
                json={
                    "title": "Queue isolation E2E",
                    "mode": "claude",
                    "cc_model": "gpt-5.6-sol",
                    "parallel_agents_enabled": False,
                },
            )
            response.raise_for_status()
            conv = response.json()

            async with playwright.async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                context_a = await browser.new_context()
                context_b = await browser.new_context()
                page_a = await context_a.new_page()
                page_b = await context_b.new_page()
                page_errors: list[str] = []
                network_events: list[tuple] = []
                page_a.on("pageerror", lambda exc: page_errors.append(str(exc)))
                page_b.on("pageerror", lambda exc: page_errors.append(str(exc)))
                page_a.on(
                    "request",
                    lambda request: network_events.append(
                        ("request", request.method, request.url, request.post_data)
                    ) if request.method == "POST" else None,
                )
                page_a.on(
                    "response",
                    lambda response: network_events.append(
                        ("response", response.status, response.url)
                    ) if response.request.method == "POST" else None,
                )

                for page in (page_a, page_b):
                    await page.goto(
                        f"{base_url}/?conversation={conv['id']}&thread=new&view=chat",
                        wait_until="domcontentloaded",
                    )
                    await page.locator("#user-input").wait_for(state="visible")
                    await page.wait_for_function(
                        "State.ws && State.ws.readyState === WebSocket.OPEN "
                        "&& !State._restoringView && document.querySelector('#loading-overlay').classList.contains('hidden')"
                    )

                await install_turn_probe(page_a)
                await page_a.locator("#user-input").fill("first controlled turn")
                await arm_turn_probe(page_a)
                await page_a.evaluate("sendMessage()")
                await controller.wait_for_runs(1)
                first = controller.runs[0]
                await page_a.wait_for_function("State.isStreaming === true")
                await page_a.wait_for_function(
                    "document.querySelector('.looming-phase') "
                    "&& !document.querySelector('.looming-phase').classList.contains('hidden') "
                    "&& document.querySelector('.loom-anim')?.textContent?.trim()"
                )
                active_turn_ui = await page_a.evaluate("""() => ({
                    clotho: !!document.querySelector('.stream-waiting'),
                    loom: document.querySelector('.loom-anim')?.textContent || '',
                    looming: document.querySelector('.looming-text')?.textContent || '',
                    cancel: document.querySelector('.cancel-draft-btn')?.textContent || '',
                    interrupt: document.querySelector('.interrupt-draft-btn')?.textContent || '',
                })""")
                assert active_turn_ui["clotho"] is False, active_turn_ui
                assert active_turn_ui["loom"].strip(), active_turn_ui
                timing = await turn_metrics(page_a)
                assert {"send_to_shell_ms", "send_to_first_output_ms"} <= timing.keys()
                assert active_turn_ui["looming"] == "Looming", active_turn_ui
                assert active_turn_ui["cancel"] == "⊘", active_turn_ui
                assert active_turn_ui["interrupt"] == "⏸", active_turn_ui

                # Pin the exact in-flight assistant through the current dialog.
                # The draft is durable before the cached tree contains it.
                await page_a.evaluate("workspaceSetOpen('left', true); setWorkspaceLeftMode(false)")
                await page_a.locator('#workspace-pin-current').click()
                await page_a.get_by_role('textbox', name='Pin label', exact=True).fill('Live draft path')
                await page_a.locator('.workspace-pin-dialog [type="submit"]').click()
                await page_a.wait_for_function("State.branchPins.some(pin => pin.label === 'Live draft path')")
                live_pin = next(pin for pin in await db.get_branch_pins(conv['id']) if pin['label'] == 'Live draft path')
                assert live_pin['message_id'] == first.draft_id
                await page_a.locator('#btn-bookmarks-global').click()
                assert await page_a.locator('#bookmarks-list-global .workspace-item').filter(has_text='Live draft path').is_enabled()
                await page_a.locator('#btn-bookmarks-global').click()

                # A separate browser is parked on a separate empty thread. It
                # must neither render A's private stream nor launch a sibling
                # while parallel agents are disabled. Its normal send is still
                # durable: it becomes a queued root behind A's occupied lane.
                assert await page_b.evaluate("State.isStreaming") is False
                assert "provider partial" not in await page_b.locator("#messages").inner_text()
                await page_b.locator("#user-input").fill("queued standalone root")
                await page_b.evaluate("sendMessage()")
                await page_b.wait_for_function("_queuedGeneration !== null")
                assert len(controller.runs) == 1
                queued_root_id = await page_b.evaluate("_queuedGeneration.id")
                queued_root = await db.get_message(int(queued_root_id))
                first_prompt = await db.get_message(first.parent_id)
                assert queued_root["content"] == "queued standalone root"
                assert queued_root["parent_id"] is None
                assert queued_root["loom_thread_id"] != first_prompt["loom_thread_id"]
                await page_b.evaluate("cancelQueuedMessage(_queuedGeneration)")
                queue_deadline = asyncio.get_running_loop().time() + 5
                while await db.list_queued_generations(conv["id"]):
                    if asyncio.get_running_loop().time() >= queue_deadline:
                        raise AssertionError("queued standalone root was not cancelled")
                    await asyncio.sleep(0.05)

                # Queue a continuation from A, background the tab, then force
                # its WebSocket to reconnect. No hard refresh is allowed to
                # help the page and refocus must not change its durable cursor.
                await page_a.locator("#user-input").fill("queued continuation")
                async with page_a.expect_request(lambda request: request.method == "POST" and request.url.endswith(f"/api/conversations/{conv['id']}/messages")) as sent_queue_request:
                    await page_a.evaluate("sendMessage()")
                sent_queue_payload = (await sent_queue_request.value).post_data_json
                await asyncio.sleep(0.25)
                queue = (await client.get(
                    f"/api/conversations/{conv['id']}/generation-queue"
                )).json()
                if len(queue) != 1:
                    browser_state = await page_a.evaluate(
                        "({isStreaming: State.isStreaming, actual: _isActuallyStreaming(), "
                        "followingDraft: State._followingDraftMsgId, "
                        "input: document.querySelector('#user-input').value, "
                        "queued: _queuedGeneration, messages: State.messages})"
                    )
                    raise AssertionError(
                        f"browser did not persist the queued continuation: {browser_state!r}"
                    )
                try:
                    await page_a.wait_for_function(
                        "document.querySelector('.message.queued-message') !== null",
                        timeout=5_000,
                    )
                except Exception as exc:
                    browser_state = await page_a.evaluate(
                        "({queued: _queuedGeneration, queueItems: _generationQueueItems, "
                        "messages: State.messages, html: document.querySelector('#messages').innerHTML, "
                        "editing: !!document.querySelector('.edit-message-input'), "
                        "renderDeferred: _messageRenderDeferredForEdit, cursor: State.loomCursor, "
                        "convId: State.currentConvId, input: document.querySelector('#user-input').value})"
                    )
                    browser_state["serverQueueForCursor"] = await page_a.evaluate(
                        "async () => { const t = State.loomCursor?.thread_id || ''; "
                        "return await (await fetch(`/api/conversations/${State.currentConvId}/generation-queue?loom_thread_id=${encodeURIComponent(t)}`)).json(); }"
                    )
                    raise AssertionError(
                        f"queued message was not rendered: {browser_state!r}"
                    ) from exc
                assert queue[0]["content"] == "queued continuation"
                replay = await client.post(f"/api/conversations/{conv['id']}/messages", json=sent_queue_payload)
                assert replay.status_code == 200, replay.text
                assert replay.json()["id"] == queue[0]["user_msg_id"]
                assert replay.json()["idempotent_replay"] is True
                assert len(await db.list_queued_generations(conv["id"])) == 1
                assert len(controller.runs) == 1
                visual_order = await page_a.evaluate("""({draftId, queuedId}) => {
                    const messages = State.messages || [];
                    const draftStateIndex = messages.findIndex(message =>
                        String(message.id) === String(draftId));
                    const queuedStateIndex = messages.findIndex(message =>
                        String(message.id) === String(queuedId));
                    const rows = [...document.querySelectorAll('#messages > .message')];
                    const draftEl = document.querySelector(
                        `#messages > .message[data-msg-id="${draftId}"]`);
                    const queuedEl = document.querySelector(
                        `#messages > .message[data-msg-id="${queuedId}"]`);
                    return {
                        draftStateIndex,
                        queuedStateIndex,
                        draftDomIndex: rows.indexOf(draftEl),
                        queuedDomIndex: rows.indexOf(queuedEl),
                        streaming: !!draftEl?.classList.contains('streaming'),
                    };
                }""", {
                    "draftId": queue[0]["wait_for_draft_id"],
                    "queuedId": queue[0]["user_msg_id"],
                })
                assert visual_order["draftStateIndex"] >= 0
                assert visual_order["queuedStateIndex"] == visual_order["draftStateIndex"] + 1
                assert visual_order["queuedDomIndex"] == visual_order["draftDomIndex"] + 1
                assert visual_order["streaming"] is True

                cursor_before_background = await page_a.evaluate(
                    "JSON.stringify(State.loomCursor)"
                )
                await page_b.bring_to_front()
                await page_a.evaluate(
                    "document.dispatchEvent(new Event('visibilitychange'))"
                )

                reconnect_started = time.perf_counter()
                await page_a.evaluate("window.__queueTestSocket = State.ws; State.ws.close()")
                await page_a.wait_for_function(
                    "State.ws && State.ws !== window.__queueTestSocket "
                    "&& State.ws.readyState === WebSocket.OPEN",
                    timeout=10_000,
                )
                await page_a.wait_for_function(
                    "_queuedGeneration && _queuedGeneration.content === 'queued continuation'"
                )
                await page_a.bring_to_front()
                await page_a.evaluate(
                    "document.dispatchEvent(new Event('visibilitychange'))"
                )
                assert await page_a.evaluate(
                    "JSON.stringify(State.loomCursor)"
                ) == cursor_before_background

                timing["reconnect_ms"] = (time.perf_counter() - reconnect_started) * 1000

                # Admin kill is the dangerous race seen in production:
                # teardown takes longer than the queue scheduler interval.
                # Kill must pause the queued continuation; it must never treat
                # cancellation as permission to start another provider.
                kill_response = await client.post(
                    f"/api/generations/{first.draft_id}/kill"
                )
                assert kill_response.status_code == 200, kill_response.text
                await asyncio.wait_for(first.cancel_requested.wait(), timeout=5)
                await asyncio.wait_for(first.exited.wait(), timeout=5)
                await asyncio.sleep(2.2)  # cross two scheduler ticks
                assert len(controller.runs) == 1
                assert controller.max_active_count == 1
                paused_queue = (await client.get(
                    f"/api/conversations/{conv['id']}/generation-queue"
                )).json()
                assert len(paused_queue) == 1
                assert paused_queue[0]["status"] == "paused"
                await page_a.wait_for_function(
                    "_queuedGeneration && _queuedGeneration.queue_status === 'paused'"
                )
                await page_a.locator('.queue-resume-btn').wait_for(state="visible")
                assert await page_a.evaluate(
                    "JSON.stringify(State.loomCursor)"
                ) == cursor_before_background

                # Only an explicit Resume action may reopen the serial lane.
                await page_a.locator('.queue-resume-btn').click()
                await controller.wait_for_runs(2, timeout=10)
                second = controller.runs[1]
                assert first.exited_at is not None
                assert second.started_at >= first.exited_at
                assert controller.max_active_count == 1

                await page_a.wait_for_function(
                    "document.querySelector('.message.queued-message') === null",
                    timeout=10_000,
                )
                assert await page_a.evaluate("_queuedGeneration === null") is True

                second.release.set()
                await asyncio.wait_for(second.exited.wait(), timeout=5)
                await page_a.wait_for_function("State.isStreaming === false")
                final_tree = (
                    await client.get(f"/api/conversations/{conv['id']}/tree")
                ).json()
                queued = await db.get_message(int(queue[0]["user_msg_id"]))
                assert queued is not None, final_tree
                assert queued["content"] == "queued continuation"
                second_draft = await db.get_message(second.draft_id)
                assert second_draft is not None, final_tree
                assert second_draft["content"] == (
                    f"controlled run {second.gen_id} complete"
                )
                assert second_draft["parent_id"] == queued["id"]
                assert queued["parent_id"] == first.draft_id
                observe(record_property, "queue_browser", **timing,
                        controlled_max_active=controller.max_active_count, launches=2)
                assert page_errors == []

                # Editing a user turn creates a sibling branch, but it is not
                # a request for concurrent execution. This must work with the
                # conversation's parallel-agent option disabled.
                prior_edit_run_count = len(controller.runs)
                await page_a.evaluate("id => editMessage(id)", queued["id"])
                await page_a.locator(".edit-message-input").fill(
                    "edited sibling wording"
                )
                await page_a.locator(".edit-save").click()
                await controller.wait_for_runs(prior_edit_run_count + 1)
                edit_run = controller.runs[-1]
                edit_prompt = await db.get_message(edit_run.parent_id)
                assert edit_prompt["content"] == "edited sibling wording"
                assert edit_prompt["parent_id"] == queued["parent_id"]
                assert edit_prompt["send_intent"] == "continue"
                edit_run.release.set()
                await asyncio.wait_for(edit_run.exited.wait(), timeout=5)

                # The shared composer has different, intentional topology in
                # tree view: it always creates a standalone root, regardless
                # of the selected/latest branch displayed behind it.
                prior_tree_run_count = len(controller.runs)
                await page_a.evaluate("switchView('tree')")
                await page_a.locator("#user-input").fill("tree composer root")
                tree_network_start = len(network_events)
                await page_a.evaluate("sendMessage()")
                try:
                    await controller.wait_for_runs(prior_tree_run_count + 1)
                except AssertionError as exc:
                    browser_state = await page_a.evaluate("""() => ({
                        convId: State.currentConvId,
                        view: State.currentView, streaming: State.isStreaming,
                        actuallyStreaming: _isActuallyStreaming(),
                        sendInFlight: _sendInFlight,
                        input: document.querySelector('#user-input').value,
                        cursor: State.loomCursor,
                        pendingSend: _pendingSendRequest,
                        queued: _queuedGeneration,
                        toasts: [...document.querySelectorAll('.toast')].map(el => el.textContent),
                        errors: [...document.querySelectorAll('.error-message')].map(el => el.textContent),
                        messages: State.messages.map(m => ({id:m.id,parent:m.parent_id,content:m.content})),
                    })""")
                    durable_queue = await db.list_queued_generations(conv["id"])
                    durable_lease = await db.get_live_generation_lease(conv["id"])
                    durable_active = [
                        row for row in await db.list_active_generations()
                        if int(row["conv_id"]) == int(conv["id"])
                    ]
                    durable_tree = (
                        await client.get(f"/api/conversations/{conv['id']}/tree")
                    ).json()
                    server_active = [
                        {
                            "key": key,
                            "done": task.done(),
                            "snapshot": server._generation_snapshots.get(key),
                        }
                        for key, task in server._active_generations.items()
                        if int(key[0]) == int(conv["id"])
                    ]
                    raise AssertionError(
                        f"tree composer did not launch: browser={browser_state!r}; "
                        f"durable_queue={durable_queue!r}; "
                        f"durable_lease={durable_lease!r}; "
                        f"durable_active={durable_active!r}; "
                        f"server_active={server_active!r}; "
                        f"network={network_events[tree_network_start:]!r}; "
                        f"durable_tree={durable_tree!r}"
                    ) from exc
                tree_run = controller.runs[-1]
                tree_prompt = await db.get_message(tree_run.parent_id)
                assert tree_prompt["content"] == "tree composer root"
                assert tree_prompt["parent_id"] is None
                assert tree_prompt["loom_thread_id"] != queued["loom_thread_id"]
                await page_a.wait_for_function("State.currentView === 'chat'")
                immediate_tree_root = await page_a.evaluate("""() => ({
                    stateContents: State.messages.map(message => message.content || ''),
                    stateThreads: State.messages.map(message => message.loom_thread_id),
                    rendered: document.querySelector('#messages').innerText,
                    streamingConnected: !!document.querySelector('.message.assistant.streaming'),
                })""")
                assert immediate_tree_root["stateContents"][0] == "tree composer root"
                assert all(
                    str(thread_id) == str(tree_prompt["loom_thread_id"])
                    for thread_id in immediate_tree_root["stateThreads"]
                )
                assert "queued continuation" not in immediate_tree_root["rendered"]
                assert "edited sibling wording" not in immediate_tree_root["rendered"]
                assert immediate_tree_root["streamingConnected"] is True
                tree_run.release.set()
                await asyncio.wait_for(tree_run.exited.wait(), timeout=5)

                # Stop controls have intentionally different context outcomes.
                # Interrupt preserves the partial tool transcript/session and
                # permits an ordinary continuation from that exact turn.
                stop_response = await client.post(
                    "/api/conversations",
                    json={
                        "title": "Cancel versus interrupt E2E",
                        "mode": "codex",
                        "cc_model": "gpt-5.6-sol",
                        "project_dir": str(tmp_path),
                        "parallel_agents_enabled": False,
                    },
                )
                stop_response.raise_for_status()
                stop_conv = stop_response.json()
                await page_a.goto(
                    f"{base_url}/?conversation={stop_conv['id']}&thread=new&view=chat",
                    wait_until="domcontentloaded",
                )
                await page_a.wait_for_function(
                    "State.ws && State.ws.readyState === WebSocket.OPEN "
                    "&& !State._restoringView && document.querySelector('#loading-overlay').classList.contains('hidden')"
                )
                prior_stop_runs = len(controller.runs)
                await page_a.locator("#user-input").fill("interrupt this turn")
                await page_a.evaluate("sendMessage()")
                await controller.wait_for_runs(prior_stop_runs + 1)
                interrupted_run = controller.runs[-1]
                try:
                    await page_a.locator(".interrupt-draft-btn").wait_for()
                except Exception:
                    print('Missing stop control:', await page_a.evaluate('''() => ({
                        view: State.currentView, conv: State.currentConvId,
                        streaming: State.isStreaming, reconstructing: State._reconstructing,
                        following: State._followingGenId, cursor: State.loomCursor,
                        messages: State.messages.map(m => ({id:m.id, parent:m.parent_id})),
                        shell: document.querySelector('.message.streaming')?.outerHTML,
                    })'''))
                    raise
                assert await page_a.locator(".cancel-draft-btn").inner_text() == "⊘"
                assert await page_a.locator(".interrupt-draft-btn").inner_text() == "⏸"
                await page_a.locator(".interrupt-draft-btn").click()
                await asyncio.wait_for(interrupted_run.exited.wait(), timeout=8)
                await page_a.wait_for_function("State.isStreaming === false")
                interrupted_msg = await db.get_message(interrupted_run.draft_id)
                assert interrupted_msg["turn_lifecycle_state"] == "interrupted"
                assert interrupted_msg["content"] == (
                    f"provider partial {interrupted_run.gen_id}"
                )
                assert interrupted_msg["cc_session_id"] == (
                    f"controlled-session-{interrupted_run.gen_id}"
                )
                assert "example contents" in interrupted_msg["content_blocks"]

                await page_a.locator("#user-input").fill("continue after interrupt")
                await page_a.evaluate("sendMessage()")
                await controller.wait_for_runs(prior_stop_runs + 2)
                continuation_run = controller.runs[-1]
                continuation_prompt = await db.get_message(continuation_run.parent_id)
                assert continuation_prompt["parent_id"] == interrupted_msg["id"]
                continuation_run.release.set()
                await asyncio.wait_for(continuation_run.exited.wait(), timeout=5)
                await page_a.wait_for_function("State.isStreaming === false")
                await page_a.evaluate(
                    "async () => { if (State._cursorSyncPromise) await State._cursorSyncPromise; }"
                )

                # Cancel keeps the prompt/topology but removes partial output,
                # tool blocks, and native session continuity for a clean retry.
                await page_a.evaluate(
                    "convId => loadConversation(convId, {newThread: true})",
                    stop_conv["id"],
                )
                await page_a.wait_for_function(
                    "State.ws && State.ws.readyState === WebSocket.OPEN"
                )
                prior_cancel_runs = len(controller.runs)
                await page_a.locator("#user-input").fill("cancel this turn")
                await page_a.evaluate("sendMessage()")
                await controller.wait_for_runs(prior_cancel_runs + 1)
                cancelled_run = controller.runs[-1]
                await page_a.locator(".cancel-draft-btn").wait_for()
                await page_a.locator(".cancel-draft-btn").click()
                await asyncio.wait_for(cancelled_run.exited.wait(), timeout=8)
                await page_a.wait_for_function("State.isStreaming === false")
                cancelled_msg = await db.get_message(cancelled_run.draft_id)
                assert cancelled_msg["turn_lifecycle_state"] == "cancelled"
                assert cancelled_msg["content"] == (
                    "[Turn cancelled; partial response discarded]"
                )
                assert cancelled_msg["content_blocks"] is None
                assert cancelled_msg["cc_session_id"] is None
                assert cancelled_msg["cc_session_mode"] is None

                # Positive control: when the workspace explicitly enables
                # parallel agents, two independent pages may overlap, but each
                # must render only the run launched from that page.
                parallel_workspace = tmp_path / "parallel-workspace"
                parallel_workspace.mkdir()
                parallel_response = await client.post(
                    "/api/conversations",
                    json={
                        "title": "Intentional parallel E2E",
                        "mode": "claude",
                        "cc_model": "gpt-5.6-sol",
                        "project_dir": str(parallel_workspace),
                        "parallel_agents_enabled": True,
                    },
                )
                parallel_response.raise_for_status()
                parallel_conv = parallel_response.json()
                await asyncio.gather(
                    page_a.evaluate(
                        "convId => loadConversation(convId, {newThread: true})",
                        parallel_conv["id"],
                    ),
                    page_b.evaluate(
                        "convId => loadConversation(convId, {newThread: true})",
                        parallel_conv["id"],
                    ),
                )
                await asyncio.gather(
                    page_a.wait_for_function(
                        "State.ws && State.ws.readyState === WebSocket.OPEN"
                    ),
                    page_b.wait_for_function(
                        "State.ws && State.ws.readyState === WebSocket.OPEN"
                    ),
                )
                await page_a.locator("#user-input").fill("parallel page A")
                await page_b.locator("#user-input").fill("parallel page B")
                prior_run_count = len(controller.runs)
                await asyncio.gather(
                    page_a.evaluate("sendMessage()"),
                    page_b.evaluate("sendMessage()"),
                )
                await controller.wait_for_runs(prior_run_count + 2)
                parallel_runs = controller.runs[prior_run_count:]
                assert controller.active_count == 2
                assert controller.max_active_count == 2

                run_by_prompt = {}
                for run in parallel_runs:
                    prompt = await db.get_message(run.parent_id)
                    run_by_prompt[prompt["content"]] = run
                run_a = run_by_prompt["parallel page A"]
                run_b = run_by_prompt["parallel page B"]
                await page_a.wait_for_function(
                    "text => document.querySelector('#messages').innerText.includes(text)",
                    arg=f"provider partial {run_a.gen_id}",
                )
                await page_b.wait_for_function(
                    "text => document.querySelector('#messages').innerText.includes(text)",
                    arg=f"provider partial {run_b.gen_id}",
                )
                assert f"provider partial {run_b.gen_id}" not in (
                    await page_a.locator("#messages").inner_text()
                )
                assert f"provider partial {run_a.gen_id}" not in (
                    await page_b.locator("#messages").inner_text()
                )

                # The activity drawer can inspect both exact branches without
                # subscribing this page to the sibling's generated content or
                # changing the branch this page is viewing.
                # Rendering a stream does not await its own cursor POST. Let
                # that legitimate user -> draft advance settle before measuring
                # whether opening the drawer changes the selected branch.
                await page_a.wait_for_function(
                    "id => Number(State.loomCursor?.leaf_message_id) === id",
                    arg=run_a.draft_id,
                )
                visible_branch_before = await page_a.evaluate(
                    "String(State.loomCursor?.leaf_message_id || '')"
                )
                await page_a.locator("#notif-bell").click()
                await page_a.wait_for_function(
                    "document.querySelectorAll('.parallel-activity-item').length === 2"
                )
                activity_text = await page_a.locator(
                    ".parallel-activity-section"
                ).inner_text()
                assert activity_text.count("Looming") == 2
                assert "Codex" in activity_text
                assert "Message #" not in activity_text
                assert await page_a.evaluate(
                    "String(State.loomCursor?.leaf_message_id || '')"
                ) == visible_branch_before
                assert f"provider partial {run_b.gen_id}" not in (
                    await page_a.locator("#messages").inner_text()
                )
                await page_a.locator("#notif-clear-notifications").click()
                run_a.release.set()
                run_b.release.set()
                await asyncio.gather(run_a.exited.wait(), run_b.exited.wait())

                await context_b.close()
                await context_a.close()
                await browser.close()
    finally:
        for record in controller.runs:
            record.release.set()
        for task in list(server._active_generations.values()):
            task.cancel()
        if server._active_generations:
            await asyncio.gather(
                *list(server._active_generations.values()),
                return_exceptions=True,
            )
        server._active_generations.clear()
        server._generation_snapshots.clear()
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task
        uv_server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(server_task, timeout=10)


async def test_real_browser_can_cancel_visible_prelaunch_without_losing_prompt(
    tmp_database, tmp_path, monkeypatch, record_property, isolate_model_discovery
):
    """Workspace preparation is immediate UI state and remains cancellable."""
    import server
    from agent_supervisor import AgentSupervisor

    isolate_model_discovery()
    capture_started = threading.Event()
    capture_release = threading.Event()
    provider_entered = asyncio.Event()
    child_emit_gate = asyncio.Event()
    child_hold = asyncio.Event()
    child_permission_gate = asyncio.Event()
    child_decisions = []
    approval_text = "fixture review: " + "bounded-artifact " * 25

    async def controlled_child(_run, emit):
        await child_emit_gate.wait()
        await emit("agent_launch_info", {
            "effective_harness": "codex",
            "effective_model": "gpt-5.6-sol",
        })
        await emit("stream_chunk", {"content": "live child output"})
        await child_permission_gate.wait()
        for index in range(2):
            decision = await server.handle_cc_permission({
                "loom_conv_id": _run["conversation_id"],
                "agent_run_id": _run["run_id"],
                "tool_name": "FixtureReview",
                "tool_input": {"command": f"{approval_text}{index}"},
                "source": "fixture-child",
            })
            child_decisions.append(decision)
        await child_hold.wait()
        return {"text": "live child complete"}

    def blocking_capture(*args, **kwargs):
        capture_started.set()
        capture_release.wait(timeout=15)
        return object()

    async def should_not_launch(websocket, conv_id, data):
        provider_entered.set()

    server._active_generations.clear()
    server._generation_snapshots.clear()
    server._active_websockets.clear()
    monkeypatch.setattr(server, "_event_router", SubscriptionRouter())
    monkeypatch.setattr(server, "capture_workspace_snapshot", blocking_capture)
    monkeypatch.setattr(server, "default_recovery_root", lambda: tmp_path / "recovery")
    monkeypatch.setattr(server, "_handle_generation", should_not_launch)
    original_agent_supervisor = server._agent_supervisor
    child_supervisor = AgentSupervisor(
        launcher=controlled_child,
        event_sink=server._publish_agent_event,
    )
    server._agent_supervisor = child_supervisor
    await child_supervisor.start()

    workspace = tmp_path / "prelaunch-workspace"
    workspace.mkdir()
    port = _free_port()
    uv_server = uvicorn.Server(
        uvicorn.Config(
            server.app,
            ws="websockets-sansio",
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="off",
        )
    )
    server_task = asyncio.create_task(uv_server.serve())
    base_url = f"http://127.0.0.1:{port}"

    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=5) as client:
            await _eventually(lambda: uv_server.started, timeout=10)
            response = await client.post(
                "/api/conversations",
                json={
                    "title": "Visible prelaunch",
                    "mode": "codex",
                    "cc_model": "gpt-5.6-sol",
                    "project_dir": str(workspace),
                    "parallel_agents_enabled": False,
                },
            )
            response.raise_for_status()
            conv = response.json()
            long_agent_task = "Review the current UI without changing files. " * 20
            await db.create_agent_run(
                "browser-agent-manager-run",
                conv["id"],
                long_agent_task,
                requested_harness="codex",
                requested_model="gpt-5.6-sol",
                project_dir=str(workspace),
            )
            await db.update_agent_run(
                "browser-agent-manager-run",
                status="completed",
                result={"text": "UI review completed"},
                completed_at=time.time(),
            )

            async with playwright.async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                context = await browser.new_context()
                page = await context.new_page()
                page_errors: list[str] = []
                page.on("pageerror", lambda exc: page_errors.append(str(exc)))
                # Enter through normal startup. A manual load while init() is
                # restoring this route races two cursor-creation requests.
                async with page.expect_request("**/api/health"):
                    await page.goto(
                        f"{base_url}/?conversation={conv['id']}&thread=new&view=chat",
                        wait_until="domcontentloaded",
                    )
                await page.locator("#user-input").wait_for(state="visible")
                await page.wait_for_function(
                    "State.ws?.readyState === WebSocket.OPEN && !State._restoringView "
                    "&& document.querySelector('#loading-overlay').classList.contains('hidden')"
                )

                # Durable child runs are managed from a compact popover beside
                # Canvas, never rendered as a full-width object in chat.
                assert await page.locator("#messages-container #agent-manager-panel").count() == 0
                assert await page.locator("#btn-agent-manager").is_visible()
                await page.locator("#btn-agent-manager").click()
                await page.locator("#agent-manager-panel").wait_for(state="visible")
                agent_manager_ui = await page.evaluate("""() => {
                    const panel = document.querySelector('#agent-manager-panel');
                    const task = panel.querySelector('.agent-task');
                    const anchor = document.querySelector('#agent-manager-anchor');
                    const bounds = panel.getBoundingClientRect();
                    const taskStyle = getComputedStyle(task);
                    return {
                        inAgentPane: anchor.parentElement?.id === 'cc-inline-controls',
                        width: bounds.width,
                        viewportWidth: innerWidth,
                        taskIsTruncated: taskStyle.overflow === 'hidden'
                            && taskStyle.textOverflow === 'ellipsis'
                            && task.scrollWidth > task.clientWidth,
                    };
                }""")
                assert agent_manager_ui["inAgentPane"] is True
                assert agent_manager_ui["width"] <= 1072
                assert agent_manager_ui["width"] <= agent_manager_ui["viewportWidth"] - 32
                assert agent_manager_ui["taskIsTruncated"] is True
                await page.locator(".agent-run-card > summary").click()
                await page.locator('.agent-task-details > summary').click()
                assert long_agent_task.strip() in await page.locator(".agent-full-task").inner_text()
                await page.locator("#agent-manager-close").click()

                # A newly spawned child updates the closed panel badge through
                # the exact run-scoped WebSocket subscription. Its output is
                # live, and panel cancellation affects only that child.
                client_instance_id = await page.evaluate("() => _loomClientInstanceId")
                child_response = await client.post(
                    f"/api/conversations/{conv['id']}/agent-runs",
                    json={
                        "task": "live child task",
                        "harness": "codex",
                        "model": "gpt-5.6-sol",
                        "project_dir": str(workspace),
                        "client_instance_id": client_instance_id,
                    },
                )
                child_response.raise_for_status()
                child_run = child_response.json()
                child_emit_gate.set()
                await page.wait_for_function(
                    "document.querySelector('#agent-manager-count')?.textContent === '1'",
                    timeout=10_000,
                )
                await page.locator("#btn-agent-manager").click()
                await page.locator("#agent-manager-panel").wait_for(state="visible")
                live_card = page.locator(
                    f'.agent-run-card:has([data-agent-cancel="{child_run["run_id"]}"])'
                )
                await live_card.locator(":scope > summary").click()
                await page.wait_for_function(
                    "text => document.querySelector('#agent-manager-panel')?.textContent.includes(text)",
                    arg="live child output",
                )

                # The bridge's low sequence number must not be discarded after
                # persisted child output. Review uses the existing permission
                # notification controls, without touching the parent chat.
                parent_html = await page.locator("#messages").inner_html()
                child_permission_gate.set()
                await live_card.locator("[data-agent-permission]").wait_for(state="visible")
                assert await live_card.get_attribute("open") is not None
                await live_card.locator("[data-agent-permission]").click()
                notification = page.locator(".notif-permission:not(.notif-resolved)")
                await notification.wait_for(state="visible")
                assert approval_text.strip() in await notification.inner_text()
                await notification.locator(".permission-feedback-input").fill("Use only this fixture")
                await notification.locator('[data-action="allow"]').click()
                await _eventually(lambda: len(child_decisions) == 1)
                assert child_decisions[0]["allow"] is True
                assert child_decisions[0]["feedback"] == "Use only this fixture"
                assert await page.locator("#messages").inner_html() == parent_html
                await _eventually(lambda: any(
                    p.get("response") is None
                    for p in server._pending_hook_permissions.values()
                    if p.get("agent_run_id") == child_run["run_id"]
                ))

                # A hard reload must recover the active badge, subscription,
                # and persisted output without requiring the parent agent or
                # a manual refresh/open cycle first.
                async with page.expect_request("**/api/health"):
                    await page.reload(wait_until="domcontentloaded")
                await page.wait_for_function(
                    "State.ws?.readyState === WebSocket.OPEN && !State._restoringView "
                    "&& document.querySelector('#loading-overlay').classList.contains('hidden')"
                )
                await page.locator("#user-input").wait_for(state="visible")
                await page.wait_for_function(
                    "document.querySelector('#agent-manager-count')?.textContent === '1'",
                    timeout=10_000,
                )
                await page.locator("#btn-agent-manager").click()
                await page.locator("#agent-manager-panel").wait_for(state="visible")
                live_card = page.locator(
                    f'.agent-run-card:has([data-agent-cancel="{child_run["run_id"]}"])'
                )
                await live_card.locator(":scope > summary").click()
                await page.wait_for_function(
                    "text => document.querySelector('#agent-manager-panel')?.textContent.includes(text)",
                    arg="live child output",
                )
                # New page identity recovers the pending child request only
                # after subscribing to that child. Denial feedback reaches the
                # bridge, and resolved UUIDs cannot resurrect on late replay.
                await live_card.locator("[data-agent-permission]").wait_for(state="visible")
                await live_card.locator("[data-agent-permission]").click()
                notification = page.locator(".notif-permission:not(.notif-resolved)")
                await notification.wait_for(state="visible")
                replay = await page.evaluate("() => ({...State.agentRuns.find(r => r._permissionPending)._permissions})")
                await notification.locator(".permission-feedback-input").fill("Do not use the other fixture")
                await notification.locator('[data-action="deny"]').click()
                await _eventually(lambda: len(child_decisions) == 2)
                assert child_decisions[1]["allow"] is False
                assert child_decisions[1]["decision_source"] == "user"
                assert child_decisions[1]["feedback"] == "Do not use the other fixture"
                await page.wait_for_function("!State.agentRuns.some(r => r._permissionPending)")
                await page.evaluate("events => Object.values(events).forEach(_handleAgentEvent)", replay)
                assert await page.evaluate("!State.agentRuns.some(r => r._permissionPending)")
                if not await page.locator('#notif-dropdown').is_visible():
                    await page.locator('#notif-bell').click()
                await page.locator("#notif-clear-notifications").click()
                if await page.locator('#notif-dropdown').is_visible():
                    await page.locator('#notif-close').click()
                if not await page.locator('#agent-manager-panel').is_visible():
                    await page.locator("#btn-agent-manager").click()
                await page.locator('#agent-manager-panel').wait_for(state='visible')
                assert await live_card.get_attribute("open") is not None
                await live_card.locator("[data-agent-cancel]").click()
                for _ in range(100):
                    stopped_child = await db.get_agent_run(child_run["run_id"])
                    if stopped_child["status"] == "cancelled":
                        break
                    await asyncio.sleep(0.02)
                assert stopped_child["status"] == "cancelled"
                await page.wait_for_function(
                    "document.querySelector('#agent-manager-count')?.classList.contains('hidden')",
                    timeout=10_000,
                )
                await page.locator("#agent-manager-close").click()

                await install_turn_probe(page)
                await page.locator("#user-input").fill("keep this saved prompt")
                await arm_turn_probe(page)
                await page.evaluate("sendMessage()")
                await _eventually(capture_started.is_set, timeout=10)
                await page.wait_for_timeout(500)
                preparation_ui = await page.evaluate("""() => ({
                    isStreaming: State.isStreaming,
                    followingGenId: State._followingGenId,
                    pendingLaunch: _pendingLaunch,
                    shell: !!document.querySelector('.message.assistant.streaming'),
                    lifecycle: document.querySelector('.stream-waiting-text')?.textContent || '',
                    lifecycleFont: document.querySelector('.stream-waiting')
                        ? getComputedStyle(document.querySelector('.stream-waiting')).fontFamily
                        : '',
                    clotho: document.querySelector('.clotho-spinner')?.textContent || '',
                    clothoInFooter: !!document.querySelector(
                        '.stream-thinking-footer > .clotho-spinner'
                    ),
                    clothoInContent: !!document.querySelector(
                        '.message-content .clotho-spinner'
                    ),
                    loomingHidden: document.querySelector('.looming-phase')?.classList.contains('hidden'),
                    globalStatusExists: !!document.querySelector('#generation-status'),
                    errors: [...document.querySelectorAll('.retry-error')].map(el => el.textContent),
                })""")
                assert preparation_ui["shell"], preparation_ui
                assert "recovery snapshot" in preparation_ui["lifecycle"], preparation_ui
                assert "Space Grotesk" in preparation_ui["lifecycleFont"], preparation_ui
                assert preparation_ui["clotho"].strip(), preparation_ui
                assert preparation_ui["clothoInFooter"] is True, preparation_ui
                assert preparation_ui["clothoInContent"] is False, preparation_ui
                assert preparation_ui["loomingHidden"] is True, preparation_ui
                assert preparation_ui["globalStatusExists"] is False, preparation_ui
                assert await page.locator(".cancel-draft-btn").inner_text() == "⊘"
                assert await page.locator(".cancel-draft-btn").get_attribute(
                    "aria-label"
                ) == "Cancel launch; keep the user message"
                assert await page.locator(".interrupt-draft-btn").count() == 0
                assert (await page.title()).endswith("EXO: Visible prelaunch")
                assert (await page.locator("#loom-favicon").get_attribute("href")).startswith(
                    "data:image/svg+xml"
                )

                await page.locator(".cancel-draft-btn").click()
                await page.wait_for_function("State.isStreaming === false", timeout=10_000)
                await _eventually(
                    lambda: not any(
                        not task.done() for task in server._active_generations.values()
                    ),
                    timeout=10,
                )
                assert not provider_entered.is_set()
                timing = await turn_metrics(page)
                assert "send_to_shell_ms" in timing
                assert "send_to_first_output_ms" not in timing
                observe(record_property, "prelaunch_browser", **timing, launches=0)
                rows = (await client.get(
                    f"/api/conversations/{conv['id']}/tree"
                )).json()
                user_rows = [row for row in rows if row["role"] == "user"]
                assert len(user_rows) == 1
                saved_prompt = await db.get_message(user_rows[0]["id"])
                assert saved_prompt["content"] == "keep this saved prompt"
                assert not [row for row in rows if row["role"] == "assistant"]
                assert page_errors == []

                # Phone-only presentation stays separate from the desktop UI:
                # cyan text pause glyph, original Loom animation without its
                # label, two-row Clotho status, and a keyboard-safe app height.
                assert not await page.locator("html").evaluate(
                    "el => el.classList.contains('true-mobile')"
                )
                mobile_context = await browser.new_context(
                    viewport={"width": 390, "height": 844},
                    has_touch=True,
                    is_mobile=True,
                )
                mobile_page = await mobile_context.new_page()
                await mobile_page.goto(base_url, wait_until="domcontentloaded")
                await mobile_page.wait_for_function(
                    "document.documentElement.classList.contains('true-mobile')"
                )
                mobile_ui = await mobile_page.evaluate("""() => {
                    const host = document.createElement('div');
                    host.className = 'message assistant';
                    host.innerHTML = `
                        <div class="message-header">
                            <div class="message-header-left">
                                <span class="message-role">Goose</span>
                                <details class="model-attestation model-attestation-configured">
                                    <summary>Goose ACP · llama:Qwen3.8-27B-NVFP4-MTP-Q8 attested</summary>
                                </details>
                            </div>
                            <div class="message-actions"><button>↻</button><button>⧉</button><button>⬡</button></div>
                        </div>
                        <div class="stream-thinking-footer">
                            <span class="stream-waiting">
                                <span class="stream-waiting-text">MCP startup: configuring several services</span>
                            </span>
                            <button class="cancel-draft-btn">⊘</button>
                            <button class="interrupt-draft-btn">⏸</button>
                            <span class="stream-waiting-anim">=*=</span>
                            <span class="looming-phase">
                                <span class="loom-anim">loom frame</span>
                                <span class="looming-text">Looming</span>
                                <span class="looming-ellipsis">...</span>
                            </span>
                        </div>`;
                    document.body.appendChild(host);
                    const loadingHost = document.createElement('div');
                    loadingHost.className = 'message assistant streaming';
                    loadingHost.innerHTML = `
                        <div class="message-header">
                            <div class="message-header-left">
                                <span class="message-role">Goose</span>
                                <span class="model-attestation-slot">
                                    <details class="model-attestation model-attestation-requested">
                                        <summary>${'Extremely-long-loading-model-identity-'.repeat(12)}</summary>
                                    </details>
                                </span>
                            </div>
                        </div>`;
                    document.body.appendChild(loadingHost);
                    const footer = host.querySelector('.stream-thinking-footer');
                    const pause = footer.querySelector('.interrupt-draft-btn');
                    const looming = footer.querySelector('.looming-phase');
                    const pill = host.querySelector('.model-attestation > summary').getBoundingClientRect();
                    const actions = host.querySelector('.message-actions').getBoundingClientRect();
                    const cancel = footer.querySelector('.cancel-draft-btn').getBoundingClientRect();
                    const status = footer.querySelector('.stream-waiting').getBoundingClientRect();
                    const clotho = footer.querySelector('.stream-waiting-anim').getBoundingClientRect();
                    const loadingBounds = loadingHost.getBoundingClientRect();
                    const loadingPill = loadingHost.querySelector(
                        '.model-attestation > summary'
                    ).getBoundingClientRect();
                    return {
                        pauseText: getComputedStyle(pause, '::after').content,
                        pauseFontSize: getComputedStyle(pause).fontSize,
                        loomingDisplay: getComputedStyle(looming).display,
                        loomAnimationDisplay: getComputedStyle(looming.querySelector('.loom-anim')).display,
                        loomingTextDisplay: getComputedStyle(looming.querySelector('.looming-text')).display,
                        headerDoesNotOverlap: pill.right <= actions.left
                            || actions.right <= pill.left
                            || pill.bottom <= actions.top
                            || actions.bottom <= pill.top,
                        loadingPillStaysInside: loadingPill.left >= loadingBounds.left
                            && loadingPill.right <= loadingBounds.right,
                        controlsStayOnRow: Math.abs(
                            (cancel.top + cancel.height / 2) - (clotho.top + clotho.height / 2)
                        ) < 2,
                        statusAboveControls: status.bottom <= cancel.top,
                        appHeight: document.querySelector('#app').getBoundingClientRect().height,
                        visualHeight: window.visualViewport.height,
                    };
                }""")
                assert mobile_ui["pauseText"] == '"Ⅱ"'
                assert mobile_ui["pauseFontSize"] == "0px"
                # Flex items are blockified by the browser; either computed
                # value proves the original animation container remains shown.
                assert mobile_ui["loomingDisplay"] in {"flex", "inline-flex"}
                assert mobile_ui["loomAnimationDisplay"] in {"block", "inline-block"}
                assert mobile_ui["loomingTextDisplay"] == "none"
                assert mobile_ui["headerDoesNotOverlap"] is True
                assert mobile_ui["loadingPillStaysInside"] is True
                assert mobile_ui["controlsStayOnRow"] is True
                assert mobile_ui["statusAboveControls"] is True
                assert abs(mobile_ui["appHeight"] - mobile_ui["visualHeight"]) < 1
                await mobile_context.close()

                await context.close()
                await browser.close()
    finally:
        child_hold.set()
        await child_supervisor.stop()
        server._agent_supervisor = original_agent_supervisor
        capture_release.set()
        for task in list(server._active_generations.values()):
            task.cancel()
        if server._active_generations:
            await asyncio.gather(
                *list(server._active_generations.values()),
                return_exceptions=True,
            )
        server._active_generations.clear()
        server._generation_snapshots.clear()
        uv_server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(server_task, timeout=10)


@pytest.mark.skipif(
    os.getenv("LOOM_RUN_LIVE_CODEX_VERIFY") != "1",
    reason="set LOOM_RUN_LIVE_CODEX_VERIFY=1 for the isolated real-Codex smoke",
)
@pytest.mark.parametrize(
    "stop_action,continuity", [("interrupt", "forked"), ("cancel", "rebuilt")]
)
async def test_live_codex_queue_transitions_only_after_process_exit(
    tmp_database, tmp_path, monkeypatch, stop_action, continuity, record_property
):
    await _native_queue_lifecycle(tmp_path, monkeypatch, stop_action, continuity, record_property, "codex")


@pytest.mark.skipif(os.getenv("LOOM_RUN_LIVE_GOOSE_VERIFY") != "1",
                    reason="Opt-in: real Goose/OpenRouter lifecycle usage")
@pytest.mark.parametrize("stop_action,continuity", [("interrupt", "forked"), ("cancel", "rebuilt")])
async def test_live_goose_queue_transitions_only_after_process_exit(
    tmp_database, tmp_path, monkeypatch, stop_action, continuity, record_property
):
    import openrouter_client
    if not openrouter_client.api_key():
        pytest.skip("No existing OpenRouter credentials")
    await _native_queue_lifecycle(tmp_path, monkeypatch, stop_action, continuity, record_property, "goose")


async def _native_queue_lifecycle(tmp_path, monkeypatch, stop_action, continuity, record_property, harness):
    """Same real browser, scheduler and OS-process assertions for both harnesses."""
    import server
    import psutil

    pass
    monkeypatch.setattr(server, "default_recovery_root", lambda: tmp_path / "recovery")

    server._active_generations.clear()
    server._generation_snapshots.clear()
    server._active_websockets.clear()
    server._active_claude_procs.clear()
    monkeypatch.setattr(server, "_active_goose_procs", {})
    monkeypatch.setattr(server.config, "enable_goose", True)
    monkeypatch.setattr(server, "_event_router", SubscriptionRouter())
    if harness == "goose":
        monkeypatch.setenv("GOOSE_PATH_ROOT", str(tmp_path / "goose-state"))
        monkeypatch.setenv("LOOM_GOOSE_DISABLE_KEYRING", "1")
        # Keep the native built-in tools; omit unrelated MCP launch costs.
        # Durable-child ownership is covered separately, not inferred here.
        monkeypatch.setattr(server.goose_client, "_loom_mcp_servers", lambda *a, **k: [])

    port = _free_port()
    monkeypatch.setattr(server.config, "port", port)
    uv_server = uvicorn.Server(
        uvicorn.Config(
            server.app,
            ws="websockets-sansio",
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="off",
        )
    )
    server_task = asyncio.create_task(uv_server.serve())
    scheduler_task = asyncio.create_task(server._queued_generation_scheduler_loop())
    base_url = f"http://127.0.0.1:{port}"
    monitor_stop = asyncio.Event()
    monitor_task = None
    monitor_samples: list[list[dict]] = []
    provider_processes: list[tuple[int, float]] = []
    provider_descendants: dict[tuple[int, float], set[tuple[int, float]]] = {}
    os_samples: list[list[int]] = []
    native_events: list[list[dict]] = []
    native_routes = []
    original_ws_send = server._ws_send
    async def traced_ws_send(conv_id, data):
        if data.get('type') in {'generation_preparing', 'stream_start', 'stream_end'}:
            native_routes.append({'type': data['type'],
                'task_key': getattr(asyncio.current_task(), '_gen_key', None),
                'sockets': [{'client': getattr(ws, '_client_instance_id', None),
                             'state': str(ws.client_state),
                             'subscriptions': sorted(server._event_router.subscriptions(ws))}
                            for ws in server._active_websockets.get(conv_id, set())]})
        return await original_ws_send(conv_id, data)
    monkeypatch.setattr(server, '_ws_send', traced_ws_send)
    adapter = server.codex_client if harness == "codex" else server.goose_client
    launch_name = "run_" + harness
    original_launch = getattr(adapter, launch_name)
    launch_resumes = []

    async def tracked_launch(*args, **kwargs):
        launch_resumes.append(kwargs.get("resume_session_id"))
        proc, events = await original_launch(*args, **kwargs)
        provider_processes.append((proc.pid, psutil.Process(proc.pid).create_time()))
        trace = []
        native_events.append(trace)
        async def traced_events():
            try:
                async for event in events:
                    # Status/error metadata only; no tool arguments or prompts.
                    trace.append({"type": event.get("type"), **(
                        {"status": str(event.get("message") or event.get("text") or "")[:500]}
                        if event.get("type") in {"status", "error"} else {}
                    )})
                    yield event
            finally:
                await events.aclose()
        return proc, traced_events()

    monkeypatch.setattr(adapter, launch_name, tracked_launch)

    def living_test_providers():
        # Independent of Loom's run registry: remember launcher and observed
        # descendants even after their parent exits or a durable row disappears.
        alive = []
        for pid, created in list(provider_processes):
            identities = provider_descendants.setdefault((pid, created), {(pid, created)})
            group_alive = False
            for child_pid, child_created in list(identities):
                try:
                    proc = psutil.Process(child_pid)
                    if proc.create_time() != child_created or not proc.is_running():
                        continue
                    group_alive = True
                    for child in proc.children(recursive=True):
                        identities.add((child.pid, child.create_time()))
                except psutil.NoSuchProcess:
                    pass
            if group_alive:
                alive.append(pid)
        return alive

    async def monitor_generations(client: httpx.AsyncClient):
        while not monitor_stop.is_set():
            os_samples.append(await asyncio.to_thread(living_test_providers))
            try:
                rows = (await client.get("/api/generations")).json()
                monitor_samples.append(rows)
            except Exception:
                pass
            await asyncio.sleep(0.05)

    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
            await _eventually(lambda: uv_server.started, timeout=10)
            disposable_workspace = tmp_path / "workspace"
            disposable_workspace.mkdir()
            response = await client.post(
                "/api/conversations",
                json={
                    "title": "Native lifecycle fixture",
                    "mode": "codex" if harness == "codex" else "goose",
                    "cc_model": "gpt-5.6-luna" if harness == "codex" else
                        os.getenv("LOOM_LIFECYCLE_GOOSE_MODEL", "goose:openrouter:z-ai/glm-5.3-flash"),
                    "cc_effort": "low",
                    "project_dir": str(disposable_workspace),
                    "parallel_agents_enabled": False,
                },
            )
            response.raise_for_status()
            conv = response.json()
            monitor_task = asyncio.create_task(monitor_generations(client))

            async with playwright.async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                context = await browser.new_context()
                page = await context.new_page()
                page_errors: list[str] = []
                page.on("pageerror", lambda exc: page_errors.append(str(exc)))
                # Use the actual entry route, not a manual navigation racing
                # init()'s async restoration of a different conversation.
                async with page.expect_request("**/api/health"):
                    await page.goto(f"{base_url}/?conversation={conv['id']}&thread=new&view=chat",
                                    wait_until="domcontentloaded")
                await page.locator("#user-input").wait_for(state="visible")
                await page.wait_for_function(
                    "State.ws?.readyState === WebSocket.OPEN && !State._restoringView "
                    "&& document.querySelector('#loading-overlay').classList.contains('hidden')"
                )

                await install_turn_probe(page)
                await page.evaluate("""() => {
                    window.nativeUITrace = [];
                    const trace = label => {
                        nativeUITrace.push({label, streaming:State.isStreaming,
                            following:State._followingGenId, cursor:{...State.loomCursor},
                            messages:State.messages.map(m=>m.id)});
                        if (nativeUITrace.length > 80) nativeUITrace.shift();
                    };
                    const handle = handleWSMessage;
                    window.handleWSMessage = function(data) {
                        const result = handle(data);
                        if (['stream_start','stream_end','generation_idle','generation_active',
                             'cancelled','interrupted','queue_started'].includes(data.type))
                            trace(data.type + ':' + data.gen_id);
                        return result;
                    };
                    const reload = loadMessages;
                    window.loadMessages = async function(...args) {
                        trace('load start');
                        await reload(...args);
                        trace('load end');
                    };
                }""")
                first_prompt = (
                    "This is an isolated lifecycle verification. Do not edit files. "
                    "First print PARTIAL_OK as a progress message, then "
                    "Use the shell to run C:\\Python314\\python.exe -c "
                    "\"import time; time.sleep(30)\", then reply exactly FIRST_OK."
                )
                await page.locator("#user-input").fill(first_prompt)
                await arm_turn_probe(page)
                await page.evaluate("sendMessage()")
                await page.wait_for_function("State.isStreaming === true", timeout=30_000)
                first_rows = await _eventually(
                    lambda: next(
                        (
                            sample for sample in reversed(monitor_samples)
                            if any(row.get("pid_alive") for row in sample)
                        ),
                        None,
                    ),
                    timeout=30,
                )
                first_pid = next(row["pid"] for row in first_rows if row.get("pid_alive"))
                await page.wait_for_function(
                    "document.querySelector('.model-attestation-verified" +
                    (", .model-attestation-configured" if harness == "goose" else "") + "') !== null",
                    timeout=30_000,
                )
                # Stop only after real partial output, not merely a session ID.
                await _eventually(lambda: any(e['type'] in {'text_delta', 'thinking_delta', 'tool_start'}
                                             for e in native_events[0]), timeout=90)

                await page.locator("#user-input").fill(
                    "Reply exactly SECOND_OK. Do not use tools or edit files."
                )
                await page.evaluate("sendMessage()")
                await page.wait_for_function(
                    "document.querySelector('.message.queued-message') !== null",
                    timeout=15_000,
                )
                queued = (await client.get(
                    f"/api/conversations/{conv['id']}/generation-queue"
                )).json()
                assert len(queued) == 1

                # Background and reconnect without changing the parked cursor.
                parked = await page.evaluate("JSON.stringify(State.loomCursor)")
                reconnect_started = time.perf_counter()
                await page.evaluate("""() => {
                    Object.defineProperty(document, 'hidden', {configurable:true, get:()=>true});
                    document.dispatchEvent(new Event('visibilitychange'));
                    window.previousLifecycleSocket = State.ws; State.ws.close();
                }""")
                await page.wait_for_function("State.ws !== previousLifecycleSocket && State.ws?.readyState === WebSocket.OPEN && !State.ws._needsSync")
                await page.evaluate("""() => {
                    Object.defineProperty(document, 'hidden', {configurable:true, get:()=>false});
                    document.dispatchEvent(new Event('visibilitychange'));
                }""")
                assert await page.evaluate("JSON.stringify(State.loomCursor)") == parked
                reconnect_ms = (time.perf_counter() - reconnect_started) * 1000

                await page.evaluate(
                    "interruptGeneration()" if stop_action == "interrupt" else "cancelGeneration()"
                )
                await page.wait_for_function(
                    "_queuedGeneration && _queuedGeneration.queue_status === 'paused'",
                    timeout=30_000,
                )
                # Cancellation is a stop boundary. Cross multiple scheduler
                # ticks and prove no replacement PID appears until Resume.
                await asyncio.sleep(2.2)
                assert not any(
                    row.get("pid_alive") and row.get("pid") != first_pid
                    for sample in monitor_samples
                    for row in sample
                )
                paused = (await client.get(
                    f"/api/conversations/{conv['id']}/generation-queue"
                )).json()
                assert paused[0]["status"] == "paused"
                predecessor = await db.get_message(queued[0]["wait_for_draft_id"])
                assert predecessor["turn_lifecycle_state"] == (
                    "interrupted" if stop_action == "interrupt" else "cancelled"
                )
                assert bool(predecessor.get("cc_session_id")) == (stop_action == "interrupt")
                if stop_action == "interrupt":
                    assert predecessor.get("content_blocks"), "Real partial context must survive Interrupt"
                if stop_action == "cancel":
                    assert predecessor.get("content_blocks") is None
                    assert predecessor.get("cc_session_mode") is None
                try:
                    await arm_turn_probe(page)
                    await page.locator('.queue-resume-btn').click()
                except playwright.TimeoutError as exc:
                    diagnostic = await page.evaluate("""() => ({
                        streaming: State.isStreaming,
                        queueStatus: _queuedGeneration?.queue_status,
                        queueId: _queuedGeneration?.id,
                        rows: [...document.querySelectorAll('.queued-message')].map(el => ({
                            id: el.dataset.msgId,
                            label: el.querySelector('.queue-status-label')?.textContent,
                            resume: !!el.querySelector('.queue-resume-btn'),
                        })),
                    })""")
                    (tmp_path / "queue-ui-diagnostic.json").write_text(
                        json.dumps(diagnostic, indent=2), encoding="utf-8"
                    )
                    await page.screenshot(path=str(tmp_path / "queue-ui-failure.png"))
                    raise AssertionError(f"Paused queue did not expose Resume: {diagnostic}") from exc
                await _eventually(
                    lambda: any(
                        row.get("pid_alive") and row.get("pid") != first_pid
                        for sample in monitor_samples
                        for row in sample
                    ),
                    timeout=30,
                )
                await page.wait_for_function(
                    "document.querySelector('.message.queued-message') === null",
                    timeout=30_000,
                )
                try:
                    await page.wait_for_function(
                        "State.messages.some(m => m.role === 'assistant' "
                        "&& String(m.content || '').includes('SECOND_OK')) "
                        "&& State.isStreaming === false",
                        timeout=90_000,
                    )
                except playwright.TimeoutError as exc:
                    # Synthetic fixture only; keep diagnostics in pytest output,
                    # never in the count-only portable preflight summary.
                    ui = await page.evaluate("""() => ({
                        streaming: State.isStreaming,
                        queueStatus: _queuedGeneration?.queue_status,
                        messageIds: State.messages.map(m => m.id),
                        trace: window.nativeUITrace,
                    })""")
                    children = await db.get_children(int(queued[0]["user_msg_id"]))
                    detail = [{
                        "id": row["id"], "state": row.get("turn_lifecycle_state"),
                        "content": str(row.get("content") or "")[-800:],
                        "attestation": row.get("model_attestation"),
                    } for row in children]
                    raise AssertionError(
                        f"Native successor did not finish: ui={ui}; "
                        f"children={detail}; live_test_trees={living_test_providers()}; "
                        f"page_errors={page_errors}; events={native_events[-1][-12:]}"
                        f"; routes={native_routes}"
                    ) from exc

                queued_message_id = int(queued[0]["user_msg_id"])
                durable_children = await db.get_children(queued_message_id)
                second_response = next(
                    message for message in durable_children
                    if message.get("role") == "assistant"
                    and "SECOND_OK" in str(message.get("content") or "")
                )
                assert second_response.get("model_attestation")
                second_attestation = json.loads(second_response["model_attestation"])
                assert second_attestation["session_continuity"] == continuity
                assert launch_resumes == [None, predecessor.get("cc_session_id")]
                assert len(durable_children) == 1, "One queued send must create exactly one successor"
                queued_user = await db.get_message(queued_message_id)
                assert queued_user["parent_id"] == predecessor["id"]
                if stop_action == "interrupt":
                    assert second_attestation["session_continuity_reason"] == "resume"
                    assert second_attestation["source_session_id"] == predecessor["cc_session_id"]
                assert int(second_response["time_to_first_token_ms"]) > 0
                print(
                    f"real {harness} {stop_action} -> {continuity} TTFT:",
                    second_response["time_to_first_token_ms"],
                    "ms",
                )
                latest_badge = page.locator(
                    ".model-attestation-verified summary, .model-attestation-configured summary"
                ).last
                expected_label = "native fork" if stop_action == "interrupt" else "history rebuild"
                assert expected_label in await latest_badge.inner_text()

                # The process-backed lane itself never contains two live
                # app-server PIDs in any monitor sample.
                assert max(
                    sum(1 for row in sample if row.get("pid_alive"))
                    for sample in monitor_samples
                ) == 1
                assert len(provider_processes) == 2
                assert max(map(len, os_samples)) == 1
                await _eventually(lambda: not living_test_providers(), timeout=15)
                timing = await turn_metrics(page)
                assert "send_to_first_output_ms" in timing
                scenario = ("native_" if harness == "codex" else "native_goose_") + stop_action
                observe(record_property, scenario, **timing,
                        native_ttft_ms=second_response["time_to_first_token_ms"],
                        reconnect_ms=reconnect_ms, os_samples=len(os_samples),
                        max_live_provider_trees=max(map(len, os_samples)), launches=len(provider_processes),
                        remaining_provider_trees=len(living_test_providers()))
                print(f"OS process-tree samples: {len(os_samples)}; maximum live test providers: 1")
                assert page_errors == []
                assert list(disposable_workspace.iterdir()) == []

                await context.close()
                await browser.close()

            monitor_stop.set()
            await monitor_task
    finally:
        monitor_stop.set()
        if monitor_task is not None:
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)
        for task in list(server._active_generations.values()):
            task.cancel()
        if server._active_generations:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(
                        *list(server._active_generations.values()),
                        return_exceptions=True,
                    ),
                    timeout=15,
                )
        server._active_generations.clear()
        server._generation_snapshots.clear()
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task
        uv_server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(server_task, timeout=10)
