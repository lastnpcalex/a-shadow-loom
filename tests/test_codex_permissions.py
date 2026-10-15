import asyncio
import io
import json
import sys

import pytest


@pytest.fixture(autouse=True)
def explicit_balanced_permission_profile(monkeypatch):
    """These approval workflows exercise Balanced, independently of user defaults."""
    from config import config
    monkeypatch.setattr(config, "default_permission_profile", "balanced")


@pytest.mark.parametrize("output_key,exit_key", [("aggregatedOutput", "exitCode"), ("aggregated_output", "exit_code")])
def test_command_result_supports_current_and_legacy_fields(output_key, exit_key):
    import codex_client
    item = {"type": "commandExecution", "status": "completed", output_key: "fixture output", exit_key: 0}
    assert codex_client._codex_tool_output(item) == "fixture output"
    assert not codex_client._codex_tool_failed(item)
    item[exit_key] = 2
    assert codex_client._codex_tool_failed(item)
    item[exit_key] = None
    item["status"] = "declined"
    assert codex_client._codex_tool_failed(item)


def test_codex_default_permission_mode_requests_user_approval():
    import codex_client

    assert codex_client._codex_approval_policy("default") == "on-request"
    assert codex_client._codex_approval_policy("plan") == "on-request"
    assert codex_client._codex_approval_policy(None) == "on-request"
    assert codex_client._codex_approval_policy("on-request") == "on-request"
    assert codex_client._codex_approval_policy("never") == "never"


