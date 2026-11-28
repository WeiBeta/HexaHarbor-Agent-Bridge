from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from agent_bridge.paths import dashboard_assets_dir

# Minimal deterministic Node DOM harness that executes the real i18n.js + app.js
# against an in-memory DOM matching the AGY Slots markup in index.html.
_NODE_DOM_HARNESS = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const assetsDir = process.argv[2];
const scenarioName = process.argv[3];

const catalogs = {
  "zh-CN": JSON.parse(fs.readFileSync(path.join(assetsDir, "locales", "zh-CN.json"), "utf8")),
  "en": JSON.parse(fs.readFileSync(path.join(assetsDir, "locales", "en.json"), "utf8")),
};

class MiniClassList {
  constructor(node) {
    this._node = node;
    this._set = new Set();
  }
  _syncFromAttr(str) {
    this._set = new Set(String(str || "").split(/\s+/).filter(Boolean));
  }
  _syncToAttr() {
    this._node._attrs["class"] = Array.from(this._set).join(" ");
  }
  add(...cls) { cls.forEach((c) => this._set.add(c)); this._syncToAttr(); }
  remove(...cls) { cls.forEach((c) => this._set.delete(c)); this._syncToAttr(); }
  toggle(c, force) {
    if (force === undefined) {
      if (this._set.has(c)) this._set.delete(c); else this._set.add(c);
    } else if (force) this._set.add(c); else this._set.delete(c);
    this._syncToAttr();
  }
  contains(c) { return this._set.has(c); }
}

class MiniTextNode {
  constructor(text) {
    this.nodeType = 3;
    this.textContent = String(text ?? "");
    this.parentNode = null;
  }
}

