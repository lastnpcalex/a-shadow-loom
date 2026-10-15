import json

import pytest

import gemini_client


def test_named_hooks_preserve_other_customizations_and_use_seconds(tmp_path, monkeypatch):
    import codex_client
    (tmp_path / '.git').mkdir()
    agents = tmp_path / '.agents'
    agents.mkdir()
    hook = agents / 'hooks.json'
    original = {'user-linter': {'PostToolUse': [{'matcher': '*', 'hooks': [{'command': 'echo lint'}]}]}}
    hook.write_text(json.dumps(original), encoding='utf-8')
    monkeypatch.setattr(gemini_client, '_ensure_hook_trusted', lambda *a: None)
    monkeypatch.setattr(codex_client, '_agent_control_mcp_config', lambda *a, **k: None)
    gemini_client._configure_permission_hook(str(tmp_path))
    configured = json.loads(hook.read_text(encoding='utf-8'))
    assert configured['user-linter'] == original['user-linter']
    loom = configured['loom-permissions']
    assert loom['PreToolUse'][0]['hooks'][0]['timeout'] == 86400
    assert loom['PostToolUse'][0]['hooks'][0]['timeout'] == 90
    gemini_client._configure_permission_hook(str(tmp_path))
    assert json.loads(hook.read_text(encoding='utf-8')) == configured


def test_malformed_existing_hook_file_is_not_overwritten(tmp_path, monkeypatch):
    hook = tmp_path / '.agents' / 'hooks.json'
    hook.parent.mkdir()
    hook.write_text('[]', encoding='utf-8')
    monkeypatch.setattr(gemini_client, '_ensure_hook_trusted', lambda *a: None)
    with pytest.raises(ValueError):
        gemini_client._configure_permission_hook(str(tmp_path))
    assert hook.read_text(encoding='utf-8') == '[]'


def test_agy_supplied_cwd_and_paths_survive_normalization(tmp_path):
    from cc_permission_hook import normalize_tool_args
    from permission_policy import normalize_permission_request
    payload = normalize_tool_args('run_command', {'CommandLine': 'echo fixture', 'Cwd': str(tmp_path.parent)})
    action = normalize_permission_request(tool_name='Bash', tool_input=payload, workspace_root=str(tmp_path))
    assert action.boundary.value == 'outside_workspace'
    payload = normalize_tool_args('list_dir', {'DirectoryPath': str(tmp_path.parent)})
    action = normalize_permission_request(tool_name='Glob', tool_input=payload, workspace_root=str(tmp_path))
    assert action.boundary.value == 'outside_workspace'


def test_only_exact_legacy_loom_hooks_are_migrated(tmp_path, monkeypatch):
    import codex_client
    import sys
    (tmp_path / '.git').mkdir()
    hook = tmp_path / '.agents' / 'hooks.json'
    hook.parent.mkdir()
    old = {'PreToolUse': {'matcher': '*', 'hooks': [{'type': 'command',
        'command': f'& "{sys.executable}" "{gemini_client._HOOK_SCRIPT}" --event PreToolUse', 'timeout': 86400000}]},
        'PostToolUse': {'matcher': '*', 'hooks': [{'command': 'echo user-hook'}]}}
    hook.write_text(json.dumps(old), encoding='utf-8')
    monkeypatch.setattr(gemini_client, '_ensure_hook_trusted', lambda *a: None)
    monkeypatch.setattr(codex_client, '_agent_control_mcp_config', lambda *a, **k: None)
    gemini_client._configure_permission_hook(str(tmp_path))
    result = json.loads(hook.read_text(encoding='utf-8'))
    assert 'PreToolUse' not in result
    assert result['PostToolUse'] == old['PostToolUse']
    assert 'loom-permissions' in result


def test_windows_hook_command_quotes_only_the_fixed_entry_point(monkeypatch):
    import base64
    monkeypatch.setattr(gemini_client.sys, 'platform', 'win32')
    monkeypatch.setattr(gemini_client.sys, 'executable', "C:/Fixture's Python/python.exe")
    monkeypatch.setattr(gemini_client, '_HOOK_SCRIPT', 'C:/Fixture Workspace/hook.py')
    command = gemini_client._agy_hook_command('PreToolUse')
    assert '"' not in command and "'" not in command
    decoded = base64.b64decode(command.split()[-1]).decode('utf-16-le')
    assert "'C:/Fixture''s Python/python.exe'" in decoded
    assert "'C:/Fixture Workspace/hook.py' --event PreToolUse" in decoded
    with pytest.raises(ValueError):
        gemini_client._agy_hook_command('anything; else')


@pytest.mark.parametrize('role', ['LOOM_BACKSTAGE_PARENT_ID'])
def test_native_powershell_still_respects_existing_role_lockdowns(monkeypatch, role):
    import io
    import sys
    import cc_permission_hook
    from tests.test_plan_approval_hook import _FakeResponse
    monkeypatch.setenv('LOOM_CONV_ID', '1')
    for name in ('LOOM_BACKSTAGE_PARENT_ID',):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(role, '1')
    monkeypatch.setattr(sys, 'argv', ['hook.py', '--event', 'PreToolUse'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({
        'tool_name': 'PowerShell', 'tool_input': {'command': 'echo fixture'}})))
    output = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', output)
    def response(request, **kwargs):
        assert request.full_url.endswith('/api/cc-tool-start')
        return _FakeResponse()
    monkeypatch.setattr(cc_permission_hook.urllib.request, 'urlopen', response)
    with pytest.raises(SystemExit):
        cc_permission_hook.main()
    assert json.loads(output.getvalue())['hookSpecificOutput']['permissionDecision'] == 'deny'
