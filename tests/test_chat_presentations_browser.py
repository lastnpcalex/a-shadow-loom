"""Actual chat rendering and downloads through a disposable local server."""
import json
from pathlib import Path
import uuid
from urllib.parse import urlsplit

from PIL import Image
from playwright.async_api import expect

import database as db
from tests.test_agent_activity_browser import activity_browser, open_conversation


async def test_explicit_presentations_stream_reload_and_live_download(activity_browser, monkeypatch, tmp_path):
    import server
    page, conv, user, assistant, runs = activity_browser
    monkeypatch.setenv('EXO_STATE_DIR', str(tmp_path / 'presentation-state'))
    workspace = Path(conv['project_dir'])
    Image.new('RGB', (20, 10), 'red').save(workspace / 'mentioned.png')
    dataset = workspace / 'dataset.txt'
    dataset.write_text('original', encoding='utf-8')
    await db.update_message_content(assistant['id'], content='Only mentioning `mentioned.png`', content_blocks='[]')
    await open_conversation(page, conv, assistant)
    reply = page.locator(f".message[data-msg-id='{assistant['id']}']")
    await expect(reply).to_contain_text('Only mentioning')
    await expect(reply.locator('.generated-image')).to_have_count(0)
    class Live:
        def done(self):
            return False
    key = (conv['id'], user['id'], 41)
    monkeypatch.setattr(server, '_active_generations', {key: Live()})
    monkeypatch.setattr(server, '_generation_snapshots', {key: {'draft_msg_id': assistant['id'], 'project_dir': str(workspace)}})
    for ws in server._active_websockets[conv['id']]:
        server._event_router.subscribe(ws, 'gen:41')
    await page.evaluate("State._followingGenId = 41; _subscribedRunIds.add('gen:41')")
    origin = urlsplit(page.url)
    from search_space.scopes import issue_scope
    scope_token = issue_scope(conv['id'], str(workspace), run_id='gen:41')
    async def select(**payload):
        response = await page.request.post(f"{origin.scheme}://{origin.netloc}/api/conversations/{conv['id']}/chat-items", data={
            'request_id': str(uuid.uuid4()), 'run_id': 'gen:41', 'scope_token': scope_token, **payload,
        })
        assert response.status == 200, await response.text()
        return await response.json()
    image = await select(mode='attach', path='mentioned.png', caption='Selected image')
    await expect(reply.locator('.chat-item img')).to_have_count(1)
    await page.wait_for_function("document.querySelector('.chat-item img')?.naturalWidth === 20")
    await select(mode='attach', content='# Selected Markdown\n<script>window.presentationScriptRan=true</script>', kind='markdown', label='Report')
    await expect(reply.locator('.chat-item-markdown h1')).to_have_text('Selected Markdown')
    code = await select(mode='attach', content='</code><script>window.presentationScriptRan=true</script>', language='html', label='Snippet')
    await expect(reply.locator(f"[data-chat-item-id='{code['id']}'] code")).to_contain_text('<script>')
    assert not await page.evaluate('!!window.presentationScriptRan')
    linked = await select(mode='link', path='dataset.txt')
    await expect(reply.locator('.chat-item')).to_have_count(4)
    # An event for a sibling run cannot append content to this message.
    await page.evaluate("data => handleWSMessage(data)", {
        'type': 'chat_item', 'run_id': 'gen:99', 'gen_id': 99,
        'draft_msg_id': assistant['id'], 'item': {**image, 'id': str(uuid.uuid4())},
    })
    await expect(reply.locator('.chat-item')).to_have_count(4)
    dataset.write_text('updated when clicked', encoding='utf-8')
    async with page.expect_download() as download_info:
        await reply.locator(f"[data-chat-item-id='{linked['id']}'] a").click()
    download = await download_info.value
    assert Path(await download.path()).read_text(encoding='utf-8') == 'updated when clicked'
    await page.reload(wait_until='domcontentloaded')
    await page.wait_for_function("typeof loadConversation === 'function' && !document.body.classList.contains('app-booting')")
    await open_conversation(page, conv, assistant)
    await expect(reply.locator('.chat-item')).to_have_count(4)
    await expect(reply.locator('.generated-image')).to_have_count(1)
    await page.wait_for_function("document.querySelector('.chat-item img')?.naturalWidth === 20")
    # Old replies retain their existing inferred preview on reload.
    connection = await db.get_db()
    await connection.execute('UPDATE messages SET chat_items=NULL WHERE id=?', (assistant['id'],))
    await connection.commit()
    await page.evaluate('id => loadMessages(id)', conv['id'])
    await expect(reply.locator('.detected-images img')).to_have_count(1)
