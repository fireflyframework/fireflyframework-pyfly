/**
 * PyFly Admin — Skeleton Loaders.
 *
 * Lightweight shimmer placeholders shown while a view fetches its data.
 * They mirror the eventual layout (stat cards + table, etc.) so the page
 * doesn't jump, and read as more polished than a bare spinner.
 *
 * All builders return detached DOM nodes; callers append and later remove
 * them (or call replaceChildren) once real content is ready.
 */

/**
 * A single shimmer line.
 * @param {string} [width='100%']
 * @param {string} [height='12px']
 * @returns {HTMLElement}
 */
export function skeletonLine(width = '100%', height = '12px') {
    const el = document.createElement('div');
    el.className = 'skeleton skeleton-line';
    el.style.width = width;
    el.style.height = height;
    return el;
}

/**
 * A responsive row of skeleton stat cards.
 * @param {number} [count=4]  Number of cards (maps to the .grid-N utility).
 * @returns {HTMLElement}
 */
export function skeletonStatCards(count = 4) {
    const cols = count === 2 ? 2 : count === 3 ? 3 : 4;
    const row = document.createElement('div');
    row.className = `grid-${cols} mb-lg`;
    for (let i = 0; i < count; i++) {
        const card = document.createElement('div');
        card.className = 'skeleton skeleton-stat';
        row.appendChild(card);
    }
    return row;
}

/**
 * A card containing skeleton rows, approximating a data table.
 * @param {object} [opts]
 * @param {number} [opts.rows=6]
 * @returns {HTMLElement}
 */
export function skeletonTable({ rows = 6 } = {}) {
    const card = document.createElement('div');
    card.className = 'admin-card';
    const body = document.createElement('div');
    body.className = 'admin-card-body';
    // A heavier header line, then lighter body rows.
    const head = skeletonLine('40%', '18px');
    head.style.marginBottom = '20px';
    body.appendChild(head);
    for (let i = 0; i < rows; i++) {
        const line = skeletonLine(i % 3 === 0 ? '70%' : '100%', '15px');
        line.style.marginBottom = '14px';
        body.appendChild(line);
    }
    card.appendChild(body);
    return card;
}

/**
 * A generic card with a few text lines (for non-tabular views).
 * @param {object} [opts]
 * @param {number} [opts.lines=4]
 * @returns {HTMLElement}
 */
export function skeletonCard({ lines = 4 } = {}) {
    const card = document.createElement('div');
    card.className = 'admin-card';
    const body = document.createElement('div');
    body.className = 'admin-card-body';
    for (let i = 0; i < lines; i++) {
        body.appendChild(skeletonLine(i === 0 ? '50%' : `${70 + (i * 7) % 30}%`, '14px'));
    }
    card.appendChild(body);
    return card;
}

/**
 * Composite page skeleton: a stat-card row plus a table card — the most
 * common admin view shape.
 * @param {object} [opts]
 * @param {number} [opts.stats=4]
 * @param {number} [opts.rows=6]
 * @returns {DocumentFragment}
 */
export function pageSkeleton({ stats = 4, rows = 6 } = {}) {
    const frag = document.createDocumentFragment();
    if (stats > 0) frag.appendChild(skeletonStatCards(stats));
    frag.appendChild(skeletonTable({ rows }));
    return frag;
}
