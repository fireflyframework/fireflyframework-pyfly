/**
 * PyFly Admin — Bean Dependency Graph View.
 *
 * D3 force-directed graph showing bean dependencies.
 * Nodes colored by stereotype, sized by resolution count.
 * Data source: GET /admin/api/beans/graph
 *
 * Features:
 *   - Real-time search filtering by bean name
 *   - Stereotype toggle pills (composable with search)
 *   - Click-to-zoom with dependency highlighting
 *   - Slide-in detail panel (fetches full bean data)
 *   - Enhanced tooltip with scope, timing, and dependency counts
 *   - Stats bar showing totals and connected components
 */

/* global d3 */

import { createEmptyStateCard } from '../components/empty-state.js';
import { attachFullscreen } from '../components/fullscreen.js';
import { skeletonCard } from '../components/skeleton.js';

const STEREOTYPE_COLORS = {
    service:         '#3b82f6',
    controller:      '#8b5cf6',
    rest_controller: '#a78bfa',
    repository:      '#10b981',
    component:       '#f59e0b',
    configuration:   '#06b6d4',
    none:            '#64748b',
};

/* ── Helpers ─────────────────────────────────────────────────── */

/**
 * Count connected components using BFS over the undirected edge set.
 * @param {Array<{id: string}>} nodes
 * @param {Array<{source: string|object, target: string|object}>} edges
 * @returns {number}
 */
function _countConnectedComponents(nodes, edges) {
    const adj = new Map();
    for (const n of nodes) {
        adj.set(n.id, []);
    }
    for (const e of edges) {
        const src = typeof e.source === 'object' ? e.source.id : e.source;
        const tgt = typeof e.target === 'object' ? e.target.id : e.target;
        if (adj.has(src)) adj.get(src).push(tgt);
        if (adj.has(tgt)) adj.get(tgt).push(src);
    }
    const visited = new Set();
    let components = 0;
    for (const n of nodes) {
        if (visited.has(n.id)) continue;
        components++;
        const queue = [n.id];
        visited.add(n.id);
        while (queue.length > 0) {
            const cur = queue.shift();
            for (const neighbor of (adj.get(cur) || [])) {
                if (!visited.has(neighbor)) {
                    visited.add(neighbor);
                    queue.push(neighbor);
                }
            }
        }
    }
    return components;
}

/**
 * Build a key-value table from an entries array, skipping null/undefined values.
 * @param {Array<[string, *]>} entries
 * @returns {HTMLTableElement}
 */
