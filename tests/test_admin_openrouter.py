"""Usage projections and browser counters, with no provider or host traffic."""
import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from playwright.async_api import expect

import admin_server as admin
from tests.test_admin_restart_browser import dashboard


def usage():
    return {
        'api_key_configured': True, 'management_key_configured': True,
        'limits': {'weekly_limit_usd': 12.5, 'monthly_limit_usd': 50},
        'current_key': {'usage_daily': 2, 'usage_weekly': 13, 'usage_monthly': 20,
                        'limit': 75, 'limit_remaining': 55, 'limit_reset': 'monthly'},
        'account': {'total_credits': 100, 'total_usage': 40},
        'fetched_at': 1700000000, 'warnings': [],
    }


@pytest.fixture
def main_only(monkeypatch):
    monkeypatch.setattr(admin, 'INSTANCES', {'main': {'port': 61011}, 'other': {'port': 61012}})


async def test_admin_reads_main_loaded_budgets_and_only_projects_monitor_fields(main_only, monkeypatch):
    data = usage()
    # Provider identity/credential fields must not travel into this monitor.
    data['secrets'] = {'api_key': {'preview': 'PRIVATE_FIXTURE'}}
    data['current_key']['label'] = 'PRIVATE_FIXTURE'
    data['current_key']['creator_user_id'] = 'PRIVATE_FIXTURE'
    upstream = AsyncMock(return_value=httpx.Response(200, json=data))
    monkeypatch.setattr(admin, '_get_instance', upstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin.app), base_url='http://admin') as client:
        response = await client.get('/api/openrouter/usage')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    upstream.assert_awaited_once_with(61011, '/api/openrouter/usage', timeout=12.0)
    result = response.json()
    assert result['current_key']['usage_daily'] == 2
    assert result['current_key']['usage_weekly'] == 13
    assert result['current_key']['usage_monthly'] == 20
    assert result['limits'] == data['limits']
    assert 'daily_limit_usd' not in result['limits']
    assert result['account']['total_credits'] == 100
    assert 'PRIVATE_FIXTURE' not in response.text and 'secrets' not in result


@pytest.mark.parametrize('failure', ['offline', 'http', 'invalid'])
async def test_unavailable_main_never_falls_back_to_other_instance_or_reports_zero(main_only, monkeypatch, failure):
    upstream = AsyncMock()
    if failure == 'offline':
        upstream.side_effect = ConnectionError('PRIVATE_FIXTURE')
    else:
        upstream.return_value = httpx.Response(500 if failure == 'http' else 200, text='PRIVATE_FIXTURE')
    monkeypatch.setattr(admin, '_get_instance', upstream)
    response = await admin.api_openrouter_usage()
    assert response.status_code in (502, 503)
    assert b'PRIVATE_FIXTURE' not in response.body
    assert b'usage_daily' not in response.body
    upstream.assert_awaited_once_with(61011, '/api/openrouter/usage', timeout=12.0)


async def test_partial_missing_and_zero_values_remain_distinct(main_only, monkeypatch):
    data = usage()
    data['current_key'] = {'usage_daily': 0, 'usage_weekly': 'invalid', 'limit': 0,
                           'limit_remaining': 0, 'limit_reset': 'daily'}
    data['account_error'] = 'PRIVATE_FIXTURE'
    monkeypatch.setattr(admin, '_get_instance', AsyncMock(return_value=httpx.Response(200, json=data)))
    import json
    result = json.loads((await admin.api_openrouter_usage()).body)
    assert result['current_key']['usage_daily'] == 0
    assert result['current_key']['usage_weekly'] is None
    assert result['current_key']['usage_monthly'] is None
    assert result['current_key']['limit'] == 0
    assert result['account'] is None
    assert result['warnings'] == ['OpenRouter account credits are unavailable.']
    data['current_key_error'] = 'PRIVATE_FIXTURE'
    monkeypatch.setattr(admin, '_get_instance', AsyncMock(return_value=httpx.Response(200, json=data)))
    result = json.loads((await admin.api_openrouter_usage()).body)
    assert result['current_key'] is None
    assert 'PRIVATE_FIXTURE' not in str(result)


