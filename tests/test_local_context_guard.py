import asyncio
import copy
import json

import httpx
import pytest

from local_context_guard import LocalContextGuard, SUMMARY_INSTRUCTIONS, SUMMARY_NOTICE
from replay_budget import ReplayBudgetExceeded


def guarded_backend(capacity=10000, summary_finish="stop"):
    inference = []

    def serve(request):
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        if path == "/props":
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": capacity}})
        if path == "/apply-template":
            return httpx.Response(200, json={"prompt": json.dumps(body.get("messages", [])) + json.dumps(body.get("tools", []))})
        if path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1] * len(body["content"])})
        if path == "/v1/messages/count_tokens":
            count = len(json.dumps(body.get("messages", []))) + len(json.dumps(body.get("system", ""))) + len(json.dumps(body.get("tools", [])))
            return httpx.Response(200, json={"input_tokens": count})
        if path in {"/v1/chat/completions", "/v1/messages"}:
            count = len(json.dumps(body.get("messages", []))) + len(json.dumps(body.get("tools", [])))
            assert count + body.get("max_tokens", 0) < capacity
            inference.append(body)
            summary = body["messages"][0].get("content") == SUMMARY_INSTRUCTIONS
            if summary:
                assert not body.get("tools") and body["stream"] is False
            return httpx.Response(200, json={"model": body.get("model"), "choices": [{
                "finish_reason": summary_finish if summary else "stop",
                "message": {"content": "Constraint: review only. Prior tool succeeded. Next: verify."},
            }]})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
    return LocalContextGuard("http://local-test", client=client), inference


@pytest.mark.parametrize("anthropic", [False, True])
async def test_old_history_compacts_on_same_backend_and_keeps_current_turn(anthropic):
    guard, calls = guarded_backend()
    payload = {"model": "local.gguf", "max_tokens": 500, "stream": True, "messages": [
        {"role": "user", "content": "old record " * 1500},
        {"role": "assistant", "content": "Old outcome"},
        {"role": "user", "content": "Keep this request verbatim"},
    ]}
    if anthropic:
        payload["system"] = "Loom contract: preserve permissions"
        payload["tools"] = [{"name": "Read", "input_schema": {"type": "object"}}]
    else:
        payload["messages"].insert(0, {"role": "system", "content": "Loom contract: preserve permissions"})
        payload["tools"] = [{"type": "function", "function": {"name": "Read", "parameters": {"type": "object"}}}]
    original = copy.deepcopy(payload)
    async with guard.client:
        compacted = await guard.prepare(payload, anthropic=anthropic)
    assert payload == original
    assert compacted["messages"][-1] == original["messages"][-1]
    assert compacted["tools"] == original["tools"]
    assert compacted.get("system") == original.get("system")
    assert calls and all(call["model"] == "local.gguf" for call in calls)
    assert "Original history preserved" in list(guard.events._queue)[-1]["text"]


@pytest.mark.parametrize("anthropic", [False, True])
async def test_single_long_tool_loop_compacts_results_without_replaying_tools(anthropic):
    guard, calls = guarded_backend()
    call = {"role": "assistant", "tool_calls": [{"id": "read-2", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]}
    result = {"role": "tool", "tool_call_id": "read-2", "content": "result " * 2500}
    if anthropic:
        call = {"role": "assistant", "content": [{"type": "tool_use", "id": "read-2", "name": "Read", "input": {}}]}
        result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "read-2", "is_error": True, "content": "result " * 2500}]}
    payload = {"model": "local.gguf", "max_tokens": 500, "messages": [
        {"role": "user", "content": "Current task: do not change code"}, call, result,
    ]}
    original = copy.deepcopy(payload)
    async with guard.client:
        compacted = await guard.prepare(payload, anthropic=anthropic)
    assert payload == original
    assert compacted["messages"][:2] == original["messages"][:2]
    captured = compacted["messages"][-1]
    captured = captured["content"][0] if anthropic else captured
    assert captured.get("tool_use_id", captured.get("tool_call_id")) == "read-2"
    if anthropic:
        assert captured["is_error"] is True
    assert captured["content"].startswith(SUMMARY_NOTICE)
    assert all(not c.get("tools") for c in calls)


