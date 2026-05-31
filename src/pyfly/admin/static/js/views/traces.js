/**
 * PyFly Admin — Traces View.
 *
 * Real-time HTTP trace viewer with SSE live updates, pause/resume,
 * clear, status filter pills, click-to-detail panel — plus live
 * request analytics: latency percentiles, status-code mix, and a
 * latency-distribution histogram.
 *
 * Data sources:
 *   GET /admin/api/traces?limit=500 -> { traces: [...], total: N }
 *   SSE /admin/api/sse/traces       -> event type "trace"
 */

import { createBarChart } from '../charts.js';
import { createEmptyStateCard } from '../components/empty-state.js';
import { pageSkeleton } from '../components/skeleton.js';
import { createMethodBadge } from '../components/status-badge.js';
import { sse } from '../sse.js';

/* ── Helpers ──────────────────────────────────────────────────── */

/**
 * Format an ISO timestamp to a compact time string.
 * @param {string|Date} ts
 * @returns {string}
 */
function formatTime(ts) {
    if (!ts) return '--';
    try {
        const d = ts instanceof Date ? ts : new Date(ts);
        return d.toLocaleTimeString(undefined, {
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
        });
    } catch (_) {
        return '--';
    }
}

/**
 * Format a duration in milliseconds compactly (ms, or seconds when large).
 * @param {number|null} ms
 * @returns {string}
 */
function formatMs(ms) {
    if (ms == null || !Number.isFinite(ms)) return '--';
    if (ms >= 1000) return (ms / 1000).toFixed(2) + ' s';
    return ms.toFixed(1) + ' ms';
}

/**
 * Return a CSS class for an HTTP status code.
 * @param {number} status
 * @returns {string}
 */
function statusColorClass(status) {
    if (status >= 500) return 'badge-danger';
    if (status >= 400) return 'badge-warning';
    if (status >= 300) return 'badge-info';
    if (status >= 200) return 'badge-success';
    return 'badge-neutral';
}

/**
 * Create a status badge element.
 * @param {number} status
 * @returns {HTMLSpanElement}
 */
function createStatusCodeBadge(status) {
    const badge = document.createElement('span');
    badge.className = `badge ${statusColorClass(status)}`;
    badge.textContent = String(status);
    return badge;
}

/**
 * Get the status group for filtering (e.g. "2xx", "4xx").
 * @param {number} status
 * @returns {string}
 */
function statusGroup(status) {
    if (status >= 500) return '5xx';
    if (status >= 400) return '4xx';
    if (status >= 300) return '3xx';
    if (status >= 200) return '2xx';
    return 'other';
}

/* ── Analytics ────────────────────────────────────────────────── */

/** Status groups shown in the mix bar/legend, with theme colours. */
const STATUS_META = [
    { key: '2xx', label: '2xx Success', color: '--admin-success' },
    { key: '3xx', label: '3xx Redirect', color: '--admin-info' },
    { key: '4xx', label: '4xx Client', color: '--admin-warning' },
    { key: '5xx', label: '5xx Server', color: '--admin-danger' },
    { key: 'other', label: 'Other', color: '--admin-text-muted' },
];

/** Latency histogram buckets (upper bound exclusive, in ms). */
const LATENCY_BUCKETS = [
    { label: '<10', max: 10 },
    { label: '10–50', max: 50 },
    { label: '50–100', max: 100 },
    { label: '100–250', max: 250 },
    { label: '250–500', max: 500 },
    { label: '0.5–1s', max: 1000 },
    { label: '≥1s', max: Infinity },
];

/**
 * Client-side cap on the live trace buffer (mirrors the server ring buffer).
 * Bounds memory, table DOM size, and per-refresh analytics cost on a
 * long-lived dashboard tab, and keeps the analytics window a stable "last N"
 * set consistent with the ?limit=500 initial fetch.
 */
const MAX_TRACES = 500;

/**
 * Nearest-rank percentile over an ascending-sorted array.
 * @param {number[]} sorted  Ascending durations.
 * @param {number} p         Percentile (0-100).
 * @returns {number|null}
 */
function percentile(sorted, p) {
    if (sorted.length === 0) return null;
    if (sorted.length === 1) return sorted[0];
    const rank = Math.ceil((p / 100) * sorted.length);
    const idx = Math.min(sorted.length - 1, Math.max(0, rank - 1));
    return sorted[idx];
}

