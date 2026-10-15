# Subscription quota monitoring

The admin Overview shows account quota windows. The main UI shows a banner
at 80% and 90% consumed. These are informational warnings: they do not block
generation, switch providers, or infer that a provider will accept a request.
OpenRouter spend remains in its existing budget monitor.

Codex is queried through `initialize` / `initialized` /
`account/rateLimits/read`, using the host's installed CLI and existing login.
The probe never starts a thread or turn. A Main-owned lock coalesces browser
requests, with independent schedules for each provider. Successful checks are
cached for five minutes; transient failures retry after one minute without
repolling healthy providers. Authentication rejections and throttling wait five
minutes. Refresh buttons obey those schedules. Each probe has a 15-second
deadline and process-tree cleanup.
Window labels use the returned duration; a primary window is not assumed to
be five hours. Raw RPC errors, account identifiers, and credentials are omitted.

Claude is now queried automatically using `GET https://api.anthropic.com/api/oauth/usage`
and the access token in the installed CLI's `.claude/.credentials.json`
(`CLAUDE_CONFIG_DIR` is respected). This reports five-hour and shared weekly
windows, plus Sonnet/Opus windows when the provider supplies them. No status-line
configuration or interactive session is required for these quota reads.

Antigravity is queried automatically through its installed CLI's Cloud Code
`retrieveUserQuotaSummary` endpoint. On Windows, the current login is read from
the native Credential Manager entry `gemini:antigravity`. The legacy file
`.gemini/antigravity-cli/antigravity-oauth-token` is used only when that entry
does not exist (or on other platforms). An inaccessible or invalid native
entry does not silently select a different legacy account. Expired logins with
a refresh token are renewed through the installed CLI's `agy models` command,
then reread from their native store. This command starts no model turn; output
is discarded and the process is bounded and cleaned up. The daily host is tried first,
then the standard host for unavailable responses. Authentication failures and
429 throttling stop the check rather than triggering another request. The
provider's four identified buckets are projected into the same window format:
five-hour and weekly limits for Gemini models and for Claude/GPT models.
Both windows appear in each AGY/ExoGoo card and have independent percentages,
reset times, and warnings. Missing or invalid buckets are omitted rather than
represented as zero usage; durations come from bucket identities, never reset
countdowns. No per-model allowances are summed or inferred.

Both OAuth routes were verified live on this Windows installation without
generating a response. These are internal provider endpoints and can change.
Only access tokens are used, only fixed provider hosts receive them, redirects
are disabled, and neither credentials nor raw API responses are exposed in
the browser. Credentials are reread each scheduled check; the monitor leaves
credential storage to the native CLIs and requests native AGY renewal when
expiry is known. If credentials cannot be renewed or
access is rejected, the previous quota is marked stale with a login-refresh
message. Credential files are never rewritten by the monitor.

Claude's earlier passive exporter remains an optional fallback if the OAuth
quota read is unavailable and no prior direct observation exists. To enable
that fallback, configure this command in an interactive Claude Code session's
`statusLine` setting (use your checkout's actual absolute path):

```json
{
  "statusLine": {
    "type": "command",
    "command": "C:/Python314/python.exe C:/Users/USERNAME/OneDrive/Documents/Loom2/a-shadow-loom/tools/claude_usage_statusline.py"
  }
}
```

This is an optional configuration example; installation is not automatic.
If you have a status-line command already, preserve it and call this exporter
with the same stdin from your existing script. The exporter prints a compact
quota status and atomically saves only percentages, resets, and observation
time to `provider-usage/claude.json` under EXO's external machine-state root
(`%LOCALAPPDATA%/A Shadow Loom` on Windows, or `EXO_STATE_DIR` when set).
The interactive CLI and Main must share this root. The sample command replaces
the displayed status line if configured directly.

Claude quota fields can be independently absent and are reported only after
an ordinary response in eligible sessions. The exporter itself makes no network
or model calls. This account may differ from the account used by Loom; quota
data is host-wide, not conversation-specific. No per-model weekly allowance
is invented when the status line only reports the shared weekly window.
Antigravity quota collection does not depend on Stop hooks or `agy -p /usage`.

Observations older than 15 minutes, failed refreshes, and passed reset times
are displayed as historical data. They never imply a restored quota or trigger
a current high-usage warning. Unknown data is not represented as zero usage.
The main banner names only providers with uncertain quota data; their failure
does not invalidate another provider's current observation or usage warning.
Detailed source diagnostics remain in Admin Overview.
Visible pages check once a minute; hidden pages pause requests. All tabs share
Main's quota cache. No monitoring daemon runs while browsers are closed.

Run the final integrated zero-turn acceptance probe with:

```powershell
C:/Python314/python.exe tools/provider_quota_probe.py
```

It prints only normalized monitor data and exits nonzero if any provider fails
to return current quota windows. Automated tests mock all provider traffic.

Sources:

- [Codex app-server account quota RPC](https://developers.openai.com/codex/app-server)
- [Claude Code status-line quota fields](https://code.claude.com/docs/en/statusline)
- [Antigravity Stop hook schema](https://www.antigravity.google/docs/hooks)
- [Antigravity native keyring authentication](https://www.antigravity.google/docs/cli/install/)
- The installed Claude and AGY binaries, their credential JSON schemas, and
  live quota-only HTTP responses were inspected to verify the active collectors.

After updating server code, Main and Admin need a human-controlled reload
through the admin UI. Agents must not restart their own Loom host.
