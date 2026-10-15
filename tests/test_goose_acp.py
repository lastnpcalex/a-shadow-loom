"""Goose ACP integration invariants."""

import asyncio
import json
from contextlib import closing

import pytest


async def _create_goose_adapter_test_conversation(db, title, **kwargs):
    """Direct adapter tests opt out of the production serial-lane wrapper."""
    conv = await db.create_conversation(title, **kwargs)
    await db.update_conversation_fields(conv["id"], parallel_agents_enabled=1)
    return await db.get_conversation(conv["id"])


async def test_goose_launch_inherits_page_and_scope_from_generation_task(monkeypatch, tmp_path):
    import goose_client
    import replay_budget
    from unittest.mock import AsyncMock
    monkeypatch.setattr(goose_client, "_goose_env", lambda *args: {})
    monkeypatch.setattr(replay_budget, "check_fresh_payload", AsyncMock())
    captured = {}
    class CapturedLaunch(Exception):
        pass
    async def launch(*args, **kwargs):
        captured.update(kwargs["env"])
        raise CapturedLaunch()
    monkeypatch.setattr(goose_client.asyncio, "create_subprocess_exec", launch)
    async def generation():
        task = asyncio.current_task()
        task._gen_key = (42, 1, 9)
        task._client_instance_id = "fixture-page"
        with pytest.raises(CapturedLaunch):
            await goose_client.run_goose("fixture", cwd=str(tmp_path), goose_exe="goose", conv_id=42)
    await asyncio.create_task(generation())
    assert captured["LOOM_CLIENT_INSTANCE_ID"] == "fixture-page"
    assert captured["LOOM_PERMISSION_SCOPE"] == "gen:9"
    assert captured["LOOM_PARENT_RUN_ID"] == "gen:9"


async def test_goose_permission_bridge_forwards_generation_scope(monkeypatch):
    import goose_client

    captured = {}

    class FakeResponse:
        def json(self):
            return {"allow": True, "feedback": "Proceed with the reviewed path."}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, *, json, timeout):
            captured.update(json)
            return FakeResponse()

    class FakeRpc:
        async def respond(self, req_id, *, result):
            captured["reply"] = result

    monkeypatch.setattr(goose_client.httpx, "AsyncClient", FakeClient)
    await goose_client._bridge_permission(
        FakeRpc(),
        9,
        {
            "toolCall": {"title": "shell"},
            "options": [{"id": "allow_once", "label": "Allow once"}],
        },
        7,
        3000,
        "gen:41",
    )

    assert captured["permission_scope"] == "gen:41"
    assert captured["source"] == "goose_acp"
    assert captured["reply"]["outcome"]["optionId"] == "allow_once"


