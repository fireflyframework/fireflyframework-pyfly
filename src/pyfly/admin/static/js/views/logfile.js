/**
 * PyFly Admin — Log Viewer.
 *
 * Real-time log viewer with SSE live tail, level filtering,
 * pause/resume, clear, and auto-scroll controls.
 *
 * Data sources:
 *   GET  /admin/api/logfile       -> { available, records: [...], total }
 *   POST /admin/api/logfile/clear -> { cleared }
 *   SSE  /admin/api/sse/logfile   -> event type "log"
 */

import { createEmptyStateCard } from '../components/empty-state.js';
import { createFilterToolbar } from '../components/filter-toolbar.js';
import { attachFullscreen } from '../components/fullscreen.js';
import { pageSkeleton } from '../components/skeleton.js';
import { showToast } from '../components/toast.js';
import { sse } from '../sse.js';

/* ── Helpers ──────────────────────────────────────────────────── */

function levelBadgeClass(level) {
    switch ((level || '').toUpperCase()) {
        case 'ERROR':   case 'CRITICAL': return 'log-badge-error';
        case 'WARNING': return 'log-badge-warning';
        case 'INFO':    return 'log-badge-info';
        case 'DEBUG':   return 'log-badge-debug';
        default:        return 'log-badge-debug';
    }
}

function formatTimestamp(ts) {
    if (!ts) return '';
    try {
        const d = new Date(ts);
        return d.toLocaleTimeString(undefined, {
            hour: '2-digit', minute: '2-digit', second: '2-digit',
            fractionalSecondDigits: 3,
        });
    } catch (_) {
        return ts;
    }
}

function createLogLine(record) {
    const div = document.createElement('div');
    div.className = 'log-line';

    // Timestamp
    const timeSpan = document.createElement('span');
    timeSpan.className = 'log-timestamp';
    timeSpan.textContent = formatTimestamp(record.timestamp);
    div.appendChild(timeSpan);

    // Level badge
    const levelSpan = document.createElement('span');
    levelSpan.className = `log-badge ${levelBadgeClass(record.level)}`;
    levelSpan.textContent = (record.level || 'INFO');
    div.appendChild(levelSpan);

    // Logger name
    const loggerSpan = document.createElement('span');
    loggerSpan.className = 'log-logger';
    loggerSpan.textContent = record.logger || '';
    div.appendChild(loggerSpan);

    // Event / message
    const eventSpan = document.createElement('span');
    eventSpan.className = 'log-event';
    eventSpan.textContent = record.message || '';
    div.appendChild(eventSpan);

    // Context (key=value pairs)
    if (record.context) {
        const ctxSpan = document.createElement('span');
        ctxSpan.className = 'log-context';
        ctxSpan.textContent = record.context;
        div.appendChild(ctxSpan);
    }

    return div;
}

/* ── Render ───────────────────────────────────────────────────── */

