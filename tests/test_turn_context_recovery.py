"""All ordinary harnesses: real handlers/DB/planner, synthetic native events."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import context_checkpoints
import database as db
import server
import turn_recovery
from tests.test_continuity_launch_matrix import MODELS, setup, run


def test_recovery_launch_guard_matches_lean_exhaustively():
    cases = json.loads((Path(__file__).parents[1] / "formal/coordination/generated/recovery.json").read_text())
    assert len(cases) == 16
    for args, expected in cases:
        assert turn_recovery.may_launch(*args) == expected
        assert not turn_recovery.may_launch(*args[:3], True)


@pytest.mark.parametrize("harness", ["goose", "gemini"])
@pytest.mark.parametrize("status,recorded", [("in_progress", False), ("", False), ("completed", True), ("failed", True)])
def test_adapter_progress_is_not_a_recorded_outcome(harness, status, recorded):
    import goose_client
    import gemini_client
    if harness == "goose":
        events = goose_client.dispatch_session_update({"sessionUpdate":"tool_call_update",
            "toolCallId":"t1", "status":status, "content":{"text":"output so far"}}, {})
    else:
        events = gemini_client._agy_step_events({"event":"step_update", "step_update":{
            "step_index":1, "state":status, "tool_info":{"name":"run_command", "output":"output so far"}}}, {})
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["outcome_recorded"] is recorded
    assert result["is_error"] is (status == "failed")


async def test_unavailable_summary_is_explicit_and_never_cloud_fallback(monkeypatch):
    import httpx
    import context_runtime
    import local_context_guard
    from continuity_engine import ContextLimit
    guard = AsyncMock()
    guard.upstream = "http://synthetic-local"
    guard.capacity.side_effect = httpx.ConnectError("private transport detail")
    monkeypatch.setattr(local_context_guard, "LocalContextGuard", lambda *args: guard)
    with pytest.raises(ContextLimit, match="no cloud fallback") as error:
        await context_runtime.ConfiguredLocalSummarizer().summarize("private material", 1000)
    assert "private" not in str(error.value)
    guard.close.assert_awaited_once()


@pytest.mark.parametrize("harness", MODELS)
@pytest.mark.parametrize("outcome", ["completed", "empty_result", "unresolved", "progress_only", "error", "success_text", "second_overflow", "stream_exception", "id_reuse"])
async def test_boundary_recovery_preserves_actions_and_is_bounded(tmp_database, tmp_path, monkeypatch, harness, outcome):
    conv, current, calls = await setup(monkeypatch, tmp_path, harness, harness)
    branch = await db.get_branch_to_root(current["id"])
    await db.update_message_content(branch[0]["id"], content="Keep synthetic key private; finish grading.")
    client = getattr(server, {"claude":"claude_client", "codex":"codex_client", "gemini":"gemini_client", "goose":"goose_client"}[harness])
    method = "run_" + harness
    normal = getattr(client, method)
    mutations, receipts = [], []
    async def launch(prompt, *args, **kwargs):
        proc, _ = await normal(prompt, *args, **kwargs)
        attempt = len(calls)
        if attempt == 2:
            assert kwargs.get("resume_session_id") is None
            assert '"outcome_recorded":true' in prompt
            assert "saved-once" in prompt or outcome == "empty_result"
            assert "Do not repeat completed actions" in prompt
            token = prompt.split("context_scope:", 1)[1].split()[0]
            leaf = await db.get_active_leaf(conv["id"])
            record = await context_checkpoints.retrieve(token, conv["id"], message_id=leaf["id"])
            receipts.append(record)
        async def events():
            yield {"type":"session_info", "session_id":f"attempt-{attempt}", "model":MODELS[harness]}
            if attempt == 1:
                yield {"type":"text_delta", "text":"Existing partial work.\n"}
                yield {"type":"tool_start", "tool_id":"write-1", "name":"Write"}
                mutations.append("write")
                if outcome != "unresolved":
                    yield {"type":"tool_result", "tool_id":"write-1", "content":"" if outcome == "empty_result" else "saved-once",
                           "is_error":outcome == "error", "outcome_recorded":outcome != "progress_only"}
            else:
                if outcome == "id_reuse":
                    yield {"type":"tool_start", "tool_id":"write-1", "name":"Read"}
                    yield {"type":"tool_result", "tool_id":"write-1", "content":"read-only continuation"}
                yield {"type":"text_delta", "text":"Continued without another write."}
            if attempt == 1 and outcome == "stream_exception":
                raise RuntimeError("context window exceeded")
            fail = outcome != "success_text" and (attempt == 1 or outcome == "second_overflow")
            yield {"type":"result", "is_error":fail, "terminal_confirmed":True,
                   "error":"context window exceeded" if fail else "",
                   "result_text":"context window exceeded" if fail or outcome == "success_text" else "Done",
                   "session_id":f"attempt-{attempt}"}
        return proc, events()
    monkeypatch.setattr(client, method, launch)
    leaf = await run(conv, current["id"], harness)
    expected = 1 if outcome in {"unresolved", "progress_only", "error", "success_text"} else 2
    assert len(calls) == expected and mutations == ["write"]
    assert leaf["parent_id"] == current["id"]
    starts = [call.args[1] for call in server._ws_send.call_args_list if call.args[1].get("type") == "stream_start"]
    assert len(starts) == 1 and starts[0]["parent_id"] == current["id"]
    assert leaf["content"].startswith("Existing partial work.")
    blocks = json.loads(leaf["content_blocks"])
    tools = [b for b in blocks if b["type"] == "tool_use"]
    assert len(tools) == (2 if outcome == "id_reuse" else 1)
    if outcome == "id_reuse":
        assert tools[0]["tool_id"] != tools[1]["tool_id"]
        assert tools[0]["result"] == "saved-once"
        assert tools[1]["result"] == "read-only continuation"
    if expected == 2:
        assert receipts
        assert leaf["cc_session_id"] == "attempt-2"
        if outcome != "second_overflow":
            # Direct handler fixtures retain the legacy NULL success value;
            # the generation/lease wrapper supplies the final lifecycle state.
            assert leaf["turn_lifecycle_state"] in {None, "completed"}
            assert "Continued without another write." in leaf["content"]
            follow = await db.add_message(conv["id"], "user", "Continue", parent_id=leaf["id"])
            monkeypatch.setattr(client, method, normal)
            await run(conv, follow["id"], harness)
            assert calls[-1][1].get("resume_session_id") == "attempt-2"
    if outcome in {"unresolved", "progress_only", "error", "second_overflow"}:
        assert leaf["turn_lifecycle_state"] in {"failed", "partial_failed"}


@pytest.mark.parametrize("harness", MODELS)
async def test_failed_teardown_never_launches_replacement(tmp_database, tmp_path, monkeypatch, harness):
    conv, current, calls = await setup(monkeypatch, tmp_path, harness, harness)
    client = getattr(server, harness + "_client")
    method = "run_" + harness
    normal = getattr(client, method)
    async def launch(*args, **kwargs):
        proc, _ = await normal(*args, **kwargs)
        async def events():
            yield {"type":"tool_start", "tool_id":"write", "name":"Write"}
            yield {"type":"tool_result", "tool_id":"write", "content":"saved"}
            yield {"type":"result", "is_error":True, "error":"context window exceeded"}
        return proc, events()
    monkeypatch.setattr(client, method, launch)
    monkeypatch.setattr(server, "_teardown_child_provider", AsyncMock(side_effect=RuntimeError("exit unverified")))
    leaf = await run(conv, current["id"], harness)
    assert len(calls) == 1
    assert "saved" in leaf["content_blocks"]


def test_no_identity_no_outcome_or_retry_budget_cannot_authorize_recovery():
    complete = {"type":"tool_use", "tool_id":"a", "outcome_recorded":True}
    assert turn_recovery.decide("context window exceeded", [complete]).allowed
    assert not turn_recovery.decide("context window exceeded", [complete], attempts=1).allowed
    assert not turn_recovery.decide("context window exceeded", [complete], enabled=False).allowed
    assert not turn_recovery.decide("context window exceeded", [{**complete, "tool_id":""}]).allowed
    assert not turn_recovery.decide("upstream timeout", [complete]).allowed


@pytest.mark.parametrize("edit", ["append", "ancestor", "tool", "text", "foreign", "expired_owner"])
async def test_frozen_attempt_scope_is_readable_but_cannot_expand(tmp_database, edit):
    from continuity_engine import BranchSnapshot
    conv = await db.create_conversation("Synthetic recovery evidence")
    user = await db.add_message(conv["id"], "user", "Task")
    blocks = [{"type":"tool_use", "tool_id":"a", "result":"saved", "outcome_recorded":True}]
    draft = await db.add_message(conv["id"], "assistant", "Partial", parent_id=user["id"])
    await db.update_message_content(draft["id"], content_blocks=json.dumps(blocks))
    snapshot = BranchSnapshot.from_messages(await db.get_branch_to_root(draft["id"]))
    owner = asyncio.create_task(asyncio.Event().wait())
    try:
        token = await context_checkpoints.issue_retrieval_scope(snapshot, owner=owner, frozen_tail=True)
        await db.update_message_content(draft["id"], content="Partial plus new output",
            content_blocks=json.dumps([*blocks, {"type":"text", "text":"NEW_SECRET"}]), cc_session_id="new-session")
        if edit == "ancestor":
            await db.update_message_content(user["id"], content="Edited")
        if edit == "tool":
            await db.update_message_content(draft["id"], content_blocks='[]')
        if edit == "text":
            await db.update_message_content(draft["id"], content="Replaced")
        if edit == "expired_owner":
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
        if edit == "append":
            record = await context_checkpoints.retrieve(token, conv["id"], message_id=draft["id"])
            assert "saved" in record["record_fragment"]
            assert "NEW_SECRET" not in record["record_fragment"]
        else:
            with pytest.raises(PermissionError):
                await context_checkpoints.retrieve(token, conv["id"] + (1 if edit == "foreign" else 0), message_id=draft["id"])
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
