"""Touch Chromium QA at /app/: isolated Main, no credentials or providers."""
import asyncio
from pathlib import Path

import pytest
import database as db
from tests.test_agent_activity_browser import activity_browser, open_conversation


@pytest.fixture
async def mobile_browser(activity_browser):
    original, conv, user, assistant, runs = activity_browser
    context = await original.context.browser.new_context(
        viewport={'width':412,'height':915}, device_scale_factor=2,
        is_mobile=True, has_touch=True,
        user_agent='Mozilla/5.0 (Linux; Android 15; Pixel 9a) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Mobile Safari/537.36')
    page = await context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    await page.route('**/app/**', lambda route: route.fulfill(
        path=str(Path('static/index.html').resolve()), content_type='text/html'))
    origin = original.url.split('/')[0]+'//'+original.url.split('/')[2]
    await page.goto(origin+'/app/', wait_until='domcontentloaded')
    # Startup may replace the route after the shell first becomes available.
    # Finish that navigation before tests invoke loadConversation in the page.
    await page.wait_for_load_state('networkidle')
    await page.wait_for_function("typeof State !== 'undefined' && !document.body.classList.contains('app-booting')")
    try:
        yield page, conv, user, assistant, runs
        assert not errors, errors
    finally:
        await context.close()


async def test_mobile_regenerate_reconnects_and_preserves_action(mobile_browser):
    page, conv, user, assistant, runs = mobile_browser
    await open_conversation(page, conv, assistant)
    await page.evaluate('''() => {
        window.mobileLaunches = [];
        const send = WebSocket.prototype.send;
        WebSocket.prototype.send = function (raw) {
            const msg = JSON.parse(raw);
            if (['generate','regenerate'].includes(msg.action)) {mobileLaunches.push(msg); return;}
            return send.call(this, raw);
        };
        State.ws._replaced = true; State.ws.close();
    }''')
    await page.locator(f'.message[data-msg-id="{assistant["id"]}"] button[title="Regenerate"]').tap()
    await page.wait_for_function('mobileLaunches.length === 1')
    launch = await page.evaluate('mobileLaunches[0]')
    assert launch['action'] == 'regenerate'
    assert launch['parent_id'] == user['id']
    assert launch['loom_thread_id'] == await page.evaluate('State.loomCursor.thread_id')
    # Sending the packet is not admission. Keep the old answer until the
    # server acknowledges a replacement; a disconnected launch cannot erase it.
    assert await page.locator('.message[data-msg-id="%s"]' % assistant['id']).count() == 1


async def test_mobile_branch_paints_while_discovery_is_pending(mobile_browser):
    page, conv, user, assistant, runs = mobile_browser
    await open_conversation(page, conv, assistant)
    sibling = await db.add_message(conv['id'], 'assistant', 'New branch painted without refresh', parent_id=user['id'])
    release = asyncio.Event()
    async def delay(route):
        await release.wait()
        await route.continue_()
    await page.route('**/api/cc-models', delay)
    try:
        await page.evaluate('id => { void switchToBranch(id, id, {exact:true}); }', sibling['id'])
        await page.locator(f'.message[data-msg-id="{sibling["id"]}"]').wait_for(state='visible', timeout=5000)
        assert not release.is_set()
        assert await page.evaluate('State.loomCursor.leaf_message_id') == sibling['id']
    finally:
        release.set()
        await page.unroute('**/api/cc-models', delay)


async def test_mobile_casual_upload_survives_navigation_and_blocks_early_send(mobile_browser):
    page, *_ = mobile_browser
    await page.evaluate('startCasualChat()')
    await page.locator('#user-input').fill('Use my attachment')
    await page.evaluate('''() => {
        API.upload = () => new Promise(resolve => window.finishUpload = resolve);
        window.uploadTask = attachImage(new File(['fixture'], 'mobile-notes.txt', {type:'text/plain'}));
    }''')
    await page.locator('#btn-send').tap()
    assert await page.evaluate('State.currentConvId') is None
    assert await page.locator('#user-input').input_value() == 'Use my attachment'
    await page.evaluate("finishUpload({path:'/fixture/mobile-notes.txt',url:'/uploads/mobile-notes.txt',is_image:false})")
    await page.evaluate('uploadTask')
    assert await page.locator('.file-thumb-name').inner_text() == 'mobile-notes.txt'
    await page.locator('#header-search-space').tap()
    await page.locator('#workspace-search-chat').tap()
    await page.wait_for_selector('#view-chat.active')
    assert await page.evaluate('State.pendingImages[0].path') == '/fixture/mobile-notes.txt'
    assert await page.locator('#user-input').input_value() == 'Use my attachment'


