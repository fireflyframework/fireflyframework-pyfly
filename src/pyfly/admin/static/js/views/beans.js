/**
 * PyFly Admin — Beans View.
 *
 * Filterable, sortable bean explorer with a slide-in detail panel.
 * Uses the reusable table component with custom cell renderers
 * for stereotype badges and scope labels.
 *
 * Data sources:
 *   GET /admin/api/beans          -> { beans: [...], total: N }
 *   GET /admin/api/beans/{name}   -> bean detail object
 */

import { createEmptyStateCard } from '../components/empty-state.js';
import { pageSkeleton } from '../components/skeleton.js';
import { createTable } from '../components/table.js';

/* ── Helpers ──────────────────────────────────────────────────── */

/** Map stereotype names to badge CSS classes. */
const STEREOTYPE_CLASSES = {
    service:      'badge-info',
    repository:   'badge-success',
    controller:   'badge-get',
    component:    'badge-neutral',
    configuration:'badge-warning',
    filter:       'badge-patch',
};

/**
 * Create a small coloured badge for a stereotype value.
 * @param {string} stereotype
 * @returns {HTMLSpanElement}
 */
function createStereotypeBadge(stereotype) {
    const badge = document.createElement('span');
    const norm = (stereotype || '').toLowerCase();
    const cls = STEREOTYPE_CLASSES[norm] || 'badge-neutral';
    badge.className = `badge ${cls}`;
    badge.textContent = stereotype || '--';
    return badge;
}

/**
 * Truncate a module path, keeping the last two segments.
 * e.g. "myapp.services.user_service.UserService" -> "...user_service.UserService"
 * @param {string} typePath
 * @returns {string}
 */
function truncateType(typePath) {
    if (!typePath) return '--';
    const parts = typePath.split('.');
    if (parts.length <= 2) return typePath;
    return '...' + parts.slice(-2).join('.');
}

/**
 * Create a checkmark or X icon element.
 * @param {boolean} yes
 * @returns {HTMLSpanElement}
 */
function createCheckIcon(yes) {
    const span = document.createElement('span');
    span.style.fontWeight = '600';
    if (yes) {
        span.textContent = '\u2713';
        span.style.color = 'var(--admin-success)';
    } else {
        span.textContent = '\u2717';
        span.style.color = 'var(--admin-danger)';
    }
    return span;
}

/**
 * Format a creation time value as "X.Xms" or "--".
 * @param {number|null} ms
 * @returns {string}
 */
function formatCreationTime(ms) {
    if (ms == null) return '--';
    return ms.toFixed(1) + 'ms';
}

/**
 * Build a key-value table from an object, skipping null/undefined entries.
 * @param {Object<string, *>} data
 * @returns {HTMLTableElement}
 */
