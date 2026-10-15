"""A landing reply must preserve explicit navigation within its own thread."""
from tests.test_agent_activity_browser import activity_browser


async def test_terminal_wait_rechecks_selected_sibling(activity_browser):
    page, conv, *_ = activity_browser
    result = await page.evaluate("""async convId => {
        State.currentConvId = convId;
        State.loomCursor = {thread_id: 'same-thread', leaf_message_id: 10};
        let release;
        State._cursorSyncPromise = new Promise(resolve => { release = resolve; });
        const originalSelect = selectServerLoomCursor;
        const originalRemember = rememberConversationLeaf;
        const writes = [];
        selectServerLoomCursor = async (...args) => { writes.push(['cursor', ...args]); };
        rememberConversationLeaf = (...args) => { writes.push(['url', ...args]); };
        try {
            const settlement = _syncTerminalLoomCursor({conv_id: convId,
                message: {id: 11, parent_id: 10, loom_thread_id: 'same-thread'}});
            State.loomCursor = {thread_id: 'same-thread', leaf_message_id: 20};
            release();
            await settlement;
            return {writes, leaf: State.loomCursor.leaf_message_id};
        } finally {
            selectServerLoomCursor = originalSelect;
            rememberConversationLeaf = originalRemember;
            State._cursorSyncPromise = null;
        }
    }""", conv['id'])
    assert result == {'writes': [], 'leaf': 20}


async def test_terminal_on_selected_parent_still_advances(activity_browser):
    page, conv, *_ = activity_browser
    writes = await page.evaluate("""async convId => {
        State.currentConvId = convId;
        State.loomCursor = {thread_id: 'same-thread', leaf_message_id: 10};
        State._cursorSyncPromise = null;
        const originalSelect = selectServerLoomCursor;
        const originalRemember = rememberConversationLeaf;
        const writes = [];
        selectServerLoomCursor = async (conv, leaf) => { writes.push(['cursor', leaf]); };
        rememberConversationLeaf = (conv, leaf) => { writes.push(['url', leaf]); };
        try {
            await _syncTerminalLoomCursor({conv_id: convId,
                message: {id: 11, parent_id: 10, loom_thread_id: 'same-thread'}});
            return writes;
        } finally {
            selectServerLoomCursor = originalSelect;
            rememberConversationLeaf = originalRemember;
            State._cursorSyncPromise = null;
        }
    }""", conv['id'])
    assert writes == [['url', 11], ['cursor', 11]]
