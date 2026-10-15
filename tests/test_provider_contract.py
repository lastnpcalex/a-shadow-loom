"""Cross-provider Loom user-contract invariants."""

import asyncio
import json
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent


def _assistant(message_id, mode, session_id, model="", content="reply"):
    return {
        "id": message_id,
        "role": "assistant",
        "content": content,
        "cc_session_id": session_id,
        "cc_session_mode": mode,
        "cc_model_used": model,
    }


async def _wait_until(predicate, *, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await predicate()
        if value:
            return value
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not satisfied before timeout")
        await asyncio.sleep(0.01)


@pytest.mark.parametrize("target_mode", ["claude", "codex", "gemini", "goose", "hermes", "dream"])
def test_session_contract_resumes_only_the_nearest_compatible_assistant(target_mode):
    from provider_contract import select_resume_session

    decision = select_resume_session(
        [
            {"id": 1, "role": "user", "content": "first"},
            _assistant(2, target_mode, "session-nearest"),
            {"id": 3, "role": "user", "content": "next"},
        ],
        target_mode,
    )

    assert decision.can_resume
    assert decision.session_id == "session-nearest"
    assert decision.reason == "resume"


def test_continuity_notice_distinguishes_resume_and_harness_handoff():
    from provider_contract import add_continuity_notice, select_resume_session

    resumed_branch = [
        _assistant(1, "goose", "goose-1", "goose:openrouter:z-ai/glm-5.3-flash"),
        {"id": 2, "role": "user", "content": "next"},
    ]
    resumed = select_resume_session(resumed_branch, "goose")
    resumed_prompt = add_continuity_notice(
        "next", resumed, resumed_branch, "goose"
    )
    assert '<loom_continuity kind="native_resume">' in resumed_prompt
    assert "prior Goose ACP native session" in resumed_prompt
    assert resumed_prompt.endswith("\n\nnext")

    handoff_branch = [
        _assistant(3, "codex", "codex-1", "gpt-5.6-sol"),
        {"id": 4, "role": "user", "content": "switch"},
    ]
    handoff = select_resume_session(handoff_branch, "goose")
    handoff_prompt = add_continuity_notice(
        "history", handoff, handoff_branch, "goose"
    )
    assert '<loom_handoff kind="harness">' in handoff_prompt
    assert "Codex to Goose ACP" in handoff_prompt
    assert "canonical branch history" in handoff_prompt


def test_continuity_notice_names_model_handoff():
    from provider_contract import add_continuity_notice, select_resume_session

    branch = [
        _assistant(1, "goose", "goose-1", "goose:openrouter:z-ai/glm-5.2"),
        {"id": 2, "role": "user", "content": "switch model"},
    ]
    decision = select_resume_session(
        branch,
        "goose",
        target_model="goose:openrouter:z-ai/glm-5.3-flash",
        models_match=lambda recorded, target: recorded == target,
    )
    prompt = add_continuity_notice(
        "history",
        decision,
        branch,
        "goose",
        target_model="goose:openrouter:z-ai/glm-5.3-flash",
    )

    assert '<loom_handoff kind="model">' in prompt
    assert "glm-5.2" in prompt
    assert "glm-5.3-flash" in prompt


def test_permission_feedback_notice_is_branch_local_and_xml_safe():
    from provider_contract import add_permission_feedback_notice

    prompt = add_permission_feedback_notice(
        "current task",
        [
            {
                "tool_name": "shell <copy>",
                "allowed": False,
                "feedback": "Use FA26 & refuse overwrite.",
            },
            {"tool_name": "Edit", "allowed": True, "feedback": ""},
        ],
    )

    assert (
        '<decision tool="shell &lt;copy&gt;" result="denied" source="user">'
        in prompt
    )
    assert "Use FA26 &amp; refuse overwrite." in prompt
    assert prompt.endswith("current task")


def test_partial_provider_failure_never_implicitly_resumes_native_session():
    from provider_contract import select_resume_session

    branch = [
        {"id": 1, "role": "user", "content": "do the work"},
        {
            "id": 2,
            "role": "assistant",
            "content": "partial work\n\n[Goose turn failed: upstream timeout]",
            "content_blocks": '[{"type":"tool_use","result":"landed"}]',
            "cc_session_id": "failed-goose-session",
            "cc_session_mode": "goose",
            "cc_model_used": "goose:openrouter:z-ai/glm-5.3-flash",
            "turn_lifecycle_state": "partial_failed",
        },
    ]

    decision = select_resume_session(
        branch,
        "goose",
        target_model="goose:openrouter:z-ai/glm-5.3-flash",
        models_match=lambda recorded, target: recorded == target,
    )

    assert decision.session_id is None
    assert decision.reason == "assistant_failed"


async def test_acp_continuation_receives_feedback_from_its_immediate_parent_only(
    tmp_database,
):
    import database as db
    import server

    conv = await db.create_conversation("ACP feedback", mode="goose")
    first_user = await db.add_message(conv["id"], "user", "first")
    first_assistant = await db.add_message(
        conv["id"], "assistant", "partial", parent_id=first_user["id"]
    )
    await db.save_permission_resolution(
        "goose-note",
        conv["id"],
        "shell",
        False,
        "Use the reviewed destination.",
        source="goose_acp",
        decision_source="loom_policy",
        draft_msg_id=first_assistant["id"],
    )
    second_user = await db.add_message(
        conv["id"], "user", "continue", parent_id=first_assistant["id"]
    )
    branch = await db.get_branch_to_root(second_user["id"])

    prompt = await server._append_prior_acp_permission_feedback("continue", branch)

    assert '<loom_permission_feedback>' in prompt
    assert 'result="blocked" source="loom_policy"' in prompt
    assert "Use the reviewed destination." in prompt
    assert prompt.endswith("continue")


@pytest.mark.parametrize("target_mode,foreign_mode", [
    ("claude", "codex"),
    ("codex", "gemini"),
    ("gemini", "claude"),
    ("goose", "codex"),
    ("hermes", "dream"),
    ("dream", "hermes"),
])
def test_session_contract_never_resurrects_a_session_across_a_to_b_to_a(
    target_mode, foreign_mode
):
    from provider_contract import select_resume_session

    decision = select_resume_session(
        [
            _assistant(1, target_mode, "old-target-session"),
            {"id": 2, "role": "user", "content": "switch"},
            _assistant(3, foreign_mode, "intervening-session"),
            {"id": 4, "role": "user", "content": "switch back"},
        ],
        target_mode,
    )

    assert not decision.can_resume
    assert decision.reason == "provider_boundary"
    assert decision.message_id == 3


def test_session_contract_model_switch_and_legacy_rows_force_replay():
    from provider_contract import select_resume_session

    model_switch = select_resume_session(
        [_assistant(1, "goose", "old", model="goose:openrouter:model-a")],
        "goose",
        target_model="goose:openrouter:model-b",
        models_match=lambda recorded, target: recorded == target,
    )
    legacy = select_resume_session(
        [_assistant(2, None, "unscoped")],
        "dream",
    )

    assert model_switch.reason == "model_boundary"
    assert not model_switch.can_resume
    assert legacy.reason == "legacy_unscoped_session"
    assert not legacy.can_resume


def test_session_contract_compact_and_invalid_assistant_turns_are_boundaries():
    from provider_contract import select_resume_session

    compact = select_resume_session(
        [
            _assistant(1, "claude", "old"),
            {"id": 2, "role": "system", "content": "[CC context compactified (handoff)]"},
            {"id": 3, "role": "user", "content": "continue"},
        ],
        "claude",
    )
    errored = select_resume_session(
        [
            _assistant(1, "claude", "old"),
            _assistant(2, "claude", "bad", content="[Error: failed]"),
        ],
        "claude",
    )

    assert compact.reason == "compact_boundary"
    assert errored.reason == "assistant_error"
    assert not compact.can_resume
    assert not errored.can_resume


def test_automatic_handoff_only_compacts_a_nearest_native_claude_turn():
    import server

    codex_branch = [
        _assistant(1, "codex", "codex-session", "gpt-6-astra"),
        {"id": 2, "role": "user", "content": "continue"},
    ]
    codex_decision, codex_source = server._compactable_anthropic_source(codex_branch)

    assert codex_decision.reason == "provider_boundary"
    assert codex_source is None

    claude_branch = [
        _assistant(3, "claude", "claude-session", "claude-opus-4-7[1m]"),
        {"id": 4, "role": "user", "content": "switch provider"},
    ]
    claude_decision, claude_source = server._compactable_anthropic_source(
        claude_branch
    )

    assert claude_decision.session_id == "claude-session"
    assert claude_source["id"] == 3


def test_provider_handoff_replay_prefers_final_answer_over_large_tool_transcript():
    import json
    import server

    tool_payload = "TOOL_PAYLOAD_MUST_NOT_REPLAY_" * 20_000
    branch = [
        {"role": "user", "content": "Do the work", "image_path": None},
        {
            "role": "assistant",
            "content": "The work is complete; use artifact.md.",
            "content_blocks": json.dumps([
                {"type": "tool_use", "name": "Bash", "input": tool_payload, "result": tool_payload},
                {"type": "text", "text": "The work is complete; use artifact.md."},
            ]),
        },
        {"role": "user", "content": "Continue in parallel", "image_path": None},
    ]

    prompt = server._build_claude_history_prompt(branch)

    assert "The work is complete; use artifact.md." in prompt
    assert "TOOL_PAYLOAD_MUST_NOT_REPLAY_" not in prompt
    assert "Continue in parallel" in prompt


def test_provider_handoff_replay_keeps_tool_only_partial_turns():
    import json
    import server

    branch = [
        {"role": "user", "content": "Inspect it", "image_path": None},
        {
            "role": "assistant",
            "content": "",
            "content_blocks": json.dumps([
                {"type": "tool_use", "name": "Read", "input": "artifact.md", "result": "important evidence"},
            ]),
        },
        {"role": "user", "content": "What did you find?", "image_path": None},
    ]

    prompt = server._build_claude_history_prompt(branch)

    assert "[Used tool: Read]" in prompt
    assert "important evidence" in prompt


def test_model_picker_preserves_unlisted_selector_without_access_claim():
    source = (REPO / "static" / "app.js").read_text(encoding="utf-8")

    assert "prev = 'sonnet'" not in source
    assert "(not in discovered list)" in source
    assert "(currently unavailable)" not in source
    assert "sel.value = prev" in source


def test_historical_harness_label_prefers_message_model_over_current_mode():
    from tests.js_model_registry import evaluate
    assert evaluate("[_agentKindForModel('goose:openrouter:z-ai/glm-5.2', {mode:'codex'}), _agentKindForModel('codex-gpt-5.6-sol', {mode:'goose'})]") == ["goose", "codex"]


def test_antigravity_only_accepts_provider_confirmed_model_identity():
    source = (REPO / "server.py").read_text(encoding="utf-8")

    assert 'evt.get("model_confirmed", True)' in source
    assert '"stage": "model_confirmation"' in source
    assert "Antigravity model mismatch" in source


def test_unknown_selector_has_no_implicit_harness():
    import server

    assert server._mode_for_cc_model("not-a-provider-selector") is None
    assert server._mode_for_cc_model("claude-opus-4-6") == "claude"
    assert server._mode_for_cc_model("gemini:gemini-3.7-flash-high") == "gemini"
    assert server._mode_for_cc_model("codex-gpt-5.6-sol") == "codex"
    assert server._mode_for_cc_model("goose:openrouter:z-ai/glm-5.2") == "goose"


@pytest.mark.asyncio
async def test_conversation_update_rejects_unknown_selector(tmp_database):
    import database as db
    import server

    conv = await db.create_conversation("Selector Contract", mode="claude")
    with pytest.raises(server.HTTPException) as exc:
        await server.api_update_conversation(
            conv["id"], {"cc_model": "not-a-provider-selector"}
        )

    assert exc.value.status_code == 400
    assert "Unknown model selector" in exc.value.detail
    saved = await db.get_conversation(conv["id"])
    assert saved["cc_model"] == "sonnet"


@pytest.mark.asyncio
async def test_shared_handler_switch_back_replays_intervening_codex_turn(
    monkeypatch, tmp_database, tmp_path
):
    import database as db
    import server

    conv = await db.create_conversation(
        "Claude A-B-A",
        mode="claude",
        project_dir=str(tmp_path),
    )
    await db.update_conversation_fields(
        conv["id"], cc_model="claude-opus-4-6", parallel_agents_enabled=1
    )
    conv = await db.get_conversation(conv["id"])
    u1 = await db.add_message(conv["id"], "user", "first")
    a1 = await db.add_message(
        conv["id"], "assistant", "old claude reply", parent_id=u1["id"],
        cc_session_id="claude-old",
    )
    await db.update_message_content(
        a1["id"], cc_session_mode="claude", cc_model_used="claude-opus-4-6"
    )
    u2 = await db.add_message(
        conv["id"], "user", "ask codex", parent_id=a1["id"]
    )
    c1 = await db.add_message(
        conv["id"], "assistant", "intervening codex reply", parent_id=u2["id"],
        cc_session_id="codex-middle",
    )
    await db.update_message_content(
        c1["id"], cc_session_mode="codex", cc_model_used="gpt-5.6-sol"
    )
    u3 = await db.add_message(
        conv["id"], "user", "back to claude", parent_id=c1["id"]
    )

    captured = {}

    async def fake_run_claude(prompt, *args, **kwargs):
        captured["prompt"] = prompt
        captured["resume_session_id"] = kwargs.get("resume_session_id")

        class Proc:
            pid = 9123
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {
                "type": "session_info",
                "session_id": "claude-new",
                "model": "claude-opus-4-6",
            }
            yield {"type": "text_delta", "text": "replayed"}
            yield {
                "type": "result",
                "session_id": "claude-new",
                "is_error": False,
                "result_text": "replayed",
            }

        return Proc(), events()

    monkeypatch.setattr(server.claude_client, "run_claude", fake_run_claude)

    await server._handle_claude_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=await db.get_conversation(conv["id"]),
        data={
            "action": "generate",
            "parent_id": u3["id"],
            "cc_model": "claude-opus-4-6",
        },
    )

    assert captured["resume_session_id"] is None
    assert "old claude reply" in captured["prompt"]
    assert "intervening codex reply" in captured["prompt"]
    assert "back to claude" in captured["prompt"]


