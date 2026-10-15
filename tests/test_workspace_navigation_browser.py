from tests.test_agent_activity_browser import activity_browser, open_conversation
from tests.test_exoloom_mobile_browser import mobile_browser
import database as db


async def test_mobile_navigation_headers_and_leaves(mobile_browser, tmp_path):
    page, conv, user, assistant, _ = mobile_browser
    await open_conversation(page, conv, assistant)
    assert await page.locator('#workspace-controls-left button').all_text_contents() == ['Forest', 'Trees', 'Branches', 'Leaves']
    assert await page.locator('#workspace-controls-right button').all_text_contents() == ['Canvas', 'Workspace', 'Tree View', 'Goals']
    await page.evaluate("State.currentConv.mode = 'codex'; syncWorkspaceLayout()")
    tools = await page.locator('#workspace-tree').bounding_box()
    goals = await page.locator('#workspace-goals').bounding_box()
    assert goals['y'] == tools['y']
    await page.locator('#workspace-projects').tap()
    assert await page.locator('#workspace-project-panel').is_visible()
    assert await page.locator('#workspace-new-space').is_visible()
    assert not await page.locator('#workspace-new-chat').is_visible()
    await page.locator('#workspace-pin-tree').tap()
    await page.wait_for_function('State.currentConv.starred === 1')
    assert (await db.get_conversation(conv['id']))['starred'] == 1
    await page.locator('#workspace-toggle-left').tap()
    assert await page.locator('#workspace-new-chat').is_visible()
    assert await page.locator('#workspace-pin-current').is_visible()
    assert not await page.locator('#workspace-new-space').is_visible()
    await db.add_bookmark(conv['id'], assistant['id'])
    await page.locator('#workspace-toggle-pins').tap()
    await page.locator('#workspace-message-pins button[aria-label="Preview saved message"]').wait_for(state='visible')
    await page.screenshot(path=str(tmp_path / 'pixel-leaves-list.png'))
    await page.locator('#workspace-message-pins button[aria-label="Preview saved message"]').tap()
    await page.locator('#workspace-message-pins .message').wait_for(state='visible')
    assert not await page.locator('#workspace-left').is_visible()
    bounds = await page.locator('#workspace-message-pins').bounding_box()
    assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= 412
    assert bounds['width'] == 320
    assert await page.locator('#workspace-message-pins input, #workspace-message-pins > .workspace-actions').count() == 0
    assert await page.locator(f'#messages .message[data-msg-id="{user["id"]}"] .chat-bookmark-btn').get_attribute('title') == 'Pin message to Leaves'
    header_buttons = page.locator('#header-right > button, #header-right > a, #header-right > div > button')
    rectangles = await header_buttons.evaluate_all('(buttons) => buttons.filter(button => getComputedStyle(button).visibility !== "hidden").map(button => {const r=button.getBoundingClientRect();return {width:r.width,height:r.height,y:r.y};})')
    assert len(rectangles) >= 4
    assert len({(r['width'], r['height'], r['y']) for r in rectangles}) == 1
    assert await page.locator('#workspace-message-pins button[aria-label="Branch"]').is_visible()
    screenshot = tmp_path / 'pixel-leaves.png'
    await page.screenshot(path=str(screenshot))
    print(f'Leaves preview: {screenshot}')
    await page.locator('#workspace-message-pins .workspace-item').tap()
    await page.wait_for_function('(id) => State.loomCursor.leaf_message_id === id', arg=assistant['id'])
    assert not await page.locator('#workspace-message-pins').is_visible()
    await page.locator('#workspace-toggle-left').tap()
    assert not await page.locator('#workspace-message-pins').is_visible()


