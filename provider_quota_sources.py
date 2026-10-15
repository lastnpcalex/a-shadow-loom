"""Authenticated, zero-generation quota readers for installed Claude and AGY.

Only fixed provider endpoints receive access tokens. Normal CLI login/refresh
owns credential storage; AGY's zero-turn models command renews expired logins.
No secrets or provider error bodies
are returned, logged, or saved by this module.
"""
import asyncio
from datetime import datetime
import json
import math
import os
from pathlib import Path
import sys
import time

import httpx


class QuotaReadError(RuntimeError):
    """Fixed, safe diagnostic suitable for display in the monitor."""

    def __init__(self, message, *, retry_seconds=60):
        super().__init__(message)
        self.retry_seconds = retry_seconds


def finite(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def timestamp(value):
    numeric = finite(value)
    if numeric is not None:
        return numeric if numeric > 0 else None
    if isinstance(value, str):
        try:
            date = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return date.timestamp() if date.tzinfo else None
        except (ValueError, OverflowError, OSError):
            pass
    return None


def credential_json(path):
    try:
        with path.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (OSError, ValueError):
        raise QuotaReadError('Local CLI OAuth credentials are missing or unreadable.') from None


def antigravity_keyring():
    """Read AGY's current Windows login; never enumerate or modify credentials."""
    if sys.platform != 'win32':
        return None
    import ctypes
    from ctypes import wintypes

    class Credential(ctypes.Structure):
        _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
                    ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
                    ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                    ('CredentialBlob', ctypes.POINTER(ctypes.c_byte)), ('Persist', wintypes.DWORD),
                    ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                    ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]

    api = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.POINTER(ctypes.POINTER(Credential))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    record = ctypes.POINTER(Credential)()
    if not api.CredReadW('gemini:antigravity', 1, 0, ctypes.byref(record)):
        if ctypes.get_last_error() == 1168:  # No entry: older CLI file storage.
            return None
        raise QuotaReadError('Antigravity native credential store is unavailable to this process.')
    try:
        if not 0 < record.contents.CredentialBlobSize <= 65536:
            raise ValueError()
        data = json.loads(ctypes.string_at(record.contents.CredentialBlob,
                                          record.contents.CredentialBlobSize))
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, UnicodeError):
        raise QuotaReadError('Antigravity native login is unreadable. Sign in with the provider CLI.') from None
    finally:
        api.CredFree(record)


def antigravity_profile():
    profile = antigravity_keyring()
    if profile is None:
        home = Path(os.environ.get('USERPROFILE') or Path.home())
        profile = credential_json(home / '.gemini' / 'antigravity-cli' / 'antigravity-oauth-token')
    return profile


def token_value(data, field):
    token = data.get(field) if isinstance(data, dict) else None
    if not isinstance(token, str) or not token.strip() or '\n' in token or '\r' in token:
        raise QuotaReadError('Local CLI OAuth access token is unavailable. Sign in with the provider CLI.', retry_seconds=300)
    return token


def access_token(provider):
    home = Path(os.environ.get('USERPROFILE') or Path.home())
    if provider == 'claude':
        override = os.environ.get('CLAUDE_CONFIG_DIR', '').strip()
        root = Path(override).expanduser() if override else home / '.claude'
        data = credential_json(root / '.credentials.json').get('claudeAiOauth', {})
        return token_value(data, 'accessToken')
    else:
        return token_value(antigravity_profile().get('token', {}), 'access_token')


async def renew_antigravity_login():
    """Let the native, zero-turn model-list command renew its own saved login."""
    from gemini_client import _find_agy_exe, cancel_gemini
    proc = None
    try:
        async with asyncio.timeout(8):
            kwargs = {'creationflags': 0x08000000 | 0x00000200} if sys.platform == 'win32' else {}
            proc = await asyncio.create_subprocess_exec(
                _find_agy_exe(), 'models', stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, **kwargs)
            if await proc.wait() != 0:
                raise QuotaReadError('Antigravity CLI could not renew its login. Sign in with the provider CLI.', retry_seconds=300)
    except (OSError, TimeoutError):
        raise QuotaReadError('Antigravity CLI login renewal is unavailable.', retry_seconds=300) from None
    finally:
        if proc is not None:
            await cancel_gemini(proc)


async def antigravity_access_token():
    profile = antigravity_profile()
    data = profile.get('token', {})
    token = token_value(data, 'access_token')
    expires = timestamp(data.get('expiry'))
    if expires is not None and expires <= time.time() + 60:
        if not isinstance(data.get('refresh_token'), str) or not data['refresh_token'].strip():
            raise QuotaReadError('Antigravity CLI login expired. Sign in with the provider CLI.', retry_seconds=300)
        await renew_antigravity_login()
        profile = antigravity_profile()
        data = profile.get('token', {})
        token = token_value(data, 'access_token')
        expires = timestamp(data.get('expiry'))
        if expires is not None and expires <= time.time():
            raise QuotaReadError('Antigravity CLI login is still expired after renewal.', retry_seconds=300)
    return token


