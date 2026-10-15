"""Isolated OAuth readers: bounded requests, fixed hosts, sanitized projections."""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

import provider_quota_sources as sources
import provider_usage as usage


def claude_data():
    return {'five_hour': {'utilization': 27, 'resets_at': '2026-10-05T01:30:00Z'},
            'seven_day': {'utilization': 99, 'resets_at': '2026-10-07T17:00:00+00:00'},
            'seven_day_opus': None, 'email': 'PRIVATE_FIXTURE'}


def agy_data():
    return {'models': {'gemini-test': {'displayName': 'Gemini Test', 'quotaInfo': {
        'remainingFraction': .7721997, 'resetTime': '2026-10-05T01:30:57Z'}},
        'internal': {'quotaInfo': {'remainingFraction': 1}},
        'hidden': {'isInternal': True, 'displayName': 'Internal', 'quotaInfo': {'remainingFraction': 0}},
        'claude-test': {'displayName': 'Claude Test', 'quotaInfo': {'remainingFraction': 0}},
        'unknown': {'displayName': 'Unknown', 'quotaInfo': {}}}, 'email': 'PRIVATE_FIXTURE'}


def agy_summary():
    return {'groups': [{'displayName': 'Gemini Models', 'buckets': [
        {'bucketId': 'gemini-weekly', 'window': 'weekly', 'remainingFraction': .1,
         'resetTime': '2026-10-05T01:30:57Z'},
        {'bucketId': 'gemini-5h', 'window': '5h', 'remainingFraction': .8,
         'resetTime': '2026-10-05T01:30:00Z'}]},
        {'displayName': 'Claude and GPT models', 'buckets': [
        {'bucketId': '3p-weekly', 'window': 'weekly', 'remainingFraction': .49},
        {'bucketId': '3p-5h', 'window': '5h', 'remainingFraction': 1}]}],
        'private': 'PRIVATE_FIXTURE'}


def test_agy_summary_keeps_four_independent_windows_and_provider_resets():
    rows = sources.antigravity_quota_windows(agy_summary())
    assert [row['used_percent'] for row in rows] == [20, 90, 0, 51]
    assert [row['window_minutes'] for row in rows] == [300, 10080, 300, 10080]
    assert [row['pool'] for row in rows] == ['google', 'google', 'third_party', 'third_party']
    assert rows[1]['label'] == 'Gemini Models: Weekly'
    assert rows[1]['resets_at'] == 1791163857
    assert rows[2]['resets_at'] is None
    assert 'PRIVATE_FIXTURE' not in json.dumps(rows)


@pytest.mark.parametrize('value', [None, True, '.5', float('nan'), -1, 1.1])
def test_agy_summary_missing_invalid_or_disabled_fraction_is_not_zero(value):
    data = agy_summary()
    data['groups'][0]['buckets'][0]['remainingFraction'] = value
    rows = sources.antigravity_quota_windows(data)
    assert len(rows) == 3
    assert not any(row['label'] == 'Gemini Models: Weekly' for row in rows)


def test_agy_summary_rejects_unknown_mismatched_duplicate_and_malformed_buckets():
    data = agy_summary()
    buckets = data['groups'][0]['buckets']
    buckets.extend([buckets[0].copy(), {'bucketId': 'unknown', 'window': '5h', 'remainingFraction': 1},
                    {'bucketId': [], 'remainingFraction': 1}, None])
    assert len(sources.antigravity_quota_windows(data)) == 4
    buckets[1]['window'] = 'weekly'
    assert len(sources.antigravity_quota_windows(data)) == 3
    assert sources.antigravity_quota_windows({'groups': None, **agy_data()}) == []
    assert sources.antigravity_quota_windows({'groups': [None, {'buckets': None}]}) == []


def test_percent_fraction_and_reset_normalization():
    claude = sources.claude_quota_windows(claude_data())
    agy = sources.antigravity_quota_windows(agy_data())
    assert [item['used_percent'] for item in claude] == [27, 99]
    assert claude[0]['resets_at'] == 1791163800
    assert [item['label'] for item in agy] == ['Gemini Test', 'Claude Test']
    assert [item['used_percent'] for item in agy] == [22.78, 100]
    assert agy[1]['resets_at'] is None
    assert 'PRIVATE_FIXTURE' not in json.dumps([claude, agy])
    assert sources.timestamp('2026-10-05T01:30:00') is None


