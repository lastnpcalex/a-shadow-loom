"""Provider exit acknowledgement must eventually release retained ownership."""
import asyncio
from types import SimpleNamespace

import database as db


async def test_late_provider_exit_clears_tracking_and_lease(monkeypatch, tmp_database):
    import server

    conv = await db.create_conversation('Late provider exit', mode='claude')
    draft = await db.add_message(conv['id'], 'assistant', 'Preserved partial answer')
    lease = await db.try_acquire_generation_lease(conv['id'], 'late-exit')
    await db.bind_generation_lease(lease['lease_id'], draft_msg_id=draft['id'], state='running')
    await db.register_active_generation(draft['id'], conv['id'], 999999)
    exited = asyncio.Event()

    class Process:
        returncode = None

        async def wait(self):
            await exited.wait()
            self.returncode = 0
            return 0

    process = Process()
    task = SimpleNamespace(_coordination_processes=[process])
    notifications = []

    async def idle(conv_id):
        notifications.append(conv_id)

    monkeypatch.setattr(server, '_send_generation_idle_if_quiescent', idle)
    cleanup = asyncio.create_task(server._finish_deferred_generation_teardown(
        task, conv['id'], draft['id'], lease['lease_id'], 'interrupted'
    ))
    await asyncio.sleep(0)
    assert await db.get_live_generation_lease(conv['id'])
    assert await db.list_active_generations()
    exited.set()
    await cleanup
    assert not await db.get_live_generation_lease(conv['id'])
    assert not await db.list_active_generations()
    assert (await db.get_message(draft['id']))['content'] == 'Preserved partial answer'
    assert notifications == [conv['id']]
