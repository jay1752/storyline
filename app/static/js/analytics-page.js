// Analytics stock page: sticky section nav (scroll-spy) and the interactive price chart.
// The chart reads the downsampled series the route embeds as JSON — no extra request.
(function () {
  // ---------- scroll-spy for the sticky sub-nav ----------
  const links = Array.from(document.querySelectorAll("#subnav a"));
  const targets = links.map((a) => document.querySelector(a.getAttribute("href"))).filter(Boolean);
  if (links.length && "IntersectionObserver" in window) {
    const visible = new Map();
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => visible.set(e.target.id, e.isIntersecting ? e.boundingClientRect.top : null));
      const current = targets.find((t) => visible.get(t.id) !== null && visible.get(t.id) !== undefined);
      if (current) links.forEach((a) => a.classList.toggle("active", a.getAttribute("href") === "#" + current.id));
    }, { rootMargin: "-20% 0px -65% 0px" });
    targets.forEach((t) => io.observe(t));
  }

  // ---------- price chart ----------
  const dataEl = document.getElementById("price-data");
  const host = document.getElementById("price-chart");
  if (!dataEl || !host) return;
  let data;
  try { data = JSON.parse(dataEl.textContent); } catch (e) { return; }
  if (!data || !data.c || data.c.length < 2) return;

  const cur = host.dataset.currency || "";
  const locale = cur === "₹" ? "en-IN" : "en-US";
  const DAYS = { "1M": 31, "6M": 183, "1Y": 366, "5Y": 1830, MAX: Infinity };
  const ns = "http://www.w3.org/2000/svg";
  const NUM = (v) => cur + Number(v).toLocaleString(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const fmtDate = (iso, long) => {
    const d = new Date(iso + "T00:00:00");
    return d.toLocaleDateString("en-GB", long ? { day: "numeric", month: "short", year: "numeric" } : { month: "short", year: "2-digit" });
  };

  let range = "1Y";
  const tip = document.createElement("div");
  tip.className = "price-tip";
  host.appendChild(tip);

  function sliceFor(r) {
    const days = DAYS[r];
    if (!isFinite(days)) return { d: data.d, c: data.c };
    const last = new Date(data.d[data.d.length - 1] + "T00:00:00");
    const cutoff = new Date(last.getTime() - days * 86400000).toISOString().slice(0, 10);
    let start = data.d.findIndex((x) => x >= cutoff);
    if (start < 0) start = 0;
    if (data.d.length - start < 2) start = Math.max(0, data.d.length - 2);
    return { d: data.d.slice(start), c: data.c.slice(start) };
  }

  function el(name, attrs) {
    const n = document.createElementNS(ns, name);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  }

  function draw() {
    const s = sliceFor(range);
    const W = Math.max(host.clientWidth, 280);
    const H = W < 520 ? 220 : 290;
    const padL = 58, padR = 10, padT = 12, padB = 26;
    const iw = W - padL - padR, ih = H - padT - padB;
    const n = s.c.length;
    let lo = Math.min(...s.c), hi = Math.max(...s.c);
    const span = hi - lo || hi * 0.02 || 1;
    lo -= span * 0.08; hi += span * 0.08;
    // Older history is weekly and recent history daily, so x follows the calendar, not the point index.
    const T = s.d.map((x) => Date.parse(x + "T00:00:00Z")), t0 = T[0], tSpan = (T[n - 1] - T[0]) || 1;
    const X = (i) => padL + (iw * (T[i] - t0)) / tSpan;
    const Y = (v) => padT + ih - ((v - lo) / (hi - lo)) * ih;
    const ret = (s.c[n - 1] / s.c[0] - 1) * 100;
    const tone = ret >= 0 ? "var(--pos)" : "var(--neg)";

    const period = document.getElementById("price-period");
    if (period) {
      period.textContent = "";
      const b = document.createElement("b");
      b.className = ret >= 0 ? "val-pos" : "val-neg";
      b.textContent = (ret >= 0 ? "+" : "") + ret.toFixed(1) + "%";
      period.append(b, " over " + (range === "MAX" ? "full history" : range));
    }

    const old = host.querySelector("svg");
    if (old) old.remove();
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": "Price history" });
    const defs = el("defs", {});
    const grad = el("linearGradient", { id: "pg", x1: 0, y1: 0, x2: 0, y2: 1 });
    grad.appendChild(el("stop", { offset: "0%", "stop-color": tone, "stop-opacity": 0.28 }));
    grad.appendChild(el("stop", { offset: "100%", "stop-color": tone, "stop-opacity": 0 }));
    defs.appendChild(grad);
    svg.appendChild(defs);

    for (let k = 0; k <= 4; k++) {
      const v = lo + ((hi - lo) * k) / 4;
      const y = Y(v);
      svg.appendChild(el("line", { x1: padL, x2: W - padR, y1: y, y2: y, class: "pc-grid" }));
      const t = el("text", { x: padL - 8, y: y + 3.5, "text-anchor": "end", class: "pc-axis" });
      t.textContent = Number(v).toLocaleString(locale, { maximumFractionDigits: v >= 100 ? 0 : 1 });
      svg.appendChild(t);
    }
    const long = range === "1M" || range === "6M";
    const ticks = Math.min(5, n);
    for (let k = 0; k < ticks; k++) {
      const target = t0 + (tSpan * k) / (ticks - 1);
      let i = 0;
      for (let j = 0; j < n; j++) if (Math.abs(T[j] - target) < Math.abs(T[i] - target)) i = j;
      const t = el("text", { x: X(i), y: H - 7, "text-anchor": k === 0 ? "start" : k === ticks - 1 ? "end" : "middle", class: "pc-axis" });
      t.textContent = long ? fmtDate(s.d[i], false).replace(/ \d+$/, "") + " " + s.d[i].slice(8, 10) : fmtDate(s.d[i], false);
      svg.appendChild(t);
    }

    const line = s.c.map((v, i) => (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(v).toFixed(1)).join(" ");
    svg.appendChild(el("path", { d: line + ` L${X(n - 1).toFixed(1)} ${padT + ih} L${padL} ${padT + ih} Z`, fill: "url(#pg)" }));
    svg.appendChild(el("path", { d: line, fill: "none", stroke: tone, "stroke-width": 2, "stroke-linejoin": "round" }));
    svg.appendChild(el("circle", { cx: X(n - 1), cy: Y(s.c[n - 1]), r: 4, fill: tone, stroke: "var(--card)", "stroke-width": 2 }));

    const cross = el("line", { y1: padT, y2: padT + ih, class: "pc-cross", visibility: "hidden" });
    const dot = el("circle", { r: 4.5, fill: tone, stroke: "var(--card)", "stroke-width": 2, visibility: "hidden" });
    svg.append(cross, dot);
    const hit = el("rect", { x: padL, y: padT, width: iw, height: ih, fill: "transparent" });
    svg.appendChild(hit);

    const move = (ev) => {
      const box = svg.getBoundingClientRect();
      const px = ((ev.clientX - box.left) / box.width) * W;
      const tt = t0 + ((px - padL) / iw) * tSpan;
      let lo2 = 0, hi2 = n - 1;
      while (hi2 - lo2 > 1) { const m = (lo2 + hi2) >> 1; if (T[m] < tt) lo2 = m; else hi2 = m; }
      const i = Math.abs(T[lo2] - tt) <= Math.abs(T[hi2] - tt) ? lo2 : hi2;
      const x = X(i), y = Y(s.c[i]);
      cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", x); dot.setAttribute("cy", y); dot.setAttribute("visibility", "visible");
      const chg = (s.c[i] / s.c[0] - 1) * 100;
      tip.textContent = "";
      const a = document.createElement("div"); a.className = "pt-date"; a.textContent = fmtDate(s.d[i], true);
      const b = document.createElement("div"); b.className = "pt-price"; b.textContent = NUM(s.c[i]);
      const c = document.createElement("div"); c.className = chg >= 0 ? "val-pos pt-chg" : "val-neg pt-chg"; c.textContent = (chg >= 0 ? "+" : "") + chg.toFixed(1) + "% from start";
      tip.append(a, b, c);
      tip.style.opacity = 1;
      const tw = tip.offsetWidth;
      tip.style.left = Math.max(4, Math.min(W - tw - 4, x - tw / 2)) + "px";
      tip.style.top = Math.max(0, y - tip.offsetHeight - 12) + "px";
    };
    const leave = () => { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.style.opacity = 0; };
    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerleave", leave);
    host.insertBefore(svg, tip);
  }

  document.querySelectorAll("#price-range button").forEach((b) => {
    b.addEventListener("click", () => {
      range = b.dataset.range;
      document.querySelectorAll("#price-range button").forEach((x) => x.classList.toggle("active", x === b));
      draw();
    });
  });

  draw();
  if ("ResizeObserver" in window) {
    let w = host.clientWidth;
    new ResizeObserver(() => { if (Math.abs(host.clientWidth - w) > 2) { w = host.clientWidth; draw(); } }).observe(host);
  }
})();
