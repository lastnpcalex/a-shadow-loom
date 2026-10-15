"""Sanitized Goose 1.45 wire shapes and Windows/Codex permission regressions."""
from copy import deepcopy
from pathlib import Path
import json

import pytest

from acp_permissions import permission_action, remember_tool_call
from permission_policy import normalize_permission_request, decide, PermissionProfile
from permission_rules import action_rule
from permission_commands import display_argv


def goose_wire(tool, args, call_id="fixture-call"):
    # The initial notification, unlike request_permission, includes identity.
    initial = {"sessionUpdate": "tool_call", "toolCallId": call_id,
               "title": "A presentation title, not authorization", "kind": "other",
               "status": "pending", "rawInput": deepcopy(args),
               "_meta": {"goose": {"toolCall": {"toolName": f"developer__{tool}", "extensionName": "developer"}}}}
    request = {"sessionId": "fixture-session", "toolCall": {
        "toolCallId": call_id, "title": "Read something (not authoritative)",
        "kind": "other", "status": "pending", "rawInput": deepcopy(args)},
        "options": [{"optionId": "allow_once", "kind": "allow_once"},
                    {"optionId": "reject_once", "kind": "reject_once"}]}
    return initial, request


@pytest.mark.parametrize("extension,name,expected", [
    ("loom-agent-control", "retrieve_context", "auto_allow"),
    ("loom-agent-control", "loom-agent-control__retrieve_context", "auto_allow"),
    ("unrelated", "retrieve_context", "ask"),
    ("loom-agent-control", "spawn_agent", "ask"),
])
def test_scoped_retrieval_is_read_only_only_for_exact_mcp_identity(tmp_path, extension, name, expected):
    initial, request = goose_wire("unused", {"token": "synthetic-token", "message_id": 1})
    initial["_meta"]["goose"]["toolCall"] = {"toolName": name, "extensionName": extension}
    state = {}
    remember_tool_call(state, initial)
    tool, args = permission_action(request, state=state, cwd=str(tmp_path))
    action = normalize_permission_request(tool_name=tool, tool_input=args, workspace_root=str(tmp_path))
    assert decide(PermissionProfile.BALANCED, action).value == expected


def test_scoped_retrieval_hook_and_mismatched_acp_payload(tmp_path):
    action = normalize_permission_request(tool_name="mcp__loom-agent-control__retrieve_context",
                                         tool_input={"token": "synthetic"}, workspace_root=str(tmp_path))
    assert decide(PermissionProfile.BALANCED, action).value == "auto_allow"
    initial, request = goose_wire("unused", {"token": "first"})
    initial["_meta"]["goose"]["toolCall"] = {"toolName": "retrieve_context", "extensionName": "loom-agent-control"}
    request["toolCall"]["rawInput"] = {"token": "different"}
    state = {}
    remember_tool_call(state, initial)
    tool, args = permission_action(request, state=state, cwd=str(tmp_path))
    action = normalize_permission_request(tool_name=tool, tool_input=args, workspace_root=str(tmp_path))
    assert decide(PermissionProfile.BALANCED, action).value == "ask"


@pytest.mark.parametrize("tool,args,effect,balanced,trusted", [
    ("read", {"path": "seed.txt"}, "read", "auto_allow", "auto_allow"),
    ("tree", {"path": "."}, "read", "auto_allow", "auto_allow"),
    ("write", {"path": "output.txt", "content": "fixture"}, "workspace_write", "auto_allow", "auto_allow"),
    ("edit", {"path": "output.txt", "before": "fixture", "after": "updated"}, "workspace_write", "auto_allow", "auto_allow"),
    ("shell", {"command": "git status --short"}, "execute", "ask", "auto_allow"),
])
def test_goose_real_wire_identity_survives_permission_update(tmp_path, tool, args, effect, balanced, trusted):
    import goose_client
    initial, request = goose_wire(tool, args)
    state = {}
    events = goose_client.dispatch_session_update(initial, state)
    assert any(e["type"] == "tool_input_delta" for e in events)
    assert json.loads(next(e["json"] for e in events if e["type"] == "tool_input_delta")) == args
    name, payload = permission_action(request, state=state, cwd=str(tmp_path))
    action = normalize_permission_request(tool_name=name, tool_input=payload, workspace_root=str(tmp_path))
    assert action.effect.value == effect
    assert decide(PermissionProfile.BALANCED, action).value == balanced
    assert decide(PermissionProfile.TRUSTED, action).value == trusted
    assert action_rule(action, payload) is not None
    goose_client.dispatch_session_update({"sessionUpdate": "tool_call_update", "toolCallId": "fixture-call", "status": "completed"}, state)
    assert state["permission_tool_calls"] == {}


