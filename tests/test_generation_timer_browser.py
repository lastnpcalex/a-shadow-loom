"""Elapsed reply time across turns, thinking, and missing reconnect clocks."""
from tests.test_queue_presentation_browser import queue_browser


async def test_timer_lifecycle(queue_browser):
    page, conv, assistant, queued = queue_browser
    result = await page.evaluate("""() => {
        _streamStartTime = Date.now() - 456000;
        _resetStreamState();
        const cleared = _streamStartTime === 0 && _genTimerInterval === null;
        appendStreamingMessage();
        const fresh = streamingDiv.querySelector('.gen-timer').textContent;
        _streamStartTime = Date.now() - 30000;
        _startGenTimer();
        const restored = streamingDiv.querySelector('.gen-timer').textContent;
        State._streamIsOurBranch = true;
        const beforeThinking = _streamStartTime;
        handleWSMessage({type: 'thinking_end'});
        const preserved = _streamStartTime === beforeThinking;
        _clearTerminalStreamState();
        const stopped = _streamStartTime === 0 && _genTimerInterval === null;
        removeStreamingMessage();
        return {cleared, fresh, restored, preserved, stopped};
    }""")
    assert result == dict(cleared=True, fresh="0:00", restored="0:30", preserved=True, stopped=True)


async def test_preparing_new_run_does_not_inherit_previous_clock(queue_browser):
    page, conv, assistant, queued = queue_browser
    result = await page.evaluate("""() => {
        _streamStartTime = Date.now() - 456000;
        State._followingGenId = 'old-timer-run';
        _subscribedRunIds.add('gen:new-timer-run');
        const event = {type: 'generation_preparing', gen_id: 'new-timer-run',
            conv_id: State.currentConvId};
        handleWSMessage(event);
        const start = _streamStartTime;
        const fresh = Date.now() - start < 1000;
        _streamStartTime = start - 30000;
        const repeatedStart = _streamStartTime;
        handleWSMessage(event);
        const preserved = _streamStartTime === repeatedStart;
        removeStreamingMessage();
        _clearTerminalStreamState();
        return {fresh, preserved};
    }""")
    assert result == dict(fresh=True, preserved=True)
