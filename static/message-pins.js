/* Message pins reuse persisted bookmarks; branch pins retain their own scope. */
document.addEventListener('DOMContentLoaded', () => {
    const toggle = document.getElementById('workspace-toggle-pins');
    const pane = document.createElement('aside');
    pane.id = 'workspace-message-pins';
    pane.setAttribute('aria-label', 'Leaves: saved messages');
    pane.hidden = true;
    document.getElementById('workspace-right').before(pane);
    let epoch = 0;
    let busy = false;
    let rendered = '';
    const close = () => {
        pane.hidden = true;
        ++epoch;
        toggle.setAttribute('aria-expanded', 'false');
    };
    window.closeWorkspaceLeaves = close;
    async function render() {
        if (pane.hidden || busy) return;
        busy = true;
        const request = ++epoch;
        try {
            const pins = await API.get('/api/bookmarks');
            const messages = await Promise.all(pins.map(pin => API.get(`/api/bookmarks/${pin.id}/message`).catch(() => null)));
            if (request !== epoch || pane.hidden) return;
            const signature = JSON.stringify([pins, messages]);
            if (signature === rendered) return;
            rendered = signature;
            const title = document.createElement('h3'); title.textContent = 'Leaves';
            pane.replaceChildren(title);
            if (!pins.length) {
                const empty = document.createElement('p'); empty.textContent = 'Pin a message to Leaves using its hexagon button.'; pane.append(empty);
            }
            pins.forEach((pin, index) => {
                const msg = messages[index];
                if (!msg) return;
                // The common renderer supplies role styling, Markdown, images and tool blocks.
                const card = createMessageElement(msg);
                card.hidden = true;
                card.removeAttribute('data-msg-id');
                card.querySelectorAll('[id]').forEach(node => node.removeAttribute('id'));
                card.querySelectorAll('.branch-slot').forEach(node => node.remove());
                const actions = card.querySelector('.message-actions');
                if (actions) {
                    actions.replaceChildren();
                    const action = (name, callback) => {
                        const button = document.createElement('button');
                        const icons = {'Go to message': '\u2197', Branch: '\u2325', Copy: '\u29c9', Edit: '\u270e', Unpin: '\u00d7'};
                        button.textContent = icons[name] || name;
                        button.title = name; button.setAttribute('aria-label', name);
                        button.onclick = async () => {
                            button.disabled = true;
                            try { await callback(); }
                            catch (error) { showToast(error.detail || error.message || 'Action failed', 'error'); }
                            finally { button.disabled = false; }
                        };
                        actions.append(button);
                    };
                    const land = async () => {
                        close();
                        if (Number(State.currentConvId) !== Number(pin.conversation_id)) await loadConversation(pin.conversation_id);
                        State._skipLoadOnChat = true; switchView('chat');
                        await switchToBranch(msg.id, msg.id, {exact: true});
                    };
                    action('Go to message', land);
                    action('Branch', async () => { await land(); await forkFromMessage(msg.id); });
                    action('Copy', () => navigator.clipboard.writeText(msg.content || ''));
                    if (msg.role === 'user') action('Edit', async () => { await land(); editMessage(msg.id); });
                    action('Unpin', async () => {
                        await API.del(`/api/bookmarks/${pin.id}`);
                        State.bookmarks = (State.bookmarks || []).filter(item => item.id !== pin.id);
                        renderMessages(); refreshOpenBookmarksPanels(); render();
                    });
                }
                const row = document.createElement('div'); row.className = 'workspace-branch-row';
                const open = document.createElement('button'); open.type = 'button'; open.className = 'workspace-item';
                open.textContent = (msg.content || '').replace(/\s+/g, ' ').trim().slice(0, 100) || 'Saved message';
                const detail = document.createElement('small'); detail.textContent = `${pin.conversation_title || 'Tree'} · ${msg.role === 'user' ? 'You' : 'Assistant'}`;
                open.append(detail);
                open.onclick = async () => {
                    try {
                        close();
                        if (Number(State.currentConvId) !== Number(pin.conversation_id)) await loadConversation(pin.conversation_id);
                        State._skipLoadOnChat = true; switchView('chat');
                        await switchToBranch(msg.id, msg.id, {exact: true});
                    } catch (error) { showToast(error.detail || 'Could not open leaf', 'error'); }
                };
                const preview = document.createElement('button'); preview.type = 'button'; preview.className = 'workspace-row-pin';
                preview.textContent = '\u2304'; preview.title = 'Preview saved message'; preview.setAttribute('aria-label', preview.title);
                preview.setAttribute('aria-expanded', 'false');
                preview.onclick = () => { card.hidden = !card.hidden; preview.setAttribute('aria-expanded', String(!card.hidden)); };
                row.append(open, preview);
                pane.append(row, card);
            });
        } catch (error) { showToast(error.detail || 'Could not load pinned messages', 'error'); }
        finally { busy = false; }
    }
    toggle.onclick = () => {
        if (!pane.hidden) { close(); return; }
        pane.hidden = false;
        if (typeof workspaceSetOpen === 'function') {
            workspaceSetOpen('left', false, false);
            if (isMobileDevice()) {
                workspaceSetOpen('right', false, false);
                setWorkspaceCanvasOpen(false);
                _workspaceGoalsOpen = false;
                _closeAgentManager();
                syncWorkspaceLayout();
            }
        }
        toggle.setAttribute('aria-expanded', 'true');
        render();
    };
    document.addEventListener('keydown', event => { if (event.key === 'Escape') close(); });
    // Reflect pins and edits from this page and other devices while the panel is open.
    setInterval(async () => {
        if (document.hidden) return;
        if (State.currentView === 'chat') render();
        if (!(State.agentRuns || []).length) return;
        try {
            const saved = await API.get('/api/agent-notification-dismissals');
            for (const key of saved) _agentDismissals.add(key);
            renderAgentActivity();
        } catch (_) { /* Retry on the next refresh. */ }
    }, 5000);
});
