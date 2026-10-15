# Continuity implementation correction plan

Date: 2026-09-19
Baseline reviewed: `fbef6be`
Status: PLAN ONLY. The production repair described here is not implemented.

This plan supersedes the implementation approach in `fbef6be`. It preserves
useful checkpoint storage and retrieval work, but rejects the claim that a
one-shot call to a normal provider launcher is destination-owned incremental
compaction. `PORTABLE_CONTEXT_REPAIR_PLAN.md` remains the requirements source;
this document specifies the corrected implementation and release sequence.

## Required outcome

The release must satisfy all five behaviors as one system:

1. Compatible same harness and model: native resume immediately. No replay,
   compaction or maintenance may block the user turn.
2. Different harness and the complete rendered request fits: direct canonical
   replay. No summarization and no retrieval substitution.
3. Oversized fresh handoff: the selected destination model processes bounded,
   ordered source fragments with rolling portable state, the recent tail and
   current request remain verbatim, and the normal working turn launches.
4. Retrieval: used only after a recorded unsupported capability or exhausted,
   bounded preparation failure. Missing implementation is not a provider failure.
5. Dependencies: no mandatory llama-server, separate summarizer model, second
   API key, source-provider login or unrelated inference provider. Preparation
   uses the exact selected destination route and existing credentials.

An exceptional current request that cannot itself fit is not criterion 3. Its
exact text remains in SQLite and uses the explicit document-retrieval workflow.
The UI must disclose that exception instead of calling it ordinary compaction.

## Immediate containment before further implementation

Do not activate the `fbef6be` preparation path on the live host. Before any
other continuity change, make one focused containment checkpoint:

- Disable automatic construction of the current `DestinationSummarizer`.
- Preserve native resume, fitting replay, valid checkpoint reuse and bounded
  retrieval behavior already present.
- Remove no checkpoint rows and rewrite no conversation history.
- Add a test proving an oversized handoff cannot launch a hidden normal agent
  in `.` or any project checkout.
- Add a test proving `permission_mode="plan"` is never used for preparation.

This is a temporary safe state, not completion. It prevents hidden preparation
workers from writing `implementation_plan.md`, `.agents/loom_prompt_*.md`, or any
other file in EXO or a user workspace while the real executor is built.

## One authoritative architecture

### 1. One production reducer

Replace the dead duplicate `continuity_engine.decide()` with a pure reducer that
the production launch path actually calls. Its inputs are observations, not
provider names:

- stop/cancel state;
- compatible native-session evidence;
- complete replay admission result;
- valid portable-view admission result;
- preparation capability and phase;
- preparation result admission;
- retry budget and terminal failure evidence.

The reducer returns `resume`, `replay`, `reuse`, `prepare`, `execute`,
`retrieve`, or `stop`. `ContextEngine.prepare`, final rendered admission,
manual compaction, child recovery and every top-level/queued launch consume this
same reducer. There must be no second hand-written decision tree.

Lean generates the exhaustive decision cases. Python tests invoke the actual
runtime reducer used by the server. A separate unused Python function matching
Lean does not count as conformance.

### 2. Immutable preparation operation

Create a durable preparation record owned by the target generation:

```text
operation_id
owner_run_id
conversation_id
source_leaf_id
source_revision
destination { harness, requested_model, backend }
phase
covered record/fragment cursor
accepted rolling-state revision
retained-tail message IDs
current-user message ID
attempt/call budget
created/updated/terminal timestamps
terminal reason and verified process-exit evidence
```

The source is one immutable root-to-leaf snapshot. Append beyond a sealed prefix
is allowed; edit, deletion or fork before the cursor invalidates publication.
Cancellation revokes execution/publication authority. A preparation terminal
event is not a user-turn completion event and cannot dispatch a queue by itself.

### 3. A preparation transport, not a normal agent launch

Add a narrow provider adapter contract:

```text
capabilities(route) -> PreparationCapabilities
prepare_chunk(request) -> PreparationChunkResult
cancel_preparation(operation_id) -> VerifiedExit
```

`PreparationChunkRequest` contains only:

- operation and destination identity;
- prior accepted portable state, or an explicit empty state;
- one bounded ordered evidence fragment set;
- source record IDs and character/token offsets;
- output schema and output-token cap;
- remaining call budget.

It never contains the project task as an executable instruction. Historical
records are delimited data. The result includes effective-model attestation,
portable structured state, consumed fragment ranges, usage/admission evidence,
provider session identity when applicable, and terminal status.

