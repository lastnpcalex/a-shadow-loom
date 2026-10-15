"""Casual chat creation leaves provider execution and ordinary creation intact."""
import asyncio

import database as db


async def test_lost_response_and_concurrent_creation_share_one_chat(client, tmp_path, monkeypatch):
    import server
    root = tmp_path / 'ExoHome'
    monkeypatch.setattr(server, '_CASUAL_CHAT_ROOT', root)
    payload = {'request_id': 'same-request', 'cc_model': 'sonnet', 'title': 'A casual question'}
    responses = await asyncio.gather(*(client.post('/api/casual-chats', json=payload) for _ in range(3)))
    assert all(response.status_code == 200 for response in responses)
    ids = {response.json()['id'] for response in responses}
    assert len(ids) == 1
    conv = responses[0].json()
    assert conv['project_dir'] == str(root.resolve())
    assert conv['mode'] == 'claude'
    assert not await db.get_conversation_tree(conv['id'])
    assert len(await db.list_conversations()) == 1
    changed = await client.post('/api/casual-chats', json={**payload, 'cc_effort': 'low'})
    assert changed.status_code == 409
    other = await client.post('/api/casual-chats', json={**payload, 'request_id': 'other-request'})
    assert other.json()['id'] != conv['id']


async def test_casual_creation_is_explicit_and_unknown_model_fails_closed(client, tmp_path, monkeypatch):
    import server
    root = tmp_path / 'ExoHome'
    monkeypatch.setattr(server, '_CASUAL_CHAT_ROOT', root)
    rejected = await client.post('/api/casual-chats', json={'request_id': 'bad', 'cc_model': 'made-up-provider'})
    assert rejected.status_code == 400
    assert not root.exists()
    ordinary = await client.post('/api/conversations', json={'title': 'Ordinary conversation'})
    assert ordinary.status_code == 200
    assert ordinary.json()['project_dir'] is None
    assert not root.exists()


async def test_deleted_chat_creation_request_never_recreates_chat(client, tmp_path, monkeypatch):
    import server
    monkeypatch.setattr(server, '_CASUAL_CHAT_ROOT', tmp_path / 'ExoHome')
    payload = {'request_id': 'deleted-request', 'cc_model': 'sonnet'}
    created = await client.post('/api/casual-chats', json=payload)
    conv_id = created.json()['id']
    await db.delete_conversation(conv_id)
    retries = await asyncio.gather(*(client.post('/api/casual-chats', json=payload) for _ in range(3)))
    assert all(response.status_code == 410 for response in retries)
    assert not await db.list_conversations()
    fresh = await client.post('/api/casual-chats', json={**payload, 'request_id': 'fresh-request'})
    assert fresh.status_code == 200 and fresh.json()['id'] != conv_id
