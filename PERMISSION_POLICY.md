# Loom Permission Policy

Hermes, Dream Space and Prometheus are retired; their historical permission audit labels remain readable. No active acceptance test requires Hermes.

## Goal

Make permission behavior nearly invariant across Codex, Claude Code,
Antigravity, Goose, and future harnesses without asking a fuzzy command
classifier to guess user intent.

Loom should normalize the approval request the harness chose to surface, apply
one small structural policy, present one consistent UI, and report the real
resolution source. It must never describe a policy block, lifecycle
cancellation, timeout, or bridge failure as a user rejection.

## Canonical action

Provider adapters normalize requests into:

- Effect: read, workspace write, execute, network, external mutation,
  sensitive read, lifecycle, or unknown.
- Boundary: inside this workspace, outside it, external service, or unknown.
- Validity, warning annotations, and separate recognized-destructive-operation evidence.
- An already-validated remembered-rule match.

Harness and tool names are audit provenance, not policy inputs. Raw commands,
arguments, feedback, and workspace paths are not written to the policy audit.

## Profiles

| Action | Strict | Balanced | Trusted |
|---|---|---|---|
| Read inside workspace | Allow | Allow | Allow |
| Write inside workspace | Ask | Allow | Allow |
| Execute inside workspace | Ask | Ask | Allow |
| External, sensitive, lifecycle, or unknown | Ask | Ask | Ask |
| Valid remembered rule | Allow | Allow | Allow |

Warnings can decorate an ask; they cannot create a denial or revoke an
automatic allow. Invalid requests fail structurally. Only an explicit human
resolution may be recorded as `user_denied`.

### Opt-in: Trust — destructive review only

The separate `trust_review` profile auto-allows all valid bridged requests except
recognized destructive operations. This includes unknown commands, outside-workspace
access, network requests and sensitive reads. It intentionally grants more access
than Trusted. New conversations default to Trusted; saved conversations keep their
existing profile, and forks inherit it. Legacy rows without a profile retain the
Balanced fallback. The original three profiles' decision rules are unchanged.

Pending approvals produce a global bell notice in every profile, including
Trusted. That notice carries request and conversation identity only. The tool
payload and approval authority remain on the owning page or explicit Admin
supervision. Automatically allowed tools do not become pending approvals.

Recognized deletion/clearing tools, native file deletions, delete-file patches,
literal delete/format commands, forced copy/move, Robocopy mirror/purge, and explicit
Git discard/deletion/force-push operations require one-time approval. Existing branch
grants cannot override this review, and these cards offer no Allow for Branch.
Recursive scans and quoted command descriptions alone are not destructive evidence.
The same pending request, user notes, cancellation, and admin approval flow is reused.

Detection is bounded structural recognition, **not a safety guarantee**. Arbitrary
scripts, aliases, encoded programs, unrecognized tools or destructive effects inside
ordinary edits can pass automatically. Only surfaced requests are covered; role/run
ownership restrictions and harness-native safeguards remain separate. Selecting this
mode is deliberate trust in the agent, with a review opportunity for known operations.

## Remembered rules

### File-preservation precondition

Recognized in-workspace structured mutations additionally pass
`workspace_integrity.py` before any automatic allow. Existing-file replacements,
recognized deletions and large replacements require one-time review in every
profile; branch grants cannot bypass it. Preimages include ignored files and live
bytes are rechecked after an approval wait. This is separate from the Lean-proven
profile table, not a change to its decision fixtures. Audits expose the profile
decision and `effective_decision` separately. See `WORKSPACE_INTEGRITY.md` for
coverage, resource limits and native/shell gaps.

“Allow for Branch” creates a structured rule derived by Loom, not a
provider-supplied policy amendment. A rule applies only when
all three conditions hold:

1. The workspace identity matches.
2. The requesting branch descends from the branch that granted it.
3. The normalized command/action shape matches.

