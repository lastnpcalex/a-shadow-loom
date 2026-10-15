"""Controlled fake provider processes for the production binding tests.

Launched by the binding test's spawn interceptor as:
    python fake_provider.py HARNESS EVIDENCE_PATH [original provider argv...]

This is a real subprocess speaking the provider's real wire protocol over
real stdio pipes. Loom's production client adapters parse its output through
their normal parsers — nothing here fabricates a Loom event. The fake records
its own launch evidence (argv, cwd, child env, received protocol payloads,
issued session ids) as JSON lines on EVIDENCE_PATH; the binding tests read
that recorded evidence as their oracle.

Env overrides (set by the test, propagated through the real launch env):
  LOOM_FAKE_REPORT_MODEL   report this effective model instead of the launch model
  LOOM_FAKE_AGENT_CAPABILITIES  goose: JSON agentCapabilities in the initialize result
"""
import json
import os
import sys
import time
import uuid

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


def record(evidence, **fields):
    fields.setdefault("ts", time.time())
    with open(evidence, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(fields, ensure_ascii=False) + "\n")


def argvalue(argv, flag):
    for index, token in enumerate(argv):
        if token == flag and index + 1 < len(argv):
            return argv[index + 1]
    return None


def emit(line):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def reply(rid, result):
    emit(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}))


def reply_error(rid, message):
    emit(json.dumps({"jsonrpc": "2.0", "id": rid,
                     "error": {"code": -32000, "message": message}}))


