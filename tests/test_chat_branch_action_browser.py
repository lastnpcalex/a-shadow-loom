import database as db
from playwright.async_api import expect
from tests.test_agent_activity_browser import activity_browser, open_conversation


async def test_branch_selects_exact_boundary_without_copying_conversation(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    await open_conversation(page, conv, assistant)
    count = len(await db.list_conversations())
    await page.evaluate(f'forkFromMessage({user["id"]})')
    assert await page.evaluate('State.currentConvId') == conv['id']
    assert await page.evaluate('State.loomCursor.leaf_message_id') == user['id']
    assert len(await db.list_conversations()) == count
    await expect(page.locator('#user-input')).to_be_focused()
