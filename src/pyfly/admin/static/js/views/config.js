/**
 * PyFly Admin — Configuration View.
 *
 * Displays application configuration properties grouped by prefix
 * using collapsible accordion sections with search filtering.
 *
 * Data source:  GET /admin/api/config
 *   -> { groups: { "pyfly.web": { "port": {value, origin, sensitive}, ... }, ... } }
 *      (effective values, sorted, masked, attributed to their source)
 */

import { createEmptyStateCard } from '../components/empty-state.js';
import { createFilterToolbar } from '../components/filter-toolbar.js';
import { pageSkeleton } from '../components/skeleton.js';

/* ── Helpers ──────────────────────────────────────────────────── */

/**
 * Create a chevron SVG for expand/collapse toggles.
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
 * Build a key-value table for a group's properties.
 * @param {Object<string, *>} props
 * @returns {HTMLTableElement}
 */
function buildGroupTable(props) {
    const table = document.createElement('table');
    table.className = 'kv-table';
    const tbody = document.createElement('tbody');

    for (const [key, entry] of Object.entries(props)) {
        // Each entry is { value, origin, sensitive }. Tolerate a bare scalar too.
        const isRich = entry != null && typeof entry === 'object' && !Array.isArray(entry) && 'value' in entry;
        const val = isRich ? entry.value : entry;
        const origin = isRich ? entry.origin : '';
        const sensitive = isRich ? entry.sensitive : false;

        const tr = document.createElement('tr');

        const th = document.createElement('th');
        th.textContent = key;
        tr.appendChild(th);

        const td = document.createElement('td');
        td.className = 'text-mono text-sm';
        const display = Array.isArray(val) ? val.join(', ') : (val != null ? String(val) : '');
        td.textContent = display;

        if (sensitive) {
            td.style.color = 'var(--admin-text-muted)';
        } else if (val === true || val === false || val === 'true' || val === 'false') {
            td.style.color = 'var(--admin-warning)';
            td.style.fontWeight = '600';
        } else if (typeof val === 'number' || (typeof val === 'string' && val !== '' && !isNaN(Number(val)))) {
            td.style.color = 'var(--admin-info)';
            td.style.fontWeight = '600';
        }
        tr.appendChild(td);

        // Origin (source) attribution column.
        const originTd = document.createElement('td');
        originTd.className = 'text-xs text-muted';
        originTd.style.textAlign = 'right';
        originTd.textContent = origin ? _shortOrigin(origin) : '';
        originTd.title = origin || '';
        tr.appendChild(originTd);

        tbody.appendChild(tr);
    }

    table.appendChild(tbody);
    return table;
}

/**
 * Shorten a verbose source name (file path / annotation) for the origin badge.
 * @param {string} origin
 * @returns {string}
 */
function _shortOrigin(origin) {
    let s = String(origin);
    // Strip directory, keep the filename / source label.
    const slash = s.lastIndexOf('/');
    if (slash >= 0) s = s.slice(slash + 1);
    return s.length > 40 ? s.slice(0, 39) + '…' : s;
}

/**
 * Build a single collapsible accordion section for a config group.
 * @param {string}  prefix      Group prefix name
 * @param {object}  props       Key-value properties for this group
 * @param {boolean} startExpanded
 * @returns {HTMLElement}
 */
function buildAccordionSection(prefix, props, startExpanded) {
    const card = document.createElement('div');
    card.className = 'admin-card';
    card.style.marginBottom = '12px';

    // Header
    const header = document.createElement('div');
    header.className = 'collapsible-header';
    if (startExpanded) {
        header.classList.add('expanded');
    }

    header.appendChild(createChevron());

    const nameEl = document.createElement('span');
    nameEl.style.fontWeight = '600';
    nameEl.style.flex = '1';
    nameEl.className = 'text-mono';
    nameEl.textContent = prefix;
    header.appendChild(nameEl);

    const countBadge = document.createElement('span');
    countBadge.className = 'badge badge-neutral';
    const propCount = Object.keys(props).length;
    countBadge.textContent = propCount + (propCount === 1 ? ' property' : ' properties');
    header.appendChild(countBadge);

    card.appendChild(header);

    // Collapsible content
    const content = document.createElement('div');
    content.className = 'collapsible-content';
    if (startExpanded) {
        content.classList.add('expanded');
    }

    const contentInner = document.createElement('div');
    contentInner.style.padding = '0 16px 16px';

    contentInner.appendChild(buildGroupTable(props));

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

    return card;
}

/**
 * Filter accordion sections by search term.
 *
 * If a group name matches the search, all its properties are shown.
 * If a property key or value matches, that group is shown with only
 * matching rows visible. Matching sections are auto-expanded;
 * non-matching sections are collapsed and hidden.
 *
 * @param {Array<{element: HTMLElement, prefix: string, props: object}>} sections
 * @param {string} search  Lowercase search term
 * @returns {number} Number of matching properties across all visible groups
 */
