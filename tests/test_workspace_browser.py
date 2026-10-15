"""Read-only workspace inspection must not escape or inherit host Git."""
from pathlib import Path
import subprocess
import sys

import pytest
import database as db
from workspace_browser import WorkspaceBrowseError, list_workspace_directory, git_workspace_summary


def test_directory_scope_private_paths_and_bounded_listing(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / 'notes.txt').write_text('notes')
    (root / '.env').write_text('private')
    (root / 'folder').mkdir()
    result = list_workspace_directory(str(root))
    assert [item['name'] for item in result['entries']] == ['folder', 'notes.txt']
    assert result['complete']
    assert not list_workspace_directory(str(root), limit=1)['complete']
    for path in ['..', str(tmp_path.resolve()), '.env', '.git/objects', '.git.', '.git ', '.git./objects', 'folder/..', 'folder:stream']:
        with pytest.raises(WorkspaceBrowseError):
            list_workspace_directory(str(root), path)


def test_nested_workspace_never_shows_parent_git(tmp_path, monkeypatch):
    import workspace_browser
    root = tmp_path / 'ExoHome'
    root.mkdir()
    calls = []

    def run(command):
        calls.append(command)
        return str(tmp_path), True

    monkeypatch.setattr(workspace_browser, '_read_git', run)
    result = git_workspace_summary(str(root))
    assert not result['repository']
    assert len(calls) == 1
    assert calls[0][-2:] == ['rev-parse', '--show-toplevel']
    assert '--no-optional-locks' in calls[0]


def test_exact_git_root_has_read_only_bounded_summary(tmp_path, monkeypatch):
    import workspace_browser
    calls = []

    def run(command):
        calls.append(command)
        output = str(tmp_path) if 'rev-parse' in command else ('x' * 33000 if 'status' in command else 'summary')
        return output[:32000], len(output) <= 32000

    monkeypatch.setattr(workspace_browser, '_read_git', run)
    result = git_workspace_summary(str(tmp_path))
    assert result['repository'] and not result['complete']
    assert len(result['status']) == 32000
    assert all('--no-optional-locks' in command for command in calls)
    assert all('core.fsmonitor=false' in command for command in calls)
    assert len(calls) == 4


@pytest.mark.parametrize('pipe', ['stdout', 'stderr'])
def test_git_capture_limits_both_pipes_and_reaps(pipe):
    from workspace_browser import _read_git
    output, complete = _read_git([sys.executable, '-c',
        f"import sys,time; sys.{pipe}.write('x'*1000000); sys.{pipe}.flush(); time.sleep(30)"])
    assert not complete
    assert len(output) == (32000 if pipe == 'stdout' else 0)


def test_git_summary_disables_configured_fsmonitor(tmp_path):
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    marker = tmp_path / 'hook-ran'
    subprocess.run(['git', '-C', str(tmp_path), 'config', 'core.fsmonitor',
                    f'echo hook > "{marker.as_posix()}"'], check=True)
    assert git_workspace_summary(str(tmp_path))['repository']
    assert not marker.exists()


def test_directory_replacement_after_validation_is_not_followed(tmp_path, monkeypatch):
    import workspace_browser
    root = tmp_path / 'workspace'
    outside = tmp_path / 'outside'
    root.mkdir()
    outside.mkdir()
    (outside / 'secret.txt').write_text('private')
    target = root / 'folder'
    target.mkdir()
    original = workspace_browser._directory_scan

    def replace_before_open(scan_root, path):
        target.rmdir()
        if sys.platform == 'win32':
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(target), str(outside)],
                           check=True, capture_output=True)
        else:
            target.symlink_to(outside, target_is_directory=True)
        return original(scan_root, path)

    monkeypatch.setattr(workspace_browser, '_directory_scan', replace_before_open)
    with pytest.raises((WorkspaceBrowseError, OSError)):
        list_workspace_directory(str(root), 'folder')


def test_open_directory_stays_pinned_during_enumeration(tmp_path):
    from workspace_browser import _directory_scan
    root = tmp_path / 'workspace'
    root.mkdir()
    target = root / 'folder'
    target.mkdir()
    (target / 'original.txt').write_text('original')
    with _directory_scan(root, Path('folder')) as scan:
        if sys.platform == 'win32':
            with pytest.raises(OSError):
                target.rename(root / 'moved')
        else:
            target.rename(root / 'moved')
            target.mkdir()
            (target / 'replacement.txt').write_text('replacement')
        assert [entry.name for entry in scan] == ['original.txt']


async def test_workspace_api_is_conversation_scoped(client, tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / 'public.md').write_text('visible')
    conv = await db.create_conversation('Scoped files', mode='codex', project_dir=str(root))
    response = await client.get(f"/api/conversations/{conv['id']}/workspace-files")
    assert response.status_code == 200
    assert response.json()['entries'][0]['path'] == 'public.md'
    assert response.headers['cache-control'] == 'no-store'
    outside = await client.get(f"/api/conversations/{conv['id']}/workspace-files", params={'path': '..'})
    assert outside.status_code == 400
    missing = await client.get('/api/conversations/987654321/workspace-files')
    assert missing.status_code == 404