/**
 * Bucket ascending durations into the LATENCY_BUCKETS counts.
 * @param {number[]} sorted
 * @returns {number[]}
 */
function bucketize(sorted) {
    const counts = LATENCY_BUCKETS.map(() => 0);
    for (const d of sorted) {
        for (let i = 0; i < LATENCY_BUCKETS.length; i++) {
            if (d < LATENCY_BUCKETS[i].max) { counts[i]++; break; }
        }
    }
    return counts;
}

/**
 * Compute request analytics over the current trace buffer.
 * @param {object[]} traces
 */
function computeAnalytics(traces) {
    const durations = traces
        .map((t) => t.duration_ms)
        .filter((d) => typeof d === 'number' && Number.isFinite(d))
        .sort((a, b) => a - b);

    const counts = { '2xx': 0, '3xx': 0, '4xx': 0, '5xx': 0, other: 0 };
    for (const t of traces) counts[statusGroup(t.status)]++;

    const total = traces.length;
    const errors = counts['4xx'] + counts['5xx'];
    const avg = durations.length
        ? durations.reduce((a, b) => a + b, 0) / durations.length
        : null;

    return {
        total,
        avg,
        p50: percentile(durations, 50),
        p90: percentile(durations, 90),
        p95: percentile(durations, 95),
        p99: percentile(durations, 99),
        max: durations.length ? durations[durations.length - 1] : null,
        errorRate: total ? (errors / total) * 100 : 0,
        counts,
        buckets: bucketize(durations),
    };
}

/* ── Small builders ───────────────────────────────────────────── */

/**
 * Build a stat card; returns the card and its value element.
 * @param {string} label
 * @returns {{ card: HTMLElement, valueEl: HTMLElement }}
 */
function buildStatCard(label) {
    const card = document.createElement('div');
    card.className = 'stat-card';
    const content = document.createElement('div');
    content.className = 'stat-card-content';
    const valueEl = document.createElement('div');
    valueEl.className = 'stat-card-value';
    valueEl.textContent = '--';
    content.appendChild(valueEl);
    const labelEl = document.createElement('div');
    labelEl.className = 'stat-card-label';
    labelEl.textContent = label;
    content.appendChild(labelEl);
    card.appendChild(content);
    return { card, valueEl };
}

/**
 * Build one labelled stat in the percentile strip.
 * @param {string} label
 * @returns {{ el: HTMLElement, set: (t: string) => void }}
 */
function buildTrendStat(label) {
    const el = document.createElement('div');
    el.className = 'trend-stat';
    const lab = document.createElement('div');
    lab.className = 'trend-stat-label';
    lab.textContent = label;
    el.appendChild(lab);
    const val = document.createElement('div');
    val.className = 'trend-stat-value';
    val.textContent = '--';
    el.appendChild(val);
    return { el, set: (t) => { val.textContent = t; } };
}

/* ── Detail Panel ────────────────────────────────────────────── */

