"""Machine-local EXO state paths that must never live in a project checkout."""

from __future__ import annotations

import os
from pathlib import Path


def state_root() -> Path:
    """Return the external state root, preserving the historical directory name."""
    override = os.environ.get("EXO_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data).expanduser().resolve() / "A Shadow Loom"
    state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    if state_home:
        return Path(state_home).expanduser().resolve() / "a-shadow-loom"
    return Path.home().resolve() / ".local" / "state" / "a-shadow-loom"


def state_path(*parts: str) -> Path:
    return state_root().joinpath(*parts)
