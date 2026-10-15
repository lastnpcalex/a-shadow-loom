"""Decode ACP approvals using protocol identity, never human-readable titles.

Goose 1.45 emits identity on the initial tool_call, but only an `other` kind
on request_permission. Keep that evidence per native run and join by call ID.
No global cache, filesystem effects, native grants, or title-based inference.
"""
from copy import deepcopy
import json


def _arguments(call):
    for key in ("rawInput", "raw_input", "arguments", "input"):
        value = call.get(key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                continue
        if isinstance(value, dict):
            return deepcopy(value)
    return None


def remember_tool_call(state, call):
    call_id = str(call.get("toolCallId") or call.get("tool_call_id") or call.get("id") or "")
    if call_id:
        state.setdefault("permission_tool_calls", {})[call_id] = deepcopy(call)


def tool_input_json(call):
    """Arguments are JSON, not content blocks (a `content` argument is data)."""
    arguments = _arguments(call)
    return json.dumps(arguments, ensure_ascii=False) if arguments is not None else ""


def permission_action(params, *, state=None, cwd="", harness="goose"):
    call = params.get("toolCall") or params.get("tool_call") or params.get("tool") or {}
    if not isinstance(call, dict):
        call = {}
    call_id = str(call.get("toolCallId") or call.get("tool_call_id") or call.get("id") or "")
    initial = (state or {}).get("permission_tool_calls", {}).get(call_id, {})
    args = _arguments(call)
    initial_args = _arguments(initial)
    # A changed payload must not inherit the authorization identity of a
    # different operation, even if the upstream reused a call ID.
    conflict = bool(initial and args is not None and initial_args is not None and args != initial_args)
    evidence = call if not initial else initial
    if args is None and initial:
        args = initial_args
    args = args if args is not None else {}
    name = "Unknown"
    identity_source = "unrecognized_acp"
    if harness != "goose" and not conflict:
        native_name = call.get("name") or call.get("toolName") or evidence.get("name")
        name = {"read_file": "Read", "write_file": "Write", "edit_file": "Edit",
                "terminal": "Bash", "shell": "Bash", "web_search": "WebSearch",
                "web_fetch": "WebFetch"}.get(native_name, "Unknown") if isinstance(native_name, str) else "Unknown"
        if name != "Unknown":
            identity_source = "acp_tool_name"
    if harness == "goose":
        meta = evidence.get("_meta") or {}
        goose = meta.get("goose") if isinstance(meta, dict) else None
        identity = goose.get("toolCall") if isinstance(goose, dict) else None
        if isinstance(identity, dict) and not conflict:
            native_name = identity.get("toolName")
            extension = identity.get("extensionName")
            if isinstance(native_name, str) and native_name.startswith("developer__"):
                if extension in (None, "developer"):
                    extension, native_name = "developer", native_name.removeprefix("developer__")
            if extension == "developer" and isinstance(native_name, str):
                name = {"read": "Read", "write": "Write", "edit": "Edit",
                        "shell": "Bash", "tree": "Glob"}.get(native_name, "Unknown")
                identity_source = "goose_tool_metadata"
            elif extension == "loom-agent-control" and native_name in {
                "retrieve_context", "loom-agent-control__retrieve_context",
                "search_conversations", "loom-agent-control__search_conversations",
                "read_conversation_context", "loom-agent-control__read_conversation_context",
                "get_conversation_details", "loom-agent-control__get_conversation_details",
            }:
                # This exact Loom endpoint is read-only and validates a live,
                # revision-bound capability. Never infer it from a title.
                name = "LoomContextRead"
                identity_source = "goose_tool_metadata"
    # Typed ACP categories support other harnesses and future Goose versions.
    # `other`, display titles and arbitrary extension names cannot auto-allow.
    if name == "Unknown" and not conflict:
        kind = call.get("kind") or evidence.get("kind")
        name = {"read": "Read", "edit": "Edit", "execute": "Bash",
                "fetch": "WebFetch"}.get(kind, "Unknown") if isinstance(kind, str) else "Unknown"
        # ACP search does not distinguish local file search from network search.
        # Require explicit local-path evidence before treating it as Grep.
        if kind == "search" and any(isinstance(args.get(k), str) and args[k] for k in ("path", "file_path")):
            name = "Grep"
        if name != "Unknown":
            identity_source = "acp_kind"
    if name in {"Read", "Write", "Edit"} and not any(
        isinstance(args.get(k), str) and args[k] for k in ("path", "file_path", "filePath")
    ):
        name = "Unknown"
    if name == "Bash":
        command = args.get("command")
        if not ((isinstance(command, str) and command.strip()) or
                (isinstance(command, list) and command and all(isinstance(v, str) and v for v in command))):
            name = "Unknown"
    payload = {**args}
    if cwd and not any(payload.get(k) for k in ("cwd", "workdir", "working_directory")):
        payload["cwd"] = cwd
    payload["acp_identity"] = {"tool_call_id": call_id, "source": identity_source,
                               "conflict": conflict, "title": str(call.get("title") or "")}
    return name, payload
