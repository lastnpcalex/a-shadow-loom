// Usage & Activity — read-only token telemetry aggregated by Main EXO from
// the per-turn counters already stored on assistant messages (the same
// figures chat footers display). Rendered as period cards, period-over-period
// comparisons, a daily bar chart and provider coverage.

let usageLoading = false;
let usageLastLoad = 0;
let usageLastData = null;
let usageChartData = null;
let usageChartController = null;
let usageChartRequest = 0;

function usageCategoryLabel(value) {
    return {claude: 'Claude Code', codex: 'Codex', goose: 'Goose', gemini: 'Antigravity',
        weave: 'Weave / direct API', 'llama-server': 'Llama Server', unlabeled: 'Unlabeled'}[value] || value;
}

function usageChartColor(category) {
    const palette = ['var(--cyan)', 'var(--violet)', 'var(--green)', 'var(--amber)', 'var(--text-dim)', 'var(--text)'];
    return palette[Math.max(0, usageChartData.categories.indexOf(category)) % palette.length];
}

async function loadUsageChart() {
    const request = ++usageChartRequest;
    usageChartController?.abort();
    const controller = usageChartController = new AbortController();
    const status = document.getElementById('usage-chart-status');
    const period = document.getElementById('usage-chart-period').value;
    document.getElementById('usage-chart-custom').hidden = period !== 'custom';
    const params = new URLSearchParams({period, group: document.getElementById('usage-chart-group').value,
        selection: document.getElementById('usage-chart-category').value});
    if (period === 'custom') {
        params.set('start', document.getElementById('usage-chart-start').value);
        params.set('end', document.getElementById('usage-chart-end').value);
    }
    status.textContent = 'Loading chart…';
    const timer = setTimeout(() => controller.abort(), 30000);
    try {
        const response = await fetch(`/api/usage/chart?${params}`, {cache:'no-store',signal:controller.signal});
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
        if (request !== usageChartRequest) return;
        usageChartData = data;
        const picker = document.getElementById('usage-chart-category');
        picker.replaceChildren(new Option('All categories','all'), ...data.categories.map(c => new Option(usageCategoryLabel(c),c)));
        picker.value = data.selection;
        renderUsageChart();
        status.textContent = `${usageFmtTok(data.totals.input)} in · ${usageFmtTok(data.totals.output)} out · ${data.totals.turns} turns · ${usageCoverage(data.totals.coverage)}`;
    } catch (error) {
        if (request === usageChartRequest) status.textContent = `Chart unavailable: ${error.message}. ${usageChartData ? 'Previous chart retained.' : 'No chart loaded yet.'}`;
    } finally { clearTimeout(timer); }
}

function renderUsageChart() {
    if (!usageChartData) return;
    const data = usageChartData;
    const categoryColors = document.getElementById('usage-chart-colors').value === 'category';
    renderUsageDaily(data.buckets,categoryColors);
    const shown = data.selection === 'all' ? data.breakdown.map(r => r.category) : [data.selection];
    document.getElementById('usage-chart-legend').innerHTML = categoryColors
        ? shown.map(c => `<span><i class="usage-chart-swatch" style="background:${usageChartColor(c)}"></i>${esc(usageCategoryLabel(c))}</span>`).join('')
        : '<span><i class="usage-chart-swatch" style="background:var(--cyan)"></i>Input</span><span><i class="usage-chart-swatch" style="background:var(--violet)"></i>Output</span>';
    const rows = data.breakdown.map(r => `<tr><td>${esc(usageCategoryLabel(r.category))}</td><td>${usageFmtTok(r.input)}</td><td>${usageFmtTok(r.output)}</td><td>${r.turns}</td><td>${usageCoverage(r.coverage)}</td></tr>`).join('');
    document.getElementById('usage-chart-breakdown').innerHTML = `<table class="usage-providers-table"><thead><tr><th>${data.group === 'harness' ? 'Harness' : 'Backend / provider'} · selected period</th><th>In</th><th>Out</th><th>Turns</th><th>Coverage</th></tr></thead><tbody>${rows}</tbody></table>`;
}

