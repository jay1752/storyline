// Investment rules page: edit one rule at a time (Save enables only when something changed), reset a rule to the layer
// above it, or reset the whole table. Every action is one small JSON call and then a reload, so the badges stay truthful.
(function () {
  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    let out = {};
    try { out = await r.json(); } catch (e) { /* non-JSON error page */ }
    if (!r.ok) throw new Error(out.error || "Couldn't save that.");
    return out;
  }

  document.querySelectorAll("table.rules-table").forEach((table) => {
    const scope = table.dataset.scope, setId = table.dataset.set ? Number(table.dataset.set) : null;
    table.querySelectorAll("tbody tr").forEach((row) => {
      const val = row.querySelector(".rule-val"), on = row.querySelector(".rule-on");
      const save = row.querySelector(".rule-save"), reset = row.querySelector(".rule-reset"), msg = row.querySelector(".rule-msg");
      const startVal = val.value, startOn = on ? on.checked : true;  // daily-story rows have no On switch
      const say = (t, bad) => { msg.textContent = t || ""; msg.classList.toggle("bad", !!bad); };
      const changed = () => { save.disabled = val.value === startVal && (on ? on.checked : true) === startOn; say(""); };
      val.addEventListener("input", changed);
      val.addEventListener("change", changed);  // <select>
      if (on) on.addEventListener("change", changed);
      save.addEventListener("click", async () => {
        save.disabled = true;
        try { await (setId ? post("/rules/set/save", { set_id: setId, key: row.dataset.key, value: val.value, enabled: on ? on.checked : true }) : post("/rules/save", { scope, key: row.dataset.key, value: val.value, enabled: on ? on.checked : true })); location.reload(); }
        catch (e) { say(e.message, true); save.disabled = false; }
      });
      reset.addEventListener("click", async () => {
        reset.disabled = true;
        try { await (setId ? post("/rules/set/reset", { set_id: setId, key: row.dataset.key }) : post("/rules/reset", { scope, key: row.dataset.key })); location.reload(); }
        catch (e) { say(e.message, true); reset.disabled = false; }
      });
    });
  });

  [["reset-all-user", "user", "Reset ALL of your rules to the platform defaults? Stocks with their own rules keep them."],
   ["reset-all-platform", "platform", "Reset ALL platform defaults to the built-in values? This affects every user who hasn't set their own."]]
    .forEach(([id, scope, ask]) => {
      const b = document.getElementById(id);
      if (!b) return;
      b.addEventListener("click", async () => {
        if (!confirm(ask)) return;
        try { await post("/rules/reset", { scope }); location.reload(); } catch (e) { alert(e.message); }
      });
    });

  // screeners: create, rename, delete, reset all
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
    if (!confirm("Delete the screener \"" + b.dataset.name + "\"? Stocks that use it go back to your own rules.")) return;
    try { await post("/rules/set/delete", { set_id: Number(b.dataset.set) }); location.href = "/rules#screeners"; } catch (err) { alert(err.message); }
  }));
  const resetSet = document.getElementById("reset-all-set"), tbl = document.querySelector("table.rules-table[data-set]");
  if (resetSet && tbl) resetSet.addEventListener("click", async () => {
    if (!confirm("Put every rule in this screener back to your own rules as they are now?")) return;
    try { await post("/rules/set/reset", { set_id: Number(tbl.dataset.set) }); location.reload(); } catch (err) { alert(err.message); }
  });

  // every section folds and unfolds; the choice is remembered. A section that a link points at (#screeners, ?set=) always opens.
  const KEY = "tenetfolio.rules.fold";
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { /* private mode */ }
  document.querySelectorAll("section.section").forEach((sec) => {
    const head = sec.querySelector(":scope > .section-head"), title = head && head.querySelector(".section-title");
    if (!head || !title) return;
    const key = title.textContent.trim();
    const btn = document.createElement("button");
    btn.type = "button"; btn.className = "sec-toggle";
    btn.innerHTML = '<svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true"><path d="M5 8l5 5 5-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    const set = (collapsed, remember) => {
      sec.classList.toggle("collapsed", collapsed);
      btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
      btn.setAttribute("aria-label", (collapsed ? "Expand " : "Collapse ") + key);
      if (remember) { saved[key] = collapsed ? 1 : 0; try { localStorage.setItem(KEY, JSON.stringify(saved)); } catch (e) { /* ignore */ } }
    };
    const targeted = (sec.id && location.hash === "#" + sec.id) || (sec.id === "screeners" && new URLSearchParams(location.search).has("set"));
    set(!targeted && saved[key] === 1, false);
    btn.addEventListener("click", (e) => { e.stopPropagation(); set(!sec.classList.contains("collapsed"), true); });
    title.style.cursor = "pointer";
    title.addEventListener("click", () => set(!sec.classList.contains("collapsed"), true));
    title.insertBefore(btn, title.firstChild);  // inside the heading, so it sits on the same line as the title
  });
})();
