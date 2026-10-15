"""Static UI invariants for tree-level branch sends."""

import re
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def test_tree_view_composer_creates_a_server_owned_root_thread():
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")

    tree_view = re.search(
        r"else if \(view === 'tree'\) \{(?P<body>.*?)\n\s*\} else if \(view === 'chat'\)",
        app_source,
        re.S,
    )
    assert tree_view, "tree view switch branch not found"
    assert "inputArea?.classList.remove('hidden')" in tree_view.group("body")
    assert "Start a new thread" in tree_view.group("body")
    assert "Choose a branch to open in chat, or use + Thread" not in chat_source
    assert "if (isFromTree)" in chat_source
    assert "await createServerLoomThreadCursor(sendConvId)" in chat_source
    assert "isNewRootThread = true" in chat_source
    assert "await switchToBranch(msg.id, msg.id, { exact: true })" in chat_source
    assert "_isolateTreeRootLiveStart(msg)" in chat_source
    assert "State.messages = matchingDraft ? [message, matchingDraft] : [message]" in chat_source
    assert "isNewRootThread ? 'new_thread' : 'continue'" in chat_source
    assert "State._skipLoadOnChat = true" in chat_source
    assert "switchView('chat')" in chat_source
    assert "function _getTreePromptParentId()" not in chat_source


def test_new_thread_send_has_explicit_root_topology():
    source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")

    assert "let isNewRootThread = isNewConversationThread(sendConvId)" in source
    assert "send_intent: isNewRootThread ? 'new_thread' : 'continue'" in source
    assert "targetParentId = null" in source
    assert "isNewRootThread ? 'new_thread' : 'continue'" in source
    assert "cursor.leaf_message_id != null" in source
    assert "loom_thread_id: cursor.thread_id" in source


def test_chat_send_uses_visible_leaf_as_parent():
    source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")
    assert "const visibleLeafAtStart = _currentVisibleLeafId()" in source
    assert "targetParentId = visibleLeafAtStart || null" in source
    assert "await _messageTopologyContract(" in source
    assert "another agent may update the" in source
    assert "_acceptMessageCursor(msg, sendConvId)" in source
    assert "launch_generation: true" in source
    assert "API.post(`/api/conversations/${sendConvId}/messages`" in source
    assert "showToast(err.detail || err.message || 'Failed to send message', 'error')" in source


def test_composer_and_send_route_are_bound_to_the_exact_viewport():
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")
    send_body = re.search(
        r"async function sendMessage\(\) \{(?P<body>.*?)\n\}\n\nasync function cancelQueuedMessage",
        chat_source,
        re.S,
    )
    assert send_body
    body = send_body.group("body")
    assert "const sendConvId = State.currentConvId" in body
    assert "launch_generation: true" in body
    assert "API.post(`/api/conversations/${sendConvId}/messages`" in body
    assert "if (!msg.queued && !Array.isArray(msg.generation_ids))" in body
    assert "convId: sendConvId" in body
    assert "threadId: msg.loom_thread_id" in body
    assert "function _composerDraftStorageKey" in app_source
    assert "saveComposerDraft();" in app_source
    assert "restoreComposerDraft(convId, State.loomCursor)" in app_source


def test_queued_message_follows_running_assistant_instead_of_branching():
    source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")
    server_source = (REPO / "server.py").read_text(encoding="utf-8")
    database_source = (REPO / "database.py").read_text(encoding="utf-8")

    assert "function _activeQueueParentId()" in source
    assert "State._followingDraftMsgId" in source
    assert "targetParentId = queueParentId" in source
    assert "msgData.queue_after_draft_id = queueParentId" in source
    assert "pre-stream visible leaf here makes the user prompt and assistant" in source
    assert "One message is already queued" in source
    assert "if (msg.queued)" in source
    assert "function _flushQueuedGeneration" not in source
    assert 'CREATE TABLE IF NOT EXISTS queued_generations' in database_source
    assert 'type": "queue_started"' in server_source
    assert "_dispatch_ready_queued_generations" in server_source
    assert "message.queued = false" in source
    assert "_syncQueuedMessagePresentation(queuedEl, { queued: false })" in source
    assert "row.classList.toggle('queued-message', queued)" in source
    assert "function _placeQueuedMessageAfterDraft(message)" in source
    assert "messages.splice(draftIndex + 1, 0, queued)" in source
    assert "function _placeQueuedMessageAfterDraftInDom(message)" in source
    assert "draftEl.after(queuedEl)" in source
    assert "const streamingMsg = State.isStreaming" in source


