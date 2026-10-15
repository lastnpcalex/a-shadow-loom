/* The original exported portal preset and renderer, isolated from legacy Loom. */
(async function () {
    'use strict';
    let origin = location.origin;
    const admin = new URLSearchParams(location.search).get('admin');
    if (admin) {
        try { const url = new URL(admin); if (url.hostname === location.hostname && url.protocol === location.protocol && url.port === '3002') origin = url.origin; } catch {}
    }
    let viewer, pending, applied;
    const palettes = {cyan:'#00ffff',violet:'#c084fc',ember:'#ffad66',crimson:'#ff526d',mint:'#68ffbb'};
    function apply(value) {
        if (!viewer) { pending = value; return; }
        const next = structuredClone(window.NEON_DEFAULT_SETTINGS);
        // A background shares the GPU with chat and must adapt before it stalls input.
        next.look.auto_res = true;
        next.look.render_scale = 1;
        for (const [path,item] of Object.entries(value.profile || {})) {
            const keys = path.split('.');
            if (keys.length === 1 && Object.hasOwn(next,path)) next[path] = item;
            else if (keys.length === 2 && ['observer','camera','look'].includes(keys[0]) && Object.hasOwn(next[keys[0]],keys[1])) next[keys[0]][keys[1]] = item;
        }
        if (value.grid === true) next.neon_grid = true;
        if (value.floor === false) next.neon_floor = false;
        if (value.stationary === true) next.observer.motion = false;
        window.EXO_FRAME_LIMIT = [20,30,60].includes(value.fps) ? value.fps : 30;
        next.look.target_fps = Math.max(24, window.EXO_FRAME_LIMIT);
        if (Object.hasOwn(palettes, value.color)) {
            const color = palettes[value.color];
            for (const field of ['floor_color','floor_major_color','hole_glow_color','gas_color','sky_plume_color']) next.look[field] = color;
        }
        const signature = JSON.stringify(next);
        if (signature !== applied) { viewer.applySettings(next, true); applied = signature; draw(); }
        viewer.setActive(value.active === true && !document.hidden);
    }
    function draw() {
        if (!viewer) return;
        const caption = viewer.params.caption;
        EXOLettering.draw(document.getElementById('lettering'), caption.text, innerWidth, innerHeight, caption);
    }
    window.addEventListener('message', event => {
        if (event.source !== parent || event.origin !== origin || event.data?.type !== 'exo-scene-settings') return;
        const value = event.data.settings;
        if (value && typeof value === 'object') apply(value);
    });
    window.addEventListener('resize', draw);
    try {
        // Bundle the same Orbitron font in the frame, including offline operation.
        const font = new FontFace('Orbitron', window.NEON_ORBITRON_URL, {weight:'400 900'});
        document.fonts.add(font); await font.load();
        viewer = createNeonBlackhole({container:document.getElementById('scene'),base:new URL('./',location.href).href,
            skyMotion:true,antialiasing:true,settings:window.NEON_DEFAULT_SETTINGS});
        window.NeonViewer = viewer;
        await viewer.ready; await viewer.textureReady;
        if (pending) apply(pending); else draw();
        parent.postMessage({type:'exo-scene-ready'}, origin);
    } catch (error) {
        viewer?.setActive(false);
        parent.postMessage({type:'exo-scene-unavailable'}, origin);
        console.warn('Exoloom renderer unavailable; the supplied artwork remains visible.', error);
    }
})();
