# Coordination calculus: first runtime slice

This is a stateful model, not a second permission table. Lean is a development
and CI dependency only. Python and browser JavaScript remain the runtime.

## Model and invariants

`LoomCoordination/Model.lean` defines `step(State, Event) -> (State, Effect)` for
one predecessor run and one queued continuation. State keeps terminal outcome,
process-exit acknowledgement, lease ownership, queue state, explicit resume
authorization, and diagnostic error evidence separate. It is **not** the whole
scheduler or a replacement database schema.

Events distinguish arbitrary tool text from native terminal evidence, user
stop, process exit, lease release, explicit resume and queue dispatch. Terminal
outcome is stable within a model run. Exit without a terminal becomes failure;
a diagnostic alone cannot override confirmed completion. Queue dispatch cannot
run while leased or before exit. Paused work requires explicit resume.

Theorems establish:

- Tool text is inert for every string, not merely example error phrases.
- Confirmed completion supersedes prior diagnostics.
- Release requires exit; dispatch requires exit and a released lane.
- Unsuccessful, unapproved predecessors cannot launch queued work.
- Paused work cannot dispatch; repeating dispatch does not launch twice.
- `not leased -> exited` is preserved by every transition and, by induction,
  arbitrary finite event traces starting in a safe state.
- A separate view reducer rejects foreign identity and prevents a landed view
  from being replaced by a running snapshot.

The view rule deliberately does not order two streaming snapshots or two landed
revisions. Exact identity and richer revision ordering remain separate concerns.

## Runtime connections

`LoomCoordination/Launch.lean` additionally models browser launch feedback.
Submission becomes visible immediately; admission becomes running; transport
loss becomes reconciliation rather than idle. Every finite transition trace
after submission retains feedback, and repeated submission cannot reset an
outstanding action. `static/coordination.js` mirrors this reducer and `chat.js`
uses it for owned launch operations. The generated 30-transition launch oracle
is compared against the JavaScript implementation and checked for drift in CI.

These theorems do not prove DOM visibility, pointer hit testing, eventual
provider completion, or an authenticated public tunnel. Browser interaction
checks cover send, rerun, live text, connection loss before acknowledgement,
reload during a run, foreground recovery, terminal rendering and composer
availability. They execute real HTTP/WebSocket admission against disposable
servers with controlled providers. Actual tunnel/device verification remains
a separate deployment obligation; a passing reducer is not that verification.

| Boundary | Consumer and effect |
| --- | --- |
| Confirmed result versus earlier diagnostics | `server.py` Claude-compatible result loops use `coordination.result_failed`; only confirmed Codex native results get terminal authority here |
| Completion versus teardown | `_generation_task_entry` uses `finish_effects` before release/queue dispatch; CLI handles, including retries, must acknowledge exit |
| Durable queue predecessor | `_dispatch_ready_queued_generations` uses `queue_effect`; eligibility still goes through SQLite claim and exclusive launch admission |
| Stale browser snapshot | `static/coordination.js` is loaded before chat; the active-snapshot filter and branch-response merge use its `viewStep` |

SQLite stores existing lifecycle labels. Boundary projections normalize legacy
`cancel`, `interrupt`, and `partial_failed`; unknown terminal labels cannot
authorize automatic continuation. A paused queue is not made runnable merely
because an authorization flag exists: the explicit Resume operation must first
change its queue state.

This integration also restricts the task-done release fallback to tasks which
never entered their coroutine. It must not override a finalizer retaining its
lease. CLI cleanup keeps orphan-tracking rows when exit is unacknowledged.
If teardown fails, queued work is paused and the lane stays held for diagnosis;
this is a safety property, **not a proof of eventual recovery or availability**.

## Proof, conformance, integration

From this directory, with the pinned toolchain installed:

```text
lake build --wfail
lake exe coordination_fixtures generated/cases.json
```

`Main.lean` generates 3,360 transitions (240 finite states x 14 representative
events), eight view cases and five multi-event traces. All terminal outcomes,
flags and queue states are enumerated, including unreachable states. Strings
are represented by two samples in fixtures; their arbitrary-string property is
proved separately. `tests/test_coordination.py` compares Python and Node against
this oracle and deliberately broken reducers must fail conformance.

The [lifecycle preflight](../../LIFECYCLE_PREFLIGHT.md) adds real SQLite/task
admission, provider doubles, real HTTP/WebSockets and Chromium. It covers held
leases after missing exit acknowledgement, early cancellation, successful
completion after tool errors, paused queues, stale snapshots, and follow-ups/
edited branches with exact per-page cursor and native-session assertions.

`.github/workflows/coordination.yml` builds with warnings treated as failures,
regenerates and compares the oracle, then runs the default lifecycle preflight
on Windows. That runner rejects skipped tests, missing observations and changed
source. Windows is supported by the [official Lean action](https://github.com/leanprover/lean-action#quick-setup).
The workflow has no paid model calls, secrets or production lifecycle controls.
Making its check mandatory in GitHub branch protection is a separate repository
setting; adding the workflow does not change that setting or deploy anything.

## Trust boundary and unfinished work

`Recovery.lean` adds a separate bounded continuation guard, used by
`turn_recovery.claim` immediately before an overflow replacement launch.
It proves that launch requires eligibility and exit evidence, no stop request,
and an unused recovery attempt; applying the transition twice cannot launch
twice. `recovery_fixtures` emits all 16 Boolean input combinations, checked by
`tests/test_turn_context_recovery.py` and regenerated in CI.

Eligibility is still supplied by runtime event normalization (explicit context
failure and recorded, non-error outcomes for every tool call). The proof does
not establish that a provider reported a truthful outcome, that a process tree
was correctly reaped, or that a summary/model will preserve task semantics.
All-harness handler tests and native acceptance address those separate seams.

The proof is of the Lean model. Exhaustive finite conformance is not a proof of
the Python interpreter, JavaScript engine, adapters, OS or database. Runtime
consumers currently project existing state at selected decision points; they do
not yet replay all real events through one persisted state machine. A correct
reducer fed wrong evidence can still make the wrong real-world decision.

In particular:

- Provider parsers establish native terminal identity; legacy failure-marker
  interpretation remains outside this calculus.
- The wrapper checks retained root-process `returncode` values. Descendant
  teardown, failed launches before a handle is returned, retry serialization,
  and restart recovery still depend on adapter/process integration tests.
- SQLite claim/lease atomicity and effect execution are tested, not proved.
  `launch_queued` is permission to attempt atomic admission, not a direct spawn.
- Browser routing supplies exact conversation/run/message ownership. Bounded
  terminal caches, delayed cursor writes, and transport delivery are not proved.
- Token budgets, summarization quality, durable children, admin lifecycle, and
  fairness/eventual progress are not proved here. Weave remains unchanged.

Next formal expansions should add explicit identity/epoch/revision ownership
and persistence/crash-recovery refinement, then liveness assumptions. Do not
broaden claims just because this model and its fixtures pass.