const USAGE_PERIOD_LABELS = {
    rolling_5h: 'Last 5 hours',
    today: 'Today',
    week: 'This week',
    month: 'This month',
    year: 'This year',
};

const USAGE_COVERAGE_TEXT = {
    reported: 'provider-reported',
    partial: 'partial history',
    sparse: 'sparse history',
    unavailable: 'no counters recorded',
    'no-activity': 'no turns in period',
};

function usageFmtTok(n) {
    if (n === null || n === undefined) return 'Unavailable';
    if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
    return String(n);
}

function usageDelta(pct) {
    if (pct === null || !Number.isFinite(pct)) return '<span class="usage-delta flat">vs prev: n/a</span>';
    const rounded = Math.round(pct * 10) / 10;
    const cls = rounded > 0 ? 'up' : rounded < 0 ? 'down' : 'flat';
    const arrow = rounded > 0 ? '▲' : rounded < 0 ? '▼' : '＝';
    return `<span class="usage-delta ${cls}" title="Compared with the previous calendar period up to the equivalent local date/time, clamped for shorter months; five hours uses the preceding five hours">${arrow} ${Math.abs(rounded)}% vs prev</span>`;
}

function usageCoverage(coverage) {
    return USAGE_COVERAGE_TEXT[coverage] || 'coverage unknown';
}

function usageAxisScale(maximum) {
    const raw = Math.max(4, maximum) / 4;
    const power = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].find(n => n * power >= raw) * power;
    const top = step * 4;
    const unit = top >= 1e9 ? {value:1e9, label:'billions'}
        : top >= 1e6 ? {value:1e6, label:'millions'}
        : top >= 1e3 ? {value:1e3, label:'thousands'} : {value:1, label:''};
    return {top, unit, ticks:[4,3,2,1,0].map(n => ({position:n*25,
        label:(step*n/unit.value).toLocaleString(undefined,{maximumFractionDigits:2})}))};
}

function usageAxisDate(day, resolution) {
    if (resolution === 'hour') {
        return new Intl.DateTimeFormat(undefined, {timeZone:usageChartData.timezone, month:'short', day:'numeric',
            hour:'2-digit', minute:'2-digit', hour12:false}).format(new Date(day));
    }
    const date = new Date(`${day}${resolution === 'month' ? '-01' : ''}T00:00:00Z`);
    return new Intl.DateTimeFormat(undefined, {timeZone:'UTC', month:'short',
        ...(resolution === 'month' ? {year:'numeric'} : {day:'numeric'})}).format(date);
}

function renderUsagePeriod(key, period) {
    const label = USAGE_PERIOD_LABELS[key] || key;
    const noActivity = period.coverage === 'no-activity';
    const values = period.coverage === 'unavailable'
        ? `<div class="usage-tokens">Unavailable</div><div class="usage-io">${period.turns} turns without counters</div>`
        : noActivity
        ? `<div class="usage-tokens">—</div><div class="usage-io">No turns recorded</div>`
        : `<div class="usage-tokens">${usageFmtTok(period.total)}</div>
           <div class="usage-io">${usageFmtTok(period.input)} in · ${usageFmtTok(period.output)} out · ${period.turns} turn${period.turns === 1 ? '' : 's'}</div>`;
    return `<div class="usage-period" data-period="${key}">
        <h3>${label}</h3>
        ${values}
        ${usageDelta(period.change?.total_pct)}
        <div class="usage-coverage">${usageCoverage(period.coverage)}</div>
    </div>`;
}

