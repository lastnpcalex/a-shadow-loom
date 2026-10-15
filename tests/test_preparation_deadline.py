"""The production preparation binding must obey the configured whole-turn limit."""
import asyncio
import json

import pytest
import context_preparation as prep
import context_runtime
import replay_budget
from preparation_errors import PreparationCapacityError


async def test_workspace_deadline_never_launches_provider(monkeypatch, tmp_path):
    import threading
    import server
    from unittest.mock import AsyncMock
    release = threading.Event()
    def slow_snapshot(*args, **kwargs):
        release.wait(2)
    monkeypatch.setattr(server.config, 'context_preparation_timeout_seconds', .03)
    monkeypatch.setattr(server.db, 'get_conversation', AsyncMock(return_value={
        'id': 1, 'mode': 'codex', 'project_dir': str(tmp_path), 'canvas_enabled': False,
    }))
    monkeypatch.setattr(server, 'capture_workspace_snapshot', slow_snapshot)
    provider = AsyncMock()
    monkeypatch.setattr(server, '_handle_generation', provider)
    monkeypatch.setattr(server, '_ws_send', AsyncMock())
    try:
        with pytest.raises(TimeoutError, match='provider was not launched'):
            await server._handle_generation_with_workspace_safety(None, 1, {'parent_id': 1})
        provider.assert_not_awaited()
    finally:
        release.set()


def test_destination_uses_configured_budget(monkeypatch):
    monkeypatch.setattr(context_runtime.config, 'context_preparation_max_calls', 3)
    monkeypatch.setattr(context_runtime.config, 'context_preparation_timeout_seconds', 7)
    summarizer = context_runtime.DestinationSummarizer(model='model', harness='codex', conv_id=161)
    assert summarizer.coordinator.budget.maximum_calls == 3
    assert summarizer.coordinator.budget.maximum_elapsed_seconds == 7
    assert summarizer.coordinator.conversation_id == 161


async def test_elapsed_deadline_prevents_retry_and_next_cut(monkeypatch):
    monkeypatch.setattr(prep, 'DURABILITY', False)
    async def capacity(*args, **kwargs):
        return 9000, 'test'
    monkeypatch.setattr(replay_budget, 'context_capacity', capacity)
    calls = []
    async def execute(*args, timeout_seconds, **kwargs):
        calls.append(timeout_seconds)
        await asyncio.sleep(timeout_seconds + .01)
        raise TimeoutError('provider timed out')
    monkeypatch.setattr(prep, 'execute_preparation_chunk', execute)
    coordinator = prep.BoundedPreparationCoordinator({'harness':'codex','model':'model'},
        budget=prep.PreparationBudget(maximum_elapsed_seconds=.05))
    records = json.dumps([{'id':1,'role':'user','content':'A'*9000}])
    with pytest.raises(PreparationCapacityError):
        await coordinator.summarize(records, 1000)
    with pytest.raises(PreparationCapacityError):
        await coordinator.summarize(records, 1000)
    assert len(calls) == 1
    assert 0 < calls[0] <= .05


async def test_real_transport_deadline_tears_down_provider(monkeypatch, tmp_path):
    import server
    class Proc:
        def poll(self): return 0
    process = Proc()
    async def stream():
        await asyncio.Event().wait()
        yield {}
    async def launch(*args, **kwargs): return process, stream()
    teardown = []
    async def verify(proc, harness): teardown.append((proc, harness))
    monkeypatch.setattr(server.codex_client, 'run_codex', launch)
    monkeypatch.setattr(prep, 'verify_process_teardown', verify)
    with pytest.raises(TimeoutError):
        await prep.execute_preparation_chunk('test', {'harness':'codex','model':'model'},
            tmp_path, output_cap=1000, timeout_seconds=.02)
    assert teardown == [(process, 'codex')]


async def test_handoff_progress_updates_reconnect_snapshot(monkeypatch):
    import server
    from unittest.mock import AsyncMock
    monkeypatch.setattr(server, '_generation_snapshots', {})
    sent = AsyncMock()
    monkeypatch.setattr(server, '_ws_send', sent)
    async def prepare(branch, **kwargs):
        await kwargs['status']('Preparing context on codex')
        return 'prepared'
    monkeypatch.setattr(context_runtime, 'prepare_branch', prepare)
    task = asyncio.current_task()
    task._gen_key = (161,30376,2)
    try:
        await server._prepare_agent_handoff([], {'id':161}, 'model', 'codex')
        assert server._generation_snapshots[task._gen_key]['phase'] == 'context_preparation'
        assert server._generation_snapshots[task._gen_key]['status_text'] == 'Preparing context on codex'
        event = sent.call_args.args[1]
        assert event['type'] == 'generation_preparing'
        assert event['parent_id'] == 30376
    finally:
        del task._gen_key
