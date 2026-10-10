// Admin > Scheduled jobs: pause/resume, edit time + weekdays, reset to default, run now. Every action is a small JSON call
// followed by a reload so the table (last run, next run, badges) is always read back from the server.
(function () {
  const table = document.getElementById("jobs-table");
  if (!table) return;
  const NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  async function post(key, action, body) {
    const r = await fetch("/admin/jobs/" + key + "/" + action, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    let out = {};
    try { out = await r.json(); } catch (e) { /* non-JSON */ }
    if (!r.ok) throw new Error(out.error || "That didn't work.");
    return out;
  }

  table.querySelectorAll("tbody tr[data-key]").forEach((row) => {
    const key = row.dataset.key;
    const msg = row.querySelector(".job-msg");
    const say = (t, bad) => { msg.textContent = t || ""; msg.classList.toggle("bad", !!bad); };
    const on = row.querySelector(".job-on");
    const days = () => row.dataset.days.split(",").filter(Boolean).map(Number);

    on.addEventListener("change", async () => {
      try { await post(key, "save", row.dataset.every ? { enabled: on.checked, every: +row.dataset.every } : { enabled: on.checked, time: row.dataset.time, days: days() }); location.reload(); }
      catch (e) { on.checked = !on.checked; say(e.message, true); }
    });

    row.querySelector(".job-run").addEventListener("click", async (ev) => {
      const b = ev.currentTarget; b.disabled = true; say("Starting…");
      try { const out = await post(key, "run"); say("Started — " + out.started + ". Reload to follow it."); setTimeout(() => location.reload(), 1800); }
      catch (e) { say(e.message, true); b.disabled = false; }
    });

    row.querySelector(".job-edit").addEventListener("click", () => {
      let ed = row.nextElementSibling && row.nextElementSibling.classList.contains("job-editor") ? row.nextElementSibling : null;
      if (ed) { ed.remove(); return; }
      ed = document.createElement("tr"); ed.className = "job-editor";
      const td = document.createElement("td"); td.colSpan = 6;
      const wrap = document.createElement("div"); wrap.className = "job-edit-form";
      if (row.dataset.every) {  // interval job (e.g. the hourly exchange rate): just the number of minutes
        const n = document.createElement("input"); n.type = "number"; n.min = 5; n.max = 1440; n.step = 5; n.value = row.dataset.every; n.className = "form-input"; n.setAttribute("aria-label", "Minutes between runs");
        const lab2 = document.createElement("span"); lab2.className = "section-note"; lab2.textContent = "minutes between runs (5 to 1440)";
        const save2 = document.createElement("button"); save2.type = "button"; save2.className = "btn btn-sm btn-primary"; save2.textContent = "Save";
        const reset2 = document.createElement("button"); reset2.type = "button"; reset2.className = "btn btn-sm"; reset2.textContent = "↺ Reset to default"; reset2.title = "Back to every " + row.dataset.defaultEvery + " minutes";
        const cancel2 = document.createElement("button"); cancel2.type = "button"; cancel2.className = "btn btn-sm btn-ghost"; cancel2.textContent = "Cancel";
        const m3 = document.createElement("span"); m3.className = "rule-msg";
        wrap.append(n, lab2, save2, reset2, cancel2, m3); td.appendChild(wrap); ed.appendChild(td); row.after(ed);
        const bad = (e) => { m3.textContent = e.message; m3.classList.add("bad"); };
        save2.onclick = async () => { try { await post(key, "save", { enabled: on.checked, every: +n.value }); location.reload(); } catch (e) { bad(e); } };
        reset2.onclick = async () => { try { await post(key, "reset"); location.reload(); } catch (e) { bad(e); } };
        cancel2.onclick = () => ed.remove();
        return;
      }
      const t = document.createElement("input"); t.type = "time"; t.value = row.dataset.time; t.className = "form-input"; t.setAttribute("aria-label", "Start time, IST");
      const lab = document.createElement("span"); lab.className = "section-note"; lab.textContent = "starts at (IST) on:";
      const dayBoxes = NAMES.map((n, i) => {
        const l = document.createElement("label"); l.className = "day-chip";
        const c = document.createElement("input"); c.type = "checkbox"; c.value = i; c.checked = days().includes(i);
        l.append(c, document.createTextNode(n)); return [l, c];
      });
      const save = document.createElement("button"); save.type = "button"; save.className = "btn btn-sm btn-primary"; save.textContent = "Save";
      const reset = document.createElement("button"); reset.type = "button"; reset.className = "btn btn-sm"; reset.textContent = "↺ Reset to default";
      reset.title = "Back to " + row.dataset.defaultTime + " on " + row.dataset.defaultDays.split(",").map((d) => NAMES[+d]).join(", ");
      const cancel = document.createElement("button"); cancel.type = "button"; cancel.className = "btn btn-sm btn-ghost"; cancel.textContent = "Cancel";
      const m2 = document.createElement("span"); m2.className = "rule-msg";
      wrap.append(t, lab, ...dayBoxes.map((x) => x[0]), save, reset, cancel, m2);
      td.appendChild(wrap); ed.appendChild(td); row.after(ed);
      const fail = (e) => { m2.textContent = e.message; m2.classList.add("bad"); };
      save.onclick = async () => {
        const picked = dayBoxes.filter((x) => x[1].checked).map((x) => +x[1].value);
        try { await post(key, "save", { enabled: on.checked, time: t.value, days: picked }); location.reload(); } catch (e) { fail(e); }
      };
      reset.onclick = async () => { try { await post(key, "reset"); location.reload(); } catch (e) { fail(e); } };
      cancel.onclick = () => ed.remove();
    });
  });
})();
