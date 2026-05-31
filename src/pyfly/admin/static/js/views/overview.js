/**
 * PyFly Admin — Overview View.
 *
 * Dashboard landing page showing health status, bean statistics,
 * stereotype donut chart, wiring breakdown, app info, and quick links.
 *
 * Data source: GET /admin/api/overview
 */

import { DonutChart, GaugeChart, createLineChart, createGaugeChart, createBarChart } from '../charts.js';
import { createEmptyStateCard } from '../components/empty-state.js';
import { pageSkeleton } from '../components/skeleton.js';
import { createStatusBadge } from '../components/status-badge.js';
import { sse } from '../sse.js';

/* ── Helpers ──────────────────────────────────────────────────── */

/**
 * Format a duration in seconds to a human-readable string.
 * @param {number} seconds
 * @returns {string}  e.g. "2d 5h 13m"
 */
function formatUptime(seconds) {
    if (seconds == null || seconds < 0) return '--';
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    const parts = [];
    if (d > 0) parts.push(`${d}d`);
    if (h > 0 || d > 0) parts.push(`${h}h`);
    parts.push(`${m}m`);
    if (d === 0 && h === 0) parts.push(`${s}s`);
    return parts.join(' ');
}

/**
 * Create a stat card element.
 */
function createStatCard({ label, value, subtitle, iconClass = 'primary', icon = '' }) {
    const card = document.createElement('div');
    card.className = 'stat-card';

    const content = document.createElement('div');
    content.className = 'stat-card-content';

    if (value instanceof Node) {
        content.appendChild(value);
    } else {
        const valEl = document.createElement('div');
        valEl.className = 'stat-card-value';
        valEl.textContent = value != null ? String(value) : '--';
        content.appendChild(valEl);
    }

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

    const iconEl = document.createElement('div');
    iconEl.className = `stat-card-icon ${iconClass}`;
    if (icon) iconEl.innerHTML = icon;
    card.appendChild(iconEl);

    return card;
}

// Feather-style line icons (20×20, inherit `currentColor` from the icon tint).
const STAT_ICONS = {
    health: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>',
    beans: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>',
    uptime: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
    profiles: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>',
};

/**
 * Build a wiring progress bar item.
 */
function buildWiringItem(label, count, maxCount, color) {
    const item = document.createElement('div');
    item.className = 'wiring-item';

    const labelEl = document.createElement('div');
    labelEl.className = 'wiring-item-label';
    labelEl.textContent = label;
    item.appendChild(labelEl);

    const bar = document.createElement('div');
    bar.className = 'wiring-item-bar';
    const fill = document.createElement('div');
    fill.className = 'wiring-item-fill';
    fill.style.width = maxCount > 0 ? `${(count / maxCount) * 100}%` : '0%';
    fill.style.background = `var(${color})`;
    bar.appendChild(fill);
    item.appendChild(bar);

    const countEl = document.createElement('div');
    countEl.className = 'wiring-item-count';
    countEl.textContent = String(count);
    item.appendChild(countEl);

    return item;
}

/* ── Quick-link definitions ───────────────────────────────────── */

const QUICK_LINKS = [
    { label: 'Beans',       route: 'beans',       description: 'Explore registered components' },
    { label: 'Health',      route: 'health',      description: 'Service health checks' },
    { label: 'Loggers',     route: 'loggers',     description: 'Runtime log-level control' },
    { label: 'Metrics',     route: 'metrics',     description: 'Application metrics' },
    { label: 'Environment', route: 'env',         description: 'Profiles & properties' },
    { label: 'Traces',      route: 'traces',      description: 'Request traces' },
];

/* ── Render ───────────────────────────────────────────────────── */

