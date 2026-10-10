// Chart explorer: any trend graph on a stock page opens full-size with labelled axes, units,
// a metric picker, period controls, summary stats, exact hover/keyboard values and the data table.
//
// Triggers: any element with data-explore="price" or data-explore="metric" (+ data-set, data-metric).
// Data: <script id="explorer-data"> (every row of every table, raw numbers) and <script id="price-data">.
// Everything is built with DOM APIs (textContent) — no HTML injection.
(function () {
  const dataEl = document.getElementById("explorer-data");
  const priceEl = document.getElementById("price-data");
  let E = null, P = null;
  try { if (dataEl) E = JSON.parse(dataEl.textContent); } catch (e) { E = null; }
  try { if (priceEl) P = JSON.parse(priceEl.textContent); } catch (e) { P = null; }
  if (P && !(P.c && P.c.length > 1)) P = null;
  if (!E && !P) return;

  const SVGNS = "http://www.w3.org/2000/svg";
  const CUR = (E && E.currency) || (E && E.price && E.price.currency) || "";
  const LOC = CUR === "₹" ? "en-IN" : "en-US";
  const PRICE_DAYS = { "1M": 31, "3M": 92, "6M": 183, "1Y": 366, "5Y": 1830, MAX: Infinity };
  const PRICE_LABEL = { "1M": "1 month", "3M": "3 months", "6M": "6 months", "1Y": "1 year", "5Y": "5 years", MAX: "full history" };

  // ---------- formatting ----------
  const grp = (v, dp) => Number(v).toLocaleString(LOC, { minimumFractionDigits: dp, maximumFractionDigits: dp });
  function fmt(v, unit, dpHint) {
    if (v == null || !isFinite(v)) return "—";
    if (unit === "%") return grp(v, dpHint != null ? dpHint : 1) + "%";
    if (unit === "x") return grp(v, 2) + "x";
    if (unit === "₹" || unit === "$") return unit + grp(v, 2);
    if (unit === "") return grp(v, 0);
    const a = Math.abs(v);
    return grp(v, a >= 1000 ? 0 : a >= 100 ? 1 : 2);
  }
  const signed = (s, v) => (v > 0 ? "+" : v < 0 ? "−" : "") + s.replace("-", "").replace("−", "");
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const fmtDate = (iso, long) => {
    const y = iso.slice(0, 4), m = MON[+iso.slice(5, 7) - 1], d = +iso.slice(8, 10);
    return long ? d + " " + m + " " + y : m + " " + y.slice(2);
  };
  const DAY = 86400000, tms = (iso) => Date.parse(iso + "T00:00:00Z");

  function niceScale(lo, hi, n) {
    if (!(hi > lo)) { const d = Math.abs(lo) * 0.05 || 1; lo -= d; hi += d; }
    const raw = (hi - lo) / n, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
    const step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
    const a = Math.floor(lo / step + 1e-9) * step, b = Math.ceil(hi / step - 1e-9) * step;
    const ticks = [];
    for (let v = a; v <= b + step / 2; v += step) ticks.push(Number(v.toPrecision(12)));
    return { lo: a, hi: b, step, ticks };
  }
  const tickFmt = (step) => (v) => grp(v, step >= 1 ? 0 : Math.min(4, Math.ceil(-Math.log10(step))));

  function mk(name, attrs, ns) {
    const n = ns ? document.createElementNS(SVGNS, name) : document.createElement(name);
    if (attrs) for (const k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  }
  const svgEl = (name, attrs) => mk(name, attrs, true);
  function icon(id, cls) {
    const sv = svgEl("svg", { class: cls || "icon" });
    sv.appendChild(svgEl("use", { href: "#" + id }));
    return sv;
  }
  function div(cls, text) { const d = mk("div"); if (cls) d.className = cls; if (text != null) d.textContent = text; return d; }
  function span(cls, text) { const d = mk("span"); if (cls) d.className = cls; if (text != null) d.textContent = text; return d; }

  // ---------- state ----------
  const S = { open: false, key: null, range: null, type: "auto", log: false, trigger: null, idx: null };
  let ui = null, spec = null;

  // Metric catalogue: [{key:"quarterly|Revenue", set, label, unit, ...}] grouped by set.
  function catalogue() {
    const out = [];
    if (P) out.push({ key: "price", group: "Market", label: "Share price", price: true });
    if (E) for (const sk in E.sets) {
      const set = E.sets[sk];
      set.rows.forEach((r) => out.push({ key: sk + "|" + r.label, group: set.title, set: sk, label: r.label, unit: r.unit, row: r }));
    }
    return out;
  }

  // ---------- build the dialog once ----------
  function build() {
    const scrim = div("cx-scrim");
    scrim.setAttribute("hidden", "");
    const dlg = mk("div", { class: "cx", role: "dialog", "aria-modal": "true", "aria-labelledby": "cx-title", tabindex: "-1" });

    const head = div("cx-head");
    const titles = div("cx-titles");
    const h = mk("h2", { id: "cx-title", class: "cx-title" });
    const sub = div("cx-sub");
    titles.append(h, sub);
    const close = mk("button", { type: "button", class: "icon-btn cx-close", "aria-label": "Close chart" });
    close.appendChild(icon("i-x"));
    head.append(titles, close);

    const controls = div("cx-controls");
    const metricWrap = mk("label", { class: "cx-field" });
    const metricLab = span("stat-label", "Metric");
    const select = mk("select", { class: "form-input cx-select", "aria-label": "Metric" });
    metricWrap.append(metricLab, select);
    const rangeWrap = div("cx-field");
    const rangeLab = span("stat-label", "Period");
    const range = mk("div", { class: "seg", role: "group", "aria-label": "Period shown" });
    rangeWrap.append(rangeLab, range);
    const typeWrap = div("cx-field");
    const typeLab = span("stat-label", "Display");
    const typeSeg = mk("div", { class: "seg", role: "group", "aria-label": "Chart style" });
    typeWrap.append(typeLab, typeSeg);
    const csv = mk("button", { type: "button", class: "btn btn-sm cx-csv" });
    csv.append(icon("i-arrow-down", "icon icon-sm"), " CSV");
    controls.append(metricWrap, rangeWrap, typeWrap, csv);

    const stats = div("cx-stats");
    const plot = div("price-chart cx-plot");
    const tip = div("price-tip");
    plot.append(tip);
    const legend = div("cx-legend");
    const foot = div("cx-foot");

    const details = mk("details", { class: "cx-details" });
    const sum = mk("summary");
    sum.textContent = "Data table";
    const tableWrap = div("table-wrap cx-table");
    details.append(sum, tableWrap);

    dlg.append(head, controls, stats, plot, legend, foot, details);
    scrim.appendChild(dlg);
    document.body.appendChild(scrim);

    // events
    close.addEventListener("click", closeDlg);
    scrim.addEventListener("mousedown", (e) => { if (e.target === scrim) closeDlg(); });
    select.addEventListener("change", () => { S.key = select.value; S.range = null; S.type = "auto"; S.log = false; render(true); draw(); });
    csv.addEventListener("click", downloadCsv);
    dlg.addEventListener("keydown", trapKeys);
    return { scrim, dlg, h, sub, select, range, rangeWrap, typeSeg, typeWrap, stats, plot, tip, legend, foot, details, tableWrap, csv };
  }

  function trapKeys(e) {
    if (e.key === "Escape") { e.stopPropagation(); closeDlg(); return; }
    if (e.key !== "Tab") return;
    const f = Array.from(ui.dlg.querySelectorAll("button:not([disabled]), select, summary, [tabindex='0']")).filter((x) => x.offsetParent !== null);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  function openDlg(key, trigger) {
    if (!ui) ui = build();
    S.key = key; S.trigger = trigger || null; S.range = null; S.type = "auto"; S.log = false;
    if (key === "price") {
      const a = document.querySelector("#price-range button.active");
      if (a) S.range = a.dataset.range;
    }
    S.open = true;
    ui.scrim.removeAttribute("hidden");
    document.documentElement.classList.add("cx-open");
    render(true);
    void ui.scrim.offsetWidth; // flush layout so the fade-in runs and the plot has a real width
    ui.scrim.classList.add("on");
    draw();
    ui.dlg.focus();
  }

  function closeDlg() {
    if (!S.open) return;
    S.open = false;
    ui.scrim.classList.remove("on");
    ui.scrim.setAttribute("hidden", "");
    document.documentElement.classList.remove("cx-open");
    if (S.trigger && document.contains(S.trigger)) S.trigger.focus();
  }

  // ---------- series ----------
  function priceSeries() {
    const r = S.range || "1Y";
    const days = PRICE_DAYS[r];
    let start = 0;
    if (isFinite(days)) {
      const last = new Date(P.d[P.d.length - 1] + "T00:00:00");
      const cutoff = new Date(last.getTime() - days * 86400000).toISOString().slice(0, 10);
      start = P.d.findIndex((x) => x >= cutoff);
      if (start < 0) start = 0;
      if (P.d.length - start < 2) start = Math.max(0, P.d.length - 2);
    }
    return { d: P.d.slice(start), c: P.c.slice(start), r };
  }

  function metricSeries(item) {
    const set = E.sets[item.set];
    let labels = set.columns.slice(), fulls = set.columns.map((c, i) => c + (set.ends && set.ends[i] ? " · period ending " + fmtDate(set.ends[i], true) : "")), vals = item.row.values.slice();
    // trim empty periods at both ends — the chart should start and end on a real value
    let a = 0, b = vals.length;
    while (a < b && vals[a] == null) a++;
    while (b > a && vals[b - 1] == null) b--;
    labels = labels.slice(a, b); fulls = fulls.slice(a, b); vals = vals.slice(a, b);
    const total = vals.length;
    let rng = S.range;
    if (rng == null) rng = total > 12 ? "12" : "all";
    const n = rng === "all" ? total : Math.min(total, parseInt(rng, 10));
    const from = total - n;
    return { labels: labels.slice(from), fulls: fulls.slice(from), vals: vals.slice(from), total, rng, set };
  }

  // ---------- render controls + stats (then chart) ----------
  function render(rebuildSelect) {
    const cat = catalogue();
    const item = cat.find((c) => c.key === S.key) || cat[0];
    if (!item) return closeDlg();
    S.key = item.key;

    if (rebuildSelect) {
      ui.select.textContent = "";
      const groups = {};
      cat.forEach((c) => {
        if (!groups[c.group]) { groups[c.group] = mk("optgroup", { label: c.group }); ui.select.appendChild(groups[c.group]); }
        const o = mk("option", { value: c.key });
        o.textContent = c.price ? "Share price" : c.label + (c.unit ? " (" + c.unit + ")" : "");
        groups[c.group].appendChild(o);
      });
    }
    ui.select.value = S.key;

    // range + display controls
    ui.range.textContent = ""; ui.typeSeg.textContent = "";
    const seg = (host, opts, current, on) => opts.forEach(([val, text]) => {
      const b = mk("button", { type: "button", class: val === current ? "active" : "", "aria-pressed": val === current ? "true" : "false" });
      b.textContent = text;
      b.addEventListener("click", () => on(val));
      host.appendChild(b);
    });

    if (item.price) {
      const cur = S.range || "1Y";
      seg(ui.range, Object.keys(PRICE_DAYS).map((k) => [k, k === "MAX" ? "All" : k]), cur, (v) => { S.range = v; render(false); draw(); });
      seg(ui.typeSeg, [["lin", "Linear"], ["log", "Log"]], S.log ? "log" : "lin", (v) => { S.log = v === "log"; render(false); draw(); });
    } else {
      const ms = metricSeries(item);
      const opts = [4, 8, 12, 20].filter((k) => k < ms.total).map((k) => [String(k), "Last " + k]);
      opts.push(["all", ms.total > 1 ? "All " + ms.total : "All"]);
      seg(ui.range, opts, ms.rng, (v) => { S.range = v; render(false); draw(); });
      const auto = item.unit === "%" || item.unit === "x" ? "line" : "bars";
      const type = S.type === "auto" ? auto : S.type;
      seg(ui.typeSeg, [["bars", "Bars"], ["line", "Line"]], type, (v) => { S.type = v; render(false); draw(); });
    }
    ui.rangeWrap.hidden = false;
    ui.typeWrap.hidden = false;

    buildSpec(item);
    renderHead(item);
    renderStats();
    renderFoot(item);
    renderTable(item);
  }

  // ---------- spec: everything the chart + stats need, independent of drawing ----------
  function buildSpec(item) {
    if (item.price) {
      const s = priceSeries();
      const ret = (s.c[s.c.length - 1] / s.c[0] - 1) * 100;
      spec = {
        kind: "line", price: true, n: s.c.length, vals: s.c, unit: CUR, ytitle: "Close price (" + CUR + ")", xtitle: "Date",
        xlabel: (i) => fmtDate(s.d[i], false), full: (i) => fmtDate(s.d[i], true), tone: ret >= 0 ? "pos" : "neg",
        dates: s.d, rangeKey: s.r, log: S.log, item,
      };
    } else {
      const ms = metricSeries(item);
      const auto = item.unit === "%" || item.unit === "x" ? "line" : "bars";
      spec = {
        kind: S.type === "auto" ? auto : S.type, n: ms.vals.length, vals: ms.vals, unit: item.unit, labels: ms.labels, fulls: ms.fulls,
        ytitle: item.label + (item.unit ? " (" + item.unit + ")" : ""), xtitle: ms.set.xtitle || "Period",
        xlabel: (i) => ms.labels[i], full: (i) => ms.fulls[i] || ms.labels[i], tone: "neutral", item, set: ms.set,
        dp: E.sets[item.set] && item.set === "ownership" ? 2 : null,
      };
    }
  }

  function renderHead(item) {
    ui.h.textContent = item.price ? "Share price" : item.label;
    ui.sub.textContent = "";
    const bits = [];
    const nm = E ? E.symbol + " · " + E.name : "";
    if (nm) bits.push(nm);
    if (item.price) {
      bits.push("Daily close, " + CUR);
      bits.push(PRICE_LABEL[spec.rangeKey] + ": " + fmtDate(spec.dates[0], true) + " – " + fmtDate(spec.dates[spec.n - 1], true));
    } else {
      bits.push(spec.set.title);
      if (item.unit) bits.push("Unit: " + item.unit);
      if (spec.n) bits.push(spec.xlabel(0) + " – " + spec.xlabel(spec.n - 1) + " · " + spec.vals.filter((v) => v != null).length + " periods");
    }
    bits.forEach((t, i) => { if (i) ui.sub.append(span("cx-dot", "·")); ui.sub.append(span("", t)); });
  }

  const lbl = (i) => (spec.price ? spec.full(i) : spec.xlabel(i));
  function statCell(label, value, sub, tone) {
    const c = div("cx-stat");
    c.append(span("stat-label", label));
    const v = mk("b"); v.textContent = value; if (tone) v.className = tone === "pos" ? "val-pos" : tone === "neg" ? "val-neg" : "";
    c.append(v);
    if (sub) c.append(span("cx-stat-sub", sub));
    return c;
  }
  function pctChange(a, b, unit) {
    // change from a to b: percent for money-like values, percentage points for ratios
    if (a == null || b == null) return null;
    if (unit === "%" || unit === "x") return { text: signed(grp(b - a, unit === "%" ? 1 : 2), b - a) + (unit === "%" ? " pp" : "x"), v: b - a };
    if (a === 0) return null;
    const p = ((b - a) / Math.abs(a)) * 100;
    return { text: signed(grp(p, 1), p) + "%", v: p };
  }

  function renderStats() {
    ui.stats.textContent = "";
    const vals = spec.vals;
    const idx = vals.map((v, i) => i).filter((i) => vals[i] != null);
    if (!idx.length) return;
    const fi = idx[0], li = idx[idx.length - 1];
    let hi = fi, lo = fi;
    idx.forEach((i) => { if (vals[i] > vals[hi]) hi = i; if (vals[i] < vals[lo]) lo = i; });
    const F = (v) => fmt(v, spec.unit, spec.dp);
    if (spec.price) {
      const ret = pctChange(vals[fi], vals[li], "");
      let peak = vals[0], dd = 0;
      vals.forEach((v) => { if (v > peak) peak = v; dd = Math.min(dd, (v / peak - 1) * 100); });
      ui.stats.append(
        statCell("Last close", F(vals[li]), lbl(li)),
        statCell("Start of period", F(vals[fi]), lbl(fi)),
        statCell("Change", ret.text, "over " + PRICE_LABEL[spec.rangeKey], ret.v >= 0 ? "pos" : "neg"),
        statCell("High", F(vals[hi]), lbl(hi)),
        statCell("Low", F(vals[lo]), lbl(lo)),
        statCell("Max drawdown", dd.toFixed(1).replace("-", "−") + "%", "peak to trough in view", dd < -0.05 ? "neg" : ""),
      );
      return;
    }
    const prev = idx.length > 1 ? idx[idx.length - 2] : null;
    const chPrev = prev != null ? pctChange(vals[prev], vals[li], spec.unit) : null;
    const chAll = idx.length > 1 ? pctChange(vals[fi], vals[li], spec.unit) : null;
    const avg = idx.reduce((s, i) => s + vals[i], 0) / idx.length;
    ui.stats.append(statCell("Latest", F(vals[li]), lbl(li)));
    if (chPrev) ui.stats.append(statCell("vs previous period", chPrev.text, lbl(prev) + " → " + lbl(li), chPrev.v > 0 ? "pos" : chPrev.v < 0 ? "neg" : ""));
    if (chAll) ui.stats.append(statCell("Over this view", chAll.text, lbl(fi) + " → " + lbl(li), chAll.v > 0 ? "pos" : chAll.v < 0 ? "neg" : ""));
    ui.stats.append(statCell("High", F(vals[hi]), lbl(hi)), statCell("Low", F(vals[lo]), lbl(lo)), statCell("Average", F(avg), idx.length + " periods"));
  }

  function renderFoot(item) {
    ui.legend.textContent = "";
    ui.foot.textContent = "";
    const key = (cls, text) => { const s = span("cx-key"); s.append(span("cx-swatch " + cls), document.createTextNode(text)); return s; };
    if (item.price) {
      ui.legend.append(key(spec.tone === "pos" ? "pos" : "neg", "Daily close" + (spec.log ? " (log scale)" : "")));
    } else if (spec.kind === "bars") {
      ui.legend.append(key("s", "Reported value"), key("acc", "Latest period"));
      if (spec.vals.some((v) => v != null && v < 0)) ui.legend.append(key("neg", "Negative"));
    } else {
      ui.legend.append(key("s", "Reported value"), key("acc", "Latest period"));
    }
    ui.legend.append(span("cx-hint", "Hover, tap or use ← → to read exact values"));

    const notes = [];
    if (item.price) {
      notes.push("Closing prices in " + CUR + (E && E.price ? ", last updated " + E.price.as_of : "") + ". Older history is thinned to weekly points; the last ~2 years are daily.");
    } else {
      if (item.row.note) notes.push(item.row.note);
      notes.push("Figures are as reported in company filings. A gap means the period had no reported value.");
      const missing = spec.vals.filter((v) => v == null).length;
      if (missing) notes.push(missing + " period" + (missing > 1 ? "s" : "") + " in this view had no value.");
    }
    notes.forEach((t) => ui.foot.append(div("cx-note", t)));
  }

  function renderTable(item) {
    ui.tableWrap.textContent = "";
    if (item.price) { ui.details.hidden = true; return; }
    ui.details.hidden = false;
    const t = mk("table", { class: "fin" });
    const thead = mk("thead"), hr = mk("tr");
    ["Period", "Value" + (item.unit ? " (" + item.unit + ")" : ""), "Change vs previous"].forEach((x, i) => {
      const th = mk("th"); th.textContent = x; if (i) th.style.textAlign = "right"; hr.appendChild(th);
    });
    thead.appendChild(hr); t.appendChild(thead);
    const tb = mk("tbody");
    let prevV = null;
    for (let i = spec.n - 1; i >= 0; i--) {
      const v = spec.vals[i];
      // previous *reported* value to the left of i
      let pv = null;
      for (let j = i - 1; j >= 0; j--) if (spec.vals[j] != null) { pv = spec.vals[j]; break; }
      const ch = v != null ? pctChange(pv, v, item.unit) : null;
      const tr = mk("tr");
      const a = mk("td"), b = mk("td"), c = mk("td");
      a.textContent = spec.full(i);
      b.textContent = fmt(v, spec.unit, spec.dp); b.style.textAlign = "right";
      c.textContent = ch ? ch.text : "—"; c.style.textAlign = "right";
      if (ch) c.className = ch.v > 0 ? "val-pos" : ch.v < 0 ? "val-neg" : "";
      tr.append(a, b, c); tb.appendChild(tr);
    }
    t.appendChild(tb); ui.tableWrap.appendChild(t);
  }

  // ---------- chart ----------
  function draw() {
    if (!S.open || !spec) return;
    const host = ui.plot;
    const W = Math.max(host.clientWidth, 280);
    const H = W < 520 ? 300 : Math.max(300, Math.min(470, Math.round(window.innerHeight * 0.42)));
    const n = spec.n, vals = spec.vals, bars = spec.kind === "bars";
    const real = vals.filter((v) => v != null);
    if (!real.length) return;
    let dmin = Math.min(...real), dmax = Math.max(...real);
    if (bars) { dmin = Math.min(0, dmin); dmax = Math.max(0, dmax); }
    const logOn = spec.price && spec.log && dmin > 0;
    const sc = niceScale(logOn ? dmin * 0.95 : dmin, logOn ? dmax * 1.05 : dmax, W < 520 ? 4 : 5);
    if (!bars && !logOn && !spec.price && dmin >= 0 && sc.lo < 0) { sc.ticks = sc.ticks.filter((t) => t >= 0); sc.lo = 0; }
    const tf = tickFmt(sc.step);
    const tickStrs = sc.ticks.map(tf);
    const padL = Math.min(96, Math.max(...tickStrs.map((s) => s.length)) * 6.6 + 40);
    const padR = 14, padT = 18, padB = 46;
    const iw = W - padL - padR, ih = H - padT - padB;
    const lg = (v) => Math.log(v);
    const Y = (v) => padT + ih - (logOn ? (lg(v) - lg(sc.lo)) / (lg(sc.hi) - lg(sc.lo)) : (v - sc.lo) / (sc.hi - sc.lo)) * ih;
    const band = iw / n;
    // Price points are daily recently and weekly further back, so the x position follows the calendar.
    const T = spec.price ? spec.dates.map(tms) : null;
    const t0 = T ? T[0] : 0, tSpan = T ? (T[n - 1] - T[0]) || 1 : 1;
    const X = (i) => (bars ? padL + band * (i + 0.5) : T ? padL + (iw * (T[i] - t0)) / tSpan : padL + (n === 1 ? iw / 2 : (iw * i) / (n - 1)));

    const old = ui.plot.querySelector("svg");
    if (old) old.remove();
    const svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img", tabindex: "0",
      "aria-label": spec.ytitle + " by " + spec.xtitle.toLowerCase() + ", " + n + " points. Use left and right arrow keys to read values." });
    const tone = spec.tone === "pos" ? "var(--pos)" : spec.tone === "neg" ? "var(--neg)" : "var(--structure)";

    // y grid + labels
    sc.ticks.forEach((t, k) => {
      const y = Y(t);
      svg.appendChild(svgEl("line", { x1: padL, x2: W - padR, y1: y, y2: y, class: "pc-grid" }));
      const tx = svgEl("text", { x: padL - 8, y: y + 3.5, "text-anchor": "end", class: "pc-axis" });
      tx.textContent = tickStrs[k]; svg.appendChild(tx);
    });
    // axis titles
    const yt = svgEl("text", { transform: "translate(13 " + (padT + ih / 2) + ") rotate(-90)", "text-anchor": "middle", class: "pc-title" });
    yt.textContent = spec.ytitle; svg.appendChild(yt);
    const xt = svgEl("text", { x: padL + iw / 2, y: H - 6, "text-anchor": "middle", class: "pc-title" });
    xt.textContent = spec.xtitle; svg.appendChild(xt);
    // zero line for bars / signed data
    if (sc.lo < 0 && sc.hi > 0) svg.appendChild(svgEl("line", { x1: padL, x2: W - padR, y1: Y(0), y2: Y(0), class: "pc-zero" }));

    // x labels: calendar-aware for price, thinned-to-fit for periods
    const labelIdx = [], labelText = {};
    if (spec.price) {
      const spanDays = tSpan / DAY;
      const monthly = spanDays > 95;
      const first = {};
      const cand = [];
      if (spanDays > 95) {
        const yearly = spanDays > 900;
        for (let i = 0; i < n; i++) {
          const key = yearly ? spec.dates[i].slice(0, 4) : spec.dates[i].slice(0, 7);
          if (!first[key]) { first[key] = 1; cand.push(i); }
        }
        const fmtT = (i) => (yearly ? spec.dates[i].slice(0, 4) : fmtDate(spec.dates[i], false));
        const need = (yearly ? 4 : 6) * 6.6 + 22;
        const step = Math.max(1, Math.ceil((need * cand.length) / iw));
        // keep the label away from the left edge so it never collides with the y-axis labels
        cand.forEach((i, k) => { if (k % step === 0 && X(i) - padL >= need / 2 - 6) { labelIdx.push(i); labelText[i] = fmtT(i); } });
      } else {
        const cnt = Math.max(2, Math.min(7, Math.floor(iw / 70)));
        let last = "";
        for (let k = 0; k < cnt; k++) {
          const target = T[0] + (tSpan * k) / (cnt - 1);
          let best = 0; for (let i = 0; i < n; i++) if (Math.abs(T[i] - target) < Math.abs(T[best] - target)) best = i;
          const t = +spec.dates[best].slice(8, 10) + " " + MON[+spec.dates[best].slice(5, 7) - 1];
          if (t !== last) { labelIdx.push(best); labelText[best] = t; last = t; }
        }
      }
    } else {
      const per = bars ? band : iw / Math.max(1, n - 1);
      const maxLab = Math.max(...Array.from({ length: n }, (_, i) => spec.xlabel(i).length));
      const need = maxLab * 6.3 + 10;
      const every = Math.max(1, Math.ceil(need / per));
      if (bars) { for (let i = n - 1; i >= 0; i -= every) labelIdx.push(i); }
      else {
        const cnt = Math.max(2, Math.min(n, Math.floor(iw / need)));
        for (let k = 0; k < cnt; k++) labelIdx.push(Math.round((k * (n - 1)) / Math.max(1, cnt - 1)));
      }
      labelIdx.forEach((i) => { labelText[i] = spec.xlabel(i); });
    }
    new Set(labelIdx).forEach((i) => {
      const anchor = !bars && !spec.price && i === 0 ? "start" : !bars && !spec.price && i === n - 1 ? "end" : "middle";
      const tx = svgEl("text", { x: X(i), y: padT + ih + 16, "text-anchor": anchor, class: "pc-axis" });
      tx.textContent = labelText[i];
      svg.appendChild(tx);
      svg.appendChild(svgEl("line", { x1: X(i), x2: X(i), y1: padT + ih, y2: padT + ih + 4, class: "pc-tick" }));
    });

    const hl = svgEl("rect", { class: "pc-hl", y: padT, height: ih, width: bars ? Math.max(band, 4) : 0, visibility: "hidden" });
    svg.appendChild(hl);

    if (bars) {
      const bw = Math.min(64, Math.max(4, band * 0.62));
      const y0 = Y(Math.max(sc.lo, Math.min(0, sc.hi)));
      for (let i = 0; i < n; i++) {
        const v = vals[i]; if (v == null) continue;
        const y = Y(v), top = Math.min(y, y0), h = Math.max(1.5, Math.abs(y - y0));
        const cls = "cx-bar" + (v < 0 ? " neg" : "") + (i === n - 1 ? " last" : "");
        svg.appendChild(svgEl("rect", { x: X(i) - bw / 2, y: top, width: bw, height: h, rx: 2.5, class: cls }));
      }
      // value labels when there is room
      if (band >= 46 || n <= 8) {
        for (let i = 0; i < n; i++) {
          const v = vals[i]; if (v == null) continue;
          const y = Y(v), up = v >= 0;
          const tx = svgEl("text", { x: X(i), y: up ? y - 6 : y + 13, "text-anchor": "middle", class: "cx-vlabel" + (v < 0 ? " neg" : "") });
          tx.textContent = fmt(v, spec.unit, spec.dp);
          svg.appendChild(tx);
        }
      }
    } else {
      // line (with a gap at missing values) + soft area for price
      let d = "", pen = false;
      for (let i = 0; i < n; i++) {
        if (vals[i] == null) { pen = false; continue; }
        d += (pen ? "L" : "M") + X(i).toFixed(1) + " " + Y(vals[i]).toFixed(1) + " "; pen = true;
      }
      if (spec.price) {
        const gid = "cxg";
        const defs = svgEl("defs"), g = svgEl("linearGradient", { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 });
        g.append(svgEl("stop", { offset: "0%", "stop-color": tone, "stop-opacity": 0.26 }), svgEl("stop", { offset: "100%", "stop-color": tone, "stop-opacity": 0 }));
        defs.appendChild(g); svg.appendChild(defs);
        svg.appendChild(svgEl("path", { d: d + "L" + X(n - 1).toFixed(1) + " " + (padT + ih) + " L" + X(0).toFixed(1) + " " + (padT + ih) + " Z", fill: "url(#" + gid + ")" }));
      }
      svg.appendChild(svgEl("path", { d: d, fill: "none", stroke: tone, "stroke-width": spec.price ? 2 : 2.2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
      if (!spec.price || n <= 40) for (let i = 0; i < n; i++) {
        if (vals[i] == null) continue;
        svg.appendChild(svgEl("circle", { cx: X(i), cy: Y(vals[i]), r: i === n - 1 ? 4.5 : 3, fill: i === n - 1 ? "var(--accent)" : tone, stroke: "var(--card)", "stroke-width": 1.5 }));
      }
      if (spec.price) svg.appendChild(svgEl("circle", { cx: X(n - 1), cy: Y(vals[n - 1]), r: 4.5, fill: tone, stroke: "var(--card)", "stroke-width": 2 }));
      // end-value tag for the metric line when no labels on points
      if (!spec.price && n > 14 && vals[n - 1] != null) {
        const tx = svgEl("text", { x: X(n - 1), y: Y(vals[n - 1]) - 9, "text-anchor": "end", class: "cx-vlabel" });
        tx.textContent = fmt(vals[n - 1], spec.unit, spec.dp); svg.appendChild(tx);
      } else if (!spec.price && n <= 14) {
        for (let i = 0; i < n; i++) if (vals[i] != null) {
          const tx = svgEl("text", { x: X(i), y: Y(vals[i]) - 9, "text-anchor": "middle", class: "cx-vlabel" });
          tx.textContent = fmt(vals[i], spec.unit, spec.dp); svg.appendChild(tx);
        }
      }
    }

    const cross = svgEl("line", { y1: padT, y2: padT + ih, class: "pc-cross", visibility: "hidden" });
    const dot = svgEl("circle", { r: 5, fill: tone, stroke: "var(--card)", "stroke-width": 2, visibility: "hidden" });
    svg.append(cross, dot);
    const hit = svgEl("rect", { x: padL, y: padT, width: iw, height: ih, fill: "transparent" });
    svg.appendChild(hit);

    function show(i) {
      i = Math.max(0, Math.min(n - 1, i));
      S.idx = i;
      const x = X(i), v = vals[i];
      if (bars) { hl.setAttribute("x", x - band / 2); hl.setAttribute("width", Math.max(band, 4)); hl.setAttribute("visibility", "visible"); }
      else { cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible"); }
      if (!bars && v != null) { dot.setAttribute("cx", x); dot.setAttribute("cy", Y(v)); dot.setAttribute("visibility", "visible"); } else dot.setAttribute("visibility", "hidden");
      const tip = ui.tip; tip.textContent = "";
      tip.append(div("pt-date", spec.full(i)), div("pt-price", v == null ? "No reported value" : fmt(v, spec.unit, spec.dp) + (spec.price || !spec.item.unit || ["%", "x", "₹", "$"].includes(spec.item.unit) ? "" : " " + spec.item.unit)));
      if (v != null) {
        if (spec.price) {
          const c = (v / vals[0] - 1) * 100;
          tip.append(div("pt-chg " + (c >= 0 ? "val-pos" : "val-neg"), (c >= 0 ? "+" : "−") + Math.abs(c).toFixed(1) + "% from start of view"));
        } else {
          let pv = null; for (let j = i - 1; j >= 0; j--) if (vals[j] != null) { pv = vals[j]; break; }
          const ch = pctChange(pv, v, spec.item.unit);
          if (ch) tip.append(div("pt-chg " + (ch.v >= 0 ? "val-pos" : "val-neg"), ch.text + " vs previous period"));
        }
      }
      tip.style.opacity = 1;
      const tw = tip.offsetWidth, th = tip.offsetHeight;
      tip.style.left = Math.max(4, Math.min(W - tw - 4, x - tw / 2)) + "px";
      const yy = v == null ? padT + ih / 2 : Y(v);
      tip.style.top = Math.max(0, yy - th - (bars ? 30 : 14)) + "px";
    }
    function hide() { S.idx = null; cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); hl.setAttribute("visibility", "hidden"); ui.tip.style.opacity = 0; }
    function at(ev) {
      const box = svg.getBoundingClientRect();
      const px = ((ev.clientX - box.left) / box.width) * W;
      if (bars) return Math.floor((px - padL) / band);
      if (!T) return Math.round(((px - padL) / iw) * (n - 1));
      const tt = t0 + ((px - padL) / iw) * tSpan;
      let lo2 = 0, hi2 = n - 1;
      while (hi2 - lo2 > 1) { const m = (lo2 + hi2) >> 1; if (T[m] < tt) lo2 = m; else hi2 = m; }
      return Math.abs(T[lo2] - tt) <= Math.abs(T[hi2] - tt) ? lo2 : hi2;
    }
    hit.addEventListener("pointermove", (ev) => show(at(ev)));
    hit.addEventListener("pointerdown", (ev) => show(at(ev)));
    hit.addEventListener("pointerleave", hide);
    svg.addEventListener("keydown", (e) => {
      const k = e.key;
      if (k !== "ArrowLeft" && k !== "ArrowRight" && k !== "Home" && k !== "End") return;
      e.preventDefault();
      let i = S.idx == null ? (k === "ArrowLeft" ? n : -1) : S.idx;
      i = k === "Home" ? 0 : k === "End" ? n - 1 : i + (k === "ArrowRight" ? 1 : -1);
      show(i);
    });
    svg.addEventListener("blur", hide);
    ui.plot.insertBefore(svg, ui.tip);
    ui.plot.style.minHeight = H + "px";
  }

  // ---------- CSV of exactly what is on screen ----------
  function downloadCsv() {
    if (!spec) return;
    const q = (s) => '"' + String(s).replace(/"/g, '""') + '"';
    const lines = [spec.price ? "date,close" : "period,value,unit"];
    for (let i = 0; i < spec.n; i++) {
      if (spec.price) lines.push(spec.dates[i] + "," + spec.vals[i]);
      else lines.push(q(spec.full(i)) + "," + (spec.vals[i] == null ? "" : spec.vals[i]) + "," + q(spec.item.unit || ""));
    }
    const blob = new Blob([lines.join("\n") + "\n"], { type: "text/csv" });
    const a = mk("a", { href: URL.createObjectURL(blob), download: ((E && E.symbol) || "chart").replace(/[^\w.-]+/g, "_") + "-" + (spec.price ? "price" : spec.item.label.replace(/\W+/g, "_").toLowerCase()) + ".csv" });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }

  // ---------- triggers ----------
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-explore]");
    if (!t) return;
    const kind = t.dataset.explore;
    if (kind === "price") { if (P) { e.preventDefault(); openDlg("price", t); } return; }
    const key = t.dataset.set + "|" + t.dataset.metric;
    if (E && E.sets[t.dataset.set] && E.sets[t.dataset.set].rows.some((r) => r.label === t.dataset.metric)) { e.preventDefault(); openDlg(key, t); }
  });
  document.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches && e.target.matches("[data-explore][tabindex]")) { e.preventDefault(); e.target.click(); }
  });
  if ("ResizeObserver" in window) {
    let t = null;
    window.addEventListener("resize", () => { clearTimeout(t); t = setTimeout(draw, 80); });
  }
  window.TenetfolioChartExplorer = { open: openDlg, close: closeDlg };
})();
