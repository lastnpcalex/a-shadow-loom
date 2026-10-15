"""Pre-write preservation for surfaced, structured file mutations.

This is an integrity precondition, separate from the permission profile. It does
not infer filesystem effects from arbitrary shell programs or prove that a model
read a file. Native tools execute after this check; they retain a final TOCTOU
window. Broker tools additionally revalidate immediately before writing.
"""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from workspace_safety import (
    MAX_RECOVERY_FILE_BYTES, MAX_RECOVERY_TOTAL_BYTES, _store_blob,
    _write_json_atomic, default_recovery_root,
)


@dataclass(frozen=True)
class Mutation:
    path: str
    kind: str  # replace, edit, delete
    removed_lines: int = 0
    content: str | None = None
    before: str | None = None


@dataclass
class IntegrityCheck:
    entries: list[dict] = field(default_factory=list)
    review_reason: str = ""
    error: str = ""
    manifest_path: str = ""


def _patch_path(value: str, *, git_prefix=False) -> str:
    value = value.split("\t", 1)[0].strip()
    if value.startswith('"'):
        value = json.loads(value)
    if git_prefix and value.startswith(("a/", "b/")):
        value = value[2:]
    return value


def patch_mutations(patch: str) -> list[Mutation]:
    """Decode headers, never paths mentioned inside file contents."""
    mutations = []
    current = None
    old = None
    removed = 0

    def finish():
        if current:
            mutations.append(Mutation(current[0], current[1], removed))

    # Custom apply_patch headers cannot be mistaken for unified diff payloads.
    custom = patch.startswith("*** Begin Patch")
    git_segment = False
    in_hunk = False
    old_remaining = new_remaining = 0
    for line in patch.splitlines():
        if custom:
            match = re.match(r"\*\*\* (Add|Update|Delete) File: (.+)$", line)
            if match:
                finish()
                current = (_patch_path(match[2]), {"Add": "replace", "Update": "edit", "Delete": "delete"}[match[1]])
                removed = 0
            elif line.startswith("*** Move to: "):
                mutations.append(Mutation(_patch_path(line[13:]), "replace"))
            elif line.startswith("-"):
                removed += 1
        else:
            if in_hunk:
                if line.startswith("\\ No newline"):
                    continue
                if line.startswith("-"):
                    old_remaining -= 1
                    removed += 1
                elif line.startswith("+"):
                    new_remaining -= 1
                elif line.startswith(" "):
                    old_remaining -= 1
                    new_remaining -= 1
                else:
                    raise ValueError("Malformed unified-diff hunk")
                if old_remaining < 0 or new_remaining < 0:
                    raise ValueError("Malformed unified-diff line counts")
                in_hunk = bool(old_remaining or new_remaining)
            elif line.startswith("diff --git "):
                if git_segment and current is None:
                    raise ValueError("Git patch segment has no supported text headers")
                finish()
                current, old, removed, in_hunk = None, None, 0, False
                git_segment = True
            elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ",
                                  "GIT binary patch", "Binary files ", "diff --cc ", "diff --combined ")):
                raise ValueError("Rename/copy/binary/combined patches require a supported structured file request")
            elif re.search(r"(?:mode |^index .* )(?:120000|160000)$", line):
                raise ValueError("Symlink/submodule patches are not supported by the file guard")
            elif line.startswith("--- "):
                if current:
                    finish()
                    current, removed = None, 0
                old = _patch_path(line[4:], git_prefix=True)
            elif line.startswith("+++ "):
                if old is None:
                    raise ValueError("Unified diff is missing its source header")
                new = _patch_path(line[4:], git_prefix=True)
                current = (old, "delete") if new == "/dev/null" else (new, "replace" if old == "/dev/null" else "edit")
                if old not in (None, "/dev/null", new) and new != "/dev/null":
                    mutations.append(Mutation(old, "delete"))
                    current = (new, "replace")
            elif line.startswith("@@"):
                if current is None:
                    raise ValueError("Unified diff is missing its file headers")
                match = re.match(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", line)
                if not match:
                    raise ValueError("Malformed unified-diff header")
                old_remaining = int(match[1]) if match[1] is not None else 1
                new_remaining = int(match[2]) if match[2] is not None else 1
                in_hunk = bool(old_remaining or new_remaining)
    if in_hunk:
        raise ValueError("Incomplete unified-diff hunk")
    if git_segment and current is None:
        raise ValueError("Git patch segment has no supported text headers")
    finish()
    return mutations


def mutations_for_request(tool_name: str, tool_input, approval_method="") -> list[Mutation]:
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except ValueError:
            tool_input = {"patch": tool_input}
    if not isinstance(tool_input, dict):
        return []
    d = tool_input
    name = tool_name.lower()
    changes = d.get("changes", d.get("fileChanges"))
    if isinstance(changes, (dict, list)) and (name == "edit" or "filechange" in approval_method.lower()):
        result = []
        pairs = changes.items() if isinstance(changes, dict) else ((c.get("path"), c) for c in changes if isinstance(c, dict))
        for path, change in pairs:
            if not isinstance(change, dict) or not isinstance(path, str):
                continue
            kind = change.get("kind", change.get("type", "update"))
            move = (kind.get("move_path", kind.get("movePath")) if isinstance(kind, dict)
                    else change.get("move_path", change.get("movePath")))
            kind = kind.get("type") if isinstance(kind, dict) else kind
            diff = change.get("diff") or change.get("unifiedDiff") or ""
            removed = sum(line.startswith("-") and not line.startswith("---") for line in str(diff).splitlines())
            result.append(Mutation(path, {"add": "replace", "delete": "delete", "remove": "delete"}.get(kind, "edit"), removed))
            if isinstance(move, str):
                result.append(Mutation(move, "replace"))
        return result
    if name in {"apply_patch", "patch", "apply_unified_patch"}:
        patch = next((d[k] for k in ("patch", "patch_text", "input", "command") if isinstance(d.get(k), str)), "")
        targets = patch_mutations(patch.strip())
        if not targets:
            raise ValueError("Patch request has no supported file headers")
        return targets
    if name not in {"write", "write_file", "create_file", "edit", "edit_file", "replace", "delete_file", "remove_file"}:
        return []
    paths = [d[k] for k in ("file_path", "path", "filePath", "TargetFile") if isinstance(d.get(k), str) and d[k]]
    kind = "replace" if name in {"write", "write_file", "create_file"} else "delete" if name in {"delete_file", "remove_file"} else "edit"
    before = d.get("old_string", d.get("before", ""))
    removed = len(before.splitlines()) if isinstance(before, str) else 0
    content = d.get("content") if isinstance(d.get("content"), str) else None
    return [Mutation(path, kind, removed, content, before if isinstance(before, str) else None) for path in dict.fromkeys(paths)]


def checked_path(root: Path, path: str, cwd: str = "") -> Path:
    """Reject traversal through links/junctions, including dangling final links."""
    base = Path(cwd) if cwd else root
    if not base.is_absolute():
        base = root / base
    raw = Path(path)
    raw = raw if raw.is_absolute() else base / raw
    raw = Path(os.path.abspath(raw))
    if raw == root or not raw.is_relative_to(root):
        raise ValueError("Mutation target is outside the assigned workspace")
    cursor = raw
    while cursor != root:
        if cursor.is_symlink() or cursor.is_junction():
            raise ValueError("Mutation target traverses a symlink or junction")
        cursor = cursor.parent
    return raw


def live_version(path: Path) -> tuple[str | None, bytes | None]:
    try:
        before = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None, None
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_RECOVERY_FILE_BYTES:
        raise ValueError("Target is not a regular file within the recovery size limit")
    with path.open("rb") as handle:
        data = handle.read(MAX_RECOVERY_FILE_BYTES + 1)
        opened = os.fstat(handle.fileno())
    after = path.stat(follow_symlinks=False)
    # Windows Python stat(path) and fstat(handle) can expose different ctime
    # semantics (creation versus change time). Never compare those as one clock.
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns,
                          s.st_ctime_ns if os.name != "nt" else None)
    if len(data) > MAX_RECOVERY_FILE_BYTES or identity(before) != identity(opened) or identity(before) != identity(after):
        raise ValueError("Target changed during recovery capture")
    return hashlib.sha256(data).hexdigest(), data


