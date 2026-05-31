/**
 * PyFly Admin — Health View.
 *
 * Displays overall application health status and a component tree
 * with expand/collapse and live updates via SSE.
 *
 * Data source:  GET /admin/api/health
 * SSE stream:   /health  (event type "health")
 */

import { createEmptyStateCard } from '../components/empty-state.js';
import { pageSkeleton } from '../components/skeleton.js';
import { createStatusBadge } from '../components/status-badge.js';
import { sse } from '../sse.js';

/* ── Helpers ──────────────────────────────────────────────────── */

/**
 * Format an ISO timestamp or Date to a human-readable string.
 * @param {Date|string|number} [ts]
 * @returns {string}
 */
function formatTimestamp(ts) {
    if (!ts) return 'just now';
    try {
        const d = ts instanceof Date ? ts : new Date(ts);
        return d.toLocaleTimeString(undefined, {
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
        });
    } catch (_) {
        return 'just now';
    }
}

/**
 * Create a chevron SVG used for expand/collapse toggles.
 * @returns {SVGElement}
 */
function createChevron() {
    const svgNS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('class', 'chevron');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    const path = document.createElementNS(svgNS, 'path');
    path.setAttribute('d', 'M9 18l6-6-6-6');
    svg.appendChild(path);
    return svg;
}

/**
 * Build a key-value table from a details object.
 * @param {Object<string, *>} details
 * @returns {HTMLTableElement}
 */