function renderUsageDaily(daily, categoryColors = false) {
    const container = document.getElementById('usage-daily');
    if (!container) return;
    if (!daily || !daily.length) {
        container.innerHTML = '<div class="empty-note">No daily history yet.</div>';
        return;
    }
    const scale = usageAxisScale(Math.max(...daily.map(d => d.total || 0), 0));
    const max = scale.top;
    const bars = daily.map(d => {
        const pct = d.total > 0 ? d.total / max * 100 : 0;
        const outputPct = d.total > 0 ? (d.output || 0) / d.total * 100 : 0;
        const details = (d.breakdown || []).map(r => `${usageCategoryLabel(r.category)}: ${r.total === null ? 'Unavailable' : r.total.toLocaleString()} (${usageFmtTok(r.input)} in / ${usageFmtTok(r.output)} out)`);
        const title = `${d.day}: ${d.total === null ? 'Unavailable' : d.total.toLocaleString()} tokens (${d.input === null ? 'Unavailable' : d.input.toLocaleString()} in · ${d.output === null ? 'Unavailable' : d.output.toLocaleString()} out) · ${d.turns} turns · ${usageCoverage(d.coverage)}${details.length ? '\n'+details.join('\n') : ''}`;
        const segments = categoryColors ? (d.breakdown || []).map(r => `<span class="usage-chart-segment" style="height:${d.total > 0 ? (r.total || 0)/d.total*100 : 0}%;background:${usageChartColor(r.category)}"></span>`).join('')
            : `<span class="usage-bar-output" style="height:${outputPct}%"></span>`;
        return `<div tabindex="0" class="usage-bar${categoryColors ? ' categories' : ''}${d.total === null ? ' missing' : !d.total ? ' empty' : ''}" style="height:${pct}%" title="${esc(title)}" aria-label="${esc(title)}">${segments}</div>`;
    }).join('');
    const resolution = usageChartData?.resolution || 'day';
    const tickCount = Math.min(daily.length, Math.max(2, Math.min(7,
        Math.floor(Math.max(180, container.clientWidth-64)/(resolution === 'hour' ? 130 : 85)))));
    const indices = [...new Set(Array.from({length:tickCount},(_,i) => tickCount === 1 ? 0
        : Math.round(i*(daily.length-1)/(tickCount-1))))];
    const xTicks = indices.map(i => `<span class="usage-x-tick${daily.length > 1 && i === 0 ? ' first' : daily.length > 1 && i === daily.length-1 ? ' last' : ''}" style="left:${(i+0.5)/daily.length*100}%">${esc(usageAxisDate(daily[i].day,resolution))}</span>`).join('');
    const yTicks = scale.ticks.map(t => `<span class="usage-y-tick" style="bottom:${t.position}%">${esc(t.label)}</span>`).join('');
    const grid = scale.ticks.map(t => `<span class="usage-grid-line" style="bottom:${t.position}%"></span>`).join('');
    container.innerHTML = `
        <div class="usage-y-title">Tokens${scale.unit.label ? ` (${scale.unit.label})` : ''}</div>
        <div class="usage-axis-layout">
            <div class="usage-y-axis" aria-label="Token count scale">${yTicks}</div>
            <div class="usage-plot"><div class="usage-chart-grid" aria-hidden="true">${grid}</div><div class="usage-bars">${bars}</div></div>
            <div></div><div class="usage-x-axis">${xTicks}</div>
        </div>
        <div class="usage-x-title">${resolution === 'hour' ? 'Date / time' : resolution === 'month' ? 'Month' : 'Date'} · ${esc(usageChartData?.timezone || '')}</div>
        <div class="usage-bars-range">${esc(usageChartData?.start || daily[0].day)} → ${esc(usageChartData?.end || daily[daily.length - 1].day)} · ${esc(usageChartData?.resolution || 'day')} buckets · ${esc(usageChartData?.timezone || '')} · Gray: no activity / missing counters</div>`;
}

function renderUsageProviders(providers) {
    const container = document.getElementById('usage-providers');
    if (!container) return;
    if (!providers || !providers.length) {
        container.innerHTML = '<div class="empty-note">No provider history yet.</div>';
        return;
    }
    const rows = providers.map(p => `<tr>
        <td>${esc(p.provider === 'llama-server' ? 'Llama Server' : p.provider)}</td>
        <td>${usageFmtTok(p.input)}</td>
        <td>${usageFmtTok(p.output)}</td>
        <td>${p.turns}</td>
        <td>${usageCoverage(p.coverage)}</td>
    </tr>`).join('');
    container.innerHTML = `<table class="usage-providers-table">
        <thead><tr><th>Provider</th><th>In</th><th>Out</th><th>Turns</th><th>Coverage</th></tr></thead>
        <tbody>${rows}</tbody></table>`;
}

