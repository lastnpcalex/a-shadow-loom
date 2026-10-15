"""Branch actions must not move rows when exposed by hover or focus."""
from pathlib import Path
import pytest

playwright = pytest.importorskip('playwright.async_api')


async def test_branch_hover_keeps_geometry():
    async with playwright.async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            page.set_default_timeout(5000)
            await page.set_content('<div id="workspace-left"><div id="workspace-branches"></div></div>')
            await page.add_style_tag(content=(Path(__file__).parents[1] / 'static/chat-workspace.css').read_text(encoding='utf-8'))
            await check_geometry(page)
        finally:
            await browser.close()


async def check_geometry(page):
    await page.evaluate("""() => {
        const list = document.getElementById('workspace-branches');
        list.replaceChildren();
        for (let i = 0; i < 3; i++) {
            const row = document.createElement('div');
            row.className = 'workspace-branch-row' + (i === 1 ? ' workspace-pinned-row' : '');
            const button = document.createElement('button');
            button.className = 'workspace-item'; button.textContent = 'Long branch title '.repeat(20);
            if (i !== 1) {
                const detail = document.createElement('small');
                detail.textContent = 'Long branch preview '.repeat(20); button.append(detail);
            }
            row.append(button);
            for (let j = 0; j < (i === 1 ? 2 : 1); j++) {
                const action = document.createElement('button');
                action.className = 'workspace-row-pin'; action.textContent = '✎';
                row.append(action);
            }
            list.append(row);
        }
    }""")
    rows = page.locator('#workspace-branches .workspace-branch-row')
    for width in (1920, 1000, 390):
        await page.set_viewport_size({'width': width, 'height': 1080})
        await page.mouse.move(0, 0)
        await page.evaluate('document.activeElement.blur()')
        before = [await rows.nth(i).bounding_box() for i in range(3)]
        for i in range(3):
            await rows.nth(i).hover()
            assert [await rows.nth(j).bounding_box() for j in range(3)] == before
            await rows.nth(i).locator('.workspace-row-pin').first.focus()
            assert [await rows.nth(j).bounding_box() for j in range(3)] == before
            assert await rows.nth(i).locator('.workspace-item').evaluate(
                '(el) => el.scrollWidth > el.clientWidth')
