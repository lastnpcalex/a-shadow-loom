# EXO run ownership, windows, and lifecycle: execution plan

Created 2026-09-19. Status: PLAN ONLY; no runtime repair claimed.

Companion to CONTINUITY_EXECUTION_PLAN.md. That plan owns history selection,
handoff construction, retrieval and context recovery. This plan owns the identity,
delivery and lifecycle of the work carrying that context. Their shared queue/run
requirements are ONE implementation, with one acceptance suite. Do not build
parallel orchestration systems or two incompatible run schemas.

This turn authorizes this document and its local checkpoint. Implementation and
production activation are separate work. Another implementer should be able to
execute this plan without relying on the conversation transcript.

## 1. User contracts

- A turn uses the branch the user submitted, even if they navigate immediately.
  Another window cannot change that turn's context, model, queued parent, or view.
- Refresh/reconnect shows the correct persisted and streaming output. Switching
  away does not cancel execution. Switching back needs no manual page repair.
- Queued continuations follow the selected predecessor in order. New roots and
  forks happen only through explicit intent. Retrying an uncertain submission
  cannot send it twice.
- Cancel affects the chosen run and discards its partial context as specified;
  Interrupt preserves it. Queues pause after stops/failures and resume explicitly.
- Optional parallel agents remain usable in the same checkout. Ownership and
  controls stay exact for each run, including children and preparation attempts.
- A provider finishing, failing, or awaiting teardown produces an honest visible
  state in chat, Agents, and admin. Preparation failures cannot strand Looming.
- Permissions identify the actual request/run and record who or what decided.
  Remembered branch grants work as displayed without crossing sibling scopes.
- The selected model, launch route, effective identity, and capacity have clear
  provenance. Adding a model on an existing route remains a data/config change.
- Diagnostics help find faults without logging transcript snippets or making
  slow browsers stall provider processing.

Preserve existing mobile controls, Clotho/OG animations, Canvas tools, notifications,
feature development. Do not introduce a worktree workflow or redesign the UI.

## 2. Evidence register: avoid turning guesses into requirements

The 2026-09-19 read-only audit inspected server.py, database.py, event_router.py,
static/chat.js, provider tracking, model registry/budget code and existing tests.
Working files are changing; confirm every observation against the source used
for implementation. Record the commit plus dirty-file digest, not HEAD alone.

| ID | Observation | Evidence level and required response |
| --- | --- | --- |
| R1 | Runs use task keys, transient snapshots, persistent process rows and leases; event identity may come from current asyncio task attributes | Confirmed structure; multiple representations are valid if one is authoritative and the others are projections. Trace all writers and reconciliation first. |
| R2 | _active_claude_procs/_active_goose_procs use conversation keys, but _track_generation_provider retains handles on each task | Confirmed diagnostics limitation in parallel runs. NOT proof of wrong-process cancellation: preserve task-owned teardown and test actual control paths. |
| R3 | api_list_generations marks a row in_memory if ANY live task shares its conversation | Confirmed overbroad identity check; one sibling can make a stale row appear owned. Match the exact run/attempt/draft. |
| R4 | Initial client ownership can be inferred from origin page and thread; run subscriptions otherwise exist | Suspected same-thread navigation race. Reproduce pending launch versus navigation versus queue dispatch before choosing the change. |
| R5 | Queues persist predecessor/thread/page/settings; messages already persist send_intent, target_anchor_id and client_request_id with a unique index | Earlier claim that idempotency/intent were absent was too broad. Verify atomic linkage and retry behavior across these existing records; do not duplicate them. |
| R6 | _ws_send logs the first 30 content characters on every call and awaits websocket sends serially | Confirmed content logging and blocking structure; quantify slow-client impact with controlled backpressure. |
| R7 | OpenRouter DEFAULT_MODELS is a projection of model_registry; local tuning and runtime capacities are separate inputs | NOT proof of independent catalogs. Trace precedence, refresh and cache invalidation; retain appropriate separate configuration sources. |
| R8 | Several legacy history helpers remain; ordinary callers use shared preparation and other callers include specialized/retired paths | Presence alone is not a production bypass. Inventory reachability; continuity plan owns migration of actual ordinary bypasses. |
| R9 | Harnesses differ in native automatic approval and permission transports | Known boundary. Prove parity for bridged requests; do not promise identical native approval behavior or add a blanket restrictive mode. |
| R10 | Some tests replace the entire generation handler or summary route; other browser/native tests retain more production code | Coverage limitation, not evidence tests are worthless. Label the replaced boundaries and add missing composed tests. |
| R11 | Startup _cleanup_stale_drafts calls delete_branch for old empty drafts before orphan reaping; delete_branch traverses descendants | Confirmed potentially destructive path; reproduce with queued descendants and live ownership. No claim yet that a particular user's data was lost through it. |

