import pytest

from context_summary import BoundedSummarizer
from continuity_engine import ContextLimit, SummaryResult


class Backend:
    identity = "synthetic-authorized"

    def __init__(self):
        self.requests = []

    async def capacity(self):
        return 2048

    async def count(self, messages, output):
        return sum(len(m["content"]) for m in messages)

    async def complete(self, messages, output):
        assert await self.count(messages, output) + output + 256 <= 2048
        self.requests.append(messages)
        return SummaryResult("Synthetic retained facts", self.identity)


async def test_summary_of_oversized_source_checks_every_request():
    backend = Backend()
    result = await BoundedSummarizer(backend).summarize("x" * 9000, 256)
    assert len(backend.requests) > 1
    assert result.route == backend.identity
    fragments = [r[1]["content"].split("\nNext historical fragment:\n", 1)[1] for r in backend.requests]
    assert "".join(fragments) == "x" * 9000


async def test_summary_loop_is_bounded():
    with pytest.raises(ContextLimit, match="bounded"):
        await BoundedSummarizer(Backend(), max_requests=1).summarize("x" * 9000, 256)


async def test_summary_cancellation_propagates():
    import asyncio
    class Cancelled(Backend):
        async def complete(self, messages, output):
            raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await BoundedSummarizer(Cancelled()).summarize("short", 256)
