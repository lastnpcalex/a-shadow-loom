"""Provider-independent session continuity rules for Loom agent harnesses.

The Loom message tree is the source of truth. Provider-native sessions are an
optimization that may be resumed only when the nearest prior assistant turn is
provably compatible with the target harness. Searching past an intervening
assistant turn can silently omit that turn when an older session is resumed.
"""

from dataclasses import dataclass
from html import escape
import json
from typing import Callable, Iterable, Mapping


@dataclass(frozen=True)
class ResumeDecision:
    """Why Loom will resume a native provider session or rebuild from history."""

    session_id: str | None
    reason: str
    message_id: int | None = None

    @property
    def can_resume(self) -> bool:
        return bool(self.session_id)


_HARNESS_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
    "gemini": "Antigravity",
    "goose": "Goose ACP",
    "dream": "Dream",
    "hermes": "Hermes ACP",
}


def add_continuity_notice(
    prompt: str,
    decision: ResumeDecision,
    branch: Iterable[Mapping],
    target_mode: str,
    *,
    target_model: str | None = None,
) -> str:
    """Prepend a concise provider-boundary notice to the model payload.

    This is intentionally much smaller than the Loom agent contract. It tells
    a resumed model where its memory came from, or tells a fresh model why Loom
    included canonical branch history. The notice is prompt-only and never
    becomes a visible conversation-tree message.
    """
    messages = list(branch)
    boundary = next(
        (m for m in messages if m.get("id") == decision.message_id),
        None,
    ) or {}
    target_harness = _HARNESS_LABELS.get(target_mode, target_mode or "agent")

    if decision.can_resume:
        notice = (
            f"Loom forked/resumed the prior {target_harness} native session. "
            "Its prior context is retained; this payload adds only the current user turn."
        )
        if decision.reason == "verified_completed_recovery":
            notice += (
                " Loom previously marked the source turn failed, but its native terminal record "
                "has now been verified as completed. Retain its finished work; do not repeat actions."
            )
        elif decision.reason == "recoverable_failure":
            notice += (
                " The prior turn failed after a confirmed native terminal event. "
                "Inspect retained tool outcomes before repeating any action; this is a new user-directed turn."
            )
        return f'<loom_continuity kind="native_resume">{notice}</loom_continuity>\n\n{prompt}'

    if decision.reason == "provider_boundary":
        source_mode = str(boundary.get("cc_session_mode") or "previous harness")
        source_harness = _HARNESS_LABELS.get(source_mode, source_mode)
        notice = (
            f"Agent harness changed from {source_harness} to {target_harness}. "
            "Loom rebuilt this turn from the canonical branch history included below."
        )
        return f'<loom_handoff kind="harness">{notice}</loom_handoff>\n\n{prompt}'

    if decision.reason == "model_boundary":
        source_model = str(boundary.get("cc_model_used") or "the previous model")
        target = str(target_model or "the selected model")
        notice = (
            f"Model changed from {source_model} to {target} within {target_harness}. "
            "Loom rebuilt this turn from the canonical branch history included below."
        )
        return f'<loom_handoff kind="model">{notice}</loom_handoff>\n\n{prompt}'

    if decision.reason == "manual_checkpoint":
        return ('<loom_continuity kind="checkpoint">The user requested a shared branch checkpoint. '
                'This is a fresh native session using that checkpoint and subsequent records; '
                'original evidence is retrievable. Do not repeat completed actions.</loom_continuity>\n\n' + prompt)

    if decision.reason.startswith("native_resume_"):
        notice = (
            f"The prior {target_harness} native session could not continue "
            f"({decision.reason}). Loom rebuilt from canonical branch history below."
        )
        return f'<loom_continuity kind="history_rebuild">{notice}</loom_continuity>\n\n{prompt}'

    return prompt


