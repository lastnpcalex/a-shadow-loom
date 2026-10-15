# Isolated lifecycle and responsiveness preflight

Run alongside the [permission preflight](PERMISSION_POLICY.md#pre-deployment-acceptance):

```powershell
C:\Python314\python.exe tools/lifecycle_preflight.py
# Publish only after every mandatory stage passes without source drift:
C:\Python314\python.exe tools/lifecycle_preflight.py --publish
# Optional authenticated Codex usage in disposable workspaces:
C:\Python314\python.exe tools/lifecycle_preflight.py --native-codex
# Both harnesses, three independent turn-pair cycles each (paid usage):
C:\Python314\python.exe tools/lifecycle_preflight.py --native-codex --native-goose --native-repeats 3
# Real parent/child approvals and isolated browser resource A/B measurements:
C:\Python314\python.exe tools/lifecycle_preflight.py --native-children --browser-resources
# Models themselves spawn and collect a nested cross-harness chain (paid usage):
C:\Python314\python.exe tools/lifecycle_preflight.py --native-delegation
```

Requires the normal test dependencies and Playwright Chromium. Each stage runs
in a separate pytest process with fresh SQLite databases and ephemeral local
listeners. Fixtures stop only listeners/providers they own. No production host
excluded. Direct Anthropic subscription is not required.

Artifacts default to a new OS-temp directory. `--output-dir` requires a new
directory: prior evidence, including failures, is never overwritten.
`report.json` contains numeric-only observations, test counts, Git revision and
a live-source digest. Raw logs/XML stay in that directory; inspect before sharing.
Native session artifacts and raw reports do not belong in source commits.

Native repetitions are bounded to 1–10. Every repetition runs in a new pytest
process with its own database, workspace and report; failures stop the run and
are never replaced by a passing retry. Acceptance requires **all** planned stages.
Codex uses `gpt-5.6-luna`; Goose uses existing OpenRouter credentials and
`goose:openrouter:z-ai/glm-5.3-flash` (override with
`LOOM_LIFECYCLE_GOOSE_MODEL`). Goose's state directory is disposable; the fixture
omits optional MCP servers, retains its native developer tools and does not
edit installed profiles. Missing credentials/CLI or skipped tests fail acceptance.

## Coverage

Browser acceptance also exercises touch-mobile rerun through admission, partial
output, focus return, reload while running and durable landing. A lost launch
acknowledgement must recover without replaying the provider command. Submitted
prompt URLs restore a unique saved reply, and a failure before provider launch
must remain visible after reload. Tests use the rendered workspace controls.
The launch-state Lean proofs and generated JavaScript oracle cover state
transitions; they do not certify rendered pixels, authenticated tunnel delivery,
provider availability or every possible CSS change. Those require browser and
deployment evidence, and an unverified deployment is not a passing acceptance.

The [coordination calculus](formal/coordination/README.md) now supplies the
completion/teardown/queue and landed-view decision rules. Core acceptance
includes its generated Python/Node conformance, negative-control reducers,
runtime lease-finalizer tests, and the local context-guard suite. The Windows
`Coordination proofs and lifecycle acceptance` workflow runs the default core
and browser stages after building Lean and comparing its generated fixture.
Skips fail acceptance; paid native tests remain opt-in. GitHub branch-protection
requirements are not configured by this workflow.

Edited-follow-up acceptance reads the exact page's persisted cursor, not legacy
conversation-wide `is_active` flags. It deliberately changes that legacy
selection and still requires correct message parents, native session continuity
and another successful send on the edited branch. Failed browser assertions
drain their fixture-owned generation tasks before closing the temporary database.

| Stage | Evidence |
| --- | --- |
| Core | Atomic database leases, duplicate sends, durable queue ancestry, session/handoff selection, nested scheduling, permission ownership, batching, recovery/cache correctness |
| Browser | Real SPA/HTTP/WebSockets: queue order, background/refocus/reconnect, delayed teardown, paused queue/explicit Resume, interrupt versus cancel, tree-root sends, editing/pinning, positive parallel control, child approvals/replay, model handoffs |
| Native Codex (opt-in) | Interrupt → paused queue → native fork; cancel → paused queue → rebuild. PID/create-time monitoring follows fixture-owned descendants independently of Loom's registry |

Handoffs use production routing/adapters with controlled provider streams:
Codex → Codex → Goose → Codex. Checks inspect launch payloads, native session
selection, message parents and browser thread identity. This is **not** a live
cross-provider inference test.

The default child scenario uses the production supervisor and approval bridge with a
controlled worker. Parent interrupt/cancel cannot resolve its pending approval;
reconnect restores that request to the child; browser approval and its note
reach that child without moving the chat cursor.

The opt-in native Goose stage uses the same browser/scheduler/process-monitoring
implementation as Codex: real partial output, queue, hidden/refocus/reconnect,
stop, paused queue across scheduler ticks and explicit Resume. Interrupt must
retain partial context and pass its exact session ID into a native fork; Cancel
must discard it and rebuild. Assertions check durable parent IDs, one successor,
browser completion, continuity badges and zero remaining fixture-owned processes.
Goose's configured-model badge is not misrepresented as runtime verification.
These are top-level provider cycles, **not** native durable-child or cross-harness
inference tests. Tool-result retention still has separate controlled coverage;
the native cycle requires partial output, not a completed tool execution.

`--native-children` adds four real-provider cases: Codex and Goose, each with
parent Interrupt and Cancel. A real parent and child both wait for distinct
approvals. Stopping the parent must retire only its approval (source
`loom_lifecycle`), reap only its process tree, and leave the child waiting.
After reconnect, the Agents panel must expose that exact child request. An
allowed synthetic command must produce persisted tool output; a subsequent
denied fixture write must not create its file. Both decisions and notes are
checked in SQLite. Completion must retain the child's native session identity,
leave the chat cursor/tree unchanged and leave zero fixture-owned processes.
Decisions inspect literal command arguments, never arrival order. Goose may
insert native TODO writes: at most four bounded, metadata-identified fixture
plans are allowed through the same browser UI and counted separately. Unexpected
actions fail the test without an approval; planning cannot consume either of
the two required command decisions.

The child uses the same harness/model as its parent. Admission goes through the
real API used by agent-control MCP, **not a model-chosen MCP spawn call**. This
does not prove nested delegation, cross-harness child inheritance, or delivery
of denial notes into every provider's current model context. Optional Loom MCP
servers are omitted; installed Codex configuration can still start its own MCP
servers. HTTPS uses existing certificates read-only when present; otherwise the
fixture uses HTTP. Native Goose requires existing OpenRouter credentials. No
installed profile, certificate or production database is modified.

`--native-delegation` exercises Codex -> Goose -> Codex and the reverse using
real model-issued `loom-agent-control` MCP calls. The test sends only the root
user task; it does not create children through the API. Each coordinator must
spawn once, wait, then get its child's result. A random fixture value is absent
from both task payloads and can be learned only by the leaf's file read; it must
return through both ancestors. Coordinators may use MCP and bounded native TODO
planning, not read the file themselves. The leaf's write is denied through the
browser's actual permission handler and must leave no file behind.

The scheduler has one independent leaf slot. The two durable descendants share
an overlapping scope, so completion tests the former ancestor/wait deadlock.
Assertions check exact parent IDs, page ownership, models, native sessions,
unchanged chat tree/cursor, reconnect during the leaf's pending approval, and
zero surviving test-owned provider trees. Three live trees during that approval
are intentional: two coordinating ancestors and one leaf, not three independent
conversation turns. This bounded scenario does not prove arbitrary agent
planning quality, all model combinations, or nested cancellation recovery.

The MCP test also guards Goose's top-level generation/page identity. The MCP
caller falls back to `LOOM_PARENT_RUN_ID` only when no current child ID exists;
nested callers always retain their immediate identity. Goose inherits its page
and effective permission scope from its generation task. These backend fixes
require a human-controlled reload before affecting the running host.

This test exposed two Codex adapter gaps: children omitted the direct Loom
permission handler used by parents, and v2 command results lost camel-case
`aggregatedOutput`/`exitCode` fields. Children now use the direct handler; command
output, failed exits and declined tools are normalized without making a declined
tool a failed whole turn. These backend corrections need a human-controlled
host reload to affect live traffic.

Missing/duplicate observations, failures, skips, malformed reports, nonzero
pytest exits and changed source fail acceptance. Paid tests do not start after
a failed earlier stage. Native permission checks for Goose, Codex,
Claude-Code/OpenRouter and Antigravity remain in the separate permission runner.

Portable continuity is a mandatory default stage, not an advisory pytest list.
A passing report may be published to external EXO state with `--publish`; a
failed, incomplete, skipped, or source-drifted run cannot replace the last
passing report. Admin compares that report with both the checkout and Main's
startup build identity. Only an exact three-way match is `VERIFIED`.

## Measurement definitions and limits

- **Send to shell:** browser action to provisional assistant DOM insertion, not
  database draft creation.
- **Send to stream start/first output:** browser receipt of the real WebSocket
  event. Native queued-turn timing starts at explicit Resume.
- **Preparation:** server-reported recovery snapshot duration when available.
- **Native TTFT:** separately persisted server timing, excluding the snapshot.
- **Reconnect:** socket close through reconciliation and state-preservation
  checks, not just TCP connect.
- **Child burst:** 800 deltas across four children; batch/paint counts and time;
  preservation of editors, expanded tools, caret and scroll; a background
  interval with no child repaint.
- **Browser cost:** Chromium renderer `TaskDuration` divided by elapsed time,
  layout time, JS heap and DOM nodes. Not whole-machine CPU, GPU time or total
  browser RSS. Headless visibility emulation is not OS tab throttling.
- **Recovery:** cold/warm 32 MiB synthetic workspace, elapsed time and actual
  bytes read. Warm reuse must read zero unchanged content bytes.

Timings are observations, not hardware-independent latency gates. Repeated
runs with the same source/workload/hardware establish a baseline. One run does
not establish p95 latency, memory leaks, physical-phone or WAN performance.
OS process sampling waits 50 ms between queries, plus OS query latency; database tests provide separate
atomicity evidence. A controlled-provider counter is not OS-process evidence.
The browser observation also records whether the WebGL raytracer canvas was
present: costs with a graphics fallback must not be presented as full-raytracer
performance. Native acceptance verifies no fixture-owned provider remains alive.

`--browser-resources` measures three alternating raytracer-on/off idle pairs
using the existing toggle in a disposable Chromium profile. Every sample
records task time, heap, DOM nodes and layout cost; a missing WebGL canvas fails
the case rather than silently measuring the fallback. A separate emulated
visibility signal measures application behavior, **not actual OS background-tab
throttling**. No animation, theme or production preference is changed. These
short samples do not establish GPU cost, steady-state leaks or physical-device
performance; those still require a real phone/laptop and longer repeated runs.

The delayed-discovery regression holds the optional local-model response open
until remote chat is usable, then changes selection before releasing it. The
picker renders registry entries immediately and accepts only the newest async
refresh, preserving selections, unsent text and the selected branch. This is
a frontend-only dependency repair; no animations or Weave behavior are removed.

The fast-completion regression holds a send's HTTP response until its real
WebSocket terminal event has been applied, in both chat and tree views. A late
acknowledgement must not revive loading, reverse user/assistant order or move
the cursor back from the completed response. Exact-run terminal evidence wins.

Queue-control checks also force an unchanged-chain reload while streaming.
Background synchronization must not detach the existing controls, focus or
editor merely because a streaming flag is set; missing stream recovery is
separate from repainting the surrounding message list.

Reconnect admission selects the newest connected socket for the exact page
identity, ignoring closed/superseded sockets still awaiting cleanup. Queue and
child launches must not attach their run subscription to a stale connection.
This server-side repair requires a human-controlled host reload to become active.

The continuation regression now includes a successful source/log read containing
error phrases, a failed tool followed by confirmed native success, and the next
user turn on a branch too large to replay. Tool text must not poison the whole
turn. Browser acceptance sends a follow-up and an edited sibling, checks the
exact source session and parent IDs, and requires both to land without refresh.
It also covers a previously misclassified source recovered from native evidence.
The branch response is deliberately captured before completion and delivered
afterward: it must not erase landed content/telemetry or revive its spinner.
A subsequent send after the edited reply checks that the cursor remains usable.
Merging is exact-message scoped; only that edit's launch IDs may add its child,
never an unrelated sibling or another conversation's stream.

Legacy Codex recovery checks only the nearest failed assistant with verified
session ownership and retained terminal evidence. It reads at most a bounded
tail of that exact local UUIDv7 session journal; it launches no provider and
does not rewrite the historical message. A newer start/abort, a native error,
missing final answer, stale timestamps, or ambiguous/missing journal blocks
recovery. `task_complete` alone is not proof of success. Unsupported native
journal layouts retain the existing rebuild policy. Normal resumes do not
perform this check. Lean's permission proofs do not prove these lifecycle rules.

## Roadmap status

The six original areas have implementations: child ownership/teardown, nested
delegation/capabilities, recovery/context budgets, shared permissions,
incremental snapshots/event efficiency, and consolidated model registration.
This runner fills integration/measurement gaps; it does not certify a bug-free
application or deploy code. Repeated native Codex/Goose top-level lifecycle
acceptance and same-harness durable-child approval lifecycle acceptance are
available. Bounded model-driven nested Codex/Goose delegation has its own opt-in
stage. Arbitrary cross-provider task quality, nested cancellation, and physical-
device measurements remain distinct coverage extensions. The headless browser
connection is not a physical phone; connect a real device for those measurements.
