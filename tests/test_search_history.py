"""History retrieval, isolation, persistence, and semantic ranking acceptance."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

import database as db
import server
from search_space import SearchIndex, SearchOperator, SearchPolicyError, workspace_key
from search_space.chat import SearchChat
from search_space.embeddings import EmbeddingsUnavailable, LocalEmbeddings
from search_space.scopes import issue_scope, resolve_scope, revoke_scope
from search_space.service import SearchService


class FixtureEncoder:
    model = "synthetic-concepts-v1"

    def __init__(self):
        self.calls = []

    def encode(self, texts):
        self.calls += texts
        return [[1.0, 0.0] if any(w in t.lower() for w in ("backup", "recovery", "undo"))
                 else [0.0, 1.0] for t in texts]


@pytest.fixture
def history_index(tmp_path, monkeypatch):
    index = SearchIndex(tmp_path / "external" / "search.sqlite3", embeddings=FixtureEncoder())
    monkeypatch.setattr(server, "_search_index", index)
    return index


async def conversation(tmp_path, index, name="Project", policy="cloud", mode="codex"):
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    conv = await db.create_conversation(name, mode=mode, project_dir=str(root))
    index.set_policy(root, policy)
    return conv, root


async def test_semantic_only_match_cached_vectors_and_no_unchanged_transcript_reload(tmp_path, history_index, monkeypatch):
    conv, root = await conversation(tmp_path, history_index)
    match = await db.add_message(conv["id"], "assistant", "Backups protect earlier files")
    await db.add_message(conv["id"], "assistant", "Planetary astronomy")
    service = SearchService(history_index, server._search_source_id(), [workspace_key(root)], local=False)
    first = await service.search_conversations("undo")
    assert first["retrieval_mode"] == "semantic+keyword"
    assert [r["message_id"] for r in first["sources"]] == [match["id"]]
    calls = len(history_index.embeddings.calls)

    async def forbidden(*args):
        raise AssertionError("unchanged search reloaded transcript text")
    monkeypatch.setattr(db, "list_search_documents", forbidden)
    second = await service.search_conversations("undo")
    assert second["sources"][0]["url"] == f"/?conversation={conv['id']}&branch={match['id']}&view=chat"
    assert len(history_index.embeddings.calls) == calls + 1  # query only


async def test_changed_conversation_only_reindexes_and_metadata_deletions_are_fresh(tmp_path, history_index, monkeypatch):
    first, root = await conversation(tmp_path, history_index)
    second = await db.create_conversation("Other", mode="claude", project_dir=str(root))
    a = await db.add_message(first["id"], "user", "backup alpha")
    b = await db.add_message(second["id"], "user", "backup beta")
    service = SearchService(history_index, server._search_source_id(), [workspace_key(root)], local=False)
    await service.sync()
    original, fetched = db.list_search_documents, []
    async def tracking(ids):
        fetched.append(ids)
        return await original(ids)
    monkeypatch.setattr(db, "list_search_documents", tracking)
    await db.update_conversation_fields(first["id"], title="Renamed")
    await service.sync()
    assert fetched == [[first["id"]]]
    assert history_index.document(a["id"], server._search_source_id())["conversation_title"] == "Renamed"
    assert history_index.document(b["id"], server._search_source_id())
    connection = await db.get_db()
    await connection.execute("DELETE FROM messages WHERE id=?", (a["id"],))
    await service.sync()
    assert history_index.document(a["id"], server._search_source_id()) is None
    await connection.execute("DELETE FROM conversations WHERE id=?", (second["id"],))
    await service.sync()
    assert history_index.document(b["id"], server._search_source_id()) is None


async def test_context_follows_ancestry_not_sibling_timestamps_and_preserves_tool_evidence(tmp_path, history_index):
    conv, root = await conversation(tmp_path, history_index)
    parent = await db.add_message(conv["id"], "user", "Please make a backup")
    left = await db.add_message(conv["id"], "assistant", "Proposed backup", parent_id=parent["id"])
    sibling = await db.add_message(conv["id"], "assistant", "SIBLING SECRET", parent_id=parent["id"])
    leaf = await db.add_message(conv["id"], "assistant", "Backup recorded", parent_id=left["id"],
                                content_blocks=json.dumps([{"type": "tool_result", "content": "copy completed"}]))
    service = SearchService(history_index, server._search_source_id(), [workspace_key(root)], local=False)
    await service.sync()
    result = await service.read_conversation_context(leaf["id"], radius=10)
    assert [m["message_id"] for m in result["messages"]] == [parent["id"], left["id"], leaf["id"]]
    assert "copy completed" in result["messages"][-1]["content"]
    assert "SIBLING SECRET" not in json.dumps(result)
    assert [m["message_id"] for m in history_index.context(leaf["id"], radius=10, source_id=server._search_source_id())] == [parent["id"], left["id"], leaf["id"]]
    page = await service.read_conversation_context(leaf["id"], radius=0, limit=5)
    assert page["messages"][0]["next_offset"] == 5
    following = await service.read_conversation_context(leaf["id"], radius=0, offset=5, limit=5)
    assert page["messages"][0]["content"] + following["messages"][0]["content"] == "Backup rec"
    assert sibling["id"] not in [m["message_id"] for m in result["messages"]]


async def test_messages_containing_only_recorded_tool_results_are_searchable(tmp_path, history_index):
    conv, root = await conversation(tmp_path, history_index)
    tool = await db.add_message(conv["id"], "assistant", "recorded", content_blocks=json.dumps([
        {"type":"tool_result", "content":"backup completed for inherited files"}]))
    await db.update_message_content(tool["id"], content="")
    service = SearchService(history_index, server._search_source_id(), [workspace_key(root)], local=False)
    found = await service.search_conversations("backup")
    assert tool["id"] in [row["message_id"] for row in found["sources"]]
    assert "inherited files" in found["sources"][0]["snippet"]


async def test_history_api_rejects_forged_folders_foreign_messages_and_revoked_tokens(tmp_path, history_index, monkeypatch):
    own, root = await conversation(tmp_path, history_index, "Own")
    other, _ = await conversation(tmp_path, history_index, "Other")
    own_message = await db.add_message(own["id"], "user", "backup own")
    foreign = await db.add_message(other["id"], "user", "backup foreign")
    token = issue_scope(own["id"], str(root))
    monkeypatch.setenv("LOOM_PUBLIC_URL", "https://100.103.144.94:3000")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        endpoint = "/api/history/search_conversations"
        assert (await client.post(endpoint, json={"query": "backup"})).status_code == 403
        headers = {"x-loom-history": token}
        result = await client.post(endpoint, json={"query": "backup"}, headers=headers)
        assert result.status_code == 200, result.text
        sources = result.json()["sources"]
        assert {s["message_id"] for s in sources} == {own_message["id"]}
        assert sources[0]["url"] == f"https://100.103.144.94:3000/?conversation={own['id']}&branch={own_message['id']}&view=chat"
        widened = await client.post(endpoint, json={"query": "backup", "workspace_keys": ["foreign"]}, headers=headers)
        assert widened.status_code == 400
        assert (await client.post("/api/history/read_conversation_context", json={"message_id": foreign["id"]}, headers=headers)).status_code == 403
        assert (await client.post("/api/history/write_file", json={}, headers=headers)).status_code == 403
        revoke_scope(token)
        assert (await client.post(endpoint, json={"query": "backup"}, headers=headers)).status_code == 403


@pytest.mark.parametrize("local,policy,expected", [(False,"sensitive",403),(True,"sensitive",200),(False,"excluded",403),(False,"cloud",200)])
async def test_history_api_enforces_agent_disclosure_route(tmp_path, history_index, local, policy, expected):
    conv, root = await conversation(tmp_path, history_index, policy=policy)
    await db.add_message(conv["id"], "user", "backup private")
    token = issue_scope(conv["id"], str(root), local=local)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.post("/api/history/search_conversations", json={"query": "backup"}, headers={"x-loom-history": token})
        assert response.status_code == expected, response.text


async def test_capability_is_database_bound_and_owned_by_task(tmp_path, history_index, monkeypatch):
    conv, root = await conversation(tmp_path, history_index)
    ready, finish = asyncio.Event(), asyncio.Event()
    async def owner():
        token = issue_scope(conv["id"], str(root))
        ready.token = token
        ready.set()
        await finish.wait()
    task = asyncio.create_task(owner())
    await ready.wait()
    assert resolve_scope(ready.token)
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "foreign.db"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.post("/api/history/search_conversations", json={"query":"backup"}, headers={"x-loom-history": ready.token})
        assert response.status_code == 403
    finish.set()
    await task
    await asyncio.sleep(0)
    assert resolve_scope(ready.token) is None


async def test_search_chat_persists_followups_and_only_executes_history_tools(tmp_path, history_index):
    conv, root = await conversation(tmp_path, history_index)
    await db.add_message(conv["id"], "user", "Planetary astronomy")
    leaf = await db.add_message(conv["id"], "assistant", "Backups preserve inherited changes")
    chat = SearchChat(history_index, server._search_source_id())
    session = chat.create([workspace_key(root)])
    assert not (history_index.path.parent / "operator-workspaces" / session["session_id"]).exists()
    seen, actions = [], iter([
        {"tool":"write_file","arguments":{"path":"owned.txt","content":"unsafe"}},
        {"tool":"read_conversation_context","arguments":{"message_id":leaf["id"]}},
        {"answer":f"Found the backup discussion [S{leaf['id']}]."},
        {"tool":"search_conversations","arguments":{"query":"undo"}},
        {"answer":f"That same conversation preserves changes [S{leaf['id']}], not [S999]."},
    ])
    async def complete(messages, route):
        seen.append(json.dumps(messages))
        assert route == "cloud"
        return json.dumps(next(actions))
    first = await chat.ask(session["session_id"], "Where are backups?", complete=complete)
    second = await chat.ask(session["session_id"], "The inherited changes one", complete=complete)
    assert first["inspected_message_ids"] == [leaf["id"]]
    assert "Where are backups?" in seen[-1] and "Found the backup discussion" in seen[-1]
    assert "[unsupported source]" in second["answer"]
    assert second["sources"][0]["message_id"] == leaf["id"]
    assert second["sources"][0]["source"] == first["sources"][0]["source"]
    assert not (root / "owned.txt").exists()
    assert all(action["tool"] != "write_file" for action in first["actions"])
    in_memory = chat.get(session["session_id"])
    assert in_memory is not None and len(in_memory["turns"]) == 2
    with history_index._connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM search_sessions").fetchone()
        assert row[0] == 0
    SearchChat.discard_workspace(workspace_key(root))
    assert chat.get(session["session_id"]) is None
    with pytest.raises(ValueError, match="ended"):
        await chat.ask(session["session_id"], "Again", complete=complete)


async def test_policy_change_mid_chat_never_launches_another_cloud_completion(tmp_path, history_index):
    conv, root = await conversation(tmp_path, history_index)
    await db.add_message(conv["id"], "user", "backup")
    chat = SearchChat(history_index, server._search_source_id())
    session = chat.create([workspace_key(root)])
    calls = []
    async def complete(messages, route):
        calls.append(route)
        history_index.set_policy(root, "sensitive")
        return json.dumps({"tool":"search_conversations","arguments":{"query":"undo"}})
    with pytest.raises(SearchPolicyError):
        await chat.ask(session["session_id"], "backup", complete=complete)
    assert calls == ["cloud"]
    assert chat.get(session["session_id"]) is None


async def test_legacy_operator_respects_disclosure_changes_while_expanding(tmp_path, history_index):
    _, root = await conversation(tmp_path, history_index)
    calls = []
    async def complete(messages, route):
        calls.append(route)
        history_index.set_policy(root, "sensitive")
        return '["backup"]'
    operator = SearchOperator(history_index, completion=complete, source_id=server._search_source_id())
    with pytest.raises(SearchPolicyError):
        await operator.ask("backup", [workspace_key(root)])
    assert calls == ["cloud"]


def test_unavailable_semantic_model_is_explicit_keyword_fallback(tmp_path):
    class Offline:
        model = "offline"
        def encode(self, texts):
            raise EmbeddingsUnavailable("fixture offline")
    index = SearchIndex(tmp_path / "search.sqlite3", embeddings=Offline())
    key = index.set_policy(tmp_path / "workspace", "cloud")["workspace_key"]
    from tests.test_search_space import document
    index.sync_workspace(tmp_path / "workspace", [document(1, "backup")])
    result = index.hybrid_search("backup", [key])
    assert result["retrieval_mode"] == "keyword" and result["notice"] == "fixture offline"
    assert result["sources"][0]["message_id"] == 1


def test_embedding_loader_is_offline_and_never_downloads_for_a_query(tmp_path, monkeypatch):
    import fastembed
    seen = {}
    def fake_model(*args, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("not cached")
    monkeypatch.setattr(fastembed, "TextEmbedding", fake_model)
    with pytest.raises(EmbeddingsUnavailable):
        LocalEmbeddings(tmp_path / "empty").encode(["private synthetic text"])
    assert seen["local_files_only"] is True
