"""Shared fixtures for Loom test suite."""

import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import httpx

import database as db

# The loopback/Tailscale gate and paired-browser requirement are exercised by
# tests/test_loom_auth.py; everywhere else test clients act as the paired user.
# setdefault so a subprocess server inherits the same choice.
os.environ.setdefault("LOOM_AUTH", "off")
os.environ.setdefault("LOOM_NETWORK_GATE", "off")

# database.DB_PATH starts as config.json's db_path, i.e. the LIVE database.
# Point it at a throwaway file before any test runs, so code that reaches the
# database outside the per-test fixture (a leaked task, a background thread,
# a fixture teardown race) can never open or lock the real one.
_LIVE_DB_PATH = str(db.DB_PATH)
_GUARD_DB_DIR = Path(tempfile.gettempdir()) / "loom-pytest-guard"
_GUARD_DB_DIR.mkdir(parents=True, exist_ok=True)
db.DB_PATH = str(_GUARD_DB_DIR / f"outside-fixture-{os.getpid()}.db")


def pytest_configure(config):
    # pytest's default basetemp root (%TEMP%/pytest-of-<user>) can be created
    # with an unreadable owner SID on Windows (e.g. by a sandboxed run); once
    # that happens every tmp_path fixture dies in PermissionError after a long
    # retry loop. Redirect to a private root unless the caller passed one.
    # Outside the repo on purpose: a OneDrive-synced basetemp invites sync
    # locks on test SQLite files.
    if config.option.basetemp is None:
        base = Path(tempfile.gettempdir()) / "loom-pytest"
        try:
            base.mkdir(parents=True, exist_ok=True)
            # Try creating a temporary test file in it to verify write permissions
            test_file = base / ".pytest_write_test"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink()
        except OSError:
            import getpass
            username = getpass.getuser()
            base = Path(tempfile.gettempdir()) / f"loom-pytest-{username}"
        config.option.basetemp = base

    # Register the live-Dream marker used by the Phase 0.5 tool-call probe.
    config.addinivalue_line(
        "markers", "live_dream: requires the live Dream sidecar at :8787"
    )


@pytest.fixture(autouse=True)
async def tmp_database(tmp_path):
    """Override DB_PATH to a temp file for every test, run migrations, clean up after."""
    db_file = str(tmp_path / "test_loom.db")
    original = db.DB_PATH
    assert Path(original).resolve() != Path(_LIVE_DB_PATH).resolve(), "tests must never use the live DB"
    db.DB_PATH = db_file
    await db.init_db()
    yield db_file
    await db.close_db()
    db.DB_PATH = original


@pytest.fixture(autouse=True)
def isolated_admin_route_tests(request, monkeypatch, tmp_path):
    """Old Admin route fixtures are HTTP-only and have no browser login.

    Keep their existing network/origin gate while testing route behavior.
    The real new Admin gate is exercised in test_admin_auth/test_loom_auth,
    including actual Chromium enrollment, Tailscale login and WS revocation.
    """
    name = Path(str(request.node.fspath)).name
    if (name.startswith('test_admin_') and name != 'test_admin_auth.py') or name == 'test_openrouter_admin_and_dream_gating.py':
        import admin_auth
        import loom_auth
        monkeypatch.setenv('EXO_STATE_DIR', str(tmp_path / 'admin-fixture-state'))
        async def fixture_gate(self, scope, receive, send):
            await loom_auth.LoomAccessMiddleware(self.app)(scope, receive, send)
        monkeypatch.setattr(admin_auth.AdminGate, '__call__', fixture_gate)


@pytest.fixture
def isolate_model_discovery(monkeypatch):
    """Opt-in controlled probes for browser fixtures; native fixtures opt out."""
    def install():
        import server
        import llama_client
        import dream_client

        async def health():
            return {
                "status": "mock", "models": [], "model_meta": {},
                "model_name_map": {}, "local_models": [],
                "model_available": False, "mock_mode": True,
            }

        monkeypatch.setattr(server, "health_check", health)
        monkeypatch.setattr(llama_client, "health_check", health)
        monkeypatch.setattr(dream_client, "health", AsyncMock(return_value=None))

    return install


@pytest.fixture
async def client():
    """Async HTTP test client using httpx + ASGITransport.

    Mocks local_summary.preload so we never load Gemma in tests.
    Mocks llama health_check to avoid real network calls.
    """
    with patch("local_summary.preload", new_callable=AsyncMock) as mock_preload, \
         patch("server.local_summary.preload", new_callable=AsyncMock):
        from server import app
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.fixture
def mock_llama():
    """Patch llama_client functions to avoid real network calls."""
    health_result = {
        "status": "ok",
        "models": ["Qwen3.6-27B-NVFP4.gguf"],
        "target_model": "Qwen3.6-27B-NVFP4.gguf",
        "model_available": True,
        "mock_mode": False,
    }

    async def fake_stream(*args, **kwargs):
        for token in ["Hello", " from", " mock", " Llama", "!"]:
            yield token

    with patch("server.health_check", new_callable=AsyncMock, return_value=health_result) as mock_hc, \
         patch("server.stream_chat", side_effect=fake_stream) as mock_sc:
        yield {"health_check": mock_hc, "stream_chat": mock_sc}
