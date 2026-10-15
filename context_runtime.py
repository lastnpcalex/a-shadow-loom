"""Loom binding for the harness-neutral continuity engine.

One runtime binding for native capability evidence, fresh plans, final rendered
admission, manual boundaries and child evidence. Preparation uses only the
selected destination; unavailable preparation selects scoped retrieval.
"""
import asyncio
from dataclasses import asdict, replace

from config import config
from continuity_engine import BranchSnapshot, Checkpoint, ContextEngine, ContextLimit, NativeCapability, canonical, fingerprint
from context_checkpoints import SQLiteCheckpointStore, issue_retrieval_scope
from loom_agent_prompt import load_loom_agent_prompt
from replay_budget import assess_payload, estimate_tokens


class DisabledLocalSummarizer:
    """The local summarizer that Astra added has been removed.

    No automatic local GPU summarization is performed. Cloud-to-cloud handoffs
    use destination-owned preparation with bounded evidence retrieval fallback.
    """
    identity = "disabled"

    def __init__(self, status=None):
        self.status = status

    async def summarize(self, text, max_tokens):
        from local_context_guard import LocalContextGuard
        guard = LocalContextGuard(config.llama_host_url())
        try:
            raise ContextLimit(
                "The automatic local summarizer has been removed; no cloud fallback was used. "
                "Recorded messages and tool outcomes are preserved. "
                "Select a destination with sufficient capacity to admit the branch without summarization."
            )
        finally:
            await guard.close()


ConfiguredLocalSummarizer = DisabledLocalSummarizer


from context_preparation import BoundedPreparationCoordinator, PreparationBudget


class DestinationSummarizer:
    """Destination-owned preparation adapter.

    Coordinates bounded chunk preparation on the selected destination
    model/harness with tools disabled, isolated working directory,
    and verified process teardown.
    """
    def __init__(self, *, model: str, harness: str, backend: str | None = None,
                 workspace: str = ".", conv_id: int = 0, status=None):
        self.model = model
        self.harness = harness
        self.backend = backend
        self.workspace = workspace
        self.conv_id = conv_id
        self.status = status
        self.coordinator = BoundedPreparationCoordinator(
            {"harness": harness, "model": model, "backend": backend},
            status=status,
            conversation_id=conv_id,
            budget=PreparationBudget(
                maximum_calls=config.context_preparation_max_calls,
                maximum_elapsed_seconds=config.context_preparation_timeout_seconds,
            ),
        )
        self.identity = self.coordinator.identity

    async def summarize(self, text: str, max_tokens: int):
        return await self.coordinator.summarize(text, max_tokens)


async def prepare_branch(branch, *, model, harness, backend=None, reason="handoff", status=None, frozen_tail=False,
                         workspace="."):
    if not branch:
        return "(continue)"
    snapshot = BranchSnapshot.from_messages(branch)
    # Latest attachments only. Historical attachment references remain in the
    # records and can be retrieved without loading all image embeddings again.
    import json
    latest_user = next((m for m in reversed(branch) if m["role"] == "user"), {})
    paths = latest_user.get("image_path") or []
    if isinstance(paths, str):
        try:
            paths = json.loads(paths)
        except ValueError:
            paths = [paths]
    paths = paths if isinstance(paths, list) else [paths]
    store = SQLiteCheckpointStore()
    preferred = await store.manual_for(snapshot) or await store.valid_for(snapshot)
    return await prepare_snapshot(snapshot, model=model, harness=harness, backend=backend,
                                  reason=reason, status=status, image_count=len(paths), store=store,
                                  notice=await retrieval_notice(snapshot, frozen_tail=frozen_tail), preferred=preferred,
                                  workspace=workspace)


