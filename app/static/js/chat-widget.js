// Floating assistant, present on every page (base.html includes _chat_widget.html).
//
// - Context: the page/symbol is inferred from the URL and sent as a *hint*; the server still
//   gets real data through its own tools.
// - History is an opaque JSON string the server hands back after each turn (pydantic_ai's own
//   message format) — stored and replayed verbatim, never inspected here.
// - Attached files are not part of that history; the positions the server computed arrive as a
//   separate "proposal" event rendered as a confirm card (nothing is saved until the user clicks).
// - The conversation (display messages + history) lives in sessionStorage so it survives page
//   navigation within a tab. Markdown is rendered with DOM nodes only — model output is never
//   assigned to innerHTML.

const STORE_KEY = "storyline.chat.v1";
const IMPORT_MESSAGE = "Import my positions from this file.";

const chat = {
  messages: [], // {role, text, ctx, tools}
  history: null, // opaque server history
  open: false,
  expanded: false,
  streaming: false,
  abort: null,
  pendingFile: null,
  lastCtxKey: null,
};

// ---------- persistence ----------

function _save() {
  try {
    sessionStorage.setItem(STORE_KEY, JSON.stringify({
      messages: chat.messages, history: chat.history, open: chat.open, expanded: chat.expanded, lastCtxKey: chat.lastCtxKey,
    }));
  } catch (e) {
    // quota — keep the visible transcript, drop the (large) model history
    try {
      sessionStorage.setItem(STORE_KEY, JSON.stringify({ messages: chat.messages, history: null, open: chat.open, expanded: chat.expanded, lastCtxKey: chat.lastCtxKey }));
    } catch (e2) { /* storage unavailable — chat still works, just won't survive navigation */ }
  }
}

function _load() {
  try {
    const raw = sessionStorage.getItem(STORE_KEY);
    if (!raw) return;
    const s = JSON.parse(raw);
    chat.messages = Array.isArray(s.messages) ? s.messages : [];
    chat.history = s.history || null;
    chat.open = !!s.open;
    chat.expanded = !!s.expanded;
    chat.lastCtxKey = s.lastCtxKey || null;
  } catch (e) { /* corrupt — start fresh */ }
}

// ---------- page context ----------

function _pageContext() {
  const path = window.location.pathname;
  const analyticsMatch = path.match(/^\/analytics\/(.+)$/);
  if (analyticsMatch) return { page: "analytics", symbol: decodeURIComponent(analyticsMatch[1]), label: decodeURIComponent(analyticsMatch[1]) };
  if (path.startsWith("/management")) return { page: "management", symbol: null, label: "Tracked stocks" };
  if (path.startsWith("/portfolio")) return { page: "portfolio", symbol: null, label: "Portfolio" };
  return { page: null, symbol: null, label: "General" };
}

function _ctxKey(ctx) { return (ctx.page || "") + "|" + (ctx.symbol || ""); }

function _suggestions() {
  const ctx = _pageContext();
  const importIt = { label: "Import positions from a statement", attach: true };
  if (ctx.page === "management") {
    return { title: "Your tracked stocks", sub: "Ask about positions, the watchlist, or import a statement.", items: [importIt, { label: "What's on my watchlist?" }, { label: "Which of my positions need attention?" }] };
  }
  if (ctx.page === "portfolio") {
    return { title: "Your portfolio", sub: "Ask about concentration, risk, or import a statement.", items: [importIt, { label: "Summarize my portfolio" }, { label: "Which holding is my biggest risk?" }] };
  }
  if (ctx.page === "analytics" && ctx.symbol) {
    return { title: ctx.symbol, sub: "I can read this stock's fundamentals and price history.", items: [{ label: "Summarize this stock's fundamentals" }, { label: "Does it pass my hard filters?" }, { label: "How has the price moved lately?" }] };
  }
  return { title: "How can I help?", sub: "Open a stock or your portfolio and I'll work from that screen.", items: [importIt, { label: "What's on my watchlist?" }] };
}

const TOOL_LABEL = {
  get_stock_summary: (a) => "Fundamentals" + (a ? " · " + a : ""),
  get_price_history: (a) => "Price history" + (a ? " · " + a : ""),
  get_portfolio_or_watchlist: () => "Your tracked stocks",
  search_stocks: (a) => "Stock search" + (a ? " · " + a : ""),
  propose_positions: () => "Statement import",
};

