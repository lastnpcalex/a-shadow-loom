// A fast, read-only quota window; retain the existing telemetry renderers.
(() => {
    const button = document.createElement('button');
    button.id = 'btn-usage'; button.type = 'button'; button.title = 'Usage and quotas';
    button.setAttribute('aria-label', 'Usage and quotas');
    button.setAttribute('aria-haspopup', 'dialog');
    button.setAttribute('aria-expanded', 'false');
    button.setAttribute('aria-controls', 'modal-usage');
    button.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2 22 12 12 22 2 12Z"/><path d="M8 15v-4m4 6V7m4 8V9"/></svg>';
    document.getElementById('btn-settings').before(button);
    const modal = document.createElement('div');
    modal.id = 'modal-usage'; modal.className = 'hidden';
    modal.innerHTML = '<div class="usage-content" role="dialog" aria-labelledby="usage-title"><div class="usage-heading"><h2 id="usage-title">Usage</h2><button type="button" class="popup-control" id="usage-refresh" title="Refresh usage" aria-label="Refresh usage"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M20 12a8 8 0 1 0-2 5"/></svg></button><button type="button" class="popup-control" id="usage-close" title="Close usage" aria-label="Close usage"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg></button></div><div id="usage-summary"></div></div>';
    document.body.appendChild(modal);
    const summary = document.getElementById('usage-summary');
    summary.append(document.getElementById('provider-usage-monitor'), document.getElementById('settings-openrouter-monitor'));
    document.querySelector('[data-settings-tab="usage"]').remove();
    function close(restoreFocus = false) {
        modal.classList.add('hidden');
        button.setAttribute('aria-expanded', 'false');
        if (restoreFocus) button.focus();
    }
    function position() {
        const anchor = button.getBoundingClientRect();
        modal.style.top = `${anchor.bottom + 8}px`;
        const width = Math.min(420, window.innerWidth - 16);
        modal.style.right = `${Math.min(Math.max(8, window.innerWidth - anchor.right), window.innerWidth - width - 8)}px`;
        modal.style.maxHeight = `${Math.max(80, Math.min(560, window.innerHeight * .65, window.innerHeight - anchor.bottom - 16))}px`;
    }
    function refresh() { window.loadProviderUsage?.(true); window.loadSettingsOpenRouterUsage?.(true); }
    button.addEventListener('click', () => {
        if (!modal.classList.contains('hidden')) { close(); return; }
        position();
        modal.classList.remove('hidden');
        button.setAttribute('aria-expanded', 'true');
        refresh();
    });
    document.addEventListener('click', event => {
        if (!modal.contains(event.target) && !button.contains(event.target)) close();
    });
    window.addEventListener('resize', position);
    document.getElementById('usage-refresh').addEventListener('click', refresh);
    document.getElementById('usage-close').addEventListener('click', () => close(true));
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && !modal.classList.contains('hidden')) {
            event.preventDefault(); close(true);
        }
    });
})();
