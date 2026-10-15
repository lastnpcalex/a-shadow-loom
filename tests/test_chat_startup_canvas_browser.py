from tests.test_agent_activity_browser import activity_browser, open_conversation


async def test_base_route_opens_normal_exohome_blank_branch(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    assert await page.evaluate('State.currentConv.title') == 'ExoHome'
    assert await page.evaluate('State.currentConv.project_dir.replaceAll("\\\\", "/").endsWith("/ExoHome")')
    assert await page.evaluate('State.loomCursor.leaf_message_id') is None
    await open_conversation(page, conv, assistant)
    await page.goto(page.url.split('?')[0])
    await page.wait_for_function("!document.body.classList.contains('app-booting')")
    assert await page.evaluate('State.currentConv.title') == 'ExoHome'
    assert await page.evaluate('State.loomCursor.leaf_message_id') is None
    assert not await page.locator('#view-home').is_visible()
    assert not await page.locator('#workspace-new-chat-settings').is_visible()
    assert await page.locator('#cc-model-inline').is_visible()
    await page.locator('#user-input').fill('Keep my unsent home draft')
    await page.goto(page.url.split('?')[0])
    await page.wait_for_function("!document.body.classList.contains('app-booting')")
    assert await page.locator('#user-input').input_value() == 'Keep my unsent home draft'


async def test_canvas_does_not_reposition_toolbar_or_duplicate_copy(activity_browser, tmp_path):
    page, conv, user, assistant, runs = activity_browser
    await page.set_viewport_size({'width': 1600, 'height': 1000})
    await open_conversation(page, conv, assistant)
    await page.locator('#btn-canvas-toggle').click()
    await page.wait_for_function('State.currentConv.canvas_enabled')
    before = await page.locator('#workspace-controls-left').bounding_box()
    await page.locator('#workspace-toggle-canvas').click()
    await page.wait_for_selector('#workspace-canvas-pane')
    assert await page.locator('#workspace-controls-left').bounding_box() == before
    assert await page.locator('#workspace-copy-canvas').count() == 0
    assert await page.locator('#canvas-slug-pill').is_visible()
    assert await page.locator('#btn-canvas-toggle').is_visible()
    geometry = await page.evaluate("""() => {
        const m = document.getElementById('messages').getBoundingClientRect();
        const i = document.getElementById('input-row').getBoundingClientRect();
        return Math.abs(m.x+m.width/2-i.x-i.width/2);
    }""")
    assert geometry < 2
    await page.screenshot(path=str(tmp_path / 'stable-canvas-controls.png'))
    await page.locator('#workspace-canvas-close').click()
    await page.set_viewport_size({'width': 1000, 'height': 800})
    await page.locator('#workspace-toggle-canvas').click()
    await page.wait_for_selector('#workspace-canvas-pane')


async def test_canvas_copy_invokes_clipboard_during_click(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    await open_conversation(page, conv, assistant)
    await page.route(f'**/api/canvas/{conv["id"]}/share-link', lambda route:
        route.fulfill(json={'url': 'https://desktop.tail123.ts.net:3000/vivid-crow-53ct'}))
    await page.evaluate("""() => {
        State.currentConv.canvas_enabled = true;
        State.currentConv.canvas_slug = 'vivid-crow-53ct';
        Object.defineProperty(navigator, 'clipboard', {configurable:true, value:{
            write: async items => {
                window.copyHadClickPermission = navigator.userActivation.isActive;
                window.copiedCanvasURL = await (await items[0].getType('text/plain')).text();
            }
        }});
        updateCanvasSlugPill();
    }""")
    await page.locator('#canvas-slug-pill').click()
    await page.wait_for_function("window.copiedCanvasURL === 'https://desktop.tail123.ts.net:3000/vivid-crow-53ct'")
    assert await page.evaluate('window.copyHadClickPermission')
