"""Actual approval-card DOM and click behavior, without a live Loom process."""
from pathlib import Path
import pytest

playwright = pytest.importorskip("playwright.async_api")


async def test_permission_cards_show_scope_and_submit_denial_note():
    source = (Path(__file__).resolve().parents[1] / "static" / "chat.js").read_text(encoding="utf-8")
    # Exercise the actual production renderers with only their IO boundaries
    # stubbed. No provider, listener, host lifecycle or real permissions touched.
    functions = []
    for name, following in [("permissionSupportsAllowAll", "addPermissionNotification"),
                            ("_renderPermissionNotifItem", None),
                            ("showPermissionPrompt", "_permissionPromptHost")]:
        start = source.index(f"function {name}(")
        end = source.index(f"function {following}(", start) if following else source.index("\nfunction ", start + 10)
        functions.append(source[start:end])
    async with playwright.async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content('<div id="host"></div><div id="notif"></div>')
            await page.add_script_tag(content="""
                var streamingDiv = null;
                var sent = [];
                var State = {currentConvId: 1, ws: {readyState: 1, send: s => sent.push(JSON.parse(s))}};
                var escapeHtml = s => { const x = document.createElement('span'); x.textContent = String(s); return x.innerHTML; };
                var _sameConvId = (a,b) => String(a) === String(b);
                var _permissionBelongsToVisibleBranch = () => true;
                var _permissionPromptHost = () => document.getElementById('host');
                var scrollToBottom = () => {};
                var resolvePermissionNotification = () => {};
                var resolvePermissionPrompt = () => {};
                var _renderNotifDropdown = () => {};
                var _renderNotifBell = () => {};
                var showToast = () => {};
                var _notifications = [];
            """ + "\n".join(functions))
            await page.evaluate("""() => {
                showPermissionPrompt({request_id: 'fixture-1', tool_name: 'Bash', supports_allow_all: true,
                    input_summary: 'python -m pytest tests/a.py',
                    rule_preview: 'python -m pytest <workspace-file>; branch #12 and descendants; until revoked or server restart.'});
            }""")
            assert '<workspace-file>' in await page.locator('.permission-body').inner_text()
            await page.locator('.permission-feedback-input').fill('Use the reviewed target.')
            await page.get_by_role('button', name='Allow for Branch', exact=True).click()
            assert await page.evaluate('sent[0]') == {"action": "permission_response", "request_id": "fixture-1",
                "allow": True, "always": True, "feedback": "Use the reviewed target."}
            await page.evaluate("""() => {
                document.getElementById('notif').appendChild(_renderPermissionNotifItem({
                    requestId:'fixture-2', convId:1, agentRunId:'fixture-child', toolName:'shell', time:new Date(),
                    supportsAllowAll:false, rulePreview:'This request supports one-time approval only.', inputSummary:'opaque command'
                }));
            }""")
            assert await page.locator('#notif .allow-all').count() == 0
            assert 'one-time approval only' in await page.locator('#notif').inner_text()
            await page.locator('#notif input').fill('Please change the destination.')
            await page.locator('#notif button.deny').click()
            assert await page.evaluate('sent[1].feedback') == 'Please change the destination.'
            assert await page.evaluate('sent[1].allow') is False
            await page.evaluate("""() => {
                document.getElementById('host').innerHTML = '';
                showPermissionPrompt({request_id:'preservation', tool_name:'Write', supports_allow_all:false,
                    risk_level:'destructive', risk_reason:'Replace existing file canvas/index.html. Pre-write recovery retained.',
                    input_summary:'canvas/index.html', rule_preview:'File preservation review is one-time; branch grants cannot bypass it.'});
            }""")
            assert 'Replace existing file canvas/index.html' in await page.locator('#host').inner_text()
            assert await page.locator('#host').get_by_role('button', name='Allow for Branch', exact=True).count() == 0
            await page.locator('#host .permission-feedback-input').fill('Keep the existing tools; add a separate page.')
            await page.locator('#host').get_by_role('button', name='Deny', exact=True).click()
            assert await page.evaluate('sent[2].feedback') == 'Keep the existing tools; add a separate page.'
            assert await page.evaluate('sent[2].allow') is False
        finally:
            await browser.close()


