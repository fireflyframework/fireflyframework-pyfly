/**
 * PyFly Admin — Command Palette (⌘K / Ctrl-K).
 *
 * A keyboard-first launcher that fuzzy-filters every view + quick action and
 * navigates on Enter. Built with safe DOM construction (no innerHTML with data).
 */

import { createSvgIcon, ICONS, NAV_ITEMS, SERVER_ITEMS } from './sidebar.js';

/**
 * Install the command palette and its global ⌘K / Ctrl-K shortcut.
 *
 * @param {object}   opts
 * @param {function} opts.onNavigate    Called with a route id to navigate.
 * @param {boolean}  [opts.serverMode]  Include server-mode views.
 * @param {function} [opts.onToggleTheme]  Toggle dark/light.
 * @returns {{ open: function, close: function }}
 */
export function installCommandPalette({ onNavigate, serverMode = false, onToggleTheme = null }) {
    const navItems = serverMode ? [...NAV_ITEMS, ...SERVER_ITEMS] : NAV_ITEMS;

    const commands = [
        ...navItems.map((it) => ({
            label: it.label,
            hint: 'Go to',
            icon: it.icon,
            keywords: `${it.label} ${it.id} ${it.section || ''}`.toLowerCase(),
            run: () => onNavigate(it.id),
        })),
        {
            label: 'Toggle theme',
            hint: 'Action',
            icon: 'cog',
            keywords: 'theme dark light mode toggle',
            run: () => onToggleTheme && onToggleTheme(),
        },
        {
            label: 'Wallboard mode',
            hint: 'Action',
            icon: 'chart',
            keywords: 'wallboard fullscreen kiosk tv display',
            run: () => onNavigate('wallboard'),
        },
    ];

    let active = 0;
    let filtered = commands;

    // ── DOM ──────────────────────────────────────────────────────
    const overlay = document.createElement('div');
    overlay.className = 'cmd-palette-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-label', 'Command palette');

    const panel = document.createElement('div');
    panel.className = 'cmd-palette';

    const inputWrap = document.createElement('div');
    inputWrap.className = 'cmd-palette-input-wrap';
    inputWrap.appendChild(createSvgIcon('M21 21l-4.35-4.35 M11 19a8 8 0 100-16 8 8 0 000 16z'));
    const input = document.createElement('input');
    input.className = 'cmd-palette-input';
    input.type = 'text';
    input.placeholder = 'Search views and actions…';
    input.setAttribute('aria-label', 'Search');
    inputWrap.appendChild(input);
    const kbd = document.createElement('kbd');
    kbd.className = 'cmd-palette-esc';
    kbd.textContent = 'ESC';
    inputWrap.appendChild(kbd);
    panel.appendChild(inputWrap);

    const list = document.createElement('div');
    list.className = 'cmd-palette-list';
    panel.appendChild(list);

    overlay.appendChild(panel);

    function renderList() {
        list.textContent = '';
        if (filtered.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'cmd-palette-empty';
            empty.textContent = 'No matching commands';
            list.appendChild(empty);
            return;
        }
        filtered.forEach((cmd, i) => {
            const item = document.createElement('div');
            item.className = 'cmd-palette-item' + (i === active ? ' active' : '');
            item.setAttribute('role', 'option');

            const iconData = ICONS[cmd.icon];
            if (iconData) {
                const ic = createSvgIcon(iconData);
                ic.classList.add('cmd-palette-item-icon');
                item.appendChild(ic);
            }
            const label = document.createElement('span');
            label.className = 'cmd-palette-item-label';
            label.textContent = cmd.label;
            item.appendChild(label);

            const hint = document.createElement('span');
            hint.className = 'cmd-palette-item-hint';
            hint.textContent = cmd.hint;
            item.appendChild(hint);

            item.addEventListener('mousemove', () => {
                if (active !== i) { active = i; highlight(); }
            });
            item.addEventListener('click', () => execute(i));
            list.appendChild(item);
        });
    }

    function highlight() {
        [...list.children].forEach((el, i) => el.classList.toggle('active', i === active));
        const el = list.children[active];
        if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
    }

    function filter(query) {
        const q = query.trim().toLowerCase();
        filtered = q ? commands.filter((c) => q.split(/\s+/).every((t) => c.keywords.includes(t))) : commands;
        active = 0;
        renderList();
    }

    function execute(i) {
        const cmd = filtered[i];
        close();
        if (cmd) cmd.run();
    }

    function open() {
        if (overlay.classList.contains('open')) return;
        input.value = '';
        filter('');
        document.body.appendChild(overlay);
        // next frame for the transition
        requestAnimationFrame(() => overlay.classList.add('open'));
        input.focus();
    }

    function close() {
        overlay.classList.remove('open');
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
    }

    // ── Events ───────────────────────────────────────────────────
    input.addEventListener('input', () => filter(input.value));

    overlay.addEventListener('mousedown', (e) => {
        if (e.target === overlay) close();
    });

    input.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            active = Math.min(active + 1, filtered.length - 1);
            highlight();
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            active = Math.max(active - 1, 0);
            highlight();
        } else if (e.key === 'Enter') {
            e.preventDefault();
            execute(active);
        } else if (e.key === 'Escape') {
            e.preventDefault();
            close();
        }
    });

    // Global shortcut: ⌘K / Ctrl-K (and Ctrl-/ as an alias)
    document.addEventListener('keydown', (e) => {
        const meta = e.metaKey || e.ctrlKey;
        if (meta && (e.key === 'k' || e.key === 'K')) {
            e.preventDefault();
            overlay.classList.contains('open') ? close() : open();
        }
    });

    return { open, close };
}
