# Explicit chat attachments and file links

Ordinary agent harnesses use the shared `loom-agent-control` MCP surface:

```python
attach_to_chat(path="artifacts/chart.png", caption="Selected chart")
attach_to_chat(content="# Report\n\nResults", kind="markdown", label="Report")
attach_to_chat(content="print(42)", language="python", label="Example")
link_file(path="artifacts/dataset.zip", label="Download current dataset")
```

`attach_to_chat` captures a version in external EXO state storage. Validated
PNG/JPEG/GIF/WebP images display inline; UTF-8 Markdown and code files show
previews; other files offer downloads. Snapshots are limited to 32 MiB and text
previews to 256 KiB. A reply supports up to 32 selected items. Code previews
escape HTML; Markdown uses the chat's existing sanitizer.

`link_file` stores a workspace reference without copying file bytes. Clicking
downloads the current file, including code, archives and other binary formats.
Links are marked **Current file**. Removed files return **File no longer
available**. Path containment is checked at selection and again at download;
live files always download instead of executing at Loom's origin.

The caller supplies its exact run identity and server-issued live run capability,
never a target message. Capabilities expire with their task and on server restart;
reused numeric generation IDs cannot authorize an old agent. Loom binds
parent selections to the live run's assistant draft and delivers `chat_item`
events through exact run subscriptions. Draft persistence, reconnects and
reloads use the same `messages.chat_items` list, independently of streamed text
and tool blocks. Missing or ended runs cannot publish. New replies start with
an empty list; filename mentions and tool observations no longer select images.
Older replies with a NULL list retain their historical rendering.

Child calls select items on that child run. `get_agent` returns `chat_items`
without publishing them to the parent tree. The parent explicitly chooses:

```python
attach_to_chat(source_id="<id returned in child chat_items>")
```

This reuses the selected snapshot even if the source file changes. Child file
selection respects its declared read/write paths. Sensitive children cannot
publish; they retain the existing human-release handoff. Locked-down operator
tool surfaces keep their existing restrictions.

Metadata is stored in the conversation database; snapshot blobs live under
`state_path("chat-attachments")`, outside project workspaces. Forking a
conversation creates independent item references to the same preserved blobs.
Deleting the original conversation does not break the fork's downloads.
Unreferenced blobs are retained; automatic storage reclamation is not provided.

`POST /api/conversations/{id}/chat-items` is an agent-channel admission route.
`GET /api/chat-items/{id}/file` uses the normal browser gate; it is not on the
agent-channel allowlist. Explicit request IDs make retries idempotent. Raster
snapshots may render inline; `?download=true` always downloads them.

Validation: `tests/test_chat_presentations.py` covers ownership, path boundaries,
snapshots versus live files, retries, concurrent appends, child selection,
sensitive restrictions and forks. `tests/test_chat_presentations_browser.py`
uses Chromium and a disposable server for live display, downloads, safe text
previews, reloads and legacy rendering. These tests do not launch providers or
control the active Loom host.
