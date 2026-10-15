"""Recovery exclusions must not silently discard user-owned project files."""
import workspace_safety as safety


def test_tracked_scratch_and_profile_files_survive_exclusion(tmp_path):
    import subprocess
    subprocess.run(['git','init',str(tmp_path)],check=True,capture_output=True)
    for name in ['user_profile.py','profile.json','scratch/notes.md','tmp_feature.py']:
        p=tmp_path/name
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text('original')
    subprocess.run(['git','-C',str(tmp_path),'add','.'],check=True,capture_output=True)
    paths, dirty, is_git, _ = safety._workspace_paths(tmp_path.resolve())
    assert is_git
    assert {'user_profile.py','profile.json','scratch/notes.md','tmp_feature.py'} <= paths
    assert 'tmp_feature.py' in dirty


def test_fallback_preserves_profiles_notes_and_env(tmp_path,monkeypatch):
    monkeypatch.setattr(safety,'_git_bytes',lambda *_:None)
    for name in ['user_profile.py','profile.json','scratch/notes.md','.env']:
        p=tmp_path/name
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text('original')
    assert {'user_profile.py','profile.json','scratch/notes.md','.env'} <= safety._workspace_paths(tmp_path.resolve())[0]
