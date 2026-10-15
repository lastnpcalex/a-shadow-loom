from playwright.async_api import expect

import database as db
from tests.test_agent_activity_browser import activity_browser, open_conversation


async def test_folders_flag_routes_directly_without_creating_home_chat(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    home_id = await page.evaluate('State.currentConvId')
    await db.delete_conversation(home_id)  # Disposable fixture database only.
    base = page.url.split('?')[0]
    await page.goto(base + '?folders')
    await page.wait_for_function("!document.body.classList.contains('app-booting')")
    await expect(page.locator('#view-home')).to_be_visible()
    assert await page.evaluate('State.currentConvId') is None
    assert len(await db.list_conversations()) == 1
    assert page.url == base + '?folders'
    await page.reload()
    await page.wait_for_function("!document.body.classList.contains('app-booting')")
    await expect(page.locator('#view-home')).to_be_visible()
    assert len(await db.list_conversations()) == 1
    await open_conversation(page, conv, assistant)
    assert 'folders' not in page.url
    await page.locator('#workspace-library').click()
    await expect(page.locator('#view-home')).to_be_visible()
    assert page.url == base + '?folders'


async def test_desktop_footer_has_shared_loom_surface_and_keeps_controls_clickable(activity_browser, tmp_path):
    page, conv, user, assistant, runs = activity_browser
    await page.set_viewport_size({'width': 1600, 'height': 1000})
    await open_conversation(page, conv, assistant)
    await page.evaluate("workspaceSetOpen('left', false); workspaceSetOpen('right', false)")
    await page.wait_for_function("document.getElementById('view-chat').classList.contains('workspace-controls-flanking')")
    surface = await page.evaluate("""() => {
        const body = document.getElementById('chat-workspace-body');
        const css = getComputedStyle(body, '::before');
        return {background: css.backgroundColor, blur: css.backdropFilter, width: css.width,
            height: parseFloat(css.height), composer: document.getElementById('input-area').offsetHeight,
            bodyWidth: body.clientWidth};
    }""")
    assert surface['background'] == 'rgba(5, 5, 15, 0.9)'
    assert surface['blur'] != 'none'
    assert abs(float(surface['width'].replace('px', '')) - surface['bodyWidth']) < 1
    assert abs(surface['height'] - surface['composer']) < 1
    await page.locator('#user-input').fill('The composer keeps its Loom surface')
    await page.locator('#workspace-toggle-left').click()
    await expect(page.locator('#workspace-left')).to_be_visible()
    await page.screenshot(path=str(tmp_path / 'loom-footer-desktop.png'))
    await page.set_viewport_size({'width': 1000, 'height': 800})
    await page.wait_for_function("!document.getElementById('view-chat').classList.contains('workspace-controls-flanking')")
    assert await page.locator('#chat-workspace-toolbar').evaluate('el => getComputedStyle(el).backgroundColor') != 'rgba(0, 0, 0, 0)'
    assert await page.locator('#input-area').evaluate('el => getComputedStyle(el).backgroundColor') == 'rgba(5, 5, 15, 0.9)'