async def test_desktop_panels_follow_control_sides(activity_browser):
    page, conv, user, assistant, _ = activity_browser
    await page.set_viewport_size({'width': 1600, 'height': 1000})
    await open_conversation(page, conv, assistant)
    await page.evaluate("workspaceSetOpen('left', true, false); workspaceSetOpen('right', true, false)")
    tools = await page.locator('#workspace-right').bounding_box()
    chat = await page.locator('#workspace-chat-column').bounding_box()
    navigation = await page.locator('#workspace-left').bounding_box()
    assert navigation['x'] < chat['x'] < tools['x']
    canvas = await page.locator('#workspace-toggle-canvas').bounding_box()
    workspace = await page.locator('#workspace-toggle-right').bounding_box()
    tree = await page.locator('#workspace-tree').bounding_box()
    assert 0 <= workspace['x'] - (canvas['x'] + canvas['width']) <= 10
    assert 0 <= tree['x'] - (workspace['x'] + workspace['width']) <= 10


async def test_message_path_copy_paste_exact_and_cross_tree(mobile_browser, tmp_path):
    page, conv, user, assistant, _ = mobile_browser
    await db.add_message(conv['id'], 'assistant', 'Sibling branch', parent_id=user['id'])
    await open_conversation(page, conv, assistant)
    await page.evaluate('async (id) => {await switchToBranch(id, id, {exact:true}); State._skipLoadOnChat=true; switchView("chat");}', assistant['id'])
    greek = page.locator(f'#messages .message[data-msg-id="{assistant["id"]}"] .message-branch-label')
    assert 'α' in await greek.inner_text()
    assert await greek.is_visible()
    assert await page.locator('#breadcrumb-trail').is_visible()
    assert 'root' in await page.locator('#breadcrumb-trail').inner_text()
    assert await page.locator('#branch-path-input').input_value() == ''
    await page.evaluate('''() => {
        window.copiedAddress = '';
        Object.defineProperty(navigator, 'clipboard', {configurable:true, value:{writeText:async value => {window.copiedAddress=value;}, readText:async () => window.copiedAddress}});
    }''')
    await page.locator(f'.message[data-msg-id="{user["id"]}"] .message-branch-label').tap()
    feedback = await page.wait_for_function('''(id) => {
        const notice = document.querySelector('.message-path-feedback');
        if (!notice) return false;
        const source = document.querySelector(`.message[data-msg-id="${id}"] .message-branch-label`).getBoundingClientRect();
        return {text:notice.textContent, header:document.getElementById('branch-path-status').textContent,
            distance:Math.abs(notice.getBoundingClientRect().top - source.bottom)};
    }''', arg=user['id'])
    result = await feedback.json_value()
    assert result['text'] == 'Path copied' and result['header'] == ''
    assert result['distance'] < 12
    assert await page.locator('#branch-path-input').input_value() == ''
    address = await page.evaluate('copiedAddress')
    await page.locator('#btn-branch-paste').tap()
    assert await page.locator('#branch-path-input').input_value() == address
    assert f'/tree:{conv["id"]}/' in address and address.endswith(f'/msg:{user["id"]}')
    assert await page.evaluate('copiedAddress') == address
    assert await page.locator('#app-header #btn-new-thread, #app-header #btn-pin-current-branch, #app-header #btn-branch-pins, #app-header #btn-to-tree').count() == 0
    assert await page.locator('#branch-path-input').is_visible()
    await page.locator('#btn-branch-go').tap()
    await page.wait_for_function('(id) => State.loomCursor.leaf_message_id === id', arg=user['id'])
    await page.locator(f'#messages .message[data-msg-id="{user["id"]}"]').wait_for(state='visible')
    other = await db.create_conversation('Other tree')
    other_message = await db.add_message(other['id'], 'user', 'Different tree root')
    await page.evaluate('(id) => loadConversation(id)', other['id'])
    await page.locator('#branch-path-input').fill(address)
    await page.locator('#btn-branch-go').tap()
    await page.wait_for_function('(target) => State.currentConvId === target.conv && State.loomCursor.leaf_message_id === target.message && State.currentView === "chat"', arg={'conv':conv['id'], 'message':user['id']})
    await page.locator(f'#messages .message[data-msg-id="{user["id"]}"]').wait_for(state='visible')
    assert await page.evaluate('State.currentView') == 'chat'
    await page.screenshot(path=str(tmp_path / 'pixel-address-navigation.png'))
    print(f'Address preview: {tmp_path / "pixel-address-navigation.png"}')
