"""Shared, offline model metadata. Configuration is never runtime attestation.

Selectors are stable persisted API values; a model identity and a harness route
are separate things. Discovery augments these defaults, never rewrites selections.
No provider calls, credentials, or local-model tuning are loaded here.
"""

from copy import deepcopy
import json
from pathlib import Path
import re


# Route policy, not a per-model allowlist. New supported models are data-only.
ROUTES = {
    "anthropic": {"prefix": "", "label_prefix": "", "group": "Anthropic", "mode": "claude", "harness": "claude", "backend": "anthropic", "evidence": "runtime_init_or_provider_message_start"},
    "codex": {"prefix": "codex-", "label_prefix": "Codex ", "group": "ChatGPT Codex (codex)", "mode": "codex", "harness": "codex", "backend": "openai", "evidence": "app_server_thread_response"},
    "gemini": {"prefix": "gemini:", "label_prefix": "Gemini ", "group": "Antigravity (agy)", "mode": "gemini", "harness": "gemini", "backend": "antigravity", "evidence": "runtime_init"},
    "umans": {"prefix": "", "label_prefix": "", "group": "Umans AI", "mode": "umans", "harness": "claude", "backend": "umans", "evidence": "runtime_init_or_provider_message_start"},
    "openrouter": {"prefix": "openrouter:", "label_prefix": "OpenRouter ", "group": "OpenRouter", "mode": "openrouter", "harness": "claude", "backend": "openrouter", "evidence": "provider_message_start"},
    "goose": {"prefix": "goose:openrouter:", "label_prefix": "Goose OpenRouter ", "group": "Goose ACP", "mode": "goose", "harness": "goose", "backend": "openrouter", "evidence": "acp_model_state_or_configured"},
    "goose_smart": {"prefix": "goose:auto:openrouter:", "label_prefix": "Goose Smart ", "group": "Goose ACP - Smart/Subagents", "mode": "goose", "harness": "goose", "backend": "openrouter", "evidence": "acp_model_state_or_configured"},
    "weave": {"prefix": "openrouter:", "label_prefix": "OpenRouter ", "group": None, "mode": "weave", "harness": "chat_completions", "backend": "openrouter", "evidence": "response_model"},
}

# Export these exact rules to JS; do not maintain a second model-name classifier.
# Exact catalog/discovery selectors take priority over these compatibility rules.
SELECTOR_RULES = [
    {"pattern": r"^goose:", "mode": "goose", "harness": "goose", "backend": None},
    # Display-only compatibility; retired selectors must never fall through to a local model.
    {"pattern": r"^(hermes|prometheus)(:|$)", "mode": "hermes", "harness": "hermes", "backend": None, "retired": True},
    {"pattern": r"^openrouter:", "mode": "openrouter", "harness": "claude", "backend": "openrouter"},
    {"pattern": r"^(codex|gpt-\d|o3|o4)", "mode": "codex", "harness": "codex", "backend": "openai"},
    {"pattern": r"^gemini", "mode": "gemini", "harness": "gemini", "backend": "antigravity"},
    {"pattern": r"^(sonnet|opus|haiku)(\[.*\])?$|^claude-(fable|opus|sonnet|haiku)-", "mode": "claude", "harness": "claude", "backend": "anthropic"},
    {"pattern": r"^umans-", "mode": "umans", "harness": "claude", "backend": "umans"},
    {"pattern": r"^dream:", "mode": "claude", "harness": "claude", "backend": "dream"},
    {"pattern": r"\.gguf$|@llama-server$", "mode": "local", "harness": "claude", "backend": "llama"},
]


def retirement_reason(mode=None, selector=None):
    """Reject retired launch routes without migrating or deleting historical data.

    `dream` was Hermes Dream Space; `dream:<model>` still means Claude Code
    backed by the Dream server, and Goose's Dream backend is unaffected.
    """
    if str(mode or "").strip().lower() in {"hermes", "dream", "prometheus"} or re.match(
        r"^(hermes|prometheus)(:|$)", str(selector or "").strip(), re.IGNORECASE
    ):
        return ("Hermes, Dream Space and Prometheus have been retired. "
                "History is preserved. Choose and save another harness/model in conversation settings to continue.")
    return None


