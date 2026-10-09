/* Analytics page: choose which section groups are shown (multi-select), with presets.
   State: <html data-view="perf health"> is set by an inline script before first paint (from ?show=, then localStorage), so the page never
   flashes all sections. This file wires the controls, persists the choice and mirrors it into the URL for sharing. */
(function () {
  const bar = document.getElementById("viewbar");
  const de = document.documentElement;
  const tabs = bar ? Array.from(bar.querySelectorAll(".vtab")) : [];
  const avail = tabs.map((b) => b.dataset.grp);

  const current = () => (de.dataset.view || "").split(" ").filter(Boolean);

  function render(sel) {
    const ordered = avail.filter((k) => sel.includes(k));
    de.dataset.view = ordered.join(" ");
    tabs.forEach((b) => {
      const on = ordered.includes(b.dataset.grp);
      b.setAttribute("aria-pressed", on ? "true" : "false");
      b.classList.toggle("on", on);
    });
    try { localStorage.setItem("stocktale.view", ordered.join(",")); } catch (e) { /* private mode */ }
    const u = new URL(location.href);
    u.searchParams.set("show", ordered.join(","));
    history.replaceState(null, "", u);
  }

  if (bar) {
    render(current());
    tabs.forEach((b) => b.addEventListener("click", () => {
      const sel = current();
      const i = sel.indexOf(b.dataset.grp);
      if (i > -1) { if (sel.length > 1) sel.splice(i, 1); } else { sel.push(b.dataset.grp); }  // keep at least one section on
      render(sel);
    }));
    bar.querySelectorAll("[data-preset]").forEach((b) => b.addEventListener("click", () => {
      const want = b.dataset.preset === "all" ? avail : b.dataset.preset.split(",").filter((k) => avail.includes(k));
      if (want.length) render(want);
    }));
  }

  // Quarterly / Annual switch inside Performance
  const sw = document.getElementById("perf-switch");
  if (sw) {
    const paint = () => sw.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x.dataset.pane === de.dataset.perf));
    paint();
    sw.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      de.dataset.perf = b.dataset.pane;
      try { localStorage.setItem("stocktale.perf", b.dataset.pane); } catch (err) { /* private mode */ }
      paint();
    });
  }

  // A link or hash that points into a hidden group (e.g. the quarterly range buttons) opens that group.
  function reveal(hash) {
    if (!hash || hash.length < 2) return;
    const el = document.querySelector(hash);
    const grp = el && el.closest(".grp");
    if (grp && bar && !current().includes(grp.dataset.grp)) render(current().concat(grp.dataset.grp));
    if (el && el.dataset.pane && el.dataset.pane !== de.dataset.perf && sw) { de.dataset.perf = el.dataset.pane; sw.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x.dataset.pane === de.dataset.perf)); }
  }
  reveal(location.hash);
  window.addEventListener("hashchange", () => reveal(location.hash));
})();
