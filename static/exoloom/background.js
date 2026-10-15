/* Exact supplied artwork, with an opt-in instance of the original portal scene. */
(function () {
    'use strict';
    const container = document.getElementById('blackhole-container');
    if (!container) return;
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    const portrait = matchMedia('(orientation: portrait)');
    let frame = null, video = null, captionLayer = null, current = null, enabled = true, profile = {};
    const videoCaption = {enabled:true,text:'EXOLOOM',style:'ice',font:'Orbitron',size:11,
        x:50,y:68,width:90,uppercase:true,bevel:1,grid:.85,stretch:1,line_gap:1.12,
        angular:false,metal:.65,glow:.85,fuzz:.22,tint_color:'#ffffff'};
    function drawVideoCaption() {
        if (!captionLayer || !window.EXOLettering) return;
        const caption = {...videoCaption};
        for (const [path,value] of Object.entries(profile)) {
            const [group,field] = path.split('.');
            if (group === 'caption' && Object.hasOwn(caption,field)) caption[field] = value;
        }
        captionLayer.hidden = !caption.enabled || !caption.text;
        if (captionLayer.hidden) return;
        const dpr = Math.min(devicePixelRatio || 1,2);
        EXOLettering.draw(captionLayer.firstChild, caption.text,
            Math.round(container.clientWidth*dpr), Math.round(container.clientHeight*dpr), caption);
    }
    function showVideoCaption() {
        if (!captionLayer) {
            captionLayer = document.createElement('div'); captionLayer.id = 'exoloom-video-caption';
            captionLayer.setAttribute('aria-hidden','true');
            captionLayer.appendChild(document.createElement('canvas')); container.appendChild(captionLayer);
        }
        drawVideoCaption();
    }
    function stopVideo() {
        captionLayer?.remove(); captionLayer = null;
        if (video) { video.pause(); video.removeAttribute('src'); video.load(); video.remove(); video = null; }
        container.style.removeProperty('background-image');
    }
    function playVideo() {
        if (!video) return;
        if (document.hidden || !enabled) video.pause();
        else video.play().catch(() => { /* Poster remains visible if autoplay is unavailable. */ });
    }
    function send() {
        frame?.contentWindow?.postMessage({type:'exo-scene-settings',settings:{...current,profile,active:enabled && !document.hidden}}, location.origin);
    }
    function clear() {
        frame?.remove(); frame = null;
        stopVideo();
        container.classList.remove('exoloom-background');
    }
    function apply(settings, on = enabled) {
        current = {...settings}; enabled = on;
        if (settings.theme !== 'exoloom' || !on) { clear(); return; }
        container.classList.add('exoloom-background');
        if (settings.scene === 'video') {
            frame?.remove(); frame = null;
            const variant = portrait.matches ? 'mobile' : 'desktop';
            const base = '/static/exoloom/video/' + variant;
            container.style.setProperty('background-image', `url("${base}.jpg")`, 'important');
            if (reduced.matches) { stopVideo(); container.style.setProperty('background-image', `url("${base}.jpg")`, 'important'); showVideoCaption(); return; }
            if (!video) {
                video = document.createElement('video');
                video.id = 'exoloom-video'; video.muted = true; video.loop = true;
                video.playsInline = true; video.autoplay = true;
                video.setAttribute('aria-hidden','true'); video.tabIndex = -1;
                container.appendChild(video);
            }
            if (video.dataset.variant !== variant) {
                video.dataset.variant = variant; video.poster = base + '.jpg'; video.src = base + '.mp4';
            }
            showVideoCaption(); playVideo(); return;
        }
        stopVideo();
        if (settings.scene !== 'animated' || reduced.matches) {
            frame?.remove(); frame = null; return;
        }
        if (!frame) {
            frame = document.createElement('iframe');
            frame.id = 'exoloom-scene'; frame.title = 'Exoloom black hole';
            frame.tabIndex = -1; frame.setAttribute('aria-hidden','true');
            frame.setAttribute('sandbox','allow-scripts allow-same-origin');
            frame.addEventListener('load', send);
            frame.src = '/static/exoloom/scene.html';
            container.appendChild(frame);
        } else send();
    }
    window.addEventListener('message', event => {
        if (!frame || event.source !== frame.contentWindow || event.origin !== location.origin) return;
        if (event.data?.type === 'exo-scene-ready') { frame.classList.add('ready'); send(); }
        if (event.data?.type === 'exo-scene-unavailable') { frame.remove(); frame = null; }
    });
    document.addEventListener('visibilitychange', () => { send(); playVideo(); });
    portrait.addEventListener('change', () => { if (current?.scene === 'video') apply(current, enabled); });
    window.addEventListener('resize', drawVideoCaption);
    document.fonts.ready.then(drawVideoCaption);
    reduced.addEventListener('change', () => { if (current) apply(current, enabled); });
    window.EXOScene = {apply,clear};
    async function refreshProfile() {
        try {
            const response = await fetch('/api/visual-settings',{cache:'no-store'});
            if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) return;
            profile = await response.json(); send(); drawVideoCaption();
        } catch {}
    }
    refreshProfile();
    window.addEventListener('focus',refreshProfile);
    setInterval(() => { if (!document.hidden && current?.theme === 'exoloom') refreshProfile(); },60000);
})();