@pytest.mark.asyncio
async def test_same_codex_native_continuation_neither_replays_nor_runs_handoff_gate(
    monkeypatch, tmp_database, tmp_path
):
    import database as db
    import server

    conv = await db.create_conversation(
        "Codex native continuation",
        mode="codex",
        project_dir=str(tmp_path),
    )
    await db.update_conversation_fields(
        conv["id"], cc_model="gpt-6-astra", parallel_agents_enabled=1
    )
    conv = await db.get_conversation(conv["id"])
    u1 = await db.add_message(conv["id"], "user", "first request")
    a1 = await db.add_message(
        conv["id"],
        "assistant",
        "old codex reply",
        parent_id=u1["id"],
        cc_session_id="codex-native",
    )
    await db.update_message_content(
        a1["id"], cc_session_mode="codex", cc_model_used="gpt-6-astra"
    )
    u2 = await db.add_message(
        conv["id"], "user", "continue natively", parent_id=a1["id"]
    )
    captured = {}

    async def fail_sum_branch_tokens(*args, **kwargs):
        raise AssertionError("native Codex continuation must not run the replay gate")

    async def fake_run_codex(prompt, *args, **kwargs):
        captured["prompt"] = prompt
        captured["resume_session_id"] = kwargs.get("resume_session_id")

        class Proc:
            pid = 9124
            returncode = 0

            async def wait(self):
                return 0

        async def events():
            yield {
                "type": "session_info",
                "session_id": "codex-next",
                "model": "gpt-6-astra",
                "model_confirmed": True,
            }
            yield {"type": "text_delta", "text": "continued"}
            yield {
                "type": "result",
                "session_id": "codex-next",
                "is_error": False,
                "result_text": "continued",
            }

        return Proc(), events()

    monkeypatch.setattr(db, "sum_branch_tokens", fail_sum_branch_tokens)
    monkeypatch.setattr(server.codex_client, "run_codex", fake_run_codex)

    await server._handle_claude_generation(
        websocket=None,
        conv_id=conv["id"],
        conv=conv,
        data={
            "action": "generate",
            "parent_id": u2["id"],
            "cc_model": "gpt-6-astra",
        },
    )

    assert captured["resume_session_id"] == "codex-native"
    assert "first request" not in captured["prompt"]
    assert "old codex reply" not in captured["prompt"]
    assert captured["prompt"].endswith("continue natively")


