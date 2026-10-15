"""Data-only model registration and Python/JS routing conformance. No live LLMs."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import model_registry as registry
from tests.js_model_registry import evaluate


CATALOG = Path(__file__).resolve().parent.parent / "model_catalog.json"
CANARY = {
    "backend": "openrouter", "id": "fixture-vendor/registry-canary",
    "label": "Registry Canary", "context_length": 360_000,
    "routes": ["openrouter", "goose", "goose_smart", "weave"],
    "aliases": ["registry-canary", "openrouter:legacy-canary"], "capabilities": {"tools": True, "effort": True, "max": False, "xhigh": False},
}


@pytest.fixture
def canary_registry(monkeypatch):
    import server
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    data["models"].append(deepcopy(CANARY))
    catalog = registry.ModelRegistry(data)
    monkeypatch.setattr(registry, "REGISTRY", catalog)
    # Nothing outside this single in-memory definition changes. No user config.
    monkeypatch.setattr(server, "CC_MODELS", server._initial_cc_models())
    monkeypatch.setattr(server.config, "enable_goose", True)
    return catalog


async def test_one_definition_drives_both_harnesses_and_weave(client, canary_registry, monkeypatch):
    import goose_client
    import model_context
    import openrouter_client
    import replay_budget
    import server
    slug = CANARY["id"]
    rows = (await client.get("/api/cc-models")).json()
    groups = {g["group"]: {m["value"]: m for m in g["models"]} for g in rows}
    for route in ("openrouter", "goose", "goose_smart"):
        spec = registry.ROUTES[route]
        selector = spec["prefix"] + slug
        entry = groups[spec["group"]][selector]
        assert entry["model_id"] == slug and entry["context_length"] == 360_000
        assert entry["harness"] == spec["harness"] and entry["backend"] == "openrouter"
        assert server._mode_for_cc_model(selector) == spec["mode"]
        assert model_context.handoff_threshold(selector) == 315_000
        assert not model_context.is_local_llama(selector)
        assert await replay_budget.context_capacity(selector, spec["harness"]) == (360_000, "OpenRouter model metadata")
        assert "status" not in entry and "effective_model" not in entry
    assert server._mode_for_cc_model("registry-canary") == "openrouter"
    assert openrouter_client.model_slug("registry-canary") == slug
    assert openrouter_client.model_slug("openrouter:legacy-canary") == slug
    assert await replay_budget.context_capacity("openrouter:legacy-canary", "claude") == (360_000, "OpenRouter model metadata")
    assert openrouter_client.model_slug("openrouter:" + slug) == slug
    assert server._goose_selector_for_model("registry-canary") == "goose:openrouter:" + slug
    monkeypatch.setattr(openrouter_client, "api_key", lambda: "fixture-not-a-key")
    monkeypatch.setattr(openrouter_client, "base_url", lambda: "https://provider.invalid/api/v1")
    for prefix in ("goose:openrouter:", "goose:auto:openrouter:"):
        env = goose_client._goose_env(prefix + slug, "default")
        assert env["GOOSE_PROVIDER"] == "openrouter" and env["GOOSE_MODEL"] == slug
        evidence = goose_client._goose_model_attestation(prefix + slug)
        assert evidence["status"] == "configured" and evidence["effective_model"] is None
    assert any(m["name"] == "openrouter:" + slug for m in openrouter_client.public_models())
    assert evaluate("[_isOpenRouterModel('registry-canary'), _agentKindForModel('registry-canary', {mode:'goose'}), _agentKindForModel('goose:openrouter:fixture-vendor/registry-canary', {mode:'codex'}), _gooseRouteModelValue('registry-canary')]") == [
        True, "openrouter", "goose", "goose:openrouter:" + slug]
    assert evaluate("[_supportsEffortValue('registry-canary'), _allowsMaxEffortValue('registry-canary'), _allowsXhighEffortValue('goose:openrouter:fixture-vendor/registry-canary')]") == [True, False, False]


@pytest.mark.parametrize("backend,route,model_id", [
    ("anthropic", "anthropic", "claude-fixture-9"),
    ("openai", "codex", "gpt-99-fixture"),
    ("antigravity", "gemini", "gemini-99.0-flash-high"),
    ("umans", "umans", "umans-fixture"),
])
async def test_native_catalog_pins_supplement_discovery_and_supply_context(monkeypatch, client, backend, route, model_id):
    import server
    import model_context
    import replay_budget
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    data["models"].append({"backend": backend, "id": model_id, "label": "Fixture", "context_length": 120_000, "routes": [route]})
    catalog = registry.ModelRegistry(data)
    monkeypatch.setattr(registry, "REGISTRY", catalog)
    monkeypatch.setattr(server.config, "enable_umans_models", True)
    spec = registry.ROUTES[route]
    # Simulate an already-refreshed discovery group, with no canary in it.
    monkeypatch.setattr(server, "CC_MODELS", [{"group": spec["group"], "models": [{"value": "existing", "label": "Existing discovery"}]}])
    if route == 'codex':
        monkeypatch.setattr(server, 'load_local_codex_models', lambda: [])
    response = await client.get("/api/cc-models")
    values = {m["value"] for m in response.json()[0]["models"]}
    selector = spec["prefix"] + model_id
    expected = {"existing", selector}
    if route == 'codex':
        expected.update(row['value'] for row in catalog.fallback('codex'))
    assert values == expected
    assert server._mode_for_cc_model(selector) == spec["mode"]
    if backend == "anthropic":
        assert model_context.is_anthropic(selector)
    assert await replay_budget.context_capacity(selector, spec["harness"]) == (120_000, "catalog context (runtime unavailable)")
    assert evaluate(f"_agentKindForModel({json.dumps(selector)}, {{mode:'goose'}})") == spec["mode"]


async def test_runtime_and_native_resume_still_win_over_catalog(canary_registry, monkeypatch):
    import replay_budget
    capacity, source = await replay_budget.context_capacity("registry-canary", "goose", runtime_context=32_768)
    assert capacity == 32_768 and source == "harness runtime context"
    monkeypatch.setattr(replay_budget, "context_capacity", AsyncMock(side_effect=AssertionError("native resume must not probe")))
    assert await replay_budget.check_fresh_payload("large" * 5000, model="registry-canary", harness="goose", resume_session_id="existing") is None


def test_python_js_share_all_catalog_selectors_and_compatibility_rules():
    exported = registry.REGISTRY.public()
    selectors = list(exported["entries"]) + [
        "codex:gpt-99-future", "gpt-6-astra", "gpt-oss-120b", "gemini:future",
        "goose:auto:openrouter:unknown/model", "hermes:custom:fixture",
        "openrouter:future/unknown", "claude-fable-5", "claude-opus-4-8[1m]",
        "fixture.gguf", "fixture@llama-server", "dream:fixture", "unknown", "",
    ]
    actual = evaluate(json.dumps(selectors) + ".map(s => {const r = EXOModelRegistry.resolve(s); return [r.mode || null, r.harness || null, r.backend || null];})")
    expected = [[registry.resolve(s).get(k) for k in ("mode", "harness", "backend")] for s in selectors]
    assert actual == expected
    assert evaluate("_agentKindForModel('openrouter:future/unknown', {mode:'goose'})") == "openrouter"
    assert registry.resolve("unknown") == {}


def test_explicit_local_and_hermes_modes_are_not_inferred_from_cc_selector():
    import server
    assert server._mode_for_cc_model("fixture.gguf") is None
    assert server._mode_for_cc_model("hermes:fixture") is None


def test_schema_rejects_invalid_metadata_and_route_collisions():
    base = {"version": 1, "models": [CANARY]}
    mutations = [
        {"context_length": 0}, {"context_length": True}, {"context_length": "360000"},
        {"handoff_threshold": 400_000}, {"routes": []}, {"routes": ["missing"]},
        {"routes": ["codex"]}, {"routes": ["goose", "goose"]},
        {"aliases": ["sonnet"]}, {"backend": "unimplemented"},
        {"capabilities": {"tools": "yes"}}, {"aliases": "wrong type"},
    ]
    for update in mutations:
        data = deepcopy(base)
        data["models"][0].update(update)
        with pytest.raises(ValueError):
            registry.ModelRegistry(data)
    duplicate = deepcopy(base)
    duplicate["models"].append(deepcopy(CANARY))
    with pytest.raises(ValueError, match="Duplicate"):
        registry.ModelRegistry(duplicate)


async def test_unknown_openrouter_model_never_probes_local_context(monkeypatch):
    import replay_budget
    monkeypatch.setattr(replay_budget, "_local_capacity", AsyncMock(side_effect=AssertionError("remote model must not probe llama-server")))
    assert await replay_budget.context_capacity("goose:chat:openrouter:unknown/model", "goose") == (200_000, "conservative provider fallback (runtime unavailable)")


async def test_bootstrap_is_no_cache_pure_metadata(client, canary_registry, monkeypatch):
    import server
    monkeypatch.setattr(server, "health_check", AsyncMock(side_effect=AssertionError("registry must not probe local models")))
    response = await client.get("/api/model-registry.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["content-type"].startswith("application/javascript")
    assert evaluate("EXOModelRegistry.resolve('registry-canary').harness", data={"version": 1}, extra=response.text) == "claude"
    for forbidden in ("api_key", "token", "effective_model", '"status": "verified"'):
        assert forbidden not in response.text


def test_old_running_host_fallback_keeps_known_picker_routes():
    groups = [{"group": "OpenRouter", "models": [{"value": "openrouter:fixture/new", "label": "Fixture"}]},
              {"group": "Goose ACP", "models": [{"value": "goose:openrouter:fixture/new", "label": "Fixture"}]}]
    assert evaluate("[_isOpenRouterModel('fixture/new'), _isGooseValue('goose:openrouter:fixture/new')]",
                    data={"version": 1}, extra="EXOModelRegistry.mergeGroups(" + json.dumps(groups) + ");") == [True, True]


def test_codex_picker_retains_catalog_pin_with_partial_discovery(monkeypatch):
    import server
    rows = [{'value': 'codex-gpt-6-sol', 'label': 'Discovered Sol'}]
    monkeypatch.setattr(server, 'load_local_codex_models', lambda: rows)
    models = server.get_initial_codex_models()
    assert any(row['value'] == 'codex-gpt-6.1-sol' for row in models)
    assert [row for row in models if row['value'] == 'codex-gpt-6-sol'] == rows
    assert rows == [{'value': 'codex-gpt-6-sol', 'label': 'Discovered Sol'}]
    assert all('effective_model' not in row and 'status' not in row for row in models)


def test_codex_picker_tracks_cache_updates_after_startup_without_provider_call(monkeypatch, tmp_path):
    import server
    import codex_client
    cache = tmp_path / '.codex' / 'models_cache.json'
    cache.parent.mkdir()
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.setattr(codex_client, 'discover_codex_models', AsyncMock(
        side_effect=AssertionError('Picker must only read local cache')))
    cache.write_text(json.dumps({'models': [{'slug': 'gpt-old-fixture', 'display_name': 'Old fixture'}]}))
    startup = [{'group': 'ChatGPT Codex (codex)', 'models': server.get_initial_codex_models()}]
    monkeypatch.setattr(server, 'CC_MODELS', startup)
    cache.write_text(json.dumps({'models': [
        {'slug': 'gpt-6.1-sol', 'display_name': 'Sol 6.1 discovered'},
        {'slug': 'gpt-new-fixture', 'display_name': 'New fixture'},
        {'slug': 'gpt-hidden-fixture', 'display_name': 'Hidden', 'visibility': 'hide'},
    ]}))
    models = server._cc_models_with_runtime_goose_llama()[0]['models']
    values = [row['value'] for row in models]
    assert values.count('codex-gpt-6.1-sol') == 1
    assert models[0]['label'] == 'Codex (Sol 6.1 discovered)'
    assert 'codex-gpt-new-fixture' in values
    assert 'codex-gpt-old-fixture' not in values
    assert 'codex-gpt-hidden-fixture' not in values
    assert startup[0]['models'][0]['value'] == 'codex-gpt-old-fixture'
    codex_client.discover_codex_models.assert_not_awaited()
    cache.write_text('incomplete cache write')
    recovered = server._cc_models_with_runtime_goose_llama()[0]['models']
    assert any(row['value'] == 'codex-gpt-6.1-sol' for row in recovered)
    assert any(row['value'] == 'codex-gpt-old-fixture' for row in recovered)


async def test_pareto_model_group_in_picker(client, monkeypatch):
    import server
    monkeypatch.setattr(server, "CC_MODELS", server._initial_cc_models())
    monkeypatch.setattr(server.config, "enable_goose", True)
    resp = await client.get("/api/cc-models")
    assert resp.status_code == 200
    groups = resp.json()
    assert len(groups) > 0
    pareto = next((g for g in groups if g["group"] == "Pareto"), None)
    assert pareto is not None, "Pareto group must be present in picker"
    models = {m["value"]: m for m in pareto["models"]}
    assert "gemini:gemini-3.8-flash-high" in models
    assert models["gemini:gemini-3.8-flash-high"]["effort"] == "high"
    assert models["gemini:gemini-3.8-flash-high"]["label"] == "Gemini Flash 3.8 (High)"

    assert "codex-gpt-6.1-sol" in models
    assert models["codex-gpt-6.1-sol"]["effort"] == "medium"
    assert models["codex-gpt-6.1-sol"]["label"] == "GPT 6.1 Sol (Medium)"

    assert "goose:openrouter:z-ai/glm-5.3-flash" in models
    assert models["goose:openrouter:z-ai/glm-5.3-flash"]["effort"] == "xhigh"
    assert models["goose:openrouter:z-ai/glm-5.3-flash"]["label"] == "GLM 5.3 Flash (Extra High in Goose)"

    sonnet_models = [m for m in pareto["models"] if "sonnet" in m["value"].lower()]
    assert [m["value"] for m in sonnet_models] == ["claude-sonnet-5-5[1m]"]
    assert sonnet_models[0]["effort"] == "medium"
    assert sonnet_models[0]["label"] == "Claude Sonnet 5.5 (Medium in CC, 1M)"
    from model_context import is_1m_anthropic
    assert is_1m_anthropic(sonnet_models[0]["value"])

    # Verify routing
    assert server._mode_for_cc_model("gemini:gemini-3.8-flash-high") == "gemini"
    assert server._mode_for_cc_model("codex-gpt-6.1-sol") == "codex"
    assert server._mode_for_cc_model("goose:openrouter:z-ai/glm-5.3-flash") == "goose"
    assert server._mode_for_cc_model("claude-sonnet-5-5[1m]") == "claude"
