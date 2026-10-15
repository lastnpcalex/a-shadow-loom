"""ChatGPT Codex app-server subprocess wrapper.

Runs Codex through the app-server JSONL protocol so Loom can broker live
approval requests instead of using non-interactive `codex exec`.
"""

import asyncio
from html import escape
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from loom_agent_prompt import loom_agent_contract_block, merge_system_prompts

log = logging.getLogger(__name__)

_IGNORED_APP_SERVER_EVENTS = {
    "account/rateLimits/updated",
    "remoteControl/status/changed",
    "serverRequest/resolved",
    "turn/started",
    "thread/tokenUsage/updated",
    "thread.tokenUsage.updated",
}

_IGNORED_APP_SERVER_ITEM_TYPES = {
    "agentMessage",
    "reasoning",
    "userMessage",
}


def _toml_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_literal(v) for v in values) + "]"


def _codex_config_args(key: str, value: str) -> list[str]:
    return ["-c", f"{key}={value}"]




def _web_tools_mcp_config() -> dict:
    'Keyless web_search/web_fetch (DuckDuckGo + trafilatura) for operators.\n\n    sources on the open web but codex has no Anthropic WebSearch/WebFetch.\n    '
    script = Path(__file__).parent / "mcp_web_tools.py"
    if not script.is_file():
        return {}
    return {
        "command": sys.executable,
        "args": [str(script)],
        "startup_timeout_sec": 20.0,
        "tool_timeout_sec": 600.0,
        "default_tools_approval_mode": "approve",
    }


def _agent_control_mcp_config(
    conv_id: int,
    server_port: int,
    *,
    agent_run_id: str | None = None,
    parent_run_id: str | None = None,
    client_instance_id: str | None = None,
    workspace_root: str | None = None,
    local_search: bool = False,
    history_token: str | None = None,
) -> dict:
    """Configure Loom's portable child-run control surface."""
    script = Path(__file__).parent / "mcp_loom_agents.py"
    if not script.is_file():
        return {}
    env = {
        "LOOM_CONV_ID": str(conv_id),
        "LOOM_PORT": str(server_port),
        "LOOM_WORKSPACE_ROOT": str(workspace_root or Path.cwd()),
    }
    if conv_id and workspace_root:
        from search_space.scopes import issue_scope
        env["LOOM_HISTORY_TOKEN"] = history_token or issue_scope(
            conv_id, workspace_root, local=local_search,
            run_id=agent_run_id or parent_run_id, client_instance_id=client_instance_id,
        )
    if agent_run_id:
        env["LOOM_AGENT_RUN_ID"] = str(agent_run_id)
    if parent_run_id:
        env["LOOM_PARENT_RUN_ID"] = str(parent_run_id)
    if client_instance_id:
        env["LOOM_CLIENT_INSTANCE_ID"] = str(client_instance_id)
    return {
        "command": sys.executable,
        "args": [str(script)],
        "env": env,
        "startup_timeout_sec": 20.0,
        "tool_timeout_sec": 1200.0,
        "default_tools_approval_mode": "approve",
    }




def _codex_developer_instructions(
    configured_instructions: str | None = None,
    backstage_parent_id: int | None = None,
    
) -> str | None:
    """Merge Loom into Codex's additive developer-instruction channel.

    ``baseInstructions`` replaces Codex's built-in model instructions and must
    not be used here.  ``developerInstructions`` is a separate developer-role
    message that Codex carries with the native thread and restores after
    compaction.  Preserve any user/project developer instructions resolved by
    ``config/read`` instead of shadowing them with Loom's request override.
    """
    if backstage_parent_id:
        # Dedicated roles already receive their own instructions. Omitting the
        # request override lets Codex resolve normal config layers itself.
        return None
    return merge_system_prompts(
        configured_instructions,
        loom_agent_contract_block("codex"),
    ) or None


