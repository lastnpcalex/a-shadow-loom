"""Admin-managed OpenRouter model overrides + Dream picker gating."""

import json

import pytest


# ── model_registry overrides ────────────────────────────────────────────


def _write_overrides(tmp_path, monkeypatch, models):
    import model_registry

    path = tmp_path / "model_overrides.json"
    if models is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(json.dumps({"version": 1, "models": models}), encoding="utf-8")
    monkeypatch.setattr(model_registry, "_overrides_path", lambda: path)
    return model_registry


def test_overrides_add_and_hot_reload(tmp_path, monkeypatch):
    mod = _write_overrides(tmp_path, monkeypatch, [
        {
            "backend": "openrouter", "id": "vendor/test-model",
            "label": "Test Model", "context_length": 131072,
            "routes": ["openrouter", "goose", "weave"], "capabilities": {"tools": True},
        }
    ])
    registry = mod.reload_registry()
    entries = {e["value"]: e for e in registry.entries("openrouter")}
    assert "openrouter:vendor/test-model" in entries
    weave = {e["value"] for e in registry.entries("weave")}
    assert "openrouter:vendor/test-model" in weave
    goose = {e["value"] for e in registry.entries("goose")}
    assert "goose:openrouter:vendor/test-model" in goose
    assert mod.openrouter_models()[0]["name"].startswith("openrouter:")


def test_overrides_replace_same_id_catalog_entry(tmp_path, monkeypatch):
    mod = _write_overrides(tmp_path, monkeypatch, [
        {
            "backend": "openrouter", "id": "z-ai/glm-5.3-flash",
            "label": "GLM 5.3 Flash (edited)", "context_length": 262144,
            "routes": ["openrouter", "goose", "weave"], "capabilities": {"tools": True},
        }
    ])
    registry = mod.reload_registry()
    entry = {e["value"]: e for e in registry.entries("openrouter")}["openrouter:z-ai/glm-5.3-flash"]
    assert entry["label"] == "OpenRouter GLM 5.3 Flash (edited)"
    model = registry.model("openrouter:z-ai/glm-5.3-flash")
    assert model["context_length"] == 262144


def test_overrides_reject_non_openrouter_backend(tmp_path, monkeypatch):
    mod = _write_overrides(tmp_path, monkeypatch, [])
    with pytest.raises(ValueError):
        mod.validate_openrouter_overrides([
            {"backend": "anthropic", "id": "claude-test", "label": "X",
             "context_length": 1000, "routes": ["anthropic"]},
        ])


def test_overrides_reject_invalid_entries(tmp_path, monkeypatch):
    mod = _write_overrides(tmp_path, monkeypatch, [])
    with pytest.raises(ValueError):
        mod.validate_openrouter_overrides([
            {"backend": "openrouter", "id": "vendor/dup", "label": "dup",
             "context_length": 10, "routes": ["openrouter"]},
            {"backend": "openrouter", "id": "vendor/dup", "label": "dup2",
             "context_length": 10, "routes": ["openrouter"]},
        ])
    with pytest.raises(ValueError):
        mod.validate_openrouter_overrides([
            {"backend": "openrouter", "id": "vendor/x", "label": "X",
             "context_length": 10, "routes": ["anthropic"]},
        ])


def test_save_writes_file_and_reloads(tmp_path, monkeypatch):
    mod = _write_overrides(tmp_path, monkeypatch, None)
    cleaned = mod.save_openrouter_overrides([
        {"backend": "openrouter", "id": "vendor/saved-model", "label": "Saved",
         "context_length": 65536, "routes": ["openrouter", "weave"]},
    ])
    assert cleaned[0]["id"] == "vendor/saved-model"
    saved = json.loads((tmp_path / "model_overrides.json").read_text(encoding="utf-8"))
    assert saved["models"][0]["id"] == "vendor/saved-model"
    assert any(e["value"] == "openrouter:vendor/saved-model" for e in mod.REGISTRY.entries("openrouter"))


# ── models editor endpoint ──────────────────────────────────────────────


async def test_models_endpoint_lists_openrouter(client, monkeypatch, tmp_path):
    import model_registry

    path = tmp_path / "model_overrides.json"
    path.write_text(json.dumps({"version": 1, "models": []}), encoding="utf-8")
    monkeypatch.setattr(model_registry, "_overrides_path", lambda: path)
    resp = await client.get("/api/models/openrouter")
    assert resp.status_code == 200
    data = resp.json()
    ids = {m["id"] for m in data["models"]}
    assert "z-ai/glm-5.3-flash" in ids
    assert data["overrides_active"] is False


async def test_models_endpoint_validates_before_reload(client, monkeypatch, tmp_path):
    import model_registry

    path = tmp_path / "model_overrides.json"
    path.write_text(json.dumps({"version": 1, "models": []}), encoding="utf-8")
    monkeypatch.setattr(model_registry, "_overrides_path", lambda: path)
    resp = await client.post("/api/models/openrouter", json={"models": [
        {"backend": "anthropic", "id": "nope", "label": "nope",
         "context_length": 10, "routes": ["anthropic"]},
    ]})
    assert resp.status_code == 400
    # overrides file untouched
    assert json.loads(path.read_text(encoding="utf-8"))["models"] == []


# ── Dream availability gating ───────────────────────────────────────────


async def test_cc_models_hides_dream_when_down(client, monkeypatch):
    import server

    monkeypatch.setattr(server, "_dream_picker_cache", {"ok": False, "checked_at": server.time.time()})
    resp = await client.get("/api/cc-models")
    assert resp.status_code == 200
    groups = resp.json()
    names = [g["group"] for g in groups]
    assert "Dream via Claude Code" not in names
    for group in groups:
        for m in group["models"]:
            assert not str(m.get("value", "")).lower().startswith(("goose:dream:", "goose:auto:dream:", "dream:"))


async def test_all_models_omits_dream_when_down(client, monkeypatch):
    import dream_client
    import server

    async def _down(host=None, timeout=2.0):
        return None

    monkeypatch.setattr(dream_client, "health", _down)
    server._ALL_ENGINES_CACHE = None
    resp = await client.get("/api/local/all-models")
    assert resp.status_code == 200
    models = resp.json()["models"]
    assert not [m for m in models if m.get("backend") == "dream"]
    # OpenRouter registry selectors are served for the Weave dropdown.
    assert any(m.get("backend") == "openrouter" for m in models)