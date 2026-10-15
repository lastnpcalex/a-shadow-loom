import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from tests.lifecycle_metrics import valid_row
from tools.lifecycle_preflight import read_stage, REQUIRED, stage_plan


def test_required_plan_keeps_coordination_and_recent_regressions():
    from tools.lifecycle_preflight import CORE, BROWSER, CONTINUITY
    assert {"tests/test_coordination.py", "tests/test_local_context_guard.py",
            "tests/test_codex_completion_recheck.py", "tests/test_queue_durable_topology.py"} <= set(CORE)
    assert {"tests/test_followup_continuity_browser.py", "tests/test_turn_recovery_browser.py"} <= set(BROWSER)
    assert {'tests/test_regeneration_user_workflow.py', 'tests/test_exoloom_mobile_browser.py',
            'tests/test_mobile_landing_browser.py'} <= set(BROWSER)
    assert {'mobile_rerun_foreground', 'launch_lost_ack_recovery'} <= REQUIRED['browser']
    assert {"tests/test_portable_context_requirements.py", "tests/test_continuity_engine.py",
            "tests/test_preparation_tool_free.py", "tests/test_production_binding.py",
            "tests/test_model_attestation.py"} <= set(CONTINUITY)


def test_repetitions_are_distinct_and_cover_each_native_harness():
    plan = stage_plan(True, True, 3)
    assert [name for name, _, _ in plan] == [
        "core", "browser", "continuity", "native", "native_goose", "native-2", "native_goose-2", "native-3", "native_goose-3"]
    assert all(selectors for _, _, selectors in plan)
    assert [kind for _, kind, _ in plan].count("native_goose") == 3
    assert [name for name, _, _ in stage_plan()] == ["core", "browser", "continuity"]


@pytest.mark.parametrize("repeats", [0, -1, 11, True, 1.5])
def test_repetitions_are_bounded(repeats):
    with pytest.raises(ValueError):
        stage_plan(True, True, repeats)


def test_repetition_requires_native_harness():
    with pytest.raises(ValueError):
        stage_plan(repeats=2)


def test_children_and_resource_stages_are_explicit_and_complete():
    assert [n for n, _, _ in stage_plan(children=True, resources=True, repeats=2)] == [
        "core", "browser", "continuity", "resources", "native_children", "native_children-2"]
    assert REQUIRED["native_children"] == {
        f"native_child_{h}_{a}" for h in ("codex", "goose") for a in ("interrupt", "cancel")}
    assert len(REQUIRED["resources"]) == 7


def test_native_fixture_decisions_follow_literal_action_not_order_or_title():
    from tests.test_native_child_lifecycle import fixture_action
    def shell(command):
        return {"tool_name": "Bash", "tool_input": {"command": command}}
    assert fixture_action(shell('powershell.exe -NoProfile -Command "Write-Output CHILD_TOOL_OK"'), "goose", {}) == "allow"
    assert fixture_action(shell('powershell.exe -NoProfile -Command "Set-Content -LiteralPath denied.txt -Value forbidden"'), "goose", {}) == "deny"
    for command in ('Write-Output CHILD_TOOL_OK; Set-Content unwanted.txt bad', 'Set-Content other.txt CHILD_TOOL_OK'):
        with pytest.raises(AssertionError):
            fixture_action(shell(command), "goose", {})
    with pytest.raises(AssertionError):
        fixture_action({"tool_name": "Unknown", "tool_input": {"content": "CHILD_TOOL_OK"}}, "goose", {})


def test_native_fixture_todo_requires_exact_metadata_and_payload():
    from tests.test_native_child_lifecycle import fixture_action
    request = {"tool_name": "Unknown", "tool_input": {"acp_identity": {"tool_call_id": "todo-1"}, "content": "Fixture plan"}}
    call = {"_meta": {"goose": {"toolCall": {"extensionName": "todo", "toolName": "todo__todo_write"}}},
            "rawInput": {"content": "Fixture plan"}}
    assert fixture_action(request, "goose", {"todo-1": call}) == "plan"
    request["tool_input"]["path"] = "unexpected.txt"
    with pytest.raises(AssertionError):
        fixture_action(request, "goose", {"todo-1": call})
    del request["tool_input"]["path"]
    with pytest.raises(AssertionError):
        fixture_action(request, "goose", {})
    call["rawInput"]["content"] = "Changed after read"
    with pytest.raises(AssertionError):
        fixture_action(request, "goose", {"todo-1": call})


def test_child_and_resource_flags_survive_into_report(tmp_path, monkeypatch):
    import tools.lifecycle_preflight as runner
    monkeypatch.setattr(runner, "source_identity", lambda: {"commit": "synthetic"})
    monkeypatch.setattr(runner, "run_stage", lambda *args: {"passed": True})
    artifacts = tmp_path / "new-artifacts"
    assert runner.main(["--native-children", "--browser-resources", "--output-dir", str(artifacts)]) == 0
    result = json.loads((artifacts / "report.json").read_text())
    assert result["native_children_requested"] and result["browser_resources_requested"]
    assert result["planned_stages"] == ["core", "browser", "continuity", "resources", "native_children"]
    assert result["passed"]


