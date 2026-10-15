"""Sanitized native MCP approval reproduction; no display-title classification."""
import copy
import asyncio
import json

import pytest

import codex_client as codex


def wire():
    item = {"id":"tool-1", "threadId":"thread-1", "turnId":"turn-1", "server":"fixture",
            "tool":"inspect_action", "arguments":{"nonce":"synthetic", "operation":"inspect"}}
    params = {"threadId":"thread-1", "turnId":"turn-1", "serverName":"fixture", "mode":"form",
              "_meta":{"codex_approval_kind":"mcp_tool_call", "persist":["session","always"],
                       "tool_params":dict(item["arguments"])},
              "message":"Untrusted display title says some_other_tool", "requestedSchema":{"type":"object", "properties":{}}}
    return params, {"tool-1":item}


def test_real_mcp_approval_joins_exact_protocol_item_and_maps_one_time_answer():
    params, items = wire()
    assert codex._is_mcp_tool_approval(params)
    enriched = codex._mcp_approval_params(params, items)
    assert codex._app_permission_payload("mcpServer/elicitation/request", enriched) == (
        "mcp__fixture__inspect_action", {"nonce":"synthetic", "operation":"inspect"})
    assert "_loom_mcp_item" not in params
    assert codex._app_approval_response("mcpServer/elicitation/request", True, True, enriched) == {"action":"accept", "content":{}}
    assert codex._app_approval_response("mcpServer/elicitation/request", False, False, enriched) == {"action":"decline", "content":None}


@pytest.mark.parametrize("field,value", [("threadId","other"), ("turnId","other"),
                                        ("serverName","other"), ("_meta",{"tool_params":{}})])
def test_wrong_owner_or_arguments_cannot_borrow_tool_identity(field, value):
    params, items = wire()
    params[field] = value
    params["_loom_mcp_item"] = items["tool-1"]  # even injected metadata is stripped
    enriched = codex._mcp_approval_params(params, items)
    assert codex._app_permission_payload("mcpServer/elicitation/request", enriched)[0] == "MCPToolApproval"


def test_ambiguous_parallel_tool_calls_are_not_guessed():
    params, items = wire()
    items["tool-2"] = {**items["tool-1"], "id":"tool-2", "tool":"other_action"}
    assert "_loom_mcp_item" not in codex._mcp_approval_params(params, items)


@pytest.mark.parametrize("field,item_field", [("threadId", "threadId"), ("turnId", "turnId"), ("serverName", "server")])
def test_missing_owner_on_both_events_is_not_identity_evidence(field, item_field):
    params, items = wire()
    params.pop(field)
    items["tool-1"].pop(item_field)
    assert "_loom_mcp_item" not in codex._mcp_approval_params(params, items)


@pytest.mark.parametrize("change", [{"mode":"url"}, {"_meta":{}},
    {"requestedSchema":{"type":"object", "properties":{"password":{"type":"string"}}}}])
def test_arbitrary_forms_are_not_converted_to_yes_no_approval(change):
    params, _ = wire()
    params.update(copy.deepcopy(change))
    assert not codex._is_mcp_tool_approval(params)


@pytest.mark.parametrize("allow,arbitrary_form", [(True, False), (False, False), (False, True)])
async def test_app_server_mcp_request_reaches_bridge_and_answers_native_protocol(tmp_path, monkeypatch, allow, arbitrary_form):
    params, items = wire()
    if arbitrary_form:
        params["_meta"] = {}
    calls, answers = [], []
    class Input:
        closed = False
        def is_closing(self): return self.closed
        def close(self): self.closed = True
        def write(self, raw):
            data = json.loads(raw)
            rid, method = data.get("id"), data.get("method")
            if rid is None: return
            if method == "initialize": result = {"userAgent":"test"}
            elif method == "config/read": result = {"config":{}}
            elif method == "thread/start":
                result = {"thread":{"id":"thread-1"}, "model":"gpt-5.6-luna", "modelProvider":"openai"}
            elif method == "turn/start": result = {"turn":{"id":"turn-1"}}
            elif rid == "fixture-approval":
                answers.append(data.get("result"))
                proc.feed({"method":"turn/completed", "params":{"threadId":"thread-1",
                           "turn":{"id":"turn-1", "status":"completed", "items":[]}}})
                return
            else: result = {}
            proc.feed({"id":rid, "result":result})
            if method == "turn/start":
                proc.feed({"method":"item/started", "params":{"threadId":"thread-1", "turnId":"turn-1",
                           "item":{**items["tool-1"], "type":"mcpToolCall"}}})
                proc.feed({"id":"fixture-approval", "method":"mcpServer/elicitation/request", "params":params})
        async def drain(self): await asyncio.sleep(0)
    class Process:
        pid = 987654321
        returncode = None
        def __init__(self):
            self.stdin = Input()
            self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        def feed(self, event): self.stdout.feed_data((json.dumps(event) + "\n").encode())
        def terminate(self):
            self.returncode = 0
            self.stdout.feed_eof()
            self.stderr.feed_eof()
        kill = terminate
        async def wait(self): return self.returncode
    proc = Process()
    async def spawn(*args, **kwargs): return proc
    async def permission(payload):
        calls.append(payload)
        return {"allow":allow, "decision_source":"user"}
    monkeypatch.setattr(codex.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(codex, "_find_codex_exe", lambda: "fixture-codex")
    monkeypatch.setattr(codex, "_thread_mcp_servers", lambda *a, **k: {})
    monkeypatch.setattr(codex.sys, "platform", "linux")
    _, stream = await codex.run_codex("synthetic task", str(tmp_path), model="gpt-5.6-luna",
                                     conv_id=17, permission_scope="agent:fixture",
                                     permission_request_handler=permission)
    async with asyncio.timeout(5):
        events = [event async for event in stream]
    if arbitrary_form:
        assert not calls
        assert answers == [{"action":"cancel", "content":None}]
        assert any("not denied by the user" in str(event) for event in events)
    else:
        assert len(calls) == 1
        assert calls[0]["tool_name"] == "mcp__fixture__inspect_action"
        assert calls[0]["tool_input"] == items["tool-1"]["arguments"]
        assert calls[0]["permission_scope"] == "agent:fixture"
        assert answers == [{"action":"accept" if allow else "decline", "content":{} if allow else None}]
