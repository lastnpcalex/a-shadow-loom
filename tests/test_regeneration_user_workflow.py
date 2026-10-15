import asyncio
import json

import pytest
import database as db
import server
from tests.test_lifecycle_handoff_browser import lifecycle_browser
from tests.test_fullstack_queue_isolation import _ControlledProvider
from tests.lifecycle_metrics import observe
from tests.test_fullstack_queue_isolation import _free_port


@pytest.mark.asyncio
@pytest.mark.parametrize('lifecycle_browser', [{'mobile': True}], indirect=True)
async def test_send_and_followup_land_with_no_working_socket(lifecycle_browser, monkeypatch):
    page, conv, client, workspace = lifecycle_browser
    controller = _ControlledProvider()
    monkeypatch.setattr(server, '_handle_generation', controller.handle)
    send_event = server._ws_send
    async def finish_fixture(conv_id, data, *args, **kwargs):
        # The older provider double omits durable terminal metadata and removes
        # its snapshot before the shared cursor finalizer can read it. Match
        # the real provider completion contract so HTTP can recover that data.
        if data.get('type') == 'stream_end':
            await db.update_message_content(data['message']['id'], turn_lifecycle_state='completed', generation_ms=1000,
                content_blocks=json.dumps([{'type':'text','text':data['message']['content']}]))
            await server._advance_unambiguous_generation_cursors(getattr(asyncio.current_task(), '_gen_key'))
        return await send_event(conv_id, data, *args, **kwargs)
    monkeypatch.setattr(server, '_ws_send', finish_fixture)
    await page.evaluate('''port => {
        const Native=window.WebSocket;
        window.failedSocketCount=0;
        window.WebSocket=new Proxy(Native,{construct(target,args) {
            const url=new URL(args[0]);url.port=String(port);
            const socket=new target(url.toString(),...args.slice(1));
            socket.addEventListener('error',()=>failedSocketCount++);
            return socket;
        }});
        State.ws.close(1000,'Fixture tunnel unavailable');
    }''', _free_port())
    await page.wait_for_function('failedSocketCount > 0')
    await page.locator('#user-input').fill('Send through HTTP while the tunnel socket fails')
    await page.locator('#btn-send').click()
    await controller.wait_for_runs(1)
    first=controller.runs[0]
    await page.wait_for_function('State.isStreaming && document.querySelector(".message.streaming")')
    await page.evaluate('''id => {
        window.replyRowMaximum=0;
        new MutationObserver(() => {
            const rows=[...document.querySelectorAll(".message.assistant")].filter(row => row.classList.contains("streaming") || row.dataset.msgId===String(id));
            replyRowMaximum=Math.max(replyRowMaximum,rows.length);
        }).observe(document.getElementById("messages"),{childList:true,subtree:true});
    }''', first.draft_id)
    # Hold the provider open through an HTTP recovery tick: final-state-only
    # assertions miss the generating placeholder plus persisted ghost draft.
    # Disconnected recovery checks active-run metadata without repeatedly
    # downloading history while a turn is still running. Hold through one
    # recovery response, then verify the single placeholder and final landing.
    async with page.expect_response(lambda r: r.url.endswith('/api/generations')):
        pass
    assert await page.locator('.message.assistant').count() == 1
    first.release.set()
    await page.wait_for_function('!State.isStreaming && document.querySelector("#messages")?.textContent.includes("complete")', timeout=15000)
    assert await page.locator(f'.message[data-msg-id="{first.draft_id}"]').count() == 1
    assert await page.evaluate('replyRowMaximum') <= 1
    await page.locator('#user-input').fill('Continue the answer without refreshing')
    await page.locator('#btn-send').click()
    await controller.wait_for_runs(2)
    second=controller.runs[1]
    prompt=await db.get_message(second.parent_id)
    assert prompt['parent_id'] == first.draft_id
    second.release.set()
    await page.wait_for_function('!State.isStreaming && !document.querySelector(".message.streaming")', timeout=15000)
    assert await page.locator(f'.message[data-msg-id="{second.draft_id}"]').count() == 1
    assert len(controller.runs) == 2


