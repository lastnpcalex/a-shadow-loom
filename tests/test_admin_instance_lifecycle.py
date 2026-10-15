import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, mock_open

import pytest
from fastapi.responses import JSONResponse

import admin_server


_TEST_INSTANCE = "lifecycle_test_main"
_TEST_PORT = 65432
_LIVE_LOOM_PORTS = {
    int(admin_server.INSTANCES["main"]["port"]),
    int(admin_server.ADMIN_PORT),
}


@pytest.fixture(autouse=True)
def restore_child_processes(monkeypatch, tmp_path):
    """Keep lifecycle tests physically isolated from the Loom hosting pytest.

    These tests call admin route functions directly. Using the production
    instance name once allowed an incompletely mocked test to POST /shutdown
    to the active server. A disposable instance plus live-port tripwires makes
    that failure mode impossible even when a future test forgets a mock.
    """
    original = dict(admin_server._child_procs)
    blocked_live_calls: list[tuple[str, int, str]] = []
    async def guarded_get(port, path):
        if int(port) in _LIVE_LOOM_PORTS:
            blocked_live_calls.append(("GET", int(port), str(path)))
            raise AssertionError("lifecycle test attempted to contact the active Loom host")
        raise ConnectionError("isolated lifecycle test instance has no listener")

    async def guarded_post(port, path):
        if int(port) in _LIVE_LOOM_PORTS:
            blocked_live_calls.append(("POST", int(port), str(path)))
            raise AssertionError("lifecycle test attempted to contact the active Loom host")
        raise ConnectionError("isolated lifecycle test instance has no listener")

    admin_server._child_procs.clear()
    admin_server._instance_lifecycle_locks.clear()
    admin_server._admin_restart_requested = False
    monkeypatch.setattr(admin_server, '_admin_restart_helper', None)
    monkeypatch.setattr(admin_server, '_admin_restart_requested_at', None)
    admin_server.INSTANCES[_TEST_INSTANCE] = {
        "port": _TEST_PORT,
        "label": "Isolated lifecycle test",
        "db": str(tmp_path / "lifecycle-test.db"),
    }
    monkeypatch.setattr(admin_server, "_get_instance", guarded_get)
    monkeypatch.setattr(admin_server, "_post_instance", guarded_post)
    try:
        yield
        assert blocked_live_calls == []
    finally:
        admin_server.INSTANCES.pop(_TEST_INSTANCE, None)
        admin_server._child_procs.clear()
        admin_server._child_procs.update(original)
        admin_server._instance_lifecycle_locks.clear()
        admin_server._admin_restart_requested = False


def _discovery(*identities, errors=()):
    return admin_server._InstanceProcessDiscovery(
        identities=tuple(identities), errors=tuple(errors)
    )


def test_pytest_cannot_target_live_main_instance(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "lifecycle safety tripwire")
    with pytest.raises(RuntimeError, match="isolated test instance"):
        admin_server._assert_safe_lifecycle_target("main")
    admin_server._assert_safe_lifecycle_target(_TEST_INSTANCE)


@pytest.mark.asyncio
async def test_pytest_cannot_invoke_live_admin_restart(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "admin self-restart tripwire")
    popen = Mock()
    monkeypatch.setattr(admin_server.subprocess, "Popen", popen)

    response = await admin_server.admin_restart()

    assert response.status_code == 409
    assert "pytest" in json.loads(response.body)["error"]
    popen.assert_not_called()


