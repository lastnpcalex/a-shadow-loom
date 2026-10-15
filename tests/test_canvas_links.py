import json
import subprocess
from types import SimpleNamespace

import pytest
import canvas_links


async def test_share_link_endpoint_preserves_requested_port(tmp_database, monkeypatch):
    import httpx
    import database as db
    import server
    conv = await db.create_conversation('Canvas link fixture', mode='codex')
    await db.update_conversation_fields(conv['id'], canvas_enabled=1, canvas_slug='dark-penguin-x7k2')
    calls = []
    def link(slug, scheme, port):
        calls.append((slug, scheme, port))
        return f'{scheme}://desktop.tail123.ts.net:{port}/{slug}'
    monkeypatch.setattr(canvas_links, 'canvas_tailnet_url', link)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='https://100.103.144.94:3000') as client:
        response = await client.get(f'/api/canvas/{conv["id"]}/share-link')
    assert response.status_code == 200
    assert response.json()['url'] == 'https://desktop.tail123.ts.net:3000/dark-penguin-x7k2'
    assert calls == [('dark-penguin-x7k2', 'https', 3000)]


def test_tailnet_link_uses_dns_and_instance_port(monkeypatch):
    monkeypatch.setattr(canvas_links.shutil, 'which', lambda _: 'tailscale')
    def status(args, **kwargs):
        assert args == ['tailscale', 'status', '--json']
        assert kwargs['timeout'] == 3
        return SimpleNamespace(stdout=json.dumps({'Self': {'DNSName': 'desktop.tail123.ts.net.'}}))
    monkeypatch.setattr(canvas_links.subprocess, 'run', status)
    assert canvas_links.canvas_tailnet_url('dark-penguin-x7k2', 'https', 3000) == 'https://desktop.tail123.ts.net:3000/dark-penguin-x7k2'


@pytest.mark.parametrize('hostname', ['100.103.144.94', '', 'evil.test/path', 'desktop.ts.net'])
def test_invalid_dns_never_falls_back_to_ip(monkeypatch, hostname):
    monkeypatch.setattr(canvas_links.shutil, 'which', lambda _: 'tailscale')
    monkeypatch.setattr(canvas_links.subprocess, 'run', lambda *a, **k:
        SimpleNamespace(stdout=json.dumps({'Self': {'DNSName': hostname}})))
    with pytest.raises(ValueError):
        canvas_links.canvas_tailnet_url('dark-penguin-x7k2', 'https', 3000)


def test_tailscale_timeout_reports_unavailable(monkeypatch):
    monkeypatch.setattr(canvas_links.shutil, 'which', lambda _: 'tailscale')
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired('tailscale', 3)
    monkeypatch.setattr(canvas_links.subprocess, 'run', timeout)
    with pytest.raises(ValueError):
        canvas_links.canvas_tailnet_url('dark-penguin-x7k2', 'https', 3000)
