/**
 * PyFly Admin — CQRS View.
 *
 * Displays CQRS command and query handler registrations with
 * stat cards, bus pipeline feature indicators, searchable table,
 * and kind badges.
 *
 * Data source:
 *   GET /admin/api/cqrs -> { handlers: [...], total: N, pipeline: {...} }
 */

import { createEmptyStateCard } from '../components/empty-state.js';
import { pageSkeleton } from '../components/skeleton.js';
import { createTable } from '../components/table.js';

/* ── Helpers ──────────────────────────────────────────────────── */

/** Badge CSS class per handler kind. */
const KIND_CLASSES = {
    command: 'badge-info',
    query:   'badge-success',
};

/**
 * Create a kind badge element (command=blue, query=green).
 * @param {string} kind
 * @returns {HTMLSpanElement}
 */
function createKindBadge(kind) {
    const badge = document.createElement('span');
    const norm = (kind || '').toLowerCase();
    const cls = KIND_CLASSES[norm] || 'badge-neutral';
    badge.className = `badge ${cls}`;
    badge.textContent = (kind || '--').toUpperCase();
    return badge;
}

/**
 * Truncate a module path, keeping the last two segments.
 * e.g. "myapp.handlers.create_user.CreateUserHandler" -> "...create_user.CreateUserHandler"
 * @param {string} typePath
 * @returns {string}
 */
function truncateType(typePath) {
    if (!typePath) return '--';
    const parts = typePath.split('.');
    if (parts.length <= 2) return typePath;
    return '...' + parts.slice(-2).join('.');
}

/* ── Render ───────────────────────────────────────────────────── */

