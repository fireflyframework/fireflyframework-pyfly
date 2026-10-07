/** Feature flag administration. All server-provided content enters through textContent. */
import { createEmptyStateCard } from '../components/empty-state.js';
import { createFilterToolbar } from '../components/filter-toolbar.js';
import { pageSkeleton } from '../components/skeleton.js';
import { showToast } from '../components/toast.js';

function el(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined && value !== null) node.textContent = String(value);
    return node;
}

function badge(value, tone) {
    const node = el('span', `badge badge-${tone}`);
    node.append(el('span', 'badge-dot'), el('span', null, value));
    return node;
}

function table(headers) {
    const wrap = el('div', 'admin-table-wrapper');
    const grid = el('table', 'admin-table');
    const head = el('thead');
    const row = el('tr');
    for (const header of headers) row.append(el('th', null, header));
    head.append(row);
    const body = el('tbody');
    grid.append(head, body);
    wrap.append(grid);
    return { wrap, body };
}

function field(label, control) {
    const wrap = el('label', 'page-subtitle');
    wrap.append(el('span', null, label), control);
    control.style.display = 'block';
    control.style.width = '100%';
    return wrap;
}

async function action(api, key, body) {
    try {
        return await api.post(`/flags/${encodeURIComponent(key)}`, body);
    } catch (error) {
        showToast(`${key}: ${error.message}`, 'error');
        return null;
    }
}

function pendingNotice(parent, key) {
    parent.replaceChildren(createEmptyStateCard({
        icon: 'info', title: 'Change accepted; refresh pending',
        text: `${key} was saved, but this process has not loaded the latest definition. Refresh to check it.`,
    }));
}

function renderSources(wrapper, sources) {
    const card = el('div', 'card mb-lg');
    card.append(el('h3', null, 'Sources'));
    const { wrap, body } = table(['Source', 'Status', 'Flags', 'Last refresh', 'Revision', 'Error']);
    for (const source of sources) {
        const row = el('tr');
        row.append(el('td', 'text-mono', source.name));
        const status = el('td');
        status.append(badge(source.status, source.status === 'UP' ? 'success' : source.status === 'STALE' ? 'warning' : 'danger'));
        row.append(status, el('td', null, source.flags), el('td', 'text-mono', source.lastRefresh || '--'),
            el('td', 'text-mono', source.revision || '--'), el('td', null, source.error || ''));
        body.append(row);
    }
    card.append(wrap);
    wrapper.append(card);
}

