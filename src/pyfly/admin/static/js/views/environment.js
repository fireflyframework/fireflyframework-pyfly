/**
 * PyFly Admin — Environment View.
 *
 * Displays active profiles, config sources, and a filterable
 * properties table with type-aware value styling.
 *
 * Data source:  GET /admin/api/env
 *   -> { active_profiles: [...], properties: {...}, sources: [...] }
 */

import { createEmptyStateCard } from '../components/empty-state.js';
import { createFilterToolbar } from '../components/filter-toolbar.js';
import { pageSkeleton } from '../components/skeleton.js';
import { createTable } from '../components/table.js';

/* ── Helpers ──────────────────────────────────────────────────── */

/**
 * Determine the visual type of a property value for styling purposes.
 * @param {*} val
 * @returns {"boolean"|"number"|"string"}
 */
function valueType(val) {
    if (val === true || val === false || val === 'true' || val === 'false') {
        return 'boolean';
    }
    if (typeof val === 'number' || (typeof val === 'string' && val !== '' && !isNaN(Number(val)))) {
        return 'number';
    }
    return 'string';
}

/**
 * Create a styled value element based on its detected type.
 * @param {*} val
 * @returns {HTMLSpanElement}
 */
function renderValue(val) {
    const span = document.createElement('span');
    const type = valueType(val);
    const text = val != null ? String(val) : '';

    span.textContent = text;
    span.className = 'mono';
    span.style.fontSize = '0.8rem';

    if (type === 'boolean') {
        span.style.color = 'var(--admin-warning)';
        span.style.fontWeight = '600';
    } else if (type === 'number') {
        span.style.color = 'var(--admin-info)';
        span.style.fontWeight = '600';
    } else {
        span.style.color = 'var(--admin-text)';
    }

    return span;
}

/**
 * Flatten a nested properties object into dot-notation key/value pairs.
 * @param {object} obj
 * @param {string} prefix
 * @returns {Array<{key: string, value: *}>}
 */
function flattenProperties(obj, prefix = '') {
    const entries = [];
    for (const [k, v] of Object.entries(obj)) {
        const fullKey = prefix ? `${prefix}.${k}` : k;
        if (v != null && typeof v === 'object' && !Array.isArray(v)) {
            entries.push(...flattenProperties(v, fullKey));
        } else {
            entries.push({ key: fullKey, value: v });
        }
    }
    return entries;
}

/**
 * Filter properties by search text and type pill.
 * @param {Array<{key: string, value: *}>} data
 * @param {string} search  Lowercase search term.
 * @param {string} pill    Type filter: "" | "boolean" | "number" | "string".
 * @returns {Array<{key: string, value: *}>}
 */
function filterProperties(data, search, pill) {
    return data.filter((entry) => {
        // Pill type filter
        if (pill && valueType(entry.value) !== pill) {
            return false;
        }
        // Search filter (match key or value)
        if (search) {
            const keyStr = entry.key.toLowerCase();
            const valStr = entry.value != null ? String(entry.value).toLowerCase() : '';
            if (!keyStr.includes(search) && !valStr.includes(search)) {
                return false;
            }
        }
        return true;
    });
}

/* ── Render ───────────────────────────────────────────────────── */