@pytest.mark.asyncio
async def test_compact_handoff_does_not_search_past_provider_boundary(
    monkeypatch, tmp_database, tmp_path
):
    import database as db
    import server

    conv = await db.create_conversation(
        "Compact A-B-A", mode="claude", project_dir=str(tmp_path)
    )
    u1 = await db.add_message(conv["id"], "user", "first")
    a1 = await db.add_message(
        conv["id"], "assistant", "old claude", parent_id=u1["id"],
        cc_session_id="claude-old",
    )
    await db.update_message_content(
        a1["id"], cc_session_mode="claude", cc_model_used="claude-opus-4-6"
    )
    u2 = await db.add_message(conv["id"], "user", "switch", parent_id=a1["id"])
    c1 = await db.add_message(
        conv["id"], "assistant", "codex middle", parent_id=u2["id"],
        cc_session_id="codex-middle",
    )
    await db.update_message_content(
        c1["id"], cc_session_mode="codex", cc_model_used="gpt-5.6-sol"
    )
    u3 = await db.add_message(conv["id"], "user", "continue", parent_id=c1["id"])

    async def fail_run_claude(*args, **kwargs):
        raise AssertionError("compaction must not resurrect the old Claude session")

    monkeypatch.setattr(server.claude_client, "run_claude", fail_run_claude)

    result = await server._run_compact_handoff(
        conv["id"],
        await db.get_conversation(conv["id"]),
        u3["id"],
        "claude-opus-4-6",
        "high",
        str(tmp_path),
    )

    assert result == (None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["cancel", "interrupt"])
