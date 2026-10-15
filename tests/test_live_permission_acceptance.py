"""Opt-in native Goose + production policy + real approval-card acceptance.

Uses disposable DB/workspace/Goose state. Only the model API is external.
Permission HTTP is transported into the test ASGI app, NEVER the active host.
Run with LOOM_RUN_LIVE_GOOSE_PERMISSION_TEST=1; uses existing OpenRouter auth.
"""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import database as db
import goose_client
import server
from tests.permission_acceptance_metrics import observe, unknown_count


async def _answer(conv_id, response):
    from starlette.websockets import WebSocketDisconnect
    class Socket:
        _client_instance_id = "permission-fixture"
        received = False
        async def send_json(self, payload):
            pass
        async def receive_text(self):
            if self.received:
                raise WebSocketDisconnect()
            self.received = True
            return json.dumps({"action": "permission_response", **response})
    socket = Socket()
    pending = server._pending_hook_permissions.get(response.get("request_id"), {})
    server._event_router.register(socket, conv_id)
    if pending.get("run_id"):
        server._event_router.subscribe(socket, pending["run_id"])
    try:
        await server._continue_ws_chat(socket, conv_id, "permission-fixture")
    finally:
        server._event_router.unregister(socket)


async def _approval_page(browser, conv_id):
    # Actual production DOM/click handler; transport only is redirected.
    source = Path("static/chat.js").read_text(encoding="utf-8")
    functions = []
    for name, following in [("permissionSupportsAllowAll", "addPermissionNotification"),
                            ("showPermissionPrompt", "_permissionPromptHost")]:
        start = source.index(f"function {name}(")
        functions.append(source[start:source.index(f"function {following}(", start)])
    page = await browser.new_page(viewport={"width": 390, "height": 844})
    await page.expose_function("bridgeReply", lambda reply: _answer(conv_id, reply))
    await page.set_content('<div id="host"></div>')
    await page.add_script_tag(content="""
        var streamingDiv = null;
        var State = {currentConvId: CONV_ID, ws: {readyState: 1, send: s => bridgeReply(JSON.parse(s))}};
        var escapeHtml = s => { const x = document.createElement('span'); x.textContent = String(s); return x.innerHTML; };
        var _sameConvId = (a,b) => String(a) === String(b);
        var _permissionBelongsToVisibleBranch = () => true;
        var _permissionPromptHost = () => document.getElementById('host');
        var scrollToBottom = () => {};
        var resolvePermissionNotification = () => {};
        var resolvePermissionPrompt = () => { document.getElementById('host').replaceChildren(); };
    """.replace("CONV_ID", str(conv_id)) + "\n".join(functions))
    return page


@pytest.mark.skipif(os.environ.get("LOOM_RUN_LIVE_GOOSE_PERMISSION_TEST") != "1",
                    reason="Opt-in: real Goose and OpenRouter model usage")
