"""Sanitized reproductions; never access a real Canvas, key, or provider."""
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

import database as db
import mcp_loom_workspace as broker
import server
import workspace_integrity as integrity
import workspace_safety as safety
from cc_permission_hook import normalize_tool_args
from tests.test_permission_workflows import HARNESSES, wire


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setenv("LOOM_WORKSPACE_RECOVERY_DIR", str(tmp_path / "recovery"))
    monkeypatch.setenv("LOOM_WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("LOOM_CONV_ID", "1")
    return root


def check(root, name="Write", args=None):
    return integrity.prepare_integrity_check(name, args or {"path": "canvas/index.html", "content": "unrelated prototype"},
                                             workspace=str(root), conversation_id=1, scope="gen:1")


def seed(root):
    (root / "canvas").mkdir()
    path = root / "canvas/index.html"
    path.write_bytes(b"existing tool\r\nprivate fixture data\r\n")
    return path


def test_ignored_canvas_replacement_has_exact_preimage_and_review(workspace):
    path = seed(workspace)
    (path.parent / ".gitignore").write_text("*\n", encoding="utf-8")
    before = path.read_bytes()
    result = check(workspace)
    assert not result.error
    assert "Replace existing file canvas/index.html" in result.review_reason
    assert "prior agent read is not verified" in result.review_reason
    entry = json.loads(Path(result.manifest_path).read_text())["files"][0]
    assert Path(entry["recovery_blob"]).read_bytes() == before
    assert entry["sha256"] == hashlib.sha256(before).hexdigest()
    assert path.read_bytes() == before
    path.write_text("overwritten after explicit approval", encoding="utf-8")
    assert Path(entry["recovery_blob"]).read_bytes() == before


def test_other_ignored_folder_is_journaled_without_git_inventory(workspace):
    target = workspace / ".local" / "tool.txt"
    target.parent.mkdir()
    target.write_text("working utility", encoding="utf-8")
    result = check(workspace, args={"path": ".local/tool.txt", "content": "replacement"})
    assert result.entries[0]["recovery_blob"]
    assert not result.error


def test_new_file_and_identical_write_do_not_add_a_prompt(workspace):
    assert not check(workspace).review_reason
    path = seed(workspace)
    assert not check(workspace, args={"path": str(path), "content": path.read_bytes().decode()}).review_reason


def test_targeted_edit_preserves_before_bytes_without_new_prompt(workspace):
    seed(workspace)
    result = check(workspace, "Edit", {"path": "canvas/index.html", "before": "existing", "after": "improved"})
    assert not result.review_reason and not result.error
    assert result.entries[0]["recovery_blob"]


def test_edit_cannot_disguise_a_small_whole_file_replacement(workspace):
    path = seed(workspace)
    result = check(workspace, "Edit", {"path": "canvas/index.html", "before": path.read_bytes().decode(), "after": "unrelated tool"})
    assert "entire contents" in result.review_reason


@pytest.mark.parametrize("name,args", [
    ("Edit", {"path": "canvas/index.html", "old_string": "line\n" * 26, "new_string": "replacement"}),
    ("delete_file", {"path": "canvas/index.html"}),
    ("apply_patch", {"command": "*** Begin Patch\n*** Add File: canvas/index.html\n+replacement\n*** End Patch"}),
    ("apply_patch", {"patch": "*** Begin Patch\n*** Delete File: canvas/index.html\n*** End Patch"}),
    ("Edit", {"changes": {"canvas/index.html": {"type": "add"}}}),
    ("Edit", {"changes": [{"path": "canvas/index.html", "kind": {"type": "delete"}}]}),
    ("Edit", {"fileChanges": [{"path": "canvas/index.html", "kind": {"type": "add"}}]}),
])
def test_equivalent_destructive_shapes_require_review(workspace, name, args):
    seed(workspace)
    result = check(workspace, name, args)
    assert result.review_reason and not result.error


def test_rename_cannot_hide_existing_destination(workspace):
    seed(workspace)
    result = check(workspace, "Edit", {"changes": {"source.txt": {"kind": {"type": "update", "move_path": "canvas/index.html"}}}})
    assert "Replace existing file canvas/index.html" in result.review_reason


def test_stale_approval_and_create_collision_are_rejected(workspace):
    path = seed(workspace)
    result = check(workspace)
    path.write_bytes(b"another agent's useful work")
    assert "changed while approval was pending" in integrity.revalidate_integrity(result)
    fresh = check(workspace, args={"path": "new.txt", "content": "draft"})
    (workspace / "new.txt").write_text("arrived while waiting", encoding="utf-8")
    assert integrity.revalidate_integrity(fresh)


def test_symlink_cannot_be_used_as_an_overwrite_destination(workspace, monkeypatch):
    real = workspace / "real.txt"
    real.write_text("preserve", encoding="utf-8")
    # Deterministic metadata boundary; Windows symlink creation can require
    # privileges that are intentionally absent from the preflight sandbox.
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p == workspace / "link.txt" or original(p))
    result = check(workspace, args={"path": "link.txt", "content": "erase"})
    assert "symlink" in result.error
    assert real.read_text() == "preserve"


