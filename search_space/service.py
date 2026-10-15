"""Shared bounded, read-only conversation retrieval for chat and every harness."""
import asyncio
import json

import database as db
from .index import workspace_key
from .operator import SearchOperator, SearchPolicyError
from .policy import Policy, may_read, tool_allowed, include_ancestor

_sync_locks: dict[tuple[str, str], asyncio.Lock] = {}


class SearchService:
    def __init__(self, index, source_id: str, keys: list[str], *, local: bool):
        self.index, self.source_id = index, source_id
        self.keys, self.local = list(dict.fromkeys(keys)), local

    def authorize(self) -> dict:
        plan = SearchOperator(self.index, source_id=self.source_id).plan(self.keys)
        for item in plan["workspaces"]:
            if not may_read(active=True, assigned=item["workspace_key"] in self.keys,
                            same_source=True, policy=Policy.CLOUD if item["policy"] == "cloud" else Policy.SENSITIVE,
                            local=self.local):
                raise SearchPolicyError("Sensitive history is available only to a local agent")
        return plan

    async def sync(self) -> None:
        self.authorize()
        catalog = await db.list_workspace_conversations()
        groups = {}
        for row in catalog:
            groups.setdefault(workspace_key(row["project_dir"]), []).append(row)
        await asyncio.to_thread(self.index.prune_workspaces, self.source_id, list(groups))
        for key in self.keys:
            if key not in groups:
                raise SearchPolicyError("Assigned search workspace no longer has conversations")
            lock = _sync_locks.setdefault((self.source_id, key), asyncio.Lock())
            async with lock:
                self.authorize()
                rows = groups[key]
                revisions = {str(row["conversation_id"]): row["search_revision"] for row in rows}
                signature = json.dumps({"version": 3, "revisions": revisions}, sort_keys=True)
                old = await asyncio.to_thread(self.index.sync_signature, self.source_id, key)
                if signature == old:
                    continue
                try:
                    state = json.loads(old) if old else {}
                    old_revisions = state["revisions"] if state.get("version") == 3 else {}
                except (ValueError, KeyError, TypeError):
                    old_revisions = {}
                changed_ids = [int(cid) for cid, revision in revisions.items() if old_revisions.get(cid) != revision]
                documents = await db.list_search_documents(changed_ids) if changed_ids else []
                self.authorize()
                await asyncio.to_thread(self.index.sync_workspace, rows[0]["project_dir"], documents, self.source_id, changed_ids)
                await asyncio.to_thread(self.index.prune_conversations, self.source_id, key, [int(cid) for cid in revisions])
                await asyncio.to_thread(self.index.sync_signature, self.source_id, key, signature)

    async def _target(self, message_id: int) -> tuple[dict, dict]:
        self.authorize()
        message = await db.get_message(int(message_id))
        conversation = await db.get_conversation(message["conversation_id"]) if message else None
        if (not conversation or conversation.get("backstage_parent_id")
                or not conversation.get("project_dir")
                or workspace_key(conversation["project_dir"]) not in self.keys):
            raise SearchPolicyError("Message is outside the assigned history scope")
        return message, conversation

    @staticmethod
    def link(conversation_id: int, message_id: int) -> str:
        return f"/?conversation={int(conversation_id)}&branch={int(message_id)}&view=chat"

    async def search_conversations(self, query: str, limit: int = 12) -> dict:
        query = str(query or "").strip()
        if not 2 <= len(query) <= 4000:
            raise ValueError("Search query must contain 2–4000 characters")
        await self.sync()
        result = await asyncio.to_thread(self.index.hybrid_search, query, self.keys, limit, self.source_id)
        self.authorize()
        sources = []
        for row in result["sources"]:
            try:
                message, conv = await self._target(row["message_id"])
            except SearchPolicyError:
                continue
            # A reused ID, move or deletion must never produce a false link.
            if message["conversation_id"] != row["conversation_id"]:
                continue
            sources.append({**row, "conversation_title": conv["title"],
                            "url": self.link(conv["id"], message["id"])})
        return {**result, "sources": sources}

    async def read_conversation_context(self, message_id: int, radius: int = 3,
                                        offset: int = 0, limit: int = 8000) -> dict:
        target, conv = await self._target(message_id)
        offset, limit = max(0, int(offset)), max(1, min(int(limit), 16000))
        radius = max(0, min(int(radius), 12))
        current, seen, rows, expected_id = target, set(), [], target["id"]
        for _ in range(radius + 1):
            if not current or not include_ancestor(
                    same_conversation=current["conversation_id"] == conv["id"],
                    same_thread=current.get("loom_thread_id") == target.get("loom_thread_id"),
                    same_workspace=True, parent_matches=current["id"] == expected_id,
                    unseen=current["id"] not in seen):
                break
            seen.add(current["id"])
            content = current["content"] or ""
            blocks = current.get("content_blocks")
            # Preserve recorded tool evidence as data, distinct from prose.
            evidence = blocks if isinstance(blocks, str) else json.dumps(blocks, ensure_ascii=False) if blocks else ""
            text = content + ("\nRecorded content blocks:\n" + evidence if evidence else "")
            start = offset if current["id"] == target["id"] else 0
            rows.append({"message_id": current["id"], "parent_id": current.get("parent_id"),
                         "role": current["role"], "created_at": current["created_at"],
                         "content": text[start:start + limit], "content_chars": len(text),
                         "next_offset": start + limit if start + limit < len(text) else None,
                         "url": self.link(conv["id"], current["id"])})
            expected_id = current.get("parent_id")
            current = await db.get_message(expected_id) if expected_id is not None else None
        await self._target(message_id)
        self.authorize()
        return {"conversation_id": conv["id"], "conversation_title": conv["title"],
                "message_id": target["id"], "url": self.link(conv["id"], target["id"]),
                "messages": list(reversed(rows)), "context_kind": "message ancestry"}

    async def get_conversation_details(self, message_id: int) -> dict:
        message, conv = await self._target(message_id)
        return {"conversation_id": conv["id"], "conversation_title": conv["title"],
                "workspace": conv["project_dir"], "mode": conv["mode"],
                "created_at": conv["created_at"], "updated_at": conv["updated_at"],
                "message_id": message["id"], "loom_thread_id": message.get("loom_thread_id"),
                "url": self.link(conv["id"], message["id"])}

    async def call(self, tool: str, arguments: dict) -> dict:
        if not tool_allowed(tool):
            raise SearchPolicyError("Search operator can only call history retrieval tools")
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        try:
            return await getattr(self, tool)(**arguments)
        except TypeError as exc:
            raise ValueError("Invalid search tool arguments") from exc