def test_delegation_stage_requires_both_native_chains_and_preserves_flag(tmp_path, monkeypatch):
    import tools.lifecycle_preflight as runner
    assert [n for n, _, _ in stage_plan(delegation=True, repeats=2)] == [
        "core", "browser", "continuity", "native_delegation", "native_delegation-2"]
    assert REQUIRED["native_delegation"] == {"native_delegation_codex", "native_delegation_goose"}
    monkeypatch.setattr(runner, "source_identity", lambda: {"commit": "synthetic"})
    monkeypatch.setattr(runner, "run_stage", lambda *args: {"passed": True})
    artifacts = tmp_path / "delegation"
    assert runner.main(["--native-delegation", "--output-dir", str(artifacts)]) == 0
    result = json.loads((artifacts / "report.json").read_text())
    assert result["native_delegation_requested"] and result["passed"]


def test_runner_stops_after_failure_and_does_not_accept_missing_repetitions(tmp_path, monkeypatch):
    import tools.lifecycle_preflight as runner
    monkeypatch.setattr(runner, "source_identity", lambda: {"commit": "synthetic"})
    called = []
    def run(name, kind, selectors, artifacts):
        called.append(name)
        return {"passed": name != "native-2"}
    monkeypatch.setattr(runner, "run_stage", run)
    artifacts = tmp_path / "new-artifacts"
    assert runner.main(["--native-codex", "--native-goose", "--native-repeats", "3",
                        "--output-dir", str(artifacts)]) == 1
    result = json.loads((artifacts / "report.json").read_text())
    assert called == ["core", "browser", "continuity", "native", "native_goose", "native-2"]
    assert not result["passed"] and len(result["planned_stages"]) == 9


def test_publish_is_explicit_and_only_follows_complete_acceptance(tmp_path, monkeypatch):
    import tools.lifecycle_preflight as runner

    identity = {"commit": "synthetic", "tested_source_sha256": "digest"}
    monkeypatch.setattr(runner, "source_identity", lambda: identity)
    monkeypatch.setattr(runner, "run_stage", lambda *args: {"passed": True})
    published = []
    monkeypatch.setattr(runner, "publish_passing_report", lambda report: published.append(report) or tmp_path / "published.json")
    artifacts = tmp_path / "accepted"
    assert runner.main(["--publish", "--output-dir", str(artifacts)]) == 0
    assert len(published) == 1 and published[0]["passed"] is True

    monkeypatch.setattr(runner, "run_stage", lambda *args: {"passed": False})
    failed = tmp_path / "failed"
    assert runner.main(["--publish", "--output-dir", str(failed)]) == 1
    assert len(published) == 1


def report(tmp_path, status=None, row=None):
    root = ET.Element("testsuite")
    case = ET.SubElement(root, "testcase", name="synthetic")
    if status:
        ET.SubElement(case, status).text = "PRIVATE FAILURE NOT IN SUMMARY"
    if row is not None:
        ET.SubElement(ET.SubElement(case, "properties"), "property", name="lifecycle_case", value=json.dumps(row))
    path = tmp_path / "report.xml"
    ET.ElementTree(root).write(path)
    return path


@pytest.mark.parametrize("status", ["failure", "error", "skipped"])
def test_nonpass_or_skip_is_not_acceptance(tmp_path, status):
    result = read_stage(report(tmp_path, status=status), 0, set())
    assert not result["passed"]
    assert "PRIVATE" not in json.dumps(result)


def test_missing_metric_case_and_missing_report_fail_closed(tmp_path):
    assert not read_stage(report(tmp_path), 0, REQUIRED["browser"])["passed"]
    assert not read_stage(tmp_path / "absent", 0, set())["passed"]


@pytest.mark.parametrize("metric", [float("nan"), float("inf"), -1, True, "path", {"command":"private"}])
def test_numeric_metrics_only(metric):
    assert not valid_row({"scenario":"queue_browser", "metrics":{"reconnect_ms":metric}})


def test_report_rejects_unknown_data_and_accepts_numeric_observation(tmp_path):
    row = {"scenario":"queue_browser", "metrics":{"reconnect_ms":23}}
    assert read_stage(report(tmp_path, row=row), 0, {"queue_browser"})["passed"]
    row["metrics"]["command"] = "SECRET"
    result = read_stage(report(tmp_path, row=row), 0, {"queue_browser"})
    assert not result["passed"] and result["invalid_observations"] == 1
    assert "SECRET" not in json.dumps(result)


def test_runner_has_no_host_control_inputs():
    source = Path("tools/lifecycle_preflight.py").read_text(encoding="utf-8")
    assert '--host' not in source and '/shutdown' not in source and '/restart' not in source
    assert 'exist_ok=False' in source


def test_duplicate_unexpected_and_nonzero_exit_fail(tmp_path):
    row = {"scenario":"queue_browser", "metrics":{"launches":2}}
    path = report(tmp_path, row=row)
    assert not read_stage(path, 1, {"queue_browser"})["passed"]
    assert not read_stage(path, 0, {"native_interrupt"})["passed"]
    tree = ET.parse(path)
    case = tree.getroot().find("testcase")
    tree.getroot().append(ET.fromstring(ET.tostring(case)))
    tree.write(path)
    assert read_stage(path, 0, {"queue_browser"})["duplicate_cases"] == ["queue_browser"]


def test_unreadable_report_is_not_a_pass(tmp_path):
    path = tmp_path / "broken.xml"
    path.write_text("<incomplete", encoding="utf-8")
    assert not read_stage(path, 0, set())["passed"]
