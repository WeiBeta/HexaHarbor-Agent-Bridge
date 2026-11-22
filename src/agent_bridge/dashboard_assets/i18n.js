(() => {
  "use strict";

  const DEFAULT_LOCALE = "zh-CN";
  const SUPPORTED_LOCALES = ["zh-CN", "en"];
  const catalogs = Object.create(null);
  let currentLocale = DEFAULT_LOCALE;

  function normalizeLocale(value) {
    if (typeof value !== "string") return DEFAULT_LOCALE;
    const cleaned = value.trim().toLowerCase();
    if (cleaned === "en" || cleaned === "en-us" || cleaned === "en_us" || cleaned === "en-gb" || cleaned === "en_gb") {
      return "en";
    }
    if (cleaned === "zh-cn" || cleaned === "zh_cn" || cleaned === "zh" || cleaned === "cn" || cleaned === "zh-hans") {
      return "zh-CN";
    }
    return DEFAULT_LOCALE;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function lookupRaw(catalog, key) {
    if (!catalog || typeof key !== "string") return undefined;
    const parts = key.split(".");
    let cur = catalog;
    for (const part of parts) {
      if (!cur || typeof cur !== "object" || !(part in cur)) return undefined;
      cur = cur[part];
    }
    return typeof cur === "string" ? cur : undefined;
  }

  function interpolate(template, params) {
    if (!params) return template;
    return template.replace(/\{([a-zA-Z0-9_]+)\}/g, (match, name) => {
      if (Object.prototype.hasOwnProperty.call(params, name) && params[name] !== undefined && params[name] !== null) {
        return String(params[name]);
      }
      return match;
    });
  }

  function t(key, params) {
    const raw = lookupRaw(catalogs[currentLocale], key) ?? lookupRaw(catalogs[DEFAULT_LOCALE], key);
    if (raw === undefined) return key;
    return interpolate(raw, params);
  }

  function renderCodeTemplate(key, codeMap) {
    const raw = lookupRaw(catalogs[currentLocale], key) ?? lookupRaw(catalogs[DEFAULT_LOCALE], key);
    if (raw === undefined) return escapeHtml(key);
    let out = "";
    let lastIndex = 0;
    const re = /\{([a-zA-Z0-9_]+)\}/g;
    let m;
    while ((m = re.exec(raw)) !== null) {
      out += escapeHtml(raw.slice(lastIndex, m.index));
      const name = m[1];
      if (codeMap && Object.prototype.hasOwnProperty.call(codeMap, name)) {
        out += `<code>${escapeHtml(codeMap[name])}</code>`;
      } else {
        out += escapeHtml(m[0]);
      }
      lastIndex = re.lastIndex;
    }
    out += escapeHtml(raw.slice(lastIndex));
    return out;
  }

  function dataAttrToCamel(attrName) {
    return attrName
      .replace(/^data-code-/, "")
      .replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
  }

  function applyStaticDom(root = document) {
    document.documentElement.lang = currentLocale;
    document.title = t("app.pageTitle");

    root.querySelectorAll("[data-i18n]").forEach((el) => {
      const key = el.getAttribute("data-i18n");
      if (key) el.textContent = t(key);
    });

    root.querySelectorAll("[data-i18n-code]").forEach((el) => {
      const key = el.getAttribute("data-i18n-code");
      if (!key) return;
      const codeMap = Object.create(null);
      for (const attr of Array.from(el.attributes)) {
        if (attr.name.startsWith("data-code-")) {
          codeMap[dataAttrToCamel(attr.name)] = attr.value;
        }
      }
      el.innerHTML = renderCodeTemplate(key, codeMap);
    });

    root.querySelectorAll("[data-i18n-title]").forEach((el) => {
      const key = el.getAttribute("data-i18n-title");
      if (key) el.setAttribute("title", t(key));
    });

    root.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      const key = el.getAttribute("data-i18n-placeholder");
      if (key) el.setAttribute("placeholder", t(key));
    });

    root.querySelectorAll("[data-i18n-aria-label]").forEach((el) => {
      const key = el.getAttribute("data-i18n-aria-label");
      if (key) el.setAttribute("aria-label", t(key));
    });

    root.querySelectorAll("#lang-switch [data-locale]").forEach((btn) => {
      const active = btn.getAttribute("data-locale") === currentLocale;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", String(active));
    });
  }

  const BACKEND_EXACT_MAP = {
    "已开启开机自启": "backend.autostartEnabled",
    "已关闭开机自启": "backend.autostartDisabled",
    "系统托盘已启动": "backend.trayStarted",
    "系统托盘已在运行": "backend.trayAlreadyRunning",
    "已拉起系统托盘": "backend.trayLaunched",
    "系统托盘已关闭": "backend.trayStopped",
    "系统托盘仅支持 Windows": "backend.trayWindowsOnly",
    "非 Windows 平台无托盘": "backend.trayNonWindows",
    "未找到 PowerShell 或托盘脚本": "backend.trayMissing",
    "已保存服务设置": "backend.serviceSaved",
    "开机自启已被另一个工作目录占用": "backend.autostartOccupied",
    "未找到系统文件管理器命令": "backend.noFileManager",
    "missing or invalid X-Bridge-CSRF token": "backend.csrfInvalid",
    "cross-origin mutation rejected": "backend.crossOriginRejected",
    "forbidden Host header (loopback only)": "backend.hostForbidden",
    "request body too large": "backend.bodyTooLarge",
    "Content-Type must be application/json": "backend.contentTypeJson",
    'confirmation required: pass {"confirm": "CLEAR"}': "backend.clearConfirmationRequired",
  };

  function translateSingleBackendSegment(segment) {
    const trimmed = String(segment ?? "").trim();
    if (!trimmed) return "";
    if (Object.prototype.hasOwnProperty.call(BACKEND_EXACT_MAP, trimmed)) {
      return t(BACKEND_EXACT_MAP[trimmed]);
    }
    const portMatch = /^端口已保存为 (\d+)，点击「重启服务」后生效$/.exec(trimmed);
    if (portMatch) {
      return t("backend.portSavedRestart", { port: portMatch[1] });
    }
    const taskMatch = /^unknown task (.+)$/.exec(trimmed);
    if (taskMatch) {
      return t("backend.unknownTask", { taskId: taskMatch[1] });
    }
    if (/^unsupported locale\b/i.test(trimmed)) {
      return t("backend.unsupportedLocale");
    }
    if (/^(Failed to fetch|Load failed|NetworkError)/i.test(trimmed)) {
      return t("backend.networkUnreachable");
    }
    return trimmed;
  }

  function translateBackendText(message) {
    const raw = String(message ?? "").trim();
    if (!raw) return "";
    if (raw.includes(" · ")) {
      return raw
        .split(" · ")
        .map((part) => translateSingleBackendSegment(part))
        .filter(Boolean)
        .join(" · ");
    }
    return translateSingleBackendSegment(raw);
  }

  function translateDiscountLabel(label, ruleId) {
    if (!label) return "";
    if (label === "ZCode GLM 150% 额度加成" || (ruleId === "preset-zcode-glm-150" && label === "ZCode GLM 150% 额度加成")) {
      return t("pricing.presetZcodeGlm150");
    }
    return label;
  }

  async function loadCatalog(locale) {
    const canonical = normalizeLocale(locale);
    if (catalogs[canonical]) return catalogs[canonical];
    const res = await fetch(`/assets/locales/${encodeURIComponent(canonical)}.json`, { cache: "no-store" });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    const data = await res.json();
    if (data && typeof data === "object") {
      catalogs[canonical] = data;
    }
    return catalogs[canonical];
  }

  async function init(initialLocale) {
    currentLocale = normalizeLocale(initialLocale);
    const ordered = currentLocale === DEFAULT_LOCALE ? [DEFAULT_LOCALE, "en"] : [currentLocale, DEFAULT_LOCALE];
    try {
      await Promise.allSettled(ordered.map((loc) => loadCatalog(loc)));
      applyStaticDom();
    } finally {
      document.documentElement.setAttribute("data-i18n-ready", "1");
    }
    return currentLocale;
  }

  async function setLocale(nextLocale) {
    const canonical = normalizeLocale(nextLocale);
    if (!catalogs[canonical]) {
      await loadCatalog(canonical).catch(() => {});
    }
    currentLocale = canonical;
    applyStaticDom();
    return currentLocale;
  }

  function getLocale() {
    return currentLocale;
  }

  function intlLocale() {
    return currentLocale === "en" ? "en-US" : "zh-CN";
  }

  window.AbI18n = {
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    init,
    setLocale,
    getLocale,
    intlLocale,
    normalizeLocale,
    t,
    applyStaticDom,
    translateBackendText,
    translateDiscountLabel,
  };
})();
