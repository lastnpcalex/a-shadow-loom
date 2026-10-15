import pytest
from unittest.mock import AsyncMock

from acceptance_status import evaluate_acceptance, publish_passing_report, read_published_report


IDENTITY = {"commit": "abc", "tested_source_sha256": "123"}


def test_only_passing_source_stable_report_can_publish(tmp_path):
    target = tmp_path / "acceptance" / "report.json"
    for report in (
        {"passed": False, "source_unchanged": True, "source": IDENTITY},
        {"passed": True, "source_unchanged": False, "source": IDENTITY},
        {"passed": True, "source_unchanged": True, "source": {}},
    ):
        with pytest.raises(ValueError):
            publish_passing_report(report, target)
    report = {"passed": True, "source_unchanged": True, "source": IDENTITY}
    assert publish_passing_report(report, target) == target
    assert read_published_report(target) == report


def test_acceptance_is_verified_only_on_three_way_identity_match():
    report = {"passed": True, "source_unchanged": True, "source": IDENTITY}
    assert evaluate_acceptance(report, IDENTITY, IDENTITY)["state"] == "verified"
    assert evaluate_acceptance(report, {**IDENTITY, "commit": "new"}, IDENTITY)["state"] == "stale"
    assert evaluate_acceptance(report, IDENTITY, {**IDENTITY, "commit": "old"})["state"] == "stale"
    assert evaluate_acceptance(report, IDENTITY, None)["state"] == "unverified"
    assert evaluate_acceptance(None, IDENTITY, IDENTITY)["state"] == "unverified"


def test_source_identity_covers_formal_portable_context_inputs(tmp_path, monkeypatch):
    import subprocess
    from acceptance_status import source_identity

    (tmp_path / "formal" / "portable_context").mkdir(parents=True)
    model = tmp_path / "formal" / "portable_context" / "Model.lean"
    cases = tmp_path / "formal" / "portable_context" / "cases.json"
    model.write_text("def version := 1", encoding="utf-8")
    cases.write_text('{"version": 1}', encoding="utf-8")

    def fake_run(args, **_kwargs):
        if args[1] == "ls-files":
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        return subprocess.CompletedProcess(args, 0, stdout="deadbeef\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    before = source_identity(tmp_path)["tested_source_sha256"]
    model.write_text("def version := 2", encoding="utf-8")
    after_model = source_identity(tmp_path)["tested_source_sha256"]
    cases.write_text('{"version": 2}', encoding="utf-8")
    after_cases = source_identity(tmp_path)["tested_source_sha256"]
    assert before != after_model != after_cases


@pytest.mark.asyncio
async def test_main_reports_the_identity_captured_at_startup(monkeypatch):
    import json
    import server

    monkeypatch.setattr(server, "_STARTUP_SOURCE_IDENTITY", IDENTITY)
    response = await server.api_build_identity()
    assert json.loads(response.body)["source"] == IDENTITY


@pytest.mark.asyncio
async def test_admin_requires_report_checkout_and_running_main_to_match(monkeypatch):
    import json
    import admin_server

    report = {"passed": True, "source_unchanged": True, "source": IDENTITY}
    response = type("Response", (), {
        "status_code": 200,
        "json": lambda self: {"source": IDENTITY},
    })()
    monkeypatch.setattr(admin_server, "source_identity", lambda: IDENTITY)
    monkeypatch.setattr(admin_server, "read_published_report", lambda: report)
    monkeypatch.setattr(admin_server, "_get_instance", AsyncMock(return_value=response))
    result = await admin_server.api_acceptance()
    assert json.loads(result.body)["state"] == "verified"


def test_admin_renders_acceptance_as_a_distinct_operator_status():
    from pathlib import Path

    html = Path("static/admin/index.html").read_text(encoding="utf-8")
    js = Path("static/admin/admin.js").read_text(encoding="utf-8")
    assert 'id="acceptance-state"' in html
    assert "accepted ${short(data.tested_source)}" in js
    assert "data.state === 'verified'" in js
