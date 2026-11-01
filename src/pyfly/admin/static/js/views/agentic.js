/** Application-owned Agentic discovery and numeric usage. All actions are read-only. */
import { createTable } from '../components/table.js';
import { pageSkeleton } from '../components/skeleton.js';

function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
}
function card(title, content) {
    const section = node('section', 'admin-card mb-lg');
    const header = node('div', 'admin-card-header');
    header.append(node('h2', '', title));
    section.append(header, content);
    return section;
}
function metric(label, value) {
    const item = node('div', 'stat-card');
    const content = node('div', 'stat-card-content');
    content.append(node('div', 'stat-card-value', value), node('div', 'stat-card-label', label));
    item.append(content);
    return item;
}
const number = value => Number(value || 0).toLocaleString();
const money = value => '$' + Number(value || 0).toFixed(4);
function costEstimate(usage) {
    if (!usage?.record_count || usage.cost_status === 'unavailable') return 'No observations';
    const amount = usage.known_cost_usd;
    if (!['known', 'partial'].includes(usage.cost_status) || amount == null
        || !Number.isFinite(Number(amount)) || Number(amount) < 0) return 'Unknown';
    return money(amount) + (usage.cost_status === 'partial' ? ' (partial)' : '');
}

export async function render(container, api, { signal } = {}) {
    const wrapper = node('div', 'agentic-view');
    const header = node('div', 'page-header');
    const heading = node('div');
    heading.append(node('h1', '', 'Agentic'), node('p', 'page-subtitle', 'Agents, tools and usage in this application.'));
    const refresh = node('button', 'btn', 'Refresh');
    refresh.type = 'button';
    header.append(heading, refresh);
    const body = node('div');
    wrapper.append(header, body);
    container.replaceChildren(wrapper);
    let requestVersion = 0;

    async function load() {
        const version = ++requestVersion;
        refresh.disabled = true;
        const loading = node('div');
        loading.setAttribute('role', 'status');
        loading.setAttribute('aria-label', 'Loading Agentic data');
        loading.append(pageSkeleton({ stats: 4, rows: 4 }));
        body.replaceChildren(loading);
        try {
            const data = await api.get('/agentic', { signal });
            if (signal?.aborted || version !== requestVersion) return;
            body.replaceChildren();
            const stats = node('div', 'grid-4 mb-lg');
            stats.append(metric('Agents', number(data.agents.length)), metric('Tools', number(data.tools.length)),
                metric('Workflows', number(data.workflows.length)), metric('Model requests', number(data.usage.total_requests)));
            body.append(stats);
            const usage = node('div', 'admin-card-body agentic-metrics');
            for (const [label, value] of [
                ['Input tokens', number(data.usage.total_input_tokens)],
                ['Output tokens', number(data.usage.total_output_tokens)],
                ['Total tokens', number(data.usage.total_tokens)],
                ['Estimated cost', costEstimate(data.usage)],
                ['Total latency', number(Math.round(data.usage.total_latency_ms || 0)) + ' ms'],
                ['Retained calls', number(data.usage.record_count)],
            ]) {
                const item = node('div');
                item.append(node('div', 'text-muted', label), node('strong', '', value));
                usage.append(item);
            }
            body.append(card('Usage', usage));
            const note = node('p', 'text-muted mb-lg', 'Usage shows retained observations in this process. Costs are estimates; unknown pricing is excluded from the known subtotal.');
            body.append(note);
            const quota = node('div', 'admin-card-body');
            quota.append(node('p', '', data.quota.enabled ? 'Quota management enabled' : 'Quota management disabled'));
            if (data.quota.enabled) {
                quota.append(node('p', 'text-secondary', data.quota.daily_budget_usd == null
                    ? 'No daily budget configured' : `Daily budget: ${money(data.quota.daily_budget_usd)}`));
                for (const [index, limit] of (data.quota.rate_limits || []).entries()) {
                    quota.append(node('p', 'text-secondary', `Rate limit ${index + 1}: ${limit.remaining} of ${limit.limit} requests remaining per minute`));
                }
            }
            body.append(card('Quota', quota));
            const details = node('div');
            async function showDetails(kind, name) {
                const region = node('section', 'admin-card mb-lg');
                region.setAttribute('role', 'region');
                region.setAttribute('aria-label', kind === 'agents' ? 'Agent details' : 'Tool details');
                region.setAttribute('tabindex', '-1');
                const detailBody = node('div', 'admin-card-body');
                detailBody.append(node('p', '', 'Loading details…'));
                region.append(detailBody);
                details.replaceChildren(region);
                region.focus();
                try {
                    const detail = await api.get(`/agentic/${kind}/${encodeURIComponent(name)}`, { signal });
                    if (signal?.aborted || version !== requestVersion) return;
                    detailBody.replaceChildren(node('h3', '', detail.name));
                    detailBody.append(node('p', '', `Tags: ${(detail.tags || []).join(', ') || 'None'}`));
                    if (kind === 'agents') {
                        detailBody.append(node('p', '', `Version: ${detail.version}`));
                        detailBody.append(node('p', '', `Model requests: ${number(detail.usage.total_requests)} · Total tokens: ${number(detail.usage.total_tokens)} · Estimated cost: ${costEstimate(detail.usage)}`));
                    } else {
                        detailBody.append(node('p', '', `Parameters: ${number(detail.parameter_count)}`));
                    }
                } catch (err) {
                    if (signal?.aborted) return;
                    detailBody.textContent = 'Could not load details. Refresh and select the item again.';
                    detailBody.setAttribute('role', 'alert');
                }
            }
            for (const [kind, title, columns] of [
                ['agents', 'Agents', [{ key: 'version', label: 'Version' }]],
                ['tools', 'Tools', [{ key: 'parameter_count', label: 'Parameters' }]],
                ['workflows', 'Workflows', [{ key: 'status', label: 'Status' }]],
            ]) {
                const tableBody = node('div', 'admin-card-body');
                const table = createTable({
                    columns: [{ key: 'name', label: 'Name', render(value) {
                        if (kind === 'workflows') return node('span', '', value);
                        const button = node('button', 'btn btn-sm', value);
                        button.type = 'button';
                        button.addEventListener('click', () => showDetails(kind, value));
                        return button;
                    } }, ...columns, ...(kind === 'workflows' ? [] : [{ key: 'tags', label: 'Tags', render(value) {
                        return node('span', 'text-secondary', (value || []).join(', ') || '—');
                    } }])],
                    data: data[kind], searchable: true, emptyText: `No ${kind} registered`,
                });
                table.querySelector('input')?.setAttribute('aria-label', `Filter ${kind}`);
                tableBody.append(table);
                body.append(card(title, tableBody));
            }
            body.append(node('p', 'text-muted mb-lg', 'Workflows show registered definitions. Execution history is not collected by this page.'), details);
        } catch (err) {
            if (signal?.aborted || version !== requestVersion) return;
            const error = node('div', 'admin-card-body');
            error.setAttribute('role', 'alert');
            error.append(node('h2', '', 'Could not load Agentic data'), node('p', '', 'Check your admin access and application runtime, then try again.'));
            const retry = node('button', 'btn btn-primary', 'Try again');
            retry.type = 'button';
            retry.addEventListener('click', load);
            error.append(retry);
            body.replaceChildren(error);
        } finally {
            if (version === requestVersion) refresh.disabled = false;
        }
    }
    refresh.addEventListener('click', load);
    await load();
    return () => { requestVersion++; };
}
