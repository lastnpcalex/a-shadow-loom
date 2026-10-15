"""Harness-neutral contract tests. These do NOT claim native-provider acceptance."""
import copy
import itertools

import pytest

from continuity_engine import (
    BranchSnapshot, ContextEngine, ContextLimit, NativeCapability, SummaryResult, decide,
    preparation_failure_diagnostics,
)
from preparation_errors import (
    PreparationAttestationError,
    PreparationCapacityError,
    PreparationTransportError,
)

HARNESSES = ("claude", "codex", "gemini", "goose")


def branch(source, repeat=1):
    return [
        {"id": 1, "parent_id": None, "conversation_id": 1, "role": "user",
         "content": "Keep the answer key private. " + "historical detail " * repeat},
        {"id": 2, "parent_id": 1, "conversation_id": 1, "role": "assistant",
         "content": "Saved the key; grading is unfinished.", "cc_session_mode": source,
         "cc_session_id": "source-session", "turn_lifecycle_state": "completed",
         "content_blocks": [{"type": "tool_use", "name": "Write", "input": "key.md",
                             "result": "saved"}]},
        {"id": 3, "parent_id": 2, "conversation_id": 1, "role": "user",
         "content": "Continue grading, do not rewrite the answer key."},
    ]


class MemoryStore:
    def __init__(self):
        self.records = []

    async def find(self, conversation_id, fingerprint, route):
        return next((r for r in self.records if r.conversation_id == conversation_id
                     and r.fingerprint == fingerprint and r.summary_route == route), None)

    async def save(self, checkpoint):
        self.records.append(checkpoint)


class SummaryDouble:
    identity = "synthetic-local"

    def __init__(self):
        self.calls = []

    async def summarize(self, text, max_tokens):
        self.calls.append((text, max_tokens))
        return SummaryResult("Keep key private; key.md saved; grading unfinished. "
                             "Verify evidence before repeating writes.", self.identity)


async def size(text):
    return len(text)  # deliberately strict deterministic test tokenizer


@pytest.mark.parametrize("source,target", itertools.permutations(HARNESSES, 2))
async def test_all_directed_handoffs_use_same_engine(source, target):
    records = branch(source, 250)
    original = copy.deepcopy(records)
    summarizer, store = SummaryDouble(), MemoryStore()
    engine = ContextEngine(store=store, summarizer=summarizer, measure=size)
    plan = await engine.prepare(BranchSnapshot.from_messages(records),
                                NativeCapability(None, "provider_boundary"),
                                input_budget=1800, retrieval_notice="Retrieve exact evidence by message ID.")
    assert plan.kind == "checkpoint"
    assert plan.resume_session_id is None
    assert len(plan.prompt) <= 1800
    assert records[-1]["content"] in plan.prompt
    assert "key.md saved" in plan.prompt
    assert plan.covered_ids == (1, 2)
    assert records == original
    assert store.records and summarizer.calls


@pytest.mark.parametrize("harness", HARNESSES)
async def test_native_resume_does_not_measure_or_summarize_history(harness):
    async def forbidden(*args):
        raise AssertionError("native continuation must bypass replay work")
    engine = ContextEngine(store=MemoryStore(), summarizer=None, measure=forbidden)
    snapshot = BranchSnapshot.from_messages(branch(harness, 100_000))
    plan = await engine.prepare(snapshot, NativeCapability("source-session", "resume"),
                                input_budget=0, retrieval_notice="")
    assert plan.kind == "native"
    assert plan.prompt == snapshot.messages[-1]["content"]
    assert plan.resume_session_id == "source-session"


async def test_checkpoint_reuse_and_content_revision_invalidation():
    records = branch("opaque-harness", 250)
    store, summary = MemoryStore(), SummaryDouble()
    engine = ContextEngine(store=store, summarizer=summary, measure=size)
    for _ in range(2):
        await engine.prepare(BranchSnapshot.from_messages(records), NativeCapability(None, "handoff"),
                             input_budget=1800, retrieval_notice="retrieve")
    assert len(summary.calls) == 1
    records[0]["content"] += " A correction."
    await engine.prepare(BranchSnapshot.from_messages(records), NativeCapability(None, "handoff"),
                         input_budget=1800, retrieval_notice="retrieve")
    assert len(summary.calls) == 2


