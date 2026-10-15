"""Rendered phone geometry and recovery against a disposable Main instance."""
from pathlib import Path

import pytest
import database as db
from tests.test_agent_activity_browser import activity_browser, open_conversation
from tests.test_exoloom_mobile_browser import mobile_browser
from tests.test_fullstack_queue_isolation import _free_port


async def test_http_lands_answer_when_every_replacement_socket_fails(mobile_browser):
    page, conv, user, assistant, _ = mobile_browser
    # Exercise Chromium's real connection failure against a closed local port.
    # HTTP still reaches the disposable Main; no fake socket delivers events.
    await page.evaluate('''port => {
        const Native = window.WebSocket;
        window.failedSocketCount = 0;
        window.WebSocket = new Proxy(Native,{construct(target,args) {
            const url = new URL(args[0]);url.port=String(port);
            const socket = new target(url.toString(),...args.slice(1));
            socket.addEventListener('error',()=>window.failedSocketCount++);
            return socket;
        }});
    }''', _free_port())
    await page.evaluate('id => loadConversation(id)', conv['id'])
    await page.evaluate('async ({conv,assistant}) => {await selectServerLoomCursor(conv.id,assistant.id);await loadMessages(conv.id,{preserveView:true});}', {'conv':conv,'assistant':assistant})
    await page.locator('#user-input').fill('Next prompt survives the outage')
    await page.evaluate('id => editMessage(id)', user['id'])
    await page.locator('.edit-message-input').fill('Unsaved edit survives the outage')
    await page.evaluate('''assistant => {
        window.editorBefore = document.querySelector('.edit-message-input');
        State.isStreaming = true;
        State._followingGenId = 777;
        State._followingDraftMsgId = assistant.id;
        State._liveStreamDraft = {...assistant,content:'Partial answer',content_blocks:null};
        document.querySelector(`.message[data-msg-id="${assistant.id}"]`).remove();
        appendStreamingMessage(null,null,assistant.id);
        streamingDiv.querySelector('.message-content').textContent='Partial answer';
        State.ws.close(1000,'Fixture lost tunnel');
    }''', assistant)
    await db.update_message_content(assistant['id'], turn_lifecycle_state='completed', generation_ms=1000)
    await page.wait_for_function('!State.isStreaming && !document.querySelector(".message.streaming")', timeout=12000)
    assert await page.evaluate('failedSocketCount') > 0, 'Chromium must actually fail to connect'
    assert 'Saved paragraph 59' in await page.locator(f'.message[data-msg-id="{assistant["id"]}"]').inner_text()
    assert await page.locator('#user-input').input_value() == 'Next prompt survives the outage'
    assert await page.locator('.edit-message-input').input_value() == 'Unsaved edit survives the outage'
    assert await page.evaluate('editorBefore === document.querySelector(".edit-message-input")')
    assert await page.evaluate('State.loomCursor.leaf_message_id') == assistant['id']


async def test_reconnect_lands_saved_answer_while_typing_and_editing(mobile_browser):
    page, conv, user, assistant, _ = mobile_browser
    await db.update_message_content(assistant['id'], turn_lifecycle_state='completed', generation_ms=1000)
    await open_conversation(page, conv, assistant)
    await page.evaluate('switchView("chat")')
    await page.locator('#user-input').fill('My next prompt stays here')
    await page.evaluate('id => editMessage(id)', user['id'])
    await page.locator('.edit-message-input').fill('Unsaved message edit')
    await page.evaluate('''({assistant}) => {
        window.editorBefore = document.querySelector('.edit-message-input');
        const row = document.querySelector(`.message[data-msg-id="${assistant.id}"]`);
        row.remove();
        State.messages = State.messages.filter(m => m.id !== assistant.id);
        State.isStreaming = true;
        State._followingGenId = 777;
        State._followingDraftMsgId = assistant.id;
        State._liveStreamDraft = {...assistant, content:'Partial answer', content_blocks:null};
        appendStreamingMessage(null, null, assistant.id);
        streamingDiv.querySelector('.message-content').textContent='Partial answer';
        State.ws.close(1000, 'Periodic relay reconnect');
    }''', {'assistant':assistant})
    # Partial text survives the close before the replacement socket gets a snapshot.
    assert await page.locator('.message.streaming .message-content').inner_text() == 'Partial answer'
    await page.wait_for_function('!State.isStreaming && !document.querySelector(".message.streaming")')
    assert 'Saved paragraph 59' in await page.locator(f'.message[data-msg-id="{assistant["id"]}"]').inner_text()
    assert await page.locator('#user-input').input_value() == 'My next prompt stays here'
    assert await page.locator('.edit-message-input').input_value() == 'Unsaved message edit'
    assert await page.evaluate('editorBefore === document.querySelector(".edit-message-input")')


async def test_failed_prelaunch_clears_shell_while_other_branch_is_active(mobile_browser):
    page, conv, user, assistant, _ = mobile_browser
    await open_conversation(page, conv, user)
    await page.locator('#user-input').fill('Keep this next prompt')
    await page.evaluate('''({user}) => {
        State.isStreaming = true;
        State._streamIsOurBranch = true;
        State._followingGenId = 777;
        State._followingDraftMsgId = null;
        _pendingLaunch = {genId:777,runId:'gen:777',parentId:user.id,threadId:State.loomCursor.thread_id};
        appendStreamingMessage();
        handleWSMessage({type:'generation_active',snapshots:[{gen_id:778,run_id:'gen:778',parent_id:999999,loom_thread_id:'other-thread'}]});
    }''', {'user':user})
    await page.wait_for_function('!State.isStreaming && !document.querySelector(".message.streaming")')
    assert await page.locator('#user-input').input_value() == 'Keep this next prompt'