The transport must not use ordinary `run_codex`, `run_gemini`, `run_goose` or
`run_claude` defaults. Those launch agents, inject the Loom contract, attach
tools/MCPs and apply user-facing modes. Provider clients may share protocol and
stream parsers, but preparation needs explicit request builders with the
following enforced properties:

- no `plan` prompt or instruction to write a file;
- no workspace MCP, subagent, shell, edit, web or image tools;
- no permission request path;
- no project checkout as cwd;
- no unselected provider configuration;
- a real output-token cap;
- a bounded timeout and cancellation handle;
- effective harness/model/provider attestation;
- verified process-tree exit before the working launch.

If a CLI requires a cwd or prompt file, use an operation-specific directory
under EXO's external machine-state root, never EXO's Git tree or the user's work
folder. Store no bearer tokens or raw private context in telemetry. Remove the
temporary prompt file after verified exit; recovery state belongs in SQLite.

Provider-specific mechanisms are implementations of this contract:

- **Codex:** app-server request with empty MCP configuration, approval `never`,
  read-only sandbox and no collaboration-plan injection. Verify the thread's
  effective model before accepting output.
- **Antigravity/Gemini:** a tool-free preparation request in the isolated cwd;
  no normal plan-mode wrapper and no project `.agents` prompt file. Treat lack
  of a verified tool-free mode as `unsupported`, not permission to improvise.
- **Goose:** ACP chat/tool-free mode, empty builtins/extensions and the selected
  Goose provider/model configuration. Verify no developer/MCP tool registry was
  advertised during initialization.
- **Claude-compatible routes:** explicit exhaustive tool denial, no project MCP
  configuration and correct routing flags for Anthropic, local llama, Umans,
  OpenRouter or Dream. A wildcard is accepted only if a native probe proves the
  installed CLI interprets it as all tools.

The adapter inventory and native probes come before claiming support. Unsupported
installed versions fail explicitly and may use retrieval; they do not silently
fall through to another route.

### 4. Deterministic bounded chunk construction

Choose the recent verbatim tail first. Preserve the current request and as many
complete recent user/assistant exchanges as admission permits, with a declared
minimum policy. Everything before that boundary is the compacted prefix.

Canonicalize the prefix into fragments carrying:

```text
message_id, role, content kind, tool-call/result identity,
fragment start/end, total length, lifecycle/effect evidence
```

Build each request against the selected destination's measured runtime capacity:

```text
fixed preparation instructions
+ prior portable state
+ next fragment set
+ schema overhead
+ requested output
+ safety margin
<= destination input capacity
```

Split a giant record at safe text boundaries while preserving its record ID and
exact offsets. Never split a tool-call/result identity away from its owning
metadata. Never execute historical calls.

Each accepted result replaces the prior portable state and advances the cursor
over exactly the reported contiguous fragment ranges. The next request contains
only the accepted state plus new evidence, not all earlier raw evidence. Use a
fresh bounded preparation context for each call unless a provider exposes a
verified native compaction operation that produces the same portable state.
Merely continuing a growing native transcript is not bounded compaction.

The portable-state schema retains:

- current objective and explicit constraints;
- later corrections/superseded instructions;
- decisions and rationale;
- completed effects with DB-backed receipts;
- uncertain effects requiring reconciliation;
- pending work and durable child IDs;
- artifact paths/references;
- unanswered questions;
- source message IDs for material claims.

Deterministic DB evidence—not the model—supplies completed/uncertain effect and
child ownership fields. Validate JSON/schema, route attestation, coverage,
state-size cap and final admission before accepting a result. Semantic fidelity
is a native behavioral test requirement, not something schema validation proves.

### 5. Bounded failure and retry rules

Every fragment has a finite validation retry budget. A retry must change a
measured constraint: smaller evidence chunk, smaller requested output, or a
schema-repair request. Identical retries are forbidden.

Preparation fails terminally only after one of:

- installed destination explicitly lacks a required tool-free transport;
- timeout/process failure exhausts the declared retry budget;
- repeated invalid/empty/wrong-route/oversized output exhausts its budget;
- no positive evidence allowance remains after required overhead;
- cancellation or source invalidation occurs.

Cancellation stops; it never converts into retrieval. Other terminal preparation
failures may select retrieval with a precise recorded reason. Do not catch all
`Exception` values and relabel programming errors, permission mistakes, DB locks
or teardown failures as ordinary model failure.

Provider teardown errors are fatal to the attempt. Retain ownership and refuse
the working launch until the process and descendants are confirmed exited.

