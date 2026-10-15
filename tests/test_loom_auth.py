"""Network gate, paired-browser requirement, relay secret and approval channel."""

import httpx
import pytest
from fastapi import FastAPI, WebSocket

import loom_auth

DEFAULT_PAIRING_ENABLED = loom_auth.PAIRING_ENABLED


@pytest.fixture(autouse=True)
def auth_on(monkeypatch, tmp_path):
    # Keep regression coverage for the retained legacy pairing machinery.
    monkeypatch.setattr(loom_auth, "PAIRING_ENABLED", True)
    monkeypatch.setenv("LOOM_AUTH", "on")
    monkeypatch.setenv("LOOM_NETWORK_GATE", "on")
    monkeypatch.delenv("LOOM_ALLOWED_NETWORKS", raising=False)
    monkeypatch.setenv("EXO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(loom_auth, "_secret_cache", None)
    monkeypatch.setattr(loom_auth, "_pair_codes", {})
    monkeypatch.setattr(loom_auth, "_pair_attempts", {})


def _app():
    app = FastAPI()

    @app.get("/")
    async def index():
        return {"page": "index"}

    @app.post("/api/secret-action")
    async def action():
        return {"done": True}

    @app.post("/api/cc-permission")
    async def agent_channel():
        return {"queued": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"ok": True})
        await websocket.close()

    loom_auth.install(app, public_prefixes=("/static/",), agent_routes=(r"/api/cc-permission",))
    return app


def _client(app, host="127.0.0.1"):
    transport = httpx.ASGITransport(app=app, client=(host, 5555))
    return httpx.AsyncClient(transport=transport, base_url="https://localhost")


@pytest.mark.parametrize("setting", ["on", "remote", "off"])
async def test_pairing_disabled_even_with_old_launcher_override(monkeypatch, setting):
    assert DEFAULT_PAIRING_ENABLED is False
    monkeypatch.setattr(loom_auth, "PAIRING_ENABLED", DEFAULT_PAIRING_ENABLED)
    monkeypatch.setenv("LOOM_AUTH", setting)
    assert not loom_auth.auth_enabled()
    # Separate Main and Admin app objects need neither each other nor a session.
    for app in (_app(), _app()):
        async with _client(app, host="100.101.102.103") as client:
            response = await client.get("/", headers={"accept": "text/html"})
            assert response.status_code == 200
            assert (await client.post("/api/secret-action")).status_code == 200
        async with _client(app, host="192.168.1.20") as client:
            assert (await client.get("/")).status_code == 403


@pytest.mark.parametrize("host,allowed", [
    ("127.0.0.1", True), ("::1", True), ("100.101.102.103", True),
    ("fd7a:115c:a1e0::5", True), ("192.168.1.20", False), ("8.8.8.8", False), ("testclient", False),
])
def test_network_gate(host, allowed):
    assert loom_auth.network_allowed(host) is allowed


def test_extra_networks_and_gate_off(monkeypatch):
    monkeypatch.setenv("LOOM_ALLOWED_NETWORKS", "192.168.1.0/24")
    assert loom_auth.network_allowed("192.168.1.20")
    monkeypatch.setenv("LOOM_NETWORK_GATE", "off")
    assert loom_auth.network_allowed("8.8.8.8")


async def test_lan_client_refused_before_any_route():
    async with _client(_app(), host="192.168.1.20") as client:
        response = await client.post("/api/cc-permission")
    assert response.status_code == 403


async def test_unpaired_browser_refused_and_redirected_to_pairing():
    async with _client(_app()) as client:
        action = await client.post("/api/secret-action")
        page = await client.get("/", headers={"accept": "text/html"})
        pair = await client.get("/pair")
    assert action.status_code == 401
    assert page.status_code == 302 and page.headers["location"] == "/pair"
    assert pair.status_code == 200 and "Pair this browser" in pair.text


