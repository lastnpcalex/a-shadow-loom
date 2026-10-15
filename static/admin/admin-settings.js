// Main-owned machine settings, edited through the authenticated Admin proxy.
(() => {
    let snapshot = null;
    let busy = false;
    const status = document.getElementById('machine-settings-status');
    const mainForm = document.getElementById('machine-settings-form');
    const tuningForm = document.getElementById('machine-tuning-form');
    const modelSelect = document.getElementById('machine-tuning-model');
    function setBusy(value) {
        busy = value;
        document.querySelectorAll('#machine-settings-form input, #machine-settings-form select, #machine-settings-form button, #machine-tuning-form input, #machine-tuning-form button, #machine-tuning-model, #btn-machine-reload').forEach(el => { el.disabled = value; });
    }
    function renderFields(id, fields, values) {
        const root = document.getElementById(id); root.replaceChildren();
        for (const field of fields) {
            const label = document.createElement('label'); label.className = 'machine-setting';
            const name = document.createElement('span'); name.textContent = field.label;
            const input = document.createElement(field.type === 'profile' ? 'select' : 'input');
            if (field.type === 'profile') {
                input.className = 'select';
                for (const [value, text] of [['strict', 'Strict'], ['balanced', 'Balanced'], ['trusted', 'Trusted'], ['trust_review', 'Full Trust']]) {
                    const option = document.createElement('option'); option.value = value; option.textContent = text; input.appendChild(option);
                }
            }
            input.name = field.key; input.dataset.kind = field.type;
            input.type = field.type === 'boolean' ? 'checkbox' : field.type === 'text' ? 'text' : 'number';
            if (field.min !== null) input.min = field.min;
            if (field.max !== null) input.max = field.max;
            if (input.type === 'number') input.step = field.type === 'integer' ? '1' : 'any';
            if (input.type === 'checkbox') input.checked = !!values[field.key];
            else input.value = values[field.key] ?? '';
            const help = document.createElement('small'); help.textContent = field.description;
            if (field.type === 'profile') {
                const updateHelp = () => { help.textContent = field.description + (input.value === 'trust_review' ? ' Full Trust allows bridged requests automatically and reviews recognized destructive operations. Unrecognized destructive scripts can pass; native safeguards and role restrictions still apply.' : ''); };
                input.addEventListener('change', updateHelp); updateHelp();
            }
            label.append(name, input, help); root.appendChild(label);
        }
    }
    function renderTuning() {
        if (!snapshot) return;
        renderFields('machine-tuning-fields', snapshot.tuning_fields, snapshot.models[modelSelect.value] || {});
        tuningForm.querySelector('button').disabled = busy || !modelSelect.value;
    }
    function collect(form) {
        return Object.fromEntries([...form.querySelectorAll('input, select')].map(input => [input.name,
            input.type === 'checkbox' ? input.checked : input.type === 'number' ? input.value === '' ? null : Number(input.value) : input.value]));
    }
    function changed(values, base) {
        return Object.fromEntries(Object.entries(values).filter(([key, value]) => {
            const previous = base[key] ?? (typeof value === 'boolean' ? false : typeof value === 'string' ? '' : null);
            return value !== previous;
        }));
    }
    async function load() {
        if (busy) return;
        setBusy(true); status.textContent = 'Loading Main settings…';
        try {
            const response = await fetch('/api/machine-settings', {cache: 'no-store'});
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Settings unavailable');
            snapshot = data;
            renderFields('machine-settings-fields', data.fields, data.settings);
            modelSelect.replaceChildren();
            for (const model of data.disk_models.filter(name => !/^mmproj/i.test(name))) {
                const option = document.createElement('option'); option.value = model; option.textContent = model; modelSelect.appendChild(option);
            }
            if (data.disk_models.includes(data.settings.llama_model)) modelSelect.value = data.settings.llama_model;
            status.textContent = 'Settings loaded from Main. Local server changes take effect on its next launch.';
        } catch (error) { status.textContent = error.message; }
        finally {
            setBusy(false);
            mainForm.querySelector('button').disabled = !snapshot;
            renderTuning();
        }
    }
    async function save(event, tuning) {
        event.preventDefault();
        if (busy || !snapshot) return;
        const values = tuning ? changed(collect(tuningForm), snapshot.models[modelSelect.value] || {})
            : changed(collect(mainForm), snapshot.settings);
        if (!Object.keys(values).length) { status.textContent = 'No settings changed.'; return; }
        const payload = tuning ? {model: modelSelect.value, tuning: values} : {settings: values};
        setBusy(true); status.textContent = 'Saving…';
        try {
            const response = await fetch('/api/machine-settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
            const data = await response.json();
            if (!response.ok || !data.ok) throw new Error(data.error || 'Save failed');
            if (!tuning && 'default_permission_profile' in values) {
                const check = await fetch('/api/machine-settings', {cache: 'no-store'});
                const saved = await check.json();
                if (!check.ok || saved.settings?.default_permission_profile !== values.default_permission_profile) {
                    throw new Error('Default trust level was not saved. Restart Main and Admin manually if they have not loaded this update, then reload settings and save again.');
                }
            }
            if (tuning) snapshot.models[modelSelect.value] = {...snapshot.models[modelSelect.value], ...payload.tuning};
            else Object.assign(snapshot.settings, payload.settings);
            status.textContent = 'Saved. No server was restarted.';
        } catch (error) { status.textContent = error.message; }
        finally { setBusy(false); tuningForm.querySelector('button').disabled = !modelSelect.value; }
    }
    mainForm.addEventListener('submit', event => save(event, false));
    tuningForm.addEventListener('submit', event => save(event, true));
    modelSelect.addEventListener('change', renderTuning);
    document.getElementById('btn-machine-reload').addEventListener('click', load);
    window.loadMachineSettings = load;
})();
