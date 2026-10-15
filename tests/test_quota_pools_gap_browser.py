import time

from playwright.async_api import expect
from tests.test_agent_activity_browser import activity_browser, open_conversation


async def test_quota_banner_groups_pools_and_branch_list_meets_footer(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    now = time.time()
    rows = [
        {'label': label, 'used_percent': used, 'resets_at': now + 3600}
        for label, used in [('Gemini Flash', 85), ('Gemini Pro', 85),
                            ('Claude Sonnet', 91), ('Claude Opus', 91), ('GPT OSS', 91)]
    ]
    await page.route('**/api/provider-usage', lambda route: route.fulfill(json={
        'providers': [{'provider': 'antigravity', 'label': 'Antigravity',
                       'status': 'ok', 'observed_at': now, 'windows': rows}]}))
    await page.set_viewport_size({'width': 1600, 'height': 1000})
    await open_conversation(page, conv, assistant)
    await page.evaluate('loadProviderUsage(true)')
    banner = page.locator('#provider-usage-warning')
    await expect(banner).to_contain_text('Google models: 85% used')
    await expect(banner).to_contain_text('Non-Google models: 91% used')
    assert 'Gemini Flash' not in await banner.inner_text()
    assert 'Claude Sonnet' not in await banner.inner_text()
    await expect(banner).to_have_class('critical')
    await page.evaluate("workspaceSetOpen('left', true); syncWorkspaceLayout()")
    geometry = await page.evaluate('''() => {
        const list = document.getElementById('workspace-left-scroll').getBoundingClientRect();
        const panel = document.getElementById('workspace-left').getBoundingClientRect();
        const footer = document.getElementById('chat-workspace-toolbar').getBoundingClientRect();
        return {list: list.bottom, panel: panel.bottom, footer: footer.top};
    }''')
    assert abs(geometry['panel'] - geometry['list']) <= 1.5
    assert abs(geometry['panel'] - geometry['footer']) <= 1.5
    await page.locator('#btn-usage').click()
    await expect(page.locator('#provider-usage-cards')).to_contain_text('Gemini Flash')
    await expect(page.locator('#provider-usage-cards')).to_contain_text('Claude Opus')
    await expect(page.locator('#provider-usage-cards .quota-provider')).to_have_count(2)
    from provider_quota_sources import antigravity_quota_windows
    from tests.test_provider_quota_sources import agy_summary
    rows[:] = antigravity_quota_windows(agy_summary())
    for row in rows:
        row['resets_at'] = now + 3600
    rows[0]['used_percent'] = 85
    await page.evaluate('loadProviderUsage(true)')
    cards = page.locator('#provider-usage-cards')
    await expect(cards.locator('progress')).to_have_count(4)
    for pool in [cards.locator('.quota-provider').nth(0), cards.locator('.quota-provider').nth(1)]:
        await expect(pool).to_contain_text('5-hour')
        await expect(pool).to_contain_text('Weekly')
        reset = pool.locator('.quota-window').nth(0).locator('small')
        assert '/' not in await reset.inner_text()
        await expect(reset).to_have_attribute('aria-label', __import__('re').compile('Reset:'))
    await expect(banner).to_contain_text('Google models 5-hour: 85% used')
    await expect(banner).to_contain_text('Google models Weekly: 90% used')
    await expect(banner).not_to_contain_text('Non-Google models')
    rows[0]['resets_at'] = now - 1
    await page.evaluate('loadProviderUsage(true)')
    await expect(banner).not_to_contain_text('5-hour: 85% used')
    await expect(banner).to_contain_text('Weekly: 90% used')