async def test_stop_before_first_token_preserves_queue_with_distinct_context_semantics(
    monkeypatch, tmp_database, tmp_path, disposition
):
    """Both stops preserve topology; only Interrupt preserves native context."""
    import database as db
    import server

    conv = await db.create_conversation(
        "Cancel before token",
        mode="codex",
        project_dir=str(tmp_path),
    )
    await db.update_conversation_fields(
        conv["id"], cc_model="gpt-5.6-sol", parallel_agents_enabled=1
    )
    conv = await db.get_conversation(conv["id"])
    prompt = await db.add_message(conv["id"], "user", "slow first turn")
    stream_block = asyncio.Event()

    class Proc:
        pid = 0
        returncode = None

    async def fake_run_codex(*args, **kwargs):
        async def events():
            yield {
                "type": "session_info",
                "session_id": "codex-cancelled-session",
                "model": "gpt-5.6-sol",
                "model_confirmed": True,
                "model_attestation": {
                    "status": "verified",
                    "harness": "Codex app-server",
                    "requested_model": "gpt-5.6-sol",
                    "launch_model": "gpt-5.6-sol",
                    "effective_model": "gpt-5.6-sol",
                    "model_provider": "openai",
                    "source": "codex_app_server_thread_response",
                    "verification_level": "harness",
                },
            }
            await stream_block.wait()

        return Proc(), events()

    async def fake_cancel(_proc):
        _proc.returncode = -1

    async def no_ws(*args, **kwargs):
        return None

    monkeypatch.setattr(server.codex_client, "run_codex", fake_run_codex)
    monkeypatch.setattr(server.codex_client, "cancel_codex", fake_cancel)
    monkeypatch.setattr(server, "_ws_send", no_ws)

    task = asyncio.create_task(
        server._handle_claude_generation(
            None,
            conv["id"],
            conv,
            {"action": "generate", "parent_id": prompt["id"], "cc_model": "gpt-5.6-sol"},
        )
    )

    async def find_draft():
        children = await db.get_children(prompt["id"])
        return next(
            (message for message in children if message["role"] == "assistant"),
            None,
        )

    draft = await _wait_until(find_draft)

    async def session_is_recorded():
        message = await db.get_message(draft["id"])
        return message if message and message.get("model_attestation") else None

    # A draft is created before the provider process starts. This test covers
    # stopping after native session establishment but before model output, so
    # wait for the session event rather than racing the launch boundary.
    await _wait_until(session_is_recorded)
    queued = await db.add_message(
        conv["id"], "user", "queued after cancel", parent_id=draft["id"]
    )
    await db.enqueue_generation(
        queued["id"], conv["id"], draft["id"], settings={"cc_model": "gpt-5.6-sol"}
    )

    server._request_generation_stop(task, disposition)
    await asyncio.gather(task, return_exceptions=True)

    saved_draft = await db.get_message(draft["id"])
    saved_queue = await db.get_message(queued["id"])
    queue_rows = await db.list_queued_generations(conv["id"])
    assert saved_draft is not None
    if disposition == "interrupt":
        assert saved_draft["content"] == "[Turn interrupted before response]"
        assert saved_draft["cc_session_id"] == "codex-cancelled-session"
        assert saved_draft["cc_session_mode"] == "codex"
        assert saved_draft["turn_lifecycle_state"] == "interrupted"
    else:
        assert saved_draft["content"] == "[Turn cancelled; partial response discarded]"
        assert saved_draft["cc_session_id"] is None
        assert saved_draft["cc_session_mode"] is None
        assert saved_draft["content_blocks"] is None
        assert saved_draft["turn_lifecycle_state"] == "cancelled"
    attestation = json.loads(saved_draft["model_attestation"])
    assert attestation["session_continuity"] == "rebuilt"
    assert attestation["session_continuity_reason"] == "no_assistant_session"
    assert saved_queue is not None
    assert saved_queue["parent_id"] == draft["id"]
    assert [row["user_msg_id"] for row in queue_rows] == [queued["id"]]


