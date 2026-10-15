"""Real MCP stdio -> isolated HTTP -> temporary SQLite across harness configs."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import database as db
import server
import codex_client
import goose_client
import gemini_client
from search_space import workspace_key
from search_space.scopes import resolve_scope
from tests.test_search_history import history_index, conversation
from tests.test_fullstack_queue_isolation import _eventually, _free_port


@pytest.mark.parametrize("harness,local", [("claude",False),("local",True),("dream-claude",True),
    ("openrouter",False),("umans",False),("codex",False),("gemini",False),("goose",False),
    ("goose-local",True),("goose-dream",True),("codex-https",False)])
async def test_all_harness_mcp_transports_search_own_workspace_only(tmp_path, history_index, monkeypatch, harness, local):
    conv, root = await conversation(tmp_path, history_index, mode=harness)
    own = await db.add_message(conv["id"], "user", "backup fixture")
    foreign, _ = await conversation(tmp_path, history_index, "Foreign")
    forbidden = await db.add_message(foreign["id"], "user", "backup out of scope")
    port = _free_port()
    if harness.startswith("goose"):
        cfg = next(c for c in goose_client._loom_mcp_servers(conv["id"], port, workspace_root=str(root), local_search=local)
                   if c["name"] == "loom-agent-control")
        env = {row["name"]: row["value"] for row in cfg["env"]}
    elif harness == "gemini":
        monkeypatch.setattr(gemini_client, "_ensure_hook_trusted", lambda *args: None)
        monkeypatch.setattr(gemini_client, "_agy_workspace_root", lambda path: path)
        gemini_client._configure_permission_hook(str(root), conv_id=conv["id"], server_port=port)
        cfg = json.loads((root / ".agents/mcp_config.json").read_text())["mcpServers"]["loom-agent-control"]
        env = cfg["env"]
    else:
        cfg = codex_client._agent_control_mcp_config(conv["id"], port, workspace_root=str(root), local_search=local)
        env = cfg["env"]
    assert resolve_scope(env["LOOM_HISTORY_TOKEN"]).local == local
    tls = {}
    if harness == "codex-https":
        # Generate throwaway credentials; never read the live host's private key.
        from datetime import datetime, timedelta, timezone
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1)).sign(key, hashes.SHA256()))
        (tmp_path / "test-cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (tmp_path / "test-key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        tls = {"ssl_certfile":str(tmp_path / "test-cert.pem"), "ssl_keyfile":str(tmp_path / "test-key.pem")}
    fixture = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, lifespan="off", log_level="warning", ws="websockets-sansio", **tls))
    task = asyncio.create_task(fixture.serve())
    await _eventually(lambda: fixture.started)
    try:
        params = StdioServerParameters(command=cfg["command"], args=cfg["args"], env={**os.environ, **env})
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert {"search_conversations", "read_conversation_context", "get_conversation_details"} <= names
                found = await session.call_tool("search_conversations", {"query":"backup"})
                result = json.loads(found.content[0].text)
                assert {r["message_id"] for r in result["sources"]} == {own["id"]}
                context = await session.call_tool("read_conversation_context", {"message_id":own["id"]})
                assert json.loads(context.content[0].text)["messages"][0]["content"] == "backup fixture"
                details = await session.call_tool("get_conversation_details", {"message_id":own["id"]})
                assert json.loads(details.content[0].text)["url"].endswith(f"?conversation={conv['id']}&branch={own['id']}&view=chat")
                denied = await session.call_tool("read_conversation_context", {"message_id":forbidden["id"]})
                assert "refused (403)" in denied.content[0].text
    finally:
        fixture.should_exit = True
        await asyncio.wait_for(task, timeout=10)


@pytest.mark.parametrize("flag,local", [(None,False),("use_llama",True),("use_dream",True),
                                       ("use_umans",False),("use_openrouter",False)])
async def test_claude_launch_really_passes_the_history_capability(tmp_path, monkeypatch, flag, local):
    import claude_client
    import local_context_guard
    import context_runtime
    from unittest.mock import AsyncMock
    captured = {}
    class StopLaunch(Exception):
        pass
    class Guard:
        secret = "fixture"
        def __init__(self, *args, **kwargs): pass
        async def start(self): return "http://127.0.0.1:1"
        async def close(self): pass
    async def launch(*args, **kwargs):
        captured["servers"] = json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]
        raise StopLaunch()
    monkeypatch.setattr(claude_client, "_configure_permission_hook", lambda *args: None)
    monkeypatch.setattr(local_context_guard, "LocalContextGuard", Guard)
    monkeypatch.setattr(context_runtime, "admit_rendered", AsyncMock(side_effect=lambda prompt, **kwargs: prompt))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    monkeypatch.setenv("UMANS_API_KEY", "fixture-key")
    monkeypatch.setattr(claude_client.openrouter_client, "api_key", lambda: "fixture-key")
    with pytest.raises(StopLaunch):
        await claude_client.run_claude("fixture", str(tmp_path), conv_id=42, **({flag:True} if flag else {}))
    token = captured["servers"]["loom-agent-control"]["env"]["LOOM_HISTORY_TOKEN"]
    assert resolve_scope(token).local == local


@pytest.mark.parametrize("resume,fork,method", [(None,False,"session/new"),
    ("previous",False,"session/load"),("previous",True,"session/fork")])
async def test_goose_session_handshake_renews_history_capability(tmp_path, monkeypatch, resume, fork, method):
    from unittest.mock import AsyncMock
    import context_runtime
    captured = {}
    class Input:
        def close(self): pass
        def is_closing(self): return False
    class Proc:
        returncode = 0
        stdin = Input()
        stderr = asyncio.StreamReader()
        stdout = asyncio.StreamReader()
    proc = Proc()
    proc.stderr.feed_eof()
    async def rpc(_rpc, called_method, params, *_args):
        if called_method == "initialize":
            return {"agentInfo":{"name":"fixture"}}
        captured.update(method=called_method, params=params)
        raise RuntimeError("bounded handshake probe finished")
    monkeypatch.setattr(context_runtime, "admit_rendered", AsyncMock(side_effect=lambda prompt, **kwargs: prompt))
    monkeypatch.setattr(goose_client, "_rpc_request_via_reader", rpc)
    monkeypatch.setattr(goose_client, "cancel_goose", AsyncMock())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    async def owner():
        _, stream = await goose_client.run_goose("fixture", cwd=str(tmp_path), conv_id=42,
            model="goose:openrouter:z-ai/glm-5.3-flash", resume_session_id=resume, fork_session=fork)
        events = [event async for event in stream]
        assert any(event["type"] == "error" for event in events)
        assert captured["method"] == method
        control = next(c for c in captured["params"]["mcpServers"] if c["name"] == "loom-agent-control")
        token = next(row["value"] for row in control["env"] if row["name"] == "LOOM_HISTORY_TOKEN")
        assert resolve_scope(token).workspace_key == workspace_key(tmp_path)
        return token
    token = await asyncio.create_task(owner())
    await asyncio.sleep(0)
    assert resolve_scope(token) is None
