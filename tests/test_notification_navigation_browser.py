"""Notification navigation uses the owning conversation and exact message."""
import database as db
from playwright.async_api import expect
from tests.test_agent_activity_browser import activity_browser, open_conversation


async def test_approval_and_completion_notifications_navigate(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    target = await db.create_conversation('Approval destination', mode='codex')
    prompt = await db.add_message(target['id'], 'user', 'Destination prompt')
    reply = await db.add_message(target['id'], 'assistant', 'Destination reply', parent_id=prompt['id'])
    await open_conversation(page, conv, assistant)
    await page.evaluate("""data => {
        _notifications.push({type: 'permission-notice', requestId: 'navigation-review',
            convId: data.convId, messageId: data.messageId, time: new Date()});
        _renderNotifBell();
    }""", dict(convId=target['id'], messageId=reply['id']))
    await page.locator('#notif-bell').click()
    await page.get_by_role('button', name='Open conversation', exact=True).click()
    await page.wait_for_function('id => State.currentConvId === id && State.currentView === "chat"', arg=target['id'])
    await page.wait_for_function('id => State.loomCursor?.leaf_message_id === id', arg=reply['id'])
    await expect(page.locator('#notif-dropdown')).to_be_hidden()
    assert await page.evaluate('_notifications.some(n => n.requestId === "navigation-review")')
    await page.evaluate("""data => {
        _notifications.push({type: 'branch', branchActivity: true, id: data.messageId,
            convId: data.convId, convTitle: 'Completed destination', preview: 'Navigation completion', time: new Date()});
        _renderNotifBell();
    }""", dict(convId=conv['id'], messageId=assistant['id']))
    await page.locator('#notif-bell').click()
    await page.locator('.notif-new-branch').filter(has_text='Navigation completion').click()
    await page.wait_for_function('id => State.currentConvId === id && State.currentView === "chat"', arg=conv['id'])
    await page.wait_for_function('id => State.loomCursor?.leaf_message_id === id', arg=assistant['id'])
