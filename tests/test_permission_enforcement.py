"""Live-policy acceptance: disposable SQLite, no real provider or host actions."""
import asyncio
import json
from pathlib import Path

import pytest

import database as db
import permission_policy as policy
import permission_rules
import server


@pytest.fixture(autouse=True)
def isolated_permissions(monkeypatch):
    monkeypatch.setattr(server, "_permission_rules", {})
    monkeypatch.setattr(server, "_pending_hook_permissions", {})
    monkeypatch.setattr(server, "_generation_snapshots", {})


def normalize(root, name="Bash", payload=None, method=""):
    return policy.normalize_permission_request(tool_name=name, tool_input=payload or {},
        workspace_root=str(root), approval_method=method)


@pytest.mark.parametrize("payload", [
    {"toolCall": {"title": "Write the output", "kind": "execute", "rawInput": {"command": "node build.js"}}},
    {"command": "node build.js", "content": {"tool_name": "Write", "path": "safe.txt"}},
])
def test_command_title_or_result_cannot_downgrade_execution(tmp_path, payload):
    action = normalize(tmp_path, "Bash", payload)
    assert action.effect is policy.ActionEffect.EXECUTE
    assert policy.decide(policy.PermissionProfile.BALANCED, action) is policy.PolicyDecision.ASK


@pytest.mark.parametrize("payload", [
    {"file_path": "../outside.txt", "cwd": "."},
    {"paths": ["safe.txt", "../outside.txt"]},
    {"changes": {"safe.txt": {}, "../outside.txt": {}}},
    {"command": "*** Begin Patch\n*** Update File: safe.txt\n*** Move to: ../outside.txt\n*** End Patch"},
])
def test_every_mutation_path_must_be_inside_workspace(tmp_path, payload):
    action = normalize(tmp_path, "Write", payload)
    assert action.boundary is policy.ActionBoundary.OUTSIDE_WORKSPACE
    assert policy.decide(policy.PermissionProfile.BALANCED, action) is policy.PolicyDecision.ASK


def test_relative_paths_use_request_workspace_not_loom_cwd(tmp_path):
    assert normalize(tmp_path, "Edit", {"path": "src/test.py"}).boundary is policy.ActionBoundary.WORKSPACE
    assert normalize(tmp_path, "Edit", {"cwd": str(tmp_path)}).boundary is policy.ActionBoundary.UNKNOWN
    action = normalize(tmp_path, "unrecognized_writer", {"result": {"name": "Read"}})
    assert action.effect is policy.ActionEffect.UNKNOWN


@pytest.mark.parametrize("command", [
    "python -m pytest tests/a.py; node other.js", "python -m pytest $(whoami)",
    "python -c 'print(1)'", 'powershell -Command "Get-ChildItem | Out-String"',
    "python -m pytest ../outside.py", "python -m pytest *.py",
])
def test_opaque_or_escaping_commands_have_no_widened_rule(tmp_path, command):
    args = {"command": command}
    assert permission_rules.action_rule(normalize(tmp_path, payload=args), args) is None


def test_rule_shape_preserves_runner_module_options_and_ids(tmp_path):
    def shape(command):
        args = {"command": command}
        return permission_rules.action_rule(normalize(tmp_path, payload=args), args)["shape"]
    assert shape("python -m pytest tests/a.py -q") == shape("python -m pytest tests/b.py -q")
    assert shape("python -m pytest tests/a.py -q") != shape("python -m pytest tests/a.py -s")
    assert shape("node app.js apply --course 111") != shape("node app.js apply --course 222")
    assert shape("node app.js apply --course 111") != shape("node other.js apply --course 111")
    assert shape("git checkout feature/one") != shape("git checkout feature/two")
    assert shape("curl https://example.invalid/one") != shape("curl https://example.invalid/two")
    assert shape("node app.js --url example.invalid") != shape("node app.js --url another.invalid")
    assert shape("git diff -- src/a.py") == shape("git diff -- src/b.py")