@pytest.mark.parametrize(
    ("decision_source", "expected_outcome"),
    [
        ("user", "selected"),
        ("loom_policy", "cancelled"),
        ("loom_lifecycle", "cancelled"),
        ("loom_timeout", "cancelled"),
    ],
)
async def test_goose_permission_bridge_only_maps_user_deny_to_reject(
    monkeypatch, decision_source, expected_outcome
):
    import goose_client

    captured = {}

    class FakeResponse:
        def json(self):
            return {
                "allow": False,
                "decision_source": decision_source,
                "message": "Permission did not run.",
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            captured["timeout"] = kwargs["timeout"]
            return FakeResponse()

    class FakeRpc:
        async def respond(self, req_id, *, result):
            captured["reply"] = result

    monkeypatch.setattr(goose_client.httpx, "AsyncClient", FakeClient)
    await goose_client._bridge_permission(
        FakeRpc(),
        10,
        {
            "toolCall": {"title": "shell"},
            "options": [{"id": "reject_once", "label": "Reject once"}],
        },
        7,
        3000,
        "gen:42",
    )

    assert captured["timeout"] > 24 * 60 * 60
    assert captured["reply"]["outcome"]["outcome"] == expected_outcome
    if decision_source == "user":
        assert captured["reply"]["outcome"]["optionId"] == "reject_once"


async def test_goose_permission_bridge_failure_is_cancelled_not_user_reject(
    monkeypatch,
):
    import goose_client

    captured = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            raise TimeoutError("bridge stalled")

    class FakeRpc:
        async def respond(self, req_id, *, result):
            captured["reply"] = result

    monkeypatch.setattr(goose_client.httpx, "AsyncClient", FakeClient)
    await goose_client._bridge_permission(
        FakeRpc(),
        11,
        {
            "toolCall": {"title": "shell"},
            "options": [{"id": "reject_once", "label": "Reject once"}],
        },
        7,
        3000,
    )

    assert captured["reply"] == {"outcome": {"outcome": "cancelled"}}


def test_goose_model_parser_and_env_openrouter(monkeypatch):
    import goose_client

    monkeypatch.setenv("GOOSE_FAST_MODEL", "google/gemini-2.5-flash")
    monkeypatch.delenv("LOOM_GOOSE_FAST_MODEL", raising=False)
    monkeypatch.setattr("openrouter_client.api_key", lambda: "sk-or-test")
    monkeypatch.setattr("openrouter_client.base_url", lambda: "https://openrouter.ai/api/v1")

    provider, model = goose_client.split_goose_model("goose:openrouter:z-ai/glm-5.2")
    assert provider == "openrouter"
    assert model == "z-ai/glm-5.2"

    env = goose_client._goose_env("goose:openrouter:z-ai/glm-5.2", "default")
    assert env["GOOSE_PROVIDER"] == "openrouter"
    assert env["GOOSE_MODEL"] == "z-ai/glm-5.2"
    assert env["GOOSE_FAST_MODEL"] == "z-ai/glm-5.3-flash"
    assert env["GOOSE_MODE"] == "approve"
    assert env["OPENROUTER_API_KEY"] == "sk-or-test"
    assert env["OPENROUTER_HOST"] == "https://openrouter.ai"
    assert env["GOOSE_PROVIDER__TYPE"] == "openrouter"
    assert env["GOOSE_PROVIDER__API_KEY"] == "sk-or-test"
    assert env["GOOSE_PROVIDER__HOST"] == "https://openrouter.ai"


def test_goose_glm_53_flash_uses_exact_openrouter_slug(monkeypatch):
    import goose_client

    monkeypatch.setattr("openrouter_client.api_key", lambda: "sk-or-test")
    monkeypatch.setattr(
        "openrouter_client.base_url", lambda: "https://openrouter.ai/api/v1"
    )

    selector = "goose:openrouter:z-ai/glm-5.3-flash"
    provider, model = goose_client.split_goose_model(selector)
    env = goose_client._goose_env(selector, "default")

    assert (provider, model) == ("openrouter", "z-ai/glm-5.3-flash")
    assert env["GOOSE_PROVIDER"] == "openrouter"
    assert env["GOOSE_MODEL"] == "z-ai/glm-5.3-flash"


def test_goose_openrouter_fast_model_allows_loom_override(monkeypatch):
    import goose_client

    monkeypatch.setenv("LOOM_GOOSE_FAST_MODEL", "moonshotai/kimi-k2.7-code")
    monkeypatch.setattr("openrouter_client.api_key", lambda: "sk-or-test")
    monkeypatch.setattr(
        "openrouter_client.base_url", lambda: "https://openrouter.ai/api/v1"
    )

    env = goose_client._goose_env(
        "goose:openrouter:deepseek/deepseek-v4-flash-0731", "approve"
    )

    assert env["GOOSE_MODEL"] == "deepseek/deepseek-v4-flash-0731"
    assert env["GOOSE_FAST_MODEL"] == "moonshotai/kimi-k2.7-code"


def test_goose_env_openrouter_merges_reasoning_effort(monkeypatch):
    import goose_client

    monkeypatch.setenv("OPENROUTER_PARAMETERS", '{"plugins":[{"id":"web"}]}')
    monkeypatch.setattr("openrouter_client.api_key", lambda: "sk-or-test")
    monkeypatch.setattr("openrouter_client.base_url", lambda: "https://openrouter.ai/api/v1")

    env = goose_client._goose_env("goose:openrouter:deepseek/deepseek-v4-flash-0731", "approve", "max")
    params = json.loads(env["OPENROUTER_PARAMETERS"])

    assert params["plugins"] == [{"id": "web"}]
    assert params["reasoning"] == {"effort": "max", "enabled": True}


def test_goose_prompt_blocks_include_native_images(tmp_path):
    import base64
    import goose_client

    image = tmp_path / "sample.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nsample")

    blocks = goose_client._prompt_blocks("inspect this", [image])

    assert blocks[0] == {"type": "text", "text": "inspect this"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["mimeType"] == "image/png"
    assert blocks[1]["data"] == base64.b64encode(image.read_bytes()).decode("ascii")
    assert blocks[1]["uri"].startswith("file:///")


def test_goose_windows_app_path_resolves_to_embedded_cli(monkeypatch, tmp_path):
    import goose_client

    install = tmp_path / "Goose"
    app_exe = install / "Goose.exe"
    cli_exe = install / "resources" / "bin" / "goose.exe"
    cli_exe.parent.mkdir(parents=True)
    app_exe.write_text("", encoding="utf-8")
    cli_exe.write_text("", encoding="utf-8")

    monkeypatch.setattr(goose_client.sys, "platform", "win32")
    monkeypatch.setenv("GOOSE_EXE", str(app_exe))

    assert goose_client.default_goose_exe() == str(cli_exe)
    assert goose_client._goose_command(str(app_exe)) == [str(cli_exe)]


def test_goose_acp_work_dir_is_absolute(monkeypatch, tmp_path):
    import goose_client

    monkeypatch.chdir(tmp_path)
    assert goose_client._goose_work_dir(".") == str(tmp_path.resolve())


def test_goose_auto_selector_sets_mode_without_changing_provider(monkeypatch):
    import goose_client

    monkeypatch.delenv("LOOM_GOOSE_FAST_MODEL", raising=False)
    monkeypatch.setattr("openrouter_client.api_key", lambda: "sk-or-test")
    monkeypatch.setattr("openrouter_client.base_url", lambda: "https://openrouter.ai/api/v1")

    provider, model = goose_client.split_goose_model("goose:auto:openrouter:z-ai/glm-5.2")
    assert provider == "openrouter"
    assert model == "z-ai/glm-5.2"
    assert goose_client.permission_mode_for_model("goose:auto:openrouter:z-ai/glm-5.2", "approve") == "smart_approve"

    env = goose_client._goose_env("goose:auto:openrouter:z-ai/glm-5.2", "smart_approve")
    assert env["GOOSE_PROVIDER"] == "openrouter"
    assert env["GOOSE_MODEL"] == "z-ai/glm-5.2"
    assert env["GOOSE_FAST_MODEL"] == "z-ai/glm-5.3-flash"
    assert env["GOOSE_MODE"] == "approve"  # Shared Loom policy owns decisions.
    assert env["OPENROUTER_API_KEY"] == "sk-or-test"
    assert env["OPENROUTER_HOST"] == "https://openrouter.ai"
    assert env["GOOSE_PROVIDER__TYPE"] == "openrouter"
    assert env["GOOSE_PROVIDER__API_KEY"] == "sk-or-test"


def test_goose_model_parser_and_env_dream(monkeypatch):
    import goose_client

    class Cfg:
        dream_host = "http://localhost:8787"
        dream_context_size = 131072

    monkeypatch.setattr("config.config", Cfg())
    env = goose_client._goose_env("goose:dream:diffusiongemma-test", "chat")
    assert env["GOOSE_PROVIDER"] == "openai"
    assert env["GOOSE_MODEL"] == "diffusiongemma-test"
    assert env["GOOSE_MODE"] == "chat"
    assert env["OPENAI_HOST"] == "http://127.0.0.1:8787"
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:8787"
    assert env["OPENAI_API_KEY"] == "loom-local"
    assert env["OPENAI_BASE_PATH"] == "v1/chat/completions"
    assert env["GOOSE_PROVIDER__TYPE"] == "openai"
    assert env["GOOSE_PROVIDER__HOST"] == "http://127.0.0.1:8787"
    assert env["GOOSE_PROVIDER__API_KEY"] == "loom-local"
    assert env["GOOSE_CONTEXT_LIMIT"] == "131072"


def test_goose_dispatch_normalizes_acp_updates():
    import goose_client

    state = {}
    assert goose_client.dispatch_session_update(
        {"sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}},
        state,
    ) == [{"type": "text_delta", "text": "hi"}]

    events = goose_client.dispatch_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "t1",
            "title": "Read",
            "content": {"path": "README.md"},
        },
        state,
    )
    assert events[0] == {"type": "tool_start", "name": "Read", "tool_id": "t1"}
    assert events[1]["type"] == "tool_input_delta"

    events = goose_client.dispatch_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "t1",
            "status": "completed",
            "content": {"text": "done"},
        },
        state,
    )
    assert events == [{"type": "tool_result", "content": "done", "tool_id": "t1", "is_error": False, "outcome_recorded": True}]


