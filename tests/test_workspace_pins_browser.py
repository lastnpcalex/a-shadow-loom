from playwright.async_api import expect
import database as db
from tests.test_agent_activity_browser import activity_browser, open_conversation


async def test_conversation_hover_pin_rename_and_unpin(activity_browser, tmp_path):
    page, conv, user, assistant, runs = activity_browser
    await open_conversation(page, conv, assistant)
    await page.evaluate("setWorkspaceLeftMode(true); document.querySelectorAll('.workspace-conversation-group').forEach(g => g.open = true)")
    row = page.locator(f'#workspace-conversations [data-conversation-id="{conv["id"]}"]').last
    await page.mouse.move(1000, 100)
    await page.evaluate('document.activeElement.blur()')
    await expect(row.locator('.workspace-row-pin').first).to_have_css('opacity', '0')
    height = await row.evaluate('el => el.getBoundingClientRect().height')
    assert await row.evaluate('el => Math.abs(el.children[0].getBoundingClientRect().width - el.getBoundingClientRect().width) < 1')
    await row.hover()
    await expect(row.locator('.workspace-row-pin').first).to_have_css('opacity', '1')
    assert await row.evaluate('el => el.getBoundingClientRect().height') == height
    await expect(row.locator('.workspace-item')).to_have_css('clip-path', 'inset(0px 41px 0px 0px round 6px)')
    await row.get_by_role('button', name='Pin conversation', exact=True).click()
    await expect(page.locator('#workspace-conversations')).to_contain_text('Pins (1)')
    await page.mouse.move(1000, 100)
    await page.evaluate('document.activeElement.blur()')
    await expect(row.locator('.workspace-row-pin').first).to_have_css('opacity', '0')
    height = await row.evaluate('el => el.getBoundingClientRect().height')
    assert await row.evaluate('el => Math.abs(el.children[0].getBoundingClientRect().width - el.getBoundingClientRect().width) < 1')
    await row.hover()
    await expect(row.locator('.workspace-row-pin').first).to_have_css('opacity', '1')
    assert await row.evaluate('el => el.getBoundingClientRect().height') == height
    await expect(row.locator('.workspace-item')).to_have_css('clip-path', 'inset(0px 82px 0px 0px round 6px)')
    await page.locator('#workspace-left').screenshot(path=str(tmp_path / 'conversation-hover.png'))
    print(f'Hover screenshot: {tmp_path / "conversation-hover.png"}')
    await row.get_by_role('button', name='Rename conversation', exact=True).click()
    await page.get_by_role('textbox', name='Conversation name').fill('Renamed conversation pin')
    await page.locator('.workspace-pin-dialog [type="submit"]').click()
    await expect(row).to_contain_text('Renamed conversation pin')
    await row.hover()
    await row.get_by_role('button', name='Remove conversation pin', exact=True).click()
    await expect(row.get_by_role('button', name='Pin conversation', exact=True, include_hidden=True)).to_be_attached()
    stored = await db.get_conversation(conv['id'])
    assert stored['title'] == 'Renamed conversation pin'
    assert not stored['starred']


