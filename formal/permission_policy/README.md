# Loom permission policy proofs

This project is a development/CI specification, not a Loom runtime dependency.
It mirrors the finite decision algebra in permission_policy.py, proves the core
provenance/scope/profile invariants, and generates the JSON cases consumed by
the Python conformance suite.

The four profiles generate 2,048 exhaustive decisions. The opt-in `trust_review`
profile asks for recognized destructive actions before considering remembered
rules, and allows all other valid actions. Proofs also preserve the original
three profiles' outcomes regardless of the new evidence bit. Detection itself
is tested boundary code, not proven by Lean.

Run `lake build`, then run:

```text
lake exe loom_permission_fixtures generated/permission_cases.json
```

CI must fail when regenerating the fixture changes the checked-in file. Provider
normalizers then run the same generated cases through the Python runtime.

Lean proves behavior after a request has been normalized. Provider payload
parsing, path canonicalization, and command-family extraction remain boundary
code and require property, fuzz, and adapter integration tests.

The live Python endpoint now enforces this policy for Loom-bridged requests.
Profiles are persisted per conversation; branch grants remain server-session
local and revocable. See `../../PERMISSION_POLICY.md` for native-harness limits.
Audit records contain no raw commands, arguments, feedback, or workspace paths.
