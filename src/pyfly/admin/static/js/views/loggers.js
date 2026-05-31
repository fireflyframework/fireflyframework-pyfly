/**
 * PyFly Admin — Loggers View.
 *
 * Displays application loggers with runtime level management.
 * Supports searching/filtering by name and changing log levels
 * with instant POST feedback and toast notifications.
 *
 * Data source:  GET  /admin/api/loggers
 *   -> { loggers: { "ROOT": { configuredLevel, effectiveLevel }, ... }, levels: [...] }
 * Action:       POST /admin/api/loggers/{name}  body: { level: "DEBUG" }
 */

import { createEmptyStateCard } from '../components/empty-state.js';
import { createFilterToolbar } from '../components/filter-toolbar.js';
import { pageSkeleton } from '../components/skeleton.js';
import { showToast } from '../components/toast.js';

/* ── Helpers ──────────────────────────────────────────────────── */

/** Map log level names to CSS colour values. */
const LEVEL_COLORS = {
    TRACE:    'var(--admin-text-muted)',
    DEBUG:    'var(--admin-info)',
    INFO:     'var(--admin-primary)',
    WARNING:  'var(--admin-warning)',
    WARN:     'var(--admin-warning)',
    ERROR:    'var(--admin-danger)',
    CRITICAL: 'var(--admin-danger)',
    FATAL:    'var(--admin-danger)',
};

/**
 * Create a styled level label element.
 * @param {string} level
 * @returns {HTMLSpanElement}
 */
function createLevelLabel(level) {
    const span = document.createElement('span');
    span.className = 'text-mono';
    span.style.fontSize = '0.8rem';
    span.style.fontWeight = '600';
    span.textContent = level || '--';

    const color = LEVEL_COLORS[(level || '').toUpperCase()] || 'var(--admin-text-muted)';
    span.style.color = color;

    if ((level || '').toUpperCase() === 'CRITICAL' || (level || '').toUpperCase() === 'FATAL') {
        span.style.fontWeight = '700';
    }

    return span;
}

/* ── Render ───────────────────────────────────────────────────── */