async def test_turn_footer_expands_only_on_click(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    await open_conversation(page, conv, assistant)
    await page.evaluate('''() => {
        const msg = {...State.messages.find(m => m.role === 'assistant'), generation_ms: 1000};
        const el = createMessageElement(msg);
        el.id = 'footer-click-fixture';
        document.getElementById('messages').append(el);
    }''')
    footer = page.locator('#footer-click-fixture .message-telemetry')
    summary = footer.locator('summary')
    panel = footer.locator('.message-telemetry-panel')
    await summary.hover()
    await expect(panel).to_be_hidden()
    await summary.click()
    await expect(panel).to_be_visible()
    await page.mouse.move(1000, 100)
    await expect(panel).to_be_visible()
    await summary.click()
    await expect(panel).to_be_hidden()


async def test_recent_pin_does_not_navigate_and_message_bookmarks_stay_separate(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    await open_conversation(page, conv, assistant)
    await page.evaluate("workspaceSetOpen('left', true); setWorkspaceLeftMode(false)")
    before = await page.evaluate('JSON.stringify(State.loomCursor)')
    await expect(page.locator('#workspace-branches .workspace-row-pin').first).to_have_text('⬡')
    await page.locator('#workspace-branches .workspace-branch-row').first.hover()
    assert await page.locator('#workspace-branches .workspace-branch-row').first.evaluate('row => { const a = row.children[0].getBoundingClientRect(); const b = row.children[1].getBoundingClientRect(); return Math.abs(a.top-b.top) < 1 && Math.abs(a.height-b.height) < 1; }')
    await page.locator('#workspace-branches .workspace-row-pin').first.click()
    await expect(page.locator('#workspace-branches [data-pin-id]')).to_have_count(1)
    assert await page.evaluate('JSON.stringify(State.loomCursor)') == before
    assert len(await db.get_branch_pins(conv['id'])) == 1
    await page.set_viewport_size({'width': 390, 'height': 844})
    await page.wait_for_timeout(300)
    await page.evaluate("workspaceSetOpen('left', true); setWorkspaceLeftMode(false)")
    await page.locator('#workspace-branches .workspace-pinned-row').hover()
    await page.locator('.workspace-pinned-row [aria-label="Rename pin"]').click()
    await page.locator('.workspace-pin-dialog input').fill('Renamed pin')
    await page.locator('.workspace-pin-dialog [type="submit"]').click()
    await expect(page.locator('#workspace-branches')).to_contain_text('Renamed pin')
    assert await page.locator('#workspace-left').evaluate('el => el.scrollWidth <= el.clientWidth')
    await page.locator('#workspace-branches .workspace-pinned-row').hover()
    await page.locator('.workspace-pinned-row [aria-label="Remove pin"]').click()
    await page.locator('.loom-confirm-dialog button').get_by_text('Cancel', exact=True).click()
    assert len(await db.get_branch_pins(conv['id'])) == 1
    await page.locator('#btn-bookmarks-global').click()
    await expect(page.locator('#workspace-header-pin')).to_have_count(0)
    await expect(page.locator('#bookmarks-panel-global')).to_be_visible()
    await expect(page.locator('#bookmarks-list-global')).to_contain_text('Renamed pin')
    assert await page.evaluate('(() => { const b = document.getElementById("btn-bookmarks-global").getBoundingClientRect(); const p = document.getElementById("bookmarks-panel-global").getBoundingClientRect(); return Math.abs(p.top - b.bottom - 4) < 2 && p.left >= 0 && p.right <= innerWidth; })()')
    await page.keyboard.press('Escape')
    await expect(page.locator('#bookmarks-panel-global')).to_be_hidden()
    await page.locator('#btn-bookmarks-global').click()
    await expect(page.locator('#branch-pins-panel')).to_be_hidden()
    await expect(page.locator('#workspace-left')).to_be_visible()
    await expect(page.locator('#workspace-branches')).to_contain_text('Renamed pin')
    await page.locator('#workspace-branch-filter').click()
    await expect(page.locator('#bookmarks-panel-global')).to_be_hidden()
    await page.evaluate('''() => {
        State.bookmarks.push({id: 999, conversation_id: State.currentConvId,
            message_id: State.treeData[0].id, description: 'Existing saved note'});
        scheduleWorkspaceRender();
    }''')
    # Message bookmarks are presented in Leaves; branch pins retain their own list.
    await expect(page.locator('#workspace-branches')).not_to_contain_text('Existing saved note')
    assert await page.evaluate("State.bookmarks.some(pin => pin.description === 'Existing saved note')")
    await page.locator('#workspace-branches .workspace-pinned-row').filter(has_text='Renamed pin').hover()
    await page.locator('.workspace-pinned-row').filter(has_text='Renamed pin').get_by_role('button', name='Remove pin', exact=True).click()
    await page.locator('.loom-confirm-dialog button').get_by_text('Remove', exact=True).click()
    await expect(page.locator('#workspace-branches')).not_to_contain_text('Renamed pin')
    assert len(await db.get_branch_pins(conv['id'])) == 0
    await page.locator('#workspace-pin-current').click()
    await expect(page.locator('#workspace-branches .workspace-pin-editor')).to_be_visible()
    await page.locator('.workspace-pin-editor input').fill('Inline pin')
    await page.evaluate('renderChatWorkspace()')
    await expect(page.locator('.workspace-pin-editor input')).to_have_value('Inline pin')
    await page.locator('.workspace-pin-editor [type="submit"]').click()
    await expect(page.locator('#workspace-branches')).to_contain_text('Inline pin')
    await expect(page.locator('#branch-pins-panel')).to_be_hidden()
