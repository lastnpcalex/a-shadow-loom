# Portable context calculus and further proof plan

This is an executable specification, not a new runtime implementation.
Pinned toolchain: Lean 4.33.1; no third-party Lean packages needed.

```powershell
lean --run Model.lean cases.json
```

Rebuilding the oracle must produce the checked-in file byte-for-byte. CI should
compile with warnings as errors and compare regenerated output before running
the conformance suite. The current task compiled using the installed compiler
directly because the elan launcher attempted an unnecessary network check.

## What the current file proves

- Stop dominates every decision.
- Compatible native resume takes priority over replay/preparation.
- Retrieval requires failed or unsupported preparation; `untried` selects prepare.
- Execution of a preparation result requires fit.
- Accepted fragment progress is contiguous and within the recorded total.
- Cancelled preparation cannot advance coverage.
- Exact prefix plus uncovered suffix reconstructs the source sequence.
- Complete request admission includes output reserve.

`cases.json` enumerates 128 combinations, including inconsistent/unreachable
inputs to detect accidental fallthrough. Python test coverage checks the oracle;
it does not yet compare a production reducer, which has not been implemented.

## Required next model: transitions, not another decision table

Extend this namespace with branch/source revision, owner/attempt, covered prefix,
fragment cursor, destination identity, preparation phase, remaining call budget,
published summary reference, retained tail, terminal evidence and exit evidence.

Events: Begin, AcceptFragment, RejectFragment, Publish, Append, EditPrefix,
Fork, Cancel, ProviderExit, Retry, FinalAdmission, Execute, RetrievalFallback.

Prove inductively over reachable traces:

1. Accepted coverage never skips/repeats a fragment or crosses a sibling branch.
2. Publication requires an unchanged covered prefix; appending beyond it is safe.
3. The current user input and declared retained tail remain uncovered.
4. Every preparation execution uses the bound destination, with mutations disabled.
5. Stop revokes publication/execution authority. Teardown precedes replacement.
6. Each retry decreases a natural-number budget and changes an admissibility
   constraint; no infinite retry is possible. Rejected fragments do not advance.
7. Queue dispatch cannot treat preparation completion as user-turn completion.
8. Identical repeated event delivery cannot execute an effect twice.
9. Conditional progress: finite evidence, positive chunk allowance, responsive
   selected provider, successful valid compact outputs and fair scheduling imply
   reaching execution; bounded failure reaches explicit fallback/error instead.

The final statement must list its assumptions. A summary can fail to compress or
lose an essential fact; Lean cannot prove those away. A giant indivisible task
may need exact document retrieval even when the historical view is compact.

## Connecting proofs to executable Python and adapters

Extract one pure decision/transition function from the existing runtime during
implementation. Compare every generated case/trace against that actual function.
Do not introduce a second Python reference implementation solely to make the
oracle green. Assert at production entry points that the decision is consumed.

Generate traces involving model switches, edits, append-during-preparation,
cancel/exit, duplicate terminal events, smaller runtime limits and retry exhaustion.
Inject deliberate mutants (untried -> retrieve, ignore revision, ignore output
reserve, advance rejected fragment, ignore stop) and require conformance failure.

Test SQLite publication transactions under interleavings, test adapter identity
and tool restriction through actual protocol requests, and test semantic results
with native inference separately. A proof about truthful input flags does not
prove that adapters supplied truthful input flags.
