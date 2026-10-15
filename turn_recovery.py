"""Harness-neutral, bounded recovery at a recorded tool boundary.

A tool result is provider evidence, not proof of arbitrary external effects.
Missing/error outcomes never authorize automatic replay. Callers must retire
the old provider, persist evidence and use the shared context planner.
"""
from dataclasses import dataclass
import secrets

from provider_contract import context_exhausted


@dataclass(frozen=True)
class RecoveryDecision:
    allowed: bool
    reason: str


def may_launch(eligible, exited, stopped, used):
    """Lean-conformant launch boundary; not a proof of adapter/OS evidence."""
    return bool(eligible and exited and not stopped and not used)


def claim(decision, task, proc):
    used = bool(getattr(task, "_context_recovery_attempts", 0))
    exited = proc is None or getattr(proc, "returncode", None) is not None
    if not may_launch(decision.allowed, exited, bool(task.cancelling()), used):
        raise RuntimeError("Context recovery was stopped, exhausted its retry, or lacks provider exit evidence")
    task._context_recovery_attempts = 1
    return "recovery-" + secrets.token_hex(12) + ":"


def namespace_event(event, prefix):
    if prefix and event.get("type") in {"tool_start", "tool_input_delta", "tool_result"} and event.get("tool_id"):
        return {**event, "native_tool_id": event["tool_id"], "tool_id": prefix + event["tool_id"]}
    return event


async def events(stream):
    """Normalize raised context failures without inventing native completion.

    Cancellation and unrelated transport failures retain their existing paths.
    The launch boundary still requires independently verified process exit.
    """
    try:
        async for event in stream:
            yield event
    except Exception as exc:
        if not context_exhausted(exc):
            raise
        yield {"type": "error", "message": str(exc)}
        yield {"type": "result", "is_error": True, "error": str(exc), "terminal_confirmed": False}


def decide(error, blocks, *, attempts=0, enabled=True):
    if not enabled or not context_exhausted(error):
        return RecoveryDecision(False, "not_context_exhaustion")
    if attempts >= 1:
        return RecoveryDecision(False, "recovery_limit")
    for block in blocks:
        if not isinstance(block, dict):
            return RecoveryDecision(False, "unresolved_tool_outcome")
        if block.get("type") == "tool_use" and (
                not block.get("tool_id") or block.get("outcome_recorded") is not True or block.get("is_error")):
            return RecoveryDecision(False, "unresolved_tool_outcome")
    return RecoveryDecision(True, "context_exhausted_at_recorded_boundary")