def test_unified_diff_multiple_files_and_header_like_content(workspace):
    first = workspace / "a.txt"
    second = workspace / "b.txt"
    first.write_text("-- not a path\n", encoding="utf-8")
    second.write_text("important\n", encoding="utf-8")
    patch = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n--- not a path\n+replacement\n--- a/b.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-important\n"
    targets = integrity.patch_mutations(patch)
    assert [(t.path, t.kind) for t in targets] == [("a.txt", "edit"), ("b.txt", "delete")]
    result = check(workspace, "apply_patch", {"patch": patch})
    assert "Delete existing file b.txt" in result.review_reason
    assert len(result.entries) == 2


def test_corrupt_same_size_blob_is_not_trusted(workspace, monkeypatch):
    path = seed(workspace)
    blob = workspace.parent / "bad-blob"
    blob.write_bytes(b"x" * len(path.read_bytes()))
    monkeypatch.setattr(integrity, "_store_blob", lambda *args: str(blob))
    assert "verification" in check(workspace).error


@pytest.mark.parametrize("tail", [
    "diff --git a/secret b/dest\nsimilarity index 100%\nrename from secret\nrename to dest\n",
    "diff --git a/data b/data\nGIT binary patch\n",
    "diff --git a/tool b/tool\nold mode 100644\nnew mode 100755\n",
    "diff --git a/link b/link\nnew file mode 120000\n--- /dev/null\n+++ b/link\n@@ -0,0 +1 @@\n+../outside\n",
])
def test_broker_cannot_ignore_unsupported_targets_in_mixed_patch(workspace, monkeypatch, tail):
    (workspace / "ok.txt").write_text("old\n", encoding="utf-8")
    patch = "diff --git a/ok.txt b/ok.txt\n--- a/ok.txt\n+++ b/ok.txt\n@@ -1 +1 @@\n-old\n+new\n" + tail
    def unexpected(*args, **kwargs):
        pytest.fail("Unsupported target reached approval or execution")
    monkeypatch.setattr(broker, "_ask_permission", unexpected)
    monkeypatch.setattr(broker.subprocess, "run", unexpected)
    assert broker.apply_unified_patch(patch).startswith("Error inspecting patch targets")
    assert (workspace / "ok.txt").read_text() == "old\n"


def test_small_whole_file_patch_requires_review(workspace):
    (workspace / "one.txt").write_text("important\n", encoding="utf-8")
    result = check(workspace, "apply_patch", {"patch": "--- a/one.txt\n+++ b/one.txt\n@@ -1 +1 @@\n-important\n+replacement\n"})
    assert result.review_reason and result.entries[0]["recovery_blob"]


def test_opaque_patch_fails_with_system_explanation(workspace):
    result = check(workspace, "apply_patch", {"patch": "not supported diff syntax"})
    assert "no supported file headers" in result.error


def test_cwd_alias_points_journal_at_actual_nested_target(workspace):
    seed(workspace)
    result = check(workspace, args={"path": "index.html", "workdir": "canvas", "content": "replace"})
    assert result.entries[0]["path"] == str(workspace / "canvas/index.html")
    assert result.review_reason


def test_recovery_failure_never_auto_authorizes(workspace, monkeypatch):
    path = seed(workspace)
    def fail(*args):
        raise OSError("fixture disk full")
    monkeypatch.setattr(integrity, "_store_blob", fail)
    result = check(workspace)
    assert "No write was authorized" in result.error
    assert path.read_bytes().startswith(b"existing tool")


def test_recovery_root_may_not_be_inside_target_workspace(workspace):
    seed(workspace)
    result = integrity.prepare_integrity_check("Write", {"path": "canvas/index.html"}, workspace=str(workspace), recovery_root=workspace / "archive")
    assert "outside the workspace" in result.error


def test_arbitrary_shell_is_not_misrepresented_as_a_decoded_file_write(workspace):
    result = check(workspace, "Bash", {"command": "python opaque-program.py"})
    assert not result.entries and not result.review_reason


