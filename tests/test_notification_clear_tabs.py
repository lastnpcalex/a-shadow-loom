from playwright.async_api import expect
from tests.test_agent_activity_browser import activity_browser, open_conversation


async def test_clear_synchronizes_tabs_and_preserves_pending_and_new(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    await open_conversation(page, conv, assistant)
    other = await page.context.new_page()
    await other.goto(page.url)
    await other.wait_for_function("typeof _renderNotifBell === 'function' && typeof State !== 'undefined'")
    seed = '''() => {
        _notifications.push({type:'branch', branchActivity:true, id:999999, convId:1, preview:'Dismiss fixture', time:new Date()},
                            {type:'permission', requestId:'pending-fixture', resolved:false, toolName:'Read', time:new Date()});
        _renderNotifBell(); _renderNotifDropdown();
    }'''
    for tab in [page, other]:
        await tab.evaluate(seed)
    await page.evaluate("document.querySelector('.notif-clear-all').click()")
    await other.wait_for_function("!_notifications.some(n => n.id === 999999)")
    assert await other.evaluate("_notifications.some(n => n.requestId === 'pending-fixture' && !n.resolved)")
    await other.evaluate("_notifications.push({type:'branch',branchActivity:true,id:999998,convId:1,preview:'New fixture',time:new Date()}); _renderNotifBell()")
    assert await other.evaluate('_notifications.some(n => n.id === 999998)')
    await other.close()
