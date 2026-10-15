// Admin-only metadata/approval surface. Never subscribes to or changes a chat cursor.
const supervisionCards = new Map();
const supervisionCancellations = new Map();
const supervisionRunKey = (instance, draft) => JSON.stringify([instance, String(draft)]);
let supervisionRefresh = null;
let supervisionTimer = null;

function supervisionURL(instance, item, view, exact = true) {
    if (!['http', 'https'].includes(instance.scheme) || !Number.isInteger(instance.port)) return '';
    const url = new URL(location.href);
    url.protocol = instance.scheme + ':';
    url.port = String(instance.port);
    url.pathname = '/';
    url.search = '';
    url.hash = '';
    url.searchParams.set('conversation', String(item.conv_id));
    url.searchParams.set('view', view);
    if (exact && item.branch_id != null) url.searchParams.set('branch', String(item.branch_id));
    return url.href;
}

function supervisionLinks(instance, item) {
    const link = (text, view, exact) => `<a class="btn btn-ghost" href="${esc(supervisionURL(instance, item, view, exact))}" target="_blank" rel="noopener">${esc(text)}</a>`;
    return link('Conversation #' + item.conv_id, 'tree', false)
        + (item.branch_id != null ? link('Branch #' + item.branch_id, 'tree', true) + link('Chat', 'chat', true) : '');
}

function supervisionIdentity(instance, item) {
    return `<strong>${esc(item.title)}</strong>
        <div class="activity-meta">${esc(instance.label || instance.instance)} · ${esc(item.mode || item.source || '')} · ${esc(item.model || '')}</div>
        <div class="activity-path">${esc(item.project_dir || 'No workspace folder')}</div>
        <div class="activity-meta">${esc(item.run_id || 'Conversation-scoped request')}</div>`;
}

function supervisionAge(value) {
    if (!value) return '';
    const millis = typeof value === 'number' ? value * 1000 : Date.parse(value);
    if (!Number.isFinite(millis)) return '';
    const age = Math.max(0, Math.floor((Date.now() - millis) / 1000));
    return ` · ${age}s`;
}

function createPermissionCard(instance, item) {
    const card = document.createElement('article');
    card.className = 'permission-card';
    const raw = typeof item.tool_input === 'string' ? item.tool_input : JSON.stringify(item.tool_input || {}, null, 2);
    const summary = typeof item.input_summary === 'string' ? item.input_summary : JSON.stringify(item.input_summary || '');
    card.innerHTML = `<div class="activity-identity">${supervisionIdentity(instance, item)}</div>
        <div class="activity-links">${supervisionLinks(instance, item)}</div>
        <h3>${esc(item.tool_name || 'Tool request')} <span class="count">${esc(item.profile || '')}</span></h3>
        <pre class="permission-summary">${esc(summary || raw)}</pre>
        ${item.risk_reason ? `<p class="permission-warning">${esc(item.risk_reason)}</p>` : ''}
        <details><summary>Exact arguments and approval scope</summary><pre>${esc(raw)}</pre>
            <p class="permission-rule">${esc(item.rule_preview || 'This request supports one-time approval only.')}</p></details>
        <label class="permission-note">Optional note to the agent (for example, why you are denying)
            <textarea class="text-input" rows="2" maxlength="4000" aria-label="Permission feedback"></textarea></label>
        <div class="permission-actions">
            <button type="button" class="btn btn-green" data-decision="allow">Allow once</button>
            ${item.supports_allow_all ? '<button type="button" class="btn btn-cyan" data-decision="branch">Allow for Branch</button>' : ''}
            <button type="button" class="btn btn-warn" data-decision="deny">Deny</button>
        </div>
        <div class="permission-result empty-note" role="status"></div>`;
    card.querySelectorAll('[data-decision]').forEach(button => {
        button.addEventListener('click', () => answerAdminPermission(card, button.dataset.decision));
    });
    return card;
}

function setPermissionAvailable(card, available) {
    card._available = available;
    card.classList.toggle('stale', !available);
    card.querySelectorAll('button').forEach(button => { button.disabled = !available || !!card._busy; });
    if (!available) card.querySelector('.permission-result').textContent = 'Instance unavailable — last known request. Refresh before deciding.';
    else if (!card._busy && !card._error) card.querySelector('.permission-result').textContent = '';
}