def test_admin_restart_helper_waits_for_exact_pid_before_spawn(monkeypatch, tmp_path):
    identity = (5151, 123.5)
    waits = Mock(return_value=True)
    popen = Mock(return_value=SimpleNamespace(pid=5252))
    monkeypatch.setattr(admin_server, "_wait_for_identity_exit_blocking", waits)
    monkeypatch.setattr(admin_server, "_admin_port_is_free", Mock(return_value=True))
    monkeypatch.setattr(admin_server.subprocess, "Popen", popen)

    result = admin_server._run_admin_restart_helper(
        identity,
        port=65428,
        admin_script=tmp_path / "admin_server.py",
        admin_dir=tmp_path,
        child_log=tmp_path / "admin.log",
    )

    assert result == 0
    waits.assert_called_once_with(
        identity, admin_server._ADMIN_SELF_GRACEFUL_EXIT_TIMEOUT
    )
    command = popen.call_args.args[0]
    assert command == [sys.executable, str(tmp_path / "admin_server.py")]


def test_admin_restart_helper_real_pid_handoff(monkeypatch, tmp_path):
    import psutil

    marker = tmp_path / "replacement-started.txt"
    replacement_script = tmp_path / "replacement_admin.py"
    replacement_script.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('started', encoding='utf-8')\n",
        encoding="utf-8",
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    old_process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.4)"],
        creationflags=flags,
    )
    identity = (old_process.pid, float(psutil.Process(old_process.pid).create_time()))
    spawn = subprocess.Popen
    replacements = []

    def capture_replacement(*args, **kwargs):
        replacement = spawn(*args, **kwargs)
        replacements.append(replacement)
        return replacement

    monkeypatch.setattr(admin_server.subprocess, 'Popen', capture_replacement)
    monkeypatch.setattr(admin_server, "_ADMIN_SELF_GRACEFUL_EXIT_TIMEOUT", 3)
    monkeypatch.setattr(admin_server, "_ADMIN_SELF_FORCE_EXIT_TIMEOUT", 1)
    started_at = time.monotonic()
    try:
        result = admin_server._run_admin_restart_helper(
            identity,
            port=65425,
            admin_script=replacement_script,
            admin_dir=tmp_path,
            child_log=tmp_path / "admin.log",
        )
        assert result == 0
        assert time.monotonic() - started_at >= 0.2
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.read_text(encoding="utf-8") == "started"
        assert old_process.wait(timeout=1) == 0
    finally:
        if old_process.poll() is None:
            old_process.kill()
            old_process.wait(timeout=3)
        for replacement in replacements:
            replacement.wait(timeout=3)


def test_admin_restart_helper_refuses_while_exact_pid_survives(
    monkeypatch, tmp_path
):
    identity = (5353, 456.5)
    waits = Mock(side_effect=[False, False])
    terminate = Mock()
    popen = Mock()
    monkeypatch.setattr(admin_server, "_wait_for_identity_exit_blocking", waits)
    monkeypatch.setattr(admin_server, "_terminate_exact_process", terminate)
    monkeypatch.setattr(admin_server.subprocess, "Popen", popen)

    result = admin_server._run_admin_restart_helper(
        identity,
        port=65427,
        admin_script=tmp_path / "admin_server.py",
        admin_dir=tmp_path,
        child_log=tmp_path / "admin.log",
    )

    assert result == 2
    terminate.assert_called_once_with(identity)
    popen.assert_not_called()


def test_admin_restart_helper_refuses_occupied_port(monkeypatch, tmp_path):
    monkeypatch.setattr(
        admin_server, "_wait_for_identity_exit_blocking", Mock(return_value=True)
    )
    monkeypatch.setattr(admin_server, "_ADMIN_SELF_FORCE_EXIT_TIMEOUT", 0)
    monkeypatch.setattr(admin_server, "_admin_port_is_free", Mock(return_value=False))
    popen = Mock()
    monkeypatch.setattr(admin_server.subprocess, "Popen", popen)

    result = admin_server._run_admin_restart_helper(
        (5454, 789.5),
        port=65426,
        admin_script=tmp_path / "admin_server.py",
        admin_dir=tmp_path,
        child_log=tmp_path / "admin.log",
    )

    assert result == 3
    popen.assert_not_called()