def test_conversation_load_always_releases_blocking_overlay():
    source = (REPO / "static" / "app.js").read_text(encoding="utf-8")
    start = source.index("async function loadConversation(")
    end = source.index("async function createConversation()", start)
    body = source[start:end]

    assert "showLoading();\n    try {" in body
    assert "} finally {" in body
    assert "if (requestSeq === _conversationLoadRequestSeq) hideLoading();" in body


def test_parallel_ws_generate_requires_explicit_parent():
    source = (REPO / "server.py").read_text(encoding="utf-8")
    assert 'parallel_same_checkout and action == "generate" and "parent_id" not in data' in source
    assert "parent_id is required when parallel agents are enabled" in source


def test_stale_manual_generate_preserves_exact_route_across_reconnect():
    source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")
    match = re.search(r"function _triggerParallelGenerate\(.*?(?P<body>\{.*?)\n\}\n", source, re.S)
    assert match, "acknowledged generation launch function not found"
    body = match.group("body")

    assert "parentId: targetParentId, packet, owner" in body
    assert "_pendingGenerates.set(key, operation)" in body
    assert "_openGenerationLaunchSocket(operation)" in body
    assert "ws.close()" not in body
    assert "The selected Loom thread changed" in source


def test_tree_keeps_run_control_without_obsolete_prompt_button():
    source = (REPO / "static" / "tree.js").read_text(encoding="utf-8")

    assert "tree-node-send-here-btn" not in source
    assert "focusTreePromptParent" not in source
    assert "tree-node-run-btn" in source
    assert "runTreeUserMessage(data)" in source
    assert "switchToBranch(data.id, data.id, { exact: true })" in source
    assert "_triggerParallelGenerate(count, data.id)" in source


def test_chat_layout_keeps_composer_and_latest_control_aligned():
    source = (REPO / "static" / "style.css").read_text(encoding="utf-8")
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")

    assert "#view-chat {\n    flex-direction: column;\n    position: relative;" in source
    assert "left: 50%;" in source
    assert "transform: translateX(-50%);" in source
    assert "#input-row {\n    display: flex;" in source
    assert "min-height: 42px;" in source
    assert "ta.offsetWidth <= 0 || ta.getClientRects().length === 0" in app_source
    assert "requestAnimationFrame(() => autoResizeTextarea())" in app_source


def test_large_tree_refreshes_are_single_flight_and_initial_data_is_marked_fresh():
    tree_source = (REPO / "static" / "tree.js").read_text(encoding="utf-8")
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")

    assert "treeFetchConvId: null" in tree_source
    assert "TREE.treeFetchConvId === convId" in tree_source
    assert "TREE.treeFetchPromise === fetchPromise" in tree_source
    assert "markTreeDataFresh(convId)" in app_source
    assert "if (refreshTree._pending) return refreshTree._pending" in chat_source
    assert "await refreshTreeData({ force: true })" in chat_source


def test_tree_run_uses_exact_branch_snapshot_for_stream_parent():
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")

    assert "async function switchToBranch(leafId, scrollToMsgId, options = {})" in chat_source
    assert "options.exact" in chat_source
    assert "const convId = State.currentConvId;" in chat_source
    assert "API.get(`/api/conversations/${convId}/branch/${leafId}`)" in chat_source
    assert "State._branchSwitchSeq !== switchSeq || !_sameConvId(State.currentConvId, convId)" in chat_source


def test_branch_path_bar_navigates_exactly_and_loads_conversation_scoped_pins():
    index_source = (REPO / "static" / "index.html").read_text(encoding="utf-8")
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="branch-path-input"' in index_source
    assert 'id="btn-pin-current-branch"' in index_source
    assert 'id="branch-pins-panel"' in index_source
    assert "function resolveBranchPath(value)" in app_source
    assert "switchToBranch(messageId, messageId, { exact: true })" in app_source
    assert "API.get(`/api/conversations/${convId}/branch-pins`)" in app_source
    assert "State.branchPins = branchPins || []" in app_source


def test_pinning_live_draft_refreshes_branch_path_map_before_rendering():
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")

    assert "async function refreshBranchPinPath(messageId)" in app_source
    assert "await API.get(`/api/conversations/${convId}/tree`)" in app_source
    assert "State.branchNames = computeBranchNames(roots, nodeMap, childrenMap)" in app_source
    assert "await refreshBranchPinPath(saved.message_id)" in app_source