function renderUsageActivity(data) {
    const periods = data.periods || {};
    const order = ['rolling_5h', 'today', 'week', 'month', 'year'];
    const periodsEl = document.getElementById('usage-periods');
    if (periodsEl) {
        periodsEl.innerHTML = order
            .filter(key => periods[key])
            .map(key => renderUsagePeriod(key, periods[key]))
            .join('') || '<div class="empty-note">No usage periods available.</div>';
    }
    if (!document.getElementById('usage-chart-start').value) {
        document.getElementById('usage-chart-start').value = data.daily[0].day;
        document.getElementById('usage-chart-end').value = data.generated_at.slice(0,10);
    }
    document.getElementById('usage-chart-end').max = data.generated_at.slice(0,10);
    document.getElementById('usage-chart-start').max = data.generated_at.slice(0,10);
    renderUsageProviders(data.providers);
    renderUsageReports(data);
    const allTime = data.all_time || {};
    const note = document.getElementById('usage-all-time');
    if (note) {
        note.textContent = allTime.turns
            ? `All-time: ${usageFmtTok(allTime.input)} in · ${usageFmtTok(allTime.output)} out across ${allTime.turns} turns` +
              (allTime.first_recorded_turn ? ` · counters since ${new Date(allTime.first_recorded_turn).toLocaleDateString()}` : '')
            : 'No recorded turns with token counters yet.';
    }
}

function renderUsageReports(data) {
    const avg = data.averages || {};
    document.getElementById('usage-averages').textContent = `${(avg.turns_per_day || 0).toFixed(1)} turns/day · ${(avg.turns_per_active_day || 0).toFixed(1)} turns/active day · ${avg.active_days || 0} active days across ${avg.calendar_days || 0} calendar days`;
    document.getElementById('usage-accounting-note').textContent = data.input_semantics;
    const options = data.settings;
    if (!document.activeElement?.closest('#usage-schedule')) {
        document.getElementById('usage-timezone').value = options.timezone;
        document.getElementById('usage-hour').value = options.hour;
        document.getElementById('usage-auto').checked = options.enabled;
    }
    const picker = document.getElementById('usage-report-picker');
    const selected = picker.value;
    picker.replaceChildren(...data.snapshots.map(r => new Option(`My day in tokens — ${r.day}`, r.day)));
    if (data.snapshots.some(r => r.day === selected)) picker.value = selected;
    renderSelectedUsageReport();
}

function selectedUsageReport() {
    return usageLastData?.snapshots?.find(r => r.day === document.getElementById('usage-report-picker').value);
}