async def test_no_summary_if_current_input_alone_exceeds_budget():
    records = branch("anything", 200)
    records[-1]["content"] = "CURRENT " * 1000
    summary = SummaryDouble()
    engine = ContextEngine(store=MemoryStore(), summarizer=summary, measure=size)
    plan = await engine.prepare(BranchSnapshot.from_messages(records), NativeCapability(None, "handoff"),
                                input_budget=1800, retrieval_notice="retrieve")
    assert plan.kind == "retrieval"
    assert plan.current_input_id == records[-1]["id"]
    assert not summary.calls
    with pytest.raises(ContextLimit, match="current input"):
        await engine.prepare(BranchSnapshot.from_messages(records), NativeCapability(None, "handoff"),
                             input_budget=1800, retrieval_notice="")


async def test_failed_summary_never_persists_checkpoint():
    class Broken(SummaryDouble):
        async def summarize(self, text, max_tokens):
            return SummaryResult("", self.identity)
    store = MemoryStore()
    engine = ContextEngine(store=store, summarizer=Broken(), measure=size)
    plan = await engine.prepare(BranchSnapshot.from_messages(branch("opaque", 250)),
                                NativeCapability(None, "handoff"), input_budget=1800,
                                retrieval_notice="retrieve")
    assert plan.kind == "retrieval"
    assert not store.records


def test_runtime_decide_matches_all_lean_cases():
    import json
    from pathlib import Path
    cases = json.loads((Path(__file__).parents[1] / "formal/portable_context/cases.json").read_text())
    for row in cases:
        assert decide(
            stopped=row["stopped"],
            native=row["native"],
            replay_fits=row["replay_fits"],
            view_fits=row["view_fits"],
            preparation=row["preparation"],
            result_fits=row["result_fits"],
        ) == row["action"]


async def test_recovery_cannot_summarize_away_current_user_request():
    records = branch("opaque", 250)
    task = "Grade only section B; never publish grades or replace the key."
    records[-1]["content"] = task
    records.append({"id": 4, "parent_id": 3, "conversation_id": 1,
                    "role": "assistant", "content": "Recorded result " * 1000})
    engine = ContextEngine(store=MemoryStore(), summarizer=SummaryDouble(), measure=size)
    plan = await engine.prepare(BranchSnapshot.from_messages(records),
                                NativeCapability(None, "context_exhausted"),
                                input_budget=1800, retrieval_notice="retrieve")
    assert plan.kind == "checkpoint"
    assert plan.covered_ids == (1, 2, 3, 4)
    assert plan.prompt.endswith("Current user input (verbatim):\n" + task)
    assert "Do not repeat completed actions" in plan.prompt
    assert len(plan.prompt) <= 1800


@pytest.mark.parametrize("mutation", ["parent", "conversation", "duplicate"])
def test_snapshot_rejects_wrong_topology(mutation):
    records = branch("opaque")
    if mutation == "parent":
        records[-1]["parent_id"] = 99
    elif mutation == "conversation":
        records[-1]["conversation_id"] = 2
    else:
        records[-1]["id"] = records[0]["id"]
    with pytest.raises(ValueError):
        BranchSnapshot.from_messages(records)


async def test_overflow_recovery_is_explicit_and_never_retries_ambiguous_tools():
    engine = ContextEngine(store=MemoryStore(), summarizer=SummaryDouble(), measure=size)
    with pytest.raises(ValueError, match="reconcile"):
        await engine.prepare(BranchSnapshot.from_messages(branch("opaque", 250)),
                             NativeCapability("source-session", "context_exhausted", usable=False,
                                              unresolved_side_effects=True),
                             input_budget=1800, retrieval_notice="retrieve")
    plan = await engine.prepare(BranchSnapshot.from_messages(branch("opaque", 250)),
                                NativeCapability("source-session", "context_exhausted", usable=False),
                                input_budget=1800, retrieval_notice="retrieve")
    assert plan.reason == "context_exhausted"
    assert plan.resume_session_id is None

# --- Section 7: one observed ContinuityState, one authoritative dispatch ---