def prepare_integrity_check(tool_name, tool_input, *, workspace, approval_method="", conversation_id=0, scope="", recovery_root=None) -> IntegrityCheck:
    result = IntegrityCheck()
    try:
        mutations = mutations_for_request(tool_name, tool_input, approval_method)
        if not mutations or not workspace:
            return result
        if len(mutations) > 128:
            raise ValueError("Too many file targets in one request (maximum 128)")
        root = Path(workspace).resolve()
        recovery = Path(recovery_root or default_recovery_root()).resolve()
        if recovery == root or recovery.is_relative_to(root):
            raise ValueError("Recovery storage must be outside the workspace")
        cwd = next((tool_input[k] for k in ("cwd", "workdir", "working_directory", "Cwd")
                    if isinstance(tool_input.get(k), str) and tool_input[k]), "") if isinstance(tool_input, dict) else ""
        reasons = []
        total = 0
        for mutation in mutations:
            # Outside-workspace approvals keep their existing policy. Do not
            # read private external destinations while classifying a request.
            try:
                path = checked_path(root, mutation.path, cwd)
            except ValueError as exc:
                if "outside the assigned workspace" in str(exc):
                    continue
                raise
            digest, data = live_version(path)
            entry = {"path": str(path), "workspace": str(root), "sha256": digest, "size": len(data) if data is not None else None,
                     "kind": mutation.kind, "recovery_blob": None}
            result.entries.append(entry)
            if data is None:
                continue
            total += len(data)
            if total > MAX_RECOVERY_TOTAL_BYTES:
                raise ValueError("Pre-write recovery request exceeds its total size limit")
            # Journal every existing structured-write target, even if ignored.
            entry["recovery_blob"] = _store_blob(recovery / "blobs", digest, data)
            # Unlike metadata cache checks, an overwrite safeguard verifies the
            # actual stored bytes before granting write authority.
            if hashlib.sha256(Path(entry["recovery_blob"]).read_bytes()).hexdigest() != digest:
                raise ValueError("Recovery blob failed content verification")
            relative = path.relative_to(root).as_posix()
            unchanged = mutation.content is not None and mutation.content.encode("utf-8") == data
            if mutation.kind == "replace" and not unchanged:
                reasons.append(f"Replace existing file {relative} ({len(data)} bytes). A prior agent read is not verified; review preservation of existing functionality.")
            elif mutation.kind == "delete":
                reasons.append(f"Delete existing file {relative} ({len(data)} bytes).")
            elif data and mutation.before and mutation.before.encode("utf-8") == data:
                reasons.append(f"Replace entire contents of {relative} through an Edit operation.")
            elif (data and mutation.removed_lines >= len(data.splitlines())) or mutation.removed_lines >= 25 or (mutation.removed_lines >= 5 and mutation.removed_lines * 2 >= len(data.splitlines())):
                reasons.append(f"Large replacement in {relative}: {mutation.removed_lines} existing lines targeted.")
        if any(entry["recovery_blob"] for entry in result.entries):
            manifest = recovery / "writes" / f"conversation-{int(conversation_id)}" / f"{uuid.uuid4().hex}.json"
            _write_json_atomic(manifest, {"version": 1, "created_at": time.time(), "conversation_id": conversation_id,
                                        "scope": scope, "workspace": str(root), "files": result.entries})
            result.manifest_path = str(manifest)
        result.review_reason = " ".join(reasons[:4])
        if result.review_reason and result.manifest_path:
            result.review_reason += f" Pre-write recovery: {result.manifest_path}. One-time approval only."
    except (OSError, ValueError, TypeError) as exc:
        result.error = f"Loom workspace preservation could not verify this mutation: {exc}. No write was authorized."
    return result


def revalidate_integrity(check: IntegrityCheck) -> str:
    try:
        for entry in check.entries:
            path = checked_path(Path(entry["workspace"]), entry["path"])
            digest, _ = live_version(path)
            if digest != entry["sha256"]:
                return "Loom workspace changed while approval was pending. Read the current file and submit a new scoped edit; the old write was not authorized."
    except (OSError, ValueError) as exc:
        return f"Loom could not revalidate the approved file: {exc}. The write was not authorized."
    return ""