async def test_agent_channel_allowed_only_from_loopback():
    async with _client(_app()) as client:
        assert (await client.post("/api/cc-permission")).status_code == 200
    async with _client(_app(), host="100.101.102.103") as client:
        assert (await client.post("/api/cc-permission")).status_code == 401


async def test_old_constant_admin_header_no_longer_authenticates():
    async with _client(_app()) as client:
        response = await client.post("/api/secret-action", headers={"x-loom-admin": "1"})
    assert response.status_code == 401


async def test_relay_secret_works_only_from_loopback():
    headers = loom_auth.relay_headers()
    async with _client(_app()) as client:
        assert (await client.post("/api/secret-action", headers=headers)).status_code == 200
    async with _client(_app(), host="100.101.102.103") as client:
        assert (await client.post("/api/secret-action", headers=headers)).status_code == 401


async def test_pairing_code_issues_cookie_once():
    app = _app()
    async with _client(app) as client:
        code = (await client.post("/api/auth/pair-code", headers=loom_auth.relay_headers())).json()["code"]
    async with _client(app, host="100.101.102.103") as phone:
        assert (await phone.post("/api/secret-action")).status_code == 401
        paired = await phone.post("/api/auth/pair", json={"code": code.lower()})
        assert paired.status_code == 200
        cookie = paired.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" in cookie
        assert (await phone.post("/api/secret-action")).status_code == 200
        assert (await phone.get("/api/auth/status")).json()["paired"] is True
    async with _client(app, host="100.101.102.104") as other:
        assert (await other.post("/api/auth/pair", json={"code": code})).status_code == 403


def test_pair_codes_expire():
    issued = loom_auth.issue_pair_code(now=1000.0)
    assert not loom_auth.redeem_pair_code(issued["code"], now=1000.0 + loom_auth.PAIR_CODE_TTL_SECONDS + 1)


def test_websocket_requires_pairing():
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    app = _app()
    with TestClient(app, client=("127.0.0.1", 5555), base_url="https://localhost") as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("wss://localhost/ws"):
                pass
        token, _ = loom_auth.browser_store.issue_session("Test browser")
        client.cookies.set(loom_auth.COOKIE_NAME, token)
        with client.websocket_connect("wss://localhost/ws") as ws:
            assert ws.receive_json() == {"ok": True}


def test_pairing_defaults_on_for_remote_access(monkeypatch):
    monkeypatch.delenv("LOOM_AUTH")
    assert loom_auth.auth_enabled()
    monkeypatch.setenv("LOOM_AUTH", "off")
    assert not loom_auth.auth_enabled()


async def test_pairing_off_admits_tailscale_but_not_lan(monkeypatch):
    monkeypatch.setenv("LOOM_AUTH", "off")
    async with _client(_app(), host="100.101.102.103") as phone:
        assert (await phone.post("/api/secret-action")).status_code == 200
    async with _client(_app(), host="192.168.1.20") as lan:
        assert (await lan.post("/api/secret-action")).status_code == 403


@pytest.mark.parametrize("pairing", ["on", None])
async def test_main_admin_relay_always_requires_secret(monkeypatch, pairing):
    """Turning pairing off must not reopen the constant-header approval path."""
    import server
    if pairing is None:
        monkeypatch.delenv("LOOM_AUTH")
    transport = httpx.ASGITransport(app=server.app, client=("127.0.0.1", 5555))
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        forged = await client.get("/api/admin/supervision", headers={"x-loom-admin": "1"})
        assert forged.status_code in {401, 403}
        relayed = await client.get("/api/admin/supervision", headers=loom_auth.relay_headers())
        assert relayed.status_code == 200


def test_secret_lives_outside_checkout(tmp_path):
    assert loom_auth.secret_file().is_relative_to(tmp_path / "state")
    assert loom_auth.machine_secret() == loom_auth.machine_secret()