async def test_upload_finishing_after_branch_switch_stays_in_original_draft(mobile_browser):
    page, conv, user, assistant, runs = mobile_browser
    await open_conversation(page, conv, assistant)
    sibling = await db.add_message(conv['id'], 'assistant', 'Other branch', parent_id=user['id'])
    await page.locator('#user-input').fill('Draft on original branch')
    await page.evaluate('''() => {
        window.originalDraftKey = _composerDraftStorageKey(State.currentConvId, State.loomCursor);
        API.upload = () => new Promise(resolve => window.finishUpload = resolve);
        window.uploadTask = attachImage(new File(['fixture'], 'original.txt'));
    }''')
    await page.evaluate('id => switchToBranch(id, id, {exact:true})', sibling['id'])
    await page.evaluate("finishUpload({path:'/fixture/original.txt',url:'/uploads/original.txt',is_image:false})")
    await page.evaluate('uploadTask')
    assert await page.evaluate('State.pendingImages.length') == 0
    assert await page.evaluate('JSON.parse(sessionStorage.getItem(originalDraftKey)).pendingImages[0].path') == '/fixture/original.txt'
    await page.evaluate('id => switchToBranch(id, id, {exact:true})', assistant['id'])
    assert await page.evaluate('State.pendingImages[0].path') == '/fixture/original.txt'


@pytest.mark.parametrize('return_before_finish', [False, True])
async def test_casual_upload_finishing_during_search_keeps_draft(mobile_browser, return_before_finish):
    page, *_ = mobile_browser
    await page.evaluate('startCasualChat()')
    await page.locator('#user-input').fill('Draft with a slow phone upload')
    await page.evaluate('''() => {
        API.upload = () => new Promise(resolve => window.finishUpload = resolve);
        window.uploadTask = attachImage(new File(['fixture'], 'slow.txt'));
    }''')
    await page.locator('#header-search-space').tap()
    if return_before_finish:
        await page.locator('#workspace-search-chat').tap()
        await page.wait_for_selector('#view-chat.active')
    await page.evaluate("finishUpload({path:'/fixture/slow.txt',url:'/uploads/slow.txt',is_image:false})")
    await page.evaluate('uploadTask')
    if not return_before_finish:
        await page.locator('#workspace-search-chat').tap()
        await page.wait_for_selector('#view-chat.active')
    assert await page.evaluate('State.pendingImages[0].path') == '/fixture/slow.txt'
    assert await page.locator('#user-input').input_value() == 'Draft with a slow phone upload'


async def test_old_casual_upload_does_not_attach_to_fresh_chat(mobile_browser):
    page, *_ = mobile_browser
    await page.evaluate('startCasualChat()')
    await page.evaluate('''() => {
        API.upload = () => new Promise(resolve => window.finishUpload = resolve);
        window.uploadTask = attachImage(new File(['fixture'], 'old.txt'));
        startCasualChat();
    }''')
    await page.locator('#user-input').fill('A separate draft')
    await page.evaluate("finishUpload({path:'/fixture/old.txt',url:'/uploads/old.txt',is_image:false})")
    await page.evaluate('uploadTask')
    assert await page.evaluate('State.pendingImages.length') == 0
    assert await page.locator('#user-input').input_value() == 'A separate draft'


