"""Isolated quota probes and browser warnings; no live accounts or host lifecycle."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from playwright.async_api import async_playwright, expect

import provider_usage as usage


@pytest.fixture(autouse=True)
def no_live_subscription_calls(monkeypatch):
    monkeypatch.setattr(usage, 'read_claude_quota', AsyncMock(side_effect=usage.QuotaReadError('Fixture OAuth unavailable.')))
    monkeypatch.setattr(usage, 'read_antigravity_quota', AsyncMock(side_effect=usage.QuotaReadError('Fixture OAuth unavailable.')))


def quota(percent=85):
    return {"rateLimits": {"primary": {"usedPercent": percent, "windowDurationMins": 300,
                                       "resetsAt": 9000}, "secondary": None}}


def test_windows_preserve_unknown_zero_multiple_buckets_and_durations():
    assert usage.codex_windows(quota(0))[0]['used_percent'] == 0
    assert usage.codex_windows(quota(None)) == []
    assert usage.codex_windows(quota(True)) == []
    assert usage.codex_windows(quota(float('nan'))) == []
    data = {'rateLimitsByLimitId': {'codex': {'primary': {'usedPercent': 42, 'windowDurationMins': 10080}},
                                   'other': {'secondary': {'usedPercent': 95, 'windowDurationMins': 15}}}}
    rows = usage.codex_windows(data)
    assert [row['label'] for row in rows] == ['codex: Weekly', 'other: 15 min']
    assert rows[0]['resets_at'] is None


async def test_single_flight_cache_failure_retains_stale_and_never_leaks_errors(monkeypatch):
    clock = [1000]
    monkeypatch.setattr(usage.time, 'time', lambda: clock[0])
    monkeypatch.setattr(usage, 'passive_claude', lambda now: {'provider': 'claude', 'windows': [], 'note': 'Fixture cache unavailable.'})
    read = AsyncMock(return_value=quota())
    monkeypatch.setattr(usage, 'read_codex_quota', read)
    monitor = usage.UsageMonitor()
    snapshots = await asyncio.gather(*(monitor.snapshot() for _ in range(8)))
    assert read.await_count == 1
    assert snapshots[0]['providers'][0]['status'] == 'ok'
    clock[0] += 301
    read.side_effect = RuntimeError('PRIVATE_TOKEN_FIXTURE')
    stale = await monitor.snapshot()
    assert stale['providers'][0]['status'] == 'stale'
    assert stale['providers'][0]['observed_at'] == 1000
    assert stale['providers'][0]['windows'][0]['used_percent'] == 85
    assert 'PRIVATE_TOKEN_FIXTURE' not in json.dumps(stale)
    await monitor.snapshot()
    assert read.await_count == 2
    clock[0] += 301
    read.side_effect = None
    read.return_value = quota(0)
    recovered = await monitor.snapshot()
    assert recovered['providers'][0]['status'] == 'ok'
    assert recovered['providers'][0]['windows'][0]['used_percent'] == 0


def test_passive_cache_fresh_stale_invalid_and_allowlisted(monkeypatch, tmp_path):
    cache = tmp_path / 'claude.json'
    monkeypatch.setattr(usage, 'state_path', lambda *args: cache)
    assert usage.passive_claude(1000)['status'] == 'unavailable'
    cache.write_text(json.dumps({'observed_at': 1000, 'token': 'PRIVATE_TOKEN_FIXTURE',
                                'rate_limits': {'five_hour': {'used_percentage': 90, 'resets_at': 5000}}}))
    result = usage.passive_claude(1001)
    assert result['status'] == 'ok'
    assert 'PRIVATE_TOKEN_FIXTURE' not in json.dumps(result)
    assert usage.passive_claude(2000)['status'] == 'stale'
    assert usage.passive_claude(500)['status'] == 'unavailable'
    cache.write_text('[]')
    assert usage.passive_claude(1000)['status'] == 'unavailable'


async def test_cancelled_first_probe_can_retry(monkeypatch):
    read = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(usage, 'read_codex_quota', read)
    monkeypatch.setattr(usage, 'passive_claude', lambda now: {'provider': 'claude', 'windows': [], 'note': 'Fixture cache unavailable.'})
    monitor = usage.UsageMonitor()
    with pytest.raises(asyncio.CancelledError):
        await monitor.snapshot()
    read.side_effect = None
    read.return_value = quota(0)
    assert (await monitor.snapshot())['providers'][0]['status'] == 'ok'


async def test_empty_codex_response_retains_previous_observation(monkeypatch):
    clock = [1000]
    monkeypatch.setattr(usage.time, 'time', lambda: clock[0])
    read = AsyncMock(return_value=quota(42))
    monkeypatch.setattr(usage, 'read_codex_quota', read)
    monitor = usage.UsageMonitor()
    await monitor.snapshot()
    clock[0] += 300
    read.return_value = {}
    result = (await monitor.snapshot())['providers'][0]
    assert result['status'] == 'stale'
    assert result['observed_at'] == 1000
    assert result['windows'][0]['used_percent'] == 42


def test_statusline_exporter_atomic_projection_and_missing_fields(monkeypatch, tmp_path):
    from tools import claude_usage_statusline as exporter
    cache = tmp_path / 'state' / 'claude.json'
    monkeypatch.setattr(exporter, 'state_path', lambda *args: cache)
    assert '90% used' in exporter.export({'token': 'PRIVATE_TOKEN_FIXTURE', 'rate_limits': {
        'five_hour': {'used_percentage': 90, 'resets_at': 9999, 'email': 'PRIVATE_TOKEN_FIXTURE'}}})
    assert 'PRIVATE_TOKEN_FIXTURE' not in cache.read_text()
    exporter.export({})
    assert json.loads(cache.read_text())['rate_limits'] == {}
    assert list(cache.parent.iterdir()) == [cache]


class FakeStdin:
    def __init__(self):
        self.sent = []
        self.closed = False

    def write(self, data):
        self.sent.append(json.loads(data))

    async def drain(self):
        pass

    def close(self):
        self.closed = True


@pytest.mark.parametrize('failure', [False, True])
async def test_rpc_only_handshake_and_quota_always_cleans_up(monkeypatch, failure):
    import codex_client
    stdout = asyncio.StreamReader()
    responses = [{'id': 1, 'result': {}}, {'method': 'notification', 'params': {}},
                 {'id': 77, 'method': 'tool/approval', 'params': {}},
                 {'id': 2, 'error': {'message': 'PRIVATE_FIXTURE'}} if failure else {'id': 2, 'result': quota()}]
    for response in responses:
        stdout.feed_data((json.dumps(response) + '\n').encode())
    stdout.feed_eof()
    proc = type('Proc', (), {'stdin': FakeStdin(), 'stdout': stdout})()
    launch = AsyncMock(return_value=proc)
    cleanup = AsyncMock()
    monkeypatch.setattr(usage.asyncio, 'create_subprocess_exec', launch)
    monkeypatch.setattr(codex_client, 'cancel_codex', cleanup)
    monkeypatch.setattr(codex_client, '_find_codex_exe', lambda: 'fixture-codex')
    if failure:
        with pytest.raises(RuntimeError, match='Quota RPC unavailable'):
            await usage.read_codex_quota()
    else:
        assert await usage.read_codex_quota() == quota()
    assert [item.get('method') for item in proc.stdin.sent] == ['initialize', 'initialized', 'account/rateLimits/read', None]
    assert proc.stdin.sent[-1]['error']['code'] == -32601
    assert proc.stdin.closed
    cleanup.assert_awaited_once_with(proc)
    assert '--disable' in launch.call_args.args


async def test_rpc_cancellation_still_cleans_up(monkeypatch):
    import codex_client
    proc = type('Proc', (), {'stdin': FakeStdin(), 'stdout': asyncio.StreamReader()})()
    monkeypatch.setattr(usage.asyncio, 'create_subprocess_exec', AsyncMock(return_value=proc))
    cleanup = AsyncMock()
    monkeypatch.setattr(codex_client, 'cancel_codex', cleanup)
    task = asyncio.create_task(usage.read_codex_quota())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.stdin.closed
    cleanup.assert_awaited_once_with(proc)


async def test_admin_proxies_only_main_and_reports_missing_endpoint(monkeypatch):
    import admin_server as admin
    monkeypatch.setattr(admin, 'INSTANCES', {'main': {'port': 61011}, 'test': {'port': 61012}})
    upstream = AsyncMock(return_value=httpx.Response(200, json={'providers': [], 'poll_seconds': 300}))
    monkeypatch.setattr(admin, '_get_instance', upstream)
    response = await admin.api_provider_usage()
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    upstream.assert_awaited_once_with(61011, '/api/provider-usage', timeout=25.0)
    upstream.side_effect = RuntimeError('PRIVATE_FIXTURE')
    response = await admin.api_provider_usage()
    assert response.status_code == 503
    assert b'PRIVATE_FIXTURE' not in response.body


async def test_main_endpoint_uses_shared_monitor_and_no_store(monkeypatch):
    import server
    snapshot = AsyncMock(return_value={'providers': [], 'poll_seconds': 300})
    monkeypatch.setattr(usage.monitor, 'snapshot', snapshot)
    response = await server.api_get_provider_usage()
    assert response.headers['cache-control'] == 'no-store'
    assert json.loads(response.body)['providers'] == []
    snapshot.assert_awaited_once_with()


def browser_snapshot(now, percent=85):
    return {'providers': [{'provider': 'codex', 'label': 'Codex', 'status': 'ok', 'observed_at': now,
                           'note': 'Host account', 'windows': [{'label': '5-hour', 'used_percent': percent,
                                                              'resets_at': now + 3600}]}], 'stale_seconds': 900}


async def test_main_warning_threshold_reset_expiry_stale_and_recovery():
    script = Path('static/admin/provider-usage.js').read_text(encoding='utf-8')
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.clock.install()
            await page.route('**/*', lambda route: route.fulfill(body='<header id="app-header"></header>', content_type='text/html'))
            await page.goto('http://quota.fixture/')
            now = await page.evaluate('Date.now() / 1000')
            state = {'body': browser_snapshot(now)}
            await page.route('**/api/provider-usage', lambda route: route.fulfill(json=state['body'], status=state.get('status', 200)))
            await page.add_script_tag(content=script)
            banner = page.locator('#provider-usage-warning')
            await expect(banner).to_contain_text('85% used')
            state['body'] = browser_snapshot(now, 79)
            await page.evaluate('loadProviderUsage(true)')
            await expect(banner).to_be_hidden()
            state['body'] = browser_snapshot(now, 90)
            await page.evaluate('loadProviderUsage(true)')
            await expect(banner).to_contain_text('90% used')
            await expect(banner).to_have_class('critical')
            state['body']['providers'][0]['windows'][0]['resets_at'] = now - 1
            await page.evaluate('loadProviderUsage(true)')
            await expect(banner).to_contain_text('stale')
            state['body'] = browser_snapshot(now, 95)
            state['status'] = 503
            await page.evaluate('loadProviderUsage(true)')
            await expect(banner).to_contain_text('stale')
            state['status'] = 200
            state['body'] = browser_snapshot(now, 0)
            await page.evaluate('loadProviderUsage(true)')
            await expect(banner).to_be_hidden()
            state['body']['providers'].append({'provider': 'claude', 'label': 'Claude Code',
                                              'status': 'unavailable', 'windows': [], 'observed_at': None})
            await page.evaluate('loadProviderUsage(true)')
            await expect(banner).to_contain_text('unavailable or stale')
            state['body']['providers'][0]['windows'][0]['used_percent'] = 90
            await page.evaluate('loadProviderUsage(true)')
            await expect(banner).to_contain_text('90% used')
            await expect(banner).to_contain_text('unavailable or stale for Claude Code')
            await expect(banner).not_to_contain_text('stale for Codex')
            assert not errors
        finally:
            await browser.close()


async def test_admin_cards_unavailable_narrow_layout_and_refresh():
    from tests.test_admin_restart_browser import dashboard
    async with dashboard({'pid': 101}) as (page, posts, _):
        now = await page.evaluate('Date.now() / 1000')
        data = browser_snapshot(now)
        data['providers'].append({'label': 'Antigravity', 'status': 'unavailable', 'observed_at': None,
                                  'windows': [], 'note': 'No verified source'})
        await page.route('**/api/provider-usage', lambda route: route.fulfill(json=data))
        await page.evaluate("specsLoaded = true; setView('quotas'); loadProviderUsage(true)")
        card = page.locator('#provider-usage-monitor')
        await expect(card).to_contain_text('85% used')
        await expect(card).to_contain_text('Unavailable')
        await expect(card.locator('progress')).to_have_attribute('value', '85')
        await page.set_viewport_size({'width': 390, 'height': 844})
        assert await card.evaluate('(el) => el.scrollWidth <= el.clientWidth')
        assert not posts


async def test_admin_agy_representatives_and_extension_diagnostics():
    from tests.test_admin_restart_browser import dashboard
    async with dashboard({'pid': 101}) as (page, posts, _):
        now = await page.evaluate('Date.now()/1000')
        data = {'providers': [{'provider': 'antigravity', 'label': 'Antigravity',
            'status': 'ok', 'observed_at': now, 'windows': [
                {'label': 'Gemini Alpha', 'used_percent': 99, 'resets_at': now - 1},
                {'label': 'Gemini Flash', 'used_percent': 25, 'resets_at': now + 300},
                {'label': 'Gemini Pro', 'used_percent': 70, 'resets_at': now + 300},
                {'label': 'GPT-OSS', 'used_percent': 40, 'resets_at': now + 600},
            ]}]}
        await page.route('**/api/provider-usage', lambda route: route.fulfill(json=data))
        await page.evaluate("specsLoaded=true; setView('quotas'); loadProviderUsage(true)")
        cards = page.locator('#provider-usage-cards')
        await expect(cards.locator('.quota-provider')).to_have_count(2)
        await expect(cards.locator('progress')).to_have_count(2)
        await expect(cards.locator('progress').nth(0)).to_have_attribute('value', '25')
        await expect(cards.locator('progress').nth(1)).to_have_attribute('value', '40')
        await expect(cards).to_contain_text('AGY')
        await expect(cards).to_contain_text('ExoGoo')
        await expect(cards).not_to_contain_text('Weekly')
        data['providers'][0]['windows'] = data['providers'][0]['windows'][:3]
        await page.locator('#btn-provider-usage-refresh').click()
        await expect(cards.locator('progress')).to_have_count(1)
        await expect(cards).to_contain_text('No quota data available')
        from tests.test_provider_quota_sources import agy_summary
        data['providers'][0]['windows'] = usage.antigravity_quota_windows(agy_summary())
        for row in data['providers'][0]['windows']:
            row['resets_at'] = now + 600
        await page.locator('#btn-provider-usage-refresh').click()
        await expect(cards.locator('progress')).to_have_count(4)
        for index, pool_label in enumerate(['AGY', 'ExoGoo']):
            pool = cards.locator('.quota-provider').nth(index)
            await expect(pool).to_contain_text(pool_label)
            await expect(pool).to_contain_text('5-hour')
            await expect(pool).to_contain_text('Weekly')
        assert await cards.locator('progress').evaluate_all('(els) => els.map(el => Number(el.value))') == [20, 90, 0, 51]
        # Both the main popup and admin use this renderer; preserve both windows
        # in narrow layouts too.
        await page.set_viewport_size({'width': 390, 'height': 844})
        assert await cards.evaluate('(el) => el.scrollWidth <= el.clientWidth')
        await page.route('**/api/extensions', lambda route: route.fulfill(json={'extensions': [{
            'id':'fixture', 'name':'Fixture extension', 'kind':'service',
            'workspace_root':'C:/Fixture', 'manifest_path':'C:/Fixture/manifest.json',
            'cwd':'C:/Fixture/app', 'command':['python','<entry>.py'],
            'health_url':'http://127.0.0.1:9999/health',
            'health':{'state':'unhealthy','http_status':503,'detail':'fixture unavailable'}}]}))
        await page.evaluate("setView('extensions')")
        await page.locator('.extension-diagnostics summary').click()
        await expect(page.locator('.extension-diagnostics')).to_contain_text('503')
        await expect(page.locator('.extension-diagnostics')).to_contain_text('<entry>.py')
        assert await page.locator('.extension-diagnostics entry').count() == 0
        assert not posts


async def test_admin_quota_loading_failure_retention_and_recovery():
    from tests.test_admin_restart_browser import dashboard
    async with dashboard({'pid': 101}) as (page, posts, _):
        release = asyncio.Event()
        state = {'status': 503, 'body': {'error': 'Fixture unavailable'}}

        async def respond(route):
            await release.wait()
            await route.fulfill(status=state['status'], json=state['body'])

        await page.route('**/api/provider-usage', respond)
        await page.evaluate("specsLoaded = true; setView('quotas'); void loadProviderUsage(true)")
        status = page.locator('#provider-usage-status')
        button = page.locator('#btn-provider-usage-refresh')
        await expect(status).to_contain_text('Checking quota observations')
        await expect(button).to_be_disabled()
        release.set()
        await expect(status).to_contain_text('Quota monitor unavailable')
        await expect(button).to_be_enabled()
        state.update(status=200, body=browser_snapshot(await page.evaluate('Date.now() / 1000')))
        await button.click()
        await expect(page.locator('#provider-usage-cards')).to_contain_text('85% used')
        state['status'] = 503
        await button.click()
        await expect(status).to_contain_text('previous observations retained')
        await expect(page.locator('#provider-usage-cards')).to_contain_text('(stale)')
        state.update(status=200, body=browser_snapshot(await page.evaluate('Date.now() / 1000'), 0))
        await button.click()
        await expect(page.locator('#provider-usage-cards')).to_contain_text('0% used')
        await expect(page.locator('#provider-usage-cards')).not_to_contain_text('(stale)')
        assert not posts