Do not revive the old shared-connection transaction diagnosis without checking:
get_db currently uses autocommit and multi-statement writes have a transaction
helper. Audit remaining ownership/close races separately from the fixed issue.

## 3. Authority and data model

Extend existing tables/helpers where sound. Write one authoritative durable run
record for each requested execution before expensive preparation or launch.
Memory holds handles/caches; browser state is a projection; neither can invent
durable completion, branch placement, or permission authority.

| Entity | Stable facts | Allowed mutable facts |
| --- | --- | --- |
| Submission | Existing client request key, workspace/conversation, explicit intent, chosen anchor, authoring page, selected settings | Acceptance/result linkage; explicit queue editing changes a version, never silently reuses the key with different content |
| Run | Globally unique persisted run ID, submission/user message, thread, source evidence revision, kind, parent/child owner | Lifecycle revision, draft link, terminal result, current attempt; generation counter is compatibility metadata |
| Attempt | Run ID + attempt ID, selected harness/model/backend and config revision, owning host epoch | Native session, provider handle metadata, observed identity, start/exit evidence, error |
| Queue dependency | Submission, intended predecessor run/draft or prior queued submission, thread, ordinal | Queued/claimed/paused/dispatched, explicit resume authorization, resolved parent link |
| View | Distinct page identity; explicit conversation/thread/branch selection | Cursor revision, navigation revision, followed run, subscriptions, event watermarks |
| Permission | Request ID, run/attempt/tool call, workspace/action scope, requesting surface | Pending or terminal decision, source, note, grant reference; one terminal decision |

Do not make the origin page the permanent owner of execution or exclusive reader.
Authorized users may explicitly open an active run from chat/admin and observe it.
Navigation ownership, observation, and authority to approve/cancel are distinct.
Closing the originating page does not cancel the run or make permissions
unanswerable through the existing authorized admin surface.

Runs need IDs unique across process restarts, instances, and database selection.
Events identify the instance/database epoch as appropriate. Old gen:N references
must not attach to a new run after an in-memory counter resets. Keep compatibility
parsing at the boundary, with verified durable lookup rather than guessing.

Store provider PID plus process-start identity/host ownership when available.
Never kill a recycled PID solely because an old database row contains its number.
Keep one-to-many attempt/process ownership; cloud requests without a PID still
need an explicit request cancellation/terminal contract.

### State transitions and atomicity

Use existing coordination reducers and extend their inputs. Persist an execution
outcome separately from provider teardown: a terminal answer is not proof all
owned processes exited, and a released lease is not proof of successful output.
Suggested user-facing phases map from queued, preparing, running, waiting for
permission, stopping/draining, and terminal. Outcome distinguishes completion,
cancel, interrupt, failure and interrupted-by-host-restart.

Admission atomically links submission, queue dependency or reservation, run,
and cursor update where applicable. Use revision checks and unique constraints.
A reused request key returns the original result; different payloads with the
same key return a clear conflict. Explicit Retry after a terminal failure is a
new authorized attempt, distinguishable from a transport retry.

Queue claiming links the claim to one durable run. If a process stops between
claim, draft creation, provider spawn, registration and acknowledgement, startup
reconciles these facts before considering another launch. Never reset every
claim and relaunch blindly. External process creation cannot be atomic with
SQLite: record launch intent and use verified process/attempt ownership to
reconcile uncertain outcomes. Do not promise exactly-once arbitrary tool effects.

Commit authoritative control transitions with deliverable event records (a small
transactional outbox or equivalent). Drain with retry/deduplication. Do not store
each streamed token as its own transaction: persist bounded draft snapshots and
sequence watermarks; control records and terminal outcomes must remain durable.

## 4. Window attachment, navigation and queues

Generate distinct identities for duplicate tabs. Full reload may create a new
page identity; explicitly reacquire a view using durable branch/run references.
Do not copy writable cursor ownership from sessionStorage or require an old
vanished page ID to observe the run. Test normal and duplicated windows separately.

Capture an explicit submission/view revision before asynchronous send. Correlate
POST acceptance and WebSocket preparation/start with the submission and run IDs.
Either ordering must converge on one draft. If the user navigates meanwhile,
background execution continues but its delayed event cannot switch the viewport
or overwrite the new cursor. Opening the run explicitly can attach it afterward.

