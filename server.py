"""FastAPI server with REST endpoints and WebSocket streaming."""

import sys

# Windows defaults stdout/stderr to the system codepage (CP1252), which cannot
# encode characters outside Latin-1 (e.g. Greek, Cyrillic).  Reconfigure to
# UTF-8 so print() / logging never raises UnicodeEncodeError.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import time as _time  # alias used by generation_ms timing in CC/OODA/Weave handlers
import uuid
from pathlib import Path

from asyncio_compat import install_windows_transport_cleanup

install_windows_transport_cleanup()

from fastapi import (
    Body,
    FastAPI,
    Header,
    WebSocket,
    WebSocketDisconnect,
    UploadFile,
    File,
    HTTPException,
    Request,
)
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.websockets import WebSocketState

from canvas_slug import generate_canvas_slug, is_valid_slug
from canvas_workspace import add_canvas_notice, canvas_revision, canvas_root, ensure_canvas_workspace

import httpx
import database as db
import coordination
import agent_access
import chat_presentations
from config import config
from character_loader import (
    load_all_characters,
    load_character,
    save_character,
    delete_character,
    load_all_personas,
    load_persona,
    save_persona,
    delete_persona,
    load_all_lore,
    load_lore_entry,
    save_lore,
    delete_lore,
)
from local_llm import health_check, stream_chat, sync_chat, describe_image
from ooda_harness import (
    build_ooda_system_prompt,
    parse_ooda_block,
    repair_ooda_block,
    extract_post_ooda_prose,
    execute_ooda_reads,
    execute_ooda_updates,
)
from prompt_engine import (
    build_system_prompt,
    assemble_prompt,
    get_style_nudge,
    STYLE_NUDGES,
)
from context_manager import get_context_for_generation
import claude_client
import gemini_client
import codex_client
import goose_client
import model_context
import model_registry
import permission_policy as permission_policy_model
import permission_rules
import turn_recovery
from dataclasses import replace as dataclass_replace
from provider_contract import (
    ResumeDecision,
    add_continuity_notice,
    add_permission_feedback_notice,
    recovery_evidence,
    partial_replay_text,
    resume_rejected,
    context_exhausted,
    select_resume_session,
)
from workspace_safety import (
    capture_workspace_snapshot,
    default_recovery_root,
    finalize_workspace_snapshot,
)
from workspace_integrity import prepare_integrity_check, revalidate_integrity
from event_router import (
    canonical_run_id,
    EventSequencer,
    GLOBAL_NOTIFICATION_KINDS,
    LoomEventEnvelope,
    SubscriptionRouter,
    content_free_notification,
)
import local_summary
import openrouter_client
from search_space import (
    SearchBackendUnavailable,
    SearchIndex,
    SearchOperator,
    SearchPolicyError,
    canonical_workspace,
    workspace_key,
)
from search_space.service import SearchService
from search_space.chat import SearchChat
from search_space.scopes import resolve_scope
from search_space.policy import Policy, may_read
from skill_scanner import get_all_skills, BUILTIN_COMMANDS
from agent_supervisor import AgentSupervisor, AgentTeardownError
import image_generation
import loom_auth
from acceptance_status import source_identity as acceptance_source_identity

from contextlib import asynccontextmanager

CRON_DB_PATH = Path("loom_cron.db")
CRON_ALLOWED_SUFFIXES = {".py", ".ps1", ".js"}
CRON_MIN_INTERVAL_SECONDS = 60
CRON_DENY_PATTERNS = (
    "rm -rf",
    "remove-item -recurse",
    "remove-item -force -recurse",
    "del /s",
    "erase /s",
    "rmdir /s",
    "rd /s",
    "format ",
    "shutdown ",
    "reboot",
    "mkfs",
    "diskpart",
)
_cron_scheduler_task: asyncio.Task | None = None
_queued_generation_scheduler_task: asyncio.Task | None = None
_agent_supervisor: AgentSupervisor | None = None
_shutdown_requested = False
_generation_admission_lock = asyncio.Lock()
_background_tasks: set[asyncio.Task] = set()
_cron_running: set[int] = set()
_dream_shim_proc: subprocess.Popen | None = None
_search_index = SearchIndex()
_CASUAL_CHAT_ROOT = Path(__file__).parent / "ExoHome"


def _search_source_id() -> str:
    """Keep identical message IDs from switchable Loom databases separate."""
    identity = str(Path(db.DB_PATH).expanduser().resolve())
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _truthy_setting(value) -> bool:
    """Normalize SQLite/API boolean-ish values without making "0" truthy."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


_AGENT_WORKSPACE_MODES = frozenset({
    "claude", "local", "gemini",
    "codex", "goose", "umans", "openrouter",
})


def _minimal_ooda_enabled(conv: dict | None) -> bool:
    if not conv or conv.get("minimal_ooda_enabled") is None:
        return True
    return _truthy_setting(conv.get("minimal_ooda_enabled"))


def _mode_for_cc_model(cc_model: str | None) -> str | None:
    route = model_registry.resolve(cc_model)
    mode = route.get("mode")
    # Local/Weave keep their explicit conversation-mode selection.
    return mode if mode in {"claude", "gemini", "codex", "goose", "openrouter", "umans"} else ("claude" if is_dream_claude_model(cc_model) else None)


def _minimal_weave_system_prompt(conv: dict | None) -> str:
    """Minimal Weave must not fall back to the full RP system prompt."""
    ooda_guidance = """Before each response, privately use an OODA loop for roleplay continuity: observe the player's last action and the immediate scene, orient through the viewpoint and motives defined by this system message, decide on one concrete response beat, then act through in-character prose.

Do not print the OODA steps, analysis, XML tags, state-card operations, tool calls, or out-of-character notes. The visible answer should be only the roleplay response."""
    if not conv:
        return ooda_guidance
    value = conv.get("system_prompt")
    base = value.strip() if isinstance(value, str) else ""
    if not _minimal_ooda_enabled(conv):
        return base
    if not base:
        return ooda_guidance
    return f"{base}\n\n{ooda_guidance}"

# ── Canvas CLAUDE.md template ──
CANVAS_CLAUDE_MD = """\
# Interactive Canvas

This directory is a live canvas rendered in the user's browser as an iframe.
Write the page to index.html; no separate app server is needed. While visible
inside Loom, the iframe checks for saved changes roughly every three seconds.
See CANVAS_GUIDE.md for refresh limits and SDK behavior.
Existing Canvas files are persistent shared user work, even when gitignored.
Read the existing entry page and imports before editing. Preserve tools and data;
put unrelated prototypes on separate pages rather than replacing index.html.

## Structure

- `index.html` — entry point, always loaded by the iframe
- `triggers/*.md` — prompt templates for SDK-driven interactions (see below)
- All other files (CSS, JS, images) use relative paths from index.html

## Canvas SDK

Include the SDK to let the canvas page trigger Loom actions:

```html
<script src="/static/canvas-sdk.js"></script>
```

### API

| Method | Description |
|--------|-------------|
| `Loom.send(prompt, opts?)` | Send a chat message and trigger AI generation. `opts`: `{imagePaths: [...], parentId: int}` |
| `Loom.upload(file)` | Upload a `File` object. Returns `{path, url, is_image}` |
| `Loom.uploadAndSend(file, prompt)` | Upload a file then send a message referencing it |
| `Loom.loadTrigger(name, vars?)` | Load `triggers/{name}.md`, interpolate `{{key}}` → value |
| `Loom.dropZone(el, opts?)` | Make an element a file drop target. `opts`: `{trigger: 'name'}` or `{prompt: '...'}` |
| `Loom.getConvId()` | Returns `{convId: int}` — the current conversation ID |
| `Loom.on(event, handler)` | Listen for Loom events |

### Trigger templates

Put prompt templates in `canvas/triggers/*.md`. They're plain text with `{{variable}}`
placeholders that get interpolated by `Loom.loadTrigger()`.

Example `triggers/analyze.md`:
```
Analyze the uploaded file "{{filename}}" and update the canvas with a visualization.
Read the current Canvas first. Add results in a scoped panel or separate page;
preserve the existing entry point, tools, data, and navigation.
```

### Drop zone pattern

```javascript
const dropArea = document.getElementById('drop-area');
Loom.dropZone(dropArea, { trigger: 'analyze' });
// When a file is dropped:
// 1. File is uploaded to Loom
// 2. triggers/analyze.md is loaded and {{filename}} is filled in
// 3. Message is sent, AI generates a response
// 4. AI writes to canvas/, iframe auto-refreshes
```

### Direct send pattern

```javascript
document.getElementById('run-btn').addEventListener('click', () => {
    const userInput = document.getElementById('custom-input').value;
    Loom.send(`Update the canvas dashboard: ${userInput}`);
});
```

## Guidelines

- The canvas runs inside a sandboxed iframe with `allow-scripts allow-same-origin`
- You have full access to the Loom REST API (same-origin) from canvas JS
- Keep index.html self-contained or use relative imports
- The user sees the canvas in tree view as a thumbnail and can click to fullview
- Build progressively — start simple, layer in interactivity
- SDK sends continue the current branch and use normal queue/permission rules; they do not implicitly request parallel work
- Open the conversation in Loom for SDK sends; the standalone slug is a viewer without a selected chat branch
"""


def _resolve_agent_target(run: dict, conv: dict | None) -> tuple[str, str]:
    """Resolve a child request without silently changing its model."""
    requested_harness = str(run.get("requested_harness") or "").strip().lower()
    profile = str(run.get("model_profile") or "").strip().lower()
    harness = requested_harness
    if profile == "local":
        harness = "local"
    if not harness:
        harness = str((conv or {}).get("mode") or "claude").strip().lower()
    aliases = {
        "agy": "gemini",
        "antigravity": "gemini",
        "cc": "claude",
        "dream_claude": "dream-claude",
    }
    harness = aliases.get(harness, harness)

    model = str(run.get("requested_model") or "").strip()
    inherited_harness = not requested_harness and profile != "local"
    if not model and inherited_harness and conv:
        if harness in {"claude", "codex", "gemini", "goose", "openrouter", "umans", "dream-claude"}:
            model = str(conv.get("cc_model") or "").strip()
        elif harness == "local":
            model = str(conv.get("local_model") or "").strip()
    if inherited_harness and model and harness in {"claude", "openrouter", "umans"}:
        routed = _mode_for_cc_model(model)
        if is_dream_claude_model(model):
            harness = "dream-claude"
        elif routed:
            harness = routed
    if reason := model_registry.retirement_reason(harness, model):
        raise ValueError(reason)
    if harness not in {
        "claude", "codex", "gemini", "goose", "local",
        "openrouter", "umans", "dream-claude",
    }:
        raise ValueError(f"Unsupported child harness: {harness}")
    if not model and profile == "local":
        model = (
            str((conv or {}).get("local_model") or "")
            if (conv or {}).get("mode") == "local" else ""
        ) or config.llama_model
    if not model:
        defaults = {
            "claude": "sonnet",
            "codex": "Codex (GPT-4o)",
            "gemini": "Gemini 3.7 Flash (High)",
            "goose": config.goose_model,
            "local": config.llama_model,
            "dream-claude": (
                f"dream:{config.dream_model}" if getattr(config, "dream_model", None) else ""
            ),
            "openrouter": "",
            "umans": "",
        }
        model = str(defaults[harness] or "").strip()
    if not model:
        raise ValueError(f"No model configured for child harness: {harness}")
    if harness == "local" and is_dream_claude_model(model):
        harness = "dream-claude"
    return harness, model


async def _resolve_parent_run_context(conv_id: int, parent_run_id: str | None) -> dict | None:
    if not parent_run_id:
        return None
    parent_id = str(parent_run_id).removeprefix("agent:")
    if not parent_id.startswith("gen:"):
        return await db.get_agent_run(parent_id)

    try:
        gen_seq = int(parent_id.split(":", 1)[1])
    except (TypeError, ValueError):
        return None

    for key, snapshot in _generation_snapshots.items():
        if int(key[0]) == int(conv_id) and int(key[2]) == gen_seq:
            model = snapshot.get("cc_model")
            harness = snapshot.get("harness") or (_mode_for_cc_model(model) if model else None)
            draft_msg_id = snapshot.get("draft_msg_id")
            parent_msg_id = draft_msg_id or snapshot.get("parent_id") or key[1]
            return {
                "run_id": f"gen:{gen_seq}",
                "conversation_id": conv_id,
                "effective_model": model,
                "requested_model": model,
                "effective_harness": harness,
                "requested_harness": harness,
                "parent_message_id": parent_msg_id,
                "project_dir": snapshot.get("project_dir"),
                "loom_thread_id": snapshot.get("loom_thread_id"),
            }

    recent = _recent_generations.get((int(conv_id), gen_seq))
    if recent:
        model = recent.get("cc_model")
        harness = recent.get("harness") or (_mode_for_cc_model(model) if model else None)
        draft_msg_id = recent.get("draft_msg_id")
        parent_msg_id = draft_msg_id or recent.get("parent_id")
        return {
            "run_id": f"gen:{gen_seq}",
            "conversation_id": conv_id,
            "effective_model": model,
            "requested_model": model,
            "effective_harness": harness,
            "requested_harness": harness,
            "parent_message_id": parent_msg_id,
            "project_dir": recent.get("project_dir"),
            "loom_thread_id": recent.get("loom_thread_id"),
        }

    return None


def _agent_inheritance_context(conv: dict | None, parent: dict | None) -> dict:
    """Nested defaults come from the caller, not mutable conversation settings."""
    context = dict(conv or {})
    if parent:
        harness = parent.get("effective_harness") or parent.get("requested_harness")
        model = parent.get("effective_model") or parent.get("requested_model")
        if harness:
            context["mode"] = harness
        if model:
            context.update(cc_model=model, local_model=model)
    return context


async def _publish_agent_event(run: dict, stored_event: dict) -> None:
    """Publish a child event only to sockets subscribed to that run."""
    await _publish_agent_events(run, [stored_event])


def _agent_event_envelope(run: dict, stored_event: dict):
    payload = dict(stored_event.get("payload") or {})
    kind = stored_event.get("event_kind") or payload.get("type") or "agent_event"
    return LoomEventEnvelope.from_payload(
        {**payload, "type": kind},
        conversation_id=run.get("conversation_id"),
        loom_thread_id=run.get("loom_thread_id"),
        run_id=run.get("run_id"),
        parent_id=run.get("parent_message_id"),
        sequence=stored_event.get("sequence") or 0,
        event_id=stored_event.get("event_id"),
        scope="run",
    )


async def _publish_agent_events(run: dict, stored_events: list[dict]) -> None:
    if not stored_events:
        return
    events = [_agent_event_envelope(run, event) for event in stored_events]
    event = events[-1]
    frames = [_scrub_surrogates(item.as_dict()) for item in events]
    batch = _agent_event_envelope(run, {
        "event_kind": "agent_event_batch", "payload": {"events": frames},
        "sequence": event.sequence,
    }).as_dict()
    conv_id = int(run["conversation_id"])
    recipients = _event_router.recipients(event, _active_websockets.get(conv_id, set()))
    for websocket in list(recipients):
        try:
            if agent_access.sensitive(run) and not loom_auth.credential(getattr(websocket, "scope", {})):
                # No raw deltas, error strings, tool inputs or artifact paths.
                safe_events = [{**stored, "payload": agent_access.event_status(stored.get("payload") or {})}
                               for stored in stored_events]
                for safe in safe_events:
                    await websocket.send_json(_agent_event_envelope(run, safe).as_dict())
                continue
            if len(frames) > 1 and getattr(websocket, "_agent_event_batches", False):
                await websocket.send_json(batch)
            else:
                # Long-lived tabs with the previous bundle keep receiving the
                # original wire format until they refresh.
                for frame in frames:
                    await websocket.send_json(frame)
        except Exception:
            _active_websockets.get(conv_id, set()).discard(websocket)
            _event_router.unregister(websocket)


async def _teardown_child_provider(proc, harness: str) -> None:
    """Use the provider's tree cleanup and await exit before releasing a child."""
    cancel = {
        "codex": codex_client.cancel_codex,
        "gemini": gemini_client.cancel_gemini,
        "goose": goose_client.cancel_goose,
        "claude": claude_client.cancel_claude,
        "local": claude_client.cancel_claude,
        "openrouter": claude_client.cancel_claude,
        "umans": claude_client.cancel_claude,
        "dream-claude": claude_client.cancel_claude,
    }.get(harness)
    descendants = []
    try:
        if getattr(proc, "returncode", None) is None:
            if getattr(proc, "pid", None):
                import psutil
                try:
                    descendants = await asyncio.to_thread(
                        lambda: psutil.Process(proc.pid).children(recursive=True)
                    )
                except psutil.NoSuchProcess:
                    pass
            if cancel is None:
                raise RuntimeError(f"No child cleanup routine for {harness}")
            await cancel(proc)
        # Adapters that already reaped their process need no second wait.
        if getattr(proc, "returncode", None) is None:
            await asyncio.wait_for(proc.wait(), timeout=5)
        if getattr(proc, "returncode", None) is None:
            raise RuntimeError("Provider still has no exit status")
        if descendants:
            import psutil
            _gone, alive = await asyncio.to_thread(psutil.wait_procs, descendants, timeout=5)
            if alive:
                raise RuntimeError("Provider descendants are still alive")
    except Exception as exc:
        raise AgentTeardownError(
            f"{harness} child cleanup could not confirm process-tree exit; scope retained: {exc}"
        ) from exc


def _child_terminal_error(item: dict) -> str:
    """Normalize provider terminal failures without mistaking tool errors for failures."""
    kind = item.get("type")
    reason = str(item.get("stop_reason") or item.get("stopReason") or "").lower()
    if kind != "error" and not (
        kind == "result" and (item.get("is_error") or reason in {
            "error", "failed", "incomplete", "cancelled", "canceled", "interrupted",
        })
    ):
        return ""
    detail = item.get("error") or item.get("message") or reason or "Provider failed"
    if isinstance(detail, dict):
        detail = detail.get("message") or json.dumps(detail)
    return str(detail)


async def _launch_agent_run(
    run: dict,
    emit,
) -> dict:
    """Run one child provider turn without creating a conversation message."""
    conv = await db.get_conversation(int(run["conversation_id"]))
    # Provider adapters read these attributes when constructing MCP/hook
    # configuration.  They live on the supervisor task, never on the parent
    # conversation generation task.
    current_task = asyncio.current_task()
    if current_task is not None:
        current_task._client_instance_id = run.get("client_instance_id")
        current_task._agent_run_id = run.get("run_id")
        current_task._parent_run_id = run.get("parent_run_id")
        current_task._context_plan = None
        current_task._context_replay_region = ""
    parent = await _resolve_parent_run_context(int(run["conversation_id"]), run.get("parent_run_id"))
    harness, model = _resolve_agent_target(run, _agent_inheritance_context(conv, parent))
    cwd = run.get("project_dir") or (conv or {}).get("project_dir") or "."
    cwd = str(Path(cwd).resolve())
    scope = run.get("path_scope") or []
    if agent_access.sensitive(run) and harness != "local":
        raise ValueError("Sensitive children require local inference")
    image_request = run.get("image_request")
    if image_request is not None:
        if harness != "gemini":
            raise ValueError("Image jobs require the Antigravity harness")
        image_request = await asyncio.to_thread(
            image_generation.prepare_image_request, cwd, image_request,
        )
        if image_request != run["image_request"]:
            raise ValueError("Image request paths changed since admission")
    bounded_task = image_generation.image_task(image_request) if image_request else run["task"]
    scope_note = (
        "\n\nLoom child-run scope: work only within these relative paths: "
        + ", ".join(str(item) for item in scope)
        + ". Do not modify files outside this scope."
        if scope else ""
    )
    child_task = (
        "You are a Loom-managed durable child run. Complete only the bounded "
        "task below and return a concise result for the parent agent. Your "
        "parent may finish before you; Loom persists your result independently. "
        "Do not assume you received the parent conversation transcript.\n\n"
        f"Task:\n{bounded_task}{scope_note}"
    )
    if run.get("access") is not None:
        child_task += "\nDeclared access: " + json.dumps(run["access"])
        if agent_access.sensitive(run):
            child_task += "\nSensitive local task: keep raw information in this child. The human releases any parent handoff."
    child_task = add_canvas_notice(child_task, conv)
    if image_request is None:
        from child_context import prepare as prepare_child_context
        async def context_status(text):
            await emit("status", {"text": text})
        backend = "llama" if harness == "local" else None
        if harness == "goose":
            backend = goose_client.split_goose_model(model)[0]
        child_task = await prepare_child_context(
            run, child_task, model=model, harness=harness,
            backend=backend, status=context_status,
        )
    port = int(config.port)
    await emit(
        "agent_launch_info",
        {
            "requested_harness": run.get("requested_harness"),
            "requested_model": run.get("requested_model"),
            "effective_harness": harness,
            "effective_model": model,
            "profile": run.get("model_profile"),
            "cwd": cwd,
            "path_scope": scope,
            "context_plan": getattr(current_task, "_context_plan", None),
        },
    )
    await db.update_agent_run(
        run["run_id"], effective_harness=harness, effective_model=model
    )

    permission_scope = f"agent:{run['run_id']}"
    common = {
        "conv_id": int(run["conversation_id"]),
        "server_port": port,
        "permission_mode": "default",
        "permission_scope": permission_scope,
    }
    client_instance_id = run.get("client_instance_id")
    proc = None
    stream = None
    if harness == "codex":
        proc, stream = await codex_client.run_codex(
            child_task, cwd, model=model, **common,
            agent_run_id=run["run_id"],
            permission_request_handler=handle_cc_permission,
        )
    elif harness == "gemini":
        proc, stream = await gemini_client.run_gemini(
            child_task, cwd, model=model, **common,
            agent_run_id=run["run_id"],
        )
    elif harness == "goose":
        proc, stream = await goose_client.run_goose(
            child_task, conv_id=int(run["conversation_id"]), cwd=cwd, model=model, loom_port=port,
            permission_mode="default", permission_scope=permission_scope,
            agent_run_id=run["run_id"], parent_run_id=run.get("parent_run_id"),
            client_instance_id=client_instance_id,
            context_replay_history=getattr(current_task, "_context_replay_region", ""),
        )
    elif harness in {"claude", "local", "openrouter", "umans", "dream-claude"}:
        requested_model = model
        provider_model = model
        route_options = {}
        if harness == "local":
            # Local mode is Claude Code backed by llama-server, with the same
            # tools, contract, permission hook and nested-control MCP as its parent.
            route_options["use_llama"] = True
        elif harness == "openrouter":
            provider_model = openrouter_client.model_slug(model)
            route_options["use_openrouter"] = True
        elif harness == "umans":
            route_options["use_umans"] = True
        elif harness == "dream-claude":
            provider_model = _dream_claude_model_id(model)
            route_options["use_dream"] = True
        proc, stream = await claude_client.run_claude(
            child_task, cwd, model=provider_model, requested_model=requested_model,
            **common, **route_options,
            context_replay_history=getattr(current_task, "_context_replay_region", ""),
            extra_env={
                "LOOM_AGENT_RUN_ID": run["run_id"],
                "LOOM_PARENT_RUN_ID": run.get("parent_run_id"),
            },
            extra_disallowed_tools=(["Agent", "Task", "Bash", "PowerShell", "KillShell", "WebSearch", "WebFetch"]
                                    if agent_access.sensitive(run) else
                                    ["Agent", "Task", "Bash", "PowerShell", "Write", "Edit", "NotebookEdit"]
                                    if run.get("access") is not None and not run["access"]["write"] else None),
        )
    else:
        raise ValueError(f"Unsupported child harness: {harness}")

    full_text = ""
    final: dict = {}
    failure = ""
    saw_result = False
    image_evidence = image_generation.NativeImageEvidence()
    try:
        if proc is not None and getattr(proc, "pid", None):
            await db.update_agent_run(run["run_id"], pid=int(proc.pid))
        async for item in stream:
            item = dict(item or {})
            if image_request is not None:
                image_evidence.observe(item)
            kind = item.get("type") or "status"
            # ACP adapters also expose their native numeric approval request.
            # Only the Loom bridge owns the actionable UUID and resolution.
            if kind == "permission_request":
                continue
            failure = failure or _child_terminal_error(item)
            if kind in {"text_delta", "stream_chunk"}:
                full_text += item.get("text") or item.get("content") or ""
            if kind in {"session_info", "result"}:
                if item.get("session_id"):
                    await db.update_agent_run(
                        run["run_id"], provider_session_id=str(item["session_id"])
                    )
                effective = item.get("model") or item.get("effective_model")
                if effective:
                    await db.update_agent_run(run["run_id"], effective_model=str(effective))
            if kind == "result":
                final = item
                saw_result = True
                if not full_text:
                    full_text = str(item.get("result_text") or item.get("text") or "")
            await emit(kind, item)
    finally:
        async def finish_child():
            try:
                if proc is not None:
                    await _teardown_child_provider(proc, harness)
                    await db.update_agent_run(run["run_id"], pid=None)
            finally:
                await _resolve_pending_permissions_for_scope(
                    int(run["conversation_id"]), permission_scope,
                    message="Loom child run ended before a permission decision; this was not a user denial.",
                )
                _auto_approve_permissions.pop((int(run["conversation_id"]), permission_scope), None)

        # Repeated stop clicks cannot detach teardown or permission retirement.
        cleanup = asyncio.create_task(finish_child())
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError
    error = failure or ("Provider stream ended without a terminal result" if not saw_result else "")
    image_result = {}
    if image_request is not None and not error:
        if not image_evidence.succeeded:
            error = "No successful native generate_image tool call was observed"
        else:
            try:
                artifact = await asyncio.to_thread(
                    image_generation.image_artifact, cwd, image_request, int(run["conversation_id"]),
                )
                image_result = {"artifacts": [artifact]}
            except (ValueError, OSError) as exc:
                error = f"Image artifact validation failed: {exc}"
    return {
        "text": full_text,
        "result": final,
        "harness": harness,
        "model": model,
        "error": error,
        **image_result,
    }


async def _cancel_background_task(task: asyncio.Task | None, label: str) -> None:
    if not task:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print(f"[SHUTDOWN] {label} cleanup failed: {exc}")


def _spawn_background_task(
    coroutine,
    label: str,
    *,
    allow_during_shutdown: bool = False,
) -> asyncio.Task | None:
    """Own a background coroutine so shutdown can cancel and drain it."""
    if _shutdown_requested and not allow_during_shutdown:
        close = getattr(coroutine, "close", None)
        if close:
            close()
        return None
    task = asyncio.create_task(coroutine, name=f"loom:{label}")
    _background_tasks.add(task)

    def _finished(done: asyncio.Task) -> None:
        _background_tasks.discard(done)
        if done.cancelled():
            return
        try:
            error = done.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            print(f"[BACKGROUND] {label} failed: {error}")

    task.add_done_callback(_finished)
    return task


async def _drain_tracked_background_tasks_for_shutdown() -> None:
    """Cancel every Loom-owned auxiliary task before closing SQLite."""
    while True:
        tasks = [task for task in _background_tasks if not task.done()]
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _request_generation_shutdown() -> None:
    """Reject new launch admissions without waiting on an in-flight launch."""
    global _shutdown_requested
    _shutdown_requested = True


async def _close_generation_admission() -> None:
    """Reject new launches, then wait until earlier admission is registered."""
    _request_generation_shutdown()
    async with _generation_admission_lock:
        pass


async def _drain_active_generations_for_shutdown() -> None:
    """Interrupt active turns and wait for provider process-tree teardown."""
    tasks = list({
        task for task in _active_generations.values() if not task.done()
    })
    if not tasks:
        return
    print(f"[SHUTDOWN] Interrupting {len(tasks)} active generation(s)")
    for task in tasks:
        _request_generation_stop(task, "interrupt")
    done, pending = await asyncio.wait(tasks, timeout=20.0)
    if done:
        await asyncio.gather(*done, return_exceptions=True)
    if not pending:
        return

    # Adapter teardown normally owns its subprocess. If a task remains after
    # the grace period, force only the exact persisted provider process trees.
    rows = await db.list_active_generations()
    async def _reap_provider(row: dict) -> None:
        pid = row.get("pid")
        if pid and _pid_alive(int(pid)):
            _kill_pid(int(pid))
            await _wait_for_pid_exit(int(pid), timeout=8.0)

    # One bounded reap window for all providers; N providers must not turn an
    # 8-second phase into N*8 seconds and outrun the admin's outer deadline.
    await asyncio.gather(*(_reap_provider(row) for row in rows))
    for task in pending:
        task.cancel()
    done_after_kill, pending = await asyncio.wait(pending, timeout=5.0)
    if done_after_kill:
        await asyncio.gather(*done_after_kill, return_exceptions=True)
    if pending:
        print(
            f"[SHUTDOWN] {len(pending)} generation task(s) resisted teardown; "
            "database reopen is now disabled"
        )


@asynccontextmanager
async def lifespan(app):
    global _cron_scheduler_task
    global _queued_generation_scheduler_task
    global _agent_supervisor
    global _shutdown_requested
    _shutdown_requested = False
    await db.init_db()
    await _cron_init_db()
    from usage_activity import activity
    _spawn_background_task(activity.run(), "usage activity snapshots")
    # Reap orphan CC/LLM subprocesses from prior server instances.
    await _reap_orphan_generations()
    # Clean up stale draft messages (empty assistant msgs older than 30 min)
    await _cleanup_stale_drafts()
    # Release only leases whose provider rows were successfully reaped. A
    # process tree that resisted termination keeps its row and serial lane.
    await db.release_stale_generation_leases()
    # Any launch claim belonged to the previous process. The durable queued
    # message remains canonical and will be picked up by the new scheduler.
    await db.reset_launching_queued_generations()
    # Re-broadcast pending permission requests from DB (server restart)
    await _reload_pending_permissions()
    # Ensure Llama Server is reachable (warns if not)
    _spawn_background_task(_ensure_llama(), "ensure llama")
    # Warm the local-model caches once Llama Server is up so the settings panel
    # opens instantly. Runs as a background task so we don't block startup
    # while waiting on a slow Llama Server probe.
    async def _warm_model_caches():
        await asyncio.sleep(2)  # let _ensure_llama finish first
        try:
            await _refresh_local_models_cache()
            await _refresh_vision_models_cache()
            await _refresh_all_engines_cache()
            print(f"[CACHE] Warmed model caches: {len((_LOCAL_MODELS_CACHE or {}).get('models', []))} models, {len((_VISION_MODELS_CACHE or {}).get('models', []))} vision-capable, {len((_ALL_ENGINES_CACHE or {}).get('models', []))} cross-engine")
        except Exception as e:
            print(f"[CACHE] Initial warm failed (will fetch on demand): {e}")
    _spawn_background_task(_warm_model_caches(), "warm model caches")
    # Sync model IDs from the running Llama Server into config
    _spawn_background_task(_update_model_ids_on_startup(), "sync model ids")

    async def _refresh_remote_models_on_startup():
        await asyncio.sleep(3)
        try:
            print("[STARTUP] Refreshing Anthropic models...")
            await api_cc_models_refresh()
            print("[STARTUP] Anthropic models refreshed successfully")
        except Exception as e:
            print(f"[STARTUP] Anthropic model refresh skipped/failed: {e}")

        try:
            print("[STARTUP] Refreshing Codex models from cache...")
            local_models = load_local_codex_models()
            if local_models:
                for group in CC_MODELS:
                    if group["group"].startswith("ChatGPT Codex"):
                        group["models"] = local_models
                        print(f"[STARTUP] Codex models updated from cache: {len(local_models)} models loaded")
                        break
        except Exception as e:
            print(f"[STARTUP] Codex model refresh failed: {e}")

        try:
            print("[STARTUP] Refreshing Gemini models from cache...")
            local_gemini = load_local_gemini_models()
            if local_gemini:
                for group in CC_MODELS:
                    if group["group"].startswith("Antigravity"):
                        group["models"] = local_gemini
                        print(f"[STARTUP] Gemini models updated from cache: {len(local_gemini)} models loaded")
                        break
        except Exception as e:
            print(f"[STARTUP] Gemini model refresh failed: {e}")

    _spawn_background_task(
        _refresh_remote_models_on_startup(), "refresh remote models"
    )
    _cron_scheduler_task = asyncio.create_task(_cron_scheduler_loop())
    _queued_generation_scheduler_task = asyncio.create_task(
        _queued_generation_scheduler_loop()
    )
    _agent_supervisor = AgentSupervisor(
        launcher=_launch_agent_run,
        event_sink=_publish_agent_event,
        batch_sink=_publish_agent_events,
        target_resolver=_resolve_agent_target,
    )
    await _agent_supervisor.start()
    try:
        yield
    finally:
        # Share an admission barrier with _launch_generation_task. Once this
        # lock is released every launch that began earlier is registered in
        # _active_generations, and every later launch sees shutdown=True.
        await _close_generation_admission()
        # Stop schedulers first so teardown cannot claim queued work or create
        # another generation while the existing providers are being drained.
        await _cancel_background_task(_cron_scheduler_task, "cron scheduler")
        await _cancel_background_task(
            _queued_generation_scheduler_task, "generation queue scheduler"
        )
        if _agent_supervisor:
            try:
                await _agent_supervisor.stop()
            except Exception as exc:
                print(f"[SHUTDOWN] Agent supervisor cleanup failed: {exc}")
            _agent_supervisor = None
        await _drain_tracked_background_tasks_for_shutdown()
        try:
            await _drain_active_generations_for_shutdown()
        except Exception as exc:
            print(f"[SHUTDOWN] Generation drain failed: {exc}")
        # Generation finalizers can schedule cancellation-safe lease releases;
        # drain that final wave before disabling database access.
        await _drain_tracked_background_tasks_for_shutdown()
        # Nothing may open/reopen the process-global SQLite connection after
        # this point. The close and WAL checkpoint are the final lifecycle act.
        db.begin_shutdown()
        try:
            await db.close_db()
        except Exception as exc:
            print(f"[SHUTDOWN] Database close failed: {exc}")


def _pid_alive(pid: int) -> bool:
    """Cross-platform PID liveness check. Returns False on unknown error."""
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                exit_code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
                return bool(ok) and exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(h)
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def _kill_pid(pid: int) -> bool:
    """Force-kill a process. Returns True if the kill was dispatched."""
    if not pid or pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import subprocess
            # /T kills the child tree too — CC spawns sub-procs (e.g. bash for tools)
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=5,
            )
        else:
            import signal as _signal
            os.kill(pid, _signal.SIGKILL)
        return True
    except Exception as e:
        print(f"[REAP] Failed to kill pid={pid}: {e}")
        return False


async def _wait_for_pid_exit(pid: int | None, timeout: float = 10.0) -> bool:
    """Wait until an exact provider PID is gone without blocking the event loop."""
    if not pid:
        return True
    deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
    while _pid_alive(pid):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


async def _reap_orphan_generations():
    """On server startup, any row in active_generations came from a prior
    server instance that didn't finalize before shutdown. Kill the subprocess
    if it's still alive, mark the draft errored, and delete the tracking row."""
    rows = await db.list_active_generations()
    if not rows:
        print("[REAP] No orphan generations to check")
        return
    print(f"[REAP] Found {len(rows)} tracked generation(s) from prior run")
    for row in rows:
        pid = row.get("pid")
        draft_msg_id = row.get("draft_msg_id")
        conv_id = row.get("conv_id")
        alive = _pid_alive(pid) if pid else False
        killed = False
        if alive:
            killed = _kill_pid(pid)
            print(f"[REAP] conv={conv_id} draft={draft_msg_id} pid={pid} alive — killed={killed}")
        else:
            print(f"[REAP] conv={conv_id} draft={draft_msg_id} pid={pid} already dead")
        if alive and killed:
            alive = not await _wait_for_pid_exit(pid, timeout=8.0)
        if alive:
            # Fail closed. Keep both the active-generation row and its
            # database lease so the scheduler cannot launch another provider
            # beside a process tree that resisted startup reaping.
            print(
                f"[REAP] conv={conv_id} draft={draft_msg_id} pid={pid} "
                "still alive; retaining serial lane"
            )
            continue
        # Mark draft as errored if it's still an empty draft (don't clobber
        # finalized content).
        try:
            m = await db.get_message(draft_msg_id)
            if m and (m.get("role") == "assistant") and not (m.get("content") or "").strip():
                await db.update_message_content(
                    draft_msg_id,
                    content="[Error: generation orphaned by server restart]",
                )
        except Exception as e:
            print(f"[REAP] Could not update draft {draft_msg_id}: {e}")
        await db.unregister_active_generation(draft_msg_id)
    print(f"[REAP] Reaped {len(rows)} orphan generation(s)")


async def _cleanup_stale_drafts():
    """Retain and repair empty assistant draft messages older than 30 minutes on startup.

    Does not delete branches, queued descendants, or parent links.
    """
    import time

    cutoff = time.time() - 1800  # 30 minutes
    conn = await db.get_db()
    rows = await conn.execute_fetchall(
        "SELECT id FROM messages WHERE role='assistant' AND (content IS NULL OR content='') AND content_blocks IS NULL AND created_at < ?",
        (cutoff,),
    )
    if rows:
        ids = [r["id"] for r in rows]
        print(f"[STARTUP] Repairing {len(ids)} stale draft(s): {ids}")
        for msg_id in ids:
            await db.update_message_content(
                msg_id,
                content="[Generation interrupted by server restart]",
                turn_lifecycle_state="interrupted",
            )
    await conn.close()


async def _reload_pending_permissions():
    """Keep pending permissions durable; route them on the owning socket.

    This used to broadcast one permission from every conversation to every
    connected WebSocket and then delete the durable row.  Reconnect is now a
    read of the row, filtered by the explicit run subscription.
    """
    import time

    # Clean up very old permissions (1 hour+) before reloading
    await db.delete_old_pending_permissions(time.time() - 3600)
    print(f"[STARTUP] Cleaned up permissions older than 1 hour")

    # Keep rows in SQLite.  A reconnecting socket will request its own
    # conversation's pending rows through ``_send_pending_permissions``.
    conn = await db.get_db()
    rows = await conn.execute_fetchall(
        "SELECT request_id, conv_id, run_id, parent_id, draft_msg_id, client_instance_id, permission_scope "
        "FROM pending_permissions ORDER BY started_at"
    )
    for row in rows:
        print(
            f"[STARTUP] Preserved pending permission: {row['request_id']} "
            f"for conv={row['conv_id']} run={row['run_id'] or row['permission_scope'] or 'conversation'}"
        )
    await conn.close()


async def _ensure_llama():
    """Check if Llama Server is reachable; warn if not."""
    try:
        status = await health_check()
        if status.get("status") == "ok":
            print(
                f"[STARTUP] Llama Server already running — {len(status.get('models', []))} model(s) available"
            )
            return
    except Exception:
        pass

    print("[STARTUP] Llama Server not reachable — local turns will fail until it starts")



def _cron_now() -> float:
    return time.time()


def _cron_iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _cron_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(CRON_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _cron_init_db_sync() -> None:
    with _cron_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cron_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conv_id INTEGER NOT NULL,
                workspace TEXT NOT NULL,
                script TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                every_seconds INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                archived_at REAL,
                last_run_at REAL,
                last_finished_at REAL,
                next_run_at REAL NOT NULL,
                last_status TEXT,
                last_exit_code INTEGER,
                last_output TEXT,
                last_error TEXT,
                run_count INTEGER NOT NULL DEFAULT 0,
                skip_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cron_active_due ON cron_jobs(archived, enabled, next_run_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cron_conv ON cron_jobs(conv_id, archived)")
        conn.commit()


async def _cron_init_db() -> None:
    await asyncio.to_thread(_cron_init_db_sync)


def _cron_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("created_at", "updated_at", "archived_at", "last_run_at", "last_finished_at", "next_run_at"):
        d[f"{key}_display"] = _cron_iso(d.get(key))
    return d


def _cron_list_jobs_sync(include_archived: bool = False, conv_id: int | None = None) -> list[dict]:
    where = []
    args: list = []
    if not include_archived:
        where.append("archived = 0")
    if conv_id is not None:
        where.append("conv_id = ?")
        args.append(conv_id)
    sql = "SELECT * FROM cron_jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY archived ASC, enabled DESC, next_run_at ASC, id DESC"
    with _cron_connect() as conn:
        return [_cron_row_to_dict(r) for r in conn.execute(sql, args).fetchall()]


async def _cron_list_jobs(include_archived: bool = False, conv_id: int | None = None) -> list[dict]:
    return await asyncio.to_thread(_cron_list_jobs_sync, include_archived, conv_id)


def _cron_validate_script(script: str, workspace: str) -> tuple[Path, str]:
    if not isinstance(script, str) or not script.strip():
        raise HTTPException(status_code=400, detail="script is required")
    if "\x00" in script or script.startswith(("~", "/", "\\")) or ":" in Path(script).parts[0]:
        raise HTTPException(status_code=400, detail="script must be a relative path inside the conversation workspace")
    rel = Path(script)
    if any(part in ("", ".", "..") for part in rel.parts):
        raise HTTPException(status_code=400, detail="script path may not contain traversal segments")
    workspace_path = Path(workspace).resolve()
    target = (workspace_path / rel).resolve()
    try:
        target.relative_to(workspace_path)
    except ValueError:
        raise HTTPException(status_code=400, detail="script must stay inside the conversation workspace")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="script file does not exist")
    if target.suffix.lower() not in CRON_ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"script suffix must be one of {sorted(CRON_ALLOWED_SUFFIXES)}")
    try:
        text = target.read_text(encoding="utf-8", errors="ignore").lower()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"could not read script for safety scan: {e}")
    for pattern in CRON_DENY_PATTERNS:
        if pattern in text:
            raise HTTPException(status_code=400, detail=f"script contains rejected command pattern: {pattern}")
    return target, str(rel).replace("\\", "/")


def _cron_interval_from_payload(data: dict) -> int:
    seconds = data.get("every_seconds")
    if seconds is None and data.get("every_minutes") is not None:
        seconds = int(data["every_minutes"]) * 60
    if seconds is None and data.get("every_hours") is not None:
        seconds = int(data["every_hours"]) * 3600
    try:
        seconds = int(seconds)
    except Exception:
        raise HTTPException(status_code=400, detail="provide every_seconds, every_minutes, or every_hours")
    if seconds < CRON_MIN_INTERVAL_SECONDS:
        raise HTTPException(status_code=400, detail=f"interval must be at least {CRON_MIN_INTERVAL_SECONDS} seconds")
    return seconds


def _cron_create_job_sync(conv_id: int, workspace: str, script: str, description: str, every_seconds: int, enabled: bool) -> dict:
    now = _cron_now()
    next_run = now + every_seconds
    with _cron_connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO cron_jobs
                (conv_id, workspace, script, description, every_seconds, enabled, created_at, updated_at, next_run_at, last_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (conv_id, workspace, script, description, every_seconds, 1 if enabled else 0, now, now, next_run, "created"),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM cron_jobs WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _cron_row_to_dict(row)


async def _cron_create_job(conv_id: int, workspace: str, script: str, description: str, every_seconds: int, enabled: bool) -> dict:
    return await asyncio.to_thread(_cron_create_job_sync, conv_id, workspace, script, description, every_seconds, enabled)


def _cron_archive_job_sync(job_id: int) -> bool:
    now = _cron_now()
    with _cron_connect() as conn:
        cur = conn.execute(
            "UPDATE cron_jobs SET enabled = 0, archived = 1, archived_at = ?, updated_at = ?, last_status = ? WHERE id = ? AND archived = 0",
            (now, now, "archived", job_id),
        )
        conn.commit()
        return cur.rowcount > 0


async def _cron_archive_job(job_id: int) -> bool:
    return await asyncio.to_thread(_cron_archive_job_sync, job_id)


def _cron_set_enabled_sync(job_id: int, enabled: bool) -> bool:
    now = _cron_now()
    with _cron_connect() as conn:
        cur = conn.execute(
            "UPDATE cron_jobs SET enabled = ?, updated_at = ?, last_status = ? WHERE id = ? AND archived = 0",
            (1 if enabled else 0, now, "enabled" if enabled else "disabled", job_id),
        )
        conn.commit()
        return cur.rowcount > 0


async def _cron_set_enabled(job_id: int, enabled: bool) -> bool:
    return await asyncio.to_thread(_cron_set_enabled_sync, job_id, enabled)


def _cron_due_jobs_sync(now: float) -> list[dict]:
    with _cron_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM cron_jobs WHERE archived = 0 AND enabled = 1 AND next_run_at <= ? ORDER BY next_run_at ASC",
            (now,),
        ).fetchall()
        return [_cron_row_to_dict(r) for r in rows]


def _cron_update_after_run_sync(job_id: int, started: float, exit_code: int | None, output: str, error: str, every_seconds: int) -> None:
    now = _cron_now()
    status = "ok" if exit_code == 0 else "error"
    with _cron_connect() as conn:
        conn.execute(
            """
            UPDATE cron_jobs
            SET last_run_at = ?, last_finished_at = ?, next_run_at = ?, last_status = ?,
                last_exit_code = ?, last_output = ?, last_error = ?, run_count = run_count + 1, updated_at = ?
            WHERE id = ?
            """,
            (started, now, now + every_seconds, status, exit_code, output[-4000:], error[-4000:], now, job_id),
        )
        conn.commit()


def _cron_mark_skipped_sync(job_id: int, every_seconds: int) -> None:
    now = _cron_now()
    with _cron_connect() as conn:
        conn.execute(
            "UPDATE cron_jobs SET skip_count = skip_count + 1, next_run_at = ?, last_status = ?, updated_at = ? WHERE id = ?",
            (now + every_seconds, "skipped_overlap", now, job_id),
        )
        conn.commit()


def _cron_command(script_path: Path) -> list[str]:
    suffix = script_path.suffix.lower()
    if suffix == ".py":
        return [sys.executable, str(script_path)]
    if suffix == ".ps1":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)]
    if suffix == ".js":
        return ["node", str(script_path)]
    raise RuntimeError(f"unsupported script suffix: {suffix}")


async def _cron_run_job(job: dict) -> None:
    job_id = job["id"]
    if job_id in _cron_running:
        await asyncio.to_thread(_cron_mark_skipped_sync, job_id, int(job["every_seconds"]))
        return
    _cron_running.add(job_id)
    started = _cron_now()
    try:
        script_path, _ = _cron_validate_script(job["script"], job["workspace"])
        proc = await asyncio.create_subprocess_exec(
            *_cron_command(script_path),
            cwd=job["workspace"],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=min(max(int(job["every_seconds"]) - 5, 30), 3600))
            output = stdout.decode("utf-8", errors="replace")
            error = stderr.decode("utf-8", errors="replace")
            exit_code = proc.returncode
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            output = ""
            error = "cron job timed out"
            exit_code = -1
    except Exception as e:
        output = ""
        error = str(e)
        exit_code = -1
    finally:
        _cron_running.discard(job_id)
    await asyncio.to_thread(_cron_update_after_run_sync, job_id, started, exit_code, output, error, int(job["every_seconds"]))


async def _cron_scheduler_loop() -> None:
    while True:
        try:
            due = await asyncio.to_thread(_cron_due_jobs_sync, _cron_now())
            for job in due:
                _spawn_background_task(_cron_run_job(job), f"cron job {job['id']}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[CRON] Scheduler error: {e}")
        await asyncio.sleep(5)


CRON_HELP = {
    "purpose": "Create simple persisted interval jobs for scripts inside a Loom conversation workspace.",
    "create_endpoint": "POST /api/conversations/{conv_id}/cronjobs",
    "list_endpoint": "GET /api/cron/jobs?include_archived=false",
    "archive_endpoint": "DELETE /api/cron/jobs/{job_id}",
    "example": {
        "script": "scripts/pull_bluesky.py",
        "description": "Pull Bluesky profile data and update the conversation workspace cache.",
        "every_seconds": 3600,
        "enabled": True,
    },
    "rules": [
        "script must be a relative path inside the conversation workspace",
        f"script suffix must be one of {sorted(CRON_ALLOWED_SUFFIXES)}",
        f"minimum interval is {CRON_MIN_INTERVAL_SECONDS} seconds",
        "jobs skip overlapping runs by default",
        "delete archives and disables the job; archived jobs remain visible to admin",
        "scripts inherit the Loom server environment, so API keys should come from .env or process env",
    ],
    "rejected_patterns": list(CRON_DENY_PATTERNS),
}


try:
    _STARTUP_SOURCE_IDENTITY = acceptance_source_identity()
except Exception as _identity_error:
    print(f"[ACCEPTANCE] Build identity unavailable: {_identity_error}")
    _STARTUP_SOURCE_IDENTITY = None


app = FastAPI(title="EX umbra Omnia", lifespan=lifespan)

# Loopback/Tailscale gate plus remote browser authentication by default. Agent
# processes (hooks, MCP servers, child runs) call only these routes, from
# loopback, and never carry the browser cookie. They request; they don't approve.
loom_auth.install(
    app,
    public_prefixes=("/static/",),
    agent_routes=(
        r"/api/cc-permission",
        r"/api/cc-tool-start",
        r"/api/cc-tool-result",
        r"/api/local/describe-image",
        r"/api/conversations/\d+/agent-runs",
        r"/api/conversations/\d+/chat-items",
        r"/api/agent-runs/[^/]+(/cancel|/resume|/context-retrieval)?",
        r"/api/conversations/\d+/context-retrieval",
        r"/api/search-space/(query|retrieve)",
        r"/api/history/(search_conversations|read_conversation_context|get_conversation_details)",
        r"/api/state-schemas",
        r"/api/conversations/\d+/state",
        r"/api/state/[^/]+",
    ),
)

# --- Graceful shutdown endpoint ---
_server_ref: list = []  # holds uvicorn.Server for shutdown


@app.post("/shutdown")
async def shutdown():
    """Ask Uvicorn to exit without blocking the admin's HTTP timeout.

    New admissions are rejected synchronously. Lifespan teardown waits behind
    any launch already holding the admission lock before it drains providers.
    """
    _request_generation_shutdown()
    if _server_ref:
        _server_ref[0].should_exit = True
        return JSONResponse({"status": "shutting down"})
    # Fallback: signal the process
    import signal

    os.kill(os.getpid(), signal.SIGINT)
    return JSONResponse({"status": "shutting down (signal)"})


@app.get("/api/generations")
async def api_list_generations():
    """List currently tracked generations (for admin dashboard)."""
    rows = await db.list_active_generations()
    # Annotate each with liveness + in-memory status
    for r in rows:
        r["pid_alive"] = _pid_alive(r.get("pid")) if r.get("pid") else False
        draft_id = r.get("draft_msg_id")
        r["in_memory"] = any(
            not t.done() and (
                _generation_snapshots.get(k, {}).get("draft_msg_id") == draft_id
                or getattr(t, "_draft_msg_id", None) == draft_id
                or (draft_id is None and k[0] == r.get("conv_id"))
            )
            for k, t in _active_generations.items()
        )
    return rows


@app.get("/api/build-identity")
async def api_build_identity():
    """Identity captured when this Main process loaded its source."""
    return JSONResponse(
        {"source": _STARTUP_SOURCE_IDENTITY},
        headers={"Cache-Control": "no-store"},
    )


def _require_admin_relay(request: Request) -> None:
    # The admin UI is a privileged surface. Browser requests go through the
    # admin server, which relays with the machine secret; a constant header
    # would let any local process (including an agent's shell) approve itself.
    if (not request.client or not loom_auth.is_loopback(request.client.host)
            or request.headers.get("origin")
            # Checked here, not via middleware state, so the gate holds even
            # if the route is mounted without the access middleware.
            or loom_auth.credential(request.scope) != "relay"):
        raise HTTPException(403, "Admin loopback relay required")


@app.get("/api/admin/supervision")
async def api_admin_supervision(request: Request):
    """Small metadata projection; no transcripts, cursor writes or PID scans."""
    _require_admin_relay(request)
    rows = await db.list_active_generations()
    runs = {r["draft_msg_id"]: {
        "conv_id": r["conv_id"], "draft_msg_id": r["draft_msg_id"],
        "branch_id": r["draft_msg_id"], "run_id": None, "kind": "generation",
        "mode": r.get("mode"), "project_dir": r.get("project_dir"),
        "started_at": r.get("started_at"), "pid": r.get("pid"),
        "status": "tracked", "loom_thread_id": r.get("loom_thread_id"),
    } for r in rows}
    for key, task in list(_active_generations.items()):
        if task.done():
            continue
        snap = _generation_snapshots.get(key) or {}
        draft = snap.get("draft_msg_id")
        attestation = snap.get("model_attestation") or {}
        item = runs.setdefault(draft or f"gen:{key[2]}", {})
        item.update({"conv_id": key[0], "run_id": f"gen:{key[2]}",
                     "kind": "generation", "draft_msg_id": draft,
                     "branch_id": draft or snap.get("parent_id") or key[1],
                     "mode": attestation.get("harness") or item.get("mode"),
                     "model": attestation.get("effective_model") or attestation.get("launch_model"),
                     "status": "cancelling" if getattr(task, "_stop_disposition", None)
                     else ("running" if draft else "preparing")})
        if snap.get("started_at"):
            item.setdefault("started_at", snap["started_at"])
    for child in await db.list_agent_runs(statuses=["queued", "launching", "running", "cancelling"]):
        run_id = f"agent:{child['run_id']}"
        runs[run_id] = {"conv_id": child["conversation_id"], "run_id": run_id,
                        "kind": "child", "branch_id": child.get("parent_message_id"),
                        "mode": child.get("effective_harness") or child.get("requested_harness"),
                        "model": child.get("effective_model") or child.get("requested_model"),
                        "status": child["status"], "started_at": child.get("started_at"),
                        "project_dir": child.get("project_dir"), "pid": child.get("pid")}
    permissions = []
    public_fields = ("conv_id", "run_id", "parent_id", "draft_msg_id", "permission_scope",
                     "tool_name", "tool_input", "input_summary", "source", "risk_level",
                     "risk_reason", "rule_preview", "supports_allow_all", "started_at")
    for request_id, pending in list(_pending_hook_permissions.items()):
        if pending.get("response") is not None:
            continue
        item = {name: pending.get(name) for name in public_fields}
        item["request_id"] = request_id
        owner = next((r for r in runs.values() if r.get("run_id") == item.get("run_id")
                      and r["conv_id"] == item["conv_id"]), None) if item.get("run_id") else None
        item["branch_id"] = item.get("draft_msg_id") or item.get("parent_id") or (owner or {}).get("branch_id")
        item["profile"] = (pending.get("policy") or {}).get("profile")
        if owner:
            if owner["status"] != "cancelling":
                owner["status"] = "awaiting_permission"
            item["project_dir"] = owner.get("project_dir")
            item["mode"] = owner.get("mode")
            item["model"] = owner.get("model")
        permissions.append(item)
    conversations = {}
    for item in [*runs.values(), *permissions]:
        cid = item["conv_id"]
        if cid not in conversations:
            conversations[cid] = await db.get_conversation(cid) or {}
        conv = conversations[cid]
        item["title"] = conv.get("title") or f"Conversation #{cid}"
        item["project_dir"] = item.get("project_dir") or conv.get("project_dir") or ""
        item["mode"] = item.get("mode") or item.get("source") or conv.get("mode")
        # A model selected for the NEXT turn is not evidence about this run.
        if not item.get("model") and item.get("status") == "preparing" and conv.get("cc_model"):
            item["model"] = "Selected: " + conv["cc_model"]
    # A final draft can be persisted before provider teardown finishes. Keep
    # such turns out of the inbox until their owning task has actually ended.
    live_drafts = {item.get("draft_msg_id") for item in runs.values() if item.get("draft_msg_id")}
    try:
        landed = await db.list_landed_generations()
        # Recheck after the read's await boundary as new tasks may have started.
        live_drafts.update(getattr(task, "_draft_msg_id", None)
                           for task in _active_generations.values() if not task.done())
        live_drafts.discard(None)  # children have no draft of their own
        landed = [item for item in landed if item.get("draft_msg_id") not in live_drafts]
    except Exception as exc:
        # Optional history must never take working approvals/active tracking down.
        print(f"[ADMIN] Completion history unavailable ({type(exc).__name__})")
        landed = None
    return JSONResponse(_scrub_surrogates({"generations": list(runs.values()), "permissions": permissions,
                                          "landed": landed, "landed_limit": 100}),
                        headers={"Cache-Control": "no-store"})


@app.post("/api/admin/permissions/{request_id}")
async def api_admin_permission_response(request_id: str, request: Request, data: dict = Body(...)):
    _require_admin_relay(request)
    if type(data.get("allow")) is not bool or type(data.get("always", False)) is not bool:
        raise HTTPException(422, "Approval choices must be booleans")
    pending = _pending_hook_permissions.get(request_id)
    if not pending or pending.get("response") is not None:
        raise HTTPException(409, "Request already resolved or no longer active")
    if (data.get("conv_id") != pending.get("conv_id")
            or data.get("run_id") != pending.get("run_id")):
        raise HTTPException(409, "Permission owner changed; refresh the request")
    if data.get("always") and not pending.get("supports_allow_all"):
        raise HTTPException(422, "This request supports one-time approval only")
    if not await _answer_pending_permission(request_id, data, channel="admin"):
        raise HTTPException(409, "Request already resolved or no longer active")
    return {"resolved": True, "allowed": data["allow"]}


@app.post("/api/conversations/{conv_id}/context-retrieval")
async def api_context_retrieval(conv_id: int, data: dict = Body(...)):
    from context_checkpoints import retrieve
    try:
        return await retrieve(data.get("token"), conv_id, message_id=data.get("message_id"),
                              offset=data.get("offset", 0), limit=data.get("limit", 8000))
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/agent-runs/{run_id}/context-retrieval")
async def api_child_context_retrieval(run_id: str, data: dict = Body(...)):
    from child_context import retrieve
    try:
        return await retrieve(data.get("token"), run_id, message_id=data.get("message_id"),
                              offset=data.get("offset", 0), limit=data.get("limit", 8000))
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/conversations/{conv_id}/agent-runs")
async def api_spawn_agent_run(conv_id: int, data: dict = Body(...)):
    """Queue a Loom-managed child run without creating a tree message."""
    global _agent_supervisor
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if _agent_supervisor is None:
        _agent_supervisor = AgentSupervisor(
            launcher=_launch_agent_run,
            event_sink=_publish_agent_event,
            batch_sink=_publish_agent_events,
            target_resolver=_resolve_agent_target,
        )
        await _agent_supervisor.start()
    parent_run_id = str(data.get("parent_run_id") or "").strip() or None
    if parent_run_id and parent_run_id.startswith("agent:"):
        parent_run_id = parent_run_id.split(":", 1)[1]
    inferred_parent_message_id = None
    parent = None
    if parent_run_id:
        parent = await _resolve_parent_run_context(conv_id, parent_run_id)
        if not parent:
            synthetic_generation = str(parent_run_id).startswith("gen:")
            if not synthetic_generation:
                raise HTTPException(status_code=400, detail="parent_run_id does not belong to this conversation")
        if parent:
            inferred_parent_message_id = parent.get("parent_message_id")
    project_dir = data.get("project_dir") or (parent or {}).get("project_dir") or conv.get("project_dir") or "."
    try:
        access = agent_access.validate(project_dir, data.get("access"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if parent and agent_access.sensitive(parent):
        if access is None:
            access = parent["access"]
        if not access.get("sensitive"):
            raise HTTPException(status_code=400, detail="Sensitive descendants cannot broaden disclosure")
    path_scope = data.get("path_scope") or []
    if not isinstance(path_scope, list) or any(not isinstance(item, str) for item in path_scope):
        raise HTTPException(status_code=400, detail="path_scope must be a list of paths")
    workspace_root = Path(project_dir).resolve()
    image_request = data.get("image_request")
    if image_request is not None:
        try:
            image_request = await asyncio.to_thread(
                image_generation.prepare_image_request, workspace_root, image_request,
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        data = {
            **data, "harness": "agy", "profile": None,
            "model": data.get("model") or image_generation.DEFAULT_AGENT_MODEL,
            "task": image_generation.image_task(image_request),
        }
        path_scope = image_generation.image_scope(image_request)
    parent_message_id = data.get("parent_message_id") or inferred_parent_message_id
    if parent_message_id is not None:
        parent_message = await db.get_message(parent_message_id)
        if not parent_message or int(parent_message.get("conversation_id")) != int(conv_id):
            raise HTTPException(status_code=400, detail="parent_message_id does not belong to this conversation")
    normalized_scope = []
    for raw_path in path_scope:
        candidate = Path(raw_path)
        resolved = (candidate if candidate.is_absolute() else workspace_root / candidate).resolve()
        if resolved != workspace_root and workspace_root not in resolved.parents:
            raise HTTPException(status_code=400, detail=f"path_scope escapes project_dir: {raw_path}")
        normalized_scope.append(str(resolved.relative_to(workspace_root)) if resolved != workspace_root else ".")
    try:
        harness, model = _resolve_agent_target(
            {"requested_harness": data.get("harness"), "requested_model": data.get("model"),
             "model_profile": data.get("profile")},
            _agent_inheritance_context(conv, parent),
        )
        if access and access.get("sensitive") and harness != "local":
            raise ValueError("Sensitive children require profile=local")
        run = await _agent_supervisor.spawn(
            conv_id,
            str(data.get("task") or ""),
            parent_run_id=parent_run_id,
            parent_message_id=parent_message_id,
            harness=harness,
            model=model,
            profile=data.get("profile"),
            project_dir=str(project_dir),
            path_scope=normalized_scope,
            client_instance_id=str(data.get("client_instance_id") or "").strip() or None,
            defer_start=True,
            image_request=image_request,
            access=access,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    client_id = str(run.get("client_instance_id") or "").strip()
    if client_id:
        websocket = _find_client_websocket(conv_id, client_id)
        if websocket:
            _event_router.subscribe(websocket, run["run_id"])
    queued_event = await db.append_agent_event(
        run["run_id"], "agent_queued", {"task": run["task"], "status": "queued"}
    )
    await _publish_agent_event(run, queued_event)
    _agent_supervisor.wake()
    return run


@app.get("/api/conversations/{conv_id}/agent-runs")
async def api_list_agent_runs(conv_id: int, parent_run_id: str | None = None, request: Request = None):
    if not await db.get_conversation(conv_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return [agent_access.public_run(run, human=bool(request and loom_auth.credential(request.scope)))
            for run in await db.list_agent_runs(conv_id, parent_run_id=parent_run_id)]


@app.get("/api/agent-runs/{run_id}")
async def api_get_agent_run(run_id: str, request: Request = None):
    run = await db.get_agent_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if not agent_access.sensitive(run):
        run['chat_items'] = await chat_presentations.run_items(f'agent:{run_id}')
    return agent_access.public_run(run, human=bool(request and loom_auth.credential(request.scope)))


@app.get("/api/agent-runs/{run_id}/events")
async def api_list_agent_events(run_id: str, after_sequence: int = 0, limit: int = 500, request: Request = None):
    run = await db.get_agent_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if agent_access.sensitive(run) and not (request and loom_auth.credential(request.scope)):
        raise HTTPException(status_code=403, detail="Sensitive logs require a paired browser")
    return await db.list_agent_events(run_id, after_sequence=after_sequence, limit=limit)


@app.post("/api/agent-runs/{run_id}/release")
async def api_release_agent_result(run_id: str, data: dict, request: Request):
    if loom_auth.credential(request.scope) != "browser":
        raise HTTPException(status_code=403, detail="Only a paired human browser can release sensitive output")
    run = await db.get_agent_run(run_id)
    if not run or not agent_access.sensitive(run) or run["status"] != "completed":
        raise HTTPException(status_code=409, detail="Release requires a completed sensitive child")
    text = data.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 32000:
        raise HTTPException(status_code=400, detail="Release must contain 1–32000 characters")
    await db.update_agent_run(run_id, released_result=text)
    return {"released": True}


@app.post("/api/agent-runs/{run_id}/cancel")
async def api_cancel_agent_run(run_id: str, request: Request = None):
    global _agent_supervisor
    if not await db.get_agent_run(run_id):
        raise HTTPException(status_code=404, detail="Agent run not found")
    if _agent_supervisor is not None:
        run = await _agent_supervisor.cancel(run_id)
    else:
        run = await db.request_agent_cancel(run_id)
    return agent_access.public_run(run, human=bool(request and loom_auth.credential(request.scope)))


@app.post("/api/agent-runs/{run_id}/resume")
async def api_resume_agent_run(run_id: str, request: Request = None):
    """Explicit recovery from owned evidence, only after verified provider exit."""
    global _agent_supervisor
    existing = await db.get_agent_run(run_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if not db.agent_run_can_resume(existing):
        raise HTTPException(status_code=409, detail="Child must be stopped and paused/failed, without cancellation, before recovery")
    if _agent_supervisor is None:
        _agent_supervisor = AgentSupervisor(
            launcher=_launch_agent_run,
            event_sink=_publish_agent_event,
            batch_sink=_publish_agent_events,
            target_resolver=_resolve_agent_target,
        )
        await _agent_supervisor.start()
    try:
        run = await _agent_supervisor.resume(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return agent_access.public_run(run, human=bool(request and loom_auth.credential(request.scope)))


@app.post("/api/generations/{draft_msg_id}/kill")
async def api_kill_generation(draft_msg_id: int):
    """Cancel exactly one generation and retain its lane until provider exit."""
    rows = await db.list_active_generations()
    row = next((r for r in rows if r.get("draft_msg_id") == draft_msg_id), None)
    if not row:
        return JSONResponse({"error": "not tracked"}, status_code=404)
    pid = row.get("pid")
    conv_id = int(row.get("conv_id"))
    generation_lease = await db.get_live_generation_lease(conv_id)
    if (
        generation_lease
        and generation_lease.get("draft_msg_id") is not None
        and int(generation_lease["draft_msg_id"]) != int(draft_msg_id)
    ):
        generation_lease = None
    matching_keys = _active_generation_keys_for_cancel(
        conv_id, draft_msg_id=draft_msg_id
    )
    matching_tasks = [
        _active_generations[key]
        for key in matching_keys
        if key in _active_generations
    ]
    await _pause_serial_queue_for_cancel(conv_id, draft_msg_id=draft_msg_id)
    for task in matching_tasks:
        _request_generation_stop(task, "cancel")

    # Let the provider adapter perform graceful, tree-aware teardown first.
    # If it does not finish, force-kill only this exact provider process tree.
    # Never cancel sibling tasks merely because they share a conversation.
    if matching_tasks:
        done, pending = await asyncio.wait(matching_tasks, timeout=12.0)
        if done:
            await asyncio.gather(*done, return_exceptions=True)
    else:
        pending = set()
    killed = False
    if pid and _pid_alive(pid):
        killed = _kill_pid(pid)
        await _wait_for_pid_exit(pid, timeout=8.0)
        if pending:
            done_after_kill, pending = await asyncio.wait(pending, timeout=4.0)
            if done_after_kill:
                await asyncio.gather(*done_after_kill, return_exceptions=True)
    pid_alive = bool(pid and _pid_alive(pid))
    # Mark draft as errored if still empty
    try:
        m = await db.get_message(draft_msg_id)
        if m and m.get("role") == "assistant" and not (m.get("content") or "").strip():
            await db.update_message_content(
                draft_msg_id,
                content="[Error: generation killed by admin]",
            )
    except Exception:
        pass
    task_alive = any(not task.done() for task in matching_tasks)
    if pid_alive or task_alive:
        return JSONResponse(
            {
                "status": "cancelling",
                "pid": pid,
                "pid_killed": killed,
                "pid_alive": pid_alive,
            },
            status_code=202,
        )
    # A live handler normally removes its own row in `finally`. If the server
    # no longer owns a task, cleanup is safe only after the PID is gone.
    await db.unregister_active_generation(draft_msg_id)
    if generation_lease:
        await db.release_generation_lease(
            generation_lease["lease_id"], "killed"
        )
    await db.update_message_content(draft_msg_id, turn_lifecycle_state="cancelled")
    await _send_generation_idle_if_quiescent(conv_id)
    return {
        "status": "killed",
        "pid": pid,
        "pid_killed": killed,
        "pid_alive": False,
    }


@app.get("/api/debug/state")
async def api_debug_state(request: Request):
    """Read-only diagnostic endpoint — localhost only."""
    import llama_client

    if not request.client or request.client.host not in ("127.0.0.1", "localhost", "::1"):
        raise HTTPException(status_code=403, detail="Localhost access only")

    # Active generations (in-memory task dict)
    gen_memory = []
    for k, t in _active_generations.items():
        gen_memory.append({
            "conv_id": k[0],
            "parent_id": k[1],
            "seq": k[2],
            "done": t.done(),
        })

    # Active generations (SQLite DB tracking)
    gen_db = await db.list_active_generations()

    goose_procs = []
    for cid, proc in _active_goose_procs.items():
        goose_procs.append({
            "conv_id": cid,
            "pid": proc.pid,
            "alive": _pid_alive(proc.pid) if proc.pid else False,
        })

    # Llama model name map
    model_map = dict(getattr(llama_client, "_model_name_map", {}))

    # Llama config values
    llama_cfg = {
        "llama_model": config.llama_model,
        "llama_models_dir": config.llama_models_dir,
    }

    return {
        "active_generations_memory": gen_memory,
        "active_generations_db": gen_db,
        "active_goose_procs": goose_procs,
        "model_name_map": model_map,
        "llama_config": llama_cfg,
    }


@app.get("/api/debug/stream-state/{conv_id}")
async def api_debug_stream_state(conv_id: int, request: Request):
    """Per-conv 'is this thing actually streaming right now' diagnostic.

    Use when a generation looks frozen in the UI: tells you whether the
    backend is still sending events, when the last one fired, what type it
    was, and how many WS clients were attached at that send. Lets you
    distinguish "server stopped generating" from "UI is dropping chunks".
    Localhost only.
    """
    if not request.client or request.client.host not in ("127.0.0.1", "localhost", "::1"):
        raise HTTPException(status_code=403, detail="Localhost access only")

    now = time.time()
    state = _stream_state.get(conv_id, {})
    last_at = state.get("last_event_at")
    in_memory_gens = [
        {"conv_id": k[0], "parent_id": k[1], "seq": k[2], "done": t.done()}
        for k, t in _active_generations.items() if k[0] == conv_id
    ]
    db_gens = [r for r in await db.list_active_generations() if r.get("conv_id") == conv_id]
    cc_proc = _active_claude_procs.get(conv_id)
    goose_proc = _active_goose_procs.get(conv_id)
    clients = _active_websockets.get(conv_id)

    return {
        "conv_id": conv_id,
        "now": now,
        "last_stream_event_at": last_at,
        "seconds_since_last_event": (now - last_at) if last_at else None,
        "last_event_type": state.get("last_event_type"),
        "events_sent_this_session": state.get("events_sent", 0),
        "clients_at_last_send": state.get("clients_at_send"),
        "connections_at_last_send": state.get("connections_at_send"),
        "recipients_at_last_send": state.get("clients_at_send"),
        "ws_clients_now": len(clients) if clients else 0,
        "active_generations_in_memory": in_memory_gens,
        "active_generations_db": db_gens,
        "claude_subprocess_pid": cc_proc.pid if cc_proc else None,
        "claude_subprocess_alive": _pid_alive(cc_proc.pid) if cc_proc and cc_proc.pid else False,
        "goose_subprocess_pid": goose_proc.pid if goose_proc else None,
        "goose_subprocess_alive": _pid_alive(goose_proc.pid) if goose_proc and goose_proc.pid else False,
    }


@app.get("/api/cron/help")
async def api_cron_help():
    """LLM-facing reference for creating Loom cron jobs."""
    return CRON_HELP


@app.get("/api/cron/jobs")
async def api_cron_jobs(include_archived: bool = False):
    return await _cron_list_jobs(include_archived=include_archived)


@app.get("/api/conversations/{conv_id}/cronjobs")
async def api_conversation_cron_jobs(conv_id: int, include_archived: bool = False):
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    return await _cron_list_jobs(include_archived=include_archived, conv_id=conv_id)


@app.post("/api/conversations/{conv_id}/cronjobs")
async def api_create_conversation_cron_job(conv_id: int, data: dict = Body(...)):
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    workspace = conv.get("project_dir") or "."
    if workspace == ".":
        raise HTTPException(status_code=400, detail="conversation has no dedicated workspace directory")
    every_seconds = _cron_interval_from_payload(data)
    _, script = _cron_validate_script(data.get("script", ""), workspace)
    description = str(data.get("description") or "").strip()
    if len(description) > 2000:
        raise HTTPException(status_code=400, detail="description must be 2000 characters or fewer")
    enabled = bool(data.get("enabled", True))
    job = await _cron_create_job(conv_id, str(Path(workspace).resolve()), script, description, every_seconds, enabled)
    return JSONResponse(job, status_code=201)


@app.put("/api/cron/jobs/{job_id}")
async def api_update_cron_job(job_id: int, data: dict = Body(...)):
    if "enabled" not in data:
        raise HTTPException(status_code=400, detail="only enabled can be updated for now")
    ok = await _cron_set_enabled(job_id, bool(data.get("enabled")))
    if not ok:
        raise HTTPException(status_code=404, detail="cron job not found")
    jobs = await _cron_list_jobs(include_archived=True)
    return next((j for j in jobs if j["id"] == job_id), {"status": "ok"})


@app.delete("/api/cron/jobs/{job_id}")
async def api_archive_cron_job(job_id: int):
    ok = await _cron_archive_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="cron job not found")
    return {"status": "archived", "id": job_id}


# Ensure upload directory exists
os.makedirs(config.upload_dir, exist_ok=True)

# Active WebSocket generation tasks — keyed by (conv_id, parent_id, seq) for parallel support
# CC mode only allows one per conv; Weave/OODA allow multiple (even on same parent)
_active_generations: dict[tuple[int, int | None, int], asyncio.Task] = {}
_gen_seq = 0  # monotonic counter for unique gen keys
# Active Claude Code subprocesses (for cancellation)
_active_claude_procs: dict[int, asyncio.subprocess.Process] = {}
# Active Goose ACP subprocesses (for cancellation) — main Loom provider lane
_active_goose_procs: dict[int, asyncio.subprocess.Process] = {}
# Active WebSocket connections per conversation — multiple clients can watch the same conv
_active_websockets: dict[int, set[WebSocket]] = {}
# Explicit event subscriptions.  A conversation socket is not implicitly
# subscribed to every generation in that conversation.
_event_router = SubscriptionRouter()
_event_sequencer = EventSequencer()

# Per-conv stream-event tracker: lets /api/debug/stream-state answer "is this
# conv getting tokens right now, and when was the last one" without scraping
# logs. Pruned implicitly — entries only grow during active generations and
# get overwritten on the next event.
_stream_state: dict[int, dict] = {}
_STREAM_EVENT_TYPES = frozenset({
    "stream_chunk", "thinking_chunk", "text_delta",
    "tool_start", "tool_input_chunk", "tool_result",
    "stream_end", "stream_start", "generation_preparing", "generation_active",
    "cancelled", "interrupted",
})
# Pending hook-based permission requests: request_id -> {event, response, conv_id}
_pending_hook_permissions: dict[str, dict] = {}
# Permission requests the user approved for the current branch generation.
# Keyed by (conversation id, permission scope), then an exact request fingerprint.
_auto_approve_permissions: dict[tuple[int, str], set[str]] = {}
# Session-local grants outlive a generation, not a server restart. Legacy
# exact/tool-wide caches are never consulted by the authoritative policy.
_permission_rules: dict[str, dict] = {}


async def _permission_context(conv: dict, scope: str, child: dict | None = None) -> dict:
    branch = _generation_branch_for_scope(conv["id"], scope)
    root = str((child or {}).get("project_dir") or conv.get("project_dir") or "")
    anchor = (child or {}).get("parent_message_id")
    if not child:
        anchor = branch.get("draft_msg_id") or branch.get("parent_id")
        if not anchor and scope.startswith("gen:"):
            lease = await db.get_generation_lease(scope)
            if lease and lease["conv_id"] == conv["id"] and not lease.get("released_at"):
                anchor = lease.get("draft_msg_id") or lease.get("user_msg_id")
    ancestors = await db.get_message_ancestor_ids(conv["id"], anchor) if anchor else set()
    return {"workspace": permission_rules.workspace_identity(root),
            "branch_id": anchor if anchor in ancestors else None,
            "ancestor_ids": ancestors, "conversation_id": conv["id"]}


def _remember_permission_rule(pending: dict) -> bool:
    candidate = pending.get("rule_candidate")
    if not candidate or not pending.get("supports_allow_all"):
        return False
    # Repeated approval of the same capability does not grow the cache.
    for rule in _permission_rules.values():
        if all(rule.get(k) == candidate.get(k) for k in
               ("conversation_id", "workspace", "branch_id", "shape")):
            return True
    rule_id = str(uuid.uuid4())
    _permission_rules[rule_id] = {"id": rule_id, **candidate}
    return True


@app.get("/api/conversations/{conv_id}/permission-rules")
async def list_permission_rules(conv_id: int):
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    return {"profile": conv.get("permission_profile") or "balanced", "lifetime": "server_session",
            "rules": [permission_rules.public_rule(r) for r in _permission_rules.values()
                      if r["conversation_id"] == conv_id]}


@app.delete("/api/conversations/{conv_id}/permission-rules/{rule_id}")
async def revoke_permission_rule(conv_id: int, rule_id: str):
    rule = _permission_rules.get(rule_id)
    if not rule or rule["conversation_id"] != conv_id:
        raise HTTPException(404, "Permission rule not found")
    del _permission_rules[rule_id]
    return {"revoked": True}


# Live generation state — survives WS disconnects so reconnecting clients get a snapshot.
# Keyed by gen_key (conv_id, parent_id, seq). Updated on every stream event.
_generation_snapshots: dict[tuple[int, int | None, int], dict] = {}


async def _answer_pending_permission(request_id: str, data: dict, *, websocket=None,
                                     channel: str | None = None) -> bool:
    """Shared first-response-wins resolver; callers establish UI authority.

    ``decision_source`` stays "user" (providers such as Goose key on it);
    ``decision_channel`` records which authenticated surface answered.
    """
    pending = _pending_hook_permissions.get(request_id)
    if not pending or pending.get("response") is not None:
        return False
    allow = bool(data.get("allow", False))
    feedback = str(data.get("feedback") or "").strip()[:4000]
    integrity = pending.get("integrity_check")
    integrity_error = await asyncio.to_thread(revalidate_integrity, integrity) if allow and integrity else ""
    if integrity_error:
        allow = False
        feedback = integrity_error
    resolution_source = "loom_policy" if integrity_error else "user"
    if channel is None:
        channel = "browser" if websocket is not None else "unknown"
    pending_conv_id = int(pending["conv_id"])
    permission_scope = pending.get("permission_scope") or _permission_scope_for_active_generation(pending_conv_id)
    always = bool(data.get("always")) and bool(
        pending.get("supports_allow_all", True)
    )
    # Re-check after awaits; only the first responder can grant.
    if pending.get("response") is not None:
        return False
    remembered = allow and always and _remember_permission_rule(pending)
    pending["response"] = {
        "allow": allow,
        "always": False,
        "remembered": bool(remembered),
        "feedback": feedback,
        "decision_source": resolution_source,
        "decision_channel": channel,
    }
    pending["event"].set()
    await db.save_permission_resolution(
        request_id,
        pending_conv_id,
        pending.get("tool_name") or "Unknown",
        allow,
        feedback,
        source=pending.get("source") or "loom_permission_hook",
        decision_source=resolution_source,
        decision_channel=channel,
        run_id=pending.get("run_id"),
        parent_id=pending.get("parent_id"),
        draft_msg_id=pending.get("draft_msg_id"),
        permission_scope=permission_scope,
    )
    # Also clean up from DB
    await db.delete_pending_permission(request_id)
    await _broadcast_permission_resolved(
        request_id, pending, allow=allow, feedback=feedback,
        decision_source=resolution_source, permission_scope=permission_scope, websocket=websocket)
    return True


async def _broadcast_permission_resolved(request_id: str, pending: dict, *, allow: bool, feedback: str,
                                         decision_source: str, permission_scope: str,
                                         websocket=None) -> None:
    """Tell subscribed pages a request is settled so its card stops accepting clicks."""
    pending_conv_id = int(pending["conv_id"])
    await _ws_broadcast_all({"type": "permission_notice", "request_id": request_id,
                             "conv_id": pending_conv_id, "status": "resolved"})
    resolved = LoomEventEnvelope.from_payload(
        {
            "type": "permission_resolved",
            "request_id": request_id,
            "allowed": allow,
            "conv_id": pending_conv_id,
            "permission_scope": permission_scope,
            "feedback": feedback,
            "decision_source": decision_source,
        },
        conversation_id=pending_conv_id,
        run_id=pending.get("run_id")
        or (permission_scope if permission_scope.startswith(("gen:", "agent:")) else None),
        parent_id=pending.get("parent_id"),
        draft_id=pending.get("draft_msg_id"),
        client_instance_id=pending.get("client_instance_id"),
        sequence=_event_sequencer.next(
            canonical_run_id(pending.get("run_id")
                              or (permission_scope if permission_scope.startswith(("gen:", "agent:")) else None)),
            pending_conv_id,
        ),
    )
    recipients = _event_router.recipients(
        resolved, _active_websockets.get(pending_conv_id, set())
    )
    origin = _find_client_websocket(pending_conv_id, pending.get("client_instance_id"))
    if origin is not None:
        recipients.add(origin)  # The request can precede this page's run subscription.
    if websocket is not None:
        recipients.add(websocket)  # Acknowledge the answering chat page.
    for target in list(recipients):
        try:
            await target.send_json(resolved.as_dict())
        except Exception:
            _active_websockets.get(pending_conv_id, set()).discard(target)
            _event_router.unregister(target)


async def _resolve_pending_permissions_for_scope(
    conv_id: int,
    permission_scope: str,
    *,
    message: str,
    decision_source: str = "loom_lifecycle",
) -> None:
    """Finish unresolved approvals without impersonating a user denial.

    ACP permission requests are long-lived HTTP calls.  When their owning
    generation ends, wake those calls with a lifecycle result before removing
    the durable pending row.  Selecting a provider's reject option is reserved
    for an actual Deny click in Loom.
    """
    for request_id, pending in list(_pending_hook_permissions.items()):
        if (
            pending.get("conv_id") != conv_id
            or pending.get("permission_scope") != permission_scope
            or pending.get("response") is not None
        ):
            continue
        pending["response"] = {
            "allow": False,
            "always": False,
            "feedback": message,
            "decision_source": decision_source,
        }
        # Wake the provider bridge before persistence. Database shutdown or a
        # transient lock must not strand the adapter until its HTTP timeout.
        pending["event"].set()
        try:
            await db.save_permission_resolution(
                request_id,
                conv_id,
                pending.get("tool_name") or "Unknown",
                False,
                message,
                source=pending.get("source") or "loom_permission_hook",
                decision_source=decision_source,
                run_id=pending.get("run_id"),
                parent_id=pending.get("parent_id"),
                draft_msg_id=pending.get("draft_msg_id"),
                permission_scope=permission_scope,
            )
            await db.delete_pending_permission(request_id)
        except Exception as exc:
            print(
                f"[PERM] Failed to persist {decision_source} outcome for "
                f"{request_id}: {exc}"
            )


def _stable_json(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return str(value)


def _permission_fingerprint(tool_name: str, tool_input, approval_method: str = "") -> str:
    """Stable key for the exact permission request a Loom approval grants."""
    method = str(approval_method or "")
    tool = str(tool_name or "Unknown")
    if method == "item/permissions/requestApproval":
        requested = {}
        if isinstance(tool_input, dict):
            for key in ("permissions", "requestedPermissions", "requested_permissions"):
                if isinstance(tool_input.get(key), dict):
                    requested = tool_input[key]
                    break
        return f"{method}:{_stable_json(requested)}"
    if method == "item/fileChange/requestApproval":
        return f"{method}:{_stable_json(tool_input)}"
    if method == "item/commandExecution/requestApproval":
        command = ""
        if isinstance(tool_input, dict):
            command = str(tool_input.get("command") or tool_input.get("cmd") or tool_input.get("execCommand") or "")
            if not command and isinstance(tool_input.get("argv"), list):
                command = " ".join(str(part) for part in tool_input["argv"])
        return f"{method}:{command or _stable_json(tool_input)}"
    if method:
        return f"{method}:{tool}"
    return tool


def _permission_scope_for_active_generation(conv_id: int, explicit_scope: str = "") -> str:
    """Return the branch-generation scope for remembered permissions."""
    explicit = str(explicit_scope or "").strip()
    if explicit:
        return explicit
    active = [
        gk for gk, task in _active_generations.items()
        if gk[0] == conv_id and not task.done()
    ]
    if len(active) == 1:
        return f"gen:{active[0][2]}"
    return "manual"


def _permission_scope_gen_id(permission_scope: str) -> int | None:
    scope = str(permission_scope or "")
    if not scope.startswith("gen:"):
        return None
    try:
        return int(scope.split(":", 1)[1])
    except ValueError:
        return None


def _generation_branch_for_scope(conv_id: int, permission_scope: str) -> dict:
    """Return branch identifiers for a generation-scoped permission request."""
    gen_id = _permission_scope_gen_id(permission_scope)
    if gen_id is None:
        return {}
    for key, snapshot in _generation_snapshots.items():
        if key[0] == conv_id and key[2] == gen_id:
            return {
                "gen_id": gen_id,
                "parent_id": snapshot.get("parent_id"),
                "draft_msg_id": snapshot.get("draft_msg_id"),
                "client_instance_id": snapshot.get("client_instance_id"),
            }
    return {"gen_id": gen_id}


async def _permission_socket_is_on_exact_draft(
    websocket: WebSocket,
    conv_id: int,
    draft_msg_id: int | str | None,
) -> bool:
    """Allow recovery on the owning draft, never an observing descendant."""
    if draft_msg_id is None:
        return False
    client_instance_id = str(
        getattr(websocket, "_client_instance_id", "") or ""
    ).strip()
    if not client_instance_id:
        return False
    cursor = await db.get_client_loom_cursor(conv_id, client_instance_id)
    return bool(
        cursor
        and cursor.get("leaf_message_id") is not None
        and str(cursor["leaf_message_id"]) == str(draft_msg_id)
    )


_COMMAND_KEYS = {"command", "cmd", "shell_command", "script"}
_TOOL_NAME_KEYS = {"name", "title", "tool_name", "toolName", "kind"}
_SHELL_TOOL_HINTS = (
    "bash",
    "shell",
    "cmd",
    "powershell",
    "terminal",
    "execute",
    "run_command",
)
_GOOSE_AUTO_ALLOW_TOOL_NAMES = {"tree"}
_DESTRUCTIVE_COMMAND_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (
        re.compile(r"(?i)\brm\s+-[^\s;&|]*r[^\s;&|]*"),
        "recursive rm is blocked",
    ),
    (
        re.compile(r"(?i)\b(?:remove-item|ri)\b[^\r\n;&|]*-(?:recurse|r)\b"),
        "recursive Remove-Item is blocked",
    ),
    (
        re.compile(r"(?i)\brm\b[^\r\n;&|]*-(?:recurse|r)\b"),
        "recursive PowerShell rm is blocked",
    ),
    (
        re.compile(r"(?i)\b(?:rd|rmdir)\b[^\r\n;&|]*/s\b"),
        "recursive rmdir/rd is blocked",
    ),
    (
        re.compile(r"(?i)\b(?:del|erase)\b[^\r\n;&|]*/s\b"),
        "recursive del/erase is blocked",
    ),
    (
        re.compile(r"(?i)\brobocopy\b[^\r\n;&|]*/(?:mir|purge)\b"),
        "robocopy mirror/purge is blocked",
    ),
)
_DESTRUCTIVE_GIT_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (
        re.compile(r"(?i)\bgit\s+clean\b[^\r\n;&|]*-[^\r\n;&|]*f"),
        "git clean can permanently remove untracked workspace files",
    ),
    (
        re.compile(r"(?i)\bgit\s+reset\b[^\r\n;&|]*--hard\b"),
        "git reset --hard discards tracked workspace changes",
    ),
    (
        re.compile(r"(?i)\bgit\s+restore\b"),
        "git restore overwrites current workspace content",
    ),
    (
        re.compile(r"(?i)\bgit\s+checkout\b[^\r\n;&|]*\s--(?:\s|$)"),
        "git checkout -- overwrites current workspace content",
    ),
    (
        re.compile(r"(?i)\bgit\s+stash\s+(?:drop|clear)\b"),
        "dropping Git stashes can destroy recoverable work",
    ),
    (
        re.compile(r"(?i)\bgit\s+branch\b[^\r\n;&|]*\s-D(?:\s|$)"),
        "git branch -D deletes an unmerged branch",
    ),
    (
        re.compile(r"(?i)\bgit\s+(?:checkout|switch)\b[^\r\n;&|]*(?:\s-f(?:\s|$)|--force\b|--discard-changes\b)"),
        "forced Git branch switching can discard workspace changes",
    ),
    (
        re.compile(r"(?i)\bgit\s+push\b[^\r\n;&|]*(?:--force(?:-with-lease|-if-includes)?\b|\s-f(?:\s|$))"),
        "force-pushing rewrites remote branch history",
    ),
    (
        re.compile(r"(?i)\bgit\s+push\b[^\r\n;&|]*\s(?::[^\s;&|]+|--delete\b)"),
        "deleting a remote Git branch changes shared history",
    ),
)
_DESTRUCTIVE_VERB_RE = re.compile(r"(?i)\b(?:rm|remove-item|ri|rd|rmdir|del|erase)\b")
_BROAD_TARGET_RE = re.compile(
    r"(?i)(?:^|\s)(?:"
    r"[A-Z]:[\\/](?:\s|$)"
    r"|[\\/](?:\s|$)"
    r"|\.{1,2}(?:\s|$)"
    r"|\*"
    r"|%USERPROFILE%"
    r"|%HOMEPATH%"
    r"|\$HOME"
    r"|\$PWD"
    r")"
)


def _iter_permission_dicts(value, *, _depth: int = 0):
    if _depth > 8:
        return
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _iter_permission_dicts(item, _depth=_depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_permission_dicts(item, _depth=_depth + 1)


def _iter_permission_strings(value, *, _depth: int = 0):
    if _depth > 8:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _iter_permission_strings(item, _depth=_depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_permission_strings(item, _depth=_depth + 1)


def _permission_tool_names(tool_name: str, tool_input) -> set[str]:
    names = {str(tool_name or "").strip().lower()}
    for obj in _iter_permission_dicts(tool_input):
        for key in _TOOL_NAME_KEYS:
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                names.add(value.strip().lower())
    return names


def _extract_permission_command(tool_name: str, tool_input) -> str:
    if isinstance(tool_input, str):
        return tool_input if any(h in str(tool_name or "").lower() for h in _SHELL_TOOL_HINTS) else ""
    for obj in _iter_permission_dicts(tool_input):
        for key in _COMMAND_KEYS:
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("arguments", "input", "args"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    for command_key in _COMMAND_KEYS:
                        command = parsed.get(command_key)
                        if isinstance(command, str) and command.strip():
                            return command.strip()
    raw_tool = str(tool_name or "")
    shell_prefixes = tuple(f"{hint} {chr(0x00B7)}" for hint in _SHELL_TOOL_HINTS)
    lowered = raw_tool.lower()
    for prefix in shell_prefixes:
        if lowered.startswith(prefix):
            return raw_tool.split(chr(0x00B7), 1)[1].strip()
    return ""


def _looks_like_shell_permission(tool_name: str, tool_input, command: str) -> bool:
    if command:
        return True
    return any(
        hint in name
        for name in _permission_tool_names(tool_name, tool_input)
        for hint in _SHELL_TOOL_HINTS
    )


def _has_unclosed_quote(command: str) -> bool:
    quote = ""
    escaped = False
    for ch in command:
        if escaped:
            escaped = False
            continue
        if ch in ("\\", "^", "`"):
            escaped = True
            continue
        if ch in ("'", '"'):
            if not quote:
                quote = ch
            elif quote == ch:
                quote = ""
    return bool(quote)


def _read_shell_word(text: str, start: int = 0) -> tuple[str, int]:
    """Read one shell-ish word without treating quoted payload text as code."""
    size = len(text)
    index = start
    while index < size and text[index].isspace():
        index += 1
    if index >= size:
        return "", size
    quote = text[index] if text[index] in ("'", '"') else ""
    if quote:
        index += 1
        begin = index
        while index < size:
            if text[index] == quote and (index == begin or text[index - 1] not in ("\\", "`")):
                return text[begin:index], index + 1
            index += 1
        return text[begin:], size
    begin = index
    while index < size and not text[index].isspace():
        index += 1
    return text[begin:index], index


def _split_shell_segments(command: str) -> list[str]:
    """Split command operators while preserving separators inside quotes."""
    segments: list[str] = []
    begin = 0
    quote = ""
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
            continue
        if char in ("^", "`"):
            escaped = True
            continue
        if char in ("'", '"'):
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
            continue
        if not quote and char in ";|&\r\n":
            segment = command[begin:index].strip()
            if segment:
                segments.append(segment)
            begin = index + 1
    tail = command[begin:].strip()
    if tail:
        segments.append(tail)
    return segments


def _strip_outer_shell_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1].strip()
    return value


def _shell_payload(arguments: str, flags: set[str]) -> str:
    index = 0
    while index < len(arguments):
        word, end = _read_shell_word(arguments, index)
        if not word:
            break
        if word.lower() in flags:
            return _strip_outer_shell_quotes(arguments[end:])
        index = end
    return ""


def _effective_shell_segments(command: str, *, _depth: int = 0) -> list[str]:
    """Return executable command segments, unwrapping common shell launchers.

    Permission payloads from Codex commonly contain a full PowerShell wrapper.
    Safety checks must inspect the wrapped command, but never words inside an
    unrelated quoted argument such as a Git commit message.
    """
    if _depth > 5:
        return [command.strip()] if command.strip() else []
    effective: list[str] = []
    for segment in _split_shell_segments(command):
        executable, end = _read_shell_word(segment)
        basename = executable.replace("/", "\\").rsplit("\\", 1)[-1].lower()
        arguments = segment[end:].strip()
        payload = ""
        if basename in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
            payload = _shell_payload(arguments, {"-command", "-c", "-commandwithargs"})
        elif basename in {"cmd", "cmd.exe"}:
            payload = _shell_payload(arguments, {"/c", "/k"})
        elif basename in {"bash", "bash.exe", "sh", "sh.exe", "zsh", "zsh.exe"}:
            payload = _shell_payload(arguments, {"-c", "-lc"})
        if payload:
            effective.extend(_effective_shell_segments(payload, _depth=_depth + 1))
        else:
            effective.append(segment)
    return effective


def _unbounded_scan_reason(command: str) -> str:
    """Classify actual scan executables, never matching quoted argument text."""
    for segment in _effective_shell_segments(command):
        executable, end = _read_shell_word(segment)
        basename = executable.replace("/", "\\").rsplit("\\", 1)[-1].lower()
        arguments = segment[end:]
        if basename in {"tree", "tree.com"}:
            return "unbounded tree scan is blocked"
        if basename == "dir" and re.search(r"(?i)(?:^|\s)/s(?:\s|$)", arguments):
            return "unbounded dir /s scan is blocked"
        if basename in {"get-childitem", "gci", "ls"} and re.search(
            r"(?i)(?:^|\s)-(?:recurse|r)(?:\s|$)", arguments
        ):
            return "unbounded recursive directory scan is blocked"
        if basename == "ls" and re.search(
            r"(?:^|\s)-[A-Za-z]*R[A-Za-z]*(?:\s|$)", arguments
        ):
            return "unbounded ls -R scan is blocked"
    return ""


def _dangerous_executable_reason(command: str) -> str:
    """Match destructive programs as executables, not words in arguments.

    Raw word-boundary matching classified PowerShell's harmless
    ``Format-List`` and ``Format-Table`` cmdlets as the disk formatter.  The
    shell parser already unwraps cmd/PowerShell/bash launchers, so inspect the
    executable token of each effective segment instead.
    """
    for segment in _effective_shell_segments(command):
        executable, _ = _read_shell_word(segment)
        basename = executable.replace("/", "\\").rsplit("\\", 1)[-1].lower()
        if basename in {"format", "format.com"}:
            return "disk format is blocked"
        if basename in {"diskpart", "diskpart.exe"}:
            return "diskpart is blocked"
    return ""


def _unsafe_shell_command_reason(tool_name: str, tool_input) -> str:
    command = _extract_permission_command(tool_name, tool_input)
    if not _looks_like_shell_permission(tool_name, tool_input, command):
        return ""
    if not command:
        return ""

    for pattern, reason in _DESTRUCTIVE_COMMAND_PATTERNS:
        if pattern.search(command):
            if _has_unclosed_quote(command):
                return f"{reason}; command also has unclosed quotes"
            return reason

    executable_reason = _dangerous_executable_reason(command)
    if executable_reason:
        return executable_reason

    if _DESTRUCTIVE_VERB_RE.search(command) and _BROAD_TARGET_RE.search(command):
        if _has_unclosed_quote(command):
            return "broad destructive command with unclosed quotes is blocked"
        return "broad destructive command target is blocked"

    if _has_unclosed_quote(command) and _DESTRUCTIVE_VERB_RE.search(command):
        return "destructive command with unclosed quotes is blocked"

    scan_reason = _unbounded_scan_reason(command)
    if scan_reason:
        return scan_reason

    return ""


def _destructive_git_command_reason(tool_name: str, tool_input) -> str:
    """Return a reason that requires explicit Loom approval, not an auto-deny."""
    command = _extract_permission_command(tool_name, tool_input)
    if not _looks_like_shell_permission(tool_name, tool_input, command) or not command:
        return ""
    for pattern, reason in _DESTRUCTIVE_GIT_PATTERNS:
        if pattern.search(command):
            if _has_unclosed_quote(command):
                return f"{reason}; command also has unclosed quotes"
            return reason

    return ""


def _looks_like_goose_permission(data: dict) -> bool:
    tool_input = data.get("tool_input")
    return (
        isinstance(tool_input, dict)
        and isinstance(tool_input.get("params"), dict)
        and (
            "toolCall" in tool_input
            or "tool_call" in tool_input
            or "toolCall" in tool_input.get("params", {})
            or "tool_call" in tool_input.get("params", {})
        )
    )


def _goose_auto_allow_reason(tool_name: str, tool_input) -> str:
    """Allow low-risk native Goose housekeeping tools without a Loom prompt."""
    command = _extract_permission_command(tool_name, tool_input)
    if command:
        return ""

    names = _permission_tool_names(tool_name, tool_input)
    if any(name in _GOOSE_AUTO_ALLOW_TOOL_NAMES or name.startswith("tree ") for name in names):
        return "Goose native tree tool"

    text = "\n".join(_iter_permission_strings(tool_input)).lower()
    if any("todo" in name for name in names):
        return "Goose todo bookkeeping"
    if ".goose" in text and "todo" in text:
        return "Goose todo bookkeeping"
    return ""


def _permission_policy_shadow_enabled() -> bool:
    """Run the formal-policy runtime beside the legacy gate without enforcing it."""
    return str(os.environ.get("LOOM_PERMISSION_POLICY_SHADOW", "1")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


_recent_generations: dict[tuple[int, int], dict] = {}
_MAX_RECENT_GENERATIONS = 128


def _update_gen_snapshot(gen_key: tuple, **fields):
    """Update the live snapshot for an active generation (called on every stream event)."""
    snap = _generation_snapshots.get(gen_key)
    if snap is None:
        snap = {
            "full_text": "",
            "content_blocks": [],
            "input_tokens": 0,
            "output_tokens": 0,
            "started_at": 0,
            "draft_msg_id": None,
            "parent_id": None,
            "cc_model": "",
            "mode": "claude",
        }
        _generation_snapshots[gen_key] = snap
    previous_draft_id = snap.get("draft_msg_id")
    snap.update(fields)
    if len(gen_key) >= 3:
        record_key = (int(gen_key[0]), int(gen_key[2]))
        recent = _recent_generations.setdefault(record_key, {})
        recent.update(snap)
        if len(_recent_generations) > _MAX_RECENT_GENERATIONS:
            oldest = next(iter(_recent_generations))
            _recent_generations.pop(oldest, None)
    if previous_draft_id is None and snap.get("draft_msg_id") is not None:
        try:
            _spawn_background_task(
                _advance_unambiguous_generation_cursors(gen_key),
                "advance generation cursor",
            )
            task = asyncio.current_task()
            lease_id = getattr(task, "_generation_lease_id", None)
            if lease_id:
                _spawn_background_task(
                    db.bind_generation_lease(
                        lease_id,
                        draft_msg_id=int(snap["draft_msg_id"]),
                        state="running",
                    ),
                    "bind generation lease",
                )
        except RuntimeError:
            pass
    return snap


async def _advance_unambiguous_generation_cursors(gen_key: tuple) -> None:
    """Advance only the page that launched one unambiguous run."""
    snapshot = _generation_snapshots.get(gen_key) or {}
    thread_id = snapshot.get("loom_thread_id")
    client_instance_id = snapshot.get("client_instance_id")
    draft_msg_id = snapshot.get("draft_msg_id")
    parent_id = snapshot.get("parent_id", gen_key[1])
    if not thread_id or not client_instance_id or draft_msg_id is None or parent_id is None:
        return
    client_siblings = [
        key for key, task in _active_generations.items()
        if not task.done()
        and key[0] == gen_key[0]
        and str((_generation_snapshots.get(key) or {}).get("loom_thread_id"))
            == str(thread_id)
        and str((_generation_snapshots.get(key) or {}).get("parent_id", key[1]))
            == str(parent_id)
        and str((_generation_snapshots.get(key) or {}).get("client_instance_id") or "")
            == str(client_instance_id)
    ]
    # One page asking for several sibling generations has no unique child to
    # follow. Different pages launching one sibling each remain independent.
    if len(client_siblings) != 1:
        return
    await db.advance_loom_cursor_for_generation(
        gen_key[0], thread_id, client_instance_id, parent_id, draft_msg_id
    )


def _parse_image_paths(image_path) -> list[str]:
    """Parse image_path field: handles single string, JSON array string, or list."""
    if not image_path:
        return []
    if isinstance(image_path, list):
        return image_path
    try:
        parsed = json.loads(image_path)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return [image_path]



# ── Static files ──

class _LoomStaticFiles(StaticFiles):
    """Revalidate executable UI assets so source and browser cannot drift."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if Path(path).suffix.lower() in {".html", ".js", ".css"}:
            response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", _LoomStaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


# ── Canvas ──


@app.post("/api/conversations/{conv_id}/canvas")
async def toggle_canvas(conv_id: int, data: dict = None):
    """Enable or disable canvas for a conversation."""
    data = data or {}
    enabled = bool(data.get("enabled", True))
    conv = await db.get_conversation(conv_id)
    if not conv:
        return JSONResponse({"error": "Not found"}, status_code=404)

    project_dir = conv.get("project_dir") or ""
    if enabled and not project_dir:
        # Auto-create a workspace for conversations without a project_dir
        workspace = (Path("canvas_workspaces") / str(conv_id)).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        project_dir = str(workspace)
        await db.update_conversation_fields(conv_id, project_dir=project_dir)

    canvas_slug = conv.get("canvas_slug") or ""
    if enabled:
        try:
            await asyncio.to_thread(ensure_canvas_workspace, project_dir, CANVAS_CLAUDE_MD)
        except (OSError, ValueError):
            return JSONResponse({"error": "Could not prepare Canvas inside the workspace"}, status_code=400)

        # Mint a stable slug the first time canvas is enabled. Retry on the
        # off chance the unique index rejects the generated slug.
        if not canvas_slug:
            for _ in range(5):
                candidate = generate_canvas_slug()
                if not await db.get_conversation_by_canvas_slug(candidate):
                    canvas_slug = candidate
                    break
            if canvas_slug:
                await db.update_conversation_fields(conv_id, canvas_slug=canvas_slug)

    await db.update_conversation_fields(conv_id, canvas_enabled=1 if enabled else 0)
    return {
        "ok": True,
        "enabled": enabled,
        "project_dir": project_dir,
        "canvas_slug": canvas_slug or None,
    }


@app.get("/api/canvas/{conv_id}/share-link")
async def canvas_share_link(conv_id: int, request: Request):
    from canvas_links import canvas_tailnet_url
    conv = await db.get_conversation(conv_id)
    if not conv or not conv.get("canvas_enabled") or not is_valid_slug(conv.get("canvas_slug") or ""):
        return JSONResponse({"detail": "No enabled Canvas link"}, status_code=404)
    try:
        url = await asyncio.to_thread(canvas_tailnet_url, conv["canvas_slug"],
                                      request.url.scheme, request.url.port)
    except ValueError as error:
        return JSONResponse({"detail": str(error)}, status_code=503)
    return {"url": url}


@app.get("/api/canvas/{conv_id}")
async def list_canvas_files(conv_id: int):
    """List files in a conversation's canvas directory."""
    conv = await db.get_conversation(conv_id)
    if not conv or not conv.get("project_dir"):
        return JSONResponse({"error": "No canvas"}, status_code=404)
    try:
        canvas_dir = canvas_root(conv["project_dir"])
    except (OSError, ValueError):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    if not canvas_dir.is_dir():
        return {"files": []}
    files = [f.name for f in canvas_dir.iterdir() if f.is_file()]
    return {"files": sorted(files)}


@app.get("/api/conversations/{conv_id}/canvas/revision")
async def get_canvas_revision(conv_id: int):
    conv = await db.get_conversation(conv_id)
    if not conv or not conv.get("canvas_enabled") or not conv.get("project_dir"):
        return JSONResponse({"error": "No enabled canvas"}, status_code=404)
    try:
        result = await asyncio.to_thread(canvas_revision, conv["project_dir"])
    except (OSError, ValueError):
        result = {"revision": None, "complete": False}
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.get("/api/canvas/{conv_id}/{file_path:path}")
async def serve_canvas_file(conv_id: int, file_path: str = "index.html"):
    """Serve a file from a conversation's canvas directory."""
    conv = await db.get_conversation(conv_id)
    if not conv or not conv.get("project_dir"):
        return JSONResponse({"error": "No canvas"}, status_code=404)
    if not file_path:
        file_path = "index.html"
    try:
        canvas_dir = canvas_root(conv["project_dir"])
        target = (canvas_dir / file_path).resolve()
    except (OSError, ValueError):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    if not target.is_relative_to(canvas_dir):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    if not target.is_file():
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(str(target), headers={"Cache-Control": "no-cache"})


@app.get("/search")
@app.get("/")
async def index():
    return FileResponse(
        "static/index.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/{slug}")
async def canvas_by_slug(slug: str):
    """Direct-access wrapper: serve a full-viewport iframe of a conversation's canvas
    when the URL path matches a canvas slug. Strict regex prevents collisions with
    any other top-level path."""
    if not is_valid_slug(slug):
        return JSONResponse({"error": "Not found"}, status_code=404)
    conv = await db.get_conversation_by_canvas_slug(slug)
    if not conv or not conv.get("canvas_enabled"):
        return JSONResponse({"error": "Not found"}, status_code=404)
    # Title is user-controlled — escape for HTML safety.
    from html import escape as _esc
    title = _esc(conv.get("title") or slug)
    html = (
        '<!doctype html><meta charset="utf-8">'
        f"<title>{title}</title>"
        "<style>html,body{margin:0;height:100%;background:#000}"
        "iframe{border:0;width:100vw;height:100vh;display:block;background:#fff}</style>"
        '<iframe sandbox="allow-scripts allow-same-origin allow-modals allow-downloads" '
        f'data-canvas-conv="{conv["id"]}" src="/api/canvas/{conv["id"]}/index.html"></iframe>'
        '<script src="/static/canvas-viewer.js?v=1"></script>'
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


# ── Health ──


@app.get("/api/health")
async def api_health():
    llama_status = await health_check()
    return llama_status




# Map of probe targets to (port, health_path). All run on localhost alongside
# main Loom; main Loom proxies them so the settings panel can probe over HTTPS
# without browser mixed-content blocking the direct HTTP request.
_STATUS_TARGETS = {
    "admin":   (3002, "/api/status"),
    
    "comfyui": (8188, "/system_stats"),
}


@app.get("/api/server-status/{target}")
async def api_server_status(target: str):
    """Server-side proxy of a co-located service's health endpoint.
    Browser probes from HTTPS are blocked as mixed content, so we probe from
    the server trying HTTPS first, then HTTP. Returns {up: bool}."""
    if target == "llama":
        from urllib.parse import urlparse
        try:
            url = config.llama_host_url()
            parsed = urlparse(url)
            port = parsed.port or 8000
        except Exception:
            port = 8000
        path = "/v1/models"
    else:
        spec = _STATUS_TARGETS.get(target)
        if not spec:
            return {"up": False}
        port, path = spec
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            for scheme in ("https", "http"):
                try:
                    r = await client.get(f"{scheme}://127.0.0.1:{port}{path}")
                    if r.status_code == 200:
                        return {"up": True}
                except Exception:
                    pass
    except Exception:
        pass
    return {"up": False}


BSKY_API = "https://bsky.social/xrpc"
BSKY_PUBLIC_API = "https://public.api.bsky.app/xrpc"

# Endpoints available on the read-only public API (no auth needed)
# Verified: 200 with real data on public.api.bsky.app
# getTimeline requires auth on both APIs — intentionally excluded
_BSKY_PUBLIC_ENDPOINTS = {
    "app.bsky.feed.getAuthorFeed",
    "app.bsky.actor.getProfile",
    "app.bsky.graph.getFollows",
}


async def _resolve_bsky_handle(handle: str) -> str | None:
    """Resolve a Bluesky handle to its DID via the public identity API.
    Returns the original string if it's already a DID, or the resolved DID.
    Returns None on failure."""
    import urllib.request, urllib.parse
    import asyncio, ssl

    # Already a DID — nothing to resolve
    if handle.startswith("did:"):
        return handle

    def _resolve():
        ctx = ssl.create_default_context()
        url = f"https://bsky.social/xrpc/com.atproto.identity.resolveHandle?handle={urllib.parse.quote(handle)}"
        req = urllib.request.Request(url, method="GET")
        try:
            resp = urllib.request.urlopen(req, timeout=5, context=ctx)
            data = json.loads(resp.read())
            return data.get("did")
        except Exception:
            return None

    return await asyncio.to_thread(_resolve)


async def _do_bsky_proxy(method: str, path: str, body: dict = None, query_params: dict = None, auth_header: str = None):
    """Core handler for the Bluesky XRPC proxy.
    Uses urllib (not httpx) because httpx gets blocked by bsky's AWS WAF while urllib works fine.
    Public read endpoints route to public.api.bsky.app, everything else goes to bsky.social.
    Auto-resolves handle values in actor/author query params to DIDs."""
    import urllib.request, urllib.parse
    import asyncio, ssl

    target = BSKY_PUBLIC_API if method == "GET" and path in _BSKY_PUBLIC_ENDPOINTS else BSKY_API

    # Resolve any handle values in actor/author params to DIDs
    if query_params:
        for key in ("actor", "author"):
            val = query_params.get(key)
            if val and not val.startswith("did:"):
                resolved = await _resolve_bsky_handle(val)
                if resolved:
                    query_params[key] = resolved
                    print(f"[BSKY] Resolved handle {val} -> {resolved}")

    url = f"{target}/{path}"
    if query_params:
        qs = urllib.parse.urlencode(query_params, doseq=True)
        url = f"{url}?{qs}"

    data = json.dumps(body).encode("utf-8") if body else None
    fwd_headers = {"Content-Type": "application/json"}
    if auth_header:
        fwd_headers["Authorization"] = auth_header

    print(f"[BSKY] {method} {path} target={target.split('//')[1].split('/')[0]} params={query_params} auth={bool(auth_header)}")

    def _fetch():
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, data=data, headers=fwd_headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=10, context=ctx)
            return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    status, raw = await asyncio.to_thread(_fetch)
    try:
        content = json.loads(raw)
        return JSONResponse(status_code=status, content=content)
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(status_code=status, content={"_raw": raw.decode("utf-8", errors="replace")})


@app.post("/api/proxy/bsky/{path:path}")
async def proxy_bsky_post(path: str, body: dict = Body(default={}), authorization: str = Header(None)):
    """Proxy Bluesky ATProto XRPC POST calls so the browser isn't blocked by CORS."""
    return await _do_bsky_proxy("POST", path, body, auth_header=authorization)


@app.get("/api/proxy/bsky/{path:path}")
async def proxy_bsky_get(request: Request, path: str, authorization: str = Header(None)):
    """Proxy Bluesky ATProto XRPC GET calls."""
    qp = dict(request.query_params) if len(request.query_params) > 0 else None
    return await _do_bsky_proxy("GET", path, query_params=qp, auth_header=authorization)


# ── Bluesky token storage (canvas uses this to persist session tokens) ──

@app.get("/api/bluesky-token/{conv_id}")
async def get_bsky_token(conv_id: int):
    """Return the stored Bluesky access token for a conversation."""
    conv = await db.get_conversation(conv_id)
    if not conv:
        return JSONResponse({"error": "not found"}, status_code=404)
    token = conv.get("bsky_token")
    if not token:
        return JSONResponse({"error": "no token stored"}, status_code=404)
    return {"token": token}


@app.put("/api/bluesky-token/{conv_id}")
async def set_bsky_token(conv_id: int, data: dict = Body(default={})):
    """Store a Bluesky access token for a conversation."""
    token = data.get("token")
    if not token:
        return JSONResponse({"error": "no token provided"}, status_code=400)
    await db.update_conversation_fields(conv_id, bsky_token=token)
    return {"ok": True}


@app.delete("/api/bluesky-token/{conv_id}")
async def delete_bsky_token(conv_id: int):
    """Clear the stored Bluesky access token for a conversation."""
    await db.update_conversation_fields(conv_id, bsky_token=None)
    return {"ok": True}


@app.get("/api/admin-status")
async def api_admin_status():
    """Back-compat alias for the unified server-status endpoint."""
    return await api_server_status("admin")


# ── Local model list cache ────────────────────────────────────────────────
# Serves the .gguf file list from disk so the UI model dropdown is instant.

_LOCAL_MODELS_CACHE: dict | None = None
_VISION_MODELS_CACHE: dict | None = None


async def _refresh_local_models_cache() -> dict:
    global _LOCAL_MODELS_CACHE
    status = await health_check()
    import llama_client as _lc
    disk_models = _lc.list_local_models()
    _LOCAL_MODELS_CACHE = {
        "backend": "llama",
        "host": config.llama_host,
        "models": disk_models,
        "disk_models": disk_models,
        "target_model": config.llama_model,
        "available": status.get("model_available", False),
        "fetched_at": time.time(),
    }
    return _LOCAL_MODELS_CACHE


async def _refresh_vision_models_cache() -> dict:
    """Vision model list — same as main models (llama-server uses same model for all tasks)."""
    global _VISION_MODELS_CACHE
    import llama_client as _lc
    disk_models = _lc.list_local_models()
    _VISION_MODELS_CACHE = {
        "backend": "llama",
        "host": config.llama_host,
        "models": disk_models,
        "fetched_at": time.time(),
    }
    return _VISION_MODELS_CACHE


def _cache_is_stale(cache: dict | None) -> bool:
    """Returns True if the cache was built against a different host than current config."""
    if not cache:
        return True
    return cache.get("backend") != "llama" or cache.get("host") != config.llama_host


@app.get("/api/local/models")
async def api_local_models():
    """Active-backend-aware model list, served from cache. Use
    POST /api/local/refresh-models to force a re-fetch."""
    if _cache_is_stale(_LOCAL_MODELS_CACHE):
        await _refresh_local_models_cache()
    return _LOCAL_MODELS_CACHE


@app.get("/api/local/vision-models")
async def api_local_vision_models():
    """Vision-capable models from the active backend, served from cache."""
    if _cache_is_stale(_VISION_MODELS_CACHE):
        await _refresh_vision_models_cache()
    return _VISION_MODELS_CACHE


@app.post("/api/local/refresh-models")
async def api_local_refresh_models():
    """Force-refresh both model caches. Wired to the 'Refresh' button in
    Settings; also called after llama-server starts/stops."""
    await _refresh_local_models_cache()
    await _refresh_vision_models_cache()
    await _refresh_all_engines_cache()
    return {
        "models": _LOCAL_MODELS_CACHE.get("models", []),
        "vision_models": _VISION_MODELS_CACHE.get("models", []),
        "backend": "llama",
    }


# ── All models list ───────────────────────────────────────────────────────
# Returns .gguf models from disk + any live server models.
# Used by the Braid/Weave inline dropdown.

_ALL_ENGINES_CACHE: dict | None = None
_ALL_ENGINES_CACHE_TTL_SEC = 10
# Single-flight guard: concurrent callers of api_local_all_models (the settings
# panel fires several in the same tick) must share one refresh rather than each
# re-probing llama-server (5s timeout) and the dream sidecar (2s timeout).
# Held only for the duration of _refresh_all_engines_cache.
_ALL_ENGINES_REFRESH_LOCK: asyncio.Lock = asyncio.Lock()


def _all_engines_cache_fresh() -> bool:
    """True if the all-engines cache exists and is within its TTL."""
    if not _ALL_ENGINES_CACHE:
        return False
    return time.time() - float(_ALL_ENGINES_CACHE.get("fetched_at", 0)) <= _ALL_ENGINES_CACHE_TTL_SEC


async def _refresh_all_engines_cache() -> dict:
    """Scan disk for .gguf files and probe llama-server for loaded models.

    The llama-server and dream sidecar health probes run concurrently: they
    were previously sequential (await health_check() then await
    dream_client.health()), which stacked a 5s llama timeout on top of a 2s
    dream timeout. With gather they overlap, so worst case is max() not sum().
    """
    global _ALL_ENGINES_CACHE
    import llama_client as _lc
    disk_models = _lc.list_local_models()

    # Probe both backends concurrently. Each returns its own result/exception
    # so one slow/down service can't block or abort the other.
    status_task = _lc.health_check()
    async def _dream_probe():
        try:
            import dream_client as _dc
            return await _dc.health(config.dream_host, timeout=2.0)
        except Exception:
            return None
    status, dh = await asyncio.gather(status_task, _dream_probe(), return_exceptions=True)

    # health_check already returns a dict on failure (mock mode), but guard
    # anyway in case gather surfaced it as an exception object.
    if isinstance(status, Exception):
        status = {"models": [], "model_name_map": {}}

    live_models = status.get("models", [])
    model_name_map = status.get("model_name_map", {})
    model_meta = status.get("model_meta", {})
    out: list[dict] = []
    for m in disk_models:
        is_loaded = False
        server_id = model_name_map.get(m)
        if m in model_name_map and model_name_map[m] in live_models:
            is_loaded = True
        elif live_models and m == status.get("target_model"):
            is_loaded = True
            server_id = m
        configured = bool(config.llama_model and models_match(m, config.llama_model))
        out.append({
            "name": m,
            "backend": "llama",
            "loaded": is_loaded,
            "configured": configured,
            "server_id": server_id,
            "runtime": model_meta.get(server_id or m, {}),
        })

    for server_id in live_models:
        if any((model_name_map.get(m) == server_id) or (m == server_id) for m in disk_models):
            continue
        out.append({
            "name": server_id,
            "backend": "llama",
            "loaded": True,
            "configured": bool(config.llama_model and models_match(server_id, config.llama_model)),
            "server_id": server_id,
            "runtime": model_meta.get(server_id, {}),
        })

    # Append Dream Engine models from an OpenAI-compatible DiffusionGemma
    # endpoint. backend:"dream" routes direct generation through dream_client.
    # This model endpoint is independent of the retired Hermes harness.
    dream_seen: set[str] = set()
    dream_model = (getattr(config, "dream_model", "") or "").strip()
    # Only offer Dream entries while the sidecar answers its health probe:
    # when the Dream server is down, dropdowns show no Dream models at all
    # instead of unloaded placeholders.
    dream_up = isinstance(dh, dict)
    if dream_up and dream_model:
        out.append({"name": dream_model, "backend": "dream", "loaded": False, "configured": True})
        dream_seen.add(dream_model)

    if dream_up:
        avail = dh.get("available") or []
        loaded = bool(dh.get("loaded_model"))
        for mid in avail:
            name = str(mid)
            if name in dream_seen:
                for model in out:
                    if model.get("backend") == "dream" and model.get("name") == name:
                        model["loaded"] = loaded
                continue
            out.append({"name": name, "backend": "dream", "loaded": loaded, "configured": name == dream_model})
            dream_seen.add(name)

    _ALL_ENGINES_CACHE = {
        "models": out,
        "active_backend": "llama",
        "llama_host": config.llama_host,
        "target_model": config.llama_model,
        "live_models": live_models,
        "fetched_at": time.time(),
    }
    return _ALL_ENGINES_CACHE


def _with_openrouter_models(cache: dict) -> dict:
    """Serve cached engine models plus registry OpenRouter selectors.

    OpenRouter models are not probed like local engines; the shared registry
    (hot-reloadable from the admin Models editor) is the source of truth, so
    they are merged at response time instead of living in the TTL cache.
    """
    data = dict(cache)
    data["models"] = [*cache.get("models", []), *model_registry.openrouter_models()]
    return data


@app.get("/api/local/all-models")
async def api_local_all_models():
    """Returns all local .gguf models plus live llama-server models.
    Used by the inline dropdown for model selection."""
    if _all_engines_cache_fresh():
        return _with_openrouter_models(_ALL_ENGINES_CACHE)
    # Single-flight: hold the lock so concurrent callers serialize on one
    # refresh, then re-check freshness once inside — another caller may have
    # just refreshed the cache while we waited for the lock.
    async with _ALL_ENGINES_REFRESH_LOCK:
        if _all_engines_cache_fresh():
            return _with_openrouter_models(_ALL_ENGINES_CACHE)
        await _refresh_all_engines_cache()
    return _with_openrouter_models(_ALL_ENGINES_CACHE)


@app.post("/api/local/refresh-all-models")
async def api_local_refresh_all_models():
    return await _refresh_all_engines_cache()


# ── Per-model configuration ────────────────────────────────────────────────
# models_config.json lives in the project root next to config.json.
# Format: { "ModelName.gguf": { ctx_size, ngl, flash_attn, kv_quant, threads,
#             batch, ubatch, mlock, extra_args } }

MODEL_CONFIG_PATH = Path(__file__).parent / "models_config.json"
_KV_QUANT_OPTIONS = ["none", "K4", "K4_0", "K5", "K5_0", "K8", "K8_0"]


def _load_model_configs() -> dict:
    if MODEL_CONFIG_PATH.exists():
        try:
            with open(MODEL_CONFIG_PATH, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_model_configs(cfg: dict):
    try:
        with open(MODEL_CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[SERVER] Failed to save models_config.json: {e}")


def _default_ctx(name: str) -> int:
    """Guess context size from model name. Check larger sizes first."""
    import re
    # Match patterns like "27b", "32b" etc. - largest match wins
    m = re.search(r'(\d+)b', name, re.IGNORECASE)
    if m:
        size = int(m.group(1))
        if size <= 3:
            return 200000
        if size <= 8:
            return 150000
        if size <= 14:
            return 100000
        if size <= 27:
            return 80000
        if size <= 70:
            return 40000
    return 150000


def _new_model_default(name: str) -> dict:
    return {
        "ctx_size": _default_ctx(name),
        "ngl": 999,
        "flash_attn": True,
        "kv_quant": "none",
        "threads": None,
        "batch": None,
        "ubatch": None,
        "mlock": False,
        "mmproj": None,
        "extra_args": "",
        "server_model_id": None,
    }


def _sync_disk_models():
    """Add new .gguf files to config with defaults. Never deletes."""
    import llama_client as _lc
    cfg = _load_model_configs()
    disk = _lc.list_local_models()
    for m in disk:
        if m not in cfg:
            cfg[m] = _new_model_default(m)
    _save_model_configs(cfg)
    return cfg


@app.get("/api/models-config")
async def api_get_models_config():
    """Return models_config.json (synced with disk)."""
    cfg = _sync_disk_models()
    return {"models": cfg, "kv_quant_options": _KV_QUANT_OPTIONS}


@app.put("/api/models-config")
async def api_update_models_config(data: dict):
    """Update all model configs. Only saves entries for models that exist on disk."""
    import llama_client as _lc
    disk_set = set(_lc.list_local_models())
    cfg = {}
    for name, settings in data.get("models", {}).items():
        if name in disk_set:
            cfg[name] = settings
    _save_model_configs(cfg)
    return {"status": "ok", "saved": len(cfg)}


@app.get("/api/disk-models")
async def api_get_disk_models():
    """Return list of .gguf files in the models directory."""
    import llama_client as _lc
    models = _lc.list_local_models()
    return {"models": models}


@app.post("/api/update-model-ids")
async def api_update_model_ids():
    """Update server_model_id in config by querying the running Llama Server.

    Matches the server-registered model ID to the active config model name
    and persists it in models_config.json.
    """
    import httpx
    import llama_client as _lc
    cfg = _load_model_configs()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{config.llama_host_url()}/v1/models")
            resp.raise_for_status()
            data = resp.json()
            server_models = [m["id"] for m in data.get("data", [])]
    except Exception as e:
        return {"status": "error", "message": str(e)}

    # Match server IDs to local model configs
    active = config.llama_model or ""
    active_norm = active.lower().replace("-", "").replace("_", "").replace(".", "").replace(":", "")
    for model_file in cfg:
        norm = model_file.lower().replace("-", "").replace("_", "").replace(".", "").replace(":", "")
        for srv_id in server_models:
            srv_norm = srv_id.lower().replace("-", "").replace("_", "").replace(".", "").replace(":", "")
            if norm and (norm in srv_norm or srv_norm in norm):
                cfg[model_file]["server_model_id"] = srv_id

    _save_model_configs(cfg)
    return {"status": "ok", "updated": len(server_models), "config": cfg}


async def _update_model_ids_on_startup():
    """On server startup, sync server_model_id from the running Llama Server."""
    await asyncio.sleep(3)  # let Llama Server finish starting
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{config.llama_host_url()}/v1/models")
            resp.raise_for_status()
            data = resp.json()
            server_models = [m["id"] for m in data.get("data", [])]
    except Exception as e:
        print(f"[MODEL-IDS] Could not fetch server models: {e}")
        return

    cfg = _load_model_configs()
    for model_file in cfg:
        norm = model_file.lower().replace("-", "").replace("_", "").replace(".", "").replace(":", "")
        for srv_id in server_models:
            srv_norm = srv_id.lower().replace("-", "").replace("_", "").replace(".", "").replace(":", "")
            if norm and (norm in srv_norm or srv_norm in norm):
                cfg[model_file]["server_model_id"] = srv_id

    _save_model_configs(cfg)
    for k, v in cfg.items():
        sid = v.get("server_model_id")
        if sid:
            print(f"[MODEL-IDS] {k} -> {sid}")


# ── Characters ──




# ── Characters ──


@app.get("/api/characters")
async def api_characters():
    chars = load_all_characters(config.characters_dir)
    return chars


@app.post("/api/characters")
async def api_create_character(data: dict):
    if not data.get("name", "").strip():
        raise HTTPException(400, "Character name is required")
    char = save_character(config.characters_dir, data)
    if not char:
        raise HTTPException(500, "Failed to save character")
    return char


@app.put("/api/characters/{char_id}")
async def api_update_character(char_id: str, data: dict):
    if not data.get("name", "").strip():
        raise HTTPException(400, "Character name is required")
    data["id"] = char_id
    char = save_character(config.characters_dir, data)
    if not char:
        raise HTTPException(500, "Failed to save character")
    return char


@app.delete("/api/characters/{char_id}")
async def api_delete_character(char_id: str):
    deleted = delete_character(config.characters_dir, char_id)
    if not deleted:
        raise HTTPException(404, "Character not found")
    return {"ok": True}


# ── Character State Cards (Tier 1) ──


@app.get("/api/characters/{char_id}/state")
async def api_get_character_state(char_id: str):
    return await db.get_character_state_cards(char_id)


@app.post("/api/characters/{char_id}/state")
async def api_create_character_state(char_id: str, data: dict):
    return await db.create_character_state_card(
        char_id,
        data["schema_id"],
        data["label"],
        data.get("data", {}),
        data.get("is_readonly", False),
    )


@app.put("/api/character-state/{card_id}")
async def api_update_character_state(card_id: int, data: dict):
    return await db.update_character_state_card(card_id, data.get("data", {}))


@app.delete("/api/character-state/{card_id}")
async def api_delete_character_state(card_id: int):
    await db.delete_character_state_card(card_id)
    return {"ok": True}


@app.post("/api/characters/{char_id}/duplicate")
async def api_duplicate_character(char_id: str):
    """Duplicate a character and its global state cards."""
    char = load_character(os.path.join(config.characters_dir, f"{char_id}.md"))
    if not char:
        raise HTTPException(404, "Character not found")
    # Generate unique ID
    base_name = char["name"] + " Copy"
    new_data = {**char, "name": base_name, "id": None}  # id=None → auto-slug
    new_char = save_character(config.characters_dir, new_data)
    if not new_char:
        raise HTTPException(500, "Failed to duplicate character")
    # Copy global state cards
    old_cards = await db.get_character_state_cards(char_id)
    for card in old_cards:
        card_data = (
            json.loads(card["data"]) if isinstance(card["data"], str) else card["data"]
        )
        await db.create_character_state_card(
            new_char["id"],
            card["schema_id"],
            card["label"],
            card_data,
            card.get("is_readonly", False),
        )
    return new_char


# ── Personas ──


@app.get("/api/personas")
async def api_personas():
    return load_all_personas("personas")


@app.post("/api/personas")
async def api_create_persona(data: dict):
    if not data.get("name", "").strip():
        raise HTTPException(400, "Persona name is required")
    persona = save_persona("personas", data)
    if not persona:
        raise HTTPException(500, "Failed to save persona")
    return persona


@app.put("/api/personas/{persona_id}")
async def api_update_persona(persona_id: str, data: dict):
    if not data.get("name", "").strip():
        raise HTTPException(400, "Persona name is required")
    data["id"] = persona_id
    persona = save_persona("personas", data)
    if not persona:
        raise HTTPException(500, "Failed to save persona")
    return persona


@app.delete("/api/personas/{persona_id}")
async def api_delete_persona(persona_id: str):
    deleted = delete_persona("personas", persona_id)
    if not deleted:
        raise HTTPException(404, "Persona not found")
    return {"ok": True}


# ── Lore ──


@app.get("/api/lore")
async def api_lore():
    return load_all_lore("lore")


@app.post("/api/lore")
async def api_create_lore(data: dict):
    if not data.get("name", "").strip():
        raise HTTPException(400, "Lore entry name is required")
    entry = save_lore("lore", data)
    if not entry:
        raise HTTPException(500, "Failed to save lore entry")
    return entry


@app.put("/api/lore/{lore_id}")
async def api_update_lore(lore_id: str, data: dict):
    if not data.get("name", "").strip():
        raise HTTPException(400, "Lore entry name is required")
    data["id"] = lore_id
    entry = save_lore("lore", data)
    if not entry:
        raise HTTPException(500, "Failed to save lore entry")
    return entry


@app.delete("/api/lore/{lore_id}")
async def api_delete_lore(lore_id: str):
    deleted = delete_lore("lore", lore_id)
    if not deleted:
        raise HTTPException(404, "Lore entry not found")
    return {"ok": True}


# ── Conversations ──


@app.get("/api/conversations")
async def api_list_conversations():
    return await db.list_conversations()


@app.get("/api/search")
async def api_search(q: str = ""):
    if len(q) < 2:
        return []
    return await db.search_conversations(q)


async def _search_workspace_catalog() -> list[dict]:
    conversations = await db.list_workspace_conversations()
    grouped: dict[str, dict] = {}
    for row in conversations:
        try:
            key = workspace_key(row["project_dir"])
        except ValueError:
            continue
        item = grouped.setdefault(key, {
            "workspace_key": key,
            "project_dir": row["project_dir"],
            "canonical_path": canonical_workspace(row["project_dir"]),
            "conversation_ids": [],
            "conversation_count": 0,
            "message_count": 0,
            "updated_at": 0,
            "titles": [],
        })
        item["conversation_ids"].append(int(row["conversation_id"]))
        item["conversation_count"] += 1
        item["message_count"] += int(row.get("message_count") or 0)
        item["updated_at"] = max(float(item["updated_at"]), float(row.get("updated_at") or 0))
        if len(item["titles"]) < 4:
            item["titles"].append(row.get("conversation_title") or "Untitled")
    policies = {row["workspace_key"]: row["policy"] for row in await asyncio.to_thread(_search_index.policies)}
    for key, item in grouped.items():
        item["policy"] = policies.get(key, "excluded")
    return sorted(grouped.values(), key=lambda row: row["updated_at"], reverse=True)


async def _sync_search_workspaces(keys: list[str]) -> list[dict]:
    catalog = {row["workspace_key"]: row for row in await _search_workspace_catalog()}
    missing = [key for key in keys if key not in catalog]
    if missing:
        raise HTTPException(404, f"Unknown workspace key(s): {', '.join(missing[:3])}")
    try:
        await SearchService(_search_index, _search_source_id(), keys, local=True).sync()
    except SearchPolicyError as exc:
        raise HTTPException(409, str(exc)) from exc
    return [catalog[key] for key in dict.fromkeys(keys)]


def _search_chat() -> SearchChat:
    return SearchChat(_search_index, _search_source_id())


@app.post("/api/search-space/sessions")
async def api_search_session_create(data: dict = Body(...)):
    keys = data.get("workspace_keys")
    if not isinstance(keys, list) or not 1 <= len(keys) <= 32 or not all(isinstance(k, str) for k in keys):
        raise HTTPException(400, "Select at least one workspace")
    await _sync_search_workspaces(keys)
    return _search_chat().create(list(dict.fromkeys(keys)))


@app.get("/api/search-space/sessions/{session_id}")
async def api_search_session_get(session_id: str):
    session = _search_chat().get(session_id)
    if not session:
        raise HTTPException(404, "Search conversation no longer exists or has expired")
    try:
        SearchOperator(_search_index, source_id=_search_source_id()).plan(session["workspace_keys"])
    except SearchPolicyError as exc:
        raise HTTPException(409, str(exc)) from exc
    return session


@app.get("/api/search-space/sessions")
async def api_search_sessions_list():
    return []


@app.post("/api/search-space/sessions/{session_id}/messages")
async def api_search_session_ask(session_id: str, data: dict = Body(...)):
    try:
        return await _search_chat().ask(session_id, data.get("question"))
    except SearchPolicyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except SearchBackendUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/search-space/sessions/{session_id}")
async def api_search_session_delete(session_id: str):
    _search_chat().delete(session_id)
    return {"deleted": True}


async def _history_service(request: Request) -> tuple[SearchService, object]:
    scope = resolve_scope(request.headers.get("x-loom-history", ""))
    same_source = scope is not None and scope.source_id == _search_source_id()
    if not scope or not same_source:
        raise HTTPException(403, "History capability is missing, expired or belongs to another Loom database")
    conv = await db.get_conversation(scope.conversation_id)
    if not conv or conv.get('backstage_parent_id'):
        raise HTTPException(403, "History is unavailable to this role")
    assigned_dir = conv.get("project_dir")
    if scope.run_id and not scope.run_id.startswith("gen:"):
        run = await db.get_agent_run(scope.run_id.removeprefix("agent:"))
        if not run or run["conversation_id"] != scope.conversation_id or run["status"] != "running":
            raise HTTPException(403, "History run is no longer active")
        assigned_dir = run["project_dir"]
    assigned = bool(assigned_dir and workspace_key(assigned_dir) == scope.workspace_key)
    policy_name = _search_index.policy_for(assigned_dir) if assigned else "excluded"
    policy = {"cloud": Policy.CLOUD, "sensitive": Policy.SENSITIVE}.get(policy_name, Policy.EXCLUDED)
    if not may_read(active=True, assigned=assigned, same_source=same_source, policy=policy, local=scope.local):
        raise HTTPException(403, "History scope is excluded, changed, or requires a local agent")
    return SearchService(_search_index, _search_source_id(), [scope.workspace_key], local=scope.local), scope


def _history_origin(request: Request, scope) -> str:
    # Prefer the actual browser connection which launched this run. This gives
    # remote users their Tailscale URL rather than an MCP loopback URL.
    websocket = _find_client_websocket(scope.conversation_id, scope.client_instance_id) if scope.client_instance_id else None
    if websocket is not None:
        from urllib.parse import urlsplit
        url = urlsplit(str(websocket.url))
        return f"{'https' if url.scheme == 'wss' else 'http'}://{url.netloc}"
    public = os.environ.get("LOOM_PUBLIC_URL", "").strip().rstrip("/")
    if public.startswith(("https://", "http://")):
        return public
    return str(request.base_url).rstrip("/")


@app.post("/api/history/{tool}")
async def api_history_tool(tool: str, request: Request, data: dict = Body(...)):
    service, scope = await _history_service(request)
    try:
        result = await service.call(tool, data)
        # Recheck the capability and assignment after asynchronous retrieval.
        await _history_service(request)
    except SearchPolicyError as exc:
        raise HTTPException(403, str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    origin = _history_origin(request, scope)
    def links(value):
        if isinstance(value, dict):
            return {key: origin + item if key == "url" and isinstance(item, str) and item.startswith("/?conversation=")
                    else links(item) for key, item in value.items()}
        if isinstance(value, list):
            return [links(item) for item in value]
        return value
    return links(result)


@app.get("/api/search-space/workspaces")
async def api_search_space_workspaces():
    return await _search_workspace_catalog()


@app.put("/api/search-space/workspaces/policy")
async def api_search_space_policy(data: dict = Body(...)):
    project_dir = str(data.get("project_dir") or "").strip()
    policy = str(data.get("policy") or "").strip().lower()
    catalog = await _search_workspace_catalog()
    known = {row["workspace_key"]: row for row in catalog}
    try:
        key = workspace_key(project_dir)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if key not in known:
        raise HTTPException(404, "Workspace is not used by a Loom conversation")
    try:
        result = await asyncio.to_thread(_search_index.set_policy, known[key]["project_dir"], policy)
        if policy == "excluded":
            _search_chat().discard_workspace(key)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return result


@app.post("/api/search-space/index")
async def api_search_space_index(data: dict = Body(...)):
    keys = data.get("workspace_keys") or []
    if not isinstance(keys, list) or not keys:
        raise HTTPException(400, "workspace_keys must be a non-empty list")
    return {"workspaces": await _sync_search_workspaces([str(key) for key in keys])}


@app.post("/api/search-space/query")
async def api_search_space_query(data: dict = Body(...)):
    question = str(data.get("question") or "").strip()
    keys = data.get("workspace_keys") or []
    if len(question) < 2 or not isinstance(keys, list) or not keys:
        raise HTTPException(400, "A question and at least one workspace are required")
    normalized = [str(key) for key in keys]
    await _sync_search_workspaces(normalized)
    operator = SearchOperator(_search_index, source_id=_search_source_id())
    try:
        return await operator.ask(question, normalized, limit=int(data.get("limit") or 24))
    except SearchPolicyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except SearchBackendUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/search-space/retrieve")
async def api_search_space_retrieve(data: dict = Body(...)):
    query = str(data.get("query") or "").strip()
    keys = data.get("workspace_keys") or []
    if len(query) < 2 or not isinstance(keys, list) or not keys:
        raise HTTPException(400, "A query and at least one workspace are required")
    normalized = [str(key) for key in keys]
    await _sync_search_workspaces(normalized)
    try:
        plan = SearchOperator(_search_index, source_id=_search_source_id()).plan(normalized)
    except SearchPolicyError as exc:
        raise HTTPException(409, str(exc)) from exc
    matches = await asyncio.to_thread(
        _search_index.search, [query], normalized, int(data.get("limit") or 24), _search_source_id()
    )
    return {**plan, "sources": matches}


@app.get("/api/conversations/{conv_id}/search")
async def api_search_conversation(conv_id: int, q: str = ""):
    if len(q) < 2:
        return []
    return await db.search_conversation_messages(conv_id, q)


@app.post("/api/conversations")
async def api_create_conversation(data: dict = None):
    data = data or {}
    title = data.get("title", "New Conversation")
    character_id = data.get("character_id")
    persona_id = data.get("persona_id")
    lore_ids = data.get("lore_ids", [])
    style_nudge = data.get("style_nudge", "Natural")
    mode = data.get("mode", "weave")
    if reason := model_registry.retirement_reason(mode, data.get("cc_model")):
        raise HTTPException(410, reason)
    project_dir = data.get("project_dir")
    parallel_agents_enabled = 1 if _truthy_setting(data.get("parallel_agents_enabled")) else 0
    pass

    first_turn = data.get("first_turn", "character")  # "character" or "user"
    custom_scene = data.get("custom_scene")

    cc_model = data.get("cc_model", "sonnet")
    cc_effort = data.get("cc_effort", "high")
    local_model = data.get("local_model")
    system_only = _truthy_setting(data.get("system_only")) and mode == "weave"
    system_prompt = data.get("system_prompt")
    if system_only:
        character_id = None
        persona_id = None
        lore_ids = []
        custom_scene = None
        first_turn = "user"

    pass

    if mode == "claude":
        resolved_mode = _mode_for_cc_model(cc_model)
        if resolved_mode is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown model selector '{cc_model}'; refresh the picker or choose an available model",
            )
        mode = resolved_mode
        is_umans = is_umans_model(cc_model)
        is_openrouter = openrouter_client.is_openrouter_model(cc_model)
        if is_umans or is_openrouter:
            _reject_disabled_umans_for_new_selection(cc_model)

    if parallel_agents_enabled:
        pass
        if mode not in _AGENT_WORKSPACE_MODES:
            raise HTTPException(400, "Parallel agents are only available for agent conversations")
        if not project_dir:
            raise HTTPException(400, "Parallel agents require a working directory")

    conv = await db.create_conversation(
        title, character_id, mode=mode, project_dir=project_dir
    )

    # Store additional fields
    import json as _json

    fields = dict(
        persona_id=persona_id,
        lore_ids=_json.dumps(lore_ids),
        style_nudge=style_nudge,
        custom_scene=custom_scene,
        cc_model=cc_model,
        cc_effort=cc_effort,
        ooda_enabled=0 if system_only else (1 if mode == "weave" else 0),
        system_only=1 if system_only else 0,
        system_prompt=(system_prompt.strip() if isinstance(system_prompt, str) and system_prompt.strip() else None),
        parallel_agents_enabled=parallel_agents_enabled,
    )
    pass
    if mode in ("local", "weave") and local_model:
        fields["local_model"] = local_model
    await db.update_conversation_fields(conv["id"], **fields)
    # Refresh conv data
    conv = await db.get_conversation(conv["id"])

    # Auto-seed state cards for OODA-enabled Weave conversations
    if mode == "weave" and character_id:
        global_cards = await db.get_character_state_cards(character_id)
        if global_cards:
            await db.copy_character_state_to_conversation(character_id, conv["id"])
        else:
            char = load_character(
                os.path.join(config.characters_dir, f"{character_id}.md")
            )
            if char:
                await db.create_state_card(
                    conv["id"],
                    "character_state",
                    char.get("name", "Character"),
                    {
                        "personality": char.get("personality", ""),
                        "appearance": "",
                        "current_mood": "",
                        "current_goal": "",
                        "physical_state": "",
                        "speech_pattern": "",
                        "relationship_to_player": "",
                        "secrets": "",
                    },
                )
                if char.get("scenario"):
                    await db.create_state_card(
                        conv["id"],
                        "scene_state",
                        "current",
                        {
                            "location": "",
                            "time_of_day": "",
                            "atmosphere": "",
                            "characters_present": "",
                            "recent_events": char["scenario"],
                            "tension_level": "",
                        },
                    )
        if persona_id:
            persona = load_persona(os.path.join("personas", f"{persona_id}.md"))
            if persona:
                await db.create_state_card(
                    conv["id"],
                    "persona_state",
                    persona["name"],
                    {
                        "description": persona.get("content", ""),
                        "appearance": "",
                        "goals": "",
                    },
                )
        if lore_ids:
            for lid in lore_ids:
                entry = load_lore_entry(os.path.join("lore", f"{lid}.md"))
                if entry:
                    await db.create_state_card(
                        conv["id"],
                        "lore",
                        entry["name"],
                        {
                            "content": entry["content"],
                        },
                        is_readonly=True,
                    )

    # If character goes first:
    #   - Custom scene → add it as a user message so the model responds to it
    #   - No custom scene + greeting exists → use the static greeting
    #   - No custom scene + no greeting → leave empty, client triggers generation
    if first_turn == "character" and character_id:
        char = load_character(os.path.join(config.characters_dir, f"{character_id}.md"))
        if custom_scene:
            # Add custom scene as a user message for the model to respond to
            scene_msg = await db.add_message(conv["id"], "user", custom_scene)
            await db.set_active_branch(conv["id"], scene_msg["id"])
        elif char and char.get("greeting"):
            greeting_msg = await db.add_message(
                conv["id"], "assistant", char["greeting"]
            )
            await db.set_active_branch(conv["id"], greeting_msg["id"])

    return conv


@app.post("/api/casual-chats")
async def api_create_casual_chat(data: dict = Body(...)):
    """Explicit casual intent; leave ordinary conversation defaults untouched."""
    request_id = str(data.get("request_id") or "").strip()
    model = str(data.get("cc_model") or "").strip()
    effort = str(data.get("cc_effort") or "high").strip()
    if not request_id or len(request_id) > 128:
        raise HTTPException(400, "A bounded chat request ID is required")
    mode = _mode_for_cc_model(model)
    if mode is None or model_registry.retirement_reason(mode, model):
        raise HTTPException(400, "Choose an available agent model for the new chat")
    if is_umans_model(model) or openrouter_client.is_openrouter_model(model):
        _reject_disabled_umans_for_new_selection(model)
    workspace = _CASUAL_CHAT_ROOT
    if workspace.is_symlink() or workspace.is_junction():
        raise HTTPException(400, "ExoHome must be an ordinary workspace directory")
    try:
        workspace.mkdir(exist_ok=True)
    except OSError as exc:
        raise HTTPException(400, "ExoHome is unavailable; check its directory permissions") from exc
    try:
        return await db.create_casual_conversation(
            request_id, str(data.get("title") or "New chat")[:120], mode,
            str(workspace.resolve()), model, effort,
        )
    except db.DeletedCasualChatRequest as exc:
        raise HTTPException(410, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/conversations/{conv_id}/codex-goal")
async def api_update_codex_goal(conv_id: int, data: dict):
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    if conv.get("mode") != "codex" or not is_codex_model(conv.get("cc_model") or ""):
        raise HTTPException(
            400, "Codex goal /goal is only available for Codex conversations"
        )

    action = (data.get("action") or "get").lower()
    objective = data.get("objective")
    status = data.get("status")
    token_budget = data.get("token_budget")

    if action == "set" and status is None:
        status = "active"

    if action == "pause":
        action = "set"
        status = "paused"
    elif action == "resume":
        action = "set"
        status = "active"

    def _loom_goal():
        return {
            "objective": conv.get("codex_goal_objective"),
            "status": conv.get("codex_goal_status"),
            "tokenBudget": conv.get("codex_goal_token_budget"),
            "tokensUsed": conv.get("codex_goal_tokens_used") or 0,
            "timeUsedSeconds": conv.get("codex_goal_time_used_seconds") or 0,
        }

    session_id = conv.get("claude_session_id")

    if action == "get" and not session_id:
        return {"source": "loom", "goal": _loom_goal()}

    cwd = conv.get("project_dir") or str(Path(__file__).parent / "workspaces" / "codex")
    Path(cwd).mkdir(parents=True, exist_ok=True)

    result = await codex_client.manage_codex_goal(
        action=action,
        cwd=cwd,
        conv_id=conv_id,
        server_port=config.port,
        model=conv.get("cc_model") or "Codex (GPT-4o)",
        permission_mode=conv.get("cc_permission_mode") or "default",
        resume_session_id=session_id,
        objective=objective,
        status=status,
        token_budget=token_budget,
    )

    goal = result.get("goal") or {}
    thread_id = result.get("thread_id")
    await db.update_conversation_fields(
        conv_id,
        claude_session_id=thread_id,
        codex_goal_objective=goal.get("objective"),
        codex_goal_status=goal.get("status"),
        codex_goal_token_budget=goal.get("tokenBudget"),
        codex_goal_tokens_used=goal.get("tokensUsed"),
        codex_goal_time_used_seconds=goal.get("timeUsedSeconds"),
        codex_goal_updated_at=time.time(),
    )
    return result


@app.get("/api/conversations/{conv_id}")
async def api_get_conversation(conv_id: int):
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    # Lazy-migrate legacy local model names to loaded model filenames if they match
    if conv.get("local_model") and config.llama_model:
        local_model = conv["local_model"]
        if models_match(local_model, config.llama_model):
            if local_model != config.llama_model:
                conv["local_model"] = config.llama_model
                await db.update_conversation_fields(conv_id, local_model=config.llama_model)
    # Lazy-migrate legacy local model names in cc_model to loaded model filenames if they match
    if conv.get("cc_model") and config.llama_model:
        cc_model = conv["cc_model"]
        is_api = any(
            cc_model.startswith(prefix)
            for prefix in ("claude-", "fable", "sonnet", "haiku", "opus", "gemini", "Gemini", "gpt", "GPT", "goose:", "openrouter:", "dream:", "umans-", "codex", "o3", "o4")
        )
        if not is_api and models_match(cc_model, config.llama_model):
            if cc_model != config.llama_model:
                conv["cc_model"] = config.llama_model
                await db.update_conversation_fields(conv_id, cc_model=config.llama_model)
    if conv.get("cc_model") and (conv.get("mode") or "").lower() in {"claude", "codex", "gemini", "goose", "umans", "openrouter"}:
        expected_mode = _mode_for_cc_model(conv.get("cc_model"))
        if expected_mode and expected_mode != conv.get("mode"):
            conv["mode"] = expected_mode
            await db.update_conversation_fields(conv_id, mode=expected_mode)
    # Lazy-mint a slug for conversations that had canvas enabled before
    # the canvas_slug column existed.
    if conv.get("canvas_enabled") and not conv.get("canvas_slug"):
        for _ in range(5):
            candidate = generate_canvas_slug()
            if not await db.get_conversation_by_canvas_slug(candidate):
                conv["canvas_slug"] = candidate
                await db.update_conversation_fields(conv_id, canvas_slug=candidate)
                break
    return conv


@app.put("/api/conversations/{conv_id}")
async def api_update_conversation(conv_id: int, data: dict):
    """Update conversation settings (style_nudge, persona_id, lore_ids, etc.)."""
    import json as _json

    fields = {}
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if "style_nudge" in data:
        fields["style_nudge"] = data["style_nudge"]
    if "persona_id" in data:
        fields["persona_id"] = data["persona_id"]
    if "lore_ids" in data:
        fields["lore_ids"] = _json.dumps(data["lore_ids"])
    if "title" in data:
        fields["title"] = data["title"]
    if "custom_scene" in data:
        fields["custom_scene"] = data["custom_scene"]
    if "starred" in data:
        fields["starred"] = int(data["starred"])
    if "nsfw" in data:
        fields["nsfw"] = int(data["nsfw"])
    if "cc_model" in data:
        cc_model = data["cc_model"]
        if reason := model_registry.retirement_reason(None, cc_model):
            raise HTTPException(410, reason)
        resolved_mode = _mode_for_cc_model(cc_model)
        if resolved_mode is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown model selector '{cc_model}'; refresh the picker or choose an available model",
            )
        fields["cc_model"] = cc_model
        fields["mode"] = resolved_mode
    if "cc_effort" in data:
        fields["cc_effort"] = data["cc_effort"]
    if "cc_permission_mode" in data:
        fields["cc_permission_mode"] = data["cc_permission_mode"]
    if "permission_profile" in data:
        if not isinstance(data["permission_profile"], str) or data["permission_profile"] not in {p.value for p in permission_policy_model.PermissionProfile}:
            raise HTTPException(400, "Unknown Loom permission profile")
        fields["permission_profile"] = data["permission_profile"]
    if "local_model" in data:
        fields["local_model"] = data["local_model"]
    if "ooda_enabled" in data:
        conv = await db.get_conversation(conv_id)
        fields["ooda_enabled"] = 0 if conv and _truthy_setting(conv.get("system_only")) else int(_truthy_setting(data["ooda_enabled"]))
    if "system_only" in data:
        fields["system_only"] = int(_truthy_setting(data["system_only"]))
        if fields["system_only"]:
            fields["ooda_enabled"] = 0
    if "system_prompt" in data:
        value = data["system_prompt"]
        fields["system_prompt"] = value.strip() if isinstance(value, str) and value.strip() else None
    if "folder" in data:
        fields["folder"] = data["folder"]
    if "parallel_agents_enabled" in data:
        desired = 1 if _truthy_setting(data.get("parallel_agents_enabled")) else 0
        if desired:
            mode = fields.get("mode") or conv.get("mode")
            pass
            if mode not in _AGENT_WORKSPACE_MODES:
                raise HTTPException(400, "Parallel agents are only available for agent conversations")
            if not conv.get("project_dir"):
                raise HTTPException(400, "Parallel agents require a working directory")
        fields["parallel_agents_enabled"] = desired
    if fields:
        await db.update_conversation_fields(conv_id, **fields)
    return await db.get_conversation(conv_id)


@app.delete("/api/conversations/{conv_id}")
async def api_delete_conversation(conv_id: int):
    await db.delete_conversation(conv_id)
    return {"ok": True}


# ── Bookmarks ──


@app.get("/api/bookmarks")
async def api_get_all_bookmarks():
    return await db.get_all_bookmarks()


@app.get("/api/agent-notification-dismissals")
async def api_agent_notification_dismissals():
    return await db.get_agent_notification_dismissals()


@app.post("/api/agent-notification-dismissals")
async def api_dismiss_agent_notifications(data: dict):
    keys = data.get('keys')
    if not isinstance(keys, list) or len(keys) > 2048 or any(
        not isinstance(key, str) or len(key) > 512 for key in keys
    ):
        raise HTTPException(status_code=422, detail="Invalid notification keys")
    await db.dismiss_agent_notifications(keys)
    return await db.get_agent_notification_dismissals()


@app.get("/api/bookmarks/{bookmark_id}/message")
async def api_bookmark_message(bookmark_id: int):
    bookmarks = await db.get_all_bookmarks()
    bookmark = next((item for item in bookmarks if item['id'] == bookmark_id), None)
    if bookmark is None:
        raise HTTPException(status_code=404, detail="Pin not found")
    message = await db.get_message(bookmark['message_id'])
    if message is None or message['conversation_id'] != bookmark['conversation_id']:
        raise HTTPException(status_code=404, detail="Message not found")
    return message


@app.get("/api/conversations/{conv_id}/bookmarks")
async def api_get_bookmarks(conv_id: int):
    return await db.get_bookmarks(conv_id)


@app.post("/api/conversations/{conv_id}/bookmarks")
async def api_add_bookmark(conv_id: int, data: dict):
    return await db.add_bookmark(
        conv_id,
        data["message_id"],
        data.get("branch_name", ""),
        data.get("description", ""),
    )


@app.put("/api/bookmarks/{bookmark_id}")
async def api_update_bookmark(bookmark_id: int, data: dict):
    return await db.update_bookmark(bookmark_id, data.get("description", ""))


@app.delete("/api/bookmarks/{bookmark_id}")
async def api_delete_bookmark(bookmark_id: int):
    await db.delete_bookmark(bookmark_id)
    return {"ok": True}


# ── Branch Pins ──


def _branch_pin_label(data: dict) -> str:
    label = data.get("label", "")
    if not isinstance(label, str):
        raise HTTPException(status_code=400, detail="label must be a string")
    label = label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="label is required")
    if len(label) > 120:
        raise HTTPException(status_code=400, detail="label must be 120 characters or fewer")
    return label


@app.get("/api/conversations/{conv_id}/branch-pins")
async def api_get_branch_pins(conv_id: int):
    return await db.get_branch_pins(conv_id)


@app.post("/api/conversations/{conv_id}/branch-pins")
async def api_add_branch_pin(conv_id: int, data: dict):
    message_id = data.get("message_id")
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        raise HTTPException(status_code=400, detail="message_id must be an integer")
    pin = await db.add_branch_pin(conv_id, message_id, _branch_pin_label(data))
    if not pin:
        raise HTTPException(status_code=404, detail="message not found in this conversation")
    return pin


@app.put("/api/branch-pins/{pin_id}")
async def api_update_branch_pin(pin_id: int, data: dict):
    pin = await db.update_branch_pin(pin_id, _branch_pin_label(data))
    if not pin:
        raise HTTPException(status_code=404, detail="branch pin not found")
    return pin


@app.delete("/api/branch-pins/{pin_id}")
async def api_delete_branch_pin(pin_id: int):
    if not await db.delete_branch_pin(pin_id):
        raise HTTPException(status_code=404, detail="branch pin not found")
    return {"ok": True}


# ── State Cards ──


@app.get("/api/state-schemas")
async def api_get_state_schemas():
    return await db.get_state_schemas()


@app.get("/api/conversations/{conv_id}/state")
async def api_get_state_cards(conv_id: int, schema_id: str = None):
    return await db.get_state_cards(conv_id, schema_id)


@app.post("/api/conversations/{conv_id}/state")
async def api_create_state_card(conv_id: int, data: dict):
    return await db.create_state_card(
        conv_id,
        data["schema_id"],
        data["label"],
        data.get("data", {}),
        data.get("is_readonly", False),
    )


@app.put("/api/state/{card_id}")
async def api_update_state_card(card_id: int, data: dict):
    return await db.update_state_card(card_id, data.get("data", {}))


@app.delete("/api/state/{card_id}")
async def api_delete_state_card(card_id: int):
    await db.delete_state_card(card_id)
    return {"ok": True}


# Conversation-scoped variants — used by the backstage MCP server so a
# compromised agent can't mutate cards in other conversations by guessing ids.
@app.put("/api/conversations/{conv_id}/state/{card_id}")
async def api_update_scoped_state_card(conv_id: int, card_id: int, data: dict):
    existing = await db.get_state_card(card_id) if hasattr(db, "get_state_card") else None
    if existing is None:
        # Fallback: fetch via the conversation list
        all_cards = await db.get_state_cards(conv_id)
        existing = next((c for c in all_cards if c.get("id") == card_id), None)
    if not existing or existing.get("conversation_id") != conv_id:
        raise HTTPException(404, "Card not found in this conversation")
    return await db.update_state_card(card_id, data.get("data", {}))


@app.delete("/api/conversations/{conv_id}/state/{card_id}")
async def api_delete_scoped_state_card(conv_id: int, card_id: int):
    all_cards = await db.get_state_cards(conv_id)
    if not any(c.get("id") == card_id for c in all_cards):
        raise HTTPException(404, "Card not found in this conversation")
    await db.delete_state_card(card_id)
    return {"ok": True}


@app.get("/api/conversations/{conv_id}/branch-state/{msg_id}")
async def api_get_branch_state(conv_id: int, msg_id: int):
    """Get effective state for a specific branch point (base + deltas)."""
    return await db.get_branch_state(conv_id, msg_id)


@app.post("/api/conversations/{conv_id}/backstage")
async def api_get_or_create_backstage(conv_id: int):
    """Return the backstage conversation for this parent, creating on first call."""
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    if conv.get("backstage_parent_id"):
        raise HTTPException(400, "Cannot create backstage of a backstage conversation")
    return await db.get_or_create_backstage(conv_id)


@app.post("/api/conversations/{conv_id}/state/seed")
async def api_seed_state_cards(conv_id: int):
    """Auto-seed state cards from the conversation's character, persona, and lore."""
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")

    cards_created = []

    # Seed from character — prefer Tier 1 global state cards, fallback to text extraction
    if conv.get("character_id"):
        char_id = conv["character_id"]
        global_cards = await db.get_character_state_cards(char_id)
        if global_cards:
            # Copy Tier 1 → Tier 2
            count = await db.copy_character_state_to_conversation(char_id, conv_id)
            if count:
                cards_created.extend(await db.get_state_cards(conv_id))
        else:
            # Fallback: extract from character text
            char = load_character(os.path.join(config.characters_dir, f"{char_id}.md"))
            if char:
                card = await db.create_state_card(
                    conv_id,
                    "character_state",
                    char.get("name", "Character"),
                    {
                        "personality": char.get("personality", ""),
                        "appearance": "",
                        "current_mood": "",
                        "current_goal": "",
                        "physical_state": "",
                        "speech_pattern": "",
                        "relationship_to_player": "",
                        "secrets": "",
                    },
                )
                if card:
                    cards_created.append(card)
                if char.get("scenario"):
                    scene = await db.create_state_card(
                        conv_id,
                        "scene_state",
                        "current",
                        {
                            "location": "",
                            "time_of_day": "",
                            "atmosphere": "",
                            "characters_present": "",
                            "recent_events": char["scenario"],
                            "tension_level": "",
                        },
                    )
                    if scene:
                        cards_created.append(scene)

    # Seed persona as persona_state card
    if conv.get("persona_id"):
        persona = load_persona(os.path.join("personas", f"{conv['persona_id']}.md"))
        if persona:
            card = await db.create_state_card(
                conv_id,
                "persona_state",
                persona["name"],
                {
                    "description": persona.get("content", ""),
                    "appearance": "",
                    "goals": "",
                },
            )
            if card:
                cards_created.append(card)

    # Seed from lore
    if conv.get("lore_ids"):
        try:
            lore_ids = (
                json.loads(conv["lore_ids"])
                if isinstance(conv["lore_ids"], str)
                else conv["lore_ids"]
            )
        except (ValueError, TypeError):
            lore_ids = []
        for lid in lore_ids:
            entry = load_lore_entry(os.path.join("lore", f"{lid}.md"))
            if entry:
                card = await db.create_state_card(
                    conv_id,
                    "lore",
                    entry["name"],
                    {
                        "content": entry["content"],
                    },
                    is_readonly=True,
                )
                if card:
                    cards_created.append(card)

    return {"seeded": len(cards_created), "cards": cards_created}


# ── Tree ──

GREEK = [
    "α",
    "β",
    "γ",
    "δ",
    "ε",
    "ζ",
    "η",
    "θ",
    "ι",
    "κ",
    "λ",
    "μ",
    "ν",
    "ξ",
    "ο",
    "π",
    "ρ",
    "σ",
    "τ",
    "υ",
    "φ",
    "χ",
    "ψ",
    "ω",
]


def _compute_branch_names(tree: list[dict]) -> dict[int, str]:
    """Compute branch position labels (matching tree.js UI) for all nodes."""
    node_map = {n["id"]: n for n in tree}
    children_map: dict[int | None, list[int]] = {}
    roots = []
    for n in tree:
        pid = n.get("parent_id")
        if pid is None:
            roots.append(n["id"])
        else:
            children_map.setdefault(pid, []).append(n["id"])
    # Sort children by id (creation order) to match JS
    for k in children_map:
        children_map[k].sort()
    roots.sort()

    names = {}

    def _get_label(depth):
        return GREEK[depth] if depth < len(GREEK) else f"branch{depth}"

    def walk(node_id, prefix, pos, fork_depth):
        label = f"{prefix}{pos}" if prefix else f"{pos}"
        names[node_id] = label
        kids = children_map.get(node_id, [])
        if len(kids) == 1:
            walk(kids[0], prefix, pos + 1, fork_depth)
        elif len(kids) > 1:
            for i, kid in enumerate(kids):
                fl = _get_label(i)
                new_prefix = f"{prefix}{pos}.{fl}" if prefix else f"{pos}.{fl}"
                walk(kid, new_prefix, 1, fork_depth + 1)

    for i, root in enumerate(roots):
        root_label = _get_label(i) if len(roots) > 1 else ""
        walk(root, root_label, 1, 0)
    return names


@app.get("/api/conversations/{conv_id}/tree")
async def api_get_tree(conv_id: int):
    tree = await db.get_conversation_tree(conv_id)
    return tree


@app.get("/api/conversations/{conv_id}/tree/map")
async def api_get_tree_map(conv_id: int):
    """Return tree with branch position labels and session info for debugging."""
    d = await db.get_db()
    rows = await d.execute_fetchall(
        """SELECT id, parent_id, role, cc_session_id,
                  length(content) as content_len,
                  length(content_blocks) as blocks_len,
                  summary
           FROM messages WHERE conversation_id = ?
           ORDER BY created_at""",
        (conv_id,),
    )
    nodes = [dict(r) for r in rows]
    names = _compute_branch_names(nodes)
    result = []
    for n in nodes:
        sess = n.get("cc_session_id")
        result.append(
            {
                "pos": names.get(n["id"], "?"),
                "id": n["id"],
                "parent": n.get("parent_id"),
                "role": n["role"][0],  # u/a
                "session": sess[:8] if sess else None,
                "content": n.get("content_len") or 0,
                "blocks": n.get("blocks_len") or 0,
                "summary": (n.get("summary") or "")[:50],
            }
        )
    return result


@app.put("/api/conversations/{conv_id}/messages/{msg_id}")
async def api_update_message(conv_id: int, msg_id: int, data: dict):
    """Update a user message's content in-place (no new branch)."""
    existing = await db.get_message(msg_id)
    if not existing or int(existing.get("conversation_id") or 0) != int(conv_id):
        raise HTTPException(404, "Message not found in this conversation")
    content = data.get("content", "").strip()
    if not content:
        return {"ok": False, "error": "Content cannot be empty"}
    image_path = data.get("image_path")
    await db.update_message_content(msg_id, content=content)
    if image_path is not None:
        d = await get_db()
        ip = json.dumps(image_path) if isinstance(image_path, list) else image_path
        await d.execute("UPDATE messages SET image_path = ? WHERE id = ?", (ip, msg_id))
        await d.commit()
    msg = await db.get_message(msg_id)
    return msg


@app.delete("/api/conversations/{conv_id}/messages/{msg_id}")
async def api_delete_branch(conv_id: int, msg_id: int):
    """Delete a message and its entire subtree."""
    existing = await db.get_message(msg_id)
    if not existing or int(existing.get("conversation_id") or 0) != int(conv_id):
        raise HTTPException(404, "Message not found in this conversation")
    queued_before_ids = {
        item["user_msg_id"]
        for item in await db.list_queued_generations(conv_id)
    }
    result = await db.delete_branch(msg_id)
    if result["deleted"] == 0:
        return {"ok": False, "error": "Message not found"}

    # If we deleted part of the active branch, re-activate from parent or first remaining root
    if result.get("parent_id"):
        await db.set_active_branch(conv_id, result["parent_id"])
    else:
        # Deleted a root — try to activate another root if any exist
        tree = await db.get_conversation_tree(conv_id)
        if tree:
            # Find a leaf to activate
            ids = {n["id"] for n in tree}
            parent_ids = {n["parent_id"] for n in tree if n["parent_id"]}
            leaves = ids - parent_ids
            if leaves:
                await db.set_active_branch(conv_id, next(iter(leaves)))

    queued_after_ids = {
        item["user_msg_id"]
        for item in await db.list_queued_generations(conv_id)
    }
    removed_queue_ids = sorted(queued_before_ids - queued_after_ids)
    if removed_queue_ids:
        await _ws_send(
            conv_id,
            {
                "type": "queue_changed",
                "queued": False,
                "user_msg_id": removed_queue_ids[0],
                "user_msg_ids": removed_queue_ids,
            },
        )

    return {"ok": True, "deleted": result["deleted"]}


# ── Branch ──


@app.get("/api/conversations/{conv_id}/branch/{leaf_id}")
async def api_get_branch(conv_id: int, leaf_id: int):
    leaf = await db.get_message(leaf_id)
    if not leaf or int(leaf.get("conversation_id") or 0) != int(conv_id):
        raise HTTPException(404, "Branch message not found in this conversation")
    branch = await db.get_branch_to_root(leaf_id)
    return branch


@app.post("/api/conversations/{conv_id}/switch-branch/{leaf_id}")
async def api_switch_branch(conv_id: int, leaf_id: int):
    leaf = await db.get_message(leaf_id)
    if not leaf or int(leaf.get("conversation_id") or 0) != int(conv_id):
        raise HTTPException(404, "Branch message not found in this conversation")
    # Walk down from clicked node to deepest descendant (follow first/latest child)
    current = leaf_id
    while True:
        children = await db.get_children(current)
        if not children:
            break
        current = max(children, key=lambda c: c.get("created_at", 0))["id"]
    await db.set_active_branch(conv_id, current)
    branch = await db.get_active_branch(conv_id)
    return branch


# ── Messages ──


@app.get("/api/conversations/{conv_id}/threads")
async def api_list_loom_threads(conv_id: int):
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    return await db.list_loom_threads(conv_id)


@app.get("/api/conversations/{conv_id}/cursor/{client_instance_id}")
async def api_get_loom_cursor(conv_id: int, client_instance_id: str):
    cursor = await db.get_client_loom_cursor(conv_id, client_instance_id)
    if not cursor:
        raise HTTPException(404, "No Loom cursor exists for this browser page")
    return cursor


@app.post("/api/conversations/{conv_id}/cursor")
async def api_select_loom_cursor(conv_id: int, data: dict):
    try:
        cursor = await db.select_loom_cursor(
            conv_id,
            str(data.get("client_instance_id") or ""),
            str(data.get("loom_thread_id") or ""),
            data.get("leaf_message_id"),
            expected_revision=data.get("expected_revision"),
        )
        await _prune_client_generation_subscriptions(
            conv_id, str(data.get("client_instance_id") or "")
        )
        return cursor
    except db.LoomCursorConflict as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/conversations/{conv_id}/threads")
async def api_create_loom_thread(conv_id: int, data: dict):
    """Create an empty thread and supersede this page's older cursor."""
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    client_instance_id = str(data.get("client_instance_id") or "").strip()
    if not client_instance_id:
        raise HTTPException(400, "client_instance_id is required")
    existing_cursor = await db.get_client_loom_cursor(conv_id, client_instance_id)
    expected_revision = data.get("expected_revision")
    if expected_revision is not None:
        try:
            expected_revision = int(expected_revision)
        except (TypeError, ValueError):
            raise HTTPException(400, "expected_revision must be an integer")
    if existing_cursor and (
        expected_revision is None
        or int(existing_cursor["revision"]) != int(expected_revision)
    ):
        raise HTTPException(409, "The Loom cursor changed before the new thread was created")
    thread = await db.create_loom_thread(
        conv_id, client_instance_id=client_instance_id
    )
    try:
        cursor = await db.select_loom_cursor(
            conv_id,
            client_instance_id,
            thread["thread_id"],
            None,
            expected_revision=expected_revision,
        )
    except db.LoomCursorConflict as exc:
        connection = await db.get_db()
        await connection.execute(
            "DELETE FROM loom_threads WHERE thread_id = ? AND root_message_id IS NULL",
            (thread["thread_id"],),
        )
        await connection.commit()
        raise HTTPException(409, str(exc))
    await _prune_client_generation_subscriptions(conv_id, client_instance_id)
    return {"thread": thread, "cursor": cursor}


@app.post("/api/conversations/{conv_id}/messages")
async def api_add_message(conv_id: int, data: dict):
    role = data.get("role", "user")
    content = data.get("content", "")
    raw_image_path = data.get("image_path")
    # ``parent_id`` is an explicit topology value. Omitting it is never an
    # instruction to consult the conversation-global active branch.
    parent_id_provided = "parent_id" in data
    parent_id = data.get("parent_id")
    target_anchor_id = parent_id
    client_instance_id = str(data.get("client_instance_id") or "").strip() or None
    requested_send_intent = str(data.get("send_intent") or "continue").strip().lower()
    topology_intent = str(data.get("topology_intent") or "").strip().lower()
    if requested_send_intent not in {"continue", "parallel", "new_thread"}:
        raise HTTPException(400, "send_intent must be 'continue', 'parallel', or 'new_thread'")
    if topology_intent and topology_intent not in {"continue", "branch", "new_thread"}:
        raise HTTPException(400, "topology_intent must be 'continue', 'branch', or 'new_thread'")
    is_new_thread = requested_send_intent == "new_thread" or topology_intent == "new_thread"
    send_intent = "new_thread" if is_new_thread else requested_send_intent
    if is_new_thread and (not parent_id_provided or parent_id is not None):
        raise HTTPException(400, "A new thread requires an explicit null parent_id")

    # Normalize image_path: accept string, list, or null → store as JSON array or null
    if isinstance(raw_image_path, list):
        image_path = json.dumps(raw_image_path) if raw_image_path else None
    elif raw_image_path:
        image_path = raw_image_path  # legacy single string
    else:
        image_path = None

    if not content.strip() and not image_path:
        raise HTTPException(400, "Message content required")

    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    client_request_id = str(data.get("client_request_id") or "").strip() or None
    if reason := model_registry.retirement_reason(
        conv.get("mode"), data.get("cc_model") or conv.get("cc_model")
    ):
        raise HTTPException(410, reason)
    if client_request_id and len(client_request_id) > 128:
        raise HTTPException(400, "client_request_id is too long")
    if role == "user" and client_request_id:
        existing_message = await db.get_message_by_client_request(
            conv_id, client_request_id
        )
        if existing_message:
            # Idempotency is checked before cursor validation: a successful
            # first attempt already advanced that cursor, so its network retry
            # must return the original row rather than fail stale or relaunch.
            existing_message["idempotent_replay"] = True
            existing_message["generation_ids"] = []
            queued_rows = await db.list_queued_generations(conv_id)
            queued_row = next(
                (
                    item for item in queued_rows
                    if int(item.get("user_msg_id")) == int(existing_message["id"])
                ),
                None,
            )
            existing_message["queued"] = bool(queued_row)
            if queued_row:
                existing_message["queue_after_draft_id"] = queued_row.get(
                    "wait_for_draft_id"
                )
                existing_message["queue_status"] = queued_row.get("status")
            return existing_message
    parallel_enabled = _truthy_setting(conv.get("parallel_agents_enabled"))
    if role == "user" and send_intent == "parallel" and not parallel_enabled:
        raise HTTPException(
            409,
            "Parallel message launches are disabled for this conversation",
        )
    if not parent_id_provided:
        raise HTTPException(
            400,
            "parent_id is required; Loom never infers a message target from the active branch",
        )

    # An explicit null is a root request. User roots additionally require the
    # new-thread cursor contract below; internal/system callers still have to
    # say null rather than inheriting mutable global state.
    if parent_id is not None:
        parent = await db.get_message(parent_id)
        if not parent or parent.get("conversation_id") != conv_id:
            raise HTTPException(400, "parent_id does not belong to this conversation")

    cursor_contract = None
    loom_thread_id = str(data.get("loom_thread_id") or "").strip() or None
    launch_generation = role == "user" and bool(data.get("launch_generation", False))
    if role == "user":
        required = [
            key
            for key in (
                "loom_thread_id",
                "cursor_id",
                "cursor_revision",
                "expected_leaf_id",
                "topology_intent",
            )
            if key not in data
        ]
        if required or not client_instance_id:
            missing = required + ([] if client_instance_id else ["client_instance_id"])
            raise HTTPException(
                409,
                "This browser is using Loom's legacy message contract; reload before sending "
                f"(missing: {', '.join(missing)})",
            )
        expected_leaf_id = data.get("expected_leaf_id")
        same_expected_parent = (
            (expected_leaf_id is None and parent_id is None)
            or str(expected_leaf_id) == str(parent_id)
        )
        if not same_expected_parent:
            raise HTTPException(409, "The requested parent does not match this tab's expected leaf")
        try:
            cursor_revision = int(data.get("cursor_revision"))
        except (TypeError, ValueError):
            raise HTTPException(400, "cursor_revision must be an integer")
        cursor_contract = await db.get_loom_cursor(str(data.get("cursor_id")))
        cursor_leaf_matches = bool(
            cursor_contract
            and (
                (cursor_contract.get("leaf_message_id") is None and expected_leaf_id is None)
                or str(cursor_contract.get("leaf_message_id")) == str(expected_leaf_id)
            )
        )
        if not cursor_contract or not (
            int(cursor_contract["conversation_id"]) == int(conv_id)
            and str(cursor_contract["client_instance_id"]) == str(client_instance_id)
            and str(cursor_contract["thread_id"]) == str(loom_thread_id)
            and int(cursor_contract["revision"]) == cursor_revision
            and cursor_leaf_matches
        ):
            raise HTTPException(409, "The Loom thread cursor changed; no message was attached")
        thread = await db.get_loom_thread(loom_thread_id)
        if not thread or int(thread["conversation_id"]) != int(conv_id):
            raise HTTPException(400, "loom_thread_id does not belong to this conversation")
        if parent_id is not None and str(parent.get("loom_thread_id") or "") != loom_thread_id:
            raise HTTPException(409, "The selected leaf belongs to a different Loom thread")
        if is_new_thread:
            if thread.get("root_message_id") is not None:
                raise HTTPException(409, "This standalone Loom thread already has a root message")
        elif parent_id is None:
            raise HTTPException(409, "Only an explicit new_thread action may create a root message")

    # Normal sends continue the branch selected by this request. If that exact
    # branch has an active assistant draft, the user message belongs after the
    # draft and its generation intent is persisted below. An explicit parallel
    # send deliberately keeps the supplied anchor and creates a sibling branch.
    queue_after_draft_id = None
    if role == "user" and send_intent == "continue" and topology_intent != "branch":
        queue_after_draft_id = await _resolve_queue_target(
            conv_id,
            parent_id,
            data.get("queue_after_draft_id"),
        )
        if queue_after_draft_id is not None:
            parent_id = queue_after_draft_id

    # An ordinary UI send persists the message and starts its run in this one
    # server request. The browser is not the owner of a second, reconnectable
    # "generate" command that could later target whichever conversation is
    # current. Queued continuations use their durable scheduler instead.
    reserved_lease_id = None
    if launch_generation and queue_after_draft_id is None:
        mode = _mode_for_cc_model(data.get("cc_model")) or conv.get("mode", "weave")
        serial_lane = mode in _AGENT_WORKSPACE_MODES and not parallel_enabled
        if serial_lane:
            reserved_lease_id = uuid.uuid4().hex
            acquired = await db.try_acquire_generation_lease(
                conv_id,
                reserved_lease_id,
                user_msg_id=None,
                draft_parent_id=None,
                client_instance_id=client_instance_id,
            )
            if not acquired:
                reserved_lease_id = None
                queue_after_draft_id = await _single_lane_queue_barrier_draft_id(
                    conv_id
                )
                if queue_after_draft_id is None:
                    raise HTTPException(
                        409,
                        "The active agent has not exposed one durable draft yet; retry this send in a moment",
                    )
                # The draft may have appeared AFTER _resolve_queue_target's
                # preflight. Reconcile topology at the reservation boundary
                # too, but never reparent a new root or an explicit branch.
                if send_intent == "continue" and topology_intent != "branch" and parent_id is not None:
                    barrier = await db.get_message(queue_after_draft_id)
                    if barrier and parent_id in {barrier["id"], barrier.get("parent_id")}:
                        parent_id = barrier["id"]

    if queue_after_draft_id is not None:
        existing_queue = await db.list_queued_generations(conv_id)
        if any(
            str(item.get("wait_for_draft_id")) == str(queue_after_draft_id)
            for item in existing_queue
        ):
            raise HTTPException(
                409,
                "This running agent already has a queued message; edit or cancel it before adding another",
            )

    describe_context = data.get("describe_context", "").strip() or None
    try:
        msg = await db.add_message(
            conv_id, role, content, parent_id=parent_id, image_path=image_path,
            describe_context=describe_context,
            origin_client_id=client_instance_id,
            target_anchor_id=target_anchor_id,
            send_intent=send_intent,
            loom_thread_id=loom_thread_id,
            client_request_id=client_request_id,
        )
    except (sqlite3.IntegrityError, ValueError) as exc:
        if reserved_lease_id:
            await db.release_generation_lease(reserved_lease_id, "launch_failed")
        if client_request_id:
            existing_message = await db.get_message_by_client_request(
                conv_id, client_request_id
            )
            if existing_message:
                existing_message["idempotent_replay"] = True
                existing_message["generation_ids"] = []
                return existing_message
        if role == "user" and is_new_thread:
            raise HTTPException(
                409, "This standalone Loom thread already received its root message"
            ) from exc
        raise
    if queue_after_draft_id is not None:
        generation_settings = {
            key: data[key]
            for key in ("cc_model", "cc_effort", "cc_permission_mode")
            if data.get(key) is not None
        }
        try:
            await db.enqueue_generation(
                msg["id"],
                conv_id,
                queue_after_draft_id,
                client_instance_id=client_instance_id,
                loom_thread_id=loom_thread_id,
                settings=generation_settings,
                generation_count=data.get("generation_count", 1),
            )
        except sqlite3.IntegrityError:
            # A competing window won the one-continuation slot after our
            # preflight check. Remove only the row created by this request.
            await db.delete_branch(msg["id"])
            raise HTTPException(
                409,
                "This running branch already has a queued continuation; edit or cancel it before adding another",
            )
        msg["queued"] = True
        msg["queue_after_draft_id"] = queue_after_draft_id
    else:
        msg["queued"] = False
    if cursor_contract is not None:
        try:
            advanced_cursor = await db.advance_loom_cursor(
                cursor_contract["cursor_id"],
                conversation_id=conv_id,
                client_instance_id=client_instance_id,
                thread_id=loom_thread_id,
                expected_leaf_id=data.get("expected_leaf_id"),
                expected_revision=int(data.get("cursor_revision")),
                new_leaf_id=msg["id"],
            )
        except db.LoomCursorConflict as exc:
            await db.delete_branch(msg["id"])
            if reserved_lease_id:
                await db.release_generation_lease(
                    reserved_lease_id, "launch_failed"
                )
            raise HTTPException(409, str(exc))
        msg["loom_cursor"] = advanced_cursor
    await db.set_active_branch(conv_id, msg["id"])
    if queue_after_draft_id is not None:
        await _ws_send(
            conv_id,
            {
                "type": "queue_changed",
                "queued": True,
                "user_msg_id": msg["id"],
                "wait_for_draft_id": queue_after_draft_id,
            },
        )
        # Covers the race where the assistant finished between queue-target
        # resolution and the durable insert.
        _spawn_background_task(
            _dispatch_ready_queued_generations(conv_id),
            f"queue dispatch {conv_id}",
        )
    elif launch_generation:
        generation_data = {
            key: data[key]
            for key in ("cc_model", "cc_effort", "cc_permission_mode")
            if data.get(key) is not None
        }
        generation_data.update(
            {
                "action": "generate",
                "parent_id": msg["id"],
                "client_instance_id": client_instance_id,
                "loom_thread_id": msg["loom_thread_id"],
            }
        )
        count = max(1, min(5, int(data.get("generation_count") or 1)))
        if conv.get("mode") != "weave":
            count = 1
        websocket = _find_client_websocket(conv_id, client_instance_id)
        launched = []
        for _ in range(count):
            try:
                gen_key, _task = await _launch_generation_task(
                    websocket,
                    conv_id,
                    generation_data,
                    client_instance_id,
                    preacquired_lease_id=reserved_lease_id,
                )
            except Exception:
                if reserved_lease_id:
                    await db.release_generation_lease(
                        reserved_lease_id, "launch_failed"
                    )
                raise
            reserved_lease_id = None
            launched.append(gen_key[2])
        msg["generation_ids"] = launched
    return msg


@app.get("/api/conversations/{conv_id}/generation-queue")
async def api_generation_queue(conv_id: int, loom_thread_id: str | None = None):
    if not await db.get_conversation(conv_id):
        raise HTTPException(404, "Conversation not found")
    items = await db.list_queued_generations(conv_id)
    if loom_thread_id:
        items = [
            item for item in items
            if str(item.get("loom_thread_id") or "") == str(loom_thread_id)
        ]
    return items


@app.post(
    "/api/conversations/{conv_id}/generation-queue/{user_msg_id}/resume"
)
async def api_resume_queued_generation(conv_id: int, user_msg_id: int):
    message = await db.get_message(user_msg_id)
    if not message or int(message.get("conversation_id")) != int(conv_id):
        raise HTTPException(404, "Queued message not found")
    conv = await db.get_conversation(conv_id)
    if reason := model_registry.retirement_reason((conv or {}).get("mode"), (conv or {}).get("cc_model")):
        raise HTTPException(410, reason)
    if not await db.resume_queued_generation(user_msg_id):
        raise HTTPException(409, "Queued message is not paused")
    _spawn_background_task(
        _dispatch_ready_queued_generations(conv_id), f"queue dispatch {conv_id}"
    )
    await _ws_send(
        conv_id,
        {
            "type": "queue_changed",
            "queued": True,
            "status": "queued",
            "user_msg_id": user_msg_id,
        },
    )
    return {"ok": True, "user_msg_id": user_msg_id, "status": "queued"}


@app.get("/api/conversations/{conv_id}/messages/{msg_id}/siblings")
async def api_get_siblings(conv_id: int, msg_id: int):
    siblings = await db.get_siblings(msg_id)
    return siblings


@app.get("/api/conversations/{conv_id}/messages/{msg_id}/children")
async def api_get_children(conv_id: int, msg_id: int):
    children = await db.get_children(msg_id)
    return children


# ── Regenerate (branch) ──


@app.post("/api/conversations/{conv_id}/regenerate/{msg_id}")
async def api_regenerate(conv_id: int, msg_id: int):
    """Create a branch point: new sibling of msg_id from the same parent."""
    msg = await db.get_message(msg_id)
    if not msg:
        raise HTTPException(404, "Message not found")
    # Return parent info so client knows where to generate from
    return {"parent_id": msg["parent_id"], "original_id": msg_id}


@app.post("/api/conversations/{conv_id}/redescribe/{msg_id}")
async def api_redescribe(conv_id: int, msg_id: int):
    """Re-describe all images attached to a user message.

    Emits describe_start/describe_done WebSocket events so the UI can show
    a spinner and updated description.
    """
    msg = await db.get_message(msg_id)
    if not msg or not msg.get("image_path"):
        raise HTTPException(404, "Message not found or has no images")

    img_paths = _parse_image_paths(msg["image_path"])
    desc_map: dict[str, str] = {}
    _describe_model = config.vision_model or config.llama_model

    # Get describe context from the message
    _ctx = msg.get("describe_context") or None

    await _ws_send(conv_id, {
        "type": "describe_start",
        "parent_msg_id": msg_id,
        "image_count": len(img_paths),
        "model": _describe_model,
        "started_at": _time.time()
    })

    _desc_start = _time.time()
    for ip in img_paths:
        src = Path(ip).resolve()
        try:
            desc = await asyncio.wait_for(describe_image(str(src), model=config.vision_model or None, context=_ctx), timeout=120)
            desc_map[src.name] = desc
        except asyncio.TimeoutError:
            print(f"[DESCRIBE] Timed out describing {src.name}")
        except Exception as e:
            print(f"[DESCRIBE] Failed to describe image {src.name}: {e}")

    _elapsed = (_time.time() - _desc_start) * 1000

    if desc_map:
        try:
            await db.update_message_image_alt(msg_id, json.dumps(desc_map))

            _describe_content = f"[Image description — {_describe_model}]\n\n" + "\n\n".join(desc_map.values())
            _db = await db.get_db()
            existing_rows = await _db.execute_fetchall(
                "SELECT id FROM messages WHERE conversation_id=? AND role='system' AND parent_id=? AND content LIKE ?",
                (conv_id, msg_id, "[Image description —%")
            )
            if existing_rows:
                await _db.execute("UPDATE messages SET content=? WHERE id=?", (_describe_content, existing_rows[0]["id"]))
            else:
                await db.add_message(conv_id, "system", _describe_content, parent_id=msg_id)
        except Exception as e:
            print(f"[DESCRIBE] Failed to persist descriptions: {e}")

    await _ws_send(conv_id, {
        "type": "describe_done",
        "parent_msg_id": msg_id,
        "descriptions": desc_map,
        "model": _describe_model,
        "elapsed_ms": _elapsed
    })

    return {"descriptions": desc_map, "elapsed_ms": _elapsed, "model": _describe_model}


@app.post("/api/conversations/{conv_id}/fork/{msg_id}")
async def api_fork_conversation(conv_id: int, msg_id: int):
    """Fork a conversation from a specific message, creating a new conversation."""
    new_conv = await db.fork_conversation(conv_id, msg_id)
    if not new_conv:
        raise HTTPException(404, "Conversation not found")
    return new_conv


# ── Export / Import ──


@app.get("/api/conversations/{conv_id}/export")
async def api_export_conversation(conv_id: int):
    """Export a conversation with all messages as JSON."""
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    tree = await db.get_conversation_tree(conv_id)
    # Get full message content (tree only has preview)
    full_db = await db.get_db()
    rows = await full_db.execute_fetchall(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at",
        (conv_id,),
    )
    await full_db.close()
    messages = [dict(r) for r in rows]
    export = {
        "type": "loom_conversation",
        "version": 1,
        "conversation": dict(conv),
        "messages": messages,
    }
    # Sanitize filename for HTTP header (ASCII only)
    import unicodedata

    safe_title = unicodedata.normalize("NFKD", conv["title"] or "conversation")
    safe_title = (
        safe_title.encode("ascii", "ignore").decode("ascii").strip() or "conversation"
    )
    return JSONResponse(
        export,
        headers={"Content-Disposition": f'attachment; filename="{safe_title}.json"'},
    )


@app.post("/api/conversations/import")
async def api_import_conversation(file: UploadFile = File(...)):
    """Import a conversation from JSON."""
    content = await file.read()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

    if data.get("type") != "loom_conversation":
        raise HTTPException(400, "Not a Loom conversation export")

    conv_data = data["conversation"]
    new_conv = await db.create_conversation(
        conv_data.get("title", "Imported"),
        conv_data.get("character_id"),
    )
    # Update optional fields
    fields = {}
    for key in ("persona_id", "lore_ids", "style_nudge", "custom_scene", "system_only", "system_prompt"):
        if key in conv_data:
            fields[key] = conv_data[key]
    if fields:
        await db.update_conversation_fields(new_conv["id"], **fields)

    # Import messages, mapping old IDs to new
    id_map = {}
    for msg in data.get("messages", []):
        new_parent = id_map.get(msg.get("parent_id")) if msg.get("parent_id") else None
        new_msg = await db.add_message(
            new_conv["id"],
            msg["role"],
            msg.get("content", ""),
            parent_id=new_parent,
            image_path=msg.get("image_path"),
        )
        id_map[msg["id"]] = new_msg["id"]
        if msg.get("summary"):
            await db.update_message_summary(new_msg["id"], msg["summary"])
        if msg.get("is_active"):
            await db.set_active_branch(new_conv["id"], new_msg["id"])

    return await db.get_conversation(new_conv["id"])


@app.get("/api/characters/{char_id}/export")
async def api_export_character(char_id: str):
    """Download a character .md file."""
    filepath = os.path.join(config.characters_dir, f"{char_id}.md")
    if not os.path.exists(filepath):
        raise HTTPException(404, "Character not found")
    return FileResponse(filepath, filename=f"{char_id}.md", media_type="text/markdown")


@app.post("/api/characters/import")
async def api_import_character(file: UploadFile = File(...)):
    """Import a character from a .md file."""
    content = (await file.read()).decode("utf-8")
    filename = Path(file.filename).stem
    filepath = os.path.join(config.characters_dir, f"{filename}.md")
    os.makedirs(config.characters_dir, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    char = load_character(filepath)
    return char or {"id": filename, "name": filename}


@app.get("/api/personas/{persona_id}/export")
async def api_export_persona(persona_id: str):
    filepath = os.path.join("personas", f"{persona_id}.md")
    if not os.path.exists(filepath):
        raise HTTPException(404, "Persona not found")
    return FileResponse(
        filepath, filename=f"{persona_id}.md", media_type="text/markdown"
    )


@app.post("/api/personas/import")
async def api_import_persona(file: UploadFile = File(...)):
    content = (await file.read()).decode("utf-8")
    filename = Path(file.filename).stem
    filepath = os.path.join("personas", f"{filename}.md")
    os.makedirs("personas", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    persona = load_persona(filepath)
    return persona or {"id": filename, "name": filename}


@app.get("/api/lore/{lore_id}/export")
async def api_export_lore(lore_id: str):
    filepath = os.path.join("lore", f"{lore_id}.md")
    if not os.path.exists(filepath):
        raise HTTPException(404, "Lore not found")
    return FileResponse(filepath, filename=f"{lore_id}.md", media_type="text/markdown")


@app.post("/api/lore/import")
async def api_import_lore(file: UploadFile = File(...)):
    content = (await file.read()).decode("utf-8")
    filename = Path(file.filename).stem
    filepath = os.path.join("lore", f"{filename}.md")
    os.makedirs("lore", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    entry = load_lore_entry(filepath)
    return entry or {"id": filename, "name": filename}


# ── File Upload ──

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_TEXT_EXTS = {
    ".md",
    ".txt",
    ".pdf",
    ".json",
    ".csv",
    ".py",
    ".js",
    ".ts",
    ".html",
    ".css",
    ".xml",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".sh",
    ".bat",
    ".ps1",
    ".log",
    ".rst",
    ".tex",
    ".r",
    ".sql",
    ".docx",
    ".doc",
    ".xlsx",
    ".xls",
    ".pptx",
    ".ppt",
}
_ALLOWED_EXTS = _IMAGE_EXTS | _TEXT_EXTS


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    ext = Path(file.filename).suffix.lower()
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(400, f"Unsupported file format: {ext}")

    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(config.upload_dir, filename)

    with open(filepath, "wb") as f:
        content = await file.read()
        f.write(content)

    is_image = ext in _IMAGE_EXTS

    # For local/braid mode: also copy to project_dir so CC can read it
    # We'll determine project_dir later from the message context
    # For now, store the relative path so we can resolve it when needed
    return {
        "path": filepath,
        "url": f"/uploads/{filename}",
        "is_image": is_image,
        "original_name": file.filename,
    }


# ── Config ──


def _resolve_llama_server_exe_for_config(raw: str | None, models_dir: str | None) -> str | None:
    raw = (raw or "").strip().strip('"')
    if not raw:
        return None
    p = Path(raw).expanduser()
    if p.is_absolute():
        return str(p) if p.is_file() else None
    found = shutil.which(raw)
    if found:
        return found

    candidates = [Path(__file__).parent / p]
    if raw.lower() in {"llama-server", "llama-server.exe"}:
        model_root = Path(models_dir or config.llama_models_dir).expanduser()
        candidates.extend([
            model_root.parent / "bin" / "llama-server.exe",
            model_root.parent / "llama-server.exe",
            Path.home() / "OneDrive" / "Documents" / "LS" / "bin" / "llama-server.exe",
            Path.home() / "Documents" / "LS" / "bin" / "llama-server.exe",
            Path(r"C:\LlamaServer\bin\llama-server.exe"),
            Path(r"C:\LlamaServer\llama-server.exe"),
        ])
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _normalized_config_dict() -> dict:
    data = config.to_dict()
    resolved = _resolve_llama_server_exe_for_config(
        data.get("llama_server_exe"),
        data.get("llama_models_dir"),
    )
    if resolved and resolved != data.get("llama_server_exe"):
        config.llama_server_exe = resolved
        config.save()
        data = config.to_dict()
    return data


@app.get("/api/config")
async def api_get_config():
    config.load()
    return _normalized_config_dict()


@app.get("/api/visual-settings")
async def api_visual_settings():
    from visual_settings import load
    return JSONResponse(load(), headers={"Cache-Control":"no-store"})


@app.put("/api/config")
async def api_update_config(data: dict):
    config.load()
    if "llama_server_exe" in data:
        incoming = str(data.get("llama_server_exe") or "").strip()
        current = _resolve_llama_server_exe_for_config(
            config.llama_server_exe,
            config.llama_models_dir,
        )
        incoming_is_unresolved_bare = (
            incoming.lower() in {"llama-server", "llama-server.exe"}
            and not shutil.which(incoming)
        )
        if incoming_is_unresolved_bare and current:
            data = dict(data)
            data["llama_server_exe"] = current
        else:
            resolved = _resolve_llama_server_exe_for_config(
                incoming,
                data.get("llama_models_dir") or config.llama_models_dir,
            )
            if resolved:
                data = dict(data)
                data["llama_server_exe"] = resolved
    config.update_from_dict(data)
    return _normalized_config_dict()


# ── OpenRouter ──


@app.get("/api/openrouter/secrets")
async def api_get_openrouter_secrets():
    return {"ok": True, "status": openrouter_client.secret_status()}


@app.post("/api/openrouter/secrets")
async def api_post_openrouter_secrets(data: dict):
    updates = {}
    clear_names = set()
    for key, env_name in (
        ("api_key", "OPENROUTER_API_KEY"),
        ("management_key", "OPENROUTER_MANAGEMENT_KEY"),
    ):
        if key not in data:
            continue
        value = data.get(key)
        if value is None or value == "":
            clear_names.add(env_name)
        else:
            updates[env_name] = value
    result = openrouter_client.write_dotenv_secrets(
        updates=updates, clear_names=clear_names
    )
    return {
        "ok": True,
        "updated": result["updated"],
        "cleared": result["cleared"],
        "status": result["status"],
    }


@app.get("/api/openrouter/usage")
async def api_get_openrouter_usage():
    return await openrouter_client.usage_snapshot()


@app.get("/api/provider-usage")
async def api_get_provider_usage():
    from provider_usage import monitor
    return JSONResponse(await monitor.snapshot(), headers={"Cache-Control": "no-store"})


from usage_activity import router as usage_activity_router
app.include_router(usage_activity_router)


@app.post("/api/openrouter/provision")
async def api_post_openrouter_provision(data: dict = None):
    data = data or {}
    key = await openrouter_client.create_limited_key(
        name=data.get("name", "EX umbra Omnia"),
        limit=data.get("limit", 12.5),
        limit_reset=data.get("limit_reset", "weekly"),
        include_byok_in_limit=data.get("include_byok_in_limit", True),
    )
    return {"ok": True, "key": key}


# ── OpenRouter model registry editor (admin-managed) ──


def _openrouter_model_projection() -> dict:
    """List merged OpenRouter models with an overrides-file marker per entry."""
    try:
        overrides_ids = {str(m.get("id", "")).casefold() for m in model_registry.load_overrides()}
    except ValueError:
        overrides_ids = set()
    return {
        "models": [
            {
                "id": m["id"], "label": m["label"],
                "context_length": m["context_length"],
                "handoff_threshold": m.get("handoff_threshold"),
                "aliases": m.get("aliases", []),
                "capabilities": m.get("capabilities", {}),
                "routes": m["routes"],
                "overridden": m["id"].casefold() in overrides_ids,
            }
            for m in model_registry.openrouter_catalog_models()
        ],
        "overrides_active": bool(overrides_ids),
    }


def _refresh_registry_derived_cc_groups() -> None:
    """Rebuild registry-derived CC picker groups after a registry hot reload.

    CC_MODELS holds startup snapshots. Refresh the OpenRouter and Goose ACP
    groups from the live registry, preserving runtime-derived entries (Goose
    Dream selectors) which carry no registry model_id.
    """
    route_by_group = {
        "OpenRouter": "openrouter",
        "Goose ACP": "goose",
        "Goose ACP - Smart/Subagents": "goose_smart",
    }
    for group in CC_MODELS:
        route = route_by_group.get(group.get("group", ""))
        if not route:
            continue
        fresh = {e["value"].casefold(): e for e in model_registry.REGISTRY.entries(route)}
        preserved = [m for m in group.get("models", []) if not m.get("model_id")]
        group["models"] = list(fresh.values()) + preserved


@app.get("/api/models/openrouter")
async def api_get_openrouter_models():
    """Current merged OpenRouter model list for the admin slugs editor."""
    return _openrouter_model_projection()


@app.post("/api/models/openrouter")
async def api_post_openrouter_models(data: dict = None):
    """Replace the OpenRouter override set, hot-reload the registry, refresh pickers."""
    data = data or {}
    try:
        model_registry.save_openrouter_overrides(data.get("models") or [])
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    _refresh_registry_derived_cc_groups()
    global _ALL_ENGINES_CACHE
    _ALL_ENGINES_CACHE = None
    return {"ok": True, **_openrouter_model_projection()}


def load_local_codex_models() -> list[dict]:
    """Load Codex models from the local ~/.codex/models_cache.json file if it exists."""
    import os
    from pathlib import Path
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    cache_path = home / ".codex" / "models_cache.json"
    if not cache_path.exists():
        return []
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        models = []
        for m in data.get("models", []):
            slug = m.get("slug")
            display_name = m.get("display_name")
            if (
                slug
                and display_name
                and m.get("visibility", "list") != "hide"
                and m.get("supported_in_api", True) is not False
            ):
                models.append({
                    "value": f"codex-{slug}",
                    "label": f"Codex ({display_name})"
                })
        return models
    except Exception as e:
        print(f"[CODEX] Failed to load local models cache: {e}")
        return []


def get_initial_codex_models() -> list[dict]:
    return _codex_picker_models(load_local_codex_models())


def _codex_picker_models(discovered: list[dict]) -> list[dict]:
    """Discovered selectors plus explicit catalog pins, without access claims."""
    rows = []
    seen = set()
    for row in [*discovered, *model_registry.REGISTRY.fallback("codex")]:
        identity = row["value"].casefold()
        if identity not in seen:
            rows.append(dict(row))
            seen.add(identity)
    return rows


def load_local_gemini_models() -> list[dict]:
    """Read cached models without blocking a request/startup on the agy CLI."""
    import os
    from pathlib import Path
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    from gemini_client import agy_models_cache_path, _agy_model_rows
    cache_path = agy_models_cache_path()
    if not cache_path.exists():
        cache_path = home / ".gemini" / "models_cache.json"  # legacy read-only fallback
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8-sig"))
            models = _agy_model_rows(data.get("models", []))
            if models:
                return models
        except Exception as e:
            print(f"[GEMINI] Failed to load local models cache: {e}")

    return []


def get_initial_gemini_models() -> list[dict]:
    return load_local_gemini_models() or model_registry.REGISTRY.fallback("gemini")


# ── CC Model List ──
# Picker defaults are generated from the shared catalog. Discovery may replace
# native groups; runtime local/Dream entries are derived separately.
def _initial_cc_models() -> list[dict]:
    registry = model_registry.REGISTRY
    pareto_models = [
        {"value": "gemini:gemini-3.8-flash-high", "label": "Gemini Flash 3.8 (High)", "effort": "high"},
        {"value": "codex-gpt-6.1-sol", "label": "GPT 6.1 Sol (Medium)", "effort": "medium"},
        {"value": "goose:openrouter:z-ai/glm-5.3-flash", "label": "GLM 5.3 Flash (Extra High in Goose)", "effort": "xhigh"},
        {"value": "claude-sonnet-5-5[1m]", "label": "Claude Sonnet 5.5 (Medium in CC, 1M)", "effort": "medium"},
    ]
    return [
        {"group": "Pareto", "models": pareto_models},
        {"group": "Anthropic", "models": registry.fallback("anthropic")},
        {"group": "Antigravity (agy)", "models": get_initial_gemini_models()},
        {"group": "ChatGPT Codex (codex)", "models": get_initial_codex_models()},
        {"group": "Goose ACP", "models": registry.entries("goose") + [{
            "value": f"goose:dream:{config.dream_model}", "label": f"Goose Dream ({config.dream_model})"}]},
        {"group": "Goose ACP - Smart/Subagents", "models": registry.entries("goose_smart") + [{
            "value": f"goose:auto:dream:{config.dream_model}", "label": f"Goose Smart Dream ({config.dream_model})"}]},
        {"group": "OpenRouter", "models": registry.entries("openrouter")},
        {"group": "Umans AI", "models": registry.fallback("umans")},
        {"group": "Dream via Claude Code", "models": [{
            "value": f"dream:{config.dream_model}", "label": f"Dream Claude ({config.dream_model})"}]},
    ]


CC_MODELS = _initial_cc_models()


def _runtime_goose_llama_model(auto: bool = False) -> dict | None:
    """Goose selector for the currently configured local llama-server model."""
    model = (config.llama_model or "").strip()
    if not model:
        return None
    prefix = "goose:auto:llama:" if auto else "goose:llama:"
    label_prefix = "Goose Smart Llama Server" if auto else "Goose Llama Server"
    return {
        "value": f"{prefix}{model}",
        "label": f"{label_prefix} ({Path(model).name})",
    }


def _cc_models_with_runtime_goose_llama() -> list[dict]:
    """Return CC model groups plus runtime-derived Goose local entries.

    CC_MODELS is mutated by remote-model refreshes and is intentionally kept as
    the base list. The local llama model can change through Settings without a
    process restart, so derive the Goose-local selector when serving the picker.
    """
    groups: list[dict] = []
    for group in CC_MODELS:
        copied = {
            "group": group.get("group", ""),
            "models": list(group.get("models", [])),
        }
        if copied["group"].startswith("ChatGPT Codex"):
            # App-server can update its cache during an ordinary agent turn.
            # Re-read that local evidence instead of retaining the startup list
            # until an explicit refresh or a host restart. No provider call.
            copied["models"] = _codex_picker_models(load_local_codex_models() or copied["models"])
        if copied["group"] == "Goose ACP":
            entry = _runtime_goose_llama_model(auto=False)
            if entry and not any(m.get("value") == entry["value"] for m in copied["models"]):
                copied["models"].append(entry)
        elif copied["group"] == "Goose ACP - Smart/Subagents":
            entry = _runtime_goose_llama_model(auto=True)
            if entry and not any(m.get("value") == entry["value"] for m in copied["models"]):
                copied["models"].append(entry)
        # Explicit native pins supplement discovery, including after refresh.
        family = ("codex" if copied["group"].startswith("ChatGPT Codex") else
                  "gemini" if copied["group"].startswith("Antigravity") else
                  "anthropic" if copied["group"].startswith("Anthropic") and "Auto" not in copied["group"] else
                  "umans" if copied["group"] == "Umans AI" else None)
        if family:
            for entry in model_registry.REGISTRY.entries(family):
                if not any(m.get("value") == entry["value"] for m in copied["models"]):
                    copied["models"].append(entry)
        copied["models"] = [{**model_registry.resolve(m.get("value")), **m} for m in copied["models"]]
        groups.append(copied)
    return groups


# Cached Dream-sidecar availability for picker filtering. None means "never
# probed" and keeps the legacy show-Dream behavior until the first probe runs.
_DREAM_PICKER_PROBE_TTL_SEC = 60
_dream_picker_cache: dict = {"ok": None, "checked_at": 0.0}


async def _dream_available_cached() -> bool | None:
    """Short-TTL Dream health result so picker filtering costs minimal latency."""
    now = time.time()
    if _dream_picker_cache["ok"] is not None and now - _dream_picker_cache["checked_at"] <= _DREAM_PICKER_PROBE_TTL_SEC:
        return _dream_picker_cache["ok"]
    try:
        import dream_client as _dc
        status = await asyncio.wait_for(_dc.health(config.dream_host, timeout=1.0), timeout=1.2)
    except Exception:
        status = None
    _dream_picker_cache["ok"] = bool(status)
    _dream_picker_cache["checked_at"] = now
    return _dream_picker_cache["ok"]


def _drop_dream_picker_entries(groups: list[dict]) -> list[dict]:
    """Remove Dream-backed picker entries and the Dream group when Dream is down."""
    dream_prefixes = ("goose:dream:", "goose:auto:dream:", "dream:")
    filtered = []
    for group in groups:
        if group.get("group", "") == "Dream via Claude Code":
            continue
        models = [m for m in group.get("models", [])
                  if not str(m.get("value", "")).lower().startswith(dream_prefixes)]
        filtered.append({**group, "models": models})
    return filtered


def _visible_cc_models(dream_available: bool | None = None) -> list[dict]:
    """Return CC model groups, hiding disabled compatibility provider groups.

    When ``dream_available`` is False (or the last cached probe says Dream is
    down) Dream-backed selectors are hidden from the picker.
    """
    groups = _cc_models_with_runtime_goose_llama()
    if not config.enable_goose:
        groups = [
            {**g, "models": [m for m in g.get("models", []) if not str(m.get("value", "")).lower().startswith("goose:")]}
            if g.get("group") == "Pareto" else g
            for g in groups
        ]
    if config.enable_umans_models and config.enable_goose:
        if dream_available is None:
            dream_available = _dream_picker_cache["ok"]
        if dream_available is False:
            groups = _drop_dream_picker_entries(groups)
        return groups
    groups = [
        group
        for group in groups
        if (config.enable_umans_models or "umans" not in group.get("group", "").lower())
        and (config.enable_goose or "goose" not in group.get("group", "").lower())
    ]
    if dream_available is None:
        dream_available = _dream_picker_cache["ok"]
    if dream_available is False:
        groups = _drop_dream_picker_entries(groups)
    return groups


@app.get("/api/cc-models")
async def api_cc_models():
    return _visible_cc_models(dream_available=await _dream_available_cached())


@app.get("/api/model-registry.js")
async def api_model_registry_script():
    """Offline bootstrap before SPA rendering; no model probes or credentials."""
    metadata = model_registry.REGISTRY.public(_visible_cc_models())
    data = json.dumps(metadata, ensure_ascii=True).replace("<", "\\u003c")
    return Response(
        "EXOModelRegistry.install(" + data + ");\n",
        media_type="application/javascript", headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/goose/diagnostics")
async def api_goose_diagnostics():
    return {
        "enabled": bool(config.enable_goose),
        "default_model": config.goose_model,
        "mode": config.goose_mode,
        "builtins": config.goose_builtins,
        **goose_client.diagnostics(config.goose_exe or None),
    }


# 1M-context support is per family+version, not per family. Subscription users
# can only enable 1M via the CLI's `[1m]` model suffix (`--betas` is
# API-key-only), and the CLI's suffix parser is independent of alias-vs-full-ID
# resolution, so `sonnet[1m]` and `claude-sonnet-4-7[1m]` both work. Sonnet
# supports 1M across our surfaced range; Opus only from 4.7 onward (Opus 4.6 has
# no 1M tier and a pinned `claude-opus-4-6[1m]` just rate-limits/fails); Haiku
# never. Aliases (`opus`, `sonnet`) resolve to the latest version, which always
# supports 1M, so the Auto group emits `[1m]` for both.
_CC_ONEM_MIN_VERSION = {"fable": (5, 0), "opus": (4, 7), "sonnet": (4, 5)}


def _supports_1m(family: str, version: tuple[int, int]) -> bool:
    floor = _CC_ONEM_MIN_VERSION.get(family)
    return floor is not None and version >= floor


# Family display order, applied to both the Auto group and within Pinned.
_CC_FAMILY_ORDER = ["fable", "opus", "sonnet", "haiku"]
_CC_ALIAS_FAMILIES = {"opus", "sonnet", "haiku"}

import re as _re
# claude-<family>-<major>[-<minor>][-<yyyymmdd>] — minor is 1-2 digits and
# optional (e.g. `claude-opus-4-20250514` is version 4.0 with a date, NOT
# minor=20250514). The optional 8-digit date tail is anchored so it can't be
# mistaken for the minor.
_CC_MODEL_ID_RE = _re.compile(
    r"^claude-(fable|opus|sonnet|haiku)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$",
    _re.IGNORECASE,
)


def _read_oauth_token() -> str | None:
    """Read the Claude Code subscription OAuth access token from the standard
    credentials file. Returns None if missing/unreadable. The expiresAt timestamp
    is intentionally not checked — the API returns 401 if it's actually expired,
    and reading the file each call means the user can refresh their session via
    the Claude Code CLI without restarting Loom."""
    cred_path = Path.home() / ".claude" / ".credentials.json"
    try:
        data = json.loads(cred_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return (data.get("claudeAiOauth") or {}).get("accessToken")


def _build_anthropic_model_dropdowns(api_models: list[dict]) -> tuple[list[dict], list[dict], dict[str, list]]:
    # Group every returned claude-* model by family. display_name comes
    # pre-formatted ("Claude Opus 4.8"); strip the "Claude " prefix so labels
    # read "Opus 4.8" to match the compact dropdown style.
    by_family: dict[str, list[tuple[tuple[int, int], str, str]]] = {
        f: [] for f in _CC_FAMILY_ORDER
    }
    for m in api_models:
        model_id = m.get("id", "")
        match = _CC_MODEL_ID_RE.match(model_id)
        if not match:
            continue
        family = match.group(1).lower()
        if family not in by_family:
            continue
        version = (int(match.group(2)), int(match.group(3) or 0))
        if version < (4, 5):
            continue
        label = (m.get("display_name") or "").removeprefix("Claude ").strip()
        if not label:
            label = f"{family.capitalize()} {version[0]}.{version[1]}"
        by_family[family].append((version, model_id, label))

    if not any(by_family.values()):
        raise ValueError("Anthropic /v1/models returned no recognizable claude-* models.")

    for family in by_family:
        by_family[family].sort(key=lambda t: t[0], reverse=True)

    # Auto group — one alias entry per family with a known Claude Code alias.
    # The CLI's bundled alias map (frozen at the CLI's build time) decides which
    # concrete ID runs, so the label can't honestly name a version.
    auto_models: list[dict] = []
    for family in _CC_FAMILY_ORDER:
        if not by_family[family]:
            continue
        if family not in _CC_ALIAS_FAMILIES:
            continue
        cap = family.capitalize()
        auto_models.append({"value": family, "label": f"{cap} (latest)"})
        # Alias resolves to the family's latest version, which always supports 1M
        # if any version in the family does.
        if family in _CC_ONEM_MIN_VERSION:
            auto_models.append({"value": f"{family}[1m]", "label": f"{cap} (latest, 1M)"})

    # Pinned group — every concrete claude-* ID, with [1m] siblings interleaved
    # per version so each release block stays together. Fable's documented API
    # ID is already 1M-context, so we do not add a synthetic suffix variant.
    pinned_models: list[dict] = []
    for family in _CC_FAMILY_ORDER:
        for _ver, model_id, label in by_family[family]:
            pinned_models.append({"value": model_id, "label": label})
            if family != "fable" and _supports_1m(family, _ver):
                pinned_models.append({"value": f"{model_id}[1m]", "label": f"{label} (1M)"})

    return auto_models, pinned_models, by_family


def _update_gemini_in_cc_models(models: list[dict]):
    for group in CC_MODELS:
        if group["group"].startswith("Antigravity"):
            group["models"] = models
            return
    CC_MODELS.append({"group": "Antigravity", "models": models})


def _update_codex_in_cc_models(models: list[dict]):
    for group in CC_MODELS:
        if group["group"].startswith("ChatGPT Codex"):
            group["models"] = models
            return
    CC_MODELS.append({"group": "ChatGPT Codex", "models": models})


def _update_anthropic_in_cc_models(auto_models: list[dict], pinned_models: list[dict]):
    new_cc_models: list[dict] = []
    inserted = False
    for group in CC_MODELS:
        if group["group"].startswith("Anthropic"):
            if not inserted:
                new_cc_models.append({"group": "Anthropic — Auto", "models": auto_models})
                new_cc_models.append({"group": "Anthropic — Pinned", "models": pinned_models})
                inserted = True
        else:
            new_cc_models.append(group)
    if not inserted:
        new_cc_models.insert(0, {"group": "Anthropic — Pinned", "models": pinned_models})
        new_cc_models.insert(0, {"group": "Anthropic — Auto", "models": auto_models})
    CC_MODELS[:] = new_cc_models


@app.post("/api/cc-models/refresh")
async def api_cc_models_refresh(provider: str = "all"):
    """Refresh model options for supported agent harnesses (Anthropic, Antigravity, Codex).

    provider can be "all", "anthropic", "gemini", or "codex". When "all", discovery runs
    concurrently across providers without one provider's failure blocking the others.
    """
    req_provider = (provider or "all").strip().lower()
    refreshed: list[str] = []
    skipped: dict[str, str] = {}
    by_family: dict = {}

    if req_provider == "gemini":
        import gemini_client
        models = await asyncio.to_thread(gemini_client.discover_agy_models)
        if not models:
            raise HTTPException(502, "Antigravity model discovery failed; existing models were retained.")
        _update_gemini_in_cc_models(models)
        return {
            "ok": True,
            "refreshed": ["gemini"],
            "skipped": {},
            "models": _visible_cc_models(),
            "families": [],
        }

    if req_provider == "codex":
        import codex_client
        models = await codex_client.discover_codex_models(force=True)
        if not models:
            raise HTTPException(502, "Codex model discovery failed; existing models were retained.")
        _update_codex_in_cc_models(models)
        return {
            "ok": True,
            "refreshed": ["codex"],
            "skipped": {},
            "models": _visible_cc_models(),
            "families": [],
        }

    async def _refresh_anthropic():
        nonlocal by_family
        token = _read_oauth_token()
        if not token:
            if req_provider == "anthropic":
                raise HTTPException(
                    401,
                    "No Claude Code login found (~/.claude/.credentials.json). "
                    "Run `claude` once to sign in, then retry.",
                )
            skipped["anthropic"] = "No Claude Code login found (~/.claude/.credentials.json)"
            return

        headers = {
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://api.anthropic.com/v1/models",
                    params={"limit": 100},
                    headers=headers,
                )
        except httpx.HTTPError as e:
            if req_provider == "anthropic":
                raise HTTPException(502, f"Failed to reach Anthropic /v1/models: {e}")
            skipped["anthropic"] = f"Failed to reach Anthropic /v1/models: {e}"
            return

        if resp.status_code == 401:
            if req_provider == "anthropic":
                raise HTTPException(
                    401,
                    "Anthropic rejected the login token (expired?). "
                    "Run `claude` and complete sign-in again, then retry.",
                )
            skipped["anthropic"] = "Anthropic rejected the login token (expired)"
            return
        if resp.status_code != 200:
            if req_provider == "anthropic":
                raise HTTPException(502, f"Anthropic /v1/models returned {resp.status_code}: {resp.text[:200]}")
            skipped["anthropic"] = f"Anthropic returned {resp.status_code}"
            return

        try:
            auto_models, pinned_models, fams = _build_anthropic_model_dropdowns(resp.json().get("data", []))
            by_family.update(fams)
            _update_anthropic_in_cc_models(auto_models, pinned_models)
            refreshed.append("anthropic")
        except Exception as e:
            if req_provider == "anthropic":
                raise HTTPException(502, str(e))
            skipped["anthropic"] = str(e)

    async def _refresh_gemini():
        import gemini_client
        try:
            models = await asyncio.to_thread(gemini_client.discover_agy_models)
            if not models:
                models = await asyncio.to_thread(load_local_gemini_models)
            if models:
                _update_gemini_in_cc_models(models)
                refreshed.append("gemini")
            else:
                skipped["gemini"] = "Antigravity CLI discovery returned no models"
        except Exception as e:
            skipped["gemini"] = str(e)

    async def _refresh_codex():
        import codex_client
        try:
            models = await codex_client.discover_codex_models(force=True)
            if not models:
                models = load_local_codex_models()
            if models:
                _update_codex_in_cc_models(models)
                refreshed.append("codex")
            else:
                skipped["codex"] = "Codex discovery returned no models"
        except Exception as e:
            skipped["codex"] = str(e)

    tasks = []
    if req_provider in ("all", "anthropic"):
        tasks.append(_refresh_anthropic())
    if req_provider in ("all", "gemini"):
        tasks.append(_refresh_gemini())
    if req_provider in ("all", "codex"):
        tasks.append(_refresh_codex())

    await asyncio.gather(*tasks, return_exceptions=False if req_provider != "all" else True)

    families = sorted(f for f, v in by_family.items() if v) if by_family else []
    return {
        "ok": True,
        "refreshed": refreshed,
        "skipped": skipped,
        "models": _visible_cc_models(),
        "families": families,
    }


_VISION_MODEL_CACHE: dict[tuple[str, str], bool] = {}


@app.get("/api/llama/models")
async def api_llama_models():
    """Return .gguf models from disk plus any models reported by live llama-server."""
    import llama_client as _lc
    disk_models = _lc.list_local_models()
    status = await _lc.health_check()
    live = status.get("models", [])
    all_m = list(disk_models)
    for m in live:
        if m not in all_m:
            all_m.append(m)
    return {"models": all_m, "live": live, "disk": disk_models}


# ── Directory Browser (for Claude mode project picker) ──


@app.get("/api/browse-dirs")
async def api_browse_dirs(path: str = ""):
    """List subdirectories of a given path for the folder picker UI."""
    import string

    if not path:
        # Return drive roots on Windows, or / on Unix
        if os.name == "nt":
            drives = []
            for letter in string.ascii_uppercase:
                drive = f"{letter}:\\"
                if os.path.isdir(drive):
                    drives.append({"name": f"{letter}:", "path": drive})
            return {"parent": None, "dirs": drives, "current": ""}
        else:
            path = "/"

    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise HTTPException(400, "Not a directory")

    parent = os.path.dirname(path) if path != os.path.dirname(path) else None

    try:
        entries = []
        for entry in sorted(os.scandir(path), key=lambda e: e.name.lower()):
            if entry.is_dir() and not entry.name.startswith("."):
                entries.append({"name": entry.name, "path": entry.path})
        return {"parent": parent, "dirs": entries, "current": path}
    except PermissionError:
        return {
            "parent": parent,
            "dirs": [],
            "current": path,
            "error": "Permission denied",
        }


# ── Serve Project Files (images, etc.) ──

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}
_MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
}


_PREVIEW_TEXT_EXTENSIONS = {".md", ".markdown", ".txt"}


def _project_file_servable(relative: Path) -> bool:
    """Images and plan/task previews only; never dotfiles such as .env or .git.

    Hidden folders are refused except Claude Code plan files (.claude/plans/*.md),
    which the artifacts panel links to.
    """
    parts = relative.parts
    if not parts:
        return False
    suffix = relative.suffix.lower()
    if suffix not in _IMAGE_EXTENSIONS and suffix not in _PREVIEW_TEXT_EXTENSIONS:
        return False
    hidden = [p for p in parts if p.startswith(".")]
    if not hidden:
        return True
    return (len(parts) == 3 and [p.lower() for p in parts[:2]] == [".claude", "plans"]
            and suffix in _PREVIEW_TEXT_EXTENSIONS and not parts[2].startswith("."))


async def _chat_presentation_owner(conv_id: int, run_id: str):
    """Only a live exact run may publish; never infer ownership from focus."""
    run_id = canonical_run_id(run_id)
    if not run_id:
        raise HTTPException(400, 'Exact run identity required')
    if run_id.startswith('gen:'):
        for key, snapshot in list(_generation_snapshots.items()):
            if key[0] == conv_id and f'gen:{key[2]}' == run_id:
                task = _active_generations.get(key)
                if task is None or task.done():
                    break
                draft_id = snapshot.get('draft_msg_id')
                message = await db.get_message(draft_id) if draft_id else None
                if not message or message['conversation_id'] != conv_id or message['role'] != 'assistant':
                    break
                conv = await db.get_conversation(conv_id)
                return run_id, snapshot.get('project_dir') or (conv or {}).get('project_dir') or '.', draft_id, None
        raise HTTPException(409, 'Generation is no longer active or has no draft')
    run = await db.get_agent_run(run_id.removeprefix('agent:'))
    if not run or run['conversation_id'] != conv_id or run['status'] != 'running':
        raise HTTPException(409, 'Child run is not active in this conversation')
    if agent_access.sensitive(run):
        raise HTTPException(403, 'Sensitive children require a human-released handoff')
    return run_id, run['project_dir'], None, run


@app.post('/api/conversations/{conv_id}/chat-items')
async def api_publish_chat_item(conv_id: int, data: dict = Body(...)):
    run_id, workspace, message_id, child = await _chat_presentation_owner(conv_id, data.get('run_id'))
    # Reuse the server-issued run capability, without invoking history search.
    # Numeric generation IDs alone can be reused after a host restart.
    from search_space.scopes import resolve_scope
    from search_space.index import workspace_key
    capability = resolve_scope(data.get('scope_token'))
    if (not capability or capability.conversation_id != conv_id
            or canonical_run_id(capability.run_id) != run_id
            or capability.workspace_key != workspace_key(workspace)
            or capability.source_id != _search_source_id()):
        raise HTTPException(403, 'Chat publication requires this live run capability')
    try:
        if not isinstance(data.get('request_id'), str):
            raise ValueError('A UUID request_id is required')
        request_id = str(uuid.UUID(data.get('request_id', '')))
        existing = await chat_presentations.get_item(request_id)
        if existing:
            if existing['conversation_id'] != conv_id or existing['run_id'] != run_id or existing['message_id'] != message_id:
                raise ValueError('Presentation request belongs to another run')
            return chat_presentations.public_item(existing['item'])
        source_id = data.get('source_id')
        if source_id:
            if not isinstance(source_id, str):
                raise ValueError('source_id must be an attachment ID')
            if data.get('path') is not None or data.get('content') is not None or data.get('mode') != 'attach':
                raise ValueError('source_id must be used alone with attach')
            source = await chat_presentations.get_item(source_id)
            if not source or source['conversation_id'] != conv_id or source['item']['mode'] != 'attach':
                raise ValueError('Attachment source unavailable in this conversation')
            # Sensitive items are never admitted, and children cannot broaden access
            # by copying a selection belonging to a different child.
            if child and source['run_id'] != run_id:
                raise ValueError('Children may only reuse their own selected attachments')
            item = {**source['item'], 'id': request_id}
        else:
            if child and data.get('path') is not None:
                target = chat_presentations.workspace_file(workspace, data['path'])
                relative = str(target.relative_to(Path(workspace).resolve()))
                if not any(agent_access.contains(p, relative) for p in agent_access.scopes(child)[0]):
                    raise ValueError('File is outside the child read scope')
            item = await asyncio.to_thread(chat_presentations.prepare, workspace, {**data, 'request_id': request_id})
        # Recheck ownership after file IO in case cancellation happened while reading.
        await _chat_presentation_owner(conv_id, run_id)
        result = await chat_presentations.save(conv_id, run_id, message_id, item)
    except (ValueError, TypeError, OSError, UnicodeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if message_id is not None:
        message = await db.get_message(message_id)
        await _ws_send(conv_id, {'type': 'chat_item', 'run_id': run_id, 'gen_id': int(run_id.split(':')[1]),
                                'draft_msg_id': message_id, 'loom_thread_id': message.get('loom_thread_id'),
                                'parent_id': message.get('parent_id'), 'item': result})
    return result


@app.get('/api/chat-items/{item_id}/file')
async def api_download_chat_item(item_id: str, download: bool = False):
    stored = await chat_presentations.get_item(item_id)
    if not stored:
        raise HTTPException(404, 'File reference unavailable')
    item = stored['item']
    try:
        target = await asyncio.to_thread(chat_presentations.download_path, item)
    except (ValueError, OSError) as exc:
        raise HTTPException(404, 'File no longer available') from exc
    # Only validated raster snapshots may render inline. Live files always download.
    inline = item['mode'] == 'attach' and item['kind'] == 'image' and not download
    return FileResponse(target, media_type=item.get('media_type') if inline else 'application/octet-stream',
                        filename=Path(item.get('path') or item['label']).name,
                        content_disposition_type='inline' if inline else 'attachment',
                        headers={'Content-Security-Policy': "sandbox; default-src 'none'",
                                 'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store'})


@app.get("/api/conversations/{conv_id}/file")
async def serve_project_file(conv_id: int, path: str = ""):
    """Serve a file from a conversation's project directory (scoped, images only by default)."""
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    project_dir = conv.get("project_dir")
    if not project_dir:
        raise HTTPException(400, "No project directory set")

    base = Path(project_dir).resolve()
    target = (base / path).resolve()

    # Component-aware containment: "C:\proj" must not contain "C:\proj-other".
    if not target.is_relative_to(base):
        raise HTTPException(403, "Path traversal blocked")
    if not _project_file_servable(target.relative_to(base)):
        raise HTTPException(403, "Only images and markdown/text previews are served")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "File not found")

    suffix = target.suffix.lower()
    media_type = _MIME_MAP.get(suffix) or "text/plain"

    # Agent-written files (e.g. an SVG with a script) must never run with
    # Loom's origin when opened directly.
    return FileResponse(
        target,
        media_type=media_type,
        filename=target.name,
        content_disposition_type="inline",
        headers={"Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'",
                 "X-Content-Type-Options": "nosniff"},
    )


@app.get("/api/conversations/{conv_id}/artifacts")
async def serve_project_artifacts(conv_id: int):
    """Scan conversation's project directory for planning and artifact files."""
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    project_dir = conv.get("project_dir")
    if not project_dir:
        raise HTTPException(400, "No project directory set")

    base = Path(project_dir).resolve()
    if not base.exists() or not base.is_dir():
        return {"artifacts": []}

    # Files we want to check for in the workspace
    standard_files = ["CLAUDE.md", "task.md", "tasks.md", "walkthrough.md", "implementation_plan.md"]
    artifacts = []

    # 1. Check standard root files
    for filename in standard_files:
        target = base / filename
        if target.exists() and target.is_file():
            artifacts.append({
                "name": filename,
                "path": filename,
                "size": target.stat().st_size,
                "modified": target.stat().st_mtime
            })

    # 2. Check files inside .claude/plans/
    plans_dir = base / ".claude" / "plans"
    if plans_dir.is_dir():
        try:
            for p in plans_dir.glob("*.md"):
                if p.is_file():
                    rel_path = f".claude/plans/{p.name}"
                    artifacts.append({
                        "name": p.name,
                        "path": rel_path,
                        "size": p.stat().st_size,
                        "modified": p.stat().st_mtime
                    })
        except Exception:
            pass

    # Sort by name
    artifacts.sort(key=lambda x: x["name"].lower())
    return {"artifacts": artifacts}


@app.get("/api/conversations/{conv_id}/workspace-files")
async def api_workspace_files(conv_id: int, path: str = ""):
    from workspace_browser import WorkspaceBrowseError, list_workspace_directory
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    if not conv.get("project_dir"):
        raise HTTPException(400, "No working directory selected")
    try:
        result = await asyncio.to_thread(list_workspace_directory, conv["project_dir"], path)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except (WorkspaceBrowseError, OSError) as exc:
        raise HTTPException(400, "Workspace directory unavailable") from exc


@app.get("/api/conversations/{conv_id}/workspace-git")
async def api_workspace_git(conv_id: int):
    from workspace_browser import WorkspaceBrowseError, git_workspace_summary
    conv = await db.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    if not conv.get("project_dir"):
        raise HTTPException(400, "No working directory selected")
    try:
        result = await asyncio.to_thread(git_workspace_summary, conv["project_dir"])
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except (WorkspaceBrowseError, OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(400, "Git information unavailable") from exc


# ── Claude Code Permission Hook Endpoint ──

# Leave time for the denial to travel back before the caller's own limit.
_PERMISSION_WAIT_MARGIN_SECONDS = 30
_PERMISSION_DEFAULT_WAIT_SECONDS = 24 * 60 * 60


def _permission_wait_limit(source: str) -> int:
    """Seconds Loom keeps a request open, bounded by the caller's own limit."""
    if source == "claude_code":
        return claude_client.PERMISSION_HOOK_TIMEOUT_SECONDS - _PERMISSION_WAIT_MARGIN_SECONDS
    return _PERMISSION_DEFAULT_WAIT_SECONDS


@app.post("/api/cc-permission")
async def handle_cc_permission(data: dict):
    """Receive permission request from CC hook script, forward to UI, wait for response.

    The hook script (cc_permission_hook.py) POSTs here when CC needs tool approval.
    This endpoint long-polls until the user responds in the browser UI (no timeout).
    """
    conv_id = int(data.get("loom_conv_id", 0))
    request_id = str(uuid.uuid4())

    tool_name = data.get("tool_name", "Unknown")
    tool_input = data.get("tool_input", {})
    source = str(data.get("source") or "loom_permission_hook").strip()[:80]

    print(
        f"[PERM] Hook request: conv={conv_id} request_id={request_id}"
    )

    approval_method = data.get("approval_method", "")
    explicit_scope = data.get("permission_scope", "")
    if not explicit_scope and data.get("agent_run_id"):
        explicit_scope = f"agent:{data['agent_run_id']}"
    permission_scope = _permission_scope_for_active_generation(
        conv_id,
        explicit_scope,
    )
    child_run = None
    if permission_scope.startswith("agent:"):
        child_run = await db.get_agent_run(permission_scope.split(":", 1)[1])
        if (
            not child_run or int(child_run["conversation_id"]) != conv_id
            or child_run.get("status") not in {"launching", "running"}
        ):
            return {
                "allow": False, "decision_source": "loom_lifecycle",
                "message": "Loom child permission has no matching conversation/run owner.",
            }
        data = {**data, "agent_run_id": child_run["run_id"],
                "client_instance_id": child_run.get("client_instance_id")}
    conv = await db.get_conversation(conv_id) if conv_id else None
    if not conv:
        return {"allow": False, "decision_source": "loom_lifecycle",
                "message": "Loom permission request has no conversation owner."}
    context = await _permission_context(conv, permission_scope, child_run)
    # Existing command patterns annotate asks only; they cannot veto policy.
    risk_reason = (_destructive_git_command_reason(tool_name, tool_input)
                   or _unsafe_shell_command_reason(tool_name, tool_input))
    risk_reason = risk_reason.replace("is blocked", "requires review")
    normalized_input = tool_input
    if isinstance(tool_input, dict) and data.get("cwd") and not tool_input.get("cwd"):
        normalized_input = {**tool_input, "cwd": data["cwd"]}
    action = permission_policy_model.normalize_permission_request(
        tool_name=tool_name, tool_input=normalized_input, approval_method=approval_method,
        harness=source, workspace_root=context["workspace"], warned=bool(risk_reason))
    if child_run:
        access_error = agent_access.tool_error(child_run, tool_name, normalized_input, action.effect.value)
        if access_error:
            return {"allow": False, "decision_source": "loom_policy", "message": access_error}
    candidate = permission_rules.action_rule(action, normalized_input)
    if candidate and context["branch_id"]:
        candidate = {**candidate, **{k: context[k] for k in
                                   ("workspace", "branch_id", "conversation_id")}}
    else:
        candidate = None
    matched = any(permission_rules.matches(
        rule, conversation_id=conv_id, workspace=context["workspace"],
        ancestor_ids=context["ancestor_ids"], candidate=candidate,
    ) for rule in _permission_rules.values())
    action = dataclass_replace(action, rule_matched=matched)
    profile = permission_policy_model.profile_from_value(conv.get("permission_profile"))
    destructive_review = profile is permission_policy_model.PermissionProfile.TRUST_REVIEW and action.destructive
    if profile is permission_policy_model.PermissionProfile.TRUST_REVIEW:
        # Only explicit destructive evidence prompts in this opt-in profile.
        # Leave legacy warning/approval behavior untouched in other profiles.
        risk_reason = action.destructive_reason
        action = dataclass_replace(action, warned=bool(risk_reason))
        if destructive_review:
            candidate = None  # one-time only, including after an older branch grant
    decision = permission_policy_model.decide(profile, action)
    # File preservation is a precondition, not a model/profile permission or
    # a remembered grant. All harnesses reaching this endpoint share it.
    integrity = await asyncio.to_thread(
        prepare_integrity_check, tool_name, normalized_input,
        workspace=context["workspace"], approval_method=approval_method,
        conversation_id=conv_id, scope=permission_scope,
    )
    if integrity.review_reason:
        candidate = None
        risk_reason = integrity.review_reason
    effective_decision = (permission_policy_model.PolicyDecision.INVALID_REQUEST if integrity.error else
                          permission_policy_model.PolicyDecision.ASK_WITH_WARNING
                          if integrity.review_reason else decision)
    policy = {"mode": "enforced", "profile": profile.value, "decision": decision.value,
              "action": action.audit_dict(), "legacy_outcome": "not_evaluated",
              "effective_decision": effective_decision.value,
              "integrity_review": bool(integrity.review_reason),
              "integrity_blocked": bool(integrity.error)}
    branch_info = _generation_branch_for_scope(conv_id, permission_scope)
    try:
        await db.save_permission_policy_audit(
            request_id, conv_id, policy,
            run_id=canonical_run_id(permission_scope if permission_scope.startswith(("gen:", "agent:")) else None),
            parent_id=branch_info.get("parent_id"), draft_msg_id=branch_info.get("draft_msg_id"),
            permission_scope=permission_scope)
    except Exception as exc:
        print(f"[PERM] Policy audit persistence failed: {type(exc).__name__}")
    if integrity.error:
        return {"allow": False, "decision_source": "loom_policy",
                "message": integrity.error, "feedback": integrity.error}
    if decision is permission_policy_model.PolicyDecision.INVALID_REQUEST:
        return {"allow": False, "decision_source": "loom_policy", "message": "Invalid Loom permission request."}
    if tool_name not in ("ExitPlanMode", "exit_plan_mode") and effective_decision is permission_policy_model.PolicyDecision.AUTO_ALLOW:
        integrity_error = await asyncio.to_thread(revalidate_integrity, integrity)
        if integrity_error:
            return {"allow": False, "decision_source": "loom_policy",
                    "message": integrity_error, "feedback": integrity_error}
        return {"allow": True, "always": False, "decision_source": "loom_policy",
                "message": "Allowed by Loom branch rule." if matched else f"Allowed by Loom {profile.value} policy.",
                "policy": policy}
    permission_key = _permission_fingerprint(tool_name, tool_input, approval_method)

    # Build a human-readable summary
    input_summary = ""
    if isinstance(tool_input, dict):
        if "command" in tool_input:
            input_summary = tool_input["command"]
        elif "file_path" in tool_input:
            input_summary = tool_input["file_path"]
        elif "description" in tool_input:
            input_summary = tool_input["description"]
        else:
            input_summary = json.dumps(tool_input)[:500]
    elif isinstance(tool_input, str):
        input_summary = tool_input[:500]

    # For ExitPlanMode, read the actual plan file content
    if tool_name in ("ExitPlanMode", "exit_plan_mode"):
        plan_content = ""
        # Check project-local .claude/plans/ first, then user-level
        conv = await db.get_conversation(conv_id) if conv_id else None
        plan_dirs = []
        if conv and conv.get("project_dir"):
            plan_dirs.append(Path(conv["project_dir"]) / ".claude" / "plans")
        plan_dirs.append(Path.home() / ".claude" / "plans")
        for plan_dir in plan_dirs:
            if plan_dir.is_dir():
                plan_files = sorted(plan_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
                if plan_files:
                    try:
                        plan_content = plan_files[0].read_text(encoding="utf-8")
                        print(f"[PERM] Read plan file: {plan_files[0]} ({len(plan_content)} chars)")
                    except Exception as e:
                        print(f"[PERM] Failed to read plan file: {e}")
                    break
        if plan_content:
            input_summary = plan_content
            tool_input = {"plan": plan_content, "planFilePath": str(plan_files[0])}

    # Sanitize strings — tool_input can contain surrogate characters that crash WS send
    def _sanitize(obj):
        if isinstance(obj, str):
            return obj.encode("utf-8", errors="replace").decode("utf-8")
        if isinstance(obj, dict):
            return {k: _sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_sanitize(v) for v in obj]
        return obj

    # Build permission message once — broadcast to all active WebSockets globally
    perm_msg = {
        "type": "permission_request",
        "request_id": request_id,
        "conv_id": conv_id,
        "tool_name": tool_name,
        "tool_input": _sanitize(tool_input),
        "input_summary": _sanitize(input_summary),
        "approval_method": approval_method,
        "permission_key": permission_key,
        "permission_scope": permission_scope,
        "client_instance_id": data.get("client_instance_id"),
        "agent_run_id": data.get("agent_run_id"),
        "parent_run_id": data.get("parent_run_id"),
        "source": source,
    }
    perm_msg.update({"policy": policy, "supports_allow_all": candidate is not None,
                     "rule_preview": (f"{candidate['description']}; workspace {context['workspace']}; "
                                      f"branch #{context['branch_id']} and descendants; until revoked or server restart.")
                                      if candidate else "File preservation review is one-time; branch grants cannot bypass it." if integrity.review_reason
                                      else "Destructive review is one-time; branch grants cannot bypass it." if destructive_review
                                      else permission_rules.unavailable_reason(action, has_branch=bool(context["branch_id"]))})
    if risk_reason:
        perm_msg.update(
            {
                "risk_level": "destructive",
                "risk_reason": risk_reason,
            }
        )
    perm_msg.update(_generation_branch_for_scope(conv_id, permission_scope))
    # ``gen:<n>`` identifies a normal conversation generation while
    # ``agent:<uuid>`` identifies a supervisor-owned child run.  Both are
    # exact run scopes; neither may fall back to a conversation-wide socket.
    permission_run_id = canonical_run_id(
        permission_scope
        if permission_scope.startswith(("gen:", "agent:"))
        else None
    )
    perm_msg["run_id"] = permission_run_id
    # Persist to DB first (survives server restart)
    await db.save_pending_permission(
        request_id,
        conv_id,
        tool_name,
        tool_input,
        input_summary,
        run_id=permission_run_id,
        parent_id=perm_msg.get("parent_id"),
        draft_msg_id=perm_msg.get("draft_msg_id"),
        client_instance_id=perm_msg.get("client_instance_id"),
        permission_scope=permission_scope,
        source=source,
    )
    print(f"[PERM] Persisted permission request to DB: {request_id}")

    # Register the waiter BEFORE publishing. A fast browser response must
    # never arrive while the request exists only in the database.
    _pending_hook_permissions[request_id] = {
        "event": asyncio.Event(),
        "started_at": time.time(),
        "response": None,
        "conv_id": conv_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "input_summary": input_summary,
        "approval_method": approval_method,
        "permission_key": permission_key,
        "permission_scope": permission_scope,
        "run_id": permission_run_id,
        "risk_level": "destructive" if risk_reason else "",
        "risk_reason": risk_reason,
        "supports_allow_all": candidate is not None,
        "rule_candidate": candidate,
        "integrity_check": integrity,
        "rule_preview": perm_msg["rule_preview"],
        "policy": policy,
        "parent_id": perm_msg.get("parent_id"),
        "draft_msg_id": perm_msg.get("draft_msg_id"),
        "client_instance_id": perm_msg.get("client_instance_id"),
        "agent_run_id": data.get("agent_run_id"),
        "source": source,
    }

    # Route permission requests by the exact generation subscription.  A
    # manual request without a run remains conversation-scoped.
    permission_event = LoomEventEnvelope.from_payload(
        perm_msg,
        conversation_id=conv_id,
        run_id=permission_run_id,
        draft_id=perm_msg.get("draft_msg_id"),
        parent_id=perm_msg.get("parent_id"),
        client_instance_id=perm_msg.get("client_instance_id"),
        sequence=_event_sequencer.next(permission_run_id, conv_id),
    )
    recipients = _event_router.recipients(
        permission_event, _active_websockets.get(conv_id, set())
    )
    # A run may have multiple observers: for example, a window positioned on
    # a queued descendant subscribes to its predecessor so it can show that
    # predecessor's progress. Observation must not confer approval authority.
    # Interactive permission requests are therefore unicast to the immutable
    # page that launched the owning generation. This also covers the brief
    # startup interval before that page's explicit run subscription exists.
    origin_client_id = str(perm_msg.get("client_instance_id") or "").strip()
    if origin_client_id:
        origin = _find_client_websocket(conv_id, origin_client_id)
        if origin is not None:
            recipients = {origin}
        else:
            # A hard refresh replaces page identity. If the launch page is no
            # longer connected, recover only on pages whose durable cursor is
            # the exact owning assistant draft. Queued descendants and sibling
            # branches may observe the run but cannot inherit its approvals.
            recipients = {
                ws
                for ws in recipients
                if await _permission_socket_can_recover(
                    ws, conv_id, permission_run_id, perm_msg.get("draft_msg_id")
                )
            }
    sent_count = 0
    for ws in list(recipients):
        try:
            if child_run and agent_access.sensitive(child_run) and not loom_auth.credential(getattr(ws, "scope", {})):
                continue
            await ws.send_json(_scrub_surrogates(permission_event.as_dict()))
            sent_count += 1
        except Exception as exc:
            print(f"[PERM] Failed to send scoped permission to conv={conv_id}: {exc}")
            _active_websockets.get(conv_id, set()).discard(ws)
            _event_router.unregister(ws)
    print(f"[PERM] Routed permission_request to {sent_count} subscribed websocket(s)")
    await _ws_broadcast_all({"type": "permission_notice", "request_id": request_id,
                             "conv_id": conv_id, "status": "pending",
                             "message_id": perm_msg.get("draft_msg_id") or perm_msg.get("parent_id")})

    # Mark as broadcasted
    await db.mark_permission_broadcasted(request_id)

    # Wait for user response with a timeout.
    # Keep the tool blocked while it waits for a real user decision. Pending
    # requests are persisted and rebroadcast on reconnect/startup. The wait must
    # end before the caller gives up: Claude Code abandons its PreToolUse hook
    # after the configured hook timeout and treats that as a refusal, so a
    # later click would approve nothing. Other bridges wait a day.
    _PERM_TOTAL_DEADLINE = _permission_wait_limit(source)
    _PERM_POLL_INTERVAL = 30

    elapsed = 0
    user_response = None
    decision_source = ""
    while elapsed < _PERM_TOTAL_DEADLINE:
        perm_data = _pending_hook_permissions.get(request_id)
        if not perm_data:
            decision_source = "loom_lifecycle"
            break  # owning generation ended or request was otherwise retired
        if perm_data.get("response") is not None:
            user_response = perm_data["response"]
            decision_source = str(
                user_response.get("decision_source") or "user"
            )
            _pending_hook_permissions.pop(request_id, None)
            break

        # Wait on the event with a short timeout, then re-check
        wait = min(_PERM_POLL_INTERVAL, _PERM_TOTAL_DEADLINE - elapsed)
        try:
            await asyncio.wait_for(perm_data["event"].wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
        elapsed += wait
    else:
        # Deadline reached. A click that landed during the last wait still
        # counts; otherwise close the request so a late click cannot record
        # an approval that nothing is waiting for.
        perm_data = _pending_hook_permissions.get(request_id)
        if perm_data is not None and perm_data.get("response") is not None:
            user_response = perm_data["response"]
            decision_source = str(user_response.get("decision_source") or "user")
        elif perm_data is not None:
            decision_source = "loom_timeout"
            perm_data["response"] = {"allow": False, "always": False, "feedback": "",
                                     "decision_source": decision_source}
        _pending_hook_permissions.pop(request_id, None)

    if user_response is None:
        if not decision_source:
            decision_source = "loom_timeout"
        message = (
            "Loom permission request expired without a user decision; the agent was told no."
            if decision_source == "loom_timeout"
            else "Loom permission request was cancelled because its owning agent turn ended before a user decision."
        )
        if decision_source == "loom_timeout" and perm_data is not None:
            await _broadcast_permission_resolved(
                request_id, perm_data, allow=False, feedback=message,
                decision_source=decision_source, permission_scope=permission_scope)
        print(f"[PERM] Unresolved permission ended via {decision_source}: {request_id}")
        await db.save_permission_resolution(
            request_id,
            conv_id,
            tool_name,
            False,
            message,
            source=source,
            decision_source=decision_source,
            run_id=permission_run_id,
            parent_id=perm_msg.get("parent_id"),
            draft_msg_id=perm_msg.get("draft_msg_id"),
            permission_scope=permission_scope,
        )
        await db.delete_pending_permission(request_id)
        return {
            "allow": False,
            "message": message,
            "feedback": message,
            "decision_source": decision_source,
        }

    allowed = user_response.get("allow", False)
    if decision_source == "user":
        print(f"[PERM] User decision: {'allow' if allowed else 'deny'}")
    else:
        print(f"[PERM] Permission resolved by {decision_source}: {'allow' if allowed else 'cancel'}")

    if allowed:
        response = {
            "allow": True,
            # Rules remain in Loom. Native session-wide approval would bypass
            # descendant/workspace checks on the next request.
            "always": False,
            "decision_source": decision_source,
        }
    else:
        response = {
            "allow": False,
            "message": (
                "Denied by user in Loom UI"
                if decision_source == "user"
                else str(user_response.get("feedback") or "Permission request cancelled by Loom")
            ),
            "decision_source": decision_source,
        }
    feedback = str(user_response.get("feedback") or "").strip()[:4000]
    if feedback:
        response["feedback"] = feedback
        response["message"] = feedback
    return response


@app.post("/api/cc-tool-start")
async def handle_cc_tool_start(data: dict):
    """Receive tool start event from CC hook script, forward to event stream."""
    conv_id = int(data.get("loom_conv_id", 0))
    tool_name = data.get("tool_name", "")
    tool_id = data.get("tool_id", "")
    tool_input = data.get("tool_input", {})
    permission_scope = str(data.get("permission_scope") or "").strip()
    if not permission_scope and data.get("agent_run_id"):
        permission_scope = f"agent:{data['agent_run_id']}"
    
    print(f"[PERM] Hook tool-start: conv={conv_id} tool={tool_name} id={tool_id}")
    
    queue = gemini_client.active_queue(conv_id, permission_scope)
    if queue is not None:
        queue.put_nowait({
            "type": "tool_start",
            "name": tool_name,
            "tool_id": tool_id,
        })
        if tool_input:
            queue.put_nowait({
                "type": "tool_input_delta",
                "json": json.dumps(tool_input, indent=2),
                "tool_id": tool_id,
            })
    return {"status": "ok"}


@app.post("/api/cc-tool-result")
async def handle_cc_tool_result(data: dict):
    """Receive tool result event from CC hook script, forward to event stream."""
    conv_id = int(data.get("loom_conv_id", 0))
    tool_name = data.get("tool_name", "")
    tool_id = data.get("tool_id", "")
    content = data.get("content", "")
    is_error = data.get("is_error", False)
    permission_scope = str(data.get("permission_scope") or "").strip()
    if not permission_scope and data.get("agent_run_id"):
        permission_scope = f"agent:{data['agent_run_id']}"
    
    print(f"[PERM] Hook tool-result: conv={conv_id} tool={tool_name} id={tool_id} error={is_error}")
    
    queue = gemini_client.active_queue(conv_id, permission_scope)
    if queue is not None:
        queue.put_nowait({
            "type": "tool_result",
            "content": content,
            "tool_id": tool_id,
            "is_error": is_error,
        })
    return {"status": "ok"}


# ── Skills & Modules ──


@app.get("/api/skills")
async def list_skills(conv_id: int = None):
    """List available skills for a conversation (or globally).
    Scans both built-in skills and .claude/skills/ in the project directory.
    """
    project_dir = None
    agent = "claude"
    if conv_id:
        conv = await db.get_conversation(conv_id)
        if conv:
            project_dir = conv.get("project_dir")
            cc_model = (conv.get("cc_model") or "").lower()
            mode = (conv.get("mode") or "").lower()
            if mode == "codex" or is_codex_model(conv.get("cc_model") or ""):
                agent = "codex"
            elif mode == "gemini" or is_gemini_model(conv.get("cc_model") or "") or "antigravity" in cc_model:
                agent = "agy"
    skills = get_all_skills(project_dir, agent=agent)
    return skills


@app.get("/api/cc-hooks")
async def get_cc_hooks():
    """Read CC hooks from settings files and return them."""
    import json as _json
    hooks = {}
    # Check all settings locations CC uses
    paths = [
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.local.json",
        Path(".claude") / "settings.json",
        Path(".claude") / "settings.local.json",
    ]
    for p in paths:
        try:
            if p.is_file():
                data = _json.loads(p.read_text(encoding="utf-8"))
                file_hooks = data.get("hooks", {})
                if file_hooks:
                    hooks[str(p)] = file_hooks
        except Exception:
            pass
    return {"hooks": hooks, "paths": [str(p) for p in paths]}


@app.get("/api/codex-diagnostics")
async def codex_diagnostics(conv_id: int = None, project_dir: str = None):
    """Report Codex state that can surprise Loom users across arbitrary folders."""
    target_dir = project_dir
    if conv_id:
        conv = await db.get_conversation(conv_id)
        if conv:
            target_dir = conv.get("project_dir") or target_dir
    target_path = Path(target_dir).resolve() if target_dir else Path.cwd().resolve()

    config_path = Path.home() / ".codex" / "config.toml"
    config_text = ""
    stale_global_entries: list[str] = []
    if config_path.is_file():
        try:
            config_text = config_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            config_text = f"<read failed: {e}>"
    for marker in ("loom-file-edits", "loom-actions", "mcp_loom_file_edits", "mcp_loom_actions"):
        if marker in config_text:
            stale_global_entries.append(marker)

    project_hook_path = target_path / ".codex" / "hooks.json"
    project_hooks = None
    if project_hook_path.is_file():
        try:
            project_hooks = json.loads(project_hook_path.read_text(encoding="utf-8"))
        except Exception as e:
            project_hooks = {"error": str(e)}

    expected_approval, expected_sandbox = codex_client._codex_launch_policies(
        "default"
    )
    return {
        "target_dir": str(target_path),
        "expected_launch": {
            "surface": "app-server",
            "approval_policy": expected_approval,
            "sandbox": expected_sandbox,
            "writable_roots": [str(target_path)],
            "hook_scope": "disabled",
            "mcp_servers": ["loom-agent-control"],
        },
        "project_hook": {
            "path": str(project_hook_path),
            "exists": project_hook_path.is_file(),
            "scope": "project-local" if project_hook_path.is_file() else None,
            "ignored_by_loom_codex": True,
            "reason": "Codex app-server launches use --disable hooks; approval requests come from app-server JSON-RPC.",
            "hooks": project_hooks,
        },
        "global_config": {
            "path": str(config_path),
            "exists": config_path.is_file(),
            "stale_loom_entries": stale_global_entries,
        },
        "warnings": [
            *(
                ["Project-local .codex/hooks.json exists; Loom disables Codex hooks for app-server launches so stale repo hooks are ignored."]
                if project_hook_path.is_file()
                else []
            ),
            *(
                [f"Global Codex config contains stale Loom markers: {', '.join(stale_global_entries)}"]
                if stale_global_entries
                else []
            ),
        ],
    }


@app.get("/api/modules")
async def list_modules(module_type: str = None):
    """List registered modules from the database."""
    return await db.get_modules(module_type=module_type)


@app.post("/api/modules/sync")
async def sync_modules(conv_id: int = None):
    """Sync skills from filesystem into the modules database.
    Call this after changing project_dir or adding new skills.
    """
    project_dir = None
    if conv_id:
        conv = await db.get_conversation(conv_id)
        if conv:
            project_dir = conv.get("project_dir")
    skills = get_all_skills(project_dir)
    synced = []
    for skill in skills:
        module = await db.upsert_module(
            module_id=skill["id"],
            name=skill["name"],
            module_type="skill",
            description=skill.get("description", ""),
            source=skill.get("source_path", "builtin"),
            config={
                "command": skill.get("command", ""),
                "prompt_template": skill.get("prompt_template", ""),
            },
        )
        synced.append(module)
    return {"synced": len(synced), "modules": synced}


@app.put("/api/modules/{module_id}/enabled")
async def toggle_module(module_id: str, data: dict):
    """Enable or disable a module."""
    enabled = data.get("enabled", True)
    await db.set_module_enabled(module_id, enabled)
    return {"ok": True}


@app.post("/api/skills/create")
async def create_user_skill(data: dict):
    """Create a custom user skill by writing a SKILL.md file.

    Expects: { conv_id, name, description, prompt_template }
    Writes to: <project_dir>/.claude/skills/<name>/SKILL.md
    """
    conv_id = data.get("conv_id")
    name = data.get("name", "").strip().lower().replace(" ", "-")
    description = data.get("description", "")
    prompt_template = data.get("prompt_template", "")

    if not name:
        raise HTTPException(400, "Skill name is required")
    if not prompt_template:
        raise HTTPException(400, "Prompt template is required")

    # Determine project directory
    project_dir = "."
    if conv_id:
        conv = await db.get_conversation(int(conv_id))
        if conv and conv.get("project_dir"):
            project_dir = conv["project_dir"]

    skills_dir = Path(project_dir) / ".claude" / "skills" / name
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skills_dir / "SKILL.md"

    content = f"---\ndescription: {description}\n---\n\n{prompt_template}\n"
    skill_md.write_text(content, encoding="utf-8")

    return {
        "ok": True,
        "path": str(skill_md),
        "skill": {
            "id": f"skill:custom:{name}",
            "name": name,
            "command": f"/{name}",
            "description": description,
            "prompt_template": prompt_template,
        },
    }


@app.get("/api/skills/user")
async def list_user_skills(conv_id: int = None):
    """List only user-created custom skills (from .claude/skills/)."""
    from skill_scanner import scan_skills_dir

    project_dir = "."
    if conv_id:
        conv = await db.get_conversation(conv_id)
        if conv and conv.get("project_dir"):
            project_dir = conv["project_dir"]
    return scan_skills_dir(project_dir)


@app.delete("/api/skills/user/{skill_name}")
async def delete_user_skill(skill_name: str, conv_id: int = None):
    """Delete a user-created skill by removing its directory."""
    import shutil

    project_dir = "."
    if conv_id:
        conv = await db.get_conversation(conv_id)
        if conv and conv.get("project_dir"):
            project_dir = conv["project_dir"]
    skills_dir = Path(project_dir) / ".claude" / "skills" / skill_name
    if not skills_dir.exists():
        raise HTTPException(404, f"Skill '{skill_name}' not found")
    shutil.rmtree(skills_dir)
    return {"ok": True, "deleted": skill_name}


# ── WebSocket Chat ──


def _scrub_surrogates(obj):
    """Strip unpaired UTF-16 surrogates from strings in a JSON-serializable obj.

    CC tool inputs (especially Bash command strings chunked mid-codepoint) can
    contain lone surrogates like '\\udc90'. json.dumps + websocket.send_json
    encodes to UTF-8 which rejects them, raising UnicodeEncodeError and
    tearing down the WS handler. This recurses through dicts/lists and
    replaces bad chars with U+FFFD.
    """
    if isinstance(obj, str):
        try:
            obj.encode("utf-8")
            return obj
        except UnicodeEncodeError:
            return obj.encode("utf-8", "replace").decode("utf-8")
    if isinstance(obj, dict):
        return {k: _scrub_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_surrogates(v) for v in obj]
    return obj


async def _ws_send(conv_id: int, data: dict):
    """Normalize and route one event through the explicit subscription bus.

    The old implementation used ``_origin_ws`` as an implicit subscription and
    allowed conversation-wide delivery for events without a generation task.
    Every event now receives a typed identity envelope.  Run events have no
    delivery fallback: a socket must explicitly subscribe to that run.
    """
    task = asyncio.current_task()
    gen_key = getattr(task, "_gen_key", None)
    snapshot = _generation_snapshots.get(gen_key) if gen_key else {}
    snapshot = snapshot or {}
    run_id = data.get("run_id")
    if run_id is None and gen_key:
        run_id = f"gen:{gen_key[2]}"
    tagged = dict(data)
    if gen_key:
        tagged.setdefault("gen_id", gen_key[2])
        tagged.setdefault("parent_id", snapshot.get("parent_id", gen_key[1]))
        if snapshot.get("draft_msg_id") is not None:
            tagged.setdefault("draft_msg_id", snapshot.get("draft_msg_id"))
        client_instance_id = snapshot.get("client_instance_id") or getattr(
            task, "_client_instance_id", None
        )
        if client_instance_id:
            tagged.setdefault("client_instance_id", client_instance_id)
    envelope = LoomEventEnvelope.from_payload(
        tagged,
        conversation_id=conv_id,
        loom_thread_id=(
            tagged.get("loom_thread_id") or snapshot.get("loom_thread_id")
        ),
        run_id=run_id,
        draft_id=tagged.get("draft_msg_id"),
        parent_id=tagged.get("parent_id"),
        client_instance_id=tagged.get("client_instance_id"),
        sequence=_event_sequencer.next(canonical_run_id(run_id), conv_id),
    )
    data = envelope.as_dict()
    clients = _active_websockets.get(conv_id, set())
    # Never derive a subscription from the task's origin socket. Launch,
    # reconnect, and explicit subscribe paths establish authorization before
    # content is emitted; absent a subscription, a run event has no recipient.
    recipients = _event_router.recipients(envelope, clients)
    _t = data.get("type", "?")
    # Per-conv tracker for the /api/debug/stream-state endpoint: lets a
    # future LLM (or admin) ask "is conv N actively streaming right now?"
    # without scraping logs. Only stream-shaped events count.
    if _t in _STREAM_EVENT_TYPES:
        _stream_state[conv_id] = {
            "last_event_type": _t,
            "last_event_at": time.time(),
            "events_sent": _stream_state.get(conv_id, {}).get("events_sent", 0) + 1,
            "clients_at_send": len(recipients) if recipients else 0,
            "connections_at_send": len(clients),
        }
    _content = data.get("content", "") or ""
    _content_bytes = len(_content.encode("utf-8", errors="replace")) if isinstance(_content, str) else 0
    print(f"[WS-TRACE] conv={conv_id} type={_t} run={envelope.run_id or '-'} seq={envelope.sequence} clients={len(recipients) if recipients else 0} connections={len(clients)} recipients={len(recipients)} bytes={_content_bytes}")
    if not recipients:
        return
    dead = []
    scrubbed = None
    for ws in list(recipients):
        try:
            await asyncio.wait_for(ws.send_json(data), timeout=5.0)
        except UnicodeEncodeError:
            # Unpaired surrogate in data — socket is fine, payload is bad.
            # Scrub once and retry; don't mark ws dead.
            if scrubbed is None:
                scrubbed = _scrub_surrogates(data)
            try:
                await asyncio.wait_for(ws.send_json(scrubbed), timeout=5.0)
            except Exception:
                dead.append(ws)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)
        _event_router.unregister(ws)


async def _ws_reply(websocket: WebSocket, conv_id: int, data: dict) -> None:
    """Send a direct, typed conversation reply to one socket.

    Action-level errors and acknowledgements do not originate in a provider
    generation, but they still use the same wire envelope so clients never
    have to guess whether an untyped payload is a stream event.
    """
    envelope = LoomEventEnvelope.from_payload(
        data,
        conversation_id=conv_id,
        run_id=data.get("run_id") or data.get("gen_id"),
        draft_id=data.get("draft_msg_id"),
        parent_id=data.get("parent_id"),
        client_instance_id=data.get("client_instance_id"),
        scope="conversation",
        sequence=_event_sequencer.next(None, conv_id),
    )
    await websocket.send_json(_scrub_surrogates(envelope.as_dict()))


def _has_active_generation_for_conv(conv_id: int) -> bool:
    return any(
        k[0] == conv_id and not task.done()
        for k, task in _active_generations.items()
    )


async def _provider_lane_occupied(conv_id: int) -> bool:
    """Return whether SQLite still owns the serial provider lane.

    Task and PID state remain defensive evidence for legacy/in-flight rows,
    but neither can grant admission. New runs acquire the database lease
    atomically before an asyncio task or provider process exists.
    """
    try:
        if await db.get_live_generation_lease(conv_id):
            return True
    except Exception as exc:
        print(f"[GEN] Could not verify generation lease for conv={conv_id}: {exc!r}")
        return True
    if _has_active_generation_for_conv(conv_id):
        return True
    try:
        rows = await db.list_active_generations()
    except Exception as exc:
        print(f"[GEN] Could not verify provider lane for conv={conv_id}: {exc!r}")
        return True
    return any(
        int(row.get("conv_id")) == int(conv_id)
        and bool(row.get("pid"))
        and _pid_alive(int(row["pid"]))
        for row in rows
    )


async def _single_lane_queue_barrier_draft_id(conv_id: int) -> int | None:
    """Return the one durable draft currently occupying a serial lane.

    This is execution ordering only; it must never become the new message's
    topology parent unless the send is an actual continuation of that draft.
    A missing or ambiguous draft fails closed so Loom never guesses which run
    a queued launch should follow.
    """
    draft_ids: set[int] = set()
    try:
        lease = await db.get_live_generation_lease(conv_id)
    except Exception as exc:
        print(f"[GEN] Could not resolve generation lease for conv={conv_id}: {exc!r}")
        return None
    if lease and lease.get("draft_msg_id") is not None:
        draft_ids.add(int(lease["draft_msg_id"]))
    for key, task in _active_generations.items():
        if key[0] != int(conv_id) or task.done():
            continue
        snapshot = _generation_snapshots.get(key) or {}
        draft_id = snapshot.get("draft_msg_id")
        if draft_id is not None:
            draft_ids.add(int(draft_id))
    try:
        rows = await db.list_active_generations()
    except Exception as exc:
        print(f"[GEN] Could not resolve queue barrier for conv={conv_id}: {exc!r}")
        return None
    for row in rows:
        if int(row.get("conv_id")) != int(conv_id):
            continue
        pid = row.get("pid")
        if pid and _pid_alive(int(pid)):
            draft_ids.add(int(row["draft_msg_id"]))
    if len(draft_ids) != 1:
        return None
    return next(iter(draft_ids))


async def _pause_serial_queue_for_cancel(
    conv_id: int, *, draft_msg_id: int | None = None
) -> int:
    """Make cancel/kill a durable stop boundary before touching the task."""
    lease = await db.get_live_generation_lease(conv_id)
    if (
        lease
        and lease.get("draft_msg_id") is not None
        and draft_msg_id is not None
        and int(lease["draft_msg_id"]) != int(draft_msg_id)
    ):
        # A stale admin row must not pause or release a newer exact run.
        return 0
    if lease:
        await db.mark_generation_lease_cancelling(lease["lease_id"])
    conv = await db.get_conversation(conv_id)
    parallel_enabled = bool(
        conv and _truthy_setting(conv.get("parallel_agents_enabled"))
    )
    # A serial cancellation pauses the whole lane. In an explicitly parallel
    # conversation, only continuations dependent on this exact draft pause.
    paused = await db.pause_queued_generations(
        conv_id,
        wait_for_draft_id=(draft_msg_id if parallel_enabled else None),
    )
    if paused:
        await _ws_send(
            conv_id,
            {
                "type": "queue_changed",
                "queued": True,
                "status": "paused",
                "wait_for_draft_id": draft_msg_id,
            },
        )
    return paused


def _active_generation_keys_for_cancel(
    conv_id: int,
    gen_id: int | str | None = None,
    draft_msg_id: int | str | None = None,
) -> list[tuple[int, int | None, int]]:
    """Resolve a cancel request to one generation, or all for legacy clients."""
    selected = []
    gen_text = str(gen_id) if gen_id is not None else None
    draft_text = str(draft_msg_id) if draft_msg_id is not None else None
    scoped = gen_text is not None or draft_text is not None
    for key, task in _active_generations.items():
        if key[0] != conv_id or task.done():
            continue
        snapshot = _generation_snapshots.get(key) or {}
        if gen_text is not None and str(key[2]) != gen_text:
            continue
        if draft_text is not None and str(snapshot.get("draft_msg_id")) != draft_text:
            continue
        selected.append(key)
    if scoped:
        return selected
    return [
        key for key, task in _active_generations.items()
        if key[0] == conv_id and not task.done()
    ]


def _generation_stop_disposition(task: asyncio.Task | None = None) -> str:
    """Return the user-requested stop meaning for the current generation."""
    task = task or asyncio.current_task()
    disposition = getattr(task, "_stop_disposition", "cancel") if task else "cancel"
    return "interrupt" if disposition == "interrupt" else "cancel"


def _request_generation_stop(task: asyncio.Task, disposition: str) -> None:
    """Annotate the exact task before injecting CancelledError into it."""
    task._stop_disposition = (
        "interrupt" if disposition == "interrupt" else "cancel"
    )
    task.cancel()


async def _finalize_user_stopped_generation(
    conv_id: int,
    draft_msg_id: int | None,
    *,
    partial_text: str = "",
    content_blocks=None,
    session_id: str | None = None,
    session_mode: str | None = None,
    model_used: str | None = None,
    model_attestation=None,
    generation_ms: int | None = None,
    time_to_first_token_ms: int | None = None,
) -> dict | None:
    """Persist the invariant difference between Cancel and Interrupt.

    Cancel retains the topology node but destroys partial provider context.
    Interrupt retains partial text/tools and native-session ownership so a
    later message can continue deliberately from that stopped turn.
    """
    if not draft_msg_id:
        return None
    disposition = _generation_stop_disposition()
    if disposition == "interrupt":
        text = partial_text if str(partial_text or "").strip() else "[Turn interrupted before response]"
        serialized_blocks = None
        if content_blocks:
            serialized_blocks = (
                content_blocks
                if isinstance(content_blocks, str)
                else json.dumps(content_blocks)
            )
        serialized_attestation = None
        if model_attestation:
            serialized_attestation = (
                model_attestation
                if isinstance(model_attestation, str)
                else json.dumps(model_attestation, separators=(",", ":"))
            )
        await db.update_message_content(
            draft_msg_id,
            content=text,
            content_blocks=serialized_blocks,
            cc_session_id=session_id,
            cc_session_mode=session_mode,
            cc_model_used=model_used,
            model_attestation=serialized_attestation,
            generation_ms=generation_ms,
            time_to_first_token_ms=time_to_first_token_ms,
            turn_lifecycle_state="interrupted",
        )
    else:
        await db.discard_message_partial_context(
            draft_msg_id,
            generation_ms=generation_ms,
            time_to_first_token_ms=time_to_first_token_ms,
        )
    message = await db.get_message(draft_msg_id)
    event_type = "interrupted" if disposition == "interrupt" else "cancelled"
    await _ws_send(
        conv_id,
        {
            "type": event_type,
            "disposition": disposition,
            "message": dict(message) if message else None,
            "draft_msg_id": draft_msg_id,
        },
    )
    task = asyncio.current_task()
    if task is not None:
        task._stop_event_sent = True
    return message


async def _send_generation_idle_if_quiescent(conv_id: int):
    if not await _provider_lane_occupied(conv_id):
        await _ws_send(conv_id, {"type": "generation_idle"})


async def _probe_followed_generation(websocket, conv_id: int, data: dict):
    """Recover a missed terminal event for exactly one page's followed draft.

    A healthy heartbeat does not prove that stream_end arrived. Do not infer
    completion from silence, a sibling run, or an empty in-memory registry.
    Only a persisted terminal row may replace the followed stream.
    """
    run_id = canonical_run_id(data.get("run_id"))
    try:
        draft_id = int(data.get("draft_msg_id"))
        gen_id = int(run_id[4:]) if run_id and run_id.startswith("gen:") else None
    except (ValueError, TypeError):
        return
    if gen_id is None:
        return
    for key, task in list(_active_generations.items()):
        snap = _generation_snapshots.get(key) or {}
        active_draft_id = snap.get("draft_msg_id") or getattr(task, "_draft_msg_id", None)
        if key[0] != conv_id or str(active_draft_id) != str(draft_id) or task.done():
            continue
        # Generation counters may be reused after a host restart. The durable
        # draft ID must agree as well; never attach a different run.
        if run_id == f"gen:{key[2]}" and run_id not in _event_router.subscriptions(websocket):
            if await _websocket_can_subscribe_run(websocket, conv_id, run_id):
                _event_router.subscribe(websocket, run_id)
                await _send_active_gen_state(websocket, conv_id)
        return
    message = await db.get_message(draft_id)
    if not message or message.get("conversation_id") != conv_id or message.get("role") != "assistant":
        return
    lifecycle = message.get("turn_lifecycle_state")
    if lifecycle not in {"completed", "cancelled", "interrupted", "failed", "partial_failed"}:
        # Legacy completed rows do not have an explicit lifecycle stamp.
        if message.get("generation_ms") is None:
            return
    cursor = await db.get_client_loom_cursor(conv_id, getattr(websocket, "_client_instance_id", ""))
    if not cursor:
        return
    # A queued continuation may be the cursor leaf; its followed draft is an
    # ancestor. Read-only ancestry validation never advances that cursor.
    ancestor_ids = await db.get_message_ancestor_ids(conv_id, cursor["leaf_message_id"]) if cursor.get("leaf_message_id") else set()
    if draft_id not in ancestor_ids:
        return
    event = LoomEventEnvelope.from_payload(
        {"type": "generation_terminal", "message": dict(message), "gen_id": gen_id},
        conversation_id=conv_id, run_id=run_id, draft_id=draft_id,
        parent_id=message.get("parent_id"), loom_thread_id=cursor.get("thread_id"),
        scope="run", sequence=_event_sequencer.next(run_id, conv_id),
    )
    await websocket.send_json(event.as_dict())


async def _record_first_response(conv_id, draft_id, gen_key, started_at, first_ms):
    """Persist first observable provider output, including local reasoning."""
    if first_ms is not None:
        return first_ms
    import time
    first_ms = max(0, int((time.time() - started_at) * 1000))
    await db.update_message_content(draft_id, time_to_first_token_ms=first_ms)
    if gen_key:
        _update_gen_snapshot(gen_key, time_to_first_token_ms=first_ms)
    await _ws_send(conv_id, {"type": "first_token", "elapsed_ms": first_ms})
    return first_ms


async def _permission_socket_can_recover(websocket, conv_id, run_id, draft_msg_id) -> bool:
    """Child controls are conversation-scoped; parent approvals remain draft-exact."""
    if str(run_id or "").startswith("agent:"):
        return (
            run_id in _event_router.subscriptions(websocket)
            and await _websocket_can_subscribe_run(websocket, conv_id, run_id)
        )
    return await _permission_socket_is_on_exact_draft(websocket, conv_id, draft_msg_id)


async def _child_permission_response_authorized(websocket, conv_id, pending) -> bool:
    if int(pending.get("conv_id") or 0) != int(conv_id):
        return False
    origin_id = str(pending.get("client_instance_id") or "")
    if origin_id:
        if origin_id == str(getattr(websocket, "_client_instance_id", "") or ""):
            return True
        if _find_client_websocket(conv_id, origin_id) is not None:
            return False
    return await _permission_socket_can_recover(
        websocket, conv_id, pending.get("run_id") or pending.get("permission_scope"), None,
    )


async def _send_pending_permissions_to_socket(
    websocket: WebSocket, conv_id: int, *, only_new: bool = False
) -> None:
    """Replay only permissions visible to this socket's explicit runs."""
    try:
        rows = await db.get_pending_permissions_for_conv(conv_id)
    except Exception as exc:
        print(f"[WS] Could not load pending permissions for conv={conv_id}: {exc}")
        return
    subscriptions = _event_router.subscriptions(websocket)
    delivered = getattr(websocket, "_delivered_permission_ids", set())
    for row in rows:
        if only_new and row.get("request_id") in delivered:
            continue
        pending = _pending_hook_permissions.get(row.get("request_id"), {})
        if pending.get("response") is not None:
            continue
        permission_scope = (
            pending.get("permission_scope")
            or row.get("permission_scope")
            or ""
        )
        raw_run_id = pending.get("run_id") or row.get("run_id")
        if raw_run_id is None and permission_scope.startswith(("gen:", "agent:")):
            raw_run_id = permission_scope
        run_id = canonical_run_id(raw_run_id)
        if run_id and run_id.startswith("agent:"):
            child = await db.get_agent_run(run_id.split(":", 1)[1])
            if child and agent_access.sensitive(child) and not loom_auth.credential(getattr(websocket, "scope", {})):
                continue
        origin_client_id = (
            pending.get("client_instance_id")
            or row.get("client_instance_id")
            or ""
        )
        websocket_client_id = str(
            getattr(websocket, "_client_instance_id", "") or ""
        )
        # As with live delivery, observing an exact run does not grant a page
        # authority over its interactive permission prompts. A persisted
        # origin always wins; subscription routing is only the compatibility
        # fallback for legacy rows that predate page identity.
        if origin_client_id:
            if str(origin_client_id) != websocket_client_id:
                origin_connected = (
                    _find_client_websocket(conv_id, origin_client_id) is not None
                )
                if origin_connected or not await _permission_socket_can_recover(
                    websocket, conv_id, run_id, row.get("draft_msg_id")
                ):
                    continue
        elif run_id is not None and run_id not in subscriptions:
            continue
        payload = {
            "type": "permission_request",
            "request_id": row.get("request_id"),
            "conv_id": conv_id,
            "tool_name": pending.get("tool_name") or row.get("tool_name"),
            "tool_input": pending.get("tool_input") or row.get("tool_input"),
            "input_summary": pending.get("input_summary") or row.get("input_summary"),
            "approval_method": pending.get("approval_method", ""),
            "permission_scope": permission_scope,
            "source": pending.get("source") or row.get("source") or "",
            "parent_id": pending.get("parent_id") or row.get("parent_id"),
            "draft_msg_id": pending.get("draft_msg_id") or row.get("draft_msg_id"),
            "client_instance_id": pending.get("client_instance_id") or row.get("client_instance_id"),
            "supports_allow_all": pending.get("supports_allow_all", False),
            "rule_preview": pending.get("rule_preview", "This request supports one-time approval only."),
            "policy": pending.get("policy"),
            "risk_level": pending.get("risk_level", ""),
            "risk_reason": pending.get("risk_reason", ""),
        }
        payload.update(_generation_branch_for_scope(conv_id, permission_scope))
        envelope = LoomEventEnvelope.from_payload(
            payload,
            conversation_id=conv_id,
            run_id=run_id,
            draft_id=payload.get("draft_msg_id"),
            parent_id=payload.get("parent_id"),
            client_instance_id=payload.get("client_instance_id"),
            sequence=_event_sequencer.next(run_id, conv_id),
        )
        try:
            await websocket.send_json(_scrub_surrogates(envelope.as_dict()))
            delivered.add(row.get("request_id"))
            websocket._delivered_permission_ids = delivered
        except Exception as exc:
            print(f"[WS] Failed to replay permission {row.get('request_id')}: {exc}")


async def _handle_generation_with_workspace_safety(
    websocket: WebSocket | None, conv_id: int, data: dict
):
    """Wrap every tool-capable provider turn in one neutral recovery boundary."""
    snapshot = None
    conv = await db.get_conversation(conv_id)
    project_dir = conv.get("project_dir") if conv else None
    mode = conv.get("mode") if conv else None
    gen_key = getattr(asyncio.current_task(), "_gen_key", None)
    generation_id = gen_key[2] if gen_key else data.get("_gen_id")

    async def _preparing(phase: str, text: str, **extra) -> None:
        if gen_key:
            _update_gen_snapshot(
                gen_key,
                phase=phase,
                status_text=text,
                **extra,
            )
        await _ws_send(
            conv_id,
            {
                "type": "generation_preparing",
                "phase": phase,
                "text": text,
                "parent_id": data.get("parent_id"),
                "cc_model": data.get("cc_model") or (conv or {}).get("cc_model"),
                **extra,
            },
        )

    preparation_started = time.monotonic()
    preparation_limit = max(0.01, float(getattr(config, "context_preparation_timeout_seconds", 120)))

    async def _bounded_workspace_preparation(function, *args, phase, **kwargs):
        work = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            while True:
                elapsed = time.monotonic() - preparation_started
                remaining = preparation_limit - elapsed
                if remaining <= 0:
                    raise TimeoutError(f"Workspace preparation exceeded {preparation_limit:g}s; provider was not launched")
                done, _ = await asyncio.wait({work}, timeout=min(10, remaining))
                if done:
                    return work.result()
                await _preparing(phase, f"Preparing workspace ({int(time.monotonic() - preparation_started)}s elapsed)…")
        finally:
            # Cancellation stops awaiting the worker, never starts a provider.
            # The filesystem worker may finish its external recovery writes.
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)

    if project_dir and mode in _AGENT_WORKSPACE_MODES:
        await _preparing(
            "workspace_snapshot",
            "Preparing workspace recovery snapshot…",
        )
        recovery_root = default_recovery_root()
        snapshot_started = time.perf_counter()
        try:
            snapshot = await _bounded_workspace_preparation(
                capture_workspace_snapshot,
                project_dir,
                recovery_root,
                conversation_id=conv_id,
                generation_id=generation_id,
                phase="workspace_snapshot",
            )
        except TimeoutError:
            raise
        except Exception as exc:
            print(f"[WORKSPACE-SAFETY] Snapshot failed for conv={conv_id}: {exc!r}")
            elapsed_ms = int((time.perf_counter() - snapshot_started) * 1000)
            await _preparing(
                "launching",
                "Workspace recovery unavailable · launching agent…",
                preparation_ms=elapsed_ms,
            )
        else:
            elapsed_ms = int((time.perf_counter() - snapshot_started) * 1000)
            recovery_stats = getattr(snapshot, "stats", None) or {}
            skipped = recovery_stats.get("skipped_count", 0)
            if snapshot is None:
                status_text = "Workspace recovery unavailable · launching agent…"
            elif skipped:
                status_text = f"Workspace recovery partial ({skipped} files skipped; {elapsed_ms / 1000:.1f}s) · launching agent…"
            else:
                reused = recovery_stats.get("files_reused", 0)
                reuse_note = f"; reused {reused} files" if reused else ""
                status_text = f"Workspace protected ({elapsed_ms / 1000:.1f}s{reuse_note}) · launching agent…"
            await _preparing(
                "launching",
                status_text,
                preparation_ms=elapsed_ms,
                recovery_stats=recovery_stats,
            )
    else:
        await _preparing("launching", "Launching agent…")

    try:
        if conv and conv.get("canvas_enabled") and project_dir:
            await _bounded_workspace_preparation(
                ensure_canvas_workspace, project_dir, CANVAS_CLAUDE_MD,
                phase="canvas_workspace",
            )
        await _handle_generation(websocket, conv_id, data)
    finally:
        if snapshot is not None:
            try:
                report = await asyncio.to_thread(finalize_workspace_snapshot, snapshot)
                if report.get("changed_count") or report.get("warnings"):
                    await _ws_send(conv_id, report)
            except Exception as exc:
                print(f"[WORKSPACE-SAFETY] Change report failed for conv={conv_id}: {exc!r}")


async def _ws_broadcast_all(data: dict):
    """Broadcast only content-free global notifications.

    Generated text, tool output, permissions, and branch mutation events must
    use :func:`_ws_send` and an exact run subscription.  This helper is kept
    for liveness/notification signals and strips generated previews before
    delivery so a global event can never mutate an unrelated chat.
    """
    raw = dict(data or {})
    kind = str(raw.get("event_kind") or raw.get("type") or "event")
    if kind not in GLOBAL_NOTIFICATION_KINDS:
        print(f"[WS] Refusing non-notification global event: {kind}")
        return
    raw = content_free_notification(raw)
    envelope = LoomEventEnvelope.from_payload(
        raw,
        conversation_id=raw.get("conversation_id", raw.get("conv_id")),
        run_id=None,
        draft_id=None,
        parent_id=None,
        client_instance_id=None,
        sequence=_event_sequencer.next(None, raw.get("conversation_id", raw.get("conv_id"))),
        scope="global",
    )
    event = envelope.as_dict()
    dead_pairs = []
    for cid, clients in list(_active_websockets.items()):
        recipients = _event_router.recipients(envelope, clients)
        for ws in list(recipients):
            try:
                await ws.send_json(event)
            except Exception:
                dead_pairs.append((cid, ws))
    for cid, ws in dead_pairs:
        remaining = _active_websockets.get(cid)
        if remaining:
            remaining.discard(ws)
            _event_router.unregister(ws)
            if not remaining:
                _active_websockets.pop(cid, None)


async def _send_active_gen_state(websocket: WebSocket, conv_id: int) -> bool:
    """Send generation_active (with snapshots) or generation_idle to one ws.
    Returns True if a generation is active, False otherwise."""
    active_gen_keys = [
        k for k, t in _active_generations.items() if k[0] == conv_id and not t.done()
    ]
    if not active_gen_keys:
        idle = LoomEventEnvelope.from_payload(
            {"type": "generation_idle"},
            conversation_id=conv_id,
            scope="conversation",
            sequence=_event_sequencer.next(None, conv_id),
        )
        await websocket.send_json(idle.as_dict())
        return False
    snapshots = []
    subscriptions = _event_router.subscriptions(websocket)
    for gk in active_gen_keys:
        snap = _generation_snapshots.get(gk)
        if snap:
            snapshot = {
                    "gen_id": gk[2],
                    "run_id": f"gen:{gk[2]}",
                    "parent_id": snap.get("parent_id"),
                    "draft_msg_id": snap.get("draft_msg_id"),
                    "input_tokens": snap.get("input_tokens", 0),
                    "output_tokens": snap.get("output_tokens", 0),
                    "started_at": snap.get("started_at", 0),
                    "cc_model": snap.get("cc_model", ""),
                    "model_attestation": snap.get("model_attestation"),
                    "mode": snap.get("mode", "claude"),
                    "client_instance_id": snap.get("client_instance_id"),
                    "loom_thread_id": snap.get("loom_thread_id"),
                    "phase": snap.get("phase"),
                    "status_text": snap.get("status_text"),
                    "preparation_ms": snap.get("preparation_ms"),
                    "time_to_first_token_ms": snap.get("time_to_first_token_ms"),
                }
            # A conversation socket may learn that runs exist, but generated
            # content is a run-scoped payload.  Only an explicit subscription
            # receives the progressive draft needed for reconnect recovery.
            if snapshot["run_id"] in subscriptions:
                snapshot["full_text"] = snap.get("full_text", "")
                snapshot["content_blocks"] = snap.get("content_blocks", [])
            snapshots.append(snapshot)
    active_event = LoomEventEnvelope.from_payload(
        {"type": "generation_active", "snapshots": snapshots},
        conversation_id=conv_id,
        scope="conversation",
        sequence=_event_sequencer.next(None, conv_id),
    )
    await websocket.send_json(active_event.as_dict())
    return True


async def _websocket_can_subscribe_run(
    websocket: WebSocket,
    conv_id: int,
    run_id: str | int | None,
) -> bool:
    """Authorize subscriptions from durable topology, never browser focus.

    Normal generation output is visible to a page only when that page's
    server cursor is on the run's exact parent or draft in the same Loom
    thread. Child-agent activity is a separate conversation-scoped surface.
    """
    normalized = canonical_run_id(run_id)
    if not normalized:
        return False
    if normalized.startswith("agent:"):
        run = await db.get_agent_run(normalized.split(":", 1)[1])
        return bool(run and int(run.get("conversation_id")) == int(conv_id))
    if not normalized.startswith("gen:"):
        return False
    try:
        gen_id = int(normalized.split(":", 1)[1])
    except (TypeError, ValueError):
        return False
    match = None
    for key, task in _active_generations.items():
        if key[0] == int(conv_id) and key[2] == gen_id and not task.done():
            match = (key, _generation_snapshots.get(key) or {})
            break
    if not match:
        return False
    client_instance_id = getattr(websocket, "_client_instance_id", None)
    if not client_instance_id:
        return False
    cursor = await db.get_client_loom_cursor(conv_id, client_instance_id)
    if not cursor:
        return False
    key, snapshot = match
    thread_id = snapshot.get("loom_thread_id")
    cursor_leaf = cursor.get("leaf_message_id")
    allowed_leaf_ids = {
        str(snapshot.get("parent_id", key[1])),
        str(snapshot.get("draft_msg_id")),
    }
    if cursor_leaf is not None and str(cursor_leaf) not in allowed_leaf_ids:
        queued = await db.list_queued_generations(conv_id)
        allowed_leaf_ids.update(
            str(item.get("user_msg_id"))
            for item in queued
            if str(item.get("wait_for_draft_id"))
            == str(snapshot.get("draft_msg_id"))
        )
    return bool(
        thread_id
        and str(cursor.get("thread_id")) == str(thread_id)
        and cursor_leaf is not None
        and str(cursor_leaf) in allowed_leaf_ids
    )


async def _prune_client_generation_subscriptions(
    conv_id: int, client_instance_id: str | None
) -> None:
    """Drop run subscriptions that no longer match a page's server cursor."""
    if not client_instance_id:
        return
    for websocket in list(_active_websockets.get(conv_id, set())):
        if str(getattr(websocket, "_client_instance_id", "")) != str(
            client_instance_id
        ):
            continue
        for run_id in list(_event_router.subscriptions(websocket)):
            if run_id.startswith("gen:") and not await _websocket_can_subscribe_run(
                websocket, conv_id, run_id
            ):
                _event_router.unsubscribe(websocket, run_id)
        await _subscribe_cursor_generation(websocket, conv_id)


async def _subscribe_cursor_generation(websocket: WebSocket, conv_id: int) -> str | None:
    """Subscribe a page when exactly one run occupies its exact thread leaf."""
    client_instance_id = getattr(websocket, "_client_instance_id", None)
    if not client_instance_id:
        return None
    cursor = await db.get_client_loom_cursor(conv_id, client_instance_id)
    if not cursor or cursor.get("leaf_message_id") is None:
        return None
    queued = await db.list_queued_generations(conv_id)
    candidates = []
    for key, task in _active_generations.items():
        if key[0] != int(conv_id) or task.done():
            continue
        snapshot = _generation_snapshots.get(key) or {}
        if str(snapshot.get("loom_thread_id") or "") != str(cursor.get("thread_id") or ""):
            continue
        allowed_leaf_ids = {
            str(snapshot.get("parent_id", key[1])),
            str(snapshot.get("draft_msg_id")),
        }
        allowed_leaf_ids.update(
            str(item.get("user_msg_id"))
            for item in queued
            if str(item.get("wait_for_draft_id"))
            == str(snapshot.get("draft_msg_id"))
        )
        if str(cursor["leaf_message_id"]) not in allowed_leaf_ids:
            continue
        candidates.append(key)
    if len(candidates) != 1:
        return None
    selected_key = candidates[0]
    selected_snapshot = _generation_snapshots.get(selected_key) or {}
    if (
        selected_snapshot.get("draft_msg_id") is not None
        and str(cursor.get("leaf_message_id"))
        == str(selected_snapshot.get("parent_id", selected_key[1]))
    ):
        try:
            await db.advance_loom_cursor(
                cursor["cursor_id"],
                conversation_id=conv_id,
                client_instance_id=client_instance_id,
                thread_id=cursor["thread_id"],
                expected_leaf_id=cursor["leaf_message_id"],
                expected_revision=cursor["revision"],
                new_leaf_id=int(selected_snapshot["draft_msg_id"]),
            )
        except db.LoomCursorConflict:
            return None
    run_id = f"gen:{selected_key[2]}"
    _event_router.subscribe(websocket, run_id)
    return run_id


@app.websocket("/ws/chat/{conv_id}")
async def ws_chat(websocket: WebSocket, conv_id: int):
    await websocket.accept()
    websocket._loom_connected_at = time.monotonic()
    websocket_client_instance_id = websocket.query_params.get("client_instance_id")
    websocket_run_id = websocket.query_params.get("run_id")
    websocket._client_instance_id = websocket_client_instance_id
    websocket._agent_event_batches = websocket.query_params.get("child_event_batches") == "1"
    print(f"[WS] Connection opened for conv={conv_id}")
    if conv_id not in _active_websockets:
        _active_websockets[conv_id] = set()
    _active_websockets[conv_id].add(websocket)
    _event_router.register(websocket, conv_id)

    # The durable cursor is the authority. A remembered run id is only a hint;
    # it cannot subscribe a page positioned on another thread.
    if websocket_run_id and await _websocket_can_subscribe_run(
        websocket, conv_id, websocket_run_id
    ):
        _event_router.subscribe(websocket, canonical_run_id(websocket_run_id))
    else:
        await _subscribe_cursor_generation(websocket, conv_id)
    await _continue_ws_chat(
        websocket, conv_id, websocket_client_instance_id
    )


def _active_generation_snapshots_for_conv(conv_id: int) -> list[dict]:
    snapshots = []
    for key, task in _active_generations.items():
        if key[0] != conv_id or task.done():
            continue
        snapshot = dict(_generation_snapshots.get(key) or {})
        snapshot.setdefault("parent_id", key[1])
        snapshot.setdefault("gen_id", key[2])
        snapshots.append(snapshot)
    return snapshots


async def _resolve_queue_target(
    conv_id: int,
    anchor_id: int | None,
    explicit_draft_id: int | str | None = None,
) -> int | None:
    """Resolve continuation intent against live branch state.

    The request's anchor is authoritative. We only queue when that anchor is
    the active draft itself or the parent of exactly one active draft. Focus,
    the database-wide active flag, and another tab's stream are irrelevant.
    """
    snapshots = _active_generation_snapshots_for_conv(conv_id)
    # Queue topology and lane serialization must consult the same durable
    # owner. A missing streaming snapshot is not evidence the draft vanished.
    lease = await db.get_live_generation_lease(conv_id)
    durable_ids = set()
    if lease and lease.get("draft_msg_id") is not None:
        durable_ids.add(int(lease["draft_msg_id"]))
    # Parallel runs have no exclusive conversation lease, but still have
    # durable run rows. Resolve only children of this request's exact anchor.
    durable_ids.update(int(run["draft_msg_id"]) for run in await db.list_active_generations()
                       if run.get("conv_id") == conv_id)
    for draft_id in durable_ids:
        durable_draft = await db.get_message(draft_id)
        if (durable_draft and durable_draft.get("conversation_id") == conv_id
                and durable_draft.get("role") == "assistant"):
            if not any(str(snap.get("draft_msg_id")) == str(durable_draft["id"]) for snap in snapshots):
                snapshots.append({"draft_msg_id": durable_draft["id"],
                                  "parent_id": durable_draft.get("parent_id")})
    if explicit_draft_id is not None:
        # Preserve the exact queue parent even if its terminal event raced
        # this POST. The scheduler separately checks terminal success before
        # launching; disappearance from the live registry cannot make siblings.
        try:
            draft_id = int(explicit_draft_id)
        except (TypeError, ValueError):
            raise HTTPException(400, "queue_after_draft_id must be a message ID")
        draft = await db.get_message(draft_id)
        if not draft or draft.get("conversation_id") != conv_id or draft.get("role") != "assistant":
            raise HTTPException(400, "queue_after_draft_id is not an assistant draft in this conversation")
        if anchor_id is not None and str(anchor_id) not in {
            str(draft["id"]), str(draft.get("parent_id"))
        }:
            raise HTTPException(409, "The queued continuation target does not match this tab's branch anchor")
        return draft["id"]

    exact_drafts = [
        snap for snap in snapshots
        if snap.get("draft_msg_id") is not None
        and str(snap.get("draft_msg_id")) == str(anchor_id)
    ]
    if len(exact_drafts) == 1:
        return int(exact_drafts[0]["draft_msg_id"])

    child_drafts = [
        snap for snap in snapshots
        if snap.get("draft_msg_id") is not None
        and str(snap.get("parent_id")) == str(anchor_id)
    ]
    if len(child_drafts) > 1:
        raise HTTPException(
            409,
            "More than one generation is active below this branch anchor; select the exact draft to continue it",
        )
    if child_drafts:
        return int(child_drafts[0]["draft_msg_id"])
    if any(
        snap.get("draft_msg_id") is None
        and str(snap.get("parent_id")) == str(anchor_id)
        for snap in snapshots
    ):
        raise HTTPException(
            409,
            "The running reply has not created its durable draft yet; retry this continuation in a moment",
        )
    return None


def _find_client_websocket(conv_id: int, client_instance_id: str | None):
    if not client_instance_id:
        return None
    candidates = []
    for websocket in _active_websockets.get(conv_id, set()):
        if str(getattr(websocket, "_client_instance_id", "")) != str(client_instance_id):
            continue
        if any(getattr(websocket, field, WebSocketState.CONNECTED) != WebSocketState.CONNECTED
               for field in ("client_state", "application_state")):
            continue
        candidates.append(websocket)
    # Reconnect cleanup can overlap launch admission. Subscribe the current
    # connection, never an arbitrary old/closed member of the conversation set.
    return max(candidates, key=lambda ws: getattr(ws, "_loom_connected_at", 0), default=None)


async def _run_id_belongs_to_conversation(run_id: str | int | None, conv_id: int) -> bool:
    """Authorize a subscription against durable/in-memory run ownership."""
    normalized = canonical_run_id(run_id)
    if not normalized:
        return False
    if normalized.startswith("gen:"):
        try:
            gen_id = int(normalized.split(":", 1)[1])
        except (TypeError, ValueError):
            return False
        return any(
            key[0] == int(conv_id) and key[2] == gen_id and not task.done()
            for key, task in _active_generations.items()
        )
    if normalized.startswith("agent:"):
        run = await db.get_agent_run(normalized.split(":", 1)[1])
        return bool(run and int(run.get("conversation_id")) == int(conv_id))
    return False


class GenerationLaneOccupied(RuntimeError):
    """A non-parallel conversation already owns its durable provider lane."""


async def _create_generation_draft(
    conv_id: int,
    conv: dict,
    parent_id: int,
) -> dict:
    """Create one provider draft with auditable serial-lane authorization.

    Browser sockets, active-branch state, and in-memory task registries are
    deliberately irrelevant here.  In a non-parallel agent conversation the
    exact asyncio task must own the exact live SQLite lease, and the inserted
    message records that lease ID.  The database trigger rejects every other
    empty assistant-draft insert.
    """
    task = asyncio.current_task()
    gen_key = getattr(task, "_gen_key", None) if task else None
    lease_id = getattr(task, "_generation_lease_id", None) if task else None

    def _lease_rejected(detail: str) -> GenerationLaneOccupied:
        if task is not None:
            task._generation_terminal_override = "lease_rejected"
        return GenerationLaneOccupied(detail)

    serial_lane = bool(
        conv
        and conv.get("mode") in _AGENT_WORKSPACE_MODES
        and not _truthy_setting(conv.get("parallel_agents_enabled"))
    )
    if serial_lane and not lease_id:
        raise _lease_rejected(
            "A non-parallel provider draft cannot be created without its generation lease"
        )
    if lease_id:
        lease = await db.get_generation_lease(lease_id)
        expected_run_id = f"gen:{gen_key[2]}" if gen_key else None
        if not lease or not (
            int(lease.get("conv_id")) == int(conv_id)
            and lease.get("released_at") is None
            and lease.get("state") in {"launching", "running"}
            and (expected_run_id is None or str(lease.get("run_id")) == expected_run_id)
        ):
            raise _lease_rejected(
                "The generation lease no longer authorizes this provider draft"
            )
        if not await db.bind_generation_lease(
            lease_id,
            draft_parent_id=int(parent_id),
            state="launching",
        ):
            raise _lease_rejected(
                "The generation lease could not authorize this draft parent"
            )
    draft = await db.add_message(
        conv_id,
        "assistant",
        "",
        parent_id=parent_id,
        generation_lease_id=lease_id,
    )
    if lease_id and not await db.bind_generation_lease(
        lease_id,
        draft_parent_id=int(parent_id),
        draft_msg_id=int(draft["id"]),
        state="running",
    ):
        await db.delete_branch(draft["id"])
        raise _lease_rejected(
            "The generation lease ended before its provider draft was bound"
        )
    if task is not None:
        task._draft_msg_id = int(draft["id"])
    return draft


def _track_generation_provider(proc) -> None:
    """Retain owned subprocess handles even after adapter registries are cleared."""
    task = asyncio.current_task()
    if task is not None and proc is not None:
        handles = getattr(task, "_coordination_processes", None)
        if handles is None:
            handles = task._coordination_processes = []
        if not any(handle is proc for handle in handles):
            handles.append(proc)


def _generation_providers_exited(task=None) -> bool:
    task = task if task is not None else asyncio.current_task()
    # Adapter return is the drain boundary for HTTP-only providers. CLI adapters
    # additionally need acknowledgement from every process they launched (also
    # retries). Descendant-tree teardown remains an adapter integration contract.
    return all(getattr(proc, "returncode", None) is not None
               for proc in getattr(task, "_coordination_processes", ()))


async def _finish_deferred_generation_teardown(task, conv_id, draft_msg_id, lease_id, terminal_state):
    """Keep durable ownership until every adapter process acknowledges exit."""
    processes = getattr(task, "_coordination_processes", ())
    await asyncio.gather(*(proc.wait() for proc in processes))
    if not _generation_providers_exited(task):
        return
    if draft_msg_id is not None:
        await db.unregister_active_generation(draft_msg_id)
    if lease_id:
        await db.release_generation_lease(lease_id, terminal_state)
    await _send_generation_idle_if_quiescent(conv_id)


async def _generation_task_entry(
    websocket: WebSocket | None,
    conv_id: int,
    payload: dict,
    lease_id: str | None,
) -> None:
    asyncio.current_task()._coordination_entered = True
    terminal_state = "completed"
    try:
        if payload.get("action") == "compact":
            await _handle_compact(websocket, conv_id, payload)
        else:
            await _handle_generation_with_workspace_safety(websocket, conv_id, payload)
    except asyncio.CancelledError:
        terminal_state = _generation_stop_disposition()
        raise
    except Exception as exc:
        terminal_state = "error"
        task = asyncio.current_task()
        if payload.get("action") != "compact" and not getattr(task, "_draft_msg_id", None):
            # Admission already succeeded. Persist a terminal receipt even if
            # preparation failed before the adapter could create its draft.
            error_text = f"Turn failed before provider launch: {type(exc).__name__}: {exc}"
            try:
                conv = await db.get_conversation(conv_id)
                draft = await _create_generation_draft(conv_id, conv, payload["parent_id"])
                await db.update_message_content(
                    draft["id"], content=f"[Error: {error_text}]",
                    turn_lifecycle_state="failed",
                )
                gen_key = getattr(task, "_gen_key", None)
                if gen_key:
                    _update_gen_snapshot(gen_key, draft_msg_id=draft["id"])
                    await _advance_unambiguous_generation_cursors(gen_key)
                await _ws_send(conv_id, {
                    "type": "error", "error": error_text,
                    "parent_id": payload["parent_id"], "message_id": draft["id"],
                    "message": await db.get_message(draft["id"]),
                })
            except Exception as receipt_error:
                print(f"[GEN] Could not persist launch failure for conv={conv_id}: {receipt_error!r}")
                await _ws_send(conv_id, {
                    "type": "error", "error": error_text,
                    "parent_id": payload.get("parent_id"),
                })
        raise
    finally:
        task = asyncio.current_task()
        draft_msg_id = getattr(task, "_draft_msg_id", None) if task else None
        terminal_override = (
            getattr(task, "_generation_terminal_override", None) if task else None
        )
        if terminal_state == "completed" and terminal_override:
            terminal_state = str(terminal_override)
        # Several provider adapters turn their own terminal failure into a
        # durable assistant marker instead of re-raising it.  That is useful
        # for the tree, but it must never make a queued successor look like a
        # successful continuation. Derive the lease outcome from the final
        # persisted turn as well as the Python task outcome.
        if terminal_state == "completed" and draft_msg_id is not None:
            final_draft = await db.get_message(draft_msg_id)
            lifecycle = str(
                (final_draft or {}).get("turn_lifecycle_state") or ""
            ).lower()
            final_text = str((final_draft or {}).get("content") or "").lstrip()
            if lifecycle in {"cancelled", "interrupted", "failed", "partial_failed"}:
                terminal_state = lifecycle
            elif final_text.startswith("[Error:"):
                terminal_state = "error"
            elif final_text.startswith("[Turn interrupted"):
                terminal_state = "interrupted"

        exited = _generation_providers_exited(task)
        release_effect, queue_effect = coordination.finish_effects(
            terminal_state, exited=exited, leased=bool(lease_id)
        )
        if not exited:
            print(f"[GEN] Retaining lease {lease_id}: provider exit not acknowledged")
            _spawn_background_task(
                _finish_deferred_generation_teardown(
                    task, conv_id, draft_msg_id, lease_id, terminal_state
                ),
                f"generation teardown {conv_id}:{draft_msg_id}",
                allow_during_shutdown=True,
            )
        if (terminal_state != "completed" or not exited) and draft_msg_id is not None:
            paused = await db.pause_queued_generations(
                conv_id, wait_for_draft_id=int(draft_msg_id)
            )
            if paused:
                await _ws_send(
                    conv_id,
                    {
                        "type": "queue_changed",
                        "queued": True,
                        "status": "paused",
                        "wait_for_draft_id": int(draft_msg_id),
                    },
                )
        # Adapter cleanup is responsible for its subprocess tree; the reducer
        # additionally refuses release without owned-process exit acknowledgement.
        # Until release, SQLite's partial unique index keeps the lane closed.
        if release_effect == "release_lease":
            await db.release_generation_lease(lease_id, terminal_state)
        if exited and draft_msg_id is not None:
            await db.unregister_active_generation(draft_msg_id)
        # Queue dispatch happens only after the durable lease records a
        # successful predecessor.  Cancel, interrupt, provider error, crash,
        # and process teardown all leave dependent work visibly paused.
        if queue_effect == "launch_queued":
            _spawn_background_task(
                _dispatch_ready_queued_generations(conv_id),
                f"queue dispatch {conv_id}",
            )


async def _launch_generation_task(
    websocket: WebSocket | None,
    conv_id: int,
    data: dict,
    client_instance_id: str | None = None,
    *,
    preacquired_lease_id: str | None = None,
) -> tuple[tuple[int, int | None, int], asyncio.Task]:
    """Serialize launch admission against Loom shutdown."""
    async with _generation_admission_lock:
        if _shutdown_requested:
            if preacquired_lease_id:
                await db.release_generation_lease(
                    preacquired_lease_id, "shutdown"
                )
            raise HTTPException(
                status_code=503,
                detail="Loom is shutting down; this generation was not launched",
            )
        return await _launch_generation_task_admitted(
            websocket,
            conv_id,
            data,
            client_instance_id,
            preacquired_lease_id=preacquired_lease_id,
        )


async def _launch_generation_task_admitted(
    websocket: WebSocket | None,
    conv_id: int,
    data: dict,
    client_instance_id: str | None = None,
    *,
    preacquired_lease_id: str | None = None,
) -> tuple[tuple[int, int | None, int], asyncio.Task]:
    """Atomically reserve and create one generation task.

    For non-parallel agent conversations the SQLite lease is acquired before
    an asyncio task, assistant draft, workspace snapshot, or provider process
    can start. Every launch path calls this helper.
    """
    global _gen_seq
    parent_id = data.get("parent_id")
    _gen_seq += 1
    gen_key = (conv_id, parent_id, _gen_seq)
    conv = await db.get_conversation(conv_id)
    launch_mode = _mode_for_cc_model(data.get("cc_model")) or (
        conv.get("mode") if conv else None
    )
    if reason := model_registry.retirement_reason(
        (conv or {}).get("mode"), data.get("cc_model") or (conv or {}).get("cc_model")
    ):
        if preacquired_lease_id:
            await db.release_generation_lease(preacquired_lease_id, "error")
        raise HTTPException(410, reason)
    serial_lane = bool(
        conv
        and launch_mode in _AGENT_WORKSPACE_MODES
        and not _truthy_setting(conv.get("parallel_agents_enabled"))
    )
    lease_id = preacquired_lease_id
    if serial_lane and not lease_id:
        lease_id = uuid.uuid4().hex
        acquired = await db.try_acquire_generation_lease(
            conv_id,
            lease_id,
            run_id=f"gen:{_gen_seq}",
            user_msg_id=parent_id,
            draft_parent_id=parent_id,
            client_instance_id=client_instance_id,
        )
        if not acquired:
            raise GenerationLaneOccupied(
                f"Conversation {conv_id} already has a provider generation lease"
            )
    elif lease_id:
        bound = await db.bind_generation_lease(
            lease_id,
            run_id=f"gen:{_gen_seq}",
            user_msg_id=parent_id,
            draft_parent_id=parent_id,
            state="launching",
        )
        if not bound:
            raise GenerationLaneOccupied(
                f"Generation lease {lease_id} is no longer active"
            )
    payload = dict(data)
    payload["_gen_id"] = _gen_seq
    if client_instance_id:
        payload["client_instance_id"] = client_instance_id
    try:
        task = asyncio.create_task(
            _generation_task_entry(websocket, conv_id, payload, lease_id)
        )
    except Exception:
        if lease_id:
            await db.release_generation_lease(lease_id, "launch_failed")
        raise
    task._gen_key = gen_key
    task._client_instance_id = client_instance_id
    task._generation_lease_id = lease_id
    _active_generations[gen_key] = task
    if websocket is not None:
        _event_router.register(websocket, conv_id)
        _event_router.subscribe(websocket, f"gen:{gen_key[2]}")
    effective_cc_model = data.get("cc_model") or (conv.get("cc_model") if conv else None)
    _update_gen_snapshot(
        gen_key,
        parent_id=parent_id,
        client_instance_id=client_instance_id,
        loom_thread_id=data.get("loom_thread_id"),
        cc_model=effective_cc_model,
        harness=launch_mode,
        phase="scheduled",
        status_text="Preparing turn…",
        started_at=time.time(),
    )

    # Provider handlers normally remove their own registry/snapshot entries.
    # A cancellation during the pre-provider recovery snapshot never enters a
    # provider handler, so clean that early-exit path up here as well.
    def _forget_finished(done_task: asyncio.Task) -> None:
        if _active_generations.get(gen_key) is done_task:
            _active_generations.pop(gen_key, None)
            _generation_snapshots.pop(gen_key, None)
        if _generation_providers_exited(done_task):
            _spawn_background_task(
                _send_generation_idle_if_quiescent(conv_id),
                f"generation idle {conv_id}",
                allow_during_shutdown=True,
            )
        # A task cancelled before its coroutine first executes cannot reach
        # _generation_task_entry.finally. Never override a finalizer which
        # retained its lease because provider teardown was not acknowledged.
        if lease_id and not getattr(done_task, "_coordination_entered", False):
            try:
                _spawn_background_task(
                    db.release_generation_lease(
                        lease_id,
                        (
                            _generation_stop_disposition(done_task)
                            if done_task.cancelled() else "launch_failed"
                        ),
                    ),
                    "generation lease release",
                    allow_during_shutdown=True,
                )
            except RuntimeError:
                pass

    task.add_done_callback(_forget_finished)
    return gen_key, task


_queued_generation_dispatch_lock = asyncio.Lock()


async def _dispatch_ready_queued_generations(conv_id: int | None = None):
    """Launch durable continuations whose prerequisite draft has finished."""
    async with _queued_generation_dispatch_lock:
        queued = await db.list_queued_generations(conv_id, include_launching=False)
        for item in queued:
            item_conv_id = int(item["conv_id"])
            wait_for_draft_id = int(item["wait_for_draft_id"])

            # The prerequisite's durable lease is the authority for whether
            # this is a continuation after success or work that must wait for
            # human review.  Volatile task/PID registries can disappear on an
            # adapter EOF or restart and are never sufficient to jump a queue.
            barrier_lease = await db.get_generation_lease_for_draft(
                wait_for_draft_id
            )
            if barrier_lease:
                decision = coordination.queue_effect(
                    held=barrier_lease.get("released_at") is None,
                    outcome=str(barrier_lease.get("terminal_state") or ""),
                    paused=item.get("status") == "paused",
                    authorized=_truthy_setting(item.get("resume_authorized")),
                )
                if decision == "none":
                    continue
                if decision == "pause_queue":
                    paused = await db.pause_queued_generations(
                        item_conv_id, wait_for_draft_id=wait_for_draft_id
                    )
                    if paused:
                        await _ws_send(
                            item_conv_id,
                            {
                                "type": "queue_changed",
                                "queued": True,
                                "status": "paused",
                                "wait_for_draft_id": wait_for_draft_id,
                            },
                        )
                    continue
            else:
                # Compatibility for queues created before durable leases were
                # introduced.  A clearly stopped/failed predecessor is still
                # fail-closed; only an ordinary persisted answer may proceed.
                barrier = await db.get_message(wait_for_draft_id)
                lifecycle = str(
                    (barrier or {}).get("turn_lifecycle_state") or ""
                ).lower()
                barrier_text = str((barrier or {}).get("content") or "").lstrip()
                failed_legacy_barrier = (
                    lifecycle in {"cancelled", "interrupted", "failed", "partial_failed"}
                    or barrier_text.startswith("[Error:")
                    or barrier_text.startswith("[Turn interrupted")
                )
                if (
                    failed_legacy_barrier
                    and not _truthy_setting(item.get("resume_authorized"))
                ):
                    paused = await db.pause_queued_generations(
                        item_conv_id, wait_for_draft_id=wait_for_draft_id
                    )
                    if paused:
                        await _ws_send(
                            item_conv_id,
                            {
                                "type": "queue_changed",
                                "queued": True,
                                "status": "paused",
                                "wait_for_draft_id": wait_for_draft_id,
                            },
                        )
                    continue

            snapshots = _active_generation_snapshots_for_conv(item_conv_id)
            active_draft_ids = {
                str(snap.get("draft_msg_id"))
                for snap in snapshots
                if snap.get("draft_msg_id") is not None
            }
            if str(item["wait_for_draft_id"]) in active_draft_ids:
                continue

            # A non-parallel agent conversation still has a one-process lane.
            # If another branch became active first, keep this continuation in
            # the queue instead of cancelling or stealing that generation.
            conv = await db.get_conversation(item_conv_id)
            if reason := model_registry.retirement_reason(
                (conv or {}).get("mode"), (item.get("settings") or {}).get("cc_model") or (conv or {}).get("cc_model")
            ):
                await db.pause_queued_generations(item_conv_id, wait_for_draft_id=wait_for_draft_id)
                await _ws_send(item_conv_id, {"type": "queue_changed", "queued": True,
                    "status": "paused", "user_msg_id": item["user_msg_id"], "reason": reason})
                continue
            parallel_enabled = bool(
                conv and _truthy_setting(conv.get("parallel_agents_enabled"))
            )
            if not parallel_enabled and await _provider_lane_occupied(item_conv_id):
                continue
            if any(
                str(snap.get("parent_id")) == str(item["user_msg_id"])
                for snap in snapshots
            ):
                await db.delete_queued_generation(item["user_msg_id"])
                continue
            queue_lease_id = None
            serial_lane = bool(
                conv
                and conv.get("mode") in _AGENT_WORKSPACE_MODES
                and not parallel_enabled
            )
            if serial_lane:
                queue_lease_id = uuid.uuid4().hex
                acquired = await db.try_acquire_generation_lease(
                    item_conv_id,
                    queue_lease_id,
                    user_msg_id=int(item["user_msg_id"]),
                    draft_parent_id=int(item["user_msg_id"]),
                    client_instance_id=item.get("client_instance_id"),
                )
                if not acquired:
                    continue
            if not await db.claim_queued_generation(item["user_msg_id"]):
                if queue_lease_id:
                    await db.release_generation_lease(
                        queue_lease_id, "claim_lost"
                    )
                continue

            try:
                websocket = _find_client_websocket(
                    item_conv_id, item.get("client_instance_id")
                )
                settings = dict(item.get("settings") or {})
                settings.update(
                    {
                        "action": "generate",
                        "parent_id": item["user_msg_id"],
                        "client_instance_id": item.get("client_instance_id"),
                        "loom_thread_id": item.get("loom_thread_id"),
                    }
                )
                count = max(1, min(5, int(item.get("generation_count") or 1)))
                if not conv or conv.get("mode") != "weave":
                    count = 1
                launched = []
                for _ in range(count):
                    gen_key, _task = await _launch_generation_task(
                        websocket,
                        item_conv_id,
                        settings,
                        item.get("client_instance_id"),
                        preacquired_lease_id=queue_lease_id,
                    )
                    queue_lease_id = None
                    launched.append(gen_key[2])
                await db.delete_queued_generation(item["user_msg_id"])
                await _ws_send(
                    item_conv_id,
                    {
                        "type": "queue_started",
                        "user_msg_id": item["user_msg_id"],
                        "wait_for_draft_id": item["wait_for_draft_id"],
                        "gen_ids": launched,
                        "client_instance_id": item.get("client_instance_id"),
                    },
                )
            except Exception as exc:
                print(
                    f"[QUEUE] Failed to launch queued message {item['user_msg_id']}: {exc!r}"
                )
                if queue_lease_id:
                    await db.release_generation_lease(
                        queue_lease_id, "launch_failed"
                    )
                await db.release_queued_generation(item["user_msg_id"])


async def _continue_ws_chat(
    websocket: WebSocket,
    conv_id: int,
    websocket_client_instance_id: str | None,
):
    # Tell the client whether a generation is running — include live snapshot if available
    queued_items = await db.list_queued_generations(conv_id)
    page_cursor = (
        await db.get_client_loom_cursor(conv_id, websocket_client_instance_id)
        if websocket_client_instance_id else None
    )
    if page_cursor:
        queued_items = [
            item for item in queued_items
            if str(item.get("loom_thread_id") or "")
            == str(page_cursor.get("thread_id") or "")
        ]
    else:
        queued_items = []
    queue_event = LoomEventEnvelope.from_payload(
        {"type": "queue_state", "items": queued_items},
        conversation_id=conv_id,
        scope="conversation",
        sequence=_event_sequencer.next(None, conv_id),
    )
    await websocket.send_json(queue_event.as_dict())
    has_active = await _send_active_gen_state(websocket, conv_id)
    # Replay only this conversation's permissions and only for runs this
    # socket explicitly owns.  Never replay the process-wide pending map.
    await _send_pending_permissions_to_socket(websocket, conv_id)
    # Global notices carry identity only. Approval payloads and authority stay
    # on the owning page (or explicit Admin supervision), including Trusted.
    for notice in await db.get_permission_notices():
        if _pending_hook_permissions.get(notice["request_id"], {}).get("response") is None:
            await websocket.send_json({"type": "permission_notice", "scope": "global",
                                       **notice, "status": "pending"})

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            action = data.get("action")

            if action == "ping":
                # Reply so the client's heartbeat worker sees inbound activity
                # and doesn't force-reconnect (which would trigger loadMessages
                # and wipe any in-progress textarea edit).
                try:
                    pong = LoomEventEnvelope.from_payload(
                        {"type": "pong"}, conversation_id=conv_id,
                        scope="conversation",
                        sequence=_event_sequencer.next(None, conv_id),
                    )
                    await websocket.send_json(pong.as_dict())
                    await _probe_followed_generation(websocket, conv_id, data)
                    # The old page's disconnect can finish after initial replay.
                    # Retry undelivered prompts under the same exact-draft gate.
                    await _send_pending_permissions_to_socket(websocket, conv_id, only_new=True)
                except Exception:
                    pass
                continue

            if action == "request_snapshot":
                # Client noticed its streaming UI is detached/missing while a
                # generation is still running — re-emit current state so it can
                # rebuild without a full page refresh.
                try:
                    await _send_active_gen_state(websocket, conv_id)
                    await _probe_followed_generation(websocket, conv_id, data)
                    await _send_pending_permissions_to_socket(websocket, conv_id)
                except Exception as e:
                    print(f"[WS] request_snapshot failed for conv={conv_id}: {e!r}")
                continue

            if action in {"cancel", "interrupt"}:
                # New clients identify the visible generation. Legacy clients
                # omit both selectors and retain the old cancel-all behavior.
                cancelled_keys = _active_generation_keys_for_cancel(
                    conv_id,
                    gen_id=data.get("gen_id"),
                    draft_msg_id=data.get("draft_msg_id"),
                )
                if not cancelled_keys:
                    await _ws_reply(
                        websocket,
                        conv_id,
                        {
                            "type": "error",
                            "error": "The selected generation is no longer active",
                            "gen_id": data.get("gen_id"),
                            "draft_msg_id": data.get("draft_msg_id"),
                        },
                    )
                    continue
                cancelled_snapshots = {
                    key: dict(_generation_snapshots.get(key) or {})
                    for key in cancelled_keys
                }
                selected_draft_id = data.get("draft_msg_id")
                if selected_draft_id is None and len(cancelled_keys) == 1:
                    selected_draft_id = (
                        cancelled_snapshots.get(cancelled_keys[0]) or {}
                    ).get("draft_msg_id")
                await _pause_serial_queue_for_cancel(
                    conv_id,
                    draft_msg_id=(
                        int(selected_draft_id)
                        if selected_draft_id is not None else None
                    ),
                )
                cancelled_tasks = []
                cancelled_task_by_key = {}
                for key in cancelled_keys:
                    task = _active_generations.get(key)
                    if task:
                        _request_generation_stop(task, action)
                        cancelled_tasks.append(task)
                        cancelled_task_by_key[key] = task
                # Wake exact pending approvals as lifecycle cancellations.
                # A generation stop is not a user denial of each tool request.
                for key in cancelled_keys:
                    await _resolve_pending_permissions_for_scope(
                        conv_id,
                        f"gen:{key[2]}",
                        message=(
                            "Loom permission request was cancelled because the owning "
                            f"agent turn was {action}ed before a user decision."
                        ),
                    )
                # Keep this lane registered until provider teardown actually finishes.
                # Provider handlers remove their own snapshots only after exit.
                if cancelled_tasks:
                    try:
                        done, _pending = await asyncio.wait(
                            cancelled_tasks,
                            timeout=8.0,
                        )
                        if done:
                            await asyncio.gather(*done, return_exceptions=True)
                    except asyncio.TimeoutError:
                        pass  # still registered; queue remains blocked
                # Send one scoped event per stopped generation. The task's
                # own CancelledError handler may also emit this; the client
                # filters/idempotently clears only the stream it owns.
                for key in cancelled_keys:
                    task = _active_generations.get(key) or cancelled_task_by_key.get(key)
                    if task is not None and not task.done():
                        continue
                    if task is not None and getattr(task, "_stop_event_sent", False):
                        continue
                    snapshot = cancelled_snapshots.get(key) or {}
                    event_type = (
                        "interrupted" if action == "interrupt" else "cancelled"
                    )
                    await _ws_send(
                        conv_id,
                        {
                            "type": event_type,
                            "disposition": action,
                            "gen_id": key[2],
                            "parent_id": snapshot.get("parent_id", key[1]),
                            "draft_msg_id": snapshot.get("draft_msg_id"),
                            "client_instance_id": snapshot.get("client_instance_id"),
                        },
                    )
                continue

            if action == "subscribe_run":
                run_id = canonical_run_id(data.get("run_id") or data.get("gen_id"))
                allowed = bool(
                    run_id
                    and await _websocket_can_subscribe_run(websocket, conv_id, run_id)
                    and _event_router.subscribe(websocket, run_id)
                )
                subscription = LoomEventEnvelope.from_payload(
                    {"type": "run_subscription", "run_id": run_id,
                     "subscribed": allowed},
                    conversation_id=conv_id, run_id=run_id,
                    scope="conversation",
                    sequence=_event_sequencer.next(None, conv_id),
                )
                await websocket.send_json(subscription.as_dict())
                if allowed:
                    await _send_pending_permissions_to_socket(websocket, conv_id)
                continue

            if action == "unsubscribe_run":
                run_id = canonical_run_id(data.get("run_id") or data.get("gen_id"))
                removed = bool(run_id and _event_router.unsubscribe(websocket, run_id))
                subscription = LoomEventEnvelope.from_payload(
                    {"type": "run_subscription", "run_id": run_id,
                     "subscribed": False, "removed": removed},
                    conversation_id=conv_id, run_id=run_id,
                    scope="conversation",
                    sequence=_event_sequencer.next(None, conv_id),
                )
                await websocket.send_json(subscription.as_dict())
                continue

            if action == "permission_response":
                request_id = data.get("request_id", "")
                # Resolve the hook-based pending permission
                pending = _pending_hook_permissions.get(request_id)
                if pending and pending.get("response") is None:
                    if int(pending.get("conv_id") or conv_id) != conv_id:
                        continue
                    origin = str(pending.get("client_instance_id") or "")
                    page = str(getattr(websocket, "_client_instance_id", "") or "")
                    if origin and origin != page:
                        if _find_client_websocket(conv_id, origin) is not None or not await _permission_socket_can_recover(
                            websocket, conv_id, pending.get("run_id"), pending.get("draft_msg_id")
                        ):
                            continue
                    if str(pending.get("permission_scope") or "").startswith("agent:"):
                        if not await _child_permission_response_authorized(websocket, conv_id, pending):
                            continue
                        # Another observer may have answered during validation.
                        if pending.get("response") is not None:
                            continue
                    await _answer_pending_permission(request_id, data, websocket=websocket)
                continue

            if action == "compact":
                try:
                    await _launch_generation_task(
                        websocket, conv_id, data, websocket_client_instance_id)
                except (GenerationLaneOccupied, HTTPException) as exc:
                    await _ws_reply(websocket, conv_id, {
                        "type": "error", "error": getattr(exc, "detail", str(exc)),
                    })
                continue

            print(f"[WS] Received action={action} for conv={conv_id}")
            if action in ("generate", "regenerate"):
                parent_id = data.get("parent_id")
                client_instance_id = (
                    data.get("client_instance_id") or websocket_client_instance_id
                )
                if client_instance_id:
                    data["client_instance_id"] = client_instance_id

                # A generation is bound to the persisted message/thread it
                # extends. Browser focus, active flags, and socket origin are
                # never valid topology inputs.
                if parent_id is None:
                    await _ws_reply(
                        websocket,
                        conv_id,
                        {
                            "type": "error",
                            "error": "A generation requires an explicit persisted parent message",
                        },
                    )
                    continue
                generation_parent = await db.get_message(parent_id)
                if not generation_parent or int(
                    generation_parent.get("conversation_id") or 0
                ) != int(conv_id):
                    await _ws_reply(
                        websocket,
                        conv_id,
                        {
                            "type": "error",
                            "error": "The generation parent does not belong to this conversation",
                        },
                    )
                    continue
                generation_thread_id = generation_parent.get("loom_thread_id")
                if not generation_thread_id:
                    await _ws_reply(
                        websocket,
                        conv_id,
                        {
                            "type": "error",
                            "error": "The generation parent has no Loom thread identity",
                        },
                    )
                    continue
                requested_thread_id = data.get("loom_thread_id")
                if requested_thread_id and str(requested_thread_id) != str(
                    generation_thread_id
                ):
                    await _ws_reply(
                        websocket,
                        conv_id,
                        {
                            "type": "error",
                            "error": "The generation target changed threads before launch",
                        },
                    )
                    continue
                if client_instance_id:
                    launch_cursor = await db.get_client_loom_cursor(
                        conv_id, client_instance_id
                    )
                    if not launch_cursor or not (
                        str(launch_cursor.get("thread_id")) == str(generation_thread_id)
                        and str(launch_cursor.get("leaf_message_id")) == str(parent_id)
                    ):
                        await _ws_reply(
                            websocket,
                            conv_id,
                            {
                                "type": "error",
                                "error": "This tab moved before the generation launched; no run was started",
                            },
                        )
                        continue
                data["loom_thread_id"] = generation_thread_id

                # Check conversation mode to decide parallel policy
                conv = await db.get_conversation(conv_id)
                mode = conv.get("mode", "weave") if conv else "weave"
                # CLI-agent subprocess modes — Claude Code (claude), Braid (local,
                # = CC via llama-server), and Hermes (hermes acp). All three drive a
                # single subprocess per conversation, so parallel generations on
                # different branches would race the same child. (Including "local"
                # here fixes a latent Braid bug — it was previously treated like
                # Weave/OODA and allowed to spawn parallel CC subprocesses.)
                is_subprocess_agent = mode in (
                    "claude", "local",
                    "gemini", "codex", "goose", "umans", "openrouter",
                )
                parallel_same_checkout = bool(
                    conv and _truthy_setting(conv.get("parallel_agents_enabled"))
                )

                if parallel_same_checkout and action == "generate" and "parent_id" not in data:
                    await _ws_reply(
                        websocket,
                        conv_id,
                        {
                            "type": "error",
                            "error": "parent_id is required when parallel agents are enabled so Loom cannot launch on the wrong branch",
                        },
                    )
                    continue

                if is_subprocess_agent and not parallel_same_checkout:
                    # Only one agent generation per conversation
                    cc_busy = any(
                        k[0] == conv_id and not t.done()
                        for k, t in _active_generations.items()
                    )
                    if cc_busy:
                        # A socket is transport, not run ownership. Use the
                        # immutable launch client id for duplicate suppression;
                        # reconnecting or focusing another socket cannot adopt
                        # or cancel the run.
                        old_key = next(
                            k
                            for k, t in _active_generations.items()
                            if k[0] == conv_id and not t.done()
                        )
                        old_task = _active_generations[old_key]
                        same_launch_client = bool(
                            client_instance_id
                            and str(getattr(old_task, "_client_instance_id", ""))
                            == str(client_instance_id)
                        )
                        if same_launch_client:
                            if action == "generate" and (
                                parent_id is None or parent_id == old_key[1]
                            ):
                                await _send_active_gen_state(websocket, conv_id)
                                continue
                        # A send/regenerate request is never an implicit cancel.
                        # Releasing the task entry before provider teardown let a
                        # second Codex process enter a non-parallel lane while the
                        # first process was still editing the checkout.  Only the
                        # explicit cancel action may stop a run, and its handler
                        # keeps the lane occupied until provider cleanup finishes.
                        await _ws_reply(
                            websocket,
                            conv_id,
                            {
                                "type": "error",
                                "error": (
                                    "An agent generation is already running in this "
                                    "conversation. Queue a continuation or cancel the "
                                    "active run explicitly before launching another."
                                ),
                            },
                        )
                        continue
                elif action == "regenerate":
                    # Regenerate: cancel any existing gen on same parent
                    for k in [
                        k
                        for k in _active_generations
                        if k[0] == conv_id
                        and k[1] == parent_id
                        and not _active_generations[k].done()
                    ]:
                        _active_generations.pop(k).cancel()

                # Weave/OODA generate: allow parallel, even on same parent.
                # Durable queued continuations enter through this same helper.
                generation_count = max(
                    1, min(5, int(data.get("generation_count") or 1))
                )
                if mode != "weave":
                    generation_count = 1
                for _ in range(generation_count):
                    try:
                        await _launch_generation_task(
                            websocket, conv_id, data, client_instance_id
                        )
                    except GenerationLaneOccupied:
                        await _ws_reply(
                            websocket,
                            conv_id,
                            {
                                "type": "error",
                                "error": (
                                    "This conversation's provider lane is already "
                                    "occupied; the second launch was not started."
                                ),
                            },
                        )
                        break

    except WebSocketDisconnect:
        print(f"[WS] Client disconnected conv={conv_id}")
        # Remove this websocket but do NOT cancel active generation — let it finish and save
        clients = _active_websockets.get(conv_id)
        if clients:
            clients.discard(websocket)
            _event_router.unregister(websocket)
            if not clients:
                _active_websockets.pop(conv_id, None)
    except Exception as e:
        _event_router.unregister(websocket)
        print(f"[WS] Error conv={conv_id}: {e}")
        if websocket.client_state == WebSocketState.CONNECTED:
            await _ws_reply(websocket, conv_id, {"type": "error", "error": str(e)})


async def _queued_generation_scheduler_loop():
    """Recovery loop for reconnects, races, and server-restored queues."""
    while True:
        try:
            await _dispatch_ready_queued_generations()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[QUEUE] Scheduler pass failed: {exc!r}")
        await asyncio.sleep(1)


def _model_family(model_id: str) -> str | None:
    """Return a Claude family name for a model id or alias."""
    if not model_id:
        return None
    m = model_id.lower()
    for fam in ("fable", "opus", "sonnet", "haiku"):
        if fam in m:
            return fam
    return None


def _detect_model_fallback(requested: str, actual: str) -> str | None:
    """If CC silently downgraded to a different family, return a warning line."""
    rf = _model_family(requested)
    af = _model_family(actual)
    if rf and af and rf != af:
        return f"Claude Code fell back from {rf} to {af} (usage cap on {rf})"
    return None


def _format_rate_limit_note(data: dict) -> str:
    """Build a user-facing note from CC's rate_limit_event payload.

    CC emits rate_limit_event for proactive warnings (e.g. approaching a cap).
    Tries common field shapes; falls back to a raw JSON dump so we learn the
    schema if it changes.
    """
    if not data:
        return ""
    
    # Flatten rate_limit_info keys if present
    info = data.get("rate_limit_info") or {}
    if not isinstance(info, dict):
        info = {}
        
    parts = []
    
    # Check limit type/window
    limit_type = info.get("rateLimitType") or data.get("limit_type") or data.get("window") or data.get("scope")
    if limit_type:
        parts.append(f"limit_type={limit_type}")
        
    # Check status
    status = info.get("status") or data.get("status")
    if status:
        parts.append(f"status={status}")

    # Check reset time/delay
    resets_at = info.get("resetsAt") or data.get("resets_at") or data.get("reset_at") or data.get("reset")
    if resets_at:
        try:
            import time
            resets_str = time.ctime(float(resets_at))
            parts.append(f"resets_at={resets_str}")
        except Exception:
            parts.append(f"resets_at={resets_at}")
            
    retry_after = data.get("retry_after") or data.get("retry_after_seconds") or data.get("retry_delay_ms")
    if retry_after:
        parts.append(f"retry_after={retry_after}")

    # Overage information
    overage_status = info.get("overageStatus")
    if overage_status:
        parts.append(f"overage_status={overage_status}")
    overage_reason = info.get("overageDisabledReason")
    if overage_reason:
        parts.append(f"overage_reason={overage_reason}")

    if not parts:
        parts.append("raw=" + json.dumps(data, default=str)[:300])
        
    return "\n\n[Loom note: rate-limit details from CC — " + "; ".join(parts) + "]"


def _format_synthetic_error_note(syn: dict) -> str:
    """Note for CC-fabricated error messages (model: '<synthetic>').

    CC writes these client-side after a 4xx from the API. The wording often
    mislabels the rate-limit window — e.g. saying "monthly" for an hourly
    trip — so flag that the text above is generated by CC, not the API.
    """
    err = syn.get("error", "") or "unknown"
    status = syn.get("status")
    suffix = (
        " — wording is fabricated by CC and may mislabel the rate-limit window "
        "(e.g. 'monthly' shown for an hourly cap). Check the Anthropic Console "
        "for actual reset times."
        if err == "rate_limit"
        else ""
    )
    return (
        f"\n\n[Loom note: CC-synthesized error — type={err}"
        f"{f', status={status}' if status else ''}{suffix}]"
    )


def _cc_result_error_detail(result_info: dict) -> str:
    """Return the best human-readable CC error detail without treating it as output."""
    if not isinstance(result_info, dict):
        return ""
    # A successful result's prose is never an error message, even if an
    # earlier tool/event failed. Otherwise the whole answer is duplicated
    # into a synthetic interruption note and poisons session recovery.
    keys = ("error", "result_text") if result_info.get("is_error") else ("error",)
    for key in keys:
        value = result_info.get(key)
        if isinstance(value, dict):
            value = value.get("message") or json.dumps(value)
        elif isinstance(value, list):
            value = "\n".join(str(item) for item in value)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _failed_session_attestation(attestation, result, session_id, mode, error):
    """Keep failure provenance separate from harness/model verification."""
    record = dict(attestation or {})
    result = result or {}
    record["session_recovery"] = recovery_evidence(
        session_id, mode, error,
        terminal_confirmed=(
            result.get("terminal_confirmed") is True
            and bool(session_id)
            and result.get("session_id") == session_id
        ),
        error_code=result.get("error_code"),
    )
    return record


async def _recheck_completed_codex_boundary(branch, decision, model, project_dir):
    """Recover a falsely failed *nearest* boundary using native status evidence.

    No source row is rewritten, no older session is searched for, and neither
    the browser nor assistant prose can certify completion. A newer successful
    native turn must belong to this immutable snapshot and postdate the draft.
    """
    if decision.reason != "assistant_failed":
        return decision
    boundary = next((m for m in branch if m.get("id") == decision.message_id), {})
    if boundary.get("cc_session_mode") != "codex" or not boundary.get("cc_session_id"):
        return decision
    try:
        attestation = boundary.get("model_attestation") or {}
        if isinstance(attestation, str):
            attestation = json.loads(attestation)
        evidence = attestation.get("session_recovery") or {}
        session_id = boundary["cc_session_id"]
        created = float(boundary.get("created_at") or 0)
        if (attestation.get("status") != "verified" or evidence.get("terminal_confirmed") is not True
                or evidence.get("session_id") != session_id or evidence.get("session_mode") != "codex"
                or created <= 0):
            return decision
        native = await codex_client.read_codex_last_turn(session_id, project_dir)
        if (not native or not native.get("id") or native.get("status") != "completed" or native.get("error")
                or float(native.get("completedAt") or 0) < created):
            return decision
        if (float(native.get("startedAt") or 0) < int(created)
                or float(native.get("completedAt") or 0) < float(native.get("startedAt") or 0)):
            return decision
        candidate = select_resume_session([{**boundary, "turn_lifecycle_state": "completed"}],
                                          "codex", target_model=model)
        if candidate.can_resume:
            return ResumeDecision(candidate.session_id, "verified_completed_recovery", decision.message_id)
    except (TypeError, ValueError, AttributeError):
        pass
    return decision


async def _handoff_payload_budget(branch, model, harness, *, backend=None):
    # An early compaction decision; adapters recheck their final rendered
    # payload after wrappers/attachments. Never call this for native resumes.
    from replay_budget import assess_payload
    from loom_agent_prompt import load_loom_agent_prompt
    budget = await assess_payload(
        _build_claude_history_prompt(branch, None), model=model, harness=harness,
        instructions=load_loom_agent_prompt(), backend=backend,
        image_count=sum(len(_parse_image_paths(m.get("image_path"))) for m in branch if m.get("image_path")),
    )
    # A guessed remote limit must not trigger lossy source compaction. Codex
    # can expose a larger configured window when its app-server initializes;
    # the adapter's final admission check remains authoritative for this run.
    return None if budget.context_source.startswith("conservative") else budget


def _cc_has_streamed_output(full_text: str, content_blocks: list[dict]) -> bool:
    """True if CC streamed anything user-visible before a terminal event."""
    if (full_text or "").strip():
        return True
    for block in content_blocks or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            if (block.get("text") or "").strip():
                return True
        elif btype == "thinking":
            if (block.get("text") or "").strip():
                return True
        elif btype == "tool_use":
            # Even an unfinished tool block is useful partial work.
            return True
        elif btype:
            return True
    return False


def _cc_should_adopt_result_text(evt: dict) -> bool:
    """Use CC result text only as a non-error fallback when no deltas arrived."""
    return bool(evt.get("result_text")) and not evt.get("is_error")


def _append_cc_text_block(content_blocks: list[dict], text: str) -> None:
    if not text:
        return
    if content_blocks and content_blocks[-1].get("type") == "text":
        content_blocks[-1]["text"] = (content_blocks[-1].get("text") or "").rstrip() + text
    else:
        content_blocks.append({"type": "text", "text": text})


def _append_cc_interrupt_note(
    full_text: str,
    content_blocks: list[dict],
    result_info: dict,
) -> str:
    """Append an interruption note while preserving streamed tool/thinking blocks."""
    detail = _cc_result_error_detail(result_info) or "agent exited unexpectedly"
    if full_text:
        note = f"\n\n---\n[Turn interrupted: {detail}]"
        updated = full_text.rstrip() + note
    else:
        n_tools = sum(
            1
            for block in content_blocks or []
            if isinstance(block, dict) and block.get("type") == "tool_use"
        )
        if n_tools:
            note = (
                f"\n\n---\n[Turn interrupted: agent stopped after {n_tools} tool call(s) "
                f"without producing a final response - {detail}]"
            )
        else:
            note = (
                "\n\n---\n[Turn interrupted before producing a final response - "
                f"{detail}]"
            )
        updated = note.strip()
    if full_text and not content_blocks:
        content_blocks.append({"type": "text", "text": updated})
    else:
        _append_cc_text_block(content_blocks, note)
    return updated


def _append_provider_failure_note(
    full_text: str,
    content_blocks: list[dict],
    provider: str,
    detail: str,
) -> str:
    """Preserve useful partial work while making terminal failure explicit."""
    provider_label = str(provider or "Provider").strip()
    error_detail = str(detail or "provider exited unexpectedly").strip()
    note = f"\n\n---\n[{provider_label} turn failed: {error_detail}]"
    if full_text:
        updated = full_text.rstrip() + note
    else:
        n_tools = sum(
            1
            for block in content_blocks or []
            if isinstance(block, dict) and block.get("type") == "tool_use"
        )
        if n_tools:
            note = (
                f"\n\n---\n[{provider_label} turn failed after {n_tools} tool call(s) "
                f"without a final response: {error_detail}]"
            )
        updated = note.strip()
    _append_cc_text_block(content_blocks, note if full_text or content_blocks else updated)
    return updated


async def _patch_marker_with_summary(conv_id: int, marker_id: int, new_session: str):
    """Background task: once CC has flushed the post-compact transcript,
    read the summary out of the JSONL and UPDATE the marker row content.
    Emits `compact_summary_ready` so the UI can swap in the narrative text."""
    try:
        summary = await claude_client.read_compact_summary(new_session)
        if not summary:
            return
        _dbh = await db.get_db()
        # Prepend the existing header so we keep the token count line.
        row = await _dbh.execute_fetchall(
            "SELECT content FROM messages WHERE id = ?", (marker_id,)
        )
        header = ""
        if row:
            header = (dict(row[0]).get("content") or "").split("\n", 1)[0]
        new_content = (header + "\n\n---\nPreviously:\n" + summary).strip()
        await _dbh.execute(
            "UPDATE messages SET content = ? WHERE id = ?",
            (new_content, marker_id),
        )
        await _dbh.commit()
        # The summary itself is durable message content. Never broadcast it to
        # every window in the conversation; send metadata only so a window
        # already rendering this marker can refetch its exact cursor branch.
        await _ws_send(
            conv_id,
            {"type": "branch_content_changed", "message_id": marker_id},
        )
    except Exception as e:
        print(f"[Handoff] Failed to patch marker {marker_id} with summary: {e}")


def _compactable_anthropic_source(
    branch: list[dict],
) -> tuple[ResumeDecision, dict | None]:
    """Return the nearest assistant only when it is a resumable Claude turn.

    Automatic handoff compaction is a Claude-native operation. In particular,
    a long Codex or Goose branch can continue in its own native session without
    replaying Loom history, so its cumulative branch size is irrelevant here.
    """
    decision = select_resume_session(branch, "claude")
    source_message = next(
        (msg for msg in branch if msg.get("id") == decision.message_id),
        None,
    )
    source_model = (source_message or {}).get("cc_model_used") or ""
    if not decision.can_resume or not model_context.is_anthropic(source_model):
        return decision, None
    return decision, source_message


async def _run_compact_handoff(
    conv_id: int,
    conv: dict,
    parent_leaf_id: int,
    target_model: str,
    cc_effort: str,
    project_dir: str,
) -> tuple[int | None, str | None, int | None]:
    """Execute a compact-and-handoff before switching to a non-1M target.

    Runs CC `/compact` on the most recent 1M-capable session, forks a new
    branch rooted at a compact marker under `parent_leaf_id`, and returns
    `(marker_id, new_session_id, new_parent_id)`. `new_parent_id` is the
    msg id the caller should use as `parent_id` for the pending generation —
    either the marker itself, or (when `parent_leaf_id` was a user message we
    re-parented under the marker) the unchanged user message id.
    Returns (None, None, None) on failure.
    """
    # Compact only the nearest assistant boundary. Searching farther back for
    # an older Claude session after another harness has spoken would create a
    # summary that silently omits the intervening turn.
    branch = await db.get_branch_to_root(parent_leaf_id)
    resume_decision, source_message = _compactable_anthropic_source(branch)
    prev_model = (source_message or {}).get("cc_model_used") or ""
    if source_message is None:
        await _ws_send(
            conv_id,
            {
                "type": "status",
                "text": (
                    "Handoff skipped — nearest assistant boundary is not a "
                    f"compactable Anthropic session ({resume_decision.reason})"
                ),
            },
        )
        return None, None, None
    session_id = resume_decision.session_id
    compact_model = prev_model

    await _ws_send(
        conv_id,
        {"type": "status", "text": f"Compacting for handoff to {target_model}..."},
    )

    server_port = int(os.environ.get("LOOM_PORT", 3000))
    try:
        proc, event_stream = await claude_client.run_claude(
            prompt="/compact",
            cwd=project_dir,
            conv_id=conv_id,
            server_port=server_port,
            model=compact_model,
            requested_model=compact_model,
            effort=cc_effort,
            resume_session_id=session_id,
            fork_session=False,
            use_llama=False,
        )
    except Exception as e:
        await _ws_send(conv_id, {"type": "status", "text": f"Handoff launch failed: {e}"})
        return None, None, None

    # If the caller's parent is a user message (the turn we're about to
    # generate for), we re-parent it UNDER the marker so the replay-history
    # path keeps that user message visible after truncating at the marker.
    parent_row = None
    _dbh = await db.get_db()
    _prows = await _dbh.execute_fetchall(
        "SELECT id, role, parent_id FROM messages WHERE id = ?", (parent_leaf_id,)
    )
    if _prows:
        parent_row = dict(_prows[0])
    reparent_user = bool(parent_row and parent_row.get("role") == "user")
    marker_parent = parent_row.get("parent_id") if reparent_user else parent_leaf_id

    marker_id = None
    new_session = None
    pre_tokens = None
    async for evt in event_stream:
        etype = evt.get("type", "")
        if etype == "compact_boundary":
            pre_tokens = evt.get("pre_tokens")
            token_info = f" — {pre_tokens:,} tokens before" if pre_tokens else ""
            content = f"[CC context compactified (handoff → {target_model}){token_info}]"
            new_session = evt.get("session_id", "") or ""
            if new_session:
                await db.update_conversation_fields(
                    conv_id, claude_session_id=new_session
                )
            marker = await db.add_message(
                conv_id, "system",
                content,
                parent_id=marker_parent,
                is_active=True,
                cc_session_id=new_session or None,
            )
            marker_id = marker["id"]
            if reparent_user:
                # Slot the marker between the user message and its old parent.
                await _dbh.execute(
                    "UPDATE messages SET parent_id = ? WHERE id = ?",
                    (marker_id, parent_leaf_id),
                )
                await _dbh.commit()
                await db.set_active_branch(conv_id, parent_leaf_id)
            else:
                await db.set_active_branch(conv_id, marker_id)
            await _ws_send(
                conv_id,
                {
                    "type": "compact_boundary",
                    "trigger": "handoff",
                    "pre_tokens": pre_tokens,
                    "marker_id": marker_id,
                    "target_model": target_model,
                },
            )
        elif etype == "result":
            rs = evt.get("session_id", "")
            if rs and not new_session:
                new_session = rs
                await db.update_conversation_fields(
                    conv_id, claude_session_id=rs
                )

    if marker_id and new_session:
        _spawn_background_task(
            _patch_marker_with_summary(conv_id, marker_id, new_session),
            f"compact marker summary {marker_id}",
        )
        await _ws_send(
            conv_id,
            {"type": "status", "text": f"Handoff complete — continuing on {target_model}"},
        )
    elif not marker_id:
        await _ws_send(
            conv_id,
            {"type": "status", "text": "Handoff did not produce a compact marker — continuing anyway"},
        )
    new_parent_id = parent_leaf_id if reparent_user else marker_id
    return marker_id, new_session, new_parent_id


async def _handle_compact(websocket: WebSocket, conv_id: int, data: dict):
    """Create a shared checkpoint, without mutating a native session or cursor.

    Called through generation admission so shutdown/cancel/serial ownership are
    identical to other agent work. Publication rechecks the requesting cursor.
    """
    parent_id = data.get("parent_id")
    thread_id = str(data.get("loom_thread_id") or "")
    envelope = {"parent_id": parent_id, "loom_thread_id": thread_id, "checkpoint_only": True}
    success = False

    async def reply(event):
        if websocket is not None:
            try:
                await _ws_reply(websocket, conv_id, {**event, **envelope})
                return
            except (WebSocketDisconnect, RuntimeError, OSError):
                pass  # A browser disconnect cannot abort durable preparation.
        await _ws_send(conv_id, {**event, **envelope})

    try:
        conv = await db.get_conversation(conv_id)
        if not conv or conv.get("mode") not in _AGENT_WORKSPACE_MODES:
            raise ValueError("Manual checkpoints require an agent conversation; Weave is unchanged")
        if conv.get('backstage_parent_id'):
            raise ValueError("Specialized operator compaction is not part of this checkpoint operation")
        client_id = str(data.get("client_instance_id") or "")
        cursor = await db.get_client_loom_cursor(conv_id, client_id) if client_id else None
        parent = await db.get_message(parent_id) if parent_id is not None else None
        if not parent or parent.get("conversation_id") != conv_id:
            raise ValueError("The compaction parent is not in this conversation")
        if not cursor or not (
            cursor["thread_id"] == thread_id == parent.get("loom_thread_id")
            and cursor["leaf_message_id"] == parent_id
            and cursor["revision"] == data.get("cursor_revision")
        ):
            raise ValueError("This tab moved before compaction started; retry from the selected branch")
        # Never summarize a currently streaming/unfinished draft. A parallel
        # branch may proceed independently because activation changes no source.
        if parent.get("role") == "assistant" and (
            not str(parent.get("content") or "").strip()
            or parent.get("turn_lifecycle_state") in {"running", "preparing", "streaming"}
        ):
            raise ValueError("Wait for or interrupt the selected turn before compacting")
        from context_runtime import manual_checkpoint

        async def status(text):
            await reply({"type": "status", "text": text})

        branch = await db.get_branch_to_root(parent_id)
        branch_ids = {message["id"] for message in branch}
        if any(row.get("draft_msg_id") in branch_ids for row in await db.list_active_generations()):
            raise ValueError("The selected branch still has an active provider; interrupt or wait before compacting")
        await status("Preparing a shared branch checkpoint...")
        checkpoint = await manual_checkpoint(branch, cursor, status=status, focus=data.get("focus") or "")
        success = True
        await status(f"Checkpoint saved for {len(checkpoint.covered_ids)} messages; next turn will use it")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        asyncio.current_task()._generation_terminal_override = "error"
        # Network exceptions may contain URLs or payload details. Keep those out
        # of browser/log output; known validation errors are deliberately safe.
        detail = str(exc) if isinstance(exc, ValueError) else (
            "Configured local checkpoint backend unavailable or failed; history and native sessions are unchanged"
        )
        await reply({"type": "error", "error": detail})
    finally:
        await reply({"type": "compact_done", "success": success})


def is_codex_model(model: str) -> bool:
    return model_registry.resolve(model).get("mode") == "codex"


def is_gemini_model(model: str) -> bool:
    return model_registry.resolve(model).get("mode") == "gemini"


def is_umans_model(model: str) -> bool:
    """Check if model ID is an Umans AI model (e.g. umans-coder, umans-flash)."""
    return model_registry.resolve(model).get("mode") == "umans"


def is_goose_model(model: str) -> bool:
    return goose_client.is_goose_model(model)


def _umans_disabled_detail() -> str:
    return (
        "Umans AI models are deprecated and disabled in Loom. "
        "Use the OpenRouter GLM/Kimi entries, or enable the legacy Umans "
        "compatibility switch in Settings."
    )


def _reject_disabled_umans_for_new_selection(model: str | None, *, existing_model: str | None = None):
    if not model or config.enable_umans_models or not is_umans_model(model):
        return
    if existing_model and is_umans_model(existing_model):
        return
    raise HTTPException(status_code=400, detail=_umans_disabled_detail())


def is_dream_claude_model(model: str) -> bool:
    """True for the explicit Claude Code -> Dream shim selector value."""
    if not model:
        return False
    ml = model.lower()
    if ml.startswith("dream:"):
        return True
    dream_model = (getattr(config, "dream_model", "") or "").lower()
    return bool(dream_model and ml == dream_model and "diffusiongemma" in ml)


def _dream_claude_model_id(model: str) -> str:
    if model and model.lower().startswith("dream:"):
        return model.split(":", 1)[1] or (getattr(config, "dream_model", "") or "diffusiongemma")
    return model or (getattr(config, "dream_model", "") or "diffusiongemma")


def _goose_selector_for_model(model: str | None) -> str | None:
    """Return a Goose-routed selector for models Goose can host directly."""
    raw = (model or "").strip()
    if not raw:
        return None
    if goose_client.is_goose_model(raw):
        return raw
    if openrouter_client.is_openrouter_model(raw):
        return f"goose:openrouter:{openrouter_client.model_slug(raw)}"
    if raw.lower().endswith(".gguf"):
        return f"goose:llama:{raw}"
    if is_dream_claude_model(raw):
        return f"goose:dream:{_dream_claude_model_id(raw)}"
    return None


def _dream_shim_url() -> str:
    port = os.getenv("DREAM_SHIM_PORT", "8788")
    base = os.getenv("DREAM_SHIM_BASE_URL", f"http://127.0.0.1:{port}")
    if not base.startswith(("http://", "https://")):
        base = f"http://{base}"
    return base.replace("//localhost", "//127.0.0.1").rstrip("/")


async def _ensure_dream_shim_running() -> tuple[bool, str]:
    """Start the Anthropic->Dream shim on demand.

    This is intentionally scoped to Dream-Claude launches. It does not affect
    Dream Space, Weave, Hermes, or the normal llama-server backend.
    """
    global _dream_shim_proc
    url = _dream_shim_url()
    try:
        async with httpx.AsyncClient(timeout=0.75) as client:
            r = await client.get(f"{url}/health")
            if r.status_code == 200:
                return True, url
    except Exception:
        pass

    script = Path(__file__).parent / "anthropic_dream_router.py"
    if not script.is_file():
        return False, f"Dream shim script not found: {script}"

    if _dream_shim_proc and _dream_shim_proc.poll() is None:
        return True, url

    try:
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        log_path = Path(__file__).parent / "dream_shim.log"
        log = open(log_path, "a", encoding="utf-8")
        _dream_shim_proc = subprocess.Popen(
            [sys.executable, str(script)],
            cwd=str(Path(__file__).parent),
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )
    except Exception as e:
        return False, f"Failed to start Dream shim: {e}"

    for _ in range(30):
        await asyncio.sleep(0.2)
        try:
            async with httpx.AsyncClient(timeout=0.5) as client:
                r = await client.get(f"{url}/health")
                if r.status_code == 200:
                    return True, url
        except Exception:
            pass
    return False, f"Dream shim started but did not answer at {url}/health"


# parity". "claude" covers the whole claude_client launch family (incl.
# local llama, which reuses the same CLI flags and tool stripping); "umans"
# is its own entry because it is a distinct (if trivial) port — umans models
# also launch through claude_client but are tracked separately so the parity
# matrix stays an explicit signpost for future debuggers.




def models_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    def norm(s: str) -> str:
        s = s.lower()
        if s.endswith(".gguf"):
            s = s[:-5]
        return "".join(c for c in s if c.isalnum())
    na = norm(a)
    nb = norm(b)
    return na == nb or na in nb or nb in na


_MODEL_ATTESTATION_SOURCE_DEFAULTS = {
    "codex_app_server_thread_response": ("Codex app-server", "harness"),
    "claude_code_system_init": ("Claude Code", "harness"),
    "anthropic_compatible_message_start": ("Claude Code", "provider_response"),
    "agy_cli_launch_arguments": ("Antigravity (agy)", "launch"),
    "antigravity_stream_init": ("Antigravity (agy)", "harness"),
    "goose_process_environment": ("Goose ACP", "launch"),
    "goose_acp_session_model_state": ("Goose ACP", "harness"),
    "hermes_acp_session_model_state": ("Hermes ACP", "harness"),
    "hermes_acp_set_model_ack": ("Hermes ACP", "harness"),
    "openrouter_request_accepted": ("OpenRouter API", "launch"),
    "openrouter_request_configuration": ("OpenRouter API", "launch"),
    "openrouter_stream_chunk": ("OpenRouter API", "provider_response"),
    "openrouter_completion_response": ("OpenRouter API", "provider_response"),
    "dream_request_configuration": ("Dream", "launch"),
    "llama_server_request_configuration": ("Llama Server", "launch"),
}


def _normalize_turn_model_attestation(attestation: dict) -> dict:
    """Fill schema fields omitted by attestations from older live adapters."""
    normalized = dict(attestation)
    defaults = _MODEL_ATTESTATION_SOURCE_DEFAULTS.get(normalized.get("source"))
    if defaults:
        if not normalized.get("harness"):
            normalized["harness"] = defaults[0]
        if not normalized.get("verification_level"):
            normalized["verification_level"] = defaults[1]
    if not normalized.get("harness"):
        normalized["harness"] = "Unknown harness"
    if not normalized.get("verification_level"):
        normalized["verification_level"] = (
            "harness" if normalized.get("status") == "verified" else "selection"
        )
    return normalized


def _attach_preparation_provenance(attestation: dict, task=None) -> dict:
    """Attach every preparatory inference to the visible turn identity.

    A provider attestation proves only the user-facing generation.  It must not
    turn the whole turn green while a hidden preparation call used a different
    or unverified model.  Accepted preparation calls already contain the
    destination route and provider-attested model; preserve that chain here and
    fail the aggregate status closed when it disagrees with the selected model.
    """
    candidate = dict(attestation)
    task = task or asyncio.current_task()
    trace = getattr(task, "_context_trace", None) if task else None
    raw_calls = (
        trace.get("preparation_attempts") or trace.get("preparation_calls")
        if isinstance(trace, dict) else None
    )
    if not isinstance(raw_calls, list) or not raw_calls:
        return candidate

    selected = (
        candidate.get("requested_model")
        or candidate.get("launch_model")
        or candidate.get("effective_model")
    )
    preparation = []
    chain_verified = True
    for index, raw in enumerate(raw_calls, 1):
        if not isinstance(raw, dict):
            chain_verified = False
            continue
        route = raw.get("route") if isinstance(raw.get("route"), dict) else {}
        route_model = route.get("model")
        attested_model = raw.get("attested_model")
        accepted = raw.get("accepted") is True
        matches = bool(
            selected and route_model and attested_model
            and models_match(str(selected), str(route_model))
            and models_match(str(route_model), str(attested_model))
        )
        chain_verified = chain_verified and accepted and matches
        preparation.append({
            "phase": "context_preparation",
            "call": index,
            "harness": route.get("harness"),
            "requested_model": route_model,
            "effective_model": attested_model,
            "backend": route.get("backend"),
            "status": "verified" if accepted and matches else "unverified",
        })

    candidate["preparation"] = {
        "call_count": len(raw_calls),
        "operation_id": trace.get("operation_id") if isinstance(trace, dict) else None,
        "status": "verified" if chain_verified and len(preparation) == len(raw_calls) else "unverified",
        "calls": preparation,
    }
    if not chain_verified or len(preparation) != len(raw_calls):
        candidate["status"] = "unverified"
        candidate["identity_warning"] = (
            "One or more context-preparation calls were not verified as the selected destination model"
        )
    return candidate


async def _record_turn_model_attestation(
    conv_id: int,
    draft_msg_id: int,
    parent_id: int | None,
    attestation: dict | None,
    gen_key=None,
) -> dict | None:
    """Persist and broadcast the strongest model identity evidence for a turn."""
    if not isinstance(attestation, dict):
        return None
    candidate = _attach_preparation_provenance(
        _normalize_turn_model_attestation(attestation)
    )
    payload_budget = getattr(asyncio.current_task(), "_replay_budget", None)
    if payload_budget:
        candidate["payload_budget"] = payload_budget
    context_plan = getattr(asyncio.current_task(), "_context_plan", None)
    if context_plan:
        candidate["context_plan"] = context_plan
    existing = None
    message = await db.get_message(draft_msg_id)
    if message and message.get("model_attestation"):
        try:
            existing = _normalize_turn_model_attestation(
                json.loads(message["model_attestation"])
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            existing = None
    if isinstance(existing, dict):
        existing_status = existing.get("status")
        candidate_status = candidate.get("status")
        weak_statuses = {"requested", "configured", "unverified"}
        if existing_status in {"verified", "mismatch"} and candidate_status in weak_statuses:
            return existing
        if (
            existing_status == "verified"
            and candidate_status == "verified"
            and existing.get("verification_level") == "provider_response"
            and candidate.get("verification_level") != "provider_response"
        ):
            return existing

    await db.update_message_content(
        draft_msg_id,
        model_attestation=json.dumps(candidate, separators=(",", ":")),
    )
    if gen_key:
        _update_gen_snapshot(gen_key, model_attestation=candidate)
    await _ws_send(
        conv_id,
        {
            "type": "model_attestation",
            "draft_msg_id": draft_msg_id,
            "parent_id": parent_id,
            "attestation": candidate,
        },
    )
    return candidate


def _model_attestation_status_text(attestation: dict) -> str:
    status = attestation.get("status") or "unverified"
    harness = attestation.get("harness") or "agent harness"
    effective = attestation.get("effective_model") or attestation.get("launch_model") or "unknown"
    provider = attestation.get("model_provider") or "unknown provider"
    if status == "verified":
        return f"Verified by {harness}: {effective} via {provider}"
    if status == "mismatch":
        return f"Model mismatch in {harness}: runtime reported {effective}"
    if status == "configured":
        return f"Configured {harness}: {effective}; effective model not exposed by runtime"
    return f"Unverified {harness} model identity"


async def _handle_generation(websocket: WebSocket, conv_id: int, data: dict):
    """Handle a generation request over WebSocket — routes by conversation mode."""
    parent_id = data.get("parent_id")
    if parent_id is None:
        raise ValueError("Generation requires an explicit persisted parent message")
    parent_msg = await db.get_message(parent_id)
    if not parent_msg or int(parent_msg.get("conversation_id") or 0) != int(conv_id):
        raise ValueError("Generation parent does not belong to this conversation")
    parent_thread_id = parent_msg.get("loom_thread_id")
    if not parent_thread_id:
        raise ValueError("Generation parent has no Loom thread identity")
    if data.get("loom_thread_id") and str(data["loom_thread_id"]) != str(
        parent_thread_id
    ):
        raise ValueError("Generation thread identity does not match its parent")
    data["loom_thread_id"] = parent_thread_id

    conv = await db.get_conversation(conv_id)
    if reason := model_registry.retirement_reason((conv or {}).get("mode"), data.get("cc_model") or (conv or {}).get("cc_model")):
        raise HTTPException(410, reason)
    if conv and conv.get("local_model") and config.llama_model:
        local_model = conv["local_model"]
        if models_match(local_model, config.llama_model):
            if local_model != config.llama_model:
                await db.update_conversation_fields(conv_id, local_model=config.llama_model)
                conv = dict(conv)
                conv["local_model"] = config.llama_model
    if conv and conv.get("cc_model") and config.llama_model:
        cc_model = conv["cc_model"]
        is_api = any(
            cc_model.startswith(prefix)
            for prefix in ("claude-", "fable", "sonnet", "haiku", "opus", "gemini", "Gemini", "gpt", "GPT", "goose:", "openrouter:", "dream:", "umans-", "codex", "o3", "o4")
        )
        if not is_api and models_match(cc_model, config.llama_model):
            if cc_model != config.llama_model:
                await db.update_conversation_fields(conv_id, cc_model=config.llama_model)
                conv = dict(conv)
                conv["cc_model"] = config.llama_model

    mode = conv.get("mode", "weave") if conv else "weave"
    if mode in {"claude", "codex", "gemini", "openrouter", "umans", "goose"}:
        requested_cc_model = data.get("cc_model") or conv.get("cc_model") or ""
        target_mode = _mode_for_cc_model(requested_cc_model)
        if target_mode is None:
            await _ws_send(
                conv_id,
                {
                    "type": "error",
                    "provider": mode,
                    "stage": "model_resolution",
                    "requested_model": requested_cc_model,
                    "error": (
                        f"Unknown model selector '{requested_cc_model}'; "
                        "refresh the picker or choose an available model"
                    ),
                },
            )
            return
        if target_mode != mode:
            conv = dict(conv)
            conv["mode"] = target_mode
            conv["cc_model"] = requested_cc_model
            mode = target_mode
            if not _truthy_setting(conv.get("parallel_agents_enabled")):
                await db.update_conversation_fields(
                    conv_id, mode=target_mode, cc_model=requested_cc_model
                )

    if mode in ("claude", "gemini", "codex", "openrouter"):
        await _handle_claude_generation(websocket, conv_id, conv, data)
        return

    if mode == "goose":
        await _handle_goose_generation(websocket, conv_id, conv, data)
        return

    if mode == "local":
        await _handle_local_generation(websocket, conv_id, conv, data)
        return

    if mode == "umans":
        await _handle_umans_generation(websocket, conv_id, conv, data)
        return

    # NOTE: A dream-model short-circuit used to live here, routing weave convs
    # that picked the DiffusionGemma model to _handle_dream_completion. It was
    # removed so dream-model weave convs flow into _handle_ooda_generation /
    # _handle_weave_generation, which build the full structured prompt
    # (character + style + state cards + persona/lore) via prompt_engine and
    # pass it to llama_client.stream_chat/sync_chat — and llama_client already
    # routes dream models to config.dream_host transparently
    # (_is_dream_model / _chat_host_for_model). _handle_dream_completion is
    # retained below but no longer reached from this dispatcher.

    # Backstage convs always go through OODA so state card tools are available.
    # The model is picked from cc-inline-controls in the UI and sent as cc_model;
    # inject it as local_model so the OODA handler uses it for the llama call.
    if conv.get("backstage_parent_id") and data.get("cc_model"):
        conv = dict(conv)
        conv["local_model"] = data["cc_model"]
        # Also persist so the dropdown re-selects on next load
        await db.update_conversation_fields(conv_id, local_model=data["cc_model"])

    if _truthy_setting(conv.get("ooda_enabled")) and not _truthy_setting(conv.get("system_only")):
        await _handle_ooda_generation(websocket, conv_id, conv, data)
    else:
        await _handle_weave_generation(websocket, conv_id, conv, data)


async def _handle_claude_generation(
    websocket: WebSocket, conv_id: int, conv: dict, data: dict
):
    """Handle Claude Code CLI generation with session resume support."""
    if asyncio.current_task() is not None:
        asyncio.current_task()._context_plan = None
        asyncio.current_task()._context_replay_region = ""
    print(f"[CC-GEN] Entered: conv_id={conv_id}, action={data.get('action')}, parent_id={data.get('parent_id')}, cc_model={data.get('cc_model')}, _use_llama={conv.get('_use_llama')}")
    proc = None
    is_gemini = False
    is_codex = False
    draft_msg_id = None
    full_text = ""
    content_blocks: list[dict] = []
    new_session_id = ""
    resume_session_id = None
    cc_session_mode = ""
    actual_model = ""
    cc_model = ""
    model_attestation = None
    _gen_start_t = _time.time()
    first_token_ms = None
    try:
        action = data.get("action")
        parent_id = data.get("parent_id")
        _describe_msg_id = None

        if parent_id is None:
            raise ValueError("Claude generation requires an explicit parent message")

        project_dir = conv.get("project_dir") or "."
        canvas_enabled = bool(conv.get("canvas_enabled"))
        if project_dir != "." and not os.path.isdir(project_dir):
            await _ws_send(
                conv_id,
                {
                    "type": "error",
                    "error": f"Working directory not found: {project_dir}",
                },
            )
            return
        # For local mode, _handle_local_generation already set cc_model and
        # _use_llama on the conv dict — don't let the frontend override it.
        # For claude mode, prefer the client's current UI state over stale DB.
        if conv.get("_use_llama"):
            cc_model = conv.get("cc_model") or "sonnet"
        else:
            cc_model = data.get("cc_model") or conv.get("cc_model") or "sonnet"
        cc_effort = data.get("cc_effort") or conv.get("cc_effort") or "high"
        cc_permission_mode = (
            data.get("cc_permission_mode")
            or conv.get("cc_permission_mode")
            or "default"
        )
        # Persist the exact generation settings together. This closes the race
        # where a send can outrun the dropdown's independent PUT request.
        generation_settings = {}
        if data.get("cc_model") and data["cc_model"] != conv.get("cc_model"):
            generation_settings["cc_model"] = cc_model
        if data.get("cc_effort") and data["cc_effort"] != conv.get("cc_effort"):
            generation_settings["cc_effort"] = cc_effort
        if (
            data.get("cc_permission_mode")
            and data["cc_permission_mode"] != conv.get("cc_permission_mode")
        ):
            generation_settings["cc_permission_mode"] = cc_permission_mode
        if generation_settings and not _truthy_setting(conv.get("parallel_agents_enabled")):
            await db.update_conversation_fields(conv_id, **generation_settings)

        # Identify provider based on model name. Accepts aliases (sonnet/opus/
        # haiku), pinned full IDs (claude-opus-4-6), and `[1m]` suffix variants.
        is_anthropic = model_context.is_anthropic(cc_model)
        is_gemini = is_gemini_model(cc_model)
        is_codex = is_codex_model(cc_model)
        is_dream = is_dream_claude_model(cc_model)
        is_openrouter = openrouter_client.is_openrouter_model(cc_model)
        # .gguf models go through Claude Code with ANTHROPIC_BASE_URL pointed
        # at the local llama-server (which speaks /v1/messages natively on :11434).
        is_llama = cc_model.endswith(".gguf")
        # Umans AI models: remote Anthropic-compatible endpoint.
        is_umans = is_umans_model(cc_model)
        # Only use llama when explicitly flagged (local mode) or when model is a .gguf.
        # The _use_llama flag is set by _handle_local_generation on a shallow copy.
        use_llama = conv.get("_use_llama", False) or is_llama
        # Use umans when explicitly flagged or when model is umans-*.
        use_umans = conv.get("_use_umans", False) or is_umans
        # Use Dream when explicitly selected from the Claude Code model picker.
        use_dream = is_dream
        use_openrouter = is_openrouter
        # Belt-and-suspenders: NEVER route Anthropic/Gemini/Codex through llama/umans/openrouter
        if is_anthropic or is_gemini or is_codex:
            use_llama = False
            use_umans = False
            use_dream = False
            use_openrouter = False
        # Mutually exclusive: don't route through both at once
        if use_umans or use_dream or use_openrouter:
            use_llama = False
        if use_dream:
            use_umans = False
        if use_openrouter:
            use_umans = False
            use_dream = False
        provider_model = (
            _dream_claude_model_id(cc_model)
            if use_dream
            else openrouter_client.model_slug(cc_model) if use_openrouter else cc_model
        )
        cc_session_mode = (
            "gemini" if is_gemini
            else "codex" if is_codex
            else "dream-claude" if use_dream
            else "openrouter-claude" if use_openrouter
            else "claude"
        )
        print(f"[CC] Model routing: cc_model={cc_model!r} provider_model={provider_model!r} is_anthropic={is_anthropic} is_gemini={is_gemini} is_codex={is_codex} is_dream={is_dream} is_openrouter={is_openrouter} is_llama={is_llama} use_llama={use_llama} is_umans={is_umans} use_umans={use_umans} use_dream={use_dream} use_openrouter={use_openrouter} conv._use_llama={conv.get('_use_llama')} conv.mode={conv.get('mode')}")

        target_mode = "gemini" if is_gemini else ("codex" if is_codex else ("openrouter" if use_openrouter else ("umans" if use_umans else ("local" if use_llama else ("dream-claude" if use_dream else "claude")))))

        # Operator conversations only launch on providers with a ported
        # lockdown — the model picker can otherwise route a locked-down conv
        pass

        if conv.get("mode") != target_mode:
            print(f"[CC] Updating conversation {conv_id} mode from {conv.get('mode')} to {target_mode}")
            await db.update_conversation_fields(conv_id, mode=target_mode)
            conv["mode"] = target_mode

        # --- Session resume logic ---
        # Every turn uses --resume + --fork-session. Each assistant message
        # gets its own immutable session snapshot. This means branches, edits,
        # regenerates, and linear continuations all use the same operation.
        # If no ancestor session exists (first message), fall through to
        # full history rebuild.
        resume_session_id = None
        fork_session = True  # always fork — every turn creates a new snapshot
        use_resume = False
        resume_reason = "no_prior_session"
        continuity_decision = ResumeDecision(None, "no_assistant_session")
        branch = []

        if parent_id:
            branch = await db.get_branch_to_root(parent_id)

        if parent_id and branch:
            resume_decision = select_resume_session(branch, cc_session_mode, target_model=cc_model)
            if is_codex:
                resume_decision = await _recheck_completed_codex_boundary(
                    branch, resume_decision, cc_model, project_dir,
                )
            if not conv.get('backstage_parent_id'):
                from context_runtime import respect_manual_boundary
                resume_decision = await respect_manual_boundary(branch, resume_decision)
            continuity_decision = resume_decision
            resume_session_id = resume_decision.session_id
            resume_reason = resume_decision.reason
            if not resume_session_id:
                print(
                    f"[CC] Native session rebuild: reason={resume_decision.reason} "
                    f"boundary_msg={resume_decision.message_id} target={cc_session_mode}"
                )

        if resume_session_id:
            use_resume = True
            print(f"[CC] Session resume: id={resume_session_id} fork={fork_session}")

            # agy-operator turns launch fresh-conv (no --conversation, by
            # design — fresh tool registry per turn, see gemini_client.py:622).
            # So agy has NO server-side memory to lean on: the one-line
            # latest-user-message short-circuit below is correct for codex
            # (stateful thread/fork) and claude (stateful --resume) but
            # AMNESIAC for agy — the one-line prompt lands on a process with
            # zero conversation history. Force the full-history-rebuild path
            # so agy gets prior turns as text. resume_session_id/use_resume
            # stay set so the client override (gemini_client.py:489) still
            # fires and logs. See [[agy-operator-turn2-no-response]].
            latest_user_content = ""
            if branch:
                for msg in reversed(branch):
                    if msg["role"] == "user":
                        latest_user_content = msg["content"]
                        break

            prompt = latest_user_content or "(continue)"
        else:
            # No session resume — build full history from branch
            print(f"[CC] No session resume, building full history from branch, parent_id={parent_id}, is_gemini={is_gemini}")
            if parent_id:
                branch = await db.get_branch_to_root(parent_id)
                print(f"[CC] Retrieved branch with {len(branch)} messages")
                prompt = await _prepare_agent_handoff(branch, conv, cc_model, cc_session_mode,
                                                      project_dir=project_dir)
                if not prompt:
                    if is_gemini or is_codex:
                        print(f"[CC] WARNING: Empty prompt from branch for Gemini/Codex!")
                        prompt = "(continue)"
                    else:
                        await _ws_send(
                            conv_id, {"type": "error", "error": "No message to send to Claude"}
                        )
                        return
            else:
                print(f"[CC] No parent_id, no branch to retrieve")
                prompt = "(continue)"

        if not conv.get('backstage_parent_id'):
            if use_resume:
                from context_runtime import renew_native_retrieval
                prompt = await renew_native_retrieval(prompt, branch)
            prompt = add_continuity_notice(
                prompt,
                continuity_decision,
                branch,
                cc_session_mode,
                target_model=cc_model,
            )

        session_continuity = {
            "status": "forked" if use_resume else "rebuilt",
            "reason": resume_reason,
            "source_session_id": resume_session_id if use_resume else None,
        }

        # Retrieve describe context from DB (vision model only, never sent to chat model)
        _describe_context = None
        if branch:
            for msg in reversed(branch):
                if msg["role"] == "user":
                    _describe_context = msg.get("describe_context")
                    break

        # Attach images if present on the latest user message
        # (runs for both session-resume and fresh-session paths)
        if branch:
            last_user_msg = None
            for msg in reversed(branch):
                if msg["role"] == "user":
                    last_user_msg = msg
                    break
            if last_user_msg and last_user_msg.get("image_path"):
                img_paths = _parse_image_paths(last_user_msg["image_path"])
                import shutil

                file_notes = []
                # Track which files are images for describe
                image_files: list[tuple[str, Path]] = []  # (original_name, attached_path)

                # Step 1: Copy all files to attached_files/
                for ip in img_paths:
                    src = Path(ip).resolve()
                    file_ext = src.suffix.lower()
                    attached_files_dir = Path(project_dir) / "attached_files"
                    attached_files_dir.mkdir(exist_ok=True)
                    dest = attached_files_dir / src.name
                    copied = False
                    try:
                        shutil.copy2(str(src), str(dest))
                        copied = True
                    except Exception as e:
                        print(
                            f"[UPLOAD] Failed to copy file {src.name} to attached_files/: {e}"
                        )
                    if file_ext in _IMAGE_EXTS and copied:
                        image_files.append((src.name, dest))
                    if use_llama:
                        if file_ext not in _IMAGE_EXTS:
                            file_notes.append(f"{src.name} (in attached_files/)")
                    else:
                        if copied:
                            file_notes.append(f"{src.name} (in attached_files/)")
                        else:
                            file_notes.append(str(src).replace("\\", "/"))

                   # Step 2: Describe images (local mode only) — runs ONCE for all images
                desc_map: dict[str, str] = {}
                print(f"[DESCRIBE] check — use_llama={use_llama}, use_umans={use_umans}, image_files={len(image_files)}, cc_model={cc_model}")
                if not use_llama or not image_files:
                    print(f"[DESCRIBE] Skipping: use_llama={use_llama}, image_files={len(image_files)}")
                if use_llama and image_files:
                    # Pre-flight image describe is llama-only. umans rides the
                    # same Anthropic transport as Claude (api.code.umans.ai),
                    # so Read delivers native image content blocks to it directly —
                    # no text-summary detour, and no "do NOT read image files" header.
                    _describe_model = config.vision_model or config.llama_model
                    print(f"[DESCRIBE] Running describe for {len(image_files)} image(s), model={_describe_model}")
                    await _ws_send(conv_id, {
                        "type": "describe_start",
                        "parent_msg_id": last_user_msg["id"],
                        "image_count": len(image_files),
                        "model": _describe_model,
                        "started_at": _time.time()
                    })
                    _describe_start = _time.time()
                    for orig_name, attached_path in image_files:
                        try:
                            await _ws_send(conv_id, {"type": "status", "text": f"Describing image {orig_name}..."})
                            desc = await asyncio.wait_for(describe_image(str(attached_path), model=config.vision_model or None, context=_describe_context), timeout=120)
                            file_notes.append(f"[Image: {desc}]")
                            desc_map[orig_name] = desc
                        except asyncio.TimeoutError:
                            print(f"[DESCRIBE] Timed out describing {orig_name} (120s)")
                        except Exception as e:
                            print(f"[DESCRIBE] Failed to describe image {orig_name}: {e}")
                    _describe_elapsed = (_time.time() - _describe_start) * 1000
                    await _ws_send(conv_id, {
                        "type": "describe_done",
                        "parent_msg_id": last_user_msg["id"],
                        "descriptions": desc_map,
                        "model": _describe_model,
                        "elapsed_ms": _describe_elapsed
                    })

                # Step 3: Persist descriptions and create describe message
                if desc_map:
                    try:
                        await db.update_message_image_alt(
                            last_user_msg["id"], json.dumps(desc_map)
                        )
                        await _ws_send(conv_id, {
                            "type": "image_describe",
                            "message_id": last_user_msg["id"],
                            "descriptions": desc_map,
                            "model": _describe_model,
                        })
                    except Exception as e:
                        print(f"[DESCRIBE] Failed to persist/emit descriptions: {e}")

                    _describe_content = f"[Image description — {_describe_model}]\n\n" + "\n\n".join(desc_map.values())
                    _describe_msg = await db.add_message(
                        conv_id, "system", _describe_content,
                        parent_id=last_user_msg["id"]
                    )
                    _describe_msg_id = _describe_msg["id"]

                if file_notes:
                    files_str = "\n".join(f"  • {note}" for note in file_notes)
                    _has_img = any(note.startswith("[Image") for note in file_notes)
                    _hdr = "[Attached files — image descriptions already provided, do NOT read image files.]" if _has_img else f"[User attached {len(file_notes)} file(s). See attached_files/ for the files.]"
                    prompt += f"\n\n{_hdr}\n{files_str}"
        # Create a newly authorized draft for this exact generation. Never
        # revive an old empty assistant child: it belongs to an earlier run
        # and cannot prove ownership of this provider lane.
        _draft_parent = _describe_msg_id or parent_id
        draft_msg = await _create_generation_draft(conv_id, conv, _draft_parent)
        draft_msg_id = draft_msg["id"]
        await db.update_message_content(draft_msg_id, cc_model_used=cc_model)
        await db.set_active_branch(conv_id, draft_msg_id)

        # Initialize live snapshot for this generation (survives WS disconnects)

        _gen_key_local = getattr(asyncio.current_task(), "_gen_key", None)
        if _gen_key_local:
            _update_gen_snapshot(
                _gen_key_local,
                full_text="",
                content_blocks=[],
                input_tokens=0,
                output_tokens=0,
                started_at=_time.time(),
                draft_msg_id=draft_msg_id,
                parent_id=parent_id,
                cc_model=cc_model,
                mode="gemini" if is_gemini else ("codex" if is_codex else ("openrouter" if use_openrouter else ("umans" if use_umans else ("local" if use_llama else ("dream-claude" if use_dream else "claude"))))),
            )

        # Let the client know we're launching
        if is_gemini:
            launch_label = f"Launching agy ({cc_model})..."
        elif is_codex:
            launch_label = f"Launching Codex ({cc_model})..."
        elif use_umans:
            launch_label = f"Launching {cc_model} via Umans..."
        elif use_openrouter:
            launch_label = f"Launching {provider_model} via OpenRouter..."
        elif use_dream:
            launch_label = f"Launching {provider_model} via Dream shim..."
        elif use_llama:
            launch_label = f"Launching {cc_model} via Llama Server..."
        else:
            launch_label = f"Launching Claude Code ({cc_model})..."
        if use_resume:
            launch_label += " (forking native session)"
        else:
            launch_label += f" (rebuilding history: {resume_reason})"
        await _ws_send(
            conv_id, {"type": "status", "text": launch_label, "parent_id": parent_id}
        )
        await _ws_send(
            conv_id,
            {
                "type": "stream_start",
                "parent_id": parent_id,
                "draft_msg_id": draft_msg_id,
                "cc_model": cc_model,
                "session_continuity": session_continuity,
            },
        )
        # Mirror the FE's _streamStartTime so the persisted generation_ms
        # matches what the user saw as the live timer.
        _gen_start_t = _time.time()

        # Initialize accumulation vars before try so error handlers can reference them
        full_text = ""
        content_blocks = []
        new_session_id = ""
        actual_model = ""

        # Launch CC — with resume if available, with fallback on failure
        prompt = add_canvas_notice(prompt, conv)
        try:
            if use_dream:
                ok, info = await _ensure_dream_shim_running()
                if not ok:
                    raise RuntimeError(info)
                await _ws_send(
                    conv_id,
                    {"type": "status", "text": f"Dream shim ready at {info}", "parent_id": parent_id},
                )
            if is_gemini:
                proc, event_stream = await gemini_client.run_gemini(
                    prompt,
                    project_dir,
                    conv_id=conv_id,
                    server_port=config.port,
                    model=cc_model,
                    effort=cc_effort,
                    permission_mode=cc_permission_mode,
                    resume_session_id=resume_session_id if use_resume else None,
                    fork_session=fork_session,
                    backstage_parent_id=conv.get("backstage_parent_id"),
                    
                    is_first_turn=not use_resume,
                )
            elif is_codex:
                proc, event_stream = await codex_client.run_codex(
                    prompt,
                    project_dir,
                    conv_id=conv_id,
                    server_port=config.port,
                    model=cc_model,
                    effort=cc_effort,
                    permission_mode=cc_permission_mode,
                    resume_session_id=resume_session_id if use_resume else None,
                    fork_session=fork_session,
                    backstage_parent_id=conv.get("backstage_parent_id"),
                    
                    permission_request_handler=handle_cc_permission,
                )
            else:
                proc, event_stream = await claude_client.run_claude(
                    prompt,
                    project_dir,
                    conv_id=conv_id,
                    server_port=config.port,
                    model=provider_model,
                    requested_model=cc_model,
                    context_replay_history=_local_replay_region(branch) if use_llama else "",
                    effort=cc_effort,
                    permission_mode=cc_permission_mode,
                    resume_session_id=resume_session_id if use_resume else None,
                    fork_session=fork_session,
                    use_llama=use_llama,
                    use_umans=use_umans,
                    use_dream=use_dream,
                    use_openrouter=use_openrouter,
                    backstage_parent_id=conv.get("backstage_parent_id"),
                    
                )
        except Exception as e:
            if use_resume and (resume_rejected(e) or context_exhausted(e)):
                # Fallback: retry without --resume (session may be stale/deleted)
                print(f"[CC] Resume failed ({e}), falling back to full history")
                await _ws_send(conv_id, {"type": "status", "text": "Session resume failed — rebuilding from history..."})
                branch = await db.get_branch_to_root(parent_id) if parent_id else []
                prompt = await _prepare_agent_handoff(branch, conv, cc_model, cc_session_mode,
                                                      project_dir=project_dir, reason="native_resume_launch_failed")
                prompt = add_continuity_notice(
                    prompt,
                    ResumeDecision(
                        None,
                        "native_resume_launch_failed",
                        continuity_decision.message_id,
                    ),
                    branch,
                    cc_session_mode,
                    target_model=cc_model,
                )
                prompt = add_canvas_notice(prompt, conv)
                if is_gemini:
                    proc, event_stream = await gemini_client.run_gemini(
                        prompt,
                        project_dir,
                        conv_id=conv_id,
                        server_port=config.port,
                        model=cc_model,
                        effort=cc_effort,
                        permission_mode=cc_permission_mode,
                        
                    )
                elif is_codex:
                    proc, event_stream = await codex_client.run_codex(
                        prompt,
                        project_dir,
                        conv_id=conv_id,
                        server_port=config.port,
                        model=cc_model,
                        effort=cc_effort,
                        permission_mode=cc_permission_mode,
                        
                        permission_request_handler=handle_cc_permission,
                    )
                else:
                    proc, event_stream = await claude_client.run_claude(
                        prompt,
                        project_dir,
                        conv_id=conv_id,
                        server_port=config.port,
                        model=provider_model,
                        requested_model=cc_model,
                        context_replay_history=_local_replay_region(branch) if use_llama else "",
                        effort=cc_effort,
                        permission_mode=cc_permission_mode,
                        use_llama=use_llama,
                        use_umans=use_umans,
                        use_dream=use_dream,
                        use_openrouter=use_openrouter,
                        backstage_parent_id=conv.get("backstage_parent_id"),
                        
                    )
                use_resume = False
                session_continuity = {
                    "status": "rebuilt",
                    "reason": "native_resume_launch_failed",
                    "source_session_id": resume_session_id,
                }
            else:
                raise

        _active_claude_procs[conv_id] = proc
        _track_generation_provider(proc)

        # Persist the generation so server-restart can reap or (phase 2) rescue.
        # session_id is filled in later when CC emits session_info.
        try:
            await db.register_active_generation(
                draft_msg_id=draft_msg_id,
                conv_id=conv_id,
                pid=proc.pid,
                project_dir=project_dir,
                mode="openrouter" if use_openrouter else ("umans" if use_umans else ("local" if use_llama else ("dream-claude" if use_dream else ("gemini" if is_gemini else ("codex" if is_codex else "claude"))))),
            )
        except Exception as e:
            print(f"[GEN] Failed to register active generation: {e}")

        full_text = ""
        content_blocks = []
        current_block = None
        result_info = {}
        new_session_id = ""
        actual_model = ""  # what CC actually ran (may differ from cc_model if CC fell back)
        model_attestation: dict | None = None
        first_token_ms: int | None = None
        total_input_tokens = 0
        total_output_tokens = 0
        got_error = False
        rate_limit_data: dict | None = None
        synthetic_error: dict | None = None

        async def _record_model_attestation(evt: dict) -> None:
            """Persist and broadcast provider-returned model identity evidence."""
            nonlocal model_attestation
            attestation = evt.get("model_attestation")
            if not isinstance(attestation, dict):
                return
            attestation = {
                **attestation,
                "session_continuity": session_continuity["status"],
                "session_continuity_reason": session_continuity["reason"],
                "source_session_id": session_continuity.get("source_session_id"),
            }
            recorded = await _record_turn_model_attestation(
                conv_id,
                draft_msg_id,
                parent_id,
                attestation,
                _gen_key_local,
            )
            if recorded is None:
                return
            model_attestation = recorded
            # The typed `model_attestation` event (broadcast by
            # _record_turn_model_attestation) updates the header chip next to
            # the role label. Do NOT also broadcast this as a `status` text:
            # that paints the "Verified by ..." sentence into the footer
            # beneath the looming animation, which no other provider does.

        async def _record_first_provider_output(event_type: str) -> None:
            """Persist the user's actual wait to the first model-originated event."""
            nonlocal first_token_ms
            if first_token_ms is not None or event_type not in {
                "text_delta", "thinking_delta", "tool_start"
            }:
                return
            first_token_ms = int((_time.time() - _gen_start_t) * 1000)
            await db.update_message_content(
                draft_msg_id, time_to_first_token_ms=first_token_ms
            )
            if _gen_key_local:
                _update_gen_snapshot(
                    _gen_key_local, time_to_first_token_ms=first_token_ms
                )
            await _ws_send(
                conv_id,
                {"type": "first_token", "elapsed_ms": first_token_ms},
            )

        async for evt in turn_recovery.events(event_stream):
            etype = evt["type"]
            await _record_first_provider_output(etype)

            if etype == "session_info":
                new_session_id = evt.get("session_id", "") or new_session_id
                reported_model = evt.get("model", "")
                if reported_model and evt.get("model_confirmed", True):
                    actual_model = reported_model
                await _record_model_attestation(evt)
                # Patch session_id onto the tracking row for future rescue support
                try:
                    await db.update_active_generation_session(draft_msg_id, new_session_id)
                except Exception:
                    pass

            elif etype == "codex_launch_info":
                if _gen_key_local:
                    _update_gen_snapshot(
                        _gen_key_local,
                        codex_launch_info=evt,
                        cc_model=evt.get("model", cc_model),
                    )
                await _ws_send(
                    conv_id,
                    {
                        "type": "status",
                        "text": (
                            f"Codex request: model={evt.get('model')} "
                            f"(fallback disabled), approval={evt.get('approval_policy')}, "
                            f"sandbox={evt.get('sandbox')}, cwd={evt.get('cwd')}"
                        ),
                    },
                )

            elif etype == "status":
                await _ws_send(
                    conv_id,
                    {
                        "type": "status",
                        "text": evt.get("text", ""),
                        "parent_id": parent_id,
                    },
                )

            elif etype == "text_delta":
                full_text += evt["text"]
                if current_block and current_block["type"] == "text":
                    current_block["text"] += evt["text"]
                else:
                    current_block = {"type": "text", "text": evt["text"]}
                    content_blocks.append(current_block)
                await _ws_send(
                    conv_id, {"type": "stream_chunk", "content": evt["text"]}
                )

            elif etype == "tool_start":
                current_block = {
                    "type": "tool_use",
                    "name": evt["name"],
                    "tool_id": evt.get("tool_id", ""),
                    "input": "",
                    "result": "",
                }
                content_blocks.append(current_block)
                await _ws_send(
                    conv_id,
                    {
                        "type": "tool_start",
                        "name": evt["name"],
                        "tool_id": evt.get("tool_id", ""),
                    },
                )

            elif etype == "tool_input_delta":
                if current_block and current_block["type"] == "tool_use":
                    current_block["input"] += evt["json"]
                await _ws_send(
                    conv_id,
                    {
                        "type": "tool_input_chunk",
                        "content": evt["json"],
                        "tool_id": evt.get("tool_id", ""),
                    },
                )

            elif etype == "ask_user_question":
                await _ws_send(
                    conv_id,
                    {
                        "type": "ask_user_question",
                        "questions": evt.get("questions", []),
                        "tool_id": evt.get("tool_id", ""),
                    },
                )

            elif etype == "plan_ready":
                await _ws_send(
                    conv_id,
                    {
                        "type": "plan_ready",
                        "plan": evt.get("plan", ""),
                        "plan_file": evt.get("plan_file", ""),
                        "tool_id": evt.get("tool_id", ""),
                    },
                )
                # Global notification for plan completion
                conv_title = conv.get("title", "Conversation")
                await _ws_broadcast_all(
                    {
                        "type": "plan_landed",
                        "conv_id": conv_id,
                        "conv_title": conv_title,
                        "plan_file": evt.get("plan_file", ""),
                    }
                )

            elif etype == "tool_result":
                result_content = evt.get("content", "")
                tool_id = evt.get("tool_id", "")
                # Tool observations are not chat attachment selections.
                image_url = None
                for block in reversed(content_blocks):
                    if block["type"] == "tool_use" and block.get("tool_id") == tool_id:
                        block["result"] = result_content
                        block["outcome_recorded"] = evt.get("outcome_recorded", True) is True
                        block["is_error"] = bool(evt.get("is_error"))
                        if image_url:
                            block["image_url"] = image_url
                        break
                current_block = None
                tool_result_msg = {
                    "type": "tool_result",
                    "content": result_content,
                    "tool_id": tool_id,
                }
                if image_url:
                    tool_result_msg["image_url"] = image_url
                if evt.get("is_error"):
                    tool_result_msg["is_error"] = True
                await _ws_send(conv_id, tool_result_msg)
                # Progressive save: update draft with accumulated content_blocks
                await db.update_message_content(
                    draft_msg_id,
                    content=full_text,
                    content_blocks=json.dumps(content_blocks),
                )
                # Notify frontend if a canvas file was written
                if canvas_enabled:
                    matched_block = None
                    for block in reversed(content_blocks):
                        if block["type"] == "tool_use" and block.get("tool_id") == tool_id:
                            matched_block = block
                            break
                    if matched_block and matched_block.get("name") in ("Write", "Edit", "MultiEdit"):
                        tool_input_str = matched_block.get("input", "")
                        if "canvas/" in tool_input_str or "canvas\\" in tool_input_str:
                            await _ws_send(conv_id, {"type": "canvas_updated"})

            elif etype == "thinking_delta":
                if current_block and current_block["type"] == "thinking":
                    current_block["text"] += evt["text"]
                else:
                    current_block = {"type": "thinking", "text": evt["text"]}
                    content_blocks.append(current_block)
                await _ws_send(
                    conv_id, {"type": "thinking_chunk", "content": evt["text"]}
                )

            elif etype == "usage":
                # Each API call within a turn carries the full cached prefix in
                # input_tokens, so summing double-counts across tool-call cycles.
                # Track the LAST value (= final context size at end of turn);
                # output_tokens IS per-call delta so summing is correct there.
                total_input_tokens = evt.get("input_tokens", 0)
                total_output_tokens += evt.get("output_tokens", 0)
                await db.update_message_content(draft_msg_id, turn_input_tokens=total_input_tokens,
                                                turn_output_tokens=total_output_tokens)
                await _ws_send(
                    conv_id,
                    {
                        "type": "usage",
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                    },
                )

            elif etype == "compact_boundary":
                # CC compactified its context — fork into a new branch.
                # The compact summary becomes the first message of the new branch,
                # and the draft (assistant response) is reparented under it.
                trigger = evt.get("trigger", "auto")
                pre_tokens = evt.get("pre_tokens")
                token_info = f" — {pre_tokens:,} tokens before" if pre_tokens else ""
                content = f"[CC context compactified ({trigger}){token_info}]"
                # CC forks to a new session at the compact boundary. Capture it
                # now so the post-compact assistant reply doesn't end up orphaned
                # if the result event's session_id is missing or a retry resets state.
                compact_session = evt.get("session_id", "") or ""
                if compact_session:
                    new_session_id = compact_session
                    await db.update_conversation_fields(
                        conv_id, claude_session_id=compact_session
                    )
                marker = await db.add_message(
                    conv_id, "system",
                    content,
                    parent_id=parent_id,
                    is_active=True,
                    cc_session_id=compact_session or None,
                )
                # Reparent the draft under the compact marker so the branch is:
                # ... → parent → [compact marker] → draft(assistant) → ...
                # The DRAFT must remain the active leaf — otherwise the
                # streaming assistant response ends up on a sibling branch and
                # the user has to click "Switch to this branch" to see their
                # own reply.
                if draft_msg_id:
                    _db = await db.get_db()
                    await _db.execute(
                        "UPDATE messages SET parent_id = ? WHERE id = ?",
                        (marker["id"], draft_msg_id),
                    )
                    await _db.commit()
                    await db.set_active_branch(conv_id, draft_msg_id)
                else:
                    await db.set_active_branch(conv_id, marker["id"])
                await _ws_send(
                    conv_id,
                    {
                        "type": "compact_boundary",
                        "trigger": trigger,
                        "pre_tokens": pre_tokens,
                        "marker_id": marker["id"],
                    },
                )
                if compact_session:
                    _spawn_background_task(
                        _patch_marker_with_summary(conv_id, marker["id"], compact_session),
                        f"compact marker summary {marker['id']}",
                    )

            elif etype == "api_retry":
                attempt = evt.get("attempt", 1)
                max_retries = evt.get("max_retries", 5)
                delay_ms = evt.get("retry_delay_ms", 0)
                error = evt.get("error", "unknown")
                await _ws_send(
                    conv_id,
                    {
                        "type": "status",
                        "text": f"API retry {attempt}/{max_retries} ({error}) — retrying in {delay_ms // 1000}s...",
                    },
                )

            elif etype == "auto_continue":
                rnd = evt.get("round", 1)
                suffix = f" (round {rnd})" if rnd > 1 else ""
                await _ws_send(
                    conv_id,
                    {"type": "status", "text": f"Continuing long response{suffix}…"},
                )

            elif etype == "cc_raw_event":
                # Forward unknown events to UI for debugging
                raw_data = evt.get("data", {})
                raw_type = evt.get("event_type", "")
                print(
                    f"[CC] Unknown event type={raw_type}: {json.dumps(raw_data, default=str)[:300]}"
                )
                await _ws_send(
                    conv_id,
                    {
                        "type": "cc_debug_event",
                        "event_type": raw_type,
                        "data": raw_data,
                    },
                )

            elif etype == "rate_limit":
                rate_limit_data = evt.get("data") or {}
                print(
                    f"[CC] rate_limit_event: {json.dumps(rate_limit_data, default=str)[:500]}"
                )
                await _ws_send(
                    conv_id,
                    {"type": "rate_limit_info", "data": rate_limit_data},
                )

            elif etype == "cc_synthetic_error":
                synthetic_error = {
                    "error": evt.get("error", ""),
                    "status": evt.get("status"),
                }
                print(f"[CC] synthetic error: {synthetic_error}")
                await _ws_send(
                    conv_id,
                    {"type": "cc_synthetic_error", **synthetic_error},
                )

            elif etype == "error":
                got_error = True
                result_info = {
                    **result_info,
                    "is_error": True,
                    "error": evt.get("message") or evt.get("error") or "Provider error",
                    "launch_failed": bool(evt.get("launch_failed")),
                    "resume_failed": bool(evt.get("resume_failed")),
                }
            elif etype == "result":
                result_info = evt
                got_error = coordination.result_failed(
                    got_error, is_codex and evt.get("terminal_confirmed") is True,
                    bool(evt.get("is_error")),
                )
                # Use result text as fallback if no text came from assistant events
                # Don't adopt error text as full_text — it would block the retry fallback
                if not full_text and _cc_should_adopt_result_text(evt):
                    full_text = evt["result_text"]
                    content_blocks.append({"type": "text", "text": full_text})

            # Keep live snapshot in sync (reconnecting clients read this)
            if _gen_key_local:
                _update_gen_snapshot(
                    _gen_key_local,
                    full_text=full_text,
                    content_blocks=content_blocks,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )

        if _active_claude_procs.get(conv_id) is proc:
            _active_claude_procs.pop(conv_id, None)

        # If --resume failed, retry with full history fallback. Some local
        # backends can also accept a resumed session and exit 0 while producing
        # an empty content block; treat that as a poisoned/stale resume too.
        # Context exhaustion can recover at a recorded tool boundary. Other
        # resume failures still require an empty attempt; never replay unknown
        # tool effects or treat arbitrary tool text as terminal evidence.
        empty_resume = (
            use_resume
            and not got_error
            and not full_text.strip()
            and not content_blocks
            and bool(result_info)
        )
        recovery = turn_recovery.decide(_cc_result_error_detail(result_info) if got_error or result_info.get("is_error") else "", content_blocks,
            attempts=getattr(asyncio.current_task(), "_context_recovery_attempts", 0),
            enabled=not conv.get('backstage_parent_id'))
        recovering_partial = recovery.allowed and _cc_has_streamed_output(full_text, content_blocks)
        if recovery.reason == "unresolved_tool_outcome":
            result_info["error"] = _cc_result_error_detail(result_info) + " Automatic recovery paused: a tool outcome is unresolved or reported an error. Retained evidence requires reconciliation before repeating actions."
        launch_failed = bool(result_info.get("launch_failed"))
        safe_resume_failure = (
            use_resume
            and not _cc_has_streamed_output(full_text, content_blocks)
            and (
                launch_failed
                or resume_rejected(_cc_result_error_detail(result_info))
                or empty_resume
            )
        )
        if recovery.allowed or safe_resume_failure:
            session_continuity = {
                "status": "rebuilt",
                "reason": (
                    recovery.reason if recovery.allowed else
                    (
                        "native_resume_empty_response" if empty_resume else
                        (
                            "native_resume_launch_failed" if launch_failed else
                            "native_resume_runtime_error"
                        )
                    )
                ),
                "source_session_id": (new_session_id or resume_session_id) if recovery.allowed else resume_session_id,
            }
            error_detail = result_info.get("result_text", "") or result_info.get("error", "")
            if empty_resume:
                print("[CC] Resume returned empty response, retrying with full history")
                status_text = "Session returned no response — retrying with full history..."
            elif launch_failed:
                print(f"[CC] Resume launch failed, retrying with full history: {error_detail[:200]}")
                status_text = "Session resume failed — retrying with full history..."
            else:
                print(f"[CC] Resume returned error, retrying with full history: {error_detail[:200]}")
                status_text = "Session error — retrying with full history..."
            if recovering_partial:
                status_text = "Context exhausted — preserving recorded outcomes and preparing continuation..."
            await _ws_send(conv_id, {"type": "status", "text": status_text})
            await _teardown_child_provider(proc, "gemini" if is_gemini else "codex" if is_codex else "claude")
            if recovering_partial:
                await db.update_message_content(draft_msg_id, content=full_text, content_blocks=json.dumps(content_blocks),
                    cc_session_id=new_session_id or None, cc_session_mode=cc_session_mode, cc_model_used=cc_model,
                    turn_lifecycle_state="partial_failed")
            branch = await db.get_branch_to_root(draft_msg_id if recovering_partial else parent_id) if parent_id else []
            fallback_prompt = await _prepare_agent_handoff(branch, conv, cc_model, cc_session_mode,
                                                           project_dir=project_dir,
                                                           reason=session_continuity["reason"], frozen_tail=recovering_partial)
            fallback_prompt = add_continuity_notice(
                fallback_prompt,
                ResumeDecision(
                    None,
                    session_continuity["reason"],
                    continuity_decision.message_id,
                ),
                branch,
                cc_session_mode,
                target_model=cc_model,
            )
            # Re-attach images
            if branch and not recovering_partial:
                last_user_msg = None
                for msg in reversed(branch):
                    if msg["role"] == "user":
                        last_user_msg = msg
                        break
                if last_user_msg and last_user_msg.get("image_path"):
                    img_paths = _parse_image_paths(last_user_msg["image_path"])
                    import shutil

                    file_notes = []
                    image_files: list[tuple[str, Path]] = []
                    _describe_msg_id = None

                    # Step 1: Copy files
                    for ip in img_paths:
                        src = Path(ip).resolve()
                        file_ext = src.suffix.lower()
                        attached_files_dir = Path(project_dir) / "attached_files"
                        attached_files_dir.mkdir(exist_ok=True)
                        dest = attached_files_dir / src.name
                        copied = False
                        try:
                            shutil.copy2(str(src), str(dest))
                            copied = True
                        except Exception as e:
                            print(
                                f"[UPLOAD] Failed to copy file {src.name} to attached_files/: {e}"
                            )
                        if file_ext in _IMAGE_EXTS and copied:
                            image_files.append((src.name, dest))
                        if use_llama:
                            if file_ext not in _IMAGE_EXTS:
                                file_notes.append(f"{src.name} (in attached_files/)")
                        else:
                            if copied:
                                file_notes.append(f"{src.name} (in attached_files/)")
                            else:
                                file_notes.append(str(src).replace("\\", "/"))

                    # Step 2: Describe images (runs ONCE)
                    desc_map: dict[str, str] = {}
                    if use_llama and image_files:
                        # umans takes the Claude path: native image content blocks
                        # via Read, no describe_image pre-flight. See note above.
                        _describe_model = config.vision_model or config.llama_model
                        await _ws_send(conv_id, {
                            "type": "describe_start",
                            "parent_msg_id": last_user_msg["id"],
                            "image_count": len(image_files),
                            "model": _describe_model,
                            "started_at": _time.time()
                        })
                        _describe_start = _time.time()
                        for orig_name, attached_path in image_files:
                            try:
                                await _ws_send(conv_id, {"type": "status", "text": f"Describing image {orig_name}..."})
                                desc = await asyncio.wait_for(describe_image(str(attached_path), model=config.vision_model or None, context=_describe_context), timeout=120)
                                file_notes.append(f"[Image: {desc}]")
                                desc_map[orig_name] = desc
                            except asyncio.TimeoutError:
                                print(f"[DESCRIBE] Timed out describing {orig_name} (120s)")
                            except Exception as e:
                                print(f"[DESCRIBE] Failed to describe image {orig_name}: {e}")
                        _describe_elapsed = (_time.time() - _describe_start) * 1000
                        await _ws_send(conv_id, {
                            "type": "describe_done",
                            "parent_msg_id": last_user_msg["id"],
                            "descriptions": desc_map,
                            "model": _describe_model,
                            "elapsed_ms": _describe_elapsed
                        })

                    # Step 3: Persist
                    if desc_map:
                        try:
                            await db.update_message_image_alt(
                                last_user_msg["id"], json.dumps(desc_map)
                            )
                            await _ws_send(conv_id, {
                                "type": "image_describe",
                                "message_id": last_user_msg["id"],
                                "descriptions": desc_map,
                                "model": _describe_model,
                            })
                        except Exception as e:
                            print(f"[DESCRIBE] Failed to persist/emit descriptions: {e}")

                        if not _describe_msg_id:
                            _describe_content = f"[Image description — {_describe_model}]\n\n" + "\n\n".join(desc_map.values())
                            _describe_msg = await db.add_message(
                                conv_id, "system", _describe_content,
                                parent_id=last_user_msg["id"]
                            )
                            _describe_msg_id = _describe_msg["id"]
                            if draft_msg_id:
                                _db = await db.get_db()
                                await _db.execute(
                                    "UPDATE messages SET parent_id = ? WHERE id = ?",
                                    (_describe_msg_id, draft_msg_id),
                                )
                                await _db.commit()

                    if file_notes:
                        files_str = "\n".join(f"  • {note}" for note in file_notes)
                        _has_img = any(note.startswith("[Image") for note in file_notes)
                        _hdr = "[Attached files — image descriptions already provided, do NOT read image files.]" if _has_img else f"[User attached {len(file_notes)} file(s). See attached_files/ for the files.]"
                        fallback_prompt += f"\n\n{_hdr}\n{files_str}"

            fallback_prompt = add_canvas_notice(fallback_prompt, conv)
            recovery_event_prefix = ""
            if recovery.allowed:
                recovery_event_prefix = turn_recovery.claim(recovery, asyncio.current_task(), proc)
            if is_gemini:
                proc, event_stream = await gemini_client.run_gemini(
                    fallback_prompt,
                    project_dir,
                    conv_id=conv_id,
                    server_port=config.port,
                    model=cc_model,
                    effort=cc_effort,
                    permission_mode=cc_permission_mode,
                    backstage_parent_id=conv.get("backstage_parent_id"),
                    
                )
            elif is_codex:
                proc, event_stream = await codex_client.run_codex(
                    fallback_prompt,
                    project_dir,
                    conv_id=conv_id,
                    server_port=config.port,
                    model=cc_model,
                    effort=cc_effort,
                    permission_mode=cc_permission_mode,
                    backstage_parent_id=conv.get("backstage_parent_id"),
                    
                    permission_request_handler=handle_cc_permission,
                )
            else:
                proc, event_stream = await claude_client.run_claude(
                    fallback_prompt,
                    project_dir,
                    conv_id=conv_id,
                    server_port=config.port,
                    model=provider_model,
                    requested_model=cc_model,
                    context_replay_history=_local_replay_region(branch) if use_llama else "",
                    effort=cc_effort,
                    permission_mode=cc_permission_mode,
                    use_llama=use_llama,
                    use_umans=use_umans,
                    use_dream=use_dream,
                    use_openrouter=use_openrouter,
                    backstage_parent_id=conv.get("backstage_parent_id"),
                    
                )
            _active_claude_procs[conv_id] = proc
            _track_generation_provider(proc)

            if not recovering_partial:
                full_text = ""
                content_blocks = []
            current_block = None
            result_info = {}
            got_error = False
            new_session_id = ""
            if not recovering_partial:
                total_input_tokens = 0
                total_output_tokens = 0

            async for evt in turn_recovery.events(event_stream):
                evt = turn_recovery.namespace_event(evt, recovery_event_prefix)
                etype = evt["type"]
                await _record_first_provider_output(etype)
                if etype == "session_info":
                    new_session_id = evt.get("session_id", "") or new_session_id
                    model_name = evt.get("model", cc_model)
                    if model_name and evt.get("model_confirmed", True):
                        actual_model = model_name
                    await _record_model_attestation(evt)
                    await _ws_send(
                        conv_id,
                        {
                            "type": "status",
                            "text": f"Connected — {model_name} is thinking...",
                            "parent_id": parent_id,
                        },
                    )
                elif etype == "codex_launch_info":
                    await _ws_send(
                        conv_id,
                        {
                            "type": "status",
                            "text": (
                                f"Codex request: model={evt.get('model')} "
                                f"(fallback disabled), approval={evt.get('approval_policy')}, "
                                f"sandbox={evt.get('sandbox')}, cwd={evt.get('cwd')}"
                            ),
                        },
                    )
                elif etype == "status":
                    await _ws_send(
                        conv_id,
                        {
                            "type": "status",
                            "text": evt.get("text", ""),
                            "parent_id": parent_id,
                        },
                    )
                elif etype == "text_delta":
                    full_text += evt["text"]
                    if current_block and current_block["type"] == "text":
                        current_block["text"] += evt["text"]
                    else:
                        current_block = {"type": "text", "text": evt["text"]}
                        content_blocks.append(current_block)
                    await _ws_send(
                        conv_id, {"type": "stream_chunk", "content": evt["text"]}
                    )
                elif etype == "tool_start":
                    current_block = {
                        "type": "tool_use",
                        "name": evt["name"],
                        "tool_id": evt.get("tool_id", ""),
                        "input": "",
                        "result": "",
                    }
                    content_blocks.append(current_block)
                    await _ws_send(
                        conv_id,
                        {
                            "type": "tool_start",
                            "name": evt["name"],
                            "tool_id": evt.get("tool_id", ""),
                        },
                    )
                elif etype == "tool_input_delta":
                    if current_block and current_block["type"] == "tool_use":
                        current_block["input"] += evt["json"]
                    await _ws_send(
                        conv_id,
                        {
                            "type": "tool_input_chunk",
                            "content": evt["json"],
                            "tool_id": evt.get("tool_id", ""),
                        },
                    )
                elif etype == "ask_user_question":
                    await _ws_send(
                        conv_id,
                        {
                            "type": "ask_user_question",
                            "questions": evt.get("questions", []),
                            "tool_id": evt.get("tool_id", ""),
                        },
                    )
                elif etype == "plan_ready":
                    await _ws_send(
                        conv_id,
                        {
                            "type": "plan_ready",
                            "plan": evt.get("plan", ""),
                            "plan_file": evt.get("plan_file", ""),
                            "tool_id": evt.get("tool_id", ""),
                        },
                    )
                    conv_title = conv.get("title", "Conversation")
                    await _ws_broadcast_all(
                        {
                            "type": "plan_landed",
                            "conv_id": conv_id,
                            "conv_title": conv_title,
                            "plan_file": evt.get("plan_file", ""),
                        }
                    )
                elif etype == "tool_result":
                    result_content = evt.get("content", "")
                    tool_id = evt.get("tool_id", "")
                    # Find the matching tool_use block to get tool name + input
                    matched_block = None
                    for block in reversed(content_blocks):
                        if (
                            block["type"] == "tool_use"
                            and block.get("tool_id") == tool_id
                        ):
                            block["result"] = result_content
                            block["outcome_recorded"] = evt.get("outcome_recorded", True) is True
                            block["is_error"] = bool(evt.get("is_error"))
                            matched_block = block
                            break
                    current_block = None

                    # Tool inputs/results are evidence, not attachment intent.
                    image_url = None

                    tool_result_msg = {
                        "type": "tool_result",
                        "content": result_content,
                        "tool_id": tool_id,
                    }
                    if evt.get("is_error"):
                        tool_result_msg["is_error"] = True
                    if image_url:
                        tool_result_msg["image_url"] = image_url
                        if matched_block:
                            matched_block["image_url"] = image_url
                    await _ws_send(conv_id, tool_result_msg)
                    # Progressive save
                    await db.update_message_content(
                        draft_msg_id,
                        content=full_text,
                        content_blocks=json.dumps(content_blocks),
                    )
                elif etype == "thinking_delta":
                    if current_block and current_block["type"] == "thinking":
                        current_block["text"] += evt["text"]
                    else:
                        current_block = {"type": "thinking", "text": evt["text"]}
                        content_blocks.append(current_block)
                    await _ws_send(
                        conv_id, {"type": "thinking_chunk", "content": evt["text"]}
                    )
                elif etype == "usage":
                    # See the primary CC handler above — track LAST input,
                    # SUM output. Each API call within a turn carries the full
                    # cached prefix so summing input double-counts.
                    total_input_tokens = evt.get("input_tokens", 0)
                    total_output_tokens += evt.get("output_tokens", 0)
                    await db.update_message_content(draft_msg_id, turn_input_tokens=total_input_tokens,
                                                    turn_output_tokens=total_output_tokens)
                    await _ws_send(
                        conv_id,
                        {
                            "type": "usage",
                            "input_tokens": total_input_tokens,
                            "output_tokens": total_output_tokens,
                        },
                    )
                elif etype == "rate_limit":
                    rate_limit_data = evt.get("data") or {}
                    print(
                        f"[CC] rate_limit_event (retry): {json.dumps(rate_limit_data, default=str)[:500]}"
                    )
                    await _ws_send(
                        conv_id,
                        {"type": "rate_limit_info", "data": rate_limit_data},
                    )
                elif etype == "cc_synthetic_error":
                    synthetic_error = {
                        "error": evt.get("error", ""),
                        "status": evt.get("status"),
                    }
                    print(f"[CC] synthetic error (retry): {synthetic_error}")
                    await _ws_send(
                        conv_id,
                        {"type": "cc_synthetic_error", **synthetic_error},
                    )
                elif etype == "error":
                    got_error = True
                    result_info = {**result_info, "is_error": True, "error": evt.get("message") or evt.get("error") or "Provider error"}
                elif etype == "result":
                    result_info = evt
                    got_error = coordination.result_failed(
                        got_error, is_codex and evt.get("terminal_confirmed") is True,
                        bool(evt.get("is_error")),
                    )
                    if not full_text and _cc_should_adopt_result_text(evt):
                        full_text = evt["result_text"]
                        content_blocks.append({"type": "text", "text": full_text})

                # Keep live snapshot in sync (fallback retry loop)
                if _gen_key_local:
                    _update_gen_snapshot(
                        _gen_key_local,
                        full_text=full_text,
                        content_blocks=content_blocks,
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                    )

            if _active_claude_procs.get(conv_id) is proc:
                _active_claude_procs.pop(conv_id, None)

        # If CC produced no output at all, mark draft as error (don't delete)
        if not _cc_has_streamed_output(full_text, content_blocks):
            provider_name = (
                "Antigravity (agy)"
                if is_gemini
                else ("ChatGPT Codex" if is_codex else ("Umans" if use_umans else ("Llama Server" if use_llama else "Claude Code")))
            )
            error_msg = _cc_result_error_detail(result_info)
            if not error_msg:
                if rate_limit_data:
                    info = rate_limit_data.get("rate_limit_info", {})
                    limit_type = info.get("rateLimitType") or rate_limit_data.get("rateLimitType") or "rate cap"
                    resets_at = info.get("resetsAt") or rate_limit_data.get("resetsAt")
                    if resets_at:
                        try:
                            import time
                            reset_time_str = time.ctime(float(resets_at))
                            error_msg = f"{provider_name} rate limit reached ({limit_type} limit) — resets at {reset_time_str}"
                        except Exception:
                            error_msg = f"{provider_name} rate limit reached ({limit_type} limit)"
                    else:
                        error_msg = f"{provider_name} rate limit reached ({limit_type} limit)"
                else:
                    error_msg = f"{provider_name} exited with no response"

            if use_llama and not rate_limit_data:
                error_msg += f" — check that llama-server is running with '{cc_model}'"
            if use_umans and not rate_limit_data:
                error_msg += f" — check that UMANS_API_KEY is set"
            content = f"[Error: {error_msg}]"
            if rate_limit_data:
                content += _format_rate_limit_note(rate_limit_data)
            failed_session = result_info.get("session_id") or new_session_id or None
            model_attestation = _failed_session_attestation(
                model_attestation, result_info, failed_session, cc_session_mode, error_msg,
            )
            asyncio.current_task()._generation_terminal_override = "failed"
            await db.update_message_content(
                draft_msg_id, content=content, cc_session_id=failed_session,
                cc_session_mode=cc_session_mode, cc_model_used=cc_model,
                turn_lifecycle_state="failed", model_attestation=json.dumps(model_attestation),
            )
            await _ws_send(conv_id, {"type": "error", "error": error_msg})
            await _ws_send(
                conv_id,
                {"type": "stream_end", "message": await db.get_message(draft_msg_id)},
            )
            return

        # Partial output: agent died mid-turn (hook timeout, crash, etc.)
        # Save whatever text/tools streamed before death so the user isn't
        # left with a blank message — they can read what happened and retry.
        failed_turn = got_error or result_info.get("is_error") or not result_info or not full_text.strip()
        if failed_turn and _cc_has_streamed_output(full_text, content_blocks):
            full_text = _append_cc_interrupt_note(full_text, content_blocks, result_info)

        # CC streamed an error response (e.g. "You've hit your org's monthly
        # usage limit") as assistant text. Augment the saved draft so the user
        # knows the wording came from CC, not the API, and may be wrong about
        # the window. Prefer synthetic_error metadata (the 429 path); fall back
        # to rate_limit_event data (the proactive-warning path).
        if result_info.get("is_error") and full_text and "[Loom note:" not in full_text:
            note = ""
            if synthetic_error:
                note = _format_synthetic_error_note(synthetic_error)
            elif rate_limit_data:
                note = _format_rate_limit_note(rate_limit_data)
            if note:
                full_text = full_text.rstrip() + note
                if content_blocks and content_blocks[-1].get("type") == "text":
                    content_blocks[-1]["text"] = (
                        content_blocks[-1]["text"].rstrip() + note
                    )
                await db.update_message_content(
                    draft_msg_id,
                    content=full_text,
                    content_blocks=json.dumps(content_blocks),
                )

        # Extract cost info
        cost_usd = result_info.get("cost_usd", 0)
        output_tokens = total_output_tokens
        # Final context size at end of turn — see the streaming usage handler
        # above for why we don't sum input_tokens across API calls.
        input_tokens = total_input_tokens
        new_session_id = result_info.get("session_id", "") or new_session_id
        duration_ms = result_info.get("duration_ms", 0)
        if failed_turn:
            model_attestation = _failed_session_attestation(
                model_attestation, result_info, new_session_id, cc_session_mode,
                _cc_result_error_detail(result_info) or ("rate limit" if rate_limit_data else ""),
            )
            asyncio.current_task()._generation_terminal_override = "partial_failed"
            await db.update_message_content(draft_msg_id, turn_lifecycle_state="partial_failed")

        # Record what CC actually ran (from session_info), not just what we asked
        # for. If CC silently downgraded (usage cap, availability), surface it.
        resolved_model = actual_model or cc_model
        if use_llama:
            model_used_field = f"{cc_model}@llama-server"
        elif use_dream:
            model_used_field = f"{provider_model}@dream-shim"
        elif use_openrouter:
            model_used_field = f"openrouter:{provider_model}"
        else:
            model_used_field = resolved_model

        if is_gemini and actual_model:
            expected_model = gemini_client._loom_model_to_agy(cc_model, cc_effort)
            if expected_model.casefold() != actual_model.casefold():
                await _ws_send(
                    conv_id,
                    {
                        "type": "error",
                        "provider": "antigravity",
                        "stage": "model_confirmation",
                        "requested_model": cc_model,
                        "expected_model": expected_model,
                        "actual_model": actual_model,
                        "error": (
                            f"Antigravity model mismatch: requested '{cc_model}' "
                            f"(launch id '{expected_model}'), but agy reported '{actual_model}'"
                        ),
                    },
                )

        fallback_note = _detect_model_fallback(cc_model, actual_model)
        if fallback_note:
            await _ws_send(
                conv_id,
                {
                    "type": "warning",
                    "text": f"{fallback_note} — this branch is now on {actual_model}. Further turns may auto-compact sooner.",
                    "requested_model": cc_model,
                    "actual_model": actual_model,
                },
            )

        # Finalize the draft message with full content
        _gen_ms = int((_time.time() - _gen_start_t) * 1000)
        await db.update_message_content(
            draft_msg_id,
            content=full_text,
            content_blocks=json.dumps(content_blocks),
            turn_cost_usd=cost_usd,
            turn_input_tokens=input_tokens,
            turn_output_tokens=output_tokens,
            cc_session_id=new_session_id or None,
            cc_session_mode=cc_session_mode,
            cc_model_used=model_used_field,
            turn_lifecycle_state="completed" if recovering_partial and not failed_turn else None,
            model_attestation=(
                json.dumps(model_attestation, separators=(",", ":"))
                if model_attestation else None
            ),
            generation_ms=_gen_ms,
            time_to_first_token_ms=first_token_ms,
        )
        msg = await db.get_message(draft_msg_id)
        if not msg:
            print(
                f"[GEN] Draft {draft_msg_id} missing at finalize; "
                "creating recovery assistant message"
            )
            recovered = await db.add_message(
                conv_id,
                "assistant",
                full_text,
                parent_id=_describe_msg_id or parent_id,
                content_blocks=json.dumps(content_blocks),
                turn_cost_usd=cost_usd,
                turn_input_tokens=input_tokens,
                turn_output_tokens=output_tokens,
                cc_session_id=new_session_id or None,
            )
            draft_msg_id = recovered["id"]
            await db.update_message_content(
                draft_msg_id,
                cc_session_mode=cc_session_mode,
                cc_model_used=model_used_field,
                model_attestation=(
                    json.dumps(model_attestation, separators=(",", ":"))
                    if model_attestation else None
                ),
                generation_ms=_gen_ms,
            )
            await db.set_active_branch(conv_id, draft_msg_id)
            msg = await db.get_message(draft_msg_id)

        # Update conversation with session_id and cumulative cost
        old_cost = conv.get("total_cost_usd") or 0
        await db.update_conversation_fields(
            conv_id,
            claude_session_id=new_session_id,
            total_cost_usd=old_cost + cost_usd,
        )

        cost_info = {
            "cost_usd": cost_usd,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "duration_ms": duration_ms,
        }

        end_msg = {
            "type": "stream_end",
            "message": dict(msg),
            "cost": cost_info,
        }
        if failed_turn:
            await _ws_send(conv_id, {"type": "error", "error": _cc_result_error_detail(result_info) or "Provider exited before completing the turn", "turn_lifecycle_state": "partial_failed"})
        await _ws_send(conv_id, end_msg)
        # Notify all connected clients (cross-conversation bell)
        conv_title = conv.get("title", "Conversation")
        preview = (full_text or "").replace("#", "").replace("*", "").strip()[:120]
        await _ws_broadcast_all(
            {
                "type": "branch_landed",
                "conv_id": conv_id,
                "conv_title": conv_title,
                "message_id": draft_msg_id,
                "preview": preview,
            }
        )

    except asyncio.CancelledError:
        if _active_claude_procs.get(conv_id) is proc:
            _active_claude_procs.pop(conv_id, None)
        if proc:
            try:
                if is_gemini:
                    await gemini_client.cancel_gemini(proc)
                elif is_codex:
                    await codex_client.cancel_codex(proc)
                else:
                    await claude_client.cancel_claude(proc)
            except Exception:
                pass
        await _finalize_user_stopped_generation(
            conv_id,
            draft_msg_id,
            partial_text=full_text,
            content_blocks=content_blocks,
            session_id=new_session_id or resume_session_id or None,
            session_mode=cc_session_mode,
            model_used=(actual_model or cc_model) if (actual_model or cc_model) else None,
            model_attestation=model_attestation,
            generation_ms=int((_time.time() - _gen_start_t) * 1000),
            time_to_first_token_ms=first_token_ms,
        )
        raise
    except Exception as e:
        if _active_claude_procs.get(conv_id) is proc:
            _active_claude_procs.pop(conv_id, None)
        print(f"[GEN] Claude generation error conv={conv_id}: {e}")
        import traceback

        traceback.print_exc()
        if draft_msg_id:
            lifecycle = "partial_failed" if _cc_has_streamed_output(full_text, content_blocks) else "failed"
            asyncio.current_task()._generation_terminal_override = lifecycle
            await db.update_message_content(draft_msg_id, turn_lifecycle_state=lifecycle)
        # Save accumulated work to draft so it's not lost
        if draft_msg_id and (full_text or content_blocks):
            await db.update_message_content(
                draft_msg_id,
                content=full_text or "[Generation interrupted]",
                content_blocks=json.dumps(content_blocks) if content_blocks else None,
                cc_session_id=new_session_id or resume_session_id or None,
                cc_session_mode=cc_session_mode,
                cc_model_used=(actual_model or cc_model) if (actual_model or cc_model) else None,
                model_attestation=(
                    json.dumps(model_attestation, separators=(",", ":"))
                    if model_attestation else None
                ),
                time_to_first_token_ms=first_token_ms,
            )
            print(f"[GEN] Saved partial draft {draft_msg_id} on error")
        elif draft_msg_id:
            await db.update_message_content(
                draft_msg_id,
                content=f"[Error: {e}]",
                cc_session_id=new_session_id or resume_session_id or None,
                cc_session_mode=cc_session_mode,
                cc_model_used=(actual_model or cc_model) if (actual_model or cc_model) else None,
                model_attestation=(
                    json.dumps(model_attestation, separators=(",", ":"))
                    if model_attestation else None
                ),
                generation_ms=int((_time.time() - _gen_start_t) * 1000),
                time_to_first_token_ms=first_token_ms,
            )
        await _ws_send(conv_id, {"type": "error", "error": str(e)})
    finally:
        _gen_key = getattr(asyncio.current_task(), "_gen_key", None)
        if _gen_key:
            _active_generations.pop(_gen_key, None)
            _generation_snapshots.pop(_gen_key, None)
            _auto_approve_permissions.pop((conv_id, f"gen:{_gen_key[2]}"), None)
        await _send_generation_idle_if_quiescent(conv_id)
        # Drop tracking only after owned processes acknowledge exit. A terminal
        # result by itself is not proof that there is nothing left to reap.
        try:
            if 'draft_msg_id' in locals() and draft_msg_id and _generation_providers_exited():
                await db.unregister_active_generation(draft_msg_id)
        except Exception:
            pass
        # Clean up only this generation's pending permissions. A sibling agent
        # in the same conversation may still be waiting for its own decision.
        finished_scope = f"gen:{_gen_key[2]}" if _gen_key else "manual"
        await _resolve_pending_permissions_for_scope(
            conv_id,
            finished_scope,
            message=(
                "Loom permission request was cancelled because the owning "
                "Claude-compatible agent turn ended before a user decision."
            ),
        )


async def _prepare_agent_handoff(branch, conv, model, harness, *, project_dir=None, reason="handoff", frozen_tail=False):
    """Single fresh-context path for all ordinary agent harnesses.

    Specialized operators retain their explicit context/lockdown contract.
    Normal native resumes do not call this function.
    """
    if conv.get('backstage_parent_id'):
        return _build_claude_history_prompt(branch, project_dir) or "(continue)"
    from context_runtime import prepare_branch
    backend = None
    if harness == "goose":
        backend = goose_client.split_goose_model(model)[0]
    elif conv.get("_use_llama") or str(model).endswith(".gguf"):
        backend = "llama"
    elif harness == "openrouter-claude":
        backend = "openrouter"
    elif harness == "dream-claude":
        backend = "dream"

    async def status(text):
        task = asyncio.current_task()
        gen_key = getattr(task, "_gen_key", None)
        if gen_key:
            _update_gen_snapshot(gen_key, phase="context_preparation", status_text=text)
        await _ws_send(conv["id"], {"type": "generation_preparing", "text": text,
            "phase": "context_preparation",
            "parent_id": gen_key[1] if gen_key else None})

    workspace = str(project_dir or conv.get("project_dir") or ".")
    return await prepare_branch(branch, model=model, harness=harness, backend=backend,
                                reason=reason, status=status, frozen_tail=frozen_tail,
                                workspace=workspace)


def _build_claude_history_prompt(branch: list[dict], project_dir: Path = None) -> str:
    """Build a text prompt from conversation history (fallback when --resume unavailable).

    Also scans for attached files and includes references to them.

    If the branch contains one or more `[CC context compactified ...]` system
    markers, the history is truncated at the LAST such marker and the marker's
    body (which should contain the narrative summary) is injected as a
    preamble. This keeps replay prompts compact after a handoff.
    """
    history_parts = []
    # Collect all attached files from the conversation
    attached_files = []

    # Respect compact markers: treat the latest one as a "start here" point.
    compact_preamble = ""
    last_compact_idx = None
    for i, msg in enumerate(branch):
        if msg.get("role") != "system":
            continue
        content = (msg.get("content") or "").lstrip()
        if content.startswith("[CC context compactified"):
            last_compact_idx = i
    if last_compact_idx is not None:
        compact_preamble = (branch[last_compact_idx].get("content") or "").strip()
        branch = branch[last_compact_idx + 1 :]
    if compact_preamble:
        history_parts.append(compact_preamble)

    for msg in branch:
        if msg["role"] == "system":
            continue
        if msg["role"] == "user":
            # Check for attached files in image_path
            if msg.get("image_path"):
                img_paths = _parse_image_paths(msg["image_path"])
                for ip in img_paths:
                    if project_dir and Path(ip).is_file():
                        attached_files.append(ip)
            history_parts.append(f"Human: {msg['content']}")
        elif msg["role"] == "assistant":
            if msg.get("turn_lifecycle_state") in {"failed", "partial_failed", "interrupted"}:
                history_parts.append(f"Assistant: {partial_replay_text(msg)}")
                continue
            # The final assistant content is the portable provider handoff.
            # content_blocks contains the same answer plus potentially megabytes
            # of shell/MCP inputs and outputs. Replaying those blocks instead of
            # the final answer can inflate a modest branch beyond a fresh
            # harness's context window. Fall back to blocks only for historical
            # tool-only/partial turns that have no saved assistant text.
            assistant_content = str(msg.get("content") or "").strip()
            if assistant_content:
                history_parts.append(f"Assistant: {assistant_content}")
                continue
            blocks = None
            if msg.get("content_blocks"):
                try:
                    blocks = (
                        json.loads(msg["content_blocks"])
                        if isinstance(msg["content_blocks"], str)
                        else msg["content_blocks"]
                    )
                except (json.JSONDecodeError, TypeError):
                    blocks = None
            if blocks:
                parts = []
                for block in blocks:
                    if block.get("type") == "text" and block.get("text"):
                        parts.append(block["text"])
                    elif block.get("type") == "tool_use":
                        tool_summary = f"[Used tool: {block.get('name', 'unknown')}]"
                        if block.get("input"):
                            inp = (
                                block["input"][:2000]
                                if len(block.get("input", "")) > 2000
                                else block.get("input", "")
                            )
                            tool_summary += f"\nInput: {inp}"
                        if block.get("result"):
                            res = (
                                block["result"][:2000]
                                if len(block.get("result", "")) > 2000
                                else block.get("result", "")
                            )
                            tool_summary += f"\nResult: {res}"
                        parts.append(tool_summary)
                history_parts.append(f"Assistant: {chr(10).join(parts)}")
            else:
                history_parts.append("Assistant: [No saved assistant text]")

    if not history_parts:
        base = ""
    elif len(history_parts) > 1:
        history = "\n\n".join(history_parts[:-1])
        latest = history_parts[-1].removeprefix("Human: ")
        base = f"<conversation_history>\n{history}\n</conversation_history>\n\n{latest}"
    else:
        base = history_parts[0].removeprefix("Human: ")

    if attached_files:
        files_str = "\n".join(f"  • {Path(ip).name} (in attached_files/)" for ip in attached_files)
        return f"[Attached files:]\n{files_str}\n\n{base}" if base else f"[Attached files:]\n{files_str}"
    return base


def _local_replay_region(branch: list[dict]) -> str:
    """Identify only the renderer-owned history, never tags in current input."""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if getattr(task, "_context_plan", None) is not None:
        return getattr(task, "_context_replay_region", "")
    if not branch or branch[-1].get("role") != "user":
        return ""
    rendered = _build_claude_history_prompt(branch, None)
    suffix = "\n\n" + str(branch[-1].get("content") or "")
    if rendered.startswith("<conversation_history>\n") and rendered.endswith(suffix):
        region = rendered[:-len(suffix)]
        if region.endswith("\n</conversation_history>"):
            return region
    return ""


def _copy_latest_user_attachments_for_goose(
    branch: list[dict],
    project_dir: str | Path,
) -> tuple[list[Path], list[str]]:
    """Copy latest user attachments into project_dir/attached_files for Goose.

    Returns native image paths for ACP image content plus human-readable file notes.
    """
    last_user_msg = next((m for m in reversed(branch) if m.get("role") == "user"), None)
    if not last_user_msg or not last_user_msg.get("image_path"):
        return [], []

    root = Path(project_dir or ".")
    attached_dir = root / "attached_files"
    image_paths: list[Path] = []
    file_notes: list[str] = []

    for raw_path in _parse_image_paths(last_user_msg["image_path"]):
        src = Path(raw_path).expanduser()
        if not src.is_file():
            file_notes.append(str(src).replace("\\", "/"))
            continue

        dest = attached_dir / src.name
        copied = False
        try:
            attached_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dest))
            copied = True
        except Exception as exc:
            print(f"[Goose] Failed to copy attachment {src.name}: {exc}")

        usable_path = dest if copied else src
        if src.suffix.lower() in _IMAGE_EXTS:
            image_paths.append(usable_path)
        if copied:
            file_notes.append(f"{src.name} (in attached_files/)")
        else:
            file_notes.append(str(src.resolve()).replace("\\", "/"))

    return image_paths, file_notes


def _append_attachment_notes(prompt: str, file_notes: list[str]) -> str:
    if not file_notes:
        return prompt
    files_str = "\n".join(f"  • {note}" for note in file_notes)
    header = f"[User attached {len(file_notes)} file(s). See attached_files/ for copied files.]"
    return f"{prompt}\n\n{header}\n{files_str}" if prompt else f"{header}\n{files_str}"


async def _append_prior_acp_permission_feedback(
    prompt: str,
    branch: list[dict],
) -> str:
    """Add branch-local permission notes that ACP could not carry in-place."""
    latest_user = next(
        (message for message in reversed(branch) if message.get("role") == "user"),
        None,
    )
    prior_draft_id = (latest_user or {}).get("parent_id")
    if prior_draft_id is None:
        return prompt
    resolutions = await db.get_permission_resolutions_for_draft(int(prior_draft_id))
    acp_resolutions = [
        resolution
        for resolution in resolutions
        if str(resolution.get("source") or "") in {"goose_acp", "hermes_acp"}
    ]
    return add_permission_feedback_notice(prompt, acp_resolutions)


async def _handle_goose_generation(
    websocket: WebSocket, conv_id: int, conv: dict, data: dict
):
    """Handle Goose mode through ACP over stdio."""
    import time as _time

    if asyncio.current_task() is not None:
        asyncio.current_task()._context_plan = None
        asyncio.current_task()._context_replay_region = ""

    draft_msg_id = None
    full_text = ""
    content_blocks: list[dict] = []
    current_block: dict | None = None
    new_session_id = ""
    input_tokens = 0
    output_tokens = 0
    got_error = False
    last_error = ""
    last_error_stage = ""
    last_error_retryable = False
    result_info: dict = {}
    model_attestation: dict | None = None
    first_token_ms: int | None = None
    start_t = _time.time()
    proc = None

    def _goose_error_text(value, default: str = "Goose error") -> str:
        if isinstance(value, BaseException):
            if isinstance(value, (asyncio.TimeoutError, TimeoutError)) and not str(value).strip():
                text = "Goose operation timed out"
            else:
                text = str(value).strip() or value.__class__.__name__
        else:
            text = str(value or "").strip()
        return text or default

    try:
        action = data.get("action")
        parent_id = data.get("parent_id")
        if parent_id is None:
            raise ValueError("Goose generation requires an explicit parent message")

        project_dir = conv.get("project_dir") or "."
        if project_dir != "." and not os.path.isdir(project_dir):
            await _ws_send(conv_id, {"type": "error", "error": f"Working directory not found: {project_dir}"})
            return

        requested_model = data.get("cc_model") or conv.get("cc_model") or config.goose_model
        cc_model = requested_model
        if not goose_client.is_goose_model(cc_model):
            cc_model = _goose_selector_for_model(cc_model)
            if not cc_model:
                await _ws_send(conv_id, {
                    "type": "error",
                    "provider": "goose",
                    "stage": "model_resolution",
                    "requested_model": requested_model,
                    "error": (
                        f"Goose model resolution failed for '{requested_model}': "
                        "the selector is not a Goose, OpenRouter, loaded GGUF, or Dream model"
                    ),
                })
                return
        cc_permission_mode = data.get("cc_permission_mode") or conv.get("cc_permission_mode") or config.goose_mode or "approve"
        cc_permission_mode = goose_client.permission_mode_for_model(cc_model, cc_permission_mode)
        cc_effort = data.get("cc_effort") or conv.get("cc_effort") or "high"
        settings_updates = {}
        if cc_model != conv.get("cc_model"):
            settings_updates["cc_model"] = cc_model
        if cc_effort != conv.get("cc_effort"):
            settings_updates["cc_effort"] = cc_effort
        if cc_permission_mode != conv.get("cc_permission_mode"):
            settings_updates["cc_permission_mode"] = cc_permission_mode
        if settings_updates and not _truthy_setting(conv.get("parallel_agents_enabled")):
            await db.update_conversation_fields(conv_id, **settings_updates)
        pass

        branch = await db.get_branch_to_root(parent_id) if parent_id else []
        resume_session_id = None
        use_resume = False
        resume_decision = select_resume_session(
            branch,
            "goose",
            target_model=cc_model,
            models_match=lambda recorded, target: (
                goose_client.model_label(recorded) == goose_client.model_label(target)
            ),
        )
        from context_runtime import respect_manual_boundary
        resume_decision = await respect_manual_boundary(branch, resume_decision)
        resume_session_id = resume_decision.session_id
        use_resume = resume_decision.can_resume
        if not use_resume:
            print(
                f"[GOOSE] Native session rebuild: reason={resume_decision.reason} "
                f"boundary_msg={resume_decision.message_id}"
            )

        session_continuity = {
            "status": "forked" if use_resume else "rebuilt",
            "reason": resume_decision.reason,
            "source_session_id": resume_session_id if use_resume else None,
        }

        if use_resume:
            latest_user = next((m for m in reversed(branch) if m.get("role") == "user"), None)
            prompt = (latest_user or {}).get("content") or "(continue)"
        else:
            prompt = await _prepare_agent_handoff(branch, conv, cc_model, "goose")

        prompt = await _append_prior_acp_permission_feedback(prompt, branch)

        if use_resume:
            from context_runtime import renew_native_retrieval
            prompt = await renew_native_retrieval(prompt, branch)
        prompt = add_continuity_notice(
            prompt,
            resume_decision,
            branch,
            "goose",
            target_model=cc_model,
        )

        goose_image_paths, goose_file_notes = _copy_latest_user_attachments_for_goose(branch, project_dir)
        prompt = _append_attachment_notes(prompt, goose_file_notes)

        draft_msg = await _create_generation_draft(conv_id, conv, parent_id)
        draft_msg_id = draft_msg["id"]
        await db.update_message_content(draft_msg_id, cc_model_used=cc_model)
        await db.set_active_branch(conv_id, draft_msg_id)

        _gen_key_local = getattr(asyncio.current_task(), "_gen_key", None)
        if _gen_key_local:
            _update_gen_snapshot(
                _gen_key_local,
                full_text="",
                content_blocks=[],
                input_tokens=0,
                output_tokens=0,
                started_at=_time.time(),
                draft_msg_id=draft_msg_id,
                parent_id=parent_id,
                cc_model=cc_model,
                mode="goose",
            )

        await _ws_send(
            conv_id,
            {
                "type": "status",
                "text": f"Launching Goose ACP ({goose_client.model_label(cc_model)})"
                        + (" (forking session)" if use_resume else " (building history)"),
                "parent_id": parent_id,
            },
        )
        await _ws_send(
            conv_id,
            {
                "type": "stream_start",
                "parent_id": parent_id,
                "draft_msg_id": draft_msg_id,
                "cc_model": cc_model,
                "session_continuity": session_continuity,
            },
        )

        async def _register_goose_proc() -> None:
            if proc is None:
                return
            _active_goose_procs[conv_id] = proc
            _track_generation_provider(proc)
            try:
                await db.register_active_generation(
                    draft_msg_id=draft_msg_id,
                    conv_id=conv_id,
                    pid=getattr(proc, "pid", 0) or 0,
                    project_dir=project_dir,
                    mode="goose",
                )
            except Exception:
                pass

        async def _retire_goose_proc() -> None:
            nonlocal proc
            old_proc = proc
            if _active_goose_procs.get(conv_id) is old_proc:
                _active_goose_procs.pop(conv_id, None)
            proc = None
            if old_proc is None:
                return
            try:
                await asyncio.wait_for(old_proc.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                try:
                    await goose_client.cancel_goose(old_proc)
                except Exception:
                    pass
            except Exception:
                pass

        async def _start_goose(
            start_prompt: str,
            *,
            start_resume_session_id: str | None = None,
            start_fork_session: bool = False,
        ):
            nonlocal proc
            start_prompt = add_canvas_notice(start_prompt, conv)
            proc, start_stream = await goose_client.run_goose(
                start_prompt,
                conv_id=conv_id,
                model=cc_model,
                cwd=project_dir,
                loom_port=config.port,
                goose_exe=config.goose_exe or None,
                permission_mode=cc_permission_mode,
                branch=branch,
                context_replay_history=_local_replay_region(branch),
                resume_session_id=start_resume_session_id,
                fork_session=start_fork_session,
                
                builtins=config.goose_builtins,
                effort=cc_effort,
                image_paths=goose_image_paths,
                is_first_turn=not bool(start_resume_session_id),
                progress_timeout_seconds=config.goose_progress_timeout_seconds,
            )
            await _register_goose_proc()
            return start_stream

        async def _mark_goose_first_output(event_type: str) -> None:
            nonlocal first_token_ms
            if first_token_ms is not None or event_type not in {
                "text_delta", "thinking_delta", "tool_start", "tool_result"
            }:
                return
            first_token_ms = int((_time.time() - start_t) * 1000)
            await db.update_message_content(
                draft_msg_id, time_to_first_token_ms=first_token_ms
            )
            if _gen_key_local:
                _update_gen_snapshot(
                    _gen_key_local, time_to_first_token_ms=first_token_ms
                )
            await _ws_send(
                conv_id,
                {"type": "first_token", "elapsed_ms": first_token_ms},
            )

        async def _consume_goose_stream(event_stream, recovery_event_prefix="") -> None:
            nonlocal full_text, content_blocks, current_block
            nonlocal new_session_id, input_tokens, output_tokens
            nonlocal got_error, last_error, result_info
            nonlocal last_error_stage, last_error_retryable
            nonlocal model_attestation

            async for evt in turn_recovery.events(event_stream):
                evt = turn_recovery.namespace_event(evt, recovery_event_prefix)
                etype = evt.get("type")
                if etype == "session_info":
                    new_session_id = evt.get("session_id") or new_session_id
                    attestation = evt.get("model_attestation")
                    if isinstance(attestation, dict):
                        attestation = {
                            **attestation,
                            "session_continuity": session_continuity["status"],
                            "session_continuity_reason": session_continuity["reason"],
                            "source_session_id": session_continuity.get("source_session_id"),
                        }
                        recorded = await _record_turn_model_attestation(
                            conv_id,
                            draft_msg_id,
                            parent_id,
                            attestation,
                            _gen_key_local,
                        )
                        if recorded:
                            model_attestation = recorded
                            await _ws_send(
                                conv_id,
                                {
                                    "type": "status",
                                    "text": _model_attestation_status_text(recorded),
                                    "parent_id": parent_id,
                                },
                            )
                    if model_attestation and model_attestation.get("status") == "mismatch":
                        raise RuntimeError(
                            f"Goose model mismatch: requested '{cc_model}', ACP reported "
                            f"'{model_attestation.get('effective_model')}'"
                        )
                    try:
                        await db.update_active_generation_session(draft_msg_id, new_session_id)
                    except Exception:
                        pass
                elif etype == "status":
                    await _ws_send(conv_id, {"type": "status", "text": evt.get("text", ""), "parent_id": parent_id})
                elif etype == "text_delta":
                    text = evt.get("text", "")
                    if not text:
                        continue
                    await _mark_goose_first_output(etype)
                    full_text += text
                    if current_block and current_block.get("type") == "text":
                        current_block["text"] += text
                    else:
                        current_block = {"type": "text", "text": text}
                        content_blocks.append(current_block)
                    await _ws_send(conv_id, {"type": "stream_chunk", "content": text})
                elif etype == "thinking_delta":
                    text = evt.get("text", "")
                    if text:
                        await _mark_goose_first_output(etype)
                        if current_block and current_block.get("type") == "thinking":
                            current_block["text"] += text
                        else:
                            current_block = {"type": "thinking", "text": text}
                            content_blocks.append(current_block)
                        await _ws_send(conv_id, {"type": "thinking_chunk", "content": text})
                elif etype == "tool_start":
                    await _mark_goose_first_output(etype)
                    current_block = {
                        "type": "tool_use",
                        "name": evt.get("name", "goose_tool"),
                        "tool_id": evt.get("tool_id", ""),
                        "input": "",
                        "result": "",
                    }
                    content_blocks.append(current_block)
                    await _ws_send(conv_id, {"type": "tool_start", "name": current_block["name"], "tool_id": current_block["tool_id"]})
                elif etype == "tool_input_delta":
                    if current_block and current_block.get("type") == "tool_use":
                        current_block["input"] += evt.get("json", "")
                    await _ws_send(conv_id, {"type": "tool_input_chunk", "content": evt.get("json", ""), "tool_id": evt.get("tool_id", "")})
                elif etype == "tool_result":
                    await _mark_goose_first_output(etype)
                    tool_id = evt.get("tool_id", "")
                    result = evt.get("content", "")
                    for block in reversed(content_blocks):
                        if block.get("type") == "tool_use" and block.get("tool_id") == tool_id:
                            block["result"] = result
                            block["outcome_recorded"] = evt.get("outcome_recorded", True) is True
                            block["is_error"] = bool(evt.get("is_error"))
                            break
                    current_block = None
                    await _ws_send(conv_id, {"type": "tool_result", "content": result, "tool_id": tool_id, "is_error": evt.get("is_error", False)})
                elif etype == "usage":
                    input_tokens += int(evt.get("input_tokens") or 0)
                    output_tokens += int(evt.get("output_tokens") or 0)
                    await db.update_message_content(draft_msg_id, turn_input_tokens=input_tokens,
                                                    turn_output_tokens=output_tokens)
                    await _ws_send(conv_id, {"type": "usage", "input_tokens": input_tokens, "output_tokens": output_tokens})
                elif etype == "permission_request":
                    pass
                elif etype == "error":
                    got_error = True
                    last_error = _goose_error_text(evt.get("error") or evt.get("message"), "")
                    last_error_stage = str(evt.get("stage") or "generation")
                    last_error_retryable = bool(evt.get("retryable"))
                elif etype == "result":
                    result_info = evt
                    new_session_id = evt.get("session_id") or new_session_id
                    stop_reason = str(evt.get("stop_reason") or "").strip().lower()
                    if evt.get("is_error") or stop_reason in {"error", "failed", "incomplete"}:
                        got_error = True
                        if not last_error:
                            last_error = _goose_error_text(
                                _cc_result_error_detail(evt),
                                f"Goose ended with {stop_reason}",
                            )

                if _gen_key_local:
                    _update_gen_snapshot(
                        _gen_key_local,
                        full_text=full_text,
                        content_blocks=content_blocks,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    )

        try:
            event_stream = await _start_goose(
                prompt,
                start_resume_session_id=resume_session_id if use_resume else None,
                start_fork_session=use_resume,
            )
            await _consume_goose_stream(event_stream)
        except Exception as exc:
            if not use_resume and not context_exhausted(exc):
                raise
            got_error = True
            last_error = _goose_error_text(exc)
            last_error_stage = "generation"
            last_error_retryable = goose_client.is_retryable_goose_failure(last_error)

        # ACP may wrap a pre-inference context rejection in assistant text.
        # Normalize that exact terminal envelope before deciding whether real
        # output occurred. Never erase partial work or tool activity to retry.
        clean_text, wrapped_error = goose_client.split_goose_terminal_failure(full_text)
        if wrapped_error:
            full_text = clean_text
            last_error = wrapped_error
            got_error = True
            cleaned_blocks = []
            for block in content_blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    clean, wrapped = goose_client.split_goose_terminal_failure(block.get("text") or "")
                    if wrapped:
                        if clean:
                            cleaned_blocks.append({**block, "text": clean})
                        continue
                cleaned_blocks.append(block)
            content_blocks = cleaned_blocks
        streamed_before_failure = _cc_has_streamed_output(full_text, content_blocks)
        empty_resume = use_resume and bool(result_info) and not got_error and not streamed_before_failure
        safe_resume_failure = use_resume and got_error and (resume_rejected(last_error) or context_exhausted(last_error)) and not streamed_before_failure
        recovery = turn_recovery.decide(last_error if got_error else "", content_blocks,
            attempts=getattr(asyncio.current_task(), "_context_recovery_attempts", 0),
            enabled=not conv.get('backstage_parent_id'))
        recovering_partial = recovery.allowed and streamed_before_failure
        if recovery.reason == "unresolved_tool_outcome":
            last_error += " Automatic recovery paused: a tool outcome is unresolved or reported an error. Retained evidence requires reconciliation before repeating actions."
        if recovery.allowed or (use_resume and (safe_resume_failure or empty_resume)):
            session_continuity = {
                "status": "rebuilt",
                "reason": (
                    "native_resume_empty_response"
                    if empty_resume else "native_resume_runtime_error"
                ),
                "source_session_id": resume_session_id,
            }
            if recovery.allowed:
                session_continuity["reason"] = recovery.reason
                session_continuity["source_session_id"] = new_session_id or resume_session_id
            if empty_resume:
                status_text = "Goose session returned no response - retrying with full history..."
            else:
                status_text = "Goose session failed - rebuilding from history..."
            if recovering_partial:
                status_text = "Context exhausted — preserving recorded outcomes and preparing continuation..."
            await _ws_send(conv_id, {"type": "status", "text": status_text, "parent_id": parent_id})
            if proc is not None:
                await _teardown_child_provider(proc, "goose")
            await _retire_goose_proc()
            if recovering_partial:
                await db.update_message_content(draft_msg_id, content=full_text, content_blocks=json.dumps(content_blocks),
                    cc_session_id=new_session_id or None, cc_session_mode="goose", cc_model_used=cc_model,
                    turn_lifecycle_state="partial_failed")
                branch = await db.get_branch_to_root(draft_msg_id)
            else:
                full_text = ""
                content_blocks = []
            current_block = None
            new_session_id = ""
            if not recovering_partial:
                input_tokens = 0
                output_tokens = 0
            got_error = False
            last_error = ""
            last_error_stage = ""
            last_error_retryable = False
            result_info = {}
            if _gen_key_local:
                _update_gen_snapshot(
                    _gen_key_local,
                    full_text=full_text,
                    content_blocks=content_blocks,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cc_model=cc_model,
                    mode="goose",
                )
            prompt = await _prepare_agent_handoff(branch, conv, cc_model, "goose",
                                                  reason=session_continuity["reason"], frozen_tail=recovering_partial)
            prompt = await _append_prior_acp_permission_feedback(prompt, branch)
            prompt = add_continuity_notice(
                prompt,
                ResumeDecision(
                    None,
                    session_continuity["reason"],
                    resume_decision.message_id,
                ),
                branch,
                "goose",
                target_model=cc_model,
            )
            prompt = _append_attachment_notes(prompt, goose_file_notes)
            recovery_event_prefix = ""
            if recovery.allowed:
                recovery_event_prefix = turn_recovery.claim(recovery, asyncio.current_task(), proc)
            event_stream = await _start_goose(prompt)
            await _consume_goose_stream(event_stream, recovery_event_prefix)

        # Goose v41 reports several provider failures as an ordinary assistant
        # message ending in its canonical retry footer. Remove that envelope
        # from the useful partial text and treat it as terminal evidence.
        clean_text, wrapped_error = goose_client.split_goose_terminal_failure(full_text)
        if wrapped_error:
            full_text = clean_text
            last_error = wrapped_error
            got_error = True
            last_error_stage = last_error_stage or "provider_response"
            last_error_retryable = (
                last_error_retryable
                or goose_client.is_retryable_goose_failure(wrapped_error)
            )
            for index in range(len(content_blocks) - 1, -1, -1):
                block = content_blocks[index]
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                clean_block, block_error = goose_client.split_goose_terminal_failure(
                    block.get("text") or ""
                )
                if block_error:
                    if clean_block:
                        block["text"] = clean_block
                    else:
                        content_blocks.pop(index)
                    break

        if not full_text.strip() and not got_error:
            got_error = True
            last_error = (
                _goose_error_text(_cc_result_error_detail(result_info), "")
                or "Goose ended without producing a final response"
            )

        if got_error:
            error_msg = _goose_error_text(last_error, "Goose generation failed")
            had_partial = _cc_has_streamed_output(full_text, content_blocks)
            content = (
                _append_provider_failure_note(
                    full_text,
                    content_blocks,
                    "Goose",
                    error_msg,
                )
                if had_partial
                else f"[Error: {error_msg}]"
            )
            lifecycle = "partial_failed" if had_partial else "failed"
            gen_ms = int((_time.time() - start_t) * 1000)
            model_attestation = _failed_session_attestation(
                model_attestation, result_info, new_session_id, "goose", error_msg,
            )
            await db.update_message_content(
                draft_msg_id,
                content=content,
                content_blocks=json.dumps(content_blocks) if content_blocks else None,
                turn_input_tokens=input_tokens,
                turn_output_tokens=output_tokens,
                cc_session_id=new_session_id or None,
                cc_session_mode="goose",
                cc_model_used=cc_model,
                generation_ms=gen_ms,
                time_to_first_token_ms=first_token_ms,
                turn_lifecycle_state=lifecycle,
                model_attestation=json.dumps(model_attestation),
            )
            await db.set_active_branch(conv_id, draft_msg_id)
            msg = await db.get_message(draft_msg_id)
            task = asyncio.current_task()
            if task is not None:
                task._generation_terminal_override = lifecycle
            await _ws_send(conv_id, {
                "type": "error",
                "provider": "goose",
                "stage": last_error_stage or "generation",
                "requested_model": cc_model,
                "error": error_msg,
                "retryable": last_error_retryable,
                "lifecycle": lifecycle,
                "message_id": draft_msg_id,
                "draft_msg_id": draft_msg_id,
                "message": dict(msg) if msg else None,
            })
            return

        gen_ms = int((_time.time() - start_t) * 1000)
        await db.update_message_content(
            draft_msg_id,
            content=full_text,
            content_blocks=json.dumps(content_blocks) if content_blocks else None,
            turn_input_tokens=input_tokens,
            turn_output_tokens=output_tokens,
            cc_session_id=new_session_id or None,
            cc_session_mode="goose",
            cc_model_used=cc_model,
            turn_lifecycle_state="completed" if recovering_partial else None,
            generation_ms=gen_ms,
            time_to_first_token_ms=first_token_ms,
        )
        await db.set_active_branch(conv_id, draft_msg_id)
        msg = await db.get_message(draft_msg_id)
        await _ws_send(conv_id, {"type": "stream_end", "message": dict(msg)})
        preview = full_text.replace("#", "").replace("*", "").strip()[:120]
        await _ws_broadcast_all({
            "type": "branch_landed",
            "conv_id": conv_id,
            "conv_title": conv.get("title", "Conversation"),
            "message_id": draft_msg_id,
            "preview": preview,
        })

    except asyncio.CancelledError:
        if proc is not None:
            try:
                await goose_client.cancel_goose(proc)
            except Exception:
                pass
        await _finalize_user_stopped_generation(
            conv_id,
            draft_msg_id,
            partial_text=full_text,
            content_blocks=content_blocks,
            session_id=new_session_id or resume_session_id or None,
            session_mode="goose",
            model_used=cc_model if "cc_model" in locals() else None,
            model_attestation=model_attestation,
            generation_ms=int((_time.time() - start_t) * 1000),
            time_to_first_token_ms=first_token_ms,
        )
        raise
    except Exception as exc:
        error_msg = _goose_error_text(exc)
        print(f"[Goose] Error: {error_msg}")
        saved_msg = None
        if draft_msg_id:
            had_partial = _cc_has_streamed_output(full_text, content_blocks)
            failed_content = (
                _append_provider_failure_note(
                    full_text,
                    content_blocks,
                    "Goose",
                    error_msg,
                )
                if had_partial
                else f"[Error: {error_msg}]"
            )
            lifecycle = "partial_failed" if had_partial else "failed"
            await db.update_message_content(
                draft_msg_id,
                content=failed_content,
                content_blocks=json.dumps(content_blocks) if content_blocks else None,
                cc_session_id=new_session_id or None,
                cc_session_mode="goose",
                cc_model_used=locals().get("cc_model"),
                generation_ms=int((_time.time() - start_t) * 1000),
                time_to_first_token_ms=first_token_ms,
                turn_lifecycle_state=lifecycle,
            )
            saved_msg = await db.get_message(draft_msg_id)
            task = asyncio.current_task()
            if task is not None:
                task._generation_terminal_override = lifecycle
        await _ws_send(conv_id, {
            "type": "error",
            "provider": "goose",
            "stage": "launch_or_stream",
            "requested_model": locals().get("cc_model") or locals().get("requested_model"),
            "error": error_msg,
            "retryable": goose_client.is_retryable_goose_failure(error_msg),
            "lifecycle": (
                (saved_msg or {}).get("turn_lifecycle_state") if saved_msg else "failed"
            ),
            "message_id": draft_msg_id,
            "draft_msg_id": draft_msg_id,
            "message": dict(saved_msg) if saved_msg else None,
        })
    finally:
        _gen_key = getattr(asyncio.current_task(), "_gen_key", None)
        if _gen_key:
            _active_generations.pop(_gen_key, None)
            _generation_snapshots.pop(_gen_key, None)
        if _active_goose_procs.get(conv_id) is proc:
            _active_goose_procs.pop(conv_id, None)
        if proc is not None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                try:
                    await goose_client.cancel_goose(proc)
                except Exception:
                    pass
            except Exception:
                pass
        if draft_msg_id:
            try:
                if _generation_providers_exited():
                    await db.unregister_active_generation(draft_msg_id)
            except Exception:
                pass
        await _send_generation_idle_if_quiescent(conv_id)
        finished_scope = f"gen:{_gen_key[2]}" if _gen_key else "manual"
        await _resolve_pending_permissions_for_scope(
            conv_id,
            finished_scope,
            message=(
                "Loom permission request was cancelled because the owning "
                "Goose turn ended before a user decision."
            ),
        )


async def _handle_local_generation(
    websocket: WebSocket, conv_id: int, conv: dict, data: dict
):
    """Handle Local mode: Claude Code launched against llama-server."""
    print(f"[LOCAL-GEN] Starting: conv_id={conv_id}, local_model={conv.get('local_model')}, action={data.get('action')}, parent_id={data.get('parent_id')}")
    # Local mode = Claude Code powered by a local model. Standard .gguf models
    # route through llama-server; the explicit Dream model routes through the
    # isolated Anthropic->Dream shim.
    conv = dict(conv)  # mutable copy
    # Map local_model into cc_model so _handle_claude_generation uses it
    local_model = conv.get("local_model") or config.llama_model
    if is_dream_claude_model(local_model):
        conv["cc_model"] = f"dream:{_dream_claude_model_id(local_model)}"
        conv["_use_llama"] = False
    else:
        conv["cc_model"] = local_model
        conv["_use_llama"] = True
    await _handle_claude_generation(websocket, conv_id, conv, data)


async def _handle_umans_generation(
    websocket: WebSocket, conv_id: int, conv: dict, data: dict
):
    """Handle Umans mode: Claude Code launched against api.code.umans.ai."""
    print(f"[UMANS-GEN] Starting: conv_id={conv_id}, cc_model={conv.get('cc_model')}, action={data.get('action')}, parent_id={data.get('parent_id')}")
    # Umans mode = Claude Code powered by Umans AI remote models.
    # Reuse the full CC handler but with use_umans=True.
    conv = dict(conv)  # mutable copy
    conv["cc_model"] = conv.get("cc_model") or config.umans_model or "umans-coder"
    conv["_use_umans"] = True
    await _handle_claude_generation(websocket, conv_id, conv, data)


async def _handle_dream_completion(
    websocket: WebSocket, conv_id: int, conv: dict, data: dict
):
    """Dream Engine generation — one-shot OpenAI completion. (DEPRECATED for Weave)

    Used for Weave conversations that pick the Dream Engine model. No agent
    loop, no tools, no ACP session — just direct diffusion generation via
    dream_client.

    DEPRECATED: dream-model weave convs now flow through _handle_ooda_generation
    / _handle_weave_generation, which build the full structured prompt via
    prompt_engine and rely on llama_client's transparent dream-host routing.
    This handler is retained for reference and potential explicit invocation
    but is no longer reached from _handle_generation. It also relies on
    _build_claude_history_prompt which strips character/system/style/state
    context — the very limitation the new routing fixes.
    """
    import time as _time
    import dream_client as _dc
    draft_msg_id = None
    full_text = ""
    content_blocks: list[dict] = []
    start_t = _time.time()
    try:
        action = data.get("action")
        parent_id = data.get("parent_id")
        if parent_id is None:
            raise ValueError("Dream generation requires an explicit parent message")

        project_dir = conv.get("project_dir") or "."
        branch = await db.get_branch_to_root(parent_id) if parent_id else []
        prompt = _build_claude_history_prompt(branch, project_dir) or "(continue)"
        prompt = await _append_prior_acp_permission_feedback(prompt, branch)
        model = conv.get("local_model") or config.dream_model or None

        draft_msg = await _create_generation_draft(conv_id, conv, parent_id)
        draft_msg_id = draft_msg["id"]
        await _ws_send(conv_id, {"type": "stream_start", "parent_id": parent_id,
                                 "draft_msg_id": draft_msg_id,
                                 "local_model": model})
        await _ws_send(conv_id, {"type": "status", "text": "Dream Engine generating (canvas denoising)…"})

        res = await _dc.dream_chat_sync(
            [{"role": "user", "content": prompt}],
            model=model,
            host=config.dream_host,
            max_tokens=data.get("max_tokens") or config.max_tokens,
        )
        reasoning = res.get("reasoning_content") or ""
        if reasoning:
            content_blocks.append({"type": "thinking", "text": reasoning})
            await _ws_send(conv_id, {"type": "thinking_chunk", "content": reasoning})

        content = res.get("content") or ""
        if content:
            full_text = content
            content_blocks.append({"type": "text", "text": content})
            await _ws_send(conv_id, {"type": "text_delta", "draft_msg_id": draft_msg_id,
                                     "delta": content})

        usage = res.get("usage") or {}
        in_tok = usage.get("prompt_tokens", 0)
        out_tok = usage.get("completion_tokens", 0)
        if in_tok or out_tok:
            await _ws_send(conv_id, {"type": "usage",
                                    "input_tokens": in_tok, "output_tokens": out_tok})
            await _ws_send(conv_id, {"type": "context_info",
                                     "total_tokens": in_tok + out_tok})

        if not full_text.strip() and not content_blocks:
            if draft_msg_id:
                await db.delete_branch(draft_msg_id)
            await _ws_send(conv_id, {"type": "error",
                                     "error": "Dream returned an empty response — try again"})
            return

        gen_ms = int((_time.time() - start_t) * 1000)
        await db.update_message_content(
            draft_msg_id, content=full_text,
            content_blocks=json.dumps(content_blocks) if content_blocks else None,
            cc_session_id=None,
            cc_model_used=f"dream:{model}" if model else "dream:default",
            generation_ms=gen_ms,
        )
        await db.set_active_branch(conv_id, draft_msg_id)
        msg = await db.get_message(draft_msg_id)
        await _ws_send(conv_id, {"type": "stream_end", "message": dict(msg)})
        preview = full_text.replace("#", "").replace("*", "").strip()[:120]
        await _ws_broadcast_all({"type": "branch_landed", "conv_id": conv_id,
                                 "conv_title": conv.get("title", "Conversation"),
                                 "message_id": draft_msg_id, "preview": preview})

    except asyncio.CancelledError:
        await _finalize_user_stopped_generation(
            conv_id,
            draft_msg_id,
            partial_text=full_text,
            content_blocks=content_blocks,
            model_used=f"dream:{model}" if "model" in locals() and model else "dream:default",
            generation_ms=int((_time.time() - start_t) * 1000),
        )
        raise
    except Exception as e:
        if draft_msg_id and (full_text.strip() or content_blocks):
            try:
                await db.update_message_content(
                    draft_msg_id,
                    content=full_text,
                    content_blocks=json.dumps(content_blocks) if content_blocks else None,
                )
            except Exception:
                pass
        elif draft_msg_id:
            await db.delete_branch(draft_msg_id)
        print(f"[Dream] completion error conv={conv_id}: {e}")
        await _ws_send(conv_id, {"type": "error", "error": str(e)})
    finally:
        _gen_key = getattr(asyncio.current_task(), "_gen_key", None)
        if _gen_key:
            _active_generations.pop(_gen_key, None)
            _generation_snapshots.pop(_gen_key, None)
        await _send_generation_idle_if_quiescent(conv_id)


async def _handle_ooda_generation(
    websocket: WebSocket, conv_id: int, conv: dict, data: dict
):
    """Handle OODA-enhanced Weave generation — single-pass with repair fallback."""
    import re as _re

    draft_msg_id = None
    final_prose = ""
    first_response_ms = None
    try:
        action = data.get("action")
        parent_id = data.get("parent_id")
        print(
            f"[OODA] Starting ooda_generation conv={conv_id} action={action} parent={parent_id}"
        )

        if parent_id is None:
            raise ValueError("OODA generation requires an explicit parent message")

        # ── Setup (same as weave) ──
        character = None
        if conv and conv.get("character_id"):
            char_path = os.path.join(
                config.characters_dir, f"{conv['character_id']}.md"
            )
            character = load_character(char_path)

        style_nudge_name = conv.get("style_nudge", "Natural") if conv else "Natural"
        nudge_index = 0
        for i, nudge in enumerate(STYLE_NUDGES):
            if nudge["name"] == style_nudge_name:
                nudge_index = i
                break

        persona = None
        if conv and conv.get("persona_id"):
            persona = load_persona(os.path.join("personas", f"{conv['persona_id']}.md"))

        lore_entries = []
        if conv and conv.get("lore_ids"):
            try:
                lore_ids = (
                    json.loads(conv["lore_ids"])
                    if isinstance(conv["lore_ids"], str)
                    else conv["lore_ids"]
                )
            except (ValueError, TypeError):
                lore_ids = []
            for lid in lore_ids:
                entry = load_lore_entry(os.path.join("lore", f"{lid}.md"))
                if entry:
                    lore_entries.append(entry)

        context = await get_context_for_generation(conv_id, character, leaf_id=parent_id)
        if action == "regenerate" and parent_id is not None:
            context["verbatim_messages"] = [
                m for m in context["verbatim_messages"] if m["id"] <= parent_id
            ]

        custom_scene = conv.get("custom_scene") if conv else None
        base_system = build_system_prompt(
            character=character,
            style_nudge_index=nudge_index,
            scenario_override=custom_scene,
        )

        # ── OODA enhancement: build system prompt with branch-aware state ──
        if parent_id:
            state_cards = await db.get_branch_state(conv_id, parent_id)
        else:
            state_cards = await db.get_state_cards(conv_id)
        global_cards = (
            await db.get_character_state_cards(conv.get("character_id", ""))
            if conv.get("character_id")
            else []
        )
        ooda_system = build_ooda_system_prompt(
            base_system, state_cards, global_cards=global_cards
        )

        example_msgs = character.get("example_messages", []) if character else []
        messages = assemble_prompt(
            system_prompt=ooda_system,
            example_messages=example_msgs,
            summary=context.get("summary"),
            conversation_messages=context["verbatim_messages"],
            persona=persona,
            lore_entries=lore_entries,
        )

        actual_tokens = sum(len(m["content"]) // 3 for m in messages)
        active_nudge = get_style_nudge(nudge_index)
        context_payload = {
            "type": "context_info",
            "total_tokens": actual_tokens,
            "was_compactified": context["was_compactified"],
            "style_nudge": active_nudge["name"],
            "parent_id": parent_id,
        }
        if context["was_compactified"]:
            context_payload["total_messages"] = context.get("total_messages")
            context_payload["verbatim_count"] = context.get("verbatim_count")
            context_payload["summarized_count"] = context.get("summarized_count")
            context_payload["summary_text"] = context.get("summary")
            # Auto-branch: insert a system marker at the compaction point
            summ_count = context.get("summarized_count", "?")
            verb_count = context.get("verbatim_count", "?")
            marker = await db.add_message(
                conv_id, "system",
                f"[Context compactified — {summ_count} messages summarized, {verb_count} sent verbatim]",
                parent_id=parent_id,
            )
            parent_id = marker["id"]
            context_payload["compaction_marker_id"] = marker["id"]
        await _ws_send(conv_id, context_payload)

        # Create draft message in DB so it appears as ghost node on tree.
        draft_msg = await _create_generation_draft(conv_id, conv, parent_id)
        draft_msg_id = draft_msg["id"]
        weave_model = conv.get("local_model") or config.llama_model or ""
        await db.update_message_content(draft_msg_id, cc_model_used=weave_model)
        _gen_key_local = getattr(asyncio.current_task(), "_gen_key", None)
        if _gen_key_local:
            _update_gen_snapshot(
                _gen_key_local,
                full_text="",
                content_blocks=[],
                started_at=_time.time(),
                draft_msg_id=draft_msg_id,
                parent_id=parent_id,
                cc_model=weave_model,
                mode="weave-ooda",
            )

        print(
            f"[OODA] System prompt: {len(ooda_system)} chars, {len(messages)} messages, {len(state_cards)} state cards"
        )
        await _ws_send(
            conv_id,
            {
                "type": "stream_start",
                "parent_id": parent_id,
                "draft_msg_id": draft_msg_id,
                "local_model": weave_model,
            },
        )
        _ooda_start_t = _time.time()

        # ── OODA pass (single-shot: model emits <ooda> block + prose together) ──
        print(f"[OODA] Generating OODA block + prose...")
        await _ws_send(
            conv_id,
            {
                "type": "status",
                "text": "OODA: Observing and orienting...",
                "parent_id": parent_id,
            },
        )
        weave_model = weave_model or None
        ooda_max_tokens = int(getattr(config, "weave_max_tokens", None) or getattr(config, "max_tokens", 2048) or 2048)
        raw_pass1, usage_pass1 = await sync_chat(
            messages,
            max_tokens=ooda_max_tokens,
            think=False,
            model=weave_model,
            return_usage=True,
        )
        if raw_pass1:
            # Non-streaming OODA can measure response arrival, not internal TTFT.
            first_response_ms = await _record_first_response(
                conv_id, draft_msg_id, _gen_key_local, _ooda_start_t, first_response_ms,
            )
        await _record_turn_model_attestation(
            conv_id,
            draft_msg_id,
            parent_id,
            (usage_pass1 or {}).get("model_attestation"),
            _gen_key_local,
        )
        # Check if cancelled during the sync call
        if asyncio.current_task().cancelled():
            raise asyncio.CancelledError()
        cleaned_pass1 = _re.sub(r"<think>[\s\S]*?</think>", "", raw_pass1)
        cleaned_pass1 = _re.sub(r"<think>[\s\S]*?</think>\s*", "", cleaned_pass1).strip()
        print(f"[OODA] Generation done: {len(cleaned_pass1)} chars")
        print(f"[OODA] Raw OODA output:\n{cleaned_pass1[:1500]}")

        # Parse OODA block
        ooda = parse_ooda_block(cleaned_pass1)
        if ooda is None:
            # Fallback stage 1: regex-repair truncated/loose OODA tags so
            # state deltas aren't lost when the model omits </ooda> or emits
            # <update_state/> tags with no <ooda> wrapper.
            ooda = repair_ooda_block(cleaned_pass1)
            if ooda:
                print("[OODA] parse failed, repair_ooda_block recovered partial block")
                await _ws_send(
                    conv_id,
                    {"type": "status",
                     "text": "OODA: recovered partial block (repair)",
                     "parent_id": parent_id},
                )

        if ooda:
            # Emit OODA steps as tool blocks for visibility
            if ooda["observe"]:
                tool_id = f"ooda-observe-{conv_id}"
                await _ws_send(
                    conv_id,
                    {
                        "type": "tool_start",
                        "name": "OODA: Observe",
                        "tool_id": tool_id,
                        "ooda": True,
                    },
                )
                await _ws_send(
                    conv_id,
                    {
                        "type": "tool_result",
                        "content": ooda["observe"],
                        "tool_id": tool_id,
                    },
                )
            if ooda["orient"]:
                tool_id = f"ooda-orient-{conv_id}"
                await _ws_send(
                    conv_id,
                    {
                        "type": "tool_start",
                        "name": "OODA: Orient",
                        "tool_id": tool_id,
                        "ooda": True,
                    },
                )
                await _ws_send(
                    conv_id,
                    {
                        "type": "tool_result",
                        "content": ooda["orient"],
                        "tool_id": tool_id,
                    },
                )
            if ooda["decide"]:
                tool_id = f"ooda-decide-{conv_id}"
                await _ws_send(
                    conv_id,
                    {
                        "type": "tool_start",
                        "name": "OODA: Decide",
                        "tool_id": tool_id,
                        "ooda": True,
                    },
                )
                await _ws_send(
                    conv_id,
                    {
                        "type": "tool_result",
                        "content": ooda["decide"],
                        "tool_id": tool_id,
                    },
                )

            resolved = await execute_ooda_reads(conv_id, ooda["reads"])
            print(
                f"[OODA] Resolved {len(resolved)} reads, applying {len(ooda['updates'])} updates, {len(ooda['creates'])} creates"
            )

            # State updates saved as branch deltas only (Tier 3) — base cards stay pristine
            # Notify client of the effective state change for this branch
            if ooda["updates"] or ooda["creates"]:
                await _ws_send(
                    conv_id, {"type": "state_update", "updates": ooda["updates"]}
                )

        # ── Extract prose (single-pass: prose comes after </ooda> tag) ──
        final_prose = ""
        if ooda:
            final_prose = extract_post_ooda_prose(cleaned_pass1)
        if not final_prose:
            # No OODA block or no prose after it — use the whole output
            final_prose = cleaned_pass1
            # Strip closed ooda blocks
            final_prose = _re.sub(r"<ooda>[\s\S]*?</ooda>\s*", "", final_prose).strip()
            # Strip truncated/unclosed ooda blocks (model ran out of tokens)
            final_prose = _re.sub(r"<ooda>[\s\S]*$", "", final_prose).strip()

        # ── Fallback stage 2: second model pass when repair failed AND no prose ──
        # This is the only path that costs an extra model call. It fires when the
        # first pass produced neither a parseable OODA block nor any usable
        # prose — the rare total-malfunction case (e.g. model refused the OODA
        # format entirely). Keeps the common case single-pass.
        if not final_prose.strip() and not ooda:
            print("[OODA] Pass 1 produced no OODA block and no prose — running repair pass")
            await _ws_send(
                conv_id,
                {"type": "status",
                 "text": "OODA: re-prompting (malformed output)...",
                 "parent_id": parent_id},
            )
            repair_messages = list(messages) + [{
                "role": "user",
                "content": (
                    "Your previous output did not contain a valid <ooda>...</ooda> block. "
                    "Emit a complete <ooda> block with <observe>, <orient>, <decide>, and any "
                    "<read_state>/<update_state>/<create_state> tags, followed by 1-3 "
                    "paragraphs of in-character prose. Do not include any other commentary."
                ),
            }]
            raw_pass2, usage_pass2 = await sync_chat(
                repair_messages,
                max_tokens=ooda_max_tokens,
                think=False,
                model=weave_model,
                return_usage=True,
            )
            if raw_pass2:
                first_response_ms = await _record_first_response(
                    conv_id, draft_msg_id, _gen_key_local, _ooda_start_t, first_response_ms,
                )
            await _record_turn_model_attestation(
                conv_id,
                draft_msg_id,
                parent_id,
                (usage_pass2 or {}).get("model_attestation"),
                _gen_key_local,
            )
            if asyncio.current_task().cancelled():
                raise asyncio.CancelledError()
            cleaned_pass2 = _re.sub(r"<think>[\s\S]*?</think>", "", raw_pass2)
            cleaned_pass2 = _re.sub(r"<think>[\s\S]*?</think>\s*", "", cleaned_pass2).strip()
            ooda2 = parse_ooda_block(cleaned_pass2)
            if ooda2 is None:
                ooda2 = repair_ooda_block(cleaned_pass2)
            if ooda2:
                ooda = ooda2
                resolved2 = await execute_ooda_reads(conv_id, ooda["reads"])
                print(
                    f"[OODA] Repair pass recovered: {len(resolved2)} reads, "
                    f"{len(ooda['updates'])} updates, {len(ooda['creates'])} creates"
                )
                if ooda["updates"] or ooda["creates"]:
                    await _ws_send(
                        conv_id, {"type": "state_update", "updates": ooda["updates"]}
                    )
            final_prose = extract_post_ooda_prose(cleaned_pass2) if ooda else ""
            if not final_prose:
                final_prose = cleaned_pass2
                final_prose = _re.sub(r"<ooda>[\s\S]*?</ooda>\s*", "", final_prose).strip()
                final_prose = _re.sub(r"<ooda>[\s\S]*$", "", final_prose).strip()

        ooda_input_tokens = int((usage_pass1 or {}).get("input_tokens") or 0)
        ooda_output_tokens = int((usage_pass1 or {}).get("output_tokens") or 0)
        if "usage_pass2" in locals():
            ooda_input_tokens += int((usage_pass2 or {}).get("input_tokens") or 0)
            ooda_output_tokens += int((usage_pass2 or {}).get("output_tokens") or 0)
        if ooda_input_tokens or ooda_output_tokens:
            await _ws_send(
                conv_id,
                {
                    "type": "usage",
                    "input_tokens": ooda_input_tokens,
                    "output_tokens": ooda_output_tokens,
                },
            )

        # Stream prose to client
        for i in range(0, len(final_prose), 8):
            await _ws_send(
                conv_id, {"type": "stream_chunk", "content": final_prose[i : i + 8]}
            )

        if not final_prose.strip():
            if ooda:
                # OODA analysis succeeded but no prose — save analysis summary as content
                summary_parts = []
                if ooda.get("observe"):
                    summary_parts.append(f"*{ooda['observe'][:200]}*")
                if ooda.get("orient"):
                    summary_parts.append(f"*{ooda['orient'][:200]}*")
                final_prose = (
                    "\n\n".join(summary_parts)
                    if summary_parts
                    else "[OODA analysis completed but no prose generated — try regenerating]"
                )
                for i in range(0, len(final_prose), 8):
                    await _ws_send(
                        conv_id,
                        {"type": "stream_chunk", "content": final_prose[i : i + 8]},
                    )
            else:
                if draft_msg_id:
                    await db.delete_branch(draft_msg_id)
                await _ws_send(
                    conv_id,
                    {
                        "type": "error",
                        "error": "Model returned an empty response — try regenerating",
                    },
                )
                return

        # Update draft with final content
        _ooda_gen_ms = int((_time.time() - _ooda_start_t) * 1000)
        await db.update_message_content(
            draft_msg_id,
            content=final_prose,
            turn_input_tokens=ooda_input_tokens,
            turn_output_tokens=ooda_output_tokens,
            cc_model_used=weave_model,
            generation_ms=_ooda_gen_ms,
        )
        await db.set_active_branch(conv_id, draft_msg_id)
        msg = await db.get_message(draft_msg_id)
        # Save branch-level state deltas (Tier 3)
        if ooda and ooda.get("updates"):
            await db.save_state_deltas(draft_msg_id, ooda["updates"])
        await _ws_send(conv_id, {"type": "stream_end", "message": dict(msg)})
        conv_title = conv.get("title", "Conversation")
        preview = (final_prose or "").replace("#", "").replace("*", "").strip()[:120]
        await _ws_broadcast_all(
            {
                "type": "branch_landed",
                "conv_id": conv_id,
                "conv_title": conv_title,
                "message_id": draft_msg_id,
                "preview": preview,
            }
        )

    except asyncio.CancelledError:
        await _finalize_user_stopped_generation(
            conv_id,
            draft_msg_id,
            partial_text=final_prose,
            model_used=locals().get("weave_model"),
            generation_ms=int(
                (_time.time() - locals().get("_ooda_start_t", _time.time())) * 1000
            ),
        )
        raise
    except Exception as e:
        if draft_msg_id and final_prose and final_prose.strip():
            await db.update_message_content(draft_msg_id, content=final_prose)
        elif draft_msg_id:
            await db.delete_branch(draft_msg_id)
        print(f"[OODA] Generation error conv={conv_id}: {e}")
        import traceback

        traceback.print_exc()
        await _ws_send(conv_id, {"type": "error", "error": str(e)})
    finally:
        _gen_key = getattr(asyncio.current_task(), "_gen_key", None)
        if _gen_key:
            _active_generations.pop(_gen_key, None)
            _generation_snapshots.pop(_gen_key, None)
        await _send_generation_idle_if_quiescent(conv_id)


async def _handle_weave_generation(
    websocket: WebSocket, conv_id: int, conv: dict, data: dict
):
    """Handle Weave (local llama) generation — original logic."""
    draft_msg_id = None
    full_response = ""
    first_response_ms = None
    try:
        action = data.get("action")
        parent_id = data.get("parent_id")
        print(
            f"[GEN] _handle_weave_generation called: action={action} parent_id={parent_id} conv_id={conv_id}"
        )
        print(f"[GEN] conv={conv}")

        if parent_id is None:
            raise ValueError("Weave generation requires an explicit parent message")
        system_only = bool(conv) and _truthy_setting(conv.get("system_only"))
        character = None
        if (not system_only) and conv and conv.get("character_id"):
            char_path = os.path.join(
                config.characters_dir, f"{conv['character_id']}.md"
            )
            print(
                f"[GEN] Loading character from: {char_path} (exists={os.path.exists(char_path)})"
            )
            character = load_character(char_path)
            print(
                f"[GEN] Character loaded: name={character.get('name') if character else 'NONE'}, personality_len={len(character.get('personality','')) if character else 0}, scenario_len={len(character.get('scenario','')) if character else 0}"
            )
        else:
            print(
                f"[GEN] No character_id on conv! character_id={conv.get('character_id') if conv else 'NO CONV'} system_only={system_only}"
            )

        # Get style nudge from conversation settings (user-selected, not rotating)
        style_nudge_name = "Natural" if system_only else (conv.get("style_nudge", "Natural") if conv else "Natural")
        nudge_index = 0
        for i, nudge in enumerate(STYLE_NUDGES):
            if nudge["name"] == style_nudge_name:
                nudge_index = i
                break

        # Load persona if set
        persona = None
        if (not system_only) and conv and conv.get("persona_id"):
            persona_path = os.path.join("personas", f"{conv['persona_id']}.md")
            persona = load_persona(persona_path)
            print(
                f"[GEN] Persona: id={conv['persona_id']} path={persona_path} loaded={'yes' if persona else 'NO'}"
            )
        else:
            print(f"[GEN] No persona_id set on conversation")

        # Load lore entries if set
        lore_entries = []
        if (not system_only) and conv and conv.get("lore_ids"):
            import json as _json

            try:
                lore_ids = (
                    _json.loads(conv["lore_ids"])
                    if isinstance(conv["lore_ids"], str)
                    else conv["lore_ids"]
                )
            except (ValueError, TypeError):
                lore_ids = []
            for lid in lore_ids:
                entry = load_lore_entry(os.path.join("lore", f"{lid}.md"))
                if entry:
                    lore_entries.append(entry)

        # Get context (with potential compactification)
        # For regenerate, truncate context to parent_id so we don't include
        # the old response we're regenerating
        context = await get_context_for_generation(conv_id, character, leaf_id=parent_id)
        if action == "regenerate" and parent_id is not None:
            context["verbatim_messages"] = [
                m for m in context["verbatim_messages"] if m["id"] <= parent_id
            ]

        # Run repetition analysis on recent assistant messages
        # Build system prompt (use custom scene if set)
        custom_scene = None if system_only else (conv.get("custom_scene") if conv else None)
        if system_only:
            system_prompt = _minimal_weave_system_prompt(conv)
        else:
            system_prompt = build_system_prompt(
                character=character,
                style_nudge_index=nudge_index,
                scenario_override=custom_scene,
            )

        # Assemble full prompt (persona + lore injected as user turn)
        example_msgs = [] if system_only else (character.get("example_messages", []) if character else [])
        messages = assemble_prompt(
            system_prompt=system_prompt,
            example_messages=example_msgs,
            summary=context.get("summary"),
            conversation_messages=context["verbatim_messages"],
            persona=persona,
            lore_entries=lore_entries,
        )

        # Debug: log assembled prompt
        print(f"[GEN] System prompt length: {len(system_prompt)}")
        print(f"[GEN] System prompt preview: {system_prompt[:300]}...")
        print(f"[GEN] Total messages in prompt: {len(messages)}")
        print(
            f"[GEN] Context verbatim_messages count: {len(context['verbatim_messages'])}"
        )
        for i, m in enumerate(messages):
            print(f"[GEN]   msg[{i}] role={m['role']} len={len(m['content'])}")

        # Send context info — use actual assembled prompt token count
        actual_tokens = sum(len(m["content"]) // 3 for m in messages)
        active_nudge = get_style_nudge(nudge_index)
        context_payload = {
            "type": "context_info",
            "total_tokens": actual_tokens,
            "was_compactified": context["was_compactified"],
            "style_nudge": active_nudge["name"],
            "parent_id": parent_id,
        }
        if context["was_compactified"]:
            context_payload["total_messages"] = context.get("total_messages")
            context_payload["verbatim_count"] = context.get("verbatim_count")
            context_payload["summarized_count"] = context.get("summarized_count")
            context_payload["summary_text"] = context.get("summary")
            # Auto-branch: insert a system marker at the compaction point
            summ_count = context.get("summarized_count", "?")
            verb_count = context.get("verbatim_count", "?")
            marker = await db.add_message(
                conv_id, "system",
                f"[Context compactified — {summ_count} messages summarized, {verb_count} sent verbatim]",
                parent_id=parent_id,
            )
            parent_id = marker["id"]
            context_payload["compaction_marker_id"] = marker["id"]
        await _ws_send(conv_id, context_payload)

        # Create draft message in DB so it appears as ghost node on tree.
        draft_msg = await _create_generation_draft(conv_id, conv, parent_id)
        draft_msg_id = draft_msg["id"]

        # Stream the response
        weave_model = conv.get("local_model") or config.llama_model or ""
        await db.update_message_content(draft_msg_id, cc_model_used=weave_model)
        print(
            f"[GEN] Starting generation for conv={conv_id} parent={parent_id} model={config.llama_model}"
        )
        await _ws_send(
            conv_id,
            {
                "type": "stream_start",
                "parent_id": parent_id,
                "draft_msg_id": draft_msg_id,
                "local_model": weave_model,
            },
        )

        # Initialize live snapshot for Weave generation
        import time as _time
        _weave_start_t = _time.time()

        _gen_key_local = getattr(asyncio.current_task(), "_gen_key", None)
        if _gen_key_local:
            _update_gen_snapshot(
                _gen_key_local,
                full_text="",
                content_blocks=[],
                started_at=_time.time(),
                draft_msg_id=draft_msg_id,
                parent_id=parent_id,
                cc_model=weave_model or "",
                mode="weave",
            )

        import llama_client as _llama_client
        await _record_turn_model_attestation(
            conv_id,
            draft_msg_id,
            parent_id,
            _llama_client.configured_completion_attestation(weave_model),
            _gen_key_local,
        )

        full_response = ""
        _thinking_buffer = ""
        content_blocks: list[dict] = []
        current_block: dict | None = None
        total_input_tokens = 0
        total_output_tokens = 0
        weave_model = weave_model or None
        weave_max_tokens = int(getattr(config, "weave_max_tokens", None) or getattr(config, "max_tokens", 2048) or 2048)
        async for item in stream_chat(messages, model=weave_model, max_tokens=weave_max_tokens):
            if isinstance(item, dict):
                if item.get("type") == "model_attestation":
                    await _record_turn_model_attestation(
                        conv_id,
                        draft_msg_id,
                        parent_id,
                        item.get("model_attestation"),
                        _gen_key_local,
                    )
                elif item.get("type") == "thinking_delta":
                    if item.get("text"):
                        first_response_ms = await _record_first_response(
                            conv_id, draft_msg_id, _gen_key_local, _weave_start_t, first_response_ms,
                        )
                    _thinking_buffer += item["text"]
                    if current_block and current_block.get("type") == "thinking":
                        current_block["text"] += item["text"]
                    else:
                        current_block = {"type": "thinking", "text": item["text"]}
                        content_blocks.append(current_block)
                    await _ws_send(
                        conv_id, {"type": "thinking_chunk", "content": item["text"]}
                    )
                    if _gen_key_local:
                        _update_gen_snapshot(
                            _gen_key_local,
                            full_text=full_response,
                            content_blocks=content_blocks,
                            input_tokens=total_input_tokens,
                            output_tokens=total_output_tokens,
                        )
                else:
                    # thinking_start, thinking_end, usage — forward as-is
                    if item.get("type") == "usage":
                        total_input_tokens = int(item.get("input_tokens") or total_input_tokens or 0)
                        total_output_tokens = int(item.get("output_tokens") or total_output_tokens or 0)
                        await db.update_message_content(draft_msg_id, turn_input_tokens=total_input_tokens,
                                                        turn_output_tokens=total_output_tokens)
                    await _ws_send(conv_id, item)
                continue
            if item:
                first_response_ms = await _record_first_response(
                    conv_id, draft_msg_id, _gen_key_local, _weave_start_t, first_response_ms,
                )
            full_response += item
            if current_block and current_block.get("type") == "text":
                current_block["text"] += item
            else:
                current_block = {"type": "text", "text": item}
                content_blocks.append(current_block)
            await _ws_send(
                conv_id,
                {
                    "type": "stream_chunk",
                    "content": item,
                },
            )
            # Keep live snapshot in sync
            if _gen_key_local:
                _update_gen_snapshot(
                    _gen_key_local,
                    full_text=full_response,
                    content_blocks=content_blocks,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )

        # Strip thinking blocks from models that include tags in their content field
        # (qwen3 uses <think>.../», DeepSeek/llama.cpp uses <think>...</think>)
        import re as _re

        cleaned = _re.sub(r"<think>[\s\S]*?</think>\s*", "", full_response)
        cleaned = _re.sub(r"<think>[\s\S]*?</think>\s*", "", cleaned).strip()
        if cleaned:
            full_response = cleaned

        # If response is empty, send error instead of saving empty message
        if not full_response.strip() and content_blocks:
            full_response = "[No visible response]"
            if current_block and current_block.get("type") == "text":
                current_block["text"] += full_response
            else:
                content_blocks.append({"type": "text", "text": full_response})

        if not full_response.strip():
            print(
                f"[WARN] Empty response. Raw length={len(full_response)} Cleaned length={len(cleaned)}"
            )
            if draft_msg_id:
                await db.delete_branch(draft_msg_id)
            await _ws_send(
                conv_id,
                {
                    "type": "error",
                    "error": "Model returned an empty response — try again",
                },
            )
            return

        # Update draft with final content
        _weave_gen_ms = int((_time.time() - _weave_start_t) * 1000)
        await db.update_message_content(
            draft_msg_id,
            content=full_response,
            content_blocks=json.dumps(content_blocks) if content_blocks else None,
            turn_input_tokens=total_input_tokens,
            turn_output_tokens=total_output_tokens,
            cc_model_used=weave_model,
            generation_ms=_weave_gen_ms,
        )
        await db.set_active_branch(conv_id, draft_msg_id)
        msg = await db.get_message(draft_msg_id)
        await _ws_send(
            conv_id,
            {
                "type": "stream_end",
                "message": dict(msg),
            },
        )
        conv_title = conv.get("title", "Conversation")
        preview = (full_response or "").replace("#", "").replace("*", "").strip()[:120]
        await _ws_broadcast_all(
            {
                "type": "branch_landed",
                "conv_id": conv_id,
                "conv_title": conv_title,
                "message_id": draft_msg_id,
                "preview": preview,
            }
        )

    except asyncio.CancelledError:
        await _finalize_user_stopped_generation(
            conv_id,
            draft_msg_id,
            partial_text=full_response,
            content_blocks=locals().get("content_blocks"),
            model_used=locals().get("weave_model"),
            generation_ms=int(
                (_time.time() - locals().get("_weave_start_t", _time.time())) * 1000
            ),
        )
        raise
    except Exception as e:
        # Save partial content or clean up empty draft
        if draft_msg_id and full_response.strip():
            _weave_partial_ms = int((_time.time() - _weave_start_t) * 1000)
            await db.update_message_content(draft_msg_id, content=full_response, generation_ms=_weave_partial_ms)
        elif draft_msg_id:
            await db.delete_branch(draft_msg_id)
        print(f"[GEN] Weave generation error conv={conv_id}: {e}")
        await _ws_send(conv_id, {"type": "error", "error": str(e)})
    finally:
        _gen_key = getattr(asyncio.current_task(), "_gen_key", None)
        if _gen_key:
            _active_generations.pop(_gen_key, None)
            _generation_snapshots.pop(_gen_key, None)
        await _send_generation_idle_if_quiescent(conv_id)


if __name__ == "__main__":
    import uvicorn

    ssl_kwargs = {}
    if os.path.exists(config.ssl_certfile) and os.path.exists(config.ssl_keyfile):
        ssl_kwargs["ssl_certfile"] = config.ssl_certfile
        ssl_kwargs["ssl_keyfile"] = config.ssl_keyfile
        print(f"[SSL] HTTPS enabled — cert={config.ssl_certfile}")
    else:
        print("[SSL] No certs found — running plain HTTP")

    uv_config = uvicorn.Config(
        app,
        host=config.host,
        port=config.port,
        ws="websockets-sansio",
        ws_ping_interval=20,
        ws_ping_timeout=20,
        # Keep the transport peer authoritative for network/authentication gates.
        proxy_headers=False,
        **ssl_kwargs,
    )
    server = uvicorn.Server(uv_config)
    _server_ref.append(server)
    server.run()
