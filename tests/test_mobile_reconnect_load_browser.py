"""Recovery load regression with native failed sockets and isolated storage."""
import asyncio
from playwright.async_api import expect

import database as db
from tests.test_agent_activity_browser import activity_browser, open_conversation
from tests.test_exoloom_mobile_browser import mobile_browser
from tests.test_fullstack_queue_isolation import _free_port


async def test_running_outage_polls_metadata_without_history_or_hidden_tree(mobile_browser):
    page, conv, user, assistant, _ = mobile_browser
    await open_conversation(page, conv, assistant)
    await page.evaluate('''async ({conv,assistant}) => {
        await selectServerLoomCursor(conv.id, assistant.id);
        await loadMessages(conv.id, {preserveView:true});
        switchView('chat');
    }''', {'conv': conv, 'assistant': assistant})
    await page.wait_for_load_state('networkidle')
    await page.wait_for_function('!refreshTree._pending && !State.ws._needsSync')
    await db.register_active_generation(assistant['id'], conv['id'], 123456789)
    requests = []
    page.on('request', lambda request: requests.append(request.url))
    await page.evaluate('''({port,assistant}) => {
        const Native = WebSocket;
        window.failedSocketCount = 0;
        window.WebSocket = new Proxy(Native, {construct(target,args) {
            const url = new URL(args[0]); url.port=String(port);
            const socket = new target(url.toString(),...args.slice(1));
            socket.addEventListener('error',()=>failedSocketCount++);
            return socket;
        }});
        window.hiddenTreeRenders = 0;
        const originalRender = renderTree;
        renderTree = (...args) => {hiddenTreeRenders++; return originalRender(...args);};
        State.isStreaming = true;
        State._followingGenId = 777;
        State._followingDraftMsgId = assistant.id;
        State._liveStreamDraft = {...assistant, content:'Partial answer', content_blocks:null};
        document.querySelector(`.message[data-msg-id="${assistant.id}"]`).remove();
        appendStreamingMessage(null,null,assistant.id);
        State.ws.close(1000,'Fixture network outage');
    }''', {'port': _free_port(), 'assistant': assistant})
    requests.clear()
    for _ in range(24):
        if sum('/api/generations' in url for url in requests) >= 2:
            break
        await asyncio.sleep(.5)
    assert sum('/api/generations' in url for url in requests) >= 2
    assert not any('/tree' in url or '/branch/' in url for url in requests), requests
    assert await page.evaluate('hiddenTreeRenders') == 0
    assert await page.evaluate('failedSocketCount') > 0
    await expect(page.locator('#chat-connection-status')).to_contain_text('A turn is running')
    await page.locator('#user-input').fill('Typing remains available during outage')
    await db.update_message_content(assistant['id'], turn_lifecycle_state='completed', generation_ms=1000)
    await db.unregister_active_generation(assistant['id'])
    await page.wait_for_function('!State.isStreaming', timeout=12000)
    assert 'Saved paragraph 59' in await page.locator(f'.message[data-msg-id="{assistant["id"]}"]').inner_text()
    assert await page.locator('#user-input').input_value() == 'Typing remains available during outage'
    requests.clear()
    await asyncio.sleep(7)
    assert not any('/tree' in url or '/branch/' in url for url in requests), requests
    assert await page.evaluate('hiddenTreeRenders') == 0


async def test_reopened_page_without_remembered_run_shows_status_and_lands_final(mobile_browser):
    page, conv, user, assistant, _ = mobile_browser
    await open_conversation(page, conv, assistant)
    await page.evaluate('''async ({conv,assistant}) => {
        await selectServerLoomCursor(conv.id,assistant.id);
        await loadMessages(conv.id,{preserveView:true});
        switchView('chat');
    }''', {'conv': conv, 'assistant': assistant})
    await page.wait_for_load_state('networkidle')
    await page.wait_for_function('!refreshTree._pending && !State.ws._needsSync')
    await db.register_active_generation(assistant['id'],conv['id'],123456789)
    await page.evaluate('''port => {
        const Native=WebSocket;
        window.outageNativeSocket=Native;
        window.WebSocket=new Proxy(Native,{construct(target,args) {
            const url=new URL(args[0]);url.port=String(port);
            return new target(url.toString(),...args.slice(1));
        }});
        _clearTerminalStreamState({clearParallel:true});
        State.ws.close(1000,'Reopened page without a live socket');
    }''', _free_port())
    await page.wait_for_function('document.getElementById("chat-connection-status")?.textContent.includes("A turn is running")')
    assert await page.evaluate('State._followingGenId') is None
    assert not await page.evaluate('State.isStreaming')
    await page.locator('#user-input').fill('Draft remains intact')
    await db.update_message_content(assistant['id'],content='Final reply after reopen',
        content_blocks='[{"type":"text","text":"Final reply after reopen"}]',
        turn_lifecycle_state='completed',generation_ms=1000)
    await db.unregister_active_generation(assistant['id'])
    await page.wait_for_function('document.getElementById("messages").textContent.includes("Final reply after reopen")',timeout=12000)
    assert await page.locator('#user-input').input_value() == 'Draft remains intact'
    await page.evaluate('''() => {
        window.WebSocket=outageNativeSocket;
        State.ws._replaced=true;
        State.ws.close();
        connectWebSocket(State.currentConvId);
    }''')
    await page.wait_for_function('State.ws?.readyState === WebSocket.OPEN && !document.getElementById("chat-connection-status")')
    assert await page.locator('#chat-connection-status').count() == 0


async def test_snapshot_and_direct_tree_calls_do_not_build_hidden_mobile_tree(mobile_browser):
    page, conv, user, assistant, _ = mobile_browser
    await open_conversation(page, conv, assistant)
    await page.wait_for_load_state('networkidle')
    result = await page.evaluate('''async convId => {
        switchView('chat');
        let created=0;
        const original=createNode;
        createNode=(...args)=>{created++;return original(...args);};
        await _syncMessagesForStreaming(convId);
        const metadata=State.treeData;
        State.treeData=Array.from({length:5000},(_,i)=>({id:i+100000,parent_id:null,role:'assistant',created_at:0}));
        await renderTree();
        const hiddenCreated=created;
        State.treeData=metadata;
        switchView('tree');
        await renderTree();
        createNode=original;
        return {hiddenCreated,visibleCreated:created};
    }''', conv['id'])
    assert result['hiddenCreated'] == 0
    assert result['visibleCreated'] > 0