// ---------- tiny DOM helpers ----------

function _el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function _icon(id, cls) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", "icon" + (cls ? " " + cls : ""));
  const use = document.createElementNS(ns, "use");
  use.setAttribute("href", "#" + id);
  svg.appendChild(use);
  return svg;
}

const $ = (id) => document.getElementById(id);

// ---------- markdown (DOM-only, safe by construction) ----------

function _inline(text, parent) {
  const re = /(`[^`\n]+`)|(\*\*[^*\n]+?\*\*)|(\[[^\]\n]+\]\(https?:\/\/[^\s)]+\))|(\*[^*\n]+?\*)|(_[^_\n]+?_)|(\$[A-Z]{2,8}:[A-Za-z0-9.\-]{1,15})|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;
  let last = 0;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
    const tok = m[0];
    if (m[1]) {
      parent.appendChild(_el("code", null, tok.slice(1, -1)));
    } else if (m[2]) {
      const s = _el("strong");
      _inline(tok.slice(2, -2), s);
      parent.appendChild(s);
    } else if (m[3]) {
      const mm = tok.match(/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/);
      const a = _el("a", null, mm[1]);
      a.href = mm[2];
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      parent.appendChild(a);
    } else if (m[6]) {
      const a = _el("a", "ticker", "$" + tok.split(":")[1]);
      a.href = "/analytics/" + tok.slice(1);
      parent.appendChild(a);
    } else if (m[7]) {
      const a = _el("a", null, tok);
      a.href = tok;
      a.target = "_blank";
      a.rel = "noopener noreferrer nofollow";
      parent.appendChild(a);
    } else {
      const em = _el("em");
      _inline(tok.slice(1, -1), em);
      parent.appendChild(em);
    }
    last = m.index + tok.length;
  }
  if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
}

const _TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const _isBlockStart = (l) => /^(#{1,4})\s/.test(l) || /^\s*([-*+]|\d+[.)])\s+/.test(l) || /^```/.test(l) || /^>/.test(l) || /^-{3,}\s*$/.test(l);

function _cells(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.trim());
}

