/**
 * PyFly Admin — Transactions View.
 *
 * Displays registered saga definitions and TCC transactions
 * with step/participant detail, dependency visualization,
 * and phase coverage indicators.
 *
 * Data source:
 *   GET /admin/api/transactions -> { sagas, tcc, saga_count, tcc_count, total, in_flight }
 */

import { createEmptyStateCard } from '../components/empty-state.js';
import { pageSkeleton } from '../components/skeleton.js';

/* ── Helpers ──────────────────────────────────────────────────── */

/**
 * Create a stat card element.
 * @param {string|number} value
 * @param {string} label
 * @returns {HTMLDivElement}
 */
function createStatCard(value, label) {
    const card = document.createElement('div');
    card.className = 'stat-card';
    const content = document.createElement('div');
    content.className = 'stat-card-content';
    const valEl = document.createElement('div');
    valEl.className = 'stat-card-value';
    valEl.textContent = String(value);
    content.appendChild(valEl);
    const labelEl = document.createElement('div');
    labelEl.className = 'stat-card-label';
    labelEl.textContent = label;
    content.appendChild(labelEl);
    card.appendChild(content);
    return card;
}

/**
 * Create a phase indicator (colored dot + label).
 * @param {string} label
 * @param {boolean} present
 * @returns {HTMLSpanElement}
 */
function createPhaseIndicator(label, present) {
    const span = document.createElement('span');
    span.style.display = 'inline-flex';
    span.style.alignItems = 'center';
    span.style.gap = '4px';
    span.style.marginRight = '12px';
    span.style.fontSize = '0.8rem';

    const dot = document.createElement('span');
    dot.style.width = '8px';
    dot.style.height = '8px';
    dot.style.borderRadius = '50%';
    dot.style.display = 'inline-block';
    dot.style.backgroundColor = present
        ? 'var(--admin-success)'
        : 'var(--admin-text-muted)';
    dot.style.opacity = present ? '1' : '0.3';
    span.appendChild(dot);

    const text = document.createElement('span');
    text.textContent = label;
    text.style.color = present
        ? 'var(--admin-text-primary)'
        : 'var(--admin-text-muted)';
    span.appendChild(text);

    return span;
}

/**
 * Create a badge element.
 * @param {string} text
 * @param {string} cls  CSS badge class
 * @returns {HTMLSpanElement}
 */
function createBadge(text, cls) {
    const badge = document.createElement('span');
    badge.className = `badge ${cls}`;
    badge.textContent = text;
    return badge;
}

/**
 * Format milliseconds for display.
 * @param {number} ms
 * @returns {string}
 */
function formatMs(ms) {
    if (!ms || ms === 0) return 'none';
    if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
    return `${ms}ms`;
}

/* ── Saga Section ─────────────────────────────────────────────── */

/**
 * Render a single saga definition card.
 * @param {object} saga
 * @returns {HTMLDivElement}
 */
