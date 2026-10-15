"""Hard invariants for Loom thread placement and per-page cursors."""

import asyncio

import database as db


async def _empty_cursor(conv_id: int, client_id: str) -> dict:
    thread = await db.create_loom_thread(conv_id, client_instance_id=client_id)
    return await db.select_loom_cursor(
        conv_id, client_id, thread["thread_id"], None, force=True
    )


def _contract(cursor: dict, *, intent: str, parent_id):
    return {
        "client_instance_id": cursor["client_instance_id"],
        "loom_thread_id": cursor["thread_id"],
        "cursor_id": cursor["cursor_id"],
        "cursor_revision": cursor["revision"],
        "expected_leaf_id": cursor["leaf_message_id"],
        "topology_intent": intent,
        "parent_id": parent_id,
    }


async def test_backfill_adds_thread_identity_without_changing_tree(tmp_database):
    conv = await db.create_conversation("Historical tree")
    root = await db.add_message(conv["id"], "user", "root")
    child = await db.add_message(
        conv["id"], "assistant", "child", parent_id=root["id"]
    )
    conn = await db.get_db()
    await conn.execute(
        "UPDATE messages SET loom_thread_id = NULL WHERE conversation_id = ?",
        (conv["id"],),
    )
    await conn.execute(
        "DELETE FROM loom_threads WHERE conversation_id = ?", (conv["id"],)
    )
    await conn.commit()

    await db._backfill_loom_threads(conn)

    restored_root = await db.get_message(root["id"])
    restored_child = await db.get_message(child["id"])
    assert restored_root["parent_id"] is None
    assert restored_child["parent_id"] == root["id"]
    assert restored_root["loom_thread_id"] == restored_child["loom_thread_id"]
    assert restored_root["loom_thread_id"] == f"loom:{conv['id']}:{root['id']}"


async def test_child_inherits_thread_and_second_root_is_rejected(tmp_database):
    conv = await db.create_conversation("One root per thread")
    cursor = await _empty_cursor(conv["id"], "page-a")
    root = await db.add_message(
        conv["id"], "user", "root", loom_thread_id=cursor["thread_id"]
    )
    child = await db.add_message(
        conv["id"], "assistant", "child", parent_id=root["id"]
    )
    assert child["loom_thread_id"] == root["loom_thread_id"]

    try:
        await db.add_message(
            conv["id"], "user", "bad root", loom_thread_id=cursor["thread_id"]
        )
    except ValueError as exc:
        assert "second root" in str(exc)
    else:
        raise AssertionError("A Loom thread accepted two root messages")


async def test_cursor_revision_rejects_stale_navigation_and_advance(tmp_database):
    conv = await db.create_conversation("Cursor CAS")
    cursor = await _empty_cursor(conv["id"], "page-a")
    root = await db.add_message(
        conv["id"], "user", "root", loom_thread_id=cursor["thread_id"]
    )
    advanced = await db.advance_loom_cursor(
        cursor["cursor_id"],
        conversation_id=conv["id"],
        client_instance_id="page-a",
        thread_id=cursor["thread_id"],
        expected_leaf_id=None,
        expected_revision=cursor["revision"],
        new_leaf_id=root["id"],
    )
    assert advanced["leaf_message_id"] == root["id"]

    for operation in (
        db.select_loom_cursor(
            conv["id"], "page-a", cursor["thread_id"], root["id"],
            expected_revision=cursor["revision"],
        ),
        db.advance_loom_cursor(
            cursor["cursor_id"],
            conversation_id=conv["id"],
            client_instance_id="page-a",
            thread_id=cursor["thread_id"],
            expected_leaf_id=None,
            expected_revision=cursor["revision"],
            new_leaf_id=root["id"],
        ),
    ):
        try:
            await operation
        except db.LoomCursorConflict:
            pass
        else:
            raise AssertionError("A stale cursor mutation was accepted")


async def test_user_message_contract_fails_closed_even_without_parallel_mode(
    client, mock_llama
):
    conv = await db.create_conversation("Fail closed", mode="codex")
    response = await client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"role": "user", "content": "ambiguous", "parent_id": None},
    )
    assert response.status_code == 409
    assert "legacy message contract" in response.json()["detail"]
    assert await db.get_conversation_tree(conv["id"]) == []


async def test_every_external_message_write_requires_explicit_parent(
    client, mock_llama
):
    conv = await db.create_conversation("No active-leaf fallback")
    root = await db.add_message(conv["id"], "user", "existing root")
    await db.set_active_branch(conv["id"], root["id"])

    response = await client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={"role": "system", "content": "must not infer placement"},
    )
    assert response.status_code == 400
    assert "parent_id is required" in response.json()["detail"]
    assert await db.get_children(root["id"]) == []


async def test_global_active_branch_cannot_redirect_an_exact_cursor_send(
    client, mock_llama
):
    conv = await db.create_conversation("Active is not topology", mode="codex")
    first = await db.add_message(conv["id"], "user", "first root")
    second = await db.add_message(conv["id"], "user", "second root")
    cursor = await db.select_loom_cursor(
        conv["id"], "page-first", first["loom_thread_id"], first["id"]
    )
    await db.set_active_branch(conv["id"], second["id"])

    response = await client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={
            "role": "user",
            "content": "belongs under first",
            "send_intent": "continue",
            **_contract(cursor, intent="continue", parent_id=first["id"]),
        },
    )
    assert response.status_code == 200
    message = response.json()
    assert message["parent_id"] == first["id"]
    assert message["loom_thread_id"] == first["loom_thread_id"]
    assert message["loom_thread_id"] != second["loom_thread_id"]


