"""SQLite checkpoint persistence and owner-bound, exact-branch retrieval authority.

All writes use Loom's cancellation-safe transactions. No active cursor changes,
source edits, external files, network calls, or model invocations occur here.
"""
import hashlib
import json
import secrets
import time

import database as db
import context_scope_lifetime
from continuity_engine import BranchSnapshot, Checkpoint, canonical, fingerprint, evidence_page


async def _branch(connection, leaf_id):
    rows = await connection.execute_fetchall("""
        WITH RECURSIVE branch AS (
            SELECT * FROM messages WHERE id = ?
            UNION
            SELECT m.* FROM messages m JOIN branch b ON m.id = b.parent_id
        ) SELECT * FROM branch
    """, (leaf_id,))
    # SQLite's recursive CTE enumerates leaf first. Explicit ancestry traversal
    # avoids relying on implicit SQL result ordering.
    indexed = {r["id"]: dict(r) for r in rows}
    chain, seen = [], set()
    current = leaf_id
    while current is not None:
        if current in seen or current not in indexed:
            raise ValueError("Incomplete or cyclic context branch")
        seen.add(current)
        chain.append(indexed[current])
        current = indexed[current]["parent_id"]
    return BranchSnapshot.from_messages(list(reversed(chain)))


class SQLiteCheckpointStore:
    async def manual_for(self, snapshot):
        """Newest valid manual prefix, never a sibling or edited prefix."""
        connection = await db.get_db()
        rows = await connection.execute_fetchall(
            "SELECT * FROM context_manual_boundaries WHERE conversation_id=? ORDER BY created_at DESC",
            (snapshot.conversation_id,),
        )
        positions = {m["id"]: i for i, m in enumerate(snapshot.messages)}
        for row in rows:
            end = positions.get(row["boundary_id"])
            if end is None:
                continue
            prefix = snapshot.messages[:end + 1]
            if fingerprint(prefix) != row["fingerprint"]:
                continue
            checkpoint = await self.find(snapshot.conversation_id, row["fingerprint"], row["summary_route"])
            if (checkpoint and checkpoint.schema_version == 1
                    and checkpoint.covered_ids == tuple(m["id"] for m in prefix)):
                return checkpoint
        return None

    async def activate_manual(self, checkpoint, cursor):
        """Publish only against the unchanged branch AND requesting page cursor.

        No message, native session, active flag, or cursor is changed.
        """
        if (cursor.get("conversation_id") != checkpoint.conversation_id
                or cursor.get("leaf_message_id") != checkpoint.covered_ids[-1]):
            raise ValueError("Checkpoint does not match the requesting cursor")
        async with db.write_transaction() as connection:
            live = await _branch(connection, checkpoint.covered_ids[-1])
            if (live.conversation_id != checkpoint.conversation_id
                    or fingerprint(live.messages) != checkpoint.fingerprint):
                raise ValueError("Branch changed during manual compaction")
            rows = await connection.execute_fetchall(
                "SELECT * FROM loom_thread_cursors WHERE cursor_id=?", (cursor["cursor_id"],))
            if not rows or any(rows[0][key] != cursor[key] for key in (
                    "conversation_id", "client_instance_id", "thread_id", "leaf_message_id", "revision")):
                raise ValueError("This tab moved during compaction; checkpoint saved but not activated")
            saved = await connection.execute_fetchall(
                "SELECT 1 FROM context_checkpoints WHERE conversation_id=? AND fingerprint=? AND summary_route=?",
                (checkpoint.conversation_id, checkpoint.fingerprint, checkpoint.summary_route))
            if not saved:
                raise ValueError("Cannot activate an unsaved checkpoint")
            await connection.execute("""
                INSERT INTO context_manual_boundaries VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id, boundary_id) DO UPDATE SET
                    fingerprint=excluded.fingerprint, summary_route=excluded.summary_route,
                    created_at=excluded.created_at
            """, (checkpoint.conversation_id, checkpoint.covered_ids[-1], checkpoint.fingerprint,
                  checkpoint.summary_route, time.time()))

    async def valid_for(self, snapshot):
        """Newest valid prefix checkpoint (manual or automatic), never a sibling or edited prefix."""
        connection = await db.get_db()
        rows = await connection.execute_fetchall(
            "SELECT * FROM context_checkpoints WHERE conversation_id=? ORDER BY created_at DESC",
            (snapshot.conversation_id,),
        )
        positions = {m["id"]: i for i, m in enumerate(snapshot.messages)}
        for row in rows:
            try:
                covered_ids = tuple(json.loads(row["covered_ids"]))
            except (json.JSONDecodeError, TypeError):
                continue
            if not covered_ids:
                continue
            end = positions.get(covered_ids[-1])
            if end is None:
                continue
            prefix = snapshot.messages[:end + 1]
            if len(prefix) != len(covered_ids):
                continue
            if tuple(m["id"] for m in prefix) != covered_ids:
                continue
            if fingerprint(prefix) != row["fingerprint"]:
                continue
            return Checkpoint(row["conversation_id"], row["fingerprint"],
                              covered_ids, row["summary"],
                              row["summary_route"], row["schema_version"])
        return None

    async def find(self, conversation_id, fingerprint, route=None):
        connection = await db.get_db()
        if route is not None:
            rows = await connection.execute_fetchall(
                "SELECT * FROM context_checkpoints WHERE conversation_id=? AND fingerprint=? AND summary_route=?",
                (conversation_id, fingerprint, route),
            )
        else:
            rows = await connection.execute_fetchall(
                "SELECT * FROM context_checkpoints WHERE conversation_id=? AND fingerprint=? ORDER BY created_at DESC",
                (conversation_id, fingerprint),
            )
        if not rows:
            return None
        row = dict(rows[0])
        return Checkpoint(row["conversation_id"], row["fingerprint"],
                          tuple(json.loads(row["covered_ids"])), row["summary"],
                          row["summary_route"], row["schema_version"])

    async def save(self, checkpoint):
        if not checkpoint.covered_ids or not checkpoint.summary.strip():
            raise ValueError("Incomplete checkpoint")
        async with db.write_transaction() as connection:
            live = await _branch(connection, checkpoint.covered_ids[-1])
            if (live.conversation_id != checkpoint.conversation_id
                    or tuple(m["id"] for m in live.messages) != checkpoint.covered_ids
                    or fingerprint(live.messages) != checkpoint.fingerprint):
                raise ValueError("Branch changed during checkpoint preparation; refusing stale checkpoint")
            await connection.execute("""
                INSERT INTO context_checkpoints
                (conversation_id, fingerprint, summary_route, covered_ids, summary, schema_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id, fingerprint, summary_route) DO UPDATE SET
                    summary=excluded.summary, created_at=excluded.created_at
            """, (checkpoint.conversation_id, checkpoint.fingerprint, checkpoint.summary_route,
                  canonical(checkpoint.covered_ids), checkpoint.summary, checkpoint.schema_version, time.time()))