function _buildKvTable(entries) {
    const table = document.createElement('table');
    table.className = 'kv-table';
    const tbody = document.createElement('tbody');
    for (const [key, val] of entries) {
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
function _createSectionHeader(text) {
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
function _buildDependencyTree(chain, depth) {
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
            li.appendChild(_buildDependencyTree(dep.dependencies, depth + 1));
        }

        list.appendChild(li);
    }

    return list;
}

/**
 * Create an SVG close icon (X) in the given namespace.
 * @returns {SVGSVGElement}
 */
function _createCloseIconSvg() {
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
    return svg;
}

/**
 * Create a search icon SVG for the filter toolbar.
 * @returns {SVGSVGElement}
 */
function _createSearchIconSvg() {
    const svgNS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    const circle = document.createElementNS(svgNS, 'circle');
    circle.setAttribute('cx', '11');
    circle.setAttribute('cy', '11');
    circle.setAttribute('r', '8');
    svg.appendChild(circle);
    const line = document.createElementNS(svgNS, 'line');
    line.setAttribute('x1', '21');
    line.setAttribute('y1', '21');
    line.setAttribute('x2', '16.65');
    line.setAttribute('y2', '16.65');
    svg.appendChild(line);
    return svg;
}

/* ── Detail Panel ────────────────────────────────────────────── */

/**
 * Create the detail panel overlay + panel structure (initially hidden).
 * Returns { overlay, panel, show(bean), hide(), destroy() }.
 */
function _createDetailPanel(api) {
    const overlay = document.createElement('div');
    overlay.className = 'detail-panel-overlay';

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
    closeBtn.appendChild(_createCloseIconSvg());
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

    function _onKeyDown(e) {
        if (e.key === 'Escape') hide();
    }
    document.addEventListener('keydown', _onKeyDown);

    /**
     * Populate and show the panel for a given bean name.
     * Fetches detail from the API; falls back to graph node data.
     * @param {object} nodeData  - the graph node datum
     */
    async function show(nodeData) {
        panelBody.replaceChildren();
        panelTitle.textContent = nodeData.name || 'Bean Detail';

        // Show a loading spinner while fetching
        const loader = document.createElement('div');
        loader.className = 'loading-spinner';
        panelBody.appendChild(loader);

        overlay.classList.add('open');
        panel.classList.add('open');

        let bean;
        try {
            bean = await api.get('/beans/' + encodeURIComponent(nodeData.name));
        } catch (_err) {
            // Fall back to the node data we have from the graph
            bean = nodeData;
        }

        panelBody.replaceChildren();
        panelTitle.textContent = bean.name || 'Bean Detail';

        // Basic information
        const basicSection = document.createElement('div');
        basicSection.className = 'mb-lg';
        basicSection.appendChild(_createSectionHeader('Basic Information'));

        const stereotypeBadge = document.createElement('span');
        stereotypeBadge.className = 'badge badge-info';
        stereotypeBadge.textContent = bean.stereotype || '--';

        basicSection.appendChild(_buildKvTable([
            ['Name', bean.name],
            ['Type', bean.type],
            ['Scope', bean.scope],
            ['Stereotype', stereotypeBadge],
            ['Module', bean.module],
            ['File', bean.file_path || bean.file],
            ['Initialized', bean.initialized != null ? (bean.initialized ? 'Yes' : 'No') : null],
            ['Creation Time', bean.creation_time_ms != null ? bean.creation_time_ms.toFixed(1) + 'ms' : null],
            ['Resolutions', bean.resolution_count != null ? String(bean.resolution_count) : '0'],
        ]));
        panelBody.appendChild(basicSection);

        // Docstring
        const docText = bean.docstring || bean.doc;
        if (docText) {
            const docSection = document.createElement('div');
            docSection.className = 'mb-lg';
            docSection.appendChild(_createSectionHeader('Documentation'));
            const docBlock = document.createElement('div');
            docBlock.className = 'code-block';
            docBlock.textContent = docText;
            docSection.appendChild(docBlock);
            panelBody.appendChild(docSection);
        }

        // Conditions
        const conditions = bean.conditions || [];
        if (conditions.length > 0) {
            const condSection = document.createElement('div');
            condSection.className = 'mb-lg';
            condSection.appendChild(_createSectionHeader('Conditions'));
            if (typeof conditions[0] === 'object') {
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
                    tdVal.textContent = cond.value || cond.name || cond.class_name || '--';
                    tr.appendChild(tdVal);
                    const tdStatus = document.createElement('td');
                    const statusBadge = document.createElement('span');
                    statusBadge.className = cond.passed ? 'badge badge-success' : 'badge badge-danger';
                    statusBadge.textContent = cond.passed ? 'pass' : 'fail';
                    tdStatus.appendChild(statusBadge);
                    tr.appendChild(tdStatus);
                    condTbody.appendChild(tr);
                }
                condTable.appendChild(condTbody);
                const condTableWrap = document.createElement('div');
                condTableWrap.className = 'admin-table-wrapper';
                condTableWrap.appendChild(condTable);
                condSection.appendChild(condTableWrap);
            } else {
                // Simple string array of conditions
                const condList = document.createElement('div');
                condList.className = 'code-block';
                condList.textContent = conditions.join('\n');
                condSection.appendChild(condList);
            }
            panelBody.appendChild(condSection);
        }

        // Dependency Chain (recursive tree)
        const chain = bean.dependency_chain || [];
        if (chain.length > 0) {
            const chainSection = document.createElement('div');
            chainSection.className = 'mb-lg';
            chainSection.appendChild(_createSectionHeader('Dependency Chain'));
            const treeWrap = document.createElement('div');
            treeWrap.style.padding = '8px 10px';
            treeWrap.style.background = 'var(--admin-bg)';
            treeWrap.style.borderRadius = 'var(--admin-radius-sm)';
            treeWrap.style.border = '1px solid var(--admin-border-subtle)';
            treeWrap.appendChild(_buildDependencyTree(chain));
            chainSection.appendChild(treeWrap);
            panelBody.appendChild(chainSection);
        }

        // Dependencies (flat list, when no dependency_chain)
        if (chain.length === 0) {
            const deps = bean.dependencies || [];
            if (deps.length > 0) {
                const depSection = document.createElement('div');
                depSection.className = 'mb-lg';
                depSection.appendChild(_createSectionHeader('Dependencies'));
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

        // Autowired fields
        const autowired = bean.autowired_fields || bean.autowired || [];
        if (autowired.length > 0) {
            const awSection = document.createElement('div');
            awSection.className = 'mb-lg';
            awSection.appendChild(_createSectionHeader('Autowired Fields'));
            const awList = document.createElement('div');
            awList.className = 'flex flex-col gap-sm';
            for (const field of autowired) {
                const item = document.createElement('div');
                item.className = 'mono text-sm';
                item.style.padding = '6px 10px';
                item.style.background = 'var(--admin-bg)';
                item.style.borderRadius = 'var(--admin-radius-sm)';
                item.style.border = '1px solid var(--admin-border-subtle)';
                if (typeof field === 'string') {
                    item.textContent = field;
                } else {
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
                    reqBadge.className = field.required ? 'badge badge-warning' : 'badge badge-neutral';
                    reqBadge.textContent = field.required ? 'required' : 'optional';
                    item.appendChild(reqBadge);
                }
                awList.appendChild(item);
            }
            awSection.appendChild(awList);
            panelBody.appendChild(awSection);
        }

        // Lifecycle methods
        const postConstruct = bean.post_construct
            ? (Array.isArray(bean.post_construct) ? bean.post_construct : [bean.post_construct])
            : [];
        const preDestroy = bean.pre_destroy
            ? (Array.isArray(bean.pre_destroy) ? bean.pre_destroy : [bean.pre_destroy])
            : [];
        if (postConstruct.length > 0 || preDestroy.length > 0) {
            const lcSection = document.createElement('div');
            lcSection.className = 'mb-lg';
            lcSection.appendChild(_createSectionHeader('Lifecycle Methods'));
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
                item.appendChild(document.createTextNode(methodName));
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
                item.appendChild(document.createTextNode(methodName));
                lcList.appendChild(item);
            }
            lcSection.appendChild(lcList);
            panelBody.appendChild(lcSection);
        }
    }

    function destroy() {
        document.removeEventListener('keydown', _onKeyDown);
        hide();
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        if (panel.parentNode) panel.parentNode.removeChild(panel);
    }

    return { overlay, panel, show, hide, destroy };
}

/* ── Main Render ─────────────────────────────────────────────── */

export async function render(container, api) {
    container.replaceChildren();

    const wrapper = document.createElement('div');
    wrapper.className = 'view-enter';

    // Page header
    const header = document.createElement('div');
    header.className = 'page-header';
    const headerLeft = document.createElement('div');
    const h1 = document.createElement('h1');
    h1.textContent = 'Bean Dependency Graph';
    headerLeft.appendChild(h1);
    const sub = document.createElement('div');
    sub.className = 'page-subtitle';
    sub.textContent = 'force-directed dependency visualization';
    headerLeft.appendChild(sub);
    header.appendChild(headerLeft);
    wrapper.appendChild(header);

    // Loading skeleton (graph area is non-tabular)
    const loader = document.createElement('div');
    loader.appendChild(skeletonCard({ lines: 6 }));
    wrapper.appendChild(loader);
    container.appendChild(wrapper);

    // Fetch graph data
    let data;
    try {
        data = await api.get('/beans/graph');
    } catch (err) {
        wrapper.removeChild(loader);
        wrapper.appendChild(createEmptyStateCard({
            icon: 'alert',
            tone: 'danger',
            title: 'Failed to load graph',
            text: err.message,
        }));
        return;
    }

    wrapper.removeChild(loader);

    if (!data.nodes || data.nodes.length === 0) {
        wrapper.appendChild(createEmptyStateCard({
            icon: 'activity',
            title: 'No beans registered',
            text: 'No beans are registered in the application context to visualize.',
        }));
        return;
    }

    // ── Pre-compute edge index maps ──────────────────────────
    // Maps: nodeId -> Set of edge indices (outgoing / incoming)
    const outgoingMap = new Map();
    const incomingMap = new Map();
    for (const n of data.nodes) {
        outgoingMap.set(n.id, new Set());
        incomingMap.set(n.id, new Set());
    }
    for (let i = 0; i < data.edges.length; i++) {
        const e = data.edges[i];
        const srcId = typeof e.source === 'object' ? e.source.id : e.source;
        const tgtId = typeof e.target === 'object' ? e.target.id : e.target;
        if (outgoingMap.has(srcId)) outgoingMap.get(srcId).add(i);
        if (incomingMap.has(tgtId)) incomingMap.get(tgtId).add(i);
    }

    // Collect unique stereotypes from nodes
    const stereotypesInGraph = new Set();
    for (const n of data.nodes) {
        stereotypesInGraph.add(n.stereotype || 'none');
    }
    const stereotypeList = [...stereotypesInGraph].sort();

    // ── Filter Toolbar ───────────────────────────────────────
    const toolbar = document.createElement('div');
    toolbar.className = 'filter-toolbar';

    // Search section
    const searchSection = document.createElement('div');
    searchSection.className = 'filter-toolbar-search';
    const searchInputWrap = document.createElement('div');
    searchInputWrap.className = 'search-input';
    searchInputWrap.appendChild(_createSearchIconSvg());
    const searchInput = document.createElement('input');
    searchInput.type = 'text';
    searchInput.className = 'input';
    searchInput.placeholder = 'Filter beans by name...';
    searchInputWrap.appendChild(searchInput);
    searchSection.appendChild(searchInputWrap);
    toolbar.appendChild(searchSection);

    // Stereotype pills section
    const pillsSection = document.createElement('div');
    pillsSection.className = 'filter-pills';
    const activeStereotypes = new Set();

    for (const st of stereotypeList) {
        const pill = document.createElement('button');
        pill.className = 'filter-pill';
        pill.textContent = st;
        pill.addEventListener('click', () => {
            if (activeStereotypes.has(st)) {
                activeStereotypes.delete(st);
                pill.classList.remove('active');
            } else {
                activeStereotypes.add(st);
                pill.classList.add('active');
            }
            applyFilters();
        });
        pillsSection.appendChild(pill);
    }
    toolbar.appendChild(pillsSection);
    wrapper.appendChild(toolbar);

    // ── SVG container card ────────────────────────────────────
    const card = document.createElement('div');
    card.className = 'admin-card';
    const cardBody = document.createElement('div');
    cardBody.className = 'admin-card-body bean-graph-body';
    cardBody.style.padding = '0';
    cardBody.style.overflow = 'hidden';
    cardBody.style.position = 'relative';
    card.appendChild(cardBody);
    wrapper.appendChild(card);
    container.appendChild(wrapper);

    // Responsive dimensions (re-measured on resize / fullscreen toggle).
    let width = cardBody.clientWidth || 900;
    let height = cardBody.clientHeight || 600;

    // Create SVG with D3 — fills the card body; the viewBox tracks its pixel size.
    const svg = d3.select(cardBody)
        .append('svg')
        .attr('width', '100%')
        .attr('height', '100%')
        .attr('viewBox', `0 0 ${width} ${height}`);

    // Arrow marker for directed edges
    const defs = svg.append('defs');
    defs.append('marker')
        .attr('id', 'arrowhead')
        .attr('viewBox', '0 -5 10 10')
        .attr('refX', 28)
        .attr('refY', 0)
        .attr('markerWidth', 6)
        .attr('markerHeight', 6)
        .attr('orient', 'auto')
        .append('path')
        .attr('d', 'M0,-5L10,0L0,5')
        .attr('fill', '#64748b');

    // Second marker for dependent (incoming) highlight color
    defs.append('marker')
        .attr('id', 'arrowhead-dependent')
        .attr('viewBox', '0 -5 10 10')
        .attr('refX', 28)
        .attr('refY', 0)
        .attr('markerWidth', 6)
        .attr('markerHeight', 6)
        .attr('orient', 'auto')
        .append('path')
        .attr('d', 'M0,-5L10,0L0,5')
        .attr('fill', '#f59e0b');

    // Third marker for dependency (outgoing) highlight color
    defs.append('marker')
        .attr('id', 'arrowhead-dependency')
        .attr('viewBox', '0 -5 10 10')
        .attr('refX', 28)
        .attr('refY', 0)
        .attr('markerWidth', 6)
        .attr('markerHeight', 6)
        .attr('orient', 'auto')
        .append('path')
        .attr('d', 'M0,-5L10,0L0,5')
        .attr('fill', '#3b82f6');

    const g = svg.append('g');

    // Zoom/pan
    const zoom = d3.zoom()
        .scaleExtent([0.2, 4])
        .on('zoom', (event) => g.attr('transform', event.transform));
    svg.call(zoom);

    // Force simulation (stronger repulsion for larger nodes)
    const simulation = d3.forceSimulation(data.nodes)
        .force('link', d3.forceLink(data.edges).id(d => d.id).distance(120))
        .force('charge', d3.forceManyBody().strength(-350))
        .force('center', d3.forceCenter(width / 2, height / 2))
        .force('collision', d3.forceCollide().radius(d => nodeRadius(d) + 8));

    // Draw edges (dashed for autowired injection, solid for constructor)
    const links = g.append('g')
        .selectAll('line')
        .data(data.edges)
        .join('line')
        .attr('stroke', '#64748b')
        .attr('stroke-opacity', 0.4)
        .attr('stroke-width', d => d.type === 'autowired' ? 1 : 1.5)
        .attr('stroke-dasharray', d => d.type === 'autowired' ? '4,3' : null)
        .attr('marker-end', 'url(#arrowhead)');

    // Node radius based on resolution count + connection degree
    function nodeRadius(d) {
        const resolutions = d.resolution_count || 0;
        const outCount = outgoingMap.get(d.id) ? outgoingMap.get(d.id).size : 0;
        const inCount = incomingMap.get(d.id) ? incomingMap.get(d.id).size : 0;
        const degree = outCount + inCount;
        return Math.max(10, Math.min(35, Math.sqrt(resolutions + degree * 2 + 1) * 5));
    }

    // Draw nodes
    const nodes = g.append('g')
        .selectAll('circle')
        .data(data.nodes)
        .join('circle')
        .attr('r', nodeRadius)
        .attr('fill', d => STEREOTYPE_COLORS[d.stereotype] || STEREOTYPE_COLORS.none)
        .attr('stroke', '#1e293b')
        .attr('stroke-width', 1.5)
        .style('cursor', 'pointer')
        .call(d3.drag()
            .on('start', (event, d) => {
                if (!event.active) simulation.alphaTarget(0.3).restart();
                d.fx = d.x; d.fy = d.y;
            })
            .on('drag', (event, d) => { d.fx = event.x; d.fy = event.y; })
            .on('end', (event, d) => {
                if (!event.active) simulation.alphaTarget(0);
                d.fx = null; d.fy = null;
            })
        );

    // Labels
    const labels = g.append('g')
        .selectAll('text')
        .data(data.nodes)
        .join('text')
        .text(d => d.name)
        .attr('font-size', '11px')
        .attr('font-weight', '500')
        .attr('fill', '#94a3b8')
        .attr('dx', d => nodeRadius(d) + 6)
        .attr('dy', 4);

    // ── Enhanced Tooltip ─────────────────────────────────────
    const tooltip = d3.select(cardBody)
        .append('div')
        .style('position', 'absolute')
        .style('background', 'var(--admin-card-bg, #1a1a2e)')
        .style('border', '1px solid var(--admin-border, #2d3748)')
        .style('border-radius', '6px')
        .style('padding', '8px 12px')
        .style('font-size', '12px')
        .style('color', 'var(--admin-text, #e2e8f0)')
        .style('pointer-events', 'none')
        .style('opacity', 0)
        .style('z-index', 10);

    nodes
        .on('mouseover', (event, d) => {
            const ttNode = tooltip.node();
            ttNode.textContent = '';

            const strong = document.createElement('strong');
            strong.textContent = d.name;
            ttNode.appendChild(strong);
            ttNode.appendChild(document.createElement('br'));

            const typeSpan = document.createElement('span');
            typeSpan.style.color = 'var(--admin-text-muted)';
            typeSpan.textContent = d.type;
            ttNode.appendChild(typeSpan);
            ttNode.appendChild(document.createElement('br'));

            ttNode.appendChild(document.createTextNode('Stereotype: ' + (d.stereotype || 'none')));
            ttNode.appendChild(document.createElement('br'));

            ttNode.appendChild(document.createTextNode('Scope: ' + (d.scope || 'singleton')));
            ttNode.appendChild(document.createElement('br'));

            if (d.creation_time_ms != null) {
                ttNode.appendChild(document.createTextNode('Creation: ' + d.creation_time_ms.toFixed(1) + 'ms'));
                ttNode.appendChild(document.createElement('br'));
            }

            ttNode.appendChild(document.createTextNode('Initialized: ' + (d.initialized ? 'yes' : 'no')));
            ttNode.appendChild(document.createElement('br'));

            ttNode.appendChild(document.createTextNode('Resolutions: ' + (d.resolution_count || 0)));
            ttNode.appendChild(document.createElement('br'));

            // Dependency counts
            const outCount = outgoingMap.get(d.id) ? outgoingMap.get(d.id).size : 0;
            const inCount = incomingMap.get(d.id) ? incomingMap.get(d.id).size : 0;
            ttNode.appendChild(document.createTextNode('Dependencies (out): ' + outCount));
            ttNode.appendChild(document.createElement('br'));
            ttNode.appendChild(document.createTextNode('Dependents (in): ' + inCount));

            tooltip.style('opacity', 1);
        })
        .on('mousemove', (event) => {
            const rect = cardBody.getBoundingClientRect();
            tooltip
                .style('left', (event.clientX - rect.left + 12) + 'px')
                .style('top', (event.clientY - rect.top - 10) + 'px');
        })
        .on('mouseout', () => tooltip.style('opacity', 0));

    // ── Detail Panel ─────────────────────────────────────────
    const detailPanel = _createDetailPanel(api);
    document.body.appendChild(detailPanel.overlay);
    document.body.appendChild(detailPanel.panel);

    // ── Dependency Highlighting + Click Behavior ─────────────
    let highlightedNodeId = null;

    function resetHighlight() {
        highlightedNodeId = null;
        nodes.attr('opacity', 1);
        labels.attr('opacity', 1);
        links
            .attr('stroke', '#64748b')
            .attr('stroke-opacity', 0.4)
            .attr('stroke-width', d => d.type === 'autowired' ? 1 : 1.5)
            .attr('stroke-dasharray', d => d.type === 'autowired' ? '4,3' : null)
            .attr('marker-end', 'url(#arrowhead)');
    }

    function highlightNode(d) {
        highlightedNodeId = d.id;
        const outEdges = outgoingMap.get(d.id) || new Set();
        const inEdges = incomingMap.get(d.id) || new Set();

        // Collect connected node IDs
        const connectedNodeIds = new Set();
        connectedNodeIds.add(d.id);
        for (const ei of outEdges) {
            const e = data.edges[ei];
            const tgtId = typeof e.target === 'object' ? e.target.id : e.target;
            connectedNodeIds.add(tgtId);
        }
        for (const ei of inEdges) {
            const e = data.edges[ei];
            const srcId = typeof e.source === 'object' ? e.source.id : e.source;
            connectedNodeIds.add(srcId);
        }

        // Dim unrelated nodes
        nodes.attr('opacity', n => connectedNodeIds.has(n.id) ? 1 : 0.15);
        labels.attr('opacity', n => connectedNodeIds.has(n.id) ? 1 : 0.15);

        // Style edges
        links.each(function(e, i) {
            const el = d3.select(this);
            if (outEdges.has(i)) {
                // Outgoing dependency: thicker, blue
                el.attr('stroke', '#3b82f6')
                    .attr('stroke-opacity', 0.9)
                    .attr('stroke-width', 2.5)
                    .attr('stroke-dasharray', e.type === 'autowired' ? '4,3' : null)
                    .attr('marker-end', 'url(#arrowhead-dependency)');
            } else if (inEdges.has(i)) {
                // Incoming dependent: thicker, amber
                el.attr('stroke', '#f59e0b')
                    .attr('stroke-opacity', 0.9)
                    .attr('stroke-width', 2.5)
                    .attr('stroke-dasharray', e.type === 'autowired' ? '4,3' : null)
                    .attr('marker-end', 'url(#arrowhead-dependent)');
            } else {
                // Unrelated: dim
                el.attr('stroke', '#64748b')
                    .attr('stroke-opacity', 0.15)
                    .attr('stroke-width', 1)
                    .attr('stroke-dasharray', e.type === 'autowired' ? '4,3' : null)
                    .attr('marker-end', 'url(#arrowhead)');
            }
        });
    }

    // Node click: highlight + zoom + detail panel
    nodes.on('click', (event, d) => {
        event.stopPropagation();

        // Zoom to node
        const transform = d3.zoomIdentity
            .translate(width / 2, height / 2)
            .scale(2)
            .translate(-d.x, -d.y);
        svg.transition().duration(500).call(zoom.transform, transform);

        // Highlight dependencies
        highlightNode(d);

        // Show detail panel
        detailPanel.show(d);
    });

    // Click on SVG background to reset highlighting
    svg.on('click', () => {
        resetHighlight();
        // Re-apply any active search/stereotype filters
        applyFilters();
    });

    // ── Filter Logic ─────────────────────────────────────────
    function applyFilters() {
        const query = searchInput.value.trim().toLowerCase();
        const hasStereotypeFilter = activeStereotypes.size > 0;
        const hasSearchFilter = query.length > 0;

        if (!hasSearchFilter && !hasStereotypeFilter) {
            // No filters active: reset everything (unless a node is highlighted)
            if (highlightedNodeId) {
                const highlightedDatum = data.nodes.find(n => n.id === highlightedNodeId);
                if (highlightedDatum) {
                    highlightNode(highlightedDatum);
                    return;
                }
            }
            nodes.attr('opacity', 1);
            labels.attr('opacity', 1);
            links
                .attr('stroke', '#64748b')
                .attr('stroke-opacity', 0.4)
                .attr('stroke-width', d => d.type === 'autowired' ? 1 : 1.5)
                .attr('stroke-dasharray', d => d.type === 'autowired' ? '4,3' : null)
                .attr('marker-end', 'url(#arrowhead)');
            return;
        }

        // Clear any click-based highlighting when filters are active
        highlightedNodeId = null;

        // Determine matching node IDs
        const matchingIds = new Set();
        for (const n of data.nodes) {
            const matchesSearch = !hasSearchFilter || n.name.toLowerCase().includes(query);
            const matchesStereotype = !hasStereotypeFilter || activeStereotypes.has(n.stereotype || 'none');
            if (matchesSearch && matchesStereotype) {
                matchingIds.add(n.id);
            }
        }

        nodes.attr('opacity', n => matchingIds.has(n.id) ? 1 : 0.15);
        labels.attr('opacity', n => matchingIds.has(n.id) ? 1 : 0.15);

        links.each(function(e) {
            const el = d3.select(this);
            const srcId = typeof e.source === 'object' ? e.source.id : e.source;
            const tgtId = typeof e.target === 'object' ? e.target.id : e.target;
            const dashArray = e.type === 'autowired' ? '4,3' : null;
            if (matchingIds.has(srcId) && matchingIds.has(tgtId)) {
                el.attr('stroke', '#64748b')
                    .attr('stroke-opacity', 0.4)
                    .attr('stroke-width', e.type === 'autowired' ? 1 : 1.5)
                    .attr('stroke-dasharray', dashArray)
                    .attr('marker-end', 'url(#arrowhead)');
            } else {
                el.attr('stroke', '#64748b')
                    .attr('stroke-opacity', 0.15)
                    .attr('stroke-width', 1)
                    .attr('stroke-dasharray', dashArray)
                    .attr('marker-end', 'url(#arrowhead)');
            }
        });
    }

    function _onSearchInput() {
        applyFilters();
    }
    searchInput.addEventListener('input', _onSearchInput);

    // ── Tick ─────────────────────────────────────────────────
    simulation.on('tick', () => {
        links
            .attr('x1', d => d.source.x)
            .attr('y1', d => d.source.y)
            .attr('x2', d => d.target.x)
            .attr('y2', d => d.target.y);
        nodes.attr('cx', d => d.x).attr('cy', d => d.y);
        labels.attr('x', d => d.x).attr('y', d => d.y);
    });

    // ── Responsive resize + fullscreen ───────────────────────
    let _destroyed = false;
    function resizeGraph() {
        // A deferred resize must not revive a stopped simulation after teardown.
        if (_destroyed || !cardBody.isConnected) return;
        width = cardBody.clientWidth || width;
        height = cardBody.clientHeight || height;
        svg.attr('viewBox', `0 0 ${width} ${height}`);
        simulation.force('center', d3.forceCenter(width / 2, height / 2));
        simulation.alpha(0.3).restart();
    }

    let _resizeRAF = null;
    function onWindowResize() {
        if (_resizeRAF) return;
        _resizeRAF = requestAnimationFrame(() => {
            _resizeRAF = null;
            resizeGraph();
        });
    }
    window.addEventListener('resize', onWindowResize);

    // Expand-to-fullscreen (re-measures the graph once the card has resized).
    const fullscreen = attachFullscreen(card, { onResize: () => resizeGraph(), label: 'Expand graph' });

    // Reset view (clears pan/zoom back to the default transform).
    const resetBtn = document.createElement('button');
    resetBtn.type = 'button';
    resetBtn.className = 'btn btn-sm';
    resetBtn.textContent = 'Reset view';
    resetBtn.style.position = 'absolute';
    resetBtn.style.top = '10px';
    resetBtn.style.left = '10px';
    resetBtn.style.zIndex = '5';
    resetBtn.addEventListener('click', () => {
        svg.transition().duration(400).call(zoom.transform, d3.zoomIdentity);
    });
    card.appendChild(resetBtn);

    // ── Legend ────────────────────────────────────────────────
    const legendCard = document.createElement('div');
    legendCard.className = 'admin-card';
    legendCard.style.marginTop = '16px';
    const legendBody = document.createElement('div');
    legendBody.className = 'admin-card-body';
    legendBody.style.display = 'flex';
    legendBody.style.flexWrap = 'wrap';
    legendBody.style.gap = '16px';
    legendBody.style.alignItems = 'center';
    for (const [stereotype, color] of Object.entries(STEREOTYPE_COLORS)) {
        const item = document.createElement('div');
        item.style.display = 'flex';
        item.style.alignItems = 'center';
        item.style.gap = '6px';
        const dot = document.createElement('div');
        dot.style.width = '12px';
        dot.style.height = '12px';
        dot.style.borderRadius = '50%';
        dot.style.background = color;
        const label = document.createElement('span');
        label.style.fontSize = '12px';
        label.style.color = 'var(--admin-text-muted)';
        label.textContent = stereotype;
        item.appendChild(dot);
        item.appendChild(label);
        legendBody.appendChild(item);
    }

    // Edge type legend separator
    const sep = document.createElement('div');
    sep.style.width = '1px';
    sep.style.height = '20px';
    sep.style.background = 'var(--admin-border)';
    sep.style.margin = '0 4px';
    legendBody.appendChild(sep);

    // Constructor edge
    const ctorItem = document.createElement('div');
    ctorItem.style.display = 'flex';
    ctorItem.style.alignItems = 'center';
    ctorItem.style.gap = '6px';
    const ctorLine = document.createElement('div');
    ctorLine.style.width = '24px';
    ctorLine.style.height = '2px';
    ctorLine.style.background = '#64748b';
    const ctorLabel = document.createElement('span');
    ctorLabel.style.fontSize = '12px';
    ctorLabel.style.color = 'var(--admin-text-muted)';
    ctorLabel.textContent = 'constructor';
    ctorItem.appendChild(ctorLine);
    ctorItem.appendChild(ctorLabel);
    legendBody.appendChild(ctorItem);

    // Autowired edge
    const awItem = document.createElement('div');
    awItem.style.display = 'flex';
    awItem.style.alignItems = 'center';
    awItem.style.gap = '6px';
    const awLine = document.createElement('div');
    awLine.style.width = '24px';
    awLine.style.height = '0';
    awLine.style.borderTop = '2px dashed #64748b';
    const awLabel = document.createElement('span');
    awLabel.style.fontSize = '12px';
    awLabel.style.color = 'var(--admin-text-muted)';
    awLabel.textContent = 'autowired';
    awItem.appendChild(awLine);
    awItem.appendChild(awLabel);
    legendBody.appendChild(awItem);

    legendCard.appendChild(legendBody);
    wrapper.appendChild(legendCard);

    // ── Stats Bar ────────────────────────────────────────────
    const componentCount = _countConnectedComponents(data.nodes, data.edges);

    const statsBar = document.createElement('div');
    statsBar.style.display = 'flex';
    statsBar.style.gap = '24px';
    statsBar.style.padding = '12px 20px';
    statsBar.style.marginTop = '12px';
    statsBar.style.fontFamily = 'var(--admin-font-mono)';
    statsBar.style.fontSize = '0.8rem';
    statsBar.style.color = 'var(--admin-text-muted)';
    statsBar.style.background = 'var(--admin-surface)';
    statsBar.style.border = '1px solid var(--admin-border)';
    statsBar.style.borderRadius = 'var(--admin-radius-lg)';

    const ctorEdgeCount = data.edges.filter(e => e.type === 'constructor').length;
    const awEdgeCount = data.edges.filter(e => e.type === 'autowired').length;

    const statsItems = [
        ['Nodes', String(data.nodes.length)],
        ['Edges', String(data.edges.length)],
        ['Constructor', String(ctorEdgeCount)],
        ['Autowired', String(awEdgeCount)],
        ['Components', String(componentCount)],
    ];
    for (const [statLabel, statValue] of statsItems) {
        const statEl = document.createElement('div');
        statEl.style.display = 'flex';
        statEl.style.alignItems = 'center';
        statEl.style.gap = '6px';
        const labelSpan = document.createElement('span');
        labelSpan.textContent = statLabel + ':';
        const valueSpan = document.createElement('span');
        valueSpan.style.fontWeight = '600';
        valueSpan.style.color = 'var(--admin-text)';
        valueSpan.textContent = statValue;
        statEl.appendChild(labelSpan);
        statEl.appendChild(valueSpan);
        statsBar.appendChild(statEl);
    }
    wrapper.appendChild(statsBar);

    // ── Cleanup ──────────────────────────────────────────────
    return function cleanup() {
        _destroyed = true;
        if (_resizeRAF) { cancelAnimationFrame(_resizeRAF); _resizeRAF = null; }
        window.removeEventListener('resize', onWindowResize);
        fullscreen.destroy();
        simulation.stop();
        detailPanel.destroy();
        searchInput.removeEventListener('input', _onSearchInput);
    };
}
