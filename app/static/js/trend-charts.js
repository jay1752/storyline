// Table/trend toggle on the analytics page — Quarterly Quality Matrix (per layer),
// Profit & Loss, and Balance Sheet. Data comes from JSON script tags the route
// already builds from the same series the table view renders (no second data
// source, just a second presentation of it). Gaps (null) are drawn as breaks in
// the line, never interpolated or zeroed — same "only show what's real" rule the
// tables already follow.
(function () {
  // Labels/period names come from this app's own hardcoded series definitions, not
  // user input — escaped anyway as defense-in-depth before going into innerHTML.
  const escapeHtml = (s) =>
    String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function buildChartSVG(metric, periods) {
    const w = 640, h = 130, padL = 40, padR = 16, padT = 16, padB = 22;
    const innerW = w - padL - padR, innerH = h - padT - padB;
    const pts = metric.values.map((v, i) => ({ i, v }));
    const known = pts.filter((p) => p.v !== null);
    if (known.length === 0) return "";

    let min = Math.min(...known.map((p) => p.v));
    let max = Math.max(...known.map((p) => p.v));
    if (metric.unit === "pp" || min <= 0) min = Math.min(min, 0);
    if (max <= 0) max = Math.max(max, 0);
    const pad = (max - min) * 0.15 || 1;
    min -= pad;
    max += pad;

    const x = (i) => padL + (innerW * i) / Math.max(periods.length - 1, 1);
    const y = (v) => padT + innerH - ((v - min) / (max - min)) * innerH;

    const segments = [];
    let current = [];
    pts.forEach((p) => {
      if (p.v === null) {
        if (current.length) segments.push(current);
        current = [];
      } else {
        current.push(p);
      }
    });
    if (current.length) segments.push(current);

    const toneClass = known[known.length - 1].v < 0 ? " neg-tone" : "";
    const pathFor = (seg) => "M " + seg.map((p) => `${x(p.i)},${y(p.v)}`).join(" L ");

    let svg = `<svg class="trend-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">`;
    if (min < 0 && max > 0) {
      svg += `<line class="trend-zero" x1="${padL}" x2="${w - padR}" y1="${y(0)}" y2="${y(0)}" />`;
    }
    [0.25, 0.5, 0.75].forEach((f) => {
      const gy = padT + innerH * f;
      svg += `<line class="trend-grid-line" x1="${padL}" x2="${w - padR}" y1="${gy}" y2="${gy}" />`;
    });
    segments.forEach((seg) => {
      svg += `<path class="trend-line${toneClass}" d="${pathFor(seg)}" />`;
    });
    pts.forEach((p) => {
      if (p.v === null) return;
      const isLast = p.i === pts.length - 1;
      svg += `<circle class="trend-dot${isLast ? " latest" : toneClass}" cx="${x(p.i)}" cy="${y(p.v)}" r="${isLast ? 4 : 2.5}" />`;
    });
    // Fixed "every 2nd point" broke down badly on a long "all" span (AAPL's 26+
    // quarters jammed a dozen overlapping labels into 640 units of width). A
    // modulo step has its own trap — forcing the very last point to always show
    // can land it right next to the last regular-step label (e.g. 26 points,
    // step 4: ...20, 24, then a forced 25 right beside it). Explicit evenly-spaced
    // indices avoid that: always exactly maxLabels labels, first and last anchored
    // at the ends, no two ever closer together than the rest.
    const maxLabels = 7;
    const n = periods.length;
    const labelIndices = n <= maxLabels
      ? periods.map((_, i) => i)
      : [...new Set(Array.from({ length: maxLabels }, (_, k) => Math.round((k * (n - 1)) / (maxLabels - 1))))];
    labelIndices.forEach((i) => {
      const isLast = i === n - 1;
      const anchor = isLast ? "end" : i === 0 ? "start" : "middle";
      svg += `<text x="${x(i)}" y="${h - 6}" text-anchor="${anchor}">${escapeHtml(periods[i])}</text>`;
    });
    svg += "</svg>";
    return svg;
  }

  // Raw values are full-precision floats straight from the DB (e.g.
  // 1503.3600000000006) — rounded for display the same way the table's own _num()
  // helper does, never shown with that kind of floating-point noise.
  function formatValue(v) {
    const rounded = Math.abs(v) >= 100 ? Math.round(v) : Math.round(v * 100) / 100;
    return rounded.toLocaleString("en-US", { maximumFractionDigits: 2 });
  }

  function buildTrendCard(metric, periods) {
    const svg = buildChartSVG(metric, periods);
    if (!svg) return "";
    const known = metric.values.filter((v) => v !== null);
    const latest = known[known.length - 1];
    const latestClass = latest < 0 ? "neg" : latest > 0 ? "pos" : "";
    // "+" only for percentage-point deltas (growth/margin vs. sector) — those are
    // genuinely comparisons. A plain magnitude like Revenue or Operating margin
    // isn't a delta, so a "+" in front of a positive number would be misleading.
    const sign = latest > 0 && metric.unit === "pp" ? "+" : "";
    return `<div class="trend-card">
      <div class="trend-card-head">
        <span class="trend-metric-name">${escapeHtml(metric.label)}</span>
        <span class="trend-latest ${latestClass}">${sign}${formatValue(latest)}${escapeHtml(metric.unit)}</span>
      </div>
      ${svg}
    </div>`;
  }

  function readJson(id) {
    const el = document.getElementById(id);
    if (!el) return null;
    try {
      return JSON.parse(el.textContent);
    } catch (e) {
      return null;
    }
  }

  // Quarterly Quality Matrix — one trend target per layer (l1/l2/l3), each inside
  // the matrix table as its own <tbody>.
  const matrixData = readJson("trend-data");
  if (matrixData) {
    Object.keys(matrixData.layers).forEach((layer) => {
      const el = document.getElementById("trend-" + layer);
      if (el) el.innerHTML = matrixData.layers[layer].map((m) => buildTrendCard(m, matrixData.periods)).join("");
    });
  }

  // Profit & Loss / Balance Sheet — same toggle pattern, but outside a table (plain
  // divs, each card with its own period list — Balance Sheet in particular only
  // lists periods that actually have a reported balance sheet, a different set
  // than the P&L's own quarters).
  ["pnl", "bs"].forEach((key) => {
    const data = readJson(key + "-trend-data");
    const el = document.getElementById("trend-" + key);
    if (data && el) el.innerHTML = data.metrics.map((m) => buildTrendCard(m, data.periods)).join("");
  });

  document.querySelectorAll(".view-toggle").forEach((toggle) => {
    toggle.addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      const layer = toggle.dataset.layer;
      const mode = btn.dataset.mode;
      toggle.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
      const body = document.querySelector(`.layer-body[data-layer="${layer}"]`);
      const panel = document.querySelector(`.trend-panel[data-layer="${layer}"], .trend-panel-block[data-layer="${layer}"]`);
      if (body) body.classList.toggle("hidden", mode === "trend");
      if (panel) panel.classList.toggle("open", mode === "trend");
    });
  });
})();
