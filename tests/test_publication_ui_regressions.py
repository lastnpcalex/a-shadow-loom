"""User-visible regressions for exact navigation, notices and budget labels."""
import asyncio
from playwright.async_api import expect
import database as db
from tests.test_agent_activity_browser import activity_browser, open_conversation
from tests.test_exoloom_mobile_browser import mobile_browser
from tests.test_admin_restart_browser import dashboard


async def test_usage_preserves_exo_budget_when_provider_cap_is_larger(activity_browser):
    page, conv, user, assistant, _ = activity_browser
    snapshot = {'api_key_configured': True, 'management_key_configured': False,
        'limits': {'weekly_limit_usd': 10, 'monthly_limit_usd': 40},
        'current_key': {'usage_weekly': 6.39, 'usage_monthly': 10.38,
            'limit': 50, 'limit_remaining': 43.61, 'limit_reset': 'weekly'},
        'account': {}, 'warnings': []}
    await page.route('**/api/openrouter/usage', lambda route: route.fulfill(json=snapshot))
    await open_conversation(page, conv, assistant)
    await page.locator('#btn-usage').click()
    weekly = page.locator('#settings-openrouter-periods section').nth(1)
    await expect(weekly).to_contain_text('EXO budget: $10.00')
    await expect(weekly).to_contain_text('$3.61 remaining')
    await expect(weekly).to_contain_text('OpenRouter key cap: $50.00')
    await expect(weekly.locator('progress')).to_have_attribute('aria-valuetext', '63.9% used, 36.1% remaining')
    async with dashboard({'pid': 101, 'openrouter': snapshot}) as (admin, _, __):
        await admin.evaluate("specsLoaded = true; setView('quotas')")
        admin_weekly = admin.locator('[data-period="weekly"]')
        await expect(admin_weekly).to_contain_text('EXO budget: $10.00')
        await expect(admin_weekly).to_contain_text('$3.61 left')
        await expect(admin_weekly).to_contain_text('OpenRouter key cap: $50.00')
        await expect(admin_weekly.locator('[role="progressbar"]')).to_have_attribute('aria-valuenow', '63.9')


async def test_notice_polling_keeps_touch_target_and_dismissal(mobile_browser):
    page, conv, user, assistant, _ = mobile_browser
    await open_conversation(page, conv, assistant)
    await page.evaluate("handleWSMessage({type:'permission_notice',status:'pending',request_id:'stable-notice',conv_id:999,message_id:888})")
    await page.locator('#notif-bell').tap()
    dismiss = page.get_by_role('button', name='Dismiss notification', exact=True)
    await expect(dismiss).to_be_visible()
    await page.evaluate("window.originalDismiss = [...document.querySelectorAll('.notif-perm-btn')].find(e => e.textContent === 'Dismiss notification')")
    await page.evaluate('refreshParallelBranchActivity()')
    assert await page.evaluate("originalDismiss.isConnected"), 'Unchanged polling must preserve the button during a gesture'
    await dismiss.tap()
    await expect(dismiss).to_have_count(0)
    assert not await page.evaluate("_resolvedPermissionNoticeIds.has('stable-notice')")


async def test_exact_cross_tree_address_does_not_visit_tree_overview(mobile_browser):
    page, conv, user, assistant, _ = mobile_browser
    await db.add_message(conv['id'], 'assistant', 'A sibling', parent_id=user['id'])
    other = await db.create_conversation('Starting tree')
    await db.add_message(other['id'], 'user', 'Another root')
    await page.evaluate('id => loadConversation(id)', other['id'])
    await page.evaluate("() => { window.addressViews=[]; window.originalSwitchView=switchView; switchView=function(view){addressViews.push(view); return originalSwitchView(view);}; }")
    await page.evaluate("address => navigateToBranchPath(address)", f"tree:{conv['id']}/root/msg:{user['id']}")
    assert await page.evaluate('State.currentView') == 'chat'
    assert 'tree' not in await page.evaluate('addressViews')
    assert await page.evaluate('State.loomCursor.leaf_message_id') == user['id']