@pytest.mark.asyncio
async def test_admin_restart_route_spawns_exact_pid_helper(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(
        admin_server,
        "_process_identity",
        Mock(return_value=(5555, 1000.25)),
    )
    popen = Mock(return_value=SimpleNamespace(pid=5656))
    monkeypatch.setattr(admin_server.subprocess, "Popen", popen)
    monkeypatch.setattr("builtins.open", mock_open())

    scheduled = []

    def capture_task(coroutine):
        scheduled.append(coroutine)
        coroutine.close()
        return SimpleNamespace()

    monkeypatch.setattr(admin_server.asyncio, "create_task", capture_task)

    response = await admin_server.admin_restart()
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["old_pid"] == 5555
    assert payload["helper_pid"] == 5656
    command = popen.call_args.args[0]
    assert command[2:] == [
        "--restart-admin-after-pid",
        "5555",
        "1000.25",
        str(admin_server.ADMIN_PORT),
    ]
    assert scheduled


def test_admin_dashboard_waits_for_replacement_pid():
    source = Path("static/admin/admin.js").read_text(encoding="utf-8")

    assert "window.currentAdminPid" in source
    assert "status.admin_pid !== oldPid" in source
    assert "Date.now() - start < 40000" in source


async def test_admin_repeat_restart_retains_identity_without_respawning(monkeypatch):
    monkeypatch.delenv('PYTEST_CURRENT_TEST', raising=False)
    monkeypatch.setattr(admin_server, '_admin_restart_requested', True)
    monkeypatch.setattr(admin_server, '_admin_restart_helper', SimpleNamespace(pid=5656, poll=lambda: None))
    popen = Mock()
    monkeypatch.setattr(admin_server.subprocess, 'Popen', popen)
    response = await admin_server.admin_restart()
    data = json.loads(response.body)
    assert response.status_code == 202
    assert data['old_pid'] == admin_server.os.getpid()
    assert data['helper_pid'] == 5656
    assert data['helper_exit_code'] is None
    popen.assert_not_called()


@pytest.mark.parametrize('exit_code', [0, 2])
async def test_explicit_admin_retry_after_helper_exit_is_not_latched(monkeypatch, exit_code):
    monkeypatch.delenv('PYTEST_CURRENT_TEST', raising=False)
    monkeypatch.setattr(admin_server, '_admin_restart_requested', True)
    monkeypatch.setattr(admin_server, '_admin_restart_helper', SimpleNamespace(pid=5656, poll=lambda: exit_code))
    monkeypatch.setattr(admin_server, '_process_identity', Mock(return_value=(5555, 1000.25)))
    monkeypatch.setattr('builtins.open', mock_open())
    # Deliberately fail the mocked launch: no OS process or exit task is ever created.
    popen = Mock(side_effect=OSError('fixture launch failure'))
    monkeypatch.setattr(admin_server.subprocess, 'Popen', popen)
    response = await admin_server.admin_restart()
    assert response.status_code == 500
    assert 'fixture launch failure' in json.loads(response.body)['error']
    popen.assert_called_once()
    assert not admin_server._admin_restart_requested


async def test_admin_restart_log_failure_is_structured_and_does_not_launch(monkeypatch):
    monkeypatch.delenv('PYTEST_CURRENT_TEST', raising=False)
    monkeypatch.setattr(admin_server, '_process_identity', Mock(return_value=(5555, 1000.25)))
    monkeypatch.setattr('builtins.open', Mock(side_effect=PermissionError('fixture log unavailable')))
    popen = Mock()
    monkeypatch.setattr(admin_server.subprocess, 'Popen', popen)
    response = await admin_server.admin_restart()
    data = json.loads(response.body)
    assert response.status_code == 500
    assert 'Could not open admin restart log' in data['error']
    assert data['old_pid'] == 5555
    assert not admin_server._admin_restart_requested
    popen.assert_not_called()


async def test_admin_status_reports_exited_restart_helper(monkeypatch):
    monkeypatch.setattr(admin_server, 'check_instance', AsyncMock(return_value={'status': 'offline'}))
    monkeypatch.setattr(admin_server, '_admin_restart_requested', True)
    monkeypatch.setattr(admin_server, '_admin_restart_helper', SimpleNamespace(pid=5656, poll=lambda: 2))
    response = await admin_server.api_status()
    assert json.loads(response.body)['admin_restart'] == {
        'requested': True, 'requested_at': None, 'helper_pid': 5656, 'helper_exit_code': 2}


@pytest.mark.asyncio
async def test_admin_status_exposes_serving_pid(monkeypatch):
    monkeypatch.setattr(
        admin_server,
        "check_instance",
        AsyncMock(return_value={"status": "offline"}),
    )

    response = await admin_server.api_status()
    payload = json.loads(response.body)

    assert payload["admin_pid"] == admin_server.os.getpid()


@pytest.mark.asyncio
async def test_restart_keeps_old_pid_until_actual_exit(monkeypatch):
    identity = (4242, 123.0)
    admin_server._child_procs[_TEST_INSTANCE] = SimpleNamespace(pid=identity[0])
    monkeypatch.setattr(
        admin_server, "_collect_instance_processes", lambda _name: _discovery(identity)
    )
    monkeypatch.setattr(admin_server, "_get_instance", AsyncMock(return_value=object()))
    monkeypatch.setattr(admin_server, "_post_instance", AsyncMock(return_value=object()))

    waits = 0

    async def wait_for_exit(identities, _timeout):
        nonlocal waits
        waits += 1
        assert identities == [identity]
        assert _TEST_INSTANCE in admin_server._child_procs
        return [identity] if waits == 1 else []

    forced = Mock()
    monkeypatch.setattr(admin_server, "_wait_for_instance_exit", wait_for_exit)
    monkeypatch.setattr(admin_server, "_force_terminate_process_trees", forced)

    async def start_after_exit(name):
        assert name == _TEST_INSTANCE
        assert _TEST_INSTANCE not in admin_server._child_procs
        return JSONResponse({"status": "replacement started"})

    monkeypatch.setattr(admin_server, "_start_instance_locked", start_after_exit)
    response = await admin_server.action_restart(_TEST_INSTANCE)

    assert response.status_code == 200
    assert json.loads(response.body)["status"] == "replacement started"
    forced.assert_called_once_with([identity])


@pytest.mark.asyncio
async def test_restart_refuses_replacement_while_old_pid_survives(monkeypatch):
    identity = (4343, 456.0)
    admin_server._child_procs[_TEST_INSTANCE] = SimpleNamespace(pid=identity[0])
    monkeypatch.setattr(
        admin_server, "_collect_instance_processes", lambda _name: _discovery(identity)
    )
    monkeypatch.setattr(admin_server, "_get_instance", AsyncMock(return_value=object()))
    monkeypatch.setattr(admin_server, "_post_instance", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        admin_server,
        "_wait_for_instance_exit",
        AsyncMock(return_value=[identity]),
    )
    monkeypatch.setattr(admin_server, "_force_terminate_process_trees", Mock())
    start = AsyncMock()
    monkeypatch.setattr(admin_server, "_start_instance_locked", start)

    response = await admin_server.action_restart(_TEST_INSTANCE)
    payload = json.loads(response.body)

    assert response.status_code == 409
    assert "4343" in payload["error"]
    assert "replacement refused" in payload["error"]
    assert _TEST_INSTANCE in admin_server._child_procs
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_refuses_listenerless_database_owner(monkeypatch):
    identity = (4444, 789.0)
    monkeypatch.setattr(
        admin_server, "_collect_instance_processes", lambda _name: _discovery(identity)
    )
    popen = Mock()
    monkeypatch.setattr(admin_server.subprocess, "Popen", popen)

    response = await admin_server.action_start(_TEST_INSTANCE)
    payload = json.loads(response.body)

    assert response.status_code == 409
    assert payload["pids"] == [4444]
    assert "still alive" in payload["error"]
    popen.assert_not_called()


@pytest.mark.asyncio
async def test_offline_probe_retains_live_managed_pid(monkeypatch):
    class FailingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, _url):
            raise ConnectionError("listener already closed")

    admin_server._child_procs[_TEST_INSTANCE] = SimpleNamespace(pid=4545)
    monkeypatch.setattr(admin_server.httpx, "AsyncClient", FailingClient)
    monkeypatch.setattr(
        admin_server, "_process_identity", lambda pid, **_kwargs: (pid, 100.0)
    )

    status = await admin_server.check_instance(
        _TEST_INSTANCE, admin_server.INSTANCES[_TEST_INSTANCE]
    )

    assert status["status"] == "stopping"
    assert status["managed"] is True
    assert status["pid"] == 4545
    assert admin_server._child_procs[_TEST_INSTANCE].pid == 4545