def test_continuity_state_maps_all_lean_cases():
    """The observed-state wrapper must produce the same action as the kwargs
    reducer for every Lean fixture row (state -> action mapping parity)."""
    import json
    from pathlib import Path
    from continuity_engine import ContinuityState
    cases = json.loads((Path(__file__).parents[1] / "formal/portable_context/cases.json").read_text())
    for row in cases:
        state = ContinuityState(
            stopped=row["stopped"],
            native_available=row["native"],
            replay_fits=bool(row["replay_fits"]),
            checkpoint_fits=bool(row["view_fits"]),
            preparation_status=row["preparation"],
            result_fits=bool(row["result_fits"]),
            retrieval_available=True,
        )
        assert state.action() == row["action"], row


async def test_runtime_facts_produce_the_expected_state_actions():
    """Each observed runtime fact drives the state to the documented action."""
    from continuity_engine import ContinuityState
    # Usable native session -> resume, before any measurement happens.
    native = ContinuityState(native_available=True)
    assert native.action() == "resume"
    # Unmeasured replay -> measurement is demanded by the state itself.
    assert ContinuityState().action() == "measure_replay"
    # Measured non-fitting replay -> stored views must be measured next.
    assert ContinuityState(replay_fits=False).action() == "measure_views"
    # Replay fits -> replay, without ever touching stored views.
    assert ContinuityState(replay_fits=True).action() == "replay"
    # Nothing fits, summarizer available, untried -> prepare.
    assert ContinuityState(replay_fits=False, checkpoint_fits=False,
                           retrieval_available=True).action() == "prepare"
    # Preparation unsupported -> retrieval is the bounded fallback.
    assert ContinuityState(replay_fits=False, checkpoint_fits=False,
                           preparation_status="unsupported",
                           retrieval_available=True).action() == "retrieve"
    # Succeeded preparation with a fitting result -> execute (publish).
    assert ContinuityState(replay_fits=False, checkpoint_fits=False,
                           preparation_status="succeeded", result_fits=True,
                           retrieval_available=True).action() == "execute"
    # Succeeded preparation whose result fits nothing -> re-prepare, never
    # an unmeasured publish.
    assert ContinuityState(replay_fits=False, checkpoint_fits=False,
                           preparation_status="succeeded", result_fits=False,
                           retrieval_available=True).action() == "prepare"
    # Exhausted preparation -> retrieval fallback.
    assert ContinuityState(replay_fits=False, checkpoint_fits=False,
                           preparation_status="failed",
                           retrieval_available=True).action() == "retrieve"
    # Observed cancellation outranks every other fact.
    assert ContinuityState(stopped=True, native_available=True).action() == "stop"


async def test_reducer_is_the_sole_branch_authority():
    """A reducer returning an impossible action fails closed instead of
    falling back to a hand-written branch."""
    from continuity_engine import ContinuityState
    import continuity_engine
    records = branch("opaque", 250)
    engine = ContextEngine(store=MemoryStore(), summarizer=SummaryDouble(), measure=size)

    original = continuity_engine.ContinuityState.action
    def broken_action(self):
        if self.replay_fits is False and self.checkpoint_fits is False:
            return "definitely_not_an_action"
        return original(self)
    continuity_engine.ContinuityState.action = broken_action
    try:
        with pytest.raises(RuntimeError, match="definitely_not_an_action"):
            await engine.prepare(BranchSnapshot.from_messages(records),
                                 NativeCapability(None, "handoff"),
                                 input_budget=1800, retrieval_notice="retrieve")
    finally:
        continuity_engine.ContinuityState.action = original
    # The sane path still works after restoring the reducer.
    plan = await engine.prepare(BranchSnapshot.from_messages(records),
                                NativeCapability(None, "handoff"),
                                input_budget=1800, retrieval_notice="retrieve")
    assert plan.kind == "checkpoint"


async def test_cancellation_observation_stops_preparation():
    from continuity_engine import ContinuityCancelled
    engine = ContextEngine(store=MemoryStore(), summarizer=None, measure=size)
    with pytest.raises(ContinuityCancelled):
        await engine.prepare(BranchSnapshot.from_messages(branch("opaque", 250)),
                             NativeCapability(None, "handoff"),
                             input_budget=1800, retrieval_notice="retrieve",
                             should_stop=lambda: True)


