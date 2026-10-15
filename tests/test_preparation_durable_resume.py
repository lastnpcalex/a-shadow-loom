"""Component test: durable preparation crash recovery.

Uses the real SQLite preparation_operations record. After accepted chunks,
the coordinator is torn down mid-operation, the database connection is
closed and reopened, a fresh coordinator is constructed, and the operation
must resume from the exact next fragment with failed calls still counted.
"""
import asyncio
import json

import pytest

import context_preparation
import database as db
import replay_budget
from context_preparation import (
    BoundedPreparationCoordinator,
    PreparationBudget,
)
from context_chunking import chunk_prefix_fragments


VALID_STATE = {
    "objective": "Continue.", "constraints": "", "decisions": "",
    "completed_effects": "", "uncertain_effects": "", "pending_work": "",
    "artifacts": "", "questions": "", "sources": "",
}


@pytest.fixture(autouse=True)
def durability_on(monkeypatch, tmp_path):
    monkeypatch.setattr(context_preparation, "DURABILITY", True)
    monkeypatch.setenv("EXO_STATE_DIR", str(tmp_path / "exo-state"))


async def fake_capacity(model, harness, backend=None):
    return 9_000, "test"


@pytest.mark.parametrize("crash_after", [1, 2])
async def test_crash_resume_continues_exact_cursor_and_budget(monkeypatch, crash_after):
    monkeypatch.setattr(replay_budget, "context_capacity", fake_capacity)

    records = [
        {"id": 1, "role": "user", "content": "A" * 9000},
        {"id": 2, "role": "assistant", "content": "B" * 9000},
        {"id": 3, "role": "user", "content": "C" * 9000},
    ]

    calls = []
    crash_phase = True
    blocked_call_started = asyncio.Event()
    never_release = asyncio.Event()

    async def fake_execute(prompt, destination, isolated_cwd, *, output_cap,
                           timeout_seconds=120.0, status=None):
        idx = len(calls)
        calls.append({"prompt": prompt, "fragments": idx})
        # Make the simulated crash deterministic: after exactly crash_after
        # accepted chunks, hold the next provider call open until the worker is
        # cancelled. Polling a fast completed operation is not crash evidence.
        if crash_phase and len(calls) > crash_after:
            blocked_call_started.set()
            await never_release.wait()
        state = dict(VALID_STATE)
        state["pending_work"] = f"chunk {idx}"
        attestation = {"status": "verified", "effective_model": "test-model",
                       "launch_model": "test-model"}
        return json.dumps(state), attestation

    monkeypatch.setattr(context_preparation, "execute_preparation_chunk", fake_execute)

    destination = {"harness": "codex", "model": "test-model", "backend": None}

    # First worker: accept `crash_after` chunks, then "crash" (abandon).
    first = BoundedPreparationCoordinator(destination, conversation_id=42,
                                          budget=PreparationBudget(maximum_calls=64))
    first_budget = first.budget

    async def crash_midway():
        # Run summarize but abort the task only after the durable record
        # shows the required accepted chunks (not merely the call count).
        task = asyncio.create_task(first.summarize(json.dumps(records), 1000))
        async def recorded_ordinal():
            conn = await db.get_db()
            async with conn.execute(
                    "SELECT covered_cursor FROM preparation_operations "
                    "WHERE phase='running' AND accepted_state IS NOT NULL") as cur:
                rows = await cur.fetchall()
            best = -1
            for (cursor_json,) in rows:
                try:
                    best = max(best, int(json.loads(cursor_json or "{}").get("ordinal", -1)))
                except (ValueError, TypeError):
                    pass
            return best
        while True:
            if await recorded_ordinal() >= crash_after - 1:
                break
            if task.done():
                break
            await asyncio.sleep(0.01)
        if not task.done():
            await asyncio.wait_for(blocked_call_started.wait(), timeout=1)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        # The exact pre-crash accepted boundary from the production record.
        crash_record = await recorded_ordinal()
        first_budget.recorded_ordinal = crash_record
        return first_budget

    first_budget = await crash_midway()
    crash_phase = False
    accepted_before = len(calls)
    assert accepted_before >= crash_after

    # Simulated crash bookkeeping: close and reopen the DB connection.
    await db.close_db()
    await db.init_db()

    conn = await db.get_db()
    async with conn.execute("SELECT operation_id, conversation_id, phase, "
                            "calls_used, covered_cursor, destination FROM "
                            "preparation_operations") as cur:
        rows = await cur.fetchall()
    print("DB ROWS AFTER CRASH:", [dict(r) for r in rows])

    # A fresh coordinator reconstructs the operation.
    second = BoundedPreparationCoordinator(destination, conversation_id=42,
                                           budget=PreparationBudget(maximum_calls=64))
    calls.clear()
    result = await second.summarize(json.dumps(records), 1000)

    state = json.loads(result.text)
    assert set(state) == set(VALID_STATE)
    resumed_calls = len(calls)

    # The exact accepted boundary recorded before the crash (production
    # record, not test-invented): resume must continue from ordinal+1.
    accepted_ordinal = first_budget.recorded_ordinal
    assert accepted_ordinal >= 0, "no durable boundary was recorded before the crash"
    conn = await db.get_db()
    async with conn.execute(
            "SELECT calls_used FROM preparation_operations "
            "WHERE phase='completed' ORDER BY updated_at DESC LIMIT 1") as cur:
        row = await cur.fetchone()
    assert row is not None, "resumed operation never completed"
    assert int(row[0]) >= first_budget.calls_used, (
        "crash-resumed budget consumption was reset")

    # Baseline: a from-scratch run of the same records on a clean record set.
    third = BoundedPreparationCoordinator(destination, conversation_id=43,
                                          budget=PreparationBudget(maximum_calls=64))
    calls.clear()
    await third.summarize(json.dumps(records), 1000)
    baseline_calls = len(calls)

    # Accepted chunks were NOT resent: the resumed run started from the
    # recorded boundary and made exactly the remaining number of calls.
    assert resumed_calls == baseline_calls - (accepted_ordinal + 1), (
        f"resumed run made {resumed_calls} calls; "
        f"baseline {baseline_calls} minus {accepted_ordinal + 1} recorded-accepted")

    # The consumed budget carried over from the crashed operation.
    prior_rows = []
    conn = await db.get_db()
    async with conn.execute(
            "SELECT calls_used FROM preparation_operations ORDER BY updated_at") as cur:
        prior_rows = [r[0] for r in await cur.fetchall()]
    assert max(prior_rows) >= first_budget.calls_used, (
        "crash-resumed budget was reset")