def test_goose_terminal_error_envelope_is_separated_from_partial_output():
    import goose_client

    raw = (
        "Useful partial work."
        "Ran into this error: Server error: Upstream idle timeout exceeded.\n\n"
        "Please retry if you think this is a transient or recoverable error."
    )

    partial, detail = goose_client.split_goose_terminal_failure(raw)

    assert partial == "Useful partial work."
    assert detail == "Server error: Upstream idle timeout exceeded."
    assert goose_client.is_retryable_goose_failure(detail) is True
    assert goose_client.split_goose_terminal_failure(
        "I ran into this error while debugging, but fixed it."
    ) == ("I ran into this error while debugging, but fixed it.", "")


def test_goose_native_compaction_error_envelope_is_not_an_answer():
    import goose_client
    from provider_contract import context_exhausted
    detail = ('Request failed: Bad request (400): Loom local context admission failed: '
              'Local request still needs 90000 input tokens; budget is 80000. '
              'Current instructions/user input/tool calls were preserved. No inference request was sent..')
    raw = 'Ran into this error trying to compact: ' + detail + '\n\nPlease try again or create a new session'
    assert goose_client.split_goose_terminal_failure(raw) == ('', detail)
    assert context_exhausted(detail)
    assert goose_client.split_goose_terminal_failure('Saved artifact.\n' + raw) == ('Saved artifact.', detail)
    ordinary = 'I ran into this error trying to compact: fixed it.'
    assert goose_client.split_goose_terminal_failure(ordinary) == (ordinary, '')


@pytest.mark.asyncio
async def test_goose_progress_reader_fails_boundedly():
    import goose_client

    class SilentStream:
        async def readline(self):
            await asyncio.sleep(1)
            return b""

    with pytest.raises(asyncio.TimeoutError, match="no ACP activity.*generation"):
        await goose_client._readline_with_progress_timeout(
            SilentStream(), 0.01, phase="generation"
        )


@pytest.mark.asyncio
async def test_goose_acp_promotes_success_shaped_error_footer(monkeypatch, tmp_path):
    import goose_client

    class FakeStdin:
        def __init__(self):
            self.closed = False

        def write(self, data):
            pass

        async def drain(self):
            pass

        def is_closing(self):
            return self.closed

        def close(self):
            self.closed = True

    class FakeStdout:
        def __init__(self):
            self.lines = [
                b'{"jsonrpc":"2.0","id":1,"result":{"agentInfo":{"name":"Goose"}}}\n',
                b'{"jsonrpc":"2.0","id":2,"result":{"sessionId":"ams-acp"}}\n',
                (
                    json.dumps({
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {"update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"text": (
                                "partial workRan into this error: Server error: "
                                "Upstream idle timeout exceeded.\n\nPlease retry if you think "
                                "this is a transient or recoverable error."
                            )},
                        }},
                    }) + "\n"
                ).encode("utf-8"),
                b'{"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn"}}\n',
            ]

        async def readline(self):
            return self.lines.pop(0) if self.lines else b""

    class EmptyStderr:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class FakeProc:
        pid = 4490
        returncode = 0
        stdin = FakeStdin()
        stdout = FakeStdout()
        stderr = EmptyStderr()

        async def wait(self):
            return 0

    async def fake_subprocess(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)

    _proc, stream = await goose_client.run_goose(
        "do work",
        conv_id=1,
        model="goose:openrouter:z-ai/glm-5.3-flash",
        cwd=str(tmp_path),
        goose_exe="goose",
        progress_timeout_seconds=1,
    )
    events = [event async for event in stream]

    failure = next(event for event in events if event["type"] == "error")
    result = next(event for event in events if event["type"] == "result")
    assert failure["source"] == "goose_terminal_error_envelope"
    assert failure["retryable"] is True
    assert "Upstream idle timeout exceeded" in failure["error"]
    assert result["stop_reason"] == "error"


@pytest.mark.asyncio
async def test_goose_rpc_timeout_names_method():
    import asyncio
    import goose_client

    class Stdin:
        def write(self, data):
            pass

        async def drain(self):
            pass

    class Stdout:
        async def readline(self):
            await asyncio.sleep(1)
            return b""

    class Proc:
        stdin = Stdin()
        stdout = Stdout()

    proc = Proc()
    rpc = goose_client._RpcConn(proc)

    with pytest.raises(asyncio.TimeoutError, match="goose initialize timed out after"):
        await goose_client._rpc_request_via_reader(rpc, "initialize", {}, proc, {}, timeout=0.01)


def test_server_goose_models_are_in_main_picker():
    import server

    groups = {group["group"]: group["models"] for group in server.CC_MODELS}
    assert "Goose ACP" in groups
    values = {m["value"] for m in groups["Goose ACP"]}
    assert "goose:openrouter:z-ai/glm-5.3-flash" in values
    assert "goose:openrouter:z-ai/glm-5.2" in values
    assert any(v.startswith("goose:dream:") for v in values)
    auto_values = {m["value"] for m in groups["Goose ACP - Smart/Subagents"]}
    assert "goose:auto:openrouter:z-ai/glm-5.3-flash" in auto_values
    assert "goose:auto:openrouter:z-ai/glm-5.2" in auto_values
    assert any(v.startswith("goose:auto:dream:") for v in auto_values)
    assert server._mode_for_cc_model("goose:openrouter:z-ai/glm-5.2") == "goose"
    assert server._mode_for_cc_model("goose:auto:openrouter:z-ai/glm-5.2") == "goose"
    assert server._mode_for_cc_model("goose:openrouter:z-ai/glm-5.3-flash") == "goose"


def test_goose_model_context_unwraps_provider_thresholds():
    import model_context

    assert model_context.handoff_threshold("goose:openrouter:z-ai/glm-5.3-flash") == model_context.THRESHOLD_OPENROUTER_GLM_53_FLASH
    assert model_context.handoff_threshold("goose:openrouter:z-ai/glm-5.2") == model_context.THRESHOLD_OPENROUTER_GLM_52
    assert model_context.handoff_threshold("goose:auto:openrouter:moonshotai/kimi-k2.7-code") == model_context.THRESHOLD_OPENROUTER_KIMI_K27_CODE
    assert not model_context.needs_handoff("goose:openrouter:z-ai/glm-5.2", 200_000)
    assert model_context.needs_handoff("goose:auto:openrouter:moonshotai/kimi-k2.7-code", 300_000)