@pytest.mark.parametrize("tool", ["write", "edit", "patch"])
def test_broker_rejects_change_during_permission_wait(workspace, monkeypatch, tool):
    path = seed(workspace)
    def approve(*args):
        path.write_bytes(b"new work while waiting\r\n")
        return None
    monkeypatch.setattr(broker, "_ask_permission", approve)
    if tool == "write":
        result = broker.write_file("canvas/index.html", "replacement")
    elif tool == "edit":
        result = broker.replace_in_file("canvas/index.html", "existing", "replaced")
    else:
        result = broker.apply_unified_patch("--- a/canvas/index.html\n+++ b/canvas/index.html\n@@ -1 +1 @@\n-existing tool\n+replacement\n")
    assert "changed while approval was pending" in result
    assert path.read_bytes() == b"new work while waiting\r\n"


def test_broker_preserves_crlf_and_unrelated_content(workspace, monkeypatch):
    path = seed(workspace)
    monkeypatch.setattr(broker, "_ask_permission", lambda *args: None)
    assert broker.replace_in_file("canvas/index.html", "existing", "improved").startswith("Updated")
    assert path.read_bytes() == b"improved tool\r\nprivate fixture data\r\n"


def test_broker_preserves_mode_and_does_not_truncate_on_publication_failure(workspace, monkeypatch):
    path = seed(workspace)
    path.chmod(0o755)
    mode = path.stat().st_mode
    monkeypatch.setattr(broker, "_ask_permission", lambda *args: None)
    assert broker.replace_in_file("canvas/index.html", "existing", "improved").startswith("Updated")
    assert path.stat().st_mode == mode
    before = path.read_bytes()
    def fail(*args):
        raise OSError("fixture publication failure")
    monkeypatch.setattr(broker.os, "replace", fail)
    assert broker.replace_in_file("canvas/index.html", "improved", "new").startswith("Error writing")
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".loom-edit-*"))


def test_hook_preserves_quoted_content_and_edit_matching_strings():
    write = normalize_tool_args("write_to_file", {"TargetFile": '"file.json"', "CodeContent": '"value"'})
    assert write["file_path"] == "file.json" and write["content"] == '"value"'
    edit = normalize_tool_args("replace_file_content", {"TargetFile": "file.json", "TargetContent": '"old"', "ReplacementContent": '"new"'})
    assert edit["old_string"] == '"old"' and edit["new_string"] == '"new"'


def test_broker_create_does_not_overwrite_a_file_created_while_waiting(workspace, monkeypatch):
    def approve(*args):
        (workspace / "new.txt").write_text("someone else's work", encoding="utf-8")
    monkeypatch.setattr(broker, "_ask_permission", approve)
    assert "changed while approval was pending" in broker.write_file("new.txt", "replacement")
    assert (workspace / "new.txt").read_text() == "someone else's work"