class MiniElement {
  constructor(tag) {
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this._attrs = Object.create(null);
    this.classList = new MiniClassList(this);
    this.childNodes = [];
    this.parentNode = null;
    this._listeners = Object.create(null);
    this.value = "";
    this.disabled = false;
    this.checked = false;
    this.type = "";
    this.placeholder = "";
    this.style = {};
    this._text = null;
  }
  get id() { return this._attrs["id"] || ""; }
  set id(v) { this._attrs["id"] = String(v); }
  get className() { return this._attrs["class"] || ""; }
  set className(v) { this._attrs["class"] = String(v); this.classList._syncFromAttr(v); }
  setAttribute(k, v) {
    const key = String(k);
    this._attrs[key] = String(v);
    if (key === "class") this.classList._syncFromAttr(v);
  }
  getAttribute(k) {
    return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null;
  }
  appendChild(child) {
    if (child.parentNode) {
      const idx = child.parentNode.childNodes.indexOf(child);
      if (idx >= 0) child.parentNode.childNodes.splice(idx, 1);
    }
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }
  get textContent() {
    if (this.childNodes.length === 0) return this._text ?? "";
    return this.childNodes.map((c) => c.textContent).join("");
  }
  set textContent(v) {
    this.childNodes = [];
    this._text = String(v ?? "");
  }
  get innerHTML() {
    // AGY Slots renderer must never rely on innerHTML; return escaped text for safety checks.
    return this.textContent;
  }
  set innerHTML(v) {
    this._innerHtmlCalls = (this._innerHtmlCalls || 0) + 1;
    this.childNodes = [];
    this._text = String(v ?? "");
  }
  addEventListener(type, fn) {
    if (!this._listeners[type]) this._listeners[type] = [];
    this._listeners[type].push(fn);
  }
  dispatch(type, ev = {}) {
    ev.target = ev.target || this;
    ev.preventDefault = ev.preventDefault || (() => {});
    for (const fn of this._listeners[type] || []) {
      fn(ev);
    }
  }
  click() {
    if (this.disabled) return;
    this.dispatch("click", {});
  }
  closest(selector) {
    let cur = this;
    while (cur && cur.nodeType === 1) {
      if (matchesSimple(cur, selector)) return cur;
      cur = cur.parentNode;
    }
    return null;
  }
  querySelectorAll(selector) {
    const parts = String(selector).trim().split(/\s+/).filter(Boolean);
    if (parts.length === 0) return [];
    let current = [this];
    for (const part of parts) {
      const next = [];
      const seen = new Set();
      const walk = (node) => {
        for (const ch of node.childNodes || []) {
          if (ch.nodeType === 1) {
            if (matchesSimple(ch, part) && !seen.has(ch)) {
              seen.add(ch);
              next.push(ch);
            }
            walk(ch);
          }
        }
      };
      for (const root of current) walk(root);
      current = next;
    }
    return current;
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

function matchesSimple(el, sel) {
  sel = sel.trim();
  if (sel.startsWith("#")) return el.id === sel.slice(1);
  if (sel.startsWith(".")) return el.classList.contains(sel.slice(1));
  const attrMatch = /^([a-zA-Z0-9_-]*)\[([a-zA-Z0-9_-]+)(?:="([^"]*)")?\]$/.exec(sel);
  if (attrMatch) {
    const [, tag, attr, val] = attrMatch;
    if (tag && el.tagName !== tag.toUpperCase()) return false;
    const actual = el.getAttribute(attr);
    if (val === undefined) return actual !== null;
    return actual === val;
  }
  return el.tagName === sel.toUpperCase();
}

function createDoc() {
  const root = new MiniElement("html");
  const body = new MiniElement("body");
  root.appendChild(body);
  const byId = new Map();
  const reg = (tag, id, cls = "") => {
    const el = new MiniElement(tag);
    if (id) { el.id = id; byId.set(id, el); }
    if (cls) el.className = cls;
    body.appendChild(el);
    return el;
  };

  const csrfMeta = new MiniElement("meta");
  csrfMeta.setAttribute("name", "ab-csrf");
  csrfMeta.setAttribute("content", "test-csrf-token-123");
  const localeMeta = new MiniElement("meta");
  localeMeta.setAttribute("name", "ab-locale");
  localeMeta.setAttribute("content", "zh-CN");
  root.appendChild(csrfMeta);
  root.appendChild(localeMeta);

  reg("span", "agy-slots-state-badge", "badge");
  reg("span", "agy-slots-cleaner-badge", "badge hidden");
  reg("button", "btn-agy-slots-refresh", "btn btn-ghost btn-sm");
  reg("div", "agy-slots-root");
  const modalBackdrop = reg("div", "agy-cleanup-modal-backdrop", "modal-backdrop hidden");
  const modalBody = new MiniElement("div");
  modalBody.id = "agy-modal-body";
  byId.set("agy-modal-body", modalBody);
  modalBackdrop.appendChild(modalBody);
  reg("button", "btn-agy-modal-close");
  reg("button", "btn-agy-modal-cancel");
  reg("form", "form-agy-cleanup-confirm");
  reg("div", "toast", "toast hidden");

  const docListeners = Object.create(null);
  const doc = {
    documentElement: Object.assign(root, { lang: "zh-CN" }),
    title: "",
    hidden: false,
    activeElement: null,
    createElement: (tag) => new MiniElement(tag),
    createTextNode: (txt) => new MiniTextNode(txt),
    getElementById: (id) => {
      const direct = byId.get(id);
      if (direct) return direct;
      return body.querySelector(`#${id}`);
    },
    querySelector: (sel) => {
      if (sel === 'meta[name="ab-csrf"]') return csrfMeta;
      if (sel === 'meta[name="ab-locale"]') return localeMeta;
      return body.querySelector(sel);
    },
    querySelectorAll: (sel) => body.querySelectorAll(sel),
    addEventListener: (type, fn) => {
      if (!docListeners[type]) docListeners[type] = [];
      docListeners[type].push(fn);
    },
    dispatchDoc: (type, ev = {}) => {
      for (const fn of docListeners[type] || []) fn(ev);
    },
  };
  return { doc, body };
}

function makeSandbox(fetchHandler) {
  const { doc, body } = createDoc();
  const win = {
    __AB_DISABLE_AUTO_BOOT__: true,
    innerWidth: 1280,
    location: { origin: "http://127.0.0.1:8765", href: "http://127.0.0.1:8765/" },
    confirm: () => true,
  };
  const sandbox = {
    window: win,
    document: doc,
    localStorage: { getItem: () => null, setItem: () => {} },
    fetch: async (url, init = {}) => {
      if (String(url).includes("/assets/locales/")) {
        const loc = String(url).includes("en.json") ? "en" : "zh-CN";
        return { ok: true, status: 200, json: async () => catalogs[loc] };
      }
      return fetchHandler(url, init);
    },
    URL,
    URLSearchParams,
    console,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(assetsDir, "i18n.js"), "utf8"), sandbox);
  vm.runInContext(fs.readFileSync(path.join(assetsDir, "app.js"), "utf8"), sandbox);
  return { sandbox, doc, body };
}

function allFiveSlotsPayload(overrides = {}) {
  return {
    ok: true,
    enabled: true,
    rotation_tz: "Asia/Shanghai",
    grace_hours: 24,
    now: "2026-11-27T14:00:00+08:00",
    active_slot: "slotA",
    active_project_id: "11111111-1111-4111-8111-111111111111",
    cleaner: {
      conditional_delete_available: false,
      live_cleanup_available: true,
      live_block_reason: null,
      mode: "cli_run_command",
      model: "gemini-3-flash",
      available: true,
      reason: null,
    },
    state_error: null,
    receipt_error: null,
    slots: [
      {
        name: "slotA",
        project_id: "11111111-1111-4111-8111-111111111111",
        status: "active",
        is_active: true,
        days_since_active: 0,
        last_active_end: null,
        cooling_elapsed_hours: 0.0,
        grace_remaining_hours: 24.0,
        bound_sessions_count: 0,
        guard_error: null,
        unconfirmed_operation_id: null,
        blocked_sessions: [],
        project_file_exists: true,
        project_is_empty: true,
        last_cleanup: null,
      },
      {
        name: "slotB",
        project_id: "22222222-2222-4222-8222-222222222222",
        status: "releasable",
        is_active: false,
        days_since_active: 2,
        last_active_end: "2026-11-25T23:59:59+08:00",
        cooling_elapsed_hours: 38.0,
        grace_remaining_hours: 0.0,
        bound_sessions_count: 0,
        guard_error: null,
        unconfirmed_operation_id: null,
        blocked_sessions: [],
        project_file_exists: true,
        project_is_empty: true,
        last_cleanup: null,
      },
      {
        name: "slotC",
        project_id: "33333333-3333-4333-8333-333333333333",
        status: "cooling",
        is_active: false,
        days_since_active: 1,
        last_active_end: "2026-11-26T23:59:59+08:00",
        cooling_elapsed_hours: 14.0,
        grace_remaining_hours: 10.0,
        bound_sessions_count: 0,
        guard_error: null,
        unconfirmed_operation_id: null,
        blocked_sessions: [],
        project_file_exists: false,
        project_is_empty: false,
        last_cleanup: null,
      },
      {
        name: "slotD",
        project_id: "44444444-4444-4444-8444-444444444444",
        status: "blocked",
        is_active: false,
        days_since_active: 3,
        last_active_end: "2026-11-24T23:59:59+08:00",
        cooling_elapsed_hours: 62.0,
        grace_remaining_hours: 0.0,
        bound_sessions_count: 4,
        guard_error: null,
        unconfirmed_operation_id: null,
        blocked_sessions: [
          { session_id: "s1", native_session_id: "n1", proc_state: "running", last_active_at: "2026-11-27T12:00:00Z", reason: "active_task" },
          { session_id: "s2", native_session_id: "n2", proc_state: "idle", last_active_at: "2026-11-27T11:00:00Z", reason: "active_session" },
          { session_id: "s3", native_session_id: "n3", proc_state: "closed", last_active_at: "2026-11-27T10:00:00Z", reason: "unclosed_session" },
          { session_id: "s4", native_session_id: null, proc_state: "running", last_active_at: "2026-11-27T09:00:00Z", reason: "unattributed_active_task" },
        ],
        project_file_exists: true,
        project_is_empty: true,
        last_cleanup: null,
      },
      {
        name: "slotE",
        project_id: "55555555-5555-4555-8555-555555555555",
        status: "unknown",
        is_active: false,
        days_since_active: 4,
        last_active_end: "2026-11-23T23:59:59+08:00",
        cooling_elapsed_hours: 86.0,
        grace_remaining_hours: 0.0,
        bound_sessions_count: null,
        guard_error: "<img src=x onerror=alert(1)> cleanup completion is unconfirmed <a href='http://evil.test'>link</a>",
        unconfirmed_operation_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        blocked_sessions: [],
        project_file_exists: true,
        project_is_empty: true,
        last_cleanup: null,
      },
    ],
    last_cleanup: null,
    ...overrides,
  };
}

function validPreviewPayload(slot = "slotB", projectId = "22222222-2222-4222-8222-222222222222") {
  return {
    ok: true,
    slot,
    project_id: projectId,
    dry_run: true,
    slot_status: "releasable",
    outcome: "completed",
    executed_at: "2026-11-27T14:01:00+08:00",
    counts_confirmed: true,
    receipt_saved: true,
    eligible_ids: ["conv-1", "conv-2"],
    deleted_ids: [],
    would_delete_ids: ["conv-1", "conv-2"],
    rejected_delete_ids: [],
    unconfirmed_delete_ids: [],
    checked: 3,
    deleted: 0,
    would_delete: 2,
    skipped_running: 0,
    skipped_recent: 1,
    skipped_unverified: 0,
    cutoff_utc: "2026-11-26T06:00:00Z",
    batches: [],
    pending_batch_number: null,
    stop_reason: "preview",
    http_200_ids: [],
    remaining_ids: [],
    has_more: false,
    worker_elapsed_ms: 18400,
    native_removed: 0,
    windows_residual_reclaimed: 0,
    residual_locked: 0,
    errors: [],
  };
}

async function runScenario() {
  const requests = [];
  let nextGetPayload = null;
  let nextPostResp = null;

  const { sandbox, doc } = makeSandbox(async (url, init) => {
    const method = (init.method || "GET").toUpperCase();
    const body = init.body ? JSON.parse(init.body) : null;
    requests.push({ method, url: String(url), headers: init.headers || {}, body });
    if (method === "GET" && String(url) === "/api/agy/slots") {
      return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(nextGetPayload)) };
    }
    if (method === "POST" && String(url) === "/api/agy/slots/cleanup") {
      const resp = typeof nextPostResp === "function" ? nextPostResp(body) : nextPostResp;
      return {
        ok: resp.status >= 200 && resp.status < 300,
        status: resp.status,
        json: async () => JSON.parse(JSON.stringify(resp.data)),
      };
    }
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  });

  const i18n = sandbox.window.AbI18n;
  const api = sandbox.window.AbDashboardAgySlots;
  await i18n.init("zh-CN");

  if (scenarioName === "disabled_and_five_statuses") {
    // 1. Disabled empty state (HTTP 200, enabled === false)
    nextGetPayload = {
      ok: true,
      enabled: false,
      rotation_tz: "Asia/Shanghai",
      grace_hours: 24,
      now: "2026-11-27T14:00:00+08:00",
      active_slot: null,
      active_project_id: null,
      cleaner: { conditional_delete_available: false, live_cleanup_available: false, live_block_reason: "agy_slots is disabled", mode: "cli_run_command", model: null, available: false, reason: "agy_slots is disabled" },
      state_error: null,
      receipt_error: null,
      slots: [],
      last_cleanup: null,
    };
    await api.loadAgySlots();
    const root = doc.getElementById("agy-slots-root");
    assert.strictEqual(doc.getElementById("agy-slots-state-badge").textContent, "未启用");
    assert.ok(root.querySelector('[data-agy-state="disabled"]'));
    assert.ok(root.textContent.includes("agents.toml"));
    assert.strictEqual(requests.length, 1);
    assert.strictEqual(requests[0].method, "GET");

    await i18n.setLocale("en");
    api.renderAgySlots();
    assert.strictEqual(doc.getElementById("agy-slots-state-badge").textContent, "Disabled");
    assert.ok(root.textContent.includes("AGY Slot Rotation Not Enabled"));

    // 2. Enabled with all 5 statuses, health alerts, state_error, XSS strings, conditional_delete_available === false
    await i18n.setLocale("zh-CN");
    nextGetPayload = allFiveSlotsPayload({ state_error: "bridge state unreadable <script>bad()</script>" });
    await api.loadAgySlots();

    assert.strictEqual(doc.getElementById("agy-slots-state-badge").textContent, "已启用");
    assert.strictEqual(doc.getElementById("agy-slots-cleaner-badge").textContent, "清理器可用");
    // conditional_delete_available === false must NOT disable live cleanup
    assert.strictEqual(root.querySelector('[data-cleaner-live="1"]') !== null, true);
    assert.strictEqual(root.querySelector('[data-cleaner-conditional="0"]') !== null, true);
    assert.ok(root.textContent.includes("不影响整槽清理可用性"));
    assert.ok(root.textContent.includes("15\u201360 秒"));
    assert.ok(root.textContent.includes("不要启动 agy CLI") || root.textContent.includes("请勿启动 agy CLI"));

    // All 5 slot cards rendered with exact statuses
    const cards = root.querySelectorAll(".agy-slot-card");
    assert.strictEqual(cards.length, 5);
    assert.deepStrictEqual(
      cards.map((c) => [c.getAttribute("data-slot-name"), c.getAttribute("data-slot-status")]),
      [
        ["slotA", "active"],
        ["slotB", "releasable"],
        ["slotC", "cooling"],
        ["slotD", "blocked"],
        ["slotE", "unknown"],
      ]
    );

    // Health alert highlighted on slotC (project_file_exists=false & project_is_empty=false)
    const slotCCard = cards[2];
    assert.strictEqual(slotCCard.getAttribute("data-health-alert"), "1");
    assert.ok(slotCCard.textContent.includes("相对路径串台"));
    assert.ok(slotCCard.textContent.includes("project_file_exists = false"));

    // XSS plain-text check: zero <img>, <a>, <script> elements created anywhere in agy-slots-root
    assert.strictEqual(root.querySelectorAll("img").length, 0);
    assert.strictEqual(root.querySelectorAll("a").length, 0);
    assert.strictEqual(root.querySelectorAll("script").length, 0);
    assert.ok(root.textContent.includes("<img src=x onerror=alert(1)>"));
    assert.ok(root.textContent.includes("<script>bad()</script>"));

    // slotE has bound_sessions_count === null -> displays 未知, not 0
    const slotECard = cards[4];
    assert.ok(slotECard.textContent.includes("未知"));
    assert.ok(slotECard.textContent.includes("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"));

    // Preview permission matrix across all 5 statuses
    const previewState = {};
    for (const name of ["slotA", "slotB", "slotC", "slotD", "slotE"]) {
      api.selectAgySlot(name);
      previewState[name] = {
        previewDisabled: doc.getElementById("btn-agy-preview").disabled,
        liveDisabled: doc.getElementById("btn-agy-live-open").disabled,
      };
    }
    assert.strictEqual(previewState.slotA.previewDisabled, true);
    assert.strictEqual(previewState.slotB.previewDisabled, false);
    assert.strictEqual(previewState.slotC.previewDisabled, false);
    assert.strictEqual(previewState.slotD.previewDisabled, false);
    assert.strictEqual(previewState.slotE.previewDisabled, true);
    assert.ok(Object.values(previewState).every((s) => s.liveDisabled === true));

    // Blocked sessions reasons rendered on slotD
    api.selectAgySlot("slotD");
    assert.ok(root.textContent.includes("active_task"));
    assert.ok(root.textContent.includes("active_session"));
    assert.ok(root.textContent.includes("unclosed_session"));
    assert.ok(root.textContent.includes("unattributed_active_task"));

    // When live_cleanup_available === false, preview is disabled even for releasable/cooling/blocked
    nextGetPayload = allFiveSlotsPayload({
      cleaner: {
        conditional_delete_available: false,
        live_cleanup_available: false,
        live_block_reason: "agy executable not found",
        mode: "cli_run_command",
        model: "gemini-3-flash",
        available: false,
        reason: "agy executable not found",
      },
    });
    await api.loadAgySlots();
    api.selectAgySlot("slotB");
    assert.strictEqual(doc.getElementById("btn-agy-preview").disabled, true);
    assert.ok(root.textContent.includes("agy executable not found"));

    // Switch to EN and verify key English strings
    await i18n.setLocale("en");
    api.renderAgySlots();
    assert.ok(root.textContent.includes("Active Today"));
    assert.ok(root.textContent.includes("Releasable"));
    assert.ok(root.textContent.includes("relative-path cross-contamination"));
    assert.strictEqual(root.querySelectorAll("img").length, 0);
    assert.strictEqual(root.querySelectorAll("a").length, 0);
  }

  else if (scenarioName === "conflict_409") {
    nextGetPayload = allFiveSlotsPayload();
    await api.loadAgySlots();
    api.selectAgySlot("slotB");

    // Preview succeeds first
    nextPostResp = { status: 200, data: validPreviewPayload("slotB") };
    await api.runAgyDryRunPreview();

    // Type exact slot name & open modal
    const inp = doc.getElementById("agy-confirm-input");
    inp.value = "slotB";
    inp.dispatch("input", { target: inp });
    api.openAgyCleanupModal();

    // Live delete returns HTTP 409 with blocking_processes + blocked_sessions
    nextPostResp = {
      status: 409,
      data: {
        ok: false,
        slot: "slotB",
        project_id: "22222222-2222-4222-8222-222222222222",
        dry_run: false,
        slot_status: "releasable",
        outcome: "not_started",
        executed_at: "2026-11-27T14:02:00+08:00",
        counts_confirmed: true,
        receipt_saved: false,
        eligible_ids: [],
        deleted_ids: [],
        would_delete_ids: [],
        rejected_delete_ids: [],
        unconfirmed_delete_ids: [],
        checked: 0,
        deleted: 0,
        would_delete: 0,
        skipped_running: 0,
        skipped_recent: 0,
        skipped_unverified: 0,
        blocked_sessions: [
          { session_id: "sess-9", native_session_id: "nat-9", proc_state: "running", last_active_at: "2026-11-27T14:01:55Z", reason: "active_task" },
        ],
        blocking_processes: [
          { pid: 1234, name: "agy.exe" },
          { pid: 5678, name: "AGY.EXE" },
        ],
        errors: ["agy_cli_running"],
      },
    };
    await api.submitAgyLiveCleanup();

    const root = doc.getElementById("agy-slots-root");
    const receipt = root.querySelector('[data-agy-receipt="current"]');
    assert.ok(receipt, "409 structured receipt must be preserved");
    assert.strictEqual(receipt.getAttribute("data-agy-http-status"), "409");
    assert.ok(receipt.textContent.includes("1234"));
    assert.ok(receipt.textContent.includes("agy.exe"));
    assert.ok(receipt.textContent.includes("5678"));
    assert.ok(receipt.textContent.includes("AGY.EXE"));
    assert.ok(receipt.textContent.includes("agy_cli_running"));
    assert.ok(receipt.textContent.includes("不会替你结束进程"));
    assert.ok(receipt.textContent.includes("sess-9"));
    // Must not render any kill/terminate button inside receipt
    assert.strictEqual(receipt.querySelectorAll("button").length, 0);
    // Confirmation state consumed/invalidated
    assert.strictEqual(api.getState().confirmText, "");
    assert.strictEqual(api.getState().validPreview, null);
    assert.strictEqual(api.getState().modalOpen, false);

    await i18n.setLocale("en");
    api.renderAgySlots();
    assert.ok(doc.getElementById("agy-slots-root").textContent.includes("will not terminate processes for you"));
  }

  else if (scenarioName === "partial_unknown_502_and_tristate") {
    nextGetPayload = allFiveSlotsPayload();
    await api.loadAgySlots();
    api.selectAgySlot("slotB");

    // 1. Live partial/residual_locked receipt via HTTP 200 (ok=false, outcome=partial, stop_reason=residual_locked)
    nextPostResp = { status: 200, data: validPreviewPayload("slotB") };
    await api.runAgyDryRunPreview();
    const inp = doc.getElementById("agy-confirm-input");
    inp.value = "slotB";
    inp.dispatch("input", { target: inp });
    api.openAgyCleanupModal();

    nextPostResp = {
      status: 200,
      data: {
        ok: false,
        slot: "slotB",
        project_id: "22222222-2222-4222-8222-222222222222",
        dry_run: false,
        slot_status: "releasable",
        outcome: "partial",
        executed_at: "2026-11-27T14:05:00+08:00",
        counts_confirmed: true,
        receipt_saved: true,
        eligible_ids: ["c1", "c2", "c3", "c4", "c5"],
        deleted_ids: ["c1", "c2", "c3"],
        would_delete_ids: [],
        rejected_delete_ids: ["c4"],
        unconfirmed_delete_ids: ["c5"],
        checked: 5,
        deleted: 3,
        would_delete: 0,
        skipped_running: 0,
        skipped_recent: 0,
        skipped_unverified: 0,
        cutoff_utc: "2026-11-26T06:00:00Z",
        batches: [
          {
            batch_number: 1,
            ok: true,
            outcome_unknown: false,
            http_200_ids: ["c1", "c2", "c3"],
            worker_elapsed_ms: 21500,
            errors: [],
            residuals: [
              { id: "c1", status: "native_removed", reason: null, moved: [], remaining: [] },
              { id: "c2", status: "windows_residual_reclaimed", reason: null, moved: ["db", "wal"], remaining: [] },
              { id: "c3", status: "residual_locked", reason: "process_exit_unconfirmed", moved: null, remaining: null },
            ],
          },
        ],
        pending_batch_number: null,
        stop_reason: "residual_locked",
        http_200_ids: ["c1", "c2", "c3"],
        remaining_ids: ["c5"],
        has_more: true,
        worker_elapsed_ms: 21500,
        native_removed: 1,
        windows_residual_reclaimed: 1,
        residual_locked: 1,
        errors: ["residual_locked"],
      },
    };
    await api.submitAgyLiveCleanup();

    const root = doc.getElementById("agy-slots-root");
    const receipt = root.querySelector('[data-agy-receipt="current"]');
    assert.ok(receipt);
    // Must NEVER report round completed when stop_reason !== completed!
    assert.ok(!receipt.textContent.includes("整轮清理已完成"));
    assert.ok(receipt.textContent.includes("未完成整轮清理"));
    assert.ok(receipt.textContent.includes("residual_locked"));
    // 4 concepts strictly distinguished
    assert.strictEqual(receipt.querySelector('[data-concept="http_200_ids"] .agy-concept-value').textContent, "3");
    assert.strictEqual(receipt.querySelector('[data-concept="native_removed"] .agy-concept-value').textContent, "1");
    assert.strictEqual(receipt.querySelector('[data-concept="windows_residual_reclaimed"] .agy-concept-value').textContent, "1");
    assert.strictEqual(receipt.querySelector('[data-concept="residual_locked"] .agy-concept-value').textContent, "1");
    assert.strictEqual(receipt.querySelector('[data-concept="remaining_ids"] .agy-concept-value').textContent, "1");
    assert.strictEqual(receipt.querySelector('[data-concept="unconfirmed_delete_ids"] .agy-concept-value').textContent, "1");
    assert.strictEqual(receipt.querySelector('[data-concept="rejected_delete_ids"] .agy-concept-value').textContent, "1");
    assert.ok(receipt.textContent.includes("不等于原生删除成功"));
    assert.ok(receipt.textContent.includes("不能折算成已释放磁盘空间"));

    // Tri-state checks for moved & remaining on c1 ([] / []), c2 (["db","wal"] / []), c3 (null / null)
    const rowC1 = receipt.querySelector('[data-agy-residual-id="c1"]');
    const rowC2 = receipt.querySelector('[data-agy-residual-id="c2"]');
    const rowC3 = receipt.querySelector('[data-agy-residual-id="c3"]');
    assert.strictEqual(rowC1.querySelector('[data-field="moved"]').getAttribute("data-tristate"), "empty");
    assert.strictEqual(rowC1.querySelector('[data-field="remaining"]').getAttribute("data-tristate"), "empty");
    assert.ok(rowC1.querySelector('[data-field="moved"]').textContent.includes("0"));
    assert.strictEqual(rowC2.querySelector('[data-field="moved"]').getAttribute("data-tristate"), "items");
    assert.ok(rowC2.querySelector('[data-field="moved"]').textContent.includes("db, wal"));
    assert.strictEqual(rowC2.querySelector('[data-field="remaining"]').getAttribute("data-tristate"), "empty");
    assert.strictEqual(rowC3.querySelector('[data-field="moved"]').getAttribute("data-tristate"), "unknown");
    assert.strictEqual(rowC3.querySelector('[data-field="remaining"]').getAttribute("data-tristate"), "unknown");
    // Null must NEVER render as "0" or "无"
    const c3MovedTxt = rowC3.querySelector('[data-field="moved"]').textContent;
    const c3RemTxt = rowC3.querySelector('[data-field="remaining"]').textContent;
    assert.ok(c3MovedTxt.includes("未知") && !c3MovedTxt.includes("0") && !c3MovedTxt.includes("无"));
    assert.ok(c3RemTxt.includes("未知") && !c3RemTxt.includes("0") && !c3RemTxt.includes("无"));

    // 2. HTTP 502 with unknown stop_reason, counts_confirmed=false, flat batches[] shape fallback
    nextPostResp = { status: 200, data: validPreviewPayload("slotB") };
    await api.runAgyDryRunPreview();
    const inp2 = doc.getElementById("agy-confirm-input");
    inp2.value = "slotB";
    inp2.dispatch("input", { target: inp2 });
    api.openAgyCleanupModal();
    nextPostResp = {
      status: 502,
      data: {
        ok: false,
        slot: "slotB",
        project_id: "22222222-2222-4222-8222-222222222222",
        dry_run: false,
        slot_status: "releasable",
        outcome: "unknown",
        executed_at: "2026-11-27T14:06:00+08:00",
        counts_confirmed: false,
        receipt_saved: false,
        eligible_ids: [],
        deleted_ids: [],
        would_delete_ids: [],
        rejected_delete_ids: [],
        unconfirmed_delete_ids: ["c9"],
        checked: null,
        deleted: null,
        would_delete: null,
        skipped_running: null,
        skipped_recent: null,
        skipped_unverified: null,
        cutoff_utc: "2026-11-26T06:00:00Z",
        batches: [
          { id: "flat-1", status: "residual_locked", reason: "worker_timeout", moved: null, remaining: [] },
        ],
        pending_batch_number: 2,
        stop_reason: "unknown",
        http_200_ids: ["c9"],
        remaining_ids: ["c9"],
        has_more: null,
        worker_elapsed_ms: 60000,
        native_removed: 0,
        windows_residual_reclaimed: 0,
        residual_locked: 1,
        errors: ["cleaner timed out; execution unconfirmed"],
      },
    };
    await api.submitAgyLiveCleanup();
    const rec502 = doc.getElementById("agy-slots-root").querySelector('[data-agy-receipt="current"]');
    assert.ok(rec502, "502 structured receipt must be preserved");
    assert.strictEqual(rec502.getAttribute("data-agy-http-status"), "502");
    assert.ok(rec502.textContent.includes("cleaner timed out; execution unconfirmed"));
    assert.ok(rec502.textContent.includes("counts_confirmed = false"));
    assert.ok(rec502.textContent.includes("receipt_saved = false"));
    const flatRow = rec502.querySelector('[data-agy-residual-id="flat-1"]');
    assert.strictEqual(flatRow.querySelector('[data-field="moved"]').getAttribute("data-tristate"), "unknown");
    assert.strictEqual(flatRow.querySelector('[data-field="remaining"]').getAttribute("data-tristate"), "empty");

    // Verify ALL non-completed stop_reasons return false from agyIsFullRoundCompleted
    for (const sr of ["unknown", "worker_failed", "no_progress", "targets_changed", "residual_locked", "batch_limit", "admission_changed", "checkpoint_failed", "preview"]) {
      assert.strictEqual(
        api.agyIsFullRoundCompleted({ dry_run: false, ok: true, outcome: "completed", counts_confirmed: true, stop_reason: sr, remaining_ids: [], unconfirmed_delete_ids: [], has_more: false, pending_batch_number: null, errors: [] }),
        false,
        `stop_reason=${sr} must not be treated as full round completed`
      );
    }
    assert.strictEqual(
      api.agyIsFullRoundCompleted({ dry_run: false, ok: true, outcome: "completed", counts_confirmed: true, stop_reason: "completed", remaining_ids: [], unconfirmed_delete_ids: [], has_more: false, pending_batch_number: null, errors: [] }),
      true
    );
  }

  else if (scenarioName === "cancel_confirm_and_invalidation") {
    nextGetPayload = allFiveSlotsPayload();
    await api.loadAgySlots();
    api.selectAgySlot("slotB");

    const postCount = () => requests.filter((r) => r.method === "POST").length;

    // A. Cancel path 1: No preview + empty input -> button disabled, openAgyCleanupModal refuses & explains
    assert.strictEqual(doc.getElementById("btn-agy-live-open").disabled, true);
    api.openAgyCleanupModal();
    assert.strictEqual(api.getState().modalOpen, false);
    assert.ok(doc.getElementById("agy-missing-conditions").textContent.includes("有效预演"));
    assert.strictEqual(postCount(), 0);

    // B. Cancel path 2: Exact slot name typed, still no preview -> button disabled, no POST
    let inp = doc.getElementById("agy-confirm-input");
    inp.value = "slotB";
    inp.dispatch("input", { target: inp });
    assert.strictEqual(doc.getElementById("btn-agy-live-open").disabled, true);
    api.openAgyCleanupModal();
    assert.strictEqual(api.getState().modalOpen, false);
    assert.strictEqual(postCount(), 0);

    // C. Run valid preview (1 dry_run POST)
    nextPostResp = { status: 200, data: validPreviewPayload("slotB") };
    await api.runAgyDryRunPreview();
    assert.strictEqual(postCount(), 1);
    assert.strictEqual(requests[requests.length - 1].body.dry_run, true);

    // D. Cancel path 3: Wrong confirm text ("slotA", "SLOTB", "slotB ") -> button stays disabled, no live POST
    inp = doc.getElementById("agy-confirm-input");
    for (const wrong of ["", "slotA", "SLOTB", "slotB "]) {
      inp.value = wrong;
      inp.dispatch("input", { target: inp });
      assert.strictEqual(doc.getElementById("btn-agy-live-open").disabled, true);
      api.openAgyCleanupModal();
      assert.strictEqual(api.getState().modalOpen, false);
    }
    assert.strictEqual(postCount(), 1);

    // E. Cancel path 4: Cooling slot even with valid preview + exact slot name -> disabled, no live POST
    api.selectAgySlot("slotC");
    nextPostResp = { status: 200, data: { ...validPreviewPayload("slotC", "33333333-3333-4333-8333-333333333333"), slot_status: "cooling" } };
    await api.runAgyDryRunPreview();
    inp = doc.getElementById("agy-confirm-input");
    inp.value = "slotC";
    inp.dispatch("input", { target: inp });
    assert.strictEqual(doc.getElementById("btn-agy-live-open").disabled, true);
    api.openAgyCleanupModal();
    assert.strictEqual(api.getState().modalOpen, false);
    assert.ok(doc.getElementById("agy-missing-conditions").textContent.includes("可释放"));

    // F. Invalidation 1: Slot switch invalidates confirm + preview + open modal
    api.selectAgySlot("slotB");
    nextPostResp = { status: 200, data: validPreviewPayload("slotB") };
    await api.runAgyDryRunPreview();
    inp = doc.getElementById("agy-confirm-input");
    inp.value = "slotB";
    inp.dispatch("input", { target: inp });
    api.openAgyCleanupModal();
    assert.strictEqual(api.getState().modalOpen, true);
    api.selectAgySlot("slotD");
    assert.strictEqual(api.getState().modalOpen, false);
    assert.strictEqual(api.getState().confirmText, "");
    assert.strictEqual(api.getState().validPreview, null);
    assert.strictEqual(doc.getElementById("btn-agy-live-open").disabled, true);
    assert.strictEqual(api.getState().invalidated, true);

    // G. Invalidation 2: Project rebind (project_id changes on refresh) invalidates confirm + preview + open modal
    api.selectAgySlot("slotB");
    nextPostResp = { status: 200, data: validPreviewPayload("slotB") };
    await api.runAgyDryRunPreview();
    inp = doc.getElementById("agy-confirm-input");
    inp.value = "slotB";
    inp.dispatch("input", { target: inp });
    api.openAgyCleanupModal();
    assert.strictEqual(api.getState().modalOpen, true);
    const rebound = allFiveSlotsPayload();
    rebound.slots[1].project_id = "99999999-9999-4999-8999-999999999999";
    nextGetPayload = rebound;
    await api.loadAgySlots();
    assert.strictEqual(api.getState().modalOpen, false);
    assert.strictEqual(api.getState().confirmText, "");
    assert.strictEqual(api.getState().validPreview, null);
    assert.strictEqual(doc.getElementById("agy-confirm-input").value, "");
    assert.strictEqual(doc.getElementById("btn-agy-live-open").disabled, true);

    // H. Invalidation 3: Status change (releasable -> cooling) invalidates confirm + preview + open modal
    nextGetPayload = allFiveSlotsPayload();
    await api.loadAgySlots();
    nextPostResp = { status: 200, data: validPreviewPayload("slotB") };
    await api.runAgyDryRunPreview();
    inp = doc.getElementById("agy-confirm-input");
    inp.value = "slotB";
    inp.dispatch("input", { target: inp });
    api.openAgyCleanupModal();
    assert.strictEqual(api.getState().modalOpen, true);
    const coolingNow = allFiveSlotsPayload();
    coolingNow.slots[1].status = "cooling";
    nextGetPayload = coolingNow;
    await api.loadAgySlots();
    assert.strictEqual(api.getState().modalOpen, false);
    assert.strictEqual(api.getState().confirmText, "");
    assert.strictEqual(api.getState().validPreview, null);
    assert.strictEqual(doc.getElementById("btn-agy-live-open").disabled, true);

    // I. Invalidation 4: Preview failure invalidates confirm + prior valid preview
    nextGetPayload = allFiveSlotsPayload();
    await api.loadAgySlots();
    nextPostResp = { status: 200, data: validPreviewPayload("slotB") };
    await api.runAgyDryRunPreview();
    inp = doc.getElementById("agy-confirm-input");
    inp.value = "slotB";
    inp.dispatch("input", { target: inp });
    assert.strictEqual(doc.getElementById("btn-agy-live-open").disabled, false);
    nextPostResp = {
      status: 502,
      data: { ok: false, slot: "slotB", project_id: "22222222-2222-4222-8222-222222222222", dry_run: true, outcome: "unknown", stop_reason: "unknown", errors: ["cleaner timed out; execution unconfirmed"] },
    };
    await api.runAgyDryRunPreview();
    assert.strictEqual(api.getState().confirmText, "");
    assert.strictEqual(api.getState().validPreview, null);
    assert.strictEqual(doc.getElementById("btn-agy-live-open").disabled, true);

    // J. Confirmed live cleanup path: satisfy all 4 conditions -> cancel once -> reopen & confirm -> exactly ONE live POST!
    nextPostResp = { status: 200, data: validPreviewPayload("slotB") };
    await api.runAgyDryRunPreview();
    inp = doc.getElementById("agy-confirm-input");
    inp.value = "slotB";
    inp.dispatch("input", { target: inp });
    assert.strictEqual(doc.getElementById("btn-agy-live-open").disabled, false);
    const livePostsBefore = requests.filter((r) => r.method === "POST" && r.body && r.body.dry_run === false).length;
    assert.strictEqual(livePostsBefore, 0);

    // Open modal then cancel -> still 0 live POSTs
    api.openAgyCleanupModal();
    assert.strictEqual(api.getState().modalOpen, true);
    api.closeAgyCleanupModal();
    assert.strictEqual(api.getState().modalOpen, false);
    assert.strictEqual(requests.filter((r) => r.method === "POST" && r.body && r.body.dry_run === false).length, 0);

    // Reopen modal and submit -> exactly 1 live POST with strict body + CSRF
    api.openAgyCleanupModal();
    nextPostResp = {
      status: 200,
      data: {
        ok: true,
        slot: "slotB",
        project_id: "22222222-2222-4222-8222-222222222222",
        dry_run: false,
        slot_status: "releasable",
        outcome: "completed",
        executed_at: "2026-11-27T14:10:00+08:00",
        counts_confirmed: true,
        receipt_saved: true,
        eligible_ids: ["conv-1", "conv-2"],
        deleted_ids: ["conv-1", "conv-2"],
        would_delete_ids: [],
        rejected_delete_ids: [],
        unconfirmed_delete_ids: [],
        checked: 3,
        deleted: 2,
        would_delete: 0,
        skipped_running: 0,
        skipped_recent: 1,
        skipped_unverified: 0,
        cutoff_utc: "2026-11-26T06:00:00Z",
        batches: [],
        pending_batch_number: null,
        stop_reason: "completed",
        http_200_ids: ["conv-1", "conv-2"],
        remaining_ids: [],
        has_more: false,
        worker_elapsed_ms: 19200,
        native_removed: 2,
        windows_residual_reclaimed: 0,
        residual_locked: 0,
        errors: [],
      },
    };
    await api.submitAgyLiveCleanup();

    const livePosts = requests.filter((r) => r.method === "POST" && r.body && r.body.dry_run === false);
    assert.strictEqual(livePosts.length, 1);
    assert.strictEqual(livePosts[0].url, "/api/agy/slots/cleanup");
    assert.strictEqual(livePosts[0].headers["X-Bridge-CSRF"], "test-csrf-token-123");
    assert.strictEqual(livePosts[0].body.slot, "slotB");
    assert.strictEqual(livePosts[0].body.dry_run, false);
    assert.strictEqual(livePosts[0].body.confirm, "slotB");
    assert.strictEqual(livePosts[0].body.project_id, "22222222-2222-4222-8222-222222222222");
    assert.ok(doc.getElementById("agy-slots-root").textContent.includes("整轮清理已完成"));
  }

  else if (scenarioName === "refresh_get_only") {
    nextGetPayload = allFiveSlotsPayload();
    await api.loadAgySlots();
    await api.loadAgySlots({ showToastOnSuccess: true });
    await api.loadAgySlots({ showToastOnSuccess: false });
    assert.strictEqual(requests.length, 3);
    assert.ok(requests.every((r) => r.method === "GET" && r.url === "/api/agy/slots"));
    assert.strictEqual(requests.filter((r) => r.method === "POST").length, 0);
  }

  else {
    throw new Error(`Unknown scenario: ${scenarioName}`);
  }

  process.stdout.write(JSON.stringify({ ok: true, scenario: scenarioName }));
}

