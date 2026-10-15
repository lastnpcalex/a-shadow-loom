"""Shared Loom agent contract injection helpers."""

from pathlib import Path

_LOOM_AGENT_PATH = Path(__file__).parent / "loom_agent.md"


def load_loom_agent_prompt() -> str:
    """Return the shared role-neutral Loom contract, or an empty string."""
    try:
        return _LOOM_AGENT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def merge_system_prompts(*parts: str | None) -> str:
    """Join optional system-prompt fragments with stable spacing."""
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def loom_agent_contract_block(provider: str) -> str:
    """Render the shared contract without turning it into a user message."""
    contract = load_loom_agent_prompt()
    if not contract:
        return ""
    return (
        f'<loom_agent_contract provider="{provider}">\n'
        f"{contract}\n"
        f"</loom_agent_contract>"
    )


def prepend_loom_agent_context(prompt: str, provider: str) -> str:
    """Inject the Loom contract on a prompt-only harness's first native turn."""
    contract_block = loom_agent_contract_block(provider)
    if not contract_block:
        return prompt
    return (
        f"{contract_block}\n\n"
        f"<user_task>\n"
        f"{prompt}\n"
        f"</user_task>"
    )
