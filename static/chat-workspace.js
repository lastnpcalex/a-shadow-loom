/* Presentation only: navigation and actions retain their existing contracts. */
async function workspaceRemovePin(pin) {
    if (!await showLoomConfirm({title: 'Remove pin?', message: `Remove “${pin.label || 'Pinned branch'}” from Pins? The conversation and messages remain.`, confirmLabel: 'Remove'})) return;
    try {
        await API.del(pin.fromBookmark ? `/api/bookmarks/${pin.id}` : `/api/branch-pins/${pin.id}`);
        if (pin.fromBookmark) State.bookmarks = State.bookmarks.filter(item => item.id !== pin.id);
        else State.branchPins = State.branchPins.filter(item => item.id !== pin.id);
        updateBranchNavigation(); scheduleWorkspaceRender();
        refreshOpenBookmarksPanels();
    } catch (error) { showToast(error.detail || 'Failed to remove pin', 'error'); }
}

function workspaceRenamePin(pin, destination = document.getElementById('workspace-branches')) {
    const overlay = document.createElement('div'); overlay.className = 'workspace-pin-editor'; overlay.dataset.convId = State.currentConvId;
    document.querySelector('.workspace-pin-editor')?.remove();
    const form = document.createElement('form'); form.className = 'loom-confirm-dialog workspace-pin-dialog';
    form.setAttribute('aria-label', pin.isNew ? 'Pin branch' : 'Rename pin');
    form.innerHTML = '<h3>Rename pin</h3><input aria-label="Pin label" maxlength="120" required><div class="loom-confirm-actions"><button type="button" class="btn-secondary">Cancel</button><button type="submit" class="btn-primary">Save</button></div>';
    const input = form.querySelector('input'); input.value = pin.label || '';
    form.querySelector('h3').textContent = pin.isNew ? 'Pin branch' : 'Rename pin';
    const previous = document.activeElement;
    const close = () => { overlay.remove(); scheduleWorkspaceRender(); refreshOpenBookmarksPanels(); if (previous.isConnected) previous.focus(); };
    form.querySelector('[type="button"]').onclick = close;
    overlay.onclick = event => { if (event.target === overlay) close(); };
    overlay.onkeydown = event => {
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); }
        if (event.key === 'Tab') {
            const controls = [...form.querySelectorAll('input, button')]; const index = controls.indexOf(document.activeElement);
            event.preventDefault(); controls[(index + (event.shiftKey ? controls.length - 1 : 1)) % controls.length].focus();
        }
    };
    form.onsubmit = async event => {
        event.preventDefault(); const label = input.value.trim(); if (!label) return;
        const save = form.querySelector('[type="submit"]'); save.disabled = true;
        try {
            const saved = pin.isNew
                ? await API.post(`/api/conversations/${State.currentConvId}/branch-pins`, {message_id: pin.message_id, label})
                : await API.put(pin.fromBookmark ? `/api/bookmarks/${pin.id}` : `/api/branch-pins/${pin.id}`, pin.fromBookmark ? {description: label} : {label});
            const collection = pin.fromBookmark ? State.bookmarks : State.branchPins;
            const current = collection.find(item => item.id === pin.id);
            if (current) Object.assign(current, saved, pin.fromBookmark ? {description: label} : {label});
            if (pin.isNew) State.branchPins = [saved, ...State.branchPins.filter(item => item.message_id !== saved.message_id)];
            updateBranchNavigation(); scheduleWorkspaceRender(); close();
        } catch (error) { showToast(error.detail || 'Failed to rename pin', 'error'); save.disabled = false; }
    };
    overlay.append(form); destination.prepend(overlay); input.focus(); input.select();
}

let _workspacePinsOnly = false;
function workspaceOpenPins() {
    _workspacePinsOnly = true;
    document.getElementById('branch-pins-panel').classList.add('hidden');
    document.getElementById('workspace-branch-filter').value = '';
    setWorkspaceLeftMode(false); workspaceSetOpen('left', true);
    renderChatWorkspace();
}

let _workspaceRenderPending = false;
let _workspaceArtifactEpoch = 0;
let _workspaceArtifactConv = null;
let _casualSendBusy = false;
let _casualChatEpoch = 0;
let _casualDraftId = null;
let _casualCreation = null;
let _workspaceFilesEpoch = 0;
let _workspaceNavigationEpoch = 0;
let _workspaceInputAnchor = null;
let _workspaceAgentAnchor = null;
let _workspaceGoalsOpen = false;
let _workspaceGoalsConversation = null;
let _workspaceCanvasOpen = false;
let _workspaceCanvasWidth = Number(localStorage.getItem('exo-canvas-width')) || 560;
let _workspaceBranchSpaceId = null;

async function openExoHomeChat() {
    let conv = State.conversations.find(item => item.title === 'ExoHome'
        && /[\\/]ExoHome[\\/]?$/i.test(item.project_dir || ''));
    if (!conv) {
        const model = (_ccModelsCache || []).flatMap(group => group.models || [])
            .find(item => item.value && !item.value.startsWith('llama:'));
        if (!model) throw new Error('No model is available for ExoHome');
        const request = {request_id: localStorage.getItem('exo-home-request') || 'exo-home-default-v1',
            title: 'ExoHome', cc_model: model.value, cc_effort: model.effort || 'high'};
        try { conv = await API.post('/api/casual-chats', request); }
        catch (error) {
            if (error.status !== 410) throw error;
            request.request_id = crypto.randomUUID();
            localStorage.setItem('exo-home-request', request.request_id);
            conv = await API.post('/api/casual-chats', request);
        }
        State.conversations.unshift(conv);
    }
    await loadConversation(conv.id, {newThread: true});
}

function restoreExoHomeBlankDraft() {
    const conv = State.currentConv;
    if (conv?.title !== 'ExoHome' || State.loomCursor?.leaf_message_id != null) return;
    let draft;
    try { draft = JSON.parse(sessionStorage.getItem('exo-home-blank-draft') || sessionStorage.getItem('exo-casual-draft') || 'null'); } catch (_) {}
    if (draft && State.currentConvId === conv.id) {
        document.getElementById('user-input').value = draft.text || '';
        State.pendingImages = draft.pendingImages || draft.images || [];
        document.getElementById('describe-context').value = draft.describeContext || draft.describe || '';
        renderImagePreviews();
        autoResizeTextarea();
        saveComposerDraft();
        sessionStorage.removeItem('exo-casual-draft');
    }
}

function saveExoHomeBlankDraft() {
    if (State.currentConv?.title !== 'ExoHome') return;
    if (State.loomCursor?.leaf_message_id != null) {
        sessionStorage.removeItem('exo-home-blank-draft');
        return;
    }
    sessionStorage.setItem('exo-home-blank-draft', JSON.stringify({
        text: document.getElementById('user-input').value,
        pendingImages: State.pendingImages || [],
        describeContext: document.getElementById('describe-context').value,
    }));
}

