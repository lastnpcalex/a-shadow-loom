"""Isolated permission acceptance runner. No live DB, host URL or restart option.

Default: offline policy/workflow/browser/transport tests. --native adds paid,
authenticated native-provider tests. Direct Anthropic subscription is excluded.
Artifacts default to a NEW OS-temp directory, never a production workspace.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
OFFLINE = [f"tests/{name}.py" for name in (
    "test_permission_workflows", "test_permission_preflight", "test_permission_policy", "test_permission_payloads",
    "test_permission_enforcement", "test_permission_literal_reads", "test_permission_ui", "test_permission_trust_review",
    "test_permission_transport_acceptance", "test_admin_supervision", "test_admin_supervision_http",
    "test_workspace_integrity",
)]
NATIVE = {
    "goose": "tests/test_live_permission_acceptance.py::test_native_goose_balanced_grant_and_denial",
    "codex": "tests/test_live_permission_acceptance.py::test_native_codex_browser_grant_reused",
    "claude_openrouter": "tests/test_native_harness_permissions.py::test_native_model_permission_contract[claude_openrouter]",
    "antigravity": "tests/test_native_harness_permissions.py::test_native_model_permission_contract[antigravity]",
}
HARNESS_IDS = {"goose_acp", "codex_app_server", "claude_code", "antigravity"}
SCENARIOS = {
    "workspace_read", "workspace_write", "workspace_edit", "initial_command_restored_card",
    "queued_descendant_similar_command", "child_inherits_branch_rule", "changed_command",
    "sibling_does_not_inherit", "outside_read", "outside_write", "unknown_tool", "child_other_workspace",
    "cancelled_waiter_not_user_denial", "revoked_rule_asks", "new_server_session_forgets_rule",
    "trusted_command", "trusted_unknown_still_asks", "strict_write", "strict_read",
    "native_workspace_actions", "native_fork_command", "native_outside_denial", "native_first_command",
}
METRICS = {"prompts", "requests", "unknown", "rule_matches"}


def read_stage(path, exit_code, *, native=False):
    """Whitelist count-only observations; never copy pytest output/failure text."""
    result = {"exit_code": exit_code, "tests": {}, "observations": [], "invalid_observations": 0,
              "passed": False}
    if not path.is_file():
        result["report_missing"] = True
        return result
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError):
        result["report_unreadable"] = True
        return result
    counts = Counter()
    for case in tree.iter("testcase"):
        status = next((s for s in ("error", "failure", "skipped") if case.find(s) is not None), "passed")
        counts[status] += 1
        for prop in case.findall("./properties/property"):
            if prop.get("name") != "permission_case":
                continue
            try:
                row = json.loads(prop.get("value", ""))
                assert set(row) == {"harness", "scenario", "profile", "expected", "observed"}
                assert row["harness"] in HARNESS_IDS and row["scenario"] in SCENARIOS
                assert row["profile"] in {"strict", "balanced", "trusted", "trust_review"}
                for key in ("expected", "observed"):
                    assert isinstance(row[key], dict) and row[key] and set(row[key]) <= METRICS
                    assert all(type(n) is int and 0 <= n <= 1000 for n in row[key].values())
                assert row["expected"].keys() == row["observed"].keys()
                result["observations"].append({**row, "matched": row["expected"] == row["observed"]})
            except (AssertionError, TypeError, ValueError):
                result["invalid_observations"] += 1
    result["tests"] = dict(counts)
    result["passed"] = bool(counts["passed"] and exit_code == 0 and not counts["failure"] and not counts["error"]
                            and not result["invalid_observations"] and (not native or not counts["skipped"])
                            and all(r["matched"] for r in result["observations"]))
    return result


def require_coverage(stage, native=None):
    if native is None:
        required = {(h, s) for h in HARNESS_IDS for s in SCENARIOS if not s.startswith("native_")}
    else:
        required = set()
        for harness in native:
            identity = {"goose": "goose_acp", "codex": "codex_app_server", "claude_openrouter": "claude_code",
                        "antigravity": "antigravity"}[harness]
            names = {"native_workspace_actions", "native_fork_command", "native_outside_denial"} if harness == "goose" else {
                "native_first_command", "native_fork_command"}
            required.update((identity, s) for s in names)
    observed = Counter((r["harness"], r["scenario"]) for r in stage["observations"])
    stage["missing_cases"] = sorted(required - observed.keys())
    stage["unexpected_cases"] = sorted(observed.keys() - required)
    stage["duplicate_cases"] = sorted(k for k, n in observed.items() if n != 1)
    stage["passed"] = bool(stage["passed"] and not any(stage[k] for k in ("missing_cases", "unexpected_cases", "duplicate_cases")))


def source_identity():
    # Only code/test inputs, never machine config, credentials or live DB files.
    files = {"server.py", "database.py", "event_router.py", "permission_policy.py", "permission_rules.py",
             "permission_commands.py", "permission_risk.py", "acp_permissions.py", "codex_client.py", "claude_client.py", "gemini_client.py",
             "goose_client.py", "cc_permission_hook.py", "static/chat.js", "static/index.html", "admin_server.py",
             "workspace_integrity.py", "workspace_safety.py", "workspace_fingerprint.py", "mcp_loom_workspace.py",
             "WORKSPACE_INTEGRITY.md", "canvas_workspace.py", "loom_agent.md", "CANVAS_GUIDE.md",
             "tests/conftest.py", "tests/permission_acceptance_metrics.py", "tools/permission_preflight.py",
             "formal/permission_policy/LoomPermissionPolicy/Policy.lean", "formal/permission_policy/Main.lean",
             "formal/permission_policy/generated/permission_cases.json",
             *OFFLINE, "tests/test_live_permission_acceptance.py", "tests/test_native_harness_permissions.py"}
    files.update(str(p.relative_to(ROOT)).replace("\\", "/") for p in (ROOT / "static/admin").glob("*.*"))
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode())
        digest.update((ROOT / name).read_bytes())
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5)
    commit = head.stdout.strip()
    return {"commit": commit if re.fullmatch(r"[0-9a-f]{40,64}", commit) else None,
            "tested_source_sha256": digest.hexdigest()}


def test_environment(native, trust_agy):
    env = dict(os.environ)
    # Never inherit unrelated opt-in model tests or a caller's run identity.
    for name in list(env):
        if name.startswith(('LOOM_RUN_LIVE_', 'LOOM_RUN_NATIVE_', 'LOOM_NATIVE_AGY_TRUST_')) or name in {
            'LOOM_DB', 'LOOM_PORT', 'ADMIN_PORT', 'LOOM_AGENT_RUN_ID', 'LOOM_PARENT_RUN_ID',
            'LOOM_CLIENT_INSTANCE_ID', 'LOOM_PERMISSION_SCOPE', 'LOOM_CONV_ID',
            'LOOM_BACKSTAGE_PARENT_ID', 'PYTEST_ADDOPTS',
        }:
            env.pop(name, None)
    env.update( LOOM_USE_LLAMA="0")
    if native:
        env.update(LOOM_RUN_LIVE_GOOSE_PERMISSION_TEST="1", LOOM_RUN_LIVE_CODEX_PERMISSION_TEST="1",
                   LOOM_RUN_NATIVE_HARNESS_PERMISSIONS="1")
    if trust_agy:
        env["LOOM_NATIVE_AGY_TRUST_TEST_HOOKS"] = "1"
    return env


def run_stage(name, selectors, artifacts, *, native=False, trust_agy=False):
    xml = artifacts / f"{name}.xml"
    command = [sys.executable, "-m", "pytest", *selectors, "-q", "--tb=short", "--show-capture=no",
               "-p", "no:cacheprovider", "-o", "junit_family=xunit1", f"--junitxml={xml}",
               f"--basetemp={artifacts / (name + '-tmp')}"]
    print(f"Running {name} acceptance...", flush=True)
    with (artifacts / f"{name}.log").open("x", encoding="utf-8") as log:
        result = subprocess.run(command, cwd=ROOT, env=test_environment(native, trust_agy), stdout=log, stderr=log)
    summary = read_stage(xml, result.returncode, native=native)
    print(f"{name}: {summary['tests']}; accepted={summary['passed']}", flush=True)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", nargs="*", choices=list(NATIVE), help="Paid native checks; omit names for all four")
    parser.add_argument("--trust-agy-test-hooks", action="store_true", help="Allow native trust registration for the disposable agy project only")
    parser.add_argument("--output-dir", type=Path, help="New, non-existing artifact directory (default: unique OS temp)")
    args = parser.parse_args(argv)
    native = list(NATIVE) if args.native == [] else (args.native or [])
    if "antigravity" in native and not args.trust_agy_test_hooks:
        parser.error("Antigravity requires --trust-agy-test-hooks for its disposable project registration")
    artifacts = args.output_dir
    if artifacts is None:
        artifacts = Path(tempfile.mkdtemp(prefix="exo-permission-preflight-"))
    else:
        artifacts = artifacts.resolve()
        artifacts.mkdir(parents=True, exist_ok=False)  # never overwrite previous evidence
    report = {"schema_version": 1, "scope": "isolated_predeployment", "native_requested": native,
              "limits": ["No production workflows executed", "Counts apply to bridged requests, not every native operation",
                         "Deterministic queue ownership is not a native scheduler test"], "stages": {}}
    report["source"] = source_identity()
    report["stages"]["offline"] = run_stage("offline", OFFLINE, artifacts)
    require_coverage(report["stages"]["offline"])
    if native and report["stages"]["offline"]["passed"]:
        report["stages"]["native"] = run_stage("native", [NATIVE[n] for n in native], artifacts,
                                                native=True, trust_agy=args.trust_agy_test_hooks)
        require_coverage(report["stages"]["native"], native)
    report["source_unchanged"] = report["source"] == source_identity()
    report["passed"] = (all(s["passed"] for s in report["stages"].values())
                        and report["source_unchanged"] and (not native or "native" in report["stages"]))
    with (artifacts / "report.json").open("x", encoding="utf-8") as dest:
        json.dump(report, dest, indent=2)
    print(f"Report: {artifacts / 'report.json'}", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