def test_snapshot_covers_canvas_even_when_git_ignores_every_file(workspace):
    path = seed(workspace)
    (path.parent / ".gitignore").write_text("*\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(workspace)], check=True, capture_output=True)
    snapshot = safety.capture_workspace_snapshot(workspace, workspace.parent / "recovery", conversation_id=1, generation_id=1)
    manifest = json.loads(Path(snapshot.manifest_path).read_text())
    assert "canvas/index.html" in manifest["files"]
    before = path.read_bytes()
    path.write_text("unrelated page", encoding="utf-8")
    report = safety.finalize_workspace_snapshot(snapshot)
    entry = next(c for c in report["changed_files"] if c["path"] == "canvas/index.html")
    assert Path(entry["recovery_blob"]).read_bytes() == before
    assert entry["status"] == "modified"


def test_canvas_snapshot_limit_is_reported(workspace, monkeypatch):
    seed(workspace)
    (workspace / "canvas/extra.txt").write_text("extra", encoding="utf-8")
    monkeypatch.setattr(safety, "MAX_CANVAS_ENTRIES", 0)
    paths, skipped = safety._canvas_paths(workspace)
    assert "canvas/index.html" in paths
    assert skipped[-1]["reason"] == "canvas_entry_limit"


def test_canvas_assets_do_not_displace_dirty_source_at_snapshot_cap(workspace, monkeypatch):
    seed(workspace)
    (workspace / "canvas/index.html").write_bytes(b"entry")
    (workspace / "canvas/asset.txt").write_bytes(b"asset")
    (workspace / "source.py").write_bytes(b"dirty")
    monkeypatch.setattr(safety, "MAX_RECOVERY_TOTAL_BYTES", 10)
    files, skipped, _ = safety._collect_files(workspace,
        {"canvas/index.html", "canvas/asset.txt", "source.py"}, {"source.py"}, workspace.parent / "blobs", {})
    assert files["canvas/index.html"]["recovery_blob"]
    assert files["source.py"]["recovery_blob"]
    assert {row["path"] for row in skipped} == {"canvas/asset.txt"}


@pytest.mark.parametrize("harness", HARNESSES)
@pytest.mark.parametrize("profile", ["balanced", "trusted", "trust_review"])
async def test_native_normalizers_require_one_time_overwrite_review(harness, profile, workspace, monkeypatch):
    path = seed(workspace)
    before = path.read_bytes()
    monkeypatch.setattr(server, "_pending_hook_permissions", {})
    monkeypatch.setattr(server, "_permission_rules", {})
    conv = await db.create_conversation("Preservation fixture", mode="goose", project_dir=str(workspace))
    await db.update_conversation_fields(conv["id"], permission_profile=profile)
    payload = {**wire(harness, "write", workspace, path="canvas/index.html"), "loom_conv_id": conv["id"]}
    task = asyncio.create_task(server.handle_cc_permission(payload))
    try:
        async with asyncio.timeout(5):
            while not server._pending_hook_permissions and not task.done():
                await asyncio.sleep(.01)
            assert server._pending_hook_permissions, "Existing-file overwrite was silently authorized"
            rid, pending = next(iter(server._pending_hook_permissions.items()))
            assert pending["supports_allow_all"] is False
            audit = (await db.get_permission_policy_audits(conv["id"]))[-1]["action"]
            assert audit["effective_decision"] == "ask_with_warning" and audit["integrity_review"]
            assert "Replace existing file" in pending["risk_reason"]
            assert Path(pending["integrity_check"].entries[0]["recovery_blob"]).read_bytes() == before
            assert await server._answer_pending_permission(rid, {"allow": True, "always": True})
            response = await task
        assert response["allow"] and not response["always"]
        assert not server._permission_rules
        assert path.read_bytes() == before, "Approval itself must not perform the write"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_changed_file_invalidates_human_approval_without_blame(workspace, monkeypatch):
    path = seed(workspace)
    monkeypatch.setattr(server, "_pending_hook_permissions", {})
    conv = await db.create_conversation("Approval race", mode="goose", project_dir=str(workspace))
    task = asyncio.create_task(server.handle_cc_permission({"loom_conv_id": conv["id"], "tool_name": "Write",
        "tool_input": {"path": str(path), "content": "replace"}}))
    try:
        async with asyncio.timeout(5):
            while not server._pending_hook_permissions and not task.done():
                await asyncio.sleep(.01)
            rid = next(iter(server._pending_hook_permissions))
            path.write_text("new valuable work", encoding="utf-8")
            await server._answer_pending_permission(rid, {"allow": True})
            response = await task
        assert not response["allow"] and response["decision_source"] == "loom_policy"
        assert "changed" in response["message"] and "user" not in response["message"].lower()
        assert path.read_text() == "new valuable work"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_remembered_write_rule_cannot_bypass_overwrite_review(workspace, monkeypatch):
    path = seed(workspace)
    monkeypatch.setattr(server, "_pending_hook_permissions", {})
    monkeypatch.setattr(server, "_permission_rules", {})
    monkeypatch.setattr(server, "_generation_snapshots", {})
    conv = await db.create_conversation("Remembered preservation", mode="goose", project_dir=str(workspace))
    await db.update_conversation_fields(conv["id"], permission_profile="strict")
    user = await db.add_message(conv["id"], "user", "fixture")
    server._generation_snapshots[(conv["id"], user["id"], 101)] = {"parent_id": user["id"]}
    for target, remember in [("new.txt", True), ("canvas/index.html", False)]:
        payload = {**wire("goose_acp", "write", workspace, path=target), "loom_conv_id": conv["id"], "permission_scope": "gen:101"}
        task = asyncio.create_task(server.handle_cc_permission(payload))
        try:
            async with asyncio.timeout(5):
                while not server._pending_hook_permissions and not task.done():
                    await asyncio.sleep(.01)
                rid, pending = next(iter(server._pending_hook_permissions.items()))
                assert pending["supports_allow_all"] is remember
                if not remember:
                    assert pending["policy"]["action"]["rule_matched"] is True
                    assert pending["policy"]["effective_decision"] == "ask_with_warning"
                await server._answer_pending_permission(rid, {"allow": True, "always": True})
                assert (await task)["allow"]
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert len(server._permission_rules) == 1
    assert path.read_bytes().startswith(b"existing tool")
