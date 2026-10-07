// AI settings: provider + model dropdowns, per-provider key status, live model lists, connection test.
(function () {
  const REG = JSON.parse(document.getElementById("ai-registry").textContent);
  const STATE = JSON.parse(document.getElementById("ai-state").textContent);
  const saved = new Set(STATE.saved);
  const $ = (id) => document.getElementById(id);
  const CUSTOM = "__custom__";

  function providerOptions(sel, withNone) {
    sel.textContent = "";
    if (withNone) sel.add(new Option("None — no fallback", ""));
    Object.entries(REG).forEach(([id, p]) => {
      const tag = p.free ? "  ·  free tier" : "";
      sel.add(new Option(p.label + tag, id));
    });
  }

  // One "slot" = provider select + model select + custom input (+ key bits). Main and fallback share the logic.
  function slot(cfg) {
    const s = { provider: $(cfg.provider), model: $(cfg.model), custom: $(cfg.custom), models: null };

    s.fillModels = (list, selected) => {
      s.models = list;
      s.model.textContent = "";
      list.forEach((m) => s.model.add(new Option(m.label === m.id ? m.id : m.label + "  (" + m.id + ")", m.id)));
      s.model.add(new Option("Other — type a model ID…", CUSTOM));
      if (selected && list.some((m) => m.id === selected)) {
        s.model.value = selected; s.custom.value = "";
      } else if (selected) {
        s.model.value = CUSTOM; s.custom.value = selected;
      }
      s.syncCustom();
    };
    s.syncCustom = () => {
      const isCustom = s.model.value === CUSTOM;
      s.custom.hidden = !isCustom;
      s.custom.disabled = !isCustom; // a disabled field isn't posted, so a stale custom value can't override the dropdown
    };
    s.value = () => (s.model.value === CUSTOM ? s.custom.value.trim() : s.model.value);
    s.model.addEventListener("change", () => { s.syncCustom(); if (!s.custom.hidden) s.custom.focus(); });
    return s;
  }

  const main = slot({ provider: "provider", model: "model_name", custom: "custom_model" });
  const fb = slot({ provider: "fallback_provider", model: "fallback_model", custom: "fallback_custom" });

  providerOptions(main.provider, false);
  providerOptions(fb.provider, true);

  function keyState(el, linkEl, providerId) {
    const p = REG[providerId];
    if (!p) { el.textContent = ""; return; }
    el.textContent = !p.needs_key ? "· no key needed" : saved.has(providerId) ? "· saved ✓" : "· not saved";
    el.className = "key-state" + (saved.has(providerId) ? " ok" : "");
    linkEl.href = p.key_url;
    linkEl.textContent = p.needs_key ? "Get a " + p.label.split(" (")[0] + " key" : "Install " + p.label.split(" (")[0];
  }

  function infoText(providerId) {
    const p = REG[providerId];
    if (!p) return "";
    const bits = [];
    if (p.free) bits.push(p.free);
    bits.push(p.files ? "Can read PDFs and images (statement import)." : "Text only — can't read PDF/image statements; use CSV, or a fallback that can.");
    return bits.join(" ");
  }

  function applyMain(providerId, model) {
    main.provider.value = providerId;
    main.fillModels(REG[providerId].models, model || REG[providerId].default_model);
    $("provider-info").textContent = infoText(providerId);
    keyState($("key-state"), $("key-link"), providerId);
    $("api_key").value = "";
    const rm = $("remove-wrap");
    rm.hidden = !saved.has(providerId);
    $("remove-key").checked = false;
    const pw = $("platform-wrap");
    pw.hidden = !REG[providerId].platform_available || !REG[providerId].needs_key;
    $("use_platform_default").checked = !!STATE.settings.use_platform_default && providerId === STATE.settings.provider;
    $("models-msg").textContent = "";
    $("test-main-result").textContent = "";
  }

  function applyFallback(providerId, model) {
    fb.provider.value = providerId || "";
    const on = !!providerId;
    $("fb-model-wrap").hidden = !on; $("fb-key-wrap").hidden = !on; $("fb-test-wrap").hidden = !on;
    if (on) {
      fb.fillModels(REG[providerId].models, model || REG[providerId].default_model);
      keyState($("fb-key-state"), $("fb-key-link"), providerId);
      $("fallback-info").textContent = infoText(providerId);
    } else {
      $("fallback-info").textContent = "";
    }
    $("fallback_api_key").value = "";
    $("test-fallback-result").textContent = "";
  }

  main.provider.addEventListener("change", () => applyMain(main.provider.value));
  fb.provider.addEventListener("change", () => applyFallback(fb.provider.value));

  // remove-saved-key checkbox posts remove_key=<provider> (a dynamic hidden input keeps the form plain)
  $("ai-form").addEventListener("submit", () => {
    document.querySelectorAll("input[name=remove_key]").forEach((n) => n.remove());
    if (!$("remove-key").checked) return;
    const h = document.createElement("input");
    h.type = "hidden"; h.name = "remove_key"; h.value = main.provider.value;
    $("ai-form").appendChild(h);
  });

  async function post(url, data) {
    const body = new URLSearchParams(data);
    const resp = await fetch(url, { method: "POST", body });
    if (resp.redirected) throw new Error("Your session expired — log in again.");
    return resp.json();
  }

  async function runTest(slotObj, keyInput, resultEl, btn) {
    const model = slotObj.value();
    if (!model) { resultEl.textContent = "Choose a model first."; resultEl.className = "test-result bad"; return; }
    btn.disabled = true; resultEl.textContent = "Testing…"; resultEl.className = "test-result";
    try {
      const out = await post("/settings/test", { provider: slotObj.provider.value, model, api_key: keyInput.value });
      resultEl.textContent = out.message; resultEl.className = "test-result " + (out.ok ? "good" : "bad");
    } catch (e) { resultEl.textContent = e.message; resultEl.className = "test-result bad"; }
    btn.disabled = false;
  }
  $("test-main").addEventListener("click", () => runTest(main, $("api_key"), $("test-main-result"), $("test-main")));
  $("test-fallback").addEventListener("click", () => runTest(fb, $("fallback_api_key"), $("test-fallback-result"), $("test-fallback")));

  $("refresh-models").addEventListener("click", async () => {
    const msg = $("models-msg");
    msg.textContent = "Loading…";
    try {
      const out = await post("/settings/models", { provider: main.provider.value, api_key: $("api_key").value });
      const keep = main.value();
      main.fillModels(out.models, keep || REG[main.provider.value].default_model);
      msg.textContent = out.message || "";
    } catch (e) { msg.textContent = e.message; }
  });

  // One-click free setup: Groq (text, ~1,000 req/day) with Gemini as the fallback that can also read PDFs.
  $("preset-free").addEventListener("click", () => {
    applyMain("groq", "openai/gpt-oss-120b");
    applyFallback("gemini", "gemini-3-flash-preview");
    $("api_key").focus();
  });

  applyMain(STATE.settings.provider, STATE.settings.model);
  applyFallback(STATE.settings.fallback_provider, STATE.settings.fallback_model);
})();