function renderSupervision(data) {
    if (!data || !Array.isArray(data.instances) || data.instances.some(instance =>
        !instance || typeof instance.instance !== 'string' || typeof instance.available !== 'boolean'
        || (instance.available && (!Array.isArray(instance.generations) || !Array.isArray(instance.permissions))))) {
        throw new Error('Invalid supervision response');
    }
    const instances = data.instances;
    const known = new Set(instances.map(i => i.instance));
    let count = 0;
    let active = 0;
    const generations = document.getElementById('generations-list');
    const generationRows = [];
    const errors = [];
    for (const instance of instances) {
        if (!instance.available) {
            errors.push(`${instance.instance}: ${instance.error || 'unavailable'}`);
            for (const card of supervisionCards.values()) {
                if (card._instance.instance === instance.instance) setPermissionAvailable(card, false);
            }
            continue;
        }
        const live = new Set();
        for (const item of instance.permissions || []) {
            const key = JSON.stringify([instance.instance, item.request_id]);
            live.add(key);
            count++;
            let card = supervisionCards.get(key);
            if (!card) {
                card = createPermissionCard(instance, item);
                supervisionCards.set(key, card);
                document.getElementById('permissions-list').appendChild(card);
            }
            // Keep the same nodes: polling must not discard notes, focus,
            // selection or expanded details while a user is making a decision.
            card._item = item;
            card._instance = instance;
            setPermissionAvailable(card, true);
        }
        for (const [key, card] of supervisionCards) {
            if (card._instance.instance === instance.instance && !live.has(key)) {
                card.remove();
                supervisionCards.delete(key);
            }
        }
        const liveDrafts = new Set((instance.generations || []).filter(item => item.draft_msg_id)
            .map(item => supervisionRunKey(instance.instance, item.draft_msg_id)));
        for (const [key, pending] of supervisionCancellations) {
            if (pending.instance === instance.instance && !pending.inFlight && !liveDrafts.has(key)) {
                supervisionCancellations.delete(key);
                showToast(`Turn #${pending.draft} is no longer active.`);
            }
        }
        for (const item of instance.generations || []) {
            active++;
            const key = supervisionRunKey(instance.instance, item.draft_msg_id);
            let pending = supervisionCancellations.get(key);
            // After an ambiguous failure, only a fresh owner snapshot may
            // re-enable an explicit retry. Never replay the POST automatically.
            if (pending?.phase === 'checking' && !pending.inFlight && item.status !== 'cancelling') {
                supervisionCancellations.delete(key);
                pending = null;
            }
            const stopping = pending || item.status === 'cancelling';
            generationRows.push(`<article class="generation-card">
                <div class="activity-identity">${supervisionIdentity(instance, item)}</div>
                <div class="activity-meta">${esc(item.kind || 'generation')} · ${esc(item.status)}${item.pid ? ' · PID ' + esc(item.pid) : ''}${supervisionAge(item.started_at)}</div>
                <div class="activity-links">${supervisionLinks(instance, item)}
                ${item.draft_msg_id ? `<button type="button" class="btn btn-warn" data-kill-instance="${esc(instance.instance)}" data-draft="${esc(item.draft_msg_id)}" ${stopping ? 'disabled' : ''}>${stopping ? 'Cancelling…' : 'Cancel turn'}</button>` : ''}</div>
            </article>`);
        }
    }
    for (const card of supervisionCards.values()) {
        if (!known.has(card._instance.instance)) setPermissionAvailable(card, false);
    }
    const html = generationRows.join('');
    if (generations._html !== html) {
        generations.innerHTML = html;
        generations._html = html;
        generations.querySelectorAll('[data-kill-instance]').forEach(button => button.addEventListener('click', () => cancelAdminGeneration(button)));
    }
    document.getElementById('activity-status').textContent = errors.join(' · ');
    document.getElementById('permissions-empty').hidden = supervisionCards.size > 0;
    document.getElementById('permissions-empty').textContent = errors.length ? 'No active requests from reachable instances.' : 'No pending permission requests.';
    document.getElementById('generations-empty').hidden = active > 0;
    document.getElementById('generations-empty').textContent = errors.length
        ? 'Generation status unavailable for some instances; no active runs reported by reachable instances.'
        : 'No active generations.';
    document.getElementById('permissions-count').textContent = count ? `(${count})` : '';
    document.getElementById('activity-count').textContent = count ? String(count) : '';
    document.getElementById('gens-count').textContent = active ? `(${active})` : '';
    document.getElementById('activity-summary').textContent = `${count} permissions · ${active} runs${errors.length ? ' · some instances unavailable' : ''}`;
    updateLandedSupervision(instances);
}

