import json
import sys

import httpx
import pytest

import admin_server
from managed_extensions import ExtensionRegistry


@pytest.mark.asyncio
async def test_admin_extension_registry_is_generic_and_external(tmp_path, monkeypatch):
    owner = tmp_path / "owned-project"
    owner.mkdir()
    manifest = owner / "exo-extension.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "id": "fixture.mcp",
        "name": "Fixture MCP",
        "kind": "mcp",
        "workspace_root": ".",
        "cwd": ".",
        "start": {"command": [sys.executable, "-c", "import time; time.sleep(60)"]},
    }), encoding="utf-8")
    registry = ExtensionRegistry(tmp_path / "machine-state")
    monkeypatch.setattr(admin_server, "_extension_registry", registry)

    transport = httpx.ASGITransport(app=admin_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://admin") as client:
        registered = await client.post("/api/extensions/register", json={"manifest_path": str(manifest)})
        assert registered.status_code == 200
        rows = (await client.get("/api/extensions")).json()["extensions"]
        assert len(rows) == 1 and rows[0]["id"] == "fixture.mcp"
        assert rows[0]["workspace_root"] == str(owner.resolve())
        started = await client.post("/api/extensions/fixture.mcp/start")
        assert started.status_code == 200 and started.json()["result"]["pid"]
        blocked = await client.delete("/api/extensions/fixture.mcp")
        assert blocked.status_code == 409
        stopped = await client.post("/api/extensions/fixture.mcp/stop")
        assert stopped.status_code == 200
        removed = await client.delete("/api/extensions/fixture.mcp")
        assert removed.status_code == 200


def test_admin_extension_frontend_has_no_per_service_wiring():
    html = open("static/admin/index.html", encoding="utf-8").read()
    js = open("static/admin/admin-extensions.js", encoding="utf-8").read()
    assert 'id="view-extensions"' in html
    assert '/api/extensions/register' in js
    assert 'data-extension-id' in js
    assert 'exo-extension.json' in html