RETRIEVAL_PREPARE = dict(input_budget=1800, retrieval_notice="retrieve")


async def test_preparation_diagnostics_distinguish_failure_types():
    """Recoverable preparation failures fall back to retrieval but keep typed,
    inspectable causes on the running turn task."""
    records = branch("opaque", 250)

    async def prepared_types(summarizer):
        engine = ContextEngine(store=MemoryStore(), summarizer=summarizer, measure=size)
        plan = await engine.prepare(BranchSnapshot.from_messages(records),
                                    NativeCapability(None, "handoff"), **RETRIEVAL_PREPARE)
        return plan, list(preparation_failure_diagnostics())

    class Transport(SummaryDouble):
        async def summarize(self, text, max_tokens):
            raise PreparationTransportError("launch failed: protocol EOF")

    plan, diagnostics = await prepared_types(Transport())
    assert plan.kind == "retrieval" and plan.preparation == "preparation_failed"
    assert [d["type"] for d in diagnostics] == ["PreparationTransportError"]
    assert "protocol EOF" in diagnostics[0]["detail"]

    class Capacity(SummaryDouble):
        async def summarize(self, text, max_tokens):
            raise PreparationCapacityError("destination admission rejected the chunk")

    plan, diagnostics = await prepared_types(Capacity())
    assert plan.kind == "retrieval"
    assert [d["type"] for d in diagnostics] == ["PreparationCapacityError"]

    class Attested(SummaryDouble):
        async def summarize(self, text, max_tokens):
            raise PreparationAttestationError("Model attestation rejected: unverified")

    plan, diagnostics = await prepared_types(Attested())
    assert plan.kind == "retrieval"
    assert [d["type"] for d in diagnostics] == ["PreparationAttestationError"]

    class RouteMismatch(SummaryDouble):
        async def summarize(self, text, max_tokens):
            return SummaryResult("valid looking state", "some-other-route")

    plan, diagnostics = await prepared_types(RouteMismatch())
    assert plan.kind == "retrieval"
    assert [d["type"] for d in diagnostics] == ["PreparationInvalidOutput"]

    class Unfittable(SummaryDouble):
        async def summarize(self, text, max_tokens):
            return SummaryResult("x" * 4000, self.identity)

    plan, diagnostics = await prepared_types(Unfittable())
    assert plan.kind == "retrieval"
    assert diagnostics and diagnostics[-1]["type"] == "PreparationCapacityError"
    assert "exceeds the destination budget" in diagnostics[-1]["detail"]


async def test_preparation_diagnostics_are_fresh_per_prepare_call():
    """Diagnostics are cleared at the start of every ContextEngine.prepare()."""
    records = branch("opaque", 250)

    class Transport(SummaryDouble):
        async def summarize(self, text, max_tokens):
            raise PreparationTransportError("first-attempt transport failure")

    engine = ContextEngine(store=MemoryStore(), summarizer=Transport(), measure=size)
    await engine.prepare(BranchSnapshot.from_messages(records),
                         NativeCapability(None, "handoff"), **RETRIEVAL_PREPARE)
    assert preparation_failure_diagnostics(), "failures must be inspectable during the turn"

    await engine.prepare(BranchSnapshot.from_messages(records),
                         NativeCapability(None, "handoff"), **RETRIEVAL_PREPARE)
    # Still exactly one attempt's worth: the earlier failures were cleared.
    assert [d["type"] for d in preparation_failure_diagnostics()] == [
        "PreparationTransportError"]


async def test_cleanup_and_teardown_failures_are_visible_not_retrieval():
    """Cleanup/teardown failures fail the turn; they are recorded, then raised."""
    records = branch("opaque", 250)

    class Teardown(SummaryDouble):
        async def summarize(self, text, max_tokens):
            raise continuity_engine.ProcessTeardownError("child process did not exit")

    import continuity_engine
    engine = ContextEngine(store=MemoryStore(), summarizer=Teardown(), measure=size)
    with pytest.raises(continuity_engine.ProcessTeardownError):
        await engine.prepare(BranchSnapshot.from_messages(records),
                             NativeCapability(None, "handoff"), **RETRIEVAL_PREPARE)
    assert [d["type"] for d in preparation_failure_diagnostics()] == [
        "ProcessTeardownError"]