### 6. Atomic publication and normal execution

After all compacted-prefix fragments are covered:

1. Render `portable state + recent verbatim tail + current request`.
2. Apply the normal selected destination's complete wire admission, including
   actual instructions, tools for the working turn, media and output headroom.
3. If it does not fit, return through the reducer with measured evidence. Do not
   discard more recent records implicitly.
4. In one DB transaction, verify the covered source prefix/revision and publish
   the checkpoint plus coverage metadata.
5. Launch the ordinary working agent through its normal provider path.
6. Persist the resulting native session ID and effective model attestation.
7. Send a small same-harness follow-up and prove it resumes that session without
   replay or another preparation operation.

Preparation sessions are not automatically promoted to working sessions. They
may be promoted only when the adapter proves tools/configuration can change
safely and the resulting session meets normal launch/admission requirements.

Automatic portable checkpoints are route-agnostic only after their schema and
exact covered prefix are validated. Select the newest *best-fitting/longest*
valid prefix, not simply the latest timestamp. Raw SQLite messages remain the
canonical source and are never deleted by compaction.

## Lean correction plan

Extend `formal/portable_context/Model.lean` into a stateful transition model with:

- owner, source revision and destination identity;
- retained-tail and current-message protection;
- record/fragment cursor and accepted state revision;
- remaining validation/call budget;
- preparing, cancelling, exited, published and executing phases;
- exact terminal failure category;
- queue permission and working-session identity.

Prove over reachable traces:

1. Native compatibility always bypasses replay/preparation.
2. Fitting replay always precedes preparation.
3. Coverage advances contiguously, never on rejected output and never across a
   sibling or changed prefix.
4. Protected tail/current records are never incorporated into lossy state.
5. Execution requires complete prefix coverage, final admission, unchanged
   revision and verified preparation teardown.
6. Retrieval requires `failed` or `unsupported`; `untried` cannot retrieve.
7. Cancellation cannot publish, execute, retrieve or dispatch queued work.
8. Retry and call budgets strictly decrease, giving finite termination.
9. Duplicate events cannot publish or launch twice.
10. No transition changes the selected destination route.

Generate decision and transition traces from Lean. Test those traces against the
same pure Python reducer imported by production. Add mutation tests that delete
each runtime guard and prove the conformance suite fails. Lean does not prove
model fidelity, tokenizer truth or process isolation; those remain integration
and native acceptance obligations.

## Test rewrite and acceptance matrix

### Layer A: pure reducer and chunker

- Actual production reducer matches every Lean decision/transition fixture.
- Chunk property tests cover empty histories, hundreds of messages, Unicode,
  image references, tool blocks and individual records larger than capacity.
- Coverage union equals the exact compacted prefix: no gaps, overlaps, repeats
  or tail/current inclusion.
- Every constructed request and requested output fit its measured capacity.
- Retry, cancellation and source-edit traces satisfy the formal invariants.

### Layer B: provider request construction

Use protocol spies at the final process/RPC boundary—not a fake summarizer—to
inspect requests for each supported route:

- selected effective model and backend;
- zero tool/MCP registry;
- isolated external-state cwd;
- no plan/write-file instruction;
- enforced output cap and timeout;
- no call to local or another provider;
- correct cancellation and verified descendant exit.

Deliberately request a file write/tool call in the preparation fixture and prove
the transport cannot execute it or raise a user approval prompt.

### Layer C: production-path deterministic integration

Run the real serial generation entry, provider boundary, database, reducer,
chunker, checkpoint publication, final admission and event router. Fake only the
remote/native model response at the protocol boundary.

For all 12 directed pairs among Claude, Codex, Gemini and Goose:

- create history requiring at least three accepted chunks;
- record the exact selected destination on every call;
- validate rolling-state linkage and contiguous fragment coverage;
- preserve recent records and current request byte-for-byte;
- complete the ordinary working turn;
- resume the resulting destination session on the next turn;
- observe zero unselected/local inference calls;
- observe no workspace writes or permission requests.

An adapter-entry followed by injected failure and retrieval is a fallback test,
not a successful oversized-handoff test. Synthetic trace-oracle fixtures test
the oracle only and are never counted as provider acceptance.

### Layer D: adversarial lifecycle integration

- Edit/fork/append during chunks and immediately before publication.
- Cancel/interrupt/disconnect during each phase; verify process exit and queue
  remains paused until explicit resume.
- Invalid JSON, empty state, wrong model, excessive output, partial stream,
  timeout, crash, stale duplicate terminal event and DB lock.
