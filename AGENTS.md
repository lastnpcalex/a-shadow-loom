# AGENTS.md — Codebase Map

> Pointers, not prose. Read this file plus two or three of the files it references to orient in under five minutes.

## What This Project Is

EX umbra Omnia (EXO) is a self-hosted web interface for branching AI conversations across multiple LLM providers (Claude Code, ChatGPT Codex, Antigravity/Gemini, Goose ACP, local llama.cpp). Every conversation is a tree — branch, fork, regenerate, and search across all paths without losing anything. Providers are interchangeable shuttles on the same branching infrastructure: one database, one tree UI, one permission system. Existing Loom mode names, APIs, environment variables, and storage paths remain unchanged for compatibility.

## Top-Level Entry Points

- `server.py` — FastAPI main server (port 3000): WebSocket streaming, REST endpoints, all generation pipelines
- `admin_server.py` — Admin dashboard server (port 3002): instance/model lifecycle, instance-scoped permission supervision; legacy terminal/tools/cron APIs retained
- `static/index.html` — SPA shell; home view, chat view, tree view routing
- `static/app.js` — State management, home view, character/persona/lore CRUD, settings panel
- `static/chat.js` — WebSocket chat client, message rendering, streaming, branching, image handling
- `static/tree.js` — Interactive tree visualization (pan/zoom, drag, Greek-letter branch naming)
- `static/style.css` — Glassmorphism CSS, cyan/purple palette, responsive layout
- `static/blackhole.js` — Pre-compiled Schwarzschild raytracer WebGL background
- `static/canvas-sdk.js` — Canvas postMessage bridge SDK for interactive canvas
- `cc_permission_hook.py` — PreToolUse hook: bridges CC/agy/Codex tool permissions through browser UI
- `pytest` — Test suite; entry via `C:\Python314\python.exe -m pytest` (see `pyproject.toml`)

## Directory Map

### Root — Core Server

(also see **Top-Level Entry Points** for `server.py` and `admin_server.py`)

