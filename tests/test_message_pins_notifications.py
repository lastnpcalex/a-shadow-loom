import database as db
from pathlib import Path


async def test_dismissals_survive_reopen_and_merge(tmp_database):
    await db.dismiss_agent_notifications(['one', 'two'])
    await db.close_db()
    assert set(await db.get_agent_notification_dismissals()) == {'one', 'two'}
    await db.dismiss_agent_notifications(['two', 'three'])
    assert set(await db.get_agent_notification_dismissals()) == {'one', 'two', 'three'}


async def test_notification_api_merges_devices(client):
    assert (await client.post('/api/agent-notification-dismissals', json={'keys': ['device-a']})).status_code == 200
    response = await client.post('/api/agent-notification-dismissals', json={'keys': ['device-b']})
    assert set(response.json()) == {'device-a', 'device-b'}
    assert set((await client.get('/api/agent-notification-dismissals')).json()) == {'device-a', 'device-b'}
    assert (await client.post('/api/agent-notification-dismissals', json={'keys': [123]})).status_code == 422


async def test_pinned_message_is_exact_and_not_branch_leaf(client):
    conv = await db.create_conversation(title='Pin source')
    first = await db.add_message(conv['id'], 'user', 'Original message')
    await db.add_message(conv['id'], 'assistant', 'Later response', parent_id=first['id'])
    pin = await db.add_bookmark(conv['id'], first['id'])
    response = await client.get(f"/api/bookmarks/{pin['id']}/message")
    assert response.status_code == 200
    assert response.json()['id'] == first['id']
    assert response.json()['content'] == 'Original message'
    await db.delete_bookmark(pin['id'])
    assert (await client.get(f"/api/bookmarks/{pin['id']}/message")).status_code == 404


def test_pin_panel_toggle_and_exact_actions():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as browser_api:
        browser = browser_api.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 412, 'height': 915})
        page.set_content('<html class="true-mobile"><body style="margin:0"><button id="workspace-toggle-pins">Leaves</button><div style="position:relative;height:800px"><div id="workspace-right"></div></div></body></html>')
        page.add_style_tag(path=str(Path('static/message-pins.css').resolve()))
        page.evaluate('''() => {
            window.calls = [];
            window.State = {currentView:'chat', currentConvId:2, bookmarks:[], agentRuns:[]};
            window.API = {get: async url => url === '/api/bookmarks' ? [{id:7,message_id:42,conversation_id:1,conversation_title:'Source'}] : {id:42,role:'user',content:'Saved content'}};
            window.createMessageElement = msg => {const card=document.createElement('div');card.className='message user';card.dataset.msgId=msg.id;card.innerHTML='<div class="message-content">Saved content</div><div class="message-actions"></div>';return card;};
            window.loadConversation = async id => {calls.push(['load',id]);State.currentConvId=id;};
            window.switchView = () => {};
            window.switchToBranch = async (...args) => calls.push(['land',...args]);
            window.forkFromMessage = async id => calls.push(['branch',id]);
            window.showToast = message => {throw Error(message);};
        }''')
        page.add_script_tag(path=str(Path('static/message-pins.js').resolve()))
        page.evaluate("document.dispatchEvent(new Event('DOMContentLoaded'))")
        toggle = page.locator('#workspace-toggle-pins')
        toggle.click()
        page.get_by_role('button', name='Preview saved message', exact=True).click()
        page.get_by_text('Saved content', exact=True).wait_for()
        assert toggle.get_attribute('aria-expanded') == 'true'
        bounds = page.locator('#workspace-message-pins').bounding_box()
        assert bounds['width'] == 320
        assert bounds['x'] >= 0
        assert bounds['x'] + bounds['width'] <= 412
        assert page.locator('#workspace-message-pins h3').inner_text() == 'Leaves'
        assert page.locator('#workspace-message-pins [data-msg-id]').count() == 0
        toggle.click()
        assert page.locator('#workspace-message-pins').is_hidden()
        toggle.click()
        page.get_by_role('button', name='Branch', exact=True).click()
        page.wait_for_function('calls.length === 3')
        assert page.evaluate('calls') == [['load', 1], ['land', 42, 42, {'exact': True}], ['branch', 42]]
        browser.close()
