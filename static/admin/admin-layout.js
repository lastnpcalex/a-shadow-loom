// Move existing panels intact so their controls and state keep their owners.
(() => {
    const overview = document.getElementById('view-overview');
    const settings = document.getElementById('view-settings');
    const nav = document.getElementById('nav');
    function view(name, label, icon) {
        const section = document.createElement('section');
        section.id = `view-${name}`; section.className = 'view';
        overview.parentNode.appendChild(section);
        const button = document.createElement('button');
        button.className = 'nav-item'; button.dataset.view = name;
        button.innerHTML = `<span class="nav-icon">${icon}</span><span class="nav-label">${label}</span>`;
        nav.insertBefore(button, nav.querySelector('[data-view="guide"]'));
        return section;
    }
    const quotas = view('quotas', 'Quotas', '▥');
    const diagnostics = view('diagnostics', 'Diagnostics', '⌁');
    nav.insertBefore(nav.querySelector('[data-view="quotas"]'), nav.querySelector('[data-view="activity"]'));
    function move(id, target) { target.appendChild(document.getElementById(id).closest('.card')); }
    move('provider-usage-monitor', quotas);
    move('openrouter-monitor', quotas);
    move('db-folder', settings);
    move('devices-card', settings);
    settings.appendChild(document.getElementById('visual-form').closest('.card'));
    move('acceptance-monitor', diagnostics);
    move('specs-panel', diagnostics);
    const legacy = document.createElement('details');
    legacy.className = 'card legacy-services';
    legacy.innerHTML = '<summary>Legacy standalone Dream Engine</summary><p class="empty-note">Retired launch path. Supported Dream workflows run through Claude Code, Goose or Weave.</p>';
    legacy.appendChild(document.querySelector('[data-server="dream"]'));
    diagnostics.appendChild(legacy);
    const health = document.createElement('div');
    health.className = 'card';
    health.innerHTML = '<div class="card-head"><h2>Service reachability</h2><button class="btn btn-ghost" onclick="setView(\'diagnostics\')">Diagnostics</button></div><p class="empty-note">Port probes show reachable services, not model readiness. Optional services can remain offline.</p><div id="service-health" class="health-grid" role="status">Waiting for port probes…</div>';
    overview.prepend(health);
    const terminal = document.createElement('div');
    terminal.className = 'card';
    terminal.innerHTML = '<div class="card-head"><h2>Legacy terminal (ttyd)</h2><button class="btn btn-ghost" id="terminal-check">Check status</button></div><p id="terminal-status" class="empty-note" role="status">Check installation and listener status. An online listener does not verify interactive terminal usability.</p>';
    diagnostics.appendChild(terminal);
    window.loadTerminalStatus = async () => {
        const output = document.getElementById('terminal-status');
        try {
            const response = await fetch('/api/ttyd-status', {cache: 'no-store'});
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();
            output.textContent = `Executable: ${data.exe ? 'found' : 'not found'} · Listener: ${data.running ? 'online' : 'offline'} · Host: ${data.host ?? 'unknown'}:${data.port ?? '?'} · TLS: ${data.ssl ? 'on' : 'off'} · Authentication: ${data.auth ? 'on' : 'off'}. Interactive usability has not been verified.`;
        } catch (error) { output.textContent = `Terminal status unavailable: ${error.message}`; }
    };
    document.getElementById('terminal-check').addEventListener('click', window.loadTerminalStatus);
    nav.querySelectorAll('.nav-item').forEach(button => {
        const label = button.querySelector('.nav-label').textContent;
        button.setAttribute('aria-label', label);
        if (!button.title) button.title = label;
    });
})();
