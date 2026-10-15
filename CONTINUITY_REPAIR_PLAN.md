# Loom continuity repair: implementation goal and acceptance contract

Created: 2026-09-19. Status: **in progress; shared engine and top-level integration
implemented, systemic acceptance incomplete**.

This is the execution plan for the active continuity-repair goal, not a claim
that the listed behavior exists. Update this file with evidence at each handoff.
The user's requirements are already established; do not repeatedly ask them to
approve the same architecture or substitute unrelated feature work.

## Required user outcome

A user can keep working through long conversations and change harnesses without
losing the task, repeating completed actions, or becoming trapped by context
limits. Compatible same-harness turns use native resume/fork. Crossing harnesses
uses Loom-owned context transfer. Native context exhaustion has a recovery route
even when the source harness cannot compact its own session.

Continuity means preserving useful state, not promising lossless compression or
identical model personalities. Original evidence stays available for retrieval.
If essential current input cannot fit even without history, say precisely what
does not fit; never silently truncate it, retry forever, or call it repaired.

## Current verified gaps and integration map

Recheck live files before editing; these observations are from 2026-09-19:

- `provider_contract.py` selects the nearest compatible native boundary. Keep
  its protection against reviving an old A session after A -> B -> A.
- Ordinary top-level launches now use `context_runtime`; the legacy renderer
  remains for specialized operators and compatibility consumers. Audit before
  deleting those consumers; do not reintroduce it as ordinary context authority.
- Manual `/compact` now saves a shared checkpoint without in-place native
  mutation. The old automatic Claude helpers have no ordinary launch callers.
- `replay_budget.py` admits fresh payloads but does not supply generic recovery.
- `local_context_guard.py` compacts an outbound inference view, not a durable
  shared context checkpoint or necessarily the retained native Goose session.
- `local_summary.py` is retired on disk. Do not re-enable its separate CPU model,
  background inference, or automatic model downloads.
- `context_summary.py` is retired. The automatic local summarizer added by Astra
  has been removed from cloud-to-cloud handoffs. Handoffs replay directly within
  destination capacity (e.g. Gemini 1M, Claude 200k/1M, Codex 272k+) paired with
  scoped retrieval notices. Out-of-budget handoffs without an authorized summarizer
  fail closed with clear diagnostics rather than unconditionally invoking local GPU inference.

Inspect launch, invalid-resume fallback, manual compact, local tool-loop requests,
Weave's intentional chat-completion architecture are outside this redesign.

## Architectural contract

### One owner, two normal paths, one explicit recovery path

1. **Native continuation:** compatible nearest session -> native resume/fork plus
   current input. No full replay or cumulative-history gate on ordinary resumes.
   Model changes within a harness use actual native compatibility, not a blanket
   same-model rule or a blind assumption that every change is resumable.
2. **Harness handoff:** exact branch -> shared context preparation -> fresh
   destination session. Its next compatible turn must resume that new session.
3. **Native recovery:** exhausted/rejected/unusable native session -> preserve
   original -> same shared preparation -> fresh session with explicit recovery
   provenance. Prefer native compaction when supported and effective.

Adapters consume a shared launch plan; they must not independently decide which
history to omit or which provider may compact. Keep destination-specific wire
formats and native session handling inside adapters.

Supported agent harnesses are Codex, Claude Code (including its compatible
backends), Antigravity/Gemini, and Goose. The shared engine makes decisions from
explicit capabilities and normalized outcomes, not harness-name conditionals.
Adapters report native resume/model-change compatibility, compaction support,
history export/retrieval, effective capacity, and terminal/error evidence.
Unsupported capabilities are explicit; never guess them from a display label.

### Durable checkpoint plus bounded context

- Checkpoints are separate records, not destructive edits to conversation nodes.
- Bind each checkpoint to its exact ancestor sequence, covered boundary, source
  content revision/fingerprint, schema version, and summary provenance. Reject
  stale checkpoints after edits and checkpoints from sibling branches.
- Preserve objective, user constraints/corrections, decisions with rationale,
  unresolved questions, completed action outcomes, uncertain/in-flight actions,
  artifact references, active child IDs, and pending work. Separate evidence
  from assistant claims; a previous "done" is not proof of completion.
- Keep recent exchanges verbatim and older originals retrievable. Handoff must
  carry scoped retrieval capability, not merely IDs the destination cannot use.
