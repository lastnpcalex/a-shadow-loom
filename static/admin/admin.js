// ── Loom Admin dashboard ────────────────────────────────────────────
// Single-page client over admin_server.py's JSON API. No build step.

let META = null;          // /api/meta payload
let refreshTimer = null;
let specsLoaded = false;
let acceptanceLoadedAt = 0;

function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

function showToast(msg) {
    let panel = document.getElementById('admin-header-info');
    if (!panel) {
        panel = document.createElement('details');
        panel.id = 'admin-header-info';
        panel.innerHTML = '<summary>Info <span>0</span></summary><div class="admin-info-list" role="log" aria-live="polite"></div>';
        document.getElementById('topbar').appendChild(panel);
    }
    const list = panel.querySelector('.admin-info-list');
    const row = document.createElement('div');
    row.textContent = `${new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})} · ${msg}`;
    list.prepend(row);
    while (list.children.length > 50) list.lastElementChild.remove();
    panel.querySelector('summary span').textContent = list.children.length;
}

function ensureConfirmModal() {
    let modal = document.getElementById('confirm-modal');
    if (modal) return modal;
    modal = document.createElement('div');
    modal.id = 'confirm-modal';
    modal.className = 'confirm-modal';
    modal.innerHTML = `
        <div class="confirm-backdrop" data-confirm-cancel></div>
        <div class="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
            <div class="confirm-kicker">Confirm Action</div>
            <h2 id="confirm-title">Are you sure?</h2>
            <p id="confirm-message"></p>
            <div class="confirm-actions">
                <button class="btn btn-ghost" data-confirm-cancel>Cancel</button>
                <button class="btn btn-warn" data-confirm-ok>OK</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    return modal;
}

function confirmDialog(message, title = 'Are you sure?') {
    const modal = ensureConfirmModal();
    const titleEl = modal.querySelector('#confirm-title');
    const msgEl = modal.querySelector('#confirm-message');
    const ok = modal.querySelector('[data-confirm-ok]');
    const cancelEls = modal.querySelectorAll('[data-confirm-cancel]');
    titleEl.textContent = title;
    msgEl.textContent = message;
    const previousFocus = document.activeElement;
    modal.classList.add('open');
    const cancelButton = modal.querySelector('button[data-confirm-cancel]');
    cancelButton.focus();

    return new Promise(resolve => {
        const cleanup = (value) => {
            modal.classList.remove('open');
            ok.removeEventListener('click', onOk);
            cancelEls.forEach(el => el.removeEventListener('click', onCancel));
            document.removeEventListener('keydown', onKey);
            if (previousFocus?.isConnected) previousFocus.focus();
            resolve(value);
        };
        const onOk = () => cleanup(true);
        const onCancel = () => cleanup(false);
        const onKey = (e) => {
            if (e.key === 'Escape') { e.preventDefault(); cleanup(false); }
            if (e.key === 'Tab') {
                e.preventDefault();
                (document.activeElement === ok ? cancelButton : ok).focus();
            }
        };
        ok.addEventListener('click', onOk);
        cancelEls.forEach(el => el.addEventListener('click', onCancel));
        document.addEventListener('keydown', onKey);
    });
}

// ── View switching ──────────────────────────────────────────────────
const VIEW_TITLES = { overview: 'Overview', quotas: 'Quotas', diagnostics: 'Diagnostics', activity: 'Activity', extensions: 'Extensions', openrouter: 'OpenRouter configuration', usage: 'Usage & Activity', guide: 'User Guide', settings: 'Settings' };

function setView(name) {
    if (name === 'settings') window.loadMachineSettings?.();
    document.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.view === name));
    document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
    document.getElementById('view-title').textContent = VIEW_TITLES[name] || name;
    localStorage.setItem('loom-admin-view', name);
    if (name === 'overview') {
        loadLlamaModels({ force: true });
        renderMemoryServices();
        loadDbStorage();
        if (typeof loadProviderUsage === 'function') loadProviderUsage();
    }
    if (name === 'activity') refreshSupervision();
    if (name === 'usage') window.loadUsageActivity?.({ force: true });
    if (name === 'quotas') {
        loadOpenRouterUsage();
        if (typeof loadProviderUsage === 'function') loadProviderUsage({ force: true });
    }
    if (name === 'diagnostics') {
        if (!specsLoaded) loadSpecs();
        window.loadTerminalStatus?.();
    }
    if (name === 'extensions' && typeof loadExtensions === 'function') loadExtensions();
    if (name === 'openrouter') {
        if (typeof loadOpenRouterSettings === 'function') loadOpenRouterSettings({ force: true });
        if (typeof loadOpenRouterModels === 'function') loadOpenRouterModels({ force: true });
    }
}

window.addEventListener('hashchange', () => {
    const name = location.hash.slice(1);
    if (document.getElementById('view-' + name)) setView(name);
});

// ── Llama switch-model control ──────────────────────────────────────
function _llamaModelKey(name) {
    return String(name || '')
        .split(/[\\/]/).pop()
        .replace(/\.gguf$/i, '')
        .toLowerCase()
        .replace(/[^a-z0-9]/g, '');
}

function _matchLlamaModelName(name, models) {
    if (!name) return '';
    const base = String(name).split(/[\\/]/).pop();
    if (models.includes(base)) return base;
    const key = _llamaModelKey(base);
    return models.find(m => _llamaModelKey(m) === key) || base;
}

async function loadLlamaModels(opts = {}) {
    const sel = document.getElementById('llama-model-switch');
    if (!sel || (!opts.force && sel.dataset.loaded === '1')) return;
    try {
        const r = await fetch('/api/llama-models', { cache: 'no-store' });
        if (!r.ok) return;
        const d = await r.json();
        const models = d.models || [];
        const loadedNow = (d.loaded || []).map(m => _matchLlamaModelName(m, models)).filter(Boolean);
        const configured = _matchLlamaModelName(d.configured, models);
        const options = models.slice();
        for (const name of loadedNow) {
            if (name && !options.includes(name)) options.unshift(name);
        }
        if (configured && !options.includes(configured)) options.push(configured);
        const selected = loadedNow[0] || configured || '';
        sel.innerHTML = options.map(m => {
            const marks = [];
            if (loadedNow.some(l => l === m)) marks.push('loaded');
            if (m === configured) marks.push('default');
            return `<option value="${esc(m)}"${m === selected ? ' selected' : ''}>${esc(m)}${marks.length ? ' - ' + marks.join(', ') : ''}</option>`;
        }).join('') || '<option value="">(no .gguf files found)</option>';
        sel.dataset.loaded = '1';
        sel.dataset.loadedModel = loadedNow[0] || '';
        return;
        /*
        const loaded = (d.loaded || []).map(m => m.split(/[\\/]/).pop());
        sel.innerHTML = (d.models || []).map(m => {
            const marks = [];
            if (loaded.some(l => l === m)) marks.push('loaded');
            if (m === d.configured) marks.push('default');
            return `<option value="${esc(m)}"${m === d.configured ? ' selected' : ''}>${esc(m)}${marks.length ? ' — ' + marks.join(', ') : ''}</option>`;
        }).join('') || '<option value="">(no .gguf files found)</option>';
        sel.dataset.loaded = '1';
        */
    } catch (e) { /* admin down or no models dir */ }
}

// Memory services: shared visibility for local processes that intentionally
// park GPU or system RAM (Llama, Dream/DiffusionGemma, ComfyUI).
function fmtMb(mb) {
    if (mb === null || mb === undefined) return '-';
    const n = Number(mb) || 0;
    return n >= 1024 ? (n / 1024).toFixed(n >= 10240 ? 1 : 2) + ' GB' : Math.round(n) + ' MB';
}

function memoryStateLabel(state) {
    return {
        warm: 'Warm',
        ready: 'Ready',
        orphan: 'Orphan',
        off: 'Off',
    }[state] || state || 'Unknown';
}

function memoryStateHint(s) {
    if (s.state === 'warm') return 'loaded and intentionally parked for reuse';
    if (s.state === 'ready') return 'process is running; model loads on demand';
    if (s.state === 'orphan') return 'process exists but the service probe failed';
    return 'not running';
}

function renderMemoryPanel(s) {
    const loaded = (s.loaded || []).filter(Boolean);
    const loadedText = loaded.length ? loaded.join(', ') : (s.state === 'ready' ? 'none loaded yet' : 'none');
    const pidText = (s.pids || []).length ? (s.pids || []).join(', ') : '-';
    const idle = s.idle_secs != null
        ? Math.floor(s.idle_secs / 60) + 'm ' + (s.idle_secs % 60) + 's'
        : '-';
    const timeout = s.idle_timeout_min > 0 ? 'auto-unloads at ' + s.idle_timeout_min + 'm idle' : 'auto-unload off';
    const extra = s.key === 'dream'
        ? `<div class="memory-row memory-muted">Idle: ${esc(idle)} (${esc(timeout)})</div>`
        : '';
    return `
        <div class="memory-top">
            <span class="memory-state memory-${esc(s.state)}">${esc(memoryStateLabel(s.state))}</span>
            <span class="memory-muted">${esc(memoryStateHint(s))}</span>
        </div>
        <div class="memory-row"><b>Loaded:</b> <span class="memory-loaded">${esc(loadedText)}</span></div>
        <div class="memory-grid">
            <div><span>GPU</span><b>${esc(fmtMb(s.gpu_mb))}</b></div>
            <div><span>Active RAM</span><b>${esc(fmtMb(s.active_ram_mb))}</b></div>
            <div><span>Reserved RAM</span><b>${esc(fmtMb(s.reserved_ram_mb))}</b></div>
            <div><span>Reclaimable Cache</span><b>${esc(fmtMb(s.cache_mb))}</b></div>
        </div>
        ${extra}
        <div class="memory-row memory-muted">PID: ${esc(pidText)} - ${esc(s.release_action || '')}</div>
    `;
}

async function renderMemoryServices() {
    let data;
    try {
        const r = await fetch('/api/memory-services', { cache: 'no-store' });
        if (!r.ok) return;
        data = await r.json();
    } catch (e) { return; }
    const byKey = {};
    (data.services || []).forEach(s => { byKey[s.key] = s; });
    for (const key of ['llama', 'dream', 'comfy']) {
        const panel = document.getElementById(key + '-memory-panel');
        if (!panel) continue;
        const svc = byKey[key];
        panel.innerHTML = svc
            ? renderMemoryPanel(svc)
            : '<span class="memory-muted">Memory state unavailable.</span>';
    }
}

async function renderDreamModels() {
    await renderMemoryServices();
}
async function llamaSwitchModel() {
    const sel = document.getElementById('llama-model-switch');
    const model = sel ? sel.value : '';
    if (!model) { showToast('Pick a model first'); return; }
    if (!await confirmDialog('Restart llama-server with ' + model + '? Cold load takes ~30-90s.', 'Restart Llama')) return;
    const btn = document.getElementById('btn-llama-switch');
    btn.disabled = true;
    await runTool('llama-restart?model=' + encodeURIComponent(model), { target: 'out-llama' });
    await loadLlamaModels({ force: true });
    btn.disabled = false;
}

async function llamaUnloadForComfy() {
    if (!await confirmDialog('Unload Llama Server and release its model memory? This stops llama-server until you reload it.', 'Unload Llama')) return;
    const btn = document.getElementById('btn-llama-unload');
    if (btn) btn.disabled = true;
    await runTool('llama-unload', { target: 'out-llama' });
    await loadLlamaModels({ force: true });
    if (btn) btn.disabled = false;
}

async function llamaReloadSelected() {
    const sel = document.getElementById('llama-model-switch');
    const model = sel ? sel.value : '';
    const suffix = model ? '?model=' + encodeURIComponent(model) : '';
    const label = model || 'the configured default model';
    if (!await confirmDialog('Reload llama-server with ' + label + '? Cold load takes ~30-90s.', 'Reload Llama')) return;
    const btn = document.getElementById('btn-llama-reload');
    if (btn) btn.disabled = true;
    await runTool('llama-reload' + suffix, { target: 'out-llama' });
    await loadLlamaModels({ force: true });
    if (btn) btn.disabled = false;
}

// ── Meta / quick links ──────────────────────────────────────────────
async function loadMeta() {
    try {
        const r = await fetch('/api/meta', { cache: 'no-store' });
        if (!r.ok) return;
        META = await r.json();
    } catch (e) { return; }
    document.getElementById('admin-port').textContent = META.admin_port;
    const llamaTag = document.getElementById('llama-port-tag');
    if (llamaTag) llamaTag.textContent = ':' + META.llama_port;
    const dreamTag = document.getElementById('dream-port-tag');
    if (dreamTag) dreamTag.textContent = ':' + META.dream_port;
    const ttydTag = document.getElementById('ttyd-port-tag');
    if (ttydTag) ttydTag.textContent = ':' + META.ttyd.port;

    // Links use the page's own hostname so they work over Tailscale.
    const h = location.hostname;
    const links = [
        ['🌐 Main EXO', `https://${h}:${META.main_port}`],
        ['🧪 Test', `http://${h}:${META.test_port}`],
        ['🦙 Llama', `http://${h}:${META.llama_port}`],
        ['🎨 ComfyUI', `http://${h}:${META.comfy_port}`],
    ];
    document.getElementById('quick-links').innerHTML = links
        .map(([label, url]) => `<a class="quick-link" href="${url}" target="_blank" rel="noopener">${label}</a>`)
        .join('');
}

