// Populated synchronously by /api/model-registry.js before app/chat rendering.
// The server owns all selector rules and per-model metadata; no model lists here.
const EXOModelRegistry = (() => {
    let entries = new Map();
    let rules = [];
    function install(data) {
        if (data?.version !== 1) return;
        entries = new Map(Object.entries(data.entries || {}));
        rules = (data.rules || []).map(r => ({...r, regex: new RegExp(r.pattern, 'i')}));
    }
    function resolve(value) {
        const raw = String(value || '').trim();
        const exact = entries.get(raw.toLowerCase());
        if (exact) return exact;
        const rule = rules.find(r => r.regex.test(raw));
        return rule ? {value: raw, mode: rule.mode, harness: rule.harness, backend: rule.backend} : {};
    }
    function mergeGroups(groups) {
        for (const group of groups || []) for (const item of group.models || []) {
            const entry = {...resolve(item.value), ...item};
            // Upgrade fallback: old running hosts may not have the bootstrap
            // endpoint yet. Their picker groups still identify exact routes.
            if (!entry.mode) {
                const groupName = (group.group || '').toLowerCase();
                entry.mode = groupName.includes('goose') ? 'goose'
                    : groupName.includes('openrouter') ? 'openrouter'
                    : groupName.includes('codex') ? 'codex'
                    : groupName.includes('antigravity') ? 'gemini'
                    : groupName.includes('umans') ? 'umans'
                    : groupName.includes('anthropic') || groupName.includes('dream via claude') ? 'claude' : null;
                if (entry.mode === 'openrouter') {
                    entry.backend = 'openrouter';
                    entry.model_id = item.value.replace(/^openrouter:/i, '');
                    entry.aliases = [entry.model_id];
                }
                if (groupName.includes('anthropic')) entry.backend = 'anthropic';
            }
            if (!entry.mode) continue;
            entries.set(String(item.value).toLowerCase(), entry);
            for (const alias of entry.aliases || []) {
                entries.set(alias.toLowerCase(), {...entry, value: alias});
            }
        }
    }
    return {install, resolve, mergeGroups};
})();
