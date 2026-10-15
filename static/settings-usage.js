// Read-only account spending. No keys, provisioning or provider settings here.
(() => {
    let loading = false;
    let lastAttempt = 0;
    const status = document.getElementById('settings-openrouter-status');
    const money = value => typeof value === 'number' && Number.isFinite(value) ? `$${value.toFixed(2)}` : 'Unavailable';
    async function refresh(force = false) {
        if (loading || (!force && Date.now() - lastAttempt < 60000)) return;
        loading = true; lastAttempt = Date.now();
        const button = document.getElementById('btn-settings-openrouter-refresh');
        button.disabled = true; status.textContent = 'Checking OpenRouter spending…';
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 30000);
        try {
            const response = await fetch('/api/openrouter/usage', {cache: 'no-store', signal: controller.signal});
            if (!response.ok) throw new Error('Usage unavailable');
            const data = await response.json();
            if (typeof data.api_key_configured !== 'boolean') throw new Error('Usage unavailable');
            const key = data.current_key || {};
            const periods = document.getElementById('settings-openrouter-periods');
            periods.replaceChildren();
            for (const [period, label] of [['daily', 'Daily'], ['weekly', 'Weekly'], ['monthly', 'Monthly']]) {
                const card = document.createElement('section'); card.className = 'quota-provider';
                const heading = document.createElement('h3'); heading.textContent = label;
                const spend = document.createElement('p'); spend.textContent = `${money(key[`usage_${period}`])} used`;
                const providerCap = key.limit_reset === period && typeof key.limit === 'number';
                const cap = providerCap
                    ? key.limit : data.limits?.[`${period}_limit_usd`];
                const limit = document.createElement('small');
                limit.textContent = typeof cap === 'number' && (providerCap || cap > 0)
                    ? `${providerCap ? 'Key cap' : 'Budget'}: ${money(cap)}` : 'No period budget reported';
                const used = key[`usage_${period}`];
                const remaining = providerCap ? key.limit_remaining : typeof cap === 'number' && cap > 0 && typeof used === 'number' ? cap - used : null;
                const left = document.createElement('small');
                left.textContent = typeof remaining === 'number' ? `${money(remaining)} remaining` : 'Remaining unavailable';
                card.append(heading, spend, limit, left); periods.appendChild(card);
                if (Number.isFinite(cap) && cap > 0 && Number.isFinite(used) && used >= 0) {
                    const percent = Math.min(100, Math.max(0, used / cap * 100));
                    card.classList.toggle('nearing', percent >= 80 && percent < 90);
                    card.classList.toggle('critical', percent >= 90);
                    const bar = document.createElement('progress');
                    bar.max = 100; bar.value = percent;
                    bar.setAttribute('aria-label', `OpenRouter ${label} budget usage`);
                    bar.setAttribute('aria-valuetext', `${percent.toFixed(1)}% used, ${(100 - percent).toFixed(1)}% remaining`);
                    card.appendChild(bar);
                }
                if (typeof key[`byok_usage_${period}`] === 'number' && key[`byok_usage_${period}`] > 0) {
                    const byok = document.createElement('small'); byok.textContent = `BYOK: ${money(key[`byok_usage_${period}`])} (separate)`;
                    card.appendChild(byok);
                }
            }
            status.textContent = !data.api_key_configured ? 'OpenRouter is not configured. Provider configuration is in Admin.'
                : !data.current_key ? 'OpenRouter spending unavailable. Refresh to retry.'
                : `Updated ${new Date().toLocaleTimeString()}. Key remaining: ${money(key.limit_remaining)}.`;
            const account = data.account || {};
            document.getElementById('settings-openrouter-account').textContent = typeof account.total_credits === 'number' && typeof account.total_usage === 'number'
                ? `Account credits remaining: ${money(account.total_credits - account.total_usage)}` : '';
        } catch (_) {
            status.textContent = 'OpenRouter usage unavailable; previous observations retained. Refresh to retry.';
        } finally { clearTimeout(timer); loading = false; button.disabled = false; }
    }
    window.loadSettingsOpenRouterUsage = refresh;
    document.getElementById('btn-settings-openrouter-refresh').addEventListener('click', () => refresh(true));
})();
