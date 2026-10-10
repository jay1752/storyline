// App shell behaviour: theme toggle, collapsible sidebar, mobile drawer, and the ⌘K
// command palette (stock search + page jumps). Everything degrades to plain links if this
// file fails to load — navigation never depends on it.
(function () {
  const root = document.documentElement;
  const shell = document.getElementById("shell");

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode — preference just won't persist */ } },
  };

  // ---------- Theme: system -> light -> dark ----------
  const THEMES = ["system", "light", "dark"];
  const THEME_ICON = { system: "i-monitor", light: "i-sun", dark: "i-moon" };

  function currentTheme() {
    const t = store.get("theme");
    return THEMES.includes(t) ? t : "system";
  }

  function applyTheme(t) {
    if (t === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", t);
    document.querySelectorAll("#theme-toggle").forEach((btn) => {
      btn.title = "Theme: " + t + " (click to change)";
      const use = btn.querySelector("use");
      if (use) use.setAttribute("href", "#" + THEME_ICON[t]);
    });
  }

  applyTheme(currentTheme());
  document.querySelectorAll("#theme-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
      store.set("theme", next);
      applyTheme(next);
    });
  });

  // ---------- Palette: storyline <-> paperclip ----------
  const PALETTES = [{ id: "storyline", label: "Tenetfolio" }, { id: "paperclip", label: "Paperclip" }];
  function currentPalette() { return store.get("palette") === "paperclip" ? "paperclip" : "storyline"; }
  function applyPalette(p) {
    if (p === "paperclip") root.setAttribute("data-palette", "paperclip");
    else root.removeAttribute("data-palette");
    const other = PALETTES.find((x) => x.id !== p);
    document.querySelectorAll("#palette-toggle").forEach((btn) => {
      btn.title = "Palette: " + PALETTES.find((x) => x.id === p).label + " — switch to " + other.label;
    });
  }
  applyPalette(currentPalette());
  document.querySelectorAll("#palette-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const next = currentPalette() === "paperclip" ? "storyline" : "paperclip";
      store.set("palette", next);
      applyPalette(next);
    });
  });

  if (!shell) return; // auth pages: theme + palette only

  // ---------- Sidebar ----------
  function setSidebar(state) {
    shell.dataset.sidebar = state;
    const btn = document.getElementById("sidebar-toggle");
    if (btn) btn.title = state === "collapsed" ? "Expand sidebar" : "Collapse sidebar";
  }
  setSidebar(store.get("sidebar") === "collapsed" ? "collapsed" : "expanded");
  const sbToggle = document.getElementById("sidebar-toggle");
  if (sbToggle) {
    sbToggle.addEventListener("click", () => {
      const next = shell.dataset.sidebar === "collapsed" ? "expanded" : "collapsed";
      store.set("sidebar", next);
      setSidebar(next);
    });
  }

  // ---------- Mobile drawer ----------
  const closeDrawer = () => delete shell.dataset.drawer;
  const openBtn = document.getElementById("drawer-open");
  if (openBtn) openBtn.addEventListener("click", () => { shell.dataset.drawer = "open"; });
  const scrim = document.getElementById("scrim");
  if (scrim) scrim.addEventListener("click", closeDrawer);
  document.querySelectorAll("#sidebar a").forEach((a) => a.addEventListener("click", closeDrawer));

  // ---------- Command palette ----------
  const palette = document.getElementById("palette");
  if (!palette) return;
  const input = document.getElementById("palette-input");
  const list = document.getElementById("palette-list");

  const PAGES = [
    { label: "Analytics", href: "/analytics", icon: "i-analytics" },
    { label: "Management — tracked stocks", href: "/management", icon: "i-management" },
    { label: "Track a stock", href: "/management/add", icon: "i-plus" },
    { label: "Portfolio", href: "/portfolio", icon: "i-portfolio" },
    { label: "Investment rules", href: "/rules", icon: "i-check" },
    { label: "AI settings", href: "/settings", icon: "i-settings" },
    { label: "Your profile", href: "/profile", icon: "i-smile" },
    { label: "Data ingestion", href: "/admin", icon: "i-admin", admin: true },
  ].filter((p) => !p.admin || palette.dataset.admin === "1");

  let reqId = 0;
  let timer = null;

  function pageItem(p) {
    const a = document.createElement("a");
    a.className = "p-item";
    a.href = p.href;
    a.innerHTML = '<svg class="icon"><use href="#' + p.icon + '"/></svg>';
    const span = document.createElement("span");
    span.textContent = p.label;
    a.appendChild(span);
    return a;
  }

  function label(text) {
    const d = document.createElement("div");
    d.className = "p-label";
    d.textContent = text;
    return d;
  }

  function items() { return Array.from(list.querySelectorAll(".p-item, .stock-result")); }

  function setActive(idx) {
    const all = items();
    if (!all.length) return;
    const i = (idx + all.length) % all.length;
    all.forEach((el, n) => el.classList.toggle("active", n === i));
    all[i].scrollIntoView({ block: "nearest" });
  }

  function activeIndex() { return items().findIndex((el) => el.classList.contains("active")); }

  async function render(q) {
    const id = ++reqId;
    const query = q.trim();
    const frag = document.createDocumentFragment();
    const matchingPages = PAGES.filter((p) => !query || p.label.toLowerCase().includes(query.toLowerCase()));

    let stocksHtml = "";
    if (query) {
      try {
        const resp = await fetch("/search-stocks?q=" + encodeURIComponent(query), { headers: { "HX-Request": "true" } });
        if (id !== reqId) return; // a newer keystroke superseded this one
        if (resp.ok && !resp.redirected) stocksHtml = await resp.text();
      } catch (e) { /* offline — pages still work */ }
    }
    if (id !== reqId) return;

    list.textContent = "";
    if (stocksHtml.trim()) {
      list.appendChild(label("Stocks"));
      const holder = document.createElement("div");
      holder.innerHTML = stocksHtml; // server-rendered, autoescaped partial
      while (holder.firstChild) frag.appendChild(holder.firstChild);
      list.appendChild(frag);
    }
    if (matchingPages.length) {
      list.appendChild(label(query ? "Pages" : "Jump to"));
      matchingPages.forEach((p) => list.appendChild(pageItem(p)));
    }
    setActive(0);
  }

  function open() {
    palette.classList.add("open");
    input.value = "";
    render("");
    setTimeout(() => input.focus(), 0);
  }
  function close() { palette.classList.remove("open"); }

  document.querySelectorAll("[data-open-palette]").forEach((b) => b.addEventListener("click", () => { closeDrawer(); open(); }));
  palette.addEventListener("mousedown", (e) => { if (e.target === palette) close(); });

  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => render(input.value), 160);
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setActive(activeIndex() + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive(activeIndex() - 1); }
    else if (e.key === "Enter") {
      e.preventDefault();
      const el = items()[Math.max(activeIndex(), 0)];
      if (el && el.href) window.location.href = el.href;
    }
  });

  list.addEventListener("mousemove", (e) => {
    const el = e.target.closest(".p-item, .stock-result");
    if (el) items().forEach((n) => n.classList.toggle("active", n === el));
  });

  function typingInField(t) {
    return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
  }

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      palette.classList.contains("open") ? close() : open();
    } else if (e.key === "Escape" && palette.classList.contains("open")) {
      close();
    } else if (e.key === "/" && !e.metaKey && !e.ctrlKey && !typingInField(e.target)) {
      e.preventDefault();
      open();
    }
  });
})();