async def test_default_local_access_and_remote_login(monkeypatch):
    monkeypatch.delenv("LOOM_AUTH")
    app = _app()
    async with _client(app) as local:
        assert (await local.post("/api/secret-action")).status_code == 200
        assert (await local.get("/api/auth/status")).json()["local_access"] is True
    async with _client(app, host="100.101.102.103") as remote:
        assert (await remote.post("/api/secret-action")).status_code == 401
        assert (await remote.get("/api/auth/status")).json()["local_access"] is False
        # A forged Host or client-IP header cannot turn a remote peer into local access.
        assert (await remote.post("/api/secret-action", headers={
            "host": "localhost", "x-forwarded-for": "127.0.0.1"})).status_code == 401


@pytest.mark.parametrize("headers", [
    {"host": "portal.example.invalid"},
    {"forwarded": "for=100.101.102.103"},
    {"x-forwarded-for": "100.101.102.103"},
    {"x-forwarded-host": "portal.example.invalid"},
    {"x-forwarded-proto": "https"},
    {"cf-connecting-ip": "8.8.8.8"},
])
async def test_local_proxy_gets_no_local_or_agent_exemption(monkeypatch, headers):
    monkeypatch.delenv("LOOM_AUTH")
    async with _client(_app()) as proxy:
        assert (await proxy.post("/api/secret-action", headers=headers)).status_code == 401
        assert (await proxy.post("/api/cc-permission", headers=headers)).status_code == 401


async def test_pairing_creates_distinct_revocable_sessions_and_rejects_master_cookie():
    app = _app()
    async with _client(app) as local:
        codes = [(await local.post("/api/auth/pair-code", headers=loom_auth.relay_headers())).json()["code"]
                 for _ in range(2)]
    async with _client(app, host="100.101.102.103") as one, _client(app, host="100.101.102.104") as two:
        for client, code, label in ((one, codes[0], "Phone"), (two, codes[1], "Laptop")):
            assert (await client.post("/api/auth/pair", json={"code": code, "label": label})).status_code == 200
        first_token = one.cookies.get(loom_auth.COOKIE_NAME)
        second_token = two.cookies.get(loom_auth.COOKIE_NAME)
        assert first_token != second_token and first_token != loom_auth.machine_secret()
        listed = (await one.get("/api/auth/sessions")).json()["sessions"]
        current = next(row for row in listed if row["current"])
        assert current["label"] == "Phone"
        assert (await two.post(f"/api/auth/sessions/{current['id']}/revoke")).json()["revoked"]
        assert (await one.post("/api/secret-action")).status_code == 401
        assert (await two.post("/api/secret-action")).status_code == 200
        # Browser credentials no longer contain or accept the machine relay secret.
        one.cookies.clear()
        one.cookies.set("loom_auth", loom_auth.machine_secret())
        one.cookies.set(loom_auth.COOKIE_NAME, loom_auth.machine_secret())
        assert (await one.post("/api/secret-action")).status_code == 401
        assert (await two.post("/api/auth/logout")).status_code == 200
        assert (await two.post("/api/secret-action")).status_code == 401


async def test_recovery_provisioning_local_only_and_codes_one_use(monkeypatch, tmp_path):
    monkeypatch.delenv("LOOM_AUTH")
    app = _app()
    async with _client(app) as local:
        response = await local.post("/api/auth/recovery-codes", json={})
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        codes = response.json()["codes"]
        assert len(codes) == 5
    async with _client(app, host="100.101.102.103") as phone:
        recovered = await phone.post("/api/auth/pair", json={"code": codes[0].lower(), "label": "Recovery phone"})
        assert recovered.status_code == 200
        assert (await phone.post("/api/secret-action")).status_code == 200
        assert (await phone.post("/api/auth/recovery-codes", json={})).status_code == 403
        assert (await phone.get("/api/auth/sessions")).json()["recovery_codes_remaining"] == 4
        token = phone.cookies.get(loom_auth.COOKIE_NAME)
    async with _client(app, host="100.101.102.104") as other:
        assert (await other.post("/api/auth/pair", json={"code": codes[0]})).status_code == 403
    stored = (tmp_path / "state" / "auth" / "browser-sessions.sqlite3").read_bytes()
    assert token.encode() not in stored and all(code.encode() not in stored for code in codes)
    # Recovery is not an exemption from the network gate.
    async with _client(app, host="8.8.8.8") as internet:
        assert (await internet.post("/api/auth/pair", json={"code": codes[1]})).status_code == 403