runScenario().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""


def _run_node_scenario(scenario: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to execute AGY slots frontend DOM tests")
    with tempfile.TemporaryDirectory(prefix="ab-agy-dom-") as td:
        script_path = Path(td) / "harness.cjs"
        script_path.write_text(_NODE_DOM_HARNESS, encoding="utf-8")
        completed = subprocess.run(
            [node, str(script_path), str(dashboard_assets_dir()), scenario],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    assert completed.returncode == 0, f"Node scenario {scenario} failed:\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
    parsed = json.loads(completed.stdout)
    assert parsed["ok"] is True


def test_agy_slots_disabled_five_statuses_health_alerts_and_bilingual():
    _run_node_scenario("disabled_and_five_statuses")


def test_agy_slots_409_conflict_displays_blockers_and_processes():
    _run_node_scenario("conflict_409")


def test_agy_slots_partial_unknown_502_concepts_and_tristate():
    _run_node_scenario("partial_unknown_502_and_tristate")


def test_agy_slots_cancel_confirm_and_invalidation_paths():
    _run_node_scenario("cancel_confirm_and_invalidation")


def test_agy_slots_refresh_get_only():
    _run_node_scenario("refresh_get_only")


# ==================== Isolated Synthetic Loopback Server + Real Headless Browser E2E ====================


def _find_chromium_binary() -> str | None:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("msedge"),
    ]
    for path in candidates:
        if path and Path(path).is_file():
            return str(path)
    return None


class _SyntheticDashboardState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.csrf = "synthetic-e2e-csrf-999"
        self.locale = "zh-CN"
        self.revision = "rev-1"
        self.requests: list[dict[str, Any]] = []
        self.slots_payload: dict[str, Any] = {
            "ok": True,
            "enabled": False,
            "rotation_tz": "Asia/Shanghai",
            "grace_hours": 24,
            "now": "2026-11-27T14:00:00+08:00",
            "active_slot": None,
            "active_project_id": None,
            "cleaner": {
                "conditional_delete_available": False,
                "live_cleanup_available": False,
                "live_block_reason": "agy_slots is disabled",
                "mode": "cli_run_command",
                "model": None,
                "available": False,
                "reason": "agy_slots is disabled",
            },
            "state_error": None,
            "receipt_error": None,
            "slots": [],
            "last_cleanup": None,
        }
        self.next_cleanup_status: int = 200
        self.next_cleanup_response: dict[str, Any] = {}


def _start_synthetic_server() -> tuple[ThreadingHTTPServer, threading.Thread, _SyntheticDashboardState]:
    assets_dir = dashboard_assets_dir()
    shared = _SyntheticDashboardState()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_bytes(self, status: int, content_type: str, data: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, status: int, obj: Any) -> None:
            self._send_bytes(status, "application/json; charset=utf-8", json.dumps(obj).encode("utf-8"))

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            return json.loads(raw.decode("utf-8"))

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            with shared.lock:
                shared.requests.append({"method": "GET", "path": path})
                locale = shared.locale
                csrf = shared.csrf
                rev = shared.revision
                slots_payload = json.loads(json.dumps(shared.slots_payload))

            if path == "/":
                html = (assets_dir / "index.html").read_text(encoding="utf-8")
                html = html.replace("__AB_CSRF_TOKEN__", csrf).replace("__AB_LOCALE__", locale)
                self._send_bytes(200, "text/html; charset=utf-8", html.encode("utf-8"))
                return
            if path == "/assets/app.css":
                self._send_bytes(200, "text/css; charset=utf-8", (assets_dir / "app.css").read_bytes())
                return
            if path == "/assets/app.js":
                self._send_bytes(200, "application/javascript; charset=utf-8", (assets_dir / "app.js").read_bytes())
                return
            if path == "/assets/i18n.js":
                self._send_bytes(200, "application/javascript; charset=utf-8", (assets_dir / "i18n.js").read_bytes())
                return
            if path in ("/assets/locales/zh-CN.json", "/assets/locales/en.json"):
                fname = Path(path).name
                self._send_bytes(200, "application/json; charset=utf-8", (assets_dir / "locales" / fname).read_bytes())
                return
            if path == "/api/meta":
                self._send_json(200, {"ok": True, "locale": locale, "agents": [], "default_agents": [], "cwds": []})
                return
            if path == "/api/version":
                self._send_json(200, {"ok": True, "revision": rev, "locale": locale, "imported_receipts": 0})
                return
            if path == "/api/summary":
                self._send_json(200, {"ok": True, "overview": {"tasks": 0, "active_days": 0, "total_tokens": 0, "effective_cost_usd": None}, "daily": [], "by_model": [], "by_agent": []})
                return
            if path == "/api/pricing":
                self._send_json(200, {"ok": True, "config": {"models": {}, "harness_discounts": []}, "observed": []})
                return
            if path == "/api/service":
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "service": {
                            "running": True,
                            "pid": 1001,
                            "url": f"http://127.0.0.1:{self.server.server_address[1]}",
                            "uptime_sec": 120,
                            "windows_supported": True,
                            "tray_running": False,
                            "is_checkout": True,
                            "log_path": "synthetic/dashboard.log",
                            "config": {"auto_start": False, "tray_enabled": False, "port": self.server.server_address[1]},
                            "autostart": {"enabled": False, "matches_current": True},
                        },
                    },
                )
                return
            if path == "/api/storage":
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "storage": {
                            "ledger_bytes": 1024,
                            "record_count": 0,
                            "corrupt_lines": 0,
                            "receipts_on_disk": 0,
                            "earliest_ts": None,
                            "latest_ts": None,
                            "total_billing_bytes": 1024,
                            "billing_dir": "synthetic/billing",
                            "ledger_path": "synthetic/billing/usage_ledger.jsonl",
                            "pricing_path": "synthetic/billing/pricing.json",
                            "storage_policy_path": "synthetic/billing/storage.json",
                            "policy": {"max_bytes": 104857600, "max_age_days": 180, "auto_rotate": True},
                        },
                    },
                )
                return
            if path == "/api/agy/slots":
                self._send_json(200, slots_payload)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"not found: {path}"})

        def do_PUT(self) -> None:
            path = urlparse(self.path).path
            body = self._read_json()
            if path == "/api/preferences":
                with shared.lock:
                    shared.locale = body.get("locale", "zh-CN")
                    shared.requests.append({"method": "PUT", "path": path, "body": body})
                    loc = shared.locale
                self._send_json(200, {"ok": True, "preferences": {"locale": loc}})
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"not found: {path}"})

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            body = self._read_json()
            if path == "/api/agy/slots/cleanup":
                origin = (self.headers.get("Origin") or "").strip().lower()
                csrf_hdr = self.headers.get("X-Bridge-CSRF") or ""
                expected_origin = f"http://127.0.0.1:{self.server.server_address[1]}"
                with shared.lock:
                    shared.requests.append(
                        {
                            "method": "POST",
                            "path": path,
                            "origin": origin,
                            "csrf": csrf_hdr,
                            "body": body,
                        }
                    )
                    if csrf_hdr != shared.csrf or origin != expected_origin:
                        self._send_json(403, {"ok": False, "error": "csrf/origin mismatch"})
                        return
                    status = shared.next_cleanup_status
                    resp = json.loads(json.dumps(shared.next_cleanup_response))
                self._send_json(status, resp)
                return
            if path == "/__test__/control":
                with shared.lock:
                    if "slots_payload" in body:
                        shared.slots_payload = body["slots_payload"]
                    if "next_cleanup_status" in body:
                        shared.next_cleanup_status = int(body["next_cleanup_status"])
                    if "next_cleanup_response" in body:
                        shared.next_cleanup_response = body["next_cleanup_response"]
                    if "bump_revision" in body:
                        shared.revision = str(body["bump_revision"])
                    if body.get("clear_requests"):
                        shared.requests.clear()
                    snap = list(shared.requests)
                self._send_json(200, {"ok": True, "requests": snap})
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"not found: {path}"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, shared