def add_permission_feedback_notice(
    prompt: str,
    resolutions: Iterable[Mapping],
) -> str:
    """Carry prior-turn permission outcomes into an ACP continuation.

    ACP permission replies contain an option id but no portable free-text
    field. Loom therefore persists the outcome with the assistant draft and
    prepends it to the next branch-local continuation. The explicit source
    prevents policy/lifecycle failures from being attributed to the human.
    """
    entries = []
    for resolution in resolutions:
        feedback = str(resolution.get("feedback") or "").strip()
        if not feedback:
            continue
        tool_name = str(resolution.get("tool_name") or "tool")
        source = str(resolution.get("decision_source") or "user")
        if resolution.get("allowed"):
            decision = "allowed"
        elif source == "loom_policy":
            decision = "blocked"
        elif source != "user":
            decision = "cancelled"
        else:
            decision = "denied"
        entries.append(
            f'<decision tool="{escape(tool_name, quote=True)}" result="{decision}" '
            f'source="{escape(source, quote=True)}">'
            f"{escape(feedback)}</decision>"
        )
    if not entries:
        return prompt
    notice = (
        "<loom_permission_feedback>\n"
        "These permission outcomes were attached to the immediately prior "
        "assistant turn. Only source=\"user\" is a human decision; respect the "
        "reported source when continuing:\n"
        + "\n".join(entries)
        + "\n</loom_permission_feedback>"
    )
    return f"{notice}\n\n{prompt}"


def _has_content_blocks(message: Mapping) -> bool:
    value = message.get("content_blocks")
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and value.strip() not in {"[]", "{}", "null"}
    return bool(value)


def resume_rejected(error) -> bool:
    """Only explicit native-session rejection warrants an empty replay fallback."""
    text = str(error or "").lower()
    return any(term in text for term in (
        "session not found", "thread not found", "no such session", "no such thread",
        "invalid session", "invalid thread", "session does not exist",
        "thread does not exist", "session has been deleted", "stale session",
        "no rollout found", "rollout not found",
        "chunk exceed the limit", "separator is not found", "buffer limit",
        "codex app-server launch failed",
    ))


def context_exhausted(error) -> bool:
    """Normalize explicit native/admission context failures, not arbitrary 400s.

    Recognition is NOT permission to retry: callers must additionally establish
    a recorded tool boundary and verified teardown, or require user-directed
    reconciliation. Recognition alone never authorizes replay of an action.
    """
    text = str(error or "").casefold()
    return any(term in text for term in (
        "contextwindowexceeded", "context_length_exceeded", "context window exceeded",
        "exceeds the available context size", "maximum context length",
        "loom local context admission failed", "local request still needs",
    ))


def recovery_evidence(session_id, mode, error, *, terminal_confirmed=False, error_code=None) -> dict:
    """Evidence for a later user-directed resume, never an automatic tool retry."""
    text = str(error or "").lower()
    code = str(next(iter(error_code), "") if isinstance(error_code, dict) else (error_code or "")).lower()
    invalid = any(term in text + code for term in (
        "contextwindowexceeded", "context size", "context length", "context_length",
        "context window", "badrequest", "bad request", "not found", "no such session",
        "invalid session", "model mismatch", "corrupt", "unauthorized",
    ))
    transient = code in {
        "responsestreamdisconnected", "responsestreamconnectionfailed",
        "responsetoomanyfailedattempts", "usagelimitexceeded", "internalservererror",
    } or any(term in text for term in (
        "rate limit", "session limit", "usage limit", "overloaded", "connection reset", "econnreset",
        "service unavailable", "stream disconnected", "upstream timeout", "upstream idle timeout",
    ))
    return {
        "version": 1, "session_id": session_id, "session_mode": mode,
        "terminal_confirmed": bool(terminal_confirmed),
        "can_resume": bool(session_id and terminal_confirmed and transient and not invalid),
        "reason": "transient_provider_failure" if transient and not invalid else "unverified_or_invalid_session",
    }


def _has_recovery_evidence(message: Mapping, mode: str) -> bool:
    attestation = message.get("model_attestation") or {}
    try:
        if isinstance(attestation, str):
            attestation = json.loads(attestation)
        evidence = attestation.get("session_recovery") or {}
        return bool(
            attestation.get("status") != "mismatch"
            and evidence.get("version") == 1
            and evidence.get("can_resume") is True
            and evidence.get("terminal_confirmed") is True
            and evidence.get("session_id") == message.get("cc_session_id")
            and evidence.get("session_mode") == mode
        )
    except (TypeError, ValueError, AttributeError):
        return False


