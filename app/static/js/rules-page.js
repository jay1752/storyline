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
    const scope = table.dataset.scope;
    table.querySelectorAll("tbody tr").forEach((row) => {
      const val = row.querySelector(".rule-val"), on = row.querySelector(".rule-on");
      const save = row.querySelector(".rule-save"), reset = row.querySelector(".rule-reset"), msg = row.querySelector(".rule-msg");
      const startVal = val.value, startOn = on.checked;
      const say = (t, bad) => { msg.textContent = t || ""; msg.classList.toggle("bad", !!bad); };
      const changed = () => { save.disabled = val.value === startVal && on.checked === startOn; say(""); };
      val.addEventListener("input", changed);
      on.addEventListener("change", changed);
      save.addEventListener("click", async () => {
        save.disabled = true;
        try { await post("/rules/save", { scope, key: row.dataset.key, value: val.value, enabled: on.checked }); location.reload(); }
        catch (e) { say(e.message, true); save.disabled = false; }
      });
      reset.addEventListener("click", async () => {
        reset.disabled = true;
        try { await post("/rules/reset", { scope, key: row.dataset.key }); location.reload(); }
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
})();