def test_recovery_rotation_and_concurrent_redemption():
    from concurrent.futures import ThreadPoolExecutor
    store = loom_auth.browser_store
    old = store.rotate_recovery_codes()[0]
    new = store.rotate_recovery_codes()[0]
    assert store.issue_session("old", recovery_code=old) is None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: store.issue_session("new", recovery_code=new), range(2)))
    assert sum(result is not None for result in results) == 1


def test_session_persists_and_absolute_and_idle_expiry_are_enforced():
    store = loom_auth.browser_store
    token, session = store.issue_session("Phone", now=1000)
    assert store.session_for_token(token, now=1001)["id"] == session["id"]
    assert store.session_for_token(token, now=1000 + store.SESSION_IDLE_SECONDS) is None
    token, session = store.issue_session("Laptop", now=1000)
    for day in (20, 40, 60, 80):
        assert store.session_for_token(token, now=1000 + day * 86400)
    assert store.session_for_token(token, now=session["expires_at"]) is None


async def test_pairing_rate_limit_and_cross_origin_requests():
    async with _client(_app(), host="100.101.102.103") as client:
        for _ in range(10):
            assert (await client.post("/api/auth/pair", json={"code": "WRONG"})).status_code == 403
        limited = await client.post("/api/auth/pair", json={"code": "WRONG"})
        assert limited.status_code == 429 and limited.headers["retry-after"] == "60"
    async with _client(_app()) as local:
        response = await local.post("/api/auth/recovery-codes", json={}, headers={"origin": "https://evil.example"})
        assert response.status_code == 403
        assert (await local.post("/api/auth/pair-code", headers={
            **loom_auth.relay_headers(), "origin": "https://localhost"})).status_code == 401


async def test_remote_http_cannot_pair_even_with_recovery_code():
    code = loom_auth.browser_store.rotate_recovery_codes()[0]
    transport = httpx.ASGITransport(app=_app(), client=("100.101.102.103", 5555))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as remote:
        assert (await remote.post("/api/auth/pair", json={"code": code})).status_code == 403