/**
 * Render the log file viewer.
 * @param {HTMLElement} container
 * @param {import('../api.js').AdminAPI} api
 * @returns {function} Cleanup function to disconnect SSE.
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
    h1.textContent = 'Log Viewer';
    headerLeft.appendChild(h1);
    const sub = document.createElement('div');
    sub.className = 'page-subtitle';
    sub.textContent = 'application.logfile';
    headerLeft.appendChild(sub);
    header.appendChild(headerLeft);

    // Header right: Pause/Resume + Clear buttons
    const headerRight = document.createElement('div');
    headerRight.style.display = 'flex';
    headerRight.style.gap = '8px';
    headerRight.style.alignItems = 'center';

    // Auto-scroll toggle
    const autoScrollCheck = document.createElement('input');
    autoScrollCheck.type = 'checkbox';
    autoScrollCheck.id = 'auto-scroll-toggle';
    autoScrollCheck.checked = true;
    autoScrollCheck.style.cursor = 'pointer';
    const autoScrollLabel = document.createElement('label');
    autoScrollLabel.htmlFor = 'auto-scroll-toggle';
    autoScrollLabel.className = 'text-sm';
    autoScrollLabel.style.cursor = 'pointer';
    autoScrollLabel.style.userSelect = 'none';
    autoScrollLabel.textContent = 'Auto-scroll';
    headerRight.appendChild(autoScrollCheck);
    headerRight.appendChild(autoScrollLabel);

    const pauseBtn = document.createElement('button');
    pauseBtn.className = 'btn btn-sm';
    pauseBtn.textContent = 'Pause';

    const clearBtn = document.createElement('button');
    clearBtn.className = 'btn btn-sm';
    clearBtn.textContent = 'Clear';

    headerRight.appendChild(pauseBtn);
    headerRight.appendChild(clearBtn);
    header.appendChild(headerRight);
    wrapper.appendChild(header);

    // Loading skeleton (two stat cards + a log table)
    const loader = document.createElement('div');
    loader.appendChild(pageSkeleton({ stats: 2, rows: 8 }));
    wrapper.appendChild(loader);
    container.appendChild(wrapper);

    // Fetch initial data
    let data;
    try {
        data = await api.get('/logfile');
    } catch (err) {
        wrapper.removeChild(loader);
        wrapper.appendChild(createEmptyStateCard({
            icon: 'alert',
            tone: 'danger',
            title: 'Failed to load log data',
            text: err.message,
        }));
        return;
    }

    wrapper.removeChild(loader);

    if (!data.available) {
        wrapper.appendChild(createEmptyStateCard({
            icon: 'plug',
            title: 'Log viewing not configured',
            text: 'Enable the in-memory log handler to stream application logs here.',
        }));
        return;
    }

    // Mutable records array
    const records = data.records || [];
    let activeFilter = 'all';

    // ── Stats row ────────────────────────────────────────────
    const statsRow = document.createElement('div');
    statsRow.className = 'grid-2 mb-lg';

    const totalCard = document.createElement('div');
    totalCard.className = 'stat-card';
    const totalContent = document.createElement('div');
    totalContent.className = 'stat-card-content';
    const totalVal = document.createElement('div');
    totalVal.className = 'stat-card-value';
    totalVal.textContent = String(records.length);
    totalContent.appendChild(totalVal);
    const totalLabel = document.createElement('div');
    totalLabel.className = 'stat-card-label';
    totalLabel.textContent = 'Total Records';
    totalContent.appendChild(totalLabel);
    totalCard.appendChild(totalContent);
    statsRow.appendChild(totalCard);

    const liveCard = document.createElement('div');
    liveCard.className = 'stat-card';
    const liveContent = document.createElement('div');
    liveContent.className = 'stat-card-content';
    const liveVal = document.createElement('div');
    liveVal.className = 'stat-card-value';
    const liveBadge = document.createElement('span');
    liveBadge.className = 'badge badge-success';
    const liveDot = document.createElement('span');
    liveDot.className = 'badge-dot';
    liveBadge.appendChild(liveDot);
    const liveText = document.createElement('span');
    liveText.textContent = 'LIVE';
    liveBadge.appendChild(liveText);
    liveVal.appendChild(liveBadge);
    liveContent.appendChild(liveVal);
    const liveLabel = document.createElement('div');
    liveLabel.className = 'stat-card-label';
    liveLabel.textContent = 'Stream Status';
    liveContent.appendChild(liveLabel);
    liveCard.appendChild(liveContent);
    statsRow.appendChild(liveCard);

    wrapper.appendChild(statsRow);

    // ── Filter toolbar ───────────────────────────────────────
    const toolbar = createFilterToolbar({
        placeholder: 'Search logs\u2026',
        pills: [
            { label: 'All', value: 'all' },
            { label: 'ERROR', value: 'ERROR' },
            { label: 'WARNING', value: 'WARNING' },
            { label: 'INFO', value: 'INFO' },
            { label: 'DEBUG', value: 'DEBUG' },
        ],
        onFilter: ({ pill }) => {
            activeFilter = pill;
            renderLogLines();
        },
        totalCount: records.length,
    });
    wrapper.appendChild(toolbar);

    // ── Log output card ──────────────────────────────────────
    const logCard = document.createElement('div');
    logCard.className = 'admin-card';

    const logHeader = document.createElement('div');
    logHeader.className = 'admin-card-header';
    const logTitle = document.createElement('h3');
    logTitle.textContent = 'Log Output';
    logHeader.appendChild(logTitle);
    logCard.appendChild(logHeader);

    const logOutput = document.createElement('div');
    logOutput.className = 'logfile-output';

    function renderLogLines() {
        logOutput.replaceChildren();
        const filtered = activeFilter === 'all'
            ? records
            : records.filter(r => r.level === activeFilter);
        if (filtered.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'log-line';
            empty.style.color = 'var(--admin-text-muted)';
            empty.textContent = activeFilter === 'all'
                ? 'No log records yet\u2026'
                : `No ${activeFilter} records`;
            logOutput.appendChild(empty);
        } else {
            for (const record of filtered) {
                logOutput.appendChild(createLogLine(record));
            }
        }
        toolbar.updateCount(filtered.length, records.length);
        if (autoScrollCheck.checked) {
            logOutput.scrollTop = logOutput.scrollHeight;
        }
    }

    renderLogLines();
    logCard.appendChild(logOutput);
    wrapper.appendChild(logCard);

    // Expand the log output to fullscreen (button lives in the card header).
    const fullscreen = attachFullscreen(logCard, {
        anchor: logHeader,
        label: 'Expand log output',
        onResize: () => {
            if (autoScrollCheck.checked) logOutput.scrollTop = logOutput.scrollHeight;
        },
    });

    function updateStats() {
        totalVal.textContent = String(records.length);
    }

    // ── SSE: Real-time log updates ───────────────────────────
    let paused = false;

    sse.connectTyped('/logfile', 'log', (logRecord) => {
        if (paused) return;
        records.push(logRecord);
        // Append to output if it passes the active filter
        if (activeFilter === 'all' || logRecord.level === activeFilter) {
            // Remove the "no records" placeholder if present
            const first = logOutput.firstChild;
            if (first && first.textContent.startsWith('No ')) {
                logOutput.replaceChildren();
            }
            logOutput.appendChild(createLogLine(logRecord));
            toolbar.updateCount(
                logOutput.querySelectorAll('.log-line').length,
                records.length,
            );
        }
        updateStats();
        if (autoScrollCheck.checked) {
            logOutput.scrollTop = logOutput.scrollHeight;
        }
    });

    // ── Pause/Resume ─────────────────────────────────────────
    pauseBtn.addEventListener('click', () => {
        paused = !paused;
        pauseBtn.textContent = paused ? 'Resume' : 'Pause';
        if (paused) {
            liveText.textContent = 'PAUSED';
            liveBadge.className = 'badge badge-warning';
        } else {
            liveText.textContent = 'LIVE';
            liveBadge.className = 'badge badge-success';
        }
    });

    // ── Clear ────────────────────────────────────────────────
    clearBtn.addEventListener('click', async () => {
        clearBtn.disabled = true;
        clearBtn.textContent = 'Clearing\u2026';
        try {
            await api.post('/logfile/clear', {});
            records.length = 0;
            renderLogLines();
            updateStats();
            showToast('Log records cleared', 'success');
        } catch (err) {
            showToast('Failed to clear logs: ' + err.message, 'error');
        } finally {
            clearBtn.disabled = false;
            clearBtn.textContent = 'Clear';
        }
    });

    // ── Cleanup ──────────────────────────────────────────────
    return function cleanup() {
        sse.disconnect('/logfile');
        fullscreen.destroy();
    };
}