function renderSagaCard(saga) {
    const card = document.createElement('div');
    card.className = 'admin-card';

    // Header
    const header = document.createElement('div');
    header.className = 'admin-card-header';
    header.style.cursor = 'pointer';

    const headerLeft = document.createElement('div');
    headerLeft.style.display = 'flex';
    headerLeft.style.alignItems = 'center';
    headerLeft.style.gap = '8px';

    const chevron = document.createElement('span');
    chevron.textContent = '▸';
    chevron.style.transition = 'transform 0.2s';
    chevron.style.display = 'inline-block';
    headerLeft.appendChild(chevron);

    const title = document.createElement('h3');
    title.style.margin = '0';
    title.textContent = saga.name;
    headerLeft.appendChild(title);

    const stepBadge = createBadge(`${saga.step_count} steps`, 'badge-info');
    headerLeft.appendChild(stepBadge);

    if (saga.layer_concurrency > 0) {
        const concBadge = createBadge(`concurrency: ${saga.layer_concurrency}`, 'badge-neutral');
        headerLeft.appendChild(concBadge);
    }

    header.appendChild(headerLeft);

    const headerRight = document.createElement('div');
    const typeMono = document.createElement('span');
    typeMono.className = 'mono';
    typeMono.style.fontSize = '0.75rem';
    typeMono.style.color = 'var(--admin-text-muted)';
    typeMono.textContent = saga.type || '';
    headerRight.appendChild(typeMono);
    header.appendChild(headerRight);

    card.appendChild(header);

    // Collapsible body
    const body = document.createElement('div');
    body.className = 'admin-card-body';
    body.style.display = 'none';
    body.style.padding = '0';

    // Steps table
    const table = document.createElement('table');
    table.className = 'admin-table';

    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    for (const col of ['Step ID', 'Dependencies', 'Compensation', 'Retry', 'Timeout', 'CPU Bound']) {
        const th = document.createElement('th');
        th.textContent = col;
        headRow.appendChild(th);
    }
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    for (const step of saga.steps || []) {
        const row = document.createElement('tr');

        // Step ID
        const tdId = document.createElement('td');
        const idSpan = document.createElement('span');
        idSpan.className = 'mono';
        idSpan.style.fontWeight = '500';
        idSpan.textContent = step.id;
        tdId.appendChild(idSpan);
        row.appendChild(tdId);

        // Dependencies
        const tdDeps = document.createElement('td');
        if (step.depends_on && step.depends_on.length > 0) {
            for (const dep of step.depends_on) {
                const depBadge = createBadge(dep, 'badge-neutral');
                depBadge.style.marginRight = '4px';
                tdDeps.appendChild(depBadge);
            }
        } else {
            const noDep = document.createElement('span');
            noDep.className = 'text-muted';
            noDep.textContent = 'root';
            tdDeps.appendChild(noDep);
        }
        row.appendChild(tdDeps);

        // Compensation
        const tdComp = document.createElement('td');
        tdComp.appendChild(
            createBadge(
                step.has_compensation ? 'Yes' : 'No',
                step.has_compensation ? 'badge-success' : 'badge-warning'
            )
        );
        row.appendChild(tdComp);

        // Retry
        const tdRetry = document.createElement('td');
        const retrySpan = document.createElement('span');
        retrySpan.className = 'mono';
        retrySpan.textContent = step.retry > 0 ? `${step.retry}x` : '--';
        tdRetry.appendChild(retrySpan);
        row.appendChild(tdRetry);

        // Timeout
        const tdTimeout = document.createElement('td');
        const toSpan = document.createElement('span');
        toSpan.className = 'mono';
        toSpan.textContent = formatMs(step.timeout_ms);
        tdTimeout.appendChild(toSpan);
        row.appendChild(tdTimeout);

        // CPU Bound
        const tdCpu = document.createElement('td');
        if (step.cpu_bound) {
            tdCpu.appendChild(createBadge('CPU', 'badge-warning'));
        } else {
            const dash = document.createElement('span');
            dash.className = 'text-muted';
            dash.textContent = '--';
            tdCpu.appendChild(dash);
        }
        row.appendChild(tdCpu);

        tbody.appendChild(row);
    }
    table.appendChild(tbody);
    body.appendChild(table);
    card.appendChild(body);

    // Toggle collapse
    let expanded = false;
    header.addEventListener('click', () => {
        expanded = !expanded;
        body.style.display = expanded ? 'block' : 'none';
        chevron.style.transform = expanded ? 'rotate(90deg)' : 'rotate(0deg)';
    });

    return card;
}

/* ── TCC Section ──────────────────────────────────────────────── */

/**
 * Render a single TCC definition card.
 * @param {object} tcc
 * @returns {HTMLDivElement}
 */
