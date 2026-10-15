"""Chat/composer geometry and independent Canvas pane on desktop and phone."""
import time
import pytest
import database as db
from tests.test_agent_activity_browser import activity_browser, open_conversation


async def assert_aligned(page):
    geometry = await page.evaluate("""() => {
        const messages = document.getElementById('messages').getBoundingClientRect();
        const input = document.getElementById('input-row').getBoundingClientRect();
        return {messageCenter:messages.x+messages.width/2, inputCenter:input.x+input.width/2,
                overflow:document.documentElement.scrollWidth > innerWidth};
    }""")
    assert abs(geometry['messageCenter'] - geometry['inputCenter']) < 2
    assert not geometry['overflow']


async def test_desktop_composer_tracks_panes_and_canvas_can_resize(activity_browser, tmp_path):
    page, conv, user, assistant, runs = activity_browser
    await page.set_viewport_size({'width': 1920, 'height': 1080})
    await open_conversation(page, conv, assistant)
    await page.evaluate("switchView('chat')")
    await page.wait_for_function("document.getElementById('input-area').parentNode.id === 'workspace-chat-column'")
    assert (await page.locator('#messages').bounding_box())['width'] > 1000
    assert (await page.locator('#workspace-left').bounding_box())['width'] >= 300
    assert (await page.locator('#workspace-right').bounding_box())['width'] >= 360
    assert await page.locator('#workspace-goals').evaluate('(node) => node.previousElementSibling.id') == 'workspace-tree'
    await assert_aligned(page)
    await page.locator('#workspace-toggle-right').click()
    await assert_aligned(page)
    await page.locator('#workspace-toggle-canvas').click()
    await page.locator('#btn-canvas-toggle').click()
    await page.wait_for_selector('#workspace-canvas-pane-iframe')
    assert await page.evaluate("""() => {
        const frame = document.getElementById('workspace-canvas-pane-iframe');
        return _canvasMessageFrame({origin:location.origin,source:frame.contentWindow},State.currentConvId) === frame;
    }""")
    before = (await page.locator('#workspace-canvas-pane').bounding_box())['width']
    divider = await page.locator('#workspace-canvas-resizer').bounding_box()
    await page.mouse.move(divider['x'] + 4, divider['y'] + 100)
    await page.mouse.down()
    await page.mouse.move(divider['x'] - 160, divider['y'] + 100)
    await page.mouse.up()
    assert (await page.locator('#workspace-canvas-pane').bounding_box())['width'] > before + 100
    await assert_aligned(page)
    await page.locator('#workspace-canvas-resizer').press('ArrowRight')
    await assert_aligned(page)
    await page.screenshot(path=str(tmp_path / 'desktop-canvas-layout.png'))
    await page.locator('#workspace-canvas-close').click()
    assert await page.locator('#workspace-canvas-pane-iframe').get_attribute('src') is None
    await page.locator('#workspace-tree').click()
    await page.wait_for_function("document.getElementById('input-area').parentNode.id !== 'workspace-chat-column'")
    await page.evaluate("switchView('chat')")
    await assert_aligned(page)


async def test_branch_sections_filter_and_saved_pins(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    old = await db.add_message(conv['id'], 'user', 'An older independent topic')
    recent = await db.add_message(conv['id'], 'user', 'A recent independent topic')
    connection = await db.get_db()
    await connection.execute('UPDATE messages SET created_at = ? WHERE id = ?', (time.time()-10*86400, old['id']))
    await connection.commit()
    await open_conversation(page, conv, assistant)
    await page.evaluate("switchView('chat')")
    await page.evaluate('id => navigateToBranchPath(`msg:${id}`)', assistant['id'])
    await page.locator('#workspace-pin-current').click()
    await page.get_by_role('textbox', name='Pin label', exact=True).fill('My saved branch')
    await page.locator('.workspace-pin-dialog [type="submit"]').click()
    await page.wait_for_selector('#workspace-branches [data-pin-id]')
    # Saving opens Pins-only; reopen Branches before checking age groups.
    await page.locator('#workspace-toggle-left').click()
    await page.locator('#workspace-toggle-left').click()
    assert await page.locator('#workspace-branches h3').all_text_contents() == ['Pinned', 'Recents', 'Older']
    assert await page.locator(f'#workspace-branches [data-leaf-id="{assistant["id"]}"]').count() == 0
    await page.locator('#workspace-branch-filter').fill('older independent')
    assert await page.locator('#workspace-branches [data-leaf-id]').count() == 1
    await page.locator('#workspace-branch-filter').fill('My saved')
    assert await page.locator('#workspace-branches [data-pin-id]').count() == 1


async def test_phone_canvas_overlays_without_squeezing_chat(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    await page.set_viewport_size({'width': 390, 'height': 844})
    await page.add_init_script("""(() => { const native = matchMedia;
        window.matchMedia = query => { const result = native(query);
            if (query === '(pointer: coarse)' || query === '(any-pointer: fine)')
                Object.defineProperty(result,'matches',{value:query === '(pointer: coarse)'});
            return result; }; })();""")
    await page.reload(wait_until='domcontentloaded')
    await open_conversation(page, conv, assistant)
    await page.evaluate("switchView('chat')")
    width = (await page.locator('#messages').bounding_box())['width']
    await page.locator('#workspace-toggle-canvas').click()
    assert (await page.locator('#messages').bounding_box())['width'] == width
    assert not await page.locator('#workspace-canvas-resizer').is_visible()
    assert (await page.locator('#workspace-canvas-pane').bounding_box())['width'] <= 390
    await page.keyboard.press('Escape')
    assert not await page.locator('#workspace-canvas-pane').is_visible()
    await assert_aligned(page)
