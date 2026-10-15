"""Exercise the actual /compact composer command, socket, lease and UI."""
import asyncio
import json

import pytest

import context_runtime
import database as db
import server
from continuity_engine import SummaryResult
from tests.test_lifecycle_handoff_browser import lifecycle_browser
from tests.test_fullstack_queue_isolation import _eventually


async def test_browser_manual_checkpoint_preserves_view_and_followup(lifecycle_browser, monkeypatch):
    page, conv, client, workspace = lifecycle_browser
    calls = []
    entered, finish = asyncio.Event(), asyncio.Event()

    class Summary:
        identity = 'synthetic-local'
        def __init__(self, status=None):
            pass
        async def summarize(self, text, max_tokens):
            entered.set()
            await finish.wait()
            return SummaryResult('Synthetic project ORCHID remains pending.', self.identity)

    async def launch(prompt, *args, **kwargs):
        calls.append((prompt, kwargs))
        class Proc:
            pid = 0
            returncode = 0
        async def events():
            yield dict(type='session_info', session_id=f'session-{len(calls)}', model=kwargs.get('model'))
            yield dict(type='text_delta', text=f'ORCHID pending {len(calls)}')
            yield dict(type='result', session_id=f'session-{len(calls)}', is_error=False)
        return Proc(), events()

    monkeypatch.setattr(context_runtime, 'ConfiguredLocalSummarizer', Summary)
    monkeypatch.setattr(server.codex_client, 'run_codex', launch)
    await page.locator('#user-input').fill('Remember synthetic ORCHID pending')
    await page.evaluate('sendMessage()')
    await page.wait_for_function("!State.isStreaming && State.messages.some(m => m.content === 'ORCHID pending 1')")
    await page.evaluate('async () => { if (State._cursorSyncPromise) await State._cursorSyncPromise; }')
    before = await page.evaluate('JSON.stringify({cursor:State.loomCursor, ids:State.messages.map(m=>m.id)})')
    await page.locator('#user-input').fill('/compact')
    await page.evaluate('sendMessage()')
    await asyncio.wait_for(entered.wait(), 10)
    assert await db.get_live_generation_lease(conv['id'])
    assert len(calls) == 1
    finish.set()
    async def saved():
        connection = await db.get_db()
        return await connection.execute_fetchall('SELECT * FROM context_manual_boundaries')
    await _eventually(saved)
    from context_checkpoints import SQLiteCheckpointStore
    from continuity_engine import BranchSnapshot
    checkpoint = await SQLiteCheckpointStore().manual_for(
        BranchSnapshot.from_messages(await db.get_branch_to_root(json.loads(before)['cursor']['leaf_message_id'])))
    assert checkpoint and checkpoint.summary == 'Synthetic project ORCHID remains pending.'
    async def released():
        return await db.get_live_generation_lease(conv['id']) is None
    await _eventually(released)
    # Informational notices now live in Notifications rather than a toast.
    await page.locator('#notif-bell').click()
    await page.locator('#notif-dropdown').get_by_text('Checkpoint saved.', exact=False).wait_for()
    await page.locator('#notif-bell').click()
    assert await page.evaluate('JSON.stringify({cursor:State.loomCursor, ids:State.messages.map(m=>m.id)})') == before
    assert len(await db.get_conversation_tree(conv['id'])) == 2
    await page.locator('#user-input').fill('Continue ORCHID')
    await page.evaluate('sendMessage()')
    await page.wait_for_function("!State.isStreaming && State.messages.some(m => m.content === 'ORCHID pending 2')")
    assert len(calls) == 2 and calls[-1][1].get('resume_session_id') is None
    # The replay-first planner can retain this tiny history verbatim. The
    # manual boundary still forces a fresh native session; overflow/reuse of
    # the saved summary is covered by the manual-checkpoint admission tests.
    assert 'Remember synthetic ORCHID pending' in calls[-1][0]