def partial_replay_text(message: Mapping) -> str:
    """Portable partial outcomes, not just the prose preceding a failed tool."""
    parts = [str(message.get("content") or "").strip()]
    blocks = message.get("content_blocks") or []
    try:
        if isinstance(blocks, str):
            blocks = json.loads(blocks)
    except (TypeError, ValueError):
        blocks = []
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        parts.append(f"[Prior tool: {block.get('name', 'unknown')}]")
        for key, label in (("input", "Input"), ("result", "Captured result")):
            value = block.get(key)
            text = value if isinstance(value, str) else json.dumps(value, default=str) if value is not None else ""
            if text:
                parts.append(f"{label}: {text[:2000]}" + (" [preview truncated]" if len(text) > 2000 else ""))
            elif key == "result":
                parts.append("No result was captured. Verify the action's outcome before repeating it.")
    return "\n".join(part for part in parts if part) or "[Partial turn with no captured output]"


def select_resume_session(
    branch: Iterable[Mapping],
    target_mode: str,
    *,
    target_model: str | None = None,
    models_match: Callable[[str, str], bool] | None = None,
) -> ResumeDecision:
    """Select a resumable session at the nearest assistant boundary.

    A provider switch must rebuild from Loom history. Consequently this stops
    at the first prior assistant turn even when it is foreign, unscoped,
    model-incompatible, errored, empty, or missing a native session id. It
    never searches farther back for an older compatible session.

    Legacy rows with no ``cc_session_mode`` deliberately rebuild once. Their
    native session namespace cannot be proven safe across harnesses; after the
    fresh turn, the newly saved message carries an explicit mode and normal
    same-provider resume behavior returns.
    """
    messages = list(branch)
    for message in reversed(messages):
        role = message.get("role")
        content = str(message.get("content") or "")

        if role == "system":
            if content.lstrip().startswith("[CC context compactified"):
                return ResumeDecision(None, "compact_boundary", message.get("id"))
            continue
        if role != "assistant":
            continue

        message_id = message.get("id")
        lifecycle = str(message.get("turn_lifecycle_state") or "").strip().lower()
        session_id = str(message.get("cc_session_id") or "").strip()
        # Cancel before output creates a topology placeholder so queued work
        # retains its parent. It is not a provider boundary and owns no native
        # context. Treat only the exact no-work placeholder as transparent;
        # interrupted or partially retained turns remain substantive.
        if (
            lifecycle in {"cancelled", "canceled"}
            and not session_id
            and not _has_content_blocks(message)
            and content.strip() in {
                "", "[Turn cancelled; partial response discarded]",
                "[Turn canceled; partial response discarded]",
            }
        ):
            continue
        recoverable = lifecycle in {"failed", "partial_failed"} and _has_recovery_evidence(message, target_mode)
        if lifecycle in {"failed", "partial_failed"} and not recoverable:
            # Keep the native session id for diagnosis, but do not implicitly
            # trust a provider session whose terminal state was ambiguous or
            # failed. A later turn rebuilds from Loom's canonical partial text
            # and tool transcript instead.
            return ResumeDecision(None, "assistant_failed", message_id)
        if content.startswith("[Error:") and not recoverable:
            return ResumeDecision(None, "assistant_error", message_id)
        if not content.strip() and not _has_content_blocks(message) and not recoverable:
            return ResumeDecision(None, "assistant_empty", message_id)

        if not session_id:
            return ResumeDecision(None, "assistant_without_session", message_id)

        session_mode = str(message.get("cc_session_mode") or "").strip()
        if not session_mode:
            return ResumeDecision(None, "legacy_unscoped_session", message_id)
        if session_mode != target_mode:
            return ResumeDecision(None, "provider_boundary", message_id)

        if target_model and (models_match is not None or recoverable):
            recorded_model = str(message.get("cc_model_used") or "").strip()
            if not recorded_model:
                return ResumeDecision(None, "model_unscoped_session", message_id)
            if models_match is not None:
                compatible = models_match(recorded_model, target_model)
            else:
                # Failure recovery additionally checks the unchanged launch
                # selector. Runtime aliases (sonnet -> pinned Claude ID) may
                # differ from it; attestation retains both independently.
                attestation = message.get("model_attestation") or {}
                if isinstance(attestation, str):
                    attestation = json.loads(attestation)  # validated above
                compatible = target_model in (recorded_model, attestation.get("requested_model"))
            if not compatible:
                return ResumeDecision(None, "model_boundary", message_id)

        return ResumeDecision(session_id, "recoverable_failure" if recoverable else "resume", message_id)

    return ResumeDecision(None, "no_assistant_session")