For each asynchronous branch load, cursor update, queue fetch, and stream attach,
apply the result only if its request/navigation revision still matches. Abort
obsolete fetches where practical. A same-conversation check alone is insufficient
when two branches of that conversation are being visited rapidly.

Preserve cursor compare-and-swap behavior and give one component responsibility
for advancement. Server draft creation and client stream_start cannot perform
independent unconditional cursor moves. Observe a run through authorized exact
identity; current leaf, focus, missing thread fields, or any live run in the
conversation cannot confer attachment by themselves.

Persist queue chains using existing submission/topology fields. Ordinary queued
follow-ups form a serial continuation; explicit forks remain separate. Resolve
pending parents once, transactionally. Model/harness choices are captured per
submission; changing workspace defaults does not silently retarget queued work.
Queue edits retain content through status refresh and use revision checks.

Render queued nodes after their recorded predecessor; a newly rendered branch
must not temporarily contain the old branch's messages. A queued run starting
after its origin tab navigates stays background activity until explicitly opened.
Queue dispatch after success waits for verified teardown. Cancel, interrupt,
failed preparation and crashes pause dependants until explicit Resume.

Use run-keyed browser state and a separate followed-run selection. Derive active
counts from run states rather than increment/decrement counters that can drift
after duplicate or missing events. Terminal events cannot clear another run's
spinner, controls, model pill, statistics, permission dialog, or pending editor.

## 5. Delivery, snapshots and recovery

Keep event_router and exact subscriptions; supply an explicit run/attempt context
to publishers, including callbacks and background tasks. Current asyncio task
attributes may be compatibility inputs but cannot silently determine ownership.
Missing required identity is observable through sanitized diagnostics and a
recoverable run error, not a conversation broadcast or an invisible dropped turn.

Version envelopes and snapshots. Carry run/attempt, source revision, draft,
thread, event sequence, and host epoch where needed. Define whether sequence
numbers cover public stream events or internal events so legitimate gaps are
not mistaken for loss. A snapshot states the sequence through which it includes
content. Subscribe plus snapshot must close the race with subsequent deltas.

Clients deduplicate delivery, detect gaps, and reconcile from the run's durable
projection. An older snapshot cannot overwrite later text or resurrect terminal
Looming. Distinct attempts may reset provider-native tool IDs; map them with the
attempt identity so old output cannot update a new tool card.

Prepare errors before provider output receive durable run status and clear the
loading UI. After a dropped terminal notification, reconnect recovers the stored
outcome. Admin activity and landed lists read the same projections and preserve
their existing workspace/root grouping and local review marks.

Replace serial unbounded websocket awaits with bounded per-connection delivery
queues and writers. Bound bytes as well as item count. Coalesce replaceable
snapshots; preserve control ordering. If a slow client cannot keep up, detach it
with an explicit resync requirement while provider execution and other clients
continue. Persist final output; do not silently drop deltas and present a complete
answer. Terminal/control transitions remain recoverable without a live socket.

Bound caches, sequence maps, ended subscriptions, DOM nodes and reconnect timers.
Retain only necessary active/recent state and paginate older data. Measure before
optimizing. Avoid rebuilding every message/tool panel on each status update;
preserve scroll, open panels, editors, selection, focus and mobile composition.

### Restart and retention

Reconcile durable ownership before housekeeping. Age and an empty text field do
not establish that a draft is disposable: it may own tools, queued descendants,
an active provider, retrieval scopes, or a child's parent link. Replace automatic
subtree deletion with state repair/retention. Any eventual garbage collection
must demonstrate no user content, descendants, references or live owners and
must not run as a side effect of this migration.

Persist interrupted/failed outcomes for partial drafts as well as empty drafts.
Resolve orphan approvals as lifecycle outcomes if their waiting process is gone;
do not display an actionable orphan approval that cannot reach a requester.
Recover work visibly; do not automatically repeat uncertain external actions.
Preserve existing admin-only restart behavior and prove actual host/process exit
using isolated fixtures. Tests must never stop either active Loom host.

## 6. Permissions and provider capabilities

Keep the authoritative permission algebra and current user-selected profiles.
Normalize actual tool identity, raw arguments, working directory and owned run
before policy evaluation. Permission replies correlate to one request/attempt,
and duplicate or late replies have no effect. Persist denial notes and honest
decision_source for user, policy, timeout, lifecycle and bridge failures.

Evaluate remembered grants against canonical workspace, branch-descendant scope,
action template and current request. Display the actual scope. Cover sibling
denial, child inheritance/narrowing, explicit revocation, restart lifetime and
workspace changes. Do not infer blanket permissions from conversation membership
or a read-only stream subscription. Admin/chat races produce one decision.

