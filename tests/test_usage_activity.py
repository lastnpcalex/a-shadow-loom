"""Tests for the Usage & Activity read-only aggregation module."""

import time
from datetime import datetime, timedelta

import pytest

from database import get_db
from usage_activity import UsageActivity, _coverage, _pct_change, _period_bounds


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    # All fixture messages and dashboard windows share one deterministic clock.
    from zoneinfo import ZoneInfo
    fixed = datetime(2026, 10, 13, 12, tzinfo=ZoneInfo('America/Los_Angeles'))
    monkeypatch.setattr(time, 'time', lambda: fixed.timestamp())
    return fixed


async def _insert_conversation() -> int:
    conn = await get_db()
    cur = await conn.execute(
        "INSERT INTO conversations (title, created_at, updated_at) VALUES ('usage-test', ?, ?)",
        (time.time(), time.time()),
    )
    return cur.lastrowid


async def _insert_assistant_message(conv_id: int, created_at: float, *, cc_session_mode=None,
                                    input_tokens=None, output_tokens=None) -> None:
    conn = await get_db()
    await conn.execute(
        """INSERT INTO messages (conversation_id, role, content, token_estimate,
                                 is_active, created_at, cc_session_mode,
                                 turn_input_tokens, turn_output_tokens)
           VALUES (?, 'assistant', 'x', 0, 1, ?, ?, ?, ?)""",
        (conv_id, created_at, cc_session_mode, input_tokens, output_tokens),
    )


async def test_year_previous_window_stays_in_previous_year(fixed_clock):
    now = fixed_clock
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    bounds = {b["key"]: b for b in _period_bounds(now)}
    year = bounds["year"]
    # The YoY baseline must lie entirely within the previous calendar year.
    assert year["prev_start"] < year["prev_end"] <= year_start


async def test_coverage_and_change_helpers():
    assert _coverage(10, 10) == "reported"
    assert _coverage(7, 10) == "partial"
    assert _coverage(1, 10) == "sparse"
    assert _coverage(0, 10) == "unavailable"
    assert _coverage(0, 0) == "no-activity"
    assert _pct_change(150, 100) == pytest.approx(50.0)
    assert _pct_change(50, 100) == pytest.approx(-50.0)
    assert _pct_change(10, 0) is None


async def test_snapshot_aggregates_stored_counters(tmp_database, fixed_clock):
    conv = await _insert_conversation()
    now = fixed_clock
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Anchor today's turns a fixed offset past local midnight so the test
    # cannot flake across a day boundary however long the suite runs.
    await _insert_assistant_message(conv, (midnight + timedelta(hours=1)).timestamp(),
                                    cc_session_mode="codex", input_tokens=1000, output_tokens=500)
    await _insert_assistant_message(conv, (midnight + timedelta(hours=2)).timestamp(),
                                    cc_session_mode="codex", input_tokens=2000, output_tokens=700)
    await _insert_assistant_message(conv, (midnight + timedelta(hours=3)).timestamp())
    # Yesterday 23:30 local: outside today's window, counted turn.
    await _insert_assistant_message(conv, (midnight - timedelta(minutes=30)).timestamp(),
                                    cc_session_mode="lorem", input_tokens=100, output_tokens=50)

    activity = UsageActivity()
    snap = await activity.snapshot(force=True)

    today = snap["periods"]["today"]
    assert today["input"] == 3000
    assert today["output"] == 1200
    assert today["total"] == 4200
    assert today["turns"] == 3
    assert today["counted_turns"] == 2
    assert today["coverage"] == "partial"

    # Today's midnight is inside the current ISO week by definition; the
    # yesterday turn may fall in either ISO week depending on the weekday,
    # so only today's guaranteed contributions are asserted.
    week = snap["periods"]["week"]
    assert week["turns"] >= 3
    assert week["input"] >= 3000

    days = {d["day"] for d in snap["daily"]}
    assert midnight.strftime("%Y-%m-%d") in days

    providers = {p["provider"]: p for p in snap["providers"]}
    assert "codex" in providers
    assert providers["codex"]["input"] == 3000
    assert providers["codex"]["output"] == 1200

    assert snap["all_time"]["input"] == 3100
    assert snap["all_time"]["output"] == 1250
    assert snap["all_time"]["first_recorded_turn"] is not None
    assert "input_semantics" in snap and "periods" in snap


