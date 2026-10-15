"""Real MCP stdio -> isolated HTTP -> SQLite, including long-turn ownership.

No live host/model; native model-initiated callers are separately opt-in.
"""
import asyncio
import json
import os
from pathlib import Path
import sys

import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import database as db
import server
import child_context
from context_checkpoints import issue_retrieval_scope
from continuity_engine import BranchSnapshot
from tests.test_fullstack_queue_isolation import _free_port, _eventually


@pytest.fixture
async def retrieval_listener(tmp_database, monkeypatch):
    port = _free_port()
    monkeypatch.setattr(server.config, "port", port)
    certs = Path(server.__file__).parent / "certs"
    tls = all((certs / name).is_file() for name in ("cert.pem", "key.pem"))
    ssl_args = {"ssl_certfile": str(certs / "cert.pem"), "ssl_keyfile": str(certs / "key.pem")} if tls else {}
    listener = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port,
                                             ws="websockets-sansio",
                                             lifespan="off", log_level="warning",
                                             timeout_graceful_shutdown=2, **ssl_args))
    serving = asyncio.create_task(listener.serve())
    try:
        await _eventually(lambda: listener.started)
        yield port
    finally:
        listener.should_exit = True  # only this fixture-owned listener
        await asyncio.wait_for(serving, 5)


@pytest.mark.parametrize("child", [False, True], ids=["branch", "child"])
async def test_real_mcp_retrieval_survives_ttl_but_not_owner(retrieval_listener, tmp_path, child):
    conv = await db.create_conversation("Synthetic MCP retrieval")
    message = await db.add_message(conv["id"], "user", "BRANCH_ONLY_PROOF")
    release = asyncio.Event()
    ready = asyncio.Future()
    async def owner():
        if child:
            run = await db.create_agent_run("mcp-child", conv["id"], "CHILD_ONLY_PROOF")
            evidence, sequence = await child_context.snapshot(run["run_id"])
            note = await child_context.notice(run["run_id"], evidence, sequence)
            ready.set_result(note.split("context_scope:", 1)[1].strip())
        else:
            evidence = BranchSnapshot.from_messages(await db.get_branch_to_root(message["id"]))
            ready.set_result(await issue_retrieval_scope(evidence, ttl_seconds=1, owner=asyncio.current_task()))
        await release.wait()
    task = asyncio.create_task(owner())
    token = await ready
    env = {**os.environ, "LOOM_PORT": str(retrieval_listener), "LOOM_CONV_ID": str(conv["id"]),
           "LOOM_AGENT_RUN_ID": "mcp-child" if child else "", "LOOM_PARENT_RUN_ID": "",
           "LOOM_WORKSPACE_ROOT": str(tmp_path)}
    params = StdioServerParameters(command=sys.executable,
        args=[str(Path(server.__file__).parent / "mcp_loom_agents.py")], env=env)
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                async def call(**args):
                    result = await client.call_tool("retrieve_context", {"token": token, **args})
                    return "".join(c.text for c in result.content if c.type == "text")
                # Expire the persisted TTL deterministically, without moving
                # Python's global clock or sleeping through an hour.
                async with db.write_transaction() as connection:
                    await connection.execute("UPDATE context_retrieval_scopes SET expires_at=0")
                index = json.loads(await call())
                result = await call(message_id=index["messages"][0]["id"])
                assert ("CHILD_ONLY_PROOF" if child else "BRANCH_ONLY_PROOF") in result
                if child:
                    assert "BRANCH_ONLY_PROOF" not in result
                assert "failed" in (await call(message_id=999999999)).lower()
                release.set()
                await task
                assert "failed" in (await call()).lower()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
