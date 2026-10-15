from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pytest
from usage_activity_chart import build_chart
from usage_activity_store import footer_harness

TZ=ZoneInfo('America/Los_Angeles')
NOW=datetime(2026,10,13,12,tzinfo=TZ)


@pytest.mark.parametrize('period,count,resolution',[
    ('day',12,'hour'),('week',7,'day'),('month',31,'day'),('year',13,'month'),
])
def test_ranges_and_empty_buckets(period,count,resolution):
    result=build_chart([],NOW,period)
    assert len(result['buckets'])==count
    assert result['resolution']==resolution
    assert all(b['turns']==0 for b in result['buckets'])


def test_filters_breakdown_and_boundary_counts():
    records=[((NOW-timedelta(days=1)).timestamp(),'llama-server',10,5,'goose'),
             ((NOW-timedelta(days=1)).timestamp(),'codex',20,10,'codex'),
             (NOW.timestamp(),'codex',100,100,'codex')]
    chart=build_chart(records,NOW,'week')
    assert chart['totals']['total']==45
    assert sum(b['total'] for b in chart['buckets'])==45
    assert {d['category'] for d in chart['breakdown']}=={'goose','codex'}
    filtered=build_chart(records,NOW,'week',selection='goose')
    assert filtered['totals']['total']==15
    backend=build_chart(records,NOW,'week',group='provider',selection='llama-server')
    assert backend['totals']==filtered['totals']


def test_custom_includes_end_date_and_rejects_bad_dates():
    records=[(datetime(2026,10,12,23,59,tzinfo=TZ).timestamp(),'codex',2,3,'codex')]
    result=build_chart(records,NOW,'custom','2026-10-10','2026-10-12')
    assert len(result['buckets'])==3
    assert result['totals']['total']==5
    for start,end in [('bad','2026-10-12'),('2026-10-13','2026-10-12'),('2026-10-13','2026-10-14')]:
        with pytest.raises(ValueError):
            build_chart([],NOW,'custom',start,end)


@pytest.mark.parametrize('day,hours',[(datetime(2026,3,8,23,59,tzinfo=TZ),23),
                                    (datetime(2026,11,1,23,59,tzinfo=TZ),25)])
def test_hour_buckets_follow_dst(day,hours):
    chart=build_chart([],day,'day')
    assert len(chart['buckets'])==hours
    assert len({b['day'] for b in chart['buckets']})==hours


def test_max_keeps_older_history_and_month_totals():
    records=[(datetime(2024,1,15,tzinfo=TZ).timestamp(),'codex',3,7,'codex'),
             (datetime(2026,10,12,tzinfo=TZ).timestamp(),'goose',4,6,'goose')]
    chart=build_chart(records,NOW,'max')
    assert len(chart['buckets'])==34
    assert chart['totals']['total']==20
    assert sum(b['total'] for b in chart['buckets'])==20


def test_harness_is_independent_of_backend():
    assert footer_harness('goose','{"model_provider":"llama"}')=='goose'
    assert footer_harness('claude','{"model_provider":"llama"}')=='claude'
    assert footer_harness(None,'{"harness":"Llama Server"}')=='weave'
    assert footer_harness(None,None)=='unlabeled'
    assert footer_harness(None,'{"harness":[]}')=='unlabeled'
    assert footer_harness('openrouter',None)=='unlabeled'


async def test_existing_ledger_migration_preserves_deleted_history():
    from database import get_db
    import usage_activity_store as store
    conn=await get_db()
    await conn.execute('''CREATE TABLE usage_activity_footer (message_id INTEGER PRIMARY KEY,
        created_at REAL NOT NULL, provider TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER)''')
    await conn.execute('INSERT INTO usage_activity_footer VALUES (99, ?, ?, 10, 5)',
                       (NOW.timestamp()-86400,'codex'))
    await store.retain_footer_usage(conn)
    rows=await conn.execute_fetchall('SELECT provider, input_tokens, output_tokens, harness FROM usage_activity_footer')
    assert tuple(rows[0])==('codex',10,5,'codex')
    await store.retain_footer_usage(conn)
    assert len(await store.rows(conn))==1


async def test_admin_chart_proxy_preserves_filters_and_status(monkeypatch):
    import admin_server
    import httpx
    from starlette.requests import Request
    request=Request({'type':'http','method':'GET','path':'/api/usage/chart',
        'query_string':b'period=custom&start=2026-10-10&end=2026-10-12&group=harness&selection=goose',
        'headers':[]})
    async def get(port,path,timeout):
        assert port==1234
        assert path=='/api/usage/chart?period=custom&start=2026-10-10&end=2026-10-12&group=harness&selection=goose'
        return httpx.Response(400,json={'detail':'Unknown category'})
    monkeypatch.setattr(admin_server,'INSTANCES',{'main':{'port':1234}})
    monkeypatch.setattr(admin_server,'_get_instance',get)
    response=await admin_server.api_usage_chart(request)
    assert response.status_code==400


async def test_chart_route_and_validation(activity_api):
    import httpx
    app,_=activity_api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.get('/api/usage/chart?period=day')
        assert response.status_code==200
        assert response.json()['resolution']=='hour'
        assert (await client.get('/api/usage/chart?period=custom&start=bad')).status_code==400
        assert (await client.get('/api/usage/chart?period=bad')).status_code==400
        assert (await client.get('/api/usage/chart?selection=bad')).status_code==400


# Share the isolated modular API fixture, without importing any live host.
from tests.test_usage_activity_integration import activity_api