function renderSelectedUsageReport() {
    const report = selectedUsageReport();
    document.getElementById('usage-report-preview').textContent = report?.markdown || 'No saved daily snapshots yet.';
    const card = document.getElementById('usage-report-card');
    if (!report) {
        card.innerHTML = '<p class="empty-note">Your daily snapshot will appear here once it’s saved.</p>';
    } else {
        const exact = n => typeof n === 'number' && Number.isFinite(n) ? n.toLocaleString() : 'Unavailable';
        const day = new Intl.DateTimeFormat(undefined,{timeZone:'UTC',year:'numeric',month:'long',day:'numeric'})
            .format(new Date(`${report.day}T00:00:00Z`));
        const generated = new Intl.DateTimeFormat(undefined,{timeZone:report.timezone,dateStyle:'medium',timeStyle:'short'})
            .format(new Date(report.generated_at));
        const delta = report.change_pct;
        const comparison = typeof delta === 'number' && Number.isFinite(delta)
            ? `${delta > 0 ? '+' : ''}${delta.toFixed(1)}% vs previous day`
            : 'Previous-day comparison unavailable';
        const inputPct = report.total > 0 ? (report.input || 0)/report.total*100 : 0;
        const mix = report.total > 0 && report.input !== null && report.output !== null
            ? `<div class="usage-snapshot-mix" aria-hidden="true"><span style="width:${inputPct}%"></span></div>` : '';
        const providers = (report.providers || []).map(p => {
            const total = p.total ?? (p.input === null && p.output === null ? null : (p.input || 0)+(p.output || 0));
            const share = report.total > 0 && total > 0 ? total/report.total*100 : 0;
            return `<li><div class="usage-snapshot-provider-head"><span>${esc(usageCategoryLabel(p.provider))}</span><span>${esc(exact(total))}</span></div>
                <div class="usage-snapshot-provider-track" aria-hidden="true"><span style="width:${share}%"></span></div>
                <div class="usage-snapshot-provider-io">${esc(exact(p.input))} in · ${esc(exact(p.output))} out</div></li>`;
        }).join('');
        card.innerHTML = `<header class="usage-snapshot-head"><div><div class="usage-snapshot-eyebrow">My day in tokens</div>
            <h3>${esc(day)}</h3></div><span class="usage-snapshot-badge">${esc(usageCoverage(report.coverage))}</span></header>
            <div class="usage-snapshot-total">${esc(exact(report.total))}<span>recorded tokens</span></div>
            <div class="usage-snapshot-comparison">${esc(comparison)}</div>
            ${mix}<div class="usage-snapshot-stats"><div><span>Input</span><strong>${esc(exact(report.input))}</strong></div>
            <div><span>Output</span><strong>${esc(exact(report.output))}</strong></div><div><span>Turns</span><strong>${esc(exact(report.turns))}</strong></div></div>
            <div class="usage-snapshot-provider-title">Provider breakdown</div>
            <ul class="usage-snapshot-providers">${providers || '<li>No provider activity recorded.</li>'}</ul>
            <footer><time datetime="${esc(report.generated_at)}">Saved ${esc(generated)}</time> · ${esc(report.timezone)}
            <div>${esc(exact(report.counted_turns))} of ${esc(exact(report.turns))} turns have counters. Input: cyan · Output: violet.</div></footer>`;
    }
    document.querySelectorAll('[data-usage-export]').forEach(b => b.disabled = !report);
}

async function downloadUsageReport(format) {
    const report = selectedUsageReport();
    if (!report) return;
    if (format === 'png') {
        const button = document.querySelector('[data-usage-export="png"]');
        const status = document.getElementById('usage-report-status');
        button.disabled = true;
        try {
            const card = document.getElementById('usage-report-card');
            // Freeze this selected card before waiting, so changing the picker
            // cannot export a different day under the original filename.
            const bounds = card.getBoundingClientRect();
            const clone = card.cloneNode(true);
            const originals = [card, ...card.querySelectorAll('*')];
            const copies = [clone, ...clone.querySelectorAll('*')];
            originals.forEach((node, index) => {
                const style = getComputedStyle(node);
                for (const property of style) copies[index].style.setProperty(property, style.getPropertyValue(property));
            });
            clone.style.margin = '0';
            const width = Math.ceil(bounds.width), height = Math.ceil(bounds.height);
            const markup = new XMLSerializer().serializeToString(clone);
            const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}"><foreignObject width="100%" height="100%">${markup}</foreignObject></svg>`;
            const picture = new Image();
            picture.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
            await picture.decode();
            const canvas = document.createElement('canvas');
            canvas.width = width * 2; canvas.height = height * 2;
            const context = canvas.getContext('2d');
            context.scale(2, 2);
            context.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim() || '#0b0b15';
            context.fillRect(0, 0, width, height);
            context.drawImage(picture, 0, 0, width, height);
            const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
            if (!blob) throw new Error('Image export unavailable');
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url; link.download = `my-day-in-tokens-${report.day}.png`;
            link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
            status.textContent = 'PNG downloaded.';
        } catch (error) {
            status.textContent = `Could not download PNG: ${error.message}`;
        } finally { button.disabled = !selectedUsageReport(); }
        return;
    }
    const content = format === 'json' ? JSON.stringify(report, null, 2) : report.markdown;
    const url = URL.createObjectURL(new Blob([content], {type: format === 'json' ? 'application/json' : 'text/markdown;charset=utf-8'}));
    const link = document.createElement('a');
    link.href = url; link.download = `my-day-in-tokens-${report.day}.${format === 'json' ? 'json' : 'md'}`;
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function usageWrite(path, method, data, button) {
    button.disabled = true;
    const status = document.getElementById('usage-report-status');
    status.textContent = 'Saving…';
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);
    try {
        const response = await fetch(path, {method, headers: {'Content-Type': 'application/json'},
            body: data ? JSON.stringify(data) : undefined, signal: controller.signal});
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || result.error || `HTTP ${response.status}`);
        await loadUsageActivity({force: true});
        status.textContent = method === 'POST' ? 'Yesterday’s snapshot saved.' : 'Schedule saved.';
    } catch (error) { status.textContent = `Could not save: ${error.message}`; }
    finally { clearTimeout(timer); button.disabled = false; }
}