/**
 * Render the overview dashboard.
 * @param {HTMLElement} container
 * @param {import('../api.js').AdminAPI} api
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
    h1.textContent = 'Overview';
    headerLeft.appendChild(h1);
    const sub = document.createElement('div');
    sub.className = 'page-subtitle';
    sub.textContent = 'application.dashboard';
    headerLeft.appendChild(sub);
    header.appendChild(headerLeft);
    wrapper.appendChild(header);

    // Loading skeleton (4 stat cards + charts/content)
    const loader = document.createElement('div');
    loader.appendChild(pageSkeleton({ stats: 4, rows: 5 }));
    wrapper.appendChild(loader);
    container.appendChild(wrapper);

    let data;
    try {
        data = await api.get('/overview');
    } catch (err) {
        wrapper.removeChild(loader);
        wrapper.appendChild(createEmptyStateCard({
            icon: 'alert',
            tone: 'danger',
            title: 'Failed to load overview',
            text: err.message,
        }));
        return;
    }

    wrapper.removeChild(loader);

    const app = data.app || {};
    const health = data.health || {};
    const beans = data.beans || {};
    const wiring = data.wiring || {};

    // ── Status cards row (grid-4) ─────────────────────────────
    const statsRow = document.createElement('div');
    statsRow.className = 'grid-4 mb-lg';

    // 1) Health status
    const healthBadge = createStatusBadge(health.status || 'UNKNOWN');
    statsRow.appendChild(
        createStatCard({ label: 'Health Status', value: healthBadge, iconClass: 'success', icon: STAT_ICONS.health }),
    );

    // 2) Total beans
    statsRow.appendChild(createStatCard({
        label: 'Total Beans',
        value: String(beans.total != null ? beans.total : 0),
        iconClass: 'primary',
        icon: STAT_ICONS.beans,
    }));

    // 3) Uptime
    statsRow.appendChild(createStatCard({
        label: 'Uptime',
        value: formatUptime(app.uptime_seconds),
        subtitle: `Port ${app.web_port || 8080}`,
        iconClass: 'info',
        icon: STAT_ICONS.uptime,
    }));

    // 4) Active profiles
    const profiles = app.profiles || [];
    statsRow.appendChild(createStatCard({
        label: 'Active Profiles',
        value: profiles.length > 0 ? profiles.join(', ') : 'default',
        subtitle: `Python ${app.python_version || ''}`,
        iconClass: 'warning',
        icon: STAT_ICONS.profiles,
    }));

    wrapper.appendChild(statsRow);

    // ── Two-column layout: donut chart + wiring ──────────────
    const midRow = document.createElement('div');
    midRow.className = 'grid-2 mb-lg';

    // Stereotype donut chart
    const stereotypes = beans.stereotypes || {};
    const chartCard = document.createElement('div');
    chartCard.className = 'admin-card';

    const chartHeader = document.createElement('div');
    chartHeader.className = 'admin-card-header';
    const chartTitle = document.createElement('h3');
    chartTitle.textContent = 'Beans by Stereotype';
    chartHeader.appendChild(chartTitle);
    const chartSubtitle = document.createElement('span');
    chartSubtitle.className = 'card-subtitle';
    chartSubtitle.textContent = `${beans.total || 0} total`;
    chartHeader.appendChild(chartSubtitle);
    chartCard.appendChild(chartHeader);

    const chartBody = document.createElement('div');
    chartBody.className = 'admin-card-body';

    const stereotypeLabels = Object.keys(stereotypes);
    const stereotypeValues = Object.values(stereotypes);

    if (stereotypeLabels.length > 0) {
        const chartRow = document.createElement('div');
        chartRow.style.display = 'flex';
        chartRow.style.alignItems = 'center';
        chartRow.style.gap = '24px';

        const chartContainer = document.createElement('div');
        chartContainer.style.flexShrink = '0';
        const canvas = document.createElement('canvas');
        chartContainer.appendChild(canvas);
        chartRow.appendChild(chartContainer);

        // Legend
        const legend = document.createElement('div');
        legend.className = 'donut-legend';
        legend.style.flex = '1';

        chartRow.appendChild(legend);
        chartBody.appendChild(chartRow);
        chartCard.appendChild(chartBody);
        midRow.appendChild(chartCard);

        requestAnimationFrame(() => {
            const donut = new DonutChart(canvas, {
                data: stereotypeValues,
                labels: stereotypeLabels,
                size: 170,
                centerValue: String(beans.total || 0),
                centerLabel: 'BEANS',
            });

            // Build legend with resolved colours
            const colors = donut.getColors();
            for (let i = 0; i < stereotypeLabels.length; i++) {
                const item = document.createElement('div');
                item.className = 'donut-legend-item';

                const dot = document.createElement('div');
                dot.className = 'donut-legend-dot';
                dot.style.background = colors[i];
                item.appendChild(dot);

                const label = document.createElement('span');
                label.className = 'donut-legend-label';
                label.textContent = stereotypeLabels[i];
                item.appendChild(label);

                const val = document.createElement('span');
                val.className = 'donut-legend-value';
                val.textContent = String(stereotypeValues[i]);
                item.appendChild(val);

                legend.appendChild(item);
            }
        });
    } else {
        const noData = document.createElement('div');
        noData.className = 'text-muted text-sm';
        noData.textContent = 'No stereotype data available';
        chartBody.appendChild(noData);
        chartCard.appendChild(chartBody);
        midRow.appendChild(chartCard);
    }

    // Wiring summary card with progress bars
    const wiringCard = document.createElement('div');
    wiringCard.className = 'admin-card';

    const wiringHeader = document.createElement('div');
    wiringHeader.className = 'admin-card-header';
    const wiringTitle = document.createElement('h3');
    wiringTitle.textContent = 'Wiring & Configuration';
    wiringHeader.appendChild(wiringTitle);
    wiringCard.appendChild(wiringHeader);

    const wiringBody = document.createElement('div');
    wiringBody.className = 'admin-card-body';

    // Wiring progress bars
    const wiringItems = [
        { label: 'Event Listeners', key: 'event_listeners', color: '--admin-primary' },
        { label: 'Message Listeners', key: 'message_listeners', color: '--admin-info' },
        { label: 'CQRS Handlers', key: 'cqrs_handlers', color: '--admin-success' },
        { label: 'Scheduled Tasks', key: 'scheduled', color: '--admin-warning' },
        { label: 'Async Methods', key: 'async_methods', color: '--admin-danger' },
        { label: 'Post Processors', key: 'post_processors', color: '--admin-text-muted' },
    ];

    const maxWiring = Math.max(1, ...wiringItems.map((w) => wiring[w.key] || 0));
    const wiringList = document.createElement('div');
    for (const w of wiringItems) {
        const count = wiring[w.key] || 0;
        wiringList.appendChild(buildWiringItem(w.label, count, maxWiring, w.color));
    }
    wiringBody.appendChild(wiringList);

    // App info section
    const appInfoSection = document.createElement('div');
    appInfoSection.style.marginTop = '20px';
    appInfoSection.style.paddingTop = '16px';
    appInfoSection.style.borderTop = '1px solid var(--admin-border-subtle)';

    const appMeta = [];
    if (app.name) appMeta.push(['Application', app.name]);
    if (app.version) appMeta.push(['Version', app.version]);
    if (app.framework_version) appMeta.push(['Framework', `PyFly ${app.framework_version}`]);
    if (app.python_version) appMeta.push(['Python', app.python_version]);
    if (app.platform) appMeta.push(['Platform', app.platform]);
    if (app.description) appMeta.push(['Description', app.description]);

    const metaTable = document.createElement('table');
    metaTable.className = 'kv-table';
    const metaTbody = document.createElement('tbody');
    for (const [key, val] of appMeta) {
        const tr = document.createElement('tr');
        const th = document.createElement('th');
        th.textContent = key;
        const td = document.createElement('td');
        td.textContent = val;
        tr.appendChild(th);
        tr.appendChild(td);
        metaTbody.appendChild(tr);
    }
    metaTable.appendChild(metaTbody);
    appInfoSection.appendChild(metaTable);
    wiringBody.appendChild(appInfoSection);

    wiringCard.appendChild(wiringBody);
    midRow.appendChild(wiringCard);

    wrapper.appendChild(midRow);

    // ── Runtime monitoring row (grid-3) ──────────────────────
    const MAX_DATA_POINTS = 60;
    const memoryData = [];
    const memoryLabels = [];
    let memChart = null;
    let threadGauge = null;
    let gcChart = null;

    const runtimeRow = document.createElement('div');
    runtimeRow.className = 'grid-3 mb-lg';

    // 1) Memory Line Chart card
    const memCard = document.createElement('div');
    memCard.className = 'admin-card';
    const memHeader = document.createElement('div');
    memHeader.className = 'admin-card-header';
    const memTitle = document.createElement('h3');
    memTitle.textContent = 'Memory RSS (MB)';
    memHeader.appendChild(memTitle);
    memCard.appendChild(memHeader);
    const memBody = document.createElement('div');
    memBody.className = 'admin-card-body';
    memBody.style.height = '200px';
    const memCanvas = document.createElement('canvas');
    memBody.appendChild(memCanvas);
    memCard.appendChild(memBody);
    runtimeRow.appendChild(memCard);

    // 2) Thread Count Gauge card
    const threadCard = document.createElement('div');
    threadCard.className = 'admin-card';
    const threadHeader = document.createElement('div');
    threadHeader.className = 'admin-card-header';
    const threadTitle = document.createElement('h3');
    threadTitle.textContent = 'Thread Count';
    threadHeader.appendChild(threadTitle);
    threadCard.appendChild(threadHeader);
    const threadBody = document.createElement('div');
    threadBody.className = 'admin-card-body';
    threadBody.style.display = 'flex';
    threadBody.style.justifyContent = 'center';
    threadBody.style.alignItems = 'center';
    const threadCanvas = document.createElement('canvas');
    threadCanvas.width = 180;
    threadCanvas.height = 180;
    threadBody.appendChild(threadCanvas);
    threadCard.appendChild(threadBody);
    runtimeRow.appendChild(threadCard);

    // 3) GC Collections Bar Chart card
    const gcCard = document.createElement('div');
    gcCard.className = 'admin-card';
    const gcHeader = document.createElement('div');
    gcHeader.className = 'admin-card-header';
    const gcTitle = document.createElement('h3');
    gcTitle.textContent = 'GC Collections';
    gcHeader.appendChild(gcTitle);
    gcCard.appendChild(gcHeader);
    const gcBody = document.createElement('div');
    gcBody.className = 'admin-card-body';
    gcBody.style.height = '200px';
    const gcCanvas = document.createElement('canvas');
    gcBody.appendChild(gcCanvas);
    gcCard.appendChild(gcBody);
    runtimeRow.appendChild(gcCard);

    wrapper.appendChild(runtimeRow);

    // Initialise charts after the canvases are in the DOM
    requestAnimationFrame(() => {
        memChart = createLineChart(memCanvas, {
            label: 'Memory RSS (MB)',
            color: '--admin-primary',
        });

        threadGauge = createGaugeChart(threadCanvas, {
            value: 0,
            label: 'Threads',
            thresholds: { warning: 60, danger: 80 },
        });

        gcChart = createBarChart(gcCanvas, {
            labels: ['Gen 0', 'Gen 1', 'Gen 2'],
            data: [0, 0, 0],
            colors: ['--admin-primary', '--admin-success', '--admin-warning'],
        });
    });

    // Connect SSE for real-time runtime data
    sse.connectTyped('/runtime', 'runtime', (data) => {
        // Memory line chart — rolling window
        const rss = data.memory && data.memory.rss_mb;
        if (rss != null) {
            const ts = data.timestamp
                ? new Date(data.timestamp).toLocaleTimeString()
                : new Date().toLocaleTimeString();
            memoryData.push(rss);
            memoryLabels.push(ts);
            if (memoryData.length > MAX_DATA_POINTS) {
                memoryData.shift();
                memoryLabels.shift();
            }
            if (memChart) {
                memChart.update([...memoryData], [...memoryLabels]);
            }
        }

        // Thread gauge
        const threads = data.threads && data.threads.active;
        if (threads != null && threadGauge) {
            threadGauge.update(threads);
        }

        // GC bar chart
        const collections = data.gc && data.gc.collections;
        if (Array.isArray(collections) && gcChart) {
            gcChart.update([...collections]);
        }
    });

    // ── Quick links ───────────────────────────────────────────
    const linksSection = document.createElement('div');
    linksSection.className = 'mb-lg';

    const linksHeader = document.createElement('h3');
    linksHeader.style.marginBottom = '12px';
    linksHeader.style.fontSize = '0.95rem';
    linksHeader.style.fontWeight = '600';
    linksHeader.textContent = 'Quick Links';
    linksSection.appendChild(linksHeader);

    const linksGrid = document.createElement('div');
    linksGrid.className = 'grid-3';

    for (const link of QUICK_LINKS) {
        const card = document.createElement('div');
        card.className = 'admin-card';
        card.style.cursor = 'pointer';
        card.addEventListener('click', () => {
            window.location.hash = link.route;
        });

        const body = document.createElement('div');
        body.className = 'admin-card-body';

        const name = document.createElement('div');
        name.style.fontWeight = '600';
        name.style.marginBottom = '4px';
        name.textContent = link.label;
        body.appendChild(name);

        const desc = document.createElement('div');
        desc.className = 'text-muted text-sm';
        desc.textContent = link.description;
        body.appendChild(desc);

        card.appendChild(body);
        linksGrid.appendChild(card);
    }

    linksSection.appendChild(linksGrid);
    wrapper.appendChild(linksSection);

    // Return cleanup function to disconnect SSE and destroy charts
    return function cleanup() {
        sse.disconnect('/runtime');
        if (memChart) memChart.destroy();
        if (threadGauge) threadGauge.destroy();
        if (gcChart) gcChart.destroy();
    };
}
