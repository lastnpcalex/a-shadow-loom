"""Opt-in, model-driven MCP delegation through real Codex/Goose; never a live host."""
import asyncio
import json
import os
import time
import uuid

import psutil
import pytest

import database as db
import server
from agent_supervisor import AgentSupervisor
from permission_commands import command_template, display_argv
from tests.lifecycle_metrics import observe
from tests.test_fullstack_queue_isolation import _eventually
from tests.test_lifecycle_handoff_browser import lifecycle_browser
from tests.test_native_child_lifecycle import command_task, fixture_action

pytestmark = pytest.mark.skipif(os.getenv("LOOM_RUN_NATIVE_DELEGATION_VERIFY") != "1",
                              reason="Opt-in paid model-driven Codex/Goose MCP delegation")

MODELS = {"codex": "gpt-5.6-luna", "goose": "goose:openrouter:z-ai/glm-5.3-flash"}


def delegation_task(harness, task):
    return ("Isolated end-to-end Loom delegation acceptance. Use only the loom-agent-control MCP tools, "
            "not provider-native subagents, shell, scripts, or HTTP. Call spawn_agent exactly once with "
            f"harness={harness!r}, model={MODELS[harness]!r}, path_scope=['.'], and this exact self-contained task: "
            + json.dumps(task) + ". Keep the returned run_id; call wait_agent(run_id, timeout_sec=120), "
            "repeat wait if still active, then call get_agent after completion and report its actual result. "
            "Do not finish while your child is active, invent output, read its files yourself, or spawn replacements.")


def tool_action(name):
    return next((n for n in ("spawn_agent", "wait_agent", "get_agent") if str(name).endswith(n)), None)


