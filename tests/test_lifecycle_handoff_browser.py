"""Real SPA/router/scheduler with controlled providers; never a live host."""
import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import uvicorn

import database as db
import server
from agent_supervisor import AgentSupervisor
from event_router import SubscriptionRouter
from tests.lifecycle_metrics import observe
from tests.test_fullstack_queue_isolation import _ControlledProvider, _eventually, _free_port

playwright = pytest.importorskip("playwright.async_api")


@pytest.fixture
async def lifecycle_browser(tmp_database, tmp_path, monkeypatch, request, isolate_model_discovery):
    for name in ("_active_generations", "_generation_snapshots", "_active_websockets", "_pending_hook_permissions", "_permission_rules"):
        monkeypatch.setattr(server, name, {})
    monkeypatch.setattr(server, "_event_router", SubscriptionRouter())
    monkeypatch.setattr(server, "default_recovery_root", lambda: tmp_path / "recovery")
    import context_manager
    monkeypatch.setattr(context_manager, "update_rolling_summary", AsyncMock())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    conv = await db.create_conversation("Lifecycle acceptance", mode="codex", project_dir=str(workspace))
    await db.update_conversation_fields(conv["id"], cc_model="codex-gpt-5.6-sol", parallel_agents_enabled=False)
    options = getattr(request, "param", {})
    if options.get("native"):
        harness = options["harness"]
        model = "gpt-5.6-luna" if harness == "codex" else "goose:openrouter:z-ai/glm-5.3-flash"
        await db.update_conversation_fields(conv["id"], mode=harness, cc_model=model, cc_effort="low")
    else:
        isolate_model_discovery()
    monkeypatch.setattr(server.config, "enable_goose", True)
    port = _free_port()
    monkeypatch.setattr(server.config, "port", port)
    # Native Goose chooses its callback scheme from these same certificate
    # files. Read only; this listener belongs to the fixture, never the host.
    certs = Path(server.__file__).parent / "certs"
    tls = bool(options.get("native") and all((certs / p).is_file() for p in ("cert.pem", "key.pem")))
    ssl_args = {"ssl_certfile": str(certs / "cert.pem"), "ssl_keyfile": str(certs / "key.pem")} if tls else {}
    base_url = f"{'https' if tls else 'http'}://127.0.0.1:{port}"
    uv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, lifespan="off", log_level="warning",
                                      ws="websockets-sansio",
                                      timeout_graceful_shutdown=5, **ssl_args))
    serving = asyncio.create_task(uv.serve())
    scheduler = asyncio.create_task(server._queued_generation_scheduler_loop())
    await _eventually(lambda: uv.started)
    try:
        async with playwright.async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                mobile_options = dict(viewport={'width':390,'height':844}, is_mobile=True,
                    has_touch=True, device_scale_factor=2) if options.get('mobile') else {}
                page = await browser.new_page(ignore_https_errors=True, **mobile_options)
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                # Enter through startup's real route. Manually navigating while
                # init() is still resolving its picker fetch races restoration.
                async with page.expect_request("**/api/health"):
                    await page.goto(f"{base_url}/?conversation={conv['id']}&thread=new&view=chat", wait_until="domcontentloaded")
                await page.wait_for_function("State.ws?.readyState === WebSocket.OPEN && !State._restoringView && document.querySelector('#loading-overlay').classList.contains('hidden')")
                async with httpx.AsyncClient(base_url=base_url, timeout=10, verify=False) as client:
                    yield page, conv, client, workspace
                assert not errors, errors
            finally:
                await browser.close()
    finally:
        tasks = list(server._active_generations.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 15)
        scheduler.cancel()
        await asyncio.gather(scheduler, return_exceptions=True)
        # This handle belongs solely to this fixture's ephemeral listener.
        uv.should_exit = True
        await asyncio.wait_for(serving, 10)


