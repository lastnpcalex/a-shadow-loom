"""Recovery snapshot and workspace-change audit tests."""

from pathlib import Path
import hashlib
import json
import os
import subprocess
import time

import pytest
import workspace_safety as safety
from workspace_fingerprint import file_fingerprint

from workspace_safety import (
    capture_workspace_snapshot,
    default_recovery_root,
    finalize_workspace_snapshot,
)


def test_default_recovery_root_honors_external_override(monkeypatch, tmp_path):
    recovery = tmp_path / "outside" / "recovery"
    monkeypatch.setenv("LOOM_WORKSPACE_RECOVERY_DIR", str(recovery))
    assert default_recovery_root() == recovery.resolve()


def test_workspace_snapshot_recovers_preexisting_files_and_reports_churn(tmp_path):
    workspace = tmp_path / "workspace"
    recovery = tmp_path / "recovery"
    workspace.mkdir()
    feature = workspace / "feature.py"
    feature.write_text("one\ntwo\nthree\n", encoding="utf-8")
    keep = workspace / "keep.txt"
    keep.write_text("keep\n", encoding="utf-8")

    snapshot = capture_workspace_snapshot(
        workspace,
        recovery,
        conversation_id=7,
        generation_id=11,
    )
    assert snapshot is not None

    feature.unlink()
    keep.write_text("keep\nchanged\n", encoding="utf-8")
    (workspace / "new.txt").write_text("new\n", encoding="utf-8")
    report = finalize_workspace_snapshot(snapshot)

    assert report["changed_count"] == 3
    assert report["deleted_count"] == 1
    assert any("deleted" in warning for warning in report["warnings"])
    deleted = next(item for item in report["changed_files"] if item["path"] == "feature.py")
    assert deleted["status"] == "deleted"
    assert Path(deleted["recovery_blob"]).read_text(encoding="utf-8") == "one\ntwo\nthree\n"
    modified = next(item for item in report["changed_files"] if item["path"] == "keep.txt")
    assert Path(modified["after_recovery_blob"]).read_text(encoding="utf-8") == "keep\nchanged\n"
    assert Path(snapshot.manifest_path).with_name("change-report.json").is_file()


def test_completed_parallel_result_survives_a_later_overwrite(tmp_path):
    workspace = tmp_path / "workspace"
    recovery = tmp_path / "recovery"
    workspace.mkdir()
    target = workspace / "feature.py"
    target.write_text("baseline\n", encoding="utf-8")

    first = capture_workspace_snapshot(workspace, recovery, conversation_id=1, generation_id=1)
    second = capture_workspace_snapshot(workspace, recovery, conversation_id=1, generation_id=2)

    target.write_text("first agent feature\n", encoding="utf-8")
    first_report = finalize_workspace_snapshot(first)
    first_change = first_report["changed_files"][0]

    target.write_text("second agent stale version\n", encoding="utf-8")
    finalize_workspace_snapshot(second)

    assert target.read_text(encoding="utf-8") == "second agent stale version\n"
    assert Path(first_change["after_recovery_blob"]).read_text(encoding="utf-8") == "first agent feature\n"


def _manifest(snapshot):
    return json.loads(Path(snapshot.manifest_path).read_text(encoding="utf-8"))


def _capture(workspace, recovery, generation=1):
    return capture_workspace_snapshot(workspace, recovery, conversation_id=1, generation_id=generation)


def test_warm_snapshot_and_finalization_read_zero_unchanged_file_bytes(tmp_path, monkeypatch):
    workspace, recovery = tmp_path / "workspace", tmp_path / "recovery"
    workspace.mkdir()
    monkeypatch.setattr(safety, "_git_bytes", lambda *_: None)
    raw = b"exact\r\nworking-tree\r\nbytes\xff"
    (workspace / "feature.txt").write_bytes(raw)
    first = _capture(workspace, recovery)
    assert first.stats["files_read"] == 1
    assert first.stats["bytes_read"] == len(raw)
    # Persistence is in the external index, not an in-process contents cache.
    assert list((recovery / "indexes").glob("*.json"))
    def unexpected_read(path):
        pytest.fail(f"Unchanged file content was reread: {path.name}")
    monkeypatch.setattr(safety, "_read_file", unexpected_read)
    second = _capture(workspace, recovery, 2)
    assert second.stats["files_read"] == 0
    assert second.stats["bytes_read"] == 0
    assert second.stats["files_reused"] == 1
    assert second.stats["stored_bytes"] == 0
    report = finalize_workspace_snapshot(second)
    assert report["changed_count"] == 0
    assert report["snapshot_stats"]["bytes_read"] == 0
    assert Path(_manifest(second)["files"]["feature.txt"]["recovery_blob"]).read_bytes() == raw


