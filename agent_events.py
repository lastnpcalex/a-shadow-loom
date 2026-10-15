"""Bounded child-output batches; durable ordering precedes socket delivery.

Only deltas may wait briefly in memory. Control/lifecycle events flush first;
closing a child drains its output. A hard process crash can lose the unflushed
50 ms tail, never an acknowledged permission or terminal transition.
"""
import asyncio
import json
import logging
import time
import uuid

import database as db

log = logging.getLogger(__name__)
DELTA_KINDS = frozenset({
    "text_delta", "stream_chunk", "thinking_delta", "thinking_chunk",
    "tool_input_delta", "tool_input_chunk",
})


class ChildEventWriter:
    interval = 0.05
    max_events = 64
    max_bytes = 64 * 1024

    def __init__(self, run, sink=None, batch_sink=None):
        self.run = run
        self.sink = sink
        self.batch_sink = batch_sink
        self.pending = []
        self.pending_bytes = 0
        self._timer = None
        self._flush_task = None
        self._failure = None

    async def emit(self, kind, payload=None, *, run_state=None):
        if self._failure:
            raise self._failure
        # Freeze the payload: adapters may reuse their mutable event objects.
        serialized = json.dumps(payload or {}, ensure_ascii=False)
        event = {"event_kind": kind, "payload": json.loads(serialized),
                 "event_id": str(uuid.uuid4()), "created_at": time.time()}
        if kind not in DELTA_KINDS or run_state:
            await self.flush()
            stored = await db.append_agent_events(self.run["run_id"], [event], run_state=run_state)
            await self._deliver(stored)
            return stored[0]
        self.pending.append(event)
        self.pending_bytes += len(serialized.encode("utf-8"))
        if len(self.pending) >= self.max_events or self.pending_bytes >= self.max_bytes:
            await self.flush()
        if self.pending and (self._timer is None or self._timer.done()):
            self._timer = asyncio.create_task(self._timed_flush())
        # Buffered deltas acquire their database sequence when persisted.
        return event

    async def _deliver(self, events):
        try:
            if self.batch_sink and len(events) > 1:
                await self.batch_sink(self.run, events)
            elif self.sink:
                for event in events:
                    await self.sink(self.run, event)
        except Exception:
            # A disconnected browser does not turn a successful child into a
            # failure. Every event is already available for cursor-based replay.
            log.exception("Child %s event delivery failed", self.run["run_id"])

    async def _persist_pending(self):
        try:
            while self.pending:
                batch = self.pending[:]
                stored = await db.append_agent_events(self.run["run_id"], batch)
                del self.pending[:len(batch)]
                self.pending_bytes = sum(len(json.dumps(e["payload"], ensure_ascii=False).encode("utf-8"))
                                         for e in self.pending)
                await self._deliver(stored)
        except Exception as exc:
            self._failure = exc
            raise

    async def flush(self):
        if self._failure:
            raise self._failure
        if self._flush_task is None or self._flush_task.done():
            if not self.pending:
                return
            self._flush_task = asyncio.create_task(self._persist_pending())
        # Cancellation cannot detach an in-flight commit from its ordered
        # delivery. The terminal handler joins this task before persisting stop.
        await asyncio.shield(self._flush_task)

    async def _timed_flush(self):
        try:
            while self.pending:
                await asyncio.sleep(self.interval)
                await self.flush()
        except Exception:
            # The next emit/close surfaces persistence failure to the owner.
            log.exception("Child %s output flush failed", self.run["run_id"])

    async def close(self):
        if self._timer:
            self._timer.cancel()
            await asyncio.gather(self._timer, return_exceptions=True)
        await self.flush()