@pytest.mark.asyncio
async def test_rerun_keeps_clicked_goose_model_when_cursor_repaints_picker(lifecycle_browser, monkeypatch):
    page, conv, client, workspace = lifecycle_browser
    prompt = await db.add_message(conv['id'], 'user', 'Saved prompt')
    answer = await db.add_message(conv['id'], 'assistant', 'Saved answer', parent_id=prompt['id'])
    await db.update_message_content(answer['id'], cc_model_used='codex-gpt-5.6-sol', cc_session_mode='codex')
    await page.goto(f"{str(client.base_url)}?conversation={conv['id']}&view=chat&branch={answer['id']}")
    await page.wait_for_function('State.ws?.readyState === WebSocket.OPEN && !State._restoringView')
    model = 'goose:openrouter:z-ai/glm-5.3-flash'
    async with page.expect_response(lambda r: r.request.method == 'PUT' and r.url.endswith(f"/api/conversations/{conv['id']}")):
        await page.locator('#cc-model-inline').select_option(model)
    controller = _ControlledProvider()
    calls = []
    async def goose(websocket, conv_id, selected_conv, data):
        calls.append(data['cc_model'])
        await controller.handle(websocket, conv_id, data)
    async def wrong_provider(*args, **kwargs):
        raise AssertionError('Rerun launched the wrong provider')
    monkeypatch.setattr(server, '_handle_goose_generation', goose)
    monkeypatch.setattr(server, '_handle_claude_generation', wrong_provider)
    await page.evaluate('''() => {
        const select = selectServerLoomCursor;
        selectServerLoomCursor = async (...args) => {
            const result = await select(...args);
            document.querySelector('#cc-model-inline').value = 'sonnet';
            return result;
        };
    }''')
    await page.locator(f'.message[data-msg-id="{answer["id"]}"] button[title="Regenerate"]').click()
    await controller.wait_for_runs(1)
    assert calls == [model]
    await page.wait_for_function('document.querySelector(".message.streaming")?.textContent.includes("provider partial")')
    controller.runs[0].release.set()
    await page.wait_for_function('!State.isStreaming && !State._terminalSettlementPromise')
    assert await page.locator(f'.message[data-msg-id="{controller.runs[0].draft_id}"]').is_visible()


@pytest.mark.asyncio
async def test_preprovider_failure_is_visible_and_survives_reload(lifecycle_browser, monkeypatch):
    page, conv, client, workspace = lifecycle_browser
    async def fail_preparation(*args):
        raise RuntimeError('Fixture preparation failure')
    monkeypatch.setattr(server, '_handle_generation_with_workspace_safety', fail_preparation)
    await page.locator('#user-input').fill('Submission must have a visible outcome')
    await page.locator('#btn-send').click()
    await page.wait_for_function('!State.isStreaming && document.querySelector("#messages")?.textContent.includes("Fixture preparation failure")')
    assert await page.locator('#btn-send').is_enabled()
    assert not server._active_generations
    saved = [m for m in await db.get_conversation_tree(conv['id']) if m['role'] == 'assistant']
    assert len(saved) == 1
    assert (await db.get_message(saved[0]['id']))['turn_lifecycle_state'] == 'failed'
    await page.reload(wait_until='domcontentloaded')
    await page.wait_for_function('document.querySelector("#messages")?.textContent.includes("Fixture preparation failure")')
    assert await page.locator('#btn-send').is_enabled()
    await page.locator(f'.message[data-msg-id="{saved[0]["id"]}"] button[title="Regenerate"]').click()
    await page.wait_for_function('old => !State.isStreaming && State.messages.some(m => m.role === "assistant" && m.id !== old && m.content.includes("Fixture preparation failure"))', arg=saved[0]['id'])
    replies = [m for m in await db.get_conversation_tree(conv['id']) if m['role'] == 'assistant']
    assert len(replies) == 2
    states = [(await db.get_message(m['id']))['turn_lifecycle_state'] for m in replies]
    assert states == ['failed', 'failed']
    assert await page.locator('#btn-send').is_enabled()


@pytest.mark.asyncio
@pytest.mark.parametrize('lifecycle_browser', [{'mobile': True}], indirect=True)
async def test_mobile_rerun_streams_reloads_returns_from_focus_and_lands(lifecycle_browser, monkeypatch, record_property):
    page, conv, client, workspace = lifecycle_browser
    await page.set_viewport_size({'width': 390, 'height': 844})
    controller = _ControlledProvider()
    monkeypatch.setattr(server, '_handle_generation', controller.handle)
    await page.locator('#user-input').fill('A real submitted prompt')
    await page.locator('#btn-send').click()
    await controller.wait_for_runs(1)
    first = controller.runs[0]
    await page.wait_for_function('State.isStreaming && document.querySelector(".message.streaming")')
    first.release.set()
    await page.wait_for_function('!State.isStreaming && !State._terminalSettlementPromise')
    await page.locator(f'.message[data-msg-id="{first.draft_id}"] button[title="Regenerate"]').click()
    await controller.wait_for_runs(2)
    second = controller.runs[1]
    assert second.parent_id == first.parent_id
    await page.wait_for_function('State.isStreaming && document.querySelector(".message.streaming")')
    await page.wait_for_function('document.querySelector(".message.streaming")?.textContent.includes("provider partial")')
    await page.locator('#user-input').fill('My unsent next message')
    await page.evaluate('''() => {
        window.beforeForegroundSocket = State.ws;
        State.ws._lastActivity = Date.now() - 60000;
        window.dispatchEvent(new Event('focus'));
    }''')
    await page.wait_for_function('State.ws !== beforeForegroundSocket && State.ws?.readyState === WebSocket.OPEN && !State._reconstructing')
    assert await page.locator('#user-input').input_value() == 'My unsent next message'
    assert await page.locator('.message.streaming').count() == 1
    await page.reload(wait_until='domcontentloaded')
    await page.wait_for_function('State.isStreaming && document.querySelector(".message.streaming") && !State._reconstructing')
    second.release.set()
    await page.wait_for_function('!State.isStreaming && !State._terminalSettlementPromise')
    assert await page.locator(f'.message[data-msg-id="{second.draft_id}"]').count() == 1
    assert await page.locator('.message.streaming').count() == 0
    assert await page.locator('#btn-send').is_enabled()
    assert len(controller.runs) == 2
    saved = await db.get_message(second.draft_id)
    assert 'complete' in saved['content']
    observe(record_property, 'mobile_rerun_foreground', launches=len(controller.runs))