@pytest.mark.parametrize("mutation", ["no_initial", "different_args", "other_extension", "other_id", "title_only", "missing_path"])
def test_goose_uncertain_identity_cannot_get_an_allow_or_rule(tmp_path, mutation):
    initial, request = goose_wire("read", {"path": "seed.txt"})
    if mutation == "different_args": request["toolCall"]["rawInput"] = {"command": "publish"}
    if mutation == "other_extension": initial["_meta"]["goose"]["toolCall"]["extensionName"] = "unrelated"
    if mutation == "other_id": request["toolCall"]["toolCallId"] = "other"
    if mutation == "title_only": initial.pop("_meta")
    if mutation == "missing_path":
        initial["rawInput"] = request["toolCall"]["rawInput"] = {}
    state = {}
    if mutation != "no_initial": remember_tool_call(state, initial)
    name, payload = permission_action(request, state=state, cwd=str(tmp_path))
    action = normalize_permission_request(tool_name=name, tool_input=payload, workspace_root=str(tmp_path))
    assert decide(PermissionProfile.TRUSTED, action).value == "ask"
    assert action_rule(action, payload) is None


def test_goose_outside_paths_and_cross_run_cache_still_ask(tmp_path):
    initial, request = goose_wire("write", {"path": str(tmp_path.parent / "outside.txt"), "content": "fixture"})
    state = {}; remember_tool_call(state, initial)
    name, payload = permission_action(request, state=state, cwd=str(tmp_path))
    action = normalize_permission_request(tool_name=name, tool_input=payload, workspace_root=str(tmp_path))
    assert action.boundary.value == "outside_workspace"
    assert action_rule(action, payload) is None
    assert permission_action(request, state={}, cwd=str(tmp_path))[0] == "Unknown"


@pytest.mark.parametrize("harness", ["goose", "hermes"])
def test_malformed_acp_identity_cannot_crash_or_auto_allow(harness):
    call = {"name": [], "kind": [], "rawInput": {},
            "_meta": {"goose": {"toolCall": {"extensionName": "developer", "toolName": []}}}}
    assert permission_action({"toolCall": call}, harness=harness)[0] == "Unknown"


def test_shared_normalizer_never_uses_acp_title_as_identity(tmp_path):
    action = normalize_permission_request(tool_name="Unknown", workspace_root=str(tmp_path),
        tool_input={"toolCall": {"title": "Read", "kind": "other", "rawInput": {"path": "seed.txt"}}})
    assert decide(PermissionProfile.TRUSTED, action).value == "ask"


@pytest.mark.parametrize("kind,args", [
    ("search", {"query": "fixture query"}),
    ("read", {"path": "https://example.invalid/fixture.txt"}),
    ("search", {"path": "https://example.invalid/", "query": "fixture"}),
])
def test_acp_unverified_search_and_url_paths_cannot_auto_allow(tmp_path, kind, args):
    name, payload = permission_action({"toolCall": {"kind": kind, "rawInput": args}}, cwd=str(tmp_path))
    action = normalize_permission_request(tool_name=name, tool_input=payload, workspace_root=str(tmp_path))
    assert decide(PermissionProfile.TRUSTED, action).value == "ask"
    assert action_rule(action, payload) is None


