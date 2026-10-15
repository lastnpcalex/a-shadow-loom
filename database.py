"""SQLite database with tree-structured message storage."""

import asyncio
from contextlib import asynccontextmanager

import aiosqlite
import json
import time
import uuid
from typing import Optional

import os as _os
from config import config as _config

DB_PATH = _config.db_path

_db: aiosqlite.Connection | None = None
_db_shutdown = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT 'New Conversation',
    character_id TEXT,
    persona_id TEXT,
    lore_ids TEXT NOT NULL DEFAULT '[]',
    style_nudge TEXT NOT NULL DEFAULT 'Natural',
    custom_scene TEXT,
    system_only INTEGER DEFAULT 0,
    minimal_ooda_enabled INTEGER DEFAULT 1,
    system_prompt TEXT,
    parallel_agents_enabled INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS casual_chat_requests (
    request_id TEXT PRIMARY KEY,
    conversation_id INTEGER NOT NULL UNIQUE REFERENCES conversations(id) ON DELETE CASCADE,
    selection TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deleted_casual_chat_requests (
    request_id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS chat_items (
    item_id TEXT PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL,
    message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_items_run ON chat_items(run_id);
CREATE INDEX IF NOT EXISTS idx_chat_items_conversation ON chat_items(conversation_id);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    parent_id INTEGER,
    role TEXT NOT NULL CHECK(role IN ('system','user','assistant')),
    content TEXT NOT NULL,
    image_path TEXT,
    image_alt TEXT,
    describe_context TEXT,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    summary TEXT,
    origin_client_id TEXT,
    target_anchor_id INTEGER,
    send_intent TEXT,
    loom_thread_id TEXT,
    client_request_id TEXT,
    turn_lifecycle_state TEXT,
    generation_lease_id TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES messages(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    branch_path TEXT NOT NULL DEFAULT '[]',
    content TEXT NOT NULL,
    covers_up_to INTEGER NOT NULL,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS style_state (
    conversation_id INTEGER PRIMARY KEY,
    current_nudge_index INTEGER NOT NULL DEFAULT 0,
    repetition_alert_level INTEGER NOT NULL DEFAULT 0,
    last_ngram_snapshot TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_id);
CREATE INDEX IF NOT EXISTS idx_summaries_conv ON summaries(conversation_id);

CREATE TABLE IF NOT EXISTS bookmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    branch_name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
    UNIQUE(conversation_id, message_id)
);
CREATE TABLE IF NOT EXISTS agent_notification_dismissals (
    notification_key TEXT PRIMARY KEY
);
CREATE INDEX IF NOT EXISTS idx_bookmarks_conv ON bookmarks(conversation_id);

CREATE TABLE IF NOT EXISTS branch_pins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    label TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
    UNIQUE(conversation_id, message_id)
);

-- Continuity checkpoints supplement, never replace, canonical message history.
CREATE TABLE IF NOT EXISTS context_checkpoints (
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    summary_route TEXT NOT NULL,
    covered_ids TEXT NOT NULL,
    summary TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (conversation_id, fingerprint, summary_route)
);

-- Durable portable-context preparation operations. A crash resumes from the
-- recorded accepted cursor; it never restarts compaction from scratch.
CREATE TABLE IF NOT EXISTS preparation_operations (
    operation_id TEXT PRIMARY KEY,
    owner_run_id TEXT,
    conversation_id INTEGER,
    source_revision TEXT NOT NULL,
    destination TEXT NOT NULL,
    phase TEXT NOT NULL,
    covered_cursor TEXT NOT NULL,
    accepted_state TEXT,
    call_budget INTEGER NOT NULL,
    calls_used INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    terminal_reason TEXT
);

CREATE TABLE IF NOT EXISTS context_retrieval_scopes (
    token_hash TEXT PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    leaf_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    expires_at REAL NOT NULL,
    owner_id TEXT,
    frozen_tail TEXT
);

CREATE TABLE IF NOT EXISTS context_manual_boundaries (
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    boundary_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    summary_route TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (conversation_id, boundary_id)
);

-- Stable Loom topology. A conversation may contain many independent root
-- threads; every message belongs to exactly one of them. Empty threads are
-- real server objects so "+ Thread" cannot be represented only by browser
-- memory while a prior branch request is still in flight.
CREATE TABLE IF NOT EXISTS loom_threads (
    thread_id TEXT PRIMARY KEY,
    conversation_id INTEGER NOT NULL,
    root_message_id INTEGER,
    created_by_client_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (root_message_id) REFERENCES messages(id) ON DELETE SET NULL,
    UNIQUE(conversation_id, root_message_id)
);
CREATE INDEX IF NOT EXISTS idx_loom_threads_conv
    ON loom_threads(conversation_id, created_at);

-- One optimistic cursor per browser page and conversation. Cursor revision is
-- compare-and-swap state: a late branch load cannot overwrite a newer explicit
-- navigation/new-thread action, and a stale send is rejected rather than
-- guessed onto the conversation-wide active leaf.
CREATE TABLE IF NOT EXISTS loom_thread_cursors (
    cursor_id TEXT PRIMARY KEY,
    conversation_id INTEGER NOT NULL,
    thread_id TEXT NOT NULL,
    client_instance_id TEXT NOT NULL,
    leaf_message_id INTEGER,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (thread_id) REFERENCES loom_threads(thread_id) ON DELETE CASCADE,
    FOREIGN KEY (leaf_message_id) REFERENCES messages(id) ON DELETE SET NULL,
    UNIQUE(conversation_id, client_instance_id)
);
CREATE INDEX IF NOT EXISTS idx_loom_cursors_thread
    ON loom_thread_cursors(thread_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_branch_pins_conv ON branch_pins(conversation_id);

CREATE TABLE IF NOT EXISTS state_schemas (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    fields TEXT NOT NULL,
    is_builtin INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS state_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    schema_id TEXT NOT NULL,
    label TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    is_readonly INTEGER DEFAULT 0,
    updated_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    UNIQUE(conversation_id, schema_id, label)
);
CREATE INDEX IF NOT EXISTS idx_state_cards_conv ON state_cards(conversation_id);

CREATE TABLE IF NOT EXISTS character_state_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id TEXT NOT NULL,
    schema_id TEXT NOT NULL,
    label TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    is_readonly INTEGER DEFAULT 0,
    updated_at REAL NOT NULL,
    UNIQUE(character_id, schema_id, label)
);
CREATE INDEX IF NOT EXISTS idx_char_state_cards ON character_state_cards(character_id);

CREATE TABLE IF NOT EXISTS pending_permissions (
    request_id TEXT PRIMARY KEY,
    conv_id INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    tool_input TEXT,
    input_summary TEXT,
    started_at REAL NOT NULL,
    broadcast_sent INTEGER DEFAULT 0,
    run_id TEXT,
    parent_id INTEGER,
    draft_msg_id INTEGER,
    client_instance_id TEXT,
    permission_scope TEXT,
    source TEXT,
    FOREIGN KEY (conv_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_pending_conv ON pending_permissions(conv_id);
CREATE INDEX IF NOT EXISTS idx_pending_started ON pending_permissions(started_at);

-- Durable permission decisions.  Pending rows disappear after resolution,
-- but the decision and optional human explanation remain branch-scoped so
-- adapters with decision-only protocols (notably ACP) can carry the note into
-- the next native continuation instead of silently dropping it.
CREATE TABLE IF NOT EXISTS permission_resolutions (
    request_id TEXT PRIMARY KEY,
    conv_id INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    allowed INTEGER NOT NULL,
    feedback TEXT,
    source TEXT,
    decision_source TEXT,
    run_id TEXT,
    parent_id INTEGER,
    draft_msg_id INTEGER,
    permission_scope TEXT,
    resolved_at REAL NOT NULL,
    FOREIGN KEY (conv_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_permission_resolution_draft
    ON permission_resolutions(draft_msg_id, resolved_at);

-- Shadow evaluations from the provider-independent permission policy.  These
-- rows intentionally contain normalized metadata only: never raw commands,
-- arguments, feedback, or workspace paths.
CREATE TABLE IF NOT EXISTS permission_policy_audits (
    request_id TEXT PRIMARY KEY,
    conv_id INTEGER NOT NULL,
    run_id TEXT,
    parent_id INTEGER,
    draft_msg_id INTEGER,
    permission_scope TEXT,
    harness TEXT,
    tool_name TEXT,
    profile TEXT NOT NULL,
    legacy_outcome TEXT NOT NULL,
    policy_decision TEXT NOT NULL,
    disagrees INTEGER NOT NULL DEFAULT 0,
    action_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (conv_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_permission_policy_audit_conv
    ON permission_policy_audits(conv_id, created_at);

-- Generations in flight. Row inserted when a CC/Gemini/local subprocess
-- launches, deleted when it finalizes normally. Any row we find on server
-- startup is an orphan (the server that spawned it died before finalize) —
-- we reap them in _reap_orphan_generations(). Keys off msg_id which is
-- unique per draft, so server restarts don't collide.
CREATE TABLE IF NOT EXISTS active_generations (
    draft_msg_id INTEGER PRIMARY KEY,
    conv_id INTEGER NOT NULL,
    pid INTEGER,
    session_id TEXT,
    project_dir TEXT,
    mode TEXT,
    loom_thread_id TEXT,
    started_at REAL NOT NULL,
    FOREIGN KEY (conv_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_active_gen_conv ON active_generations(conv_id);

-- A continuation typed while an assistant is still running is durable server
-- state, not browser-memory state.  A continuation may be a child of the
-- prerequisite draft; a standalone root or edited sibling keeps its own
-- topology.  This row records only that its generation must begin after the
-- prerequisite draft is no longer active.
CREATE TABLE IF NOT EXISTS queued_generations (
    user_msg_id INTEGER PRIMARY KEY,
    conv_id INTEGER NOT NULL,
    wait_for_draft_id INTEGER NOT NULL,
    client_instance_id TEXT,
    loom_thread_id TEXT,
    settings_json TEXT NOT NULL DEFAULT '{}',
    generation_count INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'queued',
    resume_authorized INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (user_msg_id) REFERENCES messages(id) ON DELETE CASCADE,
    FOREIGN KEY (conv_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (wait_for_draft_id) REFERENCES messages(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_queued_gen_conv ON queued_generations(conv_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_queued_gen_wait
    ON queued_generations(wait_for_draft_id) WHERE status IN ('queued', 'paused', 'launching');

-- SQLite, rather than volatile asyncio/PID bookkeeping, arbitrates entry to
-- a non-parallel conversation's provider lane. Terminal rows remain as
-- lifecycle evidence while the partial unique index permits exactly one
-- unreleased lease per conversation.
CREATE TABLE IF NOT EXISTS conversation_generation_leases (
    lease_id TEXT PRIMARY KEY,
    conv_id INTEGER NOT NULL,
    run_id TEXT,
    user_msg_id INTEGER,
    draft_parent_id INTEGER,
    draft_msg_id INTEGER,
    client_instance_id TEXT,
    state TEXT NOT NULL DEFAULT 'launching',
    terminal_state TEXT,
    acquired_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    released_at REAL,
    FOREIGN KEY (conv_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (user_msg_id) REFERENCES messages(id) ON DELETE SET NULL,
    FOREIGN KEY (draft_msg_id) REFERENCES messages(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_live_generation_lease
    ON conversation_generation_leases(conv_id) WHERE released_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_generation_leases_conv_time
    ON conversation_generation_leases(conv_id, acquired_at DESC);

-- Loom-managed child agent runs.  These are deliberately separate from the
-- conversation tree: a child can produce work without appending messages to
-- the parent's branch.  The supervisor owns their lifecycle independently of
-- the model turn that requested them.
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    conversation_id INTEGER NOT NULL,
    parent_run_id TEXT,
    parent_message_id INTEGER,
    loom_thread_id TEXT,
    task TEXT NOT NULL,
    image_request TEXT,
    requested_harness TEXT,
    requested_model TEXT,
    model_profile TEXT,
    effective_harness TEXT,
    effective_model TEXT,
    project_dir TEXT,
    path_scope TEXT NOT NULL DEFAULT '[]',
    access TEXT,
    queue_reason TEXT,
    released_result TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    pid INTEGER,
    provider_session_id TEXT,
    client_instance_id TEXT,
    result TEXT,
    error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    started_at REAL,
    heartbeat_at REAL,
    completed_at REAL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_message_id) REFERENCES messages(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_conv ON agent_runs(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_parent ON agent_runs(parent_run_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status, heartbeat_at);

CREATE TABLE IF NOT EXISTS agent_events (
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    PRIMARY KEY (run_id, sequence),
    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_agent_events_created ON agent_events(run_id, created_at);

-- Child checkpoints/retrieval own an event prefix, never a parent message tree.
CREATE TABLE IF NOT EXISTS agent_context_checkpoints (
    run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    summary_route TEXT NOT NULL,
    covered_ids TEXT NOT NULL,
    summary TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    PRIMARY KEY(run_id, fingerprint, summary_route)
);
CREATE TABLE IF NOT EXISTS agent_context_scopes (
    token_hash TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    event_sequence INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    owner_id TEXT NOT NULL
);
"""


def _is_sqlite_corruption_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "database disk image is malformed" in text
        or "database is corrupt" in text
    )


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db_shutdown:
        raise RuntimeError("Loom database is closed for server shutdown")
    if _db is None:
        print("[DB] Opening new shared connection")
        # Autocommit prevents a cancelled coroutine from leaving the shared
        # process-wide connection inside an implicit write transaction between
        # execute() and a later commit(). Multi-statement atomic writes use a
        # dedicated transaction connection below.
        _db = await aiosqlite.connect(DB_PATH, isolation_level=None)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA foreign_keys=ON")
        await _db.execute("PRAGMA busy_timeout=30000")
    else:
        try:
            await _db.execute("SELECT 1")
        except (ValueError, Exception) as e:
            print(f"[DB] Shared connection dead ({e}), reconnecting...")
            try:
                await _db.close()
            except Exception:
                pass
            _db = await aiosqlite.connect(DB_PATH, isolation_level=None)
            _db.row_factory = aiosqlite.Row
            await _db.execute("PRAGMA journal_mode=WAL")
            await _db.execute("PRAGMA foreign_keys=ON")
            await _db.execute("PRAGMA busy_timeout=30000")
            print("[DB] Reconnected successfully")
    return _db


# --- Durable, resumable preparation operations -----------------------------
# A preparation operation survives process/server crashes: the rolling state,
# covered cursor, call budget and terminal reason are recorded after every
# accepted chunk, so a restart resumes instead of repeating paid calls.

async def create_preparation_operation(operation_id: str, *, owner_run_id, conversation_id,
                                       source_revision: str, destination: str,
                                       covered_cursor: str, call_budget: int,
                                       calls_used: int = 0) -> None:
    """Create/resume an operation record. `calls_used` carries the consumed
    budget across crash resumes; it must never be reset to zero."""
    db = await get_db()
    now = time.time()
    await db.execute(
        "INSERT OR REPLACE INTO preparation_operations "
        "(operation_id, owner_run_id, conversation_id, source_revision, destination, phase, "
        " covered_cursor, accepted_state, call_budget, calls_used, created_at, updated_at, terminal_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (operation_id, owner_run_id, conversation_id, source_revision, destination,
         "running", covered_cursor, None, int(call_budget), int(calls_used), now, now, None))


async def advance_preparation_operation(operation_id: str, *, covered_cursor: str,
                                        accepted_state: str, calls_used: int) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE preparation_operations SET covered_cursor=?, accepted_state=?, "
        "calls_used=?, phase='running', updated_at=? WHERE operation_id=?",
        (covered_cursor, accepted_state, int(calls_used), time.time(), operation_id))


async def complete_preparation_operation(operation_id: str, *, accepted_state: str,
                                         calls_used: int) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE preparation_operations SET phase='completed', accepted_state=?, calls_used=?, "
        "terminal_reason=NULL, updated_at=? WHERE operation_id=?",
        (accepted_state, int(calls_used), time.time(), operation_id))


async def fail_preparation_operation(operation_id: str, *, terminal_reason: str,
                                     calls_used: int) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE preparation_operations SET phase='failed', terminal_reason=?, calls_used=?, "
        "updated_at=? WHERE operation_id=?",
        (terminal_reason, int(calls_used), time.time(), operation_id))


async def find_resumable_preparation(conversation_id, source_revision: str, destination: str):
    """Newest non-terminal operation bound to conversation + revision + destination."""
    db = await get_db()
    async with db.execute(
        "SELECT * FROM preparation_operations WHERE conversation_id IS ? "
        "AND source_revision=? AND destination=? "
        "AND phase='running' AND accepted_state IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
        (conversation_id, source_revision, destination)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def claim_preparation_call(operation_id: str, *, remaining_calls: int) -> bool:
    """Atomically claim one provider call slot for the operation.

    The consumed count lives in SQLite, so a crash-resumed operation keeps
    its consumption and concurrent workers cannot over-claim the remainder.
    Returns False when no slot remains."""
    db = await get_db()
    async with db.execute(
        "SELECT calls_used, call_budget, phase FROM preparation_operations "
        "WHERE operation_id=?", (operation_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return True  # durability telemetry only; in-memory budget governs
    used, budget, phase = row[0] or 0, row[1] or 0, row[2]
    if phase != "running":
        return False
    if budget and used >= budget:
        return False
    await db.execute(
        "UPDATE preparation_operations SET calls_used=?, updated_at=? "
        "WHERE operation_id=?", (used + 1, time.time(), operation_id))
    return True


async def close_db():
    global _db
    if _db:
        connection = _db
        _db = None
        try:
            in_transaction = connection.in_transaction
        except ValueError:
            in_transaction = False
        if in_transaction:
            print("[DB] Rolling back open transaction before close")
            try:
                await connection.rollback()
            except Exception as e:
                print(f"[DB] Rollback before close failed: {e}")
        print("[DB] Checkpointing WAL before close...")
        try:
            await connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:
            print(f"[DB] WAL checkpoint failed: {e}")
        print("[DB] Closing shared connection")
        try:
            await connection.close()
        except ValueError:
            pass


def begin_shutdown() -> None:
    """Prevent late generation callbacks from reopening SQLite during exit."""
    global _db_shutdown
    _db_shutdown = True


@asynccontextmanager
async def write_transaction():
    """Cancellation-safe, isolated transaction for multi-statement writes.

    The normal shared connection runs in autocommit mode. A dedicated
    connection ensures unrelated coroutines cannot accidentally join this
    transaction while it spans awaits.
    """
    if _db_shutdown:
        raise RuntimeError("Loom database is closed for server shutdown")
    connection = await aiosqlite.connect(DB_PATH, isolation_level=None)
    transaction_started = False
    try:
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys=ON")
        await connection.execute("PRAGMA busy_timeout=30000")
        await connection.execute("BEGIN IMMEDIATE")
        transaction_started = True
        try:
            yield connection
        except BaseException:
            # Cancellation is caught here before cleanup; the rollback
            # completes before cancellation propagates.
            await asyncio.shield(connection.rollback())
            transaction_started = False
            raise
        else:
            await asyncio.shield(connection.commit())
            transaction_started = False
    finally:
        if transaction_started:
            try:
                await asyncio.shield(connection.rollback())
            except Exception:
                pass
        await asyncio.shield(connection.close())


async def save_pending_permission(
    request_id: str,
    conv_id: int,
    tool_name: str,
    tool_input: dict,
    input_summary: str,
    started_at: float = None,
    *,
    run_id: str | None = None,
    parent_id: int | None = None,
    draft_msg_id: int | None = None,
    client_instance_id: str | None = None,
    permission_scope: str | None = None,
    source: str | None = None,
) -> bool:
    """Insert a new pending permission request."""
    db = await get_db()
    if started_at is None:
        started_at = time.time()
    tool_input_json = json.dumps(tool_input) if tool_input else None
    input_summary_json = json.dumps(input_summary) if input_summary else None
    await db.execute(
        """INSERT INTO pending_permissions
           (request_id, conv_id, tool_name, tool_input, input_summary, started_at,
            run_id, parent_id, draft_msg_id, client_instance_id, permission_scope,
            source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            request_id,
            conv_id,
            tool_name,
            tool_input_json,
            input_summary_json,
            started_at,
            run_id,
            parent_id,
            draft_msg_id,
            client_instance_id,
            permission_scope,
            source,
        ),
    )
    await db.commit()
    return True


async def get_pending_permission(request_id: str) -> dict | None:
    """Get a pending permission request by ID."""

    db = await get_db()
    cursor = await db.execute(
        "SELECT conv_id, tool_name, tool_input, input_summary, started_at, run_id, parent_id, draft_msg_id, client_instance_id, permission_scope, source FROM pending_permissions WHERE request_id = ?",
        (request_id,),
    )
    row = await cursor.fetchone()
    if row:
        tool_input = json.loads(row["tool_input"]) if row["tool_input"] else None
        input_summary = (
            json.loads(row["input_summary"]) if row["input_summary"] else None
        )
        return {
            "conv_id": row["conv_id"],
            "tool_name": row["tool_name"],
            "tool_input": tool_input,
            "input_summary": input_summary,
            "started_at": row["started_at"],
            "run_id": row["run_id"],
            "parent_id": row["parent_id"],
            "draft_msg_id": row["draft_msg_id"],
            "client_instance_id": row["client_instance_id"],
            "permission_scope": row["permission_scope"],
            "source": row["source"],
        }
    return None


async def delete_pending_permission(request_id: str) -> bool:
    """Delete a pending permission request."""
    db = await get_db()
    await db.execute(
        "DELETE FROM pending_permissions WHERE request_id = ?", (request_id,)
    )
    await db.commit()
    return True


async def save_permission_resolution(
    request_id: str,
    conv_id: int,
    tool_name: str,
    allowed: bool,
    feedback: str = "",
    *,
    source: str = "",
    decision_source: str = "user",
    run_id: str | None = None,
    parent_id: int | None = None,
    draft_msg_id: int | None = None,
    permission_scope: str | None = None,
    resolved_at: float | None = None,
    decision_channel: str | None = None,
) -> bool:
    """Persist one user, policy, or lifecycle permission outcome."""
    db = await get_db()
    await db.execute(
        """INSERT OR REPLACE INTO permission_resolutions
           (request_id, conv_id, tool_name, allowed, feedback, source,
            decision_source, run_id, parent_id, draft_msg_id, permission_scope,
            resolved_at, decision_channel)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(request_id),
            int(conv_id),
            str(tool_name or "Unknown"),
            1 if allowed else 0,
            str(feedback or "")[:4000],
            str(source or ""),
            str(decision_source or "user"),
            run_id,
            parent_id,
            draft_msg_id,
            permission_scope,
            float(resolved_at if resolved_at is not None else time.time()),
            decision_channel,
        ),
    )
    await db.commit()
    return True


async def get_permission_resolutions_for_draft(draft_msg_id: int) -> list[dict]:
    """Return decisions made during one assistant draft in resolution order."""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT request_id, conv_id, tool_name, allowed, feedback, source,
                  decision_source, run_id, parent_id, draft_msg_id,
                  permission_scope, resolved_at
           FROM permission_resolutions
           WHERE draft_msg_id = ?
           ORDER BY resolved_at, request_id""",
        (int(draft_msg_id),),
    )
    return [
        {
            **dict(row),
            "allowed": bool(row["allowed"]),
        }
        for row in rows
    ]


async def save_permission_policy_audit(
    request_id: str,
    conv_id: int,
    record: dict,
    *,
    run_id: str | None = None,
    parent_id: int | None = None,
    draft_msg_id: int | None = None,
    permission_scope: str | None = None,
    created_at: float | None = None,
) -> bool:
    """Persist a sanitized shadow-policy comparison for later evaluation."""
    action = dict(record.get("action") or {})
    safe_action = {
        key: action.get(key)
        for key in (
            "effect",
            "boundary",
            "valid",
            "warned",
            "rule_matched",
            "harness",
            "tool_name",
        )
    }
    if "destructive" in action:
        safe_action["destructive"] = bool(action["destructive"])
    if "effective_decision" in record:
        effective = record["effective_decision"]
        safe_action["effective_decision"] = effective if effective in {
            "auto_allow", "ask", "ask_with_warning", "invalid_request"
        } else "invalid_request"
        safe_action["integrity_review"] = bool(record.get("integrity_review"))
        safe_action["integrity_blocked"] = bool(record.get("integrity_blocked"))
    db = await get_db()
    await db.execute(
        """INSERT OR REPLACE INTO permission_policy_audits
           (request_id, conv_id, run_id, parent_id, draft_msg_id,
            permission_scope, harness, tool_name, profile, legacy_outcome,
            policy_decision, disagrees, action_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(request_id),
            int(conv_id),
            run_id,
            parent_id,
            draft_msg_id,
            permission_scope,
            str(safe_action.get("harness") or ""),
            str(safe_action.get("tool_name") or ""),
            str(record.get("profile") or "balanced"),
            str(record.get("legacy_outcome") or "unknown"),
            str(record.get("decision") or "invalid_request"),
            1 if record.get("disagrees") else 0,
            json.dumps(safe_action, ensure_ascii=False, sort_keys=True),
            float(created_at if created_at is not None else time.time()),
        ),
    )
    await db.commit()
    return True


async def get_permission_policy_audits(conv_id: int) -> list[dict]:
    """Return sanitized shadow-policy comparisons in observation order."""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT request_id, conv_id, run_id, parent_id, draft_msg_id,
                  permission_scope, harness, tool_name, profile,
                  legacy_outcome, policy_decision, disagrees, action_json,
                  created_at
           FROM permission_policy_audits
           WHERE conv_id = ?
           ORDER BY created_at, request_id""",
        (int(conv_id),),
    )
    return [
        {
            **dict(row),
            "disagrees": bool(row["disagrees"]),
            "action": json.loads(row["action_json"]),
        }
        for row in rows
    ]


async def delete_old_pending_permissions(before: float) -> int:
    """Delete pending permissions older than before timestamp."""
    db = await get_db()
    await db.execute("DELETE FROM pending_permissions WHERE started_at < ?", (before,))
    deleted = db.total_changes  # aiosqlite tracks changes in connection
    await db.commit()
    return deleted


async def get_permission_notices() -> list[dict]:
    """Identity-only pending notices for the global bell; no approval payload."""
    connection = await get_db()
    rows = await connection.execute_fetchall(
        "SELECT request_id, conv_id, COALESCE(draft_msg_id, parent_id) AS message_id "
        "FROM pending_permissions ORDER BY started_at"
    )
    return [dict(row) for row in rows]


async def get_pending_permissions_for_conv(conv_id: int) -> list[dict]:
    """Get all pending permissions for a conversation."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT request_id, tool_name, tool_input, input_summary, started_at, run_id, parent_id, draft_msg_id, client_instance_id, permission_scope, source FROM pending_permissions WHERE conv_id = ? ORDER BY started_at",
        (conv_id,),
    )
    result = []
    for row in rows:
        result.append(
            {
                "request_id": row["request_id"],
                "tool_name": row["tool_name"],
                "tool_input": (
                    json.loads(row["tool_input"]) if row["tool_input"] else None
                ),
                "input_summary": (
                    json.loads(row["input_summary"]) if row["input_summary"] else None
                ),
                "started_at": row["started_at"],
                "run_id": row["run_id"],
                "parent_id": row["parent_id"],
                "draft_msg_id": row["draft_msg_id"],
                "client_instance_id": row["client_instance_id"],
                "permission_scope": row["permission_scope"],
                "source": row["source"],
            }
        )
    return result


async def mark_permission_broadcasted(request_id: str) -> bool:
    """Mark that a permission request has been broadcast."""
    db = await get_db()
    await db.execute(
        "UPDATE pending_permissions SET broadcast_sent = 1 WHERE request_id = ?",
        (request_id,),
    )
    await db.commit()
    return True


async def delete_old_pending_permissions(before: float) -> int:
    """Delete pending permissions older than before timestamp."""
    db = await get_db()
    await db.execute("DELETE FROM pending_permissions WHERE started_at < ?", (before,))
    deleted = db.total_changes  # aiosqlite tracks changes in connection
    await db.commit()
    return deleted


async def init_db():
    global _db_shutdown
    _db_shutdown = False
    db = await get_db()
    await db.executescript(SCHEMA)
    await _run_migrations(db)
    await _install_search_revisions(db)
    await db.commit()


async def _install_search_revisions(connection):
    """Cheap revision counters cover edits/deletions, including direct SQL writes."""
    await connection.executescript("""
        CREATE TABLE IF NOT EXISTS search_revisions (
            conversation_id INTEGER PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL DEFAULT 1
        );
        INSERT OR IGNORE INTO search_revisions(conversation_id) SELECT id FROM conversations;
        CREATE TRIGGER IF NOT EXISTS search_conversation_insert AFTER INSERT ON conversations BEGIN
            INSERT OR IGNORE INTO search_revisions(conversation_id) VALUES(new.id);
        END;
        CREATE TRIGGER IF NOT EXISTS search_conversation_update
        AFTER UPDATE OF title, mode, project_dir, backstage_parent_id ON conversations BEGIN
            UPDATE search_revisions SET revision=revision+1 WHERE conversation_id=new.id;
        END;
        CREATE TRIGGER IF NOT EXISTS search_message_insert AFTER INSERT ON messages BEGIN
            UPDATE search_revisions SET revision=revision+1 WHERE conversation_id=new.conversation_id;
        END;
        CREATE TRIGGER IF NOT EXISTS search_message_update
        AFTER UPDATE OF content, content_blocks, parent_id, conversation_id, role, loom_thread_id ON messages BEGIN
            UPDATE search_revisions SET revision=revision+1
            WHERE conversation_id IN (old.conversation_id, new.conversation_id);
        END;
        CREATE TRIGGER IF NOT EXISTS search_message_delete AFTER DELETE ON messages BEGIN
            UPDATE search_revisions SET revision=revision+1 WHERE conversation_id=old.conversation_id;
        END;
        CREATE TRIGGER IF NOT EXISTS search_thread_update
        AFTER UPDATE OF root_message_id ON loom_threads BEGIN
            UPDATE search_revisions SET revision=revision+1 WHERE conversation_id=new.conversation_id;
        END;
    """)


async def _run_migrations(db):
    """Add new columns idempotently via ALTER TABLE."""
    migrations = [
        # Conversations: Claude mode fields
        "ALTER TABLE conversations ADD COLUMN mode TEXT NOT NULL DEFAULT 'weave'",
        "ALTER TABLE conversations ADD COLUMN project_dir TEXT",
        "ALTER TABLE conversations ADD COLUMN claude_session_id TEXT",
        "ALTER TABLE conversations ADD COLUMN total_cost_usd REAL DEFAULT 0",
        # Messages: Claude content blocks + cost tracking
        "ALTER TABLE messages ADD COLUMN content_blocks TEXT",
        # NULL preserves legacy image rendering; new replies explicitly select presentations.
        "ALTER TABLE messages ADD COLUMN chat_items TEXT",
        "ALTER TABLE messages ADD COLUMN turn_cost_usd REAL",
        "ALTER TABLE messages ADD COLUMN turn_input_tokens INTEGER",
        "ALTER TABLE messages ADD COLUMN turn_output_tokens INTEGER",
        # Phase 2: model & effort selection
        "ALTER TABLE conversations ADD COLUMN cc_model TEXT DEFAULT 'sonnet'",
        "ALTER TABLE conversations ADD COLUMN cc_effort TEXT DEFAULT 'high'",
        # Phase 3: starred & folders
        "ALTER TABLE conversations ADD COLUMN starred INTEGER DEFAULT 0",
        "ALTER TABLE conversations ADD COLUMN folder TEXT DEFAULT ''",
        "ALTER TABLE conversations ADD COLUMN nsfw INTEGER DEFAULT 0",
        # Phase 4: session resume — store CC session_id per message node
        "ALTER TABLE messages ADD COLUMN cc_session_id TEXT",
        # Local mode: store selected llama model per conversation
        "ALTER TABLE conversations ADD COLUMN local_model TEXT",
        # Permission mode (default, plan, auto, etc.)
        "ALTER TABLE conversations ADD COLUMN cc_permission_mode TEXT DEFAULT 'default'",
        "ALTER TABLE conversations ADD COLUMN permission_profile TEXT DEFAULT 'balanced'",
        # Incognito flag — routes Hermes turns to Prometheus (incognito, always-warm,
        # cloud-fallback) instead of the ensouled attendant. Orthogonal to cc_model,
        # like cc_permission_mode. 0 = ensouled (default), 1 = incognito.
        "ALTER TABLE conversations ADD COLUMN incognito INTEGER DEFAULT 0",
        # Bookmarked message — auto-navigate to this on conversation load (deprecated)
        "ALTER TABLE conversations ADD COLUMN bookmark_msg_id INTEGER",
        # OODA harness toggle
        "ALTER TABLE conversations ADD COLUMN ooda_enabled INTEGER DEFAULT 0",
        # Minimal Weave: skip character/state scaffolding and use only this
        # conversation-level system message plus branch history.
        "ALTER TABLE conversations ADD COLUMN system_only INTEGER DEFAULT 0",
        "ALTER TABLE conversations ADD COLUMN minimal_ooda_enabled INTEGER DEFAULT 1",
        "ALTER TABLE conversations ADD COLUMN system_prompt TEXT",
        # Tier 3: branch-level state deltas stored per assistant message
        "ALTER TABLE messages ADD COLUMN state_deltas TEXT",
        # Track which model generated each message (for provider switch detection)
        "ALTER TABLE messages ADD COLUMN cc_model_used TEXT",
        # Provider-returned model identity chain. JSON kept separate from
        # cc_model_used so a requested value can never masquerade as proof.
        "ALTER TABLE messages ADD COLUMN model_attestation TEXT",
        # Canvas view: embedded website preview in tree view
        "ALTER TABLE conversations ADD COLUMN canvas_enabled INTEGER DEFAULT 0",
        # Vision model describe focus (sent only to vision model, never chat)
        "ALTER TABLE messages ADD COLUMN describe_context TEXT",
        # Backstage: child conversation dedicated to editing this conv's state cards via an LLM agent
        "ALTER TABLE conversations ADD COLUMN backstage_parent_id INTEGER",
        # Persisted generation duration so the M:SS timer survives a refresh.
        "ALTER TABLE messages ADD COLUMN generation_ms INTEGER",
        # Time from durable draft creation/stream_start to the first provider
        # output event. This makes slow native launch versus history rebuilds
        # visible after the turn rather than only in transient server logs.
        "ALTER TABLE messages ADD COLUMN time_to_first_token_ms INTEGER",
        # Canvas slug: human-readable shortcut for direct canvas access
        "ALTER TABLE conversations ADD COLUMN canvas_slug TEXT",
        # Bluesky access token for canvas proxy auth
        "ALTER TABLE conversations ADD COLUMN bsky_token TEXT",

        # Mode tag companion to cc_session_id so the resume walk can skip
        # session IDs issued by a different backend (e.g. a Hermes session id
        # on a branch later run as Dream). NULL = legacy/unscoped and rebuilds
        # unless independent attestation below proves which harness owns it.
        "ALTER TABLE messages ADD COLUMN cc_session_mode TEXT",
        # Codex app-server goal state. Stored in Loom too because Loom forks
        # Codex threads per branch turn and may need to reapply the active goal.
        "ALTER TABLE conversations ADD COLUMN codex_goal_objective TEXT",
        "ALTER TABLE conversations ADD COLUMN codex_goal_status TEXT",
        "ALTER TABLE conversations ADD COLUMN codex_goal_token_budget INTEGER",
        "ALTER TABLE conversations ADD COLUMN codex_goal_tokens_used INTEGER DEFAULT 0",
        "ALTER TABLE conversations ADD COLUMN codex_goal_time_used_seconds INTEGER DEFAULT 0",
        "ALTER TABLE conversations ADD COLUMN codex_goal_updated_at REAL",
        # Explicit opt-in for independent agents sharing one live checkout.
        "ALTER TABLE conversations ADD COLUMN parallel_agents_enabled INTEGER DEFAULT 0",
        # Run-scoped permission routing.  These fields prevent reconnects from
        # replaying a tool approval into an unrelated conversation/window.
        "ALTER TABLE pending_permissions ADD COLUMN run_id TEXT",
        "ALTER TABLE pending_permissions ADD COLUMN parent_id INTEGER",
        "ALTER TABLE pending_permissions ADD COLUMN draft_msg_id INTEGER",
        "ALTER TABLE pending_permissions ADD COLUMN client_instance_id TEXT",
        "ALTER TABLE pending_permissions ADD COLUMN permission_scope TEXT",
        "ALTER TABLE pending_permissions ADD COLUMN source TEXT",
        "ALTER TABLE permission_resolutions ADD COLUMN decision_source TEXT DEFAULT 'user'",
        # Which authenticated surface answered (browser page, admin relay).
        "ALTER TABLE permission_resolutions ADD COLUMN decision_channel TEXT",
        "ALTER TABLE agent_runs ADD COLUMN client_instance_id TEXT",
        # Per-send provenance. These fields let us prove which browser window
        # and branch cursor authored a message without making either one a
        # conversation-global navigation authority.
        "ALTER TABLE messages ADD COLUMN origin_client_id TEXT",
        "ALTER TABLE messages ADD COLUMN target_anchor_id INTEGER",
        "ALTER TABLE messages ADD COLUMN send_intent TEXT",
        "ALTER TABLE messages ADD COLUMN loom_thread_id TEXT",
        # A browser retry reuses this key so the server returns the already
        # stored row instead of advancing the cursor or launching twice.
        "ALTER TABLE messages ADD COLUMN client_request_id TEXT",
        # Durable terminal distinction for user-directed generation stops.
        # NULL is legacy/completed behavior; interrupted retains native
        # provider context, while cancelled deliberately clears it.
        "ALTER TABLE messages ADD COLUMN turn_lifecycle_state TEXT",
        # Every provider-created assistant draft records the exact serial-lane
        # lease that authorized it.  This is topology provenance as well as a
        # database-enforced backstop against alternate/unleased launch paths.
        "ALTER TABLE messages ADD COLUMN generation_lease_id TEXT",
        "ALTER TABLE active_generations ADD COLUMN loom_thread_id TEXT",
        "ALTER TABLE queued_generations ADD COLUMN loom_thread_id TEXT",
        "ALTER TABLE queued_generations ADD COLUMN resume_authorized INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE agent_runs ADD COLUMN loom_thread_id TEXT",
        "ALTER TABLE agent_runs ADD COLUMN image_request TEXT",
        "ALTER TABLE agent_runs ADD COLUMN access TEXT",
        "ALTER TABLE agent_runs ADD COLUMN queue_reason TEXT",
        "ALTER TABLE agent_runs ADD COLUMN released_result TEXT",
        "ALTER TABLE context_retrieval_scopes ADD COLUMN owner_id TEXT",
        "ALTER TABLE context_retrieval_scopes ADD COLUMN frozen_tail TEXT",
        "ALTER TABLE conversation_generation_leases ADD COLUMN draft_parent_id INTEGER",
    ]
    for sql in migrations:
        try:
            await db.execute(sql)
        except Exception:
            pass  # Column already exists

    # A non-parallel agent provider is not allowed to create even its durable
    # assistant draft unless the insert names the one live lease authorized
    # for that exact parent.  This trigger protects the invariant even if a
    # future HTTP/WebSocket/scheduler path accidentally bypasses the normal
    # Python launch helper.
    await db.execute("DROP TRIGGER IF EXISTS enforce_serial_agent_draft_lease")
    await db.execute(
        """CREATE TRIGGER enforce_serial_agent_draft_lease
           BEFORE INSERT ON messages
           WHEN NEW.role = 'assistant'
             AND COALESCE(NEW.content, '') = ''
             AND EXISTS (
                 SELECT 1 FROM conversations c
                 WHERE c.id = NEW.conversation_id
                   AND c.mode IN (
                       'claude','local','hermes','dream','dream-claude',
                       'gemini','codex','goose','umans','openrouter'
                   )
                   AND COALESCE(c.parallel_agents_enabled, 0) = 0
             )
             AND NOT EXISTS (
                 SELECT 1 FROM conversation_generation_leases lease
                 WHERE lease.lease_id = NEW.generation_lease_id
                   AND lease.conv_id = NEW.conversation_id
                   AND lease.draft_parent_id = NEW.parent_id
                   AND lease.released_at IS NULL
                   AND lease.state IN ('launching', 'running')
             )
           BEGIN
               SELECT RAISE(ABORT, 'assistant draft requires matching generation lease');
           END"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_generation_lease "
        "ON messages(generation_lease_id)"
    )

    # Some cancelled Codex turns saved the native session ID and attestation
    # but omitted the companion harness mode. The attestation source is
    # independent evidence of ownership, so these rows can be scoped safely;
    # otherwise every following turn rebuilds the entire Loom branch.
    await db.execute(
        """UPDATE messages
           SET cc_session_mode = 'codex'
           WHERE cc_session_mode IS NULL
             AND cc_session_id IS NOT NULL
             AND model_attestation LIKE '%codex_app_server_thread_response%'"""
    )

    # Table-level migrations (CREATE IF NOT EXISTS is idempotent)
    table_migrations = [
        """CREATE TABLE IF NOT EXISTS bookmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            branch_name TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
            UNIQUE(conversation_id, message_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_bookmarks_conv ON bookmarks(conversation_id)",
        """CREATE TABLE IF NOT EXISTS branch_pins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
            UNIQUE(conversation_id, message_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_branch_pins_conv ON branch_pins(conversation_id)",
        """CREATE TABLE IF NOT EXISTS state_schemas (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            fields TEXT NOT NULL,
            is_builtin INTEGER DEFAULT 0,
            created_at REAL NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS state_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            schema_id TEXT NOT NULL,
            label TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}',
            is_readonly INTEGER DEFAULT 0,
            updated_at REAL NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
            UNIQUE(conversation_id, schema_id, label)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_state_cards_conv ON state_cards(conversation_id)",
        """CREATE TABLE IF NOT EXISTS character_state_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            character_id TEXT NOT NULL,
            schema_id TEXT NOT NULL,
            label TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}',
            is_readonly INTEGER DEFAULT 0,
            updated_at REAL NOT NULL,
            UNIQUE(character_id, schema_id, label)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_char_state_cards ON character_state_cards(character_id)",
        # Modules: pluggable skills, commands, and agent definitions
        """CREATE TABLE IF NOT EXISTS modules (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('skill','command','agent')),
            description TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'builtin',
            config TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            discovered_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""",
        # Prevent two concurrent POSTs from creating two backstage children
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_backstage_parent ON conversations(backstage_parent_id) WHERE backstage_parent_id IS NOT NULL",
        # Canvas slug uniqueness (only enforced when slug is set)
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_conv_canvas_slug ON conversations(canvas_slug) WHERE canvas_slug IS NOT NULL",
        """CREATE TABLE IF NOT EXISTS queued_generations (
            user_msg_id INTEGER PRIMARY KEY,
            conv_id INTEGER NOT NULL,
            wait_for_draft_id INTEGER NOT NULL,
            client_instance_id TEXT,
            settings_json TEXT NOT NULL DEFAULT '{}',
            generation_count INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'queued',
            resume_authorized INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (user_msg_id) REFERENCES messages(id) ON DELETE CASCADE,
            FOREIGN KEY (conv_id) REFERENCES conversations(id) ON DELETE CASCADE,
            FOREIGN KEY (wait_for_draft_id) REFERENCES messages(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_queued_gen_conv ON queued_generations(conv_id, status)",
        "DROP INDEX IF EXISTS idx_queued_gen_wait",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_queued_gen_wait ON queued_generations(wait_for_draft_id) WHERE status IN ('queued', 'paused', 'launching')",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_client_request ON messages(conversation_id, client_request_id) WHERE client_request_id IS NOT NULL",
        """CREATE TABLE IF NOT EXISTS conversation_generation_leases (
            lease_id TEXT PRIMARY KEY,
            conv_id INTEGER NOT NULL,
            run_id TEXT,
            user_msg_id INTEGER,
            draft_parent_id INTEGER,
            draft_msg_id INTEGER,
            client_instance_id TEXT,
            state TEXT NOT NULL DEFAULT 'launching',
            terminal_state TEXT,
            acquired_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            released_at REAL,
            FOREIGN KEY (conv_id) REFERENCES conversations(id) ON DELETE CASCADE,
            FOREIGN KEY (user_msg_id) REFERENCES messages(id) ON DELETE SET NULL,
            FOREIGN KEY (draft_msg_id) REFERENCES messages(id) ON DELETE SET NULL
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_live_generation_lease ON conversation_generation_leases(conv_id) WHERE released_at IS NULL",
        "CREATE INDEX IF NOT EXISTS idx_generation_leases_conv_time ON conversation_generation_leases(conv_id, acquired_at DESC)",
        """CREATE TABLE IF NOT EXISTS loom_threads (
            thread_id TEXT PRIMARY KEY,
            conversation_id INTEGER NOT NULL,
            root_message_id INTEGER,
            created_by_client_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
            FOREIGN KEY (root_message_id) REFERENCES messages(id) ON DELETE SET NULL,
            UNIQUE(conversation_id, root_message_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_loom_threads_conv ON loom_threads(conversation_id, created_at)",
        """CREATE TABLE IF NOT EXISTS loom_thread_cursors (
            cursor_id TEXT PRIMARY KEY,
            conversation_id INTEGER NOT NULL,
            thread_id TEXT NOT NULL,
            client_instance_id TEXT NOT NULL,
            leaf_message_id INTEGER,
            revision INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
            FOREIGN KEY (thread_id) REFERENCES loom_threads(thread_id) ON DELETE CASCADE,
            FOREIGN KEY (leaf_message_id) REFERENCES messages(id) ON DELETE SET NULL,
            UNIQUE(conversation_id, client_instance_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_loom_cursors_thread ON loom_thread_cursors(thread_id, updated_at)",
    ]
    for sql in table_migrations:
        await db.execute(sql)

    await _backfill_loom_threads(db)
    await db.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_one_thread_root
           ON messages(loom_thread_id)
           WHERE parent_id IS NULL AND loom_thread_id IS NOT NULL"""
    )

    # Seed builtin state schemas
    await _seed_builtin_schemas(db)


async def _backfill_loom_threads(db):
    """Assign existing message subtrees to stable deterministic Loom threads.

    Parent links and message IDs are untouched, so branch labels, pins, and
    provider sessions retain their exact historical meaning.
    """
    rows = await db.execute_fetchall(
        "SELECT id, conversation_id, parent_id, created_at, loom_thread_id FROM messages ORDER BY id"
    )
    if not rows:
        return
    by_id = {int(row["id"]): row for row in rows}
    root_cache: dict[int, int] = {}

    def root_for(message_id: int) -> int:
        visited = []
        current = message_id
        while current not in root_cache:
            row = by_id.get(current)
            if row is None or row["parent_id"] is None or int(row["parent_id"]) not in by_id:
                root_cache[current] = current
                break
            visited.append(current)
            current = int(row["parent_id"])
        root = root_cache[current]
        for item in visited:
            root_cache[item] = root
        return root

    thread_for_root: dict[tuple[int, int], str] = {}
    for row in rows:
        if row["loom_thread_id"]:
            key = (
                int(row["conversation_id"]),
                root_for(int(row["id"])),
            )
            thread_for_root.setdefault(key, str(row["loom_thread_id"]))
    for row in rows:
        conv_id = int(row["conversation_id"])
        root_id = root_for(int(row["id"]))
        key = (conv_id, root_id)
        thread_id = thread_for_root.setdefault(key, f"loom:{conv_id}:{root_id}")
        root_row = by_id[root_id]
        created_at = float(root_row["created_at"] or time.time())
        await db.execute(
            """INSERT OR IGNORE INTO loom_threads
               (thread_id, conversation_id, root_message_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (thread_id, conv_id, root_id, created_at, created_at),
        )
        if not row["loom_thread_id"]:
            await db.execute(
                "UPDATE messages SET loom_thread_id = ? WHERE id = ?",
                (thread_id, int(row["id"])),
            )

    await db.execute(
        """UPDATE active_generations
           SET loom_thread_id = (
               SELECT loom_thread_id FROM messages
               WHERE messages.id = active_generations.draft_msg_id
           )
           WHERE loom_thread_id IS NULL"""
    )
    await db.execute(
        """UPDATE queued_generations
           SET loom_thread_id = (
               SELECT loom_thread_id FROM messages
               WHERE messages.id = queued_generations.user_msg_id
           )
           WHERE loom_thread_id IS NULL"""
    )
    await db.execute(
        """UPDATE agent_runs
           SET loom_thread_id = (
               SELECT loom_thread_id FROM messages
               WHERE messages.id = agent_runs.parent_message_id
           )
           WHERE loom_thread_id IS NULL AND parent_message_id IS NOT NULL"""
    )


BUILTIN_SCHEMAS = [
    {
        "id": "character_state",
        "name": "Character State",
        "fields": json.dumps(
            [
                {
                    "name": "personality",
                    "type": "text",
                    "description": "Core personality traits",
                },
                {
                    "name": "appearance",
                    "type": "text",
                    "description": "Physical appearance",
                },
                {
                    "name": "current_mood",
                    "type": "text",
                    "description": "Current emotional state",
                },
                {
                    "name": "goals",
                    "type": "text",
                    "description": "Active goals and motivations",
                },
                {
                    "name": "relationships",
                    "type": "text",
                    "description": "Key relationships",
                },
                {
                    "name": "physical_situation",
                    "type": "text",
                    "description": "Current physical state and location",
                },
            ]
        ),
    },
    {
        "id": "scene_state",
        "name": "Scene State",
        "fields": json.dumps(
            [
                {"name": "location", "type": "text", "description": "Current location"},
                {"name": "time_of_day", "type": "text", "description": "Time of day"},
                {
                    "name": "atmosphere",
                    "type": "text",
                    "description": "Mood and atmosphere",
                },
                {
                    "name": "present_characters",
                    "type": "text",
                    "description": "Characters in the scene",
                },
                {
                    "name": "recent_events",
                    "type": "text",
                    "description": "What just happened",
                },
            ]
        ),
    },
    {
        "id": "lore",
        "name": "Lore",
        "fields": json.dumps(
            [
                {
                    "name": "content",
                    "type": "text",
                    "description": "Background information",
                },
            ]
        ),
    },
    {
        "id": "persona_state",
        "name": "Persona State",
        "fields": json.dumps(
            [
                {
                    "name": "description",
                    "type": "text",
                    "description": "Who you are in this RP",
                },
                {
                    "name": "appearance",
                    "type": "text",
                    "description": "Your physical appearance",
                },
                {"name": "goals", "type": "text", "description": "Your active goals"},
            ]
        ),
    },
]


async def _seed_builtin_schemas(db):
    """Insert builtin state schemas if they don't exist."""
    now = time.time()
    for schema in BUILTIN_SCHEMAS:
        await db.execute(
            "INSERT OR IGNORE INTO state_schemas (id, name, fields, is_builtin, created_at) VALUES (?, ?, ?, 1, ?)",
            (schema["id"], schema["name"], schema["fields"], now),
        )


# ── Conversations ──


async def create_conversation(
    title: str,
    character_id: str = None,
    mode: str = "weave",
    project_dir: str = None,
    backstage_parent_id: int = None,
) -> dict:
    now = time.time()
    async with write_transaction() as connection:
        # Explicit on insertion: retain the schema's Balanced fallback for
        # legacy rows without silently changing saved conversation choices.
        cursor = await connection.execute(
            "INSERT INTO conversations (title, character_id, mode, project_dir, backstage_parent_id, created_at, updated_at, permission_profile) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (title, character_id, mode, project_dir, backstage_parent_id, now, now, _config.default_permission_profile),
        )
        conv_id = cursor.lastrowid
        # A conversation and its required style-state row are one unit.
        await connection.execute(
            "INSERT INTO style_state (conversation_id) VALUES (?)", (conv_id,)
        )
        row = await connection.execute_fetchall(
            "SELECT * FROM conversations WHERE id = ?", (conv_id,)
        )
        result = dict(row[0])
    return result


class DeletedCasualChatRequest(ValueError):
    pass


async def create_casual_conversation(request_id: str, title: str, mode: str,
                                   project_dir: str, cc_model: str, cc_effort: str) -> dict:
    """Atomically claim a casual-chat request; lost responses never duplicate rows."""
    selection = json.dumps([mode, project_dir, cc_model, cc_effort])
    now = time.time()
    async with write_transaction() as connection:
        deleted = await connection.execute_fetchall(
            "SELECT request_id FROM deleted_casual_chat_requests WHERE request_id = ?", (request_id,)
        )
        if deleted:
            raise DeletedCasualChatRequest("This chat was deleted; start a new chat instead of retrying it")
        existing = await connection.execute_fetchall(
            "SELECT * FROM casual_chat_requests WHERE request_id = ?", (request_id,)
        )
        if existing:
            if existing[0]["selection"] != selection:
                raise ValueError("This chat request was already used with a different model selection")
            rows = await connection.execute_fetchall(
                "SELECT * FROM conversations WHERE id = ?", (existing[0]["conversation_id"],)
            )
            return dict(rows[0])
        cursor = await connection.execute(
            "INSERT INTO conversations (title, mode, project_dir, cc_model, cc_effort, ooda_enabled, created_at, updated_at, permission_profile) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (title, mode, project_dir, cc_model, cc_effort, now, now, _config.default_permission_profile),
        )
        conv_id = cursor.lastrowid
        await connection.execute("INSERT INTO style_state (conversation_id) VALUES (?)", (conv_id,))
        await connection.execute(
            "INSERT INTO casual_chat_requests (request_id, conversation_id, selection) VALUES (?, ?, ?)",
            (request_id, conv_id, selection),
        )
        rows = await connection.execute_fetchall("SELECT * FROM conversations WHERE id = ?", (conv_id,))
        return dict(rows[0])


async def list_conversations() -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM conversations WHERE backstage_parent_id IS NULL ORDER BY starred DESC, updated_at DESC"
    )

    return [dict(r) for r in rows]


async def get_or_create_backstage(parent_conv_id: int) -> dict:
    """Return the backstage child conv for this parent, creating if absent.

    The backstage conv is a normal Loom (Braid/local by default — user can
    switch to Anthropic) with an MCP server that exposes card-editing tools
    scoped to the parent conversation.
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM conversations WHERE backstage_parent_id = ? LIMIT 1",
        (parent_conv_id,),
    )
    if rows:
        return dict(rows[0])
    parent = await get_conversation(parent_conv_id)
    if not parent:
        raise ValueError(f"parent conversation {parent_conv_id} not found")
    title = f"Backstage: {parent.get('title', 'Untitled')}"
    # Sandbox: each backstage gets a neutral empty workspace so the agent
    # doesn't see (and try to refactor) the Loom repo it's hosted in.
    workspace = _os.path.join(_os.getcwd(), "backstage_workspaces", f"parent_{parent_conv_id}")
    _os.makedirs(workspace, exist_ok=True)
    return await create_conversation(
        title=title,
        character_id=parent.get("character_id"),
        mode="claude",
        project_dir=workspace,
        backstage_parent_id=parent_conv_id,
    )


async def search_conversations(query: str, limit: int = 20) -> list[dict]:
    """Search across all conversations by title and message content.
    Excludes orphaned messages (broken parent chain) and empty drafts."""
    db = await get_db()
    pattern = f"%{query}%"
    rows = await db.execute_fetchall(
        """SELECT c.id as conversation_id, c.title, c.mode,
                  m.id as message_id, m.role,
                  substr(m.content,
                         max(1, instr(lower(m.content), lower(?)) - 80),
                         200) as snippet
           FROM messages m
           JOIN conversations c ON c.id = m.conversation_id
           WHERE m.content LIKE ?
             AND m.content IS NOT NULL AND m.content != ''
             AND c.backstage_parent_id IS NULL
             AND (m.parent_id IS NULL
                  OR m.parent_id IN (SELECT id FROM messages))
           UNION
           SELECT c.id as conversation_id, c.title, c.mode,
                  NULL as message_id, NULL as role,
                  c.title as snippet
           FROM conversations c
           WHERE c.title LIKE ? AND c.backstage_parent_id IS NULL AND c.id NOT IN (
               SELECT DISTINCT m2.conversation_id FROM messages m2
               WHERE m2.content LIKE ?
                 AND m2.content IS NOT NULL AND m2.content != ''
           )
           ORDER BY conversation_id DESC
           LIMIT ?""",
        (query, pattern, pattern, pattern, limit),
    )
    return [dict(r) for r in rows]


async def search_conversation_messages(conv_id: int, query: str) -> list[dict]:
    """Search messages within a single conversation.
    Excludes orphaned messages and empty drafts."""
    db = await get_db()
    pattern = f"%{query}%"
    rows = await db.execute_fetchall(
        """SELECT id, role,
                  substr(content,
                         max(1, instr(lower(content), lower(?)) - 80),
                         200) as snippet
           FROM messages
           WHERE conversation_id = ? AND content LIKE ?
             AND content IS NOT NULL AND content != ''
             AND (parent_id IS NULL
                  OR parent_id IN (SELECT id FROM messages WHERE conversation_id = ?))
           ORDER BY created_at""",
        (query, conv_id, pattern, conv_id),
    )
    return [dict(r) for r in rows]


async def get_conversation(conv_id: int) -> Optional[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM conversations WHERE id = ?", (conv_id,)
    )

    return dict(rows[0]) if rows else None


async def get_conversation_by_canvas_slug(slug: str) -> Optional[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM conversations WHERE canvas_slug = ?", (slug,)
    )
    return dict(rows[0]) if rows else None


async def delete_conversation(conv_id: int):
    async with write_transaction() as connection:
        from usage_activity_store import retain_footer_usage
        await retain_footer_usage(connection,
            "conversation_id IN (SELECT id FROM conversations WHERE id=? OR backstage_parent_id=?)",
            (conv_id, conv_id))
        # Keep request identities after FK cascade, including backstage children.
        await connection.execute(
            "INSERT OR IGNORE INTO deleted_casual_chat_requests (request_id) "
            "SELECT request_id FROM casual_chat_requests WHERE conversation_id IN "
            "(SELECT id FROM conversations WHERE id = ? OR backstage_parent_id = ?)",
            (conv_id, conv_id),
        )
        # Delete any backstage child before the parent (no FK cascade here).
        await connection.execute(
            "DELETE FROM conversations WHERE backstage_parent_id = ?", (conv_id,)
        )
        await connection.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))


async def touch_conversation(conv_id: int):
    db = await get_db()
    await db.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?", (time.time(), conv_id)
    )
    await db.commit()


async def save_custom_scene(conv_id: int, scene: str):
    db = await get_db()
    await db.execute(
        "UPDATE conversations SET custom_scene = ? WHERE id = ?", (scene, conv_id)
    )
    await db.commit()


async def update_conversation_fields(conv_id: int, **fields):
    """Update arbitrary fields on a conversation."""
    db = await get_db()
    allowed = {
        "persona_id",
        "lore_ids",
        "style_nudge",
        "custom_scene",
        "title",
        "mode",
        "claude_session_id",
        "total_cost_usd",
        "cc_model",
        "cc_effort",
        "starred",
        "folder",
        "nsfw",
        "local_model",
        "cc_permission_mode",
        "permission_profile",
        "incognito",
        "ooda_enabled",
        "system_only",
        "minimal_ooda_enabled",
        "system_prompt",
        "canvas_enabled",
        "canvas_slug",
        "project_dir",
        "backstage_parent_id",
        "bsky_token",

        "codex_goal_objective",
        "codex_goal_status",
        "codex_goal_token_budget",
        "codex_goal_tokens_used",
        "codex_goal_time_used_seconds",
        "codex_goal_updated_at",
        "parallel_agents_enabled",
    }
    updates = []
    params = []
    for key, val in fields.items():
        if key in allowed:
            updates.append(f"{key} = ?")
            params.append(val)
    if updates:
        params.append(conv_id)
        await db.execute(
            f"UPDATE conversations SET {', '.join(updates)} WHERE id = ?", params
        )
        await db.commit()


# ── Messages ──


class LoomCursorConflict(RuntimeError):
    """A browser attempted to mutate or send from a stale topology cursor."""


async def create_loom_thread(
    conversation_id: int,
    *,
    client_instance_id: str | None = None,
) -> dict:
    """Create a durable empty root thread before its first message exists."""
    db = await get_db()
    now = time.time()
    thread_id = f"loom:{uuid.uuid4()}"
    await db.execute(
        """INSERT INTO loom_threads
           (thread_id, conversation_id, root_message_id, created_by_client_id,
            created_at, updated_at)
           VALUES (?, ?, NULL, ?, ?, ?)""",
        (thread_id, conversation_id, client_instance_id, now, now),
    )
    await db.commit()
    return await get_loom_thread(thread_id)


async def get_loom_thread(thread_id: str) -> dict | None:
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM loom_threads WHERE thread_id = ?", (str(thread_id),)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_loom_threads(conversation_id: int) -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM loom_threads WHERE conversation_id = ? ORDER BY created_at",
        (conversation_id,),
    )
    return [dict(row) for row in rows]


async def get_loom_cursor(cursor_id: str) -> dict | None:
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM loom_thread_cursors WHERE cursor_id = ?", (str(cursor_id),)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_client_loom_cursor(
    conversation_id: int, client_instance_id: str
) -> dict | None:
    db = await get_db()
    cursor = await db.execute(
        """SELECT * FROM loom_thread_cursors
           WHERE conversation_id = ? AND client_instance_id = ?""",
        (conversation_id, str(client_instance_id)),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def select_loom_cursor(
    conversation_id: int,
    client_instance_id: str,
    thread_id: str,
    leaf_message_id: int | None,
    *,
    expected_revision: int | None = None,
    force: bool = False,
) -> dict:
    """Select an exact thread/leaf using optimistic compare-and-swap."""
    if not client_instance_id:
        raise ValueError("client_instance_id is required")
    db = await get_db()
    thread = await get_loom_thread(thread_id)
    if not thread or int(thread["conversation_id"]) != int(conversation_id):
        raise ValueError("thread_id does not belong to this conversation")
    if leaf_message_id is not None:
        leaf = await get_message(int(leaf_message_id))
        if not leaf or int(leaf["conversation_id"]) != int(conversation_id):
            raise ValueError("leaf_message_id does not belong to this conversation")
        if str(leaf.get("loom_thread_id") or "") != str(thread_id):
            raise ValueError("leaf_message_id does not belong to this Loom thread")
    elif thread.get("root_message_id") is not None:
        raise ValueError("A non-empty Loom thread requires an explicit leaf")

    existing = await get_client_loom_cursor(conversation_id, client_instance_id)
    now = time.time()
    if existing:
        if not force:
            if expected_revision is None:
                raise LoomCursorConflict("expected_revision is required for an existing cursor")
            if int(existing["revision"]) != int(expected_revision):
                raise LoomCursorConflict("The Loom cursor changed before this navigation completed")
        cursor = await db.execute(
            """UPDATE loom_thread_cursors
               SET thread_id = ?, leaf_message_id = ?, revision = revision + 1,
                   updated_at = ?
               WHERE cursor_id = ? AND revision = ?""",
            (
                str(thread_id), leaf_message_id, now,
                existing["cursor_id"], int(existing["revision"]),
            ),
        )
        if cursor.rowcount != 1:
            raise LoomCursorConflict("The Loom cursor changed concurrently")
        cursor_id = existing["cursor_id"]
    else:
        if expected_revision not in (None, 0):
            raise LoomCursorConflict("The Loom cursor does not exist at that revision")
        cursor_id = str(uuid.uuid4())
        inserted = await db.execute(
            """INSERT OR IGNORE INTO loom_thread_cursors
               (cursor_id, conversation_id, thread_id, client_instance_id,
                leaf_message_id, revision, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                cursor_id, conversation_id, str(thread_id),
                str(client_instance_id), leaf_message_id, now, now,
            ),
        )
        if inserted.rowcount != 1:
            raise LoomCursorConflict("The Loom cursor was created concurrently")
    await db.commit()
    return await get_loom_cursor(cursor_id)


async def advance_loom_cursor(
    cursor_id: str,
    *,
    conversation_id: int,
    client_instance_id: str,
    thread_id: str,
    expected_leaf_id: int | None,
    expected_revision: int,
    new_leaf_id: int,
) -> dict:
    """Atomically advance a cursor after a validated message insertion."""
    db = await get_db()
    now = time.time()
    cursor = await db.execute(
        """UPDATE loom_thread_cursors
           SET leaf_message_id = ?, revision = revision + 1, updated_at = ?
           WHERE cursor_id = ? AND conversation_id = ?
             AND client_instance_id = ? AND thread_id = ? AND revision = ?
             AND ((leaf_message_id IS NULL AND ? IS NULL) OR leaf_message_id = ?)""",
        (
            new_leaf_id, now, str(cursor_id), conversation_id,
            str(client_instance_id), str(thread_id), int(expected_revision),
            expected_leaf_id, expected_leaf_id,
        ),
    )
    if cursor.rowcount != 1:
        raise LoomCursorConflict("The Loom thread cursor is stale; no message was attached")
    await db.commit()
    return await get_loom_cursor(cursor_id)


async def advance_loom_cursor_for_generation(
    conversation_id: int,
    thread_id: str,
    client_instance_id: str,
    parent_message_id: int,
    draft_message_id: int,
) -> int:
    """Move only the launching page from an exact parent to its draft.

    Another page can render the same run after an explicit/reconnect
    subscription, but the existence of a child never moves every cursor that
    happened to share its parent.
    """
    if not client_instance_id:
        return 0
    db = await get_db()
    cursor = await db.execute(
        """UPDATE loom_thread_cursors
           SET leaf_message_id = ?, revision = revision + 1, updated_at = ?
           WHERE conversation_id = ? AND thread_id = ?
             AND client_instance_id = ? AND leaf_message_id = ?""",
        (
            draft_message_id, time.time(), conversation_id,
            str(thread_id), str(client_instance_id), parent_message_id,
        ),
    )
    await db.commit()
    return cursor.rowcount


async def add_message(
    conversation_id: int,
    role: str,
    content: str,
    parent_id: int = None,
    image_path: str = None,
    is_active: bool = True,
    content_blocks: str = None,
    turn_cost_usd: float = None,
    turn_input_tokens: int = None,
    turn_output_tokens: int = None,
    cc_session_id: str = None,
    describe_context: str = None,
    origin_client_id: str = None,
    target_anchor_id: int = None,
    send_intent: str = None,
    loom_thread_id: str = None,
    client_request_id: str = None,
    generation_lease_id: str = None,
) -> dict:
    token_est = len(content) // 3
    now = time.time()
    async with write_transaction() as connection:
        if parent_id is not None:
            parent_rows = await connection.execute_fetchall(
                "SELECT conversation_id, loom_thread_id FROM messages WHERE id = ?",
                (parent_id,),
            )
            if not parent_rows or int(parent_rows[0]["conversation_id"]) != int(
                conversation_id
            ):
                raise ValueError("parent_id does not belong to this conversation")
            parent_thread_id = parent_rows[0]["loom_thread_id"]
            if not parent_thread_id:
                raise ValueError("parent message is missing Loom thread identity")
            if loom_thread_id and str(loom_thread_id) != str(parent_thread_id):
                raise ValueError("message thread_id does not match its parent")
            loom_thread_id = str(parent_thread_id)
        elif loom_thread_id:
            thread_rows = await connection.execute_fetchall(
                "SELECT * FROM loom_threads WHERE thread_id = ?",
                (str(loom_thread_id),),
            )
            thread = dict(thread_rows[0]) if thread_rows else None
            if not thread or int(thread["conversation_id"]) != int(conversation_id):
                raise ValueError("loom_thread_id does not belong to this conversation")
            if thread.get("root_message_id") is not None:
                raise ValueError("Cannot add a second root to an existing Loom thread")
        else:
            loom_thread_id = f"loom:{uuid.uuid4()}"
            await connection.execute(
                """INSERT INTO loom_threads
                   (thread_id, conversation_id, root_message_id,
                    created_by_client_id, created_at, updated_at)
                   VALUES (?, ?, NULL, ?, ?, ?)""",
                (
                    loom_thread_id,
                    conversation_id,
                    origin_client_id,
                    now,
                    now,
                ),
            )
        cursor = await connection.execute(
            """INSERT INTO messages
               (conversation_id, parent_id, role, content, image_path, token_estimate,
                is_active, content_blocks, turn_cost_usd, turn_input_tokens, turn_output_tokens,
                cc_session_id, describe_context, origin_client_id, target_anchor_id,
                send_intent, loom_thread_id, client_request_id,
                generation_lease_id, created_at, chat_items)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]')""",
            (
                conversation_id,
                parent_id,
                role,
                content,
                image_path,
                token_est,
                int(is_active),
                content_blocks,
                turn_cost_usd,
                turn_input_tokens,
                turn_output_tokens,
                cc_session_id,
                describe_context,
                origin_client_id,
                target_anchor_id,
                send_intent,
                loom_thread_id,
                client_request_id,
                generation_lease_id,
                now,
            ),
        )
        msg_id = cursor.lastrowid
        if parent_id is None:
            await connection.execute(
                """UPDATE loom_threads
                   SET root_message_id = ?, updated_at = ?
                   WHERE thread_id = ? AND root_message_id IS NULL""",
                (msg_id, now, loom_thread_id),
            )
        await connection.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )
        row = await connection.execute_fetchall(
            "SELECT * FROM messages WHERE id = ?", (msg_id,)
        )
        result = dict(row[0])

    return result


async def update_message_content(
    msg_id: int,
    content: str = None,
    content_blocks: str = None,
    turn_cost_usd: float = None,
    turn_input_tokens: int = None,
    turn_output_tokens: int = None,
    cc_session_id: str = None,
    cc_model_used: str = None,
    model_attestation: str = None,
    generation_ms: int = None,
    time_to_first_token_ms: int = None,
    cc_session_mode: str = None,
    turn_lifecycle_state: str = None,
):
    """Update a message's content and metadata (used for draft → final)."""
    db = await get_db()
    updates = []
    params = []
    if content is not None:
        updates.append("content = ?")
        params.append(content)
        updates.append("token_estimate = ?")
        params.append(len(content) // 3)
    if content_blocks is not None:
        updates.append("content_blocks = ?")
        params.append(content_blocks)
    if turn_cost_usd is not None:
        updates.append("turn_cost_usd = ?")
        params.append(turn_cost_usd)
    if turn_input_tokens is not None:
        updates.append("turn_input_tokens = ?")
        params.append(turn_input_tokens)
    if turn_output_tokens is not None:
        updates.append("turn_output_tokens = ?")
        params.append(turn_output_tokens)
    if cc_session_id is not None:
        updates.append("cc_session_id = ?")
        params.append(cc_session_id)
    if cc_session_mode is not None:
        updates.append("cc_session_mode = ?")
        params.append(cc_session_mode)
    if cc_model_used is not None:
        updates.append("cc_model_used = ?")
        params.append(cc_model_used)
    if model_attestation is not None:
        updates.append("model_attestation = ?")
        params.append(model_attestation)
    if generation_ms is not None:
        updates.append("generation_ms = ?")
        params.append(generation_ms)
    if time_to_first_token_ms is not None:
        updates.append("time_to_first_token_ms = ?")
        params.append(time_to_first_token_ms)
    if turn_lifecycle_state is not None:
        updates.append("turn_lifecycle_state = ?")
        params.append(turn_lifecycle_state)
    if updates:
        params.append(msg_id)
        await db.execute(
            f"UPDATE messages SET {', '.join(updates)} WHERE id = ?", params
        )
        await db.commit()


async def discard_message_partial_context(
    msg_id: int,
    content: str = "[Turn cancelled; partial response discarded]",
    generation_ms: int = None,
    time_to_first_token_ms: int = None,
):
    """Finalize a clean cancel without deleting the topology node.

    The user prompt and any queued descendants remain attached to this row,
    but partial assistant/tool content and native provider continuity are
    deliberately removed so a retry cannot inherit the cancelled turn.
    """
    db = await get_db()
    updates = [
        "content = ?",
        "token_estimate = ?",
        "content_blocks = NULL",
        "cc_session_id = NULL",
        "cc_session_mode = NULL",
        "turn_lifecycle_state = 'cancelled'",
    ]
    params = [content, len(content) // 3]
    if generation_ms is not None:
        updates.append("generation_ms = ?")
        params.append(generation_ms)
    if time_to_first_token_ms is not None:
        updates.append("time_to_first_token_ms = ?")
        params.append(time_to_first_token_ms)
    params.append(msg_id)
    await db.execute(
        f"UPDATE messages SET {', '.join(updates)} WHERE id = ?", params
    )
    await db.commit()


async def get_message(msg_id: int) -> Optional[dict]:
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM messages WHERE id = ?", (msg_id,))

    return dict(rows[0]) if rows else None


async def get_message_by_client_request(
    conversation_id: int, client_request_id: str
) -> Optional[dict]:
    if not client_request_id:
        return None
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT * FROM messages
           WHERE conversation_id = ? AND client_request_id = ?""",
        (int(conversation_id), str(client_request_id)),
    )
    return dict(rows[0]) if rows else None


async def update_message_summary(msg_id: int, summary: str):
    db = await get_db()
    await db.execute("UPDATE messages SET summary = ? WHERE id = ?", (summary, msg_id))
    await db.commit()


async def update_message_image_alt(msg_id: int, image_alt: str):
    db = await get_db()
    await db.execute(
        "UPDATE messages SET image_alt = ? WHERE id = ?", (image_alt, msg_id)
    )
    await db.commit()


async def get_children(msg_id: int) -> list[dict]:
    """Get all direct children of a message."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM messages WHERE parent_id = ? ORDER BY created_at", (msg_id,)
    )

    return [dict(r) for r in rows]


async def get_siblings(msg_id: int) -> list[dict]:
    """Get all siblings of a message (same parent), ordered by creation time."""
    msg = await get_message(msg_id)
    if not msg:
        return []
    db = await get_db()
    if msg["parent_id"] is None:
        rows = await db.execute_fetchall(
            """SELECT * FROM messages
               WHERE conversation_id = ? AND parent_id IS NULL AND role = ?
               ORDER BY created_at""",
            (msg["conversation_id"], msg["role"]),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE parent_id = ? ORDER BY created_at",
            (msg["parent_id"],),
        )

    return [dict(r) for r in rows]


async def sum_branch_tokens(leaf_id: int) -> int:
    """Single-roundtrip token-estimate sum from a leaf up to the root.

    Used by the compact-handoff gate on every generation; must be fast.
    Relies on SQLite's recursive CTE so we don't round-trip one row at a time.
    """
    if leaf_id is None:
        return 0
    db = await get_db()
    rows = await db.execute_fetchall(
        """
        WITH RECURSIVE branch(id, parent_id, token_estimate) AS (
            SELECT id, parent_id, token_estimate
            FROM messages WHERE id = ?
            UNION ALL
            SELECT m.id, m.parent_id, m.token_estimate
            FROM messages m
            JOIN branch b ON m.id = b.parent_id
        )
        SELECT COALESCE(SUM(token_estimate), 0) AS total FROM branch
        """,
        (leaf_id,),
    )
    if not rows:
        return 0
    return int(rows[0]["total"] or 0)


async def get_message_ancestor_ids(conv_id: int, msg_id: int) -> set[int]:
    """Permission ancestry without loading transcripts/tool payloads into memory."""
    connection = await get_db()
    rows = await connection.execute_fetchall(
        """WITH RECURSIVE ancestors(id, parent_id) AS (
               SELECT id, parent_id FROM messages WHERE id = ? AND conversation_id = ?
               UNION
               SELECT m.id, m.parent_id FROM messages m JOIN ancestors a ON m.id = a.parent_id
               WHERE m.conversation_id = ?
           ) SELECT id FROM ancestors""", (msg_id, conv_id, conv_id))
    return {row["id"] for row in rows}


async def get_branch_to_root(msg_id: int) -> list[dict]:
    """Walk from a message up to the root, return list root->leaf order."""
    chain = []
    db = await get_db()
    current_id = msg_id
    while current_id is not None:
        rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE id = ?", (current_id,)
        )
        if not rows:
            break
        msg = dict(rows[0])
        chain.append(msg)
        current_id = msg["parent_id"]

    chain.reverse()
    return chain


async def set_active_branch(conv_id: int, leaf_id: int):
    """Mark a branch as active: deactivate all, then activate root->leaf path."""
    async with write_transaction() as connection:
        await connection.execute(
            "UPDATE messages SET is_active = 0 WHERE conversation_id = ?",
            (conv_id,),
        )
        # Deactivation and root-to-leaf activation are one atomic state change.
        current_id = leaf_id
        ids_to_activate = []
        while current_id is not None:
            rows = await connection.execute_fetchall(
                "SELECT id, parent_id FROM messages WHERE id = ?", (current_id,)
            )
            if not rows:
                break
            ids_to_activate.append(rows[0]["id"])
            current_id = rows[0]["parent_id"]
        if ids_to_activate:
            placeholders = ",".join("?" * len(ids_to_activate))
            await connection.execute(
                f"UPDATE messages SET is_active = 1 WHERE id IN ({placeholders})",
                ids_to_activate,
            )


async def get_active_branch(conv_id: int) -> list[dict]:
    """Get the currently active branch messages in root→leaf order.

    Ordered by the parent chain (topological), NOT by created_at. A system
    compact marker inserted mid-stream has a later created_at than the draft
    assistant message it becomes the parent of, so ORDER BY created_at would
    render the marker AFTER the reply even though the tree (parent_id) says
    it's the reply's parent.
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT * FROM messages
           WHERE conversation_id = ? AND is_active = 1""",
        (conv_id,),
    )
    by_id = {r["id"]: dict(r) for r in rows}
    if not by_id:
        return []
    # Root: parent_id NULL, or parent not in the active set.
    child_ids = set(by_id.keys())
    roots = [
        m for m in by_id.values()
        if not m["parent_id"] or m["parent_id"] not in child_ids
    ]
    if not roots:
        # Defensive fallback — a cycle-free active set should always have
        # a root. If not, at least return something stable.
        return sorted(by_id.values(), key=lambda m: m.get("created_at") or 0)
    root = min(roots, key=lambda m: m.get("created_at") or 0)
    children_of = {}
    for m in by_id.values():
        children_of.setdefault(m["parent_id"], []).append(m)
    chain = []
    current = root
    visited = set()
    while current and current["id"] not in visited:
        visited.add(current["id"])
        chain.append(current)
        kids = children_of.get(current["id"], [])
        current = min(kids, key=lambda m: m.get("created_at") or 0) if kids else None
    return chain


async def get_active_leaf(conv_id: int) -> Optional[dict]:
    """Get the leaf message of the active branch."""
    db = await get_db()
    # The leaf is an active message with no active children
    rows = await db.execute_fetchall(
        """SELECT m.* FROM messages m
           WHERE m.conversation_id = ? AND m.is_active = 1
           AND NOT EXISTS (
               SELECT 1 FROM messages c WHERE c.parent_id = m.id AND c.is_active = 1
           )
           ORDER BY m.created_at DESC LIMIT 1""",
        (conv_id,),
    )

    return dict(rows[0]) if rows else None


async def delete_branch(msg_id: int) -> dict:
    """Delete a message and its entire subtree. Returns info about what was deleted."""
    async with write_transaction() as connection:
        # Collect all descendant IDs via BFS.
        to_delete = []
        queue = [msg_id]
        while queue:
            current = queue.pop(0)
            to_delete.append(current)
            children = await connection.execute_fetchall(
                "SELECT id FROM messages WHERE parent_id = ?", (current,)
            )
            for child in children:
                queue.append(child["id"])

        # Get the message's parent and conversation before deleting.
        rows = await connection.execute_fetchall(
            "SELECT conversation_id, parent_id FROM messages WHERE id = ?", (msg_id,)
        )
        if not rows:
            return {"deleted": 0}

        conv_id = rows[0]["conversation_id"]
        parent_id = rows[0]["parent_id"]

        # Cursor repair and subtree deletion are one transaction.
        placeholders = ",".join("?" * len(to_delete))
        await connection.execute(
            f"""UPDATE loom_thread_cursors
                SET leaf_message_id = ?, revision = revision + 1, updated_at = ?
                WHERE leaf_message_id IN ({placeholders})""",
            [parent_id, time.time(), *to_delete],
        )
        from usage_activity_store import retain_footer_usage
        await retain_footer_usage(connection, f"id IN ({placeholders})", to_delete)
        await connection.execute(
            f"DELETE FROM messages WHERE id IN ({placeholders})", to_delete
        )

    return {
        "deleted": len(to_delete),
        "conversation_id": conv_id,
        "parent_id": parent_id,
    }


async def fork_conversation(
    conv_id: int, from_msg_id: int, new_title: str = None
) -> dict:
    """Fork a conversation: create a new conversation with messages up to from_msg_id."""
    db = await get_db()

    # Get original conversation
    rows = await db.execute_fetchall(
        "SELECT * FROM conversations WHERE id = ?", (conv_id,)
    )
    if not rows:

        return None
    orig = dict(rows[0])

    # Walk from from_msg_id up to root to get the branch to copy
    branch = []
    current_id = from_msg_id
    while current_id is not None:
        msg_rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE id = ?", (current_id,)
        )
        if not msg_rows:
            break
        msg = dict(msg_rows[0])
        branch.insert(0, msg)
        current_id = msg["parent_id"]

    # Create new conversation
    title = new_title or f"{orig['title']} (fork)"
    now = time.time()
    cursor = await db.execute(
        """INSERT INTO conversations (title, character_id, persona_id, lore_ids,
           style_nudge, custom_scene, mode, project_dir, cc_model, cc_effort,
           local_model, incognito, system_only, minimal_ooda_enabled,
           system_prompt, parallel_agents_enabled, created_at, updated_at, permission_profile)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            title,
            orig.get("character_id"),
            orig.get("persona_id"),
            orig.get("lore_ids"),
            orig.get("style_nudge"),
            orig.get("custom_scene"),
            orig.get("mode", "weave"),
            orig.get("project_dir"),
            orig.get("cc_model"),
            orig.get("cc_effort"),
            orig.get("local_model"),
            # Incognito carries into forks: a fork of an incognito (Prometheus)
            # conversation stays incognito — silently de-souling on fork would be
            # the exact contamination the incognito flag exists to control.
            int(orig.get("incognito", 0) or 0),
            int(orig.get("system_only", 0) or 0),
            int(orig.get("minimal_ooda_enabled", 1) if orig.get("minimal_ooda_enabled") is not None else 1),
            orig.get("system_prompt"),
            int(orig.get("parallel_agents_enabled", 0) or 0),
            now,
            now,
            orig.get("permission_profile") or "balanced",
        ),
    )
    new_conv_id = cursor.lastrowid
    await db.execute(
        "INSERT INTO style_state (conversation_id) VALUES (?)", (new_conv_id,)
    )

    new_thread_id = f"loom:{uuid.uuid4()}"
    await db.execute(
        """INSERT INTO loom_threads
           (thread_id, conversation_id, root_message_id, created_by_client_id,
            created_at, updated_at)
           VALUES (?, ?, NULL, NULL, ?, ?)""",
        (new_thread_id, new_conv_id, now, now),
    )

    # Copy messages, mapping old IDs to new IDs
    id_map = {}
    for msg in branch:
        new_parent = id_map.get(msg["parent_id"]) if msg["parent_id"] else None
        selections = msg.get('chat_items')
        if selections is not None:
            from chat_presentations import public_item
            copied = []
            for selection in json.loads(selections):
                source = await db.execute_fetchall('SELECT payload FROM chat_items WHERE item_id = ?', (selection['id'],))
                if not source:
                    continue
                item = json.loads(source[0]['payload'])
                item['id'] = str(uuid.uuid4())
                await db.execute('INSERT INTO chat_items (item_id, conversation_id, run_id, payload) VALUES (?, ?, ?, ?)',
                                 (item['id'], new_conv_id, f'fork:{new_conv_id}', json.dumps(item)))
                copied.append(public_item(item))
            selections = json.dumps(copied)
        cursor = await db.execute(
            """INSERT INTO messages (conversation_id, parent_id, role, content,
               token_estimate, is_active, summary, image_path, image_alt,
               cc_session_id, content_blocks, loom_thread_id, created_at, chat_items)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_conv_id,
                new_parent,
                msg["role"],
                msg["content"],
                msg.get("token_estimate", 0),
                msg.get("summary"),
                msg.get("image_path"),
                msg.get("image_alt"),
                msg.get("cc_session_id"),
                msg.get("content_blocks"),
                new_thread_id,
                msg["created_at"],
                selections,
            ),
        )
        id_map[msg["id"]] = cursor.lastrowid
        if selections is not None:
            for selection in json.loads(selections):
                await db.execute('UPDATE chat_items SET message_id=? WHERE item_id=?', (cursor.lastrowid, selection['id']))

    if branch:
        await db.execute(
            "UPDATE loom_threads SET root_message_id = ? WHERE thread_id = ?",
            (id_map[branch[0]["id"]], new_thread_id),
        )

    await db.commit()
    row = await db.execute_fetchall(
        "SELECT * FROM conversations WHERE id = ?", (new_conv_id,)
    )

    return dict(row[0])


async def get_conversation_tree(conv_id: int) -> list[dict]:
    """Get all messages for a conversation (for tree visualization)."""
    db = await get_db()
    query = """SELECT id, parent_id, role, substr(content, 1, 200) as preview,
                      is_active, created_at, token_estimate, summary, image_path,
                      image_alt, loom_thread_id
               FROM messages WHERE conversation_id = ?
               ORDER BY created_at"""
    try:
        rows = await db.execute_fetchall(query, (conv_id,))
    except Exception as e:
        if "database disk image is malformed" not in str(e).lower():
            raise
        # Corrupted crash leftovers can leave stale entries in idx_messages_conv
        # while the underlying rows are gone. Bypass the index so existing,
        # readable conversations still load; order by id is a stable fallback.
        rows = await db.execute_fetchall(
            """SELECT id, parent_id, role, substr(content, 1, 200) as preview,
                      is_active, created_at, token_estimate, summary, image_path,
                      image_alt, loom_thread_id
               FROM messages NOT INDEXED
               WHERE conversation_id = ?
               ORDER BY id""",
            (conv_id,),
        )

    return [dict(r) for r in rows]


# ── Summaries ──


async def save_summary(
    conv_id: int, branch_path: list[int], content: str, covers_up_to: int
) -> dict:
    db = await get_db()
    now = time.time()
    token_est = len(content) // 3
    # Upsert: replace existing summary for this branch path
    path_json = json.dumps(branch_path)
    try:
        await db.execute(
            """INSERT OR REPLACE INTO summaries
               (conversation_id, branch_path, content, covers_up_to, token_estimate, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (conv_id, path_json, content, covers_up_to, token_est, now),
        )
        await db.commit()
        rows = await db.execute_fetchall(
            "SELECT * FROM summaries WHERE conversation_id = ? AND branch_path = ?",
            (conv_id, path_json),
        )
    except Exception as e:
        if not _is_sqlite_corruption_error(e):
            raise
        print(f"[DB] summaries table is corrupt; skipped saving summary for conversation {conv_id}: {e}")
        return {}

    return dict(rows[0]) if rows else {}


async def get_summary(conv_id: int, branch_path: list[int] = None) -> Optional[dict]:
    db = await get_db()
    path_json = json.dumps(branch_path or [])
    try:
        rows = await db.execute_fetchall(
            """SELECT * FROM summaries
               WHERE conversation_id = ? AND branch_path = ?
               ORDER BY created_at DESC LIMIT 1""",
            (conv_id, path_json),
        )
    except Exception as e:
        if not _is_sqlite_corruption_error(e):
            raise
        print(f"[DB] summaries table is corrupt; ignoring summary for conversation {conv_id}: {e}")
        return None

    return dict(rows[0]) if rows else None


# ── Style State ──


async def get_style_state(conv_id: int) -> dict:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM style_state WHERE conversation_id = ?", (conv_id,)
    )
    if not rows:
        await db.execute(
            "INSERT INTO style_state (conversation_id) VALUES (?)", (conv_id,)
        )
        await db.commit()
        rows = await db.execute_fetchall(
            "SELECT * FROM style_state WHERE conversation_id = ?", (conv_id,)
        )

    return dict(rows[0])


async def update_style_state(
    conv_id: int,
    nudge_index: int = None,
    alert_level: int = None,
    ngram_snapshot: dict = None,
):
    db = await get_db()
    updates = []
    params = []
    if nudge_index is not None:
        updates.append("current_nudge_index = ?")
        params.append(nudge_index)
    if alert_level is not None:
        updates.append("repetition_alert_level = ?")
        params.append(alert_level)
    if ngram_snapshot is not None:
        updates.append("last_ngram_snapshot = ?")
        params.append(json.dumps(ngram_snapshot))
    if updates:
        params.append(conv_id)
        await db.execute(
            f"UPDATE style_state SET {', '.join(updates)} WHERE conversation_id = ?",
            params,
        )
        await db.commit()


# ── State Cards ──


async def get_state_schemas() -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM state_schemas ORDER BY is_builtin DESC, name"
    )

    return [dict(r) for r in rows]


async def get_state_cards(conv_id: int, schema_id: str = None) -> list[dict]:
    db = await get_db()
    if schema_id:
        rows = await db.execute_fetchall(
            "SELECT * FROM state_cards WHERE conversation_id = ? AND schema_id = ? ORDER BY label",
            (conv_id, schema_id),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM state_cards WHERE conversation_id = ? ORDER BY schema_id, label",
            (conv_id,),
        )

    return [dict(r) for r in rows]


async def get_state_card_by_label(conv_id: int, schema_id: str, label: str):
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM state_cards WHERE conversation_id = ? AND schema_id = ? AND label = ?",
        (conv_id, schema_id, label),
    )

    return dict(rows[0]) if rows else None


async def create_state_card(
    conv_id: int,
    schema_id: str,
    label: str,
    data: dict = None,
    is_readonly: bool = False,
) -> dict:
    db = await get_db()
    now = time.time()
    data_json = json.dumps(data or {})
    await db.execute(
        "INSERT OR IGNORE INTO state_cards (conversation_id, schema_id, label, data, is_readonly, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (conv_id, schema_id, label, data_json, int(is_readonly), now),
    )
    await db.commit()
    rows = await db.execute_fetchall(
        "SELECT * FROM state_cards WHERE conversation_id = ? AND schema_id = ? AND label = ?",
        (conv_id, schema_id, label),
    )

    return dict(rows[0]) if rows else {}


async def update_state_card(card_id: int, data: dict) -> dict:
    db = await get_db()
    now = time.time()
    # Merge: load existing data, update fields
    rows = await db.execute_fetchall(
        "SELECT * FROM state_cards WHERE id = ?", (card_id,)
    )
    if not rows:

        return {}
    existing = json.loads(rows[0]["data"] or "{}")
    existing.update(data)
    await db.execute(
        "UPDATE state_cards SET data = ?, updated_at = ? WHERE id = ?",
        (json.dumps(existing), now, card_id),
    )
    await db.commit()
    rows = await db.execute_fetchall(
        "SELECT * FROM state_cards WHERE id = ?", (card_id,)
    )

    return dict(rows[0]) if rows else {}


async def update_state_card_field(
    conv_id: int, schema_id: str, label: str, field: str, value: str
) -> dict:
    """Update a single field on a state card, creating the card if it doesn't exist."""
    db = await get_db()
    now = time.time()
    rows = await db.execute_fetchall(
        "SELECT * FROM state_cards WHERE conversation_id = ? AND schema_id = ? AND label = ?",
        (conv_id, schema_id, label),
    )
    if rows:
        existing = json.loads(rows[0]["data"] or "{}")
        existing[field] = value
        await db.execute(
            "UPDATE state_cards SET data = ?, updated_at = ? WHERE id = ?",
            (json.dumps(existing), now, rows[0]["id"]),
        )
    else:
        data = {field: value}
        await db.execute(
            "INSERT INTO state_cards (conversation_id, schema_id, label, data, is_readonly, updated_at) VALUES (?, ?, ?, ?, 0, ?)",
            (conv_id, schema_id, label, json.dumps(data), now),
        )
    await db.commit()
    rows = await db.execute_fetchall(
        "SELECT * FROM state_cards WHERE conversation_id = ? AND schema_id = ? AND label = ?",
        (conv_id, schema_id, label),
    )

    return dict(rows[0]) if rows else {}


async def delete_state_card(card_id: int):
    db = await get_db()
    await db.execute("DELETE FROM state_cards WHERE id = ?", (card_id,))
    await db.commit()


# ── Branch State (Tier 3 — Deltas per message) ──


async def save_state_deltas(msg_id: int, deltas: list[dict]):
    """Save state deltas on an assistant message.
    deltas: [{"schema_id": ..., "label": ..., "field": ..., "value": ...}, ...]
    """
    db = await get_db()
    await db.execute(
        "UPDATE messages SET state_deltas = ? WHERE id = ?",
        (json.dumps(deltas), msg_id),
    )
    await db.commit()


async def get_branch_state(conv_id: int, leaf_msg_id: int) -> list[dict]:
    """Reconstruct effective state for a branch by applying deltas along the path.

    Returns state cards with deltas applied: base conversation cards + all
    deltas from root to leaf_msg_id, in order.
    """
    # Get base conversation state cards
    base_cards = await get_state_cards(conv_id)

    # Get the branch path (root to leaf)
    branch = await get_branch_to_root(leaf_msg_id)

    # Build effective state: start from base, apply deltas in order
    # Index base cards by (schema_id, label)
    effective = {}
    for card in base_cards:
        data = (
            json.loads(card["data"]) if isinstance(card["data"], str) else card["data"]
        )
        effective[(card["schema_id"], card["label"])] = {**card, "data": data}

    # Walk branch in chronological order (branch_to_root returns root-first)
    for msg in branch:
        deltas_raw = msg.get("state_deltas")
        if not deltas_raw:
            continue
        try:
            deltas = json.loads(deltas_raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for delta in deltas:
            key = (delta["schema_id"], delta["label"])
            if key not in effective:
                effective[key] = {
                    "schema_id": delta["schema_id"],
                    "label": delta["label"],
                    "data": {},
                }
            effective[key]["data"][delta["field"]] = delta["value"]

    return list(effective.values())


# ── Character State Cards (Tier 1 — Global) ──


async def get_character_state_cards(
    character_id: str, schema_id: str = None
) -> list[dict]:
    db = await get_db()
    if schema_id:
        rows = await db.execute_fetchall(
            "SELECT * FROM character_state_cards WHERE character_id = ? AND schema_id = ? ORDER BY label",
            (character_id, schema_id),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM character_state_cards WHERE character_id = ? ORDER BY schema_id, label",
            (character_id,),
        )

    return [dict(r) for r in rows]


async def create_character_state_card(
    character_id: str,
    schema_id: str,
    label: str,
    data: dict = None,
    is_readonly: bool = False,
) -> dict:
    db = await get_db()
    now = time.time()
    data_json = json.dumps(data or {})
    await db.execute(
        "INSERT OR IGNORE INTO character_state_cards (character_id, schema_id, label, data, is_readonly, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (character_id, schema_id, label, data_json, int(is_readonly), now),
    )
    await db.commit()
    rows = await db.execute_fetchall(
        "SELECT * FROM character_state_cards WHERE character_id = ? AND schema_id = ? AND label = ?",
        (character_id, schema_id, label),
    )

    return dict(rows[0]) if rows else {}


async def update_character_state_card(card_id: int, data: dict) -> dict:
    db = await get_db()
    now = time.time()
    rows = await db.execute_fetchall(
        "SELECT * FROM character_state_cards WHERE id = ?", (card_id,)
    )
    if not rows:

        return {}
    existing = json.loads(rows[0]["data"] or "{}")
    existing.update(data)
    await db.execute(
        "UPDATE character_state_cards SET data = ?, updated_at = ? WHERE id = ?",
        (json.dumps(existing), now, card_id),
    )
    await db.commit()
    rows = await db.execute_fetchall(
        "SELECT * FROM character_state_cards WHERE id = ?", (card_id,)
    )

    return dict(rows[0]) if rows else {}


async def delete_character_state_card(card_id: int):
    db = await get_db()
    await db.execute("DELETE FROM character_state_cards WHERE id = ?", (card_id,))
    await db.commit()


async def copy_character_state_to_conversation(character_id: str, conv_id: int) -> int:
    """Copy all character-level state cards into conversation-level state cards. Returns count."""
    db = await get_db()
    now = time.time()
    rows = await db.execute_fetchall(
        "SELECT * FROM character_state_cards WHERE character_id = ?", (character_id,)
    )
    count = 0
    for row in rows:
        await db.execute(
            "INSERT OR IGNORE INTO state_cards (conversation_id, schema_id, label, data, is_readonly, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                conv_id,
                row["schema_id"],
                row["label"],
                row["data"],
                row["is_readonly"],
                now,
            ),
        )
        count += 1
    await db.commit()

    return count


# ── Bookmarks ──


async def get_bookmarks(conv_id: int) -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM bookmarks WHERE conversation_id = ? ORDER BY created_at DESC",
        (conv_id,),
    )

    return [dict(r) for r in rows]


async def get_all_bookmarks() -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT b.*, c.title as conversation_title, c.mode as conversation_mode
           FROM bookmarks b
           JOIN conversations c ON b.conversation_id = c.id
           ORDER BY b.created_at DESC"""
    )

    return [dict(r) for r in rows]


async def get_agent_notification_dismissals() -> list[str]:
    connection = await get_db()
    rows = await connection.execute_fetchall("SELECT notification_key FROM agent_notification_dismissals")
    return [row[0] for row in rows]


async def dismiss_agent_notifications(keys: list[str]):
    connection = await get_db()
    await connection.executemany(
        "INSERT OR IGNORE INTO agent_notification_dismissals(notification_key) VALUES (?)",
        [(key,) for key in keys],
    )
    await connection.commit()


async def add_bookmark(
    conv_id: int, message_id: int, branch_name: str = "", description: str = ""
) -> dict:
    db = await get_db()
    now = time.time()
    cursor = await db.execute(
        "INSERT OR IGNORE INTO bookmarks (conversation_id, message_id, branch_name, description, created_at) VALUES (?, ?, ?, ?, ?)",
        (conv_id, message_id, branch_name, description, now),
    )
    await db.commit()
    rows = await db.execute_fetchall(
        "SELECT * FROM bookmarks WHERE conversation_id = ? AND message_id = ?",
        (conv_id, message_id),
    )

    return dict(rows[0]) if rows else {}


async def update_bookmark(bookmark_id: int, description: str) -> dict:
    db = await get_db()
    await db.execute(
        "UPDATE bookmarks SET description = ? WHERE id = ?", (description, bookmark_id)
    )
    await db.commit()
    rows = await db.execute_fetchall(
        "SELECT * FROM bookmarks WHERE id = ?", (bookmark_id,)
    )

    return dict(rows[0]) if rows else {}


async def delete_bookmark(bookmark_id: int):
    db = await get_db()
    await db.execute("DELETE FROM bookmarks WHERE id = ?", (bookmark_id,))
    await db.commit()


# ── Branch Pins ──


async def get_branch_pins(conv_id: int) -> list[dict]:
    """Return navigation pins scoped to one conversation."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM branch_pins WHERE conversation_id = ? ORDER BY updated_at DESC, id DESC",
        (conv_id,),
    )
    return [dict(r) for r in rows]


async def list_workspace_conversations() -> list[dict]:
    """Workspace catalog for Search Space; content is fetched separately."""
    connection = await get_db()
    rows = await connection.execute_fetchall(
        """SELECT c.id AS conversation_id, c.title AS conversation_title,
                  c.mode, c.project_dir, c.updated_at, COALESCE(r.revision, 0) AS search_revision,
                  COUNT(m.id) AS message_count
           FROM conversations c
           LEFT JOIN search_revisions r ON r.conversation_id=c.id
           LEFT JOIN messages m ON m.conversation_id = c.id
                              AND (trim(COALESCE(m.content, '')) != ''
                                   OR COALESCE(m.content_blocks, '') NOT IN ('', '[]', 'null'))
           WHERE c.backstage_parent_id IS NULL
             AND c.project_dir IS NOT NULL AND trim(c.project_dir) != ''
           GROUP BY c.id
           ORDER BY c.updated_at DESC, c.id DESC"""
    )
    return [dict(row) for row in rows]


async def list_search_documents(conversation_ids: list[int]) -> list[dict]:
    """Return indexable message text for an explicit set of conversations."""
    ids = list(dict.fromkeys(int(value) for value in conversation_ids))
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    connection = await get_db()
    rows = await connection.execute_fetchall(
        f"""SELECT m.id AS message_id, m.conversation_id, c.title AS conversation_title,
                   c.mode, m.role, m.content, m.content_blocks, m.parent_id, m.loom_thread_id,
                   t.root_message_id, m.created_at
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            LEFT JOIN loom_threads t ON t.thread_id = m.loom_thread_id
                                    AND t.conversation_id = m.conversation_id
            WHERE m.conversation_id IN ({placeholders})
              AND (trim(COALESCE(m.content, '')) != ''
                   OR COALESCE(m.content_blocks, '') NOT IN ('', '[]', 'null'))
              AND (m.parent_id IS NULL OR EXISTS (
                  SELECT 1 FROM messages parent
                  WHERE parent.id = m.parent_id
                    AND parent.conversation_id = m.conversation_id
              ))
            ORDER BY m.created_at, m.id""",
        ids,
    )
    return [dict(row) for row in rows]


async def add_branch_pin(conv_id: int, message_id: int, label: str) -> dict:
    """Pin an exact message path, updating its label if it is already pinned."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM messages WHERE id = ? AND conversation_id = ?",
        (message_id, conv_id),
    )
    if not rows:
        return {}

    now = time.time()
    await db.execute(
        """INSERT INTO branch_pins
               (conversation_id, message_id, label, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(conversation_id, message_id) DO UPDATE SET
               label = excluded.label,
               updated_at = excluded.updated_at""",
        (conv_id, message_id, label, now, now),
    )
    await db.commit()
    rows = await db.execute_fetchall(
        "SELECT * FROM branch_pins WHERE conversation_id = ? AND message_id = ?",
        (conv_id, message_id),
    )
    return dict(rows[0]) if rows else {}


async def update_branch_pin(pin_id: int, label: str) -> dict:
    db = await get_db()
    await db.execute(
        "UPDATE branch_pins SET label = ?, updated_at = ? WHERE id = ?",
        (label, time.time(), pin_id),
    )
    await db.commit()
    rows = await db.execute_fetchall(
        "SELECT * FROM branch_pins WHERE id = ?", (pin_id,)
    )
    return dict(rows[0]) if rows else {}


async def delete_branch_pin(pin_id: int) -> bool:
    db = await get_db()
    cursor = await db.execute("DELETE FROM branch_pins WHERE id = ?", (pin_id,))
    await db.commit()
    return cursor.rowcount > 0


# ── Modules (Skills / Commands / Agents) ──


async def upsert_module(
    module_id: str,
    name: str,
    module_type: str,
    description: str = "",
    source: str = "builtin",
    config: dict = None,
) -> dict:
    """Insert or update a module. Returns the module dict."""
    import time

    db = await get_db()
    now = time.time()
    config_json = json.dumps(config or {})
    await db.execute(
        """INSERT INTO modules (id, name, type, description, source, config, enabled, discovered_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               name=excluded.name, description=excluded.description,
               config=excluded.config, updated_at=excluded.updated_at""",
        (module_id, name, module_type, description, source, config_json, now, now),
    )
    await db.commit()
    row = await db.execute_fetchall("SELECT * FROM modules WHERE id = ?", (module_id,))
    return dict(row[0]) if row else {}


async def get_modules(module_type: str = None, enabled_only: bool = True) -> list[dict]:
    db = await get_db()
    sql = "SELECT * FROM modules"
    params = []
    conditions = []
    if module_type:
        conditions.append("type = ?")
        params.append(module_type)
    if enabled_only:
        conditions.append("enabled = 1")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY name"
    rows = await db.execute_fetchall(sql, params)
    return [dict(r) for r in rows]


async def set_module_enabled(module_id: str, enabled: bool):
    import time

    db = await get_db()
    await db.execute(
        "UPDATE modules SET enabled = ?, updated_at = ? WHERE id = ?",
        (1 if enabled else 0, time.time(), module_id),
    )
    await db.commit()


async def delete_module(module_id: str):
    db = await get_db()
    await db.execute("DELETE FROM modules WHERE id = ?", (module_id,))
    await db.commit()


# ── Helpers ──


async def count_conversation_tokens(conv_id: int) -> int:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT COALESCE(SUM(token_estimate), 0) as total FROM messages WHERE conversation_id = ? AND is_active = 1",
        (conv_id,),
    )

    return rows[0]["total"] if rows else 0


# ── Active generation tracking (orphan reap) ──


async def register_active_generation(
    draft_msg_id: int,
    conv_id: int,
    pid: int,
    session_id: str = None,
    project_dir: str = None,
    mode: str = None,
    loom_thread_id: str = None,
):
    """Record that a generation subprocess is running. Called right after
    subprocess launch in _handle_*_generation. The row survives server
    restart so we can reap orphans."""
    db = await get_db()
    if not loom_thread_id:
        draft = await get_message(draft_msg_id)
        loom_thread_id = draft.get("loom_thread_id") if draft else None
    await db.execute(
        """INSERT OR REPLACE INTO active_generations
           (draft_msg_id, conv_id, pid, session_id, project_dir, mode,
            loom_thread_id, started_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            draft_msg_id, conv_id, pid, session_id, project_dir, mode,
            loom_thread_id, time.time(),
        ),
    )
    await db.commit()


async def update_active_generation_session(draft_msg_id: int, session_id: str):
    """Patch the session_id onto a tracked generation once CC emits it.
    Makes JSONL-based rescue possible in a future phase."""
    if not session_id:
        return
    db = await get_db()
    await db.execute(
        "UPDATE active_generations SET session_id = ? WHERE draft_msg_id = ?",
        (session_id, draft_msg_id),
    )
    await db.commit()


async def unregister_active_generation(draft_msg_id: int):
    """Drop the tracking row when the generation finalizes normally."""
    db = await get_db()
    await db.execute(
        "DELETE FROM active_generations WHERE draft_msg_id = ?", (draft_msg_id,)
    )
    await db.commit()


async def list_active_generations() -> list[dict]:
    """All currently tracked generations (alive + orphaned until reaped)."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM active_generations ORDER BY started_at DESC"
    )
    return [dict(r) for r in rows]


async def list_landed_generations(limit: int = 100) -> list[dict]:
    """Read-only, bounded recent-turn metadata for admin; never infer from absence alone.

    Select the latest landed message per conversation/root thread before the
    history limit, so a busy thread cannot evict other roots. No transcript/tool
    output is returned. Native parallel turns may have timing
    but no terminal lease: report 'finished', not an attested successful outcome.
    """
    limit = max(1, min(int(limit), 100))
    connection = await get_db()
    rows = await connection.execute_fetchall(
        """WITH ranked AS (
               SELECT m.id,
                      COALESCE(l.released_at,
                          m.created_at + MAX(0, COALESCE(m.generation_ms, 0)) / 1000.0) AS ended_at,
                      ROW_NUMBER() OVER (
                          PARTITION BY m.conversation_id,
                              COALESCE(NULLIF(m.loom_thread_id, ''), 'message:' || m.id)
                          ORDER BY COALESCE(l.released_at,
                              m.created_at + MAX(0, COALESCE(m.generation_ms, 0)) / 1000.0) DESC,
                              m.id DESC
                      ) AS thread_rank
               FROM messages m JOIN conversations c ON c.id = m.conversation_id
               LEFT JOIN conversation_generation_leases l ON l.lease_id = m.generation_lease_id
               WHERE m.role = 'assistant'
                 AND NOT EXISTS (SELECT 1 FROM active_generations a WHERE a.draft_msg_id = m.id)
                 AND (m.generation_lease_id IS NULL OR l.released_at IS NOT NULL)
                 AND (l.released_at IS NOT NULL OR m.generation_ms IS NOT NULL
                      OR m.turn_lifecycle_state IN ('completed','failed','partial_failed','cancelled','interrupted'))
           ), latest AS (
               SELECT id FROM ranked WHERE thread_rank = 1 ORDER BY ended_at DESC, id DESC LIMIT ?
           )
           SELECT m.id AS draft_msg_id, m.conversation_id AS conv_id,
                  m.created_at, m.generation_ms, m.turn_lifecycle_state,
                  m.cc_session_mode, m.cc_model_used, m.model_attestation,
                  m.loom_thread_id, l.run_id, l.terminal_state, l.released_at,
                  c.title, c.project_dir, t.root_message_id,
                  CASE WHEN ltrim(m.content) LIKE '[Error:%' THEN 'failed'
                       WHEN ltrim(m.content) LIKE '[Turn interrupted%' THEN 'interrupted'
                       ELSE NULL END AS legacy_outcome
           FROM latest JOIN messages m ON m.id = latest.id
           JOIN conversations c ON c.id = m.conversation_id
           LEFT JOIN loom_threads t ON t.thread_id = m.loom_thread_id AND t.conversation_id = m.conversation_id
           LEFT JOIN conversation_generation_leases l ON l.lease_id = m.generation_lease_id
           ORDER BY m.id DESC""", (limit,))
    landed = []
    for row in rows:
        item = dict(row)
        try:
            attestation = json.loads(item.pop("model_attestation") or "{}")
        except (TypeError, ValueError):
            attestation = {}
        if not isinstance(attestation, dict):
            attestation = {}
        duration = item.pop("generation_ms")
        released = item.pop("released_at")
        mode = item.pop("cc_session_mode")
        model = item.pop("cc_model_used")
        lifecycle = item.pop("turn_lifecycle_state")
        legacy_outcome = item.pop("legacy_outcome")
        terminal_state = item.pop("terminal_state")
        item.update({
            "kind": "generation", "branch_id": item["draft_msg_id"],
            "run_id": item["run_id"] or f"message:{item['draft_msg_id']}",
            "mode": attestation.get("harness") or mode,
            "model": attestation.get("effective_model") or attestation.get("launch_model") or model,
            "status": lifecycle or legacy_outcome or terminal_state or "finished",
            "completed_at": released if released is not None else (
                item["created_at"] + max(0, duration) / 1000 if duration is not None else None),
            "completion_time_estimated": released is None,
        })
        landed.append(item)
    children = await connection.execute_fetchall(
        """SELECT a.run_id, a.conversation_id AS conv_id, a.parent_message_id AS branch_id,
                  a.status, a.created_at, a.completed_at, c.title,
                  COALESCE(a.project_dir, c.project_dir) AS project_dir,
                  COALESCE(a.effective_harness, a.requested_harness) AS mode,
                  COALESCE(a.effective_model, a.requested_model) AS model
           FROM agent_runs a JOIN conversations c ON c.id = a.conversation_id
           WHERE a.status IN ('completed','failed','cancelled') AND a.completed_at IS NOT NULL
           ORDER BY a.created_at DESC LIMIT ?""", (limit,))
    for row in children:
        item = dict(row)
        item.update(kind="child", run_id=f"agent:{item['run_id']}", completion_time_estimated=False)
        landed.append(item)
    return sorted(landed, key=lambda r: r["completed_at"] or r["created_at"], reverse=True)[:limit]


# -- Database-enforced serial generation lease --


async def try_acquire_generation_lease(
    conv_id: int,
    lease_id: str,
    *,
    run_id: str | None = None,
    user_msg_id: int | None = None,
    draft_parent_id: int | None = None,
    client_instance_id: str | None = None,
) -> dict | None:
    """Atomically acquire the one non-parallel provider lane."""
    connection = await get_db()
    now = time.time()
    cursor = await connection.execute(
        """INSERT OR IGNORE INTO conversation_generation_leases
           (lease_id, conv_id, run_id, user_msg_id, draft_parent_id,
            client_instance_id, state, acquired_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'launching', ?, ?)""",
        (
            str(lease_id), int(conv_id), run_id, user_msg_id, draft_parent_id,
            client_instance_id, now, now,
        ),
    )
    await connection.commit()
    if cursor.rowcount != 1:
        return None
    return await get_generation_lease(str(lease_id))


async def get_generation_lease(lease_id: str) -> dict | None:
    connection = await get_db()
    rows = await connection.execute_fetchall(
        "SELECT * FROM conversation_generation_leases WHERE lease_id = ?",
        (str(lease_id),),
    )
    return dict(rows[0]) if rows else None


async def get_live_generation_lease(conv_id: int) -> dict | None:
    connection = await get_db()
    rows = await connection.execute_fetchall(
        """SELECT * FROM conversation_generation_leases
           WHERE conv_id = ? AND released_at IS NULL
           ORDER BY acquired_at DESC LIMIT 1""",
        (int(conv_id),),
    )
    return dict(rows[0]) if rows else None


async def get_generation_lease_for_draft(draft_msg_id: int) -> dict | None:
    """Return the durable lease that owned an exact assistant draft."""
    connection = await get_db()
    rows = await connection.execute_fetchall(
        """SELECT * FROM conversation_generation_leases
           WHERE draft_msg_id = ?
           ORDER BY acquired_at DESC LIMIT 1""",
        (int(draft_msg_id),),
    )
    return dict(rows[0]) if rows else None


async def bind_generation_lease(
    lease_id: str,
    *,
    run_id: str | None = None,
    user_msg_id: int | None = None,
    draft_parent_id: int | None = None,
    draft_msg_id: int | None = None,
    state: str | None = None,
) -> bool:
    fields = ["updated_at = ?"]
    params: list = [time.time()]
    for column, value in (
        ("run_id", run_id),
        ("user_msg_id", user_msg_id),
        ("draft_parent_id", draft_parent_id),
        ("draft_msg_id", draft_msg_id),
        ("state", state),
    ):
        if value is not None:
            fields.append(f"{column} = ?")
            params.append(value)
    params.append(str(lease_id))
    connection = await get_db()
    cursor = await connection.execute(
        f"""UPDATE conversation_generation_leases SET {', '.join(fields)}
            WHERE lease_id = ? AND released_at IS NULL""",
        params,
    )
    await connection.commit()
    return cursor.rowcount == 1


async def mark_generation_lease_cancelling(lease_id: str) -> bool:
    return await bind_generation_lease(lease_id, state="cancelling")


async def release_generation_lease(
    lease_id: str, terminal_state: str = "completed"
) -> bool:
    connection = await get_db()
    now = time.time()
    cursor = await connection.execute(
        """UPDATE conversation_generation_leases
           SET state = 'terminal', terminal_state = ?, updated_at = ?, released_at = ?
           WHERE lease_id = ? AND released_at IS NULL""",
        (str(terminal_state), now, now, str(lease_id)),
    )
    await connection.commit()
    return cursor.rowcount == 1


async def release_stale_generation_leases() -> int:
    """Release leases left by a prior Loom process after orphan reaping.

    A tracked generation row is durable evidence that its provider may still
    be alive. Startup reaping deliberately leaves that row in place when the
    process tree does not exit, so never release the matching conversation
    lease merely because this is a new Loom process.
    """
    connection = await get_db()
    now = time.time()
    cursor = await connection.execute(
        """UPDATE conversation_generation_leases
           SET state = 'terminal', terminal_state = 'orphaned',
               updated_at = ?, released_at = ?
           WHERE released_at IS NULL
             AND NOT EXISTS (
                 SELECT 1 FROM active_generations active
                 WHERE active.conv_id = conversation_generation_leases.conv_id
             )""",
        (now, now),
    )
    await connection.commit()
    return cursor.rowcount


# ── Durable queued continuations ──


async def enqueue_generation(
    user_msg_id: int,
    conv_id: int,
    wait_for_draft_id: int,
    *,
    client_instance_id: str = None,
    loom_thread_id: str = None,
    settings: dict | None = None,
    generation_count: int = 1,
) -> dict:
    """Persist a continuation that must launch after an active draft ends."""
    db = await get_db()
    now = time.time()
    if not loom_thread_id:
        message = await get_message(user_msg_id)
        loom_thread_id = message.get("loom_thread_id") if message else None
    await db.execute(
        """INSERT INTO queued_generations
           (user_msg_id, conv_id, wait_for_draft_id, client_instance_id,
            loom_thread_id,
            settings_json, generation_count, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
        (
            user_msg_id,
            conv_id,
            wait_for_draft_id,
            client_instance_id,
            loom_thread_id,
            json.dumps(settings or {}),
            max(1, min(5, int(generation_count or 1))),
            now,
            now,
        ),
    )
    await db.commit()
    row = await db.execute_fetchall(
        "SELECT * FROM queued_generations WHERE user_msg_id = ?", (user_msg_id,)
    )
    return dict(row[0])


async def list_queued_generations(
    conv_id: int | None = None, *, include_launching: bool = True
) -> list[dict]:
    db = await get_db()
    where = []
    params = []
    if conv_id is not None:
        where.append("q.conv_id = ?")
        params.append(conv_id)
    if not include_launching:
        where.append("q.status = 'queued'")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = await db.execute_fetchall(
        f"""SELECT q.*, m.role, m.content, m.parent_id, m.image_path,
                   m.describe_context, m.created_at AS message_created_at,
                   m.origin_client_id, m.target_anchor_id, m.send_intent
            FROM queued_generations q
            JOIN messages m ON m.id = q.user_msg_id
            {clause}
            ORDER BY q.created_at""",
        params,
    )
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["settings"] = json.loads(item.pop("settings_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            item["settings"] = {}
            item.pop("settings_json", None)
        result.append(item)
    return result


async def claim_queued_generation(user_msg_id: int) -> bool:
    """Atomically claim one queued continuation for launch."""
    db = await get_db()
    cursor = await db.execute(
        """UPDATE queued_generations
           SET status = 'launching', updated_at = ?
           WHERE user_msg_id = ? AND status = 'queued'""",
        (time.time(), user_msg_id),
    )
    await db.commit()
    return cursor.rowcount == 1


async def release_queued_generation(user_msg_id: int):
    """Return a failed pre-launch claim to the durable queue."""
    db = await get_db()
    await db.execute(
        """UPDATE queued_generations
           SET status = 'queued', updated_at = ?
           WHERE user_msg_id = ? AND status = 'launching'""",
        (time.time(), user_msg_id),
    )
    await db.commit()


async def pause_queued_generations(
    conv_id: int, *, wait_for_draft_id: int | None = None
) -> int:
    """Pause queued launches at a user-requested cancel/kill boundary."""
    connection = await get_db()
    params: list = [time.time(), int(conv_id)]
    where = "conv_id = ? AND status IN ('queued', 'launching')"
    if wait_for_draft_id is not None:
        where += " AND wait_for_draft_id = ?"
        params.append(int(wait_for_draft_id))
    cursor = await connection.execute(
        f"UPDATE queued_generations SET status = 'paused', updated_at = ? WHERE {where}",
        params,
    )
    await connection.commit()
    return cursor.rowcount


async def resume_queued_generation(user_msg_id: int) -> bool:
    connection = await get_db()
    cursor = await connection.execute(
        """UPDATE queued_generations
           SET status = 'queued', resume_authorized = 1, updated_at = ?
           WHERE user_msg_id = ? AND status = 'paused'""",
        (time.time(), int(user_msg_id)),
    )
    await connection.commit()
    return cursor.rowcount == 1


async def delete_queued_generation(user_msg_id: int):
    db = await get_db()
    await db.execute(
        "DELETE FROM queued_generations WHERE user_msg_id = ?", (user_msg_id,)
    )
    await db.commit()


async def reset_launching_queued_generations():
    """A server restart makes every prior in-memory launch claim stale."""
    db = await get_db()
    await db.execute(
        """UPDATE queued_generations
           SET status = 'queued', updated_at = ?
           WHERE status = 'launching'""",
        (time.time(),),
    )
    await db.commit()


# ── Loom-managed child agent runs ──


async def create_agent_run(
    run_id: str,
    conversation_id: int,
    task: str,
    *,
    parent_run_id: str | None = None,
    parent_message_id: int | None = None,
    requested_harness: str | None = None,
    requested_model: str | None = None,
    model_profile: str | None = None,
    project_dir: str | None = None,
    path_scope: list[str] | None = None,
    client_instance_id: str | None = None,
    loom_thread_id: str | None = None,
    image_request: dict | None = None,
    access: dict | None = None,
) -> dict:
    now = time.time()
    db = await get_db()
    if not loom_thread_id and parent_message_id is not None:
        parent_message = await get_message(parent_message_id)
        loom_thread_id = (
            parent_message.get("loom_thread_id") if parent_message else None
        )
    await db.execute(
        """INSERT INTO agent_runs
           (run_id, conversation_id, parent_run_id, parent_message_id,
            loom_thread_id, task, image_request,
            requested_harness, requested_model, model_profile, project_dir,
            path_scope, client_instance_id, access, status, created_at, heartbeat_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
        (
            str(run_id),
            int(conversation_id),
            parent_run_id,
            parent_message_id,
            loom_thread_id,
            str(task),
            json.dumps(image_request, ensure_ascii=False) if image_request is not None else None,
            requested_harness,
            requested_model,
            model_profile,
            project_dir,
            json.dumps(path_scope or []),
            client_instance_id,
            json.dumps(access) if access is not None else None,
            now,
            now,
        ),
    )
    await db.commit()
    return await get_agent_run(str(run_id))


def _decode_agent_run(row) -> dict | None:
    if not row:
        return None
    item = dict(row)
    try:
        item["path_scope"] = json.loads(item.get("path_scope") or "[]")
    except (TypeError, json.JSONDecodeError):
        item["path_scope"] = []
    for key in ("result", "error", "image_request", "access"):
        value = item.get(key)
        if value and isinstance(value, str):
            try:
                item[key] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return item


async def get_agent_run(run_id: str) -> dict | None:
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM agent_runs WHERE run_id = ?", (str(run_id),)
    )
    row = await cursor.fetchone()
    return _decode_agent_run(row)


async def list_agent_runs(
    conversation_id: int | None = None,
    *,
    parent_run_id: str | None = None,
    statuses: list[str] | None = None,
) -> list[dict]:
    db = await get_db()
    where: list[str] = []
    params: list[object] = []
    if conversation_id is not None:
        where.append("conversation_id = ?")
        params.append(int(conversation_id))
    if parent_run_id is not None:
        where.append("parent_run_id = ?")
        params.append(str(parent_run_id))
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        where.append(f"status IN ({placeholders})")
        params.extend(str(s) for s in statuses)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = await db.execute_fetchall(
        f"""SELECT agent_runs.*,
            (SELECT COALESCE(MAX(sequence), 0) FROM agent_events
             WHERE agent_events.run_id = agent_runs.run_id) AS event_sequence
            FROM agent_runs {clause} ORDER BY created_at DESC""", params
    )
    return [_decode_agent_run(row) for row in rows]


async def update_agent_run(run_id: str, **fields) -> dict | None:
    allowed = {
        "status", "pid", "provider_session_id", "effective_harness",
        "effective_model", "result", "error", "cancel_requested",
        "client_instance_id",
        "started_at", "heartbeat_at", "completed_at", "requested_harness",
        "requested_model", "model_profile", "path_scope",
        "queue_reason", "released_result",
    }
    updates = []
    params: list[object] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key in {"result", "error"} and value is not None and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        if key == "path_scope":
            value = json.dumps(value or [])
        updates.append(f"{key} = ?")
        params.append(value)
    if not updates:
        return await get_agent_run(run_id)
    params.append(str(run_id))
    db = await get_db()
    await db.execute(
        f"UPDATE agent_runs SET {', '.join(updates)} WHERE run_id = ?", params
    )
    await db.commit()
    return await get_agent_run(run_id)


async def append_agent_event(
    run_id: str,
    event_kind: str,
    payload: dict | None = None,
    *,
    event_id: str | None = None,
    sequence: int | None = None,
    run_state: dict | None = None,
) -> dict:
    events = await append_agent_events(run_id, [{
        "event_kind": event_kind, "payload": payload or {},
        "event_id": event_id, "sequence": sequence,
    }], run_state=run_state)
    return events[0]


async def append_agent_events(run_id: str, items: list[dict], *, run_state: dict | None = None) -> list[dict]:
    """Persist an ordered batch and its optional terminal state in one transaction."""
    if not items:
        return []
    if run_state and not set(run_state) <= {"status", "result", "error", "cancel_requested", "completed_at", "queue_reason"}:
        raise ValueError("Unsupported child lifecycle field")
    async with write_transaction() as connection:
        cursor = await connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS last_seq FROM agent_events WHERE run_id = ?",
            (str(run_id),),
        )
        row = await cursor.fetchone()
        sequence = int(row["last_seq"] or 0)
        events = []
        for item in items:
            sequence = int(item["sequence"]) if item.get("sequence") is not None else sequence + 1
            events.append({"run_id": str(run_id), "sequence": sequence,
                           "event_id": str(item.get("event_id") or uuid.uuid4()),
                           "event_kind": str(item["event_kind"]), "payload": item.get("payload") or {},
                           "created_at": item.get("created_at") or time.time()})
        await connection.executemany(
            """INSERT OR REPLACE INTO agent_events
               (run_id, sequence, event_id, event_kind, payload, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(
                event["run_id"], event["sequence"], event["event_id"],
                event["event_kind"], json.dumps(event["payload"], ensure_ascii=False),
                event["created_at"],
            ) for event in events],
        )
        await connection.execute(
            "UPDATE agent_runs SET heartbeat_at = ? WHERE run_id = ?",
            (time.time(), str(run_id)),
        )
        if run_state:
            values = []
            for key, value in run_state.items():
                if key in {"result", "error"} and value is not None and not isinstance(value, str):
                    value = json.dumps(value, ensure_ascii=False)
                values.append(value)
            await connection.execute(
                f"UPDATE agent_runs SET {', '.join(f'{key} = ?' for key in run_state)} WHERE run_id = ?",
                (*values, str(run_id)),
            )
    return events


async def list_agent_events(run_id: str, *, after_sequence: int = 0, limit: int = 500) -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT run_id, sequence, event_id, event_kind, payload, created_at
           FROM agent_events WHERE run_id = ? AND sequence > ?
           ORDER BY sequence LIMIT ?""",
        (str(run_id), max(0, int(after_sequence)), max(1, min(5000, int(limit)))),
    )
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item["payload"] or "{}")
        except json.JSONDecodeError:
            item["payload"] = {"text": item["payload"]}
        result.append(item)
    return result


async def request_agent_cancel(run_id: str) -> dict | None:
    return await update_agent_run(run_id, cancel_requested=1, status="cancel_requested")


def agent_run_can_resume(run: dict) -> bool:
    return (run.get("pid") is None and not run.get("cancel_requested")
            and (run.get("status") == "paused"
                 or (run.get("status") == "failed" and not run.get("image_request"))))


async def request_agent_resume(run_id: str) -> dict | None:
    # Admission and the state change are atomic. A duplicate click must not
    # queue a run again after the first request has dispatched its provider.
    async with write_transaction() as connection:
        rows = await connection.execute_fetchall("SELECT * FROM agent_runs WHERE run_id=?", (run_id,))
        if not rows:
            return None
        run = _decode_agent_run(rows[0])
        if not agent_run_can_resume(run):
            raise ValueError("Only stopped paused/failed child runs without cancellation can be recovered")
        await connection.execute(
            "UPDATE agent_runs SET status='queued', provider_session_id=NULL, completed_at=NULL, "
            "error=? WHERE run_id=?",
            ("Resumed explicitly; recovering child-owned evidence, not replaying completed actions.", run_id))
        rows = await connection.execute_fetchall("SELECT * FROM agent_runs WHERE run_id=?", (run_id,))
        return _decode_agent_run(rows[0])
