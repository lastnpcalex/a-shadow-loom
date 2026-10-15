"""Recovery requires native evidence; replay admission budgets the whole payload.

No live provider, sidecar, host lifecycle action, or user workspace is used.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

import provider_contract as contract
import replay_budget as budgets


def failed_message(**updates):
    message = {
        "id": 2, "role": "assistant", "content": "partial work",
        "turn_lifecycle_state": "partial_failed", "cc_session_id": "native-2",
        "cc_session_mode": "codex", "cc_model_used": "model-a",
        "model_attestation": {"status": "verified", "session_recovery": contract.recovery_evidence(
            "native-2", "codex", "stream disconnected", terminal_confirmed=True,
        )},
    }
    message.update(updates)
    return message


@pytest.mark.parametrize("mutation", [
    {"cc_session_id": "different"}, {"cc_session_mode": "goose"},
    {"cc_model_used": "model-b"}, {"model_attestation": None},
    {"model_attestation": "{invalid"}, {"model_attestation": {"status": "mismatch"}},
])
def test_failed_resume_evidence_cannot_cross_boundaries(mutation):
    message = failed_message(**mutation)
    assert not contract.select_resume_session([message], "codex", target_model="model-a").can_resume


@pytest.mark.parametrize("error,code,terminal,allowed", [
    ("stream disconnected", None, True, True),
    ("rate limit", None, True, True),
    ("Upstream idle timeout exceeded", None, True, True),
    ("provider failure", {"ResponseStreamDisconnected": {"httpStatusCode": 502}}, True, True),
    ("You've hit your session limit · resets 7:40pm (America/Los_Angeles)", None, True, True),
    ("hit your org's monthly usage limit", None, True, True),
    ("stream disconnected", None, False, False),
    ("context window exceeded; stream disconnected", None, True, False),
    ("rate limit", "ContextWindowExceeded", True, False),
    ("session not found", None, True, False),
    ("provider process exited", None, False, False),
    ("unknown failure", None, True, False),
])
def test_explicit_recovery_evidence(error, code, terminal, allowed):
    evidence = contract.recovery_evidence("native-2", "codex", error, terminal_confirmed=terminal, error_code=code)
    assert evidence["can_resume"] is allowed
    msg = failed_message(content="", turn_lifecycle_state="failed", model_attestation={"session_recovery": evidence})
    decision = contract.select_resume_session([msg], "codex", target_model="model-a")
    assert decision.can_resume is allowed
    if allowed:
        assert decision.reason == "recoverable_failure"
        notice = contract.add_continuity_notice("new task", decision, [msg], "codex")
        assert "Inspect retained tool outcomes" in notice
        assert notice.endswith("\n\nnew task")


def test_partial_handoff_retains_tools_even_with_prose():
    import server
    message = failed_message(content_blocks=json.dumps([
        {"type": "tool_use", "name": "Write", "input": {"path": "report.md"}, "result": "saved"},
        {"type": "tool_use", "name": "Bash", "input": "unconfirmed action"},
        {"type": "tool_use", "name": "Read", "result": "x" * 2500},
    ]))
    prompt = server._build_claude_history_prompt([message, {"role": "user", "content": "continue"}], None)
    assert all(value in prompt for value in ("partial work", "report.md", "Captured result: saved", "Verify the action's outcome", "[preview truncated]"))
    assert "x" * 2001 not in prompt
    # Ordinary final answers remain compact rather than replaying tool logs.
    message["turn_lifecycle_state"] = "completed"
    assert "Captured result" not in server._build_claude_history_prompt([message], None)


def test_structured_claude_error_is_not_discarded():
    import claude_client
    import server
    result = next(e for e in claude_client._process_event({"type": "result", "is_error": True, "errors": ["rate limit"]}, {}) if e["type"] == "result")
    assert server._cc_result_error_detail(result) == "rate limit"


async def test_confirmed_codex_success_after_tool_error_remains_resumable(monkeypatch, tmp_database, tmp_path):
    import database as db
    import server

    conv = await db.create_conversation("Completed turn continuation", mode="codex", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], cc_model="codex-gpt-5.6-sol", parallel_agents_enabled=1)
    prior = await db.add_message(conv["id"], "assistant", "old history " * 100_000, cc_session_id="native-prior")
    await db.update_message_content(prior["id"], cc_session_mode="codex", cc_model_used="gpt-5.6-sol")
    user = await db.add_message(conv["id"], "user", "inspect source", parent_id=prior["id"])
    calls = []

    class Proc:
        pid = 999_989
        returncode = 0
        async def wait(self):
            return 0

    async def run(prompt, *args, **kw):
        calls.append((prompt, kw.get("resume_session_id")))
        async def events():
            yield {"type": "session_info", "session_id": "native-completed", "model": "gpt-5.6-sol"}
            yield {"type": "error", "message": "an earlier operation failed"}
            yield {"type": "text_delta", "text": "Recovered and finished."}
            yield {"type": "result", "session_id": "native-completed", "terminal_confirmed": True,
                   "is_error": False, "error": "", "result_text": "Recovered and finished."}
        return Proc(), events()

    monkeypatch.setattr(server.codex_client, "run_codex", run)
    monkeypatch.setattr(server, "_ws_send", AsyncMock())
    monkeypatch.setattr(server, "_ws_broadcast_all", AsyncMock())
    for parent, expected_session in [(user["id"], "native-prior"), (None, "native-completed")]:
        if parent is None:
            parent = (await db.add_message(conv["id"], "user", "continue", parent_id=leaf["id"]))["id"]
        await server._handle_claude_generation(None, conv["id"], await db.get_conversation(conv["id"]),
                                               {"action": "generate", "parent_id": parent})
        leaf = await db.get_active_leaf(conv["id"])
        assert leaf["content"] == "Recovered and finished."
        assert leaf["turn_lifecycle_state"] not in {"failed", "partial_failed"}
        assert calls[-1][1] == expected_session
        assert len(calls[-1][0]) < 1000, "native continuation must not replay the oversized branch"
    assert server._cc_result_error_detail({"is_error": False, "result_text": "Finished."}) == ""


@pytest.mark.parametrize("native,allowed", [
    ({"id": "turn", "status": "completed", "startedAt": 1000, "completedAt": 1010}, True),
    ({"id": "turn", "status": "failed", "startedAt": 1000, "completedAt": 1010}, False),
    ({"id": "turn", "status": "interrupted", "startedAt": 1000, "completedAt": 1010}, False),
    ({"id": "ancestor", "status": "completed", "startedAt": 900, "completedAt": 910}, False),
    ({"id": "turn", "status": "completed", "startedAt": 1000, "completedAt": 1010, "error": {"message": "failed"}}, False),
    (None, False),
])
async def test_false_failure_recovery_requires_current_native_completion(monkeypatch, native, allowed):
    import server
    message = failed_message(created_at=1000.5, model_attestation={
        "status": "verified", "session_recovery": contract.recovery_evidence(
            "native-2", "codex", "misclassified tool result", terminal_confirmed=True,
        ),
    })
    decision = contract.select_resume_session([message], "codex", target_model="model-a")
    assert not decision.can_resume
    probe = AsyncMock(return_value=native)
    monkeypatch.setattr(server.codex_client, "read_codex_last_turn", probe)
    recovered = await server._recheck_completed_codex_boundary([message], decision, "model-a", "workspace")
    assert recovered.can_resume is allowed
    probe.assert_awaited_once_with("native-2", "workspace")
    assert message["turn_lifecycle_state"] == "partial_failed", "verification must not rewrite historical messages"
    if allowed:
        assert recovered.reason == "verified_completed_recovery"
        assert "verified as completed" in contract.add_continuity_notice("next", recovered, [message], "codex")


@pytest.mark.parametrize("change", [
    {"cc_session_mode": "goose"}, {"cc_session_id": "other"}, {"created_at": None},
    {"model_attestation": {"status": "mismatch"}},
])
async def test_native_completion_probe_never_crosses_unverified_boundaries(monkeypatch, change):
    import server
    message = failed_message(created_at=1000, **change) if "created_at" not in change else failed_message(**change)
    monkeypatch.setattr(server.codex_client, "read_codex_last_turn", AsyncMock(side_effect=AssertionError("must not probe")))
    decision = contract.select_resume_session([message], "codex", target_model="model-a")
    assert await server._recheck_completed_codex_boundary([message], decision, "model-a", "workspace") == decision
    server.codex_client.read_codex_last_turn.assert_not_awaited()


@pytest.mark.parametrize("harness", ["codex", "claude", "gemini", "goose", "hermes"])
async def test_native_resume_has_no_payload_estimation_or_runtime_probe(monkeypatch, harness):
    probe = AsyncMock(side_effect=AssertionError("native resume must not run replay preflight"))
    monkeypatch.setattr(budgets, "context_capacity", probe)
    assert await budgets.check_fresh_payload("large native task" * 10000, model="anything", harness=harness, resume_session_id="native") is None
    probe.assert_not_called()


async def test_full_payload_not_just_branch_text_is_budgeted(monkeypatch):
    monkeypatch.setattr(budgets, "context_capacity", AsyncMock(return_value=(40_000, "test runtime")))
    budget = await budgets.assess_payload("p" * 45_000, model="x", harness="goose", instructions="i" * 30_000, image_count=1)
    assert budget.prompt_tokens == 15_000
    assert budget.instruction_tokens == 10_000
    assert budget.attachment_reserve > 0 and budget.output_reserve > 0 and budget.tool_reserve > 0
    with pytest.raises(budgets.ReplayBudgetExceeded, match="history was not truncated"):
        await budgets.check_fresh_payload("p" * 45_000, model="x", harness="goose", instructions="i" * 30_000, image_count=1)
    assert asyncio.current_task()._replay_budget["context_source"] == "test runtime"


@pytest.mark.parametrize("props,expected", [
    ({"default_generation_settings": {"n_ctx": 80_128}, "total_slots": 2, "model_meta": {"n_ctx_train": 262_144}}, 80_128),
    ({"n_ctx_per_seq": 32_768, "n_ctx": 131_072}, 32_768),
    ({"n_ctx": 131_072, "model_meta": {"n_ctx_train": 262_144}}, None),
    ({"default_generation_settings": []}, None),
])
def test_context_capacity_uses_per_slot_not_training_or_total_kv(props, expected):
    assert budgets.local_runtime_capacity(props) == expected


async def test_local_live_capacity_wins_over_static_threshold(monkeypatch):
    client_class = httpx.AsyncClient
    requests = []
    def serve(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 80_128}})
    monkeypatch.setattr(budgets.httpx, "AsyncClient", lambda **kw: client_class(transport=httpx.MockTransport(serve), **kw))
    monkeypatch.setattr(budgets.config, "llama_host_url", lambda: "http://local-test.invalid")
    capacity, source = await budgets.context_capacity("goose:llama:example.gguf", "goose")
    assert capacity == 80_128 and "per-slot" in source
    assert requests == ["/props"]
    with pytest.raises(budgets.ReplayBudgetExceeded):
        await budgets.check_fresh_payload("x" * 220_000, model="goose:llama:example.gguf", harness="goose")


async def test_local_config_fallback_uses_exact_model_case_insensitively(monkeypatch, tmp_path):
    client_class = httpx.AsyncClient
    monkeypatch.setattr(budgets.httpx, "AsyncClient", lambda **kw: client_class(transport=httpx.MockTransport(lambda r: httpx.Response(503)), **kw))
    monkeypatch.setattr(budgets, "Path", lambda _: tmp_path / "module.py")
    (tmp_path / "models_config.json").write_text(json.dumps({"Example-Q4_K_M.gguf": {"ctx_size": 196_608}}), encoding="utf-8")
    capacity, source = await budgets.context_capacity("goose:llama:example-q4_k_m.gguf", "goose")
    assert capacity == 196_608
    assert source == "configured llama model context (runtime unavailable)"


async def test_metadata_budget_and_runtime_override():
    capacity, source = await budgets.context_capacity("goose:auto:openrouter:z-ai/glm-5.3-flash", "goose")
    assert capacity == 1_310_720 and source == "OpenRouter model metadata"
    capacity, source = await budgets.context_capacity("gpt-6-astra", "codex", runtime_context=1_050_000)
    assert capacity == 1_050_000 and source == "harness runtime context"
    # 1M catalog entries are not an exemption from final payload admission.
    with pytest.raises(budgets.ReplayBudgetExceeded):
        await budgets.check_fresh_payload("x" * 3_000_000, model="sonnet[1m]", harness="claude")


async def test_handoff_precheck_uses_rendered_outcomes_not_database_token_sum(monkeypatch):
    import server
    monkeypatch.setattr(budgets, "context_capacity", AsyncMock(return_value=(32_768, "test")))
    branch = [failed_message(content="x" * 60_000, token_estimate=1), {"role": "user", "content": "new task"}]
    budget = await server._handoff_payload_budget(branch, "goose:llama:example.gguf", "goose")
    assert budget.prompt_tokens >= 20_000
    assert budget.instruction_tokens > 0
    assert budget.total_tokens > budget.context_tokens


async def test_unknown_remote_limit_does_not_prematurely_compact_source():
    import server
    branch = [{"role": "user", "content": "x" * 900_000}]
    assert await server._handoff_payload_budget(branch, "gpt-6-astra", "codex") is None


async def test_codex_oversize_payload_never_reaches_thread_or_turn_start(monkeypatch, tmp_path):
    import codex_client
    methods = []
    class Proc:
        pid = 999_987
        returncode = None
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdin = self
        def is_closing(self):
            return self.returncode is not None
        def close(self):
            self.returncode = 0
            self.stdout.feed_eof()
            self.stderr.feed_eof()
        def write(self, data):
            request = json.loads(data)
            methods.append(request["method"])
            if "id" not in request:
                return
            result = {"config": {"model_context_window": 32_768}} if request["method"] == "config/read" else {}
            self.stdout.feed_data((json.dumps({"id": request["id"], "result": result}) + "\n").encode())
        async def drain(self):
            await asyncio.sleep(0)
        async def wait(self):
            return self.returncode
    proc = Proc()
    monkeypatch.setattr(codex_client, "_find_codex_exe", lambda: "codex")
    monkeypatch.setattr(codex_client, "_thread_mcp_servers", lambda *a, **k: {})
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    _, stream = await codex_client.run_codex("x" * 100_000, str(tmp_path), model="gpt-6-astra")
    async with asyncio.timeout(2):
        events = [event async for event in stream]
    assert "config/read" in methods
    assert "thread/start" not in methods and "turn/start" not in methods
    assert any("history was not truncated" in e.get("message", "") for e in events)
    assert proc.returncode == 0


@pytest.mark.parametrize("harness,model", [("claude", "sonnet"), ("codex", "gpt-6-astra"), ("goose", "goose:openrouter:z-ai/glm-5.2")])
@pytest.mark.parametrize("terminal,error,recoverable", [(True, "rate limit", True), (False, "process exited", False), (True, "context window exceeded", False)])
async def test_unresolved_or_non_context_failure_requires_user_continuation(monkeypatch, tmp_database, tmp_path, harness, model, terminal, error, recoverable):
    import database as db
    import server
    conv = await db.create_conversation("Recovery regression", mode=harness, project_dir=str(tmp_path))
    # Adapter test entry, not the scheduler under test. No real provider runs.
    await db.update_conversation_fields(conv["id"], cc_model=model, parallel_agents_enabled=1)
    user = await db.add_message(conv["id"], "user", "initial")
    prior = await db.add_message(conv["id"], "assistant", "prior result", parent_id=user["id"], cc_session_id="native-1")
    await db.update_message_content(prior["id"], cc_session_mode=harness, cc_model_used=model)
    user = await db.add_message(conv["id"], "user", "do the next task", parent_id=prior["id"])
    calls = []
    class Proc:
        pid = 999_981
        returncode = 0
        async def wait(self):
            return 0
    async def run(prompt, *args, **kw):
        calls.append((prompt, kw))
        async def events():
            yield {"type": "session_info", "session_id": "native-2", "model": model}
            yield {"type": "text_delta", "text": "Partial task result."}
            yield {"type": "tool_start", "name": "Write", "tool_id": "write-1"}
            yield {"type": "tool_input_delta", "tool_id": "write-1", "json": '{"path":"report.md"}'}
            yield {"type": "tool_result", "tool_id": "write-1", "content": "mutation landed"}
            if error == "context window exceeded":
                # Completed boundaries now recover. An additional unacknowledged
                # call must still require reconciliation, retaining both records.
                yield {"type": "tool_start", "name": "Edit", "tool_id": "unknown-outcome"}
            yield {"type": "result", "session_id": "native-2", "is_error": True, "stop_reason": "error", "error": error, "terminal_confirmed": terminal}
        return Proc(), events()
    monkeypatch.setattr(getattr(server, f"{harness}_client"), f"run_{harness}", run)
    monkeypatch.setattr(server, "_ws_send", AsyncMock())
    monkeypatch.setattr(server, "_ws_broadcast_all", AsyncMock())
    handler = server._handle_goose_generation if harness == "goose" else server._handle_claude_generation
    async def generate(parent):
        await handler(None, conv["id"], await db.get_conversation(conv["id"]), {"action": "generate", "parent_id": parent, "cc_model": model})
    await generate(user["id"])
    assert len(calls) == 1, "unknown outcomes/non-context failures must not trigger automatic replay"
    leaf = await db.get_active_leaf(conv["id"])
    assert leaf["turn_lifecycle_state"] == "partial_failed"
    assert "mutation landed" in leaf["content_blocks"]
    assert asyncio.current_task()._generation_terminal_override == "partial_failed"
    assert json.loads(leaf["model_attestation"])["session_recovery"]["can_resume"] is recoverable
    user2 = await db.add_message(conv["id"], "user", "inspect then continue", parent_id=leaf["id"])
    await generate(user2["id"])
    assert len(calls) == 2
    assert calls[1][1].get("resume_session_id") == ("native-2" if recoverable else None)
    if recoverable:
        assert "Inspect retained tool outcomes" in calls[1][0]
    else:
        assert "mutation landed" in calls[1][0]


@pytest.mark.parametrize("error,launches", [("rate limit", 1), ("session not found", 2)])
async def test_empty_resume_rebuilds_only_on_explicit_session_rejection(monkeypatch, tmp_database, tmp_path, error, launches):
    import database as db
    import server
    conv = await db.create_conversation("Empty failure", mode="codex", project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], cc_model="gpt-6-astra", parallel_agents_enabled=1)
    prior = await db.add_message(conv["id"], "assistant", "prior result", cc_session_id="native")
    await db.update_message_content(prior["id"], cc_session_mode="codex", cc_model_used="gpt-6-astra")
    user = await db.add_message(conv["id"], "user", "next", parent_id=prior["id"])
    calls = []
    class Proc:
        pid = 999_988
        returncode = 0
        async def wait(self):
            return 0
    async def run(prompt, *args, **kw):
        calls.append(kw.get("resume_session_id"))
        async def events():
            if kw.get("resume_session_id"):
                yield {"type": "error", "message": error}
            else:
                yield {"type": "text_delta", "text": "fallback result"}
                yield {"type": "result", "session_id": "fresh", "is_error": False}
        return Proc(), events()
    monkeypatch.setattr(server.codex_client, "run_codex", run)
    monkeypatch.setattr(server, "_ws_send", AsyncMock())
    monkeypatch.setattr(server, "_ws_broadcast_all", AsyncMock())
    await server._handle_claude_generation(None, conv["id"], await db.get_conversation(conv["id"]), {"action": "generate", "parent_id": user["id"]})
    assert len(calls) == launches
    leaf = await db.get_active_leaf(conv["id"])
    if launches == 1:
        assert error in leaf["content"]
        assert leaf["turn_lifecycle_state"] == "failed"
    else:
        assert calls == ["native", None]
        assert leaf["content"] == "fallback result"
        assert leaf["turn_lifecycle_state"] not in {"failed", "partial_failed"}
