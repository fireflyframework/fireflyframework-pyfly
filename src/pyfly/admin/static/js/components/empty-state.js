/**
 * PyFly Admin — Empty / Error State.
 *
 * A consistent, iconographic placeholder for "no data" and error states,
 * replacing the ad-hoc title+text blocks scattered across views.
 */

const SVG_NS = 'http://www.w3.org/2000/svg';

/**
 * Icon path sets (24x24, stroke-based). Keyed by semantic name.
 * Each entry is an array of <path>/<line>/<circle> descriptors.
 */
const ICONS = {
    // Empty inbox / tray
    inbox: ['path:M22 12h-6l-2 3h-4l-2-3H2', 'path:M5.45 5.11L2 12v6a2 2 0 002 2h16a2 2 0 002-2v-6l-3.45-6.89A2 2 0 0016.76 4H7.24a2 2 0 00-1.79 1.11z'],
    // Warning triangle (errors)
    alert: ['path:M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z', 'line:12 9 12 13', 'line:12 17 12.01 17'],
    // Magnifier (no matches)
    search: ['circle:11 11 8', 'line:21 21 16.65 16.65'],
    // Database / data
    database: ['path:M21 5c0 1.66-4.03 3-9 3S3 6.66 3 5s4.03-3 9-3 9 1.34 9 3z', 'path:M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5', 'path:M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3'],
    // Activity / metrics
    activity: ['path:M22 12h-4l-3 9L9 3l-3 9H2'],
    // Server / instances
    server: ['path:M5 3h14a2 2 0 012 2v4a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2z', 'path:M5 13h14a2 2 0 012 2v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4a2 2 0 012-2z', 'line:6 7 6.01 7', 'line:6 17 6.01 17'],
    // Plug / disconnected
    plug: ['path:M12 22v-5', 'path:M9 8V2', 'path:M15 8V2', 'path:M18 8v3a6 6 0 01-12 0V8z'],
};

/**
 * Build an SVG element from an icon descriptor list.
 * @param {string} name
 * @returns {SVGElement}
 */
function buildIcon(name) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.75');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');

    const parts = ICONS[name] || ICONS.inbox;
    for (const spec of parts) {
        const [kind, coords] = spec.split(':');
        if (kind === 'path') {
            const p = document.createElementNS(SVG_NS, 'path');
            p.setAttribute('d', coords);
            svg.appendChild(p);
        } else if (kind === 'line') {
            const [x1, y1, x2, y2] = coords.split(' ');
            const l = document.createElementNS(SVG_NS, 'line');
            l.setAttribute('x1', x1); l.setAttribute('y1', y1);
            l.setAttribute('x2', x2); l.setAttribute('y2', y2);
            svg.appendChild(l);
        } else if (kind === 'circle') {
            const [cx, cy, r] = coords.split(' ');
            const c = document.createElementNS(SVG_NS, 'circle');
            c.setAttribute('cx', cx); c.setAttribute('cy', cy); c.setAttribute('r', r);
            svg.appendChild(c);
        }
    }
    return svg;
}

/**
 * Create a consistent empty/error state block.
 * @param {object} opts
 * @param {string} [opts.icon='inbox']  One of the ICONS keys.
 * @param {string} opts.title           Headline.
 * @param {string} [opts.text]          Supporting description.
 * @param {'muted'|'danger'} [opts.tone='muted']  Icon tint.
 * @returns {HTMLElement}
 */
export function createEmptyState({ icon = 'inbox', title, text, tone = 'muted' } = {}) {
    const wrap = document.createElement('div');
    wrap.className = 'empty-state';

    const svg = buildIcon(icon);
    if (tone === 'danger') svg.style.color = 'var(--admin-danger)';
    wrap.appendChild(svg);

    if (title) {
        const t = document.createElement('div');
        t.className = 'empty-state-title';
        t.textContent = title;
        wrap.appendChild(t);
    }
    if (text) {
        const p = document.createElement('div');
        p.className = 'empty-state-text';
        p.textContent = text;
        wrap.appendChild(p);
    }
    return wrap;
}

/**
 * Create an empty/error state wrapped in a card (the common full-width
 * "no data" / "failed to load" panel).
 * @param {object} opts  Same as createEmptyState.
 * @returns {HTMLElement}
 */
export function createEmptyStateCard(opts) {
    const card = document.createElement('div');
    card.className = 'admin-card';
    const body = document.createElement('div');
    body.className = 'admin-card-body';
    body.appendChild(createEmptyState(opts));
    card.appendChild(body);
    return card;
}