async def prepare_snapshot(snapshot, *, model, harness, backend, reason, status,
                           store, notice, image_count=0, preferred=None, summarizer=None, workspace="."):
    """One budget/summary engine for branch and independently owned child evidence."""
    budget = await assess_payload("", model=model, harness=harness, backend=backend,
                                  instructions=load_loom_agent_prompt(), image_count=image_count)
    # Canvas/continuity/permission notices and adapter-specific instructions are
    # added after this layer. Their reserve is explicit and the adapter's final
    # rendered admission remains mandatory (including native hidden overhead).
    input_budget = budget.context_tokens - budget.total_tokens - 4096

    counter = None
    if backend == "llama":
        from local_context_guard import LocalContextGuard
        counter = LocalContextGuard(config.llama_host_url())
    try:
        async def measure(prompt):
            if counter is not None:
                # The running tokenizer is authoritative for local destinations.
                # Native instructions/tools/output remain separately reserved;
                # the guard still checks the final harness-rendered request.
                return await counter.count({"model": config.llama_model,
                                            "messages": [{"role": "user", "content": prompt}]})
            return estimate_tokens(prompt)

        active_summarizer = summarizer
        if active_summarizer is None:
            if ConfiguredLocalSummarizer is not DisabledLocalSummarizer and ConfiguredLocalSummarizer is not None:
                active_summarizer = ConfiguredLocalSummarizer(status) if callable(ConfiguredLocalSummarizer) else ConfiguredLocalSummarizer
            elif harness in {"claude", "codex", "gemini", "goose", "local", "openrouter", "umans", "dream-claude", "openrouter-claude"}:
                active_summarizer = DestinationSummarizer(model=model, harness=harness, backend=backend,
                                                          workspace=workspace, conv_id=snapshot.conversation_id, status=status)

        engine = ContextEngine(store=store, summarizer=active_summarizer,
                               measure=measure)
        plan = await engine.prepare(snapshot, NativeCapability(None, reason),
                                    input_budget=input_budget, retrieval_notice=notice, preferred=preferred)
    finally:
        if counter is not None:
            await counter.close()
    task = asyncio.current_task()
    if task:
        plan = replace(plan, source_revision=fingerprint(snapshot.messages),
                       retrievable_ids=tuple(m["id"] for m in snapshot.messages),
                       identity=plan_identity(snapshot, model, harness, backend, workspace), budget=budget.as_dict())
        record_plan(plan, input_budget)
        task._context_preparation = dict(snapshot=snapshot, store=store, notice=notice, preferred=preferred,
                                         plan=plan, attempts=0, model=model, harness=harness, backend=backend)
    return plan.prompt


def plan_identity(snapshot, model, harness, backend, workspace):
    task = asyncio.current_task()
    key = getattr(task, "_gen_key", None)
    return {"workspace": str(workspace), "conversation_id": snapshot.conversation_id,
            "source_leaf_id": snapshot.messages[-1]["id"],
            "current_user_id": next((m["id"] for m in reversed(snapshot.messages) if m["role"] == "user"), None),
            "run_id": getattr(task, "_agent_run_id", None) or (f"gen:{key[2]}" if key else None),
            "origin_page": getattr(task, "_client_instance_id", None),
            **getattr(task, "_context_identity", {}), "model": model, "harness": harness, "backend": backend}


def record_plan(plan, input_budget=None):
    task = asyncio.current_task()
    if task is None:
        return
    task._context_replay_region = plan.replay_region
    # Prompts and bearer credentials are execution-only, never telemetry.
    task._context_plan = {"version": 2, "kind": plan.kind, "reason": plan.reason,
                          "covered_message_ids": list(plan.covered_ids),
                          "selected_ids": list(plan.selected_ids), "retrievable_ids": list(plan.retrievable_ids),
                          "source_revision": plan.source_revision, "identity": plan.identity,
                          "budget": plan.budget, "context_source": plan.budget.get("context_source"),
                          "input_budget": input_budget, "preparation": plan.preparation,
                          "summary_route": plan.checkpoint.summary_route if plan.checkpoint else None,
                          "lifecycle": plan.lifecycle}


async def admit_rendered(prompt, *, model, harness, resume_session_id=None, **kwargs):
    """Adapter renders first; a changed wire constraint returns to this planner.

    Preserve all adapter notices/instructions verbatim. Replace only the exact
    planned evidence region, and admit the replacement before any model request.
    """
    from replay_budget import check_fresh_payload
    task = asyncio.current_task()
    state = getattr(task, "_context_preparation", None)
    if resume_session_id or getattr(task, "_context_preparing", False) or not state:
        await check_fresh_payload(prompt, model=model, harness=harness, resume_session_id=resume_session_id, **kwargs)
        return prompt
    budget = await assess_payload(prompt, model=model, harness=harness, **kwargs)
    old = state["plan"]
    if budget.total_tokens > budget.context_tokens:
        if state["attempts"] >= 2 or not old.prompt or prompt.count(old.prompt) != 1:
            raise ContextLimit("Final destination payload cannot be reduced safely; evidence is preserved")
        state["attempts"] += 1
        shell = prompt.replace(old.prompt, "", 1)
        overhead = await assess_payload(shell, model=model, harness=harness, **kwargs)
        available = overhead.context_tokens - overhead.total_tokens - 128
        async def measure(text):
            return estimate_tokens(text)
        engine = ContextEngine(store=state["store"], summarizer=None, measure=measure)
        new = await engine.prepare(state["snapshot"], NativeCapability(None, "final_payload_revision"),
                                   input_budget=available, retrieval_notice=state["notice"],
                                   preferred=old.checkpoint or state["preferred"])
        prompt = prompt.replace(old.prompt, new.prompt, 1)
        if new.prompt == old.prompt:
            raise ContextLimit("Final admission made no progress; identical retries are forbidden")
        old = replace(new, identity=old.identity, source_revision=old.source_revision,
                      retrievable_ids=old.retrievable_ids)
    admitted = await check_fresh_payload(prompt, model=model, harness=harness, **kwargs)
    plan = replace(old, budget=admitted, lifecycle="admitted")
    state["plan"] = plan
    record_plan(plan, admitted["context_tokens"] - admitted["total_tokens"] + admitted["prompt_tokens"])
    task._context_plan["admission_attempts"] = state["attempts"] + 1
    return prompt


