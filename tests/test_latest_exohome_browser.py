from playwright.async_api import expect
from tests.test_agent_activity_browser import activity_browser, open_conversation


async def test_latest_is_visible_above_composer_and_scrolls_to_bottom(activity_browser, tmp_path):
    page, conv, user, assistant, runs = activity_browser
    await page.set_viewport_size({'width': 1600, 'height': 1000})
    await open_conversation(page, conv, assistant)
    await page.locator('#messages-container').evaluate('el => { el.scrollTop = 0; el.dispatchEvent(new Event("scroll")); }')
    latest = page.locator('#jump-to-latest')
    await expect(latest).to_be_visible()
    button = await latest.bounding_box()
    composer = await page.locator('#input-area').bounding_box()
    assert button['y'] + button['height'] <= composer['y']
    await latest.click()
    await page.wait_for_function('(() => { const e = document.getElementById("messages-container"); return e.scrollHeight-e.scrollTop-e.clientHeight < 20; })()')
    await expect(latest).to_be_hidden()
    await page.locator('#input-area').evaluate('el => el.style.minHeight = "300px"')
    await page.locator('#messages-container').evaluate('el => { el.scrollTop = 0; el.dispatchEvent(new Event("scroll")); }')
    await expect(latest).to_be_visible()
    await page.wait_for_function('parseFloat(getComputedStyle(document.getElementById("jump-to-latest")).bottom) >= 316')
    button = await latest.bounding_box()
    composer = await page.locator('#input-area').bounding_box()
    assert button['y'] + button['height'] <= composer['y']
    await page.screenshot(path=str(tmp_path / 'latest-tall-composer.png'))


async def test_exohome_opens_branches_despite_other_space_preference(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    await page.set_viewport_size({'width': 1600, 'height': 1000})
    await page.evaluate('localStorage.setItem("exo-sidebar-left", "false")')
    await page.goto(page.url.split('?')[0])
    await page.wait_for_function("!document.body.classList.contains('app-booting')")
    await expect(page.locator('#workspace-left')).to_be_visible()
    await expect(page.locator('#workspace-toggle-left')).to_have_attribute('aria-expanded', 'true')
    assert await page.evaluate('State.currentConv.permission_profile') == 'trusted'
    await page.locator('#workspace-toggle-left').click()
    await expect(page.locator('#workspace-left')).to_be_hidden()
    await page.evaluate('renderChatWorkspace()')
    await expect(page.locator('#workspace-left')).to_be_hidden()
    await open_conversation(page, conv, assistant)
    await page.evaluate('openExoHomeChat()')
    await expect(page.locator('#workspace-left')).to_be_visible()
