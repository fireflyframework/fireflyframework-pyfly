/**
 * PyFly Admin — Runtime Monitoring View.
 *
 * Displays real-time process metrics: memory RSS, thread count,
 * GC collections (gen0/gen1/gen2), and CPU time. Each metric
 * is shown as a stat card and tracked on a 5-minute rolling
 * line chart (60 data points at 5s intervals).
 *
 * Data source: GET /admin/api/runtime + SSE /runtime (event: runtime)
 */

/* global Chart */

import { createLineChart, cssVar, hexToRgba } from '../charts.js';
import { createEmptyStateCard } from '../components/empty-state.js';
import { pageSkeleton } from '../components/skeleton.js';
import { sse } from '../sse.js';

/* ── Constants ──────────────────────────────────────────────── */

const MAX_DATA_POINTS = 60;

/* ── Helpers ────────────────────────────────────────────────── */

/**
 * Format a Unix timestamp (seconds) to a locale time string.
 * Falls back to the current time if no timestamp is provided.
 * @param {number|undefined} timestamp
 * @returns {string}
 */
function formatTimestamp(timestamp) {
    if (timestamp) {
        return new Date(timestamp * 1000).toLocaleTimeString();
    }
    return new Date().toLocaleTimeString();
}

/**
 * Push a value into a rolling-window array, trimming to MAX_DATA_POINTS.
 * @param {Array} arr
 * @param {*} value
 */
function pushRolling(arr, value) {
    arr.push(value);
    if (arr.length > MAX_DATA_POINTS) {
        arr.shift();
    }
}

/**
 * Create a stat card element.
 * @param {{ label: string, value: string, subtitle?: string, iconClass?: string }} opts
 * @returns {HTMLElement}
 */
function createStatCard({ label, value, subtitle, iconClass = 'primary' }) {
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

    if (subtitle) {
        const subEl = document.createElement('div');
        subEl.style.fontSize = '0.7rem';
        subEl.style.color = 'var(--admin-text-muted)';
        subEl.style.marginTop = '2px';
        subEl.textContent = subtitle;
        content.appendChild(subEl);
    }

    card.appendChild(content);

    const icon = document.createElement('div');
    icon.className = `stat-card-icon ${iconClass}`;
    card.appendChild(icon);

    return card;
}

/**
 * Build a chart card with a header title and a canvas element.
 * @param {string} title
 * @returns {{ card: HTMLElement, canvas: HTMLCanvasElement }}
 */
function buildChartCard(title) {
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
 * Resolve a CSS variable to a colour value with a fallback.
 * @param {string} varName
 * @param {string} fallback
 * @returns {string}
 */
function resolveColor(varName, fallback) {
    return cssVar(varName) || fallback;
}

/**
 * Return shared Chart.js theme options for multi-dataset charts.
 * @returns {object}
 */
function chartThemeOptions() {
    const text = cssVar('--admin-text') || '#e2e8f0';
    const muted = cssVar('--admin-text-muted') || '#64748b';
    const grid = cssVar('--admin-border-subtle') || '#162032';
    const font = cssVar('--admin-font-mono') || 'monospace';

    return {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 300 },
        scales: {
            x: {
                grid: { color: grid },
                ticks: { color: muted, font: { family: font, size: 10 } },
            },
            y: {
                beginAtZero: true,
                grid: { color: grid },
                ticks: { color: muted, font: { family: font, size: 11 } },
            },
        },
        plugins: {
            legend: {
                display: true,
                labels: { color: muted, font: { family: font, size: 11 } },
            },
            tooltip: {
                backgroundColor: cssVar('--admin-card-bg') || '#1a1a2e',
                titleColor: text,
                bodyColor: muted,
                borderColor: grid,
                borderWidth: 1,
            },
        },
    };
}

/* ── Render ───────────────────────────────────────────────────── */

/**
 * Render the runtime monitoring view.
 * @param {HTMLElement} container
 * @param {import('../api.js').AdminAPI} api
 * @returns {Promise<function>} Cleanup function
 */
