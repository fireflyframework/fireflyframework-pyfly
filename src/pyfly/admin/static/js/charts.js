/**
 * PyFly Admin — Chart Library (Chart.js Adapters).
 *
 * Wraps Chart.js instances with PyFly theme integration.
 * All colours are read from CSS custom properties so they
 * match the active theme automatically.
 *
 * Exports both factory functions (createLineChart, etc.)
 * and backward-compatible classes (LineChart, etc.).
 */

/* global Chart */

/* ── Helpers ──────────────────────────────────────────────────── */

export function cssVar(name) {
    return getComputedStyle(document.documentElement)
        .getPropertyValue(name)
        .trim();
}

export function hexToRgba(hex, alpha) {
    if (!hex || !hex.startsWith('#')) return `rgba(100, 100, 100, ${alpha})`;
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function resolveColor(colorVar) {
    if (!colorVar) return cssVar('--admin-primary') || '#5fbf2e';
    if (colorVar.startsWith('#')) return colorVar;
    return cssVar(colorVar) || cssVar('--admin-primary') || '#5fbf2e';
}

function themeDefaults() {
    return {
        text: cssVar('--admin-text') || '#e2e8f0',
        muted: cssVar('--admin-text-muted') || '#64748b',
        grid: cssVar('--admin-border-subtle') || '#162032',
        font: cssVar('--admin-font-mono') || 'monospace',
    };
}

/* ── Factory Functions ────────────────────────────────────────── */

/**
 * Create a Chart.js line chart for time-series data.
 * @param {HTMLCanvasElement} canvas
 * @param {object} options
 * @returns {{ update(data, labels), destroy(), instance: Chart }}
 */
export function createLineChart(canvas, options = {}) {
    const theme = themeDefaults();
    const color = resolveColor(options.color);

    const chart = new Chart(canvas, {
        type: 'line',
        data: {
            labels: options.labels || [],
            datasets: [{
                label: options.label || '',
                data: options.data || [],
                borderColor: color,
                backgroundColor: hexToRgba(color, 0.1),
                fill: options.fill !== false,
                tension: 0.4,
                pointRadius: 3,
                pointBackgroundColor: color,
                borderWidth: 2,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 300 },
            scales: {
                x: {
                    grid: { color: theme.grid },
                    ticks: { color: theme.muted, font: { family: theme.font, size: 10 } },
                },
                y: {
                    beginAtZero: true,
                    grid: { color: theme.grid },
                    ticks: { color: theme.muted, font: { family: theme.font, size: 11 } },
                },
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: cssVar('--admin-card-bg') || '#1a1a2e',
                    titleColor: theme.text,
                    bodyColor: theme.muted,
                    borderColor: theme.grid,
                    borderWidth: 1,
                },
            },
        },
    });

    return {
        update(data, labels) {
            chart.data.datasets[0].data = data;
            if (labels) chart.data.labels = labels;
            chart.update('none');
        },
        destroy() { chart.destroy(); },
        instance: chart,
    };
}

/**
 * Create a Chart.js bar chart.
 * @param {HTMLCanvasElement} canvas
 * @param {object} options
 * @returns {{ update(data, labels), destroy(), instance: Chart }}
 */
export function createBarChart(canvas, options = {}) {
    const theme = themeDefaults();
    const defaultColor = cssVar('--admin-primary') || '#5fbf2e';
    const colors = (options.colors || []).map(c => resolveColor(c));

    const chart = new Chart(canvas, {
        type: 'bar',
        data: {
            labels: options.labels || [],
            datasets: [{
                data: options.data || [],
                backgroundColor: colors.length > 0
                    ? colors.map(c => hexToRgba(c, 0.8))
                    : hexToRgba(defaultColor, 0.8),
                borderColor: colors.length > 0 ? colors : defaultColor,
                borderWidth: 1,
                borderRadius: 4,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 300 },
            scales: {
                x: {
                    grid: { display: false },
                    ticks: { color: theme.muted, font: { family: theme.font, size: 10 } },
                },
                y: {
                    beginAtZero: true,
                    grid: { color: theme.grid },
                    ticks: { color: theme.muted, font: { family: theme.font, size: 11 } },
                },
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: cssVar('--admin-card-bg') || '#1a1a2e',
                    titleColor: theme.text,
                    bodyColor: theme.muted,
                },
            },
        },
    });

    return {
        update(data, labels) {
            chart.data.datasets[0].data = data;
            if (labels) chart.data.labels = labels;
            chart.update('none');
        },
        destroy() { chart.destroy(); },
        instance: chart,
    };
}

/**
 * Create a Chart.js doughnut chart.
 * @param {HTMLCanvasElement} canvas
 * @param {object} options
 * @returns {{ update(data, labels), destroy(), getColors(), instance: Chart }}
 */
export function createDonutChart(canvas, options = {}) {
    const defaultColorVars = [
        '--admin-primary', '--admin-success', '--admin-warning',
        '--admin-info', '--admin-danger', '--admin-text-muted',
    ];
    const colorVars = options.colors || defaultColorVars;
    const colors = colorVars.map(c => resolveColor(c));

    const chart = new Chart(canvas, {
        type: 'doughnut',
        data: {
            labels: options.labels || [],
            datasets: [{
                data: options.data || [],
                backgroundColor: colors,
                borderWidth: 0,
                hoverOffset: 4,
            }],
        },
        options: {
            responsive: false,
            cutout: '62%',
            animation: { animateRotate: true, duration: 600 },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: cssVar('--admin-card-bg') || '#1a1a2e',
                    titleColor: cssVar('--admin-text') || '#e2e8f0',
                    bodyColor: cssVar('--admin-text-muted') || '#64748b',
                },
            },
        },
        plugins: options.centerValue ? [{
            id: 'centerText',
            afterDraw(chart) {
                const { ctx, width, height } = chart;
                ctx.save();
                // Value
                ctx.fillStyle = cssVar('--admin-text') || '#e2e8f0';
                const fontSize = Math.round(Math.min(width, height) * 0.17);
                ctx.font = `700 ${fontSize}px ${cssVar('--admin-font-mono') || 'monospace'}`;
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                const cy = options.centerLabel ? height / 2 - 6 : height / 2;
                ctx.fillText(options.centerValue, width / 2, cy);
                // Label
                if (options.centerLabel) {
                    ctx.fillStyle = cssVar('--admin-text-muted') || '#64748b';
                    const labelSize = Math.round(Math.min(width, height) * 0.065);
                    ctx.font = `500 ${labelSize}px ${cssVar('--admin-font-sans') || 'sans-serif'}`;
                    ctx.fillText(options.centerLabel, width / 2, height / 2 + height * 0.12);
                }
                ctx.restore();
            },
        }] : [],
    });

    return {
        update(data, labels) {
            chart.data.datasets[0].data = data;
            if (labels) chart.data.labels = labels;
            chart.update();
        },
        destroy() { chart.destroy(); },
        getColors() { return [...colors]; },
        instance: chart,
    };
}