/**
 * Render the CQRS handler listing view.
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
    h1.textContent = 'CQRS';
    headerLeft.appendChild(h1);
    const sub = document.createElement('div');
    sub.className = 'page-subtitle';
    sub.textContent = 'application.cqrs';
    headerLeft.appendChild(sub);
    header.appendChild(headerLeft);
    wrapper.appendChild(header);

    // Loading skeleton (three stat cards + a table)
    const loader = document.createElement('div');
    loader.appendChild(pageSkeleton({ stats: 3, rows: 6 }));
    wrapper.appendChild(loader);
    container.appendChild(wrapper);

    // Fetch CQRS data
    let data;
    try {
        data = await api.get('/cqrs');
    } catch (err) {
        wrapper.removeChild(loader);
        wrapper.appendChild(createEmptyStateCard({
            icon: 'alert',
            tone: 'danger',
            title: 'Failed to load CQRS data',
            text: err.message,
        }));
        return;
    }

    wrapper.removeChild(loader);

    const handlers = data.handlers || [];
    const total = data.total != null ? data.total : handlers.length;
    const commandCount = handlers.filter(h => h.kind === 'command').length;
    const queryCount = handlers.filter(h => h.kind === 'query').length;

    // ── Stat cards row ───────────────────────────────────────
    const statsRow = document.createElement('div');
    statsRow.className = 'grid-3 mb-lg';

    // Total handlers
    const totalCard = document.createElement('div');
    totalCard.className = 'stat-card';
    const totalContent = document.createElement('div');
    totalContent.className = 'stat-card-content';
    const totalVal = document.createElement('div');
    totalVal.className = 'stat-card-value';
    totalVal.textContent = String(total);
    totalContent.appendChild(totalVal);
    const totalLabel = document.createElement('div');
    totalLabel.className = 'stat-card-label';
    totalLabel.textContent = 'Total Handlers';
    totalContent.appendChild(totalLabel);
    totalCard.appendChild(totalContent);
    statsRow.appendChild(totalCard);

    // Commands count
    const cmdCard = document.createElement('div');
    cmdCard.className = 'stat-card';
    const cmdContent = document.createElement('div');
    cmdContent.className = 'stat-card-content';
    const cmdVal = document.createElement('div');
    cmdVal.className = 'stat-card-value';
    cmdVal.textContent = String(commandCount);
    cmdContent.appendChild(cmdVal);
    const cmdLabel = document.createElement('div');
    cmdLabel.className = 'stat-card-label';
    cmdLabel.textContent = 'Commands';
    cmdContent.appendChild(cmdLabel);
    cmdCard.appendChild(cmdContent);
    statsRow.appendChild(cmdCard);

    // Queries count
    const qryCard = document.createElement('div');
    qryCard.className = 'stat-card';
    const qryContent = document.createElement('div');
    qryContent.className = 'stat-card-content';
    const qryVal = document.createElement('div');
    qryVal.className = 'stat-card-value';
    qryVal.textContent = String(queryCount);
    qryContent.appendChild(qryVal);
    const qryLabel = document.createElement('div');
    qryLabel.className = 'stat-card-label';
    qryLabel.textContent = 'Queries';
    qryContent.appendChild(qryLabel);
    qryCard.appendChild(qryContent);
    statsRow.appendChild(qryCard);

    wrapper.appendChild(statsRow);

    // ── Bus Pipeline card ─────────────────────────────────────
    const pipeline = data.pipeline || {};
    const pipelineCard = document.createElement('div');
    pipelineCard.className = 'admin-card mb-lg';

    const pipelineHeader = document.createElement('div');
    pipelineHeader.className = 'admin-card-header';
    const pipelineTitle = document.createElement('h3');
    pipelineTitle.textContent = 'Bus Pipeline';
    pipelineHeader.appendChild(pipelineTitle);
    pipelineCard.appendChild(pipelineHeader);

    const pipelineBody = document.createElement('div');
    pipelineBody.className = 'admin-card-body';
    pipelineBody.style.display = 'flex';
    pipelineBody.style.flexWrap = 'wrap';
    pipelineBody.style.gap = '16px';

    const pipelineFeatures = [
        { key: 'command_bus', label: 'Command Bus' },
        { key: 'query_bus', label: 'Query Bus' },
        { key: 'validation', label: 'Validation' },
        { key: 'authorization', label: 'Authorization' },
        { key: 'metrics', label: 'Metrics' },
        { key: 'event_publishing', label: 'Event Publishing' },
    ];

    for (const feat of pipelineFeatures) {
        const active = !!pipeline[feat.key];
        const item = document.createElement('div');
        item.style.display = 'inline-flex';
        item.style.alignItems = 'center';
        item.style.gap = '6px';

        const dot = document.createElement('span');
        dot.style.width = '10px';
        dot.style.height = '10px';
        dot.style.borderRadius = '50%';
        dot.style.display = 'inline-block';
        dot.style.backgroundColor = active
            ? 'var(--admin-success)'
            : 'var(--admin-text-muted)';
        dot.style.opacity = active ? '1' : '0.3';
        item.appendChild(dot);

        const label = document.createElement('span');
        label.style.fontSize = '0.85rem';
        label.style.color = active
            ? 'var(--admin-text-primary)'
            : 'var(--admin-text-muted)';
        label.textContent = feat.label;
        item.appendChild(label);

        pipelineBody.appendChild(item);
    }

    pipelineCard.appendChild(pipelineBody);
    wrapper.appendChild(pipelineCard);

    // ── Handlers table ───────────────────────────────────────
    const tableCard = document.createElement('div');
    tableCard.className = 'admin-card';

    const tableHeader = document.createElement('div');
    tableHeader.className = 'admin-card-header';
    const tableTitle = document.createElement('h3');
    tableTitle.textContent = 'Registered Handlers';
    tableHeader.appendChild(tableTitle);
    tableCard.appendChild(tableHeader);

    const tableBody = document.createElement('div');
    tableBody.className = 'admin-card-body';
    tableBody.style.padding = '0';

    const tableEl = createTable({
        columns: [
            {
                key: 'kind',
                label: 'Kind',
                render(val) {
                    return createKindBadge(val);
                },
            },
            {
                key: 'message_name',
                label: 'Message Name',
                render(val) {
                    const span = document.createElement('span');
                    span.style.fontWeight = '500';
                    span.textContent = val || '--';
                    return span;
                },
            },
            {
                key: 'handler_name',
                label: 'Handler Name',
                render(val) {
                    const span = document.createElement('span');
                    span.textContent = val || '--';
                    return span;
                },
            },
            {
                key: 'message_type',
                label: 'Full Type',
                render(val) {
                    const span = document.createElement('span');
                    span.className = 'mono truncate';
                    span.style.maxWidth = '280px';
                    span.style.display = 'inline-block';
                    span.title = val || '';
                    span.textContent = truncateType(val);
                    return span;
                },
            },
        ],
        data: handlers,
        searchable: true,
        sortable: true,
        emptyText: 'No CQRS handlers registered',
    });

    tableBody.appendChild(tableEl);
    tableCard.appendChild(tableBody);
    wrapper.appendChild(tableCard);
}