// ── Polling ─────────────────────────────────────────────────────────
function scheduleRefresh(delay = 10000) {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refreshAll, delay);
}

async function refreshAll() {
    try {
        // Independent, minute-throttled request: never delay the activity poll.
        loadOpenRouterUsage();
        await Promise.all([refreshInstances(), refreshSupervision(), refreshPorts(), loadDbStorage(), loadAcceptance()]);
        const overview = document.getElementById('view-overview');
        if (overview?.classList.contains('active') && document.activeElement?.id !== 'llama-model-switch') {
            await loadLlamaModels({ force: true });
            await renderDreamModels();
        }
    } catch (e) { /* keep ticking */ }
    scheduleRefresh();
}

// Maps each server-card head-actions slot to the buttons that should render
// in each state. Keeps the up/down conditional logic — and the "no Stop button
// when nothing is running" rule — in one place.
const SERVER_HEAD_ACTIONS = {
    llama: {
        on: [
            { cls: 'btn btn-cyan', label: 'Restart', tool: 'llama-restart' },
            { cls: 'btn btn-warn', label: 'Unload', tool: 'llama-unload', confirm: 'Unload Llama Server and release its model memory?' },
        ],
        off: [
            { cls: 'btn btn-green', label: 'Start',  tool: 'llama-start' },
        ],
        target: 'out-llama',
    },
    comfy: {
        on: [
            { cls: 'btn btn-warn', label: 'Unload', tool: 'comfyui-stop', confirm: 'Unload ComfyUI by stopping its process?' },
        ],
        off: [
            { cls: 'btn btn-green', label: 'Start',  tool: 'comfyui-start' },
        ],
        target: 'out-comfy',
    },
    dream: {
        on: [
            { cls: 'btn btn-cyan', label: 'Restart', tool: 'dream-restart', confirm: 'Restart Dream Engine? This unloads Dream, releases cache, then starts it again.' },
            { cls: 'btn btn-warn', label: 'Unload', tool: 'dream-unload', confirm: 'Unload Dream Engine and release reclaimable model cache?' },
        ],
        off: [
            { cls: 'btn btn-green', label: 'Start',  tool: 'dream-start' },
        ],
        target: 'out-dream',
    },
};