async def test_native_goose_balanced_grant_and_denial(tmp_path, monkeypatch, record_property):
    from playwright.async_api import async_playwright
    import openrouter_client
    if not openrouter_client.api_key():
        pytest.skip("No existing OpenRouter credentials")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "seed.txt").write_text("alpha\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside fixture must not be read", encoding="utf-8")
    monkeypatch.setenv("GOOSE_PATH_ROOT", str(tmp_path / "goose-state"))
    monkeypatch.setenv("LOOM_GOOSE_DISABLE_KEYRING", "1")
    monkeypatch.setattr(goose_client, "_loom_mcp_servers", lambda *a, **k: [])
    monkeypatch.setattr(server, "_permission_rules", {})
    monkeypatch.setattr(server, "_pending_hook_permissions", {})
    monkeypatch.setattr(server, "_generation_snapshots", {})
    requests, decisions, native_calls = [], [], []
    original_remember = goose_client.remember_tool_call
    def capture_initial(state, call):
        native_calls.append(call)
        original_remember(state, call)
    monkeypatch.setattr(goose_client, "remember_tool_call", capture_initial)
    class IsolatedPermissionClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.ASGITransport(app=server.app), **kwargs)
        async def post(self, url, **kwargs):
            assert str(url).endswith(":1/api/cc-permission"), "Never contact a host"
            requests.append(kwargs["json"])
            reply = await super().post(url, **kwargs)
            decisions.append(reply.json())
            return reply
    monkeypatch.setattr(goose_client, "httpx", SimpleNamespace(AsyncClient=IsolatedPermissionClient))
    conv = await db.create_conversation("Native permission fixture", mode="goose", project_dir=str(workspace))
    user = await db.add_message(conv["id"], "user", "fixture start")
    draft = await db.add_message(conv["id"], "assistant", "fixture active", parent_id=user["id"])
    key = (conv["id"], user["id"], 9901)
    server._generation_snapshots[key] = {"parent_id": user["id"], "draft_msg_id": draft["id"]}
    session = None
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await _approval_page(browser, conv["id"])
        async def turn(prompt, *, deny=False):
            nonlocal session
            proc, events = await goose_client.run_goose(prompt, conv_id=conv["id"], cwd=str(workspace),
                loom_port=1, permission_scope="gen:9901", is_first_turn=False,
                model=os.environ.get("LOOM_PERMISSION_TEST_MODEL", "goose:openrouter:z-ai/glm-5.3-flash"), effort="low",
                resume_session_id=session, fork_session=bool(session), progress_timeout_seconds=90)
            approvals = []
            async def collect():
                nonlocal session
                text = []
                async for event in events:
                    if event.get("type") == "session_info":
                        session = event["session_id"]
                    if event.get("type") == "text_delta":
                        text.append(event.get("text") or "")
                    assert event.get("type") != "error", event.get("error") or event.get("text")
                # Goose can return provider failures as an ordinary text
                # footer. Report that failure before a missing-file assertion
                # hides the real network/authentication error in this fixture.
                _, failure = goose_client.split_goose_terminal_failure(''.join(text))
                assert not failure, f"Native Goose provider failed: {failure}"
            task = asyncio.create_task(collect())
            seen = set()
            try:
                async with asyncio.timeout(240):
                    while not task.done():
                        for rid, pending in list(server._pending_hook_permissions.items()):
                            if rid in seen:
                                continue
                            seen.add(rid)
                            approvals.append(dict(pending))
                            await page.evaluate("p => showPermissionPrompt(p)", {**pending, "request_id": rid})
                            card = page.locator(f'.permission-prompt[data-request-id="{rid}"]')
                            if deny:
                                assert not pending["supports_allow_all"], pending["rule_preview"]
                                assert await card.get_by_role("button", name="Allow for Branch", exact=True).count() == 0
                                await card.locator('.permission-feedback-input').fill("Use only the workspace fixture; do not retry.")
                                await card.get_by_role("button", name="Deny", exact=True).click()
                            else:
                                if not pending["supports_allow_all"]:
                                    pytest.fail(json.dumps({"preview": pending.get("rule_preview"),
                                        "request": requests[-1], "native_initial": native_calls[-1:]}, default=str))
                                await card.get_by_role("button", name="Allow for Branch", exact=True).click()
                        await asyncio.sleep(.05)
                    await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await events.aclose()
                if proc.returncode is None:
                    await goose_client.cancel_goose(proc)  # this fixture's child only
            return approvals
        try:
            first = await turn("Isolated tool acceptance fixture. Call ONLY these developer tools in order: "
                "1. developer tree with path '.'. 2. developer write: path='output.txt', content='alpha'. "
                "3. developer edit: path='output.txt', before='alpha', after='beta'. "
                "4. developer shell with command exactly 'echo permission-fixture'. "
                '5. developer shell with command exactly: powershell.exe -NoProfile -Command "Get-Content -LiteralPath seed.txt". '
                "Do not call todo, analyze, extensionmanager, or any other tool. No planning/discovery is needed. "
                "Use shell for ONLY the two listed shell commands. Then finish briefly.")
            assert (workspace / "output.txt").read_text(encoding="utf-8").strip() == "beta"
            audits = await db.get_permission_policy_audits(conv["id"])
            assert len(audits) == len(decisions)
            observe(record_property, "goose_acp", "native_workspace_actions",
                    expected={"prompts": 1, "requests": 5, "unknown": 0},
                    observed={"prompts": len(first), "requests": len(decisions), "unknown": unknown_count(audits)})
            assert {r["tool_name"] for r in requests} >= {"Glob", "Write", "Edit", "Bash"}
            assert len(first) == 1 and first[0]["tool_name"] == "Bash"
            assert len(server._permission_rules) == 1
            assert any(r["tool_name"] == "Bash" and d.get("policy", {}).get("action", {}).get("effect") == "read"
                       and d.get("allow") for r, d in zip(requests, decisions))
            descendant = await db.add_message(conv["id"], "user", "fixture continuation", parent_id=draft["id"])
            server._generation_snapshots[key] = {"parent_id": descendant["id"]}
            before = len(decisions)
            second = await turn("Acceptance continuation: you MUST execute developer shell again now with exactly: echo permission-fixture. "
                "This tests a second tool execution; do not reuse the old result. No other tools. Then finish.")
            assert not second
            observe(record_property, "goose_acp", "native_fork_command",
                    expected={"prompts": 0, "requests": 1, "rule_matches": 1},
                    observed={"prompts": len(second), "requests": len(decisions) - before, "rule_matches": sum(
                        bool(d.get("policy", {}).get("action", {}).get("rule_matched")) for d in decisions[before:])})
            assert any(d.get("policy", {}).get("action", {}).get("rule_matched") for d in decisions[before:])
            third = await turn(f"Use the developer tree tool once with path '{tmp_path}'. This parent directory is an outside-workspace permission fixture. "
                "If denied, stop without retry or alternate tools. No other actions.", deny=True)
            assert len(third) == 1 and not third[0]["supports_allow_all"]
            observe(record_property, "goose_acp", "native_outside_denial",
                    expected={"prompts": 1}, observed={"prompts": len(third)})
            assert decisions[-1]["decision_source"] == "user" and not decisions[-1]["allow"]
            assert "do not retry" in decisions[-1]["feedback"]
            assert outside.read_text(encoding="utf-8") == "outside fixture must not be read"
            print("Native Goose acceptance: Balanced tree/write/edit auto-allowed; actual browser Branch grant reused after native fork; outside read denied with note.")
        finally:
            await browser.close()