function syncWorkspaceLayout() {
    const agents = document.getElementById('agent-manager-anchor');
    if (!_workspaceAgentAnchor) {
        _workspaceAgentAnchor = document.createComment('shared agent manager home');
        agents.before(_workspaceAgentAnchor);
    }
    const agentPane = document.getElementById('workspace-agent-pane');
    if (State.currentView === 'chat') {
        const controls = _agentManagerTarget();
        if (controls) controls.insertBefore(agents, controls.querySelector('#btn-canvas-toggle'));
    }
    else if (agentPane?.contains(agents)) _workspaceAgentAnchor.after(agents);
    if (_workspaceGoalsConversation !== State.currentConvId) {
        _workspaceGoalsConversation = State.currentConvId;
        _workspaceGoalsOpen = false;
    }
    const hasGoal = !!State.currentConv?.codex_goal_objective?.trim();
    document.getElementById('workspace-goals')?.classList.toggle('hidden', State.currentConv?.mode !== 'codex');
    if (agentPane) agentPane.hidden = !_workspaceGoalsOpen || !hasGoal || State.currentView !== 'chat' || State.currentConv?.mode !== 'codex';
    document.getElementById('workspace-goals')?.setAttribute('aria-expanded', String(!!agentPane && !agentPane.hidden));
    const input = document.getElementById('input-area');
    if (!_workspaceInputAnchor) {
        _workspaceInputAnchor = document.createComment('shared composer home');
        input.before(_workspaceInputAnchor);
    }
    const column = document.getElementById('workspace-chat-column');
    const latest = document.getElementById('jump-to-latest');
    if (latest && latest.parentNode !== column) column.appendChild(latest);
    if (State.currentView === 'chat') {
        if (input.parentNode !== column) column.appendChild(input);
    } else if (input.parentNode === column) _workspaceInputAnchor.after(input);
    const pane = document.getElementById('workspace-canvas-pane');
    const room = document.getElementById('chat-workspace-body').clientWidth
        - ['left', 'right', 'agent-pane', 'message-pins'].reduce((sum, side) => {
            const node = document.getElementById(`workspace-${side}`);
            return sum + (!node || node.hidden || isMobileDevice() ? 0 : node.offsetWidth);
        }, 0) - (document.getElementById('workspace-left').hidden && innerWidth >= 1400 ? 638 : 328);
    if (!isMobileDevice() && State.currentView === 'chat' && room < 240) _workspaceCanvasOpen = false;
    pane.hidden = !_workspaceCanvasOpen || State.currentView !== 'chat' || !State.currentConvId;
    if (!pane.hidden && !isMobileDevice()) pane.style.width = `${Math.min(Math.max(240, _workspaceCanvasWidth), room)}px`;
    const separator = document.getElementById('workspace-canvas-resizer');
    separator.hidden = pane.hidden || isMobileDevice();
    separator.setAttribute('aria-valuenow', String(Math.round(pane.clientWidth)));
    separator.setAttribute('aria-valuemin', '240');
    separator.setAttribute('aria-valuemax', String(Math.max(240, Math.round(room))));
    document.getElementById('workspace-toggle-canvas').setAttribute('aria-expanded', String(!pane.hidden));
    document.getElementById('workspace-toggle-canvas').disabled = !State.currentConvId;
    const view = document.getElementById('view-chat');
    view.style.setProperty('--workspace-composer-height', `${input.offsetHeight}px`);
    const bounds = view.getBoundingClientRect();
    const flank = State.currentView === 'chat' && !isMobileDevice() && bounds.width >= 1400;
    view.classList.toggle('workspace-controls-flanking', flank);
    if (flank) {
        const columnBounds = column.getBoundingClientRect();
        view.style.setProperty('--workspace-compose-left', `${Math.max(24, 310 - (columnBounds.left - bounds.left) + 24)}px`);
        view.style.setProperty('--workspace-compose-right', `${Math.max(24, 280 - (bounds.right - columnBounds.right) + 24)}px`);
        view.style.setProperty('--workspace-controls-left', '310px');
        view.style.setProperty('--workspace-controls-right', '280px');
        view.style.setProperty('--workspace-composer-height', `${input.offsetHeight}px`);
    }
    // Align the popup with message cards, including asymmetric sidebar padding.
    const messages = document.getElementById('messages');
    if (State.currentView === 'chat' && messages) {
        const inputBounds = input.getBoundingClientRect();
        const messageBounds = messages.getBoundingClientRect();
        const messageStyle = getComputedStyle(messages);
        const paddingLeft = parseFloat(messageStyle.paddingLeft) || 0;
        const paddingRight = parseFloat(messageStyle.paddingRight) || 0;
        const mobile = isMobileDevice();
        input.style.setProperty('--workspace-agent-panel-left', `${mobile ? 0 : messageBounds.left + paddingLeft - inputBounds.left}px`);
        input.style.setProperty('--workspace-agent-panel-width', `${mobile ? inputBounds.width : Math.max(0, messageBounds.width - paddingLeft - paddingRight)}px`);
    }
}

function setWorkspaceCanvasOpen(open) {
    if (open) {
        if (isMobileDevice()) window.closeWorkspaceLeaves?.();
        _closeAgentManager();
        workspaceSetOpen('right', false, false);
    }
    if (open && (isMobileDevice() || document.getElementById('chat-workspace-body').clientWidth < 1100)) {
        for (const side of ['left', 'right']) workspaceSetOpen(side, false, false);
    }
    _workspaceCanvasOpen = open;
    syncWorkspaceLayout();
    syncWorkspaceCanvas();
}

function syncCasualSettings() {
    const model = document.getElementById('workspace-new-model');
    const effort = document.getElementById('workspace-new-effort');
    if (_casualCreation) {
        model.value = _casualCreation.cc_model;
        effort.value = _casualCreation.cc_effort;
    }
    model.disabled = _casualSendBusy || !!_casualCreation;
    effort.disabled = model.disabled || !_supportsEffortValue(model.value);
}

function saveCasualDraft() {
    if (State.currentConvId || State.currentView !== 'chat') return;
    sessionStorage.setItem('exo-casual-draft', JSON.stringify({
        draft_id: _casualDraftId,
        text: document.getElementById('user-input').value,
        images: State.pendingImages || [],
        describe: document.getElementById('describe-context')?.value || '',
        creation: _casualCreation,
        model: document.getElementById('workspace-new-model').value,
        effort: document.getElementById('workspace-new-effort').value,
    }));
}

