"""Visible workspace controls and page-local warning dismissal."""
import time
from tests.test_agent_activity_browser import activity_browser, open_conversation


async def test_flanking_agents_and_toggle_indicators(activity_browser, tmp_path):
    page, conv, user, assistant, runs = activity_browser
    await page.set_viewport_size({'width': 1920, 'height': 1080})
    await open_conversation(page, conv, assistant)
    await page.evaluate("switchView('chat')")
    await page.locator('#btn-agent-manager').click()
    await page.wait_for_selector('#agent-manager-panel')
    assert await page.locator('#btn-agent-manager').get_attribute('aria-expanded') == 'true'
    assert await page.locator('#agent-manager-anchor').evaluate('(el) => el.parentNode.id') == 'cc-inline-controls'
    assert not await page.locator('#workspace-right').is_visible()
    await page.locator('#workspace-branch-heading').click()
    assert await page.locator('#agent-manager-panel').is_visible()
    await page.screenshot(path=str(tmp_path / 'agents-sidebar.png'))
    await page.locator('#agent-manager-close').click()
    assert await page.locator('#btn-agent-manager').get_attribute('aria-expanded') == 'false'
    for side in ['left', 'right']:
        await page.evaluate('(side) => workspaceSetOpen(side, true)', side)
        await page.locator(f'#workspace-toggle-{side}').click()
        assert await page.locator(f'#workspace-toggle-{side}').get_attribute('aria-expanded') == 'false'
        await page.locator(f'#workspace-toggle-{side}').click()
        assert await page.locator(f'#workspace-toggle-{side}').get_attribute('aria-expanded') == 'true'
    await page.wait_for_function("document.getElementById('view-chat').classList.contains('workspace-controls-flanking')")
    controls = await page.locator('#workspace-controls-left').bounding_box()
    composer = await page.locator('#input-area').bounding_box()
    panel = await page.locator('#workspace-left').bounding_box()
    assert abs(controls['y'] - composer['y']) < 2
    assert panel['y'] + panel['height'] <= controls['y'] + 2
    await page.screenshot(path=str(tmp_path / 'flanking-controls.png'))
    await page.set_viewport_size({'width': 1000, 'height': 800})
    await page.wait_for_function("!document.getElementById('view-chat').classList.contains('workspace-controls-flanking')")
    toolbar = await page.locator('#chat-workspace-toolbar').bounding_box()
    messages = await page.locator('#messages-container').bounding_box()
    assert toolbar['y'] + toolbar['height'] <= messages['y'] + 1
    await page.locator('#btn-agent-manager').click()
    await page.wait_for_selector('#agent-manager-panel')
    agent_panel = await page.locator('#agent-manager-panel').bounding_box()
    assert agent_panel['y'] >= toolbar['y'] + toolbar['height']
    assert await page.locator('#workspace-toggle-canvas').evaluate('(el) => el.parentNode.id') == 'workspace-controls-right'