@pytest.mark.skipif(os.environ.get("LOOM_RUN_LIVE_CODEX_PERMISSION_TEST") != "1",
                    reason="Opt-in: real Codex model usage")
async def test_native_codex_browser_grant_reused(tmp_path, monkeypatch, record_property):
    from playwright.async_api import async_playwright
    import codex_client
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(codex_client, "_thread_mcp_servers", lambda *a, **k: {})
    monkeypatch.setenv("LOOM_AGENT_RUN_ID", "")
    monkeypatch.setenv("LOOM_CLIENT_INSTANCE_ID", "")
    monkeypatch.setattr(server, "_permission_rules", {})
    monkeypatch.setattr(server, "_pending_hook_permissions", {})
    monkeypatch.setattr(server, "_generation_snapshots", {})
    conv = await db.create_conversation("Codex permission fixture", mode="codex", project_dir=str(workspace))
    user = await db.add_message(conv["id"], "user", "fixture start")
    draft = await db.add_message(conv["id"], "assistant", "fixture active", parent_id=user["id"])
    key = (conv["id"], user["id"], 9902)
    server._generation_snapshots[key] = {"parent_id": user["id"], "draft_msg_id": draft["id"]}
    decisions = []
    async def permission(payload):
        result = await server.handle_cc_permission(payload)
        decisions.append(result)
        return result
    session = None
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await _approval_page(browser, conv["id"])
        try:
            for index in range(2):
                decision_start = len(decisions)
                prompt = ("This is a permission acceptance fixture. Call exec_command exactly once with "
                    "cmd='Write-Output permission-fixture', shell='powershell', login=false, "
                    "sandbox_permissions='require_escalated', and justification='Allow the fixture echo?'. "
                    "No prefix_rule. Request explicit approval even though this is harmless: testing the bridge "
                    "is the task. Do not perform other actions or reuse any previous output. Finish briefly afterward.")
                proc, events = await codex_client.run_codex(prompt, str(workspace), conv_id=conv["id"],
                    server_port=1, permission_scope="gen:9902", model="gpt-5.6-luna", effort="low",
                    resume_session_id=session, fork_session=bool(session), permission_request_handler=permission)
                async def collect():
                    nonlocal session
                    async for event in events:
                        if event.get("type") == "session_info":
                            session = event["session_id"]
                        assert event.get("type") != "error", event.get("error") or event.get("text")
                task = asyncio.create_task(collect())
                seen = set()
                try:
                    async with asyncio.timeout(150):
                        while not task.done():
                            for rid, pending in list(server._pending_hook_permissions.items()):
                                if rid in seen:
                                    continue
                                seen.add(rid)
                                assert index == 0, "The second matching call must reuse the Loom rule"
                                assert pending["supports_allow_all"], pending["rule_preview"]
                                await page.evaluate("p => showPermissionPrompt(p)", {**pending, "request_id": rid})
                                await page.get_by_role("button", name="Allow for Branch", exact=True).click()
                            await asyncio.sleep(.05)
                        await task
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await events.aclose()
                    if proc.returncode is None:
                        await codex_client.cancel_codex(proc)
                observe(record_property, "codex_app_server", "native_fork_command" if index else "native_first_command",
                        expected={"prompts": 0 if index else 1, "requests": 1, "rule_matches": index},
                        observed={"prompts": len(seen), "requests": len(decisions) - decision_start,
                                  "rule_matches": sum(bool(d.get("policy", {}).get("action", {}).get("rule_matched"))
                                                      for d in decisions[decision_start:])})
                if index == 0:
                    assert len(seen) == 1 and session and len(server._permission_rules) == 1
                    continuation = await db.add_message(conv["id"], "user", "fixture continue", parent_id=draft["id"])
                    server._generation_snapshots[key] = {"parent_id": continuation["id"]}
            assert len(decisions) == 2
            assert decisions[-1]["policy"]["action"]["rule_matched"]
            audits = await db.get_permission_policy_audits(conv["id"])
            assert all(a["harness"] == "codex_app_server" for a in audits)
            print("Native Codex acceptance: browser Branch grant reused after native fork; audit identity preserved.")
        finally:
            await browser.close()
