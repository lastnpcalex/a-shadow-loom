from tests.test_agent_activity_browser import activity_browser, open_conversation


async def test_effort_save_stays_with_chat_and_preferences_are_per_model(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    await open_conversation(page, conv, assistant)
    result = await page.evaluate('''async () => {
        const model = document.getElementById('cc-model-inline');
        const effort = document.getElementById('cc-effort-inline');
        model.add(new Option('GPT fixture', 'gpt-6.1'));
        model.add(new Option('GLM fixture', 'goose:openrouter:z-ai/glm-5.3'));
        const originalPut = API.put;
        let release;
        API.put = () => new Promise(resolve => {release = resolve;});
        model.value = 'gpt-6.1'; effort.value = 'low';
        effort.dispatchEvent(new Event('change'));
        await new Promise(resolve => setTimeout(resolve, 0));
        const other = {id: 9999, mode: 'codex', cc_model: 'gpt-6.1', cc_effort: 'xhigh'};
        State.currentConv = other; State.currentConvId = other.id;
        release({cc_effort: 'low', cc_model: 'wrong-model'});
        await new Promise(resolve => setTimeout(resolve, 0));
        const unaffected = {...other};
        API.put = async (url, body) => body;
        model.value = 'goose:openrouter:z-ai/glm-5.3'; effort.value = 'xhigh';
        effort.dispatchEvent(new Event('change'));
        await new Promise(resolve => setTimeout(resolve, 0));
        const preferences = [_preferredModelEffort('gpt-6.1'),
            _preferredModelEffort('goose:openrouter:z-ai/glm-5.3')];
        API.put = originalPut;
        return {unaffected, preferences};
    }''')
    assert result['unaffected']['cc_effort'] == 'xhigh'
    assert result['unaffected']['cc_model'] == 'gpt-6.1'
    assert result['preferences'] == ['low', 'xhigh']


async def test_explicit_preset_and_newer_effort_win_with_agents_running(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    await open_conversation(page, conv, assistant)
    result = await page.evaluate('''async () => {
        const model = document.getElementById('cc-model-inline');
        const effort = document.getElementById('cc-effort-inline');
        const originalPut = API.put;
        const writes = []; const releases = [];
        API.put = (url, body) => new Promise(resolve => {
            writes.push({...body}); releases.push(() => resolve({...body}));
        });
        const preset = new Option('GPT high fixture', 'gpt-6.1');
        preset.dataset.effort = 'high'; model.add(preset);
        localStorage.setItem('exo-model-effort:gpt-6.1', 'low');
        model.selectedIndex = preset.index;
        model.dispatchEvent(new Event('change'));
        await new Promise(resolve => setTimeout(resolve, 0));
        const clickedEffort = effort.value;
        effort.value = 'medium'; effort.dispatchEvent(new Event('change'));
        effort.value = 'low'; effort.dispatchEvent(new Event('change'));
        const optimistic = State.currentConv.cc_effort;
        const before = writes.length;
        for (let i = 0; i < 3; i++) {
            releases[i](); await new Promise(resolve => setTimeout(resolve, 0));
        }
        const message = {}; _attachCCSettings(message);
        await updateInlineCCControls(State.currentConv);
        const refreshedEffort = effort.value;
        const selectedPreset = model.selectedOptions[0]?.dataset.effort;
        API.put = originalPut;
        return {clickedEffort, optimistic, before, writes, effort: effort.value,
            saved: State.currentConv.cc_effort, sent: message.cc_effort,
            agents: State.agentRuns.length, refreshedEffort, selectedPreset};
    }''')
    assert result['agents'] >= 4
    assert result['clickedEffort'] == 'high'
    assert result['before'] == 1
    assert [row['cc_effort'] for row in result['writes']] == ['high', 'medium', 'low']
    assert result['writes'][0]['cc_model'] == 'gpt-6.1'
    assert result['optimistic'] == result['effort'] == result['saved'] == result['sent'] == 'low'
    assert result['refreshedEffort'] == 'low'
    assert result['selectedPreset'] in (None, 'low')
