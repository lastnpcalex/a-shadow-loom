"""Component test (NOT a production binding test): real multi-chunk
preparation through the production coordinator, real checkpoint publication
in SQLite, next-turn view reuse, and next-next-turn native resume. Only the
transport (execute_preparation_chunk) and the runtime capacity measurement
are faked; chunking, rolling state, admission, attestation, schema
validation, the reducer and the checkpoint store are production code.

The working_launch / native-session segments of the oracle trace are
constructed by THIS TEST, so they do not prove a production working launch
or a provider-generated session. Production binding evidence must come from
a controlled fake provider process driven through server.py (see the
portable continuity repair guide, Rule Zero)."""
import asyncio
import json

import pytest

import context_preparation
import replay_budget
from continuity_engine import (
    BranchSnapshot, ContextEngine, NativeCapability, fingerprint,
)
from context_checkpoints import SQLiteCheckpointStore
from context_runtime import DestinationSummarizer
from tests.portable_context_trace_oracle import assert_incremental_handoff

import database as db


VALID_STATE = {
    "objective": "Continue the recorded task.",
    "constraints": "Keep ORCHID private.",
    "decisions": "Follow the recorded plan.",
    "completed_effects": "",
    "uncertain_effects": "",
    "pending_work": "",
    "artifacts": "",
    "questions": "",
    "sources": "",
}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Offline test must not launch inference")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)

    async def fake_capacity(model, harness, backend=None):
        return 9_000, "test"

    monkeypatch.setattr(replay_budget, "context_capacity", fake_capacity)

    async def fake_execute(prompt, destination, isolated_cwd, *, output_cap,
                           timeout_seconds=120.0, status=None):
        # The transport is the only mock: it attests the requested model and
        # returns a schema-valid state quoting the evidence it consumed.
        state = dict(VALID_STATE)
        state["completed_effects"] = f"consumed {len(prompt)} chars"
        state["sources"] = "records"
        attestation = {"status": "verified",
                       "effective_model": destination["model"],
                       "launch_model": destination["model"]}
        return json.dumps(state), attestation

    monkeypatch.setattr(context_preparation, "execute_preparation_chunk", fake_execute)


async def fake_measure(text):
    return len(text)  # Test unit, never described as provider tokens.


async def saved_branch():
    conv = await db.create_conversation("Handoff e2e", mode="claude")
    u1 = await db.add_message(conv["id"], "user", "X" * 9000)
    a1 = await db.add_message(conv["id"], "assistant", "Y" * 9000, parent_id=u1["id"])
    u2 = await db.add_message(conv["id"], "user", "Z" * 9000, parent_id=a1["id"])
    a2 = await db.add_message(conv["id"], "assistant", "W" * 4000, parent_id=u2["id"])
    current = await db.add_message(conv["id"], "user", "Continue the work.",
                                   parent_id=a2["id"])
    branch = await db.get_branch_to_root(current["id"])
    return conv, branch


async def test_multichunk_preparation_publishes_reuses_and_resumes():
    conv, branch = await saved_branch()
    snapshot = BranchSnapshot.from_messages(branch)
    summarizer = DestinationSummarizer(model="test-model", harness="claude",
                                       workspace=".", conv_id=conv["id"])
    notice = "Scoped fixture evidence is available."

    # Turn 1: oversized branch -> real multi-chunk preparation + publication.
    engine = ContextEngine(store=SQLiteCheckpointStore(), summarizer=summarizer,
                           measure=fake_measure)
    plan = await engine.prepare(snapshot, NativeCapability(None, "handoff"),
                                input_budget=20_000, retrieval_notice=notice)
    assert plan.kind == "checkpoint", "Preparation did not produce a working plan"
    checkpoint = plan.checkpoint
    assert checkpoint is not None and checkpoint.summary

    trace = asyncio.current_task()._context_trace
    assert trace is not None
    assert len(trace["preparation_calls"]) >= 2, "No multi-chunk preparation ran"
    assert trace["minimum_accepted_calls"] == len(trace["preparation_calls"])
    for call in trace["preparation_calls"]:
        assert call["tools_enabled"] is False
        assert call["accepted"] is True
        assert call["attested_model"] == "test-model"
        assert call["request_tokens"] + call["output_reserve"] <= call["capacity"]

    # The checkpoint is genuinely published in the store.
    stored = await SQLiteCheckpointStore().find(
        conv["id"], checkpoint.fingerprint, checkpoint.summary_route)
    assert stored is not None and stored.summary == checkpoint.summary

    # Turn 2: new current request; the published view is reused verbatim.
    answer = await db.add_message(conv["id"], "assistant", "Recent answer.",
                                  parent_id=branch[-1]["id"])
    new_user = await db.add_message(conv["id"], "user", "New current task.",
                                    parent_id=answer["id"])
    branch2 = await db.get_branch_to_root(new_user["id"])
    snapshot2 = BranchSnapshot.from_messages(branch2)
    engine2 = ContextEngine(store=SQLiteCheckpointStore(), summarizer=summarizer,
                            measure=fake_measure)
    plan2 = await engine2.prepare(snapshot2, NativeCapability(None, "handoff"),
                                  input_budget=20_000, retrieval_notice=notice,
                                  preferred=checkpoint)
    assert plan2.kind == "checkpoint", "Published view was not reused next turn"
    assert plan2.checkpoint.summary == checkpoint.summary
    assert "New current task." in plan2.prompt

    # Turn 3: the working turn produced a native session; next turn resumes it.
    engine3 = ContextEngine(store=SQLiteCheckpointStore(), summarizer=None,
                            measure=fake_measure)
    plan3 = await engine3.prepare(snapshot2,
                                  NativeCapability("native-session-1", "resume"),
                                  input_budget=20_000, retrieval_notice="")
    assert plan3.kind == "native" and plan3.prompt == "New current task."

    # The production trace satisfies the structural handoff oracle.
    history = snapshot.messages[:-1]
    starts = [i for i, m in enumerate(history) if m["role"] == "user"]
    cut = [i for i in starts[-2:] if i > 0][0]
    recent = list(history[cut:]) + [snapshot.messages[-1]]
    full_trace = dict(trace)
    full_trace["expected_recent_records"] = recent
    full_trace["expected_current_request"] = "Continue the work."
    full_trace["working_launch"] = {
        "route": trace["selected_route"],
        "state_id": trace["preparation_calls"][-1]["result_state_id"],
        "recent_records": recent,
        "current_request": "Continue the work.",
        "request_tokens": 1000,
        "output_reserve": 4096,
        "capacity": 1_000_000,
        "completed": True,
        "native_session_id": "native-session-1",
    }
    full_trace["followup_source_session"] = "native-session-1"
    full_trace["retrieval_fallback"] = False
    full_trace["unselected_inference_calls"] = 0
    assert_incremental_handoff(full_trace)