function filterAccordions(sections, search) {
    if (!search) {
        // No filter — show all sections, collapse all except the first
        let matchingProps = 0;
        for (let i = 0; i < sections.length; i++) {
            const sec = sections[i];
            sec.element.style.display = '';
            matchingProps += Object.keys(sec.props).length;

            // Reset rows visibility
            const rows = sec.element.querySelectorAll('.kv-table tbody tr');
            for (const row of rows) {
                row.style.display = '';
            }

            // Restore first-expanded state
            const header = sec.element.querySelector('.collapsible-header');
            const content = sec.element.querySelector('.collapsible-content');
            if (i === 0) {
                header.classList.add('expanded');
                content.classList.add('expanded');
            } else {
                header.classList.remove('expanded');
                content.classList.remove('expanded');
            }
        }
        return matchingProps;
    }

    let totalMatchingProps = 0;

    for (const sec of sections) {
        const groupNameMatches = sec.prefix.toLowerCase().includes(search);
        let sectionHasMatch = groupNameMatches;
        let matchingPropsInGroup = 0;

        // Check each property row
        const rows = sec.element.querySelectorAll('.kv-table tbody tr');
        for (const row of rows) {
            const th = row.querySelector('th');
            const td = row.querySelector('td');
            const keyText = th ? th.textContent.toLowerCase() : '';
            const valText = td ? td.textContent.toLowerCase() : '';

            if (groupNameMatches || keyText.includes(search) || valText.includes(search)) {
                row.style.display = '';
                matchingPropsInGroup++;
                sectionHasMatch = true;
            } else {
                row.style.display = 'none';
            }
        }

        if (sectionHasMatch) {
            sec.element.style.display = '';
            totalMatchingProps += matchingPropsInGroup;

            // Auto-expand matching sections
            const header = sec.element.querySelector('.collapsible-header');
            const content = sec.element.querySelector('.collapsible-content');
            header.classList.add('expanded');
            content.classList.add('expanded');
        } else {
            sec.element.style.display = 'none';

            // Collapse hidden sections
            const header = sec.element.querySelector('.collapsible-header');
            const content = sec.element.querySelector('.collapsible-content');
            header.classList.remove('expanded');
            content.classList.remove('expanded');
        }
    }

    return totalMatchingProps;
}

/* ── Render ───────────────────────────────────────────────────── */

/**
 * Render the configuration view.
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
    h1.textContent = 'Configuration';
    headerLeft.appendChild(h1);
    const sub = document.createElement('div');
    sub.className = 'page-subtitle';
    sub.textContent = 'application.config';
    headerLeft.appendChild(sub);
    header.appendChild(headerLeft);
    wrapper.appendChild(header);

    // Loading skeleton (two stat cards + property rows)
    const loader = document.createElement('div');
    loader.appendChild(pageSkeleton({ stats: 2, rows: 6 }));
    wrapper.appendChild(loader);
    container.appendChild(wrapper);

    // Fetch config data
    let configData;
    try {
        configData = await api.get('/config');
    } catch (err) {
        wrapper.removeChild(loader);
        wrapper.appendChild(createEmptyStateCard({
            icon: 'alert',
            tone: 'danger',
            title: 'Failed to load configuration',
            text: err.message,
        }));
        return;
    }

    wrapper.removeChild(loader);

    const groups = configData.groups || {};
    const groupNames = Object.keys(groups);

    if (groupNames.length === 0) {
        wrapper.appendChild(createEmptyStateCard({
            icon: 'inbox',
            title: 'No configuration groups found',
            text: 'No application configuration properties were exposed by this service.',
        }));
        return;
    }

    // ── Count total properties ──────────────────────────────────
    let totalProps = 0;
    for (const name of groupNames) {
        totalProps += Object.keys(groups[name]).length;
    }

    // ── Build accordion sections (store references for filtering) ──
    const accordionSections = [];
    const accordionContainer = document.createElement('div');

    for (let i = 0; i < groupNames.length; i++) {
        const name = groupNames[i];
        const props = groups[name];
        const section = buildAccordionSection(name, props, i === 0);
        accordionSections.push({ element: section, prefix: name, props });
        accordionContainer.appendChild(section);
    }

    // ── Filter toolbar ──────────────────────────────────────────
    const toolbar = createFilterToolbar({
        placeholder: 'Search configuration...',
        pills: [],
        onFilter: ({ search }) => {
            const matchCount = filterAccordions(accordionSections, search);
            toolbar.updateCount(matchCount, totalProps);
        },
        totalCount: totalProps,
    });

    wrapper.appendChild(toolbar);

    // ── Stat row ─────────────────────────────────────────────
    const statsRow = document.createElement('div');
    statsRow.className = 'grid-3 mb-lg';

    // Total groups
    const totalGroupCard = document.createElement('div');
    totalGroupCard.className = 'stat-card';
    const totalContent = document.createElement('div');
    totalContent.className = 'stat-card-content';
    const totalVal = document.createElement('div');
    totalVal.className = 'stat-card-value';
    totalVal.textContent = String(groupNames.length);
    totalContent.appendChild(totalVal);
    const totalLabel = document.createElement('div');
    totalLabel.className = 'stat-card-label';
    totalLabel.textContent = 'Config Groups';
    totalContent.appendChild(totalLabel);
    totalGroupCard.appendChild(totalContent);
    statsRow.appendChild(totalGroupCard);

    // Total properties
    const totalPropsCard = document.createElement('div');
    totalPropsCard.className = 'stat-card';
    const propsContent = document.createElement('div');
    propsContent.className = 'stat-card-content';
    const propsVal = document.createElement('div');
    propsVal.className = 'stat-card-value';
    propsVal.textContent = String(totalProps);
    propsContent.appendChild(propsVal);
    const propsLabel = document.createElement('div');
    propsLabel.className = 'stat-card-label';
    propsLabel.textContent = 'Total Properties';
    propsContent.appendChild(propsLabel);
    totalPropsCard.appendChild(propsContent);
    statsRow.appendChild(totalPropsCard);

    wrapper.appendChild(statsRow);

    // ── Accordion sections ───────────────────────────────────
    wrapper.appendChild(accordionContainer);
}
