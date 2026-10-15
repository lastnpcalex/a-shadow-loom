from unittest.mock import AsyncMock

import pytest

import context_runtime
import database as db
import local_context_guard
from replay_budget import PayloadBudget
from tests.test_continuity_launch_matrix import SyntheticSummary


async def test_local_preparation_counts_with_destination_tokenizer(monkeypatch):
    conv = await db.create_conversation('Synthetic tokenizer disagreement')
    user = await db.add_message(conv['id'], 'user', 'KEEP_KEY_PRIVATE ' + ' x' * 8000)
    old = await db.add_message(conv['id'], 'assistant', 'Saved key.md', parent_id=user['id'])
    current = await db.add_message(conv['id'], 'user', 'Next task', parent_id=old['id'])
    counters = []
    class Counter:
        def __init__(self, *args):
            self.closed = False
            self.counts = []
            counters.append(self)
        async def count(self, payload):
            count = len(payload['messages'][0]['content'])
            self.counts.append(count)
            return count
        async def close(self):
            self.closed = True
    monkeypatch.setattr(local_context_guard, 'LocalContextGuard', Counter)
    monkeypatch.setattr(context_runtime, 'ConfiguredLocalSummarizer', SyntheticSummary)
    monkeypatch.setattr(context_runtime, 'assess_payload', AsyncMock(return_value=PayloadBudget(
        15000, 'test-local', 0, 0, 1000, 0, 1000, 1000)))
    # A byte/3 estimate fits; the exact counter does not. The shared planner
    # must checkpoint BEFORE invoking the destination harness.
    prompt = await context_runtime.prepare_branch(await db.get_branch_to_root(current['id']),
                                                  model='synthetic.gguf', harness='goose', backend='llama')
    assert len(counters) == 1 and counters[0].closed
    assert max(counters[0].counts) > 10000
    assert 'old detail' not in prompt and ' x x x' not in prompt
    assert 'KEEP_KEY_PRIVATE' in prompt and 'Next task' in prompt
    assert '"checkpoint"' in prompt


async def test_local_counter_failure_is_not_an_estimate_fallback(monkeypatch):
    conv = await db.create_conversation('Synthetic unavailable tokenizer')
    user = await db.add_message(conv['id'], 'user', 'Task')
    class Counter:
        closed = False
        def __init__(self, *args):
            pass
        async def count(self, payload):
            raise ValueError('Tokenizer unavailable')
        async def close(self):
            Counter.closed = True
    monkeypatch.setattr(local_context_guard, 'LocalContextGuard', Counter)
    monkeypatch.setattr(context_runtime, 'assess_payload', AsyncMock(return_value=PayloadBudget(
        15000, 'test-local', 0, 0, 1000, 0, 1000, 1000)))
    with pytest.raises(ValueError, match='Tokenizer unavailable'):
        await context_runtime.prepare_branch(await db.get_branch_to_root(user['id']),
                                             model='synthetic.gguf', harness='claude', backend='llama')
    assert Counter.closed
