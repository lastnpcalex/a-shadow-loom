"""Modular footer-token activity dashboard and daily snapshots."""

from __future__ import annotations

import asyncio
import json
import logging
import calendar
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
import usage_activity_store as store

CACHE_TTL_SECONDS = 30.0
DAILY_WINDOW_DAYS = 60

def _midnight(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _week_start(dt: datetime) -> datetime:
    """ISO week start: Monday 00:00 local."""
    d = _midnight(dt)
    return d - timedelta(days=d.weekday())


def _shift_months(dt: datetime, months: int) -> datetime:
    m = dt.month - 1 + months
    year = dt.year + m // 12
    month = m % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _coverage(counted: int, turns: int) -> str:
    if turns <= 0:
        return "no-activity"
    ratio = counted / turns
    if ratio >= 0.98:
        return "reported"
    if ratio >= 0.5:
        return "partial"
    if ratio > 0:
        return "sparse"
    return "unavailable"


def _pct_change(current: int, previous: int):
    if current is None or previous is None or previous <= 0:
        return None
    return (current - previous) / previous * 100.0


def _period_bounds(now: datetime) -> list[dict]:
    """Current and equivalent-elapsed-previous windows, in local time."""
    midnight = _midnight(now)
    week_start = _week_start(now)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    def elapsed(start: datetime) -> timedelta:
        return now - start

    prev_month_start = _shift_months(month_start, -1)
    prev_year_start = _shift_months(year_start, -12)
    prev_week_start = week_start - timedelta(weeks=1)
    prev_day_start = midnight - timedelta(days=1)

    return [
        {
            "key": "rolling_5h",
            "label": "Last 5 hours",
            "start": datetime.fromtimestamp(now.timestamp()-5*3600, now.tzinfo),
            "end": now,
            "prev_start": datetime.fromtimestamp(now.timestamp()-10*3600, now.tzinfo),
            "prev_end": datetime.fromtimestamp(now.timestamp()-5*3600, now.tzinfo),
        },
        {
            "key": "today",
            "label": "Today",
            "start": midnight,
            "end": now,
            "prev_start": prev_day_start,
            "prev_end": prev_day_start + elapsed(midnight),
        },
        {
            "key": "week",
            "label": "This week",
            "start": week_start,
            "end": now,
            "prev_start": prev_week_start,
            "prev_end": prev_week_start + elapsed(week_start),
        },
        {
            "key": "month",
            "label": "This month",
            "start": month_start,
            "end": now,
            "prev_start": prev_month_start,
            "prev_end": min(month_start, _shift_months(now, -1)),
        },
        {
            "key": "year",
            "label": "This year",
            "start": year_start,
            "end": now,
            "prev_start": prev_year_start,
            "prev_end": _shift_months(now, -12),
        },
    ]



INPUT_NOTE = (
    "Activity totals use recorded chat-footer counters, not billing consumption. "
    "Claude input represents final context size; child-agent usage is excluded. "
    "Missing historical telemetry cannot be reconstructed. Saved reports are immutable; "
    "late counters appear in dashboard totals but do not rewrite saved reports."
)


class UsageActivity:
    def __init__(self, clock=None):
        self.clock = clock or time.time
        self._cache = None
        self._lock = asyncio.Lock()

    async def snapshot(self, force=False):
        import database as db
        async with self._lock:
            cache_key = (db.DB_PATH, time.monotonic())
            if (not force and self._cache and self._cache[0] == cache_key[0]
                    and cache_key[1]-self._cache[1] < CACHE_TTL_SECONDS):
                return self._cache[2]
            # Isolated transaction makes mirroring and report capture coherent.
            async with db.write_transaction() as conn:
                await store.retain_footer_usage(conn)
                options = await store.settings(conn)
                tz = ZoneInfo(options['timezone'])
                now = datetime.fromtimestamp(self.clock(), tz)
                await store.catch_up(conn, tz, now, options)
                records = await store.rows(conn, end=now.timestamp())
                periods = {}
                for spec in _period_bounds(now):
                    def window(start, end):
                        return store.summarize([r for r in records if start.timestamp() <= r[0] < end.timestamp()])
                    current = window(spec['start'], spec['end'])
                    previous = window(spec['prev_start'], spec['prev_end'])
                    periods[spec['key']] = {
                        **current, 'label': spec['label'], 'start': spec['start'].isoformat(),
                        'end': spec['end'].isoformat(), 'previous': previous,
                        'prev_start': spec['prev_start'].isoformat(), 'prev_end': spec['prev_end'].isoformat(),
                        'change': {key+'_pct': _pct_change(current[key], previous[key]) for key in ('input','output','total')},
                    }
                grouped = {}
                for r in records:
                    day = datetime.fromtimestamp(r[0], tz).date().isoformat()
                    grouped.setdefault(day, []).append(r)
                daily = []
                for offset in reversed(range(DAILY_WINDOW_DAYS)):
                    day = (now.date()-timedelta(days=offset)).isoformat()
                    daily.append({'day': day, **store.summarize(grouped.get(day, []))})
                all_time = store.summarize(records)
                all_time['first_recorded_turn'] = next((datetime.fromtimestamp(r[0],tz).isoformat()
                    for r in sorted(records, key=lambda r:r[0]) if r[2] is not None or r[3] is not None), None)
                elapsed_days = (now.date()-datetime.fromtimestamp(min(r[0] for r in records),tz).date()).days+1 if records else 0
                active_days = len(grouped)
                payload = {
                    'generated_at': now.isoformat(), 'timezone': str(tz),
                    'periods': periods, 'daily': daily, 'providers': store.providers(records),
                    'all_time': all_time, 'input_semantics': INPUT_NOTE, 'settings': options,
                    'averages': {'active_days': active_days, 'calendar_days': elapsed_days,
                        'turns_per_day': len(records)/elapsed_days if elapsed_days else 0,
                        'turns_per_active_day': len(records)/active_days if active_days else 0},
                    'snapshots': await store.report_history(conn, tz),
                }
            self._cache = (*cache_key, payload)
            return payload

    async def yesterday(self):
        import database as db
        async with self._lock:
            async with db.write_transaction() as conn:
                await store.retain_footer_usage(conn)
                options = await store.settings(conn)
                tz = ZoneInfo(options['timezone'])
                now = datetime.fromtimestamp(self.clock(),tz)
                result = await store.save_report(conn, now.date()-timedelta(days=1), tz, now)
            self._cache = None
            return result

    async def chart(self, period='month', start=None, end=None, group='harness', selection='all'):
        from usage_activity_chart import build_chart
        import database as db
        await self.snapshot()
        conn = await db.get_db()
        options = await store.settings(conn)
        now = datetime.fromtimestamp(self.clock(),ZoneInfo(options['timezone']))
        async with conn.execute('SELECT created_at, provider, input_tokens, output_tokens, harness FROM usage_activity_footer WHERE created_at < ?',
                                (now.timestamp(),)) as cur:
            records = await cur.fetchall()
        return build_chart(records, now, period, start, end, group, selection)

    async def configure(self, data):
        if not isinstance(data,dict) or set(data) != {'timezone','hour','enabled'}:
            raise ValueError('Supply timezone, hour and enabled')
        if type(data['hour']) is not int or not 0 <= data['hour'] <= 23 or type(data['enabled']) is not bool:
            raise ValueError('Hour must be 0–23; enabled must be a boolean')
        if not isinstance(data['timezone'],str):
            raise ValueError('Timezone must be an IANA name')
        try:
            ZoneInfo(data['timezone'])
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Unknown IANA timezone') from None
        import database as db
        async with self._lock:
            async with db.write_transaction() as conn:
                await store.ensure_schema(conn)
                await conn.execute('INSERT OR REPLACE INTO usage_activity_settings VALUES (1, ?)', (json.dumps(data),))
            self._cache = None
        return data

    async def run(self):
        while True:
            try:
                await self.snapshot(force=True)
            except Exception:
                logging.getLogger(__name__).exception('Usage Activity refresh failed')
            await asyncio.sleep(60)


activity = UsageActivity()
router = APIRouter(prefix='/api/usage')


@router.get('/activity')
async def get_activity():
    return JSONResponse(await activity.snapshot(force=True), headers={'Cache-Control':'no-store'})


@router.post('/snapshots/yesterday')
async def create_yesterday():
    return await activity.yesterday()


@router.get('/chart')
async def get_chart(period: str = 'month', start: str | None = None, end: str | None = None,
                    group: str = 'harness', selection: str = 'all'):
    try:
        return JSONResponse(await activity.chart(period,start,end,group,selection),headers={'Cache-Control':'no-store'})
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc


@router.put('/settings')
async def put_settings(data: dict):
    try:
        return await activity.configure(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
