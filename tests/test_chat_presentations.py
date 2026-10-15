"""Explicit presentation authority, immutable snapshots, and live download semantics."""
import asyncio
import json
from pathlib import Path
import uuid

import pytest
from PIL import Image

import database as db
import chat_presentations as presentations


def request(mode='attach', **kwargs):
    return {'mode': mode, 'request_id': str(uuid.uuid4()), **kwargs}


@pytest.fixture
async def owner(monkeypatch, tmp_path):
    import server
    monkeypatch.setenv('EXO_STATE_DIR', str(tmp_path / 'state'))
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    conv = await db.create_conversation('Presentations')
    await db.update_conversation_fields(conv['id'], project_dir=str(workspace))
    user = await db.add_message(conv['id'], 'user', 'Attach selected files')
    draft = await db.add_message(conv['id'], 'assistant', 'report.png', parent_id=user['id'])
    key = (conv['id'], user['id'], 41)
    class Live:
        stopped = False
        def done(self):
            return self.stopped
    task = Live()
    monkeypatch.setattr(server, '_generation_snapshots', {key: {'draft_msg_id': draft['id'], 'project_dir': str(workspace)}})
    monkeypatch.setattr(server, '_active_generations', {key: task})
    events = []
    async def send(cid, data):
        events.append((cid, data))
    monkeypatch.setattr(server, '_ws_send', send)
    return conv, draft, workspace, task, events


async def publish(client, owner, data, run_id='gen:41'):
    from search_space.scopes import issue_scope
    token = data.get('scope_token') or issue_scope(owner[0]['id'], str(owner[2]), run_id=run_id)
    return await client.post(f"/api/conversations/{owner[0]['id']}/chat-items", json={**data, 'run_id': run_id, 'scope_token': token})


async def test_image_snapshot_and_live_link_have_distinct_downloads(client, owner):
    conv, draft, workspace, task, events = owner
    image = workspace / 'chart with spaces.png'
    Image.new('RGB', (12, 8), 'red').save(image)
    original = image.read_bytes()
    selection = request(path=image.name)
    result = await publish(client, owner, selection)
    assert result.status_code == 200, result.text
    attached = result.json()
    assert attached['kind'] == 'image'
    assert 'workspace' not in attached and 'blob' not in attached
    linked = (await publish(client, owner, request('link', path=image.name))).json()
    Image.new('RGB', (12, 8), 'blue').save(image)
    assert (await client.get(attached['url'])).content == original
    live = await client.get(linked['url'])
    assert live.content == image.read_bytes()
    assert live.headers['content-disposition'].startswith('attachment;')
    assert live.headers['content-type'] == 'application/octet-stream'
    assert (await client.get(attached['url'] + '?download=true')).headers['content-disposition'].startswith('attachment;')
    image.unlink()
    assert (await client.get(linked['url'])).status_code == 404
    assert (await client.get(attached['url'])).content == original
    # A retry neither appends nor copies again, even after source disappearance.
    assert (await publish(client, owner, selection)).json()['id'] == attached['id']
    saved = await db.get_message(draft['id'])
    assert len(json.loads(saved['chat_items'])) == 2
    assert events[0][1]['run_id'] == 'gen:41'
    assert events[0][1]['draft_msg_id'] == draft['id']
    assert events[0][1]['type'] == 'chat_item'


async def test_inline_content_placement_parallel_and_terminal_guards(client, owner):
    conv, draft, workspace, task, events = owner
    other = await db.add_message(conv['id'], 'assistant', 'Sibling', parent_id=draft['parent_id'])
    responses = await asyncio.gather(*[
        publish(client, owner, request(content=f'<script>unsafe</script> {i}', kind='markdown' if i else 'code', language='python'))
        for i in range(3)
    ])
    assert all(r.status_code == 200 for r in responses)
    assert len(json.loads((await db.get_message(draft['id']))['chat_items'])) == 3
    assert json.loads((await db.get_message(other['id']))['chat_items']) == []
    assert (await publish(client, owner, request(content='wrong'), 'gen:999')).status_code == 409
    task.stopped = True
    assert (await publish(client, owner, request(content='late'))).status_code == 409