async function loadUsageActivity({ force = false } = {}) {
    const view = document.getElementById('view-usage');
    if (!view || usageLoading) return;
    if (!force && (document.hidden || !view.classList.contains('active')
            || (usageLastLoad && Date.now() - usageLastLoad < 60000))) return;
    usageLoading = true;
    usageLastLoad = Date.now();
    const button = document.getElementById('btn-usage-refresh');
    const status = document.getElementById('usage-status');
    if (button) button.disabled = true;
    if (status) status.textContent = 'Loading usage…';
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);
    try {
        const response = await fetch('/api/usage/activity', { cache: 'no-store', signal: controller.signal });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        if (!data || typeof data !== 'object' || !data.periods) throw new Error('Usage aggregation unavailable.');
        usageLastData = data;
        renderUsageActivity(data);
        await loadUsageChart();
        if (status) status.textContent = `Updated ${new Date(data.generated_at).toLocaleTimeString()} · ${data.timezone}`;
    } catch (error) {
        if (status) status.textContent = `Usage unavailable: ${error.message}. ${usageLastData ? 'Last known figures retained; not current.' : ''}`;
    } finally {
        clearTimeout(timer);
        if (button) button.disabled = false;
        usageLoading = false;
    }
}

document.addEventListener('visibilitychange', () => {
    if (!document.hidden) loadUsageActivity();
});

document.getElementById('btn-usage-refresh')?.addEventListener('click', () => loadUsageActivity({ force: true }));

window.loadUsageActivity = loadUsageActivity;
setInterval(() => loadUsageActivity(), 60000);

document.getElementById('usage-report-picker').addEventListener('change', renderSelectedUsageReport);
document.querySelectorAll('[data-usage-export]').forEach(b => b.addEventListener('click', () => downloadUsageReport(b.dataset.usageExport)));
document.getElementById('btn-usage-yesterday').addEventListener('click', event => usageWrite('/api/usage/snapshots/yesterday', 'POST', null, event.currentTarget));
document.getElementById('usage-schedule').addEventListener('submit', event => {
    event.preventDefault();
    usageWrite('/api/usage/settings', 'PUT', {timezone: document.getElementById('usage-timezone').value.trim(),
        hour: Number(document.getElementById('usage-hour').value), enabled: document.getElementById('usage-auto').checked},
        document.getElementById('btn-usage-schedule'));
});

document.getElementById('usage-chart-controls').addEventListener('submit',event => { event.preventDefault(); loadUsageChart(); });
document.getElementById('usage-chart-group').addEventListener('change',() => {
    document.getElementById('usage-chart-category').value = 'all'; loadUsageChart();
});
for (const id of ['usage-chart-period','usage-chart-category']) document.getElementById(id).addEventListener('change',loadUsageChart);
document.getElementById('usage-chart-colors').addEventListener('change',renderUsageChart);
let usageChartResizeTimer;
window.addEventListener('resize', () => {
    clearTimeout(usageChartResizeTimer);
    usageChartResizeTimer = setTimeout(renderUsageChart, 150);
});
