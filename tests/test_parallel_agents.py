"""Regression coverage for same-checkout parallel agent coordination."""

import asyncio
import sqlite3

import database as db
import pytest


async def _message_contract(conv_id, client_instance_id, leaf_id=None, *, intent="continue"):
    """Create/select the same durable topology cursor the browser must use."""
    if leaf_id is None:
        thread = await db.create_loom_thread(
            conv_id, client_instance_id=client_instance_id
        )
        cursor = await db.select_loom_cursor(
            conv_id, client_instance_id, thread["thread_id"], None, force=True
        )
    else:
        leaf = await db.get_message(leaf_id)
        cursor = await db.select_loom_cursor(
            conv_id,
            client_instance_id,
            leaf["loom_thread_id"],
            leaf_id,
        )
    return {
        "client_instance_id": client_instance_id,
        "loom_thread_id": cursor["thread_id"],
        "cursor_id": cursor["cursor_id"],
        "cursor_revision": cursor["revision"],
        "expected_leaf_id": cursor["leaf_message_id"],
        "topology_intent": intent,
    }


async def test_database_generation_lease_is_atomic_and_persists_terminal_state(
    tmp_database,
):
    conv = await db.create_conversation("Atomic serial lane", mode="codex")

    first, second = await asyncio.gather(
        db.try_acquire_generation_lease(conv["id"], "lease-a"),
        db.try_acquire_generation_lease(conv["id"], "lease-b"),
    )
    winners = [lease for lease in (first, second) if lease is not None]
    assert len(winners) == 1
    winner = winners[0]
    assert (await db.get_live_generation_lease(conv["id"]))["lease_id"] == winner["lease_id"]

    assert await db.release_generation_lease(winner["lease_id"], "completed")
    assert await db.get_live_generation_lease(conv["id"]) is None
    terminal = await db.get_generation_lease(winner["lease_id"])
    assert terminal["terminal_state"] == "completed"
    assert terminal["released_at"] is not None
    assert await db.try_acquire_generation_lease(
        conv["id"], "lease-after-terminal"
    )


async def test_serial_agent_draft_requires_exact_live_database_lease(tmp_database):
    conv = await db.create_conversation("Draft lease backstop", mode="codex")
    prompt = await db.add_message(conv["id"], "user", "first prompt")

    with pytest.raises(sqlite3.IntegrityError, match="matching generation lease"):
        await db.add_message(
            conv["id"], "assistant", "", parent_id=prompt["id"]
        )

    lease = await db.try_acquire_generation_lease(
        conv["id"],
        "lease-authorized-draft",
        run_id="gen:123",
        user_msg_id=prompt["id"],
        draft_parent_id=prompt["id"],
    )
    assert lease is not None
    draft = await db.add_message(
        conv["id"],
        "assistant",
        "",
        parent_id=prompt["id"],
        generation_lease_id=lease["lease_id"],
    )
    assert draft["generation_lease_id"] == lease["lease_id"]


async def test_startup_stale_lease_release_keeps_a_tracked_provider_lane(
    tmp_database,
):
    conv = await db.create_conversation("Stubborn orphan lane", mode="codex")
    prompt = await db.add_message(conv["id"], "user", "still running")
    draft = await db.add_message(
        conv["id"], "assistant", "partial", parent_id=prompt["id"]
    )
    lease = await db.try_acquire_generation_lease(
        conv["id"], "lease-stubborn", user_msg_id=prompt["id"]
    )
    assert lease is not None
    await db.bind_generation_lease(
        lease["lease_id"], draft_msg_id=draft["id"], state="running"
    )
    await db.register_active_generation(
        draft_msg_id=draft["id"],
        conv_id=conv["id"],
        pid=424242,
        project_dir="C:/disposable",
        mode="codex",
    )

    assert await db.release_stale_generation_leases() == 0
    assert (await db.get_live_generation_lease(conv["id"]))["lease_id"] == lease["lease_id"]

    await db.unregister_active_generation(draft["id"])
    assert await db.release_stale_generation_leases() == 1
    terminal = await db.get_generation_lease(lease["lease_id"])
    assert terminal["terminal_state"] == "orphaned"


async def test_message_send_idempotency_precedes_stale_cursor_validation(
    client, tmp_database
):
    conv = await db.create_conversation("Idempotent send", mode="weave")
    contract = await _message_contract(
        conv["id"], "idempotent-window", None, intent="new_thread"
    )
    payload = {
        "role": "user",
        "content": "store exactly once",
        "parent_id": None,
        "send_intent": "new_thread",
        "client_request_id": "request-once-123",
        **contract,
    }

    first = await client.post(
        f"/api/conversations/{conv['id']}/messages", json=payload
    )
    assert first.status_code == 200, first.text
    replay = await client.post(
        f"/api/conversations/{conv['id']}/messages", json=payload
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["idempotent_replay"] is True
    connection = await db.get_db()
    rows = await connection.execute_fetchall(
        "SELECT id, content FROM messages WHERE conversation_id = ?",
        (conv["id"],),
    )
    assert [(row["id"], row["content"]) for row in rows] == [
        (first.json()["id"], "store exactly once")
    ]


def test_browser_queue_is_restored_from_server_and_never_flushed_locally():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "static" / "chat.js").read_text(
        encoding="utf-8"
    )
    assert "/generation-queue" in source
    assert "if (msg.queued)" in source
    assert "function _flushQueuedGeneration" not in source
    assert "if (!_attempt) {\n        _queuedGeneration = null;" not in source


