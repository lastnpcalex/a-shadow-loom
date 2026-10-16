"""Nested managed delegation: real scheduler/API, disposable DB, fake providers."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import database as db
import server
from agent_supervisor import AgentSupervisor, _scope_contains


async def wait_status(run_id, expected, timeout=4):
    async with asyncio.timeout(timeout):
        while True:
            row = await db.get_agent_run(run_id)
            if row["status"] in expected:
                return row
            await asyncio.sleep(0.01)


async def test_nested_waits_finish_at_one_leaf_slot_despite_earlier_unrelated_job(tmp_path):
    conv = await db.create_conversation("Nested delegation fixture")
    started = []
    alive = set()
    max_alive = 0
    jobs = {}

    async def launcher(run, emit):
        nonlocal max_alive
        name = run["task"]
        started.append(name)
        alive.add(name)
        max_alive = max(max_alive, len(alive))
        try:
            if name == "parent":
                # This unrelated overlapping FIFO job cannot block descendants.
                jobs["unrelated"] = await supervisor.spawn(
                    conv["id"], "unrelated", project_dir=str(tmp_path),
                )
            if name in {"parent", "child"}:
                child_name = "child" if name == "parent" else "grandchild"
                nested = await supervisor.spawn(
                    conv["id"], child_name, parent_run_id=f"agent:{run['run_id']}",
                )
                jobs[child_name] = nested
                assert nested["path_scope"] == ["src"]
                assert nested["client_instance_id"] == "origin-window"
                assert nested["requested_harness"] == "local"
                assert nested["requested_model"] == "fixture-model"
                done = await wait_status(nested["run_id"], {"completed", "failed"})
                assert done["status"] == "completed", done["error"]
            await emit("text_delta", {"text": name})
            return {"text": name}
        finally:
            alive.remove(name)

    supervisor = AgentSupervisor(launcher)
    supervisor.max_concurrent = 1
    await supervisor.start()
    try:
        root = await supervisor.spawn(
            conv["id"], "parent", harness="local", model="fixture-model",
            project_dir=str(tmp_path), path_scope=["src"], client_instance_id="origin-window",
        )
        done = await wait_status(root["run_id"], {"completed", "failed"})
        assert done["status"] == "completed", done["error"]
        await wait_status(jobs["unrelated"]["run_id"], {"completed"})
        assert started == ["parent", "child", "grandchild", "unrelated"]
        assert max_alive == 3  # two bounded coordinating ancestors, one leaf
        assert await db.get_conversation_tree(conv["id"]) == []
    finally:
        await supervisor.stop()


@pytest.mark.parametrize("disjoint", [False, True])
async def test_nested_siblings_keep_scope_and_leaf_limits(tmp_path, disjoint):
    conv = await db.create_conversation("Sibling fixture")
    ready, release = asyncio.Event(), asyncio.Event()
    started = []
    jobs = []

    async def launcher(run, emit):
        started.append(run["task"])
        if run["task"] == "parent":
            for index, scope in enumerate((["src/a", "src/b"] if disjoint else ["src/a", "src/a"])):
                jobs.append(await supervisor.spawn(
                    conv["id"], f"child-{index}", parent_run_id=run["run_id"], path_scope=[scope],
                ))
            ready.set()
            for child in jobs:
                await wait_status(child["run_id"], {"completed"})
        else:
            await release.wait()
        return {"text": "done"}

    supervisor = AgentSupervisor(launcher)
    supervisor.max_concurrent = 2
    await supervisor.start()
    try:
        root = await supervisor.spawn(conv["id"], "parent", project_dir=str(tmp_path), path_scope=["src"])
        await asyncio.wait_for(ready.wait(), 2)
        await wait_status(jobs[0]["run_id"], {"running"})
        if disjoint:
            await wait_status(jobs[1]["run_id"], {"running"})
            async with asyncio.timeout(2):
                while len(started) < 3:
                    await asyncio.sleep(0.01)
            assert set(started) == {"parent", "child-0", "child-1"}
        else:
            await asyncio.sleep(0.6)  # at least one additional dispatcher tick
            assert (await db.get_agent_run(jobs[1]["run_id"]))["status"] == "queued"
            assert started == ["parent", "child-0"]
        release.set()
        await wait_status(root["run_id"], {"completed"})
    finally:
        release.set()
        await supervisor.stop()


async def test_disjoint_descendant_still_obeys_one_leaf_budget(tmp_path):
    conv = await db.create_conversation("Leaf budget fixture")
    release, ready = asyncio.Event(), asyncio.Event()
    jobs = []

    async def launcher(run, emit):
        if run["task"] == "parent":
            for scope in ["a", "b"]:
                jobs.append(await supervisor.spawn(
                    conv["id"], scope, parent_run_id=run["run_id"], path_scope=[scope],
                ))
            ready.set()
            for job in jobs:
                await wait_status(job["run_id"], {"completed"})
        else:
            await release.wait()
        return {"text": "done"}

    supervisor = AgentSupervisor(launcher)
    supervisor.max_concurrent = 1
    await supervisor.start()
    try:
        root = await supervisor.spawn(conv["id"], "parent", project_dir=str(tmp_path))
        await asyncio.wait_for(ready.wait(), 2)
        await wait_status(jobs[0]["run_id"], {"running"})
        await asyncio.sleep(0.6)
        assert (await db.get_agent_run(jobs[1]["run_id"]))["status"] == "queued"
        release.set()
        await wait_status(root["run_id"], {"completed"})
    finally:
        release.set()
        await supervisor.stop()


async def test_cancelling_parent_does_not_cancel_child_or_release_child_scope(tmp_path):
    conv = await db.create_conversation("Durable descendant fixture")
    child_ready, release = asyncio.Event(), asyncio.Event()
    jobs = {}

    async def launcher(run, emit):
        if run["task"] == "parent":
            jobs["child"] = await supervisor.spawn(conv["id"], "child", parent_run_id=run["run_id"])
            await asyncio.Event().wait()
        elif run["task"] == "child":
            child_ready.set()
            await release.wait()
        return {"text": "done"}

    supervisor = AgentSupervisor(launcher)
    supervisor.max_concurrent = 1
    await supervisor.start()
    try:
        root = await supervisor.spawn(conv["id"], "parent", project_dir=str(tmp_path))
        await asyncio.wait_for(child_ready.wait(), 2)
        other = await supervisor.spawn(conv["id"], "unrelated", project_dir=str(tmp_path))
        await supervisor.cancel(root["run_id"])
        await asyncio.sleep(0.6)
        assert (await db.get_agent_run(root["run_id"]))["status"] == "cancelled"
        assert (await db.get_agent_run(jobs["child"]["run_id"]))["status"] == "running"
        assert (await db.get_agent_run(other["run_id"]))["status"] == "queued"
        release.set()
        await wait_status(other["run_id"], {"completed"})
    finally:
        release.set()
        await supervisor.stop()


async def test_unsafe_parent_teardown_cannot_be_bypassed_by_descendant(tmp_path):
    conv = await db.create_conversation("Unverified ancestor fixture")
    supervisor = AgentSupervisor()
    parent = await supervisor.spawn(conv["id"], "parent", project_dir=str(tmp_path), defer_start=True)
    await db.update_agent_run(parent["run_id"], status="running")
    child = await supervisor.spawn(conv["id"], "child", parent_run_id=parent["run_id"], defer_start=True)
    await db.update_agent_run(parent["run_id"], status="cancel_requested", cancel_requested=1)
    # Do not invoke restart recovery; exercise the occupied live dispatcher.
    loop = asyncio.create_task(supervisor._dispatch_loop())
    try:
        await asyncio.sleep(0.6)
        assert (await db.get_agent_run(child["run_id"]))["status"] == "queued"
        assert supervisor._runs == {}
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)


async def test_nested_identity_scope_and_depth_validation(tmp_path):
    conv = await db.create_conversation("Lineage fixture")
    other = await db.create_conversation("Foreign fixture")
    supervisor = AgentSupervisor()
    supervisor.max_depth = 1
    parent = await supervisor.spawn(conv["id"], "parent", project_dir=str(tmp_path), path_scope=["src"], defer_start=True)
    await db.update_agent_run(parent["run_id"], status="running")
    for changes, match in [
        ({"path_scope": ["."]}, "within"),
        ({"path_scope": ["src2"]}, "within"),
        ({"path_scope": ["../escape"]}, "escapes"),
        ({"project_dir": str(tmp_path / "different")}, "within"),
        ({"parent_run_id": "missing"}, "parent"),
        ({"conversation_id": other["id"]}, "conversation"),
    ]:
        kwargs = dict(conversation_id=conv["id"], task="invalid", parent_run_id=parent["run_id"], defer_start=True)
        kwargs.update(changes)
        with pytest.raises(ValueError, match=match):
            await supervisor.spawn(**kwargs)
    child = await supervisor.spawn(conv["id"], "valid", parent_run_id=parent["run_id"], path_scope=["src/nested"], defer_start=True)
    await db.update_agent_run(child["run_id"], status="running")
    with pytest.raises(ValueError, match="nesting limit"):
        await supervisor.spawn(conv["id"], "too deep", parent_run_id=child["run_id"])
    await db.update_agent_run(parent["run_id"], status="completed")
    with pytest.raises(ValueError, match="active child"):
        await supervisor.spawn(conv["id"], "stale caller", parent_run_id=parent["run_id"])
    assert len(await db.list_agent_runs(conv["id"])) == 2


async def test_ancestry_cycle_fails_closed():
    conv = await db.create_conversation("Cycle fixture")
    first = await db.create_agent_run("cycle-a", conv["id"], "a", parent_run_id="cycle-b")
    second = await db.create_agent_run("cycle-b", conv["id"], "b", parent_run_id="cycle-a")
    supervisor = AgentSupervisor()
    with pytest.raises(ValueError, match="cycle"):
        await supervisor._ancestors(first)
    loop = asyncio.create_task(supervisor._dispatch_loop())
    try:
        await wait_status(first["run_id"], {"failed"})
        await wait_status(second["run_id"], {"failed"})
        assert supervisor._runs == {}
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)


@pytest.mark.parametrize("mode,model", [
    ("local", "local-fixture"), ("codex", "codex-fixture"),
    ("goose", "openrouter:fixture/model"), ("dream-claude", "dream:fixture"),
])
async def test_api_nested_inherits_caller_not_changed_conversation(client, tmp_path, monkeypatch, mode, model):
    conv = await db.create_conversation("Mutable selection fixture", mode="claude", project_dir=str(tmp_path))
    supervisor = AgentSupervisor()
    monkeypatch.setattr(server, "_agent_supervisor", supervisor)
    parent = await supervisor.spawn(
        conv["id"], "parent", harness=mode, model=model,
        project_dir=str(tmp_path), path_scope=["src"], client_instance_id="origin", defer_start=True,
    )
    await db.update_agent_run(parent["run_id"], status="running", effective_harness=mode, effective_model=model)
    response = await client.post(f"/api/conversations/{conv['id']}/agent-runs", json={
        "task": "child", "parent_run_id": parent["run_id"],
    })
    assert response.status_code == 200, response.text
    child = response.json()
    assert child["requested_harness"] == mode
    assert child["requested_model"] == model
    assert child["path_scope"] == ["src"]
    assert child["client_instance_id"] == "origin"
    assert server._resolve_agent_target(child, {"mode": "gemini", "cc_model": "changed"}) == (mode, model)
    invalid = await client.post(f"/api/conversations/{conv['id']}/agent-runs", json={
        "task": "broaden", "parent_run_id": parent["run_id"], "path_scope": ["."],
    })
    assert invalid.status_code == 400


@pytest.mark.parametrize("model,expected_harness,route,provider_model", [
    ("local-fixture", "local", "use_llama", "local-fixture"),
    ("dream:local-fixture", "dream-claude", "use_dream", "local-fixture"),
])
async def test_local_child_keeps_tools_permissions_identity_and_nested_control(
    tmp_path, monkeypatch, model, expected_harness, route, provider_model,
):
    conv = await db.create_conversation("Local tools fixture", mode="local", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], local_model=model)
    calls = []

    async def run_claude(prompt, cwd, **kwargs):
        calls.append(kwargs)
        assert "bounded fixture task" in prompt
        assert cwd == str(tmp_path.resolve())
        assert asyncio.current_task()._agent_run_id == "local-child"
        async def stream():
            yield {"type": "session_info", "session_id": "fixture-session", "model": model}
            yield {"type": "tool_start", "tool_id": "t1", "tool_name": "Read"}
            yield {"type": "tool_input_delta", "tool_id": "t1", "delta": '{"file_path":"fixture.txt"}'}
            yield {"type": "tool_result", "tool_id": "t1", "content": "tool result"}
            yield {"type": "result", "text": "done", "session_id": "fixture-session"}
        return SimpleNamespace(pid=None, returncode=0), stream()

    def forbidden_chat(*args, **kwargs):
        pytest.fail("Local managed child must not use Weave/plain chat completion")

    monkeypatch.setattr(server.claude_client, "run_claude", run_claude)
    monkeypatch.setattr(server, "stream_chat", forbidden_chat)
    monkeypatch.setattr(server, "_handle_weave_generation", forbidden_chat)
    run = await db.create_agent_run("local-child", conv["id"], "bounded fixture task", project_dir=str(tmp_path))
    supervisor = AgentSupervisor(server._launch_agent_run)
    await supervisor._run_one(run)
    saved = await db.get_agent_run(run["run_id"])
    assert saved["status"] == "completed", saved["error"]
    assert saved["effective_harness"] == expected_harness
    assert saved["provider_session_id"] == "fixture-session"
    assert calls[0][route] is True
    assert calls[0]["model"] == provider_model
    assert calls[0]["conv_id"] == conv["id"]
    assert calls[0]["permission_mode"] == "default"
    assert calls[0]["permission_scope"] == "agent:local-child"
    assert calls[0]["extra_env"]["LOOM_AGENT_RUN_ID"] == "local-child"
    events = await db.list_agent_events(run["run_id"])
    assert {"tool_start", "tool_input_delta", "tool_result", "agent_completed"} <= {e["event_kind"] for e in events}


def test_local_profile_preserves_local_parent_model(monkeypatch):
    monkeypatch.setattr(server.config, "llama_model", "global-other-model")
    assert server._resolve_agent_target(
        {"model_profile": "local"}, {"mode": "local", "local_model": "parent-model"},
    ) == ("local", "parent-model")


def test_scope_containment_is_component_based():
    parent = {"project_dir": ".", "path_scope": ["src", "tests"]}
    assert _scope_contains(parent, {"project_dir": ".", "path_scope": ["src/nested", "tests/test_a.py"]})
    assert not _scope_contains(parent, {"project_dir": ".", "path_scope": ["src-other"]})


async def test_local_child_actual_cli_configuration_retains_tools_and_control(tmp_path, monkeypatch):
    """Run the real adapter; intercept only process creation and hook installation."""
    # This fixture has no native parent. Other tests can leave snapshots with
    # the same disposable conversation ID and gen:71 identifier.
    monkeypatch.setattr(server, '_generation_snapshots', {})
    monkeypatch.setattr(server, '_recent_generations', {})
    calls = []
    hook_calls = []
    import local_context_guard
    guard_type = local_context_guard.LocalContextGuard
    guards = []

    def context_guard(*args, **kwargs):
        guard = guard_type(*args, **kwargs)
        guards.append(guard)
        return guard

    monkeypatch.setattr(local_context_guard, "LocalContextGuard", context_guard)
    pass
    monkeypatch.setattr(server.claude_client, "_configure_permission_hook", lambda cwd: hook_calls.append(cwd))
    monkeypatch.setattr(server.config, "llama_host_url", lambda: "http://127.0.0.1:19999")

    async def create_subprocess(*args, **kwargs):
        calls.append((args, kwargs))
        stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
        stdout.feed_data(json.dumps({
            "type": "result", "subtype": "success", "result": "fixture result", "is_error": False,
            "session_id": "fixture-native-session",
        }).encode() + b"\n")
        stdout.feed_eof()
        stderr.feed_eof()
        return SimpleNamespace(pid=None, returncode=0, stdout=stdout, stderr=stderr,
                               stdin=None, wait=AsyncMock(return_value=0))

    monkeypatch.setattr(server.claude_client.asyncio, "create_subprocess_exec", create_subprocess)
    conv = await db.create_conversation("Local adapter fixture", mode="local", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], local_model="fixture-model.gguf")
    run = await db.create_agent_run(
        "local-cli-fixture", conv["id"], "bounded task", project_dir=str(tmp_path),
        client_instance_id="fixture-window", parent_run_id="gen:71",
    )
    await AgentSupervisor(server._launch_agent_run)._run_one(run)
    saved = await db.get_agent_run(run["run_id"])
    assert saved["status"] == "completed", saved["error"]
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "claude"
    blocked = args[args.index("--disallowedTools") + 1].split(",")
    assert not {"Read", "Write", "Edit", "Bash", "Agent", "Task"}.intersection(blocked)
    servers = json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]
    assert {"loom-agent-control", "loom-workspace", "web-tools"} <= servers.keys()
    control_env = servers["loom-agent-control"]["env"]
    assert control_env["LOOM_AGENT_RUN_ID"] == "agent:local-cli-fixture"
    assert control_env["LOOM_CONV_ID"] == str(conv["id"])
    assert control_env["LOOM_CLIENT_INSTANCE_ID"] == "fixture-window"
    assert servers["loom-workspace"]["env"]["LOOM_PERMISSION_SCOPE"] == "agent:local-cli-fixture"
    assert "Nested children inherit or narrow" in args[args.index("--append-system-prompt") + 1]
    env = kwargs["env"]
    assert guards[0].upstream == "http://127.0.0.1:19999"
    assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    assert env["ANTHROPIC_BASE_URL"] != guards[0].upstream
    assert env["ANTHROPIC_AUTH_TOKEN"] == guards[0].secret
    assert guards[0]._closed
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "fixture-model.gguf"
    assert env["LOOM_USE_LLAMA"] == "1"
    assert env["LOOM_PERMISSION_SCOPE"] == "agent:local-cli-fixture"
    assert env["LOOM_AGENT_RUN_ID"] == "local-cli-fixture"
    assert hook_calls == [str(tmp_path)]