async def test_mobile_appearance_controls_persist_and_fit(mobile_browser, tmp_path):
    page, *_ = mobile_browser
    await page.evaluate("openModal('modal-settings'); document.querySelector('[data-settings-tab=display]').click()")
    await page.locator('#appearance-theme').select_option('exoloom')
    await page.locator('#appearance-font').select_option('orbitron')
    await page.locator('#appearance-scene').select_option('animated')
    await page.locator('#appearance-color').select_option('violet')
    await page.locator('#appearance-stationary').check()
    await page.locator('#appearance-fps').select_option('20')
    assert await page.evaluate("document.documentElement.dataset.loomTheme") == 'exoloom'
    saved = await page.evaluate('EXOAppearance.settings()')
    assert saved == {'theme':'exoloom','scene':'animated','font':'orbitron','color':'violet','grid':False,'floor':True,'stationary':True,'fps':20}
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    await page.screenshot(path=str(tmp_path/'exoloom-mobile-display.png'))
    await page.reload(wait_until='domcontentloaded')
    await page.wait_for_function("typeof EXOAppearance !== 'undefined'")
    assert await page.evaluate('EXOAppearance.settings()') == saved


async def test_mobile_file_picker_upload_reaches_send_payload(mobile_browser, monkeypatch, tmp_path):
    from config import config
    monkeypatch.setattr(config, 'upload_dir', str(tmp_path))
    page, conv, user, assistant, _ = mobile_browser
    await open_conversation(page, conv, assistant)
    await page.locator('#file-input').set_input_files({
        'name':'phone-notes.txt', 'mimeType':'text/plain', 'buffer':b'Actual mobile upload fixture'})
    await page.wait_for_function("State.pendingImages.length === 1 && _attachmentUploads === 0")
    attachment = await page.evaluate('State.pendingImages[0]')
    assert Path(attachment['path']).read_bytes() == b'Actual mobile upload fixture'
    await page.evaluate('''() => {
        window.mobileSends = [];
        const post = API.post.bind(API);
        API.post = async function (path, payload) {
            if (path.endsWith('/messages')) {
                mobileSends.push(payload);
                throw new Error('Fixture stops before provider launch');
            }
            return post(path, payload);
        };
    }''')
    await page.locator('#user-input').fill('Read these notes')
    await page.locator('#btn-send').tap()
    await page.wait_for_function('mobileSends.length === 1')
    assert attachment['path'] in (await page.evaluate('mobileSends[0]'))['image_path']


async def test_mobile_blackhole_shader_and_landscape(mobile_browser, tmp_path):
    page, *_ = mobile_browser
    shader_errors = []
    page.on('console', lambda msg: shader_errors.append(msg.text)
            if msg.type == 'error' and ('Shader' in msg.text or 'WebGL' in msg.text) else None)
    await page.reload(wait_until='domcontentloaded')
    await page.evaluate("EXOAppearance.apply({theme:'exoloom',scene:'animated',font:'readable',color:'cyan',grid:true,floor:true,stationary:true,fps:20})")
    await page.wait_for_selector('#exoloom-scene.ready', timeout=30000)
    await asyncio.sleep(1)
    frame = page.frame_locator('#exoloom-scene')
    await frame.locator('#scene canvas').wait_for(state='attached')
    cyan = await page.screenshot(path=str(tmp_path/'blackhole-cyan.png'))
    await page.evaluate("EXOAppearance.apply({...EXOAppearance.settings(),color:'violet'})")
    await asyncio.sleep(.5)
    violet = await page.screenshot(path=str(tmp_path/'blackhole-violet.png'))
    assert cyan != violet, 'Changing the palette must change the rendered disk'
    assert not shader_errors, shader_errors
    await page.screenshot(path=str(tmp_path/'exoloom-mobile-home.png'))
    await page.set_viewport_size({'width':915,'height':412})
    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    await page.screenshot(path=str(tmp_path/'exoloom-mobile-landscape.png'))


async def test_portal_navigation_and_reload_keep_app_prefix(mobile_browser):
    page, conv, user, assistant, _ = mobile_browser
    await open_conversation(page, conv, assistant)
    assert '/app/?' in page.url
    assert '/app/?' in await page.evaluate('conversationWindowUrl(State.currentConvId)')
    await page.reload(wait_until='domcontentloaded')
    await page.wait_for_function('State.currentConvId !== null && !document.body.classList.contains("app-booting")')
    assert await page.evaluate('State.loomCursor.leaf_message_id') == assistant['id']
    await page.locator('#header-search-space').tap()
    assert '/app/search' in page.url
    await page.reload(wait_until='domcontentloaded')
    await page.wait_for_selector('#view-search.active')
    assert '/app/search' in page.url
    await page.evaluate('''() => renderSearchSpaceTurn({question:'Fixture',answer:'Fixture',sources:[
        {conversation_id:1,message_id:2,source:'Fixture',conversation_title:'Original chat',snippet:'Saved answer'}]})''')
    assert '/app/?' in await page.locator('.search-space-source').last.get_attribute('href')