class ModelRegistry:
    def __init__(self, data):
        if data.get("version") != 1:
            raise ValueError("Unsupported model catalog version")
        self.models = deepcopy(data.get("models", []))
        self.fallback_groups = deepcopy(data.get("fallback_groups", {}))
        self._identities = {}
        self._selectors = {}
        for model in self.models:
            required = {"backend", "id", "label", "context_length", "routes"}
            if not required <= model.keys():
                raise ValueError(f"Model missing required fields: {required - model.keys()}")
            if model["backend"] not in {r["backend"] for r in ROUTES.values()}:
                raise ValueError("Unsupported model backend; a new backend needs an adapter")
            if not all(isinstance(model[k], str) and model[k].strip() for k in ("id", "label")):
                raise ValueError("Model id/label must be non-empty strings")
            if type(model["context_length"]) is not int or model["context_length"] <= 0:
                raise ValueError("context_length must be a positive integer")
            capabilities = model.get("capabilities", {})
            if not isinstance(capabilities, dict) or any(
                key not in {"tools", "vision", "effort", "xhigh", "max"} or type(value) is not bool
                for key, value in capabilities.items()
            ):
                raise ValueError("capabilities accepts boolean tools/vision/effort/xhigh/max fields")
            aliases = model.get("aliases", [])
            if not isinstance(aliases, list) or any(not isinstance(a, str) or not a.strip() for a in aliases):
                raise ValueError("aliases must be a list of non-empty strings")
            if aliases and model["backend"] != "openrouter":
                raise ValueError("Custom aliases are OpenRouter-only; native aliases belong to the harness")
            threshold = model.get("handoff_threshold")
            if threshold is not None and (type(threshold) is not int or not 0 < threshold < model["context_length"]):
                raise ValueError("handoff_threshold must fit inside context_length")
            if not isinstance(model["routes"], list) or not model["routes"] or len(set(model["routes"])) != len(model["routes"]):
                raise ValueError("routes must be a non-empty unique list")
            identity = (model["backend"], model["id"].casefold())
            if identity in self._identities:
                raise ValueError(f"Duplicate model identity: {identity}")
            self._identities[identity] = model
            for name in model["routes"]:
                if name not in ROUTES:
                    raise ValueError(f"Unknown harness route: {name}")
                if ROUTES[name]["backend"] != model["backend"]:
                    raise ValueError(f"Route {name} cannot serve backend {model['backend']}")
                if name == "weave":
                    continue  # Same selector, different intentional conversation mode.
                value = ROUTES[name]["prefix"] + model["id"]
                if value.casefold() in self._selectors:
                    raise ValueError(f"Duplicate selector: {value}")
                self._selectors[value.casefold()] = self._entry(model, name)
            # Keep historical bare slugs usable even if not in the dropdown.
            default_route = {"openrouter": "openrouter", "anthropic": "anthropic", "openai": "codex", "antigravity": "gemini", "umans": "umans"}[model["backend"]]
            default = self._entry(model, default_route)
            for alias in [model["id"], *model.get("aliases", [])]:
                key = alias.casefold()
                if key in self._selectors:
                    existing = self._selectors[key]
                    if alias == default["value"] and existing.get("model_id") == model["id"] and existing.get("backend") == model["backend"]:
                        continue
                    raise ValueError(f"Duplicate selector/alias: {alias}")
                if any(re.search(r["pattern"], key) and r["mode"] != default["mode"] for r in SELECTOR_RULES):
                    raise ValueError(f"Alias conflicts with an existing harness selector: {alias}")
                self._selectors[key] = {**default, "value": alias}
        for family, rows in self.fallback_groups.items():
            mode = "claude" if family == "anthropic" else family
            backend = {"anthropic": "anthropic", "codex": "openai", "gemini": "antigravity", "umans": "umans"}[family]
            for row in rows:
                key = row["value"].casefold()
                self._selectors.setdefault(key, {**row, "mode": mode, "harness": "claude" if mode == "umans" else mode, "backend": backend})

    @staticmethod
    def _entry(model, route):
        r = ROUTES[route]
        return {"value": r["prefix"] + model["id"], "label": r["label_prefix"] + model["label"],
                "model_id": model["id"], "mode": r["mode"], "harness": r["harness"],
                "backend": model["backend"], "context_length": model["context_length"],
                "capabilities": deepcopy(model.get("capabilities", {})), "expected_evidence": r["evidence"],
                "aliases": [model["id"], *model.get("aliases", [])] if route not in {"goose", "goose_smart", "weave"} else []}

    def entries(self, route):
        return [self._entry(m, route) for m in self.models if route in m["routes"]]

    def fallback(self, family):
        rows = deepcopy(self.fallback_groups.get(family, []))
        extra = self.entries(family)
        replaced = {row["value"].casefold() for row in extra}
        return [row for row in rows if row["value"].casefold() not in replaced] + extra

    def resolve(self, selector):
        raw = str(selector or "").strip()
        key = raw.casefold()
        if key in self._selectors:
            return deepcopy(self._selectors[key])
        for rule in SELECTOR_RULES:
            if re.search(rule["pattern"], key):
                return {"value": raw, **{k: v for k, v in rule.items() if k != "pattern"}}
        return {}

    def model(self, selector):
        raw = unwrap_goose(selector)
        entry = self.resolve(raw)
        if not entry.get("model_id") and raw.lower().startswith("openrouter:"):
            raw = raw.split(":", 1)[1]
            entry = self.resolve(raw)
        identity = (entry.get("backend"), str(entry.get("model_id", raw)).casefold())
        return deepcopy(self._identities.get(identity))

    def public(self, groups=()):
        entries = deepcopy(self._selectors)
        # Dynamic cache/OAuth discovery supplies selectors, never 'verified'.
        for group in groups:
            name = group.get("group", "")
            for item in group.get("models", []):
                value = item.get("value", "")
                metadata = self.resolve(value)
                if name.startswith("Antigravity"):
                    metadata.update(mode="gemini", harness="gemini", backend="antigravity")
                elif name.startswith("ChatGPT Codex"):
                    metadata.update(mode="codex", harness="codex", backend="openai")
                elif name.startswith("Anthropic"):
                    metadata.update(mode="claude", harness="claude", backend="anthropic")
                entries[value.casefold()] = {**metadata, "value": value, "label": item.get("label", value)}
        return {"version": 1, "entries": entries, "rules": deepcopy(SELECTOR_RULES)}