@pytest.mark.asyncio
async def test_interrupt_retains_partial_tools_for_native_resume_while_cancel_discards_them(
    tmp_database,
):
    import database as db
    import server
    from provider_contract import select_resume_session

    conv = await db.create_conversation("Stop semantics", mode="codex")
    blocks = [
        {
            "type": "tool_use",
            "name": "Read",
            "tool_id": "tool-1",
            "input": '{"path":"notes.txt"}',
            "result": "retained tool result",
        }
    ]

    async def finalize(disposition: str, draft_id: int):
        task = asyncio.current_task()
        task._stop_disposition = disposition
        await server._finalize_user_stopped_generation(
            conv["id"],
            draft_id,
            partial_text="partial reasoning made visible",
            content_blocks=blocks,
            session_id=f"{disposition}-session",
            session_mode="codex",
            model_used="gpt-5.6-sol",
        )

    user_interrupt = await db.add_message(conv["id"], "user", "interrupt")
    interrupted = await db.add_message(
        conv["id"], "assistant", "draft", parent_id=user_interrupt["id"]
    )
    interrupt_task = asyncio.create_task(finalize("interrupt", interrupted["id"]))
    await interrupt_task
    interrupted = await db.get_message(interrupted["id"])
    assert interrupted["content"] == "partial reasoning made visible"
    assert "retained tool result" in interrupted["content_blocks"]
    assert interrupted["cc_session_id"] == "interrupt-session"
    assert select_resume_session(
        [user_interrupt, interrupted], "codex"
    ).session_id == "interrupt-session"

    user_cancel = await db.add_message(conv["id"], "user", "cancel")
    cancelled = await db.add_message(
        conv["id"], "assistant", "draft", parent_id=user_cancel["id"]
    )
    cancel_task = asyncio.create_task(finalize("cancel", cancelled["id"]))
    await cancel_task
    cancelled = await db.get_message(cancelled["id"])
    assert cancelled["content"] == "[Turn cancelled; partial response discarded]"
    assert cancelled["content_blocks"] is None
    assert cancelled["cc_session_id"] is None
    decision = select_resume_session([user_cancel, cancelled], "codex")
    assert decision.session_id is None
    assert decision.reason == "no_assistant_session"


