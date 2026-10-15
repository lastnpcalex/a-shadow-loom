"""Production-boundary tests for tool-free preparation.

Rule Zero: these tests capture values from real production code paths
(child environment construction, outbound JSON-RPC requests, provider
events) and never manufacture the outcome they assert.

Layers:
- Live-process environment contamination (sequential launches, one process).
- Goose ACP preparation handshake: mcpServers==[], tool-advertising abort.
- Real model attestation (verified status, exact normalized equality).
"""
import asyncio
import json
import os
import subprocess
import sys

import pytest

import claude_client
import goose_client
from context_preparation import _attestation_verified
from preparation_errors import PreparationTransportError


# ---------------------------------------------------------------------------
# 1. Environment contamination (sequential launches, ONE process)
# ---------------------------------------------------------------------------

class _SilentProc:
    """Fake child process with real silent pipes."""

    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.pid = 424242

    def poll(self):
        return 0

    def kill(self):
        pass

    def terminate(self):
        pass

    def wait(self):
        return asyncio.sleep(0)


async def _spawned_envs(monkeypatch, coro_factory):
    captured = []

    async def fake_exec(*cmd, **kwargs):
        captured.append((cmd, kwargs))
        return _SilentProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await coro_factory()
    return captured


async def _drain(stream):
    async for _ in stream:
        pass


async def test_preparation_then_ordinary_launch_do_not_contaminate(monkeypatch, tmp_path):
    os_snapshot = dict(os.environ)

    async def prep_launch():
        proc, stream = await claude_client.run_claude(
            "prepare state", str(tmp_path / "prep"), conv_id=0, server_port=3000,
            model="test-model", requested_model="test-model",
            permission_mode="default", preparation=True)
        await _drain(stream)

    spawns = await _spawned_envs(monkeypatch, prep_launch)
    assert spawns, "preparation launch never spawned a child"
    child_env = spawns[0][1]["env"]
    assert child_env.get("LOOM_PREPARATION") == "1"
    assert "LOOM_PREPARATION" not in os.environ, (
        "preparation mutated the process-global environment")
    assert os.environ == os_snapshot

    async def ordinary_launch():
        proc, stream = await claude_client.run_claude(
            "ordinary turn", str(tmp_path / "normal"), conv_id=0, server_port=3000,
            model="test-model", requested_model="test-model",
            permission_mode="default")
        await _drain(stream)

    spawns2 = await _spawned_envs(monkeypatch, ordinary_launch)
    assert spawns2, "ordinary launch never spawned a child"
    ordinary_env = spawns2[0][1]["env"]
    assert "LOOM_PREPARATION" not in ordinary_env, (
        "ordinary launch inherited a preparation lockdown")
    assert "LOOM_PREPARATION" not in os.environ


def _run_hook(env, event):
    hook = os.path.join(os.path.dirname(claude_client.__file__), "cc_permission_hook.py")
    return subprocess.run(
        [sys.executable, hook],
        input=json.dumps(event).encode("utf-8"),
        capture_output=True, timeout=60, env=env)


def test_hook_denies_every_tool_only_in_preparation_mode(tmp_path):
    event = {"tool_name": "Bash", "tool_input": {"command": "dir"}}

    env = {k: v for k, v in os.environ.items() if k != "LOOM_PREPARATION"}
    env.update({"LOOM_PORT": "1", "LOOM_CONV_ID": "0"})
    proc = _run_hook(env, event)
    combined = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    assert "preparation mode" not in combined

    env_prep = dict(env)
    env_prep["LOOM_PREPARATION"] = "1"
    proc = _run_hook(env_prep, event)
    combined = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    assert "preparation mode" in combined


# ---------------------------------------------------------------------------
# 2. Goose ACP preparation handshake
# ---------------------------------------------------------------------------

class _FakeStdin:
    def __init__(self):
        self.requests = []
        self._buf = bytearray()

    def write(self, data):
        self._buf.extend(data)
        while b"\n" in self._buf:
            line, _, rest = bytes(self._buf).partition(b"\n")
            self._buf = bytearray(rest)
            if line.strip():
                self.requests.append(json.loads(line.decode("utf-8")))

    async def drain(self):
        pass

    def close(self):
        pass

    def is_closing(self):
        return False


