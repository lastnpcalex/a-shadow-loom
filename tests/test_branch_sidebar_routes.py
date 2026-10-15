"""Space entry starts blank; explicit branch links remain exact."""
from tests.test_agent_activity_browser import activity_browser


async def test_bare_space_route_starts_blank_but_branch_reload_preserves_path(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    origin = page.url.split('?')[0].rstrip('/')
    await page.evaluate('sessionStorage.clear()')
    await page.goto(f"{origin}/?conversation={conv['id']}", wait_until='domcontentloaded')
    await page.wait_for_function('State.loomCursor != null && State.currentView === "chat"')
    assert await page.evaluate('State.messages.length') == 0
    assert await page.evaluate('State.loomCursor.leaf_message_id') is None
    await page.locator('#workspace-branches [data-leaf-id]').click()
    await page.wait_for_function('id => State.loomCursor?.leaf_message_id === id', arg=assistant['id'])
    await page.reload(wait_until='domcontentloaded')
    await page.wait_for_function('id => State.loomCursor?.leaf_message_id === id && State.messages.length === 2', arg=assistant['id'])
    assert await page.evaluate('State.messages.length') == 2
    await page.goto(f"{origin}/?conversation={conv['id']}", wait_until='domcontentloaded')
    await page.wait_for_function('id => State.loomCursor?.leaf_message_id === id && State.messages.length === 2', arg=assistant['id'])
    assert await page.evaluate('State.messages.length') == 2


async def test_rename_cancel_keeps_blank_space_entry(activity_browser):
    page, conv, *_ = activity_browser
    await page.locator('#workspace-library').click()
    await page.wait_for_selector('#view-home.active')
    while await page.locator('#conv-list .conv-folder-group.is-collapsed .conv-folder-header').count():
        await page.locator('#conv-list .conv-folder-group.is-collapsed .conv-folder-header').first.click()
    card = page.locator('.conv-item').filter(has=page.locator('.conv-title-text', has_text=conv['title']))
    await card.locator('.conv-edit-btn').click()
    await page.locator('.conv-title-input').press('Escape')
    await card.locator('.conv-title-text').click()
    await page.wait_for_function('State.loomCursor != null && State.currentView === "chat"')
    assert await page.evaluate('State.loomCursor.leaf_message_id') is None
    assert await page.evaluate('State.messages.length') == 0


async def test_pending_space_picker_does_not_override_folders(activity_browser):
    import asyncio
    page, conv, *_ = activity_browser
    await page.evaluate('id => loadConversation(id)', conv['id'])
    await page.evaluate("switchView('chat')")
    ready, release = asyncio.Event(), asyncio.Event()

    async def delay_tree(route):
        ready.set()
        await release.wait()
        await route.continue_()

    await page.route('**/api/conversations/*/tree', delay_tree)
    picker = page.locator('#workspace-projects')
    if await picker.get_attribute('aria-expanded') != 'true':
        await picker.click()
    group = page.locator('#workspace-conversations details').filter(
        has=page.locator('.workspace-item', has_text=conv['title']))
    if not await group.evaluate('element => element.open'):
        await group.locator('summary').click()
    await page.locator('#workspace-conversations .workspace-item').filter(has_text=conv['title']).click()
    try:
        await asyncio.wait_for(ready.wait(), 10)
        await page.evaluate("switchView('home')")
    finally:
        release.set()
    await page.wait_for_function('document.getElementById("loading-overlay").classList.contains("hidden")')
    assert await page.evaluate('State.currentView') == 'home'
