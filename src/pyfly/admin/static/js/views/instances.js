/**
 * PyFly Admin — Instances View.
 *
 * Fleet overview showing registered application instances with status
 * badges, URLs, and last-checked timestamps.  Provides stat cards for
 * total instances, UP count, and DOWN count.
 *
 * Data source: GET /admin/api/instances
 */

import { createEmptyStateCard } from '../components/empty-state.js';
import { pageSkeleton } from '../components/skeleton.js';
import { createStatusBadge } from '../components/status-badge.js';

/* -- Helpers --------------------------------------------------------- */

/**
 * Create a stat card element.
 *
 * @param {object}      opts
 * @param {string}      opts.label
 * @param {string|Node} opts.value     Text or DOM node for the value area.
 * @param {string}      opts.iconClass One of: primary, success, warning, danger, info.
 * @returns {HTMLElement}
 */
function createStatCard({ label, value, iconClass = 'primary' }) {
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

    card.appendChild(content);

    const icon = document.createElement('div');
    icon.className = `stat-card-icon ${iconClass}`;
    card.appendChild(icon);

    return card;
}

/**
 * Format an ISO timestamp to a human-readable string.
 * @param {string|null} isoString
 * @returns {string}
 */
function formatLastChecked(isoString) {
    if (!isoString) return 'Never';
    try {
        const d = new Date(isoString);
        return d.toLocaleString(undefined, {
            month: 'short',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
        });
    } catch (_) {
        return isoString;
    }
}

/**
 * Build a single instance card.
 *
 * @param {object} instance  { name, url, status, last_checked, metadata }
 * @returns {HTMLElement}
 */
function buildInstanceCard(instance) {
    const card = document.createElement('div');
    card.className = 'admin-card';
    card.style.marginBottom = '12px';

    const body = document.createElement('div');
    body.className = 'admin-card-body';
    body.style.display = 'flex';
    body.style.alignItems = 'center';
    body.style.justifyContent = 'space-between';
    body.style.flexWrap = 'wrap';
    body.style.gap = '12px';

    // Left: name + url
    const left = document.createElement('div');

    const nameEl = document.createElement('div');
    nameEl.style.fontWeight = '600';
    nameEl.style.fontSize = '1rem';
    nameEl.textContent = instance.name;
    left.appendChild(nameEl);

    const urlEl = document.createElement('div');
    urlEl.className = 'text-muted text-sm text-mono';
    urlEl.textContent = instance.url;
    left.appendChild(urlEl);

    body.appendChild(left);

    // Right: status badge + last checked
    const right = document.createElement('div');
    right.style.display = 'flex';
    right.style.alignItems = 'center';
    right.style.gap = '16px';

    const lastChecked = document.createElement('span');
    lastChecked.className = 'text-muted text-sm';
    lastChecked.textContent = formatLastChecked(instance.last_checked);
    right.appendChild(lastChecked);

    right.appendChild(createStatusBadge(instance.status || 'UNKNOWN'));

    body.appendChild(right);
    card.appendChild(body);

    return card;
}

/* -- Render ---------------------------------------------------------- */

/**
 * Render the instances fleet overview.
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
    h1.textContent = 'Instances';
    headerLeft.appendChild(h1);
    const sub = document.createElement('div');
    sub.className = 'page-subtitle';
    sub.textContent = 'server.instances';
    headerLeft.appendChild(sub);
    header.appendChild(headerLeft);
    wrapper.appendChild(header);

    // Loading skeleton (stat cards + instance list)
    const loader = document.createElement('div');
    loader.appendChild(pageSkeleton({ stats: 3, rows: 5 }));
    wrapper.appendChild(loader);
    container.appendChild(wrapper);

    // Fetch instances data
    let data;
    try {
        data = await api.get('/instances');
    } catch (err) {
        wrapper.removeChild(loader);
        wrapper.appendChild(createEmptyStateCard({
            icon: 'alert',
            tone: 'danger',
            title: 'Failed to load instances',
            text: err.message,
        }));
        return;
    }

    wrapper.removeChild(loader);

    const instances = data.instances || [];

    // -- Empty state --
    if (instances.length === 0) {
        wrapper.appendChild(createEmptyStateCard({
            icon: 'server',
            title: 'No instances registered',
            text: 'Register application instances via the admin server API or configure static discovery.',
        }));
        return;
    }

    // -- Stat cards row --
    const upCount = instances.filter((i) => i.status === 'UP').length;
    const downCount = instances.filter((i) => i.status === 'DOWN').length;

    const statsRow = document.createElement('div');
    statsRow.className = 'grid-4 mb-lg';

    statsRow.appendChild(
        createStatCard({
            label: 'Total Instances',
            value: String(instances.length),
            iconClass: 'primary',
        })
    );
    statsRow.appendChild(
        createStatCard({
            label: 'Up',
            value: String(upCount),
            iconClass: 'success',
        })
    );
    statsRow.appendChild(
        createStatCard({
            label: 'Down',
            value: String(downCount),
            iconClass: 'danger',
        })
    );
    statsRow.appendChild(
        createStatCard({
            label: 'Unknown',
            value: String(instances.length - upCount - downCount),
            iconClass: 'warning',
        })
    );

    wrapper.appendChild(statsRow);

    // -- Instance cards --
    const listSection = document.createElement('div');

    const listHeader = document.createElement('h3');
    listHeader.style.marginBottom = '12px';
    listHeader.style.fontSize = '0.95rem';
    listHeader.style.fontWeight = '600';
    listHeader.textContent = 'Registered Instances';
    listSection.appendChild(listHeader);

    for (const instance of instances) {
        listSection.appendChild(buildInstanceCard(instance));
    }

    wrapper.appendChild(listSection);
}