def test_hook_based_harnesses_export_generation_permission_scope():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for filename in ("claude_client.py", "gemini_client.py"):
        source = (root / filename).read_text(encoding="utf-8")
        assert 'env["LOOM_PERMISSION_SCOPE"] = f"gen:{gen_key[2]}"' in source


async def test_parallel_agents_setting_persists_and_forks():
    conv = await db.create_conversation("Parallel", mode="codex", project_dir=".")
    await db.update_conversation_fields(conv["id"], parallel_agents_enabled=1)
    conv = await db.get_conversation(conv["id"])
    assert conv["parallel_agents_enabled"] == 1

    msg = await db.add_message(conv["id"], "user", "independent task")
    fork = await db.fork_conversation(conv["id"], msg["id"])
    assert fork["parallel_agents_enabled"] == 1


async def test_create_and_update_parallel_agents_api(client, mock_llama, tmp_path):
    response = await client.post(
        "/api/conversations",
        json={
            "title": "Parallel API",
            "mode": "claude",
            "project_dir": str(tmp_path),
            "cc_model": "sonnet",
            "parallel_agents_enabled": True,
        },
    )
    assert response.status_code == 200
    conv = response.json()
    assert conv["parallel_agents_enabled"] == 1

    response = await client.put(
        f"/api/conversations/{conv['id']}",
        json={"parallel_agents_enabled": False},
    )
    assert response.status_code == 200
    assert response.json()["parallel_agents_enabled"] == 0


async def test_parallel_message_write_requires_an_explicit_branch_or_root(
    client, mock_llama, tmp_path
):
    response = await client.post(
        "/api/conversations",
        json={
            "title": "Parallel parent integrity",
            "mode": "codex",
            "project_dir": str(tmp_path),
            "parallel_agents_enabled": True,
        },
    )
    conv = response.json()

    first_contract = await _message_contract(
        conv["id"], "window-first", None, intent="new_thread"
    )
    first = await client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={
            "role": "user", "content": "first root", "parent_id": None,
            **first_contract,
        },
    )
    assert first.status_code == 200

    ambiguous = await client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"role": "user", "content": "must not follow the active agent"},
    )
    assert ambiguous.status_code == 400
    assert "parent_id is required" in ambiguous.json()["detail"]

    second_contract = await _message_contract(
        conv["id"], "window-second", None, intent="new_thread"
    )
    second = await client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={
            "role": "user", "content": "second root", "parent_id": None,
            **second_contract,
        },
    )
    assert second.status_code == 200
    assert second.json()["parent_id"] is None
    assert second.json()["id"] != first.json()["id"]


async def test_branch_edit_is_topology_not_parallel_execution(
    client, mock_llama, tmp_path
):
    """Editing into a sibling branch remains valid when parallel runs are off."""
    conv = await db.create_conversation(
        "Edited branch", mode="codex", project_dir=str(tmp_path)
    )
    root = await db.add_message(conv["id"], "user", "root")
    assistant = await db.add_message(
        conv["id"], "assistant", "answer", parent_id=root["id"]
    )
    original = await db.add_message(
        conv["id"], "user", "original wording", parent_id=assistant["id"]
    )
    contract = await _message_contract(
        conv["id"], "edit-window", assistant["id"], intent="branch"
    )

    response = await client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={
            "role": "user",
            "content": "edited wording",
            "parent_id": assistant["id"],
            "send_intent": "continue",
            **contract,
        },
    )

    assert response.status_code == 200, response.text
    edited = response.json()
    assert edited["parent_id"] == assistant["id"]
    assert edited["send_intent"] == "continue"
    siblings = await db.get_children(assistant["id"])
    assert {message["id"] for message in siblings} == {
        original["id"], edited["id"]
    }


async def test_durable_live_pid_keeps_nonparallel_lane_occupied(
    tmp_database, monkeypatch
):
    import server

    conv = await db.create_conversation("Durable lane", mode="codex")
    prompt = await db.add_message(conv["id"], "user", "prompt")
    draft = await db.add_message(
        conv["id"], "assistant", "partial", parent_id=prompt["id"]
    )
    await db.register_active_generation(
        draft["id"], conv["id"], 44001, mode="codex"
    )
    monkeypatch.setattr(server, "_pid_alive", lambda pid: int(pid) == 44001)

    assert server._has_active_generation_for_conv(conv["id"]) is False
    assert await server._provider_lane_occupied(conv["id"]) is True


