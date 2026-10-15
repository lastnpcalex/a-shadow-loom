import json
from unittest.mock import AsyncMock

import httpx
import pytest

import database as db
from event_router import content_free_notification
from machine_settings import FIELDS, TUNING_FIELDS, validate


@pytest.mark.parametrize('saved,status', [({}, 409), ({'default_permission_profile': 'trusted'}, 409), ({'default_permission_profile': 'strict'}, 200)])
async def test_default_save_verifies_main_persistence(monkeypatch, saved, status):
    import admin_server as admin
    monkeypatch.setattr(admin, 'INSTANCES', {'main': {'port': 61011}})
    monkeypatch.setattr(admin, '_put_instance', AsyncMock(return_value=httpx.Response(200, json={'ok': True})))
    monkeypatch.setattr(admin, '_get_instance', AsyncMock(return_value=httpx.Response(200, json=saved)))
    result = await admin.api_save_machine_settings({'settings': {'default_permission_profile': 'strict'}})
    assert (result.status_code if hasattr(result, 'status_code') else 200) == status


@pytest.mark.parametrize('profile', ['strict', 'balanced', 'trusted', 'trust_review'])
async def test_configured_default_only_affects_new_conversations(monkeypatch, tmp_path, profile):
    from config import config
    existing = await db.create_conversation('Existing', mode='codex')
    monkeypatch.setattr(config, 'default_permission_profile', profile)
    assert validate({'default_permission_profile': profile}, FIELDS) == {'default_permission_profile': profile}
    new = await db.create_conversation('New', mode='codex')
    casual = await db.create_casual_conversation('default-fixture', 'Home', 'codex', str(tmp_path), 'codex', 'high')
    assert new['permission_profile'] == casual['permission_profile'] == profile
    assert (await db.get_conversation(existing['id']))['permission_profile'] == existing['permission_profile']
    message = await db.add_message(existing['id'], 'user', 'Fork')
    assert (await db.fork_conversation(existing['id'], message['id']))['permission_profile'] == existing['permission_profile']
    monkeypatch.setattr(config, 'default_permission_profile', 'balanced')
    assert (await db.create_casual_conversation('default-fixture', 'Home', 'codex', str(tmp_path), 'codex', 'high'))['permission_profile'] == profile


def test_default_profile_config_persistence_and_validation(monkeypatch):
    from config import Config
    config = Config()
    saved = []
    monkeypatch.setattr(config, 'save', lambda: saved.append(config.to_dict()))
    config.update_from_dict({'default_permission_profile': 'strict'})
    assert saved[0]['default_permission_profile'] == 'strict'
    with pytest.raises(ValueError):
        config.update_from_dict({'default_permission_profile': 'invalid'})
    with pytest.raises(ValueError):
        validate({'default_permission_profile': 'invalid'}, FIELDS)


async def test_new_profiles_and_forks_preserve_user_choice(tmp_path):
    conv = await db.create_conversation('New', mode='codex')
    assert conv['permission_profile'] == 'trusted'
    casual = await db.create_casual_conversation('fixture', 'Home', 'codex', str(tmp_path), 'codex', 'high')
    assert casual['permission_profile'] == 'trusted'
    await db.update_conversation_fields(conv['id'], permission_profile='strict')
    message = await db.add_message(conv['id'], 'user', 'Preserve strict')
    fork = await db.fork_conversation(conv['id'], message['id'])
    assert fork['permission_profile'] == 'strict'
    assert (await db.get_conversation(conv['id']))['permission_profile'] == 'strict'


async def test_global_notice_projection_and_durable_replay_have_no_tool_payload():
    conv = await db.create_conversation('Notice', mode='codex')
    await db.save_pending_permission('notice', conv['id'], 'Write', {'content': 'PRIVATE'}, 'PRIVATE', draft_msg_id=None)
    notices = await db.get_permission_notices()
    assert notices == [{'request_id': 'notice', 'conv_id': conv['id'], 'message_id': None}]
    public = content_free_notification({'type': 'permission_notice', 'request_id': 'notice',
        'conv_id': conv['id'], 'status': 'pending', 'tool_input': 'PRIVATE', 'feedback': 'PRIVATE', 'content': 'PRIVATE'})
    assert 'PRIVATE' not in json.dumps(public)
    assert public['request_id'] == 'notice'


async def test_admin_settings_project_only_machine_fields_and_patch_selected_model(monkeypatch):
    import admin_server as admin
    monkeypatch.setattr(admin, 'INSTANCES', {'main': {'port': 61011}})
    models = {'one.gguf': {'ctx_size': 8192, 'server_model_id': 'keep'}, 'two.gguf': {'ngl': 0}}
    async def get(port, path):
        assert port == 61011
        data = {'/api/config': {'temperature': .8, 'openrouter_api_key': 'PRIVATE'},
                '/api/models-config': {'models': models}, '/api/disk-models': {'models': list(models)}}[path]
        return httpx.Response(200, json=data)
    monkeypatch.setattr(admin, '_get_instance', get)
    put = AsyncMock(return_value=httpx.Response(200, json={'ok': True}))
    monkeypatch.setattr(admin, '_put_instance', put)
    response = await admin.api_machine_settings()
    assert b'PRIVATE' not in response.body
    assert json.loads(response.body)['settings']['temperature'] == .8
    assert await admin.api_save_machine_settings({'settings': {'temperature': .6}}) == {'ok': True}
    put.assert_awaited_with(61011, '/api/config', {'temperature': .6})
    assert await admin.api_save_machine_settings({'model': 'one.gguf', 'tuning': {'ngl': 0}}) == {'ok': True}
    saved = put.await_args.args[2]['models']
    assert saved['one.gguf'] == {'ctx_size': 8192, 'server_model_id': 'keep', 'ngl': 0}
    assert saved['two.gguf'] == {'ngl': 0}
    before = put.await_count
    rejected = await admin.api_save_machine_settings({'settings': {'openrouter_api_key': 'PRIVATE'}})
    assert rejected.status_code == 400 and put.await_count == before


@pytest.mark.parametrize('values,fields', [({'temperature': float('nan')}, FIELDS),
    ({'max_tokens': 1}, FIELDS), ({'enable_umans_models': 'false'}, FIELDS), ({'ngl': -1}, TUNING_FIELDS)])
def test_invalid_settings_never_reach_main(values, fields):
    with pytest.raises(ValueError):
        validate(values, fields)
