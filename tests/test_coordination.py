"""Lean-generated decisions plus real SQLite/admission boundary regressions.

No live host, model calls or real user data. Node is required, not skipped.
"""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import random
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import coordination as c
import database as db

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "formal/coordination/generated/cases.json"


def assert_conforms(reducer):
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert cases["version"] == 1 and len(cases["steps"]) == 3360
    for before, event, after, effect, failed in cases["steps"]:
        state, actual = reducer(c.State(*before), *event)
        assert (state.as_list(), actual, c.failed(state)) == (after, effect, failed), (before, event)


def test_every_finite_transition_matches_lean():
    assert_conforms(c.step)


def test_browser_launch_feedback_matches_lean():
    node = shutil.which('node')
    assert node, 'Node is mandatory for launch conformance'
    script = """
const fs = require('fs');
const c = require('./static/coordination.js');
const rows = JSON.parse(fs.readFileSync('formal/coordination/generated/cases.json.launch.json', 'utf8'));
if (rows.length !== 30) throw Error('Incomplete launch oracle');
for (const [state,event,expected] of rows) {
  if (c.launchStep(state,event) !== expected) throw Error(`launch mismatch: ${state}/${event}`);
}
"""
    subprocess.run([node, '-e', script], cwd=ROOT, check=True, timeout=15, capture_output=True)


def test_generated_traces_and_browser_decisions_match_lean():
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for trace in cases["traces"]:
        state = c.State()
        for event, expected, expected_effect in trace:
            state, effect = c.step(state, *event)
            assert (state.as_list(), effect) == (expected, expected_effect)
    for args, expected in cases["views"]:
        assert list(c.view_step(*args)) == expected
    node = shutil.which("node")
    assert node, "Node is mandatory for coordination conformance"
    script = """
const fs = require('fs');
const c = require('./static/coordination.js');
const cases = JSON.parse(fs.readFileSync('formal/coordination/generated/cases.json', 'utf8'));
for (const [args, expected] of cases.views) {
  if (JSON.stringify(c.viewStep(...args)) !== JSON.stringify(expected)) throw Error('view mismatch');
}
"""
    subprocess.run([node, "-e", script], cwd=ROOT, check=True, timeout=15, capture_output=True)


@pytest.mark.parametrize("mutation", ["early_release", "tool_text_error", "queue_after_cancel"])
def test_fixture_rejects_prior_failure_classes(mutation):
    def broken(state, event, value=None):
        if mutation == "early_release" and event == "release":
            return replace(state, leased=False), "release_lease"
        if mutation == "tool_text_error" and event == "tool_data" and value:
            return replace(state, outcome="failed"), "none"
        if mutation == "queue_after_cancel" and event == "dispatch" and not state.leased:
            return replace(state, queue="dispatched"), "launch_queued"
        return c.step(state, event, value)
    with pytest.raises(AssertionError):
        assert_conforms(broken)


def test_long_adversarial_event_traces_remain_safe():
    rng = random.Random(417)
    events = [("terminal", o) for o in c.OUTCOMES] + [
        ("process_exited",), ("release",), ("dispatch",), ("resume_queue",),
        ("stop", False), ("stop", True), ("tool_data", "timeout/failed/reconnecting"),
        ("diagnostic_error",),
    ]
    for _ in range(100):
        state = c.State()
        launches = releases = 0
        for _ in range(100):
            old = state
            state, effect = c.step(state, *rng.choice(events))
            assert state.leased or state.exited
            if old.outcome != "pending":
                assert state.outcome == old.outcome
            if old.queue == "paused" and state.queue == "waiting":
                assert state.authorized
            launches += effect == "launch_queued"
            releases += effect == "release_lease"
            assert launches <= 1 and releases <= 1


