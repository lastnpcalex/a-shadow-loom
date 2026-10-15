"""Password + Authy login for Admin, independent of Main's legacy pairing."""
from __future__ import annotations

import asyncio
import base64
import io
import ipaddress
import socket
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import admin_auth_store as store
import loom_auth

COOKIE = "__Host-exo_admin"
SETUP_COOKIE = "__Host-exo_admin_setup"
_hash_slots = asyncio.Semaphore(2)


def headers(scope):
    return {k.decode('latin1').lower(): v.decode('latin1') for k, v in scope.get("headers", [])}


def cookie(scope, name):
    try:
        jar = SimpleCookie()
        jar.load(headers(scope).get("cookie", ""))
        return jar[name].value if name in jar else ""
    except Exception:
        return ""


def network_allowed(scope):
    peer = (scope.get("client") or ("", 0))[0]
    h = headers(scope)
    # Proxy paths are not an Admin transport, even when they terminate locally.
    if any(k in h for k in ("forwarded", "x-forwarded-for", "x-forwarded-host",
                            "x-forwarded-proto", "cf-connecting-ip")):
        return False
    if loom_auth.is_loopback(peer):
        return loom_auth.direct_local(scope)
    try:
        addr = ipaddress.ip_address(peer)
        if addr.version == 6 and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        host = urlsplit('https://' + h.get('host', '')).hostname or ''
        try:
            host_ip = ipaddress.ip_address(host)
            host_ok = any(host_ip in net for net in loom_auth._TAILSCALE_NETWORKS)
        except ValueError:
            host_ok = host.lower() == socket.gethostname().lower() or host.lower().endswith('.ts.net')
        return host_ok and any(addr in net for net in loom_auth._TAILSCALE_NETWORKS)
    except ValueError:
        return False


def same_origin(scope):
    h = headers(scope)
    scheme = 'https' if scope.get('scheme') == 'wss' else scope.get('scheme', 'https')
    return h.get("origin") == scheme + "://" + h.get("host", "")


def response(data, status=200):
    return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})


def set_session(result, token):
    result.set_cookie(COOKIE, token, max_age=store.SESSION_DAYS*86400,
                      secure=True, httponly=True, samesite="strict", path="/")
    return result