@pytest.mark.asyncio
async def test_launch_connection_loss_recovers_without_replaying(lifecycle_browser, monkeypatch, record_property):
    page, conv, client, workspace = lifecycle_browser
    controller = _ControlledProvider()
    monkeypatch.setattr(server, '_handle_generation', controller.handle)
    await page.locator('#user-input').fill('Saved initial answer')
    await page.locator('#btn-send').click()
    await controller.wait_for_runs(1)
    first = controller.runs[0]
    first.release.set()
    await page.wait_for_function('!State.isStreaming && !State._terminalSettlementPromise')
    # Close the launch transport after the real server admits it, before its
    # browser message handler can observe acknowledgement. No command replay.
    await page.evaluate('''() => {
        const open = _openGenerationLaunchSocket;
        _openGenerationLaunchSocket = operation => {
            open(operation);
            const socket = operation.socket;
            const receive = socket.onmessage;
            socket.onmessage = event => {
                const data = JSON.parse(event.data);
                if (data.type === 'generation_preparing' || data.type === 'stream_start') {
                    socket.close();
                    return;
                }
                receive(event);
            };
        };
    }''')
    await page.locator(f'.message[data-msg-id="{first.draft_id}"] button[title="Regenerate"]').click()
    await controller.wait_for_runs(2)
    second = controller.runs[1]
    await page.wait_for_function('State.isStreaming && document.querySelector(".message.streaming") && !State._reconstructing')
    second.release.set()
    await page.wait_for_function('!State.isStreaming && !State._terminalSettlementPromise')
    assert await page.locator(f'.message[data-msg-id="{second.draft_id}"]').count() == 1
    assert len(controller.runs) == 2
    observe(record_property, 'launch_lost_ack_recovery', launches=len(controller.runs))


@pytest.mark.asyncio
async def test_unopened_launch_does_not_leave_checking_spinner(lifecycle_browser, monkeypatch):
    page, conv, client, workspace = lifecycle_browser
    user = await db.add_message(conv['id'], 'user', 'Existing prompt')
    answer = await db.add_message(conv['id'], 'assistant', 'Keep this saved reply', parent_id=user['id'])
    await page.goto(f"{str(client.base_url)}?conversation={conv['id']}&view=chat&branch={answer['id']}")
    await page.wait_for_function('State.ws?.readyState === WebSocket.OPEN && !State._restoringView')
    await page.evaluate('''() => {
        State.ws._lastActivity = Date.now() - 60000;
        const open = _openGenerationLaunchSocket;
        _openGenerationLaunchSocket = operation => {
            open(operation);
            operation.socket.close();
        };
    }''')
    await page.locator(f'.message[data-msg-id="{answer["id"]}"] button[title="Regenerate"]').click()
    await page.locator('#retry-bar').wait_for(state='visible', timeout=10000)
    assert 'before the command was sent' in await page.locator('#retry-bar').inner_text()
    assert await page.locator(f'.message[data-msg-id="{answer["id"]}"]').is_visible()
    assert await page.locator('.message.streaming').count() == 0
    assert await page.locator('#btn-send').is_enabled()
    assert await page.locator('#btn-retry').count() == 0
    assert not server._active_generations
    assert len(await db.get_conversation_tree(conv['id'])) == 2


@pytest.mark.asyncio
async def test_reload_submitted_prompt_restores_unique_saved_reply(lifecycle_browser):
    page, conv, client, workspace = lifecycle_browser
    user = await db.add_message(conv['id'], 'user', 'A submitted prompt')
    answer = await db.add_message(conv['id'], 'assistant', 'Saved reply must appear on reopening', parent_id=user['id'])
    await page.goto(f"{str(client.base_url)}?conversation={conv['id']}&view=chat&branch={user['id']}")
    await page.wait_for_function('id => State.messages?.some(m => m.id === id)', arg=answer['id'])
    assert await page.locator(f'.message[data-msg-id="{answer["id"]}"]').is_visible()
    assert await page.evaluate('State.loomCursor.leaf_message_id') == answer['id']
    assert await page.locator('#btn-send').is_enabled()
    sibling = await db.add_message(conv['id'], 'assistant', 'A second explicit choice', parent_id=user['id'])
    await page.goto(f"{str(client.base_url)}?conversation={conv['id']}&view=chat&branch={user['id']}")
    await page.wait_for_function('id => State.loomCursor?.leaf_message_id === id && !State._restoringView', arg=user['id'])
    assert await page.locator(f'.message[data-msg-id="{answer["id"]}"]').count() == 0
    assert await page.locator(f'.message[data-msg-id="{sibling["id"]}"]').count() == 0
