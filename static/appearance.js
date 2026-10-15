/* Browser-local presentation; never changes server or conversation settings. */
(function () {
    'use strict';
    const key = 'exo-appearance-v1';
    const portal = location.hostname === 'portal.example.invalid';
    const defaults = {theme: portal ? 'exoloom' : 'legacy', font: 'readable', scene:'animated',
        color: portal ? 'reference' : 'original', grid: false, floor: portal, stationary: false, fps: 30};
    const colors = {reference:'#00ffff',original: '#ffffff', cyan: '#00ffff', violet: '#c084fc', ember: '#ffad66', crimson: '#ff526d', mint: '#68ffbb'};
    let settings;
    try {
        const saved = JSON.parse(localStorage.getItem(key) || '{}');
        settings = {...defaults, ...saved};
        // Retire the earlier tinted-raytracer default in favor of the supplied art.
        if (saved.version !== 3 && settings.theme === 'exoloom') Object.assign(settings,
            {scene:'animated',color:'reference',floor:true,stationary:false});
    }
    catch { settings = {...defaults}; }
    function normalize(value) {
        return {theme: ['legacy','exoloom'].includes(value.theme) ? value.theme : defaults.theme,
            scene: ['reference','animated','video'].includes(value.scene) ? value.scene : 'animated',
            font: ['readable','orbitron','mono'].includes(value.font) ? value.font : 'readable',
            color: Object.hasOwn(colors, value.color) ? value.color : defaults.color,
            grid: value.grid === true, floor: value.floor === true, stationary: value.stationary === true,
            fps: [20,30,60].includes(Number(value.fps)) ? Number(value.fps) : 30};
    }
    function apply(value, save = true) {
        settings = normalize(value);
        const root = document.documentElement;
        root.dataset.loomTheme = settings.theme;
        root.dataset.loomFont = settings.font;
        root.dataset.loomFloor = String(settings.floor);
        root.dataset.loomScene = settings.scene;
        root.style.setProperty('--scene-color', colors[settings.color]);
        if (save) { try { localStorage.setItem(key, JSON.stringify({...settings,version:3})); } catch {} }
        window.LoomBlackhole?.setAppearance(settings);
        window.EXOBranding?.refresh();
        for (const field of Object.keys(settings)) {
            const input = document.getElementById(`appearance-${field}`);
            if (input) { if (input.type === 'checkbox') input.checked = settings[field]; else input.value = settings[field]; }
        }
        const still = settings.theme === 'exoloom' && settings.scene !== 'animated';
        for (const field of ['color','grid','floor','stationary','fps']) {
            const input = document.getElementById(`appearance-${field}`);
            if (input) input.disabled = still;
        }
        const scene = document.getElementById('appearance-scene');
        if (scene) scene.disabled = settings.theme !== 'exoloom';
    }
    window.EXOAppearance = {settings: () => ({...settings}), apply,
        reset: () => apply({...defaults}), colors};
    apply(settings, false);
    document.addEventListener('DOMContentLoaded', () => {
        const panel = document.getElementById('appearance-controls');
        panel?.addEventListener('change', event => {
            const field = event.target.id.replace('appearance-', '');
            if (!Object.hasOwn(settings, field)) return;
            const value = event.target.type === 'checkbox' ? event.target.checked : event.target.value;
            const next = {...settings, [field]: value};
            if (field === 'theme' && value === 'exoloom') Object.assign(next,
                {scene:'animated',color:'reference',floor:true,stationary:false});
            if (field === 'theme' && value === 'legacy' && settings.color === 'reference') next.color = 'original';
            apply(next);
        });
        document.getElementById('appearance-reset')?.addEventListener('click', () => window.EXOAppearance.reset());
        apply(settings, false);
    });
})();
