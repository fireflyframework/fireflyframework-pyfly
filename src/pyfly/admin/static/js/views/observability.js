/**
 * PyFly Admin — Observability View.
 *
 * Live server-layer observability: worker count, server uptime, active
 * connections, in-flight requests, and requests/second — sourced from the
 * framework's server_* Prometheus meters (aggregated across workers under
 * multiprocess mode). Shows stat cards, rolling line charts, a per-worker
 * breakdown table, lifecycle counters, and links into the Metrics / Traces views.
 *
 * Data source: GET /admin/api/observability + SSE /observability (event: observability)
 */

import { createLineChart } from '../charts.js';
import { createEmptyStateCard } from '../components/empty-state.js';
import { pageSkeleton } from '../components/skeleton.js';
import { createTable } from '../components/table.js';
import { sse } from '../sse.js';

const MAX_DATA_POINTS = 60;

function nowLabel(timestamp) {
    return new Date((timestamp ? timestamp * 1000 : Date.now())).toLocaleTimeString();
}

function pushRolling(arr, value) {
    arr.push(value);
    if (arr.length > MAX_DATA_POINTS) arr.shift();
}

function fmtUptime(seconds) {
    if (seconds == null) return '--';
    const s = Math.floor(seconds % 60);
    const m = Math.floor((seconds / 60) % 60);
    const h = Math.floor(seconds / 3600);
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
}

function statCard(label, value, iconClass = 'primary') {
    const card = document.createElement('div');
    card.className = 'stat-card';
    const content = document.createElement('div');
    content.className = 'stat-card-content';
    const valEl = document.createElement('div');
    valEl.className = 'stat-card-value';
    valEl.textContent = value != null ? String(value) : '--';
    content.appendChild(valEl);
    const labelEl = document.createElement('div');
    labelEl.className = 'stat-card-label';
    labelEl.textContent = label;
    content.appendChild(labelEl);
    card.appendChild(content);
    const icon = document.createElement('div');
    icon.className = `stat-card-icon ${iconClass}`;
    card.appendChild(icon);
    return card;
}

function chartCard(title) {
    const card = document.createElement('div');
    card.className = 'admin-card';
    const header = document.createElement('div');
    header.className = 'admin-card-header';
    const h3 = document.createElement('h3');
    h3.textContent = title;
    header.appendChild(h3);
    card.appendChild(header);
    const body = document.createElement('div');
    body.className = 'admin-card-body';
    body.style.height = '200px';
    const canvas = document.createElement('canvas');
    body.appendChild(canvas);
    card.appendChild(body);
    return { card, canvas };
}

/**
 * Render the observability view.
 * @param {HTMLElement} container
 * @param {import('../api.js').AdminAPI} api
 * @returns {Promise<function>} Cleanup function
 */