def test_revocation_closes_existing_websocket():
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    app = _app()

    @app.websocket("/echo")
    async def echo(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                await ws.send_text(await ws.receive_text())
        except WebSocketDisconnect:
            pass

    token, session = loom_auth.browser_store.issue_session("Phone")
    with TestClient(app, client=("100.101.102.103", 5555), base_url="https://localhost") as client:
        client.cookies.set(loom_auth.COOKIE_NAME, token)
        with client.websocket_connect("wss://localhost/echo") as ws:
            ws.send_text("before")
            assert ws.receive_text() == "before"
            loom_auth.browser_store.revoke_session(session["id"])
            ws.send_text("after")
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
            assert exc.value.code == 4401


async def test_real_admin_does_not_accept_main_pairing_sessions(monkeypatch):
    import server
    import admin_server
    monkeypatch.delenv("LOOM_AUTH")
    token, _ = loom_auth.browser_store.issue_session("Main-only browser")
    async with _client(server.app, host="100.101.102.103") as remote:
        assert (await remote.get('/api/generations')).status_code == 401
        remote.cookies.set(loom_auth.COOKIE_NAME, token)
        assert (await remote.get('/api/generations')).status_code == 200
    transport = httpx.ASGITransport(app=admin_server.app, client=('100.101.102.103', 5555))
    async with httpx.AsyncClient(transport=transport, base_url='https://100.100.100.100:3002') as remote:
        assert (await remote.get('/')).status_code == 401
        remote.cookies.set(loom_auth.COOKIE_NAME, token)
        assert (await remote.get('/')).status_code == 401


@pytest.mark.parametrize("local", [True, False])
async def test_devices_browser_revocation_recovery_and_logout(monkeypatch, local):
    """Actual Devices markup/JS and auth endpoints; no live host or credentials."""
    from pathlib import Path
    from fastapi.responses import HTMLResponse, Response
    from playwright.async_api import async_playwright, expect
    monkeypatch.delenv("LOOM_AUTH")
    root = Path(__file__).resolve().parents[1] / "static" / "admin"
    # Retained legacy pairing machinery uses a standalone fixture. The current
    # Admin card now exercises independent credentials in test_admin_auth.py.
    card = '''<h2>Legacy sessions</h2><button id="btn-pair-device">Pair</button>
        <div id="pair-device-output"></div><button id="btn-sessions-refresh">Refresh</button>
        <button id="btn-browser-logout">Sign out</button><button id="btn-recovery-codes">Recovery</button>
        <div id="browser-sessions"></div><div id="recovery-code-output"></div></div>'''
    app = FastAPI()

    @app.get("/")
    async def index():
        return HTMLResponse('<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<div class="card" id="devices-card">' + card + '<script src="/assets/admin-devices.js"></script>')

    @app.get("/assets/admin-devices.js")
    async def script():
        return Response((root / "admin-devices.js").read_text(encoding="utf-8"),
                        media_type="application/javascript")

    loom_auth.install(app, public_prefixes=("/assets/",))
    token, _ = loom_auth.browser_store.issue_session("Phone <not markup>")
    loom_auth.browser_store.issue_session("Spare laptop")
    base = "https://localhost" if local else "https://portal.fixture"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1" if local else "100.101.102.103", 5555))
    async with httpx.AsyncClient(transport=transport, base_url=base) as backend:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(viewport={"width": 390, "height": 850})
                await context.add_cookies([{"name": loom_auth.COOKIE_NAME, "value": token,
                    "url": base, "secure": True, "httpOnly": True, "sameSite": "Strict"}])
                page = await context.new_page()
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))

                async def route_handler(route):
                    request = route.request
                    response = await backend.request(request.method, request.url,
                        headers=await request.all_headers(), content=request.post_data_buffer)
                    await route.fulfill(status=response.status_code, body=response.content,
                        headers={key: value for key, value in response.headers.items() if key != "set-cookie"})

                await page.route("**/*", route_handler)
                await page.goto(base)
                await expect(page.locator("#devices-card")).to_be_visible()
                await expect(page.locator("#browser-sessions")).to_contain_text("Phone <not markup> (this browser)")
                spare = page.get_by_text("Spare laptop", exact=False).locator("..")
                await spare.get_by_role("button", name="Revoke", exact=True).click()
                await expect(page.locator("#browser-sessions")).not_to_contain_text("Spare laptop")
                recovery = page.locator("#btn-recovery-codes")
                if local:
                    await expect(recovery).to_be_visible()
                    await recovery.click()
                    await expect(page.locator("#recovery-code-output pre")).to_be_visible()
                    codes = (await page.locator("#recovery-code-output pre").inner_text()).splitlines()
                    assert len(codes) == 5 and all(code.startswith("REC-") for code in codes)
                    await expect(page.locator("#browser-sessions")).to_contain_text("5 one-use recovery codes remain")
                    await page.get_by_role("button", name="Hide saved codes").click()
                    await expect(page.locator("#recovery-code-output")).to_be_empty()
                else:
                    await expect(recovery).to_be_hidden()
                async with page.expect_response(lambda response: response.url.endswith("/api/auth/logout")) as logged_out:
                    await page.locator("#btn-browser-logout").click()
                assert (await logged_out.value).status == 200
                await page.wait_for_url(base + ("/" if local else "/pair"))
                assert loom_auth.browser_store.session_for_token(token) is None
                assert not errors, errors
            finally:
                await browser.close()
