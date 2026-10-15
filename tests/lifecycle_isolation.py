"""Opt-in pytest plugin: disable unrelated live sidecars/discovery in preflight."""
from unittest.mock import AsyncMock
import pytest


@pytest.fixture(autouse=True)
def isolated_services(monkeypatch, tmp_path):
    import server
    import context_manager
    monkeypatch.setattr(server, "default_recovery_root", lambda: tmp_path / "recovery")
    monkeypatch.setattr(server.local_summary, "preload", AsyncMock())
    monkeypatch.setattr(context_manager, "update_rolling_summary", AsyncMock())
    monkeypatch.setattr(server, "health_check", AsyncMock(return_value={"status": "offline", "models": []}))
    pass
    # all-model discovery uses module clients directly, not server.health_check.
    # Keep those optional picker requests away from running sidecars and disks.
    import llama_client
    import dream_client
    monkeypatch.setattr(llama_client, "health_check", AsyncMock(return_value={"status":"offline", "models":[]}))
    monkeypatch.setattr(llama_client, "list_local_models", lambda: [])
    monkeypatch.setattr(dream_client, "health", AsyncMock(return_value=None))
