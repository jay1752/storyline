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
