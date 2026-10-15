"""Paid native parents/children, real supervisor/HTTPS/browser approvals; isolated only."""
import asyncio
import json
import os
import time

import psutil
import pytest

import database as db
import server
from agent_supervisor import AgentSupervisor
from permission_commands import command_template, display_argv
from tests.lifecycle_metrics import observe
from tests.test_fullstack_queue_isolation import _eventually
from tests.test_lifecycle_handoff_browser import lifecycle_browser  # shared isolated fixture


pytestmark = pytest.mark.skipif(os.getenv("LOOM_RUN_NATIVE_CHILD_VERIFY") != "1",
                              reason="Opt-in real Codex/Goose durable-child usage")


def fixture_action(permission, harness, native_calls):
    """Never approve by arrival order, display title, or marker substring."""
    args = permission.get("tool_input") or {}
    if permission.get("tool_name") == "Bash":
        command = args.get("command")
        parsed = command_template(display_argv(command), command) if isinstance(command, list) else command_template(command or "")
        if parsed and parsed[0] == ["Write-Output", "CHILD_TOOL_OK"]:
            return "allow"
        if parsed and parsed[0] == ["Set-Content", "-LiteralPath", "denied.txt", "-Value", "forbidden"]:
            return "deny"
    if harness == "goose":
        call_id = (args.get("acp_identity") or {}).get("tool_call_id")
        call = native_calls.get(call_id, {})
        identity = (call.get("_meta", {}).get("goose", {}).get("toolCall")) or {}
        raw = call.get("rawInput")
        if isinstance(raw, str):
            raw = json.loads(raw)
        if (permission.get("tool_name") == "Unknown" and set(args) <= {"content", "cwd", "acp_identity"}
                and not (args.get("acp_identity") or {}).get("conflict")
                and identity.get("extensionName") == "todo" and identity.get("toolName") in {"todo_write", "todo__todo_write"}
                and isinstance(raw, dict) and set(raw) == {"content"}
                and isinstance(raw["content"], str) and len(raw["content"]) <= 4096
                and raw["content"] == args.get("content")):
            return "plan"
    raise AssertionError("Unexpected native fixture action; no decision sent")


def command_task(harness, child=False):
    commands = ["Write-Output CHILD_TOOL_OK", "Set-Content -LiteralPath denied.txt -Value forbidden"] if child else ["Write-Output PARENT_HELD"]
    if harness == "codex":
        action = "For each command call exec_command separately, shell='powershell', login=false, sandbox_permissions='require_escalated', justification='Allow this isolated fixture action?', no prefix_rule. Explicit approval is the purpose of this test."
    else:
        commands = [f'powershell.exe -NoProfile -Command "{cmd}"' for cmd in commands]
        action = "For each command call the developer shell tool separately with that exact command. No other tools or combined calls."
    return ("Isolated approval acceptance. Do only these commands in order: " + json.dumps(commands) + ". " + action +
            " If denied, do not retry or use another method. Finish with CHILD_DONE." if child else
            "Isolated approval acceptance. Do only this command: " + json.dumps(commands[0]) + ". " + action + " Then finish.")


