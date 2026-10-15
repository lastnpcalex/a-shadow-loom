"""Antigravity (agy) CLI subprocess wrapper.

Runs agy print mode (-p flag) with structured stream-json output.
Model and effort are passed directly on the agy command line.

Since the July/August 2026 agy updates, print mode supports structured
`--output-format stream-json` plus direct `--model` and `--effort` flags.
run_gemini reads agy's stdout as NDJSON and maps it into Loom's normal
event stream. Transcript tailing has been removed.
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path

from loom_agent_prompt import prepend_loom_agent_context

log = logging.getLogger(__name__)

# Use the standard Loom blocking hook
_HOOK_SCRIPT = str(Path(__file__).parent / "cc_permission_hook.py")

# Queues are keyed by the exact conversation/run scope.  The old conversation
# only key made a second Antigravity turn overwrite the first turn's tool
# notifications, which then looked like stream hijacking in the browser.
_active_queues: dict[str | int, asyncio.Queue] = {}


def active_queue(conv_id: int, permission_scope: str | None = None):
    """Return the queue for one exact run, with a legacy single-run fallback."""
    scope = str(permission_scope or "").strip()
    if scope:
        return _active_queues.get(f"{int(conv_id)}:{scope}")
    return _active_queues.get(int(conv_id))


def _agy_home() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("USERPROFILE", Path.home())) / ".gemini" / "antigravity-cli"
    return Path.home() / ".gemini" / "antigravity-cli"


_RE_RESETS_IN = re.compile(r"Resets in ([0-9hms]+)")


def _parse_agy_log_filename_ts(filename: str) -> float | None:
    import time as _time
    import re
    m = re.match(r"cli-(\d{8})_(\d{6})\.log", filename)
    if not m:
        return None
    date_str, time_str = m.groups()
    try:
        dt_str = f"{date_str} {time_str}"
        struct_time = _time.strptime(dt_str, "%Y%m%d %H%M%S")
        return _time.mktime(struct_time)
    except Exception:
        return None


def _scan_agy_log_for_error(since_ts: float) -> str | None:
    """Look at the newest agy cli-*.log for a fatal signal (429/auth) since `since_ts`.

    agy writes its real error to its own log file rather than stderr, so when
    the transcript never materializes we have to read this to surface a useful
    message instead of "exited with no response".
    """
    log_dir = _agy_home() / "log"
    if not log_dir.exists():
        return None
    try:
        candidates = []
        for p in log_dir.glob("cli-*.log"):
            filename_ts = _parse_agy_log_filename_ts(p.name)
            # Filter log files created within 30s of launch_ts to protect against OneDrive sync time skew
            if filename_ts is not None and abs(filename_ts - since_ts) <= 30.0:
                candidates.append(p)
    except OSError:
        return None
    if not candidates:
        return None
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        text = latest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    if "RESOURCE_EXHAUSTED" in text or "Individual quota reached" in text:
        m = _RE_RESETS_IN.search(text)
        resets = f" — resets in {m.group(1)}" if m else ""
        return f"Antigravity quota reached (429){resets}"
    if "You are not logged into Antigravity" in text:
        success_indicators = [
            "silent auth succeeded",
            "authenticated successfully",
            "Auth succeeded",
        ]
        if not any(indicator in text for indicator in success_indicators):
            return "Antigravity is not logged in — run `agy` interactively to sign in"
    if "PERMISSION_DENIED" in text:
        return "Antigravity permission denied — check account access"

    # Planner-loop graceful shutdown: agy's planner repeatedly emits
    # "PlannerResponse without ModifiedResponse encountered" when it can't
    # produce a modified response (a planner iteration that doesn't advance
    # the turn), then tears down its own stream ("Stopping conversation
    # stream" + "Language server shutting down") without ever exiting the OS
    # process. Death log had 106 of these; healthy sessions had 0-8. Require
    # >=10 stall lines AND both cascade lines so a session that emits a few
    # benign stall lines and recovers (no cascade) is not flagged.
    if ("Language server shutting down" in text
            and "Stopping conversation stream" in text):
        stall_count = text.count("PlannerResponse without ModifiedResponse encountered")
        if stall_count >= 10:
            return (f"Antigravity planner loop stalled — {stall_count} "
                    "PlannerResponse-without-ModifiedResponse iterations; "
                    "agy tore down the conversation stream without producing a final result")
    return None


# Liveness watchdog tuning. agy only writes a log line when a model call
# returns or a tool hook runs, so a quiet log alone does NOT mean agy is dead:
# a long thinking/final-answer generation is silent on both the log and
# stream-json stdout. Only a quiet log whose tail shows agy's own teardown
# (the planner-loop zombie: stream stopped, OS process never exits) is treated
# as dead quickly; any other silence gets the much longer hard ceiling.
_AGY_HEARTBEAT_POLLS = 30          # empty ~1.5s stdout polls between checks
_AGY_LOG_FRESH_S = 45.0            # log touched this recently => working
_AGY_ZOMBIE_QUIET_S = 60.0         # quiet after teardown markers => zombie
_AGY_QUIET_CEILING_S = 20 * 60.0   # quiet without teardown => hung model call
_AGY_SHUTDOWN_MARKERS = (
    "Stopping conversation stream",
    "Language server shutting down",
    "CLI store manager shutting down",
)
_AGY_LOG_TAIL_BYTES = 64 * 1024


def _agy_run_log(launch_ts: float) -> Path | None:
    """Return the agy cli log belonging to the run launched at `launch_ts`.

    agy names its log after its start second, so the run's own log is the one
    whose filename timestamp is closest to launch. Picking the newest mtime
    instead lets a concurrent sibling run's log stand in for this one.
    """
    log_dir = _agy_home() / "log"
    if not log_dir.exists():
        return None
    try:
        candidates = []
        for p in log_dir.glob("cli-*.log"):
            filename_ts = _parse_agy_log_filename_ts(p.name)
            if filename_ts is not None and abs(filename_ts - launch_ts) <= 30.0:
                candidates.append((abs(filename_ts - launch_ts), -p.stat().st_mtime, p))
    except OSError:
        return None
    if not candidates:
        return None
    return min(candidates, key=lambda c: (c[0], c[1]))[2]


def _agy_log_state(launch_ts: float) -> dict:
    """Describe the run's agy log: freshness, seconds quiet, teardown seen.

    `quiet_s` is None when no log exists yet. `shutdown` is True when the log
    tail contains agy's own stream-teardown lines.
    """
    import time as _time

    state = {"fresh": False, "quiet_s": None, "shutdown": False}
    log_path = _agy_run_log(launch_ts)
    if log_path is None:
        return state
    try:
        size = log_path.stat().st_size
        quiet_s = max(0.0, _time.time() - log_path.stat().st_mtime)
        with log_path.open("rb") as fh:
            fh.seek(max(0, size - _AGY_LOG_TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return state
    state["quiet_s"] = quiet_s
    state["fresh"] = quiet_s < _AGY_LOG_FRESH_S
    state["shutdown"] = any(marker in tail for marker in _AGY_SHUTDOWN_MARKERS)
    return state


async def _stop_agy_process(proc, grace: float = 0.0) -> None:
    """Stop agy and its child tree after an optional grace period.

    proc.kill() alone leaves agy's language-server children running on
    Windows, so kill the whole tree there.
    """
    if grace > 0 and proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except asyncio.TimeoutError:
            pass
    if proc.returncode is not None:
        return
    pid = getattr(proc, "pid", None)
    if sys.platform == "win32" and isinstance(pid, int):
        import subprocess
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
        except Exception as e:
            log.warning(f"[AGY] taskkill /T failed for pid {pid}: {e}")
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        log.warning("[AGY] process did not report exit after kill")


def _find_agy_exe() -> str:
    """Find the agy executable on PATH or in known install locations."""
    agy_name = "agy.exe" if sys.platform == "win32" else "agy"
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        known = os.path.join(local_app_data, "agy", "bin", "agy.exe")
        if os.path.exists(known):
            return known
    return agy_name


def _agy_effort_for_model(model: str, effort: str) -> str:
    """Use effort encoded by an agy selector; otherwise use Loom's control."""
    raw = (model or "").removeprefix("gemini:").strip().lower()
    explicit = re.search(r"(?:-|\()(?P<effort>low|medium|high)\)?$", raw)
    if explicit:
        return explicit.group("effort")
    return effort if effort in {"low", "medium", "high"} else "high"