async function answerAdminPermission(card, decision) {
    if (card._busy || !card._available) return;
    const item = card._item;
    const instance = card._instance.instance;
    const result = card.querySelector('.permission-result');
    card._busy = true;
    card._error = false;
    setPermissionAvailable(card, true);
    result.textContent = 'Sending decision…';
    try {
        const response = await fetch(`/api/supervision/${encodeURIComponent(instance)}/permissions/${encodeURIComponent(item.request_id)}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Loom-Admin': '1' },
            body: JSON.stringify({ conv_id: item.conv_id, run_id: item.run_id,
                allow: decision !== 'deny', always: decision === 'branch',
                feedback: card.querySelector('textarea').value }), signal: AbortSignal.timeout(15000),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
        result.textContent = 'Decision recorded.';
        // Retain disabled controls until the authoritative next snapshot
        // removes this resolved request (do not invite a second click).
    } catch (error) {
        card._busy = false;
        card._error = true;
        result.textContent = String(error.message || error);
        setPermissionAvailable(card, false);
        result.textContent = `${error.message || error} Refresh to check current state.`;
    }
    await refreshSupervision();
}

async function cancelAdminGeneration(button) {
    const instance = button.dataset.killInstance;
    const draft = button.dataset.draft;
    const key = supervisionRunKey(instance, draft);
    if (supervisionCancellations.has(key)) return;
    if (!await confirmDialog(`Cancel turn #${draft} on ${instance}? Partial assistant context is discarded and queued work is paused.`, 'Cancel turn')) return;
    if (supervisionCancellations.has(key)) return;
    const pending = { instance, draft, inFlight: true, phase: 'cancelling' };
    supervisionCancellations.set(key, pending);
    button.disabled = true;
    button.textContent = 'Cancelling…';
    try {
        const response = await fetch(`/api/supervision/${encodeURIComponent(instance)}/generations/${encodeURIComponent(draft)}/kill`, {
            method: 'POST', headers: { 'X-Loom-Admin': '1' }, signal: AbortSignal.timeout(45000),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || data.error || 'Cancellation response unavailable');
        showToast(data.status === 'killed' ? 'Provider stopped; confirming run cleanup…' : 'Cancellation in progress…');
    } catch (error) {
        pending.phase = 'checking';
        showToast('Response unavailable; checking the turn. No cancellation was retried.');
    } finally {
        pending.inFlight = false;
        await refreshSupervision();
    }
}

function refreshSupervision() {
    if (supervisionRefresh) return supervisionRefresh;
    if (document.hidden) return Promise.resolve();
    clearTimeout(supervisionTimer);
    supervisionRefresh = (async () => {
        try {
            const response = await fetch('/api/supervision', { cache: 'no-store', signal: AbortSignal.timeout(10000) });
            if (!response.ok) {
                const error = new Error(`HTTP ${response.status}`);
                error.status = response.status;
                throw error;
            }
            renderSupervision(await response.json());
        } catch (error) {
            markLandedUnavailable();
            document.getElementById('activity-status').textContent = error.status === 404
                ? 'The running admin backend does not support this Activity dashboard (HTTP 404). Refreshing the page cannot load backend code; an admin process update is needed.'
                : `Supervision unavailable (${error.message || 'request failed'}). Last-known data may be stale; actions are disabled until refreshed.`;
            for (const card of supervisionCards.values()) setPermissionAvailable(card, false);
            const generations = document.getElementById('generations-list');
            generations.querySelectorAll('.generation-card').forEach(card => card.classList.add('stale'));
            generations.querySelectorAll('button').forEach(button => { button.disabled = true; });
            // Re-enable controls on the next valid snapshot even if its HTML is otherwise identical.
            generations._html = null;
            document.getElementById('permissions-empty').hidden = supervisionCards.size > 0;
            document.getElementById('permissions-empty').textContent = 'Permission status unavailable — not confirmed empty.';
            document.getElementById('generations-empty').hidden = generations.children.length > 0;
            document.getElementById('generations-empty').textContent = 'Generation status unavailable — not confirmed idle.';
            document.getElementById('permissions-count').textContent = '';
            document.getElementById('gens-count').textContent = '';
            document.getElementById('activity-count').textContent = '?';
            document.getElementById('activity-summary').textContent = 'Activity unavailable';
        } finally {
            supervisionRefresh = null;
            if (!document.hidden && document.getElementById('view-activity')?.classList.contains('active')) {
                supervisionTimer = setTimeout(refreshSupervision, 5000);
            }
        }
    })();
    return supervisionRefresh;
}

document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearTimeout(supervisionTimer);
    else refreshSupervision();
});
