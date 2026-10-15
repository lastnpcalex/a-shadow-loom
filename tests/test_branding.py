"""Display-name changes must not change tab activity or compatibility names."""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_public_branding_and_compatibility_names():
    index = (REPO / "static/index.html").read_text(encoding="utf-8")
    admin = (REPO / "static/admin/index.html").read_text(encoding="utf-8")
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "<title>EX umbra Omnia</title>" in index
    assert '<button id="btn-home" title="Open ExoHome">EX umbra Omnia</button>' in index
    assert "<title>EXO: Admin</title>" in admin
    assert "# EX umbra Omnia" in readme
    assert "EXO: <workspace title>" in readme
    assert 'id="loom-favicon"' in index
    assert "a-shadow-loom.git" in readme
    assert "Loom.send(" in readme


def test_openrouter_display_name_only():
    import openrouter_client
    headers = openrouter_client.request_headers("fixture-token")
    assert headers["X-Title"] == "EX umbra Omnia"
    assert headers["Authorization"] == "Bearer fixture-token"


async def test_browser_titles_keep_activity_in_favicon():
    playwright = pytest.importorskip("playwright.async_api")
    source = (REPO / "static/chat.js").read_text(encoding="utf-8")
    code = source[source.index("const _TAB_ACTIVITY_FRAMES ="):source.index("function setBrowserTabState(")]
    async with playwright.async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content('<link id="loom-favicon" rel="icon" href="/static/img/loom-ico-transparent.png">')
            await page.add_script_tag(content="var State = {currentConv: null};\n" + code)
            await page.evaluate("_renderBrowserTabState()")
            assert await page.title() == "EX umbra Omnia"
            for state in ("looming", "permission", "done", "error", "idle"):
                await page.evaluate("""state => {
                    State.currentConv = {title: '  Work Agents  '};
                    _browserTabState = state;
                    _renderBrowserTabState();
                }""", state)
                assert await page.title() == "EXO: Work Agents"
                href = await page.locator('#loom-favicon').get_attribute('href')
                if state == "idle":
                    assert href == "/static/img/loom-ico-transparent.png"
                else:
                    assert href.startswith("data:image/svg+xml,")
            await page.evaluate("State.currentConv = null; _renderBrowserTabState()")
            assert await page.title() == "EX umbra Omnia"
        finally:
            await browser.close()
