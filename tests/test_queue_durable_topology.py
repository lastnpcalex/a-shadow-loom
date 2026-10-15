"""Queue topology must survive loss of the volatile streaming registry."""
import pytest
from fastapi import HTTPException

import database as db
import server
from tests.test_parallel_agents import _message_contract


@pytest.mark.parametrize("anchor", ["prompt", "draft", "other_root", "branch_edit"])
@pytest.mark.parametrize("miss_at_preflight", [False, True])
async def test_durable_lane_preserves_continuation_not_sibling(client, monkeypatch, anchor, miss_at_preflight):
    monkeypatch.setattr(server, "_active_generations", {})
    monkeypatch.setattr(server, "_generation_snapshots", {})
    monkeypatch.setattr(server, "_spawn_background_task", lambda coro, *a, **k: coro.close())
    conv = await db.create_conversation("Durable queue fixture", mode="codex")
    prompt = await db.add_message(conv["id"], "user", "running prompt")
    draft = await db.add_message(conv["id"], "assistant", "partial", parent_id=prompt["id"])
    await db.try_acquire_generation_lease(conv["id"], "fixture-lease", user_msg_id=prompt["id"])
    await db.bind_generation_lease("fixture-lease", draft_msg_id=draft["id"])
    if miss_at_preflight:
        from unittest.mock import AsyncMock
        # The draft became visible only when the lane reservation failed.
        monkeypatch.setattr(server, "_resolve_queue_target", AsyncMock(return_value=None))
    if anchor == "other_root":
        selected = await db.add_message(conv["id"], "user", "other root")
    else:
        selected = draft if anchor == "draft" else prompt
    contract = await _message_contract(conv["id"], "fixture-page", selected["id"],
                                       intent="branch" if anchor == "branch_edit" else "continue")
    response = await client.post(f"/api/conversations/{conv['id']}/messages", json={
        **contract, "role": "user", "content": "queued continuation",
        "parent_id": selected["id"], "send_intent": "continue", "launch_generation": True,
    })
    assert response.status_code == 200, response.text
    queued = response.json()
    assert queued["queued"]
    assert queued["queue_after_draft_id"] == draft["id"]
    expected_parent = selected["id"] if anchor in {"other_root", "branch_edit"} else draft["id"]
    assert queued["parent_id"] == expected_parent
    stored = await db.get_message(queued["id"])
    assert stored["parent_id"] == expected_parent
    if anchor in {"prompt", "draft"}:
        branch = await db.get_branch_to_root(queued["id"])
        assert [m["id"] for m in branch] == [prompt["id"], draft["id"], queued["id"]]


async def test_explicit_queue_target_survives_completion_race(tmp_database, monkeypatch):
    monkeypatch.setattr(server, "_active_generations", {})
    monkeypatch.setattr(server, "_generation_snapshots", {})
    conv = await db.create_conversation("Queue completion race", mode="codex")
    prompt = await db.add_message(conv["id"], "user", "prompt")
    draft = await db.add_message(conv["id"], "assistant", "finished", parent_id=prompt["id"])
    # The browser selected this exact draft before its terminal event arrived.
    assert await server._resolve_queue_target(conv["id"], prompt["id"], draft["id"]) == draft["id"]


@pytest.mark.parametrize("invalid", ["not-an-id", {}, []])
async def test_invalid_explicit_queue_target_is_a_client_error(monkeypatch, invalid):
    monkeypatch.setattr(server, "_generation_snapshots", {})
    conv = await db.create_conversation("Queue fixture", mode="codex")
    with pytest.raises(HTTPException) as error:
        await server._resolve_queue_target(conv["id"], None, invalid)
    assert error.value.status_code == 400


async def test_parallel_queue_uses_durable_exact_run_not_another_branch(monkeypatch):
    monkeypatch.setattr(server, "_generation_snapshots", {})
    conv = await db.create_conversation("Parallel queue fixture", mode="codex")
    await db.update_conversation_fields(conv["id"], parallel_agents_enabled=True)
    prompt = await db.add_message(conv["id"], "user", "prompt")
    other = await db.add_message(conv["id"], "user", "unrelated root")
    draft = await db.add_message(conv["id"], "assistant", "partial", parent_id=prompt["id"])
    await db.register_active_generation(draft["id"], conv["id"], pid=0)
    assert await server._resolve_queue_target(conv["id"], prompt["id"]) == draft["id"]
    assert await server._resolve_queue_target(conv["id"], other["id"]) is None
    sibling = await db.add_message(conv["id"], "assistant", "other partial", parent_id=prompt["id"])
    await db.register_active_generation(sibling["id"], conv["id"], pid=0)
    with pytest.raises(HTTPException) as error:
        await server._resolve_queue_target(conv["id"], prompt["id"])
    assert error.value.status_code == 409
    assert await server._resolve_queue_target(conv["id"], draft["id"]) == draft["id"]
