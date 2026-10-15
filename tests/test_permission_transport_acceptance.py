"""Real hook subprocess/HTTP and ACP transports against a disposable policy DB.

No model credentials or active-host routes. The sole listener is a test-owned
ephemeral loopback socket, with lifespan disabled and only approval endpoints.
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
import json
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
import uvicorn

import database as db
import server
from tests.test_live_permission_acceptance import _answer, _approval_page


@asynccontextmanager
async def permission_listener():
    app = FastAPI()
    app.post('/api/cc-permission')(server.handle_cc_permission)
    @app.post('/api/cc-tool-start')
    async def tool_start():
        return {"ok": True}  # parsing feedback, not production stream side effects
    class FixtureServer(uvicorn.Server):
        @contextmanager
        def capture_signals(self):
            yield
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.setblocking(False)
    port = sock.getsockname()[1]
    instance = FixtureServer(uvicorn.Config(app, log_level='error', lifespan='off',
                                         timeout_graceful_shutdown=2, ws='none'))
    task = asyncio.create_task(instance.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not instance.started:
                if task.done():
                    await task
                await asyncio.sleep(.01)
        yield port
    finally:
        instance.should_exit = True  # only the object created above, not Loom
        try:
            await asyncio.wait_for(task, 5)
        finally:
            sock.close()


@pytest.mark.parametrize('harness', ['claude', 'antigravity', 'antigravity_modern', 'antigravity_cmd'])
async def test_real_permission_transport_reload_and_child_scope(tmp_path, monkeypatch, harness):
    from playwright.async_api import async_playwright
    monkeypatch.setattr(server, '_permission_rules', {})
    monkeypatch.setattr(server, '_pending_hook_permissions', {})
    monkeypatch.setattr(server, '_generation_snapshots', {})
    conv = await db.create_conversation('Transport fixture', mode='goose', project_dir=str(tmp_path))
    await db.update_conversation_fields(conv['id'], permission_profile='balanced')
    user = await db.add_message(conv['id'], 'user', 'fixture')
    draft = await db.add_message(conv['id'], 'assistant', 'fixture', parent_id=user['id'])
    server._generation_snapshots[(conv['id'], user['id'], 9903)] = {
        'parent_id': user['id'], 'draft_msg_id': draft['id']}
    async with permission_listener() as port, async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        async def request(kind, *, child=None):
            args = {'command': 'echo permission-fixture'} if kind == 'execute' else (
                {'path': str(tmp_path.parent / 'outside.txt')} if kind == 'outside' else
                {'path': str(tmp_path / 'fixture.txt'), 'content': 'fixture'})
            names = {'execute': 'Bash', 'outside': 'Read', 'write': 'Write'} if harness == 'claude' else {
                'execute': 'run_shell_command', 'outside': 'read_file', 'write': 'write_file'}
            if harness.startswith('antigravity'):
                args = {'CommandLine': args['command']} if kind == 'execute' else {
                    'TargetFile': args['path'], 'CodeContent': args.get('content', '')}
            event = 'PreToolUse' if harness == 'claude' else 'BeforeTool'
            payload = {'tool_name': names[kind], 'tool_input': args, 'cwd': str(tmp_path)}
            if harness in {'antigravity_modern', 'antigravity_cmd'}:
                event = 'PreToolUse'
                payload = {'toolCall': {'name': names[kind], 'args': {**args, 'Cwd': str(tmp_path)}},
                           'stepIdx': 1, 'workspacePaths': [str(tmp_path)]}
            env = {**os.environ, 'LOOM_CONV_ID': str(conv['id']), 'LOOM_PORT': str(port),
                'LOOM_PERMISSION_SCOPE': f'agent:{child}' if child else 'gen:9903',
                'LOOM_AGENT_RUN_ID': child or '', 'LOOM_CLIENT_INSTANCE_ID': '',
                'LOOM_BACKSTAGE_PARENT_ID': '',  'LOOM_USE_LLAMA': '0'}
            command = [sys.executable, str(Path('cc_permission_hook.py').resolve()), '--event', event]
            if harness == 'antigravity_cmd':
                if sys.platform != 'win32':
                    pytest.skip('Windows native hook launcher')
                from gemini_client import _agy_hook_command
                command = ['cmd.exe', '/c', _agy_hook_command(event)]
            proc = await asyncio.create_subprocess_exec(*command, cwd=str(tmp_path), env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()), 30)
                assert proc.returncode == 0, stderr.decode(errors='replace')
                response = json.loads(stdout)
                if harness in {'antigravity_modern', 'antigravity_cmd'}:
                    assert response.get('decision') in {'allow', 'deny'}, response
                return response
            finally:
                if proc.returncode is None:
                    proc.kill()  # owned single-purpose hook, never a Loom process
                    await proc.wait()
        try:
            # Balanced automatically allows recognized writes over each transport.
            await asyncio.wait_for(request('write'), 8)
            assert not server._pending_hook_permissions
            task = asyncio.create_task(request('execute'))
            async with asyncio.timeout(8):
                while not server._pending_hook_permissions:
                    if task.done():
                        pytest.fail(f'Expected a visible approval, got {task.result()}')
                    await asyncio.sleep(.01)
            rid, pending = next(iter(server._pending_hook_permissions.items()))
            assert pending['supports_allow_all']
            # Restore from server pending state, as a new/reloaded client does.
            class Socket:
                events = []
                async def send_json(self, payload):
                    self.events.append(payload)
            ws = Socket()
            server._event_router.register(ws, conv['id'])
            server._event_router.subscribe(ws, 'gen:9903')
            try:
                await server._send_pending_permissions_to_socket(ws, conv['id'])
            finally:
                server._event_router.unregister(ws)
            restored = next(e for e in ws.events if e.get('request_id') == rid)
            assert restored['rule_preview'] == pending['rule_preview']
            page = await _approval_page(browser, conv['id'])
            await page.evaluate('p => showPermissionPrompt(p)', restored)
            await page.get_by_role('button', name='Allow for Branch', exact=True).click()
            await asyncio.wait_for(task, 8)
            assert len(server._permission_rules) == 1
            # A durable child in the same workspace inherits the branch grant.
            child = await db.create_agent_run('transport-child', conv['id'], 'fixture',
                parent_message_id=draft['id'], project_dir=str(tmp_path))
            await db.update_agent_run(child['run_id'], status='running')
            await asyncio.wait_for(request('execute', child=child['run_id']), 8)
            assert not server._pending_hook_permissions
            task = asyncio.create_task(request('outside', child=child['run_id']))
            async with asyncio.timeout(8):
                while not server._pending_hook_permissions:
                    await asyncio.sleep(.01)
            rid, pending = next(iter(server._pending_hook_permissions.items()))
            assert not pending['supports_allow_all']
            await _answer(conv['id'], {'request_id': rid, 'allow': False, 'always': False,
                'feedback': 'Use only the approved fixture workspace.'})
            denied = await asyncio.wait_for(task, 8)
            encoded = json.dumps(denied)
            assert 'deny' in encoded
            assert 'approved fixture workspace' in encoded
            audits = await db.get_permission_policy_audits(conv['id'])
            assert any(a['policy_decision'] == 'auto_allow' for a in audits)
        finally:
            for pending in list(server._pending_hook_permissions.values()):
                pending['response'] = {'allow': False, 'decision_source': 'loom_lifecycle',
                                       'message': 'Fixture teardown'}
                pending['event'].set()
            if 'task' in locals() and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await browser.close()
