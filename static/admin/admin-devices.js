// Browser sessions, one-time pairing and local recovery provisioning.
(function () {
    const btn = document.getElementById('btn-pair-device');
    const out = document.getElementById('pair-device-output');
    if (!btn || !out) return;
    let localAccess = false;
    const sessions = document.getElementById('browser-sessions');
    const recoveryButton = document.getElementById('btn-recovery-codes');
    const recoveryOutput = document.getElementById('recovery-code-output');
    const logoutButton = document.getElementById('btn-browser-logout');
    const card = document.getElementById('devices-card');
    if (card) {
        card.hidden = true;
        fetch('/api/auth/status', { cache: 'no-store' })
            .then(r => r.json()).then(s => {
                card.hidden = !s.auth_enabled;
                localAccess = s.local_access;
                // Strict pairing mode still permits local recovery provisioning.
                recoveryButton.hidden = !['localhost', '127.0.0.1', '[::1]'].includes(location.hostname);
                logoutButton.hidden = !s.paired;
                if (s.auth_enabled) loadSessions();
            })
            .catch(() => {});
    }

    function escapeText(s) {
        const d = document.createElement('div');
        d.textContent = String(s ?? '');
        return d.innerHTML;
    }

    async function loadSessions() {
        try {
            const r = await fetch('/api/auth/sessions', { cache: 'no-store' });
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const data = await r.json();
            sessions.replaceChildren();
            for (const session of data.sessions) {
                const row = document.createElement('div');
                row.style.marginBottom = '.75rem';
                const description = document.createElement('span');
                description.textContent = session.label + (session.current ? ' (this browser)' : '') +
                    ' — expires ' + new Date(session.expires_at * 1000).toLocaleDateString() + ' ';
                const revoke = document.createElement('button');
                revoke.className = 'btn btn-ghost';
                revoke.textContent = 'Revoke';
                revoke.addEventListener('click', async () => {
                    revoke.disabled = true;
                    try {
                        const response = await fetch('/api/auth/sessions/' + encodeURIComponent(session.id) +
                            '/revoke', { method: 'POST', cache: 'no-store' });
                        if (!response.ok) throw new Error('HTTP ' + response.status);
                        if (session.current && !localAccess) location.href = '/pair';
                        else await loadSessions();
                    } catch (e) {
                        out.textContent = 'Could not revoke browser: ' + e.message;
                        revoke.disabled = false;
                    }
                });
                row.append(description, revoke);
                sessions.append(row);
            }
            const note = document.createElement('div');
            note.textContent = (data.sessions.length ? '' : 'No paired browsers. ') +
                data.recovery_codes_remaining + ' one-use recovery codes remain.';
            sessions.append(note);
        } catch (e) {
            sessions.textContent = 'Could not load browser sessions: ' + e.message;
        }
    }

    document.getElementById('btn-sessions-refresh').addEventListener('click', loadSessions);
    logoutButton.addEventListener('click', async () => {
        const r = await fetch('/api/auth/logout', { method: 'POST', cache: 'no-store' });
        if (r.ok) location.href = localAccess ? '/' : '/pair';
        else out.textContent = 'Could not sign out: HTTP ' + r.status;
    });
    recoveryButton.addEventListener('click', async () => {
        recoveryButton.disabled = true;
        try {
            const r = await fetch('/api/auth/recovery-codes', { method: 'POST', cache: 'no-store',
                headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ count: 5 }) });
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const data = await r.json();
            recoveryOutput.replaceChildren();
            const note = document.createElement('p');
            note.textContent = 'Save these in your password manager or offline before traveling. ' +
                'Previous recovery codes are invalid. Each code signs in one browser once; ' +
                'network and portal requirements still apply.';
            const codes = document.createElement('pre');
            codes.style.whiteSpace = 'pre-wrap';
            codes.style.overflowWrap = 'anywhere';
            codes.textContent = data.codes.join('\n');
            const hide = document.createElement('button');
            hide.className = 'btn btn-ghost';
            hide.textContent = 'Hide saved codes';
            hide.addEventListener('click', () => recoveryOutput.replaceChildren());
            recoveryOutput.append(note, codes, hide);
            await loadSessions();
        } catch (e) {
            recoveryOutput.textContent = 'Could not create recovery codes: ' + e.message;
        } finally {
            recoveryButton.disabled = false;
        }
    });

    btn.addEventListener('click', async () => {
        btn.disabled = true;
        try {
            const r = await fetch('/api/auth/pair-code', { method: 'POST', cache: 'no-store' });
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const data = await r.json();
            const minutes = Math.round((data.ttl_seconds || 600) / 60);
            // Preserve the portal hostname when using a remote browser.
            const urls = localAccess && (data.tailscale_pair_urls || []).length ?
                data.tailscale_pair_urls : [data.pair_url];
            const links = urls.map(u =>
                '<a href="' + escapeText(u + '#code=' + encodeURIComponent(data.code)) +
                '" target="_blank" rel="noopener">' + escapeText(u) + '</a>').join(' or ');
            out.innerHTML =
                '<div style="font-size:1.6rem;letter-spacing:.2em;font-weight:600">' + escapeText(data.code) + '</div>' +
                '<div>On the other device, open ' + links + ' and enter this code. ' +
                'It works once and expires in ' + minutes + ' minutes.</div>';
        } catch (e) {
            out.textContent = 'Could not create a pairing code: ' + (e.message || e);
        } finally {
            btn.disabled = false;
        }
    });
})();
