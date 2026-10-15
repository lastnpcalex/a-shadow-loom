"""Quota telemetry only: no prompts, threads, credentials, or raw errors in output."""
import asyncio
from copy import deepcopy
import json
import math
import sys
import time

from exo_state import state_path
from provider_quota_sources import (QuotaReadError, read_claude_quota, read_antigravity_quota,
                                    claude_quota_windows, antigravity_quota_windows)

POLL_SECONDS = 300
RETRY_SECONDS = 60
STALE_SECONDS = 900


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def window(label, used, reset=None, minutes=None):
    used = number(used)
    if used is None or not 0 <= used <= 100:
        return None
    reset, minutes = number(reset), number(minutes)
    return {"label": str(label)[:100], "used_percent": used,
            "resets_at": reset if reset is not None and reset > 0 else None,
            "window_minutes": minutes if minutes is not None and minutes > 0 else None}


def codex_windows(data):
    buckets = data.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        buckets = {"codex": data.get("rateLimits")}
    result = []
    for key, bucket in list(buckets.items())[:20]:
        if not isinstance(bucket, dict):
            continue
        for slot in ("primary", "secondary"):
            item = bucket.get(slot)
            if not isinstance(item, dict):
                continue
            minutes = number(item.get("windowDurationMins"))
            duration = {300: "5-hour", 10080: "Weekly"}.get(minutes, f"{minutes:g} min" if minutes else slot)
            row = window(f"{key}: {duration}", item.get("usedPercent"), item.get("resetsAt"), minutes)
            if row:
                result.append(row)
    return result


def claude_windows(data):
    rates = data.get("rate_limits")
    rates = rates if isinstance(rates, dict) else {}
    result = []
    for key, label, minutes in (("five_hour", "5-hour", 300), ("seven_day", "Weekly", 10080),
                                ("spend_limit", "Gateway spend", None)):
        item = rates.get(key)
        if isinstance(item, dict):
            row = window(label, item.get("used_percentage"), item.get("resets_at"), minutes)
            if row:
                result.append(row)
    return result


async def read_codex_quota():
    """Short control process. Never create a thread or invoke a model."""
    from codex_client import _find_codex_exe, cancel_codex
    proc = None
    try:
        async with asyncio.timeout(15):
            kwargs = {"creationflags": 0x08000000 | 0x00000200} if sys.platform == "win32" else {}
            proc = await asyncio.create_subprocess_exec(
                _find_codex_exe(), "app-server", "--stdio", "--disable", "hooks",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=1024 * 1024, **kwargs)

            async def send(payload):
                proc.stdin.write((json.dumps(payload) + "\n").encode())
                await proc.stdin.drain()

            async def request(rid, method, params):
                await send({"id": rid, "method": method, "params": params})
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        raise RuntimeError("Quota control process closed")
                    msg = json.loads(line)
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("id") == rid and "method" not in msg:
                        if "error" in msg or not isinstance(msg.get("result"), dict):
                            raise RuntimeError("Quota RPC unavailable")
                        return msg["result"]
                    # Decline unexpected server requests; never approve tools or supply auth.
                    if "method" in msg and "id" in msg:
                        await send({"id": msg["id"], "error": {"code": -32601, "message": "Unsupported monitor request"}})

            await request(1, "initialize", {"clientInfo": {"name": "exo-quota-monitor", "version": "1"}})
            await send({"method": "initialized"})
            return await request(2, "account/rateLimits/read", {})
    finally:
        if proc is not None:
            if proc.stdin:
                proc.stdin.close()
            await cancel_codex(proc)


def passive_claude(now):
    path = state_path("provider-usage", "claude.json")
    try:
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("Oversized cache")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Invalid cache")
        observed = number(data.get("observed_at"))
        if observed is None or observed <= 0 or observed > now + 60:
            raise ValueError("Invalid observation time")
        rows = claude_windows(data)
        return {"provider": "claude", "label": "Claude Code", "source": "Passive status-line cache",
                "status": "stale" if now - observed > STALE_SECONDS else "ok" if rows else "unavailable",
                "observed_at": observed, "windows": rows,
                "note": "Updates during configured interactive Claude sessions; may differ from Loom's account."}
    except (OSError, ValueError, TypeError):
        return {"provider": "claude", "label": "Claude Code", "source": "Passive status-line cache",
                "status": "unavailable", "observed_at": None, "windows": [],
                "note": "Optional status-line exporter is not reporting quota. See PROVIDER_USAGE.md."}


