"""Admin responsiveness with blocked fixture samplers; no live host actions."""
import asyncio
import json
import threading
from unittest.mock import AsyncMock

import httpx
import pytest

import admin_server as admin


async def test_admin_identity_never_probes_instances(monkeypatch):
    probe = AsyncMock(side_effect=AssertionError('Identity must not probe Main'))
    monkeypatch.setattr(admin, 'check_instance', probe)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin.app),
                                base_url='http://admin.fixture') as client:
        response = await client.get('/api/admin-status')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['admin_pid'] == admin.os.getpid()
    assert 'instances' not in response.json()
    probe.assert_not_awaited()


@pytest.mark.parametrize('endpoint', ['memory', 'dream'])
async def test_slow_gpu_sample_leaves_event_loop_responsive(monkeypatch, endpoint):
    started, release = threading.Event(), threading.Event()
    completed = threading.Event()
    loop_thread = threading.get_ident()
    sample_threads = []

    def sample():
        sample_threads.append(threading.get_ident())
        started.set()
        try:
            release.wait(1)
            return 123
        finally:
            completed.set()

    # Every HTTP/process probe is a fixture. Only the deliberately blocked
    # GPU sampler runs, without starting any real nvidia-smi or sidecar.
    original_client = httpx.AsyncClient
    monkeypatch.setattr(admin.httpx, 'AsyncClient', lambda **kwargs: original_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kwargs))
    monkeypatch.setattr(admin, '_dream_probe', AsyncMock(return_value=None))
    monkeypatch.setattr(admin, '_dream_resolve_pid', lambda: None)
    monkeypatch.setattr(admin, '_dream_proc_mem', lambda pid: (0, 0))
    monkeypatch.setattr(admin, '_pids_by_signature', lambda *args: [])
    monkeypatch.setattr(admin, '_get_llama_port', lambda: 61111)
    monkeypatch.setattr(admin, '_dream_port', lambda: 61112)
    monkeypatch.setattr(admin, '_gpu_total_vram_used_mb', sample)
    monkeypatch.setattr(admin, '_child_procs', {})
    route = admin.api_memory_services if endpoint == 'memory' else admin.api_dream_models
    task = asyncio.create_task(route())
    try:
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(.005)
        # Without offloading, this coroutine cannot run until sample returns.
        assert not completed.is_set()
        response = await admin.api_admin_status()
        assert json.loads(response.body)['admin_pid'] == admin.os.getpid()
        assert not completed.is_set()
        assert sample_threads == [sample_threads[0]]
        assert sample_threads[0] != loop_thread
    finally:
        release.set()
        await task


async def test_quota_proxy_bounds_entire_upstream_attempt(monkeypatch):
    async def stalled(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(admin, 'INSTANCES', {'main': {'port': 61111}})
    monkeypatch.setattr(admin, '_get_instance', stalled)
    monkeypatch.setattr(admin, '_PROVIDER_USAGE_PROXY_TIMEOUT', .02)
    async with asyncio.timeout(.5):
        response = await admin.api_provider_usage()
    assert response.status_code == 503
    assert response.headers['cache-control'] == 'no-store'
