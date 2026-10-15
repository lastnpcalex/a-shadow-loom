"""Token counting, context window management, and incremental summarization.

Summarization strategy:
- All messages stay verbatim until total context exceeds max_context_tokens.
- Once over budget, keep the last `verbatim_window` messages verbatim.
- Everything older is covered by a rolling summary, built incrementally:
  each batch of messages is summarized independently by Gemma (fits its 4K context)
  and appended to the running summary, tagged with turn numbers.
  Gemma never re-reads the existing summary — just appends.
"""

from config import config
import database as db
import logging

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (rough: 1 token per ~3 chars)."""
    return len(text) // 3


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate total tokens for a list of messages."""
    total = 0
    for msg in messages:
        total += estimate_tokens(msg.get("content", ""))
        total += 4  # role/formatting overhead
    return total


async def get_context_for_generation(
    conv_id: int, character: dict = None, leaf_id: int | None = None
) -> dict:
    """Build the context window for generation.

    Returns:
        {
            "summary": str or None,
            "verbatim_messages": list[dict],
            "total_tokens": int,
            "was_compactified": bool,
        }
    """
    branch = await db.get_branch_to_root(leaf_id) if leaf_id is not None else await db.get_active_branch(conv_id)
    if not branch:
        return {
            "summary": None,
            "verbatim_messages": [],
            "total_tokens": 0,
            "was_compactified": False,
        }

    # Estimate system prompt overhead
    system_overhead = 2000
    if character:
        system_overhead += estimate_tokens(character.get("personality", ""))
        system_overhead += estimate_tokens(character.get("scenario", ""))
        for ex in character.get("example_messages", []):
            system_overhead += estimate_tokens(ex.get("content", ""))

    available_budget = config.max_context_tokens - system_overhead
    total_branch_tokens = sum(msg.get("token_estimate", 0) for msg in branch)

    # If everything fits, no compactification needed
    if total_branch_tokens <= available_budget:
        return {
            "summary": None,
            "verbatim_messages": branch,
            "total_tokens": total_branch_tokens + system_overhead,
            "was_compactified": False,
        }

    # Need compactification: keep last N messages verbatim, use rolling summary for the rest
    verbatim_window = config.verbatim_window
    verbatim_msgs = branch[-verbatim_window:] if len(branch) > verbatim_window else branch
    verbatim_tokens = sum(msg.get("token_estimate", 0) for msg in verbatim_msgs)

    # Get the existing rolling summary
    branch_ids = [m["id"] for m in branch]
    existing_summary = await db.get_summary(conv_id, branch_ids[:5])
    summary_text = existing_summary["content"] if existing_summary else None

    summary_tokens = estimate_tokens(summary_text) if summary_text else 0

    summarized_count = len(branch) - len(verbatim_msgs)

    return {
        "summary": summary_text,
        "verbatim_messages": verbatim_msgs,
        "total_tokens": system_overhead + summary_tokens + verbatim_tokens,
        "was_compactified": True,
        "total_messages": len(branch),
        "verbatim_count": len(verbatim_msgs),
        "summarized_count": summarized_count,
    }


async def update_rolling_summary(conv_id: int):
    """No-op. Legacy local Gemma background summarizer is retired."""
    return
