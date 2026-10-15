"""Canonical, provider-independent permission policy model.

This module does not execute commands. The live Loom permission endpoint uses
the small decision model mirrored by Lean under ``formal/permission_policy``.
Normalization and filesystem containment are tested separately from the proof.

The key separation is structural:

* policy decisions are ``auto_allow``, ``ask``, ``ask_with_warning``, or
  ``invalid_request``;
* only a later human resolution may become ``user_deny``;
* harness identity is audit metadata and never an input to ``decide``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping


class PermissionProfile(str, Enum):
    STRICT = "strict"
    BALANCED = "balanced"
    TRUSTED = "trusted"
    TRUST_REVIEW = "trust_review"


class ActionEffect(str, Enum):
    READ = "read"
    WORKSPACE_WRITE = "workspace_write"
    EXECUTE = "execute"
    NETWORK = "network"
    EXTERNAL_MUTATION = "external_mutation"
    SENSITIVE_READ = "sensitive_read"
    LIFECYCLE = "lifecycle"
    UNKNOWN = "unknown"


class ActionBoundary(str, Enum):
    WORKSPACE = "workspace"
    OUTSIDE_WORKSPACE = "outside_workspace"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


class PolicyDecision(str, Enum):
    AUTO_ALLOW = "auto_allow"
    ASK = "ask"
    ASK_WITH_WARNING = "ask_with_warning"
    INVALID_REQUEST = "invalid_request"


class ResolutionSource(str, Enum):
    USER = "user"
    LOOM_POLICY = "loom_policy"
    LOOM_LIFECYCLE = "loom_lifecycle"
    LOOM_TIMEOUT = "loom_timeout"
    BRIDGE_ERROR = "bridge_error"


class PermissionResolution(str, Enum):
    ALLOWED = "allowed"
    USER_DENIED = "user_denied"
    POLICY_BLOCKED = "policy_blocked"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class NormalizedPermissionAction:
    effect: ActionEffect
    boundary: ActionBoundary
    valid: bool = True
    warned: bool = False
    rule_matched: bool = False
    destructive: bool = False
    destructive_reason: str = ""
    command_family: str = ""
    workspace_root: str = ""
    harness: str = ""
    tool_name: str = ""

    def policy_dict(self) -> dict:
        """Return only fields that are inputs to the formal decision model."""
        return {
            "effect": self.effect.value,
            "boundary": self.boundary.value,
            "valid": self.valid,
            "warned": self.warned,
            "rule_matched": self.rule_matched,
            "destructive": self.destructive,
        }

    def audit_dict(self) -> dict:
        # Deliberately exclude workspace_root and command_family.  The shadow
        # audit needs normalized provenance, not raw paths or command content.
        return {
            **self.policy_dict(),
            "harness": self.harness if self.harness in {
                "codex", "codex_app_server", "claude", "claude_code", "antigravity", "gemini",
                "goose", "goose_acp", "hermes", "hermes_acp", "loom_workspace",
                "loom_permission_hook", "loom-workspace", "dream", "local", "umans", "openrouter",
            } else "unknown",
            # ACP titles can contain an entire command or private path.
            "tool_name": {ActionEffect.EXECUTE: "Bash", ActionEffect.READ: "Read",
                          ActionEffect.WORKSPACE_WRITE: "Write", ActionEffect.NETWORK: "Network",
                          ActionEffect.SENSITIVE_READ: "SensitiveRead"}.get(self.effect, "Unknown"),
        }


@dataclass(frozen=True)
class RuleMatchContext:
    same_workspace: bool
    branch_descendant: bool
    shape_matches: bool


def rule_applies(context: RuleMatchContext) -> bool:
    """A remembered rule is valid only inside its workspace and branch."""
    return (
        context.same_workspace
        and context.branch_descendant
        and context.shape_matches
    )


def _ask(warned: bool) -> PolicyDecision:
    return PolicyDecision.ASK_WITH_WARNING if warned else PolicyDecision.ASK


def decide(
    profile: PermissionProfile,
    action: NormalizedPermissionAction,
) -> PolicyDecision:
    """Return the canonical policy decision.

    There is intentionally no policy-deny result. In the legacy profiles unknown inputs ask the human;
    warnings annotate an existing ask but never turn an automatic allow into a
    rejection. The opt-in trust_review profile asks only for recognized
    destructive operations, ahead of remembered rules. Legacy profiles ignore
    that separate signal and keep their existing decisions.
    """
    if not action.valid:
        return PolicyDecision.INVALID_REQUEST
    if profile is PermissionProfile.TRUST_REVIEW:
        return PolicyDecision.ASK_WITH_WARNING if action.destructive else PolicyDecision.AUTO_ALLOW
    if action.rule_matched:
        return PolicyDecision.AUTO_ALLOW
    if (
        action.effect is ActionEffect.UNKNOWN
        or action.boundary is ActionBoundary.UNKNOWN
    ):
        return _ask(action.warned)
    if action.boundary is not ActionBoundary.WORKSPACE:
        return _ask(action.warned)
    if action.effect in {
        ActionEffect.NETWORK,
        ActionEffect.EXTERNAL_MUTATION,
        ActionEffect.SENSITIVE_READ,
        ActionEffect.LIFECYCLE,
    }:
        return _ask(action.warned)
    if action.effect is ActionEffect.READ:
        return PolicyDecision.AUTO_ALLOW
    if profile is PermissionProfile.STRICT:
        return _ask(action.warned)
    if action.effect is ActionEffect.WORKSPACE_WRITE:
        return PolicyDecision.AUTO_ALLOW
    if profile is PermissionProfile.TRUSTED and action.effect is ActionEffect.EXECUTE:
        return PolicyDecision.AUTO_ALLOW
    return _ask(action.warned)


def resolve_permission(
    *,
    allowed: bool,
    source: ResolutionSource,
) -> PermissionResolution:
    """Map a resolved request without impersonating a human decision."""
    if allowed:
        return PermissionResolution.ALLOWED
    if source is ResolutionSource.USER:
        return PermissionResolution.USER_DENIED
    if source is ResolutionSource.LOOM_POLICY:
        return PermissionResolution.POLICY_BLOCKED
    return PermissionResolution.CANCELLED


def _iter_mappings(value, *, depth: int = 0) -> Iterable[Mapping]:
    """Visit protocol input envelopes, never results, content or descriptions."""
    if depth > 8:
        return
    if isinstance(value, Mapping):
        yield value
        for key in ("toolCall", "tool_call", "params", "arguments", "args",
                    "input", "tool_input", "rawInput", "parameters"):
            item = value.get(key)
            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except (ValueError, TypeError):
                    continue
            yield from _iter_mappings(item, depth=depth + 1)


def _iter_strings(value, *, depth: int = 0) -> Iterable[str]:
    if depth > 8:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _iter_strings(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item, depth=depth + 1)


def _tool_names(tool_name: str, tool_input) -> set[str]:
    result = {str(tool_name or "").strip().lower()}
    # Only the tool envelope identifies the operation, not arbitrary arguments.
    envelope = tool_input if isinstance(tool_input, Mapping) else {}
    call = envelope.get("toolCall") or envelope.get("tool_call") or {}
    if isinstance(call, Mapping):
        for key in ("name", "toolName", "tool_name", "kind"):
            value = call.get(key)
            if isinstance(value, str) and value.strip():
                result.add(value.strip().lower())
    return result


def _first_value(tool_input, keys: tuple[str, ...]):
    for item in _iter_mappings(tool_input):
        for key in keys:
            value = item.get(key)
            if value not in (None, "", []):
                return value
    return None


def _effect_for_request(
    tool_name: str,
    tool_input,
    approval_method: str,
) -> ActionEffect:
    names = _tool_names(tool_name, tool_input)
    method = str(approval_method or "").lower()
    if _first_value(tool_input, ("networkApprovalContext",)) is not None:
        return ActionEffect.NETWORK
    if _first_value(tool_input, ("additionalPermissions", "additional_permissions")):
        # Extra sandbox grants are not equivalent to executing in a cwd.
        return ActionEffect.UNKNOWN
    if "commandexecution" in method or names & {
        "bash", "powershell", "shell", "terminal", "execute", "run_command", "exec_command",
        "task", "agent", "codebase_investigator", "explore",
    }:
        return ActionEffect.EXECUTE
    if "filechange" in method:
        return ActionEffect.WORKSPACE_WRITE
    if "permissions/requestapproval" in method:
        requested = _first_value(
            tool_input,
            ("permissions", "requestedPermissions", "requested_permissions"),
        )
        text = json.dumps(requested, default=str).lower()
        return ActionEffect.NETWORK if "network" in text else ActionEffect.UNKNOWN
    if names & {"sensitiveread", "sensitive_read"}:
        return ActionEffect.SENSITIVE_READ
    if names & {"external_mutation", "externalmutation", "publish", "deploy"}:
        return ActionEffect.EXTERNAL_MUTATION
    if names & {"websearch", "webfetch", "web_search", "web_fetch", "browser", "fetch", "google_web_search"}:
        return ActionEffect.NETWORK
    if names & {"write", "edit", "apply_patch", "patch", "create_file", "mkdir",
                "write_file", "edit_file", "notebookedit", "replace",
                "todowrite", "write_todos", "taskcreate", "taskupdate"}:
        return ActionEffect.WORKSPACE_WRITE
    if names & {"read", "glob", "grep", "search", "list", "tree", "stat",
                "read_file", "read_text_file", "read_many_files", "list_dir", "list_directory",
                "grep_search", "find_by_name", "taskget", "tasklist", "taskoutput", "toolsearch", "cronlist",
                "skill", "cli_help", "get_internal_docs"}:
        return ActionEffect.READ
    return ActionEffect.UNKNOWN


def _path_within(path_value: str, workspace_root: str) -> bool | None:
    if not path_value or not workspace_root:
        return None
    try:
        path = Path(path_value).expanduser()
        root = Path(workspace_root).expanduser().resolve(strict=False)
        if not path.is_absolute():
            path = root / path
        path = path.resolve(strict=False)
        return os.path.normcase(os.path.commonpath((str(path), str(root)))) == os.path.normcase(str(root))
    except (OSError, ValueError):
        return None


def _boundary_for_request(
    effect: ActionEffect,
    tool_input,
    workspace_root: str,
) -> ActionBoundary:
    if effect in {ActionEffect.NETWORK, ActionEffect.EXTERNAL_MUTATION}:
        return ActionBoundary.EXTERNAL
    paths = request_paths(tool_input)
    if any(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value) for value in paths):
        return ActionBoundary.EXTERNAL
    if effect is ActionEffect.WORKSPACE_WRITE and not paths:
        return ActionBoundary.UNKNOWN
    cwd = _first_value(tool_input, ("cwd", "workdir", "working_directory"))
    base = str(cwd or workspace_root)
    if workspace_root and not Path(base).is_absolute():
        base = str(Path(workspace_root) / base)
    if effect in {ActionEffect.READ, ActionEffect.EXECUTE} and not paths:
        paths = [base] if base else []
    if cwd:
        paths.append(base)
    contained = []
    for value in paths:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path(base) / path
        contained.append(_path_within(str(path), workspace_root))
    if False in contained:
        return ActionBoundary.OUTSIDE_WORKSPACE
    if contained and all(item is True for item in contained):
        return ActionBoundary.WORKSPACE
    return ActionBoundary.UNKNOWN


def request_paths(tool_input) -> list[str]:
    paths = []
    for item in _iter_mappings(tool_input):
        for key in ("file_path", "filePath", "path", "paths", "files", "notebook_path",
                    "destination", "destination_path", "dst", "source", "src",
                    "AbsolutePath", "TargetFile", "DirectoryPath", "SearchDirectory"):
            value = item.get(key)
            if isinstance(value, str) and value:
                paths.append(value)
            elif isinstance(value, list):
                paths.extend(v for v in value if isinstance(v, str) and v)
        # Codex structured file-change approvals and MCP apply_patch inputs.
        changes = item.get("changes")
        if isinstance(changes, Mapping):
            paths.extend(str(p) for p in changes)
            for change in changes.values():
                if isinstance(change, Mapping) and change.get("move_path"):
                    paths.append(str(change["move_path"]))
        for key in ("patch", "command", "input"):
            patch = item.get(key)
            if isinstance(patch, str) and patch.startswith("*** Begin Patch"):
                paths.extend(re.findall(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", patch, re.M))
    return list(dict.fromkeys(paths))


def _command_family(tool_input) -> str:
    # Provider-proposed policy amendments are suggestions, not authority.
    command = _first_value(tool_input, ("command", "cmd", "shell_command", "execCommand"))
    if isinstance(command, str):
        return command.strip()
    return ""


def once_option(options: list, allow: bool) -> str | None:
    """ACP approvals must not install a native session-wide grant."""
    wanted = "allow_once" if allow else "reject_once"
    for option in options or []:
        oid = str(option.get("optionId") or option.get("id") or option.get("option_id") or "")
        kind = str(option.get("kind") or "").lower()
        if kind == wanted or (not kind and oid.lower() == wanted):
            return oid or None
    return None


def normalize_permission_request(
    *,
    tool_name: str,
    tool_input,
    approval_method: str = "",
    harness: str = "",
    workspace_root: str = "",
    warned: bool = False,
    rule_context: RuleMatchContext | None = None,
) -> NormalizedPermissionAction:
    """Normalize payloads and explicit operations, never infer arbitrary program intent.

    A provider cannot claim a remembered-rule match. Loom derives that bit
    only from its own workspace, branch-ancestry, and action-shape checks.
    """
    effect = _effect_for_request(tool_name, tool_input, approval_method)
    if tool_name in {"LoomContextRead", "mcp__loom-agent-control__retrieve_context",
                     "mcp__loom-agent-control__search_conversations",
                     "mcp__loom-agent-control__read_conversation_context",
                     "mcp__loom-agent-control__get_conversation_details",
                     "loom-agent-control.search_conversations",
                     "loom-agent-control.read_conversation_context",
                     "loom-agent-control.get_conversation_details"}:
        # Scope ownership and revision are enforced by the retrieval endpoint,
        # independently of permission profiles. This grants no arbitrary DB read.
        effect = ActionEffect.READ
    destructive_reason = _known_destructive_reason(tool_name, tool_input, approval_method, effect)
    if effect is ActionEffect.EXECUTE:
        from permission_commands import literal_read_paths
        command = _first_value(tool_input, ("command",))
        paths = literal_read_paths(command, _first_value(tool_input, ("command_argv",))) if isinstance(command, str) else None
        if paths is not None:
            effect = ActionEffect.READ
            # Include structural paths already present too; no supplied path
            # or cwd may be hidden by recognizing the command as a file read.
            tool_input = {"paths": request_paths(tool_input) + paths,
                          "cwd": _first_value(tool_input, ("cwd", "workdir", "working_directory"))}
    boundary = _boundary_for_request(effect, tool_input, workspace_root)
    if (effect is ActionEffect.WORKSPACE_WRITE and workspace_root
            and _tool_names(tool_name, tool_input) & {"todowrite", "write_todos", "taskcreate", "taskupdate"}
            and not request_paths(tool_input)):
        # Native task/todo bookkeeping is session state, not an arbitrary
        # file write whose destination may be inferred from text content.
        boundary = ActionBoundary.WORKSPACE
    return NormalizedPermissionAction(
        effect=effect,
        boundary=boundary,
        valid=bool(str(tool_name or "").strip() or str(approval_method or "").strip()),
        warned=bool(warned),
        destructive=bool(destructive_reason),
        destructive_reason=destructive_reason,
        rule_matched=rule_applies(rule_context) if rule_context is not None else False,
        command_family=_command_family(tool_input),
        workspace_root=str(workspace_root or ""),
        harness=str(harness or ""),
        tool_name=str(tool_name or ""),
    )


def _known_destructive_reason(tool_name, tool_input, approval_method, effect):
    """Evidence comes from operation/arguments, never a provider risk label."""
    from permission_risk import destructive_command_reason
    names = _tool_names(tool_name, tool_input)
    if names & {"delete_file", "remove_file", "delete_directory", "remove_directory"}:
        return "Explicit file/directory deletion tool requires one-time review."
    if effect is ActionEffect.EXECUTE or "commandexecution" in str(approval_method).lower() or names & {
        "bash", "powershell", "shell", "terminal", "execute", "run_command", "exec_command",
    }:
        command = _first_value(tool_input, ("command", "cmd", "shell_command", "execCommand"))
        argv = command if isinstance(command, list) else _first_value(tool_input, ("command_argv",))
        reason = destructive_command_reason(command, argv)
        if argv is not None and isinstance(command, str):
            from permission_commands import display_argv
            # A native argv's display is not another shell program. But a
            # contradictory supplied argv must not hide a destructive command.
            if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv) or display_argv(argv) != command:
                reason = reason or destructive_command_reason(command)
        return reason
    if "filechange" in str(approval_method).lower():
        changes = _first_value(tool_input, ("changes",))
        entries = changes.values() if isinstance(changes, Mapping) else changes if isinstance(changes, list) else []
        for change in entries:
            if isinstance(change, Mapping):
                kind = change.get("type") or change.get("kind")
                if isinstance(kind, Mapping):
                    kind = kind.get("type")
                if kind in ("delete", "remove"):
                    return "Native file-change request deletes a file; one-time review required."
    if names & {"apply_patch", "patch"}:
        patch = tool_input if isinstance(tool_input, str) else _first_value(tool_input, ("patch", "input", "patch_text", "command"))
        if isinstance(patch, str):
            patch = patch.replace("\r\n", "\n")
        if isinstance(patch, str) and patch.startswith("*** Begin Patch\n") and "\n*** Delete File: " in patch:
            return "Patch deletes a file; one-time review required."
    return ""


def profile_from_value(value: str | None) -> PermissionProfile:
    try:
        return PermissionProfile(str(value or "balanced").strip().lower())
    except ValueError:
        return PermissionProfile.BALANCED


def build_shadow_record(
    *,
    profile: PermissionProfile,
    tool_name: str,
    tool_input,
    approval_method: str = "",
    harness: str = "",
    workspace_root: str = "",
    warned: bool = False,
    legacy_outcome: str,
) -> dict:
    """Compare one legacy outcome with the canonical policy, without enforcing it."""
    action = normalize_permission_request(
        tool_name=tool_name,
        tool_input=tool_input,
        approval_method=approval_method,
        harness=harness,
        workspace_root=workspace_root,
        warned=warned,
    )
    decision = decide(profile, action)
    legacy_allows = legacy_outcome in {"goose_auto_allow", "scoped_auto_allow"}
    legacy_asks = legacy_outcome == "ask"
    agrees = (
        (legacy_allows and decision is PolicyDecision.AUTO_ALLOW)
        or (
            legacy_asks
            and decision in {PolicyDecision.ASK, PolicyDecision.ASK_WITH_WARNING}
        )
    )
    return {
        "schema_version": 1,
        "mode": "shadow",
        "profile": profile.value,
        "decision": decision.value,
        "legacy_outcome": legacy_outcome,
        "disagrees": not agrees,
        "action": action.audit_dict(),
    }
