"""Admin supervision: disposable DB and mocked transports, never live hosts."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import AsyncMock

import httpx
import pytest

import admin_server as admin
import database as db
import loom_auth
import server


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch):
    for name in ('_pending_hook_permissions', '_permission_rules', '_active_generations',
                 '_generation_snapshots', '_active_websockets'):
        monkeypatch.setattr(server, name, {})
    monkeypatch.setattr(admin, 'INSTANCES', {
        'fixture_a': {'port': 61011, 'label': 'Fixture A'},
        'fixture_b': {'port': 61012, 'label': 'Fixture B'},
    })
    monkeypatch.setattr(admin, '_supervision_schemes', {})


async def make_waiter(tmp_path):
    conv = await db.create_conversation('Supervision fixture', mode='goose', project_dir=str(tmp_path))
    # These fixtures exercise pending approvals, independent of the new-chat default.
    await db.update_conversation_fields(conv['id'], permission_profile='balanced')
    user = await db.add_message(conv['id'], 'user', 'fixture')
    draft = await db.add_message(conv['id'], 'assistant', 'fixture', parent_id=user['id'])
    server._generation_snapshots[(conv['id'], user['id'], 9090)] = {
        'parent_id': user['id'], 'draft_msg_id': draft['id']}
    payload = {'loom_conv_id': conv['id'], 'permission_scope': 'gen:9090',
               'tool_name': 'Bash', 'tool_input': {'command': 'python -m pytest tests/a.py -q',
                                                  'cwd': str(tmp_path)}, 'source': 'goose_acp'}
    task = asyncio.create_task(server.handle_cc_permission(payload))
    async with asyncio.timeout(5):
        while not server._pending_hook_permissions:
            if task.done():
                raise AssertionError(await task)
            await asyncio.sleep(.01)
    request_id = next(iter(server._pending_hook_permissions))
    return conv, draft, payload, request_id, task


def main_client(headers=None):
    # The admin server relays with the machine secret; see loom_auth.
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app, client=('127.0.0.1', 1)),
                             base_url='http://fixture',
                             headers=loom_auth.relay_headers() if headers is None else headers)


def test_claude_code_wait_ends_before_its_hook_is_killed():
    import claude_client
    assert server._permission_wait_limit('claude_code') < claude_client.PERMISSION_HOOK_TIMEOUT_SECONDS
    for source in ('goose_acp', 'codex_app_server', 'antigravity', 'loom_permission_hook'):
        assert server._permission_wait_limit(source) >= 24 * 60 * 60


async def test_expired_request_is_closed_to_late_clicks(tmp_path, monkeypatch):
    monkeypatch.setattr(server, '_permission_wait_limit', lambda source: 0.2)
    conv, draft, payload, request_id, task = await make_waiter(tmp_path)
    outcome = await asyncio.wait_for(task, 5)
    assert not outcome['allow'] and outcome['decision_source'] == 'loom_timeout'
    assert request_id not in server._pending_hook_permissions
    assert not await server._answer_pending_permission(request_id, {'allow': True}, channel='browser')
    async with main_client() as client:
        late = await client.post(f'/api/admin/permissions/{request_id}',
                                 json={'allow': True, 'conv_id': conv['id'], 'run_id': 'gen:9090'})
        assert late.status_code == 409
    saved = await db.get_permission_resolutions_for_draft(draft['id'])
    assert [row['decision_source'] for row in saved] == ['loom_timeout']
    assert not saved[0]['allowed']


async def test_constant_admin_header_cannot_list_or_answer_permissions(tmp_path):
    conv, draft, payload, request_id, task = await make_waiter(tmp_path)
    try:
        async with main_client({'X-Loom-Admin': '1'}) as forged:
            assert (await forged.get('/api/admin/supervision')).status_code == 403
            data = {'allow': True, 'conv_id': conv['id'], 'run_id': 'gen:9090'}
            assert (await forged.post(f'/api/admin/permissions/{request_id}', json=data)).status_code == 403
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('allow,remember', [(False, False), (True, False), (True, True)])
async def test_admin_uses_same_waiter_notes_rules_and_exact_links(tmp_path, allow, remember):
    conv, draft, payload, request_id, task = await make_waiter(tmp_path)
    try:
        async with main_client() as client:
            snapshot = await client.get('/api/admin/supervision')
            assert snapshot.status_code == 200
            assert snapshot.headers['cache-control'] == 'no-store'
            permission = snapshot.json()['permissions'][0]
            assert permission['branch_id'] == draft['id']
            assert permission['project_dir'] == str(tmp_path)
            assert permission['supports_allow_all']
            assert 'event' not in permission and 'rule_candidate' not in permission
            data = {'allow': allow, 'always': remember, 'conv_id': conv['id'],
                    'run_id': 'gen:9090', 'feedback': 'Use the review copy, not production.'}
            responses = await asyncio.gather(*[
                client.post(f'/api/admin/permissions/{request_id}', json=data) for _ in range(2)])
            assert sorted(r.status_code for r in responses) == [200, 409]
            outcome = await asyncio.wait_for(task, 2)
            assert outcome['allow'] == allow
            assert outcome['decision_source'] == 'user'
            assert outcome['feedback'] == data['feedback']
            assert len(server._permission_rules) == int(remember)
            assert (await client.get('/api/admin/supervision')).json()['permissions'] == []
            saved = await db.get_permission_resolutions_for_draft(draft['id'])
            assert len(saved) == 1 and saved[0]['decision_source'] == 'user'
            if remember:
                payload['tool_input']['command'] = 'python -m pytest tests/b.py -q'
                followup = await asyncio.wait_for(server.handle_cc_permission(payload), 2)
                assert followup['allow'] and followup['decision_source'] == 'loom_policy'
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_owner_mismatch_stale_request_and_cross_origin_fail_closed(tmp_path):
    conv, draft, payload, request_id, task = await make_waiter(tmp_path)
    try:
        async with main_client() as client:
            data = {'allow': True, 'conv_id': conv['id'], 'run_id': 'gen:WRONG'}
            assert (await client.post(f'/api/admin/permissions/{request_id}', json=data)).status_code == 409
            data['run_id'] = 'gen:9090'
            assert (await client.post(f'/api/admin/permissions/{request_id}', json=data,
                                     headers={'Origin': 'http://untrusted.invalid'})).status_code == 403
            assert (await client.post('/api/admin/permissions/expired', json=data)).status_code == 409
            data['allow'] = 'false'
            assert (await client.post(f'/api/admin/permissions/{request_id}', json=data)).status_code == 422
            assert not task.done()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app, client=('192.0.2.10', 1)),
                                    base_url='http://fixture', headers=loom_auth.relay_headers()) as remote:
            assert (await remote.get('/api/admin/supervision')).status_code == 403
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_admin_resolution_clears_origin_before_run_subscription(tmp_path, monkeypatch):
    conv, draft, payload, request_id, task = await make_waiter(tmp_path)
    class OriginSocket:
        send_json = AsyncMock()
    origin = OriginSocket()
    monkeypatch.setattr(server, '_find_client_websocket', lambda cid, page: origin)
    monkeypatch.setattr(server._event_router, 'recipients', lambda *args: set())
    try:
        async with main_client() as client:
            response = await client.post(f'/api/admin/permissions/{request_id}', json={
                'allow': False, 'conv_id': conv['id'], 'run_id': 'gen:9090'})
            assert response.status_code == 200
            assert (await asyncio.wait_for(task, 2))['allow'] is False
            origin.send_json.assert_awaited_once()
            assert origin.send_json.call_args.args[0]['type'] == 'permission_resolved'
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_snapshot_matches_exact_run_and_includes_preparing_children(tmp_path, monkeypatch):
    conv = await db.create_conversation('Runs', mode='codex', project_dir=str(tmp_path))
    user = await db.add_message(conv['id'], 'user', 'fixture')
    drafts = [await db.add_message(conv['id'], 'assistant', 'fixture', parent_id=user['id']) for _ in range(2)]
    for draft in drafts:
        await db.register_active_generation(draft['id'], conv['id'], 99999999, mode='codex')
    server._active_generations[(conv['id'], user['id'], 1)] = SimpleNamespace(done=lambda: False)
    server._generation_snapshots[(conv['id'], user['id'], 1)] = {'draft_msg_id': drafts[0]['id']}
    server._generation_snapshots[(conv['id'], user['id'], 1)]['model_attestation'] = {
        'harness': 'Codex app-server', 'effective_model': 'actual-model'}
    await db.update_conversation_fields(conv['id'], cc_model='different-next-model')
    server._active_generations[(conv['id'], user['id'], 2)] = SimpleNamespace(done=lambda: False)
    monkeypatch.setattr(db, 'list_agent_runs', AsyncMock(return_value=[{
        'run_id': 'child-fixture', 'conversation_id': conv['id'], 'parent_message_id': drafts[0]['id'],
        'status': 'running', 'requested_harness': 'goose', 'project_dir': str(tmp_path)}]))
    server._pending_hook_permissions['child-request'] = {
        'conv_id': conv['id'], 'run_id': 'agent:child-fixture', 'response': None, 'tool_name': 'Read'}
    async with main_client() as client:
        data = (await client.get('/api/admin/supervision')).json()
    assert len(data['generations']) == 4
    assert next(r for r in data['generations'] if r['run_id'] == 'gen:1')['model'] == 'actual-model'
    assert not next(r for r in data['generations'] if r.get('draft_msg_id') == drafts[1]['id']).get('model')
    assert next(r for r in data['generations'] if r.get('draft_msg_id') == drafts[1]['id'])['status'] == 'tracked'
    assert next(r for r in data['generations'] if r['run_id'] == 'gen:2')['status'] == 'preparing'
    assert next(r for r in data['generations'] if r['kind'] == 'child')['status'] == 'awaiting_permission'
    assert data['permissions'][0]['branch_id'] == drafts[0]['id']


async def test_proxy_aggregates_and_never_searches_another_instance_on_mutation(monkeypatch):
    requests = []
    real_client = httpx.AsyncClient
    def transport(request):
        requests.append(request)
        assert request.url.port in {61011, 61012}  # physically exclude active hosts
        # The relay authenticates with the machine secret, never a constant.
        assert request.headers[loom_auth.RELAY_HEADER] == loom_auth.machine_secret()
        if request.method == 'GET':
            return httpx.Response(200, json={'generations': [], 'permissions': [{'request_id': 'same-id'}]})
        return httpx.Response(409, json={'detail': 'already resolved'})
    monkeypatch.setattr(admin.httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(transport)))
    async with real_client(transport=httpx.ASGITransport(app=admin.app), base_url='http://admin.fixture') as client:
        data = (await client.get('/api/supervision')).json()
        assert [i['instance'] for i in data['instances']] == ['fixture_a', 'fixture_b']
        for instance in data['instances']:
            assert instance['permissions'][0]['request_id'] == 'same-id'
        requests.clear()
        path = '/api/supervision/fixture_b/permissions/same-id'
        result = await client.post(path, json={'allow': True}, headers={'X-Loom-Admin': '1', 'Origin': 'http://admin.fixture'})
        assert result.status_code == 409
        assert len(requests) == 1 and requests[0].url.port == 61012
        assert (await client.post(path, json={}, headers={'X-Loom-Admin': '1', 'Origin': 'http://evil.invalid'})).status_code == 403
        assert (await client.post(path, json={})).status_code == 403
        assert (await client.post('/api/supervision/unknown/permissions/id', json={}, headers={'X-Loom-Admin': '1'})).status_code == 404
        assert len(requests) == 1


async def test_proxy_timeout_is_not_replayed_and_offline_is_explicit(monkeypatch):
    requests = []
    real_client = httpx.AsyncClient
    def transport(request):
        requests.append(request)
        assert request.url.port in {61011, 61012}
        raise httpx.ReadTimeout('fixture timeout', request=request)
    monkeypatch.setattr(admin.httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(transport)))
    admin._supervision_schemes['fixture_a'] = 'http'
    async with real_client(transport=httpx.ASGITransport(app=admin.app), base_url='http://admin.fixture') as client:
        response = await client.post('/api/supervision/fixture_a/permissions/id', json={'allow': True}, headers={'X-Loom-Admin': '1'})
        assert response.status_code == 502
        assert len(requests) == 1 and requests[0].url.scheme == 'http'
        data = (await client.get('/api/supervision')).json()
        assert all(not item['available'] and item['error'] for item in data['instances'])
        assert sum(r.method == 'POST' for r in requests) == 1


async def test_legacy_cancel_never_contacts_any_instance(monkeypatch):
    post = AsyncMock(side_effect=AssertionError('An unscoped mutation must never be relayed'))
    monkeypatch.setattr(admin, '_post_instance', post)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin.app), base_url='http://admin.fixture') as client:
        result = await client.post('/api/generations-proxy/88/kill')
    assert result.status_code == 410
    assert 'instance' in result.json()['detail'].lower()
    post.assert_not_called()


async def test_cancel_timeout_covers_provider_teardown_without_mutation_retry(monkeypatch):
    requests = []
    real_client = httpx.AsyncClient
    def transport(request):
        requests.append(request)
        assert request.url.port == 61012
        assert request.extensions['timeout']['read'] > 12 + 8 + 4
        raise httpx.ReadTimeout('ambiguous cancellation', request=request)
    monkeypatch.setattr(admin.httpx, 'AsyncClient', lambda **kwargs: real_client(
        transport=httpx.MockTransport(transport), **kwargs))
    admin._supervision_schemes['fixture_b'] = 'http'
    async with real_client(transport=httpx.ASGITransport(app=admin.app), base_url='http://admin.fixture') as client:
        response = await client.post('/api/supervision/fixture_b/generations/88/kill', headers={'X-Loom-Admin': '1'})
    assert response.status_code == 502
    assert len(requests) == 1


async def test_supervision_reports_cancellation_even_with_an_unresolved_permission(tmp_path):
    conv, draft, payload, request_id, task = await make_waiter(tmp_path)
    server._active_generations[(conv['id'], draft['parent_id'], 9090)] = SimpleNamespace(
        done=lambda: False, _stop_disposition='cancel')
    try:
        async with main_client() as client:
            data = (await client.get('/api/admin/supervision')).json()
        assert data['generations'][0]['status'] == 'cancelling'
        assert data['permissions'][0]['request_id'] == request_id
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_permission_ci_only_invokes_existing_test_files():
    import re
    workflow = Path('.github/workflows/permission-policy.yml').read_text(encoding='utf-8')
    invocations = re.findall(r'python -m pytest ([^\n]+)', workflow)
    assert invocations
    for invocation in invocations:
        for path in re.findall(r'tests/[\w/]+\.py', invocation):
            assert Path(path).is_file(), f'CI invokes missing test: {path}'
    assert 'tests/test_admin_supervision_http.py' in workflow
    assert 'hermes' not in workflow


async def test_admin_cannot_promote_a_one_time_permission(tmp_path):
    conv, draft, payload, request_id, task = await make_waiter(tmp_path)
    try:
        server._pending_hook_permissions[request_id]['supports_allow_all'] = False
        async with main_client() as client:
            response = await client.post(f'/api/admin/permissions/{request_id}', json={
                'allow': True, 'always': True, 'conv_id': conv['id'], 'run_id': 'gen:9090'})
            assert response.status_code == 422
            assert not server._permission_rules and not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('width', [390, 800, 1280])
async def test_activity_browser_preserves_notes_links_and_card_width(tmp_path, width):
    from playwright.async_api import async_playwright, expect
    root = Path(__file__).resolve().parents[1] / 'static' / 'admin'
    long = 'long-path-or-model-' * 40
    item = {'request_id': 'fixture-request', 'conv_id': 55, 'branch_id': 88, 'run_id': 'gen:77',
            'title': 'A workspace <not markup>', 'project_dir': long, 'model': long, 'mode': 'goose',
            'tool_name': 'Bash', 'tool_input': {'command': long}, 'input_summary': long,
            'rule_preview': 'Only this workspace/branch and command shape.', 'supports_allow_all': True}
    instance = {'instance': 'fixture_a', 'label': 'Fixture A', 'scheme': 'https', 'port': 61011,
                'available': True, 'permissions': [item], 'generations': [{**item, 'status': 'awaiting_permission', 'draft_msg_id': 88}]}
    posts, urls, cancellations = [], [], []
    async def route_handler(route):
        request = route.request
        path = urlparse(request.url).path
        urls.append(path)
        if path == '/':
            return await route.fulfill(body=(root / 'index.html').read_text(encoding='utf-8'), content_type='text/html')
        if path.startswith('/assets/'):
            file = root / path.split('/')[-1]
            return await route.fulfill(body=file.read_text(encoding='utf-8'), content_type='text/css' if file.suffix == '.css' else 'application/javascript')
        if path == '/api/supervision':
            return await route.fulfill(json={'instances': [instance]})
        if '/permissions/' in path:
            posts.append(request.post_data_json)
            instance['permissions'] = []
            return await route.fulfill(json={'resolved': True})
        if path.endswith('/generations/88/kill'):
            cancellations.append(path)
            instance['generations'][0]['status'] = 'cancelling'
            return await route.fulfill(status=202, json={'status': 'cancelling'})
        if path == '/api/db-storage':
            return await route.fulfill(json={'folder': long, 'filename': long, 'resolved': long})
        if path == '/api/meta':
            return await route.fulfill(json={'admin_port': 61010, 'main_port': 61011, 'test_port': 61012,
                                            'llama_port': 61111, 'ttyd': {'port': 61112}})
        if path == '/api/llama-models':
            return await route.fulfill(json={'models': [long], 'current': long})
        if path == '/api/memory-services':
            return await route.fulfill(json={'services': [{'key': 'llama', 'state': 'ready', 'loaded_models': [long],
                'gpu_mb': 1000, 'active_ram_mb': 1000, 'reserved_ram_mb': 1000, 'cache_mb': 1000}]})
        if path == '/tools/system-specs':
            return await route.fulfill(json={'status': 'ok_html', 'output': f'<div class="specs-grid"><div class="spec-card"><div class="spec-row"><span class="spec-lbl">GPU</span><span class="spec-val">{long}</span></div></div></div>'})
        if path.startswith('/api/'):
            return await route.fulfill(json={})
        await route.abort()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(viewport={'width': width, 'height': 900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.add_init_script("localStorage.setItem('loom-admin-view', 'activity')")
        await page.route('**/*', route_handler)
        await page.goto('http://admin.fixture/')
        note = page.locator('.permission-card textarea')
        await expect(note).to_be_visible()
        await note.fill('Please use the review copy.')
        await page.locator('.permission-card summary').click()
        await note.focus()
        await page.evaluate('refreshSupervision()')
        await expect(note).to_have_value('Please use the review copy.')
        await expect(note).to_be_focused()
        assert await page.locator('.permission-card details').get_attribute('open') is not None
        assert await page.get_by_role('link', name='Chat', exact=True).first.get_attribute('href') == 'https://admin.fixture:61011/?conversation=55&view=chat&branch=88'
        assert await page.locator('[data-view="terminal"], [data-view="tools"], [data-view="cron"]').count() == 0
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        instance['available'] = False
        await page.evaluate('refreshSupervision()')
        await expect(page.get_by_role('button', name='Allow once')).to_be_disabled()
        await expect(note).to_have_value('Please use the review copy.')
        instance['available'] = True
        await page.evaluate('refreshSupervision()')
        await page.get_by_role('button', name='Allow for Branch', exact=True).click()
        await expect(page.locator('.permission-card')).to_have_count(0)
        assert posts == [{'conv_id': 55, 'run_id': 'gen:77', 'allow': True, 'always': True, 'feedback': 'Please use the review copy.'}]
        cancel = page.locator('[data-kill-instance="fixture_a"]')
        await cancel.click()
        await page.locator('[data-confirm-ok]').click()
        await expect(cancel).to_have_text('Cancelling…')
        await page.evaluate('refreshSupervision()')
        await expect(cancel).to_be_disabled()
        # Offline is not proof of termination. Preserve the pending action,
        # and do not offer/replay another cancellation after reconnection.
        instance['available'] = False
        await page.evaluate('refreshSupervision()')
        assert await page.evaluate('supervisionCancellations.size') == 1
        instance['available'] = True
        await page.evaluate('refreshSupervision()')
        await expect(cancel).to_be_disabled()
        instance['generations'] = []
        await page.evaluate('refreshSupervision()')
        await expect(cancel).to_have_count(0)
        assert await page.evaluate('supervisionCancellations.size') == 0
        assert cancellations == ['/api/supervision/fixture_a/generations/88/kill']
        await page.locator('[data-view="overview"]').click()
        await expect(page.locator('#db-folder')).to_have_value(long)
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        assert not any('cron' in url or 'ttyd' in url for url in urls)
        assert errors == []
        await browser.close()
