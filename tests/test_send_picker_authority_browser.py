from tests.test_agent_activity_browser import activity_browser, open_conversation


async def test_send_uses_visible_picker_over_branch_model(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    await open_conversation(page, conv, assistant)
    result = await page.evaluate('''() => {
        const picker = document.getElementById('cc-model-inline');
        picker.add(new Option('Selected fixture model', 'selected-fixture'));
        picker.value = 'selected-fixture';
        _explicitCCModel = {convId: String(State.currentConvId),
            threadId: State.loomCursor.thread_id, model: 'stale-fixture'};
        const payload = {};
        _attachCCSettings(payload);
        return {payload, selected: picker.value};
    }''')
    assert result['payload']['cc_model'] == result['selected'] == 'selected-fixture'
    sent = []
    async def intercept(route):
        sent.append(route.request.post_data_json)
        # Capture the actual Send request without launching a provider.
        await route.fulfill(status=503, json={'detail': 'isolated send capture'})
    await page.route('**/api/conversations/*/messages', intercept)
    await page.locator('#user-input').fill('Verify selected model')
    await page.locator('#btn-send').click()
    await page.wait_for_function('!_sendInFlight')
    assert sent and sent[0]['cc_model'] == 'selected-fixture'
