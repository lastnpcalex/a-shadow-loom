# EXO system review and follow-up backlog

This is the durable return point for work intentionally deferred while the core
revived.

## Modularity

- [x] External manifest-reference registry for workspace-owned services/MCPs.
- [x] Generic admin list/register/start/stop/update UI; no per-service card.
- [x] Persist and verify PID + creation time across admin reloads.
- [ ] Provider-neutral MCP attachment contract: translate one manifest into
  Claude Code, Codex, Antigravity, and Goose native MCP configuration without
  silently widening locked-down roles.
- [ ] Extension dependency/order declarations, readiness dependencies, log
  viewer, and update rollback evidence.
- [ ] Test adoption of an already-running registered extension after a complete
  admin process replacement.

## Search Space

- [x] Explicit `excluded` / `cloud` / `sensitive` workspace policy; excluded by
  default and unrelated to ordinary conversation model selection.
- [x] External, database-namespaced local FTS index and exact source links.
- [x] Cloud-only GLM 5.3 Flash route; any sensitive scope forces llama-server.
- [x] Fail closed with user-facing errors when the required local model is down.
- [x] Locked operator surface with no filesystem/shell/general-network writes.
- [ ] Evaluate retrieval quality on a sanitized corpus of half-remembered
  projects, decisions, filenames, and outcomes; record recall@k and citation
  correctness.
- [ ] Add local embeddings/vector retrieval without requiring a runtime cloud
  embedding service; keep sensitive vectors and source text machine-local.
- [ ] Add redaction/retention controls, explicit rebuild/purge status, index size,
  and per-workspace last-indexed diagnostics.
- [ ] Decide whether Search Space should become a special conversation type while
  preserving its current tool boundary and source navigation.

## Reliability and interoperability

- [ ] Priority continuity repair: native resume within a compatible harness;
  shared checkpoint/retrieval and bounded recovery across harnesses or context
  exhaustion. Execution plan and workflow acceptance: `CONTINUITY_REPAIR_PLAN.md`.
  Do not mark complete from component tests alone.
- [x] Ignored Canvas recovery and structured-write preservation precondition;
  see `WORKSPACE_INTEGRITY.md` for tested guarantees and the adjacent failure audit.
- [ ] Close native approval-to-write races and unstructured writes to ignored
  files outside Canvas without silently replacing normal tools with read-only mode.
- [ ] Add bounded child-run snapshots and private recovery retention controls.
- [ ] Extend the formal model to integrity preconditions; hashes/read requests
  must not be mistaken for evidence that a model understood a file.
- [ ] Audit approval intake during teardown: a request still preparing before
  waiter registration can miss the one-shot scope cleanup; test delayed intake.
- [ ] Bound recovery line-diff computation on large repetitive text; the current
  `SequenceMatcher(autojunk=False)` has no execution budget.
- [ ] Replace the generic project-file route's string-prefix boundary check with
  a component-aware containment check, including sibling-prefix regression tests.

- [ ] Repeat physical Android/iPhone and ARM-laptop profiling: visual viewport,
  background/refocus/reconnect, CPU, memory, battery, and long-tree rendering.
- [ ] Periodically run the isolated lifecycle and permission preflights after
  provider CLI upgrades; cover every installed harness/model combination.
- [ ] Expand nested child cancellation/recovery tests across native and
  cross-harness children, including a parent ending while grandchildren remain.
- [ ] Track harness-native automatic approvals separately from requests bridged
  through EXO; parity applies only at the shared bridge boundary.
- [ ] Measure real send-to-draft, workspace preparation, TTFT, reconnect latency,
  child-event batching, browser heap, and background-tab CPU on representative
  large workspaces.
- [ ] Make process-global async single-flight locks event-loop agnostic or reset
  them with app lifecycle. The full suite can bind `_ALL_ENGINES_REFRESH_LOCK`
  to an earlier pytest loop even though its focused test passes independently.
- [ ] Keep a regression scenario for hidden tab → queued continuation → refocus →
  cancel/interrupt → explicit resume, validating leases, message parents,
  cursor stability, and provider process overlap.

## Stretch: reusable locked workspace profiles

hard-coding a new Loom mode. A profile should declare:

- allowed MCP/tool surface and explicit absence of filesystem/shell tools;
- workspace root and state directories;
- allowed harness/model routes and local-only constraints;
- typed actions that require user authority;
- injected role/operating instructions;
- auditable persistence and migration rules.

Profiles should live with the workspace that owns them, be registered by
reference through the extension system, and fail closed when a harness cannot
enforce the declared capability surface. This is a design goal, not current
runtime behavior.