- Retrieval is bounded, read-only, and limited to authorized branch evidence;
  attempts to fetch another conversation/branch must be rejected.
- Keep tool calls/results paired. Never treat an unresolved call as completed,
  migrate native tool IDs as executable commands, or replay writes automatically.
- Historical images remain referenced and retrievable. Include native images
  only when selected for the current task under the payload budget; disclose
  omissions rather than claiming the destination saw them.

### Budgeting, summary execution, and privacy

- Budget the complete destination launch: rendered instructions, tools, recent
  messages, checkpoint, attachments, output headroom, and estimation margin.
  Prefer effective runtime capacity; label catalog estimates and uncertainty.
- Summarize in bounded chunks with bounded output and merge requests. The
  summarization path itself must work when the source exceeds model capacity.
- Use an explicit authorized summary backend; no implicit dependency on Claude,
  separate CPU model, downloads, or cloud fallback. Do not repurpose Search
  Space's disclosure flags as universal conversation policy without checking
  their semantics. Define and test the disclosure decision for this operation.
- Persist only successfully validated checkpoints. Cancellation or failure leaves
  source history and the last valid checkpoint intact. Serialize or deduplicate
  equivalent work without globally blocking independent branches.
- Bound attempts and demonstrate progress. No infinite re-summary loop. Retain
  completed tool outcomes before recovery; ambiguous side effects require
  reconciliation/user-directed continuation rather than automatic repetition.
- Diagnostics contain counts, timings, IDs, routes, and error classes, not raw
  prompts, private filenames, summaries, keys, or tool arguments.

### Existing lifecycle contracts remain binding

Context preparation stays inside the owning generation lease, cancellation,
permission, and exact-event-routing scope. It cannot launch an extra provider,
advance another tab's cursor, reparent a queued message, release the lane early,
or lose durable child ownership. Show bounded progress while preparing context.
No main/admin host restart, stop, kill, or live database migration during tests.

## Implementation sequence and exit gates

Each phase must produce evidence for a requirement. A committed module alone
does not satisfy a phase or the goal.

### A. Define the cross-harness contract and acceptance matrix first

- [ ] Trace actual production callers and record competing context owners.
- [x] Specify shared launch-plan/checkpoint schemas, transition rules, and adapter
  capability contracts before implementing provider-specific fixes.
- [ ] Define parameterized production-path tests for all 12 directed handoffs
  between the four supported harnesses, all four same-harness continuations,
  round trips, compatible/incompatible model changes, and overflow recovery.
- [ ] Reproduce oversized local-Goose continuation and large-to-small handoff as
  regression cases in that matrix, not as the template for the architecture.
- [ ] Include a native-compaction failure and a further follow-up after recovery.
- [ ] Establish existing resume, queue, cursor, cancellation, and privacy baselines.

Exit: failures are attributable to specific transitions, not just isolated helper
inputs. Do not persist the user's real private conversation as a test fixture.

### B. Build the harness-independent context engine

- [x] Implement shared checkpoint storage and migration/compatibility tests.
- [x] Implement bounded summary/merge, destination admission, and branch retrieval.
- [ ] Implement native-continuation, handoff, and recovery decisions against the
  capability contract; keep native protocol mechanics out of shared policy.
- [ ] Test the same engine across capability combinations, destination capacities,
  branch changes, summary failures, disclosure restrictions, and side effects.

Exit: one engine implements the contract without Goose/Claude-specific recovery
policy. This is foundational implementation, not evidence of working native
handoffs or goal completion.

### C. Integrate all adapters and remove competing ownership

- [ ] Implement capability/protocol adapters for every supported agent harness.
- [ ] Route all supported agent harnesses, invalid-session fallbacks,
  explicit/manual compaction, and durable children through the shared contract.
- [ ] Exercise each production launch/recovery entry point, including failures
  during native compaction and tool-loop requests, through the common matrix.
- [ ] Retire duplicate compaction/replay decisions after proving their consumers
  migrated. Audit imports, startup/background tasks, routes, and old markers.
- [ ] Preserve old history/checkpoint compatibility without deleting stored data.
- [ ] Keep native resume as the normal path, including compatible model changes.
- [ ] Add context-origin/coverage/recovery evidence to existing turn metadata;
  avoid unrelated UI redesign.

