"""All browser traffic is fulfilled in-memory. No live lifecycle endpoint exists."""
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.async_api import async_playwright, expect


@asynccontextmanager
async def dashboard(state):
    root = Path(__file__).resolve().parents[1] / 'static' / 'admin'
    posts, navigations = [], []

    async def route_handler(route):
        request = route.request
        path = urlparse(request.url).path
        if urlparse(request.url).hostname != 'admin.fixture':
            return await route.abort()
        if path == '/':
            navigations.append(path)
            return await route.fulfill(body=(root / 'index.html').read_text(encoding='utf-8'), content_type='text/html')
        if path.startswith('/assets/'):
            file = root / path.rsplit('/', 1)[-1]
            return await route.fulfill(body=file.read_text(encoding='utf-8'), content_type='text/css' if file.suffix == '.css' else 'application/javascript')
        if request.method != 'GET':
            posts.append(path)
            assert path == '/admin/restart', 'Unexpected mutation in isolated dashboard'
            if state.get('disconnect'):
                return await route.abort('connectionreset')
            if state.get('post_html'):
                return await route.fulfill(status=state.get('post_status', 200), body='<html>fixture error</html>', content_type='text/html')
            return await route.fulfill(status=state.get('post_status', 200), json=state.get('post_body', {'old_pid': 101, 'status': 'restarting'}))
        if path == '/api/admin-status':
            state['identity_gets'] = state.get('identity_gets', 0) + 1
            if state.get('identity_missing'):
                return await route.fulfill(status=404, json={'detail': 'Not Found'})
            if state.get('identity_status', 200) != 200:
                return await route.fulfill(status=state['identity_status'], json={'detail': 'Fixture unavailable'})
            return await route.fulfill(json={'admin_pid': state.get('pid'), 'admin_restart': state.get('helper', {})})
        if path == '/api/status':
            state['instance_gets'] = state.get('instance_gets', 0) + 1
            return await route.fulfill(json={'admin_pid': state.get('pid'), 'admin_restart': state.get('helper', {}), 'instances': []})
        if path == '/api/supervision':
            return await route.fulfill(status=state.get('supervision_status', 200), json=state.get('supervision', {'instances': []}))
        if path == '/api/openrouter/usage':
            state['openrouter_gets'] = state.get('openrouter_gets', 0) + 1
            return await route.fulfill(status=state.get('openrouter_status', 200), json=state.get('openrouter', {
                'api_key_configured': False, 'management_key_configured': False,
                'fetched_at': 1700000000, 'limits': {}, 'current_key': None, 'warnings': [],
            }))
        if path == '/api/meta':
            return await route.fulfill(json={'admin_port': 61010, 'main_port': 61011, 'test_port': 61012, 'llama_port': 61111, 'ttyd': {'port': 61112}})
        return await route.fulfill(json={})

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.route('**/*', route_handler)
            await page.add_init_script("localStorage.setItem('loom-admin-view', 'activity')")
            await page.clock.install()
            await page.goto('http://admin.fixture/')
            await expect(page.locator('#view-activity')).to_have_class('view active')
            yield page, posts, navigations
            assert not errors
        finally:
            await browser.close()


@pytest.mark.parametrize('response', [
    {},  # A successful acknowledgement still requires a replacement PID.
    {'post_status': 202, 'post_body': {'status': 'already requested'}},
    {'post_html': True},
    {'disconnect': True},
])
async def test_restart_never_confirms_unchanged_pid_or_replays_post(response):
    state = {'pid': 101, **response}
    async with dashboard(state) as (page, posts, navigations):
        await page.locator('#btn-admin-restart').click()
        await expect(page.locator('#admin-lifecycle-status')).to_contain_text('checking' if response.get('post_html') or response.get('disconnect') else 'waiting')
        await page.evaluate("adminAction('restart')")  # duplicate click while pending
        await expect(page.locator('#btn-admin-restart')).to_be_disabled()
        await page.clock.fast_forward(41000)
        await expect(page.locator('#admin-lifecycle-status')).to_contain_text('not confirmed')
        assert posts == ['/admin/restart'] and len(navigations) == 1
        await expect(page.locator('#btn-admin-restart')).to_be_enabled()
        await page.clock.fast_forward(5000)
        await expect(page.locator('#admin-lifecycle-status')).to_contain_text('not confirmed')


@pytest.mark.parametrize('pid', [None, '101', 0])
async def test_restart_requires_identity_before_post(pid):
    async with dashboard({'pid': pid}) as (page, posts, navigations):
        await page.locator('#btn-admin-restart').click()
        await expect(page.locator('#admin-lifecycle-status')).to_contain_text('No restart request was sent')
        assert not posts and len(navigations) == 1


async def test_restart_http_error_is_visible_without_reload():
    async with dashboard({'pid': 101, 'post_html': True, 'post_status': 500}) as (page, posts, navigations):
        await page.locator('#btn-admin-restart').click()
        await expect(page.locator('#admin-lifecycle-status')).to_contain_text('HTTP 500')
        await page.clock.fast_forward(45000)
        assert posts == ['/admin/restart'] and len(navigations) == 1


