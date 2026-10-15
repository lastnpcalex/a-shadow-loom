"""Isolated acceptance of Admin login; never touches the live host/store."""
import base64
import concurrent.futures
import time

import httpx
import pytest
from fastapi import FastAPI

import admin_auth
import admin_auth_store as store
import loom_auth

PASSWORD = 'test-only-owner-password'


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('EXO_STATE_DIR', str(tmp_path / 'private'))
    monkeypatch.setattr(loom_auth, '_secret_cache', None)


def app():
    a = FastAPI()
    @a.get('/')
    async def root():
        return {'private': True}
    @a.post('/api/action')
    async def action():
        return {'ok': True}
    admin_auth.install(a, instance_ports=('3000',))
    return a


def client(a, peer='127.0.0.1', url='https://localhost:3002'):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=a, client=(peer, 1234)),
                             base_url=url, headers={'origin': url})


def enroll(now=None):
    now = time.time() if now is None else now
    token, seed = store.start_setup(PASSWORD, now)
    raw = base64.b32decode(seed)
    code = store.totp(raw, int(now//30))
    codes = store.confirm_setup(token, code, now)
    session = store.activate_setup(token, 'local browser', now)
    return raw, codes, session


def test_rfc6238_vector():
    # RFC 6238 SHA1 8-digit vector ends in these six digits.
    assert store.totp(b'12345678901234567890', 59//30) == '287082'


def test_setup_atomic_and_cannot_overwrite_owner():
    token, seed = store.start_setup(PASSWORD, 1000)
    assert not store.configured()
    with pytest.raises(ValueError):
        store.activate_setup(token, 'browser', 1001)
    codes = store.confirm_setup(token, store.totp(base64.b32decode(seed), 33), 1001)
    assert not store.configured()
    current = store.activate_setup(token, 'browser', 1001)
    assert store.configured() and store.session(current, 1002)
    with pytest.raises(ValueError):
        store.start_setup('another-test-password', 1002)
    with pytest.raises(ValueError):
        store.activate_setup(token, 'browser', 1002)
    assert store.login('wrong-password', codes[0], 'remote', 1002) is None
    assert store.login(PASSWORD, codes[0], 'remote', 1002)
    assert store.login(PASSWORD, codes[0], 'remote', 1002) is None


def test_expired_setup_and_replayed_otp():
    token, seed = store.start_setup(PASSWORD, 1000)
    with pytest.raises(ValueError):
        store.confirm_setup(token, store.totp(base64.b32decode(seed), 54), 1601)
    raw, codes, _ = enroll(2000)
    assert store.login(PASSWORD, store.totp(raw, 66), 'remote', 2001) is None
    assert store.login(PASSWORD, store.totp(raw, 67), 'remote', 2010)
    assert store.login(PASSWORD, store.totp(raw, 67), 'remote', 2011) is None


def test_concurrent_recovery_only_once():
    _, codes, _ = enroll()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        result = list(executor.map(lambda _: store.login(PASSWORD, codes[0], 'remote'), range(2)))
    assert sum(bool(r) for r in result) == 1


def test_sessions_persist_revoke_and_expire():
    _, _, token = enroll(2000)
    current = store.session(token, 2001)
    assert current and store.session(token, 2002)
    assert not store.session(token, 2000+store.IDLE_DAYS*86400+1)
    store.revoke(current['id'])
    assert not store.session(token, 2002)


def test_rate_limit_persists_and_expires():
    assert all(store.allow_attempt('login', 1000) for _ in range(10))
    assert not store.allow_attempt('login', 1001)
    assert store.allow_attempt('login', 1301)


def test_recovery_rotation_requires_fresh_password_and_otp():
    raw, codes, _ = enroll()
    fresh = store.totp(raw, int(time.time()//30)+1)
    assert store.rotate_recovery('wrong-password', fresh) is None
    assert store.rotate_recovery(PASSWORD, 'ADM-not-an-otp') is None
    replacement = store.rotate_recovery(PASSWORD, fresh)
    assert len(replacement) == 8
    assert store.login(PASSWORD, codes[0], 'browser') is None
    assert store.login(PASSWORD, replacement[0], 'browser')
    assert store.rotate_recovery(PASSWORD, fresh) is None


async def test_health_and_service_relay_cannot_authorize_admin_controls():
    enroll()
    transport = httpx.ASGITransport(app=app(), client=('127.0.0.1', 1234))
    async with httpx.AsyncClient(transport=transport, base_url='https://localhost:3002') as c:
        assert (await c.get('/api/status')).json() == {'up': True}
        assert (await c.get('/')).status_code == 401
        assert (await c.post('/api/action', headers=loom_auth.relay_headers())).status_code == 401


async def test_real_admin_dashboard_and_assets_accept_only_admin_session():
    import admin_server
    _, _, token = enroll()
    async with client(admin_server.app, '100.101.102.103', 'https://100.100.100.100:3002') as c:
        assert (await c.get('/assets/admin-security.js')).status_code == 401
        c.cookies.set(admin_auth.COOKIE, token)
        page = await c.get('/')
        assert page.status_code == 200 and 'Admin access' in page.text
        assert page.headers['cache-control'] == 'no-store'
        assert (await c.get('/assets/admin-security.js')).status_code == 200
        assert (await c.get('/api/admin-auth/sessions')).status_code == 200


async def test_full_browser_enrollment_remote_login_logout():
    a = app()
    async with client(a) as local:
        assert (await local.get('/')).status_code == 200
        start = await local.post('/api/admin-auth/setup/start', json={'password': PASSWORD})
        assert start.status_code == 200
        assert start.json()['qr'].startswith('data:image/png;base64,')
        raw = base64.b32decode(start.json()['seed'])
        confirm = await local.post('/api/admin-auth/setup/confirm', json={'code': store.totp(raw, int(time.time()//30))})
        assert confirm.status_code == 200
        codes = confirm.json()['codes']
        assert not store.configured()
        assert (await local.post('/api/admin-auth/setup/activate', json={})).status_code == 200
        assert (await local.get('/')).status_code == 200
        assert '__Host-exo_admin' in local.cookies
    async with client(a, '100.101.102.103', 'https://100.100.100.100:3002') as remote:
        assert (await remote.get('/')).status_code == 401
        page = await remote.get('/', headers={'accept': 'text/html'})
        assert page.status_code == 303 and page.headers['location'] == '/login'
        assert (await remote.get('/login')).status_code == 200
        login = await remote.post('/api/admin-auth/login', json={'password': PASSWORD, 'code': codes[0]})
        assert login.status_code == 200 and 'httponly' in login.headers['set-cookie'].lower()
        assert (await remote.get('/')).status_code == 200
        sessions = (await remote.get('/api/admin-auth/sessions')).json()
        assert sessions['recovery_codes_remaining'] == 7
        assert (await remote.post('/api/action', headers={'origin': 'https://100.100.100.100:3000'})).status_code == 200
        assert (await remote.post('/api/admin-auth/logout', json={})).status_code == 200
        assert (await remote.get('/')).status_code == 401


@pytest.mark.parametrize('peer,url,extra', [
    ('192.168.1.2', 'https://100.100.100.100:3002', {}),
    ('8.8.8.8', 'https://100.100.100.100:3002', {}),
    ('127.0.0.1', 'https://portal.example.invalid', {}),
    ('100.101.102.103', 'https://portal.example.invalid', {}),
    ('127.0.0.1', 'https://localhost:3002', {'x-forwarded-for': '100.101.102.103'}),
    ('100.101.102.103', 'https://100.100.100.100:3002', {'cf-connecting-ip': '100.101.102.103'}),
])
async def test_network_cannot_be_disabled_or_spoofed(monkeypatch, peer, url, extra):
    monkeypatch.setenv('LOOM_NETWORK_GATE', 'off')
    monkeypatch.setenv('LOOM_ALLOWED_NETWORKS', '0.0.0.0/0')
    async with client(app(), peer, url) as c:
        assert (await c.get('/login', headers=extra)).status_code == 403


async def test_main_pairing_cookie_and_local_browser_do_not_bypass_admin():
    enroll()
    async with client(app()) as c:
        assert (await c.get('/')).status_code == 401
        assert (await c.get('/', headers={'cookie': '__Host-loom_session=' + 'A'*43})).status_code == 401
        assert (await c.post('/api/action', headers={'origin': 'https://evil.example'})).status_code == 403
        assert (await c.post('/api/admin-auth/login', headers={'origin': 'https://localhost:3000'}, json={})).status_code == 403


async def test_remote_cannot_enroll_or_run_actions_before_setup():
    async with client(app(), '100.101.102.103', 'https://100.100.100.100:3002') as c:
        assert (await c.post('/api/admin-auth/setup/start', json={'password': PASSWORD})).status_code == 401
        assert (await c.post('/api/action')).status_code == 401
        assert not store.configured()


async def test_real_browser_setup_and_recovery_login():
    """Real login JS against isolated ASGI state, at phone viewport width."""
    from http.cookies import SimpleCookie
    from playwright.async_api import async_playwright, expect
    a = app()
    async with client(a) as backend, async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={'width': 390, 'height': 850})
            page = await context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            async def route_handler(route):
                req = route.request
                res = await backend.request(req.method, req.url,
                    headers=await req.all_headers(), content=req.post_data_buffer)
                for value in res.headers.get_list('set-cookie'):
                    jar = SimpleCookie(); jar.load(value)
                    for name, morsel in jar.items():
                        if morsel['max-age'] == '0':
                            await context.clear_cookies(name=name)
                        else:
                            await context.add_cookies([{'name': name, 'value': morsel.value,
                                'url': 'https://localhost:3002', 'secure': True,
                                'httpOnly': True, 'sameSite': 'Strict'}])
                await route.fulfill(status=res.status_code, body=res.content,
                    headers={k: v for k, v in res.headers.items() if k != 'set-cookie'})
            await page.route('**/*', route_handler)
            await page.goto('https://localhost:3002/login')
            await expect(page.locator('#setup')).to_be_visible()
            await page.locator('#new-password').fill(PASSWORD)
            await page.locator('#repeat-password').fill(PASSWORD)
            await page.locator('#setup button').click()
            await expect(page.locator('#confirm')).to_be_visible()
            raw = base64.b32decode(await page.locator('#seed').inner_text())
            await page.locator('#setup-code').fill(store.totp(raw, int(time.time()//30)))
            await page.locator('#confirm button').click()
            await expect(page.locator('#recovery')).to_be_visible()
            codes = (await page.locator('#codes').inner_text()).splitlines()
            assert len(codes) == 8
            await expect(page.locator('#activate')).to_be_disabled()
            assert not store.configured()
            await page.locator('#saved').check()
            await page.locator('#activate').click()
            await page.wait_for_url('https://localhost:3002/')
            assert store.configured()
            await context.clear_cookies()
            backend.cookies.clear()
            await page.goto('https://localhost:3002/login')
            await expect(page.locator('#login')).to_be_visible()
            await page.locator('#password').fill(PASSWORD)
            await page.locator('#code').fill(codes[0])
            await page.locator('#login button').click()
            await page.wait_for_url('https://localhost:3002/')
            assert not errors, errors
        finally:
            await browser.close()


def test_admin_websocket_revocation():
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from fastapi import WebSocket
    a = app()
    @a.websocket('/ws')
    async def echo(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                await ws.send_text(await ws.receive_text())
        except WebSocketDisconnect:
            await ws.close(code=4401)
    _, _, token = enroll()
    with TestClient(a, client=('100.101.102.103', 1234), base_url='https://100.100.100.100:3002') as c:
        c.cookies.set(admin_auth.COOKIE, token)
        with c.websocket_connect('wss://100.100.100.100:3002/ws', headers={'origin': 'https://100.100.100.100:3002'}) as ws:
            ws.send_text('before')
            assert ws.receive_text() == 'before'
            store.revoke(store.session(token)['id'])
            ws.send_text('after')
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
