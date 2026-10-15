"""agy runs in one workspace write shared .agents config one at a time."""

import asyncio

import gemini_client


class _SilentProc:
    async def wait(self):
        await asyncio.Event().wait()


async def test_same_repo_shares_one_lock_and_output_releases_it(tmp_path, monkeypatch):
    monkeypatch.setattr(gemini_client, "_AGY_CONFIG_LOCKS", {})
    (tmp_path / ".git").mkdir()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    first = await gemini_client._acquire_workspace_config_lock(str(tmp_path / "a"))
    second_task = asyncio.create_task(gemini_client._acquire_workspace_config_lock(str(tmp_path / "b")))
    await asyncio.sleep(0.05)
    assert not second_task.done(), "second launch must wait while the first writes/loads config"

    output = asyncio.Event()
    release = asyncio.create_task(gemini_client._release_config_lock_when_loaded(first, output, _SilentProc()))
    output.set()
    second = await asyncio.wait_for(second_task, 2)
    assert second is first
    await release
    second.release()


async def test_silent_agy_releases_after_settle_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(gemini_client, "_AGY_CONFIG_LOCKS", {})
    monkeypatch.setattr(gemini_client, "_AGY_CONFIG_SETTLE_SECONDS", 0.05)
    lock = await gemini_client._acquire_workspace_config_lock(str(tmp_path))
    await asyncio.wait_for(
        gemini_client._release_config_lock_when_loaded(lock, asyncio.Event(), _SilentProc()), 2)
    assert not lock.locked()


async def test_other_workspaces_do_not_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(gemini_client, "_AGY_CONFIG_LOCKS", {})
    for name in ("x", "y"):
        (tmp_path / name / ".git").mkdir(parents=True)
    held = await gemini_client._acquire_workspace_config_lock(str(tmp_path / "x"))
    other = await asyncio.wait_for(gemini_client._acquire_workspace_config_lock(str(tmp_path / "y")), 1)
    assert other is not held
    held.release()
    other.release()