/**
 * Render the loggers view.
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
    h1.textContent = 'Loggers';
    headerLeft.appendChild(h1);
    const sub = document.createElement('div');
    sub.className = 'page-subtitle';
    sub.textContent = 'application.loggers';
    headerLeft.appendChild(sub);
    header.appendChild(headerLeft);
    wrapper.appendChild(header);

    // Loading skeleton (two stat cards + a table)
    const loader = document.createElement('div');
    loader.appendChild(pageSkeleton({ stats: 2, rows: 8 }));
    wrapper.appendChild(loader);
    container.appendChild(wrapper);

    // Fetch loggers data
    let loggersData;
    try {
        loggersData = await api.get('/loggers');
    } catch (err) {
        wrapper.removeChild(loader);
        wrapper.appendChild(createEmptyStateCard({
            icon: 'alert',
            tone: 'danger',
            title: 'Failed to load loggers',
            text: err.message,
        }));
        return;
    }

    wrapper.removeChild(loader);

    const loggersMap = loggersData.loggers || {};
    const levels = loggersData.levels || ['TRACE', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];

    // Convert loggers map to array for the table
    const loggerEntries = Object.entries(loggersMap).map(([name, info]) => ({
        name,
        configuredLevel: info.configuredLevel || info.configured_level || '--',
        effectiveLevel: info.effectiveLevel || info.effective_level || '--',
        description: info.description || '',
    }));

    // ── Filter toolbar ───────────────────────────────────────
    const toolbar = createFilterToolbar({
        placeholder: 'Filter loggers by name...',
        pills: [
            { label: 'All', value: '' },
            { label: 'DEBUG', value: 'DEBUG' },
            { label: 'INFO', value: 'INFO' },
            { label: 'WARNING', value: 'WARNING' },
            { label: 'ERROR', value: 'ERROR' },
            { label: 'CRITICAL', value: 'CRITICAL' },
        ],
        onFilter: ({ search, pill }) => {
            renderTableBody(search, pill);
        },
        totalCount: loggerEntries.length,
    });
    wrapper.appendChild(toolbar);

    // ── Stat cards ───────────────────────────────────────────
    const statsRow = document.createElement('div');
    statsRow.className = 'grid-3 mb-lg';

    const totalCard = document.createElement('div');
    totalCard.className = 'stat-card';
    const totalContent = document.createElement('div');
    totalContent.className = 'stat-card-content';
    const totalVal = document.createElement('div');
    totalVal.className = 'stat-card-value';
    totalVal.textContent = String(loggerEntries.length);
    totalContent.appendChild(totalVal);
    const totalLabel = document.createElement('div');
    totalLabel.className = 'stat-card-label';
    totalLabel.textContent = 'Total Loggers';
    totalContent.appendChild(totalLabel);
    totalCard.appendChild(totalContent);
    statsRow.appendChild(totalCard);

    // Count by effective level
    const levelCounts = {};
    for (const entry of loggerEntries) {
        const lvl = entry.effectiveLevel.toUpperCase();
        levelCounts[lvl] = (levelCounts[lvl] || 0) + 1;
    }
    const topLevels = Object.entries(levelCounts)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 2);

    for (const [lvl, count] of topLevels) {
        const card = document.createElement('div');
        card.className = 'stat-card';
        const cardContent = document.createElement('div');
        cardContent.className = 'stat-card-content';
        const cardVal = document.createElement('div');
        cardVal.className = 'stat-card-value';
        cardVal.textContent = String(count);
        cardContent.appendChild(cardVal);
        const cardLabel = document.createElement('div');
        cardLabel.className = 'stat-card-label';
        cardLabel.textContent = lvl + ' Level';
        cardContent.appendChild(cardLabel);
        card.appendChild(cardContent);
        statsRow.appendChild(card);
    }

    wrapper.appendChild(statsRow);

    // ── Effective level distribution ─────────────────────────
    const LEVEL_ORDER = ['TRACE', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];
    if (loggerEntries.length > 0) {
        const distCard = document.createElement('div');
        distCard.className = 'admin-card mb-lg';
        const distHeader = document.createElement('div');
        distHeader.className = 'admin-card-header';
        const distTitle = document.createElement('h3');
        distTitle.textContent = 'Effective Level Distribution';
        distHeader.appendChild(distTitle);
        distCard.appendChild(distHeader);
        const distBody = document.createElement('div');
        distBody.className = 'admin-card-body';

        const bar = document.createElement('div');
        bar.className = 'status-bar';
        bar.setAttribute('aria-hidden', 'true');
        const legend = document.createElement('div');
        legend.className = 'status-legend';

        const total = loggerEntries.length;
        // Include any non-standard effective levels (e.g. NOTSET) so the bar
        // segments and percentages sum to 100% rather than silently dropping them.
        const extraLevels = Object.keys(levelCounts).filter((l) => !LEVEL_ORDER.includes(l));
        for (const lvl of [...LEVEL_ORDER, ...extraLevels]) {
            const count = levelCounts[lvl] || 0;
            if (count === 0) continue;
            const pct = (count / total) * 100;
            const color = LEVEL_COLORS[lvl] || 'var(--admin-text-muted)';

            const seg = document.createElement('div');
            seg.className = 'status-bar-seg';
            seg.style.background = color;
            seg.style.width = pct + '%';
            seg.title = `${lvl}: ${count}`;
            bar.appendChild(seg);

            const item = document.createElement('div');
            item.className = 'status-legend-item';
            const dot = document.createElement('span');
            dot.className = 'status-legend-dot';
            dot.style.background = color;
            item.appendChild(dot);
            const lab = document.createElement('span');
            lab.textContent = lvl;
            item.appendChild(lab);
            const c = document.createElement('span');
            c.className = 'status-legend-count';
            c.textContent = String(count);
            item.appendChild(c);
            const p = document.createElement('span');
            p.className = 'status-legend-pct';
            p.textContent = `(${pct.toFixed(0)}%)`;
            item.appendChild(p);
            legend.appendChild(item);
        }
        distBody.appendChild(bar);
        distBody.appendChild(legend);
        distCard.appendChild(distBody);
        wrapper.appendChild(distCard);
    }

    // ── Loggers table card ───────────────────────────────────
    const tableCard = document.createElement('div');
    tableCard.className = 'admin-card';

    const tableCardHeader = document.createElement('div');
    tableCardHeader.className = 'admin-card-header';
    const tableTitle = document.createElement('h3');
    tableTitle.textContent = 'Loggers';
    tableCardHeader.appendChild(tableTitle);
    tableCard.appendChild(tableCardHeader);

    const tableBody = document.createElement('div');
    tableBody.className = 'admin-card-body';
    tableBody.style.padding = '12px 20px 0';

    // Table container — scrolls within a viewport-relative height so the
    // toolbar, stats and distribution stay visible (sticky header).
    const tableWrap = document.createElement('div');
    tableWrap.className = 'admin-table-wrapper';
    tableWrap.style.maxHeight = 'min(58vh, 620px)';
    tableWrap.style.overflowY = 'auto';

    const table = document.createElement('table');
    table.className = 'admin-table';

    // Table head
    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    const headers = ['Name', 'Description', 'Configured Level', 'Effective Level', 'Actions'];
    for (const label of headers) {
        const th = document.createElement('th');
        th.textContent = label;
        headRow.appendChild(th);
    }
    thead.appendChild(headRow);
    table.appendChild(thead);

    // Table body
    const tbody = document.createElement('tbody');
    table.appendChild(tbody);

    /**
     * Render the table body based on the current search text and pill filter.
     * @param {string} search  Lowercase search string for logger name.
     * @param {string} pill    Level pill value (e.g. "DEBUG") or "" for all.
     */
    function renderTableBody(search, pill) {
        tbody.replaceChildren();

        const filtered = loggerEntries.filter((e) => {
            const matchesSearch = !search || e.name.toLowerCase().includes(search);
            const matchesPill = !pill || e.effectiveLevel.toUpperCase() === pill.toUpperCase();
            return matchesSearch && matchesPill;
        });

        toolbar.updateCount(filtered.length, loggerEntries.length);

        if (filtered.length === 0) {
            const tr = document.createElement('tr');
            const td = document.createElement('td');
            td.colSpan = 5;
            td.style.textAlign = 'center';
            td.style.padding = '32px 16px';
            td.style.color = 'var(--admin-text-muted)';
            td.textContent = (search || pill) ? 'No loggers match the filter' : 'No loggers found';
            tr.appendChild(td);
            tbody.appendChild(tr);
            return;
        }

        for (const entry of filtered) {
            const tr = document.createElement('tr');

            // Name column
            const tdName = document.createElement('td');
            const nameSpan = document.createElement('span');
            nameSpan.className = 'mono';
            nameSpan.textContent = entry.name;
            tdName.appendChild(nameSpan);
            tr.appendChild(tdName);

            // Description column
            const tdDesc = document.createElement('td');
            tdDesc.className = 'text-muted text-sm';
            tdDesc.textContent = entry.description || '';
            tr.appendChild(tdDesc);

            // Configured Level column
            const tdConfigured = document.createElement('td');
            tdConfigured.appendChild(createLevelLabel(entry.configuredLevel));
            tr.appendChild(tdConfigured);

            // Effective Level column
            const tdEffective = document.createElement('td');
            tdEffective.appendChild(createLevelLabel(entry.effectiveLevel));
            tr.appendChild(tdEffective);

            // Actions column — level change dropdown + reset button
            const tdActions = document.createElement('td');
            tdActions.style.display = 'flex';
            tdActions.style.alignItems = 'center';
            tdActions.style.gap = '6px';

            const select = document.createElement('select');
            select.className = 'select';
            select.style.width = 'auto';
            select.style.minWidth = '120px';
            select.style.padding = '4px 28px 4px 8px';
            select.style.fontSize = '0.8rem';

            for (const lvl of levels) {
                const option = document.createElement('option');
                option.value = lvl;
                option.textContent = lvl;
                if (lvl.toUpperCase() === entry.configuredLevel.toUpperCase()) {
                    option.selected = true;
                }
                select.appendChild(option);
            }

            /** Re-fetch logger state to verify a level change took effect. */
            async function refreshEntry() {
                try {
                    const fresh = await api.get('/loggers');
                    const info = (fresh.loggers || {})[entry.name];
                    if (info) {
                        entry.configuredLevel = info.configuredLevel || info.configured_level || '--';
                        entry.effectiveLevel = info.effectiveLevel || info.effective_level || '--';
                        tdConfigured.replaceChildren();
                        tdConfigured.appendChild(createLevelLabel(entry.configuredLevel));
                        tdEffective.replaceChildren();
                        tdEffective.appendChild(createLevelLabel(entry.effectiveLevel));
                    }
                } catch (_) {
                    // ignore refresh failures
                }
            }

            select.addEventListener('change', async () => {
                const newLevel = select.value;
                try {
                    await api.post(
                        '/loggers/' + encodeURIComponent(entry.name),
                        { level: newLevel }
                    );
                    showToast(
                        'Logger "' + entry.name + '" set to ' + newLevel,
                        'success'
                    );
                    await refreshEntry();
                } catch (err) {
                    showToast(
                        'Failed to update logger: ' + err.message,
                        'error'
                    );
                    select.value = entry.configuredLevel;
                }
            });

            tdActions.appendChild(select);

            // Reset button (sets to NOTSET — inherits from parent)
            if (entry.name !== 'ROOT') {
                const resetBtn = document.createElement('button');
                resetBtn.className = 'btn btn-sm';
                resetBtn.textContent = 'Reset';
                resetBtn.title = 'Reset to NOTSET (inherit from parent)';
                resetBtn.addEventListener('click', async () => {
                    try {
                        await api.post(
                            '/loggers/' + encodeURIComponent(entry.name),
                            { level: 'NOTSET' }
                        );
                        showToast(
                            'Logger "' + entry.name + '" reset to NOTSET',
                            'success'
                        );
                        await refreshEntry();
                        select.value = entry.configuredLevel;
                    } catch (err) {
                        showToast(
                            'Failed to reset logger: ' + err.message,
                            'error'
                        );
                    }
                });
                tdActions.appendChild(resetBtn);
            }

            tr.appendChild(tdActions);
            tbody.appendChild(tr);
        }
    }

    // Initial render
    renderTableBody('', '');

    tableWrap.appendChild(table);
    tableBody.appendChild(tableWrap);
    tableCard.appendChild(tableBody);
    wrapper.appendChild(tableCard);
}