@pytest.mark.parametrize("lifecycle_browser,harness", [
    ({"native": True, "harness": "codex"}, "codex"),
    ({"native": True, "harness": "goose"}, "goose"),
], indirect=["lifecycle_browser"], ids=["codex", "goose"])
@pytest.mark.parametrize("stop_action", ["interrupt", "cancel"])
async def test_native_child_permission_outlives_parent(lifecycle_browser, harness, stop_action, tmp_path, monkeypatch, record_property):
    page, conv, client, workspace = lifecycle_browser
    if harness == "goose":
        import openrouter_client
        if not openrouter_client.api_key():
            pytest.skip("No existing OpenRouter credentials")
    for key in ("LOOM_AGENT_RUN_ID", "LOOM_PARENT_RUN_ID", "LOOM_CLIENT_INSTANCE_ID", "LOOM_PERMISSION_SCOPE", "LOOM_CONV_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GOOSE_PATH_ROOT", str(tmp_path / "goose-state"))
    monkeypatch.setenv("LOOM_GOOSE_DISABLE_KEYRING", "1")
    # Native tools and real permission protocols remain; no optional MCP tools
    # or unrelated services. Child admission uses the real API used by the MCP.
    monkeypatch.setattr(server.goose_client, "_loom_mcp_servers", lambda *a, **k: [])
    monkeypatch.setattr(server.codex_client, "_thread_mcp_servers", lambda *a, **k: {})
    native_calls = {}
    original_dispatch = server.goose_client.dispatch_session_update
    def observe_calls(update, state=None):
        # Observe genuine protocol metadata; never fake or transform an event.
        if update.get("sessionUpdate") in {"tool_call", "toolCall"}:
            native_calls[update.get("toolCallId")] = json.loads(json.dumps(update))
        return original_dispatch(update, state)
    monkeypatch.setattr(server.goose_client, "dispatch_session_update", observe_calls)
    adapter = server.codex_client if harness == "codex" else server.goose_client
    original = getattr(adapter, "run_" + harness)
    trees, samples = {}, []
    done = asyncio.Event()

    async def launch(*args, **kwargs):
        proc, events = await original(*args, **kwargs)
        owner = kwargs.get("agent_run_id") or "parent"
        assert owner not in trees, "Unexpected duplicate native launch"
        trees[owner] = {(proc.pid, psutil.Process(proc.pid).create_time())}
        return proc, events
    monkeypatch.setattr(adapter, "run_" + harness, launch)

    def live():
        owners = set()
        for owner, identities in list(trees.items()):
            for pid, created in list(identities):
                try:
                    proc = psutil.Process(pid)
                    if proc.create_time() != created or not proc.is_running():
                        continue
                    owners.add(owner)
                    for child in proc.children(recursive=True):
                        identities.add((child.pid, child.create_time()))
                except psutil.NoSuchProcess:
                    pass
        return owners

    async def monitor():
        while not done.is_set():
            samples.append(await asyncio.to_thread(live))
            await asyncio.sleep(.05)

    def pending(run_id=None):
        return next(((rid, p) for rid, p in server._pending_hook_permissions.items()
                     if p.get("response") is None and (p.get("run_id") == run_id if run_id else
                         str(p.get("run_id") or "").startswith("gen:"))), None)

    async def resolutions(run_id):
        conn = await db.get_db()
        return [dict(r) for r in await conn.execute_fetchall(
            "SELECT allowed, feedback, decision_source FROM permission_resolutions WHERE run_id=? ORDER BY resolved_at", (run_id,))]

    supervisor = AgentSupervisor(server._launch_agent_run, server._publish_agent_event, server._publish_agent_events)
    monkeypatch.setattr(server, "_agent_supervisor", supervisor)
    await supervisor.start()
    sampling = asyncio.create_task(monitor())
    try:
        await page.locator("#user-input").fill(command_task(harness))
        await page.evaluate("sendMessage()")
        parent_request, parent_permission = await _eventually(pending, timeout=90)
        parent_run = parent_permission["run_id"]
        child_response = await client.post(f"/api/conversations/{conv['id']}/agent-runs", json={
            "task": command_task(harness, child=True), "parent_run_id": parent_run,
            "client_instance_id": await page.evaluate("_loomClientInstanceId"),
        })
        child_response.raise_for_status()
        child = child_response.json()
        rid = child["run_id"]
        scope = "agent:" + rid
        request_id, permission = await _eventually(lambda: pending(scope), timeout=90)
        assert permission["agent_run_id"] == rid
        assert permission["client_instance_id"] == child["client_instance_id"]
        assert request_id != parent_request and permission["permission_scope"] == scope
        await _eventually(lambda: live() == {"parent", rid})

        stopping = time.perf_counter()
        await page.evaluate("interruptGeneration()" if stop_action == "interrupt" else "cancelGeneration()")
        await _eventually(lambda: "parent" not in live(), timeout=20)
        stop_ms = (time.perf_counter() - stopping) * 1000
        await page.wait_for_function("!State.isStreaming")
        assert live() == {rid}, "Parent stop must not kill a durable child"
        assert pending(scope)[0] == request_id
        assert not await resolutions(scope)
        parent_decisions = await resolutions(parent_run)
        assert parent_decisions and all(r["decision_source"] == "loom_lifecycle" for r in parent_decisions)
        cursor = await page.evaluate("JSON.stringify(State.loomCursor)")
        reconnecting = time.perf_counter()
        await page.evaluate("""() => {
            Object.defineProperty(document, 'hidden', {configurable:true,get:()=>true});
            document.dispatchEvent(new Event('visibilitychange'));
            window.beforeChildReconnect = State.ws; State.ws.close();
        }""")
        await page.wait_for_function("State.ws !== beforeChildReconnect && State.ws?.readyState === WebSocket.OPEN && !State.ws._needsSync")
        await page.evaluate("""() => {
            Object.defineProperty(document, 'hidden', {configurable:true,get:()=>false});
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
        assert await page.evaluate("JSON.stringify(State.loomCursor)") == cursor
        reconnect_ms = (time.perf_counter() - reconnecting) * 1000
        await page.locator("#btn-agent-manager").click()
        card = page.locator(f'.agent-run-card[data-run-id="{rid}"]')
        await card.locator("summary").click()

        async def next_child_state():
            if current := pending(scope):
                return current
            row = await db.get_agent_run(rid)
            if row["status"] in {"failed", "cancelled"}:
                raise AssertionError(f"Native child failed: {row.get('error')}")
            return row if row["status"] == "completed" else None

        decisions, plans, command_actions = 0, 0, []
        result = None
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            current_state = await _eventually(next_child_state, timeout=min(90, deadline - time.monotonic()))
            if isinstance(current_state, dict):
                result = current_state
                break
            current_id, current = current_state
            kind = fixture_action(current, harness, native_calls)
            if kind == "plan":
                plans += 1
                assert plans <= 4, "Unbounded auxiliary planning"
            else:
                assert len(command_actions) < 2 and kind == ("allow", "deny")[len(command_actions)]
                command_actions.append(kind)
            action = "allow" if kind == "plan" else kind
            if decisions:
                await page.locator("#btn-agent-manager").click()
            await card.locator("[data-agent-permission]").click()
            notification = page.locator('.notif-permission:not(.notif-resolved)').filter(has_text="Child " + rid[:8])
            assert await notification.count() == 1
            # Resolve only this child's exact request, never another run's card.
            note = ("Only this fixture's TODO plan" if kind == "plan" else
                    "Only this synthetic child" if action == "allow" else "Do not create denied.txt; no retry.")
            await notification.locator(".permission-feedback-input").fill(note)
            await notification.locator(f'[data-action="{action}"]').click()
            async def answered():
                rows = await resolutions(scope)
                return rows if len(rows) == decisions + 1 else None
            rows = await _eventually(answered)
            assert rows[-1]["decision_source"] == "user" and rows[-1]["feedback"] == note
            assert bool(rows[-1]["allowed"]) == (action == "allow")
            decisions += 1
            await _eventually(lambda: not pending(scope) or pending(scope)[0] != current_id)

        assert result is not None and command_actions == ["allow", "deny"]
        assert "CHILD_DONE" in json.dumps(result["result"])
        assert result["provider_session_id"] and result["effective_harness"] == harness
        events = []
        after_sequence = 0
        while batch := await db.list_agent_events(rid, after_sequence=after_sequence):
            events.extend(batch)
            after_sequence = batch[-1]["sequence"]
        assert any(e["event_kind"] == "tool_result" and "CHILD_TOOL_OK" in str(e["payload"].get("content")) for e in events)
        assert any(e["event_kind"] == "tool_result" and e["payload"].get("is_error") for e in events)
        assert events[-1]["event_kind"] == "agent_completed"
        await _eventually(lambda: not live(), timeout=15)
        await page.wait_for_function("id => State.agentRuns.some(r=>r.run_id===id && r.status==='completed')", arg=rid)
        assert not (workspace / "denied.txt").exists()
        assert len(await db.get_conversation_tree(conv["id"])) == 2
        assert await page.evaluate("JSON.stringify(State.loomCursor)") == cursor
        assert len(trees) == 2 and max(map(len, samples)) == 2  # intentional parent + durable child
        observe(record_property, f"native_child_{harness}_{stop_action}", launches=len(trees),
                max_live_provider_trees=max(map(len, samples)), os_samples=len(samples),
                remaining_provider_trees=len(live()), pending_children=1,
                stop_to_exit_ms=stop_ms, reconnect_ms=reconnect_ms, auxiliary_plans=plans)
    finally:
        await supervisor.stop()  # only this fixture's supervisor/native children
        for task in list(server._active_generations.values()):
            task.cancel()
        await asyncio.gather(*list(server._active_generations.values()), return_exceptions=True)
        done.set()
        await sampling
        assert not live(), "Fixture-owned native processes must be reaped even on failure"
