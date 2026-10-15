"""Numeric-only preflight observations; no prompt, path, PID or command logging."""
import json
import math


SCENARIOS = {
    "mobile_rerun_foreground", "launch_lost_ack_recovery",
    "queue_browser", "prelaunch_browser", "child_burst", "snapshot_io",
    "handoff_browser", "child_parent_stop", "native_interrupt", "native_cancel", "discovery_browser",
    "native_goose_interrupt", "native_goose_cancel",
    "native_child_codex_interrupt", "native_child_codex_cancel",
    "native_child_goose_interrupt", "native_child_goose_cancel",
    "native_delegation_codex", "native_delegation_goose",
    "browser_idle_on_1", "browser_idle_on_2", "browser_idle_on_3",
    "browser_idle_off_1", "browser_idle_off_2", "browser_idle_off_3", "browser_visibility_signal",
}
METRICS = {
    "send_to_shell_ms", "send_to_stream_start_ms", "send_to_first_output_ms", "preparation_ms",
    "reconnect_ms", "screen_load_ms", "stop_to_exit_ms", "native_ttft_ms", "os_samples",
    "max_live_provider_trees", "controlled_max_active", "launches", "burst_events", "burst_batches",
    "burst_paints", "burst_ms", "wall_ms", "task_ms", "task_cpu_percent", "heap_bytes",
    "dom_nodes", "layout_ms", "hidden_task_ms", "hidden_wall_ms", "cold_ms", "warm_ms",
    "cold_bytes_read", "warm_bytes_read", "files_reused", "pending_children", "handoffs",
    "raytracer_canvas", "remaining_provider_trees", "auxiliary_plans",
}


def valid_row(row):
    return (isinstance(row, dict) and set(row) == {"scenario", "metrics"}
            and row["scenario"] in SCENARIOS and isinstance(row["metrics"], dict)
            and bool(row["metrics"]) and set(row["metrics"]) <= METRICS
            and all(type(v) in {int, float} and math.isfinite(v) and v >= 0
                    for v in row["metrics"].values()))


def observe(record_property, scenario, **metrics):
    row = {"scenario": scenario, "metrics": metrics}
    if not valid_row(row):
        raise ValueError("Invalid lifecycle metric schema")
    record_property("lifecycle_case", json.dumps(row, sort_keys=True))


async def install_turn_probe(page):
    """Test-only hooks observe real event/render boundaries, not provider claims."""
    await page.evaluate("""() => {
        window.lifecycleProbe = {};
        const original = handleWSMessage;
        window.handleWSMessage = function(data) {
            const p = window.lifecycleProbe;
            const now = performance.now();
            if (p.sent != null) {
                if (data.type === 'generation_preparing' && data.preparation_ms != null)
                    p.preparation = data.preparation_ms;
                if (data.type === 'stream_start' && p.stream == null) p.stream = now;
                if (p.output == null && (data.type === 'first_token' ||
                    (data.type === 'stream_chunk' && data.content) ||
                    (data.type === 'thinking_chunk' && data.content) ||
                    data.type === 'tool_start')) p.output = now;
            }
            return original.apply(this, arguments);
        };
        new MutationObserver(() => {
            const p = window.lifecycleProbe;
            if (p.sent != null && p.shell == null &&
                document.querySelector('.message.assistant.streaming')) p.shell = performance.now();
        }).observe(document.querySelector('#messages'), {childList:true, subtree:true});
    }""")


async def arm_turn_probe(page):
    await page.evaluate("window.lifecycleProbe = {sent: performance.now()}")


async def turn_metrics(page):
    p = await page.evaluate("window.lifecycleProbe")
    result = {}
    for key, name in (("shell", "send_to_shell_ms"), ("stream", "send_to_stream_start_ms"),
                      ("output", "send_to_first_output_ms")):
        if key in p:
            result[name] = max(0, p[key] - p["sent"])
    if "preparation" in p:
        result["preparation_ms"] = p["preparation"]
    return result


async def browser_sample(cdp):
    return {r["name"]: r["value"] for r in (await cdp.send("Performance.getMetrics"))["metrics"]}


def browser_delta(before, after):
    wall = after["Timestamp"] - before["Timestamp"]
    task = after["TaskDuration"] - before["TaskDuration"]
    return {"wall_ms": wall * 1000, "task_ms": task * 1000,
            "task_cpu_percent": task / wall * 100 if wall > 0 else 0,
            "layout_ms": (after["LayoutDuration"] - before["LayoutDuration"]) * 1000,
            "heap_bytes": after["JSHeapUsedSize"], "dom_nodes": after["Nodes"]}
