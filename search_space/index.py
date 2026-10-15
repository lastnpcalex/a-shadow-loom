"""External SQLite FTS index and workspace disclosure policy.

Conversation text is duplicated only into this machine-local database. Nothing
in this module calls a model or a network service; routing lives in operator.py.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import sqlite3
import time
import json
from contextlib import contextmanager
from typing import Iterable

from exo_state import state_path
from .embeddings import LocalEmbeddings, EmbeddingsUnavailable, chunks
from .policy import include_ancestor


SEARCH_POLICIES = frozenset({"excluded", "cloud", "sensitive"})
_TERM_RE = re.compile(r"[^\W_][\w.-]{1,63}", re.UNICODE)


def canonical_workspace(path: str | Path) -> str:
    value = str(path or "").strip()
    if not value:
        raise ValueError("Workspace path is required")
    normalized = os.path.normpath(os.path.abspath(os.path.expanduser(value)))
    return os.path.normcase(normalized)


def workspace_key(path: str | Path) -> str:
    return hashlib.sha256(canonical_workspace(path).encode("utf-8")).hexdigest()


class SearchIndex:
    def __init__(self, path: str | Path | None = None, *, embeddings=None):
        self.path = Path(path).resolve() if path else state_path("search-space", "search.sqlite3")
        self.embeddings = embeddings if embeddings is not None else LocalEmbeddings()

    @contextmanager
    def _connect(self):
        connection = self._open()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS workspace_policies (
                workspace_key TEXT PRIMARY KEY,
                canonical_path TEXT NOT NULL,
                display_path TEXT NOT NULL,
                policy TEXT NOT NULL CHECK(policy IN ('excluded','cloud','sensitive')),
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                document_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                workspace_key TEXT NOT NULL,
                conversation_id INTEGER NOT NULL,
                conversation_title TEXT NOT NULL,
                mode TEXT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                loom_thread_id TEXT,
                root_message_id INTEGER,
                created_at REAL NOT NULL,
                content_hash TEXT NOT NULL,
                UNIQUE(source_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_search_documents_workspace
                ON documents(source_id, workspace_key, conversation_id, created_at);
            CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                conversation_title, content,
                content='documents', content_rowid='document_id',
                tokenize='porter unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
                INSERT INTO documents_fts(rowid, conversation_title, content)
                VALUES (new.document_id, new.conversation_title, new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
                INSERT INTO documents_fts(documents_fts, rowid, conversation_title, content)
                VALUES ('delete', old.document_id, old.conversation_title, old.content);
            END;
            CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
                INSERT INTO documents_fts(documents_fts, rowid, conversation_title, content)
                VALUES ('delete', old.document_id, old.conversation_title, old.content);
                INSERT INTO documents_fts(rowid, conversation_title, content)
                VALUES (new.document_id, new.conversation_title, new.content);
            END;
            """
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)")}
        for name, kind in (("parent_id", "INTEGER"), ("content_blocks", "TEXT")):
            if name not in columns:
                connection.execute(f"ALTER TABLE documents ADD COLUMN {name} {kind}")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS sync_state (
                source_id TEXT NOT NULL, workspace_key TEXT NOT NULL, signature TEXT NOT NULL,
                PRIMARY KEY(source_id, workspace_key)
            );
            CREATE TABLE IF NOT EXISTS search_sessions (
                session_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                workspace_keys TEXT NOT NULL, created_at REAL NOT NULL,
                turns TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS semantic_vectors (
                source_id TEXT NOT NULL, message_id INTEGER NOT NULL, model TEXT NOT NULL,
                content_hash TEXT NOT NULL, vector TEXT NOT NULL,
                PRIMARY KEY(source_id, message_id, model)
            );
            CREATE TRIGGER IF NOT EXISTS search_vector_delete AFTER DELETE ON documents BEGIN
                DELETE FROM semantic_vectors WHERE source_id=old.source_id AND message_id=old.message_id;
            END;
        """)
        return connection

    def set_policy(self, path: str | Path, policy: str) -> dict:
        policy = str(policy or "").strip().lower()
        if policy not in SEARCH_POLICIES:
            raise ValueError(f"Unknown search policy: {policy}")
        display = str(Path(path).expanduser())
        canonical = canonical_workspace(path)
        key = workspace_key(path)
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO workspace_policies
                   (workspace_key, canonical_path, display_path, policy, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(workspace_key) DO UPDATE SET
                       canonical_path=excluded.canonical_path,
                       display_path=excluded.display_path,
                       policy=excluded.policy,
                       updated_at=excluded.updated_at""",
                (key, canonical, display, policy, time.time()),
            )
            if policy == "excluded":
                connection.execute("DELETE FROM documents WHERE workspace_key = ?", (key,))
                connection.execute("DELETE FROM sync_state WHERE workspace_key = ?", (key,))
                connection.execute("DELETE FROM search_sessions WHERE EXISTS "
                                   "(SELECT 1 FROM json_each(workspace_keys) WHERE value=?)", (key,))
        return {"workspace_key": key, "project_dir": display, "policy": policy}

    def policy_for(self, path: str | Path) -> str:
        key = workspace_key(path)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT policy FROM workspace_policies WHERE workspace_key = ?", (key,)
            ).fetchone()
        return str(row["policy"]) if row else "excluded"

    def policies(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT workspace_key, display_path AS project_dir, policy, updated_at "
                "FROM workspace_policies ORDER BY lower(display_path)"
            ).fetchall()
        return [dict(row) for row in rows]

    def sync_workspace(self, path: str | Path, documents: Iterable[dict], source_id: str = "default",
                       conversation_ids: list[int] | None = None) -> dict:
        key = workspace_key(path)
        policy = self.policy_for(path)
        if policy == "excluded":
            raise ValueError("Workspace is excluded from Search Space")
        rows = []
        for item in documents:
            content = str(item.get("content") or "")
            blocks = item.get("content_blocks")
            if blocks and blocks not in ("[]", "null"):
                evidence = blocks if isinstance(blocks, str) else json.dumps(blocks, ensure_ascii=False)
                content += "\nRecorded content blocks:\n" + evidence
            if not content.strip():
                continue
            rows.append((
                str(source_id), int(item["message_id"]), key, int(item["conversation_id"]),
                str(item.get("conversation_title") or "Untitled"),
                str(item.get("mode") or ""), str(item.get("role") or "system"),
                content, item.get("loom_thread_id"), item.get("root_message_id"),
                float(item.get("created_at") or 0),
                item.get("parent_id"), blocks if isinstance(blocks, str) else json.dumps(blocks) if blocks else None,
                hashlib.sha256(json.dumps([content, item.get("parent_id"), item.get("content_blocks"),
                    item.get("conversation_title"), item.get("mode"), item.get("role"),
                    item.get("loom_thread_id"), item.get("root_message_id")],
                    ensure_ascii=False).encode("utf-8")).hexdigest(),
            ))
        ids = {row[1] for row in rows}
        changed = 0
        with self._connect() as connection:
            # Serialize the disclosure check with writes, so a concurrent
            # exclusion cannot be followed by recreating private documents.
            connection.execute("BEGIN IMMEDIATE")
            policy_row = connection.execute("SELECT policy FROM workspace_policies WHERE workspace_key=?", (key,)).fetchone()
            if not policy_row or policy_row[0] == "excluded":
                raise ValueError("Workspace is excluded from Search Space")
            extra = ""
            parameters = [str(source_id), key]
            if conversation_ids is not None:
                if not conversation_ids:
                    return {"workspace_key": key, "policy": policy, "documents": 0, "changed": 0, "removed": 0}
                extra = f" AND conversation_id IN ({','.join('?' for _ in conversation_ids)})"
                parameters.extend(conversation_ids)
            current = {
                int(row["message_id"]): str(row["content_hash"])
                for row in connection.execute(
                    "SELECT message_id, content_hash FROM documents WHERE source_id = ? AND workspace_key = ?" + extra,
                    parameters,
                )
            }
            for row in rows:
                if current.get(row[1]) == row[-1]:
                    continue
                connection.execute(
                    """INSERT INTO documents
                       (source_id, message_id, workspace_key, conversation_id, conversation_title,
                        mode, role, content, loom_thread_id, root_message_id, created_at,
                        parent_id, content_blocks, content_hash)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(source_id, message_id) DO UPDATE SET
                         workspace_key=excluded.workspace_key,
                         conversation_id=excluded.conversation_id,
                         conversation_title=excluded.conversation_title,
                         mode=excluded.mode, role=excluded.role, content=excluded.content,
                         loom_thread_id=excluded.loom_thread_id,
                         root_message_id=excluded.root_message_id,
                         parent_id=excluded.parent_id, content_blocks=excluded.content_blocks,
                         created_at=excluded.created_at, content_hash=excluded.content_hash""",
                    row,
                )
                changed += 1
            stale = set(current) - ids
            if stale:
                connection.executemany(
                    "DELETE FROM documents WHERE source_id = ? AND message_id = ?",
                    [(str(source_id), item) for item in stale],
                )
        return {"workspace_key": key, "policy": policy, "documents": len(rows), "changed": changed, "removed": len(stale)}

    @staticmethod
    def query_expression(queries: Iterable[str]) -> str:
        terms: list[str] = []
        seen: set[str] = set()
        for query in queries:
            for match in _TERM_RE.findall(str(query or "")):
                term = match.strip(".-").lower()
                if len(term) < 2 or term in seen:
                    continue
                seen.add(term)
                terms.append(term.replace('"', '""'))
                if len(terms) >= 24:
                    break
            if len(terms) >= 24:
                break
        return " OR ".join(f'"{term}"*' for term in terms)

    def search(self, queries: Iterable[str], workspace_keys: Iterable[str], limit: int = 24, source_id: str = "default") -> list[dict]:
        keys = list(dict.fromkeys(str(key) for key in workspace_keys if key))
        expression = self.query_expression(queries)
        if not keys or not expression:
            return []
        placeholders = ",".join("?" for _ in keys)
        sql = f"""SELECT d.message_id, d.conversation_id, d.conversation_title,
                         d.mode, d.role, d.loom_thread_id, d.root_message_id,
                         d.created_at, d.workspace_key,
                         snippet(documents_fts, 1, '[', ']', ' … ', 32) AS snippet,
                         bm25(documents_fts, 2.5, 1.0) AS score
                  FROM documents_fts
                  JOIN documents d ON d.document_id = documents_fts.rowid
                  WHERE documents_fts MATCH ? AND d.source_id = ?
                    AND d.workspace_key IN ({placeholders})
                  ORDER BY score, d.created_at DESC LIMIT ?"""
        with self._connect() as connection:
            rows = connection.execute(
                sql,
                (expression, str(source_id), *keys, max(1, min(int(limit), 100))),
            ).fetchall()
        return [self.linked(dict(row)) for row in rows]

    @staticmethod
    def linked(row: dict) -> dict:
        return {**row, "url": f"/?conversation={int(row['conversation_id'])}&branch={int(row['message_id'])}&view=chat"}

    def sync_signature(self, source_id: str, key: str, signature: str | None = None) -> str | None:
        with self._connect() as connection:
            if signature is not None:
                connection.execute("INSERT OR REPLACE INTO sync_state SELECT ?,?,? "
                                   "WHERE EXISTS (SELECT 1 FROM workspace_policies WHERE workspace_key=? AND policy!='excluded')",
                                   (source_id, key, signature, key))
                return signature
            row = connection.execute("SELECT signature FROM sync_state WHERE source_id=? AND workspace_key=?",
                                     (source_id, key)).fetchone()
            return row[0] if row else None

    def prune_workspaces(self, source_id: str, live_keys: list[str]) -> None:
        with self._connect() as connection:
            for row in list(connection.execute("SELECT DISTINCT workspace_key FROM documents WHERE source_id=?", (source_id,))):
                if row[0] not in live_keys:
                    connection.execute("DELETE FROM documents WHERE source_id=? AND workspace_key=?", (source_id, row[0]))
                    connection.execute("DELETE FROM sync_state WHERE source_id=? AND workspace_key=?", (source_id, row[0]))

    def document(self, message_id: int, source_id: str = "default") -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM documents WHERE source_id=? AND message_id=?",
                                     (source_id, int(message_id))).fetchone()
        return self.linked(dict(row)) if row else None

    def prune_conversations(self, source_id: str, key: str, live_ids: list[int]) -> None:
        with self._connect() as connection:
            existing = list(connection.execute("SELECT DISTINCT conversation_id FROM documents WHERE source_id=? AND workspace_key=?", (source_id, key)))
            for row in existing:
                if row[0] not in live_ids:
                    connection.execute("DELETE FROM documents WHERE source_id=? AND workspace_key=? AND conversation_id=?", (source_id, key, row[0]))

    def hybrid_search(self, query: str, keys: list[str], limit: int = 24,
                      source_id: str = "default") -> dict:
        """Reciprocal-rank fusion of FTS and local semantic vectors in this scope."""
        limit = max(1, min(int(limit), 50))
        lexical = self.search([query], keys, limit=max(limit, 50), source_id=source_id)
        if not keys:
            return {"sources": [], "retrieval_mode": "keyword"}
        placeholders = ",".join("?" for _ in keys)
        with self._connect() as connection:
            documents = [dict(row) for row in connection.execute(
                f"SELECT d.message_id,d.conversation_id,d.conversation_title,d.mode,d.role,"
                f"d.created_at,d.workspace_key,d.loom_thread_id,d.root_message_id,d.parent_id,d.content_hash,"
                f"v.vector AS cached_vector, CASE WHEN v.content_hash=d.content_hash THEN NULL ELSE d.content END AS content "
                f"FROM documents d LEFT JOIN semantic_vectors v ON d.source_id=v.source_id "
                f"AND d.message_id=v.message_id AND v.model=? AND v.content_hash=d.content_hash "
                f"WHERE d.source_id=? AND d.workspace_key IN ({placeholders})",
                (self.embeddings.model, source_id, *keys))]
        cache = {doc["message_id"]: {"content_hash":doc["content_hash"], "vector":doc["cached_vector"]}
                 for doc in documents if doc["cached_vector"] is not None}
        try:
            q = self.embeddings.encode([query])[0]
            lexical_mids = {row["message_id"] for row in lexical}
            uncached = []
            for doc in documents:
                entry = cache.get(doc["message_id"])
                if entry is None or entry["content_hash"] != doc["content_hash"]:
                    uncached.append(doc)
            # Prioritize candidates found by lexical search, then recent documents
            uncached.sort(key=lambda doc: (doc["message_id"] not in lexical_mids, -float(doc.get("created_at") or 0)))
            # Bounded budget per query keeps interactive search latency under ~1-2s
            MAX_CHUNKS_PER_QUERY = 16
            pending = []
            chunk_budget = MAX_CHUNKS_PER_QUERY
            for doc in uncached:
                if chunk_budget <= 0:
                    break
                doc_chunks = chunks(doc["conversation_title"] + "\n" + doc["content"])
                if len(doc_chunks) > 2:
                    doc_chunks = doc_chunks[:2]
                pending.append((doc, doc_chunks))
                chunk_budget -= len(doc_chunks)
            # Encode several short messages together; save each batch rather
            # than opening a SQLite connection for every individual message.
            batches, batch, size = [], [], 0
            for item in pending:
                if batch and size + len(item[1]) > 64:
                    batches.append(batch)
                    batch, size = [], 0
                batch.append(item)
                size += len(item[1])
            if batch:
                batches.append(batch)
            for batch in batches:
                vectors = self.embeddings.encode([text for _, texts in batch for text in texts])
                cursor = 0
                with self._connect() as connection:
                    for doc, texts in batch:
                        value = json.dumps(vectors[cursor:cursor + len(texts)])
                        cursor += len(texts)
                        cache[doc["message_id"]] = {"content_hash":doc["content_hash"], "vector":value}
                        # Revocation/deletion during inference cannot recreate
                        # an embedding whose canonical indexed row vanished.
                        connection.execute("INSERT OR REPLACE INTO semantic_vectors "
                            "SELECT ?,?,?,?,? WHERE EXISTS (SELECT 1 FROM documents "
                            "WHERE source_id=? AND message_id=? AND content_hash=?)",
                            (source_id, doc["message_id"], self.embeddings.model, doc["content_hash"],
                             value, source_id, doc["message_id"], doc["content_hash"]))
            import numpy as np
            q = np.asarray(q, dtype=float)
            q /= max(float(np.linalg.norm(q)), 1e-12)
            semantic = []
            for doc in documents:
                cached = cache.get(doc["message_id"])
                if not cached or cached.get("vector") is None:
                    continue
                vectors = np.asarray(json.loads(cached["vector"]), dtype=float)
                norms = np.maximum(np.linalg.norm(vectors, axis=1), 1e-12)
                scores = vectors @ q / norms
                best = int(np.argmax(scores))
                if float(scores[best]) < 0.35:
                    continue
                semantic.append(self.linked({k: v for k, v in doc.items() if k not in {"content", "cached_vector"}})
                                | {"score": float(scores[best]), "chunk": best})
            semantic.sort(key=lambda row: row["score"], reverse=True)
            semantic = semantic[:max(limit, 50)]
            # Warm search reads vector metadata and only the top candidates'
            # text, rather than loading every historical transcript again.
            with self._connect() as connection:
                for row in semantic:
                    text = connection.execute("SELECT content FROM documents WHERE source_id=? AND message_id=?",
                                              (source_id, row["message_id"])).fetchone()
                    texts = chunks(row["conversation_title"] + "\n" + text[0]) if text else [""]
                    row["snippet"] = texts[min(row.pop("chunk"), len(texts) - 1)][:700]
        except EmbeddingsUnavailable as exc:
            return {"sources": lexical[:limit], "retrieval_mode": "keyword", "notice": str(exc)}
        fused, rows = {}, {}
        for ranking in (lexical, semantic[:max(limit, 50)]):
            for rank, row in enumerate(ranking):
                mid = row["message_id"]
                fused[mid] = fused.get(mid, 0) + 1 / (60 + rank + 1)
                rows.setdefault(mid, row)
        ordered = sorted(fused, key=lambda mid: (-fused[mid], -rows[mid]["created_at"]))
        # Prevent a long branch from swallowing every result; retain at most
        # three distinct message matches per conversation.
        sources, counts = [], {}
        for mid in ordered:
            row = rows[mid]
            conv = row["conversation_id"]
            if counts.get(conv, 0) >= 3:
                continue
            sources.append(row | {"score": fused[mid]})
            counts[conv] = counts.get(conv, 0) + 1
            if len(sources) >= limit:
                break
        return {"sources": sources, "retrieval_mode": "semantic+keyword"}

    def context(self, message_id: int, radius: int = 2, source_id: str = "default") -> list[dict]:
        # Timestamp neighbors can be siblings or independent threads. Only
        # follow actual parent pointers, validating identity at every step.
        target = self.document(message_id, source_id)
        rows, seen = [], set()
        current, expected_id = target, message_id
        for _ in range(max(0, min(int(radius), 20)) + 1):
            if not current or current["message_id"] in seen:
                break
            if not include_ancestor(same_conversation=current["conversation_id"] == target["conversation_id"],
                    same_thread=current["loom_thread_id"] == target["loom_thread_id"],
                    same_workspace=current["workspace_key"] == target["workspace_key"],
                    parent_matches=current["message_id"] == expected_id, unseen=current["message_id"] not in seen):
                break
            rows.append(current)
            seen.add(current["message_id"])
            if current.get("parent_id") is None:
                break
            expected_id = current["parent_id"]
            current = self.document(expected_id, source_id)
        return list(reversed(rows))

    def create_session(self, source_id: str, keys: list[str]) -> dict:
        import uuid
        session_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute("INSERT INTO search_sessions VALUES(?,?,?,?,?)",
                               (session_id, source_id, json.dumps(keys), time.time(), "[]"))
        return self.session(session_id, source_id)

    def session(self, session_id: str, source_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM search_sessions WHERE session_id=? AND source_id=?",
                                     (session_id, source_id)).fetchone()
        if not row:
            return None
        return {**dict(row), "workspace_keys": json.loads(row["workspace_keys"]), "turns": json.loads(row["turns"])}

    def append_turn(self, session_id: str, source_id: str, turn: dict) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT turns FROM search_sessions WHERE session_id=? AND source_id=?",
                                     (session_id, source_id)).fetchone()
            if row is None:
                raise ValueError("Search conversation no longer exists")
            turns = json.loads(row[0])
            turns.append(turn)
            connection.execute("UPDATE search_sessions SET turns=? WHERE session_id=? AND source_id=?",
                               (json.dumps(turns, ensure_ascii=False), session_id, source_id))

    def sessions(self, source_id: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM search_sessions WHERE source_id=? ORDER BY created_at DESC LIMIT 100", (source_id,)).fetchall()
        result = []
        for row in rows:
            turns = json.loads(row["turns"])
            result.append({"session_id": row["session_id"], "created_at": row["created_at"],
                           "title": turns[0]["question"][:80] if turns else "New search",
                           "workspace_keys": json.loads(row["workspace_keys"]), "turn_count": len(turns)})
        return result

    def delete_session(self, session_id: str, source_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM search_sessions WHERE session_id=? AND source_id=?", (session_id, source_id))
