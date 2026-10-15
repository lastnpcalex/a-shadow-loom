# Local context admission and provider review

Implemented 2026-09-12. This change covers Goose ACP and Claude Code backed by
llama-server. Other providers retain native context management.

## Request behavior

Each local provider invocation owns an authenticated loopback proxy. It admits
every inference request, including requests within a single tool loop, using
the live `/props` per-slot capacity. Anthropic payloads use
`/v1/messages/count_tokens`; OpenAI payloads use `/apply-template` and `/tokenize`.
Requested output tokens and a 10% margin are reserved. Media embeddings have
an additional conservative reserve on the OpenAI path; that reserve is an
estimate, not a proof of every possible image/video size.

If a request does not fit, the same local model summarizes older complete
exchanges. Large completed tool-result bodies can also be summarized without
removing their call IDs, error flags or envelopes. Summary inference is
tool-free, chunked and token-checked. Incomplete/empty summaries fail closed.
The final request is checked again before inference. A streamed inference
request is never replayed automatically.

System/developer instructions, tools, current user input and pending tool-call
arguments remain intact. Fresh replay compaction requires the exact history
region identified by Loom's renderer; tags supplied by a user or tool do not
authorize compaction. If protected material still cannot fit, the run reports
an admission error and sends no generation request.

This is an outbound context projection: no canonical message is removed or
reparented, and no native session is edited. Summaries are cached in memory for
that invocation only. Subsequent runs may need to summarize again. The UI
receives progress through the existing run-scoped status stream. Cancellation
closes the proxy, upstream requests and summary work with the provider run.
Native forks, permission hooks, durable children, and model selectors remain
in place. The proxy never controls the Loom host or llama-server lifecycle.

Goose receives the live context capacity, a 70% native compaction threshold,
the summarize strategy, and the same local model for auxiliary requests. The
per-request guard covers cases where native compaction misses a tool loop.

## Codex review

The installed Codex 0.153.4 schema confirms `tokenUsage.last`, `tokenUsage.total`,
`modelContextWindow`, and `capabilities.experimentalApi`. Loom now uses the
last request for current input/context, derives output deltas from cumulative
usage without counting the previous session's output, and filters foreign
thread/turn events before accounting. Native `contextCompaction` progress is
visible. A thread-wide idle notification cannot terminate the active turn;
an exact turn terminal is required. Normal launches enable the same experimental
capability as the existing goal-control path. Native compaction remains owned
by Codex; no cumulative Loom-history threshold is imposed on its resumes.

## Validation

- Synthetic live local request: 109,709 input tokens against a 100,096-token
  slot; summarized to 185 tokens in five passes. The result ID was retained,
  the expected acknowledgement returned, and no tools executed.
- Installed Goose 1.45.0: tool-free fresh run and forked continuation both
  succeeded through separate proxy instances; the fork retained the marker.
- Focused regression coverage includes local admission, failed summaries,
  protected instructions, tool pairing during steering, cancellation, spawn
  failure cleanup, local durable children, provider boundaries, and Codex
  usage/compaction/terminal events. Permission, Canvas and model-registry
  contract suites were also exercised.

Reproduce the opt-in live checks (synthetic content only):

```powershell
C:\Python314\python.exe tools/local_context_preflight.py --live
C:\Python314\python.exe tools/local_context_preflight.py --live --native-goose
```

These checks use the running local model and create synthetic native Goose
sessions. They do not start/restart either host or execute workspace tools.

## Research

- [Goose tool-loop compaction issue](https://github.com/aaif-goose/goose/issues/11072)
- [Goose structured overflow classification issue](https://github.com/aaif-goose/goose/issues/11048)
- [llama-server token-count and template endpoints](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
- [Codex app-server protocol](https://learn.chatgpt.com/docs/app-server)

The native probes and offline checks do not prove every model's summary
quality. High-detail context can lose detail through summarization; original
transcripts are deliberately retained for verification. An older llama-server
without the required count endpoints fails admission rather than silently
forwarding an oversized payload.
