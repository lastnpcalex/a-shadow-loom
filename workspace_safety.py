"""Provider-independent recovery snapshots for agent workspaces.

Snapshots are intentionally outside the project checkout. They preserve the
live files an agent inherited (including uncommitted and untracked files), then
produce a post-turn report without mutating the workspace or Git index.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

from workspace_fingerprint import file_fingerprint


_WINDOWS_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0


MAX_RECOVERY_FILE_BYTES = 8 * 1024 * 1024
MAX_RECOVERY_TOTAL_BYTES = 256 * 1024 * 1024
MAX_REPORTED_FILES = 500
MAX_CANVAS_ENTRIES = 4096
_FALLBACK_EXCLUDES = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", ".env", "node_modules",
    "__pycache__", ".pytest_cache", ".pytest_tmp", ".mypy_cache", ".ruff_cache",
    ".tox", ".nox", ".cache", "dist", "build", "target", "out",
    ".next", ".nuxt", ".turbo", ".parcel-cache",
    "workspaces", "backstage_workspaces", "canvas_workspaces",
}

_EXCLUDE_PATH_RE = re.compile(
    r"(^|/)(?:"
    r"\.git|\.hg|\.svn|\.venv|venv|env|node_modules|__pycache__|"
    r"dist|build|target|out|\.next|\.nuxt|\.turbo|\.parcel-cache|"
    r"\.pytest_cache|\.pytest_tmp|\.pytest-.*|pytest-tmp.*|"
    r"\.mypy_cache|\.ruff_cache|\.tox|\.nox|\.cache|"
    r"\.tmp.*|tmp_.*|"
    r"recovery_stage_.*|recovery_verify_.*|"
    r"chrome-profile|chromium-profile|User Data|"
    r"workspaces|backstage_workspaces|canvas_workspaces|"
    r"to-delete.*|test-output.*|slide-qa-.*"
    r")(/|$)",
    re.IGNORECASE,
)


def _is_excluded_path(path_str: str) -> bool:
    normalized = path_str.replace("\\", "/")
    return bool(_EXCLUDE_PATH_RE.search(normalized))


@dataclass(frozen=True)
class WorkspaceSnapshot:
    snapshot_id: str
    root: str
    manifest_path: str
    stats: dict | None = None


def default_recovery_root() -> Path:
    """Return persistent recovery storage that is never inside the checkout by default."""
    override = os.environ.get("LOOM_WORKSPACE_RECOVERY_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data).expanduser().resolve() / "A Shadow Loom" / "workspace-recovery"
    state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    if state_home:
        return Path(state_home).expanduser().resolve() / "a-shadow-loom" / "workspace-recovery"
    return Path.home().resolve() / ".local" / "state" / "a-shadow-loom" / "workspace-recovery"


def _git_bytes(root: Path, args: list[str]) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            creationflags=_WINDOWS_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _decode_nul_paths(payload: bytes | None) -> set[str]:
    if payload is None:
        return set()
    return {
        part.decode("utf-8", errors="replace").replace("\\", "/")
        for part in payload.split(b"\0")
        if part
    }


def _fallback_paths(root: Path) -> set[str]:
    paths: set[str] = set()
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in _FALLBACK_EXCLUDES
                   and not _is_excluded_path(d)
                   and not (Path(current) / d).is_symlink()
                   and not (Path(current) / d).is_junction()]
        base = Path(current)
        for name in files:
            path = base / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                rel = path.relative_to(root).as_posix()
                if not _is_excluded_path(rel):
                    paths.add(rel)
            except (OSError, ValueError):
                continue
    return paths


def _workspace_paths(root: Path) -> tuple[set[str], set[str], bool, str | None]:
    top_level_raw = _git_bytes(root, ["rev-parse", "--show-toplevel"])
    if top_level_raw is None:
        return _fallback_paths(root), set(), False, None
    try:
        top_level = Path(
            top_level_raw.decode("utf-8", errors="replace").strip()
        ).resolve()
    except (OSError, ValueError):
        return _fallback_paths(root), set(), False, None
    if top_level != root:
        # A directory nested beneath some unrelated parent checkout is its own
        # workspace for snapshot purposes. Inheriting the parent's ignore rules
        # can otherwise make every live file disappear from the snapshot.
        return _fallback_paths(root), set(), False, None

    tracked_and_untracked = _git_bytes(
        root, ["ls-files", "-co", "--exclude-standard", "-z"]
    )
    if tracked_and_untracked is None:
        return _fallback_paths(root), set(), False, None

    tracked = _decode_nul_paths(_git_bytes(root, ["ls-files", "-z"]))
    paths = {p for p in _decode_nul_paths(tracked_and_untracked)
             if p in tracked or not _is_excluded_path(p)}
    status_raw = _git_bytes(root, ["status", "--porcelain=v1", "-z"])
    if status_raw is not None:
        dirty_raw: set[str] = set()
        parts = status_raw.split(b"\0")
        i = 0
        while i < len(parts):
            part = parts[i]
            if len(part) > 3:
                status_code = part[:2]
                dirty_raw.add(part[3:].decode("utf-8", errors="replace").replace("\\", "/"))
                if b"R" in status_code or b"C" in status_code:
                    i += 1
                    if i < len(parts) and parts[i]:
                        dirty_raw.add(parts[i].decode("utf-8", errors="replace").replace("\\", "/"))
            i += 1
        dirty = {p for p in dirty_raw if p in tracked or not _is_excluded_path(p)}
    else:
        dirty = set()
        dirty.update(_decode_nul_paths(_git_bytes(root, ["diff", "--name-only", "--no-renames", "-z"])))
        dirty.update(_decode_nul_paths(_git_bytes(root, ["diff", "--cached", "--name-only", "--no-renames", "-z"])))
        dirty.update(_decode_nul_paths(_git_bytes(root, ["ls-files", "--others", "--exclude-standard", "-z"])))
        dirty = {p for p in dirty if p in tracked or not _is_excluded_path(p)}
    head_raw = _git_bytes(root, ["rev-parse", "HEAD"])
    head = head_raw.decode("ascii", errors="replace").strip() if head_raw else None
    return paths, dirty, True, head


def _safe_dir(
    directory: Path,
    root: Path,
    dir_cache: dict[Path, bool],
    dir_lock: threading.Lock | None = None,
) -> bool:
    if directory == root:
        return True
    if dir_lock:
        with dir_lock:
            cached = dir_cache.get(directory)
    else:
        cached = dir_cache.get(directory)
    if cached is not None:
        return cached

    chain: list[Path] = []
    curr = directory
    safe = True
    while curr != root:
        if dir_lock:
            with dir_lock:
                hit = dir_cache.get(curr)
        else:
            hit = dir_cache.get(curr)
        if hit is not None:
            if not hit:
                safe = False
            break
        if curr.parent == curr:
            safe = False
            break
        try:
            if curr.is_symlink() or curr.is_junction():
                safe = False
                break
        except OSError:
            safe = False
            break
        chain.append(curr)
        curr = curr.parent

    if dir_lock:
        with dir_lock:
            for d in chain:
                dir_cache[d] = safe
    else:
        for d in chain:
            dir_cache[d] = safe
    return safe


def _safe_file(
    root: Path,
    relative: str,
    dir_cache: dict[Path, bool] | None = None,
    dir_lock: threading.Lock | None = None,
) -> Path | None:
    try:
        raw_candidate = root / relative
        if raw_candidate == root:
            return None
        if dir_cache is not None:
            if raw_candidate.is_symlink() or raw_candidate.is_junction():
                return None
            if not _safe_dir(raw_candidate.parent, root, dir_cache, dir_lock):
                return None
        else:
            cursor = raw_candidate
            while cursor != root:
                if cursor.is_symlink() or cursor.is_junction():
                    return None
                if cursor.parent == cursor:
                    return None
                cursor = cursor.parent
        if not raw_candidate.is_file():
            return None
        return raw_candidate
    except (OSError, ValueError):
        return None


def _canvas_paths(root: Path) -> tuple[set[str], list[dict]]:
    """Canvas is user-authored live state even though its default ignores '*'.

    Bound metadata traversal; do not follow links or scoop up dependency trees.
    Other ignored directories are covered only by surfaced pre-write journals.
    """
    paths, skipped, pending = set(), [], [root / "canvas"]
    # Prioritize the entry even when directory enumeration reaches its cap.
    entry = root / "canvas" / "index.html"
    try:
        if not pending[0].is_symlink() and not pending[0].is_junction():
            if entry.is_file() and not entry.is_symlink():
                paths.add("canvas/index.html")
    except OSError:
        skipped.append({"path": "canvas/index.html", "reason": "unreadable_canvas_entry"})
    seen = 0
    while pending:
        directory = pending.pop()
        try:
            if directory.is_symlink() or directory.is_junction():
                skipped.append({"path": directory.relative_to(root).as_posix(), "reason": "linked_canvas_directory"})
                continue
            with os.scandir(directory) as entries:
                for entry in entries:
                    seen += 1
                    if seen > MAX_CANVAS_ENTRIES:
                        skipped.append({"path": "canvas/", "reason": "canvas_entry_limit"})
                        return paths, skipped
                    relative = Path(entry.path).relative_to(root).as_posix()
                    if entry.is_symlink() or Path(entry.path).is_junction():
                        skipped.append({"path": relative, "reason": "linked_canvas_entry"})
                    elif entry.is_dir(follow_symlinks=False):
                        if entry.name not in _FALLBACK_EXCLUDES:
                            pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        paths.add(relative)
        except FileNotFoundError:
            pass
        except OSError:
            skipped.append({"path": directory.relative_to(root).as_posix(), "reason": "unreadable_canvas_directory"})
    return paths, skipped


def _read_file(path: Path) -> bytes | None:
    try:
        # Bound allocations even if a file grows after the size probe.
        with path.open("rb") as stream:
            return stream.read(MAX_RECOVERY_FILE_BYTES + 1)
    except OSError:
        return None


def _is_text(data: bytes) -> bool:
    if b"\0" in data[:8192]:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _store_blob(blob_root: Path, digest: str, data: bytes) -> str:
    blob = blob_root / digest[:2] / digest
    if not _blob_available(blob, len(data)):
        blob.parent.mkdir(parents=True, exist_ok=True)
        temp = blob.with_name(f".{digest}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_bytes(data)
            os.replace(temp, blob)
        except OSError:
            # Another capture can publish the same content-addressed blob while
            # Windows rejects our replacement. Accept only identical bytes;
            # permission/disk failures without a complete winner still fail.
            if not _blob_available(blob, len(data)) or _read_file(blob) != data:
                raise
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
    return str(blob)


def _blob_available(path: Path, size: int) -> bool:
    try:
        return not path.is_symlink() and path.is_file() and path.stat().st_size == size
    except OSError:
        return False


def _index_path(recovery_root: Path, root: Path) -> Path:
    key = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()
    return recovery_root / "indexes" / f"{key}.json"


def _load_index(path: Path, root: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("version") == 1 and value.get("workspace") == str(root):
            files = value.get("files")
            return files if isinstance(files, dict) else {}
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def _save_index(path: Path, root: Path, files: dict) -> None:
    # The index is only a hint. Concurrent writers may replace it with an older
    # view; current fingerprints still decide reuse. Immutable manifests/blobs
    # are the recovery authority, not this mutable pointer.
    try:
        _write_json_atomic(path, {"version": 1, "workspace": str(root), "files": files})
    except OSError:
        pass


def _reusable_entry(entry, fingerprint, blob_root: Path) -> dict | None:
    if not fingerprint or not isinstance(entry, dict) or entry.get("fingerprint") != fingerprint:
        return None
    digest = entry.get("sha256")
    size = entry.get("size")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    if not isinstance(size, int) or size < 0:
        return None
    blob = blob_root / digest[:2] / digest
    if not _blob_available(blob, size):
        return None
    return {"sha256": digest, "size": size, "text": bool(entry.get("text")),
            "fingerprint": fingerprint, "recovery_blob": str(blob)}


def _probe_path(
    root: Path,
    relative: str,
    cache_entry: dict | None,
    blob_root: Path,
    dir_cache: dict[Path, bool],
    dir_lock: threading.Lock,
) -> tuple[str, Path | None, list[int | str] | None, dict | None, bool]:
    path = _safe_file(root, relative, dir_cache, dir_lock)
    if path is None:
        deleted = False
        try:
            (root / relative).lstat()
        except FileNotFoundError:
            deleted = True
        except OSError:
            pass
        return (relative, None, None, None, deleted)
    fingerprint = file_fingerprint(path)
    reused = _reusable_entry(cache_entry, fingerprint, blob_root)
    return (relative, path, fingerprint, reused, False)


def _collect_files(root: Path, paths: set[str], dirty: set[str], blob_root: Path, cache: dict):
    files, skipped = {}, []
    stats = {"files_read": 0, "files_reused": 0, "bytes_read": 0,
             "stored_bytes": 0, "protected_bytes": 0, "skipped_count": 0}
    # Protect the Canvas entry, then live modifications; bulky Canvas assets
    # must not displace dirty source files when a cold capture reaches its cap.
    sorted_paths = sorted(paths, key=lambda p: (p != "canvas/index.html", p not in dirty, not p.startswith("canvas/"), p))
    if not sorted_paths:
        return files, skipped, stats

    dir_cache: dict[Path, bool] = {}
    dir_lock = threading.Lock()
    max_workers = min(16, max(1, os.cpu_count() or 4), len(sorted_paths))
    probe_results: dict[str, tuple[Path | None, list[int | str] | None, dict | None, bool]] = {}

    def worker(r: str):
        return _probe_path(root, r, cache.get(r), blob_root, dir_cache, dir_lock)

    if max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for r, path, fp, reused, deleted in executor.map(worker, sorted_paths):
                probe_results[r] = (path, fp, reused, deleted)
    else:
        for r in sorted_paths:
            _, path, fp, reused, deleted = worker(r)
            probe_results[r] = (path, fp, reused, deleted)

    for relative in sorted_paths:
        path, fingerprint, reused, deleted = probe_results[relative]
        if path is None:
            # Missing paths are normal deletions. An existing unreadable or
            # unsupported target is not evidence of deletion.
            if deleted:
                continue
            files[relative] = {"sha256": None, "recovery_blob": None, "size": None,
                               "dirty_before": relative in dirty}
            skipped.append({"path": relative, "reason": "unreadable_or_unsupported"})
            continue
        if reused is not None:
            files[relative] = {**reused, "dirty_before": relative in dirty}
            stats["files_reused"] += 1
            stats["protected_bytes"] += reused["size"]
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        entry = {"sha256": None, "size": size, "dirty_before": relative in dirty,
                 "text": None, "recovery_blob": None, "fingerprint": None}
        files[relative] = entry
        reason = None
        if size is None:
            reason = "unreadable"
        elif size > MAX_RECOVERY_FILE_BYTES:
            reason = "file_too_large"
        elif stats["bytes_read"] + size > MAX_RECOVERY_TOTAL_BYTES:
            reason = "snapshot_cap"
        if reason:
            skipped.append({"path": relative, "reason": reason, "size": size})
            continue
        data = _read_file(path)
        stats["files_read"] += 1
        if data is None:
            skipped.append({"path": relative, "reason": "unreadable"})
            continue
        stats["bytes_read"] += len(data)
        after_fingerprint = file_fingerprint(path)
        # A changing file cannot be cached or called a stable recovery copy.
        if ((fingerprint is not None and after_fingerprint != fingerprint)
                or len(data) != size):
            skipped.append({"path": relative, "reason": "changed_during_capture"})
            continue
        if len(data) > MAX_RECOVERY_FILE_BYTES or stats["bytes_read"] > MAX_RECOVERY_TOTAL_BYTES:
            skipped.append({"path": relative, "reason": "snapshot_cap"})
            continue
        digest = hashlib.sha256(data).hexdigest()
        blob = blob_root / digest[:2] / digest
        if not _blob_available(blob, len(data)):
            stats["stored_bytes"] += len(data)
        entry.update(sha256=digest, size=len(data), text=_is_text(data),
                     recovery_blob=_store_blob(blob_root, digest, data),
                     fingerprint=after_fingerprint if fingerprint is not None else None)
        stats["protected_bytes"] += len(data)
    stats["skipped_count"] = len(skipped)
    return files, skipped, stats


def capture_workspace_snapshot(
    workspace: str | Path,
    recovery_root: str | Path,
    *,
    conversation_id: int,
    generation_id: int | None,
) -> WorkspaceSnapshot | None:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        return None

    recovery_root = Path(recovery_root).expanduser().resolve()
    if recovery_root == root or root in recovery_root.parents:
        raise ValueError("Workspace recovery storage must be outside the workspace")

    paths, dirty, is_git, head = _workspace_paths(root)
    canvas_paths, canvas_skipped = _canvas_paths(root)
    paths.update(canvas_paths)
    now = time.time()
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_id = f"{stamp}-c{conversation_id}-g{generation_id or 0}-{uuid.uuid4().hex[:8]}"
    snapshot_dir = recovery_root / "snapshots" / f"conversation-{conversation_id}" / snapshot_id
    blob_root = recovery_root / "blobs"

    index_path = _index_path(recovery_root, root)
    files, skipped, stats = _collect_files(root, paths, dirty, blob_root, _load_index(index_path, root))
    skipped.extend(canvas_skipped)
    stats["skipped_count"] = len(skipped)

    manifest = {
        "version": 2,
        "snapshot_id": snapshot_id,
        "workspace": str(root),
        "conversation_id": conversation_id,
        "generation_id": generation_id,
        "created_at": now,
        "git": is_git,
        "git_head": head,
        "coverage": "Git-visible files plus bounded Canvas tree; other ignored files require a surfaced pre-write journal.",
        "files": files,
        "skipped": skipped,
        "stored_bytes": stats["stored_bytes"],
        "stats": stats,
        "blob_root": str(blob_root),
        "restore_note": (
            "Copy a recovery_blob back to workspace/path only after reviewing the live file. "
            "Snapshots never restore files automatically."
        ),
    }
    manifest_path = snapshot_dir / "manifest.json"
    _write_json_atomic(manifest_path, manifest)
    _save_index(index_path, root, files)
    return WorkspaceSnapshot(snapshot_id, str(root), str(manifest_path), stats)


def _line_delta(before: bytes, after: bytes) -> tuple[int, int] | tuple[None, None]:
    if not _is_text(before) or not _is_text(after):
        return None, None
    before_lines = before.decode("utf-8").splitlines()
    after_lines = after.decode("utf-8").splitlines()
    added = 0
    removed = 0
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            removed += i2 - i1
        if tag in {"replace", "insert"}:
            added += j2 - j1
    return added, removed


def finalize_workspace_snapshot(snapshot: WorkspaceSnapshot) -> dict:
    manifest_path = Path(snapshot.manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = Path(manifest["workspace"]).resolve()
    current_paths, dirty, _, current_head = _workspace_paths(root)
    canvas_paths, canvas_skipped = _canvas_paths(root)
    current_paths.update(canvas_paths)
    before_files: dict[str, dict] = manifest.get("files", {})
    all_paths = sorted(set(before_files) | current_paths)
    changed: list[dict] = []
    total_added = 0
    total_removed = 0
    deleted = 0
    preexisting_changed = 0
    blob_root = Path(manifest.get("blob_root") or manifest_path.parents[3] / "blobs")
    index_path = _index_path(blob_root.parent, root)
    cache = {**before_files, **_load_index(index_path, root)}
    after_files, skipped, stats = _collect_files(root, set(all_paths), dirty, blob_root, cache)
    skipped.extend(canvas_skipped)
    stats["skipped_count"] = len(skipped)
    after_manifest_path = manifest_path.parent / "after-manifest.json"
    _write_json_atomic(after_manifest_path, {
        "version": 2, "snapshot_id": snapshot.snapshot_id, "workspace": str(root),
        "git_head": current_head, "files": after_files, "skipped": skipped, "stats": stats,
    })
    _save_index(index_path, root, after_files)

    for relative in all_paths:
        before = before_files.get(relative)
        after = after_files.get(relative)
        if before is None and after is None:
            continue
        after_hash = after.get("sha256") if after else None
        if before and before.get("sha256") and before["sha256"] == after_hash:
            continue
        if before is None:
            status = "created"
        elif after is None:
            status = "deleted"
            deleted += 1
        elif not before.get("sha256") or not after_hash:
            status = "unverified"
        else:
            status = "modified"

        after_recovery_blob = after.get("recovery_blob") if after else None
        after_data = _read_file(Path(after_recovery_blob)) if after_recovery_blob else None
        before_data = b"" if before is None else None
        blob = before.get("recovery_blob") if before else None
        if blob:
            before_data = _read_file(Path(blob))
        comparable = before_data is not None and (after_data is not None or after is None)
        added, removed = _line_delta(before_data, after_data or b"") if comparable else (None, None)
        if added is not None:
            total_added += added
        if removed is not None:
            total_removed += removed
        dirty_before = bool(before and before.get("dirty_before"))
        if dirty_before and status != "unverified":
            preexisting_changed += 1
        changed.append(
            {
                "path": relative,
                "status": status,
                "dirty_before": dirty_before,
                "before_sha256": before.get("sha256") if before else None,
                "after_sha256": after_hash,
                "added_lines": added,
                "removed_lines": removed,
                "recovery_blob": blob,
                "after_recovery_blob": after_recovery_blob,
            }
        )

    warnings: list[str] = []
    if manifest.get("skipped") or skipped:
        warnings.append(f"Recovery coverage is partial: {len(manifest.get('skipped', []))} pre-turn and {len(skipped)} post-turn file(s) skipped")
    if deleted:
        warnings.append(f"{deleted} file(s) were deleted")
    if preexisting_changed:
        warnings.append(f"{preexisting_changed} file(s) that already had live changes were modified again")
    if total_removed >= 25:
        warnings.append(f"{total_removed} lines were removed across text files")
    if manifest.get("git_head") and current_head and manifest["git_head"] != current_head:
        warnings.append("Git HEAD changed during the agent turn")

    verified_changes = [item for item in changed if item["status"] != "unverified"]
    unverified = [item["path"] for item in changed if item["status"] == "unverified"]
    report = {
        "type": "workspace_change_report",
        "snapshot_id": snapshot.snapshot_id,
        "workspace": str(root),
        "manifest_path": str(manifest_path),
        "coverage": manifest.get("coverage", "Legacy snapshot; ignored-file coverage is not guaranteed."),
        "changed_count": len(verified_changes),
        "created_count": sum(1 for item in changed if item["status"] == "created"),
        "modified_count": sum(1 for item in changed if item["status"] == "modified"),
        "deleted_count": deleted,
        "added_lines": total_added,
        "removed_lines": total_removed,
        "stored_after_bytes": stats["stored_bytes"],
        "snapshot_stats": stats,
        "after_manifest_path": str(after_manifest_path),
        "unverified_count": len(unverified),
        "unverified_files": unverified[:MAX_REPORTED_FILES],
        "warnings": warnings,
        "changed_files": verified_changes[:MAX_REPORTED_FILES],
        "truncated": max(len(verified_changes), len(unverified)) > MAX_REPORTED_FILES,
    }
    _write_json_atomic(manifest_path.parent / "change-report.json", report)
    return report
