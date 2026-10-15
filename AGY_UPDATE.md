# Antigravity integration cleanup

Reviewed against the installed `agy models` output on 2026-09-12. The fallback
catalog includes Gemini 3.8/3.7/3.6 Flash and 3.1 Pro. Exact discovered model
selectors are preserved, including non-Google models served through agy.

The model refresh button uses `/api/cc-models/refresh?provider=gemini` when an
Antigravity model is selected. Discovery runs off the main event loop and needs
no Claude login. Validated, deduplicated rows are written atomically beneath
`exo_state.state_root()/model-discovery/agy.json`. A failed refresh retains the
existing picker and cache. Startup reads caches without launching agy; the old
`~/.gemini/models_cache.json` remains a read-only compatibility fallback.

Stream tool IDs prioritize the hook-compatible numeric step index, including
zero. Empty `DONE` results close tools. Session identity is captured from root
initialization events, and reasoning deltas use the shared `text` field.
Directory searches retain their paths for shared permission boundary checks.

Actual `ExitPlanMode` requests require a scoped user response even under Trust
with destructive review. The Lean decision algebra is unchanged; this is an
explicit interaction gate around automatic tool permission. Plan notifications
are informational and do not submit competing chat turns. Writing a plan file
alone neither means approval nor pauses the CLI. Question notifications retain
the existing composer behavior; they do not claim a native in-process answer
channel. No file-name heuristic authorizes implementation.

Regression coverage is in `tests/test_agy_update_regressions.py` and
`tests/test_plan_approval_hook.py`, alongside operator, permission, model-registry,
attestation, and provider contract tests. Live model discovery was checked; this
cleanup does not claim an end-to-end live agy generation or fork acceptance run.

Cleanup validation: 99 Gemini/operator/model/permission tests, 284 shared
permission/provider/Goose/local-context/Codex tests, and four targeted API tests
passed (387 total). JavaScript syntax and Python compilation checks passed.
The shared test run emitted a Playwright `TargetClosedError` during teardown
despite all assertions passing, plus dependency/deprecation warnings.