@pytest.mark.parametrize("blocks", [False, True])
async def test_fresh_cross_harness_replay_retains_contract_and_latest_request(blocks):
    guard, _ = guarded_backend()
    text = "CONTRACT\n<conversation_history>" + "history " * 1800 + "</conversation_history>\nCURRENT REQUEST"
    guard.replay_history = "<conversation_history>" + "history " * 1800 + "</conversation_history>"
    payload = {"model": "local.gguf", "max_tokens": 500, "messages": [{
        "role": "user", "content": [{"type": "text", "text": text}] if blocks else text,
    }]}
    async with guard.client:
        compacted = await guard.prepare(payload)
    text = compacted["messages"][0]["content"]
    text = text[0]["text"] if blocks else text
    assert text.startswith("CONTRACT\n<conversation_history>")
    assert text.endswith("</conversation_history>\nCURRENT REQUEST")
    assert SUMMARY_NOTICE in text


async def test_oversized_current_instruction_fails_before_inference():
    guard, calls = guarded_backend()
    async with guard.client:
        with pytest.raises(ReplayBudgetExceeded, match="Current instructions/user input/tool calls were preserved"):
            await guard.prepare({"max_tokens": 500, "messages": [{"role": "user", "content": "new task " * 2000}]})
    assert not calls


