(() => {
  "use strict";

  const CSRF_TOKEN = document.querySelector('meta[name="ab-csrf"]')?.getAttribute("content") || "";
  const COL_STORAGE_KEY = "hh.billing.records.cols.v1";
  const THEME_STORAGE_KEY = "hh.billing.theme.v1";
  const DEFAULT_COLS = { effort: true, tokens: true, rate: true, cost: true, duration: true };

  const state = {
    activeTab: "stats",
    meta: null,
    summary: null,
    pricing: null,
    storage: null,
    records: null,
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
    recTimer: null,
    pricingView: "observed", // observed | unpriced | user | presets
    pricingSearch: "",
    drawerTaskId: null,
    modalCtx: null,
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
    return Number(n).toLocaleString("en-US");
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
    const map = {
      user: ["用户手动", "badge-accent"],
      user_derived: ["用户派生", "badge-accent"],
      preset: ["OpenCodex 预置", "badge-ok"],
      preset_derived: ["预置派生", "badge-ok"],
      unpriced: ["未定价", "badge-warn"],
    };
    const [label, cls] = map[src] || ["未定价", "badge-warn"];
    return `<span class="badge ${cls}">${esc(label)}</span>`;
  }

  function statusBadge(status) {
    if (status === "completed") return `<span class="badge badge-ok">completed</span>`;
    if (status === "failed") return `<span class="badge badge-danger">failed</span>`;
    if (status === "cancelled") return `<span class="badge badge-warn">cancelled</span>`;
    return `<span class="badge">${esc(status || "unknown")}</span>`;
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
    $("brand-meta").textContent =
      `v${state.meta.version} · 本地循环地址 · 仅元数据（不含对话正文）· 账本 ${fmtInt(state.meta.total_records)} 条 · USD`;

    // Populate cwd dropdowns
    const cwds = state.meta.cwds || [];
    for (const selId of ["stats-cwd-select", "rec-cwd"]) {
      const sel = $(selId);
      const prev = sel.value;
      sel.innerHTML = `<option value="">全部目录 (${cwds.length})</option>` +
        cwds.map((c) => `<option value="${esc(c)}">${esc(shortPath(c))}</option>`).join("");
      if (cwds.includes(prev)) sel.value = prev;
    }

    // Populate records agent dropdown and discount agent dropdown
    const recAgentSel = $("rec-agent");
    const prevRecAgent = recAgentSel.value;
    recAgentSel.innerHTML = `<option value="">全部 Agent</option>` +
      state.meta.agents.map((a) => `<option value="${esc(a.name)}">${esc(a.name)} (${a.tasks})</option>`).join("");
    recAgentSel.value = prevRecAgent;

    const discAgentSel = $("disc-agent");
    const prevDiscAgent = discAgentSel.value;
    discAgentSel.innerHTML = state.meta.agents
      .map((a) => `<option value="${esc(a.name)}">${esc(a.name)}</option>`)
      .join("") + `<option value="*">* (全部 Agent)</option>`;
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
        return `<button type="button" class="pill ${sel ? "selected" : ""}" data-agent="${esc(a.name)}">
          <span>${esc(a.name)}</span>
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
      ? available.filter((x) => x.model.toLowerCase().includes(q) || x.agent.toLowerCase().includes(q))
      : available;
    const selectedCount = available.filter((x) => state.selectedModels.has(x.pair)).length;
    $("stats-model-count").textContent = available.length ? `(已选 ${selectedCount}/${available.length})` : "(当前 Agent 暂无模型记录)";

    if (!available.length) {
      wrap.innerHTML = `<span class="muted">当前所选 Agent 尚未在账本或定价表中出现模型</span>`;
      return;
    }
    const multiAgent = state.selectedAgents.size > 1;
    wrap.innerHTML = visible
      .map((x) => {
        const sel = state.selectedModels.has(x.pair);
        const label = multiAgent ? `${x.agent} / ${x.model}` : x.model;
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
        banner.innerHTML = `<span>当前筛选下暂无记录，但账本中共有 ${fmtInt(state.meta.total_records)} 条记录（${esc(nonEmpty.map((a) => `${a.name}:${a.tasks}`).join(" / "))}）。</span>
          <button type="button" class="btn btn-primary btn-sm" id="btn-show-nonempty">切换到有数据的 Agent</button>`;
        banner.classList.remove("hidden");
        $("btn-show-nonempty")?.addEventListener("click", () => {
          state.selectedAgents = new Set(nonEmpty.map((a) => a.name));
          state.selectedModels.clear();
          for (const a of state.selectedAgents) selectAllModelsForAgent(a);
          renderAgentPills();
          renderModelPills();
          loadSummary().catch((e) => showToast(e.message, true));
        });
      } else {
        banner.classList.add("hidden");
      }
    } else {
      banner.classList.add("hidden");
    }

    // KPI Cards
    const covText = ov.coverage_ratio !== null ? `已计量 ${ov.measured_tasks}/${ov.tasks} (${fmtPct(ov.coverage_ratio)})` : "暂无任务";
    const unpricedNote = ov.unpriced_tasks > 0 ? `已排除 ${ov.unpriced_tasks} 条未定价` : "全部已计价";
    const listDiff =
      ov.list_cost_usd !== null && ov.effective_cost_usd !== null && Math.abs(ov.list_cost_usd - ov.effective_cost_usd) > 1e-6
        ? `原价 ${fmtUsd(ov.list_cost_usd)} · `
        : "";
    const hitSub =
      ov.cache_hit_rate !== null
        ? `综合命中率 ${fmtPct(ov.cache_hit_rate)} · 缓存写入 ${fmtCompact(ov.cache_write_tokens)}`
        : "所选记录未上报缓存明细";
    const outSub =
      ov.reasoning_tokens > 0
        ? `含推理 ${fmtCompact(ov.reasoning_tokens)}${ov.tok_per_sec ? ` · ${ov.tok_per_sec} tok/s` : ""}`
        : ov.tok_per_sec
          ? `端到端 ${ov.tok_per_sec} tok/s (${ov.tps_samples} 样本)`
          : "—";

    $("stats-kpis").innerHTML = `
      <div class="kpi-card">
        <div class="kpi-label">任务总数</div>
        <div class="kpi-value">${fmtInt(ov.tasks)}</div>
        <div class="kpi-sub">${esc(covText)} · 活跃 ${ov.active_days} 天</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Token 总数</div>
        <div class="kpi-value">${fmtCompact(ov.total_tokens)}</div>
        <div class="kpi-sub mono">${fmtInt(ov.total_tokens)}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">输入 (净非缓存)</div>
        <div class="kpi-value">${fmtCompact(ov.uncached_input_tokens)}</div>
        <div class="kpi-sub">总输入含缓存 ${fmtCompact(ov.input_tokens)}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">缓存命中</div>
        <div class="kpi-value">${ov.cache_read_tokens !== null ? fmtCompact(ov.cache_read_tokens) : "—"}</div>
        <div class="kpi-sub">${esc(hitSub)}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">输出 Token</div>
        <div class="kpi-value">${fmtCompact(ov.output_tokens)}</div>
        <div class="kpi-sub">${esc(outSub)}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">API 标价折算 (USD)</div>
        <div class="kpi-value">${fmtUsd(ov.effective_cost_usd)}</div>
        <div class="kpi-sub">${esc(listDiff + unpricedNote)}</div>
      </div>
    `;

    renderBarChart();
    renderModelsTable();
    renderAgentsTable();
  }

  function renderBarChart() {
    const daily = state.summary?.daily || [];
    const stage = $("chart-stage");
    const legend = $("chart-legend");
    const metric = state.chartMetric;

    if (metric === "tokens") {
      legend.innerHTML = `
        <span class="legend-item"><span class="legend-dot" style="background:var(--bar-in)"></span>输入 (净)</span>
        <span class="legend-item"><span class="legend-dot" style="background:var(--bar-cache)"></span>缓存命中</span>
        <span class="legend-item"><span class="legend-dot" style="background:var(--bar-out)"></span>输出</span>`;
    } else if (metric === "cost") {
      legend.innerHTML = `<span class="legend-item"><span class="legend-dot" style="background:var(--bar-cost)"></span>折算费用 (USD)</span>`;
    } else {
      legend.innerHTML = `<span class="legend-item"><span class="legend-dot" style="background:var(--accent)"></span>任务数</span>`;
    }

    if (!daily.length) {
      stage.innerHTML = `<div class="chart-empty">当前筛选范围内没有可绘制的每日数据</div>`;
      return;
    }

    const values = daily.map((d) =>
      metric === "tokens" ? d.total_tokens : metric === "cost" ? d.effective_cost_usd : d.tasks
    );
    const maxVal = Math.max(...values, 0);
    const safeMax = maxVal > 0 ? maxVal : 1;
    const fmtAxis = (v) => (metric === "cost" ? fmtUsd(v) : metric === "tasks" ? String(Math.round(v)) : fmtCompact(v));

    const gridLines = [1, 0.75, 0.5, 0.25, 0]
      .map(
        (ratio) => `<div class="chart-grid-line"><span class="chart-grid-label">${esc(fmtAxis(safeMax * ratio))}</span></div>`
      )
      .join("");

    const labelStep = Math.max(1, Math.ceil(daily.length / 10));
    const barsHtml = daily
      .map((d, idx) => {
        const val = values[idx];
        const totalPct = maxVal > 0 ? Math.max(val > 0 ? 2 : 0, (val / safeMax) * 100) : 0;
        let inner = "";
        if (metric === "tokens") {
          const denom = d.uncached_input_tokens + d.cache_read_tokens + d.output_tokens;
          if (denom > 0) {
            const pIn = (d.uncached_input_tokens / denom) * 100;
            const pCache = (d.cache_read_tokens / denom) * 100;
            const pOut = Math.max(0, 100 - pIn - pCache);
            inner = `
              <div class="bar-seg-in" style="height:${pIn.toFixed(2)}%"></div>
              <div class="bar-seg-cache" style="height:${pCache.toFixed(2)}%"></div>
              <div class="bar-seg-out" style="height:${pOut.toFixed(2)}%"></div>`;
          } else {
            inner = `<div class="bar-seg-in" style="height:100%"></div>`;
          }
        } else if (metric === "cost") {
          inner = `<div class="bar-seg-cost" style="height:100%"></div>`;
        } else {
          inner = `<div class="bar-seg-tasks" style="height:100%"></div>`;
        }
        const showLabel = idx % labelStep === 0 || idx === daily.length - 1;
        return `<button type="button" class="bar-col" data-idx="${idx}" data-date="${esc(d.date)}" aria-label="${esc(d.date)}">
          <div class="bar-stack" style="height:${totalPct.toFixed(2)}%">${inner}</div>
          ${showLabel ? `<span class="bar-date">${esc(d.date.slice(5))}</span>` : ""}
        </button>`;
      })
      .join("");

    stage.innerHTML = `<div class="chart-grid">${gridLines}</div><div class="chart-bars">${barsHtml}</div>`;

    const tooltip = $("chart-tooltip");
    stage.querySelectorAll(".bar-col").forEach((btn) => {
      const idx = Number(btn.getAttribute("data-idx"));
      const d = daily[idx];
      btn.addEventListener("pointerenter", (ev) => {
        tooltip.innerHTML = `
          <div class="mono" style="font-weight:650;margin-bottom:4px">${esc(d.date)}</div>
          <div>任务：<b>${fmtInt(d.tasks)}</b>（已计量 ${fmtInt(d.measured_tasks)}）</div>
          <div>总 Token：<b>${fmtInt(d.total_tokens)}</b></div>
          <div>输入(净)：${fmtInt(d.uncached_input_tokens)} · 缓存：${fmtInt(d.cache_read_tokens)}</div>
          <div>输出：${fmtInt(d.output_tokens)}</div>
          <div>折算费用：<b>${d.priced_tasks > 0 ? fmtUsd(d.effective_cost_usd) : "未定价"}</b>${d.unpriced_tasks > 0 ? ` (排 ${d.unpriced_tasks})` : ""}</div>`;
        tooltip.classList.remove("hidden");
        positionTooltip(ev);
      });
      btn.addEventListener("pointermove", positionTooltip);
      btn.addEventListener("pointerleave", () => tooltip.classList.add("hidden"));
      btn.addEventListener("click", () => {
        tooltip.classList.add("hidden");
        state.recDay = d.date;
        state.recOffset = 0;
        if (state.selectedAgents.size === 1) {
          state.recAgent = Array.from(state.selectedAgents)[0];
          $("rec-agent").value = state.recAgent;
        }
        switchTab("records");
      });
    });
  }

  function positionTooltip(ev) {
    const tooltip = $("chart-tooltip");
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
      ? rows.filter((r) => r.model.toLowerCase().includes(q) || r.agent.toLowerCase().includes(q))
      : rows;
    $("models-table-count").textContent = String(filtered.length);

    if (!filtered.length) {
      body.innerHTML = `<tr><td colspan="13" class="muted" style="text-align:center;padding:24px">暂无匹配的模型统计记录</td></tr>`;
      return;
    }

    body.innerHTML = filtered
      .map((r) => {
        const discBadge =
          r.discount_multiplier !== 1
            ? ` <span class="badge badge-accent" title="${esc(r.discount_label || "")}">×${Number(r.discount_multiplier).toFixed(3)}</span>`
            : "";
        const costCell =
          r.effective_cost_usd !== null
            ? `<span title="原价 ${fmtUsd(r.list_cost_usd)}">${fmtUsd(r.effective_cost_usd)}</span>${discBadge}`
            : `<span class="badge badge-warn">未定价</span>`;
        return `<tr>
          <td class="mono" title="${esc(r.model)}">${esc(r.model)}</td>
          <td><span class="badge">${esc(r.agent)}</span></td>
          <td>
            <span class="share-bar"><span class="share-fill" style="width:${(r.share_ratio * 100).toFixed(1)}%"></span></span>
            <span class="num muted">${fmtPct(r.share_ratio)}</span>
          </td>
          <td class="num">${fmtInt(r.tasks)} <span class="muted">(${fmtInt(r.measured_tasks)})</span></td>
          <td class="num">${fmtCompact(r.uncached_input_tokens)}</td>
          <td class="num">${r.cache_read_tokens !== null ? fmtCompact(r.cache_read_tokens) : "—"}</td>
          <td class="num">${r.cache_write_tokens > 0 ? fmtCompact(r.cache_write_tokens) : "—"}</td>
          <td class="num">${fmtCompact(r.output_tokens)}</td>
          <td class="num">${fmtPct(r.cache_hit_rate)}</td>
          <td class="num">${r.tok_per_sec !== null ? `${r.tok_per_sec}` : "—"}</td>
          <td class="num">${fmtCompact(r.total_tokens)}</td>
          <td class="num">${costCell}</td>
          <td class="num">
            <button type="button" class="btn btn-ghost btn-sm btn-quick-price"
              data-agent="${esc(r.agent)}" data-model="${esc(r.model)}">定价</button>
          </td>
        </tr>`;
      })
      .join("");
  }

  function renderAgentsTable() {
    const body = $("agents-table-body");
    const rows = state.summary?.agents || [];
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="10" class="muted" style="text-align:center;padding:18px">暂无数据</td></tr>`;
      return;
    }
    body.innerHTML = rows
      .map(
        (a) => `<tr>
        <td><span class="badge badge-accent">${esc(a.agent)}</span></td>
        <td>
          <span class="share-bar"><span class="share-fill" style="width:${(a.share_ratio * 100).toFixed(1)}%"></span></span>
          <span class="num muted">${fmtPct(a.share_ratio)}</span>
        </td>
        <td class="num">${fmtInt(a.tasks)}</td>
        <td class="num">${fmtCompact(a.uncached_input_tokens)}</td>
        <td class="num">${fmtCompact(a.cache_read_tokens)}</td>
        <td class="num">${fmtCompact(a.output_tokens)}</td>
        <td class="num">${fmtPct(a.cache_hit_rate)}</td>
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
      ? `<span class="badge badge-accent">已锁定日期：${esc(state.recDay)} <button type="button" class="btn-link" id="btn-clear-day">×</button></span>`
      : `<span class="muted">点击任意行查看任务详情抽屉（仅元数据，不含正文）</span>`;
    $("btn-clear-day")?.addEventListener("click", () => {
      state.recDay = "";
      state.recOffset = 0;
      loadRecords().catch((e) => showToast(e.message, true));
    });

    const st = state.records.scope_totals;
    const exclParts = [];
    if (st.unpriced_tasks > 0) exclParts.push(`${st.unpriced_tasks} 条未定价`);
    if (st.unmeasured_tasks > 0) exclParts.push(`${st.unmeasured_tasks} 条未计量`);
    $("rec-scope-bar").innerHTML = `
      <div>当前筛选合计：<b>${fmtInt(st.tasks)}</b> 次任务 · <b>${fmtCompact(st.total_tokens)}</b> tokens · <b>${fmtUsd(st.effective_cost_usd)}</b>${exclParts.length ? ` <span class="muted">（已排除 ${esc(exclParts.join("、"))}）</span>` : ""}</div>
      <div class="muted">显示第 ${state.records.total ? state.records.offset + 1 : 0}–${Math.min(state.records.total, state.records.offset + state.records.items.length)} / 共 ${fmtInt(state.records.total)} 条</div>
    `;

    const body = $("records-table-body");
    if (!state.records.items.length) {
      body.innerHTML = `<tr><td colspan="10" class="muted" style="text-align:center;padding:24px">没有匹配的任务记录</td></tr>`;
    } else {
      body.innerHTML = state.records.items
        .map((item) => {
          const t = item.tokens;
          const c = item.cost;
          let tokHtml = `<span class="muted">未上报</span>`;
          if (t.measured) {
            const parts = [];
            parts.push(`<span title="净输入">${fmtCompact(t.uncached_input_tokens)}</span>`);
            if (t.cache_reported && t.cache_read_tokens !== null) {
              parts.push(`<span class="tok-c" title="缓存命中">c:${fmtCompact(t.cache_read_tokens)}</span>`);
            }
            if (t.cache_write_tokens > 0) {
              parts.push(`<span class="tok-w" title="缓存写入">w:${fmtCompact(t.cache_write_tokens)}</span>`);
            }
            parts.push(`<span title="输出">→${fmtCompact(t.output_tokens)}</span>`);
            if (t.reasoning_tokens > 0) {
              parts.push(`<span class="tok-r" title="含推理 Token">r:${fmtCompact(t.reasoning_tokens)}</span>`);
            }
            tokHtml = `<div class="tok-chips">${parts.join("")}</div>`;
          }
          const modelNote =
            item.observed_model && item.model && item.observed_model !== item.model
              ? ` <span class="badge badge-warn" title="请求 ${esc(item.model)} → 实测 ${esc(item.observed_model)}">实测</span>`
              : "";
          const deltaBadge = t.delta_from_conversation
            ? ` <span class="badge" title="由 Antigravity 续接会话累计值做差换算">Δ</span>`
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
            <td><span class="badge">${esc(item.agent)}</span></td>
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

    $("records-pager-info").textContent = `第 ${Math.floor(state.records.offset / state.records.limit) + 1} 页 · 每页 ${state.records.limit} 条`;
    $("btn-rec-prev").disabled = state.records.offset <= 0;
    $("btn-rec-next").disabled = state.records.offset + state.records.limit >= state.records.total;
    applyColumnVisibility();
  }

  async function openDrawer(taskId) {
    try {
      const res = await apiGet(`/api/records/${encodeURIComponent(taskId)}`);
      state.drawerTaskId = taskId;
      renderDrawer(res.record);
      $("drawer-backdrop").classList.remove("hidden");
      $("detail-drawer").classList.remove("hidden");
    } catch (err) {
      showToast(`加载详情失败：${err.message}`, true);
    }
  }

  function closeDrawer() {
    state.drawerTaskId = null;
    $("drawer-backdrop").classList.add("hidden");
    $("detail-drawer").classList.add("hidden");
  }

  function renderDrawer(r) {
    $("drawer-task-id").textContent = r.task_id;
    const t = r.tokens;
    const c = r.cost;
    const scopeNote = t.delta_from_conversation
      ? t.delta_reset_warning
        ? "conversation（检测到计数器回退，已兜底取本轮快照）"
        : "conversation（已自动减去同会话上一轮累计值）"
      : r.usage_scope;

    let costSection = "";
    if (c.priced) {
      costSection = `
        <div class="kv-grid">
          <div class="kv-k">匹配价格键</div><div class="kv-v mono">${esc(c.matched_price_key)} ${priceSourceBadge(c.price_source)}</div>
          <div class="kv-k">单价 ($/1M)</div><div class="kv-v mono">输入 ${fmtRate(c.rate_input)} · 输出 ${fmtRate(c.rate_output)} · 缓存命中 ${fmtRate(c.rate_cache_read)} · 写入 ${fmtRate(c.rate_cache_write)}</div>
          <div class="kv-k">分项费用</div><div class="kv-v mono">输入 ${fmtUsd(c.input_cost_usd)} + 缓存 ${fmtUsd(c.cache_read_cost_usd)} + 写入 ${fmtUsd(c.cache_write_cost_usd)} + 输出 ${fmtUsd(c.output_cost_usd)}</div>
          <div class="kv-k">API 标价原价</div><div class="kv-v mono">${fmtUsd(c.list_cost_usd)}</div>
          <div class="kv-k">Harness 折扣</div><div class="kv-v">${c.discount_label ? `${esc(c.discount_label)}（额度 ${c.discount_quota_boost_pct}% · 倍率 ×${c.discount_multiplier}）` : "无折扣（×1.0）"}</div>
          <div class="kv-k">折算计费 (USD)</div><div class="kv-v mono"><b>${fmtUsd(c.effective_cost_usd)}</b></div>
        </div>`;
    } else {
      const reasons = {
        usage_missing: "该任务未上报 Token usage",
        usage_breakdown_missing: "仅上报了 total_tokens，缺少输入/输出分项",
        price_unmatched: "未匹配到该模型单价，请点击下方按钮手动设定",
      };
      costSection = `<div class="muted">${esc(reasons[c.unavailable_reason] || c.unavailable_reason || "无法计价")}</div>`;
    }

    const alertsHtml =
      r.error || (r.warnings && r.warnings.length)
        ? `<div class="detail-sec">
            <h4>错误与告警摘要</h4>
            ${r.error ? `<div class="danger-text mono" style="font-size:12px;margin-bottom:6px">${esc(r.error)}</div>` : ""}
            ${(r.warnings || []).map((w) => `<div class="muted mono" style="font-size:12px">• ${esc(w)}</div>`).join("")}
          </div>`
        : "";

    $("drawer-body").innerHTML = `
      <div class="detail-sec">
        <h4>基本信息</h4>
        <div class="kv-grid">
          <div class="kv-k">Agent / Harness</div><div class="kv-v"><span class="badge badge-accent">${esc(r.agent)}</span></div>
          <div class="kv-k">状态</div><div class="kv-v">${statusBadge(r.status)} ${r.stop_reason ? `<span class="muted mono">(${esc(r.stop_reason)})</span>` : ""}</div>
          <div class="kv-k">模型</div><div class="kv-v mono">${esc(r.effective_model)}${r.model && r.observed_model && r.model !== r.observed_model ? ` <span class="muted">(请求 ${esc(r.model)})</span>` : ""}</div>
          <div class="kv-k">推理强度</div><div class="kv-v mono">${esc(r.observed_effort || r.effort || "—")}</div>
          <div class="kv-k">Session ID</div><div class="kv-v mono">${esc(r.session_id)}</div>
          <div class="kv-k">原生会话 ID</div><div class="kv-v mono">${esc(r.native_session_id || "—")}</div>
          <div class="kv-k">项目目录 (cwd)</div><div class="kv-v mono">${esc(r.cwd || "—")}</div>
          <div class="kv-k">标题</div><div class="kv-v">${esc(r.title || "—")}</div>
          <div class="kv-k">文件改动数</div><div class="kv-v mono">${fmtInt(r.files_changed_total)}${r.files_outside_cwd_total > 0 ? `（工作区外 ${r.files_outside_cwd_total}）` : ""}</div>
        </div>
      </div>

      <div class="detail-sec">
        <h4>性能与时间</h4>
        <div class="kv-grid">
          <div class="kv-k">开始时间</div><div class="kv-v mono">${esc(fmtTime(r.started_at || r.created_at))}</div>
          <div class="kv-k">结束时间</div><div class="kv-v mono">${esc(fmtTime(r.finished_at))}</div>
          <div class="kv-k">总耗时</div><div class="kv-v mono">${esc(fmtDuration(r.duration_ms))}</div>
          <div class="kv-k">端到端输出</div><div class="kv-v mono">${r.tok_per_sec !== null ? `${r.tok_per_sec} tok/s` : "—"}</div>
        </div>
      </div>

      <div class="detail-sec">
        <h4>Token 归一化明细</h4>
        <div class="kv-grid">
          <div class="kv-k">总 Token</div><div class="kv-v mono"><b>${fmtInt(t.total_tokens)}</b></div>
          <div class="kv-k">总输入（含缓存）</div><div class="kv-v mono">${fmtInt(t.input_tokens)}</div>
          <div class="kv-k">输入（净非缓存）</div><div class="kv-v mono">${fmtInt(t.uncached_input_tokens)}</div>
          <div class="kv-k">缓存命中 (c)</div><div class="kv-v mono">${t.cache_reported ? `${fmtInt(t.cache_read_tokens)} (${fmtPct(r.cache_hit_rate)})` : "未上报"}</div>
          <div class="kv-k">缓存写入 (w)</div><div class="kv-v mono">${t.cache_write_tokens !== null ? fmtInt(t.cache_write_tokens) : "—"}</div>
          <div class="kv-k">输出（含推理）</div><div class="kv-v mono">${fmtInt(t.output_tokens)}</div>
          <div class="kv-k">其中推理/思考</div><div class="kv-v mono">${fmtInt(t.reasoning_tokens)}</div>
          <div class="kv-k">统计口径</div><div class="kv-v">${esc(scopeNote)}</div>
        </div>
      </div>

      <div class="detail-sec">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <h4 style="margin:0">API 标价折算 (USD)</h4>
          <button type="button" class="btn btn-ghost btn-sm" id="btn-drawer-edit-price">调整此模型定价</button>
        </div>
        ${costSection}
      </div>

      ${alertsHtml}

      <div class="detail-sec">
        <h4>原始 usage 元数据（无正文）</h4>
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
      body.innerHTML = `<tr><td colspan="8" class="muted" style="text-align:center;padding:18px">暂无 Harness 折扣规则（全部按原价 ×1.0 折算）</td></tr>`;
      return;
    }
    body.innerHTML = rules
      .map(
        (r, idx) => `<tr>
        <td><input type="checkbox" class="disc-toggle" data-idx="${idx}" ${r.enabled ? "checked" : ""} /></td>
        <td><span class="badge badge-accent">${esc(r.agent)}</span></td>
        <td class="mono">${esc(r.model_pattern || "*")}</td>
        <td>${r.mode === "quota_boost" ? "额度膨胀 %" : "计费倍率 ×"}</td>
        <td class="num">${Number(r.quota_boost_pct).toFixed(1)}%</td>
        <td class="num"><b>×${Number(r.cost_multiplier).toFixed(4)}</b></td>
        <td>${esc(r.label || "—")} ${r.preset ? '<span class="badge">预置</span>' : ""}</td>
        <td class="num"><button type="button" class="btn btn-ghost btn-sm disc-delete" data-idx="${idx}">删除</button></td>
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
          subLabel: `${m.agent} · ${m.tasks} 次任务`,
          source: m.source,
          rate: m.rate,
          discount: m.discount_multiplier !== 1 ? `${m.discount_label || ""} (×${Number(m.discount_multiplier).toFixed(3)})` : "—",
          hasUserOverride: Object.keys(userModels).some(
            (k) => k.toLowerCase() === m.model.toLowerCase() || k.toLowerCase() === `${m.agent}::${m.model}`.toLowerCase()
          ),
        }));
    } else if (view === "user") {
      rows = Object.entries(userModels).map(([k, rate]) => {
        const [ag, md] = k.includes("::") ? k.split("::", 2) : ["*", k];
        return {
          key: k,
          agent: ag === "*" ? "" : ag,
          subLabel: ag === "*" ? "全局模型覆盖" : `仅限 ${ag}`,
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
          subLabel: `参考归属：${p.agent_hint}`,
          source: hasOverride ? "user" : "preset",
          rate: effectiveRate,
          discount: "—",
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
      body.innerHTML = `<tr><td colspan="8" class="muted" style="text-align:center;padding:22px">暂无匹配的定价项</td></tr>`;
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
          <td class="muted" style="font-size:12px">${esc(r.discount)}</td>
          <td class="num">
            <button type="button" class="btn btn-ghost btn-sm btn-edit-price-row"
              data-agent="${esc(r.agent || "")}" data-key="${esc(r.exactKey || r.key)}">编辑</button>
          </td>
        </tr>`;
      })
      .join("");
  }

  function updateDiscountPreview() {
    const mode = $("disc-mode").value;
    const val = Number($("disc-value").value);
    $("disc-value-label").textContent = mode === "quota_boost" ? "额度百分比 (%)" : "计费倍率 (×)";
    if (!Number.isFinite(val) || val <= 0) {
      $("disc-preview").textContent = "无效";
      return;
    }
    if (mode === "quota_boost") {
      $("disc-preview").textContent = `等效 ×${(100 / val).toFixed(4)}`;
    } else {
      $("disc-preview").textContent = `等效额度 ${(100 / val).toFixed(1)}%`;
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
    $("modal-scope-label").textContent = `仅覆盖当前 Agent (${scopedAgent}::${baseModel})`;
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

  // ==================== Tab 4: Storage (磁盘管理) ====================
  async function loadStorage() {
    const res = await apiGet("/api/storage");
    state.storage = res.storage;
    renderStorage();
  }

  function renderStorage() {
    const st = state.storage;
    if (!st) return;
    const p = st.policy;
    const maxBytes = p.max_bytes;
    const pct = maxBytes ? Math.min(100, (st.ledger_bytes / maxBytes) * 100) : 0;
    const capText = maxBytes ? `${fmtBytes(st.ledger_bytes)} / ${fmtBytes(maxBytes)} (${pct.toFixed(1)}%)` : `${fmtBytes(st.ledger_bytes)}（不限体积）`;

    $("storage-kpis").innerHTML = `
      <div class="kpi-card">
        <div class="kpi-label">独立账本占用</div>
        <div class="kpi-value">${fmtBytes(st.ledger_bytes)}</div>
        <div class="kpi-sub">${esc(capText)}</div>
        ${maxBytes ? `<div class="progress-bar"><div class="progress-fill" style="width:${pct.toFixed(1)}%"></div></div>` : ""}
      </div>
      <div class="kpi-card">
        <div class="kpi-label">账本记录条数</div>
        <div class="kpi-value">${fmtInt(st.record_count)}</div>
        <div class="kpi-sub">${st.corrupt_lines > 0 ? `跳过异常行 ${st.corrupt_lines}` : "格式完好"} · 本机 receipts ${fmtInt(st.receipts_on_disk)} 个</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">最早记录时间</div>
        <div class="kpi-value" style="font-size:16px">${esc(fmtTime(st.earliest_ts))}</div>
        <div class="kpi-sub">保留策略：${p.max_age_days ? `${p.max_age_days} 天` : "不限天数"}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">最新记录时间</div>
        <div class="kpi-value" style="font-size:16px">${esc(fmtTime(st.latest_ts))}</div>
        <div class="kpi-sub">配置总占用 ${fmtBytes(st.total_billing_bytes)}</div>
      </div>
    `;

    $("st-enable-size").checked = p.max_bytes !== null;
    $("st-max-mb").disabled = p.max_bytes === null;
    $("st-max-mb").value = p.max_bytes !== null ? String(Math.max(1, Math.round(p.max_bytes / (1024 * 1024)))) : "100";

    $("st-enable-age").checked = p.max_age_days !== null;
    $("st-max-days").disabled = p.max_age_days === null;
    $("st-max-days").value = p.max_age_days !== null ? String(p.max_age_days) : "180";

    $("st-auto-rotate").checked = Boolean(p.auto_rotate);

    $("storage-paths").innerHTML = `
      <div>目录：${esc(st.billing_dir)}</div>
      <div>账本：${esc(st.ledger_path)}</div>
      <div>定价：${esc(st.pricing_path)}</div>
      <div>策略：${esc(st.storage_policy_path)}</div>
    `;
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
    refreshActiveTab().catch((e) => showToast(e.message, true));
  }

  async function refreshActiveTab() {
    if (state.activeTab === "stats") await loadSummary();
    else if (state.activeTab === "records") await loadRecords();
    else if (state.activeTab === "pricing") await loadPricing();
    else if (state.activeTab === "storage") await loadStorage();
  }

  async function refreshAll(firstLoad = false) {
    state.meta = await apiGet("/api/meta");
    syncModelsToMeta(firstLoad);
    renderMetaControls();
    await Promise.all([loadPricing(), refreshActiveTab()]);
    if (state.drawerTaskId) {
      openDrawer(state.drawerTaskId);
    }
  }

  // ==================== Event Wiring ====================
  function wireEvents() {
    document.querySelectorAll(".tab").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.getAttribute("data-tab")));
    });
    $("btn-theme").addEventListener("click", toggleTheme);
    $("btn-refresh").addEventListener("click", () => {
      refreshAll(false).then(() => showToast("已刷新")).catch((e) => showToast(e.message, true));
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
      loadSummary().catch((e) => showToast(e.message, true));
    });

    $("btn-agent-agy").addEventListener("click", () => {
      state.selectedAgents = new Set(state.meta?.default_agents || ["antigravity"]);
      state.selectedModels.clear();
      for (const a of state.selectedAgents) selectAllModelsForAgent(a);
      renderAgentPills();
      renderModelPills();
      loadSummary().catch((e) => showToast(e.message, true));
    });

    $("btn-agent-all").addEventListener("click", () => {
      state.selectedAgents = new Set((state.meta?.agents || []).map((a) => a.name));
      state.selectedModels.clear();
      for (const a of state.selectedAgents) selectAllModelsForAgent(a);
      renderAgentPills();
      renderModelPills();
      loadSummary().catch((e) => showToast(e.message, true));
    });

    $("btn-agent-clear").addEventListener("click", () => {
      state.selectedAgents.clear();
      state.selectedModels.clear();
      renderAgentPills();
      renderModelPills();
      loadSummary().catch((e) => showToast(e.message, true));
    });

    // Model pills
    $("stats-model-pills").addEventListener("click", (ev) => {
      const btn = ev.target.closest(".pill");
      if (!btn) return;
      const pair = btn.getAttribute("data-pair");
      if (state.selectedModels.has(pair)) state.selectedModels.delete(pair);
      else state.selectedModels.add(pair);
      renderModelPills();
      loadSummary().catch((e) => showToast(e.message, true));
    });

    $("stats-model-search").addEventListener("input", (ev) => {
      state.statsModelQuery = ev.target.value;
      renderModelPills();
    });

    $("btn-model-all").addEventListener("click", () => {
      for (const x of modelsForSelectedAgents()) state.selectedModels.add(x.pair);
      renderModelPills();
      loadSummary().catch((e) => showToast(e.message, true));
    });

    $("btn-model-none").addEventListener("click", () => {
      for (const x of modelsForSelectedAgents()) state.selectedModels.delete(x.pair);
      renderModelPills();
      loadSummary().catch((e) => showToast(e.message, true));
    });

    // Time range & cwd
    $("stats-range-group").addEventListener("click", (ev) => {
      const btn = ev.target.closest(".seg");
      if (!btn) return;
      state.statsRange = btn.getAttribute("data-range");
      $("stats-range-group").querySelectorAll(".seg").forEach((s) => s.classList.toggle("active", s === btn));
      $("stats-custom-range").classList.toggle("hidden", state.statsRange !== "custom");
      if (state.statsRange !== "custom") loadSummary().catch((e) => showToast(e.message, true));
    });

    $("btn-stats-apply-range").addEventListener("click", () => {
      state.statsStart = $("stats-start").value;
      state.statsEnd = $("stats-end").value;
      loadSummary().catch((e) => showToast(e.message, true));
    });

    $("stats-cwd-select").addEventListener("change", (ev) => {
      state.statsCwd = ev.target.value;
      loadSummary().catch((e) => showToast(e.message, true));
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
      loadRecords().catch((e) => showToast(e.message, true));
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

    $("rec-auto-refresh").addEventListener("change", (ev) => {
      clearInterval(state.recTimer);
      state.recTimer = null;
      if (ev.target.checked) {
        state.recTimer = setInterval(() => {
          if (state.activeTab === "records") loadRecords().catch(() => {});
        }, 10000);
      }
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
      loadRecords().catch((e) => showToast(e.message, true));
    });

    $("records-table-body").addEventListener("click", (ev) => {
      const tr = ev.target.closest("tr[data-task-id]");
      if (!tr) return;
      openDrawer(tr.getAttribute("data-task-id"));
    });

    $("btn-rec-prev").addEventListener("click", () => {
      state.recOffset = Math.max(0, state.recOffset - state.recLimit);
      loadRecords().catch((e) => showToast(e.message, true));
    });
    $("btn-rec-next").addEventListener("click", () => {
      state.recOffset += state.recLimit;
      loadRecords().catch((e) => showToast(e.message, true));
    });

    // Drawer
    $("btn-drawer-close").addEventListener("click", closeDrawer);
    $("drawer-backdrop").addEventListener("click", closeDrawer);
    $("btn-drawer-copy").addEventListener("click", async () => {
      if (!state.drawerTaskId) return;
      try {
        await navigator.clipboard.writeText(state.drawerTaskId);
        showToast("已复制任务 ID");
      } catch {
        showToast("剪贴板不可用", true);
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
        showToast("已添加 Harness 折扣规则");
      } catch (err) {
        showToast(`保存失败：${err.message}`, true);
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
        showToast("已更新折扣开关");
      } catch (err) {
        showToast(`更新失败：${err.message}`, true);
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
        showToast("已删除折扣规则");
      } catch (err) {
        showToast(`删除失败：${err.message}`, true);
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
        showToast(`已保存 ${key} 单价`);
      } catch (err) {
        showToast(`保存失败：${err.message}`, true);
      }
    });

    $("pricing-table-body").addEventListener("click", (ev) => {
      const btn = ev.target.closest(".btn-edit-price-row");
      if (!btn) return;
      openPriceModal(btn.getAttribute("data-agent"), btn.getAttribute("data-key"));
    });

    $("btn-pricing-reset").addEventListener("click", async () => {
      if (!window.confirm("确认清除所有手动模型定价并恢复默认 Harness 折扣预置？")) return;
      try {
        state.pricing = await apiMutate("POST", "/api/pricing/reset", {});
        await refreshAll(false);
        showToast("已恢复默认预置定价与折扣");
      } catch (err) {
        showToast(`重置失败：${err.message}`, true);
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
        showToast(`已更新 ${key} 价格`);
      } catch (err) {
        showToast(`保存失败：${err.message}`, true);
      }
    });
    $("btn-modal-delete").addEventListener("click", async () => {
      const key = $("modal-key").value.trim();
      try {
        state.pricing = await apiMutate("PUT", "/api/pricing", { delete_model: key });
        closePriceModal();
        await refreshAll(false);
        showToast(`已移除 ${key} 手动覆盖`);
      } catch (err) {
        showToast(`删除失败：${err.message}`, true);
      }
    });

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
        renderStorage();
        showToast("已保存磁盘留存策略");
      } catch (err) {
        showToast(`保存失败：${err.message}`, true);
      }
    });

    $("btn-storage-prune").addEventListener("click", async () => {
      try {
        const res = await apiMutate("POST", "/api/storage/prune", {});
        state.storage = res.storage;
        renderStorage();
        const r = res.result;
        if (r.changed) {
          showToast(`已清理：超期 ${r.removed_by_age} 条，超体积 ${r.removed_by_size} 条，释放 ${fmtBytes(r.removed_bytes)}`);
        } else {
          showToast("当前账本未超限，无需裁剪");
        }
        await refreshAll(false);
      } catch (err) {
        showToast(`清理失败：${err.message}`, true);
      }
    });

    $("btn-storage-backfill").addEventListener("click", async () => {
      try {
        const res = await apiMutate("POST", "/api/storage/backfill", {});
        state.storage = res.storage;
        renderStorage();
        const r = res.result;
        showToast(`回填完成：扫描 ${r.scanned} 条，新增 ${r.imported} 条，跳过已有 ${r.skipped_existing} 条`);
        await refreshAll(false);
      } catch (err) {
        showToast(`回填失败：${err.message}`, true);
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
        showToast("请输入大写 CLEAR 以确认清空", true);
        return;
      }
      try {
        const res = await apiMutate("POST", "/api/storage/clear", { confirm: "CLEAR" });
        state.storage = res.storage;
        closeClearModal();
        renderStorage();
        await refreshAll(false);
        showToast(`已清空账本（移除 ${fmtInt(res.result.cleared_records)} 条，释放 ${fmtBytes(res.result.cleared_bytes)}）`);
      } catch (err) {
        showToast(`清空失败：${err.message}`, true);
      }
    });

    // Esc key closes overlays
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") {
        closeDrawer();
        closePriceModal();
        closeClearModal();
      }
    });
  }

  // ==================== Boot ====================
  initTheme();
  wireEvents();
  updateDiscountPreview();
  refreshAll(true).catch((err) => showToast(`初始化加载失败：${err.message}`, true));
})();