`config.py` — Configuration dataclass; env vars, config.json persistence, `_PERSISTED_KEYS`
`database.py` — SQLite schema; conversations, messages, summaries, branches, state tables
`prompt_engine.py` — System prompt assembly; style nudges, repetition detection, `assemble_prompt()`
`context_manager.py` — Token counting; context window management, incremental summarization routing
`local_llm.py` — Dispatcher for local-model backend (Weave/OODA); routes to llama_client
`local_summary.py` — Deprecated/retired stub; superseded by local_context_guard for local llama-server compactification
`local_tools.py` — Tool definitions for local-mode agents; read_file, write_file, list_dir
`character_loader.py` — Parse/save character, persona, and lore .md files with YAML frontmatter
`model_catalog.json` — Curated model definitions and native picker defaults; add models here, not to per-provider lists
`model_registry.py` — Shared selector/harness routes, metadata validation, browser bootstrap, and catalog projections
`model_context.py` — Registry-backed model classification and legacy context-threshold compatibility helpers
`replay_budget.py` — Fresh-agent payload admission; destination capacity, instruction/tool/output reserves; native resumes bypass the gate
`permission_policy.py` — Authoritative bridged-permission algebra and structural normalization; Lean-conformant decisions
`permission_risk.py` — Bounded explicit destructive-operation recognition for opt-in Trust/review; not arbitrary program safety analysis
`permission_rules.py` — Session-local workspace/branch-descendant/action templates; preview, matching, and revocation
`permission_commands.py` — Literal command/PowerShell templates; preserves argument and wrapper boundaries without evaluating scripts
`acp_permissions.py` — Per-run ACP tool identity/raw-input join; Goose ACP permission normalization, never display-title inference
`PERMISSION_POLICY.md` — Permission UX, profile matrix, Lean proof boundary, and staged migration plan
`provider_contract.py` — Provider-independent native-session boundary selection; prevents A → B → A from reviving a stale harness session
`continuity_engine.py` — Harness-neutral fresh-context/checkpoint planning; native continuation bypasses replay work
`context_runtime.py` — Shared handoff/manual-checkpoint binding, native retrieval renewal, budgets, and fail-closed limit enforcement
`turn_recovery.py` — Shared bounded overflow-continuation gate, recorded tool outcomes, teardown/stop checks and attempt-scoped tool identities
`child_context.py` — Child-owned event-prefix adapter for that same engine; explicit recovery, separate checkpoint/retrieval storage, no parent-tree replay
`context_scope_lifetime.py` — Ephemeral task ownership for retrieval capabilities; long turns retain access, completion/cancel/restart revoke it
`context_checkpoints.py` — Revision-bound checkpoint storage, cursor-checked manual boundaries and exact-branch evidence retrieval
`context_summary.py` — Deprecated/retired stub; local summarizer removed per CONTINUITY_REPAIR_PLAN.md
`CONTINUITY_REPAIR_PLAN.md` — Active repair scope, acceptance matrix, evidence ledger, and unresolved integration gaps
`event_router.py` — Typed WebSocket event envelopes and exact run subscriptions; generated content never falls back to conversation broadcast
`coordination.py` — Lean-conformant pure completion/teardown/queue decisions; `static/coordination.js` mirrors landed-view ordering
`agent_supervisor.py` — Durable Loom-owned child runs and event history, separate from the conversation message tree
`agent_events.py` — Bounded child-output persistence/delivery batches; control flushes and cancellation drain
`image_generation.py` — AGY image-job request validation, native-tool evidence, bounded raster verification; exposed by `mcp_loom_agents.py`
`loom_auth.py` — Loopback/Tailscale network gate, opt-in paired-browser cookie, machine relay secret (always required for admin approvals); agent-channel route allowlist lives in `server.py`
`workspace_safety.py` — Incremental external recovery blobs, immutable before/after manifests, and per-turn change reports
`workspace_integrity.py` — Shared structured-write preimages, one-time replacement review, stale-approval checks; limits in `WORKSPACE_INTEGRITY.md`
`workspace_fingerprint.py` — File identity/change metadata for cache reuse; Windows change-time and open-writer checks; failure forces rereads
`exo_state.py` — External machine-state root for features that must never write into a project checkout
`managed_extensions.py` — User-registered workspace manifest references plus verified generic process lifecycle
`search_space/` — Explicit workspace disclosure policy, external FTS index, locked local/cloud search operator
`loom_agent_prompt.py` — Shared Loom agent contract loader; `prepend_loom_agent_context()`, prompt merging
`loom_agent.md` — Plaintext Loom agent contract (read by loom_agent_prompt.py)
`skill_scanner.py` — Scan Claude Code skills, built-in commands, user skills for slash-command autocomplete
`canvas_slug.py` — Human-readable slug generator for canvas URLs (adjective-noun-suffix pattern)
`canvas_workspace.py` — Harness-neutral Canvas bootstrap, concise launch notices, safe root resolution, bounded metadata revisions
`ooda_harness.py` — OODA loop harness for Weave RP; XML parser, state executors, single-pass generation with repair fallback
`backstage.md` — Backstage feature description or design notes

### Root — Provider Client Adapters

`claude_client.py` — Claude Code CLI subprocess wrapper; NDJSON stream parser, fork-every-turn sessions; also the umans launch path (`use_umans=True`, same CLI at `api.code.umans.ai`)
`gemini_client.py` — Antigravity (agy) CLI subprocess wrapper; plain text mode, log error scanning
`llama_client.py` — llama-server client; OpenAI-compatible /v1/chat/completions, vision, model resolution
`ollama_client.py` — Staged for deletion (`D`); legacy Ollama adapter; **do not re-enable**
`vllm_client.py` — Staged for deletion (`D`); legacy vLLM adapter; **do not re-enable**

### Root — MCP Servers (stdio)