async def test_browser_handoff_uses_canonical_branch_without_repositioning(lifecycle_browser, monkeypatch, record_property):
    page, conv, client, workspace = lifecycle_browser
    calls = []

    def launcher(harness):
        async def launch(prompt, *args, **kwargs):
            index = len(calls)
            session = f"fixture-{harness}-{index}"
            calls.append({"harness": harness, "prompt": prompt, "resume": kwargs.get("resume_session_id"), "session": session})
            class Proc:
                pid = 990000 + index
                returncode = 0
                async def wait(self):
                    return 0
            async def events():
                yield {"type": "session_info", "session_id": session, "model": kwargs.get("model"), "model_confirmed": True}
                yield {"type": "text_delta", "text": f"retained-{harness}-{index}"}
                yield {"type": "result", "session_id": session, "result_text": f"retained-{harness}-{index}", "is_error": False}
            return Proc(), events()
        return launch

    monkeypatch.setattr(server.codex_client, "run_codex", launcher("codex"))
    monkeypatch.setattr(server.goose_client, "run_goose", launcher("goose"))
    # Two Codex turns, a Goose handoff, then return to Codex. Native IDs are
    # invented only by the test provider; payload selection is production code.
    models = ["codex-gpt-5.6-sol", "codex-gpt-5.6-sol", "goose:openrouter:z-ai/glm-5.3-flash", "codex-gpt-5.6-sol"]
    previous_leaf = None
    thread = None
    for index, model in enumerate(models):
        cursor_before = await page.evaluate("JSON.stringify(State.loomCursor)")
        async with page.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith(f"/api/conversations/{conv['id']}")):
            await page.locator("#cc-model-inline").select_option(model)
        await page.wait_for_function("model => State.currentConv.cc_model === model", arg=model)
        assert await page.evaluate("JSON.stringify(State.loomCursor)") == cursor_before
        await page.locator("#user-input").fill(f"bounded fixture request {index}")
        await page.evaluate("sendMessage()")
        await _eventually(lambda: len(calls) == index + 1)
        try:
            await page.wait_for_function("text => !State.isStreaming && State.messages.some(m => m.content === text)",
                                         arg=f"retained-{calls[index]['harness']}-{index}")
        except playwright.TimeoutError as exc:
            ui = await page.evaluate("""() => ({streaming: State.isStreaming,
                messages: State.messages.map(m => ({id:m.id, parent:m.parent_id, content:m.content})),
                cursor: State.loomCursor, selected: State.currentConv.cc_model})""")
            tree = await db.get_conversation_tree(conv["id"])
            rows = [await db.get_message(r["id"]) for r in tree]
            durable = [{k: r.get(k) for k in ("id", "parent_id", "content", "turn_lifecycle_state")} for r in rows]
            raise AssertionError(f"Fixture handoff {index} failed: ui={ui}; durable={durable}") from exc
        await page.evaluate("async () => { if (State._cursorSyncPromise) await State._cursorSyncPromise; }")
        # Tree rows are lightweight previews; hydrate canonical message content.
        tree = await db.get_conversation_tree(conv["id"])
        rows = [await db.get_message(r["id"]) for r in tree]
        user = next(r for r in rows if r["content"] == f"bounded fixture request {index}")
        answer = next(r for r in rows if r["content"] == f"retained-{calls[index]['harness']}-{index}")
        assert user["parent_id"] == previous_leaf
        assert answer["parent_id"] == user["id"]
        previous_leaf = answer["id"]
        thread = thread or user["loom_thread_id"]
        assert answer["loom_thread_id"] == thread
        assert await page.evaluate("State.loomCursor.thread_id") == thread
        if index == 1:
            assert calls[index]["resume"] == calls[0]["session"]
            assert "retained-codex-0" not in calls[index]["prompt"]
        elif index >= 2:
            assert calls[index]["resume"] is None
            assert "loom_handoff" in calls[index]["prompt"]
            for prior in range(index):
                assert f"retained-{calls[prior]['harness']}-{prior}" in calls[index]["prompt"]
    assert len(await db.get_conversation_tree(conv["id"])) == 8
    observe(record_property, "handoff_browser", handoffs=2, launches=len(calls))


