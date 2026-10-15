import json

import httpx
import pytest

import database as db
import mcp_search_space
import server
from search_space import SearchBackendUnavailable, SearchIndex, SearchOperator, SearchPolicyError, workspace_key


def document(message_id, content, *, conversation=1, title="Fixture", thread="thread-a"):
    return {
        "message_id": message_id,
        "conversation_id": conversation,
        "conversation_title": title,
        "mode": "codex",
        "role": "assistant",
        "content": content,
        "loom_thread_id": thread,
        "root_message_id": 10,
        "created_at": float(message_id),
    }


def test_index_policy_is_explicit_external_and_exclusion_removes_copies(tmp_path):
    workspace = tmp_path / "owned-workspace"
    workspace.mkdir()
    index = SearchIndex(tmp_path / "state" / "search.sqlite3")
    assert index.policy_for(workspace) == "excluded"
    with pytest.raises(ValueError, match="excluded"):
        index.sync_workspace(workspace, [document(1, "alpha migration result")])

    policy = index.set_policy(workspace, "sensitive")
    report = index.sync_workspace(workspace, [document(1, "alpha migration result")])
    assert policy["workspace_key"] == workspace_key(workspace)
    assert report == {"workspace_key": workspace_key(workspace), "policy": "sensitive", "documents": 1, "changed": 1, "removed": 0}
    assert index.search(["migration"], [workspace_key(workspace)])[0]["message_id"] == 1

    # Updates and removals are incremental, and opting out purges the local copy.
    report = index.sync_workspace(workspace, [document(1, "alpha release result"), document(2, "beta")])
    assert report["changed"] == 2 and report["removed"] == 0
    report = index.sync_workspace(workspace, [document(2, "beta")])
    assert report["changed"] == 0 and report["removed"] == 1
    index.set_policy(workspace, "excluded")
    assert index.search(["beta"], [workspace_key(workspace)]) == []


def test_index_keeps_reused_message_ids_separate_across_loom_databases(tmp_path):
    workspace = tmp_path / "shared-workspace"
    workspace.mkdir()
    index = SearchIndex(tmp_path / "search.sqlite3")
    key = index.set_policy(workspace, "cloud")["workspace_key"]

    index.sync_workspace(workspace, [document(1, "alpha from database one")], source_id="database-one")
    index.sync_workspace(workspace, [document(1, "beta from database two")], source_id="database-two")

    assert [row["message_id"] for row in index.search(["alpha"], [key], source_id="database-one")] == [1]
    assert index.search(["beta"], [key], source_id="database-one") == []
    assert [row["message_id"] for row in index.search(["beta"], [key], source_id="database-two")] == [1]
    assert index.search(["alpha"], [key], source_id="database-two") == []


def test_search_mcp_cannot_widen_server_assigned_scope(monkeypatch):
    monkeypatch.setenv("LOOM_API_URL", "http://127.0.0.1:3000")
    monkeypatch.setenv("LOOM_SEARCH_WORKSPACE_KEYS", json.dumps(["allowed-workspace"]))
    captured = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    def post(url, *, json, timeout, verify):
        captured.update({"url": url, "json": json, "timeout": timeout, "verify": verify})
        return Response()

    monkeypatch.setattr(mcp_search_space.httpx, "post", post)
    result = json.loads(mcp_search_space._post(
        "/api/search-space/retrieve",
        {"query": "fixture", "workspace_keys": ["attacker-selected"]},
    ))

    assert result == {"ok": True}
    assert captured["json"]["workspace_keys"] == ["allowed-workspace"]
    assert captured["url"] == "http://127.0.0.1:3000/api/search-space/retrieve"


@pytest.mark.asyncio
async def test_operator_routes_all_cloud_remote_and_any_sensitive_local(tmp_path):
    cloud = tmp_path / "cloud"
    sensitive = tmp_path / "sensitive"
    cloud.mkdir(); sensitive.mkdir()
    index = SearchIndex(tmp_path / "search.sqlite3")
    index.set_policy(cloud, "cloud")
    index.set_policy(sensitive, "sensitive")
    index.sync_workspace(cloud, [document(1, "phoenix deployment was completed")])
    index.sync_workspace(sensitive, [document(2, "private phoenix notes", conversation=2)])
    calls = []

    async def complete(messages, route):
        calls.append((messages, route))
        return '["phoenix", "deployment"]' if len(calls) in {1, 3} else "Found it in [S1]."

    operator = SearchOperator(index, completion=complete)
    result = await operator.ask("Where did I finish Phoenix?", [workspace_key(cloud)])
    assert result["route"] == "cloud" and result["sources"][0]["message_id"] == 1
    assert [route for _, route in calls[:2]] == ["cloud", "cloud"]

    result = await operator.ask("What do I remember about Phoenix?", [workspace_key(cloud), workspace_key(sensitive)])
    assert result["route"] == "local"
    assert [route for _, route in calls[2:]] == ["local", "local"]
    assert {source["message_id"] for source in result["sources"]} == {1, 2}

    index.set_policy(cloud, "excluded")
    with pytest.raises(SearchPolicyError, match="Excluded"):
        operator.plan([workspace_key(cloud)])


@pytest.mark.asyncio
async def test_sensitive_completion_fails_closed_when_llama_is_down(tmp_path, monkeypatch):
    workspace = tmp_path / "sensitive"
    workspace.mkdir()
    index = SearchIndex(tmp_path / "search.sqlite3")
    index.set_policy(workspace, "sensitive")
    operator = SearchOperator(index)

    async def down():
        return {"status": "error", "error": "fixture offline", "models": []}

    monkeypatch.setattr("search_space.operator.llama_client.health_check", down)
    with pytest.raises(SearchBackendUnavailable, match="requires llama-server.*fixture offline"):
        await operator._complete([{"role": "user", "content": "test"}], "local")


@pytest.mark.asyncio
async def test_search_space_api_groups_shared_workspaces_and_never_changes_chat_model(tmp_path, monkeypatch):
    workspace = tmp_path / "shared"
    workspace.mkdir()
    first = await db.create_conversation("First", mode="codex", project_dir=str(workspace))
    second = await db.create_conversation("Second", mode="goose", project_dir=str(workspace))
    await db.add_message(first["id"], "user", "The heliotrope migration completed successfully")
    await db.add_message(second["id"], "assistant", "Follow-up validation was green")
    index = SearchIndex(tmp_path / "external" / "search.sqlite3")
    monkeypatch.setattr(server, "_search_index", index)

    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        workspaces = (await client.get("/api/search-space/workspaces")).json()
        assert len(workspaces) == 1
        item = workspaces[0]
        assert item["conversation_count"] == 2 and item["policy"] == "excluded"
        response = await client.put("/api/search-space/workspaces/policy", json={
            "project_dir": str(workspace), "policy": "cloud",
        })
        assert response.status_code == 200
        indexed = await client.post("/api/search-space/index", json={"workspace_keys": [item["workspace_key"]]})
        assert indexed.status_code == 200
    assert index.search(
        ["heliotrope"], [item["workspace_key"]], source_id=server._search_source_id()
    )[0]["conversation_id"] == first["id"]
    assert (await db.get_conversation(first["id"]))["mode"] == "codex"
    assert (await db.get_conversation(second["id"]))["mode"] == "goose"
