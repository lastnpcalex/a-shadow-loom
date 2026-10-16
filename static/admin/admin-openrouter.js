// Read-only monitor of the API key configured in Main EXO.
let openRouterLoading = false;
let openRouterLastAttempt = 0;
let openRouterLastSnapshot = null;

function openRouterNumber(value) {
    return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function openRouterMoney(value) {
    const n = openRouterNumber(value);
    return n === null ? 'Unavailable' : `$${n.toFixed(2)}`;
}

function renderOpenRouter(snapshot) {
    const key = snapshot.current_key || {};
    const limits = snapshot.limits || {};
    const labels = { daily: 'Daily', weekly: 'Weekly', monthly: 'Monthly' };
    document.getElementById('openrouter-periods').innerHTML = Object.entries(labels).map(([period, label]) => {
        const used = openRouterNumber(key[`usage_${period}`]);
        const localLimit = openRouterNumber(limits[`${period}_limit_usd`]);
        const keyLimit = openRouterNumber(key.limit);
        const providerCap = key.limit_reset === period && keyLimit !== null;
        const dailyKeyCap = providerCap && !(localLimit > 0);
        const limit = dailyKeyCap ? keyLimit : localLimit > 0 ? localLimit : null;
        // Provider remaining includes its own accounting (including BYOK when selected).
        const remaining = dailyKeyCap ? openRouterNumber(key.limit_remaining)
            : used !== null && limit !== null ? limit - used : null;
        const consumed = dailyKeyCap ? remaining === null ? null : limit - remaining : used;
        const percent = limit !== null && consumed !== null
            ? limit === 0 ? 100 : Math.min(100, Math.max(0, consumed / limit * 100)) : null;
        const capLabel = dailyKeyCap ? 'OpenRouter key cap' : 'EXO budget';
        const budget = limit === null ? `No ${period} budget configured`
            : `${capLabel}: ${openRouterMoney(limit)}${remaining !== null
                ? ` · ${remaining < 0 ? openRouterMoney(-remaining) + ' over' : openRouterMoney(remaining) + ' left'}` : ''}`;
        const byok = openRouterNumber(key[`byok_usage_${period}`]);
        return `<div class="openrouter-period${percent >= 100 ? ' exhausted' : percent >= 80 ? ' nearing' : ''}" data-period="${period}">
            <h3>${label}</h3><div class="openrouter-spend">${openRouterMoney(used)}</div>
            <div class="openrouter-budget">${esc(budget)}</div>
            ${providerCap && localLimit > 0 ? `<div class="empty-note">OpenRouter key cap: ${openRouterMoney(keyLimit)} · ${openRouterMoney(key.limit_remaining)} left</div>` : ''}
            ${percent !== null ? `<div class="openrouter-meter" role="progressbar" aria-label="${label} ${capLabel}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent.toFixed(1)}"><span style="width:${percent}%"></span></div>` : ''}
            ${byok > 0 ? `<div class="empty-note">BYOK: ${openRouterMoney(byok)} (separate)</div>` : ''}
        </div>`;
    }).join('');

    const keyLimit = openRouterNumber(key.limit);
    const reset = { daily: 'daily', weekly: 'weekly', monthly: 'monthly' }[key.limit_reset]
        || (key.limit_reset === null ? 'no scheduled reset' : 'reset unavailable');
    document.getElementById('openrouter-key-cap').textContent = !snapshot.api_key_configured
        ? 'Set an OpenRouter API key in EXO settings to monitor usage.'
        : !snapshot.current_key ? 'OpenRouter key cap unavailable.'
        : keyLimit === null ? 'OpenRouter key cap: none configured.'
        : `OpenRouter key cap: ${openRouterMoney(keyLimit)} · ${openRouterMoney(key.limit_remaining)} remaining · ${reset}${key.include_byok_in_limit ? ' · includes BYOK' : ''}`;
    const account = snapshot.account || {};
    const credits = openRouterNumber(account.total_credits);
    const spent = openRouterNumber(account.total_usage);
    document.getElementById('openrouter-account').textContent = credits !== null && spent !== null
        ? `Account credits: ${openRouterMoney(credits - spent)} left · ${openRouterMoney(spent)} used across all keys`
        : snapshot.management_key_configured ? 'Account credits unavailable.' : 'Account credits require an optional management key.';
}

async function loadOpenRouterUsage({ force = false } = {}) {
    const card = document.getElementById('openrouter-monitor');
    if (!card || openRouterLoading) return;
    if (!force && (document.hidden || !document.getElementById('view-quotas')?.classList.contains('active')
            || (openRouterLastAttempt && Date.now() - openRouterLastAttempt < 60000))) return;
    openRouterLoading = true;
    openRouterLastAttempt = Date.now();
    const button = document.getElementById('btn-openrouter-refresh');
    const status = document.getElementById('openrouter-monitor-status');
    button.disabled = true;
    status.textContent = 'Checking OpenRouter usage…';
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);
    try {
        const response = await fetch('/api/openrouter/usage', { cache: 'no-store', signal: controller.signal });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        if (typeof data.api_key_configured !== 'boolean') throw new Error('Usage monitor response unavailable.');
        const failedKey = data.api_key_configured && !data.current_key;
        if (!failedKey || !openRouterLastSnapshot) {
            renderOpenRouter(data);
            openRouterLastSnapshot = data;
        }
        card.classList.toggle('stale', failedKey);
        const warning = (data.warnings || []).join(' ');
        status.textContent = failedKey
            ? `${warning || 'Key usage unavailable.'} Displayed counters are not current.`
            : `Updated ${new Date(data.fetched_at * 1000).toLocaleTimeString()}${warning ? ' · ' + warning : ''}`;
    } catch (error) {
        card.classList.add('stale');
        status.textContent = `Usage unavailable: ${error.name === 'AbortError' ? 'request timed out' : error.message}. ${openRouterLastSnapshot ? 'Last known counters retained; not current.' : 'No usage confirmed.'}`;
    } finally {
        clearTimeout(timer);
        openRouterLoading = false;
        button.disabled = false;
    }
}

