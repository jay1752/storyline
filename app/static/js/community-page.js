// Community: a read-only daily briefing on top, then "Community notes" — conversations newest-first, 10 per page,
// each with its replies inline. All text goes through textContent / the DOM-only markdown renderer
// (renderMarkdown in chat-widget.js); message bodies are never assigned to innerHTML.
(function () {
  const notes = document.getElementById("notes");
  if (!notes) return;
  const $ = (id) => document.getElementById(id);
  const EMOJIS = ["👍", "🐂", "🐻", "🔥", "👀"];
  const INLINE = 3; // replies shown before "show earlier replies"
  const filterSymbol = notes.dataset.symbol || "";
  const q = filterSymbol ? "&symbol=" + encodeURIComponent(filterSymbol) : "";

  // ---------- helpers ----------
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
  const icon = (id) => { const ns = "http://www.w3.org/2000/svg"; const s = document.createElementNS(ns, "svg"); s.setAttribute("class", "icon icon-sm"); const u = document.createElementNS(ns, "use"); u.setAttribute("href", "#" + id); s.appendChild(u); return s; };
  async function api(path, opts) {
    const resp = await fetch(path, opts);
    if (resp.redirected) throw new Error("Your session expired — log in again.");
    let data = {};
    try { data = await resp.json(); } catch (e) { /* non-JSON error page */ }
    if (!resp.ok) throw new Error(data.error || "Something went wrong.");
    return data;
  }
  const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  function flash(msg) { const f = el("div", "toast", msg); document.body.appendChild(f); setTimeout(() => f.remove(), 3500); }

  function whenLabel(iso) {
    const d = new Date(iso), now = new Date();
    const same = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
    const y = new Date(now); y.setDate(now.getDate() - 1);
    const t = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    if (same(d, now)) return "Today, " + t;
    if (same(d, y)) return "Yesterday, " + t;
    return d.toLocaleDateString([], { day: "numeric", month: "short", year: d.getFullYear() === now.getFullYear() ? undefined : "numeric" }) + ", " + t;
  }
  const replyLabel = (n) => (n === 0 ? "Reply" : n + (n === 1 ? " reply" : " replies"));

  // ================= Daily briefing (read-only) =================
  const brief = JSON.parse($("brief-data").textContent);
  function showBriefing(b) {
    const body = $("brief-body");
    body.textContent = "";
    if (!b) {
      body.classList.remove("clamp");
      body.appendChild(el("p", "muted", "Today's briefing is written automatically around 07:30 IST. Check back soon."));
      $("brief-more").hidden = true;
      return;
    }
    const d = new Date(b.date + "T00:00:00");
    $("brief-date-label").textContent = d.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long", year: "numeric" });
    // the card header already carries the title, so drop the body's first line
    body.appendChild(renderMarkdown(b.body.replace(/^\*\*Daily market briefing[^\n]*\*\*\n+/, "")));
    body.classList.add("clamp");
    const more = $("brief-more");
    more.hidden = false; more.textContent = "Read the full briefing";
    more.onclick = () => { const closed = body.classList.toggle("clamp"); more.textContent = closed ? "Read the full briefing" : "Show less"; };
    const m = b.meta || {};
    $("brief-model").textContent = m.degraded ? "AI summary unavailable — headlines only" : (m.model ? "Written by " + m.model : "");
  }
  showBriefing(brief.briefing);
  if (brief.dates && brief.dates.length > 1) {
    const sel = $("brief-date");
    brief.dates.forEach((dt, i) => sel.add(new Option(i === 0 ? "Latest · " + dt : dt, dt)));
    $("brief-pick").hidden = false;
    sel.onchange = async () => { try { showBriefing((await api("/community/api/briefing?date=" + sel.value)).briefing); } catch (e) { flash(e.message); } };
  }

  // ================= Community notes =================
  const state = { threads: new Map(), minId: Infinity, maxId: 0, pending: [] };
  // per-thread reply state: loaded replies, whether all are shown
  const tstate = new Map();

  function renderBody(p, container) {
    container.textContent = "";
    if (p.deleted) { container.appendChild(el("span", "muted", "This message was deleted.")); return; }
    container.appendChild(renderMarkdown(p.body));
  }

  function reactionBar(p, wrap) {
    wrap.textContent = "";
    if (p.deleted) return;
    EMOJIS.forEach((e) => {
      const r = (p.reactions || {})[e];
      if (!r) return;
      const b = el("button", "rx" + (r.mine ? " mine" : ""), e + " " + r.n);
      b.type = "button"; b.onclick = () => toggleReaction(p, e);
      wrap.appendChild(b);
    });
  }

  async function toggleReaction(p, emoji) {
    try { p.reactions = (await post("/community/api/react", { id: p.id, emoji })).reactions; refreshMsg(p); } catch (e) { flash(e.message); }
  }

  function refreshMsg(p) {
    document.querySelectorAll('.msg[data-id="' + p.id + '"]').forEach((n) => {
      const main = n.querySelector(".msg-main");
      renderBody(p, main.querySelector(".msg-body"));
      reactionBar(p, main.querySelector(".msg-react"));
      const rb = main.querySelector(".reply-btn.toggle span");
      if (rb) rb.textContent = replyLabel(p.reply_count);
      n.classList.toggle("gone", !!p.deleted);
    });
  }

  // one message (a conversation's opening message, or a reply)
  function messageNode(p, onReplyClick) {
    const isReply = !!p.parent_id;
    const n = el("div", "msg" + (isReply ? " reply" : "") + (p.deleted ? " gone" : ""));
    n.dataset.id = p.id;
    const av = el("div", "msg-av"); av.textContent = (p.author || "?")[0].toUpperCase();
    const main = el("div", "msg-main");
    const head = el("div", "msg-head");
    head.appendChild(el("b", "msg-author", p.author));
    const t = el("time", "msg-time", whenLabel(p.created)); t.title = new Date(p.created).toLocaleString();
    head.appendChild(t);
    if (p.symbol) { const a = el("a", "msg-sym", "$" + p.symbol.split(":")[1]); a.href = "/community?symbol=" + encodeURIComponent(p.symbol); a.title = "Conversations about " + p.symbol; head.appendChild(a); }
    const body = el("div", "msg-body md"); renderBody(p, body);
    const rx = el("div", "msg-react"); reactionBar(p, rx);
    main.append(head, body, rx);

    const foot = el("div", "msg-foot");
    if (!p.deleted) {
      const add = el("button", "reply-btn add-rx"); add.type = "button"; add.title = "Add reaction"; add.appendChild(icon("i-smile"));
      add.onclick = (ev) => {
        ev.stopPropagation();
        foot.querySelectorAll(".rx-pick").forEach((x) => x.remove());
        const pick = el("span", "rx-pick");
        EMOJIS.forEach((e) => { const b = el("button", "rx", e); b.type = "button"; b.onclick = () => { pick.remove(); toggleReaction(p, e); }; pick.appendChild(b); });
        foot.appendChild(pick);
        setTimeout(() => document.addEventListener("click", () => pick.remove(), { once: true }), 0);
      };
      foot.appendChild(add);
    }
    if (!isReply) {
      const rb = el("button", "reply-btn toggle"); rb.type = "button";
      rb.append(icon("i-chat"), el("span", null, replyLabel(p.reply_count)));
      rb.onclick = onReplyClick;
      foot.appendChild(rb);
    }
    if (p.mine) {
      const del = el("button", "reply-btn del-btn"); del.type = "button"; del.title = "Delete"; del.appendChild(icon("i-trash"));
      del.onclick = async () => {
        if (!confirm("Delete this message?")) return;
        try {
          await post("/community/api/delete", { id: p.id });
          p.deleted = true; p.body = ""; refreshMsg(p);
          if (isReply) { const ts = tstate.get(p.parent_id); if (ts) { ts.loaded.delete(p.id); const root = state.threads.get(p.parent_id); if (root) { root._post.reply_count = Math.max(0, root._post.reply_count - 1); refreshMsg(root._post); renderReplies(root._post.id); } } }
        } catch (e) { flash(e.message); }
      };
      foot.appendChild(del);
    }
    main.appendChild(foot);
    n.append(av, main);
    return n;
  }

  // ----- replies (inline, oldest-first; latest 3 unless expanded) -----
  function renderReplies(id) {
    const root = state.threads.get(id);
    const ts = tstate.get(id);
    if (!root || !ts) return;
    const p = root._post;
    const all = Array.from(ts.loaded.values()).sort((a, b) => a.id - b.id);
    const visible = ts.expanded ? all : all.slice(-INLINE);
    const box = root.querySelector(".replies");
    box.textContent = "";
    const hidden = Math.max(0, p.reply_count - visible.length);
    const earlier = root.querySelector(".earlier");
    earlier.hidden = hidden === 0;
    earlier.textContent = "Show " + hidden + " earlier " + (hidden === 1 ? "reply" : "replies");
    visible.forEach((r) => box.appendChild(messageNode(r)));
    root.querySelector(".note-replies").hidden = visible.length === 0 && hidden === 0 && root.querySelector(".reply-form").hidden;
  }

  async function expandReplies(id) {
    const ts = tstate.get(id);
    try {
      const out = await api("/community/api/thread/" + id);
      ts.loaded = new Map(out.replies.map((r) => [r.id, r]));
      ts.expanded = true;
      state.threads.get(id)._post.reply_count = out.replies.length;
      refreshMsg(state.threads.get(id)._post);
      renderReplies(id);
    } catch (e) { flash(e.message); }
  }

  function threadNode(p) {
    const card = el("article", "note");
    card._post = p;
    card.dataset.id = p.id;
    tstate.set(p.id, { loaded: new Map((p.replies || []).map((r) => [r.id, r])), expanded: false });

    const form = el("form", "reply-form"); form.hidden = true;
    const ta = el("textarea"); ta.rows = 1; ta.placeholder = "Write a reply…"; ta.maxLength = 4000;
    const send = el("button", "btn btn-sm btn-primary", "Reply"); send.type = "submit";
    form.append(ta, send);
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); } });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = ta.value.trim();
      if (!text) return;
      send.disabled = true;
      try {
        const out = await post("/community/api/post", { body: text, parent_id: p.id });
        ta.value = "";
        tstate.get(p.id).loaded.set(out.post.id, out.post);
        p.reply_count += 1;
        refreshMsg(p); renderReplies(p.id);
        card.querySelector(".note-replies").hidden = false;
      } catch (err) { flash(err.message); }
      send.disabled = false; ta.focus();
    });

    const open = () => { form.hidden = !form.hidden; card.querySelector(".note-replies").hidden = false; if (!form.hidden) ta.focus(); };
    const root = messageNode(p, open);
    const wrap = el("div", "note-replies");
    const earlier = el("button", "linklike earlier", ""); earlier.type = "button"; earlier.hidden = true;
    earlier.onclick = () => expandReplies(p.id);
    wrap.append(earlier, el("div", "replies"), form);
    card.append(root, wrap);
    state.threads.set(p.id, card);
    renderReplies(p.id);
    return card;
  }

  // ----- the list -----
  function updateEmpty() {
    $("empty").hidden = state.threads.size > 0;
  }

  function addThreads(posts, where) {
    const list = $("threads");
    posts.forEach((p) => {
      if (state.threads.has(p.id)) return;
      const node = threadNode(p);
      if (where === "top") list.insertBefore(node, list.firstChild); else list.appendChild(node);
      state.maxId = Math.max(state.maxId, p.id);
      state.minId = Math.min(state.minId, p.id);
    });
    updateEmpty();
  }

  async function loadMore(initial) {
    const btn = $("load-more");
    btn.disabled = true;
    try {
      const out = await api("/community/api/feed?x=1" + (initial ? "" : "&before=" + state.minId) + q);
      addThreads(out.posts, "bottom");
      // pagination cursor must advance even when every row on a page was a hidden/deleted conversation
      $("load-more-row").hidden = !out.has_more;
      $("older-note").textContent = out.has_more && out.older ? out.older + " older conversation" + (out.older === 1 ? "" : "s") : "";
      btn.textContent = "Load " + Math.min(10, out.older || 10) + " more conversation" + ((out.older || 10) === 1 ? "" : "s");
    } catch (e) { flash(e.message); }
    btn.disabled = false;
  }
  $("load-more").onclick = () => loadMore(false);

  // ----- composing -----
  const ta = $("composer-text");
  const grow = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 220) + "px"; };
  ta.addEventListener("input", grow);
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("composer").requestSubmit(); } });
  $("composer").addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = ta.value.trim();
    if (!text) return;
    $("composer-send").disabled = true;
    try {
      const out = await post("/community/api/post", { body: text });
      ta.value = ""; grow();
      out.post.replies = [];
      addThreads([out.post], "top");
      const node = state.threads.get(out.post.id);
      node.classList.add("fresh"); setTimeout(() => node.classList.remove("fresh"), 2500);
    } catch (err) { flash(err.message); }
    $("composer-send").disabled = false; ta.focus();
  });

  $("rename").onclick = async () => {
    const cur = $("my-name").textContent;
    const name = prompt("Display name shown on your messages (2–24 characters). Leave blank to go back to your default.", cur);
    if (name === null) return;
    try { await post("/community/api/name", { name }); location.reload(); } catch (e) { flash(e.message); }
  };

  // ----- live updates -----
  const banner = $("new-banner");
  function showPending() {
    const n = state.pending.length;
    banner.hidden = n === 0;
    banner.querySelector("button").textContent = n + " new conversation" + (n === 1 ? "" : "s") + " — show";
  }
  banner.querySelector("button").onclick = () => {
    state.pending.forEach((p) => addThreads([p], "top"));
    state.pending = []; showPending();
    notes.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  async function pollOnce() {
    const ids = Array.from(state.threads.keys()).join(",");
    const out = await api("/community/api/poll?after=" + state.maxId + "&ids=" + ids + q);
    const nearTop = notes.getBoundingClientRect().top > -300;
    out.posts.forEach((p) => {
      if (state.threads.has(p.id) || state.pending.some((x) => x.id === p.id)) return;
      if (nearTop) addThreads([p], "top"); else state.pending.push(p);
      state.maxId = Math.max(state.maxId, p.id);
    });
    showPending();
    for (const [id, u] of Object.entries(out.updates || {})) {
      const node = state.threads.get(+id);
      if (!node) continue;
      const p = node._post;
      const changed = p.reply_count !== u.reply_count;
      p.reactions = u.reactions; p.deleted = u.deleted; p.reply_count = u.reply_count;
      refreshMsg(p);
      if (changed) {
        try {
          const t = await api("/community/api/thread/" + id);
          const ts = tstate.get(+id);
          const src = ts.expanded ? t.replies : t.replies.slice(-INLINE);
          ts.loaded = new Map(src.map((r) => [r.id, r]));
          renderReplies(+id);
        } catch (e) { /* next tick */ }
      }
    }
  }
  setInterval(() => { if (!document.hidden) pollOnce().catch(() => {}); }, 15000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollOnce().catch(() => {}); });

  loadMore(true);
})();
