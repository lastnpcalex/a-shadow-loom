import dataclasses
import asyncio

import pytest

import database as db
from context_checkpoints import SQLiteCheckpointStore, issue_retrieval_scope, retrieve
from continuity_engine import BranchSnapshot, Checkpoint, fingerprint


async def saved_branch():
    conv = await db.create_conversation("Synthetic continuity")
    user = await db.add_message(conv["id"], "user", "Keep synthetic key private")
    assistant = await db.add_message(conv["id"], "assistant", "Saved synthetic artifact", parent_id=user["id"])
    return BranchSnapshot.from_messages(await db.get_branch_to_root(assistant["id"]))


async def test_checkpoint_round_trip_and_stale_write(tmp_database):
    snapshot = await saved_branch()
    cp = Checkpoint(snapshot.conversation_id, fingerprint(snapshot.messages),
                    tuple(m["id"] for m in snapshot.messages), "Synthetic summary", "synthetic-local")
    store = SQLiteCheckpointStore()
    await store.save(cp)
    assert await store.find(cp.conversation_id, cp.fingerprint, cp.summary_route) == cp
    assert await store.find(cp.conversation_id, cp.fingerprint, "other-route") is None
    await db.update_message_content(snapshot.messages[0]["id"], content="Correction")
    with pytest.raises(ValueError, match="changed"):
        await store.save(dataclasses.replace(cp, summary="stale result"))
    assert (await store.find(cp.conversation_id, cp.fingerprint, cp.summary_route)).summary == "Synthetic summary"


async def test_retrieval_scope_is_exact_and_paginated(tmp_database):
    snapshot = await saved_branch()
    token = await issue_retrieval_scope(snapshot)
    listing = await retrieve(token, snapshot.conversation_id)
    assert [m["id"] for m in listing["messages"]] == [m["id"] for m in snapshot.messages]
    first = await retrieve(token, snapshot.conversation_id, message_id=snapshot.messages[0]["id"], limit=20)
    assert len(first["record_fragment"]) == 20
    assert first["next_offset"] == 20
    sibling = await db.add_message(snapshot.conversation_id, "user", "Sibling private content",
                                   parent_id=snapshot.messages[0]["id"])
    with pytest.raises(PermissionError):
        await retrieve(token, snapshot.conversation_id, message_id=sibling["id"])
    with pytest.raises(PermissionError):
        await retrieve(token, snapshot.conversation_id + 1)
    with pytest.raises(PermissionError):
        await retrieve("wrong", snapshot.conversation_id)
    await db.update_message_content(snapshot.messages[0]["id"], content="Edited branch")
    with pytest.raises(PermissionError, match="edited"):
        await retrieve(token, snapshot.conversation_id)


async def test_expired_scope_cannot_retrieve(tmp_database, monkeypatch):
    import context_checkpoints
    snapshot = await saved_branch()
    token = await issue_retrieval_scope(snapshot, ttl_seconds=1)
    now = context_checkpoints.time.time()
    monkeypatch.setattr(context_checkpoints.time, "time", lambda: now + 2)
    with pytest.raises(PermissionError):
        await retrieve(token, snapshot.conversation_id)


async def test_existing_database_initialization_is_idempotent(tmp_database):
    snapshot = await saved_branch()
    await db.init_db()
    token = await issue_retrieval_scope(snapshot)
    assert (await retrieve(token, snapshot.conversation_id))["messages"]


async def test_pre_owner_schema_migrates_without_losing_existing_scope(tmp_database):
    snapshot = await saved_branch()
    token = await issue_retrieval_scope(snapshot)
    async with db.write_transaction() as connection:
        await connection.execute("ALTER TABLE context_retrieval_scopes DROP COLUMN owner_id")
        await connection.execute("ALTER TABLE context_retrieval_scopes DROP COLUMN frozen_tail")
    await db.init_db()
    assert (await retrieve(token, snapshot.conversation_id))["messages"]


async def test_native_continuation_renews_expired_scope_without_replay(tmp_database, monkeypatch):
    import context_checkpoints
    import context_runtime
    from unittest.mock import AsyncMock
    snapshot = await saved_branch()
    old = await issue_retrieval_scope(snapshot, ttl_seconds=1)
    now = context_checkpoints.time.time()
    monkeypatch.setattr(context_checkpoints.time, "time", lambda: now + 2)
    monkeypatch.setattr(context_runtime, "assess_payload", AsyncMock(side_effect=AssertionError("no replay budget")))
    monkeypatch.setattr(context_runtime, "ConfiguredLocalSummarizer", lambda *a: pytest.fail("no summary"))
    prompt = await context_runtime.renew_native_retrieval("New task", list(snapshot.messages))
    new = prompt.split("context_scope:", 1)[1].split()[0]
    assert new != old and prompt.endswith("\n\nNew task")
    assert "Saved synthetic artifact" not in prompt
    with pytest.raises(PermissionError):
        await retrieve(old, snapshot.conversation_id)
    assert (await retrieve(new, snapshot.conversation_id))["messages"]


@pytest.mark.parametrize("ending", ["completed", "cancelled", "restart"])
async def test_long_turn_scope_lives_only_with_exact_owner(tmp_database, monkeypatch, ending):
    import context_checkpoints
    import context_scope_lifetime
    snapshot = await saved_branch()
    release = asyncio.Event()
    owner = asyncio.create_task(release.wait())
    try:
        token = await issue_retrieval_scope(snapshot, ttl_seconds=1, owner=owner)
        now = context_checkpoints.time.time()
        monkeypatch.setattr(context_checkpoints.time, "time", lambda: now + 7200)
        # Another issuance must not prune the still-active long turn.
        await issue_retrieval_scope(snapshot)
        assert (await retrieve(token, snapshot.conversation_id))["messages"]
        if ending == "completed":
            release.set()
            await owner
        elif ending == "cancelled":
            owner.cancel()  # revokes even before cancellation/teardown finishes
        else:
            monkeypatch.setattr(context_scope_lifetime, "_owners", {})
        with pytest.raises(PermissionError, match="no longer active"):
            await retrieve(token, snapshot.conversation_id)
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)


async def test_live_owner_never_bypasses_branch_revision(tmp_database):
    snapshot = await saved_branch()
    token = await issue_retrieval_scope(snapshot, owner=asyncio.current_task())
    await db.update_message_content(snapshot.messages[0]["id"], content="Changed source")
    with pytest.raises(PermissionError, match="edited"):
        await retrieve(token, snapshot.conversation_id)
