"""Access control shared by the main and admin servers.

Two layers, both enforced by one ASGI middleware:

1. Network gate. Only loopback and Tailscale addresses may connect at all.
   Loom is reached remotely over Tailscale only; LAN and internet clients are
   refused before any route runs.
2. Legacy browser pairing is disabled at the owner's request. Pairing routes
   and stored sessions remain for compatibility, but no browser needs to pair.
   This applies to both Main and Admin, including direct Tailscale access.

Independent of pairing, the admin approval relay on the main server always
requires the machine relay header (see server._require_admin_relay), so a
local process cannot approve requests with a constant header.

Honest limit: a program running as the same OS user can still read the secret
file with deliberate effort. Only a separate OS account for agents closes that.
Switches are launch-time environment variables only, never config.json,
because agents can edit files in the workspace:

    LOOM_NETWORK_GATE=off        disable the network gate
    LOOM_ALLOWED_NETWORKS=a/b,.. extra CIDRs allowed through the gate
    LOOM_AUTH                   legacy setting; cannot re-enable disabled pairing
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Iterable

from fastapi import Body, HTTPException, Request, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from exo_state import state_path
import loom_auth_store as browser_store

COOKIE_NAME = "__Host-loom_session"
LOCAL_COOKIE_NAME = "loom_session_local"
RELAY_HEADER = "x-loom-relay"
PAIR_CODE_TTL_SECONDS = 600
_MAX_OPEN_CODES = 20
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I

_TAILSCALE_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)

_secret_cache: str | None = None
_pair_codes: dict[str, float] = {}
_pair_attempts: dict[str, tuple[float, int]] = {}

# Browser pairing is disabled at the owner's request. Old launcher environment
# overrides must not silently restore the requirement after a restart.
PAIRING_ENABLED = False


# ── Settings ──

def network_gate_enabled() -> bool:
    return os.environ.get("LOOM_NETWORK_GATE", "").strip().lower() not in {"off", "0", "false"}


def auth_enabled() -> bool:
    return PAIRING_ENABLED and os.environ.get("LOOM_AUTH", "remote").strip().lower() not in {"off", "0", "false"}


def _local_exemption_enabled() -> bool:
    return os.environ.get("LOOM_AUTH", "remote").strip().lower() in {"", "remote"}


def _extra_networks() -> list:
    nets = []
    for raw in os.environ.get("LOOM_ALLOWED_NETWORKS", "").split(","):
        raw = raw.strip()
        if raw:
            try:
                nets.append(ipaddress.ip_network(raw, strict=False))
            except ValueError:
                pass
    return nets


# ── Network gate ──

def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_loopback


def network_allowed(host: str | None) -> bool:
    if not network_gate_enabled():
        return True
    if is_loopback(host):
        return True
    try:
        addr = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in net for net in (*_TAILSCALE_NETWORKS, *_extra_networks()))


def local_tailscale_addresses() -> list[str]:
    """This machine's Tailscale addresses, for showing pairing links to phones."""
    import socket
    found: list[str] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None)
    except OSError:
        return found
    for info in infos:
        host = info[4][0].split("%")[0]
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            continue
        if any(addr in net for net in _TAILSCALE_NETWORKS) and host not in found:
            found.append(host)
    return found


# ── Machine secret ──

def secret_file() -> Path:
    return state_path("auth", "loom-auth.key")


def machine_secret() -> str:
    """Load or create the per-machine secret shared by every Loom server.

    Lives in the external state root, never in a checkout or in os.environ
    (every harness launch copies os.environ into the agent process).
    """
    global _secret_cache
    if _secret_cache:
        return _secret_cache
    path = secret_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secrets.token_urlsafe(32))
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 32:
        raise RuntimeError(f"Loom auth secret at {path} is malformed; delete it to regenerate")
    _secret_cache = value
    return value


def relay_headers() -> dict[str, str]:
    """Headers a Loom server sends when relaying to another Loom server."""
    return {RELAY_HEADER: machine_secret()}


def _matches_secret(value: str | None) -> bool:
    if not value:
        return False
    return hmac.compare_digest(value.encode("utf-8"), machine_secret().encode("utf-8"))


# ── Pairing codes ──

def issue_pair_code(now: float | None = None) -> dict:
    now = time.time() if now is None else now
    for code, expiry in list(_pair_codes.items()):
        if expiry <= now:
            del _pair_codes[code]
    while len(_pair_codes) >= _MAX_OPEN_CODES:
        _pair_codes.pop(min(_pair_codes, key=_pair_codes.get))
    code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    _pair_codes[code] = now + PAIR_CODE_TTL_SECONDS
    return {"code": code, "expires_at": _pair_codes[code], "ttl_seconds": PAIR_CODE_TTL_SECONDS}


