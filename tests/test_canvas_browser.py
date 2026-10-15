"""Actual Canvas/SDK JavaScript and Chromium iframes; all HTTP stays in memory."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.async_api import async_playwright, expect

STATIC = Path(__file__).resolve().parents[1] / 'static'
CANVAS_JS = (STATIC / 'chat.js').read_text(encoding='utf-8').split('// ── Canvas ──', 1)[1].split('// ── Code Block Preview ──', 1)[0]


@asynccontextmanager
async def canvas_page(state=None, standalone=False, legacy=False):
    state = state if state is not None else {}
    posts, frame_loads = [], []
    second_arrived = asyncio.Event()

    async def route_handler(route):
        req = route.request
        path = urlparse(req.url).path
        if urlparse(req.url).hostname != 'canvas.fixture':
            return await route.abort()
        if path == '/':
            return await route.fulfill(content_type='text/html', body='''
                <button id="btn-to-tree"></button><button id="btn-canvas-toggle"></button>
                <div id="canvas-fullview"><iframe id="canvas-fullview-iframe" data-canvas-conv="7"
                  src="/api/canvas/7/index.html"></iframe></div>
                <style>.hidden {display:none}</style>''')
        if path.startswith('/static/'):
            return await route.fulfill(content_type='application/javascript', body=(STATIC / path.rsplit('/', 1)[-1]).read_text(encoding='utf-8'))
        if path.startswith('/api/canvas/'):
            frame_loads.append(path)
            return await route.fulfill(content_type='text/html', body='<script src="/static/canvas-sdk.js"></script><h1>Fixture Canvas</h1>')
        if path.endswith('/canvas/revision'):
            state['polls'] = state.get('polls', 0) + 1
            return await route.fulfill(json={'revision': state.get('revision', 'a'), 'complete': True})
        if path.endswith('/messages') and req.method == 'POST':
            data = req.post_data_json
            posts.append(data)
            if state.get('reverse') and data['content'] == 'first':
                await asyncio.wait_for(second_arrived.wait(), timeout=5)
                await asyncio.sleep(0.05)
            if data['content'] == 'second':
                second_arrived.set()
            return await route.fulfill(json={'id': 202 if data['content'] == 'second' else 201,
                'queued': state.get('queued', False), 'generation_ids': []})
        raise AssertionError(f'Unexpected Canvas HTTP {req.method} {path}')

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.route('**/*', route_handler)
            await page.clock.install()
            await page.goto('http://canvas.fixture/')
            await page.evaluate('''() => {
                window.State = {currentConvId: 7, currentView: 'tree', currentConv: {mode: 'goose', canvas_enabled: 1}};
                window._sameConvId = (a,b) => a != null && b != null && String(a) === String(b);
                window._activeQueueParentId = () => 43;
                window._currentVisibleLeafId = () => 42;
                window._attachCCSettings = p => {p.cc_model = 'goose:fixture';};
                window._messageTopologyContract = async (parent, intent, conv) => {
                    if (window.beforeTopology) await beforeTopology();
                    return {parent_id: parent, topology_intent: intent, cursor_id: 'fixture', cursor_revision: 1};
                };
                window._acceptMessageCursor = () => {};
                window.rememberConversationLeaf = () => {};
                window.loadMessages = async () => {};
                window._triggerParallelGenerate = () => {throw new Error('Canvas must not launch twice');};
                window._queuedGeneration = null;
            }''')
            script = (STATIC / 'canvas-viewer.js').read_text(encoding='utf-8') if standalone else CANVAS_JS
            if legacy:
                script = '''addEventListener('message', e => {
                    if(e.data.source === 'loom-canvas') e.source.postMessage({source:'loom-host', type:'message-sent', id:201}, location.origin);
                });'''
            await page.add_script_tag(content=script)
            frame = page.frames[1]
            await frame.wait_for_function('typeof Loom !== "undefined"')
            yield page, frame, posts, frame_loads
            assert not errors
        finally:
            await browser.close()


async def test_sdk_concurrent_responses_match_ids_on_plain_http():
    async with canvas_page({'reverse': True}) as (page, frame, posts, loads):
        # Deliberately insecure HTTP origin also exercises the UUID fallback.
        result = await frame.evaluate('async () => Promise.all([Loom.send("first"), Loom.send("second"), Loom.getConvId()])')
        assert [r.get('id') for r in result[:2]] == [201, 202]
        assert result[2]['convId'] == 7
        assert len(posts) == 2 and posts[0]['client_request_id'] != posts[1]['client_request_id']


async def test_canvas_queued_send_uses_running_draft_and_duplicate_delivery_is_one_post():
    async with canvas_page({'queued': True}) as (page, frame, posts, loads):
        await frame.evaluate('''() => {
            window.replies = [];
            addEventListener('message', e => { if(e.data.source === 'loom-host') replies.push(e.data); });
            const request = {source: 'loom-canvas', type: 'send-message', request_id: 'fixture-repeat', content: 'continue'};
            parent.postMessage(request, location.origin);
            parent.postMessage(request, location.origin);
        }''')
        await frame.wait_for_function('replies.length === 2')
        assert len(posts) == 1
        assert posts[0]['parent_id'] == posts[0]['queue_after_draft_id'] == 43
        assert posts[0]['send_intent'] == posts[0]['topology_intent'] == 'continue'
        assert await frame.evaluate('replies.every(r => r.id === 201 && r.queued)')
        assert await page.evaluate('_queuedGeneration.id') == 201


async def test_bridge_rejects_forged_senders_and_navigation_during_preparation():
    async with canvas_page() as (page, frame, posts, loads):
        await page.evaluate('''() => {
            const data = {source: 'loom-canvas', type: 'send-message', content: 'forged'};
            dispatchEvent(new MessageEvent('message', {data, origin: location.origin, source: window}));
            dispatchEvent(new MessageEvent('message', {data, origin: 'https://foreign.invalid',
                source: document.querySelector('iframe').contentWindow}));
            window.beforeTopology = async () => { State.currentConvId = 8; };
        }''')
        result = await frame.evaluate('async () => { try { await Loom.send("stale"); } catch(e) { return e.message; } }')
        assert 'Conversation changed' in result
        assert not posts


async def test_visible_canvas_detects_any_writer_and_hidden_canvas_does_not_poll():
    state = {}
    async with canvas_page(state) as (page, frame, posts, loads):
        await page.evaluate('_pollCanvasRevision()')
        assert state['polls'] == 1 and len(loads) == 1
        state['revision'] = 'changed-by-goose-or-shell'
        await page.evaluate('_pollCanvasRevision()')
        await page.wait_for_function('document.querySelector("iframe").src.includes("?t=")')
        assert len(loads) == 2
        await page.evaluate('_pollCanvasRevision()')
        assert len(loads) == 2  # No reload loop when metadata stays unchanged.
        await page.evaluate('document.getElementById("canvas-fullview").classList.add("hidden")')
        count = state['polls']
        await page.clock.fast_forward(9000)
        assert state['polls'] == count
        await page.evaluate('openCanvasFullview()')
        await page.clock.fast_forward(1)
        assert state['polls'] > count


@pytest.mark.parametrize('mode', ['goose', 'codex', 'claude', 'local'])
async def test_close_canvas_restores_control_for_all_modes(mode):
    async with canvas_page() as (page, frame, posts, loads):
        await page.evaluate('(mode) => {State.currentConv.mode=mode; openCanvasFullview(); closeCanvasFullview();}', mode)
        await expect(page.locator('#btn-canvas-toggle')).to_be_visible()


async def test_standalone_reports_missing_cursor_immediately_not_after_timeout():
    async with canvas_page(standalone=True) as (page, frame, posts, loads):
        assert (await frame.evaluate('Loom.getConvId()'))['convId'] == 7
        result = await frame.evaluate('async () => { try { await Loom.send("do work"); } catch(e) { return e.message; } }')
        assert 'standalone viewer has no selected branch' in result
        assert not posts


async def test_sdk_upgrade_accepts_unambiguous_legacy_reply_but_never_guesses_between_sends():
    async with canvas_page(legacy=True) as (page, frame, posts, loads):
        assert (await frame.evaluate('Loom.send("single")'))['id'] == 201
        results = await frame.evaluate('async () => Promise.allSettled([Loom.send("one"), Loom.send("two")])')
        assert all(result['status'] == 'rejected' for result in results)