function renderServerHeadActions(key, on) {
    const slot = document.getElementById('head-actions-' + key);
    if (!slot) return;
    const cfg = SERVER_HEAD_ACTIONS[key];
    if (!cfg) return;
    const buttons = on ? cfg.on : cfg.off;
    slot.innerHTML = buttons.map((b, i) =>
        `<button class="${b.cls}" data-srv="${key}" data-srv-i="${i}">${b.label}</button>`
    ).join('');
    slot.querySelectorAll('button').forEach(btn => {
        const b = buttons[+btn.dataset.srvI];
        btn.addEventListener('click', () => {
            const opts = { target: cfg.target };
            if (b.confirm) confirmTool(b.tool, b.confirm, opts);
            else runTool(b.tool, opts);
        });
    });
}

async function refreshPorts() {
    let d = {};
    try {
        const r = await fetch('/api/ports-status', { cache: 'no-store' });
        if (r.ok) d = await r.json();
    } catch (e) { return; }
    const order = [['main', 'EXO'], ['test', 'Test'], ['llama', 'Llama'], ['dream', 'Dream Engine'], ['comfy', 'Comfy']];
    const health = document.getElementById('service-health');
    if (health) health.innerHTML = order.filter(([key]) => key !== 'dream').map(([key, label]) =>
        `<div class="health-tile"><span>${label}</span><strong class="${typeof d[key] !== 'boolean' ? '' : d[key] ? 'health-online' : 'health-offline'}">${typeof d[key] !== 'boolean' ? 'Unknown' : d[key] ? 'Reachable' : 'Offline'}</strong></div>`).join('');
    document.getElementById('pulse-grid').innerHTML = order
        .map(([k, label]) => `<span class="pulse-item"><span class="dot ${d[k] ? 'on' : 'off'}"></span>${label}</span>`)
        .join('');
    for (const k of ['llama', 'comfy', 'dream']) {
        const dot = document.getElementById('dot-' + k);
        if (dot) dot.className = 'dot ' + (d[k] ? 'on' : 'off');
    }
    for (const k of ['llama', 'comfy', 'dream']) renderServerHeadActions(k, !!d[k]);
}