class AdminGate:
    def __init__(self, app, instance_ports=()):
        self.app = app
        self.instance_ports = {int(p) for p in instance_ports}

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)

        async def reject(status, message):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4401 if status == 401 else 4403})
            else:
                await response({"error": message}, status)(scope, receive, send)

        if not network_allowed(scope):
            return await reject(403, "Admin requires a direct loopback or Tailscale connection")
        if scope.get("scheme") not in {"https", "wss"}:
            return await reject(403, "Admin requires HTTPS")
        h = headers(scope)
        for name in (b'host', b'origin', b'cookie', b'x-loom-relay'):
            if sum(k.lower() == name for k, _ in scope.get('headers', [])) > 1:
                return await reject(400, 'Duplicate security header')
        path = scope.get("path", "")
        origin_ok = same_origin(scope)
        if 'origin' in h and not origin_ok and not path.startswith('/api/admin-auth/'):
            try:
                origin = urlsplit(h['origin'])
                target = urlsplit('https://' + h.get('host', ''))
                origin_ok = (origin.scheme == 'https' and origin.hostname == target.hostname
                             and origin.port in self.instance_ports and not origin.username and not origin.password)
            except ValueError:
                origin_ok = False
        if "origin" in h and not origin_ok:
            return await reject(403, "Use the Admin page directly")
        # Main's existing local health probe needs only an alive/dead result,
        # not the dashboard's instance/process metadata.
        if (path == '/api/status' and scope.get('method') == 'GET'
                and loom_auth.direct_local(scope) and 'origin' not in h
                and not cookie(scope, COOKIE)):
            return await response({'up': True})(scope, receive, send)
        if scope.get("method") not in {None, "GET", "HEAD", "OPTIONS"} and "origin" not in h:
            # Local, non-browser service relays remain authenticated separately.
            if not (loom_auth.direct_local(scope) and loom_auth._matches_secret(h.get(loom_auth.RELAY_HEADER))):
                return await reject(403, "A same-origin browser request is required")
        configured = await asyncio.to_thread(store.configured)
        current = await asyncio.to_thread(store.session, cookie(scope, COOKIE))
        scope.setdefault("state", {})["admin_session"] = current
        public = path in {"/login", "/api/admin-auth/status", "/api/admin-auth/login"} or (
            path.startswith("/api/admin-auth/setup/") and loom_auth.direct_local(scope))
        relay_route = (scope.get('method'), path) in {
            ('GET', '/api/status')}
        relay = (relay_route and loom_auth.direct_local(scope) and "origin" not in h and
                 loom_auth._matches_secret(h.get(loom_auth.RELAY_HEADER)))
        preflight = scope.get('method') == 'OPTIONS' and origin_ok
        if current or public or relay or preflight or (not configured and loom_auth.direct_local(scope)):
            # Re-check persistent session validity on every incoming WS message.
            async def guarded_receive():
                event = await receive()
                if current and event["type"] == "websocket.receive" and not await asyncio.to_thread(
                        store.session, cookie(scope, COOKIE)):
                    return {"type": "websocket.disconnect", "code": 4401}
                return event
            async def protected_send(event):
                if event['type'] == 'http.response.start':
                    event = dict(event)
                    event['headers'] = [(k, v) for k, v in event.get('headers', []) if k.lower() != b'cache-control'] + [
                        (b'cache-control', b'no-store'), (b'x-frame-options', b'DENY'),
                        (b'x-content-type-options', b'nosniff')]
                await send(event)
            return await self.app(scope, guarded_receive, protected_send)
        if scope["type"] == "http" and scope.get("method") == "GET" and "text/html" in h.get("accept", ""):
            return await RedirectResponse("/login", status_code=303)(scope, receive, send)
        return await reject(401, "Sign in to Admin with your password and Authy code")


async def payload(request):
    if not same_origin(request.scope):
        raise HTTPException(403, "Open Admin directly")
    import json
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 8192:
            raise HTTPException(413, "Request too large")
        chunks.append(chunk)
    try:
        data = json.loads(b''.join(chunks))
        password, code = data.get("password", ""), data.get("code", "")
        if not isinstance(password, str) or not isinstance(code, str) or len(password.encode()) > 1024 or len(code) > 100:
            raise ValueError()
        return password, code
    except (ValueError, AttributeError):
        raise HTTPException(400, "Invalid request")