def test_copied_root_breadcrumb_keeps_its_resolvable_computed_label():
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")

    assert "label: rootLabel, displayLabel: 'root'" in app_source
    assert "const fullPath = messageBranchPath(lastMsg.id)" in app_source


def test_tree_delete_confirm_uses_loom_modal():
    source = (REPO / "static" / "tree.js").read_text(encoding="utf-8")
    assert "showLoomConfirm({" in source
    assert "Delete branch?" in source
    assert "confirm(" not in source


def test_main_ui_uses_loom_confirm_instead_of_native_confirm():
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")
    tree_source = (REPO / "static" / "tree.js").read_text(encoding="utf-8")

    assert "function showLoomConfirm" in app_source
    assert "confirm(" not in app_source
    assert "confirm(" not in tree_source


def test_parallel_agents_same_checkout_ui_is_explicit():
    index_source = (REPO / "static" / "index.html").read_text(encoding="utf-8")
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="parallel-agents-enabled"' in index_source
    assert 'id="cfg-parallel-agents-enabled"' in index_source
    assert "Parallel agents in same checkout" in index_source
    assert "parallel_agents_enabled" in app_source


def test_parallel_branch_loading_keeps_an_exact_window_cursor():
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")

    assert "sessionStorage.setItem(_tabBranchKey(convId), String(leafId))" in app_source
    assert "if (remembered) return remembered" in app_source
    assert "findDescendantBranchLeaf" not in app_source
    assert "Only explicit navigation and a generation this" in app_source
    assert "findLoadableConversationLeaf(treeData, convId)" in app_source
    assert "cursor.leaf_message_id" in chat_source
    assert re.search(r"API\.get\(\s*`/api/conversations/\$\{convId\}/cursor/", chat_source)
    assert "rememberConversationLeaf(State.currentConvId, loadedLeaf.id)" in chat_source


def test_parallel_reconnect_uses_only_server_authorized_snapshot_content():
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")

    assert "Object.prototype.hasOwnProperty.call(s, 'full_text')" in chat_source
    assert "if (snap.run_id) {" in chat_source
    assert "_subscribedRunIds.add(String(snap.run_id))" in chat_source
    assert "State._followingGenId = snap.gen_id ?? null" in chat_source
    assert "snapshots.find(s => _generationWindowOwnership(s) !== false" not in chat_source


def test_parallel_stream_ownership_never_uses_visible_leaf_as_authority():
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")

    assert "const windowOwnership = _generationWindowOwnership(data)" in chat_source
    assert "data.type === 'stream_start' || data.type === 'generation_preparing'" in chat_source
    assert "const shouldFollow = exactRunOwned && (!State.isStreaming || pendingMatch)" in chat_source
    assert "const isOnOurBranch = windowOwnership !== false" not in chat_source
    assert "if (_OWNED_STREAM_EVENT_TYPES.has(data.type) && data.gen_id != null) return false" in chat_source
    assert "const targetParentId = parentId ?? _currentVisibleLeafId()" in chat_source


def test_parallel_generation_events_are_scoped_to_exact_run_subscriptions():
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")
    server_source = (REPO / "server.py").read_text(encoding="utf-8")

    assert "const _loomClientInstanceId" in chat_source
    assert "new URLSearchParams({ client_instance_id: _loomClientInstanceId })" in chat_source
    assert "wsParams.set('visible_leaf_id'" not in chat_source
    assert "client_instance_id: _loomClientInstanceId" in chat_source
    assert "function _generationWindowOwnership(data)" in chat_source
    assert "if (_generationWindowOwnership(data) === false) return false" in chat_source
    assert 'websocket.query_params.get("client_instance_id")' in server_source
    assert "task._client_instance_id = client_instance_id" in server_source
    assert 'tagged.setdefault("client_instance_id", client_instance_id)' in server_source
    assert "_event_router.recipients(envelope, clients)" in server_source
    assert "Never derive a subscription from the task's origin socket" in server_source
    assert "async def _websocket_can_subscribe_run(" in server_source
    assert 'str(cursor.get("thread_id")) == str(thread_id)' in server_source
    assert "browser focus must never decide" in chat_source
    assert "await _subscribe_cursor_generation(websocket, conv_id)" in server_source
    assert "// Refresh tree to show ghost/draft nodes for all parallel generations\n            refreshTree();" in chat_source