@pytest.mark.parametrize("harness,name,payload,method", [
    ("claude_code", "Write", {"file_path": "notes.txt"}, "PreToolUse"),
    ("antigravity", "Write", {"file_path": "notes.txt"}, "BeforeTool"),
    ("codex", "FileChange", {"changes": {"notes.txt": {"type": "add"}}}, "item/fileChange/requestApproval"),
    ("goose_acp", "Write notes", {"toolCall": {"kind": "edit", "rawInput": {"path": "notes.txt"}}}, ""),
    ("hermes_acp", "Write notes", {"toolCall": {"kind": "edit", "arguments": {"path": "notes.txt"}}}, ""),
    ("loom_workspace", "Edit", {"file_path": "notes.txt"}, ""),
])
async def test_all_bridges_use_same_live_balanced_decision(tmp_path, harness, name, payload, method):
    conv = await db.create_conversation("Policy fixture", mode="codex", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], permission_profile="balanced")
    reply = await server.handle_cc_permission({"loom_conv_id": conv["id"], "tool_name": name,
        "tool_input": payload, "source": harness, "approval_method": method})
    assert reply["allow"] is True
    assert reply["always"] is False
    assert reply["decision_source"] == "loom_policy"
    assert reply["policy"]["mode"] == "enforced"
    audits = await db.get_permission_policy_audits(conv["id"])
    assert audits[-1]["policy_decision"] == "auto_allow"
    assert str(tmp_path) not in json.dumps(audits)


async def wait_pending(task):
    for _ in range(100):
        if task.done():
            pytest.fail(f"Expected an approval request, got {task.result()}")
        if server._pending_hook_permissions:
            return next(iter(server._pending_hook_permissions.items()))
        await asyncio.sleep(.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    pytest.fail("No permission request arrived")


async def answer(conv_id, request_id, *, allow, remember=False):
    from starlette.websockets import WebSocketDisconnect
    class Socket:
        _client_instance_id = "policy-window"
        received = False
        async def send_json(self, payload):
            pass
        async def receive_text(self):
            if self.received:
                raise WebSocketDisconnect()
            self.received = True
            return json.dumps({"action": "permission_response", "request_id": request_id,
                              "allow": allow, "always": remember, "feedback": "Fixture decision"})
    await server._continue_ws_chat(Socket(), conv_id, "policy-window")


async def test_branch_grant_survives_forks_not_siblings_or_workspace_changes(tmp_path, client):
    conv = await db.create_conversation("Branch policy", mode="codex", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], permission_profile="balanced")
    root = await db.add_message(conv["id"], "user", "start")
    draft = await db.add_message(conv["id"], "assistant", "working", parent_id=root["id"])
    descendant = await db.add_message(conv["id"], "user", "continue", parent_id=draft["id"])
    sibling = await db.add_message(conv["id"], "user", "separate", parent_id=root["id"])
    scope = "gen:901"
    key = (conv["id"], root["id"], 901)
    server._generation_snapshots[key] = {"parent_id": root["id"], "draft_msg_id": draft["id"]}
    def request(command="python -m pytest tests/a.py -q", harness="codex"):
        return server.handle_cc_permission({"loom_conv_id": conv["id"], "permission_scope": scope,
            "source": harness, "tool_name": "Bash", "tool_input": {"command": command}})
    first = asyncio.create_task(request())
    rid, pending = await wait_pending(first)
    assert pending["supports_allow_all"] is True
    assert "<workspace-file>" in pending["rule_preview"]
    await answer(conv["id"], rid, allow=True, remember=True)
    result = await asyncio.wait_for(first, 2)
    assert result["allow"] is True and result["always"] is False
    assert len(server._permission_rules) == 1

    # New generation, another harness, same branch descendant: rule survives.
    del server._generation_snapshots[key]
    key = (conv["id"], descendant["id"], 902)
    scope = "gen:902"
    server._generation_snapshots[key] = {"parent_id": descendant["id"]}
    result = await asyncio.wait_for(request("python -m pytest tests/b.py -q", "goose_acp"), 2)
    assert result["allow"] and result["policy"]["action"]["rule_matched"]

    for parent, command, workspace in [
        (sibling["id"], "python -m pytest tests/b.py -q", tmp_path),
        (descendant["id"], "python -m pytest tests/b.py -s", tmp_path),
        (descendant["id"], "python -m pytest tests/b.py -q", tmp_path / "other"),
    ]:
        await db.update_conversation_fields(conv["id"], project_dir=str(workspace))
        server._generation_snapshots[key] = {"parent_id": parent}
        pending_task = asyncio.create_task(request(command))
        rid, _ = await wait_pending(pending_task)
        await answer(conv["id"], rid, allow=False)
        assert not (await asyncio.wait_for(pending_task, 2))["allow"]

    rules = (await client.get(f"/api/conversations/{conv['id']}/permission-rules")).json()
    assert rules["lifetime"] == "server_session" and len(rules["rules"]) == 1
    rule_id = rules["rules"][0]["id"]
    assert (await client.delete(f"/api/conversations/{conv['id'] + 1}/permission-rules/{rule_id}")).status_code == 404
    assert (await client.delete(f"/api/conversations/{conv['id']}/permission-rules/{rule_id}")).status_code == 200
    assert not server._permission_rules


