"""Production handler matrix with synthetic providers and summarizer.

Exercises real DB/session/preparation/finalization wiring; NOT native acceptance.
Never launches a CLI, accesses user records, or changes a host process.
"""
import itertools
from unittest.mock import AsyncMock

import pytest

import database as db
import context_runtime
from continuity_engine import SummaryResult
from replay_budget import PayloadBudget


MODELS = {"claude": "claude-opus-4-6", "codex": "gpt-6-astra",
          "gemini": "gemini:gemini-3.7-flash-high", "goose": "goose:openrouter:z-ai/glm-5.2"}


class SyntheticSummary:
    identity = "synthetic-local"
    calls = []

    def __init__(self, status=None):
        pass

    async def summarize(self, text, max_tokens):
        self.calls.append(text)
        assert "KEEP_KEY_PRIVATE" in text
        return SummaryResult("KEEP_KEY_PRIVATE; key.md was saved. Grading remains unfinished. "
                             "Do not repeat the saved write; retrieve original evidence.", self.identity)


async def setup(monkeypatch, tmp_path, source, target):
    import server
    conv = await db.create_conversation("Synthetic harness matrix", mode=target, project_dir=str(tmp_path))
    await db.update_conversation_fields(conv["id"], cc_model=MODELS[target], parallel_agents_enabled=1)
    user = await db.add_message(conv["id"], "user", "KEEP_KEY_PRIVATE " + "old detail " * 10000)
    assistant = await db.add_message(conv["id"], "assistant", "key.md saved; unfinished grading",
                                     parent_id=user["id"], cc_session_id="source-session")
    await db.update_message_content(assistant["id"], cc_session_mode=source, cc_model_used=MODELS[source],
                                    turn_lifecycle_state="completed")
    current = await db.add_message(conv["id"], "user", "Continue grading without rewriting key.md",
                                   parent_id=assistant["id"])
    calls = []

    class Proc:
        pid = 0
        returncode = 0
        async def wait(self):
            return 0

    async def launch(prompt, *args, **kwargs):
        calls.append((prompt, kwargs))
        async def events():
            yield {"type": "session_info", "session_id": "destination-session", "model": MODELS[target],
                   "model_confirmed": True}
            yield {"type": "text_delta", "text": "Grading continued; key preserved."}
            yield {"type": "result", "session_id": "destination-session", "is_error": False,
                   "terminal_confirmed": True, "result_text": "Grading continued; key preserved."}
        return Proc(), events()

    for client, name in ((server.claude_client, "run_claude"), (server.codex_client, "run_codex"),
                         (server.gemini_client, "run_gemini"), (server.goose_client, "run_goose")):
        monkeypatch.setattr(client, name, launch)
    monkeypatch.setattr(server, "_ws_send", AsyncMock())
    monkeypatch.setattr(server, "_ws_broadcast_all", AsyncMock())
    monkeypatch.setattr(server, "_run_compact_handoff", AsyncMock(side_effect=AssertionError("Claude-only gate forbidden")))
    monkeypatch.setattr(context_runtime, "assess_payload", AsyncMock(return_value=PayloadBudget(
        20000, "synthetic destination", 0, 0, 1000, 0, 1000, 1000)))
    monkeypatch.setattr(context_runtime, "ConfiguredLocalSummarizer", SyntheticSummary)
    SyntheticSummary.calls = []
    return conv, current, calls


async def run(conv, parent, target):
    import server
    handler = server._handle_goose_generation if target == "goose" else server._handle_claude_generation
    await handler(None, conv["id"], await db.get_conversation(conv["id"]),
                  {"action": "generate", "parent_id": parent, "cc_model": MODELS[target]})
    return await db.get_active_leaf(conv["id"])


@pytest.mark.parametrize("source,target", itertools.permutations(MODELS, 2))
async def test_every_directed_handler_handoff_and_native_followup(monkeypatch, tmp_database, tmp_path, source, target):
    conv, current, calls = await setup(monkeypatch, tmp_path, source, target)
    result = await run(conv, current["id"], target)
    assert len(calls) == 1
    assert calls[0][1].get("resume_session_id") is None
    assert "KEEP_KEY_PRIVATE" in calls[0][0]
    assert "Continue grading without rewriting key.md" in calls[0][0]
    assert "context_scope:" in calls[0][0]
    assert len(calls[0][0]) < 10000
    if target == 'goose':
        region = calls[0][1]['context_replay_history']
        assert region and region in calls[0][0]
        assert 'Continue grading without rewriting key.md' not in region
    assert SyntheticSummary.calls
    assert result["cc_session_id"] == "destination-session"
    assert result["parent_id"] == current["id"]
    followup = await db.add_message(conv["id"], "user", "Next exam", parent_id=result["id"])
    await run(conv, followup["id"], target)
    assert len(calls) == 2
    assert calls[1][1].get("resume_session_id") == "destination-session"
    assert "old detail" not in calls[1][0]
    assert "context_scope:" in calls[1][0]
    assert calls[1][0].split("context_scope:", 1)[1].split()[0] != calls[0][0].split("context_scope:", 1)[1].split()[0]
    assert len(SyntheticSummary.calls) == 1


@pytest.mark.parametrize("harness", MODELS)
async def test_every_native_handler_skips_shared_replay(monkeypatch, tmp_database, tmp_path, harness):
    conv, current, calls = await setup(monkeypatch, tmp_path, harness, harness)
    await run(conv, current["id"], harness)
    assert len(calls) == 1
    assert calls[0][1].get("resume_session_id") == "source-session"
    assert "old detail" not in calls[0][0]
    assert not SyntheticSummary.calls


@pytest.mark.parametrize("harness", MODELS)
@pytest.mark.parametrize("tool_ran", [False, True])
async def test_native_context_error_recovers_once_at_recorded_tool_boundary(
        monkeypatch, tmp_database, tmp_path, harness, tool_ran):
    import server
    conv, current, calls = await setup(monkeypatch, tmp_path, harness, harness)
    client, method = {
        "claude": (server.claude_client, "run_claude"),
        "codex": (server.codex_client, "run_codex"),
        "gemini": (server.gemini_client, "run_gemini"),
        "goose": (server.goose_client, "run_goose"),
    }[harness]
    normal = getattr(client, method)

    async def launch(prompt, *args, **kwargs):
        proc, stream = await normal(prompt, *args, **kwargs)
        if len(calls) > 1:
            return proc, stream
        async def failed():
            yield {"type": "session_info", "session_id": "exhausted-session", "model": MODELS[harness]}
            if tool_ran:
                yield {"type": "tool_start", "tool_id": "counted-write", "name": "Write"}
                yield {"type": "tool_result", "tool_id": "counted-write", "content": "saved"}
            yield {"type": "result", "session_id": "exhausted-session", "is_error": True,
                   "terminal_confirmed": True, "error": "context window exceeded",
                   "result_text": "context window exceeded"}
        return proc, failed()
    monkeypatch.setattr(client, method, launch)
    result = await run(conv, current["id"], harness)
    assert len(calls) == 2
    assert calls[-1][1].get("resume_session_id") is None
    assert "KEEP_KEY_PRIVATE" in calls[-1][0]
    assert result["cc_session_id"] == "destination-session"
    if tool_ran:
        import json
        tools = [b for b in json.loads(result["content_blocks"]) if b["type"] == "tool_use"]
        assert len(tools) == 1 and tools[0]["outcome_recorded"] is True
        assert tools[0]["result"] == "saved"
