"""Opt-in synthetic overflow admission probe against the configured llama-server.

Uses only synthetic records and the already loaded local model. Never starts,
stops, reloads, or configures a server; never executes tools or changes a tree.
"""

import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import config
from local_context_guard import LocalContextGuard


async def probe():
    guard = LocalContextGuard(config.llama_host_url())
    try:
        model = config.llama_model
        capacity = await guard.capacity(model)
        record = "Synthetic read result: the task is review only; no edits were approved. Record unchanged.\n"
        payload = {
            "model": model, "stream": False, "temperature": 0, "max_tokens": 2048,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {"role": "system", "content": "This is an isolated read-only diagnostic. Never call tools."},
                {"role": "user", "content": "After the completed read below, reply exactly CONTEXT_GUARD_OK. Do not call tools."},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "synthetic-read", "type": "function",
                    "function": {"name": "Read", "arguments": '{"file_path":"synthetic-record.txt"}'},
                }]},
                {"role": "tool", "tool_call_id": "synthetic-read", "content": record * 1000},
            ],
        }
        count = await guard.count(payload)
        payload["messages"][-1]["content"] = record * (int(1000 * capacity * 1.1 / count) + 1)
        before = await guard.count(payload)
        assert before > capacity, (before, capacity)
        compacted = await guard.prepare(payload)
        after = await guard.count(compacted)
        response = await guard.client.post(guard.upstream + "/v1/chat/completions", json=compacted)
        response.raise_for_status()
        answer = response.json()["choices"][0]["message"].get("content", "")
        events = list(guard.events._queue)
        result = {
            "capacity": capacity, "before_tokens": before, "after_tokens": after,
            "summary_passes": sum("summarizing older" in e["text"] for e in events),
            "original_tool_id_preserved": compacted["messages"][-1]["tool_call_id"] == "synthetic-read",
            "answer_ok": answer.strip() == "CONTEXT_GUARD_OK",
            "tools_executed": 0,
        }
        print(json.dumps(result, indent=2))
        if not result["answer_ok"]:
            raise RuntimeError("Local model did not return the expected diagnostic acknowledgement")
    finally:
        await guard.close()


async def native_goose_probe():
    import goose_client
    # This isolated diagnostic exposes no MCP servers or built-in tools.
    goose_client._loom_mcp_servers = lambda *args, **kwargs: []
    cwd = tempfile.mkdtemp(prefix="loom-goose-context-probe-")
    session_id = None
    responses = []
    for prompt in (
        "Remember the marker BLUE_FRAME_93. Reply exactly INITIAL_OK. Do not call tools.",
        "What was the marker? Reply with the marker only. Do not call tools.",
    ):
        proc, stream = await goose_client.run_goose(
            prompt, cwd=cwd, model="goose:llama:" + config.llama_model,
            goose_exe=config.goose_exe or None, builtins=[], is_first_turn=False,
            resume_session_id=session_id, fork_session=bool(session_id),
        )
        text = ""
        try:
            async with asyncio.timeout(180):
                async for event in stream:
                    if event.get("type") == "text_delta":
                        text += event.get("text", "")
                    if event.get("session_id"):
                        session_id = event["session_id"]
                    if event.get("type") == "error":
                        raise RuntimeError(event.get("error") or event.get("message"))
        finally:
            await goose_client.cancel_goose(proc)
            await stream.aclose()
        responses.append(text.strip())
    result = {"native_goose_fresh_ok": responses[0] == "INITIAL_OK",
              "native_goose_fork_retains_context": responses[1] == "BLUE_FRAME_93",
              "tools_exposed": 0, "synthetic_session_id": session_id}
    print(json.dumps(result, indent=2))
    if not result["native_goose_fresh_ok"] or not result["native_goose_fork_retains_context"]:
        raise RuntimeError("Native Goose diagnostic failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True, help="Use synthetic records on the configured local model")
    parser.add_argument("--native-goose", action="store_true", help="Check native Goose fresh/fork continuity without exposing tools")
    args = parser.parse_args()
    asyncio.run(native_goose_probe() if args.native_goose else probe())