Exit: no production route silently retains the old provider-only recovery trap.
Goose is a required regression case, not an architectural first implementation
that will be generalized later. Integration may be sequenced for reviewability,
but no adapter may introduce a separate continuity policy to get its tests green.

### D. Adversarial acceptance and rollout evidence

- [ ] Execute the matrix below against the final source snapshot.
- [ ] Verify all directed harness pairs through production-path integration tests
  and run native-provider acceptance for every available directed pair, including
  successful native follow-ups after handoff and recovery.
- [ ] List each unavailable native pair separately; integration doubles are not
  native evidence and a two-provider demonstration is not universal acceptance.
- [ ] Audit staged diffs and commit coherent changes without absorbing unrelated work.
- [ ] Report tested source hash, harness versions, effective model capacities,
  evidence locations, timing/resource measurements, and untested combinations.
- [ ] Provide human-controlled activation instructions only when changes require
  them. Never activate by restarting the host from inside an agent turn.

## Acceptance matrix

| Workflow | Required evidence |
| --- | --- |
| Same harness, many turns | Native session/fork lineage; only new task submitted; no repeated full replay |
| Every directed harness pair | All 12 cross-harness routes use shared policy through real production entry points; native results recorded separately per pair |
| Large-context A -> smaller B -> A | Destination requests fit; intervening work retained; no stale A resurrection; next A resumes |
| Each harness exhausts native context | Capability-based compaction/recovery; retained outcomes; successful next native turn |
| Local Goose exceeds capacity | Native compaction failure handled; bounded recovery succeeds; next turn succeeds too |
| Summary input itself oversized | Each chunk/merge admitted; no hidden truncation of current task; bounded attempts |
| Fact and task retention | Seeded earlier corrections, decisions, artifact pointers, unfinished tasks survive; exact evidence is retrievable |
| Side effects before overflow | Completed action counted once; unresolved outcome not blindly re-executed |
| Edited branch / concurrent siblings | No stale or cross-branch checkpoint; isolated scope and cancellation |
| Cancel / interrupt / queue / reconnect | Existing retain/discard semantics, paused queue, stable cursor, one serial provider |
| Children and permissions | Correct ownership, scoped approvals, no orphan/double launch during handoff |
| Backend unavailable / sensitive data | Clear recoverable error, unchanged history, no cloud fallback or raw-content diagnostics |
| Repeated recovery and long sessions | Bounded payloads, measured summary cost/TTFT/memory; no new background CPU worker |

Use disposable workspaces, temporary databases, isolated ports, synthetic facts
and privacy canaries, and harmless counted side effects. Provider credentials
must not enter reports. Native execution remains subject to actual permissions
and availability; unavailable acceptance stays explicitly unverified.

## Proof boundary and completion rule

Lean is useful for branch coverage, permitted transitions, budget arithmetic,
and no-retry/no-double-launch invariants in a defined model, with production
reducers checked against it. It cannot establish summary fidelity, real native
resume behavior, or correct adapter wiring by itself.

The goal is **not complete** until all supported adapters use the shared engine,
all directed pairs pass production-path integration coverage, and every available
native pair passes continuous handoff/return/follow-up acceptance with semantic
retention and lifecycle checks. Local-Goose overflow remains a mandatory native
regression, not a substitute for cross-harness coverage. Unavailable native pairs
must remain explicitly unverified; do not claim universal native verification or
production readiness. A component-test count is never the substitute.

This file's universal scope and dependency-first gates supersede the original
Goose-first/two-provider minimum described when the active goal was created.

Record evidence in four separate categories: implemented, isolated/synthetic
verified, native-provider verified, and unverified/blocked. If verification
discovers a gap, reopen the corresponding requirement rather than redefining it.

## Evidence ledger

