"""Goals live in the side lane; Agents have one composer control."""
from tests.test_agent_activity_browser import activity_browser, open_conversation


async def test_goals_lane_and_single_agent_control(activity_browser):
    page, conv, user, assistant, runs = activity_browser
    await open_conversation(page, conv, assistant)
    await page.evaluate("switchView('chat')")
    # This presentation fixture's default model may resolve to another harness.
    await page.evaluate("() => { State.currentConv.mode = 'codex'; renderCodexGoalRow(); }")
    assert await page.locator('#workspace-agents').count() == 0
    assert await page.locator('#btn-agent-manager').is_visible()
    assert await page.locator('#codex-goal-row').evaluate('(node) => node.parentElement.id') == 'workspace-agent-pane'
    assert not await page.locator('#workspace-agent-pane').is_visible()
    assert await page.locator('#input-area #codex-goal-row').count() == 0
    assert await page.locator('#workspace-goals').is_visible(), await page.evaluate("JSON.stringify({mode:State.currentConv.mode, view:State.currentView})")
    await page.locator('#workspace-goals').click()
    assert await page.locator('#modal-codex-goal').is_visible()
    assert not await page.locator('#workspace-agent-pane').is_visible()
    await page.evaluate("document.getElementById('modal-codex-goal').classList.add('hidden')")
    await page.evaluate("() => { State.currentConv.codex_goal_objective = 'Review all three decks'; State.currentConv.codex_goal_status = 'active'; renderCodexGoalRow(); }")
    await page.locator('#workspace-goals').click()
    assert await page.locator('#workspace-agent-pane').is_visible()
    assert await page.locator('#codex-goal-objective').text_content() == 'Review all three decks'
    await page.locator('#workspace-goals-close').click()
    assert not await page.locator('#workspace-agent-pane').is_visible()
    await page.locator('#btn-agent-manager').click()
    assert await page.locator('#agent-manager-panel').is_visible()
    assert not await page.locator('#workspace-agent-pane').is_visible()
    await page.evaluate("() => { State.currentConv.codex_goal_objective = null; renderCodexGoalRow(); }")
    assert not await page.locator('#codex-goal-row').is_visible()