function createDetailPanel() {
    const overlay = document.createElement('div');
    overlay.className = 'detail-panel-overlay';

    const panel = document.createElement('div');
    panel.className = 'detail-panel';

    const panelHeader = document.createElement('div');
    panelHeader.className = 'detail-panel-header';
    const panelTitle = document.createElement('h3');
    panelTitle.textContent = 'Trace Detail';
    panelHeader.appendChild(panelTitle);

    const closeBtn = document.createElement('button');
    closeBtn.className = 'detail-panel-close';
    closeBtn.setAttribute('aria-label', 'Close detail panel');
    const svgNS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    const l1 = document.createElementNS(svgNS, 'line');
    l1.setAttribute('x1', '18'); l1.setAttribute('y1', '6');
    l1.setAttribute('x2', '6');  l1.setAttribute('y2', '18');
    svg.appendChild(l1);
    const l2 = document.createElementNS(svgNS, 'line');
    l2.setAttribute('x1', '6');  l2.setAttribute('y1', '6');
    l2.setAttribute('x2', '18'); l2.setAttribute('y2', '18');
    svg.appendChild(l2);
    closeBtn.appendChild(svg);
    panelHeader.appendChild(closeBtn);
    panel.appendChild(panelHeader);

    const panelBody = document.createElement('div');
    panelBody.className = 'detail-panel-body';
    panel.appendChild(panelBody);

    function hide() {
        overlay.classList.remove('open');
        panel.classList.remove('open');
    }

    closeBtn.addEventListener('click', hide);
    overlay.addEventListener('click', hide);

    function show(trace) {
        panelBody.textContent = '';
        panelTitle.textContent = `${trace.method} ${trace.path}`;

        const kv = document.createElement('table');
        kv.className = 'kv-table';

        const rows = [
            ['Timestamp', formatTime(trace.timestamp)],
            ['Method', () => createMethodBadge(trace.method)],
            ['Path', trace.path || '--'],
            ['Query String', trace.query_string || '--'],
            ['Status', () => createStatusCodeBadge(trace.status)],
            ['Duration', (trace.duration_ms != null ? trace.duration_ms.toFixed(1) : '--') + ' ms'],
            ['Client Host', trace.client_host || '--'],
            ['Content Type', trace.content_type || '--'],
            ['Content Length', trace.content_length != null ? String(trace.content_length) + ' bytes' : '--'],
            ['User Agent', trace.user_agent || '--'],
        ];

        for (const [label, value] of rows) {
            const tr = document.createElement('tr');
            const th = document.createElement('th');
            th.textContent = label;
            tr.appendChild(th);
            const td = document.createElement('td');
            if (typeof value === 'function') {
                td.appendChild(value());
            } else {
                const span = document.createElement('span');
                span.className = label === 'User Agent' ? 'text-sm' : 'mono';
                span.textContent = value;
                td.appendChild(span);
            }
            tr.appendChild(td);
            kv.appendChild(tr);
        }
        panelBody.appendChild(kv);

        overlay.classList.add('open');
        panel.classList.add('open');
    }

    return { overlay, panel, show, hide };
}

/**
 * Create a table row from a trace object.
 * @param {object} trace
 * @param {function} onClick
 * @returns {HTMLTableRowElement}
 */
function createTraceRow(trace, onClick) {
    const tr = document.createElement('tr');
    tr.classList.add('clickable');
    tr.addEventListener('click', () => onClick(trace));

    const tdTime = document.createElement('td');
    tdTime.textContent = formatTime(trace.timestamp);
    tdTime.className = 'text-mono text-sm';
    tr.appendChild(tdTime);

    const tdMethod = document.createElement('td');
    tdMethod.appendChild(createMethodBadge(trace.method));
    tr.appendChild(tdMethod);

    const tdPath = document.createElement('td');
    const pathSpan = document.createElement('span');
    pathSpan.className = 'mono';
    pathSpan.textContent = trace.path || '--';
    tdPath.appendChild(pathSpan);
    tr.appendChild(tdPath);

    const tdStatus = document.createElement('td');
    tdStatus.appendChild(createStatusCodeBadge(trace.status));
    tr.appendChild(tdStatus);

    const tdDuration = document.createElement('td');
    tdDuration.className = 'text-mono text-sm';
    const ms = trace.duration_ms != null ? trace.duration_ms.toFixed(1) : '--';
    tdDuration.textContent = ms + ' ms';
    tr.appendChild(tdDuration);

    return tr;
}

/* ── Render ───────────────────────────────────────────────────── */

/**
 * Render the traces view.
 * @param {HTMLElement} container
 * @param {import('../api.js').AdminAPI} api
 * @returns {function} Cleanup function.
 */