def test_cancelled_no_work_placeholder_is_transparent_to_same_harness_resume():
    from provider_contract import select_resume_session

    branch = [
        {"id": 1, "role": "user", "content": "first"},
        {"id": 2, "role": "assistant", "content": "completed",
         "cc_session_id": "goose-native", "cc_session_mode": "goose",
         "cc_model_used": "goose:openrouter:z-ai/glm-5.3-flash",
         "turn_lifecycle_state": "completed"},
        {"id": 3, "role": "user", "content": "cancel this"},
        {"id": 4, "role": "assistant",
         "content": "[Turn cancelled; partial response discarded]",
         "cc_session_id": None, "cc_session_mode": None,
         "content_blocks": None, "turn_lifecycle_state": "cancelled"},
        {"id": 5, "role": "user", "content": "continue after cancellation"},
    ]
    decision = select_resume_session(
        branch, "goose", target_model="goose:openrouter:z-ai/glm-5.3-flash",
        models_match=lambda recorded, target: recorded == target,
    )
    assert decision.session_id == "goose-native"
    assert decision.reason == "resume"
    assert decision.message_id == 2


def test_resume_rejected_recognizes_stream_limits_and_rollout_errors():
    from provider_contract import resume_rejected

    assert resume_rejected('Codex app-server launch failed: Codex app-server stdout failed: Separator is not found, and chunk exceed the limit')
    assert resume_rejected('no rollout found for thread id 01a0df8c-4247-7662-99b3-0d9c3c8b3216')
    assert resume_rejected('Codex app-server stdout line exceeded buffer limit: ValueError: Separator is not found, and chunk exceed the limit')
    assert resume_rejected('session not found')
    assert not resume_rejected('rate limit exceeded')
    assert not resume_rejected('invalid tool arguments')


def test_codex_thread_request_sets_exclude_turns_on_resume_and_fork():
    from codex_client import _codex_thread_request

    # Fresh session: thread/start does not set excludeTurns
    method, params = _codex_thread_request(
        cwd='/workspace',
        codex_model='o3',
        approval_policy='on-request',
        sandbox_mode='workspace-write',
    )
    assert method == 'thread/start'
    assert 'excludeTurns' not in params

    # Resumed session: thread/resume sets excludeTurns: True
    method, params = _codex_thread_request(
        cwd='/workspace',
        codex_model='o3',
        approval_policy='on-request',
        sandbox_mode='workspace-write',
        resume_session_id='sess-123',
    )
    assert method == 'thread/resume'
    assert params['excludeTurns'] is True
    assert params['threadId'] == 'sess-123'

    # Forked session: thread/fork sets excludeTurns: True
    method, params = _codex_thread_request(
        cwd='/workspace',
        codex_model='o3',
        approval_policy='on-request',
        sandbox_mode='workspace-write',
        resume_session_id='sess-123',
        fork_session=True,
    )
    assert method == 'thread/fork'
    assert params['excludeTurns'] is True
    assert params['threadId'] == 'sess-123'