def _loom_model_to_agy(model: str, effort: str) -> str:
    """Map a Loom selector without overriding model identity encoded in it."""
    raw = (model or "").strip()
    if raw.lower().startswith("gemini:"):
        raw = raw[7:].strip()
    ml = raw.lower()

    # Discovered values are exact agy selectors, including their effort tier.
    if re.fullmatch(r"(?:claude|gpt)-[a-z0-9.-]+", raw) and raw not in {
        "claude-sonnet", "claude-opus", "gpt-oss", "gpt-oss-120b",
    }:
        return raw
    if re.fullmatch(r"gemini-\d+(?:\.\d+)+-(?:flash|pro)(?:-(?:low|medium|high))?", ml):
        return raw

    legacy_gemini = re.search(r"gemini\s+(\d+(?:\.\d+)+)\s+(flash|pro)", ml)
    if legacy_gemini:
        encoded_effort = _agy_effort_for_model(raw, effort)
        return f"gemini-{legacy_gemini.group(1)}-{legacy_gemini.group(2)}-{encoded_effort}"
    if "sonnet" in ml:
        if "4-6" in ml or "4.6" in ml or ml in {"claude-sonnet", "sonnet"}:
            return "claude-sonnet-4-6"
        return raw
    if "opus" in ml:
        if "4-6" in ml or "4.6" in ml or ml in {"claude-opus", "opus"}:
            return "claude-opus-4-6-thinking"
        return raw
    if "gpt-oss" in ml:
        if "120b" in ml or ml in {"gpt-oss", "gpt-oss-120b"}:
            return "gpt-oss-120b-medium"
        return raw
    return raw


def agy_models_cache_path() -> Path:
    """Loom-owned discovery cache, separate from the Gemini CLI's cache."""
    from exo_state import state_path
    return state_path("model-discovery", "agy.json")


def _agy_model_rows(items) -> list[dict]:
    """Validate discovery/cache rows and keep labels stable across reloads."""
    models = []
    seen = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        slug, label = item.get("slug"), item.get("display_name")
        if not isinstance(slug, str) or not isinstance(label, str):
            continue
        slug, label = slug.strip(), label.strip()
        if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)+", slug) or not label or slug in seen:
            continue
        seen.add(slug)
        models.append({"value": f"gemini:{slug}", "label": label})
    return models