async def request_json(client, method, url, token, *, provider):
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json',
               'User-Agent': 'claude-code' if provider == 'claude' else 'antigravity'}
    if provider == 'claude':
        headers['anthropic-beta'] = 'oauth-2025-04-20'
    try:
        async with client.stream(method, url, headers=headers, **({'json': {}} if method == 'POST' else {})) as response:
            if response.status_code in (401, 403):
                raise QuotaReadError('CLI OAuth access was rejected or expired. Refresh the provider CLI login.', retry_seconds=300)
            if response.status_code == 429:
                raise QuotaReadError('Quota service throttled this check; retry at the next scheduled refresh.', retry_seconds=300)
            if response.status_code != 200:
                raise QuotaReadError('Provider quota endpoint is unavailable.')
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 1024 * 1024:
                    raise QuotaReadError('Provider quota response exceeded the size limit.')
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise QuotaReadError('Provider returned an invalid quota response.')
            return data
    except (httpx.HTTPError, ValueError):
        raise QuotaReadError('Provider quota request failed or returned invalid data.') from None


async def read_claude_quota():
    token = access_token('claude')
    async with asyncio.timeout(15), httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False) as client:
        return await request_json(client, 'GET', 'https://api.anthropic.com/api/oauth/usage', token, provider='claude')


def claude_quota_windows(data):
    result = []
    for key, label, minutes in (('five_hour', '5-hour', 300), ('seven_day', 'Weekly', 10080),
                                ('seven_day_sonnet', 'Weekly Sonnet', 10080), ('seven_day_opus', 'Weekly Opus', 10080),
                                ('seven_day_oauth_apps', 'Weekly OAuth apps', 10080), ('extra_usage', 'Extra usage', None)):
        item = data.get(key)
        if not isinstance(item, dict) or (key == 'extra_usage' and item.get('is_enabled') is not True):
            continue
        used = finite(item.get('utilization'))
        if used is not None and 0 <= used <= 100:
            result.append({'label': label, 'used_percent': used, 'resets_at': timestamp(item.get('resets_at')),
                           'window_minutes': minutes})
    return result


async def read_antigravity_quota():
    # These are the installed CLI's Cloud Code hosts, not configurable destinations.
    async with asyncio.timeout(15), httpx.AsyncClient(timeout=6, follow_redirects=False, trust_env=False) as client:
        token = await antigravity_access_token()
        for base in ('https://daily-cloudcode-pa.googleapis.com', 'https://cloudcode-pa.googleapis.com'):
            try:
                data = await request_json(client, 'POST', base + '/v1internal:retrieveUserQuotaSummary',
                                          token, provider='antigravity')
                if antigravity_quota_windows(data):
                    return data
            except QuotaReadError as exc:
                # Authentication and throttling are not reasons to hit another host.
                if 'rejected' in str(exc) or 'throttled' in str(exc):
                    raise
        raise QuotaReadError('Antigravity quota summary is unavailable.')


def antigravity_quota_windows(data):
    # The summary identifies both windows explicitly. A reset countdown cannot
    # identify a window: even a weekly bucket can reset less than five hours away.
    if 'groups' in data:
        groups = data.get('groups')
        if not isinstance(groups, list):
            return []
        result, seen = [], set()
        buckets_by_id = {
            'gemini-5h': ('google', 'Gemini Models', '5h', '5-hour', 300),
            'gemini-weekly': ('google', 'Gemini Models', 'weekly', 'Weekly', 10080),
            '3p-5h': ('third_party', 'Claude and GPT models', '5h', '5-hour', 300),
            '3p-weekly': ('third_party', 'Claude and GPT models', 'weekly', 'Weekly', 10080),
        }
        for group in groups[:20]:
            buckets = group.get('buckets') if isinstance(group, dict) else None
            if not isinstance(buckets, list):
                continue
            for bucket in buckets[:20]:
                if not isinstance(bucket, dict):
                    continue
                bucket_id = bucket.get('bucketId')
                spec = buckets_by_id.get(bucket_id) if isinstance(bucket_id, str) else None
                if spec is None or bucket_id in seen:
                    continue
                pool, label, period, duration, minutes = spec
                fraction = finite(bucket.get('remainingFraction'))
                if bucket.get('window') != period or fraction is None or not 0 <= fraction <= 1:
                    continue
                seen.add(bucket_id)
                result.append({'label': f'{label}: {duration}', 'pool': pool,
                               'used_percent': round((1 - fraction) * 100, 4),
                               'resets_at': timestamp(bucket.get('resetTime')), 'window_minutes': minutes})
        return sorted(result, key=lambda row: (row['pool'], row['window_minutes']))
    # Compatibility with prior observations; these carry no window identity.
    models = data.get('models')
    if not isinstance(models, dict):
        return []
    result = []
    seen = set()
    for key, item in list(models.items())[:200]:
        if not isinstance(item, dict) or item.get('isInternal') is True:
            continue
        label = item.get('displayName')
        quota = item.get('quotaInfo')
        if not isinstance(label, str) or not label.strip() or not isinstance(quota, dict):
            continue
        fraction = finite(quota.get('remainingFraction'))
        if fraction is None or not 0 <= fraction <= 1:
            continue
        used, reset = round((1 - fraction) * 100, 4), timestamp(quota.get('resetTime'))
        identity = (label, used, reset)
        if identity not in seen:
            seen.add(identity)
            result.append({'label': label[:100], 'used_percent': used, 'resets_at': reset, 'window_minutes': None})
    return result