def _configured_developer_instructions(config_response: dict) -> str | None:
    """Extract effective developer instructions from a config/read response."""
    result = config_response.get("result") if isinstance(config_response, dict) else None
    config = result.get("config") if isinstance(result, dict) else None
    value = config.get("developer_instructions") if isinstance(config, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def _thread_mcp_servers(
    conv_id: int,
    server_port: int,
    
    *,
    agent_run_id: str | None = None,
    parent_run_id: str | None = None,
    client_instance_id: str | None = None,
    workspace_root: str | None = None,
) -> dict:
    "Return Loom's thread-scoped MCP surface for Codex.\n\n    "
    control = _agent_control_mcp_config(
        conv_id,
        server_port,
        agent_run_id=agent_run_id,
        parent_run_id=parent_run_id,
        client_instance_id=client_instance_id,
        workspace_root=workspace_root,
    )
    return {"loom-agent-control": control} if control else {}



def _mcp_server_config_args(name: str, config: dict) -> list[str]:
    if not config:
        return []
    args: list[str] = []
    server_key = f'mcp_servers."{name}"'
    args += _codex_config_args(f"{server_key}.command", _toml_literal(config["command"]))
    args += _codex_config_args(f"{server_key}.args", _toml_array(config["args"]))
    if config.get("required"):
        args += _codex_config_args(f"{server_key}.required", "true")
    if "startup_timeout_sec" in config:
        args += _codex_config_args(f"{server_key}.startup_timeout_sec", str(config["startup_timeout_sec"]))
    if "tool_timeout_sec" in config:
        args += _codex_config_args(f"{server_key}.tool_timeout_sec", str(config["tool_timeout_sec"]))
    if "default_tools_approval_mode" in config:
        args += _codex_config_args(
            f"{server_key}.default_tools_approval_mode",
            _toml_literal(config["default_tools_approval_mode"]),
        )
    for key, value in config.get("env", {}).items():
        args += _codex_config_args(f"{server_key}.env.{key}", _toml_literal(value))
    return args


def _codex_tool_name(item: dict) -> str:
    """Return a stable Loom-facing name for Codex tool items."""
    item_type = item.get("type")
    if item_type in ("command_execution", "commandExecution", "local_shell_call", "localShellCall"):
        return "Bash"
    if item_type in ("file_change", "fileChange"):
        return "Edit"
    if item_type in ("mcp_tool_call", "mcpToolCall"):
        server = item.get("server") or "mcp"
        tool = item.get("tool") or item.get("toolName") or "tool"
        return f"{server}.{tool}"
    if item_type in ("dynamic_tool_call", "dynamicToolCall"):
        namespace = item.get("namespace")
        tool = item.get("tool") or item.get("toolName") or "tool"
        return f"{namespace}.{tool}" if namespace else tool
    return (
        item.get("name")
        or item.get("tool_name")
        or item.get("function_name")
        or item.get("call_name")
        or item_type
        or "tool"
    )


def _is_codex_tool_item(item: dict) -> bool:
    """True for Codex item types that should render as Loom tool blocks."""
    item_type = item.get("type")
    return item_type in {
        "mcp_tool_call",
        "mcpToolCall",
        "dynamic_tool_call",
        "dynamicToolCall",
        "command_execution",
        "commandExecution",
        "local_shell_call",
        "localShellCall",
        "function_call",
        "tool_call",
        "custom_tool_call",
        "file_change",
        "fileChange",
    } or any(key in item for key in ("tool_name", "function_name", "call_id"))


def _codex_item_id(item: dict, raw: dict) -> str:
    """Pick the most stable id available for matching start/result events."""
    return str(
        item.get("id")
        or item.get("call_id")
        or item.get("tool_call_id")
        or item.get("item_id")
        or raw.get("item_id")
        or raw.get("timestamp")
        or ""
    )


def _text_from_value(value) -> str:
    """Extract readable text from common Codex scalar/list/dict content shapes."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "".join(_text_from_value(part) for part in value)
    if isinstance(value, dict):
        for key in ("text", "content", "output_text", "message", "summary"):
            text = _text_from_value(value.get(key))
            if text:
                return text
        if "error" in value:
            return _text_from_value(value["error"])
        try:
            return json.dumps(value, indent=2)
        except TypeError:
            return str(value)
    return str(value)


def _codex_item_text(item: dict) -> str:
    """Extract assistant/reasoning text from current and likely Codex item shapes."""
    for key in ("text", "content", "output_text", "message", "summary"):
        text = _text_from_value(item.get(key))
        if text:
            return text
    return ""


def _codex_tool_input(item: dict) -> str:
    """Format Codex tool input for Loom's tool detail panel."""
    item_type = item.get("type")
    if item_type in ("command_execution", "commandExecution", "local_shell_call", "localShellCall"):
        command = item.get("command") or item.get("cmd") or item.get("script") or ""
        return json.dumps({"command": command}, indent=2) if command else ""
    if item_type in ("file_change", "fileChange"):
        changes = item.get("changes") or item.get("fileChanges") or []
        return json.dumps({"changes": changes}, indent=2)
    args = None
    for key in ("arguments", "input", "tool_input", "params", "parameters", "args"):
        if key in item:
            args = item.get(key)
            break
    if args is not None:
        if isinstance(args, str):
            return args
        try:
            return json.dumps(args, indent=2)
        except TypeError:
            return str(args)
    return ""


def _codex_tool_output(item: dict) -> str:
    """Extract Codex tool output regardless of item type."""
    if item.get("type") in ("file_change", "fileChange"):
        changes = item.get("changes") or item.get("fileChanges") or []
        try:
            return json.dumps(changes, indent=2)
        except TypeError:
            return str(changes)
    for key in ("aggregatedOutput", "aggregated_output", "formatted_output", "output", "result", "text", "content", "error"):
        value = item.get(key)
        if value is not None:
            return _text_from_value(value)
    return ""


def _codex_tool_failed(item: dict) -> bool:
    """A rejected tool is not successful, nor necessarily a failed whole turn."""
    exit_code = item.get("exitCode", item.get("exit_code"))
    return item.get("status") in {"failed", "declined"} or exit_code not in (None, 0)


def _codex_diff_payload(raw: dict) -> dict:
    params = raw.get("params") or {}
    diff = (
        params.get("diff")
        or params.get("unifiedDiff")
        or params.get("unified_diff")
        or params.get("patch")
        or raw.get("diff")
        or raw.get("patch")
        or ""
    )
    files = (
        params.get("files")
        or params.get("fileChanges")
        or params.get("changes")
        or raw.get("files")
        or raw.get("fileChanges")
        or raw.get("changes")
        or []
    )
    return {
        "kind": "codex_diff",
        "threadId": params.get("threadId") or raw.get("threadId"),
        "turnId": params.get("turnId") or raw.get("turnId"),
        "diff": diff,
        "files": files,
    }


def _codex_diff_tool_id(raw: dict) -> str:
    params = raw.get("params") or {}
    ident = (
        params.get("turnId")
        or params.get("threadId")
        or raw.get("turnId")
        or raw.get("threadId")
        or "thread"
    )
    return f"codex-diff:{ident}"


def _codex_usage(raw: dict) -> dict | None:
    """Normalize token usage from turn/item payloads when Codex includes it."""
    params = raw.get("params") or {}
    candidates = [
        raw.get("usage"),
        raw.get("token_usage"),
        raw.get("tokenUsage"),
        raw.get("metrics", {}).get("usage") if isinstance(raw.get("metrics"), dict) else None,
        params.get("usage") if isinstance(params, dict) else None,
        params.get("token_usage") if isinstance(params, dict) else None,
        params.get("tokenUsage") if isinstance(params, dict) else None,
    ]
    for container_name in ("turn", "thread", "message", "item"):
        container = params.get(container_name) if isinstance(params, dict) else None
        if isinstance(container, dict):
            candidates.extend([
                container.get("usage"),
                container.get("token_usage"),
                container.get("tokenUsage"),
            ])
    usage = next((candidate for candidate in candidates if isinstance(candidate, dict)), None)
    if not usage:
        return None

    # Current app-server envelopes distinguish the last request's context
    # from cumulative session billing. Never display 'total' as context size.
    if isinstance(usage.get("last"), dict):
        last = usage["last"]
        total = usage.get("total") or {}
        return {
            "type": "usage",
            "input_tokens": int(last.get("inputTokens") or 0),
            "output_tokens": int(total.get("outputTokens") or 0),
            "_first_output_tokens": int(last.get("outputTokens") or 0),
            "context_window": usage.get("modelContextWindow"),
        }

    input_tokens = (
        usage.get("input_tokens")
        or usage.get("inputTokens")
        or usage.get("prompt_tokens")
        or usage.get("promptTokens")
        or usage.get("total_input_tokens")
        or usage.get("totalInputTokens")
        or 0
    )
    output_tokens = (
        usage.get("output_tokens")
        or usage.get("outputTokens")
        or usage.get("completion_tokens")
        or usage.get("completionTokens")
        or usage.get("total_output_tokens")
        or usage.get("totalOutputTokens")
        or 0
    )
    if not input_tokens and not output_tokens:
        return None
    return {
        "type": "usage",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _codex_status_payload(raw: dict, params: dict | None = None) -> dict:
    """Return the nested status-bearing payload from app-server notifications."""
    params = params or raw.get("params") or {}
    for container in (
        params.get("turn"),
        raw.get("turn"),
        params.get("thread"),
        raw.get("thread"),
        params,
        raw,
    ):
        if isinstance(container, dict):
            return container
    return {}


def _codex_status_value(raw: dict, params: dict | None = None) -> str:
    payload = _codex_status_payload(raw, params)
    value = (
        payload.get("status")
        or payload.get("state")
        or payload.get("phase")
        or payload.get("lifecycle")
        or ""
    )
    if isinstance(value, dict):
        value = (
            value.get("type")
            or value.get("status")
            or value.get("state")
            or value.get("phase")
            or value.get("kind")
            or ""
        )
    return str(value).lower()


def _codex_turn_id(raw: dict, params: dict | None = None) -> str:
    params = params or raw.get("params") or {}
    payload = _codex_status_payload(raw, params)
    return str(
        payload.get("id")
        or payload.get("turnId")
        or payload.get("turn_id")
        or raw.get("turnId")
        or raw.get("turn_id")
        or params.get("turnId")
        or params.get("turn_id")
        or ""
    )


def _codex_terminal_status(raw: dict, params: dict | None = None) -> tuple[bool, bool, str]:
    """Return (is_terminal, is_error, message) for status notifications."""
    params = params or raw.get("params") or {}
    etype = raw.get("type") or raw.get("method") or ""
    status = _codex_status_value(raw, params)
    # A thread is a long-lived native conversation, not one model turn.
    # App-server may report it idle between tool/image-view phases while the
    # active turn is still running. Treating that as turn completion caused
    # Loom to kill the app-server and record a synthetic user interruption.
    # Only an exact turn terminal event may finalize an active generation.
    if etype in ("thread/status/changed", "thread.status.changed"):
        return False, False, ""
    if etype in ("turn/status/changed", "turn.status.changed"):
        if status in ("completed", "complete", "finished", "success", "succeeded"):
            return True, False, ""
        if status in ("failed", "error", "cancelled", "canceled", "interrupted"):
            return True, True, _text_from_value(
                params.get("error") or raw.get("error") or "Codex turn failed"
            )
    return False, False, ""


def _codex_completed_turn_error(raw: dict, params: dict | None = None) -> str:
    """Extract an error from a terminal turn payload before treating it as success."""
    params = params or raw.get("params") or {}
    turn = params.get("turn") or raw.get("turn") or {}
    error = (
        turn.get("error")
        or params.get("error")
        or raw.get("error")
    )
    if error:
        return _text_from_value(error)
    status = _codex_status_value(raw, params)
    if status in {"failed", "error", "cancelled", "canceled", "interrupted"}:
        return f"Codex turn {status}"
    return ""


def _find_codex_exe() -> str:
    """Find the codex executable on PATH or in known install locations."""
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        # 1. Standard installer path:
        known = os.path.join(local_app_data, "Programs", "OpenAI", "Codex", "bin", "codex.exe")
        if os.path.exists(known):
            return known
        
        # 2. User profile fallback (current bin):
        home = os.environ.get("USERPROFILE", "")
        known_home = os.path.join(home, ".codex", "packages", "standalone", "current", "bin", "codex.exe")
        if os.path.exists(known_home):
            return known_home
            
        # 3. User profile fallback (current root):
        known_home_alt = os.path.join(home, ".codex", "packages", "standalone", "current", "codex.exe")
        if os.path.exists(known_home_alt):
            return known_home_alt

        # 4. Search PATH using shutil.which (check codex.cmd first for npm, then codex.exe)
        import shutil
        cmd_path = shutil.which("codex.cmd")
        if cmd_path:
            return cmd_path
        
        exe_path = shutil.which("codex.exe")
        if exe_path:
            return exe_path
            
        fallback_path = shutil.which("codex")
        if fallback_path:
            return fallback_path
            
        return "codex.cmd"
    return "codex"


_CODEX_MODEL_SLUG_RE = re.compile(r"gpt-[0-9][A-Za-z0-9_.-]*", re.IGNORECASE)


def _normalize_codex_model_selector(model: str) -> str:
    raw = str(model or "").strip()
    lowered = raw.lower()
    if lowered.startswith("codex-"):
        raw = raw[len("codex-"):].strip()
    elif lowered.startswith("codex:"):
        raw = raw.split(":", 1)[1].strip()
    elif lowered.startswith("codex "):
        raw = raw.split(None, 1)[1].strip()

    match = _CODEX_MODEL_SLUG_RE.search(raw)
    if match:
        return match.group(0).lower()
    return raw


def codex_models_cache_path() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".codex" / "models_cache.json"


def _parse_codex_models_json(data: dict) -> list[dict]:
    models = []
    for m in data.get("models", []):
        slug = m.get("slug") or m.get("id") or m.get("model")
        display_name = m.get("display_name") or m.get("displayName") or slug
        if (
            slug
            and display_name
            and m.get("visibility", "list") != "hide"
            and m.get("supported_in_api", True) is not False
        ):
            models.append({
                "value": f"codex-{slug}",
                "label": f"Codex ({display_name})",
            })
    return models


_CODEX_STREAM_LIMIT = 128 * 1024 * 1024  # 128 MiB buffer for app-server JSON-RPC lines
_CODEX_COMPLETION_RECHECK_SEC = 15.0


async def discover_codex_models(force: bool = False) -> list[dict]:
    """Query `codex app-server` via JSON-RPC `model/list` or read cached models.

    Returns dropdown model rows: [{"value": "codex-...", "label": "Codex (...)"}].
    """
    import time
    cache_path = codex_models_cache_path()
    if not force and cache_path.exists():
        try:
            st = cache_path.stat()
            if time.time() - st.st_mtime < 3600.0:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                models = _parse_codex_models_json(data)
                if models:
                    return models
        except Exception as e:
            log.debug(f"[CODEX] Failed reading models cache: {e}")

    # Active discovery via lightweight codex app-server
    codex_exe = _find_codex_exe()
    proc = None
    try:
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000 | 0x00000200
        proc = await asyncio.create_subprocess_exec(
            codex_exe, "app-server", "--stdio", "--disable", "hooks",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_CODEX_STREAM_LIMIT,
            **kwargs,
        )

        async def _rpc_call(req_id: int, method: str, params: dict | None = None) -> dict:
            line = json.dumps({"id": req_id, "method": method, "params": params or {}}) + "\n"
            proc.stdin.write(line.encode("utf-8"))
            await proc.stdin.drain()
            while True:
                resp_line = await proc.stdout.readline()
                if not resp_line:
                    raise EOFError("codex app-server closed stdout")
                try:
                    msg = json.loads(resp_line.decode("utf-8", errors="replace"))
                    if msg.get("id") == req_id:
                        return msg
                except json.JSONDecodeError:
                    continue

        async def _notify(method: str, params: dict | None = None) -> None:
            line = json.dumps({"method": method, "params": params or {}}) + "\n"
            proc.stdin.write(line.encode("utf-8"))
            await proc.stdin.drain()

        async with asyncio.timeout(15.0):
            await _rpc_call(1, "initialize", {
                "clientInfo": {"name": "Loom", "version": "1"},
                "capabilities": {"experimental": True},
            })
            await _notify("initialized")
            list_resp = await _rpc_call(2, "model/list", {})

        raw_models = list_resp.get("result", {}).get("data", [])
        if raw_models:
            cache_items = []
            for m in raw_models:
                slug = m.get("id") or m.get("model")
                display_name = m.get("displayName") or m.get("display_name") or slug
                cache_items.append({
                    "slug": slug,
                    "display_name": display_name,
                    "description": m.get("description", ""),
                })
            try:
                import tempfile
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache_path.parent,
                                                 suffix=".tmp", delete=False) as output:
                    temp_p = Path(output.name)
                    json.dump({"models": cache_items, "fetched_at": time.time()}, output, indent=2)
                temp_p.replace(cache_path)
            except Exception as ce:
                log.debug(f"[CODEX] Failed saving models_cache.json: {ce}")

            return _parse_codex_models_json({"models": cache_items})
    except Exception as e:
        err_detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        log.warning(f"[CODEX] Active model discovery failed: {err_detail}")
    finally:
        if proc is not None:
            try:
                await cancel_codex(proc)
            except Exception:
                pass

    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            return _parse_codex_models_json(data)
        except Exception:
            pass
    return []


def _default_codex_model() -> str:
    env_default = os.environ.get("LOOM_CODEX_DEFAULT_MODEL") or os.environ.get("CODEX_MODEL")
    if env_default and env_default.strip():
        return _normalize_codex_model_selector(env_default)

    cache_path = Path(os.environ.get("USERPROFILE") or Path.home()) / ".codex" / "models_cache.json"
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        for entry in data.get("models", []):
            slug = entry.get("slug")
            if (
                slug
                and entry.get("visibility", "list") != "hide"
                and entry.get("supported_in_api", True) is not False
            ):
                return _normalize_codex_model_selector(slug)
    except Exception:
        pass

    return "gpt-5.6-sol"


def _loom_model_to_codex(model: str) -> str:
    """Map Loom's model selection to Codex model identifiers without silent downgrades."""
    normalized = _normalize_codex_model_selector(model)
    return normalized or _default_codex_model()


def _codex_reasoning_effort(effort: str | None) -> str | None:
    """Return an app-server reasoning effort without dropping current Codex values."""
    normalized = str(effort or "").strip().lower()
    if normalized == "minimal":
        normalized = "low"
    if normalized in {"none", "low", "medium", "high", "xhigh", "max", "ultra"}:
        return normalized
    return None


def _codex_model_attestation(
    requested_model: str,
    launch_model: str,
    thread_result: dict | None,
    *,
    session_id: str = "",
    app_server_user_agent: str = "",
) -> dict:
    """Build the model chain-of-custody returned by Codex app-server.

    ``requested_model`` is Loom's UI selector. ``launch_model`` is the exact
    normalized value sent to app-server. The effective model and provider are
    intentionally read only from the thread start/resume/fork response; never
    substitute the requested value when app-server did not attest one.
    """
    result = thread_result if isinstance(thread_result, dict) else {}
    effective_model = str(result.get("model") or "").strip()
    model_provider = str(result.get("modelProvider") or "").strip()

    if effective_model and effective_model.casefold() != launch_model.casefold():
        status = "mismatch"
    elif effective_model and model_provider:
        status = "verified"
    else:
        status = "unverified"

    return {
        "status": status,
        "harness": "Codex app-server",
        "requested_model": str(requested_model or "").strip(),
        "launch_model": launch_model,
        "effective_model": effective_model or None,
        "model_provider": model_provider or None,
        "source": "codex_app_server_thread_response",
        "verification_level": "harness",
        "session_id": session_id or None,
        "thread_id": session_id or None,
        "app_server": app_server_user_agent or None,
        "fallback_allowed": False,
    }


def _codex_approval_policy(permission_mode: str | None) -> str:
    """Map Loom's permission selector to Codex's approval policy.

    Codex Plan mode is a collaboration mode, not an approval policy. Loom's
    app-server integration does not currently set that collaboration mode, so
    any legacy "plan" value is treated like the interactive default.
    """
    mode = (permission_mode or "default").lower()
    if mode in ("never", "none"):
        return "never"
    if mode in ("on-request", "request"):
        return "on-request"
    return "on-request"


def _codex_launch_policies(permission_mode: str | None) -> tuple[str, str]:
    '(approval_policy, sandbox_mode) for a codex launch.\n\n    Operator threads: read-only sandbox with approvalPolicy=never, so write\n    and escalation attempts fail instead of raising a clickable prompt.\n    Codex cannot drop its shell, so the guarantee is "shell exists but\n    cannot write"; posterior commits still raise their own Loom approval\n    '
    pass
    return _codex_approval_policy(permission_mode), "workspace-write"


def _codex_thread_request(
    cwd: str,
    codex_model: str,
    approval_policy: str,
    sandbox_mode: str,
    thread_config: dict | None = None,
    resume_session_id: str | None = None,
    fork_session: bool = False,
    developer_instructions: str | None = None,
) -> tuple[str, dict]:
    """Build the app-server thread request method and params."""
    params = {
        "cwd": cwd,
        "model": codex_model,
        # A selected Loom model is a contract, not a hint. Surface an explicit
        # availability error instead of silently running a different model.
        "allowProviderModelFallback": False,
        "approvalPolicy": approval_policy,
        "approvalsReviewer": "user",
        "sandbox": sandbox_mode,
    }
    if thread_config:
        params["config"] = thread_config
    if developer_instructions:
        # This supplements Codex's base instructions. Supplying it on
        # resume/fork also upgrades pre-fix Loom threads whose contract only
        # existed as repeated user-prompt XML.
        params["developerInstructions"] = developer_instructions

    if resume_session_id:
        params["threadId"] = resume_session_id
        params["excludeTurns"] = True
        return ("thread/fork" if fork_session else "thread/resume"), params

    params["sessionStartSource"] = "startup"
    params["threadSource"] = "user"
    return "thread/start", params


def _json_dumps_line(payload: dict) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def _compact_json(value, limit: int = 4000) -> str:
    try:
        text = json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[truncated {len(text) - limit} chars]"


def _app_user_input(prompt: str) -> list[dict]:
    return [{"type": "text", "text": prompt}]


def _app_sandbox_policy(cwd: str) -> dict:
    pass
    return {
        "type": "workspaceWrite",
        "writableRoots": [cwd],
        "networkAccess": False,
    }


def _codex_goal_set_params(
    thread_id: str,
    objective: str | None = None,
    status: str | None = None,
    token_budget: int | None = None,
) -> dict:
    params = {"threadId": thread_id}
    if objective is not None:
        params["objective"] = objective
    if status is not None:
        params["status"] = status
    if token_budget is not None:
        params["tokenBudget"] = token_budget
    return params


def _codex_goal_from_response(response: dict) -> dict | None:
    result = response.get("result") if isinstance(response, dict) else None
    if not isinstance(result, dict):
        return None
    goal = result.get("goal")
    return goal if isinstance(goal, dict) else None


def _mcp_approval_params(params: dict, pending_items: dict) -> dict:
    """Join protocol evidence, never extract tool identity from display prose."""
    enriched = dict(params)
    enriched.pop("_loom_mcp_item", None)
    meta = params.get("_meta") or {}
    if not isinstance(meta, dict) or meta.get("codex_approval_kind") != "mcp_tool_call":
        return enriched
    if not all(params.get(key) for key in ("threadId", "turnId", "serverName")):
        return enriched
    args = meta.get("tool_params")
    candidates = [item for item in pending_items.values()
                  if item.get("threadId") == params.get("threadId")
                  and item.get("turnId") == params.get("turnId")
                  and item.get("server") == params.get("serverName")
                  and isinstance(args, dict) and item.get("arguments") == args]
    if len(candidates) == 1:
        enriched["_loom_mcp_item"] = candidates[0]
    return enriched


def _is_mcp_tool_approval(params: dict) -> bool:
    meta = params.get("_meta") or {}
    schema = params.get("requestedSchema") or {}
    return (params.get("mode") == "form" and isinstance(meta, dict)
            and meta.get("codex_approval_kind") == "mcp_tool_call"
            and isinstance(schema, dict) and schema.get("type") == "object"
            and not schema.get("properties") and not schema.get("required"))


def _app_permission_payload(method: str, params: dict) -> tuple[str, dict]:
    if method == "mcpServer/elicitation/request":
        item = params.get("_loom_mcp_item") or {}
        if item.get("server") and item.get("tool") and isinstance(item.get("arguments"), dict):
            return f"mcp__{item['server']}__{item['tool']}", dict(item["arguments"])
        # An ambiguous join may be reviewed once, but cannot inherit a read
        # classification or remembered tool grant based on its display title.
        return "MCPToolApproval", {k: v for k, v in params.items() if k != "_loom_mcp_item"}
    if method == "item/commandExecution/requestApproval":
        command = params.get("command") or params.get("cmd") or params.get("argv") or params.get("execCommand")
        from permission_commands import display_argv
        payload = dict(params)
        if isinstance(command, list) and all(isinstance(part, str) for part in command):
            payload["command_argv"] = command[:]
            command = display_argv(command)
        payload["command"] = command or ""
        return "Bash", payload
    if method == "item/fileChange/requestApproval":
        return "Edit", params
    if method == "item/permissions/requestApproval":
        return "PermissionRequest", params
    if method == "item/tool/requestUserInput":
        return params.get("toolName") or params.get("tool") or "ToolInput", params
    return method, params


def _requested_permissions(params: dict) -> dict:
    for key in ("permissions", "requestedPermissions", "requested_permissions"):
        value = params.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _app_approval_response(method: str, allow: bool, always: bool, params: dict | None = None) -> dict:
    params = params or {}
    if method == "mcpServer/elicitation/request":
        # Remembered rules remain Loom-owned; never grant MCP-wide persistence.
        return {"action": "accept" if allow else "decline", "content": {} if allow else None}
    decision = "acceptForSession" if allow and always else ("accept" if allow else "decline")
    if method == "item/permissions/requestApproval":
        if allow:
            return {
                "permissions": _requested_permissions(params),
                "scope": "session" if always else "turn",
            }
        return {
            "permissions": {
                "filesystem": {"entries": []},
                "network": {"enabled": False},
            },
            "scope": "turn",
            "strictAutoReview": True,
        }
    if method == "item/tool/requestUserInput":
        return {"decision": decision}
    return {"decision": decision}


def _permission_feedback_text(result: dict, *, allow: bool) -> str:
    """Format meaningful permission feedback without inventing a user choice."""
    feedback = str(result.get("feedback") or "").strip()
    source = str(result.get("decision_source") or "")
    if not feedback and not allow and source.startswith("loom_"):
        feedback = str(result.get("message") or "").strip()
    if not feedback:
        return ""
    source_attr = escape(source or "user", quote=True)
    if allow:
        decision = "allowed"
    elif source == "loom_policy":
        decision = "blocked"
    elif source and source != "user":
        decision = "cancelled"
    else:
        decision = "denied"
    return (
        f'<loom_permission_feedback result="{decision}" source="{source_attr}">'
        f"{escape(feedback)}</loom_permission_feedback>"
    )


async def _post_loom_permission(
    server_port: int,
    conv_id: int,
    method: str,
    params: dict,
    permission_scope: str = "",
    permission_request_handler=None,
) -> dict:
    tool_name, tool_input = _app_permission_payload(method, params)
    payload = {
        "loom_conv_id": conv_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "source": "codex_app_server",
        "approval_method": method,
        "permission_scope": permission_scope,
    }
    for env_name, payload_name in (
        ("LOOM_AGENT_RUN_ID", "agent_run_id"),
        ("LOOM_CLIENT_INSTANCE_ID", "client_instance_id"),
    ):
        value = os.environ.get(env_name, "").strip()
        if not value:
            task = asyncio.current_task()
            attr_name = "_agent_run_id" if payload_name == "agent_run_id" else "_client_instance_id"
            value = str(getattr(task, attr_name, "") or "").strip() if task else ""
        if value:
            payload[payload_name] = value
    if permission_request_handler is not None:
        try:
            return await permission_request_handler(payload)
        except Exception as exc:
            log.warning("[CODEX] Direct Loom permission handler failed (%s)", type(exc).__name__)
            return {"allow": False, "decision_source": "bridge_error",
                    "message": f"Loom permission handler failed ({type(exc).__name__})."}

    def _post() -> dict:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{server_port}/api/cc-permission",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=86400) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
        except urllib.error.URLError as exc:
            return {"allow": False, "decision_source": "bridge_error",
                    "message": f"Loom permission bridge failed ({type(exc).__name__})."}

    return await asyncio.to_thread(_post)


async def manage_codex_goal(
    action: str,
    cwd: str,
    conv_id: int = 0,
    server_port: int = 8000,
    model: str = "Codex (GPT-4o)",
    permission_mode: str = "default",
    resume_session_id: str | None = None,
    objective: str | None = None,
    status: str | None = None,
    token_budget: int | None = None,
    
) -> dict:
    """Run a short app-server control session for Codex thread goal commands."""
    workspace_root = Path(cwd).resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    cwd = str(workspace_root)

    codex_model = _loom_model_to_codex(model)
    codex_exe = _find_codex_exe()
    approval_policy, sandbox_mode = _codex_launch_policies(permission_mode)
    mcp_servers_cfg = _thread_mcp_servers(
        conv_id,
        server_port,
        
        client_instance_id=getattr(asyncio.current_task(), "_client_instance_id", None),
        workspace_root=cwd,
    )
    mcp_args = [
        arg
        for name, server_cfg in mcp_servers_cfg.items()
        for arg in _mcp_server_config_args(name, server_cfg)
    ]
    cmd = [codex_exe, "app-server", *mcp_args, "--stdio", "--disable", "hooks"]
    env = {
        **os.environ,
        "LOOM_CONV_ID": str(conv_id),
        "LOOM_PORT": str(server_port),
        "LOOM_API_URL": f"http://127.0.0.1:{server_port}",
        "LOOM_WORKSPACE_ROOT": cwd,
    }
    pass
    if preparation:
        # Defense in depth for every preparation launch, operator or not:
        # mark the child as preparation so any downstream tool gate
        # (cc_permission_hook) denies every tool call.
        env["LOOM_PREPARATION"] = "1"

    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000 | 0x00000200

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=_CODEX_STREAM_LIMIT,
        **kwargs,
    )

    stderr_lines: list[str] = []
    pending_responses: dict[str | int, asyncio.Future] = {}
    request_id = 0

    async def _read_stderr():
        try:
            async for line in proc.stderr:
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    stderr_lines.append(text)
                    print(f"[CODEX-stderr] {text}")
        except Exception as e:
            log.error(f"[CODEX] Error reading goal stderr: {e}")

    stderr_task = asyncio.create_task(_read_stderr())

    async def _read_stdout():
        try:
            async for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    print(f"[CODEX] Non-JSON goal line on stdout: {line[:200]}")
                    continue
                if "id" in raw and ("result" in raw or "error" in raw) and "method" not in raw:
                    fut = pending_responses.pop(raw.get("id"), None)
                    if fut and not fut.done():
                        fut.set_result(raw)
        except Exception as e:
            for fut in pending_responses.values():
                if not fut.done():
                    fut.set_exception(RuntimeError(f"Codex app-server stdout failed: {e}"))

    stdout_task = asyncio.create_task(_read_stdout())

    async def _send(payload: dict):
        if not proc.stdin or proc.stdin.is_closing():
            raise RuntimeError("Codex app-server stdin is closed")
        proc.stdin.write(_json_dumps_line(payload))
        await proc.stdin.drain()

    async def _request(method: str, params: dict | None = None) -> dict:
        nonlocal request_id
        request_id += 1
        rid = request_id
        fut = asyncio.get_running_loop().create_future()
        pending_responses[rid] = fut
        payload = {"id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        await _send(payload)
        return await asyncio.wait_for(fut, timeout=120)

    cleaned_up = False

    async def _cleanup():
        nonlocal cleaned_up
        if cleaned_up:
            return
        cleaned_up = True
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        if proc.returncode is None:
            # The npm ``codex.cmd`` wrapper owns a Node/app-server process
            # tree. Terminating only the wrapper can orphan the actual agent,
            # so every cleanup path uses the same tree-aware cancellation.
            await cancel_codex(proc)
        else:
            await proc.wait()
        if not stdout_task.done():
            stdout_task.cancel()
        try:
            await stdout_task
        except asyncio.CancelledError:
            pass
        await stderr_task

    try:
        init = await _request(
            "initialize",
            {
                "clientInfo": {"name": "Loom", "version": "1"},
                "capabilities": {"experimental": True, "experimentalApi": True},
            },
        )
        if init.get("error"):
            raise RuntimeError(json.dumps(init["error"]))
        await _send({"method": "initialized"})

        method, thread_params = _codex_thread_request(
            cwd,
            codex_model,
            approval_policy,
            sandbox_mode,
            {"mcp_servers": mcp_servers_cfg} if mcp_servers_cfg else None,
            resume_session_id,
            fork_session=False,
        )
        thread_start = await _request(method, thread_params)
        if thread_start.get("error"):
            raise RuntimeError(json.dumps(thread_start["error"]))

        thread_result = thread_start.get("result") or {}
        thread = thread_result.get("thread") or {}
        thread_id = thread.get("id") or thread.get("threadId") or resume_session_id
        if not thread_id:
            raise RuntimeError("Codex app-server did not return a thread id")

        normalized = (action or "get").lower()
        if normalized == "get":
            goal_response = await _request("thread/goal/get", {"threadId": thread_id})
        elif normalized == "clear":
            goal_response = await _request("thread/goal/clear", {"threadId": thread_id})
        elif normalized == "set":
            goal_response = await _request(
                "thread/goal/set",
                _codex_goal_set_params(
                    thread_id,
                    objective=objective,
                    status=status,
                    token_budget=token_budget,
                ),
            )
        else:
            raise ValueError(f"Unsupported Codex goal action: {action}")

        if goal_response.get("error"):
            raise RuntimeError(json.dumps(goal_response["error"]))
        return {
            "action": normalized,
            "thread_id": thread_id,
            "goal": _codex_goal_from_response(goal_response),
            "raw": goal_response.get("result") or {},
        }
    finally:
        await _cleanup()


def _completed_rollout_tail(path: str, session_id: str) -> dict | None:
    """Read terminal evidence from an immutable native session log.

    Bounded to 2 MiB, with the session header checked independently. A newer
    started/aborted turn prevents accepting an ancestor's completion.
    """
    from datetime import datetime
    with open(path, "rb") as stream:
        before = os.fstat(stream.fileno())
        header = json.loads(stream.readline(256 * 1024))
        if header.get("type") != "session_meta" or (header.get("payload") or {}).get("id") != session_id:
            return None
        size = stream.seek(0, 2)
        offset = max(0, size - 2 * 1024 * 1024)
        stream.seek(offset)
        if offset:
            stream.readline(2 * 1024 * 1024)  # Discard a potentially partial JSON line.
        tail = stream.read(2 * 1024 * 1024)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return None
    for line in reversed(tail.splitlines()):
        try:
            item = json.loads(line)
        except ValueError:
            return None  # A partial/corrupt newest record cannot certify completion.
        payload = item.get("payload") or {}
        if item.get("type") != "event_msg" or payload.get("type") not in {"task_complete", "task_started", "turn_aborted"}:
            continue
        if payload.get("type") != "task_complete" or not payload.get("turn_id"):
            return None
        # task_complete means ended, not necessarily succeeded. Failed native
        # turns carry error here, and may have no final assistant message.
        if payload.get("error") or not str(payload.get("last_agent_message") or "").strip():
            return None
        return {"id": payload["turn_id"], "status": "completed", "error": None,
                "startedAt": payload.get("started_at"),
                "completedAt": datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")).timestamp(),
                "source": "codex_rollout_task_complete"}
    return None


async def read_codex_last_turn(session_id: str, cwd: str) -> dict | None:
    """Read only the exact UUIDv7 snapshot's bounded journal tail.

    The installed app-server cannot read unloaded turns without resuming them.
    Use its local journal instead; do not launch a provider, change its state,
    scan historical sessions recursively, or add overhead to normal resumes.
    Unknown IDs/layouts or missing evidence simply remain non-resumable.
    """
    from datetime import datetime, timedelta, timezone
    from uuid import UUID

    def read():
        ident = UUID(session_id)
        if ident.version != 7 or str(ident) != session_id:
            return None
        day = datetime.fromtimestamp(int(ident.hex[:12], 16) / 1000, timezone.utc)
        native_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        candidates = []
        for delta in (0, -1, 1):
            dated = native_home / "sessions" / (day + timedelta(days=delta)).strftime("%Y/%m/%d")
            candidates.extend(dated.glob(f"rollout-*-{session_id}.jsonl"))
        if len(candidates) != 1:
            return None
        return _completed_rollout_tail(str(candidates[0]), session_id)

    try:
        return await asyncio.wait_for(asyncio.to_thread(read), timeout=2)
    except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError, TimeoutError):
        log.warning("[CODEX] Prior terminal verification unavailable; preserving history-rebuild policy")
    return None


