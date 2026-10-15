"""Deterministic pre-deployment workflows through real normalizers and policy.

No tool execution, models, production database, host lifecycle or live HTTP.
Native wire-shape fixtures complement, rather than impersonate, model tests.
"""
import asyncio

import pytest

import database as db
import server
from acp_permissions import permission_action, remember_tool_call
from cc_permission_hook import normalize_tool_args, normalize_tool_name
from codex_client import _app_permission_payload
from tests.permission_acceptance_metrics import observe
from tests.test_live_permission_acceptance import _answer
from tests.test_permission_payloads import goose_wire


HARNESSES = ["goose_acp", "codex_app_server", "claude_code", "antigravity"]


def wire(harness, kind, root, *, path="seed.txt", command="python -m pytest tests/a.py -q"):
    method = ""
    if harness == "goose_acp":
        tool = {"read": "shell", "write": "write", "edit": "edit", "command": "shell", "unknown": "unmapped"}[kind]
        args = ({"command": f'powershell.exe -NoProfile -Command "Get-Content -LiteralPath {path}"'} if kind == "read" else
                {"command": command} if kind == "command" else
                {"path": path, "content": "fixture"} if kind == "write" else
                {"path": path, "before": "fixture", "after": "edited"} if kind == "edit" else {"path": path})
        initial, request = goose_wire(tool, args)
        state = {}
        remember_tool_call(state, initial)
        name, args = permission_action(request, state=state, cwd=str(root))
    elif harness == "codex_app_server":
        if kind in {"command", "read"}:
            method = "item/commandExecution/requestApproval"
            argv = (["powershell.exe", "-NoProfile", "-Command", f"Get-Content -LiteralPath {path}"]
                    if kind == "read" else command.split())
            name, args = _app_permission_payload(method, {"command": argv, "cwd": str(root)})
        elif kind in {"write", "edit"}:
            method = "item/fileChange/requestApproval"
            name, args = _app_permission_payload(method, {"changes": {path: {"type": "add" if kind == "write" else "update"}}})
        else:
            name, args = "Unknown", {"path": path}
    elif harness == "antigravity":
        native = {"read": "view_file", "write": "write_to_file", "edit": "replace_file_content",
                  "command": "run_command", "unknown": "unmapped"}[kind]
        args = {"CommandLine": command, "Cwd": str(root)} if kind == "command" else {"TargetFile": path, "CodeContent": "fixture"}
        name, args = normalize_tool_name(native), normalize_tool_args(native, args)
    else:
        name = {"read": "Read", "write": "Write", "edit": "Edit", "command": "PowerShell", "unknown": "Unmapped"}[kind]
        args = {"command": command} if kind == "command" else {"file_path": path, "content": "fixture"}
    return {"source": harness, "tool_name": name, "tool_input": {**args, "cwd": str(root)}, "approval_method": method}


