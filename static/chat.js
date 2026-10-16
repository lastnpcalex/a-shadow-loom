/**
 * Loom — Chat rendering, WebSocket, image upload, streaming, branch nav
 */

// ── Image Path Helper ──

function parseImagePaths(imagePath) {
    if (!imagePath) return [];
    if (typeof imagePath === 'object' && Array.isArray(imagePath)) return imagePath;
    // Try JSON array
    try {
        const parsed = JSON.parse(imagePath);
        if (Array.isArray(parsed)) return parsed;
    } catch {}
    // Single path string
    return [imagePath];
}

// ── WebSocket ──

let _wsReconnectDelay = 2000;
let _wsReconnectTimer = null;
let _disconnectedRecoveryTimer = null;
let _disconnectedHistoryCheck = {key: null, at: 0, settled: false};
let _transportReportWindow = {minute: -1, count: 0, pending: 0};

function _reportPublicSocket(socket, stage, event = {}) {
    if (location.hostname !== 'portal.example.invalid' || !/^[a-f0-9]{32}$/.test(socket._transportTrace || '')
        || !['open', 'error', 'close'].includes(stage)) return;
    socket._transportReported ||= new Set();
    if (socket._transportReported.has(stage)) return;
    socket._transportReported.add(stage);
    const minute = Math.floor(Date.now() / 60000);
    if (_transportReportWindow.minute !== minute) {
        _transportReportWindow.minute = minute; _transportReportWindow.count = 0;
    }
    if (_transportReportWindow.count >= 12 || _transportReportWindow.pending >= 2) return;
    _transportReportWindow.count++; _transportReportWindow.pending++;
    const boundedTime = value => Math.min(86400000, Math.max(0, Math.floor(value)));
    const report = {trace: socket._transportTrace, stage, opened: socket._openedAt != null,
        code: Number.isInteger(event.code) && event.code >= 0 && event.code <= 4999 ? event.code : 0,
        clean: event.wasClean === true,
        elapsed_ms: boundedTime(Date.now() - socket._transportStartedAt),
        idle_ms: boundedTime(Date.now() - (socket._lastActivity || socket._transportStartedAt))};
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 3000);
    // The browser supplies existing cookies. Never read or export credentials,
    // URLs, close reasons, event bodies, message contents, or exception text.
    void fetch('/api/transport-report', {method:'POST', mode:'same-origin',
        headers:{'Content-Type':'application/json'}, body:JSON.stringify(report),
        signal:controller.signal, cache:'no-store'})
        .catch(() => {}).finally(() => {clearTimeout(timeout); _transportReportWindow.pending--;});
}

function _showConnectionStatus(text = '') {
    let status = document.getElementById('chat-connection-status');
    if (!text) { status?.remove(); return; }
    const input = document.getElementById('input-row');
    if (!input) return;
    if (!status) {
        status = document.createElement('div');
        status.id = 'chat-connection-status';
        status.className = 'turn-lifecycle-status';
        status.setAttribute('role', 'status');
        const label = document.createElement('span');
        label.className = 'turn-lifecycle-text';
        status.appendChild(label);
        input.before(status);
    }
    const label = status.firstChild;
    if (label.textContent !== text) label.textContent = text;
}

async function _recoverDisconnectedMessages(signal) {
    const convId = State.currentConvId;
    const threadId = State.loomCursor?.thread_id;
    const draftId = State._followingDraftMsgId;
    const runId = State._followingGenId;
    const owner = {convId, conversationSeq: _conversationLoadRequestSeq,
        branchSeq: State._branchSwitchSeq || 0,
        navigationEpoch: typeof _workspaceNavigationEpoch === 'undefined' ? 0 : _workspaceNavigationEpoch};
    const read = async url => {
        const response = await fetch(url, {signal, cache: 'no-store'});
        if (!response.ok) throw await apiError(response, 'GET', url);
        return response.json();
    };
    const [cursor, active] = await Promise.all([
        read(`/api/conversations/${convId}/cursor/${encodeURIComponent(_loomClientInstanceId)}`),
        read('/api/generations'),
    ]);
    if (!_sendOwnerIsCurrent(owner) || State._followingGenId !== runId
        || State.loomCursor?.thread_id !== threadId || State.ws?.readyState === WebSocket.OPEN) return;
    // Active-run metadata has no transcript. Never download/re-render the
    // history repeatedly merely to discover that a tracked turn is still running.
    const running = active.some(run => _sameConvId(run.conv_id, convId)
        && (draftId != null ? _sameMessageId(run.draft_msg_id, draftId)
            : threadId != null && String(run.loom_thread_id) === String(threadId)));
    _showConnectionStatus(running
        ? 'Live updates disconnected. A turn is running in this thread.'
        : 'Live updates disconnected. Checking saved replies.');
    if (running) return;
    const key = `${convId}/${cursor.cursor_id}/${cursor.revision}/${cursor.leaf_message_id}/${runId}`;
    if (_disconnectedHistoryCheck.key === key
        && (_disconnectedHistoryCheck.settled || Date.now() - _disconnectedHistoryCheck.at < 15000)) return;
    _disconnectedHistoryCheck = {key, at: Date.now(), settled: false};
    const recovered = await _recoverIdleMessages({requireTerminal: true, transportIndependent: true, signal});
    if (recovered && _sendOwnerIsCurrent(owner) && !State.isStreaming && _disconnectedHistoryCheck.key === key) {
        const settledCursor = State.loomCursor;
        _disconnectedHistoryCheck.key = `${convId}/${settledCursor?.cursor_id}/${settledCursor?.revision}/${settledCursor?.leaf_message_id}/${State._followingGenId}`;
        _disconnectedHistoryCheck.settled = true;
    }
}

function _scheduleDisconnectedRecovery() {
    if (_disconnectedRecoveryTimer != null) return;
    _disconnectedRecoveryTimer = setTimeout(async () => {
        // Keep this timer occupied across the request: retries must not start
        // overlapping HTTP reads while the socket is repeatedly replaced.
        const controller = new AbortController();
        const deadline = setTimeout(() => controller.abort(), 5000);
        try {
            if (State.currentConvId != null && State.currentView !== 'home'
                && !document.hidden && State.ws?.readyState !== WebSocket.OPEN
                && !State.ws?._authExpired) {
                await _recoverDisconnectedMessages(controller.signal);
            }
        } catch (error) {
            _showConnectionStatus('Live updates disconnected. Server status unavailable.');
            if (error.name !== 'AbortError') console.warn('[Recovery] Status check unavailable');
        } finally {
            clearTimeout(deadline);
            _disconnectedRecoveryTimer = null;
            if (State.currentConvId != null && State.currentView !== 'home'
                && State.ws?.readyState !== WebSocket.OPEN && !State.ws?._authExpired) {
                _scheduleDisconnectedRecovery();
            }
        }
    }, 3000);
}
let _lastResumeProbeAt = 0;
let _queuedGeneration = null;
let _generationQueueItems = [];
let _pendingLaunch = null;
// Retain the exact POST body after an ambiguous network failure. Retrying the
// same visible composer action reuses both its topology contract and
// client_request_id, so the server can return the first durable insert rather
// than attaching or launching a duplicate.
let _pendingSendRequest = null;
let _submittedSendOwner = null;
let _launchRecoveryNotice = null;

function _showLaunchRecoveryNotice() {
    if (!_launchRecoveryNotice || !_sendOwnerIsCurrent(_launchRecoveryNotice.owner)) return;
    const notice = _launchRecoveryNotice;
    showRetryBar(notice.text);
    _launchRecoveryNotice = notice;
    document.getElementById('btn-retry')?.remove();
}

function _sendOwnerIsCurrent(owner) {
    return !!owner && _sameConvId(State.currentConvId, owner.convId)
        && owner.conversationSeq === _conversationLoadRequestSeq
        && owner.branchSeq === (State._branchSwitchSeq || 0)
        && owner.navigationEpoch === (typeof _workspaceNavigationEpoch === 'undefined' ? 0 : _workspaceNavigationEpoch);
}
// A completed WS turn can beat its HTTP send acknowledgement. Keep bounded,
// exact-run terminal evidence so the late acknowledgement cannot revive it.
const _recentTerminalRuns = new Map();
function _terminalRunKey(convId, runId) { return `${convId}/${runId}`; }
// Per-page identity: unlike sessionStorage, this is never cloned into a newly
// opened/duplicated Loom window. The server echoes it on generation events so
// two windows on the same parent do not both attach to the first agent stream.
const _loomClientInstanceId = globalThis.crypto?.randomUUID
    ? globalThis.crypto.randomUUID()
    : `loom-${Date.now()}-${Math.random().toString(36).slice(2)}`;
const _subscribedRunIds = new Set();
const _receivedSocketEventIds = new Set();

function _handleSocketMessage(data) {
    // Launch and viewport sockets can briefly share an exact subscription.
    // Deduplicate at transport receipt, before reconstruction queues events:
    // queue draining must still process the already-received envelope once.
    if (data.event_id) {
        if (_receivedSocketEventIds.has(data.event_id)) return;
        _receivedSocketEventIds.add(data.event_id);
        if (_receivedSocketEventIds.size > 2048) _receivedSocketEventIds.delete(_receivedSocketEventIds.values().next().value);
    }
    handleWSMessage(data);
}

function _canonicalClientRunId(data) {
    if (!data) return null;
    if (data.run_id != null) return String(data.run_id);
    if (data.gen_id != null) return `gen:${data.gen_id}`;
    return null;
}

function _followedGenerationProbe() {
    return State._followingGenId != null && State._followingDraftMsgId != null
        ? {run_id: `gen:${State._followingGenId}`, draft_msg_id: State._followingDraftMsgId}
        : {};
}

function connectWebSocket(convId, _attempt) {
    // Cancel any pending reconnect timer
    if (_wsReconnectTimer) { clearTimeout(_wsReconnectTimer); _wsReconnectTimer = null; }

    const followedThreadId = State._liveStreamDraft?.loom_thread_id ?? _pendingLaunch?.threadId;
    const followedConvId = State._liveStreamDraft?.conversation_id ?? State.ws?._loomConvId;
    if (State._followingGenId != null && (
        (followedThreadId != null && String(followedThreadId) !== String(State.loomCursor?.thread_id))
        || (followedConvId != null && !_sameConvId(convId, followedConvId)))) {
        _resetStreamState();
        removeStreamingMessage();
        hideGenStatus();
        setBrowserTabState('idle');
    }

    // Close previous WS without triggering its onclose reconnect
    if (State.ws) {
        State.ws._replaced = true;  // flag so onclose skips reconnect
        clearTimeout(State.ws._handshakeTimer);
        if (State.ws._pingWorker) { State.ws._pingWorker.terminate(); State.ws._pingWorker = null; }
        State.ws.close();
        State.ws = null;
    }

    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    const wsParams = new URLSearchParams({ client_instance_id: _loomClientInstanceId });
    const transportTrace = location.hostname === 'portal.example.invalid'
        ? crypto.randomUUID().replace(/-/g, '') : null;
    if (transportTrace) wsParams.set('transport_trace', transportTrace);
    wsParams.set('child_event_batches', '1');
    // Reconnect ownership is explicit. Branch visibility is deliberately not
    // sent as stream authority; the server reads this page's durable cursor.
    if (State._followingGenId != null) {
        wsParams.set('run_id', `gen:${State._followingGenId}`);
    }
    const wsUrl = `${protocol}://${location.host}/ws/chat/${convId}?${wsParams}`;
    const ws = new WebSocket(wsUrl);
    ws._transportTrace = transportTrace;
    ws._transportStartedAt = Date.now();
    ws._reconnectAttempt = _attempt || 0;
    ws._handshakeTimer = setTimeout(() => {
        if (ws._replaced || State.ws !== ws || ws.readyState !== WebSocket.CONNECTING) return;
        _reportPublicSocket(ws, 'error');
        _showConnectionStatus('Live connection did not open. Reconnecting…');
        // A native CONNECTING socket can take minutes to report failure.
        // Retire it explicitly; its eventual close must not start a second retry.
        ws._replaced = true;
        try { ws.close(); } catch {}
        const attempt = (ws._reconnectAttempt || 0) + 1;
        _wsReconnectTimer = setTimeout(() => {
            _wsReconnectTimer = null;
            if (State.ws === ws && _sameConvId(State.currentConvId, convId)
                && State.currentView !== 'home') connectWebSocket(convId, attempt);
        }, Math.min(500 * Math.pow(2, Math.min(attempt - 1, 4)), 8000));
    }, 10000);
    _showConnectionStatus('Connecting live updates…');
    _scheduleDisconnectedRecovery();

    ws.onopen = () => {
        clearTimeout(ws._handshakeTimer);
        if (ws._replaced || State.ws !== ws || !_sameConvId(convId, State.currentConvId)) return;
        console.log('WebSocket connected');
        _showConnectionStatus();
        ws._openedAt = Date.now();
        _reportPublicSocket(ws, 'open');
        _wsReconnectDelay = 2000; // reset backoff on success
        ws._lastActivity = Date.now();
        // Web Worker keepalive — runs at full speed even in background tabs
        // (setInterval gets throttled to ~60s by Chrome in background). The
        // worker fires every 15s; we use it to both ping the server AND check
        // whether we've gone silent for too long (half-open TCP on mobile
        // Safari / NAT timeouts typically present this way — readyState stays
        // OPEN forever but no traffic flows).
        const workerBlob = new Blob([`setInterval(() => postMessage('tick'), 15000)`], {type: 'text/javascript'});
        const workerUrl = URL.createObjectURL(workerBlob);
        try { ws._pingWorker = new Worker(workerUrl); }
        finally { URL.revokeObjectURL(workerUrl); }
        ws._pingWorker.onmessage = () => {
            if (ws.readyState !== WebSocket.OPEN) return;
            // Staleness check BEFORE sending — if the socket's already silent
            // for 35s, force-close so onclose can trigger a reconnect. Don't
            // send a ping into the void; it'd reset nothing.
            const since = Date.now() - (ws._lastActivity || 0);
            if (since > 35000) {
                console.warn(`[WS] No activity for ${Math.round(since/1000)}s — forcing reconnect`);
                try { ws.close(); } catch {}
                return;
            }
            ws.send(JSON.stringify({ action: 'ping', ..._followedGenerationProbe() }));
        };
        // Server immediately sends generation_active (with snapshot) or generation_idle.
        // We let those handlers trigger loadMessages — no more racy setTimeout.
        ws._needsSync = true;
        if (State._followingGenId != null) {
            ws.send(JSON.stringify({ action: 'subscribe_run', run_id: `gen:${State._followingGenId}` }));
        }
        for (const run of (State.agentRuns || [])) {
            if (run && !['completed', 'failed', 'cancelled'].includes(run.status)) {
                ws.send(JSON.stringify({ action: 'subscribe_run', run_id: `agent:${run.run_id}` }));
            }
        }
        // A hard reload starts with no in-memory child state. Rehydrate the
        // durable Agents badge and replay active run events as soon as this
        // conversation's socket is ready; opening the panel is not required.
        void refreshAgentRuns();
        // If the user hit Send while the old socket was stale, replay now.
        _flushPendingGenerate();
    };

    ws.onmessage = (event) => {
        // A replaced socket can still have deliveries queued in the browser.
        if (ws._replaced || State.ws !== ws || !_sameMessageId(convId, State.currentConvId)) return;
        ws._lastActivity = Date.now();
        // An upgrade followed immediately by a drop must retain backoff.
        if (Date.now() - ws._openedAt >= 10000) ws._reconnectAttempt = 0;
        const data = JSON.parse(event.data);
        _handleSocketMessage(data);
    };

    ws.onerror = (err) => {
        _reportPublicSocket(ws, 'error');
        console.error('WebSocket error:', err);
        void _checkPublicTransport();
    };

    ws.onclose = (event) => {
        clearTimeout(ws._handshakeTimer);
        _reportPublicSocket(ws, 'close', event);
        console.log('WebSocket closed');
        if (ws._pingWorker) { ws._pingWorker.terminate(); ws._pingWorker = null; }
        // If this WS was intentionally replaced, don't reconnect
        if (ws._replaced || State.ws !== ws) return;
        _showConnectionStatus('Live updates disconnected. Reconnecting…');
        if (event?.code === 4401) {
            _showConnectionStatus('Sign-in expired. Reload to sign in; your draft is saved.');
            ws._authExpired = true;
            if (_wsReconnectTimer) { clearTimeout(_wsReconnectTimer); _wsReconnectTimer = null; }
            if (typeof saveComposerDraft === 'function') saveComposerDraft();
            State.isStreaming = false;
            document.getElementById('btn-send').disabled = false;
            removeStreamingMessage();
            showGenStatus('Sign-in expired. Reload this page to sign in; your draft is saved.', true);
            showToast('Sign-in expired. Reload this page to continue.', 'warning');
            return;
        }
        // Keep partial content mounted until the replacement socket reconciles it.
        _scheduleDisconnectedRecovery();
        if (State.isStreaming) {
            showGenStatus('Reconnecting... generation continues on server', true);
        }
        // Reconnect: instant first try, then back off (cap at 8s)
        if (State.currentConvId === convId && State.currentView !== 'home') {
            const attempt = (ws._reconnectAttempt || 0) + 1;
            const delay = attempt <= 1 ? 100 : Math.min(500 * Math.pow(2, attempt - 2), 8000);
            console.log(`[WS] Reconnecting in ${Math.round(delay)}ms (attempt ${attempt})`);
            _wsReconnectTimer = setTimeout(() => {
                _wsReconnectTimer = null;
                if (State.currentConvId === convId && State.currentView !== 'home') {
                    connectWebSocket(convId, attempt);
                }
            }, delay);
        }
    };

    State.ws = ws;
    ws._loomConvId = convId;
}

// ── Generation Status ──
let _streamTokenCount = 0;
let _streamStartTime = 0;
let _lastChunkAt = 0;
let _droppedChunkCount = 0;

// Browser-tab identity is part of the turn lifecycle: each Loom window names
// its conversation and advertises whether that exact window is looming,
// awaiting permission, or has landed a result. The favicon is generated
// locally so background tabs remain distinguishable without a network asset.
const _TAB_ACTIVITY_FRAMES = [
    '\u280b', '\u2819', '\u2839', '\u2838', '\u283c',
    '\u2834', '\u2826', '\u2827', '\u2807', '\u280f',
];
const _tabFaviconCache = new Map();
let _browserTabState = 'idle';
let _browserTabFrame = 0;
let _browserTabTicker = null;
let _browserTabDoneTimer = null;

function _browserTabBaseTitle() {
    const conversationTitle = String(State.currentConv?.title || '').trim();
    return conversationTitle ? `EXO: ${conversationTitle}` : 'EX umbra Omnia';
}

function _browserTabFavicon(glyph, color) {
    const key = `${glyph}:${color}`;
    if (_tabFaviconCache.has(key)) return _tabFaviconCache.get(key);
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">`
        + `<rect width="32" height="32" rx="8" fill="#080816"/>`
        + `<text x="16" y="22" text-anchor="middle" font-family="Segoe UI Symbol,monospace" font-size="21" font-weight="700" fill="${color}">${glyph}</text>`
        + `</svg>`;
    const value = `data:image/svg+xml,${encodeURIComponent(svg)}`;
    _tabFaviconCache.set(key, value);
    return value;
}

function _renderBrowserTabState() {
    const favicon = document.getElementById('loom-favicon')
        || document.querySelector('link[rel~="icon"]');
    if (favicon && !favicon.dataset.defaultHref) {
        favicon.dataset.defaultHref = favicon.getAttribute('href') || '/static/exoloom/icon.png';
    }
    const base = _browserTabBaseTitle();
    // Keep the tab label stable and reserve transient activity state for the
    // favicon. Duplicating the same glyph in both places makes background tabs
    // visually noisy and causes their labels to shift while Loom is working.
    document.title = base;
    let glyph = '';
    let color = '#00ffff';
    if (_browserTabState === 'looming') {
        glyph = _TAB_ACTIVITY_FRAMES[_browserTabFrame % _TAB_ACTIVITY_FRAMES.length];
    } else if (_browserTabState === 'permission') {
        glyph = '!';
        color = '#fbbf24';
    } else if (_browserTabState === 'done') {
        glyph = '\u2713';
        color = '#4ade80';
    } else if (_browserTabState === 'error') {
        glyph = '\u00d7';
        color = '#fb7185';
    }
    if (favicon) {
        favicon.type = glyph || /\.svg(?:[?#]|$)/i.test(favicon.dataset.defaultHref || '')
            ? 'image/svg+xml' : 'image/png';
        favicon.href = glyph
            ? _browserTabFavicon(glyph, color)
            : favicon.dataset.defaultHref;
    }
}

function setBrowserTabState(state = 'idle') {
    _browserTabState = state;
    if (_browserTabDoneTimer) {
        clearTimeout(_browserTabDoneTimer);
        _browserTabDoneTimer = null;
    }
    if (_browserTabTicker) {
        clearInterval(_browserTabTicker);
        _browserTabTicker = null;
    }
    if (state === 'looming') {
        _browserTabFrame = 0;
        _browserTabTicker = setInterval(() => {
            _browserTabFrame = (_browserTabFrame + 1) % _TAB_ACTIVITY_FRAMES.length;
            _renderBrowserTabState();
        }, 160);
    } else if (state === 'done' && !document.hidden) {
        _browserTabDoneTimer = setTimeout(() => setBrowserTabState('idle'), 6000);
    }
    _renderBrowserTabState();
}

function syncBrowserTabIdentity() {
    _renderBrowserTabState();
}

document.addEventListener('visibilitychange', () => {
    if (!document.hidden && ['done', 'error'].includes(_browserTabState)) {
        _browserTabDoneTimer = setTimeout(() => setBrowserTabState('idle'), 1800);
    }
});
syncBrowserTabIdentity();

// ── Stream Debug Overlay ──
// A tiny overlay that surfaces streaming diagnostics. It has two modes:
//   • verbose (Settings → Display → "Show stream debug overlay", or ?debug=1):
//     lights up during any active stream — the full live view for debugging.
//   • default (off): only lights up when something is BROKEN — stuck
//     _reconstructing, dropped chunks, or a non-OPEN websocket. The failure
//     warning is never silenced; the toggle only controls healthy-stream noise.
(function _initStreamDebugOverlay() {
    function streamDebugEnabled() {
        return new URLSearchParams(location.search).has('debug')
            || localStorage.getItem('loom-stream-debug') === '1';
    }
    function mount() {
        if (document.getElementById('stream-debug-overlay')) return;
        const el = document.createElement('div');
        el.id = 'stream-debug-overlay';
        el.style.cssText = 'position:fixed;bottom:6px;right:6px;z-index:99999;font:11px/1.3 ui-monospace,Consolas,monospace;background:rgba(0,0,0,0.72);color:#9ff;padding:4px 7px;border:1px solid rgba(0,255,255,0.25);border-radius:4px;pointer-events:none;display:none;white-space:pre;';
        document.body.appendChild(el);
        setInterval(() => {
            const s = (typeof State !== 'undefined') ? State : null;
            if (!s) { el.style.display = 'none'; return; }
            const verbose = streamDebugEnabled();
            // Debug off = overlay never shows, even on a real failure.
            if (!verbose) { el.style.display = 'none'; return; }
            const chunkAge = _lastChunkAt ? ((Date.now() - _lastChunkAt) / 1000).toFixed(1) + 's' : '—';
            const wsState = ['CONNECTING','OPEN','CLOSING','CLOSED'][s.ws ? s.ws.readyState : 3] || '?';
            const stuckRecon = s._reconstructing && _lastChunkAt && (Date.now() - _lastChunkAt) > 5000;
            const socketDown = !!(s.ws && s.ws.readyState !== 1);
            // Default-off: only surface when something is actually broken.
            const broken = stuckRecon || _droppedChunkCount > 0 || socketDown;
            const active = verbose ? (!!s.isStreaming || !!s._reconstructing || broken) : broken;
            if (!active) { el.style.display = 'none'; return; }
            el.style.color = broken ? '#ff9' : '#9ff';
            el.style.borderColor = broken ? 'rgba(255,180,0,0.7)' : 'rgba(0,255,255,0.25)';
            const lines = [
                `streaming=${!!s.isStreaming} recon=${!!s._reconstructing} ourBranch=${s._streamIsOurBranch}`,
                `chunks=${_streamTokenCount} dropped=${_droppedChunkCount} lastChunk=${chunkAge}`,
                `ws=${wsState} conv=${s.currentConvId}`,
            ];
            if (stuckRecon) lines.push('⚠ recon STUCK — chunks being dropped');
            el.textContent = lines.join('\n');
            el.style.display = 'block';
        }, 500);
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', mount);
    } else {
        mount();
    }
})();

// DOM-authoritative streaming check. State.isStreaming is a flag we set and
// try to clear cleanly, but it leaks any time stream_end fires on a dropped
// WS, or when a permission prompt auto-creates a streamingDiv between turns
// and we lose track of whose turn it belonged to. Symptom: user sends a new
// message, it "disappears" (queued behind a stream that already ended), or
// stream chunks land in a detached div. The only ground truth is whether
// streamingDiv is (a) set and (b) still attached to #messages.
function _isActuallyStreaming() {
    if (!State.isStreaming) return false;
    if (!streamingDiv) return false;
    return !!streamingDiv.isConnected;
}

// Force-clear streaming state. Called before starting a new turn, to recover
// from any leaked flags / stale streamingDiv references.
function _resetStreamState() {
    _stopGenTimer();
    _streamStartTime = 0;
    if (typeof _streamFlushTimer !== 'undefined' && _streamFlushTimer) {
        clearTimeout(_streamFlushTimer);
        _streamFlushTimer = null;
    }
    if (typeof _streamBuffer !== 'undefined') _streamBuffer = '';
    State.isStreaming = false;
    State._streamIsOurBranch = undefined;
    if (State._followingGenId != null) _subscribedRunIds.delete(`gen:${State._followingGenId}`);
    State._followingGenId = null;
    State._followingDraftMsgId = null;
    State._parallelCount = 0;
    State._liveStreamDraft = null;
    _pendingLaunch = null;
    _queuedStreamEventsDuringReconstruct = [];
    if (streamingDiv && !streamingDiv.isConnected) {
        streamingDiv = null;  // detached; drop the stale ref
    }
}

function _clearTerminalStreamState({ clearParallel = false } = {}) {
    _stopGenTimer();
    _streamStartTime = 0;
    if (typeof _streamFlushTimer !== 'undefined' && _streamFlushTimer) {
        clearTimeout(_streamFlushTimer);
        _streamFlushTimer = null;
    }
    if (State._reconstructWatchdog) {
        clearTimeout(State._reconstructWatchdog);
        State._reconstructWatchdog = null;
    }
    _streamBuffer = '';
    State.isStreaming = false;
    State._reconstructing = false;
    State._streamIsOurBranch = undefined;
    if (State._followingGenId != null) _subscribedRunIds.delete(`gen:${State._followingGenId}`);
    State._followingGenId = null;
    State._followingDraftMsgId = null;
    State._compactedThisGen = false;
    State._compactData = null;
    State._liveStreamDraft = null;
    _pendingLaunch = null;
    if (clearParallel) State._parallelCount = 0;
    _queuedStreamEventsDuringReconstruct = [];
    hideThinkingIndicator();
    // One window follows at most one run. Duplicate preparing/start delivery
    // can leave an older shell detached from `streamingDiv`; after the owning
    // terminal settles it is never a legitimate active generation surface.
    document.querySelectorAll('.message.assistant.streaming').forEach(element => {
        if (element !== streamingDiv) element.remove();
    });
    const sendBtn = document.getElementById('btn-send');
    if (sendBtn) sendBtn.disabled = false;
}

function _syncTerminalLoomCursor(data) {
    const messageId = data?.message?.id ?? data?.draft_msg_id ?? null;
    const convId = data?.conv_id ?? State.currentConvId;
    if (messageId == null || !_sameConvId(convId, State.currentConvId)) {
        return Promise.resolve(null);
    }
    const messageParent = data?.message?.parent_id ?? data?.parent_id ?? null;
    const terminalThreadId = data?.message?.loom_thread_id ?? data?.loom_thread_id ?? null;
    const terminalStillOwnsCursor = () => {
        const currentThreadId = State.loomCursor?.thread_id ?? null;
        const currentLeaf = State.loomCursor?.leaf_message_id ?? null;
        return (!terminalThreadId || !currentThreadId
            || String(terminalThreadId) === String(currentThreadId))
            && (currentLeaf == null || _sameMessageId(currentLeaf, messageId)
                || _sameMessageId(currentLeaf, messageParent));
    };
    // A terminal event belongs to the thread that launched it. In particular,
    // finishing an old branch must not move an empty standalone-thread cursor
    // that the user created while the terminal event was settling.
    if (!terminalStillOwnsCursor()) return Promise.resolve(State.loomCursor);
    // A queued continuation is a descendant already selected by this page.
    // Completion/cancellation of its ancestor must never move that cursor
    // backwards. Only settle a terminal that directly extends the selected
    // leaf or is already the selected leaf.
    const prior = State._cursorSyncPromise;
    const sync = (async () => {
        try { if (prior) await prior; } catch {}
        if (!_sameConvId(convId, State.currentConvId)) return null;
        // Re-check after serialized cursor work: the user may have switched or
        // created a thread while this terminal settlement was waiting.
        if (!terminalStillOwnsCursor()) return State.loomCursor;
        rememberConversationLeaf(convId, messageId);
        try {
            return await selectServerLoomCursor(convId, messageId);
        } catch (error) {
            // The server may already have advanced the exact page cursor. Read
            // that authority once; never infer a revision in the browser.
            const current = await API.get(
                `/api/conversations/${convId}/cursor/${encodeURIComponent(_loomClientInstanceId)}`
            );
            return _storeLoomCursor(current);
        }
    })();
    State._cursorSyncPromise = sync;
    return sync;
}

function _settleTerminalStream(data, { clearParallel = false, reload = false } = {}) {
    const followedGen = State._followingGenId;
    const settlement = _syncTerminalLoomCursor(data)
        .catch(error => {
            console.warn('[Loom] Terminal cursor synchronization failed:', error);
            showToast('The reply landed, but this page could not confirm its branch cursor. Reconnect before sending.', 'error');
            return null;
        })
        .finally(() => {
            // A queued/new stream may have started while the cursor request was
            // in flight. Never let the old terminal clear the newer owner.
            if (State._followingGenId === followedGen) {
                _clearTerminalStreamState({ clearParallel });
                // A reconnect snapshot rebuild can recreate a live streaming
                // placeholder for the settled run after its terminal already
                // processed. Once this terminal still owns the following
                // pointer, that shell is never a legitimate active surface.
                removeStreamingMessage();
                if (reload) loadMessages(State.currentConvId);
            }
            if (State._terminalSettlementPromise === settlement) {
                State._terminalSettlementPromise = null;
            }
        });
    State._terminalSettlementPromise = settlement;
    return settlement;
}

let _publicTransportCheckAt = 0;
async function _checkPublicTransport() {
    if (location.hostname !== 'portal.example.invalid' || document.hidden
        || Date.now() - _publicTransportCheckAt < 60000) return;
    _publicTransportCheckAt = Date.now();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
        const response = await fetch('/api/transport-health', {cache:'no-store',signal:controller.signal});
        if (!response.ok) {
            console.warn('[Transport check]', {http_status:response.status});
            return;
        }
        const result = await response.json();
        // Explicit projection: never log response text or credential values.
        console.info('[Transport check]', {authenticated:result.authenticated===true,
            origin_status:Number.isInteger(result.origin_status)?result.origin_status:null,
            socket_open:result.socket_open===true,pong:result.pong===true});
    } catch { console.warn('[Transport check] HTTP check unavailable'); }
    finally {clearTimeout(timer);}
}

// Probe the WS when the tab returns to foreground. readyState alone is not
// enough on mobile — the socket often reports OPEN while actually being
// TCP-dead. If we've had no inbound message recently, force-close so the
// normal reconnect path can replace it.
function _probeWSOnResume(source) {
    if (!State.currentConvId) return;
    if (State.ws?._authExpired) return;
    const now = Date.now();
    if (source !== 'online' && now - _lastResumeProbeAt < 300) return;
    _lastResumeProbeAt = now;
    const ws = State.ws;
    // No inbound activity exists during the handshake. Focus must not cancel
    // that handshake or mistake its unset timestamp for a dead open socket.
    if (ws?.readyState === WebSocket.CONNECTING) return;
    if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
        console.log(`[WS] ${source} — no live socket, reconnecting`);
        connectWebSocket(State.currentConvId);
        return;
    }
    const since = Date.now() - (ws._lastActivity || 0);
    if (since > 15000) {
        console.log(`[WS] ${source} — ${Math.round(since/1000)}s since last activity, forcing reconnect`);
        try { ws.close(); } catch {}
        // onclose will trigger reconnect. Don't loadMessages here — the new
        // socket's generation_active / generation_idle will handle sync.
        return;
    }
    // Socket looks fresh, but mobile browsers can suspend JS delivery while
    // leaving readyState OPEN. Ask for authoritative state so a hidden-tab
    // stream catches up without requiring a manual refresh.
    if (State.isStreaming || State._reconstructing || (streamingDiv && !streamingDiv.isConnected)) {
        console.log(`[WS] ${source} — requesting stream snapshot`);
        ws._needsSync = true;
        _requestSnapshotIfStreaming(true);
        return;
    }
    // Socket looks fresh. A light resync covers anything we missed while blurred.
    if (!State.isStreaming && !document.querySelector('.edit-message-input')) {
        loadMessages(State.currentConvId);
    }
}

document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') _probeWSOnResume('visibilitychange');
});
window.addEventListener('focus', () => _probeWSOnResume('focus'));
window.addEventListener('online', () => _probeWSOnResume('online'));
// iOS Safari can restore or foreground pages without firing visibilitychange.
window.addEventListener('pageshow', (e) => {
    _probeWSOnResume(e.persisted ? 'pageshow(bfcache)' : 'pageshow');
});

async function _fetchCursorBranchMessages(convId, {signal, includeTree = true} = {}) {
    const read = signal ? async url => {
        const response = await fetch(url, {signal, cache: 'no-store'});
        if (!response.ok) throw await apiError(response, 'GET', url);
        return response.json();
    } : url => API.get(url);
    const owner = {
        convId, conversationSeq: _conversationLoadRequestSeq,
        branchSeq: State._branchSwitchSeq || 0,
        navigationEpoch: typeof _workspaceNavigationEpoch === 'undefined' ? 0 : _workspaceNavigationEpoch,
    };
    const previousCursor = JSON.stringify(State.loomCursor);
    const [treeData, cursor] = await Promise.all([
        includeTree ? read(`/api/conversations/${convId}/tree`) : Promise.resolve(State.treeData),
        read(`/api/conversations/${convId}/cursor/${encodeURIComponent(_loomClientInstanceId)}`),
    ]);
    if (!_sendOwnerIsCurrent(owner) || JSON.stringify(State.loomCursor) !== previousCursor) {
        return false;
    }
    _storeLoomCursor(cursor);
    const cursorAtStart = `${cursor.cursor_id}:${cursor.revision}:${cursor.leaf_message_id ?? ''}`;
    State.treeData = treeData;  // keep branch indicators in sync
    if (typeof markTreeDataFresh === 'function') markTreeDataFresh(convId);
    hideRetryBar();
    if (cursor.leaf_message_id == null) {
        State.messages = [];
        return true;
    }
    const leaf = includeTree ? treeData.find(item => _sameMessageId(item.id, cursor.leaf_message_id))
        : {id: cursor.leaf_message_id};
    if (leaf) {
        const branchMessages = await read(`/api/conversations/${convId}/branch/${leaf.id}`);
        if (!_sendOwnerIsCurrent(owner)
            || `${State.loomCursor?.cursor_id}:${State.loomCursor?.revision}:${State.loomCursor?.leaf_message_id ?? ''}` !== cursorAtStart) {
            return false;
        }
        State.messages = branchMessages;
        rememberConversationLeaf(convId, leaf.id);
    } else {
        throw new Error('The server cursor points to a message missing from this conversation tree');
    }
    await _refreshQueuedGeneration(convId, cursorAtStart, read);
    if (!_sendOwnerIsCurrent(owner)
        || `${State.loomCursor?.cursor_id}:${State.loomCursor?.revision}:${State.loomCursor?.leaf_message_id ?? ''}` !== cursorAtStart) return false;
    _mergeLiveStreamDraftIntoState();
    return true;
}

function _queuedMessageFromItem(item) {
    return {
        id: item.user_msg_id,
        conversation_id: item.conv_id,
        parent_id: item.parent_id,
        role: item.role || 'user',
        content: item.content || '',
        image_path: item.image_path || null,
        describe_context: item.describe_context || null,
        created_at: item.message_created_at || item.created_at,
        origin_client_id: item.origin_client_id || item.client_instance_id || null,
        target_anchor_id: item.target_anchor_id ?? null,
        send_intent: item.send_intent || 'continue',
        queued: true,
        queue_status: item.status || 'queued',
        queue_after_draft_id: item.wait_for_draft_id,
    };
}

function _applyGenerationQueueState(items) {
    _generationQueueItems = Array.isArray(items) ? items : [];
    const queuedIds = new Set(
        _generationQueueItems.map(item => String(item.user_msg_id))
    );
    let presentationChanged = false;
    for (const message of State.messages || []) {
        if (!message?.queued || queuedIds.has(String(message.id))) continue;
        message.queued = false;
        delete message.queue_status;
        delete message.queue_after_draft_id;
        presentationChanged = true;
        const queuedEl = document.querySelector(`.message[data-msg-id="${message.id}"]`);
        if (queuedEl) {
            _syncQueuedMessagePresentation(queuedEl, message);
        }
    }
    const visibleIds = new Set((State.messages || []).map(msg => String(msg.id)));
    const visibleLeafId = _currentVisibleLeafId();
    const selected = _generationQueueItems.find(item =>
        visibleIds.has(String(item.user_msg_id))
        || (_sameMessageId(visibleLeafId, item.wait_for_draft_id)
            && visibleIds.has(String(item.wait_for_draft_id)))
    );
    const previousId = _queuedGeneration?.id;
    const previousStatus = _queuedGeneration?.queue_status;
    _queuedGeneration = selected ? _queuedMessageFromItem(selected) : null;
    if (_queuedGeneration) {
        _placeQueuedMessageAfterDraft(_queuedGeneration);
        _placeQueuedMessageAfterDraftInDom(_queuedGeneration);
    }
    return presentationChanged
        || String(previousId ?? '') !== String(_queuedGeneration?.id ?? '')
        || String(previousStatus ?? '') !== String(_queuedGeneration?.queue_status ?? '');
}

function _placeQueuedMessageAfterDraft(message) {
    if (!message?.id) return;
    const messages = State.messages || (State.messages = []);
    const existingIndex = messages.findIndex(item => _sameMessageId(item.id, message.id));
    const queued = existingIndex >= 0
        ? Object.assign(messages[existingIndex], message)
        : message;
    if (existingIndex >= 0) messages.splice(existingIndex, 1);

    const waitForDraftId = queued.queue_after_draft_id;
    if (waitForDraftId != null
        && State._liveStreamDraft
        && _sameMessageId(State._liveStreamDraft.id, waitForDraftId)
        && !messages.some(item => _sameMessageId(item.id, waitForDraftId))) {
        const firstDependentQueue = messages.findIndex(item =>
            item?.queued && _sameMessageId(item.queue_after_draft_id, waitForDraftId)
        );
        if (firstDependentQueue >= 0) {
            messages.splice(firstDependentQueue, 0, State._liveStreamDraft);
        } else {
            messages.push(State._liveStreamDraft);
        }
    }

    const draftIndex = waitForDraftId == null
        ? -1
        : messages.findIndex(item => _sameMessageId(item.id, waitForDraftId));
    if (draftIndex >= 0) messages.splice(draftIndex + 1, 0, queued);
    else messages.push(queued);
}

function _placeQueuedMessageAfterDraftInDom(message) {
    if (!message?.id) return;
    const container = document.getElementById('messages');
    if (!container) return;
    let queuedEl = container.querySelector(`.message[data-msg-id="${message.id}"]`);
    // A status change needs to update an existing row even when its parent is
    // outside the rendered window. Never replace the row's content or editor.
    if (queuedEl) _syncQueuedMessagePresentation(queuedEl, message);
    if (message.queue_after_draft_id == null) return;
    const draftEl = container.querySelector(
        `.message[data-msg-id="${message.queue_after_draft_id}"]`
    );
    if (!draftEl) return;
    if (!queuedEl) queuedEl = createMessageElement(message);
    if (draftEl.nextElementSibling !== queuedEl) draftEl.after(queuedEl);
}

function _syncQueuedMessagePresentation(row, message) {
    const queued = !!message.queued;
    row.classList.toggle('queued-message', queued);
    row.classList.toggle('paused-queued-message', queued && message.queue_status === 'paused');
    const bar = row.querySelector('.queue-actions');
    if (!queued) {
        bar?.remove();
        return;
    }
    // Save/Cancel edit own the action slot until the edit is finished.
    if (row.querySelector('.edit-message-input')) return;
    if (bar) _updateQueueActions(bar, message);
    else row.appendChild(createQueueActions(message));
}

async function _refreshQueuedGeneration(convId = State.currentConvId, expectedCursor = undefined, read = url => API.get(url)) {
    if (!convId) {
        _applyGenerationQueueState([]);
        return false;
    }
    try {
        const threadParam = State.loomCursor?.thread_id
            ? `?loom_thread_id=${encodeURIComponent(State.loomCursor.thread_id)}`
            : '';
        const items = await read(`/api/conversations/${convId}/generation-queue${threadParam}`);
        if (!_sameMessageId(convId, State.currentConvId)) return false;
        if (expectedCursor !== undefined
            && `${State.loomCursor?.cursor_id}:${State.loomCursor?.revision}:${State.loomCursor?.leaf_message_id ?? ''}` !== expectedCursor) return false;
        return _applyGenerationQueueState(items);
    } catch (err) {
        console.warn('[QUEUE] Failed to restore durable queue state:', err);
        return false;
    }
}

async function _syncMessagesForStreaming(convId) {
    if (await _fetchCursorBranchMessages(convId) === false || State.currentView !== 'tree') return;
    if (typeof renderTree === 'function') {
        if (typeof TREE !== 'undefined') TREE._skipCenter = true;
        renderTree();
    } else if (typeof refreshTree === 'function') {
        refreshTree();
    }
}

// Reconnects preserve editors, expanded tool output, the composer, and scroll.
function _reconcileMessageRows(previous) {
    const container = document.getElementById('messages');
    const scroller = document.getElementById('messages-container');
    const top = scroller.scrollTop;
    const atBottom = scroller.scrollHeight - top - scroller.clientHeight < 60;
    const _rowSig = message => JSON.stringify({ ...message, queue_status: undefined });
    const old = new Map(previous.map(message => [String(message.id), _rowSig(message)]));
    const visible = State.messages.slice(Math.min(VIRTUAL_SCROLL.renderedStart || 0, State.messages.length));
    if (State.isStreaming && streamingDiv?.isConnected && _pendingLaunch
        && State._followingDraftMsgId == null && !streamingDiv.dataset.msgId) {
        const candidates = visible.filter(message => message.role === 'assistant'
            && _sameMessageId(message.parent_id, _pendingLaunch.parentId)
            && message.loom_thread_id === _pendingLaunch.threadId
            && message.loom_thread_id === State.loomCursor?.thread_id);
        // The cursor chain supplies one exact child for this pending launch.
        // Bind the existing shell before row reconciliation can insert it twice.
        if (candidates.length === 1) {
            State._followingDraftMsgId = candidates[0].id;
            streamingDiv.dataset.msgId = candidates[0].id;
        }
    }
    const ids = new Set(visible.map(message => String(message.id)));
    for (const row of container.querySelectorAll('.message[data-msg-id]')) {
        if (!ids.has(row.dataset.msgId) && row !== streamingDiv
            && !row.querySelector('.edit-message-input')) row.remove();
    }
    for (const message of visible) {
        const row = container.querySelector(`.message[data-msg-id="${message.id}"]`);
        if ((row && row === streamingDiv) || row?.querySelector('.edit-message-input')) continue;
        if (!row || old.get(String(message.id)) !== _rowSig(message)) {
            const replacement = createMessageElement(message);
            if (row) row.replaceWith(replacement);
            else container.insertBefore(replacement, streamingDiv?.isConnected ? streamingDiv : null);
        } else if (row && message.queued) {
            _syncQueuedMessagePresentation(row, message);
        }
    }
    if (State.messages.length) container.querySelector('.empty-loom-hint')?.remove();
    scroller.scrollTop = atBottom ? scroller.scrollHeight : top;
}

async function loadMessages(convId, { preserveView = false, signal, refreshTreeAfter = true } = {}) {
    try {
        // NOTE: compactify banner is NOT cleared here — it's cleared in
        // loadConversation() when switching conversations, not on message reload

        const prevCount = State.messages.length;
        const previous = State.messages.map(m => ({ ...m }));
        const prevQueuedId = _queuedGeneration?.id;
        // Signature includes empty/non-empty content state so an empty draft
        // filling in (same id, "" → real text) forces a re-render instead of
        // being skipped by the cheap-diff bail below. Queue status on the same
        // queued item is reconciled in place by _refreshQueuedGeneration.
        const _msgSig = m => JSON.stringify({ ...m, queue_status: undefined });
        const prevIdSig = State.messages.map(_msgSig).join(',');
        if (await _fetchCursorBranchMessages(convId, {signal, includeTree: refreshTreeAfter}) === false) return false;
        // Cheap change detection: if the message id list is identical to what
        // we already rendered, skip the container.innerHTML='' + full rebuild.
        // Heartbeat-driven WS reconnects used to call loadMessages on every
        // generation_idle and wipe the DOM even when nothing actually changed
        // — that's what the user was seeing as flicker.
        const newIdSig = State.messages.map(_msgSig).join(',');
        if (newIdSig === prevIdSig && prevIdSig !== ''
            && String(prevQueuedId ?? '') === String(_queuedGeneration?.id ?? '')) {
            // Same chain: queue controls were reconciled in place above. A
            // streaming flag alone is not permission to detach focused rows.
            // Recover a missing stream independently of the surrounding DOM.
            if (State.isStreaming && !_isActuallyStreaming()
                && State._streamIsOurBranch !== false) _requestSnapshotIfStreaming();
            if (refreshTreeAfter && typeof refreshTree === 'function') refreshTree();
            return true;
        }
        if (preserveView) {
            _reconcileMessageRows(previous);
            if (refreshTreeAfter && typeof refreshTree === 'function') refreshTree();
            return true;
        }
        renderMessages();
        // If still streaming, renderMessages just destroyed the streaming div.
        // Drop the stale ref and ask the server for a snapshot — the reply
        // will rebuild the div with prior text intact, instead of an empty
        // box that loses everything streamed before this reload.
        if (streamingDiv && !streamingDiv.isConnected) streamingDiv = null;
        if (State.isStreaming && !streamingDiv) {
            if (State._streamIsOurBranch !== false) {
                _requestSnapshotIfStreaming();
            }
        }
        scrollToBottom();
        if (State.messages.length > prevCount && prevCount > 0 && !State.isStreaming) {
            showToast('Response loaded');
        }
        return true;
    } catch (err) {
        console.error('loadMessages failed:', err);
        return false;
    }
}

let _pendingTurnStatus = null;

async function _recoverIdleMessages({ requireTerminal = false, transportIndependent = false, signal } = {}) {
    const convId = State.currentConvId;
    const socket = State.ws;
    const runId = State._followingGenId;
    const owner = {
        convId, conversationSeq: _conversationLoadRequestSeq,
        branchSeq: State._branchSwitchSeq || 0,
        navigationEpoch: typeof _workspaceNavigationEpoch === 'undefined' ? 0 : _workspaceNavigationEpoch,
    };
    if (!await loadMessages(convId, { preserveView: true, signal, refreshTreeAfter: !transportIndependent })
        || !_sendOwnerIsCurrent(owner) || (!transportIndependent && State.ws !== socket)
        || State._followingGenId !== runId) return;
    const draftId = State._followingDraftMsgId;
    const saved = State.messages.find(message => draftId != null
        ? _sameMessageId(message.id, draftId)
        : message.role === 'assistant' && (_pendingLaunch
            ? _sameMessageId(message.parent_id, _pendingLaunch.parentId)
                && (!message.loom_thread_id || message.loom_thread_id === _pendingLaunch.threadId)
            : transportIndependent && runId == null
                && _sameMessageId(message.id, State.loomCursor?.leaf_message_id)));
    if (requireTerminal && (!saved || (!['completed', 'cancelled', 'interrupted', 'failed', 'partial_failed'].includes(saved.turn_lifecycle_state)
        && saved.generation_ms == null))) return;
    if (saved && streamingDiv?.isConnected) {
        const savedRow = document.querySelector(`.message[data-msg-id="${saved.id}"]`);
        // Before stream_start the shell has no draft ID. HTTP reconciliation
        // already inserted the saved row; replacing the shell would duplicate it.
        if (savedRow && savedRow !== streamingDiv) removeStreamingMessage();
        else streamingDiv.replaceWith(createMessageElement(saved));
    }
    else removeStreamingMessage();
    _clearTerminalStreamState({ clearParallel: true });
    hideGenStatus();
    _showLaunchRecoveryNotice();
    return true;
}

function showGenStatus(text, reconnecting = false) {
    const waiting = streamingDiv?.isConnected
        ? streamingDiv.querySelector('.stream-waiting')
        : null;
    const waitingText = waiting?.querySelector('.stream-waiting-text');
    const inline = streamingDiv?.isConnected
        ? streamingDiv.querySelector('.turn-lifecycle-status')
        : null;
    const inlineText = inline?.querySelector('.turn-lifecycle-text');
    if (waiting && waitingText) {
        // Before provider output, Clotho is the single turn-status surface.
        // MCP/configuration/launch updates replace the generic waiting copy
        // here instead of creating a second spinner in the footer.
        waitingText.dataset.statusText = text;
        waitingText.textContent = text;
        inline?.classList.add('hidden');
        _pendingTurnStatus = null;
    } else if (inline && inlineText) {
        inlineText.textContent = text;
        inline.classList.remove('hidden');
        _pendingTurnStatus = null;
    } else {
        // Status can beat creation/reconstruction of the in-turn shell. Keep
        // it in memory and apply it when appendStreamingMessage mounts that
        // shell; never resurrect the obsolete below-turn status row.
        _pendingTurnStatus = { text, reconnecting };
    }
    const pending = !!_pendingLaunch || /prepar|launch|saving/i.test(String(text || ''));
    document.querySelectorAll('.cancel-draft-btn').forEach(button => {
        button.textContent = '⊘';
        button.title = pending
            ? 'Cancel launch; keep the user message'
            : 'Cancel and discard partial context';
        button.setAttribute('aria-label', button.title);
        button.disabled = false;
    });
    document.querySelectorAll('.stream-thinking-footer').forEach(footer => {
        let interrupt = footer.querySelector('.interrupt-draft-btn');
        if (pending) {
            interrupt?.remove();
        } else if (!interrupt) {
            interrupt = document.createElement('button');
            interrupt.className = 'interrupt-draft-btn';
            interrupt.textContent = '⏸';
            interrupt.title = 'Interrupt and preserve partial context';
            interrupt.setAttribute('aria-label', interrupt.title);
            interrupt.addEventListener('click', interruptGeneration);
            const cancel = footer.querySelector('.cancel-draft-btn');
            if (cancel) cancel.insertAdjacentElement('afterend', interrupt);
            else footer.prepend(interrupt);
        }
        if (interrupt) interrupt.disabled = false;
    });
    setBrowserTabState('looming');
    scrollToBottom();
}

function hideGenStatus() {
    _pendingTurnStatus = null;
    streamingDiv?.querySelector('.turn-lifecycle-status')?.classList.add('hidden');
}

function showRetryBar(errorMsg) {
    hideRetryBar();
    const container = document.getElementById('messages');
    const bar = document.createElement('div');
    bar.id = 'retry-bar';
    bar.className = 'retry-bar';
    bar.innerHTML = `
        <span class="retry-error">${escapeHtml(errorMsg)}</span>
        <button class="btn-small retry-btn" id="btn-retry">Retry</button>
        <button class="retry-dismiss" title="Dismiss">✕</button>
    `;
    bar.querySelector('#btn-retry').addEventListener('click', async () => {
        hideRetryBar();
        // Retry: send generate for the current active leaf
        if (State.ws && State.ws.readyState === WebSocket.OPEN) {
            try {
                const parentId = _currentVisibleLeafId();
                await selectServerLoomCursor(State.currentConvId, parentId);
                showGenStatus('Retrying...');
                const retryMsg = {
                    action: 'generate',
                    parent_id: parentId,
                    client_instance_id: _loomClientInstanceId,
                    loom_thread_id: State.loomCursor?.thread_id || null,
                };
                _attachCCSettings(retryMsg);
                State.ws.send(JSON.stringify(retryMsg));
            } catch (err) {
                showToast(err.detail || err.message || 'Retry target changed', 'error');
            }
        }
    });
    bar.querySelector('.retry-dismiss').addEventListener('click', () => {
        hideRetryBar();
    });
    container.appendChild(bar);
    scrollToBottom();
}

function hideRetryBar() {
    _launchRecoveryNotice = null;
    const existing = document.getElementById('retry-bar');
    if (existing) existing.remove();
}

function _sameMessageId(a, b) {
    if (a === undefined || a === null || b === undefined || b === null) return false;
    return String(a) === String(b);
}

function _currentVisibleLeafId() {
    const visible = (State.messages || []).filter(m => m && m.id != null);
    return visible.length ? visible[visible.length - 1].id : null;
}

async function _messageTopologyContract(parentId, topologyIntent, convId = State.currentConvId, capturedCursor = null, treeData = State.treeData) {
    let cursor = capturedCursor || State.loomCursors?.[String(convId)] || null;
    if (topologyIntent === 'new_thread') {
        if (!cursor || cursor.leaf_message_id != null) {
            throw new Error('This tab no longer points to an empty Loom thread');
        }
        if (parentId != null) {
            throw new Error('A standalone Loom thread cannot have a parent');
        }
    } else {
        if (parentId == null) {
            throw new Error('Choose an exact message before continuing this thread');
        }
        // Ordinary Send already has the exact selected leaf. Do not re-read
        // the mutable viewport after an asynchronous slash-command lookup.
        if (!_sameMessageId(cursor?.leaf_message_id, parentId)) {
            cursor = await selectServerLoomCursor(convId, parentId, treeData);
        }
    }
    return {
        parent_id: parentId,
        loom_thread_id: cursor.thread_id,
        cursor_id: cursor.cursor_id,
        cursor_revision: cursor.revision,
        expected_leaf_id: cursor.leaf_message_id,
        topology_intent: topologyIntent,
        client_instance_id: _loomClientInstanceId,
    };
}

function _acceptMessageCursor(message, convId = State.currentConvId) {
    if (message?.loom_cursor) _storeLoomCursor(message.loom_cursor);
    if (message?.id != null) rememberConversationLeaf(convId, message.id);
}

function _activeQueueParentId() {
    if (!_isActuallyStreaming()) return null;
    const draftId = State._followingDraftMsgId
        ?? State._liveStreamDraft?.id
        ?? streamingDiv?.dataset?.msgId;
    if (draftId === undefined || draftId === null || draftId === '') return null;
    const numericId = Number(draftId);
    return Number.isFinite(numericId) ? numericId : draftId;
}

function _streamParentMatchesVisibleBranch(parentId, draftMsgId) {
    // The server may already have advanced this page's durable cursor while
    // its old answer is still painted during launch/reconnect reconciliation.
    const cursorLeaf = State.loomCursor?.leaf_message_id;
    if (cursorLeaf != null && (_sameMessageId(cursorLeaf, parentId)
        || (draftMsgId != null && _sameMessageId(cursorLeaf, draftMsgId)))) return true;
    if (draftMsgId != null && (State.messages || []).some(m => _sameMessageId(m.id, draftMsgId))) {
        return true;
    }
    if (parentId == null) return _currentVisibleLeafId() == null;
    return _sameMessageId(parentId, _currentVisibleLeafId());
}

function _submittedReplyLeaf(nodes, anchor) {
    if (!anchor || anchor.role !== 'user') return anchor;
    const replies = nodes.filter(node => node.role === 'assistant'
        && _sameMessageId(node.parent_id, anchor.id)
        && String(node.loom_thread_id) === String(anchor.loom_thread_id));
    return replies.length === 1 ? replies[0] : anchor;
}

function _generationWindowOwnership(data) {
    if (!data) return false;
    const runId = _canonicalClientRunId(data);
    // Run identity is authoritative whenever present. The first stream_start
    // is the subscription handshake for a server-launched turn: the server has
    // already routed it to the exact launch socket, while the browser could not
    // know the newly allocated run id before receiving this event. Bootstrap
    // only from immutable launch provenance, never from focus or branch
    // visibility. Every later run event requires the recorded subscription.
    if (runId) {
        const eventConvId = data.conversation_id ?? data.conv_id;
        if (eventConvId != null && !_sameConvId(eventConvId, State.currentConvId)) return false;
        if (data.loom_thread_id != null && State.loomCursor?.thread_id != null
            && String(data.loom_thread_id) !== String(State.loomCursor.thread_id)) return false;
        const alreadyOwned = _subscribedRunIds.has(runId)
            || (State._followingGenId != null
                && runId === `gen:${State._followingGenId}`);
        if (alreadyOwned) return true;
        const sameClient = data.client_instance_id
            && String(data.client_instance_id) === String(_loomClientInstanceId);
        const sameConversation = eventConvId == null
            || _sameConvId(eventConvId, State.currentConvId);
        const currentThreadId = State.loomCursor?.thread_id ?? null;
        const sameThread = (data.loom_thread_id == null && currentThreadId == null)
            || (currentThreadId != null
                && data.loom_thread_id != null
                && String(data.loom_thread_id) === String(currentThreadId));
        const sameAnchor = _sendOwnerIsCurrent(_submittedSendOwner)
            || _sameMessageId(data.parent_id, State.loomCursor?.leaf_message_id)
            || _sameMessageId(data.draft_msg_id, State.loomCursor?.leaf_message_id)
            || (_pendingLaunch && _sameMessageId(_pendingLaunch.parentId, data.parent_id));
        if ((data.type === 'stream_start' || data.type === 'generation_preparing')
            && sameClient && sameConversation && sameThread && sameAnchor) {
            _subscribedRunIds.add(runId);
            return true;
        }
        return false;
    }
    if (data.client_instance_id
        && String(data.client_instance_id) === String(_loomClientInstanceId)) return true;
    return data.client_instance_id == null ? null : false;
}

function _draftBlocks(value) {
    if (Array.isArray(value)) return value;
    if (typeof value === 'string' && value) {
        try {
            const parsed = JSON.parse(value);
            if (Array.isArray(parsed)) return parsed;
        } catch {}
    }
    return [];
}

function _blockTextLength(blocks) {
    return _draftBlocks(blocks).reduce((total, block) => {
        return total + String(block?.text || '').length
            + String(block?.input || '').length
            + String(block?.result || '').length;
    }, 0);
}

function _mergeLiveStreamDraftIntoState() {
    const live = State._liveStreamDraft;
    if (!live || live.id == null) return null;
    const idx = (State.messages || []).findIndex(m => _sameMessageId(m.id, live.id));
    if (idx < 0) {
        const queuedIndex = (State.messages || []).findIndex(message =>
            message?.queued && _sameMessageId(message.queue_after_draft_id, live.id)
        );
        if (queuedIndex >= 0) State.messages.splice(queuedIndex, 0, live);
        else State.messages.push(live);
        return live;
    }
    const fetched = State.messages[idx];
    if (['completed', 'cancelled', 'interrupted', 'failed', 'partial_failed'].includes(fetched.turn_lifecycle_state)
        || fetched.generation_ms != null) return fetched;
    if (String(fetched.content || '').length > String(live.content || '').length) {
        live.content = fetched.content;
    }
    if (_blockTextLength(fetched.content_blocks) > _blockTextLength(live.content_blocks)) {
        live.content_blocks = _draftBlocks(fetched.content_blocks);
    }
    if (!live.model_attestation && fetched.model_attestation) {
        live.model_attestation = fetched.model_attestation;
    }
    const selections = new Map(parseChatItems(fetched.chat_items).map(item => [item.id, item]));
    for (const item of parseChatItems(live.chat_items)) selections.set(item.id, item);
    if (fetched.chat_items != null || live.chat_items != null) live.chat_items = JSON.stringify([...selections.values()]);
    State._liveStreamDraft = { ...fetched, ...live };
    State.messages[idx] = State._liveStreamDraft;
    return State._liveStreamDraft;
}

function _ensureLiveStreamDraft(data = {}) {
    const draftId = data.draft_msg_id ?? State._followingDraftMsgId;
    if (draftId == null) return null;
    if (State._liveStreamDraft && _sameMessageId(State._liveStreamDraft.id, draftId)) {
        return _mergeLiveStreamDraftIntoState();
    }
    const existing = (State.messages || []).find(m => _sameMessageId(m.id, draftId));
    State._liveStreamDraft = {
        ...(existing || {}),
        id: draftId,
        conversation_id: State.currentConvId,
        role: 'assistant',
        parent_id: data.parent_id ?? existing?.parent_id ?? null,
        loom_thread_id: data.loom_thread_id
            ?? existing?.loom_thread_id
            ?? State.loomCursor?.thread_id
            ?? null,
        content: existing?.content || '',
        content_blocks: _draftBlocks(existing?.content_blocks),
        chat_items: existing?.chat_items ?? '[]',
        cc_model_used: data.cc_model || data.local_model || existing?.cc_model_used || '',
        session_continuity: data.session_continuity
            ?? existing?.session_continuity
            ?? null,
    };
    return _mergeLiveStreamDraftIntoState();
}

function _appendLiveDraftText(text) {
    const draft = _ensureLiveStreamDraft();
    if (!draft || !text) return;
    draft.content = (draft.content || '') + text;
    const blocks = _draftBlocks(draft.content_blocks);
    const last = blocks[blocks.length - 1];
    if (last?.type === 'text') last.text = (last.text || '') + text;
    else blocks.push({ type: 'text', text });
    draft.content_blocks = blocks;
}

function _appendLiveDraftThinking(text) {
    const draft = _ensureLiveStreamDraft();
    if (!draft || !text) return;
    const blocks = _draftBlocks(draft.content_blocks);
    const last = blocks[blocks.length - 1];
    if (last?.type === 'thinking') last.text = (last.text || '') + text;
    else blocks.push({ type: 'thinking', text });
    draft.content_blocks = blocks;
}

function _startLiveDraftTool(data) {
    const draft = _ensureLiveStreamDraft(data);
    if (!draft) return;
    const blocks = _draftBlocks(draft.content_blocks);
    blocks.push({
        type: 'tool_use',
        name: data.name || 'Tool',
        tool_id: data.tool_id || '',
        input: '',
        result: '',
    });
    draft.content_blocks = blocks;
}

function _liveDraftTool(toolId) {
    const draft = _ensureLiveStreamDraft();
    if (!draft) return null;
    const blocks = _draftBlocks(draft.content_blocks);
    draft.content_blocks = blocks;
    for (let i = blocks.length - 1; i >= 0; i--) {
        if (blocks[i]?.type === 'tool_use' && (!toolId || _sameMessageId(blocks[i].tool_id, toolId))) {
            return blocks[i];
        }
    }
    return null;
}

function _syncLiveDraftFromSnapshot(snap) {
    const draft = _ensureLiveStreamDraft(snap || {});
    if (!draft || !snap) return draft;
    if (String(snap.full_text || '').length > String(draft.content || '').length) {
        draft.content = snap.full_text;
    }
    if (_blockTextLength(snap.content_blocks) > _blockTextLength(draft.content_blocks)) {
        draft.content_blocks = _draftBlocks(snap.content_blocks);
    }
    if (snap.model_attestation) {
        draft.model_attestation = JSON.stringify(_parseModelAttestation(snap.model_attestation));
    }
    if (snap.time_to_first_token_ms != null) draft.time_to_first_token_ms = snap.time_to_first_token_ms;
    return draft;
}

const _OWNED_STREAM_EVENT_TYPES = new Set([
    'generation_preparing',
    'thinking_start', 'thinking_end', 'thinking_chunk', 'stream_chunk',
    'tool_start', 'tool_input_chunk', 'tool_result', 'usage', 'chat_item',
    'model_attestation', 'stream_end', 'cancelled', 'interrupted', 'error', 'permission_request',
]);

function _isOurBranch(data) {
    if (_generationWindowOwnership(data) === false) return false;
    // Every run-scoped event must match the exact subscribed run.  This gate
    // intentionally runs before legacy branch checks below.
    const eventRunId = _canonicalClientRunId(data);
    if (eventRunId && !_subscribedRunIds.has(eventRunId)
        && !(State._followingGenId != null
            && eventRunId === `gen:${State._followingGenId}`)) return false;
    // If we're following a specific gen_id, only match that one
    if (State._followingGenId != null && data.gen_id != null) {
        return data.gen_id === State._followingGenId;
    }
    // If we're not streaming AND we've already established branch tracking, reject stale
    // parallel sibling events. But don't reject if tracking hasn't been set up yet —
    // that means we're in the pre-stream window (generate sent, stream_start not yet received).
    // Rejecting there silently drops error/status events for a generation that just failed fast.
    if (!State.isStreaming && data.gen_id != null &&
        (State._streamIsOurBranch !== undefined || State._followingGenId != null)) {
        return false;
    }
    // If we already determined this via stream_start, use cached result
    if (State._streamIsOurBranch !== undefined) return State._streamIsOurBranch;
    // For pre-stream messages (status, context_info), check parent_id if available
    if (data.parent_id != null || data.draft_msg_id != null) {
        return _streamParentMatchesVisibleBranch(data.parent_id, data.draft_msg_id);
    }
    if (_OWNED_STREAM_EVENT_TYPES.has(data.type) && data.gen_id != null) return false;
    // Unknown — assume ours (will be corrected on stream_start)
    return true;
}

// Event types that indicate the model/runtime is actively producing output
// or doing tool work — any of these should clear the pre-first-token
// "still working" indicator so it never lingers when activity is visible.
const _ACTIVITY_EVENT_TYPES = new Set([
    'stream_chunk',
    // thinking_chunk is handled in its switch case so an empty provider
    // heartbeat cannot falsely clear the waiting indicator.
    'tool_start', 'tool_input_chunk', 'tool_result', 'tool_use',
    'ask_user_question',
    'state_update', 'compact_boundary', 'compact_summary_ready', 'compact_done',
    'plan_ready', 'plan_landed', 'canvas_updated', 'image_describe', 'chat_item',
]);

const _RECONSTRUCT_QUEUE_EVENT_TYPES = new Set([
    'generation_preparing',
    'thinking_start', 'thinking_end', 'thinking_chunk',
    'stream_chunk', 'tool_start', 'tool_input_chunk', 'tool_result', 'chat_item',
    'usage', 'first_token', 'model_attestation', 'stream_end', 'generation_terminal', 'cancelled', 'interrupted', 'error', 'permission_request', 'generation_idle',
]);
let _queuedStreamEventsDuringReconstruct = [];

function _queueStreamEventDuringReconstruct(data) {
    if (!State._reconstructing || !_RECONSTRUCT_QUEUE_EVENT_TYPES.has(data.type)) return false;
    if (data.gen_id != null && State._followingGenId != null && data.gen_id !== State._followingGenId) {
        return false;
    }
    if (State._streamIsOurBranch === false) return false;
    _queuedStreamEventsDuringReconstruct.push(data);
    return true;
}

function _drainQueuedStreamEvents() {
    if (!_queuedStreamEventsDuringReconstruct.length) return;
    const queued = _queuedStreamEventsDuringReconstruct;
    _queuedStreamEventsDuringReconstruct = [];
    for (const event of queued) {
        handleWSMessage(event);
    }
}

function _permissionBelongsToVisibleBranch(data) {
    if (data.gen_id != null && State._followingGenId != null) {
        return String(data.gen_id) === String(State._followingGenId);
    }
    return _isOurBranch(data);
}

function _agentRunForEvent(runId) {
    if (!runId) return null;
    const id = String(runId).replace(/^agent:/, '');
    let run = (State.agentRuns || []).find(item => String(item.run_id) === id);
    if (!run) {
        run = { run_id: id, conversation_id: State.currentConvId };
        State.agentRuns = [...(State.agentRuns || []), run];
    }
    return run;
}

const _ACTIVE_AGENT_RUN_STATUSES = new Set([
    'queued', 'needs_relaunch', 'launching', 'running', 'cancel_requested',
]);

function _agentManagerTarget() {
    const mode = String(State.currentConv?.mode || '').toLowerCase();
    if (mode === 'hermes' || mode === 'weave') return document.getElementById('weave-inline-controls');
    if (mode === 'dream') return document.getElementById('dream-inline-controls');
    if (['claude', 'local', 'codex', 'gemini', 'goose', 'umans', 'openrouter'].includes(mode)) {
        return document.getElementById('cc-inline-controls');
    }
    return null;
}

let _agentViewEpoch = 0;
let _agentRefreshJob = null;
let _agentRenderTimer = null;
let _agentRenderDirty = false;

function resetAgentActivity() {
    _agentViewEpoch++;
    _agentRefreshJob = null;
    clearTimeout(_agentRenderTimer);
    _agentRenderTimer = null;
    _agentRenderDirty = true;
}

function _scheduleAgentRender(immediate = false) {
    _agentRenderDirty = true;
    if (document.hidden) return;
    if (immediate) {
        clearTimeout(_agentRenderTimer);
        _agentRenderTimer = null;
        renderAgentActivity();
    } else if (_agentRenderTimer == null) {
        _agentRenderTimer = setTimeout(() => {
            _agentRenderTimer = null;
            if (!document.hidden) renderAgentActivity();
        }, 100);
    }
}

document.addEventListener('visibilitychange', () => {
    if (!document.hidden && _agentRenderDirty) _scheduleAgentRender(true);
});

function _agentRunIsCurrent(run, epoch) {
    return epoch === _agentViewEpoch && _sameConvId(run.conversation_id, State.currentConvId)
        && (State.agentRuns || []).includes(run);
}

async function _replayAgentRun(run) {
    if (run._replayJob) return run._replayJob;
    const epoch = _agentViewEpoch;
    const replay = async () => {
        try {
            while (_agentRunIsCurrent(run, epoch)) {
                const after = Number(run._lastSequence || 0);
                if (after >= Number(run._replayTarget || 0)) break;
                const events = await API.get(
                    `/api/agent-runs/${encodeURIComponent(run.run_id)}/events?after_sequence=${after}&limit=500`
                );
                if (!_agentRunIsCurrent(run, epoch)) return;
                if (!Array.isArray(events) || !events.length) break;
                for (const event of events) {
                    _applyAgentEventToRun(run, {
                        ...(event.payload || {}), type: event.event_kind,
                        event_kind: event.event_kind, sequence: event.sequence,
                        run_id: `agent:${run.run_id}`, _historical: true,
                    });
                }
                _scheduleAgentRender();
                if (Number(run._lastSequence || 0) <= after) break;
            }
        } catch (err) {
            console.warn(`[Agent manager] event replay failed for ${run.run_id}`, err);
        }
    };
    run._replayJob = replay();
    try { await run._replayJob; }
    finally { run._replayJob = null; }
}

async function refreshAgentRuns() {
    if (!State.currentConvId) return;
    if (_agentRefreshJob) return _agentRefreshJob;
    const job = _refreshAgentRuns();
    _agentRefreshJob = job;
    try { await job; }
    finally { if (_agentRefreshJob === job) _agentRefreshJob = null; }
}

async function _refreshAgentRuns() {
    const requestedConv = State.currentConvId;
    const epoch = _agentViewEpoch;
    try {
        const [rows, dismissals] = await Promise.all([
            API.get(`/api/conversations/${requestedConv}/agent-runs`),
            API.get('/api/agent-notification-dismissals'),
        ]);
        for (const key of dismissals) _agentDismissals.add(key);
        if (epoch !== _agentViewEpoch || String(State.currentConvId) !== String(requestedConv)) return;
        const previous = new Map((State.agentRuns || []).map(run => [run.run_id, run]));
        if (!Array.isArray(rows)) return;
        const merged = rows.map(row => {
            const run = previous.get(row.run_id) || {};
            const newerState = Number(run._statusSequence || 0) > Number(row.event_sequence || 0)
                ? { status: run.status, result: run.result, error: run.error,
                    pid: run.pid, cancel_requested: run.cancel_requested } : {};
            Object.assign(run, row, newerState);
            run._statusSequence = Math.max(Number(run._statusSequence || 0), Number(row.event_sequence || 0));
            run._replayTarget = Math.max(Number(run._replayTarget || 0), Number(row.event_sequence || 0));
            // A completed result is already a full snapshot; don't replay its
            // entire token/tool history just to show a preview on a fresh page.
            if (!run._lastSequence && run.result?.text && !_ACTIVE_AGENT_RUN_STATUSES.has(run.status)) {
                run._lastSequence = Number(row.event_sequence || 0);
            }
            previous.delete(row.run_id);
            return run;
        });
        // A child may be announced while this HTTP snapshot is in flight.
        State.agentRuns = [...merged, ...previous.values()];
        const activeRuns = State.agentRuns.filter(run => (
            _ACTIVE_AGENT_RUN_STATUSES.has(run.status || 'queued')
        ));
        if (State.ws?.readyState === WebSocket.OPEN) {
            for (const run of activeRuns) {
                State.ws.send(JSON.stringify({
                    action: 'subscribe_run',
                    run_id: `agent:${run.run_id}`,
                }));
            }
        }
        _scheduleAgentRender(true);
        // Replay terminal runs too: a child may have finished while the tab was
        // disconnected. Limit simultaneous history requests to two per page.
        const pending = State.agentRuns.filter(run => Number(run._lastSequence || 0) < Number(run._replayTarget || 0));
        for (let i = 0; i < pending.length; i += 2) {
            if (epoch !== _agentViewEpoch) return;
            await Promise.all(pending.slice(i, i + 2).map(_replayAgentRun));
        }
        if (epoch === _agentViewEpoch) _scheduleAgentRender(true);
    } catch (err) {
        console.warn('[Agent manager] refresh failed', err);
    }
}

function _agentRunOutput(run) {
    if (!_ACTIVE_AGENT_RUN_STATUSES.has(run.status) && run.result?.text) return String(run.result.text);
    if (run.last_output) return String(run.last_output);
    if (typeof run.error === 'string' && run.error) return run.error;
    if (run.error?.message) return String(run.error.message);
    if (typeof run.result === 'string') return run.result;
    for (const key of ['text', 'output', 'message']) {
        if (run.result?.[key]) return String(run.result[key]);
    }
    return '';
}

function _closeAgentManager() {
    const panel = document.getElementById('agent-manager-panel');
    const toggle = document.getElementById('btn-agent-manager');
    panel?.classList.add('hidden');
    toggle?.setAttribute('aria-expanded', 'false');
    document.getElementById('workspace-agents')?.setAttribute('aria-expanded', 'false');
    if (typeof syncWorkspaceLayout === 'function') syncWorkspaceLayout();
}

const _agentDismissalsKey = 'exo-agent-notification-dismissals-v1';
function _readAgentDismissals() {
    try {
        const saved = JSON.parse(localStorage.getItem(_agentDismissalsKey) || '[]');
        return Array.isArray(saved) ? saved.filter(item => typeof item === 'string').slice(-2048).map(item => {
            // Previous keys included completed_at, which arrives after live terminal events.
            try { const key = JSON.parse(item); return JSON.stringify(key.slice(0, 3)); }
            catch { return item; }
        }) : [];
    } catch { return []; }
}
const _agentDismissals = new Set(_readAgentDismissals());
function _agentNotificationKey(run) {
    return JSON.stringify([run.run_id, run.status, run.started_at || null]);
}
function _agentHasNotification(run) {
    return ['completed', 'failed'].includes(run.status) && !_agentDismissals.has(_agentNotificationKey(run));
}
async function clearAgentNotifications() {
    const keys = (State.agentRuns || []).filter(_agentHasNotification).map(_agentNotificationKey);
    try {
        const saved = await API.post('/api/agent-notification-dismissals', {keys});
        for (const key of saved) _agentDismissals.add(key);
    } catch (error) {
        showToast(error.detail || 'Could not save notification dismissals', 'error');
        return;
    }
    for (const key of _readAgentDismissals()) _agentDismissals.add(key);
    while (_agentDismissals.size > 2048) _agentDismissals.delete(_agentDismissals.values().next().value);
    try { localStorage.setItem(_agentDismissalsKey, JSON.stringify([..._agentDismissals])); } catch {}
    renderAgentActivity();
}
window.addEventListener('storage', event => {
    if (event.key !== _agentDismissalsKey) return;
    for (const key of _readAgentDismissals()) _agentDismissals.add(key);
    renderAgentActivity();
});

function _bindAgentManager() {
    const anchor = document.getElementById('agent-manager-anchor');
    const toggle = document.getElementById('btn-agent-manager');
    const panel = document.getElementById('agent-manager-panel');
    const close = document.getElementById('agent-manager-close');
    const refresh = document.getElementById('agent-activity-refresh');
    const list = document.getElementById('agent-activity-list');
    if (!anchor || !toggle || !panel || !list || anchor.dataset.bound) return;
    anchor.dataset.bound = '1';
    const clear = document.createElement('button');
    clear.id = 'agent-clear-notifications';
    clear.type = 'button';
    clear.textContent = 'Clear notifications';
    clear.title = 'Dismiss completed and failed counts; keep all agent history';
    clear.addEventListener('click', clearAgentNotifications);
    panel.querySelector('.agent-manager-header-actions').prepend(clear);

    toggle.addEventListener('click', async () => {
        const opening = panel.classList.contains('hidden');
        panel.classList.toggle('hidden', !opening);
        toggle.setAttribute('aria-expanded', opening ? 'true' : 'false');
        document.getElementById('workspace-agents')?.setAttribute('aria-expanded', String(opening));
        if (opening) {
            if (State.currentView === 'chat') {
                workspaceSetOpen('right', false, false);
                if (isMobileDevice()) {
                    workspaceSetOpen('left', false, false);
                    setWorkspaceCanvasOpen(false);
                }
            }
            renderAgentActivity();
            if (typeof syncWorkspaceLayout === 'function') syncWorkspaceLayout();
            await refreshAgentRuns();
        } else if (typeof syncWorkspaceLayout === 'function') syncWorkspaceLayout();
    });
    close?.addEventListener('click', _closeAgentManager);
    refresh?.addEventListener('click', refreshAgentRuns);
    document.getElementById('agent-dock-attention')?.addEventListener('click', async () => {
        if (panel.classList.contains('hidden')) toggle.click();
        renderAgentActivity();
        const waiting = list.querySelector('[data-status="permission"]');
        if (waiting) { waiting.open = true; waiting.scrollIntoView({block: 'nearest'}); }
    });
    list.addEventListener('click', async event => {
        const release = event.target.closest('[data-agent-release]');
        if (release) {
            event.preventDefault();
            event.stopPropagation();
            const card = release.closest('.agent-run-card');
            const text = card.querySelector('.agent-release-text').value.trim();
            if (!text) { showToast('Enter the handoff to release.', 'error'); return; }
            release.disabled = true;
            try {
                await API.post(`/api/agent-runs/${encodeURIComponent(card.dataset.runId)}/release`, {text});
                release.textContent = 'Handoff released';
            } catch (error) {
                showToast(error.detail || error.message || 'Could not release handoff', 'error');
                release.disabled = false;
            }
            return;
        }
        const review = event.target.closest('[data-agent-permission]');
        if (review) {
            event.preventDefault();
            event.stopPropagation();
            const card = review.closest('.agent-run-card');
            card.open = true;
            card.querySelector('.agent-permissions')?.scrollIntoView({block: 'nearest'});
            return;
        }
        const cancel = event.target.closest('[data-agent-cancel]');
        const resume = event.target.closest('[data-agent-resume]');
        if (!cancel && !resume) return;
        event.preventDefault();
        event.stopPropagation();
        const action = cancel || resume;
        const runId = cancel?.dataset.agentCancel || resume?.dataset.agentResume;
        if (!runId || action.disabled) return;
        const run = _agentRunForEvent(runId);
        const epoch = _agentViewEpoch;
        const statusSequence = Number(run._statusSequence || 0);
        run._actionPending = cancel ? 'cancel' : 'resume';
        action.disabled = true;
        action.textContent = cancel ? 'Stopping…' : 'Resuming…';
        try {
            const verb = cancel ? 'cancel' : 'resume';
            const updated = await API.post(`/api/agent-runs/${encodeURIComponent(runId)}/${verb}`);
            if (_agentRunIsCurrent(run, epoch)) {
                const newerState = Number(run._statusSequence || 0) > statusSequence
                    ? {status: run.status, result: run.result, error: run.error,
                        pid: run.pid, cancel_requested: run.cancel_requested} : {};
                Object.assign(run, updated, newerState);
            }
        } catch (err) {
            if (_agentRunIsCurrent(run, epoch)) {
                showToast(err.detail || err.message || `Could not ${cancel ? 'cancel' : 'resume'} subagent`, 'error');
            }
        } finally {
            run._actionPending = null;
            if (_agentRunIsCurrent(run, epoch)) _scheduleAgentRender(true);
        }
    });
    document.addEventListener('click', event => {
        if (State.currentView !== 'chat' && !panel.classList.contains('hidden') && !anchor.contains(event.target)) {
            _closeAgentManager();
        }
    });
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && !panel.classList.contains('hidden')) _closeAgentManager();
    });
}

function renderAgentActivity() {
    if (document.hidden) { _agentRenderDirty = true; return; }
    _agentRenderDirty = false;
    const anchor = document.getElementById('agent-manager-anchor');
    const panel = document.getElementById('agent-manager-panel');
    const list = document.getElementById('agent-activity-list');
    const count = document.getElementById('agent-manager-count');
    if (!anchor || !panel || !list || !count) return;
    _bindAgentManager();

    const target = _agentManagerTarget();
    anchor.classList.toggle('hidden', !target);
    if (!target) {
        _closeAgentManager();
        return;
    }
    const placement = target;
    if (State.currentView === 'chat') {
        const canvas = placement.querySelector('#btn-canvas-toggle');
        if (anchor.parentElement !== placement || (canvas && anchor.nextElementSibling !== canvas)) placement.insertBefore(anchor, canvas);
    } else if (anchor.parentElement !== placement) placement.appendChild(anchor);

    const allRuns = State.agentRuns || [];
    const activeCount = allRuns.filter(run => _ACTIVE_AGENT_RUN_STATUSES.has(run.status || 'queued')).length;
    _agentSetText(count, String(activeCount));
    count.classList.toggle('hidden', activeCount === 0);
    count.title = `${activeCount} running or queued`;
    count.setAttribute('aria-label', count.title);
    const workspaceCount = document.getElementById('workspace-agent-count');
    if (workspaceCount) {
        _agentSetText(workspaceCount, String(activeCount));
        workspaceCount.classList.toggle('hidden', activeCount === 0);
    }
    anchor.classList.toggle('has-active-agents', activeCount > 0);
    const pendingCount = allRuns.reduce((sum, run) => sum + Object.keys(run._permissions || {}).length, 0);
    const finished = allRuns.filter(run => run.status === 'completed' && _agentHasNotification(run)).length;
    const failures = allRuns.filter(run => run.status === 'failed' && _agentHasNotification(run)).length;
    document.getElementById('agent-clear-notifications').disabled = finished + failures === 0;
    const status = document.getElementById('agent-dock-status');
    for (const [kind, total, label] of [['completed', finished, 'completed'], ['failed', failures, 'failed']]) {
        let badge = status.querySelector(`.agent-dock-${kind}`);
        if (!badge) {
            if (!status.querySelector('.agent-dock-badge')) status.replaceChildren();
            badge = document.createElement('span');
            badge.className = `agent-dock-badge agent-dock-${kind}`;
            status.appendChild(badge);
        }
        _agentSetText(badge, String(total));
        badge.classList.toggle('hidden', total === 0);
        badge.title = `${total} ${label}`;
        badge.setAttribute('aria-label', badge.title);
    }
    const attention = document.getElementById('agent-dock-attention');
    const attentionCount = allRuns.filter(run => Object.keys(run._permissions || {}).length || run._permissionPending
        || ['needs_relaunch', 'paused', 'blocked'].includes(run.status)).length;
    _agentSetText(attention, `! ${attentionCount}`);
    attention.title = `${attentionCount} agents need attention (${pendingCount} pending approvals)`;
    attention.setAttribute('aria-label', attention.title);
    attention.classList.toggle('hidden', !attentionCount);
    // A closed panel needs only the badge. Do not allocate/rebuild its cards.
    if (panel.classList.contains('hidden')) return;
    const runs = [...allRuns].sort((a, b) =>
        Number(_ACTIVE_AGENT_RUN_STATUSES.has(b.status)) - Number(_ACTIVE_AGENT_RUN_STATUSES.has(a.status))
    ).slice(0, 20);
    const runMap = new Map(runs.map(run => [String(run.run_id), run]));
    const depthFor = (run, seen = new Set()) => {
        const runId = String(run.run_id || '');
        const parentId = String(run.parent_run_id || '').replace(/^agent:/, '');
        if (!parentId || parentId.startsWith('gen:') || seen.has(runId)) return 0;
        const parent = runMap.get(parentId);
        if (!parent) return 0;
        return Math.min(4, 1 + depthFor(parent, new Set([...seen, runId])));
    };
    for (const run of runs) run._depth = depthFor(run);
    if (list.dataset.conversationId !== String(State.currentConvId)) {
        list.replaceChildren();
        list.dataset.conversationId = String(State.currentConvId);
    }
    const oldCards = new Map([...list.querySelectorAll('.agent-run-card')].map(el => [el.dataset.runId, el]));
    const scrollTop = list.scrollTop;
    const top = list.getBoundingClientRect().top;
    const visibleCard = [...oldCards.values()].find(el => el.getBoundingClientRect().bottom > top);
    const visibleOffset = visibleCard ? visibleCard.getBoundingClientRect().top - top : 0;

    if (!runs.length) {
        if (!list.querySelector('.agent-manager-empty')) {
            const empty = document.createElement('div');
            empty.className = 'agent-manager-empty';
            empty.textContent = 'No Loom-managed subagents in this conversation.';
            list.replaceChildren(empty);
        }
        return;
    }
    list.querySelector('.agent-manager-empty')?.remove();

    for (const [index, run] of runs.entries()) {
        const statusName = run.status || 'queued';
        const displayStatus = run._permissionPending ? 'permission' : statusName;
        const details = oldCards.get(String(run.run_id)) || _createAgentCard(run.run_id);
        oldCards.delete(String(run.run_id));
        details.dataset.status = displayStatus;
        details.style.setProperty('--agent-indent', `${Number(run._depth || 0) * 12}px`);
        _agentSetText(details.querySelector('.agent-status'), displayStatus === 'permission' ? 'Needs approval' : displayStatus.replaceAll('_', ' '));
        _agentSetText(details.querySelector('.agent-task'), (run.task || '(untitled task)').split('\n')[0].slice(0, 140));
        _agentSetText(details.querySelector('.agent-elapsed'), _activityElapsed(run.started_at || run.created_at));
        const waitLabel = {workspace_conflict: 'Waiting for workspace access', inference_capacity: 'Waiting for inference capacity', worker_capacity: 'Waiting for worker capacity'}[run.queue_reason];
        _agentSetText(details.querySelector('.agent-meta'), `${run.effective_harness || run.requested_harness || 'agent'} · ${run.effective_model || run.requested_model || run.model_profile || 'default'}${run.access?.sensitive || run.sensitive ? ' · Sensitive local task' : ''}${statusName === 'queued' && waitLabel ? ' · ' + waitLabel : ''}`);
        _agentSetText(details.querySelector('.agent-full-task'), run.task || '');
        _agentSetText(details.querySelector('.agent-full-result'), typeof run.result?.text === 'string' ? run.result.text : '');
        details.querySelector('.agent-result-details').classList.toggle('hidden', !run.result?.text);
        details.querySelector('.agent-release').classList.toggle('hidden', !run.access?.sensitive || statusName !== 'completed');
        const error = details.querySelector('.agent-error');
        _agentSetText(error, String(run.error?.message || run.error || ''));
        error.classList.toggle('hidden', !run.error);
        const output = details.querySelector('.agent-output');
        const outputText = _agentRunOutput(run).slice(-1600);
        const wasAtBottom = output.scrollHeight - output.clientHeight - output.scrollTop < 4;
        const outputScroll = output.scrollTop;
        _agentSetText(output, outputText);
        output.classList.toggle('hidden', !outputText);
        output.scrollTop = wasAtBottom ? output.scrollHeight : outputScroll;
        const active = _ACTIVE_AGENT_RUN_STATUSES.has(statusName);
        const recoverable = run.pid == null && !run.cancel_requested &&
            (statusName === 'paused' || (statusName === 'failed' && !run.image_request));
        const cancel = details.querySelector('[data-agent-cancel]');
        const resume = details.querySelector('[data-agent-resume]');
        const review = details.querySelector('[data-agent-permission]');
        cancel.classList.toggle('hidden', !active);
        resume.classList.toggle('hidden', !recoverable);
        resume.title = 'Continue from retained evidence; reconcile uncertain tool outcomes before repeating actions';
        review.classList.toggle('hidden', !active || !run._permissionPending);
        _renderAgentPermissions(details, run);
        cancel.disabled = statusName === 'cancel_requested' || !!run._actionPending;
        resume.disabled = !!run._actionPending;
        _agentSetText(cancel, statusName === 'cancel_requested' || run._actionPending === 'cancel' ? 'Stopping…' : 'Cancel');
        _agentSetText(resume, run._actionPending === 'resume' ? 'Resuming…' : 'Resume');
        details.querySelector('.agent-run-actions').classList.toggle('hidden', !active && !recoverable);
        if (list.children[index] !== details) list.insertBefore(details, list.children[index] || null);
    }
    for (const old of oldCards.values()) old.remove();
    list.scrollTop = scrollTop;
    if (visibleCard?.isConnected) list.scrollTop += visibleCard.getBoundingClientRect().top - top - visibleOffset;
}

function _agentSetText(node, text) {
    if (node.textContent !== text) node.textContent = text;
}

function _renderAgentPermissions(card, run) {
    const container = card.querySelector('.agent-permissions');
    const pending = new Set(Object.keys(run._permissions || {}));
    for (const node of [...container.children]) if (!pending.has(node.dataset.requestId)) node.remove();
    for (const requestId of pending) {
        if ([...container.children].some(node => node.dataset.requestId === requestId)) continue;
        const notification = _notifications.find(n => n.type === 'permission' && n.requestId === requestId);
        if (!notification) continue;
        const node = _renderPermissionNotifItem(notification);
        node.dataset.requestId = requestId;
        container.appendChild(node);
    }
}

function _createAgentCard(runId) {
    const details = document.createElement('details');
    details.className = 'agent-run-card';
    details.dataset.runId = runId;
    // Static skeleton only; all provider/task content is assigned as text.
    details.innerHTML = `<summary><span class="agent-status"></span><span class="agent-task"></span><span class="agent-elapsed"></span></summary>
        <div class="agent-run-details"><div class="agent-meta"></div><details class="agent-task-details"><summary>Full task and inputs</summary><pre class="agent-full-task"></pre></details>
        <div class="agent-permissions"></div>
        <details class="agent-result-details hidden"><summary>Full result</summary><pre class="agent-full-result"></pre></details>
        <details class="agent-release hidden"><summary>Release a handoff to the parent</summary><p>Only this text will be shared. Raw logs stay private.</p><textarea class="agent-release-text" rows="4" maxlength="32000" aria-label="Handoff to release"></textarea><button type="button" data-agent-release>Release handoff</button></details>
        <div class="agent-error hidden"></div><pre class="agent-output hidden"></pre>
        <div class="agent-run-actions"><button type="button" data-agent-cancel>Cancel</button>
        <button type="button" data-agent-resume>Resume</button><button type="button" data-agent-permission>Review request</button></div></div>`;
    details.querySelector('[data-agent-cancel]').dataset.agentCancel = runId;
    details.querySelector('[data-agent-resume]').dataset.agentResume = runId;
    details.querySelector('[data-agent-permission]').dataset.agentPermission = runId;
    return details;
}

function _applyAgentEventToRun(run, data) {
    if (!run) return;
    const kind = data.event_kind || data.type || '';
    // Approval UUIDs, not stream sequence numbers, identify permission state.
    // Bridge events and persisted child output have independent sequencers.
    if (!data._historical && (kind === 'permission_request' || kind === 'permission_resolved')) {
        if (!data.request_id) return;
        run._permissions = run._permissions || {};
        run._resolvedPermissions = run._resolvedPermissions || new Set();
        if (kind === 'permission_resolved') {
            run._resolvedPermissions.add(data.request_id);
            delete run._permissions[data.request_id];
        } else if (!run._resolvedPermissions.has(data.request_id)) {
            run._permissions[data.request_id] = data;
        }
        run._permissionPending = Object.keys(run._permissions).length > 0;
        return;
    }
    const sequence = Number(data.sequence || 0);
    if (sequence && sequence <= Number(run._lastSequence || 0)) return;
    run._replayTarget = Math.max(Number(run._replayTarget || 0), sequence);
    if (sequence > Number(run._lastSequence || 0) + 1) {
        run._pendingEvents = run._pendingEvents || new Map();
        // Bound memory while reconnect catches up; overflow is still in SQLite.
        if (run._pendingEvents.size < 512) run._pendingEvents.set(sequence, data);
        if (kind.startsWith('agent_')) _applyAgentPayload(run, data, false);
        return;
    }
    if (sequence) run._lastSequence = sequence;
    run._pendingEvents?.delete(sequence);
    _applyAgentPayload(run, data);
    while (run._pendingEvents?.has(Number(run._lastSequence || 0) + 1)) {
        const next = Number(run._lastSequence || 0) + 1;
        const pending = run._pendingEvents.get(next);
        run._pendingEvents.delete(next);
        run._lastSequence = next;
        _applyAgentPayload(run, pending);
    }
}

function _applyAgentPayload(run, data, includeOutput = true) {
    const kind = data.event_kind || data.type || '';
    const sequence = Number(data.sequence || 0);
    if (kind.startsWith('agent_')) {
        if (sequence && sequence <= Number(run._statusSequence || 0)) return;
        if (sequence) run._statusSequence = sequence;
        // Explicit teardown evidence, never infer process exit merely from a
        // failure label. Older events without this field retain their PID.
        if (Object.prototype.hasOwnProperty.call(data, 'pid')) run.pid = data.pid;
        if (Object.prototype.hasOwnProperty.call(data, 'cancel_requested')) run.cancel_requested = data.cancel_requested;
    }
    if (kind === 'agent_queued') {
        run.status = 'queued';
        if (data.task) run.task = data.task;
    } else if (kind === 'agent_waiting') {
        run.queue_reason = data.queue_reason;
    } else if (kind === 'agent_started') {
        run.status = 'running';
        if (data.started_at != null) run.started_at = data.started_at;
        run.queue_reason = null;
        if (data.task) run.task = data.task;
    } else if (kind === 'agent_completed') {
        run.status = data.status || 'completed';
        if (data.result) run.result = data.result;
    }
    else if (kind === 'agent_cancelled') run.status = 'cancelled';
    else if (kind === 'agent_paused') run.status = 'paused';
    else if (kind === 'agent_failed') { run.status = 'failed'; run.error = data.error || ''; }
    else if (kind === 'agent_stop_failed') { run.status = 'cancel_requested'; run.error = data.error || ''; }
    else if (kind === 'agent_launch_info') {
        for (const key of ['effective_harness', 'effective_model', 'requested_harness', 'requested_model', 'profile']) {
            if (data[key] != null) run[key] = data[key];
        }
    } else if (includeOutput && (kind === 'stream_chunk' || kind === 'text_delta')) {
        run.last_output = `${run.last_output || ''}${data.content || data.text || ''}`.slice(-1600);
    }
}

function _handleAgentEvent(data) {
    if (data.conv_id && !_sameConvId(data.conv_id, State.currentConvId)) return;
    if ((data.event_kind || data.type) === 'agent_event_batch') {
        for (const item of (data.events || [])) {
            if (item.run_id === data.run_id && item.conv_id === data.conv_id) _handleAgentEvent(item);
        }
        return;
    }
    const run = _agentRunForEvent(data.run_id);
    if (!run) return;
    _applyAgentEventToRun(run, data);
    const kind = data.event_kind || data.type;
    if (kind === 'permission_request' && run._permissions?.[data.request_id]) {
        addPermissionNotification({ ...data, agent_run_id: run.run_id });
    } else if (kind === 'permission_resolved') {
        resolvePermissionNotification(data.request_id, data.allowed, data.feedback || '', data.decision_source);
    } else if (['agent_completed', 'agent_failed', 'agent_cancelled', 'agent_paused', 'agent_stop_failed'].includes(kind)
        && (!data.sequence || Number(data.sequence) >= Number(run._statusSequence || 0))
        && ['completed', 'failed', 'cancelled', 'paused', 'cancel_requested'].includes(run.status)) {
        for (const requestId of Object.keys(run._permissions || {})) {
            _applyAgentEventToRun(run, { type: 'permission_resolved', request_id: requestId });
            resolvePermissionNotification(requestId, false, 'Child run ended before a decision.', 'loom_lifecycle');
        }
    }
    if (Number(run._lastSequence || 0) < Number(run._replayTarget || 0)) void _replayAgentRun(run);
    _scheduleAgentRender(kind === 'permission_request' || kind === 'permission_resolved' || kind.startsWith('agent_'));
}

const _resolvedPermissionNoticeIds = new Set();
const _dismissedPermissionNoticeIds = new Set((() => {
    try {const value=JSON.parse(localStorage.getItem('exo-dismissed-permission-notices') || '[]');
        return Array.isArray(value)?value.filter(id=>typeof id==='string').slice(-512):[];
    } catch {return [];}
})());
function _dismissPermissionNotice(requestId, render = true) {
    _dismissedPermissionNoticeIds.add(String(requestId));
    while (_dismissedPermissionNoticeIds.size > 512) _dismissedPermissionNoticeIds.delete(_dismissedPermissionNoticeIds.values().next().value);
    try {localStorage.setItem('exo-dismissed-permission-notices',JSON.stringify([..._dismissedPermissionNoticeIds]));} catch {}
    for (let i=_notifications.length-1;i>=0;i--) {
        if (_notifications[i].type==='permission-notice' && _notifications[i].requestId===requestId) _notifications.splice(i,1);
    }
    if(render){_renderNotifBell();_renderNotifDropdown();}
}
function handleWSMessage(data) {
    // Child runs are durable supervisor activity, not conversation messages.
    // Consume them before any legacy chat switch can append or navigate.
    if (data?.scope === 'run' && String(data.run_id || '').startsWith('agent:')) {
        _handleAgentEvent(data);
        return;
    }
    if (['stream_end', 'generation_terminal', 'cancelled', 'interrupted', 'error'].includes(data.type)
        && data.conv_id != null && _canonicalClientRunId(data)) {
        _recentTerminalRuns.set(_terminalRunKey(data.conv_id, _canonicalClientRunId(data)),
            data.message?.id ?? data.draft_msg_id ?? null);
        if (_recentTerminalRuns.size > 128) {
            _recentTerminalRuns.delete(_recentTerminalRuns.keys().next().value);
        }
    }
    if (_queueStreamEventDuringReconstruct(data)) return;
    if (_ACTIVITY_EVENT_TYPES.has(data.type) && _isOurBranch(data)) _removeStreamWaiting();
    switch (data.type) {
        case 'chat_item': {
            if (!_isOurBranch(data)) break;
            const message = State.messages.find(m => _sameMessageId(m.id, data.draft_msg_id));
            if (message) {
                let items = parseChatItems(message.chat_items);
                if (!items.some(item => item.id === data.item.id)) items.push(data.item);
                message.chat_items = JSON.stringify(items);
                if (_sameMessageId(State._liveStreamDraft?.id, message.id)) State._liveStreamDraft.chat_items = message.chat_items;
            }
            const element = document.querySelector(`.message[data-msg-id="${data.draft_msg_id}"]`);
            if (element) {
                let container = element.querySelector('.chat-presentations');
                if (!container) {
                    container = document.createElement('div');
                    container.className = 'chat-presentations';
                    element.appendChild(container);
                }
                if (!container.querySelector(`[data-chat-item-id="${CSS.escape(data.item.id)}"]`)) {
                    container.insertAdjacentHTML('beforeend', renderChatItem(data.item));
                }
                scrollToBottom();
            }
            break;
        }
        case 'permission_notice': {
            const index = _notifications.findIndex(n => n.requestId === data.request_id
                && (n.type === 'permission' || n.type === 'permission-notice'));
            if (data.status === 'resolved') {
                _resolvedPermissionNoticeIds.add(data.request_id);
                if (_resolvedPermissionNoticeIds.size > 512) _resolvedPermissionNoticeIds.delete(_resolvedPermissionNoticeIds.values().next().value);
                if (index !== -1 && _notifications[index].type === 'permission-notice') _notifications.splice(index, 1);
            } else if (data.status === 'pending' && index === -1 && !_resolvedPermissionNoticeIds.has(data.request_id)
                && !_dismissedPermissionNoticeIds.has(String(data.request_id))) {
                _notifications.unshift({type: 'permission-notice', requestId: data.request_id,
                    convId: data.conv_id, messageId: data.message_id, time: new Date()});
            }
            _renderNotifBell();
            if (!document.getElementById('notif-dropdown')?.classList.contains('hidden')) _renderNotifDropdown();
            break;
        }
        case 'generation_terminal': {
            // Heartbeat recovery of a dropped terminal event. A stale reply
            // cannot finish a newer stream, even after gen IDs are reused.
            if (!_sameMessageId(data.conv_id, State.currentConvId)
                || _canonicalClientRunId(data) !== `gen:${State._followingGenId}`
                || !_sameMessageId(data.message?.id, State._followingDraftMsgId)) break;
            const visible = streamingDiv?.isConnected ? streamingDiv
                : document.querySelector(`.message[data-msg-id="${data.message.id}"]`);
            if (visible && !visible.querySelector('.edit-message-input')) {
                streamingDiv = visible;
                finalizeStreamingMessage(data.message, null, {preserveViewport: true});
            } else {
                _stopGenTimer();
                _stopStreamWaitingTicker();
                const index = State.messages.findIndex(m => _sameMessageId(m.id, data.message.id));
                if (index >= 0) State.messages[index] = data.message;
                streamingDiv = null;
            }
            _clearTerminalStreamState();
            hideGenStatus();
            const lifecycle = data.message.turn_lifecycle_state;
            setBrowserTabState(['failed', 'partial_failed'].includes(lifecycle) ? 'error'
                : ['cancelled', 'interrupted'].includes(lifecycle) ? 'idle' : 'done');
            // Do not navigate, dispatch queues, replace editors, or announce
            // completion again. This only repairs the exact message card.
            refreshTree();
            break;
        }
        case 'run_subscription':
            if (data.subscribed && data.run_id) _subscribedRunIds.add(String(data.run_id));
            if (data.removed && data.run_id) _subscribedRunIds.delete(String(data.run_id));
            break;

        case 'agent_event':
            _handleAgentEvent(data);
            break;

        case 'generation_preparing': {
            if (_generationWindowOwnership(data) !== true) break;
            const runId = _canonicalClientRunId(data);
            const sameTurn = runId && (runId === _pendingLaunch?.runId
                || runId === `gen:${State._followingGenId}`);
            if (!sameTurn || !_streamStartTime) _streamStartTime = Date.now();
            if (runId) _subscribedRunIds.add(runId);
            State._streamIsOurBranch = true;
            State._followingGenId = data.gen_id ?? State._followingGenId ?? null;
            State._followingDraftMsgId = data.draft_msg_id ?? null;
            State.isStreaming = true;
            _pendingLaunch = {
                genId: data.gen_id ?? null,
                runId,
                parentId: data.parent_id ?? null,
                threadId: data.loom_thread_id ?? null,
            };
            if (!_streamStartTime) _streamStartTime = Date.now();
            if (!streamingDiv || !streamingDiv.isConnected) {
                appendStreamingMessage(data.cc_model || data.local_model);
            }
            showGenStatus(data.text || 'Preparing turn…');
            break;
        }

        case 'context_info':
            if (!_isOurBranch(data)) break;
            updateContextInfo(data);
            if (data.was_compactified) {
                const summ = data.summarized_count || '?';
                const verb = data.verbatim_count || '?';
                showGenStatus(`Context: ${data.total_tokens.toLocaleString()} tokens (${summ} summarized, ${verb} verbatim) — Waiting for model...`);
            } else {
                showGenStatus(`Context: ${data.total_tokens.toLocaleString()} tokens — Waiting for model...`);
            }
            break;

        case 'compact_boundary':
            if (!_isOurBranch(data)) break;
            // Claude Code compactified its context — forked into a new branch.
            // When streaming, we CAN'T call switchToBranch — it rebuilds the
            // DOM and detaches the streamingDiv, so the incoming stream chunks
            // pile into an orphan div. Instead, splice the marker into State
            // and insert it DIRECTLY BEFORE the streaming div so the visual
            // order matches the tree (parent → [compact marker] → draft).
            State._compactedThisGen = true;
            State._compactData = data;
            showGenStatus('Context compactified — continuing generation...');
            if (data.marker_id) {
                const trigger = data.trigger || 'auto';
                const preTokens = data.pre_tokens;
                const tokenInfo = preTokens ? ` — ${preTokens.toLocaleString()} tokens before` : '';
                const markerContent = `[CC context compactified (${trigger})${tokenInfo}]`;
                // Find the draft (streaming) message — it should be the active leaf.
                const draftMsg = State.messages.find(m => m.role === 'assistant' && !m.content?.trim());
                const markerMsg = {
                    id: data.marker_id,
                    role: 'system',
                    content: markerContent,
                    parent_id: draftMsg ? draftMsg.parent_id : null,
                };
                // Re-parent the draft under the marker in local state so ordering is stable.
                if (draftMsg) draftMsg.parent_id = data.marker_id;

                // Insert into State.messages right before the draft (or at end)
                if (draftMsg) {
                    const draftIdx = State.messages.indexOf(draftMsg);
                    if (draftIdx >= 0) {
                        State.messages.splice(draftIdx, 0, markerMsg);
                    } else {
                        State.messages.push(markerMsg);
                    }
                } else {
                    State.messages.push(markerMsg);
                }

                // DOM insert: put the marker right before streamingDiv if it
                // exists and is attached, else just append to messages container.
                const container = document.getElementById('messages');
                const markerEl = createMessageElement(markerMsg);
                if (streamingDiv && streamingDiv.parentNode === container) {
                    container.insertBefore(markerEl, streamingDiv);
                } else if (container) {
                    container.appendChild(markerEl);
                }
                refreshTree();
            } else {
                loadMessages(State.currentConvId);
            }
            break;

        case 'compact_done':
            if (!_isOurBranch(data)) break;
            if (data.checkpoint_only) {
                hideGenStatus();
                if (data.success) showToast('Checkpoint saved. Your next turn will start from it; original history is preserved.', 6000);
                break; // No message/cursor changed; never rebuild the viewport.
            }
            // Compaction finished — clear status and reload to show the new branch
            hideGenStatus();
            loadMessages(State.currentConvId);
            break;

        case 'compact_summary_ready':
            if (!_isOurBranch(data)) break;
            // Background task patched the marker row with the narrative summary.
            // Update the marker element in place (if it's visible) so the user
            // can expand "Previously" without a reload.
            if (data.marker_id) {
                const markerEl = document.querySelector(`.compact-marker[data-msg-id="${data.marker_id}"]`);
                if (markerEl) {
                    const summaryBox = markerEl.querySelector('.compact-marker-summary');
                    if (summaryBox && data.summary) {
                        summaryBox.innerHTML = formatContent(data.summary);
                    }
                }
                // Sync State.messages so re-renders keep the summary
                const m = State.messages?.find(x => x.id === data.marker_id);
                if (m && data.summary) {
                    const header = (m.content || '').split('\n', 1)[0];
                    m.content = header + '\n\n---\nPreviously:\n' + data.summary;
                }
            }
            break;

        case 'status':
            if (!_isOurBranch(data)) break;
            showGenStatus(data.text || 'Looming...');
            break;

        case 'first_token': {
            if (!_isOurBranch(data)) break;
            _removeStreamWaiting();
            const draft = _ensureLiveStreamDraft(data);
            if (draft) draft.time_to_first_token_ms = data.elapsed_ms;
            break;
        }

        case 'model_attestation': {
            if (!_isOurBranch(data)) break;
            const attestation = _parseModelAttestation(data.attestation);
            if (!attestation) break;
            _updateStreamingModelAttestation(attestation);
            const draft = _ensureLiveStreamDraft(data)
                || State.messages?.find(m => _sameMessageId(m.id, data.draft_msg_id));
            if (draft) draft.model_attestation = JSON.stringify(attestation);
            break;
        }

        case 'conv_field_update':
            // Server resolved a field after the fact (e.g. the model Hermes
            // actually used when the conv had none pinned).
            if (State.currentConv) {
                for (const [k, v] of Object.entries(data)) {
                    if (k !== 'type') State.currentConv[k] = v;
                }
            }
            break;

        case 'image_describe': {
            const msg = State.messages.find(m => m.id === data.message_id);
            if (msg) {
                let existing = {};
                if (msg.image_alt) {
                    try { existing = JSON.parse(msg.image_alt) || {}; } catch { existing = {}; }
                }
                msg.image_alt = JSON.stringify({ ...existing, ...(data.descriptions || {}) });
            }
            const msgEl = document.querySelector(`.message[data-msg-id="${data.message_id}"]`);
            if (msgEl && data.descriptions) {
                for (const [filename, desc] of Object.entries(data.descriptions)) {
                    const figs = msgEl.querySelectorAll(`.message-image-figure[data-img-name="${CSS.escape(filename)}"]`);
                    figs.forEach(fig => {
                        let cap = fig.querySelector('.image-description');
                        if (!cap) {
                            cap = document.createElement('figcaption');
                            cap.className = 'image-description';
                            cap.title = 'From vision model';
                            fig.appendChild(cap);
                        }
                        cap.textContent = desc;
                    });
                }
            }
            break;
        }

        case 'describe_start': {
            // Create a describe message element with spinner
            const parentMsgEl = document.querySelector(`.message[data-parent-msg-id="${data.parent_msg_id}"]`)
                || document.querySelector(`.message[data-msg-id="${data.parent_msg_id}"]`);
            const container = document.getElementById('messages');
            const describeDiv = document.createElement('div');
            describeDiv.className = 'message system message-describing';
            describeDiv.dataset.parentMsgId = data.parent_msg_id;
            describeDiv.dataset.describePending = 'true';

            const modelLabel = data.model || 'vision model';
            describeDiv.innerHTML =
                '<div class="message-header">' +
                    '<div class="message-header-left">' +
                        '<span class="describe-label">Describing Image — ' + escapeHtml(modelLabel) + '</span>' +
                    '</div>' +
                    '<div class="message-actions">' +
                        '<span class="gen-timer">0:00</span>' +
                    '</div>' +
                '</div>' +
                '<div class="describe-content">' +
                    '<span class="describe-spinner">' +
                        '<span class="loom-anim"></span>' +
                        ' Describing...' +
                    '</span>' +
                '</div>';

            if (parentMsgEl && parentMsgEl.nextSibling) {
                container.insertBefore(describeDiv, parentMsgEl.nextSibling);
            } else {
                container.appendChild(describeDiv);
            }

            // Start timer
            if (!State._describeTimers) State._describeTimers = {};
            State._describeTimers[data.parent_msg_id] = {
                start: Date.now(),
                element: describeDiv,
                interval: setInterval(() => {
                    const elapsed = Math.floor((Date.now() - State._describeTimers[data.parent_msg_id].start) / 1000);
                    const timerEl = describeDiv.querySelector('.gen-timer');
                    if (timerEl) timerEl.textContent = _formatElapsed(elapsed);
                }, 1000)
            };

            showGenStatus('Describing image (' + modelLabel + ')...');
            break;
        }

        case 'describe_done': {
            // Update the describe message with results
            const describeDiv = document.querySelector(`.message-describing[data-parent-msg-id="${data.parent_msg_id}"]`);

            // Also update image_describe for backwards compat
            const msg = State.messages.find(m => m.id === data.parent_msg_id);
            if (msg) {
                let existing = {};
                if (msg.image_alt) {
                    try { existing = JSON.parse(msg.image_alt) || {}; } catch { existing = {}; }
                }
                msg.image_alt = JSON.stringify({ ...existing, ...(data.descriptions || {}) });
            }

            if (describeDiv) {
                const timerData = State._describeTimers && State._describeTimers[data.parent_msg_id];
                if (timerData && timerData.interval) {
                    clearInterval(timerData.interval);
                    const elapsed = Math.floor(data.elapsed_ms / 1000);
                    const timerEl = describeDiv.querySelector('.gen-timer');
                    if (timerEl) timerEl.textContent = _formatElapsed(elapsed);
                }

                // Build description text
                let descText = '';
                if (data.descriptions) {
                    for (const [filename, desc] of Object.entries(data.descriptions)) {
                        if (descText) descText += '\n\n';
                        descText += desc;
                    }
                }

                const contentDiv = describeDiv.querySelector('.describe-content');
                if (contentDiv) {
                    contentDiv.innerHTML = formatContent(descText);
                }

                // Add reroll button
                const actionsDiv = describeDiv.querySelector('.message-actions');
                if (actionsDiv && !describeDiv.querySelector('.describe-reroll')) {
                    const rerollBtn = document.createElement('button');
                    rerollBtn.className = 'btn-small describe-reroll';
                    rerollBtn.innerHTML = '&#x21BB;';
                    rerollBtn.title = 'Re-describe';
                    rerollBtn.onclick = () => rerollDescribe(State.currentConvId, data.parent_msg_id);
                    actionsDiv.insertBefore(rerollBtn, actionsDiv.firstChild);
                }

                describeDiv.dataset.describePending = 'false';
            }

            showGenStatus('Image described — Generating...');
            break;
        }

        case 'stream_start': {
            const parentId = data.parent_id;
            const windowOwnership = _generationWindowOwnership(data);
            const exactRunOwned = windowOwnership === true;
            const eventRunId = _canonicalClientRunId(data);
            const pendingMatch = !!(_pendingLaunch && eventRunId
                && _pendingLaunch.runId === eventRunId);
            // If the isStreaming flag leaked from a previous turn (no live
            // streamingDiv attached) clear it now so we actually create a
            // fresh div for this stream. Without this, stream chunks fall
            // into the void and the user has to refresh to see anything.
            if (State.isStreaming && !_isActuallyStreaming()) {
                console.log('[WS] stream_start: clearing leaked isStreaming flag');
                _resetStreamState();
            }
            // Only follow the FIRST stream on our branch — parallel siblings stream silently
            // The exact run subscription decides ownership. The visible leaf
            // is deliberately irrelevant: rendering may lag the atomic
            // message+launch response, and browser focus must never decide
            // which in-flight turn belongs here.
            const shouldFollow = exactRunOwned && (!State.isStreaming || pendingMatch);
            console.log('[WS] stream_start parent_id=', parentId, 'gen_id=', data.gen_id, 'follow=', shouldFollow);

            if (shouldFollow) {
                State._streamIsOurBranch = true;
                State._followingGenId = data.gen_id ?? null;
                if (data.run_id != null) _subscribedRunIds.add(String(data.run_id));
                else if (data.gen_id != null) _subscribedRunIds.add(`gen:${data.gen_id}`);
                State._followingDraftMsgId = data.draft_msg_id ?? null;
                const currentLeaf = State.loomCursor?.leaf_message_id;
                // The atomic message+launch endpoint starts the provider before
                // returning its user row. An exact subscribed stream_start can
                // therefore beat the POST response that advances this page
                // from the empty/root cursor to `parentId`. `pendingMatch` is
                // run ownership evidence for this window, so it is safe to
                // follow that draft; visible focus or conversation-global state
                // is never used as authority.
                const matchesCurrentBranch = pendingMatch
                    || parentId == null
                    || _sameMessageId(currentLeaf, parentId);
                if (data.draft_msg_id != null && matchesCurrentBranch) {
                    rememberConversationLeaf(State.currentConvId, data.draft_msg_id);
                    State._cursorSyncPromise = selectServerLoomCursor(
                        State.currentConvId, data.draft_msg_id
                    ).catch(err => {
                        console.warn('[Loom] Could not follow the durable draft cursor:', err);
                        return null;
                    });
                }
                State.isStreaming = true;
                State._parallelCount = (State._parallelCount || 0) + 1;
                _streamTokenCount = 0;
                if (!pendingMatch || !_streamStartTime) _streamStartTime = Date.now();
                _streamBuffer = '';
                _streamFlushTimer = null;
                hideRetryBar();
                hidePlanBar();
                const draft = _ensureLiveStreamDraft(data);
                if (pendingMatch && streamingDiv?.isConnected) {
                    if (data.draft_msg_id != null) streamingDiv.dataset.msgId = data.draft_msg_id;
                    _updateStreamingModelAttestation(draft?.model_attestation);
                } else {
                    appendStreamingMessage(
                        data.cc_model || data.local_model,
                        draft?.model_attestation,
                        data.draft_msg_id,
                        draft?.session_continuity,
                    );
                }
                _pendingLaunch = null;
                showGenStatus(data.status_text || 'Waiting for first token…');
            } else if (exactRunOwned) {
                // Parallel sibling — count it but don't render
                State._parallelCount = (State._parallelCount || 0) + 1;
            } else if (!State.isStreaming) {
                State._streamIsOurBranch = false;
            }
            // Refresh tree to show ghost/draft nodes for all parallel generations
            refreshTree();
            break;
        }

        case 'thinking_start':
            if (!_isOurBranch(data)) break;
            // Only show for CC/Local modes (Weave already has "Looming..." footer)
            if (State.currentConv && State.currentConv.mode !== 'weave') {
                showThinkingIndicator();
            }
            break;

        case 'thinking_end':
            if (!_isOurBranch(data)) break;
            hideThinkingIndicator();
            _streamTokenCount = 0;
            break;

        case 'stream_chunk':
            if (!State._streamIsOurBranch) break;
            hideGenStatus();
            _streamTokenCount++;
            _lastChunkAt = Date.now();
            _appendLiveDraftText(data.content);
            appendStreamChunk(data.content);
            break;

        case 'tool_start':
            if (!State._streamIsOurBranch) break;
            hideGenStatus();
            _startLiveDraftTool(data);
            appendToolBlock(data.name, data.tool_id, data.ooda);
            break;

        case 'tool_input_chunk':
            if (!State._streamIsOurBranch) break;
            _removeStreamWaiting();
            {
                const tool = _liveDraftTool(data.tool_id);
                if (tool) tool.input = (tool.input || '') + (data.content || '');
            }
            appendToolInput(data.content, data.tool_id);
            break;

        case 'tool_result':
            if (!State._streamIsOurBranch) break;
            _removeStreamWaiting();
            {
                const tool = _liveDraftTool(data.tool_id);
                if (tool) {
                    tool.result = data.content || '';
                    if (data.image_url) tool.image_url = data.image_url;
                }
            }
            finalizeToolBlock(data.content, data.tool_id, data.image_url, data.is_error);
            break;

        case 'thinking_chunk':
            if (!State._streamIsOurBranch) break;
            if (!data.content) break;
            // Non-empty thinking is provider output: the model has connected,
            // even when ordinary response text has not started yet.
            _removeStreamWaiting();
            _appendLiveDraftThinking(data.content);
            appendThinkingChunk(data.content);
            break;

        case 'usage': {
            if (!State._streamIsOurBranch) break;
            const draft = _ensureLiveStreamDraft(data);
            if (draft) {
                draft.turn_input_tokens = data.input_tokens || draft.turn_input_tokens || 0;
                draft.turn_output_tokens = data.output_tokens || draft.turn_output_tokens || 0;
            }
            if (typeof scheduleWorkspaceRender === 'function') scheduleWorkspaceRender();
            if (!streamingDiv) break;
            const tokEl = streamingDiv.querySelector('.gen-token-info');
            if (tokEl) {
                tokEl.textContent = (data.input_tokens ? 'in ' + _fmtTok(data.input_tokens) + ' ' : '') + 'out ' + _fmtTok(data.output_tokens) + ' - ';
                tokEl.dataset.hasUsage = '1';  // stop timer from overwriting with chunk count
            }
            // If output tokens registered, work is happening — drop the
            // pre-first-token indicator even if no chunks/tools fired yet.
            if ((data.output_tokens || 0) > 0) _removeStreamWaiting();
            // Compaction banner removed — branch fork is the visual cue now
            break;
        }

        case 'rate_limit_info': {
            const rd = data.data || {};
            let delayStr = '';
            if (rd.retry_after_seconds) delayStr = ` (retry in ${rd.retry_after_seconds}s)`;
            else if (rd.retry_delay_ms) delayStr = ` (retry in ${Math.round(rd.retry_delay_ms / 1000)}s)`;
            else if (rd.resets_at) {
                const diff = Math.round((new Date(rd.resets_at) - new Date()) / 1000);
                if (diff > 0) delayStr = ` (retry in ${diff}s)`;
            }
            const note = `Rate limit hit — waiting on API${delayStr}`;
            showToast(note, 'error');
            if (State._streamIsOurBranch && streamingDiv) {
                const waitEl = streamingDiv.querySelector('.generating-placeholder');
                if (waitEl) waitEl.innerHTML = '<span class="thinking-dots"></span> ' + escapeHtml(note);
            }
            break;
        }

        case 'ask_user_question':
            renderAskUserQuestion(data.questions, data.tool_id);
            break;

        case 'plan_ready':
            // This is informational. The scoped permission_request owns plan
            // approval; sending a new chat turn cannot resolve that request.
            if (document.hidden && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
                new Notification('EXO — Plan Ready', {
                    body: 'Plan awaiting review' + (data.plan_file ? ': ' + data.plan_file : ''),
                    icon: '/static/exoloom/icon.png',
                });
            }
            break;

        case 'permission_request': {
            if (_resolvedPermissionNoticeIds.has(data.request_id)) break;
            const permissionIsVisible = _permissionBelongsToVisibleBranch(data);
            // Permission request landing means tool work is happening — drop the
            // "still waiting for first token" indicator.
            if (permissionIsVisible) _removeStreamWaiting();
            // Always add to notification bell (works from any conversation)
            addPermissionNotification(data);
            // Also render inline if we're viewing the right conversation.
            // If a snapshot reconstruction is in flight, queue the prompt —
            // rendering it now would attach it to a streamingDiv that's about
            // to be destroyed by _reconstructFromSnapshot's remove+re-append.
            if (permissionIsVisible && (!data.conv_id || _sameConvId(data.conv_id, State.currentConvId))) {
                setBrowserTabState('permission');
                if (State._reconstructing) {
                    (State._pendingPermPrompts = State._pendingPermPrompts || []).push(data);
                } else {
                    showPermissionPrompt(data);
                }
            }
            // Push notification if tab is hidden
            if (document.hidden && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
                const n = new Notification('EXO — Permission Request', {
                    body: `${data.tool_name}: ${(data.input_summary || '').substring(0, 100)}`,
                    icon: '/static/exoloom/icon.png',
                    tag: 'perm-' + data.request_id,
                    requireInteraction: true,
                });
                n.onclick = () => { window.focus(); n.close(); };
            }
            break;
        }

        case 'branch_content_changed':
            // Metadata-only notification. Refetch only when this page already
            // renders the changed message; never navigate to another branch.
            if ((State.messages || []).some(m => _sameMessageId(m.id, data.message_id))) {
                loadMessages(State.currentConvId);
            }
            break;

        case 'permission_resolved':
            resolvePermissionPrompt(data.request_id, data.allowed, data.feedback || '', data.decision_source);
            resolvePermissionNotification(data.request_id, data.allowed, data.feedback || '', data.decision_source);
            if (State.isStreaming) setBrowserTabState('looming');
            break;

        case 'cc_debug_event':
            console.log('[CC debug]', data.event_type, data.data);
            break;

        case 'branch_landed': {
            // Global notification — a generation completed somewhere (maybe another conversation)
            const isCurrentConv = _sameConvId(data.conv_id, State.currentConvId);
            const landedMessageIsVisible = isCurrentConv
                && State.currentView === 'chat'
                && !document.hidden
                && (State.messages || []).some(message =>
                    _sameMessageId(message.id, data.message_id)
                );
            if (!landedMessageIsVisible) {
                _addBranchActivityNotification({
                    type: 'branch',
                    branchActivity: true,
                    id: data.message_id,
                    convId: data.conv_id,
                    convTitle: data.conv_title || 'Conversation',
                    parentId: null,
                    preview: (data.preview || 'Response complete').slice(0, 120),
                    time: new Date(),
                });
            } else {
                markBranchActivitySeen(data.conv_id, [data.message_id]);
            }
            // Other windows do not receive this generation's private token
            // stream. Refresh their tree from this content-free completion
            // notice without changing the branch each window is viewing.
            if (isCurrentConv) {
                refreshTree();
                // Reconcile this page's cursor, never navigate to the notice's leaf.
                if (!State.isStreaming && !State._terminalSettlementPromise) {
                    loadMessages(State.currentConvId, { preserveView: true });
                }
            }
            if (isCurrentConv && (State.messages || []).some(message =>
                _sameMessageId(message.id, data.message_id)
            )) {
                setBrowserTabState('done');
            }
            refreshParallelBranchActivity();
            // Browser push if tab hidden
            if (document.hidden && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
                new Notification('EX umbra Omnia', {
                    body: `${data.conv_title || 'Conversation'} — response complete`,
                    icon: '/static/exoloom/icon.png',
                });
            }
            break;
        }

        case 'workspace_change_report': {
            const changed = Number(data.changed_count || 0);
            const removed = Number(data.removed_lines || 0);
            const deleted = Number(data.deleted_count || 0);
            const warningText = (data.warnings || []).join('; ');
            const summary = `${changed} workspace file${changed === 1 ? '' : 's'} changed`
                + (deleted ? `, ${deleted} deleted` : '')
                + (removed ? `, ${removed} lines removed` : '');
            _notifications.unshift({
                type: 'workspace',
                convId: data.conv_id ?? State.currentConvId,
                summary,
                warningText,
                manifestPath: data.manifest_path || '',
                changedFiles: data.changed_files || [],
                time: new Date(),
            });
            _renderNotifBell();
            showToast(warningText ? `Workspace warning: ${warningText}` : summary, warningText ? 'error' : 'warning');
            break;
        }

        case 'plan_landed': {
            const isCurrentConv = _sameConvId(data.conv_id, State.currentConvId);
            const isWatching = isCurrentConv && State.currentView === 'chat' && !document.hidden;
            if (!isWatching) {
                _notifications.push({
                    type: 'branch',
                    id: Date.now(),
                    convId: data.conv_id,
                    convTitle: data.conv_title || 'Conversation',
                    parentId: null,
                    preview: 'Plan ready' + (data.plan_file ? ' — ' + data.plan_file : ''),
                    time: new Date(),
                });
                _renderNotifBell();
            }
            if (document.hidden && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
                new Notification('EXO — Plan Ready', {
                    body: `${data.conv_title || 'Conversation'} — plan awaiting review`,
                    icon: '/static/exoloom/icon.png',
                });
            }
            break;
        }

        case 'canvas_updated':
            refreshCanvasIframe();
            break;

        case 'state_update':
            // OODA harness updated branch state — refresh with branch-aware data
            if (State.currentConvId) {
                const leaf = State.messages?.filter(m => m.role !== 'system').slice(-1)[0];
                const stateUrl = leaf
                    ? `/api/conversations/${State.currentConvId}/branch-state/${leaf.id}`
                    : `/api/conversations/${State.currentConvId}/state`;
                API.get(stateUrl).then(cards => {
                    State.stateCards = cards;
                    if (typeof renderStateCards === 'function') renderStateCards();
                });
            }
            break;

        case 'queue_state': {
            const changed = _applyGenerationQueueState(data.items || []);
            if (changed && !State.isStreaming && !State._reconstructing) {
                renderMessages();
            }
            break;
        }

        case 'queue_changed':
            _refreshQueuedGeneration(State.currentConvId).then(changed => {
                if (changed && !State.isStreaming && !State._reconstructing) {
                    loadMessages(State.currentConvId);
                }
            });
            break;

        case 'queue_started': {
            // Queue lifecycle is durable shared state. Every tab viewing this
            // exact thread may clear the queued styling, but only the authoring
            // tab follows the run or changes its status text.
            const isOrigin = !data.client_instance_id
                || String(data.client_instance_id) === String(_loomClientInstanceId);
            _generationQueueItems = _generationQueueItems.filter(
                item => !_sameMessageId(item.user_msg_id, data.user_msg_id)
            );
            const queuedId = data.user_msg_id;
            const queuedMessage = (State.messages || []).find(
                message => _sameMessageId(message.id, queuedId)
            );
            if (queuedMessage) {
                queuedMessage.queued = false;
                delete queuedMessage.queue_status;
                delete queuedMessage.queue_after_draft_id;
            }
            if (_queuedGeneration && _sameMessageId(_queuedGeneration.id, queuedId)) {
                _queuedGeneration = null;
            }
            const queuedEl = document.querySelector(`.message[data-msg-id="${queuedId}"]`);
            if (queuedEl) {
                _syncQueuedMessagePresentation(queuedEl, { queued: false });
            }
            if (isOrigin) {
                showGenStatus('Sending queued message...');
            }
            break;
        }

        case 'stream_end': {
            // Is this the stream we're following, or a parallel sibling?
            const isFollowed = State._followingGenId != null
                ? data.gen_id === State._followingGenId
                : State._streamIsOurBranch;
            // Decrement parallel counter
            if (State._parallelCount > 0) State._parallelCount--;
            const allDone = !State._parallelCount;

            if (!isFollowed) {
                // Parallel sibling finished — refresh tree (bell handled by branch_landed)
                refreshTree();
                // If the completed message is on our current branch (e.g. user navigated
                // to a draft that just finished), reload messages to show the content
                if (data.message && data.message.id) {
                    const viewedIds = new Set(State.messages.map(m => m.id));
                    if (viewedIds.has(data.message.id)) {
                        loadMessages(State.currentConvId);
                    }
                }
                if (allDone) {
                    State._streamIsOurBranch = undefined;
                    if (State._followingGenId != null) _subscribedRunIds.delete(`gen:${State._followingGenId}`);
                    State._followingGenId = null;
                }
                break;
            }
            // Our followed stream ended
            const queuedAfterThisDraft = _queuedGeneration
                && (_sameMessageId(_queuedGeneration.parent_id, data.message?.id)
                    || _sameMessageId(_queuedGeneration.queue_after_draft_id, data.draft_msg_id));
            if (data.message && data.message.id && !queuedAfterThisDraft
                && (_sameMessageId(State.loomCursor?.leaf_message_id, data.message.id)
                    || _sameMessageId(State.loomCursor?.leaf_message_id, data.message.parent_id))
                && (!data.message.loom_thread_id || !State.loomCursor?.thread_id
                    || String(data.message.loom_thread_id) === String(State.loomCursor.thread_id))) {
                rememberConversationLeaf(State.currentConvId, data.message.id);
            }
            hideGenStatus();
            // Clear ghost node before tree refresh so it doesn't persist
            // Tree refreshes on stream_end/cancel/error to replace draft with final node
            // Bell + browser push now handled by branch_landed (global broadcast)
            if (streamingDiv) {
                finalizeStreamingMessage(data.message, data.cost);
            } else {
                loadMessages(State.currentConvId);
            }
            setBrowserTabState('done');
            refreshTree();
            if (queuedAfterThisDraft) {
                _clearTerminalStreamState({ clearParallel: allDone });
            } else {
                _settleTerminalStream(data, { clearParallel: allDone });
            }
            break;
        }

        case 'cancelled':
            if (!_isOurBranch(data)) break;
            removeStreamingMessage();
            hideGenStatus();
            setBrowserTabState('idle');
            showRetryBar('Generation cancelled');
            refreshTree();
            // Cursor authority must settle before isStreaming becomes false;
            // otherwise a follow-up can attach to the prior user row.
            _settleTerminalStream(data, { clearParallel: true, reload: true });
            break;

        case 'interrupted':
            if (!_isOurBranch(data)) break;
            if (streamingDiv && data.message) {
                finalizeStreamingMessage(data.message);
            } else {
                removeStreamingMessage();
                loadMessages(State.currentConvId);
            }
            hideGenStatus();
            setBrowserTabState('idle');
            showToast('Turn interrupted; partial context was preserved');
            refreshTree();
            _settleTerminalStream(data, { clearParallel: true });
            break;

        case 'warning':
            // Server-side non-fatal warning (e.g. CC silently downgraded model).
            // Show as a sticky toast so the user actually reads it.
            if (data.text) showToast(data.text, 8000);
            break;

        case 'error':
            if (!_isOurBranch(data)) {
                hideGenStatus();
                refreshTree();
                if (data.message_id) {
                    const viewedIds = new Set(State.messages.map(m => m.id));
                    if (viewedIds.has(data.message_id)) loadMessages(State.currentConvId);
                }
                break;
            }
            // Provider adapters may persist a structured failed turn with its
            // partial text/tool transcript. Render that authoritative row
            // instead of appending another transient interruption string.
            if (data.message) {
                if (streamingDiv) finalizeStreamingMessage(data.message);
                else loadMessages(State.currentConvId);
            } else {
                const preservedPartial = markStreamingMessageInterrupted(data.error || 'Generation error');
                if (!preservedPartial) removeStreamingMessage();
            }
            _clearTerminalStreamState({ clearParallel: true });
            hideGenStatus();
            setBrowserTabState('error');
            if (data.error && data.error.includes('another branch')) {
                showToast(data.error, 'error');
            } else {
                const retryPrefix = data.retryable ? 'Retryable provider failure: ' : '';
                showRetryBar(retryPrefix + (data.error || 'Generation error'));
            }
            break;

        case 'generation_active': {
            // Reconnected while a generation is still running — use snapshot to rebuild UI
            hideRetryBar();
            hidePlanBar();
            const snapshots = (data.snapshots || []).filter(s => {
                const runId = s.run_id || (s.gen_id != null ? `gen:${s.gen_id}` : null);
                const key = _terminalRunKey(State.currentConvId, runId);
                const landed = _recentTerminalRuns.has(key)
                    && _sameMessageId(_recentTerminalRuns.get(key), s.draft_msg_id);
                return LoomCoordination.viewStep(landed, true, false)[1];
            });
            if (data.snapshots?.length && !snapshots.length) {
                if (State.ws) State.ws._needsSync = false;
                break; // All supplied snapshots were superseded by terminal evidence.
            }
            const followedRun = State._followingGenId != null
                ? `gen:${State._followingGenId}` : null;
            // Reconnect ownership comes from the server's exact run
            // subscription; branch visibility is not an ownership signal.
            const rememberedSnap = snapshots.find(s => {
                const runId = s.run_id || (s.gen_id != null ? `gen:${s.gen_id}` : null);
                return followedRun && runId === followedRun;
            });
            // Progressive content appears only on snapshots the server routed
            // to this page's exact durable thread cursor. It is therefore a
            // safe reconnect authority even after a full JS reload lost the
            // in-memory followed run id.
            const snap = followedRun ? rememberedSnap : snapshots.find(s => (
                Object.prototype.hasOwnProperty.call(s, 'full_text')
                || Object.prototype.hasOwnProperty.call(s, 'content_blocks')
            ));
            const snapVisible = snap && _streamParentMatchesVisibleBranch(snap.parent_id, snap.draft_msg_id);
            if (snap && snapVisible && snap.draft_msg_id == null && snap.status_text) {
                const runId = snap.run_id || (snap.gen_id != null ? `gen:${snap.gen_id}` : null);
                if (runId) {
                    _subscribedRunIds.add(String(runId));
                    if (State.ws?.readyState === WebSocket.OPEN) {
                        State.ws.send(JSON.stringify({ action: 'subscribe_run', run_id: runId }));
                    }
                }
                State.isStreaming = true;
                State._streamIsOurBranch = true;
                State._followingGenId = snap.gen_id ?? null;
                State._followingDraftMsgId = null;
                _pendingLaunch = {
                    genId: snap.gen_id ?? null,
                    runId,
                    parentId: snap.parent_id ?? null,
                    threadId: snap.loom_thread_id ?? null,
                };
                _streamStartTime = (snap.started_at || 0) * 1000 || Date.now();
                if (!streamingDiv?.isConnected) appendStreamingMessage(snap.cc_model);
                showGenStatus(snap.status_text);
                if (State.ws) State.ws._needsSync = false;
                break;
            }
            if (snap && snapVisible) {
                if (snap.run_id) {
                    _subscribedRunIds.add(String(snap.run_id));
                    if (State.ws?.readyState === WebSocket.OPEN) {
                        State.ws.send(JSON.stringify({ action: 'subscribe_run', run_id: snap.run_id }));
                    }
                }
                State.isStreaming = true;
                const visibleDraft = State.messages.some(m => _sameMessageId(m.id, snap.draft_msg_id));
                State._streamIsOurBranch = visibleDraft || _streamParentMatchesVisibleBranch(snap.parent_id, snap.draft_msg_id);
                State._followingGenId = snap.gen_id ?? null;
                State._followingDraftMsgId = snap.draft_msg_id ?? null;
                _syncLiveDraftFromSnapshot(snap);
                _streamStartTime = (snap.started_at || 0) * 1000 || _streamStartTime || Date.now();
                _streamTokenCount = 0;
                if (!State._reconstructing) {
                    State._reconstructing = true;
                    // Watchdog: if neither .then nor .catch fires within 15s
                    // (hung loadMessages, server unresponsive, synchronous
                    // throw before promise creation), force-clear so the
                    // appendStreamChunk/_flushStreamBuffer gates don't drop
                    // every chunk silently forever. 15s is comfortably past
                    // any reasonable localhost loadMessages roundtrip.
                    if (State._reconstructWatchdog) clearTimeout(State._reconstructWatchdog);
                    State._reconstructWatchdog = setTimeout(() => {
                        if (State._reconstructing) {
                            console.warn('[Loom] _reconstructing watchdog fired — force-clearing after 15s of inactivity');
                            State._reconstructing = false;
                        }
                        State._reconstructWatchdog = null;
                    }, 15000);
                    const activeWs = State.ws;
                    const hadAttachedStream = !!(streamingDiv && streamingDiv.isConnected);
                    const syncPromise = hadAttachedStream
                        ? _syncMessagesForStreaming(State.currentConvId)
                        : loadMessages(State.currentConvId);
                    let discardQueuedEvents = false;
                    // Keep _reconstructing true through message sync and any
                    // snapshot rebuild. Events received in this window are
                    // queued until the DOM is stable, then replayed.
                    syncPromise.then(() => {
                        if (State.ws !== activeWs) {
                            discardQueuedEvents = true;
                            return;
                        }
                        // Reconstruct from snapshot. Do not treat a non-empty
                        // draft row as terminal here: several providers persist
                        // partial text to the draft while the turn is still
                        // active, specifically so reconnect/refresh can recover
                        // streamed content.
                        //
                        // Terminal state is handled only by explicit stream_end
                        // or generation_idle events.
                        //
                        // This covers the race
                        // where stream_end fired during loadMessages but the
                        // assistant row wasn't yet committed to the DB. Don't
                        // gate on State.isStreaming: a fast/rate-limited turn
                        // can finish before we get here, and we still need to
                        // render the partial output until the final lands.
                        const visibleDraft = State.messages.some(m => _sameMessageId(m.id, snap.draft_msg_id));
                        const stillOurs = visibleDraft || _streamParentMatchesVisibleBranch(snap.parent_id, snap.draft_msg_id);
                        State._streamIsOurBranch = stillOurs;
                        if (stillOurs && _recentTerminalRuns.has(_terminalRunKey(
                                State.currentConvId,
                                snap.run_id || (snap.gen_id != null ? `gen:${snap.gen_id}` : null)))) {
                            const snapshotRunId = snap.run_id || (snap.gen_id != null ? `gen:${snap.gen_id}` : null);
                            const terminalQueued = _queuedStreamEventsDuringReconstruct.some(event =>
                                _canonicalClientRunId(event) === snapshotRunId
                                && ['stream_end', 'generation_terminal', 'cancelled', 'interrupted', 'error'].includes(event.type)
                            );
                            // Receipt records terminal evidence before it is
                            // painted. Retain the owner and queued terminal so
                            // finally can finalize its message/tab state once;
                            // clearing here would silently discard completion.
                            if (terminalQueued) return;
                            // This page already processed the run's terminal
                            // evidence while the message sync was in flight.
                            // Its snapshot must not resurrect a live streaming
                            // placeholder for a finished turn.
                            _clearTerminalStreamState({ clearParallel: true });
                            removeStreamingMessage();
                            return;
                        }
                        if (stillOurs && _snapshotShouldReplaceLiveStream(snap)) {
                            _reconstructFromSnapshot(snap);
                        }
                    }).catch((err) => {
                        console.warn('[WS] generation_active sync failed:', err);
                    }).finally(() => {
                        if (State._reconstructWatchdog) {
                            clearTimeout(State._reconstructWatchdog);
                            State._reconstructWatchdog = null;
                        }
                        State._reconstructing = false;
                        if (discardQueuedEvents || State.ws !== activeWs) {
                            _queuedStreamEventsDuringReconstruct = [];
                        } else {
                            _drainQueuedStreamEvents();
                        }
                        _drainPendingPermPrompts();
                    });
                }
            } else if (followedRun && State._followingDraftMsgId != null) {
                // An absent snapshot is not authority to attach a sibling or
                // clear the current card. Ask for its persisted terminal row.
                if (State.ws?.readyState === WebSocket.OPEN) {
                    State.ws.send(JSON.stringify({action: 'ping', ..._followedGenerationProbe()}));
                }
            } else if (snapshots.length > 0) {
                // Other branches are active, but this tab is not following
                // them. Keep the tree live without stealing the chat stream.
                // The server's complete active-run list supersedes a stale
                // followed run even when it failed before creating a draft.
                // Requiring a saved terminal assistant strands that launch
                // shell forever while an unrelated branch remains active.
                if (followedRun) _recoverIdleMessages();
                else _clearTerminalStreamState();
                refreshTree();
            } else {
                // No usable snapshot. Some providers may not maintain live
                // snapshots, so keep the UI receptive and reconstruct from
                // the persisted draft row if one is visible.
                State.isStreaming = true;
                State._streamIsOurBranch = true;
                State._followingGenId = null;
                if (!State._reconstructing) {
                    State._reconstructing = true;
                    const activeWs = State.ws;
                    loadMessages(State.currentConvId).then(() => {
                        if (State.ws !== activeWs) {
                            return;
                        }
                        if (!streamingDiv || !streamingDiv.isConnected) {
                            appendStreamingMessage();
                        }
                    }).catch((err) => {
                        console.warn('[WS] generation_active no-snapshot sync failed:', err);
                    }).finally(() => {
                        State._reconstructing = false;
                        if (State.ws !== activeWs) {
                            _queuedStreamEventsDuringReconstruct = [];
                        } else {
                            _drainQueuedStreamEvents();
                        }
                        _drainPendingPermPrompts();
                    });
                }
            }
            if (State.ws) State.ws._needsSync = false;
            break;
        }

        case 'generation_idle':
            // Server confirms no generation running — reset any stuck streaming state.
            // If we were mid-reconstruction, force a reload: a fast turn can finish
            // (and commit its final assistant message) in the gap between
            // generation_active and generation_idle, leaving the new row unrendered.
            const wasReconstructing = State._reconstructing;
            State._reconstructing = false;
            // If a generate is stashed and about to fire, skip the reload —
            // an in-flight renderMessages() races with the upcoming stream_start
            // and wipes the streamingDiv mid-stream. The new stream will paint
            // the response itself; no reload needed.
            if (_pendingGenerate && [..._pendingGenerates.values()].some(_launchOperationIsVisible)) {
                if (State.ws) State.ws._needsSync = false;
                break;
            }
            // A terminal event has rendered, but its exact page cursor is
            // still settling. It owns isStreaming until that authority lands.
            if (State._terminalSettlementPromise) {
                if (State.ws) State.ws._needsSync = false;
                break;
            }
            if (State.isStreaming || (State.ws && State.ws._needsSync) || wasReconstructing) {
                _recoverIdleMessages();
            }
            if (State.ws) State.ws._needsSync = false;
            if (_browserTabState === 'looming' || _browserTabState === 'permission') {
                setBrowserTabState('idle');
            }
            break;

        case 'pong':
            // Heartbeat ack — no UI action needed; onmessage already updated _lastActivity.
            break;
    }
}

function updateContextInfo(data) {
    let banner = document.getElementById('compactify-banner');
    if (data.was_compactified) {
        if (!banner) {
            banner = document.createElement('div');
            banner.id = 'compactify-banner';
            // Place above input area so it's visible without scrolling
            const inputArea = document.getElementById('input-area');
            inputArea.parentNode.insertBefore(banner, inputArea);
        }
        // Build summary line and expanded details per source
        let summaryLine, details = [];
        if (data.pre_tokens != null) {
            // Claude Code compaction
            const trigger = data.trigger === 'manual' ? 'manual' : 'auto';
            summaryLine = `Context compactified (${trigger})`;
            details.push(`Before: ${data.pre_tokens.toLocaleString()} tokens`);
            if (data.post_tokens) {
                details.push(`After: ${data.post_tokens.toLocaleString()} tokens`);
            }
        } else if (data.summarized_count != null) {
            // Local (llama) compaction
            summaryLine = `Context compactified — ${data.summarized_count} of ${data.total_messages || '?'} messages summarized`;
            details.push(`${data.verbatim_count || '?'} messages sent verbatim`);
            if (data.total_tokens) details.push(`Post-compaction context: ${data.total_tokens.toLocaleString()} tokens`);
            if (data.summary_text) {
                details.push(`--- Rolling summary ---\n${data.summary_text}`);
            }
        } else {
            summaryLine = 'Context compactified';
        }

        const detailsHtml = details.length
            ? `<div class="compactify-details">${details.map(d => `<pre>${escapeHtml(d)}</pre>`).join('')}</div>`
            : '';
        // Preserve open/closed state of existing <details> element
        const existingDetails = banner.querySelector('details.compactify-collapse');
        const wasOpen = existingDetails ? existingDetails.open : false;
        banner.innerHTML = `<details class="compactify-collapse"${wasOpen ? ' open' : ''}><summary><span class="compactify-icon">⧈</span> ${escapeHtml(summaryLine)}</summary>${detailsHtml}</details>`;
        banner.classList.remove('hidden');
    } else if (banner) {
        banner.classList.add('hidden');
    }
}

function _snapshotTextLength(snap) {
    let len = 0;
    const blocks = snap?.content_blocks || [];
    for (const block of blocks) {
        if (block.type === 'text') len += (block.text || '').length;
        else if (block.type === 'thinking') len += (block.text || '').length;
    }
    return Math.max(len, (snap?.full_text || '').length);
}

function _snapshotNonTextBlockCount(snap) {
    const blocks = snap?.content_blocks || [];
    return blocks.filter(block => block.type && block.type !== 'text').length;
}

function _currentStreamingContentStats() {
    if (!streamingDiv || !streamingDiv.isConnected) {
        return { textLength: 0, blockCount: 0 };
    }
    _flushStreamBufferNow();
    const contentEl = streamingDiv.querySelector('.message-content');
    if (!contentEl) return { textLength: 0, blockCount: 0 };
    const clone = contentEl.cloneNode(true);
    clone.querySelectorAll('.stream-waiting, .stream-waiting-anim, .typing-cursor')
        .forEach(el => el.remove());
    return {
        textLength: clone.textContent.length,
        blockCount: clone.querySelectorAll('.tool-block, .cc-thinking, .ask-question-block').length,
    };
}

function _snapshotShouldReplaceLiveStream(snap) {
    if (!streamingDiv || !streamingDiv.isConnected) return true;
    const current = _currentStreamingContentStats();
    return _snapshotTextLength(snap) > current.textLength
        || _snapshotNonTextBlockCount(snap) > current.blockCount;
}

/**
 * Reconstruct streaming UI from a server-side generation snapshot.
 * Called on WS reconnect when a generation is mid-flight.
 */
function _reconstructFromSnapshot(snap) {
    _syncLiveDraftFromSnapshot(snap);
    removeStreamingMessage();
    appendStreamingMessage(snap.cc_model, snap.model_attestation, snap.draft_msg_id);
    if (!streamingDiv) return;

    const contentEl = streamingDiv.querySelector('.message-content');
    const blocks = snap.content_blocks || [];

    if (blocks.length > 0) {
        // Walk blocks in chronological order: text spans, tool blocks, and
        // thinking blocks land in the same sequence the model produced them.
        // The trailing text span carries the streaming cursor.
        let pendingText = '';
        const flushText = (withCursor) => {
            if (!pendingText) return;
            const textSpan = document.createElement('span');
            textSpan.className = 'streaming-text';
            textSpan.dataset.rawContent = pendingText;
            textSpan.innerHTML = formatContent(pendingText) + (withCursor ? '<span class="typing-cursor"></span>' : '');
            contentEl.appendChild(textSpan);
            pendingText = '';
        };
        for (let i = 0; i < blocks.length; i++) {
            const block = blocks[i];
            if (block.type === 'text') {
                pendingText += block.text || '';
                continue;
            }
            // Flush any accumulated text before a non-text block.
            flushText(false);
            if (block.type === 'tool_use') {
                if (block.result) {
                    const toolDiv = document.createElement('div');
                    const imageHtml = toolImageFigureHtml(block.image_url);
                    const open = imageHtml && block.name !== 'Read';
                    toolDiv.className = 'tool-block' + (open ? ' expanded' : '');
                    const inputPreview = (block.input || '').substring(0, 3000);
                    const resultPreview = (block.result || '').substring(0, 2000);
                    toolDiv.innerHTML =
                        '<div class="tool-block-header" data-tool-toggle>' +
                        '<span class="tool-toggle">' + (open ? '&#9662;' : '&#9656;') + '</span> ' +
                        '<span class="tool-name">' + escapeHtml(block.name || 'Tool') + '</span>' +
                        '</div>' +
                        '<div class="tool-block-body">' +
                        (inputPreview ? '<pre class="tool-block-input">' + escapeHtml(inputPreview) + '</pre>' : '') +
                        '<div class="tool-block-result"><pre>' + escapeHtml(resultPreview) + '</pre></div>' +
                        imageHtml +
                        '</div>';
                    contentEl.appendChild(toolDiv);
                } else {
                    appendToolBlock(block.name, block.tool_id, false);
                    if (block.input) {
                        appendToolInput(block.input, block.tool_id);
                    }
                }
            } else if (block.type === 'thinking') {
                const thinkDiv = document.createElement('div');
                thinkDiv.className = 'cc-thinking';
                thinkDiv.innerHTML =
                    '<div class="cc-thinking-toggle">Thinking...</div>' +
                    '<div class="cc-thinking-content" style="display:none;">' + escapeHtml(block.text || '') + '</div>';
                contentEl.appendChild(thinkDiv);
            }
        }
        // Trailing text gets the live cursor since the stream may continue.
        flushText(true);
    } else if (snap.full_text) {
        // No structured blocks — just raw text (Weave mode)
        const textSpan = document.createElement('span');
        textSpan.className = 'streaming-text';
        textSpan.dataset.rawContent = snap.full_text;
        textSpan.innerHTML = formatContent(snap.full_text) + '<span class="typing-cursor"></span>';
        contentEl.appendChild(textSpan);
    }

    // Reset stream buffer so new chunks append cleanly
    _streamBuffer = '';
    _streamFlushTimer = null;

    // Re-render pending permission prompts that were lost when we rebuilt the
    // streaming div — but ONLY for the current conversation. Without this
    // filter, a page reload that replays every globally-pending perm inlines
    // prompts from other loom conversations into this branch's stream.
    for (const n of _notifications) {
        if (n.type !== 'permission' || n.resolved) continue;
        if (n.convId && !_sameConvId(n.convId, State.currentConvId)) continue;
        showPermissionPrompt({
            request_id: n.requestId,
            conv_id: n.convId,
            gen_id: n.genId,
            parent_id: n.parentId,
            draft_msg_id: n.draftMsgId,
            tool_name: n.toolName,
            approval_method: n.approvalMethod,
            input_summary: n.inputSummary,
            risk_level: n.riskLevel,
            risk_reason: n.riskReason,
            supports_allow_all: n.supportsAllowAll,
            rule_preview: n.rulePreview,
        });
    }

    showGenStatus('Reconnected — streaming in progress', true);
    scrollToBottom();
    console.log('[WS] Reconstructed streaming UI from snapshot:', blocks.length, 'blocks,', (snap.full_text || '').length, 'chars');
}

// ── Skills / Slash Commands ──

let _cachedSkills = null;  // cached from /api/skills
let _cachedSkillsKey = null;

async function _loadSkills() {
    const modelKey = State.currentConv?.cc_model || State.currentConv?.local_model || '';
    const cacheKey = `${State.currentConvId || ''}:${State.currentConv?.mode || ''}:${modelKey}`;
    if (_cachedSkills && _cachedSkillsKey === cacheKey) return _cachedSkills;
    try {
        const convParam = State.currentConvId ? `?conv_id=${State.currentConvId}` : '';
        _cachedSkills = await API.get(`/api/skills${convParam}`);
        _cachedSkillsKey = cacheKey;
    } catch {
        _cachedSkills = [];
        _cachedSkillsKey = cacheKey;
    }
    return _cachedSkills;
}

function _getModeLabel(conv) {
    if (!conv) return 'system';
    const mode = conv.mode || 'weave';
    if (mode.toLowerCase() === 'weave') return mode;
    const agentKind = _agentKindForModel(conv.cc_model || conv.local_model || '', conv);
    if (agentKind) return agentKind;
    return mode;
}


/** Candidates for the "/" autocomplete. Retired sessions do not advertise tools. */
async function _slashCandidates() {
    if (_retiredHarnessMode(State.currentConv)) return [];
    return _loadSkills();
}

function _invalidateSkillsCache() {
    _cachedSkills = null;
    _cachedSkillsKey = null;
}

/**
 * Translate a /slash command into a natural language prompt for CC.
 * Returns null if the input is not a slash command.
 */
function _translateSlashCommand(content, skills) {
    if (!content.startsWith('/')) return null;
    const match = content.match(/^\/(\S+)\s*(.*)?$/);
    if (!match) return null;

    const cmdName = match[1].toLowerCase();
    const args = (match[2] || '').trim();

    const skill = skills.find(s =>
        s.command === `/${cmdName}` || s.name === cmdName
    );

    if (!skill) return null;

    // Meta commands are handled by Loom natively, not sent to CC
    if (skill.mode === 'meta') {
        return { meta: true, skillName: skill.name, args };
    }

    let prompt = skill.prompt_template || `Run the ${skill.name} skill.`;
    prompt = prompt.replace('{args}', args || '');

    // If user provided args and template didn't have {args}, append them
    if (args && !skill.prompt_template?.includes('{args}')) {
        prompt += `\n\nAdditional context: ${args}`;
    }

    return { prompt, skillName: skill.name };
}

function _isCodexConversation() {
    const conv = State.currentConv;
    if (!conv) return false;
    return (conv.mode || '').toLowerCase() === 'codex';
}

function _addChatSystemMessage(html, extraClass = '') {
    const container = document.getElementById('messages');
    if (!container) return;
    const el = document.createElement('div');
    el.className = 'system-message ' + extraClass;
    el.innerHTML = html;
    container.appendChild(el);
    el.scrollIntoView({ behavior: 'smooth' });
}

function _goalPayloadWithBudget(payload) {
    if (!payload.objective) return payload;
    const match = payload.objective.match(/\s+--budget\s+(\d+)\s*$/i);
    if (!match) return payload;
    return {
        ...payload,
        objective: payload.objective.slice(0, match.index).trim(),
        token_budget: Number(match[1]),
    };
}

function _parseGoalCommand(args) {
    const text = (args || '').trim();
    if (!text) return { action: 'get' };

    const parts = text.split(/\s+/);
    const verb = parts[0].toLowerCase();
    const rest = parts.slice(1).join(' ').trim();

    if (['clear', 'reset', 'off'].includes(verb)) return { action: 'clear' };
    if (verb === 'pause') return { action: 'pause' };
    if (verb === 'resume') return { action: 'resume' };
    if (['view', 'show', 'status'].includes(verb)) return { action: 'get' };
    if (['set', 'edit'].includes(verb)) {
        if (!rest) return { error: '/goal needs an objective.' };
        return _goalPayloadWithBudget({ action: 'set', objective: rest });
    }
    return _goalPayloadWithBudget({ action: 'set', objective: text, start: true });
}

function _codexGoalFromState() {
    const conv = State.currentConv;
    if (!conv) return null;
    const objective = (conv.codex_goal_objective || '').trim();
    if (!objective) return null;
    return {
        objective,
        status: conv.codex_goal_status || 'active',
        tokenBudget: conv.codex_goal_token_budget || null,
        tokensUsed: conv.codex_goal_tokens_used || 0,
        timeUsedSeconds: conv.codex_goal_time_used_seconds || 0,
    };
}

function _applyCodexGoalResult(data) {
    if (!State.currentConv || !data) return;
    const goal = data.goal || null;
    if (data.thread_id) State.currentConv.claude_session_id = data.thread_id;
    State.currentConv.codex_goal_objective = goal ? goal.objective : null;
    State.currentConv.codex_goal_status = goal ? (goal.status || 'active') : null;
    State.currentConv.codex_goal_token_budget = goal ? (goal.tokenBudget || null) : null;
    State.currentConv.codex_goal_tokens_used = goal ? (goal.tokensUsed || 0) : 0;
    State.currentConv.codex_goal_time_used_seconds = goal ? (goal.timeUsedSeconds || 0) : 0;
    renderCodexGoalRow();
}

function _formatCodexGoalResult(data) {
    const goal = data && data.goal;
    if (!goal) {
        return data && data.action === 'clear'
            ? 'Codex goal cleared.'
            : 'No Codex goal is set.';
    }
    const status = goal.status || 'active';
    const lines = [`Codex goal (${status})`, '', goal.objective || ''];
    if (goal.tokenBudget) lines.push('', `Token budget: ${goal.tokenBudget}`);
    if (goal.tokensUsed || goal.timeUsedSeconds) {
        lines.push(`Progress: ${goal.tokensUsed || 0} tokens, ${goal.timeUsedSeconds || 0}s`);
    }
    return `<pre style="font-size:0.85em;color:var(--text-muted);white-space:pre-wrap">${escapeHtml(lines.join('\n'))}</pre>`;
}

async function _postCodexGoal(payload, { systemMessage = false, toast = true } = {}) {
    const convId = State.currentConvId;
    if (!convId) throw new Error('No conversation selected');
    if (!_isCodexConversation()) throw new Error('/goal is only available for Codex conversations');
    if (toast) showToast(payload.action === 'get' ? 'Reading Codex goal...' : 'Updating Codex goal...');
    const data = await API.post(`/api/conversations/${convId}/codex-goal`, payload);
    if (State.currentConvId === convId) {
        _applyCodexGoalResult(data);
        if (systemMessage) _addChatSystemMessage(_formatCodexGoalResult(data));
    }
    return data;
}

function _codexGoalProgressText(goal) {
    if (!goal) return '0 tokens, 0s';
    const tokens = Number(goal.tokensUsed || 0);
    const seconds = Number(goal.timeUsedSeconds || 0);
    const budget = goal.tokenBudget ? ` / ${goal.tokenBudget}` : '';
    return `${tokens}${budget} tokens, ${seconds}s`;
}

function _setCodexGoalModalBusy(busy) {
    document.querySelectorAll('#modal-codex-goal button, #modal-codex-goal textarea, #modal-codex-goal input')
        .forEach(el => { el.disabled = !!busy; });
    if (!busy) _populateCodexGoalModal();
}

function _populateCodexGoalModal() {
    const goal = _codexGoalFromState();
    const status = goal ? (goal.status || 'active') : 'none';
    const statusEl = document.getElementById('codex-goal-window-status');
    const objectiveEl = document.getElementById('codex-goal-objective-input');
    const budgetEl = document.getElementById('codex-goal-budget-input');
    const progressEl = document.getElementById('codex-goal-progress');
    const toggleBtn = document.getElementById('btn-codex-goal-modal-toggle');
    const clearBtn = document.getElementById('btn-codex-goal-modal-clear');
    const startBtn = document.getElementById('btn-codex-goal-modal-start');

    if (statusEl) {
        statusEl.dataset.status = status;
        statusEl.textContent = goal ? `Goal ${status}` : 'No goal set';
    }
    if (objectiveEl) objectiveEl.value = goal?.objective || '';
    if (budgetEl) budgetEl.value = goal?.tokenBudget || '';
    if (progressEl) progressEl.textContent = _codexGoalProgressText(goal);
    if (toggleBtn) {
        toggleBtn.textContent = status === 'paused' ? 'Resume' : 'Pause';
        toggleBtn.disabled = !goal;
    }
    if (clearBtn) clearBtn.disabled = !goal;
    if (startBtn) startBtn.disabled = !goal || status === 'paused';
}

async function openCodexGoalWindow({ refresh = false, focus = false } = {}) {
    if (!_isCodexConversation()) {
        showToast('/goal is only available for Codex conversations', 'error');
        return;
    }
    _populateCodexGoalModal();
    const modal = document.getElementById('modal-codex-goal');
    if (modal) modal.classList.remove('hidden');
    if (refresh) {
        try {
            _setCodexGoalModalBusy(true);
            await _postCodexGoal({ action: 'get' }, { systemMessage: false, toast: false });
            _populateCodexGoalModal();
        } catch (err) {
            const msg = err.detail || err.message || 'Codex goal command failed';
            showToast(msg, 'error');
        } finally {
            _setCodexGoalModalBusy(false);
        }
    }
    if (focus) {
        setTimeout(() => document.getElementById('codex-goal-objective-input')?.focus(), 0);
    }
}

function renderCodexGoalRow() {
    const row = document.getElementById('codex-goal-row');
    if (!row) return;
    row.classList.add('hidden');
    const statusEl = document.getElementById('codex-goal-status');
    const objectiveEl = document.getElementById('codex-goal-objective');
    const editBtn = document.getElementById('btn-codex-goal-edit');
    const toggleBtn = document.getElementById('btn-codex-goal-toggle');
    const clearBtn = document.getElementById('btn-codex-goal-clear');
    const isCodex = _isCodexConversation();
    if (!isCodex) return;
    row.classList.toggle('hidden', State.currentView !== 'chat' || !_codexGoalFromState());
    if (typeof syncWorkspaceLayout === 'function') syncWorkspaceLayout();

    const goal = _codexGoalFromState();
    const status = goal ? (goal.status || 'active') : 'none';
    if (statusEl) statusEl.textContent = status;
    if (objectiveEl) {
        objectiveEl.textContent = goal ? goal.objective : 'No goal set';
        objectiveEl.title = goal ? goal.objective : 'No Codex goal set';
    }
    if (editBtn) editBtn.textContent = goal ? 'Edit' : 'Set';
    if (toggleBtn) {
        const paused = status === 'paused';
        toggleBtn.textContent = paused ? 'Resume' : 'Pause';
        toggleBtn.disabled = !goal;
        toggleBtn.title = paused ? 'Resume Codex goal' : 'Pause Codex goal';
    }
    if (clearBtn) clearBtn.disabled = !goal;
    _populateCodexGoalModal();
}

function initCodexGoalControls() {
    const viewBtn = document.getElementById('btn-codex-goal-view');
    const editBtn = document.getElementById('btn-codex-goal-edit');
    const toggleBtn = document.getElementById('btn-codex-goal-toggle');
    const clearBtn = document.getElementById('btn-codex-goal-clear');
    const modalSaveBtn = document.getElementById('btn-codex-goal-modal-save');
    const modalToggleBtn = document.getElementById('btn-codex-goal-modal-toggle');
    const modalClearBtn = document.getElementById('btn-codex-goal-modal-clear');
    const modalStartBtn = document.getElementById('btn-codex-goal-modal-start');
    if (!viewBtn || viewBtn.dataset.codexGoalBound === '1') return;
    viewBtn.dataset.codexGoalBound = '1';

    viewBtn.addEventListener('click', () => openCodexGoalWindow({ refresh: true }));

    editBtn?.addEventListener('click', () => openCodexGoalWindow({ refresh: true, focus: true }));

    modalSaveBtn?.addEventListener('click', async () => {
        const text = (document.getElementById('codex-goal-objective-input')?.value || '').trim();
        if (!text) {
            showToast('/goal needs an objective.', 'error');
            return;
        }
        const budgetRaw = (document.getElementById('codex-goal-budget-input')?.value || '').trim();
        const payload = { action: 'set', objective: text };
        if (budgetRaw) payload.token_budget = Number(budgetRaw);
        try {
            _setCodexGoalModalBusy(true);
            await _postCodexGoal(payload, { systemMessage: false });
            _populateCodexGoalModal();
            showToast('Codex goal updated');
        } catch (err) {
            const msg = err.detail || err.message || 'Codex goal command failed';
            showToast(msg, 'error');
        } finally {
            _setCodexGoalModalBusy(false);
        }
    });

    const toggleGoal = async () => {
        const current = _codexGoalFromState();
        if (!current) return;
        const action = current.status === 'paused' ? 'resume' : 'pause';
        try {
            _setCodexGoalModalBusy(true);
            await _postCodexGoal({ action }, { systemMessage: false });
            _populateCodexGoalModal();
            showToast(action === 'resume' ? 'Codex goal resumed' : 'Codex goal paused');
        } catch (err) {
            const msg = err.detail || err.message || 'Codex goal command failed';
            showToast(msg, 'error');
        } finally {
            _setCodexGoalModalBusy(false);
        }
    };
    toggleBtn?.addEventListener('click', toggleGoal);
    modalToggleBtn?.addEventListener('click', toggleGoal);

    clearBtn?.addEventListener('click', () => openCodexGoalWindow({ refresh: false }));

    modalClearBtn?.addEventListener('click', async () => {
        if (!_codexGoalFromState()) return;
        try {
            _setCodexGoalModalBusy(true);
            await _postCodexGoal({ action: 'clear' }, { systemMessage: false });
            _populateCodexGoalModal();
            showToast('Codex goal cleared');
        } catch (err) {
            const msg = err.detail || err.message || 'Codex goal command failed';
            showToast(msg, 'error');
        } finally {
            _setCodexGoalModalBusy(false);
        }
    });

    modalStartBtn?.addEventListener('click', async () => {
        const current = _codexGoalFromState();
        if (!current || current.status === 'paused') return;
        document.getElementById('modal-codex-goal')?.classList.add('hidden');
        try {
            await _startCodexGoalTurn(current.objective);
        } catch (err) {
            const msg = err.detail || err.message || 'Failed to start Codex goal';
            showToast(msg, 'error');
        }
    });

    renderCodexGoalRow();
}

async function _startCodexGoalTurn(objective) {
    if (!objective) return;
    const convId = State.currentConvId;
    const parentId = _currentVisibleLeafId();
    const payload = {
        role: 'user',
        content: objective,
        send_intent: 'continue',
        generation_count: State.branchCount || 1,
        launch_generation: true,
    };
    Object.assign(payload, await _messageTopologyContract(parentId, 'continue', convId));
    _attachCCSettings(payload);
    const msg = await API.post(`/api/conversations/${convId}/messages`, payload);
    _acceptMessageCursor(msg, convId);
    if (!_sameConvId(State.currentConvId, convId)) return;
    if (msg.queued) _queuedGeneration = msg;
    if (!State.messages.some(existing => _sameMessageId(existing.id, msg.id))) {
        State.messages.push(msg);
    }
    renderMessages();
    scrollToBottom(true);
    if (msg.queued) {
        showToast('Goal queued behind the running turn');
        return;
    }
    showGenStatus('Starting goal...');
    if (!Array.isArray(msg.generation_ids)) {
        _triggerParallelGenerate(1, msg.id, {
            convId,
            threadId: msg.loom_thread_id,
        });
    }
}

/**
 * Handle meta commands that Loom processes natively (not sent to CC).
 */
function _handleMetaCommand(name, args) {
    function addSystemMessage(html, extraClass = '') {
        _addChatSystemMessage(html, extraClass);
    }

    switch (name) {
        case 'plan':
            if (!State.currentConvId) {
                showToast('No conversation selected', 'error');
                break;
            }
            API.get(`/api/conversations/${State.currentConvId}/artifacts`).then(res => {
                const artifacts = res.artifacts || [];
                const planFile = artifacts.find(a => a.name.toLowerCase() === 'implementation_plan.md');
                if (!planFile) {
                    addSystemMessage('No active implementation plan (`implementation_plan.md`) found. Run a slash command or ask the assistant to create a plan first.');
                    return;
                }
                fetch(`/api/conversations/${State.currentConvId}/file?path=${encodeURIComponent(planFile.path)}`)
                    .then(r => {
                        if (!r.ok) throw new Error(r.statusText);
                        return r.text();
                    })
                    .then(content => {
                        previewArtifact(planFile.name, content);
                    })
                    .catch(err => showToast('Failed to load plan: ' + err, 'error'));
            }).catch(err => showToast('Failed to check workspace artifacts: ' + err, 'error'));
            break;
        case 'tasks':
        case 'task':
            if (!State.currentConvId) {
                showToast('No conversation selected', 'error');
                break;
            }
            API.get(`/api/conversations/${State.currentConvId}/artifacts`).then(res => {
                const artifacts = res.artifacts || [];
                const taskFile = artifacts.find(a => a.name.toLowerCase() === 'task.md' || a.name.toLowerCase() === 'tasks.md');
                if (!taskFile) {
                    addSystemMessage('No active task list found. Create a `task.md` file in the workspace to get started.');
                    return;
                }
                fetch(`/api/conversations/${State.currentConvId}/file?path=${encodeURIComponent(taskFile.path)}`)
                    .then(r => {
                        if (!r.ok) throw new Error(r.statusText);
                        return r.text();
                    })
                    .then(content => {
                        const cleanHtml = formatContent(content);
                        addSystemMessage(`<div class="markdown-body" style="font-size:0.9em;color:var(--text-secondary)">${cleanHtml}</div>`, 'task-list-system');
                    })
                    .catch(err => showToast('Failed to load task file: ' + err, 'error'));
            }).catch(err => showToast('Failed to check workspace artifacts: ' + err, 'error'));
            break;
        case 'goal': {
            if (!State.currentConvId) {
                showToast('No conversation selected', 'error');
                break;
            }
            if (!_isCodexConversation()) {
                showToast('/goal is only available for Codex conversations', 'error');
                break;
            }
            const payload = _parseGoalCommand(args);
            if (payload.error) {
                addSystemMessage(escapeHtml(payload.error));
                showToast(payload.error, 'error');
                break;
            }
            _postCodexGoal(payload, { systemMessage: true }).then(data => {
                if (payload.start && data.goal && payload.objective) {
                    showToast('Starting Codex goal...');
                    _startCodexGoalTurn(payload.objective).catch(err => {
                        const msg = err.detail || err.message || 'Failed to start Codex goal';
                        addSystemMessage(escapeHtml(msg));
                        showToast(msg, 'error');
                    });
                } else {
                    if (payload.action === 'get') showToast(data.goal ? 'Codex goal loaded' : 'No active Codex goal');
                    else if (payload.action === 'clear') showToast('Codex goal cleared');
                    else if (payload.action === 'pause') showToast('Codex goal paused');
                    else if (payload.action === 'resume') showToast('Codex goal resumed');
                    else showToast(data.goal ? 'Codex goal updated' : 'No active Codex goal');
                }
            }).catch(err => {
                const msg = err.detail || err.message || 'Codex goal command failed';
                addSystemMessage(escapeHtml(msg));
                showToast(msg, 'error');
            });
            break;
        }
        case 'artifacts':
        case 'artifact':
            if (!State.currentConvId) {
                showToast('No conversation selected', 'error');
                break;
            }
            if (name === 'artifact' && args) {
                const targetName = args.trim().toLowerCase();
                API.get(`/api/conversations/${State.currentConvId}/artifacts`).then(res => {
                    const artifacts = res.artifacts || [];
                    const match = artifacts.find(a => a.name.toLowerCase() === targetName || a.name.toLowerCase().replace('.md', '') === targetName);
                    if (!match) {
                        showToast(`Artifact "${args}" not found`, 'error');
                        return;
                    }
                    fetch(`/api/conversations/${State.currentConvId}/file?path=${encodeURIComponent(match.path)}`)
                        .then(r => {
                            if (!r.ok) throw new Error(r.statusText);
                            return r.text();
                        })
                        .then(content => {
                            previewArtifact(match.name, content);
                        })
                        .catch(err => showToast('Failed to load artifact: ' + err, 'error'));
                }).catch(err => showToast('Failed to list artifacts: ' + err, 'error'));
                break;
            }
            API.get(`/api/conversations/${State.currentConvId}/artifacts`).then(res => {
                const artifacts = res.artifacts || [];
                if (artifacts.length === 0) {
                    addSystemMessage('No planning or artifact files found in this workspace.');
                    return;
                }
                let html = `<div style="font-size:0.9em;color:var(--text-secondary)">`;
                html += `<strong style="color:var(--cyan)">Project Artifacts:</strong><ul style="margin:8px 0;padding-left:20px;list-style-type:square">`;
                artifacts.forEach(a => {
                    const sizeKB = Math.round(a.size / 102) / 10;
                    html += `<li style="margin-bottom:4px">`
                        + `<a href="#" class="artifact-link" data-path="${escapeHtml(a.path)}" data-name="${escapeHtml(a.name)}" style="color:var(--cyan);text-decoration:underline">${escapeHtml(a.name)}</a> `
                        + `<span style="font-size:0.8em;color:var(--text-dim)">(${sizeKB} KB)</span>`
                        + `</li>`;
                });
                html += `</ul></div>`;
                addSystemMessage(html);
            }).catch(err => {
                showToast('Failed to fetch artifacts: ' + err, 'error');
            });
            break;
        case 'help':
            // Alias for /skills — show all available commands
            _handleMetaCommand('skills', args);
            break;
        case 'skills':
            _loadSkills().then(skills => {
                const lines = skills.map(s =>
                    `${s.command}  [${s.source}/${s.mode || 'headless'}]  ${s.description || ''}`
                );
                showToast(`${skills.length} commands available`, 3000);
                // Show as a system message in chat
                const container = document.getElementById('messages-container');
                if (container) {
                    const el = document.createElement('div');
                    el.className = 'system-message';
                    el.innerHTML = `<pre style="font-size:0.85em;color:var(--text-dim);white-space:pre-wrap">`
                        + `Available commands (${skills.length}):\n\n`
                        + escapeHtml(lines.join('\n'))
                        + `</pre>`;
                    container.appendChild(el);
                    el.scrollIntoView({ behavior: 'smooth' });
                }
            });
            break;
        case 'status':
            showToast(State.isGenerating ? 'Generation in progress...' : 'Idle — no active generation');
            break;
        case 'stats':
        case 'usage':
            API.get('/api/health').then(h => {
                showToast(`Uptime: ${Math.round((h.uptime || 0) / 60)}m | Conversations: ${h.conversations || '?'}`, 4000);
            }).catch(() => showToast('Could not fetch stats'));
            break;
        case 'permissions':
            showToast('Permissions are managed via the notification bell', 3000);
            break;
        case 'export':
            if (State.currentConvId) {
                window.open(`/api/conversations/${State.currentConvId}/export`, '_blank');
                showToast('Exporting conversation...');
            } else {
                showToast('No conversation selected');
            }
            break;
        case 'settings':
            // Open settings panel if it exists
            const settingsBtn = document.querySelector('[data-action="settings"]') || document.getElementById('settings-btn');
            if (settingsBtn) settingsBtn.click();
            else showToast('Settings panel not available');
            break;
        case 'fast':
            showToast('Fast mode toggle — configure in conversation settings', 3000);
            break;
        case 'passes':
            showToast('Review passes — configure in conversation settings', 3000);
            break;
        case 'privacy':
            showToast('Privacy settings — configure in Claude Code directly', 3000);
            break;
        case 'hooks':
            API.get('/api/cc-hooks').then(data => {
                const container = document.getElementById('messages-container');
                if (!container) { showToast('Could not display hooks'); return; }
                const el = document.createElement('div');
                el.className = 'system-message';
                const hookEntries = data.hooks || {};
                if (Object.keys(hookEntries).length === 0) {
                    el.innerHTML = `<pre style="font-size:0.85em;color:var(--text-dim);white-space:pre-wrap">No CC hooks configured.\n\nSearched:\n${(data.paths || []).map(p => '  ' + p).join('\n')}</pre>`;
                } else {
                    let lines = 'CC Hooks:\n\n';
                    for (const [file, hooks] of Object.entries(hookEntries)) {
                        lines += `── ${file} ──\n`;
                        for (const [event, rules] of Object.entries(hooks)) {
                            lines += `  ${event}:\n`;
                            const ruleList = Array.isArray(rules) ? rules : [rules];
                            for (const rule of ruleList) {
                                const cmd = rule.command || rule.cmd || JSON.stringify(rule);
                                const matcher = rule.matcher ? ` (${rule.matcher})` : '';
                                lines += `    → ${cmd}${matcher}\n`;
                            }
                        }
                        lines += '\n';
                    }
                    el.innerHTML = `<pre style="font-size:0.85em;color:var(--text-dim);white-space:pre-wrap">${escapeHtml(lines)}</pre>`;
                }
                container.appendChild(el);
                el.scrollIntoView({ behavior: 'smooth' });
            }).catch(() => showToast('Failed to read hooks config'));
            break;
        case 'compact': {
            const cursor = State.loomCursor;
            const parentId = _currentVisibleLeafId();
            if (!State.ws || State.ws.readyState !== WebSocket.OPEN) {
                showToast('Reconnect before preparing a checkpoint', 'error');
                break;
            }
            if (!cursor || cursor.leaf_message_id !== parentId || parentId == null) {
                showToast('Select the exact branch to compact first', 'error');
                break;
            }
            State.ws.send(JSON.stringify({
                action: 'compact', parent_id: parentId,
                client_instance_id: _loomClientInstanceId,
                loom_thread_id: cursor.thread_id, cursor_revision: cursor.revision,
                focus: args || '',
            }));
            showToast('Preparing checkpoint on the configured local model...', 5000);
            break;
        }
        case 'clear': {
            // Start a fresh branch from the current point
            if (!State.currentConvId) { showToast('No conversation selected'); break; }
            const leaf = State.messages[State.messages.length - 1];
            if (!leaf) { showToast('No messages to branch from'); break; }
            API.post(`/api/conversations/${State.currentConvId}/messages`, {
                role: 'system',
                content: '[Fresh branch started by user]',
                parent_id: leaf.id,
            }).then(marker => {
                switchToBranch(marker.id, marker.id);
                showToast('Fresh branch started');
            }).catch(() => showToast('Failed to create branch', 'error'));
            break;
        }
        case 'compact-test': {
            // Simulate full compact branching flow — creates a real system message
            if (!State.currentConvId) { showToast('No conversation selected'); break; }
            const ctLeaf = State.messages[State.messages.length - 1];
            if (!ctLeaf) { showToast('No messages to compact from'); break; }
            API.post(`/api/conversations/${State.currentConvId}/messages`, {
                role: 'system',
                content: '[CC context compactified (test) — 185,000 tokens before]',
                parent_id: ctLeaf.id,
            }).then(marker => {
                showToast('Compact test — switching to new branch', 3000);
                switchToBranch(marker.id, marker.id);
            }).catch(() => showToast('Failed to create test compact marker', 'error'));
            break;
        }
        default:
            showToast(`/${name} is handled locally but not yet implemented`, 3000);
    }
}

/**
 * Initialize slash command autocomplete on the input textarea.
 */
function _initSlashAutocomplete() {
    const input = document.getElementById('user-input');
    const container = document.getElementById('input-area') || input.parentElement;

    // Create autocomplete dropdown
    const dropdown = document.createElement('div');
    dropdown.id = 'slash-autocomplete';
    dropdown.className = 'slash-autocomplete hidden';
    container.style.position = 'relative';
    container.appendChild(dropdown);

    let _selectedIdx = -1;
    let _currentMatches = [];

    input.addEventListener('input', async () => {
        const val = input.value;
        if (!val.startsWith('/') || val.includes('\n')) {
            dropdown.classList.add('hidden');
            _currentMatches = [];
            return;
        }
        const query = val.slice(1).toLowerCase();
        // Show loading spinner on first fetch
        if (!_cachedSkills) {
            dropdown.innerHTML = '<div class="slash-item slash-loading"><span class="slash-desc">Loading commands...</span></div>';
            dropdown.classList.remove('hidden');
        }
        const skills = await _slashCandidates();
        // Filter by prefix first (for Tab completion), then fuzzy
        const prefixMatches = skills.filter(s =>
            s.name.toLowerCase().startsWith(query) ||
            (s.command || '').toLowerCase().startsWith('/' + query)
        );
        const fuzzyMatches = query
            ? skills.filter(s =>
                !prefixMatches.includes(s) && (
                    s.name.toLowerCase().includes(query) ||
                    (s.description || '').toLowerCase().includes(query)
                )
              )
            : [];
        const matches = [...prefixMatches, ...fuzzyMatches].slice(0, 15);
        if (!query) matches.splice(0, matches.length, ...skills.slice(0, 31));
        _currentMatches = matches;

        if (matches.length === 0) {
            dropdown.classList.add('hidden');
            return;
        }

        _selectedIdx = -1;
        dropdown.innerHTML = '';
        matches.forEach((skill, i) => {
            const item = document.createElement('div');
            item.className = 'slash-item';
            let sourceClass = 'system';
            let sourceLabel = 'system';
            if (skill.source === 'user') {
                sourceClass = 'user';
                sourceLabel = 'user';
            } else if (skill.mode === 'meta') {
                sourceClass = 'meta';
                sourceLabel = 'loom';
            } else if (skill.mode === 'hermes') {
                sourceClass = 'hermes';
                sourceLabel = 'hermes';
            } else if (State.currentConv) {
                const label = _getModeLabel(State.currentConv);
                sourceClass = label;
                sourceLabel = label;
            }
            const modeClass = skill.mode === 'meta' ? 'meta' : '';
            const sourceTag = `<span class="slash-source ${sourceClass} ${modeClass}">${sourceLabel}</span>`;
            item.innerHTML =
                `<span class="slash-cmd">${escapeHtml(skill.command || '/' + skill.name)}</span>` +
                sourceTag +
                `<span class="slash-desc">${escapeHtml(skill.description || '').substring(0, 80)}</span>`;
            item.addEventListener('click', () => {
                input.value = (skill.command || '/' + skill.name) + ' ';
                dropdown.classList.add('hidden');
                input.focus();
            });
            item.addEventListener('mouseenter', () => {
                dropdown.querySelectorAll('.slash-item').forEach(el => el.classList.remove('selected'));
                item.classList.add('selected');
                _selectedIdx = i;
            });
            dropdown.appendChild(item);
        });
        dropdown.classList.remove('hidden');
    });

    // Keyboard navigation
    input.addEventListener('keydown', (e) => {
        if (dropdown.classList.contains('hidden')) return;
        const items = dropdown.querySelectorAll('.slash-item');
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            _selectedIdx = Math.min(_selectedIdx + 1, items.length - 1);
            items.forEach((el, i) => el.classList.toggle('selected', i === _selectedIdx));
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            _selectedIdx = Math.max(_selectedIdx - 1, 0);
            items.forEach((el, i) => el.classList.toggle('selected', i === _selectedIdx));
        } else if (e.key === 'Tab') {
            // CLI-style tab completion: fill to longest common prefix
            e.preventDefault();
            if (_currentMatches.length === 0) return;
            if (_selectedIdx >= 0) {
                // Item highlighted — select it directly
                items[_selectedIdx].click();
                return;
            }
            // Find longest common prefix among match names
            const names = _currentMatches.map(s => s.name.toLowerCase());
            let prefix = names[0];
            for (let i = 1; i < names.length; i++) {
                while (!names[i].startsWith(prefix)) {
                    prefix = prefix.slice(0, -1);
                }
                if (!prefix) break;
            }
            if (_currentMatches.length === 1) {
                // Single match — complete fully with trailing space
                input.value = '/' + _currentMatches[0].name + ' ';
                dropdown.classList.add('hidden');
            } else if (prefix.length > input.value.length - 1) {
                // Extend to common prefix
                input.value = '/' + prefix;
                input.dispatchEvent(new Event('input'));
            }
        } else if (e.key === 'Enter' && _selectedIdx >= 0) {
            e.preventDefault();
            items[_selectedIdx].click();
        } else if (e.key === 'Escape') {
            dropdown.classList.add('hidden');
        }
    });

    // Close on outside click
    document.addEventListener('click', (e) => {
        if (!container.contains(e.target)) dropdown.classList.add('hidden');
    });
}


// ── Send Message ──

let _sendInFlight = false;

function _integrateSentMessageDuringLiveStart(message) {
    const liveDraft = State._liveStreamDraft;
    const pendingParentMatches = !!(
        _pendingLaunch
        && State.isStreaming
        && streamingDiv?.isConnected
        && _sameMessageId(_pendingLaunch.parentId, message?.id)
    );
    const liveStartedForMessage = !!(
        State.isStreaming
        && streamingDiv
        && streamingDiv.isConnected
        && ((liveDraft && _sameMessageId(liveDraft.parent_id, message?.id))
            || pendingParentMatches)
    );
    if (!liveStartedForMessage) return false;

    // stream_start can arrive before the message POST response. Re-rendering
    // here destroys the new streaming element and makes the next send appear
    // idle. Merge the user row before its assistant draft without repainting.
    const existingIndex = (State.messages || []).findIndex(existing =>
        _sameMessageId(existing.id, message.id)
    );
    if (existingIndex >= 0) {
        Object.assign(State.messages[existingIndex], message);
    } else {
        const draftIndex = liveDraft ? (State.messages || []).findIndex(existing =>
            _sameMessageId(existing.id, liveDraft.id)
        ) : -1;
        if (draftIndex >= 0) State.messages.splice(draftIndex, 0, message);
        else State.messages.push(message);
    }

    const container = document.getElementById('messages');
    const existingEl = container?.querySelector(
        `.message[data-msg-id="${message.id}"]`
    );
    container?.querySelector('.empty-loom-hint')?.remove();
    if (!existingEl && streamingDiv?.isConnected) {
        streamingDiv.parentNode.insertBefore(
            createMessageElement(message),
            streamingDiv,
        );
    }
    return true;
}

function _isolateTreeRootLiveStart(message) {
    const liveDraft = State._liveStreamDraft;
    const matchingDraft = liveDraft
        && _sameMessageId(liveDraft.parent_id, message?.id)
        ? liveDraft
        : null;

    // The POST and stream events may race, but a tree-composer root is already
    // authoritative. Replace the old visible branch locally without tearing
    // down the live stream shell that arrived first.
    State.messages = matchingDraft ? [message, matchingDraft] : [message];
    for (const node of [message, matchingDraft]) {
        if (!node?.id || (State.treeData || []).some(existing =>
            _sameMessageId(existing.id, node.id)
        )) continue;
        State.treeData.push(node);
    }

    const container = document.getElementById('messages');
    if (!container) return;
    let userEl = container.querySelector(`.message[data-msg-id="${message.id}"]`);
    if (!userEl) userEl = createMessageElement(message);
    for (const child of [...container.children]) {
        if (child === userEl || child === streamingDiv) continue;
        child.remove();
    }
    if (streamingDiv?.isConnected) {
        container.insertBefore(userEl, streamingDiv);
    } else if (!userEl.isConnected) {
        container.appendChild(userEl);
    }

    if (VIRTUAL_SCROLL.observer) {
        VIRTUAL_SCROLL.observer.disconnect();
        VIRTUAL_SCROLL.observer = null;
    }
    VIRTUAL_SCROLL.renderedStart = 0;
    hideRetryBar();
    if (typeof updateBranchNavigation === 'function') updateBranchNavigation();
}

async function sendMessage() {
    if (State.ws?._authExpired) {
        showToast('Sign-in expired. Reload this page to continue; your draft is saved.', 'warning');
        return;
    }
    if (typeof pendingAttachmentUploads === 'function' && pendingAttachmentUploads() > 0) {
        showToast('Wait for the attachment upload to finish before sending.', 'warning');
        return;
    }
    if (_sendInFlight) return;
    if (!State.currentConvId) {
        if (typeof sendFirstCasualMessage === 'function' && State.currentView === 'chat') {
            await sendFirstCasualMessage();
            return;
        }
        showToast('Create or select a conversation first', 'error');
        return;
    }

    const sendConvId = State.currentConvId;
    const sendCursorAtStart = State.loomCursor;
    const sendComposerKey = _composerDraftStorageKey(sendConvId, sendCursorAtStart);
    const input = document.getElementById('user-input');
    const originalInputValue = input.value;
    let content = originalInputValue.trim();
    const isClaudeMode = State.currentConv && State.currentConv.mode === 'claude';
    const hasImages = State.pendingImages.length > 0;
    if (!content && !State.pendingReply && !(hasImages && !isClaudeMode)) return;

    // Capture every part of the submitted action before the first await. The
    // shared composer and branch may belong to a different window by then.
    if (State.currentView === 'tree') {
        State._branchSwitchSeq = (State._branchSwitchSeq || 0) + 1;
    }
    const sendOwner = {
        convId: sendConvId,
        conversationSeq: _conversationLoadRequestSeq,
        branchSeq: State._branchSwitchSeq || 0,
        navigationEpoch: typeof _workspaceNavigationEpoch === 'undefined' ? 0 : _workspaceNavigationEpoch,
    };
    const imagePaths = hasImages ? State.pendingImages.map(img => img.path) : null;
    const pendingReply = State.pendingReply ? { ...State.pendingReply } : null;
    const describeContext = document.getElementById('describe-context')?.value.trim() || '';
    const isFromTree = State.currentView === 'tree';
    const generationCount = State.branchCount || 1;
    const sendMode = State.currentConv?.mode;
    const generationSettings = {};
    _attachCCSettings(generationSettings);
    const sendTreeData = [...(State.treeData || []), ...(State.messages || [])];
    const visibleLeafAtStart = _currentVisibleLeafId();
    const streamingAtStart = _isActuallyStreaming();
    const queueParentAtStart = streamingAtStart ? _activeQueueParentId() : null;
    const queueAtStart = _queuedGeneration;
    let sendCursor = sendCursorAtStart;
    let isNewRootThread = isNewConversationThread(sendConvId);
    _sendInFlight = true;
    _submittedSendOwner = sendOwner;
    try {

        // Handle slash commands — meta commands work in all modes
        if (content.startsWith('/')) {
            const skills = await _loadSkills();
            const translated = _translateSlashCommand(content, skills);
            if (translated && translated.meta) {
                if (_sendOwnerIsCurrent(sendOwner)) {
                    _handleMetaCommand(translated.skillName, translated.args);
                    if (input.value === originalInputValue) input.value = '';
                }
                return;
            }
            // Headless commands: substitute the NL prompt template for the raw /slash text
            if (translated) {
                content = translated.prompt;
                showToast(`Running: /${translated.skillName}`);
            }
        }

        // Fold any highlight-to-reply excerpt into the outgoing text — after slash
        // translation so the quote never masks a /command
        content = _composeReplyContent(content, pendingReply);

        // Add user message via REST — send image paths as JSON array
        const sendFingerprint = JSON.stringify({
            convId: sendConvId,
            content,
            imagePaths,
            describeContext,
        });
        const retrySendData = _pendingSendRequest?.fingerprint === sendFingerprint
            ? { ..._pendingSendRequest.msgData }
            : null;
        if (isFromTree && !retrySendData) {
            // The shared composer means "start a standalone thread" while the
            // tree is visible. Bind that intent to a new server-owned empty thread
            // before deriving any parent; selected/latest nodes are navigation
            // context only and must never become this send's parent.
            // Cursor reconciliation may repaint the branch while this request is
            // in flight. Persist the captured text first so a stale-cursor retry
            // or a real failure cannot silently erase what the user submitted.
            saveComposerDraft(sendConvId, sendCursorAtStart);
            try {
                sendCursor = await createServerLoomThreadCursor(sendConvId);
                isNewRootThread = true;
            } catch (err) {
                if (_sameConvId(State.currentConvId, sendConvId)
                    && !input.value && originalInputValue) {
                    input.value = originalInputValue;
                    autoResizeTextarea();
                }
                showToast(err.detail || err.message || 'Failed to create a new thread', 'error');
                return;
            }
        }
        let queueParentId = null;
        if (retrySendData) {
            isNewRootThread = retrySendData.topology_intent === 'new_thread'
                || retrySendData.send_intent === 'new_thread';
            queueParentId = retrySendData.queue_after_draft_id ?? null;
        } else if (!isNewRootThread && streamingAtStart) {
            if (queueAtStart) {
                showToast('One message is already queued; edit or cancel it first', 'error');
                return;
            }
            queueParentId = queueParentAtStart;
            if (queueParentId == null) {
                showToast('The running reply is not ready to accept a queued continuation yet', 'error');
                return;
            }
        } else if (!isNewRootThread && _sendOwnerIsCurrent(sendOwner) && State.isStreaming) {
            console.log('[SEND] stale isStreaming flag - resetting before new turn');
            _resetStreamState();
        }
        const msgData = {
            role: 'user',
            content: content,
            image_path: imagePaths,
            client_request_id: retrySendData?.client_request_id || (
                globalThis.crypto?.randomUUID
                    ? globalThis.crypto.randomUUID()
                    : `send-${Date.now()}-${Math.random().toString(36).slice(2)}`
            ),
            send_intent: isNewRootThread ? 'new_thread' : 'continue',
            generation_count: generationCount,
            launch_generation: true,
        };
        Object.assign(msgData, generationSettings);
        if (describeContext) msgData.describe_context = describeContext;
        // A standalone thread is an explicit root, independent of focus, view,
        // active agents, or whichever branch most recently finished loading.
        let targetParentId;
        if (isNewRootThread) {
            targetParentId = null;
        } else if (queueParentId != null) {
            // A queued continuation belongs after the running assistant. Using the
            // pre-stream visible leaf here makes the user prompt and assistant
            // siblings, which silently turns a linear queue into a branch.
            targetParentId = queueParentId;
            msgData.queue_after_draft_id = queueParentId;
        } else {
            // Continue the leaf this tab renders; another agent may update the
            // conversation-wide active flag, but that is never this send's parent.
            targetParentId = visibleLeafAtStart || null;
        }

        if (retrySendData) {
            Object.assign(msgData, retrySendData);
        } else {
            Object.assign(
                msgData,
                await _messageTopologyContract(
                    targetParentId,
                    isNewRootThread ? 'new_thread' : sendTreeData.some(node => _sameMessageId(node.parent_id, targetParentId)) ? 'branch' : 'continue',
                    sendConvId,
                    sendCursor,
                    sendTreeData,
                ),
            );
        }
        _pendingSendRequest = {
            fingerprint: sendFingerprint,
            msgData: { ...msgData },
        };
        const msg = await API.post(`/api/conversations/${sendConvId}/messages`, msgData);
        _pendingSendRequest = null;
        const firstRunId = msg.generation_ids?.length ? `gen:${msg.generation_ids[0]}` : null;
        const terminalKey = _terminalRunKey(sendConvId, firstRunId);
        const alreadyFinished = !msg.queued && firstRunId && _recentTerminalRuns.has(terminalKey);
        // stream_start/end already advanced this cursor past the user row.
        if (!alreadyFinished && _sendOwnerIsCurrent(sendOwner)) _acceptMessageCursor(msg, sendConvId);

        // An older running server still needs its explicit WS launch. Dispatch
        // that immutable action even if navigation changed the viewport while
        // the user row was being persisted. No current picker/leaf is consulted.
        if (!msg.queued && !Array.isArray(msg.generation_ids)) {
            _triggerParallelGenerate(generationCount, msg.id, {
                convId: sendConvId,
                threadId: msg.loom_thread_id,
                settings: generationSettings,
                submitted: true,
                owner: sendOwner,
                mode: sendMode,
            });
        }

        // The server already launched or durably queued this exact message.
        // If the page navigated while the request was in flight, never paint
        // the old conversation's message into the new viewport.
        if (!_sendOwnerIsCurrent(sendOwner)) return;

        // Preserve any text typed while the network request was in flight.
        if (input.value === originalInputValue) {
            input.value = '';
            if (sendComposerKey) sessionStorage.removeItem(sendComposerKey);
            saveComposerDraft(sendConvId, State.loomCursor);
        } else {
            saveComposerDraft(sendConvId, State.loomCursor);
        }
        autoResizeTextarea();
        clearPendingImages();
        clearPendingReply();

        if (alreadyFinished) {
            // Reconcile canonical order without inventing a second loading
            // phase. Do not navigate a tab that moved to another thread while
            // its POST was outstanding.
            if (State._cursorSyncPromise) await State._cursorSyncPromise;
            if (_sendOwnerIsCurrent(sendOwner)
                && State.loomCursor?.thread_id === msg.loom_thread_id) {
                if (isFromTree && State.currentView === 'tree') {
                    const leaf = _recentTerminalRuns.get(terminalKey) || msg.id;
                    const branchOwner = {...sendOwner, branchSeq: (State._branchSwitchSeq || 0) + 1};
                    await switchToBranch(leaf, msg.id, { exact: true });
                    if (!_sendOwnerIsCurrent(branchOwner)) return;
                    State._skipLoadOnChat = true;
                    switchView('chat');
                } else {
                    await loadMessages(sendConvId);
                }
            }
            return;
        }

        // The server allocates the run in the same request that stores the
        // user message. Usually generation_preparing reaches the WebSocket
        // first; this response is the deterministic fallback if that event
        // raced the socket or the tab reconnected at exactly that moment.
        if (!msg.queued && Array.isArray(msg.generation_ids)
            && msg.generation_ids.length && (!State.isStreaming || !streamingDiv?.isConnected)) {
            const genId = msg.generation_ids[0];
            const runId = `gen:${genId}`;
            _subscribedRunIds.add(runId);
            State._followingGenId = genId;
            State._followingDraftMsgId = null;
            State._streamIsOurBranch = true;
            State.isStreaming = true;
            _pendingLaunch = {
                genId,
                runId,
                parentId: msg.id,
                threadId: msg.loom_thread_id || null,
            };
            _streamStartTime = Date.now();
            appendStreamingMessage(msg.cc_model || State.currentConv?.cc_model);
            showGenStatus('Preparing turn…');
            if (State.ws?.readyState === WebSocket.OPEN
                && _sameConvId(State.ws._loomConvId, sendConvId)) {
                // Local ownership alone does not authorize server delivery.
                // The launch may have raced a replaced/absent origin socket.
                State.ws.send(JSON.stringify({action: 'subscribe_run', run_id: runId}));
                State.ws.send(JSON.stringify({action: 'request_snapshot'}));
            } else {
                connectWebSocket(sendConvId);
            }
        }

        const liveStartAlreadyRendered = !msg.queued
            && _integrateSentMessageDuringLiveStart(msg);

        // A tree-composer send created a standalone root above. Open that exact
        // root in chat so the user can watch its run; no selected/latest tree
        // node participates in placement.
        if (isFromTree) {
            if (liveStartAlreadyRendered) {
                _isolateTreeRootLiveStart(msg);
            } else {
                const branchOwner = {...sendOwner, branchSeq: (State._branchSwitchSeq || 0) + 1};
                await switchToBranch(msg.id, msg.id, { exact: true });
                if (!_sendOwnerIsCurrent(branchOwner)) return;
                const loadedMessage = State.messages.find(existing =>
                    _sameMessageId(existing.id, msg.id)
                );
                if (loadedMessage) Object.assign(loadedMessage, msg);
            }
            State._skipLoadOnChat = true;
            switchView('chat');
        }

        // Queue ownership and readiness come from the durable server response.
        if (msg.queued) {
            // Queue it — will fire when current stream ends
            _queuedGeneration = msg;
            msg.queue_status = msg.queue_status || 'queued';
            msg.queue_after_draft_id = msg.queue_after_draft_id ?? queueParentId;
            _placeQueuedMessageAfterDraft(msg);
            // Re-render so a tree-originated send updates the row that
            // switchToBranch already painted instead of appending a duplicate.
            renderMessages();
            scrollToBottom(true);
            showToast('Message queued — will send after current turn');
        } else {
            if (!liveStartAlreadyRendered) {
                if (!isFromTree) State.messages.push(msg);
                renderMessages();
            }
            scrollToBottom(true);

            const count = generationCount;
            // Preserve an authoritative preparation phase that may have
            // arrived over the socket before this POST response.
            if (!_pendingLaunch) {
                showGenStatus(count > 1 ? `Generating ${count} branches...` : 'Sending...');
            }
        }
    } catch (err) {
        // A server HTTP response is definitive: nothing ambiguous remains to
        // retry under this idempotency key. Network/transport failures retain
        // the exact request because the server may already have committed it.
        if (err?.status) _pendingSendRequest = null;
        showToast(err.detail || err.message || 'Failed to send message', 'error');
    } finally {
        _sendInFlight = false;
        if (_submittedSendOwner === sendOwner) _submittedSendOwner = null;
    }
}

async function cancelQueuedMessage(msg) {
    _queuedGeneration = null;
    // Remove from DB
    try {
        await API.del(`/api/conversations/${State.currentConvId}/messages/${msg.id}`);
        if (msg.parent_id != null) {
            await selectServerLoomCursor(State.currentConvId, msg.parent_id);
            rememberConversationLeaf(State.currentConvId, msg.parent_id);
        }
    } catch { /* already gone */ }
    // Remove from local state
    State.messages = State.messages.filter(m => m.id !== msg.id);
    const el = document.querySelector(`.message[data-msg-id="${msg.id}"]`);
    if (el) el.remove();
    showToast('Queued message cancelled');
}

async function resumeQueuedMessage(msg) {
    const convId = State.currentConvId;
    try {
        await API.post(
            `/api/conversations/${convId}/generation-queue/${msg.id}/resume`,
        );
        if (!_sameConvId(convId, State.currentConvId)) return;
        // Reconcile authoritative queue state without repainting editors or
        // overwriting a queue_started event that already arrived during POST.
        await _refreshQueuedGeneration(convId);
        showToast('Queued message resumed');
    } catch (err) {
        showToast(err.detail || err.message || 'Failed to resume queued message', 'error');
    }
}

function _currentQueueMessage(msg) {
    return (State.messages || []).find(item => _sameMessageId(item.id, msg.id)) || msg;
}

function _updateQueueActions(bar, msg) {
    bar._queueMessage = msg;
    const paused = msg.queue_status === 'paused';
    const label = bar.querySelector('.queue-status-label');
    const text = paused ? 'paused' : 'queued';
    if (label.textContent !== text) label.textContent = text;
    const resume = bar.querySelector('.queue-resume-btn');
    if (paused && !resume) {
        const button = document.createElement('button');
        button.className = 'btn-small queue-resume-btn';
        button.title = 'Resume queued message';
        button.textContent = 'Resume';
        button.addEventListener('click', () => resumeQueuedMessage(bar._queueMessage));
        label.after(button);
    } else if (!paused) {
        resume?.remove();
    }
}

function createQueueActions(msg) {
    const bar = document.createElement('div');
    bar.className = 'queue-actions';
    bar.innerHTML = '<span class="queue-status-label"></span>'
        + '<button class="btn-small queue-edit-btn" title="Edit queued message">Edit</button>'
        + '<button class="btn-small queue-cancel-btn" title="Cancel queued message">Cancel</button>';
    bar.querySelector('.queue-cancel-btn').addEventListener(
        'click', () => cancelQueuedMessage(bar._queueMessage)
    );
    bar.querySelector('.queue-edit-btn').addEventListener(
        'click', () => editQueuedMessage(bar._queueMessage)
    );
    _updateQueueActions(bar, msg);
    return bar;
}

async function editQueuedMessage(msg) {
    const msgEl = document.querySelector(`.message[data-msg-id="${msg.id}"]`);
    if (!msgEl) return;
    const contentEl = msgEl.querySelector('.message-content');
    const queueBar = msgEl.querySelector('.queue-actions');

    const textarea = document.createElement('textarea');
    textarea.className = 'edit-message-input';
    textarea.value = msg.content;
    textarea.rows = Math.max(3, msg.content.split('\n').length);
    contentEl.replaceWith(textarea);
    textarea.focus();
    const _resizeQueuedEdit = () => {
        const clone = textarea.cloneNode(true);
        clone.style.position = 'absolute';
        clone.style.visibility = 'hidden';
        clone.style.height = 'auto';
        clone.style.width = textarea.offsetWidth + 'px';
        textarea.parentNode.appendChild(clone);
        textarea.style.height = Math.min(clone.scrollHeight, 600) + 'px';
        clone.remove();
    };
    textarea.addEventListener('input', _resizeQueuedEdit);
    requestAnimationFrame(_resizeQueuedEdit);

    const btnRow = document.createElement('div');
    btnRow.className = 'edit-message-actions';
    btnRow.innerHTML = `<button class="btn-small edit-save">Save</button>`
        + `<button class="btn-small edit-cancel">Cancel edit</button>`;
    if (queueBar) queueBar.replaceWith(btnRow);
    else textarea.after(btnRow);

    const finishEdit = () => {
        const current = _currentQueueMessage(msg);
        const newContent = document.createElement('div');
        newContent.className = 'message-content';
        newContent.innerHTML = formatContent(current.content);
        textarea.replaceWith(newContent);
        btnRow.remove();
        _syncQueuedMessagePresentation(msgEl, current);
    };
    btnRow.querySelector('.edit-cancel').addEventListener('click', finishEdit);

    btnRow.querySelector('.edit-save').addEventListener('click', async () => {
        const newText = textarea.value.trim();
        if (!newText) return;
        try {
            const updated = await API.put(
                `/api/conversations/${State.currentConvId}/messages/${msg.id}`,
                { content: newText }
            );
            // Update local references
            msg.content = updated.content || newText;
            if (_queuedGeneration && _queuedGeneration.id === msg.id) {
                _queuedGeneration.content = msg.content;
            }
            const stateMsg = State.messages.find(m => m.id === msg.id);
            if (stateMsg) stateMsg.content = msg.content;

            finishEdit();
            showToast('Queued message updated');
        } catch {
            showToast('Failed to update message', 'error');
        }
    });
}

// ── Regenerate ──

async function regenerateMessage(msgId) {
    if (State.isStreaming) {
        showToast('This branch is still running. Stop its current turn before regenerating.', 'warning');
        return;
    }
    const convId = State.currentConvId;
    const threadId = State.loomCursor?.thread_id;
    const branchSwitchSeq = State._branchSwitchSeq;
    // Capture the user's picker before moving to the prompt. Cursor loading
    // can repaint the picker from an older assistant on that ancestor branch.
    const settings = {};
    _attachCCSettings(settings);

    try {
        const cursorAtStart = `${State.loomCursor?.cursor_id}:${State.loomCursor?.revision}:${State.loomCursor?.leaf_message_id ?? ''}`;
        const result = await API.post(`/api/conversations/${convId}/regenerate/${msgId}`);
        if (State._branchSwitchSeq !== branchSwitchSeq) return;
        if (!_sameConvId(State.currentConvId, convId) || State.loomCursor?.thread_id !== threadId) return;
        if (`${State.loomCursor?.cursor_id}:${State.loomCursor?.revision}:${State.loomCursor?.leaf_message_id ?? ''}` !== cursorAtStart) return;
        await selectServerLoomCursor(convId, result.parent_id);
        if (State._branchSwitchSeq !== branchSwitchSeq) return;
        if (!_sameConvId(State.currentConvId, convId)) return;

        _triggerParallelGenerate(1, result.parent_id, {convId, threadId: State.loomCursor.thread_id,
            action: 'regenerate', settings, originalMessageId: msgId});
    } catch (err) {
        showToast('Regeneration failed', 'error');
    }
}

// ── Cancel Generation ──

function _stopGeneration(disposition) {
    if (!State.ws || State.ws.readyState !== WebSocket.OPEN) {
        showToast('Reconnecting before this turn can be stopped', 'error');
        return;
    }
    const action = disposition === 'interrupt' ? 'interrupt' : 'cancel';
    const msg = { action };
    const genId = State._followingGenId ?? _pendingLaunch?.genId;
    if (genId != null) msg.gen_id = genId;
    const draftMsgId = State._followingDraftMsgId;
    if (draftMsgId != null) msg.draft_msg_id = draftMsgId;
    if (genId == null && draftMsgId == null) {
        showToast('This turn is no longer active', 'error');
        return;
    }
    showGenStatus(action === 'interrupt' ? 'Interrupting turn…' : 'Cancelling turn…');
    document.querySelectorAll('.cancel-draft-btn, .interrupt-draft-btn').forEach(button => {
        button.disabled = true;
    });
    State.ws.send(JSON.stringify(msg));
    // If not actively streaming (e.g. viewing a draft), reload after a short
    // delay to pick up the server-side cleanup (draft deletion)
    if (!State.isStreaming && State.currentConvId) {
        setTimeout(() => loadMessages(State.currentConvId), 500);
    }
}

function cancelGeneration() {
    _stopGeneration('cancel');
}

function interruptGeneration() {
    _stopGeneration('interrupt');
}

// ── Notifications (background generation landings + permission requests) ──

const _notifications = [];
const _BRANCH_ACTIVITY_SESSION_KEY = 'loom-branch-activity:v1';
const _NOTIFICATION_DISMISSALS_KEY = 'loom-notification-dismissals:v1';
function _notificationDismissalId(item) {
    return JSON.stringify([item.type, item.convId ?? null, item.requestId ?? item.id ?? null,
        item.id == null && item.requestId == null ? String(item.time || '') : null]);
}
function _readNotificationDismissals() {
    try { const value = JSON.parse(localStorage.getItem(_NOTIFICATION_DISMISSALS_KEY) || '[]'); return new Set(Array.isArray(value) ? value.filter(id => typeof id === 'string') : []); }
    catch { return new Set(); }
}
function _applyNotificationDismissals() {
    const dismissed = _readNotificationDismissals();
    for (let index = _notifications.length - 1; index >= 0; index--) {
        const item = _notifications[index];
        if (!(item.type === 'permission' && !item.resolved) && dismissed.has(_notificationDismissalId(item))) _notifications.splice(index, 1);
    }
}
function _shareNotificationDismissals() {
    const dismissed = _readNotificationDismissals();
    for (const item of _notifications) if (!(item.type === 'permission' && !item.resolved)) dismissed.add(_notificationDismissalId(item));
    try { localStorage.setItem(_NOTIFICATION_DISMISSALS_KEY, JSON.stringify([...dismissed].slice(-2048))); } catch {}
}
window.addEventListener('storage', event => {
    if (event.key !== _NOTIFICATION_DISMISSALS_KEY) return;
    _applyNotificationDismissals(); _persistBranchActivityNotifications(); _renderNotifBell(); _renderNotifDropdown();
});
let _parallelBranchActivity = [];
let _parallelActivityPollTimer = null;
let _parallelActivityRequestSeq = 0;

function _sameConvId(a, b) {
    if (a === undefined || a === null || b === undefined || b === null) return false;
    return String(a) === String(b);
}

function _parallelActivityEnabled() {
    const value = State.currentConv?.parallel_agents_enabled;
    return value === true || value === 1 || value === '1' || value === 'true';
}

function _restoreBranchActivityNotifications() {
    try {
        const saved = JSON.parse(sessionStorage.getItem(_BRANCH_ACTIVITY_SESSION_KEY) || '[]');
        if (!Array.isArray(saved)) return;
        for (const item of saved.slice(0, 50)) {
            if (!item || item.id == null || item.convId == null) continue;
            _notifications.push({
                ...item,
                type: 'branch',
                branchActivity: true,
                time: item.time ? new Date(item.time) : new Date(),
            });
        }
    } catch {
        // sessionStorage can be unavailable in privacy-restricted contexts.
    }
}

function _persistBranchActivityNotifications() {
    try {
        const saved = _notifications
            .filter(item => item.type === 'branch' && item.branchActivity)
            .slice(0, 50)
            .map(item => ({
                type: 'branch',
                branchActivity: true,
                id: item.id,
                convId: item.convId,
                convTitle: item.convTitle,
                parentId: item.parentId ?? null,
                preview: item.preview || '',
                time: item.time instanceof Date ? item.time.toISOString() : item.time,
            }));
        sessionStorage.setItem(_BRANCH_ACTIVITY_SESSION_KEY, JSON.stringify(saved));
    } catch {
        // The in-memory notification remains usable even if persistence fails.
    }
}

function _addBranchActivityNotification(notification) {
    const existing = _notifications.find(item =>
        item.type === 'branch'
        && item.branchActivity
        && _sameConvId(item.convId, notification.convId)
        && _sameMessageId(item.id, notification.id)
    );
    if (existing) Object.assign(existing, notification, { branchActivity: true });
    else _notifications.unshift({ ...notification, branchActivity: true });
    _persistBranchActivityNotifications();
    _renderNotifBell();
}

function markBranchActivitySeen(convId, messageIds) {
    const ids = new Set((Array.isArray(messageIds) ? messageIds : [messageIds])
        .filter(id => id != null)
        .map(String));
    if (!ids.size) return;
    const before = _notifications.length;
    for (let index = _notifications.length - 1; index >= 0; index--) {
        const item = _notifications[index];
        if (item.type === 'branch'
            && item.branchActivity
            && _sameConvId(item.convId, convId)
            && ids.has(String(item.id))) {
            _notifications.splice(index, 1);
        }
    }
    if (_notifications.length !== before) {
        _persistBranchActivityNotifications();
        _renderNotifBell();
    }
}

function _recomputeActivityBranchNames(treeData) {
    if (!Array.isArray(treeData) || typeof computeBranchNames !== 'function') return;
    const nodeMap = {};
    const childrenMap = {};
    const roots = [];
    for (const node of treeData) {
        nodeMap[node.id] = node;
        childrenMap[node.id] = [];
    }
    for (const node of treeData) {
        if (node.parent_id && nodeMap[node.parent_id]) childrenMap[node.parent_id].push(node.id);
        else roots.push(node.id);
    }
    State.branchNames = computeBranchNames(roots, nodeMap, childrenMap);
}

function _activityHarnessLabel(mode) {
    const labels = {
        claude: 'Claude Code', codex: 'Codex', gemini: 'Antigravity',
        goose: 'Goose', hermes: 'Hermes', dream: 'Dream',
        umans: 'Umans', openrouter: 'OpenRouter', local: 'Local',
    };
    return labels[String(mode || '').toLowerCase()] || String(mode || 'Agent');
}

function _activityBranchPath(messageId) {
    return State.branchNames?.[messageId] || `Message #${messageId}`;
}

function _activityElapsed(startedAt) {
    const seconds = Math.max(0, Math.floor((Date.now() - Number(startedAt || 0) * 1000) / 1000));
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
    return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

async function refreshParallelBranchActivity() {
    const requestSeq = ++_parallelActivityRequestSeq;
    const convId = State.currentConvId;
    if (!convId || !_parallelActivityEnabled()) {
        _parallelBranchActivity = [];
        _renderNotifBell();
        if (!document.getElementById('notif-dropdown')?.classList.contains('hidden')) {
            _renderNotifDropdown();
        }
        return;
    }
    try {
        const [generationRows, queuedRows] = await Promise.all([
            API.get('/api/generations'),
            API.get(`/api/conversations/${convId}/generation-queue`),
        ]);
        if (requestSeq !== _parallelActivityRequestSeq || !_sameConvId(convId, State.currentConvId)) return;

        const active = (Array.isArray(generationRows) ? generationRows : [])
            .filter(row => _sameConvId(row.conv_id, convId));
        const waiting = (Array.isArray(queuedRows) ? queuedRows : [])
            .filter(row => row.status === 'queued' || row.status === 'paused');
        const messageIds = [
            ...active.map(row => row.draft_msg_id),
            ...waiting.map(row => row.user_msg_id),
        ].filter(id => id != null);
        if (messageIds.some(id => !State.branchNames?.[id])) {
            const treeData = await API.get(`/api/conversations/${convId}/tree`);
            if (requestSeq !== _parallelActivityRequestSeq || !_sameConvId(convId, State.currentConvId)) return;
            State.treeData = treeData;
            if (typeof markTreeDataFresh === 'function') markTreeDataFresh(convId);
            _recomputeActivityBranchNames(treeData);
        }

        _parallelBranchActivity = [
            ...active.map(row => {
                const awaitingPermission = _notifications.some(item =>
                    item.type === 'permission'
                    && !item.resolved
                    && _sameMessageId(item.draftMsgId, row.draft_msg_id)
                );
                return {
                    key: `active:${row.draft_msg_id}`,
                    convId,
                    messageId: row.draft_msg_id,
                    state: awaitingPermission ? 'permission' : 'looming',
                    stateLabel: awaitingPermission ? 'Permission' : 'Looming',
                    harness: _activityHarnessLabel(row.mode),
                    startedAt: Number(row.started_at || 0),
                };
            }),
            ...waiting.map(row => ({
                key: `${row.status}:${row.user_msg_id}`,
                convId,
                messageId: row.user_msg_id,
                state: row.status,
                stateLabel: row.status === 'paused' ? 'Paused' : 'Queued',
                harness: _activityHarnessLabel(State.currentConv?.mode),
                startedAt: Number(row.created_at || row.message_created_at || 0),
            })),
        ].sort((left, right) => right.startedAt - left.startedAt);
        _renderNotifBell();
        if (!document.getElementById('notif-dropdown')?.classList.contains('hidden')) {
            _renderNotifDropdown();
        }
    } catch (error) {
        console.warn('[ACTIVITY] Failed to refresh parallel branch activity:', error);
    }
}

function _stopParallelActivityPolling() {
    if (_parallelActivityPollTimer) clearInterval(_parallelActivityPollTimer);
    _parallelActivityPollTimer = null;
}

function _startParallelActivityPolling() {
    _stopParallelActivityPolling();
    refreshParallelBranchActivity();
    _parallelActivityPollTimer = setInterval(() => {
        const dropdown = document.getElementById('notif-dropdown');
        if (!dropdown || dropdown.classList.contains('hidden')) {
            _stopParallelActivityPolling();
            return;
        }
        refreshParallelBranchActivity();
    }, 3000);
}

function addInfoNotification(message, severity = 'info') {
    _notifications.unshift({ type: 'info', preview: String(message), severity, time: new Date() });
    const infos = _notifications.filter(n => n.type === 'info');
    for (const notice of infos.slice(50)) _notifications.splice(_notifications.indexOf(notice), 1);
    _renderNotifBell();
    if (!document.getElementById('notif-dropdown').classList.contains('hidden')) _renderNotifDropdown();
}

function showCopyLinkDialog(text) {
    const modal = document.createElement('div');
    modal.className = 'modal';
    modal.innerHTML = '<div class="modal-content" role="dialog" aria-modal="true" aria-label="Copy Canvas link"><h2>Copy Canvas link</h2><input type="text" readonly aria-label="Canvas link"><button type="button">Close</button></div>';
    const previous = document.activeElement;
    const close = () => { modal.remove(); previous?.focus(); };
    modal.querySelector('button').addEventListener('click', close);
    modal.addEventListener('keydown', event => {
        if (event.key === 'Escape') close();
        if (event.key === 'Tab') {
            event.preventDefault();
            (document.activeElement === modal.querySelector('input') ? modal.querySelector('button') : modal.querySelector('input')).focus();
        }
    });
    document.body.appendChild(modal);
    const input = modal.querySelector('input'); input.value = text; input.focus(); input.select();
}

function addNotification(message) {
    const preview = (message.content || '').replace(/[#*_`>\[\]]/g, '').trim();
    _addBranchActivityNotification({
        type: 'branch',
        branchActivity: true,
        id: message.id,
        convId: State.currentConvId,
        convTitle: State.currentConv?.title || 'Conversation',
        parentId: message.parent_id,
        preview: preview.slice(0, 120) + (preview.length > 120 ? '…' : ''),
        time: new Date(),
    });
}

function permissionSupportsAllowAll(toolName) {
    const name = String(toolName || '');
    return name !== 'ExitPlanMode' && name !== 'exit_plan_mode';
}

function addPermissionNotification(data) {
    // Replace the metadata notice when this page receives the owning prompt.
    const noticeIndex = _notifications.findIndex(n => n.type === 'permission-notice' && n.requestId === data.request_id);
    if (noticeIndex !== -1) _notifications.splice(noticeIndex, 1);
    // Don't duplicate — check if we already have this request_id
    if (_notifications.some(n => n.type === 'permission' && n.requestId === data.request_id)) return;
    _notifications.unshift({  // permissions go to the top
        type: 'permission',
        requestId: data.request_id,
        convId: data.conv_id || State.currentConvId,
        genId: data.gen_id,
        parentId: data.parent_id,
        draftMsgId: data.draft_msg_id,
        agentRunId: data.agent_run_id,
        toolName: data.tool_name || 'Unknown',
        toolInput: data.tool_input,
        approvalMethod: data.approval_method || '',
        inputSummary: data.agent_run_id ? String(data.input_summary || '') : (data.input_summary || '').substring(0, 200),
        riskLevel: data.risk_level || '',
        riskReason: data.risk_reason || '',
        rulePreview: data.rule_preview || '',
        supportsAllowAll: data.supports_allow_all !== false && permissionSupportsAllowAll(data.tool_name),
        resolved: false,
        time: new Date(),
    });
    _renderNotifBell();
}

function resolvePermissionNotification(requestId, allowed, feedback = '', decisionSource = 'user') {
    const n = _notifications.find(n => n.type === 'permission' && n.requestId === requestId);
    if (n) {
        n.resolved = true;
        n.allowed = allowed;
        n.decisionSource = decisionSource;
        n.feedback = feedback || n.feedback || '';
        // Re-render if dropdown is visible
        const dropdown = document.getElementById('notif-dropdown');
        if (dropdown && !dropdown.classList.contains('hidden')) _renderNotifDropdown();
        // Remove after a short delay
        setTimeout(() => {
            const idx = _notifications.indexOf(n);
            if (idx !== -1) _notifications.splice(idx, 1);
            _renderNotifBell();
        }, 2000);
    }
}

function _renderNotifBell() {
    _applyNotificationDismissals();
    const bell = document.getElementById('notif-bell');
    const badge = document.getElementById('notif-badge');
    if (!bell) return;
    bell.classList.remove('hidden');
    const pendingPerms = _notifications.filter(n => (n.type === 'permission' || n.type === 'permission-notice') && !n.resolved).length;
    const total = _notifications.length;
    const activeCount = _parallelBranchActivity.length;
    bell.classList.toggle('notif-running', activeCount > 0);
    if (total > 0) {
        badge.textContent = total;
        badge.classList.remove('hidden');
        bell.classList.add('notif-active');
        // Pulse the bell for pending permissions
        if (pendingPerms > 0) {
            bell.classList.add('notif-urgent');
        } else {
            bell.classList.remove('notif-urgent');
        }
    } else {
        badge.classList.add('hidden');
        bell.classList.remove('notif-active', 'notif-urgent');
    }
}

function _appendNotifSection(list, title, items, renderItem, className = '') {
    if (!items.length) return;
    const section = document.createElement('section');
    section.className = `notif-section ${className}`.trim();
    const heading = document.createElement('div');
    heading.className = 'notif-section-title';
    heading.innerHTML = `<span>${escapeHtml(title)}</span><span>${items.length}</span>`;
    section.appendChild(heading);
    for (const item of items) section.appendChild(renderItem(item));
    list.appendChild(section);
}

function _activityBranchUrl(convId, messageId) {
    const url = new URL(window.location.href);
    url.searchParams.set('conversation', String(convId));
    url.searchParams.set('branch', String(messageId));
    url.searchParams.delete('thread');
    url.searchParams.set('view', 'chat');
    return url.href;
}

async function _navigateToActivityBranch(convId, messageId) {
    const dropdown = document.getElementById('notif-dropdown');
    dropdown?.classList.add('hidden');
    _stopParallelActivityPolling();
    if (!_sameConvId(State.currentConvId, convId)) {
        State._skipLoadOnChat = true;
        await loadConversation(convId);
    }
    if (messageId != null) {
        await switchToBranch(messageId, messageId, { exact: true });
        markBranchActivitySeen(convId, [messageId]);
    }
    State._skipLoadOnChat = true;
    switchView('chat');
}

function _renderParallelActivityItem(activity) {
    const item = document.createElement('div');
    item.className = `notif-item parallel-activity-item activity-${activity.state}`;
    const path = _activityBranchPath(activity.messageId);
    const icon = activity.state === 'permission'
        ? '!'
        : (activity.state === 'paused' ? 'Ⅱ' : (activity.state === 'queued' ? '·' : '◌'));
    const elapsed = activity.startedAt ? _activityElapsed(activity.startedAt) : '';
    item.innerHTML =
        `<span class="parallel-activity-icon" aria-hidden="true">${icon}</span>` +
        `<span class="parallel-activity-main">` +
        `<span class="parallel-activity-path" title="${escapeHtml(path)}">${escapeHtml(path)}</span>` +
        `<span class="parallel-activity-meta">${escapeHtml(activity.harness)} · ${escapeHtml(activity.stateLabel)}` +
        (elapsed ? ` · ${escapeHtml(elapsed)}` : '') +
        `</span></span>` +
        `<button class="parallel-activity-popout" type="button" title="Open this branch in another Loom window">↗</button>`;
    item.addEventListener('click', () => _navigateToActivityBranch(activity.convId, activity.messageId));
    item.querySelector('.parallel-activity-popout')?.addEventListener('click', event => {
        event.stopPropagation();
        window.open(_activityBranchUrl(activity.convId, activity.messageId), '_blank', 'noopener');
    });
    return item;
}

let _notificationRenderSignature = null;
function _renderNotifDropdown() {
    _applyNotificationDismissals();
    const dropdown = document.getElementById('notif-dropdown');
    const mobile = document.documentElement.classList.contains('true-mobile');
    const host = mobile ? document.body : document.getElementById('notif-bell-wrap');
    if (host && dropdown.parentElement !== host) host.appendChild(dropdown);
    const list = document.getElementById('notif-list');
    // Polling unchanged activity must not replace buttons during a touch gesture.
    const signature = JSON.stringify([_notifications, _parallelBranchActivity]);
    if (signature === _notificationRenderSignature && list.childNodes.length) return;
    _notificationRenderSignature = signature;
    list.innerHTML = '';
    const branchActivity = _notifications.filter(item => item.type === 'branch' && item.branchActivity);
    const otherNotifications = _notifications.filter(item => !(item.type === 'branch' && item.branchActivity));
    if (_parallelBranchActivity.length === 0 && branchActivity.length === 0 && otherNotifications.length === 0) {
        list.innerHTML = '<div class="notif-empty">No notifications</div>';
        return;
    }
    _appendNotifSection(
        list,
        'Active branches',
        _parallelBranchActivity,
        _renderParallelActivityItem,
        'parallel-activity-section',
    );
    _appendNotifSection(list, 'New activity', branchActivity, _renderBranchNotifItem, 'new-activity-section');
    const regularContainer = document.createDocumentFragment();
    for (const n of otherNotifications) {
        if (n.type === 'info') {
            const item = document.createElement('div');
            item.className = 'notif-item notif-info';
            item.textContent = `${n.time.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})} · ${n.preview}`;
            regularContainer.appendChild(item);
        } else if (n.type === 'permission-notice') {
            const item = document.createElement('div');
            item.className = 'notif-item notif-permission';
            item.innerHTML = `<div class="notif-perm-header">Approval needed · Conversation #${escapeHtml(n.convId)}</div>
                <div class="notif-perm-summary">Review on the originating page or in Admin Activity.</div>
                <button type="button" class="notif-perm-btn">Review in Admin</button>`;
            item.querySelector('button').addEventListener('click', () => {
                const url = new URL(location.origin); url.port = '3002'; url.hash = 'activity';
                window.open(url.href, '_blank', 'noopener');
            });
            item.addEventListener('click', event => {
                if (event.target.closest('button')) return;
                _navigateToActivityBranch(n.convId, n.messageId);
            });
            const openConversation = document.createElement('button');
            openConversation.type = 'button';
            openConversation.className = 'notif-perm-btn';
            openConversation.textContent = 'Open conversation';
            openConversation.addEventListener('click', event => {
                event.stopPropagation();
                _navigateToActivityBranch(n.convId, n.messageId);
            });
            if (typeof _loomRouteBase !== 'undefined' && _loomRouteBase === '/app/') {
                item.querySelector('button').remove();
                item.querySelector('.notif-perm-summary').textContent = 'Review on the originating page. Private Admin is available over Tailscale.';
            }
            const dismiss=document.createElement('button');
            item.appendChild(openConversation);
            dismiss.type='button';dismiss.className='notif-perm-btn';dismiss.textContent='Dismiss notification';
            dismiss.title='Hide this notice on this browser. The approval decision remains pending.';
            dismiss.addEventListener('click',event=>{event.stopPropagation();_dismissPermissionNotice(n.requestId);});
            item.appendChild(dismiss);
            regularContainer.appendChild(item);
        } else if (n.type === 'permission') {
            regularContainer.appendChild(_renderPermissionNotifItem(n));
        } else if (n.type === 'workspace') {
            regularContainer.appendChild(_renderWorkspaceNotifItem(n));
        } else {
            regularContainer.appendChild(_renderBranchNotifItem(n));
        }
    }
    if (otherNotifications.length) {
        const regularSection = document.createElement('section');
        regularSection.className = 'notif-section';
        regularSection.innerHTML = '<div class="notif-section-title"><span>Notifications</span></div>';
        regularSection.appendChild(regularContainer);
        list.appendChild(regularSection);
    }
    // Clear all button at bottom
    if (_notifications.length) {
        const clearBtn = document.createElement('div');
        clearBtn.className = 'notif-clear-all';
        clearBtn.textContent = 'Clear notifications';
        clearBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            _shareNotificationDismissals();
            for (const notice of [..._notifications]) if (notice.type==='permission-notice') _dismissPermissionNotice(notice.requestId,false);
            const pending = _notifications.filter(n =>
                n.type === 'permission' && !n.resolved);
            _notifications.splice(0, _notifications.length, ...pending);
            _persistBranchActivityNotifications();
            _renderNotifBell();
            _renderNotifDropdown();
        });
        list.appendChild(clearBtn);
    }
}

function _renderBranchNotifItem(n) {
    const item = document.createElement('div');
    item.className = 'notif-item notif-branch' + (n.branchActivity ? ' notif-new-branch' : '');
    const timeStr = n.time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    if (n.branchActivity) {
        const path = _sameConvId(n.convId, State.currentConvId)
            ? _activityBranchPath(n.id)
            : (n.convTitle || `Conversation ${n.convId}`);
        item.innerHTML =
            '<span class="notif-new-icon" aria-hidden="true">✦</span>' +
            '<span class="parallel-activity-main">' +
            `<span class="parallel-activity-path" title="${escapeHtml(path)}">${escapeHtml(path)}</span>` +
            `<span class="parallel-activity-meta">Completed · ${escapeHtml(timeStr)}</span>` +
            `<span class="notif-preview">${escapeHtml(n.preview || 'Response complete')}</span>` +
            '</span>' +
            '<button class="parallel-activity-popout" type="button" title="Open this branch in another Loom window">↗</button>';
        item.querySelector('.parallel-activity-popout')?.addEventListener('click', event => {
            event.stopPropagation();
            window.open(_activityBranchUrl(n.convId, n.id), '_blank', 'noopener');
        });
    } else {
        item.innerHTML = `<span class="notif-time">${timeStr}</span>`
            + `<span class="notif-preview">${escapeHtml(n.preview || '(empty)')}</span>`;
    }
    item.addEventListener('click', async () => {
        document.getElementById('notif-dropdown').classList.add('hidden');
        _stopParallelActivityPolling();
        const idx = _notifications.indexOf(n);
        if (idx !== -1) _notifications.splice(idx, 1);
        _persistBranchActivityNotifications();
        _renderNotifBell();
        if (!_sameConvId(State.currentConvId, n.convId)) {
            State._skipLoadOnChat = true;
            await loadConversation(n.convId);
        }
        if (n.branchActivity) await switchToBranch(n.id, n.id, { exact: true });
        else await switchToBranch(n.id);
        markBranchActivitySeen(n.convId, [n.id]);
        switchView('chat');
    });
    return item;
}

function _renderPermissionNotifItem(n) {
    const item = document.createElement('div');
    item.className = 'notif-item notif-permission' + (n.resolved ? ' notif-resolved' : '');
    const timeStr = n.time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

    if (n.resolved) {
        item.innerHTML =
            `<span class="notif-time">${timeStr}</span>` +
            `<span class="notif-perm-status ${n.allowed ? 'perm-allowed' : 'perm-denied'}">${n.decisionSource && n.decisionSource !== 'user' ? 'Ended' : (n.allowed ? 'Allowed' : 'Denied')}</span> ` +
            `<span class="notif-perm-tool">${escapeHtml(n.toolName)}</span>` +
            (n.feedback ? `<span class="notif-perm-feedback-sent">${escapeHtml(n.feedback)}</span>` : '');
        return item;
    }

    const isPlanNotif = n.toolName === 'ExitPlanMode' || n.toolName === 'exit_plan_mode';
    if (isPlanNotif) {
        item.classList.add('notif-plan');
        item.innerHTML =
            `<div class="notif-perm-header">` +
            `<span class="notif-time">${timeStr}</span>` +
            `<span class="notif-perm-tool">&#x1F9F5; Plan Ready</span>` +
            `</div>` +
            `<input class="permission-feedback-input notif-permission-feedback" type="text" maxlength="4000" placeholder="Optional note to the agent">` +
            `<div class="notif-perm-actions">` +
            `<button class="notif-perm-btn plan-approve" data-action="allow">Approve</button>` +
            `<button class="notif-perm-btn plan-revise" data-action="deny">Revise</button>` +
            `</div>`;
    } else {
        const allowAllButton = n.supportsAllowAll === false
            ? ''
            : `<button class="notif-perm-btn allow-all" data-action="allow-all">Allow for Branch</button>`;
        item.innerHTML =
            `<div class="notif-perm-header">` +
            `<span class="notif-time">${timeStr}</span>` +
            `<span class="notif-perm-tool">${escapeHtml(n.toolName)}</span>` +
            `</div>` +
            (n.approvalMethod ? `<div class="notif-perm-summary">${escapeHtml(n.approvalMethod)}</div>` : '') +
            (n.agentRunId ? `<div class="notif-perm-summary">Child ${escapeHtml(n.agentRunId.slice(0, 8))}</div>` : '') +
            (n.riskReason ? `<div class="notif-perm-summary notif-perm-risk">Warning: ${escapeHtml(n.riskReason)}</div>` : '') +
            (n.rulePreview ? `<div class="notif-perm-summary">Branch rule: ${escapeHtml(n.rulePreview)}</div>` : '') +
            `<div class="notif-perm-summary">${escapeHtml(n.inputSummary)}</div>` +
            (n.toolInput != null ? `<details class="agent-request-arguments"><summary>Exact arguments</summary><pre>${escapeHtml(typeof n.toolInput === 'string' ? n.toolInput : JSON.stringify(n.toolInput, null, 2))}</pre></details>` : '') +
            `<input class="permission-feedback-input notif-permission-feedback" type="text" maxlength="4000" placeholder="Optional note to the agent">` +
            `<div class="notif-perm-actions">` +
            `<button class="notif-perm-btn allow" data-action="allow">Allow</button>` +
            `<button class="notif-perm-btn deny" data-action="deny">Deny</button>` +
            allowAllButton +
            `</div>`;
    }

    item.querySelectorAll('.notif-perm-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const action = btn.dataset.action;
            const allow = action === 'allow' || action === 'allow-all';
            const always = action === 'allow-all';
            const feedback = (item.querySelector('.permission-feedback-input')?.value || '').trim();
            if (!_sameConvId(n.convId, State.currentConvId)) {
                showToast('Open the owning conversation to answer this permission request.', 'error');
                return;
            }
            if (n.agentRunId && (!State.ws || State.ws.readyState !== WebSocket.OPEN)) {
                showToast('Disconnected: reconnect before responding to this request.', 'error');
                return;
            }
            if (State.ws && State.ws.readyState === WebSocket.OPEN) {
                State.ws.send(JSON.stringify({
                    action: 'permission_response',
                    request_id: n.requestId,
                    allow, always, feedback,
                }));
            }
            n.resolved = true;
            n.allowed = allow;
            n.feedback = feedback;
            item.querySelectorAll('button').forEach(button => { button.disabled = true; });
            _scheduleAgentRender(true);
            _renderNotifDropdown();
            _renderNotifBell();
            // Also update the inline prompt if it exists
            resolvePermissionPrompt(n.requestId, allow, feedback);
            // If plan was approved, flip dropdown back to Act
            if (isPlanNotif && allow) {
                const permSel = document.getElementById('cc-permission-mode-inline');
                if (permSel) permSel.value = 'default';
            }
            // Auto-remove after delay
            setTimeout(() => {
                const idx = _notifications.indexOf(n);
                if (idx !== -1) _notifications.splice(idx, 1);
                _renderNotifBell();
            }, 2000);
        });
    });

    // Click on the item body (not buttons) navigates to the conversation
    item.addEventListener('click', async (e) => {
        if (e.target.closest('.notif-perm-btn, .permission-feedback-input')) return;
        if (n.agentRunId) return; // Child review must not move the parent chat cursor.
        if (!_sameConvId(State.currentConvId, n.convId)) {
            State._skipLoadOnChat = true;
            await loadConversation(n.convId);
        }
        const targetId = n.draftMsgId || n.parentId;
        if (targetId != null) await switchToBranch(targetId, targetId, { exact: true });
        switchView('chat');
    });

    return item;
}

// Wire up bell + clear — called once from initInlineCCControls or on DOMContentLoaded
function _initNotifications() {
    const bell = document.getElementById('notif-bell');
    const dropdown = document.getElementById('notif-dropdown');
    if (!bell) return;
    _restoreBranchActivityNotifications();
    _renderNotifBell();
    bell.addEventListener('click', (e) => {
        e.stopPropagation();
        dropdown.classList.toggle('hidden');
        if (!dropdown.classList.contains('hidden')) {
            _renderNotifDropdown();
            _startParallelActivityPolling();
        } else {
            _stopParallelActivityPolling();
        }
    });
    document.getElementById('notif-refresh').addEventListener('click', (e) => {
        e.stopPropagation();
        const before = _notifications.length;
        _notifications.splice(0, _notifications.length, ..._notifications.filter(n => !(n.type === 'permission' && n.resolved)));
        if (_notifications.length !== before) {
            _persistBranchActivityNotifications();
            _renderNotifBell();
        }
        refreshParallelBranchActivity();
    });
    // Close dropdown on outside click
    document.addEventListener('click', (e) => {
        if (!dropdown.contains(e.target) && e.target !== bell) {
            dropdown.classList.add('hidden');
            _stopParallelActivityPolling();
        }
    });
}

// ── Render Messages ──

const VIRTUAL_SCROLL = {
    initialCount: 15,   // messages to render initially (from the end)
    batchSize: 10,      // messages to load per scroll-up
    renderedStart: 0,   // index into State.messages of the first rendered msg
    observer: null,     // IntersectionObserver for the sentinel
};

// An inline edit is user-owned transient state. Background queue/stream
// reconciliation may update State.messages while it is open, but must not
// repaint the container and detach the textarea underneath the user.
let _inlineEditContext = null;
let _messageRenderDeferredForEdit = false;

function _releaseInlineEditRenderGuard({ flushDeferred = false } = {}) {
    _inlineEditContext = null;
    const shouldFlush = flushDeferred && _messageRenderDeferredForEdit;
    _messageRenderDeferredForEdit = false;
    if (shouldFlush) renderMessages();
}

function renderMessages() {
    const activeEditor = document.querySelector('.edit-message-input');
    if (_inlineEditContext
        && activeEditor
        && _sameConvId(_inlineEditContext.convId, State.currentConvId)) {
        _messageRenderDeferredForEdit = true;
        return;
    }
    if (_inlineEditContext && (!_sameConvId(_inlineEditContext.convId, State.currentConvId) || !activeEditor)) {
        _releaseInlineEditRenderGuard();
    }

    // Ensure branch names are computed from current tree data
    if (State.treeData && State.treeData.length > 0 && typeof computeBranchNames === 'function') {
        const nodeMap = {};
        const childrenMap = {};
        const roots = [];
        for (const n of State.treeData) { nodeMap[n.id] = n; childrenMap[n.id] = []; }
        for (const n of State.treeData) {
            if (n.parent_id && nodeMap[n.parent_id]) childrenMap[n.parent_id].push(n.id);
            else roots.push(n.id);
        }
        State.branchNames = computeBranchNames(roots, nodeMap, childrenMap);
    }

    if (typeof updateBranchNavigation === 'function') updateBranchNavigation();
    if (State.currentView === 'chat' && typeof updateBreadcrumbs === 'function') updateBreadcrumbs();

    const container = document.getElementById('messages');
    container.innerHTML = '';
    // Clear streamingDiv reference — innerHTML='' detaches it from DOM
    streamingDiv = null;

    // Clean up previous observer
    if (VIRTUAL_SCROLL.observer) {
        VIRTUAL_SCROLL.observer.disconnect();
        VIRTUAL_SCROLL.observer = null;
    }

    if (State.messages.length === 0 && State.currentConvId) {
        container.innerHTML = '<div class="empty-loom-hint">' +
            '<p>No messages on this branch.</p>' +
            '<p>Type a message below to start a new thread.</p>' +
            '</div>';
        return;
    }

    const renderMsgs = State.messages;

    // Only render the last N messages initially
    const startIdx = Math.max(0, renderMsgs.length - VIRTUAL_SCROLL.initialCount);
    VIRTUAL_SCROLL.renderedStart = startIdx;

    // Add sentinel at top if there are older messages to load
    if (startIdx > 0) {
        const sentinel = document.createElement('div');
        sentinel.id = 'scroll-sentinel';
        sentinel.className = 'scroll-sentinel';
        sentinel.textContent = `↑ ${startIdx} older messages`;
        container.appendChild(sentinel);

        // Set up IntersectionObserver to load more on scroll
        const scrollParent = document.getElementById('messages-container');
        VIRTUAL_SCROLL.observer = new IntersectionObserver((entries) => {
            if (entries[0].isIntersecting && !VIRTUAL_SCROLL.isLoadingOlder) {
                loadOlderMessages(renderMsgs, container, scrollParent);
            }
        }, { root: scrollParent, threshold: 0.1 });
        VIRTUAL_SCROLL.observer.observe(sentinel);
    }

    // Render the visible messages
    for (let i = startIdx; i < renderMsgs.length; i++) {
        container.appendChild(createMessageElement(renderMsgs[i]));
    }

    // Convert the exact live draft to a streaming div. A queued continuation
    // may follow it in State.messages, so the last row is not necessarily the
    // assistant that is still streaming.
    const lastMsg = State.messages[State.messages.length - 1];
    const liveDraftId = State._liveStreamDraft?.id ?? State._followingDraftMsgId;
    const streamingMsg = State.isStreaming
        ? (State.messages.find(message =>
            message.role === 'assistant'
            && liveDraftId != null
            && _sameMessageId(message.id, liveDraftId)
        ) || (lastMsg?.role === 'assistant' ? lastMsg : null))
        : null;
    if (streamingMsg) {
        const streamingEl = container.querySelector(
            `.message[data-msg-id="${streamingMsg.id}"]`
        );
        const nextElement = streamingEl?.nextElementSibling || null;
        if (streamingEl) streamingEl.remove();
        appendStreamingMessage(
            streamingMsg.cc_model_used,
            streamingMsg.model_attestation,
            streamingMsg.id,
            streamingMsg.session_continuity,
        );
        if (nextElement?.isConnected && streamingDiv?.isConnected) {
            container.insertBefore(streamingDiv, nextElement);
        }
        // Render accumulated content_blocks from the draft
        if (streamingMsg.content_blocks && streamingDiv) {
            try {
                const blocks = typeof streamingMsg.content_blocks === 'string'
                    ? JSON.parse(streamingMsg.content_blocks) : streamingMsg.content_blocks;
                if (blocks && blocks.length > 0) {
                    const contentEl = streamingDiv.querySelector('.message-content');
                    contentEl.innerHTML = renderContentBlocks(blocks);
                }
            } catch {}
        }
        // Also show any text content accumulated so far
        if (streamingMsg.content && streamingMsg.content.trim() && streamingDiv) {
            const contentEl = streamingDiv.querySelector('.message-content');
            if (!contentEl.innerHTML.trim()) {
                contentEl.innerHTML = formatContent(streamingMsg.content);
            }
        }
        if (streamingDiv && parseChatItems(streamingMsg.chat_items).length) {
            streamingDiv.insertAdjacentHTML('beforeend', renderChatItems(streamingMsg.chat_items));
        }
    } else {
        // Empty assistant messages are rendered as "Generating..." by createMessageElement.
        // The retry bar is only shown from WS error/cancelled events, not from render path,
        // because empty drafts in the DB are still-generating (server deletes failed drafts).
        if (lastMsg && lastMsg.role === 'user' && !State.isStreaming) {
            showGenerateBar();
        }
    }

    // Check if the last message has children on other branches
    if (lastMsg && !State.isStreaming) {
        showChildBranchHint(lastMsg.id, container);
    }
}

function loadOlderMessages(renderMsgs, container, scrollParent) {
    const currentStart = VIRTUAL_SCROLL.renderedStart;
    if (currentStart <= 0 || VIRTUAL_SCROLL.isLoadingOlder) return;
    VIRTUAL_SCROLL.isLoadingOlder = true;

    try {
        // Calculate new range
        const newStart = Math.max(0, currentStart - VIRTUAL_SCROLL.batchSize);
        const batch = renderMsgs.slice(newStart, currentStart);

        // Anchor to an existing DOM element rather than scrollHeight deltas.
        // scrollHeight can keep growing after layout (images, fonts, code
        // blocks) so the delta math was under-adjusting and the view would
        // jump. Remember the first-rendered old message's screen position,
        // then scroll it back to the same spot after insert.
        const sentinel = document.getElementById('scroll-sentinel');
        const anchorEl = sentinel ? sentinel.nextElementSibling : container.firstElementChild;
        const anchorTopBefore = anchorEl ? anchorEl.getBoundingClientRect().top : null;

        const fragment = document.createDocumentFragment();
        for (let i = 0; i < batch.length; i++) {
            fragment.appendChild(createMessageElement(batch[i]));
        }
        const refNode = sentinel ? sentinel.nextSibling : container.firstChild;
        container.insertBefore(fragment, refNode);

        VIRTUAL_SCROLL.renderedStart = newStart;

        // Update sentinel text or remove if no more messages
        if (newStart <= 0) {
            if (sentinel) sentinel.remove();
            if (VIRTUAL_SCROLL.observer) {
                VIRTUAL_SCROLL.observer.disconnect();
                VIRTUAL_SCROLL.observer = null;
            }
        } else if (sentinel) {
            sentinel.textContent = `↑ ${newStart} older messages`;
        }

        // Restore scroll position: after paint, anchor element has shifted
        // down by (batch height). Offset scrollTop so its getBoundingClientRect
        // is back where it was. Double rAF covers fonts/images that resize
        // between layout and paint.
        const restoreAnchor = () => {
            if (anchorEl && anchorTopBefore != null) {
                const anchorTopAfter = anchorEl.getBoundingClientRect().top;
                const shift = anchorTopAfter - anchorTopBefore;
                if (shift) {
                    const prevBehavior = scrollParent.style.scrollBehavior;
                    scrollParent.style.scrollBehavior = 'auto';
                    scrollParent.scrollTop += shift;
                    // Force a reflow to apply the scroll change before restoring scrollBehavior
                    void scrollParent.offsetHeight;
                    scrollParent.style.scrollBehavior = prevBehavior;
                }
            }
            VIRTUAL_SCROLL.isLoadingOlder = false;
        };
        requestAnimationFrame(() => requestAnimationFrame(restoreAnchor));
    } catch (e) {
        VIRTUAL_SCROLL.isLoadingOlder = false;
        throw e;
    }
}

function showChildBranchHint(msgId, container) {
    // Compute children from State.treeData (no API call needed)
    if (!State.treeData) return;
    const children = State.treeData.filter(n => n.parent_id === msgId);
    if (!children || children.length === 0) return;

    const hint = document.createElement('div');
    hint.className = 'child-branch-hint';
    const count = children.length;
    hint.innerHTML = `<span>${count} response${count > 1 ? 's' : ''} on ${count > 1 ? 'branches' : 'a branch'} below</span>`;

    for (const child of children) {
        const btn = document.createElement('button');
        const preview = (child.preview || '').substring(0, 40) + ((child.preview || '').length > 40 ? '...' : '');
        btn.textContent = preview || child.role;
        btn.title = 'Switch to this branch';
        btn.addEventListener('click', async () => {
            await switchToBranch(child.id);
        });
        hint.appendChild(btn);
    }

    container.appendChild(hint);
}

// Submitted WS launches own their transport. Replacing the viewport socket
// during navigation cannot delete a captured launch or replay it on another
// conversation's socket. Modern Send uses the atomic HTTP endpoint above;
// these bounded sockets also support manual Run and older running servers.
let _pendingGenerate = null;
const _pendingGenerates = new Map();

function _launchOperationIsVisible(operation) {
    return _sendOwnerIsCurrent(operation.owner)
        && String(State.loomCursor?.thread_id) === String(operation.threadId);
}

function _forgetPendingGenerate(operation) {
    if (_pendingGenerates.get(operation.key) !== operation) return;
    _pendingGenerates.delete(operation.key);
    _pendingGenerate = _pendingGenerates.values().next().value || null;
}

async function _recoverGenerationLaunch(operation) {
    operation.view = LoomCoordination.launchStep(operation.view, 'disconnected');
    if (!_launchOperationIsVisible(operation)) return;
    if (!streamingDiv?.isConnected) appendStreamingMessage();
    showGenStatus('Checking the submitted turn…', true);
    connectWebSocket(operation.convId);
    const recoverySocket = State.ws;
    const snapshotCheck = new Promise(resolve => {
        const timer = setTimeout(() => finish(null), 5000);
        const receive = event => {
            const data = JSON.parse(event.data);
            if (data.type === 'generation_active' || data.type === 'generation_idle') finish(data);
        };
        const closed = () => finish(null);
        function finish(data) {
            clearTimeout(timer);
            recoverySocket.removeEventListener('message', receive);
            recoverySocket.removeEventListener('close', closed);
            resolve(data);
        }
        recoverySocket.addEventListener('message', receive);
        recoverySocket.addEventListener('close', closed);
    });
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), 5000);
    let recovered = false;
    let confirmedInactive = false;
    try {
        const url = `/api/conversations/${operation.convId}/messages/${operation.parentId}/children`;
        const response = await fetch(url, {signal: controller.signal});
        if (!response.ok) throw await apiError(response, 'GET', url);
        const children = await response.json();
        const snapshot = await snapshotCheck;
        if (!_launchOperationIsVisible(operation)) return;
        const running = (snapshot?.snapshots || []).some(run =>
            String(run.client_instance_id) === String(_loomClientInstanceId)
            && String(run.loom_thread_id) === String(operation.threadId)
            && _sameMessageId(run.parent_id, operation.parentId));
        // No draft is expected while workspace preparation is still running.
        // Only the fresh snapshot can distinguish that from a failed launch.
        if (running) { recovered = true; return; }
        confirmedInactive = snapshot != null;
        const candidates = children.filter(message => message.role === 'assistant'
            && String(message.loom_thread_id) === String(operation.threadId)
            && Number(message.created_at) >= operation.startedAt
            && !_sameMessageId(message.id, operation.originalMessageId));
        if (candidates.length === 1) {
            recovered = true;
            await selectServerLoomCursor(operation.convId, candidates[0].id);
            if (!_launchOperationIsVisible(operation)) return;
            await loadMessages(operation.convId, {preserveView: true});
            connectWebSocket(operation.convId);
        }
    } catch (error) {
        recovered = false;
    } finally {
        clearTimeout(deadline);
        // A connected snapshot may already have recovered this exact launch.
        // Never erase that run because the independent HTTP check found no draft.
        const followedNewRun = State._followingGenId != null
            && (operation.runId ? `gen:${State._followingGenId}` === operation.runId
                : _sameMessageId(State._liveStreamDraft?.parent_id ?? _pendingLaunch?.parentId, operation.parentId));
        if (!recovered && !followedNewRun && _launchOperationIsVisible(operation)) {
            operation.view = LoomCoordination.launchStep(operation.view, 'rejected');
            removeStreamingMessage();
            _clearTerminalStreamState({clearParallel: true});
            hideGenStatus();
            _launchRecoveryNotice = {owner: operation.owner, text: operation.sent
                ? 'Connection lost. Could not confirm this turn. Your saved reply is preserved; check the branch before rerunning.'
                : 'Launch connection failed before the command was sent. Your saved reply is preserved.'};
            _showLaunchRecoveryNotice();
            if (operation.originalMessageId != null && (!operation.sent || confirmedInactive)) {
                try {
                    await selectServerLoomCursor(operation.convId, operation.originalMessageId);
                    if (!_launchOperationIsVisible(operation)) return;
                    await loadMessages(operation.convId, {preserveView: true});
                } catch (_) { /* The persistent receipt still reports uncertainty. */ }
            }
            _launchRecoveryNotice = {owner: operation.owner, text: operation.sent
                ? 'Connection lost. Could not confirm this turn. Your saved reply is preserved; check the branch before rerunning.'
                : 'Launch connection failed before the command was sent. Your saved reply is preserved.'};
            // Ambiguous admission is not permission to launch a second paid run.
            _showLaunchRecoveryNotice();
        }
    }
}

function _openGenerationLaunchSocket(operation) {
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    const params = new URLSearchParams({client_instance_id: _loomClientInstanceId});
    const viewport = State.ws;
    const reuseViewport = viewport?.readyState === WebSocket.OPEN
        && _sameConvId(viewport._loomConvId, operation.convId)
        && Date.now() - (viewport._lastActivity || 0) < 15000;
    const socket = reuseViewport ? viewport
        : new WebSocket(`${protocol}://${location.host}/ws/chat/${operation.convId}?${params}`);
    const priorMessage = reuseViewport ? socket.onmessage : null;
    const priorClose = reuseViewport ? socket.onclose : null;
    let launchMessage, launchClose;
    const finishTransport = () => {
        clearTimeout(launchTimeout);
        if (keepalive) clearInterval(keepalive);
        if (!reuseViewport) { socket.close(); return; }
        if (socket.onmessage === launchMessage) socket.onmessage = priorMessage;
        if (socket.onclose === launchClose) socket.onclose = priorClose;
    };
    socket._loomConvId = operation.convId;
    operation.socket = socket;
    const launchTimeout = setTimeout(() => {
        if (!operation.runId) {
            finishTransport();
            _forgetPendingGenerate(operation);
            void _recoverGenerationLaunch(operation);
        }
    }, 5000);
    let keepalive = null;
    const launchOpen = () => {
        _flushPendingGenerate(socket);
        keepalive = setInterval(() => {
            if (socket.readyState === WebSocket.OPEN) {
                socket.send(JSON.stringify({action: 'ping',
                    ...(operation.runId ? {run_id: operation.runId, draft_msg_id: operation.draftId} : {})}));
            }
        }, 15000);
    };
    const receiveLaunch = event => {
        const data = JSON.parse(event.data);
        if (data.type === 'branch_landed' || data.type === 'permission_notice') {
            _handleSocketMessage(data);
            return;
        }
        const runId = _canonicalClientRunId(data);
        if ((data.type === 'generation_preparing' || data.type === 'stream_start'
                || (data.type === 'error' && data.message && runId))
            && _sameMessageId(data.parent_id, operation.parentId)
            && String(data.client_instance_id || '') === String(_loomClientInstanceId)) {
            operation.runId = runId;
            operation.view = LoomCoordination.launchStep(operation.view, 'admitted');
            operation.draftId = data.draft_msg_id ?? operation.draftId;
            clearTimeout(launchTimeout);
            _forgetPendingGenerate(operation);
            if (_launchOperationIsVisible(operation) && runId) {
                _subscribedRunIds.add(runId);
                if (data.type === 'error') {
                    State._followingGenId = data.gen_id;
                    State._followingDraftMsgId = data.message.id;
                    State._streamIsOurBranch = true;
                    State.isStreaming = true;
                }
            }
            if (_launchOperationIsVisible(operation) && operation.originalMessageId != null) {
                const index = State.messages.findIndex(message => _sameMessageId(message.id, operation.originalMessageId));
                if (index !== -1) {
                    State.messages = State.messages.slice(0, index);
                    renderMessages();
                }
            }
            // Establish the viewport transport after launch acknowledgement.
            // Connecting before admission can capture an idle snapshot that
            // arrives after preparing/start on this independent socket.
            if (_launchOperationIsVisible(operation)
                && (!State.ws || State.ws.readyState !== WebSocket.OPEN
                    || Date.now() - (State.ws._lastActivity || 0) > 30000)) {
                connectWebSocket(operation.convId);
            }
            if (_launchOperationIsVisible(operation) && State.ws?.readyState === WebSocket.OPEN && runId) {
                State.ws.send(JSON.stringify({action: 'subscribe_run', run_id: runId}));
                State.ws.send(JSON.stringify({action: 'request_snapshot'}));
            }
        }
        if (!operation.runId || runId !== operation.runId) {
            if (data.type === 'error' && !runId) {
                showToast(data.error || 'The submitted generation could not be launched', 'error');
                _forgetPendingGenerate(operation);
                if (_launchOperationIsVisible(operation)) hideGenStatus();
                operation.terminal = true;
                operation.view = LoomCoordination.launchStep(operation.view, 'rejected');
                if (_launchOperationIsVisible(operation)) {
                    removeStreamingMessage();
                    showRetryBar(data.error || 'The submitted generation could not be launched');
                }
                finishTransport();
            }
            return;
        }
        const terminal = ['stream_end', 'generation_terminal', 'cancelled', 'interrupted', 'error'].includes(data.type);
        if (_launchOperationIsVisible(operation)) {
            // OPEN alone is not subscription evidence: a fast terminal can
            // arrive before the viewport has acknowledged its exact run.
            // Typed envelopes let both transports bridge that gap safely.
            if (data.event_id || State.ws?.readyState !== WebSocket.OPEN) _handleSocketMessage(data);
        } else if (terminal) {
            const messageId = data.message?.id ?? data.draft_msg_id ?? null;
            _recentTerminalRuns.set(_terminalRunKey(operation.convId, runId), messageId);
            if (_recentTerminalRuns.size > 128) _recentTerminalRuns.delete(_recentTerminalRuns.keys().next().value);
            if (messageId != null && ['stream_end', 'generation_terminal'].includes(data.type)) {
                handleWSMessage({type: 'branch_landed', conv_id: operation.convId, message_id: messageId});
            }
        }
        if (terminal) {
            operation.terminal = true;
            operation.view = LoomCoordination.launchStep(operation.view, 'terminal');
            _forgetPendingGenerate(operation);
            finishTransport();
        }
    };
    const closeLaunch = () => {
        clearTimeout(launchTimeout);
        if (keepalive) clearInterval(keepalive);
        if (_pendingGenerates.get(operation.key) !== operation) {
            if (operation.runId && !operation.terminal) void _recoverGenerationLaunch(operation);
            return;
        }
        _forgetPendingGenerate(operation);
        // A sent command has an ambiguous outcome. Do not replay a possible
        // launch and duplicate paid work; reconnect snapshots recover its run.
        void _recoverGenerationLaunch(operation);
    };
    launchMessage = event => {
        receiveLaunch(event);
        if (priorMessage) priorMessage.call(socket, event);
    };
    launchClose = event => {
        if (priorClose) priorClose.call(socket, event);
        closeLaunch();
    };
    socket.onmessage = launchMessage;
    socket.onclose = launchClose;
    if (reuseViewport) launchOpen();
    else {
        socket.onopen = launchOpen;
        socket.onerror = () => {};
    }
}

function _triggerParallelGenerate(count, parentId, route = {}) {
    if (State.ws?._authExpired) {
        showToast('Sign-in expired. Reload this page to continue.', 'warning');
        return false;
    }
    const convId = route.convId ?? State.currentConvId;
    const cursor = State.loomCursors?.[String(convId)] || null;
    const threadId = route.threadId ?? cursor?.thread_id ?? null;
    if (!threadId || (!route.submitted && (!_sameConvId(State.currentConvId, convId)
        || String(State.loomCursor?.thread_id || '') !== String(threadId)))) {
        showToast('The selected Loom thread changed; generation was not launched', 'error');
        return false;
    }
    const isWeave = (route.mode ?? State.currentConv?.mode) === 'weave';
    const n = isWeave ? Math.max(1, Math.min(5, count)) : 1;
    const targetParentId = parentId ?? _currentVisibleLeafId();
    if (targetParentId == null) {
        showToast('Choose an exact message before launching generation', 'error');
        return false;
    }
    const settings = route.settings ? { ...route.settings } : {};
    if (!route.settings) _attachCCSettings(settings);
    const packet = {
        ...settings,
        action: route.action || 'generate',
        parent_id: targetParentId,
        client_instance_id: _loomClientInstanceId,
        loom_thread_id: threadId,
        generation_count: n,
    };
    const owner = route.owner || {
        convId,
        conversationSeq: _conversationLoadRequestSeq,
        branchSeq: State._branchSwitchSeq || 0,
        navigationEpoch: typeof _workspaceNavigationEpoch === 'undefined' ? 0 : _workspaceNavigationEpoch,
    };

    // Every launch needs acknowledgement. Reuse a recently responsive viewport;
    // only a stale or disconnected viewport needs another transport handshake.
    const key = `${convId}/${threadId}/${targetParentId}/${packet.action}`;
    if (_pendingGenerates.has(key)) return false;
    _launchRecoveryNotice = null;
    const operation = {key, convId, threadId, parentId: targetParentId, packet, owner,
        sent: false, startedAt: Date.now() / 1000, originalMessageId: route.originalMessageId,
        view: LoomCoordination.launchStep('idle', 'submit')};
    _pendingGenerates.set(key, operation);
    _pendingGenerate = _pendingGenerate || operation;
    if (_launchOperationIsVisible(operation)) {
        if (!streamingDiv?.isConnected) appendStreamingMessage();
        showGenStatus('Submitting turn…');
    }
    _openGenerationLaunchSocket(operation);
    return true;
}

function _recoverForegroundConnection() {
    if (document.hidden || !State.currentConvId || State.currentView === 'home') return;
    const socket = State.ws;
    if (socket?.readyState === WebSocket.CONNECTING) return;
    if (!socket || socket.readyState !== WebSocket.OPEN
        || Date.now() - (socket._lastActivity || 0) > 15000) {
        connectWebSocket(State.currentConvId);
    } else {
        socket.send(JSON.stringify({action: 'request_snapshot', ..._followedGenerationProbe()}));
    }
}
document.addEventListener('visibilitychange', _recoverForegroundConnection);
window.addEventListener('pageshow', _recoverForegroundConnection);
window.addEventListener('focus', _recoverForegroundConnection);

// Fired by connectWebSocket onopen — replays a stashed generate so the user
// doesn't have to resend after a silent mobile reconnect.
function _flushPendingGenerate(socket = State.ws) {
    if (socket?.readyState !== WebSocket.OPEN) return;
    for (const operation of _pendingGenerates.values()) {
        if (operation.sent || operation.socket !== socket
            || !_sameConvId(socket._loomConvId, operation.convId)) continue;
        // Keep pending ownership until the server acknowledges this exact
        // parent/run. No timer gap, and no mutable picker or viewport lookup.
        socket.send(JSON.stringify(operation.packet));
        operation.sent = true;
    }
}

/** Attach cc_model/effort/permission to a WS message from current UI state */
function _attachCCSettings(msg) {
    const conv = State.currentConv;
    if (!conv || (conv.mode !== 'claude' && conv.mode !== 'local' && conv.mode !== 'codex' && conv.mode !== 'gemini' && conv.mode !== 'goose' && conv.mode !== 'umans' && conv.mode !== 'openrouter')) return;
    const modelSel = document.getElementById('cc-model-inline');
    const effortSel = document.getElementById('cc-effort-inline');
    const branchModel = (typeof _selectedCCModelForBranch === 'function')
        ? _selectedCCModelForBranch(conv) : null;
    // The visible picker is authoritative at send time. Branch history only
    // supplies a fallback when there is no selected model in the composer.
    const chosenModel = (modelSel && modelSel.value) || branchModel || conv.cc_model || null;
    if (chosenModel) msg.cc_model = chosenModel;
    if (effortSel && effortSel.value) msg.cc_effort = effortSel.value;
    else if (conv.cc_effort) msg.cc_effort = conv.cc_effort;
    msg.cc_permission_mode = 'default';
}

function showGenerateBar() {
    hideRetryBar();
    const container = document.getElementById('messages');
    const bar = document.createElement('div');
    bar.id = 'retry-bar';
    bar.className = 'retry-bar generate-bar';
    bar.innerHTML = `
        <span class="retry-error">No response yet</span>
        <button class="btn-small retry-btn" id="btn-generate">Generate</button>
    `;
    bar.querySelector('#btn-generate').addEventListener('click', () => {
        hideRetryBar();
        const count = State.branchCount || 1;
        _triggerParallelGenerate(count);
    });
    container.appendChild(bar);
    scrollToBottom();
}

function _modelsMatch(a, b) {
    if (!a || !b) return false;
    const norm = (s) => s.toLowerCase()
        .replace(/\.gguf$/i, '')
        .replace(/[:\-_.]/g, '');
    const na = norm(a);
    const nb = norm(b);
    return na === nb || na.includes(nb) || nb.includes(na);
}

function _agentKindForModel(model, conv) {
    // Persisted model route takes precedence over the current conversation.
    // Runtime attestation remains the stronger evidence where available.
    const route = EXOModelRegistry.resolve(model);
    if (route.mode) return route.mode;
    const mode = (conv?.mode || '').toLowerCase();
    if (['claude', 'gemini', 'codex', 'goose', 'local', 'hermes', 'umans', 'openrouter', 'dream'].includes(mode)) return mode;
    return '';
}

function _assistantRoleLabelForModel(model, conv) {
    if ((conv?.mode || '').toLowerCase() === 'weave') return getCharacterName();
    const agentKind = _agentKindForModel(model, conv);
    if (agentKind === 'gemini') return 'Gemini';
    if (agentKind === 'codex') return 'Codex';
    if (agentKind === 'goose') return 'Goose';
    if (agentKind === 'openrouter') return 'OpenRouter';
    if (agentKind === 'umans') return 'Umans';
    if (agentKind === 'local') return 'Braid';
    if (agentKind === 'hermes') return 'Hermes';
    if (agentKind === 'dream') return 'Dream Space';
    if (agentKind === 'claude') return 'Claude';
    return getCharacterName();
}

function createMessageElement(msg, cost, elapsed) {
    // Fall back to the persisted generation duration when the caller didn't
    // supply a live timer value — so the M:SS badge survives a page refresh.
    if ((elapsed === undefined || elapsed === null || elapsed === 0) && msg && msg.generation_ms) {
        elapsed = Math.round(msg.generation_ms / 1000);
    }
    const isErrorMsg = msg.role === 'assistant' && msg.content?.startsWith('[Error:');
    const hasBlocks = Array.isArray(msg.content_blocks) ? msg.content_blocks.length > 0 : (typeof msg.content_blocks === 'string' && msg.content_blocks.length > 2);
    const isDraft = msg.role === 'assistant' && !msg.content?.trim() && !hasBlocks && !isErrorMsg && (!msg.turn_lifecycle_state || msg.turn_lifecycle_state === 'generating');
    const isDescribeMsg = msg.role === 'system' && typeof msg.content === 'string' && msg.content.trimStart().startsWith('[Image description —');
    // A queued message is one that's been POSTed to the DB (so it has an id and
    // a role) but is waiting for the current stream to end before it generates.
    // It renders with a queue-bar (Edit/Cancel) that calls editQueuedMessage
    // (in-place save). The standard action row below would otherwise also
    // render the ✎ pencil → editMessage → "Send as new branch", which is the
    // wrong affordance for a queued message and surprises the user with a new
    // root branch when they only meant to tweak the queued text. Suppress the
    // standard actions for queued messages; the queue-bar handles editing.
    const isQueued = !!_queuedGeneration && _queuedGeneration.id === msg.id;
    const div = document.createElement('div');
    div.className = `message ${msg.role}${isErrorMsg ? ' message-error' : ''}${isDraft ? ' message-generating' : ''}${isDescribeMsg ? ' message-describing' : ''}`;
    div.dataset.msgId = msg.id;

    const msgModel = (msg.cc_model_used || State.currentConv?.cc_model || '').toLowerCase();
    const agentKind = _agentKindForModel(msgModel, State.currentConv);
    const isClaudeMode = agentKind === 'claude';
    const isLocalMode = agentKind === 'local';
    const isHermesMode = agentKind === 'hermes';
    const isCliAgentMode = ['claude', 'gemini', 'codex', 'goose', 'local', 'hermes', 'umans', 'openrouter'].includes(agentKind);
    const roleLabel = msg.role === 'system' ? 'System'
        : msg.role === 'user' ? 'You'
        : _assistantRoleLabelForModel(msgModel, State.currentConv);
    // Local model tag to show alongside the Braid/Hermes label
    const _lmName = State.currentConv && State.currentConv.local_model;
    let activeModelName = _lmName || State.loadedModel || '';
    if (_lmName && State.loadedModel && _modelsMatch(_lmName, State.loadedModel)) {
        activeModelName = State.loadedModel;
    }
    const localModelTag = (isLocalMode || isHermesMode)
        ? (activeModelName
            ? `<span class="local-model-tag">(${escapeHtml(activeModelName)})</span>`
            : `<span class="local-model-tag">(${isHermesMode ? 'Hermes' : 'local model'})</span>`)
        : '';
    const modelAttestationTag = msg.role === 'assistant'
        ? _renderModelAttestation(msg.model_attestation)
        : '';
    const branchLabelFull = State.branchNames?.[msg.id] || '';
    // Middle-truncate long branch labels so they don't push action buttons off
    // the row (matches tree nodes + breadcrumb).
    const branchLabel = branchLabelFull.length > 14
        ? branchLabelFull.slice(0, Math.ceil(14 / 2) - 1) + '…' + branchLabelFull.slice(-Math.floor(14 / 2))
        : branchLabelFull;

    const isBm = State.bookmarks?.some(b => b.message_id === msg.id);
    const bmBtn = `<button onclick="toggleChatBookmark(${msg.id})" title="${isBm ? 'Remove message from Leaves' : 'Pin message to Leaves'}" aria-label="${isBm ? 'Remove message from Leaves' : 'Pin message to Leaves'}" class="chat-bookmark-btn${isBm ? ' active' : ''}">${isBm ? '⏣' : '⬡'}</button>`;
    let actionsHtml = '';
    if (msg.role === 'system') {
        if (isDescribeMsg) {
            actionsHtml = '<button class="btn-small describe-reroll" onclick="rerollDescribe(State.currentConvId, ' + msg.parent_id + ')" title="Re-describe">&#x21BB;</button>';
        }
    } else if (msg.role === 'assistant') {
        actionsHtml = '<button onclick="regenerateMessage(' + msg.id + ')" title="Regenerate">&#x21BB;</button>' +
            '<button onclick="forkFromMessage(' + msg.id + ')" title="Branch" aria-label="Branch">&#x2325;</button>' +
            '<button onclick="copyMessage(' + msg.id + ')" title="Copy">&#x29C9;</button>' + bmBtn;
    } else if (isQueued) {
        // Queued user message: editing goes through the queue-bar's Edit button
        // (editQueuedMessage → in-place save), NOT the standard ✎ pencil
        // (editMessage → "Send as new branch"). Render no standard actions;
        // the queue-bar appended by the caller is the sole affordance.
        actionsHtml = '';
    } else {
        actionsHtml = '<button onclick="editMessage(' + msg.id + ')" title="Edit">&#x270E;</button>' +
            '<button onclick="forkFromMessage(' + msg.id + ')" title="Branch" aria-label="Branch">&#x2325;</button>' +
            '<button onclick="copyMessage(' + msg.id + ')" title="Copy">&#x29C9;</button>' + bmBtn;
    }

    // Branch indicator (async - will fill in after render)
    const branchPlaceholder = `<span class="branch-slot" data-msg-id="${msg.id}"></span>`;

    // Render content: use content_blocks if available (Claude mode), otherwise plain text
    let contentHtml = '';
    let blocks = null;
    if (msg.content_blocks) {
        try {
            blocks = typeof msg.content_blocks === 'string' ? JSON.parse(msg.content_blocks) : msg.content_blocks;
        } catch { blocks = null; }
    }

    if (isDraft) {
        contentHtml = '<span class="generating-placeholder"><span class="thinking-dots"></span> Generating...</span>'
            + ' <button onclick="cancelGeneration()" title="Cancel and discard partial context" aria-label="Cancel and discard partial context" class="cancel-draft-btn">&#x2298;</button>'
            + ' <button onclick="interruptGeneration()" title="Interrupt and preserve partial context" aria-label="Interrupt and preserve partial context" class="interrupt-draft-btn">&#x23F8;</button>';
    } else if (msg.role === 'system' && typeof msg.content === 'string' && msg.content.trimStart().startsWith('[Image description —')) {
        // Render as describe message
        const modelMatch = msg.content.match(/^\[Image description — ([^\]]+)\]/);
        const modelLabel = modelMatch ? modelMatch[1] : 'vision model';
        const descText = msg.content.replace(/^\[Image description — [^\]]+\]\s*/, '').trim();
        contentHtml = '<div class="describe-label">Describing Image — ' + escapeHtml(modelLabel) + '</div>' +
            '<div class="describe-content">' + formatContent(descText) + '</div>';
        // Set data-parent-msg-id and reroll button for persisted describe messages
        if (msg.parent_id) {
            div.dataset.parentMsgId = msg.parent_id;
        }
    } else if (msg.role === 'system' && typeof msg.content === 'string' && msg.content.trimStart().startsWith('[CC context compactified')) {
        contentHtml = renderCompactMarker(msg.content, msg.id);
    } else if (blocks && blocks.length > 0) {
        contentHtml = renderContentBlocks(blocks);
    } else {
        contentHtml = formatContent(msg.content);
    }

    // Provider failures can retain tool/think blocks but put their terminal
    // diagnostic only in plain content. Do not hide that outcome behind the
    // richer blocks and leave a tool-only turn looking unfinished.
    if (blocks?.length && ['failed', 'partial_failed', 'cancelled', 'interrupted'].includes(msg.turn_lifecycle_state)) {
        const diagnostic = String(msg.content || '').match(/(?:^|\n)\[([^\n]*(?:failed|cancelled|interrupted)[^\n]*)\]\s*$/i);
        const label = diagnostic?.[1] || ({failed:'Turn failed.',partial_failed:'Turn failed before completion. Partial output is preserved.',
            cancelled:'Turn cancelled.',interrupted:'Turn interrupted.'})[msg.turn_lifecycle_state];
        contentHtml += `<p class="turn-terminal-notice" role="status">${escapeHtml(label)}</p>`;
    }

    const timestampHtml = _formatMessageTimestamp(msg);

    // Footer metadata: message timestamp plus assistant generation stats.
    let footerHtml = '';
    {
        const c = cost || {};
        const usd = c.cost_usd || msg.turn_cost_usd || 0;
        const inTok = c.input_tokens || msg.turn_input_tokens || 0;
        const outTok = c.output_tokens || msg.turn_output_tokens || 0;
        const durMs = c.duration_ms || msg.generation_ms || (msg.role === 'assistant' && elapsed ? elapsed * 1000 : 0);
        const firstTokenMs = msg.time_to_first_token_ms;
        const parts = [];
        if (timestampHtml) parts.push(timestampHtml);
        if (inTok || outTok) {
            const tokenParts = [];
            if (inTok) tokenParts.push(`${_fmtTok(inTok)} in`);
            if (outTok) tokenParts.push(`${_fmtTok(outTok)} out`);
            parts.push(tokenParts.join(' / '));
        }
        const telemetry = [];
        if (usd) telemetry.push(`Cost $${usd.toFixed(4)}`);
        if (msg.role === 'assistant' && Number.isFinite(firstTokenMs) && firstTokenMs >= 0) {
            const firstSecs = Math.round(firstTokenMs / 1000);
            const firstLabel = firstSecs >= 60
                ? _formatElapsed(firstSecs)
                : `${(firstTokenMs / 1000).toFixed(1)}s`;
            telemetry.push(`<span class="first-token-time" title="Time from launch to first observed provider output (response arrival for non-streaming models)">First response ${firstLabel}</span>`);
        }
        if (msg.role === 'assistant' && durMs) {
            const secs = Math.round(durMs / 1000);
            let genTime = '';
            if (secs >= 60) {
                genTime = _formatElapsed(secs);
            } else {
                genTime = `${(durMs/1000).toFixed(1)}s`;
            }
            telemetry.push(`<span class="generation-time" title="Time spent generating this reply">Generation time ${genTime}</span>`);
        }
        if (parts.length || telemetry.length) {
            const compact = parts.join(' · ');
            footerHtml = telemetry.length
                ? `<details class="message-footer message-telemetry"><summary aria-label="Message time, tokens and generation details">${compact || 'Generation details'}</summary><div class="message-telemetry-panel">${telemetry.join(' · ')}</div></details>`
                : `<div class="message-footer">${compact}</div>`;
        }
    }

    let imgHtml = '';
    if (msg.image_path) {
        const paths = parseImagePaths(msg.image_path);
        if (paths.length > 0) {
            const imageExts = ['.png', '.jpg', '.jpeg', '.gif', '.webp'];
            let descMap = {};
            if (msg.image_alt) {
                try { descMap = JSON.parse(msg.image_alt) || {}; } catch { descMap = {}; }
            }
            imgHtml = '<div class="message-images">' +
                paths.map(p => {
                    const filename = p.split(/[\\/]/).pop();
                    const ext = '.' + filename.split('.').pop().toLowerCase();
                    if (imageExts.includes(ext)) {
                        const desc = descMap[filename];
                        const descHtml = desc
                            ? `<figcaption class="image-description" title="From vision model">${escapeHtml(desc)}</figcaption>`
                            : '';
                        return `<figure class="message-image-figure" data-img-name="${escapeHtml(filename)}"><img class="message-image" src="/uploads/${escapeHtml(filename)}" alt="Attached image">${descHtml}</figure>`;
                    }
                    return '<a class="message-file-attach" href="/uploads/' + filename + '" target="_blank" title="' + escapeHtml(filename) + '">&#128196; ' + escapeHtml(ext.toUpperCase().slice(1)) + ' file attached</a>';
                }).join('') +
                '</div>';
        }
    }

    // Show project images the reply itself names. Tool inputs/outputs (folder
    // listings, greps, file reads) are deliberately not scanned: an image that
    // merely appears in a listing was not produced by the turn.
    let projectImgHtml = '';
    if (msg.role === 'assistant' && isCliAgentMode && State.currentConv && msg.chat_items == null) {
        const imgEntries = replyImageEntries(msg.content || '', State.currentConv.project_dir, State.currentConvId);
        const figure = e =>
            `<figure class="detected-image-figure">` +
            `<img class="generated-image" src="${e.url}" alt="${escapeHtml(e.name)}" loading="lazy" onerror="this.closest('figure').remove()">` +
            `<figcaption>${escapeHtml(e.name)}</figcaption></figure>`;
        const VISIBLE = 6;
        if (imgEntries.length > 0) {
            const extra = imgEntries.slice(VISIBLE);
            projectImgHtml = '<div class="detected-images">' + imgEntries.slice(0, VISIBLE).map(figure).join('') +
                (extra.length ? `<details class="detected-images-more"><summary>${extra.length} more image${extra.length === 1 ? '' : 's'}</summary>` +
                    extra.map(figure).join('') + '</details>' : '') + '</div>';
        }
    }

    div.innerHTML = '<div class="message-header">' +
        '<div class="message-header-left">' +
            '<span class="message-role">' + escapeHtml(roleLabel) + '</span>' +
            (localModelTag ? `<span class="local-model-label">${localModelTag}</span>` : '') + modelAttestationTag + (branchLabel ? '<span class="message-branch-label" title="' + escapeHtml(branchLabelFull) + ' — click to copy branch path">' + escapeHtml(branchLabel) + '</span>' : '') +
        '</div>' +
        '<div class="message-actions">' + branchPlaceholder + actionsHtml + '</div>' +
        '</div>' +
        '<div class="message-content">' + contentHtml + '</div>' +
        imgHtml + projectImgHtml + renderChatItems(msg.chat_items) + footerHtml;

    // Click-to-preview for detected project images
    div.querySelectorAll('.detected-images .generated-image').forEach(img => {
        img.addEventListener('click', () => {
            const body = document.getElementById('preview-modal-body');
            body.innerHTML = '<img src="' + img.src + '" style="max-width:100%;max-height:80vh;">';
            document.getElementById('modal-preview').classList.remove('hidden');
        });
    });

    // Load sibling info for branch indicator
    loadBranchIndicator(msg.id, div.querySelector('.branch-slot'));

    // Click-to-copy branch label
    const branchEl = div.querySelector('.message-branch-label');
    if (branchEl) {
        branchEl.setAttribute('role', 'button');
        branchEl.setAttribute('tabindex', '0');
        branchEl.title = 'Copy full message path to the address bar';
        branchEl.setAttribute('aria-label', 'Copy full message path: ' + branchLabelFull);
        const copyPath = async () => {
            await copyMessageBranchPath(msg.id, msg.conversation_id || State.currentConvId, branchEl);
            branchEl.classList.add('path-copied');
            branchEl.setAttribute('aria-label', 'Full message path copied to the address bar');
            setTimeout(() => {
                branchEl.classList.remove('path-copied');
                branchEl.setAttribute('aria-label', 'Copy full message path: ' + branchLabelFull);
            }, 2000);
        };
        branchEl.addEventListener('click', copyPath);
        branchEl.addEventListener('keydown', event => {
            if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); copyPath(); }
        });
    }

    if (isQueued) {
        div.classList.add('queued-message');
        const queueStatus = _queuedGeneration?.queue_status || msg.queue_status || 'queued';
        msg.queue_status = queueStatus;
        if (queueStatus === 'paused') div.classList.add('paused-queued-message');
        div.appendChild(createQueueActions(msg));
    }

    return div;
}

function _parseMaybeJson(value) {
    if (!value) return null;
    if (typeof value !== 'string') return value;
    try { return JSON.parse(value); } catch { return null; }
}

function renderUnifiedDiff(diffText, fallbackTitle) {
    if (!diffText) return null;
    const lines = String(diffText).split('\n');
    let diffHtml = '<div class="diff-header">' + escapeHtml(fallbackTitle || 'Codex diff') + '</div>';
    diffHtml += '<div class="diff-body">';
    for (const line of lines) {
        const cls = line.startsWith('+') && !line.startsWith('+++') ? 'diff-add'
            : line.startsWith('-') && !line.startsWith('---') ? 'diff-remove'
            : line.startsWith('@@') ? 'diff-hunk'
            : 'diff-context';
        diffHtml += '<div class="' + cls + '">' + escapeHtml(line || ' ') + '</div>';
    }
    diffHtml += '</div>';
    return diffHtml;
}

function renderFileChangeSummary(changes) {
    if (!Array.isArray(changes) || !changes.length) return null;
    let diffHtml = '<div class="diff-header">Codex file changes</div>';
    diffHtml += '<div class="diff-body">';
    for (const change of changes) {
        const path = change.path || change.file || change.filePath || 'unknown file';
        const kind = change.kind || change.type || change.status || 'change';
        diffHtml += '<div class="diff-context">' + escapeHtml(kind + ': ' + path) + '</div>';
    }
    diffHtml += '</div>';
    return diffHtml;
}

function renderEditDiff(inputJson, resultJson) {
    try {
        const inputData = _parseMaybeJson(inputJson) || {};
        const resultData = _parseMaybeJson(resultJson) || {};
        const data = resultData.kind === 'codex_diff' ? resultData : inputData;
        if (data.kind === 'codex_diff') {
            return renderUnifiedDiff(data.diff, data.turnId || data.threadId || 'Codex diff')
                || renderFileChangeSummary(data.files)
                || '<div class="tool-block-input">' + escapeHtml(JSON.stringify(data, null, 2)) + '</div>';
        }
        if (Array.isArray(data.changes) || Array.isArray(data.fileChanges)) {
            return renderFileChangeSummary(data.changes || data.fileChanges);
        }
        const filePath = data.file_path || 'unknown file';
        const oldStr = data.old_string || '';
        const newStr = data.new_string || '';
        if (!oldStr && !newStr) return null;
        const oldLines = oldStr.split('\n');
        const newLines = newStr.split('\n');
        let diffHtml = '<div class="diff-header">' + escapeHtml(filePath) + '</div>';
        diffHtml += '<div class="diff-body">';
        for (const line of oldLines) {
            diffHtml += '<div class="diff-remove">- ' + escapeHtml(line) + '</div>';
        }
        for (const line of newLines) {
            diffHtml += '<div class="diff-add">+ ' + escapeHtml(line) + '</div>';
        }
        diffHtml += '</div>';
        return diffHtml;
    } catch {
        return null;
    }
}

function detectLangFromPath(filePath) {
    if (!filePath) return '';
    const ext = filePath.split('.').pop().toLowerCase();
    const map = {
        js:'javascript', ts:'typescript', jsx:'jsx', tsx:'tsx',
        py:'python', rb:'ruby', rs:'rust', go:'go', java:'java',
        c:'c', cpp:'cpp', h:'c', hpp:'cpp', cs:'csharp', php:'php',
        html:'html', htm:'html', css:'css', scss:'scss',
        json:'json', yaml:'yaml', yml:'yaml', toml:'toml', xml:'xml', svg:'svg',
        md:'markdown', sh:'bash', bash:'bash', sql:'sql',
    };
    return map[ext] || ext;
}

function renderToolBody(toolName, input, resultDisplay) {
    let inputHtml = '', resultHtml = '';
    try {
        const p = input ? JSON.parse(input) : {};
        switch (toolName) {
            case 'Write': {
                const fp = p.file_path || '';
                const content = p.content || '';
                const lang = detectLangFromPath(fp);
                inputHtml = '<div class="tool-file-path">' + escapeHtml(fp) + '</div>';
                if (content) inputHtml += '<div class="tool-code-content">' + formatContent('```' + lang + '\n' + content + '\n```') + '</div>';
                break;
            }
            case 'Read': {
                const fp = p.file_path || '';
                inputHtml = '<div class="tool-file-path">' + escapeHtml(fp) + '</div>';
                if (resultDisplay) {
                    const lang = detectLangFromPath(fp);
                    resultHtml = '<div class="tool-code-content">' + formatContent('```' + lang + '\n' + resultDisplay + '\n```') + '</div>';
                }
                break;
            }
            case 'Bash': {
                const cmd = p.command || '';
                if (cmd) inputHtml = '<div class="tool-code-content">' + formatContent('```bash\n' + cmd + '\n```') + '</div>';
                if (resultDisplay) resultHtml = '<div class="tool-block-result"><pre class="tool-output">' + escapeHtml(resultDisplay) + '</pre></div>';
                break;
            }
            default: {
                if (input) inputHtml = '<div class="tool-block-input">' + escapeHtml(input) + '</div>';
                if (resultDisplay) resultHtml = '<div class="tool-block-result">' + escapeHtml(resultDisplay) + '</div>';
            }
        }
    } catch {
        if (input) inputHtml = '<div class="tool-block-input">' + escapeHtml(input) + '</div>';
        if (resultDisplay) resultHtml = '<div class="tool-block-result">' + escapeHtml(resultDisplay) + '</div>';
    }
    return inputHtml + resultHtml;
}

/** Inline figure for an image a tool returned (served from the project). */
function toolImageFigureHtml(url) {
    if (!url || !String(url).startsWith('/api/conversations/')) return '';
    let name = 'image';
    try { name = decodeURIComponent(String(url).split('path=').pop() || '').split(/[/\\]/).pop() || 'image'; } catch { /* keep default */ }
    return '<div class="tool-image-result"><figure class="detected-image-figure">' +
        '<img class="generated-image" src="' + escapeHtml(url) + '" alt="' + escapeHtml(name) + '" loading="lazy" ' +
        'onerror="this.closest(\'figure\').remove()">' +
        '<figcaption>' + escapeHtml(name) + '</figcaption></figure></div>';
}

function parseChatItems(value) {
    try {
        const items = typeof value === 'string' ? JSON.parse(value) : value;
        return Array.isArray(items) ? items : [];
    } catch { return []; }
}

function renderChatItem(item) {
    // Public metadata supplies only Loom URLs; never trust a tool-supplied href.
    if (!/^[0-9a-f-]{36}$/i.test(item?.id || '')) return '';
    const url = `/api/chat-items/${item.id}/file`;
    const label = escapeHtml(item.label || 'File');
    const caption = item.caption ? `<p>${escapeHtml(item.caption)}</p>` : '';
    let preview = '';
    if (item.mode === 'attach' && item.kind === 'image') {
        preview = `<a href="${url}" target="_blank" rel="noopener"><img class="generated-image" src="${url}" alt="${label}" loading="lazy"></a>`;
    } else if (item.mode === 'attach' && item.kind === 'markdown') {
        preview = `<div class="chat-item-markdown">${formatContent(item.content || '')}</div>`;
    } else if (item.mode === 'attach' && item.kind === 'code') {
        // Escape the whole text so snippets cannot close a Markdown fence or run HTML.
        preview = `<pre><code>${escapeHtml(item.content || '')}</code></pre>`;
    }
    const description = item.mode === 'link' ? 'Current file' : 'Attached snapshot';
    return `<section class="chat-item" data-chat-item-id="${item.id}">${preview}${caption}` +
        `<a class="message-file-attach" href="${url}?download=true">${label}</a> <small>${description}${item.language ? ' · ' + escapeHtml(item.language) : ''}</small></section>`;
}

function renderChatItems(value) {
    const items = parseChatItems(value);
    return items.length ? `<div class="chat-presentations">${items.map(renderChatItem).join('')}</div>` : '';
}

function renderContentBlocks(blocks) {
    // Render chronologically: text / tool / text / tool. A sentence split by
    // a tool call shows as text-before, tool, text-after on consecutive lines
    // — the tool sits visually within the paragraph rather than being banished
    // to the bottom of the message.
    let html = '';
    let textBuf = '';
    const flushText = () => {
        if (textBuf.trim()) html += formatContent(textBuf);
        textBuf = '';
    };
    const renderToolBlockHtml = (block) => {
        const name = block.name || 'Tool';
        const rawInput = (block.input || '').trim();
        const input = rawInput.length > 3000 ? rawInput.substring(0, 3000) + '\n... (truncated)' : rawInput;
        const result = (block.result || '').trim();
        const resultDisplay = result.length > 2000 ? result.substring(0, 2000) + '\n... (truncated)' : result;
        const isEdit = (name === 'Edit');
        const diffHtml = isEdit ? renderEditDiff(input, result) : null;
        const bodyHtml = diffHtml || renderToolBody(name, input, resultDisplay);
        // A tool-made image (screenshot) stays visible; a Read of an existing
        // image stays inside the collapsed step.
        const imageHtml = toolImageFigureHtml(block.image_url);
        const open = imageHtml && name !== 'Read';
        return '<div class="tool-block' + (open ? ' expanded' : '') + '">' +
            '<div class="tool-block-header" data-tool-toggle>' +
                '<span class="tool-name">' + escapeHtml(name) + '</span>' +
                '<span class="tool-toggle">' + (open ? '&#9662;' : '&#9656;') + '</span>' +
            '</div>' +
            '<div class="tool-block-body">' + bodyHtml + imageHtml + '</div>' +
        '</div>';
    };
    for (const block of blocks) {
        if (block.type === 'text') {
            if (block.text) textBuf += block.text;
            continue;
        }
        if (block.type === 'tool_use') {
            flushText();
            html += renderToolBlockHtml(block);
            continue;
        }
        if (block.type === 'thinking') {
            flushText();
            const text = (block.text || '').trim();
            if (text) {
                html += '<div class="cc-thinking">' +
                    '<div class="cc-thinking-toggle">Thinking...</div>' +
                    '<div class="cc-thinking-content" style="display:none;">' + escapeHtml(text) + '</div>' +
                '</div>';
            }
            continue;
        }
    }
    flushText();
    return html;
}

function loadBranchIndicator(msgId, slot) {
    // Compute siblings from State.treeData (no API call needed)
    if (!State.treeData) return;
    const msg = State.treeData.find(n => n.id === msgId);
    if (!msg) return;

    // Find siblings: messages with the same parent_id
    const parentId = msg.parent_id;
    if (parentId == null) return; // Independent roots are separate threads.
    const siblings = State.treeData.filter(n => _sameMessageId(n.parent_id, parentId));
    if (siblings.length <= 1) return;

    siblings.sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
    const currentIndex = siblings.findIndex(s => s.id === msgId);
    const total = siblings.length;
    const siblingIds = siblings.map(s => s.id);

    const indicator = document.createElement('span');
    indicator.className = 'branch-indicator';
    indicator.title = `Response ${currentIndex + 1} of ${total}`;
    indicator.innerHTML = `
        <button ${currentIndex === 0 ? 'disabled' : ''}>‹</button>
        <span>${currentIndex + 1}/${total}</span>
        <button ${currentIndex === total - 1 ? 'disabled' : ''}>›</button>
    `;

    const buttons = indicator.querySelectorAll('button');
    buttons[0].setAttribute('aria-label', 'Previous response');
    buttons[1].setAttribute('aria-label', 'Next response');
    buttons[0].addEventListener('click', async (e) => {
        e.stopPropagation();
        if (currentIndex > 0) await switchToBranch(siblingIds[currentIndex - 1]);
    });
    buttons[1].addEventListener('click', async (e) => {
        e.stopPropagation();
        if (currentIndex < total - 1) await switchToBranch(siblingIds[currentIndex + 1]);
    });

    slot.replaceWith(indicator);
}

function _reconcileNavigatedBranch(branch, convId, launchRunIds = []) {
    if (!_sameConvId(State.currentConvId, convId)) return branch;
    // A branch GET can precede stream_end but arrive afterward. Preserve newer
    // exact-message evidence; never pull a different/sibling run into this view.
    const local = new Map(State.messages.map(message => [String(message.id), message]));
    if (State._liveStreamDraft) local.set(String(State._liveStreamDraft.id), State._liveStreamDraft);
    const terminalIds = new Set([..._recentTerminalRuns]
        .filter(([key]) => key.startsWith(`${convId}/`)).map(([, id]) => String(id)));
    const merged = branch.map(message => {
        const newer = local.get(String(message.id));
        const landed = message.generation_ms != null
            || ['completed', 'failed', 'partial_failed', 'cancelled', 'interrupted'].includes(message.turn_lifecycle_state);
        const accepted = LoomCoordination.viewStep(
            terminalIds.has(String(message.id)), true, landed,
        )[1];
        return newer && (!accepted || (!landed
            && _sameMessageId(State._followingDraftMsgId, message.id))) ? newer : message;
    });
    // Only a send/edit acknowledgement may follow its newly allocated child.
    // Ordinary explicit navigation must retain its exact requested boundary.
    for (const runId of launchRunIds) {
        const draftId = _recentTerminalRuns.get(_terminalRunKey(convId, runId))
            ?? (runId === `gen:${State._followingGenId}` ? State._followingDraftMsgId : null);
        const child = local.get(String(draftId));
        if (child && _sameMessageId(child.parent_id, merged.at(-1)?.id)) merged.push(child);
    }
    return merged;
}

async function switchToBranch(leafId, scrollToMsgId, options = {}) {
    const convId = State.currentConvId;
    const switchSeq = (State._branchSwitchSeq || 0) + 1;
    State._branchSwitchSeq = switchSeq;
    const composerInput = document.getElementById('user-input');
    const inputAtStart = composerInput ? composerInput.value : '';
    saveComposerDraft();
    try {
        if (typeof showLoading === 'function') showLoading();
        // Ordinary navigation follows the clicked branch to its newest leaf.
        // Exact navigation is used by tree Run so its generation parent cannot
        // drift to a newer descendant created by another agent.
        let [branch, treeData] = await Promise.all([
            options.exact
                ? API.get(`/api/conversations/${convId}/branch/${leafId}`)
                : API.post(`/api/conversations/${convId}/switch-branch/${leafId}`),
            API.get(`/api/conversations/${convId}/tree`),
        ]);
        if (State._cursorSyncPromise) await State._cursorSyncPromise;
        if (State._branchSwitchSeq !== switchSeq || !_sameConvId(State.currentConvId, convId)) return;
        branch = _reconcileNavigatedBranch(branch, convId, options.launchRunIds);
        let loadedLeaf = branch[branch.length - 1];
        if (!loadedLeaf) throw new Error('The selected branch has no messages');
        await selectServerLoomCursor(convId, loadedLeaf.id, [...treeData, ...branch]);
        if (State._branchSwitchSeq !== switchSeq || !_sameConvId(State.currentConvId, convId)) return;
        branch = _reconcileNavigatedBranch(branch, convId, options.launchRunIds);
        loadedLeaf = branch[branch.length - 1];
        // A run subscription owns output, not navigation. Detach the old
        // renderer when this explicit branch no longer contains its draft.
        if (State._followingGenId != null && !branch.some(message =>
            _sameMessageId(message.id, State._followingDraftMsgId)
        )) {
            _resetStreamState();
            removeStreamingMessage();
            setBrowserTabState('idle');
        }
        // Explicit branch navigation supersedes an inline edit. Background
        // message reconciliation never reaches this path and remains deferred.
        _releaseInlineEditRenderGuard();
        State.messages = branch;
        if (typeof _clearExplicitCCModel === 'function') _clearExplicitCCModel();
        State.treeData = treeData;
        markBranchActivitySeen(State.currentConvId, branch.map(message => message.id));
        if (loadedLeaf) rememberConversationLeaf(State.currentConvId, loadedLeaf.id);
        if (composerInput && composerInput.value !== inputAtStart) {
            saveComposerDraft(State.currentConvId, State.loomCursor);
        } else {
            restoreComposerDraft(State.currentConvId, State.loomCursor);
        }
        if (State.ws?.readyState === WebSocket.OPEN) {
            State.ws.send(JSON.stringify({ action: 'request_snapshot' }));
        }
        hideRetryBar();
        hideGenStatus();
        renderMessages();
        renderTree();
        // Navigation must paint without waiting for optional model discovery.
        if (typeof updateInlineCCControls === 'function' && State.currentConv) {
            void updateInlineCCControls(State.currentConv).catch(() => {});
        }

        // Scroll to the clicked message, or bottom if not specified
        const targetId = scrollToMsgId || leafId;
        const renderMsgs = State.messages;
        const targetIdx = renderMsgs.findIndex(m => m.id === targetId);

        if (targetIdx >= 0 && targetIdx < VIRTUAL_SCROLL.renderedStart) {
            // Message is above the virtual scroll window — load from that point
            const container = document.getElementById('messages');
            const scrollParent = document.getElementById('messages-container');
            const sentinel = document.getElementById('scroll-sentinel');
            const refNode = sentinel ? sentinel.nextSibling : container.firstChild;
            const fragment = document.createDocumentFragment();
            for (let i = targetIdx; i < VIRTUAL_SCROLL.renderedStart; i++) {
                fragment.appendChild(createMessageElement(renderMsgs[i]));
            }
            container.insertBefore(fragment, refNode);
            VIRTUAL_SCROLL.renderedStart = targetIdx;
            if (sentinel) sentinel.textContent = `↑ ${targetIdx} older messages`;
            if (targetIdx <= 0 && sentinel) sentinel.remove();
        }

        // Double rAF: first frame triggers reflow, second frame has correct layout
        requestAnimationFrame(() => requestAnimationFrame(() => {
            const targetEl = document.querySelector(`.message[data-msg-id="${targetId}"]`);
            if (targetEl) {
                targetEl.scrollIntoView({ behavior: 'instant', block: 'center' });
                targetEl.classList.add('message-highlight');
                setTimeout(() => targetEl.classList.remove('message-highlight'), 2000);
            } else {
                scrollToBottom();
            }
        }));
    } catch (err) {
        showToast('Failed to switch branch', 'error');
    } finally {
        if (typeof hideLoading === 'function') hideLoading();
    }
}

function getCharacterName() {
    if (!State.currentConvId) return 'Assistant';
    const conv = State.conversations.find(c => c.id === State.currentConvId);
    if (!conv) return 'Assistant';
    if ((conv.mode || 'weave').toLowerCase() === 'weave') {
        if (conv.character_id) {
            const char = State.characters.find(c => c.id === conv.character_id);
            return char ? char.name : 'Assistant';
        }
        return conv.local_model || 'Assistant';
    }
    const ccModel = conv.cc_model || '';
    const agentKind = _agentKindForModel(ccModel || conv.local_model || '', conv);
    if (agentKind === 'codex') return 'Codex';
    if (agentKind === 'gemini') return 'Gemini';
    if (agentKind === 'local') return conv.local_model || 'Local';
    if (agentKind === 'hermes') return conv.local_model || 'Hermes';
    if (agentKind === 'claude') return 'Claude';
    if (!conv.character_id) {
        // Weave/OODA without a character attached: show the model name (mirrors
        // Braid's behavior). Falls through to "Assistant" if nothing's set.
        if (conv.mode === 'weave' && conv.local_model) return conv.local_model;
        return 'Assistant';
    }
    const char = State.characters.find(c => c.id === conv.character_id);
    return char ? char.name : 'Assistant';
}

// Configure marked for chat rendering
if (typeof marked !== 'undefined') {
    const renderer = new marked.Renderer();
    // Force all links to open in new tab via DOMPurify hook (works with any marked version)
    if (typeof DOMPurify !== 'undefined') {
        DOMPurify.addHook('afterSanitizeAttributes', function(node) {
            if (node.tagName === 'A' && node.getAttribute('href')) {
                node.setAttribute('target', '_blank');
                node.setAttribute('rel', 'noopener noreferrer');
            }
        });
    }
    // Override code renderer to add toolbar with copy/preview buttons
    renderer.code = function({ text, lang, escaped }) {
        const safeLang = lang ? escapeHtml(lang.match(/^\S*/)?.[0] || '') : '';
        const langClass = safeLang ? ' class="language-' + safeLang + '"' : '';
        const isPreviewable = safeLang && ['html', 'svg', 'htm'].includes(safeLang.toLowerCase());
        const code = text.replace(/\n$/, '') + '\n';
        const codeHtml = escaped ? code : escapeHtml(code);

        let toolbar = '<div class="code-toolbar">';
        if (safeLang) toolbar += '<span class="code-lang-label">' + safeLang + '</span>';
        else toolbar += '<span class="code-lang-label"></span>';
        toolbar += '<div class="code-toolbar-actions">';
        if (isPreviewable) toolbar += '<button class="code-action-btn" data-code-action="preview" title="Preview">Preview</button>';
        toolbar += '<button class="code-action-btn" data-code-action="copy" title="Copy code">Copy</button>';
        toolbar += '</div></div>';

        return '<div class="code-block-wrapper">' + toolbar +
            '<pre><code' + langClass + '>' + codeHtml + '</code></pre></div>\n';
    };
    // Harnesses cite files as markdown links whose targets are local paths
    // (C:/…, file:///G:/…, /mnt/c/…, /G:/…, <C:\…>, relative files). As web
    // links they open a broken tab, so render them as copyable file chips.
    const defaultLink = marked.Renderer.prototype.link;
    renderer.link = function(token) {
        const ref = localFileRef(token.href);
        if (!ref) return defaultLink.call(this, token);
        // A label that is itself inline code would nest <code> in the chip.
        let label = this.parser.parseInline(token.tokens || []).replace(/^<code>([\s\S]*)<\/code>$/, '$1');
        if (ref.line && !label.includes(ref.line)) label += escapeHtml(ref.line);
        return '<code class="file-ref" role="button" tabindex="0" data-file-path="' + escapeHtml(ref.path) +
            '" title="' + escapeHtml(ref.path + ref.line) + ' (click to copy)">' + label + '</code>';
    };
    marked.setOptions({ breaks: true, gfm: true, renderer });
}

/** Project images named in reply text, as served-file entries (deduped by path). */
function replyImageEntries(text, projectDir, convId) {
    const root = String(projectDir || '').replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
    const seen = new Map();
    const isImage = s => /\.(?:png|jpe?g|gif|webp)(?::\d+)?$/i.test(s.trim());
    const body = String(text);
    // Paths may contain spaces inside `code spans` and (link targets);
    // elsewhere only space-free tokens count, so prose is never swallowed.
    const spanPattern = /`([^`\n]+)`|\]\(<?([^)>\n]+)>?\)/g;
    const candidates = [
        ...[...body.matchAll(spanPattern)].map(m => m[1] || m[2]),
        ...(body.replace(spanPattern, ' ')
            .match(/(?:[A-Za-z]:)?[\w/\\.-]*[\w-]+\.(?:png|jpe?g|gif|webp)\b/gi) || []),
    ].filter(isImage);
    for (const raw of candidates) {
        let norm = raw.trim().replace(/\\/g, '/');
        const ref = localFileRef(norm);
        if (ref) norm = ref.path.replace(/\\/g, '/');
        if (/^[A-Za-z]:\//.test(norm) || norm.startsWith('/')) {
            // Absolute paths are shown only when they sit inside the project.
            if (!root || !norm.toLowerCase().startsWith(root + '/')) continue;
            norm = norm.slice(root.length + 1);
        }
        norm = norm.replace(/^\.\//, '');
        if (!norm || norm.split('/').includes('..')) continue;
        const key = norm.toLowerCase();
        if (!seen.has(key)) {
            seen.set(key, {
                url: `/api/conversations/${convId}/file?path=${encodeURIComponent(norm)}`,
                name: norm.split('/').pop(),
            });
        }
    }
    return [...seen.values()];
}

/** Parse a markdown link target as a local file reference, or null for web links. */
function localFileRef(href) {
    let h = String(href || '').trim().replace(/^<|>$/g, '');
    if (!h || /^(https?:|mailto:|tel:|#)/i.test(h)) return null;
    try { h = decodeURI(h); } catch { /* keep raw */ }
    h = h.replace(/^file:\/\/\/?/i, '');
    let m = h.match(/^\/mnt\/([a-z])\/(.*)$/i);
    if (m) h = m[1].toUpperCase() + ':/' + m[2];
    m = h.match(/^\/([a-z]):[\\/](.*)$/i);
    if (m) h = m[1].toUpperCase() + ':/' + m[2];
    const absolute = /^[a-z]:[\\/]/i.test(h) || h.startsWith('/') || h.startsWith('\\\\');
    if (!absolute && /^[a-z][a-z0-9+.-]*:/i.test(h)) return null;  // another URL scheme
    let line = '';
    const lineMatch = h.match(/(?::(\d+)(?:-(\d+))?|#L?(\d+)(?:-L?(\d+))?)$/i);
    if (lineMatch) {
        const start = lineMatch[1] || lineMatch[3];
        const end = lineMatch[2] || lineMatch[4];
        line = ':' + start + (end ? '-' + end : '');
        h = h.slice(0, lineMatch.index);
    }
    if (!absolute && !/[\\/]/.test(h) && !/\.[\w-]{1,8}$/.test(h)) return null;
    return { path: h, line };
}

function renderCompactMarker(content, msgId) {
    // Marker body looks like:
    //   [CC context compactified (handoff → <target>) — N tokens before]
    //   \n\n---\nPreviously:\n<summary>
    // Header is the first line. Everything after the "Previously:" label (or
    // an empty fallback until the summary is patched in) goes in a default-
    // collapsed block.
    const text = (content || '').trim();
    const firstNl = text.indexOf('\n');
    const header = firstNl === -1 ? text : text.slice(0, firstNl);
    let summary = '';
    const marker = text.indexOf('Previously:');
    if (marker !== -1) {
        summary = text.slice(marker + 'Previously:'.length).trim();
    } else if (firstNl !== -1) {
        const rest = text.slice(firstNl + 1).replace(/^[-\s]+/, '').trim();
        if (rest) summary = rest;
    }
    const summaryHtml = summary ? formatContent(summary) : '<em class="compact-summary-pending">Summary pending…</em>';
    return (
        '<div class="compact-marker" data-msg-id="' + msgId + '">' +
            '<div class="compact-marker-header">' +
                '<span class="compact-marker-text">' + escapeHtml(header) + '</span>' +
            '</div>' +
            '<div class="compact-marker-previously">' +
                '<button class="compact-marker-toggle" onclick="this.parentElement.classList.toggle(\'expanded\')">' +
                    '<span class="compact-marker-arrow">▸</span> Previously' +
                '</button>' +
                '<div class="compact-marker-summary">' + summaryHtml + '</div>' +
            '</div>' +
        '</div>'
    );
}

/** Format elapsed seconds: <60s shows seconds, >=60s shows MM:SS, >=3600s shows HH:MM:SS */
function _formatElapsed(seconds) {
    if (seconds < 60) return seconds + 's';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h > 0) return String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
    return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

function _formatMessageTimestamp(msg) {
    if (!msg || !msg.created_at) return '';
    let millis = Number(msg.created_at);
    if (!Number.isFinite(millis)) return '';
    millis = millis > 1e12 ? millis : millis * 1000;
    if (msg.role === 'assistant' && msg.generation_ms) {
        millis += Number(msg.generation_ms) || 0;
    }
    const date = new Date(millis);
    if (Number.isNaN(date.getTime())) return '';
    const label = msg.role === 'assistant' && msg.generation_ms ? 'Landed' : 'Sent';
    const text = date.toLocaleString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
    });
    const full = date.toLocaleString(undefined, {
        weekday: 'short',
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
        second: '2-digit',
        timeZoneName: 'short',
    });
    return `<time class="message-timestamp" datetime="${date.toISOString()}" title="${label} ${escapeHtml(full)}">${escapeHtml(text)}</time>`;
}

const MARKDOWN_SANITIZE_CONFIG = {
    ADD_TAGS: ['button'],
    ADD_ATTR: ['data-code-action', 'aria-hidden'],
};

function sanitizeMarkdownHtml(raw) {
    return typeof DOMPurify !== 'undefined'
        ? DOMPurify.sanitize(raw, MARKDOWN_SANITIZE_CONFIG)
        : raw;
}

function renderLatexExpression(source, latex, displayMode) {
    if (typeof katex === 'undefined') return escapeHtml(source);
    try {
        return katex.renderToString(latex.trim(), {
            displayMode,
            throwOnError: false,
            strict: 'ignore',
            output: 'html',
        });
    } catch {
        return escapeHtml(source);
    }
}

function replaceLatexInTextSegment(segment, renderedMath) {
    const mathPattern = /(\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|(^|[^\w\\])\$((?:\\.|[^\n$\\])+?)\$(?![\w]))/g;
    return segment.replace(mathPattern, (match, _all, dollarDisplay, bracketDisplay, parenInline, dollarPrefix, dollarInline) => {
        const displayLatex = dollarDisplay !== undefined ? dollarDisplay : bracketDisplay;
        const inlineLatex = parenInline !== undefined ? parenInline : dollarInline;
        const displayMode = displayLatex !== undefined;
        const latex = displayMode ? displayLatex : inlineLatex;
        if (!latex || !latex.trim()) return match;

        const prefix = dollarInline !== undefined ? dollarPrefix : '';
        const source = dollarInline !== undefined ? '$' + dollarInline + '$' : match;
        const token = 'LOOMMATHPLACEHOLDER' + renderedMath.length + 'END';
        renderedMath.push(renderLatexExpression(source, latex, displayMode));
        return prefix + token;
    });
}

function prepareMarkdownLatex(text, renderedMath) {
    const codePattern = /(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*(?:`|$))/g;
    let out = '';
    let last = 0;
    text.replace(codePattern, (match, _unused, offset) => {
        out += replaceLatexInTextSegment(text.slice(last, offset), renderedMath);
        out += match;
        last = offset + match.length;
        return match;
    });
    out += replaceLatexInTextSegment(text.slice(last), renderedMath);
    return out;
}

function parseMarkdownWithLatex(text) {
    const renderedMath = [];
    const prepared = prepareMarkdownLatex(text, renderedMath);
    let raw = marked.parse(prepared);
    renderedMath.forEach((html, i) => {
        raw = raw.replaceAll('LOOMMATHPLACEHOLDER' + i + 'END', html);
    });
    return sanitizeMarkdownHtml(raw);
}

function formatSystemMessageTags(text) {
    if (!text) return '';
    
    if (!text.includes('<SYSTEM_MESSAGE>') && !text.includes('[Tool Output]:')) {
        return text;
    }
    
    let processed = text;
    
    // Replace [Tool Output]: prefix followed by system message explanation
    processed = processed.replace(/\[Tool Output\]:\s*The following is a <SYSTEM_MESSAGE>[\s\S]*?pay attention to\.?/gi, '');
    
    // Replace standalone [Tool Output]: with a styled label/header
    processed = processed.replace(/\[Tool Output\]:/gi, '<strong class="tool-output-label">Tool Output:</strong>');
    
    // Replace <SYSTEM_MESSAGE>... </SYSTEM_MESSAGE> blocks
    const regex = /<SYSTEM_MESSAGE>([\s\S]*?)<\/SYSTEM_MESSAGE>/g;
    processed = processed.replace(regex, (match, inner) => {
        const msgMatch = inner.match(/\[Message\]\s+timestamp=([^\s]+)\s+sender=([^\s]+)\s+priority=([^\s]+)\s+content=([\s\S]*)/i);
        if (msgMatch) {
            const timestamp = msgMatch[1];
            const sender = msgMatch[2].toUpperCase();
            const priority = msgMatch[3];
            const content = msgMatch[4].trim();
            
            const cleanPriority = priority.replace(/^MESSAGE_PRIORITY_/i, '');
            const priorityClass = cleanPriority.toLowerCase();
            const cleanTime = timestamp.replace('T', ' ').replace('Z', ' UTC');
            
            let icon = 'ℹ️';
            if (priorityClass === 'high') icon = '🚨';
            else if (priorityClass === 'medium') icon = '⚠️';
            else if (sender === 'SYSTEM') icon = '⚙️';
            
            return `<div class="system-message-tag-block priority-${priorityClass}">
                <div class="system-message-tag-header">
                    <span class="system-message-tag-icon">${icon}</span>
                    <span class="system-message-tag-sender">${sender} NOTICE</span>
                    <span class="system-message-tag-priority ${priorityClass}">${cleanPriority}</span>
                    <span class="system-message-tag-time">${cleanTime}</span>
                </div>
                <div class="system-message-tag-content">${formatContent(content)}</div>
            </div>`;
        }
        
        return `<div class="system-message-tag-block">
            <div class="system-message-tag-content">${formatContent(inner.trim())}</div>
        </div>`;
    });
    
    return processed;
}

function formatContent(text) {
    if (!text) return '';
    text = formatSystemMessageTags(text);
    if (typeof marked !== 'undefined') {
        return parseMarkdownWithLatex(text);
    }
    // Fallback if marked not loaded
    let html = escapeHtml(text);
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');
    const paras = html.split(/\n\n+/).filter(p => p.trim());
    if (paras.length === 0) return '';
    return paras.map(p => '<p>' + p.replace(/\n/g, '<br>') + '</p>').join('');
}

// ── Thinking Indicator ──

function showThinkingIndicator() {
    if (!streamingDiv) return;
    // Add thinking indicator without replacing content
    let indicator = streamingDiv.querySelector('.thinking-indicator');
    if (!indicator) {
        indicator = document.createElement('span');
        indicator.className = 'thinking-indicator';
        indicator.innerHTML = '<span class="thinking-dots"></span> Thinking...';
        streamingDiv.querySelector('.message-content').appendChild(indicator);
    }
    scrollToBottom();
}

function hideThinkingIndicator() {
    if (!streamingDiv) return;
    const indicator = streamingDiv.querySelector('.thinking-indicator');
    if (indicator) indicator.remove();
}

// ── Streaming Message ──

let streamingDiv = null;
let _lastSnapshotRequestAt = 0;

function _requestSnapshotIfStreaming(force = false) {
    // Throttle: at most one request every 750ms — server reply ('generation_active')
    // takes one round-trip and triggers _reconstructFromSnapshot which rebuilds the div.
    const now = Date.now();
    if (!force && now - _lastSnapshotRequestAt < 750) return;
    if (!State.ws || State.ws.readyState !== WebSocket.OPEN) return;
    _lastSnapshotRequestAt = now;
    try {
        State.ws.send(JSON.stringify({ action: 'request_snapshot', ..._followedGenerationProbe() }));
    } catch {}
}

let _genTimerInterval = null;
let _loomAnimInterval = null;
const _loomFrames = [
    "╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴",
    "╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴",
    "╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴",
    "╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴",
    "─╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌",
    "─╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌",
    "─╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌",
    "◆╼╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌",
    "╾◆╼╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌",
    "═╾◆╼╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌",
    "══╾◆╼╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌",
    "═══╾◆╼╌╌╌╌╌╌╌╌╌╌╌╌╌╌",
    "════╾◆╼╌╌╌╌╌╌╌╌╌╌╌╌╌",
    "═════╾◆╼╌╌╌╌╌╌╌╌╌╌╌╌",
    "══════╾◆╼╌╌╌╌╌╌╌╌╌╌╌",
    "═══════╾◆╼╌╌╌╌╌╌╌╌╌╌",
    "════════╾◆╼╌╌╌╌╌╌╌╌╌",
    "═════════╾◆╼╌╌╌╌╌╌╌╌",
    "══════════╾◆╼╌╌╌╌╌╌╌",
    "═══════════╾◆╼╌╌╌╌╌╌",
    "════════════╾◆╼╌╌╌╌╌",
    "═════════════╾◆╼╌╌╌╌",
    "══════════════╾◆╼╌╌╌",
    "═══════════════╾◆╼╌╌",
    "════════════════╾◆╼╌",
    "═════════════════╾◆╼",
    "══════════════════╾◆",
    "════════════════════",
    "════════════════════",
    "════════════════════",
    "══════════✂═════════",
    "══════════✂═════════",
    "══════════✂═════════",
    "≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼",
    "∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈",
    "≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼",
    "∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈",
    "≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼",
    "∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈∼≈",
    "────────────────────",
    "╌──────────────────╌",
    "╌╌────────────────╌╌",
    "╌╌╌──────────────╌╌╌",
    "╌╌╌╌────────────╌╌╌╌",
    "╌╌╌╌╌──────────╌╌╌╌╌",
    "╌╌╌╌╌╌────────╌╌╌╌╌╌",
    "╌╌╌╌╌╌╌──────╌╌╌╌╌╌╌",
    "╌╌╌╌╌╌╌╌────╌╌╌╌╌╌╌╌",
    "╌╌╌╌╌╌╌╌╌──╌╌╌╌╌╌╌╌╌",
    "╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌",
    "╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴",
    "╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴",
    "╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴╴",
];

function _fmtTok(n) {
    if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k';
    return String(n);
}

function _startGenTimer() {
    if (!_streamStartTime) _streamStartTime = Date.now();
    if (_genTimerInterval) clearInterval(_genTimerInterval);
    const update = () => {
        if (!streamingDiv || !_streamStartTime) return;
        const secs = Math.max(0, Math.floor((Date.now() - _streamStartTime) / 1000));
        const timerEl = streamingDiv.querySelector('.gen-timer');
        if (timerEl) timerEl.textContent = Math.floor(secs / 60) + ':' + String(secs % 60).padStart(2, '0');
        const tokEl = streamingDiv.querySelector('.gen-token-info');
        if (tokEl && _streamTokenCount > 0 && !tokEl.dataset.hasUsage) {
            tokEl.textContent = '↓' + _fmtTok(_streamTokenCount) + ' · ';
        }
    };
    update();
    _genTimerInterval = setInterval(update, 1000);
    // Start loom animation
    if (_loomAnimInterval) clearInterval(_loomAnimInterval);
    let frame = 0;
    _loomAnimInterval = setInterval(() => {
        if (!streamingDiv) return;
        const el = streamingDiv.querySelector('.loom-anim');
        if (el) el.textContent = _loomFrames[frame];
        frame = (frame + 1) % _loomFrames.length;
    }, 120);
}

function _stopGenTimer() {
    if (_genTimerInterval) { clearInterval(_genTimerInterval); _genTimerInterval = null; }
    if (_loomAnimInterval) { clearInterval(_loomAnimInterval); _loomAnimInterval = null; }
}

function appendStreamingMessage(
    ccModel,
    modelAttestation = null,
    draftMsgId = null,
    sessionContinuity = null,
) {
    const container = document.getElementById('messages');
    streamingDiv = document.createElement('div');
    streamingDiv.className = 'message assistant streaming';
    if (draftMsgId != null) streamingDiv.dataset.msgId = draftMsgId;
    const isWeave = (State.currentConv?.mode || '').toLowerCase() === 'weave';
    const modelForLabel = isWeave
        ? (ccModel || State.currentConv?.local_model || State.currentConv?.cc_model)
        : (ccModel || State.currentConv?.cc_model || State.currentConv?.local_model);
    const label = _assistantRoleLabelForModel(modelForLabel, State.currentConv);
    const initialHarness = isWeave ? 'Weave' : label;
    let initialAttestation = modelAttestation
        ? _parseModelAttestation(modelAttestation)
        : (modelForLabel ? {
            status: 'requested',
            harness: initialHarness,
            requested_model: modelForLabel,
            source: 'loom_ui_selection',
            fallback_allowed: false,
        } : null);
    if (initialAttestation && sessionContinuity) {
        const continuity = typeof sessionContinuity === 'object'
            ? sessionContinuity
            : { status: sessionContinuity };
        initialAttestation = {
            ...initialAttestation,
            session_continuity: continuity.status,
            session_continuity_reason: continuity.reason,
            source_session_id: continuity.source_session_id,
        };
    }
    const stopControls = _pendingLaunch
        ? '<button onclick="cancelGeneration()" title="Cancel launch; keep the user message" aria-label="Cancel launch; keep the user message" class="cancel-draft-btn">&#x2298;</button>'
        : '<button onclick="cancelGeneration()" title="Cancel and discard partial context" aria-label="Cancel and discard partial context" class="cancel-draft-btn">&#x2298;</button>'
          + '<button onclick="interruptGeneration()" title="Interrupt and preserve partial context" aria-label="Interrupt and preserve partial context" class="interrupt-draft-btn">&#x23F8;</button>';
    streamingDiv.innerHTML = '<div class="message-header">' +
        '<div class="message-header-left">' +
            '<span class="message-role">' + escapeHtml(label) + '</span>' +
            '<span class="model-attestation-slot">' + _renderModelAttestation(initialAttestation) + '</span>' +
        '</div>' +
        '</div>' +
        '<div class="message-content"></div>' +
        '<div class="stream-thinking-footer">' +
        '<span class="stream-waiting" role="status" aria-live="polite">' +
            '<span class="stream-waiting-text" data-status-text="Waiting for first token…">Waiting for first token…</span>' +
        '</span>' +
        stopControls +
        '<span class="stream-waiting-anim clotho-spinner" aria-hidden="true"></span>' +
        '<span class="looming-phase hidden" aria-label="Looming">' +
            '<span class="loom-anim" aria-hidden="true"></span>' +
            '<span class="looming-text">Looming</span>' +
            '<span class="looming-ellipsis" aria-hidden="true"><span>.</span><span>.</span><span>.</span></span>' +
        '</span>' +
        '<span class="turn-lifecycle-status hidden" role="status" aria-live="polite">' +
            '<span class="turn-lifecycle-text"></span>' +
        '</span>' +
        '<span class="gen-stats"><span class="gen-token-info"></span><span class="gen-timer">0:00</span></span>' +
        '</div>';
    container.querySelector('.empty-loom-hint')?.remove();
    container.appendChild(streamingDiv);
    _startGenTimer();
    _startStreamWaitingTicker();
    if (_pendingTurnStatus) {
        const pendingStatus = _pendingTurnStatus;
        _pendingTurnStatus = null;
        showGenStatus(pendingStatus.text, pendingStatus.reconnecting);
    }
    scrollToBottom();
}

function _renderWorkspaceNotifItem(n) {
    const item = document.createElement('div');
    item.className = 'notif-item notif-workspace';
    const timeStr = n.time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const paths = (n.changedFiles || []).slice(0, 8).map(f => `${f.status}: ${f.path}`).join('\n');
    item.innerHTML = `<div class="notif-perm-header">`
        + `<span class="notif-time">${timeStr}</span>`
        + `<span class="notif-perm-tool">Workspace change report</span></div>`
        + `<div class="notif-perm-summary">${escapeHtml(n.summary || '')}</div>`
        + (n.warningText ? `<div class="notif-perm-summary notif-perm-risk">${escapeHtml(n.warningText)}</div>` : '')
        + (paths ? `<div class="notif-perm-summary"><pre>${escapeHtml(paths)}</pre></div>` : '')
        + (n.manifestPath ? `<div class="notif-perm-summary" title="Recovery manifest">Recovery: ${escapeHtml(n.manifestPath)}</div>` : '');
    return item;
}

const _ATTESTATION_SOURCE_DEFAULTS = {
    codex_app_server_thread_response: ['Codex app-server', 'harness'],
    claude_code_system_init: ['Claude Code', 'harness'],
    anthropic_compatible_message_start: ['Claude Code', 'provider_response'],
    agy_cli_launch_arguments: ['Antigravity (agy)', 'launch'],
    antigravity_stream_init: ['Antigravity (agy)', 'harness'],
    goose_process_environment: ['Goose ACP', 'launch'],
    goose_acp_session_model_state: ['Goose ACP', 'harness'],
    hermes_acp_session_model_state: ['Hermes ACP', 'harness'],
    hermes_acp_set_model_ack: ['Hermes ACP', 'harness'],
    openrouter_request_accepted: ['OpenRouter API', 'launch'],
    openrouter_request_configuration: ['OpenRouter API', 'launch'],
    openrouter_stream_chunk: ['OpenRouter API', 'provider_response'],
    openrouter_completion_response: ['OpenRouter API', 'provider_response'],
    dream_request_configuration: ['Dream', 'launch'],
    llama_server_request_configuration: ['Llama Server', 'launch'],
};

function _harnessLabelForModel(model) {
    const kind = _agentKindForModel(model, State.currentConv);
    if (kind === 'codex') return 'Codex app-server';
    if (kind === 'claude') return 'Claude Code';
    if (kind === 'gemini') return 'Antigravity (agy)';
    if (kind === 'goose') return 'Goose ACP';
    if (kind === 'hermes') return 'Hermes ACP';
    if (kind === 'openrouter') return 'OpenRouter API';
    if (kind === 'local') return 'Llama Server';
    if (kind === 'dream') return 'Dream';
    if (kind === 'umans') return 'Claude Code (Umans)';
    return '';
}

function _parseModelAttestation(value) {
    if (!value) return null;
    let parsed = value;
    if (typeof value === 'string') {
        try { parsed = JSON.parse(value); }
        catch { return null; }
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;

    // Attestations persisted before the cross-provider schema had source and
    // model evidence but no harness/verification fields. Recover those fields
    // from the evidence source so old verified Codex turns do not render as
    // "Unknown harness" (or inherit an unrelated current conversation mode).
    const att = { ...parsed };
    const sourceDefaults = _ATTESTATION_SOURCE_DEFAULTS[att.source];
    const model = att.effective_model || att.launch_model || att.requested_model || '';
    if (!att.harness) {
        att.harness = sourceDefaults?.[0] || _harnessLabelForModel(model) || 'Unknown harness';
    }
    if (!att.verification_level) {
        att.verification_level = sourceDefaults?.[1]
            || (att.status === 'verified' ? 'harness' : 'selection');
    }
    return att;
}

function _modelAttestationSourceLabel(source) {
    if (source === 'codex_app_server_thread_response') return 'Codex app-server thread response';
    if (source === 'claude_code_system_init') return 'Claude Code structured init';
    if (source === 'anthropic_compatible_message_start') return 'Anthropic-compatible response metadata';
    if (source === 'agy_cli_launch_arguments') return 'Antigravity launch arguments';
    if (source === 'antigravity_stream_init') return 'Antigravity structured init';
    if (source === 'goose_process_environment') return 'Goose process configuration';
    if (source === 'goose_acp_session_model_state') return 'Goose ACP session model state';
    if (source === 'hermes_acp_session_model_state') return 'Hermes ACP session model state';
    if (source === 'hermes_acp_set_model_ack') return 'Hermes ACP set-model acknowledgement';
    if (source === 'openrouter_request_accepted') return 'OpenRouter accepted request';
    if (source === 'openrouter_request_configuration') return 'OpenRouter request configuration';
    if (source === 'openrouter_stream_chunk') return 'OpenRouter response metadata';
    if (source === 'openrouter_completion_response') return 'OpenRouter completion response';
    if (source === 'openai_compatible_request_accepted') return 'OpenAI-compatible backend accepted request';
    if (source === 'dream_request_configuration') return 'Dream request configuration';
    if (source === 'llama_server_request_configuration') return 'llama-server request configuration';
    if (source === 'openai_compatible_stream_chunk') return 'OpenAI-compatible response metadata';
    if (source === 'openai_compatible_completion_response') return 'OpenAI-compatible completion response';
    if (source === 'local_mock_response') return 'Local mock response (no model proof)';
    if (source === 'loom_ui_selection') return 'Loom UI selection (not yet provider-confirmed)';
    return source || 'Unknown';
}

function _renderModelAttestation(value) {
    const att = _parseModelAttestation(value);
    if (!att) return '';
    const status = ['verified', 'mismatch', 'configured', 'unverified', 'requested'].includes(att.status)
        ? att.status : 'unverified';
    const model = att.effective_model || att.launch_model || att.requested_model || 'unknown model';
    const harness = att.harness || 'Unknown harness';
    const continuity = att.session_continuity || '';
    const continuityReason = att.session_continuity_reason || '';
    const continuityLabel = continuityReason === 'provider_boundary' ? 'harness handoff'
        : continuityReason === 'model_boundary' ? 'model handoff'
        : continuity === 'forked' ? 'native fork'
        : continuity === 'resumed' ? 'native resume'
        : continuity === 'rebuilt' ? 'history rebuild'
        : continuity;
    const preparation = att.preparation && typeof att.preparation === 'object'
        ? att.preparation : null;
    const preparationCalls = Array.isArray(preparation?.calls) ? preparation.calls : [];
    const preparationLabel = preparationCalls.length
        ? `${preparationCalls.length} call${preparationCalls.length === 1 ? '' : 's'} · ${preparation.status || 'unverified'}`
        : '';
    const mark = status === 'verified' ? '&#10003;'
        : status === 'mismatch' ? '&#10005;'
        : status === 'configured' ? '&#9671;'
        : status === 'requested' ? '&#8594;' : '?';
    const suffix = status === 'requested' ? ' requested'
        : status === 'mismatch' ? ' mismatch'
        : status === 'configured' ? ' configured'
        : status === 'unverified' ? ' unverified' : '';
    const rows = [
        ['Status', status],
        ['Harness', att.harness],
        ['UI selection', att.requested_model],
        ['Launch request', att.launch_model],
        ['Effective model', att.effective_model],
        ['Provider', att.model_provider],
        ['Verification', att.verification_level],
        ['Evidence', _modelAttestationSourceLabel(att.source)],
        ['Fallback', att.fallback_allowed === false ? 'disabled' : (att.fallback_allowed === true ? 'allowed' : '')],
        ['Thread', att.thread_id],
        ['Session continuity', continuityLabel],
        ['Continuity reason', att.session_continuity_reason],
        ['Source session', att.source_session_id],
        ['App server', att.app_server],
        ['Preparation', preparationLabel],
        ['Identity warning', att.identity_warning],
    ].filter(([, val]) => val !== undefined && val !== null && val !== '');
    const title = status === 'verified'
        ? `${model} was reported by ${harness}. Click for the full chain of custody.`
        : status === 'configured'
            ? `${harness} was configured for ${model}, but did not expose independent effective-model evidence.`
        : status === 'requested'
            ? `${model} is the selected model; waiting for provider confirmation.`
            : `Model identity is ${status}. Click for details.`;
    const preparationRows = preparationCalls.map((call, index) => {
        const route = [call.harness, call.requested_model].filter(Boolean).join(' · ');
        const effective = call.effective_model ? ` → ${call.effective_model}` : '';
        return [`Preparation ${index + 1}`, `${route}${effective} · ${call.status || 'unverified'}`];
    });
    const panel = rows.concat(preparationRows).map(([key, val]) =>
        `<div><span>${escapeHtml(String(key))}</span><code>${escapeHtml(String(val))}</code></div>`
    ).join('');
    return `<details class="model-attestation model-attestation-${status}" title="${escapeHtml(title)}">` +
        `<summary><span class="model-attestation-full">${mark} ${escapeHtml(harness)} · ${escapeHtml(model)}${escapeHtml(suffix)}` +
        `${continuityLabel ? ` · ${escapeHtml(continuityLabel)}` : ''}</span>` +
        `<span class="model-attestation-compact">${mark} ${escapeHtml(model)}${escapeHtml(suffix)}</span></summary>` +
        `<div class="model-attestation-panel">${panel}</div>` +
        `</details>`;
}

function _updateStreamingModelAttestation(attestation) {
    if (!streamingDiv) return;
    const slot = streamingDiv.querySelector('.model-attestation-slot');
    if (slot) slot.innerHTML = _renderModelAttestation(attestation);
}

let _streamWaitingTicker = null;
let _streamWaitingFrame = 0;
const _CLOTHO_SPINNER_INTERVAL_MS = 115;
const _CLOTHO_SPINNER_FRAMES = [
    '·           ·',
    ' ·         · ',
    '  ⠐       ⠂  ',
    '   ⠒     ⠚   ',
    '    ⠒⠤ ⠚⠤    ',
    '   ⠤⢎   ⡰⠤   ',
    '  ──⢎   ⡰──  ',
    '  ──⤹   ⤸──  ',
    ' ━━━⤹ ◈ ⤸━━━ ',
    ' ━━━⠤ ◈ ⠤━━━ ',
    ' ═════ ◈ ═════ ',
    ' ═════ ✦ ═════ ',
    ' ═════ ✧ ═════ ',
    ' ━━━⠤ ◈ ⤤━━━ ',
    ' ━━━⤸ ◈ ⤹━━━ ',
    '  ──⤸   ⤹──  ',
    '  ──⡰   ⢎──  ',
    '   ⠤⡰   ⢎⠤   ',
    '    ⠒⠤ ⠚⠤    ',
    '   ⠒     ⠚   ',
    '  ⠐       ⠂  ',
    ' ·         · ',
    '·           ·',
    '             ',
];
function _startStreamWaitingTicker() {
    if (_streamWaitingTicker) clearInterval(_streamWaitingTicker);
    _streamWaitingFrame = 0;
    const start = Date.now();
    const paint = () => {
        if (!streamingDiv) { _stopStreamWaitingTicker(); return; }
        const animEl = streamingDiv.querySelector('.stream-waiting-anim');
        const textEl = streamingDiv.querySelector('.stream-waiting-text');
        if (!animEl || !textEl) { _stopStreamWaitingTicker(); return; }
        animEl.textContent = _CLOTHO_SPINNER_FRAMES[_streamWaitingFrame];
        _streamWaitingFrame = (_streamWaitingFrame + 1) % _CLOTHO_SPINNER_FRAMES.length;
        const secs = Math.floor((Date.now() - start) / 1000);
        const status = textEl.dataset.statusText || 'Waiting for first token…';
        if (secs > 10 && /waiting for first token/i.test(status)) {
            textEl.textContent = `${status} (${secs}s — large context can take 30–90s)`;
        } else if (secs > 4) {
            textEl.textContent = `${status} (${secs}s)`;
        } else {
            textEl.textContent = status;
        }
    };
    paint();
    _streamWaitingTicker = setInterval(paint, _CLOTHO_SPINNER_INTERVAL_MS);
}
function _stopStreamWaitingTicker() {
    if (_streamWaitingTicker) { clearInterval(_streamWaitingTicker); _streamWaitingTicker = null; }
}
function _removeStreamWaiting() {
    _stopStreamWaitingTicker();
    if (!streamingDiv) return;
    const el = streamingDiv.querySelector('.stream-waiting');
    if (el) el.remove();
    streamingDiv.querySelector('.stream-waiting-anim')?.remove();
    streamingDiv.querySelector('.looming-phase')?.classList.remove('hidden');
}

let _streamBuffer = '';
let _streamFlushTimer = null;

function appendStreamChunk(content) {
    // If the streamingDiv got detached (e.g. by a mid-stream renderMessages
    // that happens when loadMessages or reconstruct races with chunks),
    // drop the stale ref and ask the server for a snapshot so we can rebuild
    // without forcing the user to refresh the page.
    if (streamingDiv && !streamingDiv.isConnected) {
        streamingDiv = null;
    }
    if (!streamingDiv) {
        // Drop this chunk (server snapshot already contains it) and ask for
        // a fresh snapshot so the reconstruction path can rebuild the UI.
        // BUT: if reconstruction is already in flight (post-reconnect rebuild
        // from a prior generation_active), don't request another snapshot —
        // the in-flight loadMessages + _reconstructFromSnapshot will rebuild
        // the streamingDiv momentarily, and re-requesting now triggers a
        // generation_active → loadMessages → renderMessages loop that wipes
        // the streamingDiv before it can render anything.
        _droppedChunkCount++;
        if (State.isStreaming && State._streamIsOurBranch !== false && !State._reconstructing) {
            _requestSnapshotIfStreaming();
        }
        _streamBuffer = '';
        return;
    }
    _streamBuffer += content;
    // Throttle DOM updates to max every 50ms
    if (!_streamFlushTimer) {
        _streamFlushTimer = setTimeout(_flushStreamBuffer, 50);
    }
}

// Throttle scroll during streaming so rapid chunk flushes don't thrash layout.
let _lastStreamScrollAt = 0;
function _scrollDuringStream() {
    const now = Date.now();
    if (now - _lastStreamScrollAt < 250) return;
    _lastStreamScrollAt = now;
    scrollToBottom();
}

function _flushStreamBuffer() {
    _streamFlushTimer = null;
    if (streamingDiv && !streamingDiv.isConnected) {
        streamingDiv = null;
        if (State.isStreaming && State._streamIsOurBranch !== false && !State._reconstructing) {
            _requestSnapshotIfStreaming();
        }
        _streamBuffer = '';
        return;
    }
    if (!streamingDiv || !_streamBuffer) return;
    _removeStreamWaiting();
    const contentEl = streamingDiv.querySelector('.message-content');
    // Append text to the trailing streaming-text span. If the most recent
    // child is a tool block (or anything else), start a new span after it
    // so text that arrives after a tool call renders below the tool —
    // chronological order, no sentence-split-then-reassemble.
    let textSpan = null;
    for (let i = contentEl.children.length - 1; i >= 0; i--) {
        const child = contentEl.children[i];
        if (child.classList && child.classList.contains('streaming-text')) {
            textSpan = child;
            break;
        }
        if (child.classList && (child.classList.contains('tool-block') || child.classList.contains('cc-thinking'))) {
            break;
        }
    }
    if (!textSpan) {
        textSpan = document.createElement('span');
        textSpan.className = 'streaming-text';
        contentEl.appendChild(textSpan);
    }
    // Append-only: just tack on a text node with the new delta. O(1) per flush
    // regardless of total message size. We accept raw markdown chars visible
    // during the stream (no `**bold**` formatting until finalize) in exchange
    // for flushes that don't re-parse the entire message every 50ms.
    const delta = _streamBuffer;
    _streamBuffer = '';
    const prev = textSpan.dataset.rawContent || '';
    textSpan.dataset.rawContent = prev + delta;
    textSpan.appendChild(document.createTextNode(delta));
    // Keep cursor at the end
    const cursor = contentEl.querySelector('.typing-cursor');
    if (cursor && cursor !== contentEl.lastChild) contentEl.appendChild(cursor);
    _scrollDuringStream();
}

// Flush any buffered text deltas synchronously. Call this before appending a
// tool/thinking block: stream_chunk text is throttled through a 50ms timer,
// but tool_start fires immediately — without this, a tool block jumps ahead of
// text that arrived before it, splitting a sentence (even mid-word) around it.
function _flushStreamBufferNow() {
    if (_streamFlushTimer) {
        clearTimeout(_streamFlushTimer);
        _streamFlushTimer = null;
    }
    _flushStreamBuffer();
}

function finalizeStreamingMessage(msg, cost, {preserveViewport = false} = {}) {
    if (!streamingDiv) return;
    _stopGenTimer();
    _stopStreamWaitingTicker();

    // Cancel any pending incremental flush — we're about to do a full markdown
    // render in one pass, so a trailing setTimeout would just waste work.
    if (_streamFlushTimer) {
        clearTimeout(_streamFlushTimer);
        _streamFlushTimer = null;
    }
    _streamBuffer = '';

    // Replace the streaming div (which holds append-only raw text) with a
    // fully rendered message element. This is the ONE markdown parse pass per
    // turn — O(N) instead of O(N²) from per-chunk reparsing.
    const existingIdx = State.messages.findIndex(m => m.id === msg.id);
    if (existingIdx >= 0) State.messages[existingIdx] = msg;
    else State.messages.push(msg);
    const elapsed = _streamStartTime ? Math.round((Date.now() - _streamStartTime) / 1000) : 0;
    const newEl = createMessageElement(msg, cost, elapsed);
    const scroller = document.getElementById('messages-container');
    const scrollTop = scroller?.scrollTop;
    streamingDiv.replaceWith(newEl);
    streamingDiv = null;
    State._liveStreamDraft = null;
    if (preserveViewport) {
        if (scroller) scroller.scrollTop = scrollTop;
    } else scrollToBottom();
}

function removeStreamingMessage() {
    _stopGenTimer();
    _stopStreamWaitingTicker();
    if (streamingDiv) {
        streamingDiv.remove();
        streamingDiv = null;
    }
    _pendingLaunch = null;
    // Remove any draft messages remaining in the DOM
    document.querySelectorAll('.message-generating').forEach(el => {
        const msgId = el.dataset?.msgId;
        if (msgId && State.messages?.some(m =>
            String(m.id) === String(msgId) &&
            (m.content?.trim() || (Array.isArray(m.content_blocks) ? m.content_blocks.length : m.content_blocks) || m.turn_lifecycle_state)
        )) {
            el.classList.remove('message-generating');
            return;
        }
        el.remove();
    });
}

function _streamingMessageHasContent() {
    if (!streamingDiv) return false;
    _flushStreamBufferNow();
    const contentEl = streamingDiv.querySelector('.message-content');
    if (!contentEl) return false;
    const clone = contentEl.cloneNode(true);
    clone.querySelectorAll('.stream-waiting, .stream-waiting-anim, .typing-cursor')
        .forEach(el => el.remove());
    return clone.textContent.trim().length > 0 || !!clone.querySelector('.tool-block, .cc-thinking, .ask-question-block');
}

function markStreamingMessageInterrupted(errorMsg) {
    if (!streamingDiv) return false;
    if (!_streamingMessageHasContent()) return false;

    _stopGenTimer();
    _stopStreamWaitingTicker();
    _removeStreamWaiting();
    streamingDiv.classList.add('stream-interrupted');
    streamingDiv.querySelectorAll('.typing-cursor').forEach(el => el.remove());
    const footer = streamingDiv.querySelector('.stream-thinking-footer');
    if (footer) {
        footer.textContent = errorMsg ? `Interrupted: ${errorMsg}` : 'Interrupted';
    }
    return true;
}

// ── Claude Code: Tool + Thinking Blocks ──

function appendToolBlock(name, toolId, isOoda) {
    if (!streamingDiv) return;
    _flushStreamBufferNow();  // land any pending text before this block
    _removeStreamWaiting();
    // Add show-ooda class on first OODA block so they're visible during streaming
    if (isOoda && !streamingDiv.classList.contains('show-ooda')) {
        streamingDiv.classList.add('show-ooda');
    }
    const contentEl = streamingDiv.querySelector('.message-content');
    const block = document.createElement('div');
    block.className = 'tool-block expanded' + (isOoda ? ' ooda-block' : '');
    block.dataset.toolId = toolId;
    block.innerHTML = '<div class="tool-block-header">' +
        '<span class="tool-name">' + escapeHtml(name) + '</span>' +
        '<span class="tool-toggle">&#9662;</span>' +
        '</div>' +
        '<div class="tool-block-body">' +
        '<div class="tool-block-input"></div>' +
        '<div class="tool-block-result"></div>' +
        '</div>';
    block.querySelector('.tool-block-header').addEventListener('click', () => {
        block.classList.toggle('expanded');
        block.querySelector('.tool-toggle').textContent = block.classList.contains('expanded') ? '▾' : '▸';
    });
    contentEl.appendChild(block);
    scrollToBottom();
}

function appendToolInput(json, toolId) {
    if (!streamingDiv) return;
    const block = streamingDiv.querySelector(`.tool-block[data-tool-id="${toolId}"]`)
                || streamingDiv.querySelector('.tool-block:last-child');
    if (!block) return;
    const inputEl = block.querySelector('.tool-block-input');
    // Cap displayed input to prevent DOM overload
    if (inputEl.textContent.length < 3000) {
        inputEl.textContent += json;
    }
}

function finalizeToolBlock(result, toolId, imageUrl, isError) {
    if (!streamingDiv) return;
    const block = streamingDiv.querySelector(`.tool-block[data-tool-id="${toolId}"]`)
                || streamingDiv.querySelector('.tool-block:last-child');
    if (!block) return;
    const resultEl = block.querySelector('.tool-block-result');
    // Truncate long results for display
    const display = result.length > 2000 ? result.substring(0, 2000) + '\n... (truncated)' : result;
    const toolName = block.querySelector('.tool-name')?.textContent || '';
    const inputText = block.querySelector('.tool-block-input')?.textContent || '';
    const diffHtml = toolName === 'Edit' ? renderEditDiff(inputText, result) : null;
    if (diffHtml) {
        const inputEl = block.querySelector('.tool-block-input');
        if (inputEl) inputEl.textContent = '';
        resultEl.innerHTML = diffHtml;
    } else {
        resultEl.textContent = display;
    }

    // Show success/error indicator on the header
    const header = block.querySelector('.tool-block-header');
    if (header && !header.querySelector('.tool-status')) {
        const indicator = document.createElement('span');
        indicator.className = isError ? 'tool-status tool-error' : 'tool-status tool-success';
        indicator.textContent = isError ? '✗' : '✓';
        indicator.title = isError ? 'Failed' : 'Success';
        header.appendChild(indicator);
    }
    if (isError) block.classList.add('tool-errored');

    // If this tool produced an image, display it inline
    if (imageUrl) {
        const filename = decodeURIComponent(imageUrl.split('path=').pop() || '').split(/[/\\]/).pop() || 'image';
        const imgContainer = document.createElement('div');
        imgContainer.className = 'tool-image-result';
        const figure = document.createElement('figure');
        figure.className = 'detected-image-figure';
        const img = document.createElement('img');
        img.src = imageUrl;
        img.alt = filename;
        img.className = 'generated-image';
        img.addEventListener('click', () => {
            const body = document.getElementById('preview-modal-body');
            body.innerHTML = '<img src="' + imageUrl + '" style="max-width:100%;max-height:80vh;">';
            document.getElementById('modal-preview').classList.remove('hidden');
        });
        const caption = document.createElement('figcaption');
        caption.textContent = filename;
        figure.appendChild(img);
        figure.appendChild(caption);
        imgContainer.appendChild(figure);
        block.querySelector('.tool-block-body').appendChild(imgContainer);
        // Reading an existing image is looking, not producing: keep that
        // step collapsed. Screenshots and other tool-made images open.
        if (toolName !== 'Read') {
            block.classList.add('expanded');
            scrollToBottom();
            return;
        }
    }
    // Auto-collapse after result arrives
    block.classList.remove('expanded');
    block.querySelector('.tool-toggle').textContent = '▸';
    scrollToBottom();
}

function appendThinkingChunk(text) {
    if (!streamingDiv) return;
    _flushStreamBufferNow();  // land any pending text before this block
    _removeStreamWaiting();
    const contentEl = streamingDiv.querySelector('.message-content');
    // Find the LAST thinking block (if multiple exist) or create one if the last element isn't a thinking block
    let thinkingEl = contentEl.lastElementChild;
    if (!thinkingEl || !thinkingEl.classList.contains('cc-thinking')) {
        thinkingEl = document.createElement('div');
        thinkingEl.className = 'cc-thinking';
        thinkingEl.innerHTML = '<div class="cc-thinking-toggle">Thinking...</div>' +
            '<div class="cc-thinking-content"></div>';
        thinkingEl.querySelector('.cc-thinking-toggle').addEventListener('click', () => {
            const content = thinkingEl.querySelector('.cc-thinking-content');
            content.style.display = content.style.display === 'none' ? 'block' : 'none';
        });
        contentEl.appendChild(thinkingEl);
    }
    const thinkingContent = thinkingEl.querySelector('.cc-thinking-content');
    thinkingContent.textContent += text;
    scrollToBottom();
}

// ── Fork ──

async function forkFromMessage(msgId) {
    if (!State.currentConvId) return;
    try {
        await switchToBranch(msgId, msgId, {exact: true});
        document.getElementById('user-input').focus();
        showToast('Branch point selected. Send a message to create a branch in this conversation.');
    } catch (err) {
        showToast('Branch failed', 'error');
    }
}

// ── Edit User Message ──

function editMessage(msgId) {
    const msg = State.messages.find(m => m.id === msgId);
    if (!msg || msg.role !== 'user') return;

    const msgEl = document.querySelector(`.message[data-msg-id="${msgId}"]`);
    if (!msgEl) return;
    const existingEditor = document.querySelector('.edit-message-input');
    if (existingEditor) {
        existingEditor.focus();
        return;
    }
    _inlineEditContext = { convId: State.currentConvId, msgId };
    _messageRenderDeferredForEdit = false;
    const contentEl = msgEl.querySelector('.message-content');

    // Track files for this edit: start with originals, allow adding more
    const originalPaths = parseImagePaths(msg.image_path);
    const editFiles = originalPaths.map(p => ({ path: p, original: true }));

    const textarea = document.createElement('textarea');
    textarea.className = 'edit-message-input';
    textarea.value = msg.content;
    textarea.rows = Math.max(3, msg.content.split('\n').length);
    contentEl.replaceWith(textarea);
    textarea.focus();
    // Auto-grow as the user types/pastes — clone-measure to avoid flex flicker.
    const _resizeEditTextarea = () => {
        const clone = textarea.cloneNode(true);
        clone.style.position = 'absolute';
        clone.style.visibility = 'hidden';
        clone.style.height = 'auto';
        clone.style.width = textarea.offsetWidth + 'px';
        textarea.parentNode.appendChild(clone);
        textarea.style.height = Math.min(clone.scrollHeight, 600) + 'px';
        clone.remove();
    };
    textarea.addEventListener('input', _resizeEditTextarea);
    // Defer once so the textarea is laid out before we measure.
    requestAnimationFrame(_resizeEditTextarea);

    // File preview area (above buttons)
    const filePreview = document.createElement('div');
    filePreview.className = 'edit-file-preview';
    textarea.after(filePreview);

    function renderEditFiles() {
        if (editFiles.length === 0) {
            filePreview.classList.add('hidden');
            filePreview.innerHTML = '';
            return;
        }
        filePreview.classList.remove('hidden');
        filePreview.innerHTML = editFiles.map((f, i) => {
            const name = f.path.split('/').pop().split('\\').pop();
            return `<span class="edit-file-chip${f.original ? '' : ' new-file'}" title="${f.original ? 'From original' : 'New attachment'}">${name}<button class="edit-file-remove" data-idx="${i}">&times;</button></span>`;
        }).join('');
        filePreview.querySelectorAll('.edit-file-remove').forEach(btn => {
            btn.addEventListener('click', () => {
                editFiles.splice(parseInt(btn.dataset.idx), 1);
                renderEditFiles();
            });
        });
    }
    renderEditFiles();

    // Describe focus input for image descriptions (only shown when images attached)
    const describeRow = document.createElement('details');
    describeRow.className = 'edit-describe-context hidden';
    describeRow.innerHTML = `
        <summary>Describe focus</summary>
        <label class="hidden" title="Extra focus for vision model image description — not sent to chat model">Describe focus</label>
        <textarea class="edit-describe-input" rows="2" placeholder="e.g. focus on body language and composition" autocomplete="off" spellcheck="false">${escapeHtml(msg.describe_context || '')}</textarea>
    `;
    filePreview.after(describeRow);

    // Show/hide describe row based on whether images are attached
    function updateDescribeVisibility() {
        const hasImg = editFiles.some(f => {
            const ext = f.path.split('.').pop().toLowerCase();
            return ['jpg','jpeg','png','gif','webp','bmp','svg'].includes(ext);
        });
        describeRow.classList.toggle('hidden', !hasImg);
    }
    updateDescribeVisibility();
    // Patch renderEditFiles to also update describe visibility
    const _origRenderEditFiles = renderEditFiles;
    renderEditFiles = function() { _origRenderEditFiles(); updateDescribeVisibility(); };

    const isWeave = State.currentConv && State.currentConv.mode === 'weave';
    const btnRow = document.createElement('div');
    btnRow.className = 'edit-message-actions';
    const pillHtml = isWeave ? `<div class="branch-count-pill pill-compact" title="Branches to generate (click to cycle)"><span class="branch-count-icon">⑂</span><span class="branch-count-value">${State.branchCount}</span></div>` : '';
    btnRow.innerHTML = `
        <label class="btn-small edit-attach" title="Attach file(s) — you can also paste or drop files onto the textarea">
            <input type="file" class="edit-file-input" multiple hidden>
            📎
        </label>
        <button class="btn-small edit-save" title="Send as new branch (Shift+Enter)">Send as new branch</button>
        ${pillHtml}
        <button class="btn-small edit-cancel">Cancel</button>
    `;
    if (isWeave) _setupBranchPillClick(btnRow.querySelector('.branch-count-pill'));
    describeRow.after(btnRow);

    // Shared uploader: takes a File, uploads it, appends to editFiles, refreshes chips.
    async function _uploadEditFile(file) {
        if (!file) return;
        try {
            const result = await API.upload(file);
            editFiles.push({ path: result.path, original: false });
            renderEditFiles();
        } catch { showToast('File upload failed', 'error'); }
    }
    async function _uploadEditFiles(fileList) {
        const files = Array.from(fileList || []);
        if (!files.length) return;
        const editAttachLabel = btnRow.querySelector('.edit-attach');
        const orig = editAttachLabel.textContent;
        editAttachLabel.classList.add('uploading');
        editAttachLabel.textContent = '⏳';
        for (const f of files) await _uploadEditFile(f);
        editAttachLabel.textContent = orig || '📎';
        editAttachLabel.classList.remove('uploading');
    }

    // File picker (now accepts multiple)
    const editFileInput = btnRow.querySelector('.edit-file-input');
    editFileInput.addEventListener('change', async (e) => {
        await _uploadEditFiles(e.target.files);
        e.target.value = '';
    });

    // Paste clipboard images / files directly into the textarea
    textarea.addEventListener('paste', async (e) => {
        const items = e.clipboardData && e.clipboardData.items;
        if (!items) return;
        const files = [];
        for (const item of items) {
            // Image from clipboard or a regular File
            if (item.kind === 'file') {
                const f = item.getAsFile();
                if (f) files.push(f);
            }
        }
        if (files.length > 0) {
            e.preventDefault();
            await _uploadEditFiles(files);
        }
    });

    // Drop files onto the textarea
    textarea.addEventListener('dragover', (e) => {
        if (e.dataTransfer && e.dataTransfer.types && e.dataTransfer.types.indexOf('Files') !== -1) {
            e.preventDefault();
            textarea.classList.add('drag-over');
        }
    });
    textarea.addEventListener('dragleave', () => textarea.classList.remove('drag-over'));
    textarea.addEventListener('drop', async (e) => {
        if (!(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length)) return;
        e.preventDefault();
        textarea.classList.remove('drag-over');
        await _uploadEditFiles(e.dataTransfer.files);
    });

    // Shift+Enter = send (matches main #user-input). Plain Enter = newline.
    textarea.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && e.shiftKey) {
            e.preventDefault();
            btnRow.querySelector('.edit-save').click();
        }
    });

    btnRow.querySelector('.edit-cancel').addEventListener('click', () => {
        const newContent = document.createElement('div');
        newContent.className = 'message-content';
        newContent.innerHTML = formatContent(msg.content);
        textarea.replaceWith(newContent);
        filePreview.remove();
        describeRow.remove();
        btnRow.remove();
        _releaseInlineEditRenderGuard({ flushDeferred: true });
    });

    btnRow.querySelector('.edit-save').addEventListener('click', async () => {
        const newText = textarea.value.trim();
        if (!newText) return;

        try {
            const parentId = msg.parent_id || null;
            const allPaths = editFiles.map(f => f.path);
            const convId = State.currentConvId;
            const editsRootMessage = parentId == null;
            if (editsRootMessage) {
                await createServerLoomThreadCursor(convId);
            }

            const editDescInput = describeRow.querySelector('.edit-describe-input');
            const editDescCtx = editDescInput ? editDescInput.value.trim() : '';
            const editMsgData = {
                role: 'user',
                content: newText,
                image_path: allPaths.length > 0 ? allPaths : null,
                // Branching conversation topology is not permission to run a
                // second provider concurrently. The server can queue this
                // exact branch if the single-agent lane is occupied.
                send_intent: editsRootMessage ? 'new_thread' : 'continue',
                generation_count: State.branchCount || 1,
                launch_generation: true,
            };
            Object.assign(
                editMsgData,
                await _messageTopologyContract(
                    parentId,
                    editsRootMessage ? 'new_thread' : 'branch',
                    convId,
                ),
            );
            _attachCCSettings(editMsgData);
            if (editDescCtx) editMsgData.describe_context = editDescCtx;
            const newMsg = await API.post(`/api/conversations/${convId}/messages`, editMsgData);
            const launchRunIds = (newMsg.generation_ids || []).map(id => `gen:${id}`);
            if (!launchRunIds.some(id => _recentTerminalRuns.has(_terminalRunKey(convId, id)))) {
                _acceptMessageCursor(newMsg, convId);
            }
            if (State._cursorSyncPromise) await State._cursorSyncPromise;
            if (!_sameConvId(State.currentConvId, convId)) return;

            // Switch to and scroll to the new branch, then kick off generation
            await switchToBranch(newMsg.id, newMsg.id, { launchRunIds });
            hideRetryBar();  // loadMessages inside switchToBranch re-creates generate bar; hide it
            hidePlanBar();

            if (!Array.isArray(newMsg.generation_ids)) {
                _triggerParallelGenerate(State.branchCount || 1, newMsg.id, {
                    convId,
                    threadId: newMsg.loom_thread_id,
                });
            }

        } catch (err) {
            showToast(err.detail || err.message || 'Failed to save edit', 'error');
        }
    });
}

// ── Copy ──

function copyMessage(msgId) {
    const msg = State.messages.find(m => m.id === msgId);
    if (msg) {
        navigator.clipboard.writeText(msg.content).then(
            () => showToast('Copied'),
            () => showToast('Copy failed', 'error')
        );
    }
}

// ── Highlight to Reply ──
// Select text inside any message bubble and a floating "Reply ↩" button
// appears; clicking it pins the excerpt above the input, and the next send
// carries it as a quoted block. Mode-agnostic by design: the excerpt rides
// inside the message text itself, so every backend (Loom, Braid, Hermes,

let _replyFloatBtn = null;

function _selectionInSingleMessage() {
    // Selecting inside an edit box focuses it — that's editing, not quoting
    const ae = document.activeElement;
    if (ae && (ae.tagName === 'TEXTAREA' || ae.tagName === 'INPUT')) return null;
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null;
    const text = sel.toString().trim();
    if (!text) return null;
    const range = sel.getRangeAt(0);
    const contentOf = (node) => {
        const el = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
        return el ? el.closest('#messages .message .message-content') : null;
    };
    const start = contentOf(range.startContainer);
    const end = contentOf(range.endContainer);
    // Only offer Reply when the whole selection sits inside one message body
    if (!start || start !== end) return null;
    return { msgEl: start.closest('.message'), text, rect: range.getBoundingClientRect() };
}

function _hideReplyFloatBtn() {
    if (_replyFloatBtn) {
        _replyFloatBtn.remove();
        _replyFloatBtn = null;
    }
}

function _showReplyFloatBtn(hit) {
    _hideReplyFloatBtn();
    const btn = document.createElement('button');
    btn.id = 'reply-float-btn';
    btn.innerHTML = 'Reply <span class="reply-float-arrow">&#x21A9;</span>';
    // mousedown (not click) so we beat the browser collapsing the selection
    btn.addEventListener('mousedown', (e) => {
        e.preventDefault();
        e.stopPropagation();
        setPendingReply(hit.msgEl, hit.text);
        _hideReplyFloatBtn();
        window.getSelection()?.removeAllRanges();
    });
    document.body.appendChild(btn);
    const bw = btn.offsetWidth, bh = btn.offsetHeight;
    let left = hit.rect.left + (hit.rect.width - bw) / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - bw - 8));
    let top = hit.rect.top - bh - 8;
    if (top < 8) top = hit.rect.bottom + 8;
    btn.style.left = `${left}px`;
    btn.style.top = `${top}px`;
    _replyFloatBtn = btn;
}

function setPendingReply(msgEl, text) {
    const roleLabel = msgEl.querySelector('.message-role')?.textContent?.trim()
        || (msgEl.classList.contains('user') ? 'You' : 'Assistant');
    State.pendingReply = {
        msgId: msgEl.dataset.msgId || null,
        label: roleLabel,
        text,
    };
    renderReplyQuoteBar();
    saveComposerDraft();
}

function clearPendingReply({ persist = true } = {}) {
    State.pendingReply = null;
    renderReplyQuoteBar();
    if (persist) saveComposerDraft();
}

function renderReplyQuoteBar() {
    const bar = document.getElementById('reply-quote-bar');
    if (!bar) return;
    const reply = State.pendingReply;
    if (!reply) {
        bar.classList.add('hidden');
        return;
    }
    bar.classList.remove('hidden');
    bar.querySelector('.reply-quote-label').textContent = `Replying to ${reply.label}`;
    bar.querySelector('.reply-quote-text').textContent = reply.text;
}

/** Format the pending excerpt as a quoted block prepended to the outgoing message. */
function _composeReplyContent(content, reply = State.pendingReply) {
    if (!reply) return content;
    const quoted = reply.text.split('\n').map(l => '> ' + l).join('\n');
    const header = `Replying to ${reply.label}:`;
    return content
        ? `${header}\n${quoted}\n\n${content}`
        : `${header}\n${quoted}`;
}

function _initHighlightReply() {
    // Re-evaluate the selection after mouse/touch interactions settle
    const refresh = () => {
        setTimeout(() => {
            const hit = _selectionInSingleMessage();
            if (hit) _showReplyFloatBtn(hit);
            else _hideReplyFloatBtn();
        }, 0);
    };
    document.addEventListener('mouseup', refresh);
    document.addEventListener('touchend', refresh);
    document.addEventListener('selectionchange', () => {
        const sel = window.getSelection();
        if (!sel || sel.isCollapsed) _hideReplyFloatBtn();
    });
    // The floating button is fixed-positioned; hide rather than track on scroll
    document.getElementById('messages-container')
        ?.addEventListener('scroll', _hideReplyFloatBtn, { passive: true });

    const clearBtn = document.getElementById('btn-reply-quote-clear');
    if (clearBtn) clearBtn.addEventListener('click', clearPendingReply);
    // Click the chip body to jump back to the quoted message
    document.getElementById('reply-quote-bar')?.addEventListener('click', (e) => {
        if (e.target.closest('#btn-reply-quote-clear')) return;
        const msgId = State.pendingReply?.msgId;
        if (!msgId) return;
        const target = document.querySelector(`#messages .message[data-msg-id="${msgId}"]`);
        if (target) {
            target.scrollIntoView({ behavior: 'smooth', block: 'center' });
            target.classList.add('message-highlight');
            setTimeout(() => target.classList.remove('message-highlight'), 1600);
        }
    });
}

/** Re-describe all images attached to a user message */
async function rerollDescribe(convId, msgId) {
    const describeDiv = document.querySelector(`.message-describing[data-parent-msg-id="${msgId}"]`);
    if (!describeDiv) return;

    // Show spinner state
    const contentDiv = describeDiv.querySelector('.describe-content');
    if (contentDiv) {
        contentDiv.innerHTML = '<span class="describe-spinner"><span class="loom-anim"></span> Re-describing...</span>';
    }
    const timerEl = describeDiv.querySelector('.gen-timer');
    if (timerEl) timerEl.textContent = '0:00';

    // Remove old reroll button
    const oldBtn = describeDiv.querySelector('.describe-reroll');
    if (oldBtn) oldBtn.remove();

    // Start timer
    const startTime = Date.now();
    const timer = setInterval(() => {
        const elapsed = Math.floor((Date.now() - startTime) / 1000);
        if (timerEl) timerEl.textContent = _formatElapsed(elapsed);
    }, 1000);

    try {
        const resp = await API.post(`/api/conversations/${convId}/redescribe/${msgId}`);
        clearInterval(timer);

        if (resp && resp.descriptions) {
            let descText = '';
            for (const [filename, desc] of Object.entries(resp.descriptions)) {
                if (descText) descText += '\n\n';
                descText += desc;
            }
            if (contentDiv) contentDiv.innerHTML = formatContent(descText);
            if (timerEl) timerEl.textContent = _formatElapsed(Math.floor(resp.elapsed_ms / 1000));

            // Add reroll button back
            const actionsDiv = describeDiv.querySelector('.message-actions');
            if (actionsDiv) {
                const rerollBtn = document.createElement('button');
                rerollBtn.className = 'btn-small describe-reroll';
                rerollBtn.innerHTML = '&#x21BB;';
                rerollBtn.title = 'Re-describe';
                rerollBtn.onclick = () => rerollDescribe(convId, msgId);
                actionsDiv.insertBefore(rerollBtn, actionsDiv.firstChild);
            }

            // Update state
            const msg = State.messages.find(m => m.id === msgId);
            if (msg) {
                let existing = {};
                if (msg.image_alt) {
                    try { existing = JSON.parse(msg.image_alt) || {}; } catch { existing = {}; }
                }
                msg.image_alt = JSON.stringify({ ...existing, ...resp.descriptions });
            }
            const modelLabel = resp.model || 'vision model';
            const sysMsg = State.messages.find(m => m.role === 'system' && m.parent_id === msgId && m.content?.trimStart()?.startsWith('[Image description —'));
            if (sysMsg) {
                sysMsg.content = `[Image description — ${modelLabel}]\n\n` + descText;
            }
        }
    } catch (e) {
        clearInterval(timer);
        showToast('Re-describe failed: ' + (e.message || e), 'error');
        if (contentDiv) contentDiv.innerHTML = '<span style="color:var(--danger)">Re-describe failed</span>';
    }
}

// ── Chat Bookmark Toggle ──

async function toggleChatBookmark(msgId) {
    if (!State.currentConvId) return;
    const existing = State.bookmarks.find(b => b.message_id === msgId);
    if (existing) {
        await API.del(`/api/bookmarks/${existing.id}`);
        State.bookmarks = State.bookmarks.filter(b => b.id !== existing.id);
        showToast('Bookmark removed');
    } else {
        const branchName = State.branchNames?.[msgId] || '';
        const bm = await API.post(`/api/conversations/${State.currentConvId}/bookmarks`, {
            message_id: msgId,
            branch_name: branchName,
            description: '',
        });
        bm.conversation_title = State.currentConv?.title;
        bm.conversation_mode = State.currentConv?.mode;
        State.bookmarks.push(bm);
        showToast('Bookmarked');
    }
    renderMessages();
    if (typeof refreshOpenBookmarksPanels === 'function') refreshOpenBookmarksPanels();
}

// ── Refresh Tree ──

async function refreshTree() {
    if (!State.currentConvId) return;
    if (refreshTree._pending) return refreshTree._pending;
    const convId = State.currentConvId;
    try {
        refreshTree._pending = (async () => {
            const data = typeof refreshTreeData === 'function'
                ? await refreshTreeData({ force: true })
                : await API.get(`/api/conversations/${convId}/tree`);
            if (!_sameConvId(State.currentConvId, convId)) return;
            State.treeData = data;
            if (typeof markTreeDataFresh === 'function') markTreeDataFresh(convId);
            if (typeof TREE !== 'undefined') TREE._skipCenter = true;
            // Chat recovery must not lay out thousands of hidden tree nodes.
            // Opening the tree view renders the current data on demand.
            if (State.currentView === 'tree') await renderTree();
        })();
        await refreshTree._pending;
    } catch {
        // Silently fail
    } finally {
        refreshTree._pending = null;
    }
}

// ── Permission Prompts ──

// Queued permission prompts that arrived while _reconstructFromSnapshot
// was in flight. Rendering them immediately would attach them to a
// streamingDiv that's about to be destroyed and replaced.
function _drainPendingPermPrompts() {
    const queue = State._pendingPermPrompts;
    if (!queue || !queue.length) return;
    State._pendingPermPrompts = [];
    for (const data of queue) {
        if (!_permissionBelongsToVisibleBranch(data)) continue;
        showPermissionPrompt(data);
    }
}

function showPermissionPrompt(data) {
    if (!_permissionBelongsToVisibleBranch(data)) return;
    // Detect and drop stale streamingDiv ref before attaching a prompt to it.
    if (streamingDiv && !streamingDiv.isConnected) {
        streamingDiv = null;
    }
    // Dedup: reconstruction loop + queued-during-reconstruct replay both fire
    // showPermissionPrompt for the same request_id. Skip if already rendered
    // anywhere in the document (stream div, draft message, etc.).
    if (data.request_id && document.querySelector(`.permission-prompt[data-request-id="${data.request_id}"]`)) {
        return;
    }
    const contentEl = _permissionPromptHost(data);
    if (!contentEl) return;

    const prompt = document.createElement('div');
    prompt.dataset.requestId = data.request_id;

    const toolName = escapeHtml(data.tool_name || 'Unknown');
    const isPlan = data.tool_name === 'ExitPlanMode' || data.tool_name === 'exit_plan_mode';

    if (isPlan) {
        prompt.className = 'permission-prompt plan-prompt';
        let planInput = data.tool_input || {};
        if (typeof planInput === 'string') try { planInput = JSON.parse(planInput); } catch {}
        const planRaw = planInput.plan || planInput.planFilePath || data.input_summary || '';
        const planFileName = planInput.planFilePath ? planInput.planFilePath.split(/[/\\]/).pop() : '';
        // Render plan as markdown if marked is available, otherwise fall back to escaped pre
        const planHtml = (typeof marked !== 'undefined' && planRaw.length > 10)
            ? formatContent(planRaw)
            : '<pre>' + escapeHtml(planRaw) + '</pre>';
        prompt.innerHTML = '<div class="permission-header">' +
            '<span class="permission-icon">&#x1F9F5;</span>' +
            '<span class="permission-title">Plan Ready for Review</span>' +
            (planFileName ? '<span class="plan-filename">' + escapeHtml(planFileName) + '</span>' : '') +
            '</div>' +
            (planRaw ? '<div class="permission-body"><div class="permission-input plan-content">' + planHtml + '</div></div>' : '') +
            '<input class="permission-feedback-input" type="text" maxlength="4000" placeholder="Optional note to the agent">' +
            '<div class="permission-actions">' +
            '<button class="btn-permission allow plan-approve" data-perm-action="allow" data-request-id="' + data.request_id + '">Approve Plan</button>' +
            '<button class="btn-permission deny plan-revise" data-perm-action="deny" data-request-id="' + data.request_id + '">Revise</button>' +
            '</div>';
    } else {
        const isDestructive = data.risk_level === 'destructive';
        prompt.className = 'permission-prompt' + (isDestructive ? ' destructive-permission' : '');
        const inputSummary = escapeHtml(data.input_summary || JSON.stringify(data.tool_input || {}).substring(0, 300));
        const approvalMethod = escapeHtml(data.approval_method || '');
        const riskReason = escapeHtml(data.risk_reason || '');
        const allowAllButton = data.supports_allow_all !== false && permissionSupportsAllowAll(data.tool_name)
            ? '<button class="btn-permission allow-all" data-perm-action="allow-all" data-request-id="' + data.request_id + '">Allow for Branch</button>'
            : '';
        prompt.innerHTML = '<div class="permission-header">' +
            '<span class="permission-icon">' + (isDestructive ? '&#x26A0;' : '&#x1f512;') + '</span>' +
            '<span class="permission-title">' + (isDestructive ? 'Permission Request — Warning' : 'Permission Request') + '</span>' +
            '</div>' +
            '<div class="permission-body">' +
            '<div class="permission-tool">Tool: <strong>' + toolName + '</strong></div>' +
            (approvalMethod ? '<div class="permission-tool">Permission: <strong>' + approvalMethod + '</strong></div>' : '') +
            (riskReason ? '<div class="permission-risk">' + riskReason + '</div>' : '') +
            (data.rule_preview ? '<div class="permission-tool">Branch rule: ' + escapeHtml(data.rule_preview) + '</div>' : '') +
            (inputSummary ? '<div class="permission-input"><pre>' + inputSummary + '</pre></div>' : '') +
            '</div>' +
            '<input class="permission-feedback-input" type="text" maxlength="4000" placeholder="Optional note to the agent">' +
            '<div class="permission-actions">' +
            '<button class="btn-permission allow" data-perm-action="allow" data-request-id="' + data.request_id + '">Allow</button>' +
            '<button class="btn-permission deny" data-perm-action="deny" data-request-id="' + data.request_id + '">Deny</button>' +
            allowAllButton +
            '</div>';
    }

    // Attach button handlers
    prompt.querySelectorAll('.btn-permission').forEach(btn => {
        btn.addEventListener('click', () => {
            const action = btn.dataset.permAction;
            const requestId = btn.dataset.requestId;
            const allow = action === 'allow' || action === 'allow-all';
            const always = action === 'allow-all';
            const feedback = (prompt.querySelector('.permission-feedback-input')?.value || '').trim();

            if (State.ws && State.ws.readyState === WebSocket.OPEN) {
                State.ws.send(JSON.stringify({
                    action: 'permission_response',
                    request_id: requestId,
                    allow: allow,
                    always: always,
                    feedback: feedback,
                }));
            }

            // Disable buttons while waiting
            prompt.querySelectorAll('.btn-permission').forEach(b => b.disabled = true);
            const feedbackInput = prompt.querySelector('.permission-feedback-input');
            if (feedbackInput) feedbackInput.disabled = true;
            prompt.querySelector('.permission-title').textContent =
                allow ? 'Allowed' + (always ? ' (branch)' : '') : 'Denied';
            prompt.classList.add(allow ? 'resolved-allow' : 'resolved-deny');

            // Also clear the notification bell
            resolvePermissionNotification(requestId, allow, feedback);

            // If this was a plan approval, flip dropdown back to Act
            if (prompt.classList.contains('plan-prompt') && allow) {
                const permSel = document.getElementById('cc-permission-mode-inline');
                if (permSel) permSel.value = 'default';
            }
        });
    });

    contentEl.appendChild(prompt);
    scrollToBottom();
}

function _permissionPromptHost(data) {
    // During an active stream, permission prompts belong in the draft assistant
    // message. Outside an active stream, render a standalone prompt message
    // without creating/changing streamingDiv; a fake streamingDiv leaks state
    // and can make later turns look split or stuck.
    if (streamingDiv && !streamingDiv.isConnected) {
        streamingDiv = null;
    }
    if (State.isStreaming) {
        if (!streamingDiv) {
            const draft = _ensureLiveStreamDraft(data);
            appendStreamingMessage(
                data.cc_model || data.local_model || draft?.cc_model_used,
                draft?.model_attestation,
                data.draft_msg_id || draft?.id,
            );
        }
        return streamingDiv?.querySelector('.message-content') || null;
    }

    const container = document.getElementById('messages');
    if (!container) return null;
    const host = document.createElement('div');
    host.className = 'message assistant permission-message';
    host.innerHTML = '<div class="message-header">' +
        '<span class="message-role">Loom</span>' +
        '</div>' +
        '<div class="message-content"></div>';
    container.appendChild(host);
    return host.querySelector('.message-content');
}

function resolvePermissionPrompt(requestId, allowed, feedback = '', decisionSource = '') {
    // Update the prompt if it hasn't been updated yet (e.g., from timeout)
    const prompt = document.querySelector(`.permission-prompt[data-request-id="${requestId}"]`);
    if (prompt && !prompt.classList.contains('resolved-allow') && !prompt.classList.contains('resolved-deny')) {
        prompt.querySelectorAll('.btn-permission').forEach(b => b.disabled = true);
        const feedbackInput = prompt.querySelector('.permission-feedback-input');
        if (feedbackInput) {
            if (feedback && !feedbackInput.value) feedbackInput.value = feedback;
            feedbackInput.disabled = true;
        }
        prompt.querySelector('.permission-title').textContent =
            allowed ? 'Allowed' : (decisionSource === 'loom_timeout' ? 'Expired' : 'Denied');
        prompt.classList.add(allowed ? 'resolved-allow' : 'resolved-deny');

        // If this was a plan approval, flip dropdown back to Act
        if (prompt.classList.contains('plan-prompt') && allowed) {
            const permSel = document.getElementById('cc-permission-mode-inline');
            if (permSel) permSel.value = 'default';
        }
    }
}

// ── AskUserQuestion / ExitPlanMode Rendering ──

function renderAskUserQuestion(questions, toolId) {
    if (!streamingDiv) return;
    const contentEl = streamingDiv.querySelector('.message-content');

    for (const q of questions) {
        const block = document.createElement('div');
        block.className = 'ask-question-block';
        block.innerHTML = '<div class="ask-question-header">' + escapeHtml(q.header || 'Question') + '</div>' +
            '<div class="ask-question-text">' + escapeHtml(q.question) + '</div>' +
            '<div class="ask-question-options"></div>';

        const optionsEl = block.querySelector('.ask-question-options');
        for (const opt of (q.options || [])) {
            const btn = document.createElement('button');
            btn.className = 'ask-question-option';
            btn.innerHTML = '<strong>' + escapeHtml(opt.label) + '</strong>' +
                (opt.description ? '<span>' + escapeHtml(opt.description) + '</span>' : '');
            btn.addEventListener('click', () => {
                const input = document.getElementById('user-input');
                input.value = opt.label;
                input.focus();
                // Mark selected
                optionsEl.querySelectorAll('.ask-question-option').forEach(b => b.classList.remove('selected'));
                btn.classList.add('selected');
            });
            optionsEl.appendChild(btn);
        }
        contentEl.appendChild(block);
    }
    scrollToBottom();
}

function renderPlanReady(plan, planFile, toolId) {
    // Show as a persistent bar in the messages container (not inside streamingDiv which
    // gets destroyed on stream_end — by then the user hasn't had a chance to click anything).
    hidePlanBar();
    const container = document.getElementById('messages');
    const bar = document.createElement('div');
    bar.id = 'plan-bar';
    bar.className = 'retry-bar plan-bar';
    bar.innerHTML =
        '<span class="plan-bar-label">Plan ready' +
        (planFile ? ' — <code>' + escapeHtml(planFile) + '</code>' : '') + '</span>' +
        '<button class="btn-small plan-action-btn approve" id="btn-plan-approve">Approve</button>' +
        '<button class="btn-small plan-action-btn revise" id="btn-plan-revise">Revise</button>';

    bar.querySelector('#btn-plan-approve').addEventListener('click', () => {
        hidePlanBar();
        const input = document.getElementById('user-input');
        input.value = 'Approved, proceed with the plan.';
        sendMessage();
    });
    bar.querySelector('#btn-plan-revise').addEventListener('click', () => {
        hidePlanBar();
        const input = document.getElementById('user-input');
        input.value = "I'd like to revise the plan: ";
        input.focus();
        // Move cursor to end
        input.selectionStart = input.selectionEnd = input.value.length;
    });

    container.appendChild(bar);
    scrollToBottom();
}

function hidePlanBar() {
    const existing = document.getElementById('plan-bar');
    if (existing) existing.remove();
}

// ── Canvas ──

let _canvasRevisionTimer = null;
let _canvasRevisionBusy = false;
let _canvasRevisionOwner = null;
let _canvasLastRevision = null;

function _visibleCanvasConvId() {
    if (document.hidden || !State.currentConv?.canvas_enabled) return null;
    const frames = document.querySelectorAll('#canvas-fullview-iframe, .canvas-node-iframe, #workspace-canvas-iframe, #workspace-canvas-pane-iframe');
    return [...frames].some(frame => frame.getClientRects().length
        && frame.src.startsWith(`${location.origin}/api/canvas/${State.currentConvId}/`))
        ? State.currentConvId : null;
}

async function _pollCanvasRevision() {
    clearTimeout(_canvasRevisionTimer);
    _canvasRevisionTimer = null;
    const convId = _visibleCanvasConvId();
    if (convId == null || _canvasRevisionBusy) return;
    _canvasRevisionBusy = true;
    try {
        const resp = await fetch(`/api/conversations/${convId}/canvas/revision`, { cache: 'no-store' });
        if (!resp.ok) return;
        const state = await resp.json();
        if (!_sameConvId(_visibleCanvasConvId(), convId)) return;
        if (!state.complete || !state.revision) return;
        if (_sameConvId(_canvasRevisionOwner, convId) && _canvasLastRevision
            && _canvasLastRevision !== state.revision) refreshCanvasIframe();
        _canvasRevisionOwner = convId;
        _canvasLastRevision = state.revision;
    } catch { /* An unavailable host is not a Canvas edit. */ }
    finally {
        _canvasRevisionBusy = false;
        if (_visibleCanvasConvId() != null) _canvasRevisionTimer = setTimeout(_pollCanvasRevision, 3000);
    }
}

function _scheduleCanvasRevision() {
    clearTimeout(_canvasRevisionTimer);
    _canvasRevisionTimer = setTimeout(_pollCanvasRevision, 0);
}
document.addEventListener('visibilitychange', _scheduleCanvasRevision);

function refreshCanvasIframe() {
    if (!State.currentConvId) return;
    const src = `/api/canvas/${State.currentConvId}/index.html?t=${Date.now()}`;
    // Refresh whichever canvas iframe is visible
    const fullview = document.getElementById('canvas-fullview-iframe');
    if (fullview && !document.getElementById('canvas-fullview')?.classList.contains('hidden')) {
        fullview.src = src;
    }
    // Also refresh the tree node thumbnail
    const thumb = document.querySelector('.canvas-node-iframe');
    if (thumb) thumb.src = src;
    const sidebar = document.getElementById('workspace-canvas-iframe');
    if (sidebar && !sidebar.hidden) sidebar.src = src;
    const pane = document.getElementById('workspace-canvas-pane-iframe');
    if (pane && !pane.hidden) pane.src = src;
}

function openCanvasFullview() {
    const fullview = document.getElementById('canvas-fullview');
    if (!fullview || !State.currentConvId) return;
    fullview.classList.remove('hidden');
    const iframe = document.getElementById('canvas-fullview-iframe');
    iframe.src = `/api/canvas/${State.currentConvId}/index.html?t=${Date.now()}`;
    // Show tree button so user can navigate back
    const treeBtn = document.getElementById('btn-to-tree');
    if (treeBtn) treeBtn.classList.remove('hidden');
    // Hide canvas toggle — no purpose inside the canvas
    const canvasBtn = document.getElementById('btn-canvas-toggle');
    if (canvasBtn) canvasBtn.classList.add('hidden');
    _scheduleCanvasRevision();
}

function closeCanvasFullview() {
    const fullview = document.getElementById('canvas-fullview');
    if (fullview) fullview.classList.add('hidden');
    // Hide tree button again if we're in tree view
    if (State.currentView === 'tree') {
        const treeBtn = document.getElementById('btn-to-tree');
        if (treeBtn) treeBtn.classList.add('hidden');
    }
    // Restore canvas toggle button
    const canvasBtn = document.getElementById('btn-canvas-toggle');
    if (canvasBtn) canvasBtn.classList.toggle('hidden', !State.currentConv || !['tree', 'chat'].includes(State.currentView));
    _scheduleCanvasRevision();
}

async function toggleCanvas() {
    if (!State.currentConvId || !State.currentConv) return;
    const isEnabled = State.currentConv.canvas_enabled;
    const newState = !isEnabled;
    const convId = State.currentConvId;
    try {
        const resp = await API.post(`/api/conversations/${convId}/canvas`, { enabled: newState });
        if (!_sameConvId(State.currentConvId, convId)) return;
        State.currentConv.canvas_enabled = newState ? 1 : 0;
        if (resp?.project_dir) State.currentConv.project_dir = resp.project_dir;
        if (resp && resp.canvas_slug) State.currentConv.canvas_slug = resp.canvas_slug;
        const btn = document.getElementById('btn-canvas-toggle');
        if (btn) btn.classList.toggle('active', newState);
        updateCanvasSlugPill();
        // Re-render tree without resetting camera position
        if (typeof TREE !== 'undefined') TREE._skipCenter = true;
        if (typeof renderTree === 'function') renderTree();
        _scheduleCanvasRevision();
        if (typeof scheduleWorkspaceRender === 'function') scheduleWorkspaceRender();
    } catch (e) {
        console.error('Canvas toggle failed:', e);
        showToast(e.detail || e.message || 'Could not change Canvas settings', 'error');
    }
}

function updateCanvasVisibility() {
    const btn = document.getElementById('btn-canvas-toggle');
    if (!btn) return;
    const hasConv = !!State.currentConv;
    const showBtn = hasConv && ['tree', 'chat'].includes(State.currentView);
    btn.classList.toggle('hidden', !showBtn);
    if (btn) btn.classList.toggle('active', !!(hasConv && State.currentConv?.canvas_enabled));
    btn.textContent = State.currentConv?.canvas_enabled ? 'Disable Canvas' : 'Enable Canvas';
    // Show/hide canvas focus button in tree toolbar
    const focusBtn = document.getElementById('btn-canvas-focus');
    if (focusBtn) focusBtn.classList.toggle('hidden', !(showBtn && State.currentConv?.canvas_enabled));
    // Close fullview if canvas was disabled
    if (!State.currentConv?.canvas_enabled) closeCanvasFullview();
    _scheduleCanvasRevision();
}

document.getElementById('btn-canvas-toggle')?.addEventListener('click', toggleCanvas);

// ── Canvas Slug Pill ──
// Copy-to-clipboard pill showing the direct-access canvas URL (e.g. dark-penguin-x7k2)

function updateCanvasSlugPill() {
    const pill = document.getElementById('canvas-slug-pill');
    if (!pill) return;
    const slug = State.currentConv?.canvas_slug;
    const enabled = !!State.currentConv?.canvas_enabled;
    const toggle = document.getElementById('btn-canvas-toggle');
    if (toggle) toggle.textContent = enabled ? 'Disable Canvas' : 'Enable Canvas';
    if (slug && enabled) {
        _canvasShareLink(State.currentConv).catch(() => {});
        pill.textContent = '🔗 ' + slug;
        pill.classList.remove('hidden');
        pill.onclick = () => {
            copyCanvasLink().then(copied => {
                if (!copied || State.currentConv?.canvas_slug !== slug) return;
                pill.textContent = 'Copied!';
                setTimeout(() => {
                    if (State.currentConv?.canvas_slug === slug) pill.textContent = '🔗 ' + slug;
                }, 1500);
            });
        };
    } else {
        pill.classList.add('hidden');
    }
}

const _canvasShareLinks = new Map();

function _canvasShareLink(conv) {
    const key = `${conv.id}:${conv.canvas_slug}`;
    if (!_canvasShareLinks.has(key)) {
        const request = API.get(`/api/canvas/${conv.id}/share-link`).then(data => {
            if (!data.url) throw new Error('Canvas sharing endpoint unavailable; reload Main through Admin');
            return data.url;
        }).catch(error => {
            _canvasShareLinks.delete(key);
            throw error;
        });
        _canvasShareLinks.set(key, request);
    }
    return _canvasShareLinks.get(key);
}

async function copyCanvasLink() {
    const conv = State.currentConv;
    if (!conv?.canvas_enabled || !conv.canvas_slug) return false;
    try {
        const url = _canvasShareLink(conv);
        // Pass the promise during the click gesture; Safari expires activation
        // when a network lookup is awaited before invoking the clipboard API.
        if (navigator.clipboard?.write && typeof ClipboardItem !== 'undefined') {
            try {
                await navigator.clipboard.write([new ClipboardItem({'text/plain':
                    url.then(text => new Blob([text], {type: 'text/plain'}))})]);
            } catch (_) {
                const text = await url;
                const field = document.createElement('textarea');
                field.value = text;
                field.style.cssText = 'position:fixed;opacity:0';
                document.body.appendChild(field);
                field.select();
                const copied = document.execCommand('copy');
                field.remove();
                if (!copied) { showCopyLinkDialog(text); return false; }
            }
        } else {
            const text = await url;
            try { await navigator.clipboard.writeText(text); }
            catch (_) {
                const field = document.createElement('textarea');
                field.value = text;
                field.style.cssText = 'position:fixed;opacity:0';
                document.body.appendChild(field);
                field.select();
                const copied = document.execCommand('copy');
                field.remove();
                if (!copied) { showCopyLinkDialog(text); return false; }
            }
        }
        showToast('Canvas tailnet link copied');
        return true;
    } catch (error) {
        showToast(error.status === 404
            ? 'Canvas sharing endpoint is not loaded. Reload Main through Admin, then refresh this page.'
            : error.detail || error.message || 'Could not copy Canvas tailnet link', 'error');
        return false;
    }
}

// Update pill visibility when conversation or view changes
const _origUpdateCanvasVis = updateCanvasVisibility;
updateCanvasVisibility = function () {
    _origUpdateCanvasVis();
    updateCanvasSlugPill();
};

// ── Canvas postMessage Bridge ──
// Allows canvas iframes to trigger Loom actions via window.parent.postMessage
const _canvasRequests = new WeakMap();

function _canvasMessageFrame(e, convId) {
    if (e.origin !== location.origin || !State.currentConv?.canvas_enabled) return null;
    return [...document.querySelectorAll('#canvas-fullview-iframe, .canvas-node-iframe, #workspace-canvas-iframe, #workspace-canvas-pane-iframe')]
        .find(frame => frame.contentWindow === e.source && frame.isConnected
            && frame.src.startsWith(`${location.origin}/api/canvas/${convId}/`));
}

window.addEventListener('message', async (e) => {
    // Only accept messages from our own canvas iframes
    if (!e.data || e.data.source !== 'loom-canvas') return;
    const convId = State.currentConvId;
    if (!convId || !_canvasMessageFrame(e, convId)) return;
    const reply = data => e.source.postMessage({
        source: 'loom-host', request_id: e.data.request_id, ...data,
    }, location.origin);

    if (e.data.type === 'send-message') {
        let requests = _canvasRequests.get(e.source);
        if (!requests) { requests = new Map(); _canvasRequests.set(e.source, requests); }
        const requestId = String(e.data.request_id || (globalThis.crypto?.randomUUID
            ? crypto.randomUUID() : `canvas-${Date.now()}-${Math.random().toString(36).slice(2)}`));
        // Repeated delivery reuses the same operation and response, not a new cursor write.
        if (requests.has(requestId)) { reply(await requests.get(requestId).promise); return; }
        for (const [id, request] of requests) {
            if (requests.size < 64) break;
            if (request.done) requests.delete(id);
        }
        if (requests.size >= 64) {
            reply({ type: 'error', error: 'Too many pending Canvas requests; wait for them to finish.' });
            return;
        }
        const operation = (async () => {
          try {
            const queueParentId = e.data.parent_id === undefined && !e.data.parallel
                ? _activeQueueParentId() : null;
            const parentId = e.data.parent_id !== undefined
                ? e.data.parent_id
                : (queueParentId ?? _currentVisibleLeafId() ?? null);
            const topologyIntent = e.data.parallel ? 'branch' : 'continue';
            const payload = {
                role: 'user',
                content: e.data.content || '',
                send_intent: e.data.parallel ? 'parallel' : 'continue',
                generation_count: 1,
                launch_generation: true,
                client_request_id: `canvas-${requestId}`,
            };
            if (queueParentId != null) payload.queue_after_draft_id = queueParentId;
            _attachCCSettings(payload);
            Object.assign(
                payload,
                await _messageTopologyContract(parentId, topologyIntent, convId),
            );
            if (!_sameConvId(State.currentConvId, convId) || !_canvasMessageFrame(e, convId)) {
                throw new Error('Conversation changed before Canvas could send. No message was sent.');
            }
            if (e.data.image_paths) payload.image_path = e.data.image_paths;
            const resp = await fetch(`/api/conversations/${convId}/messages`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (!resp.ok) throw await apiError(resp, 'POST', `/api/conversations/${convId}/messages`);
            const msg = await resp.json();
            if (msg.id) {
                const sameConv = _sameConvId(State.currentConvId, convId);
                if (sameConv) _acceptMessageCursor(msg, convId);
                if (msg.queued && sameConv) {
                    _queuedGeneration = msg;
                    rememberConversationLeaf(convId, msg.id);
                    if (_sameConvId(State.currentConvId, convId)) await loadMessages(convId);
                } else if (!Array.isArray(msg.generation_ids)
                    && _sameConvId(State.currentConvId, convId)) {
                    _triggerParallelGenerate(1, msg.id, {
                        convId,
                        threadId: msg.loom_thread_id,
                    });
                }
                return { type: 'message-sent', id: msg.id, queued: !!msg.queued };
            }
            throw new Error('Canvas send returned no message ID.');
          } catch (err) {
            return { type: 'error', error: err.message };
          }
        })();
        const request = { promise: operation, done: false };
        requests.set(requestId, request);
        const result = await operation;
        request.done = true;
        reply(result);
    } else if (e.data.type === 'get-conv-id') {
        reply({ type: 'conv-id', convId });
    }
});

// ── Code Block Preview ──

function toggleCodePreview(wrapper, btn) {
    const existing = wrapper.querySelector('.code-preview-panel');
    if (existing) { existing.remove(); btn.textContent = 'Preview'; return; }
    btn.textContent = 'Close';
    const codeEl = wrapper.querySelector('pre code');
    if (!codeEl) return;
    const rawCode = codeEl.textContent;
    const isSvg = (codeEl.className || '').includes('language-svg');

    const panel = document.createElement('div');
    panel.className = 'code-preview-panel';
    const toolbar = document.createElement('div');
    toolbar.className = 'code-preview-toolbar';
    toolbar.innerHTML = '<span class="code-preview-label">Preview</span>' +
        '<button class="code-action-btn" data-code-action="popout" title="Open larger">Popout</button>';
    panel.appendChild(toolbar);

    const iframe = document.createElement('iframe');
    iframe.className = 'code-preview-iframe';
    iframe.sandbox = 'allow-scripts';
    panel.appendChild(iframe);
    wrapper.appendChild(panel);

    const doc = iframe.contentDocument || (iframe.contentWindow && iframe.contentWindow.document);
    if (!doc) return;
    doc.open();
    if (isSvg) {
        doc.write('<!DOCTYPE html><html><head><style>body{margin:0;background:#1a1a2e;display:flex;align-items:center;justify-content:center;min-height:100vh;}</style></head><body>' + rawCode + '</body></html>');
    } else {
        doc.write(rawCode);
    }
    doc.close();
    scrollToBottom();
}

function openPreviewModal(wrapper) {
    const codeEl = wrapper.querySelector('pre code');
    if (!codeEl) return;
    const rawCode = codeEl.textContent;
    const isSvg = (codeEl.className || '').includes('language-svg');

    const modal = document.getElementById('modal-preview');
    const body = document.getElementById('preview-modal-body');
    modal.classList.remove('hidden');
    body.innerHTML = '';

    const iframe = document.createElement('iframe');
    iframe.className = 'preview-modal-iframe';
    iframe.sandbox = 'allow-scripts';
    body.appendChild(iframe);

    const doc = iframe.contentDocument || (iframe.contentWindow && iframe.contentWindow.document);
    if (!doc) { modal.classList.add('hidden'); return; }
    doc.open();
    if (isSvg) {
        doc.write('<!DOCTYPE html><html><head><style>body{margin:0;background:#1a1a2e;display:flex;align-items:center;justify-content:center;min-height:100vh;}</style></head><body>' + rawCode + '</body></html>');
    } else {
        doc.write(rawCode);
    }
    doc.close();
}
function previewArtifact(filename, content) {
    const modal = document.getElementById('modal-preview');
    const title = modal.querySelector('h2');
    if (title) title.textContent = filename;
    const body = document.getElementById('preview-modal-body');
    modal.classList.remove('hidden');
    body.innerHTML = `<div class="markdown-body" style="padding:20px;overflow-y:auto;max-height:75vh;color:var(--text-primary)">`
        + formatContent(content)
        + `</div>`;
}


// ── Event Delegation for Tool Blocks + Thinking + Code Actions ──
const _fileCopyFeedbackTimers = new WeakMap();
async function copyFileReference(fileRef) {
    try {
        await navigator.clipboard.writeText(fileRef.dataset.filePath);
        clearTimeout(_fileCopyFeedbackTimers.get(fileRef));
        let feedback = fileRef.querySelector('.file-copy-feedback');
        if (!feedback) {
            feedback = document.createElement('span');
            feedback.className = 'file-copy-feedback';
            feedback.setAttribute('role', 'status');
            fileRef.appendChild(feedback);
        }
        feedback.textContent = ' ✓ Path copied';
        _fileCopyFeedbackTimers.set(fileRef, setTimeout(() => {
            feedback.remove();
            _fileCopyFeedbackTimers.delete(fileRef);
        }, 2200));
        showToast('Path copied: ' + fileRef.dataset.filePath);
    } catch {
        showToast('Could not copy the path', 'error');
    }
}
document.addEventListener('keydown', event => {
    const fileRef = event.target.closest('.file-ref[data-file-path]');
    if (fileRef && (event.key === 'Enter' || event.key === ' ')) {
        event.preventDefault();
        fileRef.click();
    }
});
document.addEventListener('click', (e) => {
    // File reference chip: copy the full path
    const fileRef = e.target.closest('.file-ref[data-file-path]');
    if (fileRef) {
        e.preventDefault();
        e.stopPropagation();
        void copyFileReference(fileRef);
        return;
    }
    // Code block: Copy
    const copyBtn = e.target.closest('[data-code-action="copy"]');
    if (copyBtn) {
        e.stopPropagation();
        const wrapper = copyBtn.closest('.code-block-wrapper');
        const codeEl = wrapper && wrapper.querySelector('pre code');
        if (codeEl) {
            navigator.clipboard.writeText(codeEl.textContent).then(
                () => { copyBtn.textContent = 'Copied!'; setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1500); },
                () => { copyBtn.textContent = 'Failed'; }
            );
        }
        return;
    }
    // Code block: Preview toggle
    const previewBtn = e.target.closest('[data-code-action="preview"]');
    if (previewBtn) {
        e.stopPropagation();
        const wrapper = previewBtn.closest('.code-block-wrapper');
        if (wrapper) toggleCodePreview(wrapper, previewBtn);
        return;
    }
    // Code block: Popout to modal
    const popoutBtn = e.target.closest('[data-code-action="popout"]');
    if (popoutBtn) {
        e.stopPropagation();
        const wrapper = popoutBtn.closest('.code-block-wrapper');
        if (wrapper) openPreviewModal(wrapper);
        return;
    }
    // Close preview modal
    if (e.target.closest('[data-close-modal-preview]')) {
        document.getElementById('modal-preview').classList.add('hidden');
        document.getElementById('preview-modal-body').innerHTML = '';
        return;
    }
    // Click delegation for .artifact-link
    const artifactLink = e.target.closest('.artifact-link');
    if (artifactLink) {
        e.preventDefault();
        e.stopPropagation();
        const path = artifactLink.getAttribute('data-path');
        const name = artifactLink.getAttribute('data-name');
        if (State.currentConvId && path) {
            fetch(`/api/conversations/${State.currentConvId}/file?path=${encodeURIComponent(path)}`)
                .then(r => {
                    if (!r.ok) throw new Error(r.statusText);
                    return r.text();
                })
                .then(content => {
                    previewArtifact(name, content);
                })
                .catch(err => showToast('Failed to load artifact: ' + err, 'error'));
        }
        return;
    }
    // Tool block expand/collapse
    const header = e.target.closest('[data-tool-toggle]');
    if (header) {
        e.stopPropagation();
        const block = header.closest('.tool-block');
        if (block) {
            block.classList.toggle('expanded');
            const toggle = header.querySelector('.tool-toggle');
            if (toggle) toggle.textContent = block.classList.contains('expanded') ? '▾' : '▸';
        }
        return;
    }
    // Thinking block expand/collapse
    const thinkToggle = e.target.closest('.cc-thinking-toggle');
    if (thinkToggle) {
        const content = thinkToggle.nextElementSibling;
        if (content) content.style.display = content.style.display === 'none' ? 'block' : 'none';
    }
});