def test_codex_model_mapping_preserves_explicit_latest_selection(monkeypatch):
    import codex_client

    monkeypatch.delenv("LOOM_CODEX_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("CODEX_MODEL", raising=False)

    assert codex_client._loom_model_to_codex("codex-gpt-5.6-sol") == "gpt-5.6-sol"
    assert codex_client._loom_model_to_codex("codex-gpt-6-astra") == "gpt-6-astra"
    assert codex_client._loom_model_to_codex("Codex (GPT-5.6-Sol)") == "gpt-5.6-sol"
    assert codex_client._loom_model_to_codex("gpt-5.6-terra") == "gpt-5.6-terra"
    assert codex_client._loom_model_to_codex("codex-my-explicit-model") == "my-explicit-model"


def test_codex_model_mapping_uses_cache_only_when_selection_missing(monkeypatch, tmp_path):
    import codex_client

    home = tmp_path / "home"
    cache_dir = home / ".codex"
    cache_dir.mkdir(parents=True)
    (cache_dir / "models_cache.json").write_text(
        json.dumps({
            "models": [
                {"slug": "gpt-reserve", "display_name": "GPT-Reserve", "visibility": "hide", "supported_in_api": True},
                {"slug": "gpt-6-astra", "display_name": "GPT-6-Astra", "visibility": "list", "supported_in_api": True},
                {"slug": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol", "supported_in_api": True},
                {"slug": "gpt-5.5", "display_name": "GPT-5.5", "supported_in_api": True},
            ]
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("LOOM_CODEX_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("CODEX_MODEL", raising=False)

    assert codex_client._loom_model_to_codex("") == "gpt-6-astra"
    assert codex_client._loom_model_to_codex("codex-new-provider-model") == "new-provider-model"


def test_codex_has_a_distinct_history_rebuild_policy():
    import model_context

    for model in ("codex-gpt-5.6-sol", "gpt-5.6-sol", "codex-gpt-6-astra", "gpt-6-astra"):
        assert model_context.is_codex(model)
        assert not model_context.is_local_llama(model)
        assert model_context.handoff_threshold(model) == model_context.THRESHOLD_CODEX
        assert model_context.provider_label(model) == "Codex"

    assert not model_context.is_codex("gpt-oss-120b")


def test_codex_reasoning_effort_preserves_current_levels():
    import codex_client

    assert codex_client._codex_reasoning_effort("xhigh") == "xhigh"
    assert codex_client._codex_reasoning_effort("max") == "max"
    assert codex_client._codex_reasoning_effort("ultra") == "ultra"
    assert codex_client._codex_reasoning_effort("minimal") == "low"
    assert codex_client._codex_reasoning_effort("invalid") is None


def test_codex_thread_request_disables_silent_model_fallback():
    import codex_client

    method, params = codex_client._codex_thread_request(
        "C:/workspace",
        "gpt-5.6-sol",
        "on-request",
        "workspace-write",
    )

    assert method == "thread/start"
    assert params["model"] == "gpt-5.6-sol"
    assert params["allowProviderModelFallback"] is False


def test_codex_model_attestation_separates_ui_request_from_effective_model():
    import codex_client

    attestation = codex_client._codex_model_attestation(
        "codex-gpt-5.6-sol",
        "gpt-5.6-sol",
        {
            "model": "gpt-5.6-sol",
            "modelProvider": "openai",
            "thread": {"id": "thread-123"},
        },
        session_id="thread-123",
        app_server_user_agent="codex_cli_rs/1.2.3",
    )

    assert attestation == {
        "status": "verified",
        "harness": "Codex app-server",
        "requested_model": "codex-gpt-5.6-sol",
        "launch_model": "gpt-5.6-sol",
        "effective_model": "gpt-5.6-sol",
        "model_provider": "openai",
        "source": "codex_app_server_thread_response",
        "verification_level": "harness",
        "session_id": "thread-123",
        "thread_id": "thread-123",
        "app_server": "codex_cli_rs/1.2.3",
        "fallback_allowed": False,
    }


def test_codex_model_attestation_detects_mismatch_and_missing_evidence():
    import codex_client

    mismatch = codex_client._codex_model_attestation(
        "codex-gpt-5.6-sol",
        "gpt-5.6-sol",
        {"model": "gpt-5.6-terra", "modelProvider": "openai"},
    )
    missing = codex_client._codex_model_attestation(
        "codex-gpt-5.6-sol",
        "gpt-5.6-sol",
        {"model": "gpt-5.6-sol"},
    )

    assert mismatch["status"] == "mismatch"
    assert mismatch["effective_model"] == "gpt-5.6-terra"
    assert missing["status"] == "unverified"
    assert missing["effective_model"] == "gpt-5.6-sol"
    assert missing["model_provider"] is None


def test_codex_app_server_approval_mapping_is_session_scoped():
    import codex_client

    assert codex_client._app_approval_response(
        "item/commandExecution/requestApproval",
        allow=True,
        always=False,
    ) == {"decision": "accept"}
    assert codex_client._app_approval_response(
        "item/commandExecution/requestApproval",
        allow=True,
        always=True,
    ) == {"decision": "acceptForSession"}
    assert codex_client._app_approval_response(
        "item/fileChange/requestApproval",
        allow=False,
        always=False,
    ) == {"decision": "decline"}


def test_codex_permissions_approval_echoes_requested_permissions():
    import codex_client

    requested = {
        "filesystem": {
            "entries": [
                {
                    "path": r"C:\repo\.git",
                    "access": "write",
                }
            ]
        },
        "network": {"enabled": False},
    }

    assert codex_client._app_approval_response(
        "item/permissions/requestApproval",
        allow=True,
        always=True,
        params={"permissions": requested},
    ) == {
        "permissions": requested,
        "scope": "session",
    }


def test_codex_app_server_sandbox_policy_scopes_workspace(tmp_path):
    import codex_client

    policy = codex_client._app_sandbox_policy(str(tmp_path))

    assert policy["type"] == "workspaceWrite"
    assert policy["writableRoots"] == [str(tmp_path)]
    assert policy["networkAccess"] is False


def test_codex_goal_set_params_use_app_server_shape():
    import codex_client

    params = codex_client._codex_goal_set_params(
        "thr_123",
        objective="Finish the migration",
        status="active",
        token_budget=40000,
    )

    assert params == {
        "threadId": "thr_123",
        "objective": "Finish the migration",
        "status": "active",
        "tokenBudget": 40000,
    }


def test_codex_slash_commands_include_goal():
    import skill_scanner

    commands = skill_scanner.get_all_skills(agent="codex")
    goal = next(cmd for cmd in commands if cmd["name"] == "goal")

    assert goal["command"] == "/goal"
    assert goal["mode"] == "meta"


def test_codex_token_usage_updated_event_is_normalized():
    import codex_client

    raw = {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": "thr_1",
            "tokenUsage": {
                "totalInputTokens": 1234,
                "totalOutputTokens": 567,
            },
        },
    }

    assert codex_client._codex_usage(raw) == {
        "type": "usage",
        "input_tokens": 1234,
        "output_tokens": 567,
    }


def test_codex_completed_turn_preserves_provider_error_detail():
    import codex_client

    raw = {
        "method": "turn/completed",
        "params": {
            "turn": {
                "id": "turn-1",
                "status": "failed",
                "error": {"message": "Input exceeds the model context window"},
            }
        },
    }

    assert codex_client._codex_completed_turn_error(raw) == (
        "Input exceeds the model context window"
    )


async def test_codex_stdout_eof_terminates_turn_instead_of_hanging(
    monkeypatch, tmp_path
):
    import codex_client

    class FakeStdin:
        def __init__(self, proc):
            self.proc = proc
            self.closed = False

        def is_closing(self):
            return self.closed

        def close(self):
            self.closed = True

        def write(self, payload):
            request = json.loads(payload.decode("utf-8"))
            request_id = request.get("id")
            method = request.get("method")
            if request_id is None:
                return
            if method == "initialize":
                result = {"userAgent": "codex-test/1"}
            elif method == "config/read":
                result = {"config": {}}
            elif method == "thread/start":
                result = {
                    "thread": {"id": "thread-eof"},
                    "model": "gpt-5.6-sol",
                    "modelProvider": "openai",
                }
            elif method == "turn/start":
                result = {"turn": {"id": "turn-eof"}}
            else:
                result = {}
            self.proc.stdout.feed_data(
                (json.dumps({"id": request_id, "result": result}) + "\n").encode()
            )
            if method == "turn/start":
                asyncio.get_running_loop().call_soon(self.proc.exit_early)

        async def drain(self):
            await asyncio.sleep(0)

    class FakeProcess:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdin = FakeStdin(self)
            self.returncode = None
            self.pid = 12345
            self._exited = asyncio.Event()

        def exit_early(self):
            if self.returncode is not None:
                return
            self.returncode = 17
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._exited.set()

        async def wait(self):
            await self._exited.wait()
            return self.returncode

        def terminate(self):
            self.exit_early()

        def kill(self):
            self.exit_early()

    fake = FakeProcess()

    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(codex_client, "_find_codex_exe", lambda: "codex")
    monkeypatch.setattr(codex_client, "_thread_mcp_servers", lambda *a, **k: {})

    _proc, event_stream = await codex_client.run_codex(
        "test prompt",
        str(tmp_path),
        conv_id=99,
        model="codex-gpt-5.6-sol",
    )
    events = []
    async with asyncio.timeout(2):
        async for event in event_stream:
            events.append(event)

    result = next(event for event in events if event.get("type") == "result")
    assert result["is_error"] is True
    assert "closed its output" in result["error"]
    assert "exit code 17" in result["error"]


@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
async def test_codex_thread_idle_does_not_abort_an_active_tool_turn(
    monkeypatch, tmp_path, terminal_status
):
    import codex_client

    class FakeStdin:
        def __init__(self, proc):
            self.proc = proc
            self.closed = False

        def is_closing(self):
            return self.closed

        def close(self):
            self.closed = True

        def write(self, payload):
            request = json.loads(payload.decode("utf-8"))
            request_id = request.get("id")
            method = request.get("method")
            if request_id is None:
                return
            if method == "initialize":
                result = {"userAgent": "codex-test/1"}
            elif method == "config/read":
                result = {"config": {}}
            elif method == "thread/start":
                result = {
                    "thread": {"id": "thread-idle"},
                    "model": "gpt-5.6-sol",
                    "modelProvider": "openai",
                }
            elif method == "turn/start":
                result = {"turn": {"id": "turn-idle"}}
            else:
                result = {}
            self.proc.feed({"id": request_id, "result": result})
            if method == "turn/start":
                asyncio.get_running_loop().call_soon(self.proc.finish_turn)

        async def drain(self):
            await asyncio.sleep(0)

    class FakeProcess:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdin = FakeStdin(self)
            self.returncode = None
            self.pid = 12347
            self._exited = asyncio.Event()

        def feed(self, event):
            self.stdout.feed_data((json.dumps(event) + "\n").encode())

        def finish_turn(self):
            # This conversation-level idle notification is not authority to
            # end the exact active turn. More model output follows it.
            self.feed({
                "method": "thread/status/changed",
                "params": {"threadId": "thread-idle", "status": "idle"},
            })
            self.feed({"method": "warning", "params": {"message": "nonfatal configuration warning"}})
            for thread, turn, inputs, outputs, last_outputs in [
                ("other-thread", "other-turn", 999999, 999999, 999999),
                ("thread-idle", "old-turn", 888888, 888888, 888888),
                ("thread-idle", "turn-idle", 80000, 9000, 10),
                ("thread-idle", "turn-idle", 4000, 9005, 5),
                ("thread-idle", "turn-idle", 0, 9005, 0),
            ]:
                self.feed({"method": "thread/tokenUsage/updated", "params": {
                    "threadId": thread, "turnId": turn, "tokenUsage": {
                        "last": {"inputTokens": inputs, "outputTokens": last_outputs},
                        "total": {"inputTokens": 1000000, "outputTokens": outputs},
                        "modelContextWindow": 100000,
                    },
                }})
            for method in ("item/started", "item/completed"):
                self.feed({"method": method, "params": {
                    "threadId": "thread-idle", "turnId": "turn-idle",
                    "item": {"id": "compact-1", "type": "contextCompaction"},
                }})
            self.feed({"method": "item/completed", "params": {"item": {
                "id": "command-ok", "type": "commandExecution", "command": "echo fixture",
                "status": "completed", "aggregatedOutput": "FIXTURE_TOOL_OK", "exitCode": 0,
            }}})
            self.feed({"method": "item/completed", "params": {"item": {
                "id": "source-read", "type": "commandExecution", "command": "read source fixture",
                "status": "completed", "aggregatedOutput": 'if "windows sandbox: timed out" in output or "runner pipe-in" in output:',
                "exitCode": 0,
            }}})
            self.feed({"method": "item/completed", "params": {"item": {
                "id": "command-declined", "type": "commandExecution", "command": "echo fixture",
                "status": "declined", "aggregatedOutput": None, "exitCode": None,
            }}})
            self.feed({"method": "turn/completed", "params": {"threadId": "other-thread", "turn": {"id": "other-turn", "status": "completed"}}})
            if terminal_status == "failed":
                self.feed({"method": "error", "params": {"turnId": "turn-idle", "willRetry": False, "error": {"message": "stream disconnected"}}})
            self.feed({
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "thread-idle",
                    "turnId": "turn-idle",
                    "item": {"type": "agentMessage"},
                    "delta": "final answer",
                },
            })
            self.feed({
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-idle",
                    "turn": {"id": "turn-idle", "status": terminal_status, "items": [{
                        "id": "command-exit", "type": "commandExecution", "command": "exit 2",
                        "status": "completed", "aggregatedOutput": "runner pipe-in: windows sandbox: timed out", "exitCode": 2,
                    }],
                             "error": {"message": "stream disconnected", "codexErrorInfo": "ResponseStreamDisconnected"} if terminal_status == "failed" else None},
                },
            })

        async def wait(self):
            await self._exited.wait()
            return self.returncode

        def terminate(self):
            self.returncode = 0
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._exited.set()

        def kill(self):
            self.terminate()

    fake = FakeProcess()

    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(codex_client, "_find_codex_exe", lambda: "codex")
    monkeypatch.setattr(codex_client, "_thread_mcp_servers", lambda *a, **k: {})
    monkeypatch.setattr(codex_client.sys, "platform", "linux")

    _proc, event_stream = await codex_client.run_codex(
        "test prompt",
        str(tmp_path),
        conv_id=101,
        model="codex-gpt-5.6-sol",
    )
    events = []
    async with asyncio.timeout(2):
        async for event in event_stream:
            events.append(event)

    result = next(event for event in events if event.get("type") == "result")
    assert result["is_error"] is (terminal_status == "failed")
    assert result["terminal_confirmed"] is True
    if terminal_status == "failed":
        assert result["error_code"] == "ResponseStreamDisconnected"
    assert result["result_text"] == "final answer"
    usage = [event for event in events if event["type"] == "usage"]
    assert [event["input_tokens"] for event in usage] == [80000, 4000, 0]
    assert [event["output_tokens"] for event in usage] == [10, 5, 0]
    assert all(event["context_window"] == 100000 for event in usage)
    assert any(event.get("text") == "Codex native context compactified." for event in events)
    tool_results = {e["tool_id"]: e for e in events if e["type"] == "tool_result"}
    assert tool_results["command-ok"]["content"] == "FIXTURE_TOOL_OK"
    assert not tool_results["command-ok"]["is_error"]
    assert tool_results["command-declined"]["is_error"]
    assert "windows sandbox: timed out" in tool_results["command-exit"]["content"]
    assert tool_results["command-exit"]["is_error"]
    assert not tool_results["source-read"]["is_error"]
    assert not any(event["type"] == "error" for event in events)
    assert any(
        event.get("type") == "text_delta" and event.get("text") == "final answer"
        for event in events
    )


async def test_cancelling_codex_event_stream_waits_for_process_exit(
    monkeypatch, tmp_path
):
    import codex_client

    class FakeStdin:
        def __init__(self, proc):
            self.proc = proc
            self.closed = False

        def is_closing(self):
            return self.closed

        def close(self):
            self.closed = True

        def write(self, payload):
            request = json.loads(payload.decode("utf-8"))
            request_id = request.get("id")
            method = request.get("method")
            if request_id is None:
                return
            if method == "initialize":
                result = {"userAgent": "codex-test/1"}
            elif method == "config/read":
                result = {"config": {}}
            elif method == "thread/start":
                result = {
                    "thread": {"id": "thread-cancel"},
                    "model": "gpt-5.6-sol",
                    "modelProvider": "openai",
                }
            elif method == "turn/start":
                result = {"turn": {"id": "turn-cancel"}}
            else:
                result = {}
            self.proc.stdout.feed_data(
                (json.dumps({"id": request_id, "result": result}) + "\n").encode()
            )

        async def drain(self):
            await asyncio.sleep(0)

    class FakeProcess:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdin = FakeStdin(self)
            self.returncode = None
            self.pid = 12346
            self.terminated = False
            self._exited = asyncio.Event()

        async def wait(self):
            await self._exited.wait()
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._exited.set()

        def kill(self):
            self.terminate()

    fake = FakeProcess()

    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(codex_client, "_find_codex_exe", lambda: "codex")
    monkeypatch.setattr(codex_client, "_thread_mcp_servers", lambda *a, **k: {})
    monkeypatch.setattr(codex_client.sys, "platform", "linux")

    _proc, event_stream = await codex_client.run_codex(
        "test prompt",
        str(tmp_path),
        conv_id=100,
        model="codex-gpt-5.6-sol",
    )
    session_started = asyncio.Event()

    async def consume():
        async for event in event_stream:
            if event.get("type") == "session_info":
                session_started.set()

    consumer = asyncio.create_task(consume())
    async with asyncio.timeout(2):
        await session_started.wait()
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)

    assert fake.terminated is True
    assert fake.returncode == -15
    assert fake._exited.is_set()


async def test_codex_app_server_permission_uses_direct_handler():
    import codex_client

    calls = []

    async def handler(payload):
        calls.append(payload)
        return {"allow": True, "always": True}

    result = await codex_client._post_loom_permission(
        12345,
        77,
        "item/fileChange/requestApproval",
        {"fileChanges": [{"path": "proof.txt"}]},
        permission_scope="gen:12",
        permission_request_handler=handler,
    )

    assert result == {"allow": True, "always": True}
    assert len(calls) == 1
    assert calls[0]["loom_conv_id"] == 77
    assert calls[0]["source"] == "codex_app_server"
    assert calls[0]["approval_method"] == "item/fileChange/requestApproval"
    assert calls[0]["tool_name"] == "Edit"
    assert calls[0]["permission_scope"] == "gen:12"


def test_codex_permission_hook_posts_to_loom_and_outputs_allow(monkeypatch):
    import cc_permission_hook

    calls = []

    class FakeResponse:
        def __init__(self, body=b"{}"):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self._body

    def fake_urlopen(req, timeout=None, context=None):
        body = json.loads(req.data.decode("utf-8"))
        calls.append((req.full_url, body, timeout))
        if req.full_url.endswith("/api/cc-permission"):
            return FakeResponse(json.dumps({"allow": True}).encode("utf-8"))
        return FakeResponse()

    stdin = io.StringIO(json.dumps({
        "hook_event_name": "PermissionRequest",
        "tool": {
            "name": "shell_command",
            "arguments": {"command": "Set-Content proof.txt APPROVED"},
        },
    }))
    stdout = io.StringIO()

    monkeypatch.setenv("LOOM_CONV_ID", "42")
    monkeypatch.setenv("LOOM_PERMISSION_SCOPE", "gen:314")
    monkeypatch.setenv("LOOM_PORT", "3000")
    monkeypatch.setattr(sys, "argv", ["cc_permission_hook.py", "--event", "PermissionRequest"])
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(cc_permission_hook.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(SystemExit) as exit_info:
        cc_permission_hook.main()

    assert exit_info.value.code == 0
    permission_calls = [c for c in calls if c[0].endswith("/api/cc-permission")]
    assert len(permission_calls) == 1
    sent_payload = permission_calls[0][1]
    assert sent_payload["loom_conv_id"] == "42"
    assert sent_payload["permission_scope"] == "gen:314"
    assert sent_payload["source"] == "claude_code"
    assert sent_payload["tool_name"] == "shell_command"
    assert sent_payload["tool_input"] == {"command": "Set-Content proof.txt APPROVED"}

    hook_output = json.loads(stdout.getvalue())
    assert hook_output["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert hook_output["hookSpecificOutput"]["decision"]["behavior"] == "allow"


async def test_loom_permission_endpoint_blocks_until_user_decision(tmp_database):
    import database as db
    import server

    server._pending_hook_permissions.clear()
    server._auto_approve_permissions.clear()
    conv = await db.create_conversation(
        "Permission Test",
        mode="codex",
        project_dir=str(server.Path.cwd()),
    )

    request_task = asyncio.create_task(server.handle_cc_permission({
        "loom_conv_id": conv["id"],
        "tool_name": "Bash",
        "tool_input": {"command": "Set-Content proof.txt APPROVED"},
    }))

    for _ in range(50):
        if server._pending_hook_permissions:
            break
        await asyncio.sleep(0.02)

    assert len(server._pending_hook_permissions) == 1
    request_id, pending = next(iter(server._pending_hook_permissions.items()))
    conn = await db.get_db()
    rows = await conn.execute_fetchall(
        "SELECT conv_id, tool_name, input_summary FROM pending_permissions WHERE request_id = ?",
        (request_id,),
    )
    assert len(rows) == 1
    assert rows[0]["conv_id"] == conv["id"]
    assert rows[0]["tool_name"] == "Bash"
    assert json.loads(rows[0]["input_summary"]) == "Set-Content proof.txt APPROVED"

    try:
        assert not request_task.done()
        pending["response"] = {"allow": False}
        pending["event"].set()

        response = await asyncio.wait_for(request_task, timeout=1)
        assert response == {
            "allow": False,
            "message": "Denied by user in Loom UI",
            "decision_source": "user",
        }
        assert request_id not in server._pending_hook_permissions
    finally:
        if not request_task.done():
            request_task.cancel()


def test_permission_command_guard_blocks_nested_goose_cmd_delete():
    import server

    tool_input = {
        "toolCall": {
            "title": "shell",
            "arguments": {
                "command": 'cmd /c rmdir /s /q "C:\\Users\\USERNAME\\OneDrive\\Documents\\Design-Language"',
            },
        },
        "params": {
            "toolCall": {
                "kind": "execute",
                "name": "shell",
            },
        },
    }

    reason = server._unsafe_shell_command_reason("shell", tool_input)

    assert "recursive rmdir" in reason


def test_permission_command_guard_allows_non_destructive_windows_probe():
    import server

    reason = server._unsafe_shell_command_reason(
        "Bash",
        {"command": 'cd "C:/Users/USERNAME/OneDrive/Documents/Design-Language" && git status --short'},
    )

    assert reason == ""


def test_destructive_git_is_approval_gated_instead_of_blocked():
    import server

    tool_input = {"command": "git reset --hard HEAD"}
    assert server._unsafe_shell_command_reason("Bash", tool_input) == ""
    assert "discards tracked workspace changes" in server._destructive_git_command_reason(
        "Bash", tool_input
    )


@pytest.mark.parametrize(
    "command, reason_fragment",
    [
        ("git clean -fd", "untracked workspace files"),
        ("git checkout -f main", "discard workspace changes"),
        ("git switch --discard-changes main", "discard workspace changes"),
        ("git push origin main --force-with-lease", "remote branch history"),
        ("git push origin --delete obsolete", "deleting a remote Git branch"),
    ],
)
def test_destructive_git_variants_require_loom_approval(command, reason_fragment):
    import server

    assert reason_fragment in server._destructive_git_command_reason(
        "Bash", {"command": command}
    )


async def test_destructive_git_permission_waits_for_one_time_user_decision(tmp_database):
    import database as db
    import server

    server._pending_hook_permissions.clear()
    server._auto_approve_permissions.clear()
    conv = await db.create_conversation(
        "Destructive Git Approval",
        mode="codex",
        project_dir=str(server.Path.cwd()),
    )
    tool_input = {"command": "git restore -- static/chat.js"}
    fingerprint = server._permission_fingerprint("Bash", tool_input)
    server._auto_approve_permissions[(conv["id"], "manual")] = {fingerprint}

    request_task = asyncio.create_task(
        server.handle_cc_permission(
            {
                "loom_conv_id": conv["id"],
                "tool_name": "Bash",
                "tool_input": tool_input,
            }
        )
    )
    for _ in range(50):
        if server._pending_hook_permissions:
            break
        await asyncio.sleep(0.02)

    assert len(server._pending_hook_permissions) == 1
    request_id, pending = next(iter(server._pending_hook_permissions.items()))
    assert pending["risk_level"] == "destructive"
    assert pending["supports_allow_all"] is False
    assert "overwrites current workspace" in pending["risk_reason"]
    try:
        pending["response"] = {"allow": False, "always": False}
        pending["event"].set()
        response = await asyncio.wait_for(request_task, timeout=1)
        assert response["allow"] is False
    finally:
        server._pending_hook_permissions.pop(request_id, None)
        server._auto_approve_permissions.clear()
        if not request_task.done():
            request_task.cancel()


def test_permission_command_guard_extracts_goose_json_arguments():
    import server

    reason = server._unsafe_shell_command_reason(
        "shell",
        {
            "toolCall": {
                "name": "shell",
                "arguments": '{"command": "cmd /c del /s /q C:\\\\tmp\\\\bad"}',
            }
        },
    )

    assert "recursive del" in reason


def test_permission_command_guard_allows_native_goose_tree_tool():
    import server

    reason = server._unsafe_shell_command_reason(
        "tree · C:/Users/USERNAME/OneDrive/Documents/Design-Language",
        {
            "toolCall": {
                "title": "tree",
                "arguments": {"path": "C:/Users/USERNAME/OneDrive/Documents/Design-Language"},
            }
        },
    )

    assert reason == ""
    assert server._goose_auto_allow_reason(
        "tree · C:/Users/USERNAME/OneDrive/Documents/Design-Language",
        {
            "toolCall": {
                "title": "tree",
                "arguments": {"path": "C:/Users/USERNAME/OneDrive/Documents/Design-Language"},
            }
        },
    ) == "Goose native tree tool"


def test_permission_command_guard_blocks_unbounded_shell_scans():
    import server

    assert "tree scan" in server._unsafe_shell_command_reason(
        "shell",
        {"command": "cmd /c tree C:\\Users\\USERNAME\\OneDrive\\Documents\\Design-Language /f"},
    )
    assert "recursive directory scan" in server._unsafe_shell_command_reason(
        "shell",
        {"command": 'powershell -NoProfile -Command "Get-ChildItem -Recurse C:\\Users\\USERNAME"'},
    )


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "Fix tree send viewport and lifecycle status layout"',
        'powershell -Command \'git commit -m "Fix tree send viewport"\'',
        (
            '"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
            '-Command \'git commit -m "Fix tree send viewport and lifecycle status layout"\''
        ),
        'Write-Output "tree"',
        'git commit -m "Document dir /s and ls -R safeguards"',
    ],
)
def test_permission_command_guard_ignores_scan_words_in_quoted_arguments(command):
    import server

    assert server._unsafe_shell_command_reason(
        "Bash", {"command": command}
    ) == ""


async def test_permission_request_falls_back_to_exact_origin_socket(tmp_database):
    import database as db
    import server

    class Socket:
        def __init__(self):
            self.events = []
            self._client_instance_id = "window-origin"

        async def send_json(self, payload):
            self.events.append(payload)

    server._pending_hook_permissions.clear()
    server._auto_approve_permissions.clear()
    conv = await db.create_conversation(
        "Permission Origin Fallback",
        mode="codex",
        project_dir=str(server.Path.cwd()),
    )
    socket = Socket()
    other_socket = Socket()
    other_socket._client_instance_id = "window-other"
    server._active_websockets[conv["id"]] = {socket, other_socket}
    server._event_router.register(socket, conv["id"])
    server._event_router.register(other_socket, conv["id"])
    # Both pages may legitimately observe the same generation (for example,
    # one is sitting on a queued descendant), but only its launch page owns
    # permission decisions.
    server._event_router.subscribe(socket, "gen:991")
    server._event_router.subscribe(other_socket, "gen:991")
    request_task = asyncio.create_task(
        server.handle_cc_permission(
            {
                "loom_conv_id": conv["id"],
                "tool_name": "Bash",
                "tool_input": {"command": "git status --short"},
                "permission_scope": "gen:991",
                "client_instance_id": "window-origin",
            }
        )
    )
    try:
        for _ in range(50):
            if socket.events and server._pending_hook_permissions:
                break
            await asyncio.sleep(0.02)
        assert socket.events
        assert all(event["type"] == "permission_notice" for event in other_socket.events)
        assert socket.events[0]["type"] == "permission_request"
        assert socket.events[0]["run_id"] == "gen:991"
        request_id, pending = next(iter(server._pending_hook_permissions.items()))
        pending["response"] = {"allow": False, "always": False}
        pending["event"].set()
        result = await asyncio.wait_for(request_task, timeout=1)
        assert result["allow"] is False
        assert request_id not in server._pending_hook_permissions
    finally:
        if not request_task.done():
            request_task.cancel()
        server._event_router.unregister(socket)
        server._event_router.unregister(other_socket)
        server._active_websockets.pop(conv["id"], None)


async def test_pending_permission_replays_to_exact_origin_without_subscription(
    tmp_database,
):
    import database as db
    import server

    class Socket:
        def __init__(self):
            self.events = []
            self._client_instance_id = "window-origin"

        async def send_json(self, payload):
            self.events.append(payload)

    conv = await db.create_conversation(
        "Permission Origin Replay",
        mode="codex",
        project_dir=str(server.Path.cwd()),
    )
    await db.save_pending_permission(
        "pending-origin",
        conv["id"],
        "Bash",
        {"command": "git status --short"},
        "git status --short",
        run_id="gen:992",
        client_instance_id="window-origin",
        permission_scope="gen:992",
    )
    socket = Socket()
    server._event_router.register(socket, conv["id"])
    try:
        await server._send_pending_permissions_to_socket(socket, conv["id"])
        assert len(socket.events) == 1
        assert socket.events[0]["request_id"] == "pending-origin"
        assert socket.events[0]["run_id"] == "gen:992"
    finally:
        server._event_router.unregister(socket)


async def test_pending_permission_does_not_replay_to_another_run_observer(
    tmp_database,
):
    import database as db
    import server

    class Socket:
        def __init__(self, client_id):
            self.events = []
            self._client_instance_id = client_id

        async def send_json(self, payload):
            self.events.append(payload)

    conv = await db.create_conversation(
        "Permission Observer Isolation",
        mode="codex",
        project_dir=str(server.Path.cwd()),
    )
    await db.save_pending_permission(
        "pending-origin-isolated",
        conv["id"],
        "Bash",
        {"command": "git status --short"},
        "git status --short",
        run_id="gen:993",
        client_instance_id="window-origin",
        permission_scope="gen:993",
    )
    origin = Socket("window-origin")
    observer = Socket("window-observer")
    server._event_router.register(origin, conv["id"])
    server._event_router.register(observer, conv["id"])
    server._event_router.subscribe(origin, "gen:993")
    server._event_router.subscribe(observer, "gen:993")
    try:
        await server._send_pending_permissions_to_socket(observer, conv["id"])
        await server._send_pending_permissions_to_socket(origin, conv["id"])
        assert observer.events == []
        assert [event["request_id"] for event in origin.events] == [
            "pending-origin-isolated"
        ]
    finally:
        server._event_router.unregister(origin)
        server._event_router.unregister(observer)


async def test_pending_permission_replays_to_exact_draft_after_origin_reload(
    tmp_database,
):
    import database as db
    import server

    class Socket:
        def __init__(self, client_id):
            self.events = []
            self._client_instance_id = client_id

        async def send_json(self, payload):
            self.events.append(payload)

    conv = await db.create_conversation(
        "Permission Reload Recovery",
        mode="codex",
        project_dir=str(server.Path.cwd()),
    )
    prompt = await db.add_message(conv["id"], "user", "run it")
    draft = await db.add_message(
        conv["id"], "assistant", "partial", parent_id=prompt["id"]
    )
    sibling = await db.add_message(conv["id"], "user", "other root")
    await db.save_pending_permission(
        "pending-reloaded-origin",
        conv["id"],
        "Bash",
        {"command": "git status --short"},
        "git status --short",
        run_id="gen:994",
        parent_id=prompt["id"],
        draft_msg_id=draft["id"],
        client_instance_id="window-before-reload",
        permission_scope="gen:994",
    )
    replacement = Socket("window-after-reload")
    unrelated = Socket("window-other-branch")
    await db.select_loom_cursor(
        conv["id"], replacement._client_instance_id,
        draft["loom_thread_id"], draft["id"],
    )
    await db.select_loom_cursor(
        conv["id"], unrelated._client_instance_id,
        sibling["loom_thread_id"], sibling["id"],
    )
    server._event_router.register(replacement, conv["id"])
    server._event_router.register(unrelated, conv["id"])
    server._event_router.subscribe(replacement, "gen:994")
    server._event_router.subscribe(unrelated, "gen:994")
    try:
        await server._send_pending_permissions_to_socket(unrelated, conv["id"])
        await server._send_pending_permissions_to_socket(replacement, conv["id"])
        assert unrelated.events == []
        assert [event["request_id"] for event in replacement.events] == [
            "pending-reloaded-origin"
        ]
    finally:
        server._event_router.unregister(replacement)
        server._event_router.unregister(unrelated)


async def test_loom_permission_endpoint_warns_instead_of_auto_denial(tmp_database):
    import database as db
    import server

    server._pending_hook_permissions.clear()
    server._auto_approve_permissions.clear()
    conv = await db.create_conversation(
        "Unsafe Permission Test",
        mode="goose",
        project_dir=str(server.Path.cwd()),
    )

    request_task = asyncio.create_task(server.handle_cc_permission({
        "loom_conv_id": conv["id"],
        "tool_name": "shell",
        "tool_input": {
            "toolCall": {
                "title": "shell",
                "arguments": {
                    "command": "powershell -NoProfile -Command \"Remove-Item -Recurse -Force .\"",
                },
            },
        },
    }))
    for _ in range(100):
        if server._pending_hook_permissions:
            break
        await asyncio.sleep(.01)
    assert not request_task.done()
    rid, pending = next(iter(server._pending_hook_permissions.items()))
    assert pending["policy"]["decision"] == "ask_with_warning"
    pending["response"] = {"allow": True, "decision_source": "user"}
    pending["event"].set()
    response = await asyncio.wait_for(request_task, 2)
    assert response["allow"] is True
    assert response["decision_source"] == "user"
    await db.delete_pending_permission(rid)
    assert not server._pending_hook_permissions

    conn = await db.get_db()
    rows = await conn.execute_fetchall(
        "SELECT request_id FROM pending_permissions WHERE conv_id = ?",
        (conv["id"],),
    )
    assert rows == []
    audits = await db.get_permission_policy_audits(conv["id"])
    assert len(audits) == 1
    assert audits[0]["legacy_outcome"] == "not_evaluated"
    assert audits[0]["policy_decision"] == "ask_with_warning"
    assert audits[0]["disagrees"] is False


async def test_loom_permission_endpoint_auto_allows_workspace_goose_tree(tmp_database, tmp_path):
    import database as db
    import server

    server._pending_hook_permissions.clear()
    server._auto_approve_permissions.clear()
    conv = await db.create_conversation(
        "Safe Goose Permission Test",
        mode="goose",
        project_dir=str(tmp_path),
    )

    response = await server.handle_cc_permission({
        "loom_conv_id": conv["id"],
        "tool_name": "tree",
        "tool_input": {
            "toolCall": {
                "title": "tree",
                "arguments": {"path": str(tmp_path)},
            },
            "params": {
                "toolCall": {
                    "title": "tree",
                    "arguments": {"path": str(tmp_path)},
                },
            },
        },
    })

    assert response["allow"] is True
    assert "Loom balanced policy" in response["message"]
    assert not server._pending_hook_permissions

    conn = await db.get_db()
    rows = await conn.execute_fetchall(
        "SELECT request_id FROM pending_permissions WHERE conv_id = ?",
        (conv["id"],),
    )
    assert rows == []
    audits = await db.get_permission_policy_audits(conv["id"])
    assert len(audits) == 1
    assert audits[0]["legacy_outcome"] == "not_evaluated"
    assert audits[0]["policy_decision"] == "auto_allow"
    assert audits[0]["disagrees"] is False


def test_goose_auto_allow_detects_todo_bookkeeping():
    import server

    reason = server._goose_auto_allow_reason(
        "Write",
        {
            "toolCall": {
                "title": "Write",
                "arguments": {
                    "path": ".goose/todos.json",
                    "content": "{\"todos\": []}",
                },
            },
            "params": {"toolCall": {"title": "Write"}},
        },
    )

    assert reason == "Goose todo bookkeeping"


def test_loom_permission_fingerprint_scopes_remembered_grants():
    import server

    assert server._permission_fingerprint(
        "Bash",
        {"command": "git status"},
    ) == "Bash"
    assert server._permission_fingerprint(
        "Edit",
        {"file_path": "proof.txt"},
    ) == "Edit"
    assert server._permission_fingerprint(
        "Bash",
        {"command": "git status"},
        "item/commandExecution/requestApproval",
    ) == "item/commandExecution/requestApproval:git status"
    assert server._permission_fingerprint(
        "Edit",
        {"fileChanges": [{"path": "proof.txt"}]},
        "item/fileChange/requestApproval",
    ) == 'item/fileChange/requestApproval:{"fileChanges":[{"path":"proof.txt"}]}'
    assert server._permission_fingerprint(
        "Edit",
        {"fileChanges": [{"path": "other.txt"}]},
        "item/fileChange/requestApproval",
    ) != server._permission_fingerprint(
        "Edit",
        {"fileChanges": [{"path": "proof.txt"}]},
        "item/fileChange/requestApproval",
    )

    requested_a = {"filesystem": {"entries": [{"path": "repo", "access": "write"}]}}
    requested_b = {"filesystem": {"entries": [{"path": "repo/.git", "access": "write"}]}}
    assert server._permission_fingerprint(
        "PermissionRequest",
        {"permissions": requested_a},
        "item/permissions/requestApproval",
    ) != server._permission_fingerprint(
        "PermissionRequest",
        {"permissions": requested_b},
        "item/permissions/requestApproval",
    )


async def test_codex_diagnostics_reports_target_folder(tmp_database, tmp_path):
    import database as db
    import server

    conv = await db.create_conversation(
        "Diagnostics Test",
        mode="codex",
        project_dir=str(tmp_path),
    )

    result = await server.codex_diagnostics(conv_id=conv["id"])

    assert result["target_dir"] == str(tmp_path.resolve())
    assert result["expected_launch"]["approval_policy"] == "on-request"
    assert result["expected_launch"]["surface"] == "app-server"
    assert result["expected_launch"]["hook_scope"] == "disabled"
    assert result["expected_launch"]["sandbox"] == "workspace-write"
    assert result["expected_launch"]["writable_roots"] == [str(tmp_path.resolve())]
    assert result["project_hook"]["ignored_by_loom_codex"] is True
    assert result["project_hook"]["path"].endswith(".codex\\hooks.json") or result["project_hook"]["path"].endswith(".codex/hooks.json")


def test_codex_diff_events_normalize_to_edit_tool_payload():
    import codex_client

    raw = {
        "method": "turn/diff/updated",
        "params": {
            "threadId": "thr",
            "turnId": "turn",
            "diff": "diff --git a/a.txt b/a.txt\n+hello",
        },
    }

    assert codex_client._codex_diff_tool_id(raw) == "codex-diff:turn"
    payload = codex_client._codex_diff_payload(raw)
    assert payload["kind"] == "codex_diff"
    assert payload["threadId"] == "thr"
    assert payload["turnId"] == "turn"
    assert payload["diff"].startswith("diff --git")


def test_only_codex_turn_status_notifications_can_finalize_turns():
    import codex_client

    completed = {
        "method": "thread/status/changed",
        "params": {"threadId": "thr", "status": "idle"},
    }
    failed = {
        "method": "turn/status/changed",
        "params": {
            "turn": {"id": "turn-1", "status": "failed"},
            "error": {"message": "boom"},
        },
    }

    assert codex_client._codex_terminal_status(completed) == (False, False, "")
    assert codex_client._codex_turn_id(failed) == "turn-1"
    assert codex_client._codex_terminal_status(failed) == (True, True, "boom")


def test_codex_status_value_unwraps_object_status_payload():
    import codex_client

    nested_idle = {
        "method": "thread/status/changed",
        "params": {"thread": {"id": "thr", "status": {"type": "idle"}}},
    }
    assert codex_client._codex_status_value(nested_idle) == "idle"
    assert codex_client._codex_terminal_status(nested_idle) == (False, False, "")

    nested_failed = {
        "method": "turn/status/changed",
        "params": {
            "turn": {"id": "turn-1", "status": {"type": "failed"}},
            "error": {"message": "boom"},
        },
    }
    assert codex_client._codex_status_value(nested_failed) == "failed"
    assert codex_client._codex_terminal_status(nested_failed) == (True, True, "boom")


def test_permission_scope_gen_id_parses_generation_scope():
    import server

    assert server._permission_scope_gen_id("gen:42") == 42
    assert server._permission_scope_gen_id("manual") is None


def test_test_permission_request_hook_decision_output():
    from tools import test_permission_request_hook

    allow_output = test_permission_request_hook._hook_decision({"allow": True})
    assert allow_output["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert allow_output["hookSpecificOutput"]["decision"]["behavior"] == "allow"

    deny_output = test_permission_request_hook._hook_decision({
        "allow": False,
        "message": "nope",
    })
    assert deny_output["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert deny_output["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    assert deny_output["hookSpecificOutput"]["decision"]["message"] == "nope"


def test_permission_hook_denies_image_reads(monkeypatch):
    import cc_permission_hook

    stdin = io.StringIO(json.dumps({
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"filePath": "some/path/test_image.png"},
    }))
    stdout = io.StringIO()

    monkeypatch.setenv("LOOM_CONV_ID", "42")
    monkeypatch.setenv("LOOM_PORT", "3000")
    monkeypatch.setenv("LOOM_USE_LLAMA", "1")
    monkeypatch.setattr(sys, "argv", ["cc_permission_hook.py", "PreToolUse"])
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)

    with pytest.raises(SystemExit) as exit_info:
        cc_permission_hook.main()

    # The permission hook should exit with sys.exit(0) but deny output
    assert exit_info.value.code == 0
    hook_output = json.loads(stdout.getvalue())
    assert hook_output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Reading binary image file" in hook_output["hookSpecificOutput"]["permissionDecisionReason"]


def test_codex_permission_feedback_steers_only_meaningful_notes():
    import codex_client

    assert codex_client._permission_feedback_text(
        {"feedback": "Use the reviewed destination instead."},
        allow=False,
    ) == (
        '<loom_permission_feedback result="denied" source="user">'
        "Use the reviewed destination instead."
        "</loom_permission_feedback>"
    )
    assert codex_client._permission_feedback_text(
        {"message": "Denied by user in Loom UI"},
        allow=False,
    ) == ""
    assert "disk format" in codex_client._permission_feedback_text(
        {
            "decision_source": "loom_policy",
            "message": "Blocked by Loom command safeguard: disk format is blocked.",
        },
        allow=False,
    )
    lifecycle = codex_client._permission_feedback_text(
        {
            "decision_source": "loom_lifecycle",
            "message": "The owning turn ended before a user decision.",
        },
        allow=False,
    )
    assert 'source="loom_lifecycle"' in lifecycle
    assert 'result="cancelled"' in lifecycle
    assert "owning turn ended" in lifecycle


@pytest.mark.parametrize(
    "command",
    [
        "Get-Item proof.txt | Format-List FullName,Length",
        "Get-Process | Format-Table Name,Id",
        'powershell -NoProfile -Command "Get-Item proof.txt | Format-List FullName"',
    ],
)
def test_permission_guard_distinguishes_formatting_cmdlets_from_disk_format(command):
    import server

    assert server._unsafe_shell_command_reason("Bash", {"command": command}) == ""


def test_permission_guard_presents_mutating_node_plan_instead_of_auto_denying():
    import server

    command = (
        'node src/example-cli.js apply-plan --resource 999999 '
        '--plan "C:\\example-workspace\\plans\\reviewed-plan.json" '
        '--apply'
    )
    tool_name = f"shell · {command}"
    tool_input = {
        "toolCall": {
            "title": tool_name,
            "arguments": {"command": command},
        }
    }
    assert server._unsafe_shell_command_reason(tool_name, tool_input) == ""


@pytest.mark.parametrize(
    "command",
    [
        "format C: /Q",
        "format.com D: /FS:NTFS",
        'cmd /c "format E: /Q"',
        "diskpart /s wipe.txt",
    ],
)
def test_permission_guard_blocks_actual_disk_management_executables(command):
    import server

    reason = server._unsafe_shell_command_reason("Bash", {"command": command})
    assert "format" in reason or "diskpart" in reason


async def test_loom_permission_endpoint_returns_user_feedback_to_adapter(tmp_database):
    import database as db
    import server

    server._pending_hook_permissions.clear()
    conv = await db.create_conversation(
        "Permission Feedback",
        mode="goose",
        project_dir=str(server.Path.cwd()),
    )
    request_task = asyncio.create_task(
        server.handle_cc_permission(
            {
                "loom_conv_id": conv["id"],
                "tool_name": "shell",
                "tool_input": {"command": "Copy-Item source target"},
                "source": "goose_acp",
            }
        )
    )
    for _ in range(50):
        if server._pending_hook_permissions:
            break
        await asyncio.sleep(0.02)
    request_id, pending = next(iter(server._pending_hook_permissions.items()))
    try:
        pending["response"] = {
            "allow": False,
            "always": False,
            "feedback": "Use the FA26 drafts folder, not the source folder.",
        }
        pending["event"].set()
        response = await asyncio.wait_for(request_task, timeout=1)
        assert response["allow"] is False
        assert response["decision_source"] == "user"
        assert response["feedback"] == "Use the FA26 drafts folder, not the source folder."
        assert response["message"] == response["feedback"]
        assert pending["source"] == "goose_acp"
    finally:
        server._pending_hook_permissions.pop(request_id, None)
        if not request_task.done():
            request_task.cancel()


async def test_permission_lifecycle_cancellation_is_not_reported_as_user_denial(
    tmp_database,
):
    import database as db
    import server

    server._pending_hook_permissions.clear()
    conv = await db.create_conversation(
        "Permission Lifecycle",
        mode="goose",
        project_dir=str(server.Path.cwd()),
    )
    request_task = asyncio.create_task(
        server.handle_cc_permission(
            {
                "loom_conv_id": conv["id"],
                "tool_name": "shell",
                "tool_input": {"command": "Copy-Item source target"},
                "source": "goose_acp",
                "permission_scope": "gen:991",
            }
        )
    )
    for _ in range(50):
        if server._pending_hook_permissions:
            break
        await asyncio.sleep(0.02)
    request_id, pending = next(iter(server._pending_hook_permissions.items()))
    try:
        await server._resolve_pending_permissions_for_scope(
            conv["id"],
            "gen:991",
            message="Owning agent turn ended before a user decision.",
        )
        response = await asyncio.wait_for(request_task, timeout=1)
        assert response["allow"] is False
        assert response["decision_source"] == "loom_lifecycle"
        assert response["message"] == "Owning agent turn ended before a user decision."
        assert response["message"] != "Denied by user in Loom UI"

        rows = await db.get_permission_resolutions_for_draft(
            pending.get("draft_msg_id") or -1
        )
        # This manual fixture has no draft, so verify the audit record directly.
        conn = await db.get_db()
        row = await conn.execute_fetchall(
            "SELECT decision_source, feedback FROM permission_resolutions WHERE request_id = ?",
            (request_id,),
        )
        assert rows == []
        assert row[0]["decision_source"] == "loom_lifecycle"
        assert "before a user decision" in row[0]["feedback"]
    finally:
        server._pending_hook_permissions.pop(request_id, None)
        if not request_task.done():
            request_task.cancel()


async def test_permission_resolution_persists_feedback_for_acp_continuation(tmp_database):
    import database as db

    conv = await db.create_conversation("Permission Audit", mode="goose")
    user = await db.add_message(conv["id"], "user", "copy it")
    draft = await db.add_message(
        conv["id"], "assistant", "partial", parent_id=user["id"]
    )
    await db.save_permission_resolution(
        "permission-1",
        conv["id"],
        "shell",
        False,
        "Use the reviewed destination.",
        source="goose_acp",
        draft_msg_id=draft["id"],
    )

    rows = await db.get_permission_resolutions_for_draft(draft["id"])
    assert len(rows) == 1
    assert rows[0]["allowed"] is False
    assert rows[0]["feedback"] == "Use the reviewed destination."
    assert rows[0]["source"] == "goose_acp"


async def test_codex_permission_note_is_steered_into_the_active_turn(
    monkeypatch, tmp_path
):
    import codex_client

    class FakeStdin:
        def __init__(self, proc):
            self.proc = proc
            self.closed = False

        def is_closing(self):
            return self.closed

        def close(self):
            self.closed = True

        def write(self, payload):
            request = json.loads(payload.decode("utf-8"))
            method = request.get("method")
            rid = request.get("id")
            if method == "initialize":
                self.proc.feed({"id": rid, "result": {"userAgent": "codex-test/1"}})
            elif method == "config/read":
                self.proc.feed({"id": rid, "result": {"config": {}}})
            elif method == "thread/start":
                self.proc.feed({
                    "id": rid,
                    "result": {
                        "thread": {"id": "thread-feedback"},
                        "model": "gpt-5.6-sol",
                        "modelProvider": "openai",
                    },
                })
            elif method == "turn/start":
                self.proc.feed({"id": rid, "result": {"turn": {"id": "turn-feedback"}}})
                asyncio.get_running_loop().call_soon(self.proc.ask_permission)
            elif method == "turn/steer":
                self.proc.steer_params = request["params"]
                self.proc.feed({"id": rid, "result": {"turnId": "turn-feedback"}})
                asyncio.get_running_loop().call_soon(self.proc.finish_turn)
            elif rid == "approval-feedback":
                self.proc.approval_result = request.get("result")

        async def drain(self):
            await asyncio.sleep(0)

    class FakeProcess:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdin = FakeStdin(self)
            self.returncode = None
            self.pid = 9876
            self._exited = asyncio.Event()
            self.steer_params = None
            self.approval_result = None

        def feed(self, value):
            self.stdout.feed_data((json.dumps(value) + "\n").encode())

        def ask_permission(self):
            self.feed({
                "id": "approval-feedback",
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "thread-feedback",
                    "turnId": "turn-feedback",
                    "itemId": "item-1",
                    "startedAtMs": 1,
                    "command": "Copy-Item source target",
                },
            })

        def finish_turn(self):
            self.feed({
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-feedback",
                    "turn": {"id": "turn-feedback", "status": "completed", "items": []},
                },
            })

        async def wait(self):
            await self._exited.wait()
            return self.returncode

        def terminate(self):
            self.returncode = 0
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._exited.set()

        def kill(self):
            self.terminate()

    fake = FakeProcess()

    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake

    async def permission_handler(payload):
        assert payload["source"] == "codex_app_server"
        return {
            "allow": False,
            "feedback": "Use the reviewed destination instead.",
        }

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(codex_client, "_find_codex_exe", lambda: "codex")
    monkeypatch.setattr(codex_client, "_thread_mcp_servers", lambda *a, **k: {})
    monkeypatch.setattr(codex_client.sys, "platform", "linux")

    _proc, event_stream = await codex_client.run_codex(
        "copy it",
        str(tmp_path),
        conv_id=77,
        model="codex-gpt-5.6-sol",
        permission_request_handler=permission_handler,
    )
    async with asyncio.timeout(2):
        async for _event in event_stream:
            pass

    assert fake.approval_result == {"decision": "decline"}
    assert fake.steer_params["threadId"] == "thread-feedback"
    assert fake.steer_params["expectedTurnId"] == "turn-feedback"
    assert "Use the reviewed destination instead." in fake.steer_params["input"][0]["text"]


