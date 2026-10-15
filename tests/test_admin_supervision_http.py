"""Browser -> admin HTTP -> main HTTP -> permission waiter, no live hosts/models.

Only whitelisted production routes are served on owned ephemeral sockets;
host lifecycle routes/schedulers are not present. The provider is deterministic.
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
import socket
from urllib.parse import urlparse

import httpx
import pytest
from fastapi import FastAPI, Request
import uvicorn

import admin_server as admin
import database as db
import server
from tests.test_admin_supervision import isolated_runtime  # isolated autouse fixture


@asynccontextmanager
async def listener(app):
    class TestServer(uvicorn.Server):
        @contextmanager
        def capture_signals(self):
            yield
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.setblocking(False)
    port = sock.getsockname()[1]
    instance = TestServer(uvicorn.Config(app, log_level='error', lifespan='off',
                                       timeout_graceful_shutdown=2, ws='none'))
    task = asyncio.create_task(instance.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(8):
            while not instance.started:
                if task.done():
                    await task
                await asyncio.sleep(.01)
        yield port
    finally:
        instance.should_exit = True  # only this test-owned Uvicorn object
        try:
            await asyncio.wait_for(task, 8)
        finally:
            sock.close()


def restricted_app(source, paths):
    app = FastAPI()
    app.router.routes[:] = [route for route in source.routes if getattr(route, 'path', None) in paths]
    assert {r.path for r in app.routes} == paths
    assert not any('restart' in r.path or 'shutdown' in r.path for r in app.routes)
    return app


async def test_browser_http_permissions_and_slow_cancel_are_exact_instance(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright, expect
    main = restricted_app(server.app, {
        '/api/cc-permission', '/api/admin/supervision', '/api/admin/permissions/{request_id}',
        '/api/generations/{draft_msg_id}/kill'})
    dashboard = restricted_app(admin.app, {
        '/', '/assets/{filename}', '/api/supervision',
        '/api/supervision/{instance}/permissions/{request_id}',
        '/api/supervision/{instance}/generations/{draft_id}/kill',
        '/api/generations-proxy/{draft_msg_id}/kill'})
    # Unrelated dashboard status APIs never query the host or local sidecars.
    @dashboard.get('/api/{name}')
    async def fixture_metadata(name: str):
        if name == 'meta':
            return {'ttyd': {'port': 1}, 'main_port': 1, 'test_port': 1, 'llama_port': 1}
        return {}

    main_posts, other_posts = [], []
    @main.middleware('http')
    async def capture_main(request: Request, call_next):
        if request.method == 'POST':
            main_posts.append(request.url.path)
        return await call_next(request)

    other = FastAPI()
    other_snapshot = {'generations': [], 'permissions': []}
    @other.get('/api/admin/supervision')
    async def other_metadata():
        return other_snapshot
    @other.post('/{path:path}')
    async def wrong_owner(path: str):
        other_posts.append(path)
        raise AssertionError('A colliding ID on another instance must not receive a mutation')

    conv = await db.create_conversation('HTTP approval fixture', mode='goose', project_dir=str(tmp_path))
    # This fixture requires a pending approval, independent of owner defaults.
    await db.update_conversation_fields(conv['id'], permission_profile='balanced')
    user = await db.add_message(conv['id'], 'user', 'keep this prompt')
    draft = await db.add_message(conv['id'], 'assistant', 'partial fixture', parent_id=user['id'])
    queued = await db.add_message(conv['id'], 'user', 'queued fixture', parent_id=draft['id'])
    await db.enqueue_generation(queued['id'], conv['id'], draft['id'])
    await db.register_active_generation(draft['id'], conv['id'], 0, mode='goose')
    key = (conv['id'], user['id'], 9090)
    server._generation_snapshots[key] = {'draft_msg_id': draft['id'], 'parent_id': user['id']}
    started, stopping = asyncio.Event(), asyncio.Event()
    async def provider_fixture():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopping.set()
            await asyncio.sleep(11)  # longer than the OLD admin proxy timeout
            await server._finalize_user_stopped_generation(conv['id'], draft['id'])
        finally:
            await db.unregister_active_generation(draft['id'])
    provider = asyncio.create_task(provider_fixture())
    server._active_generations[key] = provider
    pending_tools = []
    blocked_urls, browser_errors = [], []
    try:
        await started.wait()
        async with listener(main) as main_port, listener(other) as other_port, listener(dashboard) as admin_port:
            monkeypatch.setattr(admin, 'INSTANCES', {
                'fixture_a': {'port': main_port, 'label': 'Fixture A'},
                'fixture_b': {'port': other_port, 'label': 'Fixture B'}})
            monkeypatch.setattr(admin, '_supervision_schemes', {'fixture_a': 'http', 'fixture_b': 'http'})
            # Guard all Python HTTP at the transport boundary, without faking responses.
            original_send = httpx.AsyncClient.send
            async def isolated_send(client, request, **kwargs):
                assert request.url.host == '127.0.0.1' and request.url.port in {main_port, other_port, admin_port}
                return await original_send(client, request, **kwargs)
            monkeypatch.setattr(httpx.AsyncClient, 'send', isolated_send)
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{main_port}', timeout=30) as tool_client:
                payload = {'loom_conv_id': conv['id'], 'permission_scope': 'gen:9090', 'source': 'goose_acp',
                           'tool_name': 'Bash', 'tool_input': {'command': 'python -m pytest tests/a.py -q', 'cwd': str(tmp_path)}}
                async def tool_request(data):
                    response = await tool_client.post('/api/cc-permission', json=data)
                    assert response.status_code == 200
                    return response.json()
                tool = asyncio.create_task(tool_request(payload))
                pending_tools.append(tool)
                async with asyncio.timeout(8):
                    while not server._pending_hook_permissions:
                        if tool.done():
                            raise AssertionError(await tool)
                        await asyncio.sleep(.01)
                request_id = next(iter(server._pending_hook_permissions))
                # Same IDs on a second server: permission and Cancel must stay on A.
                other_snapshot.update(generations=[{'conv_id': conv['id'], 'draft_msg_id': draft['id'],
                    'branch_id': draft['id'], 'title': 'Other fixture', 'status': 'running'}],
                    permissions=[{'request_id': request_id, 'conv_id': conv['id'], 'run_id': 'gen:9090',
                                  'branch_id': draft['id'], 'title': 'Other fixture', 'tool_name': 'Read'}])
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(headless=True)
                    page = await browser.new_page(viewport={'width': 800, 'height': 900})
                    page.on('pageerror', lambda error: browser_errors.append(str(error)))
                    async def guard_browser(route):
                        url = urlparse(route.request.url)
                        if url.hostname == 'fonts.googleapis.com':
                            return await route.fulfill(body='', content_type='text/css')
                        if url.hostname != '127.0.0.1' or url.port not in {main_port, other_port, admin_port}:
                            blocked_urls.append(route.request.url)
                            return await route.abort()
                        await route.continue_()
                    await page.route('**/*', guard_browser)
                    await page.add_init_script("localStorage.setItem('loom-admin-view', 'activity')")
                    await page.goto(f'http://127.0.0.1:{admin_port}/')
                    card = page.locator('.permission-card').filter(has_text='HTTP approval fixture')
                    await expect(card).to_be_visible()
                    assert await card.get_by_role('link', name='Chat', exact=True).get_attribute('href') == (
                        f'http://127.0.0.1:{main_port}/?conversation={conv["id"]}&view=chat&branch={draft["id"]}')
                    await card.locator('textarea').fill('Workspace test actions only.')
                    await page.evaluate('refreshSupervision()')
                    await expect(card.locator('textarea')).to_have_value('Workspace test actions only.')
                    await card.get_by_role('button', name='Allow for Branch', exact=True).click()
                    decision = await asyncio.wait_for(tool, 8)
                    assert decision['allow'] and decision['decision_source'] == 'user'
                    await expect(card).to_have_count(0)
                    # Reload the actual dashboard; grant matching stays on the main server.
                    await page.reload()
                    followup = {**payload, 'tool_input': {'command': 'python -m pytest tests/b.py -q', 'cwd': str(tmp_path)}}
                    decision = await tool_request(followup)
                    assert decision['allow'] and decision['decision_source'] == 'loom_policy'
                    denied = asyncio.create_task(tool_request({**payload, 'tool_name': 'Read',
                        'tool_input': {'path': str(tmp_path.parent / 'outside-fixture.txt')}}))
                    pending_tools.append(denied)
                    async with asyncio.timeout(8):
                        while not any(p.get('tool_name') == 'Read' and p.get('response') is None
                                      for p in server._pending_hook_permissions.values()):
                            if denied.done():
                                raise AssertionError(await denied)
                            await asyncio.sleep(.01)
                    # A reload's coalesced refresh may have started before this
                    # permission existed. Finish it, then take a fresh snapshot.
                    await page.evaluate('async () => { if (supervisionRefresh) await supervisionRefresh; await refreshSupervision(); }')
                    await expect(card).to_be_visible()
                    await card.locator('textarea').fill('Stay inside the fixture workspace.')
                    await card.get_by_role('button', name='Deny', exact=True).click()
                    decision = await asyncio.wait_for(denied, 8)
                    assert not decision['allow'] and decision['decision_source'] == 'user'
                    assert decision['feedback'] == 'Stay inside the fixture workspace.'
                    # Unscoped legacy request is retired without touching either instance.
                    response = await tool_client.post(f'http://127.0.0.1:{admin_port}/api/generations-proxy/{draft["id"]}/kill')
                    assert response.status_code == 410 and not stopping.is_set()
                    cancel = page.locator(f'[data-kill-instance="fixture_a"][data-draft="{draft["id"]}"]')
                    await cancel.click()
                    await page.locator('[data-confirm-ok]').click()
                    await asyncio.wait_for(stopping.wait(), 8)
                    await page.evaluate('refreshSupervision()')
                    await expect(cancel).to_be_disabled()
                    await expect(cancel).to_have_text('Cancelling…')
                    await page.evaluate('refreshSupervision()')
                    await expect(cancel).to_be_disabled()
                    await asyncio.wait_for(asyncio.shield(provider), 15)
                    await expect(cancel).to_have_count(0, timeout=10000)
                    assert main_posts.count(f'/api/generations/{draft["id"]}/kill') == 1
                    assert other_posts == []
                    assert (await db.get_message(user['id']))['content'] == 'keep this prompt'
                    assert (await db.get_message(queued['id']))['parent_id'] == draft['id']
                    assert (await db.list_queued_generations(conv['id']))[0]['status'] == 'paused'
                    assert 'Response unavailable' not in await page.locator('#toast').inner_text()
                    assert browser_errors == [] and blocked_urls == []
                    await browser.close()
    finally:
        for task in [*pending_tools, provider]:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending_tools, provider, return_exceptions=True)