def test_hermes_typed_json_input_uses_same_decoder(tmp_path):
    name, payload = permission_action({"toolCall": {"name": "edit_file", "kind": "other",
        "rawInput": '{"path":"fixture.txt","new_text":"fixture"}'}}, cwd=str(tmp_path), harness="hermes")
    assert name == "Edit"
    action = normalize_permission_request(tool_name=name, tool_input=payload, workspace_root=str(tmp_path))
    assert decide(PermissionProfile.BALANCED, action).value == "auto_allow"


def test_codex_array_command_preserves_arguments_and_rule(tmp_path):
    from codex_client import _app_permission_payload
    argv = ["python", "-m", "pytest", "tests/a file.py", "-q"]
    name, payload = _app_permission_payload("item/commandExecution/requestApproval", {"command": argv, "cwd": str(tmp_path)})
    assert payload["command_argv"] == argv
    assert payload["command"] == display_argv(argv)
    action = normalize_permission_request(tool_name=name, tool_input=payload, workspace_root=str(tmp_path), harness="codex_app_server")
    assert action_rule(action, payload) is not None
    assert action.audit_dict()["harness"] == "codex_app_server"


def test_claude_native_powershell_tool_is_execution_with_a_scoped_rule(tmp_path):
    # Sanitized actual Claude Code/OpenRouter hook payload on Windows.
    payload = {"command": "echo permission-fixture", "description": "Print fixture"}
    action = normalize_permission_request(tool_name="PowerShell", tool_input=payload,
        workspace_root=str(tmp_path), harness="claude_code")
    assert action.effect.value == "execute"
    assert decide(PermissionProfile.BALANCED, action).value == "ask"
    assert decide(PermissionProfile.TRUSTED, action).value == "auto_allow"
    assert action_rule(action, payload) is not None


def test_automatic_approval_is_not_extra_model_context():
    from codex_client import _permission_feedback_text
    assert _permission_feedback_text({"allow": True, "decision_source": "loom_policy", "message": "Allowed by Loom branch rule."}, allow=True) == ""
    assert "reviewed" in _permission_feedback_text({"allow": True, "decision_source": "user", "feedback": "Use the reviewed target."}, allow=True)
    code = Path("codex_client.py").read_text(encoding="utf-8")
    assert 'Approval request params:' not in code
    assert 'Approval response payload:' not in code


def windows_rule(root, script, flags=("-NoProfile",)):
    argv = ["powershell.exe", *flags, "-Command", script]
    payload = {"command": display_argv(argv), "command_argv": argv, "cwd": str(root)}
    action = normalize_permission_request(tool_name="Bash", tool_input=payload, workspace_root=str(root))
    return action_rule(action, payload)


def test_windows_single_invocation_has_a_scoped_visible_template(tmp_path):
    a = windows_rule(tmp_path, "python -m pytest tests/a.py -q")
    b = windows_rule(tmp_path, "python -m pytest tests/b.py -q")
    assert a and a["shape"] == b["shape"]
    assert "<workspace-file>" in a["description"] and "powershell.exe" in a["description"]
    assert a["shape"] != windows_rule(tmp_path, "python -m pytest tests/b.py -s")["shape"]
    assert a["shape"] != windows_rule(tmp_path, "python -m pytest tests/b.py -q", flags=())["shape"]
    assert windows_rule(tmp_path, "Get-Content -LiteralPath src/a.txt")["shape"] == windows_rule(tmp_path, "Get-Content -LiteralPath src/b.txt")["shape"]


@pytest.mark.parametrize("script", ["Get-Content ../outside.txt; Get-Item .", "$env:TARGET='outside'; node app.js",
    "python -m pytest $(whoami)", "git status | Out-String", "& { git status }", "python -c 'print(1)'",
    "Get-Content -LiteralPath ../outside.txt", "powershell -EncodedCommand Zml4dHVyZQ=="])
def test_windows_templates_do_not_approve_programs_or_escaping_operands(tmp_path, script):
    assert windows_rule(tmp_path, script) is None