@pytest.mark.asyncio
async def test_listenerless_tracked_process_is_reaped_before_stop_succeeds(monkeypatch):
    name = "restart_probe"
    admin_server.INSTANCES[name] = {
        "port": 65431,
        "label": "Restart lifecycle probe",
        "db": "restart_probe_never_created.db",
    }
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=flags,
    )
    admin_server._child_procs[name] = proc
    monkeypatch.setattr(admin_server, "_INSTANCE_GRACEFUL_EXIT_TIMEOUT", 0.05)
    monkeypatch.setattr(admin_server, "_INSTANCE_FORCE_EXIT_TIMEOUT", 3.0)
    monkeypatch.setattr(
        admin_server,
        "_get_instance",
        AsyncMock(side_effect=ConnectionError("no listener")),
    )
    monkeypatch.setattr(
        admin_server,
        "_post_instance",
        AsyncMock(side_effect=ConnectionError("no listener")),
    )
    try:
        stopped = await admin_server._stop_instance_and_verify(name)
        assert stopped["pids"] == [proc.pid]
        assert stopped["forced"] is True
        assert proc.wait(timeout=3) is not None
        assert name not in admin_server._child_procs
    finally:
        admin_server.INSTANCES.pop(name, None)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)


@pytest.mark.asyncio
async def test_concurrent_restarts_are_serialized_per_instance(monkeypatch):
    active_actions = 0
    maximum_active = 0

    async def stop(_name):
        nonlocal active_actions, maximum_active
        active_actions += 1
        maximum_active = max(maximum_active, active_actions)
        await asyncio.sleep(0.01)
        active_actions -= 1
        return {}

    async def start(_name):
        nonlocal active_actions, maximum_active
        active_actions += 1
        maximum_active = max(maximum_active, active_actions)
        await asyncio.sleep(0.01)
        active_actions -= 1
        return JSONResponse({"status": "started"})

    monkeypatch.setattr(admin_server, "_stop_instance_and_verify", stop)
    monkeypatch.setattr(admin_server, "_start_instance_locked", start)

    first, second = await asyncio.gather(
        admin_server.action_restart(_TEST_INSTANCE),
        admin_server.action_restart(_TEST_INSTANCE),
    )

    assert first.status_code == second.status_code == 200
    assert maximum_active == 1