function _instanceActionsHtml(s) {
    const on = s.status === 'online';
    if (on || s.status === 'stopping' || s.pid || (s.pids || []).length) {
        return `<button onclick="doAction('${s.name}', 'restart')" class="btn btn-cyan">↻ Restart</button>
                <button onclick="doAction('${s.name}', 'shutdown')" class="btn btn-warn">⏹ Shutdown</button>`;
    }
    return `<button onclick="doAction('${s.name}', 'start')" class="btn btn-green">▶ Start</button>`;
}

function _instanceDbHtml(s) {
    if (s.name === 'main') {
        const dbs = (window.availableDbs || []).slice();
        if (!dbs.includes(s.db)) dbs.push(s.db);
        return `<select class="select" onchange="switchDb(this.value)" style="padding: 1px 4px; font-size: 0.9em;">
            ${dbs.map(db => `<option value="${esc(db)}"${db === s.db ? ' selected' : ''}>${esc(db)}</option>`).join('')}
        </select>`;
    }
    return `<span>${esc(s.db)}</span>`;
}

async function refreshInstances() {
    const r = await fetch('/api/status', { cache: 'no-store' });
    if (!r.ok) return;
    const d = await r.json();
    if (d.admin_pid) window.currentAdminPid = d.admin_pid;
    const instances = d.instances || [];

    // Mirror state onto the matching server-cards, so the
    // start/stop/restart controls and PID/DB readouts live there.
    for (const s of instances) {
        const on = s.status === 'online';
        const dot = document.getElementById('dot-' + s.name);
        if (dot) dot.className = 'dot ' + (on ? 'on' : 'off');
        const portTag = document.getElementById(s.name + '-port-tag');
        if (portTag) portTag.textContent = ':' + s.port;
        const managedSlot = document.getElementById(s.name + '-managed-tag');
        if (managedSlot) managedSlot.innerHTML = s.managed ? '<span class="tag">managed</span>' : '';
        const head = document.getElementById('head-actions-' + s.name);
        if (head) head.innerHTML = _instanceActionsHtml(s);
        const dbSlot = document.getElementById(s.name + '-db-label');
        if (dbSlot) dbSlot.innerHTML = _instanceDbHtml(s);
        const pidSlot = document.getElementById(s.name + '-pid-label');
        if (pidSlot) pidSlot.textContent = s.pid ? 'PID ' + s.pid : '—';
    }
}