def test_search_ignores_stale_async_results():
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")
    tree_source = (REPO / "static" / "tree.js").read_text(encoding="utf-8")

    assert "let _homeSearchRequestSeq = 0" in app_source
    assert "requestSeq !== _homeSearchRequestSeq" in app_source
    assert "requestSeq !== TREE.searchRequestSeq" in tree_source
    assert "State.currentConvId !== convId" in tree_source


def test_conversations_can_open_in_independent_addressable_windows():
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")

    assert 'class="char-action-btn conv-window-btn"' in app_source
    assert "conversationWindowUrl(conv.id, { newThread: parallelAgents })" in app_source
    assert "function conversationWindowUrl(convId, { newThread = false } = {})" in app_source
    assert "url.searchParams.set('thread', 'new')" in app_source
    assert "const NEW_THREAD_CURSOR = '__new_thread__'" in app_source
    assert "if (rememberedId === NEW_THREAD_CURSOR) return null" in app_source
    assert "if (options.newThread) rememberNewConversationThread(convId)" in app_source
    assert "State.messages = []" in app_source
    assert "const routeConv = parseInt(route.get('conversation'))" in app_source
    assert "const routeNewThread = route.get('thread') === 'new'" in app_source
    assert "initialLeafId: routeBranch || null" in app_source
    assert "localStorage.getItem('loom-last-conv')" not in app_source
    assert "localStorage.getItem('loom-last-view')" not in app_source


def test_new_thread_cursor_survives_reconnect_without_following_global_active_branch():
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")
    index_source = (REPO / "static" / "index.html").read_text(encoding="utf-8")

    assert 'id="btn-new-thread"' in index_source
    assert "await createServerLoomThreadCursor(convId)" in app_source
    assert "State.loomCursor" in app_source
    assert "State._skipLoadOnChat = true" in app_source
    assert "if (cursor.leaf_message_id == null)" in chat_source
    assert "State.messages = [];" in chat_source
    assert "return;" in chat_source


def test_new_thread_cursor_invalidates_stale_conversation_and_branch_loads():
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")

    assert "let _conversationLoadRequestSeq = 0" in app_source
    assert "const requestSeq = ++_conversationLoadRequestSeq" in app_source
    assert "const requestSeq = ++_conversationLoadRequestSeq" in app_source
    assert "if (!loadIsCurrent()) return" in app_source
    assert "await selectServerLoomCursor(convId, leaf.id, treeData)" in app_source
    assert "const cursorAtStart = `${cursor.cursor_id}:${cursor.revision}" in chat_source
    assert "State.loomCursor?.cursor_id" in chat_source
    assert "_refreshQueuedGeneration(convId, cursorAtStart, read)" in chat_source


def test_parallel_stream_finalization_remembers_assistant_draft():
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")

    assert "rememberConversationLeaf(State.currentConvId, data.draft_msg_id)" in chat_source
    assert "rememberConversationLeaf(State.currentConvId, snap.draft_msg_id)" not in chat_source
    assert "Reconnect ownership comes from the server's exact run" in chat_source
    assert "rememberConversationLeaf(State.currentConvId, data.message.id)" in chat_source
    assert "_acceptMessageCursor(msg, sendConvId)" in chat_source


def test_parallel_permission_prompts_are_generation_scoped():
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")

    assert "'permission_request', 'generation_idle'" in chat_source
    assert "function _permissionBelongsToVisibleBranch(data)" in chat_source
    assert "String(data.gen_id) === String(State._followingGenId)" in chat_source
    assert "const permissionIsVisible = _permissionBelongsToVisibleBranch(data)" in chat_source
    assert "genId: data.gen_id" in chat_source
    assert "draftMsgId: data.draft_msg_id" in chat_source
    assert "switchToBranch(targetId, targetId, { exact: true })" in chat_source
    assert "if (!_permissionBelongsToVisibleBranch(data)) continue" in chat_source
    assert "if (!_permissionBelongsToVisibleBranch(data)) return" in chat_source
    assert 'placeholder="Optional note to the agent"' in chat_source
    assert "allow, always, feedback" in chat_source
    assert "resolvePermissionPrompt(data.request_id, data.allowed, data.feedback || '', data.decision_source)" in chat_source


def test_parallel_cancel_is_generation_scoped():
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")

    assert "const genId = State._followingGenId ?? _pendingLaunch?.genId" in chat_source
    assert "if (genId != null) msg.gen_id = genId" in chat_source
    assert "msg.draft_msg_id = draftMsgId" in chat_source
    assert "case 'cancelled':\n            if (!_isOurBranch(data)) break;" in chat_source
    assert "rememberConversationLeaf(State.currentConvId, data.parent_id || data.draft_msg_id)" not in chat_source
    assert "case 'permission_request': {" in chat_source


