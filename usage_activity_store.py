"""Token-only retention and immutable daily reports owned by Usage Activity."""
import json
from datetime import datetime, timedelta

DEFAULT_SETTINGS = {"timezone": "America/Los_Angeles", "hour": 7, "enabled": True}


def footer_provider(session_mode, model, attestation):
    """Identify local backend from turn evidence, never current conversation settings."""
    mode = (session_mode or '').strip()
    try:
        evidence = json.loads(attestation) if isinstance(attestation, str) else attestation
    except (TypeError, ValueError):
        evidence = None
    provider = evidence.get('model_provider') if isinstance(evidence, dict) else None
    if not isinstance(provider, str):
        provider = None
    if provider in {'llama', 'llama-server', 'local'}:
        return 'llama-server'
    # Explicit evidence of another backend wins over a model-name heuristic.
    if not provider:
        if mode == 'local':
            return 'llama-server'
        if model:
            from model_registry import resolve
            route = resolve(model)
            if route.get('backend') == 'llama':
                return 'llama-server'
            if route.get('mode') == 'goose':
                from goose_client import split_goose_model
                if split_goose_model(model)[0] == 'llama':
                    return 'llama-server'
    return mode or 'unlabeled'


def footer_harness(session_mode, attestation):
    try:
        evidence = json.loads(attestation or '{}')
    except (ValueError, TypeError):
        evidence = {}
    identity = evidence.get('harness') if isinstance(evidence,dict) else None
    known = {'Claude Code':'claude', 'Codex':'codex', 'Goose ACP':'goose', 'Antigravity':'gemini',
             'Llama Server':'weave', 'OpenRouter API':'weave', 'Dream Engine':'weave'}
    if isinstance(identity,str) and identity in known:
        return known[identity]
    mode = (session_mode or '').strip()
    if mode in {'claude', 'local', 'umans', 'dream-claude', 'openrouter-claude'}:
        return 'claude'
    if mode in {'codex', 'goose', 'gemini', 'weave'}:
        return mode
    return 'unlabeled'


async def ensure_schema(conn):
    # No foreign keys: deleting chat content must not delete activity history.
    for sql in (
        """CREATE TABLE IF NOT EXISTS usage_activity_footer (
            message_id INTEGER PRIMARY KEY, created_at REAL NOT NULL,
            provider TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER)""",
        "CREATE INDEX IF NOT EXISTS idx_usage_activity_time ON usage_activity_footer(created_at)",
        """CREATE TABLE IF NOT EXISTS usage_activity_reports (
            day TEXT NOT NULL, timezone TEXT NOT NULL, generated_at TEXT NOT NULL,
            payload TEXT NOT NULL, PRIMARY KEY(day, timezone))""",
        "CREATE TABLE IF NOT EXISTS usage_activity_settings (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS usage_activity_schedule_cursor (timezone TEXT PRIMARY KEY, day TEXT NOT NULL)",
    ):
        await conn.execute(sql)
    async with conn.execute('PRAGMA table_info(usage_activity_footer)') as cur:
        columns = {row[1] for row in await cur.fetchall()}
    if 'harness' not in columns:
        await conn.execute("ALTER TABLE usage_activity_footer ADD COLUMN harness TEXT NOT NULL DEFAULT 'unlabeled'")
        # Old retained cloud labels were session modes; preserve that evidence
        # for deleted rows. A collapsed llama-server label cannot identify its harness.
        await conn.execute("""UPDATE usage_activity_footer SET harness = CASE
            WHEN provider IN ('claude','local','umans','dream-claude','openrouter-claude') THEN 'claude'
            WHEN provider IN ('codex','goose','gemini','weave') THEN provider
            ELSE 'unlabeled' END""")


async def retain_footer_usage(conn, where="1", params=()):
    """Idempotent mirror of counts; caller may hold the deletion transaction.

    `where` is an internal SQL fragment, never supplied by an HTTP request.
    Updates replace counters, rather than adding cumulative streaming values.
    """
    await ensure_schema(conn)
    await conn.create_function('usage_footer_provider', 3, footer_provider)
    await conn.create_function('usage_footer_harness', 2, footer_harness)
    await conn.execute(f"""
        INSERT INTO usage_activity_footer
            (message_id, created_at, provider, input_tokens, output_tokens, harness)
        SELECT id, created_at, usage_footer_provider(cc_session_mode, cc_model_used, model_attestation),
               turn_input_tokens, turn_output_tokens, usage_footer_harness(cc_session_mode, model_attestation)
        FROM messages WHERE role='assistant' AND ({where})
        ON CONFLICT(message_id) DO UPDATE SET
            provider=excluded.provider, input_tokens=excluded.input_tokens,
            output_tokens=excluded.output_tokens, harness=excluded.harness
        WHERE provider IS NOT excluded.provider
           OR input_tokens IS NOT excluded.input_tokens
           OR output_tokens IS NOT excluded.output_tokens
           OR harness IS NOT excluded.harness
    """, params)