Inventory native-auto-approved versus bridged actions for each harness. Report
coverage truthfully; do not claim a policy can control a request it never receives.
Preserve the accepted limitation that Goose denial notes may reach the model on
continuation rather than immediately. Avoid creating duplicate native and Loom
dialogs or broadening restrictions merely to make counts match across harnesses.
Tests must verify ordinary permitted work remains low-friction.

## 7. Model metadata and context-plan interface

Retain model_registry as the source of selector routes and catalog defaults.
Runtime discovery and local tuning have legitimate different scopes. Define
precedence: verified per-run runtime facts, applicable model configuration or
catalog metadata, then labeled conservative fallback. Persist requested identity
separately from effective identity and capacity evidence.

One resolved model/capability object feeds picker metadata, launch, attestation
and continuity budgeting. Refresh affects future runs; it cannot silently alter
an accepted queue item or in-flight attempt. Version/invalidate caches explicitly.
Retired selectors remain readable and do not fall through to an arbitrary local
model. Adding a model to an existing backend should pass a data-only smoke test;
a new protocol/backend still requires an adapter.

The continuity planner consumes the immutable run source and model facts. It
must not move a cursor, reparent a queue, or release a lease. Its preparation and
recovery attempts publish through this plan's run identity. Context selection,
summary/retrieval strategy and full-payload admission stay owned by the companion
plan; remove actual competing ordinary callers only after reachability review.

## 8. Logging and performance

Remove unconditional streamed-content previews from WS-TRACE. Use structured
IDs, phase, sequence, timings, byte counts and error classes. Do not log raw
prompts, tool arguments/results, approval notes, retrieval tokens, secret paths
or summaries. Redaction must cover exception/log fallbacks too. Existing sensitive
logs are user data: surface their presence privately; do not delete them casually.

Give each submission/run/attempt a trace correlation so one report shows its
chosen parent, queue dependency, context-plan revision, provider launch, routing
and terminal evidence. Diagnostics must distinguish unknown, active, terminal,
and draining states and identify the loaded source version separately from disk.

Establish reproducible baselines for send-to-draft, preparation/TTFT, reconnect,
event-loop lag, websocket backlog, DB lock duration, browser long tasks, heap,
DOM growth and hidden-tab CPU. Test one stalled browser beside a healthy browser.
Separate provider inference latency from Loom overhead. Record environment,
source and percentiles; declare acceptable bounds before the final run. Physical
phone performance remains a separately labeled measurement, not an emulation claim.

## 9. Ordered implementation gates

| Stage | Concrete deliverable | Exit gate |
| --- | --- | --- |
| A: reproduce and map | Caller/writer/reader inventory for submission, cursor, run, queue, process, event, permission, model and terminal state; sanitized incidents | Each confirmed finding has a reproduction; each suspicion has a verification task or is retired with evidence |
| B: authoritative ownership | Durable identity/revisions and projections integrated with existing reducers, submission keys and transaction helpers | Concurrent requests, retries and crash boundaries produce one owned run and correct topology |
| C: execution and queue migration | All ordinary entry points, retries, preparation, children and admin controls use that identity | Serial exclusion, optional parallelism, exact cancellation and queue chaining pass through production orchestration |
| D: event/view migration | Correlated acceptance, subscriptions, snapshots, browser reducers and bounded delivery | Two-window race suite passes actual parent/cursor/DOM assertions without refresh repair |
| E: surrounding consumers | Permissions, model facts, activity/landed lists, privacy-safe diagnostics and housekeeping | No secondary consumer invents ownership or destroys referenced work; behavior and resource gates pass |
| F: reconcile and release | Remove obsolete writers, document compatibility, final source evidence and migration/recovery instructions | Composed acceptance and affected regressions pass on one snapshot; known unsupported configurations stay explicit |

Sequence is by dependency, across all supported harnesses. Do not implement a
Goose-shaped ownership policy and later copy it to other adapters. Each stage is
a reviewable checkpoint, not permission to report the overall repair complete.
Use bounded modules around the existing code; avoid wholesale server.py rewrite.

## 10. Required acceptance matrix

All tests use isolated databases, listeners, disposable workspaces and controlled
provider processes. Keep real admission, queue, database, event routing and DOM
reducers in integration tests. Inject faults at I/O boundaries instead of replacing
the entire handler whose correctness is being claimed.