function renderTccCard(tcc) {
    const card = document.createElement('div');
    card.className = 'admin-card';

    // Header
    const header = document.createElement('div');
    header.className = 'admin-card-header';
    header.style.cursor = 'pointer';

    const headerLeft = document.createElement('div');
    headerLeft.style.display = 'flex';
    headerLeft.style.alignItems = 'center';
    headerLeft.style.gap = '8px';

    const chevron = document.createElement('span');
    chevron.textContent = '▸';
    chevron.style.transition = 'transform 0.2s';
    chevron.style.display = 'inline-block';
    headerLeft.appendChild(chevron);

    const title = document.createElement('h3');
    title.style.margin = '0';
    title.textContent = tcc.name;
    headerLeft.appendChild(title);

    const partBadge = createBadge(
        `${tcc.participant_count} participant${tcc.participant_count !== 1 ? 's' : ''}`,
        'badge-info'
    );
    headerLeft.appendChild(partBadge);

    if (tcc.retry_enabled) {
        const retryBadge = createBadge(`retry: ${tcc.max_retries}x`, 'badge-neutral');
        headerLeft.appendChild(retryBadge);
    }

    header.appendChild(headerLeft);

    const headerRight = document.createElement('div');
    headerRight.style.display = 'flex';
    headerRight.style.alignItems = 'center';
    headerRight.style.gap = '8px';

    if (tcc.timeout_ms > 0) {
        const toLabel = document.createElement('span');
        toLabel.className = 'mono';
        toLabel.style.fontSize = '0.75rem';
        toLabel.style.color = 'var(--admin-text-muted)';
        toLabel.textContent = `timeout: ${formatMs(tcc.timeout_ms)}`;
        headerRight.appendChild(toLabel);
    }

    const typeMono = document.createElement('span');
    typeMono.className = 'mono';
    typeMono.style.fontSize = '0.75rem';
    typeMono.style.color = 'var(--admin-text-muted)';
    typeMono.textContent = tcc.type || '';
    headerRight.appendChild(typeMono);
    header.appendChild(headerRight);

    card.appendChild(header);

    // Collapsible body
    const body = document.createElement('div');
    body.className = 'admin-card-body';
    body.style.display = 'none';
    body.style.padding = '0';

    // Participants table
    const table = document.createElement('table');
    table.className = 'admin-table';

    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    for (const col of ['Order', 'Participant ID', 'Phases', 'Optional', 'Timeout']) {
        const th = document.createElement('th');
        th.textContent = col;
        headRow.appendChild(th);
    }
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    for (const p of tcc.participants || []) {
        const row = document.createElement('tr');

        // Order
        const tdOrder = document.createElement('td');
        const orderSpan = document.createElement('span');
        orderSpan.className = 'mono';
        orderSpan.textContent = String(p.order);
        tdOrder.appendChild(orderSpan);
        row.appendChild(tdOrder);

        // Participant ID
        const tdId = document.createElement('td');
        const idSpan = document.createElement('span');
        idSpan.className = 'mono';
        idSpan.style.fontWeight = '500';
        idSpan.textContent = p.id;
        tdId.appendChild(idSpan);
        row.appendChild(tdId);

        // Phases
        const tdPhases = document.createElement('td');
        tdPhases.appendChild(createPhaseIndicator('Try', p.has_try));
        tdPhases.appendChild(createPhaseIndicator('Confirm', p.has_confirm));
        tdPhases.appendChild(createPhaseIndicator('Cancel', p.has_cancel));
        row.appendChild(tdPhases);

        // Optional
        const tdOpt = document.createElement('td');
        tdOpt.appendChild(
            createBadge(
                p.optional ? 'Optional' : 'Required',
                p.optional ? 'badge-warning' : 'badge-neutral'
            )
        );
        row.appendChild(tdOpt);

        // Timeout
        const tdTimeout = document.createElement('td');
        const toSpan = document.createElement('span');
        toSpan.className = 'mono';
        toSpan.textContent = formatMs(p.timeout_ms);
        tdTimeout.appendChild(toSpan);
        row.appendChild(tdTimeout);

        tbody.appendChild(row);
    }
    table.appendChild(tbody);
    body.appendChild(table);
    card.appendChild(body);

    // Toggle collapse
    let expanded = false;
    header.addEventListener('click', () => {
        expanded = !expanded;
        body.style.display = expanded ? 'block' : 'none';
        chevron.style.transform = expanded ? 'rotate(90deg)' : 'rotate(0deg)';
    });

    return card;
}

