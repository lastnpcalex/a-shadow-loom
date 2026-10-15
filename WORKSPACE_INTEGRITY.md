# File preservation: guarantees and failure-mode audit

## Incident class

An agent treated an ignored Canvas entry page as a blank task surface. A later
agent recreated a smaller approximation and called it recovery. Git ignore
status did not cause either write; it hid the files from inventory and the
recovery snapshot. No real workspace files, exam data, or user transcripts are
included in this document or the regression fixtures.

## Runtime protections

`workspace_integrity.py` runs at the shared permission endpoint for recognized
structured writes, edits, file deletions and patch targets. It checks live paths,
not Git membership. Existing file bytes are copied to the external recovery
store and their stored hash is verified before authority is returned. The journal
records workspace, conversation, run scope, file path, byte hash and blob location;
it does not record the proposed new contents or shell commands.

All permission profiles, including Trust/review, require one-time review for:

- replacing an existing file, including add/create against an existing path;
- exact whole-file replacement disguised as an old-string Edit;
- recognized deletion and overwriting an existing rename destination;
- edits targeting every line, at least 25 old lines, or at least 5 and half the file.

These reviews cannot be remembered or bypassed by an earlier branch grant.
Small targeted edits and new files retain their normal profile decisions.
The existing chat, notification and admin approval cards display the reason and
recovery manifest location. Denial notes and exact run ownership are unchanged.
If the destination changes while approval is pending, the approval becomes a
Loom integrity refusal, never a claimed user denial. Failure to read, bound,
persist or verify an in-workspace preimage refuses the write with an explanation.
Limits currently match recovery: 8 MiB/file, 256 MiB/request, 128 targets/request.

Loom's `loom-workspace` file tools also recheck immediately before mutation,
create new files exclusively, preserve CRLF in targeted replacements, and publish
existing-file replacements atomically, retaining file mode bits. This does not
promise preservation of every platform-specific ACL, alternate stream or inode
identity. Native structured tools execute after the
permission response, so their final check/write interval is not atomic with Loom.
The unified-patch broker refuses unsupported binary, rename/copy-only, mode-only,
symlink and submodule patches instead of inspecting only part of a mixed patch.

## Canvas recovery and instructions

Pre/post-turn recovery explicitly includes the Canvas tree even in a Git checkout
whose ignore rules hide it. Entry-page capture is prioritized. Traversal is capped
at 4,096 Canvas entries, excludes dependency/build directories and rejects links
and junctions; skipped coverage is reported. Warm files still use the existing
fingerprint/cache checks. Other ignored folders are **not** bulk-indexed: their
recognized structured write targets get pre-write journals instead.

The shared contract, newly created guide and per-turn Canvas notice explicitly
identify Canvas as persistent user work shared across branches. Unrelated
prototypes belong on separate pages. Existing workspace guides are preserved,
not overwritten to install new prose. The short notice reaches native resumes.

Recovery lives outside project Git in `workspace-recovery/writes/conversation-N`
and `workspace-recovery/snapshots/conversation-N`. Blobs may contain private data;
keep this local state restricted, never publish it or feed it to search/cloud
indexing. Restore only after comparing with current files and with explicit
authority. This feature never automatically rolls a workspace back.

## Proactive audit: fixed and remaining boundaries

| Failure mode | Status |
|---|---|
| Ignored Canvas absent from snapshots/change reports | Covered by explicit bounded Canvas capture |
| Whole-file write silently approved as normal editing | One-time preservation review plus verified preimage |
| Create/add or rename overwrites a pre-existing destination | Destination inspected and journaled/reviewed |
| Branch grant bypasses replacement review | Integrity precondition takes precedence; regression tested |
| File changes while the human reviews approval | Revalidated; stale approval refused with Loom source |
| Broker writes its old pre-approval read | Rechecked; exclusive create and atomic replacement |
| Broker changes every CRLF while editing one string | Bytes decoded without newline normalization |
| Hook strips meaningful quotes from file contents | Only legacy path wrappers are unquoted |
| Recovery traverses links/junctions | Rejected/skipped; coverage failures explicit |
| A recreated approximation is represented as restoration | Contract forbids it; cannot mechanically prove semantic equivalence |
| Arbitrary scripts, native auto-approved tools, plugins and external writers | Not intercepted; do not claim universal write enforcement |
| A model actually reads and understands current contents | Behavioral requirement, not proven by a permission request or file hash |
| Small semantically destructive edits | Not decidable from line counts; preserve preimages and run feature tests |
| Native file changes after approval, including concurrent writers | Final TOCTOU remains; needs a write broker/native precondition or OS enforcement |
| Unstructured writes outside Canvas to ignored files | Outside current snapshot/journal coverage; needs explicit coverage policy or write interception |
| Mutations outside the assigned workspace | Existing permission policy remains; no pre-permission read or journaling of external/private targets |
| Child shell writes while no top-level snapshot is active | No independent child Canvas snapshot guarantee; structured approvals are journaled |
| Archive storage growth and private-data retention | Requires configurable retention/export controls; never auto-delete evidence in this repair |

## Tests and formal boundary

`tests/test_workspace_integrity.py` uses disposable workspaces and the actual
provider normalizers/shared endpoint, not a real exam or paid model. It covers
ignored Canvas recovery, other ignored structured targets, create collisions,
stale approvals, native add/delete/rename shapes, patch parsing, content quoting,
CRLF preservation, corrupt blobs, bounds, and branch-grant precedence.
The actual mobile approval renderer is exercised in `test_permission_ui.py`.
Both permission and lifecycle preflights include the integrity regression file.

The Lean permission table still models the profile decision. Audit records
separately report `effective_decision` and `integrity_review`; this filesystem
precondition is **not covered by that Lean proof**. It is currently protected by
runtime checks and integration tests. A hash is evidence about bytes, not a proof
that the agent inspected them or that the requested change preserves behavior.
