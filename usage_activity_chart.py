"""Bounded calendar chart buckets and independent harness/backend categories."""
from datetime import date, datetime, timedelta
from bisect import bisect_right
import usage_activity_store as store


def build_chart(records, now, period='month', start=None, end=None, group='harness', selection='all'):
    from usage_activity import _midnight, _shift_months
    if period not in {'max', 'year', 'month', 'week', 'day', 'custom'}:
        raise ValueError('Choose max, year, month, week, day or custom')
    if group not in {'harness', 'provider'}:
        raise ValueError('Choose harness or provider breakdown')
    today = _midnight(now)
    if period == 'custom':
        try:
            first, last = date.fromisoformat(start or ''), date.fromisoformat(end or '')
        except ValueError:
            raise ValueError('Supply valid start and end dates (YYYY-MM-DD)') from None
        if first > last or first < date(1970,1,1) or last > now.date():
            raise ValueError('Dates must be ordered, from 1970 onwards, and no later than today')
        left = datetime.combine(first, datetime.min.time(), now.tzinfo)
        right = min(datetime.combine(last+timedelta(days=1), datetime.min.time(), now.tzinfo), now)
    else:
        right = now
        left = {
            'day': today, 'week': today-timedelta(days=6),
            'month': _shift_months(today,-1), 'year': _shift_months(today,-12),
            'max': _midnight(datetime.fromtimestamp(min((r[0] for r in records),default=now.timestamp()),now.tzinfo)),
        }[period]
    index = 4 if group == 'harness' else 1
    categories = sorted({r[index] for r in records})
    if selection != 'all' and selection not in categories:
        raise ValueError('Unknown category')
    scoped = [r for r in records if left.timestamp() <= r[0] < right.timestamp()
              and (selection == 'all' or r[index] == selection)]
    span = (right.date()-left.date()).days+1
    resolution = 'hour' if span <= 2 else 'day' if span <= 90 else 'month'
    boundaries = []
    cursor = left
    if resolution == 'month':
        cursor = cursor.replace(day=1)
    while cursor.timestamp() < right.timestamp():
        boundaries.append(cursor)
        if resolution == 'hour':
            # Elapsed-time increments preserve both repeated DST hours.
            cursor = datetime.fromtimestamp(cursor.timestamp()+3600,now.tzinfo)
        elif resolution == 'day':
            cursor += timedelta(days=1)
        else:
            cursor = _shift_months(cursor,1)
    if not boundaries:
        boundaries = [left]
    stamps = [b.timestamp() for b in boundaries]
    grouped = [[] for _ in boundaries]
    for row in scoped:
        grouped[bisect_right(stamps,row[0])-1].append(row)
    def breakdown(rows):
        groups = {}
        for row in rows:
            groups.setdefault(row[index],[]).append(row)
        return [{'category': category, **store.summarize(items)} for category,items in sorted(groups.items())]
    buckets = [{'day': b.isoformat() if resolution == 'hour' else b.strftime('%Y-%m' if resolution == 'month' else '%Y-%m-%d'),
                **store.summarize(items), 'breakdown': breakdown(items)} for b,items in zip(boundaries,grouped)]
    return {'period': period, 'group': group, 'selection': selection, 'categories': categories,
            'timezone': str(now.tzinfo), 'start': left.isoformat(), 'end': right.isoformat(),
            'resolution': resolution, 'buckets': buckets, 'breakdown': breakdown(scoped),
            'totals': store.summarize(scoped)}
