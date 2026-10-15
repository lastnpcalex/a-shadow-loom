# Independent Admin access

Admin uses a separate password and standard six-digit TOTP compatible with
Authy. Main's legacy browser pairing remains disabled. A Main or portal login
does not authorize Admin. The exoloom gateway continues targeting Main only;
its existing `/admin` and `/api/admin` path blocks also cover Admin auth APIs.
No portal bridge or connector is enabled by this change.

## Enable once on the host

1. Reload **Admin only** using its existing human-operated restart control.
   The running process does not hot-load Python middleware. Agents must not
   perform this lifecycle operation from inside Loom.
2. On the host, open `https://localhost:3002/login` (use your actual Admin port
   if customized). Choose a unique password of at least 12 characters.
3. Scan the displayed QR in Authy, or enter the displayed setup key manually.
   Submit a current six-digit code to verify enrollment.
4. Save the eight recovery codes in your password manager or offline, then
   acknowledge saving them and choose **Enable Admin login**. Activation creates
   a session for the setup browser before it applies the login requirement.
   Passwords, seeds and recovery codes belong in that private UI, never chat.

Before enrollment, only direct local access can use the dashboard. Remote
Admin operations are denied; a remote visitor sees enrollment instructions.
Pending enrollment expires after ten minutes and cannot replace active
credentials. Existing launcher `LOOM_AUTH=off` or `LOOM_NETWORK_GATE=off`
overrides do not disable this independent Admin gate.

## Remote use and travel test

Enable Tailscale on the laptop or phone and open
`https://<the host's Tailscale IP or MagicDNS name>:3002/` directly. Sign in with
the Admin password and an Authy code. Retain the current HTTPS certificate
trust setup on each device; do not bypass certificate warnings with credentials.

Remembered Admin browser sessions persist through process restarts for up to
30 days, with a seven-day inactivity limit. You can list/revoke browsers and
sign out under **Overview → Admin access**. A new browser can authenticate
independently without a pairing code or approval from someone at the host.

Before traveling, test a fresh/incognito browser over Tailscale, then test one
saved recovery code in place of Authy, **with the password**. Each recovery code
works once. The same Authy timestep also works only once: wait for the next
code if you have just used it during setup or another login. Recovery codes
can be replaced remotely from a signed-in Admin browser using the password
and a fresh Authy code; save the new set because the old set becomes invalid.

Recovery codes replace an unavailable Authy factor, not a forgotten password
or lost Tailscale access. Keep the password and recovery material accessible
independently of your phone. Losing every credential still requires local
maintenance on the host; no unauthenticated remote reset is provided.

## Boundaries and storage

- Admin requires HTTPS and real peer loopback/Tailscale addresses. LAN/public
  peers, proxy forwarding headers and the exoloom hostname are rejected.
  The gate uses the existing Tailscale address ranges; it is not a MAC/IMEI
  whitelist or cryptographic device attestation. Maintain Tailscale ACLs and
  host firewall restrictions on port 3002; don't publicly forward that port.
- Browser mutations require a matching Origin. Auth endpoints accept only
  the exact Admin origin. Existing Main instance pages on the same hostname
  and registered instance ports may use the authenticated Admin cookie for
  existing cross-port controls. They must first sign in through Admin directly.
- Local no-Origin machine relays are restricted to the existing read-only
  activity/status routes. The machine relay secret cannot authorize Admin
  lifecycle or general dashboard controls. Main's local no-cookie health
  probe receives only `{"up":true}`, without instance/process metadata.
- Admin's cookie is separate, Secure, HttpOnly and SameSite=Strict. Session
  revocation is checked for HTTP and incoming WebSocket messages. A password
  and factor are necessary for each new session; a cookie is retained so you
  do not need to authenticate on every dashboard request.
- Credential/session state lives at
  `%LOCALAPPDATA%\A Shadow Loom\admin-auth\credentials.sqlite3`, outside the
  checkout and OneDrive (unless the operator changes `EXO_STATE_DIR`). Passwords
  use salted scrypt (`N=2^17,r=8,p=1`); session/recovery tokens are SHA-256 hashes.
  Windows DPAPI protects the TOTP seed, and directory inheritance is removed
  in favor of the current account, SYSTEM and Administrators. On non-Windows
  systems the owner-only filesystem permissions protect the seed.
- DPAPI and ACLs do not isolate secrets from an agent running as the same
  Windows account, or from machine administrators. That requires separate OS
  identities/process privileges. The enrollment QR is generated locally;
  no external QR service, authenticator provider API or seed logging is used.
- Failed password/factor attempts are bounded across restarts to ten attempts
  per five-minute window. Existing authenticated sessions are not locked out
  by unsuccessful login attempts. Authy/recovery consumption is transactional
  to prevent concurrent reuse.

## Validation

`C:\Python314\python.exe -m pytest tests/test_admin_auth.py tests/test_loom_auth.py -q`
tests isolated stores, real Chromium enrollment/recovery login, TOTP replay,
concurrent recovery reuse, origin/network/proxy denial, independent real Admin
routes, session expiry/revocation and WebSocket revocation. Existing Admin route
tests keep their previous HTTP-only fixture gate; they do not substitute for
the independent authentication acceptance tests.

References: [OWASP password storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html),
[RFC 6238](https://www.rfc-editor.org/rfc/rfc6238),
[Windows DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata).