def discover_agy_models() -> list[dict]:
    """Query `agy models` CLI and return available models as dropdown items."""
    agy_exe = _find_agy_exe()
    try:
        import subprocess
        res = subprocess.run(
            [agy_exe, "models"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if res.returncode != 0 or not res.stdout:
            return []

        cache_items = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if "\t" in line:
                slug, display_name = line.split("\t", 1)
            else:
                m = re.match(r"^([a-z0-9.-]+)\s+\((.+)\)$", line) or re.match(r"^([a-z0-9.-]+)\s+(.+)$", line)
                if m:
                    slug, display_name = m.group(1), m.group(2)
                else:
                    continue
            slug = slug.strip()
            display_name = display_name.strip()
            cache_items.append({
                "slug": slug,
                "display_name": display_name,
            })

        models = _agy_model_rows(cache_items)
        if models:
            try:
                import tempfile
                cache_path = agy_models_cache_path()
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_items = [{"slug": row["value"][7:], "display_name": row["label"]} for row in models]
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache_path.parent,
                                                     suffix=".tmp", delete=False) as output:
                        temporary = Path(output.name)
                        json.dump({"models": cache_items}, output, indent=2)
                    temporary.replace(cache_path)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            except Exception as ce:
                log.debug(f"[AGY] Failed to write models_cache.json: {ce}")

        return models
    except Exception as e:
        log.warning(f"[AGY] Failed to discover models via agy CLI: {e}")
        return []


def _agy_model_provider(model: str) -> str:
    value = str(model or "").strip().lower()
    if value.startswith("gemini"):
        return "google"
    if value.startswith("claude"):
        return "anthropic"
    if value.startswith(("gpt", "o3", "o4")):
        return "openai"
    return "antigravity"


def _agy_model_attestation(
    requested_model: str,
    launch_model: str,
    effective_model: str | None,
    *,
    source: str,
    session_id: str = "",
) -> dict:
    effective = str(effective_model or "").strip()
    if effective and effective.casefold() == str(launch_model or "").strip().casefold():
        status = "verified"
    elif effective:
        status = "mismatch"
    elif launch_model:
        status = "configured"
    else:
        status = "unverified"
    return {
        "status": status,
        "harness": "Antigravity (agy)",
        "requested_model": str(requested_model or "").strip(),
        "launch_model": str(launch_model or "").strip() or None,
        "effective_model": effective or None,
        "model_provider": _agy_model_provider(effective or launch_model),
        "source": source,
        "verification_level": "harness" if effective else "launch_configuration",
        "session_id": session_id or None,
        "fallback_allowed": False,
    }


def _set_agy_model(agy_model_id: str):
    """Update agy's settings.json with the desired model."""
    if sys.platform == "win32":
        home = Path(os.environ.get("USERPROFILE", Path.home()))
    else:
        home = Path.home()
    settings_path = home / ".gemini" / "antigravity-cli" / "settings.json"
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            settings["model"] = agy_model_id
            settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
            log.info(f"[AGY] Model updated in settings.json: {agy_model_id}")
        except Exception as e:
            log.error(f"[AGY] Failed to write model to settings.json: {e}")


def _agy_hook_command(event: str) -> str:
    if event not in {"PreToolUse", "PostToolUse"}:
        raise ValueError("Unsupported Loom hook event")
    if sys.platform == "win32":
        import base64
        # agy's Go cmd /c launcher escapes embedded quotes as literal \".
        # Keep its command line quote-free; encode only our fixed hook entry
        # point (never model input). The JSON request still travels on stdin.
        quote = lambda value: "'" + value.replace("'", "''") + "'"
        script = f"& {quote(sys.executable)} {quote(_HOOK_SCRIPT)} --event {event}; exit $LASTEXITCODE"
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {encoded}"
    return f'"{sys.executable}" "{_HOOK_SCRIPT}" --event {event}'


def _configure_permission_hook(
    cwd: str,
    backstage_parent_id: int | None = None,
    server_port: int = 8000,
    *,
    conv_id: int = 0,
    agent_run_id: str | None = None,
    parent_run_id: str | None = None,
    client_instance_id: str | None = None,
    history_token: str | None = None,
):
    """Configure hooks.json + trust entry so Antigravity CLI calls our permission hook."""
    agents_dir = Path(cwd) / ".agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    python_exe = sys.executable
    hook_path = _HOOK_SCRIPT
    pre_hook_command = _agy_hook_command("PreToolUse")
    post_hook_command = _agy_hook_command("PostToolUse")

    # PreToolUse: 24 hours — longer than Loom server-side _PERM_TOTAL_DEADLINE
    # so the server can send its deny/allow before the hook times out.
    # User can disconnect, come back, reattach the WS, and approve the hook.
    hooks_def = {
        "PreToolUse": [{
            "matcher": "*",
            "hooks": [{
                "type": "command",
                "command": pre_hook_command,
                "timeout": 86400,
            }]
        }],
        "PostToolUse": [{
            "matcher": "*",
            "hooks": [{
                "type": "command",
                "command": post_hook_command,
                "timeout": 90,
            }]
        }]
    }

    # Named hooks + event arrays are the current agy schema. Merge only our
    # entry; never replace unrelated project hooks with Loom's configuration.
    for target in {Path(cwd), _agy_workspace_root(Path(cwd))}:
        hooks_path = target / ".agents" / "hooks.json"
        existing = json.loads(hooks_path.read_text(encoding="utf-8")) if hooks_path.exists() else {}
        if not isinstance(existing, dict):
            raise ValueError("Antigravity hooks.json must be an object; refusing to overwrite it")
        for event, command, old_timeout in (("PreToolUse", pre_hook_command, 86400000),
                                             ("PostToolUse", post_hook_command, 90000)):
            # Migrate only the exact obsolete entry Loom itself wrote.
            legacy_command = f'"{python_exe}" "{hook_path}" --event {event}'
            for old_command in (legacy_command, "& " + legacy_command):
                if existing.get(event) == {"matcher": "*", "hooks": [
                        {"type": "command", "command": old_command, "timeout": old_timeout}]}:
                    del existing[event]
        existing["loom-permissions"] = hooks_def
        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        hooks_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        log.info(f"[AGY] Hooks configured: {hooks_path}")

    if backstage_parent_id:
        _certs_dir = Path(__file__).parent / "certs"
        protocol = "https" if (_certs_dir / "cert.pem").exists() and (_certs_dir / "key.pem").exists() else "http"
        mcp_config = {
            "mcpServers": {
                "loom-state-cards": {
                    "command": python_exe,
                    "args": [str(Path(__file__).parent / "mcp_state_cards.py")],
                    "env": {
                        "LOOM_API_URL": f"{protocol}://127.0.0.1:{server_port}",
                        "LOOM_BACKSTAGE_PARENT_ID": str(backstage_parent_id)
                    }
                }
            }
        }
        mcp_path = agents_dir / "mcp_config.json"
        mcp_path.write_text(json.dumps(mcp_config, indent=2), encoding="utf-8")
        log.info(f"[AGY] MCP server configured: {mcp_path}")
    else:
        # Neutral agy launch (no backstage, no operator). agy reads MCP config
        # from .agents/ at the git-root workspace it discovers — deleting that
        # file breaks agy's project discovery (falls back to default-cli-project
        # with no MCP tools). Instead of deleting, write an empty config so the
        # directory stays discoverable but registers no servers.
        from codex_client import _agent_control_mcp_config
        control = _agent_control_mcp_config(
            conv_id,
            server_port,
            agent_run_id=agent_run_id,
            parent_run_id=parent_run_id,
            client_instance_id=client_instance_id,
            workspace_root=cwd,
            history_token=history_token,
        )
        mcp_payload = {"mcpServers": {"loom-agent-control": control}} if control else {"mcpServers": {}}
        agy_root = _agy_workspace_root(Path(cwd))
        for target in (Path(cwd), agy_root):
            config_path = target / ".agents" / "mcp_config.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(json.dumps(mcp_payload, indent=2), encoding="utf-8")
            log.info(f"[AGY] Configured MCP surface: {config_path}")

    _ensure_hook_trusted(cwd, pre_hook_command)
    _ensure_hook_trusted(cwd, post_hook_command)


def _ensure_hook_trusted(cwd: str, hook_command: str):
    """Add our hook command to agy's trusted_hooks.json for this project path."""
    if sys.platform == "win32":
        home = Path(os.environ.get("USERPROFILE", Path.home()))
    else:
        home = Path.home()
    trusted_path = home / ".gemini" / "trusted_hooks.json"

    trusted = {}
    if trusted_path.exists():
        try: trusted = json.loads(trusted_path.read_text(encoding="utf-8"))
        except: trusted = {}

    trust_key = f":{hook_command}"
    norm_cwd = str(Path(cwd).resolve())

    project_hooks = trusted.get(norm_cwd, [])
    if trust_key not in project_hooks:
        project_hooks.append(trust_key)
        trusted[norm_cwd] = project_hooks
        trusted_path.parent.mkdir(parents=True, exist_ok=True)
        trusted_path.write_text(json.dumps(trusted, indent=2), encoding="utf-8")
        log.info(f"[AGY] Hook trusted for {norm_cwd}")


_STALE_PROMPT_SECONDS = 24 * 60 * 60

# One lock per agy workspace root. agy reads .agents/hooks.json and
# mcp_config.json at startup, and those files carry the launching run's
# identity; two runs in one workspace must not rewrite them in between.
_AGY_CONFIG_LOCKS: dict[str, asyncio.Lock] = {}
# Upper bound on how long a launch holds the lock waiting for agy's first
# output (by then it has loaded its config).
_AGY_CONFIG_SETTLE_SECONDS = 30.0


async def _acquire_workspace_config_lock(cwd: str) -> asyncio.Lock:
    key = os.path.normcase(str(_agy_workspace_root(Path(cwd)).resolve()))
    lock = _AGY_CONFIG_LOCKS.setdefault(key, asyncio.Lock())
    await lock.acquire()
    return lock


async def _release_config_lock_when_loaded(lock: asyncio.Lock, first_output: asyncio.Event,
                                           proc) -> None:
    """Release once agy has produced output or exited, or after the settle cap."""
    waiters = [asyncio.create_task(first_output.wait()), asyncio.create_task(proc.wait())]
    try:
        await asyncio.wait(waiters, timeout=_AGY_CONFIG_SETTLE_SECONDS,
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
        lock.release()


def _sweep_stale_prompt_files(agents_dir: Path, now: float | None = None) -> None:
    """Delete prompt files left by killed or crashed launches.

    Only files older than a day go, so a long-running turn's file is never
    removed from under it.
    """
    cutoff = (time.time() if now is None else now) - _STALE_PROMPT_SECONDS
    for stale in agents_dir.glob("loom_prompt_*.md"):
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink(missing_ok=True)
        except OSError:
            pass


def _agy_workspace_root(cwd: Path) -> Path:
    """Where agy actually anchors its workspace.

    agy walks up from cwd to find a project root marker (`.git` in practice)
    and uses that as the singular entry in `workspaceDirs`. Its `.agents/`
    and `GEMINI.md` discovery happens at that root, not at cwd. So when the
    operator workspace is a subdirectory of a larger git repo (the Loom repo,
    in our case), files dropped in `cwd/.agents/` are invisible — agy reads
    the repo-root `.agents/` instead. This walks the same path agy does so
    we can write where it will actually look.
    """
    current = cwd.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def _get_agy_project_id(workspace: Path) -> str | None:
    """Read the workspace's project ID from agy's project cache.

    agy stores workspace→project mappings in
    `~/.gemini/antigravity-cli/cache/projects.json`. Without the correct
    project ID, new conversations fall into the default-cli-project which
    has no MCP servers configured.
    """
    sys_home = Path(os.environ.get("USERPROFILE", Path.home())) if sys.platform == "win32" else Path.home()
    cache_file = sys_home / ".gemini" / "antigravity-cli" / "cache" / "projects.json"
    if not cache_file.exists():
        return None
    try:
        projects = json.loads(cache_file.read_text(encoding="utf-8"))
        ws_key = str(workspace.resolve())
        return projects.get(ws_key)
    except (json.JSONDecodeError, OSError):
        return None




def _prepare_agy_prompt(
    prompt: str,
    backstage_parent_id: int | None = None,
    
    is_first_turn: bool = True,
) -> str:
    """Inject Loom only when opening a fresh native agy conversation."""
    if backstage_parent_id or not is_first_turn:
        return prompt
    return prepend_loom_agent_context(prompt, "agy")


def normalize_tool_name(name: str) -> str:
    mapping = {
        "view_file": "Read",
        "read_file": "Read",
        "read_many_files": "Read",
        "write_to_file": "Write",
        "write_file": "Write",
        "replace_file_content": "Edit",
        "multi_replace_file_content": "Edit",
        "run_command": "Bash",
        "execute_command": "Bash",
        "run_shell_command": "Bash",
        "grep_search": "Grep",
        "list_dir": "Glob",
        "list_directory": "Glob",
        "glob": "Glob",
        "search_web": "WebSearch",
        "google_web_search": "WebSearch",
        "read_url_content": "WebFetch",
        "web_fetch": "WebFetch",
        "find_by_name": "Glob",
        "manage_task": "Task",
        "apply_patch": "apply_patch",
    }
    return mapping.get(name, name)


def clean_arg_val(val):
    if isinstance(val, str):
        if val.startswith('"') and val.endswith('"') and len(val) >= 2:
            return val[1:-1]
    return val


def normalize_tool_args(name: str, args: dict) -> dict:
    cleaned = {k: clean_arg_val(v) for k, v in args.items()}
    mapped = {}
    if name in ("view_file", "read_file", "read_many_files"):
        mapped["file_path"] = cleaned.get("AbsolutePath", cleaned.get("TargetFile", cleaned.get("file_path", "")))
    elif name in ("write_to_file", "write_file"):
        mapped["file_path"] = cleaned.get("TargetFile", cleaned.get("file_path", ""))
        mapped["content"] = cleaned.get("CodeContent", cleaned.get("content", ""))
    elif name in ("replace_file_content", "multi_replace_file_content", "edit"):
        mapped["file_path"] = cleaned.get("TargetFile", cleaned.get("file_path", ""))
        mapped["old_string"] = cleaned.get("TargetContent", cleaned.get("old_string", ""))
        mapped["new_string"] = cleaned.get("ReplacementContent", cleaned.get("new_string", ""))
    elif name in ("run_command", "execute_command", "run_shell_command"):
        mapped["command"] = cleaned.get("CommandLine", cleaned.get("command", ""))
    elif name in ("grep_search", "grep"):
        mapped["query"] = cleaned.get("Query", cleaned.get("query", ""))
        mapped["dir"] = cleaned.get("SearchPath", cleaned.get("dir", ""))
    else:
        mapped = cleaned
    for source, target in (("Cwd", "cwd"), ("DirectoryPath", "path"),
                           ("SearchDirectory", "path"), ("SearchPath", "path")):
        if source in cleaned:
            mapped.setdefault(target, cleaned[source])
    return mapped


def _determine_block_name(etype: str, content: str) -> str:
    et = etype.upper()
    if "SYSTEM" in et:
        return "System Message"
    if "GENERIC" in et:
        if "background task" in content.lower():
            return "Background Task"
        return "System Message"
    return etype.replace("_", " ").title()


def _agy_stream_event_name(raw: dict) -> str:
    return str(raw.get("event") or raw.get("type") or raw.get("method") or "").lower()


def _agy_stream_payload(raw: dict) -> dict:
    event = _agy_stream_event_name(raw)
    payload = raw.get(event) if event else None
    if isinstance(payload, dict):
        return payload
    for key in ("result", "step_update", "step", "init", "payload", "data", "params"):
        payload = raw.get(key)
        if isinstance(payload, dict):
            return payload
    return raw


def _agy_text_from_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_agy_text_from_value(v) for v in value]
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        for key in ("text", "content", "response", "message", "output", "result", "summary", "error"):
            text = _agy_text_from_value(value.get(key))
            if text:
                return text
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    return str(value)


def _agy_usage(payload: dict) -> dict | None:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens") or usage.get("inputTokens") or 0
    output_tokens = usage.get("output_tokens") or usage.get("outputTokens") or 0
    thinking_tokens = usage.get("thinking_tokens") or usage.get("thinkingTokens") or 0
    if not input_tokens and not output_tokens and not thinking_tokens:
        total = usage.get("total_tokens") or usage.get("totalTokens") or 0
        if not total:
            return None
    return {
        "type": "usage",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": thinking_tokens,
    }


def _agy_tool_event(payload: dict) -> tuple[str, str, dict, str] | None:
    tool = None
    for key in ("tool_info", "toolInfo", "tool", "tool_call", "toolCall"):
        value = payload.get(key)
        if isinstance(value, dict):
            tool = value
            break
    if not tool:
        return None

    raw_name = (
        tool.get("name")
        or tool.get("tool_name")
        or tool.get("toolName")
        or tool.get("display_name")
        or tool.get("displayName")
        or "Tool"
    )
    step_idx = next((source[key] for source in (payload, tool)
                     for key in ("step_index", "stepIndex")
                     if source.get(key) is not None), None)
    tool_id = str(
        (str(step_idx) if step_idx is not None else "")
        or tool.get("id")
        or tool.get("tool_id")
        or tool.get("toolId")
        or payload.get("id")
        or payload.get("step_id")
        or payload.get("stepId")
        or raw_name
    )
    args = (
        tool.get("input")
        or tool.get("args")
        or tool.get("arguments")
        or tool.get("parameters")
        or {}
    )
    if not isinstance(args, dict):
        args = {"input": args}
    result = _agy_text_from_value(
        tool.get("result")
        or tool.get("output")
        or tool.get("observation")
        or payload.get("tool_result")
        or payload.get("toolResult")
    )
    return str(raw_name), tool_id, args, result


def _agy_step_events(raw: dict, state: dict) -> list[dict]:
    event = _agy_stream_event_name(raw)
    payload = _agy_stream_payload(raw)
    events: list[dict] = []

    if event == "init":
        session_id = (
            raw.get("conversation_id")
            or raw.get("conversationId")
            or raw.get("session_id")
            or raw.get("sessionId")
            or payload.get("conversation_id")
            or payload.get("conversationId")
            or payload.get("session_id")
            or payload.get("sessionId")
        )
        model = (
            raw.get("model")
            or raw.get("model_id")
            or raw.get("modelId")
            or payload.get("model")
            or payload.get("model_id")
            or payload.get("modelId")
        )
        if session_id or model:
            if session_id:
                state["session_id"] = str(session_id)
            if model:
                state["model"] = str(model)
            events.append({
                "type": "session_info",
                "session_id": str(session_id or state.get("session_id") or ""),
                "model": str(model or state.get("model") or ""),
                "model_confirmed": bool(model),
            })
        return events

    if event == "result":
        result = payload
        text = _agy_text_from_value(
            result.get("response")
            or result.get("text")
            or result.get("content")
            or result.get("output")
            or result.get("result")
        )
        status = str(result.get("status") or "").lower()
        error = _agy_text_from_value(result.get("error"))
        usage_evt = _agy_usage(result)
        if usage_evt:
            events.append(usage_evt)
        events.append({
            "type": "result",
            "is_error": bool(error) or status in {"error", "failed", "cancelled", "canceled"},
            "result_text": text,
            "error": error or None,
            "session_id": str(
                raw.get("conversation_id")
                or raw.get("conversationId")
                or raw.get("session_id")
                or raw.get("sessionId")
                or result.get("conversation_id")
                or result.get("conversationId")
                or result.get("session_id")
                or result.get("sessionId")
                or state.get("session_id")
                or ""
            ),
            "duration_ms": int(float(result.get("duration_seconds") or 0) * 1000),
            "num_turns": result.get("num_turns") or result.get("numTurns") or 0,
        })
        return events

    if event not in {"step_update", "stepupdate", "step"}:
        text = _agy_text_from_value(payload)
        if text:
            events.append({"type": "status", "text": text[:500]})
        return events

    usage_evt = _agy_usage(payload)
    if usage_evt:
        events.append(usage_evt)

    tool_evt = _agy_tool_event(payload)
    status = str(payload.get("status") or payload.get("state") or payload.get("phase") or "").lower()
    if tool_evt:
        raw_name, tool_id, args, result = tool_evt
        name = normalize_tool_name(raw_name)
        if tool_id not in state.setdefault("tool_starts", set()):
            state["tool_starts"].add(tool_id)
            normalized_input = json.dumps(
                normalize_tool_args(raw_name, args), ensure_ascii=False
            )
            events.append({
                "type": "tool_start",
                "name": name,
                "tool_id": tool_id,
            })
            # Match the provider-independent event contract used by Codex,
            # Claude, Goose, and Hermes.  The server persists and renders tool
            # input from this event; embedding it only on tool_start silently
            # dropped agy's command/path arguments from the user-facing turn.
            if normalized_input and normalized_input != "{}":
                events.append({
                    "type": "tool_input_delta",
                    "json": normalized_input,
                    "tool_id": tool_id,
                })
        if result or status in {"completed", "complete", "finished", "success", "succeeded", "failed", "error", "done"}:
            events.append({
                "type": "tool_result",
                "tool_id": tool_id,
                "content": result,
                "is_error": status in {"failed", "error"},
                "outcome_recorded": status in {"completed", "complete", "finished", "success", "succeeded", "failed", "error", "done"},
            })
        if raw_name in ("ask_question", "AskUserQuestion"):
            questions = args.get("questions") if isinstance(args, dict) else []
            events.append({
                "type": "ask_user_question",
                "questions": questions if isinstance(questions, list) else [],
                "tool_id": tool_id,
            })
        elif raw_name in ("ExitPlanMode", "exit_plan_mode"):
            events.append({
                "type": "plan_ready",
                "plan": args.get("plan", "") if isinstance(args, dict) else "",
                "plan_file": args.get("planFilePath", args.get("plan_file", "")) if isinstance(args, dict) else "",
                "tool_id": tool_id,
            })
        return events

    subagent = payload.get("subagent_info") or payload.get("subagentInfo") or payload.get("subagent")
    if isinstance(subagent, dict):
        name = subagent.get("name") or subagent.get("agent") or subagent.get("id") or "subagent"
        state_text = subagent.get("status") or subagent.get("state") or subagent.get("phase") or "updated"
        detail = _agy_text_from_value(
            subagent.get("message")
            or subagent.get("summary")
            or subagent.get("description")
        )
        events.append({
            "type": "status",
            "text": f"{name}: {state_text}{f' - {detail}' if detail else ''}",
        })
        return events

    thought = _agy_text_from_value(
        payload.get("thought")
        or payload.get("thought_delta")
        or payload.get("thoughtDelta")
        or payload.get("thinking")
        or payload.get("thinking_delta")
        or payload.get("thinkingDelta")
        or payload.get("reasoning")
        or payload.get("reasoning_delta")
        or payload.get("reasoningDelta")
    )
    if thought:
        events.append({"type": "thinking_delta", "text": thought})

    text = _agy_text_from_value(
        payload.get("delta")
        or payload.get("text_delta")
        or payload.get("textDelta")
        or payload.get("content_delta")
        or payload.get("contentDelta")
        or payload.get("text")
        or payload.get("response")
    )
    if text:
        events.append({"type": "text_delta", "text": text})
        return events

    message = _agy_text_from_value(
        payload.get("message")
        or payload.get("summary")
        or payload.get("description")
        or payload.get("content")
    )
    if message:
        events.append({"type": "status", "text": message[:500]})
    return events


async def run_gemini(prompt: str, cwd: str, conv_id: int = 0, server_port: int = 8000,
                     model: str = "Gemini 3.7 Flash (High)", effort: str = "high",
                     permission_mode: str = "default",
                     resume_session_id: str = None, fork_session: bool = False,
                     backstage_parent_id: int | None = None,
                     
                     is_first_turn: bool = True,
                     permission_scope: str = "",
                     agent_run_id: str | None = None,
                     preparation: bool = False,
                     print_timeout: str | None = None):
    if preparation:
        # Tool-free preparation: no Loom agent contract, no operator surface;
        # the PreToolUse hook (LOOM_PREPARATION=1) denies every tool call.
        pass
    elif permission_mode == "plan":
        plan_instruction = (
            "You are running in PLAN MODE. Your task is to analyze the codebase and write a comprehensive "
            "implementation plan to `implementation_plan.md` in the workspace. Do NOT modify any other files "
            "or run commands that modify the repository. Once the plan is written, present it to the user "
            "and ask for their approval. After writing the plan, end your turn immediately without performing "
            "any edits."
        )
        prompt = f"{plan_instruction}\n\n{prompt}"

    task = asyncio.current_task()
    gen_key = getattr(task, "_gen_key", None)
    effective_agent_run_id = agent_run_id or (f"gen:{gen_key[2]}" if gen_key else None)
    effective_parent_run_id = getattr(task, "_parent_run_id", None) or (
        f"gen:{gen_key[2]}" if gen_key and not agent_run_id else None
    )
    history_token = None
    if conv_id and (not preparation) and (not backstage_parent_id):
        from search_space.scopes import issue_scope
        history_token = issue_scope(conv_id, cwd, run_id=effective_agent_run_id,
                                    client_instance_id=getattr(task, "_client_instance_id", None))
    def _configure_workspace():
        # .agents/hooks.json and mcp_config.json are shared by every agy run in
        # this workspace and carry this run's identity, so they are written
        # under the workspace lock immediately before launch (see below).
        _configure_permission_hook(
            cwd,
            backstage_parent_id,
            server_port,
            conv_id=conv_id,
            agent_run_id=effective_agent_run_id,
            parent_run_id=effective_parent_run_id,
            client_instance_id=getattr(task, "_client_instance_id", None),
            history_token=history_token,
        )
        pass

    if preparation:
        prompt = _prepare_agy_prompt(
            prompt, backstage_parent_id=None, 
            is_first_turn=False,
        )
    else:
        prompt = _prepare_agy_prompt(
            prompt,
            backstage_parent_id,
            
            is_first_turn=is_first_turn,
        )

    agy_model = _loom_model_to_agy(model, effort)
    agy_effort = _agy_effort_for_model(model, effort)

    queue = asyncio.Queue()
    queue_scope = str(permission_scope or effective_agent_run_id or "").strip()
    queue_key = f"{int(conv_id)}:{queue_scope}" if queue_scope else int(conv_id)
    _active_queues[queue_key] = queue
    # Keep the legacy alias only while this is the sole queue for the
    # conversation.  New server code always supplies permission_scope.
    if int(conv_id) not in _active_queues:
        _active_queues[int(conv_id)] = queue

    # stdout-driven: no transcript tailing. Loom records the conversation_id
    # agy emits on stdout. Fresh/replay turns deliberately launch without
    # --conversation so Loom history is the only memory source for that turn;
    # native same-agy resumes pass a real resume_session_id below.
    import time as _time
    launch_ts = _time.time()

    # Setup paths for session forking (still needed for resume/fork parity).
    if sys.platform == "win32":
        home = Path(os.environ.get("USERPROFILE", Path.home()))
    else:
        home = Path.home()
    brain_path = home / ".gemini" / "antigravity-cli" / "brain"

    # Operator turns are fresh-conv-by-design: each turn reads a fresh tool
    # registry from .agents/mcp_config.json. See [[agy-operator-turn2-no-response]].
    pass

    # Fork session if requested: copy brain/<src> to a new UUID folder and
    # update the conversation DB/PB so --resume points at the fork.
    if resume_session_id and fork_session:
        # uuid is imported at module level; importing it here would make it a
        # function-local name and break the prompt-file path below.
        import shutil, sqlite3
        new_session_id = str(uuid.uuid4())
        src = brain_path / resume_session_id
        dst = brain_path / new_session_id
        if src.exists() and not dst.exists():
            try:
                shutil.copytree(src, dst)
                print(f"[AGY] Forked session {resume_session_id} to new session {new_session_id}")

                conversations_path = brain_path.parent / "conversations"
                if conversations_path.exists():
                    db_src = conversations_path / f"{resume_session_id}.db"
                    db_dst = conversations_path / f"{new_session_id}.db"
                    if db_src.exists():
                        shutil.copy2(db_src, db_dst)
                        for suffix in [".db-wal", ".db-shm"]:
                            sub_src = conversations_path / f"{resume_session_id}{suffix}"
                            sub_dst = conversations_path / f"{new_session_id}{suffix}"
                            if sub_src.exists():
                                try:
                                    shutil.copy2(sub_src, sub_dst)
                                except Exception as se:
                                    log.warning(f"[AGY] Failed to copy {suffix} file: {se}")
                        try:
                            conn = sqlite3.connect(str(db_dst))
                            cursor = conn.cursor()
                            cursor.execute(
                                "UPDATE trajectory_meta SET cascade_id = ? WHERE cascade_id = ?",
                                (new_session_id, resume_session_id),
                            )
                            conn.commit()
                            conn.close()
                            print(f"[AGY] Updated cascade_id in {db_dst.name}")
                        except Exception as sqle:
                            log.warning(f"[AGY] Failed to update sqlite DB: {sqle}")

                    pb_src = conversations_path / f"{resume_session_id}.pb"
                    pb_dst = conversations_path / f"{new_session_id}.pb"
                    if pb_src.exists():
                        try:
                            pb_data = pb_src.read_bytes()
                            pb_data_updated = pb_data.replace(
                                resume_session_id.encode("utf-8"),
                                new_session_id.encode("utf-8"),
                            )
                            pb_dst.write_bytes(pb_data_updated)
                            print(f"[AGY] Copied and updated protobuf file: {pb_dst.name}")
                        except Exception as pbe:
                            log.warning(f"[AGY] Failed to update protobuf file: {pbe}")

                resume_session_id = new_session_id
            except Exception as e:
                log.warning(f"[AGY] Failed to fork session: {e}")

    agy_exe = _find_agy_exe()

    if not backstage_parent_id:
        from context_runtime import admit_rendered
        prompt = await admit_rendered(prompt, model=model, harness="gemini", resume_session_id=resume_session_id)

    # Windows has a ~32K char limit on command lines (CreateProcess).  For
    # prompts that approach this, write the full text to a temp file and pass a
    # short redirect instruction via -p instead.
    _MAX_CLI_PROMPT = 28_000  # leave headroom for the rest of the args
    prompt_file: Path | None = None

    if len(prompt) > _MAX_CLI_PROMPT:
        agents_dir = Path(cwd) / ".agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        # One file per launch: a reused per-conversation name let a retry or
        # an overlapping turn overwrite or delete another turn's prompt.
        _sweep_stale_prompt_files(agents_dir)
        prompt_file = agents_dir / f"loom_prompt_{conv_id}_{uuid.uuid4().hex[:12]}.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        cli_prompt = (
            f"Read the full conversation context from the file at "
            f"{prompt_file.name} in the .agents directory and respond "
            f"to the latest user message. Do NOT summarize the file — "
            f"treat its contents as the conversation history and reply naturally."
        )
        print(f"[AGY] Prompt too large for CLI ({len(prompt)} chars), "
              f"wrote to {prompt_file}")
    else:
        cli_prompt = prompt

    # Operator turns: do NOT pass --conversation. Each operator turn is a
    # self-contained scan/triage request; resuming the agy conversation
    # forces agy to use the tool registry baked in at conv-creation time
    # own context fill up across turns until it auto-compacts on what feels
    # to the user like turn 1. Fresh conv per turn → fresh tool registry
    # read from .agents/mcp_config.json, no carry-over compaction.
    # Broader rule: no resume_session_id means no --conversation.
    # That keeps full Loom replay/handoff prompts from mixing with agy's
    # persistent conversation memory.
    cc_args = []
    if resume_session_id:
        cc_args += ["--conversation", resume_session_id]

    # Explicitly set the agy project ID so MCP config from .agents/ loads.
    # agy's default-project cache (cache/default_project_id.txt) gets clobbered
    # by neutral runs; without --project, new operator conversations fall into
    # the "default-cli-project" which has no MCP servers configured.
    project_id = _get_agy_project_id(_agy_workspace_root(Path(cwd)))
    if project_id:
        cc_args += ["--project", project_id]
    elif not resume_session_id:
        # A fresh workspace must not inherit default-cli-project, whose
        # customization paths can omit this workspace's permission hooks.
        cc_args += ["--new-project"]

    cc_args += [
        "-p", cli_prompt,
        "--output-format", "stream-json",
        "--model", agy_model,
        "--effort", agy_effort,
        "--dangerously-skip-permissions",
    ]
    agy_timeout = (print_timeout or os.environ.get("LOOM_AGY_PRINT_TIMEOUT", "")).strip()
    if agy_timeout:
        cc_args += ["--print-timeout", agy_timeout]

    # NOTE: --sandbox is deliberately not passed for operator convs: bare
    # headless smoke runs hang with AND without the flag outside the Loom
    # harness (2026-06-11), so it cannot be validated. The PreToolUse hook
    # deny-list is the tool-blocking layer and does not depend on it
    # (--dangerously-skip-permissions only skips agy's own approvals, never
    # the hook — same combination backstage mode relies on).

    env = {
        **os.environ,
        "LOOM_CONV_ID": str(conv_id),
        "LOOM_PORT": str(server_port),
        "BASH_DEFAULT_TIMEOUT_MS": "1200000",  # 20 minutes (default is 2m)
        "BASH_MAX_TIMEOUT_MS": "3600000",      # 60 minutes (default is 10m)
    }
    if agent_run_id:
        env["LOOM_AGENT_RUN_ID"] = str(agent_run_id)
    gen_key = getattr(asyncio.current_task(), "_gen_key", None)
    if permission_scope:
        env["LOOM_PERMISSION_SCOPE"] = str(permission_scope)
    elif gen_key:
        env["LOOM_PERMISSION_SCOPE"] = f"gen:{gen_key[2]}"
    client_instance_id = getattr(task, "_client_instance_id", None)
    if client_instance_id:
        env["LOOM_CLIENT_INSTANCE_ID"] = str(client_instance_id)
    if backstage_parent_id:
        env["LOOM_BACKSTAGE_PARENT_ID"] = str(backstage_parent_id)
    pass
    if preparation:
        # Deny every tool through the PreToolUse hook; preparation is
        # text-only and must never receive a permission prompt.
        env["LOOM_PREPARATION"] = "1"

    # launch_ts already recorded above

    cmd = [agy_exe] + cc_args
    print(f"[AGY] CMD: {' '.join(cmd[:6])}{'...' if len(cmd) > 6 else ''}")
    print(f"[AGY] agy_model={agy_model}, prompt_len={len(prompt)}, cli_len={len(cli_prompt)}")

    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000 | 0x00000200

    config_lock = await _acquire_workspace_config_lock(cwd)
    try:
        _configure_workspace()
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
            **kwargs
        )
    except BaseException:
        config_lock.release()
        raise
    first_output = asyncio.Event()
    asyncio.create_task(_release_config_lock_when_loaded(config_lock, first_output, proc))

    # stdout/stderr reading tasks. agy stream-json emits one JSON event per
    # stdout line; stderr remains noisy diagnostics only.
    stdout_lines: asyncio.Queue[str | None] = asyncio.Queue()
    stderr_lines: list[str] = []

    async def _read_stderr():
        try:
            async for line in proc.stderr:
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    stderr_lines.append(text)
                    print(f"[AGY-stderr] {text}")
        except Exception as e:
            log.error(f"[AGY] Error reading stderr: {e}")
    stderr_task = asyncio.create_task(_read_stderr())

    async def _read_stdout():
        try:
            async for line in proc.stdout:
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    first_output.set()
                    stdout_lines.put_nowait(text)
        except Exception as e:
            log.error(f"[AGY] Error reading stdout: {e}")
        finally:
            stdout_lines.put_nowait(None)
    stdout_task = asyncio.create_task(_read_stdout())

    # Clean up the temp prompt file when the process finishes
    if prompt_file:
        async def _cleanup_prompt_file():
            await proc.wait()
            try:
                prompt_file.unlink(missing_ok=True)
                print(f"[AGY] Cleaned up prompt file: {prompt_file}")
            except Exception as e:
                log.warning(f"[AGY] Failed to clean up prompt file: {e}")
        asyncio.create_task(_cleanup_prompt_file())

    # stream-json event producer. This is agy's structured print-mode contract.
    async def _produce_stream_json_events():
        session_id = resume_session_id or ""
        queue.put_nowait({
            "type": "session_info",
            "session_id": session_id,
            "requested_model": model,
            "model": agy_model,
            "model_confirmed": False,
            "model_attestation": _agy_model_attestation(
                model,
                agy_model,
                None,
                source="agy_cli_launch_arguments",
                session_id=session_id,
            ),
        })

        full_text = ""
        _empty_polls = 0
        stream_state = {"session_id": session_id, "model": agy_model}
        got_result = False
        terminal_error = None
        while True:
            try:
                line = await asyncio.wait_for(stdout_lines.get(), timeout=1.5)
            except asyncio.TimeoutError:
                line = ""

            if line is None:
                break
            if line:
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    queue.put_nowait({"type": "status", "text": line[:500]})
                    continue
                for evt in _agy_step_events(raw, stream_state):
                    if evt.get("type") == "session_info":
                        stream_state["session_id"] = evt.get("session_id") or stream_state["session_id"]
                        stream_state["model"] = evt.get("model") or stream_state["model"]
                        if evt.get("model_confirmed"):
                            evt["model_attestation"] = _agy_model_attestation(
                                model,
                                agy_model,
                                evt.get("model"),
                                source="antigravity_stream_init",
                                session_id=stream_state["session_id"],
                            )
                    elif evt.get("type") == "text_delta":
                        full_text += evt.get("text", "")
                    elif evt.get("type") == "result":
                        got_result = True
                        if evt.get("result_text"):
                            if not full_text:
                                full_text = evt["result_text"]
                        elif full_text:
                            evt["result_text"] = full_text.strip()
                    queue.put_nowait(evt)
                if got_result:
                    break
                continue

            # Liveness heartbeat and error detection while still running.
            _empty_polls += 1
            if _empty_polls % _AGY_HEARTBEAT_POLLS == 0:
                now = _time.time()
                elapsed = int(now - launch_ts)
                # Specific fatal signatures (quota, auth, planner stall) win
                # over the generic liveness verdict below.
                log_err = _scan_agy_log_for_error(launch_ts)
                if log_err:
                    print(f"[AGY] Live log diagnosis ({elapsed}s): {log_err}")
                    terminal_error = log_err
                    queue.put_nowait({"type": "status", "text": log_err})
                    await _stop_agy_process(proc, grace=5.0)
                    break

                log_state = _agy_log_state(launch_ts)
                quiet_s = log_state["quiet_s"]
                if quiet_s is None:
                    quiet_s = now - launch_ts
                stop_reason = None
                if log_state["fresh"]:
                    if not full_text:
                        queue.put_nowait({
                            "type": "status",
                            "text": f"Working ({elapsed}s) — agy is processing...",
                        })
                elif log_state["shutdown"] and quiet_s > _AGY_ZOMBIE_QUIET_S:
                    stop_reason = (
                        "Antigravity tore down its conversation stream but its "
                        "process neither exited nor returned a result; Loom "
                        f"stopped it after {int(quiet_s)}s of log silence")
                elif quiet_s > _AGY_QUIET_CEILING_S:
                    stop_reason = (
                        f"Loom stopped Antigravity after {int(quiet_s)}s with no "
                        "log or stream activity (hard limit "
                        f"{int(_AGY_QUIET_CEILING_S)}s); the model call may have hung")
                else:
                    queue.put_nowait({
                        "type": "status",
                        "text": (f"Working ({elapsed}s) — agy log quiet for "
                                 f"{int(quiet_s)}s; model likely generating..."),
                    })
                if stop_reason:
                    terminal_error = stop_reason
                    print(f"[AGY] Liveness stop ({elapsed}s): {stop_reason}")
                    queue.put_nowait({"type": "status", "text": stop_reason})
                    await _stop_agy_process(proc)
                    break

            await asyncio.sleep(0.05)

        # Ensure stdout reader finished so all output is captured.
        if not stdout_task.done():
            try:
                await asyncio.wait_for(stdout_task, timeout=5.0)
            except asyncio.TimeoutError:
                stdout_task.cancel()

        post_err = terminal_error
        if not got_result:
            if not post_err:
                post_err = _scan_agy_log_for_error(launch_ts)
                if post_err:
                    print(f"[AGY] CLI log diagnosis: {post_err}")
                elif proc.returncode != 0:
                    post_err = f"Antigravity exited with code {proc.returncode}"
                elif not full_text:
                    post_err = "Antigravity exited cleanly but produced no stream-json result"
                    print(f"[AGY] CLI log diagnosis: {post_err}")

            queue.put_nowait({
                "type": "result",
                "is_error": bool(post_err) or proc.returncode != 0,
                "result_text": full_text.strip(),
                "error": post_err,
                "session_id": stream_state.get("session_id") or session_id,
            })
        queue.put_nowait(None)

    asyncio.create_task(_produce_stream_json_events())

    async def _event_stream():
        got_result = False
        queue_done = False
        
        yielded_starts = set()
        yielded_results = set()
        yielded_questions = set()
        yielded_plans = set()
        
        while not (queue_done and queue.empty()):
            try:
                evt = await queue.get()
                if evt is None:
                    queue_done = True
                    continue
                
                etype = evt.get("type")
                tool_id = evt.get("tool_id")
                
                if etype == "tool_start":
                    if tool_id in yielded_starts:
                        continue
                    yielded_starts.add(tool_id)
                    
                elif etype == "tool_result":
                    if tool_id in yielded_results:
                        continue
                    if evt.get("outcome_recorded", True):
                        yielded_results.add(tool_id)
                    
                elif etype == "ask_user_question":
                    if tool_id in yielded_questions:
                        continue
                    yielded_questions.add(tool_id)
                    
                elif etype == "plan_ready":
                    if tool_id in yielded_plans:
                        continue
                    yielded_plans.add(tool_id)
                    
                if evt.get("type") == "result":
                    got_result = True
                yield evt
            except asyncio.CancelledError:
                break

        _active_queues.pop(queue_key, None)
        if _active_queues.get(int(conv_id)) is queue:
            _active_queues.pop(int(conv_id), None)
        # Ensure stdout/stderr readers finish before reaping the process so we
        # capture all output and avoid a blocked-pipe deadlock on Windows.
        if not stdout_task.done():
            try:
                await asyncio.wait_for(stdout_task, timeout=5.0)
            except asyncio.TimeoutError:
                stdout_task.cancel()
        if not stderr_task.done():
            try:
                await asyncio.wait_for(stderr_task, timeout=5.0)
            except asyncio.TimeoutError:
                stderr_task.cancel()
        await proc.wait()

        # Emit default result if not already yielded
        if not got_result:
            yield {
                "type": "result",
                "is_error": proc.returncode != 0,
                "result_text": "",
                "session_id": resume_session_id or str(conv_id),
            }

    return proc, _event_stream()


async def cancel_gemini(proc):
    if proc.returncode is None:
        if sys.platform == 'win32':
            import subprocess
            try: subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], capture_output=True, timeout=5)
            except: proc.kill()
        else:
            proc.terminate()
        try: await asyncio.wait_for(proc.wait(), timeout=5)
        except: proc.kill()