def test_goose_selector_wraps_openrouter_and_local_models():
    import server

    assert (
        server._goose_selector_for_model("openrouter:moonshotai/kimi-k2.7-code")
        == "goose:openrouter:moonshotai/kimi-k2.7-code"
    )
    assert (
        server._goose_selector_for_model("moonshotai/kimi-k2.7-code")
        == "goose:openrouter:moonshotai/kimi-k2.7-code"
    )
    assert (
        server._goose_selector_for_model("Qwen3.8-27B-NVFP4-MTP-Q8attn.gguf")
        == "goose:llama:Qwen3.8-27B-NVFP4-MTP-Q8attn.gguf"
    )


@pytest.mark.asyncio
async def test_goose_conversation_update_honors_explicit_harness_selector(tmp_database, tmp_path):
    import database as db
    import server

    conv = await _create_goose_adapter_test_conversation(
        db, "Goose Model Save", mode="goose", project_dir=str(tmp_path)
    )

    updated = await server.api_update_conversation(
        conv["id"],
        {"cc_model": "openrouter:moonshotai/kimi-k2.7-code"},
    )

    assert updated["mode"] == "openrouter"
    assert updated["cc_model"] == "openrouter:moonshotai/kimi-k2.7-code"

    updated = await server.api_update_conversation(
        conv["id"],
        {"cc_model": "goose:openrouter:moonshotai/kimi-k2.7-code"},
    )

    assert updated["mode"] == "goose"
    assert updated["cc_model"] == "goose:openrouter:moonshotai/kimi-k2.7-code"


@pytest.mark.asyncio
async def test_goose_generation_keeps_explicit_local_goose_selector(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await _create_goose_adapter_test_conversation(
        db, "Goose Local Save", mode="goose", project_dir=str(tmp_path)
    )
    parent = await db.add_message(conv["id"], "user", "dispatch")
    captured = {}

    async def fake_goose_generation(websocket, conv_id, conv, data):
        captured["mode"] = conv["mode"]
        captured["cc_model"] = data.get("cc_model")

    async def fail_claude_generation(*args, **kwargs):
        raise AssertionError("bare local model should stay routed through Goose")

    monkeypatch.setattr(server, "_handle_goose_generation", fake_goose_generation)
    monkeypatch.setattr(server, "_handle_claude_generation", fail_claude_generation)

    await server._handle_generation(
        None,
        conv["id"],
        {
            "action": "generate",
            "parent_id": parent["id"],
            "loom_thread_id": parent["loom_thread_id"],
            "cc_model": "goose:llama:Qwen3.8-27B-NVFP4-MTP-Q8attn.gguf",
        },
    )

    assert captured == {
        "mode": "goose",
        "cc_model": "goose:llama:Qwen3.8-27B-NVFP4-MTP-Q8attn.gguf",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True], ids=["serial", "parallel"])
