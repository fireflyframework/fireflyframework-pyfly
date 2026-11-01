/** Authorized read-only journal pages. A scope handle and a cursor never grant access. */
import { createTable } from '../components/table.js';
import { pageSkeleton } from '../components/skeleton.js';

function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
}
function button(text, action) {
    const element = node('button', 'btn', text);
    element.type = 'button';
    element.addEventListener('click', action);
    return element;
}

export async function render(container, api, { signal } = {}) {
    const root = node('div', 'audit-view');
    const header = node('div', 'page-header');
    const title = node('div');
    title.append(node('h1', '', 'Audit journal'), node('p', 'page-subtitle', 'Committed facts in one currently authorized scope.'));
    const refresh = button('Refresh', () => load());
    header.append(title, refresh);
    const form = node('form', 'filter-toolbar mb-lg');
    const label = node('label', '', 'Scope handle');
    const scope = node('input', 'input');
    scope.type = 'text';
    scope.maxLength = 128;
    scope.placeholder = 'Authorized default';
    label.append(scope);
    const apply = node('button', 'btn', 'Load scope');
    apply.type = 'submit';
    form.append(label, apply);
    const content = node('div');
    root.append(header, form, content);
    container.replaceChildren(root);
    let version = 0;
    let history = [];

    function details(entry, target) {
        const region = node('section', 'admin-card mb-lg');
        region.tabIndex = -1;
        region.setAttribute('role', 'region');
        region.setAttribute('aria-label', 'Audit entry details');
        const body = node('div', 'admin-card-body');
        body.append(node('h2', '', `Entry ${entry.sequence}`));
        for (const [name, value] of [
            ['Event', entry.event.event_type], ['Outcome', entry.event.outcome],
            ['Actor', `${entry.event.actor.kind}: ${entry.event.actor.subject_id}`],
            ['Resource', `${entry.event.resource.kind}: ${entry.event.resource.resource_id}`],
            ['Revision', entry.event.resource.revision ?? 'Not reported'],
            ['Reason codes', entry.event.reason_codes.join(', ') || 'None'],
            ['Recorded', entry.recorded_at], ['Entry hash', entry.entry_hash], ['Previous hash', entry.previous_hash],
        ]) {
            body.append(node('p', 'text-muted', name), node('div', 'audit-fact', String(value)));
        }
        region.append(body);
        target.replaceChildren(region);
        region.focus();
    }

    async function load(after = 0, through = null, previous = []) {
        const current = ++version;
        const handle = scope.value.trim();
        refresh.disabled = apply.disabled = true;
        content.replaceChildren(pageSkeleton({ stats: 0, rows: 5 }));
        const query = new URLSearchParams({ after: String(after), limit: '25' });
        if (handle) query.set('scope', handle);
        if (through != null) query.set('through', String(through));
        try {
            const page = await api.get('/audit?' + query, { signal });
            if (signal?.aborted || current !== version) return;
            history = previous;
            content.replaceChildren();
            const summary = node('div', 'admin-card-body');
            const heading = node('h2', '', page.scope.project_id);
            summary.append(heading, node('p', 'text-secondary', `Tenant ${page.scope.tenant_id} · Workspace ${page.scope.workspace_id}`),
                node('p', 'text-muted', `Snapshot through sequence ${page.snapshot_through_sequence}. Current head ${page.head.sequence}. No independent anchor has been checked.`));
            const status = node('p', 'text-secondary');
            status.setAttribute('role', 'status');
            status.setAttribute('aria-live', 'polite');
            const detail = node('div');
            const table = createTable({
                columns: [
                    { key: 'sequence', label: 'Sequence', render(value, entry) { return button(`Entry ${value}`, () => details(entry, detail)); } },
                    { key: 'event', label: 'Event', render(value) { return node('span', '', value.event_type); } },
                    { key: 'event', label: 'Outcome', render(value) { return node('span', '', value.outcome); } },
                    { key: 'recorded_at', label: 'Recorded', render(value) { return node('span', '', new Date(value).toLocaleString()); } },
                ], data: page.entries, emptyText: 'No committed facts in this authorized scope',
            });
            const actions = node('div', 'filter-toolbar mb-lg');
            const back = button('Previous page', () => {
                const prior = history[history.length - 1];
                load(prior.after, prior.through, history.slice(0, -1));
            });
            back.disabled = !history.length;
            const next = button('Next page', () => load(page.next_after_sequence, page.snapshot_through_sequence,
                [...history, { after, through: page.snapshot_through_sequence }]));
            next.disabled = !page.has_more;
            const verify = button('Verify displayed range', async () => {
                verify.disabled = true;
                const parameters = new URLSearchParams({ first: String(page.entries[0].sequence), last: String(page.entries.at(-1).sequence) });
                if (handle) parameters.set('scope', handle);
                try {
                    await api.get('/audit/verify?' + parameters, { signal });
                    if (signal?.aborted || current !== version) return;
                    status.textContent = 'Range verified. No independent anchor was supplied.';
                } catch (error) {
                    if (signal?.aborted || current !== version) return;
                    status.textContent = error.status === 403 ? 'Range verification is forbidden. Refresh to check current access.' : 'Range verification is unavailable. Refresh and try again.';
                    if (error.status === 403 || error.status === 401) {
                        const denied = node('div', 'admin-card-body', 'Audit access is forbidden. Refresh to check your current scope and access.');
                        denied.setAttribute('role', 'alert');
                        content.replaceChildren(denied);
                    }
                } finally {
                    verify.disabled = !page.can_verify || !page.entries.length;
                }
            });
            verify.disabled = !page.can_verify || !page.entries.length;
            actions.append(back, next, verify);
            summary.append(status);
            const card = node('section', 'admin-card mb-lg');
            card.append(summary, table);
            content.append(card, actions, detail, node('p', 'text-muted', 'Read-only journal view. Range checks do not establish external custody or WORM retention. Archive delivery and OpenTelemetry query status are not read by this view.'));
        } catch (error) {
            if (signal?.aborted || current !== version) return;
            const alert = node('div', 'admin-card-body');
            alert.setAttribute('role', 'alert');
            const message = error.status === 403 ? 'Audit access is forbidden' : error.status === 401 ? 'Sign in to read the audit journal' : error.status === 404 ? 'Audit reader is not configured' : 'Could not load the audit journal';
            alert.append(node('h2', '', message), node('p', '', 'Check the authorized scope and your current access, then refresh.'));
            content.replaceChildren(alert);
        } finally {
            if (current === version) refresh.disabled = apply.disabled = false;
        }
    }
    form.addEventListener('submit', event => { event.preventDefault(); load(); });
    await load();
    return () => { version++; };
}