async def test_snapshot_empty_database(tmp_database):
    activity = UsageActivity()
    snap = await activity.snapshot(force=True)
    today = snap["periods"]["today"]
    assert today["turns"] == 0
    assert today["coverage"] == "no-activity"
    assert today["change"]["total_pct"] is None
    assert len(snap["daily"]) == 60
    assert all(day['turns'] == 0 for day in snap['daily'])
    assert snap["providers"] == []
    assert snap["all_time"]["first_recorded_turn"] is None


async def test_snapshot_is_cached_until_forced(tmp_database):
    conv = await _insert_conversation()
    await _insert_assistant_message(conv, time.time()-1, input_tokens=5, output_tokens=5)

    activity = UsageActivity()
    first = await activity.snapshot(force=True)
    await _insert_assistant_message(conv, time.time()-1, input_tokens=99, output_tokens=99)
    cached = await activity.snapshot()  # within TTL: no re-query
    assert cached["periods"]["today"]["input"] == first["periods"]["today"]["input"]
    forced = await activity.snapshot(force=True)
    assert forced["periods"]["today"]["input"] == 104


def test_calendar_comparisons_and_dst():
    from zoneinfo import ZoneInfo
    tz = ZoneInfo('America/Los_Angeles')
    now = datetime(2026, 3, 31, 12, tzinfo=tz)
    specs = {s['key']: s for s in _period_bounds(now)}
    assert specs['month']['prev_end'] <= specs['month']['start']
    assert specs['year']['prev_end'] == datetime(2025, 3, 31, 12, tzinfo=tz)
    for day, hours in [(datetime(2026,3,8,23,tzinfo=tz),22), (datetime(2026,11,1,23,tzinfo=tz),24)]:
        bounds = {s['key']:s for s in _period_bounds(day)}
        assert (day.timestamp()-bounds['today']['start'].timestamp())/3600 == hours
        assert day.timestamp()-bounds['rolling_5h']['start'].timestamp() == 18000


async def test_retention_updates_without_double_count_and_survives_deletion(fixed_clock):
    import database as db
    conv = await _insert_conversation()
    await _insert_assistant_message(conv, time.time()-3600, input_tokens=10, output_tokens=5)
    activity = UsageActivity()
    assert (await activity.snapshot(force=True))['all_time']['total'] == 15
    conn = await get_db()
    await conn.execute('UPDATE messages SET turn_output_tokens=9 WHERE conversation_id=?', (conv,))
    assert (await activity.snapshot(force=True))['all_time']['total'] == 19
    # Capture a final update not yet sampled by the dashboard.
    await conn.execute('UPDATE messages SET turn_output_tokens=12 WHERE conversation_id=?', (conv,))
    await db.delete_conversation(conv)
    snapshot = await activity.snapshot(force=True)
    assert snapshot['all_time']['total'] == 22
    assert snapshot['all_time']['turns'] == 1
    assert snapshot['averages']['turns_per_active_day'] == 1


async def test_missing_counters_and_daily_gaps():
    conv = await _insert_conversation()
    await _insert_assistant_message(conv, time.time()-3600)
    activity = UsageActivity()
    snapshot = await activity.snapshot(force=True)
    assert snapshot['periods']['today']['total'] is None
    assert snapshot['daily'][-1]['coverage'] == 'unavailable'
    assert snapshot['daily'][-2]['total'] == 0
    assert len(snapshot['daily']) == 60


async def test_reports_are_immutable_and_catch_up(fixed_clock):
    import usage_activity_store as store
    conv = await _insert_conversation()
    await _insert_assistant_message(conv, time.time()-86400, input_tokens=25, output_tokens=15)
    activity = UsageActivity()
    first = await activity.yesterday()
    assert first['day'] == '2026-10-12'
    assert first['total'] == 40
    assert 'Generated ' in first['markdown']
    await activity.snapshot(force=True)  # establish the scheduled catch-up cursor
    conn = await get_db()
    await conn.execute('UPDATE messages SET turn_output_tokens=200 WHERE conversation_id=?', (conv,))
    assert await activity.yesterday() == first
    from zoneinfo import ZoneInfo
    tz=ZoneInfo('America/Los_Angeles')
    async with __import__('database').write_transaction() as tx:
        await store.catch_up(tx,tz,datetime(2026,10,16,8,tzinfo=tz),store.DEFAULT_SETTINGS)
    history = await store.report_history(conn,tz)
    assert [r['day'] for r in history] == ['2026-10-15','2026-10-14','2026-10-13','2026-10-12']