@pytest.mark.parametrize('value', [None, True, '90', float('nan'), -1, 101])
def test_claude_invalid_usage_never_becomes_zero(value):
    assert sources.claude_quota_windows({'five_hour': {'utilization': value}}) == []


@pytest.mark.parametrize('value', [None, True, '.5', float('inf'), -.1, 1.1])
def test_agy_invalid_fraction_never_becomes_available(value):
    assert sources.antigravity_quota_windows({'models': {'a': {'displayName': 'Test',
                                                             'quotaInfo': {'remainingFraction': value}}}}) == []


def test_local_credentials_only_expected_files_and_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(sources, 'antigravity_keyring', lambda: None)
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    claude = tmp_path / '.claude' / '.credentials.json'
    claude.parent.mkdir()
    claude.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'CLAUDE_FIXTURE_TOKEN'}, 'token': 'IGNORE'}))
    agy = tmp_path / '.gemini' / 'antigravity-cli' / 'antigravity-oauth-token'
    agy.parent.mkdir(parents=True)
    agy.write_text(json.dumps({'token': {'access_token': 'AGY_FIXTURE_TOKEN'}}))
    assert sources.access_token('claude') == 'CLAUDE_FIXTURE_TOKEN'
    assert sources.access_token('antigravity') == 'AGY_FIXTURE_TOKEN'
    claude.write_text('invalid PRIVATE_FIXTURE')
    with pytest.raises(sources.QuotaReadError) as error:
        sources.access_token('claude')
    assert 'PRIVATE_FIXTURE' not in str(error.value)
    assert agy.read_text().find('AGY_FIXTURE_TOKEN') >= 0


def client_factory(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(sources.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


async def test_readers_fixed_endpoints_no_generation_or_credential_writes(monkeypatch):
    requests = []
    monkeypatch.setattr(sources, 'access_token', lambda provider: 'FIXTURE_TOKEN')
    monkeypatch.setattr(sources, 'antigravity_access_token', AsyncMock(return_value='FIXTURE_TOKEN'))

    def handler(request):
        requests.append(request)
        assert request.headers['authorization'] == 'Bearer FIXTURE_TOKEN'
        if request.url.host == 'api.anthropic.com':
            assert request.method == 'GET' and request.url.path == '/api/oauth/usage'
            assert request.headers['anthropic-beta'] == 'oauth-2025-04-20'
            return httpx.Response(200, json=claude_data())
        assert request.method == 'POST'
        assert request.url.host == 'daily-cloudcode-pa.googleapis.com'
        assert request.url.path == '/v1internal:retrieveUserQuotaSummary'
        assert json.loads(request.content) == {}
        return httpx.Response(200, json=agy_summary())

    client_factory(monkeypatch, handler)
    assert sources.claude_quota_windows(await sources.read_claude_quota())[1]['used_percent'] == 99
    assert sources.antigravity_quota_windows(await sources.read_antigravity_quota())[0]['used_percent'] == 20
    assert len(requests) == 2


@pytest.mark.parametrize('status, note', [(401, 'rejected'), (403, 'rejected'), (429, 'throttled'), (302, 'unavailable')])
async def test_auth_throttle_redirect_never_leaks_body_or_follows(monkeypatch, status, note):
    calls = []
    monkeypatch.setattr(sources, 'access_token', lambda provider: 'PRIVATE_FIXTURE')

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text='PRIVATE_FIXTURE', headers={'Location': 'https://other.invalid/'})

    client_factory(monkeypatch, handler)
    with pytest.raises(sources.QuotaReadError) as error:
        await sources.read_claude_quota()
    assert note in str(error.value) and 'PRIVATE_FIXTURE' not in str(error.value)
    assert len(calls) == 1


async def test_agy_fallback_uses_only_other_provider_host(monkeypatch):
    calls = []
    monkeypatch.setattr(sources, 'access_token', lambda provider: 'FIXTURE_TOKEN')
    monkeypatch.setattr(sources, 'antigravity_access_token', AsyncMock(return_value='FIXTURE_TOKEN'))

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json=agy_summary())

    client_factory(monkeypatch, handler)
    assert sources.antigravity_quota_windows(await sources.read_antigravity_quota())
    assert calls == ['daily-cloudcode-pa.googleapis.com', 'cloudcode-pa.googleapis.com']