| Date | Claim | Evidence | Status |
| --- | --- | --- | --- |
| 2026-09-19 | Requirements and implementation plan recorded | This document; active goal | Planning only |
| 2026-09-19 | Replaced Goose-first sequencing with shared-contract/engine-first ordering and universal adapter integration gates | This document | Planning only; no runtime repair claimed |
| 2026-09-19 | Shared resume selector exists; generic durable handoff missing in inspected paths | `provider_contract.py`, `server.py`, `replay_budget.py`, `local_context_guard.py` | Source inspection; no acceptance run |
| 2026-09-19 | Shared engine, revision-bound SQLite checkpoints, scoped retrieval, bounded local summary executor | `continuity_engine.py`, `context_checkpoints.py`, `context_summary.py`, `context_runtime.py` | Implemented; 30 isolated engine/storage/summary tests passed |
| 2026-09-19 | All 12 directed top-level handoffs, four native continuations, and eight output-aware recovery cases | `tests/test_continuity_launch_matrix.py` | 24 production-handler tests passed with synthetic provider/summary doubles; not native acceptance |
| 2026-09-19 | Bounded summary retains synthetic correction, artifact, policy attribution, and pending reviewer | `tests/test_context_summary_native.py`, `LOOM_RUN_CONTEXT_SUMMARY=1` | One existing-llama-server acceptance test passed; no model started/reconfigured |
| 2026-09-19 | Goose/local -> Goose resume -> Codex -> Goose -> Goose resume retains synthetic code/status | `tests/test_continuity_native.py`, `LOOM_RUN_NATIVE_CONTINUITY=1` | One short-context native test passed in isolated DB/workspace/listener; does not prove overflow recovery or other native pairs |
| 2026-09-19 | Affected server/database/queue/permissions/adapter checks and isolated browser handoff/child lifecycle | 16 test files including `tests/test_lifecycle_handoff_browser.py` | 345 passed on fixed source; full repository suite and systemic native acceptance remain outstanding |
| 2026-09-19 | Removed mandatory local summarizer; direct replay in destination capacity; destination-scaled summary budgets; fail-closed limits | `context_runtime.py`, `local_context_guard.py`, `continuity_engine.py`, `context_summary.py` | Implemented; local summarizer retired; all 163 continuity/recovery/guard/checkpoint tests passed |

### Remaining work before this goal can be called complete

- Manual `/compact` now uses generation admission and the shared summary/store.
  Activation checks the exact branch fingerprint and page cursor revision. No
  message, native session, active flag, or cursor is mutated. The next turn
  rebuilds once; a landed successor resumes natively. Cancellation, occupied
  serial lane, moved cursor, edited/sibling branch and unavailable summary backend
  are covered. Browser and real Goose/local -> checkpoint -> Goose -> Codex ->
  Goose acceptance passed. Large manual checkpoints still need resource profiling.
- Durable children launch self-contained tasks independently. Explicit paused
  recovery now uses their own task/event-prefix adapter to the shared engine,
  separate checkpoint/retrieval storage, and unchanged provider teardown. Native
  Codex and Goose recovery/MCP retrieval passed with synthetic retained evidence.
  Real provider-tree failure after a committed, unacknowledged tool effect now
  passes for Codex -> Goose/local and Goose/local -> Codex (details below).
  Full host-crash/restart, other tool semantics and other recovery combinations
  still need acceptance. Uncertain side effects are not automatically replayed.
  Image jobs keep their separate artifact workflow.
- Native exhaustion after recorded non-error tool outcomes now has one automatic
  shared-context continuation after verified teardown. The same draft/evidence
  is retained; missing/error outcomes still require user-directed reconciliation.
  All four handlers and real Goose continuation pass boundary tests (below).
  Natural exhaustion after arbitrary native tool workloads remains wider acceptance.
