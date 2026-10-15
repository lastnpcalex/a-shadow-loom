// Read-only admin inbox. Review marks belong to this browser, not chat cursors.
const landedReviewStorageKey = 'exo-admin-landed-reviewed:v1';
const landedSnapshots = new Map();
let landedStorageWarning = '';
let landedReviewed = readLandedReviews();

function readLandedReviews() {
    try {
        const saved = JSON.parse(localStorage.getItem(landedReviewStorageKey) || '[]');
        if (!Array.isArray(saved)) throw new Error('Invalid review marks');
        return new Map(saved.filter(row => Array.isArray(row) && typeof row[0] === 'string'
            && Number.isFinite(row[1])).slice(-2000));
    } catch (_) {
        landedStorageWarning = 'Review storage unavailable; marks may last only until this page closes.';
        return new Map();
    }
}

function landedItemKey(instance, item) {
    // Creation time also separates reused message IDs after switching databases.
    return JSON.stringify([instance.instance, instance.scheme, instance.port, item.kind,
        item.conv_id, item.run_id, item.draft_msg_id, item.created_at, item.completed_at, item.status]);
}

function landedWorkspaceKey(instance, item) {
    const rawPath = String(item.project_dir || '').replace(/\\/g, '/');
    let path = rawPath.replace(/\/+$/, '') || (rawPath.startsWith('/') ? '/' : '');
    // Windows aliases share a group; POSIX paths retain case sensitivity.
    if (/^[a-z]:(\/|$)/i.test(path) || path.startsWith('//')) path = path.toLowerCase();
    return JSON.stringify([instance.instance, instance.scheme, instance.port,
        path || `conversation:${item.conv_id}`]);
}

function landedSlotKey(instance, item) {
    // A child job has its own result; never hide it behind its parent's chat.
    const thread = item.kind === 'child' ? ['child', item.run_id]
        : item.loom_thread_id ? ['thread', item.loom_thread_id]
        : item.root_message_id != null ? ['root', item.root_message_id]
        : ['unknown-root', item.draft_msg_id, item.run_id];
    return JSON.stringify([landedWorkspaceKey(instance, item), item.conv_id, thread]);
}

function latestLandedEntries() {
    const entries = [...landedSnapshots.values()].flatMap(snapshot => snapshot.items.map(item => ({
        ...snapshot, item, key: landedItemKey(snapshot.instance, item),
        slot: landedSlotKey(snapshot.instance, item), workspace: landedWorkspaceKey(snapshot.instance, item),
    }))).sort((a, b) => ((b.item.completed_at ?? b.item.created_at) - (a.item.completed_at ?? a.item.created_at))
        || ((b.item.draft_msg_id || 0) - (a.item.draft_msg_id || 0)));
    const latest = new Map();
    for (const entry of entries) if (!latest.has(entry.slot)) latest.set(entry.slot, entry);
    // Group BEFORE applying review marks: hiding the latest must not reveal older turns.
    return [...latest.values()];
}

function setLandedReviewed(key, reviewed) {
    if (reviewed) landedReviewed.set(key, Date.now());
    else landedReviewed.delete(key);
    while (landedReviewed.size > 2000) landedReviewed.delete(landedReviewed.keys().next().value);
    try {
        localStorage.setItem(landedReviewStorageKey, JSON.stringify([...landedReviewed]));
        landedStorageWarning = '';
    } catch (_) {
        landedStorageWarning = 'Review storage unavailable; marks may last only until this page closes.';
    }
    renderLandedInbox();
}

function updateLandedSupervision(instances) {
    const known = new Set(instances.map(instance => instance.instance));
    for (const name of landedSnapshots.keys()) {
        if (!known.has(name)) landedSnapshots.delete(name);
    }
    for (const instance of instances) {
        const supported = instance.available && Array.isArray(instance.landed)
            && instance.landed.every(item => item && Number.isInteger(item.conv_id)
                && typeof item.run_id === 'string' && Number.isFinite(item.created_at)
                && typeof item.status === 'string');
        const prior = landedSnapshots.get(instance.instance);
        landedSnapshots.set(instance.instance, supported
            ? { instance, items: instance.landed.slice(0, 100), available: true }
            : { instance: prior?.instance || instance, items: prior?.items || [], available: false,
                error: instance.available ? 'Completion history unsupported or invalid; main/admin code may need updating.'
                    : 'Instance unavailable; last-known history only.' });
    }
    renderLandedInbox();
}

function markLandedUnavailable() {
    for (const snapshot of landedSnapshots.values()) {
        snapshot.available = false;
        snapshot.error = 'History unavailable; last-known entries only.';
    }
    renderLandedInbox('Completion history unavailable — not confirmed empty.');
}