@pytest.mark.parametrize('status', [401, 403, 429])
async def test_agy_auth_or_throttle_does_not_probe_second_host(monkeypatch, status):
    calls = []
    monkeypatch.setattr(sources, 'access_token', lambda provider: 'FIXTURE_TOKEN')
    monkeypatch.setattr(sources, 'antigravity_access_token', AsyncMock(return_value='FIXTURE_TOKEN'))

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text='PRIVATE_FIXTURE')

    client_factory(monkeypatch, handler)
    with pytest.raises(sources.QuotaReadError):
        await sources.read_antigravity_quota()
    assert len(calls) == 1


async def test_all_collectors_cached_isolated_failure_and_recovery(monkeypatch):
    clock = [1000]
    monkeypatch.setattr(usage.time, 'time', lambda: clock[0])
    codex = AsyncMock(return_value={'rateLimits': {'primary': {'usedPercent': 18}}})
    claude = AsyncMock(return_value=claude_data())
    agy = AsyncMock(return_value=agy_data())
    for name, reader in [('read_codex_quota', codex), ('read_claude_quota', claude), ('read_antigravity_quota', agy)]:
        monkeypatch.setattr(usage, name, reader)
    monitor = usage.UsageMonitor()
    results = await asyncio.gather(*(monitor.snapshot() for _ in range(5)))
    assert [item['provider'] for item in results[0]['providers']] == ['codex', 'claude', 'antigravity']
    assert all(reader.await_count == 1 for reader in (codex, claude, agy))
    clock[0] += 301
    claude.side_effect = RuntimeError('PRIVATE_FIXTURE')
    agy.return_value = {'models': {'fresh': {'displayName': 'Fresh', 'quotaInfo': {'remainingFraction': 1}}}}
    result = await monitor.snapshot()
    assert result['providers'][1]['status'] == 'stale'
    assert result['providers'][1]['windows'][1]['used_percent'] == 99
    assert result['providers'][2]['status'] == 'ok'
    assert result['providers'][2]['windows'][0]['used_percent'] == 0
    assert 'PRIVATE_FIXTURE' not in json.dumps(result)
    clock[0] += 301
    claude.side_effect = None
    assert (await monitor.snapshot())['providers'][1]['status'] == 'ok'


def test_agy_native_login_wins_over_expired_legacy_file(monkeypatch):
    monkeypatch.setattr(sources, 'antigravity_keyring', lambda: {'token': {'access_token': 'CURRENT_FIXTURE'}})
    monkeypatch.setattr(sources, 'credential_json', lambda path: pytest.fail('Must not read legacy login'))
    assert sources.access_token('antigravity') == 'CURRENT_FIXTURE'


def test_agy_native_store_error_does_not_select_another_account(monkeypatch):
    def unavailable():
        raise sources.QuotaReadError('Native store unavailable')
    monkeypatch.setattr(sources, 'antigravity_keyring', unavailable)
    monkeypatch.setattr(sources, 'credential_json', lambda path: pytest.fail('Must not fall back on store error'))
    with pytest.raises(sources.QuotaReadError, match='Native store unavailable'):
        sources.access_token('antigravity')


async def test_agy_expired_login_renewed_natively_and_reread(monkeypatch):
    profiles = iter([{'token': {'access_token': 'OLD_FIXTURE', 'expiry': 999, 'refresh_token': 'REFRESH_FIXTURE'}},
                     {'token': {'access_token': 'NEW_FIXTURE', 'expiry': 5000}}])
    monkeypatch.setattr(sources.time, 'time', lambda: 1000)
    monkeypatch.setattr(sources, 'antigravity_profile', lambda: next(profiles))
    renew = AsyncMock()
    monkeypatch.setattr(sources, 'renew_antigravity_login', renew)
    assert await sources.antigravity_access_token() == 'NEW_FIXTURE'
    renew.assert_awaited_once()


