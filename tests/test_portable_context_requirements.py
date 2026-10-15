"""Executable requirements, including intentionally RED production gaps.

Run explicitly as documented in PORTABLE_CONTEXT_REPAIR_PLAN.md. No xfail/skip.
No paid inference, live DB, or host lifecycle actions. Existing conftest supplies
an isolated SQLite database. Transport traps never manufacture a summary.
These are regression/binding tests, NOT native or semantic acceptance.
"""
import asyncio
import copy
import itertools
from unittest.mock import AsyncMock

import httpx
import pytest

import context_runtime
import database as db
from context_checkpoints import SQLiteCheckpointStore
from continuity_engine import (
    BranchSnapshot, Checkpoint, ContextEngine, ContextLimit, NativeCapability,
    SummaryResult, fingerprint,
)
from replay_budget import assess_payload, context_capacity


MODELS = {
    "claude": "claude-opus-4-6", "codex": "gpt-6-astra",
    "gemini": "gemini:gemini-3.7-flash-high",
    "goose": "goose:openrouter:z-ai/glm-5.2",
}


@pytest.fixture(autouse=True)
def isolate_inference(monkeypatch):
    # The tests may observe adapter ENTRY, but never launch a native process.
    async def forbidden(*args, **kwargs):
        raise AssertionError("This offline test must not perform network/subprocess inference")
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)


async def measure(text):
    return len(text)  # Deliberate test unit, never described as provider tokens.


async def saved_branch(content="Original requirement: keep ORCHID private."):
    conv = await db.create_conversation("Synthetic portable-context acceptance", mode="codex")
    first = await db.add_message(conv["id"], "user", content)
    old = await db.add_message(conv["id"], "assistant", "Artifact saved once.", parent_id=first["id"])
    current = await db.add_message(conv["id"], "user", "Continue; do not repeat the save.", parent_id=old["id"])
    return conv, await db.get_branch_to_root(current["id"])


@pytest.mark.parametrize("harness", MODELS)
async def test_native_fast_path_never_measures_history(harness):
    _, branch = await saved_branch("historical evidence " * 2000)
    async def forbidden(*args):
        pytest.fail("Compatible native continuation entered replay/compaction")
    engine = ContextEngine(store=SQLiteCheckpointStore(), summarizer=None, measure=forbidden)
    plan = await engine.prepare(BranchSnapshot.from_messages(branch),
        NativeCapability(f"native-{harness}", "resume"), input_budget=0, retrieval_notice="")
    assert plan.kind == "native" and plan.prompt == branch[-1]["content"]


@pytest.mark.parametrize("target", MODELS)
async def test_fitting_cloud_replay_has_no_local_inference_dependency(target):
    conv, branch = await saved_branch()
    prompt = await context_runtime.prepare_branch(branch, model=MODELS[target], harness=target,
        backend="openrouter" if target == "goose" else None)
    plan = asyncio.current_task()._context_plan
    assert plan["kind"] == "replay"
    assert all(record["content"] in prompt for record in branch)
    assert await db.get_branch_to_root(branch[-1]["id"]) == branch