@pytest.mark.parametrize("anthropic,output_key", [(False, "max_tokens"), (False, "max_completion_tokens"), (True, "max_tokens")])
@pytest.mark.parametrize("capacity,reserve,input_size", [(100096, 32000, 59000), (24576, 32000, 12000)])
async def test_native_output_ceiling_can_yield_to_exact_input(anthropic, output_key, capacity, reserve, input_size):
    guard, calls = guarded_backend(capacity)
    payload = {"model": "local.gguf", output_key: reserve,
               "messages": [{"role": "user", "content": "x" * input_size}],
               "tools": [{"name": "synthetic_tool", "description": "Keep this tool"}]}
    original = copy.deepcopy(payload)
    async with guard.client:
        prepared = await guard.prepare(payload, anthropic=anthropic)
        count = await guard.count(prepared, anthropic)
    assert payload == original
    assert prepared["messages"] == original["messages"] and prepared["tools"] == original["tools"]
    assert 8192 <= prepared[output_key] < reserve
    assert count + prepared[output_key] + max(256, capacity // 10) <= capacity
    assert any("output ceiling adjusted" in e["text"] for e in guard.events._queue)
    assert not calls  # no inference or summary required for this current input


@pytest.mark.parametrize("thinking", [None, {"type": "enabled", "budget_tokens": 16000}])
async def test_output_ceiling_never_starves_minimum_or_thinking(thinking):
    guard, calls = guarded_backend(24576)
    payload = {"max_tokens": 32000, "messages": [{"role": "user", "content": "x" * (15000 if thinking is None else 12000)}]}
    if thinking:
        payload["thinking"] = thinking
    async with guard.client:
        with pytest.raises(ReplayBudgetExceeded):
            await guard.prepare(payload)
    assert payload["max_tokens"] == 32000 and not calls


async def test_output_ceiling_negotiation_precedes_lossy_history_summary():
    guard, calls = guarded_backend(100096)
    payload = {"max_tokens": 32000, "messages": [
        {"role": "user", "content": "old exact evidence " * 4000},
        {"role": "assistant", "content": "Recorded correction, not source code"},
        {"role": "user", "content": "Continue the project"},
    ]}
    async with guard.client:
        prepared = await guard.prepare(payload)
    assert prepared["messages"] == payload["messages"]
    assert prepared["max_tokens"] < 32000 and not calls
    assert not any("Compacting" in e["text"] for e in guard.events._queue)


@pytest.mark.parametrize("anthropic", [False, True])
async def test_shared_plan_region_is_compactable_without_touching_current_input(anthropic):
    from continuity_engine import BranchSnapshot, ContextEngine, NativeCapability
    from tests.test_continuity_engine import MemoryStore, branch, size
    records = branch('codex', 1500)
    # Simulate a generous early estimate; actual destination admission remains
    # strict after native instructions and tools are assembled.
    plan = await ContextEngine(store=MemoryStore(), summarizer=None, measure=size).prepare(
        BranchSnapshot.from_messages(records), NativeCapability(None, 'provider_boundary'),
        input_budget=100000, retrieval_notice='Originals remain retrievable')
    assert plan.replay_region in plan.prompt
    assert records[-1]['content'] not in plan.replay_region
    guard, calls = guarded_backend()
    guard.replay_history = plan.replay_region
    payload = {'model': 'local.gguf', 'max_tokens': 500, 'messages': [
        {'role': 'user', 'content': plan.prompt}], 'tools': []}
    async with guard.client:
        compacted = await guard.prepare(payload, anthropic=anthropic)
    assert calls
    assert compacted['messages'][0]['content'].endswith(records[-1]['content'])
    assert 'Originals remain retrievable' in compacted['messages'][0]['content']
    assert payload['messages'][0]['content'] == plan.prompt


async def test_user_supplied_history_tags_do_not_authorize_compaction():
    guard, calls = guarded_backend()
    async with guard.client:
        with pytest.raises(ReplayBudgetExceeded):
            await guard.prepare({"max_tokens": 500, "messages": [{
                "role": "user", "content": "<conversation_history>" + "new task " * 2000 + "</conversation_history>",
            }]})
    assert not calls


def test_steering_between_call_and_result_does_not_split_the_exchange():
    from local_context_guard import _history_boundary
    messages = [
        {"role": "user", "content": "original"},
        {"role": "assistant", "tool_calls": [{"id": "call-1"}]},
        {"role": "user", "content": "additional user constraint"},
        {"role": "tool", "tool_call_id": "call-1", "content": "done"},
    ]
    assert _history_boundary(messages) == 0
    messages.append({"role": "user", "content": "next complete turn"})
    assert _history_boundary(messages) == 4


def test_server_trusted_region_keeps_tags_in_latest_user_input_outside_summary():
    import server
    current = "Please review <conversation_history>this example</conversation_history> exactly"
    branch = [{"role": "assistant", "content": "old result"}, {"role": "user", "content": current}]
    region = server._local_replay_region(branch)
    assert region == "<conversation_history>\nAssistant: old result\n</conversation_history>"
    assert current not in region


async def test_failed_summary_never_sends_execution_request_or_changes_original():
    guard, calls = guarded_backend(summary_finish="length")
    payload = {"model": "local.gguf", "max_tokens": 500, "messages": [
        {"role": "user", "content": "old " * 4000},
        {"role": "user", "content": "latest"},
    ]}
    original = copy.deepcopy(payload)
    async with guard.client:
        with pytest.raises(ReplayBudgetExceeded, match="did not finish"):
            await guard.prepare(payload)
    assert len(calls) == 1 and not calls[0].get("tools")
    assert payload == original


async def test_repeated_native_turns_and_long_tool_loops_stay_within_budget():
    guard, calls = guarded_backend(capacity=10000)
    history = [{"role": "system", "content": "Preserve workspace and approvals"}]
    async with guard.client:
        for turn in range(8):
            question = {"role": "user", "content": f"Current task {turn}: preserve this exactly"}
            call = {"role": "assistant", "tool_calls": [{"id": f"read-{turn}", "type": "function",
                    "function": {"name": "Read", "arguments": "{}"}}]}
            result = {"role": "tool", "tool_call_id": f"read-{turn}", "content": "large result " * 1300}
            history.extend([question, call, result])
            payload = {"model": "local.gguf", "max_tokens": 500, "messages": copy.deepcopy(history)}
            before = copy.deepcopy(payload)
            admitted = await guard.prepare(payload)
            assert payload == before  # Native history is not destructively rewritten.
            assert question in admitted["messages"] and call in admitted["messages"]
            assert admitted["messages"][-1]["tool_call_id"] == f"read-{turn}"
            assert await guard.count(admitted) + 500 + 1000 <= 10000
    assert calls and all(c["model"] == "local.gguf" and not c.get("tools") for c in calls)


async def test_authenticated_proxy_admits_only_bounded_requests():
    guard, calls = guarded_backend()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=guard.app), base_url="http://guard")
    payload = {"model": "local.gguf", "max_tokens": 500, "messages": [
        {"role": "user", "content": "old " * 4000}, {"role": "user", "content": "latest"},
    ]}
    async with client, guard.client:
        assert (await client.post("/v1/chat/completions", json=payload)).status_code == 401
        headers = {"Authorization": f"Bearer {guard.secret}"}
        assert (await client.post("/shutdown", headers=headers)).status_code == 404
        response = await client.post("/v1/chat/completions", json=payload, headers=headers)
        assert response.status_code == 200
    assert len(calls) > 1
    assert calls[-1]["messages"][-1]["content"] == "latest"


