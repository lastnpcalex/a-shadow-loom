"""Actual touch Chromium control geometry, effort selection and notice dismissal."""
from tests.test_exoloom_mobile_browser import mobile_browser
from tests.test_agent_activity_browser import activity_browser, open_conversation


async def test_mobile_controls_and_notice_dismissal(mobile_browser, tmp_path):
    page, conv, user, assistant, _ = mobile_browser
    await open_conversation(page, conv, assistant)
    output = tmp_path / 'mobile-controls'
    output.mkdir(parents=True, exist_ok=True)
    for width, height in [(320, 740), (390, 844), (412, 915), (844, 390)]:
        await page.set_viewport_size({'width': width, 'height': height})
        await page.evaluate('syncWorkspaceLayout()')
        await page.screenshot(path=str(output / f'controls-{width}x{height}.png'))
        geometry = await page.evaluate('''() => Object.fromEntries(
            ['cc-model-inline','cc-effort-inline','input-area','chat-workspace-toolbar'].map(id=>{
                const e=document.getElementById(id),r=e.getBoundingClientRect();
                return [id,{x:r.x,y:r.y,width:r.width,height:r.height,display:getComputedStyle(e).display}];
            }))''')
        print(width, geometry)
        # Approval profiles live in conversation settings, not this composer.
        for selector in ['cc-model-inline', 'cc-effort-inline']:
            rect = geometry[selector]
            assert rect['display'] != 'none'
            # Landscape uses the compact wide-layout controls; portrait keeps
            # the larger mobile model/effort sizes.
            assert rect['width'] >= (55 if width < 768 else 44)
            assert rect['x'] >= 0 and rect['x'] + rect['width'] <= width
            assert rect['y'] + rect['height'] <= height
        assert geometry['chat-workspace-toolbar']['height'] <= 56
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        await page.locator('#cc-effort-inline').select_option('medium')
        await page.wait_for_function("State.currentConv.cc_effort==='medium'")

    # Existing native provider controls must also remain visible and save
    # effort without changing the selected model.
    await page.set_viewport_size({'width':390,'height':844})
    models = await page.evaluate('''() => [_isAnthropicValue,_isCodexValue,_isOpenRouterValue,_isGooseValue].map(matches =>
        [...document.getElementById('cc-model-inline').options].find(o=>!o.disabled &&
            matches(o.value) && _supportsEffortValue(o.value))?.value)''')
    assert all(models), models
    for model in models:
        await page.locator('#cc-model-inline').select_option(model)
        await page.wait_for_function('(model)=>State.currentConv.cc_model===model', arg=model)
        assert await page.locator('#cc-effort-inline').is_visible(), model
        await page.locator('#cc-effort-inline').select_option('medium')
        await page.wait_for_function('''async model => {
            const saved=await API.get(`/api/conversations/${State.currentConvId}`);
            return saved.cc_model===model && saved.cc_effort==='medium';
        }''', arg=model)

    # Gemini uses model variants, so changing effort must persist the exact
    # variant rather than expose a control that silently does nothing.
    await page.set_viewport_size({'width':390,'height':844})
    await page.evaluate('''async () => {
        _ccModelsCache=[{group:'Fixture Gemini',models:['low','medium','high'].map(effort=>({
            value:`gemini:gemini-3.8-flash-${effort}`,label:`Gemini Flash ${effort}`}))}];
        State.currentConv.cc_model='gemini:gemini-3.8-flash-high';
        _rememberExplicitCCModel(State.currentConvId,State.currentConv.cc_model);
        await updateInlineCCControls(State.currentConv);
    }''')
    assert await page.locator('#cc-effort-inline').is_visible()
    assert await page.locator('#cc-effort-inline').input_value() == 'high'
    await page.locator('#cc-effort-inline').select_option('medium')
    await page.wait_for_function("State.currentConv.cc_model==='gemini:gemini-3.8-flash-medium'")
    await page.wait_for_function('''async () => {
        const result=await API.get(`/api/conversations/${State.currentConvId}`);
        return result.cc_model==='gemini:gemini-3.8-flash-medium' && result.cc_effort==='medium';
    }''')
    await page.evaluate('updateInlineCCControls(State.currentConv)')
    assert await page.locator('#cc-model-inline').input_value() == 'gemini:gemini-3.8-flash-medium'
    await page.screenshot(path=str(output / 'gemini-effort-390x844.png'))

    await page.evaluate('''() => handleWSMessage({type:'permission_notice',status:'pending',
        request_id:'fixture-orphan-notice',conv_id:999,message_id:888})''')
    await page.locator('#notif-bell').tap()
    await page.screenshot(path=str(output / 'notifications-390x844.png'))
    await page.get_by_role('button', name='Dismiss notification', exact=True).tap()
    assert await page.locator('.notif-perm-header', has_text='Conversation #999').count() == 0
    await page.evaluate('''() => handleWSMessage({type:'permission_notice',status:'pending',
        request_id:'fixture-orphan-notice',conv_id:999,message_id:888})''')
    assert await page.locator('.notif-perm-header', has_text='Conversation #999').count() == 0
    assert await page.evaluate("JSON.parse(localStorage.getItem('exo-dismissed-permission-notices')).includes('fixture-orphan-notice')")
    assert not await page.evaluate("_resolvedPermissionNoticeIds.has('fixture-orphan-notice')"), 'dismissal is not a permission decision'
    await page.evaluate('''assistant => {
        const saved={...assistant,turn_input_tokens:1200,turn_output_tokens:400,
            generation_ms:9500,time_to_first_token_ms:700,turn_cost_usd:0.012};
        document.querySelector(`.message[data-msg-id="${assistant.id}"]`).replaceWith(createMessageElement(saved));
    }''', assistant)
    footer=page.locator('.message-telemetry').last
    assert 'in' in await footer.locator('summary').inner_text()
    assert not await footer.locator('.message-telemetry-panel').is_visible()
    await footer.locator('summary').tap()
    assert await footer.locator('.message-telemetry-panel').is_visible()
    assert 'First response 0.7s' in await footer.inner_text()
    await page.screenshot(path=str(output / 'expanded-telemetry-390x844.png'))
