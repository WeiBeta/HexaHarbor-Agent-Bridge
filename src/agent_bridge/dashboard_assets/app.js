(() => {
  "use strict";

  const CSRF_TOKEN = document.querySelector('meta[name="ab-csrf"]')?.getAttribute("content") || "";
  const INITIAL_LOCALE = document.querySelector('meta[name="ab-locale"]')?.getAttribute("content") || "zh-CN";
  const I18N = window.AbI18n;
  const COL_STORAGE_KEY = "hh.billing.records.cols.v1";
  const THEME_STORAGE_KEY = "hh.billing.theme.v1";
  const REFRESH_STORAGE_KEY = "hh.billing.refresh.v1";
  const DEFAULT_COLS = { effort: true, tokens: true, rate: true, cost: true, duration: true };

  // Official brand casings for all bundled Agent IDE / Harness entries.
  const AGENT_DISPLAY_NAMES = {
    antigravity: "Antigravity",
    claude: "Claude Code",
    codex: "Codex",
    cursor: "Cursor",
    devin: "Devin",
    dsh: "DeepSeek Harness",
    grok: "Grok Build",
    kimi: "Kimi Code",
    minimax: "MiniMax Code",
    opencode: "OpenCode",
    zcode: "ZCode",
  };

  function t(key, params) {
    return I18N ? I18N.t(key, params) : key;
  }

  function trBackend(msg) {
    return I18N ? I18N.translateBackendText(msg) : String(msg ?? "");
  }

  function trDiscountLabel(label, ruleId) {
    return I18N ? I18N.translateDiscountLabel(label, ruleId) : String(label ?? "");
  }

  function agentLabel(id) {
    if (!id) return "";
    if (id === "*") return t("common.allAgentsWildcard");
    const key = String(id).trim().toLowerCase();
    return AGENT_DISPLAY_NAMES[key] || String(id);
  }

  const state = {
    activeTab: "stats",
    meta: null,
    summary: null,
    pricing: null,
    storage: null,
    service: null,
    serviceStopped: false,
    records: null,
    lastRevision: null,
    autoRefreshMs: loadRefreshMs(),
    autoRefreshTimer: null,
    pollInFlight: false,
    localeSwitching: false,
    selectedAgents: new Set(),
    selectedModels: new Set(), // keys are "agent::model"
    statsModelQuery: "",
    modelsTableQuery: "",
    statsRange: "all",
    statsStart: "",
    statsEnd: "",
    statsCwd: "",
    chartMetric: "tokens", // tokens | cost | tasks
    recAgent: "",
    recStatus: "all",
    recRange: "all",
    recSpeed: "all",
    recCwd: "",
    recSession: "",
    recQuery: "",
    recDay: "",
    recOffset: 0,
    recLimit: 50,
    recCols: loadCols(),
    pricingView: "observed", // observed | unpriced | user | presets
    pricingSearch: "",
    drawerTaskId: null,
    drawerRecord: null,
    modalCtx: null,
    infoModalSpec: null,
    agySlots: null,
    agySlotsError: null,
    agySlotsLoading: false,
    agySelectedSlotName: null,
    agyConfirmText: "",
    agyValidPreview: null,
    agyInvalidated: false,
    agyMissingNotice: null,
    agyBusyMode: null, // null | "preview" | "live"
    agyLastAction: null,
    agyModalOpen: false,
    agyModalTarget: null,
  };

  function $(id) {
    return document.getElementById(id);
  }

  function esc(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function tHtml(key, htmlParams) {
    const raw = t(key);
    let out = "";
    let lastIndex = 0;
    const re = /\{([a-zA-Z0-9_]+)\}/g;
    let m;
    while ((m = re.exec(raw)) !== null) {
      out += esc(raw.slice(lastIndex, m.index));
      const name = m[1];
      if (htmlParams && Object.prototype.hasOwnProperty.call(htmlParams, name)) {
        out += htmlParams[name];
      } else {
        out += esc(m[0]);
      }
      lastIndex = re.lastIndex;
    }
    out += esc(raw.slice(lastIndex));
    return out;
  }

  function loadRefreshMs() {
    try {
      const raw = localStorage.getItem(REFRESH_STORAGE_KEY);
      if (raw === null) return 10000;
      const n = Number(raw);
      return [0, 5000, 10000, 30000].includes(n) ? n : 10000;
    } catch {
      return 10000;
    }
  }

  function saveRefreshMs(ms) {
    try {
      localStorage.setItem(REFRESH_STORAGE_KEY, String(ms));
    } catch {
      // ignore
    }
  }

  function loadCols() {
    try {
      const raw = localStorage.getItem(COL_STORAGE_KEY);
      if (!raw) return { ...DEFAULT_COLS };
      const parsed = JSON.parse(raw);
      return {
        effort: parsed.effort !== false,
        tokens: parsed.tokens !== false,
        rate: parsed.rate !== false,
        cost: parsed.cost !== false,
        duration: parsed.duration !== false,
      };
    } catch {
      return { ...DEFAULT_COLS };
    }
  }

  function saveCols() {
    try {
      localStorage.setItem(COL_STORAGE_KEY, JSON.stringify(state.recCols));
    } catch {
      // ignore storage errors
    }
  }

  function initTheme() {
    try {
      const saved = localStorage.getItem(THEME_STORAGE_KEY);
      if (saved === "light" || saved === "dark") {
        document.documentElement.setAttribute("data-theme", saved);
      }
    } catch {
      // ignore
    }
  }

  function toggleTheme() {
    const curr = document.documentElement.getAttribute("data-theme") || "dark";
    const next = curr === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // ignore
    }
  }

  let toastTimer = null;
  function showToast(msg, isError = false) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.toggle("error", Boolean(isError));
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 3200);
  }

  async function apiGet(url) {
    const res = await fetch(url, { cache: "no-store" });
    const data = await res.json().catch(() => ({ ok: false, error: `HTTP ${res.status}` }));
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    return data;
  }

  async function apiMutate(method, url, payload = {}) {
    const res = await fetch(url, {
      method,
      headers: {
        "Content-Type": "application/json",
        "X-Bridge-CSRF": CSRF_TOKEN,
      },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({ ok: false, error: `HTTP ${res.status}` }));
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    return data;
  }

  // ==================== Formatters ====================
  function fmtInt(n) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
    return Number(n).toLocaleString(I18N ? I18N.intlLocale() : "en-US");
  }

  function fmtCompact(n) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
    const num = Number(n);
    if (Math.abs(num) >= 1e9) return `${(num / 1e9).toFixed(2)}B`;
    if (Math.abs(num) >= 1e6) return `${(num / 1e6).toFixed(2)}M`;
    if (Math.abs(num) >= 1e3) return `${(num / 1e3).toFixed(1)}K`;
    return String(Math.round(num));
  }

  function fmtPct(ratio) {
    if (ratio === null || ratio === undefined || Number.isNaN(Number(ratio))) return "—";
    return `${(Number(ratio) * 100).toFixed(1)}%`;
  }

  function fmtPct4(ratio) {
    if (ratio === null || ratio === undefined || Number.isNaN(Number(ratio))) return "—";
    const pct = Number(ratio) * 100;
    if (!Number.isFinite(pct)) return "—";
    if (pct <= 0) return "0%";
    if (pct >= 100) return "100%";
    return `${pct.toPrecision(4)}%`;
  }

  function fmtUsd(amount) {
    if (amount === null || amount === undefined || Number.isNaN(Number(amount))) return "—";
    const v = Number(amount);
    if (v === 0) return "$0.00";
    if (v < 0.01) return `$${v.toFixed(4)}`;
    if (v < 1) return `$${v.toFixed(3)}`;
    return `$${v.toFixed(2)}`;
  }

  function fmtRate(rate) {
    if (rate === null || rate === undefined || Number.isNaN(Number(rate))) return "—";
    const v = Number(rate);
    return `$${v.toFixed(v < 0.1 && v > 0 ? 4 : 2)}`;
  }

  function fmtBytes(bytes) {
    if (bytes === null || bytes === undefined) return "—";
    const n = Number(bytes);
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(2)} MB`;
    return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  }

  function fmtDuration(ms) {
    if (ms === null || ms === undefined) return "—";
    const s = Number(ms) / 1000;
    if (s < 1) return `${Math.round(ms)}ms`;
    if (s < 60) return `${s.toFixed(1)}s`;
    const m = Math.floor(s / 60);
    const rem = Math.round(s % 60);
    return `${m}m ${rem}s`;
  }

  function fmtTime(isoStr) {
    if (!isoStr) return "—";
    const d = new Date(isoStr);
    if (Number.isNaN(d.getTime())) return isoStr;
    if (I18N && I18N.getLocale() === "en") {
      return d.toLocaleString("en-US", {
        month: "short",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      });
    }
    return d.toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  }

  function shortPath(p) {
    if (!p) return "—";
    const parts = p.replaceAll("\\", "/").split("/").filter(Boolean);
    return parts.length <= 2 ? p : `…/${parts.slice(-2).join("/")}`;
  }

  function priceSourceBadge(src) {
    const clsMap = {
      user: "badge-accent",
      user_derived: "badge-accent",
      preset: "badge-ok",
      preset_derived: "badge-ok",
      unpriced: "badge-warn",
    };
    const key = Object.prototype.hasOwnProperty.call(clsMap, src) ? src : "unpriced";
    return `<span class="badge ${clsMap[key]}">${esc(t(`priceSource.${key}`))}</span>`;
  }

  function statusBadge(status) {
    if (status === "completed") return `<span class="badge badge-ok">completed</span>`;
    if (status === "failed") return `<span class="badge badge-danger">failed</span>`;
    if (status === "cancelled") return `<span class="badge badge-warn">cancelled</span>`;
    return `<span class="badge">${esc(status || "unknown")}</span>`;
  }

  // ==================== Language Switching ====================
  async function applyLocaleAndRerender(nextLocale) {
    if (!I18N) return;
    await I18N.setLocale(nextLocale);
    renderMetaControls();
    renderSummary();
    renderRecords();
    renderPricing();
    updateDiscountPreview();
    renderService({ preserveInputs: true });
    renderStorage({ preserveInputs: true });
    renderAgySlots({ preserveInputs: true });
    if (state.agyModalOpen) {
      renderAgyCleanupModal();
    }
    if (state.drawerRecord && !$("detail-drawer").classList.contains("hidden")) {
      renderDrawer(state.drawerRecord);
    }
    if (state.modalCtx && state.modalCtx.agent && !$("modal-backdrop").classList.contains("hidden")) {
      $("modal-scope-label").textContent = t("pricing.modalScopeAgent", {
        agent: agentLabel(state.modalCtx.agent),
        scopedKey: state.modalCtx.scopedKey,
      });
    }
    if (state.infoModalSpec && !$("info-modal-backdrop").classList.contains("hidden")) {
      renderInfoModal();
    }
    if (state.serviceStopped) {
      const badge = $("svc-state-badge");
      badge.textContent = t("storage.stateStopped");
      badge.className = "badge badge-warn";
    }
  }

  async function switchLocale(targetLocale) {
    if (!I18N) return;
    const canonical = I18N.normalizeLocale(targetLocale);
    if (canonical === I18N.getLocale() || state.localeSwitching) return;
    state.localeSwitching = true;
    const buttons = document.querySelectorAll("#lang-switch [data-locale]");
    buttons.forEach((b) => {
      b.disabled = true;
    });
    try {
      const res = await apiMutate("PUT", "/api/preferences", { locale: canonical });
      const saved = res?.preferences?.locale || canonical;
      await applyLocaleAndRerender(saved);
    } catch (err) {
      showToast(t("toast.langSwitchFailed", { error: trBackend(err.message) }), true);
    } finally {
      state.localeSwitching = false;
      buttons.forEach((b) => {
        b.disabled = false;
      });
    }
  }

  // ==================== Agent / Model Selection ====================
  function modelsForSelectedAgents() {
    if (!state.meta) return [];
    const out = [];
    for (const a of state.meta.agents) {
      if (!state.selectedAgents.has(a.name)) continue;
      for (const m of a.models) {
        out.push({ agent: a.name, model: m, pair: `${a.name}::${m}` });
      }
    }
    return out;
  }

  function selectAllModelsForAgent(agentName) {
    if (!state.meta) return;
    const entry = state.meta.agents.find((a) => a.name === agentName);
    if (!entry) return;
    for (const m of entry.models) {
      state.selectedModels.add(`${agentName}::${m}`);
    }
  }

  function removeModelsForAgent(agentName) {
    const prefix = `${agentName}::`;
    for (const key of Array.from(state.selectedModels)) {
      if (key.startsWith(prefix)) state.selectedModels.delete(key);
    }
  }

  function syncModelsToMeta(firstLoad = false) {
    if (!state.meta) return;
    if (firstLoad) {
      state.selectedAgents = new Set(state.meta.default_agents || []);
      state.selectedModels.clear();
      for (const a of state.selectedAgents) selectAllModelsForAgent(a);
      return;
    }
    // Keep existing model selections valid and auto-add newly appeared models for selected agents.
    const valid = new Set();
    for (const a of state.meta.agents) {
      for (const m of a.models) valid.add(`${a.name}::${m}`);
    }
    for (const k of Array.from(state.selectedModels)) {
      if (!valid.has(k)) state.selectedModels.delete(k);
    }
    for (const a of state.selectedAgents) {
      const entry = state.meta.agents.find((x) => x.name === a);
      if (!entry) continue;
      const hadAny = entry.models.some((m) => state.selectedModels.has(`${a}::${m}`));
      if (!hadAny) {
        for (const m of entry.models) state.selectedModels.add(`${a}::${m}`);
      }
    }
  }

  // ==================== Rendering: Meta Controls ====================
  function renderMetaControls() {
    if (!state.meta) return;
    $("brand-meta").textContent = t("app.brandMeta", {
      version: state.meta.version,
      records: fmtInt(state.meta.total_records),
    });

    // Populate cwd dropdowns
    const cwds = state.meta.cwds || [];
    for (const selId of ["stats-cwd-select", "rec-cwd"]) {
      const sel = $(selId);
      const prev = sel.value;
      sel.innerHTML = `<option value="">${esc(t("common.allDirectoriesCount", { count: cwds.length }))}</option>` +
        cwds.map((c) => `<option value="${esc(c)}">${esc(shortPath(c))}</option>`).join("");
      if (cwds.includes(prev)) sel.value = prev;
    }

    // Populate records agent dropdown and discount agent dropdown
    const recAgentSel = $("rec-agent");
    const prevRecAgent = recAgentSel.value;
    recAgentSel.innerHTML = `<option value="">${esc(t("common.allAgents"))}</option>` +
      state.meta.agents.map((a) => `<option value="${esc(a.name)}">${esc(agentLabel(a.name))} (${a.tasks})</option>`).join("");
    recAgentSel.value = prevRecAgent;

    const discAgentSel = $("disc-agent");
    const prevDiscAgent = discAgentSel.value;
    discAgentSel.innerHTML = state.meta.agents
      .map((a) => `<option value="${esc(a.name)}">${esc(agentLabel(a.name))}</option>`)
      .join("") + `<option value="*">${esc(t("common.allAgentsWildcard"))}</option>`;
    if (prevDiscAgent) discAgentSel.value = prevDiscAgent;
    else if (state.meta.agents.some((a) => a.name === "zcode")) discAgentSel.value = "zcode";

    renderAgentPills();
    renderModelPills();
  }

  function renderAgentPills() {
    const wrap = $("stats-agent-pills");
    if (!state.meta) return;
    wrap.innerHTML = state.meta.agents
      .map((a) => {
        const sel = state.selectedAgents.has(a.name);
        return `<button type="button" class="pill ${sel ? "selected" : ""}" data-agent="${esc(a.name)}" title="${esc(a.name)}">
          <span>${esc(agentLabel(a.name))}</span>
          <span class="pill-count">${a.tasks}</span>
        </button>`;
      })
      .join("");
  }

  function renderModelPills() {
    const wrap = $("stats-model-pills");
    const available = modelsForSelectedAgents();
    const q = state.statsModelQuery.trim().toLowerCase();
    const visible = q
      ? available.filter(
          (x) =>
            x.model.toLowerCase().includes(q) ||
            x.agent.toLowerCase().includes(q) ||
            agentLabel(x.agent).toLowerCase().includes(q)
        )
      : available;
    const selectedCount = available.filter((x) => state.selectedModels.has(x.pair)).length;
    $("stats-model-count").textContent = available.length
      ? t("stats.modelsSelectedCount", { selected: selectedCount, total: available.length })
      : t("stats.modelsEmptyCount");

    if (!available.length) {
      wrap.innerHTML = `<span class="muted">${esc(t("stats.modelsEmptyList"))}</span>`;
      return;
    }
    const multiAgent = state.selectedAgents.size > 1;
    wrap.innerHTML = visible
      .map((x) => {
        const sel = state.selectedModels.has(x.pair);
        const label = multiAgent ? `${agentLabel(x.agent)} / ${x.model}` : x.model;
        return `<button type="button" class="pill ${sel ? "selected" : ""}" data-pair="${esc(x.pair)}" title="${esc(x.pair)}">
          <span class="mono">${esc(label)}</span>
        </button>`;
      })
      .join("");
  }

  // ==================== Tab 1: Stats ====================
  async function loadSummary() {
    const params = new URLSearchParams();
    params.set("agents_filter", "1");
    params.set("agents", Array.from(state.selectedAgents).join(","));

    const availableModels = modelsForSelectedAgents();
    if (availableModels.length > 0) {
      params.set("models_filter", "1");
      const chosen = availableModels.filter((x) => state.selectedModels.has(x.pair)).map((x) => x.pair);
      params.set("models", chosen.join(","));
    }

    params.set("range", state.statsRange);
    if (state.statsRange === "custom") {
      if (state.statsStart) params.set("start", new Date(state.statsStart).toISOString());
      if (state.statsEnd) params.set("end", new Date(state.statsEnd).toISOString());
    }
    if (state.statsCwd) params.set("cwd", state.statsCwd);

    state.summary = await apiGet(`/api/summary?${params.toString()}`);
    renderSummary();
  }

  function renderSummary() {
    if (!state.summary) return;
    const ov = state.summary.overview;

    // Empty hint when selected agents have 0 tasks while other agents have tasks
    const banner = $("stats-empty-hint");
    if (ov.tasks === 0 && state.meta && state.meta.total_records > 0) {
      const nonEmpty = state.meta.agents.filter((a) => a.tasks > 0);
      if (nonEmpty.length > 0) {
        const breakdown = nonEmpty.map((a) => `${agentLabel(a.name)}: ${a.tasks}`).join(" / ");
        banner.innerHTML = `<span>${esc(t("stats.emptyHintText", { total: fmtInt(state.meta.total_records), breakdown }))}</span>
          <button type="button" class="btn btn-primary btn-sm" id="btn-show-nonempty">${esc(t("stats.emptyHintAction"))}</button>`;
        banner.classList.remove("hidden");
        $("btn-show-nonempty")?.addEventListener("click", () => {
          state.selectedAgents = new Set(nonEmpty.map((a) => a.name));
          state.selectedModels.clear();
          for (const a of state.selectedAgents) selectAllModelsForAgent(a);
          renderAgentPills();
          renderModelPills();
          loadSummary().catch((e) => showToast(trBackend(e.message), true));
        });
      } else {
        banner.classList.add("hidden");
      }
    } else {
      banner.classList.add("hidden");
    }

    // KPI Cards
    const covText =
      ov.coverage_ratio !== null
        ? t("stats.kpiCoverage", { measured: ov.measured_tasks, tasks: ov.tasks, pct: fmtPct(ov.coverage_ratio) })
        : t("stats.kpiNoTasks");
    const unpricedNote =
      ov.unpriced_tasks > 0
        ? t("stats.kpiUnpricedExcluded", { count: ov.unpriced_tasks })
        : t("stats.kpiAllPriced");
    const listDiff =
      ov.list_cost_usd !== null && ov.effective_cost_usd !== null && Math.abs(ov.list_cost_usd - ov.effective_cost_usd) > 1e-6
        ? t("stats.kpiListCostPrefix", { cost: fmtUsd(ov.list_cost_usd) })
        : "";
    const hitSub =
      ov.cache_hit_rate !== null
        ? t("stats.kpiCacheSub", { rate: fmtPct4(ov.cache_hit_rate), write: fmtCompact(ov.cache_write_tokens) })
        : t("stats.kpiCacheUnreported");
    const outSub =
      ov.reasoning_tokens > 0
        ? ov.tok_per_sec
          ? t("stats.kpiOutputReasoningTps", { reasoning: fmtCompact(ov.reasoning_tokens), tps: ov.tok_per_sec })
          : t("stats.kpiOutputReasoning", { reasoning: fmtCompact(ov.reasoning_tokens) })
        : ov.tok_per_sec
          ? t("stats.kpiOutputTps", { tps: ov.tok_per_sec, samples: ov.tps_samples })
          : "—";

    $("stats-kpis").innerHTML = `
      <div class="kpi-card">
        <div class="kpi-label">${esc(t("stats.kpiTasks"))}</div>
        <div class="kpi-value">${fmtInt(ov.tasks)}</div>
        <div class="kpi-sub">${esc(t("stats.kpiTasksSub", { coverage: covText, days: ov.active_days }))}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">${esc(t("stats.kpiTotalTokens"))}</div>
        <div class="kpi-value">${fmtCompact(ov.total_tokens)}</div>
        <div class="kpi-sub mono">${fmtInt(ov.total_tokens)}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">${esc(t("stats.kpiNetInput"))}</div>
        <div class="kpi-value">${fmtCompact(ov.uncached_input_tokens)}</div>
        <div class="kpi-sub">${esc(t("stats.kpiTotalInputSub", { tokens: fmtCompact(ov.input_tokens) }))}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">${esc(t("stats.kpiCacheRead"))}</div>
        <div class="kpi-value">${ov.cache_read_tokens !== null ? fmtCompact(ov.cache_read_tokens) : "—"}</div>
        <div class="kpi-sub">${esc(hitSub)}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">${esc(t("stats.kpiOutputTokens"))}</div>
        <div class="kpi-value">${fmtCompact(ov.output_tokens)}</div>
        <div class="kpi-sub">${esc(outSub)}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">${esc(t("stats.kpiCost"))}</div>
        <div class="kpi-value">${fmtUsd(ov.effective_cost_usd)}</div>
        <div class="kpi-sub">${esc(listDiff + unpricedNote)}</div>
      </div>
    `;

    renderBarChart();
    renderModelsTable();
    renderAgentsTable();
  }

  function parseIsoDateUtc(isoDate) {
    if (typeof isoDate !== "string") return null;
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate.trim());
    if (!m) return null;
    const year = Number(m[1]);
    const month = Number(m[2]);
    const day = Number(m[3]);
    const dt = new Date(Date.UTC(year, month - 1, day));
    if (dt.getUTCFullYear() !== year || dt.getUTCMonth() !== month - 1 || dt.getUTCDate() !== day) {
      return null;
    }
    return dt;
  }

  function formatIsoDateUtc(dt) {
    const year = String(dt.getUTCFullYear()).padStart(4, "0");
    const month = String(dt.getUTCMonth() + 1).padStart(2, "0");
    const day = String(dt.getUTCDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function enumerateIsoDates(startIso, endIso) {
    const start = parseIsoDateUtc(startIso);
    const end = parseIsoDateUtc(endIso);
    if (!start || !end || start.getTime() > end.getTime()) return null;
    const spanDays = Math.round((end.getTime() - start.getTime()) / 86400000) + 1;
    if (spanDays < 1 || spanDays > 400) return null;
    const dates = [];
    const cursor = new Date(start.getTime());
    for (let i = 0; i < spanDays; i += 1) {
      dates.push(formatIsoDateUtc(cursor));
      cursor.setUTCDate(cursor.getUTCDate() + 1);
    }
    return dates;
  }

  function buildDailyChartSlots(summary) {
    const rawDaily = Array.isArray(summary?.daily) ? summary.daily : [];
    const axis = summary?.daily_axis;
    const axisDates = axis ? enumerateIsoDates(axis.start_date, axis.end_date) : null;
    if (!axisDates) {
      const slots = rawDaily.map((d) => ({
        ...d,
        empty: false,
        usageUnknown: false,
      }));
      return {
        isFiniteAxis: false,
        hasAnyTasks: slots.length > 0,
        slots,
      };
    }

    const byDate = new Map();
    for (const point of rawDaily) {
      if (point && typeof point.date === "string") {
        byDate.set(point.date, point);
      }
    }

    const slots = axisDates.map((dateStr) => {
      const existing = byDate.get(dateStr);
      if (existing && existing.tasks > 0) {
        return {
          ...existing,
          empty: false,
          usageUnknown: !(existing.measured_tasks > 0),
        };
      }
      return {
        date: dateStr,
        tasks: 0,
        measured_tasks: 0,
        uncached_input_tokens: 0,
        cache_read_tokens: 0,
        cache_write_tokens: 0,
        output_tokens: 0,
        total_tokens: 0,
        list_cost_usd: 0,
        effective_cost_usd: 0,
        priced_tasks: 0,
        unpriced_tasks: 0,
        cache_hit_rate: null,
        empty: true,
        usageUnknown: false,
      };
    });

    return {
      isFiniteAxis: true,
      hasAnyTasks: slots.some((d) => !d.empty),
      slots,
    };
  }

  function shouldShowDailyLabel(idx, total, isFiniteAxis) {
    if (total <= 0 || idx < 0 || idx >= total) return false;
    if (!isFiniteAxis) {
      // Preserve v0.1.3 label selection for non-axis ranges ("all", "today", "custom").
      const labelStep = Math.max(1, Math.ceil(total / 10));
      return idx % labelStep === 0 || idx === total - 1;
    }
    const labelStep = total <= 10 ? 1 : Math.max(2, Math.ceil(total / 8));
    const minTailGap = Math.ceil(labelStep / 2);
    return idx === 0 || idx === total - 1 || (idx % labelStep === 0 && total - 1 - idx >= minTailGap);
  }

  function renderDailyBarInner(d, metric) {
    if (metric === "tokens") {
      const denom = d.uncached_input_tokens + d.cache_read_tokens + d.output_tokens;
      if (denom > 0) {
        const pCache = (d.cache_read_tokens / denom) * 100;
        const pIn = (d.uncached_input_tokens / denom) * 100;
        const pOut = Math.max(0, 100 - pCache - pIn);
        return `
          <div class="bar-seg-cache" style="height:${pCache.toFixed(2)}%"></div>
          <div class="bar-seg-in" style="height:${pIn.toFixed(2)}%"></div>
          <div class="bar-seg-out" style="height:${pOut.toFixed(2)}%"></div>`;
      }
      return `<div class="bar-seg-in" style="height:100%"></div>`;
    }
    if (metric === "cost") {
      return `<div class="bar-seg-cost" style="height:100%"></div>`;
    }
    return `<div class="bar-seg-tasks" style="height:100%"></div>`;
  }

  function renderDailyBarColumn({ d, idx, total, val, maxVal, safeMax, metric, isFiniteAxis }) {
    const showLabel = shouldShowDailyLabel(idx, total, isFiniteAxis);
    const totalPct = maxVal > 0 ? Math.max(val > 0 ? 2 : 0, (val / safeMax) * 100) : 0;

    let dayState = "active";
    let stackClass = "bar-stack";
    let stackStyle = `height:${totalPct.toFixed(2)}%`;
    let inner = "";
    let ariaLabel = t("stats.chartBarAria", { date: d.date });
    let btnClass = "bar-col";
    let extraAttrs = "";

    if (!isFiniteAxis) {
      inner = renderDailyBarInner(d, metric);
    } else {
      dayState = d.empty ? "empty" : d.usageUnknown ? "unmeasured" : "active";
      const unknownMetric =
        !d.empty &&
        ((metric === "tokens" && d.usageUnknown) || (metric === "cost" && !(d.priced_tasks > 0)));
      if (d.empty) {
        stackClass = "bar-stack bar-stack-empty";
        stackStyle = "height:0;min-height:0";
      } else if (unknownMetric) {
        stackClass = "bar-stack bar-stack-unmeasured";
        stackStyle = "height:4px;min-height:4px;background:transparent;border:1px dashed var(--fg-subtle);border-bottom:none";
      } else {
        stackStyle = val > 0 ? `height:${totalPct.toFixed(2)}%` : "height:2px;min-height:2px";
        inner = renderDailyBarInner(d, metric);
      }

      if (d.empty) {
        ariaLabel = t("stats.chartBarEmptyAria", { date: d.date });
        btnClass = "bar-col bar-col-empty";
        extraAttrs = ' aria-disabled="true" style="cursor:default"';
      } else if (d.usageUnknown) {
        ariaLabel = t("stats.chartBarUnmeasuredAria", { date: d.date, tasks: fmtInt(d.tasks) });
        btnClass = "bar-col bar-col-unmeasured";
      }
    }

    const isEmpty = isFiniteAxis && d.empty;
    const isUsageUnknown = isFiniteAxis && d.usageUnknown;
    return `<button type="button" class="${btnClass}" data-idx="${idx}" data-date="${esc(d.date)}" data-day-state="${dayState}" data-empty="${isEmpty ? "1" : "0"}" data-usage-unknown="${isUsageUnknown ? "1" : "0"}" aria-label="${esc(ariaLabel)}"${extraAttrs}>
      <div class="${stackClass}" style="${stackStyle}">${inner}</div>
      ${showLabel ? `<span class="bar-date">${esc(d.date.slice(5))}</span>` : ""}
    </button>`;
  }

  function buildDailyTooltipHtml(d, isFiniteAxis = true) {
    if (isFiniteAxis && d.empty) {
      return `
        <div class="tip-date">${esc(d.date)}</div>
        <div class="tip-row"><span class="tip-k">${esc(t("stats.chartNoMatchingTasks"))}</span></div>`;
    }
    if (isFiniteAxis && d.usageUnknown) {
      return `
        <div class="tip-date">${esc(d.date)}</div>
        <div class="tip-row"><span class="tip-k">${esc(t("stats.tipTasks"))}</span><span class="tip-v">${esc(t("stats.tipTasksValue", { tasks: fmtInt(d.tasks), measured: fmtInt(d.measured_tasks) }))}</span></div>
        <div class="tip-row"><span class="tip-k">${esc(t("stats.tipUsageStatus"))}</span><span class="tip-v">${esc(t("stats.tipUsageUnknown"))}</span></div>
        <div class="tip-row"><span class="tip-k">${esc(t("stats.tipTotalTokens"))}</span><span class="tip-v"><b>—</b></span></div>
        <div class="tip-row"><span class="tip-k">${esc(t("stats.tipCost"))}</span><span class="tip-v"><b>${d.priced_tasks > 0 ? fmtUsd(d.effective_cost_usd) : esc(t("common.unpriced"))}</b></span></div>`;
    }
    const hitDenom = (d.uncached_input_tokens || 0) + (d.cache_read_tokens || 0);
    const hitRatio =
      d.cache_hit_rate !== undefined && d.cache_hit_rate !== null
        ? d.cache_hit_rate
        : hitDenom > 0 && d.cache_read_tokens > 0
          ? d.cache_read_tokens / hitDenom
          : null;
    return `
      <div class="tip-date">${esc(d.date)}</div>
      <div class="tip-row"><span class="tip-k">${esc(t("stats.tipTasks"))}</span><span class="tip-v">${esc(t("stats.tipTasksValue", { tasks: fmtInt(d.tasks), measured: fmtInt(d.measured_tasks) }))}</span></div>
      <div class="tip-row"><span class="tip-k">${esc(t("stats.tipTotalTokens"))}</span><span class="tip-v"><b>${fmtInt(d.total_tokens)}</b></span></div>
      <div class="tip-row"><span class="tip-k">${esc(t("stats.tipNetInput"))}</span><span class="tip-v">${fmtInt(d.uncached_input_tokens)}</span></div>
      <div class="tip-row"><span class="tip-k">${esc(t("stats.tipCacheRead"))}</span><span class="tip-v">${fmtInt(d.cache_read_tokens)}</span></div>
      <div class="tip-row"><span class="tip-k">${esc(t("stats.tipHitRate"))}</span><span class="tip-v">${fmtPct4(hitRatio)}</span></div>
      <div class="tip-row"><span class="tip-k">${esc(t("stats.tipOutput"))}</span><span class="tip-v">${fmtInt(d.output_tokens)}</span></div>
      <div class="tip-row"><span class="tip-k">${esc(t("stats.tipCost"))}</span><span class="tip-v"><b>${d.priced_tasks > 0 ? fmtUsd(d.effective_cost_usd) : esc(t("common.unpriced"))}</b></span></div>`;
  }

  function renderDailyBarChart({ summary, metric, stage, legend, tooltip, onSelectDate }) {
    if (tooltip) tooltip.classList.add("hidden");

    if (metric === "tokens") {
      legend.innerHTML = `
        <span class="legend-item"><span class="legend-dot" style="background:var(--bar-cache)"></span>${esc(t("stats.legendCacheRead"))}</span>
        <span class="legend-item"><span class="legend-dot" style="background:var(--bar-in)"></span>${esc(t("stats.legendNetInput"))}</span>
        <span class="legend-item"><span class="legend-dot" style="background:var(--bar-out)"></span>${esc(t("stats.legendOutput"))}</span>`;
    } else if (metric === "cost") {
      legend.innerHTML = `<span class="legend-item"><span class="legend-dot" style="background:var(--bar-cost)"></span>${esc(t("stats.legendCost"))}</span>`;
    } else {
      legend.innerHTML = `<span class="legend-item"><span class="legend-dot" style="background:var(--bar-tasks)"></span>${esc(t("stats.legendTasks"))}</span>`;
    }

    const { isFiniteAxis, hasAnyTasks, slots } = buildDailyChartSlots(summary);

    if (!slots.length) {
      stage.innerHTML = `<div class="chart-empty">${esc(t("stats.chartEmpty"))}</div>`;
      return { isFiniteAxis, hasAnyTasks, slots };
    }

    const values = slots.map((d) =>
      isFiniteAxis && d.empty ? 0 : metric === "tokens" ? d.total_tokens : metric === "cost" ? d.effective_cost_usd : d.tasks
    );
    const maxVal = Math.max(...values, 0);
    const safeMax = maxVal > 0 ? maxVal : 1;
    const fmtAxis = (v) => (metric === "cost" ? fmtUsd(v) : metric === "tasks" ? String(Math.round(v)) : fmtCompact(v));

    const gridLines = [1, 0.75, 0.5, 0.25, 0]
      .map((ratio) => {
        const label = !isFiniteAxis || maxVal > 0 || ratio === 0 ? fmtAxis(safeMax * ratio) : "";
        return `<div class="chart-grid-line"><span class="chart-grid-label">${esc(label)}</span></div>`;
      })
      .join("");

    const barsHtml = slots
      .map((d, idx) =>
        renderDailyBarColumn({
          d,
          idx,
          total: slots.length,
          val: values[idx],
          maxVal,
          safeMax,
          metric,
          isFiniteAxis,
        })
      )
      .join("");

    const emptyOverlayHtml =
      isFiniteAxis && !hasAnyTasks
        ? `<div class="chart-empty" data-chart-empty-overlay="1" style="position:absolute;inset:14px 10px 28px 50px;height:auto;pointer-events:none;z-index:1;">${esc(t("stats.chartNoMatchingTasks"))}</div>`
        : "";
    const barsStyle = isFiniteAxis && slots.length >= 20 ? ' style="gap:4px"' : "";
    stage.innerHTML = `<div class="chart-grid">${gridLines}</div>${emptyOverlayHtml}<div class="chart-bars"${barsStyle}>${barsHtml}</div>`;

    stage.querySelectorAll(".bar-col").forEach((btn) => {
      const idx = Number(btn.getAttribute("data-idx"));
      const d = slots[idx];
      if (!d) return;
      btn.addEventListener("pointerenter", (ev) => {
        if (!tooltip) return;
        tooltip.innerHTML = buildDailyTooltipHtml(d, isFiniteAxis);
        tooltip.classList.remove("hidden");
        positionTooltip(ev, tooltip);
      });
      btn.addEventListener("pointermove", (ev) => positionTooltip(ev, tooltip));
      btn.addEventListener("pointerleave", () => tooltip && tooltip.classList.add("hidden"));
      btn.addEventListener("click", () => {
        if (tooltip) tooltip.classList.add("hidden");
        if (isFiniteAxis && d.empty) return;
        if (typeof onSelectDate === "function") onSelectDate(d.date);
      });
    });

    return { isFiniteAxis, hasAnyTasks, slots };
  }

  function renderBarChart() {
    renderDailyBarChart({
      summary: state.summary,
      metric: state.chartMetric,
      stage: $("chart-stage"),
      legend: $("chart-legend"),
      tooltip: $("chart-tooltip"),
      onSelectDate: (date) => {
        state.recDay = date;
        state.recOffset = 0;
        if (state.selectedAgents.size === 1) {
          state.recAgent = Array.from(state.selectedAgents)[0];
          $("rec-agent").value = state.recAgent;
        }
        switchTab("records");
      },
    });
  }

  function positionTooltip(ev, tooltipEl) {
    const tooltip = tooltipEl || $("chart-tooltip");
    if (!tooltip) return;
    const pad = 14;
    const x = Math.min(window.innerWidth - tooltip.offsetWidth - pad, ev.clientX + pad);
    const y = Math.max(pad, ev.clientY - tooltip.offsetHeight - 10);
    tooltip.style.left = `${Math.max(pad, x)}px`;
    tooltip.style.top = `${y}px`;
  }

  function renderModelsTable() {
    const body = $("models-table-body");
    const rows = state.summary?.models || [];
    const q = state.modelsTableQuery.trim().toLowerCase();
    const filtered = q
      ? rows.filter(
          (r) =>
            r.model.toLowerCase().includes(q) ||
            r.agent.toLowerCase().includes(q) ||
            agentLabel(r.agent).toLowerCase().includes(q)
        )
      : rows;
    $("models-table-count").textContent = String(filtered.length);

    if (!filtered.length) {
      body.innerHTML = `<tr><td colspan="13" class="muted" style="text-align:center;padding:24px">${esc(t("stats.modelsTableEmpty"))}</td></tr>`;
      return;
    }

    body.innerHTML = filtered
      .map((r) => {
        const discLabel = trDiscountLabel(r.discount_label || "");
        const discBadge =
          r.discount_multiplier !== 1
            ? ` <span class="badge badge-accent" title="${esc(discLabel)}">×${Number(r.discount_multiplier).toFixed(3)}</span>`
            : "";
        const costCell =
          r.effective_cost_usd !== null
            ? `<span title="${esc(t("stats.listPriceTitle", { cost: fmtUsd(r.list_cost_usd) }))}">${fmtUsd(r.effective_cost_usd)}</span>${discBadge}`
            : `<span class="badge badge-warn">${esc(t("common.unpriced"))}</span>`;
        return `<tr>
          <td class="mono" title="${esc(r.model)}">${esc(r.model)}</td>
          <td><span class="badge" title="${esc(r.agent)}">${esc(agentLabel(r.agent))}</span></td>
          <td>
            <span class="share-bar"><span class="share-fill" style="width:${(r.share_ratio * 100).toFixed(1)}%"></span></span>
            <span class="num muted">${fmtPct(r.share_ratio)}</span>
          </td>
          <td class="num">${fmtInt(r.tasks)} <span class="muted">(${fmtInt(r.measured_tasks)})</span></td>
          <td class="num">${fmtCompact(r.uncached_input_tokens)}</td>
          <td class="num">${r.cache_read_tokens !== null ? fmtCompact(r.cache_read_tokens) : "—"}</td>
          <td class="num">${r.cache_write_tokens > 0 ? fmtCompact(r.cache_write_tokens) : "—"}</td>
          <td class="num">${fmtCompact(r.output_tokens)}</td>
          <td class="num">${fmtPct4(r.cache_hit_rate)}</td>
          <td class="num">${r.tok_per_sec !== null ? `${r.tok_per_sec}` : "—"}</td>
          <td class="num">${fmtCompact(r.total_tokens)}</td>
          <td class="num">${costCell}</td>
          <td class="num">
            <button type="button" class="btn btn-ghost btn-sm btn-quick-price"
              data-agent="${esc(r.agent)}" data-model="${esc(r.model)}">${esc(t("stats.btnPrice"))}</button>
          </td>
        </tr>`;
      })
      .join("");
  }

  function renderAgentsTable() {
    const body = $("agents-table-body");
    const rows = state.summary?.agents || [];
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="10" class="muted" style="text-align:center;padding:18px">${esc(t("common.noData"))}</td></tr>`;
      return;
    }
    body.innerHTML = rows
      .map(
        (a) => `<tr>
        <td><span class="badge badge-accent" title="${esc(a.agent)}">${esc(agentLabel(a.agent))}</span></td>
        <td>
          <span class="share-bar"><span class="share-fill" style="width:${(a.share_ratio * 100).toFixed(1)}%"></span></span>
          <span class="num muted">${fmtPct(a.share_ratio)}</span>
        </td>
        <td class="num">${fmtInt(a.tasks)}</td>
        <td class="num">${fmtCompact(a.uncached_input_tokens)}</td>
        <td class="num">${fmtCompact(a.cache_read_tokens)}</td>
        <td class="num">${fmtCompact(a.output_tokens)}</td>
        <td class="num">${fmtPct4(a.cache_hit_rate)}</td>
        <td class="num">${fmtCompact(a.total_tokens)}</td>
        <td class="num">${fmtUsd(a.list_cost_usd)}</td>
        <td class="num"><b>${fmtUsd(a.effective_cost_usd)}</b></td>
      </tr>`
      )
      .join("");
  }

  // ==================== Tab 2: Records (明细) ====================
  function applyColumnVisibility() {
    document.querySelectorAll(".col-toggle").forEach((cb) => {
      const col = cb.getAttribute("data-col");
      cb.checked = Boolean(state.recCols[col]);
    });
    for (const [col, visible] of Object.entries(state.recCols)) {
      document.querySelectorAll(`[data-col-cell="${col}"]`).forEach((el) => {
        el.classList.toggle("hidden", !visible);
      });
    }
  }

  async function loadRecords() {
    const params = new URLSearchParams();
    if (state.recAgent) {
      params.set("agents_filter", "1");
      params.set("agents", state.recAgent);
    }
    if (state.recStatus && state.recStatus !== "all") params.set("status", state.recStatus);
    if (state.recRange && state.recRange !== "all") params.set("range", state.recRange);
    if (state.recSpeed && state.recSpeed !== "all") params.set("speed", state.recSpeed);
    if (state.recCwd) params.set("cwd", state.recCwd);
    if (state.recSession) params.set("session_id", state.recSession);
    if (state.recQuery) params.set("q", state.recQuery);
    if (state.recDay) params.set("day", state.recDay);
    params.set("offset", String(state.recOffset));
    params.set("limit", String(state.recLimit));

    state.records = await apiGet(`/api/records?${params.toString()}`);
    renderRecords();
  }

  function renderRecords() {
    if (!state.records) return;
    const dayWrap = $("rec-day-chip-wrap");
    dayWrap.innerHTML = state.recDay
      ? `<span class="badge badge-accent">${esc(t("records.dayLocked", { day: state.recDay }))} <button type="button" class="btn-link" id="btn-clear-day" aria-label="${esc(t("records.clearDayAria"))}">×</button></span>`
      : `<span class="muted">${esc(t("records.clickRowHint"))}</span>`;
    $("btn-clear-day")?.addEventListener("click", () => {
      state.recDay = "";
      state.recOffset = 0;
      loadRecords().catch((e) => showToast(trBackend(e.message), true));
    });

    const st = state.records.scope_totals;
    const exclParts = [];
    if (st.unpriced_tasks > 0) exclParts.push(t("records.scopeExcludedUnpriced", { count: st.unpriced_tasks }));
    if (st.unmeasured_tasks > 0) exclParts.push(t("records.scopeExcludedUnmeasured", { count: st.unmeasured_tasks }));
    const scopeMainHtml = tHtml("records.scopeSummary", {
      tasks: `<b>${esc(fmtInt(st.tasks))}</b>`,
      tokens: `<b>${esc(fmtCompact(st.total_tokens))}</b>`,
      cost: `<b>${esc(fmtUsd(st.effective_cost_usd))}</b>`,
    });
    const exclHtml = exclParts.length
      ? ` <span class="muted">${esc(t("records.scopeExcludedWrap", { parts: exclParts.join(t("common.listJoiner")) }))}</span>`
      : "";
    $("rec-scope-bar").innerHTML = `
      <div>${scopeMainHtml}${exclHtml}</div>
      <div class="muted">${esc(
        t("records.scopeShowing", {
          from: state.records.total ? state.records.offset + 1 : 0,
          to: Math.min(state.records.total, state.records.offset + state.records.items.length),
          total: fmtInt(state.records.total),
        })
      )}</div>
    `;

    const body = $("records-table-body");
    if (!state.records.items.length) {
      body.innerHTML = `<tr><td colspan="10" class="muted" style="text-align:center;padding:24px">${esc(t("records.empty"))}</td></tr>`;
    } else {
      body.innerHTML = state.records.items
        .map((item) => {
          const tok = item.tokens;
          const c = item.cost;
          let tokHtml = `<span class="muted">${esc(t("common.unreported"))}</span>`;
          if (tok.measured) {
            const parts = [];
            parts.push(`<span title="${esc(t("records.chipNetInputTitle"))}">${fmtCompact(tok.uncached_input_tokens)}</span>`);
            if (tok.cache_reported && tok.cache_read_tokens !== null) {
              parts.push(`<span class="tok-c" title="${esc(t("records.chipCacheReadTitle"))}">c:${fmtCompact(tok.cache_read_tokens)}</span>`);
            }
            if (tok.cache_write_tokens > 0) {
              parts.push(`<span class="tok-w" title="${esc(t("records.chipCacheWriteTitle"))}">w:${fmtCompact(tok.cache_write_tokens)}</span>`);
            }
            parts.push(`<span title="${esc(t("records.chipOutputTitle"))}">→${fmtCompact(tok.output_tokens)}</span>`);
            if (tok.reasoning_tokens > 0) {
              parts.push(`<span class="tok-r" title="${esc(t("records.chipReasoningTitle"))}">r:${fmtCompact(tok.reasoning_tokens)}</span>`);
            }
            tokHtml = `<div class="tok-chips">${parts.join("")}</div>`;
          }
          const modelNote =
            item.observed_model && item.model && item.observed_model !== item.model
              ? ` <span class="badge badge-warn" title="${esc(t("records.observedTitle", { requested: item.model, observed: item.observed_model }))}">${esc(t("records.observedBadge"))}</span>`
              : "";
          const deltaBadge = tok.delta_from_conversation
            ? ` <span class="badge" title="${esc(t("records.deltaTitle"))}">Δ</span>`
            : "";
          const effortVal = item.observed_effort || item.effort || "—";
          const costStr = c.priced
            ? `${fmtUsd(c.effective_cost_usd)}${c.discount_multiplier !== 1 ? ` <span class="muted">×${Number(c.discount_multiplier).toFixed(2)}</span>` : ""}`
            : `<span class="muted">—</span>`;
          return `<tr data-task-id="${esc(item.task_id)}">
            <td class="mono muted">${esc(fmtTime(item.started_at || item.created_at))}</td>
            <td class="cell-trunc" title="${esc((item.title || item.task_id) + (item.cwd ? " · " + item.cwd : ""))}">
              <div>${esc(item.title || item.task_id)}${deltaBadge}</div>
              <div class="muted mono" style="font-size:11px">${esc(shortPath(item.cwd))}</div>
            </td>
            <td><span class="badge" title="${esc(item.agent)}">${esc(agentLabel(item.agent))}</span></td>
            <td class="mono">${esc(item.effective_model)}${modelNote}</td>
            <td data-col-cell="effort" class="mono muted">${esc(effortVal)}</td>
            <td>${statusBadge(item.status)}</td>
            <td data-col-cell="tokens" class="num">${tokHtml}</td>
            <td data-col-cell="rate" class="num">${item.tok_per_sec !== null ? item.tok_per_sec : "—"}</td>
            <td data-col-cell="cost" class="num">${costStr}</td>
            <td data-col-cell="duration" class="num muted">${esc(fmtDuration(item.duration_ms))}</td>
          </tr>`;
        })
        .join("");
    }

    $("records-pager-info").textContent = t("records.pagerInfo", {
      page: Math.floor(state.records.offset / state.records.limit) + 1,
      limit: state.records.limit,
    });
    $("btn-rec-prev").disabled = state.records.offset <= 0;
    $("btn-rec-next").disabled = state.records.offset + state.records.limit >= state.records.total;
    applyColumnVisibility();
  }

  async function openDrawer(taskId) {
    try {
      const res = await apiGet(`/api/records/${encodeURIComponent(taskId)}`);
      state.drawerTaskId = taskId;
      state.drawerRecord = res.record;
      renderDrawer(res.record);
      $("drawer-backdrop").classList.remove("hidden");
      $("detail-drawer").classList.remove("hidden");
    } catch (err) {
      showToast(t("toast.loadDetailFailed", { error: trBackend(err.message) }), true);
    }
  }

  function closeDrawer() {
    state.drawerTaskId = null;
    state.drawerRecord = null;
    $("drawer-backdrop").classList.add("hidden");
    $("detail-drawer").classList.add("hidden");
  }

  function renderDrawer(r) {
    $("drawer-task-id").textContent = r.task_id;
    const tok = r.tokens;
    const c = r.cost;
    const scopeNote = tok.delta_from_conversation
      ? tok.delta_reset_warning
        ? t("drawer.scopeConvReset")
        : t("drawer.scopeConvDelta")
      : r.usage_scope;

    let costSection = "";
    if (c.priced) {
      const discDisplay = c.discount_label
        ? t("drawer.discountApplied", {
            label: trDiscountLabel(c.discount_label, c.discount_rule_id),
            pct: c.discount_quota_boost_pct,
            mult: c.discount_multiplier,
          })
        : t("drawer.discountNone");
      costSection = `
        <div class="kv-grid">
          <div class="kv-k">${esc(t("drawer.matchedPriceKey"))}</div><div class="kv-v mono">${esc(c.matched_price_key)} ${priceSourceBadge(c.price_source)}</div>
          <div class="kv-k">${esc(t("drawer.unitRates"))}</div><div class="kv-v mono">${esc(
            t("drawer.unitRatesValue", {
              input: fmtRate(c.rate_input),
              output: fmtRate(c.rate_output),
              cacheRead: fmtRate(c.rate_cache_read),
              cacheWrite: fmtRate(c.rate_cache_write),
            })
          )}</div>
          <div class="kv-k">${esc(t("drawer.costBreakdown"))}</div><div class="kv-v mono"><span class="cost-term">${esc(t("drawer.costTermInput", { amount: fmtUsd(c.input_cost_usd) }))}</span> + <span class="cost-term">${esc(t("drawer.costTermCacheRead", { amount: fmtUsd(c.cache_read_cost_usd) }))}</span> + <span class="cost-term">${esc(t("drawer.costTermCacheWrite", { amount: fmtUsd(c.cache_write_cost_usd) }))}</span> + <span class="cost-term">${esc(t("drawer.costTermOutput", { amount: fmtUsd(c.output_cost_usd) }))}</span></div>
          <div class="kv-k">${esc(t("drawer.listCost"))}</div><div class="kv-v mono">${fmtUsd(c.list_cost_usd)}</div>
          <div class="kv-k">${esc(t("drawer.harnessDiscount"))}</div><div class="kv-v">${esc(discDisplay)}</div>
          <div class="kv-k">${esc(t("drawer.effectiveCost"))}</div><div class="kv-v mono"><b>${fmtUsd(c.effective_cost_usd)}</b></div>
        </div>`;
    } else {
      const reasons = {
        usage_missing: t("drawer.reasonUsageMissing"),
        usage_breakdown_missing: t("drawer.reasonBreakdownMissing"),
        price_unmatched: t("drawer.reasonPriceUnmatched"),
      };
      costSection = `<div class="muted">${esc(reasons[c.unavailable_reason] || c.unavailable_reason || t("drawer.reasonUnavailable"))}</div>`;
    }

    const alertsHtml =
      r.error || (r.warnings && r.warnings.length)
        ? `<div class="detail-sec">
            <h4>${esc(t("drawer.secAlerts"))}</h4>
            ${r.error ? `<div class="danger-text mono" style="font-size:12px;margin-bottom:6px">${esc(r.error)}</div>` : ""}
            ${(r.warnings || []).map((w) => `<div class="muted mono" style="font-size:12px">• ${esc(w)}</div>`).join("")}
          </div>`
        : "";

    $("drawer-body").innerHTML = `
      <div class="detail-sec">
        <h4>${esc(t("drawer.secBasic"))}</h4>
        <div class="kv-grid">
          <div class="kv-k">${esc(t("common.agentHarness"))}</div><div class="kv-v"><span class="badge badge-accent" title="${esc(r.agent)}">${esc(agentLabel(r.agent))}</span></div>
          <div class="kv-k">${esc(t("common.status"))}</div><div class="kv-v">${statusBadge(r.status)} ${r.stop_reason ? `<span class="muted mono">(${esc(r.stop_reason)})</span>` : ""}</div>
          <div class="kv-k">${esc(t("common.model"))}</div><div class="kv-v mono">${esc(r.effective_model)}${r.model && r.observed_model && r.model !== r.observed_model ? ` <span class="muted">${esc(t("drawer.requestedModel", { model: r.model }))}</span>` : ""}</div>
          <div class="kv-k">${esc(t("drawer.effort"))}</div><div class="kv-v mono">${esc(r.observed_effort || r.effort || "—")}</div>
          <div class="kv-k">${esc(t("drawer.sessionId"))}</div><div class="kv-v kv-v-path mono">${esc(r.session_id)}</div>
          <div class="kv-k">${esc(t("drawer.nativeSessionId"))}</div><div class="kv-v kv-v-path mono">${esc(r.native_session_id || "—")}</div>
          <div class="kv-k">${esc(t("drawer.cwd"))}</div><div class="kv-v kv-v-path mono">${esc(r.cwd || "—")}</div>
          <div class="kv-k">${esc(t("drawer.taskTitle"))}</div><div class="kv-v">${esc(r.title || "—")}</div>
          <div class="kv-k">${esc(t("drawer.filesChanged"))}</div><div class="kv-v mono">${fmtInt(r.files_changed_total)}${r.files_outside_cwd_total > 0 ? esc(t("drawer.outsideCwd", { count: r.files_outside_cwd_total })) : ""}</div>
        </div>
      </div>

      <div class="detail-sec">
        <h4>${esc(t("drawer.secPerf"))}</h4>
        <div class="kv-grid">
          <div class="kv-k">${esc(t("drawer.startedAt"))}</div><div class="kv-v mono">${esc(fmtTime(r.started_at || r.created_at))}</div>
          <div class="kv-k">${esc(t("drawer.finishedAt"))}</div><div class="kv-v mono">${esc(fmtTime(r.finished_at))}</div>
          <div class="kv-k">${esc(t("drawer.duration"))}</div><div class="kv-v mono">${esc(fmtDuration(r.duration_ms))}</div>
          <div class="kv-k">${esc(t("drawer.endToEndOutput"))}</div><div class="kv-v mono">${r.tok_per_sec !== null ? `${r.tok_per_sec} tok/s` : "—"}</div>
        </div>
      </div>

      <div class="detail-sec">
        <h4>${esc(t("drawer.secTokens"))}</h4>
        <div class="kv-grid">
          <div class="kv-k">${esc(t("drawer.totalTokens"))}</div><div class="kv-v mono"><b>${fmtInt(tok.total_tokens)}</b></div>
          <div class="kv-k">${esc(t("drawer.totalInput"))}</div><div class="kv-v mono">${fmtInt(tok.input_tokens)}</div>
          <div class="kv-k">${esc(t("drawer.netInput"))}</div><div class="kv-v mono">${fmtInt(tok.uncached_input_tokens)}</div>
          <div class="kv-k">${esc(t("drawer.cacheRead"))}</div><div class="kv-v mono">${tok.cache_reported ? `${fmtInt(tok.cache_read_tokens)} (${fmtPct4(r.cache_hit_rate)})` : esc(t("common.unreported"))}</div>
          <div class="kv-k">${esc(t("drawer.cacheWrite"))}</div><div class="kv-v mono">${tok.cache_write_tokens !== null ? fmtInt(tok.cache_write_tokens) : "—"}</div>
          <div class="kv-k">${esc(t("drawer.outputInclReasoning"))}</div><div class="kv-v mono">${fmtInt(tok.output_tokens)}</div>
          <div class="kv-k">${esc(t("drawer.reasoning"))}</div><div class="kv-v mono">${fmtInt(tok.reasoning_tokens)}</div>
          <div class="kv-k">${esc(t("drawer.usageScope"))}</div><div class="kv-v">${esc(scopeNote)}</div>
        </div>
      </div>

      <div class="detail-sec">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <h4 style="margin:0">${esc(t("drawer.secCost"))}</h4>
          <button type="button" class="btn btn-ghost btn-sm" id="btn-drawer-edit-price">${esc(t("drawer.editModelPrice"))}</button>
        </div>
        ${costSection}
      </div>

      ${alertsHtml}

      <div class="detail-sec">
        <h4>${esc(t("drawer.secRawUsage"))}</h4>
        <pre class="json-pre">${esc(JSON.stringify(r.usage_raw || {}, null, 2))}</pre>
      </div>
    `;

    $("btn-drawer-edit-price")?.addEventListener("click", () => {
      openPriceModal(r.agent, r.effective_model);
    });
  }

  // ==================== Tab 3: Pricing & Harness Discounts ====================
  async function loadPricing() {
    state.pricing = await apiGet("/api/pricing");
    renderPricing();
  }

  function renderPricing() {
    if (!state.pricing) return;
    renderDiscountsTable();
    renderPricingTable();
  }

  function renderDiscountsTable() {
    const rules = state.pricing?.config?.harness_discounts || [];
    const body = $("discounts-table-body");
    if (!rules.length) {
      body.innerHTML = `<tr><td colspan="8" class="muted" style="text-align:center;padding:18px">${esc(t("pricing.discountsEmpty"))}</td></tr>`;
      return;
    }
    body.innerHTML = rules
      .map(
        (r, idx) => `<tr>
        <td><input type="checkbox" class="disc-toggle" data-idx="${idx}" aria-label="${esc(t("pricing.toggleRuleAria"))}" ${r.enabled ? "checked" : ""} /></td>
        <td><span class="badge badge-accent" title="${esc(r.agent)}">${esc(agentLabel(r.agent))}</span></td>
        <td class="mono">${esc(r.model_pattern || "*")}</td>
        <td>${esc(r.mode === "quota_boost" ? t("pricing.modeQuotaBoostShort") : t("pricing.modeMultiplierShort"))}</td>
        <td class="num">${Number(r.quota_boost_pct).toFixed(1)}%</td>
        <td class="num"><b>×${Number(r.cost_multiplier).toFixed(4)}</b></td>
        <td>${esc(r.label ? trDiscountLabel(r.label, r.id) : "—")} ${r.preset ? `<span class="badge">${esc(t("pricing.presetBadge"))}</span>` : ""}</td>
        <td class="num"><button type="button" class="btn btn-ghost btn-sm disc-delete" data-idx="${idx}">${esc(t("common.delete"))}</button></td>
      </tr>`
      )
      .join("");
  }

  function renderPricingTable() {
    const body = $("pricing-table-body");
    const view = state.pricingView;
    const q = state.pricingSearch.trim().toLowerCase();
    const userModels = state.pricing?.config?.models || {};
    let rows = [];

    if (view === "observed" || view === "unpriced") {
      rows = (state.pricing.observed_models || [])
        .filter((m) => (view === "unpriced" ? m.source === "unpriced" : true))
        .map((m) => ({
          key: m.model,
          agent: m.agent,
          subLabel: t("pricing.subObserved", { agent: agentLabel(m.agent), tasks: m.tasks }),
          source: m.source,
          rate: m.rate,
          discount:
            m.discount_multiplier !== 1
              ? `${trDiscountLabel(m.discount_label || "")} (×${Number(m.discount_multiplier).toFixed(3)})`
              : "—",
          hasUserOverride: Object.keys(userModels).some(
            (k) => k.toLowerCase() === m.model.toLowerCase() || k.toLowerCase() === `${m.agent}::${m.model}`.toLowerCase()
          ),
        }));
    } else if (view === "user") {
      rows = Object.entries(userModels).map(([k, rate]) => {
        const [ag] = k.includes("::") ? k.split("::", 2) : ["*", k];
        return {
          key: k,
          agent: ag === "*" ? "" : ag,
          subLabel: ag === "*" ? t("pricing.subGlobalOverride") : t("pricing.subAgentOnly", { agent: agentLabel(ag) }),
          source: "user",
          rate,
          discount: "—",
          hasUserOverride: true,
          exactKey: k,
        };
      });
    } else {
      rows = (state.pricing.presets || []).map((p) => {
        const hasOverride = Object.keys(userModels).some((k) => k.toLowerCase() === p.key.toLowerCase());
        const effectiveRate = hasOverride
          ? Object.entries(userModels).find(([k]) => k.toLowerCase() === p.key.toLowerCase())[1]
          : p.rate;
        return {
          key: p.key,
          agent: p.agent_hint,
          subLabel: t("pricing.subPresetRef", { agent: agentLabel(p.agent_hint) }),
          source: hasOverride ? "user" : "preset",
          rate: effectiveRate,
          discount:
            p.discount_multiplier !== undefined && p.discount_multiplier !== 1
              ? `${trDiscountLabel(p.discount_label || "")} (×${Number(p.discount_multiplier).toFixed(3)})`
              : "—",
          hasUserOverride: hasOverride,
        };
      });
    }

    if (q) {
      rows = rows.filter(
        (r) => r.key.toLowerCase().includes(q) || (r.agent || "").toLowerCase().includes(q) || r.subLabel.toLowerCase().includes(q)
      );
    }

    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="8" class="muted" style="text-align:center;padding:22px">${esc(t("pricing.ratesEmpty"))}</td></tr>`;
      return;
    }

    body.innerHTML = rows
      .map((r) => {
        const rate = r.rate;
        return `<tr>
          <td class="mono">${esc(r.key)}</td>
          <td>${priceSourceBadge(r.source)} <span class="muted" style="font-size:12px">${esc(r.subLabel)}</span></td>
          <td class="num">${rate ? fmtRate(rate.input) : "—"}</td>
          <td class="num">${rate ? fmtRate(rate.output) : "—"}</td>
          <td class="num">${rate ? fmtRate(rate.cache_read) : "—"}</td>
          <td class="num">${rate ? fmtRate(rate.cache_write) : "—"}</td>
          <td class="muted col-gap-left" style="font-size:12px">${esc(r.discount)}</td>
          <td class="num">
            <button type="button" class="btn btn-ghost btn-sm btn-edit-price-row"
              data-agent="${esc(r.agent || "")}" data-key="${esc(r.exactKey || r.key)}">${esc(t("common.edit"))}</button>
          </td>
        </tr>`;
      })
      .join("");
  }

  function updateDiscountPreview() {
    const mode = $("disc-mode").value;
    const val = Number($("disc-value").value);
    $("disc-value-label").textContent = mode === "quota_boost" ? t("pricing.formValueQuota") : t("pricing.formValueMultiplier");
    if (!Number.isFinite(val) || val <= 0) {
      $("disc-preview").textContent = t("pricing.previewInvalid");
      return;
    }
    if (mode === "quota_boost") {
      $("disc-preview").textContent = t("pricing.previewMultiplier", { value: (100 / val).toFixed(4) });
    } else {
      $("disc-preview").textContent = t("pricing.previewQuota", { value: (100 / val).toFixed(1) });
    }
  }

  // ==================== Quick Pricing Modal ====================
  function openPriceModal(agent, modelKey) {
    if (!state.pricing) return;
    const userModels = state.pricing.config.models || {};
    const isScopedKey = modelKey.includes("::");
    const baseModel = isScopedKey ? modelKey.split("::", 2)[1] : modelKey;
    const scopedAgent = isScopedKey ? modelKey.split("::", 2)[0] : agent;
    const scopedKey = scopedAgent ? `${scopedAgent}::${baseModel}` : baseModel;

    const exactScoped = Object.entries(userModels).find(([k]) => k.toLowerCase() === scopedKey.toLowerCase());
    const exactGlobal = Object.entries(userModels).find(([k]) => k.toLowerCase() === baseModel.toLowerCase());
    const obs = (state.pricing.observed_models || []).find(
      (o) => o.model.toLowerCase() === baseModel.toLowerCase() && (!scopedAgent || o.agent.toLowerCase() === scopedAgent.toLowerCase())
    );
    const preset = (state.pricing.presets || []).find((p) => p.key.toLowerCase() === baseModel.toLowerCase());

    const activeRate = exactScoped?.[1] || exactGlobal?.[1] || obs?.rate || preset?.rate || null;
    const useScoped = Boolean(exactScoped || isScopedKey || baseModel === "<default>");

    state.modalCtx = { agent: scopedAgent, baseModel, scopedKey };
    $("modal-scope-wrap").classList.toggle("hidden", !scopedAgent);
    $("modal-scope-agent").checked = useScoped;
    $("modal-scope-label").textContent = t("pricing.modalScopeAgent", {
      agent: agentLabel(scopedAgent),
      scopedKey,
    });
    $("modal-key").value = useScoped && scopedAgent ? scopedKey : baseModel;
    $("modal-in").value = activeRate ? String(activeRate.input) : "";
    $("modal-out").value = activeRate ? String(activeRate.output) : "";
    $("modal-cr").value = activeRate ? String(activeRate.cache_read) : "";
    $("modal-cw").value = activeRate ? String(activeRate.cache_write) : "";
    $("modal-note").value = activeRate?.note || "";
    $("btn-modal-delete").classList.toggle("hidden", !(exactScoped || exactGlobal));
    $("modal-backdrop").classList.remove("hidden");
  }

  function closePriceModal() {
    state.modalCtx = null;
    $("modal-backdrop").classList.add("hidden");
  }

  // ==================== Info Modal ====================
  function renderInfoModal() {
    const spec = state.infoModalSpec;
    if (!spec) return;
    const title = spec.titleKey ? t(spec.titleKey, spec.params) : spec.title || t("common.notice");
    const message = spec.messageKey ? t(spec.messageKey, spec.params) : spec.message || "";
    const detail = spec.detailKey ? t(spec.detailKey, spec.params) : spec.detail || "";
    $("info-modal-title").textContent = title;
    $("info-modal-message").textContent = message;
    const detailEl = $("info-modal-detail");
    if (detail) {
      detailEl.textContent = detail;
      detailEl.classList.remove("hidden");
    } else {
      detailEl.textContent = "";
      detailEl.classList.add("hidden");
    }
  }

  function openInfoModal(specOrTitle, message, detail = "") {
    if (specOrTitle && typeof specOrTitle === "object") {
      state.infoModalSpec = specOrTitle;
    } else {
      state.infoModalSpec = { title: specOrTitle, message: message || "", detail };
    }
    renderInfoModal();
    $("info-modal-backdrop").classList.remove("hidden");
  }

  function closeInfoModal() {
    state.infoModalSpec = null;
    $("info-modal-backdrop").classList.add("hidden");
  }

  function fmtUptime(sec) {
    const s = Math.max(0, Math.floor(Number(sec) || 0));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    const remS = s % 60;
    if (m < 60) return `${m}m ${remS}s`;
    const h = Math.floor(m / 60);
    const remM = m % 60;
    if (h < 24) return `${h}h ${remM}m`;
    const d = Math.floor(h / 24);
    return `${d}d ${h % 24}h`;
  }

  // ==================== Tab 4: Management (服务与磁盘管理) ====================
  async function loadService() {
    const res = await apiGet("/api/service");
    state.service = res.service;
    state.serviceStopped = false;
    renderService({ preserveInputs: false });
  }

  function renderService(opts = {}) {
    const svc = state.service;
    if (!svc) return;
    const preserveInputs = Boolean(opts.preserveInputs);
    const badge = $("svc-state-badge");
    badge.textContent = svc.running ? t("storage.stateRunning") : t("storage.stateStopped");
    badge.className = svc.running ? "badge badge-ok" : "badge badge-warn";

    const metaBadges = [];
    if (svc.pid) metaBadges.push(`<span class="badge mono">PID ${esc(svc.pid)}</span>`);
    if (svc.is_checkout) {
      metaBadges.push(`<span class="badge badge-warn">${esc(t("storage.badgeCheckout"))}</span>`);
    } else if (svc.install_kind) {
      metaBadges.push(`<span class="badge">${esc(svc.install_kind)}</span>`);
    }
    if (svc.autostart?.enabled && !svc.autostart?.matches_current) {
      metaBadges.push(`<span class="badge badge-warn">${esc(t("storage.badgeAutostartDrift"))}</span>`);
    }
    $("svc-meta-badges").innerHTML = metaBadges.join("");

    const autostartText = svc.windows_supported
      ? svc.autostart?.enabled
        ? t("storage.autostartEnabled")
        : t("storage.autostartDisabled")
      : t("storage.windowsOnly");
    const trayText = svc.windows_supported
      ? svc.tray_running
        ? t("storage.trayRunning")
        : t("storage.trayNotRunning")
      : t("storage.windowsOnly");
    $("svc-status-strip").innerHTML = `
      <div><span class="muted">${esc(t("storage.stripListen"))}</span><span class="mono">${esc(svc.url)}</span></div>
      <div><span class="muted">${esc(t("storage.stripUptime"))}</span><span class="mono">${svc.running ? esc(fmtUptime(svc.uptime_sec)) : "—"}</span></div>
      <div><span class="muted">${esc(t("storage.stripTrayAutostart"))}</span><span>${esc(trayText)} / ${esc(autostartText)}</span></div>
      <div><span class="muted">${esc(t("storage.stripLogFile"))}</span><span class="mono">${esc(svc.log_path)}</span></div>
    `;

    const winOk = Boolean(svc.windows_supported);
    if (!preserveInputs) {
      $("svc-autostart").checked = Boolean(svc.config?.auto_start);
      $("svc-tray").checked = Boolean(svc.config?.tray_enabled);
      $("svc-port").value = String(svc.config?.port ?? svc.port ?? 8765);
    }
    $("svc-autostart").disabled = !winOk;
    if (!winOk) {
      $("svc-autostart-note").textContent = t("storage.autostartNoteNonWin");
    } else if (svc.is_checkout) {
      $("svc-autostart-note").textContent = t("storage.autostartNoteCheckout");
    } else {
      $("svc-autostart-note").textContent = t("storage.autostartNoteDefault");
    }

    $("svc-tray").disabled = !winOk;
    const trayBadge = $("svc-tray-badge");
    trayBadge.textContent = !winOk
      ? t("storage.windowsOnly")
      : svc.tray_running
        ? t("storage.trayRunning")
        : t("storage.trayNotRunning");
    trayBadge.className = winOk && svc.tray_running ? "badge badge-ok" : "badge";
  }

  async function waitForServiceReconnect(targetUrl) {
    const prevPid = state.service?.pid ?? null;
    clearInterval(state.autoRefreshTimer);
    state.autoRefreshTimer = null;
    syncAutoRefreshUI(false);

    let sameOrigin = true;
    try {
      const parsed = new URL(targetUrl || window.location.href, window.location.href);
      sameOrigin = parsed.origin === window.location.origin;
    } catch {
      sameOrigin = true;
    }

    if (!sameOrigin) {
      setTimeout(() => {
        window.location.href = targetUrl;
      }, 1800);
      return;
    }

    // Wait briefly so the old process begins shutting down before polling /api/health.
    await new Promise((r) => setTimeout(r, 900));
    let sawDown = false;
    for (let i = 0; i < 45; i++) {
      try {
        const res = await fetch("/api/health", { cache: "no-store" });
        if (res.ok) {
          const data = await res.json();
          if (sawDown || (prevPid !== null && data.pid && data.pid !== prevPid)) {
            window.location.reload();
            return;
          }
        }
      } catch {
        sawDown = true;
      }
      await new Promise((r) => setTimeout(r, 700));
    }
    showToast(t("toast.reconnectTimeout"), true);
  }

  async function loadStorage() {
    const res = await apiGet("/api/storage");
    state.storage = res.storage;
    renderStorage({ preserveInputs: false });
  }

  function renderStorage(opts = {}) {
    const st = state.storage;
    if (!st) return;
    const preserveInputs = Boolean(opts.preserveInputs);
    const p = st.policy;
    const maxBytes = p.max_bytes;
    const pct = maxBytes ? Math.min(100, (st.ledger_bytes / maxBytes) * 100) : 0;
    const capText = maxBytes
      ? `${fmtBytes(st.ledger_bytes)} / ${fmtBytes(maxBytes)} (${pct.toFixed(1)}%)`
      : t("storage.kpiUnlimitedSize", { used: fmtBytes(st.ledger_bytes) });
    const healthText =
      st.corrupt_lines > 0 ? t("storage.kpiCorruptSkipped", { count: st.corrupt_lines }) : t("storage.kpiFormatOk");
    const retentionText = p.max_age_days
      ? t("storage.kpiRetentionDays", { days: p.max_age_days })
      : t("storage.kpiRetentionUnlimited");

    $("storage-kpis").innerHTML = `
      <div class="kpi-card">
        <div class="kpi-label">${esc(t("storage.kpiLedgerSize"))}</div>
        <div class="kpi-value">${fmtBytes(st.ledger_bytes)}</div>
        <div class="kpi-sub">${esc(capText)}</div>
        ${maxBytes ? `<div class="progress-bar"><div class="progress-fill" style="width:${pct.toFixed(1)}%"></div></div>` : ""}
      </div>
      <div class="kpi-card">
        <div class="kpi-label">${esc(t("storage.kpiLedgerCount"))}</div>
        <div class="kpi-value">${fmtInt(st.record_count)}</div>
        <div class="kpi-sub">${esc(t("storage.kpiRecordsSub", { health: healthText, count: fmtInt(st.receipts_on_disk) }))}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">${esc(t("storage.kpiEarliest"))}</div>
        <div class="kpi-value" style="font-size:16px">${esc(fmtTime(st.earliest_ts))}</div>
        <div class="kpi-sub">${esc(t("storage.kpiRetentionSub", { retention: retentionText }))}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">${esc(t("storage.kpiLatest"))}</div>
        <div class="kpi-value" style="font-size:16px">${esc(fmtTime(st.latest_ts))}</div>
        <div class="kpi-sub">${esc(t("storage.kpiTotalBytes", { bytes: fmtBytes(st.total_billing_bytes) }))}</div>
      </div>
    `;

    if (!preserveInputs) {
      $("st-enable-size").checked = p.max_bytes !== null;
      $("st-max-mb").disabled = p.max_bytes === null;
      $("st-max-mb").value = p.max_bytes !== null ? String(Math.max(1, Math.round(p.max_bytes / (1024 * 1024)))) : "100";

      $("st-enable-age").checked = p.max_age_days !== null;
      $("st-max-days").disabled = p.max_age_days === null;
      $("st-max-days").value = p.max_age_days !== null ? String(p.max_age_days) : "180";

      $("st-auto-rotate").checked = Boolean(p.auto_rotate);
    }

    $("storage-paths").innerHTML = `
      <div>${esc(t("storage.pathDir", { path: st.billing_dir }))}</div>
      <div>${esc(t("storage.pathLedger", { path: st.ledger_path }))}</div>
      <div>${esc(t("storage.pathPricing", { path: st.pricing_path }))}</div>
      <div>${esc(t("storage.pathPolicy", { path: st.storage_policy_path }))}</div>
    `;
  }

  // ==================== Tab 4: AGY Slot Rotation & Cleanup ====================
  const AGY_VALID_STATUSES = new Set(["active", "cooling", "blocked", "releasable", "unknown"]);
  const AGY_PREVIEW_ALLOWED_STATUSES = new Set(["cooling", "blocked", "releasable"]);

  function agyEl(tag, opts = null, ...children) {
    const node = document.createElement(tag);
    if (opts && typeof opts === "object") {
      if (opts.className) node.className = opts.className;
      if (opts.id) node.id = opts.id;
      if (opts.text !== undefined && opts.text !== null) {
        node.textContent = String(opts.text);
      }
      if (opts.attrs && typeof opts.attrs === "object") {
        for (const [k, v] of Object.entries(opts.attrs)) {
          if (v !== undefined && v !== null) node.setAttribute(k, String(v));
        }
      }
      if (opts.type) node.type = opts.type;
      if (opts.value !== undefined) node.value = String(opts.value);
      if (opts.placeholder !== undefined) node.placeholder = String(opts.placeholder);
      if (opts.disabled !== undefined) node.disabled = Boolean(opts.disabled);
      if (opts.autocomplete !== undefined) node.autocomplete = opts.autocomplete;
      if (opts.spellcheck !== undefined) node.spellcheck = opts.spellcheck;
      if (opts.on && typeof opts.on === "object") {
        for (const [evName, handler] of Object.entries(opts.on)) {
          if (typeof handler === "function") node.addEventListener(evName, handler);
        }
      }
    }
    for (const child of children) {
      if (child === null || child === undefined || child === false) continue;
      if (Array.isArray(child)) {
        for (const item of child) {
          if (item === null || item === undefined || item === false) continue;
          node.appendChild(typeof item === "string" ? document.createTextNode(item) : item);
        }
      } else if (typeof child === "string" || typeof child === "number") {
        node.appendChild(document.createTextNode(String(child)));
      } else {
        node.appendChild(child);
      }
    }
    return node;
  }

  function agyNormalizeStatus(status) {
    return typeof status === "string" && AGY_VALID_STATUSES.has(status) ? status : "unknown";
  }

  function agyStatusLabel(status) {
    const s = agyNormalizeStatus(status);
    if (s === "active") return t("agySlots.statusActive");
    if (s === "cooling") return t("agySlots.statusCooling");
    if (s === "blocked") return t("agySlots.statusBlocked");
    if (s === "releasable") return t("agySlots.statusReleasable");
    return t("agySlots.statusUnknown");
  }

  function agyStatusDesc(status) {
    const s = agyNormalizeStatus(status);
    if (s === "active") return t("agySlots.statusDescActive");
    if (s === "cooling") return t("agySlots.statusDescCooling");
    if (s === "blocked") return t("agySlots.statusDescBlocked");
    if (s === "releasable") return t("agySlots.statusDescReleasable");
    return t("agySlots.statusDescUnknown");
  }

  function agyStatusBadgeClass(status) {
    const s = agyNormalizeStatus(status);
    if (s === "active") return "badge badge-accent";
    if (s === "cooling") return "badge badge-warn";
    if (s === "blocked") return "badge badge-warn";
    if (s === "releasable") return "badge badge-ok";
    return "badge badge-danger";
  }

  function agyOutcomeLabel(outcome) {
    if (outcome === "completed") return t("agySlots.outcomeCompleted");
    if (outcome === "partial") return t("agySlots.outcomePartial");
    if (outcome === "failed") return t("agySlots.outcomeFailed");
    if (outcome === "unknown") return t("agySlots.outcomeUnknown");
    if (outcome === "unconfirmed") return t("agySlots.outcomeUnconfirmed");
    if (outcome === "not_started") return t("agySlots.outcomeNotStarted");
    if (outcome === "historical_unverified") return t("agySlots.outcomeHistoricalUnverified");
    return outcome ? String(outcome) : t("agySlots.valueUnknown");
  }

  function agyStopReasonLabel(stopReason) {
    if (stopReason === "completed") return t("agySlots.stopCompleted");
    if (stopReason === "preview") return t("agySlots.stopPreview");
    if (stopReason === "unknown") return t("agySlots.stopUnknown");
    if (stopReason === "worker_failed") return t("agySlots.stopWorkerFailed");
    if (stopReason === "no_progress") return t("agySlots.stopNoProgress");
    if (stopReason === "targets_changed") return t("agySlots.stopTargetsChanged");
    if (stopReason === "residual_locked") return t("agySlots.stopResidualLocked");
    if (stopReason === "batch_limit") return t("agySlots.stopBatchLimit");
    if (stopReason === "admission_changed") return t("agySlots.stopAdmissionChanged");
    if (stopReason === "checkpoint_failed") return t("agySlots.stopCheckpointFailed");
    if (stopReason === null || stopReason === undefined || stopReason === "") return "—";
    return String(stopReason);
  }

  function agyBlockedReasonLabel(reason) {
    if (reason === "active_task") return t("agySlots.blockedSessionReasonActiveTask");
    if (reason === "active_session") return t("agySlots.blockedSessionReasonActiveSession");
    if (reason === "unclosed_session") return t("agySlots.blockedSessionReasonUnclosedSession");
    if (reason === "unattributed_active_task") return t("agySlots.blockedSessionReasonUnattributedActiveTask");
    return reason ? String(reason) : t("agySlots.valueUnknown");
  }

  function agyResidualStatusLabel(status) {
    if (status === "native_removed") return t("agySlots.residualStatusNativeRemoved");
    if (status === "windows_residual_reclaimed") return t("agySlots.residualStatusReclaimed");
    if (status === "residual_locked") return t("agySlots.residualStatusLocked");
    return status ? String(status) : t("agySlots.valueUnknown");
  }

  function agyFormatTriState(val) {
    if (val === null || val === undefined) {
      return { kind: "unknown", text: t("agySlots.tristateUnknown") };
    }
    if (Array.isArray(val)) {
      if (val.length === 0) {
        return { kind: "empty", text: t("agySlots.tristateEmpty") };
      }
      return {
        kind: "items",
        text: t("agySlots.tristateItems", {
          count: String(val.length),
          items: val.map((item) => String(item)).join(", "),
        }),
      };
    }
    return { kind: "unknown", text: t("agySlots.tristateUnknown") };
  }

  function agyFormatHours(hours) {
    if (hours === null || hours === undefined || Number.isNaN(Number(hours))) return "—";
    const num = Number(hours);
    const rounded = Math.abs(num - Math.round(num)) < 0.05 ? String(Math.round(num)) : num.toFixed(1);
    return t("agySlots.hoursValue", { hours: rounded });
  }

  function agyLiveCleanupAvailable(data) {
    return Boolean(data && data.enabled === true && data.cleaner && data.cleaner.live_cleanup_available === true);
  }

  function agyFindSlot(data, slotName) {
    if (!data || !Array.isArray(data.slots) || !slotName) return null;
    return data.slots.find((s) => s && s.name === slotName) || null;
  }

  function agyHasHealthAlert(slot) {
    return Boolean(slot && (slot.project_file_exists === false || slot.project_is_empty === false));
  }

  function agyIsValidPreviewForSlot(slot, preview) {
    if (!slot || !preview || !preview.payload || typeof preview.payload !== "object") return false;
    const p = preview.payload;
    if (preview.slot !== slot.name || preview.project_id !== slot.project_id) return false;
    if (p.slot !== slot.name || p.project_id !== slot.project_id) return false;
    if (preview.httpStatus !== 200) return false;
    if (p.dry_run !== true || p.ok !== true) return false;
    if (p.outcome !== "completed") return false;
    if (p.counts_confirmed !== true) return false;
    if (p.stop_reason !== undefined && p.stop_reason !== null && p.stop_reason !== "preview" && p.stop_reason !== "completed") {
      return false;
    }
    if (Array.isArray(p.errors) && p.errors.length > 0) return false;
    return true;
  }

  function agyCanPreviewSlot(data, slot) {
    if (!data || !slot) return false;
    if (state.agyBusyMode !== null) return false;
    if (!agyLiveCleanupAvailable(data)) return false;
    return AGY_PREVIEW_ALLOWED_STATUSES.has(agyNormalizeStatus(slot.status));
  }

  function agyPreviewDisabledReason(data, slot) {
    if (state.agyBusyMode !== null) return t("agySlots.previewDisabledBusy");
    if (!slot) return null;
    const s = agyNormalizeStatus(slot.status);
    if (s === "active") return t("agySlots.previewDisabledActive");
    if (s === "unknown") return t("agySlots.previewDisabledUnknown");
    if (!agyLiveCleanupAvailable(data)) return t("agySlots.previewDisabledUnavailable");
    return null;
  }

  function agyEvaluateLiveConditions(data, slot, confirmText, preview) {
    const cleanerOk = agyLiveCleanupAvailable(data);
    const hasPreview = agyIsValidPreviewForSlot(slot, preview) &&
      Array.isArray(preview.payload.eligible_ids) && preview.payload.eligible_ids.length > 0;
    const status = slot ? agyNormalizeStatus(slot.status) : "unknown";
    const isReleasable = Boolean(slot && status === "releasable" && !slot.guard_error);
    const confirmMatches = Boolean(slot && typeof slot.name === "string" && slot.name.length > 0 && confirmText === slot.name);
    const missingReasons = [];
    if (!cleanerOk) missingReasons.push(t("agySlots.checkCleanerUnavailable"));
    if (!hasPreview) missingReasons.push(t("agySlots.checkPreviewMissing"));
    if (!isReleasable) missingReasons.push(t("agySlots.checkReleasableMissing", { status: agyStatusLabel(status) }));
    if (!confirmMatches) {
      missingReasons.push(t("agySlots.checkConfirmMissing", { slot: slot ? slot.name : "" }));
    }
    const allMet = Boolean(cleanerOk && hasPreview && isReleasable && confirmMatches && state.agyBusyMode === null);
    return { cleanerOk, hasPreview, isReleasable, confirmMatches, allMet, missingReasons };
  }

  function agyIsFullRoundCompleted(receipt) {
    if (!receipt || typeof receipt !== "object") return false;
    if (receipt.dry_run !== false) return false;
    if (receipt.ok !== true) return false;
    if (receipt.outcome !== "completed") return false;
    if (receipt.counts_confirmed === false) return false;
    if (receipt.stop_reason !== undefined && receipt.stop_reason !== null && receipt.stop_reason !== "completed") {
      return false;
    }
    if (Array.isArray(receipt.remaining_ids) && receipt.remaining_ids.length > 0) return false;
    if (Array.isArray(receipt.unconfirmed_delete_ids) && receipt.unconfirmed_delete_ids.length > 0) return false;
    if (receipt.has_more === true) return false;
    if (receipt.pending_batch_number !== undefined && receipt.pending_batch_number !== null) return false;
    if (Array.isArray(receipt.errors) && receipt.errors.length > 0) return false;
    return true;
  }

  function agyExtractBatchRows(receipt) {
    if (!receipt || !Array.isArray(receipt.batches) || receipt.batches.length === 0) return [];
    const out = [];
    receipt.batches.forEach((entry, idx) => {
      if (!entry || typeof entry !== "object") return;
      if (Array.isArray(entry.residuals)) {
        out.push({
          kind: "batch",
          batchNumber: entry.batch_number ?? idx + 1,
          ok: entry.ok,
          outcomeUnknown: Boolean(entry.outcome_unknown),
          http200Count: Array.isArray(entry.http_200_ids) ? entry.http_200_ids.length : 0,
          elapsedMs: entry.worker_elapsed_ms ?? null,
          errors: Array.isArray(entry.errors) ? entry.errors : [],
          residuals: entry.residuals,
        });
      } else if ("id" in entry || "moved" in entry || "remaining" in entry || "status" in entry) {
        out.push({
          kind: "flat_residual",
          batchNumber: entry.batch_number ?? idx + 1,
          residual: entry,
        });
      }
    });
    return out;
  }

  function closeAgyCleanupModal() {
    state.agyModalOpen = false;
    state.agyModalTarget = null;
    const backdrop = $("agy-cleanup-modal-backdrop");
    if (backdrop) backdrop.classList.add("hidden");
  }

  function invalidateAgyConfirmState(opts = {}) {
    const clearPreview = opts.clearPreview !== false;
    const markInvalidated = Boolean(opts.markInvalidated);
    const hadConfirmState = Boolean(state.agyConfirmText || state.agyValidPreview || state.agyModalOpen);
    state.agyConfirmText = "";
    if (clearPreview) {
      state.agyValidPreview = null;
    }
    state.agyMissingNotice = null;
    closeAgyCleanupModal();
    if (markInvalidated && hadConfirmState) {
      state.agyInvalidated = true;
    }
    const input = $("agy-confirm-input");
    if (input) input.value = "";
  }

  function applyAgySlotsData(nextData) {
    const prevData = state.agySlots;
    const prevSelectedName = state.agySelectedSlotName;
    const prevSlot = agyFindSlot(prevData, prevSelectedName);
    const prevAvail = agyLiveCleanupAvailable(prevData);

    state.agySlots = nextData;
    state.agySlotsError = null;

    if (!nextData || nextData.enabled !== true || !Array.isArray(nextData.slots) || nextData.slots.length === 0) {
      invalidateAgyConfirmState({ clearPreview: true, markInvalidated: true });
      state.agySelectedSlotName = null;
      return;
    }

    const nextSlot = agyFindSlot(nextData, prevSelectedName);
    if (!nextSlot) {
      invalidateAgyConfirmState({ clearPreview: true, markInvalidated: Boolean(prevSelectedName) });
      state.agySelectedSlotName = nextData.slots[0].name;
      return;
    }

    const nextAvail = agyLiveCleanupAvailable(nextData);
    const prevStatus = prevSlot ? agyNormalizeStatus(prevSlot.status) : null;
    const nextStatus = agyNormalizeStatus(nextSlot.status);
    const projectRebound = Boolean(prevSlot && prevSlot.project_id !== nextSlot.project_id);
    const statusChanged = Boolean(prevSlot && prevStatus !== nextStatus);
    const guardChanged = Boolean(prevSlot && (prevSlot.guard_error || null) !== (nextSlot.guard_error || null));
    const previewMismatch = Boolean(
      state.agyValidPreview &&
        (state.agyValidPreview.slot !== nextSlot.name || state.agyValidPreview.project_id !== nextSlot.project_id)
    );
    const cleanerDropped = prevAvail && !nextAvail;

    if (projectRebound || statusChanged || guardChanged || previewMismatch || cleanerDropped) {
      invalidateAgyConfirmState({ clearPreview: true, markInvalidated: true });
    } else if (nextStatus !== "releasable" && (state.agyConfirmText || state.agyModalOpen)) {
      invalidateAgyConfirmState({ clearPreview: false, markInvalidated: true });
    }
  }

  async function postAgySlotCleanup(payload) {
    const res = await fetch("/api/agy/slots/cleanup", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Bridge-CSRF": CSRF_TOKEN,
      },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => null);
    return { status: res.status, ok: res.ok, data };
  }

  async function loadAgySlots(opts = {}) {
    if (state.agySlotsLoading) return;
    state.agySlotsLoading = true;
    const refreshBtn = $("btn-agy-slots-refresh");
    if (refreshBtn) {
      refreshBtn.disabled = true;
      refreshBtn.textContent = t("agySlots.refreshing");
    }
    try {
      const res = await fetch("/api/agy/slots", { cache: "no-store" });
      const data = await res.json().catch(() => null);
      if (!res.ok || !data || data.ok === false) {
        const msg = (data && data.error) || `HTTP ${res.status}`;
        state.agySlotsError = msg;
        if (opts.showToastOnSuccess) {
          showToast(t("agySlots.loadFailed", { error: trBackend(msg) }), true);
        }
      } else {
        applyAgySlotsData(data);
        if (opts.showToastOnSuccess) {
          showToast(t("agySlots.toastRefreshed"));
        }
      }
    } catch (err) {
      state.agySlotsError = err?.message || "network error";
      if (opts.showToastOnSuccess) {
        showToast(t("agySlots.loadFailed", { error: trBackend(state.agySlotsError) }), true);
      }
    } finally {
      state.agySlotsLoading = false;
      if (state.agySlotsError) {
        state.agySlots = null;
        invalidateAgyConfirmState({ clearPreview: true, markInvalidated: true });
      }
      if (refreshBtn) {
        refreshBtn.disabled = state.agyBusyMode !== null;
        refreshBtn.textContent = t("agySlots.refreshBtn");
      }
      renderAgySlots({ preserveInputs: false });
    }
  }

  function selectAgySlot(slotName) {
    if (state.agyBusyMode !== null) return;
    if (!slotName || slotName === state.agySelectedSlotName) return;
    invalidateAgyConfirmState({ clearPreview: true, markInvalidated: true });
    state.agySelectedSlotName = slotName;
    renderAgySlots({ preserveInputs: false });
  }

  async function runAgyDryRunPreview() {
    const data = state.agySlots;
    const slot = agyFindSlot(data, state.agySelectedSlotName);
    if (!agyCanPreviewSlot(data, slot)) return;

    state.agyBusyMode = "preview";
    state.agyMissingNotice = null;
    state.agyInvalidated = false;
    closeAgyCleanupModal();
    renderAgySlots({ preserveInputs: true });

    const reqBody = { slot: slot.name, dry_run: true };
    if (typeof slot.project_id === "string" && slot.project_id.trim()) {
      reqBody.project_id = slot.project_id;
    }

    try {
      const resp = await postAgySlotCleanup(reqBody);
      const payload = resp.data || { ok: false, error: `HTTP ${resp.status}`, slot: slot.name, project_id: slot.project_id, dry_run: true };
      state.agyLastAction = {
        slot: slot.name,
        project_id: slot.project_id,
        dry_run: true,
        httpStatus: resp.status,
        payload,
        networkError: null,
      };

      // Sync slot_status from response if server reports a status change.
      if (payload && typeof payload.slot_status === "string" && AGY_VALID_STATUSES.has(payload.slot_status)) {
        if (agyNormalizeStatus(slot.status) !== agyNormalizeStatus(payload.slot_status)) {
          slot.status = payload.slot_status;
          if (Array.isArray(payload.blocked_sessions)) {
            slot.blocked_sessions = payload.blocked_sessions;
          }
          invalidateAgyConfirmState({ clearPreview: true, markInvalidated: true });
        }
      }

      const candidate = {
        slot: slot.name,
        project_id: slot.project_id,
        httpStatus: resp.status,
        payload,
      };
      if (agyIsValidPreviewForSlot(slot, candidate)) {
        state.agyValidPreview = candidate;
        state.agyInvalidated = false;
        showToast(t("agySlots.toastPreviewOk", { slot: slot.name }));
      } else {
        invalidateAgyConfirmState({ clearPreview: true, markInvalidated: true });
        if (resp.status === 409) {
          showToast(t("agySlots.toastConflict", { slot: slot.name }), true);
        } else if (resp.status === 502) {
          showToast(t("agySlots.toastBadGateway", { slot: slot.name }), true);
        } else {
          showToast(t("agySlots.toastPreviewFailed", { slot: slot.name }), true);
        }
      }
    } catch (err) {
      invalidateAgyConfirmState({ clearPreview: true, markInvalidated: true });
      const msg = err?.message || "network error";
      state.agyLastAction = {
        slot: slot.name,
        project_id: slot.project_id,
        dry_run: true,
        httpStatus: 0,
        payload: {
          ok: false,
          slot: slot.name,
          project_id: slot.project_id,
          dry_run: true,
          outcome: "unknown",
          errors: [msg],
        },
        networkError: msg,
      };
      showToast(t("agySlots.toastPreviewFailed", { slot: slot.name }), true);
    } finally {
      state.agyBusyMode = null;
      renderAgySlots({ preserveInputs: false });
    }
  }

  function renderAgyCleanupModal() {
    const body = $("agy-modal-body");
    if (!body) return;
    body.textContent = "";

    const data = state.agySlots;
    const slot = agyFindSlot(data, state.agySelectedSlotName);
    if (!slot || !state.agyModalTarget || state.agyModalTarget.slot !== slot.name || state.agyModalTarget.project_id !== slot.project_id) {
      closeAgyCleanupModal();
      return;
    }

    const previewPayload = state.agyValidPreview?.payload || null;
    const checkedText = previewPayload && previewPayload.checked !== null && previewPayload.checked !== undefined
      ? fmtInt(previewPayload.checked)
      : t("agySlots.valueUnknown");
    const wouldDeleteText = previewPayload && previewPayload.would_delete !== null && previewPayload.would_delete !== undefined
      ? fmtInt(previewPayload.would_delete)
      : t("agySlots.valueUnknown");
    const cutoffText = previewPayload && previewPayload.cutoff_utc ? String(previewPayload.cutoff_utc) : "—";

    body.appendChild(agyEl("p", { className: "small-note", text: t("agySlots.modalIntro") }));
    body.appendChild(
      agyEl(
        "div",
        { className: "agy-modal-kv" },
        agyEl(
          "div",
          { className: "agy-kv-pair" },
          agyEl("span", { className: "agy-kv-label", text: t("agySlots.modalSlotLabel") }),
          agyEl("span", { className: "agy-kv-value mono", text: slot.name })
        ),
        agyEl(
          "div",
          { className: "agy-kv-pair" },
          agyEl("span", { className: "agy-kv-label", text: t("agySlots.modalProjectLabel") }),
          agyEl("span", { className: "agy-kv-value mono", text: slot.project_id || "—" })
        ),
        agyEl(
          "div",
          { className: "agy-kv-pair" },
          agyEl("span", { className: "agy-kv-label", text: t("agySlots.modalPreviewSummaryLabel") }),
          agyEl("span", {
            className: "agy-kv-value mono",
            text: t("agySlots.modalPreviewSummaryValue", {
              checked: checkedText,
              wouldDelete: wouldDeleteText,
              cutoff: cutoffText,
            }),
          })
        )
      )
    );
    body.appendChild(agyEl("div", { className: "agy-notice agy-notice-danger", text: t("agySlots.modalWarnIrreversible") }));
    body.appendChild(agyEl("div", { className: "agy-notice agy-notice-warn", text: t("agySlots.modalWarnNoCli") }));
  }

  function openAgyCleanupModal() {
    const data = state.agySlots;
    const slot = agyFindSlot(data, state.agySelectedSlotName);
    const evalRes = agyEvaluateLiveConditions(data, slot, state.agyConfirmText, state.agyValidPreview);
    const sep = I18N && I18N.getLocale() === "en" ? "; " : "；";
    if (!evalRes.allMet || !slot) {
      state.agyMissingNotice = t("agySlots.missingConditionsPrefix") + evalRes.missingReasons.join(sep);
      updateAgyLiveControls();
      return;
    }
    state.agyMissingNotice = null;
    state.agyModalTarget = { slot: slot.name, project_id: slot.project_id };
    state.agyModalOpen = true;
    renderAgyCleanupModal();
    const backdrop = $("agy-cleanup-modal-backdrop");
    if (backdrop) backdrop.classList.remove("hidden");
  }

  async function submitAgyLiveCleanup() {
    if (!state.agyModalOpen || !state.agyModalTarget) return;
    const data = state.agySlots;
    const slot = agyFindSlot(data, state.agySelectedSlotName);
    const sep = I18N && I18N.getLocale() === "en" ? "; " : "；";
    if (!slot || state.agyModalTarget.slot !== slot.name || state.agyModalTarget.project_id !== slot.project_id) {
      invalidateAgyConfirmState({ clearPreview: true, markInvalidated: true });
      renderAgySlots({ preserveInputs: false });
      return;
    }

    const evalRes = agyEvaluateLiveConditions(data, slot, state.agyConfirmText, state.agyValidPreview);
    if (!evalRes.allMet) {
      closeAgyCleanupModal();
      state.agyMissingNotice = t("agySlots.missingConditionsPrefix") + evalRes.missingReasons.join(sep);
      updateAgyLiveControls();
      return;
    }

    const targetSlotName = slot.name;
    const targetProjectId = slot.project_id;
    const confirmWord = state.agyConfirmText;
    if (confirmWord !== targetSlotName) {
      closeAgyCleanupModal();
      state.agyMissingNotice = t("agySlots.missingConditionsPrefix") + t("agySlots.checkConfirmMissing", { slot: targetSlotName });
      updateAgyLiveControls();
      return;
    }

    closeAgyCleanupModal();
    state.agyBusyMode = "live";
    state.agyMissingNotice = null;
    state.agyInvalidated = false;
    renderAgySlots({ preserveInputs: true });

    const reqBody = {
      slot: targetSlotName,
      dry_run: false,
      confirm: confirmWord,
    };
    if (typeof targetProjectId === "string" && targetProjectId.trim()) {
      reqBody.project_id = targetProjectId;
    }

    try {
      const resp = await postAgySlotCleanup(reqBody);
      const payload = resp.data || {
        ok: false,
        error: `HTTP ${resp.status}`,
        slot: targetSlotName,
        project_id: targetProjectId,
        dry_run: false,
      };
      state.agyLastAction = {
        slot: targetSlotName,
        project_id: targetProjectId,
        dry_run: false,
        httpStatus: resp.status,
        payload,
        networkError: null,
      };
      // Consume and invalidate confirmation & preview after any live cleanup attempt.
      invalidateAgyConfirmState({ clearPreview: true, markInvalidated: false });

      if (resp.status === 409) {
        showToast(t("agySlots.toastConflict", { slot: targetSlotName }), true);
      } else if (resp.status === 502) {
        showToast(t("agySlots.toastBadGateway", { slot: targetSlotName }), true);
      } else if (resp.ok && agyIsFullRoundCompleted(payload)) {
        showToast(t("agySlots.toastLiveOk", { slot: targetSlotName }));
      } else {
        showToast(t("agySlots.toastLiveIncomplete", { slot: targetSlotName }), true);
      }
    } catch (err) {
      invalidateAgyConfirmState({ clearPreview: true, markInvalidated: false });
      const msg = err?.message || "network error";
      state.agyLastAction = {
        slot: targetSlotName,
        project_id: targetProjectId,
        dry_run: false,
        httpStatus: 0,
        payload: {
          ok: false,
          slot: targetSlotName,
          project_id: targetProjectId,
          dry_run: false,
          outcome: "unknown",
          errors: [msg],
        },
        networkError: msg,
      };
      showToast(t("agySlots.toastLiveIncomplete", { slot: targetSlotName }), true);
    } finally {
      state.agyBusyMode = null;
      renderAgySlots({ preserveInputs: false });
      // Best-effort status refresh after live cleanup while preserving structured action receipt.
      await loadAgySlots({ showToastOnSuccess: false }).catch(() => {});
    }
  }

  function renderAgyBlockedSessionsList(sessions) {
    if (!Array.isArray(sessions) || sessions.length === 0) return null;
    const container = agyEl(
      "div",
      { className: "agy-list-box", attrs: { "data-agy-blocked-sessions": "1" } },
      agyEl("div", { className: "agy-list-title", text: t("agySlots.blockedSessionsTitle", { count: fmtInt(sessions.length) }) })
    );
    for (const item of sessions) {
      if (!item || typeof item !== "object") continue;
      container.appendChild(
        agyEl(
          "div",
          { className: "agy-row-card" },
          agyEl(
            "div",
            { className: "agy-kv-pair" },
            agyEl("span", { className: "agy-kv-label", text: t("agySlots.colSessionId") }),
            agyEl("span", { className: "agy-kv-value mono", text: item.session_id || "—" })
          ),
          agyEl(
            "div",
            { className: "agy-kv-pair" },
            agyEl("span", { className: "agy-kv-label", text: t("agySlots.colNativeSessionId") }),
            agyEl("span", { className: "agy-kv-value mono", text: item.native_session_id || "—" })
          ),
          agyEl(
            "div",
            { className: "agy-kv-pair" },
            agyEl("span", { className: "agy-kv-label", text: t("agySlots.colProcState") }),
            agyEl("span", { className: "agy-kv-value mono", text: item.proc_state || "—" })
          ),
          agyEl(
            "div",
            { className: "agy-kv-pair" },
            agyEl("span", { className: "agy-kv-label", text: t("agySlots.colLastActiveAt") }),
            agyEl("span", { className: "agy-kv-value mono", text: item.last_active_at ? fmtTime(item.last_active_at) : "—" })
          ),
          agyEl(
            "div",
            { className: "agy-kv-pair" },
            agyEl("span", { className: "agy-kv-label", text: t("agySlots.colReason") }),
            agyEl("span", { className: "agy-kv-value", text: agyBlockedReasonLabel(item.reason) })
          )
        )
      );
    }
    return container;
  }

  function renderAgyBlockingProcessesList(procs) {
    if (!Array.isArray(procs) || procs.length === 0) return null;
    const box = agyEl(
      "div",
      { className: "agy-notice agy-notice-danger", attrs: { "data-agy-blocking-processes": "1" } },
      agyEl("div", { className: "agy-notice-title", text: t("agySlots.blockingProcessesTitle") }),
      agyEl("div", { className: "small-note", text: t("agySlots.blockingProcessesHint") })
    );
    const listWrap = agyEl("div", { className: "agy-list-box", attrs: { style: "margin-top:8px" } });
    for (const proc of procs) {
      if (!proc || typeof proc !== "object") continue;
      listWrap.appendChild(
        agyEl(
          "div",
          { className: "agy-row-card", attrs: { "data-blocking-pid": proc.pid !== undefined && proc.pid !== null ? String(proc.pid) : "" } },
          agyEl(
            "div",
            { className: "agy-kv-pair" },
            agyEl("span", { className: "agy-kv-label", text: t("agySlots.colPid") }),
            agyEl("span", { className: "agy-kv-value mono", text: proc.pid !== undefined && proc.pid !== null ? String(proc.pid) : "—" })
          ),
          agyEl(
            "div",
            { className: "agy-kv-pair" },
            agyEl("span", { className: "agy-kv-label", text: t("agySlots.colProcName") }),
            agyEl("span", { className: "agy-kv-value mono", text: proc.name ? String(proc.name) : "—" })
          )
        )
      );
    }
    box.appendChild(listWrap);
    return box;
  }

  function renderAgyResidualRow(res) {
    const moved = agyFormatTriState(res ? res.moved : null);
    const remaining = agyFormatTriState(res ? res.remaining : null);
    return agyEl(
      "div",
      { className: "agy-row-card", attrs: { "data-agy-residual-id": res && res.id ? String(res.id) : "" } },
      agyEl(
        "div",
        { className: "agy-kv-pair" },
        agyEl("span", { className: "agy-kv-label", text: t("agySlots.colResidualId") }),
        agyEl("span", { className: "agy-kv-value mono", text: res && res.id ? String(res.id) : "—" })
      ),
      agyEl(
        "div",
        { className: "agy-kv-pair" },
        agyEl("span", { className: "agy-kv-label", text: t("agySlots.colResidualStatus") }),
        agyEl("span", { className: "agy-kv-value", text: agyResidualStatusLabel(res ? res.status : null) })
      ),
      agyEl(
        "div",
        { className: "agy-kv-pair" },
        agyEl("span", { className: "agy-kv-label", text: t("agySlots.colResidualReason") }),
        agyEl("span", { className: "agy-kv-value mono", text: res && res.reason ? String(res.reason) : "—" })
      ),
      agyEl(
        "div",
        { className: "agy-kv-pair" },
        agyEl("span", { className: "agy-kv-label", text: t("agySlots.colMoved") }),
        agyEl("span", {
          className: "agy-tristate",
          text: moved.text,
          attrs: { "data-field": "moved", "data-tristate": moved.kind },
        })
      ),
      agyEl(
        "div",
        { className: "agy-kv-pair" },
        agyEl("span", { className: "agy-kv-label", text: t("agySlots.colRemaining") }),
        agyEl("span", {
          className: "agy-tristate",
          text: remaining.text,
          attrs: { "data-field": "remaining", "data-tristate": remaining.kind },
        })
      )
    );
  }

  function renderAgyReceiptCard(receipt, opts = {}) {
    if (!receipt || typeof receipt !== "object") return null;
    const httpStatus = opts.httpStatus ?? null;
    const titleText = opts.title || t("agySlots.receiptSavedTitle");
    const isDryRun = receipt.dry_run === true;
    const isFullComplete = agyIsFullRoundCompleted(receipt);
    const isPreviewComplete = Boolean(
      isDryRun &&
        receipt.ok === true &&
        receipt.outcome === "completed" &&
        receipt.counts_confirmed !== false &&
        (httpStatus === null || httpStatus === 200) &&
        (!Array.isArray(receipt.errors) || receipt.errors.length === 0)
    );

    const card = agyEl("div", {
      className: "agy-receipt",
      attrs: {
        "data-agy-receipt": opts.isCurrent ? "current" : "saved",
        "data-agy-http-status": httpStatus !== null ? String(httpStatus) : "",
        "data-agy-outcome": receipt.outcome ? String(receipt.outcome) : "",
        "data-agy-stop-reason": receipt.stop_reason ? String(receipt.stop_reason) : "",
      },
    });

    // Header row
    const badges = [];
    badges.push(
      agyEl("span", {
        className: isDryRun ? "badge badge-accent" : "badge badge-danger",
        text: isDryRun ? t("agySlots.receiptModeDryRun") : t("agySlots.receiptModeLive"),
      })
    );
    if (httpStatus !== null && httpStatus > 0) {
      badges.push(
        agyEl("span", {
          className: httpStatus >= 400 ? "badge badge-danger mono" : "badge badge-ok mono",
          text: t("agySlots.receiptHttpStatus", { status: String(httpStatus) }),
        })
      );
    }
    if (receipt.counts_confirmed === false) {
      badges.push(agyEl("span", { className: "badge badge-warn", text: t("agySlots.receiptCountsUnconfirmed") }));
    }
    if (receipt.receipt_saved === false) {
      badges.push(agyEl("span", { className: "badge badge-warn", text: t("agySlots.receiptNotSaved") }));
    }
    if (receipt.late_worker_receipt === true) {
      badges.push(agyEl("span", { className: "badge badge-warn", text: t("agySlots.receiptLateWorker") }));
    }

    card.appendChild(
      agyEl(
        "div",
        { className: "agy-receipt-head" },
        agyEl("div", { className: "agy-receipt-title", text: titleText }),
        agyEl("div", { className: "agy-badge-row" }, badges)
      )
    );

    // Status banner — strictly never claim round completed unless truly completed.
    if (httpStatus === 409) {
      card.appendChild(agyEl("div", { className: "agy-notice agy-notice-danger", text: t("agySlots.receiptConflictBanner") }));
    } else if (httpStatus === 502) {
      card.appendChild(agyEl("div", { className: "agy-notice agy-notice-danger", text: t("agySlots.receiptBadGatewayBanner") }));
    } else if (httpStatus !== null && httpStatus >= 400) {
      card.appendChild(
        agyEl("div", {
          className: "agy-notice agy-notice-danger",
          text: t("agySlots.receiptErrorBanner", { status: String(httpStatus) }),
        })
      );
    } else if (isPreviewComplete) {
      card.appendChild(agyEl("div", { className: "agy-notice agy-notice-ok", text: t("agySlots.receiptPreviewCompleted") }));
    } else if (isFullComplete) {
      card.appendChild(agyEl("div", { className: "agy-notice agy-notice-ok", text: t("agySlots.receiptRoundCompleted") }));
    } else {
      card.appendChild(
        agyEl("div", {
          className: "agy-notice agy-notice-warn",
          text: t("agySlots.receiptIncompleteBanner", {
            outcome: agyOutcomeLabel(receipt.outcome),
            stopReason: agyStopReasonLabel(receipt.stop_reason),
          }),
        })
      );
    }

    // Blocking processes (409)
    const procList = renderAgyBlockingProcessesList(receipt.blocking_processes);
    if (procList) card.appendChild(procList);

    // Blocking sessions attached to receipt (409 or saved receipt)
    const sessList = renderAgyBlockedSessionsList(receipt.blocked_sessions);
    if (sessList) card.appendChild(sessList);

    // Core structured metadata
    const hasMoreText =
      receipt.has_more === true
        ? t("agySlots.boolTrue")
        : receipt.has_more === false
          ? t("agySlots.boolFalse")
          : receipt.has_more === null
            ? t("agySlots.valueUnknown")
            : "—";
    const fmtCountOrUnknown = (v) => (v === null || v === undefined ? t("agySlots.valueUnknown") : fmtInt(v));

    const metaGrid = agyEl(
      "div",
      { className: "agy-meta-grid" },
      agyEl(
        "div",
        { className: "agy-meta-item" },
        agyEl("span", { className: "agy-meta-label", text: t("agySlots.metricOutcome") }),
        agyEl("span", { className: "agy-meta-val mono", text: agyOutcomeLabel(receipt.outcome) })
      ),
      agyEl(
        "div",
        { className: "agy-meta-item" },
        agyEl("span", { className: "agy-meta-label", text: t("agySlots.metricStopReason") }),
        agyEl("span", { className: "agy-meta-val mono", text: agyStopReasonLabel(receipt.stop_reason) })
      ),
      agyEl(
        "div",
        { className: "agy-meta-item" },
        agyEl("span", { className: "agy-meta-label", text: t("agySlots.metricChecked") }),
        agyEl("span", { className: "agy-meta-val mono", text: fmtCountOrUnknown(receipt.checked) })
      ),
      agyEl(
        "div",
        { className: "agy-meta-item" },
        agyEl("span", { className: "agy-meta-label", text: t("agySlots.metricWouldDelete") }),
        agyEl("span", { className: "agy-meta-val mono", text: fmtCountOrUnknown(receipt.would_delete) })
      ),
      agyEl(
        "div",
        { className: "agy-meta-item" },
        agyEl("span", { className: "agy-meta-label", text: t("agySlots.metricDeleted") }),
        agyEl("span", { className: "agy-meta-val mono", text: fmtCountOrUnknown(receipt.deleted) })
      ),
      agyEl(
        "div",
        { className: "agy-meta-item" },
        agyEl("span", { className: "agy-meta-label", text: t("agySlots.metricSkipped") }),
        agyEl("span", {
          className: "agy-meta-val mono",
          text: t("agySlots.metricSkippedValue", {
            running: fmtCountOrUnknown(receipt.skipped_running),
            recent: fmtCountOrUnknown(receipt.skipped_recent),
            unverified: fmtCountOrUnknown(receipt.skipped_unverified),
          }),
        })
      ),
      agyEl(
        "div",
        { className: "agy-meta-item" },
        agyEl("span", { className: "agy-meta-label", text: t("agySlots.metricHasMore") }),
        agyEl("span", { className: "agy-meta-val mono", text: hasMoreText })
      ),
      agyEl(
        "div",
        { className: "agy-meta-item" },
        agyEl("span", { className: "agy-meta-label", text: t("agySlots.metricPendingBatch") }),
        agyEl("span", {
          className: "agy-meta-val mono",
          text: receipt.pending_batch_number !== null && receipt.pending_batch_number !== undefined ? String(receipt.pending_batch_number) : "—",
        })
      ),
      agyEl(
        "div",
        { className: "agy-meta-item" },
        agyEl("span", { className: "agy-meta-label", text: t("agySlots.metricElapsed") }),
        agyEl("span", {
          className: "agy-meta-val mono",
          text: receipt.worker_elapsed_ms !== null && receipt.worker_elapsed_ms !== undefined ? fmtDuration(receipt.worker_elapsed_ms) : "—",
        })
      ),
      agyEl(
        "div",
        { className: "agy-meta-item" },
        agyEl("span", { className: "agy-meta-label", text: t("agySlots.metricExecutedAt") }),
        agyEl("span", { className: "agy-meta-val mono", text: receipt.executed_at ? fmtTime(receipt.executed_at) : "—" })
      ),
      agyEl(
        "div",
        { className: "agy-meta-item" },
        agyEl("span", { className: "agy-meta-label", text: t("agySlots.metricCutoffUtc") }),
        agyEl("span", { className: "agy-meta-val mono", text: receipt.cutoff_utc ? String(receipt.cutoff_utc) : "—" })
      )
    );
    card.appendChild(metaGrid);

    // Strict 4-Concept Distinction Grid
    const http200Count = Array.isArray(receipt.http_200_ids) ? receipt.http_200_ids.length : null;
    const nativeRemoved = receipt.native_removed !== undefined && receipt.native_removed !== null ? receipt.native_removed : null;
    const reclaimed = receipt.windows_residual_reclaimed !== undefined && receipt.windows_residual_reclaimed !== null ? receipt.windows_residual_reclaimed : null;
    const locked = receipt.residual_locked !== undefined && receipt.residual_locked !== null ? receipt.residual_locked : null;
    const remainingCount = Array.isArray(receipt.remaining_ids) ? receipt.remaining_ids.length : null;
    const unconfirmedCount = Array.isArray(receipt.unconfirmed_delete_ids) ? receipt.unconfirmed_delete_ids.length : null;
    const rejectedCount = Array.isArray(receipt.rejected_delete_ids) ? receipt.rejected_delete_ids.length : null;

    const conceptSpecs = [
      {
        key: "http_200_ids",
        tone: "tone-neutral",
        label: t("agySlots.conceptHttp200Label"),
        value: http200Count !== null ? fmtInt(http200Count) : "—",
        note: t("agySlots.conceptHttp200Note"),
      },
      {
        key: "native_removed",
        tone: "tone-ok",
        label: t("agySlots.conceptNativeRemovedLabel"),
        value: nativeRemoved !== null ? fmtInt(nativeRemoved) : "—",
        note: t("agySlots.conceptNativeRemovedNote"),
      },
      {
        key: "windows_residual_reclaimed",
        tone: "tone-warn",
        label: t("agySlots.conceptQuarantineLabel"),
        value: reclaimed !== null ? fmtInt(reclaimed) : "—",
        note: t("agySlots.conceptQuarantineNote"),
      },
      {
        key: "residual_locked",
        tone: locked && locked > 0 ? "tone-danger" : "tone-neutral",
        label: t("agySlots.conceptLockedLabel"),
        value: locked !== null ? fmtInt(locked) : "—",
        note: t("agySlots.conceptLockedNote"),
      },
      {
        key: "remaining_ids",
        tone: remainingCount && remainingCount > 0 ? "tone-warn" : "tone-neutral",
        label: t("agySlots.conceptRemainingLabel"),
        value: remainingCount !== null ? fmtInt(remainingCount) : "—",
        note: t("agySlots.conceptRemainingNote"),
      },
      {
        key: "unconfirmed_delete_ids",
        tone: unconfirmedCount && unconfirmedCount > 0 ? "tone-danger" : "tone-neutral",
        label: t("agySlots.conceptUnconfirmedLabel"),
        value: unconfirmedCount !== null ? fmtInt(unconfirmedCount) : "—",
        note: t("agySlots.conceptUnconfirmedNote"),
      },
      {
        key: "rejected_delete_ids",
        tone: rejectedCount && rejectedCount > 0 ? "tone-danger" : "tone-neutral",
        label: t("agySlots.conceptRejectedLabel"),
        value: rejectedCount !== null ? fmtInt(rejectedCount) : "—",
        note: t("agySlots.conceptRejectedNote"),
      },
    ];

    const conceptsWrap = agyEl(
      "div",
      { className: "agy-list-box", attrs: { "data-agy-concepts": "1" } },
      agyEl("div", { className: "agy-list-title", text: t("agySlots.conceptsTitle") }),
      agyEl(
        "div",
        { className: "agy-concepts-grid" },
        conceptSpecs.map((c) =>
          agyEl(
            "div",
            { className: `agy-concept-card ${c.tone}`, attrs: { "data-concept": c.key } },
            agyEl("div", { className: "agy-concept-label", text: c.label }),
            agyEl("div", { className: "agy-concept-value", text: c.value }),
            agyEl("div", { className: "agy-concept-note", text: c.note })
          )
        )
      )
    );
    card.appendChild(conceptsWrap);

    if (reclaimed !== null && reclaimed > 0) {
      card.appendChild(agyEl("div", { className: "agy-notice agy-notice-warn", text: t("agySlots.quarantineSpaceDisclaimer") }));
    }

    // Batches & residuals breakdown (supports both nested batch.residuals[] and flat batches[])
    const batchRows = agyExtractBatchRows(receipt);
    if (batchRows.length > 0) {
      const batchesBox = agyEl(
        "div",
        { className: "agy-list-box", attrs: { "data-agy-batches": "1" } },
        agyEl("div", { className: "agy-list-title", text: t("agySlots.batchesTitle", { count: fmtInt(batchRows.length) }) })
      );
      for (const b of batchRows) {
        if (b.kind === "flat_residual") {
          batchesBox.appendChild(renderAgyResidualRow(b.residual));
          continue;
        }
        const statusText = b.outcomeUnknown
          ? t("agySlots.batchStatusUnknown")
          : b.ok
            ? t("agySlots.batchStatusOk")
            : t("agySlots.batchStatusFailed");
        const batchCard = agyEl(
          "div",
          { className: "agy-batch-card", attrs: { "data-batch-num": String(b.batchNumber) } },
          agyEl("div", {
            className: "agy-batch-head",
            text: t("agySlots.batchHeader", {
              num: String(b.batchNumber),
              status: statusText,
              http200: fmtInt(b.http200Count),
              elapsed: b.elapsedMs !== null ? fmtDuration(b.elapsedMs) : "—",
            }),
          })
        );
        if (b.residuals.length > 0) {
          for (const res of b.residuals) {
            batchCard.appendChild(renderAgyResidualRow(res));
          }
        } else {
          batchCard.appendChild(agyEl("div", { className: "muted small-note", text: t("agySlots.batchNoResiduals") }));
        }
        if (b.errors.length > 0) {
          batchCard.appendChild(
            agyEl(
              "ul",
              { className: "agy-error-list" },
              b.errors.map((e) => agyEl("li", { text: String(e) }))
            )
          );
        }
        batchesBox.appendChild(batchCard);
      }
      card.appendChild(batchesBox);
    }

    // Errors & diagnostics (all plain text)
    const allErrors = [];
    if (Array.isArray(receipt.errors)) {
      for (const e of receipt.errors) {
        if (e !== null && e !== undefined && String(e).trim()) allErrors.push(String(e));
      }
    }
    if (receipt.error && !allErrors.includes(String(receipt.error))) {
      allErrors.push(String(receipt.error));
    }
    if (receipt.guard_error && !allErrors.includes(String(receipt.guard_error))) {
      allErrors.push(`guard_error: ${String(receipt.guard_error)}`);
    }
    if (receipt.receipt_error && !allErrors.includes(String(receipt.receipt_error))) {
      allErrors.push(`receipt_error: ${String(receipt.receipt_error)}`);
    }
    if (allErrors.length > 0) {
      card.appendChild(
        agyEl(
          "div",
          { className: "agy-notice agy-notice-danger", attrs: { "data-agy-errors": "1" } },
          agyEl("div", { className: "agy-notice-title", text: t("agySlots.errorsTitle", { count: fmtInt(allErrors.length) }) }),
          agyEl(
            "ul",
            { className: "agy-error-list" },
            allErrors.map((msg) => agyEl("li", { text: msg }))
          )
        )
      );
    }

    // Collapsible conversation ID lists (plain text only)
    const idGroups = [
      ["eligible_ids", t("agySlots.idsEligible"), receipt.eligible_ids],
      ["http_200_ids", t("agySlots.idsHttp200"), receipt.http_200_ids],
      ["deleted_ids", t("agySlots.idsDeleted"), receipt.deleted_ids],
      ["would_delete_ids", t("agySlots.idsWouldDelete"), receipt.would_delete_ids],
      ["remaining_ids", t("agySlots.idsRemaining"), receipt.remaining_ids],
      ["unconfirmed_delete_ids", t("agySlots.idsUnconfirmed"), receipt.unconfirmed_delete_ids],
      ["rejected_delete_ids", t("agySlots.idsRejected"), receipt.rejected_delete_ids],
    ].filter(([, , arr]) => Array.isArray(arr) && arr.length > 0);

    if (idGroups.length > 0) {
      card.appendChild(
        agyEl(
          "details",
          { className: "agy-ids-details" },
          agyEl("summary", { text: t("agySlots.idsListToggle") }),
          agyEl(
            "div",
            { className: "agy-ids-list" },
            idGroups.map(([key, label, arr]) =>
              agyEl(
                "div",
                { attrs: { "data-id-group": key } },
                agyEl("div", { className: "agy-ids-group-label", text: `${label} (${arr.length})` }),
                agyEl("div", { className: "agy-ids-values", text: arr.map((id) => String(id)).join(", ") })
              )
            )
          )
        )
      );
    }

    return card;
  }

  function updateAgyLiveControls() {
    const data = state.agySlots;
    const slot = agyFindSlot(data, state.agySelectedSlotName);
    const checklist = $("agy-live-checklist");
    const liveBtn = $("btn-agy-live-open");
    const missingBox = $("agy-missing-conditions");
    if (!slot || !checklist || !liveBtn) return;

    const ev = agyEvaluateLiveConditions(data, slot, state.agyConfirmText, state.agyValidPreview);
    const status = agyNormalizeStatus(slot.status);

    checklist.textContent = "";
    const items = [];
    if (!ev.cleanerOk) {
      items.push({ kind: "missing", text: t("agySlots.checkCleanerUnavailable") });
    }
    items.push({
      kind: ev.hasPreview ? "ok" : "missing",
      text: ev.hasPreview ? t("agySlots.checkPreviewOk") : t("agySlots.checkPreviewMissing"),
    });
    items.push({
      kind: ev.isReleasable ? "ok" : "missing",
      text: ev.isReleasable
        ? t("agySlots.checkReleasableOk")
        : t("agySlots.checkReleasableMissing", { status: agyStatusLabel(status) }),
    });
    items.push({
      kind: ev.confirmMatches ? "ok" : "missing",
      text: ev.confirmMatches
        ? t("agySlots.checkConfirmOk", { slot: slot.name })
        : t("agySlots.checkConfirmMissing", { slot: slot.name }),
    });
    items.push({
      kind: "info",
      text: t("agySlots.checkModalReady"),
    });

    for (const item of items) {
      const markText = item.kind === "ok" ? "✓" : item.kind === "missing" ? "✕" : "•";
      checklist.appendChild(
        agyEl(
          "li",
          { attrs: { "data-check-state": item.kind } },
          agyEl("span", { className: `agy-check-mark ${item.kind}`, text: markText, attrs: { "aria-hidden": "true" } }),
          agyEl("span", { text: item.text })
        )
      );
    }

    liveBtn.disabled = !ev.allMet;
    liveBtn.textContent = state.agyBusyMode === "live" ? t("agySlots.liveRunning") : t("agySlots.liveBtn");

    if (missingBox) {
      if (state.agyMissingNotice) {
        missingBox.textContent = state.agyMissingNotice;
        missingBox.classList.remove("hidden");
      } else {
        missingBox.textContent = "";
        missingBox.classList.add("hidden");
      }
    }
  }

  function renderAgySlots(opts = {}) {
    const root = $("agy-slots-root");
    const stateBadge = $("agy-slots-state-badge");
    const cleanerBadge = $("agy-slots-cleaner-badge");
    const refreshBtn = $("btn-agy-slots-refresh");
    if (!root || !stateBadge || !cleanerBadge) return;

    const preserveInputs = Boolean(opts.preserveInputs);
    if (preserveInputs) {
      const existingInput = $("agy-confirm-input");
      if (existingInput) state.agyConfirmText = existingInput.value;
    }

    if (refreshBtn) {
      refreshBtn.disabled = state.agySlotsLoading || state.agyBusyMode !== null;
      refreshBtn.textContent = state.agySlotsLoading ? t("agySlots.refreshing") : t("agySlots.refreshBtn");
    }

    root.textContent = "";
    root.className = "agy-slots-root";

    if (state.agySlotsError && !state.agySlots) {
      stateBadge.textContent = t("agySlots.badgeError");
      stateBadge.className = "badge badge-danger";
      cleanerBadge.classList.add("hidden");
      root.appendChild(
        agyEl(
          "div",
          { className: "agy-notice agy-notice-danger", attrs: { "data-agy-state": "error" } },
          agyEl("div", { className: "agy-notice-title", text: t("agySlots.loadFailed", { error: trBackend(state.agySlotsError) }) }),
          agyEl("div", { className: "small-note", text: t("agySlots.retryHint") })
        )
      );
      return;
    }

    if (!state.agySlots) {
      stateBadge.textContent = t("agySlots.badgeLoading");
      stateBadge.className = "badge";
      cleanerBadge.classList.add("hidden");
      return;
    }

    const data = state.agySlots;

    // Disabled state (HTTP 200, enabled === false) — render clean empty state, NOT an error.
    if (data.enabled !== true) {
      stateBadge.textContent = t("agySlots.badgeDisabled");
      stateBadge.className = "badge";
      cleanerBadge.classList.add("hidden");
      root.appendChild(
        agyEl(
          "div",
          { className: "agy-empty-box", attrs: { "data-agy-state": "disabled" } },
          agyEl("div", { className: "agy-empty-title", text: t("agySlots.disabledTitle") }),
          agyEl("p", { className: "agy-empty-body", text: t("agySlots.disabledBody") }),
          agyEl("div", { className: "muted small-note", text: t("agySlots.disabledHint") })
        )
      );
      return;
    }

    // Enabled state
    const liveAvail = agyLiveCleanupAvailable(data);
    stateBadge.textContent = t("agySlots.badgeEnabled");
    stateBadge.className = "badge badge-ok";
    cleanerBadge.classList.remove("hidden");
    cleanerBadge.textContent = liveAvail ? t("agySlots.badgeCleanerReady") : t("agySlots.badgeCleanerUnavailable");
    cleanerBadge.className = liveAvail ? "badge badge-ok" : "badge badge-warn";

    if (state.agySlotsError) {
      root.appendChild(
        agyEl("div", {
          className: "agy-notice agy-notice-danger",
          text: t("agySlots.loadFailed", { error: trBackend(state.agySlotsError) }),
        })
      );
    }

    // Global state_error / receipt_error banners (plain text)
    if (data.state_error) {
      root.appendChild(
        agyEl(
          "div",
          { className: "agy-notice agy-notice-danger", attrs: { "data-agy-state-error": "1" } },
          agyEl("div", { className: "agy-notice-title", text: t("agySlots.globalStateErrorTitle") }),
          agyEl("div", { className: "mono small-note", text: String(data.state_error) })
        )
      );
    }
    if (data.receipt_error) {
      root.appendChild(
        agyEl(
          "div",
          { className: "agy-notice agy-notice-danger", attrs: { "data-agy-receipt-error": "1" } },
          agyEl("div", { className: "agy-notice-title", text: t("agySlots.globalReceiptErrorTitle") }),
          agyEl("div", { className: "mono small-note", text: String(data.receipt_error) })
        )
      );
    }

    // Global metadata & cleaner capabilities
    const cleaner = data.cleaner || {};
    const liveBlockReason = cleaner.live_block_reason || cleaner.reason || null;
    const liveText = liveAvail
      ? t("agySlots.liveAvailable")
      : liveBlockReason
        ? t("agySlots.liveUnavailableWithReason", { reason: String(liveBlockReason) })
        : t("agySlots.liveUnavailable");
    const conditionalText = cleaner.conditional_delete_available === true
      ? t("agySlots.conditionalAvailable")
      : t("agySlots.conditionalUnavailableNote");

    root.appendChild(
      agyEl(
        "div",
        { className: "agy-meta-grid", attrs: { "data-agy-meta": "1" } },
        agyEl(
          "div",
          { className: "agy-meta-item" },
          agyEl("span", { className: "agy-meta-label", text: t("agySlots.metaActiveSlot") }),
          agyEl("span", {
            className: "agy-meta-val mono",
            text: data.active_slot ? `${data.active_slot}${data.active_project_id ? ` (${data.active_project_id})` : ""}` : t("agySlots.noneValue"),
          })
        ),
        agyEl(
          "div",
          { className: "agy-meta-item" },
          agyEl("span", { className: "agy-meta-label", text: t("agySlots.metaRotationTz") }),
          agyEl("span", { className: "agy-meta-val mono", text: data.rotation_tz ? String(data.rotation_tz) : "—" })
        ),
        agyEl(
          "div",
          { className: "agy-meta-item" },
          agyEl("span", { className: "agy-meta-label", text: t("agySlots.metaGraceHours") }),
          agyEl("span", {
            className: "agy-meta-val mono",
            text: data.grace_hours !== null && data.grace_hours !== undefined
              ? t("agySlots.metaGraceHoursValue", { hours: String(data.grace_hours) })
              : "—",
          })
        ),
        agyEl(
          "div",
          { className: "agy-meta-item" },
          agyEl("span", { className: "agy-meta-label", text: t("agySlots.metaServerNow") }),
          agyEl("span", { className: "agy-meta-val mono", text: data.now ? fmtTime(data.now) : "—" })
        ),
        agyEl(
          "div",
          { className: "agy-meta-item" },
          agyEl("span", { className: "agy-meta-label", text: t("agySlots.metaCleanerMode") }),
          agyEl("span", { className: "agy-meta-val mono", text: cleaner.mode ? String(cleaner.mode) : "—" })
        ),
        agyEl(
          "div",
          { className: "agy-meta-item" },
          agyEl("span", { className: "agy-meta-label", text: t("agySlots.metaCleanerModel") }),
          agyEl("span", { className: "agy-meta-val mono", text: cleaner.model ? String(cleaner.model) : "—" })
        ),
        agyEl(
          "div",
          { className: "agy-meta-item", attrs: { "data-cleaner-live": liveAvail ? "1" : "0" } },
          agyEl("span", { className: "agy-meta-label", text: t("agySlots.metaLiveCleanup") }),
          agyEl("span", { className: "agy-meta-val", text: liveText })
        ),
        agyEl(
          "div",
          { className: "agy-meta-item agy-meta-item-wide", attrs: { "data-cleaner-conditional": cleaner.conditional_delete_available ? "1" : "0" } },
          agyEl("span", { className: "agy-meta-label", text: t("agySlots.metaConditionalDelete") }),
          agyEl("span", { className: "agy-meta-val muted", text: conditionalText })
        )
      )
    );

    // Persistent operational safety notice
    root.appendChild(
      agyEl("div", {
        className: "agy-notice agy-notice-info",
        text: t("agySlots.operationalNotice"),
        attrs: { "data-agy-operational-notice": "1" },
      })
    );

    const slots = Array.isArray(data.slots) ? data.slots : [];
    if (slots.length === 0) {
      root.appendChild(agyEl("div", { className: "agy-empty-box", text: t("agySlots.slotEmptyList") }));
      return;
    }

    if (!agyFindSlot(data, state.agySelectedSlotName)) {
      state.agySelectedSlotName = slots[0].name;
    }

    // Slot cards grid
    const grid = agyEl("div", {
      className: "agy-slot-grid",
      attrs: { role: "group", "aria-label": t("agySlots.slotSelectAria") },
    });

    for (const slot of slots) {
      const status = agyNormalizeStatus(slot.status);
      const isSelected = slot.name === state.agySelectedSlotName;
      const healthAlert = agyHasHealthAlert(slot);
      const cardClasses = ["agy-slot-card"];
      if (isSelected) cardClasses.push("selected");
      if (healthAlert) cardClasses.push("health-alert");

      const badges = [agyEl("span", { className: agyStatusBadgeClass(status), text: agyStatusLabel(status) })];
      if (slot.project_file_exists === false) {
        badges.push(agyEl("span", { className: "badge badge-danger", text: t("agySlots.healthBadgeMissing") }));
      }
      if (slot.project_is_empty === false) {
        badges.push(agyEl("span", { className: "badge badge-danger", text: t("agySlots.healthBadgeNonEmpty") }));
      }
      if (!healthAlert) {
        badges.push(agyEl("span", { className: "badge badge-ok", text: t("agySlots.healthBadgeOk") }));
      }

      const boundText =
        slot.bound_sessions_count === null || slot.bound_sessions_count === undefined
          ? t("agySlots.valueUnknown")
          : fmtInt(slot.bound_sessions_count);

      const cardBtn = agyEl(
        "button",
        {
          type: "button",
          className: cardClasses.join(" "),
          disabled: state.agyBusyMode !== null,
          attrs: {
            "data-slot-name": slot.name,
            "data-slot-status": status,
            "data-health-alert": healthAlert ? "1" : "0",
            "aria-pressed": String(isSelected),
          },
          on: { click: () => selectAgySlot(slot.name) },
        },
        agyEl(
          "div",
          { className: "agy-slot-card-top" },
          agyEl("span", { className: "agy-slot-name mono", text: slot.name }),
          agyEl("div", { className: "agy-badge-row" }, badges)
        ),
        agyEl("div", { className: "agy-slot-uuid mono", text: slot.project_id || "—" }),
        agyEl(
          "div",
          { className: "agy-slot-kv" },
          agyEl(
            "div",
            { className: "agy-kv-pair" },
            agyEl("span", { className: "agy-kv-label", text: t("agySlots.colDaysSinceActive") }),
            agyEl("span", {
              className: "agy-kv-value mono",
              text: slot.days_since_active !== null && slot.days_since_active !== undefined
                ? t("agySlots.daysAgoValue", { days: String(slot.days_since_active) })
                : "—",
            })
          ),
          agyEl(
            "div",
            { className: "agy-kv-pair" },
            agyEl("span", { className: "agy-kv-label", text: t("agySlots.colGraceRemaining") }),
            agyEl("span", { className: "agy-kv-value mono", text: agyFormatHours(slot.grace_remaining_hours) })
          ),
          agyEl(
            "div",
            { className: "agy-kv-pair" },
            agyEl("span", { className: "agy-kv-label", text: t("agySlots.colBoundSessions") }),
            agyEl("span", { className: "agy-kv-value mono", text: boundText })
          ),
          agyEl(
            "div",
            { className: "agy-kv-pair" },
            agyEl("span", { className: "agy-kv-label", text: t("agySlots.colCoolingElapsed") }),
            agyEl("span", { className: "agy-kv-value mono", text: agyFormatHours(slot.cooling_elapsed_hours) })
          )
        )
      );

      if (healthAlert) {
        const alertLines = [];
        if (slot.project_file_exists === false) alertLines.push(t("agySlots.healthMissingFile"));
        if (slot.project_is_empty === false) alertLines.push(t("agySlots.healthNonEmpty"));
        cardBtn.appendChild(
          agyEl(
            "div",
            { className: "agy-health-callout", attrs: { "data-agy-health-alert": "1" } },
            agyEl("div", { className: "agy-health-callout-title", text: t("agySlots.healthAlertTitle") }),
            alertLines.map((line) => agyEl("div", { text: line }))
          )
        );
      }

      if (slot.guard_error) {
        cardBtn.appendChild(
          agyEl(
            "div",
            { className: "agy-notice agy-notice-danger", attrs: { "data-agy-guard-error": "1" } },
            agyEl("div", { className: "agy-notice-title", text: t("agySlots.guardErrorLabel") }),
            agyEl("div", { className: "mono small-note", text: String(slot.guard_error) })
          )
        );
      }

      if (slot.unconfirmed_operation_id) {
        cardBtn.appendChild(
          agyEl(
            "div",
            { className: "agy-notice agy-notice-warn", attrs: { "data-agy-unconfirmed-op": "1" } },
            agyEl("span", { text: `${t("agySlots.unconfirmedOpLabel")}: ` }),
            agyEl("span", { className: "mono", text: String(slot.unconfirmed_operation_id) })
          )
        );
      }

      grid.appendChild(cardBtn);
    }

    root.appendChild(grid);

    // Selected slot workbench
    const selected = agyFindSlot(data, state.agySelectedSlotName);
    if (!selected) return;

    const selStatus = agyNormalizeStatus(selected.status);
    const workbench = agyEl("div", {
      className: "agy-workbench",
      attrs: { id: "agy-slot-workbench", "data-selected-slot": selected.name, "data-selected-status": selStatus },
    });

    workbench.appendChild(
      agyEl(
        "div",
        { className: "agy-workbench-head" },
        agyEl(
          "div",
          null,
          agyEl(
            "div",
            { className: "agy-workbench-title" },
            agyEl("span", { text: t("agySlots.selectedSlotLabel", { slot: selected.name }) }),
            agyEl("span", { className: agyStatusBadgeClass(selStatus), text: agyStatusLabel(selStatus) })
          ),
          agyEl("div", { className: "muted small-note", text: agyStatusDesc(selStatus) })
        ),
        agyEl(
          "div",
          { className: "muted small-note mono" },
          `${t("agySlots.colLastActiveEnd")}: ${selected.last_active_end ? fmtTime(selected.last_active_end) : "—"}`
        )
      )
    );

    if (agyHasHealthAlert(selected)) {
      const lines = [];
      if (selected.project_file_exists === false) lines.push(t("agySlots.healthMissingFile"));
      if (selected.project_is_empty === false) lines.push(t("agySlots.healthNonEmpty"));
      workbench.appendChild(
        agyEl(
          "div",
          { className: "agy-health-callout", attrs: { "data-agy-workbench-health-alert": "1" } },
          agyEl("div", { className: "agy-health-callout-title", text: t("agySlots.healthAlertTitle") }),
          lines.map((line) => agyEl("div", { text: line }))
        )
      );
    }

    const selectedBlocked = renderAgyBlockedSessionsList(selected.blocked_sessions);
    if (selectedBlocked) workbench.appendChild(selectedBlocked);

    if (state.agyBusyMode !== null) {
      workbench.appendChild(
        agyEl("div", {
          id: "agy-inflight-banner",
          className: "agy-notice agy-notice-warn",
          text: t("agySlots.liveInflightBanner"),
        })
      );
    }

    if (state.agyInvalidated) {
      workbench.appendChild(
        agyEl("div", {
          id: "agy-invalidated-notice",
          className: "agy-notice agy-notice-warn",
          text: t("agySlots.invalidatedNotice"),
        })
      );
    }

    // Two-column action grid: Preview (dry_run) + Live Cleanup (4-condition gate)
    const canPreview = agyCanPreviewSlot(data, selected);
    const previewDisabledReason = agyPreviewDisabledReason(data, selected);

    const previewBox = agyEl(
      "div",
      { className: "agy-action-box" },
      agyEl("div", { className: "agy-action-box-title", text: t("agySlots.previewBtn") }),
      agyEl("div", { className: "muted small-note", text: t("agySlots.previewHint") }),
      agyEl("button", {
        type: "button",
        id: "btn-agy-preview",
        className: "btn btn-primary",
        disabled: !canPreview,
        text: state.agyBusyMode === "preview" ? t("agySlots.previewRunning") : t("agySlots.previewBtn"),
        on: { click: () => runAgyDryRunPreview() },
      }),
      previewDisabledReason
        ? agyEl("div", { className: "muted small-note", id: "agy-preview-disabled-reason", text: previewDisabledReason })
        : null
    );

    const confirmInput = agyEl("input", {
      type: "text",
      id: "agy-confirm-input",
      className: "input mono",
      value: state.agyConfirmText,
      placeholder: t("agySlots.confirmInputPlaceholder", { slot: selected.name }),
      disabled: state.agyBusyMode !== null,
      autocomplete: "off",
      spellcheck: false,
      attrs: { "aria-label": t("agySlots.confirmInputLabel") },
      on: {
        input: (ev) => {
          state.agyConfirmText = ev.target.value;
          state.agyMissingNotice = null;
          updateAgyLiveControls();
        },
        keydown: (ev) => {
          if (ev.key === "Enter") {
            ev.preventDefault();
            openAgyCleanupModal();
          }
        },
      },
    });

    const liveBox = agyEl(
      "div",
      { className: "agy-action-box agy-action-box-danger" },
      agyEl("div", { className: "agy-action-box-title danger-text", text: t("agySlots.liveSectionTitle") }),
      agyEl("div", { className: "small-note", text: t("agySlots.checklistTitle") }),
      agyEl("ul", { className: "agy-checklist", id: "agy-live-checklist" }),
      agyEl(
        "div",
        { className: "form-field" },
        agyEl("label", { attrs: { for: "agy-confirm-input" }, text: t("agySlots.confirmInputLabel") }),
        agyEl(
          "div",
          { className: "agy-confirm-row" },
          confirmInput,
          agyEl("button", {
            type: "button",
            id: "btn-agy-live-open",
            className: "btn btn-danger",
            disabled: true,
            text: t("agySlots.liveBtn"),
            on: { click: () => openAgyCleanupModal() },
          })
        )
      ),
      agyEl("div", { id: "agy-missing-conditions", className: "agy-notice agy-notice-warn hidden" })
    );

    workbench.appendChild(agyEl("div", { className: "agy-action-grid" }, previewBox, liveBox));

    // Structured receipt viewer(s)
    const receiptWrap = agyEl("div", { id: "agy-receipt-section", className: "agy-list-box" });
    if (state.agyLastAction && state.agyLastAction.slot === selected.name) {
      const currentReceipt = renderAgyReceiptCard(state.agyLastAction.payload, {
        httpStatus: state.agyLastAction.httpStatus,
        title: t("agySlots.receiptCurrentTitle"),
        isCurrent: true,
      });
      if (currentReceipt) receiptWrap.appendChild(currentReceipt);
    }
    const savedCleanup =
      selected.last_cleanup ||
      (data.last_cleanup && data.last_cleanup.slot === selected.name ? data.last_cleanup : null);
    if (savedCleanup) {
      const isDuplicateCurrent =
        state.agyLastAction &&
        state.agyLastAction.slot === selected.name &&
        state.agyLastAction.payload &&
        savedCleanup.operation_id &&
        state.agyLastAction.payload.operation_id === savedCleanup.operation_id &&
        state.agyLastAction.httpStatus === 200;
      if (!isDuplicateCurrent) {
        const savedReceipt = renderAgyReceiptCard(savedCleanup, {
          httpStatus: null,
          title: t("agySlots.receiptSavedTitle"),
          isCurrent: false,
        });
        if (savedReceipt) receiptWrap.appendChild(savedReceipt);
      }
    }
    if (receiptWrap.childNodes.length > 0) {
      workbench.appendChild(receiptWrap);
    }

    root.appendChild(workbench);
    updateAgyLiveControls();
  }

  // ==================== Navigation & Refresh ====================
  function switchTab(tabName) {
    state.activeTab = tabName;
    document.querySelectorAll(".tab").forEach((btn) => {
      const active = btn.getAttribute("data-tab") === tabName;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".tab-panel").forEach((panel) => {
      panel.classList.toggle("active", panel.id === `panel-${tabName}`);
    });
    refreshActiveTab({ autoPoll: false }).catch((e) => showToast(trBackend(e.message), true));
  }

  async function refreshActiveTab(opts = {}) {
    const autoPoll = Boolean(opts.autoPoll);
    if (state.activeTab === "stats") await loadSummary();
    else if (state.activeTab === "records") await loadRecords();
    else if (state.activeTab === "pricing") await loadPricing();
    else if (state.activeTab === "storage") {
      const tasks = [loadService(), loadStorage()];
      if (!autoPoll && !state.agyBusyMode) {
        tasks.push(loadAgySlots());
      }
      await Promise.all(tasks);
    }
  }

  async function refreshAll(firstLoad = false, opts = {}) {
    const autoPoll = Boolean(opts.autoPoll);
    state.meta = await apiGet("/api/meta");
    if (I18N && state.meta.locale && state.meta.locale !== I18N.getLocale() && !state.localeSwitching) {
      await I18N.setLocale(state.meta.locale);
    }
    syncModelsToMeta(firstLoad);
    renderMetaControls();
    await Promise.all([loadPricing(), refreshActiveTab({ autoPoll })]);
    if (state.drawerTaskId) {
      openDrawer(state.drawerTaskId);
    }
  }

  // ==================== Auto-Refresh (Revision Polling) ====================
  function isUserEditingOverlayOrForm() {
    if (state.agyBusyMode !== null) return true;
    const modals = ["modal-backdrop", "clear-modal-backdrop", "agy-cleanup-modal-backdrop", "info-modal-backdrop"];
    for (const id of modals) {
      const el = $(id);
      if (el && !el.classList.contains("hidden")) return true;
    }
    const active = document.activeElement;
    if (active && ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName)) {
      if (active.closest("#panel-storage") || active.closest("#panel-pricing")) return true;
    }
    return false;
  }

  function syncAutoRefreshUI(online = true) {
    const sel = $("auto-refresh-select");
    if (sel) sel.value = String(state.autoRefreshMs);
    const dot = $("live-poll-dot");
    if (dot) {
      dot.classList.toggle("active", Boolean(online && state.autoRefreshMs > 0 && !document.hidden));
    }
  }

  async function checkRevisionAndRefresh() {
    if (document.hidden || state.pollInFlight || state.autoRefreshMs <= 0) return;
    state.pollInFlight = true;
    try {
      const ver = await apiGet("/api/version");
      syncAutoRefreshUI(true);
      if (I18N && ver.locale && ver.locale !== I18N.getLocale() && !state.localeSwitching) {
        await applyLocaleAndRerender(ver.locale);
      }
      if (state.lastRevision === null) {
        state.lastRevision = ver.revision;
        return;
      }
      if (ver.revision !== state.lastRevision) {
        if (isUserEditingOverlayOrForm()) {
          // Defer reload until user closes modal / finishes form input.
          return;
        }
        state.lastRevision = ver.revision;
        await refreshAll(false, { autoPoll: true });
        if (ver.imported_receipts > 0) {
          showToast(t("toast.autoSyncedReceipts", { count: ver.imported_receipts }));
        }
      }
    } catch {
      syncAutoRefreshUI(false);
    } finally {
      state.pollInFlight = false;
    }
  }

  function startAutoRefresh() {
    clearInterval(state.autoRefreshTimer);
    state.autoRefreshTimer = null;
    syncAutoRefreshUI(true);
    if (state.autoRefreshMs > 0) {
      state.autoRefreshTimer = setInterval(() => {
        checkRevisionAndRefresh();
      }, state.autoRefreshMs);
    }
  }

  // ==================== Event Wiring ====================
  function wireEvents() {
    document.querySelectorAll(".tab").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.getAttribute("data-tab")));
    });
    $("btn-theme").addEventListener("click", toggleTheme);
    $("lang-switch")?.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-locale]");
      if (!btn) return;
      switchLocale(btn.getAttribute("data-locale"));
    });
    $("btn-refresh").addEventListener("click", () => {
      refreshAll(false)
        .then(() => showToast(t("toast.refreshed")))
        .catch((e) => showToast(trBackend(e.message), true));
    });
    $("auto-refresh-select").addEventListener("change", (ev) => {
      const ms = Number(ev.target.value);
      state.autoRefreshMs = [0, 5000, 10000, 30000].includes(ms) ? ms : 10000;
      saveRefreshMs(state.autoRefreshMs);
      startAutoRefresh();
      showToast(
        state.autoRefreshMs > 0
          ? t("toast.autoRefreshEnabled", { sec: state.autoRefreshMs / 1000 })
          : t("toast.autoRefreshDisabled")
      );
    });
    document.addEventListener("visibilitychange", () => {
      syncAutoRefreshUI(true);
      if (!document.hidden && state.autoRefreshMs > 0) {
        checkRevisionAndRefresh();
      }
    });

    // Agent pills
    $("stats-agent-pills").addEventListener("click", (ev) => {
      const btn = ev.target.closest(".pill");
      if (!btn) return;
      const agent = btn.getAttribute("data-agent");
      if (state.selectedAgents.has(agent)) {
        state.selectedAgents.delete(agent);
        removeModelsForAgent(agent);
      } else {
        state.selectedAgents.add(agent);
        selectAllModelsForAgent(agent);
      }
      renderAgentPills();
      renderModelPills();
      loadSummary().catch((e) => showToast(trBackend(e.message), true));
    });

    $("btn-agent-agy").addEventListener("click", () => {
      state.selectedAgents = new Set(state.meta?.default_agents || ["antigravity"]);
      state.selectedModels.clear();
      for (const a of state.selectedAgents) selectAllModelsForAgent(a);
      renderAgentPills();
      renderModelPills();
      loadSummary().catch((e) => showToast(trBackend(e.message), true));
    });

    $("btn-agent-all").addEventListener("click", () => {
      state.selectedAgents = new Set((state.meta?.agents || []).map((a) => a.name));
      state.selectedModels.clear();
      for (const a of state.selectedAgents) selectAllModelsForAgent(a);
      renderAgentPills();
      renderModelPills();
      loadSummary().catch((e) => showToast(trBackend(e.message), true));
    });

    $("btn-agent-clear").addEventListener("click", () => {
      state.selectedAgents.clear();
      state.selectedModels.clear();
      renderAgentPills();
      renderModelPills();
      loadSummary().catch((e) => showToast(trBackend(e.message), true));
    });

    // Model pills
    $("stats-model-pills").addEventListener("click", (ev) => {
      const btn = ev.target.closest(".pill");
      if (!btn) return;
      const pair = btn.getAttribute("data-pair");
      if (state.selectedModels.has(pair)) state.selectedModels.delete(pair);
      else state.selectedModels.add(pair);
      renderModelPills();
      loadSummary().catch((e) => showToast(trBackend(e.message), true));
    });

    $("stats-model-search").addEventListener("input", (ev) => {
      state.statsModelQuery = ev.target.value;
      renderModelPills();
    });

    $("btn-model-all").addEventListener("click", () => {
      for (const x of modelsForSelectedAgents()) state.selectedModels.add(x.pair);
      renderModelPills();
      loadSummary().catch((e) => showToast(trBackend(e.message), true));
    });

    $("btn-model-none").addEventListener("click", () => {
      for (const x of modelsForSelectedAgents()) state.selectedModels.delete(x.pair);
      renderModelPills();
      loadSummary().catch((e) => showToast(trBackend(e.message), true));
    });

    // Time range & cwd
    $("stats-range-group").addEventListener("click", (ev) => {
      const btn = ev.target.closest(".seg");
      if (!btn) return;
      state.statsRange = btn.getAttribute("data-range");
      $("stats-range-group").querySelectorAll(".seg").forEach((s) => s.classList.toggle("active", s === btn));
      $("stats-custom-range").classList.toggle("hidden", state.statsRange !== "custom");
      if (state.statsRange !== "custom") loadSummary().catch((e) => showToast(trBackend(e.message), true));
    });

    $("btn-stats-apply-range").addEventListener("click", () => {
      state.statsStart = $("stats-start").value;
      state.statsEnd = $("stats-end").value;
      loadSummary().catch((e) => showToast(trBackend(e.message), true));
    });

    $("stats-cwd-select").addEventListener("change", (ev) => {
      state.statsCwd = ev.target.value;
      loadSummary().catch((e) => showToast(trBackend(e.message), true));
    });

    $("chart-metric-group").addEventListener("click", (ev) => {
      const btn = ev.target.closest(".seg");
      if (!btn) return;
      state.chartMetric = btn.getAttribute("data-metric");
      $("chart-metric-group").querySelectorAll(".seg").forEach((s) => s.classList.toggle("active", s === btn));
      renderBarChart();
    });

    $("models-table-search").addEventListener("input", (ev) => {
      state.modelsTableQuery = ev.target.value;
      renderModelsTable();
    });

    $("models-table-body").addEventListener("click", (ev) => {
      const btn = ev.target.closest(".btn-quick-price");
      if (!btn) return;
      openPriceModal(btn.getAttribute("data-agent"), btn.getAttribute("data-model"));
    });

    // Records filters
    const reloadRecs = () => {
      state.recOffset = 0;
      loadRecords().catch((e) => showToast(trBackend(e.message), true));
    };
    $("rec-agent").addEventListener("change", (e) => { state.recAgent = e.target.value; reloadRecs(); });
    $("rec-status").addEventListener("change", (e) => { state.recStatus = e.target.value; reloadRecs(); });
    $("rec-range").addEventListener("change", (e) => { state.recRange = e.target.value; reloadRecs(); });
    $("rec-speed").addEventListener("change", (e) => { state.recSpeed = e.target.value; reloadRecs(); });
    $("rec-cwd").addEventListener("change", (e) => { state.recCwd = e.target.value; reloadRecs(); });

    let debounceTimer = null;
    const debouncedRecs = () => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(reloadRecs, 220);
    };
    $("rec-session").addEventListener("input", (e) => { state.recSession = e.target.value; debouncedRecs(); });
    $("rec-query").addEventListener("input", (e) => { state.recQuery = e.target.value; debouncedRecs(); });

    document.querySelectorAll(".col-toggle").forEach((cb) => {
      cb.addEventListener("change", () => {
        state.recCols[cb.getAttribute("data-col")] = cb.checked;
        saveCols();
        applyColumnVisibility();
      });
    });

    $("btn-rec-reset").addEventListener("click", () => {
      state.recAgent = "";
      state.recStatus = "all";
      state.recRange = "all";
      state.recSpeed = "all";
      state.recCwd = "";
      state.recSession = "";
      state.recQuery = "";
      state.recDay = "";
      state.recOffset = 0;
      $("rec-agent").value = "";
      $("rec-status").value = "all";
      $("rec-range").value = "all";
      $("rec-speed").value = "all";
      $("rec-cwd").value = "";
      $("rec-session").value = "";
      $("rec-query").value = "";
      loadRecords().catch((e) => showToast(trBackend(e.message), true));
    });

    $("records-table-body").addEventListener("click", (ev) => {
      const tr = ev.target.closest("tr[data-task-id]");
      if (!tr) return;
      openDrawer(tr.getAttribute("data-task-id"));
    });

    $("btn-rec-prev").addEventListener("click", () => {
      state.recOffset = Math.max(0, state.recOffset - state.recLimit);
      loadRecords().catch((e) => showToast(trBackend(e.message), true));
    });
    $("btn-rec-next").addEventListener("click", () => {
      state.recOffset += state.recLimit;
      loadRecords().catch((e) => showToast(trBackend(e.message), true));
    });

    // Drawer
    $("btn-drawer-close").addEventListener("click", closeDrawer);
    $("drawer-backdrop").addEventListener("click", closeDrawer);
    $("btn-drawer-copy").addEventListener("click", async () => {
      if (!state.drawerTaskId) return;
      try {
        await navigator.clipboard.writeText(state.drawerTaskId);
        showToast(t("toast.copiedTaskId"));
      } catch {
        showToast(t("toast.clipboardUnavailable"), true);
      }
    });

    // Pricing tab
    $("disc-mode").addEventListener("change", () => {
      $("disc-value").value = $("disc-mode").value === "quota_boost" ? "150" : "0.6667";
      updateDiscountPreview();
    });
    $("disc-value").addEventListener("input", updateDiscountPreview);

    $("form-add-discount").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        const mode = $("disc-mode").value;
        const val = Number($("disc-value").value);
        const rules = [...(state.pricing?.config?.harness_discounts || [])];
        const agent = $("disc-agent").value;
        const pattern = $("disc-pattern").value.trim() || "*";
        const label = $("disc-label").value.trim() || `${agent} ${pattern}`;
        rules.push({
          id: `user-${Date.now()}`,
          agent,
          model_pattern: pattern,
          mode,
          quota_boost_pct: mode === "quota_boost" ? val : 100,
          cost_multiplier: mode === "cost_multiplier" ? val : 1,
          enabled: true,
          label,
          preset: false,
        });
        state.pricing = await apiMutate("PUT", "/api/pricing", { harness_discounts: rules });
        $("disc-pattern").value = "";
        $("disc-label").value = "";
        renderPricing();
        showToast(t("toast.discountAdded"));
      } catch (err) {
        showToast(t("toast.saveFailed", { error: trBackend(err.message) }), true);
      }
    });

    $("discounts-table-body").addEventListener("change", async (ev) => {
      const cb = ev.target.closest(".disc-toggle");
      if (!cb) return;
      const idx = Number(cb.getAttribute("data-idx"));
      const rules = [...(state.pricing?.config?.harness_discounts || [])];
      if (!rules[idx]) return;
      rules[idx] = { ...rules[idx], enabled: cb.checked };
      try {
        state.pricing = await apiMutate("PUT", "/api/pricing", { harness_discounts: rules });
        renderPricing();
        showToast(t("toast.discountUpdated"));
      } catch (err) {
        showToast(t("toast.updateFailed", { error: trBackend(err.message) }), true);
      }
    });

    $("discounts-table-body").addEventListener("click", async (ev) => {
      const btn = ev.target.closest(".disc-delete");
      if (!btn) return;
      const idx = Number(btn.getAttribute("data-idx"));
      const rules = [...(state.pricing?.config?.harness_discounts || [])];
      rules.splice(idx, 1);
      try {
        state.pricing = await apiMutate("PUT", "/api/pricing", { harness_discounts: rules });
        renderPricing();
        showToast(t("toast.discountDeleted"));
      } catch (err) {
        showToast(t("toast.deleteFailed", { error: trBackend(err.message) }), true);
      }
    });

    $("pricing-view-group").addEventListener("click", (ev) => {
      const btn = ev.target.closest(".seg");
      if (!btn) return;
      state.pricingView = btn.getAttribute("data-view");
      $("pricing-view-group").querySelectorAll(".seg").forEach((s) => s.classList.toggle("active", s === btn));
      renderPricingTable();
    });

    $("pricing-search").addEventListener("input", (ev) => {
      state.pricingSearch = ev.target.value;
      renderPricingTable();
    });

    $("form-add-price").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        const key = $("price-new-key").value.trim();
        const rate = {
          input: Number($("price-new-in").value),
          output: Number($("price-new-out").value),
          cache_read: $("price-new-cr").value === "" ? 0 : Number($("price-new-cr").value),
          cache_write: $("price-new-cw").value === "" ? 0 : Number($("price-new-cw").value),
        };
        state.pricing = await apiMutate("PUT", "/api/pricing", { upsert_model: { key, rate } });
        $("price-new-key").value = "";
        $("price-new-in").value = "";
        $("price-new-out").value = "";
        $("price-new-cr").value = "";
        $("price-new-cw").value = "";
        await refreshAll(false);
        showToast(t("toast.priceSaved", { key }));
      } catch (err) {
        showToast(t("toast.saveFailed", { error: trBackend(err.message) }), true);
      }
    });

    $("pricing-table-body").addEventListener("click", (ev) => {
      const btn = ev.target.closest(".btn-edit-price-row");
      if (!btn) return;
      openPriceModal(btn.getAttribute("data-agent"), btn.getAttribute("data-key"));
    });

    $("btn-pricing-reset").addEventListener("click", async () => {
      if (!window.confirm(t("dialogs.confirmPricingReset"))) return;
      try {
        state.pricing = await apiMutate("POST", "/api/pricing/reset", {});
        await refreshAll(false);
        showToast(t("toast.pricingReset"));
      } catch (err) {
        showToast(t("toast.resetFailed", { error: trBackend(err.message) }), true);
      }
    });

    // Quick Pricing Modal
    $("modal-scope-agent").addEventListener("change", () => {
      if (!state.modalCtx) return;
      $("modal-key").value = $("modal-scope-agent").checked ? state.modalCtx.scopedKey : state.modalCtx.baseModel;
    });
    $("btn-modal-close").addEventListener("click", closePriceModal);
    $("btn-modal-cancel").addEventListener("click", closePriceModal);
    $("form-modal-price").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        const key = $("modal-key").value.trim();
        const rate = {
          input: Number($("modal-in").value),
          output: Number($("modal-out").value),
          cache_read: $("modal-cr").value === "" ? 0 : Number($("modal-cr").value),
          cache_write: $("modal-cw").value === "" ? 0 : Number($("modal-cw").value),
          note: $("modal-note").value.trim(),
        };
        state.pricing = await apiMutate("PUT", "/api/pricing", { upsert_model: { key, rate } });
        closePriceModal();
        await refreshAll(false);
        showToast(t("toast.priceUpdated", { key }));
      } catch (err) {
        showToast(t("toast.saveFailed", { error: trBackend(err.message) }), true);
      }
    });
    $("btn-modal-delete").addEventListener("click", async () => {
      const key = $("modal-key").value.trim();
      try {
        state.pricing = await apiMutate("PUT", "/api/pricing", { delete_model: key });
        closePriceModal();
        await refreshAll(false);
        showToast(t("toast.priceDeleted", { key }));
      } catch (err) {
        showToast(t("toast.deleteFailed", { error: trBackend(err.message) }), true);
      }
    });

    // Service & system settings (Management tab top section)
    $("form-service-settings").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        const portVal = Number($("svc-port").value);
        const payload = {
          auto_start: $("svc-autostart").checked,
          tray_enabled: $("svc-tray").checked,
          port: portVal,
        };
        const res = await apiMutate("PUT", "/api/service", payload);
        state.service = res.service;
        state.serviceStopped = false;
        renderService({ preserveInputs: false });
        showToast(res.message ? trBackend(res.message) : t("toast.serviceSaved"));
      } catch (err) {
        showToast(t("toast.saveFailed", { error: trBackend(err.message) }), true);
      }
    });

    $("btn-svc-restart").addEventListener("click", async () => {
      const btn = $("btn-svc-restart");
      btn.disabled = true;
      try {
        const res = await apiMutate("POST", "/api/service/restart", {});
        const targetUrl = res.url || window.location.origin;
        showToast(t("toast.restartingService", { url: targetUrl }));
        await waitForServiceReconnect(targetUrl);
      } catch (err) {
        btn.disabled = false;
        showToast(t("toast.restartFailed", { error: trBackend(err.message) }), true);
      }
    });

    // 看板更新暂不启用，原实现见 5c2596c。
    $("btn-svc-update").addEventListener("click", () => {
      openInfoModal({ titleKey: "dialogs.updateTitle", messageKey: "dialogs.updateMessage" });
    });

    $("btn-svc-folder").addEventListener("click", async () => {
      try {
        const res = await apiMutate("POST", "/api/service/open-folder", {});
        if (res.ok) showToast(t("toast.openedLogsFolder", { path: res.path }));
        else showToast(res.message ? trBackend(res.message) : t("toast.cannotOpenFolder"), true);
      } catch (err) {
        showToast(t("toast.openFolderFailed", { error: trBackend(err.message) }), true);
      }
    });

    $("btn-svc-stop").addEventListener("click", async () => {
      if (!window.confirm(t("dialogs.confirmServiceStop"))) return;
      try {
        await apiMutate("POST", "/api/service/stop", {});
        clearInterval(state.autoRefreshTimer);
        state.autoRefreshTimer = null;
        syncAutoRefreshUI(false);
        state.serviceStopped = true;
        const badge = $("svc-state-badge");
        badge.textContent = t("storage.stateStopped");
        badge.className = "badge badge-warn";
        openInfoModal({
          titleKey: "dialogs.stoppedTitle",
          messageKey: "dialogs.stoppedMessage",
          detailKey: "dialogs.stoppedDetail",
        });
      } catch (err) {
        showToast(t("toast.stopFailed", { error: trBackend(err.message) }), true);
      }
    });

    $("btn-info-close").addEventListener("click", closeInfoModal);
    $("btn-info-ok").addEventListener("click", closeInfoModal);

    // Storage tab
    $("st-enable-size").addEventListener("change", (ev) => { $("st-max-mb").disabled = !ev.target.checked; });
    $("st-enable-age").addEventListener("change", (ev) => { $("st-max-days").disabled = !ev.target.checked; });

    $("form-storage-policy").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        const payload = {
          max_mb: $("st-enable-size").checked ? Number($("st-max-mb").value) : null,
          max_age_days: $("st-enable-age").checked ? Number($("st-max-days").value) : null,
          auto_rotate: $("st-auto-rotate").checked,
        };
        const res = await apiMutate("PUT", "/api/storage", payload);
        state.storage = res.storage;
        renderStorage({ preserveInputs: false });
        showToast(t("toast.policySaved"));
      } catch (err) {
        showToast(t("toast.saveFailed", { error: trBackend(err.message) }), true);
      }
    });

    $("btn-storage-prune").addEventListener("click", async () => {
      try {
        const res = await apiMutate("POST", "/api/storage/prune", {});
        state.storage = res.storage;
        renderStorage({ preserveInputs: false });
        const r = res.result;
        if (r.changed) {
          showToast(t("toast.prunedResult", { age: r.removed_by_age, size: r.removed_by_size, bytes: fmtBytes(r.removed_bytes) }));
        } else {
          showToast(t("toast.prunedNone"));
        }
        await refreshAll(false);
      } catch (err) {
        showToast(t("toast.pruneFailed", { error: trBackend(err.message) }), true);
      }
    });

    $("btn-storage-backfill").addEventListener("click", async () => {
      try {
        const res = await apiMutate("POST", "/api/storage/backfill", {});
        state.storage = res.storage;
        renderStorage({ preserveInputs: false });
        const r = res.result;
        showToast(t("toast.backfillResult", { scanned: r.scanned, imported: r.imported, skipped: r.skipped_existing }));
        await refreshAll(false);
      } catch (err) {
        showToast(t("toast.backfillFailed", { error: trBackend(err.message) }), true);
      }
    });

    // Clear ledger confirmation modal
    const closeClearModal = () => {
      $("clear-confirm-input").value = "";
      $("clear-modal-backdrop").classList.add("hidden");
    };
    $("btn-storage-clear").addEventListener("click", () => {
      $("clear-confirm-input").value = "";
      $("clear-modal-backdrop").classList.remove("hidden");
      $("clear-confirm-input").focus();
    });
    $("btn-clear-close").addEventListener("click", closeClearModal);
    $("btn-clear-cancel").addEventListener("click", closeClearModal);
    $("form-clear-confirm").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const word = $("clear-confirm-input").value.trim();
      if (word !== "CLEAR") {
        showToast(t("toast.enterClearConfirm"), true);
        return;
      }
      try {
        const res = await apiMutate("POST", "/api/storage/clear", { confirm: "CLEAR" });
        state.storage = res.storage;
        closeClearModal();
        renderStorage({ preserveInputs: false });
        await refreshAll(false);
        showToast(
          t("toast.clearedResult", {
            records: fmtInt(res.result.cleared_records),
            bytes: fmtBytes(res.result.cleared_bytes),
          })
        );
      } catch (err) {
        showToast(t("toast.clearFailed", { error: trBackend(err.message) }), true);
      }
    });

    // AGY Slot Rotation & Cleanup controls
    $("btn-agy-slots-refresh")?.addEventListener("click", () => {
      loadAgySlots({ showToastOnSuccess: true });
    });
    $("btn-agy-modal-close")?.addEventListener("click", closeAgyCleanupModal);
    $("btn-agy-modal-cancel")?.addEventListener("click", closeAgyCleanupModal);
    $("form-agy-cleanup-confirm")?.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      await submitAgyLiveCleanup();
    });

    // Esc key closes overlays
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") {
        closeDrawer();
        closePriceModal();
        closeClearModal();
        closeAgyCleanupModal();
        closeInfoModal();
      }
    });
  }

  window.AbDashboardDailyChart = {
    enumerateIsoDates,
    buildDailyChartSlots,
    shouldShowDailyLabel,
    renderDailyBarInner,
    renderDailyBarColumn,
    buildDailyTooltipHtml,
    renderDailyBarChart,
  };

  window.AbDashboardAgySlots = {
    agyNormalizeStatus,
    agyFormatTriState,
    agyLiveCleanupAvailable,
    agyCanPreviewSlot,
    agyIsValidPreviewForSlot,
    agyEvaluateLiveConditions,
    agyIsFullRoundCompleted,
    agyExtractBatchRows,
    loadAgySlots,
    renderAgySlots,
    selectAgySlot,
    runAgyDryRunPreview,
    openAgyCleanupModal,
    closeAgyCleanupModal,
    submitAgyLiveCleanup,
    invalidateAgyConfirmState,
    getState: () => ({
      slots: state.agySlots,
      selectedSlotName: state.agySelectedSlotName,
      confirmText: state.agyConfirmText,
      validPreview: state.agyValidPreview,
      invalidated: state.agyInvalidated,
      busyMode: state.agyBusyMode,
      lastAction: state.agyLastAction,
      modalOpen: state.agyModalOpen,
      modalTarget: state.agyModalTarget,
    }),
  };

  // ==================== Boot ====================
  if (!window.__AB_DISABLE_AUTO_BOOT__) {
    initTheme();
    wireEvents();
    syncAutoRefreshUI(true);
    (async () => {
      if (I18N) {
        await I18N.init(INITIAL_LOCALE);
      } else {
        document.documentElement.setAttribute("data-i18n-ready", "1");
      }
      updateDiscountPreview();
      try {
        await refreshAll(true);
        const v = await apiGet("/api/version").catch(() => null);
        if (v && v.revision) {
          state.lastRevision = v.revision;
        }
      } catch (err) {
        showToast(t("toast.initFailed", { error: trBackend(err.message) }), true);
      } finally {
        startAutoRefresh();
      }
    })();
  }
})();