async def test_agents_counter_stays_visible_with_closed_panel(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    await open_conversation(page, conv, assistant)
    await page.evaluate("switchView('chat'); renderAgentActivity()")
    badge = page.locator('#agent-manager-count')
    await page.wait_for_function("document.getElementById('agent-manager-count').textContent === '4'")
    assert await badge.is_visible()
    assert not await page.locator('#agent-manager-panel').is_visible()
    await page.set_viewport_size({'width': 390, 'height': 844})
    assert await badge.is_visible()
    await page.evaluate("""() => {
        State.agentRuns.forEach((run, i) => run.status = i === 0 ? 'running' : 'completed');
        renderAgentActivity();
    }""")
    assert await badge.inner_text() == '1'
    await page.evaluate("State.agentRuns.forEach(run => run.status = 'completed'); renderAgentActivity()")
    assert not await badge.is_visible()


async def test_action_mode_without_plan_picker(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    await open_conversation(page, conv, assistant)
    assert await page.locator('#cc-permission-mode-inline').count() == 0
    assert await page.locator('#cfg-cc-permission').count() == 0
    settings = await page.evaluate("""() => {
        State.currentConv.cc_permission_mode = 'plan';
        const message = {};
        _attachCCSettings(message);
        return message;
    }""")
    assert settings['cc_permission_mode'] == 'default'
    await page.locator('#btn-settings').click()
    await page.wait_for_function("document.getElementById('modal-settings').dataset.loading !== '1'")
    assert await page.locator('#modal-settings').is_visible()


async def test_clear_agent_notifications_keeps_history(activity_browser):
    import database as db
    page, conv, user, assistant, runs = activity_browser
    for run, status in zip(runs, ['completed', 'failed', 'running', 'running']):
        await db.update_agent_run(run['run_id'], status=status)
    await open_conversation(page, conv, assistant)
    await page.evaluate("State.agentRuns.find(run => run.run_id === 'activity-3')._permissionPending = true; renderAgentActivity()")
    await page.locator('#btn-agent-manager').click()
    await page.locator('#agent-manager-panel').wait_for(state='visible')
    await page.locator('#agent-clear-notifications').click()
    assert not await page.locator('.agent-dock-completed').is_visible()
    assert not await page.locator('.agent-dock-failed').is_visible()
    assert await page.locator('#agent-manager-count').inner_text() == '2'
    assert await page.locator('#agent-dock-attention').is_visible()
    assert await page.locator('.agent-run-card').count() == 4
    await page.reload(wait_until='domcontentloaded')
    await page.wait_for_function("State.agentRuns?.length === 4 && !_agentRefreshJob")
    assert not await page.locator('.agent-dock-completed').is_visible()
    assert not await page.locator('.agent-dock-failed').is_visible()
    await page.locator('#btn-agent-manager').click()
    await page.locator('#agent-manager-panel').wait_for(state='visible')
    assert await page.locator('.agent-run-card').count() == 4
    # Terminal timestamps can arrive via a snapshot after the live completion was cleared.
    await db.update_agent_run('activity-0', completed_at=time.time())
    await page.evaluate("refreshAgentRuns()")
    assert not await page.locator('.agent-dock-completed').is_visible()
    await page.reload(wait_until='domcontentloaded')
    await page.wait_for_function("State.agentRuns?.length === 4 && !_agentRefreshJob")
    assert not await page.locator('.agent-dock-completed').is_visible()
    # A resumed attempt still gets a new completion notification.
    # Use durable attempt metadata: a simultaneous snapshot may legitimately
    # replace a client-only edit with the database's previous attempt.
    await db.update_agent_run('activity-0', started_at=time.time() + 10)
    await page.evaluate('refreshAgentRuns()')
    await page.locator('.agent-dock-completed').wait_for(state='visible')
    assert await page.locator('.agent-dock-completed').inner_text() == '1'


async def test_copy_canvas_link_uses_tailnet_response(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    await open_conversation(page, conv, assistant)
    await page.evaluate("""() => {
        State.currentConv.canvas_enabled = true;
        State.currentConv.canvas_slug = 'dark-penguin-x7k2';
        Object.defineProperty(navigator, 'clipboard', {configurable:true,
            value:{writeText:async text => { window.copiedCanvasLink = text; }}});
        syncWorkspaceLayout();
    }""")
    await page.route(f'**/api/canvas/{conv["id"]}/share-link', lambda route:
        route.fulfill(json={'url': 'https://desktop.tail123.ts.net:3000/dark-penguin-x7k2'}))
    await page.evaluate('updateCanvasSlugPill()')
    await page.locator('#canvas-slug-pill').click()
    await page.wait_for_function("window.copiedCanvasLink === 'https://desktop.tail123.ts.net:3000/dark-penguin-x7k2'")
    assert await page.locator('#workspace-copy-canvas').count() == 0


async def test_compact_composer_statuses_and_mobile_fit(activity_browser):
    import database as db
    page, conv, user, assistant, runs = activity_browser
    for run, status in zip(runs, ['running', 'completed', 'failed', 'needs_relaunch']):
        await db.update_agent_run(run['run_id'], status=status)
    await page.set_viewport_size({'width': 1920, 'height': 1080})
    await open_conversation(page, conv, assistant)
    await page.evaluate("""() => {
        switchView('chat');
        State.agentRuns[0].status = 'running';
        State.agentRuns[1].status = 'completed';
        State.agentRuns[2].status = 'failed';
        State.agentRuns[3].status = 'needs_relaunch';
        renderAgentActivity();
    }""")
    assert await page.locator('.agent-dock-completed').inner_text() == '1'
    assert await page.locator('.agent-dock-failed').inner_text() == '1'
    assert await page.locator('#agent-dock-attention').inner_text() == '! 1'
    assert await page.locator('#agent-dock-attention').get_attribute('aria-label') == '1 agents need attention (0 pending approvals)'
    model = await page.locator('#cc-model-inline').bounding_box()
    agents = await page.locator('#btn-agent-manager').bounding_box()
    canvas = await page.locator('#btn-canvas-toggle').bounding_box()
    assert abs(model['y'] - agents['y']) < 5
    assert model['height'] == agents['height']
    assert model['height'] == canvas['height']
    assert await page.locator('#cc-model-inline').evaluate('(el) => getComputedStyle(el).borderRadius') == await page.locator('#btn-agent-manager').evaluate('(el) => getComputedStyle(el).borderRadius')
    assert model['x'] < agents['x'] < canvas['x']
    for width in [1920, 1000]:
        await page.set_viewport_size({'width': width, 'height': 1080})
        await page.locator('#btn-agent-manager').click()
        await page.locator('#agent-manager-panel').wait_for(state='visible')
        panel = await page.locator('#agent-manager-panel').bounding_box()
        message = await page.locator('#messages > .message').first.bounding_box()
        assert panel and message and abs(panel['width'] - message['width']) < 2
        assert abs(panel['x'] - message['x']) < 2
        await page.locator('#agent-manager-close').click()
    async with await page.context.browser.new_context(is_mobile=True, has_touch=True, viewport={'width': 320, 'height': 844}) as mobile:
        phone = await mobile.new_page()
        await phone.goto(page.url, wait_until='domcontentloaded')
        await phone.wait_for_function("!document.body.classList.contains('app-booting') && !_agentRefreshJob")
        for width, height in [(320, 844), (390, 844), (768, 844), (844, 390)]:
            await phone.set_viewport_size({'width': width, 'height': height})
            await phone.wait_for_timeout(250)
            await phone.evaluate('syncWorkspaceLayout(); renderAgentActivity()')
            assert await phone.evaluate('isMobileDevice()')
            assert await phone.locator('#btn-canvas-toggle').evaluate('(el) => el.parentNode.id') == 'cc-inline-controls'
            for selector in ['#cc-model-inline', '#cc-effort-inline', '#btn-agent-manager', '#agent-dock-attention', '#btn-canvas-toggle']:
                box = await phone.locator(selector).bounding_box()
                assert box and box['x'] >= 0 and box['x'] + box['width'] <= width, (width, selector, box)
            await phone.locator('#btn-agent-manager').click()
            await phone.locator('#agent-manager-panel').wait_for(state='visible', timeout=5000)
            box = await phone.locator('#agent-manager-panel').bounding_box()
            assert box and box['x'] >= 0 and box['x'] + box['width'] <= width and box['y'] >= 0
            composer = await phone.locator('#input-area').bounding_box()
            assert abs(box['width'] - composer['width']) < 2
            heights = [await phone.locator(selector).bounding_box() for selector in ['#cc-model-inline', '#cc-effort-inline', '#btn-agent-manager', '#btn-canvas-toggle']]
            assert all(item['height'] == heights[0]['height'] for item in heights)
            await phone.locator('#agent-manager-close').click()
            await phone.locator('#agent-manager-panel').wait_for(state='hidden', timeout=5000)


async def test_branch_header_fixed_and_panel_modes_exclusive(activity_browser, tmp_path):
    page, conv, user, assistant, runs = activity_browser
    await page.set_viewport_size({'width': 1920, 'height': 1080})
    await open_conversation(page, conv, assistant)
    await page.evaluate("switchView('chat'); setWorkspaceLeftMode(false)")
    await page.evaluate("""() => {
        const list = document.getElementById('workspace-branches');
        for (let i = 0; i < 60; i++) {
            const row = document.createElement('p');
            row.textContent = 'Older branch ' + i;
            list.appendChild(row);
        }
    }""")
    before = await page.locator('#workspace-left-header').bounding_box()
    await page.locator('#workspace-left-scroll').evaluate('(el) => el.scrollTop = el.scrollHeight')
    assert await page.locator('#workspace-left-scroll').evaluate('(el) => el.scrollTop') > 0
    assert await page.locator('#workspace-left-header').bounding_box() == before
    assert await page.locator('#workspace-controls-left').evaluate('(el) => getComputedStyle(el).backgroundColor') == 'rgba(0, 0, 0, 0)'
    await page.locator('#workspace-projects').click()
    assert await page.locator('#workspace-conversations').is_visible()
    assert not await page.locator('#workspace-branches').is_visible()
    assert await page.locator('#workspace-toggle-left').get_attribute('aria-expanded') == 'false'
    await page.locator('#workspace-toggle-left').click()
    assert await page.locator('#workspace-branches').is_visible()
    assert not await page.locator('#workspace-conversations').is_visible()
    assert await page.locator('#workspace-projects').get_attribute('aria-expanded') == 'false'
    await page.screenshot(path=str(tmp_path / 'integrated-controls-fixed-header.png'))


async def test_usage_warning_stays_dismissed_until_reload(activity_browser):
    page, *_ = activity_browser
    async def quota(route):
        await route.fulfill(json={'providers': [{'label': 'Fixture', 'status': 'ok',
            'observed_at': time.time(), 'windows': [{'label': '5-hour', 'used_percent': 98,
                'resets_at': time.time()+3600}]}]})
    await page.route('**/api/provider-usage', quota)
    await page.reload()
    await page.wait_for_selector('#provider-usage-warning')
    await page.get_by_role('button', name='Dismiss usage warning until refresh').click()
    await page.evaluate('loadProviderUsage(true)')
    assert not await page.locator('#provider-usage-warning').is_visible()
    await page.reload()
    await page.wait_for_selector('#provider-usage-warning')