// ── Instance + admin actions ────────────────────────────────────────
function _writeInstanceStatus(name, text, isError) {
    // Show progress + completion on both the compact Overview note and the
    // full Servers-page server-out, so the same "starting, started" widget
    const stamp = new Date().toLocaleTimeString();
    const line = '[' + stamp + '] ' + text;
    const note = document.getElementById('status-' + name);
    if (note) {
        note.textContent = text;
        note.style.color = isError ? 'var(--red)' : (text.endsWith('...') ? 'var(--amber)' : 'var(--green)');
    }
    const out = document.getElementById('out-' + name);
    if (out) {
        const existing = out.textContent ? out.textContent + '\n' : '';
        out.textContent = existing + line;
        out.classList.toggle('error', !!isError);
        out.scrollTop = out.scrollHeight;
    }
}

async function doAction(name, action) {
    const verb = { start: 'Starting', restart: 'Restarting', shutdown: 'Shutting down' }[action] || action;
    _writeInstanceStatus(name, verb + ' ' + name + '...', false);
    try {
        const r = await fetch('/action/' + name + '/' + action, { method: 'POST' });
        const d = await r.json();
        const msg = d.status || d.error || 'done';
        const isError = !!d.error;
        _writeInstanceStatus(name, msg, isError);
        if (isError && (d.pids || []).length) {
            const head = document.getElementById('head-actions-' + name);
            if (head) head.innerHTML = _instanceActionsHtml({ name, pids: d.pids });
        }
    } catch (e) {
        _writeInstanceStatus(name, 'failed: ' + e, true);
    }
    setTimeout(refreshAll, 1500);
}

