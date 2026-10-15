"""Isolation guarantees for the run-scoped WebSocket event bus."""

from event_router import (
    EventSequencer,
    LoomEventEnvelope,
    SubscriptionRouter,
    canonical_run_id,
    content_free_notification,
)


class Socket:
    pass


def test_launch_socket_chooses_latest_connected_page_not_stale_reconnect(monkeypatch):
    from types import SimpleNamespace
    from starlette.websockets import WebSocketState
    import server

    def socket(client, when, state=WebSocketState.CONNECTED):
        return SimpleNamespace(_client_instance_id=client, _loom_connected_at=when,
                               client_state=state, application_state=state)
    closed = socket('page', 1, WebSocketState.DISCONNECTED)
    old = socket('page', 2)
    current = socket('page', 3)
    other = socket('other-page', 4)
    # Selection must not depend on the iteration order of the live set.
    monkeypatch.setattr(server, '_active_websockets', {12: [closed, old, current, other]})
    assert server._find_client_websocket(12, 'page') is current
    old.application_state = WebSocketState.DISCONNECTED
    current.client_state = WebSocketState.DISCONNECTED
    assert server._find_client_websocket(12, 'page') is None
    assert server._find_client_websocket(12, 'other-page') is other
    assert server._find_client_websocket(13, 'page') is None


def test_envelope_has_stable_identity_and_legacy_aliases():
    event = LoomEventEnvelope.from_payload(
        {"type": "stream_chunk", "content": "secret"},
        conversation_id=12,
        loom_thread_id="loom:12:root",
        run_id=7,
        draft_id=20,
        parent_id=19,
        client_instance_id="window-a",
        sequence=3,
    )
    wire = event.as_dict()
    assert event.run_id == "gen:7"
    assert wire["event"]["conversation_id"] == 12
    assert wire["event"]["loom_thread_id"] == "loom:12:root"
    assert wire["event"]["run_id"] == "gen:7"
    assert wire["event"]["draft_id"] == 20
    assert wire["event"]["parent_id"] == 19
    assert wire["event"]["client_instance_id"] == "window-a"
    assert wire["sequence"] == 3
    assert wire["type"] == wire["event_kind"] == "stream_chunk"


def test_child_uuid_has_one_canonical_wire_identity():
    assert canonical_run_id("child-uuid") == "agent:child-uuid"
    assert canonical_run_id("agent:child-uuid") == "agent:child-uuid"

    router = SubscriptionRouter()
    socket = Socket()
    router.register(socket, 12)
    assert router.subscribe(socket, "child-uuid")
    assert router.subscriptions(socket) == {"agent:child-uuid"}

    event = LoomEventEnvelope.from_payload(
        {"type": "agent_started"},
        conversation_id=12,
        run_id="child-uuid",
    )
    assert event.run_id == "agent:child-uuid"
    assert router.recipients(event, {socket}) == {socket}


def test_router_requires_exact_run_subscription_across_windows_and_conversations():
    router = SubscriptionRouter()
    a1, a2, b1 = Socket(), Socket(), Socket()
    router.register(a1, 1)
    router.register(a2, 1)
    router.register(b1, 2)
    router.subscribe(a1, "gen:10")
    router.subscribe(a2, "gen:11")
    router.subscribe(b1, "gen:10")

    event = LoomEventEnvelope.from_payload(
        {"type": "stream_chunk", "content": "x"},
        conversation_id=1,
        run_id="gen:10",
        sequence=1,
    )
    assert router.recipients(event, {a1, a2, b1}) == {a1}


def test_global_notification_removes_generated_content():
    safe = content_free_notification(
        {
            "type": "branch_landed",
            "conv_id": 1,
            "message_id": 55,
            "preview": "private generated text",
            "content": "private generated text",
        }
    )
    assert "preview" not in safe
    assert "content" not in safe
    assert safe["message_id"] == 55


def test_sequences_are_monotonic_per_run_but_independent_between_runs():
    seq = EventSequencer()
    assert [seq.next("gen:1", 1) for _ in range(3)] == [1, 2, 3]
    assert seq.next("gen:2", 1) == 1
    assert seq.next(None, 1) == 1


async def test_server_send_does_not_cross_windows_or_conversations(tmp_database):
    import asyncio
    import server

    class WebSocket:
        def __init__(self):
            self.events = []

        async def send_json(self, value):
            self.events.append(value)

    first_window, second_window, other_conv = WebSocket(), WebSocket(), WebSocket()
    server._active_websockets[101] = {first_window, second_window}
    server._active_websockets[202] = {other_conv}
    server._event_router.register(first_window, 101)
    server._event_router.register(second_window, 101)
    server._event_router.register(other_conv, 202)
    server._event_router.subscribe(first_window, "gen:501")
    server._event_router.subscribe(second_window, "gen:502")
    server._event_router.subscribe(other_conv, "gen:501")
    task = asyncio.current_task()
    old_key = getattr(task, "_gen_key", None)
    key = (101, 7, 501)
    task._gen_key = key
    server._generation_snapshots[key] = {"parent_id": 7, "draft_msg_id": 8}
    try:
        await server._ws_send(101, {"type": "stream_chunk", "content": "only one"})
        assert len(first_window.events) == 1
        assert second_window.events == []
        assert other_conv.events == []
    finally:
        if old_key is None:
            delattr(task, "_gen_key")
        else:
            task._gen_key = old_key
        server._event_router.unregister(first_window)
        server._event_router.unregister(second_window)
        server._event_router.unregister(other_conv)
        server._active_websockets.pop(101, None)
        server._active_websockets.pop(202, None)
        server._generation_snapshots.pop(key, None)


async def test_server_child_event_reaches_prefixed_reconnect_subscription(tmp_database):
    import database as db
    import server

    class WebSocket:
        def __init__(self):
            self.events = []

        async def send_json(self, value):
            self.events.append(value)

    conv = await db.create_conversation("Child event routing")
    run = await db.create_agent_run("child-uuid", conv["id"], "inspect")
    event = await db.append_agent_event(
        run["run_id"], "agent_started", {"task": "inspect"}
    )
    socket = WebSocket()
    server._active_websockets[conv["id"]] = {socket}
    server._event_router.register(socket, conv["id"])
    server._event_router.subscribe(socket, "agent:child-uuid")
    try:
        await server._publish_agent_event(run, event)
        assert len(socket.events) == 1
        assert socket.events[0]["run_id"] == "agent:child-uuid"
        assert socket.events[0]["type"] == "agent_started"
        assert socket.events[0]["task"] == "inspect"
    finally:
        server._event_router.unregister(socket)
        server._active_websockets.pop(conv["id"], None)
