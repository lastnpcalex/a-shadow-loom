# Portable context: stress-tested contract and implementation plan

Date: 2026-09-19. Baseline inspected: `0b250b4` (including `7195767`).
Status: PLAN AND REGRESSION TESTS ONLY. Production continuity is not repaired.
This document refines CONTINUITY_EXECUTION_PLAN.md using the user's five explicit
criteria below. Preserve that document and CONTINUITY_REPAIR_PLAN.md as history.
No further user architecture decision is needed to begin implementation.

## Verdict

The design is coherent **with both proactive maintenance and destination-side
overflow handling**. Periodic compaction alone does not meet criterion 3.
It cannot prevent an already-large branch, a giant new record, an edit invalidating
its summary, a failed maintenance job, or a smaller destination from exceeding
the available budget. These cases must use the same incremental compaction path.

SQLite retains the original evidence. A portable summary is an acceleration of
that evidence, not a substitute for it. Reuse the existing checkpoint storage
where suitable; this does not require another summarizer service or database.
The word checkpoint in existing code means the persisted summary plus coverage.
The user did not request a separate service to manufacture checkpoints.

This architecture supplies the missing success path. Neither this plan, Lean,
nor offline test success can establish that every model will preserve task
semantics. Completion requires the native behavioral evidence defined below.

## The five acceptance criteria

| Criterion | Required implementation | Evidence required |
| --- | --- | --- |
| Compatible same harness -> native resume | Resume nearest compatible native session; no full history replay or synchronous maintenance prerequisite | Actual session ID/fork lineage, next-turn success, zero replay/preparation calls in the ordinary launch |
| Cross-harness fitting context -> direct replay | Measure the complete destination request; send fitting canonical working context directly | Rendered source coverage, current request unchanged, zero preparation calls |
| Oversized handoff -> destination incremental compaction | Chunks, including giant-record fragments, are processed in order by the selected destination, with rolling compacted state; preserve a bounded recent tail verbatim and execute normally | Observed destination calls, contiguous coverage, finite call count, retained tail, successful ordinary turn and next native resume |
| Retrieval -> fallback only | Record exhausted bounded preparation, validation failure, or verified unsupported preparation before choosing retrieval | Failure reason and ordered trace; no retrieval-first substitution for a missing implementation |
| No unselected inference dependency | All preparation uses the selected harness/model/backend and its existing credentials; local inference only when explicitly selected | Network/process observations, including a cloud run with local service unreachable and no extra API credentials |

Here, fitting replay means the current canonical working view (valid summary,
if already compacted, plus uncovered history). When there is no valid summary,
it means raw branch history. Do not re-expand old covered records on every switch.
Existing compacted views may be reused without the originating model available.

## Stress tests of the proposal

| Case | Why an 80k promise alone fails | Required behavior |
| --- | --- | --- |
| 80k history into an 80k server | Instructions/tools/media/output also occupy capacity | Budget the entire request, reserve headroom, then allocate history |
| Current turn grows from 50k to 140k | A post-turn trigger cannot prevent the intermediate overshoot | Compact sealed prefixes at recorded tool boundaries where supported; otherwise compact incrementally at handoff; never summarize an unresolved effect as complete |
| Existing 500k branch -> 80k destination | No prior proactive summary exists | Destination chunk/compact loop; no dependency on the source or a larger model |
| Smaller runtime capacity than catalog | A nominal portable floor is insufficient | Runtime limit wins; resize chunks and rendered view for that destination |
| User sends during maintenance | Summary is incomplete or based on an older frontier | Resume remains usable; fresh handoff reuses completed work or prepares its own view under the run reservation |
| Edit/fork while a summary finishes | Late publication could attach sibling/stale state | Compare the covered prefix in one DB transaction; reject stale publication; appending after the prefix is allowed |
| Invalid/empty/oversized summary | A 200 response is not useful compacted context | Validate, retry with changed measured bounds within a finite budget, then retrieval fallback |
| Stop during chunk 4 | Cancellation must not start normal work or a fallback | Cancel preparation, verify owned process exit, retain accepted chunks, leave queued work paused |
| Failure after a tool wrote a file | Replaying the task can duplicate effects | Carry recorded receipts and uncertainty; reconcile before another mutation |
| Current request or protected tail alone exceeds capacity | It is impossible to keep it all verbatim inside that smaller request | Preserve exact source in SQLite and use explicit bounded document retrieval; disclose this exceptional mode rather than silently dropping instructions |