`mcp_loom_workspace.py` — MCP workspace server; file edits, shell commands, sensitive reads via Loom HTTP
`mcp_loom_actions.py` — Deprecated stub; raises RuntimeError (replaced by mcp_loom_workspace)
`mcp_loom_file_edits.py` — Deprecated stub; raises RuntimeError (replaced by mcp_loom_workspace)
`mcp_state_cards.py` — MCP server for Backstage agent; state-card CRUD scoped to parent conversation
`mcp_web_tools.py` — MCP stdio server; web_search (DuckDuckGo) + web_fetch (trafilatura) for local models
`mcp_search_space.py` — Locked Search Space facade; scoped natural-language search and excerpt retrieval only

### Root — Config and Data

`models_config.json` — Per-model llama-server tuning; context size, GPU layers, KV-cache quantization, flash-attn
`pyproject.toml` — Pytest config; `asyncio_mode = "auto"`, testpaths
`requirements.txt` — Python dependencies (fastapi, uvicorn, aiosqlite, httpx, llama-cpp-python, mcp, etc.)
`formal/permission_policy/` — Lean 4 policy specification, proofs, and generated exhaustive Python conformance fixture; development/CI only
`formal/coordination/` — Stateful lifecycle/view calculus, inductive safety proof and generated runtime oracle; see its README for the trust boundary

### Frontend — `static/`

(also see **Top-Level Entry Points** for `static/index.html`, `static/app.js`, `static/chat.js`, `static/tree.js`, `static/style.css`, `static/blackhole.js`, `static/canvas-sdk.js`)

`static/admin/index.html` — Admin dashboard SPA; sidebar nav, view panels, toast notifications
`static/admin/admin.js` — Admin client; instance CRUD, model-server controls, database storage
`static/admin/admin-supervision.js` — Activity tab; unified approvals, exact-instance navigation/actions, note-preserving metadata polling
`static/admin/admin-landed.js` — Admin-only recent completion inbox; explicit browser-local review marks, no chat/cursor writes
`static/admin/admin-extensions.js` — Generic UI for the extension registry and Start/Stop/Update actions
`static/admin/admin-openrouter.js` — Read-only daily/weekly/monthly usage and budget monitor, via Main's configured key
`static/admin/admin.css` — Admin dashboard styles; dark sidebar, card layout
`static/img/` — Static images; banner, favicon, background textures (stars, milkyway, spectra)

### Tests — `tests/`

`tests/__init__.py` — Package marker
`tests/conftest.py` — Shared fixtures; `tmp_database` (temp SQLite), basetemp redirect, autouse
`tests/test_anthropic_model_list.py` — Anthropic model list parsing tests
`tests/test_api.py` — REST API endpoint tests
`tests/test_codex_permissions.py` — Codex permission mode and approval mapping tests
`tests/test_context_generation.py` — Context window generation tests
`tests/test_database.py` — SQLite CRUD and tree operations tests
`tests/test_local_mode.py` — Local mode generation tests
`tests/test_loom_agent_prompt.py` — Loom agent contract injection tests
`tests/test_model_attestation.py` — Cross-provider harness/model identity and evidence-level tests
`tests/test_permission_policy.py` — Lean-fixture parity, policy invariants, normalized provider payloads, and sanitized shadow-audit tests
`tests/test_permission_workflows.py` — Counted, isolated cross-harness permission workflows; scope/lifecycle negative controls
`tools/permission_preflight.py` — Offline plus opt-in native acceptance runner; sanitized count report and tested-source digest, never live host lifecycle
`tools/lifecycle_preflight.py` — Isolated queue/tab/handoff/child acceptance and responsiveness report; optional real Codex process-tree checks
`LIFECYCLE_PREFLIGHT.md` — Acceptance coverage, metric definitions and limits; no production deployment or host control
`tests/test_ooda_harness.py` — OODA harness parsing and execution tests
`tests/test_operator_parity.py` — Operator parity tests
`tests/test_plan_approval_hook.py` — Plan approval hook tests
`tests/test_synthetic_replay.py` — Synthetic corpus replay tests
`tests/synthetic/` — Synthetic test harness; `generate_corpus.py`, `replay.py`, `score.py`, `REPORT.md`

