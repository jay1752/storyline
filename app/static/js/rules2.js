// Investment rules page: tabs, one Save bar for every changed row, per-row reset, and the screener actions.
(function () {
  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    let out = {};
    try { out = await r.json(); } catch (e) { /* non-JSON error page */ }
    if (!r.ok) throw new Error(out.error || "Couldn't save that.");
    return out;
  }

  // ---- tabs (the address hash keeps the tab, so a reload or a shared link lands in the same place)
  const tabs = [...document.querySelectorAll(".rl-tab")], panels = [...document.querySelectorAll(".rl-panel")];
  const ids = tabs.map((t) => t.dataset.tab);
  function show(id) {
    tabs.forEach((t) => { const on = t.dataset.tab === id; t.classList.toggle("on", on); t.setAttribute("aria-selected", on ? "true" : "false"); });
    panels.forEach((p) => { p.hidden = p.id !== "tab-" + id; });
  }
  const fromHash = () => { const h = location.hash.replace("#", ""); return ids.includes(h) ? h : null; };
  show(fromHash() || (new URLSearchParams(location.search).has("set") ? "screeners" : "rules"));
  tabs.forEach((t) => t.addEventListener("click", () => { history.replaceState(null, "", location.pathname + location.search + "#" + t.dataset.tab); show(t.dataset.tab); }));
  window.addEventListener("hashchange", () => { const id = fromHash(); if (id) show(id); });
  document.querySelectorAll('a[href^="#"]').forEach((a) => a.addEventListener("click", () => { const id = a.getAttribute("href").slice(1); if (ids.includes(id)) setTimeout(() => show(id), 0); }));

  // ---- rows: remember what each one started as, mark the changed ones, count them in the bar
  const rows = [...document.querySelectorAll(".rl-row")];
  const bar = document.getElementById("rl-bar"), countEl = document.getElementById("rl-count"), plural = document.getElementById("rl-plural");
  const setIdOf = (row) => { const h = row.closest("[data-set]"); return h ? Number(h.dataset.set) : null; };
  const state = new Map();
  rows.forEach((row) => {
    const val = row.querySelector(".rule-val"), on = row.querySelector(".rule-on");
    state.set(row, { val: val.value, on: on ? on.checked : true });
  });
  const dirty = (row) => { const s = state.get(row), val = row.querySelector(".rule-val"), on = row.querySelector(".rule-on"); return val.value !== s.val || (on ? on.checked : true) !== s.on; };
  function refresh() {
    let n = 0;
    rows.forEach((row) => { const d = dirty(row); row.classList.toggle("dirty", d); n += d; const on = row.querySelector(".rule-on"); row.classList.toggle("off", !!on && !on.checked); });
    countEl.textContent = n; plural.textContent = n === 1 ? "" : "s"; bar.hidden = n === 0;
  }
  rows.forEach((row) => {
    ["input", "change"].forEach((ev) => row.addEventListener(ev, refresh));
    row.querySelector(".rl-reset").addEventListener("click", async () => {
      const msg = row.querySelector(".rl-msg"), setId = setIdOf(row);
      try { await (setId ? post("/rules/set/reset", { set_id: setId, key: row.dataset.key }) : post("/rules/reset", { scope: row.dataset.scope, key: row.dataset.key })); location.reload(); }
      catch (e) { msg.textContent = e.message; msg.classList.add("bad"); }
    });
  });
  refresh();

  document.getElementById("rl-discard").addEventListener("click", () => {
    rows.forEach((row) => { const s = state.get(row), val = row.querySelector(".rule-val"), on = row.querySelector(".rule-on"); val.value = s.val; if (on) on.checked = s.on; row.querySelector(".rl-msg").textContent = ""; });
    refresh();
  });
  document.getElementById("rl-save").addEventListener("click", async (e) => {
    const btn = e.currentTarget; btn.disabled = true; btn.textContent = "Saving…";
    let failed = 0;
    for (const row of rows.filter(dirty)) {
      const val = row.querySelector(".rule-val"), on = row.querySelector(".rule-on"), msg = row.querySelector(".rl-msg"), setId = setIdOf(row);
      const body = { key: row.dataset.key, value: val.value, enabled: on ? on.checked : true };
      try { await (setId ? post("/rules/set/save", { set_id: setId, ...body }) : post("/rules/save", { scope: row.dataset.scope, ...body })); msg.textContent = ""; state.set(row, { val: val.value, on: body.enabled }); }
      catch (err) { failed++; msg.textContent = err.message; msg.classList.add("bad"); row.scrollIntoView({ block: "center" }); }
    }
    if (!failed) { location.reload(); return; }
    btn.disabled = false; btn.textContent = "Save changes"; refresh();
  });

  // ---- reset-all, and the screener actions
  document.querySelectorAll("[data-reset-all]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(b.dataset.ask)) return;
    try { await post("/rules/reset", { scope: b.dataset.resetAll }); location.reload(); } catch (e) { alert(e.message); }
  }));
  const form = document.getElementById("set-new");
  if (form) form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = document.getElementById("set-msg");
    try { const out = await post("/rules/set/create", { name: document.getElementById("set-name").value }); location.href = "/rules?set=" + out.id + "#screeners"; }
    catch (err) { msg.textContent = err.message; msg.classList.add("bad"); }
  });
  document.querySelectorAll(".set-rename").forEach((b) => b.addEventListener("click", async () => {
    const name = prompt("New name for this screener", b.dataset.name);
    if (!name || name === b.dataset.name) return;
    try { await post("/rules/set/rename", { set_id: Number(b.dataset.set), name }); location.reload(); } catch (err) { alert(err.message); }
  }));
  document.querySelectorAll(".set-delete").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm('Delete the screener "' + b.dataset.name + '"? Stocks that use it go back to your own rules.')) return;
    try { await post("/rules/set/delete", { set_id: Number(b.dataset.set) }); location.href = "/rules#screeners"; } catch (err) { alert(err.message); }
  }));
  const resetSet = document.getElementById("reset-all-set");
  if (resetSet) resetSet.addEventListener("click", async () => {
    if (!confirm("Put every rule in this screener back to your own rules as they are now?")) return;
    try { await post("/rules/set/reset", { set_id: Number(resetSet.dataset.set) }); location.reload(); } catch (err) { alert(err.message); }
  });
})();