async def test_guard_close_stops_only_its_own_listener():
    guard, _ = guarded_backend()
    base = await guard.start()
    async with httpx.AsyncClient(trust_env=False) as client:
        assert (await client.get(base + "/v1/models")).status_code == 401
    await guard.close()
    assert guard._task.done()
    assert guard._socket.fileno() == -1
    await guard.close()  # Cancellation and stream cleanup may both close it.


async def test_cancelled_stream_closes_guard_and_pending_generator():
    guard, _ = guarded_backend()
    closed = asyncio.Event()

    async def source():
        try:
            yield {"type": "text_delta", "text": "partial"}
            await asyncio.Event().wait()
        finally:
            closed.set()

    stream = guard.wrap_stream(source())
    assert (await anext(stream))["text"] == "partial"
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert closed.is_set() and guard._closed


async def test_goose_local_launch_uses_live_capacity_and_cleans_failed_spawn(tmp_path, monkeypatch):
    import goose_client
    import local_context_guard
    guard, _ = guarded_backend(capacity=32768)
    monkeypatch.setattr(local_context_guard, "LocalContextGuard", lambda *a, **kw: guard)
    monkeypatch.setenv("GOOSE_CONTEXT_LIMIT", "999999")
    captured = {}

    async def fail_spawn(*args, **kwargs):
        captured.update(kwargs["env"])
        raise RuntimeError("synthetic spawn failure")

    monkeypatch.setattr(goose_client.asyncio, "create_subprocess_exec", fail_spawn)
    with pytest.raises(RuntimeError, match="synthetic spawn failure"):
        await goose_client.run_goose("test", cwd=str(tmp_path), model="goose:llama:local.gguf", goose_exe="fixture-goose")
    assert captured["GOOSE_CONTEXT_LIMIT"] == "32768"
    assert captured["OPENAI_HOST"].startswith("http://127.0.0.1:")
    assert captured["OPENAI_API_KEY"] == guard.secret
    assert captured["GOOSE_FAST_MODEL"] == "local.gguf"
    assert captured["GOOSE_MODE"] == "approve"
    assert guard._closed and guard._task.done()


async def test_guard_emits_status_on_compaction_request():
    from local_context_guard import _is_harness_compaction_request
    assert _is_harness_compaction_request({
        "messages": [
            {"role": "system", "content": "## Task Context\n- An llm context limit was reached"},
            {"role": "user", "content": "Please summarize the conversation history"},
        ]
    })
    assert _is_harness_compaction_request({
        "system": "Distill the conversation below into a structured summary",
        "messages": [],
    })
    assert not _is_harness_compaction_request({
        "messages": [{"role": "user", "content": "Hello world"}],
    })

    guard, _ = guarded_backend()
    transport = httpx.ASGITransport(app=guard.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {guard.secret}"},
            json={
                "model": "local.gguf",
                "messages": [
                    {"role": "system", "content": "## Task Context\n- An llm context limit was reached"},
                    {"role": "user", "content": "Please summarize the conversation history"},
                ],
            },
        )
        assert resp.status_code == 200
        events = list(guard.events._queue)
        assert any("Compacting conversation context" in e.get("text", "") for e in events)
        assert any("Conversation context compaction completed" in e.get("text", "") for e in events)


def test_strip_nul_bytes_recursively_cleans_structures():
    from local_context_guard import _strip_nul_bytes

    dirty = {
        "text": "Raspberry Pi 3 Model B\x00\n4",
        "nested": [{"val": "sub\x00string"}, "clean\x00"],
        "num": 42,
        "bool": True,
    }
    cleaned = _strip_nul_bytes(dirty)
    assert cleaned == {
        "text": "Raspberry Pi 3 Model B\n4",
        "nested": [{"val": "substring"}, "clean"],
        "num": 42,
        "bool": True,
    }


@pytest.mark.asyncio
async def test_guard_strips_nul_bytes_before_upstream():
    from local_context_guard import LocalContextGuard

    guard, calls = guarded_backend()
    transport = httpx.ASGITransport(app=guard.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {guard.secret}"},
            json={
                "model": "local.gguf",
                "messages": [
                    {"role": "user", "content": "device-tree: Raspberry Pi 3\x00 model\x00"},
                ],
            },
        )
        assert resp.status_code == 200
        assert len(calls) == 1
        serialized = json.dumps(calls[0])
        assert "\x00" not in serialized
        assert "device-tree: Raspberry Pi 3 model" in serialized