async def test_fuzzy_classifier_no_longer_denies_commands(tmp_path):
    conv = await db.create_conversation("Warning fixture", mode="goose", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], permission_profile="balanced")
    task = asyncio.create_task(server.handle_cc_permission({"loom_conv_id": conv["id"],
        "tool_name": "shell", "tool_input": {"command": "Remove-Item -Recurse generated"}}))
    rid, pending = await wait_pending(task)
    assert pending["policy"]["decision"] == "ask_with_warning"
    await answer(conv["id"], rid, allow=True)
    assert (await asyncio.wait_for(task, 2))["allow"]


async def test_profile_is_validated_and_persisted(client, tmp_path):
    conv = await db.create_conversation("Profiles", mode="codex", project_dir=str(tmp_path))
    route = f"/api/conversations/{conv['id']}"
    assert (await client.put(route, json={"permission_profile": "bogus"})).status_code == 400
    assert (await client.put(route, json={"permission_profile": "trusted"})).status_code == 200
    assert (await db.get_conversation(conv["id"]))["permission_profile"] == "trusted"
    reply = await server.handle_cc_permission({"loom_conv_id": conv["id"], "tool_name": "Bash",
                                             "tool_input": {"command": "node build.js"}})
    assert reply["allow"] and reply["always"] is False


def test_acp_grants_cannot_escape_to_native_session():
    assert policy.once_option([{"id": "remember", "kind": "allow_always"}], True) is None
    assert policy.once_option([{"id": "opaque", "kind": "allow_once"}], True) == "opaque"
    assert policy.once_option([{"id": "reject_always"}], False) is None


