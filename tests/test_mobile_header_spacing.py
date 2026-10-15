from tests.test_agent_activity_browser import activity_browser, open_conversation
from tests.test_exoloom_mobile_browser import mobile_browser


async def test_mobile_breadcrumb_row_has_its_own_width(mobile_browser, tmp_path):
    page, conv, user, assistant, _ = mobile_browser
    await open_conversation(page, conv, assistant)
    await page.evaluate('State._skipLoadOnChat=true; switchView("chat")')
    for width in [360, 390, 412]:
        await page.set_viewport_size({'width': width, 'height': 915})
        icons = await page.locator('#header-right').bounding_box()
        paste = await page.locator('#branch-nav-bar').bounding_box()
        trail = await page.locator('#breadcrumb-trail').bounding_box()
        assert trail['width'] >= width - 40
        assert trail['y'] >= max(icons['y'] + icons['height'], paste['y'] + paste['height'])
        assert icons['x'] + icons['width'] <= paste['x']
        assert paste['x'] + paste['width'] <= width
        assert await page.locator('.breadcrumb-btn').first.inner_text() == 'root'
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    await page.screenshot(path=str(tmp_path / 'pixel-header-spacing.png'))
    print(f'Header preview: {tmp_path / "pixel-header-spacing.png"}')
