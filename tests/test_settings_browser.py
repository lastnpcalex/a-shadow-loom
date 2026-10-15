import re
import time

from playwright.async_api import expect

import database as db
from tests.test_agent_activity_browser import activity_browser, open_conversation
from tests.test_admin_restart_browser import dashboard


async def test_chat_settings_usage_and_save_do_not_write_machine_config(activity_browser, tmp_path):
    page, conv, user, assistant, runs = activity_browser
    await db.update_conversation_fields(conv['id'], permission_profile='trusted')
    writes = []
    page.on('request', lambda request: writes.append(request.url) if request.method == 'PUT' else None)
    await page.route('**/api/provider-usage', lambda route: route.fulfill(json={
        'providers': [{'label': 'Codex fixture', 'status': 'ok', 'observed_at': time.time(),
                       'windows': [{'label': '5 hour', 'used_percent': 20, 'resets_at': time.time() + 3600}, {'label': 'Weekly', 'used_percent': 85, 'resets_at': time.time() + 3600}]},
                      {'label': 'Claude', 'status': 'ok', 'observed_at': time.time(), 'windows': [{'label': '5 hour', 'used_percent': 30}, {'label': 'Weekly', 'used_percent': 40}]},
                      {'label': 'Antigravity', 'provider': 'antigravity', 'status': 'ok', 'observed_at': time.time(), 'windows': [{'label': 'Gemini Pro', 'used_percent': 25}, {'label': 'GPT-OSS', 'used_percent': 35}]}]}))
    await page.route('**/api/openrouter/usage', lambda route: route.fulfill(json={
        'api_key_configured': True, 'current_key': {'usage_daily': 0, 'usage_weekly': 4, 'usage_monthly': 9, 'limit_remaining': 12},
        'limits': {'weekly_limit_usd': 15}, 'secrets': {'preview': 'PRIVATE'}}))
    await open_conversation(page, conv, assistant)
    await page.locator('#btn-settings').click()
    await page.wait_for_function("document.getElementById('modal-settings').dataset.loading === '0'")
    await expect(page.locator('#cfg-cc-model')).to_be_visible()
    assert await page.locator('#cfg-host, #cfg-mc-ngl').count() == 0
    await page.locator('[data-settings-tab="permissions"]').click()
    await expect(page.locator('#cfg-permission-profile')).to_have_value('trusted')
    await page.locator('#cfg-permission-profile').select_option('strict')
    await page.locator('#btn-save-settings').click()
    await expect(page.locator('#modal-settings')).to_be_hidden()
    await page.locator('#btn-usage').click()
    await expect(page.locator('#btn-usage')).to_have_attribute('aria-expanded', 'true')
    assert await page.locator('#modal-usage [aria-modal="true"], #modal-usage .modal-backdrop').count() == 0
    await expect(page.locator('#usage-close')).to_be_visible()
    sizes = await page.locator('#usage-refresh, #usage-close').evaluate_all("els => els.map(el => {const b=el.getBoundingClientRect(); const s=el.querySelector('svg').getBoundingClientRect(); return [b.width,b.height,s.width,s.height]})")
    assert sizes[0] == sizes[1]
    assert await page.locator('#usage-close').evaluate("el => el.getBoundingClientRect().x > document.getElementById('usage-refresh').getBoundingClientRect().x")
    assert await page.locator('#btn-usage svg').evaluate('el => el.getBoundingClientRect().height') <= 20
    await expect(page.locator('#provider-usage-cards')).to_contain_text('85% used')
    await expect(page.locator('#settings-openrouter-periods')).to_contain_text('$4.00 used')
    assert 'PRIVATE' not in await page.locator('#modal-usage').inner_text()
    assert await page.locator('#modal-usage input').count() == 0
    await expect(page.locator('#settings-openrouter-periods')).to_contain_text('$11.00 remaining')
    assert await page.locator('.usage-content').evaluate('el => el.scrollHeight <= el.clientHeight'), await page.locator('.usage-content').evaluate('el => ({height:el.clientHeight,scroll:el.scrollHeight, children:[...el.querySelectorAll("section, h3, .hint")].map(x=>({id:x.id,h:x.clientHeight}))})')
    await expect(page.locator('#provider-usage-cards .quota-provider')).to_have_count(4)
    await page.screenshot(path=str(tmp_path / 'usage-desktop.png'))
    await expect(page.locator('#provider-usage-warning')).to_be_visible()
    await page.keyboard.press('Escape')
    await expect(page.locator('#modal-usage')).to_be_hidden()
    await expect(page.locator('#btn-usage')).to_be_focused()
    await page.locator('#provider-usage-warning button').click()
    await expect(page.locator('#provider-usage-warning')).to_be_hidden()
    assert (await db.get_conversation(conv['id']))['permission_profile'] == 'strict'
    assert not any('/api/config' in url or '/api/models-config' in url for url in writes)
    await page.set_viewport_size({'width': 390, 'height': 844})
    await page.locator('#btn-usage').click()
    await expect(page.locator('#modal-usage')).to_be_visible()
    for width, height in [(320, 568), (390, 844), (844, 390)]:
        await page.set_viewport_size({'width': width, 'height': height})
        await page.wait_for_timeout(100)
        assert await page.locator('#modal-usage').evaluate("el => {const r=el.getBoundingClientRect(); return r.left >= 7 && r.right <= innerWidth-7 && r.bottom <= innerHeight-7 && r.width <= 420}")
        assert await page.locator('#modal-usage').evaluate('el => el.scrollWidth <= el.clientWidth')
    await page.set_viewport_size({'width': 390, 'height': 844})
    assert await page.locator('.usage-content').evaluate('el => el.scrollWidth <= el.clientWidth')
    await page.locator('#modal-usage').evaluate('el => el.scrollTop = el.scrollHeight')
    await expect(page.locator('#settings-openrouter-periods')).to_be_in_viewport()
    await page.screenshot(path=str(tmp_path / 'settings-phone.png'))
    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    await page.locator('#btn-usage').click()
    await expect(page.locator('#modal-usage')).to_be_hidden()
    await page.locator('#btn-usage').click()
    await page.locator('#usage-title').click()
    await expect(page.locator('#modal-usage')).to_be_visible()
    await page.mouse.click(2, 842)
    await expect(page.locator('#modal-usage')).to_be_hidden()
    await page.locator('#btn-usage').click()
    await page.locator('#usage-close').click()
    await expect(page.locator('#modal-usage')).to_be_hidden()
    await expect(page.locator('#btn-usage')).to_be_focused()


