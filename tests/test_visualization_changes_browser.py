"""Navigation, grouped workspace lists and themed information in Chromium."""
from pathlib import Path

from tests.test_agent_activity_browser import activity_browser, open_conversation


async def test_visualization_changes(activity_browser):
    page, conv, user, assistant, _ = activity_browser
    dialogs = []
    page.on('dialog', lambda dialog: dialogs.append(dialog.message))
    await open_conversation(page, conv, assistant)
    await page.evaluate('''() => {
        State.conversations = [
            {id:901,title:'Star fixture',starred:true,folder:'Research'},
            {id:902,title:'Private fixture',nsfw:true,folder:'Research'},
            {id:903,title:'Work fixture',folder:'Writing'},
            {id:904,title:'Loose fixture'}];
        State.convFolderCollapsed = {};
        setWorkspaceLeftMode(true); renderChatWorkspace();
    }''')
    assert await page.locator('.workspace-conversation-group summary').all_text_contents() == [
        'Pins (1)', 'Not for Work (1)', 'Research (2)', 'Writing (1)', 'Unfiled (1)']
    assert await page.locator('.workspace-conversation-group').evaluate_all('(nodes)=>nodes.map(n=>n.open)') == [True,False,False,False,False]
    await page.locator('.workspace-conversation-group summary').filter(has_text='Writing').click()
    await page.evaluate('renderChatWorkspace()')
    assert await page.locator('.workspace-conversation-group').filter(has_text='Writing').evaluate('(n)=>n.open')
    await page.locator('#workspace-conversation-filter').fill('Work fixture')
    await page.wait_for_function("document.querySelector('#workspace-conversations').textContent.includes('Work fixture')")
    assert await page.locator('.workspace-conversation-group').count() == 1

    await page.evaluate("document.getElementById('describe-context-bar').classList.remove('hidden')")
    assert not await page.locator('#describe-context').is_visible()
    await page.locator('#describe-context-bar summary').click()
    await page.locator('#describe-context').fill('Focus on composition')
    await page.locator('#describe-context-bar summary').click()
    assert await page.locator('#describe-context').input_value() == 'Focus on composition'

    await page.evaluate("showToast('Turn landed fixture', 'warning')")
    assert await page.locator('.toast').count() == 0
    await page.locator('#notif-bell').click()
    assert await page.locator('#notif-list').get_by_text('Turn landed fixture', exact=False).is_visible()
    await page.locator('#notif-bell').click()
    await page.evaluate("showCopyLinkDialog('https://example.test/canvas')")
    assert await page.get_by_role('dialog', name='Copy Canvas link').is_visible()
    await page.keyboard.press('Escape')
    assert await page.get_by_role('dialog', name='Copy Canvas link').count() == 0

    await page.locator('#btn-folders').click()
    await page.wait_for_function("State.currentView==='home'")
    await page.locator('#btn-home').click()
    await page.wait_for_function("State.currentView==='chat' && State.currentConv?.title==='ExoHome'")
    assert not dialogs
    output = Path('artifacts/visualization-changes')
    output.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(output / 'desktop.png'))
    for width in [390, 320]:
        await page.set_viewport_size({'width': width, 'height': 844})
        await page.evaluate('syncWorkspaceLayout()')
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        for selector in ['#btn-home', '#btn-folders']:
            assert await page.locator(selector).is_visible()
        await page.screenshot(path=str(output / f'mobile-{width}.png'))

    # Admin assets in the same isolated browser; no live admin lifecycle.
    admin = await page.context.new_page()
    await admin.set_content('<header id="topbar"></header>')
    await admin.add_script_tag(content=Path('static/admin/admin.js').read_text(encoding='utf-8'))
    await admin.evaluate("showToast('Admin notice fixture')")
    await admin.locator('#admin-header-info summary').click()
    assert await admin.get_by_text('Admin notice fixture', exact=False).is_visible()
    await admin.close()
