/**
 * PyFly Admin — Mappings View.
 *
 * Displays HTTP request mappings with color-coded method badges,
 * searchable by path or controller name. Clicking a row opens a
 * detail panel with parameters, return type, and docstring.
 *
 * Data source:
 *   GET /admin/api/mappings -> { mappings: [...], total: N }
 */

import { createEmptyStateCard } from '../components/empty-state.js';
import { pageSkeleton } from '../components/skeleton.js';
import { createMethodBadge } from '../components/status-badge.js';
import { createTable } from '../components/table.js';

/* ── Detail Panel ────────────────────────────────────────────── */

function createDetailPanel() {
    const overlay = document.createElement('div');
    overlay.className = 'detail-panel-overlay';

    const panel = document.createElement('div');
    panel.className = 'detail-panel';

    // Header
    const panelHeader = document.createElement('div');
    panelHeader.className = 'detail-panel-header';
    const panelTitle = document.createElement('h3');
    panelTitle.textContent = 'Mapping Detail';
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

    // Body
    const panelBody = document.createElement('div');
    panelBody.className = 'detail-panel-body';
    panel.appendChild(panelBody);

    function hide() {
        overlay.classList.remove('open');
        panel.classList.remove('open');
    }

    closeBtn.addEventListener('click', hide);
    overlay.addEventListener('click', hide);

    function show(mapping) {
        panelBody.textContent = '';
        panelTitle.textContent = `${mapping.method} ${mapping.path}`;

        // Key-value table
        const kv = document.createElement('table');
        kv.className = 'kv-table';

        const rows = [
            ['Method', () => createMethodBadge(mapping.method)],
            ['Path', mapping.path],
            ['Controller', mapping.controller],
            ['Handler', mapping.handler],
            ['Status Code', String(mapping.status_code ?? 200)],
            ['Return Type', mapping.return_type || 'None'],
        ];

        if (mapping.response_model) {
            rows.push(['Response Model', String(mapping.response_model)]);
        }

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
                span.className = 'mono';
                span.textContent = value;
                td.appendChild(span);
            }
            tr.appendChild(td);
            kv.appendChild(tr);
        }
        panelBody.appendChild(kv);

        // Docstring
        if (mapping.doc) {
            const docHeader = document.createElement('h4');
            docHeader.textContent = 'Documentation';
            docHeader.style.marginTop = '20px';
            docHeader.style.marginBottom = '8px';
            docHeader.style.fontSize = '0.85rem';
            docHeader.style.fontWeight = '600';
            panelBody.appendChild(docHeader);

            const docBlock = document.createElement('div');
            docBlock.className = 'code-block';
            docBlock.textContent = mapping.doc;
            panelBody.appendChild(docBlock);
        }

        // Parameters table
        const params = mapping.parameters || [];
        if (params.length > 0) {
            const paramHeader = document.createElement('h4');
            paramHeader.textContent = 'Parameters';
            paramHeader.style.marginTop = '20px';
            paramHeader.style.marginBottom = '8px';
            paramHeader.style.fontSize = '0.85rem';
            paramHeader.style.fontWeight = '600';
            panelBody.appendChild(paramHeader);

            const paramTable = document.createElement('table');
            paramTable.className = 'admin-table';
            const pthead = document.createElement('thead');
            const ptr = document.createElement('tr');
            for (const h of ['Name', 'Type', 'Kind']) {
                const th = document.createElement('th');
                th.textContent = h;
                ptr.appendChild(th);
            }
            pthead.appendChild(ptr);
            paramTable.appendChild(pthead);

            const ptbody = document.createElement('tbody');
            for (const p of params) {
                const tr = document.createElement('tr');
                for (const val of [p.name, p.type, p.kind]) {
                    const td = document.createElement('td');
                    td.className = 'mono';
                    td.textContent = val || '--';
                    tr.appendChild(td);
                }
                ptbody.appendChild(tr);
            }
            paramTable.appendChild(ptbody);
            panelBody.appendChild(paramTable);
        }

        overlay.classList.add('open');
        panel.classList.add('open');
    }

    return { overlay, panel, show, hide };
}

/* ── Render ───────────────────────────────────────────────────── */

