// Stock story page: run the analysis on demand (with progress polling) and keep the journal form sensible.
(function () {
  const btn = document.getElementById("run-btn");
  const msg = document.getElementById("run-msg");
  const label = btn ? btn.querySelector("span") : null;

  function say(text, bad) {
    if (!msg) return;
    msg.hidden = !text; msg.textContent = text || ""; msg.classList.toggle("bad", !!bad);
  }

  async function poll(id) {
    for (;;) {
      await new Promise((r) => setTimeout(r, 3000));
      let out;
      try { out = await (await fetch("/management/" + id + "/status")).json(); } catch (e) { continue; }
      const run = out.run;
      if (!run || run.status === "queued" || run.status === "running") continue;
      if (run.status === "success") { location.hash = "analysis"; location.reload(); return; }
      btn.disabled = false; label.textContent = "Run analysis again";
      say("The analysis didn't complete: " + (run.error || "unknown error"), true);
      return;
    }
  }

  function busy() { btn.disabled = true; label.textContent = "Analysing… about half a minute"; say("Reading the numbers, news and your journal…", false); }

  if (btn) {
    const id = btn.dataset.id;
    if (btn.dataset.running === "1") { busy(); poll(id); }
    btn.addEventListener("click", async () => {
      busy();
      try {
        const resp = await fetch("/management/" + id + "/analyze", { method: "POST" });
        const out = await resp.json();
        if (!resp.ok) { btn.disabled = false; label.textContent = "Run analysis again"; say(out.error || "Couldn't start the analysis.", true); return; }
        poll(id);
      } catch (e) { btn.disabled = false; label.textContent = "Run analysis again"; say("Couldn't reach the server.", true); }
    });
  }

  // journal form: only ask for what each kind of entry needs
  const type = document.getElementById("jr-type");
  if (type) {
    const qty = document.getElementById("jr-qty-wrap"), price = document.getElementById("jr-price-wrap"), hint = document.getElementById("jr-hint");
    const sync = () => {
      const t = type.value;
      qty.hidden = t === "note" || t === "exit";
      price.hidden = t === "note";
      hint.textContent = { add: "Updates your quantity and average cost.", trim: "Reduces your quantity; your average cost stays.", exit: "Closes the position (it moves to Closed positions).", note: "Doesn't change the position." }[t];
    };
    type.addEventListener("change", sync); sync();
  }

  // show stored UTC times in the viewer's own timezone
  document.querySelectorAll("time[data-utc]").forEach((t) => {
    const raw = t.dataset.utc.replace(" ", "T");
    const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(raw) ? raw : raw + "Z");
    if (isNaN(d)) return;
    t.textContent = t.dataset.fmt === "date"
      ? d.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" })
      : d.toLocaleString([], { day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit" });
    t.title = d.toString();
  });
})();

// Fundamentals scan: edit one rule for THIS stock (override), or send it back to the user's default with ↺.
(function () {
  const table = document.getElementById("scan-table");
  const layersEl = document.getElementById("rule-layers");
  if (!table || !layersEl) return;
  let layers;
  try { layers = JSON.parse(layersEl.textContent); } catch (e) { return; }
  const id = table.dataset.id;

  async function post(path, body) {
    const r = await fetch("/management/" + id + "/rules/" + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    let out = {};
    try { out = await r.json(); } catch (e) { /* ignore */ }
    if (!r.ok) throw new Error(out.error || "Couldn't save that.");
  }

  table.querySelectorAll("tr[data-rule]").forEach((row) => {
    const btn = row.querySelector(".scan-edit-btn");
    const rule = layers[row.dataset.rule];
    if (!btn || !rule) return;
    btn.addEventListener("click", () => {
      const cell = row.querySelector(".needs-cell");
      if (cell.dataset.editing) return;
      cell.dataset.editing = "1";
      cell.textContent = "";
      const op = document.createElement("span"); op.textContent = rule.op + " ";
      const input = document.createElement("input");
      input.type = "number"; input.step = "any"; input.value = rule.value; input.className = "form-input scan-input"; input.setAttribute("aria-label", rule.label + " threshold");
      const unit = document.createElement("span"); unit.textContent = " " + rule.unit;
      const lab = document.createElement("label"); lab.className = "scan-on";
      const on = document.createElement("input"); on.type = "checkbox"; on.checked = rule.enabled;
      lab.append(on, document.createTextNode(" use"));
      const save = document.createElement("button"); save.type = "button"; save.className = "btn btn-sm btn-primary"; save.textContent = "Save";
      const reset = document.createElement("button"); reset.type = "button"; reset.className = "btn btn-sm"; reset.textContent = "↺ Reset";
      reset.title = "Back to " + (rule.reset_source === "built-in" ? "the built-in default" : rule.reset_source === "platform" ? "the platform default" : "your default") + ": " + rule.op + " " + rule.reset_value + rule.unit;
      reset.disabled = !rule.customised;
      const cancel = document.createElement("button"); cancel.type = "button"; cancel.className = "btn btn-sm btn-ghost"; cancel.textContent = "Cancel";
      const msg = document.createElement("div"); msg.className = "rule-msg"; msg.setAttribute("role", "status");
      const row1 = document.createElement("div"); row1.className = "scan-editor"; row1.append(op, input, unit, lab);
      const row2 = document.createElement("div"); row2.className = "scan-editor"; row2.append(save, reset, cancel);
      cell.append(row1, row2, msg);
      input.focus();
      const fail = (e) => { msg.textContent = e.message; msg.classList.add("bad"); };
      save.onclick = async () => { try { await post("save", { key: row.dataset.rule, value: input.value, enabled: on.checked }); location.reload(); } catch (e) { fail(e); } };
      reset.onclick = async () => { try { await post("reset", { key: row.dataset.rule }); location.reload(); } catch (e) { fail(e); } };
      cancel.onclick = () => location.reload();
    });
  });
})();

// Per-stock daily story switch: "Follow my rules" (no override) / Always run / Never run
(function () {
  const sel = document.getElementById("story-mode");
  if (!sel) return;
  sel.addEventListener("change", async () => {
    const v = sel.value;
    try {
      const r = await fetch("/management/" + sel.dataset.id + "/rules/" + (v === "" ? "reset" : "save"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(v === "" ? { key: "story_stock" } : { key: "story_stock", value: Number(v), enabled: true }),
      });
      if (!r.ok) { const o = await r.json().catch(() => ({})); throw new Error(o.error || "Couldn't save that."); }
      sel.classList.add("saved");
      setTimeout(() => sel.classList.remove("saved"), 900);
    } catch (e) { alert(e.message); location.reload(); }
  });
})();