async def test_goose_generation_hands_explicit_openrouter_selector_to_claude_code(
    monkeypatch, tmp_database, tmp_path, parallel
):
    import database as db
    import server

    conv = await _create_goose_adapter_test_conversation(
        db, "Goose to Direct OpenRouter", mode="goose", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(conv["id"], parallel_agents_enabled=int(parallel))
    original = await db.get_conversation(conv["id"])
    parent = await db.add_message(conv["id"], "user", "dispatch")
    captured = {}

    async def fake_claude_generation(websocket, conv_id, loaded_conv, data):
        captured["mode"] = loaded_conv["mode"]
        captured["cc_model"] = data.get("cc_model")

    async def fail_goose_generation(*args, **kwargs):
        raise AssertionError("explicit OpenRouter selector must not be rewritten into Goose")

    monkeypatch.setattr(server, "_handle_claude_generation", fake_claude_generation)
    monkeypatch.setattr(server, "_handle_goose_generation", fail_goose_generation)

    await server._handle_generation(
        None,
        conv["id"],
        {
            "action": "generate",
            "parent_id": parent["id"],
            "loom_thread_id": parent["loom_thread_id"],
            "cc_model": "openrouter:z-ai/glm-5.3-flash",
        },
    )

    assert captured == {
        "mode": "openrouter",
        "cc_model": "openrouter:z-ai/glm-5.3-flash",
    }
    updated = await db.get_conversation(conv["id"])
    assert updated["mode"] == (original["mode"] if parallel else "openrouter")
    assert updated["cc_model"] == (
        original["cc_model"] if parallel else "openrouter:z-ai/glm-5.3-flash"
    )


@pytest.mark.asyncio
async def test_goose_resume_walk_skips_foreign_mode_session(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await _create_goose_adapter_test_conversation(
        db, "Goose Isolation", mode="goose", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2")
    u1 = await db.add_message(conv["id"], "user", "first")
    a1 = await db.add_message(conv["id"], "assistant", "dream reply", parent_id=u1["id"], cc_session_id="dream-sess")
    await db.update_message_content(a1["id"], cc_session_mode="dream")
    u2 = await db.add_message(conv["id"], "user", "second", parent_id=a1["id"])

    captured = {}

    async def fake_run_goose(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["resume_session_id"] = kwargs.get("resume_session_id")

        class Proc:
            pid = 4444
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {"type": "session_info", "session_id": "goose-new"}
            yield {"type": "text_delta", "text": "ok"}
            yield {"type": "result", "session_id": "goose-new"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u2["id"]},
    )

    assert captured["resume_session_id"] is None
    assert "first" in captured["prompt"]
    assert "second" in captured["prompt"]


@pytest.mark.asyncio
async def test_goose_switch_back_replays_intervening_provider_turn(
    monkeypatch, tmp_database, tmp_path
):
    import database as db
    import server

    conv = await _create_goose_adapter_test_conversation(
        db, "Goose A-B-A", mode="goose", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(
        conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2"
    )
    u1 = await db.add_message(conv["id"], "user", "first")
    g1 = await db.add_message(
        conv["id"], "assistant", "old goose reply", parent_id=u1["id"],
        cc_session_id="goose-old",
    )
    await db.update_message_content(
        g1["id"], cc_session_mode="goose",
        cc_model_used="goose:openrouter:z-ai/glm-5.2",
    )
    u2 = await db.add_message(conv["id"], "user", "ask codex", parent_id=g1["id"])
    c1 = await db.add_message(
        conv["id"], "assistant", "intervening codex reply", parent_id=u2["id"],
        cc_session_id="codex-middle",
    )
    await db.update_message_content(
        c1["id"], cc_session_mode="codex", cc_model_used="gpt-5.6-sol"
    )
    u3 = await db.add_message(
        conv["id"], "user", "back to goose", parent_id=c1["id"]
    )

    captured = {}

    async def fake_run_goose(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["resume_session_id"] = kwargs.get("resume_session_id")

        class Proc:
            pid = 4457
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {
                "type": "session_info",
                "session_id": "goose-new",
                "model_attestation": server.goose_client._goose_model_attestation(
                    "goose:openrouter:z-ai/glm-5.2",
                    "",
                    session_id="goose-new",
                ),
            }
            yield {"type": "text_delta", "text": "replayed"}
            yield {"type": "result", "session_id": "goose-new"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u3["id"]},
    )

    assert captured["resume_session_id"] is None
    assert "old goose reply" in captured["prompt"]
    assert "intervening codex reply" in captured["prompt"]
    assert "back to goose" in captured["prompt"]
    leaf = await db.get_active_leaf(conv["id"])
    attestation = json.loads(leaf["model_attestation"])
    assert attestation["session_continuity"] == "rebuilt"
    assert attestation["session_continuity_reason"] == "provider_boundary"


@pytest.mark.asyncio
async def test_goose_resume_walk_uses_same_mode_session(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await _create_goose_adapter_test_conversation(
        db, "Goose Resume", mode="goose", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2")
    u1 = await db.add_message(conv["id"], "user", "first")
    a1 = await db.add_message(conv["id"], "assistant", "goose reply", parent_id=u1["id"], cc_session_id="goose-sess")
    await db.update_message_content(
        a1["id"],
        cc_session_mode="goose",
        cc_model_used="goose:openrouter:z-ai/glm-5.2",
    )
    u2 = await db.add_message(conv["id"], "user", "second", parent_id=a1["id"])

    captured = {}

    async def fake_run_goose(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["resume_session_id"] = kwargs.get("resume_session_id")
        captured["fork_session"] = kwargs.get("fork_session")

        class Proc:
            pid = 4445
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {
                "type": "session_info",
                "session_id": "goose-fork",
                "model_attestation": server.goose_client._goose_model_attestation(
                    "goose:openrouter:z-ai/glm-5.2",
                    "",
                    session_id="goose-fork",
                ),
            }
            yield {"type": "text_delta", "text": "continued"}
            yield {"type": "result", "session_id": "goose-fork"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u2["id"]},
    )

    assert captured["resume_session_id"] == "goose-sess"
    assert captured["fork_session"] is True
    assert '<loom_continuity kind="native_resume">' in captured["prompt"]
    assert captured["prompt"].endswith("\n\nsecond")
    leaf = await db.get_active_leaf(conv["id"])
    attestation = json.loads(leaf["model_attestation"])
    assert attestation["session_continuity"] == "forked"
    assert attestation["session_continuity_reason"] == "resume"
    assert attestation["source_session_id"] == "goose-sess"


@pytest.mark.asyncio
async def test_goose_resume_walk_skips_different_goose_model_session(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await _create_goose_adapter_test_conversation(
        db, "Goose Provider Isolation", mode="goose", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2")
    u1 = await db.add_message(conv["id"], "user", "first")
    a1 = await db.add_message(
        conv["id"],
        "assistant",
        "dream reply",
        parent_id=u1["id"],
        cc_session_id="goose-dream-sess",
    )
    await db.update_message_content(
        a1["id"],
        cc_session_mode="goose",
        cc_model_used="goose:dream:diffusiongemma-26b-a4b-it-nvfp4",
    )
    u2 = await db.add_message(conv["id"], "user", "second", parent_id=a1["id"])

    captured = {}

    async def fake_run_goose(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["resume_session_id"] = kwargs.get("resume_session_id")
        captured["model"] = kwargs.get("model")

        class Proc:
            pid = 4456
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {"type": "session_info", "session_id": "goose-openrouter-new"}
            yield {"type": "text_delta", "text": "rebuilt on openrouter"}
            yield {"type": "result", "session_id": "goose-openrouter-new"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u2["id"]},
    )

    assert captured["resume_session_id"] is None
    assert captured["model"] == "goose:openrouter:z-ai/glm-5.2"
    assert "first" in captured["prompt"]
    assert "second" in captured["prompt"]


@pytest.mark.asyncio
async def test_goose_auto_selector_overrides_conservative_permission_mode(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await _create_goose_adapter_test_conversation(
        db,
        "Goose Auto",
        mode="goose",
        project_dir=str(tmp_path),
    )
    await db.update_conversation_fields(
        conv["id"],
        cc_model="goose:auto:openrouter:z-ai/glm-5.2",
        cc_permission_mode="approve",
    )
    u1 = await db.add_message(conv["id"], "user", "delegate this")

    captured = {}

    async def fake_run_goose(prompt, **kwargs):
        captured["model"] = kwargs.get("model")
        captured["permission_mode"] = kwargs.get("permission_mode")

        class Proc:
            pid = 4446
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {"type": "session_info", "session_id": "goose-auto"}
            yield {"type": "text_delta", "text": "auto"}
            yield {"type": "result", "session_id": "goose-auto"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u1["id"], "cc_permission_mode": "approve"},
    )

    assert captured["model"] == "goose:auto:openrouter:z-ai/glm-5.2"
    assert captured["permission_mode"] == "smart_approve"


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True], ids=["serial", "parallel"])
async def test_goose_generation_passes_selected_effort(monkeypatch, tmp_database, tmp_path, parallel):
    import database as db
    import server

    conv = await _create_goose_adapter_test_conversation(
        db, "Goose Effort", mode="goose", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(
        conv["id"], cc_model="goose:openrouter:deepseek/deepseek-v4-flash-0731",
        cc_effort="high", parallel_agents_enabled=int(parallel),
    )
    u1 = await db.add_message(conv["id"], "user", "think hard")

    captured = {}

    async def fake_run_goose(prompt, **kwargs):
        captured["effort"] = kwargs.get("effort")

        class Proc:
            pid = 4449
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {"type": "session_info", "session_id": "goose-effort"}
            yield {"type": "text_delta", "text": "effort ok"}
            yield {"type": "result", "session_id": "goose-effort"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    async def handle_adapter(websocket, conv_id, data):
        await server._handle_goose_generation(websocket, conv_id, await db.get_conversation(conv_id), data)

    # Exercise real lane admission/release; the controlled provider never edits
    # files, so this adapter test does not need a workspace recovery snapshot.
    monkeypatch.setattr(server, "_handle_generation_with_workspace_safety", handle_adapter)
    _, task = await server._launch_generation_task(
        None, conv["id"],
        {"action": "generate", "parent_id": u1["id"], "cc_effort": "max"},
    )
    await task

    assert captured["effort"] == "max"
    saved = await db.get_conversation(conv["id"])
    assert saved["cc_effort"] == ("high" if parallel else "max")
    lease_id = task._generation_lease_id
    assert bool(lease_id) is (not parallel)
    if lease_id:
        lease = await db.get_generation_lease(lease_id)
        assert lease["released_at"] is not None
        assert lease["terminal_state"] == "completed"


@pytest.mark.asyncio
async def test_goose_rejects_ambiguous_model_before_launch(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await _create_goose_adapter_test_conversation(
        db, "Goose Invalid Selector", mode="goose", project_dir=str(tmp_path)
    )
    parent = await db.add_message(conv["id"], "user", "resolve this exact model")
    sent = []

    async def capture(_conv_id, event):
        sent.append(event)

    async def fail_launch(*_args, **_kwargs):
        raise AssertionError("Goose must not launch an implicitly-routed model")

    monkeypatch.setattr(server, "_ws_send", capture)
    monkeypatch.setattr(server.goose_client, "run_goose", fail_launch)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={
            "action": "generate",
            "parent_id": parent["id"],
            "loom_thread_id": parent["loom_thread_id"],
            "cc_model": "not-a-provider-selector",
        },
    )

    error = next(event for event in sent if event.get("type") == "error")
    assert error["provider"] == "goose"
    assert error["stage"] == "model_resolution"
    assert error["requested_model"] == "not-a-provider-selector"
    assert "not a Goose, OpenRouter, loaded GGUF, or Dream model" in error["error"]


@pytest.mark.asyncio
async def test_goose_resume_turn_passes_latest_image_attachment(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    image = tmp_path / "sample.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nsample")

    conv = await _create_goose_adapter_test_conversation(
        db, "Goose Image", mode="goose", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2")
    u1 = await db.add_message(conv["id"], "user", "first")
    a1 = await db.add_message(conv["id"], "assistant", "prior", parent_id=u1["id"], cc_session_id="goose-image-session")
    await db.update_message_content(
        a1["id"],
        cc_session_mode="goose",
        cc_model_used="goose:openrouter:z-ai/glm-5.2",
    )
    u2 = await db.add_message(
        conv["id"],
        "user",
        "what is in this image?",
        parent_id=a1["id"],
        image_path=json.dumps([str(image)]),
    )

    captured = {}

    async def fake_run_goose(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["image_paths"] = kwargs.get("image_paths")
        captured["resume_session_id"] = kwargs.get("resume_session_id")

        class Proc:
            pid = 4457
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {"type": "session_info", "session_id": "goose-image-fork"}
            yield {"type": "text_delta", "text": "image ok"}
            yield {"type": "result", "session_id": "goose-image-fork"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u2["id"]},
    )

    copied = tmp_path / "attached_files" / "sample.png"
    assert captured["resume_session_id"] == "goose-image-session"
    assert "sample.png (in attached_files/)" in captured["prompt"]
    assert captured["image_paths"] == [copied]
    assert copied.read_bytes() == image.read_bytes()


@pytest.mark.asyncio
async def test_goose_finalizes_content_and_usage_tokens(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await _create_goose_adapter_test_conversation(
        db, "Goose Tokens", mode="goose", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2")
    u1 = await db.add_message(conv["id"], "user", "count tokens")

    async def fake_run_goose(prompt, **kwargs):
        class Proc:
            pid = 4447
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {"type": "session_info", "session_id": "goose-token"}
            yield {"type": "text_delta", "text": "token ok"}
            yield {"type": "usage", "input_tokens": 11, "output_tokens": 5}
            yield {"type": "result", "session_id": "goose-token"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u1["id"]},
    )

    leaf = await db.get_active_leaf(conv["id"])
    assert leaf["content"] == "token ok"
    assert leaf["turn_input_tokens"] == 11
    assert leaf["turn_output_tokens"] == 5
    assert leaf["cc_session_id"] == "goose-token"
    assert leaf["cc_session_mode"] == "goose"
    assert leaf["time_to_first_token_ms"] is not None


@pytest.mark.asyncio
async def test_goose_ams_error_footer_persists_partial_failure_without_replay(
    monkeypatch, tmp_database, tmp_path
):
    import database as db
    import server

    conv = await _create_goose_adapter_test_conversation(
        db, "Goose AMS Failure", mode="goose", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(
        conv["id"], cc_model="goose:openrouter:z-ai/glm-5.3-flash"
    )
    u1 = await db.add_message(conv["id"], "user", "first")
    a1 = await db.add_message(
        conv["id"], "assistant", "prior", parent_id=u1["id"],
        cc_session_id="goose-ams-prior",
    )
    await db.update_message_content(
        a1["id"], cc_session_mode="goose",
        cc_model_used="goose:openrouter:z-ai/glm-5.3-flash",
    )
    u2 = await db.add_message(
        conv["id"], "user", "continue", parent_id=a1["id"]
    )
    calls = []

    async def fake_run_goose(prompt, **kwargs):
        calls.append(kwargs)

        class Proc:
            pid = 4491
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {"type": "session_info", "session_id": "goose-ams-fork"}
            yield {"type": "text_delta", "text": "Useful partial work."}
            yield {"type": "tool_start", "name": "Write", "tool_id": "write-1"}
            yield {
                "type": "tool_input_delta", "tool_id": "write-1",
                "json": '{"path":"canvas/app.js"}',
            }
            yield {
                "type": "tool_result", "tool_id": "write-1",
                "content": "write completed", "is_error": False,
            }
            yield {
                "type": "text_delta",
                "text": (
                    "Ran into this error: Server error: Upstream idle timeout exceeded.\n\n"
                    "Please retry if you think this is a transient or recoverable error."
                ),
            }
            # This is the misleading success-shaped ACP completion Goose v41
            # produced in AMS Migration.
            yield {
                "type": "result", "session_id": "goose-ams-fork",
                "stop_reason": "end_turn",
            }

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u2["id"]},
    )

    assert len(calls) == 1
    assert calls[0]["resume_session_id"] == "goose-ams-prior"
    assert calls[0]["progress_timeout_seconds"] == server.config.goose_progress_timeout_seconds
    leaf = await db.get_active_leaf(conv["id"])
    assert leaf["turn_lifecycle_state"] == "partial_failed"
    assert leaf["cc_session_id"] == "goose-ams-fork"
    assert leaf["time_to_first_token_ms"] is not None
    assert "Useful partial work." in leaf["content"]
    assert "Goose turn failed" in leaf["content"]
    assert "Please retry if you think" not in leaf["content"]
    assert "write completed" in leaf["content_blocks"]


@pytest.mark.asyncio
async def test_goose_does_not_replay_resume_after_tool_activity_then_exception(
    monkeypatch, tmp_database, tmp_path
):
    import database as db
    import server

    conv = await _create_goose_adapter_test_conversation(
        db, "Goose Side Effect Failure", mode="goose", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(
        conv["id"], cc_model="goose:openrouter:z-ai/glm-5.3-flash"
    )
    u1 = await db.add_message(conv["id"], "user", "first")
    a1 = await db.add_message(
        conv["id"], "assistant", "prior", parent_id=u1["id"],
        cc_session_id="goose-side-effect-prior",
    )
    await db.update_message_content(
        a1["id"], cc_session_mode="goose",
        cc_model_used="goose:openrouter:z-ai/glm-5.3-flash",
    )
    u2 = await db.add_message(
        conv["id"], "user", "continue", parent_id=a1["id"]
    )
    launches = 0

    async def fake_run_goose(prompt, **kwargs):
        nonlocal launches
        launches += 1

        class Proc:
            pid = 4492
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {"type": "session_info", "session_id": "goose-side-effect-fork"}
            yield {"type": "tool_start", "name": "Write", "tool_id": "write-2"}
            yield {
                "type": "tool_result", "tool_id": "write-2",
                "content": "mutation landed", "is_error": False,
            }
            raise RuntimeError("transport failed after tool execution")

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u2["id"]},
    )

    assert launches == 1
    leaf = await db.get_active_leaf(conv["id"])
    assert leaf["turn_lifecycle_state"] == "partial_failed"
    assert "mutation landed" in leaf["content_blocks"]


@pytest.mark.asyncio
async def test_goose_retries_when_resume_stream_raises(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await _create_goose_adapter_test_conversation(
        db, "Goose Bad Resume", mode="goose", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2")
    u1 = await db.add_message(conv["id"], "user", "first")
    a1 = await db.add_message(conv["id"], "assistant", "goose reply", parent_id=u1["id"], cc_session_id="goose-stale")
    await db.update_message_content(
        a1["id"], cc_session_mode="goose",
        cc_model_used="goose:openrouter:z-ai/glm-5.2",
    )
    u2 = await db.add_message(conv["id"], "user", "second", parent_id=a1["id"])

    calls = []

    async def fake_run_goose(prompt, **kwargs):
        calls.append({"prompt": prompt, "resume_session_id": kwargs.get("resume_session_id")})

        class Proc:
            pid = 4448 + len(calls)
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            if kwargs.get("resume_session_id"):
                raise RuntimeError("session not found")
            yield {"type": "session_info", "session_id": "goose-rebuilt"}
            yield {"type": "text_delta", "text": "rebuilt"}
            yield {"type": "result", "session_id": "goose-rebuilt"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u2["id"]},
    )

    assert [c["resume_session_id"] for c in calls] == ["goose-stale", None]
    assert '<loom_continuity kind="native_resume">' in calls[0]["prompt"]
    assert calls[0]["prompt"].endswith("\n\nsecond")
    assert "first" in calls[1]["prompt"]
    assert "second" in calls[1]["prompt"]
    leaf = await db.get_active_leaf(conv["id"])
    assert leaf["content"] == "rebuilt"
    assert leaf["cc_session_id"] == "goose-rebuilt"


@pytest.mark.asyncio
async def test_goose_retries_empty_resumed_turn(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await _create_goose_adapter_test_conversation(
        db, "Goose Empty Resume", mode="goose", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2")
    u1 = await db.add_message(conv["id"], "user", "first")
    a1 = await db.add_message(conv["id"], "assistant", "goose reply", parent_id=u1["id"], cc_session_id="goose-empty")
    await db.update_message_content(
        a1["id"], cc_session_mode="goose",
        cc_model_used="goose:openrouter:z-ai/glm-5.2",
    )
    u2 = await db.add_message(conv["id"], "user", "second", parent_id=a1["id"])

    calls = []

    async def fake_run_goose(prompt, **kwargs):
        calls.append(kwargs.get("resume_session_id"))

        class Proc:
            pid = 4451 + len(calls)
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            if kwargs.get("resume_session_id"):
                yield {"type": "session_info", "session_id": "goose-empty-fork"}
                yield {"type": "result", "session_id": "goose-empty-fork"}
                return
            yield {"type": "session_info", "session_id": "goose-retry"}
            yield {"type": "text_delta", "text": "fallback text"}
            yield {"type": "result", "session_id": "goose-retry"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u2["id"]},
    )

    assert calls == ["goose-empty", None]
    leaf = await db.get_active_leaf(conv["id"])
    assert leaf["content"] == "fallback text"
    assert leaf["cc_session_id"] == "goose-retry"


@pytest.mark.asyncio
async def test_goose_updates_live_snapshot_and_merges_thinking(monkeypatch, tmp_database, tmp_path):
    import asyncio
    import database as db
    import server

    conv = await _create_goose_adapter_test_conversation(
        db, "Goose Snapshot", mode="goose", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2")
    u1 = await db.add_message(conv["id"], "user", "think")
    gen_key = (conv["id"], u1["id"], 9001)
    asyncio.current_task()._gen_key = gen_key
    snapshots = []
    original_update = server._update_gen_snapshot

    def capture_snapshot(key, **fields):
        snapshots.append((key, dict(fields)))
        return original_update(key, **fields)

    async def fake_run_goose(prompt, **kwargs):
        class Proc:
            pid = 4454
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {"type": "session_info", "session_id": "goose-snap"}
            yield {"type": "thinking_delta", "text": "alpha "}
            yield {"type": "thinking_delta", "text": "beta"}
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "result", "session_id": "goose-snap"}

        return Proc(), events()

    monkeypatch.setattr(server, "_update_gen_snapshot", capture_snapshot)
    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    try:
        await server._handle_goose_generation(
            websocket=None,
            conv_id=conv["id"],
            conv=await db.get_conversation(conv["id"]),
            data={"action": "generate", "parent_id": u1["id"]},
        )
    finally:
        delattr(asyncio.current_task(), "_gen_key")

    assert any(key == gen_key and fields.get("full_text") == "done" for key, fields in snapshots)
    leaf = await db.get_active_leaf(conv["id"])
    blocks = json.loads(leaf["content_blocks"])
    assert blocks == [
        {"type": "thinking", "text": "alpha beta"},
        {"type": "text", "text": "done"},
    ]


@pytest.mark.asyncio
async def test_goose_generation_does_not_persist_blank_exception(monkeypatch, tmp_database, tmp_path):
    import asyncio
    import database as db
    import server

    conv = await _create_goose_adapter_test_conversation(
        db, "Goose Blank Error", mode="goose", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2")
    u1 = await db.add_message(conv["id"], "user", "use openrouter")

    async def fake_run_goose(prompt, **kwargs):
        class Proc:
            pid = 4455
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            raise asyncio.TimeoutError()
            yield

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u1["id"]},
    )

    leaf = await db.get_active_leaf(conv["id"])
    assert leaf["content"] == "[Error: Goose operation timed out]"


@pytest.mark.asyncio
async def test_goose_generation_does_not_persist_blank_error_event(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await _create_goose_adapter_test_conversation(
        db, "Goose Blank Event", mode="goose", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2")
    u1 = await db.add_message(conv["id"], "user", "use openrouter")

    async def fake_run_goose(prompt, **kwargs):
        class Proc:
            pid = 4456
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {"type": "session_info", "session_id": "goose-blank"}
            yield {"type": "error", "error": "   "}
            yield {"type": "result", "session_id": "goose-blank", "stop_reason": "error"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u1["id"]},
    )

    leaf = await db.get_active_leaf(conv["id"])
    # A blank preliminary error must not mask the later terminal reason.
    assert leaf["content"] == "[Error: Goose ended with error]"


@pytest.mark.asyncio
async def test_goose_generation_empty_text_with_tools_recorded_as_partial_failed(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await _create_goose_adapter_test_conversation(
        db, "Goose Empty Text", mode="goose", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(conv["id"], cc_model="goose:openrouter:z-ai/glm-5.2")
    u1 = await db.add_message(conv["id"], "user", "read model info")

    async def fake_run_goose(prompt, **kwargs):
        class Proc:
            pid = 4457
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {"type": "session_info", "session_id": "goose-empty-turn"}
            yield {
                "type": "tool_start",
                "tool_id": "call-1",
                "name": "bash",
            }
            yield {
                "type": "tool_input_delta",
                "tool_id": "call-1",
                "json": '{"command": "cat /proc/device-tree/model"}',
            }
            yield {
                "type": "tool_result",
                "tool_id": "call-1",
                "content": "Raspberry Pi 5",
            }
            yield {"type": "result", "session_id": "goose-empty-turn", "stop_reason": "end_turn"}

        return Proc(), events()

    monkeypatch.setattr(server.goose_client, "run_goose", fake_run_goose)

    await server._handle_goose_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={"action": "generate", "parent_id": u1["id"]},
    )

    leaf = await db.get_active_leaf(conv["id"])
    assert leaf["turn_lifecycle_state"] == "partial_failed"
    assert "Goose turn failed after 1 tool call(s) without a final response" in leaf["content"]
    assert leaf["content_blocks"] is not None
    blocks = json.loads(leaf["content_blocks"])
    assert len(blocks) >= 1


def test_goose_session_context_limit_sync(tmp_path, monkeypatch):
    import sqlite3
    import goose_client

    # SQLite's own context manager commits/rolls back; it does not close the
    # connection. Track actual handle release on update and early-return paths.
    connections = []
    original_connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            super().close()
            self.closed = True

    def tracked_connect(*args, **kwargs):
        connection = original_connect(*args, factory=TrackedConnection, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    db_dir = tmp_path / "sessions"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "sessions.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, model_config_json TEXT)")
        conn.execute(
            "INSERT INTO sessions (id, model_config_json) VALUES (?, ?)",
            ("sess-1", json.dumps({"model_name": "local.gguf", "context_limit": 100096})),
        )
        conn.execute(
            "INSERT INTO sessions (id, model_config_json) VALUES (?, ?)",
            ("sess-2", json.dumps({"model_name": "local.gguf", "context_limit": 300000})),
        )
        conn.commit()

    monkeypatch.setattr(goose_client, "_find_goose_sessions_db", lambda: db_path)

    # 1. Update lower limit to capacity
    updated = goose_client._sync_goose_session_context_limit("sess-1", 250112)
    assert updated is True
    assert all(connection.closed for connection in connections)
    with closing(sqlite3.connect(db_path)) as conn:
        cfg = json.loads(conn.execute("SELECT model_config_json FROM sessions WHERE id = 'sess-1'").fetchone()[0])
        assert cfg["context_limit"] == 250112

    # 2. Do not downgrade higher limit
    not_updated = goose_client._sync_goose_session_context_limit("sess-2", 250112)
    assert not_updated is False
    assert all(connection.closed for connection in connections)
    with closing(sqlite3.connect(db_path)) as conn:
        cfg = json.loads(conn.execute("SELECT model_config_json FROM sessions WHERE id = 'sess-2'").fetchone()[0])
        assert cfg["context_limit"] == 300000

    # 3. Nonexistent session
    assert goose_client._sync_goose_session_context_limit("nonexistent", 250112) is False
    assert all(connection.closed for connection in connections)

    # 4. No db path
    monkeypatch.setattr(goose_client, "_find_goose_sessions_db", lambda: None)
    assert goose_client._sync_goose_session_context_limit("sess-1", 250112) is False
