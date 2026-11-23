/**
 * Live dashboard for the KSP career economy monitor.
 *
 * The page polls /api/dashboard every REFRESH_MS milliseconds and re-renders
 * everything from that single JSON document. Charts are updated in place
 * (no re-creation) so hovering a point survives refreshes.
 */
(() => {
  "use strict";

  const REFRESH_MS = 2000;
  /** Kerbin calendar: 6-hour days, 426-day years. */
  const KERBIN_DAY = 6 * 3600;
  const KERBIN_YEAR = 426 * KERBIN_DAY;

  const METRICS = [
    { key: "funds", label: "Funds", token: "--series-funds", digits: 0, prefix: "√" },
    { key: "science", label: "Science", token: "--series-science", digits: 1, prefix: "" },
    { key: "reputation", label: "Reputation", token: "--series-reputation", digits: 1, prefix: "" },
  ];

  const EVENT_LABELS = {
    contract_accepted: "Contract accepted",
    contract_completed: "Contract completed",
    contract_failed: "Contract failed",
    contract_cancelled: "Contract cancelled",
    contract_reverted: "Contract reverted",
    vessel_launched: "Launch",
    vessel_recovered: "Recovery",
    launch_reverted: "Launch reverted",
    other: "Other",
  };

  /** Current x-axis mode: "real" (wall clock) or "game" (KSP universal time). */
  let axisMode = "real";
  /** Width of the plotted x range, used to pick the tick granularity. */
  let xSpan = 0;
  let lastData = null;
  const charts = {};

  /**
   * Read a CSS custom property from the root element.
   * @param {string} name - Property name, including the leading dashes.
   * @returns {string} The trimmed value.
   */
  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  /**
   * Format a number with thousands separators.
   * @param {number} value - Number to format.
   * @param {number} digits - Fraction digits.
   * @param {boolean} [signed=false] - Always show the sign.
   * @returns {string} The formatted number.
   */
  function fmt(value, digits, signed = false) {
    const text = new Intl.NumberFormat("en-US", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
      signDisplay: signed ? "exceptZero" : "auto",
    }).format(value);
    return text.replace("-", "−");
  }

  /**
   * Format funds with the KSP funds symbol.
   * @param {number} value - Amount of funds.
   * @param {boolean} [signed=false] - Always show the sign.
   * @returns {string} Text such as "+√12,300".
   */
  function funds(value, signed = false) {
    const sign = value > 0.5 && signed ? "+" : value < -0.5 ? "−" : "";
    return `${sign}√${fmt(Math.abs(value), 0)}`;
  }

  /**
   * Compact axis tick formatting (0 / 1,000 / 20K / 1.5M).
   * @param {number} value - Tick value.
   * @returns {string} The tick label.
   */
  function compact(value) {
    const abs = Math.abs(value);
    if (abs >= 10000) {
      return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 })
        .format(value).replace("-", "−");
    }
    return fmt(value, abs < 10 && value % 1 !== 0 ? 1 : 0);
  }

  /**
   * Format KSP universal time as a Kerbin date.
   * @param {number} ut - Seconds since the start of the career.
   * @param {boolean} [withTime=true] - Include hours and minutes.
   * @returns {string} Text such as "Y1 D23 04:15".
   */
  function kerbinDate(ut, withTime = true) {
    const year = Math.floor(ut / KERBIN_YEAR) + 1;
    const day = Math.floor((ut % KERBIN_YEAR) / KERBIN_DAY) + 1;
    const secs = ut % KERBIN_DAY;
    const hh = String(Math.floor(secs / 3600)).padStart(2, "0");
    const mm = String(Math.floor((secs % 3600) / 60)).padStart(2, "0");
    return withTime ? `Y${year} D${day} ${hh}:${mm}` : `Y${year} D${day}`;
  }

  /**
   * Format a wall-clock timestamp.
   * @param {number} ms - Epoch milliseconds.
   * @param {boolean} [withDate=false] - Prefix the date.
   * @returns {string} Local time text.
   */
  function clock(ms, withDate = false) {
    const d = new Date(ms);
    const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    return withDate ? `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}` : time;
  }

  /**
   * Chart.js plugin that draws a vertical crosshair at the hovered x position.
   */
  const crosshair = {
    id: "crosshair",
    afterDatasetsDraw(chart) {
      const active = chart.tooltip && chart.tooltip.getActiveElements();
      if (!active || !active.length) return;
      const x = active[0].element.x;
      const { top, bottom } = chart.chartArea;
      const ctx = chart.ctx;
      ctx.save();
      ctx.strokeStyle = token("--baseline");
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, top);
      ctx.lineTo(x, bottom);
      ctx.stroke();
      ctx.restore();
    },
  };

  /**
   * Create one single-series line chart (one metric per chart: never two y-axes).
   * @param {object} metric - Entry of METRICS.
   * @returns {Chart} The Chart.js instance.
   */
  function createChart(metric) {
    const ctx = document.getElementById(`chart-${metric.key}`);
    return new Chart(ctx, {
      type: "line",
      data: { datasets: [{ label: metric.label, data: [], parsing: false }] },
      plugins: [crosshair],
      options: {
        animation: false,
        maintainAspectRatio: false,
        interaction: { mode: "nearest", axis: "x", intersect: false },
        // Balances change in discrete jumps, so a stepped line tells the truth.
        elements: {
          line: { borderWidth: 2, stepped: "after", borderJoinStyle: "round", borderCapStyle: "round" },
          point: { radius: 0, hoverRadius: 5, hitRadius: 8, hoverBorderWidth: 2 },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            displayColors: false,
            callbacks: {
              title: (items) => xLabel(items[0].raw.x, true),
              label: (item) => metric.key === "funds"
                ? funds(item.raw.y)
                : `${metric.label}: ${fmt(item.raw.y, metric.digits)}`,
              afterLabel: (item) => (item.raw.vessel ? item.raw.vessel : ""),
            },
          },
        },
        scales: {
          x: {
            type: "linear",
            grid: { display: false },
            border: {},
            ticks: { maxTicksLimit: 5, maxRotation: 0, callback: (v) => xLabel(v, false) },
          },
          y: {
            border: { display: false },
            ticks: { maxTicksLimit: 5, callback: compact },
          },
        },
      },
    });
  }

  /**
   * Label an x value according to the current axis mode.
   * @param {number} x - Epoch ms (real) or UT seconds (game).
   * @param {boolean} detailed - Long form for tooltips.
   * @returns {string} The label.
   */
  function xLabel(x, detailed) {
    if (axisMode === "game") return kerbinDate(x, detailed);
    if (detailed) return clock(x, true);
    // Short sessions need seconds, or every tick would read the same minute;
    // multi-day histories need the date instead.
    const d = new Date(x);
    if (xSpan < 10 * 60 * 1000) return clock(x);
    if (xSpan > 24 * 3600 * 1000) {
      return d.toLocaleDateString([], { month: "short", day: "numeric" });
    }
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  /** Apply theme colors to every chart (also called when the OS theme flips). */
  function applyTheme() {
    Chart.defaults.font.family = token("--font") || "system-ui";
    Chart.defaults.color = token("--text-muted");
    for (const metric of METRICS) {
      const chart = charts[metric.key];
      if (!chart) continue;
      const color = token(metric.token);
      const ds = chart.data.datasets[0];
      ds.borderColor = color;
      ds.pointBackgroundColor = color;
      ds.pointHoverBorderColor = token("--surface");
      chart.options.scales.y.grid = { color: token("--grid") };
      chart.options.scales.x.border.color = token("--baseline");
      chart.update("none");
    }
  }

  /**
   * Push the history into the charts.
   * @param {Array<object>} samples - History points from the API.
   */
  function renderCharts(samples) {
    // In game mode, reverts make UT go backwards; sort so the line stays readable.
    const xKey = axisMode === "game" ? "ut" : "t";
    xSpan = samples.length > 1 ? samples[samples.length - 1].t - samples[0].t : 0;
    for (const metric of METRICS) {
      const points = samples.map((s) => ({ x: s[xKey], y: s[metric.key], vessel: s.vessel }));
      if (axisMode === "game") points.sort((a, b) => a.x - b.x);
      const chart = charts[metric.key];
      chart.data.datasets[0].data = points;
      chart.update("none");
    }
  }

  /**
   * Update the connection pill.
   * @param {object} status - Status object from the API.
   */
  function renderStatus(status) {
    const el = document.getElementById("status");
    el.classList.toggle("ok", status.connected);
    el.classList.toggle("down", !status.connected);
    el.querySelector(".status-text").textContent = status.connected
      ? status.message
      : `Not connected — ${status.message}`;
  }

  /**
   * Update the hero figure with the "earning or losing" answer.
   * @param {object} summary - Ledger summary from the API.
   * @param {boolean} hasData - Whether any sample exists yet.
   */
  function renderHero(summary, hasData) {
    const value = document.getElementById("hero-value");
    const verdict = document.getElementById("hero-verdict");
    const formula = document.getElementById("hero-formula");
    if (!hasData) return;
    const balance = summary.mission_balance;
    value.textContent = funds(balance, true);

    // Icon + words carry the meaning; the color only reinforces it.
    if (Math.abs(balance) < 0.5) {
      verdict.innerHTML = "No contract or vessel money recorded yet.";
    } else if (balance > 0) {
      verdict.innerHTML = '<span class="verdict-icon up" aria-hidden="true">▲</span>' +
        "<span>You are <strong>earning</strong> money: contracts pay more than your vessels cost.</span>";
    } else {
      verdict.innerHTML = '<span class="verdict-icon down" aria-hidden="true">▼</span>' +
        "<span>You are <strong>losing</strong> money: vessels cost more than contracts pay.</span>";
    }
    formula.textContent =
      `Contracts ${funds(summary.contracts_net, true)} · Vessels ${funds(summary.vessels_net, true)}` +
      ` (launches ${funds(summary.launch_costs, true)}, recovered ${funds(summary.recoveries, true)})` +
      ` · Other, not counted ${funds(summary.other, true)}`;
  }

  /**
   * Update the three stat tiles.
   * @param {object|null} current - Latest sample.
   * @param {object|null} baseline - First sample.
   */
  function renderTiles(current, baseline) {
    if (!current) return;
    for (const metric of METRICS) {
      const tile = document.querySelector(`.tile[data-metric="${metric.key}"]`);
      const now = current[metric.key];
      tile.querySelector(".tile-value").textContent =
        metric.key === "funds" ? funds(now) : fmt(now, metric.digits);
      if (baseline) {
        const delta = now - baseline[metric.key];
        const text = metric.key === "funds" ? funds(delta, true) : fmt(delta, metric.digits, true);
        const cls = delta > 0 ? "up" : delta < 0 ? "down" : "";
        tile.querySelector(".tile-delta").innerHTML =
          `<span class="${cls}">${text}</span> since ${clock(baseline.t, true)}`;
      }
    }
  }

  /**
   * Draw the diverging breakdown bars.
   * @param {object} summary - Ledger summary from the API.
   */
  function renderBreakdown(summary) {
    const items = summary.breakdown;
    const max = Math.max(1, ...items.map((i) => Math.abs(i.funds)));
    const rows = items.map((item) => {
      const width = (Math.abs(item.funds) / max) * 50; // half the track per side
      const side = item.funds >= 0 ? "pos" : "neg";
      return `<div class="bd-row" title="${item.label}: ${funds(item.funds, true)}">
        <span class="bd-label">${item.label}</span>
        <span class="bd-track"><span class="bd-bar ${side}" style="width:${width}%"></span></span>
        <span class="bd-value">${funds(item.funds, true)}</span>
      </div>`;
    });
    rows.push(`<div class="bd-row bd-total">
        <span class="bd-label">Net change</span><span></span>
        <span class="bd-value">${funds(summary.total, true)}</span>
      </div>`);
    document.getElementById("breakdown").innerHTML = rows.join("");
  }

  /**
   * Escape text for safe insertion into HTML (vessel and contract names).
   * @param {string} text - Raw text.
   * @returns {string} Escaped text.
   */
  function esc(text) {
    return String(text ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  /**
   * Fill the per-vessel table.
   * @param {Array<object>} vessels - Vessel economics from the API.
   */
  function renderVessels(vessels) {
    const body = document.getElementById("vessels");
    if (!vessels.length) {
      body.innerHTML = '<tr><td colspan="5" class="empty">No launches yet.</td></tr>';
      return;
    }
    body.innerHTML = vessels.map((v) => `<tr>
        <td>${esc(v.name)}</td>
        <td class="num">${v.launches}</td>
        <td class="num">${funds(v.cost, true)}</td>
        <td class="num">${funds(v.recovered, true)}</td>
        <td class="num"><strong>${funds(v.net, true)}</strong></td>
      </tr>`).join("");
  }

  /**
   * Fill the ledger table.
   * @param {Array<object>} events - Latest events, newest first.
   */
  function renderEvents(events) {
    const body = document.getElementById("events");
    if (!events.length) {
      body.innerHTML = '<tr><td colspan="7" class="empty">Nothing recorded yet.</td></tr>';
      return;
    }
    const signed = (value, digits) =>
      Math.abs(value) < 1e-9 ? '<span class="muted">—</span>' : fmt(value, digits, true);
    body.innerHTML = events.map((e) => {
      const group = e.kind.startsWith("contract_") ? "contract"
        : ["vessel_launched", "vessel_recovered", "launch_reverted"].includes(e.kind) ? "vessel" : "";
      return `<tr>
        <td class="num">${clock(e.t)}</td>
        <td class="num">${kerbinDate(e.ut)}</td>
        <td><span class="kind ${group}">${EVENT_LABELS[e.kind] || e.kind}</span></td>
        <td>${esc(e.description)}</td>
        <td class="num">${Math.abs(e.funds) < 0.5 ? '<span class="muted">—</span>' : funds(e.funds, true)}</td>
        <td class="num">${signed(e.science, 1)}</td>
        <td class="num">${signed(e.reputation, 1)}</td>
      </tr>`;
    }).join("");
  }

  /**
   * Render the whole page from one API response.
   * @param {object} data - Dashboard JSON.
   */
  function render(data) {
    lastData = data;
    renderStatus(data.status);
    renderHero(data.summary, Boolean(data.current));
    renderTiles(data.current, data.baseline);
    renderCharts(data.samples);
    renderBreakdown(data.summary);
    renderVessels(data.summary.vessels);
    renderEvents(data.events);
  }

  /** Fetch the dashboard and schedule the next refresh. */
  async function refresh() {
    try {
      const response = await fetch("/api/dashboard", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
    } catch (error) {
      renderStatus({ connected: false, message: `dashboard server unreachable (${error.message})` });
    } finally {
      setTimeout(refresh, REFRESH_MS);
    }
  }

  /** Wire the real/game time toggle. */
  function setupAxisToggle() {
    for (const button of document.querySelectorAll(".segmented button")) {
      button.addEventListener("click", () => {
        axisMode = button.dataset.axis;
        for (const b of document.querySelectorAll(".segmented button")) {
          b.setAttribute("aria-checked", String(b === button));
        }
        if (lastData) renderCharts(lastData.samples);
      });
    }
  }

  // Boot.
  for (const metric of METRICS) charts[metric.key] = createChart(metric);
  applyTheme();
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);
  setupAxisToggle();
  refresh();
})();
