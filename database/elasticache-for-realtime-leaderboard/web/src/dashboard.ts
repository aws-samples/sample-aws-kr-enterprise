/**
 * Dashboard section — fetches CloudWatch metrics and renders sparkline cards.
 * Polls every 10 seconds via GET /admin/metrics.
 */

import { fetchMetrics, MetricData } from "./api";

interface MetricCardConfig {
  id: string;
  title: string;
  unit: string;
  color: string;
}

const METRIC_CARDS: MetricCardConfig[] = [
  { id: "sqs_depth", title: "SQS Depth", unit: "msgs", color: "#FF9F0A" },
  { id: "lambda_invocations", title: "Lambda Invocations", unit: "/min", color: "#0A84FF" },
  { id: "lambda_errors", title: "Lambda Errors", unit: "errs", color: "#FF453A" },
  { id: "valkey_cpu", title: "Valkey CPU", unit: "%", color: "#30D158" },
  { id: "e2e_latency", title: "E2E Latency", unit: "ms", color: "#BF5AF2" },
];

const POLL_INTERVAL_MS = 10_000;

let pollTimer: ReturnType<typeof setInterval> | null = null;

/**
 * Render a responsive SVG sparkline.
 * Uses viewBox + preserveAspectRatio=none so the SVG scales to its CSS width
 * without ever causing layout overflow. Stroke is pinned to 1.5px via
 * vector-effect so the line thickness stays consistent across container widths.
 */
function renderSparkline(values: number[], color: string): string {
  if (values.length === 0) {
    return `<svg viewBox="0 0 100 32" preserveAspectRatio="none" class="sparkline"><text x="50" y="18" text-anchor="middle" fill="var(--color-label-tertiary)" font-size="9">No data</text></svg>`;
  }

  const max = Math.max(...values, 1); // avoid division by 0
  const min = Math.min(...values, 0);
  const range = max - min || 1;

  const pad = 1;
  const w = 100 - pad * 2;
  const h = 32 - pad * 2;

  const points = values.map((v, i) => {
    const x = pad + (i / Math.max(values.length - 1, 1)) * w;
    const y = pad + h - ((v - min) / range) * h;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });

  const polyline = points.join(" ");

  const areaPoints = [
    `${pad},${pad + h}`,
    ...points,
    `${pad + w},${pad + h}`,
  ].join(" ");

  return `<svg viewBox="0 0 100 32" preserveAspectRatio="none" class="sparkline">
    <polygon points="${areaPoints}" fill="${color}" opacity="0.18"/>
    <polyline points="${polyline}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
  </svg>`;
}

/**
 * Return the most recent non-nullish value, walking back through the series.
 * CloudWatch sometimes lags on the very latest bucket — fallback prevents '--' flicker.
 */
function latestValue(values: number[]): number | null {
  for (let i = values.length - 1; i >= 0; i--) {
    const v = values[i];
    if (v !== null && v !== undefined && !Number.isNaN(v)) return v;
  }
  return null;
}

/**
 * Format the latest metric value for display.
 */
function formatValue(values: number[], unit: string): string {
  const latest = latestValue(values);
  if (latest === null) return "--";

  if (unit === "ms") {
    return latest < 1000 ? `${latest.toFixed(0)}` : `${(latest / 1000).toFixed(1)}K`;
  }
  if (unit === "%") {
    return latest.toFixed(1);
  }
  if (latest >= 1_000_000) return `${(latest / 1_000_000).toFixed(1)}M`;
  if (latest >= 1_000) return `${(latest / 1_000).toFixed(1)}K`;
  return latest.toFixed(0);
}

/**
 * Build the static dashboard HTML grid.
 * Card layout: [dot + label][delta slot] / [value + inline unit] / [sparkline].
 */
function buildDashboardHTML(): string {
  const cards = METRIC_CARDS.map(
    (card) => `
    <div class="metric-card" id="metric-${card.id}">
      <div class="metric-card__header">
        <span class="metric-card__label">
          <span class="metric-card__dot" style="background:${card.color}"></span>
          ${card.title}
        </span>
        <span class="metric-card__delta metric-card__delta--flat" id="delta-${card.id}" hidden></span>
      </div>
      <div class="metric-card__value-row">
        <span class="metric-card__value" id="value-${card.id}">--</span>
        <span class="metric-card__unit">${card.unit}</span>
      </div>
      <div class="metric-card__sparkline" id="spark-${card.id}"></div>
    </div>`
  ).join("");

  return `<div class="metrics-grid">${cards}</div>
    <div class="metrics-footer">
      <span class="metrics-status" id="metrics-status">Fetching...</span>
    </div>`;
}

/**
 * Update a single metric card with fresh data.
 */
function updateCard(config: MetricCardConfig, data: MetricData | undefined): void {
  const valueEl = document.getElementById(`value-${config.id}`);
  const sparkEl = document.getElementById(`spark-${config.id}`);

  if (!valueEl || !sparkEl) return;

  const values = data?.values ?? [];
  valueEl.textContent = formatValue(values, config.unit);
  sparkEl.innerHTML = renderSparkline(values, config.color);
}

/**
 * Fetch metrics and update all cards.
 */
async function refreshMetrics(): Promise<void> {
  const statusEl = document.getElementById("metrics-status");

  try {
    const metrics = await fetchMetrics();

    for (const card of METRIC_CARDS) {
      const data = metrics[card.id as keyof typeof metrics];
      updateCard(card, data);
    }

    if (statusEl) {
      const now = new Date();
      statusEl.textContent = `Updated ${now.toLocaleTimeString()}`;
      statusEl.classList.remove("metrics-error");
    }
  } catch {
    if (statusEl) {
      statusEl.textContent = "Failed to fetch metrics";
      statusEl.classList.add("metrics-error");
    }
  }
}

/**
 * Initialize the dashboard: render cards and start polling.
 */
export function initDashboard(): void {
  const section = document.getElementById("dashboard-section");
  if (!section) return;

  // Replace the static link with the metrics grid
  const heading = section.querySelector("h2");
  section.innerHTML = "";
  if (heading) section.appendChild(heading);
  section.insertAdjacentHTML("beforeend", buildDashboardHTML());

  // Initial fetch
  refreshMetrics();

  // Poll every 10s
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(refreshMetrics, POLL_INTERVAL_MS);
}
