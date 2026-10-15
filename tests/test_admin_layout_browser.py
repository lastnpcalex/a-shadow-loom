"""Navigation and quota accounting against isolated browser routes."""
from playwright.async_api import expect
from tests.test_admin_restart_browser import dashboard


async def test_layout_preserves_controls_and_distinguishes_period_caps():
    snapshot = {'api_key_configured': True, 'management_key_configured': True,
                'current_key': {'usage_daily': 2, 'usage_weekly': 12, 'usage_monthly': 30,
                                'limit': 50, 'limit_remaining': 38, 'limit_reset': 'weekly'},
                'limits': {'monthly_limit_usd': 100},
                'account': {'total_credits': 200, 'total_usage': 80}}
    async with dashboard({'pid': 101, 'openrouter': snapshot}) as (page, posts, _):
        await page.locator('[data-view="quotas"]').click()
        await expect(page.locator('#view-quotas #openrouter-monitor')).to_be_visible()
        await expect(page.locator('[data-period="weekly"]')).to_contain_text('$38.00 left')
        await expect(page.locator('[data-period="monthly"]')).to_contain_text('$70.00 left')
        await expect(page.locator('[data-period="daily"]')).to_contain_text('No daily budget')
        await expect(page.locator('#openrouter-account')).to_contain_text('$120.00 left')
        await expect(page.locator('#view-settings #db-folder')).to_have_count(1)
        await expect(page.locator('#view-diagnostics #acceptance-monitor')).to_have_count(1)
        await page.locator('[data-view="overview"]').click()
        await expect(page.locator('#service-health')).to_contain_text('Unknown')
        await expect(page.locator('#view-overview [data-server="dream"]')).to_have_count(0)
        assert not posts