async def test_mobile_side_window_search_stays_open_during_resize_and_filter(mobile_browser):
    page, conv, user, assistant, _ = mobile_browser
    await open_conversation(page, conv, assistant)

    # 1. Open branches side window on mobile
    await page.locator('#workspace-toggle-left').tap()
    assert await page.locator('#workspace-left').is_visible()
    assert await page.locator('#workspace-branch-filter').is_visible()

    # Simulate mobile virtual keyboard appearing (triggers window resize)
    await page.set_viewport_size({'width': 412, 'height': 500})
    await page.locator('#workspace-branch-filter').fill('Saved')
    assert await page.locator('#workspace-left').is_visible()
    assert not await page.locator('#workspace-left').evaluate('(node) => node.hidden')
    assert await page.locator('#workspace-branches button.workspace-item').count() > 0

    # 2. Switch to conversations side window on mobile
    await page.locator('#workspace-projects').tap()
    assert await page.locator('#workspace-left').is_visible()
    assert await page.locator('#workspace-conversation-filter').is_visible()

    # Simulate mobile keyboard resize while searching conversations
    await page.set_viewport_size({'width': 412, 'height': 520})
    await page.locator('#workspace-conversation-filter').fill('Activity')
    assert await page.locator('#workspace-left').is_visible()
    assert not await page.locator('#workspace-left').evaluate('(node) => node.hidden')
    assert await page.locator('#workspace-conversations button.workspace-item').count() > 0


async def test_mobile_stream_footer_and_model_pill_fit_and_shrinkwrap(mobile_browser):
    page, conv, user, assistant, _ = mobile_browser
    await open_conversation(page, conv, assistant)

    # 1. Model request pill shrink-wraps without empty negative space on mobile
    await page.evaluate('''({assistant}) => {
        const rendered = {...assistant, model_attestation: JSON.stringify({
            status: 'verified', harness: 'gemini', effective_model: 'gemini-3.8-flash-high', session_continuity: 'resumed'
        })};
        document.querySelector(`.message[data-msg-id="${assistant.id}"]`).replaceWith(createMessageElement(rendered));
    }''', {'assistant': assistant})
    pill = page.locator(f'.message[data-msg-id="{assistant["id"]}"] .model-attestation summary')
    assert await pill.is_visible()
    pill_box = await pill.bounding_box()
    msg_box = await page.locator(f'.message[data-msg-id="{assistant["id"]}"]').bounding_box()
    assert pill_box['width'] < msg_box['width'] * 0.75, f"Pill too wide: {pill_box['width']} vs msg {msg_box['width']}"

    # 2. Loom streaming animation fits on one line with stats
    await page.evaluate('''() => {
        State.isStreaming = true;
        appendStreamingMessage('gemini:gemini-3.8-flash-high', {
            status: 'verified', harness: 'gemini', effective_model: 'gemini-3.8-flash-high'
        }, 99999);
        document.querySelector('.streaming .looming-phase').classList.remove('hidden');
        document.querySelector('.streaming .gen-stats .gen-token-info').textContent = 'in 30k out 1.9k - ';
        document.querySelector('.streaming .gen-stats .gen-timer').textContent = '0:37';
    }''')

    anim_rect = await page.evaluate('''() => {
        const anim = document.querySelector('.streaming .loom-anim');
        const stats = document.querySelector('.streaming .gen-stats');
        const r1 = anim.getBoundingClientRect();
        const r2 = stats.getBoundingClientRect();
        return {
            animTop: r1.top,
            statsTop: r2.top,
            sameLine: Math.abs(r1.top - r2.top) < 15
        };
    }''')
    assert anim_rect['sameLine'], f"Animation and stats not on same line: {anim_rect}"