/**
 * Render the environment view.
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
    h1.textContent = 'Environment';
    headerLeft.appendChild(h1);
    const sub = document.createElement('div');
    sub.className = 'page-subtitle';
    sub.textContent = 'application.env';
    headerLeft.appendChild(sub);
    header.appendChild(headerLeft);
    wrapper.appendChild(header);

    // Loading skeleton (profiles + sources stat cards, properties table)
    const loader = document.createElement('div');
    loader.appendChild(pageSkeleton({ stats: 2, rows: 8 }));
    wrapper.appendChild(loader);
    container.appendChild(wrapper);

    // Fetch environment data
    let envData;
    try {
        envData = await api.get('/env');
    } catch (err) {
        wrapper.removeChild(loader);
        wrapper.appendChild(createEmptyStateCard({
            icon: 'alert',
            tone: 'danger',
            title: 'Failed to load environment data',
            text: err.message,
        }));
        return;
    }

    wrapper.removeChild(loader);

    const activeProfiles = envData.active_profiles || envData.activeProfiles || [];
    const properties = envData.properties || {};
    const origins = envData.origins || {};
    const propertySources = envData.propertySources || [];
    const sources = envData.sources || [];

    // ── Flatten properties for table and toolbar ─────────────
    const propsFlat = flattenProperties(properties);
    // Attribute each effective property to its winning source.
    for (const entry of propsFlat) {
        entry.origin = origins[entry.key] || '';
    }

    // ── Filter Toolbar ──────────────────────────────────────
    const toolbar = createFilterToolbar({
        placeholder: 'Search properties...',
        pills: [
            { label: 'All', value: '' },
            { label: 'Boolean', value: 'boolean' },
            { label: 'Number', value: 'number' },
            { label: 'String', value: 'string' },
        ],
        onFilter: ({ search, pill }) => {
            const filtered = filterProperties(propsFlat, search, pill);
            tableEl.updateData(filtered);
            toolbar.updateCount(filtered.length, propsFlat.length);
        },
        totalCount: propsFlat.length,
    });
    wrapper.appendChild(toolbar);

    // ── Active Profiles card ─────────────────────────────────
    const profileCard = document.createElement('div');
    profileCard.className = 'admin-card mb-lg';

    const profileHeader = document.createElement('div');
    profileHeader.className = 'admin-card-header';
    const profileTitle = document.createElement('h3');
    profileTitle.textContent = 'Active Profiles';
    profileHeader.appendChild(profileTitle);
    profileCard.appendChild(profileHeader);

    const profileBody = document.createElement('div');
    profileBody.className = 'admin-card-body';
    profileBody.style.display = 'flex';
    profileBody.style.alignItems = 'center';
    profileBody.style.gap = '8px';
    profileBody.style.flexWrap = 'wrap';

    if (activeProfiles.length === 0) {
        const noneEl = document.createElement('span');
        noneEl.className = 'text-muted text-sm';
        noneEl.textContent = 'No active profiles';
        profileBody.appendChild(noneEl);
    } else {
        const PROFILE_BADGE_CLASSES = ['badge-info', 'badge-success', 'badge-warning', 'badge-get', 'badge-patch'];
        for (let i = 0; i < activeProfiles.length; i++) {
            const badge = document.createElement('span');
            badge.className = 'badge ' + PROFILE_BADGE_CLASSES[i % PROFILE_BADGE_CLASSES.length];
            const dot = document.createElement('span');
            dot.className = 'badge-dot';
            badge.appendChild(dot);
            const text = document.createTextNode(activeProfiles[i]);
            badge.appendChild(text);
            profileBody.appendChild(badge);
        }
    }

    profileCard.appendChild(profileBody);
    wrapper.appendChild(profileCard);

    // ── Property Sources card (Spring /actuator/env ordering) ──
    // Prefer the rich propertySources (name + property count, highest
    // precedence first); fall back to the flat source-name list.
    const sourceRows = propertySources.length
        ? propertySources.map((s) => ({
              name: s.name,
              count: s.properties ? Object.keys(s.properties).length : 0,
          }))
        : sources.map((s) => ({ name: typeof s === 'string' ? s : (s.name || ''), count: null }));

    if (sourceRows.length > 0) {
        const sourcesCard = document.createElement('div');
        sourcesCard.className = 'admin-card mb-lg';

        const sourcesHeader = document.createElement('div');
        sourcesHeader.className = 'admin-card-header';
        const sourcesTitle = document.createElement('h3');
        sourcesTitle.textContent = 'Property Sources';
        const sourcesCount = document.createElement('span');
        sourcesCount.className = 'card-subtitle';
        sourcesCount.textContent = sourceRows.length + ' sources (highest precedence first)';
        sourcesHeader.appendChild(sourcesTitle);
        sourcesHeader.appendChild(sourcesCount);
        sourcesCard.appendChild(sourcesHeader);

        const sourcesBody = document.createElement('div');
        sourcesBody.className = 'admin-card-body';
        sourcesBody.style.padding = '0';

        const sourcesList = document.createElement('div');
        for (let i = 0; i < sourceRows.length; i++) {
            const src = sourceRows[i];
            const item = document.createElement('div');
            item.style.padding = '10px 20px';
            item.style.display = 'flex';
            item.style.alignItems = 'center';
            item.style.gap = '12px';
            if (i < sourceRows.length - 1) {
                item.style.borderBottom = '1px solid var(--admin-border-subtle)';
            }

            const idx = document.createElement('span');
            idx.className = 'text-mono text-xs text-muted';
            idx.textContent = '#' + (i + 1);
            idx.style.minWidth = '28px';
            item.appendChild(idx);

            const name = document.createElement('span');
            name.className = 'text-mono text-sm';
            name.style.flex = '1';
            name.textContent = src.name;
            item.appendChild(name);

            if (src.count != null) {
                const badge = document.createElement('span');
                badge.className = 'badge badge-neutral';
                badge.textContent = src.count + (src.count === 1 ? ' property' : ' properties');
                item.appendChild(badge);
            }

            sourcesList.appendChild(item);
        }

        sourcesBody.appendChild(sourcesList);
        sourcesCard.appendChild(sourcesBody);
        wrapper.appendChild(sourcesCard);
    }

    // ── Properties table ─────────────────────────────────────
    const propsCard = document.createElement('div');
    propsCard.className = 'admin-card';

    const propsHeader = document.createElement('div');
    propsHeader.className = 'admin-card-header';
    const propsTitle = document.createElement('h3');
    propsTitle.textContent = 'Properties';
    const propsCount = document.createElement('span');
    propsCount.className = 'card-subtitle';
    propsCount.textContent = propsFlat.length + ' properties';
    propsHeader.appendChild(propsTitle);
    propsHeader.appendChild(propsCount);
    propsCard.appendChild(propsHeader);

    const propsBody = document.createElement('div');
    propsBody.className = 'admin-card-body';
    propsBody.style.padding = '0';

    const tableEl = createTable({
        columns: [
            {
                key: 'key',
                label: 'Key',
                render(val) {
                    const span = document.createElement('span');
                    span.className = 'mono';
                    span.textContent = val || '';
                    return span;
                },
            },
            {
                key: 'value',
                label: 'Value',
                render(val) {
                    return renderValue(val);
                },
            },
            {
                key: 'origin',
                label: 'Source',
                render(val) {
                    const span = document.createElement('span');
                    span.className = 'text-xs text-muted';
                    let s = val ? String(val) : '';
                    const slash = s.lastIndexOf('/');
                    if (slash >= 0) s = s.slice(slash + 1);
                    span.textContent = s.length > 40 ? s.slice(0, 39) + '…' : s;
                    span.title = val || '';
                    return span;
                },
            },
        ],
        data: propsFlat,
        searchable: false,
        sortable: true,
        emptyText: 'No properties found',
    });

    propsBody.appendChild(tableEl);
    propsCard.appendChild(propsBody);
    wrapper.appendChild(propsCard);
}