async def test_agy_fresh_login_does_not_launch_cli(monkeypatch):
    monkeypatch.setattr(sources.time, 'time', lambda: 1000)
    monkeypatch.setattr(sources, 'antigravity_profile', lambda: {'token': {'access_token': 'FRESH_FIXTURE', 'expiry': 5000}})
    renew = AsyncMock()
    monkeypatch.setattr(sources, 'renew_antigravity_login', renew)
    assert await sources.antigravity_access_token() == 'FRESH_FIXTURE'
    renew.assert_not_awaited()


async def test_agy_failed_renewal_does_not_return_expired_token(monkeypatch):
    monkeypatch.setattr(sources.time, 'time', lambda: 1000)
    monkeypatch.setattr(sources, 'antigravity_profile', lambda: {
        'token': {'access_token': 'PRIVATE_FIXTURE', 'expiry': 999, 'refresh_token': 'REFRESH_FIXTURE'}})
    monkeypatch.setattr(sources, 'renew_antigravity_login', AsyncMock())
    with pytest.raises(sources.QuotaReadError, match='still expired') as error:
        await sources.antigravity_access_token()
    assert 'PRIVATE_FIXTURE' not in str(error.value)


@pytest.mark.parametrize('cancelled', [False, True])
async def test_native_renewal_only_lists_models_and_always_cleans_up(monkeypatch, cancelled):
    import gemini_client
    proc = type('Proc', (), {'wait': AsyncMock(side_effect=asyncio.CancelledError() if cancelled else None,
                                              return_value=0)})()
    launch = AsyncMock(return_value=proc)
    cleanup = AsyncMock()
    monkeypatch.setattr(sources.asyncio, 'create_subprocess_exec', launch)
    monkeypatch.setattr(gemini_client, '_find_agy_exe', lambda: 'fixture-agy')
    monkeypatch.setattr(gemini_client, 'cancel_gemini', cleanup)
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await sources.renew_antigravity_login()
    else:
        await sources.renew_antigravity_login()
    assert launch.call_args.args == ('fixture-agy', 'models')
    assert all(launch.call_args.kwargs[key] == asyncio.subprocess.DEVNULL for key in ('stdin', 'stdout', 'stderr'))
    cleanup.assert_awaited_once_with(proc)


async def test_failed_provider_retries_without_repolling_healthy_providers(monkeypatch):
    clock = [1000]
    monkeypatch.setattr(usage.time, 'time', lambda: clock[0])
    codex = AsyncMock(return_value={'rateLimits': {'primary': {'usedPercent': 18}}})
    claude = AsyncMock(return_value=claude_data())
    agy = AsyncMock(side_effect=sources.QuotaReadError('Temporary endpoint failure'))
    for name, reader in [('read_codex_quota', codex), ('read_claude_quota', claude), ('read_antigravity_quota', agy)]:
        monkeypatch.setattr(usage, name, reader)
    monitor = usage.UsageMonitor()
    await monitor.snapshot()
    clock[0] += 59
    await monitor.snapshot()
    assert agy.await_count == 1
    clock[0] += 1
    agy.side_effect = None
    agy.return_value = agy_data()
    result = await monitor.snapshot()
    assert agy.await_count == 2
    assert codex.await_count == claude.await_count == 1
    assert result['providers'][2]['status'] == 'ok'


async def test_throttled_provider_waits_five_minutes(monkeypatch):
    clock = [1000]
    monkeypatch.setattr(usage.time, 'time', lambda: clock[0])
    monkeypatch.setattr(usage, 'read_codex_quota', AsyncMock(return_value={'rateLimits': {'primary': {'usedPercent': 18}}}))
    monkeypatch.setattr(usage, 'read_claude_quota', AsyncMock(return_value=claude_data()))
    agy = AsyncMock(side_effect=sources.QuotaReadError('Throttled', retry_seconds=300))
    monkeypatch.setattr(usage, 'read_antigravity_quota', agy)
    monitor = usage.UsageMonitor()
    await monitor.snapshot()
    clock[0] += 60
    await monitor.snapshot()
    assert agy.await_count == 1
    clock[0] += 240
    await monitor.snapshot()
    assert agy.await_count == 2