/**
 * Create a Chart.js gauge chart (doughnut with 270° arc).
 * @param {HTMLCanvasElement} canvas
 * @param {object} options
 * @returns {{ update(value), destroy(), instance: Chart }}
 */
export function createGaugeChart(canvas, options = {}) {
    const thresholds = options.thresholds || { warning: 60, danger: 80 };
    let currentValue = Math.max(0, Math.min(100, options.value || 0));

    function getColor(val) {
        if (val >= thresholds.danger) return cssVar('--admin-danger') || '#f43f5e';
        if (val >= thresholds.warning) return cssVar('--admin-warning') || '#f59e0b';
        return cssVar('--admin-success') || '#10b981';
    }

    const chart = new Chart(canvas, {
        type: 'doughnut',
        data: {
            datasets: [{
                data: [currentValue, 100 - currentValue],
                backgroundColor: [getColor(currentValue), cssVar('--admin-border-subtle') || '#162032'],
                borderWidth: 0,
            }],
        },
        options: {
            responsive: false,
            circumference: 270,
            rotation: -135,
            cutout: '80%',
            animation: { duration: 600, easing: 'easeOutCubic' },
            plugins: {
                legend: { display: false },
                tooltip: { enabled: false },
            },
        },
        plugins: [{
            id: 'gaugeText',
            afterDraw(chart) {
                const { ctx, width, height } = chart;
                ctx.save();
                // Value
                ctx.fillStyle = cssVar('--admin-text') || '#e2e8f0';
                const size = Math.min(width, height);
                ctx.font = `700 ${Math.round(size * 0.2)}px ${cssVar('--admin-font-mono') || 'monospace'}`;
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText(`${Math.round(currentValue)}%`, width / 2, height / 2 - 4);
                // Label
                if (options.label) {
                    ctx.fillStyle = cssVar('--admin-text-muted') || '#64748b';
                    ctx.font = `500 ${Math.round(size * 0.075)}px ${cssVar('--admin-font-sans') || 'sans-serif'}`;
                    ctx.fillText(options.label, width / 2, height / 2 + size * 0.16);
                }
                ctx.restore();
            },
        }],
    });

    return {
        update(value) {
            currentValue = Math.max(0, Math.min(100, value));
            chart.data.datasets[0].data = [currentValue, 100 - currentValue];
            chart.data.datasets[0].backgroundColor = [getColor(currentValue), cssVar('--admin-border-subtle') || '#162032'];
            chart.update();
        },
        destroy() { chart.destroy(); },
        instance: chart,
    };
}

/* ── Backward-Compatible Class Exports ───────────────────────── */

/**
 * LineChart class — wraps createLineChart for backward compatibility.
 * Existing views that do `new LineChart(canvas, options)` continue to work.
 */
export class LineChart {
    constructor(canvas, options = {}) {
        this.canvas = canvas;
        this.options = options;
        this._adapter = createLineChart(canvas, options);
    }
    update(data, labels) { this._adapter.update(data, labels); }
    destroy() { this._adapter.destroy(); }
}

export class BarChart {
    constructor(canvas, options = {}) {
        this.canvas = canvas;
        this.options = options;
        this._adapter = createBarChart(canvas, options);
    }
    update(data, labels) { this._adapter.update(data, labels); }
    destroy() { this._adapter.destroy(); }
}

export class DonutChart {
    constructor(canvas, options = {}) {
        this.canvas = canvas;
        this.options = options;
        this._adapter = createDonutChart(canvas, {
            data: options.data,
            labels: options.labels,
            colors: options.colors,
            centerValue: options.centerValue,
            centerLabel: options.centerLabel,
        });
    }
    draw() { /* Chart.js auto-draws */ }
    getColors() { return this._adapter.getColors(); }
    destroy() { this._adapter.destroy(); }
}

export class GaugeChart {
    constructor(canvas, options = {}) {
        this.canvas = canvas;
        this.options = options;
        this._adapter = createGaugeChart(canvas, options);
    }
    update(value) { this._adapter.update(value); }
    destroy() { this._adapter.destroy(); }
}