async def test_tool_only_failure_shows_persisted_terminal_diagnostic(mobile_browser):
    page, conv, user, assistant, _ = mobile_browser
    await db.update_message_content(assistant['id'],
        content='---\n[Goose turn failed after 7 tool calls without a final response]',
        content_blocks='[{"type":"text","text":""},{"type":"tool_use","name":"Read","input":"fixture.txt","result":"Saved output"}]',
        turn_lifecycle_state='partial_failed')
    await open_conversation(page, conv, assistant)
    row = page.locator(f'.message[data-msg-id="{assistant["id"]}"]')
    assert 'Goose turn failed after 7 tool calls' in await row.locator('.turn-terminal-notice').inner_text()
    assert await row.locator('.generating-placeholder').count() == 0


@pytest.mark.parametrize('width,height', [(320,740),(390,844),(412,915),(844,390)])
async def test_phone_headers_large_counters_drawers_and_latest_do_not_overlap(mobile_browser, width, height):
    page, conv, user, assistant, _ = mobile_browser
    await page.set_viewport_size({'width':width,'height':height})
    await open_conversation(page, conv, assistant)
    await page.evaluate('switchView("chat")')
    await page.evaluate('''({user,assistant}) => {
        State.treeData.push(...Array.from({length:10000}, (_,i) => ({id:i+100000,parent_id:user.id,role:'assistant',created_at:0})));
        const rendered = {...assistant, model_attestation:JSON.stringify({status:'verified',harness:'gemini',effective_model:'gemini-3.8-flash-medium',session_continuity:'rebuilt'})};
        document.querySelector(`.message[data-msg-id="${assistant.id}"]`).replaceWith(createMessageElement(rendered));
        const scroller = document.getElementById('messages-container');
        scroller.style.scrollBehavior='auto'; scroller.scrollTop=0;
        document.getElementById('jump-to-latest').classList.remove('hidden');
    }''', {'user':user,'assistant':assistant})
    metrics = await page.evaluate('''id => {
        const row=document.querySelector(`.message[data-msg-id="${id}"]`);
        const rect=n=>n.getBoundingClientRect();
        const left=rect(row.querySelector('.message-header-left'));
        const actions=rect(row.querySelector('.message-actions'));
        const content=rect(row.querySelector('.message-content'));
        const buttons=[...row.querySelectorAll('.message-header button')].map(rect);
        const latest=rect(document.getElementById('jump-to-latest'));
        const transcript=rect(document.getElementById('messages-container'));
        return {separate:left.bottom<=actions.top+1 || left.right<=actions.left+1 || actions.right<=left.left+1,contentBelow:actions.bottom<=content.top+1,
            contained:buttons.every(b=>b.left>=0 && b.right<=innerWidth),
            touch:buttons.every(b=>b.width>=44 && b.height>=44),
                latestInFront:latest.bottom<=transcript.bottom+20 && latest.top<transcript.bottom,transcriptHeight:transcript.height,
            toolbarHeight:document.getElementById('chat-workspace-toolbar').offsetHeight,
            overflow:document.documentElement.scrollWidth>innerWidth,
            rootCounter:!!document.querySelector('.message.user .branch-indicator')};
    }''', assistant['id'])
    assert metrics['separate'] and metrics['contentBelow'], metrics
    assert metrics['contained'] and metrics['touch'], metrics
    if width > height:
        assert metrics['transcriptHeight'] >= 120, metrics
    assert metrics['latestInFront'] and metrics['toolbarHeight'] <= 64, metrics
    assert not metrics['overflow'] and not metrics['rootCounter'], metrics
    # Expanded evidence belongs in document flow, never over the action buttons.
    badge = page.locator(f'.message[data-msg-id="{assistant["id"]}"] .model-attestation')
    await badge.locator('summary').tap()
    assert await badge.get_attribute('open') is not None
    assert await badge.locator('.model-attestation-panel').evaluate('(n)=>getComputedStyle(n).position') == 'static'
    artifacts = Path('artifacts/mobile-recovery')
    artifacts.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(artifacts/f'phone-{width}x{height}.png'))
    await page.locator('#workspace-toggle-left').tap()
    assert await page.locator('#workspace-left').is_visible()
    rect = await page.locator('#workspace-left').bounding_box()
    assert rect['x'] >= 0 and rect['x']+rect['width'] <= width+1


async def test_live_insert_removes_empty_hint(mobile_browser):
    page, conv, user, assistant, _ = mobile_browser
    await open_conversation(page, conv, assistant)
    await page.evaluate('''() => {
        document.getElementById('messages').innerHTML='<div class="empty-loom-hint">No messages</div>';
        appendStreamingMessage();
    }''')
    assert await page.locator('.empty-loom-hint').count() == 0


async def test_completion_notice_keeps_selected_sibling_and_draft(mobile_browser):
    page, conv, user, assistant, _ = mobile_browser
    sibling = await db.add_message(conv['id'], 'assistant', 'Other sibling response', parent_id=user['id'])
    await open_conversation(page, conv, assistant)
    await page.evaluate('id => switchToBranch(id,id,{exact:true})', assistant['id'])
    await page.locator('#user-input').fill('Keep this branch draft')
    await page.evaluate('data => handleWSMessage(data)', {'type':'branch_landed','conv_id':conv['id'],'message_id':sibling['id']})
    await page.wait_for_function('!State.isStreaming')
    await page.evaluate('id => loadMessages(id,{preserveView:true})', conv['id'])
    assert await page.locator(f'.message[data-msg-id="{sibling["id"]}"]').count() == 0
    assert await page.locator('#user-input').input_value() == 'Keep this branch draft'