async def test_child_selection_is_not_parent_publication_and_reuses_snapshot(client, owner):
    conv, draft, workspace, task, events = owner
    await db.create_agent_run('selected-child', conv['id'], 'Build report', project_dir=str(workspace),
                              access={'read': ['report.md'], 'write': [], 'sensitive': False})
    await db.update_agent_run('selected-child', status='running')
    report = workspace / 'report.md'
    report.write_text('# Selected report', encoding='utf-8')
    selected = await publish(client, owner, request(path=report.name), 'agent:selected-child')
    assert selected.status_code == 200, selected.text
    assert not events
    assert json.loads((await db.get_message(draft['id']))['chat_items']) == []
    run = (await client.get('/api/agent-runs/selected-child')).json()
    assert run['chat_items'][0]['id'] == selected.json()['id']
    report.write_text('Changed later', encoding='utf-8')
    copied = await publish(client, owner, request(source_id=selected.json()['id']))
    assert copied.status_code == 200, copied.text
    assert copied.json()['content'] == '# Selected report'
    assert (await client.get(copied.json()['url'])).text == '# Selected report'
    (workspace / 'outside-scope.txt').write_text('secret', encoding='utf-8')
    assert (await publish(client, owner, request('link', path='outside-scope.txt'), 'agent:selected-child')).status_code == 400
    await db.create_agent_run('private-child', conv['id'], 'Private report', project_dir=str(workspace),
                              access={'read': ['.'], 'write': [], 'sensitive': True})
    await db.update_agent_run('private-child', status='running')
    assert (await publish(client, owner, request(content='private'), 'agent:private-child')).status_code == 403


async def test_invalid_paths_files_and_cross_conversation_sources(client, owner):
    conv, draft, workspace, task, events = owner
    for value in ('../outside.txt', 'file.txt:stream', 'missing.txt'):
        assert (await publish(client, owner, request('link', path=value))).status_code == 400
    (workspace / 'fake.png').write_text('not an image', encoding='utf-8')
    assert (await publish(client, owner, request(path='fake.png'))).status_code == 400
    assert (await publish(client, owner, request(content='x' * (presentations.MAX_TEXT_BYTES + 1)))).status_code == 400
    other = await db.create_conversation('Other')
    foreign = presentations.prepare(workspace, request(content='other conversation'))
    await presentations.save(other['id'], 'gen:foreign', None, foreign)
    assert (await publish(client, owner, request(source_id=foreign['id']))).status_code == 400
    assert json.loads((await db.get_message(draft['id']))['chat_items']) == []


async def test_new_messages_explicit_and_forks_preserve_selections(client, owner):
    conv, draft, workspace, task, events = owner
    assert draft['chat_items'] == '[]'
    selected = (await publish(client, owner, request(content='print(42)', language='python'))).json()
    fork = await db.fork_conversation(conv['id'], draft['id'])
    connection = await db.get_db()
    rows = await connection.execute_fetchall("SELECT chat_items FROM messages WHERE conversation_id=? AND role='assistant'", (fork['id'],))
    copied = json.loads(rows[0]['chat_items'])[0]
    assert copied['id'] != selected['id']
    assert copied['content'] == 'print(42)'
    await db.delete_conversation(conv['id'])
    assert (await client.get(copied['url'])).text == 'print(42)'


def test_portable_mcp_commands_send_explicit_identity(monkeypatch):
    import mcp_loom_agents as mcp
    monkeypatch.setenv('LOOM_CONV_ID', '4')
    monkeypatch.setenv('LOOM_PARENT_RUN_ID', 'gen:20')
    monkeypatch.setenv('LOOM_HISTORY_TOKEN', 'fixture-capability')
    monkeypatch.delenv('LOOM_AGENT_RUN_ID', raising=False)
    calls = []
    def post(port, route, data):
        calls.append((route, data))
        return {'ok': True}
    monkeypatch.setattr(mcp, '_post', post)
    mcp.attach_to_chat(content='# Preview', kind='markdown')
    mcp.link_file('dataset.zip')
    assert [c[1]['mode'] for c in calls] == ['attach', 'link']
    assert all(c[1]['run_id'] == 'gen:20' for c in calls)
    assert calls[0][1]['request_id'] != calls[1][1]['request_id']


async def test_revoked_or_wrong_capability_cannot_publish(client, owner):
    from search_space.scopes import issue_scope, revoke_scope
    conv, draft, workspace, task, events = owner
    wrong = issue_scope(conv['id'], str(workspace), run_id='gen:99')
    assert (await publish(client, owner, request(content='wrong run', scope_token=wrong))).status_code == 403
    expired = issue_scope(conv['id'], str(workspace), run_id='gen:41')
    revoke_scope(expired)
    assert (await publish(client, owner, request(content='revoked', scope_token=expired))).status_code == 403
    assert json.loads((await db.get_message(draft['id']))['chat_items']) == []