async function renderDetail(panel, api, key, writable, refresh) {
    panel.replaceChildren(el('p', 'page-subtitle', 'Loading flag…'));
    let detail;
    try {
        detail = await api.get(`/flags/${encodeURIComponent(key)}`);
    } catch (error) {
        panel.replaceChildren(createEmptyStateCard({ icon: 'alert', tone: 'danger', title: key, text: error.message }));
        return;
    }
    const card = el('section', 'card mb-lg');
    card.append(el('h3', 'text-mono', detail.key));
    if (detail.expired) card.append(badge('Expired', 'warning'));
    card.append(el('p', 'page-subtitle', `Origin: ${detail.origin} · Version: ${detail.version ?? '--'} · Layers: ${detail.layers.map(layer => layer.source).join(' < ')}`));
    const editor = el('textarea', 'input text-mono');
    editor.rows = 12;
    editor.value = JSON.stringify(detail.definition, null, 2);
    editor.disabled = !writable;
    card.append(el('h4', null, 'Definition'), field('Flag definition (JSON)', editor));
    const controls = el('div');
    controls.style.display = 'flex';
    controls.style.flexWrap = 'wrap';
    controls.style.gap = '8px';
    const save = el('button', 'btn btn-sm btn-primary', 'Save to the store');
    save.disabled = !writable;
    save.addEventListener('click', async () => {
        let definition;
        try { definition = JSON.parse(editor.value); }
        catch (error) { showToast(`Definition is not JSON: ${error.message}`, 'error'); return; }
        const body = { action: 'put', definition };
        if (detail.version !== null) body.expectedVersion = detail.version;
        const receipt = await action(api, key, body);
        if (!receipt) return;
        if (receipt.refreshPending) { pendingNotice(panel, key); return; }
        showToast(`${key} saved`, 'success');
        await refresh(key);
    });
    controls.append(save);
    if (detail.origin === 'store') {
        const remove = el('button', 'btn btn-sm btn-danger', 'Delete store override');
        remove.disabled = !writable;
        remove.addEventListener('click', async () => {
            if (!window.confirm(`Delete the store definition of ${key}? The next layer applies again.`)) return;
            const receipt = await action(api, key, { action: 'delete', expectedVersion: detail.version });
            if (!receipt) return;
            showToast(`${key} store override deleted`, 'success');
            await refresh(receipt.deleted ? null : key);
        });
        controls.append(remove);
    }
    card.append(controls);

    const context = el('textarea', 'input text-mono');
    context.rows = 3;
    context.placeholder = '{"plan": "pro"}';
    const targetingKey = el('input', 'input');
    targetingKey.placeholder = 'Optional targeting key';
    const evaluate = el('button', 'btn btn-sm', 'Evaluate');
    const result = el('pre', 'text-mono');
    result.setAttribute('aria-live', 'polite');
    evaluate.addEventListener('click', async () => {
        let attributes;
        try { attributes = context.value.trim() ? JSON.parse(context.value) : {}; }
        catch (error) { showToast(`Context is not JSON: ${error.message}`, 'error'); return; }
        const body = { action: 'evaluate', context: attributes };
        if (targetingKey.value.trim()) body.targetingKey = targetingKey.value.trim();
        const outcome = await action(api, key, body);
        if (outcome) result.textContent = JSON.stringify(outcome, null, 2);
    });
    card.append(el('h4', null, 'Evaluation preview'), field('Context (JSON object)', context),
        field('Targeting key', targetingKey), evaluate, result, el('h4', null, 'Store history'));
    if (!detail.history.length) card.append(el('p', 'page-subtitle', 'No store changes.'));
    else {
        const { wrap, body } = table(['#', 'Action', 'Actor', 'Changed at']);
        for (const change of detail.history) {
            const row = el('tr');
            row.append(el('td', 'text-mono', change.id), el('td', null, change.action),
                el('td', null, change.actor || '--'), el('td', 'text-mono', change.changedAt));
            body.append(row);
        }
        card.append(wrap);
    }
    panel.replaceChildren(card);
}