async def test_admin_kill_targets_one_draft_and_waits_for_pid_exit(
    client, tmp_database, monkeypatch
):
    import server

    conv = await db.create_conversation("Exact admin cancel", mode="codex")
    p1 = await db.add_message(conv["id"], "user", "one")
    p2 = await db.add_message(conv["id"], "user", "two")
    d1 = await db.add_message(conv["id"], "assistant", "partial", parent_id=p1["id"])
    d2 = await db.add_message(conv["id"], "assistant", "partial", parent_id=p2["id"])
    gate1 = asyncio.Event()
    gate2 = asyncio.Event()
    task1 = asyncio.create_task(gate1.wait())
    task2 = asyncio.create_task(gate2.wait())
    key1 = (conv["id"], p1["id"], 9101)
    key2 = (conv["id"], p2["id"], 9102)
    server._active_generations[key1] = task1
    server._active_generations[key2] = task2
    server._generation_snapshots[key1] = {"draft_msg_id": d1["id"]}
    server._generation_snapshots[key2] = {"draft_msg_id": d2["id"]}
    await db.register_active_generation(d1["id"], conv["id"], 44101, mode="codex")
    await db.register_active_generation(d2["id"], conv["id"], 44102, mode="codex")
    alive = {44101: True, 44102: True}
    monkeypatch.setattr(server, "_pid_alive", lambda pid: alive.get(int(pid), False))

    def fake_kill(pid):
        alive[int(pid)] = False
        return True

    monkeypatch.setattr(server, "_kill_pid", fake_kill)
    try:
        response = await client.post(f"/api/generations/{d1['id']}/kill")
        assert response.status_code == 200, response.text
        assert response.json()["pid_alive"] is False
        assert task1.cancelled()
        assert not task2.done()
        assert alive[44102] is True
        rows = await db.list_active_generations()
        assert {row["draft_msg_id"] for row in rows} == {d2["id"]}
    finally:
        gate2.set()
        await asyncio.gather(task2, return_exceptions=True)
        server._active_generations.pop(key1, None)
        server._active_generations.pop(key2, None)
        server._generation_snapshots.pop(key1, None)
        server._generation_snapshots.pop(key2, None)
        await db.unregister_active_generation(d1["id"])
        await db.unregister_active_generation(d2["id"])


async def test_queued_user_parented_to_running_draft_stays_on_one_branch(tmp_database):
    conv = await db.create_conversation("Queued continuation", mode="codex")
    prompt = await db.add_message(conv["id"], "user", "first prompt", parent_id=None)
    draft = await db.add_message(
        conv["id"], "assistant", "running reply", parent_id=prompt["id"]
    )
    queued = await db.add_message(
        conv["id"], "user", "queued follow-up", parent_id=draft["id"]
    )

    branch = await db.get_branch_to_root(queued["id"])
    assert [message["id"] for message in branch] == [
        prompt["id"],
        draft["id"],
        queued["id"],
    ]
    assert queued["parent_id"] == draft["id"]


async def test_queued_generation_survives_database_reconnect(tmp_database):
    conv = await db.create_conversation("Durable queue", mode="codex")
    prompt = await db.add_message(conv["id"], "user", "first prompt")
    draft = await db.add_message(
        conv["id"], "assistant", "still running", parent_id=prompt["id"]
    )
    queued = await db.add_message(
        conv["id"],
        "user",
        "continue after this",
        parent_id=draft["id"],
        origin_client_id="window-a",
        target_anchor_id=prompt["id"],
        send_intent="continue",
    )
    await db.enqueue_generation(
        queued["id"],
        conv["id"],
        draft["id"],
        client_instance_id="window-a",
        settings={"cc_model": "gpt-5.6-sol", "cc_effort": "high"},
    )

    await db.close_db()
    await db.init_db()
    restored = await db.list_queued_generations(conv["id"])

    assert len(restored) == 1
    assert restored[0]["user_msg_id"] == queued["id"]
    assert restored[0]["wait_for_draft_id"] == draft["id"]
    assert restored[0]["client_instance_id"] == "window-a"
    assert restored[0]["settings"]["cc_model"] == "gpt-5.6-sol"
    assert restored[0]["target_anchor_id"] == prompt["id"]
    assert restored[0]["send_intent"] == "continue"


