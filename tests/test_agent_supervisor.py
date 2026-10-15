"""Durability and parent-independence checks for child agent runs."""

import asyncio
import json
import time
import pytest

import database as db
from agent_supervisor import AgentSupervisor


@pytest.mark.parametrize('selector', ['local', 'inherit', 'goose'])
async def test_local_inference_serializes_while_cloud_scopes_run_in_parallel(tmp_database, selector):
    import server
    conv = await db.create_conversation('Shared inference', mode='local')
    started = []
    release = asyncio.Event()
    async def launch(run, emit):
        started.append(run['task'])
        await release.wait()
        return {'text': 'done'}
    supervisor = AgentSupervisor(launcher=launch, target_resolver=server._resolve_agent_target)
    await supervisor.start()
    try:
        options = {'harness': 'local'} if selector == 'local' else ({'harness': 'goose', 'model': 'goose:llama:fixture'} if selector == 'goose' else {})
        await supervisor.spawn(conv['id'], 'local-a', project_dir='C:/fixture-a', **options)
        second = await supervisor.spawn(conv['id'], 'local-b', project_dir='C:/fixture-b', **options)
        await supervisor.spawn(conv['id'], 'cloud-a', harness='gemini', project_dir='C:/fixture-c')
        await supervisor.spawn(conv['id'], 'cloud-b', harness='gemini', project_dir='C:/fixture-d')
        for _ in range(100):
            if len(started) >= 3:
                break
            await asyncio.sleep(.02)
        assert set(started) == {'local-a', 'cloud-a', 'cloud-b'}
        assert (await db.get_agent_run(second['run_id']))['status'] == 'queued'
        release.set()
        for _ in range(100):
            if 'local-b' in started:
                break
            await asyncio.sleep(.02)
        assert 'local-b' in started
    finally:
        release.set()
        await supervisor.stop()


async def test_nested_local_work_preserves_delegation_without_deadlocking(tmp_database):
    conv = await db.create_conversation('Nested local', mode='local')
    release = asyncio.Event()
    async def launch(run, emit):
        if run['task'] == 'parent':
            await release.wait()
        return {'text': 'done'}
    supervisor = AgentSupervisor(launcher=launch)
    await supervisor.start()
    try:
        parent = await supervisor.spawn(conv['id'], 'parent', harness='local', project_dir='C:/fixture')
        for _ in range(100):
            if (await db.get_agent_run(parent['run_id']))['status'] == 'running':
                break
            await asyncio.sleep(.02)
        child = await supervisor.spawn(conv['id'], 'child', parent_run_id=parent['run_id'])
        for _ in range(100):
            result = await db.get_agent_run(child['run_id'])
            if result['status'] == 'completed':
                break
            await asyncio.sleep(.02)
        assert result['status'] == 'completed'
        assert (await db.get_agent_run(parent['run_id']))['status'] == 'running'
    finally:
        release.set()
        await supervisor.stop()


def test_spawn_agent_inherits_harness_instead_of_silently_forcing_local(monkeypatch):
    import mcp_loom_agents

    captured = {}
    monkeypatch.setenv("LOOM_CONV_ID", "42")
    monkeypatch.setenv("LOOM_PORT", "8123")
    monkeypatch.setenv("LOOM_WORKSPACE_ROOT", "C:/workspace")
    monkeypatch.setenv("LOOM_AGENT_RUN_ID", "gen:9")

    def fake_post(_port, _path, payload):
        captured.update(payload)
        return {"run_id": "child-1", "status": "queued"}

    monkeypatch.setattr(mcp_loom_agents, "_post", fake_post)
    result = json.loads(mcp_loom_agents.spawn_agent("inspect this"))

    assert result["run_id"] == "child-1"
    assert captured["harness"] is None
    assert captured["model"] is None
    assert captured["profile"] is None
    assert captured["parent_run_id"] == "gen:9"


def test_agent_control_uses_top_level_parent_identity_but_nested_caller_wins(monkeypatch):
    import mcp_loom_agents
    monkeypatch.setenv("LOOM_CONV_ID", "42")
    monkeypatch.setenv("LOOM_PARENT_RUN_ID", "gen:9")
    monkeypatch.delenv("LOOM_AGENT_RUN_ID", raising=False)
    captured = []
    def post(_port, _path, payload):
        captured.append(payload)
        return {"run_id": "synthetic-child", "status": "queued"}
    monkeypatch.setattr(mcp_loom_agents, "_post", post)
    mcp_loom_agents.spawn_agent("top-level task")
    assert captured[-1]["parent_run_id"] == "gen:9"
    monkeypatch.setenv("LOOM_AGENT_RUN_ID", "synthetic-calling-child")
    mcp_loom_agents.spawn_agent("nested task")
    assert captured[-1]["parent_run_id"] == "synthetic-calling-child"


