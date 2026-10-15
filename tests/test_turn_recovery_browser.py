"""Missed terminal delivery recovers in-place, even with a live sibling."""
from types import SimpleNamespace

import pytest

import database as db
import server
from tests.test_queue_presentation_browser import queue_browser, playwright


@pytest.mark.parametrize("lifecycle", ["completed", "interrupted"])
async def test_heartbeat_recovers_landed_turn_without_touching_queue_editor(queue_browser, monkeypatch, lifecycle):
    page, conv, assistant, queued = queue_browser
    await db.update_message_content(assistant["id"], generation_ms=3500, time_to_first_token_ms=250,
                                    turn_input_tokens=1234, turn_output_tokens=56, turn_lifecycle_state=lifecycle)
    sibling_key = (conv["id"], 8888, 92)
    server._active_generations[sibling_key] = SimpleNamespace(done=lambda: False)
    server._generation_snapshots[sibling_key] = {"draft_msg_id": 9999, "parent_id": 8888}
    await page.locator('.queue-edit-btn').click()
    await page.locator('.edit-message-input').fill('Keep this unsaved queue revision')
    await page.evaluate("""({id}) => {
        window.savedCursor = JSON.stringify(State.loomCursor);
        window.savedEditor = document.querySelector('.edit-message-input');
        savedEditor.setSelectionRange(3, 8);
        State.isStreaming = true;
        State._streamIsOurBranch = true;
        State._followingGenId = 91;
        State._followingDraftMsgId = id;
        _subscribedRunIds.add('gen:91');
        streamingDiv = document.querySelector(`.message[data-msg-id="${id}"]`);
        streamingDiv.classList.add('streaming');
        streamingDiv.insertAdjacentHTML('beforeend', '<div class="stream-thinking-footer">Looming...</div>');
        setBrowserTabState('looming');
        const scroller = document.getElementById('messages-container');
        scroller.style.scrollBehavior = 'auto';
        scroller.scrollTop = 100;
        window.savedScroll = scroller.scrollTop;
        // Even a content-bearing foreign snapshot cannot steal this stream.
        handleWSMessage({type:'generation_active', snapshots:[{
            gen_id:92, run_id:'gen:92', draft_msg_id:9999, parent_id:8888,
            full_text:'NEVER SHOW SIBLING OUTPUT', content_blocks:[]
        }]});
        State.ws._pingWorker.onmessage(); // same callback as the periodic heartbeat
    }""", {"id": assistant["id"]})
    await page.wait_for_function("!State.isStreaming && State._followingGenId == null")
    assert await page.locator('.stream-thinking-footer').count() == 0
    # Telemetry is retained inside a collapsed details panel. Inspect its data
    # without opening the panel and disturbing the queue editor/scroll oracle.
    assert await page.locator('.first-token-time').text_content() == 'First response 0.3s'
    footer = await page.locator(f'.message[data-msg-id="{assistant["id"]}"] .message-footer').text_content()
    assert '1.2k in / 56 out' in footer and 'Generation time 3.5s' in footer
    assert await page.evaluate("""() => savedEditor.isConnected && savedEditor.value === 'Keep this unsaved queue revision'
        && savedEditor.selectionStart === 3 && savedEditor.selectionEnd === 8
        && JSON.stringify(State.loomCursor) === savedCursor
        && document.getElementById('messages-container').scrollTop === savedScroll""")
    assert 'NEVER SHOW' not in await page.locator('#messages').inner_text()
    assert not server._active_generations[sibling_key].done()
    assert len(await db.list_queued_generations(conv['id'])) == 1
    # A stale snapshot of the now-terminal run must not resurrect its spinner.
    await page.evaluate("""({id}) => handleWSMessage({type:'generation_active',snapshots:[{
        gen_id:91,run_id:'gen:91',draft_msg_id:id,full_text:'stale',content_blocks:[]
    }]})""", {"id": assistant["id"]})
    assert not await page.evaluate('State.isStreaming')


async def test_terminal_reply_cannot_finish_newer_stream_or_another_conversation(queue_browser):
    page, conv, assistant, queued = queue_browser
    observed = await page.evaluate("""({id, conv}) => {
        State.isStreaming = true; State._followingGenId = 92; State._followingDraftMsgId = id;
        const stillFollowing = () => State.isStreaming && State._followingGenId === 92;
        const results = [];
        handleWSMessage({type:'generation_terminal',conv_id:conv,gen_id:91,run_id:'gen:91',message:{id}});
        results.push(stillFollowing());
        handleWSMessage({type:'generation_terminal',conv_id:conv+1,gen_id:92,run_id:'gen:92',message:{id}});
        results.push(stillFollowing());
        handleWSMessage({type:'generation_terminal',conv_id:conv,gen_id:92,run_id:'gen:92',message:{id:id+1}});
        results.push(stillFollowing());
        // Positive control: this exact terminal must finish the followed turn.
        handleWSMessage({type:'generation_terminal',conv_id:conv,gen_id:92,run_id:'gen:92',
            message:{id,role:'assistant',content:'Fixture completed',generation_ms:10,turn_lifecycle_state:'completed'}});
        results.push(!State.isStreaming && State._followingGenId == null);
        return results;
    }""", {"id": assistant["id"], "conv": conv["id"]})
    # Check each synchronous handler in the same browser task. This fixture's
    # server is actually idle; a legitimate idle frame between separate Playwright
    # calls is not evidence that one of the foreign terminal handlers accepted it.
    assert observed == [True, True, True, True]


async def test_replaced_socket_deliveries_are_ignored(queue_browser):
    page, conv, assistant, queued = queue_browser
    assert await page.evaluate("""async () => {
        const old = State.ws;
        let delivered = 0;
        const handler = handleWSMessage;
        window.handleWSMessage = () => delivered++;
        old._replaced = true;
        old.onmessage({data:JSON.stringify({type:'generation_idle'})});
        old._replaced = false;
        const current = State.ws;
        State.ws = {};
        old.onmessage({data:JSON.stringify({type:'generation_idle'})});
        State.ws = current;
        window.handleWSMessage = handler;
        return delivered === 0;
    }""")
