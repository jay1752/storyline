// Portfolio: scope tabs (All / India / US), allocation tabs, holdings filter + sort.
(function () {
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  // scope tabs
  const scopeBar = document.getElementById("pf-scope");
  if (scopeBar) {
    scopeBar.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      const scope = b.dataset.scope;
      $$("button", scopeBar).forEach((x) => x.classList.toggle("active", x === b));
      $$(".pf-scope").forEach((s) => { s.hidden = s.dataset.scope !== scope; });
      $$(".pf-group").forEach((g) => { g.hidden = scope !== "all" && g.dataset.market.toLowerCase() !== scope; });
    });
  }

  // allocation tabs
  const allocTabs = document.getElementById("pf-alloc-tabs");
  if (allocTabs) {
    allocTabs.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      $$("button", allocTabs).forEach((x) => x.classList.toggle("active", x === b));
      $$(".pf-donut-set").forEach((s) => { s.hidden = s.dataset.set !== b.dataset.set; });
    });
  }

  // filter + sort (within each market group)
  const search = document.getElementById("pf-search");
  const sort = document.getElementById("pf-sort");
  function apply() {
    const q = (search.value || "").trim().toLowerCase();
    const key = sort.value;
    $$(".pf-rows").forEach((box) => {
      const rows = $$(".pf-row", box);
      rows.sort((a, b) => {
        if (key === "name") return a.dataset.name.localeCompare(b.dataset.name);
        return parseFloat(b.dataset[key]) - parseFloat(a.dataset[key]);
      });
      rows.forEach((r) => { box.appendChild(r); r.hidden = q && !r.dataset.name.includes(q); });
    });
  }
  if (search) search.addEventListener("input", apply);
  if (sort) sort.addEventListener("change", apply);
})();