### Content Directories

`characters/` — Character definition files (.md with YAML frontmatter: name, tags, personality, scenario, greeting)
`personas/` — User persona files (.md); player characters for RP
`lore/` — Lore/history context files (.md); world-building references
`templates/` — Chat template Jinja files for local models (e.g., qwen3.6-froggeric)
`tools/` — Dev/probe scripts, including sanitized permission audits
`canvas/` — Canvas workspace (generated per conversation; .gitignore inside)
`character-creator/` — Character creator workspace; CLAUDE.md, .gitkeep placeholder

### Utility Scripts (tracked)

`start_test_server.bat` — Windows batch to start test server
`stop_test_server.py` — Stop test server script
`test_ooda_live.py` — Live OODA test harness

## Subsystem Locator

| Concern | Where it lives |
|---------|----------------|
| LLM client adapters | `claude_client.py`, `codex_client.py`, `gemini_client.py`, `goose_client.py`, `llama_client.py` |
| Thread placement / page cursors | `database.py` — `loom_threads`, `loom_thread_cursors`, optimistic cursor revisions; `server.py` message contract fails closed on stale or missing topology |
| Active generations / session state | `server.py` — module-level `_active_generations` dict, thread-bound snapshots, `_reap_orphan_generations()`, `/api/generations` endpoints |
| Run event routing | `event_router.py` — typed envelopes and exact subscriptions; branch visibility/focus is never delivery authority |
| Model registry / discovery | `model_catalog.json` + `model_registry.py`; native CLI/OAuth discovery supplements defaults; `llama_client.py` scans local models; `models_config.json` remains local tuning |
| Model context + handoff gate | `replay_budget.py` — full fresh-payload budget, local per-slot runtime capacity; `model_context.py` — provider classification |
| Provider session continuity | `provider_contract.py` — nearest-assistant boundary contract shared by all agent handlers |
| Per-turn model identity | Provider clients emit `model_attestation`; `database.py` persists it on messages; `static/chat.js` renders harness + effective-model evidence |
| Workspace preservation | `workspace_safety.py` — external recovery snapshots and per-generation change reports |
| Workspace-owned extensions | `managed_extensions.py`, `EXTENSIONS.md`, `static/admin/admin-extensions.js` |
| Semantic workspace search | `search_space/`, `mcp_search_space.py`, `SEARCH_SPACE.md`; explicit cloud/sensitive routing |
| Context window management | `context_manager.py` — token counting and context sizing; in-flight compactification handled via `local_context_guard.py` |
| System prompt assembly | `prompt_engine.py` — `build_system_prompt()`, `assemble_prompt()`, style nudges |
| Database (tree storage) | `database.py` — SQLite schema, conversations/messages/summaries/branches tables |
| Configuration | `config.py` — `Config` dataclass, `config.json` persistence, env var defaults |
| MCP workspace coordination | `mcp_loom_workspace.py` — read/write/edit/bash/sensitive-read tools via Loom HTTP |
| Backstage state cards | `mcp_state_cards.py` — conversation-scoped state-card CRUD for backstage agent |
| Web tools (local models) | `mcp_web_tools.py` — DuckDuckGo search + trafilatura fetch MCP stdio server |
| Admin panel (backend) | `admin_server.py` — FastAPI on port 3002; instances, servers, ttyd, cron, tools |
| Admin panel (frontend) | `static/admin/index.html`, `static/admin/admin.js`, `static/admin/admin-supervision.js`, `static/admin/admin.css` |
| Permission gating | `cc_permission_hook.py` — PreToolUse hook for CC/agy/Codex; browser UI bridge |
| Permission policy | `permission_policy.py` (runtime/shadow normalizer), `formal/permission_policy/` (Lean proofs + generated cases), `database.py` (`permission_policy_audits`) |
| Loom agent contract | `loom_agent.md` (plaintext), `loom_agent_prompt.py` (loader + merger) |
| OODA harness | `ooda_harness.py` — single-pass generation: observe-orient-decide-act before RP prose, with repair fallback for malformed output |
| Slash commands / skills | `skill_scanner.py` — `BUILTIN_COMMANDS`, `get_all_skills()` scans CC skills |
| Canvas | `canvas_workspace.py` (setup/notices/revisions), `canvas_slug.py` (slug gen), `server.py` routes, `static/canvas-sdk.js` (bridge), `static/canvas-viewer.js` (standalone viewer), `static/tree.js` (meta-root nodes) |
| Character system | `character_loader.py` (parser), `static/app.js` (CRUD UI), `characters/`, `personas/`, `lore/` |

