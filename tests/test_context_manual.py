"""Manual checkpoint through real generation admission, with a synthetic model."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import context_runtime
import database as db
import server
from context_checkpoints import SQLiteCheckpointStore
from continuity_engine import BranchSnapshot
from tests.test_continuity_launch_matrix import MODELS, SyntheticSummary, setup, run


async def selected(monkeypatch, tmp_path, harness):
    conv, current, calls = await setup(monkeypatch, tmp_path, harness, harness)
    await db.update_conversation_fields(conv['id'], parallel_agents_enabled=0)
    parent = await db.get_message(current['parent_id'])
    cursor = await db.select_loom_cursor(conv['id'], 'manual-page', parent['loom_thread_id'], parent['id'])
    monkeypatch.setattr(server, '_active_generations', {})
    monkeypatch.setattr(server, '_generation_snapshots', {})
    monkeypatch.setattr(server, '_shutdown_requested', False)
    monkeypatch.setattr(server, '_ws_reply', AsyncMock())
    monkeypatch.setattr(server, '_dispatch_ready_queued_generations', AsyncMock())
    data = dict(action='compact', parent_id=parent['id'], client_instance_id='manual-page',
                loom_thread_id=cursor['thread_id'], cursor_revision=cursor['revision'])
    return conv, current, calls, parent, cursor, data


@pytest.mark.parametrize('harness', MODELS)
async def test_manual_checkpoint_all_harnesses_then_fresh_then_native(monkeypatch, tmp_path, harness):
    conv, current, calls, parent, cursor, data = await selected(monkeypatch, tmp_path, harness)
    before = await db.get_branch_to_root(parent['id'])
    key, task = await server._launch_generation_task(None, conv['id'], data, 'manual-page')
    await task
    assert not calls  # No native CLI, no in-place session compaction.
    assert await db.get_branch_to_root(parent['id']) == before
    assert await db.get_client_loom_cursor(conv['id'], 'manual-page') == cursor
    assert await db.get_live_generation_lease(conv['id']) is None
    checkpoint = await SQLiteCheckpointStore().manual_for(BranchSnapshot.from_messages(before))
    assert checkpoint and checkpoint.covered_ids[-1] == parent['id']
    assert any(c.args[1].get('success') for c in server._ws_send.call_args_list)
    # Adapter-only continuation fixtures opt into parallel to bypass admission.
    await db.update_conversation_fields(conv['id'], parallel_agents_enabled=1)
    result = await run(conv, current['id'], harness)
    assert calls[-1][1].get('resume_session_id') is None
    assert 'KEEP_KEY_PRIVATE' in calls[-1][0]
    assert 'old detail' not in calls[-1][0]
    assert len(SyntheticSummary.calls) == 1
    next_user = await db.add_message(conv['id'], 'user', 'Next task', parent_id=result['id'])
    await run(conv, next_user['id'], harness)
    assert calls[-1][1].get('resume_session_id') == 'destination-session'


async def test_manual_rejects_occupied_lane(monkeypatch, tmp_path):
    conv, _, calls, _, _, data = await selected(monkeypatch, tmp_path, 'goose')
    await db.try_acquire_generation_lease(conv['id'], 'existing-provider')
    with pytest.raises(server.GenerationLaneOccupied):
        await server._launch_generation_task(None, conv['id'], data, 'manual-page')
    assert not calls and not SyntheticSummary.calls


@pytest.mark.parametrize('failure', ['cancel', 'cursor_moves', 'branch_edit', 'backend_down'])
async def test_manual_failure_never_activates_or_changes_native_session(monkeypatch, tmp_path, failure):
    conv, _, calls, parent, cursor, data = await selected(monkeypatch, tmp_path, 'goose')
    entered, finish = asyncio.Event(), asyncio.Event()

    class Controlled(SyntheticSummary):
        async def summarize(self, text, max_tokens):
            entered.set()
            await finish.wait()
            if failure == 'backend_down':
                raise RuntimeError('PRIVATE_CONTENT_MUST_NOT_LEAK')
            return await super().summarize(text, max_tokens)

    monkeypatch.setattr(context_runtime, 'ConfiguredLocalSummarizer', Controlled)
    _, task = await server._launch_generation_task(None, conv['id'], data, 'manual-page')
    await asyncio.wait_for(entered.wait(), 5)
    if failure == 'cancel':
        task.cancel()
    elif failure == 'cursor_moves':
        await db.select_loom_cursor(conv['id'], 'manual-page', cursor['thread_id'],
                                    parent['parent_id'], expected_revision=cursor['revision'])
    elif failure == 'branch_edit':
        await db.update_message_content(parent['parent_id'], content='Edited synthetic record')
    finish.set()
    if failure == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert await db.get_live_generation_lease(conv['id']) is None
    connection = await db.get_db()
    assert not await connection.execute_fetchall('SELECT * FROM context_manual_boundaries')
    assert (await db.get_message(parent['id']))['cc_session_id'] == 'source-session'
    assert not calls
    assert 'PRIVATE_CONTENT_MUST_NOT_LEAK' not in str(server._ws_send.call_args_list)


async def test_manual_wrong_cursor_revision_does_not_summarize(monkeypatch, tmp_path):
    conv, _, _, _, _, data = await selected(monkeypatch, tmp_path, 'codex')
    data['cursor_revision'] += 1
    _, task = await server._launch_generation_task(None, conv['id'], data, 'manual-page')
    await task
    assert not SyntheticSummary.calls
    assert any('moved' in c.args[1].get('error', '') for c in server._ws_send.call_args_list)


async def test_disconnected_browser_does_not_abort_checkpoint(monkeypatch, tmp_path):
    conv, _, _, parent, _, data = await selected(monkeypatch, tmp_path, 'goose')
    monkeypatch.setattr(server, '_ws_reply', AsyncMock(side_effect=OSError('socket closed')))
    class Socket:
        pass
    _, task = await server._launch_generation_task(Socket(), conv['id'], data, 'manual-page')
    await task
    snapshot = BranchSnapshot.from_messages(await db.get_branch_to_root(parent['id']))
    assert await SQLiteCheckpointStore().manual_for(snapshot)
    assert await db.get_live_generation_lease(conv['id']) is None
    assert any(c.args[1].get('success') for c in server._ws_send.call_args_list)


async def test_manual_checkpoint_does_not_cover_sibling_or_edited_branch(monkeypatch, tmp_path):
    conv, _, _, parent, _, data = await selected(monkeypatch, tmp_path, 'goose')
    _, task = await server._launch_generation_task(None, conv['id'], data, 'manual-page')
    await task
    sibling = await db.add_message(conv['id'], 'assistant', 'Other outcome', parent_id=parent['parent_id'])
    store = SQLiteCheckpointStore()
    assert await store.manual_for(BranchSnapshot.from_messages(await db.get_branch_to_root(parent['id'])))
    assert await store.manual_for(BranchSnapshot.from_messages(await db.get_branch_to_root(sibling['id']))) is None
    await db.update_message_content(parent['id'], content='Corrected outcome')
    assert await store.manual_for(BranchSnapshot.from_messages(await db.get_branch_to_root(parent['id']))) is None


def test_compact_ui_uses_exact_cursor_and_does_not_reload_checkpoint_result():
    source = Path('static/chat.js').read_text(encoding='utf-8')
    command = source.split("case 'compact': {", 1)[1].split("case 'clear':", 1)[0]
    assert 'cursor_revision: cursor.revision' in command
    assert "action: 'compact'" in command
    done = source.split("case 'compact_done':", 1)[1].split("case 'compact_summary_ready':", 1)[0]
    checkpoint = done.split('if (data.checkpoint_only)', 1)[1].split('}', 1)[0]
    assert 'loadMessages(' not in checkpoint
    assert 'break;' in checkpoint