/**
 * Render the mappings view.
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
    h1.textContent = 'Mappings';
    headerLeft.appendChild(h1);
    const sub = document.createElement('div');
    sub.className = 'page-subtitle';
    sub.textContent = 'application.mappings';
    headerLeft.appendChild(sub);
    header.appendChild(headerLeft);
    wrapper.appendChild(header);

    // Loading skeleton (method stat cards + mappings table)
    const loader = document.createElement('div');
    loader.appendChild(pageSkeleton({ stats: 4, rows: 8 }));
    wrapper.appendChild(loader);
    container.appendChild(wrapper);

    // Fetch mappings
    let data;
    try {
        data = await api.get('/mappings');
    } catch (err) {
        wrapper.removeChild(loader);
        wrapper.appendChild(createEmptyStateCard({
            icon: 'alert',
            tone: 'danger',
            title: 'Failed to load mappings',
            text: err.message,
        }));
        return;
    }

    wrapper.removeChild(loader);

    const mappings = data.mappings || [];
    const total = data.total != null ? data.total : mappings.length;

    // ── Method stat cards ─────────────────────────────────────────
    const methodCounts = {};
    for (const m of mappings) {
        methodCounts[m.method] = (methodCounts[m.method] || 0) + 1;
    }

    const statsRow = document.createElement('div');
    statsRow.className = 'grid-4 mb-lg';

    // Total card
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
    totalLabel.textContent = 'Total Mappings';
    totalContent.appendChild(totalLabel);
    totalCard.appendChild(totalContent);
    statsRow.appendChild(totalCard);

    // Method breakdown cards
    const methodOrder = ['GET', 'POST', 'PUT', 'DELETE', 'PATCH'];
    for (const method of methodOrder) {
        if (!methodCounts[method]) continue;
        const card = document.createElement('div');
        card.className = 'stat-card';
        const content = document.createElement('div');
        content.className = 'stat-card-content';
        const val = document.createElement('div');
        val.className = 'stat-card-value';
        val.textContent = String(methodCounts[method]);
        content.appendChild(val);
        const label = document.createElement('div');
        label.className = 'stat-card-label';
        label.textContent = method;
        content.appendChild(label);
        card.appendChild(content);
        statsRow.appendChild(card);
    }

    wrapper.appendChild(statsRow);

    // ── Empty state ──────────────────────────────────────────────
    if (total === 0) {
        wrapper.appendChild(createEmptyStateCard({
            icon: 'inbox',
            title: 'No request mappings',
            text: 'No HTTP routes are registered in this application.',
        }));
        return;
    }

    // ── Detail panel ─────────────────────────────────────────────
    const { overlay, panel, show } = createDetailPanel();
    wrapper.appendChild(overlay);
    wrapper.appendChild(panel);

    // ── Table ────────────────────────────────────────────────────
    const tableCard = document.createElement('div');
    tableCard.className = 'admin-card';

    const tableHeader = document.createElement('div');
    tableHeader.className = 'admin-card-header';
    const tableTitle = document.createElement('h3');
    tableTitle.textContent = 'Request Mappings';
    tableHeader.appendChild(tableTitle);
    tableCard.appendChild(tableHeader);

    const tableBody = document.createElement('div');
    tableBody.className = 'admin-card-body';
    tableBody.style.padding = '0';

    const tableEl = createTable({
        columns: [
            {
                key: 'method',
                label: 'Method',
                render(val) {
                    return createMethodBadge(val);
                },
            },
            {
                key: 'path',
                label: 'Path',
                render(val) {
                    const span = document.createElement('span');
                    span.className = 'mono';
                    span.textContent = val || '--';
                    return span;
                },
            },
            {
                key: 'controller',
                label: 'Controller',
                render(val) {
                    const span = document.createElement('span');
                    span.textContent = val || '--';
                    return span;
                },
            },
            {
                key: 'handler',
                label: 'Handler',
                render(val) {
                    const span = document.createElement('span');
                    span.className = 'mono';
                    span.textContent = val || '--';
                    return span;
                },
            },
            {
                key: 'status_code',
                label: 'Status Code',
                render(val) {
                    const span = document.createElement('span');
                    span.className = 'mono';
                    span.textContent = val != null ? String(val) : '--';
                    return span;
                },
            },
        ],
        data: mappings,
        searchable: true,
        sortable: true,
        emptyText: 'No request mappings',
        onRowClick: (row) => show(row),
    });

    tableBody.appendChild(tableEl);
    tableCard.appendChild(tableBody);
    wrapper.appendChild(tableCard);
}