## How to Run It Locally

```powershell
# Install dependencies
C:\Python314\python.exe -m pip install -r requirements.txt

# Start main server (HTTPS on :3000)
C:\Python314\python.exe server.py

# Start admin dashboard (HTTPS on :3002) — optional
C:\Python314\python.exe admin_server.py

# Run tests
C:\Python314\python.exe -m pytest

# Open browser at https://localhost:3000
```

**Prerequisites per mode:** Claude Code CLI on PATH (Loom), `llama-server` + GGUF (Braid/Weave), `codex` CLI (Codex), `agy` CLI (Antigravity), `goose` CLI (Goose ACP). See `README.md` for provider-specific setup and MCP registration.

## Conventions Worth Knowing

- **UI typography and color:** Use Space Grotesk through `--font-main` (Main) / `--font-body` (Admin) for ordinary UI text, controls, and section labels; honor the user's font setting. Reserve Orbitron / `--font-display` and ice-chrome rendering for stylized display elements. Folder section labels use a uniform 18px, weight 600, and `--text-secondary` (soft lavender in ExoLoom). Use existing theme tokens rather than introducing independent fonts or hardcoded text colors. Symbol fonts and code monospace are functional exceptions.

- **Python interpreter** is `C:\Python314\python.exe`; bare `python` may resolve to a dep-less python-manager install
- **Client adapter naming** is `<provider>_client.py` — each wraps one provider CLI or protocol
- **Config** lives in `config.json` (user-editable, gitignored) loaded by `config.py`; per-model tuning in `models_config.json` (tracked)
- **Database** is SQLite with WAL mode; `database.py` uses `aiosqlite` for async access
- **Frontend** has no build step — plain HTML/CSS/JS served static from `static/`
- **Test fixtures** use `conftest.py`'s `tmp_database` fixture (temp SQLite file per test)
- **Branch naming** uses Unicode Greek letters (α, β, γ, …) with numeric suffixes; double/triple Greek for large trees
- **Session model** is fork-every-turn: each CC generation gets its own immutable session snapshot
- **Generation** survives restarts via progressive drafts — draft message created immediately, updated as tokens arrive
- **Deprecated MCP** files (`mcp_loom_actions.py`, `mcp_loom_file_edits.py`) raise `RuntimeError` — do not re-enable

Hermes, Dream Space, and Prometheus launch paths are retired. Keep historical identity/session labels readable; never silently migrate those conversations. Dream through Claude Code/Goose and Weave remains supported.

## Needs Human Review

- `ollama_client.py` and `vllm_client.py` are staged for deletion (`D`); Ollama and vLLM are deprecated — llama-server is the only local LLM backend
- `start_test_server.bat` and `stop_test_server.py` are tracked but not referenced in README; purpose unclear
- `backstage.md` is tracked; may be a design doc or scratch — skip or add description

## Publication checkouts

If `.publication/RESUME.md` exists, read it and its linked skill before preparing or publishing changes. Its local review gates supplement this map.
