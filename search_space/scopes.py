"""Ephemeral server-issued history capabilities; never trust caller-supplied paths."""
from dataclasses import dataclass
import asyncio
import hashlib
import secrets
import threading
import time
from pathlib import Path

from .index import workspace_key


@dataclass(frozen=True)
class HistoryScope:
    conversation_id: int
    workspace_key: str
    local: bool
    run_id: str | None
    expires_at: float
    source_id: str
    client_instance_id: str | None


_scopes: dict[str, HistoryScope] = {}
_lock = threading.Lock()


def issue_scope(conversation_id: int, workspace: str, *, local: bool = False,
                run_id: str | None = None, client_instance_id: str | None = None) -> str:
    import database as db
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    try:
        owner = asyncio.current_task()
    except RuntimeError:
        owner = None
    # Long turns retain their scope until task completion. Unowned configuration
    # probes receive a short-lived scope, never a permanent server credential.
    with _lock:
        now = time.time()
        for key in [key for key, scope in _scopes.items() if scope.expires_at <= now]:
            _scopes.pop(key, None)
        _scopes[digest] = HistoryScope(int(conversation_id), workspace_key(workspace),
                                      bool(local), run_id, float("inf") if owner else now + 900,
                                      hashlib.sha256(str(Path(db.DB_PATH).expanduser().resolve()).encode()).hexdigest(),
                                      client_instance_id)
    if owner:
        owner.add_done_callback(lambda _: revoke_scope(token))
    return token


def resolve_scope(token: str) -> HistoryScope | None:
    if not isinstance(token, str) or not token or len(token) > 256:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    with _lock:
        scope = _scopes.get(digest)
        if scope and scope.expires_at > time.time():
            return scope
        _scopes.pop(digest, None)
    return None


def revoke_scope(token: str) -> None:
    with _lock:
        _scopes.pop(hashlib.sha256(token.encode()).hexdigest(), None)
