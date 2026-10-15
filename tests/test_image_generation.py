"""Image jobs must produce verified artifacts through the managed AGY route."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
import pytest

import database as db
import image_generation as images
from agent_supervisor import AgentSupervisor


def write_image(path, image_format="PNG"):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 12), "teal").save(path, format=image_format)


@pytest.mark.parametrize("path", [
    "../escape.png", "sub/../../escape.png", "C:/escape.png", "/escape.png",
    "file.png:stream", "file.svg", "", None,
])
def test_invalid_output_never_creates_files(tmp_path, path):
    with pytest.raises(ValueError):
        images.prepare_image_request(tmp_path, {"prompt": "aurora", "output_path": path})
    assert not (tmp_path / "sub").exists()


def test_existing_file_and_invalid_reference_are_rejected(tmp_path):
    output = tmp_path / "existing.png"
    output.write_bytes(b"preserve this")
    with pytest.raises(ValueError, match="already exists"):
        images.prepare_image_request(tmp_path, {"prompt": "aurora", "output_path": output.name})
    with pytest.raises(ValueError, match="Invalid raster"):
        images.prepare_image_request(tmp_path, {
            "prompt": "aurora", "output_path": "new.png", "reference_paths": [output.name],
        })
    assert output.read_bytes() == b"preserve this"


def test_junction_or_symlink_escape_is_rejected(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    try:
        (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation unavailable on this machine")
    with pytest.raises(ValueError, match="escapes"):
        images.prepare_image_request(tmp_path, {"prompt": "aurora", "output_path": "link/out.png"})


def test_encoding_and_truncation_are_checked(tmp_path):
    path = tmp_path / "wrong.png"
    write_image(path, "JPEG")
    with pytest.raises(ValueError, match="encoding"):
        images.inspect_image(path)
    write_image(path)
    path.write_bytes(path.read_bytes()[:35])
    with pytest.raises(ValueError, match="Invalid raster"):
        images.inspect_image(path)


def test_image_bounds_and_reference_types(tmp_path, monkeypatch):
    path = tmp_path / "image.png"
    write_image(path)
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", 10)
    with pytest.raises(ValueError, match="32 MiB"):
        images.inspect_image(path)
    for references in ({}, "", False, ["image.png"] * 5):
        with pytest.raises(ValueError, match="reference_paths"):
            images.prepare_image_request(tmp_path, {
                "prompt": "aurora", "output_path": "new.png", "reference_paths": references,
            })


def test_reference_request_and_native_evidence(tmp_path):
    write_image(tmp_path / "reference.png")
    request = images.prepare_image_request(tmp_path, {
        "prompt": "Use reference for colors", "output_path": "art/output.png",
        "reference_paths": ["reference.png"],
    })
    assert images.image_scope(request) == ["art", "reference.png"]
    evidence = images.NativeImageEvidence()
    evidence.observe({"type": "text_delta", "text": "generate_image succeeded"})
    evidence.observe({"type": "tool_start", "name": "mcp.generate_image", "tool_id": "1"})
    evidence.observe({"type": "tool_result", "tool_id": "1", "is_error": False})
    assert not evidence.succeeded
    evidence.observe({"type": "tool_start", "name": "generate_image", "tool_id": "2"})
    evidence.observe({"type": "tool_result", "tool_id": "2", "is_error": True})
    assert not evidence.succeeded
    evidence.observe({"type": "tool_start", "name": "generate_image", "tool_id": "3"})
    evidence.observe({"type": "tool_result", "tool_id": "3", "is_error": False})
    assert evidence.succeeded


async def test_mcp_tool_preserves_caller_context_and_queues_explicit_image_request(monkeypatch):
    import mcp_loom_agents as mcp
    monkeypatch.setenv("LOOM_CONV_ID", "42")
    monkeypatch.setenv("LOOM_WORKSPACE_ROOT", "C:/workspace")
    monkeypatch.setenv("LOOM_AGENT_RUN_ID", "parent-child")
    monkeypatch.setenv("LOOM_CLIENT_INSTANCE_ID", "browser-a")
    captured = []

    def post(port, path, payload):
        captured.append((path, payload))
        return {"run_id": "image-run", "status": "queued", "image_request": payload["image_request"]}

    monkeypatch.setattr(mcp, "_post", post)
    result = json.loads(mcp.generate_image("aurora", "art/new.png"))
    assert result["run_id"] == "image-run"
    assert result["status"] == "queued"
    path, payload = captured[0]
    assert path == "/api/conversations/42/agent-runs"
    assert payload["parent_run_id"] == "parent-child"
    assert payload["client_instance_id"] == "browser-a"
    assert payload["project_dir"] == "C:/workspace"
    assert payload["image_request"]["output_path"] == "art/new.png"
    assert "generate_image" in {tool.name for tool in await mcp.mcp.list_tools()}


async def queued_image(monkeypatch, tmp_path):
    import server
    supervisor = AgentSupervisor(server._launch_agent_run)
    monkeypatch.setattr(server, "_agent_supervisor", supervisor)
    conv = await db.create_conversation("Images", mode="codex", project_dir=str(tmp_path))
    run = await server.api_spawn_agent_run(conv["id"], {
        "image_request": {"prompt": "aurora", "output_path": "art/new.png"},
    })
    return supervisor, conv, run


@pytest.mark.parametrize("outcome", ["success", "missing", "corrupt", "prose", "tool_error", "provider_error"])
async def test_image_job_completion_requires_native_call_and_actual_image(monkeypatch, tmp_path, outcome):
    import server
    supervisor, conv, run = await queued_image(monkeypatch, tmp_path)
    assert run["requested_harness"] == "gemini"
    assert run["requested_model"] == images.DEFAULT_AGENT_MODEL
    assert run["image_request"]["output_path"] == "art/new.png"
    assert run["path_scope"] == ["art"]
    calls = []

    async def launch(prompt, cwd, **kwargs):
        calls.append(kwargs)
        assert "not a repository investigation" in prompt
        assert kwargs["permission_scope"] == f"agent:{run['run_id']}"
        assert kwargs["agent_run_id"] == run["run_id"]
        assert Path(cwd) == tmp_path

        async def stream():
            if outcome != "prose":
                yield {"type": "tool_start", "name": "generate_image", "tool_id": "8"}
                yield {"type": "tool_result", "tool_id": "8", "is_error": outcome == "tool_error"}
            path = tmp_path / "art/new.png"
            if outcome != "missing":
                write_image(path)
            if outcome == "corrupt":
                path.write_bytes(b"not a raster")
            yield {"type": "result", "text": "Image generated successfully",
                   "is_error": outcome == "provider_error", "error": "provider failed" if outcome == "provider_error" else None}

        return SimpleNamespace(pid=None, returncode=0), stream()

    monkeypatch.setattr(server.gemini_client, "run_gemini", launch)
    await supervisor._run_one(run)
    saved = await db.get_agent_run(run["run_id"])
    assert len(calls) == 1
    if outcome == "success":
        assert saved["status"] == "completed", saved["error"]
        artifact = saved["result"]["artifacts"][0]
        assert artifact["path"] == "art/new.png"
        assert (artifact["width"], artifact["height"]) == (16, 12)
        assert len(artifact["sha256"]) == 64
        assert artifact["url"] == f"/api/conversations/{conv['id']}/file?path=art%2Fnew.png"
    else:
        assert saved["status"] == "failed"
        assert saved["error"]
        assert "artifacts" not in saved["result"]


async def test_output_created_while_queued_is_preserved(monkeypatch, tmp_path):
    import server
    supervisor, conv, run = await queued_image(monkeypatch, tmp_path)
    write_image(tmp_path / "art/new.png")
    original = (tmp_path / "art/new.png").read_bytes()
    launch = AsyncMock()
    monkeypatch.setattr(server.gemini_client, "run_gemini", launch)
    await supervisor._run_one(run)
    saved = await db.get_agent_run(run["run_id"])
    assert saved["status"] == "failed"
    assert "already exists" in saved["error"]
    assert (tmp_path / "art/new.png").read_bytes() == original
    launch.assert_not_called()


async def test_nested_image_request_cannot_broaden_parent_scope(monkeypatch, tmp_path):
    import server
    from fastapi import HTTPException
    supervisor, conv, _run = await queued_image(monkeypatch, tmp_path)
    parent = await supervisor.spawn(conv["id"], "parent", project_dir=str(tmp_path),
                                    path_scope=["allowed"], defer_start=True)
    await db.update_agent_run(parent["run_id"], status="running")
    with pytest.raises(HTTPException, match="within"):
        await server.api_spawn_agent_run(conv["id"], {
            "parent_run_id": parent["run_id"],
            "image_request": {"prompt": "aurora", "output_path": "elsewhere/new.png"},
        })


@pytest.mark.parametrize("stop", ["timeout", "cancel"])
async def test_image_run_stops_and_drains_launcher(monkeypatch, tmp_path, stop):
    supervisor, conv, run = await queued_image(monkeypatch, tmp_path)
    entered = asyncio.Event()
    drained = asyncio.Event()

    async def launch(run, emit):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    supervisor.launcher = launch
    monkeypatch.setattr(images, "IMAGE_RUN_TIMEOUT_SEC", 0.02 if stop == "timeout" else 60)
    task = asyncio.create_task(supervisor._run_one(run))
    await entered.wait()
    if stop == "cancel":
        task.cancel()
    await task
    saved = await db.get_agent_run(run["run_id"])
    assert drained.is_set()
    assert saved["status"] == ("failed" if stop == "timeout" else "cancelled")
    if stop == "timeout":
        assert "run limit" in saved["error"]


async def test_image_metadata_survives_restart_pause(tmp_path):
    conv = await db.create_conversation("Images", mode="claude", project_dir=str(tmp_path))
    request = {"prompt": "aurora", "output_path": "art/new.png", "reference_paths": []}
    run = await db.create_agent_run("paused-image", conv["id"], images.image_task(request),
                                    image_request=request, project_dir=str(tmp_path))
    await db.update_agent_run(run["run_id"], status="running")
    supervisor = AgentSupervisor()
    await supervisor.start()
    try:
        saved = await db.get_agent_run(run["run_id"])
        assert saved["status"] == "paused"
        assert saved["image_request"] == request
    finally:
        await supervisor.stop()


async def test_migration_preserves_existing_child_runs(tmp_path):
    conv = await db.create_conversation("Existing", mode="claude", project_dir=str(tmp_path))
    await db.create_agent_run("existing-child", conv["id"], "preserve this task")
    connection = await db.get_db()
    # Reproduce the previous schema in the fixture database only.
    await connection.execute("ALTER TABLE agent_runs DROP COLUMN image_request")
    await connection.commit()
    await db.init_db()
    saved = await db.get_agent_run("existing-child")
    assert saved["task"] == "preserve this task"
    assert saved["image_request"] is None
    request = {"prompt": "aurora", "output_path": "art/new.png", "reference_paths": []}
    created = await db.create_agent_run("new-image", conv["id"], images.image_task(request),
                                       image_request=request)
    assert created["image_request"] == request