@pytest.mark.parametrize("harness", HARNESSES)
async def test_permission_workflow_counts(harness, tmp_path, monkeypatch, record_property, client):
    monkeypatch.setattr(server, "_permission_rules", {})
    monkeypatch.setattr(server, "_pending_hook_permissions", {})
    monkeypatch.setattr(server, "_generation_snapshots", {})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    conv = await db.create_conversation("Permission workflow fixture", mode="goose", project_dir=str(workspace))
    await db.update_conversation_fields(conv["id"], permission_profile="balanced")
    root = await db.add_message(conv["id"], "user", "fixture")
    draft = await db.add_message(conv["id"], "assistant", "fixture", parent_id=root["id"])
    continuation = await db.add_message(conv["id"], "user", "queued continuation", parent_id=draft["id"])
    await db.enqueue_generation(continuation["id"], conv["id"], draft["id"])
    sibling = await db.add_message(conv["id"], "user", "sibling fixture", parent_id=root["id"])
    scope = "gen:9910"
    key = (conv["id"], root["id"], 9910)
    server._generation_snapshots[key] = {"parent_id": root["id"], "draft_msg_id": draft["id"]}

    async def check(scenario, kind="command", *, asks=0, remember=False, deny=False, restored=False,
                    matched=False, unknown=False, eligible=True, profile="balanced", child=None, cancelled=False, **args):
        payload = {**wire(harness, kind, workspace, **args), "loom_conv_id": conv["id"],
                   "permission_scope": f"agent:{child}" if child else scope}
        task = asyncio.create_task(server.handle_cc_permission(payload))
        prompts = 0
        try:
            async with asyncio.timeout(5):
                while not task.done() and not server._pending_hook_permissions:
                    await asyncio.sleep(.01)
                if server._pending_hook_permissions:
                    rid, pending = next(iter(server._pending_hook_permissions.items()))
                    prompts = 1
                    assert pending["run_id"] == payload["permission_scope"]
                    assert pending["supports_allow_all"] is eligible
                    if restored:
                        class Socket:
                            def __init__(self):
                                self.events = []
                            async def send_json(self, event):
                                self.events.append(event)
                        socket = Socket()
                        server._event_router.register(socket, conv["id"])
                        server._event_router.subscribe(socket, payload["permission_scope"])
                        try:
                            await server._send_pending_permissions_to_socket(socket, conv["id"])
                            event = next(e for e in socket.events if e.get("request_id") == rid)
                            assert event["rule_preview"] == pending["rule_preview"]
                            assert event["run_id"] == pending["run_id"]
                        finally:
                            server._event_router.unregister(socket)
                    if cancelled:
                        await server._resolve_pending_permissions_for_scope(conv["id"], payload["permission_scope"], message="Fixture interrupted")
                    else:
                        await _answer(conv["id"], {"request_id": rid, "allow": not deny, "always": remember,
                                                  "feedback": "Stay in the fixture workspace." if deny else ""})
                result = await task
            # Inspect the persisted decision even on lifecycle cancellation.
            audits = await db.get_permission_policy_audits(conv["id"])
            action = audits[-1]["action"]
            counts = {"prompts": prompts, "unknown": int(action["effect"] == "unknown" or action["boundary"] == "unknown"),
                      "rule_matches": int(action["rule_matched"])}
            observe(record_property, harness, scenario, profile=profile,
                    expected={"prompts": asks, "unknown": int(unknown), "rule_matches": int(matched)}, observed=counts)
            assert result["allow"] is (not deny and not cancelled)
            assert not result.get("always"), "Never install a provider-wide session grant"
            source = "loom_lifecycle" if cancelled else "user" if prompts else "loom_policy"
            assert result["decision_source"] == source
            if deny:
                assert "Stay in the fixture workspace." in result["feedback"]
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    await check("workspace_read", "read")
    await check("workspace_write", "write")
    await check("workspace_edit", "edit")
    await check("initial_command_restored_card", asks=1, remember=True, restored=True)
    # Advance only the test's generation owner to the already queued child.
    # Scheduler launch/kill topology itself is covered by the browser suite.
    del server._generation_snapshots[key]
    key = (conv["id"], continuation["id"], 9911)
    scope = "gen:9911"
    server._generation_snapshots[key] = {"parent_id": continuation["id"]}
    await check("queued_descendant_similar_command", command="python -m pytest tests/b.py -q", matched=True)
    assert (await db.get_message(continuation["id"]))["parent_id"] == draft["id"]
    child = await db.create_agent_run("workflow-child", conv["id"], "fixture", parent_message_id=continuation["id"], project_dir=str(workspace))
    await db.update_agent_run(child["run_id"], status="running")
    await check("child_inherits_branch_rule", child=child["run_id"], matched=True)
    await check("changed_command", command="python -m pytest tests/a.py -s", asks=1, deny=True)
    server._generation_snapshots[key] = {"parent_id": sibling["id"]}
    await check("sibling_does_not_inherit", asks=1, deny=True)
    server._generation_snapshots[key] = {"parent_id": continuation["id"]}
    outside = str(tmp_path / "outside.txt")
    await check("outside_read", "read", path=outside, asks=1, deny=True, eligible=False)
    await check("outside_write", "write", path=outside, asks=1, deny=True, eligible=False)
    await check("unknown_tool", "unknown", asks=1, deny=True, unknown=True, eligible=False)
    foreign = await db.create_agent_run("foreign-child", conv["id"], "fixture", parent_message_id=continuation["id"], project_dir=str(tmp_path / "other"))
    await db.update_agent_run(foreign["run_id"], status="running")
    await check("child_other_workspace", child=foreign["run_id"], asks=1, deny=True, eligible=False)
    await check("cancelled_waiter_not_user_denial", command="node fixture.js", asks=1, cancelled=True)
    rules = (await client.get(f"/api/conversations/{conv['id']}/permission-rules")).json()["rules"]
    assert len(rules) == 1
    assert (await client.delete(f"/api/conversations/{conv['id']}/permission-rules/{rules[0]['id']}")).status_code == 200
    await check("revoked_rule_asks", asks=1, remember=True)
    # Simulate only the documented session-local grant lifetime; no host restart.
    server._permission_rules.clear()
    await check("new_server_session_forgets_rule", asks=1)
    await db.update_conversation_fields(conv["id"], permission_profile="trusted")
    await check("trusted_command", profile="trusted")
    await check("trusted_unknown_still_asks", "unknown", asks=1, deny=True, unknown=True, eligible=False, profile="trusted")
    await db.update_conversation_fields(conv["id"], permission_profile="strict")
    await check("strict_write", "write", asks=1, profile="strict")
    await check("strict_read", "read", profile="strict")
    assert not server._pending_hook_permissions