async def run_codex(prompt: str, cwd: str, conv_id: int = 0, server_port: int = 8000,
                    model: str = "Codex (GPT-4o)", effort: str = "high",
                    permission_mode: str = "default",
                    resume_session_id: str = None, fork_session: bool = False,
                    backstage_parent_id: int | None = None,
                    
                    permission_request_handler=None,
                    codex_goal: dict | None = None,
                    permission_scope: str = "",
                    agent_run_id: str | None = None,
                    preparation: bool = False):
    """Launch Codex app-server and yield Loom-compatible stream events.

    preparation=True requests a tool-free transport: read-only sandbox,
    approval never, empty MCP configuration and no writable roots. Ordinary
    agent launch defaults are never used for preparation.
    """
    workspace_root = Path(cwd).resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    cwd = str(workspace_root)

    codex_model = _loom_model_to_codex(model)
    codex_exe = _find_codex_exe()
    approval_policy, sandbox_mode = _codex_launch_policies(permission_mode)
    if preparation:
        approval_policy, sandbox_mode = "never", "read-only"
    task = asyncio.current_task()
    gen_key = getattr(task, "_gen_key", None)
    permission_scope = permission_scope or (f"gen:{gen_key[2]}" if gen_key else "")
    if permission_mode == "plan":
        plan_instruction = (
            "You are running in PLAN MODE. Your task is to analyze the codebase and write a comprehensive "
            "implementation plan to `implementation_plan.md` in the workspace. Do NOT modify any other files "
            "or run commands that modify the repository. Once the plan is written, present it to the user "
            "and ask for their approval. After writing the plan, end your turn immediately without performing "
            "any edits."
        )
        prompt = f"{plan_instruction}\n\n{prompt}"

    pass
    effective_agent_run_id = agent_run_id or (f"gen:{gen_key[2]}" if gen_key else None)
    effective_parent_run_id = getattr(task, "_parent_run_id", None) or (
        f"gen:{gen_key[2]}" if gen_key and not agent_run_id else None
    )
    if preparation:
        # Tool-free preparation: empty MCP configuration; no agent-control,
        mcp_servers_cfg = {}
    else:
        mcp_servers_cfg = _thread_mcp_servers(
            conv_id,
            server_port,
            
            agent_run_id=effective_agent_run_id,
            parent_run_id=effective_parent_run_id,
            client_instance_id=getattr(task, "_client_instance_id", None),
            workspace_root=cwd,
        )
    mcp_args = [
        arg
        for name, server_cfg in mcp_servers_cfg.items()
        for arg in _mcp_server_config_args(name, server_cfg)
    ]
    cmd = [codex_exe, "app-server", *mcp_args, "--stdio", "--disable", "hooks"]
    print(f"[CODEX] CMD: {' '.join(cmd)}")
    print(
        f"[CODEX] app-server model={codex_model}, approval={approval_policy}, "
        f"sandbox={sandbox_mode}, cwd={cwd}, prompt_len={len(prompt)}"
    )
    launch_info = {
        "type": "codex_launch_info",
        "surface": "app-server",
        "requested_model": model,
        "model": codex_model,
        "fallback_allowed": False,
        "approval_policy": approval_policy,
        "approvals_reviewer": "user",
        "sandbox": sandbox_mode,
        "cwd": cwd,
        "writable_roots": [] if (preparation) else [cwd],
        "hook_path": None,
        "hook_scope": "disabled",
        "mcp_servers": list(mcp_servers_cfg),
    }

    env = {
        **os.environ,
        "LOOM_CONV_ID": str(conv_id),
        "LOOM_PORT": str(server_port),
        "LOOM_API_URL": f"http://127.0.0.1:{server_port}",
        "LOOM_WORKSPACE_ROOT": cwd,
    }
    if agent_run_id:
        env["LOOM_AGENT_RUN_ID"] = str(agent_run_id)
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
        # Defense in depth for every preparation launch, operator or not:
        # mark the child as preparation so any downstream tool gate
        # (cc_permission_hook) denies every tool call.
        env["LOOM_PREPARATION"] = "1"

    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000 | 0x00000200

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=_CODEX_STREAM_LIMIT,
        **kwargs
    )

    stderr_lines: list[str] = []

    async def _read_stderr():
        try:
            async for line in proc.stderr:
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    stderr_lines.append(text)
                    print(f"[CODEX-stderr] {text}")
        except Exception as e:
            log.error(f"[CODEX] Error reading stderr: {e}")
    stderr_task = asyncio.create_task(_read_stderr())

    pending_responses: dict[str | int, asyncio.Future] = {}
    permission_feedback_tasks: set[asyncio.Task] = set()
    pending_mcp_items: dict[str, dict] = {}
    app_messages: asyncio.Queue[dict] = asyncio.Queue()
    request_id = 0

    async def _read_stdout():
        stdout_error = None
        try:
            async for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    print(f"[CODEX] Non-JSON line on stdout: {line[:200]}")
                    continue
                if "id" in raw and ("result" in raw or "error" in raw) and "method" not in raw:
                    fut = pending_responses.pop(raw.get("id"), None)
                    if fut and not fut.done():
                        fut.set_result(raw)
                    else:
                        await app_messages.put(raw)
                else:
                    await app_messages.put(raw)
        except Exception as e:
            if isinstance(e, ValueError) and ("chunk exceed the limit" in str(e) or "Separator is not found" in str(e)):
                stdout_error = f"Codex app-server stdout line exceeded buffer limit: {e}"
            else:
                stdout_error = f"Codex app-server stdout failed: {e}"
        finally:
            # EOF is terminal even when the pipe closes cleanly. Previously
            # the reader simply returned and the event stream remained blocked
            # forever on app_messages.get(), leaving the generation and every
            # queued continuation behind it permanently active.
            exit_message = stdout_error or (
                "Codex app-server closed its output before the turn completed"
            )
            for fut in list(pending_responses.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError(exit_message))
            pending_responses.clear()
            await app_messages.put(
                {
                    "type": "codex_app_server_eof",
                    "message": exit_message,
                    "returncode": proc.returncode,
                }
            )

    stdout_task = asyncio.create_task(_read_stdout())

    async def _send(payload: dict):
        if not proc.stdin or proc.stdin.is_closing():
            raise RuntimeError("Codex app-server stdin is closed")
        proc.stdin.write(_json_dumps_line(payload))
        await proc.stdin.drain()

    async def _request(method: str, params: dict | None = None) -> dict:
        nonlocal request_id
        request_id += 1
        rid = request_id
        fut = asyncio.get_running_loop().create_future()
        pending_responses[rid] = fut
        payload = {"id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        await _send(payload)
        return await fut

    async def _answer_server_request(raw: dict):
        method = raw.get("method") or ""
        rid = raw.get("id")
        params = raw.get("params") or {}
        if method == "mcpServer/elicitation/request":
            if not _is_mcp_tool_approval(params):
                await app_messages.put({"type": "loom_permission_policy_denied",
                    "message": "Loom cannot answer this MCP input form; it was cancelled by the bridge, not denied by the user."})
                await _send({"id": rid, "result": {"action": "cancel", "content": None}})
                return
            params = _mcp_approval_params(params, pending_mcp_items)
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
            "item/tool/requestUserInput",
            "mcpServer/elicitation/request",
        }:
            tool_name, _ = _app_permission_payload(method, params)
            print(f"[CODEX] Waiting for Loom permission: method={method} tool={tool_name} request_id={rid}")
            result = await _post_loom_permission(
                server_port,
                conv_id,
                method,
                params,
                permission_scope=permission_scope,
                permission_request_handler=permission_request_handler,
            )
            allow = bool(result.get("allow"))
            always = bool(result.get("always"))
            if not allow and result.get("decision_source") == "loom_policy":
                await app_messages.put(
                    {
                        "type": "loom_permission_policy_denied",
                        "message": result.get("message")
                        or "Blocked by Loom command policy",
                    }
                )
            print(f"[CODEX] Loom permission resolved: method={method} allow={allow} always={always} request_id={rid}")
            response = _app_approval_response(method, allow, always, params)
            await _send({"id": rid, "result": response})
            feedback_text = _permission_feedback_text(result, allow=allow)
            thread_id = str(params.get("threadId") or params.get("thread_id") or "")
            turn_id = str(params.get("turnId") or params.get("turn_id") or "")
            if feedback_text and thread_id and turn_id:
                async def _steer_feedback() -> None:
                    try:
                        steer = await _request(
                            "turn/steer",
                            {
                                "threadId": thread_id,
                                "expectedTurnId": turn_id,
                                "input": [{"type": "text", "text": feedback_text}],
                            },
                        )
                        if steer.get("error"):
                            log.warning(
                                "[CODEX] Permission feedback steering was rejected: %s",
                                steer["error"],
                            )
                    except Exception as exc:
                        log.warning(
                            "[CODEX] Could not steer permission feedback into active turn: %s",
                            exc,
                        )

                feedback_task = asyncio.create_task(_steer_feedback())
                permission_feedback_tasks.add(feedback_task)
                feedback_task.add_done_callback(permission_feedback_tasks.discard)
            return
        await _send({"id": rid, "error": {"code": -32601, "message": f"Loom does not handle {method}"}})

    cleaned_up = False

    async def _cleanup():
        nonlocal cleaned_up
        if cleaned_up:
            return
        cleaned_up = True
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        if proc.returncode is None:
            # ``codex.cmd`` is only the root of the app-server process tree.
            # The generation is not finished until that complete tree exits.
            await cancel_codex(proc)
        else:
            await proc.wait()
        if not stdout_task.done():
            stdout_task.cancel()
        try:
            await stdout_task
        except asyncio.CancelledError:
            pass
        await stderr_task
        if permission_feedback_tasks:
            for task in list(permission_feedback_tasks):
                if not task.done():
                    task.cancel()
            await asyncio.gather(*permission_feedback_tasks, return_exceptions=True)

    async def _event_stream():
        session_id = resume_session_id or str(conv_id)
        full_text = ""
        got_result = False
        turn_inflight = False
        turn_activity_seen = False
        active_turn_id = ""
        pending_terminal_error = ""
        last_usage_input_tokens = 0
        last_usage_output_tokens = None
        started_tool_ids: set[str] = set()
        diff_tool_ids: set[str] = set()
        unknown_event_types: set[str] = set()
        unknown_item_types: set[str] = set()
        yield launch_info

        try:
            try:
                init = await _request(
                    "initialize",
                    {
                        "clientInfo": {"name": "Loom", "version": "1"},
                        "capabilities": {"experimental": True, "experimentalApi": True},
                    },
                )
                if init.get("error"):
                    await _cleanup()
                    yield {"type": "error", "message": json.dumps(init["error"])}
                    return
                init_result = init.get("result") or {}
                app_server_user_agent = str(init_result.get("userAgent") or "").strip()
                await _send({"method": "initialized"})

                configured_instructions = None
                config_read = {}
                if not backstage_parent_id:
                    try:
                        config_read = await _request(
                            "config/read",
                            {"cwd": cwd, "includeLayers": False},
                        )
                        configured_instructions = _configured_developer_instructions(
                            config_read
                        )
                    except Exception as config_exc:
                        # Contract delivery must not make an otherwise healthy
                        # app-server unusable on versions predating config/read.
                        log.warning(
                            "[CODEX] Could not read configured developer instructions: %s",
                            config_exc,
                        )
                developer_instructions = _codex_developer_instructions(
                    configured_instructions,
                    backstage_parent_id,
                    
                )

                if not backstage_parent_id:
                    from context_runtime import admit_rendered
                    admitted_prompt = await admit_rendered(
                        prompt, model=codex_model, harness="codex", resume_session_id=resume_session_id,
                        instructions=developer_instructions or "",
                        runtime_context=((config_read.get("result") or {}).get("config") or {}).get("model_context_window"),
                    )
                else:
                    admitted_prompt = prompt

                method, thread_params = _codex_thread_request(
                    cwd,
                    codex_model,
                    approval_policy,
                    sandbox_mode,
                    {"mcp_servers": mcp_servers_cfg} if mcp_servers_cfg else None,
                    resume_session_id,
                    fork_session,
                    developer_instructions,
                )

                thread_start = await _request(
                    method,
                    thread_params,
                )
                if thread_start.get("error"):
                    await _cleanup()
                    yield {
                        "type": "error",
                        "message": f"Codex app-server launch failed: {json.dumps(thread_start['error'])}",
                        "launch_failed": True,
                        "resume_failed": bool(resume_session_id),
                    }
                    return

                thread_result = thread_start.get("result") or {}
                thread = thread_result.get("thread") or {}
                session_id = thread.get("id") or thread.get("threadId") or session_id
                model_attestation = _codex_model_attestation(
                    model,
                    codex_model,
                    thread_result,
                    session_id=session_id,
                    app_server_user_agent=app_server_user_agent,
                )
                yield {
                    "type": "session_info",
                    "session_id": session_id,
                    "model": model_attestation.get("effective_model") or "",
                    "model_provider": model_attestation.get("model_provider") or "",
                    "model_confirmed": model_attestation.get("status") == "verified",
                    "model_attestation": model_attestation,
                }
                if model_attestation["status"] != "verified":
                    await _cleanup()
                    if model_attestation["status"] == "mismatch":
                        message = (
                            "Codex model mismatch: Loom launched "
                            f"'{codex_model}', but app-server reported "
                            f"'{model_attestation.get('effective_model')}'. "
                            "No prompt was sent."
                        )
                    else:
                        message = (
                            "Codex app-server did not attest both its effective model "
                            "and model provider. No prompt was sent."
                        )
                    yield {
                        "type": "result",
                        "is_error": True,
                        "result_text": "",
                        "session_id": session_id,
                        "error": message,
                    }
                    return
                pass
                if codex_goal and codex_goal.get("objective"):
                    try:
                        goal_set = await _request(
                            "thread/goal/set",
                            _codex_goal_set_params(
                                session_id,
                                objective=codex_goal.get("objective"),
                                status=codex_goal.get("status") or "active",
                                token_budget=codex_goal.get("tokenBudget"),
                            ),
                        )
                        if goal_set.get("error"):
                            yield {
                                "type": "status",
                                "text": f"Codex goal sync failed: {json.dumps(goal_set['error'])}",
                            }
                        else:
                            yield {
                                "type": "codex_goal",
                                "goal": _codex_goal_from_response(goal_set),
                            }
                    except Exception as goal_exc:
                        yield {"type": "status", "text": f"Codex goal sync failed: {goal_exc}"}

                turn_start = await _request(
                    "turn/start",
                    {
                        "threadId": session_id,
                        "input": _app_user_input(admitted_prompt),
                        "cwd": cwd,
                        "model": codex_model,
                        "approvalPolicy": approval_policy,
                        "approvalsReviewer": "user",
                        "sandboxPolicy": _app_sandbox_policy(cwd),
                        "effort": _codex_reasoning_effort(effort),
                    },
                )
                if turn_start.get("error"):
                    await _cleanup()
                    yield {
                        "type": "error",
                        "message": f"Codex turn/start failed: {json.dumps(turn_start['error'])}",
                        "launch_failed": True,
                        "resume_failed": bool(resume_session_id),
                    }
                    return
                turn_result = turn_start.get("result") or {}
                turn = turn_result.get("turn") or {}
                active_turn_id = str(
                    turn.get("id")
                    or turn.get("turnId")
                    or turn_result.get("turnId")
                    or turn_result.get("turn_id")
                    or ""
                )
                turn_inflight = True
            except Exception as e:
                await _cleanup()
                yield {
                    "type": "error",
                    "message": f"Codex app-server launch failed: {e}",
                    "launch_failed": True,
                    "resume_failed": bool(resume_session_id),
                }
                return

            while True:
                try:
                    raw = await asyncio.wait_for(app_messages.get(), timeout=10 if pending_terminal_error else _CODEX_COMPLETION_RECHECK_SEC)
                except asyncio.TimeoutError:
                    if pending_terminal_error:
                        raise RuntimeError(f"Codex error without a confirmed terminal turn: {pending_terminal_error}")
                    # The native turn may finish without delivering turn/completed
                    # over stdio. Silence and thread-idle are never terminal proof:
                    # require the exact session journal's successful active turn.
                    terminal = await read_codex_last_turn(session_id, cwd) if active_turn_id and turn_inflight else None
                    if not terminal or terminal.get('id') != active_turn_id or terminal.get('status') != 'completed' or terminal.get('error'):
                        continue
                    yield {'type': 'status', 'text': 'Codex completed; recovering its missing completion notification.'}
                    raw = {'method': 'turn/completed', 'params': {
                        'threadId': session_id, 'turn': {
                            'id': active_turn_id, 'status': 'completed', 'items': []}}}
                method = raw.get("method")
                if method and "id" in raw:
                    approval_task = asyncio.create_task(_answer_server_request(raw))
                    permission_feedback_tasks.add(approval_task)
                    approval_task.add_done_callback(permission_feedback_tasks.discard)
                    continue

                params = raw.get("params") or {}
                etype = raw.get("type") or method
                event_thread_id = params.get("threadId") or raw.get("thread_id")
                event_turn_id = _codex_turn_id(raw, params)
                if event_thread_id and session_id and event_thread_id != session_id:
                    continue
                if event_turn_id and active_turn_id and event_turn_id != active_turn_id:
                    continue
                mcp_item = params.get("item") or raw.get("item") or {}
                if mcp_item.get("type") in {"mcpToolCall", "mcp_tool_call"}:
                    item_key = _codex_item_id(mcp_item, raw)
                    if etype in {"item/started", "item.started"}:
                        pending_mcp_items[item_key] = {**mcp_item, "threadId": event_thread_id, "turnId": event_turn_id}
                    elif etype in {"item/completed", "item.completed"}:
                        pending_mcp_items.pop(item_key, None)
                usage_evt = _codex_usage(raw)
                if usage_evt:
                    turn_activity_seen = True
                    current_input = int(usage_evt.get("input_tokens") or 0)
                    current_output = int(usage_evt.get("output_tokens") or 0)
                    usage_evt["input_tokens"] = current_input
                    first_output = usage_evt.pop("_first_output_tokens", current_output)
                    if last_usage_output_tokens is None:
                        usage_evt["output_tokens"] = first_output
                    elif current_output >= last_usage_output_tokens:
                        usage_evt["output_tokens"] = current_output - last_usage_output_tokens
                    else:
                        usage_evt["output_tokens"] = current_output
                    last_usage_input_tokens = current_input
                    last_usage_output_tokens = current_output
                    yield usage_evt

                if etype in ("turn.started", "turn/started"):
                    event_turn_id = _codex_turn_id(raw, params)
                    if not active_turn_id and event_turn_id:
                        active_turn_id = event_turn_id
                    if not event_turn_id or not active_turn_id or event_turn_id == active_turn_id:
                        turn_activity_seen = True

                elif etype == "loom_permission_policy_denied":
                    yield {
                        "type": "status",
                        "text": str(
                            raw.get("message")
                            or "Blocked by Loom command policy"
                        ),
                    }

                elif etype in ("thread.started", "thread/started"):
                    thread = params.get("thread") or raw.get("thread") or {}
                    session_id = raw.get("thread_id") or params.get("threadId") or thread.get("id") or session_id
                    yield {
                        "type": "session_info",
                        "session_id": session_id,
                        "model": codex_model,
                    }

                elif etype in (
                    "thread/status/changed",
                    "thread.status.changed",
                    "turn/status/changed",
                    "turn.status.changed",
                ):
                    terminal, is_error, err_msg = _codex_terminal_status(raw, params)
                    event_turn_id = _codex_turn_id(raw, params)
                    if (
                        terminal
                        and turn_inflight
                        and active_turn_id
                        and event_turn_id == active_turn_id
                        and etype.startswith("turn")
                    ):
                        if not is_error and not full_text:
                            is_error = True
                            err_msg = "Codex app-server became idle without an assistant response"
                            print(
                                "[CODEX] Empty terminal status: "
                                f"{json.dumps(raw, default=str)[:4000]}"
                            )
                        got_result = True
                        await _cleanup()
                        yield {
                            "type": "result",
                            "is_error": is_error,
                            "result_text": full_text,
                            "session_id": session_id,
                            "error": err_msg if is_error else "",
                        }
                        break
                    status = _codex_status_value(raw, params)
                    if status and status not in {"idle", "running", "in_progress", "active"}:
                        yield {"type": "status", "text": f"Codex status: {status}"}

                elif etype in ("thread/diff/updated", "thread.diff.updated", "turn/diff/updated", "turn.diff.updated"):
                    turn_activity_seen = True
                    tool_id = _codex_diff_tool_id(raw)
                    payload = _codex_diff_payload(raw)
                    if tool_id not in diff_tool_ids:
                        diff_tool_ids.add(tool_id)
                        started_tool_ids.add(tool_id)
                        yield {
                            "type": "tool_start",
                            "name": "Edit",
                            "tool_id": tool_id,
                        }
                        yield {
                            "type": "tool_input_delta",
                            "json": json.dumps(payload, indent=2),
                            "tool_id": tool_id,
                        }
                    yield {
                        "type": "tool_result",
                        "content": json.dumps(payload, indent=2),
                        "tool_id": tool_id,
                        "is_error": False,
                    }

                elif etype in ("mcpServer/startupStatus/updated", "mcpServer.startupStatus.updated"):
                    name = params.get("name") or raw.get("name") or "mcp"
                    status = params.get("status") or raw.get("status") or "unknown"
                    error = params.get("error") or raw.get("error")
                    text = f"MCP startup: {name} {status}"
                    if error:
                        text += f" ({error})"
                    yield {"type": "status", "text": text}

                elif etype in ("item.started", "item/started"):
                    turn_activity_seen = True
                    item = raw.get("item") or params.get("item") or params
                    item_type = item.get("type")
                    item_id = _codex_item_id(item, raw)
                    if item_type == "contextCompaction":
                        yield {"type": "status", "text": "Codex is compacting its native context..."}
                    elif _is_codex_tool_item(item):
                        tool_input = _codex_tool_input(item)
                        started_tool_ids.add(item_id)
                        yield {
                            "type": "tool_start",
                            "name": _codex_tool_name(item),
                            "tool_id": item_id,
                        }
                        if tool_input:
                            yield {
                                "type": "tool_input_delta",
                                "json": tool_input,
                                "tool_id": item_id,
                            }
                    elif item_type in _IGNORED_APP_SERVER_ITEM_TYPES:
                        pass
                    elif item_type and item_type not in unknown_item_types:
                        unknown_item_types.add(item_type)
                        print(f"[CODEX] Ignoring item.started type={item_type}: {json.dumps(item, default=str)[:500]}")

                elif etype in ("item.completed", "item/completed"):
                    turn_activity_seen = True
                    item = raw.get("item") or params.get("item") or params
                    item_type = item.get("type")
                    item_id = _codex_item_id(item, raw)
                    content = _codex_item_text(item)

                    if item_type == "contextCompaction":
                        yield {"type": "status", "text": "Codex native context compactified."}
                    elif item_type in ("reasoning", "reasoningSummary", "reasoning_summary", "summary") and content:
                        chunk_size = 128
                        for i in range(0, len(content), chunk_size):
                            yield {"type": "thinking_delta", "text": content[i:i+chunk_size]}
                            await asyncio.sleep(0.005)

                    elif (
                        item_type in ("agent_message", "agentMessage", "message", "assistant_message", "assistantMessage", "output_text")
                        or item.get("role") == "assistant"
                    ) and content:
                        if not full_text:
                            chunk_size = 64
                            for i in range(0, len(content), chunk_size):
                                yield {"type": "text_delta", "text": content[i:i+chunk_size]}
                                await asyncio.sleep(0.01)
                            full_text += content

                    elif _is_codex_tool_item(item):
                        if item_id not in started_tool_ids:
                            started_tool_ids.add(item_id)
                            tool_input = _codex_tool_input(item)
                            yield {
                                "type": "tool_start",
                                "name": _codex_tool_name(item),
                                "tool_id": item_id,
                            }
                            if tool_input:
                                yield {
                                    "type": "tool_input_delta",
                                    "json": tool_input,
                                    "tool_id": item_id,
                                }
                        yield {
                            "type": "tool_result",
                            "content": _codex_tool_output(item),
                            "tool_id": item_id,
                            "is_error": _codex_tool_failed(item),
                        }
                        # Tool output is data, not turn authority. Reading a
                        # log/source file can contain any error wording. Even
                        # a genuinely failed command can be recovered by the
                        # agent; only the native terminal event ends the turn.
                    elif item_type in _IGNORED_APP_SERVER_ITEM_TYPES:
                        pass
                    elif item_type and item_type not in unknown_item_types:
                        unknown_item_types.add(item_type)
                        print(f"[CODEX] Ignoring item.completed type={item_type}: {json.dumps(item, default=str)[:500]}")

                elif etype in ("item.updated", "item.delta", "item/agentMessage/delta", "item/reasoning/delta"):
                    turn_activity_seen = True
                    item = raw.get("item") or params.get("item") or {}
                    delta = (
                        raw.get("delta")
                        or params.get("delta")
                        or item.get("delta")
                        or raw.get("text")
                        or params.get("text")
                        or raw.get("content")
                    )
                    item_type = item.get("type") or raw.get("item_type") or (
                        "reasoning" if etype == "item/reasoning/delta" else "agentMessage"
                    )
                    text = _text_from_value(delta)
                    if item_type in ("reasoning", "reasoningSummary", "reasoning_summary", "summary") and text:
                        yield {"type": "thinking_delta", "text": text}
                    elif item_type in ("agent_message", "agentMessage", "message", "assistant_message", "assistantMessage", "output_text") and text:
                        full_text += text
                        yield {"type": "text_delta", "text": text}

                elif etype in ("turn.completed", "turn/completed"):
                    event_turn_id = _codex_turn_id(raw, params)
                    event_thread_id = params.get("threadId") or raw.get("thread_id")
                    if (event_turn_id and active_turn_id and event_turn_id != active_turn_id) or (event_thread_id and event_thread_id != session_id):
                        continue
                    turn = params.get("turn") or raw.get("turn") or {}
                    for item in turn.get("items") or []:
                        item_id = _codex_item_id(item, raw)
                        content = _codex_item_text(item)
                        if _is_codex_tool_item(item):
                            if item_id not in started_tool_ids:
                                started_tool_ids.add(item_id)
                                yield {
                                    "type": "tool_start",
                                    "name": _codex_tool_name(item),
                                    "tool_id": item_id,
                                }
                            yield {
                                "type": "tool_result",
                                "content": _codex_tool_output(item),
                                "tool_id": item_id,
                                "is_error": _codex_tool_failed(item),
                            }
                        elif (
                            item.get("type") in ("agent_message", "agentMessage", "message", "assistant_message", "assistantMessage", "output_text")
                            or item.get("role") == "assistant"
                        ) and content and not full_text:
                            full_text += content
                            yield {"type": "text_delta", "text": content}
                    completion_error = _codex_completed_turn_error(raw, params)
                    if not completion_error and not full_text:
                        completion_error = "Codex turn completed without an assistant response"
                    if completion_error:
                        print(
                            "[CODEX] Empty or failed turn/completed: "
                            f"{json.dumps(raw, default=str)[:4000]}"
                        )
                    got_result = True
                    await _cleanup()
                    yield {
                        "type": "result",
                        "is_error": bool(completion_error),
                        "terminal_confirmed": bool(active_turn_id and event_turn_id == active_turn_id),
                        "error_code": (turn.get("error") or {}).get("codexErrorInfo") if isinstance(turn.get("error"), dict) else None,
                        "result_text": full_text,
                        "session_id": session_id,
                        "error": completion_error,
                    }
                    break

                elif etype in ("warning", "configWarning") or (etype == "error" and params.get("willRetry")):
                    yield {"type": "status", "text": str(params.get("message") or "Codex is retrying an upstream request")}

                elif etype == "error":
                    # App-server reports an error before turn/completed. It is
                    # not permission to kill the thread before it saves that
                    # terminal state. Bound the wait if the terminal is lost.
                    event_turn_id = _codex_turn_id(raw, params)
                    if event_turn_id and active_turn_id and event_turn_id != active_turn_id:
                        continue
                    error = params.get("error") or raw.get("error") or {}
                    pending_terminal_error = str(error.get("message") if isinstance(error, dict) else error) or str(params.get("message") or "Provider error")
                    yield {"type": "status", "text": pending_terminal_error}

                elif etype in ("turn.failed", "turn/failed"):
                    got_result = True
                    error = raw.get("error") or params.get("error") or {}
                    err_msg = error.get("message") if isinstance(error, dict) else error
                    err_msg = err_msg or raw.get("message") or params.get("message") or "Unknown error"
                    await _cleanup()
                    yield {
                        "type": "result",
                        "is_error": True,
                        "result_text": full_text,
                        "session_id": session_id,
                        "error": err_msg,
                    }
                    break

                elif etype == "codex_app_server_eof":
                    got_result = True
                    err_msg = str(
                        raw.get("message")
                        or "Codex app-server exited before completing the turn"
                    )
                    if raw.get("returncode") not in (None, 0):
                        err_msg += f" (exit code {raw['returncode']})"
                    stderr_tail = "\n".join(stderr_lines[-20:]).strip()
                    if stderr_tail:
                        err_msg += f"\n{stderr_tail}"
                    await _cleanup()
                    yield {
                        "type": "result",
                        "is_error": True,
                        "result_text": full_text,
                        "session_id": session_id,
                        "error": err_msg,
                    }
                    break

                elif etype in _IGNORED_APP_SERVER_EVENTS:
                    pass
                elif etype not in unknown_event_types:
                    unknown_event_types.add(str(etype))
                    print(f"[CODEX] Ignoring event type={etype}: {json.dumps(raw, default=str)[:500]}")

        except asyncio.CancelledError:
            # Async-generator cancellation otherwise skips the ordinary code
            # below this handler.  Do not release Loom's generation lane until
            # the app-server process tree is confirmed gone.
            await asyncio.shield(_cleanup())
            raise
        except Exception as e:
            log.error(f"[CODEX] Error processing event stream: {e}")
            await _cleanup()
            yield {
                "type": "result",
                "is_error": True,
                "result_text": full_text,
                "session_id": session_id,
                "error": str(e),
            }

        await _cleanup()

        # Emit default result if not already yielded
        if not got_result:
            err_text = "\n".join(stderr_lines[-20:])
            yield {
                "type": "result",
                "is_error": proc.returncode != 0,
                "result_text": full_text or err_text,
                "session_id": session_id,
                "error": err_text if proc.returncode != 0 else "",
            }

    return proc, _event_stream()


async def cancel_codex(proc):
    """Kill a running Codex subprocess and its process tree."""
    if proc.returncode is None:
        if sys.platform == 'win32':
            import subprocess
            try:
                result = await asyncio.to_thread(
                    subprocess.run,
                    ['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                    capture_output=True,
                    timeout=5,
                )
                if result.returncode != 0 and proc.returncode is None:
                    proc.kill()
            except Exception:
                proc.kill()
        else:
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