let adminRestartPending = false;

function reportAdminLifecycle(message, failed = false) {
    const status = document.getElementById('admin-lifecycle-status');
    if (status) {
        status.textContent = message;
        status.classList.toggle('error', failed);
    }
    showToast(message);
}

async function loadAcceptance({ force = false } = {}) {
    if (!force && Date.now() - acceptanceLoadedAt < 30000) return;
    const badge = document.getElementById('acceptance-state');
    const status = document.getElementById('acceptance-status');
    const identities = document.getElementById('acceptance-identities');
    if (!badge || !status || !identities) return;
    try {
        const response = await fetch('/api/acceptance', { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        const state = String(data.state || 'unverified').toUpperCase();
        badge.textContent = state;
        badge.style.color = data.state === 'verified' ? 'var(--green)'
            : data.state === 'failed' ? 'var(--red)' : 'var(--amber)';
        status.textContent = data.reason || 'Acceptance status unavailable.';
        const short = value => value?.commit ? String(value.commit).slice(0, 12) : 'unavailable';
        identities.textContent = `accepted ${short(data.tested_source)} · checkout ${short(data.checkout_source)} · running ${short(data.runtime_source)}`;
        acceptanceLoadedAt = Date.now();
    } catch (error) {
        badge.textContent = 'UNVERIFIED';
        badge.style.color = 'var(--amber)';
        status.textContent = `Acceptance evidence unavailable: ${error}`;
        identities.textContent = '';
    }
}

function validAdminPid(pid) {
    return Number.isSafeInteger(pid) && pid > 0;
}

async function readAdminStatus() {
    let response = await fetch('/api/admin-status', { cache: 'no-store', signal: AbortSignal.timeout(8000) });
    // Assets can update before the serving admin is reloaded. Only a missing
    // route permits this read-only fallback; an auth/error response stays visible.
    if (response.status === 404) {
        response = await fetch('/api/status', { cache: 'no-store', signal: AbortSignal.timeout(20000) });
    }
    if (!response.ok) throw new Error(`Admin status unavailable (HTTP ${response.status}).`);
    const status = await response.json();
    if (!validAdminPid(status?.admin_pid)) throw new Error('Admin status did not identify its serving PID.');
    return status;
}

async function restartAdmin() {
    if (adminRestartPending) return;
    adminRestartPending = true;
    const buttons = ['btn-admin-restart', 'btn-admin-shutdown'].map(id => document.getElementById(id)).filter(Boolean);
    buttons.forEach(button => { button.disabled = true; });
    try {
        reportAdminLifecycle('Checking admin identity before restart…');
        // Establish identity before the mutation, even before the first dashboard poll.
        // A missing identity must never mean that any responding server is a replacement.
        let before;
        try {
            before = await readAdminStatus();
        } catch (error) {
            throw new Error(`${error.message} No restart request was sent.`);
        }
        let oldPid = before.admin_pid;
        window.currentAdminPid = oldPid;
        reportAdminLifecycle(`Requesting restart of admin PID ${oldPid}…`);
        let response;
        try {
            // Exactly one POST. A timeout/disconnect is ambiguous, never permission to retry.
            response = await fetch('/admin/restart', { method: 'POST', signal: AbortSignal.timeout(10000) });
        } catch (error) {
            reportAdminLifecycle('Restart response unavailable; checking for a replacement. Request not retried.');
        }
        if (response) {
            let data = null;
            try { data = await response.json(); } catch (error) { /* verify the PID, not this response */ }
            if (!response.ok || data?.error) {
                throw new Error(`Admin restart failed (HTTP ${response.status}): ${data?.error || data?.detail || 'response unavailable'}`);
            }
            // Older admins omit old_pid in the already-requested response. Retain the baseline.
            if (validAdminPid(data?.old_pid)) oldPid = data.old_pid;
            reportAdminLifecycle(data
                ? `Restart requested; waiting for admin PID ${oldPid} to be replaced…`
                : 'Invalid restart response; checking for a replacement. Request not retried.');
        }
        const start = Date.now();
        do {
            await new Promise(resolve => setTimeout(resolve, 1000));
            let status;
            try {
                status = await readAdminStatus();
            } catch (error) { /* Not reachable or missing identity is not replacement evidence. */ }
            if (status && status.admin_pid !== oldPid) {
                reportAdminLifecycle(`Admin restarted: PID ${oldPid} → ${status.admin_pid}.`);
                location.reload();
                return;
            }
            if (status?.admin_restart?.requested && Number.isInteger(status.admin_restart.helper_exit_code)) {
                throw new Error(`Restart helper exited (${status.admin_restart.helper_exit_code}), but admin PID ${oldPid} is still serving. Check admin_server.log; no request was retried.`);
            }
        } while (Date.now() - start < 40000);
        throw new Error(`Admin restart not confirmed: no replacement for PID ${oldPid} was verified. Check admin_server.log. No request was retried.`);
    } catch (error) {
        reportAdminLifecycle(error.message || String(error), true);
    } finally {
        adminRestartPending = false;
        buttons.forEach(button => { button.disabled = false; });
    }
}

async function adminAction(action) {
    if (action === 'restart') return restartAdmin();
    if (action !== 'shutdown' || adminRestartPending) return;
    try {
        const response = await fetch('/shutdown', { method: 'POST', signal: AbortSignal.timeout(10000) });
        const data = await response.json();
        if (!response.ok || data.error) {
            reportAdminLifecycle(data.error || `Admin shutdown failed (HTTP ${response.status}).`, true);
            return;
        }
        reportAdminLifecycle(data.status || 'Admin shutdown requested.');
    } catch (error) {
        reportAdminLifecycle('Admin shutdown response unavailable; check its state before retrying.', true);
    }
}

// ── Tools ───────────────────────────────────────────────────────────
async function confirmTool(name, msg, opts) {
    if (await confirmDialog(msg)) runTool(name, opts);
}

async function runTool(name, opts = {}) {
    clearTimeout(refreshTimer);
    const out = document.getElementById(opts.target || 'out-main');
    if (!out) { showToast('Output panel unavailable'); scheduleRefresh(); return; }
    const isPre = out.tagName === 'PRE';
    out.classList.remove('error', 'html-mode');
    out.innerHTML = '<span class="spinner"></span> Running ' + esc(name) + '...';
    try {
        const r = await fetch('/tools/' + name, { method: opts.method || 'POST' });
        const d = await r.json();
        if (d.status === 'ok_html' && !isPre) {
            out.innerHTML = d.output;
            out.classList.add('html-mode');
        } else {
            out.textContent = d.output || '(no output)';
            if (d.status === 'error') out.classList.add('error');
        }
    } catch (e) {
        out.textContent = 'Request failed: ' + e;
        out.classList.add('error');
    }
    if (/^llama-/.test(name)) {
        await loadLlamaModels({ force: true });
        await renderMemoryServices();
    }
    if (/^dream-/.test(name)) {
        await renderMemoryServices();
    }
    if (/^comfyui-|^clear-vram$|^memory-status$/.test(name)) {
        await renderMemoryServices();
    }
    scheduleRefresh(30000);
}


// ── System specs ────────────────────────────────────────────────────
async function loadSpecs() {
    specsLoaded = true;
    const panel = document.getElementById('specs-panel');
    panel.innerHTML = '<div class="empty-note"><span class="spinner"></span> Querying CPU / RAM / GPU…</div>';
    try {
        const r = await fetch('/tools/system-specs', { method: 'POST' });
        const d = await r.json();
        panel.innerHTML = d.status === 'ok_html' ? d.output : '<pre class="server-out">' + esc(d.output || 'no output') + '</pre>';
    } catch (e) {
        panel.innerHTML = '<div class="spec-error">Specs query failed: ' + esc(e) + '</div>';
    }
}


async function switchDb(dbName) {
    showToast('switching database to ' + dbName + '...');
    try {
        const r = await fetch('/api/change-db', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ db_name: dbName }),
        });
        const d = await r.json();
        if (r.ok) {
            showToast('database changed successfully' + (d.restarted ? ' (restarting EXO)' : ''));
        } else {
            showToast('failed to change database: ' + (d.error || 'unknown error'));
        }
    } catch (e) {
        showToast('error switching database: ' + e);
    }
    setTimeout(refreshAll, 1000);
}