80,000 is a maintenance target, not proof that 80,000 history tokens fit every
destination. Define a complete portable envelope `P` (initially 80,000) and use:

`history_allowance = max(0, min(P, destination_capacity) - all_other_request_costs)`

All other costs include current request, retained tail when budgeting the older
prefix, instruction/permission/Canvas notices, tool schemas, media, output and
uncertainty reserves. Count each item once. CLI overhead estimates must be named
as estimates. Final admission uses the actual renderer and runtime capacity.
An existing raw branch that fits a selected large destination still gets direct
replay; the maintenance target must not become a new refusal gate.

## One owner and one compaction operation

Evolve `continuity_engine.py`, `context_runtime.py` and `context_checkpoints.py`.
Keep source selection in one decision path; provider adapters supply mechanisms.

1. Resolve the immutable branch prefix, destination and nearest native session.
2. Ordinary compatible resume executes immediately. Portable maintenance is a
   separately recorded operation; it cannot replace the user turn or cause a
   fresh session merely because cumulative SQLite history is long.
3. A fresh launch selects the newest valid portable summary plus uncovered
   records, or full raw history if none exists. Render and measure it.
4. If it fits the destination, launch directly.
5. Otherwise, prepare through that destination incrementally. Both proactive
   maintenance and oversize handoff call this same operation, with different
   triggers. The source harness is never a prerequisite for a handoff.
6. If bounded preparation fails or the adapter proves it cannot support it,
   start explicit retrieval fallback on the selected destination. Missing code
   is an unfinished adapter, not evidence that its harness is incapable.

Before implementing, record the installed protocol/CLI evidence for each
adapter's tool restrictions, session persistence, compaction and output format.
Do not invent flags or assume every CLI supports the same compaction method.

The adapter operation uses the existing account/model route:

- Start an owned preparation session, with tool execution disabled/enforced.
- Feed a bounded historical chunk labeled as evidence, with prior rolling state.
- Compact through a verified native operation when portable state can be
  obtained, or request a structured text state from the same selected model.
- Merely asking for a summary does NOT shrink an existing native transcript.
  Without native compaction, the next bounded request must use only accepted
  rolling state plus the next chunk in a fresh preparation context.
- Continue until the older prefix is covered. Preserve the chosen recent tail
  and current request verbatim. Do not silently reduce the tail to zero.
- Where tools/session configuration can safely change, the prepared native
  session may become the working session. Otherwise start the ordinary working
  session from the compacted state and recent tail using the same destination.
  Persist the ordinary working session ID for subsequent native resume.
- Native opaque compaction alone is not a portable summary. If the native
  representation cannot transfer, produce the portable text state explicitly.

Every chunk carries record IDs and fragment offsets. Validate contiguous,
non-overlapping coverage and bound the complete request, including prior state
and requested output. A giant record is split at safe text boundaries and keeps
its owning record/tool identity. Never execute historical tool calls.
Structured state should retain objectives, latest corrections, pending work,
artifact references and unresolved issues with source IDs. Tool receipts, active
child IDs and uncertainty come from DB evidence, not a model's completion claim.
JSON/schema validation verifies structure, not factual or semantic completeness.

## Incremental maintenance and publication

Maintain a portable view for each branch prefix. Start maintenance before the
portable envelope fills; initial trigger: 70% of the current computed envelope,
with a lower target after compaction to prevent repeated tiny compactions.
Tune targets using measurements rather than treating 8k/16k summary sizes as law.
Size the output against destination capacity and retained tail; no universal 4k cap.

Use the current selected model during routine maintenance. A separate owned
preparation context must not alter the native working session. Store operation
ID, source prefix/revision, destination route, coverage offsets, state text,
version and acceptance measurements. Reuse completed fragments by source digest
and route so a retry does not process the entire branch again.

Schedule on stable recorded prefixes at turn/tool boundaries, deduplicate jobs,
and cap concurrency per workspace. It shares the existing run/lease supervision;
do not introduce an unbounded background worker. Maintenance is a visible state
distinct from a still-streaming user answer. Finish/cancel/teardown semantics must
be explicit before queue dispatch. A failed maintenance operation cannot turn
a completed user answer into failed or silently cancel its native resume.