/* ── Render ───────────────────────────────────────────────────── */

/**
 * Render the transactions view.
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
    h1.textContent = 'Transactions';
    headerLeft.appendChild(h1);
    const sub = document.createElement('div');
    sub.className = 'page-subtitle';
    sub.textContent = 'pyfly.transactional';
    headerLeft.appendChild(sub);
    header.appendChild(headerLeft);
    wrapper.appendChild(header);

    // Loading skeleton (four stat cards + a table)
    const loader = document.createElement('div');
    loader.appendChild(pageSkeleton({ stats: 4, rows: 6 }));
    wrapper.appendChild(loader);
    container.appendChild(wrapper);

    // Fetch data
    let data;
    try {
        data = await api.get('/transactions');
    } catch (err) {
        wrapper.removeChild(loader);
        wrapper.appendChild(createEmptyStateCard({
            icon: 'alert',
            tone: 'danger',
            title: 'Failed to load transaction data',
            text: err.message,
        }));
        return;
    }

    wrapper.removeChild(loader);

    const sagaCount = data.saga_count || 0;
    const tccCount = data.tcc_count || 0;
    const total = data.total || 0;
    const inFlight = data.in_flight || 0;

    // ── Stat cards row ───────────────────────────────────────
    const statsRow = document.createElement('div');
    statsRow.className = 'grid-4 mb-lg';
    statsRow.appendChild(createStatCard(total, 'Total Definitions'));
    statsRow.appendChild(createStatCard(sagaCount, 'Sagas'));
    statsRow.appendChild(createStatCard(tccCount, 'TCC Transactions'));
    statsRow.appendChild(createStatCard(inFlight, 'In-Flight'));
    wrapper.appendChild(statsRow);

    // ── Sagas section ────────────────────────────────────────
    const sagaSection = document.createElement('div');
    sagaSection.className = 'mb-lg';

    const sagaHeader = document.createElement('div');
    sagaHeader.style.display = 'flex';
    sagaHeader.style.alignItems = 'center';
    sagaHeader.style.gap = '8px';
    sagaHeader.style.marginBottom = '12px';
    const sagaTitle = document.createElement('h2');
    sagaTitle.style.margin = '0';
    sagaTitle.style.fontSize = '1.1rem';
    sagaTitle.textContent = 'Sagas';
    sagaHeader.appendChild(sagaTitle);
    sagaHeader.appendChild(createBadge(String(sagaCount), 'badge-info'));
    sagaSection.appendChild(sagaHeader);

    if (sagaCount === 0) {
        sagaSection.appendChild(createEmptyStateCard({
            icon: 'inbox',
            title: 'No sagas registered',
            text: 'No @saga definitions are registered in this application.',
        }));
    } else {
        for (const saga of data.sagas) {
            sagaSection.appendChild(renderSagaCard(saga));
        }
    }
    wrapper.appendChild(sagaSection);

    // ── TCC section ──────────────────────────────────────────
    const tccSection = document.createElement('div');

    const tccHeader = document.createElement('div');
    tccHeader.style.display = 'flex';
    tccHeader.style.alignItems = 'center';
    tccHeader.style.gap = '8px';
    tccHeader.style.marginBottom = '12px';
    const tccTitle = document.createElement('h2');
    tccTitle.style.margin = '0';
    tccTitle.style.fontSize = '1.1rem';
    tccTitle.textContent = 'TCC (Try-Confirm-Cancel)';
    tccHeader.appendChild(tccTitle);
    tccHeader.appendChild(createBadge(String(tccCount), 'badge-info'));
    tccSection.appendChild(tccHeader);

    if (tccCount === 0) {
        tccSection.appendChild(createEmptyStateCard({
            icon: 'inbox',
            title: 'No TCC transactions registered',
            text: 'No @tcc definitions are registered in this application.',
        }));
    } else {
        for (const tcc of data.tcc) {
            tccSection.appendChild(renderTccCard(tcc));
        }
    }
    wrapper.appendChild(tccSection);
}