def install(app, instance_ports=()):
    app.add_middleware(AdminGate, instance_ports=instance_ports)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        page = Path(__file__).with_name("static").joinpath("admin", "login.html").read_text(encoding="utf-8")
        return HTMLResponse(page, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})

    @app.get("/api/admin-auth/status")
    async def status(request: Request):
        current = request.scope["state"].get("admin_session")
        return response({"configured": await asyncio.to_thread(store.configured), "authenticated": bool(current),
                         "local": loom_auth.direct_local(request.scope), "session_days": store.SESSION_DAYS,
                         "idle_days": store.IDLE_DAYS})

    @app.post("/api/admin-auth/login")
    async def login(request: Request):
        password, code = await payload(request)
        # Persisted global rate limit prevents rotating tailnet peers/restarts
        # from bypassing the bound. Existing sessions stay usable if exceeded.
        if not await asyncio.to_thread(store.allow_attempt, "login"):
            return response({"error": "Too many attempts. Wait five minutes."}, 429)
        async with _hash_slots:
            token = await asyncio.to_thread(store.login, password, code, headers(request.scope).get("user-agent", "Browser"))
        if not token:
            return response({"error": "Password or code was not accepted. Used Authy codes cannot be reused."}, 401)
        return set_session(response({"ok": True}), token)

    def require_local(request):
        if not loom_auth.direct_local(request.scope):
            raise HTTPException(403, "Initial setup is only available on the host through localhost")

    @app.post("/api/admin-auth/setup/start")
    async def setup_start(request: Request):
        require_local(request)
        password, _ = await payload(request)
        if len(password) < 12:
            return response({"error": "Use a unique password of at least 12 characters."}, 400)
        if not await asyncio.to_thread(store.allow_attempt, "setup"):
            return response({"error": "Wait five minutes before retrying setup."}, 429)
        # Generate locally; never send the MFA seed to an external QR service.
        import qrcode
        async with _hash_slots:
            try:
                token, seed = await asyncio.to_thread(store.start_setup, password)
            except ValueError as e:
                return response({"error": str(e)}, 409)
        uri = "otpauth://totp/EXO%20Admin:Owner?secret=" + seed + "&issuer=EXO%20Admin&algorithm=SHA1&digits=6&period=30"
        buf = io.BytesIO()
        qrcode.make(uri).save(buf, format="PNG")
        result = response({"seed": seed, "qr": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()})
        result.set_cookie(SETUP_COOKIE, token, max_age=600, secure=True, httponly=True, samesite="strict", path="/")
        return result

    @app.post("/api/admin-auth/setup/confirm")
    async def setup_confirm(request: Request):
        require_local(request)
        _, code = await payload(request)
        if not await asyncio.to_thread(store.allow_attempt, "setup-confirm"):
            return response({"error": "Wait five minutes before retrying."}, 429)
        try:
            codes = await asyncio.to_thread(store.confirm_setup, cookie(request.scope, SETUP_COOKIE), code)
            return response({"codes": codes})
        except ValueError as e:
            return response({"error": str(e)}, 400)

    @app.post("/api/admin-auth/setup/activate")
    async def setup_activate(request: Request):
        require_local(request)
        await payload(request)
        try:
            token = await asyncio.to_thread(store.activate_setup, cookie(request.scope, SETUP_COOKIE),
                                           headers(request.scope).get("user-agent", "Browser"))
        except ValueError as e:
            return response({"error": str(e)}, 400)
        result = set_session(response({"ok": True}), token)
        result.delete_cookie(SETUP_COOKIE, secure=True, httponly=True, samesite="strict", path="/")
        return result

    def require_session(request):
        current = request.scope["state"].get("admin_session")
        if not current:
            raise HTTPException(401, "Sign in to Admin")
        return current

    @app.get("/api/admin-auth/sessions")
    async def sessions(request: Request):
        current = require_session(request)
        rows, remaining = await asyncio.to_thread(store.sessions)
        return response({"sessions": [{**r, "current": r["id"] == current["id"]} for r in rows],
                         "recovery_codes_remaining": remaining})

    @app.post("/api/admin-auth/sessions/{ident}/revoke")
    async def revoke(ident: str, request: Request):
        require_session(request)
        await payload(request)
        await asyncio.to_thread(store.revoke, ident)
        return response({"ok": True})

    @app.post("/api/admin-auth/logout")
    async def logout(request: Request):
        current = require_session(request)
        await payload(request)
        await asyncio.to_thread(store.revoke, current["id"])
        result = response({"ok": True})
        result.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict", path="/")
        return result

    @app.post('/api/admin-auth/recovery-codes')
    async def recovery_codes(request: Request):
        require_session(request)
        password, code = await payload(request)
        if not await asyncio.to_thread(store.allow_attempt, 'login'):
            return response({'error': 'Wait five minutes before retrying.'}, 429)
        async with _hash_slots:
            codes = await asyncio.to_thread(store.rotate_recovery, password, code)
        if not codes:
            return response({'error': 'Enter your password and a fresh Authy code.'}, 401)
        return response({'codes': codes})
