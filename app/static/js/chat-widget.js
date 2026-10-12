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
const IMPORT_MESSAGE_MANY = "Import my positions from these files.";
const MAX_ATTACH = 5;

const chat = {
  messages: [], // {role, text, ctx, tools}
  history: null, // opaque server history
  open: false,
  expanded: false,
  streaming: false,
  abort: null,
  pendingFiles: [],   // up to MAX_ATTACH files / photos for the next message
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
  if (path === "/analytics" || path === "/analytics/") return { page: "analytics", symbol: null, label: "Find a stock" };
  if (path.startsWith("/management")) return { page: "management", symbol: null, label: "Tracked stocks" };
  if (path.startsWith("/portfolio")) return { page: "portfolio", symbol: null, label: "Portfolio" };
  if (path.startsWith("/community")) return { page: "community", symbol: null, label: "Community" };
  if (path.startsWith("/rules")) return { page: "rules", symbol: null, label: "Investment rules" };
  return { page: null, symbol: null, label: "General" };
}

function _ctxKey(ctx) { return (ctx.page || "") + "|" + (ctx.symbol || ""); }

// Starter questions come from the server (built from the page's own data); this static set is the fallback until they arrive.
const _sugg = { key: null, data: null };

function _loadSuggestions() {
  const ctx = _pageContext();
  const key = _ctxKey(ctx);
  if (_sugg.key === key) return;
  _sugg.key = key;
  _sugg.data = null;
  const q = new URLSearchParams();
  if (ctx.page) q.set("page", ctx.page);
  if (ctx.symbol) q.set("symbol", ctx.symbol);
  fetch("/chat/suggestions?" + q.toString())
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      if (!d || !d.items || _sugg.key !== key) return;
      _sugg.data = d;
      if (!chat.messages.length) _renderLog();
    })
    .catch(() => { /* keep the static set */ });
}