async def settings(conn):
    await ensure_schema(conn)
    async with conn.execute("SELECT payload FROM usage_activity_settings WHERE id=1") as cur:
        row = await cur.fetchone()
    return json.loads(row[0]) if row else dict(DEFAULT_SETTINGS)


async def rows(conn, start=0, end=float('inf')):
    async with conn.execute("""SELECT created_at, provider, input_tokens, output_tokens
        FROM usage_activity_footer WHERE created_at >= ? AND created_at < ?""",
        (start, end)) as cur:
        return await cur.fetchall()


def summarize(records):
    turns = len(records)
    counted = sum(r[2] is not None or r[3] is not None for r in records)
    input_counted = sum(r[2] is not None for r in records)
    output_counted = sum(r[3] is not None for r in records)
    from usage_activity import _coverage
    return {
        "turns": turns, "counted_turns": counted,
        "input": sum(r[2] or 0 for r in records) if input_counted or not turns else None,
        "output": sum(r[3] or 0 for r in records) if output_counted or not turns else None,
        "total": sum((r[2] or 0) + (r[3] or 0) for r in records) if counted or not turns else None,
        "coverage": (_coverage(counted, turns) if input_counted == counted == output_counted
                     else 'partial'),
    }


def providers(records):
    groups = {}
    for record in records:
        groups.setdefault(record[1], []).append(record)
    return [{"provider": name, **summarize(items)}
            for name, items in sorted(groups.items(), key=lambda pair: -len(pair[1]))]


async def save_report(conn, day, tz, now):
    """First report wins: stable timestamp and figures across repeat requests."""
    async with conn.execute("SELECT payload FROM usage_activity_reports WHERE day=? AND timezone=?",
                            (day.isoformat(), str(tz))) as cur:
        existing = await cur.fetchone()
    if existing:
        return json.loads(existing[0])
    start = datetime.combine(day, datetime.min.time(), tz)
    end = start + timedelta(days=1)
    current = summarize(await rows(conn, start.timestamp(), end.timestamp()))
    previous = summarize(await rows(conn, (start-timedelta(days=1)).timestamp(), start.timestamp()))
    from usage_activity import _pct_change, INPUT_NOTE
    report = {
        "day": day.isoformat(), "timezone": str(tz), "generated_at": now.isoformat(),
        **current, "previous": previous,
        "change_pct": _pct_change(current['total'], previous['total']),
        "providers": providers(await rows(conn, start.timestamp(), end.timestamp())),
        "note": INPUT_NOTE,
    }
    report['markdown'] = markdown(report)
    await conn.execute("INSERT OR IGNORE INTO usage_activity_reports VALUES (?, ?, ?, ?)",
                       (report['day'], report['timezone'], report['generated_at'], json.dumps(report)))
    async with conn.execute("SELECT payload FROM usage_activity_reports WHERE day=? AND timezone=?",
                            (report['day'], report['timezone'])) as cur:
        return json.loads((await cur.fetchone())[0])


async def report_history(conn, tz):
    async with conn.execute("SELECT payload FROM usage_activity_reports WHERE timezone=? ORDER BY day DESC LIMIT 31",
                            (str(tz),)) as cur:
        return [json.loads(row[0]) for row in await cur.fetchall()]


async def catch_up(conn, tz, now, options):
    if not options['enabled']:
        return
    # Before the configured hour, today's due report is still the day before yesterday.
    due = now.date() - timedelta(days=1 if now.hour >= options['hour'] else 2)
    async with conn.execute("SELECT day FROM usage_activity_schedule_cursor WHERE timezone=?", (str(tz),)) as cur:
        row = await cur.fetchone()
        last = row[0] if row else None
    first = datetime.fromisoformat(last).date() + timedelta(days=1) if last else due
    # Bound startup work; subsequent ticks continue catch-up after long downtime.
    for offset in range(min(7, max(0, (due-first).days+1))):
        day = first+timedelta(days=offset)
        await save_report(conn, day, tz, now)
        await conn.execute("INSERT OR REPLACE INTO usage_activity_schedule_cursor VALUES (?, ?)",
                           (str(tz), day.isoformat()))


def markdown(report):
    def count(value):
        return 'Unavailable' if value is None else f'{value:,}'
    change = report['change_pct']
    lines = [f"# My day in tokens — {report['day']}", "",
             f"Generated {report['generated_at']} · {report['timezone']}", "",
             f"Input: {count(report['input'])} · Output: {count(report['output'])} · Total: {count(report['total'])}",
             f"Turns: {report['turns']} · Counted turns: {report['counted_turns']} · Coverage: {report['coverage']}",
             f"Change from previous day: {change:+.1f}%" if change is not None else "Change from previous day: unavailable",
             "", "| Provider | Input | Output |", "|---|---:|---:|"]
    for p in report['providers']:
        name = p['provider'].replace('|', '\\|').replace('\n', ' ')
        lines.append(f"| {name} | {count(p['input'])} | {count(p['output'])} |")
    return '\n'.join(lines + ['', report['note'], ''])
