"""Negative, isolation, and safety tests for destination-owned portable context preparation.

Enforces:
1. Preparation never launches in '.' or any user project directory.
2. Preparation never uses permission_mode='plan'.
3. Teardown failures are never swallowed; zombie processes trigger ProcessTeardownError.
4. Giant records are partitioned and chunked contiguously with rolling state.
"""
import asyncio
import copy
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import context_runtime
from context_chunking import chunk_prefix_fragments, fragment_record
from context_preparation import (
    BoundedPreparationCoordinator,
    execute_preparation_chunk,
    isolated_preparation_cwd,
    verify_process_teardown,
)
from continuity_engine import (
    BranchSnapshot,
    ContextEngine,
    ContextLimit,
    NativeCapability,
    ProcessTeardownError,
    SummaryResult,
)
import exo_state


VALID_STATE = {'objective': 'Continue the task.', 'constraints': 'None recorded.', 'decisions': 'Use the recorded plan.', 'completed_effects': 'Save artifact completed (record 1).', 'uncertain_effects': '', 'pending_work': 'Verify output.', 'artifacts': '', 'questions': '', 'sources': '1'}

HARNESSES = ("claude", "codex", "gemini", "goose")


@pytest.fixture(autouse=True)
def _no_durability(monkeypatch):
    """Offline tests never touch the durable preparation-operation store."""
    monkeypatch.setattr("context_preparation.DURABILITY", False)


@pytest.mark.parametrize("harness", HARNESSES)
async def test_preparation_never_uses_plan_permission_mode(monkeypatch, harness):
    """Preparation must never use permission_mode='plan' or prompt to write implementation_plan.md."""
    captured_kwargs = {}

    async def mock_run(*args, **kwargs):
        captured_kwargs.update(kwargs)
        prompt = args[0] if args else kwargs.get("prompt", "")
        # Verify plan prompt is not injected
        assert "implementation_plan.md" not in prompt
        assert "PLAN MODE" not in prompt

        payload = json.dumps(VALID_STATE)
        attestation = {"status": "verified", "effective_model": "test-model",
                       "launch_model": "test-model"}
        async def fake_stream():
            yield {"type": "text_delta", "text": payload}
            yield {"type": "result", "result_text": payload,
                   "model_attestation": attestation}

        return None, fake_stream()

    import server
    client_name = {"claude": "claude_client", "codex": "codex_client",
                   "gemini": "gemini_client", "goose": "goose_client"}[harness]
    client = getattr(server, client_name)
    monkeypatch.setattr(client, f"run_{harness}", mock_run)

    summarizer = context_runtime.DestinationSummarizer(
        model="test-model",
        harness=harness,
        workspace=".",
        conv_id=42,
    )
    res = await summarizer.summarize(
        json.dumps([{"id": 1, "role": "user", "content": "hello"}]),
        max_tokens=1000,
    )
    state = json.loads(res.text)
    assert set(state) == set(VALID_STATE)
    assert captured_kwargs.get("permission_mode") != "plan"
    assert captured_kwargs.get("permission_mode") == "default"
    # Tool-free transport: the coordinator must request preparation=True from
    # every provider client, so no launcher runs an ordinary agent.
    assert captured_kwargs.get("preparation") is True


@pytest.mark.parametrize("harness", HARNESSES)
async def test_preparation_never_runs_in_project_dir_or_dot(monkeypatch, harness):
    """Preparation must execute in an isolated directory under exo_state, never in project dir or '.'."""
    captured_cwds = []

    async def mock_run(*args, **kwargs):
        cwd = kwargs.get("cwd")
        if not cwd and len(args) > 1:
            cwd = args[1]
        captured_cwds.append(str(cwd))

        payload = json.dumps(VALID_STATE)
        attestation = {"status": "verified", "effective_model": "test-model",
                       "launch_model": "test-model"}
        async def fake_stream():
            yield {"type": "text_delta", "text": payload}
            yield {"type": "result", "result_text": payload,
                   "model_attestation": attestation}

        return None, fake_stream()

    import server
    client_name = {"claude": "claude_client", "codex": "codex_client",
                   "gemini": "gemini_client", "goose": "goose_client"}[harness]
    client = getattr(server, client_name)
    monkeypatch.setattr(client, f"run_{harness}", mock_run)

    project_dir = os.path.abspath(".")
    summarizer = context_runtime.DestinationSummarizer(
        model="test-model",
        harness=harness,
        workspace=project_dir,
        conv_id=42,
    )
    await summarizer.summarize(
        json.dumps([{"id": 1, "role": "user", "content": "hello"}]),
        max_tokens=1000,
    )

    assert captured_cwds, "No run call was captured"
    assert captured_cwds[0]  # captured via kwargs or positional
    prep_root = str(exo_state.state_path("preparation")).lower()
    for run_cwd in captured_cwds:
        assert run_cwd != ".", "Preparation ran in '.'"
        assert run_cwd != project_dir, "Preparation ran in project workspace"
        assert run_cwd != os.getcwd(), "Preparation ran in current working directory"
        assert run_cwd.lower().startswith(prep_root), f"Preparation cwd {run_cwd} not in exo_state preparation root"
        # Verify directory was cleaned up after completion
        assert not Path(run_cwd).exists(), f"Temporary directory {run_cwd} was not cleaned up"


