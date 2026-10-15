"""Tests for run ownership, window routing, and lifecycle repairs.

Verifies requirements from RUN_OWNERSHIP_EXECUTION_PLAN.md:
- R3: Exact run/draft matching in api_list_generations (no overbroad conversation matching).
- R6: Privacy-safe structured WS-TRACE logging without streamed content previews, bounded send.
- R11: Startup retains and repairs stale drafts without deleting branches or queued descendants.
"""
import asyncio
import io
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.websockets import WebSocketState

import database as db
import server


async def test_r3_api_list_generations_matches_exact_draft_not_conversation(tmp_database, monkeypatch):
    """R3: api_list_generations marks in_memory only for the exact active run/draft,

    not for another generation just because it shares the same conversation.
    """
    conv = await db.create_conversation("Run Ownership Test Conv")
    conv_id = conv["id"]

    # Register two active generations in SQLite for this conversation
    # Gen 1: draft 100
    await db.register_active_generation(
        draft_msg_id=100, conv_id=conv_id, pid=None, session_id="s1",
        project_dir=".", mode="claude", loom_thread_id="t1"
    )
    # Gen 2: draft 200 (completed/stale in DB, not currently running in memory)
    await db.register_active_generation(
        draft_msg_id=200, conv_id=conv_id, pid=None, session_id="s2",
        project_dir=".", mode="claude", loom_thread_id="t1"
    )

    # In memory, ONLY gen 1 (draft 100) is running
    task1 = MagicMock()
    task1.done.return_value = False

    gen_key1 = (conv_id, 99, 1)
    mock_active = {gen_key1: task1}
    mock_snapshots = {gen_key1: {"draft_msg_id": 100, "conv_id": conv_id}}

    monkeypatch.setattr(server, "_active_generations", mock_active)
    monkeypatch.setattr(server, "_generation_snapshots", mock_snapshots)

    rows = await server.api_list_generations()
    by_draft = {r["draft_msg_id"]: r for r in rows}

    assert 100 in by_draft
    assert 200 in by_draft

    # Exact draft 100 must be in_memory True
    assert by_draft[100]["in_memory"] is True

    # Sibling draft 200 in the same conversation MUST be in_memory False (fixed R3)
    assert by_draft[200]["in_memory"] is False


async def test_r6_ws_send_logs_structured_metadata_without_content_preview(monkeypatch):
    """R6: WS-TRACE logs structured metadata (seq, run, bytes) without logging

    unconditional content text snippets.
    """
    conv_id = 42
    secret_text = "SUPER_SECRET_USER_TOKEN_ABCD1234XYZ"

    captured_logs = []
    real_print = print

    def mock_print(*args, **kwargs):
        msg = " ".join(str(a) for a in args)
        captured_logs.append(msg)

    monkeypatch.setattr("builtins.print", mock_print)

    mock_ws = AsyncMock()
    mock_ws.client_state = WebSocketState.CONNECTED
    mock_ws.application_state = WebSocketState.CONNECTED

    monkeypatch.setattr(server, "_active_websockets", {conv_id: {mock_ws}})

    # Subscribe socket to run
    server._event_router.register(mock_ws, conv_id)
    server._event_router.subscribe(mock_ws, "gen:1")

    # Send event containing sensitive content
    await server._ws_send(conv_id, {
        "type": "stream_chunk",
        "run_id": "gen:1",
        "content": secret_text,
    })

    # Verify WS-TRACE line was printed
    ws_trace_lines = [line for line in captured_logs if "[WS-TRACE]" in line]
    assert len(ws_trace_lines) >= 1

    for line in ws_trace_lines:
        # Secret content text must NOT appear in WS-TRACE
        assert secret_text not in line
        # Must contain structured metadata: bytes, run, seq
        assert "bytes=" in line
        assert "run=gen:1" in line
        assert "seq=" in line


async def test_r6_ws_send_slow_client_timeout_detaches_socket(monkeypatch):
    """R6: Slow or stalled client WebSocket times out and is removed from active sockets

    without blocking other clients or the generation loop.
    """
    conv_id = 88

    # Create a stalled websocket whose send_json hangs forever
    slow_ws = AsyncMock()
    slow_ws.client_state = WebSocketState.CONNECTED
    slow_ws.application_state = WebSocketState.CONNECTED

    slow_ws.send_json.side_effect = asyncio.TimeoutError("Stalled client timeout")

    # Fast websocket
    fast_ws = AsyncMock()
    fast_ws.client_state = WebSocketState.CONNECTED
    fast_ws.application_state = WebSocketState.CONNECTED

    clients = {slow_ws, fast_ws}
    monkeypatch.setattr(server, "_active_websockets", {conv_id: clients})

    server._event_router.register(slow_ws, conv_id)
    server._event_router.subscribe(slow_ws, "gen:5")
    server._event_router.register(fast_ws, conv_id)
    server._event_router.subscribe(fast_ws, "gen:5")

    await server._ws_send(conv_id, {
        "type": "stream_chunk",
        "run_id": "gen:5",
        "content": "test chunk",
    })

    # Slow socket timed out and was removed
    assert slow_ws not in clients
    # Fast client received message
    fast_ws.send_json.assert_awaited()