@pytest.mark.parametrize("lifecycle_browser,root_harness", [
    ({"native": True, "harness": "codex"}, "codex"),
    ({"native": True, "harness": "goose"}, "goose"),
], indirect=["lifecycle_browser"], ids=["codex-goose-codex", "goose-codex-goose"])
async def test_models_delegate_nested_cross_harness(lifecycle_browser, root_harness, tmp_path, monkeypatch, record_property):
    page, conv, client, workspace = lifecycle_browser
    import openrouter_client
    if not openrouter_client.api_key():
        pytest.skip("Existing OpenRouter credentials required")
    for key in ("LOOM_AGENT_RUN_ID", "LOOM_PARENT_RUN_ID", "LOOM_CLIENT_INSTANCE_ID", "LOOM_PERMISSION_SCOPE", "LOOM_CONV_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GOOSE_PATH_ROOT", str(tmp_path / "goose-state"))
    monkeypatch.setenv("LOOM_GOOSE_DISABLE_KEYRING", "1")
    original_servers = server.goose_client._loom_mcp_servers
    monkeypatch.setattr(server.goose_client, "_loom_mcp_servers", lambda *a, **k: [
        s for s in original_servers(*a, **k) if s["name"] == "loom-agent-control"])
    # Keep the real agent-control MCP on both harnesses. No synthetic MCP calls
    # and no test-side child POST: the models must create all descendants.
    middle_harness = "goose" if root_harness == "codex" else "codex"
    leaf_task = command_task(root_harness, child=True).replace("Write-Output CHILD_TOOL_OK", "Get-Content -LiteralPath proof.txt")
    leaf_task += " Include the exact file contents in your final result."
    middle_task = delegation_task(root_harness, leaf_task)
    root_task = delegation_task(middle_harness, middle_task)
    proof = "DELEGATED_PROOF_" + uuid.uuid4().hex
    (workspace / "proof.txt").write_text(proof, encoding="utf-8")
    assert proof not in root_task  # only a successful leaf tool can reveal it
    native_calls, trace, trees, samples = {}, {}, {}, []
    stopped = asyncio.Event()
    original_dispatch = server.goose_client.dispatch_session_update
    def dispatch(update, state=None):
        if update.get("sessionUpdate") in {"tool_call", "toolCall"}:
            owner = getattr(asyncio.current_task(), "_agent_run_id", None) or "parent"
            native_calls[update.get("toolCallId")] = (owner, json.loads(json.dumps(update)))
        return original_dispatch(update, state)
    monkeypatch.setattr(server.goose_client, "dispatch_session_update", dispatch)
    for harness in MODELS:
        adapter = getattr(server, harness + "_client")
        original = getattr(adapter, "run_" + harness)
        async def launch(*args, _original=original, _harness=harness, **kwargs):
            proc, events = await _original(*args, **kwargs)
            owner = kwargs.get("agent_run_id") or "parent"
            assert owner not in trees, "Duplicate provider launch"
            trees[owner] = {(proc.pid, psutil.Process(proc.pid).create_time())}
            trace[owner] = {"harness": _harness, "events": []}
            async def traced():
                async for event in events:
                    trace[owner]["events"].append(dict(event))
                    yield event
            return proc, traced()
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
        while not stopped.is_set():
            samples.append(await asyncio.to_thread(live))
            await asyncio.sleep(.05)

    supervisor = AgentSupervisor(server._launch_agent_run, server._publish_agent_event, server._publish_agent_events)
    supervisor.max_concurrent = 1  # one independent leaf, coordinating ancestors do not deadlock
    monkeypatch.setattr(server, "_agent_supervisor", supervisor)
    await supervisor.start()
    sampling = asyncio.create_task(monitor())
    answered, plans, denied, reconnected = set(), 0, 0, False
    started = time.perf_counter()
    try:
        # Add only a test selector to the real renderer; retain its DOM and
        # event handlers, then click the actual displayed permission card.
        await page.evaluate("""() => {
            const render = _renderPermissionNotifItem;
            window._renderPermissionNotifItem = n => {
                const item = render(n); item.dataset.testRequestId = n.requestId; return item;
            };
        }""")
        await page.locator("#user-input").fill(root_task)
        await page.evaluate("sendMessage()")
        await page.wait_for_function("State.isStreaming && State._followingDraftMsgId != null")
        await page.wait_for_function("State.loomCursor?.leaf_message_id === State._followingDraftMsgId")
        cursor = await page.evaluate("JSON.stringify(State.loomCursor)")
        deadline = time.monotonic() + 480
        while time.monotonic() < deadline:
            rows = await db.list_agent_runs(conv["id"])
            assert len(rows) <= 2, "Model spawned unexpected extra children"
            assert all(r["status"] not in {"failed", "cancelled", "paused"} for r in rows), [(r["status"], r.get("error")) for r in rows]
            for request_id, p in list(server._pending_hook_permissions.items()):
                if p.get("response") is not None or request_id in answered:
                    continue
                args = p.get("tool_input") or {}
                allowed, kind = None, None
                raw_call = native_calls.get((args.get("acp_identity") or {}).get("tool_call_id"), (None, {}))[1]
                identity = raw_call.get("_meta", {}).get("goose", {}).get("toolCall", {})
                action = tool_action(identity.get("toolName"))
                raw = raw_call.get("rawInput")
                if isinstance(raw, str):
                    raw = json.loads(raw)
                if action and identity.get("extensionName", "").replace("-", "").replace("_", "") == "loomagentcontrol":
                    assert isinstance(raw, dict)
                    if action == "spawn_agent":
                        assert (raw.get("harness"), raw.get("model"), raw.get("task")) in {
                            (middle_harness, MODELS[middle_harness], middle_task), (root_harness, MODELS[root_harness], leaf_task)}
                        assert raw.get("path_scope") == ["."] and not raw.get("profile")
                    else:
                        assert raw.get("run_id") in {r["run_id"] for r in rows}
                    allowed, kind = True, "control"
                elif p.get("tool_name") == "Bash":
                    command = args.get("command")
                    parsed = command_template(display_argv(command), command) if isinstance(command, list) else command_template(command or "")
                    if parsed and parsed[0] == ["Get-Content", "-LiteralPath", "proof.txt"]:
                        allowed, kind = True, "read"
                if allowed is None:
                    kind = fixture_action(p, "goose", {k:v[1] for k,v in native_calls.items()})
                    assert kind in {"plan", "deny"}
                    allowed = kind == "plan"
                if kind == "plan":
                    plans += 1
                    assert plans <= 8
                if kind == "deny":
                    denied += 1
                    assert denied == 1
                    assert len(rows) == 2
                    leaf = next(r for r in rows if r["parent_run_id"] in {x["run_id"] for x in rows})
                    assert p["run_id"] == "agent:" + leaf["run_id"]
                    assert p["client_instance_id"] == await page.evaluate("_loomClientInstanceId")
                    # Reconnect with all three real providers alive, while the
                    # leaf's exact approval remains unresolved.
                    assert live() == set(trees) and len(trees) == 3
                    await page.evaluate("window.oldDelegationSocket = State.ws; State.ws.close()")
                    await page.wait_for_function("State.ws !== oldDelegationSocket && State.ws?.readyState === WebSocket.OPEN && !State.ws._needsSync")
                    assert await page.evaluate("JSON.stringify(State.loomCursor)") == cursor
                    reconnected = True
                # Require the exact real notification, then invoke its actual
                # button handler. Do not fabricate an approval API response.
                await page.wait_for_function("id => _notifications.some(n => n.requestId === id && !n.resolved)", arg=request_id)
                if not await page.locator('#notif-dropdown').is_visible():
                    await page.locator('#notif-bell').click()
                card = page.locator(f'.notif-permission[data-test-request-id="{request_id}"]')
                await card.locator('.permission-feedback-input').fill('Synthetic delegation acceptance; no retry on denial.')
                await card.locator('[data-action="allow"]' if allowed else '[data-action="deny"]').click()
                async def resolved():
                    conn = await db.get_db()
                    return await conn.execute_fetchall("SELECT allowed, decision_source, feedback FROM permission_resolutions WHERE request_id=?", (request_id,))
                resolution = (await _eventually(resolved))[0]
                assert bool(resolution["allowed"]) == allowed and resolution["decision_source"] == "user"
                answered.add(request_id)
            if len(rows) == 2 and all(r["status"] == "completed" for r in rows) and not await page.evaluate("State.isStreaming"):
                break
            await asyncio.sleep(.1)
        else:
            raise AssertionError("Model delegation did not complete within its bound")
        assert denied == 1 and reconnected
        rows = await db.list_agent_runs(conv["id"])
        middle = next(r for r in rows if str(r["parent_run_id"]).startswith("gen:"))
        leaf = next(r for r in rows if r["parent_run_id"] == middle["run_id"])
        chat = await db.get_conversation_tree(conv["id"])
        assert len(chat) == 2
        # The lightweight tree projection intentionally omits full content.
        final_message = await db.get_message(chat[-1]["id"])
        assert final_message["role"] == "assistant" and proof in final_message["content"]
        assert str(middle["parent_run_id"]).startswith("gen:")
        assert middle["parent_message_id"] == chat[-1]["id"]
        assert leaf["parent_run_id"] == middle["run_id"]
        for run, harness in ((middle, middle_harness), (leaf, root_harness)):
            assert run["effective_harness"] == harness and run["effective_model"] == MODELS[harness]
            assert run["path_scope"] == ["."] and run["provider_session_id"]
            assert run["client_instance_id"] == await page.evaluate("_loomClientInstanceId")
            assert proof in json.dumps(run["result"])
        for owner in ("parent", middle["run_id"]):
            if trace[owner]["harness"] == "codex":
                names = [e.get("name", "") for e in trace[owner]["events"] if e.get("type") == "tool_start"]
                assert all(n.startswith("loom-agent-control.") or n in {"tool_search", "ToolSearch"} for n in names), names
                actions = [tool_action(n) for n in names if "loom" in n.lower()]
            else:
                identities = [c.get("_meta", {}).get("goose", {}).get("toolCall", {})
                              for o,c in native_calls.values() if o == owner]
                assert all(i.get("extensionName", "").replace("-", "").replace("_", "") in {"loomagentcontrol", "todo"}
                           for i in identities), identities
                actions = [tool_action(c.get("_meta", {}).get("goose", {}).get("toolCall", {}).get("toolName"))
                           for o,c in native_calls.values() if o == owner]
            assert actions.count("spawn_agent") == 1 and "wait_agent" in actions and "get_agent" in actions, actions
            assert actions.index("spawn_agent") < actions.index("wait_agent") < actions.index("get_agent")
        assert not (workspace / "denied.txt").exists()
        assert await page.evaluate("JSON.stringify(State.loomCursor)") == cursor
        await _eventually(lambda: not live(), timeout=20)
        observe(record_property, "native_delegation_" + root_harness, launches=len(trees),
                max_live_provider_trees=max(map(len, samples)), remaining_provider_trees=0,
                os_samples=len(samples), auxiliary_plans=plans, wall_ms=(time.perf_counter()-started)*1000)
    finally:
        await supervisor.stop()
        tasks = list(server._active_generations.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        stopped.set()
        await sampling
        assert not live(), "Test-owned process tree survived fixture cleanup"
