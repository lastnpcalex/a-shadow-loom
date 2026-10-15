"""Typed, run-scoped WebSocket event envelopes and subscriptions.

The chat socket historically carried unstructured dictionaries and relied on
the browser's current branch to decide whether an event belonged to it.  That
is not a safe ownership model when several windows or agent runs share a
conversation.  This module keeps the wire format backwards compatible while
making routing identity explicit and server-enforced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import uuid
from typing import Any, Iterable


# Events that can change a live generation UI or expose a tool/permission
# payload.  These must always be delivered by exact run subscription.
RUN_SCOPED_KINDS = frozenset(
    {
        "stream_start",
        "generation_preparing",
        "stream_chunk",
        "thinking_start",
        "thinking_end",
        "thinking_chunk",
        "tool_start",
        "tool_input_chunk",
        "tool_result",
        "usage",
        "first_token",
        "generation_terminal",
        "model_attestation",
        "stream_end",
        "cancelled",
        "interrupted",
        "error",
        "warning",
        "permission_request",
        "permission_resolved",
        "ask_user_question",
        "plan_ready",
        "workspace_change_report",
        "state_update",
        "canvas_updated",
        "chat_item",
    }
)

# These events are intentionally content-free notifications.  They may be
# delivered globally, but must never contain generated text or branch content.
GLOBAL_NOTIFICATION_KINDS = frozenset(
    {"branch_landed", "plan_landed", "hermes_model_state", "permission_notice"}
)


def canonical_run_id(run_id: str | int | None) -> str | None:
    """Normalize generation IDs and durable child UUIDs for the wire.

    Database rows store child UUIDs without a prefix, while every WebSocket
    run ID is namespaced.  Keeping that conversion here prevents a reconnect
    from subscribing to ``agent:<uuid>`` while publishers emit bare UUIDs.
    """
    if run_id is None:
        return None
    text = str(run_id).strip()
    if not text:
        return None
    if text.startswith(("gen:", "agent:")) or ":" in text:
        return text
    if text.isdigit():
        return f"gen:{text}"
    return f"agent:{text}"


@dataclass(frozen=True)
class LoomEventEnvelope:
    """The canonical identity envelope for every WebSocket event.

    Identity fields are present even when a value is ``None`` (for example a
    global notification has no run).  Existing top-level fields are retained
    by :meth:`as_dict` so older clients continue to function during rollout.
    """

    event_id: str
    event_kind: str
    conversation_id: int | None
    loom_thread_id: str | None
    run_id: str | None
    draft_id: int | None
    parent_id: int | None
    client_instance_id: str | None
    sequence: int
    scope: str
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any] | None,
        *,
        conversation_id: int | None = None,
        loom_thread_id: str | None = None,
        run_id: str | int | None = None,
        draft_id: int | None = None,
        parent_id: int | None = None,
        client_instance_id: str | None = None,
        sequence: int = 0,
        event_id: str | None = None,
        scope: str | None = None,
    ) -> "LoomEventEnvelope":
        raw = dict(payload or {})
        kind = str(raw.get("event_kind") or raw.get("type") or "event")
        if conversation_id is None:
            conversation_id = raw.get("conversation_id", raw.get("conv_id"))
        if loom_thread_id is None:
            loom_thread_id = raw.get("loom_thread_id")
        if run_id is None:
            run_id = raw.get("run_id")
        if run_id is None and raw.get("gen_id") is not None:
            run_id = raw.get("gen_id")
        if draft_id is None:
            draft_id = raw.get("draft_id", raw.get("draft_msg_id"))
        if parent_id is None:
            parent_id = raw.get("parent_id")
        if client_instance_id is None:
            client_instance_id = raw.get("client_instance_id")
        normalized_run = canonical_run_id(run_id)
        if scope is None:
            scope = "run" if normalized_run is not None else (
                "global" if kind in GLOBAL_NOTIFICATION_KINDS else "conversation"
            )
        return cls(
            event_id=str(event_id or raw.get("event_id") or uuid.uuid4()),
            event_kind=kind,
            conversation_id=int(conversation_id) if conversation_id is not None else None,
            loom_thread_id=(str(loom_thread_id) if loom_thread_id else None),
            run_id=normalized_run,
            draft_id=int(draft_id) if draft_id is not None else None,
            parent_id=int(parent_id) if parent_id is not None else None,
            client_instance_id=(str(client_instance_id) if client_instance_id else None),
            sequence=max(0, int(sequence or 0)),
            scope=scope,
            payload=raw,
        )

    @property
    def is_run_scoped(self) -> bool:
        return self.scope == "run" or self.event_kind in RUN_SCOPED_KINDS

    def as_dict(self) -> dict[str, Any]:
        """Return a backwards-compatible JSON object with typed identity."""
        result = dict(self.payload)
        result.update(
            {
                "type": self.event_kind,
                "event_id": self.event_id,
                "event_kind": self.event_kind,
                "conversation_id": self.conversation_id,
                "conv_id": self.conversation_id,
                "loom_thread_id": self.loom_thread_id,
                "run_id": self.run_id,
                "draft_id": self.draft_id,
                "draft_msg_id": self.draft_id,
                "parent_id": self.parent_id,
                "client_instance_id": self.client_instance_id,
                "sequence": self.sequence,
                "scope": self.scope,
                "event": {
                    "event_id": self.event_id,
                    "kind": self.event_kind,
                    "conversation_id": self.conversation_id,
                    "loom_thread_id": self.loom_thread_id,
                    "run_id": self.run_id,
                    "draft_id": self.draft_id,
                    "parent_id": self.parent_id,
                    "client_instance_id": self.client_instance_id,
                    "sequence": self.sequence,
                    "scope": self.scope,
                },
            }
        )
        return result


class EventSequencer:
    """Monotonic sequence numbers per run (or conversation for globals)."""

    def __init__(self) -> None:
        self._next: dict[str, int] = {}

    def next(self, run_id: str | None, conversation_id: int | None) -> int:
        key = f"run:{run_id}" if run_id is not None else f"conv:{conversation_id}"
        value = self._next.get(key, 0) + 1
        self._next[key] = value
        return value


class SubscriptionRouter:
    """In-memory connection subscriptions with exact run matching.

    A connection is registered to one conversation by the server route.  Run
    subscriptions are explicit; there is deliberately no fallback based on
    active branch, focus, or a conversation-wide broadcast.
    """

    def __init__(self) -> None:
        self._conversations: dict[Any, int] = {}
        self._runs: dict[Any, set[str]] = {}

    def register(self, websocket: Any, conversation_id: int) -> None:
        self._conversations[websocket] = int(conversation_id)
        self._runs.setdefault(websocket, set())

    def unregister(self, websocket: Any) -> None:
        self._conversations.pop(websocket, None)
        self._runs.pop(websocket, None)

    def subscribe(self, websocket: Any, run_id: str | int) -> bool:
        if websocket not in self._conversations or run_id is None:
            return False
        normalized = canonical_run_id(run_id)
        if normalized is None:
            return False
        self._runs.setdefault(websocket, set()).add(normalized)
        return True

    def unsubscribe(self, websocket: Any, run_id: str | int) -> bool:
        runs = self._runs.get(websocket)
        if runs is None:
            return False
        normalized = canonical_run_id(run_id)
        if normalized is None:
            return False
        runs.discard(normalized)
        return True

    def subscriptions(self, websocket: Any) -> set[str]:
        return set(self._runs.get(websocket, set()))

    def recipients(
        self,
        envelope: LoomEventEnvelope,
        clients: Iterable[Any],
    ) -> set[Any]:
        available = set(clients or ())
        if envelope.scope == "global":
            return available
        if envelope.is_run_scoped:
            if envelope.run_id is None:
                return set()
            return {
                ws
                for ws in available
                if self._conversations.get(ws) == envelope.conversation_id
                and envelope.run_id in self._runs.get(ws, set())
            }
        return {
            ws
            for ws in available
            if self._conversations.get(ws) == envelope.conversation_id
        }


def content_free_notification(data: dict[str, Any]) -> dict[str, Any]:
    """Strip generated content from an event intended for global delivery."""
    allowed = {
        "type",
        "event_kind",
        "conv_id",
        "conversation_id",
        "conv_title",
        "message_id",
        "plan_file",
        "status",
        "request_id",
        # Hermes liveness is metadata only; retaining these two fields keeps
        # the header indicator useful without ever carrying model output.
        "backend",
        "up",
    }
    return {key: value for key, value in data.items() if key in allowed}
