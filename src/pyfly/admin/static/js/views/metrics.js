/**
 * PyFly Admin — Metrics View.
 *
 * Metric browser with a searchable list and a live drill-down panel.
 * Split layout: metric names on the left, live trend + measurements on
 * the right. Selecting a numeric metric starts a rolling time-series
 * chart that polls the metric on the configured refresh interval.
 *
 * Data sources:
 *   GET /admin/api/metrics          -> { names: [...], available: boolean }
 *   GET /admin/api/metrics/{name}   -> { name, measurements: [{statistic, value, tags}, ...] }
 *   GET /admin/api/settings         -> { refreshInterval, ... }
 */

/* global Chart */

import { createLineChart } from '../charts.js';
import { createEmptyStateCard } from '../components/empty-state.js';
import { createFilterToolbar } from '../components/filter-toolbar.js';
import { pageSkeleton } from '../components/skeleton.js';
import { sse } from '../sse.js';

/* ── Constants ────────────────────────────────────────────────── */

const MAX_POINTS = 60;
const DEFAULT_INTERVAL_MS = 5000;
const MIN_INTERVAL_MS = 1000;

/* ── Value helpers ────────────────────────────────────────────── */

/**
 * Is a measurement value chartable as a number?
 * @param {*} v
 * @returns {boolean}
 */
function isNumericValue(v) {
    if (typeof v === 'number') return Number.isFinite(v);
    if (typeof v === 'string' && v.trim() !== '') return Number.isFinite(Number(v));
    return false;
}

/**
 * Coerce a numeric-ish value to a Number.
 * @param {number|string} v
 * @returns {number}
 */
function toNumber(v) {
    return typeof v === 'number' ? v : Number(v);
}

/**
 * Stable identity for a measurement (statistic + tags), used to keep the
 * chart tracking the same series across polls.
 * @param {{statistic?: string, tags?: object}} m
 * @returns {string}
 */
function measurementKey(m) {
    const tags = m.tags && Object.keys(m.tags).length > 0 ? JSON.stringify(m.tags) : '';
    return `${m.statistic || 'value'}${tags ? ' ' + tags : ''}`;
}

/**
 * Filter a measurements array down to the numeric ones.
 * @param {Array} measurements
 * @returns {Array}
 */
function numericMeasurements(measurements) {
    return (measurements || []).filter((m) => isNumericValue(m.value));
}

/**
 * Format a number for compact display.
 * @param {number|null} n
 * @returns {string}
 */
function formatNumber(n) {
    if (n == null || Number.isNaN(n)) return '--';
    if (!Number.isFinite(n)) return String(n);
    if (Number.isInteger(n)) return n.toLocaleString();
    const abs = Math.abs(n);
    if (abs >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 1 });
    if (abs >= 1) return n.toFixed(2);
    return n.toFixed(4);
}

/** Current wall-clock label. */
function nowLabel() {
    return new Date().toLocaleTimeString();
}

/* ── Helpers ──────────────────────────────────────────────────── */

/**
 * Build a measurements table for a single metric.
 * @param {Array<{statistic: string, value: *, tags: object}>} measurements
 * @returns {HTMLElement}
 */
