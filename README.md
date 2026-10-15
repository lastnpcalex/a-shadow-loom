<p align="center">
  <img src="static/exoloom/reference.png" alt="EXOLOOM — cyan black hole above a reflected grid in a purple nebula">
</p>

<p align="center"><img src="static/exoloom/wordmark.png" alt="EX umbra Omnia" width="900"></p>

# EX umbra Omnia

![Python](https://img.shields.io/badge/python-3.12+-blue)

**EXO** is a self-hosted web interface for branching AI conversations across Claude Code, ChatGPT Codex, Antigravity (`agy`), Goose ACP, direct OpenRouter, Dream, and local llama.cpp models — and for handing off between them mid-conversation. Every conversation is a tree: branch, fork, regenerate, and full-text search across every path without losing anything.

Browser tabs use `EXO: <workspace title>` (the conversation's display title). The home page uses the full name. Existing repository URLs, `Loom` mode/API names, `LOOM_*` settings, databases, and recovery locations keep their names for compatibility; this rename requires no data migration.

Choose **Settings → Appearance → Exoloom** for the animated portal black hole and metallic lettering. **Admin → Settings → Background studio** configures the shared orbit, camera, disk, nebula and floor preset with a preview and explicit Save. Main has simple browser-local appearance options. Legacy Loom remains available; reduced-motion browsers use the still artwork. Asset provenance and bundled font licenses are in [static/exoloom](static/exoloom/SOURCE.md).

Hermes, its Dream Space mode, and Prometheus are retired. Existing conversations and model labels remain readable. To continue one, explicitly choose and save a supported harness/model in conversation settings. The external Hermes installation and personal state are not removed. The Dream model server and its Claude Code/Goose/Weave routes remain supported.

## Project status and implementation model

EX umbra Omnia is an active experimental project. The harnesses in this repo are
WIP operational integrations, not polished product SDK wrappers. Expect sharp
edges, frequent schema and workflow changes, and local state that may need
operator attention while features are being hardened.

The provider integrations are built around the tools you would run from a
terminal, not official application SDKs:

- **Claude** uses the Claude Code CLI as a subprocess.
- **Codex** uses the ChatGPT Codex CLI/app-server flow.
- **Antigravity/Gemini** uses the `agy` CLI.
- **Goose** uses Goose over ACP and can target OpenRouter, llama-server, or Dream.
- **OpenRouter** can also be used directly through the Claude Code-compatible shim.
- **Braid/Weave** use local `llama.cpp` / `llama-server`.
- **Dream** is a DiffusionGemma model server used by Claude Code, Goose, and Weave.
  the authority boundary.

That design is deliberate: Loom is a branching UI, state store, permission
bridge, and orchestration layer around active agent tools. It does not replace
those tools' own auth/session behavior, and it does not guarantee that
experimental provider CLI behavior will remain stable.

The repository is the source-code boundary, not a backup of a running Loom
installation. A clone contains the server, UI, provider adapters, schema
migrations, tests, and default content. Git deliberately excludes credentials,
`config.json`, SQLite databases, uploads, generated workspaces, certificates,
local model binaries, provider home directories, and recovery artifacts.

## What is a loom?

An LLM loom treats every conversation as a **tree, not a thread**. Each message is a node. At any point you can branch — regenerate, edit, fork — and explore alternate paths without losing the originals. The metaphor comes from weaving: every response is a thread, and the loom holds them all in tension so you can compare, backtrack, and choose.

This matters because LLM output is non-deterministic. The same prompt can produce a brilliant answer on one roll and a mediocre one on the next. A linear chat hides that variance — you see one path and lose the rest. A loom preserves them all. Regenerate five times, keep the best, branch from the second-best later. Edit a message from ten turns ago and watch the conversation diverge. The tree is the conversation's real shape; a single thread is just one path through it.

## The point of the system

Different work wants different engines. Frontier Claude for hard problems, a local model for private or always-on work, an RP harness for creative writing, a locked-down operator for tasks where the model must not freelance. Normally each of those is a separate app with separate history, separate UI habits, and no way to move a conversation between them.

EX umbra Omnia runs them all on one branching infrastructure: one database, one tree UI, one search index, one set of chat habits. Start a refactor on a frontier model, branch the conversation, rerun the branch on a local model, compare. Search across months of agent sessions regardless of which engine produced them. The loom is the constant; the engines are interchangeable shuttles.

## Six spaces, multiple provider lanes

### Loom — Claude Code in the browser

Connects to the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) as a subprocess. Full tool suite with streaming responses, thinking blocks, and permission proxying.

- Tool call blocks with expandable input/output and success/error indicators
- Edit tool diff rendering (red/green inline diffs)
- Collapsible extended thinking display
- Permission proxying — tool approvals appear in the browser UI
- A shared model picker for Anthropic, Antigravity/Gemini, ChatGPT Codex, Goose ACP, direct OpenRouter, Dream, and enabled compatibility providers — changeable mid-conversation
- Per-turn model attestation — the UI records the harness, requested model, launch model, effective model, evidence source, and verification level when the provider exposes them; configured-only and mismatch states remain visibly distinct from verified runtime evidence
- Thinking effort control for Anthropic and Codex models (`max` is Opus-only, `xhigh` is Opus/Codex)
- Plan/Act mode toggle
- Immutable session snapshots — every turn forks the CC session, enabling clean branching at any point
- Progressive draft saving — partial output survives navigation and reconnects; after a Loom host restart, an interrupted turn is retained or marked orphaned rather than silently resumed
- Per-turn and cumulative cost tracking
- Slash commands — Claude Code skills are scanned and exposed with autocomplete

### Adding models

Curated models live in `model_catalog.json`. Add one entry to its `models` array;
the shared registry supplies picker choices, Python/browser routing, context
budgets, and configured identity metadata. For example (fictional ID — verify an
actual provider ID and supported context before adding it):

```json
{
  "backend": "openrouter",
  "id": "example/model-id",
  "label": "Example Model",
  "context_length": 262144,
  "capabilities": {"tools": true},
  "routes": ["openrouter", "goose", "goose_smart", "weave"]
}
```

`openrouter` is Claude Code through OpenRouter; `goose` and `goose_smart` are Goose
ACP routes; `weave` keeps its existing chat-completion interface. Native pins can
use backend/route pairs `anthropic`/`anthropic`, `openai`/`codex`,
`antigravity`/`gemini`, or `umans`/`umans`. Use the exact provider ID, not a display
name, for a new pin. A genuinely new backend/harness still needs an adapter.

Optional fields: `aliases` (historical OpenRouter aliases), `handoff_threshold`
(legacy compatibility; otherwise derived from context), and boolean capability
metadata (`tools`, `vision`, `effort`, `xhigh`, `max`). Explicit effort flags drive
the picker controls; omitted flags retain existing harness behavior. Tool/vision
metadata does not create capabilities the harness or backend lacks. Catalog
identity is configuration, never evidence for a green verification check.

Native discovery and saved selectors remain intact. Runtime context wins over
catalog values; local GGUF tuning stays in `models_config.json`, and llama-server
uses its live per-slot capacity. Ordinary native resumes do not run replay checks.
Catalog definitions load with the Python process; activate edits during a
human-controlled host reload, then refresh the tab. No frontend rebuild or
per-model JavaScript edits are needed.

### Braid — Claude Code on local models

The full Claude Code harness running against a local llama.cpp `llama-server`. Same tools, same permissions, same UI — just running on your hardware.

- Full Claude Code tool suite (Bash, Read, Write, Edit, Grep, etc.)
- Web search and page fetch via the bundled DuckDuckGo/trafilatura MCP server
- Permission prompts proxied through the browser UI
- Works with compatible tool-capable GGUFs and chat templates; 64k+ context is recommended

### Weave — structured roleplay and creative writing

Character cards, personas, lore files, style nudges, and incremental summarization. A full RP harness on local models with context management that scales beyond the model's native window.

- Character system with personality, appearance, goals, relationships, scenario, greeting, and example messages
- Personas (player characters) and lore for richer world-building context
- Style nudge rotation and repetition detection
- Thinking model support (`<think>` stripping, content token counting)
- Multi-branch generation — 1-5 parallel responses per turn, pick the best
- Backstage — a side conversation with an agent that edits state cards while the RP stays untouched

#### OODA Harness

The OODA (Observe-Orient-Decide-Act) harness is cognitive scaffolding that guides the model through a structured reasoning loop before writing each response. Inspired by [metacog](https://github.com/inanna-malick/metacog) (tools as cognitive scaffolding — LLMs treat tool results as ground truth) and [popup-mcp](https://tidepool.leaflet.pub/3mcbegnuf2k2i) (amortize latency into fewer, richer passes).

**The loop:** Before generating RP prose, the model emits a structured `<ooda>` block. The server parses it, executes the state operations against the database, and returns the results. The model then writes its prose grounded in fresh state reads rather than stale context.

1. **Observe** — read the current state of characters, scenes, and relationships from the database
2. **Orient** — reason about what changed, how characters would react, what the scene demands
3. **Decide** — plan the response: what happens, who speaks, what shifts
4. **Act** — execute state updates (mood changes, location shifts, relationship evolution) and write the prose

Each step appears as a collapsible tool block in the conversation, so you can see exactly what the model observed, how it reasoned, and what state it changed.

**State cards** track the evolving state of the RP — character state, scene state, persona state, and read-only lore — in a three-tier hierarchy:

1. **Tier 1 (Character Global)** — baseline cards defined on the character itself, editable from the home page. The template copied when a character enters a conversation.
2. **Tier 2 (Conversation)** — copied from Tier 1 when OODA is enabled. The pristine base state.
3. **Tier 3 (Branch Deltas)** — state changes are saved as deltas on each assistant message. Different branches see different state; navigating a branch reconstructs base cards plus deltas along the path.

State cards are editable inline — click any field to edit, changes auto-save, the model reads them next turn.

## How the harnesses interoperate

The modes are different engines, but the loom around them is shared — which is what makes mid-conversation handoff work:

- **One tree, one store.** Every mode writes the same message-tree schema in SQLite. Branching, bookmarks, search, import/export, and the tree visualization don't care which engine produced a node.
- **Cross-provider switching.** A Loom conversation can move between Anthropic, Gemini, Codex, Goose, OpenRouter, Dream, and local models mid-conversation. Provider-native resume is allowed only at a compatible nearest-assistant boundary. A provider or model boundary forces a bounded rebuild from Loom's message tree instead of searching farther back for a stale native session.
- **Per-turn provenance.** Each assistant turn stores its harness/model attestation alongside the message. A green verified badge means Loom received runtime evidence from the harness or provider response; a configured or unverified badge is not presented as proof, and a mismatch is surfaced explicitly.
- **One chat surface.** Highlight-to-reply (select text in any message, quote it in your next send), attachments, image paste, message queuing, and slash commands work identically in every mode, because the excerpt and the attachments ride in the message itself rather than in provider-specific plumbing.
- **One permission surface.** Claude Code, Codex, and Goose approvals are bridged into Loom's browser UI. Antigravity runs with provider-native approvals skipped so Loom remains the approval layer where the adapter supports it.
- **One canvas.** Any mode can drive the Interactive Canvas; the AI writes files, the iframe live-refreshes.

> **Note:** `AskUserQuestion` is disabled in CC modes. CC's headless `-p` mode has no mechanism to send user responses back to an active `AskUserQuestion` call — stdin is closed after the initial prompt ([open feature request](https://github.com/anthropics/claude-code/issues/16712)). Until then, CC proceeds with its best judgment instead of asking.

## Parallel agents and workspace integrity

Agent conversations with a working directory can opt into **Parallel agents in
same checkout**. This is shared-checkout concurrency, not automatic Git
worktrees: independent generations can run at the same time against the live
files, and each launch is tied to an explicit message-tree parent. The option
conversations.

Before each tool-capable conversation turn, Loom takes a recovery snapshot of the
workspace outside the checkout and reports post-turn changes back to the UI.
Set `LOOM_WORKSPACE_RECOVERY_DIR` to choose that external storage location.
Snapshots are incremental: a persistent, workspace-specific index reuses existing
recovery blobs when file identity, size, modification time, and change-time
metadata match. Windows uses `FILE_BASIC_INFO.ChangeTime`, not Python's creation
time; unavailable metadata or missing/truncated blobs force a fresh read.
This relies on the filesystem reporting file changes accurately; it is not a
transactional snapshot of an entire concurrently edited directory.

The first capture reads the selected files; subsequent captures and finalization
read only cache misses. Exact live bytes (including CRLF, dirty/staged edits and
untracked files) are stored outside Git, so recovery does not depend on HEAD or
the index surviving. Every turn retains an independent before manifest and full
after manifest; replacing the mutable cache cannot erase an earlier turn.
Existing version-1 manifests remain readable. No files are restored automatically.

The 8 MiB per-file and 256 MiB new-read limits are checked before content reads;
reused blobs do not consume that new-read budget. Dirty files are prioritized.
Oversized, unreadable, or unstable files are listed as skipped, and launch status
explicitly says **recovery partial**. Read/reuse/byte counters are saved in the
manifests and reported with preparation status. Ignored files and excluded build
directories remain outside coverage, as before. Recovery storage inside the
workspace is rejected to prevent the archive from snapshotting itself.
Snapshots preserve recovery blobs and expose churn; they do not serialize
agents, automatically merge conflicts, or authorize overwriting another
agent's work. Destructive Git operations still go through Loom's permission
flow.

### Run-scoped event bus and child agents

Loom's WebSocket events carry a typed envelope with conversation, run, draft,
parent, browser-client, event-kind, and sequence identifiers. Stream, tool,
permission, and workspace-change events are delivered only to sockets that
explicitly subscribe to that exact run. Branch visibility is a navigation hint,
not an ownership claim; opening or focusing another window cannot hijack a
stream. Global notifications are deliberately content-free.

Any ordinary provider turn can request a supervisor-owned child run through the
`loom-agent-control` MCP server (`spawn_agent`, `list_agents`, `get_agent`,
`wait_agent`, `cancel_agent`, and `resume_agent`). Child runs are persisted in `agent_runs` and
`agent_events`, run outside the parent conversation tree, and continue when the
parent model turn ends. Codex, Claude Code (including OpenRouter, Umans, and
Dream-shim routes), Antigravity/Gemini, Goose, and local profiles
use the same lifecycle and run-scoped routing. The browser's
composer-adjacent **Agents** panel shows each child harness and effective model,
live output, hierarchy, and cancellation without appending child output to chat
messages.

Child output deltas are persisted in ordered batches (50 ms, 64 events, or
64 KiB, whichever triggers first), retaining individual event IDs and sequences.
Lifecycle/control events flush pending output without waiting for the timer;
bridged permissions remain immediate. Normal completion/cancellation drains
pending output. An abrupt host/process crash can lose the not-yet-flushed output
tail; this is not a write-ahead guarantee for each individual token.
Batch-capable browsers receive one frame per output batch; older open tabs retain
the individual-event format until refreshed. Reconnect replays missing events
in pages from the last contiguous sequence, including children that finished
while disconnected. The Agents panel updates cards in place, retains open cards
and scroll positions, and skips card painting while closed or backgrounded.

Child tasks are intentionally self-contained: Loom does not copy the parent
transcript into them or automatically inject their results back into the parent.
The parent uses `wait_agent`/`get_agent` when its answer depends on a child.
Omitting `harness`, `model`, and `profile` inherits the calling agent's target
(the conversation for top-level calls), captured when the job is queued.
`profile="local"` explicitly selects a local model through Claude Code's
tool-capable llama-server route; Weave's chat-completion interface is unchanged.
Nested children inherit or narrow their parent's workspace paths. A validated
ancestor/descendant chain shares its reservation so a waiting parent cannot block
its own child. Overlapping siblings and unrelated jobs still serialize; disjoint
scopes may run concurrently. Parents must not edit paths delegated to active children.
`LOOM_AGENT_MAX_CONCURRENT` (default 4) limits independent leaf jobs, not coordinating
ancestors. `LOOM_AGENT_MAX_DEPTH` (default 4) bounds nested parent edges, allowing
at most five managed processes per leaf with those defaults. Parent completion or
cancellation does not cancel durable children. A Loom shutdown pauses active child
jobs. Explicit **Resume** prepares bounded context from that child's original task
and recorded output/tool outcomes through the shared continuity engine; it does
not copy the parent transcript or silently replay completed work. Uncertain tool
outcomes require reconciliation before repeating side effects. Image jobs retain
their separate verified artifact/relaunch workflow. Provider-native subagents are
ephemeral and do not appear in this panel.

Stopped failed children also expose **Resume**, after verified process exit.
Failure never authorizes an automatic retry. Cancelled runs and runs retaining
a provider PID cannot be recovered this way; duplicate Resume requests have one
atomic admission. Recovery retains the event history and checks uncertain action
outcomes instead of assuming the last tool either succeeded or failed.

### Images through Antigravity

Ordinary tool-capable Loom agents can call `loom-agent-control.generate_image`
with a prompt and a new workspace-relative `output_path` (PNG, JPEG, or WebP).
Optional `reference_paths` accepts up to four existing workspace images; describe
their roles in the prompt. The call queues an Antigravity child and returns its
run ID immediately. Use `wait_agent`, then `get_agent`; successful jobs return
`result.artifacts` with a path, preview URL, dimensions, encoding, byte count,
and SHA-256. The server requires both a successful native `generate_image` call
and a decodable output file. Missing/corrupt files and prose-only success fail.

Example tool call: `generate_image(prompt="A mountain observatory beneath a teal aurora", output_path="assets/generated/observatory.png")`.
Retain its `run_id` for `wait_agent`, `get_agent`, and `cancel_agent`.

Image jobs use the existing AGY login, quota, permission bridge, Agents panel,
scope reservations, and cancellation. Their running limit is ten minutes.
Output existence and workspace containment are checked at admission and again
at launch; choose a new filename for each request, including retries after a
partial result. References also participate in scope reservations. The default
AGY agent is `gemini-3.8-flash-high`; the optional `model` argument changes the
agent selector, not the native image model. Weave's separate chat-completion
interface and restricted operator/backstage tool sets do not gain this tool.

Deployment requires a Main reload through the human-operated admin UI for the
image-request schema and job handling, followed by fresh provider sessions/MCP
connections to discover the new tool and contract. Native resumes may retain
older tool registries or instructions. No host lifecycle action is performed
by the agent implementing this feature.

Validated on 2026-09-13 using AGY `gemini-3.8-flash-high`: managed probe
`52645aca-3e44-4b6e-945a-79cf0e8360ee` generated a 1024x1024 PNG in 113 seconds;
`604a5436-8c01-49ee-b1d9-9f8f2db2b114` used the dedicated tool's task template
and completed in 31 seconds. The native tool events and saved output passed
the artifact validator. These were live child runs through the existing host;
the new request handler/lifecycle is covered by isolated tests, not a reload
of the active host. Probe images remain under ignored
`canvas/agy-image-probe-20260913/`.

## Search

Every message across every conversation is searchable from the home page. Type a query, get highlighted snippet results grouped by conversation, click to jump directly to the matching message on its branch.

This is particularly useful for **Claude Code sessions**. Loom mode stores every tool call, every thinking block, every response in SQLite — an indexed, searchable archive of your entire CC history that you can branch from at any point. Find that one-off bash command from two weeks ago, locate the conversation where you debugged that migration, pull up every time the agent touched a specific file.

There's also **per-conversation search** and **tree search** (find and navigate between matching nodes on the visual tree).

**Search Space** adds an opt-in natural-language search across workspace groups.
Each workspace is explicitly classified as Excluded (the default), Cloud search,
or Sensitive/local-only. Cloud-only selections use OpenRouter GLM 5.3 Flash; any
sensitive workspace forces the complete request through llama-server and fails
closed if the local server is unavailable. This classification controls only
cross-conversation indexing/search, not which models may operate in ordinary
branches. The FTS index stays in machine-local state outside the repository and
is namespaced by the active Loom database. See [SEARCH_SPACE.md](SEARCH_SPACE.md)
for the threat boundary and current retrieval limitations.

## Interactive Canvas

Any conversation can enable an **Interactive Canvas** — a live website (HTML/CSS/JS) rendered within the Loom UI that the AI builds and maintains through chat. Toggle it with the Canvas button.

The canvas appears as a **meta-root node** in the tree visualization — a glowing thumbnail positioned above all branch roots, connected by bezier curves. Click it to open the fullview, where the iframe fills the screen while the chat bar stays visible.

- Live refresh — visible foreground Canvas frames check saved file metadata every three seconds, independent of the writing harness; existing WebSocket hints can refresh sooner
- Progressive LOD — the thumbnail blurs as you zoom out, with a pulsing beacon at maximum zoom
- Zero-config — enabling Canvas creates missing `index.html`, `CLAUDE.md`, `CANVAS_GUIDE.md`, and `.gitignore` files; it preserves existing content. All tool-capable harnesses, including Goose, receive a short path notice. No separate app server is required

Canvas sends use normal branch, queue, and permission rules; they do not implicitly start parallel work. The standalone slug is a viewer without a chat cursor: open the conversation in Loom to send SDK messages. Refresh scans are metadata-only and capped at 2,048 entries; see [CANVAS_GUIDE.md](CANVAS_GUIDE.md) for limitations. Canvas pages are trusted same-origin project code, not isolated untrusted content.

### Canvas SDK

Canvas pages can talk back to Loom through a postMessage bridge:

```html
<script src="/static/canvas-sdk.js"></script>
<script>
  Loom.send("Update the chart with the latest data");
  Loom.uploadAndSend(file, "Analyze this CSV");
  const prompt = await Loom.loadTrigger('analyze', { filename: 'data.csv' });
  Loom.dropZone(element, { trigger: 'process' });
  // Loom refreshes visible Canvas frames when their files change.
</script>
```

| Method | What it does |
|--------|-------------|
| `Loom.send(content, opts)` | Send a message and trigger generation. Options: `imagePaths`, `parentId` |
| `Loom.getConvId()` | Get the current conversation ID |
| `Loom.upload(file)` | Upload a file to the conversation |
| `Loom.uploadAndSend(file, content)` | Upload a file and send a message referencing it |
| `Loom.loadTrigger(name, vars)` | Load a prompt template from `canvas/triggers/{name}.md` with `{{variable}}` interpolation |
| `Loom.dropZone(element, opts)` | Wire drag-and-drop — uploads files and sends a trigger prompt |
| `Loom.on(event, handler)` | Listen for Loom events (`canvas_updated`, `message-sent`, ...) |

## Common features

All modes share the same conversation infrastructure:

- **Tree-based conversations** — every message is a node. Branch at any point, explore alternate paths, switch between branches. Branch names use Unicode Greek letters (`α2.β1.ε6`), extending to double/triple Greek for large trees.
- **Highlight to reply** — select text in any message bubble and a floating Reply button pins the excerpt above the input; the next send carries it as an attributed quote.
- **Fork and branch** — fork any message into a new conversation; regenerate creates a sibling branch; edit any message (including the root) to diverge.
- **Ghost nodes** — active generations appear as pulsing nodes on the tree in real time.
- **Tree visualization** — interactive pan/zoom canvas, horizontal or vertical layout.
- **Bookmarks** — bookmark any message or branch.
- **Streaming generation** — real-time token streaming over WebSocket with live token rate and tool indicators.
- **Background generation** — navigate away mid-generation and come back; responses save progressively and survive reconnects and tab switches. A host restart terminates the provider process, but persisted partial work is retained or marked as an orphaned generation.
- **Notifications** — bell dropdown for landed branches and permission requests; browser push when the tab is in the background.
- **Cron jobs** — schedule a script against a conversation on an interval through the existing cron API; the unused admin Cron tab is retired.
- **Image handling** — clipboard paste, attachments, vision-model description for local modes, inline display of images the agent generates.
- **Message queuing** — send your next message while the model is still responding.
- **Per-tab state** — each browser tab remembers its own conversation and view.
- **HTTP or HTTPS / Tailscale** — binds `0.0.0.0`; HTTPS is enabled only when both configured certificate files exist, otherwise Loom starts over plain HTTP.
- **WebGL black hole** — Schwarzschild raytracer background with procedural galaxy texture and glassmorphism UI.

## Provider sessions and streaming recovery

- **Claude fork-every-turn** — Claude Code generations use `--resume <parent_session> --fork-session`. Each assistant message gets its own immutable native session snapshot.
- **Shared resume contract** — every provider stops at the nearest prior assistant boundary. Foreign, legacy-unscoped, unverified failed, empty, or incompatible boundaries rebuild from Loom history instead of reviving an older session. A failed turn can continue natively on the next user-directed turn only with matching session/harness/model evidence and a confirmed transient-failure terminal response. Ambiguous exits and invalid/context-overflow sessions still rebuild. Codex, Claude Code, and Goose expose this terminal evidence; adapters without it remain conservative.
- **Partial-failure recovery** — partial text and captured tool outcomes remain available on rebuild, including an explicit warning for tools without a captured result. Loom does not automatically replay a failed attempt after text, thinking, or tool activity. Failed turns remain failed for queue scheduling; queued successors stay paused. An empty rejected native resume can fall back once to a fresh, budgeted replay.
- **Fresh payload admission** — `replay_budget.py` estimates the complete rendered prompt and supplied instructions, plus tool/hidden-instruction, attachment, output and tokenizer reserves. Local llama launches prefer the live per-slot `/props` capacity; remote launches use available harness configuration or model metadata with labeled conservative fallbacks. Hidden CLI/MCP schemas are reserved estimates, not exact tokenizer counts or verified model limits. Oversized replays compact a compatible source when available or stop before sending the model prompt, with an actionable capacity error; history is never silently truncated to fit. Normal native resumes/forks bypass this preflight entirely. Weave's chat-completion context handling is unchanged.
- **Progressive drafts** — a draft message is created immediately, updated as text, thinking, tools, usage, and model attestation arrive, then finalized on stream end.
- **Reconnect recovery** — active in-memory generation snapshots rebuild the visible stream after WebSocket reconnects. Persisted database drafts preserve completed partial output across process failure, but the terminated provider process itself is not resumed after a host restart.

## Admin dashboard

A separate admin server (`admin_server.py`, port 3002) runs a single-page dashboard for operating the whole stack:

- **Overview** — instance and model-server lifecycle controls, database storage, live CPU/RAM/VRAM specs, and an Activity shortcut.
- **OpenRouter monitoring** in Overview — daily, weekly, and monthly API-key spend,
  configured EXO weekly/monthly budgets, provider key cap/reset/remaining credit,
  and optional account credit balance. Reads Main EXO's loaded usage endpoint;
  shared-key harnesses contribute to the same totals. EXO budgets govern direct
  API requests; the provider key cap governs all traffic using that key. A daily
  key cap is shown when configured; no new spending limit is imposed. Refreshes
  at most once per minute while Overview is visible, with manual refresh and
  explicitly stale last-known counters during outages.
- **Activity** — unified permission requests and tracked runs across all configured, reachable EXO instances. Includes preparing turns and Loom-managed children, workspace/model identity, and exact conversation/branch/chat links. Approve once, use eligible “Allow for Branch” rules, or deny with an optional note using the same resolver as chat. Tracked assistant turns have an instance-scoped Cancel control.
- **Extensions** — register a manifest path from another workspace and manage it
  without adding a bespoke EXO service card. The registry stores a reference,
  not a copy; Start/Stop/Update use the owning workspace's argument-array
  commands and verified process identity. See [EXTENSIONS.md](EXTENSIONS.md).
- **Guide** — operating reference. The unused Terminal, Tools, and Cron tabs are removed; their backend APIs and any scheduled jobs are not deleted or stopped.

Activity polls metadata rather than conversation transcripts, preserves notes and expanded request details, and disables last-known requests when an instance is unreachable. It never changes another tab's cursor. Admin approvals are privileged operations: keep the admin on a trusted network/access-controlled proxy. Same-origin admin requests relay through loopback-only main-server endpoints; this is not a new public authentication system.

**Landed generations** in Activity is an admin review inbox, including finishes while the dashboard was closed. It shows the latest landed turn per root thread, grouped by workspace and isolated by instance/conversation. Continuing a thread replaces its entry; parallel root threads keep separate entries. Root grouping happens before the 100-entry limit so a busy thread cannot crowd out other roots. Durable child-job results remain separate entries. Exact branch/chat links open the displayed completion. Reviewing it hides that thread's entry, not merely the newest row in a stack of older turns; a newer landing becomes unreviewed again. Turns with final timing but no recorded outcome say **Finished — outcome not recorded**; estimated completion times are labeled. Active tasks and unreleased leases stay out of the inbox. Missing history support or an outage is shown as unavailable, never inferred completion. This is not the full conversation archive.

**Mark reviewed** and **Show reviewed** affect only this browser's admin inbox. Opening a link does not mark it reviewed, change another tab's branch, delete a message, or acknowledge anything to an agent. Up to 2,000 review identities are kept in local storage; clearing storage or using another browser/device starts a separate review list. No transcript/tool output is stored there. The panel uses existing admin styles and the read-only supervision projection; main-server generation/queue/provider behavior is unchanged. Refreshing the admin page loads frontend changes; Python query changes require the main server to load the new code. Restarting admin alone cannot update main-server Python code.

**Restart Admin** replaces only the dashboard backend; **Restart** on the Main EXO card controls the separate chat server. The dashboard verifies the current admin PID before sending one restart request, then requires a different serving PID before reloading. Missing responses do not trigger automatic retries. Unconfirmed restarts and errors remain visible beside the admin controls; helper PID/exit status is available in `/api/status`, with restart diagnostics in `admin_server.log`.

If Activity reports HTTP 404, the loaded admin backend does not support the current frontend. A browser refresh cannot load new Python routes. Activity reports unavailable data explicitly instead of implying there are no running tasks. Loading the updated admin backend remains a human-controlled lifecycle action; it does not require restarting the main chat server.

llama-server is managed like a service: per-model context size, GPU layers, KV-cache quantization, flash attention, and mmproj are stored per GGUF and applied on launch, restartable from the Loom settings panel or the dashboard.

## Fresh-machine setup

The current deployment is developed and tested primarily on Windows with
Python 3.14. Python 3.12+ is the intended baseline. `requirements.txt` uses
minimum compatible versions; it is not an exact dependency lock file.

```powershell
# Clone
git clone https://github.com/lastnpcalex/a-shadow-loom.git
cd a-shadow-loom

# Create an isolated environment and install dependencies
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Run the main server
.\.venv\Scripts\python.exe server.py
```

On POSIX systems, use `.venv/bin/python` in place of
`.\.venv\Scripts\python.exe`; the admin tooling and current deployment remain
Windows-oriented.

Open the URL printed at startup. With no certificate files, the default is
`http://localhost:3000`; when both configured certificate files exist, it is
`https://localhost:3000`.

The base web application can start without every provider installed. Each
provider lane has its own runtime prerequisite:

| Lane or space | Runtime requirement |
|---------------|---------------------|
| Claude Code | Authenticated `claude` CLI on `PATH` |
| ChatGPT Codex | Authenticated `codex` CLI; Loom uses its app-server protocol |
| Antigravity | Authenticated `agy` CLI |
| Goose ACP | Goose installed (`goose` on `PATH` or `GOOSE_EXE`); enabled by default with `LOOM_ENABLE_GOOSE` |
| Direct OpenRouter | `OPENROUTER_API_KEY`; the optional management key enables usage/key-management features |
| Braid / Weave | `llama-server`, a compatible GGUF, and the appropriate chat template |
| Dream model server | DiffusionGemma sidecar/model and the relevant `DREAM_*` paths |

For Braid, Weave, and Goose local targets, point `llama_server_exe` and
`llama_models_dir` at the local installation in Settings → Advanced or
`config.json`, select a model, and launch llama-server from Loom or the admin
dashboard.

Loom injects the bundled `mcp_web_tools.py` server into supported local,
Goose and operator sessions. A global `claude mcp add` registration is
not required.

The optional admin dashboard runs separately:

```powershell
.\.venv\Scripts\python.exe admin_server.py
```

It prints either `http://localhost:3002` or `https://localhost:3002` depending
on whether the certificate files exist.

### What a clone does not reproduce

A fresh clone represents the tracked Loom code, but not the exact operational
state of an existing machine. Provision these separately when moving an
installation:

- `.env`, API credentials, provider authentication, and provider home/config directories
- `config.json` and machine-specific executable/model paths
- Loom SQLite databases, uploads, generated canvases, characters, personas, and lore
- SSL certificates, GGUF/model weights, llama-server, Dream, Goose, Codex, Claude, and `agy` installations

Copy secrets through an appropriate secure channel, not through Git.

## Project structure

```
server.py              -- FastAPI server, WebSocket streaming, REST endpoints
database.py            -- SQLite schema, message tree CRUD, branch management
config.py              -- Configuration (llama-server, context budget, SSL, generation)
prompt_engine.py       -- System prompt assembly, repetition detection, style nudges
context_manager.py     -- Token counting, context window management
ooda_harness.py        -- OODA loop: XML parser, state executors, prompt builder
character_loader.py    -- Parse/save character, persona, and lore .md files
llama_client.py        -- llama-server client (OpenAI-compatible chat + vision)
claude_client.py       -- Claude Code CLI subprocess wrapper, NDJSON stream parser
codex_client.py        -- ChatGPT Codex app-server wrapper
gemini_client.py       -- Antigravity (agy) subprocess wrapper
goose_client.py        -- Goose ACP client and permission bridge
dream_client.py        -- Dream sidecar client
openrouter_client.py   -- Direct OpenRouter client, secrets, usage, and limits
provider_contract.py   -- Cross-provider native-session boundary rules
workspace_safety.py    -- External recovery snapshots and post-turn change reports
exo_state.py           -- External machine-state paths shared by modular subsystems
managed_extensions.py  -- Workspace-owned extension manifests and process lifecycle
search_space/          -- Policy, external FTS index, and locked search operator
cc_permission_hook.py  -- PreToolUse hook for browser-based permission prompts
mcp_loom_workspace.py  -- Loom-gated file, shell, and sensitive-read MCP tools
mcp_search_space.py    -- Locked, workspace-scoped Search Space MCP facade
mcp_web_tools.py       -- MCP stdio server: web_search + web_fetch for local models
admin_server.py        -- Admin dashboard: instances, model servers, permission supervision
static/
  index.html           -- Single-page app shell
  app.js               -- State management, home view, character/persona/lore CRUD
  chat.js              -- WebSocket chat, message rendering, streaming, branching
  tree.js              -- Interactive tree visualization
  style.css            -- Glassmorphism, cyan/purple palette
  blackhole.js         -- Schwarzschild raytracer (pre-compiled GLSL)
  admin/               -- Admin dashboard SPA

characters/            -- Character definition files (.md)
personas/              -- User persona files (.md)
lore/                  -- Lore/history context files (.md)
certs/                 -- SSL certificates (auto-detected, gitignored)
```

## Configuration

Persisted settings are adjustable from the UI (gear icon) or by editing the
gitignored `config.json`. Important source defaults include:

| Setting | Default | Description |
|---------|---------|-------------|
| `llama_host` | `http://localhost:8000` | llama-server address |
| `llama_model` | `Qwen3.6-27B-NVFP4.gguf` | Source fallback; select a model installed on your machine |
| `llama_server_exe` | `llama-server` | Path to the llama-server binary |
| `llama_models_dir` | `C:\LlamaServer\models` | Directory scanned for .gguf files |
| `max_context_tokens` | `32768` | Context window budget |
| `verbatim_window` | `6` | Recent messages kept verbatim |
| `temperature` | `0.8` | Generation temperature |
| `top_p` | `0.9` | Nucleus sampling |
| `max_tokens` | `16384` | Default output cap for direct local/helper calls |
| `weave_max_tokens` | `2048` | Weave output cap |
| `repeat_penalty` | `1.08` | Repetition penalty |
| `goose_model` | `goose:openrouter:z-ai/glm-5.2` | Default Goose selector |
| `dream_host` | `http://127.0.0.1:8787` | Dream sidecar endpoint |
| `db_path` | `loom.db` | SQLite database path |

Per-model llama-server tuning (context size, GPU layers, KV-cache quant, threads, batch sizes, mmproj, extra args) lives in `models_config.json` and is editable from Settings → Advanced.

Machine-specific and secret settings are environment variables rather than
tracked configuration:

| Environment variable | Purpose / default |
|----------------------|-------------------|
| `LOOM_PORT` | Main server port, default `3000` |
| `ADMIN_PORT` | Admin server port, default `3002` |
| `LOOM_DB` | Initial database path override |
| `LOOM_SSL_CERT`, `LOOM_SSL_KEY` | HTTPS certificate and key; both must exist to enable HTTPS |
| `LLAMA_HOST`, `LLAMA_MODEL`, `LLAMA_SERVER_EXE`, `LLAMA_MODELS_DIR` | Local llama-server connection and installation |
| `LOOM_ENABLE_GOOSE`, `GOOSE_EXE` | Goose visibility and executable override |
| `LOOM_GOOSE_FAST_MODEL` | OpenRouter model for Goose auxiliary tasks, default `z-ai/glm-5.3-flash` |
| `LOOM_GOOSE_PROGRESS_TIMEOUT_SECONDS` | Maximum silent Goose ACP/model interval, default `900`; `0` disables the watchdog |
| `LOOM_ENABLE_DREAM`, `DREAM_*` | Dream Space enablement and sidecar/model tuning |
| `OPENROUTER_API_KEY`, `OPENROUTER_MANAGEMENT_KEY` | Direct OpenRouter inference and optional account management |
| `LOOM_WORKSPACE_RECOVERY_DIR` | External workspace snapshot store |
| `EXO_STATE_DIR` | Override external state root for extension registry, Search Space index and the machine auth secret |
| `LOOM_NETWORK_GATE` | `off` lets any network address connect; default admits only this computer and Tailscale (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) |
| `LOOM_ALLOWED_NETWORKS` | Extra comma-separated CIDRs admitted by the network gate |
| `LOOM_AUTH` | Default `remote`: remote browsers must pair; direct localhost access is exempt. `on` requires pairing locally too. `off` explicitly disables browser authentication. |

### Access control

By default both servers admit only this computer and Tailscale addresses. Remote
browsers must sign in with a pairing code; only direct loopback connections to
`localhost`, `127.0.0.1` or `::1` are exempt. Access through the computer's
Tailscale hostname still requires pairing, even when opened on that computer.
The admin approval relay always authenticates with the machine
secret (`auth\loom-auth.key` under the external state root), never a constant
header, and every approval records which surface answered it (`decision_channel`).
A program running under your own Windows account can still read that secret with
deliberate effort; only running agents under a separate account would close that.

### Browser sign-in and recovery

Remote browser authentication is enabled by default on both servers. To sign in,
double-click `pair_browser.bat` (or run `C:\Python314\python.exe tools\pair_browser.py`);
it opens EXO with a one-time code. For a phone or another Tailscale device, open
Admin → **Devices → Pair a device** on a paired browser, then open the shown link
on the new device and enter the code. Each browser gets a separate random session
token in an HttpOnly, SameSite=Strict cookie (Secure with a `__Host-` prefix on HTTPS).
Sessions survive server reloads and last up to 90 days, expiring after 30 days of
inactivity. They are per hostname (`localhost`, `127.0.0.1` and the Tailscale name
each pair separately), shared by Main and Admin on that hostname. Remote access
requires HTTPS. Admin's **Devices** card lists browsers and can revoke each one;
revocation applies to subsequent requests and messages on existing WebSockets.
The old shared `loom_auth` cookie is no longer accepted: previously paired browsers
must sign in once again. Browser sessions and recovery codes are stored as digests
in `auth\browser-sessions.sqlite3` under the external machine state root.

Before traveling, open Admin's **Devices** card directly through localhost and
choose **Create recovery codes**, or run this locally:

```powershell
C:\Python314\python.exe tools\pair_browser.py --recovery-codes
```

Save the five codes privately in a password manager or offline. Enter a code on
`/pair` to sign in a replacement browser; each works once. Generating a new set
invalidates unused codes from the previous set. Recovery codes can only be created
through direct local access (or an authenticated local relay); an already paired
remote browser cannot create them. Recovery restores a Loom session, and does not
bypass the network gate, Cloudflare, device certificates, or a separate gateway's
authentication. Keep a second enrolled device/security key for those layers.

Changes to the authentication default need a human-initiated reload of **both**
servers through the admin UI. Remove any explicit `LOOM_AUTH=off` launch override
or use `LOOM_AUTH=remote`; `LOOM_AUTH=on` requires local browsers to pair as well.
No reload or live recovery-code generation is performed by tests.

### Proxies and isolation

Both built-in launchers disable Uvicorn's proxy-header trust so the actual TCP
peer remains authoritative. If launching Uvicorn yourself, use `--no-proxy-headers`.
A proxy must preserve the public Host header and must not forward the machine
relay credential. A public hostname or forwarding headers prevent the local
browser and local-agent exemptions. Do not configure an unauthenticated proxy that
rewrites its public Host to localhost and strips all forwarding markers: it would
be indistinguishable from direct local traffic. Gateway Tailscale IPs receive no
automatic exemption. The network gate is still an address filter, not proof of
tailnet membership; enforce that membership with Tailscale and host firewall rules.

This adds Loom's own pairing/session layer. OAuth, MFA, hardware-key login and
device certificates at an external portal still need separate configuration.
Agent processes never receive the browser cookie. They reach only the agent-channel
routes (permission requests, tool events, child runs, context retrieval, state
cards) from this computer.
Local exemptions and files outside the checkout do not isolate authentication from
agents running under the same OS account. Separate OS accounts or a separate
gateway with restricted management access are required for that boundary.

## Character file format

Characters are Markdown files in `characters/` with YAML frontmatter:

```markdown
---
name: Lyra Ashwood
avatar: null
tags: [fantasy, rogue, adventurer]
---
# Personality
Description of who this character is, how they speak, their mannerisms...

# Scenario
The setting and situation where the RP begins...

# Greeting
The character's opening message to the player...

# Example Messages
## Example 1
user: Player says something
assistant: Character responds in their style
```

Characters, personas, and lore can also be created, edited, and imported/exported from the home page UI.

## Data safety

- **SQLite WAL mode** — Write-Ahead Logging for crash resilience
- **WAL checkpoint on shutdown** — the `/shutdown` endpoint checkpoints the WAL before closing
- **Graceful host lifecycle** — the admin dashboard requests `/shutdown` before starting a replacement instance; agents running inside Loom should leave host restart to the human/operator
- **Workspace recovery snapshots** — tool-capable agent turns preserve before/after file blobs outside the checkout and report deletions, large removals, and changes to files that were already dirty
- **Live workspace is canonical** — tracked-but-uncommitted and untracked files are real workspace state; snapshots are a recovery mechanism, not permission to restore from `HEAD`
- **Git excludes runtime state** — credentials, local config, databases, logs, uploads, certificates, provider settings, generated workspaces, and recovery outputs are ignored

## Validation

For isolated queue/tab/handoff/child acceptance and responsiveness measurements,
see [Lifecycle preflight](LIFECYCLE_PREFLIGHT.md). The separate
[permission preflight](PERMISSION_POLICY.md#pre-deployment-acceptance) checks
cross-harness approval behavior. Neither runner deploys code or restarts Loom.

Run the complete test suite with:

```bash
python -m pytest
```

JavaScript files have no build step and can be syntax-checked directly:

```bash
node --check static/app.js
node --check static/chat.js
node --check static/tree.js
```

that repository are not repaired by reinstalling Loom.

## Credits

- Black hole raytracer based on [pyokosmeme/black-hole](https://github.com/pyokosmeme/black-hole)
- OODA harness inspired by [metacog](https://github.com/inanna-malick/metacog) and [popup-mcp](https://tidepool.leaflet.pub/3mcbegnuf2k2i)
