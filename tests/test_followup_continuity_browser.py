"""Completed turns must stay usable for both follow-ups and edited branches."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

import database as db
import context_manager
import server
from tests.test_queue_presentation_browser import queue_browser, playwright
from tests.test_fullstack_queue_isolation import _eventually


@pytest.mark.parametrize("recover_prior", [False, True])
@pytest.mark.parametrize("delay_branch", [False, True])
async def test_followup_then_edited_branch_launch_and_land_without_reload(queue_browser, monkeypatch, recover_prior, delay_branch):
    page, conv, assistant, queued = queue_browser

    async def page_leaf(expected_content):
        # The legacy conversation-wide is_active flags are not this page's
        # cursor. A delayed switch response or another window may update them.
        await page.evaluate("async () => { if (State._cursorSyncPromise) await State._cursorSyncPromise; }")
        selected = await page.evaluate("({page: _loomClientInstanceId, leaf: State.loomCursor.leaf_message_id})")
        persisted = await db.get_client_loom_cursor(conv["id"], selected["page"])
        assert persisted["leaf_message_id"] == selected["leaf"]
        leaf = await db.get_message(persisted["leaf_message_id"])
        assert leaf["role"] == "assistant" and leaf["content"] == expected_content
        return leaf

    await db.delete_queued_generation(queued["id"])
    await db.update_conversation_fields(conv["id"], cc_model="codex-gpt-5.6-sol")
    await db.update_message_content(assistant["id"], cc_session_id="native-original", cc_session_mode="codex",
                                    cc_model_used="gpt-5.6-sol", generation_ms=10)
    if recover_prior:
        await db.update_message_content(assistant["id"], turn_lifecycle_state="partial_failed", model_attestation=json.dumps({
            "status": "verified", "session_recovery": {
                "version": 1, "terminal_confirmed": True, "can_resume": False,
                "session_id": "native-original", "session_mode": "codex",
            },
        }))
    monkeypatch.setattr(server.codex_client, "read_codex_last_turn", AsyncMock(return_value={
        "id": "completed-source", "status": "completed", "completedAt": assistant["created_at"] + 1,
        "startedAt": int(assistant["created_at"]),
        "source": "codex_rollout_task_complete",
    }))
    monkeypatch.setattr(context_manager, "update_rolling_summary", AsyncMock())
    async def forbidden_provider(*args, **kwargs):
        raise AssertionError("This isolated fixture must never launch another provider")
    monkeypatch.setattr(server.claude_client, "run_claude", forbidden_provider)
    monkeypatch.setattr(server.gemini_client, "run_gemini", forbidden_provider)
    monkeypatch.setattr(server.goose_client, "run_goose", forbidden_provider)
    calls = []
    captured_branch = asyncio.Event()

    class Proc:
        pid = 999_989
        returncode = 0
        async def wait(self):
            return 0

    async def run(prompt, *args, **kw):
        sequence = len(calls) + 1
        calls.append((prompt, kw.get("resume_session_id")))
        async def events():
            yield {"type": "session_info", "session_id": f"native-{sequence}", "model": "gpt-5.6-sol"}
            yield {"type": "tool_start", "name": "Read", "tool_id": "source"}
            yield {"type": "tool_result", "tool_id": "source", "content": "runner pipe-in: windows sandbox: timed out"}
            yield {"type": "text_delta", "text": f"Finished fixture {sequence}."}
            if sequence == 2 and delay_branch:
                await asyncio.wait_for(captured_branch.wait(), timeout=5)
            else:
                await asyncio.sleep(.1)
            yield {"type": "result", "session_id": f"native-{sequence}", "is_error": False,
                   "terminal_confirmed": True, "result_text": f"Finished fixture {sequence}."}
        return Proc(), events()

    monkeypatch.setattr(server.codex_client, "run_codex", run)
    await page.evaluate("""() => {
        State.currentConv.cc_model = 'codex-gpt-5.6-sol';
        State.currentConv.mode = 'codex';
        const picker = document.getElementById('cc-model-inline');
        picker.add(new Option('Fixture Codex', 'codex-gpt-5.6-sol'));
        picker.value = 'codex-gpt-5.6-sol';
    }""")
    await page.evaluate("async id => { await switchToBranch(id, id, {exact:true}); await _refreshQueuedGeneration(); }", assistant["id"])
    await page.locator("#user-input").fill("Follow-up fixture")
    await page.evaluate("sendMessage()")
    await page.wait_for_function("!State.isStreaming && State.messages.some(m => m.content === 'Finished fixture 1.')")
    first = await page_leaf("Finished fixture 1.")
    first_user = await db.get_message(first["parent_id"])
    assert first_user["parent_id"] == assistant["id"]
    assert calls[0][1] == "native-original"
    await _eventually(lambda: not server._active_generations)

    await page.evaluate("""() => {
        window.followupEvents = [];
        const original = handleWSMessage;
        window.handleWSMessage = function(data) {
            followupEvents.push({type:data.type, message:data.message?.content});
            return original(data);
        };
    }""")
    async def delayed_branch_snapshot(route):
        response = await route.fetch()
        captured_branch.set()
        # Capture the branch before provider completion, but deliver it after
        # terminal WS evidence. This used to repaint the landed reply as blank.
        await page.wait_for_function("followupEvents.some(e => e.type === 'stream_end' && e.message === 'Finished fixture 2.')")
        await route.fulfill(response=response)
    if delay_branch:
        await page.route(f"**/api/conversations/{conv['id']}/switch-branch/*", delayed_branch_snapshot)
    await page.evaluate("id => editMessage(id)", first_user["id"])
    await page.locator(".edit-message-input").fill("Edited follow-up fixture")
    await page.locator(".edit-save").click()
    try:
        await page.wait_for_function("!State.isStreaming && State.messages.some(m => m.content === 'Finished fixture 2.')")
    except Exception:
        print(await page.evaluate("""() => ({streaming:State.isStreaming,following:State._followingGenId,
            draft:State._followingDraftMsgId,reconstructing:State._reconstructing,
            messages:State.messages.map(m=>({id:m.id,content:m.content,parent:m.parent_id})),
            cursor:State.loomCursor, terminal:[..._recentTerminalRuns],events:followupEvents,
            body:document.getElementById('messages').innerText})"""))
        raise
    await playwright.expect(page.locator("#loading-overlay")).not_to_be_visible()
    assert await page.evaluate("!State.isStreaming && State.messages.some(m => m.content === 'Finished fixture 2.' && m.generation_ms != null)")
    # Model another window changing only the legacy shared selection. The
    # edited branch and the subsequent send must still follow this page.
    await db.set_active_branch(conv["id"], assistant["id"])
    second = await page_leaf("Finished fixture 2.")
    edited = await db.get_message(second["parent_id"])
    assert edited["parent_id"] == assistant["id"], "edited prompt must be a sibling, not follow the discarded answer"
    assert calls[1][1] == "native-original", "the edited branch resumes its selected ancestor"
    assert first["id"] != second["id"]
    assert all(len(prompt) < 1000 for prompt, _ in calls)
    if recover_prior:
        assert all("verified as completed" in prompt for prompt, _ in calls)
    assert not await page.locator(".stream-thinking-footer").count()
    await _eventually(lambda: not server._active_generations)
    # A settled-looking edit is insufficient: its cursor must accept another
    # ordinary follow-up without a refresh or a hidden stale-revision failure.
    await page.locator("#user-input").fill("Continue the edited fixture")
    await page.evaluate("sendMessage()")
    await page.wait_for_function("!State.isStreaming && State.messages.some(m => m.content === 'Finished fixture 3.')")
    third = await page_leaf("Finished fixture 3.")
    third_user = await db.get_message(third["parent_id"])
    assert third_user["parent_id"] == second["id"]
    assert calls[2][1] == "native-2"
    await _eventually(lambda: not server._active_generations)


async def test_navigation_merge_does_not_follow_siblings_or_another_workspace(queue_browser):
    page, conv, assistant, queued = queue_browser
    assert await page.evaluate("""convId => {
        const branch = [{id:201,parent_id:100,content:'selected user'}];
        const child = {id:202,parent_id:201,content:'landed',generation_ms:42};
        const sibling = {id:203,parent_id:201,content:'foreign sibling',generation_ms:99};
        State.messages = [...branch, child, sibling];
        State._liveStreamDraft = null;
        State._followingDraftMsgId = null;
        _recentTerminalRuns.set(`${convId}/gen:50`, child.id);
        _recentTerminalRuns.set(`${convId}/gen:51`, sibling.id);
        const stale = [...branch, {...child, content:'', generation_ms:null}];
        return _reconcileNavigatedBranch(stale, convId).at(-1).generation_ms === 42
            && _reconcileNavigatedBranch([...branch, {...child, generation_ms:43}], convId).at(-1).generation_ms === 43
            && _reconcileNavigatedBranch(branch, convId).length === 1
            && _reconcileNavigatedBranch(branch, convId, ['gen:50']).at(-1).id === child.id
            && _reconcileNavigatedBranch(branch, convId + 1, ['gen:50']).length === 1;
    }""", conv["id"])
