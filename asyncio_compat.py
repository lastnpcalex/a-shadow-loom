"""Bounded Windows asyncio workaround, shared by imported and CLI servers."""

import sys
from functools import wraps


def install_windows_transport_cleanup() -> bool:
    """Finish Proactor socket cleanup when peer shutdown raises on Windows.

    Python's callback notifies the protocol before shutting down the socket.
    A peer reset during shutdown skips close(), server detach and the callback
    completion flag. Catch only peer-disconnect errors and finish those steps;
    protocol errors and unexpected OS errors must remain observable.
    """
    if sys.platform != "win32":
        return False
    from asyncio.proactor_events import _ProactorBasePipeTransport

    original = _ProactorBasePipeTransport._call_connection_lost
    if getattr(original, "_loom_complete_cleanup", False):
        return False

    @wraps(original)
    def connection_lost(self, exc):
        try:
            original(self, exc)
        except OSError as error:
            peer_closed = isinstance(error, (
                ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
            )) or getattr(error, "winerror", None) in {10053, 10054, 10057}
            if getattr(self, "_sock", None) is None or not peer_closed:
                raise
        finally:
            sock = getattr(self, "_sock", None)
            if sock is not None:
                self._sock = None
                try:
                    sock.close()
                finally:
                    server = getattr(self, "_server", None)
                    self._server = None
                    try:
                        if server is not None:
                            server._detach(self)
                    finally:
                        self._called_connection_lost = True

    connection_lost._loom_complete_cleanup = True
    _ProactorBasePipeTransport._call_connection_lost = connection_lost
    return True