| Scenario | Required assertions |
| --- | --- |
| Two pages, same parent or sibling branches | Exact message parents and runs; each page follows only its intended run; model settings/context source stay captured |
| Delayed response after navigation | Old POST, branch fetch, queue fetch and snapshot cannot replace the new view/cursor |
| Reload, duplicated tab and reconnect | Distinct page identities; explicit reacquisition; no duplicate send, run-ID collision, lost output or stranded approval |
| Three queued continuations | Ordered chain under the chosen predecessor; no automatic sibling branches; settings frozen; correct DOM order after refresh |
| Edited message as new branch / new root | Explicit topology honored immediately, including stream-before-POST ordering |
| Parallel runs, mixed harnesses, same checkout | Every process/attempt represented; exact cancel affects one; diagnostics and permission scopes stay separate |
| Lost/duplicate/out-of-order events | Final persisted answer complete; snapshot watermarks respected; terminal state never regresses |
| Stops and teardown delay | Cancel/Interrupt semantics preserved; dependent queue paused; lease held until exit; explicit Resume launches once |
| Preparation error before draft/provider | Durable useful error visible in chat/admin after reconnect; no endless overlay or phantom success |
| Process stop at each admission boundary | Accepted submission, queue claim, spawn, registration, persist, terminal/outbox publication all reconcile without blind repeat |
| Host restart / PID reuse | Isolated host only; no unrelated PID killed; no old event attaches to a new run; partial drafts retain interrupted state |
| Old empty draft with queued descendants | Startup preserves subtree and queue; orphan reconciliation precedes retention decisions |
| Permission lifecycle | Matching grant reused; sibling request still asks; user note delivered as supported; late/admin/chat decisions do not cross requests |
| Child and context recovery | Parent can end while durable child survives; child's exact scope and attempt preserved across shared continuity recovery |
| Metadata refresh / new model | Picker, queued launch, effective identity and budget use documented precedence; existing-route model addition needs no provider-list edits |
| Slow/hidden browser and burst tools | Provider and other browser proceed; bounded backlog, heap/DOM/timers; explicit resync recovers complete output |
| Privacy canaries | Synthetic private strings absent from default logs, telemetry and global notifications; no retrieval secrets in fixtures |
| Existing UX | Editors, scroll, tree selection, animations, phone pause glyph and keyboard-safe layout remain usable |

Add deterministic scheduler/reducer tests, database concurrency tests, isolated
browser integration and opt-in native harness acceptance. Record what each layer
mocks. Native tests must exercise normal launch APIs; handler-only tests cannot
replace queue/lease coverage. Existing serial tests remain serial.

Extend Lean models for immutable run/branch binding, at-most-one successful
claim, terminal monotonicity, permitted cursor advancement and queue dispatch
after successful outcome plus teardown. Bind proofs to production reducers and
fixtures. Model-check event interleavings and use randomized traces with saved
seeds. Test that deliberate identity/sequence faults fail assertions. Lean proves
the modeled transitions; real browser, DB, OS and provider boundaries need tests.

Claude subscription access is unavailable; exercise supported Claude Code/local
where possible and mark unavailable native configurations separately. Do not
require a new paid account or treat unavailable configurations as passed.
Run combined queue + long handoff + reconnect + permission scenarios with the
continuity suite, so two individually green subsystems cannot hide their seam.

## 11. Migration, reporting and completion

Use additive, idempotent migrations tested on a sanitized old-schema fixture and
a copy of realistic topology. Preserve messages, Canvas/ignored files, checkpoint
evidence and child/permission records. Map legacy identifiers only when ownership
is provable; expose ambiguous old state for reconciliation rather than guessing.
Old clients may receive compatible projections, but incompatible mutation
requests receive a clear refresh/retry response without extra sends.

There must be one writer per authoritative transition during migration. Avoid
indefinite dual-write flags or silent fallback into the old scheduler. Prepare a
rollback/recovery procedure compatible with already-written data; do not solve
rollback by restoring an old database over new user work. Activation belongs to
the human after a persisted handoff; never restart an active host during this task.

The acceptance report maps each user contract and R1-R11 finding to source,
reproduction, repair or justified non-bug disposition, and test evidence. Record
source digest (including relevant uncommitted changes), harness versions, effective
configuration, native limitations, and loaded-versus-on-disk activation state.
Keep four statuses separate: implemented, deterministic verified, native verified,
and activated. Test counts alone do not advance those statuses.

Completion for the declared supported configurations requires the composed matrix,
no unreviewed alternate ownership writer, safe migration, and explicit resource
measurements. A renamed helper, added table, successful short turn or declaration
of a shared contract is not completion. Preserve unrelated changes and commit only
reviewed work attributable to this repair; pushing requires separate instruction.