async def test_normal_send_is_durably_queued_on_the_exact_running_branch(
    client, mock_llama, tmp_path
):
    import server

    conv = await db.create_conversation(
        "Queue API", mode="codex", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(conv["id"], parallel_agents_enabled=1)
    prompt = await db.add_message(conv["id"], "user", "first prompt")
    draft = await db.add_message(
        conv["id"], "assistant", "partial", parent_id=prompt["id"]
    )
    gate = asyncio.Event()
    task = asyncio.create_task(gate.wait())
    key = (conv["id"], prompt["id"], 8101)
    server._active_generations[key] = task
    server._generation_snapshots[key] = {
        "parent_id": prompt["id"],
        "draft_msg_id": draft["id"],
        "client_instance_id": "window-a",
    }
    try:
        contract = await _message_contract(
            conv["id"], "window-a", draft["id"]
        )
        response = await client.post(
            f"/api/conversations/{conv['id']}/messages",
            json={
                "role": "user",
                "content": "queued follow-up",
                "parent_id": draft["id"],
                **contract,
                "send_intent": "continue",
                "cc_model": "gpt-5.6-sol",
                "cc_effort": "high",
            },
        )
        assert response.status_code == 200
        message = response.json()
        assert message["queued"] is True
        assert message["parent_id"] == draft["id"]
        assert message["queue_after_draft_id"] == draft["id"]

        stored = await db.get_message(message["id"])
        assert stored["origin_client_id"] == "window-a"
        assert stored["target_anchor_id"] == draft["id"]
        assert stored["send_intent"] == "continue"
        queued = await db.list_queued_generations(conv["id"])
        assert queued[0]["user_msg_id"] == message["id"]
        assert queued[0]["settings"]["cc_model"] == "gpt-5.6-sol"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server._active_generations.pop(key, None)
        server._generation_snapshots.pop(key, None)


async def test_explicit_parallel_send_remains_a_sibling_of_running_branch(
    client, mock_llama, tmp_path
):
    import server

    conv = await db.create_conversation(
        "Parallel intent", mode="codex", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(conv["id"], parallel_agents_enabled=1)
    prompt = await db.add_message(conv["id"], "user", "first prompt")
    draft = await db.add_message(
        conv["id"], "assistant", "partial", parent_id=prompt["id"]
    )
    gate = asyncio.Event()
    task = asyncio.create_task(gate.wait())
    key = (conv["id"], prompt["id"], 8102)
    server._active_generations[key] = task
    server._generation_snapshots[key] = {
        "parent_id": prompt["id"],
        "draft_msg_id": draft["id"],
    }
    try:
        contract = await _message_contract(
            conv["id"], "window-b", prompt["id"], intent="branch"
        )
        response = await client.post(
            f"/api/conversations/{conv['id']}/messages",
            json={
                "role": "user",
                "content": "independent sibling",
                "parent_id": prompt["id"],
                "send_intent": "parallel",
                **contract,
            },
        )
        assert response.status_code == 200
        message = response.json()
        assert message["queued"] is False
        assert message["parent_id"] == prompt["id"]
        assert await db.list_queued_generations(conv["id"]) == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server._active_generations.pop(key, None)
        server._generation_snapshots.pop(key, None)


async def test_parallel_send_is_rejected_when_conversation_disables_parallel_agents(
    client, mock_llama, tmp_path
):
    conv = await db.create_conversation(
        "Single agent lane", mode="codex", project_dir=str(tmp_path)
    )
    prompt = await db.add_message(conv["id"], "user", "first prompt")
    contract = await _message_contract(
        conv["id"], "window-single", prompt["id"], intent="branch"
    )

    response = await client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={
            "role": "user",
            "content": "must not become a concurrent sibling",
            "parent_id": prompt["id"],
            "send_intent": "parallel",
            **contract,
        },
    )

    assert response.status_code == 409
    assert "Parallel message launches are disabled" in response.json()["detail"]
    assert await db.get_children(prompt["id"]) == []


async def test_legacy_generate_never_implicitly_cancels_nonparallel_run(
    tmp_database
):
    import json
    from starlette.websockets import WebSocketDisconnect, WebSocketState
    import server

    conv = await db.create_conversation("No implicit cancel", mode="codex")
    prompt = await db.add_message(conv["id"], "user", "running prompt")
    draft = await db.add_message(
        conv["id"], "assistant", "partial", parent_id=prompt["id"]
    )
    queued_user = await db.add_message(
        conv["id"], "user", "queued continuation", parent_id=draft["id"]
    )
    cursor = await db.select_loom_cursor(
        conv["id"],
        "window-a",
        queued_user["loom_thread_id"],
        queued_user["id"],
        force=True,
    )

    gate = asyncio.Event()
    active_task = asyncio.create_task(gate.wait())
    active_task._client_instance_id = "window-a"
    active_key = (conv["id"], prompt["id"], 8120)
    server._active_generations[active_key] = active_task
    server._generation_snapshots[active_key] = {
        "parent_id": prompt["id"],
        "draft_msg_id": draft["id"],
        "client_instance_id": "window-a",
        "loom_thread_id": prompt["loom_thread_id"],
    }

    class FakeWebSocket:
        def __init__(self):
            self.messages = []
            self.client_state = WebSocketState.CONNECTED
            self._client_instance_id = "window-a"
            self._received = False

        async def send_json(self, value):
            self.messages.append(value)

        async def receive_text(self):
            if self._received:
                raise WebSocketDisconnect()
            self._received = True
            return json.dumps(
                {
                    "action": "generate",
                    "parent_id": queued_user["id"],
                    "client_instance_id": "window-a",
                    "loom_thread_id": cursor["thread_id"],
                }
            )

    websocket = FakeWebSocket()
    server._active_websockets[conv["id"]] = {websocket}
    server._event_router.register(websocket, conv["id"])
    try:
        await server._continue_ws_chat(websocket, conv["id"], "window-a")

        assert not active_task.done()
        assert list(server._active_generations) == [active_key]
        errors = [
            message for message in websocket.messages
            if message.get("event_kind") == "error" or message.get("type") == "error"
        ]
        assert errors
        assert "Queue a continuation or cancel" in errors[-1]["error"]
    finally:
        active_task.cancel()
        await asyncio.gather(active_task, return_exceptions=True)
        server._active_generations.pop(active_key, None)
        server._generation_snapshots.pop(active_key, None)
        server._active_websockets.pop(conv["id"], None)
        server._event_router.unregister(websocket)


async def test_explicit_new_thread_cannot_be_reparented_to_an_active_branch(
    client, mock_llama, tmp_path
):
    import server

    conv = await db.create_conversation(
        "Standalone root intent", mode="codex", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(conv["id"], parallel_agents_enabled=1)
    prompt = await db.add_message(conv["id"], "user", "running root")
    draft = await db.add_message(
        conv["id"], "assistant", "partial", parent_id=prompt["id"]
    )
    gate = asyncio.Event()
    task = asyncio.create_task(gate.wait())
    key = (conv["id"], prompt["id"], 8104)
    server._active_generations[key] = task
    server._generation_snapshots[key] = {
        "parent_id": prompt["id"],
        "draft_msg_id": draft["id"],
        "client_instance_id": "window-running",
    }
    try:
        contract = await _message_contract(
            conv["id"], "window-new-root", None, intent="new_thread"
        )
        response = await client.post(
            f"/api/conversations/{conv['id']}/messages",
            json={
                "role": "user",
                "content": "standalone task",
                "parent_id": None,
                # Backward-compatible request shape used by the browser while
                # topology_intent carries the authoritative meaning.
                "send_intent": "parallel",
                **contract,
            },
        )
        assert response.status_code == 200
        message = response.json()
        assert message["queued"] is False
        assert message["parent_id"] is None

        stored = await db.get_message(message["id"])
        assert stored["target_anchor_id"] is None
        assert stored["send_intent"] == "new_thread"
        assert await db.list_queued_generations(conv["id"]) == []

        invalid = await client.post(
            f"/api/conversations/{conv['id']}/messages",
            json={
                "role": "user",
                "content": "must not attach",
                "parent_id": prompt["id"],
                "send_intent": "new_thread",
            },
        )
        assert invalid.status_code == 400
        assert "explicit null parent_id" in invalid.json()["detail"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server._active_generations.pop(key, None)
        server._generation_snapshots.pop(key, None)


async def test_normal_send_never_silently_branches_before_draft_exists(
    client, mock_llama, tmp_path
):
    import server

    conv = await db.create_conversation(
        "Pre-draft continuation", mode="codex", project_dir=str(tmp_path)
    )
    await db.update_conversation_fields(conv["id"], parallel_agents_enabled=1)
    prompt = await db.add_message(conv["id"], "user", "first prompt")
    gate = asyncio.Event()
    task = asyncio.create_task(gate.wait())
    key = (conv["id"], prompt["id"], 8103)
    server._active_generations[key] = task
    server._generation_snapshots[key] = {
        "parent_id": prompt["id"],
        "draft_msg_id": None,
    }
    try:
        contract = await _message_contract(
            conv["id"], "window-pre-draft", prompt["id"]
        )
        response = await client.post(
            f"/api/conversations/{conv['id']}/messages",
            json={
                "role": "user",
                "content": "must wait",
                "parent_id": prompt["id"],
                "send_intent": "continue",
                **contract,
            },
        )
        assert response.status_code == 409
        assert "durable draft" in response.json()["detail"]
        assert await db.get_children(prompt["id"]) == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server._active_generations.pop(key, None)
        server._generation_snapshots.pop(key, None)


async def test_ready_durable_queue_launches_without_a_browser_socket(
    monkeypatch, tmp_database
):
    import server

    conv = await db.create_conversation("Queue launch", mode="codex")
    prompt = await db.add_message(conv["id"], "user", "first prompt")
    predecessor_lease = await db.try_acquire_generation_lease(
        conv["id"],
        "lease-completed-predecessor",
        run_id="gen:9000",
        user_msg_id=prompt["id"],
        draft_parent_id=prompt["id"],
    )
    draft = await db.add_message(
        conv["id"],
        "assistant",
        "",
        parent_id=prompt["id"],
        generation_lease_id=predecessor_lease["lease_id"],
    )
    await db.bind_generation_lease(
        predecessor_lease["lease_id"], draft_msg_id=draft["id"], state="running"
    )
    await db.update_message_content(draft["id"], content="finished")
    await db.release_generation_lease(predecessor_lease["lease_id"], "completed")
    queued = await db.add_message(
        conv["id"], "user", "next prompt", parent_id=draft["id"]
    )
    await db.enqueue_generation(
        queued["id"],
        conv["id"],
        draft["id"],
        client_instance_id="offline-window",
        settings={"cc_model": "gpt-5.6-sol"},
    )
    launches = []

    async def fake_launch(
        websocket, conv_id, data, client_instance_id=None,
        *, preacquired_lease_id=None,
    ):
        launches.append((websocket, conv_id, dict(data), client_instance_id))
        if preacquired_lease_id:
            await db.release_generation_lease(
                preacquired_lease_id, "test_complete"
            )
        return (conv_id, data["parent_id"], 9101), object()

    monkeypatch.setattr(server, "_launch_generation_task", fake_launch)
    await server._dispatch_ready_queued_generations(conv["id"])

    assert len(launches) == 1
    websocket, launched_conv, data, client_id = launches[0]
    assert websocket is None
    assert launched_conv == conv["id"]
    assert data["parent_id"] == queued["id"]
    assert data["cc_model"] == "gpt-5.6-sol"
    assert client_id == "offline-window"
    assert await db.list_queued_generations(conv["id"]) == []


async def test_cancelled_predecessor_cannot_auto_dispatch_queued_turn(
    monkeypatch, tmp_database
):
    import server

    conv = await db.create_conversation("Cancelled queue barrier", mode="codex")
    prompt = await db.add_message(conv["id"], "user", "first prompt")
    lease = await db.try_acquire_generation_lease(
        conv["id"],
        "lease-cancelled-predecessor",
        run_id="gen:9002",
        user_msg_id=prompt["id"],
        draft_parent_id=prompt["id"],
    )
    draft = await db.add_message(
        conv["id"],
        "assistant",
        "",
        parent_id=prompt["id"],
        generation_lease_id=lease["lease_id"],
    )
    await db.bind_generation_lease(
        lease["lease_id"], draft_msg_id=draft["id"], state="running"
    )
    await db.discard_message_partial_context(draft["id"])
    await db.release_generation_lease(lease["lease_id"], "cancelled")
    queued = await db.add_message(
        conv["id"], "user", "do not jump", parent_id=draft["id"]
    )
    await db.enqueue_generation(
        queued["id"],
        conv["id"],
        draft["id"],
        settings={"cc_model": "gpt-5.6-sol"},
    )

    launches = []

    async def fake_launch(*args, **kwargs):
        launches.append((args, kwargs))
        raise AssertionError("cancelled predecessor must not launch its queue")

    monkeypatch.setattr(server, "_launch_generation_task", fake_launch)
    await server._dispatch_ready_queued_generations(conv["id"])

    assert launches == []
    queue = await db.list_queued_generations(conv["id"])
    assert len(queue) == 1
    assert queue[0]["status"] == "paused"


async def test_partial_provider_failure_releases_failed_lease_and_pauses_queue(
    monkeypatch, tmp_database
):
    import server

    conv = await db.create_conversation("Partial failure queue barrier", mode="goose")
    prompt = await db.add_message(conv["id"], "user", "perform the migration")
    lease = await db.try_acquire_generation_lease(
        conv["id"],
        "lease-partial-failure",
        run_id="gen:9003",
        user_msg_id=prompt["id"],
        draft_parent_id=prompt["id"],
    )
    created = {}

    async def fail_after_partial_work(_websocket, _conv_id, _payload):
        draft = await db.add_message(
            conv["id"],
            "assistant",
            "",
            parent_id=prompt["id"],
            generation_lease_id=lease["lease_id"],
        )
        await db.bind_generation_lease(
            lease["lease_id"], draft_msg_id=draft["id"], state="running"
        )
        asyncio.current_task()._draft_msg_id = draft["id"]
        queued = await db.add_message(
            conv["id"], "user", "continue after review", parent_id=draft["id"]
        )
        await db.enqueue_generation(
            queued["id"],
            conv["id"],
            draft["id"],
            settings={"cc_model": "goose:openrouter:z-ai/glm-5.3-flash"},
        )
        await db.update_message_content(
            draft["id"],
            content="partial migration result\n\n[Goose turn failed: upstream idle timeout]",
            turn_lifecycle_state="partial_failed",
        )
        created.update(draft=draft, queued=queued)

    monkeypatch.setattr(
        server, "_handle_generation_with_workspace_safety", fail_after_partial_work
    )
    await asyncio.create_task(
        server._generation_task_entry(None, conv["id"], {}, lease["lease_id"])
    )

    released = await db.get_generation_lease(lease["lease_id"])
    assert released["state"] == "terminal"
    assert released["terminal_state"] == "partial_failed"
    queue = await db.list_queued_generations(conv["id"])
    assert len(queue) == 1
    assert queue[0]["user_msg_id"] == created["queued"]["id"]
    assert queue[0]["status"] == "paused"


async def test_parallel_agents_require_agent_workspace(client, mock_llama):
    response = await client.post(
        "/api/conversations",
        json={
            "title": "Invalid Parallel",
            "mode": "weave",
            "parallel_agents_enabled": True,
        },
    )
    assert response.status_code == 400
    assert "agent conversations" in response.json()["detail"]


async def test_cancel_generation_keys_can_target_one_parallel_generation():
    import server

    gate = asyncio.Event()
    first = asyncio.create_task(gate.wait())
    second = asyncio.create_task(gate.wait())
    first_key = (81, 101, 1001)
    second_key = (81, 202, 1002)
    server._active_generations[first_key] = first
    server._active_generations[second_key] = second
    server._generation_snapshots[first_key] = {"draft_msg_id": 301}
    server._generation_snapshots[second_key] = {"draft_msg_id": 302}
    try:
        assert server._active_generation_keys_for_cancel(81, gen_id=1002) == [second_key]
        assert server._active_generation_keys_for_cancel(81, draft_msg_id=301) == [first_key]
        assert set(server._active_generation_keys_for_cancel(81)) == {first_key, second_key}
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        server._active_generations.pop(first_key, None)
        server._active_generations.pop(second_key, None)
        server._generation_snapshots.pop(first_key, None)
        server._generation_snapshots.pop(second_key, None)


async def test_ws_send_tags_generation_events_with_branch_snapshot():
    import server

    class FakeWebSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, value):
            self.messages.append(value)

    current = asyncio.current_task()
    old_key = getattr(current, "_gen_key", None)
    key = (91, 401, 7001)
    fake = FakeWebSocket()
    server._active_websockets[91] = {fake}
    server._generation_snapshots[key] = {
        "parent_id": 401,
        "draft_msg_id": 402,
        "client_instance_id": "window-abc",
        "loom_thread_id": "loom:test:91",
    }
    current._gen_key = key
    server._event_router.register(fake, 91)
    server._event_router.subscribe(fake, "gen:7001")
    try:
        await server._ws_send(91, {"type": "stream_chunk", "content": "ok"})
        assert fake.messages[-1]["gen_id"] == 7001
        assert fake.messages[-1]["parent_id"] == 401
        assert fake.messages[-1]["draft_msg_id"] == 402
        assert fake.messages[-1]["client_instance_id"] == "window-abc"
        assert fake.messages[-1]["loom_thread_id"] == "loom:test:91"
    finally:
        if old_key is None:
            delattr(current, "_gen_key")
        else:
            current._gen_key = old_key
        server._active_websockets.pop(91, None)
        server._event_router.unregister(fake)
        server._generation_snapshots.pop(key, None)


async def test_ws_send_routes_generation_stream_only_to_subscribed_socket():
    import server

    class FakeWebSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, value):
            self.messages.append(value)

    current = asyncio.current_task()
    old_key = getattr(current, "_gen_key", None)
    key = (92, 501, 7002)
    origin = FakeWebSocket()
    observer = FakeWebSocket()
    server._active_websockets[92] = {origin, observer}
    server._generation_snapshots[key] = {
        "parent_id": 501,
        "draft_msg_id": 502,
        "client_instance_id": "window-origin",
    }
    current._gen_key = key
    server._event_router.register(origin, 92)
    server._event_router.register(observer, 92)
    server._event_router.subscribe(origin, "gen:7002")
    try:
        await server._ws_send(92, {"type": "stream_chunk", "content": "private"})
        assert origin.messages[-1]["content"] == "private"
        assert observer.messages == []
    finally:
        if old_key is None:
            delattr(current, "_gen_key")
        else:
            current._gen_key = old_key
        server._active_websockets.pop(92, None)
        server._event_router.unregister(origin)
        server._event_router.unregister(observer)
        server._generation_snapshots.pop(key, None)


async def test_tabs_on_exact_thread_can_subscribe_without_stealing_run_origin():
    import server

    conv = await db.create_conversation("Exact run subscription", mode="codex")
    prompt = await db.add_message(conv["id"], "user", "prompt")
    draft = await db.add_message(
        conv["id"], "assistant", "partial", parent_id=prompt["id"]
    )
    gate = asyncio.Event()
    task = asyncio.create_task(gate.wait())
    key = (conv["id"], prompt["id"], 7003)

    class FakeWebSocket:
        pass

    old_socket = FakeWebSocket()
    old_socket._client_instance_id = "old-page"
    new_socket = FakeWebSocket()
    new_socket._client_instance_id = "new-page"
    task._client_instance_id = "old-page"
    server._active_generations[key] = task
    server._generation_snapshots[key] = {
        "parent_id": prompt["id"],
        "draft_msg_id": draft["id"],
        "client_instance_id": "old-page",
        "loom_thread_id": prompt["loom_thread_id"],
    }
    await db.select_loom_cursor(
        conv["id"], "old-page", prompt["loom_thread_id"], draft["id"]
    )
    await db.select_loom_cursor(
        conv["id"], "new-page", prompt["loom_thread_id"], prompt["id"]
    )
    server._active_websockets[conv["id"]] = {old_socket, new_socket}
    server._event_router.register(old_socket, conv["id"])
    server._event_router.register(new_socket, conv["id"])
    try:
        assert await server._websocket_can_subscribe_run(
            old_socket, conv["id"], "gen:7003"
        )
        assert await server._websocket_can_subscribe_run(
            new_socket, conv["id"], "gen:7003"
        )
        assert await server._subscribe_cursor_generation(
            new_socket, conv["id"]
        ) == "gen:7003"
        reloaded_cursor = await db.get_client_loom_cursor(conv["id"], "new-page")
        assert reloaded_cursor["leaf_message_id"] == draft["id"]
        assert task._client_instance_id == "old-page"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server._active_generations.pop(key, None)
        server._generation_snapshots.pop(key, None)
        server._active_websockets.pop(conv["id"], None)
        server._event_router.unregister(old_socket)
        server._event_router.unregister(new_socket)


async def test_parallel_drafts_advance_only_their_launching_page(tmp_database):
    import server

    conv = await db.create_conversation("Independent launch cursors", mode="codex")
    prompt = await db.add_message(conv["id"], "user", "shared parent")
    draft_a = await db.add_message(
        conv["id"], "assistant", "a", parent_id=prompt["id"]
    )
    draft_b = await db.add_message(
        conv["id"], "assistant", "b", parent_id=prompt["id"]
    )
    await db.select_loom_cursor(
        conv["id"], "window-a", prompt["loom_thread_id"], prompt["id"]
    )
    await db.select_loom_cursor(
        conv["id"], "window-b", prompt["loom_thread_id"], prompt["id"]
    )
    gate = asyncio.Event()
    task_a = asyncio.create_task(gate.wait())
    task_b = asyncio.create_task(gate.wait())
    key_a = (conv["id"], prompt["id"], 7301)
    key_b = (conv["id"], prompt["id"], 7302)
    server._active_generations[key_a] = task_a
    server._active_generations[key_b] = task_b
    server._generation_snapshots[key_a] = {
        "parent_id": prompt["id"],
        "draft_msg_id": draft_a["id"],
        "client_instance_id": "window-a",
        "loom_thread_id": prompt["loom_thread_id"],
    }
    server._generation_snapshots[key_b] = {
        "parent_id": prompt["id"],
        "draft_msg_id": draft_b["id"],
        "client_instance_id": "window-b",
        "loom_thread_id": prompt["loom_thread_id"],
    }
    try:
        await server._advance_unambiguous_generation_cursors(key_a)
        cursor_a = await db.get_client_loom_cursor(conv["id"], "window-a")
        cursor_b = await db.get_client_loom_cursor(conv["id"], "window-b")
        assert cursor_a["leaf_message_id"] == draft_a["id"]
        assert cursor_b["leaf_message_id"] == prompt["id"]

        await server._advance_unambiguous_generation_cursors(key_b)
        cursor_b = await db.get_client_loom_cursor(conv["id"], "window-b")
        assert cursor_b["leaf_message_id"] == draft_b["id"]
    finally:
        task_a.cancel()
        task_b.cancel()
        await asyncio.gather(task_a, task_b, return_exceptions=True)
        for key in (key_a, key_b):
            server._active_generations.pop(key, None)
            server._generation_snapshots.pop(key, None)


async def test_three_windows_receive_only_their_exact_thread_stream(tmp_database):
    import server

    class FakeWebSocket:
        def __init__(self, client_id):
            self._client_instance_id = client_id
            self.messages = []

        async def send_json(self, value):
            self.messages.append(value)

    conv = await db.create_conversation("Three isolated windows", mode="codex")
    gates = []
    keys = []
    sockets = []
    for index in range(3):
        client_id = f"window-{index}"
        root = await db.add_message(conv["id"], "user", f"root-{index}")
        draft = await db.add_message(
            conv["id"], "assistant", f"draft-{index}", parent_id=root["id"]
        )
        await db.select_loom_cursor(
            conv["id"], client_id, root["loom_thread_id"], draft["id"]
        )
        gate = asyncio.Event()
        task = asyncio.create_task(gate.wait())
        key = (conv["id"], root["id"], 7401 + index)
        socket = FakeWebSocket(client_id)
        server._active_generations[key] = task
        server._generation_snapshots[key] = {
            "parent_id": root["id"],
            "draft_msg_id": draft["id"],
            "client_instance_id": client_id,
            "loom_thread_id": root["loom_thread_id"],
        }
        gates.append((gate, task))
        keys.append(key)
        sockets.append(socket)

    server._active_websockets[conv["id"]] = set(sockets)
    for socket in sockets:
        server._event_router.register(socket, conv["id"])
    current = asyncio.current_task()
    old_key = getattr(current, "_gen_key", None)
    try:
        for index, socket in enumerate(sockets):
            assert await server._subscribe_cursor_generation(
                socket, conv["id"]
            ) == f"gen:{7401 + index}"
        for index, key in enumerate(keys):
            current._gen_key = key
            await server._ws_send(
                conv["id"],
                {"type": "stream_chunk", "content": f"private-{index}"},
            )
        for index, socket in enumerate(sockets):
            assert [event["content"] for event in socket.messages] == [
                f"private-{index}"
            ]
    finally:
        if old_key is None:
            delattr(current, "_gen_key")
        else:
            current._gen_key = old_key
        for _gate, task in gates:
            task.cancel()
        await asyncio.gather(
            *(task for _gate, task in gates), return_exceptions=True
        )
        for key in keys:
            server._active_generations.pop(key, None)
            server._generation_snapshots.pop(key, None)
        server._active_websockets.pop(conv["id"], None)
        for socket in sockets:
            server._event_router.unregister(socket)


async def test_unrelated_thread_cannot_subscribe_to_generation():
    import server

    conv = await db.create_conversation("Thread isolation", mode="codex")
    prompt = await db.add_message(conv["id"], "user", "prompt")
    draft = await db.add_message(
        conv["id"], "assistant", "partial", parent_id=prompt["id"]
    )
    other = await db.add_message(conv["id"], "user", "other root")
    gate = asyncio.Event()
    task = asyncio.create_task(gate.wait())
    key = (conv["id"], prompt["id"], 7004)

    class FakeWebSocket:
        pass

    new_socket = FakeWebSocket()
    new_socket._client_instance_id = "other-page"
    server._active_generations[key] = task
    server._generation_snapshots[key] = {
        "parent_id": prompt["id"],
        "draft_msg_id": draft["id"],
        "loom_thread_id": prompt["loom_thread_id"],
    }
    await db.select_loom_cursor(
        conv["id"], "other-page", other["loom_thread_id"], other["id"]
    )
    try:
        assert not await server._websocket_can_subscribe_run(
            new_socket, conv["id"], "gen:7004"
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server._active_generations.pop(key, None)
        server._generation_snapshots.pop(key, None)


async def test_every_agent_generation_uses_workspace_safety_wrapper(
    monkeypatch, tmp_database, tmp_path
):
    import server

    conv = await db.create_conversation(
        "Safe wrapper",
        mode="codex",
        project_dir=str(tmp_path),
    )
    calls = []
    sent = []
    token = object()

    def fake_capture(workspace, recovery_root, **kwargs):
        calls.append(("capture", workspace, kwargs["generation_id"]))
        return token

    def fake_finalize(snapshot):
        assert snapshot is token
        calls.append(("finalize",))
        return {"type": "workspace_change_report", "changed_count": 1, "warnings": []}

    async def fake_generation(websocket, conv_id, data):
        calls.append(("generate", conv_id))

    async def fake_send(conv_id, message):
        sent.append((conv_id, message))

    monkeypatch.setattr(server, "capture_workspace_snapshot", fake_capture)
    monkeypatch.setattr(server, "finalize_workspace_snapshot", fake_finalize)
    monkeypatch.setattr(server, "default_recovery_root", lambda: tmp_path / "recovery")
    monkeypatch.setattr(server, "_handle_generation", fake_generation)
    monkeypatch.setattr(server, "_ws_send", fake_send)

    current = asyncio.current_task()
    old_key = getattr(current, "_gen_key", None)
    current._gen_key = (conv["id"], 10, 99)
    try:
        await server._handle_generation_with_workspace_safety(None, conv["id"], {})
    finally:
        if old_key is None:
            delattr(current, "_gen_key")
        else:
            current._gen_key = old_key

    assert calls[0] == ("capture", str(tmp_path), 99)
    assert calls[1:] == [("generate", conv["id"]), ("finalize",)]
    assert sent[0][1] == {
        "type": "generation_preparing",
        "phase": "workspace_snapshot",
        "text": "Preparing workspace recovery snapshot…",
        "parent_id": None,
        "cc_model": conv.get("cc_model"),
    }
    assert sent[1][1]["type"] == "generation_preparing"
    assert sent[1][1]["phase"] == "launching"
    assert sent[1][1]["text"].startswith("Workspace protected (")
    assert sent[1][1]["preparation_ms"] >= 0
    assert sent[2][1]["type"] == "workspace_change_report"
