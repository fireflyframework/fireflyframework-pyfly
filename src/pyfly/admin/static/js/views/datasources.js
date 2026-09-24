/** Explicitly registered resources, backed by the application's existing models. */
import { button, element, formatValue, resourceForm } from '../components/resource-form.js';

export async function render(container, api, { signal } = {}) {
    const state = new URLSearchParams(location.hash.split('?')[1] || '');
    const resource = state.get('resource');
    const mode = state.get('mode') || 'list';
    const id = state.get('id');
    let editor;
    let dialog;
    let busy = false;
    const options = { signal };
    function navigate(changes) {
        const next = new URLSearchParams(state);
        for (const [key, value] of Object.entries(changes)) {
            if (value === null) next.delete(key); else next.set(key, String(value));
        }
        location.hash = 'datasources' + (next.size ? '?' + next : '');
    }
    const heading = element('div', undefined, 'data-heading');
    const titles = element('div');
    titles.append(element('p', 'APPLICATION DATA', 'data-eyebrow'), element('h1', 'Datasources'));
    heading.appendChild(titles);
    container.appendChild(heading);
    const status = element('div', '', 'data-status');
    status.setAttribute('role', 'alert');
    container.appendChild(status);
    function failure(err) {
        if (signal?.aborted) return;
        status.replaceChildren(element('p', err.status === 401 ? 'Sign in to administer application data.' : err.message));
        if (err.status === 409) status.appendChild(button('Reload record', () => {
            if (!editor?.isDirty() || confirm('Discard your changes and reload this record?')) {
                editor?.saved();
                location.reload();
            }
        }));
        editor?.showErrors(err.errors || []);
    }
    try {
        if (!resource) {
            titles.appendChild(element('p', 'Browse the models your application has registered for administration.', 'data-caption'));
            const result = await api.get('/data/sources', options);
            if (signal?.aborted) return;
            if (!result.sources.length) container.appendChild(element('p', 'No resources are registered.', 'empty-state'));
            for (const source of result.sources) {
                const section = element('section', undefined, 'data-source');
                section.appendChild(element('h2', source.id));
                const grid = element('div', undefined, 'data-resources');
                for (const item of source.resources) grid.appendChild(button(item.label,
                    () => navigate({ resource: item.id, source: source.id, page: null, search: null, id: null, mode: null }), 'data-resource'));
                section.appendChild(grid);
                container.appendChild(section);
            }
        } else {
            const base = `/data/resources/${encodeURIComponent(resource)}`;
            const schema = await api.get(`${base}/schema`, options);
            if (signal?.aborted) return;
            titles.querySelector('h1').textContent = schema.label;
            titles.appendChild(element('p', state.get('source') || 'Application datasource', 'data-caption'));
            heading.appendChild(button('All datasources', () => navigate({ resource: null, source: null, mode: null, id: null, page: null, search: null, sort: null, filters: null })));
            if (mode === 'create' || mode === 'edit') {
                const record = mode === 'edit' ? await api.get(`${base}/records/${encodeURIComponent(id)}`, options) : null;
                editor = await resourceForm(schema, record, api, signal);
                if (signal?.aborted) return;
                container.appendChild(element('h2', mode === 'edit' ? 'Edit record' : 'Create record', 'data-section-title'));
                container.appendChild(editor.form);
                const actions = element('div', undefined, 'data-actions');
                const save = element('button', 'Save', 'btn btn-primary');
                save.type = 'submit';
                actions.append(save, button('Cancel', () => navigate({ mode: null, id: null })));
                editor.form.appendChild(actions);
                editor.form.addEventListener('submit', async event => {
                    event.preventDefault();
                    if (busy) return;
                    const values = editor.values();
                    if (values === null) return;
                    busy = true;
                    save.disabled = true;
                    status.textContent = '';
                    try {
                        if (record) await api.patch(`${base}/records/${encodeURIComponent(id)}`, { values, editToken: record.edit_token }, options);
                        else await api.post(`${base}/records`, { values }, options);
                        editor.saved();
                        navigate({ mode: null, id: null });
                    } catch (err) { failure(err); }
                    finally { busy = false; save.disabled = false; }
                });
                editor.form.querySelector('input, select, textarea')?.focus();
            } else if (id) {
                const record = await api.get(`${base}/records/${encodeURIComponent(id)}`, options);
                if (signal?.aborted) return;
                const details = element('dl', undefined, 'data-details');
                for (const field of schema.fields) details.append(element('dt', field.label), element('dd', formatValue(record.values[field.name])));
                container.appendChild(details);
                const actions = element('div', undefined, 'data-actions');
                actions.appendChild(button('Back to records', () => navigate({ id: null, mode: null })));
                if (schema.operations.includes('update')) actions.appendChild(button('Edit', () => navigate({ mode: 'edit' }), 'btn btn-primary'));
                if (schema.operations.includes('delete')) actions.appendChild(button('Delete', () => {
                    dialog = element('dialog', undefined, 'data-dialog');
                    dialog.setAttribute('aria-labelledby', 'delete-title');
                    dialog.append(element('h2', 'Delete record?'));
                    dialog.firstChild.id = 'delete-title';
                    const label = Object.entries(record.values).find(([key, value]) => key !== 'id' && typeof value === 'string');
                    dialog.append(element('p', `Delete “${label ? label[1] : record.id}”? This action cannot be undone.`));
                    const error = element('p', '', 'data-field-error');
                    error.setAttribute('role', 'alert');
                    dialog.appendChild(error);
                    const cancel = button('Cancel', () => { dialog.close(); dialog.remove(); });
                    const confirmDelete = button('Confirm delete', async () => {
                        if (busy) return;
                        busy = true;
                        confirmDelete.disabled = true;
                        try {
                            await api.delete(`${base}/records/${encodeURIComponent(id)}`, { ...options, headers: { 'If-Match': record.edit_token } });
                            dialog.close(); dialog.remove();
                            navigate({ id: null, mode: null });
                        } catch (err) { error.textContent = err.message; }
                        finally { busy = false; confirmDelete.disabled = false; }
                    }, 'btn data-danger');
                    const buttons = element('div', undefined, 'data-actions');
                    buttons.append(cancel, confirmDelete);
                    dialog.appendChild(buttons);
                    container.appendChild(dialog);
                    dialog.showModal();
                    cancel.focus();
                }, 'btn data-danger'));
                container.appendChild(actions);
            } else {
                const toolbar = element('form', undefined, 'data-toolbar');
                const search = element('input');
                search.type = 'search'; search.placeholder = 'Search records'; search.value = state.get('search') || '';
                search.setAttribute('aria-label', 'Search records');
                if (schema.searchFields.length) toolbar.append(search, button('Search', () => navigate({ search: search.value, page: 1 })));
                toolbar.addEventListener('submit', event => { event.preventDefault(); navigate({ search: search.value, page: 1 }); });
                let filters = {};
                try { filters = JSON.parse(state.get('filters') || '{}'); } catch (_) { /* Invalid state resets to unfiltered. */ }
                for (const name of schema.filterFields) {
                    const field = schema.fields.find(f => f.name === name);
                    const input = element('input'); input.value = filters[name] ?? '';
                    input.placeholder = `Filter ${field.label}`; input.setAttribute('aria-label', `Filter ${field.label}`);
                    input.addEventListener('change', () => {
                        if (input.value) filters[name] = input.value; else delete filters[name];
                        navigate({ filters: JSON.stringify(filters), page: 1 });
                    });
                    toolbar.appendChild(input);
                }
                if (schema.operations.includes('create')) toolbar.appendChild(button('Create', () => navigate({ mode: 'create', id: null }), 'btn btn-primary'));
                container.appendChild(toolbar);
                const query = new URLSearchParams({ page: state.get('page') || 1, size: schema.pageSize,
                    search: state.get('search') || '', sort: state.get('sort') || '', filters: JSON.stringify(filters) });
                const result = await api.get(`${base}/records?${query}`, options);
                if (signal?.aborted) return;
                container.appendChild(element('p', `${result.total} record${result.total === 1 ? '' : 's'}`, 'data-caption'));
                if (!result.items.length) container.appendChild(element('p', 'No records found.', 'empty-state'));
                else {
                    const wrap = element('div', undefined, 'data-table-wrap');
                    const table = element('table', undefined, 'data-table');
                    const header = element('tr');
                    for (const field of schema.fields) {
                        const cell = element('th'); cell.scope = 'col';
                        cell.appendChild(button(field.label, () => navigate({ sort: state.get('sort') === field.name ? '-' + field.name : field.name, page: 1 }), 'data-sort'));
                        header.appendChild(cell);
                    }
                    const thead = element('thead'); thead.appendChild(header); table.appendChild(thead);
                    const tbody = element('tbody');
                    for (const record of result.items) {
                        const row = element('tr');
                        const labelField = schema.fields.find(f => f.type === 'string' && f.name !== 'id')?.name || schema.fields[0].name;
                        for (const field of schema.fields) {
                            const cell = element('td');
                            if (field.name === labelField && schema.operations.includes('read')) cell.appendChild(button(formatValue(record.values[field.name]), () => navigate({ id: record.id }), 'data-record-link'));
                            else cell.textContent = formatValue(record.values[field.name]);
                            row.appendChild(cell);
                        }
                        tbody.appendChild(row);
                    }
                    table.appendChild(tbody); wrap.appendChild(table); container.appendChild(wrap);
                }
                const pagination = element('div', undefined, 'data-pagination');
                const previous = button('Previous', () => navigate({ page: result.page - 1 })); previous.disabled = result.page <= 1;
                const next = button('Next', () => navigate({ page: result.page + 1 })); next.disabled = result.page * result.size >= result.total;
                pagination.append(previous, element('span', `Page ${result.page} of ${Math.max(1, Math.ceil(result.total / result.size))}`), next);
                container.appendChild(pagination);
            }
        }
    } catch (err) { failure(err); }
    function beforeUnload(event) { if (editor?.isDirty()) { event.preventDefault(); event.returnValue = ''; } }
    window.addEventListener('beforeunload', beforeUnload);
    const cleanup = () => { dialog?.remove(); window.removeEventListener('beforeunload', beforeUnload); };
    cleanup.canLeave = () => !busy && (!editor?.isDirty() || confirm('Discard your unsaved changes?'));
    return cleanup;
}