class _FakeACPProc:
    """A controlled external ACP agent process (real JSON-RPC on pipes)."""

    def __init__(self, agent_capabilities=None):
        self.stdin = _FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.pid = 999999
        self.agent_capabilities = agent_capabilities or {}
        self.session_new_params = None
        self.session_prompt_params = None
        self._seen = 0
        self._task = asyncio.create_task(self._serve())

    def _send(self, msg):
        self.stdout.feed_data((json.dumps(msg) + "\n").encode("utf-8"))

    async def _serve(self):
        try:
            while True:
                if self._seen >= len(self.stdin.requests):
                    await asyncio.sleep(0.01)
                    continue
                req = self.stdin.requests[self._seen]
                self._seen += 1
                method = req.get("method")
                rid = req.get("id")
                if method == "initialize":
                    self._send({"jsonrpc": "2.0", "id": rid, "result": {
                        "protocolVersion": "v1",
                        "agentInfo": {"name": "fake-goose", "version": "1"},
                        "agentCapabilities": self.agent_capabilities,
                    }})
                elif method == "session/new":
                    self.session_new_params = req.get("params") or {}
                    self._send({"jsonrpc": "2.0", "id": rid, "result": {
                        "sessionId": "fake-sess-1",
                        "models": {"currentModelId": "openrouter:z-ai/glm-5.2"},
                    }})
                elif method == "session/prompt":
                    self.session_prompt_params = req.get("params") or {}
                    self._send({"jsonrpc": "2.0", "id": rid, "result": {
                        "stopReason": "end_turn"}})
                else:
                    self._send({"jsonrpc": "2.0", "id": rid, "result": {}})
        except asyncio.CancelledError:
            pass

    def poll(self):
        return None

    def kill(self):
        self._task.cancel()

    def terminate(self):
        self._task.cancel()

    def wait(self):
        return asyncio.sleep(0)


def _goose_prep_result(monkeypatch, capabilities=None):
    procs = []

    async def fake_exec(*cmd, **kwargs):
        proc = _FakeACPProc(agent_capabilities=capabilities)
        procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def consume():
        try:
            proc, stream = await goose_client.run_goose(
                "prepare state", conv_id=0, cwd=".",
                model="goose:openrouter:z-ai/glm-5.2",
                loom_port=3000, permission_mode="default", builtins=[],
                preparation=True)
            async for evt in stream:
                if evt.get("type") == "session_info":
                    return evt
                if evt.get("type") == "error":
                    return evt
        except PreparationTransportError as exc:
            return {"type": "error", "error": exc}
        return {"type": "error", "error": RuntimeError(
            "stream ended without session_info")}

    return consume, procs


async def test_goose_preparation_session_new_has_zero_tools(monkeypatch):
    consume, procs = _goose_prep_result(monkeypatch)
    evt = await asyncio.wait_for(consume(), timeout=20)
    assert evt.get("type") == "session_info", evt
    proc = procs[0]
    proc.kill()
    params = proc.session_new_params
    assert params is not None, "session/new was never sent"
    assert params.get("mcpServers") == []


async def test_goose_preparation_aborts_on_advertised_tools(monkeypatch):
    consume, procs = _goose_prep_result(
        monkeypatch, capabilities={"tools": [{"name": "developer_shell"}]})
    evt = await asyncio.wait_for(consume(), timeout=20)
    err = evt.get("error")
    assert isinstance(err, (PreparationTransportError, str)), evt
    assert "advertised tool surfaces" in str(err), evt
    # Evidence must never be sent: session/prompt never happened.
    assert procs[0].session_prompt_params is None
    procs[0].kill()


async def test_goose_preparation_proceeds_on_zero_tool_handshake(monkeypatch):
    consume, procs = _goose_prep_result(monkeypatch)
    evt = await asyncio.wait_for(consume(), timeout=20)
    assert evt.get("type") == "session_info", evt
    procs[0].kill()


async def test_goose_ordinary_launch_keeps_its_tools(monkeypatch):
    procs = []

    async def fake_exec(*cmd, **kwargs):
        proc = _FakeACPProc()
        procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def consume():
        try:
            proc, stream = await goose_client.run_goose(
                "ordinary turn", conv_id=0, cwd=".",
                model="goose:openrouter:z-ai/glm-5.2",
                loom_port=3000, permission_mode="default", builtins=["developer"])
            async for evt in stream:
                if evt.get("type") == "session_info":
                    return evt
        except Exception as exc:
            return {"type": "error", "error": exc}
        return {"type": "error", "error": RuntimeError("no session_info")}

    evt = await asyncio.wait_for(consume(), timeout=20)
    assert evt.get("type") == "session_info", evt
    params = procs[0].session_new_params
    assert params is not None
    names = [s.get("name") for s in params.get("mcpServers") or []]
    assert names, "ordinary goose session lost its MCP tools"