def test_cancel_and_interrupt_have_distinct_controls_and_events():
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")

    assert "const action = disposition === 'interrupt' ? 'interrupt' : 'cancel'" in chat_source
    assert "function interruptGeneration()" in chat_source
    assert "case 'interrupted':\n            if (!_isOurBranch(data)) break;" in chat_source
    assert "Cancel and discard partial context" in chat_source
    assert "Interrupt and preserve partial context" in chat_source
    assert "Cancel launch; keep the user message" in chat_source
    assert "const stopControls = _pendingLaunch" in chat_source


def test_turn_preparation_is_inline_cancellable_and_visible_in_the_browser_tab():
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")
    index_source = (REPO / "static" / "index.html").read_text(encoding="utf-8")

    assert "case 'generation_preparing':" in chat_source
    assert "appendStreamingMessage(data.cc_model || data.local_model)" in chat_source
    assert 'class="turn-lifecycle-status hidden"' in chat_source
    assert "_pendingLaunch?.genId" in chat_source
    assert "Cancel launch" in chat_source
    assert "conversationTitle ? `EXO: ${conversationTitle}` : 'EX umbra Omnia'" in chat_source
    assert "document.title = base;" in chat_source
    assert "document.title = `${glyph} ${base}`" not in chat_source
    assert "setBrowserTabState('permission')" in chat_source
    assert "setBrowserTabState('done')" in chat_source
    assert 'id="loom-favicon"' in index_source
    assert 'id="generation-status"' not in index_source
    assert 'id="gen-status-cancel"' not in index_source
    assert 'id="streaming-indicator"' not in index_source
    assert "let _pendingTurnStatus = null;" in chat_source
    assert "showGenStatus(pendingStatus.text, pendingStatus.reconnecting);" in chat_source


def test_nonempty_thinking_clears_waiting_for_first_token():
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")
    start = chat_source.index("case 'thinking_chunk':")
    end = chat_source.index("case 'usage':", start)
    thinking_case = chat_source[start:end]

    assert "if (!data.content) break;" in thinking_case
    assert "_removeStreamWaiting();" in thinking_case


def test_clotho_pre_output_status_transitions_to_looming():
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")
    style_source = (REPO / "static" / "style.css").read_text(encoding="utf-8")
    index_source = (REPO / "static" / "index.html").read_text(encoding="utf-8")

    assert "const _CLOTHO_SPINNER_INTERVAL_MS = 115;" in chat_source
    assert "' ═════ ✦ ═════ '" in chat_source
    assert 'stream-waiting-anim clotho-spinner' in chat_source
    stream_start = chat_source.index("function appendStreamingMessage(")
    stream_end = chat_source.index("function _renderWorkspaceNotifItem", stream_start)
    stream_markup = chat_source[stream_start:stream_end]
    footer_at = stream_markup.index('<div class="stream-thinking-footer">')
    status_at = stream_markup.index('<span class="stream-waiting"')
    controls_at = stream_markup.index('stopControls +')
    clotho_at = stream_markup.index('<span class="stream-waiting-anim clotho-spinner"')
    looming_at = stream_markup.index('<span class="looming-phase hidden"')
    assert '<div class="message-content"></div>' in stream_markup
    assert footer_at < status_at < controls_at < clotho_at < looming_at
    assert "waitingText.dataset.statusText = text;" in chat_source
    assert "streamingDiv.querySelector('.looming-phase')?.classList.remove('hidden');" in chat_source
    assert 'class="looming-phase hidden"' in chat_source
    assert 'class="loom-anim" aria-hidden="true"' in chat_source
    assert 'class="looming-ellipsis"' in chat_source
    assert "_removeStreamWaiting();\n            const draft" in chat_source
    assert ".looming-phase" in style_source
    assert "animation: looming-dot" in style_source
    assert "font-family: var(--font-main);" in style_source
    assert "#generation-status" not in style_source
    # Cache keys may advance for unrelated fixes; they must not roll back to
    # a bundle older than the lifecycle behavior asserted above.
    assert int(re.search(r'chat\.js\?v=(\d+)', index_source).group(1)) >= 172
    assert int(re.search(r'style\.css\?v=(\d+)', index_source).group(1)) >= 138
    assert ".message.streaming:has(.stream-waiting) .message-content:empty" in style_source


