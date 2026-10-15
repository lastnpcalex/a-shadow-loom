"""Locked MCP facade for EXO Search Space.

This process exposes no filesystem, shell, network-fetch, or mutation tools.
Its workspace scope is fixed by server-provided workspace keys.
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP


mcp = FastMCP("exo-search-space")


def _config() -> tuple[str, list[str]] | str:
    api = os.environ.get("LOOM_API_URL", "").rstrip("/")
    host = urlparse(api).hostname or ""
    if not api or host not in {"localhost", "127.0.0.1", "::1"}:
        return "Search Space requires a loopback LOOM_API_URL."
    try:
        keys = json.loads(os.environ.get("LOOM_SEARCH_WORKSPACE_KEYS", "[]"))
    except json.JSONDecodeError:
        return "LOOM_SEARCH_WORKSPACE_KEYS is invalid JSON."
    if not isinstance(keys, list) or not keys or not all(isinstance(key, str) and key for key in keys):
        return "Search Space has no assigned workspace scope."
    return api, list(dict.fromkeys(keys))


def _post(path: str, payload: dict) -> str:
    configured = _config()
    if isinstance(configured, str):
        return configured
    api, keys = configured
    payload = {**payload, "workspace_keys": keys}
    try:
        response = httpx.post(f"{api}{path}", json=payload, timeout=240, verify=False)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except Exception:
                detail = None
            return f"Search Space error ({response.status_code}): {detail or response.text[:500]}"
        return json.dumps(response.json(), ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Search Space unavailable: {exc}"


@mcp.tool()
def search_history(question: str) -> str:
    """Answer a natural-language question from the assigned EXO workspaces.

    Cloud-classified scopes use the configured inexpensive remote model. Any
    sensitive scope forces local llama-server. Returned claims cite message
    sources; this tool cannot access unassigned or excluded workspaces.
    """
    return _post("/api/search-space/query", {"question": question})


@mcp.tool()
def find_excerpts(query: str, limit: int = 24) -> str:
    """Run deterministic local full-text retrieval without model synthesis."""
    return _post("/api/search-space/retrieve", {"query": query, "limit": max(1, min(int(limit), 50))})


if __name__ == "__main__":
    mcp.run(transport="stdio")