window.availableDbs = [];
async function loadDatabases() {
    try {
        const r = await fetch('/api/databases', { cache: 'no-store' });
        if (r.ok) {
            const d = await r.json();
            window.availableDbs = d.databases || [];
        }
    } catch (e) { /* ignore */ }
}

async function loadDbStorage() {
    const folder = document.getElementById('db-folder');
    const filename = document.getElementById('db-filename');
    const current = document.getElementById('db-current');
    if (!folder || !filename || !current) return;
    try {
        const r = await fetch('/api/db-storage', { cache: 'no-store' });
        if (!r.ok) return;
        const d = await r.json();
        if (document.activeElement !== folder) folder.value = d.folder || '';
        if (document.activeElement !== filename) filename.value = d.filename || 'loom.db';
        const size = d.size_bytes ? ` (${(d.size_bytes / 1048576).toFixed(1)} MB)` : '';
        current.classList.toggle('warn', !!d.onedrive);
        current.textContent = `Current: ${d.resolved || d.configured || 'unknown'}${size}${d.onedrive ? ' - OneDrive path' : ''}`;
    } catch (e) {
        current.textContent = 'Database storage unavailable: ' + e;
        current.classList.add('warn');
    }
}

async function saveDbStorage() {
    const folder = document.getElementById('db-folder')?.value.trim();
    const filename = document.getElementById('db-filename')?.value.trim();
    const copyCurrent = !!document.getElementById('db-copy-current')?.checked;
    const restart = !!document.getElementById('db-restart-main')?.checked;
    if (!folder || !filename) {
        showToast('folder and file are required');
        return;
    }
    const msg = copyCurrent
        ? 'Save this DB location, stop Main EXO, copy the current SQLite files, then restart Main EXO?'
        : 'Save this DB location and restart Main EXO? If the DB does not exist, a new empty DB will be created.';
    if (!await confirmDialog(msg, 'Change Database Location')) return;
    const btn = document.getElementById('btn-db-save');
    if (btn) btn.disabled = true;
    try {
        const r = await fetch('/api/db-storage', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ folder, filename, copy_current: copyCurrent, restart }),
        });
        const d = await r.json();
        if (r.ok) {
            showToast('database location saved' + (d.restarted ? ' and Main EXO restarted' : ''));
            await loadDatabases();
            await loadDbStorage();
            setTimeout(refreshAll, 1500);
        } else {
            showToast('database change failed: ' + (d.error || 'unknown error'));
        }
    } catch (e) {
        showToast('database change failed: ' + e);
    } finally {
        if (btn) btn.disabled = false;
    }
}