@pytest.mark.asyncio
async def test_old_stop_cannot_discard_newer_process_handle(monkeypatch):
    old_handle = SimpleNamespace(pid=4646)
    new_handle = SimpleNamespace(pid=4747)
    identity = (4646, 100.0)
    admin_server._child_procs[_TEST_INSTANCE] = old_handle
    monkeypatch.setattr(
        admin_server, "_collect_instance_processes", lambda _name: _discovery(identity)
    )
    monkeypatch.setattr(
        admin_server, "_get_instance", AsyncMock(side_effect=ConnectionError())
    )

    async def wait_for_exit(_identities, _timeout):
        admin_server._child_procs[_TEST_INSTANCE] = new_handle
        return []

    monkeypatch.setattr(admin_server, "_wait_for_instance_exit", wait_for_exit)

    await admin_server._stop_instance_and_verify(_TEST_INSTANCE)

    assert admin_server._child_procs[_TEST_INSTANCE] is new_handle


def test_listener_enumeration_failure_does_not_skip_database_owner_scan(
    monkeypatch, tmp_path
):
    import psutil

    name = "discovery_probe"
    db_path = tmp_path / "probe.db"
    server_path = str((admin_server.Path(admin_server.__file__).parent / "server.py").resolve())
    admin_server.INSTANCES[name] = {
        "port": 65430,
        "label": "Discovery probe",
        "db": str(db_path),
    }
    fake_proc = SimpleNamespace(
        info={"pid": 4848, "cmdline": [sys.executable, server_path], "cwd": str(tmp_path)},
        open_files=lambda: [SimpleNamespace(path=str(db_path))],
    )
    monkeypatch.setattr(
        psutil, "net_connections", Mock(side_effect=psutil.AccessDenied(pid=1))
    )
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: [fake_proc])
    monkeypatch.setattr(
        admin_server,
        "_process_identity",
        lambda pid, **_kwargs: (int(pid), 200.0) if pid is not None else None,
    )
    try:
        result = admin_server._collect_instance_processes(name)
    finally:
        admin_server.INSTANCES.pop(name, None)

    assert result.identities == ((4848, 200.0),)
    assert result.complete is False
    assert "listeners" in result.errors[0]


