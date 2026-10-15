"""Persistent, individually revocable browser sessions and one-use recovery codes.

Only token digests are stored. This is machine state, outside the checkout;
it is not an isolation boundary against programs running as the same OS user.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager

from exo_state import state_path

SESSION_TTL_SECONDS = 90 * 86400
SESSION_IDLE_SECONDS = 30 * 86400
_TOUCH_INTERVAL_SECONDS = 3600


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@contextmanager
def _connection():
    path = state_path("auth", "browser-sessions.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS browser_sessions (
            id TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE, label TEXT NOT NULL,
            created_at REAL NOT NULL, last_seen REAL NOT NULL, expires_at REAL NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS recovery_codes (
            digest TEXT PRIMARY KEY, created_at REAL NOT NULL
        )""")
        with conn:
            yield conn
    finally:
        conn.close()


def issue_session(label: str, *, recovery_code: str | None = None,
                  now: float | None = None) -> tuple[str, dict] | None:
    """Recovery consumption and session creation are one SQLite transaction."""
    now = time.time() if now is None else now
    token = secrets.token_urlsafe(32)
    session = {"id": secrets.token_hex(16), "label": label[:120],
               "created_at": now, "last_seen": now, "expires_at": now + SESSION_TTL_SECONDS}
    with _connection() as conn:
        if recovery_code is not None:
            deleted = conn.execute("DELETE FROM recovery_codes WHERE digest = ?",
                                   (_digest(recovery_code),)).rowcount
            if not deleted:
                return None
        conn.execute("DELETE FROM browser_sessions WHERE expires_at <= ? OR last_seen <= ?",
                     (now, now - SESSION_IDLE_SECONDS))
        conn.execute("INSERT INTO browser_sessions VALUES (?, ?, ?, ?, ?, ?)",
                     (session["id"], _digest(token), session["label"], now, now, session["expires_at"]))
    return token, session


def session_for_token(token: str | None, *, now: float | None = None) -> dict | None:
    if not token or len(token) != 43:
        return None
    now = time.time() if now is None else now
    with _connection() as conn:
        row = conn.execute("""SELECT id, label, created_at, last_seen, expires_at
            FROM browser_sessions WHERE digest = ? AND expires_at > ? AND last_seen > ?""",
            (_digest(token), now, now - SESSION_IDLE_SECONDS)).fetchone()
        if row is None:
            return None
        if now - row["last_seen"] >= _TOUCH_INTERVAL_SECONDS:
            conn.execute("UPDATE browser_sessions SET last_seen = ? WHERE id = ?", (now, row["id"]))
        return dict(row)


def list_sessions(*, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    with _connection() as conn:
        return [dict(row) for row in conn.execute("""SELECT id, label, created_at, last_seen, expires_at
            FROM browser_sessions WHERE expires_at > ? AND last_seen > ? ORDER BY created_at DESC""",
            (now, now - SESSION_IDLE_SECONDS))]


def revoke_session(session_id: str) -> bool:
    with _connection() as conn:
        return bool(conn.execute("DELETE FROM browser_sessions WHERE id = ?", (session_id,)).rowcount)


def rotate_recovery_codes(count: int = 5) -> list[str]:
    if not 1 <= count <= 10:
        raise ValueError("Choose between one and ten recovery codes")
    codes = ["REC-" + "-".join(value[n:n + 8] for n in range(0, 40, 8)).upper()
             for value in (secrets.token_hex(20) for _ in range(count))]
    with _connection() as conn:
        conn.execute("DELETE FROM recovery_codes")
        conn.executemany("INSERT INTO recovery_codes VALUES (?, ?)",
                         [(_digest(code), time.time()) for code in codes])
    return codes


def recovery_count() -> int:
    with _connection() as conn:
        return conn.execute("SELECT count(*) FROM recovery_codes").fetchone()[0]
