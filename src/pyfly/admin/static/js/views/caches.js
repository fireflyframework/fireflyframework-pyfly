/**
 * PyFly Admin — Caches View.
 *
 * Displays cache adapter status, stats, key listing with search,
 * per-key eviction, and bulk eviction with confirmation.
 *
 * Data sources:
 *   GET  /admin/api/caches               -> { available, type, stats, keys }
 *   GET  /admin/api/caches/keys          -> { keys: [...] }
 *   POST /admin/api/caches/{name}/evict  -> { cleared } | { evicted, key } | { error }
 */

import { createEmptyStateCard } from '../components/empty-state.js';
import { createFilterToolbar } from '../components/filter-toolbar.js';
import { pageSkeleton } from '../components/skeleton.js';
import { showToast } from '../components/toast.js';

/* ── Render ───────────────────────────────────────────────────── */

/**
 * Render the caches view.
 * @param {HTMLElement} container
 * @param {import('../api.js').AdminAPI} api
 */
export async function render(container, api) {
    container.replaceChildren();

    const wrapper = document.createElement('div');
    wrapper.className = 'view-enter';

    // Page header
    const header = document.createElement('div');
    header.className = 'page-header';
    const headerLeft = document.createElement('div');
    const h1 = document.createElement('h1');
    h1.textContent = 'Caches';
    headerLeft.appendChild(h1);
    const sub = document.createElement('div');
    sub.className = 'page-subtitle';
    sub.textContent = 'application.caches';
    headerLeft.appendChild(sub);
    header.appendChild(headerLeft);

    // Header right: Refresh + Evict All
    const headerRight = document.createElement('div');
    headerRight.style.display = 'flex';
    headerRight.style.gap = '8px';

    const refreshBtn = document.createElement('button');
    refreshBtn.className = 'btn btn-sm';
    refreshBtn.textContent = 'Refresh';

    const evictAllBtn = document.createElement('button');
    evictAllBtn.className = 'btn btn-danger btn-sm';
    evictAllBtn.textContent = 'Evict All';

    headerRight.appendChild(refreshBtn);
    headerRight.appendChild(evictAllBtn);
    header.appendChild(headerRight);
    wrapper.appendChild(header);

    // Loading skeleton (stat cards + keys table)
    const loader = document.createElement('div');
    loader.appendChild(pageSkeleton({ stats: 4, rows: 6 }));
    wrapper.appendChild(loader);
    container.appendChild(wrapper);

    // Fetch cache data
    let data;
    try {
        data = await api.get('/caches');
    } catch (err) {
        wrapper.removeChild(loader);
        wrapper.appendChild(createEmptyStateCard({
            icon: 'alert',
            tone: 'danger',
            title: 'Failed to load cache data',
            text: err.message,
        }));
        return;
    }

    wrapper.removeChild(loader);

    // ── Cache not available ──────────────────────────────────
    if (!data.available) {
        wrapper.appendChild(createEmptyStateCard({
            icon: 'database',
            title: 'No cache adapter configured',
            text: 'Register a CacheAdapter bean to enable caching.',
        }));
        return;
    }

    // ── Stats row ────────────────────────────────────────────
    const statsRow = document.createElement('div');
    statsRow.className = 'grid-3 mb-lg';

    // Adapter Type
    const typeCard = document.createElement('div');
    typeCard.className = 'stat-card';
    const typeContent = document.createElement('div');
    typeContent.className = 'stat-card-content';
    const typeVal = document.createElement('div');
    typeVal.className = 'stat-card-value text-mono';
    typeVal.style.fontSize = '1rem';
    typeVal.textContent = data.type || '--';
    typeContent.appendChild(typeVal);
    const typeLabel = document.createElement('div');
    typeLabel.className = 'stat-card-label';
    typeLabel.textContent = 'Adapter Type';
    typeContent.appendChild(typeLabel);
    typeCard.appendChild(typeContent);
    statsRow.appendChild(typeCard);

    // Cache Size
    const sizeCard = document.createElement('div');
    sizeCard.className = 'stat-card';
    const sizeContent = document.createElement('div');
    sizeContent.className = 'stat-card-content';
    const sizeVal = document.createElement('div');
    sizeVal.className = 'stat-card-value';
    sizeVal.textContent = data.stats?.size != null ? String(data.stats.size) : '--';
    sizeContent.appendChild(sizeVal);
    const sizeLabel = document.createElement('div');
    sizeLabel.className = 'stat-card-label';
    sizeLabel.textContent = 'Cache Size';
    sizeContent.appendChild(sizeLabel);
    sizeCard.appendChild(sizeContent);
    statsRow.appendChild(sizeCard);

    // Status
    const statusCard = document.createElement('div');
    statusCard.className = 'stat-card';
    const statusContent = document.createElement('div');
    statusContent.className = 'stat-card-content';
    const statusVal = document.createElement('div');
    statusVal.className = 'stat-card-value';
    const statusBadge = document.createElement('span');
    statusBadge.className = 'badge badge-success';
    const statusDot = document.createElement('span');
    statusDot.className = 'badge-dot';
    statusBadge.appendChild(statusDot);
    const statusText = document.createElement('span');
    statusText.textContent = 'AVAILABLE';
    statusBadge.appendChild(statusText);
    statusVal.appendChild(statusBadge);
    statusContent.appendChild(statusVal);
    const statusLabel = document.createElement('div');
    statusLabel.className = 'stat-card-label';
    statusLabel.textContent = 'Status';
    statusContent.appendChild(statusLabel);
    statusCard.appendChild(statusContent);
    statsRow.appendChild(statusCard);

    wrapper.appendChild(statsRow);

    // ── Keys table ───────────────────────────────────────────
    let keys = data.keys || [];

    const keysCard = document.createElement('div');
    keysCard.className = 'admin-card';

    const keysHeader = document.createElement('div');
    keysHeader.className = 'admin-card-header';
    const keysTitle = document.createElement('h3');
    keysTitle.textContent = 'Cache Keys';
    keysHeader.appendChild(keysTitle);
    const keysCount = document.createElement('span');
    keysCount.className = 'badge badge-neutral';
    keysCount.textContent = String(keys.length);
    keysHeader.appendChild(keysCount);
    keysCard.appendChild(keysHeader);

    // Search toolbar
    const toolbar = createFilterToolbar({
        placeholder: 'Search keys\u2026',
        onFilter: ({ search }) => renderKeys(search),
        totalCount: keys.length,
    });
    keysCard.appendChild(toolbar);

    const tableWrap = document.createElement('div');
    tableWrap.className = 'admin-table-wrapper';
    tableWrap.style.maxHeight = '400px';
    tableWrap.style.overflowY = 'auto';

    const table = document.createElement('table');
    table.className = 'admin-table';

    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    const thKey = document.createElement('th');
    thKey.textContent = 'Key';
    headRow.appendChild(thKey);
    const thAction = document.createElement('th');
    thAction.textContent = 'Action';
    thAction.style.width = '100px';
    thAction.style.textAlign = 'right';
    headRow.appendChild(thAction);
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');

    function renderKeys(search = '') {
        tbody.replaceChildren();
        const filtered = search
            ? keys.filter(k => k.toLowerCase().includes(search.toLowerCase()))
            : keys;

        toolbar.updateCount(filtered.length, keys.length);
        keysCount.textContent = String(keys.length);

        if (filtered.length === 0) {
            const tr = document.createElement('tr');
            const td = document.createElement('td');
            td.colSpan = 2;
            td.style.textAlign = 'center';
            td.style.padding = '32px 16px';
            td.style.color = 'var(--admin-text-muted)';
            td.textContent = keys.length === 0 ? 'No keys in cache' : 'No matching keys';
            tr.appendChild(td);
            tbody.appendChild(tr);
            return;
        }

        for (const key of filtered) {
            const tr = document.createElement('tr');
            const tdKey = document.createElement('td');
            tdKey.className = 'text-mono text-sm';
            tdKey.textContent = key;
            tr.appendChild(tdKey);

            const tdAction = document.createElement('td');
            tdAction.style.textAlign = 'right';
            const evictBtn = document.createElement('button');
            evictBtn.className = 'btn btn-sm';
            evictBtn.textContent = 'Evict';
            evictBtn.addEventListener('click', async () => {
                evictBtn.disabled = true;
                evictBtn.textContent = 'Evicting\u2026';
                try {
                    await api.post('/caches/default/evict', { key });
                    keys = keys.filter(k => k !== key);
                    renderKeys(toolbar.getState().search);
                    sizeVal.textContent = String(keys.length);
                    showToast(`Key "${key}" evicted`, 'success');
                } catch (err) {
                    showToast('Failed to evict key: ' + err.message, 'error');
                    evictBtn.disabled = false;
                    evictBtn.textContent = 'Evict';
                }
            });
            tdAction.appendChild(evictBtn);
            tr.appendChild(tdAction);
            tbody.appendChild(tr);
        }
    }

    renderKeys();
    table.appendChild(tbody);
    tableWrap.appendChild(table);
    keysCard.appendChild(tableWrap);
    wrapper.appendChild(keysCard);

    // ── Evict All with confirmation ──────────────────────────
    let confirmState = false;

    evictAllBtn.addEventListener('click', async () => {
        if (!confirmState) {
            confirmState = true;
            evictAllBtn.textContent = 'Confirm Evict All?';
            evictAllBtn.className = 'btn btn-danger btn-sm';
            // Auto-reset after 3 seconds
            setTimeout(() => {
                if (confirmState) {
                    confirmState = false;
                    evictAllBtn.textContent = 'Evict All';
                }
            }, 3000);
            return;
        }

        confirmState = false;
        evictAllBtn.disabled = true;
        evictAllBtn.textContent = 'Clearing\u2026';
        try {
            await api.post('/caches/default/evict', {});
            keys = [];
            renderKeys();
            sizeVal.textContent = '0';
            showToast('Cache cleared successfully', 'success');
        } catch (err) {
            showToast('Failed to clear cache: ' + err.message, 'error');
        } finally {
            evictAllBtn.disabled = false;
            evictAllBtn.textContent = 'Evict All';
        }
    });

    // ── Refresh ──────────────────────────────────────────────
    refreshBtn.addEventListener('click', async () => {
        refreshBtn.disabled = true;
        refreshBtn.textContent = 'Refreshing\u2026';
        try {
            const freshData = await api.get('/caches');
            keys = freshData.keys || [];
            sizeVal.textContent = freshData.stats?.size != null
                ? String(freshData.stats.size) : '--';
            renderKeys(toolbar.getState().search);
            showToast('Cache data refreshed', 'success');
        } catch (err) {
            showToast('Failed to refresh: ' + err.message, 'error');
        } finally {
            refreshBtn.disabled = false;
            refreshBtn.textContent = 'Refresh';
        }
    });
}
