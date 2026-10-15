"""ExitPlanMode through the permission hook.

Headless CC cannot exit plan mode in-session: even after a PreToolUse allow,
CC's built-in plan prompt cannot render in -p mode and auto-denies with the
bare title "Exit plan mode?" (reproduced 2026-06-11, CC 2.1.173, with an
unconditional auto-allow hook). The hook therefore translates the user's
Loom decision into an explicit message instead of passing allow through —
approval reads as approval, not as a cryptic error.
"""

import asyncio
import io
import json
import sys

import pytest


class _FakeResponse:
    def __init__(self, body=b"{}"):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def _run_hook(monkeypatch, tool_name, loom_allows, decision_source="user"):
    import cc_permission_hook

    def fake_urlopen(req, timeout=None, context=None):
        if req.full_url.endswith("/api/cc-permission"):
            return _FakeResponse(json.dumps({"allow": loom_allows, "decision_source": decision_source}).encode("utf-8"))
        return _FakeResponse()

    stdin = io.StringIO(json.dumps({
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"plan": "test plan"},
    }))
    stdout = io.StringIO()

    monkeypatch.setenv("LOOM_CONV_ID", "42")
    monkeypatch.setenv("LOOM_PORT", "3000")
    pass
    monkeypatch.delenv("LOOM_BACKSTAGE_PARENT_ID", raising=False)
    monkeypatch.setattr(sys, "argv", ["cc_permission_hook.py", "--event", "PreToolUse"])
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(cc_permission_hook.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(SystemExit):
        cc_permission_hook.main()

    return json.loads(stdout.getvalue())["hookSpecificOutput"]


def test_plan_approval_reads_as_approval_not_cryptic_error(monkeypatch):
    out = _run_hook(monkeypatch, "ExitPlanMode", loom_allows=True)
    # Deny at the hook level (CC would auto-deny anyway), but with a message
    # the model reads as approval + next-turn instructions.
    assert out["permissionDecision"] == "deny"
    assert "APPROVED" in out["permissionDecisionReason"]
    assert "do not retry ExitPlanMode" in out["permissionDecisionReason"]


def test_plan_revision_request_says_stay_in_plan_mode(monkeypatch):
    out = _run_hook(monkeypatch, "ExitPlanMode", loom_allows=False)
    assert out["permissionDecision"] == "deny"
    assert "revisions" in out["permissionDecisionReason"]
    assert "stay in plan" in out["permissionDecisionReason"]


def test_non_plan_tools_still_pass_allow_through(monkeypatch):
    out = _run_hook(monkeypatch, "Write", loom_allows=True)
    assert out["permissionDecision"] == "allow"


@pytest.mark.parametrize("source", ["loom_policy", "loom_lifecycle", "loom_timeout", "bridge_error"])
def test_plan_cancellation_does_not_impersonate_user_revisions(monkeypatch, source):
    out = _run_hook(monkeypatch, "ExitPlanMode", loom_allows=False, decision_source=source)
    assert "without a user decision" in out["permissionDecisionReason"]
    assert "revisions" not in out["permissionDecisionReason"]


def test_read_hook_consults_shared_policy_instead_of_auto_allowing(monkeypatch):
    out = _run_hook(monkeypatch, "Read", loom_allows=False, decision_source="loom_lifecycle")
    assert out["permissionDecision"] == "deny"
    assert "user" not in out["permissionDecisionReason"].lower()


@pytest.mark.parametrize("tool_name", ["ExitPlanMode", "exit_plan_mode"])
async def test_exit_plan_mode_not_auto_allowed_under_trust_review(tmp_path, monkeypatch, tool_name):
    import database as db
    import server

    conv = await db.create_conversation("Plan trust review test", mode="claude", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], permission_profile="trust_review")
    root = await db.add_message(conv["id"], "user", "plan test")
    draft = await db.add_message(conv["id"], "assistant", "plan test", parent_id=root["id"])
    monkeypatch.setattr(server, "_generation_snapshots", {
        (conv["id"], root["id"], 8844): {"parent_id": root["id"], "draft_msg_id": draft["id"]},
    })
    monkeypatch.setattr(server, "_pending_hook_permissions", {})

    # Normal write is auto-allowed under trust_review
    res_write = await server.handle_cc_permission({
        "loom_conv_id": conv["id"],
        "permission_scope": "gen:8844",
        "tool_name": "Write",
        "tool_input": {"file_path": str(tmp_path / "test.txt"), "content": "hello"},
    })
    assert res_write.get("allow") is True
    assert res_write.get("decision_source") == "loom_policy"

    # ExitPlanMode is NEVER auto-allowed by policy even under trust_review — it creates a pending permission request
    task = asyncio.create_task(server.handle_cc_permission({
        "loom_conv_id": conv["id"],
        "permission_scope": "gen:8844",
        "tool_name": tool_name,
        "tool_input": {"plan": "My Plan"},
    }))
    try:
        async with asyncio.timeout(2):
            while not server._pending_hook_permissions:
                await asyncio.sleep(0.01)
            assert not task.done()
            req_id = next(iter(server._pending_hook_permissions))
            assert server._pending_hook_permissions[req_id]["tool_name"] == tool_name
            assert await server._answer_pending_permission(req_id, {"allow": True}) is True
            res_plan = await task
            assert res_plan.get("allow") is True
            assert res_plan.get("decision_source") == "user"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