function startCasualChat({ fresh = true } = {}) {
    if (_casualSendBusy) { showToast('Wait for the new chat to finish preparing.', 'warning'); return; }
    if (State.currentConvId) saveComposerDraft();
    ++_conversationLoadRequestSeq;
    hideLoading();
    _casualChatEpoch++;
    if (fresh) sessionStorage.removeItem('exo-casual-draft');
    let draft = {};
    try { draft = JSON.parse(sessionStorage.getItem('exo-casual-draft') || '{}'); } catch (_) {}
    _casualDraftId = draft.draft_id || (globalThis.crypto?.randomUUID ? crypto.randomUUID() : `draft-${Date.now()}-${Math.random()}`);
    _casualCreation = draft.creation || null;
    if (State.ws) { State.ws._replaced = true; State.ws.close(); State.ws = null; }
    State.currentConvId = null;
    State.currentConv = null;
    State.loomCursor = null;
    State.messages = [];
    State.treeData = [];
    State.branchNames = {};
    State.agentRuns = [];
    State.activeGenerations = {};
    State.pendingImages = draft.images || [];
    State.pendingReply = null;
    if (typeof _clearTerminalStreamState === 'function') _clearTerminalStreamState({ clearParallel: true });
    State._skipLoadOnChat = true;
    switchView('chat');
    sessionStorage.removeItem('loom-tab-conv');
    renderMessages();
    const model = document.getElementById('workspace-new-model');
    model.replaceChildren();
    for (const group of _ccModelsCache || []) {
        const options = document.createElement('optgroup'); options.label = group.label || group.provider || 'Models';
        for (const item of group.models || []) {
            const option = document.createElement('option'); option.value = item.value; option.textContent = item.label;
            if (item.effort) option.dataset.effort = item.effort;
            options.appendChild(option);
        }
        model.appendChild(options);
    }
    const preferred = draft.creation?.cc_model || draft.model || localStorage.getItem('exo-casual-model');
    if ([...model.options].some(option => option.value === preferred)) model.value = preferred;
    document.getElementById('workspace-new-effort').value = draft.creation?.cc_effort || draft.effort || 'high';
    _syncEffortOptionsForModel('workspace-new-effort', model.value);
    document.getElementById('workspace-new-effort').disabled = !_supportsEffortValue(model.value);
    syncCasualSettings();
    document.getElementById('user-input').value = draft.text || '';
    document.getElementById('describe-context').value = draft.describe || '';
    for (const id of ['cc-inline-controls', 'weave-inline-controls', 'local-inline-controls']) document.getElementById(id)?.classList.add('hidden');
    if (typeof renderImagePreviews === 'function') renderImagePreviews();
    autoResizeTextarea();
    scheduleWorkspaceRender();
}

async function sendFirstCasualMessage() {
    if (_casualSendBusy) return;
    const text = document.getElementById('user-input').value;
    if (!text.trim() && !State.pendingImages.length) return;
    const model = document.getElementById('workspace-new-model').value;
    if (!model) { showToast('Choose a model before sending.', 'error'); return; }
    _casualSendBusy = true;
    const composer = document.getElementById('user-input');
    composer.readOnly = true;
    document.getElementById('file-input').disabled = true;
    const epoch = _casualChatEpoch;
    const navigationEpoch = _workspaceNavigationEpoch;
    let preparedConvId = null;
    const images = [...State.pendingImages];
    const describe = document.getElementById('describe-context')?.value || '';
    if (!_casualCreation) _casualCreation = {
        request_id: globalThis.crypto?.randomUUID ? crypto.randomUUID() : `chat-${Date.now()}-${Math.random().toString(36).slice(2)}`, cc_model: model,
        cc_effort: document.getElementById('workspace-new-effort').value,
        title: text.trim().slice(0, 80) || 'New chat',
    };
    syncCasualSettings();
    saveCasualDraft();
    try {
        const conv = await API.post('/api/casual-chats', _casualCreation);
        if (epoch !== _casualChatEpoch || navigationEpoch !== _workspaceNavigationEpoch || State.currentConvId || State.currentView !== 'chat') return;
        if (!State.conversations.some(item => item.id === conv.id)) State.conversations.unshift(conv);
        preparedConvId = conv.id;
        const loaded = await loadConversation(conv.id, { isCurrent: () => navigationEpoch === _workspaceNavigationEpoch });
        if (!loaded || epoch !== _casualChatEpoch || String(State.currentConvId) !== String(conv.id)) return;
        switchView('chat');
        document.getElementById('user-input').value = text;
        State.pendingImages = images;
        if (typeof renderImagePreviews === 'function') renderImagePreviews();
        document.getElementById('describe-context').value = describe;
        saveComposerDraft();
        localStorage.setItem('exo-casual-model', _casualCreation.cc_model);
        sessionStorage.removeItem('exo-casual-draft');
        _casualCreation = null;
        composer.readOnly = false;
        document.getElementById('file-input').disabled = false;
        await sendMessage();
    } catch (error) {
        if (preparedConvId != null && String(State.currentConvId) === String(preparedConvId)
            && navigationEpoch === _workspaceNavigationEpoch && epoch === _casualChatEpoch) {
            // The durable casual draft survives partial conversation loading.
            // Return to it without creating a second request or losing attachments.
            _casualSendBusy = false;
            startCasualChat({ fresh: false });
        }
        if (!State.currentConvId) saveCasualDraft();
        showToast(error.detail || error.message || 'Could not prepare the chat', 'error');
    } finally {
        composer.readOnly = false;
        document.getElementById('file-input').disabled = false;
        _casualSendBusy = false;
        syncCasualSettings();
        scheduleWorkspaceRender();
    }
}

function scheduleWorkspaceRender() {
    if (_workspaceRenderPending) return;
    _workspaceRenderPending = true;
    queueMicrotask(() => { _workspaceRenderPending = false; renderChatWorkspace(); });
}

function workspaceSetOpen(side, open, remember = true) {
    const panel = document.getElementById(`workspace-${side}`);
    if (!panel) return;
    panel.hidden = !open;
    if (open && (side === 'left' || isMobileDevice())) window.closeWorkspaceLeaves?.();
    if (open && (side === 'right' || isMobileDevice())) _closeAgentManager();
    document.getElementById(`workspace-toggle-${side}`).setAttribute('aria-expanded', String(open));
    if (side === 'left') {
        const conversations = panel.classList.contains('conversations-mode');
        document.getElementById('workspace-toggle-left').setAttribute('aria-expanded', String(open && !conversations));
        document.getElementById('workspace-projects').setAttribute('aria-expanded', String(open && conversations));
    }
    if (isMobileDevice() && open) workspaceSetOpen(side === 'left' ? 'right' : 'left', false, false);
    if (remember && !isMobileDevice()) localStorage.setItem(`exo-sidebar-${side}`, String(open));
    if (side === 'right') {
        if (open) refreshWorkspaceArtifacts();
        syncWorkspaceCanvas();
    }
    syncWorkspaceLayout();
    syncWorkspaceCanvas();
}

function setWorkspaceLeftMode(conversations) {
    document.getElementById('workspace-left').classList.toggle('conversations-mode', conversations);
    document.getElementById('workspace-project-panel').hidden = !conversations;
    workspaceSetOpen('left', true);
}

function workspaceButton(label, action, current = false, detail = '') {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'workspace-item';
    button.setAttribute('aria-current', String(current));
    button.textContent = label;
    if (detail) { const small = document.createElement('small'); small.textContent = detail; button.appendChild(small); }
    button.addEventListener('click', async () => {
        try { await action(); if (isMobileDevice()) workspaceSetOpen('left', false, false); }
        catch (error) { showToast(error.message || String(error), 'error'); }
    });
    return button;
}

