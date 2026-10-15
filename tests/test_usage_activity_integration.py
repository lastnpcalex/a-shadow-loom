"""Exercise the modular routes and the actual Admin DOM without live services."""
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI

import usage_activity
from tests.test_usage_activity import _insert_conversation, _insert_assistant_message


@pytest.fixture
def activity_api(monkeypatch):
    now = datetime(2026,10,13,12,tzinfo=ZoneInfo('America/Los_Angeles'))
    monkeypatch.setattr(usage_activity,'activity',usage_activity.UsageActivity(clock=lambda:now.timestamp()))
    app=FastAPI()
    app.include_router(usage_activity.router)
    return app,now


async def test_module_routes_and_validation(activity_api):
    app,now=activity_api
    conv=await _insert_conversation()
    await _insert_assistant_message(conv,now.timestamp()-86400,input_tokens=12,output_tokens=8)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.get('/api/usage/activity')
        assert response.status_code==200
        assert response.headers['cache-control']=='no-store'
        assert len(response.json()['daily'])==60
        report=(await client.post('/api/usage/snapshots/yesterday')).json()
        assert report['total']==20
        assert report['markdown'].startswith('# My day in tokens')
        assert (await client.post('/api/usage/snapshots/yesterday')).json()==report
        assert (await client.put('/api/usage/settings',json={'timezone':'bad','hour':7,'enabled':True})).status_code==400
        assert (await client.put('/api/usage/settings',json={'timezone':'UTC','hour':9,'enabled':False})).status_code==200


async def test_admin_proxy_propagates_validation(monkeypatch):
    import admin_server
    async def put(port,path,data):
        assert path=='/api/usage/settings'
        return httpx.Response(400,json={'detail':'Unknown IANA timezone'})
    monkeypatch.setattr(admin_server,'INSTANCES',{'main':{'port':1234}})
    monkeypatch.setattr(admin_server,'_put_instance',put)
    response=await admin_server.api_usage_settings({'timezone':'bad','hour':7,'enabled':True})
    assert response.status_code==400
    assert json.loads(response.body)['detail']=='Unknown IANA timezone'


