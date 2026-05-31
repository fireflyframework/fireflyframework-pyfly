/**
 * PyFly Admin — Scheduled Tasks View.
 *
 * Displays registered scheduled tasks with trigger type detection
 * and a stat card summary.
 *
 * Data source:
 *   GET /admin/api/scheduled -> { tasks: [...], total: N }
 */

import { createEmptyStateCard } from '../components/empty-state.js';
import { pageSkeleton } from '../components/skeleton.js';
import { createTable } from '../components/table.js';

/* ── Helpers ──────────────────────────────────────────────────── */

/**
 * Detect the trigger type for a scheduled task.
 * @param {object} task
 * @returns {string}
 */
function detectTriggerType(task) {
    if (task.cron != null && task.cron !== 'None' && task.cron !== '') {
        return 'Cron';
    }
    if (task.fixed_rate != null && task.fixed_rate !== 'None' && task.fixed_rate !== '') {
        return 'Fixed Rate';
    }
    if (task.fixed_delay != null && task.fixed_delay !== 'None' && task.fixed_delay !== '') {
        return 'Fixed Delay';
    }
    return 'Unknown';
}

/**
 * Get the trigger expression value for a scheduled task.
 * @param {object} task
 * @returns {string}
 */
function getTriggerExpression(task) {
    if (task.cron != null && task.cron !== 'None' && task.cron !== '') {
        return task.cron;
    }
    if (task.fixed_rate != null && task.fixed_rate !== 'None' && task.fixed_rate !== '') {
        return task.fixed_rate;
    }
    if (task.fixed_delay != null && task.fixed_delay !== 'None' && task.fixed_delay !== '') {
        return task.fixed_delay;
    }
    return '--';
}

/* ── Render ───────────────────────────────────────────────────── */

/**
 * Render the scheduled tasks view.
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
    h1.textContent = 'Scheduled Tasks';
    headerLeft.appendChild(h1);
    const sub = document.createElement('div');
    sub.className = 'page-subtitle';
    sub.textContent = 'application.scheduled';
    headerLeft.appendChild(sub);
    header.appendChild(headerLeft);
    wrapper.appendChild(header);

    // Loading skeleton (one stat card + a table)
    const loader = document.createElement('div');
    loader.appendChild(pageSkeleton({ stats: 1, rows: 6 }));
    wrapper.appendChild(loader);
    container.appendChild(wrapper);

    // Fetch scheduled tasks
    let data;
    try {
        data = await api.get('/scheduled');
    } catch (err) {
        wrapper.removeChild(loader);
        wrapper.appendChild(createEmptyStateCard({
            icon: 'alert',
            tone: 'danger',
            title: 'Failed to load scheduled tasks',
            text: err.message,
        }));
        return;
    }

    wrapper.removeChild(loader);

    const tasks = data.tasks || [];
    const total = data.total != null ? data.total : tasks.length;

    // ── Stat card ────────────────────────────────────────────────
    const statsRow = document.createElement('div');
    statsRow.className = 'grid-4 mb-lg';

    const totalCard = document.createElement('div');
    totalCard.className = 'stat-card';
    const totalContent = document.createElement('div');
    totalContent.className = 'stat-card-content';
    const totalVal = document.createElement('div');
    totalVal.className = 'stat-card-value';
    totalVal.textContent = String(total);
    totalContent.appendChild(totalVal);
    const totalLabel = document.createElement('div');
    totalLabel.className = 'stat-card-label';
    totalLabel.textContent = 'Scheduled Tasks';
    totalContent.appendChild(totalLabel);
    totalCard.appendChild(totalContent);
    statsRow.appendChild(totalCard);

    wrapper.appendChild(statsRow);

    // ── Empty state ──────────────────────────────────────────────
    if (total === 0) {
        wrapper.appendChild(createEmptyStateCard({
            icon: 'inbox',
            title: 'No scheduled tasks',
            text: 'No @scheduled methods are registered in this application.',
        }));
        return;
    }

    // ── Table ────────────────────────────────────────────────────
    const tableCard = document.createElement('div');
    tableCard.className = 'admin-card';

    const tableHeader = document.createElement('div');
    tableHeader.className = 'admin-card-header';
    const tableTitle = document.createElement('h3');
    tableTitle.textContent = 'Registered Tasks';
    tableHeader.appendChild(tableTitle);
    tableCard.appendChild(tableHeader);

    const tableBody = document.createElement('div');
    tableBody.className = 'admin-card-body';
    tableBody.style.padding = '0';

    const tableEl = createTable({
        columns: [
            {
                key: 'class',
                label: 'Class',
                render(val) {
                    const span = document.createElement('span');
                    span.className = 'mono';
                    span.textContent = val || '--';
                    return span;
                },
            },
            {
                key: 'method',
                label: 'Method',
                render(val) {
                    const span = document.createElement('span');
                    span.className = 'mono';
                    span.textContent = val || '--';
                    return span;
                },
            },
            {
                key: '_trigger_type',
                label: 'Trigger Type',
                render(_val, row) {
                    const triggerType = detectTriggerType(row);
                    const badge = document.createElement('span');
                    badge.className = 'badge';
                    if (triggerType === 'Cron') {
                        badge.classList.add('badge-info');
                    } else if (triggerType === 'Fixed Rate') {
                        badge.classList.add('badge-success');
                    } else if (triggerType === 'Fixed Delay') {
                        badge.classList.add('badge-warning');
                    } else {
                        badge.classList.add('badge-neutral');
                    }
                    badge.textContent = triggerType;
                    return badge;
                },
            },
            {
                key: '_expression',
                label: 'Expression',
                render(_val, row) {
                    const expr = getTriggerExpression(row);
                    const span = document.createElement('span');
                    span.className = 'mono';
                    span.textContent = expr;
                    return span;
                },
            },
        ],
        data: tasks,
        searchable: true,
        sortable: true,
        emptyText: 'No scheduled tasks',
    });

    tableBody.appendChild(tableEl);
    tableCard.appendChild(tableBody);
    wrapper.appendChild(tableCard);
}