function renderMarkdown(src) {
  const root = document.createDocumentFragment();
  const lines = String(src).replace(/\r\n/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }

    if (/^```/.test(line)) {
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++; // closing fence (absent while still streaming — rest is code)
      const pre = _el("pre");
      pre.appendChild(_el("code", null, buf.join("\n")));
      root.appendChild(pre);
      continue;
    }

    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      const n = _el("h" + Math.min(h[1].length + 1, 4));
      _inline(h[2], n);
      root.appendChild(n);
      i++;
      continue;
    }

    if (/^-{3,}\s*$/.test(line)) { root.appendChild(_el("hr")); i++; continue; }

    if (/^>/.test(line)) {
      const buf = [];
      while (i < lines.length && /^>/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ""));
      const bq = _el("blockquote");
      const p = _el("p");
      _inline(buf.join(" "), p);
      bq.appendChild(p);
      root.appendChild(bq);
      continue;
    }

    if (line.includes("|") && i + 1 < lines.length && _TABLE_SEP.test(lines[i + 1])) {
      const head = _cells(line);
      i += 2;
      const wrap = _el("div", "md-table");
      const table = _el("table");
      const thead = _el("thead");
      const trh = _el("tr");
      head.forEach((c) => { const th = _el("th"); _inline(c, th); trh.appendChild(th); });
      thead.appendChild(trh);
      table.appendChild(thead);
      const tbody = _el("tbody");
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        const tr = _el("tr");
        _cells(lines[i++]).forEach((c) => { const td = _el("td"); _inline(c, td); tr.appendChild(td); });
        tbody.appendChild(tr);
      }
      table.appendChild(tbody);
      wrap.appendChild(table);
      root.appendChild(wrap);
      continue;
    }

    const ul = /^\s*[-*+]\s+/.test(line);
    const ol = /^\s*\d+[.)]\s+/.test(line);
    if (ul || ol) {
      const list = _el(ol ? "ol" : "ul");
      const itemRe = ol ? /^\s*\d+[.)]\s+(.*)$/ : /^\s*[-*+]\s+(.*)$/;
      while (i < lines.length && itemRe.test(lines[i])) {
        const li = _el("li");
        _inline(lines[i].match(itemRe)[1], li);
        list.appendChild(li);
        i++;
      }
      root.appendChild(list);
      continue;
    }

    const buf = [];
    while (i < lines.length && lines[i].trim() && !_isBlockStart(lines[i]) && !(lines[i].includes("|") && i + 1 < lines.length && _TABLE_SEP.test(lines[i + 1]))) buf.push(lines[i++]);
    if (!buf.length) { buf.push(lines[i++]); }
    const p = _el("p");
    _inline(buf.join(" "), p);
    root.appendChild(p);
  }
  return root;
}

// ---------- rendering ----------

function _scrollDown(force) {
  const log = $("chat-log");
  if (!log) return;
  const near = log.scrollHeight - log.scrollTop - log.clientHeight < 140;
  if (force || near) log.scrollTop = log.scrollHeight;
}

function _renderContext() {
  const box = $("chat-context");
  if (!box) return;
  const ctx = _pageContext();
  box.textContent = "";
  box.append("Viewing ", Object.assign(_el("b"), { textContent: ctx.label }));
  const input = $("chat-input");
  if (input) input.placeholder = ctx.page === "analytics" ? "Ask about " + ctx.symbol + "…" : ctx.page ? "Ask about this page…" : "Ask anything about your stocks…";
}

function _sourcesRow(tools) {
  const row = _el("div", "chat-sources");
  row.appendChild(_el("span", "label", "Used"));
  const seen = new Set();
  for (const t of tools) {
    const fn = TOOL_LABEL[t.name];
    const text = fn ? fn(t.arg) : t.name;
    if (seen.has(text)) continue;
    seen.add(text);
    row.appendChild(_el("span", "chat-source", text));
  }
  return row;
}

function _copyButton(getText) {
  const btn = _el("button");
  btn.type = "button";
  btn.append(_icon("i-copy", "icon-sm"), "Copy");
  btn.onclick = async () => {
    try {
      await navigator.clipboard.writeText(getText());
      btn.textContent = "";
      btn.append(_icon("i-check", "icon-sm"), "Copied");
      setTimeout(() => { btn.textContent = ""; btn.append(_icon("i-copy", "icon-sm"), "Copy"); }, 1400);
    } catch (e) { /* clipboard blocked — nothing to do */ }
  };
  return btn;
}

function _viaLabel(m) {
  const via = _el("span", "chat-via", (m.fallback ? "fallback · " : "") + m.model);
  via.title = m.fallback ? "Your main model was unavailable, so the fallback model answered." : "Model that wrote this answer";
  return via;
}

function _messageNode(m) {
  if (m.role === "divider") return _el("div", "chat-divider", m.text);
  const wrap = _el("div", "chat-msg chat-msg-" + m.role);
  if (m.role === "user") {
    wrap.appendChild(_el("div", "chat-text", m.text));
    return wrap;
  }
  if (m.tools && m.tools.length) wrap.appendChild(_sourcesRow(m.tools));
  const body = _el("div", "chat-text md");
  body.appendChild(renderMarkdown(m.text));
  wrap.appendChild(body);
  const actions = _el("div", "chat-actions");
  actions.appendChild(_copyButton(() => m.text));
  if (m.model) actions.appendChild(_viaLabel(m));
  wrap.appendChild(actions);
  return wrap;
}

function _renderLog() {
  const log = $("chat-log");
  if (!log) return;
  log.textContent = "";
  if (!chat.messages.length) {
    const s = _suggestions();
    const empty = _el("div", "chat-empty");
    const head = _el("div");
    head.append(_el("h3", null, s.title), _el("p", null, s.sub));
    const list = _el("div", "chat-suggestions");
    for (const item of s.items) {
      const b = _el("button", "chat-suggest");
      b.type = "button";
      b.append(_el("span", null, item.label), _icon(item.attach ? "i-paperclip" : "i-arrow-right", "icon-sm"));
      b.onclick = () => {
        if (item.attach) pickChatFile(true);
        else { $("chat-input").value = item.label; $("chat-form").requestSubmit(); }
      };
      list.appendChild(b);
    }
    empty.append(head, list);
    log.appendChild(empty);
    return;
  }
  for (const m of chat.messages) log.appendChild(_messageNode(m));
  _scrollDown(true);
}

function _setStreaming(on) {
  chat.streaming = on;
  const btn = $("chat-send");
  if (!btn) return;
  btn.classList.toggle("stop", on);
  btn.title = on ? "Stop" : "Send";
  btn.setAttribute("aria-label", on ? "Stop" : "Send");
  btn.textContent = "";
  btn.appendChild(_icon(on ? "i-x" : "i-arrow-up"));
}

// ---------- panel ----------

function _applyOpen() {
  const panel = $("chat-panel");
  const fab = $("chat-fab");
  if (!panel) return;
  panel.classList.toggle("open", chat.open);
  panel.classList.toggle("expanded", chat.expanded);
  if (fab) fab.classList.toggle("hidden", chat.open);
  const ex = $("chat-expand");
  if (ex) {
    ex.title = chat.expanded ? "Shrink" : "Expand";
    ex.textContent = "";
    ex.appendChild(_icon(chat.expanded ? "i-shrink" : "i-expand"));
  }
}

function toggleChat(force) {
  chat.open = typeof force === "boolean" ? force : !chat.open;
  _applyOpen();
  _save();
  if (chat.open) {
    _renderContext();
    if (!chat.messages.length) _renderLog();
    setTimeout(() => { const i = $("chat-input"); if (i) i.focus(); }, 30);
  }
}

// Called from the Management "Add" page's import card.
function openChatForImport() {
  if (!chat.open) toggleChat(true);
  pickChatFile(true);
}

function newChat() {
  if (chat.abort) chat.abort.abort();
  chat.messages = [];
  chat.history = null;
  chat.lastCtxKey = null;
  chat.pendingFile = null;
  _renderPendingFile();
  _setStreaming(false);
  _renderLog();
  _save();
}

// ---------- attachments ----------

// autoSend: the "Import positions" action — picking a file IS the whole intent.
function pickChatFile(autoSend) {
  const input = $("chat-file");
  input.dataset.autosend = autoSend ? "1" : "";
  input.click();
}

function onChatFilePicked(input) {
  const file = input.files && input.files[0];
  input.value = "";
  if (!file) return;
  chat.pendingFile = file;
  _renderPendingFile();
  if (input.dataset.autosend) $("chat-form").requestSubmit();
}

function clearChatFile() {
  chat.pendingFile = null;
  _renderPendingFile();
}

function _renderPendingFile() {
  const box = $("chat-attachment");
  if (!box) return;
  box.textContent = "";
  box.classList.toggle("has", !!chat.pendingFile);
  if (!chat.pendingFile) return;
  const x = _el("button");
  x.type = "button";
  x.setAttribute("aria-label", "Remove attachment");
  x.appendChild(_icon("i-x", "icon-sm"));
  x.onclick = clearChatFile;
  box.append(_icon("i-paperclip", "icon-sm"), _el("span", "name", chat.pendingFile.name), x);
}

// ---------- import proposal card ----------

function _fmt(n, digits) {
  if (n === null || n === undefined) return "—";
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: digits });
}

function _actionText(r) {
  if (r.action === "new") return "New position";
  const e = r.existing || {};
  if (r.action === "convert") return "Watchlist → position";
  return "Update: qty " + _fmt(e.quantity, 4) + " → " + _fmt(r.quantity, 4) + ", avg " + _fmt(e.buy_price, 2) + " → " + _fmt(r.avg_price, 2);
}

function _renderProposal(p) {
  const log = $("chat-log");
  if (!log) return;
  const card = _el("div", "chat-card");
  card.appendChild(_el("div", "chat-card-title", p.rows.length ? "Review import — " + p.rows.length + " position" + (p.rows.length === 1 ? "" : "s") : "Nothing to import from this file"));

  for (const r of p.rows) {
    const row = _el("div", "chat-card-row");
    const head = _el("div", "chat-card-head");
    if (r.needs_review) row.classList.add("needs-review");
    head.append(_el("strong", null, r.symbol), _el("span", null, _fmt(r.quantity, 4) + " sh @ " + (r.avg_price === null ? "price unknown" : _fmt(r.avg_price, 2))));
    row.append(head, _el("div", "chat-card-sub", r.company_name + " · " + _actionText(r) + " · since " + r.anchor_date + " · " + r.txn_count + " row" + (r.txn_count === 1 ? "" : "s")));
    if (r.last_price !== null && r.last_price !== undefined) {
      const pct = r.return_pct === null || r.return_pct === undefined ? "" : " · " + (r.return_pct >= 0 ? "+" : "") + r.return_pct + "% vs your average";
      row.appendChild(_el("div", "chat-card-sub", "Now " + _fmt(r.last_price, 2) + " (" + r.last_price_date + ")" + pct));
    }
    if (r.match && r.match.note) row.appendChild(_el("div", "chat-card-sub", r.match.note));
    if (r.price_check && r.price_check.status === "verified") row.appendChild(_el("div", "chat-card-ok", "✓ " + r.price_check.text));
    for (const w of r.warnings || []) row.appendChild(_el("div", "chat-card-warn", "⚠ " + w));
    if (r.needs_review) row.appendChild(_el("div", "chat-card-warn", "⚠ Needs your review before you confirm."));
    card.appendChild(row);
  }

  if (p.skipped && p.skipped.length) {
    const sk = _el("div", "chat-card-skipped");
    sk.appendChild(_el("div", null, "Not imported:"));
    for (const s of p.skipped) sk.appendChild(_el("div", null, "• " + s.label + " — " + s.reason));
    card.appendChild(sk);
  }

  const status = _el("div", "chat-card-status");
  if (p.rows.length) {
    const actions = _el("div", "chat-card-actions");
    const review = p.rows.filter((r) => r.needs_review).length;
    const confirm = _el("button", null, review ? "Confirm import (" + review + " to review)" : "Confirm import");
    confirm.type = "button";
    const cancel = _el("button", "secondary", "Cancel");
    cancel.type = "button";
    const finish = (msg) => { actions.remove(); status.textContent = msg; };

    confirm.onclick = async () => {
      confirm.disabled = cancel.disabled = true;
      try {
        const resp = await fetch("/chat/proposals/" + p.proposal_id + "/confirm", { method: "POST" });
        if (resp.redirected) return finish("Session expired — log in again.");
        const out = await resp.json();
        if (!resp.ok) return finish(out.error || "Couldn't import.");
        finish("Done — " + out.created + " added, " + out.updated + " updated.");
        const ctx = _pageContext();
        if (ctx.page === "management" || ctx.page === "portfolio") setTimeout(() => window.location.reload(), 900);
      } catch (e) {
        finish("Connection error — try again.");
      }
    };
    cancel.onclick = async () => {
      confirm.disabled = cancel.disabled = true;
      try { await fetch("/chat/proposals/" + p.proposal_id + "/cancel", { method: "POST" }); } catch (e) { /* nothing was saved either way */ }
      finish("Cancelled — nothing was saved.");
    };
    actions.append(confirm, cancel);
    card.appendChild(actions);
  }
  card.appendChild(status);
  log.appendChild(card);
  _scrollDown(true);
}

// ---------- sending ----------

function _autoGrow() {
  const t = $("chat-input");
  if (!t) return;
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 136) + "px";
}

async function sendChatMessage(event) {
  if (event) event.preventDefault();
  if (chat.streaming) { if (chat.abort) chat.abort.abort(); return; }

  const input = $("chat-input");
  const message = input.value.trim();
  const file = chat.pendingFile;
  if (!message && !file) return;
  input.value = "";
  _autoGrow();
  chat.pendingFile = null;
  _renderPendingFile();

  const ctx = _pageContext();
  const key = _ctxKey(ctx);
  if (chat.messages.length && chat.lastCtxKey && chat.lastCtxKey !== key) {
    chat.messages.push({ role: "divider", text: "Now viewing " + ctx.label });
  }
  chat.lastCtxKey = key;

  const shown = (file ? "📎 " + file.name + (message ? "\n" : "") : "") + (message || (file ? IMPORT_MESSAGE : ""));
  chat.messages.push({ role: "user", text: shown });
  await runTurn(message, file, ctx);
}

async function runTurn(message, file, ctx) {
  const log = $("chat-log");
  _renderLog();

  const assistant = { role: "assistant", text: "", tools: [] };
  const wrap = _el("div", "chat-msg chat-msg-assistant streaming");
  const body = _el("div", "chat-text md");
  body.appendChild(Object.assign(_el("span", "chat-typing"), {}));
  const typing = body.firstChild;
  typing.append(_el("i"), _el("i"), _el("i"));
  if (file) body.appendChild(_el("span", "muted", " Reading your file…"));
  wrap.appendChild(body);
  log.appendChild(wrap);
  _scrollDown(true);

  const form = new FormData();
  form.append("message", message);
  if (chat.history) form.append("history", chat.history);
  if (ctx.page) form.append("page", ctx.page);
  if (ctx.symbol) form.append("symbol", ctx.symbol);
  if (file) form.append("file", file);

  chat.abort = new AbortController();
  _setStreaming(true);
  let rendered = false;
  let raf = 0;
  const paint = () => {
    raf = 0;
    body.textContent = "";
    body.appendChild(renderMarkdown(assistant.text));
    _scrollDown(false);
  };
  const fail = (msg) => {
    wrap.classList.remove("streaming");
    wrap.textContent = "";
    const err = _el("div", "chat-error");
    err.appendChild(_el("span", null, msg));
    const retry = _el("button", null, "Try again");
    retry.type = "button";
    retry.onclick = () => runTurn(message, file, ctx);
    err.appendChild(retry);
    wrap.appendChild(err);
  };

  try {
    const resp = await fetch("/chat/stream", { method: "POST", body: form, signal: chat.abort.signal });
    if (resp.redirected || !resp.body) { fail("Your session expired — log in again."); return; }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let errored = false;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop();

      for (const frame of frames) {
        let eventType = null;
        let data = null;
        for (const line of frame.split("\n")) {
          if (line.startsWith("event: ")) eventType = line.slice(7);
          else if (line.startsWith("data: ")) data = line.slice(6);
        }
        if (data === null || data === "[DONE]") continue;
        if (eventType === "error") { fail(data); errored = true; continue; }

        let parsed;
        try { parsed = JSON.parse(data); } catch (e) { continue; }

        if (parsed.type === "text") {
          assistant.text += parsed.delta;
          rendered = true;
          if (!raf) raf = requestAnimationFrame(paint);
        } else if (parsed.type === "tools") {
          assistant.tools.push(...parsed.items);
          const old = wrap.querySelector(".chat-sources");
          if (old) old.remove();
          wrap.insertBefore(_sourcesRow(assistant.tools), body);
        } else if (parsed.type === "model") {
          assistant.model = parsed.label;
          assistant.fallback = !!parsed.fallback;
        } else if (parsed.type === "history") {
          chat.history = parsed.messages;
        } else if (parsed.type === "proposal") {
          _renderProposal(parsed.proposal);
        }
      }
    }
    if (errored) return;
  } catch (e) {
    if (e.name !== "AbortError") { fail("Connection error — check your connection and try again."); return; }
  } finally {
    if (raf) cancelAnimationFrame(raf);
    _setStreaming(false);
    chat.abort = null;
  }

  wrap.classList.remove("streaming");
  if (rendered) {
    paint();
    chat.messages.push(assistant);
    const actions = _el("div", "chat-actions");
    actions.appendChild(_copyButton(() => assistant.text));
    if (assistant.model) actions.appendChild(_viaLabel(assistant));
    wrap.appendChild(actions);
  } else if (!wrap.querySelector(".chat-error")) {
    body.textContent = "";
    body.appendChild(_el("p", "muted", "Stopped."));
  }
  _save();
}

// ---------- boot ----------

document.addEventListener("DOMContentLoaded", () => {
  if (!$("chat-panel")) return;
  _load();
  _renderContext();
  _renderLog();
  _applyOpen();
  _setStreaming(false);

  $("chat-close").onclick = () => toggleChat(false);
  $("chat-new").onclick = newChat;
  $("chat-expand").onclick = () => { chat.expanded = !chat.expanded; _applyOpen(); _save(); };

  const input = $("chat-input");
  input.addEventListener("input", _autoGrow);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("chat-form").requestSubmit(); }
  });

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "/") { e.preventDefault(); toggleChat(); }
    else if (e.key === "Escape" && chat.open && $("chat-panel").contains(document.activeElement)) toggleChat(false);
  });
});
