"""agy oversize prompts: one file per launch, stale leftovers swept."""

import os
import time

import gemini_client


def test_sweep_removes_only_day_old_prompt_files(tmp_path):
    now = time.time()
    old = tmp_path / "loom_prompt_7_aaaaaaaaaaaa.md"
    fresh = tmp_path / "loom_prompt_7_bbbbbbbbbbbb.md"
    legacy = tmp_path / "loom_prompt_7.md"
    other = tmp_path / "mcp_config.json"
    for path in (old, fresh, legacy, other):
        path.write_text("x", encoding="utf-8")
    two_days_ago = now - 2 * 24 * 60 * 60
    for path in (old, legacy, other):
        os.utime(path, (two_days_ago, two_days_ago))

    gemini_client._sweep_stale_prompt_files(tmp_path, now=now)

    assert not old.exists() and not legacy.exists()
    assert fresh.exists()  # a running turn's file is never removed
    assert other.exists()  # only prompt files are touched


def test_prompt_file_name_is_unique_per_launch():
    source = open(gemini_client.__file__, encoding="utf-8").read()
    assert 'f"loom_prompt_{conv_id}.md"' not in source
    assert "uuid.uuid4().hex" in source


def test_run_gemini_does_not_shadow_module_imports():
    """A function-level `import uuid` makes uuid local to the whole function,
    so the prompt-file path raised UnboundLocalError unless a fork ran first."""
    import symtable
    source = open(gemini_client.__file__, encoding="utf-8").read()
    table = symtable.symtable(source, gemini_client.__file__, "exec")
    run = next(child for child in table.get_children() if child.get_name() == "run_gemini")
    assert "uuid" in run.get_identifiers()
    for name in ("uuid", "time"):
        if name in run.get_identifiers():
            assert not run.lookup(name).is_local(), f"{name} is shadowed inside run_gemini"
