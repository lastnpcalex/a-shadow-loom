"""Isolated lifecycle + responsiveness acceptance; no live-host URL or restart option.

Default uses controlled providers. --native-codex and --native-goose add
authenticated turn-pair tests with independent process-tree sampling.
--native-repeats repeats each pair in fresh processes without erasing failures.
--native-children exercises real durable-child approvals across parent stop.
--native-delegation exercises model-chosen MCP calls and nested cross-harness work.
--browser-resources measures the existing raytracer toggle in isolated Chromium.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.permission_preflight import test_environment
from tests.lifecycle_metrics import valid_row
from acceptance_status import publish_passing_report, source_identity

CORE = [f"tests/{name}.py" for name in (
    "test_lifecycle_preflight", "test_event_router", "test_parallel_agents", "test_queue_durable_topology",
    "test_provider_contract", "test_agent_supervisor", "test_agent_access", "test_nested_agent_contract",
    "test_agent_event_batching", "test_workspace_safety", "test_permission_workflows",
    "test_api", "test_model_registry", "test_settings_all_models_singleflight", "test_codex_permissions", "test_goose_acp",
    "test_recovery_budget", "test_codex_completion_recheck", "test_turn_telemetry_recovery",
    "test_coordination", "test_local_context_guard", "test_workspace_integrity",
)]
BROWSER = [
    "tests/test_regeneration_user_workflow.py", "tests/test_exoloom_mobile_browser.py",
    "tests/test_mobile_landing_browser.py",
    "tests/test_fullstack_queue_isolation.py::test_real_browser_queue_waits_for_provider_exit_and_survives_reconnect",
    "tests/test_fullstack_queue_isolation.py::test_real_browser_can_cancel_visible_prelaunch_without_losing_prompt",
    "tests/test_queue_presentation_browser.py", "tests/test_agent_activity_browser.py", "tests/test_agent_goals_layout.py",
    "tests/test_lifecycle_handoff_browser.py", "tests/test_model_registry_browser.py",
    "tests/test_followup_continuity_browser.py", "tests/test_turn_recovery_browser.py",
]
CONTINUITY = [
    "tests/test_portable_context_requirements.py",
    "tests/test_portable_context_spec.py",
    "tests/test_portable_context_trace_oracle.py",
    "tests/test_continuity_engine.py",
    "tests/test_context_runtime.py",
    "tests/test_context_checkpoints.py",
    "tests/test_continuity_launch_matrix.py",
    "tests/test_provider_contract.py",
    "tests/test_portable_context_isolation.py",
    "tests/test_preparation_tool_free.py",
    "tests/test_preparation_durable_resume.py",
    "tests/test_multichunk_coordinator_publishes_and_reuses_checkpoint.py",
    "tests/test_production_binding.py",
    "tests/test_model_attestation.py",
]
NATIVE = ["tests/test_fullstack_queue_isolation.py::test_live_codex_queue_transitions_only_after_process_exit"]
NATIVE_GOOSE = ["tests/test_fullstack_queue_isolation.py::test_live_goose_queue_transitions_only_after_process_exit"]
NATIVE_CHILDREN = ["tests/test_native_child_lifecycle.py"]
NATIVE_DELEGATION = ["tests/test_native_delegation.py"]
RESOURCES = ["tests/test_browser_resource_baseline.py"]
REQUIRED = {"core": {"snapshot_io"}, "browser": {"queue_browser", "prelaunch_browser", "child_burst", "handoff_browser", "child_parent_stop", "discovery_browser"},
            "continuity": set(),
            "native": {"native_interrupt", "native_cancel"},
            "native_goose": {"native_goose_interrupt", "native_goose_cancel"},
            "native_children": {f"native_child_{h}_{a}" for h in ("codex", "goose") for a in ("interrupt", "cancel")},
            "native_delegation": {"native_delegation_codex", "native_delegation_goose"},
            "resources": {f"browser_idle_{s}_{i}" for s in ("on", "off") for i in range(1, 4)} | {"browser_visibility_signal"}}
REQUIRED['browser'].update({'mobile_rerun_foreground', 'launch_lost_ack_recovery'})


def stage_plan(codex=False, goose=False, repeats=1, *, children=False, resources=False, delegation=False):
    if type(repeats) is not int or not 1 <= repeats <= 10:
        raise ValueError("native repeats must be between 1 and 10")
    if repeats != 1 and not (codex or goose or children or delegation):
        raise ValueError("native repeats require a native harness")
    stages = [("core", "core", CORE), ("browser", "browser", BROWSER),
              ("continuity", "continuity", CONTINUITY)]
    if resources:
        stages.append(("resources", "resources", RESOURCES))
    for iteration in range(1, repeats + 1):
        for enabled, kind, selectors in ((codex, "native", NATIVE), (goose, "native_goose", NATIVE_GOOSE),
                                         (children, "native_children", NATIVE_CHILDREN),
                                         (delegation, "native_delegation", NATIVE_DELEGATION)):
            if enabled:
                name = kind if iteration == 1 else f"{kind}-{iteration}"
                stages.append((name, kind, selectors))
    return stages


def read_stage(path, returncode, required):
    result = {"passed": False, "exit_code": returncode, "tests": {}, "observations": [], "invalid_observations": 0}
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError):
        result["report_missing_or_unreadable"] = True
        return result
    counts = Counter()
    scenarios = Counter()
    for case in tree.iter("testcase"):
        status = next((s for s in ("error", "failure", "skipped") if case.find(s) is not None), "passed")
        counts[status] += 1
        for prop in case.findall("./properties/property"):
            if prop.get("name") != "lifecycle_case":
                continue
            try:
                row = json.loads(prop.get("value", ""))
                if not valid_row(row):
                    raise ValueError("schema")
                result["observations"].append(row)
                scenarios[row["scenario"]] += 1
            except (ValueError, TypeError, KeyError):
                result["invalid_observations"] += 1
    result["tests"] = dict(counts)
    result["missing_cases"] = sorted(required - scenarios.keys())
    result["duplicate_cases"] = sorted(s for s, n in scenarios.items() if n != 1)
    result["unexpected_cases"] = sorted(scenarios.keys() - required)
    result["passed"] = bool(returncode == 0 and counts["passed"] and not any(counts[s] for s in ("failure", "error", "skipped"))
                            and not result["invalid_observations"] and not result["missing_cases"]
                            and not result["duplicate_cases"] and not result["unexpected_cases"])
    return result


def run_stage(name, kind, selectors, artifacts):
    xml = artifacts / f"{name}.xml"
    env = test_environment(False, False)
    if kind == "native":
        env["LOOM_RUN_LIVE_CODEX_VERIFY"] = "1"
    elif kind == "native_goose":
        env["LOOM_RUN_LIVE_GOOSE_VERIFY"] = "1"
    elif kind == "native_children":
        env["LOOM_RUN_NATIVE_CHILD_VERIFY"] = "1"
    elif kind == "native_delegation":
        env["LOOM_RUN_NATIVE_DELEGATION_VERIFY"] = "1"
    command = [sys.executable, "-m", "pytest", *selectors, "-q", "--tb=short", "--show-capture=no",
               "-p", "no:cacheprovider", "-p", "tests.lifecycle_isolation", "-o", "junit_family=xunit1",
               f"--junitxml={xml}", f"--basetemp={artifacts / (name + '-tmp')}"]
    print(f"Running isolated {name} lifecycle acceptance...", flush=True)
    # Every stage owns an OS-temporary external-state root. It must remain
    # outside the checkout even when --output-dir points inside the repository;
    # provider project-root discovery may otherwise climb into EXO and mutate
    # its .agents configuration during an allegedly isolated acceptance run.
    with tempfile.TemporaryDirectory(prefix=f"exo-{name}-state-") as state_root:
        env["EXO_STATE_DIR"] = state_root
        with (artifacts / f"{name}.log").open("x", encoding="utf-8") as log:
            proc = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=log)
    result = read_stage(xml, proc.returncode, REQUIRED[kind])
    print(f"{name}: {result['tests']}; accepted={result['passed']}", flush=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-codex", action="store_true")
    parser.add_argument("--native-goose", action="store_true")
    parser.add_argument("--native-children", action="store_true", help="Paid real Codex/Goose child approvals across parent stop/reconnect")
    parser.add_argument("--native-delegation", action="store_true", help="Paid model-driven MCP spawn/wait/get across nested Codex/Goose runs")
    parser.add_argument("--browser-resources", action="store_true", help="Headless raytracer-on/off renderer cost; not physical-device evidence")
    parser.add_argument("--native-repeats", type=int, default=1, help="1-10 fresh turn-pair repetitions per native harness")
    parser.add_argument("--output-dir", type=Path, help="New directory only; defaults outside the repository")
    parser.add_argument("--publish", action="store_true",
                        help="Publish a passing source-stable report to external EXO state")
    args = parser.parse_args(argv)
    try:
        stages = stage_plan(args.native_codex, args.native_goose, args.native_repeats,
                            children=args.native_children, resources=args.browser_resources, delegation=args.native_delegation)
    except ValueError as exc:
        parser.error(str(exc))
    artifacts = args.output_dir
    if artifacts is None:
        artifacts = Path(tempfile.mkdtemp(prefix="exo-lifecycle-preflight-"))
    else:
        artifacts = artifacts.resolve()
        artifacts.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": 2, "scope": "isolated_predeployment", "source": source_identity(),
              "platform": platform.system(), "python": platform.python_version(), "stages": {},
              "native_requested": [h for h, enabled in (("codex", args.native_codex), ("goose", args.native_goose)) if enabled],
              "native_repeats": args.native_repeats,
              "native_children_requested": args.native_children, "browser_resources_requested": args.browser_resources,
              "native_delegation_requested": args.native_delegation,
              "planned_stages": [name for name, _, _ in stages],
              "limits": ["Controlled providers are not OS-process overlap evidence",
                         "Native process-tree samples wait 50 ms between queries plus OS query latency",
                         "Native children and cross-harness inference are not exercised by the top-level queue cycles",
                         "Browser CPU is Chromium renderer main-thread task time, not whole-machine CPU",
                         "Memory is JS heap and DOM node count, not total browser/GPU RSS",
                         "Headless local Chromium is not a measurement on a physical phone or WAN",
                         "Timings are observations on synthetic workloads, not latency SLOs"]}
    for name, kind, selectors in stages:
        report["stages"][name] = run_stage(name, kind, selectors, artifacts)
        # Preserve all evidence and avoid paid launches after any failed stage.
        if not report["stages"][name]["passed"]:
            break
    report["source_unchanged"] = report["source"] == source_identity()
    report["passed"] = (report["source_unchanged"] and all(r["passed"] for r in report["stages"].values())
                        and list(report["stages"]) == report["planned_stages"])
    (artifacts / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.publish and report["passed"]:
        published = publish_passing_report(report)
        print(f"Published acceptance: {published}", flush=True)
    print(f"Report: {artifacts / 'report.json'}", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