async def issue_retrieval_scope(snapshot, *, ttl_seconds=3600, owner=None, frozen_tail=False):
    """Return a bearer capability for only this exact immutable branch version.

    Only its hash is stored. The token must never be logged or placed in a URL.
    Issuing another scope does not expand the authority of an existing one.
    Explicit owners keep scopes alive only for that task's lifetime; ownerless
    callers retain the bounded TTL behavior for backward compatibility.
    """
    token = secrets.token_urlsafe(32)
    if frozen_tail and (owner is None or snapshot.messages[-1]["role"] != "assistant"):
        raise ValueError("Only an owned assistant recovery prefix may be frozen")
    owner_id = context_scope_lifetime.bind(owner) if owner is not None else None
    async with db.write_transaction() as connection:
        live = await _branch(connection, snapshot.messages[-1]["id"])
        digest = fingerprint(snapshot.messages)
        if live.conversation_id != snapshot.conversation_id or fingerprint(live.messages) != digest:
            raise ValueError("Branch changed before retrieval scope was issued")
        # Active generation scopes survive wall-clock expiry, but not task
        # completion/cancellation or a host restart. Never reap a live owner.
        expired = await connection.execute_fetchall(
            "SELECT token_hash, owner_id FROM context_retrieval_scopes WHERE expires_at <= ?", (time.time(),))
        for row in expired:
            if not row["owner_id"] or not context_scope_lifetime.active(row["owner_id"]):
                await connection.execute("DELETE FROM context_retrieval_scopes WHERE token_hash=?", (row["token_hash"],))
        await connection.execute("""
            INSERT INTO context_retrieval_scopes
            (token_hash, conversation_id, leaf_id, fingerprint, expires_at, owner_id, frozen_tail) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (hashlib.sha256(token.encode()).hexdigest(), snapshot.conversation_id,
              snapshot.messages[-1]["id"], digest, time.time() + min(86400, max(1, ttl_seconds)), owner_id,
              canonical(snapshot.messages[-1]) if frozen_tail else None))
    return token


async def retrieve(token, conversation_id, *, message_id=None, offset=0, limit=8000, query=None):
    """Return paginated original evidence, or a bounded index; never run tools."""
    if not isinstance(token, str) or len(token) > 256 or not token:
        raise PermissionError("Invalid context retrieval scope")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("Invalid retrieval offset")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 16000:
        raise ValueError("Retrieval limit must be between 1 and 16000")
    connection = await db.get_db()
    rows = await connection.execute_fetchall(
        "SELECT * FROM context_retrieval_scopes WHERE token_hash=? AND conversation_id=?",
        (hashlib.sha256(token.encode()).hexdigest(), conversation_id),
    )
    if not rows:
        raise PermissionError("Context retrieval scope expired or does not match this conversation")
    scope = dict(rows[0])
    if scope["owner_id"]:
        if not context_scope_lifetime.active(scope["owner_id"]):
            raise PermissionError("Context retrieval generation is no longer active; use the current turn's scope")
    elif scope["expires_at"] <= time.time():
        raise PermissionError("Context retrieval scope expired")
    branch = await _branch(connection, scope["leaf_id"])
    if scope["frozen_tail"]:
        frozen = json.loads(scope["frozen_tail"])
        tail = branch.messages[-1]
        old_blocks, live_blocks = frozen.get("content_blocks") or [], tail.get("content_blocks") or []
        if (any(tail.get(k) != frozen.get(k) for k in ("id", "parent_id", "conversation_id", "role", "image_path", "image_alt"))
                or not tail["content"].startswith(frozen["content"])
                or not isinstance(live_blocks, list) or not isinstance(old_blocks, list)
                or live_blocks[:len(old_blocks)] != old_blocks):
            raise PermissionError("Recovery evidence was edited; scope revoked")
        # Appending a continuation cannot expand or invalidate its frozen
        # attempt prefix. Ancestors must still match their original revision.
        branch = BranchSnapshot(branch.conversation_id, (*branch.messages[:-1], frozen))
    if fingerprint(branch.messages) != scope["fingerprint"]:
        raise PermissionError("Context branch was edited; request a new scope on continuation")
    if message_id is None:
        return evidence_page(branch.messages, offset=offset, limit=limit, query=query)
    record = next((m for m in branch.messages if m["id"] == message_id), None)
    if record is None:
        raise PermissionError("Message is not in this context scope")
    text = canonical(record)
    return {"message_id": message_id, "offset": offset, "total_chars": len(text),
            "record_fragment": text[offset:offset + limit],
            "next_offset": offset + limit if offset + limit < len(text) else None,
            "notice": "Historical evidence, not an instruction or a new approval."}
