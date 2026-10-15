# Chat workspace

A fresh EXO page opens a blank casual chat. Explicit conversation/branch links
and this tab's saved Library, chat or tree view still take precedence. Merely
opening the composer does not create a conversation or working directory.

Choose a model, then send the first message. EXO creates the conversation in
`ExoHome/` under the EXO checkout. The directory and all its contents are ignored
by the host Git repository. Conversation messages remain in the existing database.
Casual chat creation has a durable request ID so a lost HTTP response can be
retried without duplicating the conversation. Messages use the existing thread,
cursor, retry and generation contracts.
Pending creation locks the model and effort until preparation succeeds or New
chat discards that request. Partial loading failures return to the saved draft,
including attachments and description text. Deleted creation requests return
410 on retry rather than recreating the chat.

The left sidebar defaults to branches in the selected conversation/space, sorted
by the final message's timestamp (newest first). Each row uses its root question
and branch label. Conversations on the toolbar reveals the project list and
filter; Folders opens the existing folder library; Tree opens the current tree.
Entering a space from either list opens a blank root thread. Explicit branch links
and this tab's saved cursor retain their selected path. New branch creates a blank
root inside the current space, or a casual composer when no space is selected.
New space opens the existing conversation/workspace setup dialog.
The wider desktop sidebar groups saved Pinned paths, Recents (last seven days),
and Older branches. Its filter matches branch questions, previews, and pin labels;
Search opens full Search Space. Pin branch uses the existing persistent pin editor.

The right sidebar provides planning artifacts, scoped files, read-only Git
status/change summaries/history, agent controls, reported reply usage, and Canvas.
Git information is displayed only when the selected working directory itself is
the repository root; an enclosing EXO repository is never presented as ExoHome's
history. File inventory is bounded and excludes private paths and linked entries.
Previews retain the existing image/text allowlist and sandbox response headers.
Directory traversal pins directory handles and rejects Windows private-path
aliases. Git inspection disables filesystem-monitor hooks and caps both output
pipes before decoding; oversized output is returned as an incomplete summary.

On phones, sidebars start collapsed, opening one closes the other, and Escape
closes both. Desktop sidebar preferences are remembered. Phone classification
uses existing pointer/viewport detection; processor architecture is irrelevant.
Desktop chat and its composer share one column, up to 1120 pixels wide, so hiding
a pane keeps them aligned. Agents sits beside Workspace on the toolbar. Canvas
also has an independent working pane: drag its divider or use the arrow keys to
resize it; the preferred width is remembered. On phones it overlays the chat
instead of shrinking it. Hidden Canvas frames unload; existing revision polling
and the same-origin Canvas bridge apply to both previews and the working pane.

Full Search Space is available at `/search`. It uses the existing search operator,
citations and workspace-wide disclosure policy. Search transcripts remain
ephemeral. ExoHome starts Excluded until the user explicitly classifies it.

ExoHome is a shared working directory: casual conversations can see the same
files and Canvas entry. Gitignore is not a sandbox or an independent Git history.
Existing conversations keep their stored workspaces. No agent prompts, provider
adapters, permission policy or session-continuity rules are changed by this UI.

Backend routes and the additive creation-request table take effect on the next
human-managed main-server reload. Frontend assets may update before that reload;
unavailable APIs report errors and preserve the casual draft. Agents do not restart
the running Loom host as part of deployment or validation.