export async function render(container, api) {
    container.replaceChildren();

    const wrapper = document.createElement('div');
    wrapper.className = 'view-enter';

    const header = document.createElement('div');
    header.className = 'page-header';
    const headerLeft = document.createElement('div');
    const h1 = document.createElement('h1');
    h1.textContent = 'Observability';
    headerLeft.appendChild(h1);
    const sub = document.createElement('div');
    sub.className = 'page-subtitle';
    sub.textContent = 'server.layer';
    headerLeft.appendChild(sub);
    header.appendChild(headerLeft);
    wrapper.appendChild(header);

    const loader = document.createElement('div');
    loader.appendChild(pageSkeleton({ stats: 4, rows: 4 }));
    wrapper.appendChild(loader);
    container.appendChild(wrapper);

    let data;
    try {
        data = await api.get('/observability');
    } catch (err) {
        wrapper.removeChild(loader);
        wrapper.appendChild(createEmptyStateCard({
            icon: 'alert',
            tone: 'danger',
            title: 'Failed to load observability data',
            text: err.message,
        }));
        return;
    }
    wrapper.removeChild(loader);

    if (data.available === false || data.has_prometheus === false) {
        wrapper.appendChild(createEmptyStateCard({
            icon: 'activity',
            title: 'Server observability is disabled',
            text: 'Enable pyfly.server.observability and install the observability extra (prometheus_client).',
        }));
        return;
    }

    const server = data.server || {};

    // ── Stat cards ───────────────────────────────────────────
    const statsRow = document.createElement('div');
    statsRow.className = 'grid-4 mb-lg';
    const workersEl = statCard('Workers', data.workers, 'primary');
    const uptimeEl = statCard('Server Uptime', fmtUptime(data.uptime_seconds), 'success');
    const connEl = statCard('Active Connections', data.active_connections ?? '--', 'info');
    const inflightEl = statCard('In-flight Requests', data.in_flight_requests ?? 0, 'warning');
    statsRow.append(workersEl, uptimeEl, connEl, inflightEl);
    wrapper.appendChild(statsRow);

    const statsRow2 = document.createElement('div');
    statsRow2.className = 'grid-4 mb-lg';
    const rpsEl = statCard('Requests / sec', (data.requests_per_second ?? 0).toFixed ? data.requests_per_second.toFixed(2) : data.requests_per_second, 'primary');
    const reqTotalEl = statCard('Requests Total', data.requests_total ?? '--', 'info');
    const serverEl = statCard('Server', server.name ? `${server.name} ${server.version || ''}`.trim() : '--', 'success');
    const mpEl = statCard('Multi-worker', data.multiprocess ? 'aggregated' : 'single', 'warning');
    statsRow2.append(rpsEl, reqTotalEl, serverEl, mpEl);
    wrapper.appendChild(statsRow2);

    const valueEls = {
        workers: workersEl.querySelector('.stat-card-value'),
        uptime: uptimeEl.querySelector('.stat-card-value'),
        conn: connEl.querySelector('.stat-card-value'),
        inflight: inflightEl.querySelector('.stat-card-value'),
        rps: rpsEl.querySelector('.stat-card-value'),
        reqTotal: reqTotalEl.querySelector('.stat-card-value'),
    };

    // ── Rolling charts ───────────────────────────────────────
    const labels = [];
    const connData = [];
    const inflightData = [];
    const rpsData = [];
    labels.push(nowLabel(data.timestamp));
    connData.push(data.active_connections || 0);
    inflightData.push(data.in_flight_requests || 0);
    rpsData.push(data.requests_per_second || 0);

    const chartsRow = document.createElement('div');
    chartsRow.className = 'grid-2 mb-lg';
    const { card: connCard, canvas: connCanvas } = chartCard('Active Connections');
    const { card: inflightCard, canvas: inflightCanvas } = chartCard('In-flight Requests');
    const { card: rpsCard, canvas: rpsCanvas } = chartCard('Requests / sec');
    chartsRow.append(connCard, inflightCard, rpsCard);
    wrapper.appendChild(chartsRow);

    // ── Per-worker breakdown ─────────────────────────────────
    const workerCard = document.createElement('div');
    workerCard.className = 'admin-card mb-lg';
    const wHeader = document.createElement('div');
    wHeader.className = 'admin-card-header';
    const wh3 = document.createElement('h3');
    wh3.textContent = 'Per-worker breakdown';
    wHeader.appendChild(wh3);
    workerCard.appendChild(wHeader);
    const wBody = document.createElement('div');
    wBody.className = 'admin-card-body';
    workerCard.appendChild(wBody);
    wrapper.appendChild(workerCard);

    function renderWorkerTable(rows) {
        wBody.replaceChildren();
        wBody.appendChild(createTable({
            columns: [
                { key: 'pid', label: 'Worker PID', sortable: true },
                { key: 'uptime_seconds', label: 'Uptime', render: (v) => fmtUptime(v) },
                { key: 'active_connections', label: 'Connections' },
                { key: 'in_flight_requests', label: 'In-flight' },
                { key: 'requests_total', label: 'Requests' },
                { key: 'native_connections', label: 'Native conns', render: (v) => (v == null ? 'n/a' : String(v)) },
            ],
            data: rows || [],
            emptyText: 'No workers reporting',
        }));
    }
    renderWorkerTable(data.per_worker);

    // ── Links into Metrics / Traces ──────────────────────────
    const links = document.createElement('div');
    links.className = 'mb-lg';
    links.style.display = 'flex';
    links.style.gap = '12px';
    for (const [hash, text] of [['#metrics', 'View all metrics →'], ['#traces', 'View traces →']]) {
        const a = document.createElement('a');
        a.href = hash;
        a.textContent = text;
        a.className = 'admin-link';
        links.appendChild(a);
    }
    wrapper.appendChild(links);

    // ── Charts init ──────────────────────────────────────────
    let connChart = null;
    let inflightChart = null;
    let rpsChart = null;
    requestAnimationFrame(() => {
        connChart = createLineChart(connCanvas, { label: 'Active Connections', color: '--admin-info', data: [...connData], labels: [...labels] });
        inflightChart = createLineChart(inflightCanvas, { label: 'In-flight Requests', color: '--admin-warning', data: [...inflightData], labels: [...labels] });
        rpsChart = createLineChart(rpsCanvas, { label: 'Requests / sec', color: '--admin-primary', data: [...rpsData], labels: [...labels] });
    });

    // ── SSE live updates ─────────────────────────────────────
    sse.connectTyped('/observability', 'observability', (ev) => {
        const label = nowLabel(ev.timestamp);
        pushRolling(labels, label);
        pushRolling(connData, ev.active_connections || 0);
        pushRolling(inflightData, ev.in_flight_requests || 0);
        pushRolling(rpsData, ev.requests_per_second || 0);

        valueEls.workers.textContent = ev.workers != null ? String(ev.workers) : '--';
        valueEls.uptime.textContent = fmtUptime(ev.uptime_seconds);
        valueEls.conn.textContent = ev.active_connections != null ? String(ev.active_connections) : '--';
        valueEls.inflight.textContent = String(ev.in_flight_requests ?? 0);
        valueEls.rps.textContent = (ev.requests_per_second ?? 0).toFixed ? ev.requests_per_second.toFixed(2) : String(ev.requests_per_second ?? 0);
        valueEls.reqTotal.textContent = ev.requests_total != null ? String(ev.requests_total) : '--';

        if (connChart) connChart.update([...connData], [...labels]);
        if (inflightChart) inflightChart.update([...inflightData], [...labels]);
        if (rpsChart) rpsChart.update([...rpsData], [...labels]);

        renderWorkerTable(ev.per_worker);
    });

    return function cleanup() {
        sse.disconnect('/observability');
        if (connChart) connChart.destroy();
        if (inflightChart) inflightChart.destroy();
        if (rpsChart) rpsChart.destroy();
    };
}