function openConversationSpace(convId) {
    const epoch = _workspaceNavigationEpoch;
    return loadConversation(convId, { newThread: true, isCurrent: () => epoch === _workspaceNavigationEpoch });
}

function workspaceRenameConversation(conv) {
    const overlay = document.createElement('div'); overlay.className = 'loom-confirm-overlay';
    const form = document.createElement('form'); form.className = 'loom-confirm-dialog workspace-pin-dialog';
    form.setAttribute('role', 'dialog'); form.setAttribute('aria-modal', 'true'); form.setAttribute('aria-label', 'Rename conversation');
    form.innerHTML = '<h3>Rename conversation</h3><input aria-label="Conversation name" required><div class="loom-confirm-actions"><button type="button" class="btn-secondary">Cancel</button><button type="submit" class="btn-primary">Save</button></div>';
    const input = form.querySelector('input'); input.value = conv.title || '';
    const previous = document.activeElement;
    const close = () => { overlay.remove(); if (previous?.isConnected) previous.focus(); };
    form.querySelector('[type="button"]').onclick = close;
    overlay.onclick = event => { if (event.target === overlay) close(); };
    overlay.onkeydown = event => {
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); }
        if (event.key === 'Tab') {
            const controls = [...form.querySelectorAll('input, button')];
            const index = controls.indexOf(document.activeElement);
            event.preventDefault(); controls[(index + (event.shiftKey ? controls.length - 1 : 1)) % controls.length].focus();
        }
    };
    form.onsubmit = async event => {
        event.preventDefault(); const title = input.value.trim(); if (!title) return;
        const save = form.querySelector('[type="submit"]'); save.disabled = true;
        try {
            const updated = await API.put(`/api/conversations/${conv.id}`, {title});
            conv.title = updated.title;
            if (String(State.currentConvId) === String(conv.id)) {
                State.currentConv.title = updated.title;
                document.getElementById('conv-title').textContent = updated.title;
                if (typeof syncBrowserTabIdentity === 'function') syncBrowserTabIdentity();
            }
            renderConversationList(); scheduleWorkspaceRender(); close();
        } catch (error) { showToast(error.detail || 'Failed to rename conversation', 'error'); save.disabled = false; }
    };
    overlay.append(form); document.body.append(overlay); input.focus(); input.select();
}