def test_same_size_edit_with_restored_mtime_invalidates_cache(tmp_path):
    workspace, recovery = tmp_path / "workspace", tmp_path / "recovery"
    workspace.mkdir()
    path = workspace / "feature.txt"
    path.write_bytes(b"original")
    before_stat = path.stat()
    first = _capture(workspace, recovery)
    path.write_bytes(b"modified")
    os.utime(path, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
    assert path.stat().st_mtime_ns == before_stat.st_mtime_ns
    second = _capture(workspace, recovery, 2)
    assert second.stats["files_read"] == 1
    assert Path(_manifest(first)["files"]["feature.txt"]["recovery_blob"]).read_bytes() == b"original"
    assert Path(_manifest(second)["files"]["feature.txt"]["recovery_blob"]).read_bytes() == b"modified"


@pytest.mark.parametrize("damage", ["missing_blob", "truncated_blob", "corrupt_index", "missing_index"])
def test_cache_damage_falls_back_to_live_bytes(tmp_path, damage):
    workspace, recovery = tmp_path / "workspace", tmp_path / "recovery"
    workspace.mkdir()
    (workspace / "feature.txt").write_bytes(b"recover me")
    first = _capture(workspace, recovery)
    blob = Path(_manifest(first)["files"]["feature.txt"]["recovery_blob"])
    index = next((recovery / "indexes").glob("*.json"))
    if damage == "missing_blob":
        blob.unlink()
    elif damage == "truncated_blob":
        blob.write_bytes(b"bad")
    elif damage == "corrupt_index":
        index.write_text("not json", encoding="utf-8")
    else:
        index.unlink()
    second = _capture(workspace, recovery, 2)
    assert second.stats["files_read"] == 1
    assert blob.read_bytes() == b"recover me"


def test_missing_change_metadata_disables_reuse(tmp_path, monkeypatch):
    workspace, recovery = tmp_path / "workspace", tmp_path / "recovery"
    workspace.mkdir()
    (workspace / "feature.txt").write_bytes(b"data")
    monkeypatch.setattr(safety, "file_fingerprint", lambda _: None)
    _capture(workspace, recovery)
    second = _capture(workspace, recovery, 2)
    assert second.stats["files_read"] == 1
    assert second.stats["files_reused"] == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing and change-time contract")
def test_windows_change_stamp_detects_open_writer(tmp_path):
    path = tmp_path / "writer.txt"
    path.write_bytes(b"data")
    assert file_fingerprint(path) is not None
    with path.open("r+b") as writer:
        writer.write(b"edit")
        writer.flush()
        assert file_fingerprint(path) is None
    assert file_fingerprint(path) is not None


def test_oversize_and_over_budget_files_are_not_read(tmp_path, monkeypatch):
    workspace, recovery = tmp_path / "workspace", tmp_path / "recovery"
    workspace.mkdir()
    monkeypatch.setattr(safety, "_git_bytes", lambda *_: None)
    monkeypatch.setattr(safety, "MAX_RECOVERY_FILE_BYTES", 16)
    monkeypatch.setattr(safety, "MAX_RECOVERY_TOTAL_BYTES", 20)
    for name, data in {"a": b"a" * 8, "b": b"b" * 8, "c": b"c" * 8, "huge": b"x" * 32}.items():
        (workspace / name).write_bytes(data)
    original_read = safety._read_file
    reads = []
    def read(path):
        reads.append(path.name)
        return original_read(path)
    monkeypatch.setattr(safety, "_read_file", read)
    first = _capture(workspace, recovery)
    assert reads == ["a", "b"]
    assert {item["reason"] for item in _manifest(first)["skipped"]} == {"snapshot_cap", "file_too_large"}
    reads.clear()
    second = _capture(workspace, recovery, 2)
    assert reads == ["c"]  # Reused blobs do not consume the new-read budget.
    assert second.stats["files_reused"] == 2
    assert second.stats["protected_bytes"] == 24


def test_file_changing_during_read_is_reported_not_reused(tmp_path, monkeypatch):
    workspace, recovery = tmp_path / "workspace", tmp_path / "recovery"
    workspace.mkdir()
    path = workspace / "feature.txt"
    path.write_bytes(b"before")
    original_read = safety._read_file
    def racing_read(target):
        data = original_read(target)
        target.write_bytes(b"during")
        return data
    monkeypatch.setattr(safety, "_read_file", racing_read)
    snap = _capture(workspace, recovery)
    assert _manifest(snap)["skipped"][0]["reason"] == "changed_during_capture"
    assert _manifest(snap)["files"]["feature.txt"]["sha256"] is None


def test_unreadable_after_file_is_not_reported_deleted(tmp_path, monkeypatch):
    workspace, recovery = tmp_path / "workspace", tmp_path / "recovery"
    workspace.mkdir()
    path = workspace / "feature.txt"
    path.write_bytes(b"before")
    snap = _capture(workspace, recovery)
    path.write_bytes(b"after")
    original_read = safety._read_file
    monkeypatch.setattr(safety, "_read_file", lambda p: None if p == path else original_read(p))
    report = finalize_workspace_snapshot(snap)
    assert report["deleted_count"] == 0
    assert report["unverified_count"] == 1
    assert report["changed_count"] == 0
    assert report["changed_files"] == []
    assert any("partial" in warning for warning in report["warnings"])


def test_file_vanishing_after_enumeration_is_not_a_created_file(tmp_path, monkeypatch):
    workspace, recovery = tmp_path / "workspace", tmp_path / "recovery"
    workspace.mkdir()
    snap = _capture(workspace, recovery)
    monkeypatch.setattr(safety, "_workspace_paths", lambda _: ({"vanished.txt"}, set(), False, None))
    assert finalize_workspace_snapshot(snap)["changed_count"] == 0


def test_full_after_manifest_survives_report_truncation(tmp_path, monkeypatch):
    workspace, recovery = tmp_path / "workspace", tmp_path / "recovery"
    workspace.mkdir()
    for name in ("a", "b", "c"):
        (workspace / name).write_bytes(b"before")
    snap = _capture(workspace, recovery)
    for name in ("a", "b", "c"):
        (workspace / name).write_bytes(name.encode())
    monkeypatch.setattr(safety, "MAX_REPORTED_FILES", 1)
    report = finalize_workspace_snapshot(snap)
    assert report["truncated"] and report["changed_count"] == 3
    after = json.loads(Path(report["after_manifest_path"]).read_text(encoding="utf-8"))
    for name, item in after["files"].items():
        assert Path(item["recovery_blob"]).read_bytes() == name.encode()


def test_v1_manifest_still_finalizes(tmp_path):
    workspace, recovery = tmp_path / "workspace", tmp_path / "recovery"
    workspace.mkdir()
    path = workspace / "feature.txt"
    path.write_bytes(b"before")
    snap = _capture(workspace, recovery)
    manifest = _manifest(snap)
    manifest["version"] = 1
    manifest.pop("stats")
    for entry in manifest["files"].values():
        entry.pop("fingerprint")
    Path(snap.manifest_path).write_text(json.dumps(manifest), encoding="utf-8")
    path.write_bytes(b"after")
    report = finalize_workspace_snapshot(snap)
    assert report["modified_count"] == 1
    assert Path(report["changed_files"][0]["recovery_blob"]).read_bytes() == b"before"


def test_recovery_directory_cannot_snapshot_itself(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        _capture(tmp_path, tmp_path / "recovery")


def test_git_dirty_staged_untracked_and_commit_keep_exact_live_bytes(tmp_path):
    workspace, recovery = tmp_path / "repo", tmp_path / "recovery"
    workspace.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Fixture",
            "-c", "user.email=fixture@example.invalid", *args], check=True, capture_output=True).stdout
    git("init", "-q")
    for name in ("clean.txt", "dirty.txt", "staged.txt", "deleted.txt"):
        (workspace / name).write_bytes(b"original\r\n")
    git("add", "--", "clean.txt", "dirty.txt", "staged.txt", "deleted.txt")
    git("commit", "-qm", "fixture baseline")
    (workspace / "dirty.txt").write_bytes(b"live dirty\r\n")
    (workspace / "staged.txt").write_bytes(b"staged\r\n")
    git("add", "--", "staged.txt")
    (workspace / "staged.txt").write_bytes(b"unstaged on top\r\n")
    (workspace / "untracked.txt").write_bytes(b"not in git\r\n")
    (workspace / "deleted.txt").unlink()
    before_index = hashlib.sha256((workspace / ".git/index").read_bytes()).hexdigest()
    snap = _capture(workspace, recovery)
    entries = _manifest(snap)["files"]
    assert "deleted.txt" not in entries
    for name in ("dirty.txt", "staged.txt", "untracked.txt"):
        assert entries[name]["dirty_before"]
    for name, entry in entries.items():
        assert Path(entry["recovery_blob"]).read_bytes() == (workspace / name).read_bytes()
    assert hashlib.sha256((workspace / ".git/index").read_bytes()).hexdigest() == before_index
    git("add", "--", "dirty.txt", "staged.txt", "untracked.txt", "deleted.txt")
    git("commit", "-qm", "completed feature")
    second = _capture(workspace, recovery, 2)
    assert not any(entry["dirty_before"] for entry in _manifest(second)["files"].values())
    assert any("HEAD changed" in warning for warning in finalize_workspace_snapshot(snap)["warnings"])