async def retrieval_notice(snapshot, *, frozen_tail=False):
    token = await issue_retrieval_scope(snapshot, owner=asyncio.current_task(), frozen_tail=frozen_tail)
    return ("Original branch evidence is available through loom-agent-control.retrieve_context. "
            "Pass context_scope as token; omit message_id for the index, or supply a message ID "
            "and offset for exact records. Use query to search scoped text/tool evidence. "
            "Use this new scope instead of earlier scopes. "
            "It remains valid while this generation is active, including long turns; "
            "completion, cancellation, restart or editing its source revokes access. "
            "Do not display or log its token.\n"
            f"context_scope: {token}")


async def renew_native_retrieval(prompt, branch, *, decision=None, model=None, harness=None, workspace="."):
    """Renew exact branch access, without budgeting/replaying/summarizing it."""
    if not branch:
        return prompt
    snapshot = BranchSnapshot.from_messages(branch)
    notice = await retrieval_notice(snapshot)
    if decision is not None:
        engine = ContextEngine(store=SQLiteCheckpointStore(), summarizer=None, measure=None)
        plan = await engine.prepare(snapshot, NativeCapability(decision.session_id, decision.reason),
                                    input_budget=0, retrieval_notice="")
        plan = replace(plan, identity=plan_identity(snapshot, model, harness, None, workspace),
                       preparation="native_session", lifecycle="native", source_revision=fingerprint(snapshot.messages))
        record_plan(plan)
        asyncio.current_task()._context_preparation = None
    return notice + "\n\n" + prompt


async def manual_checkpoint(branch, cursor, *, status=None, focus="", summarizer=None,
                            model=None, harness=None, backend=None, workspace="."):
    """Explicit summary, then atomic branch/cursor-bound activation; no CLI."""
    snapshot = BranchSnapshot.from_messages(branch)
    active_summarizer = summarizer
    if active_summarizer is None and ConfiguredLocalSummarizer is not DisabledLocalSummarizer and ConfiguredLocalSummarizer is not None:
        active_summarizer = ConfiguredLocalSummarizer(status) if callable(ConfiguredLocalSummarizer) else ConfiguredLocalSummarizer
    evidence = canonical(list(snapshot.messages))
    if focus:
        evidence = canonical({"user_summary_focus": str(focus)[:4000], "records": list(snapshot.messages)})
    if active_summarizer is not None:
        budget = await assess_payload("", model=model, harness=harness, backend=backend)
        output = max(128, (budget.context_tokens - budget.total_tokens) // 8)
        result = await active_summarizer.summarize(evidence, output)
        if not result.text or not result.text.strip() or result.route != active_summarizer.identity:
            raise ContextLimit("Summary failed validation; original history is unchanged")
    else:
        from continuity_engine import SummaryResult
        result = SummaryResult(canonical({"kind": "retrieval_boundary", "task_summary": None,
            "record_count": len(snapshot.messages), "first_record": snapshot.messages[0]["id"],
            "last_record": snapshot.messages[-1]["id"],
            "required_workflow": "Reconstruct the active task from scoped exact evidence before consequential actions."}),
            "retrieval-boundary:v1")
    checkpoint = Checkpoint(snapshot.conversation_id, fingerprint(snapshot.messages),
                            tuple(m["id"] for m in snapshot.messages), result.text.strip(), result.route)
    store = SQLiteCheckpointStore()
    await store.save(checkpoint)
    await store.activate_manual(checkpoint, cursor)
    return checkpoint


async def respect_manual_boundary(branch, decision):
    """A native session preceding an explicit checkpoint must rebuild once."""
    if not decision.can_resume or not branch:
        return decision
    checkpoint = await SQLiteCheckpointStore().manual_for(BranchSnapshot.from_messages(branch))
    if checkpoint and decision.message_id in checkpoint.covered_ids:
        from provider_contract import ResumeDecision
        return ResumeDecision(None, "manual_checkpoint", checkpoint.covered_ids[-1])
    return decision