Publish only after verifying the covered prefix inside the transaction. Reuse
across descendants is permitted only when that exact prefix remains an ancestor.
No implicit conversation-wide latest summary. A fork before the boundary cannot
inherit it; appending after it does not invalidate it. Model changes alone do
not invalidate portable text. Validate newly selected route/tool restrictions
separately. Preserve raw records, native session IDs, cursor and queue parents.

## Implementation slices and exit evidence

1. **Contract/routing:** use the Lean decision model below; inventory every
   top-level, queued, stale-resume, manual and durable-child launch. Each path
2. **Production preparation:** wire one adapter interface into the real runtime
   for all supported destinations. Prove tool restrictions with an attempted
   forbidden action. Implement bounded chunk processing and resume of accepted
   preparation progress. Unsupported configurations are listed explicitly.
3. **Storage/maintenance:** reuse checkpoint storage with automatic valid-prefix
   lookup, atomic publication and scheduling on sealed evidence. Manual compact
   produces an actual compacted state, not a retrieval marker labeled success.
4. **Integration:** reuse this operation on oversize handoff, native overflow,
   manual compact and child recovery. Final render changes return to the same
   budget/decision path. The actual working session launches and resumes next.
5. **Acceptance/removal:** run the test layers below and remove superseded
   automatic history/summary selection only after caller coverage is established.

Slices are dependencies, not permission to call a partial repair complete.
No host restart is part of implementation/testing. Use isolated DBs, listeners,
synthetic evidence and owned provider processes. Human activation comes after
the reviewed result. Do not blanket-revert the dirty workspace or earlier fixes.

## Tests delivered now and the test rewrite

Run the new executable requirements:

```powershell
C:\Python314\python.exe -m pytest tests/test_portable_context_requirements.py tests/test_portable_context_spec.py tests/test_portable_context_trace_oracle.py -q --tb=short -p no:cacheprovider
```

These tests intentionally fail on missing behavior. They are normal tests with
no xfail/skip; therefore the default repository suite will also report these
known gaps until implemented. Do not suppress them to report a green build.
They block network/subprocess inference and use the shared isolated SQLite fixture.

Observed on `0b250b4`, final new-suite run: **17 failed, 30 passed** in 13.94s.
That includes 13 passing mutation/acceptance-checker self-tests, not native runs.
Before adding the checker, the requirements/specification tests plus five existing
engine/runtime/storage/matrix/summary files gave **17 failed, 80 passed**.
All 63 tests from those existing files passed while the new requirements exposed
17 failures. Lean also compiled with warnings treated as errors.

- 12 failures: real `_prepare_agent_handoff` -> runtime has no destination
  adapter call before retrieval. Adapter-entry traps inject a failure, never a
  successful summary. This proves a missing binding, not successful compaction.
- 1 failure: a saved automatic summary is ignored when its producer is absent.
- 3 failures: context-limit, empty and wrong-route preparation output strand
  the branch instead of falling back. The timeout and cancellation cases pass.
- 1 failure: oversized current input errors before its retrieval path is reached.
- Passing tests cover engine native bypass, fitting cloud replay without network,
  the headroom counterexample, prefix inheritance/invalidation and retained tail,
  and the generated Lean oracle's completeness.

`tests/portable_context_trace_oracle.py` is a reusable checker for a native
runner's observations. It rejects missing/overlapping/reordered chunks, foreign
routes, lost rolling state, exceeded budgets, tool-enabled preparation, changed
tail/current request, retrieval substitution and missing native follow-up.
`test_portable_context_trace_oracle.py` mutation-tests that checker. Its synthetic
successful trace is a test of the checker, never claimed as a real model result.
The production-boundary recorder and native runs remain implementation work.

The initial test run had a new fixture error (`current_task` outside an event
loop). That test bug was removed before the reported run. No production files
were changed to obtain the results.

Keep useful existing tests; replace their *acceptance role*, not all their code:

| Existing tests | Keep as | Add/replace before completion |
| --- | --- | --- |
| `test_continuity_engine.py` | Decision/serialization unit tests | Compare the runtime decision with all 128 Lean cases; replace current-input fail-closed expectation with the explicit document exception |
| `test_continuity_launch_matrix.py` | Synthetic handler wiring tests | Real serial generation entry, real planner/budget/renderers, fake only protocol boundary; do not inject `SyntheticSummary` into production availability |
| `test_context_summary.py` | Chunk algorithm unit tests | Actual selected adapter route, giant heterogeneous records, Unicode, output-limit refusal, bounded retries, resumed progress; remove universal 4096 output expectation |
| `test_context_runtime.py` | Tokenizer/reserve unit tests | Actual rendered payload, notices/images/large schemas; final fit revision uses same planner; no mocked `assess_payload` in integration acceptance |
| `test_context_checkpoints.py` | Storage and retrieval safety | Automatic-prefix reuse across providers, stale publish on edit, inherited prefix after fork, append during preparation, crash between save/publish |
| `test_context_manual*` | UI and transactional boundary tests | Compaction using production executor; a retrieval marker must not pass as a generated summary |
| `test_continuity_native_pairs.py` | Short native smoke tests | Oversized varied history and semantic task tests through serial admission, return handoff and native follow-up |
| `test_turn_context_recovery*`, `test_child_context*` | Recorded effect/teardown tests | Real Goose in-tool overflow, bounded selected-model recovery, child-owned evidence/descendants, no duplicate effects |

Additional REQUIRED integration scenarios, not delivered by the offline tests:

- All 12 directed pairs; source/target return and same-harness follow-up. Source
  evidence lives in SQLite; do not require source authentication during handoff.
- Hundreds of varied messages exceeding *measured* destination capacity, a single
  record larger than a chunk, an image and long tool output. Recent tail has
  explicit equality assertions. Current request exceeds 4k but fits total budget.
- Observe >=2 accepted preparation calls when the fixture requires multiple
  chunks. Check coverage union/order/offsets and exact selected model identity.
- Early instruction says A; later user corrects it to B; a tool effect completed
  once and another is uncertain. Current request: "continue". Check the correct
  resulting artifact/action and a counted side effect, not recalled keywords.
- On preparation timeout/invalid output/unsupported operation, observe fallback
  retrieval and a correct task result, not merely the presence of a scope token.
- Cloud runs with local endpoint unreachable; no connection attempts there or
  unselected providers. Local Goose is tested separately at actual per-slot limit.
- Two tabs/branches, serial + parallel modes; queue while compaction runs, edit,
  disconnect, delayed snapshot, stop, resume. Assert parent/run/view identities
  and provider overlap. Maintenance must not revive a cancelled generation.
- Incremental cost: second compaction processes only previous accepted state and
  new evidence; no complete raw-history reinference or unbounded per-event DB scan.

Deterministic protocol doubles are useful for race exploration. They must not
replace the planner, renderer, budgets, DB or production preparation binding.
Native acceptance uses the real selected CLI/auth. No paid Claude subscription
is required for offline progress; unavailable native configurations stay marked
unverified. This task did not spend model quota running those acceptance cases.

An acceptance report needs source digest, harness version, configured/effective
model, capacity provenance, call sizes/counts, selected route, phase timings,
branch revision, task assertions and limitations. No bearer tokens or personal
transcripts. A missing observation is a failure or unverified gate, never a pass.

## Lean deliverables and exact proof boundary

`formal/portable_context/Model.lean` compiles on Lean 4.33.1. It proves native
priority, stop priority, failure-only retrieval, admission before execution,
contiguous accepted fragment progress, cancellation stability, prefix partition
coverage and inclusion of output headroom. It generates 128 finite decision cases.
`test_portable_context_spec.py` checks this oracle. **Production is not yet wired
to this model**, so this is not a claim of runtime conformance.

Next formal work is specified in `formal/portable_context/README.md`: full
attempt/revision/owner transitions, finite retries and conditional liveness;
generated cases must drive the production reducer's tests before activation.
No Lean proof certifies model fidelity, OS process truth, hash collision freedom,
tool lockdown, hidden provider tokenization, or correct adapter normalization.

## Done means

All five rows have separate implementation, deterministic integration, native
acceptance and activation evidence on one source snapshot. Proactive maintenance
must demonstrate growing branches stay portable under the declared workload;
legacy/oversize handoffs must demonstrate successful destination compaction.
Retrieval-only launches do not satisfy the ordinary oversized path. An adapter
that is missing remains missing, regardless of how many synthetic tests pass.

This plan addresses continuity. It does not assert that all UI, permissions or
queue defects are repaired. Their integration assertions are required so the
continuity implementation cannot claim success while breaking ordinary sends.
