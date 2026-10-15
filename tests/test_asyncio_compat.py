"""Exercise Python's actual Windows transport callback with bounded doubles."""

import sys
from types import SimpleNamespace

import pytest

from asyncio_compat import install_windows_transport_cleanup


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Proactor transport")
@pytest.mark.parametrize("shutdown_error", [
    None, ConnectionResetError("peer reset"), BrokenPipeError("broken pipe"),
    ConnectionAbortedError("peer aborted"), OSError("unexpected shutdown error"),
])
def test_transport_closes_and_detaches_after_shutdown(shutdown_error):
    from asyncio.proactor_events import _ProactorBasePipeTransport

    notified, closed, detached = [], [], []

    def shutdown(how):
        if shutdown_error:
            raise shutdown_error

    sock = SimpleNamespace(fileno=lambda: 42, shutdown=shutdown,
                           close=lambda: closed.append(True))
    transport = SimpleNamespace(
        _called_connection_lost=False, _sock=sock,
        _protocol=SimpleNamespace(connection_lost=lambda exc: notified.append(exc)),
        _server=SimpleNamespace(_detach=lambda current: detached.append(current)),
    )
    install_windows_transport_cleanup()
    callback = _ProactorBasePipeTransport._call_connection_lost
    if shutdown_error and type(shutdown_error) is OSError:
        with pytest.raises(OSError, match="unexpected shutdown error"):
            callback(transport, None)
    else:
        callback(transport, None)
    assert notified == [None] and closed == [True] and detached == [transport]
    assert transport._sock is None and transport._server is None
    assert transport._called_connection_lost
    callback(transport, None)
    assert notified == [None] and closed == [True] and detached == [transport]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Proactor transport")
def test_protocol_error_is_preserved_and_installation_is_idempotent():
    from asyncio.proactor_events import _ProactorBasePipeTransport

    def lost(exc):
        raise ConnectionResetError("protocol callback failed")

    closed = []
    transport = SimpleNamespace(
        _called_connection_lost=False, _server=None,
        _protocol=SimpleNamespace(connection_lost=lost),
        _sock=SimpleNamespace(fileno=lambda: 42, shutdown=lambda how: None,
                              close=lambda: closed.append(True)),
    )
    install_windows_transport_cleanup()
    callback = _ProactorBasePipeTransport._call_connection_lost
    assert install_windows_transport_cleanup() is False
    assert _ProactorBasePipeTransport._call_connection_lost is callback
    with pytest.raises(ConnectionResetError, match="protocol callback failed"):
        callback(transport, None)
    assert closed == [True] and transport._sock is None


def test_non_windows_installation_does_nothing(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert install_windows_transport_cleanup() is False