def test_incremental_io_benchmark(tmp_path, monkeypatch, record_property):
    workspace, recovery = tmp_path / "workspace", tmp_path / "recovery"
    workspace.mkdir()
    monkeypatch.setattr(safety, "_git_bytes", lambda *_: None)
    for index in range(128):
        (workspace / f"file-{index:03}.bin").write_bytes(bytes([index]) * (256 * 1024))
    start = time.perf_counter()
    cold = _capture(workspace, recovery)
    cold_seconds = time.perf_counter() - start
    start = time.perf_counter()
    warm = _capture(workspace, recovery, 2)
    warm_seconds = time.perf_counter() - start
    assert cold.stats["bytes_read"] == 32 * 1024 * 1024
    assert warm.stats["bytes_read"] == 0
    assert warm.stats["files_reused"] == 128
    from tests.lifecycle_metrics import observe
    observe(record_property, "snapshot_io", cold_ms=cold_seconds * 1000, warm_ms=warm_seconds * 1000,
            cold_bytes_read=cold.stats["bytes_read"], warm_bytes_read=warm.stats["bytes_read"], files_reused=128)
    print(f"Recovery benchmark: cold={cold_seconds:.3f}s, warm={warm_seconds:.3f}s; "
          f"workspace bytes read: {cold.stats['bytes_read']} -> {warm.stats['bytes_read']}")


