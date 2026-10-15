"""Explicit recovery admission; failures are not automatic retry authorization."""
import asyncio

import pytest

import database as db
import server
from agent_supervisor import AgentSupervisor


async def failed_child():
    conv = await db.create_conversation("Synthetic failed child")
    run = await db.create_agent_run("failed-recovery", conv["id"], "Finish the recorded action")
    await db.append_agent_event(run["run_id"], "tool_start", {"id": "effect", "name": "record_action"})
    await db.update_agent_run(run["run_id"], status="failed", error="Provider EOF",
                              provider_session_id="unverified", result={"text": "partial"})
    return run


async def test_failed_child_requires_explicit_recovery(client, monkeypatch):
    run = await failed_child()
    # API admission must not start a dispatcher or provider in this test.
    monkeypatch.setattr(server, "_agent_supervisor", AgentSupervisor())
    response = await client.post(f"/api/agent-runs/{run['run_id']}/resume")
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    saved = await db.get_agent_run(run["run_id"])
    assert saved["provider_session_id"] is None
    assert saved["result"] == {"text": "partial"}
    assert (await db.list_agent_events(run["run_id"]))[0]["event_kind"] == "tool_start"
    assert (await client.post(f"/api/agent-runs/{run['run_id']}/resume")).status_code == 409


@pytest.mark.parametrize("state", [
    {"status": "running"}, {"status": "completed"}, {"status": "cancelled"},
    {"status": "cancel_requested"}, {"pid": 123456789}, {"cancel_requested": 1},
    {"status": "paused", "pid": 123456789},
    {"image_request": {"output_path": "example.png"}},
])
async def test_recovery_never_clears_live_ownership_or_overrides_cancel(state, client, monkeypatch):
    run = await failed_child()
    await db.update_agent_run(run["run_id"], **state)
    if state.get("image_request"):
        async with db.write_transaction() as connection:
            await connection.execute("UPDATE agent_runs SET image_request=? WHERE run_id=?",
                                     ('{"output_path":"example.png"}', run["run_id"]))
    before = await db.get_agent_run(run["run_id"])
    monkeypatch.setattr(server, "_agent_supervisor", AgentSupervisor())
    assert (await client.post(f"/api/agent-runs/{run['run_id']}/resume")).status_code == 409
    assert await db.get_agent_run(run["run_id"]) == before


async def test_duplicate_recovery_claims_have_one_winner():
    run = await failed_child()
    outcomes = await asyncio.gather(db.request_agent_resume(run["run_id"]),
                                    db.request_agent_resume(run["run_id"]), return_exceptions=True)
    assert sum(isinstance(value, dict) and value["status"] == "queued" for value in outcomes) == 1
    assert sum(isinstance(value, ValueError) for value in outcomes) == 1


async def test_failed_run_does_not_dispatch_without_resume():
    run = await failed_child()
    calls = []
    async def launch(*args):
        calls.append(args)
        return {}
    supervisor = AgentSupervisor(launch)
    try:
        await supervisor.start()
        await asyncio.sleep(.6)  # covers a scheduler tick, not a provider wait
        assert not calls
        assert (await db.get_agent_run(run["run_id"]))["status"] == "failed"
    finally:
        await supervisor.stop()  # only this fixture-owned dispatcher
