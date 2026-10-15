"""Read-only landed history: disposable SQLite and ASGI only, no live hosts."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import database as db
import loom_auth
import server
import admin_server as admin


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name in ('_active_generations', '_generation_snapshots', '_pending_hook_permissions'):
        monkeypatch.setattr(server, name, {})


async def message(conv, *, status=None, duration=None, lease=None, parent=None):
    result = await db.add_message(conv['id'], 'assistant', 'PRIVATE fixture response', generation_lease_id=lease, parent_id=parent)
    await db.update_message_content(result['id'], generation_ms=duration, turn_lifecycle_state=status,
        cc_model_used='historic-model', cc_session_mode='goose', content_blocks='[{"private": "tool-output"}]')
    return result


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app, client=('127.0.0.1', 1)),
        base_url='http://fixture', headers=loom_auth.relay_headers())


async def test_persisted_outcomes_no_inference_from_disappearance_or_new_selection(tmp_path):
    conv = await db.create_conversation('Inbox fixture', mode='goose', project_dir=str(tmp_path))
    await db.update_conversation_fields(conv['id'], cc_model='NEXT-model')
    expected = {}
    for status in ('failed', 'partial_failed', 'cancelled', 'interrupted'):
        row = await message(conv, status=status, duration=1250)
        expected[row['id']] = status
    timed = await message(conv, duration=2000)
    expected[timed['id']] = 'finished'
    # An old message or orphaned draft with text alone is not completion evidence.
    await message(conv)
    running = await message(conv, duration=3000)
    await db.register_active_generation(running['id'], conv['id'], 99999999)
    await db.try_acquire_generation_lease(conv['id'], 'fixture-lease', run_id='gen:fixture')
    finalizing = await message(conv, duration=4000, lease='fixture-lease')
    await db.bind_generation_lease('fixture-lease', draft_msg_id=finalizing['id'])
    connection = await db.get_db()
    before = connection.total_changes
    async with client() as http:
        response = await http.get('/api/admin/supervision')
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        assert {r['draft_msg_id']: r['status'] for r in response.json()['landed']} == expected
    assert connection.total_changes == before  # no acknowledgements, cursors or lifecycle writes
    await db.release_generation_lease('fixture-lease', 'completed')
    # Metadata reopens from SQLite; it does not depend on observing the live run.
    await db.close_db()
    rows = await db.list_landed_generations()
    row = next(r for r in rows if r['draft_msg_id'] == finalizing['id'])
    assert row['status'] == 'completed' and not row['completion_time_estimated']
    assert row['branch_id'] == finalizing['id'] and row['run_id'] == 'gen:fixture'
    assert all(r['model'] == 'historic-model' for r in rows)
    assert 'PRIVATE' not in json.dumps(rows) and 'tool-output' not in json.dumps(rows)
    assert not any('content' in r or 'content_blocks' in r or 'model_attestation' in r for r in rows)
    await db.update_message_content(finalizing['id'], model_attestation=json.dumps({
        'harness': 'Codex app-server', 'effective_model': 'runtime-model', 'private': 'PRIVATE attestation'}))
    attested = next(r for r in await db.list_landed_generations() if r['draft_msg_id'] == finalizing['id'])
    assert attested['model'] == 'runtime-model' and attested['mode'] == 'Codex app-server'
    assert 'PRIVATE' not in json.dumps(attested)


async def test_task_teardown_stays_active_even_after_final_timing(tmp_path):
    conv = await db.create_conversation('Teardown fixture', mode='goose', project_dir=str(tmp_path))
    draft = await message(conv, duration=4000)
    key = (conv['id'], 0, 77)
    server._active_generations[key] = SimpleNamespace(done=lambda: False)
    server._generation_snapshots[key] = {'draft_msg_id': draft['id']}
    async with client() as http:
        data = (await http.get('/api/admin/supervision')).json()
    assert data['landed'] == [] and data['generations'][0]['draft_msg_id'] == draft['id']


async def test_busy_thread_keeps_one_landing_without_crowding_other_roots(tmp_path):
    conv = await db.create_conversation('Shared workspace', mode='goose', project_dir=str(tmp_path))
    parallel_root = await message(conv, duration=0)
    busy_root = await message(conv, duration=0)
    latest = busy_root
    for _ in range(105):
        latest = await message(conv, duration=0, parent=latest['id'])
    rows = await db.list_landed_generations(2)
    assert {row['draft_msg_id'] for row in rows} == {latest['id'], parallel_root['id']}
    assert {row['root_message_id'] for row in rows} == {busy_root['id'], parallel_root['id']}
    assert len({row['loom_thread_id'] for row in rows}) == 2
    assert (await db.list_landed_generations(1))[0]['draft_msg_id'] == latest['id']
    # A new active turn does not replace the last landed result.
    active = await message(conv, duration=0, parent=latest['id'])
    await db.register_active_generation(active['id'], conv['id'], 99999999)
    assert {row['draft_msg_id'] for row in await db.list_landed_generations(2)} == {latest['id'], parallel_root['id']}


async def test_roots_do_not_merge_by_workspace_title_or_model(tmp_path):
    roots = []
    for workspace in [tmp_path, tmp_path, tmp_path / 'other']:
        conv = await db.create_conversation('Same title', mode='goose', project_dir=str(workspace))
        roots.append(await message(conv, duration=0))
    rows = await db.list_landed_generations()
    assert {row['draft_msg_id'] for row in rows} == {root['id'] for root in roots}
    assert len({row['conv_id'] for row in rows}) == 3


async def test_latest_landing_uses_finish_order_and_children_remain_independent(tmp_path):
    conv = await db.create_conversation('Finish ordering', mode='goose', project_dir=str(tmp_path))
    root = await message(conv, duration=5000)
    newer_id = await message(conv, duration=0, parent=root['id'])
    # Controlled timestamps: the older-created turn actually landed last.
    async with db.write_transaction() as connection:
        await connection.execute('UPDATE messages SET created_at = 1000 WHERE id = ?', (root['id'],))
        await connection.execute('UPDATE messages SET created_at = 1001 WHERE id = ?', (newer_id['id'],))
    await db.create_agent_run('independent-child', conv['id'], 'Fixture task', parent_message_id=root['id'])
    await db.update_agent_run('independent-child', status='completed', completed_at=1006)
    rows = await db.list_landed_generations()
    assert len(rows) == 2 and rows[0]['kind'] == 'child'
    assert rows[1]['draft_msg_id'] == root['id'] and rows[1]['completed_at'] == 1005


async def test_children_limits_and_malformed_attestation(tmp_path):
    conv = await db.create_conversation('Child fixture', mode='codex', project_dir=str(tmp_path))
    for _ in range(104):
        # Do not invent finish times one second in the future: the no-time
        # row below must remain in the bounded window regardless of test speed.
        await message(conv, duration=0)
    malformed = await message(conv, status='failed')
    await db.update_message_content(malformed['id'], model_attestation='[]')
    await db.create_agent_run('child-done', conv['id'], 'PRIVATE child task',
        parent_message_id=malformed['id'], requested_harness='goose', requested_model='child-model')
    await db.update_agent_run('child-done', status='completed', completed_at=4102444800,
        result={'private': 'PRIVATE child result'})
    await db.create_agent_run('child-active', conv['id'], 'PRIVATE active child')
    rows = await db.list_landed_generations(1000)
    assert len(rows) == 100 and rows[0]['run_id'] == 'agent:child-done'
    assert rows[0]['branch_id'] == malformed['id'] and rows[0]['model'] == 'child-model'
    assert 'PRIVATE' not in json.dumps(rows) and 'child-active' not in json.dumps(rows)
    assert len(await db.list_landed_generations(1)) == 1
    no_time = next(r for r in rows if r.get('draft_msg_id') == malformed['id'])
    assert no_time['completed_at'] is None and no_time['status'] == 'failed'


async def test_optional_history_failure_does_not_break_active_or_permissions(monkeypatch):
    monkeypatch.setattr(db, 'list_landed_generations', AsyncMock(side_effect=RuntimeError('fixture')))
    async with client() as http:
        response = await http.get('/api/admin/supervision')
    assert response.status_code == 200
    assert response.json()['landed'] is None and response.json()['permissions'] == []


async def test_task_appearing_during_history_read_is_not_landed(monkeypatch, tmp_path):
    conv = await db.create_conversation('Concurrent metadata fixture', mode='goose', project_dir=str(tmp_path))
    draft = await message(conv, duration=1)
    read_history = db.list_landed_generations
    async def during_read():
        rows = await read_history()
        # Simulate the await boundary; no provider or real task is launched.
        server._active_generations[(conv['id'], 0, 78)] = SimpleNamespace(
            done=lambda: False, _draft_msg_id=draft['id'])
        return rows
    monkeypatch.setattr(db, 'list_landed_generations', during_read)
    async with client() as http:
        data = (await http.get('/api/admin/supervision')).json()
    assert data['landed'] == []


@pytest.mark.parametrize('history', [None, []])
async def test_admin_relay_preserves_missing_vs_empty_history(monkeypatch, history):
    real_client = httpx.AsyncClient
    def transport(request):
        assert request.url.port == 61011 and request.method == 'GET'
        return httpx.Response(200, json={'generations': [], 'permissions': [], 'landed': history, 'landed_limit': 100})
    monkeypatch.setattr(admin.httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(transport)))
    result = await admin._supervision_snapshot('fixture', {'port': 61011})
    assert result['available'] and result['landed'] == history
