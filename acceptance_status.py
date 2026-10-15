"""Lifecycle-acceptance identity and published-result status.

The verifier writes outside the checkout. Main reports the source identity it
loaded at process start. Admin can therefore distinguish a passing test report
from a stale checkout or a stale running process.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Mapping

import exo_state


ROOT = Path(__file__).resolve().parent
REPORT_PATH = exo_state.state_path("acceptance", "lifecycle-report.json")
_WINDOWS_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0


def source_identity(root: Path = ROOT) -> dict[str, str]:
    """Hash the checkout inputs covered by lifecycle acceptance."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True,
        timeout=10,
        creationflags=_WINDOWS_NO_WINDOW,
    )
    names = set(tracked.stdout.decode("utf-8").split("\0")) | {
        "acceptance_status.py", "tests/test_acceptance_status.py",
        "tools/lifecycle_preflight.py", "tests/lifecycle_metrics.py",
        "tests/lifecycle_isolation.py", "tests/test_lifecycle_preflight.py",
        "tests/test_lifecycle_handoff_browser.py",
        "tests/test_native_child_lifecycle.py",
        "tests/test_browser_resource_baseline.py",
        "tests/test_native_delegation.py", "coordination.py",
        "static/coordination.js", "tests/test_coordination.py",
        "workspace_integrity.py", "tests/test_workspace_integrity.py",
        "WORKSPACE_INTEGRITY.md", "LIFECYCLE_PREFLIGHT.md",
        ".github/workflows/permission-policy.yml",
        ".github/workflows/coordination.yml",
    }
    # Formal policy/coordination/portable-context sources and generated case
    # fixtures are acceptance inputs, not documentation.  A changed Lean model
    # or JSON oracle must make an old report stale even before it is committed.
    formal_root = root / "formal"
    if formal_root.is_dir():
        for path in formal_root.rglob("*"):
            if path.is_file() and ".lake" not in path.parts:
                names.add(path.relative_to(root).as_posix())
    files = sorted(
        root / name for name in names
        if name and (
            Path(name).suffix in {".py", ".js", ".html", ".css", ".md", ".yml", ".lean", ".json"}
            or name == "model_catalog.json"
            or name.startswith("formal/")
        )
    )
    digest = hashlib.sha256()
    for path in files:
        if 'synthetic' in path.parts:
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes() if path.is_file() else b"<deleted>")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
        text=True, timeout=5,
        creationflags=_WINDOWS_NO_WINDOW,
    )
    return {
        "commit": head.stdout.strip(),
        "tested_source_sha256": digest.hexdigest(),
    }


def publish_passing_report(report: Mapping, destination: Path = REPORT_PATH) -> Path:
    """Atomically publish only a complete passing preflight report."""
    if report.get("passed") is not True or report.get("source_unchanged") is not True:
        raise ValueError("Only a passing, source-stable lifecycle report may be published")
    source = report.get("source")
    if not isinstance(source, Mapping) or not source.get("commit") or not source.get("tested_source_sha256"):
        raise ValueError("Lifecycle report has no tested source identity")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(dict(report), indent=2), encoding="utf-8")
    os.replace(temporary, destination)
    return destination


def read_published_report(path: Path = REPORT_PATH) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def evaluate_acceptance(report: Mapping | None, checkout_identity: Mapping | None,
                        runtime_identity: Mapping | None) -> dict:
    """Return a fail-closed state suitable for the Admin UI."""
    tested = report.get("source") if isinstance(report, Mapping) else None
    result = {
        "state": "unverified", "passed": False,
        "tested_source": dict(tested) if isinstance(tested, Mapping) else None,
        "checkout_source": dict(checkout_identity) if isinstance(checkout_identity, Mapping) else None,
        "runtime_source": dict(runtime_identity) if isinstance(runtime_identity, Mapping) else None,
    }
    if not isinstance(report, Mapping):
        result["reason"] = "No published lifecycle acceptance report"
        return result
    if report.get("passed") is not True or report.get("source_unchanged") is not True:
        result.update(state="failed", reason="Published report is not a passing source-stable run")
        return result
    if tested != checkout_identity:
        result.update(state="stale", reason="Checkout differs from the accepted source")
        return result
    if runtime_identity is None:
        result["reason"] = "Running Main did not report a build identity"
        return result
    if tested != runtime_identity:
        result.update(state="stale", reason="Running Main differs from the accepted source")
        return result
    result.update(state="verified", passed=True, reason="Running Main matches the accepted source")
    return result