_NODE_CDP_E2E = r"""
const assert = require("assert");
const fs = require("fs");

const wsUrl = process.argv[2];
const baseUrl = process.argv[3];
const screenshotDir = process.argv[4] || "";

async function main() {
  const ws = new WebSocket(wsUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve);
    ws.addEventListener("error", reject);
  });

  let nextId = 1;
  const pending = new Map();
  ws.addEventListener("message", (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(msg.error.message || JSON.stringify(msg.error)));
      else resolve(msg.result);
    }
  });

  const cdp = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const id = nextId++;
      pending.set(id, { resolve, reject });
      ws.send(JSON.stringify({ id, method, params }));
    });

  const evalJs = async (expression) => {
    const res = await cdp("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (res.exceptionDetails) {
      throw new Error("Page JS error: " + JSON.stringify(res.exceptionDetails));
    }
    return res.result ? res.result.value : undefined;
  };

  const control = async (payload) => {
    const r = await fetch(`${baseUrl}/__test__/control`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return r.json();
  };

  const waitFor = async (expr, timeoutMs = 6000) => {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const val = await evalJs(expr);
      if (val) return val;
      await new Promise((r) => setTimeout(r, 40));
    }
    throw new Error(`Timed out waiting for: ${expr}`);
  };

  const snap = async (filename) => {
    if (!screenshotDir) return;
    const { data } = await cdp("Page.captureScreenshot", { format: "png", fromSurface: true });
    fs.writeFileSync(`${screenshotDir}/${filename}`, Buffer.from(data, "base64"));
  };

  const checkNoHorizontalOverflow = async (viewportWidth, label) => {
    const metrics = await evalJs(`(() => {
      const panel = document.getElementById("agy-slots-panel");
      const pr = panel ? panel.getBoundingClientRect() : null;
      const rootOverflow = document.documentElement.scrollWidth - document.documentElement.clientWidth;
      const panelOverflow = panel ? (panel.scrollWidth - panel.clientWidth) : -1;
      const outOfBounds = [];
      if (panel) {
        const nodes = panel.querySelectorAll(".agy-slot-card, .agy-action-box, .agy-concept-card, .agy-row-card, .agy-notice, .agy-health-callout, input, button");
        for (const el of nodes) {
          const r = el.getBoundingClientRect();
          if (r.width === 0 && r.height === 0) continue;
          if (r.left < -1 || r.right > window.innerWidth + 1) {
            outOfBounds.push({ tag: el.tagName, id: el.id, cls: el.className, left: Math.round(r.left), right: Math.round(r.right) });
          }
        }
      }
      return {
        innerWidth: window.innerWidth,
        docScrollW: document.documentElement.scrollWidth,
        docClientW: document.documentElement.clientWidth,
        rootOverflow,
        panelOverflow,
        panelWidth: pr ? Math.round(pr.width) : 0,
        panelHeight: pr ? Math.round(pr.height) : 0,
        outOfBoundsCount: outOfBounds.length,
        outOfBounds: outOfBounds.slice(0, 5),
      };
    })()`);
    assert.ok(
      metrics.rootOverflow <= 1 && metrics.panelOverflow <= 1 && metrics.outOfBoundsCount === 0,
      `Horizontal overflow or clipped child at ${viewportWidth}px (${label}): ${JSON.stringify(metrics)}`
    );
    assert.ok(metrics.panelHeight > 80, `AGY panel collapsed unexpectedly: ${JSON.stringify(metrics)}`);
    return metrics;
  };

  await cdp("Page.enable");
  await cdp("Runtime.enable");
  await cdp("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
  await cdp("Page.navigate", { url: baseUrl });
  await waitFor(`document.documentElement.getAttribute("data-i18n-ready") === "1"`);

  // 1. Open Management tab -> sends GET /api/agy/slots only, renders disabled empty state
  await control({ clear_requests: true });
  await evalJs(`document.querySelector('.tab[data-tab="storage"]').click()`);
  await waitFor(`Boolean(document.querySelector('#agy-slots-root [data-agy-state="disabled"]'))`);
  let reqs = (await control({})).requests;
  assert.ok(reqs.some((r) => r.method === "GET" && r.path === "/api/agy/slots"));
  assert.strictEqual(reqs.filter((r) => r.method === "POST" && r.path === "/api/agy/slots/cleanup").length, 0);

  // 2. Switch to enabled 5-slot synthetic dataset + health alert + XSS payload, click manual Refresh
  const enabledData = {
    ok: true,
    enabled: true,
    rotation_tz: "Asia/Shanghai",
    grace_hours: 24,
    now: "2026-11-27T14:00:00+08:00",
    active_slot: "slotA",
    active_project_id: "11111111-1111-4111-8111-111111111111",
    cleaner: {
      conditional_delete_available: false,
      live_cleanup_available: true,
      live_block_reason: null,
      mode: "cli_run_command",
      model: "gemini-3-flash",
      available: true,
      reason: null,
    },
    state_error: null,
    receipt_error: null,
    slots: [
      { name: "slotA", project_id: "11111111-1111-4111-8111-111111111111", status: "active", is_active: true, days_since_active: 0, last_active_end: null, cooling_elapsed_hours: 0, grace_remaining_hours: 24, bound_sessions_count: 0, guard_error: null, unconfirmed_operation_id: null, blocked_sessions: [], project_file_exists: true, project_is_empty: true, last_cleanup: null },
      { name: "slotB", project_id: "22222222-2222-4222-8222-222222222222", status: "releasable", is_active: false, days_since_active: 2, last_active_end: "2026-11-25T23:59:59+08:00", cooling_elapsed_hours: 38, grace_remaining_hours: 0, bound_sessions_count: 0, guard_error: null, unconfirmed_operation_id: null, blocked_sessions: [], project_file_exists: true, project_is_empty: true, last_cleanup: null },
      { name: "slotC", project_id: "33333333-3333-4333-8333-333333333333", status: "cooling", is_active: false, days_since_active: 1, last_active_end: "2026-11-26T23:59:59+08:00", cooling_elapsed_hours: 14, grace_remaining_hours: 10, bound_sessions_count: 0, guard_error: null, unconfirmed_operation_id: null, blocked_sessions: [], project_file_exists: false, project_is_empty: false, last_cleanup: null },
      { name: "slotD", project_id: "44444444-4444-4444-8444-444444444444", status: "blocked", is_active: false, days_since_active: 3, last_active_end: "2026-11-24T23:59:59+08:00", cooling_elapsed_hours: 62, grace_remaining_hours: 0, bound_sessions_count: 1, guard_error: null, unconfirmed_operation_id: null, blocked_sessions: [{ session_id: "s-blk-1", native_session_id: "n-blk-1", proc_state: "running", last_active_at: "2026-11-27T13:00:00Z", reason: "active_task" }], project_file_exists: true, project_is_empty: true, last_cleanup: null },
      { name: "slotE", project_id: "55555555-5555-4555-8555-555555555555", status: "unknown", is_active: false, days_since_active: 4, last_active_end: "2026-11-23T23:59:59+08:00", cooling_elapsed_hours: 86, grace_remaining_hours: 0, bound_sessions_count: null, guard_error: "<b>raw</b> <a href='http://evil'>bad</a>", unconfirmed_operation_id: "op-unconfirmed-1", blocked_sessions: [], project_file_exists: true, project_is_empty: true, last_cleanup: null },
    ],
    last_cleanup: null,
  };
  await control({ slots_payload: enabledData, clear_requests: true });
  await evalJs(`document.getElementById("btn-agy-slots-refresh").click()`);
  await waitFor(`document.querySelectorAll('#agy-slots-root .agy-slot-card').length === 5`);
  reqs = (await control({})).requests;
  assert.strictEqual(reqs.filter((r) => r.path === "/api/agy/slots" && r.method === "GET").length, 1);
  assert.strictEqual(reqs.filter((r) => r.path === "/api/agy/slots/cleanup").length, 0);

  // Verify zero HTML injection in real browser DOM
  const injectedCount = await evalJs(`document.querySelectorAll('#agy-slots-root a, #agy-slots-root b, #agy-slots-root script, #agy-slots-root img').length`);
  assert.strictEqual(injectedCount, 0);

  // Select slotB (releasable), run dry-run preview -> type exact confirm -> open modal -> cancel -> reopen -> submit
  await evalJs(`document.querySelector('.agy-slot-card[data-slot-name="slotB"]').click()`);
  await waitFor(`document.getElementById("agy-slot-workbench")?.getAttribute("data-selected-slot") === "slotB"`);

  await control({
    next_cleanup_status: 200,
    next_cleanup_response: {
      ok: true, slot: "slotB", project_id: "22222222-2222-4222-8222-222222222222", dry_run: true,
      slot_status: "releasable", outcome: "completed", executed_at: "2026-11-27T14:01:00+08:00",
      counts_confirmed: true, receipt_saved: true, eligible_ids: ["c1", "c2"], deleted_ids: [],
      would_delete_ids: ["c1", "c2"], rejected_delete_ids: [], unconfirmed_delete_ids: [],
      checked: 3, deleted: 0, would_delete: 2, skipped_running: 0, skipped_recent: 1, skipped_unverified: 0,
      cutoff_utc: "2026-11-26T06:00:00Z", batches: [], pending_batch_number: null, stop_reason: "preview",
      http_200_ids: [], remaining_ids: [], has_more: false, worker_elapsed_ms: 15500,
      native_removed: 0, windows_residual_reclaimed: 0, residual_locked: 0, errors: [],
    },
    clear_requests: true,
  });
  await evalJs(`document.getElementById("btn-agy-preview").click()`);
  await waitFor(`Boolean(document.querySelector('[data-agy-receipt="current"]'))`);

  // Wrong input keeps button disabled
  await evalJs(`(() => { const i = document.getElementById("agy-confirm-input"); i.value = "wrong"; i.dispatchEvent(new Event("input", { bubbles: true })); })()`);
  assert.strictEqual(await evalJs(`document.getElementById("btn-agy-live-open").disabled`), true);

  // Exact input unlocks button -> open modal -> cancel -> still 0 live POSTs
  await evalJs(`(() => { const i = document.getElementById("agy-confirm-input"); i.value = "slotB"; i.dispatchEvent(new Event("input", { bubbles: true })); })()`);
  assert.strictEqual(await evalJs(`document.getElementById("btn-agy-live-open").disabled`), false);
  await evalJs(`document.getElementById("btn-agy-live-open").click()`);
  await waitFor(`!document.getElementById("agy-cleanup-modal-backdrop").classList.contains("hidden")`);
  await snap("desktop-zh-modal.png");
  await evalJs(`document.getElementById("btn-agy-modal-cancel").click()`);
  assert.strictEqual(await evalJs(`document.getElementById("agy-cleanup-modal-backdrop").classList.contains("hidden")`), true);

  // Reopen & submit live cleanup -> return partial + residual_locked + tri-state null/[]/items
  await control({
    next_cleanup_status: 200,
    next_cleanup_response: {
      ok: false, slot: "slotB", project_id: "22222222-2222-4222-8222-222222222222", dry_run: false,
      slot_status: "releasable", outcome: "partial", executed_at: "2026-11-27T14:03:00+08:00",
      counts_confirmed: true, receipt_saved: true, eligible_ids: ["c1", "c2", "c3"], deleted_ids: ["c1", "c2"],
      would_delete_ids: [], rejected_delete_ids: [], unconfirmed_delete_ids: ["c3"],
      checked: 3, deleted: 2, would_delete: 0, skipped_running: 0, skipped_recent: 0, skipped_unverified: 0,
      cutoff_utc: "2026-11-26T06:00:00Z",
      batches: [{
        batch_number: 1, ok: false, outcome_unknown: false, http_200_ids: ["c1", "c2"], worker_elapsed_ms: 19400, errors: ["residual_locked"],
        residuals: [
          { id: "c1", status: "native_removed", reason: null, moved: [], remaining: [] },
          { id: "c2", status: "windows_residual_reclaimed", reason: null, moved: ["db", "brain"], remaining: [] },
          { id: "c3", status: "residual_locked", reason: "agy_cli_running", moved: null, remaining: null },
        ],
      }],
      pending_batch_number: null, stop_reason: "residual_locked", http_200_ids: ["c1", "c2"], remaining_ids: ["c3"],
      has_more: true, worker_elapsed_ms: 19400, native_removed: 1, windows_residual_reclaimed: 1, residual_locked: 1,
      errors: ["residual_locked"],
    },
    clear_requests: true,
  });
  await evalJs(`document.getElementById("btn-agy-live-open").click()`);
  await evalJs(`document.getElementById("btn-agy-modal-confirm").click()`);
  await waitFor(`document.querySelector('[data-agy-receipt="current"]')?.getAttribute("data-agy-stop-reason") === "residual_locked"`);

  reqs = (await control({})).requests;
  const livePosts = reqs.filter((r) => r.method === "POST" && r.path === "/api/agy/slots/cleanup" && r.body.dry_run === false);
  assert.strictEqual(livePosts.length, 1);
  assert.strictEqual(livePosts[0].body.slot, "slotB");
  assert.strictEqual(livePosts[0].body.dry_run, false);
  assert.strictEqual(livePosts[0].body.confirm, "slotB");
  assert.strictEqual(livePosts[0].csrf, "synthetic-e2e-csrf-999");

  await checkNoHorizontalOverflow(1280, "desktop-zh-partial");
  await snap("desktop-zh-partial.png");

  // 3. Verify 375px narrow screen in ZH (with partial receipt & tri-state badges)
  await cdp("Emulation.setDeviceMetricsOverride", { width: 375, height: 812, deviceScaleFactor: 2, mobile: true });
  await new Promise((r) => setTimeout(r, 80));
  const m375Zh = await checkNoHorizontalOverflow(375, "mobile-375-zh");
  await snap("mobile-375-zh.png");

  // 4. Switch to English via real EN button, toggle light theme, and verify 375px narrow screen + 409 modal bounds
  await evalJs(`document.getElementById("btn-lang-en").click()`);
  await waitFor(`document.documentElement.lang === "en" && document.getElementById("agy-slots-state-badge").textContent === "Enabled"`);
  await new Promise((r) => setTimeout(r, 80));
  const m375En = await checkNoHorizontalOverflow(375, "mobile-375-en");
  await snap("mobile-375-en.png");

  // Verify English tri-state & 4-concept texts in real browser DOM
  const domTextEn = await evalJs(`document.getElementById("agy-slots-root").textContent`);
  assert.ok(domTextEn.includes("Unknown (null)"));
  assert.ok(domTextEn.includes("0 (known empty [])"));
  assert.ok(domTextEn.includes("does NOT count as freed disk space"));
  assert.ok(domTextEn.includes("Cleanup round NOT completed"));
  assert.ok(!domTextEn.includes("Cleanup round completed"));

  // Check 409 blocking_processes + light theme + 375px modal geometry in real browser
  await evalJs(`document.getElementById("btn-theme").click()`);
  await control({
    next_cleanup_status: 200,
    next_cleanup_response: {
      ok: true, slot: "slotB", project_id: "22222222-2222-4222-8222-222222222222", dry_run: true,
      slot_status: "releasable", outcome: "completed", executed_at: "2026-11-27T14:04:00+08:00",
      counts_confirmed: true, receipt_saved: true, eligible_ids: ["c1"], deleted_ids: [],
      would_delete_ids: ["c1"], rejected_delete_ids: [], unconfirmed_delete_ids: [],
      checked: 1, deleted: 0, would_delete: 1, skipped_running: 0, skipped_recent: 0, skipped_unverified: 0,
      cutoff_utc: "2026-11-26T06:00:00Z", batches: [], pending_batch_number: null, stop_reason: "preview",
      http_200_ids: [], remaining_ids: [], has_more: false, worker_elapsed_ms: 15100,
      native_removed: 0, windows_residual_reclaimed: 0, residual_locked: 0, errors: [],
    },
  });
  await evalJs(`document.getElementById("btn-agy-preview").click()`);
  await waitFor(`document.querySelector('[data-agy-receipt="current"]')?.getAttribute("data-agy-stop-reason") === "preview"`);
  await evalJs(`(() => { const i = document.getElementById("agy-confirm-input"); i.value = "slotB"; i.dispatchEvent(new Event("input", { bubbles: true })); })()`);
  await evalJs(`document.getElementById("btn-agy-live-open").click()`);
  await waitFor(`!document.getElementById("agy-cleanup-modal-backdrop").classList.contains("hidden")`);
  const modalBox = await evalJs(`(() => {
    const m = document.querySelector("#agy-cleanup-modal-backdrop .modal");
    const r = m.getBoundingClientRect();
    return { left: Math.round(r.left), right: Math.round(r.right), width: Math.round(r.width), height: Math.round(r.height), overflowX: m.scrollWidth - m.clientWidth };
  })()`);
  assert.ok(modalBox.left >= 0 && modalBox.right <= 376 && modalBox.height <= 812 && modalBox.overflowX <= 1, `375px modal overflow: ${JSON.stringify(modalBox)}`);
  await snap("mobile-375-en-light-modal.png");

  await control({
    next_cleanup_status: 409,
    next_cleanup_response: {
      ok: false, slot: "slotB", project_id: "22222222-2222-4222-8222-222222222222", dry_run: false,
      slot_status: "releasable", outcome: "not_started", executed_at: "2026-11-27T14:05:00+08:00",
      counts_confirmed: true, receipt_saved: false, eligible_ids: [], deleted_ids: [], would_delete_ids: [],
      rejected_delete_ids: [], unconfirmed_delete_ids: [], checked: 0, deleted: 0, would_delete: 0,
      skipped_running: 0, skipped_recent: 0, skipped_unverified: 0,
      blocked_sessions: [], blocking_processes: [{ pid: 4321, name: "agy.exe" }], errors: ["agy_cli_running"],
    },
  });
  await evalJs(`document.getElementById("btn-agy-modal-confirm").click()`);
  await waitFor(`document.querySelector('[data-agy-receipt="current"]')?.getAttribute("data-agy-http-status") === "409"`);
  const text409 = await evalJs(`document.getElementById("agy-slots-root").textContent`);
  assert.ok(text409.includes("4321") && text409.includes("agy.exe") && text409.includes("will not terminate processes for you"));
  await checkNoHorizontalOverflow(375, "mobile-375-en-light-409");
  await snap("mobile-375-en-light-409.png");

  // 5. Verify topbar manual refresh on Management tab sends GET /api/agy/slots only (0 POSTs)
  await control({ clear_requests: true });
  await evalJs(`document.getElementById("btn-refresh").click()`);
  await new Promise((r) => setTimeout(r, 250));
  reqs = (await control({})).requests;
  assert.ok(reqs.some((r) => r.method === "GET" && r.path === "/api/agy/slots"));
  assert.strictEqual(reqs.filter((r) => r.method === "POST" && r.path === "/api/agy/slots/cleanup").length, 0);

  // 6. Verify auto-polling revision bump does NOT trigger GET /api/agy/slots
  await evalJs(`(() => { const sel = document.getElementById("auto-refresh-select"); sel.value = "5000"; sel.dispatchEvent(new Event("change", { bubbles: true })); })()`);
  await control({ bump_revision: "rev-2", clear_requests: true });
  // Trigger visibilitychange check (same handler used by auto-refresh timer)
  await evalJs(`document.dispatchEvent(new Event("visibilitychange"))`);
  await new Promise((r) => setTimeout(r, 350));
  reqs = (await control({})).requests;
  assert.strictEqual(
    reqs.filter((r) => r.path === "/api/agy/slots").length,
    0,
    `Auto-refresh must NOT poll /api/agy/slots, saw: ${JSON.stringify(reqs)}`
  );
  assert.strictEqual(reqs.filter((r) => r.path === "/api/agy/slots/cleanup").length, 0);

  ws.close();
  process.stdout.write(JSON.stringify({ ok: true, m375Zh, m375En }));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""


def test_agy_slots_real_browser_e2e_bilingual_and_375px_viewport():
    browser = _find_chromium_binary()
    node = shutil.which("node")
    if not browser or not node:
        pytest.skip("Chromium/Edge and Node are required for real browser E2E test")

    server, thread, _shared = _start_synthetic_server()
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"

    # Reserve a free localhost port for CDP
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        cdp_port = s.getsockname()[1]

    screenshot_dir = os.environ.get("AB_AGY_SLOTS_SCREENSHOT_DIR", "")
    if screenshot_dir:
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)

    user_data_dir = tempfile.mkdtemp(prefix="ab-agy-cdp-")
    chrome_proc = subprocess.Popen(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={user_data_dir}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ws_url = None
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/list", timeout=1) as resp:
                    targets = json.loads(resp.read().decode("utf-8"))
                page_target = next((t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
                if page_target:
                    ws_url = page_target["webSocketDebuggerUrl"]
                    break
            except Exception:
                time.sleep(0.15)
        assert ws_url is not None, "Headless Chromium did not expose CDP page target in time"

        with tempfile.TemporaryDirectory(prefix="ab-agy-cdp-js-") as td:
            cdp_script = Path(td) / "cdp_e2e.cjs"
            cdp_script.write_text(_NODE_CDP_E2E, encoding="utf-8")
            completed = subprocess.run(
                [node, str(cdp_script), ws_url, base_url, screenshot_dir],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=45,
                check=False,
            )
        assert completed.returncode == 0, f"Browser E2E failed:\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        parsed = json.loads(completed.stdout)
        assert parsed["ok"] is True
        assert parsed["m375Zh"]["rootOverflow"] <= 1
        assert parsed["m375Zh"]["panelOverflow"] <= 1
        assert parsed["m375En"]["rootOverflow"] <= 1
        assert parsed["m375En"]["panelOverflow"] <= 1
    finally:
        chrome_proc.terminate()
        try:
            chrome_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            chrome_proc.kill()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(user_data_dir, ignore_errors=True)
