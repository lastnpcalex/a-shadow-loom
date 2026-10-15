"""Child access declarations and disclosure projections.

Reservations coordinate cooperative agents, not arbitrary processes. Structured
file tools enforce paths; a shell's effects cannot be inferred from its cwd.
"""
import os
from pathlib import Path


def validate(root, value):
    if value is None:
        return None  # legacy whole-scope reservation
    if not isinstance(value, dict) or set(value) - {"read", "write", "sensitive"}:
        raise ValueError("access accepts read, write and sensitive only")
    result = {}
    workspace = Path(root).resolve()
    for key in ("read", "write"):
        paths = value.get(key, [])
        if not isinstance(paths, list) or any(not isinstance(p, str) or not p.strip() for p in paths):
            raise ValueError(f"access.{key} must be a list of nonempty paths")
        normalized = []
        for path in paths:
            resolved = (workspace / path).resolve()
            if resolved != workspace and workspace not in resolved.parents:
                raise ValueError(f"access.{key} escapes workspace")
            normalized.append(str(resolved.relative_to(workspace)))
        result[key] = sorted(set(normalized))
    if not isinstance(value.get("sensitive", False), bool):
        raise ValueError("access.sensitive must be Boolean")
    result["sensitive"] = value.get("sensitive", False)
    return result


def contains(parent, child):
    p = os.path.normcase(os.path.normpath(parent)).split(os.sep)
    c = os.path.normcase(os.path.normpath(child)).split(os.sep)
    return parent == "." or c[:len(p)] == p


def overlap(left, right):
    return any(contains(a, b) or contains(b, a) for a in left for b in right)


def scopes(run):
    access = run.get("access")
    if access is None:
        paths = run.get("path_scope") or ["."]
        return paths, paths
    return access["read"] + access["write"], access["write"]


def conflict_flags(same_workspace, left_write_overlap, right_write_overlap):
    return same_workspace and (left_write_overlap or right_write_overlap)


def conflicts(left, right):
    same = os.path.normcase(os.path.abspath(left.get("project_dir") or ".")) == os.path.normcase(os.path.abspath(right.get("project_dir") or "."))
    lr, lw = scopes(left)
    rr, rw = scopes(right)
    return conflict_flags(same, overlap(lw, rr), overlap(rw, lr))


def narrows(parent, child):
    pr, pw = scopes(parent)
    cr, cw = scopes(child)
    return inherits(sensitive(parent), sensitive(child), True,
                    all(any(contains(p, c) for p in pr) for c in cr),
                    all(any(contains(p, c) for p in pw) for c in cw))


def sensitive(run):
    return bool((run.get("access") or {}).get("sensitive"))


def may_disclose(is_sensitive, human, released):
    return not is_sensitive or human or released


def admitted(conflict, capacity, local_capacity):
    return not conflict and capacity and local_capacity


def inherits(parent_sensitive, child_sensitive, local, read_narrow, write_narrow):
    return read_narrow and write_narrow and (not parent_sensitive or (child_sensitive and local))


def public_run(run, *, human=False):
    if may_disclose(sensitive(run), human, False):
        return run
    # Allowlist, never redact an evolving raw record by removing known fields.
    safe = {k: run.get(k) for k in ("run_id", "conversation_id", "parent_run_id", "status",
                                  "created_at", "started_at", "completed_at", "queue_reason")}
    safe.update(task="Sensitive local task", sensitive=True,
                result={"text": run["released_result"]} if run.get("released_result") else None)
    return safe


def event_status(payload):
    # A provider's free-form diagnostic is not public lifecycle metadata.
    statuses = {"queued", "launching", "running", "completed", "failed", "paused", "cancelled", "cancel_requested"}
    reasons = {"workspace_conflict", "inference_capacity", "worker_capacity"}
    return {"status": payload.get("status") if payload.get("status") in statuses else None,
            "queue_reason": payload.get("queue_reason") if payload.get("queue_reason") in reasons else None}


def tool_error(run, name, arguments, effect):
    access = run.get("access")
    if access is None:
        return ""  # preserve legacy callers
    controls = ("list_agents", "get_agent", "wait_agent", "cancel_agent", "resume_agent", "spawn_agent", "retrieve_context", "attach_to_chat", "link_file")
    if any(name == prefix + verb for prefix in ("mcp__loom-agent-control__", "mcp__loom_agent_control__") for verb in controls):
        return ""  # server admission and disclosure still validate these calls
    if effect == "network" and sensitive(run):
        return "Sensitive child network tools are disabled."
    if sensitive(run) and effect in {"execute", "unknown", "external_mutation"}:
        return "Sensitive children require structured local tools; unrestricted execution and external tools are disabled."
    if not access["write"] and effect in {"unknown", "external_mutation"}:
        # Exact control tools manage runs, not workspace files.
        return "Read-only children cannot use unclassified or mutating tools."
    if effect == "execute" and not access["write"]:
        return "Read-only children cannot execute commands."
    if effect not in {"read", "sensitive_read", "workspace_write"}:
        return ""
    args = arguments if isinstance(arguments, dict) else {}
    if name.lower() in {"glob", "grep", "list_dir", "list_directory"} and not args.get("path"):
        args = {**args, "path": "."}
    path = next((args[k] for k in ("file_path", "path", "target_file", "AbsolutePath") if isinstance(args.get(k), str)), None)
    if not path:
        # Read-only control/metadata tools do not address filesystem paths.
        if effect == "workspace_write":
            return "Scoped writes require a structured file path."
        return ""
    root = Path(run["project_dir"]).resolve()
    target = (Path(args.get("cwd") or root) / path).resolve()
    if target != root and root not in target.parents:
        return "Child file access escapes its workspace."
    relative = str(target.relative_to(root))
    allowed = access["write"] if effect == "workspace_write" else access["read"] + access["write"]
    if not any(contains(p, relative) for p in allowed):
        return "Child file access is outside its declared access."
    return ""
