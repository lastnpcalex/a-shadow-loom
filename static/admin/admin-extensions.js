let extensionsLoading = false;

async function loadExtensions() {
    const list = document.getElementById('extensions-list');
    const status = document.getElementById('extensions-status');
    if (!list || extensionsLoading) return;
    extensionsLoading = true;
    status.textContent = 'Reading external extension registry…';
    try {
        const response = await fetch('/api/extensions', { cache: 'no-store' });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        const rows = data.extensions || [];
        list.innerHTML = rows.map(extensionCard).join('');
        status.textContent = rows.length ? `${rows.length} registered extension${rows.length === 1 ? '' : 's'}` : 'No extensions registered.';
        bindExtensionActions();
    } catch (error) {
        status.textContent = `Extension registry unavailable: ${error.message || error}`;
    } finally {
        extensionsLoading = false;
    }
}

function extensionCard(item) {
    if (item.state === 'invalid') {
        return `<article class="extension-card extension-invalid"><h3>${esc(item.id)}</h3><div class="permission-warning">${esc(item.error)}</div></article>`;
    }
    const health = item.health || {};
    const active = !!item.pid;
    const healthLabel = health.state || 'unknown';
    return `<article class="extension-card" data-extension-id="${esc(item.id)}">
        <div class="card-head"><h3><span class="dot ${healthLabel === 'healthy' || healthLabel === 'running' ? 'on' : ''}"></span>${esc(item.name)}</h3><span class="port-tag">${esc(item.kind)}</span></div>
        <div class="extension-meta"><b>${esc(item.id)}</b> · ${esc(healthLabel)}${item.pid ? ` · PID ${esc(item.pid)}` : ''}</div>
        <div class="extension-path" title="${esc(item.workspace_root)}">${esc(item.workspace_root)}</div>
        <div class="extension-path" title="${esc(item.manifest_path)}">Manifest: ${esc(item.manifest_path)}</div>
        <details class="extension-diagnostics"><summary>Launch and health details</summary>
            <dl>
                <dt>Working directory</dt><dd>${esc(item.cwd || 'Unavailable')}</dd>
                <dt>Launch arguments</dt><dd><code>${esc(JSON.stringify(item.command || []))}</code></dd>
                <dt>Health check</dt><dd>${esc(item.health_url || 'Process identity')}</dd>
                ${health.http_status != null ? `<dt>HTTP status</dt><dd>${esc(health.http_status)}</dd>` : ''}
                ${health.detail ? `<dt>Health detail</dt><dd>${esc(health.detail)}</dd>` : ''}
            </dl>
        </details>
        <div class="action-row">
            <button class="btn btn-green" data-extension-action="start" ${active ? 'disabled' : ''}>Start</button>
            <button class="btn btn-warn" data-extension-action="stop" ${active ? '' : 'disabled'}>Stop</button>
            ${item.can_update ? `<button class="btn btn-ghost" data-extension-action="update" ${active ? 'disabled' : ''}>Update</button>` : ''}
            <span class="action-spacer"></span>
            <button class="btn btn-ghost" data-extension-action="unregister" ${active ? 'disabled' : ''}>Unregister</button>
        </div>
    </article>`;
}

function bindExtensionActions() {
    document.querySelectorAll('[data-extension-action]').forEach(button => {
        button.addEventListener('click', async () => {
            const card = button.closest('[data-extension-id]');
            const id = card?.dataset.extensionId;
            const action = button.dataset.extensionAction;
            if (!id) return;
            if (['stop', 'update', 'unregister'].includes(action)
                    && !await confirmDialog(`${action[0].toUpperCase() + action.slice(1)} ${id}?`, `${action} extension`)) return;
            button.disabled = true;
            try {
                const response = await fetch(action === 'unregister'
                    ? `/api/extensions/${encodeURIComponent(id)}`
                    : `/api/extensions/${encodeURIComponent(id)}/${action}`, {
                    method: action === 'unregister' ? 'DELETE' : 'POST',
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
                showToast(`${id}: ${action} complete`);
            } catch (error) {
                showToast(`${id}: ${action} failed — ${error.message || error}`);
            }
            await loadExtensions();
        });
    });
}

async function registerExtension() {
    const input = document.getElementById('extension-manifest-path');
    const path = input?.value.trim();
    if (!path) return;
    try {
        const response = await fetch('/api/extensions/register', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ manifest_path: path }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        input.value = '';
        showToast(`registered ${data.extension.id}`);
        await loadExtensions();
    } catch (error) {
        showToast(`registration failed — ${error.message || error}`);
    }
}
