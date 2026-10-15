# Child access and disclosure

`lake build --wfail` checks the proofs. `lake exe agent_access_fixtures cases.json`
generates 56 Boolean cases consumed by `tests/test_agent_access.py`. CI must
regenerate and compare the checked-in oracle.

The model proves conflict symmetry, reader coexistence, blocking overlapping
writes, admission requiring no conflict, private unreleased output exclusion,
and local/sensitive inheritance. Runtime path overlap and nesting use these
decisions after filesystem normalization. These are finite policy proofs, not
a proof of the scheduler's entire event trace or OS sandboxing.

Path identity, symlinks, provider tool normalization, trusted browser identity,
process exit, database writes, local routing and effect execution remain tested
boundaries. Read/write reservations coordinate cooperative child runs only;
they do not reserve the parent agent's filesystem access. Structured bridged
file operations reject undeclared paths; arbitrary writable-agent shell effects
are not inferred from command text. Native operations bypassing the bridge are
not certified by this model. Sensitive local children therefore reject bridged
shell, network and unknown external operations and expose private data only to
paired browser credentials. Shared machine processes and files are outside this
API disclosure boundary.