async def test_child_permission_survives_parent_stop_and_reconnect(lifecycle_browser, monkeypatch, record_property):
    page, conv, client, workspace = lifecycle_browser
    await db.update_conversation_fields(conv['id'], permission_profile='strict')
    controller = _ControlledProvider(teardown_delay=.2)
    monkeypatch.setattr(server, "_handle_generation", controller.handle)
    decisions = []
    async def child_launcher(run, emit):
        await emit("text_delta", {"text": "Child is independent"})
        decision = await server.handle_cc_permission({
            "loom_conv_id": conv["id"], "agent_run_id": run["run_id"],
            "tool_name": "FixtureReview", "tool_input": {"task": "bounded synthetic review"}, "source": "fixture-child",
        })
        decisions.append(decision)
        return {"text": "Child completed after parent stopped"}
    supervisor = AgentSupervisor(child_launcher, event_sink=server._publish_agent_event, batch_sink=server._publish_agent_events)
    monkeypatch.setattr(server, "_agent_supervisor", supervisor)
    await supervisor.start()
    try:
        for index, action in enumerate(("interruptGeneration()", "cancelGeneration()")):
            await page.locator("#user-input").fill(f"parent fixture {index}")
            await page.evaluate("sendMessage()")
            await controller.wait_for_runs(index + 1)
            parent = controller.runs[-1]
            child = (await client.post(f"/api/conversations/{conv['id']}/agent-runs", json={
                "task": "bounded child", "parent_run_id": f"gen:{parent.gen_id}",
                "project_dir": str(workspace), "client_instance_id": await page.evaluate("_loomClientInstanceId"),
            })).json()
            await _eventually(lambda: any(p.get("run_id") == f"agent:{child['run_id']}" for p in server._pending_hook_permissions.values()))
            await page.evaluate(action)
            await asyncio.wait_for(parent.exited.wait(), 10)
            await page.wait_for_function("!State.isStreaming")
            assert len(decisions) == index
            assert (await db.get_agent_run(child["run_id"]))["status"] in {"running", "waiting_permission"}
            pending = next(p for p in server._pending_hook_permissions.values() if p.get("run_id") == f"agent:{child['run_id']}")
            assert pending.get("response") is None
            cursor = await page.evaluate("JSON.stringify(State.loomCursor)")
            await page.evaluate("window.childStopSocket = State.ws; State.ws.close()")
            await page.wait_for_function("State.ws !== childStopSocket && State.ws?.readyState === WebSocket.OPEN && !State.ws._needsSync")
            assert await page.evaluate("JSON.stringify(State.loomCursor)") == cursor
            await page.locator("#btn-agent-manager").click()
            card = page.locator(f'.agent-run-card[data-run-id="{child["run_id"]}"]')
            await card.locator(":scope > summary").click()
            await card.locator("[data-agent-permission]").click()
            notification = page.locator(".notif-permission:not(.notif-resolved)")
            await notification.locator(".permission-feedback-input").fill("Only this synthetic child")
            await notification.locator('[data-action="allow"]').click()
            await _eventually(lambda: len(decisions) == index + 1)
            assert decisions[-1]["allow"] and decisions[-1]["feedback"] == "Only this synthetic child"
            async def child_finished():
                row = await db.get_agent_run(child["run_id"])
                return row if row["status"] == "completed" else None
            assert await _eventually(child_finished)
            events = await db.list_agent_events(child["run_id"])
            assert events[-1]["event_kind"] == "agent_completed"
            assert await page.evaluate("JSON.stringify(State.loomCursor)") == cursor
            assert not await page.locator('#notif-dropdown').is_visible()
            await page.evaluate("_closeAgentManager()")
        # Durable child events never append fake assistant turns to the tree.
        assert len(await db.get_conversation_tree(conv["id"])) == 4
        observe(record_property, "child_parent_stop", pending_children=2, controlled_max_active=controller.max_active_count)
    finally:
        await supervisor.stop()


