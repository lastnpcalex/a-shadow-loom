/* Decorative canvases retain the original accessible heading text and actions. */
(function () {
    'use strict';
    let queued = false;
    const cache = new WeakMap();
    const observer = new ResizeObserver(schedule);
    function schedule() {
        if (queued) return;
        queued = true;
        requestAnimationFrame(() => { queued = false; refresh(); });
    }
    function refresh() {
        if (document.documentElement.dataset.loomTheme !== 'exoloom' || !window.EXOLettering) return;
        for (const heading of document.querySelectorAll('#btn-home, .modal h2, #view-home h1')) {
            heading.classList.add('exo-lettering');
            for (const node of [...heading.childNodes]) {
                if (node.nodeType !== Node.TEXT_NODE || !node.textContent.trim()) continue;
                const span = document.createElement('span'); span.className = 'exo-lettering-text';
                heading.insertBefore(span, node); span.appendChild(node);
                const canvas = document.createElement('canvas'); canvas.setAttribute('aria-hidden','true');
                span.appendChild(canvas); observer.observe(span);
            }
            for (const span of heading.querySelectorAll(':scope > .exo-lettering-text')) {
                const canvas = span.querySelector('canvas');
                const rect = span.getBoundingClientRect();
                if (!canvas || !rect.width || !rect.height) continue;
                const text = [...span.childNodes].filter(n => n.nodeType === Node.TEXT_NODE).map(n => n.textContent).join('').trim();
                const dpr = Math.min(devicePixelRatio, 2), font = parseFloat(getComputedStyle(span).fontSize);
                const signature = `${text}/${rect.width}/${rect.height}/${font}/${dpr}/${document.fonts.status}`;
                if (cache.get(canvas) === signature) continue;
                EXOLettering.draw(canvas, text, Math.ceil(rect.width*dpr), Math.ceil(rect.height*dpr),
                    {size:font/rect.height*100, width:heading.id === 'btn-home' ? 100 : 96});
                cache.set(canvas, signature);
            }
            heading.classList.add('exo-lettering-rendered');
        }
    }
    window.EXOBranding = {refresh:schedule};
    const mutations = new MutationObserver(schedule);
    mutations.observe(document.body, {childList:true,subtree:true,characterData:true});
    document.fonts.load('900 24px Orbitron').then(() => { cache.delete(document.querySelector('#btn-home canvas')); schedule(); });
    schedule();
})();