// ── Boot ────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
    document.querySelectorAll('.nav-item').forEach(b =>
        b.addEventListener('click', () => setView(b.dataset.view)));
    document.getElementById('btn-admin-restart').addEventListener('click', () => adminAction('restart'));
    document.getElementById('btn-admin-shutdown').addEventListener('click', async () => {
        if (await confirmDialog('Shut down the admin server?', 'Shutdown Admin')) adminAction('shutdown');
    });
    document.getElementById('btn-specs-refresh').addEventListener('click', loadSpecs);
    document.getElementById('btn-activity-refresh').addEventListener('click', refreshSupervision);
    document.getElementById('btn-openrouter-refresh')?.addEventListener('click', () => loadOpenRouterUsage({ force: true }));
    document.getElementById('btn-acceptance-refresh')?.addEventListener('click', () => loadAcceptance({ force: true }));
    document.getElementById('btn-extensions-refresh')?.addEventListener('click', loadExtensions);
    document.getElementById('btn-extension-register')?.addEventListener('click', registerExtension);
    document.getElementById('btn-llama-switch').addEventListener('click', llamaSwitchModel);
    document.getElementById('btn-llama-unload').addEventListener('click', llamaUnloadForComfy);
    document.getElementById('btn-llama-reload').addEventListener('click', llamaReloadSelected);
    document.getElementById('btn-db-refresh').addEventListener('click', loadDbStorage);
    document.getElementById('btn-db-save').addEventListener('click', saveDbStorage);

    await loadDatabases();
    await loadDbStorage();
    await loadMeta();
    const requested = location.hash.slice(1);
    const saved = document.getElementById('view-' + requested) ? requested : localStorage.getItem('loom-admin-view');
    if (saved && document.getElementById('view-' + saved)) setView(saved);
    else setView('overview');  // default view is overview
    refreshAll();
});
