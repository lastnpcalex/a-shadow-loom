// The standalone slug has no selected chat cursor. It can identify its Canvas,
// but must not invent a branch or silently launch work in the background.
(() => {
    const frame = document.querySelector('iframe[data-canvas-conv]');
    if (!frame) return;
    window.addEventListener('message', event => {
        if (event.origin !== location.origin || event.source !== frame.contentWindow
            || event.data?.source !== 'loom-canvas') return;
        const data = event.data.type === 'get-conv-id'
            ? { type: 'conv-id', convId: Number(frame.dataset.canvasConv) }
            : { type: 'error', error: 'Open this conversation in Loom to send Canvas messages; the standalone viewer has no selected branch.' };
        event.source.postMessage({ source: 'loom-host', request_id: event.data.request_id, ...data }, location.origin);
    });
})();