async def test_child_rule_uses_persisted_parent_and_actual_workspace(tmp_path):
    conv = await db.create_conversation("Child policy", mode="goose", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], permission_profile="balanced")
    root = await db.add_message(conv["id"], "user", "parent")
    draft = await db.add_message(conv["id"], "assistant", "partial", parent_id=root["id"])
    args = {"command": "node build.js --check"}
    candidate = permission_rules.action_rule(normalize(tmp_path, payload=args), args)
    server._permission_rules["fixture-rule"] = {"id": "fixture-rule", **candidate,
        "conversation_id": conv["id"], "branch_id": draft["id"],
        "workspace": permission_rules.workspace_identity(str(tmp_path))}
    run = await db.create_agent_run("fixture-child", conv["id"], "check",
        parent_message_id=draft["id"], project_dir=str(tmp_path))
    await db.update_agent_run(run["run_id"], status="running")
    payload = {"loom_conv_id": conv["id"], "agent_run_id": run["run_id"],
               "tool_name": "Bash", "tool_input": args}
    result = await asyncio.wait_for(server.handle_cc_permission(payload), 2)
    assert result["allow"] and result["policy"]["action"]["rule_matched"]
    # Run workspace ownership is immutable: use a distinct run, not an ignored
    # update_agent_run field, to exercise the foreign-workspace case.
    other = await db.create_agent_run("other-child", conv["id"], "check elsewhere",
        parent_message_id=draft["id"], project_dir=str(tmp_path / "different"))
    await db.update_agent_run(other["run_id"], status="running")
    payload["agent_run_id"] = other["run_id"]
    task = asyncio.create_task(server.handle_cc_permission(payload))
    rid, pending = await wait_pending(task)
    assert not pending["policy"]["action"]["rule_matched"]
    await server._resolve_pending_permissions_for_scope(conv["id"], "agent:other-child", message="Fixture stopped")
    assert (await asyncio.wait_for(task, 2))["decision_source"] == "loom_lifecycle"


def test_native_network_and_expanded_grants_cannot_match_execute(tmp_path):
    for extra in ({"networkApprovalContext": {"host": "example.invalid"}},
                  {"additionalPermissions": {"fileSystem": {"write": [str(tmp_path.parent)]}}}):
        action = normalize(tmp_path, payload={"command": "node build.js", **extra},
                           method="item/commandExecution/requestApproval")
        assert policy.decide(policy.PermissionProfile.TRUSTED, action) is policy.PolicyDecision.ASK
        assert permission_rules.action_rule(action, extra) is None


def test_changed_environment_does_not_reuse_a_rule(tmp_path):
    def candidate(value):
        args = {"command": "node build.js", "env": {"DESTINATION": value}}
        return permission_rules.action_rule(normalize(tmp_path, payload=args), args)
    assert candidate("review")["shape"] != candidate("publish")["shape"]


async def test_strict_write_asks_unknown_title_does_not_auto_allow(tmp_path):
    conv = await db.create_conversation("Strict fixture", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], permission_profile="strict")
    for name, payload in [("Write", {"file_path": "notes.txt"}),
                          ("Read and publish report", {"path": "notes.txt"})]:
        task = asyncio.create_task(server.handle_cc_permission({"loom_conv_id": conv["id"],
            "tool_name": name, "tool_input": payload}))
        rid, pending = await wait_pending(task)
        assert pending["policy"]["decision"] == "ask"
        await answer(conv["id"], rid, allow=False)
        assert (await asyncio.wait_for(task, 2))["decision_source"] == "user"


async def test_reconnect_restores_rule_preview(tmp_path):
    conv = await db.create_conversation("Reconnect fixture", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], permission_profile="balanced")
    root = await db.add_message(conv["id"], "user", "check")
    server._generation_snapshots[(conv["id"], root["id"], 903)] = {"parent_id": root["id"]}
    task = asyncio.create_task(server.handle_cc_permission({"loom_conv_id": conv["id"],
        "permission_scope": "gen:903", "tool_name": "Bash", "tool_input": {"command": "node build.js"}}))
    rid, pending = await wait_pending(task)
    class Socket:
        events = []
        async def send_json(self, payload):
            self.events.append(payload)
    socket = Socket()
    server._event_router.register(socket, conv["id"])
    server._event_router.subscribe(socket, "gen:903")
    try:
        await server._send_pending_permissions_to_socket(socket, conv["id"])
        event = next(e for e in socket.events if e.get("request_id") == rid)
        assert event["rule_preview"] == pending["rule_preview"]
        assert event["supports_allow_all"]
        await answer(conv["id"], rid, allow=False)
        await asyncio.wait_for(task, 2)
    finally:
        server._event_router.unregister(socket)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