def test_child_target_inherits_conversation_model_unless_local_is_explicit():
    import server

    conv = {"mode": "codex", "cc_model": "gpt-5.6-sol", "local_model": "local-qwen"}
    assert server._resolve_agent_target({}, conv) == ("codex", "gpt-5.6-sol")
    assert server._resolve_agent_target({"model_profile": "local"}, conv) == (
        "local", server.config.llama_model,
    )
    assert server._resolve_agent_target(
        {}, {"mode": "openrouter", "cc_model": "openrouter:z-ai/glm-5.3-flash"}
    ) == ("openrouter", "openrouter:z-ai/glm-5.3-flash")
    assert server._resolve_agent_target(
        {}, {"mode": "claude", "cc_model": "dream:diffusiongemma-test"}
    ) == ("dream-claude", "dream:diffusiongemma-test")
    import pytest
    with pytest.raises(ValueError, match="retired"):
        server._resolve_agent_target({}, {"mode": "dream", "local_model": "diffusiongemma-test"})


async def test_claude_compatibility_child_lanes_keep_their_provider_route(
    tmp_database, tmp_path, monkeypatch
):
    import server

    captured = []

    class Proc:
        pid = None
        returncode = 0

    async def fake_run_claude(prompt, cwd, **kwargs):
        captured.append(kwargs)

        async def events():
            yield {"type": "result", "text": "done", "model": kwargs.get("model")}

        return Proc(), events()

    monkeypatch.setattr(server.claude_client, "run_claude", fake_run_claude)
    conv = await db.create_conversation(
        "Compatibility children", mode="claude", project_dir=str(tmp_path)
    )

    async def emit(_kind, _payload):
        return {}

    cases = [
        (
            "openrouter",
            "openrouter:z-ai/glm-5.3-flash",
            {"model": "z-ai/glm-5.3-flash", "use_openrouter": True},
        ),
        ("umans", "umans-coder", {"model": "umans-coder", "use_umans": True}),
        (
            "dream-claude",
            "dream:diffusiongemma-test",
            {"model": "diffusiongemma-test", "use_dream": True},
        ),
    ]
    for index, (harness, model, expected) in enumerate(cases):
        run = await db.create_agent_run(
            f"compat-child-{index}", conv["id"], "bounded task",
            requested_harness=harness, requested_model=model,
            project_dir=str(tmp_path),
        )
        result = await server._launch_agent_run(run, emit)
        assert result["harness"] == harness
        assert captured[-1]["requested_model"] == model
        for key, value in expected.items():
            assert captured[-1][key] == value


async def test_child_run_lifecycle_is_persisted_outside_conversation_tree(tmp_database):
    conv = await db.create_conversation("Supervisor test", mode="local")
    seen = []

    async def launcher(run, emit):
        await emit("stream_chunk", {"content": "child output"})
        return {"text": "child output", "harness": "local", "model": "local-test"}

    async def sink(run, event):
        seen.append((run["run_id"], event["event_kind"]))

    supervisor = AgentSupervisor(launcher=launcher, event_sink=sink)
    await supervisor.start()
    try:
        run = await supervisor.spawn(
            conv["id"], "inspect this", harness="local", model="local-test",
            project_dir=".", client_instance_id="window-a",
        )
        for _ in range(30):
            current = await db.get_agent_run(run["run_id"])
            if current["status"] in {"completed", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.02)
        current = await db.get_agent_run(run["run_id"])
        events = await db.list_agent_events(run["run_id"])
        assert current["status"] == "completed"
        assert current["client_instance_id"] == "window-a"
        assert [event["event_kind"] for event in events] == [
            "agent_started", "stream_chunk", "agent_completed"
        ]
        assert (run["run_id"], "stream_chunk") in seen
    finally:
        await supervisor.stop()


async def test_codex_child_uses_same_permission_handler_as_parent(tmp_path, monkeypatch):
    """A child must not fall back to plain HTTP against an HTTPS Loom host."""
    import server
    conv = await db.create_conversation("Child bridge fixture", mode="codex", project_dir=str(tmp_path))
    run = await db.create_agent_run("bridge-child", conv["id"], "bounded fixture",
                                   requested_harness="codex", requested_model="gpt-5.6-luna",
                                   project_dir=str(tmp_path))
    async def launch(*args, **kwargs):
        assert kwargs.get("permission_request_handler") is server.handle_cc_permission
        assert kwargs["permission_scope"] == "agent:bridge-child"
        class Proc:
            pid = None
            returncode = 0
        async def events():
            yield {"type": "result", "text": "done"}
        return Proc(), events()
    monkeypatch.setattr(server.codex_client, "run_codex", launch)
    async def emit(*args):
        return {}
    # Match production's dedicated supervisor task, including task-local IDs.
    assert not (await asyncio.create_task(server._launch_agent_run(run, emit)))["error"]


