"""Opt-in existing llama-server summary acceptance; synthetic records only.

Does not start/reload a model or launch a Loom/CLI process. This is summary
quality evidence, not native cross-harness/session acceptance.
"""
import os

import pytest

from context_runtime import ConfiguredLocalSummarizer


@pytest.mark.skipif(os.getenv("LOOM_RUN_CONTEXT_SUMMARY") != "1", reason="Opt-in local inference")
async def test_native_summary_preserves_early_correction_and_pending_work(tmp_database):
    summarizer = ConfiguredLocalSummarizer()
    text = (
        'User [message 1]: The artifact is ORCHID-17.md. Never publish it.\n'
        'Assistant [message 2]: I believe it was saved.\n'
        'Tool [message 3]: Save failed; permission denied by policy, not user.\n'
        + 'Historical observation: formatting inspected; no artifact changes.\n' * 300
        + 'User [message 4]: Correction: ORCHID-17.md was NOT saved. '
          'Pending action: obtain reviewer QUARTZ-29 approval. Do not repeat the write automatically.\n'
    )
    result = await summarizer.summarize(text, 1024)
    assert result.route == summarizer.identity
    assert "ORCHID-17.md" in result.text
    assert "QUARTZ-29" in result.text
    lower = result.text.lower()
    assert "not saved" in lower or "save failed" in lower or "failed" in lower
    assert "policy" in lower