async def test_restart_identity_works_while_instance_poll_is_unresponsive():
    state = {'pid': 101, 'post_status': 500, 'post_body': {'error': 'Fixture refusal'}}
    async with dashboard(state) as (page, posts, _):
        # A stalled instance poll must never gate the admin's own identity.
        await page.route('**/api/status', lambda route: route.abort('timedout'))
        await page.locator('#btn-admin-restart').click()
        await expect(page.locator('#admin-lifecycle-status')).to_contain_text('Fixture refusal')
        assert state['identity_gets'] == 1
        assert posts == ['/admin/restart']


@pytest.mark.parametrize('missing', [False, True])
async def test_identity_fallback_only_for_older_missing_route(missing):
    state = {'pid': 101, 'identity_missing': missing, 'identity_status': 403}
    async with dashboard(state) as (page, posts, _):
        before = state.get('instance_gets', 0)
        if missing:
            assert await page.evaluate('readAdminStatus().then(s => s.admin_pid)') == 101
            assert state['instance_gets'] == before + 1
        else:
            with pytest.raises(Exception, match='HTTP 403'):
                await page.evaluate('readAdminStatus()')
            assert state.get('instance_gets', 0) == before
        assert not posts


async def test_restart_confirms_only_a_valid_new_serving_pid():
    state = {'pid': 101, 'post_status': 202, 'post_body': {'status': 'already requested'}}
    async with dashboard(state) as (page, posts, navigations):
        await page.locator('#btn-admin-restart').click()
        await expect(page.locator('#admin-lifecycle-status')).to_contain_text('waiting')
        state['pid'] = None
        await page.clock.run_for(1500)
        assert len(navigations) == 1
        state['pid'] = 202
        # Activity was already visible on the old page. Wait for navigation,
        # advancing the fake poll clock while asynchronous routes settle;
        # a fixed two-second jump need not include the replacement response.
        for _ in range(10):
            await page.clock.run_for(1000)
            if len(navigations) == 2:
                break
        assert len(navigations) == 2 and posts == ['/admin/restart']
        await expect(page.locator('#view-activity')).to_have_class('view active')


async def test_restart_failed_helper_is_visible_without_replay():
    state = {'pid': 101}
    async with dashboard(state) as (page, posts, navigations):
        await page.locator('#btn-admin-restart').click()
        await expect(page.locator('#admin-lifecycle-status')).to_contain_text('waiting')
        state['helper'] = {'requested': True, 'helper_exit_code': 2}
        await page.clock.run_for(1500)
        await expect(page.locator('#admin-lifecycle-status')).to_contain_text('Restart helper exited (2)')
        assert posts == ['/admin/restart'] and len(navigations) == 1


async def test_unknown_admin_action_cannot_send_a_mutation():
    async with dashboard({'pid': 101}) as (page, posts, navigations):
        await page.evaluate("adminAction('invalid-action')")
        assert not posts and len(navigations) == 1


async def test_activity_404_is_not_reported_as_idle():
    async with dashboard({'pid': 101, 'supervision_status': 404}) as (page, posts, navigations):
        await expect(page.locator('#activity-status')).to_contain_text('admin backend')
        await expect(page.locator('#generations-empty')).to_contain_text('not confirmed idle')
        await expect(page.locator('#permissions-empty')).to_contain_text('not confirmed empty')
        assert not posts


async def test_activity_failure_preserves_notes_and_disables_stale_actions():
    item = {'request_id': 'fixture', 'conv_id': 55, 'branch_id': 88, 'run_id': 'gen:77',
            'title': 'Fixture workspace', 'mode': 'goose', 'tool_name': 'Bash', 'tool_input': {}, 'status': 'running', 'draft_msg_id': 88}
    instance = {'instance': 'fixture_a', 'port': 61011, 'scheme': 'http', 'available': True,
                'permissions': [item], 'generations': [item]}
    state = {'pid': 101, 'supervision': {'instances': [instance]}}
    async with dashboard(state) as (page, posts, navigations):
        note = page.locator('.permission-card textarea')
        await note.fill('Keep this explanation.')
        state['supervision_status'] = 503
        await page.evaluate('refreshSupervision()')
        await expect(note).to_have_value('Keep this explanation.')
        await expect(page.get_by_role('button', name='Allow once', exact=True)).to_be_disabled()
        await expect(page.get_by_role('button', name='Cancel turn', exact=True)).to_be_disabled()
        await expect(page.locator('#activity-summary')).to_have_text('Activity unavailable')
        state['supervision_status'] = 200
        await page.evaluate('refreshSupervision()')
        await expect(page.get_by_role('button', name='Cancel turn', exact=True)).to_be_enabled()
        await expect(page.get_by_role('button', name='Allow once', exact=True)).to_be_enabled()
        await expect(note).to_have_value('Keep this explanation.')
        state['supervision'] = {'not_instances': []}
        await page.evaluate('refreshSupervision()')
        await expect(page.get_by_role('button', name='Cancel turn', exact=True)).to_be_disabled()
        assert not posts
