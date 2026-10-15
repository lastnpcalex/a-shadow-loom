# Implementation Plan: Fix Goose Local Truncation, Disappearing Turns, and Model Discovery

## Context & Objectives
This plan covers two interrelated stability and UX problems in Loom:
1. **Conversation 201 (Goose ACP with local llama model on Raspberry Pi)**:
   - Prompt truncation on `llama-server` due to NUL bytes (`\x00`) in device tree / sysfs tool outputs.
   - Empty-text assistant turns terminating without an explicit failure state in `server.py`.
   - DOM cleanup in `static/chat.js` deleting saved messages that have empty text but contain tool calls and thinking.
2. **Model Refresh in Settings (Antigravity & ChatGPT Codex discovery bypass)**:
   - The "↻ Refresh" button next to "Loom Model" currently uses a frontend selection hack (`isAgy ? '?provider=gemini' : ''`).
   - The general refresh path fails with HTTP 401 if Claude Code OAuth is missing, blocking discovery for all other providers.
   - The general refresh path reads stale cache files on disk instead of invoking live CLI discovery for AGY (`agy models`) or Codex (`codex app-server` `model/list`).
   - There is no active discovery runner for Codex in `codex_client.py`.

---

## Part A: Goose ACP Truncation & Disappearing Turns

### 1. Strip NUL Bytes in `local_context_guard.py` & `llama_client.py`
- Add recursive helper `_strip_nul_bytes(obj)` in `local_context_guard.py`:
  - Strips `\x00` characters from strings, lists, and dicts.
- Call `_strip_nul_bytes()` in:
  - `_forward()`: on incoming POST request bodies (`v1/chat/completions`, `v1/messages`).
  - `prepare()`: before context token counting or compaction.
  - `count()`: so `/apply-template` and `/tokenize` calls never encounter NUL characters.
- In `llama_client.py`:
  - Sanitize messages in `chat_completions()` before passing to `/v1/chat/completions` on llama-server.

### 2. Handle Zero-Text Agent Turns in `server.py`
- In `_handle_goose_generation` (`server.py` around line 12467):
  - If `not full_text.strip()` and `not got_error`:
    - Set `got_error = True`.
    - Set `last_error = _goose_error_text(_cc_result_error_detail(result_info), "") or "Goose ended without producing a final response"`.
  - The existing error branch will invoke `_append_provider_failure_note(full_text, content_blocks, "Goose", error_msg)`, attaching `\n\n---\n[Goose turn failed after N tool call(s) without a final response: ...]` to `content` and setting `turn_lifecycle_state = "partial_failed"`.
- In `_handle_claude_generation` (`server.py` around line 11406):
  - Expand `failed_turn` to include `not full_text.strip()`:
    `failed_turn = got_error or result_info.get("is_error") or not result_info or not full_text.strip()`
  - Ensures Claude/Gemini/Codex turns ending with tool blocks but zero final text receive `_append_cc_interrupt_note`.

### 3. Prevent DOM Deletion of Saved Messages in `static/chat.js`
- In `createMessageElement` (`static/chat.js` line 5380):
  - Tighten `isDraft` so messages containing `content_blocks` (tools/thinking) or existing lifecycle states are never marked as `.message-generating`:
    ```javascript
    const hasBlocks = Array.isArray(msg.content_blocks)
        ? msg.content_blocks.length > 0
        : (typeof msg.content_blocks === 'string' && msg.content_blocks.length > 2);
    const isDraft = msg.role === 'assistant'
        && !msg.content?.trim()
        && !hasBlocks
        && !isErrorMsg
        && (!msg.turn_lifecycle_state || msg.turn_lifecycle_state === 'generating');
    ```
- In `removeStreamingMessage` (`static/chat.js` line 6810):
  - Do not remove elements from the DOM if `el.dataset.msgId` matches a message in `State.messages` that has content, tool blocks, or a lifecycle state:
    ```javascript
    document.querySelectorAll('.message-generating').forEach(el => {
        const msgId = el.dataset?.msgId;
        if (msgId && State.messages?.some(m =>
            String(m.id) === String(msgId) &&
            (m.content?.trim() || (Array.isArray(m.content_blocks) ? m.content_blocks.length : m.content_blocks) || m.turn_lifecycle_state)
        )) {
            el.classList.remove('message-generating');
            return;
        }
        el.remove();
    });
    ```

---