function buildMeasurementsTable(measurements) {
    if (!measurements || measurements.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'text-muted text-sm';
        empty.style.padding = '16px 0';
        empty.textContent = 'No data available';
        return empty;
    }

    const tableWrap = document.createElement('div');
    tableWrap.className = 'admin-table-wrapper';

    const table = document.createElement('table');
    table.className = 'admin-table';

    // Head
    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    for (const label of ['Statistic', 'Value', 'Tags']) {
        const th = document.createElement('th');
        th.textContent = label;
        headRow.appendChild(th);
    }
    thead.appendChild(headRow);
    table.appendChild(thead);

    // Body
    const tbody = document.createElement('tbody');
    for (const m of measurements) {
        const tr = document.createElement('tr');

        const tdStat = document.createElement('td');
        tdStat.textContent = m.statistic || '--';
        tr.appendChild(tdStat);

        const tdVal = document.createElement('td');
        const valSpan = document.createElement('span');
        valSpan.className = 'text-mono';
        valSpan.textContent = m.value != null ? String(m.value) : '--';
        tdVal.appendChild(valSpan);
        tr.appendChild(tdVal);

        const tdTags = document.createElement('td');
        if (m.tags && Object.keys(m.tags).length > 0) {
            const tagSpan = document.createElement('span');
            tagSpan.className = 'text-mono text-sm';
            tagSpan.textContent = JSON.stringify(m.tags);
            tdTags.appendChild(tagSpan);
        } else {
            tdTags.className = 'text-muted';
            tdTags.textContent = '--';
        }
        tr.appendChild(tdTags);

        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    tableWrap.appendChild(table);
    return tableWrap;
}

/**
 * Build one stat block for the trend summary strip.
 * @param {string} label
 * @returns {{ el: HTMLElement, set: (text: string) => void }}
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
    return { el, set: (text) => { val.textContent = text; } };
}

/* ── Render ───────────────────────────────────────────────────── */

/**
 * Render the metrics browser view.
 * @param {HTMLElement} container
 * @param {import('../api.js').AdminAPI} api
 * @returns {Promise<function>} cleanup
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
    h1.textContent = 'Metrics';
    headerLeft.appendChild(h1);
    const sub = document.createElement('div');
    sub.className = 'page-subtitle';
    sub.textContent = 'application.metrics';
    headerLeft.appendChild(sub);
    header.appendChild(headerLeft);
    wrapper.appendChild(header);

    // Loading skeleton (3 stat cards + a metric list)
    const loader = document.createElement('div');
    loader.appendChild(pageSkeleton({ stats: 3, rows: 6 }));
    wrapper.appendChild(loader);
    container.appendChild(wrapper);

    // Resolve the poll interval from server settings (best-effort).
    let intervalMs = DEFAULT_INTERVAL_MS;
    let data;
    try {
        const [names, settings] = await Promise.all([
            api.get('/metrics'),
            api.get('/settings').catch(() => null),
        ]);
        data = names;
        if (settings && Number.isFinite(settings.refreshInterval)) {
            intervalMs = Math.max(MIN_INTERVAL_MS, settings.refreshInterval);
        }
    } catch (err) {
        wrapper.removeChild(loader);
        wrapper.appendChild(createEmptyStateCard({
            icon: 'alert',
            tone: 'danger',
            title: 'Failed to load metrics',
            text: err.message,
        }));
        return () => {};
    }

    wrapper.removeChild(loader);

    // If metrics not available at all
    if (data.available === false) {
        wrapper.appendChild(createEmptyStateCard({
            icon: 'activity',
            title: 'Metrics not available',
            text: 'The metrics registry is not enabled for this application.',
        }));
        return () => {};
    }

    const names = data.names || [];
    const hasPrometheus = data.has_prometheus || false;

    if (names.length === 0) {
        wrapper.appendChild(createEmptyStateCard({
            icon: 'activity',
            title: 'No metrics registered',
            text: 'No metrics have been published by this application yet.',
        }));
        return () => {};
    }

    // ── Filter toolbar ──────────────────────────────────────────
    const toolbar = createFilterToolbar({
        placeholder: 'Search metrics...',
        pills: [
            { label: 'All', value: '' },
            { label: 'HTTP', value: 'http' },
            { label: 'System', value: 'system' },
            { label: 'Process', value: 'process' },
            { label: 'Custom', value: 'custom' },
        ],
        onFilter: ({ search, pill }) => {
            renderMetricList(search, pill);
        },
        totalCount: names.length,
    });
    wrapper.appendChild(toolbar);

    // ── Stat cards row ──────────────────────────────────────────
    const statsRow = document.createElement('div');
    statsRow.className = 'grid-4 mb-lg';

    const totalCard = document.createElement('div');
    totalCard.className = 'stat-card';
    const totalContent = document.createElement('div');
    totalContent.className = 'stat-card-content';
    const totalVal = document.createElement('div');
    totalVal.className = 'stat-card-value';
    totalVal.textContent = String(names.length);
    totalContent.appendChild(totalVal);
    const totalLabel = document.createElement('div');
    totalLabel.className = 'stat-card-label';
    totalLabel.textContent = 'Total Metrics';
    totalContent.appendChild(totalLabel);
    totalCard.appendChild(totalContent);
    statsRow.appendChild(totalCard);

    // Built-in count
    const builtinCard = document.createElement('div');
    builtinCard.className = 'stat-card';
    const builtinContent = document.createElement('div');
    builtinContent.className = 'stat-card-content';
    const builtinVal = document.createElement('div');
    builtinVal.className = 'stat-card-value';
    builtinVal.textContent = String(data.builtin_count || 0);
    builtinContent.appendChild(builtinVal);
    const builtinLabel = document.createElement('div');
    builtinLabel.className = 'stat-card-label';
    builtinLabel.textContent = 'Built-in';
    builtinContent.appendChild(builtinLabel);
    builtinCard.appendChild(builtinContent);
    statsRow.appendChild(builtinCard);

    // Prometheus count
    const promCard = document.createElement('div');
    promCard.className = 'stat-card';
    const promContent = document.createElement('div');
    promContent.className = 'stat-card-content';
    const promVal = document.createElement('div');
    promVal.className = 'stat-card-value';
    promVal.textContent = hasPrometheus ? String(data.prometheus_count || 0) : '--';
    promContent.appendChild(promVal);
    const promLabel = document.createElement('div');
    promLabel.className = 'stat-card-label';
    promLabel.textContent = hasPrometheus ? 'Prometheus' : 'No Prometheus';
    promContent.appendChild(promLabel);
    promCard.appendChild(promContent);
    statsRow.appendChild(promCard);

    wrapper.appendChild(statsRow);

    // ── Split layout ────────────────────────────────────────────
    const splitLayout = document.createElement('div');
    splitLayout.className = 'metrics-split';

    // ── Left panel: metric list ─────────────────────────────────
    const leftPanel = document.createElement('div');
    leftPanel.className = 'admin-card metrics-list-panel';

    const leftHeader = document.createElement('div');
    leftHeader.className = 'admin-card-header';
    const leftTitle = document.createElement('h3');
    leftTitle.textContent = 'Metrics';
    leftHeader.appendChild(leftTitle);
    const countBadge = document.createElement('span');
    countBadge.className = 'badge badge-neutral';
    countBadge.textContent = String(names.length);
    leftHeader.appendChild(countBadge);
    leftPanel.appendChild(leftHeader);

    const leftBody = document.createElement('div');
    leftBody.style.padding = '12px';
    leftBody.style.flex = '1';
    leftBody.style.minHeight = '0';
    leftBody.style.display = 'flex';
    leftBody.style.flexDirection = 'column';

    // Metric list container — fills the panel height and scrolls internally.
    const metricList = document.createElement('div');
    metricList.style.flex = '1';
    metricList.style.minHeight = '0';
    metricList.style.overflowY = 'auto';

    let activeItem = null;

    /**
     * Build and render metric name items, filtered by search text and pill.
     * @param {string} search  Lowercase search text.
     * @param {string} pill    Pill value (prefix filter).
     */
    function renderMetricList(search, pill) {
        metricList.replaceChildren();
        activeItem = null;

        const filtered = names.filter((n) => {
            const lower = n.toLowerCase();
            // Pill filter: metric name must start with the pill value
            if (pill && !lower.startsWith(pill)) return false;
            // Search filter: metric name must include the search text
            if (search && !lower.includes(search)) return false;
            return true;
        });

        // Update toolbar count
        toolbar.updateCount(filtered.length, names.length);

        // Update left panel badge count
        countBadge.textContent = String(filtered.length);

        if (filtered.length === 0) {
            const noMatch = document.createElement('div');
            noMatch.className = 'text-muted text-sm';
            noMatch.style.padding = '12px 8px';
            noMatch.textContent = 'No matching metrics';
            metricList.appendChild(noMatch);
            return;
        }

        for (const name of filtered) {
            const item = document.createElement('div');
            item.style.padding = '8px 12px';
            item.style.cursor = 'pointer';
            item.style.borderRadius = 'var(--admin-radius)';
            item.style.fontSize = '0.85rem';
            item.style.fontFamily = 'var(--admin-font-mono)';
            item.style.transition = 'background var(--admin-transition)';
            item.style.wordBreak = 'break-all';
            item.textContent = name;

            item.addEventListener('mouseenter', () => {
                if (item !== activeItem) {
                    item.style.background = 'var(--admin-surface-hover)';
                }
            });
            item.addEventListener('mouseleave', () => {
                if (item !== activeItem) {
                    item.style.background = '';
                }
            });

            item.addEventListener('click', () => {
                // Deselect previous
                if (activeItem) {
                    activeItem.style.background = '';
                    activeItem.style.color = '';
                }
                activeItem = item;
                item.style.background = 'var(--admin-primary-dim)';
                item.style.color = 'var(--admin-primary)';
                loadMetricDetail(name);
            });

            metricList.appendChild(item);
        }
    }

    renderMetricList('', '');
    leftBody.appendChild(metricList);
    leftPanel.appendChild(leftBody);
    splitLayout.appendChild(leftPanel);

    // ── Right panel: metric detail ──────────────────────────────
    const rightPanel = document.createElement('div');
    rightPanel.className = 'metrics-detail-panel';

    const detailCard = document.createElement('div');
    detailCard.className = 'admin-card';

    const detailBody = document.createElement('div');
    detailBody.className = 'admin-card-body';

    // Initial placeholder
    const placeholder = document.createElement('div');
    placeholder.className = 'empty-state';
    const placeholderText = document.createElement('div');
    placeholderText.className = 'empty-state-text';
    placeholderText.textContent = 'Select a metric from the list to view its live trend';
    placeholder.appendChild(placeholderText);
    detailBody.appendChild(placeholder);

    detailCard.appendChild(detailBody);
    rightPanel.appendChild(detailCard);
    splitLayout.appendChild(rightPanel);

    wrapper.appendChild(splitLayout);

    /* ── Live trend state ───────────────────────────────────────
     * Only one metric is tracked at a time. Selecting another (or
     * navigating away) tears down the timer + chart first.
     */
    let liveChart = null;
    let points = [];                 // [{ t: epochMs, v: number, label: string }]
    let mode = 'value';              // 'value' | 'rate'
    let paused = false;
    let selectedKey = null;          // measurement currently charted
    let loadToken = 0;               // guards against out-of-order metric loads

    /** Tear down the live SSE subscription + chart and reset the rolling buffer. */
    function stopLive() {
        sse.disconnect('/metrics');
        if (liveChart) {
            liveChart.destroy();
            liveChart = null;
        }
        points = [];
    }

    /** Push a reading into the rolling window. */
    function pushPoint(v) {
        points.push({ t: Date.now(), v, label: nowLabel() });
        if (points.length > MAX_POINTS) points.shift();
    }

    /**
     * Derive the chart series for the current mode.
     * In 'rate' mode each point is the per-second delta from the prior point.
     * @returns {{ labels: string[], data: number[] }}
     */
    function computeSeries() {
        const labels = points.map((p) => p.label);
        if (mode === 'rate') {
            // A downward step is shown as-is (not clamped): for a gauge a
            // negative per-second rate is correct, and for a counter a drop
            // signals a reset (e.g. process restart) — both are honest signals.
            const dataPts = points.map((p, i) => {
                if (i === 0) return 0;
                const dt = (p.t - points[i - 1].t) / 1000;
                if (dt <= 0) return 0;
                return (p.v - points[i - 1].v) / dt;
            });
            return { labels, data: dataPts };
        }
        return { labels, data: points.map((p) => p.v) };
    }

    /**
     * Find the chosen measurement in a fresh measurements array, falling
     * back to the first numeric measurement if the selected one vanished.
     * @param {Array} measurements
     * @returns {object|null}
     */
    function pickChosen(measurements) {
        const found = (measurements || []).find((m) => measurementKey(m) === selectedKey);
        if (found && isNumericValue(found.value)) return found;
        const numeric = numericMeasurements(measurements);
        return numeric.length > 0 ? numeric[0] : null;
    }

    /**
     * Load and display metric detail, wiring up the live trend chart.
     * @param {string} metricName
     */
    async function loadMetricDetail(metricName) {
        // Always tear down any prior live session first.
        stopLive();
        mode = 'value';
        paused = false;
        selectedKey = null;
        // Token guards against a slower earlier fetch resolving after a newer
        // selection — without it, a stale load could install a second timer.
        const myToken = ++loadToken;

        detailBody.replaceChildren();

        // Loading state
        const loadingEl = document.createElement('div');
        loadingEl.className = 'loading-spinner';
        detailBody.appendChild(loadingEl);

        let detail;
        try {
            detail = await api.get('/metrics/' + encodeURIComponent(metricName));
        } catch (err) {
            if (myToken !== loadToken) return;  // superseded by a newer selection
            detailBody.replaceChildren();
            const errMsg = document.createElement('div');
            errMsg.className = 'text-muted text-sm';
            errMsg.style.padding = '16px 0';
            errMsg.textContent = 'Failed to load metric detail: ' + err.message;
            detailBody.appendChild(errMsg);
            return;
        }

        // A newer selection (or navigation) superseded this load while awaiting.
        if (myToken !== loadToken) return;

        detailBody.replaceChildren();

        // Metric name header
        const nameHeader = document.createElement('h3');
        nameHeader.style.marginBottom = '4px';
        nameHeader.style.fontSize = '1rem';
        nameHeader.style.fontWeight = '600';
        nameHeader.style.fontFamily = 'var(--admin-font-mono)';
        nameHeader.style.wordBreak = 'break-all';
        nameHeader.textContent = detail.name || metricName;
        detailBody.appendChild(nameHeader);

        // Description and metadata
        if (detail.description || detail.unit || detail.source) {
            const meta = document.createElement('div');
            meta.style.marginBottom = '16px';
            meta.style.fontSize = '0.8rem';
            meta.style.color = 'var(--admin-text-muted)';
            const parts = [];
            if (detail.description) parts.push(detail.description);
            if (detail.unit) parts.push(`Unit: ${detail.unit}`);
            if (detail.source) parts.push(`Source: ${detail.source}`);
            meta.textContent = parts.join(' · ');
            detailBody.appendChild(meta);
        }

        const measurements = detail.measurements || [];
        const numeric = numericMeasurements(measurements);

        // Container for the (live-refreshing) measurements table.
        const tableHost = document.createElement('div');

        function renderTable(ms) {
            tableHost.replaceChildren();
            tableHost.appendChild(buildMeasurementsTable(ms));
        }

        if (numeric.length === 0) {
            // Non-numeric metric — no chart, just the snapshot table.
            const note = document.createElement('div');
            note.className = 'text-muted text-sm';
            note.style.margin = '4px 0 16px';
            note.textContent = 'No numeric value to trend — showing the latest snapshot.';
            detailBody.appendChild(note);
            renderTable(measurements);
            detailBody.appendChild(tableHost);
            return;
        }

        // Track the first numeric measurement by default.
        selectedKey = measurementKey(numeric[0]);

        // ── Trend card ──────────────────────────────────────────
        const trendCard = document.createElement('div');
        trendCard.className = 'admin-card mb-lg';

        // Header: title + live indicator
        const trendHeader = document.createElement('div');
        trendHeader.className = 'admin-card-header';
        const trendTitle = document.createElement('h3');
        trendTitle.textContent = 'Live Trend';
        trendHeader.appendChild(trendTitle);

        const liveBadge = document.createElement('span');
        liveBadge.className = 'trend-live';
        const liveDot = document.createElement('span');
        liveDot.className = 'trend-live-dot';
        liveBadge.appendChild(liveDot);
        const liveText = document.createElement('span');
        liveText.textContent = `live · ${(intervalMs / 1000).toFixed(intervalMs % 1000 ? 1 : 0)}s`;
        liveBadge.appendChild(liveText);
        trendHeader.appendChild(liveBadge);
        trendCard.appendChild(trendHeader);

        const trendBody = document.createElement('div');
        trendBody.className = 'admin-card-body';

        // Toolbar: measurement selector + value/rate toggle + pause
        const trendToolbar = document.createElement('div');
        trendToolbar.className = 'trend-toolbar';

        // Value / Rate segmented toggle
        const seg = document.createElement('div');
        seg.className = 'seg-toggle';
        seg.setAttribute('role', 'group');
        seg.setAttribute('aria-label', 'Trend mode');
        const segButtons = {};
        for (const m of [['value', 'Value'], ['rate', 'Rate Δ/s']]) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'seg-btn' + (m[0] === mode ? ' active' : '');
            btn.textContent = m[1];
            btn.addEventListener('click', () => {
                if (mode === m[0]) return;
                mode = m[0];
                segButtons.value.classList.toggle('active', mode === 'value');
                segButtons.rate.classList.toggle('active', mode === 'rate');
                refresh();
            });
            segButtons[m[0]] = btn;
            seg.appendChild(btn);
        }
        trendToolbar.appendChild(seg);

        // Pause / Resume
        const pauseBtn = document.createElement('button');
        pauseBtn.type = 'button';
        pauseBtn.className = 'btn btn-sm';
        pauseBtn.textContent = 'Pause';
        pauseBtn.addEventListener('click', () => {
            paused = !paused;
            pauseBtn.textContent = paused ? 'Resume' : 'Pause';
            liveBadge.classList.toggle('paused', paused);
            liveText.textContent = paused ? 'paused' : `live · ${(intervalMs / 1000).toFixed(intervalMs % 1000 ? 1 : 0)}s`;
        });
        trendToolbar.appendChild(pauseBtn);

        trendBody.appendChild(trendToolbar);

        // Canvas
        const canvasWrap = document.createElement('div');
        canvasWrap.style.height = '220px';
        canvasWrap.style.marginTop = '12px';
        const canvas = document.createElement('canvas');
        canvasWrap.appendChild(canvas);
        trendBody.appendChild(canvasWrap);

        // Summary stats strip
        const statsStrip = document.createElement('div');
        statsStrip.className = 'trend-stats';
        const statCurrent = buildTrendStat('Current');
        const statMin = buildTrendStat('Min');
        const statMax = buildTrendStat('Max');
        const statAvg = buildTrendStat('Avg');
        statsStrip.appendChild(statCurrent.el);
        statsStrip.appendChild(statMin.el);
        statsStrip.appendChild(statMax.el);
        statsStrip.appendChild(statAvg.el);
        trendBody.appendChild(statsStrip);

        trendCard.appendChild(trendBody);
        detailBody.appendChild(trendCard);

        // Measurements table below the trend.
        renderTable(measurements);
        detailBody.appendChild(tableHost);

        // Seed the first reading with the metric's total (sum across label sets) —
        // the same quantity the SSE `values` stream pushes for live updates.
        const metricTotal = (ms) => numericMeasurements(ms).reduce((s, m) => s + toNumber(m.value), 0);
        pushPoint(metricTotal(measurements));

        /** Recompute series + stats and repaint the chart. */
        function refresh() {
            const { labels, data: series } = computeSeries();
            if (liveChart) liveChart.update([...series], [...labels]);

            const finite = series.filter((x) => Number.isFinite(x));
            if (finite.length === 0) {
                statCurrent.set('--');
                statMin.set('--');
                statMax.set('--');
                statAvg.set('--');
                return;
            }
            const current = series[series.length - 1];
            const min = Math.min(...finite);
            const max = Math.max(...finite);
            const avg = finite.reduce((a, b) => a + b, 0) / finite.length;
            statCurrent.set(formatNumber(current));
            statMin.set(formatNumber(min));
            statMax.set(formatNumber(max));
            statAvg.set(formatNumber(avg));
        }

        // Create the chart once the canvas is laid out.
        requestAnimationFrame(() => {
            // Guard: the view may have been torn down before the frame fired.
            if (!canvas.isConnected) return;
            const { labels, data: series } = computeSeries();
            liveChart = createLineChart(canvas, {
                label: metricName,
                color: '--admin-primary',
                data: [...series],
                labels: [...labels],
            });
            refresh();
        });

        // ── Live updates via SSE (push, not REST polling) ───────
        // The metrics SSE stream pushes a {name: total_value} snapshot at the
        // server cadence; track the selected metric's value live.
        sse.connectTyped('/metrics', 'metrics', (data) => {
            if (paused) return;
            // A metric switch (or navigation) bumps the token — drop stale ticks
            // so another metric's reading can't land in this buffer/chart.
            if (myToken !== loadToken) return;
            const values = data && data.values;
            if (!values || !(metricName in values)) return;
            const v = values[metricName];
            if (!isNumericValue(v)) return;
            pushPoint(toNumber(v));
            refresh();
        });
    }

    // ── Cleanup ─────────────────────────────────────────────────
    return function cleanup() {
        loadToken++;  // abort any in-flight metric load
        stopLive();
    };
}