def unwrap_goose(selector):
    raw = str(selector or "").strip()
    if raw.lower().startswith("goose:"):
        raw = raw.split(":", 1)[1]
        if raw.lower().startswith(("auto:", "approve:", "smart_approve:", "smart-approve:", "subagents:", "chat:")):
            raw = raw.split(":", 1)[1]
    return raw


# ── User-managed OpenRouter model overrides ─────────────────────────────
# model_overrides.json lives next to model_catalog.json and is gitignored.
# Entries may add new OpenRouter models or replace catalog entries by id.
# Only backend:"openrouter" overrides are accepted, so the tab cannot
# accidentally break native Anthropic/Codex/Gemini/Umans routes.

OVERRIDES_FILENAME = "model_overrides.json"


def _catalog_path():
    return Path(__file__).with_name("model_catalog.json")


def _overrides_path():
    return Path(__file__).with_name(OVERRIDES_FILENAME)


def load_overrides():
    """Read the gitignored override file; missing file means no overrides."""
    path = _overrides_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        raise ValueError(f"model_overrides.json is not valid JSON: {e}")
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list) or any(not isinstance(m, dict) for m in models):
        raise ValueError("model_overrides.json must contain a 'models' list of objects")
    return models


def _merge_overrides(catalog_data, overrides):
    """Return catalog data with override models replacing same-id entries."""
    merged = deepcopy(catalog_data)
    ids = {str(m.get("id", "")).casefold() for m in overrides}
    merged["models"] = [
        m for m in merged.get("models", [])
        if not (m.get("backend") == "openrouter" and str(m["id"]).casefold() in ids)
    ] + deepcopy(overrides)
    return merged


def validate_openrouter_overrides(models):
    """Validate candidate overrides by dry-building a registry; returns cleaned list."""
    if not isinstance(models, list) or not all(isinstance(m, dict) for m in models):
        raise ValueError("models must be a list of model objects")
    cleaned = deepcopy(models)
    for m in cleaned:
        if m.get("backend") != "openrouter":
            raise ValueError(
                f"Only OpenRouter-backend models may be edited here "
                f"(got backend {m.get('backend')!r} for id {m.get('id')!r})")
    ModelRegistry(_merge_overrides(json.loads(_catalog_path().read_text(encoding="utf-8")), cleaned))
    return cleaned


def save_openrouter_overrides(models):
    """Validate, atomically persist, and hot-reload the registry."""
    cleaned = validate_openrouter_overrides(models)
    path = _overrides_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"version": 1, "models": cleaned}, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    reload_registry()
    return cleaned


def reload_registry():
    """Rebuild the global registry from catalog + overrides (hot reload)."""
    global REGISTRY
    REGISTRY = ModelRegistry(_merged_catalog_data())
    return REGISTRY


def _merged_catalog_data():
    return _merge_overrides(json.loads(_catalog_path().read_text(encoding="utf-8")), load_overrides())


def openrouter_catalog_models():
    """Full merged openrouter model definitions (id, label, context, aliases...)."""
    return [deepcopy(m) for m in REGISTRY.models if m["backend"] == "openrouter"]


REGISTRY = ModelRegistry(_merged_catalog_data())


def resolve(selector):
    return REGISTRY.resolve(selector)


def is_openrouter(selector):
    return resolve(unwrap_goose(selector)).get("backend") == "openrouter"


def openrouter_slug(selector):
    raw = unwrap_goose(selector)
    entry = resolve(raw)
    if entry.get("model_id"):
        return entry["model_id"]
    if raw.lower().startswith("openrouter:"):
        raw = raw.split(":", 1)[1].strip()
    return resolve(raw).get("model_id") or raw


def openrouter_models():
    return [{"name": e["value"], "label": e["label"], "backend": e["backend"], "context_length": e["context_length"]}
            for e in REGISTRY.entries("weave")]
