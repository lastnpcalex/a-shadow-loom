import json
import xml.etree.ElementTree as ET

import pytest

from tools import permission_preflight as preflight


def fixture_xml(path, *, status=None, observation=None):
    root = ET.Element("testsuites")
    case = ET.SubElement(ET.SubElement(root, "testsuite"), "testcase", name="PRIVATE_test_title")
    ET.SubElement(case, "system-out").text = "PRIVATE_command_or_note"
    if status:
        ET.SubElement(case, status, message="PRIVATE_failure").text = "PRIVATE_trace"
    if observation is not None:
        ET.SubElement(ET.SubElement(case, "properties"), "property", name="permission_case", value=json.dumps(observation))
    ET.ElementTree(root).write(path, encoding="utf-8")


def row():
    return {"harness": "goose_acp", "scenario": "workspace_read", "profile": "balanced",
            "expected": {"prompts": 0}, "observed": {"prompts": 0}}


def test_report_is_count_only_and_missing_coverage_is_not_green(tmp_path):
    path = tmp_path / "result.xml"
    fixture_xml(path, observation=row())
    stage = preflight.read_stage(path, 0)
    assert stage["passed"] and "PRIVATE" not in json.dumps(stage)
    preflight.require_coverage(stage)
    assert not stage["passed"] and len(stage["missing_cases"]) == 75


@pytest.mark.parametrize("status,exit_code", [("failure", 1), ("error", 1), ("skipped", 0), (None, 2)])
def test_native_error_skip_or_incomplete_pytest_cannot_pass(tmp_path, status, exit_code):
    path = tmp_path / "result.xml"
    fixture_xml(path, status=status)
    assert not preflight.read_stage(path, exit_code, native=True)["passed"]


@pytest.mark.parametrize("mutation", ["harness", "scenario", "extra", "count", "mismatch"])
def test_untrusted_metadata_or_count_mismatch_cannot_pass(tmp_path, mutation):
    data = row()
    if mutation in {"harness", "scenario"}:
        data[mutation] = "PRIVATE_metadata"
    elif mutation == "extra":
        data["command"] = "PRIVATE_command"
    elif mutation == "count":
        data["observed"]["prompts"] = "PRIVATE_value"
    else:
        data["observed"]["prompts"] = 1
    path = tmp_path / "result.xml"
    fixture_xml(path, observation=data)
    stage = preflight.read_stage(path, 0)
    assert not stage["passed"] and "PRIVATE" not in json.dumps(stage)


def test_missing_or_broken_result_does_not_pass(tmp_path):
    path = tmp_path / "result.xml"
    assert not preflight.read_stage(path, 0)["passed"]
    path.write_text("<unfinished", encoding="utf-8")
    assert not preflight.read_stage(path, 0)["passed"]


def test_runner_does_not_inherit_live_scope_or_subscription_opt_in(monkeypatch):
    for name in ("LOOM_DB", "LOOM_AGENT_RUN_ID", "LOOM_PERMISSION_SCOPE", "LOOM_PORT", "PYTEST_ADDOPTS",
                 "LOOM_RUN_NATIVE_CLAUDE_SUBSCRIPTION_TEST", "LOOM_RUN_LIVE_CODEX_PERMISSION_TEST"):
        monkeypatch.setenv(name, "PRIVATE")
    env = preflight.test_environment(False, False)
    assert not any(env.get(n) == "PRIVATE" for n in ("LOOM_DB", "LOOM_AGENT_RUN_ID", "LOOM_PERMISSION_SCOPE", "LOOM_PORT", "PYTEST_ADDOPTS"))
    assert "LOOM_RUN_NATIVE_CLAUDE_SUBSCRIPTION_TEST" not in env
    assert "LOOM_RUN_LIVE_CODEX_PERMISSION_TEST" not in env
    assert "LOOM_NATIVE_AGY_TRUST_TEST_HOOKS" not in env
    assert "[claude]" not in " ".join(preflight.NATIVE.values())


def test_requires_project_hook_consent_and_new_artifacts(tmp_path):
    with pytest.raises(SystemExit):
        preflight.main(["--native", "antigravity"])
    with pytest.raises(FileExistsError):
        preflight.main(["--output-dir", str(tmp_path)])


def test_duplicate_metric_cases_are_not_complete_coverage():
    stage = {"passed": True, "observations": [row(), row()]}
    preflight.require_coverage(stage)
    assert stage["duplicate_cases"] and not stage["passed"]


async def test_manual_reply_without_policy_metadata_is_not_unknown(tmp_path, monkeypatch):
    import asyncio
    import database as db
    import server
    from tests.permission_acceptance_metrics import unknown_count
    from tests.test_permission_enforcement import wait_pending
    from tests.test_live_permission_acceptance import _answer
    monkeypatch.setattr(server.config, "default_permission_profile", "balanced")
    monkeypatch.setattr(server, "_permission_rules", {})
    monkeypatch.setattr(server, "_pending_hook_permissions", {})
    monkeypatch.setattr(server, "_generation_snapshots", {})
    conv = await db.create_conversation("Audit measurement fixture", project_dir=str(tmp_path))
    task = asyncio.create_task(server.handle_cc_permission({"loom_conv_id": conv["id"],
        "source": "goose_acp", "tool_name": "Bash", "tool_input": {"command": "echo fixture", "cwd": str(tmp_path)}}))
    try:
        rid, _ = await wait_pending(task)
        await _answer(conv["id"], {"request_id": rid, "allow": True, "always": False})
        reply = await asyncio.wait_for(task, 2)
        assert reply["allow"] and "policy" not in reply
        audits = await db.get_permission_policy_audits(conv["id"])
        assert len(audits) == 1 and unknown_count(audits) == 0
        assert unknown_count([{"action": {"effect": "read", "boundary": "unknown"}}]) == 1
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
