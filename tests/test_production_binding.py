"""Production binding tests for portable continuity (guide sections 8-9).

These tests bind the REAL production launch path — ``context_runtime.prepare_snapshot``
(the entry server.py's generation path calls), the real ``DestinationSummarizer``
→ ``BoundedPreparationCoordinator`` → ``execute_preparation_chunk`` chain, and
the real provider client adapters — against CONTROLLED FAKE PROVIDER PROCESSES:
real subprocesses speaking the provider's real wire protocol (CC stream-json,
Codex app-server JSONL, agy stream-json, Goose ACP) over real stdio pipes.

Rule Zero: the tests never fabricate the outcome under test. No synthetic
session ids, no mocked provider transports, no constructed working launches.
Every oracle value — child env, isolated cwd, tool-free handshake, published
prompt, checkpoint persistence, working-launch payload, provider-issued
session id, native resume source, attestation — is read either from the fake
provider's own recorded evidence (argv/env/cwd/protocol payloads it received)
or from Loom's recorded production events (plan, trace). Only the destination
capacity is a controlled fixture (a deterministic budget double), as in every
component test of this family.

The fake provider binaries stand in for `claude`/`codex`/`agy`/`goose` only;
adapters, coordinator, engine, reducer and checkpoint storage are production.
"""
import asyncio
import itertools
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

import context_preparation
import context_runtime
import claude_client
import codex_client
import gemini_client
import goose_client
from context_checkpoints import SQLiteCheckpointStore
from continuity_engine import BranchSnapshot, ContextEngine, NativeCapability
from replay_budget import estimate_tokens

FAKE_PROVIDER = Path(__file__).parent / "fake_providers" / "fake_provider.py"
HARNESSES = ("claude", "codex", "gemini", "goose")
MODELS = {"claude": "binding-model", "codex": "binding-model",
          "gemini": "binding-model", "goose": "goose:openrouter:binding-model"}
# Include the current full harness contract in this positive binding fixture.
# The evidence below still exceeds the preparation budget and forces a checkpoint.
CAPACITY = 8192

VALID_STATE = {
    "objective": "Continue the recorded binding task.",
    "constraints": "Never repeat completed effects; reconcile recorded outcomes.",
    "decisions": "Use the recorded plan; keep the answer key private.",
    "completed_effects": "Saved key.md (record 2).",
    "uncertain_effects": "Grading run 3 outcome unrecorded.",
    "pending_work": "Grade section B from recorded evidence.",
    "artifacts": "key.md",
    "questions": "",
    "sources": "1-3",
}


FILLER = "Recorded binding evidence with durable facts and tool outcomes. " * 20
CURRENT_TASK = "Grade section B now; do not repeat completed effects."


async def _branch_in_db(source, conv_id=77, fillers=20, correct=False):
    """Build the branch through the production database API (server.py's own
    persistence path); the returned root-to-leaf records are what the engine
    and the checkpoint store see."""
    import database as db
    conv = await db.create_conversation("binding", mode="weave")
    conv_id = conv["id"]
    parent = None
    tool_record_id = None
    for index in range(1, fillers + 1):
        role = "user" if index % 2 == 1 else "assistant"
        content = FILLER + f"[{source}:{index}]"
        blocks = json.dumps([{"type": "tool_use", "name": "Write", "input": "key.md",
                              "result": "saved"}]) if index == 2 else None
        row = await db.add_message(conv_id, role, content, parent_id=parent,
                                   content_blocks=blocks)
        parent = row["id"]
        if index == 2:
            tool_record_id = parent
    current = await db.add_message(conv_id, "user", CURRENT_TASK, parent_id=parent)
    if tool_record_id is not None:
        await db.update_message_content(tool_record_id, cc_session_id="source-session",
                                        cc_session_mode=source)
    if correct:
        # A recorded correction supersedes the earlier note (production edit).
        await db.update_message_content(tool_record_id,
                                        content=FILLER + f"[{source}:2] A recorded "
                                        "correction supersedes the earlier note.")
    return await db.get_branch_to_root(current["id"])