class UsageMonitor:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.last_attempt = None
        self.codex = None
        self.subscription = {}
        self.next_attempt = {}
        self.retry_seconds = {}

    async def refresh_subscription(self, provider, label, reader, normalize):
        previous = self.subscription.get(provider)
        self.retry_seconds[provider] = POLL_SECONDS
        try:
            rows = normalize(await reader())
            if not rows:
                raise QuotaReadError('Provider returned no subscription quota windows.')
            self.subscription[provider] = {'provider': provider, 'label': label, 'source': 'CLI OAuth quota endpoint',
                                           'status': 'ok', 'observed_at': time.time(), 'windows': rows,
                                           'note': 'Host CLI account. Shared across apps; no model turns are started.'}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.retry_seconds[provider] = getattr(exc, 'retry_seconds', RETRY_SECONDS)
            note = str(exc) if isinstance(exc, QuotaReadError) else 'Quota check failed; availability is unknown.'
            if previous and previous['windows']:
                self.subscription[provider] = {**previous, 'status': 'stale', 'note': note}
            elif provider == 'claude':
                fallback = passive_claude(time.time())
                self.subscription[provider] = {**fallback, 'note': note + ' ' + fallback['note']}
            else:
                self.subscription[provider] = {'provider': provider, 'label': label, 'source': 'CLI OAuth quota endpoint',
                                               'status': 'unavailable', 'observed_at': None, 'windows': [], 'note': note}

    async def refresh_codex(self):
        self.retry_seconds['codex'] = POLL_SECONDS
        try:
            data = await read_codex_quota()
            rows = codex_windows(data)
            if not rows:
                raise ValueError('No quota windows')
            self.codex = {'provider': 'codex', 'label': 'Codex', 'source': 'Account quota RPC',
                          'status': 'ok' if rows else 'unavailable', 'observed_at': time.time(),
                          'windows': rows, 'note': 'Host Codex login; API-key quotas are not subscription quotas.'}
        except asyncio.CancelledError:
            raise
        except Exception:
            self.retry_seconds['codex'] = RETRY_SECONDS
            if self.codex and self.codex['windows']:
                self.codex['status'] = 'stale'
                self.codex['note'] = 'Refresh failed. Last observation retained; availability is unknown.'
            else:
                self.codex = {'provider': 'codex', 'label': 'Codex', 'source': 'Account quota RPC',
                              'status': 'unavailable', 'observed_at': None, 'windows': [],
                              'note': 'Quota RPC unavailable. Check the installed CLI and ChatGPT login.'}

    async def snapshot(self):
        async with self.lock:
            now = time.time()
            checks = [('codex', self.refresh_codex),
                      ('claude', lambda: self.refresh_subscription('claude', 'Claude Code', read_claude_quota, claude_quota_windows)),
                      ('antigravity', lambda: self.refresh_subscription('antigravity', 'Antigravity', read_antigravity_quota, antigravity_quota_windows))]
            due = [(provider, check) for provider, check in checks if now >= self.next_attempt.get(provider, 0)]
            if due:
                self.last_attempt = now
                tasks = [asyncio.create_task(check()) for _, check in due]
                try:
                    await asyncio.gather(*tasks)
                    for provider, _ in due:
                        self.next_attempt[provider] = time.time() + self.retry_seconds[provider]
                except asyncio.CancelledError:
                    # An aborted first probe must not leave an empty cached provider.
                    self.last_attempt = None
                    raise
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            providers = deepcopy([self.codex, self.subscription['claude'], self.subscription['antigravity']])
            return {"providers": providers, "poll_seconds": POLL_SECONDS,
                    "stale_seconds": STALE_SECONDS, "warning_percent": 80, "critical_percent": 90}


monitor = UsageMonitor()