export async function render(container, api) {
    container.replaceChildren();

    const wrapper = document.createElement('div');
    wrapper.className = 'view-enter';

    // ── Page header ──────────────────────────────────────────
    const header = document.createElement('div');
    header.className = 'page-header';
    const headerLeft = document.createElement('div');
    const h1 = document.createElement('h1');
    h1.textContent = 'Runtime';
    headerLeft.appendChild(h1);
    const sub = document.createElement('div');
    sub.className = 'page-subtitle';
    sub.textContent = 'process.monitoring';
    headerLeft.appendChild(sub);
    header.appendChild(headerLeft);
    wrapper.appendChild(header);

    // ── Loading ──────────────────────────────────────────────
    const loader = document.createElement('div');
    loader.appendChild(pageSkeleton({ stats: 4, rows: 5 }));
    wrapper.appendChild(loader);
    container.appendChild(wrapper);

    let data;
    try {
        data = await api.get('/runtime');
    } catch (err) {
        wrapper.removeChild(loader);
        wrapper.appendChild(createEmptyStateCard({
            icon: 'alert',
            tone: 'danger',
            title: 'Failed to load runtime data',
            text: err.message,
        }));
        return;
    }

    wrapper.removeChild(loader);

    const memory = data.memory || {};
    const threads = data.threads || {};
    const gc = data.gc || {};
    const cpu = data.cpu || {};
    const python = data.python || {};

    // ── Stat cards row (grid-4) ──────────────────────────────
    const statsRow = document.createElement('div');
    statsRow.className = 'grid-4 mb-lg';

    const memoryValueEl = createStatCard({
        label: 'Memory RSS',
        value: memory.rss_mb != null ? `${memory.rss_mb.toFixed(1)} MB` : '--',
        iconClass: 'primary',
    });
    statsRow.appendChild(memoryValueEl);

    const threadValueEl = createStatCard({
        label: 'Thread Count',
        value: threads.active != null ? String(threads.active) : '--',
        subtitle: threads.daemon != null ? `${threads.daemon} daemon` : undefined,
        iconClass: 'info',
    });
    statsRow.appendChild(threadValueEl);

    const cpuValueEl = createStatCard({
        label: 'CPU Time',
        value: cpu.process_time_s != null ? `${cpu.process_time_s.toFixed(2)} s` : '--',
        iconClass: 'warning',
    });
    statsRow.appendChild(cpuValueEl);

    const pythonValueEl = createStatCard({
        label: 'Python Version',
        value: python.version || '--',
        iconClass: 'success',
    });
    statsRow.appendChild(pythonValueEl);

    wrapper.appendChild(statsRow);

    // References for updating stat card values from SSE
    const statValueEls = {
        memory: memoryValueEl.querySelector('.stat-card-value'),
        threads: threadValueEl.querySelector('.stat-card-value'),
        cpu: cpuValueEl.querySelector('.stat-card-value'),
        python: pythonValueEl.querySelector('.stat-card-value'),
    };

    // ── Rolling data arrays ──────────────────────────────────
    const labels = [];
    const memoryData = [];
    const threadData = [];
    const gcGen0Data = [];
    const gcGen1Data = [];
    const gcGen2Data = [];
    const cpuData = [];

    // Seed first data point from initial fetch
    const initialLabel = formatTimestamp(data.timestamp);
    labels.push(initialLabel);
    memoryData.push(memory.rss_mb || 0);
    threadData.push(threads.active || 0);
    cpuData.push(cpu.process_time_s || 0);

    const collections = Array.isArray(gc.collections) ? gc.collections : [0, 0, 0];
    gcGen0Data.push(collections[0] || 0);
    gcGen1Data.push(collections[1] || 0);
    gcGen2Data.push(collections[2] || 0);

    // ── Chart cards (grid-2) ─────────────────────────────────
    const chartsRow = document.createElement('div');
    chartsRow.className = 'grid-2 mb-lg';

    const { card: memCard, canvas: memCanvas } = buildChartCard('Memory RSS (MB)');
    chartsRow.appendChild(memCard);

    const { card: threadCard, canvas: threadCanvas } = buildChartCard('Thread Count');
    chartsRow.appendChild(threadCard);

    const { card: gcCard, canvas: gcCanvas } = buildChartCard('GC Collections');
    chartsRow.appendChild(gcCard);

    const { card: cpuCard, canvas: cpuCanvas } = buildChartCard('CPU Time (s)');
    chartsRow.appendChild(cpuCard);

    wrapper.appendChild(chartsRow);

    // ── Initialise charts after canvases are in the DOM ──────
    let memChart = null;
    let threadChart = null;
    let gcChartInstance = null;
    let cpuChart = null;

    requestAnimationFrame(() => {
        memChart = createLineChart(memCanvas, {
            label: 'Memory RSS (MB)',
            color: '--admin-primary',
            data: [...memoryData],
            labels: [...labels],
        });

        threadChart = createLineChart(threadCanvas, {
            label: 'Thread Count',
            color: '--admin-info',
            data: [...threadData],
            labels: [...labels],
        });

        cpuChart = createLineChart(cpuCanvas, {
            label: 'CPU Time (s)',
            color: '--admin-warning',
            data: [...cpuData],
            labels: [...labels],
        });

        // GC chart needs 3 datasets, so create the Chart.js instance directly
        const gen0Color = resolveColor('--admin-primary', '#3b82f6');
        const gen1Color = resolveColor('--admin-success', '#10b981');
        const gen2Color = resolveColor('--admin-warning', '#f59e0b');

        gcChartInstance = new Chart(gcCanvas, {
            type: 'line',
            data: {
                labels: [...labels],
                datasets: [
                    {
                        label: 'Gen 0',
                        data: [...gcGen0Data],
                        borderColor: gen0Color,
                        backgroundColor: hexToRgba(gen0Color, 0.1),
                        fill: true,
                        tension: 0.4,
                        pointRadius: 3,
                        pointBackgroundColor: gen0Color,
                        borderWidth: 2,
                    },
                    {
                        label: 'Gen 1',
                        data: [...gcGen1Data],
                        borderColor: gen1Color,
                        backgroundColor: hexToRgba(gen1Color, 0.1),
                        fill: true,
                        tension: 0.4,
                        pointRadius: 3,
                        pointBackgroundColor: gen1Color,
                        borderWidth: 2,
                    },
                    {
                        label: 'Gen 2',
                        data: [...gcGen2Data],
                        borderColor: gen2Color,
                        backgroundColor: hexToRgba(gen2Color, 0.1),
                        fill: true,
                        tension: 0.4,
                        pointRadius: 3,
                        pointBackgroundColor: gen2Color,
                        borderWidth: 2,
                    },
                ],
            },
            options: chartThemeOptions(),
        });
    });

    // ── SSE real-time updates ────────────────────────────────
    sse.connectTyped('/runtime', 'runtime', (eventData) => {
        const label = formatTimestamp(eventData.timestamp);

        // Update rolling data arrays
        pushRolling(labels, label);
        pushRolling(memoryData, (eventData.memory && eventData.memory.rss_mb) || 0);
        pushRolling(threadData, (eventData.threads && eventData.threads.active) || 0);
        pushRolling(cpuData, (eventData.cpu && eventData.cpu.process_time_s) || 0);

        const cols = (eventData.gc && Array.isArray(eventData.gc.collections))
            ? eventData.gc.collections
            : [0, 0, 0];
        pushRolling(gcGen0Data, cols[0] || 0);
        pushRolling(gcGen1Data, cols[1] || 0);
        pushRolling(gcGen2Data, cols[2] || 0);

        // Update stat cards
        if (eventData.memory && eventData.memory.rss_mb != null) {
            statValueEls.memory.textContent = `${eventData.memory.rss_mb.toFixed(1)} MB`;
        }
        if (eventData.threads && eventData.threads.active != null) {
            statValueEls.threads.textContent = String(eventData.threads.active);
        }
        if (eventData.cpu && eventData.cpu.process_time_s != null) {
            statValueEls.cpu.textContent = `${eventData.cpu.process_time_s.toFixed(2)} s`;
        }

        // Update charts
        if (memChart) {
            memChart.update([...memoryData], [...labels]);
        }
        if (threadChart) {
            threadChart.update([...threadData], [...labels]);
        }
        if (cpuChart) {
            cpuChart.update([...cpuData], [...labels]);
        }
        if (gcChartInstance) {
            gcChartInstance.data.labels = [...labels];
            gcChartInstance.data.datasets[0].data = [...gcGen0Data];
            gcChartInstance.data.datasets[1].data = [...gcGen1Data];
            gcChartInstance.data.datasets[2].data = [...gcGen2Data];
            gcChartInstance.update('none');
        }
    });

    // ── Cleanup ──────────────────────────────────────────────
    return function cleanup() {
        sse.disconnect('/runtime');
        if (memChart) memChart.destroy();
        if (threadChart) threadChart.destroy();
        if (gcChartInstance) gcChartInstance.destroy();
        if (cpuChart) cpuChart.destroy();
    };
}