function renderChatWorkspace() {
    const conversations = document.getElementById('workspace-conversations');
    if (!conversations) return;
    // Each desktop ExoHome entry starts with its branches visible. A manual
    // close remains respected during that visit; phones stay collapsed.
    if (State.currentView !== 'chat') _workspaceBranchSpaceId = null;
    else if (State.currentConv?.id === State.currentConvId && _workspaceBranchSpaceId !== State.currentConvId) {
        _workspaceBranchSpaceId = State.currentConvId;
        if (!isMobileDevice() && /[\\/]ExoHome(?:[\\/]|$)/i.test(State.currentConv?.project_dir || '')) {
            document.getElementById('workspace-left').classList.remove('conversations-mode');
            document.getElementById('workspace-project-panel').hidden = true;
            workspaceSetOpen('left', true, false);
        }
    }
    document.getElementById('workspace-new-chat-settings').hidden = !!State.currentConvId;
    document.getElementById('workspace-tree').disabled = !State.currentConvId;
    document.getElementById('workspace-branch-heading').textContent = State.currentConv?.title || 'ExoHome';
    document.getElementById('workspace-pin-current').disabled = State.loomCursor?.leaf_message_id == null;
    const treePin = document.getElementById('workspace-pin-tree');
    treePin.disabled = !State.currentConvId;
    treePin.textContent = State.currentConv?.starred ? 'Unpin Tree' : 'Pin Tree';
    treePin.setAttribute('aria-pressed', String(!!State.currentConv?.starred));
    syncWorkspaceLayout();
    const filter = document.getElementById('workspace-conversation-filter').value.toLowerCase();
    conversations.replaceChildren();
    const matching = State.conversations.filter(c => `${c.title} ${c.folder || ''} ${c.project_dir || ''}`.toLowerCase().includes(filter));
    const groups = [
        ['__starred__', 'Pins', matching.filter(c => c.starred && !c.nsfw)],
        ['__nsfw__', 'Not for Work', matching.filter(c => c.nsfw)],
        ...[...new Set(matching.map(c => c.folder).filter(Boolean))].sort((a, b) => a.localeCompare(b))
            .map(folder => [folder, folder, matching.filter(c => c.folder === folder)]),
        ['__unfiled__', 'Unfiled', matching.filter(c => !c.folder)],
    ];
    for (const [key, label, items] of groups) {
        if (!items.length) continue;
        const group = document.createElement('details');
        group.className = 'workspace-conversation-group';
        group.open = !!filter || !isFolderCollapsed(key);
        const summary = document.createElement('summary');
        summary.textContent = `${label} (${items.length})`;
        group.appendChild(summary);
        group.addEventListener('toggle', () => {
            if (filter) return;
            State.convFolderCollapsed[key] = !group.open;
            saveFolderCollapsed();
        });
        for (const conv of items.slice().sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0))) {
            const row = document.createElement('div'); row.className = 'workspace-branch-row';
            row.dataset.conversationId = conv.id;
            row.appendChild(workspaceButton(conv.title || 'Untitled', async () => {
                const loaded = await openConversationSpace(conv.id);
                if (!loaded || String(State.currentConvId) !== String(conv.id)) return;
                setWorkspaceLeftMode(false);
            }, String(conv.id) === String(State.currentConvId), conv.project_dir || conv.mode));
            if (conv.starred) {
                const edit = document.createElement('button'); edit.type = 'button'; edit.className = 'workspace-row-pin';
                edit.textContent = '✎'; edit.title = 'Rename conversation'; edit.setAttribute('aria-label', 'Rename conversation');
                edit.onclick = () => workspaceRenameConversation(conv); row.append(edit);
            }
            const pin = document.createElement('button'); pin.type = 'button'; pin.className = 'workspace-row-pin';
            pin.textContent = conv.starred ? '✕' : '⬡';
            pin.title = conv.starred ? 'Remove conversation pin' : 'Pin conversation'; pin.setAttribute('aria-label', pin.title);
            pin.onclick = async () => {
                pin.disabled = true;
                try {
                    const starred = conv.starred ? 0 : 1;
                    await API.put(`/api/conversations/${conv.id}`, {starred}); conv.starred = starred;
                    if (String(State.currentConvId) === String(conv.id) && State.currentConv) State.currentConv.starred = starred;
                    renderConversationList(); scheduleWorkspaceRender();
                } catch (error) { showToast(error.detail || 'Failed to update conversation pin', 'error'); pin.disabled = false; }
            };
            row.append(pin); group.append(row);
        }
        conversations.appendChild(group);
    }
    const branches = document.getElementById('workspace-branches');
    const activeEditor = branches.querySelector('.workspace-pin-editor');
    if (activeEditor && Number(activeEditor.dataset.convId) === Number(State.currentConvId)) return;
    branches.replaceChildren();
    const nodes = State.treeData || [];
    const byId = new Map(nodes.map(n => [n.id, n]));
    const parents = new Set(nodes.map(n => n.parent_id).filter(id => id != null));
    const rootFor = node => {
        const seen = new Set();
        while (byId.has(node.parent_id) && !seen.has(node.id)) { seen.add(node.id); node = byId.get(node.parent_id); }
        return node;
    };
    const query = document.getElementById('workspace-branch-filter').value.trim().toLowerCase();
    const section = label => {
        const heading = document.createElement('h3'); heading.textContent = label; branches.appendChild(heading);
    };
    const savedPins = State.branchPins || [];
    const pins = savedPins.filter(pin => byId.has(pin.message_id));
    const matchingPins = pins.filter(pin => `${pin.label} ${byId.get(pin.message_id)?.preview || ''} ${rootFor(byId.get(pin.message_id)).preview || ''}`.toLowerCase().includes(query));
    if (matchingPins.length || _workspacePinsOnly) {
        section('Pinned');
        if (!matchingPins.length) { const empty = document.createElement('p'); empty.textContent = 'No pins yet. Use Pin branch to save the current branch.'; branches.append(empty); }
        for (const pin of matchingPins) {
            const button = workspaceButton(pin.label || 'Pinned branch', () => navigateToBranchPath(`msg:${pin.message_id}`),
                Number(State.loomCursor?.leaf_message_id) === Number(pin.message_id));
            button.dataset.pinId = pin.id;
            const row = document.createElement('div'); row.className = 'workspace-branch-row workspace-pinned-row';
            const edit = document.createElement('button'); edit.type = 'button'; edit.className = 'workspace-row-pin'; edit.textContent = '✎'; edit.title = 'Rename pin'; edit.setAttribute('aria-label', 'Rename pin'); edit.onclick = () => workspaceRenamePin(pin);
            const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'workspace-row-pin'; remove.textContent = '✕'; remove.title = 'Remove pin'; remove.setAttribute('aria-label', 'Remove pin'); remove.onclick = () => workspaceRemovePin(pin);
            row.append(button, edit, remove); branches.appendChild(row);
        }
    }
    if (State.currentConvId && State.loomCursor?.leaf_message_id == null) {
        const blank = workspaceButton('New branch', () => {}, true, 'Ready for your first message');
        blank.dataset.blankBranch = 'true';
        branches.appendChild(blank);
    }
    const leaves = nodes.filter(n => !parents.has(n.id)).sort((a, b) =>
        Number(b.created_at || 0) - Number(a.created_at || 0) || Number(b.id) - Number(a.id));
    let group = null;
    for (const node of _workspacePinsOnly ? [] : leaves) {
        const root = rootFor(node);
        if (!`${root.preview || root.content || ''} ${node.preview || node.content || ''} ${State.branchNames[node.id] || ''}`.toLowerCase().includes(query)) continue;
        if (pins.some(pin => Number(pin.message_id) === Number(node.id))) continue;
        const label = Number(node.created_at || 0) >= Date.now() / 1000 - 7 * 86400 ? 'Recents' : 'Older';
        if (group !== label) { section(label); group = label; }
        const button = workspaceButton(
            String(root.preview || root.content || 'Untitled branch').slice(0, 80),
            () => navigateToBranchPath(`msg:${node.id}`),
            Number(State.loomCursor?.leaf_message_id) === Number(node.id),
            `${State.branchNames[node.id] || `Message ${node.id}`} · ${String(node.preview || node.content || '').slice(0, 90)}`);
        button.dataset.leafId = node.id;
        const row = document.createElement('div'); row.className = 'workspace-branch-row';
        const pinButton = document.createElement('button');
        pinButton.type = 'button'; pinButton.className = 'workspace-row-pin'; pinButton.textContent = '⬡';
        pinButton.title = 'Pin this branch'; pinButton.setAttribute('aria-label', `Pin branch ${State.branchNames[node.id] || node.id}`);
        pinButton.addEventListener('click', async () => {
            const convId = State.currentConvId;
            pinButton.disabled = true;
            try {
                const pin = await API.post(`/api/conversations/${convId}/branch-pins`, {
                    message_id: node.id,
                    label: String(root.preview || root.content || State.branchNames[node.id] || 'Pinned branch').slice(0, 120),
                });
                if (State.currentConvId === convId) {
                    State.branchPins = [pin, ...(State.branchPins || []).filter(item => item.message_id !== pin.message_id)];
                    updateBranchNavigation(); scheduleWorkspaceRender();
                }
                showToast('Branch pinned');
            } catch (error) { showToast(error.detail || 'Failed to pin branch', 'error'); pinButton.disabled = false; }
        });
        row.append(button, pinButton); branches.appendChild(row);
    }
    if (!branches.childNodes.length) branches.textContent = 'No branches yet. Send a message to begin.';
    document.getElementById('workspace-path').textContent = State.currentConv?.project_dir || 'No working directory selected.';
    const runs = State.agentRuns || [];
    document.getElementById('workspace-activity').textContent = `${runs.length} agent run${runs.length === 1 ? '' : 's'} · ${Object.keys(State.activeGenerations || {}).length} active generation(s)`;
    const lastReply = [...(State.messages || [])].reverse().find(message => message.role === 'assistant');
    const inputTokens = lastReply?.turn_input_tokens;
    const outputTokens = lastReply?.turn_output_tokens;
    document.getElementById('workspace-usage').textContent = inputTokens != null || outputTokens != null
        ? `Last reply usage: ${inputTokens ?? 'unknown'} input · ${outputTokens ?? 'unknown'} output tokens`
        : 'Reply usage is shown when reported by the provider.';
    document.getElementById('workspace-goals').classList.toggle('hidden', State.currentConv?.mode !== 'codex');
    syncWorkspaceCanvas();
    if (String(_workspaceArtifactConv) !== String(State.currentConvId)) refreshWorkspaceArtifacts();
}