async def test_message_write_launches_the_exact_persisted_thread_server_side(
    client, mock_llama, monkeypatch
):
    import server

    conv = await db.create_conversation("Atomic send and launch", mode="codex")
    cursor = await _empty_cursor(conv["id"], "page-launch")
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
        return (conv_id, data["parent_id"], 8801), object()

    monkeypatch.setattr(server, "_launch_generation_task", fake_launch)
    response = await client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={
            "role": "user",
            "content": "persist and run here",
            "send_intent": "new_thread",
            "launch_generation": True,
            **_contract(cursor, intent="new_thread", parent_id=None),
        },
    )
    assert response.status_code == 200
    message = response.json()
    assert message["generation_ids"] == [8801]
    assert len(launches) == 1
    websocket, launched_conv, data, client_id = launches[0]
    assert websocket is None
    assert launched_conv == conv["id"]
    assert data["parent_id"] == message["id"]
    assert data["loom_thread_id"] == message["loom_thread_id"]
    assert client_id == "page-launch"


async def test_new_thread_and_continuation_advance_exact_cursor(client, mock_llama):
    conv = await db.create_conversation("Exact continuation", mode="codex")
    cursor = await _empty_cursor(conv["id"], "page-a")
    root_response = await client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={
            "role": "user",
            "content": "standalone",
            "send_intent": "new_thread",
            **_contract(cursor, intent="new_thread", parent_id=None),
        },
    )
    assert root_response.status_code == 200
    root = root_response.json()
    cursor = root["loom_cursor"]
    assert root["parent_id"] is None
    assert cursor["leaf_message_id"] == root["id"]

    continuation = await client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={
            "role": "user",
            "content": "continue",
            "send_intent": "continue",
            **_contract(cursor, intent="continue", parent_id=root["id"]),
        },
    )
    assert continuation.status_code == 200
    message = continuation.json()
    assert message["parent_id"] == root["id"]
    assert message["loom_thread_id"] == root["loom_thread_id"]
    assert message["loom_cursor"]["leaf_message_id"] == message["id"]


async def test_two_simultaneous_sends_from_one_cursor_cannot_both_attach(
    client, mock_llama
):
    conv = await db.create_conversation("Send race", mode="codex")
    cursor = await _empty_cursor(conv["id"], "page-race")
    payload = {
        "role": "user",
        "send_intent": "new_thread",
        **_contract(cursor, intent="new_thread", parent_id=None),
    }

    first, second = await asyncio.gather(
        client.post(
            f"/api/conversations/{conv['id']}/messages",
            json={**payload, "content": "first contender"},
        ),
        client.post(
            f"/api/conversations/{conv['id']}/messages",
            json={**payload, "content": "second contender"},
        ),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    tree = await db.get_conversation_tree(conv["id"])
    assert len(tree) == 1
    assert tree[0]["parent_id"] is None


async def test_cross_thread_parent_is_rejected_without_mutating_tree(
    client, mock_llama
):
    conv = await db.create_conversation("Cross-thread rejection", mode="codex")
    first = await db.add_message(conv["id"], "user", "first root")
    second = await db.add_message(conv["id"], "user", "second root")
    cursor = await db.select_loom_cursor(
        conv["id"], "page-a", first["loom_thread_id"], first["id"]
    )
    before = await db.get_conversation_tree(conv["id"])
    response = await client.post(
        f"/api/conversations/{conv['id']}/messages",
        json={
            "role": "user",
            "content": "must not cross",
            "send_intent": "continue",
            **_contract(cursor, intent="continue", parent_id=second["id"]),
        },
    )
    assert response.status_code == 409
    assert await db.get_conversation_tree(conv["id"]) == before


async def test_branch_delete_moves_affected_viewport_to_surviving_parent(tmp_database):
    conv = await db.create_conversation("Delete cursor fallback")
    root = await db.add_message(conv["id"], "user", "root")
    child = await db.add_message(
        conv["id"], "assistant", "child", parent_id=root["id"]
    )
    cursor = await db.select_loom_cursor(
        conv["id"], "page-a", root["loom_thread_id"], child["id"]
    )
    await db.delete_branch(child["id"])
    restored = await db.get_client_loom_cursor(conv["id"], "page-a")
    assert restored["leaf_message_id"] == root["id"]
    assert restored["revision"] == cursor["revision"] + 1


async def test_message_routes_reject_cross_conversation_ids(client, mock_llama):
    first_conv = await db.create_conversation("First")
    second_conv = await db.create_conversation("Second")
    message = await db.add_message(first_conv["id"], "user", "private")

    branch = await client.get(
        f"/api/conversations/{second_conv['id']}/branch/{message['id']}"
    )
    update = await client.put(
        f"/api/conversations/{second_conv['id']}/messages/{message['id']}",
        json={"content": "overwrite"},
    )
    delete = await client.delete(
        f"/api/conversations/{second_conv['id']}/messages/{message['id']}"
    )
    assert branch.status_code == update.status_code == delete.status_code == 404
    assert (await db.get_message(message["id"]))["content"] == "private"
