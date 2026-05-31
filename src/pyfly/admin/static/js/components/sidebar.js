/**
 * PyFly Admin — Sidebar Navigation Component.
 *
 * Renders the navigation sidebar with SVG icons.
 * Built entirely with safe DOM construction (no innerHTML with data).
 */

/* ── SVG Icon Paths ───────────────────────────────────────────── */
export const ICONS = {
    home: 'M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2z M9 22V12h6v10',
    cube: 'M21 16V8a2 2 0 00-1-1.73l-7-4a2 2 0 00-2 0l-7 4A2 2 0 003 8v8a2 2 0 001 1.73l7 4a2 2 0 002 0l7-4A2 2 0 0021 16z M3.27 6.96L12 12.01l8.73-5.05 M12 22.08V12',
    heart: 'M20.84 4.61a5.5 5.5 0 00-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 00-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 000-7.78z',
    globe: 'M12 2a10 10 0 100 20 10 10 0 000-20z M2 12h20 M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10A15.3 15.3 0 0112 2z',
    cog: 'M12 15a3 3 0 100-6 3 3 0 000 6z M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83 0 2 2 0 010-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z',
    list: 'M8 6h13 M8 12h13 M8 18h13 M3 6h.01 M3 12h.01 M3 18h.01',
    chart: 'M18 20V10 M12 20V4 M6 20v-6',
    clock: 'M12 2a10 10 0 100 20 10 10 0 000-20z M12 6v6l4 2',
    route: 'M16.5 9.4l-9-5.19 M21 16V8a2 2 0 00-1-1.73l-7-4a2 2 0 00-2 0l-7 4A2 2 0 003 8v8a2 2 0 001 1.73l7 4a2 2 0 002 0l7-4A2 2 0 0021 16z M3.27 6.96L12 12.01l8.73-5.05 M12 22.08V12',
    database: 'M12 2C6.48 2 2 3.79 2 6v12c0 2.21 4.48 4 10 4s10-1.79 10-4V6c0-2.21-4.48-4-10-4z M2 6c0 2.21 4.48 4 10 4s10-1.79 10-4 M2 12c0 2.21 4.48 4 10 4s10-1.79 10-4',
    arrows: 'M17 1l4 4-4 4 M3 11V9a4 4 0 014-4h14 M7 23l-4-4 4-4 M21 13v2a4 4 0 01-4 4H3',
    shuffle: 'M16 3h5v5 M4 20L21 3 M21 16v5h-5 M15 15l6 6 M4 4l5 5',
    activity: 'M22 12h-4l-3 9L9 3l-3 9H2',
    fileText: 'M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z M14 2v6h6 M16 13H8 M16 17H8 M10 9H8',
    server: 'M2 4h20v6H2z M2 14h20v6H2z M6 8h.01 M6 18h.01',
};

/**
 * Navigation items definition.
 * Each item: { id, label, icon, section? }
 * section creates a section header before the item.
 */
export const NAV_ITEMS = [
    { id: '',          label: 'Overview',      icon: 'home',     section: 'Dashboard' },
    { id: 'health',    label: 'Health',         icon: 'heart' },
    { id: 'beans',     label: 'Beans',          icon: 'cube',     section: 'Application' },
    { id: 'env',       label: 'Environment',    icon: 'globe' },
    { id: 'config',    label: 'Configuration',  icon: 'cog' },
    { id: 'loggers',   label: 'Loggers',        icon: 'list' },
    { id: 'metrics',   label: 'Metrics',        icon: 'chart',    section: 'Monitoring' },
    { id: 'scheduled', label: 'Scheduled',      icon: 'clock' },
    { id: 'traces',    label: 'Traces',         icon: 'activity' },
    { id: 'mappings',  label: 'Mappings',       icon: 'route',    section: 'Infrastructure' },
    { id: 'caches',    label: 'Caches',         icon: 'database' },
    { id: 'cqrs',      label: 'CQRS',           icon: 'arrows' },
    { id: 'transactions', label: 'Transactions', icon: 'shuffle' },
    { id: 'logfile',   label: 'Log Viewer',     icon: 'fileText' },
    { id: 'runtime',  label: 'Process',        icon: 'activity',  section: 'Runtime' },
    { id: 'bean-graph', label: 'Bean Graph',   icon: 'shuffle' },
];