async function refreshWorkspaceArtifacts() {
    const epoch = ++_workspaceArtifactEpoch;
    const convId = State.currentConvId;
    _workspaceArtifactConv = convId;
    _workspaceFilesEpoch++;
    document.getElementById('workspace-files').textContent = '';
    document.getElementById('workspace-git').textContent = 'No Git snapshot loaded.';
    const list = document.getElementById('workspace-artifacts');
    if (!list) return;
    list.textContent = '';
    if (!convId || !State.currentConv?.project_dir) { list.textContent = 'Select a workspace to see artifacts.'; return; }
    if (document.getElementById('workspace-right').hidden || State.currentView !== 'chat') { _workspaceArtifactConv = null; return; }
    refreshWorkspaceFiles();
    document.getElementById('workspace-git').textContent = 'Refresh Git to inspect this workspace.';
    list.textContent = 'Loading artifacts…';
    try {
        const data = await API.get(`/api/conversations/${convId}/artifacts`);
        if (epoch !== _workspaceArtifactEpoch || String(State.currentConvId) !== String(convId)) return;
        list.replaceChildren();
        for (const item of data.artifacts || []) list.appendChild(workspaceButton(item.name, async () => {
            const response = await fetch(`/api/conversations/${convId}/file?path=${encodeURIComponent(item.path)}`);
            if (!response.ok) throw new Error(`Could not preview artifact (${response.status})`);
            const content = await response.text();
            if (String(State.currentConvId) === String(convId)) previewArtifact(item.name, content);
        }));
        if (!list.childNodes.length) list.textContent = 'No planning artifacts yet.';
    } catch (error) {
        if (epoch === _workspaceArtifactEpoch) list.textContent = `Artifacts unavailable: ${error.message}`;
    }
}

async function refreshWorkspaceFiles(path = '') {
    const epoch = ++_workspaceFilesEpoch;
    const convId = State.currentConvId;
    const list = document.getElementById('workspace-files');
    list.textContent = '';
    if (!convId || !State.currentConv?.project_dir) { list.textContent = 'No workspace selected.'; return; }
    list.textContent = 'Loading files…';
    try {
        const data = await API.get(`/api/conversations/${convId}/workspace-files?path=${encodeURIComponent(path)}`);
        if (epoch !== _workspaceFilesEpoch || String(State.currentConvId) !== String(convId)) return;
        list.replaceChildren();
        if (data.path && data.path !== '.') list.appendChild(workspaceButton('Parent folder', () => refreshWorkspaceFiles(data.path.split('/').slice(0, -1).join('/'))));
        for (const entry of data.entries || []) {
            if (entry.directory) list.appendChild(workspaceButton(`${entry.name}/`, () => refreshWorkspaceFiles(entry.path)));
            else {
                const previewable = /\.(md|markdown|txt|png|jpg|jpeg|gif|webp|svg|bmp|ico)$/i.test(entry.name);
                const link = document.createElement('a'); link.className = 'workspace-item'; link.textContent = entry.name;
                if (previewable) {
                    link.href = `/api/conversations/${convId}/file?path=${encodeURIComponent(entry.path)}`;
                    link.target = '_blank'; link.rel = 'noopener';
                } else { link.removeAttribute('href'); link.title = 'Preview is available for images and text documents.'; }
                list.appendChild(link);
            }
        }
        if (!data.complete) { const note = document.createElement('p'); note.textContent = 'Partial listing: directory entry limit reached.'; list.appendChild(note); }
        if (!list.childNodes.length) list.textContent = 'This folder is empty.';
    } catch (error) { if (epoch === _workspaceFilesEpoch) list.textContent = `Files unavailable: ${error.message}`; }
}

async function refreshWorkspaceGit() {
    const convId = State.currentConvId;
    const node = document.getElementById('workspace-git');
    if (!convId || !State.currentConv?.project_dir) { node.textContent = 'No workspace selected.'; return; }
    node.textContent = 'Reading Git…';
    try {
        const data = await API.get(`/api/conversations/${convId}/workspace-git`);
        if (String(State.currentConvId) !== String(convId)) return;
        node.textContent = data.repository ? `${data.root}\n\nStatus\n${data.status || 'Clean'}\nChanges\n${data.diff_stat || 'No unstaged changes'}\nHistory\n${data.history}${data.complete ? '' : '\nPartial output: display limit reached.'}` : data.detail;
    } catch (error) { if (String(State.currentConvId) === String(convId)) node.textContent = `Git unavailable: ${error.message}`; }
}

function syncWorkspaceCanvas() {
    const frame = document.getElementById('workspace-canvas-iframe');
    if (!frame) return;
    const visible = State.currentView === 'chat' && !document.getElementById('workspace-right').hidden && !!State.currentConv?.canvas_enabled;
    frame.hidden = !visible;
    const enable = document.getElementById('workspace-canvas-enable');
    enable.hidden = !!State.currentConv?.canvas_enabled;
    enable.disabled = !State.currentConvId;
    if (visible) {
        const url = `${location.origin}/api/canvas/${State.currentConvId}/index.html`;
        if (!frame.src.startsWith(url)) frame.src = url;
    } else frame.removeAttribute('src');
    const paneFrame = document.getElementById('workspace-canvas-pane-iframe');
    const paneVisible = !document.getElementById('workspace-canvas-pane').hidden && !!State.currentConv?.canvas_enabled;
    paneFrame.hidden = !paneVisible;
    const paneEnable = document.getElementById('workspace-canvas-pane-enable');
    paneEnable.hidden = !!State.currentConv?.canvas_enabled;
    paneEnable.disabled = !State.currentConvId;
    if (paneVisible) {
        const url = `${location.origin}/api/canvas/${State.currentConvId}/index.html`;
        if (!paneFrame.src.startsWith(url)) paneFrame.src = url;
    } else paneFrame.removeAttribute('src');
    if (typeof _scheduleCanvasRevision === 'function') _scheduleCanvasRevision();
}

