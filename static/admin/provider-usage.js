// Shared admin quota cards and main UI warnings. All values come from telemetry.
(() => {
    const card = document.getElementById('provider-usage-monitor');
    const mainUI = !!document.getElementById('app-header');
    const banner = document.createElement('div');
    banner.id = 'provider-usage-warning';
    banner.setAttribute('role', 'status');
    banner.hidden = true;
    let dismissed = false;
    const warningText = document.createElement('span');
    const dismiss = document.createElement('button');
    dismiss.type = 'button';
    dismiss.textContent = '×';
    dismiss.setAttribute('aria-label', 'Dismiss usage warning until refresh');
    dismiss.addEventListener('click', () => { dismissed = true; banner.hidden = true; });
    banner.append(warningText, dismiss);
    if (mainUI) document.getElementById('app-header')?.insertAdjacentElement('afterend', banner);
    let loading = false;
    let lastAttempt = 0;
    let snapshot = null;
    let failed = false;
    const escape = value => String(value ?? '').replace(/[&<>"']/g, ch =>
        ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
    const numeric = value => typeof value === 'number' && Number.isFinite(value);
    const resetDuration = seconds => {
        const totalMinutes = Math.max(1, Math.ceil(seconds / 60));
        const days = Math.floor(totalMinutes / 1440);
        const hours = Math.floor((totalMinutes % 1440) / 60);
        const minutes = totalMinutes % 60;
        return [days ? `${days}d` : '', hours ? `${hours}h` : '', minutes ? `${minutes}m` : '']
            .filter(Boolean).join(' ');
    };

    function render() {
        if (!snapshot) {
            if (card) {
                document.getElementById('provider-usage-status').textContent = loading
                    ? 'Checking quota observations…'
                    : failed ? 'Quota monitor unavailable; check Main EXO. Refresh to retry.' : 'Usage has not been loaded.';
            }
            if (mainUI && failed) {
                warningText.textContent = 'Quota monitor unavailable; remaining capacity is unknown.';
                banner.hidden = dismissed;
            }
            return;
        }
        const now = Date.now() / 1000;
        const providers = Array.isArray(snapshot.providers) ? snapshot.providers : [];
        const warnings = [];
        let critical = false;
        const displayProviders = card ? providers.flatMap(provider => {
            if (provider.provider !== 'antigravity' && !/antigravity/i.test(provider.label || '')) return [provider];
            const rows = (provider.windows || []).filter(row => numeric(row.used_percent));
            const representative = matches => rows.filter(matches).sort((a, b) => {
                const expired = row => numeric(row.resets_at) && row.resets_at <= now ? 1 : 0;
                return expired(a) - expired(b) || String(a.label).localeCompare(String(b.label));
            })[0];
            if (rows.some(row => row.pool)) {
                return [['AGY', 'google'], ['ExoGoo', 'third_party']].map(([label, pool]) => ({
                    ...provider, label, windows: rows.filter(row => row.pool === pool),
                }));
            }
            return [
                ['AGY', representative(row => /gemini|google/i.test(row.label || ''))],
                ['ExoGoo', representative(row => /gpt[\s_-]*oss|claude/i.test(row.label || ''))],
            ].map(([label, row]) => ({...provider, label, windows: row ? [row] : [],
                note: row ? `Representative model: ${row.label}. ${provider.note || ''}`
                    : `No representative model quota available. ${provider.note || ''}`}));
        }) : providers;
        const html = displayProviders.map(provider => {
            const old = !numeric(provider.observed_at) || now - provider.observed_at > (snapshot.stale_seconds || 900);
            const stale = failed || old || provider.status !== 'ok';
            const rows = Array.isArray(provider.windows) ? provider.windows : [];
            // Keep each pool's five-hour and weekly warnings independent;
            // coalesce repeated model observations from older snapshots.
            const pools = new Map();
            const content = rows.map(row => {
                if (!numeric(row.used_percent)) return '';
                const expired = numeric(row.resets_at) && row.resets_at <= now;
                const used = Math.min(100, Math.max(0, row.used_percent));
                const displayUsed = Number(used.toFixed(1));
                const displayRemaining = Number((100 - used).toFixed(1));
                const severity = used >= 90 ? 'critical' : used >= 80 ? 'nearing' : '';
                if (!stale && !expired && severity) {
                    if (provider.provider === 'antigravity') {
                        const family = row.pool === 'google' || /gemini|google/i.test(row.label || '') ? 'Google models' : 'Non-Google models';
                        const duration = row.window_minutes === 300 ? '5-hour' : row.window_minutes === 10080 ? 'Weekly' : '';
                        const pool = [family, duration].filter(Boolean).join(' ');
                        pools.set(pool, Math.max(pools.get(pool) || 0, displayUsed));
                    } else warnings.push(`${provider.label} ${row.label}: ${displayUsed}% used`);
                    if (severity === 'critical') critical = true;
                }
                const reset = !numeric(row.resets_at) ? 'Reset unavailable'
                    : expired ? 'Awaiting reset update'
                    : `${resetDuration(row.resets_at - now)} · ${new Date(row.resets_at * 1000).toLocaleString([], {
                        ...(row.window_minutes === 300 ? {} : {weekday: 'short'}), hour: 'numeric', minute: '2-digit'})}`;
                const label = row.pool ? (row.window_minutes === 300 ? '5-hour' : 'Weekly')
                    : provider.provider === 'codex' ? String(row.label).replace(/^codex: /i, '') : row.label;
                return `<div class="quota-window ${stale || expired ? 'stale' : severity}">
                    <strong>${escape(label)}</strong><span title="${displayRemaining}% remaining${stale || expired ? ' (last observation)' : ''}">${displayUsed}% used${stale || expired ? ' (stale)' : ''}</span>
                    <progress max="100" value="${used}" aria-label="${escape(provider.label + ' ' + row.label)} usage" aria-valuetext="${displayUsed}% used, ${displayRemaining}% remaining${stale || expired ? ' (last observation)' : ''}"></progress>
                    <small title="${numeric(row.resets_at) ? escape(new Date(row.resets_at * 1000).toLocaleString()) : ''}" aria-label="${escape('Reset: ' + reset)}">${escape(reset)}</small></div>`;
            }).join('');
            for (const [pool, used] of pools) warnings.push(`${provider.label} ${pool}: ${used}% used`);
            const status = failed ? 'Stale — connection failed' : provider.status === 'unavailable' ? 'Unavailable'
                : old || provider.status === 'stale' ? 'Stale' : 'Current';
            return `<section class="quota-provider"><h3>${escape(provider.label)} <small>${status}</small></h3>
                ${content || '<p>No quota data available.</p>'}
                <details class="quota-details"><summary>Details</summary><small>${escape(provider.note)}</small>
                ${numeric(provider.observed_at) ? `<small>Observed ${escape(new Date(provider.observed_at * 1000).toLocaleString())}</small>` : ''}</details></section>`;
        }).join('');
        if (card) {
            document.getElementById('provider-usage-cards').innerHTML = html;
            document.getElementById('provider-usage-status').textContent = loading ? 'Checking quota observations…'
                : failed ? 'Refresh failed; previous observations retained. Refresh to retry.'
                : 'Refreshes every 5m · Warnings at 80% / 90%';
            document.getElementById('provider-usage-status').classList.toggle('quota-ready', !loading && !failed);
        }
        if (mainUI) {
            const uncertain = providers.filter(p =>
                (failed || p.status !== 'ok' || !numeric(p.observed_at) || now - p.observed_at > (snapshot.stale_seconds || 900)
                    || p.windows?.some(w => numeric(w.resets_at) && w.resets_at <= now)));
            const labels = [...new Set(uncertain.map(p => p.label || p.provider || 'Unknown provider'))];
            const uncertainty = labels.length ? `Quota data unavailable or stale for ${labels.join(', ')}. Check Settings Usage for details.` : '';
            warningText.textContent = [...warnings, uncertainty].filter(Boolean).join(' · ');
            banner.classList.toggle('critical', critical);
            banner.hidden = dismissed || !warningText.textContent;
        }
    }

    async function refresh(force = false) {
        if (loading || document.hidden || (!force && Date.now() - lastAttempt < 60000)) return;
        if (card && !mainUI && !document.getElementById('view-quotas')?.classList.contains('active')) return;
        loading = true;
        lastAttempt = Date.now();
        const button = document.getElementById('btn-provider-usage-refresh');
        if (button) button.disabled = true;
        render();
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 30000);
        try {
            const response = await fetch('/api/provider-usage', {signal: controller.signal, cache: 'no-store'});
            if (!response.ok) throw new Error('Unavailable');
            const data = await response.json();
            if (!Array.isArray(data.providers)) throw new Error('Invalid snapshot');
            snapshot = data;
            failed = false;
        } catch (_) {
            failed = true;
            if (card) document.getElementById('provider-usage-status').textContent = 'Quota monitor unavailable; check Main EXO.';
        } finally {
            clearTimeout(timeout);
            loading = false;
            if (button) button.disabled = false;
            render();
        }
    }

    document.getElementById('btn-provider-usage-refresh')?.addEventListener('click', () => refresh(true));
    document.addEventListener('visibilitychange', () => refresh());
    window.loadProviderUsage = refresh;
    setInterval(() => { render(); refresh(); }, 60000);
    refresh();
})();