function buildDetailsTable(details) {
    const table = document.createElement('table');
    table.className = 'kv-table';
    const tbody = document.createElement('tbody');
    for (const [key, val] of Object.entries(details)) {
        const tr = document.createElement('tr');
        const th = document.createElement('th');
        th.textContent = key;
        const td = document.createElement('td');
        if (typeof val === 'object' && val !== null) {
            td.className = 'text-mono text-sm';
            td.textContent = JSON.stringify(val);
        } else {
            td.textContent = val != null ? String(val) : '--';
        }
        tr.appendChild(th);
        tr.appendChild(td);
        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    return table;
}

/**
 * Recursively build a health component card.
 *
 * @param {string} name        Component name
 * @param {object} component   { status, details?, components? }
 * @param {number} depth       Nesting depth (for indentation)
 * @returns {HTMLElement}
 */
function buildComponentCard(name, component, depth = 0) {
    const card = document.createElement('div');
    card.className = 'admin-card';
    if (depth > 0) {
        card.style.marginLeft = `${depth * 20}px`;
    }
    card.style.marginBottom = '12px';

    const hasChildren = component.components && Object.keys(component.components).length > 0;
    const hasDetails = component.details && Object.keys(component.details).length > 0;
    const isExpandable = hasChildren || hasDetails;

    // Header (collapsible)
    const header = document.createElement('div');
    header.className = 'collapsible-header';
    if (isExpandable) {
        header.classList.add('expanded');
    }

    if (isExpandable) {
        header.appendChild(createChevron());
    }

    // Component name
    const nameEl = document.createElement('span');
    nameEl.style.fontWeight = '600';
    nameEl.style.flex = '1';
    nameEl.textContent = name;
    header.appendChild(nameEl);

    // Status badge
    header.appendChild(createStatusBadge(component.status || 'UNKNOWN'));

    card.appendChild(header);

    // Collapsible content
    if (isExpandable) {
        const content = document.createElement('div');
        content.className = 'collapsible-content expanded';

        const contentInner = document.createElement('div');
        contentInner.style.padding = '0 16px 16px';

        // Details table
        if (hasDetails) {
            contentInner.appendChild(buildDetailsTable(component.details));
        }

        // Nested components
        if (hasChildren) {
            const childContainer = document.createElement('div');
            childContainer.className = 'mt-md';
            for (const [childName, childComp] of Object.entries(component.components)) {
                childContainer.appendChild(buildComponentCard(childName, childComp, depth + 1));
            }
            contentInner.appendChild(childContainer);
        }

        content.appendChild(contentInner);
        card.appendChild(content);

        // Toggle expand/collapse
        header.addEventListener('click', () => {
            const isExpanded = header.classList.contains('expanded');
            if (isExpanded) {
                header.classList.remove('expanded');
                content.classList.remove('expanded');
            } else {
                header.classList.add('expanded');
                content.classList.add('expanded');
            }
        });
    }

    return card;
}

/* ── Render ───────────────────────────────────────────────────── */

// References that the SSE updater needs access to
let overallBadgeContainer = null;
let timestampEl = null;
let componentTree = null;

/**
 * Rebuild the component tree and overall status from health data.
 * Used both on initial render and SSE updates.
 * @param {object} healthData
 */
function updateHealthUI(healthData) {
    // Update overall status badge
    if (overallBadgeContainer) {
        overallBadgeContainer.replaceChildren();
        overallBadgeContainer.appendChild(createStatusBadge(healthData.status || 'UNKNOWN'));
    }

    // Update timestamp
    if (timestampEl) {
        timestampEl.textContent = 'Last checked: ' + formatTimestamp(new Date());
    }

    // Rebuild component tree
    if (componentTree && healthData.components) {
        componentTree.replaceChildren();
        for (const [name, comp] of Object.entries(healthData.components)) {
            componentTree.appendChild(buildComponentCard(name, comp));
        }
    } else if (componentTree) {
        componentTree.replaceChildren();
        const noComp = document.createElement('div');
        noComp.className = 'text-muted text-sm';
        noComp.style.padding = '16px';
        noComp.textContent = 'No component data available';
        componentTree.appendChild(noComp);
    }
}

/**
 * Render the health status view.
 * @param {HTMLElement} container
 * @param {import('../api.js').AdminAPI} api
 * @returns {function} Cleanup function to disconnect SSE.
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
    h1.textContent = 'Health';
    headerLeft.appendChild(h1);
    const sub = document.createElement('div');
    sub.className = 'page-subtitle';
    sub.textContent = 'application.health';
    headerLeft.appendChild(sub);
    header.appendChild(headerLeft);
    wrapper.appendChild(header);

    // Loading skeleton (overall status stat cards + component indicator rows)
    const loader = document.createElement('div');
    loader.appendChild(pageSkeleton({ stats: 4, rows: 5 }));
    wrapper.appendChild(loader);
    container.appendChild(wrapper);

    // Fetch health data
    let healthData;
    try {
        healthData = await api.get('/health');
    } catch (err) {
        wrapper.removeChild(loader);
        wrapper.appendChild(createEmptyStateCard({
            icon: 'alert',
            tone: 'danger',
            title: 'Failed to load health data',
            text: err.message,
        }));
        return;
    }

    wrapper.removeChild(loader);

    // ── Overall status card ───────────────────────────────────
    const statusCard = document.createElement('div');
    statusCard.className = 'admin-card mb-lg';

    const statusBody = document.createElement('div');
    statusBody.className = 'admin-card-body';
    statusBody.style.display = 'flex';
    statusBody.style.alignItems = 'center';
    statusBody.style.justifyContent = 'space-between';

    const statusLeft = document.createElement('div');
    statusLeft.style.display = 'flex';
    statusLeft.style.alignItems = 'center';
    statusLeft.style.gap = '16px';

    const statusLabel = document.createElement('span');
    statusLabel.style.fontWeight = '600';
    statusLabel.style.fontSize = '1.1rem';
    statusLabel.textContent = 'Overall Status';
    statusLeft.appendChild(statusLabel);

    // Badge container (will be updated by SSE)
    overallBadgeContainer = document.createElement('span');
    overallBadgeContainer.appendChild(createStatusBadge(healthData.status || 'UNKNOWN'));
    statusLeft.appendChild(overallBadgeContainer);

    statusBody.appendChild(statusLeft);

    // Timestamp
    timestampEl = document.createElement('span');
    timestampEl.className = 'text-muted text-sm';
    timestampEl.textContent = 'Last checked: ' + formatTimestamp(new Date());
    statusBody.appendChild(timestampEl);

    statusCard.appendChild(statusBody);
    wrapper.appendChild(statusCard);

    // ── Component tree ────────────────────────────────────────
    const treeSection = document.createElement('div');

    const treeSectionHeader = document.createElement('h3');
    treeSectionHeader.style.marginBottom = '12px';
    treeSectionHeader.style.fontSize = '0.95rem';
    treeSectionHeader.style.fontWeight = '600';
    treeSectionHeader.textContent = 'Components';
    treeSection.appendChild(treeSectionHeader);

    componentTree = document.createElement('div');

    if (healthData.components && Object.keys(healthData.components).length > 0) {
        for (const [name, comp] of Object.entries(healthData.components)) {
            componentTree.appendChild(buildComponentCard(name, comp));
        }
    } else {
        componentTree.appendChild(createEmptyStateCard({
            icon: 'activity',
            title: 'No health components registered',
            text: 'This application has not registered any health indicators.',
        }));
    }

    treeSection.appendChild(componentTree);
    wrapper.appendChild(treeSection);

    // ── SSE: Real-time health updates ─────────────────────────
    sse.connectTyped('/health', 'health', (data) => {
        updateHealthUI(data);
    });

    // Return cleanup function
    return function cleanup() {
        sse.disconnect('/health');
        // Reset module-level references
        overallBadgeContainer = null;
        timestampEl = null;
        componentTree = null;
    };
}
