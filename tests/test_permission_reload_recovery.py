"""Exercise reconnect retries through the production chat action loop."""

import json
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketDisconnect

import database as db
import server
from event_router import SubscriptionRouter


@pytest.mark.parametrize("action", ["subscribe_run", "request_snapshot", "ping"])
@pytest.mark.parametrize("position", ["draft", "sibling", "descendant"])
async def test_reload_retries_permission_after_old_socket_disconnect(
    tmp_database, monkeypatch, action, position,
):
    conv = await db.create_conversation("Reload approvals", mode="codex")
    prompt = await db.add_message(conv["id"], "user", "work")
    draft = await db.add_message(conv["id"], "assistant", "working", parent_id=prompt["id"])
    sibling = await db.add_message(conv["id"], "user", "other")
    descendant = await db.add_message(conv["id"], "user", "later", parent_id=draft["id"])
    leaf = {"draft": draft, "sibling": sibling, "descendant": descendant}[position]
    await db.select_loom_cursor(conv["id"], "replacement", leaf["loom_thread_id"], leaf["id"])
    await db.save_pending_permission(
        "reload-request", conv["id"], "Bash", {"command": "git status"}, "git status",
        run_id="gen:994", parent_id=prompt["id"], draft_msg_id=draft["id"],
        client_instance_id="old-page", permission_scope="gen:994",
    )
    key = (conv["id"], prompt["id"], 994)
    monkeypatch.setattr(server, "_active_generations", {key: SimpleNamespace(done=lambda: False)})
    monkeypatch.setattr(server, "_generation_snapshots", {key: {
        "parent_id": prompt["id"], "draft_msg_id": draft["id"],
        "loom_thread_id": draft["loom_thread_id"], "client_instance_id": "old-page",
    }})
    monkeypatch.setattr(server, "_pending_hook_permissions", {})
    monkeypatch.setattr(server, "_event_router", SubscriptionRouter())

    class Socket:
        def __init__(self, client_id):
            self._client_instance_id = client_id
            self.events = []
            self.reads = 0

        async def send_json(self, payload):
            self.events.append(payload)

        async def receive_text(self):
            self.reads += 1
            if self.reads == 1:
                # Initial replay ran while the old socket was still registered.
                assert not any(e["type"] == "permission_request" for e in self.events)
                server._active_websockets[conv["id"]].discard(origin)
                return json.dumps({"action": action, "run_id": "gen:994"})
            if self.reads == 2:
                # Heartbeat retries must not re-deliver an already sent prompt.
                return json.dumps({"action": "ping"})
            raise WebSocketDisconnect()

    origin = Socket("old-page")
    replacement = Socket("replacement")
    monkeypatch.setattr(server, "_active_websockets", {conv["id"]: {origin, replacement}})
    server._event_router.register(replacement, conv["id"])
    await server._continue_ws_chat(replacement, conv["id"], "replacement")
    prompts = [e for e in replacement.events if e["type"] == "permission_request"]
    assert len(prompts) == (1 if position == "draft" else 0)
    if prompts:
        assert prompts[0]["request_id"] == "reload-request"
        assert prompts[0]["run_id"] == "gen:994"
        assert prompts[0]["draft_msg_id"] == draft["id"]
    if action == "subscribe_run" and position == "draft":
        kinds = [e["type"] for e in replacement.events]
        assert kinds.index("run_subscription") < kinds.index("permission_request")