- Mandatory local summarization has been completely decoupled from cloud handoffs.
  Cloud handoffs use direct replay within destination capacity and scoped retrieval.
  When an explicit authorized summary backend is configured, output budget scales
  with destination capacity (max(4096, input_budget // 8)). Unsummarized overflow
  fails closed with clean diagnostics rather than silently dispatching GPU inference.
- Exact-branch retrieval scopes are now owned by the actual generation task.
  They survive nominal TTL expiry while that owner is active, but completion,
  cancellation, restart/loss of owner, or source edits invalidate access. Every
  ordinary native continuation gets a fresh scope without replay or summary.
  Real MCP subprocess tests and native Codex/Goose model-initiated retrieval pass
  after forced persisted expiry. This is not a multi-hour resource soak or proof
  of retrieving new tool outcomes beyond the scope's frozen evidence prefix.
- Real local-Goose overflow with native compaction failure, shared-checkpoint
  recovery and a subsequent native follow-up now passes (counts below). All six
  pairs among Codex, Goose/local, Antigravity and Claude Code/local have bounded
  round-trip/native-resume acceptance below. Paid Claude and other backend/model
  combinations, model-change recovery, and wider lifecycle/resource acceptance
  remain unverified. Short pair tests are not overflow acceptance for each pair.
- Call-site audit confirms `_run_compact_handoff` and `_handoff_payload_budget`
  have no production callers. `_compactable_anthropic_source` is used only by
  the unused old compact helper. The legacy history renderer remains in
  specialized operators, a local compatibility renderer and the retired Dream
  handler, not ordinary shared-context launch preparation. These unused helpers
  were not broadly deleted as part of recovery. Weave remains unchanged.
  A bounded recovery launch guard now has Lean proofs and exhaustive runtime
  conformance. The final frozen-source repository pass and source digest are
  recorded below. Whole-system identity/crash refinement and production
  activation remain incomplete.

### Follow-on implementation evidence (2026-09-19)

- Manual checkpoint and renewed-scope production-handler tests passed for all
  four harnesses, including fresh-after-checkpoint then native continuation.
- Real Chromium `/compact` -> saved checkpoint -> follow-up passed with the
  same cursor and message IDs throughout compaction; no phantom assistant node.
- Real Goose/local manual checkpoint and Goose/Codex round trip passed (50.25s).
- Broad affected run passed 357 tests, including browser/child/queue/permission
  coverage. Final focused rerun after terminal-envelope and exact-tokenizer
  corrections passed 129 tests. These are affected suites, not the full repo.
- Native overflow acceptance reproduced the user's exact `Ran into this error
  trying to compact` failure. Goose returned it as successful-looking assistant
  text; the old normalizer recognized only the ordinary error footer. The
  normalizer now recognizes the compaction footer and hands it to the existing
  bounded, no-tool-activity recovery transition. The initial native retest then
  exposed underestimated local fresh replay; exact local-tokenizer planning
  replaced that estimate. Neither intermediate failure was called fixed.
- Local request admission now receives the exact replay region from the shared
  context plan; it no longer guesses the old XML region for a new JSON payload.
  Both local wire formats have a forced-underestimate admission regression.
- Real overflow acceptance finally passed in 148.64 seconds: actual local
  capacity 100,096; input budget 81,895; native compaction request 95,350 rejected
  (four native attempts); fresh shared-checkpoint request 43,526 admitted.
  The synthetic ORCHID-17 status changed from pending to reviewed after recovery,
  and the next Goose turn forked/resumed natively with the corrected status.
  No fabricated token counts, reduced fake capacity, host restart, or model
  configuration changes were used. Four native rejection attempts remain a
  measured efficiency limitation; no oversized inference was forwarded.
  Reproduce with `LOOM_RUN_NATIVE_CONTINUITY=1` and
  `tests/test_continuity_native.py -k local-overflow`; JUnit properties contain
  only admission counts. Test runtime is not a production TTFT claim.

### Child ownership and long-turn retrieval follow-on (2026-09-19)

- `child_context.py` adapts a child's original task and exact event prefix to
  the same `ContextEngine` and destination-budget/summary binding as top-level
  handoffs. Child checkpoints/scopes live in separate tables; their virtual
  record IDs never become conversation nodes. A subsequent append does not
  expand a scope; edits invalidate it. Parent/sibling records are inaccessible.
- Explicit paused-child recovery supplies recorded tool outcomes and a
  reconciliation instruction instead of just relaunching the bare task.
  Unverified paused native sessions are not blindly resumed. This is not a
  guarantee that a model will reconcile every external side effect correctly.
- Retrieval ownership is the actual asyncio generation task, not a persisted
  `running` flag. No heartbeat or wall-clock extension grants permanent access.
  Legacy ownerless scopes retain their original TTL; new scopes end with their
  owner, including cancellation and host restart.
- Real MCP subprocess -> isolated HTTPS -> SQLite tests passed for branch and
  child scopes, including forced TTL expiry, out-of-scope IDs and owner exit.
- Native Codex and Goose/local each retrieved an unknown synthetic value via
  MCP, and each recovered a synthetic paused child through the real supervisor
  and its own retrieval scope: four native tests passed under Balanced (51.12s),
  then passed again with independent disposable Git roots (47.66s).
  These are deliberately paused fixtures, not injected mid-write crashes.
- The native tests caught a missing permission normalization for the new
  retrieval tool. Exact Loom MCP identity now maps to read-only; sibling MCP
  names, different operations, and conflicting ACP payloads still ask. The
  endpoint independently enforces the capability. The Lean decision algebra
  is unchanged; new tests cover normalization outside its proof boundary.
- Initial Antigravity/Codex and Antigravity/Goose round trips passed their token
  and transition assertions (12 real turns, 78.77s). Later inspection of the
  local-Claude replies exposed a weak assertion: a refusal quoting the correct
  identifier/status also passed. These initial passes are not sufficient UX
  acceptance. The suite now requires the requested exact answer and uses an
  independent disposable Git root so CLI initialization cannot inherit the
  parent checkout's Git snapshot. Final stricter results are recorded below.
- A following local-Claude fresh turn failed before any handoff: 58,878 input
  tokens versus a 58,087 input allowance, because native max_tokens reserved
  32,000 output tokens. Shared local admission now first negotiates that ceiling
  rather than unnecessarily summarizing otherwise fitting history. It never
  goes below 8,192 or an explicit thinking budget plus one, preserves the safety
  margin, and reports the adjustment. After genuine compaction it can negotiate
  again if needed. Nine deterministic tests cover both wire formats, exact
  history preservation and refusal to starve output/thinking. The entire guard
  suite passed 27 tests. Final native-pair retest results are recorded below.
- Strict native pair acceptance: five pairs passed in the combined run; the
  sixth (Claude Code/local and Antigravity) timed out on its first local turn,
  before any handoff. The model requested an unexpected outside-workspace Write;
  Balanced asked rather than approving it. The unattended test now explicitly
  denies unexpected tool requests with a read-only test-user note. Its rerun
  passed all six turns (77.57s); this rerun happened to request no tools, so it
  does not establish native denial recovery. No mutation was auto-approved.
  Together, six pairs cover 36 real turns, both handoff directions, and native
  continuation on each harness. They verify exact ORCHID-17 pending/reviewed
  responses, parent IDs, saved session identity and forked/rebuilt attestations;
  they do not prove preservation of a large real project or every model pairing.
- Child preparation now uses Goose's existing selector parser, including Smart
  and local aliases, rather than a new partial prefix check. Destination backend
  and exact-tokenizer behavior therefore agree with the provider's launch route.
- Affected runs: 213 server/child/context/database tests; 120 browser, policy,
  local guard and retrieval tests; consolidated final run 317 passed; final
  child routing/runtime rerun 33 passed. These overlap and must not be added into
  a purported unique/full-suite total. Python compilation and diff checks pass.
  No live host was stopped/reloaded, no production conversation was edited,
  and the unrelated workspace_safety.py changes are outside this checkpoint.

### Real mid-tool child crash recovery (2026-09-19)

- The first acceptance test found a real admission gap: provider crashes leave
  children `failed`, but Resume admitted only `paused`. Stopped failed non-image
  children now have explicit recovery. An atomic database transition admits one
  of two competing Resume requests; cancellation or a retained PID prevents it.
  Failure alone never dispatches another attempt. Prior events/results remain.
- The Agents panel uses the same eligibility and carries explicit terminal PID
  evidence. A delayed running snapshot cannot overwrite newer teardown evidence
  or hide Resume. The browser test exercises that response race and clicks the
  actual Resume endpoint without changing the conversation, branch or cursor.
- Native acceptance exposed a second real gap before fault injection: Codex's
  `mcpServer/elicitation/request` was unsupported, so a tool approval was rejected
  before reaching Loom. The bridge now recognizes the structured MCP-tool form,
  joins its exact thread/turn/server/arguments to one pending native tool item,
  and uses existing Loom permission policy. Ambiguous or incomplete identity is
  not guessed from display text. Responses are one-time accept/decline, never
  native permanent grants. General input forms remain unsupported and explicitly
  report bridge cancellation, not a human denial. Protocol reference:
  https://learn.chatgpt.com/docs/app-server
- `tests/test_child_crash_native.py` passed both Codex -> Goose/local and
  Goose/local -> Codex recovery (2 tests, 54.38s). Each native child invokes a
  test-only MCP mutation; the fixture commits/fsyncs a synthetic receipt but
  withholds the acknowledgement. The test kills only its captured provider tree,
  verifies all captured processes exited, then explicitly resumes the same child
  under the other harness. The recovering agent must inspect the unknown receipt
  through MCP, preserve earlier events, and finish with exactly one mutation.
  A duplicate mutation is permitted by the test responder and counted, not
  concealed by denying it. No live Loom host, sidecar or user task was stopped.
- This establishes reconciliation for these two real lost-acknowledgement paths,
  not exactly-once arbitrary external effects. If no authoritative receipt exists,
  recovery must still report uncertainty instead of inventing an outcome. Full
  host crashes, other model/backend combinations and multi-hour resource behavior
  remain open acceptance gates.
- Verification so far: 227 affected tests; 3 real-browser tests including the
  delayed-snapshot recovery case. The first repository run had 1,367 passes,
  settings test reusing an asyncio lock across pytest loops. The latter fixture
  now resets its lock with its cache; production settings behavior is unchanged.
  `test_synthetic_replay.py` and `test_gemini_operator_turn2_forces_fresh_conv`:
  **1,389 passed, 37 skipped, 1 deselected in 1,048.64s**. Reproduce with
  The last MCP protocol/ordering/missing-owner changes were independently rerun
  with all Codex permission tests: **85 passed**. Final Python compilation,
  JavaScript syntax and staged diff checks passed. The broad run used the shared
  live worktree; unrelated extension/workspace-snapshot edits are excluded from
  this recovery checkpoint. Counts overlap and must not be added into a total.

### Recorded tool-boundary continuation (2026-09-19)

- Top-level Claude Code/backends, Codex, Antigravity and Goose use one recovery
  decision instead of treating all streamed output as a permanent retry barrier.
  Only explicit context failure can trigger it. Every current tool must have a
  matching recorded, non-error outcome; empty successful results count, missing
  IDs/results and error results do not. Ordinary rate/transport failures and
  successful prose mentioning context limits cannot authorize recovery.
  Goose/Antigravity streaming output is explicitly not completion evidence;
  their native terminal tool status supplies that fact. Antigravity no longer
  suppresses the completed result after emitting an earlier progress update.
- Providers are retired before planning/relaunch. The generation keeps its
  existing lease, draft and parent. Partial text, tools and token counts remain;
  a fresh session receives the shared destination-budgeted context, then the
  next normal user turn resumes it. One replacement is permitted, not an
  unbounded retry loop. A stopped task or unverified exit cannot claim it.
- An assistant-tail recovery keeps the latest user task verbatim outside the
  summarizable history. A deliberately lossy summary fixture covering every
  historical record cannot erase the current task or its constraints.
- Recovery scopes freeze the prior attempt's tail while allowing the same draft
  to append new output. Retrieval cannot expand into that new output, cross
  conversations, survive owner exit, or ignore edits to ancestors/retained tool
  records. Replacement tool IDs have a unique attempt prefix so a native ID
  reused by the new session cannot update an earlier tool card/result.
- Stream exceptions and error events normalize without pretending to be native
  completion. Native acceptance exposed Goose dropping the `message` error
  field and replacing it with `Goose error`; both normalized fields now work.
- Local-summary transport/timeouts report retained evidence and no cloud fallback,
  without exposing raw transport details. An alternative authorized summary
  backend is not silently introduced. Lack of local summarization remains a
  dependency when full retained evidence cannot fit the selected destination.
- `Recovery.lean` compiles with warnings as failures. Its 16-case fixture checks
  the actual Python launch guard; proof coverage is eligibility/exit/stop/one
  attempt, not model fidelity or OS process truth. CI regenerates the fixture
  and runs handler/retrieval recovery acceptance.
- Real local-Goose capacity overflow/recovery/follow-up passed again (165.33s).
  A separate real Goose MCP action -> injected overflow terminal -> native
  continuation passed (35.73s), with one counted action and the retained receipt.
  That second test is boundary fault injection, NOT naturally reached capacity.
  Its first run found the error-field bug; its second found an incorrect fixture
  denial of an omitted optional tool argument. Neither failed run was counted
  as successful acceptance; the production unresolved/error-outcome rule stayed.
  The final native rerun also passed (53.66s), after successful continuation was
  made to explicitly replace the persisted `partial_failed` state with `completed`.
  Subsequent native reruns passed with verbatim-task retention (24.02s) and
  explicit adapter tool-completion evidence (26.89s).
- The all-harness deterministic matrix covers completed/empty/unresolved/error
  outcomes, raised context exceptions, repeated overflow, reused tool IDs,
  progress-only output, fresh-session follow-up, and failed teardown.
- Final affected-source run: **255 passed in 105.25s**, covering the engine,
  all-handler matrix, child context, retrieval/checkpoints, recovery budgets,
  Goose ACP, AGY parsing and attestations. The AGY stream progress -> completed
  result regression also passed independently. Lean build with `--wfail`, Python
  compilation and staged diff checks passed. Native results are listed above;
  overlapping reruns are not summed into a larger test count.
- Broad regression evidence must not be overstated: the first in-flight run
  finished with 1,429 passed / 37 skipped / 7 failed / 1 deselected. Three failures
  were extension fixtures intentionally rejecting OneDrive-based state; three
  encoded the superseded blanket no-recovery rule; one inspected changed source
  using pre-edit Python line metadata. OS-temp and focused reruns resolved these.
  The second OS-temp run finished with **1,427 passed / 38 skipped / 17 failed /
  1 deselected in 1,143.40s**. It had imported server code before the recovered
  lifecycle-state correction: 16 failures were that `partial_failed` ->
  `completed` bug, and one expected the old generic Goose error message. All 17
  pass in the final 255-test run. Neither intermediate broad run is presented
  as a clean full-suite result on the final frozen snapshot.
  model sidecar was restarted/stopped; native fixtures owned their test providers.
- Final frozen-source repository run on `150d7be9ddca31c4dde4728b175763f243cc26c8`:
  **1,457 passed, 38 skipped, 1 deselected, 52 warnings in 1,023.62s**. No source
  edits occurred during this run. `tools.lifecycle_preflight.source_identity()`
  returned the same SHA-256 before and after:
  `5bf404c836a2a17e3125c7aedcd4bec95d9d6e01834bd5aec1901425e2a26512`.
  This digest includes the preserved, unrelated `workspace_safety.py` working-tree
  change; that file is not part of the recovery commit. Isolated OS-temp storage
  was used. Opt-in native tests remain separate evidence, not counted skips-as-passes.
  This closes the final repository-regression gap, not the documented multi-hour,
  full-host-crash, or additional model/backend acceptance limits.

### Removal of mandatory local summarizer and restoration of harness-independent handoffs (2026-09-19)

- Astra's changes introduced `ConfiguredLocalSummarizer` in `context_runtime.py`
  which unconditionally invoked `llama-server` on the user's local GPU for any
  handoff where history exceeded the initial input budget, with a hardcoded 4k cap.
  On long conversations (such as conversation 161 with 551 messages, ~325k tokens),
  switching between cloud harnesses triggered unwanted local GPU inference, timed
  out, and broke model handoffs even when the destination model had a 1M context window.
- `local_context_guard.py` has been restored to be self-contained for local loopback
  proxy compaction, eliminating its dependency on `context_summary.py`.
- `context_summary.py` has been retired. `LocalSummaryBackend` fails closed immediately
  with `ContextLimit` without attempting local GPU inference or network requests.
- `context_runtime.py` defaults to `DisabledLocalSummarizer`, removing automatic local
  GPU summarization from production cloud handoffs while preserving explicit test doubles
  and fail-closed budget diagnostics.
- `continuity_engine.py` now scales summarizer output budget dynamically with destination
  capacity (`min(remaining, max(4096, input_budget // 8))`) instead of a fixed 4,096 tokens.
- All test suites verify clean behavior: 24 tests in `test_continuity_engine.py`,
  24 tests in `test_continuity_launch_matrix.py`, 14 tests in `test_child_context.py`,
  57 tests in `test_turn_context_recovery.py`, 27 tests in `test_local_context_guard.py`,
  13 tests in `test_context_manual.py`, 10 tests in `test_context_checkpoints.py`,
  and 3 tests in `test_context_summary.py`.

## Reference, not an implementation dependency

Polytoken documents durable session history separate from bounded model context,
recorded compaction, persistent todos, and admission of summary requests:
https://docs.polytoken.dev/using-polytoken/sessions/

Borrow those ownership principles. Its single-harness documentation does not
prove cross-CLI handoff, and it is not a drop-in native-session converter.