async def test_manual_report_does_not_skip_missed_days():
    from zoneinfo import ZoneInfo
    tz=ZoneInfo('America/Los_Angeles')
    day=datetime(2026,10,10,8,tzinfo=tz)
    activity=UsageActivity(clock=lambda:day.timestamp())
    await activity.snapshot(force=True)
    activity.clock=lambda:(day+timedelta(days=5)).timestamp()
    await activity.yesterday()
    result=await activity.snapshot(force=True)
    assert [r['day'] for r in result['snapshots']]==['2026-10-14','2026-10-13','2026-10-12','2026-10-11','2026-10-10','2026-10-09']


async def test_schedule_validation_and_before_morning():
    activity=UsageActivity()
    with pytest.raises(ValueError):
        await activity.configure({'timezone':'no/such-zone','hour':7,'enabled':True})
    with pytest.raises(ValueError):
        await activity.configure({'timezone':'UTC','hour':24,'enabled':True})
    await activity.configure({'timezone':'UTC','hour':7,'enabled':False})
    assert (await activity.snapshot(force=True))['snapshots'] == []


async def test_one_missing_direction_is_not_zero():
    conv=await _insert_conversation()
    await _insert_assistant_message(conv,time.time()-1,output_tokens=20)
    current=(await UsageActivity().snapshot(force=True))['periods']['today']
    assert current['input'] is None
    assert current['output']==20
    assert current['coverage']=='partial'


@pytest.mark.parametrize('mode,model,evidence,expected', [
    (None, None, '{"model_provider":"llama-server"}', 'llama-server'),
    ('claude', 'anything', '{"model_provider":"llama"}', 'llama-server'),
    ('goose', 'goose:llama:local-model', None, 'llama-server'),
    (None, 'my-model.gguf', None, 'llama-server'),
    ('local', None, None, 'llama-server'),
    (None, None, None, 'unlabeled'),
    (None, 'my-model.gguf', '{"model_provider":"openrouter"}', 'unlabeled'),
    ('dream-claude', 'dream:local-model.gguf', None, 'dream-claude'),
    ('codex', None, '{invalid', 'codex'),
])
def test_llama_backend_classification(mode,model,evidence,expected):
    from usage_activity_store import footer_provider
    assert footer_provider(mode,model,evidence)==expected


async def test_llama_counts_are_reclassified_without_double_count():
    import json
    conv=await _insert_conversation()
    await _insert_assistant_message(conv,time.time()-1,input_tokens=100,output_tokens=20)
    activity=UsageActivity()
    first=await activity.snapshot(force=True)
    assert first['providers'][0]['provider']=='unlabeled'
    conn=await get_db()
    await conn.execute('UPDATE messages SET model_attestation=? WHERE conversation_id=?',
        (json.dumps({'model_provider':'llama-server'}),conv))
    for _ in range(2):
        result=await activity.snapshot(force=True)
        assert result['all_time']['total']==120
        assert result['providers']==[{'provider':'llama-server','turns':1,'counted_turns':1,
            'input':100,'output':20,'total':120,'coverage':'reported'}]


async def test_after_midnight_excludes_future_records():
    from zoneinfo import ZoneInfo
    now=datetime(2026,10,14,0,30,tzinfo=ZoneInfo('America/Los_Angeles'))
    conv=await _insert_conversation()
    await _insert_assistant_message(conv,now.timestamp()-60,input_tokens=2,output_tokens=3)
    await _insert_assistant_message(conv,now.timestamp()+60,input_tokens=100,output_tokens=100)
    result=await UsageActivity(clock=lambda:now.timestamp()).snapshot(force=True)
    assert result['periods']['today']['total']==5
    assert result['daily'][-1]['total']==5
    assert result['all_time']['total']==5


async def test_branch_deletion_retains_counts():
    import database as db
    conv=await db.create_conversation('delete branch')
    msg=await db.add_message(conv['id'],'assistant','x',turn_input_tokens=3,turn_output_tokens=4)
    await db.delete_branch(msg['id'])
    result=await UsageActivity(clock=lambda:time.time()+10).snapshot(force=True)
    assert result['all_time']['total']==7


async def test_before_morning_does_not_create_yesterday_report():
    from zoneinfo import ZoneInfo
    now=datetime(2026,10,13,6,59,tzinfo=ZoneInfo('America/Los_Angeles'))
    activity=UsageActivity(clock=lambda:now.timestamp())
    result=await activity.snapshot(force=True)
    assert [s['day'] for s in result['snapshots']]==['2026-10-11']
    activity.clock=lambda:(now+timedelta(minutes=1)).timestamp()
    result=await activity.snapshot(force=True)
    assert [s['day'] for s in result['snapshots']]==['2026-10-12','2026-10-11']
