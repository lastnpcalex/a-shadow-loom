"""Fresh agent payload budgets. Native resumes never enter this preflight.

Local Goose/Claude inference has a separate per-request local_context_guard,
which admits the complete native payload, including calls inside a tool loop.

Runtime limits win over configured/catalog limits. CLI-owned hidden instructions
and tool schemas cannot be counted exactly by Loom: their reserve and tokenizer
margin are explicit estimates, not provider attestations. Never silently truncate.
"""

from dataclasses import asdict, dataclass
import asyncio
import json
import math
from pathlib import Path

import httpx

from config import config
import model_context
import model_registry


class ReplayBudgetExceeded(ValueError):
    pass


@dataclass(frozen=True)
class PayloadBudget:
    context_tokens: int
    context_source: str
    prompt_tokens: int
    instruction_tokens: int
    tool_reserve: int
    attachment_reserve: int
    output_reserve: int
    safety_margin: int

    @property
    def total_tokens(self):
        return sum((self.prompt_tokens, self.instruction_tokens, self.tool_reserve,
                    self.attachment_reserve, self.output_reserve, self.safety_margin))

    def as_dict(self):
        return {**asdict(self), "total_tokens": self.total_tokens, "estimated": True}


def estimate_tokens(text) -> int:
    # UTF-8 bytes account for non-ASCII/code better than len(text)//3 alone.
    return math.ceil(len(str(text or "").encode("utf-8")) / 3)


def _positive(value):
    try:
        result = int(value)
        return result if result > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def local_runtime_capacity(props: dict) -> int | None:
    """llama-server's per-slot n_ctx, never training context or total KV size."""
    params = props.get("default_generation_settings") or {}
    if not isinstance(params, dict):
        params = {}
    # Recent llama-server exposes n_ctx per slot in generation defaults.
    return _positive(params.get("n_ctx")) or _positive(props.get("n_ctx_per_seq"))


async def _local_capacity(model):
    # Query only on a fresh launch. No global 'last model' cache across sidecar
    # reloads, and no probes/downloads on a normal resumed turn.
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(0.8, connect=0.25)) as client:
            response = await client.get(config.llama_host_url().rstrip("/") + "/props")
            response.raise_for_status()
            capacity = local_runtime_capacity(response.json())
            if capacity:
                return capacity, "llama-server /props per-slot context"
    except (httpx.HTTPError, OSError, ValueError, TypeError, AttributeError):
        pass
    try:
        catalog = json.loads(Path(__file__).with_name("models_config.json").read_text(encoding="utf-8"))
        target = str(model or config.llama_model or "").casefold()
        settings = next((value for key, value in catalog.items() if key.casefold() == target), {})
        capacity = _positive(settings.get("ctx_size"))
        if capacity:
            return capacity, "configured llama model context (runtime unavailable)"
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return _positive(config.max_context_tokens) or 32768, "configured local fallback (runtime unavailable)"


async def context_capacity(model, harness, *, backend=None, runtime_context=None):
    if _positive(runtime_context):
        return int(runtime_context), "harness runtime context"
    normalized = model_context._context_model_id(str(model or ""))
    if normalized.endswith("@llama-server"):
        normalized = normalized.removesuffix("@llama-server")
        backend = "llama"
    if backend == "llama" or normalized.startswith("llama:"):
        return await _local_capacity(normalized.removeprefix("llama:"))
    if backend == "dream" or normalized.startswith(("dream:", "custom:diffusion")):
        return _positive(config.dream_context_size) or 32768, "configured Dream context"
    item = model_registry.REGISTRY.model(normalized)
    if item and (backend is None or item["backend"] == backend):
        source = "OpenRouter model metadata" if item["backend"] == "openrouter" else "catalog context (runtime unavailable)"
        return int(item["context_length"]), source
    if backend == "openrouter" or model_context.is_openrouter(normalized):
        return 200_000, "conservative provider fallback (runtime unavailable)"
    if model_context.is_1m_anthropic(normalized):
        return 1_000_000, "catalog context (runtime unavailable)"
    if harness == "codex":
        return 272_000, "conservative Codex fallback (runtime unavailable)"
    if harness == "gemini":
        return 1_000_000, "catalog Gemini context (runtime unavailable)"
    if backend == "llama" or (harness in {"local", "hermes", "goose"} and model_context.is_local_llama(normalized)):
        return await _local_capacity(normalized.removeprefix("custom:"))
    return 200_000, "conservative provider fallback (runtime unavailable)"


async def assess_payload(prompt, *, model, harness, instructions="", image_count=0,
                         backend=None, runtime_context=None, output_reserve=8192, tool_reserve=8192):
    capacity, source = await context_capacity(model, harness, backend=backend, runtime_context=runtime_context)
    return PayloadBudget(
        capacity, source, estimate_tokens(prompt), estimate_tokens(instructions),
        tool_reserve, max(0, int(image_count)) * 4096, output_reserve,
        math.ceil(capacity * 0.10),
    )


async def check_fresh_payload(prompt, *, model, harness, resume_session_id=None, **kwargs):
    if resume_session_id:
        return None  # Native context/compaction belongs to the harness.
    budget = await assess_payload(prompt, model=model, harness=harness, **kwargs)
    capacity, source = budget.context_tokens, budget.context_source
    task = asyncio.current_task()
    if task is not None:
        task._replay_budget = budget.as_dict()
    if budget.total_tokens > capacity:
        raise ReplayBudgetExceeded(
            f"Fresh {harness} payload needs approximately {budget.total_tokens:,} tokens "
            f"including instructions, tools, attachments, output and safety reserves; "
            f"destination allows {capacity:,} ({source}). Compact the source branch or "
            "select a larger-context destination. No model prompt was sent; history was not truncated."
        )
    return budget.as_dict()