# ---------------------------------------------------------------------------
# 3. Real model attestation
# ---------------------------------------------------------------------------

def test_attestation_requires_verified_exact_match():
    requested = "gpt-6-astra"

    assert _attestation_verified(None, requested) is None
    assert _attestation_verified({}, requested) is None

    # Launch label echoed with unverified status -> reject
    assert _attestation_verified(
        {"status": "configured", "launch_model": requested,
         "effective_model": requested}, requested) is None

    # Missing effective model -> reject
    assert _attestation_verified(
        {"status": "verified", "launch_model": requested,
         "effective_model": None}, requested) is None

    # Wrong effective model -> reject
    assert _attestation_verified(
        {"status": "mismatch", "launch_model": requested,
         "effective_model": "other-model"}, requested) is None

    # Similar suffix/prefix name -> reject
    assert _attestation_verified(
        {"status": "verified", "launch_model": "x" + requested,
         "effective_model": "x" + requested}, requested) is None
    assert _attestation_verified(
        {"status": "verified", "launch_model": requested + "-mini",
         "effective_model": requested + "-mini"}, requested) is None

    # Verified exact match (any case) -> accept
    assert _attestation_verified(
        {"status": "verified", "launch_model": requested,
         "effective_model": requested.upper()}, requested) == requested.upper()

    # Goose harness route prefix normalization is exact, not suffix-based
    assert _attestation_verified(
        {"status": "verified", "launch_model": "openrouter:z-ai/glm-5.2",
         "effective_model": "openrouter:z-ai/glm-5.2"},
        "goose:openrouter:z-ai/glm-5.2") == "openrouter:z-ai/glm-5.2"


async def test_transport_without_attestation_is_unverified(monkeypatch, tmp_path):
    """A provider event that only echoes a model label is request evidence,
    never effective-model evidence: the transport reports an unverified
    attestation and preparation rejects it."""
    import context_preparation

    payload = json.dumps({
        "objective": "x", "constraints": "", "decisions": "",
        "completed_effects": "", "uncertain_effects": "",
        "pending_work": "", "artifacts": "", "questions": "", "sources": ""})

    async def mock_run(*args, **kwargs):
        async def fake_stream():
            yield {"type": "text_delta", "text": payload}
            yield {"type": "result", "result_text": payload, "model": "test-model"}
        return None, fake_stream()

    import server
    monkeypatch.setattr(server.claude_client, "run_claude", mock_run)

    output, got = await context_preparation.execute_preparation_chunk(
        "prompt", {"harness": "claude", "model": "test-model", "backend": None},
        str(tmp_path), output_cap=1000)
    # An echoed launch label is request evidence, not provider evidence.
    assert got.get("status") == "unverified", got
    assert _attestation_verified(got, "test-model") is None


async def test_transport_attestation_only_from_provider_event(monkeypatch, tmp_path):
    """The accepted attestation must originate in the provider event."""
    import context_preparation
    attestation = {"status": "verified", "effective_model": "test-model",
                   "launch_model": "test-model"}

    payload = json.dumps({
        "objective": "x", "constraints": "", "decisions": "",
        "completed_effects": "", "uncertain_effects": "",
        "pending_work": "", "artifacts": "", "questions": "", "sources": ""})

    async def mock_run(*args, **kwargs):
        async def fake_stream():
            yield {"type": "session_info", "model_attestation": attestation}
            yield {"type": "text_delta", "text": payload}
            yield {"type": "result", "result_text": payload}
        return None, fake_stream()

    import server
    monkeypatch.setattr(server.claude_client, "run_claude", mock_run)

    output, got = await context_preparation.execute_preparation_chunk(
        "prompt", {"harness": "claude", "model": "test-model", "backend": None},
        str(tmp_path), output_cap=1000)
    assert got is attestation  # the object emitted by the provider event
    assert _attestation_verified(got, "test-model") == "test-model"