def issued_sessions(evidence):
    sessions = set()
    if not os.path.exists(evidence):
        return sessions
    with open(evidence, encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") == "session_issued":
                sessions.add(row.get("session_id"))
    return sessions


def main():
    harness, evidence = sys.argv[1], sys.argv[2]
    argv = sys.argv[3:]
    env = os.environ
    prep = env.get("LOOM_PREPARATION") == "1"
    launch_env = {k: v for k, v in env.items()
                  if k.startswith(("LOOM_", "GOOSE_", "ANTHROPIC_"))}
    record(evidence, event="launch", harness=harness, argv=argv,
           cwd=os.getcwd(), preparation=prep, env=launch_env)
    report_model = env.get("LOOM_FAKE_REPORT_MODEL", "")
    text = json.dumps(VALID_STATE) if prep else (
        "Working turn complete: continued from the recorded state without repeating effects.")

    if harness == "claude":
        prompt = argvalue(argv, "-p")
        if prompt == "-":
            prompt = sys.stdin.read()
        model = argvalue(argv, "--model") or ""
        resume = argvalue(argv, "--resume")
        sessions = issued_sessions(evidence)
        sid = resume or f"cc-{uuid.uuid4().hex[:12]}"
        if resume:
            record(evidence, event="resume_requested", session_id=resume)
            if resume not in sessions:
                emit(json.dumps({"type": "result", "subtype": "error_during_execution",
                                 "session_id": resume, "is_error": True,
                                 "result": "", "error": "unknown session"}))
                return
        record(evidence, event="prompt_received", chars=len(prompt or ""))
        session_issued = resume is None
        reported = report_model or model
        emit(json.dumps({"type": "system", "subtype": "init",
                         "session_id": sid, "model": reported,
                         "cwd": os.getcwd(), "tools": [], "mcp_servers": []}))
        emit(json.dumps({"type": "stream_event", "event": {
            "type": "message_start",
            "message": {"id": "msg_1", "model": reported,
                        "usage": {"input_tokens": 10, "output_tokens": 1}}}}))
        emit(json.dumps({"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""}}}))
        emit(json.dumps({"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": text}}}))
        emit(json.dumps({"type": "stream_event", "event": {
            "type": "content_block_stop", "index": 0}}))
        emit(json.dumps({"type": "stream_event", "event": {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"input_tokens": 10, "output_tokens": 20}}}))
        emit(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                         "session_id": sid, "result": text,
                         "num_turns": 1, "duration_ms": 100,
                         "usage": {"input_tokens": 10, "output_tokens": 20}}))
        if session_issued:
            record(evidence, event="session_issued", session_id=sid, model=reported)
        return

    if harness == "codex":
        sessions = issued_sessions(evidence)
        thread_id = None
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            rid = message.get("id")
            method = message.get("method")
            params = message.get("params") or {}
            if rid is None or method is None:
                continue  # client notification
            if method == "initialize":
                reply(rid, {"userAgent": "fake-codex/1", "capabilities": {}})
            elif method == "config/read":
                reply(rid, {"config": {}})
            elif method in ("thread/start", "thread/resume", "thread/fork"):
                if method == "thread/start":
                    thread_id = f"cx-{uuid.uuid4().hex[:12]}"
                else:
                    thread_id = str(params.get("threadId") or "")
                    record(evidence, event="resume_requested", session_id=thread_id)
                    if thread_id not in sessions:
                        reply_error(rid, "unknown thread")
                        continue
                record(evidence, event="thread_start", method=method, params=params,
                       thread_id=thread_id)
                if method == "thread/start":
                    record(evidence, event="session_issued", session_id=thread_id,
                           model=params.get("model"))
                reply(rid, {"thread": {"id": thread_id},
                            "model": report_model or params.get("model"),
                            "modelProvider": "fake-openai"})
            elif method == "turn/start":
                record(evidence, event="turn_start", params=params)
                reply(rid, {"turn": {"id": "turn-1"}})
                emit(json.dumps({"method": "turn/completed", "params": {
                    "threadId": thread_id,
                    "turn": {"id": "turn-1",
                             "items": [{"type": "agentMessage", "text": text}]}}}))
            else:
                reply(rid, {})
        return

    if harness == "gemini":
        prompt = argvalue(argv, "-p")
        if prompt and os.path.isfile(prompt):
            with open(prompt, encoding="utf-8", errors="replace") as handle:
                prompt = handle.read()
        model = argvalue(argv, "--model") or ""
        resume = argvalue(argv, "--conversation")
        sessions = issued_sessions(evidence)
        sid = resume or f"agy-{uuid.uuid4().hex[:12]}"
        if resume:
            record(evidence, event="resume_requested", session_id=resume)
            if resume not in sessions:
                emit(json.dumps({"event": "result", "result": {
                    "response": "", "status": "error",
                    "error": "unknown conversation"}}))
                return
        record(evidence, event="prompt_received", chars=len(prompt or ""))
        reported = report_model or model
        emit(json.dumps({"event": "init", "conversation_id": sid, "model": reported}))
        emit(json.dumps({"event": "result", "result": {
            "response": text, "status": "ok", "duration_seconds": 0.2,
            "num_turns": 1}}))
        if resume is None:
            record(evidence, event="session_issued", session_id=sid, model=reported)
        return

    if harness == "goose":
        sessions = issued_sessions(evidence)
        capabilities = {}
        raw_caps = env.get("LOOM_FAKE_AGENT_CAPABILITIES")
        if raw_caps:
            try:
                capabilities = json.loads(raw_caps)
            except json.JSONDecodeError:
                capabilities = {}
        sid = None
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            rid = message.get("id")
            method = message.get("method")
            params = message.get("params") or {}
            if method == "initialize":
                record(evidence, event="initialize")
                reply(rid, {"protocolVersion": "v1",
                            "agentInfo": {"name": "fake-goose", "version": "1"},
                            "agentCapabilities": capabilities})
            elif method in ("session/new", "session/load", "session/fork"):
                if method == "session/new":
                    sid = f"gse-{uuid.uuid4().hex[:12]}"
                    record(evidence, event="session_new", params=params)
                    record(evidence, event="session_issued", session_id=sid)
                else:
                    sid = str(params.get("sessionId") or "")
                    record(evidence, event="resume_requested", session_id=sid)
                    if sid not in sessions:
                        reply_error(rid, "unknown session")
                        continue
                model_id = env.get("GOOSE_MODEL", "")
                if model_id and not model_id.startswith("openrouter:"):
                    model_id = "openrouter:" + model_id
                reply(rid, {"sessionId": sid,
                            "models": {"currentModelId": report_model or model_id
                                       or "openrouter:fake-model"}})
            elif method == "session/prompt":
                record(evidence, event="session_prompt", params=params)
                emit(json.dumps({"jsonrpc": "2.0", "method": "session/update",
                                 "params": {"sessionId": sid, "update": {
                                     "sessionUpdate": "agentMessageChunk",
                                     "content": {"type": "text", "text": text}}}}))
                reply(rid, {"stopReason": "end_turn"})
            elif rid is not None:
                reply(rid, {})
        return

    raise SystemExit(f"unknown harness {harness}")


if __name__ == "__main__":
    main()