async def test_admin_dashboard_snapshot_and_downloads(activity_api,tmp_path):
    playwright=pytest.importorskip('playwright.async_api')
    app,now=activity_api
    conv=await _insert_conversation()
    await _insert_assistant_message(conv,now.timestamp()-86400,input_tokens=1234,output_tokens=567)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://usage.test') as client:
        async with playwright.async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                page=await browser.new_page(viewport={'width':390,'height':844},accept_downloads=True)
                errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                async def api(route):
                    request=route.request
                    result=await client.request(request.method,request.url,content=request.post_data,
                        headers={'Content-Type':'application/json'})
                    await route.fulfill(status=result.status_code,body=result.content,content_type='application/json')
                await page.route('**/api/usage/**',api)
                await page.route('http://usage.test/',lambda route:route.fulfill(body='<html><body></body></html>',content_type='text/html'))
                await page.goto('http://usage.test/')
                html=Path('static/admin/index.html').read_text(encoding='utf-8')
                section=html[html.index('<section id="view-usage"'):html.index('<section id="view-activity"')]
                await page.set_content(section)
                await page.add_style_tag(path='static/admin/admin.css')
                await page.add_style_tag(path='static/admin/admin-activity.css')
                await page.add_script_tag(content="function esc(s){return String(s).replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));}")
                await page.add_script_tag(path='static/admin/admin-activity.js')
                await page.evaluate("document.getElementById('view-usage').classList.add('active'); loadUsageActivity({force:true})")
                await playwright.expect(page.locator('.usage-bar')).to_have_count(31)
                await playwright.expect(page.locator('.usage-grid-line')).to_have_count(5)
                await playwright.expect(page.locator('.usage-y-tick')).to_have_count(5)
                await playwright.expect(page.locator('.usage-y-title')).to_have_text('Tokens (thousands)')
                await playwright.expect(page.locator('.usage-x-title')).to_have_text('Date · America/Los_Angeles')
                ticks=await page.locator('.usage-x-tick').all_text_contents()
                assert len(ticks)>=2 and 'Sep' in ticks[0] and 'Oct' in ticks[-1]
                labels=await page.locator('.usage-x-tick').evaluate_all('(nodes)=>nodes.map(n=>({left:n.getBoundingClientRect().left,right:n.getBoundingClientRect().right}))')
                assert all(a['right'] <= b['left'] for a,b in zip(labels,labels[1:]))
                await page.locator('#usage-daily').screenshot(path=str(tmp_path/'chart-axes-mobile.png'))
                await page.set_viewport_size({'width':1280,'height':844})
                await playwright.expect(page.locator('.usage-x-tick')).to_have_count(7)
                await page.set_viewport_size({'width':390,'height':844})
                await playwright.expect(page.locator('.usage-x-tick')).to_have_count(3)
                await page.locator('#usage-chart-period').select_option('day')
                await playwright.expect(page.locator('.usage-bar')).to_have_count(12)
                await playwright.expect(page.locator('.usage-x-title')).to_have_text('Date / time · America/Los_Angeles')
                await page.locator('#usage-chart-period').select_option('week')
                await playwright.expect(page.locator('.usage-bar')).to_have_count(7)
                await page.locator('#usage-chart-period').select_option('year')
                await playwright.expect(page.locator('.usage-bar')).to_have_count(13)
                await playwright.expect(page.locator('.usage-x-title')).to_have_text('Month · America/Los_Angeles')
                await page.locator('#usage-chart-period').select_option('max')
                await playwright.expect(page.locator('.usage-bar')).to_have_count(36)
                await page.locator('#usage-chart-period').select_option('custom')
                await page.locator('#usage-chart-start').fill('2026-10-10')
                await page.locator('#usage-chart-end').fill('2026-10-12')
                await page.locator('#usage-chart-custom button').click()
                await playwright.expect(page.locator('.usage-bar')).to_have_count(3)
                await page.locator('#usage-chart-category').select_option('unlabeled')
                await playwright.expect(page.locator('#usage-chart-status')).to_contain_text('1 turns')
                await page.locator('#usage-chart-group').select_option('provider')
                await playwright.expect(page.locator('#usage-chart-breakdown th').first).to_contain_text('Backend / provider')
                await page.locator('#usage-chart-colors').select_option('io')
                await playwright.expect(page.locator('.usage-bar-output')).to_have_count(3)
                await playwright.expect(page.locator('#usage-report-preview')).to_contain_text('2026-10-12')
                await playwright.expect(page.locator('#usage-report-card h3')).to_have_text('October 12, 2026')
                await playwright.expect(page.locator('.usage-snapshot-total')).to_contain_text('1,801')
                await playwright.expect(page.locator('.usage-snapshot-stats')).to_contain_text('1,234')
                await playwright.expect(page.locator('.usage-snapshot-stats')).to_contain_text('567')
                await playwright.expect(page.locator('.usage-snapshot footer')).to_contain_text('America/Los_Angeles')
                await page.locator('#usage-report-card').screenshot(path=str(tmp_path/'daily-snapshot-mobile.png'))
                assert await page.locator('#usage-report-card').evaluate('(el)=>el.scrollWidth <= el.clientWidth')
                await page.locator('#btn-usage-yesterday').click()
                await playwright.expect(page.locator('#usage-report-status')).to_contain_text('saved')
                for fmt,ext in [('png','png'),('markdown','md'),('json','json')]:
                    async with page.expect_download() as info:
                        await page.locator(f'[data-usage-export="{fmt}"]').click()
                    download=await info.value
                    assert download.suggested_filename==f'my-day-in-tokens-2026-10-12.{ext}'
                    target=tmp_path/f'report.{ext}'
                    await download.save_as(target)
                    if fmt == 'png':
                        import struct
                        data = target.read_bytes()
                        assert data[:8] == b'\x89PNG\r\n\x1a\n'
                        width, height = struct.unpack('>II', data[16:24])
                        assert width > 300 and height > 400
                        print(f'PNG snapshot: {target}')
                    else:
                        text=target.read_text(encoding='utf-8')
                        assert '1801' in text if fmt=='json' else '1,801' in text
                await page.locator('#usage-timezone').fill('UTC')
                await page.locator('#usage-hour').fill('9')
                await page.locator('#btn-usage-schedule').click()
                await playwright.expect(page.locator('#usage-report-status')).to_have_text('Schedule saved.')
                assert not errors
            finally:
                await browser.close()
