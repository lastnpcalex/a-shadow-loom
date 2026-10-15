"""Chat sidebar navigation against an isolated HTTP server and database."""
import pytest
import json
import asyncio
import database as db

from tests.test_agent_activity_browser import activity_browser, open_conversation


async def test_search_opens_and_closes_while_model_discovery_is_pending(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    release = asyncio.Event()

    async def pending_discovery(route):
        await release.wait()
        await route.fulfill(json=[])

    await page.route('**/api/cc-models', pending_discovery)
    try:
        await page.goto(page.url.split('?')[0].rstrip('/') + '/search', wait_until='domcontentloaded')
        await page.wait_for_selector('#view-search.active')
        assert not release.is_set()
        await page.locator('#btn-search-space-close').click()
        assert await page.evaluate('State.currentView') == 'home'
    finally:
        release.set()


async def test_fresh_landing_keeps_unsent_draft_in_saved_exohome(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    assert await page.evaluate('State.currentConv.title') == 'ExoHome'
    home_id = await page.evaluate('State.currentConvId')
    assert len(await db.list_conversations()) == 2
    await page.locator('#user-input').fill('Unsent casual draft')
    await page.reload(wait_until='domcontentloaded')
    await page.wait_for_function("!document.body.classList.contains('app-booting')")
    assert await page.locator('#user-input').input_value() == 'Unsent casual draft'
    assert len(await db.list_conversations()) == 2
    assert await page.evaluate('State.currentConvId') == home_id


async def test_casual_attachments_and_description_survive_search(activity_browser):
    page, *_ = activity_browser
    await page.evaluate('startCasualChat()')
    await page.wait_for_selector('#workspace-new-chat-settings')
    await page.locator('#user-input').fill('Draft with a file')
    await page.evaluate("""() => {
        State.pendingImages = [{name:'notes.txt', is_image:false, text:'attached notes'}];
        document.getElementById('describe-context').value = 'Use these notes';
        saveComposerDraft();
    }""")
    await page.locator('#header-search-space').click()
    await page.locator('#workspace-search-chat').click()
    await page.wait_for_selector('#view-chat.active')
    assert await page.evaluate('State.pendingImages[0].name') == 'notes.txt'
    assert await page.locator('#describe-context').input_value() == 'Use these notes'
    await page.evaluate('removePendingImage(0)')
    await page.locator('#header-search-space').click()
    await page.locator('#workspace-search-chat').click()
    await page.wait_for_selector('#view-chat.active')
    assert await page.evaluate('State.pendingImages.length') == 0


async def test_pending_search_chat_navigation_does_not_override_library(activity_browser):
    page, *_ = activity_browser
    await page.evaluate('startCasualChat()')
    await page.wait_for_selector('#workspace-new-chat-settings')
    await page.locator('#header-search-space').click()
    await page.evaluate("""() => {
        populateCCModelDropdowns = () => new Promise(resolve => window.releaseModels = resolve);
    }""")
    await page.locator('#workspace-search-chat').click()
    await page.locator('#workspace-search-library').click()
    await page.evaluate('window.releaseModels()')
    assert await page.evaluate('State.currentView') == 'home'


async def test_partial_casual_loading_failure_restores_draft_on_reload(activity_browser, tmp_path, monkeypatch):
    import server
    page, *_ = activity_browser
    monkeypatch.setattr(server, '_CASUAL_CHAT_ROOT', tmp_path / 'ExoHome')
    await page.evaluate('startCasualChat()')
    await page.wait_for_selector('#workspace-new-chat-settings')
    await page.evaluate("document.getElementById('workspace-new-model').value = 'sonnet'")
    await page.locator('#user-input').fill('Question before loading failed')

    async def fail_thread_creation(route):
        await route.fulfill(status=503, json={'detail': 'Injected loading failure'})

    await page.route('**/api/conversations/*/threads', fail_thread_creation)
    await page.evaluate('sendMessage()')
    assert await page.evaluate('State.currentConvId') is None
    assert await page.locator('#user-input').input_value() == 'Question before loading failed'
    assert len(await db.list_conversations()) == 3
    await page.reload(wait_until='domcontentloaded')
    await page.wait_for_selector('#workspace-new-chat-settings')
    assert await page.locator('#user-input').input_value() == 'Question before loading failed'
    assert await page.locator('#workspace-new-model').is_disabled()


async def test_navigation_during_casual_loading_does_not_send_or_override_library(activity_browser, tmp_path, monkeypatch):
    import server
    page, *_ = activity_browser
    monkeypatch.setattr(server, '_CASUAL_CHAT_ROOT', tmp_path / 'ExoHome')
    await page.evaluate('startCasualChat()')
    ready, release = asyncio.Event(), asyncio.Event()
    posted = []

    async def delay_threads(route):
        ready.set()
        await release.wait()
        await route.continue_()

    async def block_provider(route):
        if route.request.method == 'POST':
            posted.append(route.request.post_data_json)
            await route.abort()
        else:
            await route.continue_()

    await page.wait_for_selector('#workspace-new-chat-settings')
    await page.route('**/api/conversations/*/threads', delay_threads)
    await page.route('**/api/conversations/*/messages', block_provider)
    await page.evaluate("document.getElementById('workspace-new-model').value = 'sonnet'")
    await page.locator('#user-input').fill('Do not send after navigation')
    await page.evaluate('void sendMessage()')
    try:
        await asyncio.wait_for(ready.wait(), 10)
        # The loading overlay can block clicks; this is the same view action.
        await page.evaluate("switchView('home')")
    finally:
        release.set()
    await page.wait_for_function('!_casualSendBusy')
    assert await page.evaluate('State.currentView') == 'home'
    assert not posted


async def test_sidebar_canvas_uses_existing_identity_and_workspace_file_scope(activity_browser, tmp_path):
    page, conv, user, assistant, runs = activity_browser
    (tmp_path / 'notes.md').write_text('public notes')
    (tmp_path / '.env').write_text('private data')
    await open_conversation(page, conv, assistant)
    await page.evaluate("switchView('chat')")
    await page.wait_for_selector('#workspace-files a[href]')
    assert await page.locator('#workspace-files').get_by_text('notes.md', exact=True).get_attribute('href') == f"/api/conversations/{conv['id']}/file?path=notes.md"
    assert await page.locator('#workspace-files').get_by_text('.env', exact=True).count() == 0
    await page.locator('#btn-canvas-toggle').click()
    await page.wait_for_selector('#workspace-canvas-iframe')
    assert await page.evaluate("""() => {
        const frame = document.getElementById('workspace-canvas-iframe');
        return _canvasMessageFrame({origin:location.origin,source:frame.contentWindow},State.currentConvId) === frame
            && _canvasMessageFrame({origin:location.origin,source:window},State.currentConvId) === undefined;
    }""")
    await page.screenshot(path=str(tmp_path / 'chat-workspace.png'))
    await page.locator('#workspace-toggle-right').click()
    assert await page.locator('#workspace-canvas-iframe').evaluate('(node) => node.hidden')


async def test_casual_first_send_retries_lost_creation_without_duplicate_chat(activity_browser, tmp_path, monkeypatch):
    import server
    page, conv, user, assistant, runs = activity_browser
    monkeypatch.setattr(server, '_CASUAL_CHAT_ROOT', tmp_path / 'ExoHome')
    created = []
    sends = []

    async def lose_first_creation_response(route):
        response = await route.fetch()
        created.append((await response.json())['id'])
        if len(created) == 1:
            await route.abort('failed')
        else:
            await route.fulfill(response=response)

    async def persist_without_provider(route):
        if route.request.method != 'POST':
            await route.continue_()
            return
        payload = route.request.post_data_json
        payload['launch_generation'] = False
        response = await route.fetch(post_data=json.dumps(payload))
        data = await response.json()
        sends.append((response.status, data))
        data['generation_ids'] = []
        await route.fulfill(response=response, json=data)

    await page.route('**/api/casual-chats', lose_first_creation_response)
    await page.route('**/api/conversations/*/messages', persist_without_provider)
    await page.evaluate("startCasualChat(); document.getElementById('workspace-new-model').value = 'sonnet'")
    await page.locator('#user-input').fill('Preserve this first question')
    await page.evaluate('sendMessage()')
    assert await page.locator('#user-input').input_value() == 'Preserve this first question'
    assert await page.evaluate('State.currentConvId') is None
    assert await page.locator('#workspace-new-model').is_disabled()
    assert await page.locator('#workspace-new-effort').is_disabled()
    assert await page.locator('#workspace-new-model').input_value() == 'sonnet'
    await page.evaluate('Promise.all([sendMessage(), sendMessage()])')
    assert len(created) == 2 and created[0] == created[1]
    tree = await db.get_conversation_tree(created[0])
    assert len(tree) == 1, {'sends': sends, 'toasts': await page.locator('#toast-container').inner_text()}
    assert (await db.get_message(tree[0]['id']))['content'] == 'Preserve this first question'
    assert tree[0]['parent_id'] is None


async def test_sidebar_selects_exact_branch_and_keeps_library_reachable(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    await open_conversation(page, conv, assistant)
    await page.evaluate("switchView('chat')")
    await page.wait_for_selector('#workspace-branches .workspace-item')
    await page.locator('#workspace-branches [data-leaf-id]').first.click()
    assert await page.evaluate('State.currentView') == 'chat'
    assert await page.evaluate('State.loomCursor.leaf_message_id') == assistant['id']
    assert await page.locator('#workspace-branches [data-leaf-id]').first.get_attribute('aria-current') == 'true'
    await page.locator('#workspace-library').click()
    assert await page.evaluate('State.currentView') == 'home'
    assert await page.evaluate('State.currentConvId') == conv['id']


async def test_space_entry_defaults_blank_and_branches_are_recent_first(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    newer = await db.add_message(conv['id'], 'user', 'Newest independent question')
    other = await db.create_conversation('Other space', mode='codex')
    await db.add_message(other['id'], 'user', 'Must not appear in this branch list')
    await page.evaluate('async () => { State.conversations = await API.get("/api/conversations"); renderConversationList(); }')
    await page.evaluate("switchView('home')")
    while await page.locator('#conv-list .conv-folder-group.is-collapsed .conv-folder-header').count():
        await page.locator('#conv-list .conv-folder-group.is-collapsed .conv-folder-header').first.click()
    await page.locator('.conv-title-text').get_by_text(conv['title'], exact=True).click()
    await page.wait_for_selector('#view-chat.active')
    assert await page.evaluate('State.messages.length') == 0
    assert await page.evaluate('State.loomCursor.leaf_message_id') is None
    assert await page.locator('#workspace-project-panel').evaluate('(node) => node.hidden')
    rows = page.locator('#workspace-branches [data-leaf-id]')
    assert await rows.evaluate_all('(nodes) => nodes.map(node => Number(node.dataset.leafId))') == [newer['id'], assistant['id']]
    assert 'Must not appear' not in await page.locator('#workspace-branches').inner_text()
    await page.locator('#workspace-branches [data-leaf-id]').last.click()
    await page.wait_for_function('id => State.loomCursor?.leaf_message_id === id', arg=assistant['id'])
    assert await page.evaluate('State.loomCursor.leaf_message_id') == assistant['id']
    await page.locator('#workspace-new-chat').click()
    await page.wait_for_function('State.loomCursor?.leaf_message_id == null && !_newThreadInFlight')
    assert await page.evaluate('State.currentConvId') == conv['id']
    await page.locator('#workspace-tree').click()
    assert await page.evaluate('State.currentView') == 'tree'
    await page.evaluate("switchView('chat')")
    await page.locator('#workspace-projects').click()
    assert await page.locator('#workspace-project-panel').is_visible()
    await page.locator('#workspace-conversation-filter').fill('Other space')
    await page.locator('#workspace-conversations .workspace-item').click()
    await page.wait_for_function('document.getElementById("workspace-project-panel").hidden')
    assert await page.evaluate('State.currentConvId') == other['id']
    assert await page.evaluate('State.loomCursor.leaf_message_id') is None


@pytest.mark.parametrize('phone', [True, False])
async def test_sidebar_defaults_use_phone_pointer_not_width_alone(activity_browser, phone):
    page, conv, user, assistant, runs = activity_browser
    await page.set_viewport_size({'width': 700, 'height': 850})
    # Model actual pointer capabilities, including a narrow hybrid laptop.
    await page.add_init_script("""phone => {
        const native = window.matchMedia.bind(window);
        window.matchMedia = query => {
            if (query === '(pointer: coarse)' || query === '(any-pointer: fine)') {
                const result = native(query);
                Object.defineProperty(result, 'matches', {value: query === '(pointer: coarse)' ? phone : !phone});
                return result;
            }
            return native(query);
        };
    }""".replace('phone => {', f'(() => {{ const phone = {str(phone).lower()};') + ')();')
    await page.reload(wait_until='domcontentloaded')
    await page.wait_for_function("typeof workspaceSetOpen === 'function' && typeof State !== 'undefined'")
    await open_conversation(page, conv, assistant)
    await page.evaluate("switchView('chat')")
    assert await page.locator('#workspace-left').evaluate('(node) => node.hidden') is phone
    assert await page.locator('#workspace-right').evaluate('(node) => node.hidden') is phone
    if phone:
        await page.locator('#workspace-toggle-left').click()
        await page.locator('#workspace-toggle-right').click()
        assert await page.locator('#workspace-left').evaluate('(node) => node.hidden')
        assert not await page.locator('#workspace-right').evaluate('(node) => node.hidden')
        await page.keyboard.press('Escape')
        assert await page.locator('#workspace-right').evaluate('(node) => node.hidden')