function buildKvTable(data) {
    const table = document.createElement('table');
    table.className = 'kv-table';
    const tbody = document.createElement('tbody');
    for (const [key, val] of Object.entries(data)) {
        if (val == null) continue;
        const tr = document.createElement('tr');
        const th = document.createElement('th');
        th.textContent = key;
        const td = document.createElement('td');
        if (val instanceof Node) {
            td.appendChild(val);
        } else if (Array.isArray(val)) {
            td.textContent = val.length > 0 ? val.join(', ') : '--';
        } else {
            td.textContent = String(val);
        }
        tr.appendChild(th);
        tr.appendChild(td);
        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    return table;
}

/**
 * Create a section header element with consistent styling.
 * @param {string} text
 * @returns {HTMLHeadingElement}
 */
function createSectionHeader(text) {
    const header = document.createElement('h4');
    header.textContent = text;
    header.style.marginBottom = '8px';
    header.style.fontSize = '0.85rem';
    header.style.fontWeight = '600';
    return header;
}

/**
 * Recursively render a dependency chain tree.
 * @param {Array<{name: string, type: string, dependencies: Array}>} chain
 * @param {number} [depth=0]
 * @returns {HTMLElement}
 */
function buildDependencyTree(chain, depth) {
    if (depth === undefined) depth = 0;
    const list = document.createElement('ul');
    list.style.listStyle = 'none';
    list.style.paddingLeft = depth === 0 ? '0' : '16px';
    list.style.margin = '0';

    for (const dep of chain) {
        const li = document.createElement('li');
        li.style.padding = '4px 0';

        const label = document.createElement('span');
        label.className = 'mono text-sm';
        label.textContent = dep.name;

        const typeSpan = document.createElement('span');
        typeSpan.className = 'text-sm';
        typeSpan.style.color = 'var(--admin-text-muted)';
        typeSpan.style.marginLeft = '6px';
        typeSpan.textContent = ': ' + dep.type;

        li.appendChild(label);
        li.appendChild(typeSpan);

        if (dep.dependencies && dep.dependencies.length > 0) {
            li.appendChild(buildDependencyTree(dep.dependencies, depth + 1));
        }

        list.appendChild(li);
    }

    return list;
}

/* ── Detail Panel ─────────────────────────────────────────────── */

/**
 * Create the detail panel overlay + panel structure (initially hidden).
 * Returns { overlay, panel, show(beanDetail), hide() }.
 */
function createDetailPanel() {
    // Overlay
    const overlay = document.createElement('div');
    overlay.className = 'detail-panel-overlay';

    // Panel
    const panel = document.createElement('div');
    panel.className = 'detail-panel';

    // Header
    const panelHeader = document.createElement('div');
    panelHeader.className = 'detail-panel-header';
    const panelTitle = document.createElement('h3');
    panelTitle.textContent = 'Bean Detail';
    panelHeader.appendChild(panelTitle);

    const closeBtn = document.createElement('button');
    closeBtn.className = 'detail-panel-close';
    closeBtn.setAttribute('aria-label', 'Close detail panel');
    // X icon via SVG
    const svgNS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    const line1 = document.createElementNS(svgNS, 'line');
    line1.setAttribute('x1', '18');
    line1.setAttribute('y1', '6');
    line1.setAttribute('x2', '6');
    line1.setAttribute('y2', '18');
    svg.appendChild(line1);
    const line2 = document.createElementNS(svgNS, 'line');
    line2.setAttribute('x1', '6');
    line2.setAttribute('y1', '6');
    line2.setAttribute('x2', '18');
    line2.setAttribute('y2', '18');
    svg.appendChild(line2);
    closeBtn.appendChild(svg);
    panelHeader.appendChild(closeBtn);
    panel.appendChild(panelHeader);

    // Body
    const panelBody = document.createElement('div');
    panelBody.className = 'detail-panel-body';
    panel.appendChild(panelBody);

    // Close handlers
    function hide() {
        overlay.classList.remove('open');
        panel.classList.remove('open');
    }

    closeBtn.addEventListener('click', hide);
    overlay.addEventListener('click', hide);

    /**
     * Populate and show the panel with bean detail data.
     * @param {object} bean
     */
    function show(bean) {
        panelBody.replaceChildren();
        panelTitle.textContent = bean.name || 'Bean Detail';

        // Basic info
        const basicSection = document.createElement('div');
        basicSection.className = 'mb-lg';
        basicSection.appendChild(createSectionHeader('Basic Information'));

        const basicData = {
            'Name': bean.name,
            'Type': bean.type,
            'Module': bean.module,
            'File': bean.file_path || bean.file,
            'Scope': bean.scope,
            'Stereotype': bean.stereotype ? createStereotypeBadge(bean.stereotype) : null,
            'Initialized': bean.initialized != null ? (bean.initialized ? 'Yes' : 'No') : null,
            'Creation Time': formatCreationTime(bean.creation_time_ms),
            'Resolutions': bean.resolution_count != null ? String(bean.resolution_count) : '0',
        };
        basicSection.appendChild(buildKvTable(basicData));
        panelBody.appendChild(basicSection);

        // Docstring (detail returns "doc" field, not "docstring")
        const docText = bean.docstring || bean.doc;
        if (docText) {
            const docSection = document.createElement('div');
            docSection.className = 'mb-lg';
            docSection.appendChild(createSectionHeader('Documentation'));
            const docBlock = document.createElement('div');
            docBlock.className = 'code-block';
            docBlock.textContent = docText;
            docSection.appendChild(docBlock);
            panelBody.appendChild(docSection);
        }

        // Configuration Source
        if (bean.bean_method_origin) {
            const originSection = document.createElement('div');
            originSection.className = 'mb-lg';
            originSection.appendChild(createSectionHeader('Configuration Source'));
            const originBlock = document.createElement('div');
            originBlock.className = 'mono text-sm';
            originBlock.style.padding = '6px 10px';
            originBlock.style.background = 'var(--admin-bg)';
            originBlock.style.borderRadius = 'var(--admin-radius-sm)';
            originBlock.style.border = '1px solid var(--admin-border-subtle)';
            originBlock.textContent = bean.bean_method_origin;
            originSection.appendChild(originBlock);
            panelBody.appendChild(originSection);
        }

        // Conditions with pass/fail badges
        const conditions = bean.conditions || [];
        if (conditions.length > 0 && typeof conditions[0] === 'object') {
            const condSection = document.createElement('div');
            condSection.className = 'mb-lg';
            condSection.appendChild(createSectionHeader('Conditions'));

            const condTable = document.createElement('table');
            condTable.className = 'admin-table';
            condTable.style.fontSize = '0.8rem';
            const condThead = document.createElement('thead');
            const condHeadRow = document.createElement('tr');
            for (const hdr of ['Type', 'Value', 'Status']) {
                const th = document.createElement('th');
                th.textContent = hdr;
                condHeadRow.appendChild(th);
            }
            condThead.appendChild(condHeadRow);
            condTable.appendChild(condThead);

            const condTbody = document.createElement('tbody');
            for (const cond of conditions) {
                const tr = document.createElement('tr');

                const tdType = document.createElement('td');
                tdType.className = 'mono text-sm';
                tdType.textContent = cond.type || 'unknown';
                tr.appendChild(tdType);

                const tdVal = document.createElement('td');
                tdVal.className = 'mono text-sm';
                // Show the most descriptive value available
                tdVal.textContent = cond.value || cond.name || cond.class_name || '--';
                tr.appendChild(tdVal);

                const tdStatus = document.createElement('td');
                const statusBadge = document.createElement('span');
                if (cond.passed) {
                    statusBadge.className = 'badge badge-success';
                    statusBadge.textContent = 'pass';
                } else {
                    statusBadge.className = 'badge badge-danger';
                    statusBadge.textContent = 'fail';
                }
                tdStatus.appendChild(statusBadge);
                tr.appendChild(tdStatus);

                condTbody.appendChild(tr);
            }
            condTable.appendChild(condTbody);

            const condTableWrap = document.createElement('div');
            condTableWrap.className = 'admin-table-wrapper';
            condTableWrap.appendChild(condTable);
            condSection.appendChild(condTableWrap);
            panelBody.appendChild(condSection);
        }

        // Lifecycle Methods
        const postConstruct = bean.post_construct || [];
        const preDestroy = bean.pre_destroy || [];
        if (postConstruct.length > 0 || preDestroy.length > 0) {
            const lcSection = document.createElement('div');
            lcSection.className = 'mb-lg';
            lcSection.appendChild(createSectionHeader('Lifecycle Methods'));

            const lcList = document.createElement('div');
            lcList.className = 'flex flex-col gap-sm';

            for (const methodName of postConstruct) {
                const item = document.createElement('div');
                item.className = 'mono text-sm';
                item.style.padding = '6px 10px';
                item.style.background = 'var(--admin-bg)';
                item.style.borderRadius = 'var(--admin-radius-sm)';
                item.style.border = '1px solid var(--admin-border-subtle)';

                const decorator = document.createElement('span');
                decorator.style.color = 'var(--admin-success)';
                decorator.textContent = '@post_construct ';
                item.appendChild(decorator);

                const name = document.createTextNode(methodName);
                item.appendChild(name);
                lcList.appendChild(item);
            }

            for (const methodName of preDestroy) {
                const item = document.createElement('div');
                item.className = 'mono text-sm';
                item.style.padding = '6px 10px';
                item.style.background = 'var(--admin-bg)';
                item.style.borderRadius = 'var(--admin-radius-sm)';
                item.style.border = '1px solid var(--admin-border-subtle)';

                const decorator = document.createElement('span');
                decorator.style.color = 'var(--admin-danger)';
                decorator.textContent = '@pre_destroy ';
                item.appendChild(decorator);

                const name = document.createTextNode(methodName);
                item.appendChild(name);
                lcList.appendChild(item);
            }

            lcSection.appendChild(lcList);
            panelBody.appendChild(lcSection);
        }

        // Dependency Chain (recursive tree)
        const chain = bean.dependency_chain || [];
        if (chain.length > 0) {
            const chainSection = document.createElement('div');
            chainSection.className = 'mb-lg';
            chainSection.appendChild(createSectionHeader('Dependency Chain'));

            const treeWrap = document.createElement('div');
            treeWrap.style.padding = '8px 10px';
            treeWrap.style.background = 'var(--admin-bg)';
            treeWrap.style.borderRadius = 'var(--admin-radius-sm)';
            treeWrap.style.border = '1px solid var(--admin-border-subtle)';
            treeWrap.appendChild(buildDependencyTree(chain));
            chainSection.appendChild(treeWrap);
            panelBody.appendChild(chainSection);
        }

        // Autowired Fields
        const autowired = bean.autowired_fields || [];
        if (autowired.length > 0) {
            const awSection = document.createElement('div');
            awSection.className = 'mb-lg';
            awSection.appendChild(createSectionHeader('Autowired Fields'));

            const awList = document.createElement('div');
            awList.className = 'flex flex-col gap-sm';

            for (const field of autowired) {
                const item = document.createElement('div');
                item.className = 'mono text-sm';
                item.style.padding = '6px 10px';
                item.style.background = 'var(--admin-bg)';
                item.style.borderRadius = 'var(--admin-radius-sm)';
                item.style.border = '1px solid var(--admin-border-subtle)';
                item.style.display = 'flex';
                item.style.alignItems = 'center';
                item.style.gap = '8px';

                const nameSpan = document.createElement('span');
                nameSpan.textContent = field.name;
                item.appendChild(nameSpan);

                if (field.qualifier) {
                    const qualBadge = document.createElement('span');
                    qualBadge.className = 'badge badge-info';
                    qualBadge.textContent = field.qualifier;
                    item.appendChild(qualBadge);
                }

                const reqBadge = document.createElement('span');
                if (field.required) {
                    reqBadge.className = 'badge badge-warning';
                    reqBadge.textContent = 'required';
                } else {
                    reqBadge.className = 'badge badge-neutral';
                    reqBadge.textContent = 'optional';
                }
                item.appendChild(reqBadge);

                awList.appendChild(item);
            }

            awSection.appendChild(awList);
            panelBody.appendChild(awSection);
        }

        // Dependencies (flat list, shown when no dependency_chain is available)
        if (chain.length === 0) {
            const deps = bean.dependencies || [];
            if (deps.length > 0) {
                const depSection = document.createElement('div');
                depSection.className = 'mb-lg';
                depSection.appendChild(createSectionHeader('Dependencies'));
                const depList = document.createElement('div');
                depList.className = 'flex flex-col gap-sm';
                for (const dep of deps) {
                    const depItem = document.createElement('div');
                    depItem.className = 'text-mono text-sm';
                    depItem.style.padding = '6px 10px';
                    depItem.style.background = 'var(--admin-bg)';
                    depItem.style.borderRadius = 'var(--admin-radius-sm)';
                    depItem.style.border = '1px solid var(--admin-border-subtle)';
                    depItem.textContent = typeof dep === 'string' ? dep : (dep.name || String(dep));
                    depList.appendChild(depItem);
                }
                depSection.appendChild(depList);
                panelBody.appendChild(depSection);
            }
        }

        // Metadata (primary, order, profile)
        const meta = {};
        if (bean.primary != null) meta['Primary'] = String(bean.primary);
        if (bean.order != null) meta['Order'] = String(bean.order);
        if (bean.profile) meta['Profile'] = Array.isArray(bean.profile) ? bean.profile.join(', ') : bean.profile;

        if (Object.keys(meta).length > 0) {
            const metaSection = document.createElement('div');
            metaSection.className = 'mb-lg';
            metaSection.appendChild(createSectionHeader('Metadata'));
            metaSection.appendChild(buildKvTable(meta));
            panelBody.appendChild(metaSection);
        }

        // Show panel
        overlay.classList.add('open');
        panel.classList.add('open');
    }

    return { overlay, panel, show, hide };
}

/* ── Render ───────────────────────────────────────────────────── */

/**
 * Render the beans explorer view.
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
    h1.textContent = 'Beans';
    headerLeft.appendChild(h1);
    const sub = document.createElement('div');
    sub.className = 'page-subtitle';
    sub.textContent = 'application.beans';
    headerLeft.appendChild(sub);
    header.appendChild(headerLeft);

    // List / Graph toggle
    const headerRight = document.createElement('div');
    const toggle = document.createElement('div');
    toggle.className = 'btn-group';

    const listBtn = document.createElement('button');
    listBtn.className = 'btn btn-sm btn-primary';
    listBtn.textContent = 'List';

    const graphBtn = document.createElement('button');
    graphBtn.className = 'btn btn-sm btn-default';
    graphBtn.textContent = 'Graph';

    listBtn.addEventListener('click', () => {
        listBtn.className = 'btn btn-sm btn-primary';
        graphBtn.className = 'btn btn-sm btn-default';
    });
    graphBtn.addEventListener('click', () => {
        window.location.hash = 'bean-graph';
    });

    toggle.appendChild(listBtn);
    toggle.appendChild(graphBtn);
    headerRight.appendChild(toggle);
    header.appendChild(headerRight);

    wrapper.appendChild(header);

    // Loading skeleton (stat cards + a table)
    const loader = document.createElement('div');
    loader.appendChild(pageSkeleton({ stats: 4, rows: 8 }));
    wrapper.appendChild(loader);
    container.appendChild(wrapper);

    // Fetch beans
    let data;
    try {
        data = await api.get('/beans');
    } catch (err) {
        wrapper.removeChild(loader);
        wrapper.appendChild(createEmptyStateCard({
            icon: 'alert',
            tone: 'danger',
            title: 'Failed to load beans',
            text: err.message,
        }));
        return;
    }

    wrapper.removeChild(loader);

    const beansList = data.beans || [];
    const total = data.total != null ? data.total : beansList.length;

    // ── Stat cards row ────────────────────────────────────────
    const statsRow = document.createElement('div');
    statsRow.className = 'grid-4 mb-lg';

    // Total beans
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
    totalLabel.textContent = 'Total Beans';
    totalContent.appendChild(totalLabel);
    totalCard.appendChild(totalContent);
    statsRow.appendChild(totalCard);

    // Stereotype breakdown mini badges
    const stereotypeCounts = {};
    for (const bean of beansList) {
        const st = bean.stereotype || 'other';
        stereotypeCounts[st] = (stereotypeCounts[st] || 0) + 1;
    }

    // Show up to 3 top stereotypes as stat cards
    const sortedStereotypes = Object.entries(stereotypeCounts)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 3);

    const iconClasses = ['primary', 'info', 'success'];
    for (let i = 0; i < sortedStereotypes.length; i++) {
        const [stereo, count] = sortedStereotypes[i];
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
        cardLabel.textContent = stereo;
        cardContent.appendChild(cardLabel);
        card.appendChild(cardContent);
        const cardIcon = document.createElement('div');
        cardIcon.className = `stat-card-icon ${iconClasses[i] || 'primary'}`;
        card.appendChild(cardIcon);
        statsRow.appendChild(card);
    }

    wrapper.appendChild(statsRow);

    // ── Detail panel ──────────────────────────────────────────
    const detailPanel = createDetailPanel();
    document.body.appendChild(detailPanel.overlay);
    document.body.appendChild(detailPanel.panel);

    // ── Table ─────────────────────────────────────────────────
    const tableCard = document.createElement('div');
    tableCard.className = 'admin-card';

    const tableHeader = document.createElement('div');
    tableHeader.className = 'admin-card-header';
    const tableTitle = document.createElement('h3');
    tableTitle.textContent = 'Registered Beans';
    tableHeader.appendChild(tableTitle);
    tableCard.appendChild(tableHeader);

    const tableBody = document.createElement('div');
    tableBody.className = 'admin-card-body';
    tableBody.style.padding = '0';

    const tableEl = createTable({
        columns: [
            {
                key: 'name',
                label: 'Name',
                render(val) {
                    const span = document.createElement('span');
                    span.className = 'mono';
                    span.textContent = val || '--';
                    return span;
                },
            },
            {
                key: 'type',
                label: 'Type',
                render(val) {
                    const span = document.createElement('span');
                    span.className = 'mono truncate';
                    span.style.maxWidth = '240px';
                    span.style.display = 'inline-block';
                    span.title = val || '';
                    span.textContent = truncateType(val);
                    return span;
                },
            },
            {
                key: 'scope',
                label: 'Scope',
                render(val) {
                    const span = document.createElement('span');
                    span.className = 'badge badge-neutral';
                    span.textContent = val || 'singleton';
                    return span;
                },
            },
            {
                key: 'stereotype',
                label: 'Stereotype',
                render(val) {
                    return createStereotypeBadge(val);
                },
            },
            {
                key: 'initialized',
                label: 'Initialized',
                render(val) {
                    return createCheckIcon(!!val);
                },
            },
            {
                key: 'creation_time_ms',
                label: 'Creation Time',
                render(val) {
                    const span = document.createElement('span');
                    span.className = 'mono text-sm';
                    span.textContent = formatCreationTime(val);
                    return span;
                },
            },
            {
                key: 'resolution_count',
                label: 'Resolutions',
                render(val) {
                    const span = document.createElement('span');
                    span.className = 'mono text-sm';
                    span.textContent = val != null ? String(val) : '0';
                    return span;
                },
            },
        ],
        data: beansList,
        searchable: true,
        sortable: true,
        emptyText: 'No beans found',
        onRowClick: async (row) => {
            // Fetch full detail for this bean
            try {
                const detail = await api.get(`/beans/${encodeURIComponent(row.name)}`);
                detailPanel.show(detail);
            } catch (_err) {
                // Fall back to the row data we already have
                detailPanel.show(row);
            }
        },
    });

    tableBody.appendChild(tableEl);
    tableCard.appendChild(tableBody);
    wrapper.appendChild(tableCard);

    // Cleanup: remove detail panel elements from body on navigation
    return function cleanup() {
        detailPanel.hide();
        if (detailPanel.overlay.parentNode) {
            detailPanel.overlay.parentNode.removeChild(detailPanel.overlay);
        }
        if (detailPanel.panel.parentNode) {
            detailPanel.panel.parentNode.removeChild(detailPanel.panel);
        }
    };
}
