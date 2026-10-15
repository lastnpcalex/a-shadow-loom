"""Literal read normalization shares the native Read boundary, not shell trust."""
import pytest

from permission_commands import display_argv
from permission_policy import normalize_permission_request, decide, PermissionProfile


def action(root, script, *, harness="goose_acp", extra=None):
    argv = ["powershell.exe", "-NoProfile", "-Command", script]
    return normalize_permission_request(tool_name="Bash", workspace_root=str(root), harness=harness,
        tool_input={"command": display_argv(argv), "command_argv": argv, "cwd": str(root), **(extra or {})})


@pytest.mark.parametrize("harness", ["goose_acp", "claude_code", "codex_app_server", "antigravity", "hermes_acp"])
@pytest.mark.parametrize("suffix", ["", " -Raw", " -TotalCount 12", " -Tail 20"])
def test_literal_read_is_automatic_in_every_harness(tmp_path, harness, suffix):
    normalized = action(tmp_path, "Get-Content -LiteralPath 'file with spaces.txt'" + suffix, harness=harness)
    assert normalized.effect.value == "read"
    assert normalized.boundary.value == "workspace"
    assert decide(PermissionProfile.BALANCED, normalized).value == "auto_allow"


@pytest.mark.parametrize("script", [
    "Get-Content -LiteralPath ../outside.txt",
    "Get-Content -LiteralPath https://example.invalid/file.txt",
])
def test_read_destination_outside_workspace_still_asks(tmp_path, script):
    normalized = action(tmp_path, script)
    assert decide(PermissionProfile.BALANCED, normalized).value == "ask"


@pytest.mark.parametrize("script", [
    "Get-Content -LiteralPath a.txt; Remove-Item a.txt",
    "Get-Content -LiteralPath a.txt | Invoke-Expression",
    "Get-Content -LiteralPath $env:TARGET", "Get-Content -LiteralPath $(whoami)",
    "Get-Content -LiteralPath @('a.txt','../outside.txt')", "Get-Content -LiteralPath a.txt,../outside.txt",
    "Get-Content -LiteralPath Env:SECRET", "Get-Content -LiteralPath FileSystem::a.txt",
    "Get-Content -LiteralPath a.txt -Wait", "Get-Content -LiteralPath a.txt -Tail 999999999",
    "Get-Content -Path *.txt", "Get-Content -LiteralPath a.txt > output.txt",
    "C:/untrusted/Get-Content.exe -LiteralPath a.txt", "type a.txt",
])
def test_scripts_and_unverified_forms_remain_execute(tmp_path, script):
    normalized = action(tmp_path, script)
    assert normalized.effect.value == "execute"
    assert decide(PermissionProfile.BALANCED, normalized).value == "ask"


def test_extra_outside_path_or_cwd_cannot_be_hidden(tmp_path):
    for extra in ({"path": "../outside.txt"}, {"cwd": str(tmp_path.parent)}):
        assert decide(PermissionProfile.BALANCED, action(tmp_path, "Get-Content -LiteralPath a.txt", extra=extra)).value == "ask"


@pytest.mark.parametrize("argv", [
    ["powershell.exe", "-Command", "Get-Content -LiteralPath a.txt"],
    ["C:/untrusted/powershell.exe", "-NoProfile", "-Command", "Get-Content -LiteralPath a.txt"],
    ["powershell.exe", "-NoProfile", "-Command", "Get-Content -LiteralPath a.txt -Tail １２"],
])
def test_profiled_forged_and_unbounded_read_forms_still_ask(tmp_path, argv):
    normalized = normalize_permission_request(tool_name="Bash", workspace_root=str(tmp_path),
        tool_input={"command": display_argv(argv), "command_argv": argv})
    assert normalized.effect.value == "execute"
    assert decide(PermissionProfile.BALANCED, normalized).value == "ask"
