"""Harness-independent Canvas bootstrap, orientation, and bounded change detection."""

import hashlib
import json
import os
from html import escape
from pathlib import Path


def canvas_root(project_dir: str) -> Path:
    workspace = Path(project_dir).resolve()
    root = (workspace / "canvas").resolve()
    if root == workspace or not root.is_relative_to(workspace):
        raise ValueError("Canvas directory must stay inside the workspace")
    return root


def ensure_canvas_workspace(project_dir: str, instructions: str) -> Path:
    """Create missing defaults only, including for previously enabled workspaces."""
    root = canvas_root(project_dir)
    root.mkdir(parents=True, exist_ok=True)
    defaults = {
        ".gitignore": "# Generated Canvas files; keep local.\n*\n!.gitignore\n",
        "index.html": '<!doctype html><html><head><meta charset="utf-8"><title>Canvas</title>'
        '</head><body><h1>Canvas ready</h1></body></html>\n',
        "CLAUDE.md": instructions,
        "CANVAS_GUIDE.md": Path(__file__).with_name("CANVAS_GUIDE.md").read_text(encoding="utf-8"),
    }
    for name, content in defaults.items():
        try:
            # Exclusive create also preserves another agent's concurrent first write.
            with (root / name).open("x", encoding="utf-8") as handle:
                handle.write(content)
        except FileExistsError:
            pass
    return root


def add_canvas_notice(prompt: str, conv: dict) -> str:
    if not conv.get("canvas_enabled") or not conv.get("project_dir"):
        return prompt
    if conv.get('mode') == 'backstage':
        return prompt
    root = canvas_root(conv["project_dir"])
    notice = (
        '<loom_canvas>\n'
        f'Canvas is enabled. Entry: {escape(str(root / "index.html"))}. '
        f'Guide: {escape(str(root / "CANVAS_GUIDE.md"))}. '
        'Read the guide when working on Canvas; edit these files and Loom displays them. '
        'No separate app server is needed. Canvas is persistent shared user work, even when gitignored. '
        'Read the existing entry page and imports before editing; never replace unrelated tools with a new task. '
        'Put unrelated prototypes on separate pages. Preserve existing files and respect your assigned workspace scope.\n'
        '</loom_canvas>'
    )
    return f"{notice}\n\n{prompt}"


def canvas_revision(project_dir: str, max_entries: int = 2048) -> dict:
    """Metadata only: bounded traversal, no symlink/junction following or file reads.

    Incomplete scans are explicit; the browser must not claim live refresh for them.
    """
    root = canvas_root(project_dir)
    rows = []
    pending = [root]
    complete = True
    count = 0
    try:
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    count += 1
                    if count > max_entries:
                        return {"revision": None, "complete": False}
                    if entry.is_symlink() or Path(entry.path).is_junction():
                        continue
                    relative = str(Path(entry.path).relative_to(root))
                    if entry.is_dir(follow_symlinks=False):
                        rows.append((relative, 0, 0))
                        pending.append(Path(entry.path))
                    else:
                        stat = entry.stat(follow_symlinks=False)
                        # Directory bookkeeping/change times are not authored edits.
                        # This is a display refresh hint, not a recovery fingerprint.
                        rows.append((relative, stat.st_mtime_ns, stat.st_size))
    except OSError:
        complete = False
    revision = hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest() if complete else None
    return {"revision": revision, "complete": complete}
