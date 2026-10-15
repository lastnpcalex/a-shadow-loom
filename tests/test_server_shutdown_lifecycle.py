import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import server


@pytest.mark.asyncio
async def test_shutdown_endpoint_does_not_close_database_before_lifespan(monkeypatch):
    fake_uvicorn = SimpleNamespace(should_exit=False)
    original_ref = list(server._server_ref)
    original_shutdown = server._shutdown_requested
    server._server_ref[:] = [fake_uvicorn]
    server._shutdown_requested = False
    close_db = AsyncMock()
    monkeypatch.setattr(server.db, "close_db", close_db)
    try:
        response = await server.shutdown()
        shutdown_requested_after = server._shutdown_requested
    finally:
        server._server_ref[:] = original_ref
        server._shutdown_requested = original_shutdown

    assert response.status_code == 200
    assert fake_uvicorn.should_exit is True
    assert shutdown_requested_after is True
    close_db.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_endpoint_does_not_wait_for_busy_generation_admission():
    original_ref = list(server._server_ref)
    original_shutdown = server._shutdown_requested
    fake_uvicorn = SimpleNamespace(should_exit=False)
    admission_entered = asyncio.Event()
    release_admission = asyncio.Event()

    async def hold_admission():
        async with server._generation_admission_lock:
            admission_entered.set()
            await release_admission.wait()

    holder = asyncio.create_task(hold_admission())
    await admission_entered.wait()
    server._server_ref[:] = [fake_uvicorn]
    server._shutdown_requested = False
    try:
        response = await asyncio.wait_for(server.shutdown(), timeout=0.25)
        assert response.status_code == 200
        assert fake_uvicorn.should_exit is True
        assert server._shutdown_requested is True
    finally:
        release_admission.set()
        await holder
        server._server_ref[:] = original_ref
        server._shutdown_requested = original_shutdown


@pytest.mark.asyncio
async def test_shutdown_drain_cancels_and_awaits_active_generation(monkeypatch):
    entered = asyncio.Event()
    finalized = asyncio.Event()

    async def active_generation():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()

    task = asyncio.create_task(active_generation())
    await entered.wait()
    key = (999_001, 123, 456)
    server._active_generations[key] = task
    monkeypatch.setattr(
        server.db, "list_active_generations", AsyncMock(return_value=[])
    )
    try:
        await server._drain_active_generations_for_shutdown()
    finally:
        server._active_generations.pop(key, None)

    assert task.done()
    assert finalized.is_set()


def test_generation_launch_fails_closed_after_shutdown_begins():
    assert server._generation_admission_lock is not None


@pytest.mark.asyncio
async def test_shutdown_waits_for_launch_to_register_before_draining(monkeypatch):
    original_shutdown = server._shutdown_requested
    original_seq = server._gen_seq
    entered_database_await = asyncio.Event()
    release_database_await = asyncio.Event()
    provider_entered = asyncio.Event()
    provider_release = asyncio.Event()
    release_lease = AsyncMock(return_value=True)

    async def delayed_conversation(_conv_id):
        entered_database_await.set()
        await release_database_await.wait()
        return {
            "id": 909_001,
            "mode": "codex",
            "parallel_agents_enabled": 0,
        }

    async def generation_entry(_websocket, _conv_id, _payload, _lease_id):
        provider_entered.set()
        await provider_release.wait()

    monkeypatch.setattr(server.db, "get_conversation", delayed_conversation)
    monkeypatch.setattr(
        server.db,
        "try_acquire_generation_lease",
        AsyncMock(return_value={"lease_id": "lease-test"}),
    )
    monkeypatch.setattr(server.db, "release_generation_lease", release_lease)
    monkeypatch.setattr(server, "_generation_task_entry", generation_entry)
    server._shutdown_requested = False
    launch = asyncio.create_task(
        server._launch_generation_task(
            None,
            909_001,
            {"parent_id": 123, "cc_model": "gpt-5.6-sol"},
            "page-shutdown-race",
        )
    )
    await entered_database_await.wait()
    close_admission = asyncio.create_task(server._close_generation_admission())
    await asyncio.sleep(0)
    assert close_admission.done() is False

    release_database_await.set()
    gen_key, provider_task = await launch
    await close_admission
    await provider_entered.wait()
    try:
        assert server._shutdown_requested is True
        assert server._active_generations.get(gen_key) is provider_task
    finally:
        provider_release.set()
        await provider_task
        await server._drain_tracked_background_tasks_for_shutdown()
        server._active_generations.pop(gen_key, None)
        server._generation_snapshots.pop(gen_key, None)
        server._shutdown_requested = original_shutdown
        server._gen_seq = original_seq


@pytest.mark.asyncio
async def test_shutdown_releases_preacquired_lease_without_launch(monkeypatch):
    original_shutdown = server._shutdown_requested
    release_lease = AsyncMock(return_value=True)
    monkeypatch.setattr(server.db, "release_generation_lease", release_lease)
    server._shutdown_requested = True
    try:
        with pytest.raises(HTTPException) as exc_info:
            await server._launch_generation_task(
                None,
                909_002,
                {"parent_id": 456},
                "page-shutdown-closed",
                preacquired_lease_id="preacquired-test",
            )
    finally:
        server._shutdown_requested = original_shutdown

    assert exc_info.value.status_code == 503
    release_lease.assert_awaited_once_with("preacquired-test", "shutdown")


@pytest.mark.asyncio
async def test_shutdown_cancels_and_drains_tracked_background_database_work():
    entered = asyncio.Event()
    finalized = asyncio.Event()

    async def database_work():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()

    task = server._spawn_background_task(database_work(), "shutdown test")
    assert task is not None
    await entered.wait()

    await server._drain_tracked_background_tasks_for_shutdown()

    assert task.done()
    assert finalized.is_set()
    assert task not in server._background_tasks
