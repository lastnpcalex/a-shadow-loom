"""Model classification and legacy context-threshold compatibility helpers.

These thresholds apply when Loom must rebuild a provider session from the
canonical branch, or when it can compact a Claude session before switching to
a smaller target. Native agent continuations retain provider-side context and
must not be gated by the cumulative size of Loom's stored branch. Production
agent admission and handoff checks use replay_budget's destination-aware full
payload estimate, not these legacy branch-length thresholds.
"""

import re as _re
import model_registry


# ~12% safety margin under each provider's real window, because token_estimate
# is len(content)//3 and can drift on code-heavy or tool-heavy turns.
THRESHOLD_ANTHROPIC_STD = 175_000   # 200k window → 175k gate
THRESHOLD_CODEX = 239_000           # Codex history rebuild into a 272k effective window
THRESHOLD_GEMINI = 175_000          # Gemini 1M+ but treated as non-1M for CC handoff
THRESHOLD_LOCAL_LLAMA = 220_000     # local llama-server runs Qwen 262k-context;
                                    # 220k leaves ~16% headroom for token-estimate drift


def _catalog_threshold(model_id):
    definition = model_registry.REGISTRY.model(model_id)
    if definition:
        return definition.get("handoff_threshold") or int(definition["context_length"] * 0.875)
    return THRESHOLD_ANTHROPIC_STD


# Backwards-compatible exports, derived from data rather than copied budgets.
THRESHOLD_OPENROUTER_GLM_53_FLASH = _catalog_threshold("z-ai/glm-5.3-flash")
THRESHOLD_OPENROUTER_GLM_52 = _catalog_threshold("z-ai/glm-5.2")
THRESHOLD_OPENROUTER_KIMI_K27_CODE = _catalog_threshold("moonshotai/kimi-k2.7-code")
THRESHOLD_OPENROUTER_GPT_56_LUNA = _catalog_threshold("openai/gpt-5.6-luna")
THRESHOLD_OPENROUTER_DEEPSEEK_V4_FLASH = _catalog_threshold("deepseek/deepseek-v4-flash-0731")

# claude-<family>-<major>[-<minor>][-date], minor optional (matches server.py).
_1M_ID_RE = _re.compile(r"^claude-(fable|opus|sonnet|haiku)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?")

# Minimum version per family that actually has a 1M tier. Opus only from 4.7;
# Opus 4.6[1m] rate-limits/fails. Sonnet across our range. Haiku never.
_1M_MIN_VERSION = {"fable": (5, 0), "opus": (4, 7), "sonnet": (4, 5)}


def is_1m_anthropic(model_id: str) -> bool:
    """True for 1M-context Anthropic models.

    Loom surfaces supported 1M variants as `<base>[1m]` — either an alias
    (`sonnet[1m]`, `opus[1m]`, which resolve to the family's latest version) or a
    pinned full id (`claude-opus-4-7[1m]`). Family-level aliases always count
    since the latest version supports 1M; pinned ids are gated by version so an
    old `claude-opus-4-6[1m]` does not bypass the handoff as if it were real 1M.
    """
    if not model_id:
        return False
    m = model_id.lower()
    base = m.split("[")[0] if "[" in m else m
    if base.startswith("claude-fable-"):
        match = _1M_ID_RE.match(base)
        if not match:
            return False
        version = (int(match.group(2)), int(match.group(3) or 0))
        return version >= _1M_MIN_VERSION["fable"]
    if "[1m]" not in m:
        return False
    if base in ("sonnet", "opus"):
        return True
    match = _1M_ID_RE.match(base)
    if not match:
        return False
    family = match.group(1)
    version = (int(match.group(2)), int(match.group(3) or 0))
    floor = _1M_MIN_VERSION.get(family)
    return floor is not None and version >= floor


def _context_model_id(model_id: str) -> str:
    """Normalize Loom wrapper selectors before applying context gates."""
    return model_registry.unwrap_goose(model_id).lower()


def is_anthropic(model_id: str) -> bool:
    """True for any Anthropic value Loom surfaces in the dropdown — either the
    sonnet/opus/haiku alias or a full `claude-<family>-...` ID, with optional
    `[1m]` suffix. Used by routing to decide between cloud Anthropic, Gemini,
    and local-llama paths."""
    return model_registry.resolve(_context_model_id(model_id)).get("backend") == "anthropic"


def is_gemini(model_id: str) -> bool:
    return model_registry.resolve(_context_model_id(model_id)).get("mode") == "gemini"


def is_codex(model_id: str) -> bool:
    """True for Codex selectors and raw OpenAI model IDs.

    The numeric GPT match deliberately excludes names such as ``gpt-oss``.
    """
    return model_registry.resolve(_context_model_id(model_id)).get("mode") == "codex"


def is_openrouter(model_id: str) -> bool:
    return model_registry.is_openrouter(model_id)


def _openrouter_slug(model_id: str) -> str:
    m = _context_model_id(model_id)
    return m.split(":", 1)[1] if m.startswith("openrouter:") else m


def is_local_llama(model_id: str) -> bool:
    """Local llama models — anything not owned by a remote provider."""
    if not model_id:
        return False
    if (
        is_gemini(model_id)
        or is_codex(model_id)
        or is_anthropic(model_id)
        or is_openrouter(model_id)
    ):
        return False
    return True


def handoff_threshold(target_model: str) -> int:
    """Safe payload threshold for a history rebuild or Claude handoff."""
    if is_openrouter(target_model):
        return _catalog_threshold(target_model)
    if is_codex(target_model):
        return THRESHOLD_CODEX
    if is_local_llama(target_model):
        return THRESHOLD_LOCAL_LLAMA
    if is_gemini(target_model):
        return THRESHOLD_GEMINI
    return THRESHOLD_ANTHROPIC_STD


def needs_handoff(target_model: str, branch_tokens: int) -> bool:
    """Whether a prospective rebuild/handoff exceeds the target payload bound.

    Callers must first establish that the turn actually needs a history
    rebuild or has a compactable Claude source. This is not a native-session
    context check.
    """
    if is_1m_anthropic(target_model):
        return False
    return branch_tokens > handoff_threshold(target_model)


def provider_label(target_model: str) -> str:
    """Human-readable target family — used in toasts."""
    if is_1m_anthropic(target_model):
        return "Anthropic 1M"
    if is_gemini(target_model):
        return "Gemini"
    if is_openrouter(target_model):
        return "OpenRouter"
    if is_codex(target_model):
        return "Codex"
    if is_local_llama(target_model):
        return "Local"
    return "Anthropic"