async def test_edited_history_cannot_reuse_operation(monkeypatch):
    monkeypatch.setattr(replay_budget, "context_capacity", fake_capacity)

    records = [{"id": 1, "role": "user", "content": "A" * 9000}]
    edited = [{"id": 1, "role": "user", "content": "EDITED" * 4000}]

    async def fake_execute(prompt, destination, isolated_cwd, *, output_cap,
                           timeout_seconds=120.0, status=None):
        state = dict(VALID_STATE)
        state["pending_work"] = "done"
        return json.dumps(state), {"status": "verified",
                                   "effective_model": "test-model",
                                   "launch_model": "test-model"}

    monkeypatch.setattr(context_preparation, "execute_preparation_chunk", fake_execute)

    destination = {"harness": "codex", "model": "test-model", "backend": None}
    first = BoundedPreparationCoordinator(destination, conversation_id=42,
                                          budget=PreparationBudget(maximum_calls=64))
    await first.summarize(json.dumps(records), 1000)

    # An edited branch (different fingerprint) must never resume the record.
    second = BoundedPreparationCoordinator(destination, conversation_id=42,
                                           budget=PreparationBudget(maximum_calls=64))
    calls = []

    async def counting_execute(prompt, destination, isolated_cwd, *, output_cap,
                               timeout_seconds=120.0, status=None):
        calls.append(prompt)
        return await fake_execute(prompt, destination, isolated_cwd,
                                  output_cap=output_cap)

    monkeypatch.setattr(context_preparation, "execute_preparation_chunk",
                        counting_execute)
    await second.summarize(json.dumps(edited), 1000)
    assert len(calls) >= 1, "edited history should require fresh preparation"