/** Navigation item only shown in server mode. */
export const SERVER_ITEMS = [
    { id: 'instances', label: 'Instances',  icon: 'server', section: 'Fleet' },
];

/**
 * Create an SVG element from path data.
 * @param {string} pathData  Space-separated SVG path "d" strings.
 * @returns {SVGElement}
 */
export function createSvgIcon(pathData) {
    const svgNS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('width', '18');
    svg.setAttribute('height', '18');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');

    // Each path data segment is separated by " M" at the start of a new sub-path.
    // We split on " M" keeping the M, and create separate <path> elements.
    const segments = pathData.split(/(?= M)/);
    for (const seg of segments) {
        const path = document.createElementNS(svgNS, 'path');
        path.setAttribute('d', seg.trim());
        svg.appendChild(path);
    }

    return svg;
}

/**
 * Render the sidebar navigation into the given container.
 *
 * @param {HTMLElement} container  The sidebar element.
 * @param {string}      currentRoute  Current hash route (e.g. "beans" or "").
 * @param {object}      [options]
 * @param {boolean}     [options.serverMode]  Show server-mode items.
 * @param {function}    [options.onNavigate]  Called when a nav item is clicked.
 */
export function renderSidebar(container, currentRoute, options = {}) {
    const { serverMode = false, onNavigate = null } = options;

    // Clear previous content
    container.textContent = '';

    // Brand header — the pyfly wordmark logo (no secondary label).
    const brand = document.createElement('div');
    brand.className = 'admin-sidebar-brand';

    const logo = document.createElement('img');
    logo.src = 'static/assets/pyfly-logo.png';
    logo.alt = 'PyFly';
    brand.appendChild(logo);

    container.appendChild(brand);

    // Navigation
    const nav = document.createElement('nav');
    nav.className = 'admin-sidebar-nav';

    const items = serverMode ? [...NAV_ITEMS, ...SERVER_ITEMS] : NAV_ITEMS;

    for (const item of items) {
        // Section header
        if (item.section) {
            const sectionEl = document.createElement('div');
            sectionEl.className = 'admin-sidebar-section';
            sectionEl.textContent = item.section;
            nav.appendChild(sectionEl);
        }

        // Nav item
        const link = document.createElement('a');
        link.className = 'admin-nav-item';
        link.href = `#${item.id}`;
        if (item.id === currentRoute) {
            link.classList.add('active');
        }

        // Icon
        const iconData = ICONS[item.icon];
        if (iconData) {
            link.appendChild(createSvgIcon(iconData));
        }

        // Label
        const label = document.createElement('span');
        label.textContent = item.label;
        link.appendChild(label);

        // Click handler
        if (onNavigate) {
            link.addEventListener('click', (e) => {
                e.preventDefault();
                onNavigate(item.id);
            });
        }

        nav.appendChild(link);
    }

    container.appendChild(nav);

    // Footer
    const footer = document.createElement('div');
    footer.className = 'admin-sidebar-footer';

    const footerLine1 = document.createElement('div');
    footerLine1.textContent = '\u00A9 2026 Firefly Software Foundation.';
    footer.appendChild(footerLine1);

    const footerLine2 = document.createElement('div');
    footerLine2.textContent = 'Licensed under Apache 2.0';
    footer.appendChild(footerLine2);

    container.appendChild(footer);
}

/**
 * Update the active state of sidebar nav items without re-rendering.
 *
 * @param {HTMLElement} container    The sidebar element.
 * @param {string}      currentRoute  Current hash route.
 */
export function updateSidebarActive(container, currentRoute) {
    const items = container.querySelectorAll('.admin-nav-item');
    for (const item of items) {
        const href = item.getAttribute('href') || '';
        const route = href.replace('#', '');
        if (route === currentRoute) {
            item.classList.add('active');
        } else {
            item.classList.remove('active');
        }
    }
}