async def test_child_cancel_does_not_cancel_parent_task(tmp_database):
    conv = await db.create_conversation("Supervisor cancel", mode="local")
    parent = asyncio.create_task(asyncio.Event().wait())
    child_started = asyncio.Event()

    async def launcher(run, emit):
        await emit("status", {"text": "working"})
        child_started.set()
        await asyncio.Event().wait()
        return {"text": "done"}

    supervisor = AgentSupervisor(launcher=launcher)
    await supervisor.start()
    try:
        run = await supervisor.spawn(conv["id"], "long child", harness="local")
        await asyncio.wait_for(child_started.wait(), 5)
        await supervisor.cancel(run["run_id"])
        current = await db.get_agent_run(run["run_id"])
        assert current["status"] == "cancelled"
        assert not parent.done()
    finally:
        parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)
        await supervisor.stop()


async def test_restart_pauses_child_until_explicit_resume(tmp_database):
    conv = await db.create_conversation("Supervisor restart", mode="local")
    run = await db.create_agent_run(
        "restart-child", conv["id"], "safe to rerun", requested_harness="local"
    )
    await db.update_agent_run(run["run_id"], status="running", started_at=time.time())
    launched = asyncio.Event()

    async def launcher(_run, _emit):
        launched.set()
        return {"text": "resumed result"}

    supervisor = AgentSupervisor(launcher=launcher)
    await supervisor.start()
    try:
        await asyncio.sleep(0.05)
        paused = await db.get_agent_run(run["run_id"])
        assert paused["status"] == "paused"
        assert not launched.is_set()

        await supervisor.resume(run["run_id"])
        await asyncio.wait_for(launched.wait(), timeout=2)
        for _ in range(50):
            completed = await db.get_agent_run(run["run_id"])
            if completed["status"] == "completed":
                break
            await asyncio.sleep(0.02)
        assert completed["status"] == "completed"
    finally:
        await supervisor.stop()


async def test_overlapping_child_scopes_serialize_but_disjoint_scopes_parallelize(tmp_database):
    conv = await db.create_conversation("Supervisor scopes", mode="local")
    first_release = asyncio.Event()
    second_release = asyncio.Event()
    started: list[str] = []
    active = 0
    max_active = 0

    async def launcher(run, _emit):
        nonlocal active, max_active
        started.append(run["task"])
        active += 1
        max_active = max(max_active, active)
        try:
            if run["task"] == "first":
                await first_release.wait()
            elif run["task"] == "second":
                await second_release.wait()
        finally:
            active -= 1
        return {"text": run["task"]}

    supervisor = AgentSupervisor(launcher=launcher)
    await supervisor.start()
    try:
        await supervisor.spawn(
            conv["id"], "first", harness="local", project_dir="C:/workspace"
        )
        await supervisor.spawn(
            conv["id"], "second", harness="local", project_dir="C:/workspace"
        )
        for _ in range(50):
            if started:
                break
            await asyncio.sleep(0.02)
        assert started == ["first"]
        assert max_active == 1
        first_release.set()
        for _ in range(100):
            if "second" in started:
                break
            await asyncio.sleep(0.02)
        assert started == ["first", "second"]
        second_release.set()

        for _ in range(100):
            rows = await db.list_agent_runs(conv["id"])
            if all(row["status"] == "completed" for row in rows):
                break
            await asyncio.sleep(0.02)

        started.clear()
        active = 0
        max_active = 0
        release = asyncio.Event()

        async def parallel_launcher(run, _emit):
            nonlocal active, max_active
            started.append(run["task"])
            active += 1
            max_active = max(max_active, active)
            await release.wait()
            active -= 1
            return {"text": run["task"]}

        supervisor.launcher = parallel_launcher
        await supervisor.spawn(
            conv["id"], "third", harness="gemini", project_dir="C:/workspace",
            path_scope=["frontend"],
        )
        await supervisor.spawn(
            conv["id"], "fourth", harness="gemini", project_dir="C:/workspace",
            path_scope=["backend"],
        )
        for _ in range(100):
            if len(started) == 2:
                break
            await asyncio.sleep(0.02)
        assert set(started) == {"third", "fourth"}
        assert max_active == 2
        release.set()
    finally:
        first_release.set()
        second_release.set()
        await supervisor.stop()
