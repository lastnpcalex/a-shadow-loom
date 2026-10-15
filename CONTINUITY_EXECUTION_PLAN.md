# Continuity and queue repair: execution brief

Date: 2026-09-19. Status: PLAN ONLY. No implementation or acceptance claimed.

This brief defines the next repair against the live working tree. It supersedes
conflicting implementation assumptions and completion claims in
CONTINUITY_REPAIR_PLAN.md; preserve that document as historical evidence.
The user requested a plan for another implementer. Preparing this document does
not authorize this author to implement or activate the repair in this turn.

## 1. Deliver these five user outcomes together

1. Continue a compatible native session without replaying the entire branch.
   Switch harnesses and return while retaining intervening work and corrections.
2. A long history, including a single large historical turn, does not make the
   branch unsendable. Fit the destination through bounded context and retrieval.
3. A cloud destination works with llama-server absent. No automatic local model,
   CPU summarizer, extra cloud account, or larger-context model is prerequisite.
4. Queued continuations remain attached to the recorded thread and predecessor.
   Switching tabs, refreshing, reconnecting, or changing models cannot redirect
   their parent, launch, stream, permission requests, or terminal state.
5. Every supported agent entry point consumes one authoritative continuity plan,
   and the same production path is exercised by acceptance tests.

Supported harnesses: Codex, Antigravity, Goose, and Claude Code, including their
supported model backends. Weave's intentional chat-completion interface and
No Hermes revival, unrelated UI redesign, worktree workflow, or model inflation.

## 2. What was inspected, and what is still unknown

Read on 2026-09-19: context_runtime.py, continuity_engine.py, child_context.py,
event_router.py, server.py queue dispatch, tests/test_continuity_launch_matrix.py,
and the existing repair plan. These files have ongoing changes: reread before
editing and preserve changes belonging to other work.

- prepare_snapshot currently accepts a summarizer argument, but ordinary callers
  do not thereby acquire a production summary executor. Without a usable cached
  checkpoint, oversized history raises ContextLimit when no summarizer exists.
- Native selection lives outside the engine; prepare_snapshot supplies
  NativeCapability(None, reason). A class named ContextEngine alone therefore
  does not establish one owner of all continuation decisions.
- Planning budgets a partial payload and reserves space for later additions.
  The destination adapter performs another admission check. Trace the actual
  rendered requests to establish where these measurements disagree.
- The existing handler matrix replaces provider launches, capacity assessment,
  and the summary binding. It sets parallel_agents_enabled=1. Keep these unit
  tests, but they cannot establish native routing or serial queue invariants.
- Queue dispatch uses persisted predecessor drafts and leases. The reported
  wrong-window symptom is not yet attributable to one write or event. Reproduce
  it and trace request, queue row, parent, subscription, and DOM ownership before
  choosing its fix. Correct database topology alone is insufficient.
- Historical acceptance ledgers are reports of earlier snapshots. Recheck the
  current route; a previously passing test is not present-source evidence.

## 3. Required design decisions

### One plan, with adapters supplying mechanics

Evolve the existing engine/runtime and ownership structures; do not add another
competing planner. Record a typed plan before provider execution containing:

- Workspace, conversation, thread, immutable source revision, current user ID,
  queue/predecessor identity if applicable, owning run and attempt, origin page.
- Destination harness/model/backend and native compatibility evidence.
- Mode: native continuation, fitting replay, checkpoint handoff, retrieval-led
  handoff, or recovery; reason and prior action outcomes.
- Selected evidence, exact retrievable coverage, checkpoint provenance, selected
  attachments, effective capacity source, and complete request budget.
- Permitted preparation executor, retrieval scope owner, and lifecycle state.

Adapters report capabilities and render/execute this plan. They do not select a
different history, secretly compact, or silently choose a summary provider.
Separate wire formats and provider protocol error handling remain appropriate.
Keep native sessions as the fast path: no full-history serialization, summary
request, or cumulative-history gate for an ordinary compatible continuation.

### Destination-owned preparation, with a usable fallback

Default preparation uses the user's selected destination harness, model, backend,
and existing authentication. Choosing Codex must not require a separate OpenAI
API key; choosing Antigravity must not require llama. Use a separate preparation
session so summarizing historical evidence cannot mutate the working native
session. Selecting a local destination intentionally uses that local backend.
Respect existing role restrictions; do not transfer data to another provider
or reinterpret Search Space flags as general conversation policy.

Verify each harness can execute bounded preparation with tools disabled or with
an enforced read-only evidence surface. A prompt saying "do not write" is not
enforcement. If that preparation capability is unavailable or fails, the shared
planner selects retrieval-led handoff on the chosen destination. Do not make
preparation support another compulsory gate that leaves the branch unsendable.

Context construction order:

1. If the complete rendered branch fits, send it directly.
2. Reuse a valid branch/revision-bound checkpoint plus uncovered recent evidence.
3. If needed, reduce older evidence through the selected destination in bounded
   chunks and bounded merges. Split within giant records as well as between
   messages. Count every chunk, merge, tools/instructions, and output allowance.
   Size output against useful destination space; no universal 4,096-token cap.
4. If preparation is unavailable, times out, or fails validation, launch a bounded
   retrieval-led continuation on that destination. Include the current request,
   recent context that fits, validated checkpoints if any, recorded unresolved
   actions/child IDs, and a paginated evidence index. State gaps explicitly.

Retrieval-led continuation is an actual agent workflow: it can search and fetch
exact branch records and image evidence, reconcile the active task, and then
perform the requested work. An empty prompt plus a token, the last few messages
alone, or an instruction to "start a new chat" does not satisfy this requirement.
When no validated task summary exists, the destination reconstructs it from
bounded evidence before taking consequential actions. Never fabricate goals
from truncation. Keep historical instructions distinguishable from current ones.

Preserve objectives, corrections, decisions, unfinished tasks, artifact pointers,
verified completed effects, uncertain effects, and child ownership. Checkpoints
carry source references; old records stay intact. Test retention behavior, not
merely the appearance of expected words in a response. Compression cannot promise
lossless recall or identical personality; exact information remains retrievable.

Current user input normally remains verbatim. If it alone exceeds capacity,
preserve the full input as an exact retrievable document and admit a bounded
processing request without changing its authority. Test that path separately.
An unavailable selected provider or a task requiring an indivisible input larger
than its capacity may still fail visibly. Old history volume or an unavailable
unselected llama server is not an acceptable terminal failure reason.

### Complete-payload admission and bounded recovery

Assemble/render before final admission. Include instructions, permission/Canvas
notices, tool schemas, media, retrieval responses, output headroom, and measured
or explicitly estimated hidden harness overhead. Use one budget object through
planning and execution. Runtime capacity wins over catalog guesses; do not claim
exact counts when a CLI hides its internal payload.

A final oversize rejection returns to the same planner with the revised budget.
Reduce the evidence view and retry within a recorded attempt limit. Each retry
must change a measured constraint; identical repeated requests are forbidden.
If a native session exhausts context, prefer supported native compaction, then
use the same checkpoint/retrieval workflow. Exercise Goose-to-Goose recovery
inside a tool loop and a successful later native continuation.

Preparation has a finite configurable time/call budget, progress events, and
cancellation. Reuse completed chunks keyed by evidence revision and route so a
retry does not process hundreds of messages again. No background summarization
worker is needed. Preparation and replacement providers belong to the existing
run and workspace reservation; verify old execution exits before replacement.
Preserve completed side effects; reconcile uncertain outcomes before repeating
them. Child recovery uses its own evidence and the same planning rules.

### Queue, run, and view identity

Freeze send intent at submission: continue, explicit fork, regenerate, or new
root. Persist an idempotency key, thread, anchor, predecessor, origin page, and
settings used for the queued turn. The default queued continuation follows its
recorded predecessor, never whichever branch is selected when dispatch occurs.
Multiple queued continuations form their intended serial chain; only explicit
fork intent creates siblings. Resolve any pending-parent relation atomically.

Claim queue work and reserve its run atomically, with recoverable state if the
process fails between claim and launch. Read-only run watchers must not gain
cursor ownership or permission authority. Reconnect restores explicit run
subscriptions; focus is never a substitute for run identity.

Render live events and snapshots by run/draft identity and revision. Ignore
stale snapshots and duplicate terminal events. Navigation detaches a view without
cancelling the run or moving another page. A queued row stays below its recorded
predecessor even after refresh; POST/WebSocket ordering cannot append it to an
unrelated visible branch. Normal send and edited-branch send must remain usable.

Successful completion plus verified teardown may dispatch the next continuation.
Cancel, interrupt, preparation error, or crash leaves it paused for explicit
resume. Preserve the existing Cancel-discard / Interrupt-retain distinction.
Create durable visible run state before preparation. Every prelaunch failure
must terminate its loading UI, record a useful error, and remain visible after
reconnect, even when no provider or assistant text was produced.

## 4. Implementation order and concrete deliverables

These phases are dependencies across all harnesses, not a Goose-first rollout.
Do not treat a completed phase as completion of the user task.

1. **Freeze the contract and reproduce failures.** Inventory every production
   entry for fresh/resume/fallback/manual compact/overflow/children/queued launch.
   Record its current decision owner and renderer. Add failing production-path
   reproductions for the five user outcomes. Use private incident IDs only in
   local investigation, never commit real transcripts or credentials.
2. **Make the existing contract authoritative.** Introduce the typed plan and
   adapter render/capability boundary across all supported routes. Connect run,
   queue, source, and page identities. Preserve ordinary native resume. Deliver
   an entry-point matrix with no unexplained ordinary launch bypass.
