# EXO Search Space

Search Space is an ephemeral in-memory search chat for finding earlier EXO discussions.
Ask a question naturally, refine it in conversation over multiple turns, and open
exact conversation/branch links. Search chats are held strictly in server memory
and discarded when the window closes, a new search starts, idle timeout expires, or the
server restarts. Nothing about the search chat — questions, answers, excerpts, or
transcripts — is saved to the Loom database, SQLite index, disk, or logs. The underlying
conversation search index remains persistent and incrementally maintained.

## Workspace policy

Every workspace starts **Excluded** and must be explicitly classified:

- **Cloud search** — locally indexable; retrieved excerpts may be sent to
  OpenRouter GLM 5.3 Flash for query expansion and synthesis.
- **Sensitive · local only** — locally indexable; search is allowed only through
  llama-server.
- **Excluded** — not searchable; any existing local index copies for the
  workspace are deleted.

If a search combines cloud and sensitive workspaces, the complete request uses
the local route. An unavailable or unresponsive llama-server produces an
actionable error. Sensitive searches never fall back to OpenRouter or to the
llama client's mock response.

This classification is deliberately workspace-wide for cross-conversation
search. A workspace can still use cloud models for ordinary, non-sensitive
branches and local models for sensitive branches; Search Space takes the more
conservative classification because a global index can encounter any branch.

## Data flow and capability boundary

1. SQLite revision triggers identify changed conversations. EXO copies only
   changed conversation text and recorded content blocks to an external index.
2. Local BGE embeddings and FTS5 retrieve semantic and keyword candidates,
   combined with reciprocal rank fusion. Cached vectors avoid re-embedding
   unchanged messages. Warm queries load transcript text only for top candidates.
3. The operator inspects actual message ancestry, tries additional searches,
   and answers with stable `[S#]` citations and verified links. Timestamp
   neighbors and sibling branches are never used as context.
4. Follow-ups retain prior questions, answers and source identities, with
   bounded input and fresh reads of prior sources. Deleted or moved sources
   cannot be reused outside the selected scope.

The dedicated operator receives only `search_conversations`,
`read_conversation_context`, and `get_conversation_details`. The bounded
server-side model loop rejects every other action. It does not launch a CLI
with ambient tools. Historical content, recorded tool output and earlier
answers are untrusted data. The optional legacy `mcp_search_space.py` facade
remains compatible with existing callers.

Ordinary agents receive those same three history tools through
`loom-agent-control`. A server-issued capability fixes the source database,
assigned work folder, local/cloud route and owning task. It expires when the
task finishes or is cancelled. Agents cannot supply a different folder to
widen the search. Live assignment and disclosure policy are checked before

The shared configuration covers Claude Code, Codex, Antigravity and Goose ACP,
including the local, Dream, OpenRouter and Umans routes. Goose new/load/fork
handshakes all receive a fresh capability. Browser-origin links use the page
that launched the agent; `LOOM_PUBLIC_URL` supplies the public origin when no
browser connection is available.

The index is namespaced by the active Loom database identity as well as message
ID, preventing a database switch from linking reused IDs to the wrong source.
The default external location is `%LOCALAPPDATA%\A Shadow Loom\search-space` on
Windows or the corresponding XDG state directory; `EXO_STATE_DIR` overrides the
root.

## Setup and limits

Install requirements, then explicitly prepare the local embedding model:

```powershell
C:\Python314\python.exe tools/setup_search_embeddings.py
```

Model preparation downloads public model files; it sends no conversation text.
Search loads cached models with `local_files_only=True`. If embeddings are
unavailable, results explicitly report keyword-only retrieval. Sensitive
operator synthesis still requires a working llama-server and has no cloud fallback.

The first semantic query embeds the selected corpus in batches. It can take
longer for large workspaces; later queries reuse vectors. Similarity currently
scans cached vectors rather than an approximate-nearest-neighbor index. Long
messages are embedded in overlapping chunks up to 57,600 characters, while
FTS indexes the full text and context reads provide pagination.

Excluding a workspace purges its indexed text, vectors, revision cache and
search transcripts that selected it. Operator directories contain only fixed
instructions, never conversation excerpts. Redaction and a retention UI remain
future work.

## Validation and formal boundary

`formal/search_space/Main.lean` proves disclosure routing, scope requirements,
exact ancestry identity and the three-tool allowlist. Its generated fixture is
exhaustively checked against Python; see `formal/search_space/README.md` for
the precise trust boundary. Lean does not prove model relevance, HTTP behavior
or the entire application.

Acceptance tests use temporary SQLite databases, isolated HTTP servers, real
MCP stdio clients, and a browser. Native launch/handshake tests inspect actual
adapter configuration without contacting paid model backends. The real-model
probe uses synthetic discussions and verifies a semantic match with zero FTS
matches:

```powershell
C:\Python314\python.exe -m pytest tests/test_search_history.py tests/test_search_policy.py tests/test_search_history_mcp.py tests/test_search_space_browser.py tests/test_search_space.py
C:\Python314\python.exe tools/search_semantic_probe.py
```

The new backend routes, revision triggers and per-turn capabilities take effect
when the human next reloads the main server. No main/admin lifecycle operation
is part of setup or validation.