async def test_browser_counters_limits_stale_recovery_and_narrow_layout():
    state = {'pid': 101, 'openrouter': usage()}
    async with dashboard(state) as (page, posts, _):
        # The hardware panel's separate POST probe is outside this read-only test.
        await page.evaluate("specsLoaded = true; setView('quotas')")
        daily = page.locator('[data-period="daily"]')
        weekly = page.locator('[data-period="weekly"]')
        monthly = page.locator('[data-period="monthly"]')
        await expect(daily.locator('.openrouter-spend')).to_have_text('$2.00')
        await expect(daily).to_contain_text('No daily budget configured')
        await expect(weekly).to_contain_text('$0.50 over')
        await expect(weekly.locator('[role="progressbar"]')).to_have_attribute('aria-valuenow', '100.0')
        await expect(monthly).to_contain_text('OpenRouter key cap: $75.00')
        await expect(monthly).to_contain_text('$55.00 left')
        await expect(page.locator('#openrouter-key-cap')).to_contain_text('$75.00 · $55.00 remaining · monthly')
        await expect(page.locator('#openrouter-account')).to_contain_text('$60.00 left')

        await page.set_viewport_size({'width': 390, 'height': 844})
        assert await page.locator('#openrouter-monitor').evaluate('card => card.scrollWidth <= card.clientWidth')

        # Repeated calls within the minute and background views do not poll.
        await page.evaluate('Promise.all([loadOpenRouterUsage(), loadOpenRouterUsage()])')
        assert state['openrouter_gets'] == 1
        await page.evaluate("setView('activity')")
        await page.clock.fast_forward(65000)
        assert state['openrouter_gets'] == 1
        state['openrouter_status'] = 503
        state['openrouter'] = {'error': 'Main EXO is unreachable'}
        await page.evaluate("setView('quotas')")
        await expect(page.locator('#openrouter-monitor-status')).to_contain_text('not current')
        await expect(daily.locator('.openrouter-spend')).to_have_text('$2.00')

        # A partial provider failure also keeps the last successful counters.
        state['openrouter_status'] = 200
        state['openrouter'] = {**usage(), 'current_key': None, 'warnings': ['Key status failed']}
        await page.evaluate("setView('quotas')")
        await page.locator('#btn-openrouter-refresh').click()
        await expect(page.locator('#openrouter-monitor-status')).to_contain_text('Key status failed')
        await expect(daily.locator('.openrouter-spend')).to_have_text('$2.00')
        state['openrouter'] = usage()
        state['openrouter']['current_key']['usage_daily'] = 3
        await page.locator('#btn-openrouter-refresh').click()
        await expect(daily.locator('.openrouter-spend')).to_have_text('$3.00')
        await expect(page.locator('#openrouter-monitor')).not_to_have_class('card stale')
        assert posts == []


async def test_browser_zero_daily_cap_unknown_usage_and_unconfigured_key():
    data = usage()
    data['current_key'] = {'usage_daily': 0, 'limit': 0, 'limit_remaining': 0, 'limit_reset': 'daily'}
    state = {'pid': 101, 'openrouter': data}
    async with dashboard(state) as (page, posts, _):
        await page.evaluate("specsLoaded = true; setView('quotas')")
        daily = page.locator('[data-period="daily"]')
        await expect(daily).to_contain_text('OpenRouter key cap: $0.00')
        await expect(daily.locator('[role="progressbar"]')).to_have_attribute('aria-valuenow', '100.0')
        await expect(page.locator('[data-period="weekly"] .openrouter-spend')).to_have_text('Unavailable')
        state['openrouter'] = {**data, 'api_key_configured': False, 'current_key': None}
        await page.evaluate("setView('quotas')")
        await page.locator('#btn-openrouter-refresh').click()
        await expect(page.locator('#openrouter-key-cap')).to_contain_text('Set an OpenRouter API key')
        await expect(daily.locator('.openrouter-spend')).to_have_text('Unavailable')
        assert posts == []


async def test_browser_monitor_single_flight_and_hidden_tab():
    state = {'pid': 101}
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async with dashboard(state) as (page, posts, _):
        async def delayed(route):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            await route.fulfill(json=usage())
        await page.route('**/api/openrouter/usage', delayed)
        try:
            await page.evaluate("specsLoaded = true; setView('quotas')")
            await asyncio.wait_for(started.wait(), 5)
            await page.evaluate('Promise.all([loadOpenRouterUsage({force:true}), loadOpenRouterUsage({force:true})])')
            assert calls == 1
        finally:
            release.set()
        await expect(page.locator('#openrouter-monitor-status')).to_contain_text('Updated')
        await page.evaluate("Object.defineProperty(document, 'hidden', {configurable:true, value:true})")
        await page.clock.fast_forward(65000)
        await page.evaluate('loadOpenRouterUsage()')
        assert calls == 1 and posts == []


async def test_monitor_loads_only_when_quotas_tab_is_opened():
    state = {'pid': 101, 'openrouter': usage()}
    async with dashboard(state) as (page, posts, _):
        await page.evaluate("specsLoaded = true; setView('overview'); setView('openrouter')")
        await page.evaluate('loadOpenRouterUsage()')
        assert state.get('openrouter_gets', 0) == 0
        await page.evaluate("setView('quotas')")
        await expect(page.locator('[data-period="daily"] .openrouter-spend')).to_have_text('$2.00')
        assert state['openrouter_gets'] == 1 and posts == []