Rules are session-local (lost on server restart), with a preview on inline,
notification and child approval cards and individual revocation in Settings.
The branch anchor is the requesting assistant draft, or its exact parent before
a draft exists. Children use their persisted parent-message ancestry and actual
workspace. Neither a sibling nor another conversation inherits the grant.

Workspace read/write rules cover the named effect on paths inside that workspace.
Simple command rules freeze the executable/entry point, argument count, options,
non-path values, environment and working directory. Only explicit workspace-file
operands of known pytest and `git add/diff/status -- <paths>` forms may vary.
Single literal PowerShell `-Command` invocations are supported too: the executable,
wrapper flags and working directory remain fixed. Explicit `-LiteralPath` operands
of Get-Content/Get-Item/Get-ChildItem/Test-Path/Get-FileHash/Set-Content/Add-Content
may vary only within the workspace. Copy/move destinations and other arguments
stay literal. Pipelines, substitutions, comments and encoded programs do not
acquire a remembered template. The card explains why a rule is unavailable.
Generic script arguments, branch names and URLs stay exact; they are not guessed
to be paths. For example, `python -m pytest tests/a.py -q` can cover
`python -m pytest tests/b.py -q`, not another module or changed flags. The preview
shows `<workspace-file>` slots. Shell programs, expansions, compound commands,
unknown operations and unverified destinations offer one-time approval only.
This is an execution capability, not a proof of a program's side effects.

Rules contain inspectable token templates in memory, not raw shell programs or
provider-global allow lists. No template/argument/path is copied into policy audits.
Approvals returned to providers are always one-time (or Codex turn-scoped for its
permission-expansion request); Loom rechecks scope on each bridged request.

## Admin supervision

The admin Activity tab is an explicit supervisor approval surface across configured instances. It resolves the same live waiter as chat, preserves denial feedback and eligible branch grants, and uses first-response-wins semantics. Existing chat origin/run checks remain intact; admin authority is separate and privileged. Instance-scoped proxies never search another server for a matching request ID. The internal endpoints accept only loopback relays, and browser mutations require a same-origin admin request. Keep admin access restricted to trusted operators.

Admin cancellation uses the exact instance and waits through the provider's
bounded teardown. Activity keeps the action pending through polling and reports
completion only after that instance confirms the run is no longer active.
Timeout/offline responses are uncertain, not cancellation failures to replay.
The old instance-less `/api/generations-proxy/{draft_msg_id}/kill` endpoint returns
410; use Activity or the instance-scoped supervision endpoint instead.

## Formal boundary

`formal/permission_policy/` is the authoritative finite decision algebra. Lean
proves:

- Only a user-sourced resolution can become `user_denied`.
- A branch rule requires every scope constraint.
- Strict is never more permissive than Balanced; Balanced is never more
  permissive than Trusted.
- A warning never revokes an automatic allow.
- Equal normalized actions receive equal decisions regardless of harness.
- Trust/review asks for destructive evidence even with a matching branch rule;
  all its other valid actions allow.
- The original three profiles ignore the new destructive-evidence bit.

Lean generates all 2,048 finite decision cases. Python must match every case in
CI. Lean is development/CI tooling and is not a Loom runtime dependency.

Destructive-operation recognition, normalization, path containment, and provider payload parsing sit outside the
proof boundary, so each provider adapter also needs conformance and fuzz tests.

### Native payload normalization

Goose 1.45 supplies its real tool identity in the initial ACP tool-call metadata,
but its later permission request typically says `kind: other`. `acp_permissions.py`
joins these events by tool-call ID within the same native run and reads `rawInput`.
It never authorizes based on a display title. Changed arguments, unfamiliar tools,
missing evidence and outside-workspace paths remain asks in the original profiles. The typed
ACP decoder keeps native names/categories distinct from presentation titles.