- Two tabs and parallel roots; exact run/thread/window attachment throughout.
- Completed and uncertain tool effects; prove no duplicated mutation.
- Maintenance and user turn overlap; native resume remains immediate.
- Final renderer grows after preparation; reducer replans without losing tail.

### Layer E: real native acceptance

Using synthetic non-sensitive evidence and the installed authenticated harnesses,
run every supported directed pair. Histories must exceed the destination's
measured capacity and require multiple calls. Record:

- source digest and harness versions;
- requested/effective model and provider;
- runtime context provenance;
- preparation call counts, sizes, offsets and timings;
- teardown evidence;
- final payload admission;
- task artifact assertions and counted side effects;
- next native-resume session identity;
- any unavailable configuration as unverified, never passed.

Include semantic traps: an early instruction later corrected, a completed effect
that must not repeat, an uncertain effect requiring reconciliation, artifact
references and a final request of “continue.” Correct behavior—not keyword
recall—is the assertion.

Run cloud handoffs with llama-server unreachable. Run local destinations only
when explicitly selected. No production server restart or live conversation DB
is part of acceptance; use isolated listeners, DBs and owned provider processes.

## Implementation sequence and gates

This order is architectural, not provider-first. No provider-specific slice is
called a continuity repair before the common gates pass.

1. **Containment:** disable the unsafe `fbef6be` executor; add negative tests.
2. **Reducer:** production consumes one Lean-conformant state/reducer path.
3. **Operation/chunker:** durable operation, exact fragments, rolling state,
   budgets, cancellation and transactional publication.
4. **Transport contract:** implement all supported destination transports behind
   the same interface; verify tool isolation and route identity individually.
5. **Production binding:** wire top-level, queued, recovery, manual and durable
   child launches to the same operation; pass workspace identity but never use
   the project checkout as preparation cwd.
6. **Deterministic matrix:** all 12 pairs, lifecycle races and mutation tests.
7. **Native matrix:** real successful multi-chunk handoff, normal turn and native
   follow-up for every available route; record unavailable routes honestly.
8. **Atomic cutover:** remove the containment fallback only after gates 1–7;
   inspect the full diff and run repository regression before human activation.

Each checkpoint must state which gates are still red. “Tests pass” without the
layer and behavioral claim is not a completion statement.

## Expected production file boundaries

- `continuity_engine.py`: pure reducer integration and plan construction only;
  no provider imports or subprocesses.
- `context_chunking.py` (new): deterministic records/fragments, budgets and
  rolling-state request construction.
- `context_preparation.py` (new): operation coordinator, finite retries,
  cancellation, telemetry and transport interface.
- `context_checkpoints.py`: atomic source validation, fragment/progress and
  published checkpoint persistence.
- Provider clients: explicit tool-free preparation request builders using their
  native protocol, separate from ordinary agent launch defaults.
- `context_runtime.py`: bind snapshot, reducer, coordinator, final admission and
  working launch; no direct normal-agent call disguised as a summarizer.
- `server.py`: one call into runtime with exact workspace/run/thread identity;
  no provider-specific continuity policy.
- `formal/portable_context/`: transition calculus and generated fixtures.
- `tests/`: separate unit, request-construction, deterministic production-path,
  lifecycle and opt-in native acceptance suites.

Names may change after inspecting protocol boundaries, but responsibilities may
not be collapsed back into provider-specific server handlers.

## Definition of done

The repair is complete only when one source snapshot satisfies all rows:

| Gate | Required evidence |
| --- | --- |
| Native resume | Real compatible follow-up, same native lineage, zero replay/preparation |
| Fitting replay | Complete rendered branch sent verbatim, zero preparation |
| Oversized handoff | Multiple accepted selected-destination chunks, exact coverage, retained verbatim tail/current request, normal working completion and next native resume |
| Retrieval fallback | Recorded unsupported/exhausted preparation cause; cancellation never retrieves |
| Dependency isolation | No local/unselected calls, credentials or tools; effective model attested; isolated cwd and verified teardown |
| Formal connection | Production reducer—not a duplicate—passes generated Lean cases and transition mutants |
| Lifecycle | Queue/tab/branch/child ownership preserved through cancel, reconnect, edit and fork |
| Regression | Focused, broader and repository suites reported with source digest and honest unrelated failures |

Passing adapter-entry tests, producing a summary once, or falling back safely are
not completion. No activation/restart instruction is appropriate until this table
has evidence and a reviewed implementation checkpoint exists.
