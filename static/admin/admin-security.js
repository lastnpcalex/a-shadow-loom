// Independent Admin authentication and persistent browser management.
(function () {
    const card = document.getElementById('devices-card');
    const output = document.getElementById('pair-device-output');
    const sessions = document.getElementById('browser-sessions');
    if (!card || !output || !sessions) return;
    async function request(path, method = 'GET', data = {}) {
        const options = { method, cache: 'no-store' };
        if (method === 'POST') {
            options.headers = { 'Content-Type': 'application/json' };
            options.body = JSON.stringify(data);
        }
        const r = await fetch('/api/admin-auth/' + path, options);
        if (r.status === 401) { location.href = '/login'; throw Error('Sign in to Admin'); }
        const j = await r.json();
        if (!r.ok) throw Error(j.error || j.detail || 'Request failed');
        return j;
    }
    async function refresh() {
        try {
            const s = await request('status');
            document.getElementById('btn-pair-device').hidden = s.configured;
            document.getElementById('btn-browser-logout').hidden = !s.authenticated;
            document.getElementById('btn-sessions-refresh').hidden = !s.authenticated;
            document.getElementById('admin-recovery-form').hidden = !s.authenticated;
            output.textContent = s.configured ?
                'Admin requires its own password and Authy. Remembered browsers last up to ' + s.session_days +
                ' days, with a ' + s.idle_days + '-day inactivity limit. Connect directly through Tailscale; the public portal cannot reach Admin.' :
                'Admin login is not configured. Remote access is blocked. Set up a password and Authy here on the host before traveling.';
            if (!s.authenticated) return;
            const j = await request('sessions');
            sessions.replaceChildren();
            for (const row of j.sessions) {
                const line = document.createElement('p');
                const label = document.createElement('span');
                label.textContent = row.label + (row.current ? ' (this browser)' : '') + ' — expires ' +
                    new Date(row.expires * 1000).toLocaleDateString() + ' ';
                const revoke = document.createElement('button');
                revoke.className = 'btn btn-ghost'; revoke.textContent = 'Revoke';
                revoke.onclick = async () => {
                    try {
                        await request('sessions/' + encodeURIComponent(row.id) + '/revoke', 'POST');
                        if (row.current) location.href = '/login'; else await refresh();
                    } catch (e) { output.textContent = e.message; }
                };
                line.append(label, revoke); sessions.append(line);
            }
            const note = document.createElement('p');
            note.textContent = j.recovery_codes_remaining + ' one-use recovery codes remain. Keep these and your password available while traveling.';
            sessions.append(note);
        } catch (e) { output.textContent = e.message; }
    }
    document.getElementById('btn-pair-device').onclick = () => { location.href = '/login'; };
    document.getElementById('btn-sessions-refresh').onclick = refresh;
    document.getElementById('btn-browser-logout').onclick = async () => {
        try { await request('logout', 'POST'); location.href = '/login'; }
        catch (e) { output.textContent = e.message; }
    };
    document.getElementById('admin-recovery-form').onsubmit = async e => {
        e.preventDefault();
        const form = e.currentTarget, button = form.querySelector('button');
        if (button.disabled) return;
        button.disabled = true;
        const password = document.getElementById('admin-recovery-password');
        const code = document.getElementById('admin-recovery-otp');
        const target = document.getElementById('recovery-code-output');
        try {
            const j = await request('recovery-codes', 'POST', { password: password.value, code: code.value });
            target.replaceChildren();
            const note = document.createElement('p');
            note.textContent = 'Save these codes now. Old codes are invalid. Each new code replaces Authy once, alongside your password.';
            const codes = document.createElement('pre'); codes.textContent = j.codes.join('\n');
            codes.style.overflowWrap = 'anywhere'; codes.style.whiteSpace = 'pre-wrap';
            const hide = document.createElement('button'); hide.className = 'btn btn-ghost';
            hide.textContent = 'Hide saved codes'; hide.onclick = () => target.replaceChildren();
            target.append(note, codes, hide);
            await refresh();
        } catch (error) { target.textContent = error.message; }
        finally { password.value = ''; code.value = ''; button.disabled = false; }
    };
    refresh();
})();