function renderLandedInbox(error = '') {
    const list = document.getElementById('landed-list');
    if (!list) return;
    const showReviewed = document.getElementById('landed-show-reviewed').checked;
    const entries = latestLandedEntries();
    const unread = entries.filter(entry => !landedReviewed.has(entry.key)).length;
    document.getElementById('landed-count').textContent = unread ? `(${unread} not reviewed)` : '';
    const notices = [...landedSnapshots.values()].filter(snapshot => !snapshot.available)
        .map(snapshot => `${snapshot.instance.label || snapshot.instance.instance}: ${snapshot.error}`);
    document.getElementById('landed-status').textContent = [error, ...notices, landedStorageWarning].filter(Boolean).join(' · ');
    const visible = entries.filter(entry => showReviewed || !landedReviewed.has(entry.key));
    const workspaces = new Map();
    for (const entry of visible) {
        if (!workspaces.has(entry.workspace)) workspaces.set(entry.workspace, []);
        workspaces.get(entry.workspace).push(entry);
    }
    for (const node of [...list.children]) if (!workspaces.has(node.dataset.workspaceKey)) node.remove();
    const labels = { completed: 'Completed', finished: 'Finished — outcome not recorded',
        failed: 'Failed', error: 'Failed', partial_failed: 'Partially failed', interrupted: 'Interrupted',
        cancelled: 'Cancelled', orphaned: 'Orphaned', launch_failed: 'Launch failed' };
    let groupIndex = 0;
    for (const [workspace, rows] of workspaces) {
        let group = [...list.children].find(node => node.dataset.workspaceKey === workspace);
        if (!group) {
            group = document.createElement('section');
            group.className = 'landed-workspace';
            group.dataset.workspaceKey = workspace;
            group.innerHTML = '<h3 class="landed-workspace-title"></h3><div class="landed-workspace-items"></div>';
        }
        const first = rows[0];
        group.querySelector('h3').textContent = `${first.instance.label || first.instance.instance} · ${first.item.project_dir || first.item.title || 'No workspace folder'}`;
        const cards = group.querySelector('.landed-workspace-items');
        const slots = new Set(rows.map(entry => entry.slot));
        for (const card of [...cards.children]) if (!slots.has(card.dataset.landedSlot)) card.remove();
        rows.forEach(({instance, item, key, slot, available}, index) => {
            let card = [...cards.children].find(node => node.dataset.landedSlot === slot);
            if (!card) {
                card = document.createElement('article');
                card.dataset.landedSlot = slot;
                card.className = 'generation-card landed-card';
            }
            card.dataset.landedKey = key;
            card.classList.toggle('stale', !available);
            const reviewed = landedReviewed.has(key);
            const ended = Number.isFinite(item.completed_at);
            const timestamp = new Date((ended ? item.completed_at : item.created_at) * 1000).toLocaleString();
            const timing = ended ? (item.completion_time_estimated ? 'Approx. finished' : 'Finished') : 'End time not recorded · Started';
            const html = `<div class="activity-identity">${supervisionIdentity(instance, item)}</div>
                ${item.kind !== 'child' ? `<div class="activity-meta">${item.root_message_id != null ? `Root #${esc(item.root_message_id)} · ` : ''}Latest landed turn</div>` : ''}
                <div class="activity-meta ${['completed', 'finished'].includes(item.status) ? '' : 'permission-warning'}">${esc(item.kind === 'child' ? 'Child' : 'Turn')} · ${esc(labels[item.status] || item.status)} · ${esc(timing)} ${esc(timestamp)}</div>
                ${item.kind === 'child' ? '<div class="empty-note">Result available in this conversation’s Agents panel.</div>' : ''}
                <div class="activity-links">${supervisionLinks(instance, item)}
                    <button type="button" class="btn btn-ghost" data-review>${reviewed ? 'Mark not reviewed' : 'Mark reviewed'}</button></div>`;
            if (card._html !== html || card._reviewKey !== key) {
                card.innerHTML = html;
                card._html = html;
                card._reviewKey = key;
                card.querySelector('[data-review]').addEventListener('click', () => setLandedReviewed(key, !reviewed));
            }
            if (cards.children[index] !== card) cards.insertBefore(card, cards.children[index] || null);
        });
        if (list.children[groupIndex] !== group) list.insertBefore(group, list.children[groupIndex] || null);
        groupIndex++;
    }
    const empty = document.getElementById('landed-empty');
    empty.hidden = visible.length > 0;
    empty.textContent = error || notices.length ? 'History unavailable for some instances — not confirmed empty.'
        : unread === 0 && entries.length ? 'All entries in this recent-history window are marked reviewed.'
        : 'No recorded finished turns in the recent-history window.';
}

document.getElementById('landed-show-reviewed')?.addEventListener('change', () => renderLandedInbox());
window.addEventListener('storage', event => {
    if (event.key !== landedReviewStorageKey && event.key !== null) return;
    landedReviewed = readLandedReviews();
    renderLandedInbox();
});