## Part B: Active Model Discovery & Decoupled Refresh

### 1. Active Codex Discovery in `codex_client.py`
- Implement `discover_codex_models(force: bool = False) -> list[dict]`:
  - Cache location: `~/.codex/models_cache.json`.
  - Check cache timestamp: if exists, not `force`, and updated within the last 3600 seconds, return parsed models.
  - Active runner: Launch `codex app-server --stdio --disable hooks` with a 4-second timeout.
    - Send JSON-RPC `initialize` (`clientInfo={"name": "Loom", "version": "1"}`) -> receive result.
    - Send notification `initialized`.
    - Send request `model/list` -> receive list of model objects (`id`, `displayName`, `description`).
    - Terminate child process cleanly using `cancel_codex(proc)` to avoid orphan processes on Windows.
  - Cache persistence: Write returned models to `~/.codex/models_cache.json` atomically.
  - Fallback: If CLI discovery fails or times out, gracefully fall back to existing cache or `load_local_codex_models()`.
  - Return normalized entries: `[{"value": f"codex-{slug}", "label": f"Codex ({display_name})"}]`.

### 2. Decouple & Unify Backend Refresh in `server.py`
- Refactor `api_cc_models_refresh(provider: str = "all")`:
  - Parameter `provider`: `"all"` (default), `"anthropic"`, `"gemini"`, `"codex"`.
  - Concurrently execute discovery tasks using `asyncio.gather(..., return_exceptions=True)`:
    - **Anthropic**: Read OAuth token. If missing, record in `skipped["anthropic"] = "No Claude Code OAuth token found (~/.claude/.credentials.json)"` (do not abort endpoint with 401 when `provider == "all"`). If present, query `https://api.anthropic.com/v1/models` and rebuild `Anthropic — Auto` and `Anthropic — Pinned` groups. If `provider == "anthropic"` explicitly and token is missing, return 401 for backward compatibility.
    - **Gemini**: Run `await asyncio.to_thread(gemini_client.discover_agy_models)`. Update `Antigravity` group in `CC_MODELS`. If empty, fall back to `load_local_gemini_models()`. Record in `refreshed` or `skipped`.
    - **Codex**: Run `await codex_client.discover_codex_models(force=True)`. Update `ChatGPT Codex` group in `CC_MODELS`. If empty, fall back to `load_local_codex_models()`. Record in `refreshed` or `skipped`.
  - Response payload:
    ```json
    {
      "ok": true,
      "refreshed": ["gemini", "codex"],
      "skipped": {"anthropic": "No Claude Code OAuth token found"},
      "models": [...],
      "families": [...]
    }
    ```

### 3. Frontend Modernization in `static/app.js` & `static/index.html`
- In `static/app.js` (`#btn-refresh-cc-models` click handler):
  - Always call `POST /api/cc-models/refresh?provider=all`.
  - Format informative feedback toast based on `data.refreshed` and `data.skipped`:
    e.g. *"Refreshed Antigravity (14) & Codex (7) models. Anthropic skipped (not logged in)."*
  - Re-render model dropdowns via `populateCCModelDropdowns()`.
- In `static/index.html`:
  - Update `#btn-refresh-cc-models` title: `"Refresh cloud & agent models (Claude, Antigravity, Codex)"`.
  - Update `#btn-refresh-models` title: `"Re-scan local GGUF models on disk and query llama-server"`.

---

## Verification & Testing Plan
1. **Unit tests in `tests/test_local_context_guard.py`**:
   - `_strip_nul_bytes` recursive sanitization.
   - Request bodies with NUL bytes sanitized before `/apply-template`, `/tokenize`, and `/v1/chat/completions`.
2. **Unit tests in `tests/test_api.py`**:
   - `api_cc_models_refresh` with `provider="all"` when Anthropic OAuth is missing: returns 200 with Antigravity and Codex refreshed and Anthropic reported in `skipped`.
   - `api_cc_models_refresh` with `provider="anthropic"` when token missing: returns 401.
   - Provider failure isolation: simulated failure in one provider does not prevent the others from refreshing.
3. **Turn failure note tests**:
   - Verify Goose turn with tool calls and empty text receives `partial_failed` and failure note.
4. **Manual verification**:
   - Load conversation 201: verify previously hidden messages 28140, 28142, and 28148 now appear.
   - Click "↻ Refresh" in Settings: verify models from Antigravity and Codex update without requiring Claude login.