@pytest.mark.asyncio
async def test_listenerless_sqlite_owner_is_reaped_before_write_and_restart(
    monkeypatch, tmp_path
):
    name = "sqlite_lock_probe"
    db_path = tmp_path / "locked.db"
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("CREATE TABLE probe (value TEXT)")
        connection.commit()

    child_code = (
        "import sqlite3,sys,time; "
        "c=sqlite3.connect(sys.argv[1]); "
        "c.execute('BEGIN IMMEDIATE'); "
        "c.execute(\"INSERT INTO probe VALUES ('child')\"); "
        "print('locked', flush=True); time.sleep(30)"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [sys.executable, "-c", child_code, str(db_path)],
        stdout=subprocess.PIPE,
        text=True,
        creationflags=flags,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "locked"
    admin_server.INSTANCES[name] = {
        "port": 65429,
        "label": "SQLite lock probe",
        "db": str(db_path),
    }
    admin_server._child_procs[name] = proc
    monkeypatch.setattr(admin_server, "_INSTANCE_GRACEFUL_EXIT_TIMEOUT", 0.05)
    monkeypatch.setattr(admin_server, "_INSTANCE_FORCE_EXIT_TIMEOUT", 3.0)
    monkeypatch.setattr(
        admin_server,
        "_get_instance",
        AsyncMock(side_effect=ConnectionError("listener already gone")),
    )
    monkeypatch.setattr(
        admin_server,
        "_post_instance",
        AsyncMock(side_effect=ConnectionError("listener already gone")),
    )
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with closing(sqlite3.connect(db_path, timeout=0.05)) as connection:
                connection.execute("INSERT INTO probe VALUES ('before reap')")

        result = await admin_server._stop_instance_and_verify(name)

        assert result["forced"] is True
        assert proc.wait(timeout=3) is not None
        with closing(sqlite3.connect(db_path, timeout=1.0)) as connection:
            connection.execute("INSERT INTO probe VALUES ('after reap')")
            connection.commit()
            values = [row[0] for row in connection.execute("SELECT value FROM probe")]
        assert values == ["after reap"]
    finally:
        admin_server.INSTANCES.pop(name, None)
        admin_server._child_procs.pop(name, None)
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)