async def test_r11_cleanup_stale_drafts_repairs_instead_of_delete_branch(tmp_database):
    """R11: _cleanup_stale_drafts retains empty assistant draft messages older than 30 min,

    marking them as interrupted rather than recursively deleting subtrees/branches.
    """
    conv = await db.create_conversation("Stale Draft Test")
    conv_id = conv["id"]

    # Create user message
    user_msg = await db.add_message(conv_id, "user", "Hello world")

    # Create empty assistant draft older than 30 mins (cutoff = 1800s)
    old_time = time.time() - 3600
    draft = await db.add_message(conv_id, "assistant", "", parent_id=user_msg["id"])
    draft_id = draft["id"]
    conn = await db.get_db()
    await conn.execute(
        "UPDATE messages SET created_at = ?, turn_lifecycle_state = 'running' WHERE id = ?",
        (old_time, draft_id),
    )
    await conn.commit()

    # Create a child continuation message under this draft (which delete_branch would have destroyed!)
    child_msg = await db.add_message(conv_id, "user", "Child continuation from user", parent_id=draft_id)
    await conn.commit()
    await conn.close()

    # Run _cleanup_stale_drafts
    await server._cleanup_stale_drafts()

    # Verify draft still exists in database (NOT deleted)
    draft = await db.get_message(draft_id)
    assert draft is not None
    assert draft["role"] == "assistant"
    # Lifecycle must be repaired to 'interrupted'
    assert draft["turn_lifecycle_state"] == "interrupted"
    assert "interrupted by server restart" in draft["content"]

    # Verify child message still exists and is not orphaned/deleted
    child = await db.get_message(child_msg["id"])
    assert child is not None
    assert child["parent_id"] == draft_id


async def test_r11_startup_reap_runs_before_cleanup_stale_drafts(monkeypatch):
    """R11: In server lifespan, orphan reaping must precede stale draft cleanup."""
    # lifespan closes generation admission on exit; restore this process-global
    # flag so later tests start with their own live-server fixture state.
    monkeypatch.setattr(server, "_shutdown_requested", False)
    call_order = []

    async def mock_init_db():
        call_order.append("init_db")

    async def mock_cron_init_db():
        call_order.append("cron_init_db")

    async def mock_reap_orphan_generations():
        call_order.append("reap_orphan_generations")

    async def mock_cleanup_stale_drafts():
        call_order.append("cleanup_stale_drafts")

    async def mock_release_leases():
        call_order.append("release_leases")

    async def mock_reset_launching():
        call_order.append("reset_launching")

    async def mock_reload_permissions():
        call_order.append("reload_permissions")

    monkeypatch.setattr(db, "init_db", mock_init_db)
    monkeypatch.setattr(server, "_cron_init_db", mock_cron_init_db)
    monkeypatch.setattr(server, "_reap_orphan_generations", mock_reap_orphan_generations)
    monkeypatch.setattr(server, "_cleanup_stale_drafts", mock_cleanup_stale_drafts)
    monkeypatch.setattr(db, "release_stale_generation_leases", mock_release_leases)
    monkeypatch.setattr(db, "reset_launching_queued_generations", mock_reset_launching)
    monkeypatch.setattr(server, "_reload_pending_permissions", mock_reload_permissions)
    monkeypatch.setattr(server, "_ensure_llama", AsyncMock())

    # Trigger lifespan
    async with server.lifespan(server.app):
        pass

    assert "reap_orphan_generations" in call_order
    assert "cleanup_stale_drafts" in call_order

    reap_idx = call_order.index("reap_orphan_generations")
    cleanup_idx = call_order.index("cleanup_stale_drafts")

    # Reap MUST happen before cleanup
    assert reap_idx < cleanup_idx


def test_window_ownership_thread_matching_in_chat_js():
    """Verify that chat.js requires exact thread matching and rejects missing thread fields."""
    from pathlib import Path
    chat_js = (Path(__file__).parents[1] / "static" / "chat.js").read_text(encoding="utf-8")

    # Thread matching in _generationWindowOwnership must require both to be present or both null
    assert "const sameThread = (data.loom_thread_id == null && currentThreadId == null)" in chat_js
    assert "&& data.loom_thread_id != null" in chat_js
    assert "&& String(data.loom_thread_id) === String(currentThreadId)" in chat_js

    # stream_start must check matchesCurrentBranch before moving cursor
    assert "const matchesCurrentBranch = pendingMatch" in chat_js
    assert "_sameMessageId(currentLeaf, parentId)" in chat_js
    assert "if (data.draft_msg_id != null && matchesCurrentBranch) {" in chat_js


async def test_queue_paused_on_cancel(tmp_database):
    """Cancelling a generation pauses dependent queued continuations until explicit resume."""
    conv = await db.create_conversation("Queue Pause Test")
    conv_id = conv["id"]

    draft = await db.add_message(conv_id, "assistant", "In progress draft...")
    draft_id = draft["id"]

    # Add a queued generation waiting for this draft
    queued_user_msg = await db.add_message(conv_id, "user", "Queued follow-up", parent_id=draft_id)
    await db.enqueue_generation(
        user_msg_id=queued_user_msg["id"],
        conv_id=conv_id,
        wait_for_draft_id=draft_id,
        client_instance_id="client-1",
        loom_thread_id="thread-1",
    )

    queued_items = await db.list_queued_generations(conv_id)
    assert len(queued_items) == 1
    assert queued_items[0]["status"] == "queued"

    # Now simulate cancel for this draft
    paused_count = await server._pause_serial_queue_for_cancel(conv_id, draft_msg_id=draft_id)
    assert paused_count == 1

    # Queued item must now be in 'paused' status
    queued_items_after = await db.list_queued_generations(conv_id)
    assert len(queued_items_after) == 1
    assert queued_items_after[0]["status"] == "paused"

