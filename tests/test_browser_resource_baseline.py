"""Isolated A/B measurements; no physical-device or whole-machine CPU claims."""
import asyncio

from tests.lifecycle_metrics import browser_delta, browser_sample, observe
from tests.test_lifecycle_handoff_browser import lifecycle_browser


async def test_idle_raytracer_cost_and_visibility_signal(lifecycle_browser, record_property):
    page, conv, client, workspace = lifecycle_browser
    await page.wait_for_function("document.querySelector('#blackhole-container canvas') !== null", timeout=30_000)
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Performance.enable")
    cursor = await page.evaluate("JSON.stringify(State.loomCursor)")
    # Alternate order to reduce warmup/order bias. This uses the existing user
    # toggle in a disposable browser profile, not a change to the product default.
    for index in range(3):
        for enabled in ([True, False] if index % 2 == 0 else [False, True]):
            await page.evaluate("on => on ? LoomBlackhole.enable() : LoomBlackhole.disable()", enabled)
            await asyncio.sleep(.5)
            before = await browser_sample(cdp)
            await asyncio.sleep(3)
            metrics = browser_delta(before, await browser_sample(cdp))
            assert await page.evaluate("LoomBlackhole.isEnabled()") == enabled
            assert await page.evaluate("JSON.stringify(State.loomCursor)") == cursor
            observe(record_property, f"browser_idle_{'on' if enabled else 'off'}_{index + 1}",
                    **metrics, raytracer_canvas=1)

    await page.evaluate("""() => {
        LoomBlackhole.enable();
        Object.defineProperty(document, 'hidden', {configurable:true,get:()=>true});
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    await asyncio.sleep(.5)
    before = await browser_sample(cdp)
    await asyncio.sleep(3)
    observe(record_property, "browser_visibility_signal", **browser_delta(before, await browser_sample(cdp)), raytracer_canvas=1)
    # This tests application handling of the visibility signal. Chromium is
    # headless and may still render: do not label it OS background throttling.
    await page.evaluate("""() => {
        Object.defineProperty(document, 'hidden', {configurable:true,get:()=>false});
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    assert await page.evaluate("JSON.stringify(State.loomCursor)") == cursor
