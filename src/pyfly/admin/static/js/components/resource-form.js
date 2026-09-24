/** Typed, accessible controls built exclusively from authorized field metadata. */
export function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
}

export function button(label, action, className = 'btn btn-secondary') {
    const node = element('button', label, className);
    node.type = 'button';
    node.addEventListener('click', action);
    return node;
}

export function formatValue(value) {
    if (value === null || value === undefined) return '—';
    return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

export async function resourceForm(schema, record, api, signal) {
    const form = element('form', undefined, 'data-form');
    const controls = new Map();
    let dirty = false;
    form.addEventListener('input', () => { dirty = true; });
    form.addEventListener('change', () => { dirty = true; });
    for (const field of schema.fields.filter(f => !f.read_only)) {
        const group = element('div', undefined, 'data-field');
        const label = element('label', field.label);
        label.htmlFor = `field-${field.name}`;
        let input;
        if (field.choices.length || field.relation) {
            input = element('select');
            input.appendChild(new Option('Choose a value', ''));
            for (const choice of field.choices) input.appendChild(new Option(String(choice), String(choice)));
        } else if (field.type === 'json') {
            input = element('textarea');
            input.rows = 6;
        } else {
            input = element('input');
            input.type = ({ boolean: 'checkbox', integer: 'text', number: 'number', date: 'date', datetime: 'datetime-local' })[field.type] || 'text';
            if (field.type === 'integer') { input.inputMode = 'numeric'; input.pattern = '-?[0-9]+'; }
            if (field.type === 'number') input.step = 'any';
            if (field.type === 'decimal') input.inputMode = 'decimal';
        }
        input.id = label.htmlFor;
        input.name = field.name;
        input.required = field.required && field.type !== 'boolean';
        const original = record?.values[field.name];
        if (field.type === 'boolean') input.checked = original === true;
        else if (original !== null && original !== undefined) {
            input.value = field.type === 'json' ? JSON.stringify(original, null, 2) : String(original);
            if (field.type === 'datetime') input.value = new Date(original).toISOString().slice(0, 23);
        }
        group.append(label, input);
        if (field.type === 'datetime') group.appendChild(element('span', 'Times are displayed in UTC.', 'data-caption'));
        let nullInput;
        if (field.nullable) {
            const nullLabel = element('label', undefined, 'data-null');
            nullInput = element('input');
            nullInput.type = 'checkbox';
            nullInput.checked = original === null;
            input.disabled = nullInput.checked;
            nullInput.addEventListener('change', () => { input.disabled = nullInput.checked; });
            nullLabel.append(nullInput, document.createTextNode(' Leave empty (null)'));
            group.appendChild(nullLabel);
        }
        const error = element('span', '', 'data-field-error');
        error.id = input.id + '-error';
        input.setAttribute('aria-describedby', error.id);
        group.appendChild(error);
        controls.set(field.name, { field, input, nullInput, error });
        form.appendChild(group);
        if (field.relation) {
            let page = 1;
            let relationVersion = 0;
            const navigation = element('div', undefined, 'data-actions');
            const search = element('input');
            search.type = 'search';
            search.setAttribute('aria-label', `Search ${field.label} choices`);
            const previous = button('Previous choices', () => { page--; load(); });
            const next = button('Next choices', () => { page++; load(); });
            async function load() {
                const version = ++relationVersion;
                try {
                    const params = new URLSearchParams({ page, size: 25, search: search.value });
                    const result = await api.get(`/data/resources/${encodeURIComponent(field.relation)}/records?${params}`, { signal });
                    if (signal?.aborted || version !== relationVersion) return;
                    const selected = input.value || String(original ?? '');
                    input.replaceChildren(new Option('Choose a value', ''));
                    if (selected) input.appendChild(new Option(selected, selected));
                    for (const item of result.items) {
                        if (item.id === selected) continue;
                        const value = Object.entries(item.values).find(([key, v]) => key !== 'id' && typeof v === 'string');
                        input.appendChild(new Option(value ? value[1] : item.id, item.id));
                    }
                    input.value = selected;
                    previous.disabled = page <= 1;
                    next.disabled = page * result.size >= result.total;
                } catch (err) { if (!signal?.aborted) error.textContent = err.message; }
            }
            const find = button('Find choices', () => { page = 1; load(); });
            navigation.append(search, find, previous, next);
            group.appendChild(navigation);
            await load();
        }
    }
    function values() {
        const result = {};
        let valid = true;
        for (const [name, control] of controls) {
            const { field, input, nullInput, error } = control;
            error.textContent = '';
            input.removeAttribute('aria-invalid');
            try {
                if (nullInput?.checked) result[name] = null;
                else if (field.type === 'boolean') result[name] = input.checked;
                else if (input.value === '' && !field.required && !record) continue;
                else if (field.type === 'json') result[name] = JSON.parse(input.value);
                else if (field.type === 'datetime') result[name] = new Date(input.value + 'Z').toISOString();
                else if (field.type === 'integer') {
                    if (!/^-?[0-9]+$/.test(input.value)) throw new Error('Enter an integer.');
                    result[name] = input.value;
                } else if (field.type === 'number') {
                    if (input.value === '' || !Number.isFinite(Number(input.value))) throw new Error('Enter a number.');
                    result[name] = Number(input.value);
                } else if (field.choices.length) result[name] = field.choices.find(v => String(v) === input.value);
                else result[name] = input.value;
            } catch (_) {
                error.textContent = field.type === 'json' ? 'Enter valid JSON.' : 'Enter a valid value.';
                input.setAttribute('aria-invalid', 'true');
                valid = false;
            }
        }
        if (!valid) { form.querySelector('[aria-invalid]')?.focus(); return null; }
        return result;
    }
    function showErrors(errors) {
        for (const error of errors) {
            const control = controls.get(error.loc?.[0]);
            if (!control) continue;
            control.error.textContent = error.msg;
            control.input.setAttribute('aria-invalid', 'true');
        }
        form.querySelector('[aria-invalid]')?.focus();
    }
    return { form, values, showErrors, isDirty: () => dirty, saved: () => { dirty = false; } };
}