export async function render(container, api, { signal } = {}) {
    const wrapper = el('div', 'view-enter');
    const header = el('div', 'page-header');
    const heading = el('div');
    heading.append(el('h1', null, 'Feature flags'), el('p', 'page-subtitle', 'Effective definitions and source health'));
    const refreshButton = el('button', 'btn btn-sm', 'Refresh');
    header.append(heading, refreshButton);
    wrapper.append(header);
    const content = el('div');
    content.append(pageSkeleton({ stats: 3, rows: 6 }));
    wrapper.append(content);
    container.replaceChildren(wrapper);

    let selected = null;
    async function load() {
        let data;
        try { data = await api.get('/flags', { signal }); }
        catch (error) {
            content.replaceChildren(createEmptyStateCard({ icon: 'alert', tone: 'danger', title: 'Failed to load flags', text: error.message }));
            return;
        }
        if (signal?.aborted) return;
        content.replaceChildren();
        if (!data.available) {
            content.append(createEmptyStateCard({ icon: 'inbox', title: 'Feature flags are not enabled',
                text: 'Enable pyfly.feature-flags and install the feature-flags extra.' }));
            return;
        }
        const writable = Boolean(data.writesEnabled && data.writable);
        const summary = el('p', 'page-subtitle mb-lg');
        summary.append(badge(`${data.provider.name}: ${data.provider.status}`, data.provider.status === 'READY' ? 'success' : 'warning'),
            el('span', null, writable ? ' Writes enabled.' : ' Read-only: writes need management.writes and a store.'));
        content.append(summary);
        renderSources(content, data.sources);
        const list = el('section', 'card mb-lg');
        const panel = el('div');
        const { wrap, body } = table(['Key', 'State', 'Type', 'Default variant', 'Origin', 'Version', 'Targeting']);
        const toolbar = createFilterToolbar({ placeholder: 'Search flags…', totalCount: data.flags.length,
            pills: [{ label: 'All', value: 'all' }, { label: 'Expired', value: 'expired' }, { label: 'Store', value: 'store' }],
            onFilter: ({ search, pill }) => rows(search, pill) });
        list.append(toolbar, wrap);
        content.append(list, panel);

        function rows(search = '', pill = 'all') {
            body.replaceChildren();
            const visible = data.flags.filter(flag => flag.key.toLowerCase().includes(search) &&
                (pill === 'all' || (pill === 'expired' && flag.expired) || (pill === 'store' && flag.origin === 'store')));
            toolbar.updateCount(visible.length, data.flags.length);
            for (const flag of visible) {
                const row = el('tr');
                const keyCell = el('td', 'text-mono');
                const link = el('a', null, flag.key);
                link.href = '#flags';
                link.addEventListener('click', event => { event.preventDefault(); selected = flag.key; void renderDetail(panel, api, flag.key, writable, reload); });
                keyCell.append(link);
                if (flag.expired) keyCell.append(badge('Expired', 'warning'));
                const stateCell = el('td');
                const toggle = el('button', 'btn btn-sm', flag.state);
                toggle.setAttribute('aria-label', `${flag.state === 'ENABLED' ? 'Disable' : 'Enable'} ${flag.key}`);
                toggle.disabled = !writable;
                toggle.addEventListener('click', async () => {
                    const body = { action: flag.state === 'ENABLED' ? 'disable' : 'enable' };
                    if (flag.version !== null) body.expectedVersion = flag.version;
                    const receipt = await action(api, flag.key, body);
                    if (!receipt) return;
                    if (receipt.refreshPending) { pendingNotice(panel, flag.key); return; }
                    showToast(`${flag.key} ${body.action}d`, 'success');
                    await reload(flag.key);
                });
                stateCell.append(toggle);
                const variantCell = el('td');
                const select = el('select', 'select');
                select.setAttribute('aria-label', `Default variant for ${flag.key}`);
                for (const variant of flag.variants) {
                    const option = el('option', null, variant);
                    option.value = variant;
                    option.selected = variant === flag.defaultVariant;
                    select.append(option);
                }
                select.disabled = !writable;
                select.addEventListener('change', async () => {
                    const body = { action: 'default-variant', variant: select.value };
                    if (flag.version !== null) body.expectedVersion = flag.version;
                    const receipt = await action(api, flag.key, body);
                    if (!receipt) { select.value = flag.defaultVariant ?? ''; return; }
                    if (receipt.refreshPending) { pendingNotice(panel, flag.key); return; }
                    showToast(`${flag.key} default variant saved`, 'success');
                    await reload(flag.key);
                });
                variantCell.append(select);
                row.append(keyCell, stateCell, el('td', null, flag.type), variantCell,
                    el('td', null, flag.overrides.length ? `${flag.origin} (over ${flag.overrides.join(', ')})` : flag.origin),
                    el('td', 'text-mono', flag.version ?? '--'), el('td', null, flag.targeting ? 'Targeting' : ''));
                body.append(row);
            }
        }
        rows(toolbar.getState().search, toolbar.getState().pill);
        if (selected && data.flags.some(flag => flag.key === selected)) await renderDetail(panel, api, selected, writable, reload);
    }
    async function reload(key = selected) { selected = key; await load(); }
    refreshButton.addEventListener('click', () => { void reload(); });
    await load();
}