export async function render(container, api) {
    container.replaceChildren();

    const wrapper = document.createElement('div');
    wrapper.className = 'view-enter';

    // Page header
    const header = document.createElement('div');
    header.className = 'page-header';
    const headerLeft = document.createElement('div');
    const h1 = document.createElement('h1');
    h1.textContent = 'Traces';
    headerLeft.appendChild(h1);
    const sub = document.createElement('div');
    sub.className = 'page-subtitle';
    sub.textContent = 'application.traces';
    headerLeft.appendChild(sub);
    header.appendChild(headerLeft);

    // Header right: Pause/Resume + Clear buttons
    const headerRight = document.createElement('div');
    headerRight.style.display = 'flex';
    headerRight.style.gap = '8px';

    const pauseBtn = document.createElement('button');
    pauseBtn.className = 'btn btn-sm';
    pauseBtn.textContent = 'Pause';

    const clearBtn = document.createElement('button');
    clearBtn.className = 'btn btn-sm';
    clearBtn.textContent = 'Clear';

    headerRight.appendChild(pauseBtn);
    headerRight.appendChild(clearBtn);
    header.appendChild(headerRight);
    wrapper.appendChild(header);

    // Loading skeleton (four stat cards + analytics + a table)
    const loader = document.createElement('div');
    loader.appendChild(pageSkeleton({ stats: 4, rows: 6 }));
    wrapper.appendChild(loader);
    container.appendChild(wrapper);

    // Fetch initial traces (a wider window for meaningful analytics).
    let data;
    try {
        data = await api.get('/traces?limit=500');
    } catch (err) {
        wrapper.removeChild(loader);
        wrapper.appendChild(createEmptyStateCard({
            icon: 'alert',
            tone: 'danger',
            title: 'Failed to load traces',
            text: err.message,
        }));
        return () => {};
    }

    wrapper.removeChild(loader);

    // Trace data (mutable array)
    const traces = data.traces || [];

    // ── Detail panel ─────────────────────────────────────────
    const { overlay, panel, show } = createDetailPanel();
    wrapper.appendChild(overlay);
    wrapper.appendChild(panel);

    // ── Stat cards row ───────────────────────────────────────
    const statsRow = document.createElement('div');
    statsRow.className = 'grid-4 mb-lg';
    const totalStat = buildStatCard('Total Requests');
    const avgStat = buildStatCard('Avg Duration');
    const errStat = buildStatCard('Error Rate');
    const maxStat = buildStatCard('Max Latency');
    statsRow.appendChild(totalStat.card);
    statsRow.appendChild(avgStat.card);
    statsRow.appendChild(errStat.card);
    statsRow.appendChild(maxStat.card);
    wrapper.appendChild(statsRow);

    // ── Analytics row (status mix + latency distribution) ────
    const analyticsRow = document.createElement('div');
    analyticsRow.className = 'grid-2 mb-lg';

    // Status mix card
    const mixCard = document.createElement('div');
    mixCard.className = 'admin-card';
    const mixHeader = document.createElement('div');
    mixHeader.className = 'admin-card-header';
    const mixTitle = document.createElement('h3');
    mixTitle.textContent = 'Status Mix';
    mixHeader.appendChild(mixTitle);
    mixCard.appendChild(mixHeader);
    const mixBody = document.createElement('div');
    mixBody.className = 'admin-card-body';

    const statusBar = document.createElement('div');
    statusBar.className = 'status-bar';
    // Decorative — the per-group counts/percentages are read from the legend below.
    statusBar.setAttribute('aria-hidden', 'true');
    const statusLegend = document.createElement('div');
    statusLegend.className = 'status-legend';

    // Pre-build a segment + legend item per status group.
    const mixRefs = {};
    for (const meta of STATUS_META) {
        const seg = document.createElement('div');
        seg.className = 'status-bar-seg';
        seg.style.background = `var(${meta.color})`;
        seg.style.width = '0%';
        seg.title = meta.label;
        statusBar.appendChild(seg);

        const item = document.createElement('div');
        item.className = 'status-legend-item';
        const dot = document.createElement('span');
        dot.className = 'status-legend-dot';
        dot.style.background = `var(${meta.color})`;
        item.appendChild(dot);
        const lab = document.createElement('span');
        lab.textContent = meta.label;
        item.appendChild(lab);
        const count = document.createElement('span');
        count.className = 'status-legend-count';
        count.textContent = '0';
        item.appendChild(count);
        const pct = document.createElement('span');
        pct.className = 'status-legend-pct';
        pct.textContent = '(0%)';
        item.appendChild(pct);

        mixRefs[meta.key] = { seg, item, count, pct };
        statusLegend.appendChild(item);
    }
    mixBody.appendChild(statusBar);
    mixBody.appendChild(statusLegend);
    mixCard.appendChild(mixBody);
    analyticsRow.appendChild(mixCard);

    // Latency distribution card
    const latCard = document.createElement('div');
    latCard.className = 'admin-card';
    const latHeader = document.createElement('div');
    latHeader.className = 'admin-card-header';
    const latTitle = document.createElement('h3');
    latTitle.textContent = 'Latency Distribution';
    latHeader.appendChild(latTitle);
    latCard.appendChild(latHeader);
    const latBody = document.createElement('div');
    latBody.className = 'admin-card-body';

    const latCanvasWrap = document.createElement('div');
    latCanvasWrap.style.height = '180px';
    const latCanvas = document.createElement('canvas');
    latCanvasWrap.appendChild(latCanvas);
    latBody.appendChild(latCanvasWrap);

    // Percentile strip
    const pctStrip = document.createElement('div');
    pctStrip.className = 'trend-stats';
    const pctStats = {
        p50: buildTrendStat('p50'),
        p90: buildTrendStat('p90'),
        p95: buildTrendStat('p95'),
        p99: buildTrendStat('p99'),
    };
    pctStrip.appendChild(pctStats.p50.el);
    pctStrip.appendChild(pctStats.p90.el);
    pctStrip.appendChild(pctStats.p95.el);
    pctStrip.appendChild(pctStats.p99.el);
    latBody.appendChild(pctStrip);

    latCard.appendChild(latBody);
    analyticsRow.appendChild(latCard);

    wrapper.appendChild(analyticsRow);

    // ── Status filter pills ──────────────────────────────────
    let activeFilter = '';
    const pillBar = document.createElement('div');
    pillBar.className = 'filter-pills mb-md';

    const filterOptions = ['All', '2xx', '3xx', '4xx', '5xx'];
    const pillButtons = [];

    for (const label of filterOptions) {
        const pill = document.createElement('button');
        pill.className = 'filter-pill';
        pill.textContent = label;
        if (label === 'All') pill.classList.add('active');
        pill.addEventListener('click', () => {
            activeFilter = label === 'All' ? '' : label;
            for (const p of pillButtons) p.classList.remove('active');
            pill.classList.add('active');
            renderTraces();
        });
        pillButtons.push(pill);
        pillBar.appendChild(pill);
    }
    wrapper.appendChild(pillBar);

    // ── Trace table ──────────────────────────────────────────
    const tableCard = document.createElement('div');
    tableCard.className = 'admin-card';

    const tableHeader = document.createElement('div');
    tableHeader.className = 'admin-card-header';
    const tableTitle = document.createElement('h3');
    tableTitle.textContent = 'HTTP Traces';
    tableHeader.appendChild(tableTitle);

    const liveIndicator = document.createElement('span');
    liveIndicator.className = 'badge badge-success';
    const liveDot = document.createElement('span');
    liveDot.className = 'badge-dot';
    liveIndicator.appendChild(liveDot);
    const liveText = document.createElement('span');
    liveText.textContent = 'LIVE';
    liveIndicator.appendChild(liveText);
    tableHeader.appendChild(liveIndicator);

    tableCard.appendChild(tableHeader);

    const tableWrap = document.createElement('div');
    tableWrap.className = 'admin-table-wrapper';
    tableWrap.style.maxHeight = '500px';
    tableWrap.style.overflowY = 'auto';

    const table = document.createElement('table');
    table.className = 'admin-table';

    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    const colHeaders = ['Time', 'Method', 'Path', 'Status', 'Duration'];
    for (const label of colHeaders) {
        const th = document.createElement('th');
        th.textContent = label;
        headRow.appendChild(th);
    }
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');

    function renderTraces() {
        tbody.replaceChildren();
        const filtered = activeFilter
            ? traces.filter((t) => statusGroup(t.status) === activeFilter)
            : traces;

        if (filtered.length === 0) {
            const tr = document.createElement('tr');
            tr.className = 'trace-empty-row';
            const td = document.createElement('td');
            td.colSpan = 5;
            td.style.textAlign = 'center';
            td.style.padding = '32px 16px';
            td.style.color = 'var(--admin-text-muted)';
            td.textContent = activeFilter ? `No ${activeFilter} traces` : 'No traces recorded';
            tr.appendChild(td);
            tbody.appendChild(tr);
            return;
        }
        for (const trace of filtered) {
            tbody.appendChild(createTraceRow(trace, show));
        }
    }

    renderTraces();
    table.appendChild(tbody);
    tableWrap.appendChild(table);
    tableCard.appendChild(tableWrap);
    wrapper.appendChild(tableCard);

    // ── Analytics rendering ──────────────────────────────────
    let latChart = null;

    function refreshAnalytics() {
        const a = computeAnalytics(traces);

        totalStat.valueEl.textContent = String(a.total);
        avgStat.valueEl.textContent = formatMs(a.avg);
        maxStat.valueEl.textContent = formatMs(a.max);

        errStat.valueEl.textContent = a.errorRate.toFixed(1) + '%';
        // Tint the error rate when elevated.
        errStat.valueEl.style.color =
            a.errorRate >= 5 ? 'var(--admin-danger)'
                : a.errorRate > 0 ? 'var(--admin-warning)'
                    : '';

        // Status mix bar + legend.
        for (const meta of STATUS_META) {
            const c = a.counts[meta.key] || 0;
            const refs = mixRefs[meta.key];
            const pctNum = a.total ? (c / a.total) * 100 : 0;
            refs.seg.style.width = pctNum + '%';
            refs.count.textContent = String(c);
            refs.pct.textContent = `(${pctNum.toFixed(0)}%)`;
            // Hide empty "other"/legend rows for groups with no traffic to reduce noise.
            refs.item.style.display = c === 0 && meta.key === 'other' ? 'none' : '';
        }

        // Percentile strip.
        pctStats.p50.set(formatMs(a.p50));
        pctStats.p90.set(formatMs(a.p90));
        pctStats.p95.set(formatMs(a.p95));
        pctStats.p99.set(formatMs(a.p99));

        // Latency histogram.
        if (latChart) {
            latChart.update([...a.buckets], LATENCY_BUCKETS.map((b) => b.label));
        }
    }

    // Build the histogram once the canvas is laid out, then paint analytics.
    requestAnimationFrame(() => {
        if (!latCanvas.isConnected) return;
        const a0 = computeAnalytics(traces);
        latChart = createBarChart(latCanvas, {
            data: a0.buckets,
            labels: LATENCY_BUCKETS.map((b) => b.label),
        });
        refreshAnalytics();
    });
    // Paint the non-chart analytics immediately (chart fills in on next frame).
    refreshAnalytics();

    // ── SSE: real-time trace updates (analytics refresh debounced) ──
    let paused = false;
    let analyticsTimer = null;

    function scheduleAnalyticsRefresh() {
        if (analyticsTimer !== null) return;
        analyticsTimer = setTimeout(() => {
            analyticsTimer = null;
            refreshAnalytics();
        }, 400);
    }

    sse.connectTyped('/traces', 'trace', (traceData) => {
        if (paused) return;
        traces.unshift(traceData);
        // Bound the in-memory window (newest kept).
        if (traces.length > MAX_TRACES) traces.length = MAX_TRACES;

        // Only insert into DOM if the trace matches the active filter.
        if (!activeFilter || statusGroup(traceData.status) === activeFilter) {
            // Drop the empty-state placeholder row before inserting real data.
            if (tbody.querySelector('.trace-empty-row')) tbody.replaceChildren();
            // insertBefore(node, null) appends when the table is empty.
            tbody.insertBefore(createTraceRow(traceData, show), tbody.firstChild);
            // Bound the DOM in lockstep with the buffer.
            while (tbody.childElementCount > MAX_TRACES) {
                tbody.removeChild(tbody.lastElementChild);
            }
        }
        scheduleAnalyticsRefresh();
    });

    // ── Pause/Resume ─────────────────────────────────────────
    pauseBtn.addEventListener('click', () => {
        paused = !paused;
        pauseBtn.textContent = paused ? 'Resume' : 'Pause';
        if (paused) {
            liveText.textContent = 'PAUSED';
            liveIndicator.className = 'badge badge-warning';
        } else {
            liveText.textContent = 'LIVE';
            liveIndicator.className = 'badge badge-success';
        }
    });

    // ── Clear ────────────────────────────────────────────────
    clearBtn.addEventListener('click', () => {
        traces.length = 0;
        renderTraces();
        refreshAnalytics();
    });

    // ── Cleanup ──────────────────────────────────────────────
    return function cleanup() {
        sse.disconnect('/traces');
        if (analyticsTimer !== null) {
            clearTimeout(analyticsTimer);
            analyticsTimer = null;
        }
        if (latChart) {
            latChart.destroy();
            latChart = null;
        }
    };
}