@pytest.mark.parametrize("terminal", ["completed", "error", "partial_failed", "cancelled", "interrupted"])
@pytest.mark.parametrize("exited", [False, True])
async def test_generation_finalizer_and_done_callback_obey_exit_evidence(monkeypatch, terminal, exited):
    import server
    monkeypatch.setattr(server, "_active_generations", {})
    monkeypatch.setattr(server, "_generation_snapshots", {})
    monkeypatch.setattr(server, "_shutdown_requested", False)
    monkeypatch.setattr(server, "_ws_send", AsyncMock())
    backgrounds = []
    def background(coro, *args, **kwargs):
        task = asyncio.create_task(coro)
        backgrounds.append(task)
        return task
    monkeypatch.setattr(server, "_spawn_background_task", background)
    dispatch = AsyncMock()
    monkeypatch.setattr(server, "_dispatch_ready_queued_generations", dispatch)
    conv = await db.create_conversation("Coordination fixture", mode="codex")
    prompt = await db.add_message(conv["id"], "user", "bounded fixture")
    exit_acknowledged = asyncio.Event()
    class Process:
        def __init__(self, returncode):
            self.returncode = returncode
        async def wait(self):
            if self.returncode is None:
                await exit_acknowledged.wait()
                self.returncode = 0
            return self.returncode
    handles = [Process(0), Process(0 if exited else None)]
    seen = {}
    async def provider(_ws, conv_id, payload):
        for handle in handles:
            server._track_generation_provider(handle)
        # Both old and retry handles must be retained even if registry was lost.
        server._track_generation_provider(handles[0])
        assert len(asyncio.current_task()._coordination_processes) == 2
        draft = await server._create_generation_draft(conv_id, conv, prompt["id"])
        seen["draft"] = draft["id"]
        queued = await db.add_message(conv_id, "user", "next", parent_id=draft["id"])
        await db.enqueue_generation(queued["id"], conv_id, draft["id"], settings={})
        await db.update_message_content(draft["id"], content="fixture output")
        if terminal != "completed":
            asyncio.current_task()._generation_terminal_override = terminal
    monkeypatch.setattr(server, "_handle_generation_with_workspace_safety", provider)
    _, task = await server._launch_generation_task(None, conv["id"], {"parent_id": prompt["id"]})
    await task
    await asyncio.sleep(0)  # done callback must not bypass finalizer
    if backgrounds and exited:
        await asyncio.gather(*backgrounds)
    lease = await db.get_generation_lease(task._generation_lease_id)
    assert (lease["released_at"] is not None) == exited
    if exited:
        assert lease["terminal_state"] == terminal
    queue = await db.list_queued_generations(conv["id"])
    assert queue[0]["status"] == ("queued" if exited and terminal == "completed" else "paused")
    assert dispatch.await_count == int(exited and terminal == "completed")
    if not exited:
        with pytest.raises(server.GenerationLaneOccupied):
            await server._launch_generation_task(None, conv["id"], {"parent_id": prompt["id"]})
        # Real deferred cleanup must remain pending until process exit is
        # acknowledged, then release the retained lease rather than crash.
        assert any(not task.done() for task in backgrounds)
        exit_acknowledged.set()
        await asyncio.wait_for(asyncio.gather(*backgrounds), 5)
        lease = await db.get_generation_lease(task._generation_lease_id)
        assert lease['released_at'] is not None


async def test_cancel_before_first_execution_releases_without_entering_provider(monkeypatch):
    import server
    monkeypatch.setattr(server, "_active_generations", {})
    monkeypatch.setattr(server, "_generation_snapshots", {})
    monkeypatch.setattr(server, "_shutdown_requested", False)
    provider = AsyncMock()
    monkeypatch.setattr(server, "_handle_generation_with_workspace_safety", provider)
    backgrounds = []
    def background(coro, *args, **kwargs):
        task = asyncio.create_task(coro)
        backgrounds.append(task)
        return task
    monkeypatch.setattr(server, "_spawn_background_task", background)
    conv = await db.create_conversation("Pre-entry cancellation fixture", mode="codex")
    prompt = await db.add_message(conv["id"], "user", "fixture")
    _, task = await server._launch_generation_task(None, conv["id"], {"parent_id": prompt["id"]})
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    await asyncio.gather(*backgrounds)
    provider.assert_not_awaited()
    lease = await db.get_generation_lease(task._generation_lease_id)
    assert lease["released_at"] is not None
    assert c.outcome_for_lifecycle(lease["terminal_state"]) == "cancelled"


def test_projection_keeps_unknown_outcomes_fail_closed():
    assert c.result_failed(True, True, False) is False
    assert c.result_failed(True, False, False) is True
    assert c.queue_effect(held=False, outcome="unexpected-label", paused=False, authorized=False) == "pause_queue"
    assert c.queue_effect(held=True, outcome="completed", paused=False, authorized=True) == "none"
    assert c.queue_effect(held=False, outcome="cancelled", paused=True, authorized=True) == "none"
