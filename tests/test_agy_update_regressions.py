import json
from pathlib import Path

import pytest

import gemini_client


@pytest.mark.parametrize("index", [0, 1, 2])
def test_agy_step_id_matches_hook_even_with_provider_tool_id(index):
    events = gemini_client._agy_step_events({"event": "step_update", "step_update": {
        "step_index": index, "state": "DONE",
        "tool_info": {"id": "view_file", "name": "view_file", "arguments": {"AbsolutePath": "C:/project/file.py"}},
    }}, {})
    tools = [event for event in events if event["type"] in {"tool_start", "tool_input_delta", "tool_result"}]
    assert [event["type"] for event in tools] == ["tool_start", "tool_input_delta", "tool_result"]
    assert all(event["tool_id"] == str(index) for event in tools)
    assert json.loads(tools[1]["json"]) == {"file_path": "C:/project/file.py"}


def test_agy_reasoning_uses_shared_text_contract():
    events = gemini_client._agy_step_events({"event": "step_update", "step_update": {"thought": "Reasoning summary"}}, {})
    assert {"type": "thinking_delta", "text": "Reasoning summary"} in events


@pytest.mark.parametrize("path", ["C:/Project/plan.md", "C:/Project/implementation_plan.md", "C:/Project/floorplan.md"])
def test_file_write_is_not_a_plan_approval_request(path):
    events = gemini_client._agy_step_events({"event": "step_update", "step_update": {
        "step_index": 0, "state": "DONE",
        "tool_info": {"name": "write_to_file", "arguments": {"TargetFile": path, "CodeContent": "Plan text"}},
    }}, {})
    assert not any(event["type"] == "plan_ready" for event in events)


def test_plan_notification_does_not_send_a_competing_turn():
    source = (Path(__file__).parents[1] / "static/chat.js").read_text(encoding="utf-8")
    handler = source.split("case 'plan_ready':", 1)[1].split("case 'permission_request':", 1)[0]
    assert "renderPlanReady(" not in handler
    assert "sendMessage(" not in handler


@pytest.mark.parametrize("slug", ["claude-sonnet-4-6", "claude-opus-4-6-thinking",
                                  "claude-sonnet-4-7", "gpt-oss-120b-high"])
def test_exact_discovered_selector_is_not_rewritten(slug):
    assert gemini_client._loom_model_to_agy("gemini:" + slug, "low") == slug


def test_discovery_validates_rows_and_preserves_cache_on_failure(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    import server

    monkeypatch.setenv("EXO_STATE_DIR", str(tmp_path))
    output = "Fetching available models...\nslug name\ngemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
    output += "gemini-3.8-flash-high\tDuplicate\nclaude-sonnet-4-6 (Claude Sonnet 4.6 (Thinking))\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=output))
    models = gemini_client.discover_agy_models()
    assert len(models) == 2
    assert server.load_local_gemini_models() == models
    cache = gemini_client.agy_models_cache_path()
    assert cache.is_relative_to(tmp_path)
    before = cache.read_bytes()
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1, stdout=""))
    assert gemini_client.discover_agy_models() == []
    assert cache.read_bytes() == before


async def test_agy_refresh_needs_no_anthropic_login_and_runs_off_event_loop(client, monkeypatch):
    import threading
    import server

    loop_thread = threading.get_ident()
    models = [{"value": "gemini:gemini-3.8-flash-high", "label": "Gemini 3.8 Flash (High)"}]
    def discover():
        assert threading.get_ident() != loop_thread
        return models
    monkeypatch.setattr(server, "_read_oauth_token", lambda: pytest.fail("Unexpected Claude login read"))
    monkeypatch.setattr(server, "CC_MODELS", [{"group": "Antigravity", "models": []}])
    monkeypatch.setattr(gemini_client, "discover_agy_models", discover)
    response = await client.post('/api/cc-models/refresh?provider=gemini')
    assert response.status_code == 200
    assert server.CC_MODELS[0]["models"] == models
    monkeypatch.setattr(gemini_client, "discover_agy_models", lambda: [])
    assert (await client.post('/api/cc-models/refresh?provider=gemini')).status_code == 502
    assert server.CC_MODELS[0]["models"] == models


@pytest.mark.parametrize("tool,key", [("find_by_name", "SearchDirectory"), ("list_dir", "DirectoryPath")])
def test_agy_directory_reads_preserve_permission_boundaries(tmp_path, tool, key):
    from permission_policy import normalize_permission_request, ActionBoundary, ActionEffect
    import cc_permission_hook

    for path, boundary in [(tmp_path / "inside", ActionBoundary.WORKSPACE),
                           (tmp_path.parent / "outside", ActionBoundary.OUTSIDE_WORKSPACE)]:
        action = normalize_permission_request(
            tool_name=cc_permission_hook.normalize_tool_name(tool),
            tool_input=cc_permission_hook.normalize_tool_args(tool, {key: str(path)}),
            workspace_root=str(tmp_path),
        )
        assert action.effect is ActionEffect.READ
        assert action.boundary is boundary