async def test_teardown_failure_is_not_swallowed(monkeypatch):
    """If provider process teardown fails, ProcessTeardownError must be raised and not swallowed into retrieval."""
    class FakeProc:
        pid = 99999
        def poll(self):
            return None
        def kill(self):
            pass
        def terminate(self):
            pass
        async def wait(self):
            raise TimeoutError("Process hung")

    async def mock_run(*args, **kwargs):
        async def fake_stream():
            yield {"type": "text_delta", "text": "partial"}

        return FakeProc(), fake_stream()

    import server
    monkeypatch.setattr(server.codex_client, "run_codex", mock_run)
    monkeypatch.setattr(server, "_teardown_child_provider", AsyncMock(side_effect=RuntimeError("Teardown crash")))

    summarizer = context_runtime.DestinationSummarizer(
        model="gpt-test",
        harness="codex",
        workspace=".",
        conv_id=42,
    )

    class FakeStore:
        async def find(self, *args):
            return None
        async def save(self, *args):
            pass

    async def fake_measure(text):
        return len(text)

    engine = ContextEngine(store=FakeStore(), summarizer=summarizer, measure=fake_measure)
    snapshot = BranchSnapshot(42, (
        {"id": 1, "role": "user", "content": "task " * 5000},
        {"id": 2, "role": "assistant", "content": "answer " * 5000},
        {"id": 3, "role": "user", "content": "continue"},
    ))

    # ProcessTeardownError must be raised directly and NOT swallowed to yield a retrieval plan
    with pytest.raises(ProcessTeardownError, match="Teardown failed"):
        await engine.prepare(
            snapshot,
            NativeCapability(None, "handoff"),
            input_budget=2000,
            retrieval_notice="Scoped retrieval notice",
        )


async def test_giant_records_are_split_contiguously_with_rolling_state(monkeypatch):
    """Giant records must be split contiguously without gaps/overlaps and advance state sequentially."""
    recorded_calls = []

    async def mock_execute(prompt, destination, isolated_cwd, *, output_cap, timeout_seconds=120.0, status=None):
        idx = len(recorded_calls)
        recorded_calls.append({
            "prompt": prompt,
            "isolated_cwd": isolated_cwd,
            "output_cap": output_cap,
        })
        state = dict(VALID_STATE)
        state["pending_work"] = f"Compacted state after chunk {idx}"
        attestation = {"status": "verified", "effective_model": "gpt-6-astra",
                       "launch_model": "gpt-6-astra"}
        return json.dumps(state), attestation

    import context_preparation
    import replay_budget
    monkeypatch.setattr(context_preparation, "execute_preparation_chunk", mock_execute)

    async def fake_capacity(model, harness, backend=None):
        return 8_000, "test"
    monkeypatch.setattr(replay_budget, "context_capacity", fake_capacity)

    records = [
        {"id": 1, "role": "user", "content": "A" * 15000},
        {"id": 2, "role": "assistant", "content": "B" * 12000},
        {"id": 3, "role": "user", "content": "C" * 200},
    ]

    coordinator = BoundedPreparationCoordinator(
        {"harness": "codex", "model": "gpt-6-astra", "backend": None},
    )

    res = await coordinator.summarize(json.dumps(records), max_tokens=1000)
    final_state = json.loads(res.text)
    assert final_state["pending_work"].startswith("Compacted state after chunk")
    assert len(recorded_calls) >= 2, "Giant records should have produced multiple chunks"

    trace = asyncio.current_task()._context_trace
    assert trace is not None
    assert trace["minimum_accepted_calls"] == len(recorded_calls)

    prep_calls = trace["preparation_calls"]
    assert len(prep_calls) >= 2

    # Check rolling state continuity
    prev_state = None
    for call in prep_calls:
        assert call["prior_state_id"] == prev_state
        assert call["tools_enabled"] is False
        assert call["accepted"] is True
        assert call["result_state_id"] is not None
        assert call["result_state_id"] != prev_state
        prev_state = call["result_state_id"]

    # Check contiguous fragment coverage
    all_fragments = [frag for call in prep_calls for frag in call["fragments"]]
    by_record = {}
    for f in all_fragments:
        by_record.setdefault(f["record_id"], []).append(f)

    for rid, frags in by_record.items():
        expected_len = next(len(r["content"]) for r in records if r["id"] == rid)
        curr_offset = 0
        for f in frags:
            assert f["start"] == curr_offset, f"Gap in record {rid}: expected start {curr_offset}, got {f['start']}"
            assert f["start"] < f["end"] <= expected_len
            curr_offset = f["end"]
        assert curr_offset == expected_len, f"Incomplete coverage for record {rid}"