document.addEventListener('visibilitychange', () => {
    if (!document.hidden) loadOpenRouterUsage();
});

// ── OpenRouter settings (budgets, base URL, keys) ──────────────────────
// Everything here is proxied to Main EXO by the admin server; the admin
// server itself never stores OpenRouter config or secrets.

function _orSetStatus(id, text, isError) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text || '';
    el.style.color = isError ? 'var(--red, #ff5a5a)' : '';
}

function _orKeyValue(secret) {
    if (!secret || !secret.configured) return 'missing';
    const base = secret.preview || 'configured';
    return `${base}${secret.source ? ` (${secret.source})` : ''}`;
}

async function loadOpenRouterSettings({ force = false } = {}) {
    const status = document.getElementById('or-settings-status');
    try {
        const response = await fetch('/api/openrouter/settings', { cache: 'no-store' });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        const s = data.settings || {};
        const set = (id, v) => {
            const el = document.getElementById(id);
            if (el) el.value = v === null || v === undefined ? '' : String(v);
        };
        set('or-weekly-limit', s.openrouter_weekly_limit_usd);
        set('or-monthly-limit', s.openrouter_monthly_limit_usd);
        set('or-prompt-price', s.openrouter_max_prompt_price_per_mtok);
        set('or-completion-price', s.openrouter_max_completion_price_per_mtok);
        set('or-base-url', s.openrouter_base_url);
        const sec = data.secrets || {};
        _orSetStatus('or-keys-status',
            `Inference key: ${_orKeyValue(sec.api_key)} · Management key: ${_orKeyValue(sec.management_key)}`);
        if (status) status.textContent = '';
    } catch (e) {
        _orSetStatus('or-settings-status', `Load failed: ${e.message || e}`, true);
    }
}