function _suggestions() {
  if (_sugg.data && _sugg.key === _ctxKey(_pageContext())) return _sugg.data;
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
  get_stock_analytics: (a) => "Stock analytics" + (a ? " · " + a : ""),
  get_my_rules: () => "Your rules",
  get_management_overview: () => "Your tracked stocks",
  get_portfolio_summary: () => "Your portfolio",
  get_community_briefing: () => "Daily briefing",
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
  _loadSuggestions();
  const box = $("chat-context");
  if (!box) return;
  const ctx = _pageContext();
  box.textContent = "";
  box.append("Viewing ", Object.assign(_el("b"), { textContent: ctx.label }));
  const input = $("chat-input");
  if (input) input.placeholder = ctx.page === "analytics" && ctx.symbol ? "Ask about " + ctx.symbol + " or anything else…" : ctx.page ? "Ask about this page…" : "Ask anything about your stocks…";
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
  chat.pendingFiles = [];
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

async function onChatFilePicked(input) {
  const picked = Array.from(input.files || []);
  const autosend = !!input.dataset.autosend;
  input.value = "";
  if (!picked.length) return;
  await _addPending(picked);
  if (autosend) $("chat-form").requestSubmit();
}

// Photos and screenshots: from the camera or the photo library (iPhone photos are HEIC; they are turned into a normal JPEG here). Several at once.
function pickChatPhoto() { $("chat-photo").click(); }

async function onChatPhotoPicked(input) {
  const picked = Array.from(input.files || []);
  input.value = "";
  if (!picked.length) return;
  await _addPending(picked);
  $("chat-input").focus();
}

// Adds files to the message being written (images are shrunk first); a sixth file is not taken, and the box says so.
async function _addPending(files) {
  let skipped = 0;
  for (const f of files) {
    if (chat.pendingFiles.length >= MAX_ATTACH) { skipped++; continue; }
    chat.pendingFiles.push(_isImage(f) ? await _prepareImage(f) : f);
  }
  chat.attachNote = skipped ? "Only " + MAX_ATTACH + " attachments fit in one message, so " + skipped + " " + (skipped === 1 ? "was" : "were") + " left out." : "";
  _renderPendingFile();
}

function _isImage(file) {
  return /^image\//.test(file.type) || /\.(png|jpe?g|webp|heic|heif)$/i.test(file.name || "");
}

// Re-draws an image as a JPEG no wider than 2000 px: HEIC becomes readable by the model, and a 10 MB phone photo becomes a few hundred KB.
// If the browser cannot decode it (HEIC outside Safari), the original goes up and the server says what it can read.
async function _prepareImage(file) {
  try {
    const bmp = await createImageBitmap(file);
    const scale = Math.min(1, 2000 / Math.max(bmp.width, bmp.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bmp.width * scale);
    canvas.height = Math.round(bmp.height * scale);
    canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((res) => canvas.toBlob(res, "image/jpeg", 0.85));
    if (!blob) return file;
    const base = (file.name || "photo").replace(/\.[^.]+$/, "") || "photo";
    return new File([blob], base + ".jpg", { type: "image/jpeg" });
  } catch (e) {
    return file;
  }
}

// Paste a screenshot straight into the message box.
document.addEventListener("paste", async (e) => {
  const input = $("chat-input");
  if (!input || e.target !== input) return;
  const item = Array.from((e.clipboardData && e.clipboardData.items) || []).find((i) => i.kind === "file" && /^image\//.test(i.type));
  if (!item) return;
  e.preventDefault();
  await _addPending([item.getAsFile()]);
});

function clearChatFile() {
  chat.pendingFiles = [];
  chat.attachNote = "";
  _renderPendingFile();
}

function removeChatFile(i) {
  chat.pendingFiles.splice(i, 1);
  chat.attachNote = "";
  _renderPendingFile();
}

function _renderPendingFile() {
  const box = $("chat-attachment");
  if (!box) return;
  box.textContent = "";
  const files = chat.pendingFiles || [];
  box.classList.toggle("has", files.length > 0);
  if (!files.length) return;
  files.forEach((f, i) => {
    const x = _el("button");
    x.type = "button";
    x.setAttribute("aria-label", "Remove " + f.name);
    x.appendChild(_icon("i-x", "icon-sm"));
    x.onclick = () => removeChatFile(i);
    const row = _el("div", "chat-attachment-row");
    row.append(_icon(_isImage(f) ? "i-camera" : "i-paperclip", "icon-sm"), _el("span", "name", f.name), x);
    box.appendChild(row);
  });
  if (chat.attachNote) box.appendChild(_el("div", "chat-attachment-note", chat.attachNote));
  const note = _el("div", "chat-attachment-note");
  note.append("We read these to find your holdings and don't keep them. Excel, CSV and text PDFs are read on our server. Scans, photos and unfamiliar layouts are sent to the AI provider you chose in AI settings. ");
  const link = _el("a", null, "Privacy policy");
  link.href = "/privacy";
  link.target = "_blank";
  link.rel = "noopener";
  note.append(link, ". Sending means you agree.");
  box.appendChild(note);
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

// Stock picker used on the review card: type 2+ letters, pick from the list. onPick({symbol, name}).
function _stockPicker(onPick, placeholder) {
  const box = _el("div", "stock-pick");
  const input = _el("input", "card-input wide");
  input.type = "search";
  input.placeholder = placeholder || "Search a stock…";
  input.autocomplete = "off";
  const list = _el("div", "stock-pick-list");
  let timer = 0;
  input.oninput = () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { list.textContent = ""; return; }
    timer = setTimeout(async () => {
      try {
        const resp = await fetch("/chat/stock-search?q=" + encodeURIComponent(q));
        const rows = resp.ok ? await resp.json() : [];
        list.textContent = "";
        for (const r of rows) {
          const b = _el("button", null, r.symbol.split(":").pop() + " · " + r.name);
          b.type = "button";
          b.onclick = () => { list.textContent = ""; input.value = ""; onPick(r); };
          list.appendChild(b);
        }
        if (!rows.length) list.appendChild(_el("span", "chat-card-sub", "No match"));
      } catch (e) { /* offline: leave the list empty */ }
    }, 220);
  };
  box.append(input, list);
  return box;
}

function _field(label, input) {
  const w = _el("label", "card-field");
  w.append(_el("span", null, label), input);
  return w;
}

function _numInput(value, step) {
  const i = _el("input", "card-input");
  i.type = "number";
  i.step = step || "any";
  i.min = "0";
  if (value !== null && value !== undefined) i.value = value;
  return i;
}

function _dateInput(value) {
  const i = _el("input", "card-input");
  i.type = "date";
  i.max = new Date().toISOString().slice(0, 10);
  if (value) i.value = String(value).slice(0, 10);
  return i;
}

function _renderProposal(p) {
  const log = $("chat-log");
  if (!log) return;
  const card = _el("div", "chat-card");
  card.appendChild(_el("div", "chat-card-title", p.rows.length ? "Review import — " + p.rows.length + " position" + (p.rows.length === 1 ? "" : "s") : "Nothing matched yet"));
  card.appendChild(_el("div", "chat-card-sub", "Change any number, switch the stock, untick a line to leave it out, or add one we couldn't place. Read from your file, which isn't stored."));

  const editors = []; // one per proposed row
  const added = [];   // manually filled rows from "couldn't place"

  for (const r of p.rows) {
    const row = _el("div", "chat-card-row");
    if (r.needs_review) row.classList.add("needs-review");
    const ed = { orig: r.symbol, sym: r.symbol, include: true, mode: "replace" };
    const head = _el("div", "chat-card-head");
    const inc = _el("input");
    inc.type = "checkbox";
    inc.checked = true;
    inc.setAttribute("aria-label", "Include " + r.symbol);
    inc.onchange = () => { ed.include = inc.checked; row.classList.toggle("off", !inc.checked); };
    const symEl = _el("strong", null, r.symbol);
    const nameEl = _el("span", "chat-card-name", r.company_name);
    const change = _el("button", "linklike", "Not this stock?");
    change.type = "button";
    head.append(inc, symEl, nameEl, change);
    row.appendChild(head);

    const picker = _stockPicker((hit) => { ed.sym = hit.symbol; symEl.textContent = hit.symbol; nameEl.textContent = hit.name; picker.hidden = true; }, "Search the right stock…");
    picker.hidden = true;
    change.onclick = () => { picker.hidden = !picker.hidden; if (!picker.hidden) picker.querySelector("input").focus(); };
    row.appendChild(picker);

    const qty = _numInput(r.quantity), avg = _numInput(r.avg_price), when = _dateInput(r.anchor_date);
    ed.qty = qty; ed.avg = avg; ed.when = when;
    const fields = _el("div", "card-fields");
    fields.append(_field("Shares", qty), _field("Avg price", avg), _field("Since", when));
    row.appendChild(fields);

    const sub = [_actionText(r), r.txn_count + " row" + (r.txn_count === 1 ? "" : "s")];
    if (r.last_price !== null && r.last_price !== undefined) sub.push("now " + _fmt(r.last_price, 2));
    row.appendChild(_el("div", "chat-card-sub", sub.join(" · ")));
    if (r.action === "update") {
      const mode = _el("select", "card-input");
      mode.append(Object.assign(_el("option", null, "Replace what I have (" + _fmt((r.existing || {}).quantity, 4) + " sh)"), { value: "replace" }),
                  Object.assign(_el("option", null, "Add to what I have (another account)"), { value: "combine" }));
      mode.onchange = () => { ed.mode = mode.value; };
      row.appendChild(mode);
    }
    if (r.match && r.match.note) row.appendChild(_el("div", "chat-card-sub", r.match.note));
    if (r.price_check && r.price_check.status === "verified") row.appendChild(_el("div", "chat-card-ok", "✓ " + r.price_check.text));
    for (const w of r.warnings || []) row.appendChild(_el("div", "chat-card-warn", "⚠ " + w));
    card.appendChild(row);
    editors.push(ed);
  }

  const skippedItem = (s) => {
    const item = _el("div", "chat-card-row");
    item.appendChild(_el("div", "chat-card-sub", s.label + " — " + s.reason));
    const d = s.draft || {};
    const form = _el("div", "card-form");
    form.hidden = true;
    const entry = { sym: s.symbol || null, qty: _numInput(d.quantity), avg: _numInput(d.avg_price), when: _dateInput(d.anchor_date) };
    const chosen = _el("div", "chat-card-head");
    const chosenSym = _el("strong", null, s.symbol || "Pick the stock below");
    chosen.appendChild(chosenSym);
    const pick = _stockPicker((hit) => { entry.sym = hit.symbol; chosenSym.textContent = hit.symbol + " · " + hit.name; }, "Search the stock…");
    if (s.symbol) pick.hidden = true;
    const fields = _el("div", "card-fields");
    fields.append(_field("Shares", entry.qty), _field("Avg price", entry.avg), _field("Since", entry.when));
    form.append(chosen, pick, fields);
    const open = _el("button", "linklike", "Add by hand");
    open.type = "button";
    open.onclick = () => { form.hidden = !form.hidden; if (!added.includes(entry)) added.push(entry); };
    item.append(open, form);
    return item;
  };
  const kinds = { pick: [], history: [], sold: [], other: [] };
  for (const s of p.skipped || []) (kinds[s.kind] || kinds.other).push(s);
  const section = (title, items, collapsed, render) => {
    if (!items.length) return;
    const box = collapsed ? _el("details", "chat-card-skipped") : _el("div", "chat-card-skipped");
    const head = _el(collapsed ? "summary" : "div", "chat-card-title", title);
    box.appendChild(head);
    for (const s of items) box.appendChild(render(s));
    card.appendChild(box);
  };
  section("Pick the right stock (" + (kinds.pick.length + kinds.other.length) + ") — we couldn't match these names", kinds.pick.concat(kinds.other), false, skippedItem);
  section(kinds.history.length + " sold more than this file shows — you held them before it starts", kinds.history, true, skippedItem);
  section(kinds.sold.length + " bought and sold completely — nothing to add", kinds.sold, true, (s) => _el("div", "chat-card-sub", s.label));

  const status = _el("div", "chat-card-status");
  const actions = _el("div", "chat-card-actions");
  const confirm = _el("button", null, "Confirm import");
  confirm.type = "button";
  const cancel = _el("button", "secondary", "Cancel");
  cancel.type = "button";
  const finish = (msg) => { actions.remove(); status.textContent = msg; };

  confirm.onclick = async () => {
    const body = { rows: [], added: [] };
    for (const ed of editors) {
      const e = { symbol: ed.orig, include: ed.include, mode: ed.mode, quantity: ed.qty.value, avg_price: ed.avg.value, anchor_date: ed.when.value };
      if (ed.sym !== ed.orig) e.new_symbol = ed.sym;
      body.rows.push(e);
    }
    for (const en of added) {
      if (en.sym && en.qty.value) body.added.push({ symbol: en.sym, quantity: en.qty.value, avg_price: en.avg.value, anchor_date: en.when.value });
    }
    if (!body.rows.some((e) => e.include) && !body.added.length) { status.textContent = "Nothing selected to import."; return; }
    confirm.disabled = cancel.disabled = true;
    status.textContent = "Saving…";
    try {
      const resp = await fetch("/chat/proposals/" + p.proposal_id + "/confirm", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (resp.redirected) return finish("Session expired — log in again.");
      const out = await resp.json();
      if (!resp.ok) {
        status.textContent = out.error || "Couldn't import.";  // a fixable mistake: keep the card open
        if (resp.status === 422) { confirm.disabled = cancel.disabled = false; return; }
        return finish(out.error || "Couldn't import.");
      }
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
  card.append(actions, status);
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
  const files = chat.pendingFiles.slice();
  if (!message && !files.length) return;
  input.value = "";
  _autoGrow();
  chat.pendingFiles = [];
  chat.attachNote = "";
  _renderPendingFile();

  const ctx = _pageContext();
  const key = _ctxKey(ctx);
  if (chat.messages.length && chat.lastCtxKey && chat.lastCtxKey !== key) {
    chat.messages.push({ role: "divider", text: "Now viewing " + ctx.label });
  }
  chat.lastCtxKey = key;

  const shown = (files.length ? "📎 " + files.map((f) => f.name).join(", ") + (message ? "\n" : "") : "") + (message || (files.length ? (files.length > 1 ? IMPORT_MESSAGE_MANY : IMPORT_MESSAGE) : ""));
  chat.messages.push({ role: "user", text: shown });
  await runTurn(message, files, ctx);
}

async function runTurn(message, files, ctx) {
  const log = $("chat-log");
  _renderLog();

  const assistant = { role: "assistant", text: "", tools: [] };
  const wrap = _el("div", "chat-msg chat-msg-assistant streaming");
  const body = _el("div", "chat-text md");
  body.appendChild(Object.assign(_el("span", "chat-typing"), {}));
  const typing = body.firstChild;
  typing.append(_el("i"), _el("i"), _el("i"));
  if (files.length) body.appendChild(_el("span", "muted", files.length > 1 ? " Reading your files…" : " Reading your file…"));
  wrap.appendChild(body);
  log.appendChild(wrap);
  _scrollDown(true);

  const form = new FormData();
  form.append("message", message);
  if (chat.history) form.append("history", chat.history);
  if (ctx.page) form.append("page", ctx.page);
  if (ctx.symbol) form.append("symbol", ctx.symbol);
  files.forEach((f) => form.append("files", f));

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
    retry.onclick = () => runTurn(message, files, ctx);
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

// ---------- dictation (the browser's own speech recognition; the words land in the message box, you check them and send) ----------

function _initDictation() {
  const btn = $("chat-mic");
  const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!btn || !Rec) return;                       // Firefox and a few others have none: the button stays hidden
  btn.hidden = false;
  const note = $("chat-mic-note");
  let rec = null;
  let listening = false;
  let hideNote = 0;

  const say = (text) => {
    if (!note) return;
    clearTimeout(hideNote);
    note.textContent = text;
    note.hidden = !text;
    if (text) hideNote = setTimeout(() => { note.hidden = true; }, 6000);
  };
  const setListening = (on) => {
    listening = on;
    btn.classList.toggle("listening", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.setAttribute("aria-label", on ? "Stop dictation" : "Start dictation");
    btn.title = on ? "Tap to stop" : "Speak your message";
  };

  btn.addEventListener("click", () => {
    if (listening) { try { rec.stop(); } catch (e) { /* already ended */ } return; }
    const input = $("chat-input");
    const base = input.value ? input.value.replace(/\s+$/, "") + " " : "";
    rec = new Rec();
    rec.lang = navigator.language || "en-IN";
    rec.interimResults = true;
    rec.continuous = false;
    rec.onstart = () => { setListening(true); say("Listening… tap the mic again to stop."); };
    rec.onresult = (e) => {
      let text = "";
      for (let i = 0; i < e.results.length; i++) text += e.results[i][0].transcript;
      input.value = base + text;
      _autoGrow();
    };
    rec.onerror = (e) => {
      if (e.error === "not-allowed" || e.error === "service-not-allowed") say("Microphone access is off. Allow it in your browser or phone settings, then try again.");
      else if (e.error === "no-speech") say("I didn't hear anything. Try again, a little closer to the microphone.");
      else if (e.error !== "aborted") say("Dictation stopped (" + e.error + "). You can type instead.");
    };
    rec.onend = () => { setListening(false); if (note && note.textContent.startsWith("Listening")) say(""); input.focus(); };
    try { rec.start(); } catch (e) { setListening(false); }
  });
}

// ---------- boot ----------

document.addEventListener("DOMContentLoaded", () => {
  if (!$("chat-panel")) return;
  _load();
  _renderContext();
  _renderLog();
  _applyOpen();
  _setStreaming(false);
  _initDictation();

  $("chat-close").onclick = () => toggleChat(false);
  $("chat-new").onclick = newChat;
  $("chat-expand").onclick = () => { chat.expanded = !chat.expanded; _applyOpen(); _save(); };

  const input = $("chat-input");
  input.addEventListener("input", _autoGrow);
  input.addEventListener("keydown", (e) => {
    // Enter sends on a computer (Shift+Enter makes a new line). On a phone or tablet Enter is a new line, since there is no Shift key
    // on the touch keyboard and the arrow button sends. Ctrl/Cmd+Enter sends everywhere.
    const touch = window.matchMedia && matchMedia("(pointer: coarse)").matches;
    if (e.key === "Enter" && !e.isComposing && (e.ctrlKey || e.metaKey || (!e.shiftKey && !touch))) { e.preventDefault(); $("chat-form").requestSubmit(); }
  });

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "/") { e.preventDefault(); toggleChat(); }
    else if (e.key === "Escape" && chat.open && $("chat-panel").contains(document.activeElement)) toggleChat(false);
  });
});
