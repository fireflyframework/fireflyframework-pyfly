/**
 * PyFly Admin — Fullscreen / Expand affordance for cards.
 *
 * Adds a maximize button to a card; toggling it makes the card fill the
 * viewport (position: fixed) so large content — dependency graphs, log
 * streams, charts — can be explored without being boxed into a fixed height.
 * Pressing Escape exits. An optional onResize callback fires after each toggle
 * (next frame, once layout has settled) so canvas/SVG content can re-measure.
 */

const SVG_NS = 'http://www.w3.org/2000/svg';

function svgIcon(paths) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    for (const d of paths) {
        const p = document.createElementNS(SVG_NS, 'path');
        p.setAttribute('d', d);
        svg.appendChild(p);
    }
    return svg;
}

const EXPAND = [
    'M8 3H5a2 2 0 0 0-2 2v3',
    'M21 8V5a2 2 0 0 0-2-2h-3',
    'M3 16v3a2 2 0 0 0 2 2h3',
    'M16 21h3a2 2 0 0 0 2-2v-3',
];
const COLLAPSE = [
    'M8 3v3a2 2 0 0 1-2 2H3',
    'M21 8h-3a2 2 0 0 1-2-2V3',
    'M3 16h3a2 2 0 0 1 2 2v3',
    'M16 21v-3a2 2 0 0 1 2-2h3',
];

/**
 * Attach a fullscreen toggle to a card.
 *
 * @param {HTMLElement} card                The .admin-card to expand.
 * @param {object} [opts]
 * @param {HTMLElement} [opts.anchor]       Element to append the button into
 *                                          (e.g. a card header's right side).
 *                                          If omitted, the button floats at the
 *                                          card's top-right corner.
 * @param {(isFullscreen: boolean) => void} [opts.onResize]  Called after toggle.
 * @param {string} [opts.label='Toggle fullscreen']
 * @returns {{ isFullscreen: () => boolean, exit: () => void, destroy: () => void }}
 */
export function attachFullscreen(card, { anchor = null, onResize = null, label = 'Toggle fullscreen' } = {}) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn-icon card-fs-btn';
    btn.setAttribute('aria-label', label);
    btn.title = `${label} (Esc to exit)`;
    btn.appendChild(svgIcon(EXPAND));

    let isFs = false;
    let placeholder = null;
    let resizeRAF = null;

    function setFullscreen(on) {
        if (on === isFs) return;
        isFs = on;
        if (on) {
            // Re-parent to <body> so position:fixed resolves against the
            // viewport. A transformed ancestor (e.g. the view-enter wrapper,
            // which keeps transform: translateY(0) after its animation) would
            // otherwise become the containing block and clip the overlay to the
            // content area. A comment node marks the original position.
            placeholder = document.createComment('fullscreen-placeholder');
            card.parentNode.insertBefore(placeholder, card);
            document.body.appendChild(card);
            card.classList.add('card-fullscreen');
            document.body.classList.add('has-fullscreen');
        } else {
            card.classList.remove('card-fullscreen');
            document.body.classList.remove('has-fullscreen');
            if (placeholder && placeholder.parentNode) {
                placeholder.parentNode.insertBefore(card, placeholder);
                placeholder.remove();
            }
            placeholder = null;
        }
        btn.replaceChildren(svgIcon(on ? COLLAPSE : EXPAND));
        btn.setAttribute('aria-pressed', String(on));
        // Let the browser apply the new box, then notify the caller. Cancel any
        // prior pending frame so a rapid toggle (or a toggle during teardown)
        // can't fire a stale onResize after the view is gone.
        if (resizeRAF) cancelAnimationFrame(resizeRAF);
        resizeRAF = requestAnimationFrame(() => {
            resizeRAF = null;
            if (onResize) onResize(isFs);
        });
    }

    btn.addEventListener('click', () => setFullscreen(!isFs));

    function onKey(e) {
        if (e.key === 'Escape' && isFs) setFullscreen(false);
    }
    document.addEventListener('keydown', onKey);

    if (anchor) {
        anchor.appendChild(btn);
    } else {
        if (getComputedStyle(card).position === 'static') {
            card.style.position = 'relative';
        }
        btn.classList.add('card-fs-btn-floating');
        card.appendChild(btn);
    }

    return {
        isFullscreen: () => isFs,
        exit: () => setFullscreen(false),
        destroy: () => {
            document.removeEventListener('keydown', onKey);
            if (isFs) setFullscreen(false);
            // setFullscreen(false) may have scheduled a frame; drop it so no
            // stale onResize runs after teardown.
            if (resizeRAF) { cancelAnimationFrame(resizeRAF); resizeRAF = null; }
            btn.remove();
        },
    };
}