async def test_popup_close_buttons_and_notification_footer(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    await open_conversation(page, conv, assistant)
    await page.locator('#notif-bell').click()
    await expect(page.locator('#notif-dropdown')).to_be_visible()
    sizes = await page.locator('#notif-refresh, #notif-close').evaluate_all("els => els.map(el => {const b=el.getBoundingClientRect(); const s=el.querySelector('svg').getBoundingClientRect(); return [b.width,b.height,s.width,s.height]})")
    assert sizes[0] == sizes[1]
    assert await page.locator('#notif-close').evaluate("el => el.getBoundingClientRect().x > document.getElementById('notif-refresh').getBoundingClientRect().x")
    assert await page.locator('#notif-clear-notifications').evaluate("el => el.parentElement.classList.contains('notif-footer')")
    assert await page.locator('.notif-header #notif-clear-notifications').count() == 0
    await page.locator('#notif-close').click()
    await expect(page.locator('#notif-dropdown')).to_be_hidden()
    await expect(page.locator('#notif-bell')).to_be_focused()
    await page.locator('#btn-bookmarks-global').click()
    await expect(page.locator('#bookmarks-panel-global')).to_be_visible()
    await page.locator('#bookmarks-close').click()
    await expect(page.locator('#bookmarks-panel-global')).to_be_hidden()
    await expect(page.locator('#btn-bookmarks-global')).to_be_focused()


async def test_usage_budget_bars_and_warning_thresholds(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    await open_conversation(page, conv, assistant)
    for used, severity, color in [(20, '', 'rgb(0, 255, 255)'), (80, 'nearing', 'rgb(239, 187, 101)'),
                                  (90, 'critical', 'rgb(246, 121, 121)'), (100, 'critical', 'rgb(246, 121, 121)'),
                                  (120, 'critical', 'rgb(246, 121, 121)')]:
        await page.route('**/api/openrouter/usage', lambda route: route.fulfill(json={
            'api_key_configured': True, 'current_key': {'usage_weekly': used},
            'limits': {'weekly_limit_usd': 100}}))
        await page.evaluate('loadSettingsOpenRouterUsage(true)')
        bars = page.locator('#settings-openrouter-periods progress')
        await expect(bars).to_have_count(1)
        assert await bars.evaluate('el => el.value') == min(100, used)
        assert await bars.evaluate('el => getComputedStyle(el).accentColor') == color
        if severity:
            assert await bars.evaluate('(el, name) => el.parentElement.classList.contains(name)', severity)
        await page.unroute('**/api/openrouter/usage')


async def test_settings_hides_previous_values_until_loaded(activity_browser):
    import asyncio
    page, conv, user, assistant, runs = activity_browser
    await open_conversation(page, conv, assistant)
    await page.locator('#btn-settings').click()
    await page.wait_for_function("document.getElementById('modal-settings').dataset.loading === '0'")
    await page.locator('#modal-settings .modal-backdrop').click(position={'x': 5, 'y': 5})
    release = asyncio.Event()
    async def delayed_config(route):
        response = await route.fetch()
        await release.wait()
        await route.fulfill(response=response)
    await page.route('**/api/config', delayed_config)
    await page.locator('#btn-settings').click()
    try:
        await expect(page.locator('#settings-loading-status')).to_be_visible()
        await expect(page.locator('#settings-model')).to_be_hidden()
        await expect(page.locator('#settings-tabs')).to_be_hidden()
        assert await page.locator('#modal-settings > .modal-content > h2').evaluate("el => getComputedStyle(el).fontSize") == '26px'
    finally:
        release.set()
    await page.wait_for_function("document.getElementById('modal-settings').dataset.loading === '0'")
    await expect(page.locator('#settings-model')).to_be_visible()
    await expect(page.locator('#settings-tabs')).to_be_visible()


async def test_pending_notice_reaches_other_chat_and_survives_reload(activity_browser, monkeypatch):
    import server
    page, conv, user, assistant, runs = activity_browser
    monkeypatch.setattr(server, '_pending_hook_permissions', {})
    await db.save_pending_permission('global-fixture', conv['id'], 'Write', {'content': 'PRIVATE'}, 'PRIVATE',
                                    draft_msg_id=assistant['id'], run_id='gen:999', client_instance_id='other-page')
    before = await page.evaluate('JSON.stringify(State.loomCursor)')
    await server._ws_broadcast_all({'type': 'permission_notice', 'request_id': 'global-fixture',
                                  'conv_id': conv['id'], 'message_id': assistant['id'], 'status': 'pending', 'tool_input': 'PRIVATE'})
    await expect(page.locator('#notif-bell')).to_have_class(re.compile('notif-urgent'))
    await page.locator('#notif-bell').click()
    await expect(page.locator('#notif-list')).to_contain_text('Review in Admin')
    assert 'PRIVATE' not in await page.locator('#notif-list').inner_text()
    assert await page.locator('#notif-list .allow').count() == 0
    assert before == await page.evaluate('JSON.stringify(State.loomCursor)')
    await page.reload()
    await page.wait_for_function("!document.body.classList.contains('app-booting')")
    await expect(page.locator('#notif-bell')).to_have_class(re.compile('notif-urgent'))
    await server._ws_broadcast_all({'type': 'permission_notice', 'request_id': 'global-fixture', 'conv_id': conv['id'], 'status': 'resolved'})
    await expect(page.locator('#notif-bell')).not_to_have_class(re.compile('notif-urgent'))
    await server._ws_broadcast_all({'type': 'permission_notice', 'request_id': 'global-fixture', 'conv_id': conv['id'], 'status': 'pending'})
    await expect(page.locator('#notif-bell')).not_to_have_class(re.compile('notif-urgent'))


async def test_admin_settings_save_is_explicit_and_does_not_restart(tmp_path):
    from machine_settings import FIELDS, TUNING_FIELDS, schema
    async with dashboard({'pid': 101}) as (page, posts, navigations):
        saves = []
        saved_settings = {'temperature': .8, 'llama_model': 'one.gguf', 'default_permission_profile': 'trusted'}
        async def settings(route):
            if route.request.method == 'POST':
                saves.append(route.request.post_data_json)
                saved_settings.update(route.request.post_data_json.get('settings', {}))
                return await route.fulfill(json={'ok': True})
            await route.fulfill(json={'fields': schema(FIELDS), 'tuning_fields': schema(TUNING_FIELDS),
                'settings': saved_settings, 'disk_models': ['one.gguf'],
                'models': {'one.gguf': {'ngl': 999, 'ctx_size': 8192}}})
        await page.route('**/api/machine-settings', settings)
        await page.locator('[data-view="settings"]').click()
        await expect(page.locator('#machine-settings-fields input[name="temperature"]')).to_have_value('0.8')
        header = await page.locator('#topbar').bounding_box()
        panel = await page.locator('#view-settings').bounding_box()
        assert header['y'] + header['height'] <= panel['y']
        await page.locator('#machine-settings-fields input[name="temperature"]').fill('0.6')
        await page.locator('#machine-settings-fields select[name="default_permission_profile"]').select_option('strict')
        # Fill required shared numeric defaults for this sparse fixture.
        for key, value in {'top_p': .9, 'max_tokens': 2048, 'repeat_penalty': 1.1, 'max_context_tokens': 8192, 'verbatim_window': 8}.items():
            await page.locator(f'#machine-settings-fields input[name="{key}"]').fill(str(value))
        await page.get_by_role('button', name='Save configuration', exact=True).click()
        await expect(page.locator('#machine-settings-status')).to_contain_text('Saved')
        assert saves[0]['settings']['temperature'] == .6
        assert saves[0]['settings']['default_permission_profile'] == 'strict'
        assert posts == [] and navigations == ['/']
        await page.set_viewport_size({'width': 390, 'height': 844})
        await page.screenshot(path=str(tmp_path / 'admin-settings-phone.png'))
        assert await page.locator('#view-settings').evaluate('el => el.scrollWidth <= el.clientWidth')
