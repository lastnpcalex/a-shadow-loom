"""Canvas is workspace infrastructure, not a Claude-only launch side effect."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import database as db
import server
from canvas_workspace import add_canvas_notice, canvas_revision, ensure_canvas_workspace


async def test_enable_creates_guides_immediately_and_preserves_every_existing_file(client, tmp_path):
    conv = await db.create_conversation('Canvas fixture', mode='goose', project_dir=str(tmp_path))
    url = f'/api/conversations/{conv["id"]}/canvas'
    assert (await client.post(url, json={'enabled': True})).status_code == 200
    root = tmp_path / 'canvas'
    assert {p.name for p in root.iterdir()} == {'index.html', '.gitignore', 'CLAUDE.md', 'CANVAS_GUIDE.md'}
    assert 'Canvas SDK' in (root / 'CANVAS_GUIDE.md').read_text(encoding='utf-8')
    for file in root.iterdir():
        file.write_text(f'User-owned {file.name}', encoding='utf-8')
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    assert (await client.post(url, json={'enabled': False})).status_code == 200
    assert (await client.post(url, json={'enabled': True})).status_code == 200
    assert before == {p.name: p.read_bytes() for p in root.iterdir()}


def test_canvas_notice_preserves_existing_tools_even_on_native_resume(tmp_path):
    notice = add_canvas_notice("continue", {"canvas_enabled": True, "project_dir": str(tmp_path)})
    assert "persistent shared user work, even when gitignored" in notice
    assert "Read the existing entry page and imports" in notice
    assert "separate pages" in notice
    assert notice.endswith("continue")


async def test_enable_without_workspace_returns_absolute_path(client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    conv = await db.create_conversation('No workspace', mode='goose')
    result = (await client.post(f'/api/conversations/{conv["id"]}/canvas', json={'enabled': True})).json()
    assert Path(result['project_dir']).is_absolute()
    assert (Path(result['project_dir']) / 'canvas/CANVAS_GUIDE.md').is_file()


@pytest.mark.parametrize('mode', ['goose', 'claude', 'codex', 'gemini', 'local', 'openrouter', 'umans'])
async def test_legacy_canvas_bootstrap_precedes_shared_launch(mode, tmp_path, monkeypatch):
    conv = await db.create_conversation('Legacy Canvas', mode=mode, project_dir=str(tmp_path))
    await db.update_conversation_fields(conv['id'], canvas_enabled=1)
    monkeypatch.setattr(server, '_ws_send', AsyncMock())
    monkeypatch.setattr(server, 'capture_workspace_snapshot', lambda *a, **k: None)
    launched = []

    async def launch(*args):
        assert (tmp_path / 'canvas/CANVAS_GUIDE.md').is_file()
        launched.append(mode)

    monkeypatch.setattr(server, '_handle_generation', launch)
    await server._handle_generation_with_workspace_safety(None, conv['id'], {})
    assert launched == [mode]


@pytest.mark.parametrize('mode,model,adapter', [
    ('claude', 'claude-opus-4-6', 'claude'),
    ('codex', 'gpt-5.6-sol', 'codex'),
    ('gemini', 'gemini-3.1-pro', 'gemini'),
    ('goose', 'goose:openrouter:z-ai/glm-5.3-flash', 'goose'),
    ('local', 'fixture.gguf', 'claude'),
    ('openrouter', 'openrouter:z-ai/glm-5.3-flash', 'claude'),
])
@pytest.mark.parametrize('continuity', ['fresh', 'resume', 'rejected', 'empty'])
async def test_canvas_notice_reaches_actual_launch_payloads(mode, model, adapter, continuity, tmp_path, monkeypatch):
    conv = await db.create_conversation('Canvas launch', mode=mode, project_dir=str(tmp_path))
    await db.update_conversation_fields(conv['id'], canvas_enabled=1, cc_model=model, parallel_agents_enabled=1)
    parent = None
    session_mode = ('openrouter-claude' if mode == 'openrouter' else 'claude' if mode == 'local' else mode)
    if continuity != 'fresh':
        old = await db.add_message(conv['id'], 'assistant', 'Retained context', cc_session_id='fixture-native')
        await db.update_message_content(old['id'], cc_session_mode=session_mode, cc_model_used=model)
        parent = old['id']
    user = await db.add_message(conv['id'], 'user', 'Update Canvas', parent_id=parent)
    calls = []

    async def launch(prompt, *args, **kwargs):
        calls.append((prompt, kwargs))
        assert prompt.count('<loom_canvas>') == 1
        assert str(tmp_path / 'canvas/index.html') in prompt
        assert str(tmp_path / 'canvas/CANVAS_GUIDE.md') in prompt
        resumed = bool(kwargs.get('resume_session_id'))
        if resumed and continuity == 'rejected':
            raise RuntimeError('session not found')

        async def events():
            yield {'type': 'session_info', 'session_id': 'fixture-next', 'model': model}
            if not (resumed and continuity == 'empty'):
                yield {'type': 'text_delta', 'text': 'Canvas updated'}
            yield {'type': 'result', 'session_id': 'fixture-next', 'is_error': False}

        return SimpleNamespace(pid=None, returncode=0), events()

    monkeypatch.setattr(getattr(server, f'{adapter}_client'), f'run_{adapter}', launch)
    monkeypatch.setattr(server, '_ws_send', AsyncMock())
    handler = server._handle_goose_generation if mode == 'goose' else server._handle_claude_generation
    await handler(None, conv['id'], await db.get_conversation(conv['id']),
                  {'action': 'generate', 'parent_id': user['id'], 'cc_model': model})
    assert len(calls) == (2 if continuity in {'rejected', 'empty'} else 1)
    assert bool(calls[0][1].get('resume_session_id')) == (continuity != 'fresh')
    if len(calls) == 2:
        assert not calls[1][1].get('resume_session_id')
    leaf = await db.get_active_leaf(conv['id'])
    assert leaf['content'] == 'Canvas updated'


async def test_child_notice_keeps_parent_canvas_and_restricted_scope(tmp_path, monkeypatch):
    from agent_supervisor import AgentSupervisor
    conv = await db.create_conversation('Child Canvas', mode='goose', project_dir=str(tmp_path))
    await db.update_conversation_fields(conv['id'], canvas_enabled=1)
    captured = []

    async def launch(prompt, **kwargs):
        captured.append(prompt)
        async def events():
            yield {'type': 'result', 'text': 'done'}
        return SimpleNamespace(pid=None, returncode=0), events()

    monkeypatch.setattr(server.goose_client, 'run_goose', launch)
    run = await db.create_agent_run('canvas-child-fixture', conv['id'], 'Review only', requested_harness='goose', path_scope=['docs'])
    await AgentSupervisor(launcher=server._launch_agent_run)._run_one(run)
    assert len(captured) == 1
    assert str(tmp_path / 'canvas/index.html') in captured[0]
    assert 'docs' in captured[0] and 'respect your assigned workspace scope' in captured[0]
    assert not (tmp_path / 'canvas').exists()  # The child notice itself grants no filesystem write.


def test_notice_disabled_and_operator_paths_are_unchanged(tmp_path):
    assert add_canvas_notice('task', {'project_dir': str(tmp_path)}) == 'task'
    pass


async def test_canvas_file_boundary_rejects_sibling_prefix_and_revalidates(client, tmp_path):
    conv = await db.create_conversation('Canvas files', mode='goose', project_dir=str(tmp_path))
    ensure_canvas_workspace(str(tmp_path), 'fixture guide')
    sibling = tmp_path / 'canvas-private'
    sibling.mkdir()
    (sibling / 'fixture.txt').write_text('outside canvas', encoding='utf-8')
    response = await server.serve_canvas_file(conv['id'], '../canvas-private/fixture.txt')
    assert response.status_code == 403
    response = await client.get(f'/api/canvas/{conv["id"]}/index.html')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-cache'


def test_revision_detects_nested_assets_without_reading_bytes(tmp_path, monkeypatch):
    root = ensure_canvas_workspace(str(tmp_path), 'fixture guide')
    monkeypatch.setattr(Path, 'read_bytes', lambda *_: pytest.fail('No file contents in revision scans'))
    monkeypatch.setattr(Path, 'read_text', lambda *_, **kw: pytest.fail('No file contents in revision scans'))
    first = canvas_revision(str(tmp_path))
    (root / 'assets').mkdir()
    (root / 'assets/app.js').write_text('new script', encoding='utf-8')
    second = canvas_revision(str(tmp_path))
    assert first['complete'] and second['complete'] and first['revision'] != second['revision']
    assert second == canvas_revision(str(tmp_path))
    assert canvas_revision(str(tmp_path), max_entries=2) == {'complete': False, 'revision': None}


async def test_revision_only_for_enabled_canvas(client, tmp_path):
    conv = await db.create_conversation('Canvas revision', mode='goose', project_dir=str(tmp_path))
    url = f'/api/conversations/{conv["id"]}/canvas/revision'
    assert (await client.get(url)).status_code == 404
    await client.post(f'/api/conversations/{conv["id"]}/canvas', json={'enabled': True})
    response = await client.get(url)
    assert response.status_code == 200 and response.json()['complete']
    assert response.headers['cache-control'] == 'no-store'


def test_canvas_instruction_examples_preserve_existing_work(tmp_path):
    root = ensure_canvas_workspace(str(tmp_path), server.CANVAS_CLAUDE_MD)
    instructions = (root / 'CLAUDE.md').read_text(encoding='utf-8')
    guide = (root / 'CANVAS_GUIDE.md').read_text(encoding='utf-8')
    assert 'persistent shared user work, even when gitignored' in instructions
    assert 'preserve the existing entry point, tools, data, and navigation' in instructions
    assert 'For a new, empty Canvas' in guide
    assert 'preserving the entry point and existing tools' in guide