Codex array-form commands retain their argument boundaries. Approval audits identify
`codex_app_server`; raw approval parameters and feedback are not logged. Routine
automatic approvals do not inject extra steering text. Explicit user notes and
denial explanations still reach the existing feedback path. Goose ACP has
no portable free-text decision field: notes remain persisted and are supplied on
the next Loom continuation, not guaranteed as same-turn native tool feedback.
Goose 1.45 developer tools include tree, write, edit and shell, but no plain
file-content read tool. The shared normalizer recognizes one explicit literal
read form: `powershell.exe -NoProfile -Command "Get-Content -LiteralPath file.txt"`
(also `powershell`, `pwsh`, `pwsh.exe`). Optional `-Raw`, `-TotalCount N`, or
`-Tail N` is allowed, with positive counts bounded at 10,000. Every path and
supplied working directory must still be inside the workspace. Providers,
expansion, pipelines, wildcard paths, additional statements, and other shell
forms remain execute/unknown requests; this is not a general shell classifier.
All harnesses share this normalization. Claude Code's native Windows
`PowerShell` tool is recognized as execution, with the same profile and scoped
branch-rule behavior as `Bash`. Unmapped extensions still ask in the original profiles.

Antigravity hooks use named entries with event arrays, second-based timeouts,
and camel-case `toolCall` input with top-level `decision`/`reason` output, per
the [native hook contract](https://antigravity.google/docs/hooks). Loom merges
its named hook without replacing unrelated project hooks. Launches select the
workspace's project (or create one for a fresh workspace) so hooks are discovered.
On Windows, a fixed encoded hook entry point avoids agy's `cmd` quoting bug;
model-supplied tool arguments are never interpolated into it and travel as JSON
on stdin. Legacy Claude/Gemini-style hook envelopes remain supported.

`tests/test_permission_payloads.py` covers sanitized native wire shapes and Windows
templates; `tests/test_live_permission_acceptance.py` provides opt-in native-provider
tests with disposable state and real Chromium approval controls. Neither starts,
restarts or connects to the active Loom server. Native-model tests use existing
provider authentication and are intentionally not part of ordinary offline CI.
The transport acceptance suite runs real Claude/Antigravity hook subprocesses
against an ephemeral approval-only listener. It verifies
restored browser cards, remembered grants, child scope and denial notes without
requiring a live model or touching the active host.

`tests/test_native_harness_permissions.py` extends opt-in model coverage to
Claude Code (direct or OpenRouter) and Antigravity. Antigravity requires
the separate `LOOM_NATIVE_AGY_TRUST_TEST_HOOKS=1` opt-in because its launch must
register the Loom hooks for the disposable project in its native trust store.
No provider-wide tool grant is installed. A successful echo without an observed
approval is not accepted as evidence of permission parity. Expired provider
credentials and native adapter failures are reported, not counted as passes.
Direct Anthropic testing additionally requires `LOOM_RUN_NATIVE_CLAUDE_SUBSCRIPTION_TEST=1`;
it is not required for Claude Code through OpenRouter.

For runtime friction, `tools/permission_friction_audit.py --db <database> --hours 24`
opens SQLite read-only and reports counts by harness/profile/normalized reason.
It deliberately excludes raw tool titles, commands, paths and feedback, including
legacy audit rows. Compare results with the running host's code version: source
edits cannot change policy inside an already-loaded Python process.

## Pre-deployment acceptance

Run the offline workflow, policy, hook-transport and browser checks without a
live database, model credentials or production host:

```powershell
C:\Python314\python.exe tools/permission_preflight.py
```

To additionally exercise the four supported authenticated native lanes:

```powershell
C:\Python314\python.exe tools/permission_preflight.py --native --trust-agy-test-hooks
```

Native checks incur model usage and use existing provider authentication.
`--native goose codex` selects a subset. Antigravity's separate flag authorizes
registration of Loom hooks for its disposable project in the native trust
store, not a tool-wide grant. Direct Anthropic subscriptions are excluded.
No command executes real grading, publishing, migration or file-move work.

Each invocation retains a NEW OS-temp artifact directory with a count-only
`report.json`, detailed local pytest XML/logs and disposable databases/workspaces.
`--output-dir <new-directory>` is optional; existing directories are refused.
Do not commit the detailed artifacts. The summary excludes tool arguments,
paths, notes, model output and pytest failure text. It records the Git baseline
and a digest of the tested source inputs; changes during the run invalidate
acceptance. It never starts, stops or contacts a live Loom host.

The deterministic suite measures 19 scenarios for each of four adapter shapes:
normal reads/writes/edits, initial command approval and restored cards, a queued
descendant, child inheritance, changed command, sibling isolation, outside
reads/writes, unknown tools, foreign-workspace children, cancelled waiters,
revocation, new-session grant expiry, Trusted commands/unknowns, Strict reads/writes.
Each records expected and observed prompt, unknown-action and rule-match counts.
Missing/duplicate observations and mismatches fail acceptance. Unknown requests
are deliberately expected in their negative-control cases, never silently waived.

Additional Trust/review tests exercise the same four bridge normalizers: ordinary,
unknown and outside requests auto-allow, recognized destruction asks on every
request, old branch grants cannot override review, notes are retained, and cancelled
or missing child owners remain denied by lifecycle validation. All destructive
test commands are inert payloads, never executed. The browser suite checks the
new option against the existing themed control at phone and desktop widths.

The native layer measures actual requests and browser prompts, not merely a
successful echo. Goose covers five workspace actions, a native-fork remembered
command and an outside-workspace denial with a note. Codex, Claude/OpenRouter and
Antigravity cover initial command approval and native-fork grant reuse. Failed
or skipped requested native tests cannot produce a passing full report.

These layers have distinct limits: simulated queue ownership does not execute
the scheduler, and restored-card transport tests are not all full-SPA tests.
The included real admin HTTP/browser case separately exercises refresh, exact
instance ownership, queued-message retention and delayed provider cancellation.
Native tests are representative samples, not proof against every possible model
tool choice. Counts apply to requests surfaced to Loom, not identical prompt
counts across native harnesses; Codex's [approval protocol](https://learn.chatgpt.com/docs/app-server#approvals)
depends on its native settings too. Weave remains unchanged.

## Rollout

1. **Shadow:** keep existing outcomes, persist sanitized legacy-versus-policy
   comparisons, and measure disagreements by harness/effect/boundary.
2. **Review:** inspect false asks, false blocks, unknown normalizations, and
   provider gaps from real use. Expand adapter fixtures without changing the
   finite policy casually.
3. **Preview:** show the normalized action and proposed policy result in the
   permission UI; add user-authored denial notes and structured similar-action
   rules.
4. **Enforce:** select a profile per conversation/workspace and retire fuzzy
   automatic command blocking. Keep the old result available in audit during a
   bounded rollback window.
5. **Simplify:** remove provider-specific policy branches after every harness
   passes the same fixtures and live acceptance scenarios.

The implementation now enforces the policy at `/api/cc-permission`. A persisted
conversation `permission_profile` selects Strict, Balanced, Trusted (new-chat default), or
Trust — destructive review only
in Settings. Old shadow environment flags no longer control live behavior.
Sanitized audits retain the decision and profile; new records mark the legacy
outcome `not_evaluated`. Old exact/tool-wide caches are not consulted.

Command patterns may decorate an ask with a warning, but cannot auto-deny it.
Role-specific lockdowns remain separate, as do native harness safeguards.
Loom policy outcomes identify `loom_policy`; only an explicit human response
identifies `user`. Denial notes and lifecycle/timeout feedback are preserved.

### Native boundary (not a universal sandbox)

This policy governs approvals delivered to Loom. It does not intercept every
operation inside every harness. Claude/Antigravity hooks route ordinary tools
through it; Goose uses native `approve` even for legacy Smart selectors so its
classifier does not pre-empt Loom. Goose's tool-free `chat` mode is unchanged.
Codex continues with hooks disabled and its existing workspace-write sandbox;
native auto-approved operations do not consult these profiles. Strict means
strict handling
of *bridged requests*, not a read-only filesystem or identical native prompt counts.
No provider installs a session-wide grant from Loom's “Allow for Branch”.