3. **Implement bounded transfer and recovery.** Supply destination preparation,
   checkpoint reuse, working retrieval-led fallback, giant-record processing,
   complete-payload budgeting, and native overflow handling. Integrate manual
   compact and children into the same route. Reproduce with llama unavailable.
4. **Close the lifecycle and browser races.** Fix reproduced queue placement,
   duplicate submission, stale snapshot, prelaunch failure, and reconnect errors
   using the same persisted identities. Test serial and optional parallel modes.
5. **Remove competing routes and prove the final snapshot.** Disconnect ordinary
   callers of superseded history/summary policies; retain explicitly documented
   historical readers and out-of-scope operators. Run the acceptance matrix and
   relevant regressions, record results, and checkpoint the reviewed change.

Required artifacts: caller/owner matrix, current implementation status by
requirement, reproducible acceptance runner, sanitized results tied to source
digest and harness versions, and a deployment/rollback note. Use existing test
and preflight infrastructure where useful; add no second status dashboard.

## 5. Acceptance gates that cannot be replaced by test totals

| Gate | Required demonstrated result |
| --- | --- |
| Native continuation | Each harness resumes/forks nearest compatible session; no replay or summary; model compatibility comes from adapter evidence |
| All directed handoffs | All 12 routes traverse real production orchestration; available native routes perform handoff, return, and follow-up |
| Oversized history | Hundreds of varied records, one giant historical record, corrections, tool evidence and images exceed destination capacity; resulting task succeeds |
| No local dependency | Cloud-to-cloud handoff with local endpoint unreachable; instrumented attempts to reach local inference equal zero |
| Preparation failure | Inject timeout/refusal/truncated summary or unavailable preparation capability; destination retrieves evidence and completes the task |
| Semantic continuity | Use early/late conflicting facts, superseded instructions, pending work, and a counted completed action; correct result, source retrieval, no repeated effect |
| Local Goose overflow | Reach actual runtime capacity, fail native compaction, recover, then complete another native turn; enforce bounded calls and record sizes |
| Final wire budget | Real renderers and actual capacity sources; notices/tool/media overhead cannot create a second admission trap |
| Queue identity | Two tabs on different branches, serial and parallel modes, several queued continuations, edited forks, refresh/refocus/reconnect; inspect parents, run subscriptions, and actual DOM |
| Event races | POST before/after stream start, delayed snapshots, duplicated events, missing terminal notification; no wrong-window text or stuck Looming |
| Stop and preparation error | Queue stays paused after cancel/interrupt/error; explicit resume works; visible error survives refresh; no provider overlap during teardown |
| Child recovery | Child-owned evidence, permissions, active descendant IDs and tool outcomes survive; no parent/sibling evidence substituted |
| Resources | Record send-to-draft, preparation, first output, retrieval, repeated handoff cost and browser growth; budgets declared before acceptance, no unexplained unbounded loops |

Deterministic tests may mock network/provider responses to explore scheduling and
faults. Keep real planner, routing, renderer, budgets, database, and browser state
under test. Native acceptance must use the production executor/auth route and
real model responses. No monkeypatching a summary binding into availability.
Preserve serial leases in serial tests. A tool-spawned child or summary provider
must count toward measured execution overlap.

Run adversarial fixtures larger than the measured destination capacity, with
content beyond repetitive filler. Include the latest user input over 4k tokens.
Verify behavior through the next task, not just one recalled keyword. Test
semantic retention in repeated trials and publish failures as well as passes.

CI must reject ordinary launch bypasses, accidental local summary dependencies,
changed queue parents, and stale snapshot resurrection. Use static caller checks
plus dynamic entry-point assertions; neither alone proves universal coverage.
Extend existing Lean coordination models for plan/run/queue revision transitions
where useful, with reducers checked against them. Lean does not certify model
fidelity, external process truth, or adapter wiring.

The user has no paid Claude subscription. Do not require one to make progress.
Test supported Claude Code/local routes where available; list unavailable native
backends as unverified. Do not equate those with failures, silently drop their
rows, or claim they passed. Make completion claims per tested configuration.

## 6. Completion, ownership, and activation

Each requirement has four separate states: implemented, deterministic acceptance,
native acceptance, and running-version activation. Link evidence for each; a
documentation checkbox or passing mocked matrix cannot promote another state.
If a required executor or fallback does not exist, label it missing functionality,
not merely "verification remaining."

Implementation is complete for the declared available configurations only when
all five user outcomes pass their applicable gates on one final source snapshot,
the ordinary caller inventory has no bypass, and the regressions are reviewed.
Do not count removing the local summarizer or increasing a token limit as repair
of oversized handoffs. Keep real errors distinct from continuity dead ends.

Testing uses isolated databases, synthetic project evidence, owned provider
processes, and isolated listeners. Never stop/restart the active Loom or admin
host. Preserve ignored Canvas tools and all unrelated work. Stage and commit only
attributable changes after reviewing the diff; no production transcript fixtures.
Provide the human with exact activation requirements and tested source identity
when implementation is ready. A restart is not part of this planning task.