def redeem_pair_code(code: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    normalized = re.sub(r"[^A-Za-z0-9]", "", code or "").upper()
    expiry = _pair_codes.pop(normalized, None)
    return expiry is not None and expiry > now


def _allow_pair_attempt(host: str, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    for key, (started, _) in list(_pair_attempts.items()):
        if now - started >= 60:
            del _pair_attempts[key]
    if len(_pair_attempts) >= 2048 and host not in _pair_attempts:
        return False
    for key, limit in (("*", 100), (host, 10)):
        started, count = _pair_attempts.get(key, (now, 0))
        if count >= limit:
            return False
        _pair_attempts[key] = (started, count + 1)
    return True


# ── Request helpers ──

def _header(scope, name: str) -> str | None:
    target = name.lower().encode("latin-1")
    for key, value in scope.get("headers") or []:
        if key.lower() == target:
            return value.decode("latin-1")
    return None


def _cookie(scope, name: str) -> str | None:
    raw = _header(scope, "cookie")
    if not raw:
        return None
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        return None
    morsel = jar.get(name)
    return morsel.value if morsel else None


def _local_transport(scope) -> bool:
    client = scope.get("client")
    if not client or not is_loopback(client[0]):
        return False
    return not any(_header(scope, name) is not None for name in (
        "forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "cf-connecting-ip"))


def direct_local(scope) -> bool:
    """A direct loopback connection to a loopback hostname, with no proxy markers.

    Never exempt a gateway's Tailscale IP. These checks rely on preserving the
    actual transport peer (run Uvicorn with proxy_headers=False). A trusted proxy
    must preserve its public Host; a proxy that rewrites everything to localhost
    cannot be distinguished from a local client here and is unsupported.
    """
    if not _local_transport(scope):
        return False
    from urllib.parse import urlsplit
    try:
        host = urlsplit("//" + (_header(scope, "host") or "")).hostname
    except ValueError:
        return False
    return is_loopback(host)


def _browser_session(scope) -> dict | None:
    secure = scope.get("scheme") in {"https", "wss"}
    # Plain HTTP cookies are accepted only on a direct local connection.
    if not secure and not direct_local(scope):
        return None
    name = COOKIE_NAME if secure else LOCAL_COOKIE_NAME
    try:
        return browser_store.session_for_token(_cookie(scope, name))
    except (OSError, sqlite3.Error):
        return None  # unavailable session state must never bypass authentication


def credential(scope) -> str | None:
    """Return 'browser' or 'relay' when the request carries a valid credential."""
    if _browser_session(scope):
        return "browser"
    client = scope.get("client")
    if (client and _local_transport(scope) and not _header(scope, "origin")
            and _matches_secret(_header(scope, RELAY_HEADER))):
        return "relay"
    return None


def cookie_header(secure: bool, token: str, *, max_age: int = browser_store.SESSION_TTL_SECONDS) -> str:
    name = COOKIE_NAME if secure else LOCAL_COOKIE_NAME
    parts = [f"{name}={token}", "Path=/", "HttpOnly", "SameSite=Strict", f"Max-Age={max_age}"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


# ── Middleware ──

class LoomAccessMiddleware:
    """Pure ASGI middleware so WebSocket handshakes are gated too."""

    def __init__(self, app, *, public_paths: Iterable[str] = (), public_prefixes: Iterable[str] = (),
                 agent_routes: Iterable[str] = ()):
        self.app = app
        self.public_paths = frozenset(public_paths) | {
            "/pair", "/api/auth/pair", "/api/auth/status", "/api/auth/logout"}
        self.public_prefixes = tuple(public_prefixes)
        self.agent_routes = tuple(re.compile(pattern) for pattern in agent_routes)

    def _is_public(self, path: str) -> bool:
        return path in self.public_paths or path.startswith(self.public_prefixes)

    def _is_agent_route(self, path: str) -> bool:
        return any(pattern.fullmatch(path) for pattern in self.agent_routes)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        client = scope.get("client")
        host = client[0] if client else None
        if not network_allowed(host):
            return await self._reject(scope, receive, send, 403,
                                      "Loom only accepts connections from this computer or Tailscale.")
        path = scope.get("path") or "/"
        state = scope.setdefault("state", {})
        # Always evaluated: the approval relay needs it even when pairing is off.
        session = _browser_session(scope)
        state["loom_session"] = session
        state["loom_credential"] = "browser" if session else credential(scope)
        local = direct_local(scope)
        state["loom_local_access"] = local and _local_exemption_enabled()
        if (auth_enabled() and not local and state["loom_credential"] != "relay"
                and scope.get("scheme") not in {"https", "wss"}):
            return await self._reject(scope, receive, send, 403, "Remote browser access requires HTTPS.")
        # Browser requests cannot use cross-origin sockets or mutate authenticated
        # routes from another origin. Native agent/relay requests have no Origin.
        origin = _header(scope, "origin")
        if origin:
            scheme = "https" if scope.get("scheme") in {"https", "wss"} else "http"
            if origin != f"{scheme}://{_header(scope, 'host')}":
                return await self._reject(scope, receive, send, 403, "Same-origin access required.")
        # CORS preflights carry no cookies and perform no action.
        if (not auth_enabled() or state["loom_local_access"] or state["loom_credential"] or self._is_public(path)
                or scope.get("method") == "OPTIONS"
                or (local and not origin and self._is_agent_route(path))):
            if scope["type"] == "websocket" and session:
                return await self._session_websocket(scope, receive, send)
            return await self.app(scope, receive, send)
        return await self._reject(scope, receive, send, 401,
                                  "This browser is not paired with Loom. Open /pair to pair it.")

    async def _session_websocket(self, scope, receive, send):
        # Check on every application message; revocation/expiry affects already
        # open chat sockets as well as the next HTTP request or handshake.
        closed = False

        async def valid():
            nonlocal closed
            if closed:
                return False
            if _browser_session(scope):
                return True
            closed = True
            await send({"type": "websocket.close", "code": 4401})
            return False

        async def guarded_receive():
            message = await receive()
            if message["type"] != "websocket.disconnect" and not await valid():
                return {"type": "websocket.disconnect", "code": 4401}
            return message

        async def guarded_send(message):
            if message["type"] in {"websocket.accept", "websocket.send"} and not await valid():
                raise WebSocketDisconnect(4401)
            if not closed:
                await send(message)

        try:
            await self.app(scope, guarded_receive, guarded_send)
        except WebSocketDisconnect:
            if not closed:
                raise

    async def _reject(self, scope, receive, send, status: int, detail: str):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401 if status == 401 else 4403})
            return
        wants_page = (scope.get("method") == "GET"
                      and "text/html" in (_header(scope, "accept") or ""))
        if status == 401 and wants_page:
            await send({"type": "http.response.start", "status": 302,
                        "headers": [(b"location", b"/pair"), (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": b""})
            return
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})


# ── Routes ──

PAIR_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pair this browser - EXO</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#07080d;color:#dce3f0;font:16px/1.5 system-ui,sans-serif}
main{max-width:30rem;padding:2rem;border:1px solid #2a3350;border-radius:12px;background:#0d1120}
h1{font-size:1.3rem;margin:0 0 .75rem;color:#7fe7ff}
input{font:inherit;font-size:1.4rem;letter-spacing:.2em;text-transform:uppercase;width:100%;
box-sizing:border-box;padding:.6rem;border-radius:8px;border:1px solid #2a3350;background:#05070d;color:#fff}
button{margin-top:1rem;font:inherit;padding:.6rem 1.2rem;border-radius:8px;border:0;background:#7fe7ff;color:#05070d;cursor:pointer}
p.small{font-size:.9rem;color:#9aa6c0}#msg{min-height:1.5em;margin-top:.75rem}
</style></head><body><main>
<h1>Pair this browser with EXO</h1>
<p>Sign in with a pairing code or a saved recovery code. Pairing codes work once and expire after 10 minutes.</p>
<form id="f"><input id="code" autocomplete="one-time-code" placeholder="Pairing or recovery code" maxlength="64" required>
<input id="label" placeholder="Browser name (optional)" maxlength="120" style="font-size:1rem;letter-spacing:normal;margin-top:.5rem">
<button type="submit">Sign in</button></form>
<div id="msg" role="status"></div>
<p class="small">Getting a code: on this computer, double-click <b>pair_browser.bat</b> in the Loom folder.
For another device, open the Admin page on a paired browser and use <b>Devices &rarr; Pair a device</b>.</p>
<p class="small">This browser stays signed in for up to 90 days, with a 30-day inactivity limit.
Before traveling, save one-use recovery codes from this computer. They restore Loom access;
your network and portal login requirements still apply.</p>
</main><script>
const f=document.getElementById('f'),c=document.getElementById('code'),m=document.getElementById('msg');
const q=new URLSearchParams(location.hash.slice(1)).get('code')||new URLSearchParams(location.search).get('code');
if(q)history.replaceState(null,'','/pair');
async function pair(code){m.textContent='Signing in...';
 const r=await fetch('/api/auth/pair',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({code,label:document.getElementById('label').value})});
 if(r.ok){m.textContent='Paired. Opening EXO...';history.replaceState(null,'','/pair');location.href='/';}
 else{m.textContent=r.status===429?'Too many attempts. Wait one minute and try again.':
 'That code did not work. It may have expired or already been used.';}}
f.addEventListener('submit',e=>{e.preventDefault();pair(c.value);});
if(q){c.value=q;pair(q);}
</script></body></html>"""


def install(app, *, public_paths: Iterable[str] = (), public_prefixes: Iterable[str] = (),
            agent_routes: Iterable[str] = ()) -> None:
    """Register the pairing routes and the access middleware on a FastAPI app."""

    @app.get("/pair", include_in_schema=False)
    async def pair_page():
        return HTMLResponse(PAIR_PAGE, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    def management_access(request: Request) -> None:
        state = request.scope.get("state", {})
        if not state.get("loom_credential") and not state.get("loom_local_access"):
            raise HTTPException(401, "A paired browser or local access is required")

    @app.post("/api/auth/pair", include_in_schema=False)
    async def pair_redeem(request: Request, data: dict = Body(...)):
        host = request.client.host if request.client else "unknown"
        if not _allow_pair_attempt(host):
            return JSONResponse({"paired": False}, status_code=429, headers={"Retry-After": "60"})
        code = str(data.get("code") or "").strip()
        if len(code) > 64:
            return JSONResponse({"paired": False}, status_code=403)
        label = str(data.get("label") or request.headers.get("user-agent") or "Paired browser")[:120]
        issued = None
        if code.upper().startswith("REC-"):
            issued = browser_store.issue_session(label, recovery_code=code.upper())
        elif redeem_pair_code(code):
            issued = browser_store.issue_session(label)
        if not issued:
            return JSONResponse({"paired": False}, status_code=403)
        token, _ = issued
        response = JSONResponse({"paired": True})
        response.headers["set-cookie"] = cookie_header(request.url.scheme == "https", token)
        # Retire the old cookie, which contained the machine relay secret.
        response.delete_cookie("loom_auth", path="/", secure=request.url.scheme == "https")
        response.headers["cache-control"] = "no-store"
        return response

    @app.post("/api/auth/pair-code", include_in_schema=False)
    async def pair_code(request: Request):
        management_access(request)
        issued = issue_pair_code()
        issued["pair_url"] = str(request.base_url).rstrip("/") + "/pair"
        port = request.url.port
        issued["tailscale_pair_urls"] = [
            f"{request.url.scheme}://{'[' + host + ']' if ':' in host else host}"
            f"{':' + str(port) if port else ''}/pair"
            for host in local_tailscale_addresses()
        ]
        return JSONResponse(issued, headers={"Cache-Control": "no-store"})

    @app.get("/api/auth/status", include_in_schema=False)
    async def auth_status(request: Request):
        state = request.scope.get("state", {})
        return JSONResponse({"auth_enabled": auth_enabled(), "paired": bool(state.get("loom_credential")),
                             "local_access": bool(state.get("loom_local_access")),
                             "session_days": browser_store.SESSION_TTL_SECONDS // 86400,
                             "idle_days": browser_store.SESSION_IDLE_SECONDS // 86400},
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/auth/sessions", include_in_schema=False)
    async def sessions(request: Request):
        management_access(request)
        current = request.scope.get("state", {}).get("loom_session") or {}
        rows = [{**row, "current": row["id"] == current.get("id")} for row in browser_store.list_sessions()]
        return JSONResponse({"sessions": rows, "recovery_codes_remaining": browser_store.recovery_count()},
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/auth/sessions/{session_id}/revoke", include_in_schema=False)
    async def revoke(request: Request, session_id: str):
        management_access(request)
        return JSONResponse({"revoked": browser_store.revoke_session(session_id)},
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/auth/logout", include_in_schema=False)
    async def logout(request: Request):
        current = request.scope.get("state", {}).get("loom_session")
        if current:
            browser_store.revoke_session(current["id"])
        response = JSONResponse({"paired": False}, headers={"Cache-Control": "no-store"})
        response.headers["set-cookie"] = cookie_header(request.url.scheme == "https", "", max_age=0)
        response.delete_cookie("loom_auth", path="/", secure=request.url.scheme == "https")
        return response

    @app.post("/api/auth/recovery-codes", include_in_schema=False)
    async def recovery_codes(request: Request, data: dict = Body(...)):
        management_access(request)
        if not direct_local(request.scope):
            raise HTTPException(403, "Recovery codes can only be created directly on this computer")
        count = data.get("count", 5)
        if type(count) is not int or not 1 <= count <= 10:
            raise HTTPException(400, "Choose between one and ten recovery codes")
        return JSONResponse({"codes": browser_store.rotate_recovery_codes(count)},
                            headers={"Cache-Control": "no-store"})

    app.add_middleware(LoomAccessMiddleware, public_paths=public_paths,
                       public_prefixes=public_prefixes, agent_routes=agent_routes)