def test_parallel_activity_tray_uses_exact_branches_without_stream_subscription():
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")
    style_source = (REPO / "static" / "style.css").read_text(encoding="utf-8")
    index_source = (REPO / "static" / "index.html").read_text(encoding="utf-8")

    assert "const _BRANCH_ACTIVITY_SESSION_KEY = 'loom-branch-activity:v1'" in chat_source
    assert "API.get('/api/generations')" in chat_source
    assert "API.get(`/api/conversations/${convId}/generation-queue`)" in chat_source
    assert "row.status === 'queued' || row.status === 'paused'" in chat_source
    assert "switchToBranch(messageId, messageId, { exact: true })" in chat_source
    assert "window.open(_activityBranchUrl" in chat_source
    assert "'_blank', 'noopener'" in chat_source
    assert "markBranchActivitySeen(State.currentConvId, branch.map(message => message.id))" in chat_source
    assert "_appendNotifSection(list, 'New activity'" in chat_source
    assert "setInterval(() =>" in chat_source
    assert "}, 3000);" in chat_source
    assert "subscribe_run" not in chat_source[chat_source.index("async function refreshParallelBranchActivity"):chat_source.index("function addNotification")]
    assert ".parallel-activity-section" in style_source
    assert ".activity-looming .parallel-activity-icon" in style_source
    assert ">Agents</span>" in index_source


def test_subagent_manager_is_a_compact_composer_popover_not_a_chat_row():
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")
    style_source = (REPO / "static" / "style.css").read_text(encoding="utf-8")
    index_source = (REPO / "static" / "index.html").read_text(encoding="utf-8")

    messages = re.search(
        r'<div id="messages-container">(?P<body>.*?)</div>\s*</div>\s*\n\s*<!-- Shared input area',
        index_source,
        re.S,
    )
    assert messages
    assert "agent-activity-panel" not in messages.group("body")
    assert index_source.index('id="canvas-slug-pill"') < index_source.index('id="agent-manager-anchor"')
    assert 'id="btn-agent-manager"' in index_source
    assert 'id="agent-manager-panel"' in index_source
    assert "function _agentManagerTarget()" in chat_source
    assert "placement.insertBefore(anchor, canvas)" in chat_source
    assert "API.get(`/api/conversations/${requestedConv}/agent-runs`)" in chat_source
    assert "void refreshAgentRuns();" in chat_source
    assert "`agent:${run.run_id}`" in chat_source
    assert "`/api/agent-runs/${encodeURIComponent(run.run_id)}/events?after_sequence=${after}&limit=500`" in chat_source
    assert "const verb = cancel ? 'cancel' : 'resume';" in chat_source
    assert "else if (kind === 'agent_paused') run.status = 'paused';" in chat_source
    assert "_ACTIVE_AGENT_RUN_STATUSES" in chat_source
    assert "details.className = 'agent-run-card'" in chat_source
    assert ".agent-manager-panel" in style_source
    assert "width: min(440px, calc(100vw - 32px));" in style_source
    assert ".agent-task" in style_source
    assert "text-overflow: ellipsis;" in style_source


def test_true_mobile_turn_controls_and_keyboard_viewport_are_additive():
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")
    style_source = (REPO / "static" / "style.css").read_text(encoding="utf-8")
    index_source = (REPO / "static" / "index.html").read_text(encoding="utf-8")

    assert "root.classList.toggle('true-mobile', trueMobile);" in app_source
    assert "window.visualViewport.addEventListener('resize', syncTrueMobileViewport);" in app_source
    assert "--loom-visual-viewport-height" in app_source
    assert "--loom-mobile-composer-max-height" in app_source
    assert "html.true-mobile .interrupt-draft-btn::after" in style_source
    assert "content: 'Ⅱ';" in style_source
    assert "html.true-mobile .looming-text" in style_source
    assert "html.true-mobile .looming-ellipsis" in style_source
    assert "html.true-mobile .stream-waiting" in style_source
    assert "html.true-mobile .message-actions" in style_source
    assert "html.true-mobile .looming-phase {\n    min-width: 0;" in style_source
    assert ".model-attestation-slot .model-attestation > summary" in style_source
    assert "html.true-mobile .message.streaming .model-attestation-slot" in style_source
    assert "html.true-mobile.mobile-keyboard-open #user-input" in style_source
    assert "interactive-widget=resizes-content" in index_source
    assert int(re.search(r'app\.js\?v=(\d+)', index_source).group(1)) >= 119