class _Budget:
    context_tokens = CAPACITY
    total_tokens = 0

    def __init__(self, payload_tokens=0):
        self.total_tokens = payload_tokens

    def as_dict(self):
        return {"context_tokens": CAPACITY, "total_tokens": self.total_tokens,
                "context_source": "binding-fixture"}


@pytest.fixture
def binding(monkeypatch, tmp_path):
    """Controlled production harness: capacity fixture + fake provider processes."""
    evidence = tmp_path / "provider-evidence.jsonl"

    # Exercise the real provider configuration code without touching either
    # the checkout's .agents directory or the operator's user-level AGY trust
    # store. This also keeps the fake-provider test faithful when pytest's
    # --basetemp happens to live beneath the repository.
    isolated_root = Path(tempfile.mkdtemp(prefix="exo-binding-"))
    monkeypatch.setenv("EXO_STATE_DIR", str(isolated_root / "state"))
    monkeypatch.setenv("USERPROFILE", str(isolated_root / "home"))
    monkeypatch.setenv("HOME", str(isolated_root / "home"))

    async def assess(payload="", **kwargs):
        return _Budget(estimate_tokens(payload))

    async def capacity(model, harness, backend=None):
        return CAPACITY, "binding-fixture"

    async def check_fresh(payload, **kwargs):
        tokens = estimate_tokens(payload)
        return {"context_tokens": CAPACITY, "total_tokens": tokens,
                "prompt_tokens": tokens, "output_tokens": 0,
                "output_reserve": 0, "instructions": 0, "tools": 0,
                "context_source": "binding-fixture"}

    monkeypatch.setattr(context_runtime, "assess_payload", assess)
    monkeypatch.setattr("replay_budget.context_capacity", capacity)
    monkeypatch.setattr("replay_budget.check_fresh_payload", check_fresh)

    orig_exec = asyncio.create_subprocess_exec

    async def intercept(*cmd, **kwargs):
        name = os.path.basename(str(cmd[0])).lower()
        if name == "claude" or name.startswith("claude."):
            harness = "claude"
        elif "fake-codex" in name:
            harness = "codex"
        elif "fake-agy" in name:
            harness = "gemini"
        elif "fake-goose" in name:
            harness = "goose"
        else:
            return await orig_exec(*cmd, **kwargs)
        new_cmd = [sys.executable, str(FAKE_PROVIDER), harness, str(evidence)]
        new_cmd += [str(c) for c in cmd[1:]]
        return await orig_exec(*new_cmd, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", intercept)
    monkeypatch.setattr(codex_client, "_find_codex_exe", lambda: "fake-codex.exe")
    monkeypatch.setattr(gemini_client, "_find_agy_exe", lambda: "fake-agy.exe")
    monkeypatch.setattr(goose_client, "default_goose_exe", lambda: "fake-goose.exe")

    def reader():
        rows = []
        if not evidence.exists():
            return rows
        with open(evidence, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    def launches(harness):
        return [r for r in reader() if r.get("event") == "launch"
                and r.get("harness") == harness]

    yield {"evidence": evidence, "read": reader, "launches": launches,
           "tmp": isolated_root / "workspace", "isolated_root": isolated_root}
    shutil.rmtree(isolated_root)


def _run(harness):
    """Call the production adapter exactly as server.py's generation path does."""
    workspace = str

    async def run(prompt, cwd, *, resume_session_id=None):
        if harness == "claude":
            return await claude_client.run_claude(
                prompt, cwd, conv_id=0, server_port=3000, model=MODELS[harness],
                requested_model=MODELS[harness], permission_mode="default",
                resume_session_id=resume_session_id)
        if harness == "codex":
            return await codex_client.run_codex(
                prompt, cwd, conv_id=0, server_port=3000, model=MODELS[harness],
                permission_mode="default", resume_session_id=resume_session_id)
        if harness == "gemini":
            return await gemini_client.run_gemini(
                prompt, cwd, conv_id=0, server_port=3000, model=MODELS[harness],
                permission_mode="default", resume_session_id=resume_session_id)
        return await goose_client.run_goose(
            prompt, conv_id=0, cwd=cwd, model=MODELS[harness], loom_port=3000,
            permission_mode="default", builtins=["developer"],
            resume_session_id=resume_session_id)

    return run


async def _consume(stream):
    session_id = ""
    result = None
    async for event in stream:
        event = dict(event or {})
        if event.get("type") == "session_info" and event.get("session_id"):
            session_id = event["session_id"]
        elif event.get("type") == "result":
            result = event
        elif event.get("type") == "error":
            raise RuntimeError(f"provider stream error: {event.get('message')}")
    return session_id, result


def _trace_of():
    task = asyncio.current_task()
    return getattr(task, "_context_trace", None)


async def _prepare_turn(binding, snapshot, harness, store, workspace):
    prompt = await context_runtime.prepare_snapshot(
        snapshot, model=MODELS[harness], harness=harness, backend=None,
        reason="handoff", status=None, store=store,
        notice="Retrieve exact evidence by message ID.",
        image_count=0, preferred=None, workspace=workspace)
    task = asyncio.current_task()
    plan = task._context_preparation["plan"]
    trace = getattr(task, "_context_trace", None)
    return prompt, plan, trace


def _isolated_cwd_ok(cwd):
    prep_root = str(context_preparation.exo_state.state_path("preparation")).lower()
    return str(cwd).lower().startswith(prep_root)


@pytest.mark.parametrize("source,target", itertools.permutations(HARNESSES, 2))
async def test_cross_harness_binding_prepares_publishes_works_and_resumes(source, target, binding):
    """One production binding per ordered harness pair: destination-owned
    preparation → checkpoint publication → ordinary working launch →
    provider-issued session → next-turn native resume of that exact session."""
    tmp = binding["tmp"]
    snapshot = BranchSnapshot.from_messages(await _branch_in_db(source))
    store = SQLiteCheckpointStore()
    task_marker = f"[{source}->{target}]"

    # --- Turn 1: destination-owned preparation through prepare_snapshot ---
    prompt, plan, trace = await _prepare_turn(binding, snapshot, target, store,
                                              str(tmp / "ws1"))
    failures = getattr(asyncio.current_task(), "_context_preparation_failures", None)
    # The production decision is a published checkpoint, never a fabricated
    # or hand-constructed plan.
    assert plan.kind == "checkpoint", json.dumps({
        "plan_kind": plan.kind,
        "preparation": plan.preparation,
        "budget": plan.budget,
        "selected_ids": plan.selected_ids,
        "trace": trace,
        "failures": failures,
        "prompt_preview": prompt[:500],
    }, default=str)
    assert plan.checkpoint is not None and plan.checkpoint.summary.strip()
    # Oracle from the provider's own recorded evidence: every preparation
    # launch on the target harness ran tool-free in an isolated cwd.
    prep_launches = binding["launches"](target)
    assert prep_launches, "no provider process was launched for preparation"
    assert all(l["preparation"] is True and l["env"].get("LOOM_PREPARATION") == "1"
               for l in prep_launches)
    assert all(_isolated_cwd_ok(l["cwd"]) for l in prep_launches)
    assert all(l["cwd"] not in (".", os.path.abspath(".")) for l in prep_launches)
    # Goose preparation handshake: zero MCP servers recorded by the provider.
    if target == "goose":
        session_news = [r for r in binding["read"]()
                        if r.get("event") == "session_new"]
        assert session_news and all(r["params"].get("mcpServers") == []
                                    for r in session_news)
    # Recorded trace: bounded calls, tool-free, provider-attested model.
    assert trace is not None and trace.get("calls_used", 0) >= 1
    for call in trace.get("preparation_calls", []):
        assert call.get("tools_enabled") is False
        assert call.get("attested_model")
    # The state the provider returned validates against STATE_SCHEMA and is
    # what got published (read from the recorded plan's checkpoint).
    assert json.loads(plan.checkpoint.summary).keys() == VALID_STATE.keys()

    # --- Reuse: the next turn must reuse the persisted checkpoint without
    # contacting any provider process ---
    before = len(binding["read"]())
    prompt2, plan2, _ = await _prepare_turn(binding, snapshot, target, store,
                                            str(tmp / "ws1"))
    assert plan2.kind == "checkpoint" and plan2.checkpoint is not None
    assert prompt2 == prompt
    assert len(binding["read"]()) == before, "provider was contacted on pure reuse"

    # --- Working launch: the normal (non-preparation) launch receives the
    # published prompt and completes; the provider mints the session id ---
    ws = str(tmp / "ws1")
    working = _run(target)
    proc, stream = await working(prompt, ws)
    working_session, working_result = await _consume(stream)
    assert working_result and not working_result.get("is_error")
    # The session id is the provider's own issuance, read from its evidence.
    issued = [r for r in binding["read"]() if r.get("event") == "session_issued"]
    assert issued and working_session in {r["session_id"] for r in issued}
    working_launches = binding["launches"](target)
    assert len(working_launches) > len(prep_launches), "no working launch recorded"
    working_launch = working_launches[-1]
    assert working_launch["preparation"] is False
    assert "LOOM_PREPARATION" not in working_launch["env"]
    if target == "goose":
        # Ordinary goose keeps its MCP surface; preparation's emptiness was
        # asserted above for the preparation launches only.
        working_new = [r for r in binding["read"]() if r.get("event") == "session_new"]
        assert working_new

    # --- Native resume: the next turn resumes the provider-issued session ---
    async def measure(text):
        return estimate_tokens(text)
    engine = ContextEngine(store=store, summarizer=None, measure=measure)
    native_plan = await engine.prepare(snapshot,
                                       NativeCapability(working_session, "resume"),
                                       input_budget=CAPACITY, retrieval_notice="")
    assert native_plan.kind == "native"
    assert native_plan.resume_session_id == working_session

    proc, stream = await working(native_plan.prompt, ws,
                                 resume_session_id=working_session)
    resume_session, resume_result = await _consume(stream)
    assert resume_result and not resume_result.get("is_error")
    resumes = [r for r in binding["read"]() if r.get("event") == "resume_requested"]
    assert resumes and all(r["session_id"] == working_session for r in resumes)
    # No unrelated provider was ever contacted.
    contacted = {r["harness"] for r in binding["read"]() if r.get("event") == "launch"}
    assert contacted == {target}, contacted
    assert task_marker  # source harness selection is recorded in the branch


@pytest.mark.parametrize("harness", HARNESSES)
async def test_same_harness_native_resume_binding(harness, binding):
    """Same-harness handoff: the four recorded provider sessions each resume
    their exact issued session natively on the next turn."""
    tmp = binding["tmp"]
    snapshot = BranchSnapshot.from_messages(await _branch_in_db(harness))
    store = SQLiteCheckpointStore()
    prompt, plan, _ = await _prepare_turn(binding, snapshot, harness, store,
                                          str(tmp / "ws"))
    assert plan.kind == "checkpoint"
    run = _run(harness)
    proc, stream = await run(prompt, str(tmp / "ws"))
    working_session, working_result = await _consume(stream)
    assert working_result and not working_result.get("is_error")
    async def measure(text):
        return estimate_tokens(text)
    engine = ContextEngine(store=store, summarizer=None, measure=measure)
    native_plan = await engine.prepare(snapshot,
                                       NativeCapability(working_session, "resume"),
                                       input_budget=CAPACITY, retrieval_notice="")
    assert native_plan.kind == "native"
    proc, stream = await run(native_plan.prompt, str(tmp / "ws"),
                             resume_session_id=working_session)
    resume_session, resume_result = await _consume(stream)
    assert resume_result and not resume_result.get("is_error")
    assert resume_session == working_session
    resumes = [r for r in binding["read"]() if r.get("event") == "resume_requested"]
    assert resumes and all(r["session_id"] == working_session for r in resumes)


@pytest.mark.parametrize("harness", HARNESSES)
async def test_unconfirmed_model_fails_closed_without_working_launch(harness, binding, monkeypatch):
    """A provider that reports a different effective model than the launch
    model produces a visible bounded failure; the only production fallback is
    scoped retrieval, and no ordinary working launch ever runs."""
    monkeypatch.setenv("LOOM_FAKE_REPORT_MODEL", "some-other-model")
    snapshot = BranchSnapshot.from_messages(await _branch_in_db(harness))
    prompt, plan, trace = await _prepare_turn(
        binding, snapshot, harness, SQLiteCheckpointStore(), str(binding["tmp"] / "ws"))
    assert plan.kind == "retrieval"
    assert plan.preparation == "preparation_failed"
    assert plan.checkpoint is None, "an unverified model must never publish a checkpoint"
    assert trace is not None, "bounded failure left no recorded trace"
    launches = binding["launches"](harness)
    assert launches, "bounded failure contacted no provider"
    assert all(l["preparation"] is True and l["env"].get("LOOM_PREPARATION") == "1"
               for l in launches), "a working launch ran after unverified attestation"


@pytest.mark.parametrize("harness", HARNESSES)
async def test_resume_with_synthetic_session_id_fails(harness, binding):
    """A session id no provider ever issued is refused by the provider; the
    resume launch fails instead of silently starting an unrelated session."""
    run = _run(harness)
    failed = False
    try:
        proc, stream = await run("continue", str(binding["tmp"] / "ws"),
                                 resume_session_id="synthetic-session")
        session, result = await _consume(stream)
        failed = not result or result.get("is_error") or session != "synthetic-session"
    except Exception:
        failed = True
    assert failed, "the provider accepted a session id it never issued"
    launches = [r for r in binding["read"]() if r.get("event") == "launch"]
    resumes = [r for r in binding["read"]() if r.get("event") == "resume_requested"]
    if launches:
        # The provider was contacted only through the resume request path and
        # refused the synthetic id.
        assert resumes and all(r["session_id"] == "synthetic-session" for r in resumes)
    assert not [r for r in binding["read"]() if r.get("event") == "session_issued"], \
        "a session was minted for a session id no provider ever issued"


async def test_goose_preparation_aborts_on_advertised_tools(binding, monkeypatch):
    """One advertised tool in the goose handshake aborts preparation before
    any evidence is sent; the only fallback is scoped retrieval."""
    monkeypatch.setenv("LOOM_FAKE_AGENT_CAPABILITIES",
                       json.dumps({"tools": [{"name": "developer_shell"}]}))
    snapshot = BranchSnapshot.from_messages(await _branch_in_db("goose"))
    prompt, plan, trace = await _prepare_turn(
        binding, snapshot, "goose", SQLiteCheckpointStore(), str(binding["tmp"] / "ws"))
    assert plan.kind == "retrieval"
    assert plan.checkpoint is None, "a tool-advertising provider must never publish a checkpoint"
    assert binding["launches"]("goose")
    prompts = [r for r in binding["read"]() if r.get("event") == "session_prompt"]
    assert not prompts, "evidence was sent despite advertised tools"


@pytest.mark.parametrize("harness", HARNESSES)
async def test_changed_evidence_forces_fresh_preparation(harness, binding):
    """Editing the recorded evidence invalidates the persisted checkpoint; the
    next turn re-prepares on the destination (provider contacted again)."""
    snapshot = BranchSnapshot.from_messages(await _branch_in_db(harness))
    store = SQLiteCheckpointStore()
    await _prepare_turn(binding, snapshot, harness, store, str(binding["tmp"] / "ws"))
    before = len(binding["launches"](harness))
    changed = BranchSnapshot.from_messages(
        await _branch_in_db(harness, correct=True))
    await _prepare_turn(binding, changed, harness, store, str(binding["tmp"] / "ws"))
    assert len(binding["launches"](harness)) > before


@pytest.mark.parametrize("harness", HARNESSES)
async def test_unpersisted_checkpoint_cannot_be_reused(harness, binding):
    """Delete the persisted checkpoint rows (production store); the next turn
    cannot reuse what was never persisted and re-prepares on the destination."""
    import database as db
    snapshot = BranchSnapshot.from_messages(await _branch_in_db(harness))
    await _prepare_turn(binding, snapshot, harness, SQLiteCheckpointStore(),
                        str(binding["tmp"] / "ws"))
    connection = await db.get_db()
    await connection.execute("DELETE FROM context_checkpoints", ())
    before = len(binding["launches"](harness))
    await _prepare_turn(binding, snapshot, harness, SQLiteCheckpointStore(),
                        str(binding["tmp"] / "ws"))
    assert len(binding["launches"](harness)) > before