@pytest.mark.parametrize("source,target", itertools.permutations(MODELS, 2))
async def test_oversized_production_binding_attempts_selected_destination(monkeypatch, source, target):
    """RED until production binds destination preparation. Not a chunk/CLI proof.

    The adapter deliberately fails on entry. No synthetic summary is injected.
    Only the selected adapter may be called, then retrieval may be selected.
    """
    import server
    backend = "openrouter" if target == "goose" else None
    capacity, _ = await context_capacity(MODELS[target], target, backend=backend)
    giant = "Historical material. " * (capacity // 6 + 1000)
    conv, branch = await saved_branch(giant)
    await db.update_message_content(branch[1]["id"], cc_session_mode=source,
        cc_model_used=MODELS[source], cc_session_id="synthetic-source", turn_lifecycle_state="completed")
    branch = await db.get_branch_to_root(branch[-1]["id"])
    before = copy.deepcopy(branch)
    calls = []
    def trap(harness):
        async def enter(*args, **kwargs):
            calls.append((harness, kwargs.get("model")))
            raise RuntimeError("Injected destination preparation failure")
        return enter
    for harness in MODELS:
        client = getattr(server, {"claude": "claude_client", "codex": "codex_client",
            "gemini": "gemini_client", "goose": "goose_client"}[harness])
        monkeypatch.setattr(client, "run_" + harness, trap(harness))
    monkeypatch.setattr(server, "_ws_send", AsyncMock())
    prompt = await server._prepare_agent_handoff(branch, conv, MODELS[target], target)
    assert calls, "No selected-destination preparation was attempted before fallback"
    assert all(h == target for h, _ in calls), "Preparation called an unselected harness"
    assert all(m == MODELS[target] for _, m in calls), "Preparation changed the selected model"
    assert asyncio.current_task()._context_plan["kind"] == "retrieval"
    assert branch[-1]["content"] in prompt
    assert await db.get_branch_to_root(branch[-1]["id"]) == before


async def test_automatic_portable_view_reused_without_original_executor():
    """RED: an ordinary saved view must remain usable without its producer."""
    _, branch = await saved_branch("old detail " * 80000)
    snapshot = BranchSnapshot.from_messages(branch)
    prefix = snapshot.messages[:-1]
    cp = Checkpoint(snapshot.conversation_id, fingerprint(prefix), tuple(m["id"] for m in prefix),
        "ORCHID remains private. Artifact save completed once. Continue review.", "previous-selected-provider")
    await SQLiteCheckpointStore().save(cp)  # No manual cursor activation.
    prompt = await context_runtime.prepare_branch(branch, model=MODELS["codex"], harness="codex")
    assert asyncio.current_task()._context_plan["kind"] == "checkpoint", "Persisted automatic view was ignored"
    assert cp.summary in prompt and branch[-1]["content"] in prompt


@pytest.mark.parametrize("failure", ["timeout", "limit", "empty", "wrong_route"])
async def test_preparation_failure_selects_retrieval_not_unsendable_branch(failure):
    """Engine fault injection only; this does not establish a production executor."""
    _, branch = await saved_branch("older evidence " * 1500)
    class FailedPreparation:
        identity = "selected-destination"
        async def summarize(self, text, max_tokens):
            if failure == "timeout":
                raise TimeoutError("Injected timeout")
            if failure == "limit":
                raise ContextLimit("Injected preparation overflow")
            return SummaryResult("" if failure == "empty" else "invalid", "foreign" if failure == "wrong_route" else self.identity)
    engine = ContextEngine(store=SQLiteCheckpointStore(), summarizer=FailedPreparation(), measure=measure)
    plan = await engine.prepare(BranchSnapshot.from_messages(branch), NativeCapability(None, "handoff"),
        input_budget=6000, retrieval_notice="Scoped retrieval is available in this fixture.")
    assert plan.kind == "retrieval"
    assert plan.prompt.endswith(branch[-1]["content"])


async def test_cancellation_is_never_converted_to_retrieval():
    _, branch = await saved_branch("older evidence " * 1500)
    class Cancelled:
        identity = "selected-destination"
        async def summarize(self, *args):
            raise asyncio.CancelledError
    engine = ContextEngine(store=SQLiteCheckpointStore(), summarizer=Cancelled(), measure=measure)
    with pytest.raises(asyncio.CancelledError):
        await engine.prepare(BranchSnapshot.from_messages(branch), NativeCapability(None, "handoff"),
            input_budget=6000, retrieval_notice="Scoped retrieval.")


async def test_current_input_over_capacity_can_be_retrieved_exactly():
    _, branch = await saved_branch()
    await db.update_message_content(branch[-1]["id"], content="User supplied document " * 1000)
    branch = await db.get_branch_to_root(branch[-1]["id"])
    engine = ContextEngine(store=SQLiteCheckpointStore(), summarizer=None, measure=measure)
    plan = await engine.prepare(BranchSnapshot.from_messages(branch), NativeCapability(None, "handoff"),
        input_budget=6000, retrieval_notice="Scoped retrieval.")
    assert plan.kind == "retrieval" and plan.current_input_id == branch[-1]["id"]
    assert len(plan.prompt) <= 6000
    assert (await db.get_message(branch[-1]["id"]))["content"] == branch[-1]["content"]


async def test_80k_history_is_not_an_80k_wire_request():
    """Counterexample to the earlier 'always below 80k' assurance."""
    budget = await assess_payload("x" * 240000, model=MODELS["codex"], harness="codex",
        runtime_context=80000, instructions="Required instructions", image_count=1)
    assert budget.prompt_tokens == 80000
    assert budget.total_tokens > budget.context_tokens


@pytest.mark.parametrize("mutation", ["append", "edit_covered", "sibling"])
async def test_portable_prefix_ancestry_and_verbatim_tail(mutation):
    """Reuse a real stored historical summary; do not fake generating one."""
    conv, branch = await saved_branch("Old requirement. " * 1000)
    original = BranchSnapshot.from_messages(branch)
    covered = original.messages[:-1]
    checkpoint = Checkpoint(conv["id"], fingerprint(covered), tuple(m["id"] for m in covered),
        "ORCHID private; artifact saved exactly once.", "historical-producer")
    await SQLiteCheckpointStore().save(checkpoint)
    if mutation == "append":
        answer = await db.add_message(conv["id"], "assistant", "Recent work retained verbatim.", parent_id=branch[-1]["id"])
        latest = await db.add_message(conv["id"], "user", "New current task retained verbatim.", parent_id=answer["id"])
        branch = await db.get_branch_to_root(latest["id"])
    elif mutation == "edit_covered":
        await db.update_message_content(branch[0]["id"], content="Corrected historical instruction. " * 1000)
        branch = await db.get_branch_to_root(branch[-1]["id"])
    else:
        sibling = await db.add_message(conv["id"], "assistant", "Different outcome.", parent_id=branch[0]["id"])
        latest = await db.add_message(conv["id"], "user", "Continue this sibling.", parent_id=sibling["id"])
        branch = await db.get_branch_to_root(latest["id"])
    engine = ContextEngine(store=SQLiteCheckpointStore(), summarizer=None, measure=measure)
    plan = await engine.prepare(BranchSnapshot.from_messages(branch), NativeCapability(None, "handoff"),
        input_budget=6000, retrieval_notice="Scoped fixture evidence.", preferred=checkpoint)
    if mutation == "append":
        assert plan.kind == "checkpoint"
        assert all(m["content"] in plan.prompt for m in branch[2:])
        assert plan.covered_ids == checkpoint.covered_ids
    else:
        assert plan.checkpoint is None, "Reused a stale or sibling prefix"
        assert checkpoint.summary not in plan.prompt
    assert plan.prompt.endswith(branch[-1]["content"])
