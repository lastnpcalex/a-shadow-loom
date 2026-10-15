import json
from pathlib import Path
import sys
import time

import pytest

from managed_extensions import ExtensionError, ExtensionRegistry, load_manifest


def write_manifest(path: Path, *, extension_id="fixture.service", cwd=".", command=None, **extra):
    data = {
        "schema_version": 1,
        "id": extension_id,
        "name": "Fixture service",
        "kind": "service",
        "workspace_root": ".",
        "cwd": cwd,
        "start": {"command": command or [sys.executable, "-c", "import time; time.sleep(60)"]},
        **extra,
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_registry_references_live_workspace_manifest_instead_of_copying_it(tmp_path):
    owner = tmp_path / "owner"
    owner.mkdir()
    manifest_path = write_manifest(owner / "exo-extension.json")
    registry = ExtensionRegistry(tmp_path / "external-state")
    registered = registry.register(manifest_path)
    assert registered["manifest_path"] == str(manifest_path.resolve())
    assert json.loads(registry.registry_path.read_text())["extensions"] == {
        "fixture.service": str(manifest_path.resolve())
    }
    data = json.loads(manifest_path.read_text())
    data["name"] = "Renamed by owner"
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    assert registry.describe("fixture.service")["name"] == "Renamed by owner"


def test_manifest_requires_array_commands_scoped_cwd_and_loopback_health(tmp_path):
    owner = tmp_path / "owner"
    owner.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    path = write_manifest(owner / "exo-extension.json", cwd=str(outside))
    with pytest.raises(ExtensionError, match="inside workspace_root"):
        load_manifest(path)
    write_manifest(path, health={"url": "https://example.com/status"})
    with pytest.raises(ExtensionError, match="loopback"):
        load_manifest(path)
    write_manifest(path, command="python service.py")
    with pytest.raises(ExtensionError, match="string array"):
        load_manifest(path)


@pytest.mark.parametrize("status,state", [(200, "healthy"), (302, "healthy"), (401, "unhealthy"),
                                          (404, "unhealthy"), (503, "unhealthy")])
def test_health_requires_success_status(tmp_path, monkeypatch, status, state):
    import httpx
    import managed_extensions
    owner = tmp_path / "owner"
    owner.mkdir()
    registry = ExtensionRegistry(tmp_path / "state")
    registry.register(write_manifest(owner / "exo-extension.json", health={"url": "http://127.0.0.1:9/health"}))
    monkeypatch.setattr(managed_extensions.httpx, "get", lambda *a, **k: httpx.Response(status))
    assert registry.describe("fixture.service")["health"]["state"] == state


def test_duplicate_ids_cannot_silently_change_owner(tmp_path):
    first = tmp_path / "first"; second = tmp_path / "second"
    first.mkdir(); second.mkdir()
    registry = ExtensionRegistry(tmp_path / "state")
    registry.register(write_manifest(first / "exo-extension.json"))
    with pytest.raises(ExtensionError, match="already registered"):
        registry.register(write_manifest(second / "exo-extension.json"))


def test_process_identity_survives_registry_reconstruction_and_stop(tmp_path):
    owner = tmp_path / "owner"
    owner.mkdir()
    registry = ExtensionRegistry(tmp_path / "state")
    registry.register(write_manifest(owner / "exo-extension.json"))
    started = registry.start("fixture.service")
    assert started["pid"] and started["health"]["state"] == "running"
    rebuilt = ExtensionRegistry(tmp_path / "state")
    assert rebuilt.describe("fixture.service")["pid"] == started["pid"]
    stopped = rebuilt.stop("fixture.service")
    assert stopped["pid"] is None
    time.sleep(0.05)
    assert rebuilt.describe("fixture.service")["health"]["state"] == "stopped"


def test_owner_portal_descriptor_does_not_install_host_features(tmp_path, monkeypatch):
    """Legacy companion data must not change generic service launch behavior."""
    import managed_extensions

    owner = tmp_path / "owner"
    owner.mkdir()
    (owner / "exo.portal.json").write_text("not a host configuration", encoding="utf-8")
    registry = ExtensionRegistry(tmp_path / "state")
    registry.register(write_manifest(owner / "exo-extension.json"))
    for key in ("EXO_EXTENSION_STATE_DIR", "EXO_EXTENSION_CONTROL_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    original_popen = managed_extensions.subprocess.Popen
    launches = []

    def record_launch(*args, **kwargs):
        launches.append(kwargs)
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(managed_extensions.subprocess, "Popen", record_launch)
    try:
        started = registry.start("fixture.service")
        assert started["pid"]
        assert "setup_path" not in started
        assert "EXO_EXTENSION_STATE_DIR" not in launches[0]["env"]
        assert "EXO_EXTENSION_CONTROL_TOKEN" not in launches[0]["env"]
        assert not (registry.root / "connections").exists()
    finally:
        registry.stop("fixture.service")