def test_concurrent_snapshots_keep_independent_manifests(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    workspace, recovery = tmp_path / "workspace", tmp_path / "recovery"
    workspace.mkdir()
    (workspace / "feature.txt").write_bytes(b"shared baseline")
    monkeypatch.setattr(safety, "_git_bytes", lambda *_: None)
    barrier = Barrier(2)
    original_load = safety._load_index
    def load(*args):
        value = original_load(*args)
        barrier.wait(timeout=10)
        return value
    monkeypatch.setattr(safety, "_load_index", load)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_capture, workspace, recovery, gen) for gen in (1, 2)]
        snapshots = [future.result(timeout=20) for future in futures]
    assert snapshots[0].manifest_path != snapshots[1].manifest_path
    for snapshot in snapshots:
        entry = _manifest(snapshot)["files"]["feature.txt"]
        assert Path(entry["recovery_blob"]).read_bytes() == b"shared baseline"
    assert len(list((recovery / "snapshots/conversation-1").glob("*/manifest.json"))) == 2


@pytest.mark.parametrize("winner", [b"baseline", b"corrupt!", None])
def test_blob_publish_race_requires_identical_winner(tmp_path, monkeypatch, winner):
    data = b"baseline"
    digest = hashlib.sha256(data).hexdigest()

    def competing_publish(source, destination):
        if winner is not None:
            destination.write_bytes(winner)
        raise PermissionError("Simulated Windows competing rename")

    monkeypatch.setattr(safety.os, "replace", competing_publish)
    if winner == data:
        blob = safety._store_blob(tmp_path, digest, data)
        assert Path(blob).read_bytes() == data
    else:
        with pytest.raises(PermissionError, match="competing rename"):
            safety._store_blob(tmp_path, digest, data)