document.addEventListener('DOMContentLoaded', () => {
    const searchLink = document.createElement('a');
    searchLink.id = 'header-search-space';
    searchLink.href = _loomRouteBase + 'search';
    searchLink.title = 'Search Space';
    searchLink.setAttribute('aria-label', 'Search Space');
    searchLink.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10" cy="10" r="7"/><path d="m15 15 6 6"/></svg>';
    searchLink.addEventListener('click', event => {
        if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
        event.preventDefault();
        document.getElementById('btn-search-space-open').click();
    });
    document.getElementById('btn-bookmarks-global').before(searchLink);
    const clearNotifications = document.createElement('button');
    clearNotifications.id = 'notif-clear-notifications'; clearNotifications.type = 'button';
    clearNotifications.className = 'btn-mini';
    clearNotifications.textContent = 'Clear';
    clearNotifications.title = 'Clear notifications; pending approvals remain';
    clearNotifications.setAttribute('aria-label', 'Clear notifications');
    clearNotifications.addEventListener('click', event => {
        event.stopPropagation(); document.querySelector('#notif-list .notif-clear-all')?.click();
    });
    const notificationFooter = document.createElement('div');
    notificationFooter.className = 'notif-footer';
    notificationFooter.appendChild(clearNotifications);
    document.getElementById('notif-dropdown').appendChild(notificationFooter);
    document.getElementById('notif-close').addEventListener('click', event => {
        event.stopPropagation();
        document.getElementById('notif-dropdown').classList.add('hidden');
        _stopParallelActivityPolling();
        document.getElementById('notif-bell').focus();
    });
    document.getElementById('btn-bookmarks-global').title = 'Pins';
    document.getElementById('btn-bookmarks-global').setAttribute('aria-label', 'Pins');
    const pinsButton = document.getElementById('btn-bookmarks-global');
    const pinsPanel = document.getElementById('bookmarks-panel-global');
    pinsButton.setAttribute('aria-controls', pinsPanel.id);
    const positionPins = () => {
        const bounds = pinsButton.getBoundingClientRect();
        pinsPanel.style.top = `${bounds.bottom + 4}px`;
        pinsPanel.style.right = `${innerWidth <= 650 ? 8 : Math.max(8, innerWidth - bounds.right)}px`;
        pinsPanel.style.left = innerWidth <= 650 ? '8px' : 'auto';
        pinsPanel.style.width = innerWidth <= 650 ? 'auto' : '360px';
        pinsPanel.style.maxHeight = `${Math.max(80, Math.min(innerHeight * .5, innerHeight - bounds.bottom - 16))}px`;
    };
    const closePins = () => { pinsPanel.classList.add('hidden'); pinsButton.setAttribute('aria-expanded', 'false'); };
    document.getElementById('bookmarks-close').addEventListener('click', () => {
        closePins(); pinsButton.focus();
    });
    new MutationObserver(() => {
        const open = !pinsPanel.classList.contains('hidden');
        pinsButton.setAttribute('aria-expanded', String(open));
        if (open) { positionPins(); document.getElementById('notif-dropdown').classList.add('hidden'); }
    }).observe(pinsPanel, {attributes: true, attributeFilter: ['class']});
    document.addEventListener('click', event => {
        if (!pinsPanel.classList.contains('hidden') && !pinsPanel.contains(event.target) && !pinsButton.contains(event.target)
            && !event.target.closest('.loom-confirm-overlay')) closePins();
    });
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && !pinsPanel.classList.contains('hidden')) { closePins(); pinsButton.focus(); }
    });
    window.addEventListener('resize', positionPins);
    document.querySelector('#bookmarks-panel-global .bookmarks-panel-header span').textContent = 'Saved pins';
    const originalRenderBookmarks = renderBookmarksPanel;
    renderBookmarksPanel = target => {
        originalRenderBookmarks(target);
        const list = document.getElementById('bookmarks-list-global');
        if (!list || (target && target !== 'bookmarks-list-global')) return;
        const pins = (State.branchPins || []).filter(pin => !State.bookmarks.some(bookmark => Number(bookmark.message_id) === Number(pin.message_id)));
        if (!pins.length) return;
        const heading = document.createElement('div'); heading.className = 'bookmarks-panel-header'; heading.textContent = State.currentConv?.title || 'Branch pins'; list.append(heading);
        for (const pin of pins) {
            const row = document.createElement('div'); row.className = 'workspace-branch-row workspace-pinned-row';
            const open = workspaceButton(pin.label || 'Pinned branch', () => { navigateToBranchPath(`msg:${pin.message_id}`); document.getElementById('bookmarks-panel-global').classList.add('hidden'); });
            const edit = document.createElement('button'); edit.className = 'workspace-row-pin'; edit.textContent = '✎'; edit.setAttribute('aria-label', 'Rename pin'); edit.onclick = () => workspaceRenamePin(pin, list);
            const remove = document.createElement('button'); remove.className = 'workspace-row-pin'; remove.textContent = '✕'; remove.setAttribute('aria-label', 'Remove pin'); remove.onclick = () => workspaceRemovePin(pin);
            row.append(open, edit, remove); list.append(row);
        }
        list.querySelector('.bookmarks-empty')?.remove();
    };
    document.getElementById('user-input').addEventListener('input', saveExoHomeBlankDraft);
    document.getElementById('describe-context').addEventListener('input', saveExoHomeBlankDraft);
    window.addEventListener('pagehide', saveExoHomeBlankDraft);
    const left = document.getElementById('workspace-left');
    const header = document.createElement('div');
    header.id = 'workspace-left-header';
    document.getElementById('workspace-pin-current').parentNode.id = 'workspace-branch-tools';
    const conversations = document.getElementById('workspace-conversations');
    const branches = document.getElementById('workspace-branches');
    for (const node of [...left.children]) if (node !== branches) header.appendChild(node);
    const divider = document.createElement('hr');
    divider.id = 'workspace-left-divider';
    const scroll = document.createElement('div');
    scroll.id = 'workspace-left-scroll';
    scroll.append(conversations, branches);
    left.append(header, divider, scroll);
    const toolbar = document.getElementById('chat-workspace-toolbar');
    for (const [side, ids] of [
        ['left', ['workspace-library', 'workspace-projects', 'workspace-toggle-left', 'workspace-toggle-pins']],
        ['right', ['workspace-toggle-canvas', 'workspace-toggle-right', 'workspace-tree', 'workspace-goals']],
    ]) {
        const group = document.createElement('div');
        group.id = `workspace-controls-${side}`;
        group.className = 'workspace-control-group';
        for (const id of ids) group.appendChild(document.getElementById(id));
        toolbar.appendChild(group);
    }
    const agentPane = document.createElement('aside');
    agentPane.id = 'workspace-agent-pane';
    agentPane.setAttribute('aria-label', 'Codex goals');
    agentPane.hidden = true;
    agentPane.innerHTML = '<div class="workspace-goals-header"><strong>Goals</strong><button type="button" id="workspace-goals-close" aria-label="Close goals">&times;</button></div>';
    agentPane.appendChild(document.getElementById('codex-goal-row'));
    document.getElementById('chat-workspace-body').appendChild(agentPane);
    new ResizeObserver(() => syncWorkspaceLayout()).observe(document.getElementById('input-area'));
    const separator = document.getElementById('workspace-canvas-resizer');
    let resizing = null;
    separator.addEventListener('pointerdown', event => {
        if (event.button !== 0) return;
        resizing = { x: event.clientX, width: document.getElementById('workspace-canvas-pane').clientWidth };
        separator.setPointerCapture(event.pointerId);
        document.body.classList.add('workspace-resizing');
        event.preventDefault();
    });
    separator.addEventListener('pointermove', event => {
        if (!resizing) return;
        const maximum = Number(separator.getAttribute('aria-valuemax'));
        _workspaceCanvasWidth = Math.min(maximum, Math.max(240, resizing.width + resizing.x - event.clientX));
        syncWorkspaceLayout();
    });
    const endResize = () => {
        if (!resizing) return;
        resizing = null; document.body.classList.remove('workspace-resizing');
        localStorage.setItem('exo-canvas-width', String(_workspaceCanvasWidth));
    };
    for (const event of ['pointerup', 'pointercancel', 'lostpointercapture']) separator.addEventListener(event, endResize);
    separator.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
        _workspaceCanvasWidth = Math.min(Number(separator.getAttribute('aria-valuemax')), Math.max(240,
            document.getElementById('workspace-canvas-pane').clientWidth + (event.key === 'ArrowLeft' ? 40 : -40)));
        syncWorkspaceLayout(); localStorage.setItem('exo-canvas-width', String(_workspaceCanvasWidth)); event.preventDefault();
    });
    let _previousInnerWidth = innerWidth;
    window.addEventListener('resize', () => {
        const currentWidth = innerWidth;
        if (!isMobileDevice() && _previousInnerWidth > 768 && currentWidth <= 768) {
            for (const side of ['left', 'right']) workspaceSetOpen(side, false, false);
        }
        _previousInnerWidth = currentWidth;
        syncWorkspaceLayout(); syncWorkspaceCanvas();
    });
    document.getElementById('workspace-toggle-canvas').addEventListener('click', () => setWorkspaceCanvasOpen(!_workspaceCanvasOpen));
    document.getElementById('workspace-canvas-close').addEventListener('click', () => setWorkspaceCanvasOpen(false));
    document.getElementById('workspace-canvas-pane-enable').addEventListener('click', async () => { await toggleCanvas(); scheduleWorkspaceRender(); });
    document.getElementById('workspace-branch-filter').addEventListener('input', scheduleWorkspaceRender);
    document.getElementById('workspace-pin-current').addEventListener('click', event => {
        event.stopPropagation();
        workspaceOpenPins();
        const current = currentBranchMessage();
        if (!current) return;
        const existing = State.branchPins.find(pin => Number(pin.message_id) === Number(current.id));
        workspaceRenamePin(existing || {isNew: true, message_id: current.id, label: State.branchNames[current.id] || 'Pinned branch'});
    });
    for (const side of ['left', 'right']) {
        workspaceSetOpen(side, !isMobileDevice() && localStorage.getItem(`exo-sidebar-${side}`) !== 'false', false);
        document.getElementById(`workspace-toggle-${side}`).addEventListener('click', () => {
            if (side === 'left') { _workspacePinsOnly = false; scheduleWorkspaceRender(); }
            const panel = document.getElementById(`workspace-${side}`);
            if (side === 'left' && (panel.hidden || panel.classList.contains('conversations-mode'))) setWorkspaceLeftMode(false);
            else workspaceSetOpen(side, panel.hidden);
        });
    }
    document.getElementById('workspace-conversation-filter').addEventListener('input', scheduleWorkspaceRender);
    document.getElementById('workspace-library').addEventListener('click', () => switchView('home'));
    document.getElementById('workspace-projects').addEventListener('click', () => {
        const panel = document.getElementById('workspace-left');
        if (!panel.hidden && panel.classList.contains('conversations-mode')) workspaceSetOpen('left', false);
        else setWorkspaceLeftMode(true);
    });
    document.getElementById('workspace-tree').addEventListener('click', () => {
        if (State.currentConvId) switchView('tree');
    });
    document.getElementById('workspace-search').addEventListener('click', () => {
        document.getElementById('btn-search-space-open').click();
    });
    document.getElementById('workspace-search-library').addEventListener('click', () => switchView('home'));
    document.getElementById('btn-search-space-close').addEventListener('click', () => {
        if (State.currentView === 'search') switchView('home');
    });
    document.getElementById('workspace-search-chat').addEventListener('click', async () => {
        if (State.currentConvId) switchView('chat');
        else {
            const epoch = _workspaceNavigationEpoch;
            await populateCCModelDropdowns();
            if (epoch === _workspaceNavigationEpoch && State.currentView === 'search' && !State.currentConvId) startCasualChat({ fresh: false });
        }
    });
    document.getElementById('workspace-new-chat').addEventListener('click', () => {
        if (State.currentConvId) startNewConversationThread();
        else startCasualChat();
    });
    document.getElementById('workspace-new-space').addEventListener('click', () => document.getElementById('btn-new-conv-home').click());
    document.getElementById('workspace-pin-tree').addEventListener('click', async () => {
        const conv = State.currentConv;
        if (!conv || !State.currentConvId) return;
        const button = document.getElementById('workspace-pin-tree'); button.disabled = true;
        try {
            const starred = conv.starred ? 0 : 1;
            await API.put(`/api/conversations/${conv.id}`, {starred});
            conv.starred = starred;
            const item = State.conversations.find(item => String(item.id) === String(conv.id));
            if (item) item.starred = starred;
            renderConversationList(); scheduleWorkspaceRender();
        } catch (error) { showToast(error.detail || 'Could not update tree pin', 'error'); }
        finally { button.disabled = !State.currentConvId; }
    });
    document.getElementById('user-input').addEventListener('input', saveCasualDraft);
    document.getElementById('describe-context').addEventListener('input', saveCasualDraft);
    document.getElementById('workspace-new-model').addEventListener('change', event => {
        const effort = document.getElementById('workspace-new-effort');
        if (event.target.selectedOptions[0]?.dataset.effort) effort.value = event.target.selectedOptions[0].dataset.effort;
        _syncEffortOptionsForModel('workspace-new-effort', event.target.value);
        effort.disabled = !_supportsEffortValue(event.target.value);
        saveCasualDraft();
    });
    document.getElementById('workspace-new-effort').addEventListener('change', saveCasualDraft);
    window.addEventListener('pagehide', saveCasualDraft);
    document.getElementById('workspace-artifacts-refresh').addEventListener('click', refreshWorkspaceArtifacts);
    document.getElementById('workspace-git-refresh').addEventListener('click', refreshWorkspaceGit);
    document.getElementById('workspace-goals').setAttribute('aria-controls', 'workspace-agent-pane');
    document.getElementById('workspace-goals').setAttribute('aria-expanded', 'false');
    document.getElementById('workspace-goals').addEventListener('click', event => {
        event.stopPropagation();
        if (!State.currentConv?.codex_goal_objective?.trim()) { openCodexGoalWindow({focus: true}); return; }
        _workspaceGoalsOpen = !_workspaceGoalsOpen;
        if (_workspaceGoalsOpen && isMobileDevice()) {
            window.closeWorkspaceLeaves?.();
            workspaceSetOpen('left', false, false);
            setWorkspaceCanvasOpen(false);
        }
        if (_workspaceGoalsOpen) workspaceSetOpen('right', false, false);
        renderCodexGoalRow();
        syncWorkspaceLayout();
    });
    document.getElementById('workspace-goals-close').addEventListener('click', () => {
        _workspaceGoalsOpen = false;
        syncWorkspaceLayout();
    });
    document.getElementById('workspace-canvas-enable').addEventListener('click', async () => { await toggleCanvas(); scheduleWorkspaceRender(); });
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && _workspaceGoalsOpen) {
            _workspaceGoalsOpen = false;
            syncWorkspaceLayout();
        }
        if (event.key === 'Escape' && isMobileDevice()) {
            setWorkspaceCanvasOpen(false);
            for (const side of ['left', 'right']) workspaceSetOpen(side, false, false);
        }
    });
    new MutationObserver(scheduleWorkspaceRender).observe(document.getElementById('messages'), { childList: true });
    new MutationObserver(scheduleWorkspaceRender).observe(document.getElementById('agent-activity-list'), { childList: true });
    new MutationObserver(scheduleWorkspaceRender).observe(document.getElementById('branch-pins-list'), { childList: true });
    scheduleWorkspaceRender();
});