async def test_remote_chat_opens_before_optional_local_discovery(lifecycle_browser, record_property):
    page, conv, client, workspace = lifecycle_browser
    context = await page.context.browser.new_context()
    pending, release = asyncio.Event(), asyncio.Event()
    async def hold_discovery(route):
        pending.set()
        await release.wait()
        await route.fulfill(json={"models":[{"name":"fixture-local.gguf", "backend":"llama", "loaded":True}]})
    await context.route("**/api/local/all-models", hold_discovery)
    fresh = await context.new_page()
    started = time.perf_counter()
    try:
        await fresh.goto(str(client.base_url) + f"?conversation={conv['id']}&thread=new&view=chat", wait_until="domcontentloaded")
        await asyncio.wait_for(pending.wait(), 10)
        # Discovery is deliberately never completed until AFTER chat is ready.
        # This is a dependency assertion, not a three-second latency SLO.
        await fresh.wait_for_function("State.currentConv && State.ws?.readyState === WebSocket.OPEN && !State._restoringView && document.querySelector('#loading-overlay').classList.contains('hidden')", timeout=3000)
        ready_ms = (time.perf_counter() - started) * 1000
        await fresh.locator("#user-input").fill("unsent draft must survive discovery")
        await fresh.locator("#cc-model-inline").select_option("goose:openrouter:z-ai/glm-5.3-flash")
        await fresh.wait_for_function("State.currentConv.cc_model === 'goose:openrouter:z-ai/glm-5.3-flash'")
        cursor = await fresh.evaluate("JSON.stringify(State.loomCursor)")
        release.set()
        await fresh.wait_for_function("[...document.querySelector('#cc-model-inline').options].some(o => o.value === 'fixture-local.gguf')")
        assert await fresh.locator("#cc-model-inline").input_value() == "goose:openrouter:z-ai/glm-5.3-flash"
        assert await fresh.locator("#user-input").input_value() == "unsent draft must survive discovery"
        assert await fresh.evaluate("JSON.stringify(State.loomCursor)") == cursor
        observe(record_property, "discovery_browser", screen_load_ms=ready_ms)
    finally:
        release.set()
        await context.close()


@pytest.mark.parametrize("view", ["chat", "tree"])
async def test_completed_turn_before_send_ack_is_not_revived(lifecycle_browser, monkeypatch, view):
    page, conv, client, workspace = lifecycle_browser
    controller = _ControlledProvider()
    monkeypatch.setattr(server, "_handle_generation", controller.handle)
    if view == "tree":
        await page.evaluate("switchView('tree')")
    async def hold_ack(route):
        response = await route.fetch()
        await controller.wait_for_runs(1)
        controller.runs[0].release.set()
        # WS completion must be applied BEFORE the HTTP acknowledgement.
        await page.wait_for_function("!State.isStreaming && State.messages.some(m => m.role === 'assistant' && m.content?.includes('complete'))")
        await route.fulfill(response=response)
    await page.route(f"**/api/conversations/{conv['id']}/messages", hold_ack)
    await page.locator("#user-input").fill("fast completion fixture")
    await page.evaluate("sendMessage()")
    run = controller.runs[0]
    expected = [run.parent_id, run.draft_id]
    assert await page.evaluate("State.messages.map(m => m.id)") == expected
    assert await page.locator("#messages .message").evaluate_all("els => els.map(e => Number(e.dataset.msgId))") == expected
    assert await page.evaluate("State.isStreaming") is False
    assert await page.evaluate("_pendingLaunch") is None
    assert await page.locator("#messages .streaming").count() == 0
    assert await page.evaluate("State.loomCursor.leaf_message_id") == run.draft_id
    assert await page.evaluate("State.currentView") == "chat"
    assert not await db.list_queued_generations(conv["id"])