async function saveOpenRouterSettings() {
    const status = document.getElementById('or-settings-status');
    const num = (id) => {
        const el = document.getElementById(id);
        const v = el && el.value.trim() !== '' ? Number(el.value) : null;
        return v === null || Number.isNaN(v) ? null : v;
    };
    const payload = {};
    const weekly = num('or-weekly-limit');
    const monthly = num('or-monthly-limit');
    const prompt = num('or-prompt-price');
    const completion = num('or-completion-price');
    if (weekly !== null) payload.openrouter_weekly_limit_usd = weekly;
    if (monthly !== null) payload.openrouter_monthly_limit_usd = monthly;
    if (prompt !== null) payload.openrouter_max_prompt_price_per_mtok = prompt;
    if (completion !== null) payload.openrouter_max_completion_price_per_mtok = completion;
    const baseEl = document.getElementById('or-base-url');
    if (baseEl && baseEl.value.trim()) payload.openrouter_base_url = baseEl.value.trim();
    if (status) status.textContent = 'Saving…';
    try {
        const response = await fetch('/api/openrouter/settings', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        if (status) status.textContent = 'Saved.';
        await loadOpenRouterUsage({ force: true });
    } catch (e) {
        _orSetStatus('or-settings-status', `Save failed: ${e.message || e}`, true);
    }
}

async function saveOpenRouterKeys() {
    const status = document.getElementById('or-keys-status');
    const payload = {};
    const apiEl = document.getElementById('or-api-key');
    const mgmtEl = document.getElementById('or-management-key');
    if (apiEl && apiEl.value.trim()) payload.api_key = apiEl.value.trim();
    if (mgmtEl && mgmtEl.value.trim()) payload.management_key = mgmtEl.value.trim();
    if (!Object.keys(payload).length) {
        _orSetStatus('or-keys-status', 'Paste at least one key to save.', true);
        return;
    }
    if (status) status.textContent = 'Saving keys…';
    try {
        const response = await fetch('/api/openrouter/secrets', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        if (apiEl) apiEl.value = '';
        if (mgmtEl) mgmtEl.value = '';
        const sec = data.secrets || {};
        _orSetStatus('or-keys-status',
            `Inference key: ${_orKeyValue(sec.api_key)} · Management key: ${_orKeyValue(sec.management_key)}`);
        await loadOpenRouterUsage({ force: true });
    } catch (e) {
        _orSetStatus('or-keys-status', `Save failed: ${e.message || e}`, true);
    }
}

// ── OpenRouter model slugs editor ──────────────────────────────────────
// Rows mirror the registry model shape: { backend: 'openrouter', id, label,
// context_length, handoff_threshold, aliases[], routes[] }. Saving posts the
// full list; Main validates it against the shared registry rules and
// hot-reloads the pickers.

function _orModelRow(m = {}) {
    const row = document.createElement('div');
    row.className = 'or-model-row';
    row.innerHTML = `
        <label><span>Slug (OpenRouter ID)</span><input type="text" class="text-input" data-field="id" placeholder="vendor/model-id" spellcheck="false"></label>
        <label><span>Label</span><input type="text" class="text-input" data-field="label" spellcheck="false"></label>
        <label><span>Context</span><input type="number" class="text-input" data-field="context_length" min="1024" step="1"></label>
        <label><span>Handoff ctx</span><input type="number" class="text-input" data-field="handoff_threshold" min="1" step="1" placeholder="—"></label>
        <label><span>Aliases (comma)</span><input type="text" class="text-input" data-field="aliases" spellcheck="false"></label>
        <label><span>Routes (comma)</span><input type="text" class="text-input" data-field="routes" value="openrouter,goose,weave" spellcheck="false"></label>
        <button type="button" class="btn btn-warn or-row-remove" title="Remove this model">✕</button>`;
    const set = (field, v) => {
        const el = row.querySelector(`[data-field="${field}"]`);
        if (el) el.value = v === null || v === undefined ? '' : String(v);
    };
    set('id', m.id);
    set('label', m.label);
    set('context_length', m.context_length);
    set('handoff_threshold', m.handoff_threshold);
    set('aliases', (m.aliases || []).join(', '));
    set('routes', (m.routes || []).join(','));
    row.querySelector('.or-row-remove').addEventListener('click', () => row.remove());
    return row;
}

function _collectOpenRouterModels() {
    const rows = [...document.querySelectorAll('#or-models-rows .or-model-row')];
    const models = [];
    const errors = [];
    const seen = new Set();
    for (const [i, row] of rows.entries()) {
        const get = (field) => {
            const el = row.querySelector(`[data-field="${field}"]`);
            return el ? el.value.trim() : '';
        };
        const id = get('id');
        if (!id) continue; // blank row
        const label = get('label') || id;
        const context = Number(get('context_length'));
        if (!Number.isFinite(context) || context <= 0) {
            errors.push(`Row ${i + 1} (${id}): context length must be a positive integer.`);
            continue;
        }
        const key = id.toLowerCase();
        if (seen.has(key)) errors.push(`Duplicate slug: ${id}`);
        seen.add(key);
        const model = {
            backend: 'openrouter',
            id, label,
            context_length: Math.round(context),
            routes: get('routes').split(',').map(s => s.trim()).filter(Boolean),
            capabilities: { tools: true },
        };
        const threshold = get('handoff_threshold');
        if (threshold !== '') {
            const t = Number(threshold);
            if (Number.isFinite(t) && t > 0) model.handoff_threshold = Math.round(t);
        }
        const aliases = get('aliases').split(',').map(s => s.trim()).filter(Boolean);
        if (aliases.length) model.aliases = aliases;
        models.push(model);
    }
    return { models, errors };
}

async function loadOpenRouterModels({ force = false } = {}) {
    const container = document.getElementById('or-models-rows');
    const status = document.getElementById('or-models-status');
    if (!container) return;
    if (container.dataset.loaded === '1' && !force) return;
    container.dataset.loaded = '1';
    status.textContent = 'Loading models…';
    try {
        const response = await fetch('/api/models/openrouter', { cache: 'no-store' });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        container.innerHTML = '';
        for (const m of data.models || []) container.appendChild(_orModelRow(m));
        status.textContent = data.overrides_active
            ? 'Local overrides are active (saved rows replace same-ID catalog entries).'
            : 'No local overrides yet — these entries come from the shipped catalog. Saving starts an override set.';
    } catch (e) {
        status.textContent = `Load failed: ${e.message || e}`;
    }
}

async function saveOpenRouterModels() {
    const status = document.getElementById('or-models-status');
    const { models, errors } = _collectOpenRouterModels();
    if (errors.length) {
        status.textContent = errors.join(' ');
        return;
    }
    status.textContent = 'Validating and saving…';
    try {
        const response = await fetch('/api/models/openrouter', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ models }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        const container = document.getElementById('or-models-rows');
        container.innerHTML = '';
        for (const m of data.models || []) container.appendChild(_orModelRow(m));
        status.textContent = `Saved ${models.length} model(s) — pickers reloaded.`;
        await loadOpenRouterUsage({ force: true });
    } catch (e) {
        status.textContent = `Save failed: ${e.message || e}`;
    }
}

// ── OpenRouter tab wiring ──────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('btn-or-settings-save')?.addEventListener('click', saveOpenRouterSettings);
    document.getElementById('btn-or-keys-save')?.addEventListener('click', saveOpenRouterKeys);
    document.getElementById('btn-or-settings-reload')?.addEventListener('click', () => loadOpenRouterSettings({ force: true }));
    document.getElementById('btn-or-models-add')?.addEventListener('click', () => {
        const container = document.getElementById('or-models-rows');
        if (container) container.appendChild(_orModelRow());
    });
    document.getElementById('btn-or-models-save')?.addEventListener('click', saveOpenRouterModels);
    document.getElementById('btn-or-models-reload')?.addEventListener('click', () => loadOpenRouterModels({ force: true }));
});
