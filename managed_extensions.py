"""External, manifest-driven services managed by EXO's admin process.

The registry contains references only. Manifests and commands stay in the
workspace that owns them, so adding an extension never changes the EXO repo.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any
from urllib.parse import urlparse

import httpx
import psutil

from exo_state import state_path


_ID_RE = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
_KINDS = {"service", "mcp"}


class ExtensionError(RuntimeError):
    pass


def _resolved(base: Path, value: str | None, default: str = ".") -> Path:
    candidate = Path(value or default).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _command(value: Any, field: str, *, required: bool = False) -> list[str] | None:
    if value is None and not required:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise ExtensionError(f"{field} must be a non-empty JSON string array")
    if any("\x00" in v for v in value):
        raise ExtensionError(f"{field} contains an invalid character")
    return list(value)


@dataclass(frozen=True)
class ExtensionManifest:
    id: str
    name: str
    kind: str
    manifest_path: Path
    workspace_root: Path
    cwd: Path
    command: list[str]
    update_command: list[str] | None
    health_url: str | None
    environment: dict[str, str]

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "manifest_path": str(self.manifest_path),
            "workspace_root": str(self.workspace_root),
            "cwd": str(self.cwd),
            "command": self.command,
            "can_update": bool(self.update_command),
            "health_url": self.health_url,
        }


def load_manifest(path: str | Path) -> ExtensionManifest:
    manifest_path = Path(path).expanduser().resolve()
    if not manifest_path.is_file():
        raise ExtensionError(f"Extension manifest not found: {manifest_path}")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExtensionError(f"Could not read extension manifest: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ExtensionError("Extension manifest must have schema_version 1")
    extension_id = str(raw.get("id") or "").strip().lower()
    if not _ID_RE.fullmatch(extension_id):
        raise ExtensionError("Extension id must be 2-64 lowercase letters, digits, dots, dashes, or underscores")
    kind = str(raw.get("kind") or "service").strip().lower()
    if kind not in _KINDS:
        raise ExtensionError("Extension kind must be service or mcp")
    root = _resolved(manifest_path.parent, raw.get("workspace_root"))
    cwd = _resolved(manifest_path.parent, raw.get("cwd"))
    if not root.is_dir():
        raise ExtensionError(f"Extension workspace root does not exist: {root}")
    if not cwd.is_dir() or not _inside(cwd, root):
        raise ExtensionError("Extension cwd must be an existing directory inside workspace_root")
    start = raw.get("start") if isinstance(raw.get("start"), dict) else {}
    update = raw.get("update") if isinstance(raw.get("update"), dict) else {}
    health = raw.get("health") if isinstance(raw.get("health"), dict) else {}
    command = _command(start.get("command") or raw.get("command"), "start.command", required=True)
    update_command = _command(update.get("command"), "update.command")
    health_url = str(health.get("url") or "").strip() or None
    if health_url:
        parsed = urlparse(health_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ExtensionError("health.url must be an HTTP(S) loopback URL")
    env = raw.get("environment") or {}
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise ExtensionError("environment must map string names to string values")
    return ExtensionManifest(
        id=extension_id,
        name=str(raw.get("name") or extension_id).strip()[:120],
        kind=kind,
        manifest_path=manifest_path,
        workspace_root=root,
        cwd=cwd,
        command=command or [],
        update_command=update_command,
        health_url=health_url,
        environment=dict(env),
    )


class ExtensionRegistry:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root).resolve() if root else state_path("extensions")
        self.registry_path = self.root / "registry.json"
        self.runtime_path = self.root / "runtime.json"
        self.logs_dir = self.root / "logs"
        self._handles: dict[str, subprocess.Popen] = {}

    def _read(self, path: Path, default: dict) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else dict(default)
        except (OSError, json.JSONDecodeError):
            return dict(default)

    def _write(self, path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temp, path)

    def _refs(self) -> dict[str, str]:
        raw = self._read(self.registry_path, {"schema_version": 1, "extensions": {}})
        refs = raw.get("extensions") or {}
        return {str(k): str(v) for k, v in refs.items()} if isinstance(refs, dict) else {}

    def _runtime(self) -> dict[str, dict]:
        raw = self._read(self.runtime_path, {"schema_version": 1, "processes": {}})
        procs = raw.get("processes") or {}
        return {str(k): dict(v) for k, v in procs.items() if isinstance(v, dict)} if isinstance(procs, dict) else {}

    def register(self, manifest_path: str | Path) -> dict:
        manifest = load_manifest(manifest_path)
        refs = self._refs()
        existing = refs.get(manifest.id)
        if existing and Path(existing).resolve() != manifest.manifest_path:
            raise ExtensionError(f"Extension id {manifest.id!r} is already registered from {existing}")
        refs[manifest.id] = str(manifest.manifest_path)
        self._write(self.registry_path, {"schema_version": 1, "extensions": refs})
        return self.describe(manifest.id)

    def unregister(self, extension_id: str) -> None:
        if self._identity(extension_id):
            raise ExtensionError("Stop the extension before unregistering it")
        refs = self._refs()
        if refs.pop(extension_id, None) is None:
            raise ExtensionError("Extension is not registered")
        self._write(self.registry_path, {"schema_version": 1, "extensions": refs})

    def manifest(self, extension_id: str) -> ExtensionManifest:
        path = self._refs().get(extension_id)
        if not path:
            raise ExtensionError("Extension is not registered")
        return load_manifest(path)

    def _identity(self, extension_id: str) -> tuple[psutil.Process, dict] | None:
        record = self._runtime().get(extension_id)
        if not record:
            return None
        try:
            proc = psutil.Process(int(record["pid"]))
            if abs(proc.create_time() - float(record["create_time"])) > 0.01 or not proc.is_running():
                return None
            return proc, record
        except (psutil.Error, KeyError, TypeError, ValueError):
            return None

    def _save_identity(self, extension_id: str, proc: psutil.Process | None) -> None:
        runtime = self._runtime()
        if proc is None:
            runtime.pop(extension_id, None)
        else:
            runtime[extension_id] = {"pid": proc.pid, "create_time": proc.create_time(), "started_at": time.time()}
        self._write(self.runtime_path, {"schema_version": 1, "processes": runtime})

    def _health(self, manifest: ExtensionManifest) -> dict:
        if not manifest.health_url:
            identity = self._identity(manifest.id)
            return {"state": "running" if identity else "stopped", "detail": "process identity"}
        try:
            response = httpx.get(manifest.health_url, timeout=httpx.Timeout(2.0, connect=0.5))
            # A 404 or 401 means the health route isn't answering as declared.
            return {"state": "healthy" if 200 <= response.status_code < 400 else "unhealthy",
                    "http_status": response.status_code}
        except Exception as exc:
            return {"state": "unreachable", "detail": str(exc)[:300]}

    def describe(self, extension_id: str) -> dict:
        manifest = self.manifest(extension_id)
        identity = self._identity(extension_id)
        return {**manifest.public(), "pid": identity[0].pid if identity else None, "health": self._health(manifest)}

    def list(self) -> list[dict]:
        items = []
        for extension_id in sorted(self._refs()):
            try:
                items.append(self.describe(extension_id))
            except Exception as exc:
                items.append({"id": extension_id, "state": "invalid", "error": str(exc)})
        return items

    def start(self, extension_id: str) -> dict:
        manifest = self.manifest(extension_id)
        if self._identity(extension_id):
            raise ExtensionError("Extension is already running")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.logs_dir / f"{extension_id}.log"
        log = open(log_path, "ab", buffering=0)
        kwargs: dict[str, Any] = {
            "cwd": str(manifest.cwd),
            "env": {**os.environ, **manifest.environment},
            "stdin": subprocess.DEVNULL,
            "stdout": log,
            "stderr": subprocess.STDOUT,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            handle = subprocess.Popen(manifest.command, **kwargs)
        except Exception:
            log.close()
            raise
        self._handles[extension_id] = handle
        proc = psutil.Process(handle.pid)
        self._save_identity(extension_id, proc)
        return self.describe(extension_id)

    def stop(self, extension_id: str, timeout: float = 8.0) -> dict:
        manifest = self.manifest(extension_id)
        identity = self._identity(extension_id)
        if not identity:
            self._save_identity(extension_id, None)
            return {**manifest.public(), "pid": None, "health": self._health(manifest)}
        proc, _ = identity
        processes = proc.children(recursive=True) + [proc]
        for child in reversed(processes):
            try:
                child.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(processes, timeout=max(0.1, timeout))
        for child in alive:
            try:
                child.kill()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(alive, timeout=3.0)
        if alive:
            raise ExtensionError(f"Extension process tree did not exit: {[p.pid for p in alive]}")
        self._save_identity(extension_id, None)
        self._handles.pop(extension_id, None)
        return {**manifest.public(), "pid": None, "health": self._health(manifest)}

    def update(self, extension_id: str, timeout: float = 300.0) -> dict:
        manifest = self.manifest(extension_id)
        if not manifest.update_command:
            raise ExtensionError("Extension manifest has no update command")
        if self._identity(extension_id):
            raise ExtensionError("Stop the extension before updating it")
        result = subprocess.run(
            manifest.update_command,
            cwd=str(manifest.cwd),
            env={**os.environ, **manifest.environment},
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = ((result.stdout or "") + (result.stderr or ""))[-8000:]
        return {"ok": result.returncode == 0, "returncode": result.returncode, "output": output}
