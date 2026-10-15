"""Deprecated local Gemma summarizer.

Superseded by local_context_guard.py (which uses the active llama-server backend).
This module is retired and performs no background downloads, inference, or Hugging Face calls.
"""

from typing import Optional


async def summarize(text: str, max_tokens: int = 400, temperature: float = 0.3) -> str:
    """Retired summarizer: returns empty string without performing inference or network requests."""
    return ""


async def summarize_message(content: str, role: str = "user") -> str:
    """Retired message summarizer: returns empty string."""
    return ""


def is_loaded() -> bool:
    """Check if the model is currently loaded in memory (always False)."""
    return False


def is_loading() -> bool:
    """Check if the model is currently being loaded (always False)."""
    return False


async def preload():
    """No-op. Retired summarizer does not load any model."""
    pass


def unload():
    """No-op. Retired summarizer holds no resident model."""
    pass