@pytest.mark.parametrize("width", [320, 390, 900])
async def test_settings_revoke_uses_actual_api_client(width):
    static = Path(__file__).resolve().parents[1] / "static"
    source = (static / "app.js").read_text(encoding="utf-8")
    html = (static / "index.html").read_text(encoding="utf-8")
    section = html[html.index('                <div class="form-group" id="cfg-permission-policy-group">'):
                   html.index('                <div class="settings-overrides" id="cfg-overrides-loom">')]
    api = source[source.index("const API = {"):source.index("// ── Application State")]
    start = source.index("        const policyGroup =")
    settings = source[start:source.index("        const weaveMaxTokens", start)]
    descriptions = source[source.index('    const profileDescriptions ='):source.index('    const adminSettingsUrl =')]
    async with playwright.async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": width, "height": 900})
            await page.set_content('<main style="padding: 16px; max-width: 620px">' + section +
                                   '<select class="form-select" id="reference-select"><option>Reference setting</option></select></main>')
            await page.add_style_tag(content=(static / "style.css").read_text(encoding="utf-8"))
            await page.add_script_tag(content="""
                var State = {currentConvId:1, currentConv:{id:1, mode:'goose', permission_profile:'balanced'}};
                var _settingsLoadSeq = 1;
                var deletes = [];
                var showToast = () => {};
                var fetch = async (url, options={}) => {
                    if (options.method === 'DELETE') deletes.push(url);
                    return new Response(JSON.stringify(options.method === 'DELETE' ? {revoked:true} : {
                        rules:[{id:'fixture-rule', branch_id:12, workspace:'fixture-workspace/' + 'long-scope/'.repeat(16), description:'node build.js'}]
                    }), {status:200});
                };
            """ + api + descriptions + "\nfunction populateRules() { const conv = State.currentConv; const settingsSeq = 1;\n" + settings + "\n} populateRules();")
            await page.get_by_role('button', name='Revoke', exact=True).wait_for()
            await page.evaluate("""() => {
                const profile = document.getElementById('cfg-permission-profile');
                const reference = document.getElementById('reference-select');
                for (const property of ['backgroundColor', 'color', 'fontFamily', 'fontSize', 'borderRadius', 'borderColor', 'padding']) {
                    if (getComputedStyle(profile)[property] !== getComputedStyle(reference)[property])
                        throw new Error('Permission profile differs from themed settings: ' + property);
                }
                const group = document.getElementById('cfg-permission-policy-group');
                const hint = group.querySelector('.hint');
                const bounds = group.getBoundingClientRect();
                if (hint.getBoundingClientRect().top < profile.getBoundingClientRect().bottom)
                    throw new Error('Help text must sit below the control');
                for (const element of group.querySelectorAll('select, span, button, div')) {
                    const rect = element.getBoundingClientRect();
                    if (rect.left < bounds.left - 1 || rect.right > bounds.right + 1)
                        throw new Error('Permission settings overflow: ' + element.tagName);
                }
                const ruleText = group.querySelector('#cfg-permission-rules span');
                const revoke = group.querySelector('button');
                if (ruleText.getBoundingClientRect().right > revoke.getBoundingClientRect().left)
                    throw new Error('Rule text overlaps Revoke');
            }""")
            for profile in ('strict', 'trusted', 'balanced', 'trust_review'):
                await page.locator('#cfg-permission-profile').select_option(profile)
                assert await page.locator('#cfg-permission-profile').input_value() == profile
            assert 'Unrecognized destructive scripts can pass' in await page.locator('#cfg-permission-profile-hint').inner_text()
            await page.locator('#cfg-permission-profile').focus()
            assert await page.locator('#cfg-permission-profile').evaluate("e => getComputedStyle(e).borderColor") == 'rgb(0, 255, 255)'
            await page.get_by_role('button', name='Revoke', exact=True).click()
            await page.wait_for_function("deletes.length === 1 && document.getElementById('cfg-permission-rules').textContent === 'No remembered branch rules.'")
            assert await page.evaluate('deletes[0]') == '/api/conversations/1/permission-rules/fixture-rule'
        finally:
            await browser.close()
