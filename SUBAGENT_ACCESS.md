# Child agents: access, concurrency and private results

Spawn with `access={"read":["inputs"],"write":["out/deck-a.pptx"],"sensitive":false}`.
An empty write list means read-only. A path covers that file or a directory's
descendants; declare temporary and output locations as well. Separate decks can
be edited concurrently. Readers share paths; a reader and writer of the same
path serialize. Omitted access preserves existing exclusive `path_scope`
semantics, including a whole-workspace reservation for empty legacy scope.
Nested children inherit omitted access and may narrow it, never broaden it.

`LOOM_AGENT_MAX_CONCURRENT` defaults to four independent leaf jobs.
`LOOM_AGENT_MAX_LOCAL_CONCURRENT` defaults to one and can be increased when the
inference server has sufficient slots/memory. This is admission capacity, not
verified model-server capacity. Queue metadata distinguishes workspace conflict,
local inference capacity and global worker capacity. Failed teardown retains
reservations; recovery keeps the persisted declaration.

The dock above the composer remains visible while closed, with running/queued/
completed counts and a separate approval button. Requests live in their child's
card and share the existing decision transport with notifications. Full task,
exact request arguments and complete final text have scrollable detail views.
Live output remains a bounded tail; API events retain paginated full evidence.
No request automatically opens the dock or moves the chat cursor.
The toolbar's Goals control opens the former agent side lane. Goal controls no
longer occupy the composer; without a goal the control opens the goal editor
and reserves no lane space. Agents are managed solely from the composer dock.

Use `profile="local"` and `access.sensitive=true` for private work. Admission and
launch reject cloud inference, and nested declarations must retain sensitivity.
List/get/wait/cancel/resume return an allowlisted lifecycle projection to agents
and unpaired clients. Raw records/events and approval inputs require paired
browser credentials (even on loopback). A completed child's card lets the human
write a handoff; POST `/api/agent-runs/{run_id}/release` requires a paired browser
and publishes only that supplied text. Raw accumulated output is never released
implicitly. Source tasks must not contain secrets already seen by a cloud parent.

This is not an OS isolation claim: processes share the machine, database and
workspace. Sensitive children reject bridged shell/network/unknown operations;
read-only children reject execution and unclassified mutations. Structured file
tools enforce path declarations when they reach the bridge. Native tools outside
that bridge, arbitrary shell effects in ordinary writer tasks, external writers,
and parent filesystem access remain outside the guarantee. A stronger hostile
process boundary requires an OS sandbox and isolated storage.

Proofs and their trust boundary: `formal/agent_access/README.md`. Tests cover
separate deck writers, competing writers, readers, nesting, disclosure negative
controls, structured access and existing teardown/recovery/browser behavior.
