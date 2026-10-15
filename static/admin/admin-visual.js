/* Admin-only shared preset editor; preview never saves until requested. */
(() => {
    const form = document.getElementById('visual-form'), root = document.getElementById('visual-fields');
    const status = document.getElementById('visual-status'), frame = document.getElementById('visual-preview');
    let fields = [], origin;
    function values() {
        return Object.fromEntries([...root.querySelectorAll('input,select')].map(input =>
            [input.name,input.type === 'checkbox' ? input.checked : input.type === 'number' ? Number(input.value) : input.value]));
    }
    function preview() {
        if (origin) frame.contentWindow?.postMessage({type:'exo-scene-settings',settings:{profile:values(),color:'reference',fps:30,active:!document.hidden && document.getElementById('view-settings').classList.contains('active')}},origin);
    }
    async function load() {
        try {
            const response = await fetch('/api/visual-settings',{cache:'no-store'});
            if (!response.ok) throw new Error('Background settings require the updated Admin server. Reload Admin through its UI after saving ongoing work.');
            const data = await response.json(); fields = data.fields; root.replaceChildren();
            const groups = new Map();
            for (const field of fields) {
                const group = field.path.startsWith('observer.') || field.path==='time_scale' ? 'Orbit and motion'
                    : field.path.startsWith('camera.') ? 'Camera'
                    : field.path.includes('disk') || field.path==='neon_grid' ? 'Disk'
                    : field.path.includes('floor') ? 'Floor'
                    : field.path.includes('nebula') || field.path.includes('hole_glow') ? 'Nebula and glow' : 'Rendering';
                if (!groups.has(group)) {
                    const section=document.createElement('fieldset'); section.className='visual-group';
                    const legend=document.createElement('legend'); legend.textContent=group;
                    const grid=document.createElement('div'); grid.className='machine-settings-grid';
                    section.append(legend,grid); root.append(section); groups.set(group,grid);
                }
                const label = document.createElement('label'); label.className='machine-setting';
                const caption = document.createElement('span'); caption.textContent=field.label;
                const input = document.createElement(Array.isArray(field.min) ? 'select' : 'input'); input.name=field.path;
                if (Array.isArray(field.min)) for (const choice of field.min) { const option=new Option(choice,choice); input.add(option); }
                else input.type = typeof field.default === 'boolean' ? 'checkbox' : typeof field.default === 'string' ? 'color' : 'number';
                if (input.type === 'number') { input.min=field.min; input.max=field.max; input.step='any'; }
                if (input.type === 'checkbox') input.checked=data.values[field.path]; else input.value=data.values[field.path];
                label.append(caption,input); groups.get(group).append(label);
            }
            const url=new URL(location.origin); url.port=data.main_port || '3000'; origin=url.origin;
            url.pathname='/static/exoloom/scene.html'; url.searchParams.set('admin',location.origin);
            frame.src=url.href; frame.hidden=false;
            form.querySelectorAll('button').forEach(button=>button.disabled=false);
            status.textContent='Preview changes below. Save applies this preset to Main and Exoloom browsers.';
        } catch (error) { status.textContent=error.message; }
    }
    form.addEventListener('input',preview); frame.addEventListener('load',preview);
    window.addEventListener('message',event=>{if(event.origin===origin && event.source===frame.contentWindow && event.data?.type==='exo-scene-ready') preview();});
    document.addEventListener('visibilitychange',preview);
    new MutationObserver(preview).observe(document.getElementById('view-settings'),{attributes:true,attributeFilter:['class']});
    document.getElementById('visual-load').addEventListener('click',load);
    document.getElementById('visual-reset').addEventListener('click',()=>{
        for (const field of fields) { const input=[...root.querySelectorAll('input,select')].find(item=>item.name===field.path); if(input.type==='checkbox') input.checked=field.default; else input.value=field.default; }
        preview(); status.textContent='Original preset previewed. Save to publish it.';
    });
    form.addEventListener('submit',async event=>{
        event.preventDefault(); const button=form.querySelector('[type=submit]'); button.disabled=true;
        try {
            const response=await fetch('/api/visual-settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values())});
            const data=await response.json(); if(!response.ok) throw new Error(data.error || 'Could not save preset');
            status.textContent='Saved. Main browsers receive it on focus or within one minute. No server restarted.';
        } catch(error) {status.textContent=error.message;} finally {button.disabled=false;}
    });
})();