def test_index_publish_failure_preserves_snapshot_and_cleans_temp(tmp_path, monkeypatch):
    workspace, recovery = tmp_path / "workspace", tmp_path / "recovery"
    workspace.mkdir()
    (workspace / "feature.txt").write_bytes(b"recoverable")
    replace = safety.os.replace

    def blocked_index(source, destination):
        if destination.parent.name == "indexes":
            raise PermissionError("Index hint is temporarily unavailable")
        return replace(source, destination)

    monkeypatch.setattr(safety.os, "replace", blocked_index)
    first = _capture(workspace, recovery)
    second = _capture(workspace, recovery, 2)
    for snapshot in (first, second):
        entry = _manifest(snapshot)["files"]["feature.txt"]
        assert Path(entry["recovery_blob"]).read_bytes() == b"recoverable"
    assert second.stats["files_read"] == 1
    assert list((recovery / "indexes").iterdir()) == []


def test_stale_index_cannot_hide_a_newer_file_version(tmp_path):
    workspace, recovery = tmp_path / "workspace", tmp_path / "recovery"
    workspace.mkdir()
    path = workspace / "feature.txt"
    path.write_bytes(b"version one")
    first = _capture(workspace, recovery)
    index = next((recovery / "indexes").glob("*.json"))
    old_index = index.read_bytes()
    path.write_bytes(b"version two")
    second = _capture(workspace, recovery, 2)
    index.write_bytes(old_index)  # Late writer publishing an older cache hint.
    third = _capture(workspace, recovery, 3)
    assert third.stats["files_read"] == 1
    for snapshot, expected in ((first, b"version one"), (second, b"version two"), (third, b"version two")):
        assert Path(_manifest(snapshot)["files"]["feature.txt"]["recovery_blob"]).read_bytes() == expected


@pytest.mark.parametrize("stats,expected", [
    (None, "recovery unavailable"),
    ({"files_reused": 4, "skipped_count": 0}, "reused 4 files"),
    ({"files_reused": 4, "skipped_count": 2}, "recovery partial (2 files skipped"),
])
async def test_launch_status_reports_actual_recovery_coverage(tmp_path, monkeypatch, stats, expected):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import database as db
    import server
    conv = await db.create_conversation("Snapshot lifecycle fixture", mode="codex", project_dir=str(tmp_path))
    snapshot = SimpleNamespace(stats=stats) if stats is not None else None
    monkeypatch.setattr(server, "capture_workspace_snapshot", lambda *a, **k: snapshot)
    monkeypatch.setattr(server, "finalize_workspace_snapshot", lambda _: {"changed_count": 0, "warnings": []})
    generation = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(server, "_handle_generation", generation)
    monkeypatch.setattr(server, "_ws_send", send)
    await server._handle_generation_with_workspace_safety(None, conv["id"], {})
    generation.assert_awaited_once()
    events = [call.args[1] for call in send.await_args_list]
    assert events[0]["phase"] == "workspace_snapshot"
    assert events[1]["phase"] == "launching"
    assert expected in events[1]["text"]
    assert events[1]["recovery_stats"] == (stats or {})