async def test_websocket_permission_feedback_is_persisted_with_exact_draft(
    tmp_database,
):
    import database as db
    import server
    from starlette.websockets import WebSocketDisconnect

    conv = await db.create_conversation("Permission websocket", mode="goose")
    user = await db.add_message(conv["id"], "user", "copy")
    draft = await db.add_message(
        conv["id"], "assistant", "partial", parent_id=user["id"]
    )
    request_id = "permission-websocket-note"
    await db.save_pending_permission(
        request_id,
        conv["id"],
        "shell",
        {"command": "Copy-Item source target"},
        "Copy-Item source target",
        run_id="gen:900",
        parent_id=user["id"],
        draft_msg_id=draft["id"],
        client_instance_id="window-note",
        permission_scope="gen:900",
        source="goose_acp",
    )
    server._pending_hook_permissions[request_id] = {
        "event": asyncio.Event(),
        "response": None,
        "conv_id": conv["id"],
        "tool_name": "shell",
        "tool_input": {"command": "Copy-Item source target"},
        "approval_method": "PreToolUse",
        "permission_scope": "gen:900",
        "run_id": "gen:900",
        "parent_id": user["id"],
        "draft_msg_id": draft["id"],
        "client_instance_id": "window-note",
        "source": "goose_acp",
    }

    class Socket:
        def __init__(self):
            self.events = []
            self._client_instance_id = "window-note"
            self._received = False

        async def send_json(self, payload):
            self.events.append(payload)

        async def receive_text(self):
            if self._received:
                raise WebSocketDisconnect()
            self._received = True
            return json.dumps(
                {
                    "action": "permission_response",
                    "request_id": request_id,
                    "allow": False,
                    "always": False,
                    "feedback": "Use the reviewed destination.",
                }
            )

    socket = Socket()
    server._active_websockets[conv["id"]] = {socket}
    server._event_router.register(socket, conv["id"])
    server._event_router.subscribe(socket, "gen:900")
    try:
        await server._continue_ws_chat(socket, conv["id"], "window-note")
        rows = await db.get_permission_resolutions_for_draft(draft["id"])
        assert len(rows) == 1
        assert rows[0]["request_id"] == request_id
        assert rows[0]["feedback"] == "Use the reviewed destination."
        assert rows[0]["source"] == "goose_acp"
        assert server._pending_hook_permissions[request_id]["response"]["feedback"] == (
            "Use the reviewed destination."
        )
        assert any(
            event.get("type") == "permission_resolved"
            and event.get("feedback") == "Use the reviewed destination."
            for event in socket.events
        )
    finally:
        server._pending_hook_permissions.pop(request_id, None)
        server._event_router.unregister(socket)
        server._active_websockets.pop(conv["id"], None)
