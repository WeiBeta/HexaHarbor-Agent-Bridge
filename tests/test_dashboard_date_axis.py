from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_bridge import dashboard_server
from agent_bridge.billing import compute_summary, filter_records
from agent_bridge.billing_pricing import ModelRate, PricingConfig
from agent_bridge.billing_store import append_receipt_record, load_ledger, save_pricing
from agent_bridge.dashboard_server import create_dashboard_server
from agent_bridge.paths import dashboard_assets_dir, usage_ledger_path

_NODE_RENDER_HARNESS = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const assetsDir = process.argv[1];
const payload = JSON.parse(fs.readFileSync(0, "utf8"));
const catalogs = {
  "zh-CN": JSON.parse(fs.readFileSync(path.join(assetsDir, "locales", "zh-CN.json"), "utf8")),
  "en": JSON.parse(fs.readFileSync(path.join(assetsDir, "locales", "en.json"), "utf8")),
};

function unescapeHtml(str) {
  return String(str ?? "")
    .replaceAll("&quot;", '"')
    .replaceAll("&#39;", "'")
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">")
    .replaceAll("&amp;", "&");
}

function stripTags(html) {
  return unescapeHtml(String(html ?? "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim());
}

function parseAttrs(attrText) {
  const attrs = {};
  const re = /([a-zA-Z0-9_-]+)="([^"]*)"/g;
  let m;
  while ((m = re.exec(attrText)) !== null) {
    attrs[m[1]] = unescapeHtml(m[2]);
  }
  return attrs;
}

function makeClassList(initial = []) {
  const set = new Set(initial);
  return {
    add: (...cls) => cls.forEach((c) => set.add(c)),
    remove: (...cls) => cls.forEach((c) => set.delete(c)),
    toggle: (c, force) => {
      if (force === undefined) {
        if (set.has(c)) set.delete(c);
        else set.add(c);
      } else if (force) {
        set.add(c);
      } else {
        set.delete(c);
      }
    },
    contains: (c) => set.has(c),
    toArray: () => Array.from(set),
  };
}

function parseButtons(stageHtml) {
  const buttons = [];
  const btnRe = /<button\b([^>]*)>([\s\S]*?)<\/button>/g;
  let m;
  while ((m = btnRe.exec(stageHtml)) !== null) {
    const attrs = parseAttrs(m[1]);
    const inner = m[2];
    const stackMatch = /<div\s+class="([^"]*bar-stack[^"]*)"\s+style="([^"]*)">([\s\S]*?)<\/div>/.exec(inner);
    const dateMatch = /<span\s+class="bar-date">([^<]*)<\/span>/.exec(inner);
    const listeners = {};
    buttons.push({
      attrs,
      stackClass: stackMatch ? unescapeHtml(stackMatch[1]) : null,
      stackStyle: stackMatch ? unescapeHtml(stackMatch[2]) : null,
      stackInner: stackMatch ? stackMatch[3].trim() : "",
      dateLabel: dateMatch ? unescapeHtml(dateMatch[1]) : null,
      getAttribute: (name) => (Object.prototype.hasOwnProperty.call(attrs, name) ? attrs[name] : null),
      addEventListener: (type, fn) => {
        if (!listeners[type]) listeners[type] = [];
        listeners[type].push(fn);
      },
      dispatch: (type, ev = {}) => {
        for (const fn of listeners[type] || []) fn(ev);
      },
    });
  }
  return buttons;
}

function parseEmptyElements(stageHtml) {
  const out = [];
  const re = /<div\s+class="chart-empty"([^>]*)>([\s\S]*?)<\/div>/g;
  let m;
  while ((m = re.exec(stageHtml)) !== null) {
    out.push({
      attrs: parseAttrs(m[1]),
      text: stripTags(m[2]),
    });
  }
  return out;
}

const windowObj = {
  __AB_DISABLE_AUTO_BOOT__: true,
  innerWidth: 1280,
};

const documentObj = {
  documentElement: { lang: "zh-CN", setAttribute() {} },
  title: "",
  querySelector: () => null,
  querySelectorAll: () => [],
  getElementById: () => null,
};

const sandbox = {
  window: windowObj,
  document: documentObj,
  localStorage: {
    getItem: () => null,
    setItem: () => {},
  },
  fetch: async (url) => {
    const loc = url.includes("en.json") ? "en" : "zh-CN";
    return { ok: true, status: 200, json: async () => catalogs[loc] };
  },
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

(async () => {
  const i18n = sandbox.window.AbI18n;
  const chartApi = sandbox.window.AbDashboardDailyChart;
  await i18n.init("zh-CN");

  const results = [];
  for (const job of payload.jobs) {
    await i18n.setLocale(job.locale);
    const summaryCopy = JSON.parse(JSON.stringify(job.summary));
    const originalDailySnapshot = JSON.stringify(summaryCopy.daily || []);

    let parsedButtons = [];
    const stage = {
      _html: "",
      set innerHTML(val) {
        this._html = String(val);
        parsedButtons = parseButtons(this._html);
      },
      get innerHTML() {
        return this._html;
      },
      querySelectorAll(sel) {
        if (sel === ".bar-col") return parsedButtons;
        return [];
      },
    };
    const legend = { innerHTML: "" };
    const tooltip = {
      innerHTML: "",
      style: {},
      offsetWidth: 220,
      offsetHeight: 140,
      classList: makeClassList(["hidden"]),
    };
    const clickedDates = [];

    chartApi.renderDailyBarChart({
      summary: summaryCopy,
      metric: job.metric || "tokens",
      stage,
      legend,
      tooltip,
      onSelectDate: (d) => clickedDates.push(d),
    });

    const inspectDates = job.inspectDates || parsedButtons.map((b) => b.attrs["data-date"]);
    const tooltipsByDate = {};
    for (const dateStr of inspectDates) {
      const btn = parsedButtons.find((b) => b.attrs["data-date"] === dateStr);
      if (!btn) continue;
      btn.dispatch("pointerenter", { clientX: 200, clientY: 160 });
      tooltipsByDate[dateStr] = {
        hiddenAfterEnter: tooltip.classList.contains("hidden"),
        html: tooltip.innerHTML,
        text: stripTags(tooltip.innerHTML),
      };
      btn.dispatch("pointerleave", {});
    }

    for (const dateStr of job.clickDates || []) {
      const btn = parsedButtons.find((b) => b.attrs["data-date"] === dateStr);
      if (btn) btn.dispatch("click", {});
    }

    results.push({
      id: job.id,
      locale: job.locale,
      metric: job.metric || "tokens",
      dailyMutated: JSON.stringify(summaryCopy.daily || []) !== originalDailySnapshot,
      legendText: stripTags(legend.innerHTML),
      emptyElements: parseEmptyElements(stage.innerHTML),
      bars: parsedButtons.map((b) => ({
        date: b.attrs["data-date"],
        dayState: b.attrs["data-day-state"],
        empty: b.attrs["data-empty"],
        usageUnknown: b.attrs["data-usage-unknown"],
        ariaLabel: b.attrs["aria-label"],
        ariaDisabled: b.attrs["aria-disabled"] || null,
        stackClass: b.stackClass,
        stackStyle: b.stackStyle,
        stackInner: b.stackInner,
        dateLabel: b.dateLabel,
      })),
      tooltipsByDate,
      clickedDates,
    });
  }

  process.stdout.write(JSON.stringify({ ok: true, results }));
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""


def _json_get(port: int, path: str) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, json.loads(raw.decode("utf-8"))
    finally:
        conn.close()


def _run_chart_jobs(jobs: list[dict[str, Any]], *, node_tz: str = "America/Los_Angeles") -> dict[str, dict[str, Any]]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to execute dashboard chart UI tests")
    env = os.environ.copy()
    env["TZ"] = node_tz
    completed = subprocess.run(
        [node, "-e", _NODE_RENDER_HARNESS, str(dashboard_assets_dir())],
        input=json.dumps({"jobs": jobs}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    parsed = json.loads(completed.stdout)
    assert parsed["ok"] is True
    return {item["id"]: item for item in parsed["results"]}


def _seed_pricing(home: Path) -> PricingConfig:
    cfg = PricingConfig(
        models={
            "demo-axis-model": ModelRate(input=1.0, output=2.0, cache_read=0.0, cache_write=0.0),
        }
    )
    save_pricing(cfg, home)
    return cfg


def _append_task(
    home: Path,
    *,
    task_id: str,
    started_at: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    agent: str = "antigravity",
    model: str = "demo-axis-model",
) -> None:
    receipt: dict[str, Any] = {
        "task_id": task_id,
        "session_id": f"sess_{task_id}",
        "agent": agent,
        "cwd": "C:/Dev/Demo",
        "title": f"Task {task_id}",
        "status": "completed",
        "model": model,
        "created_at": started_at,
        "started_at": started_at,
        "finished_at": started_at,
    }
    if input_tokens is not None or output_tokens is not None:
        receipt["usage"] = {
            "input_tokens": input_tokens or 0,
            "output_tokens": output_tokens or 0,
            "total_tokens": (input_tokens or 0) + (output_tokens or 0),
            "scope": "turn",
        }
    append_receipt_record(home, receipt)


def _start_server(home: Path):
    server = create_dashboard_server(home=home, host="127.0.0.1", port=0, backfill=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop_server(server, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_summary_7d_missing_day_21_preserves_totals_and_renders_bilingual_slots(tmp_path: Path, monkeypatch):
    fixed_now = datetime(2026, 11, 24, 16, 30, 0, tzinfo=timezone(timedelta(hours=8)))
    monkeypatch.setattr(dashboard_server, "_current_local_now", lambda: fixed_now)

    pricing = _seed_pricing(tmp_path)
    # Six active days across 2026-11-18..2026-11-24 with 2026-11-21 missing, plus one earlier day outside 7d.
    _append_task(tmp_path, task_id="t_old", started_at="2026-11-10T10:00:00+08:00", input_tokens=999_000, output_tokens=1_000)
    daily_specs = [
        ("2026-11-18", 100_000, 50_000),  # 150k tokens, $0.20
        ("2026-11-19", 200_000, 100_000),  # 300k tokens, $0.40
        ("2026-11-20", 300_000, 150_000),  # 450k tokens, $0.60
        ("2026-11-22", 400_000, 200_000),  # 600k tokens, $0.80
        ("2026-11-23", 500_000, 250_000),  # 750k tokens, $1.00
        ("2026-11-24", 600_000, 300_000),  # 900k tokens, $1.20
    ]
    for idx, (day, inp, out) in enumerate(daily_specs, start=1):
        _append_task(
            tmp_path,
            task_id=f"t_d{idx}",
            started_at=f"{day}T12:00:00+08:00",
            input_tokens=inp,
            output_tokens=out,
        )

    ledger_before = usage_ledger_path(tmp_path).read_text(encoding="utf-8")
    records, _ = load_ledger(tmp_path)
    baseline = compute_summary(filter_records(records, range_name="7d", now=fixed_now), pricing, now=fixed_now)

    server, thread = _start_server(tmp_path)
    try:
        status, summary = _json_get(server.bound_port, "/api/summary?range=7d")
    finally:
        _stop_server(server, thread)

    assert status == 200 and summary["ok"] is True
    # Ledger and billing aggregates must be completely unchanged.
    assert usage_ledger_path(tmp_path).read_text(encoding="utf-8") == ledger_before
    assert summary["daily_axis"] == {
        "range": "7d",
        "start_date": "2026-11-18",
        "end_date": "2026-11-24",
        "days": 7,
    }
    assert summary["overview"] == baseline["overview"]
    assert summary["daily"] == baseline["daily"]
    assert summary["overview"]["tasks"] == 6
    assert summary["overview"]["active_days"] == 6
    assert summary["overview"]["total_tokens"] == 3_150_000
    assert summary["overview"]["effective_cost_usd"] == pytest.approx(4.20, rel=1e-6)
    assert [d["date"] for d in summary["daily"]] == [
        "2026-11-18",
        "2026-11-19",
        "2026-11-20",
        "2026-11-22",
        "2026-11-23",
        "2026-11-24",
    ]

    rendered = _run_chart_jobs(
        [
            {
                "id": "zh",
                "locale": "zh-CN",
                "metric": "tokens",
                "summary": summary,
                "inspectDates": ["2026-11-20", "2026-11-21"],
                "clickDates": ["2026-11-21", "2026-11-20"],
            },
            {
                "id": "en",
                "locale": "en",
                "metric": "tokens",
                "summary": summary,
                "inspectDates": ["2026-11-20", "2026-11-21"],
                "clickDates": ["2026-11-21", "2026-11-20"],
            },
        ],
        node_tz="America/Los_Angeles",
    )

    expected_dates = [
        "2026-11-18",
        "2026-11-19",
        "2026-11-20",
        "2026-11-21",
        "2026-11-22",
        "2026-11-23",
        "2026-11-24",
    ]
    for loc, empty_hint, active_aria in (
        ("zh", "无匹配任务", "2026-11-20\uff0c点击查看当日明细"),
        ("en", "No matching tasks", "2026-11-20, view daily records"),
    ):
        res = rendered[loc]
        assert res["dailyMutated"] is False
        assert res["emptyElements"] == []
        assert [b["date"] for b in res["bars"]] == expected_dates
        assert [b["dateLabel"] for b in res["bars"]] == ["11-18", "11-19", "11-20", "11-21", "11-22", "11-23", "11-24"]

        active_bars = [b for b in res["bars"] if b["empty"] == "0"]
        empty_bars = [b for b in res["bars"] if b["empty"] == "1"]
        assert len(active_bars) == 6
        assert [b["date"] for b in empty_bars] == ["2026-11-21"]

        gap_bar = empty_bars[0]
        assert gap_bar["dayState"] == "empty"
        assert gap_bar["ariaDisabled"] == "true"
        assert gap_bar["stackStyle"] == "height:0;min-height:0"
        assert gap_bar["stackInner"] == ""
        assert empty_hint in gap_bar["ariaLabel"]

        gap_tip = res["tooltipsByDate"]["2026-11-21"]
        assert gap_tip["hiddenAfterEnter"] is False
        assert "2026-11-21" in gap_tip["text"]
        assert empty_hint in gap_tip["text"]

        day20_bar = next(b for b in res["bars"] if b["date"] == "2026-11-20")
        assert day20_bar["dayState"] == "active"
        assert day20_bar["ariaLabel"] == active_aria
        assert day20_bar["stackStyle"].startswith("height:")
        assert not day20_bar["stackStyle"].startswith("height:0")
        assert "450,000" in res["tooltipsByDate"]["2026-11-20"]["text"]
        assert "$0.600" in res["tooltipsByDate"]["2026-11-20"]["text"]

        # Clicking the empty day must not navigate; clicking the active day navigates.
        assert res["clickedDates"] == ["2026-11-20"]


def test_summary_7d_empty_data_keeps_7_zero_height_slots_and_bilingual_hint(tmp_path: Path, monkeypatch):
    fixed_now = datetime(2026, 11, 24, 16, 30, 0, tzinfo=timezone(timedelta(hours=8)))
    monkeypatch.setattr(dashboard_server, "_current_local_now", lambda: fixed_now)

    server, thread = _start_server(tmp_path)
    try:
        status, summary = _json_get(server.bound_port, "/api/summary?range=7d")
        status_30, summary_30 = _json_get(server.bound_port, "/api/summary?range=30d")
    finally:
        _stop_server(server, thread)

    assert status == 200 and summary["ok"] is True
    assert status_30 == 200 and summary_30["ok"] is True
    assert summary["daily"] == []
    assert summary["overview"]["tasks"] == 0
    assert summary["overview"]["active_days"] == 0
    assert summary["overview"]["total_tokens"] == 0
    assert summary["overview"]["effective_cost_usd"] is None
    assert summary["daily_axis"] == {
        "range": "7d",
        "start_date": "2026-11-18",
        "end_date": "2026-11-24",
        "days": 7,
    }
    assert summary_30["daily_axis"] == {
        "range": "30d",
        "start_date": "2026-10-26",
        "end_date": "2026-11-24",
        "days": 30,
    }

    rendered = _run_chart_jobs(
        [
            {
                "id": "zh",
                "locale": "zh-CN",
                "metric": "tokens",
                "summary": summary,
                "inspectDates": ["2026-11-18", "2026-11-24"],
                "clickDates": ["2026-11-18", "2026-11-24"],
            },
            {
                "id": "en",
                "locale": "en",
                "metric": "cost",
                "summary": summary,
                "inspectDates": ["2026-11-18", "2026-11-24"],
                "clickDates": ["2026-11-18", "2026-11-24"],
            },
            {
                "id": "zh_30",
                "locale": "zh-CN",
                "metric": "tasks",
                "summary": summary_30,
                "inspectDates": ["2026-10-26", "2026-11-24"],
            },
            {
                "id": "en_30",
                "locale": "en",
                "metric": "tokens",
                "summary": summary_30,
                "inspectDates": ["2026-10-26", "2026-11-24"],
            },
        ]
    )

    for loc, expected_hint in (("zh", "无匹配任务"), ("en", "No matching tasks")):
        res = rendered[loc]
        assert len(res["bars"]) == 7
        assert [b["date"] for b in res["bars"]] == [
            "2026-11-18",
            "2026-11-19",
            "2026-11-20",
            "2026-11-21",
            "2026-11-22",
            "2026-11-23",
            "2026-11-24",
        ]
        assert [b["dateLabel"] for b in res["bars"]] == ["11-18", "11-19", "11-20", "11-21", "11-22", "11-23", "11-24"]
        assert all(b["empty"] == "1" for b in res["bars"])
        assert all(b["stackStyle"] == "height:0;min-height:0" for b in res["bars"])
        assert all(b["stackInner"] == "" for b in res["bars"])
        assert all(expected_hint in b["ariaLabel"] for b in res["bars"])
        assert len(res["emptyElements"]) == 1
        assert res["emptyElements"][0]["attrs"].get("data-chart-empty-overlay") == "1"
        assert res["emptyElements"][0]["text"] == expected_hint
        assert expected_hint in res["tooltipsByDate"]["2026-11-18"]["text"]
        assert expected_hint in res["tooltipsByDate"]["2026-11-24"]["text"]
        assert res["clickedDates"] == []

        res30 = rendered[f"{loc}_30"]
        assert len(res30["bars"]) == 30
        assert all(b["empty"] == "1" and b["stackStyle"] == "height:0;min-height:0" for b in res30["bars"])
        assert res30["emptyElements"][0]["text"] == expected_hint


def test_summary_30d_continuous_axis_thins_labels_without_losing_positions(tmp_path: Path, monkeypatch):
    fixed_now = datetime(2026, 11, 24, 16, 30, 0, tzinfo=timezone(timedelta(hours=8)))
    monkeypatch.setattr(dashboard_server, "_current_local_now", lambda: fixed_now)

    _seed_pricing(tmp_path)
    # 2026-10-25 is 31 days ago (excluded); 10-26, 11-05, 11-24 are inside 30d.
    _append_task(tmp_path, task_id="t_before", started_at="2026-10-25T23:00:00+08:00", input_tokens=50_000, output_tokens=50_000)
    _append_task(tmp_path, task_id="t_first", started_at="2026-10-26T09:00:00+08:00", input_tokens=100_000, output_tokens=50_000)
    _append_task(tmp_path, task_id="t_mid", started_at="2026-11-05T14:00:00+08:00", input_tokens=200_000, output_tokens=100_000)
    _append_task(tmp_path, task_id="t_today", started_at="2026-11-24T15:00:00+08:00", input_tokens=300_000, output_tokens=150_000)

    server, thread = _start_server(tmp_path)
    try:
        status, summary = _json_get(server.bound_port, "/api/summary?range=30d")
    finally:
        _stop_server(server, thread)

    assert status == 200 and summary["ok"] is True
    assert summary["daily_axis"] == {
        "range": "30d",
        "start_date": "2026-10-26",
        "end_date": "2026-11-24",
        "days": 30,
    }
    assert summary["overview"]["tasks"] == 3
    assert summary["overview"]["active_days"] == 3
    assert summary["overview"]["total_tokens"] == 900_000
    assert summary["overview"]["effective_cost_usd"] == pytest.approx(1.20, rel=1e-6)
    assert len(summary["daily"]) == 3

    rendered = _run_chart_jobs(
        [
            {"id": "zh", "locale": "zh-CN", "metric": "tokens", "summary": summary, "inspectDates": ["2026-10-27", "2026-11-05"]},
            {"id": "en", "locale": "en", "metric": "tokens", "summary": summary, "inspectDates": ["2026-10-27", "2026-11-05"]},
        ]
    )

    start_day = datetime(2026, 10, 26).date()
    expected_30 = [(start_day + timedelta(days=i)).isoformat() for i in range(30)]
    for loc, expected_hint in (("zh", "无匹配任务"), ("en", "No matching tasks")):
        res = rendered[loc]
        assert len(res["bars"]) == 30
        assert [b["date"] for b in res["bars"]] == expected_30
        visible_labels = [b["dateLabel"] for b in res["bars"] if b["dateLabel"] is not None]
        assert 1 < len(visible_labels) < 30
        assert visible_labels[0] == "10-26"
        assert visible_labels[-1] == "11-24"
        assert [b["date"] for b in res["bars"] if b["empty"] == "0"] == ["2026-10-26", "2026-11-05", "2026-11-24"]
        assert len([b for b in res["bars"] if b["empty"] == "1"]) == 27
        assert expected_hint in res["tooltipsByDate"]["2026-10-27"]["text"]
        assert "300,000" in res["tooltipsByDate"]["2026-11-05"]["text"]


def test_unmeasured_task_day_is_distinct_from_empty_day_in_zh_and_en(tmp_path: Path, monkeypatch):
    fixed_now = datetime(2026, 11, 24, 16, 30, 0, tzinfo=timezone(timedelta(hours=8)))
    monkeypatch.setattr(dashboard_server, "_current_local_now", lambda: fixed_now)

    _seed_pricing(tmp_path)
    # 11-20: measured task; 11-21: empty; 11-22: 2 tasks with unknown usage (usage omitted).
    _append_task(tmp_path, task_id="t_meas", started_at="2026-11-20T10:00:00+08:00", input_tokens=100_000, output_tokens=50_000)
    _append_task(tmp_path, task_id="t_unk_1", started_at="2026-11-22T09:00:00+08:00")
    _append_task(tmp_path, task_id="t_unk_2", started_at="2026-11-22T15:00:00+08:00")

    server, thread = _start_server(tmp_path)
    try:
        status, summary = _json_get(server.bound_port, "/api/summary?range=7d")
    finally:
        _stop_server(server, thread)

    assert status == 200 and summary["ok"] is True
    assert summary["overview"]["tasks"] == 3
    assert summary["overview"]["measured_tasks"] == 1
    assert summary["overview"]["unmeasured_tasks"] == 2
    assert summary["overview"]["active_days"] == 2
    assert [d["date"] for d in summary["daily"]] == ["2026-11-20", "2026-11-22"]

    rendered = _run_chart_jobs(
        [
            {
                "id": "zh_tokens",
                "locale": "zh-CN",
                "metric": "tokens",
                "summary": summary,
                "inspectDates": ["2026-11-21", "2026-11-22"],
                "clickDates": ["2026-11-21", "2026-11-22"],
            },
            {
                "id": "en_tokens",
                "locale": "en",
                "metric": "tokens",
                "summary": summary,
                "inspectDates": ["2026-11-21", "2026-11-22"],
                "clickDates": ["2026-11-21", "2026-11-22"],
            },
            {
                "id": "zh_tasks",
                "locale": "zh-CN",
                "metric": "tasks",
                "summary": summary,
                "inspectDates": ["2026-11-21", "2026-11-22"],
            },
            {
                "id": "en_cost",
                "locale": "en",
                "metric": "cost",
                "summary": summary,
                "inspectDates": ["2026-11-21", "2026-11-22"],
            },
        ]
    )

    for job_id, empty_hint, unk_label, tasks_text, unpriced_text in (
        ("zh_tokens", "无匹配任务", "用量未知", "2 (已计量 0)", "未定价"),
        ("en_tokens", "No matching tasks", "Usage unknown", "2 (0 measured)", "Unpriced"),
    ):
        res = rendered[job_id]
        assert len(res["bars"]) == 7
        assert res["emptyElements"] == []
        empty_bar = next(b for b in res["bars"] if b["date"] == "2026-11-21")
        unk_bar = next(b for b in res["bars"] if b["date"] == "2026-11-22")

        assert empty_bar["dayState"] == "empty"
        assert empty_bar["empty"] == "1"
        assert empty_bar["usageUnknown"] == "0"
        assert empty_bar["stackStyle"] == "height:0;min-height:0"
        assert empty_bar["ariaDisabled"] == "true"

        assert unk_bar["dayState"] == "unmeasured"
        assert unk_bar["empty"] == "0"
        assert unk_bar["usageUnknown"] == "1"
        assert unk_bar["ariaDisabled"] is None
        assert "bar-stack-unmeasured" in (unk_bar["stackClass"] or "")
        assert "height:4px;min-height:4px" in (unk_bar["stackStyle"] or "")
        assert unk_label.lower() in unk_bar["ariaLabel"].lower()
        assert empty_hint not in unk_bar["ariaLabel"]

        unk_tip = res["tooltipsByDate"]["2026-11-22"]["text"]
        assert "2026-11-22" in unk_tip
        assert tasks_text in unk_tip
        assert unk_label in unk_tip
        assert unpriced_text in unk_tip
        assert empty_hint not in unk_tip

        empty_tip = res["tooltipsByDate"]["2026-11-21"]["text"]
        assert empty_hint in empty_tip
        assert unk_label not in empty_tip

        # Unmeasured-usage day remains clickable to view daily records; empty day does not.
        assert res["clickedDates"] == ["2026-11-22"]

    # On tasks metric, the unmeasured-usage day has 2 tasks -> renders a full 100% solid bar.
    tasks_unk = next(b for b in rendered["zh_tasks"]["bars"] if b["date"] == "2026-11-22")
    tasks_empty = next(b for b in rendered["zh_tasks"]["bars"] if b["date"] == "2026-11-21")
    assert tasks_unk["stackStyle"] == "height:100.00%"
    assert "bar-seg-tasks" in tasks_unk["stackInner"]
    assert tasks_empty["stackStyle"] == "height:0;min-height:0"

    # On cost metric, the unmeasured-usage day still shows the non-zero unknown stub, unlike the empty day.
    cost_unk = next(b for b in rendered["en_cost"]["bars"] if b["date"] == "2026-11-22")
    cost_empty = next(b for b in rendered["en_cost"]["bars"] if b["date"] == "2026-11-21")
    assert "height:4px;min-height:4px" in (cost_unk["stackStyle"] or "")
    assert cost_empty["stackStyle"] == "height:0;min-height:0"

    # Even if ONLY unmeasured-usage tasks exist in the 7d window (0 total tokens), it must NOT show an all-empty overlay.
    only_unk_home = tmp_path / "only-unmeasured"
    _append_task(only_unk_home, task_id="t_only_unk", started_at="2026-11-22T11:00:00+08:00")
    server2, thread2 = _start_server(only_unk_home)
    try:
        st2, sum2 = _json_get(server2.bound_port, "/api/summary?range=7d")
    finally:
        _stop_server(server2, thread2)
    assert st2 == 200 and sum2["overview"]["tasks"] == 1 and sum2["overview"]["active_days"] == 1
    only_unk_rendered = _run_chart_jobs(
        [{"id": "only_unk", "locale": "zh-CN", "metric": "tokens", "summary": sum2}]
    )["only_unk"]
    assert only_unk_rendered["emptyElements"] == []
    assert len(only_unk_rendered["bars"]) == 7
    assert [b["date"] for b in only_unk_rendered["bars"] if b["dayState"] == "unmeasured"] == ["2026-11-22"]
    assert len([b for b in only_unk_rendered["bars"] if b["dayState"] == "empty"]) == 6


def test_non_finite_ranges_keep_original_behavior(tmp_path: Path, monkeypatch):
    fixed_now = datetime(2026, 11, 24, 16, 30, 0, tzinfo=timezone(timedelta(hours=8)))
    monkeypatch.setattr(dashboard_server, "_current_local_now", lambda: fixed_now)

    # First check empty ledger on non-finite ranges ("all", "today", "custom").
    server, thread = _start_server(tmp_path)
    try:
        st_all_empty, sum_all_empty = _json_get(server.bound_port, "/api/summary?range=all")
        st_today_empty, sum_today_empty = _json_get(server.bound_port, "/api/summary?range=today")
        st_custom_empty, sum_custom_empty = _json_get(
            server.bound_port,
            "/api/summary?range=custom&start=2026-11-18T00:00:00Z&end=2026-11-24T23:59:59Z",
        )
    finally:
        _stop_server(server, thread)

    for st, payload in (
        (st_all_empty, sum_all_empty),
        (st_today_empty, sum_today_empty),
        (st_custom_empty, sum_custom_empty),
    ):
        assert st == 200 and payload["ok"] is True
        assert "daily_axis" not in payload
        assert payload["daily"] == []

    # Populate two non-adjacent days far apart and verify "all" does NOT pad gaps.
    _seed_pricing(tmp_path)
    _append_task(tmp_path, task_id="t_nov10", started_at="2026-11-10T10:00:00+08:00", input_tokens=100_000, output_tokens=50_000)
    _append_task(tmp_path, task_id="t_nov24", started_at="2026-11-24T10:00:00+08:00", input_tokens=200_000, output_tokens=100_000)

    server, thread = _start_server(tmp_path)
    try:
        st_all, sum_all = _json_get(server.bound_port, "/api/summary?range=all")
        st_day, sum_day = _json_get(server.bound_port, "/api/summary?range=7d&day=2026-11-24")
    finally:
        _stop_server(server, thread)

    assert st_all == 200 and "daily_axis" not in sum_all
    assert [d["date"] for d in sum_all["daily"]] == ["2026-11-10", "2026-11-24"]
    assert st_day == 200 and "daily_axis" not in sum_day

    rendered = _run_chart_jobs(
        [
            {"id": "all_empty_zh", "locale": "zh-CN", "metric": "tokens", "summary": sum_all_empty},
            {"id": "all_empty_en", "locale": "en", "metric": "tokens", "summary": sum_all_empty},
            {"id": "all_pop_zh", "locale": "zh-CN", "metric": "tokens", "summary": sum_all},
            {"id": "all_pop_en", "locale": "en", "metric": "tokens", "summary": sum_all},
        ]
    )

    assert rendered["all_empty_zh"]["bars"] == []
    assert rendered["all_empty_zh"]["emptyElements"][0]["text"] == "当前筛选范围内没有可绘制的每日数据"
    assert rendered["all_empty_en"]["bars"] == []
    assert rendered["all_empty_en"]["emptyElements"][0]["text"] == "No daily data available for the current filter"

    for job_id in ("all_pop_zh", "all_pop_en"):
        assert [b["date"] for b in rendered[job_id]["bars"]] == ["2026-11-10", "2026-11-24"]
        assert all(b["empty"] == "0" for b in rendered[job_id]["bars"])


def test_finite_axis_aligns_to_server_moment_and_timezone_not_client_tz(tmp_path: Path, monkeypatch):
    # Server is at 2026-11-24 00:30 +08:00 (still 2026-11-23 16:30 UTC).
    server_now_plus8 = datetime(2026, 11, 24, 0, 30, 0, tzinfo=timezone(timedelta(hours=8)))
    monkeypatch.setattr(dashboard_server, "_current_local_now", lambda: server_now_plus8)

    _seed_pricing(tmp_path)
    # Record timestamp is 2026-11-23T16:15:00+00:00 == 2026-11-24T00:15:00+08:00 (server's today).
    _append_task(tmp_path, task_id="t_midnight", started_at="2026-11-23T16:15:00+00:00", input_tokens=100_000, output_tokens=50_000)

    server, thread = _start_server(tmp_path)
    try:
        st, summary = _json_get(server.bound_port, "/api/summary?range=7d")
    finally:
        _stop_server(server, thread)

    assert st == 200
    assert summary["daily_axis"] == {
        "range": "7d",
        "start_date": "2026-11-18",
        "end_date": "2026-11-24",
        "days": 7,
    }
    assert [d["date"] for d in summary["daily"]] == ["2026-11-24"]

    # Render under UTC and America/Los_Angeles (both behind +08:00) — axis must still end on 2026-11-24.
    for client_tz in ("UTC", "America/Los_Angeles"):
        rendered = _run_chart_jobs(
            [{"id": "check", "locale": "en", "metric": "tokens", "summary": summary}],
            node_tz=client_tz,
        )["check"]
        assert [b["date"] for b in rendered["bars"]] == [
            "2026-11-18",
            "2026-11-19",
            "2026-11-20",
            "2026-11-21",
            "2026-11-22",
            "2026-11-23",
            "2026-11-24",
        ]
        assert rendered["bars"][-1]["date"] == "2026-11-24"
        assert rendered["bars"][-1]["empty"] == "0"
        assert all(b["empty"] == "1" for b in rendered["bars"][:-1])

    # Also test negative server offset (-05:00 at 22:30 -> already 2026-11-25 in UTC and Asia/Tokyo).
    neg_home = tmp_path / "neg-tz"
    server_now_minus5 = datetime(2026, 11, 24, 22, 30, 0, tzinfo=timezone(timedelta(hours=-5)))
    monkeypatch.setattr(dashboard_server, "_current_local_now", lambda: server_now_minus5)
    _seed_pricing(neg_home)
    _append_task(neg_home, task_id="t_neg", started_at="2026-11-25T02:00:00+00:00", input_tokens=100_000, output_tokens=50_000)
    server_neg, thread_neg = _start_server(neg_home)
    try:
        st_neg, sum_neg = _json_get(server_neg.bound_port, "/api/summary?range=7d")
    finally:
        _stop_server(server_neg, thread_neg)
    assert st_neg == 200
    assert sum_neg["daily_axis"] == {
        "range": "7d",
        "start_date": "2026-11-18",
        "end_date": "2026-11-24",
        "days": 7,
    }
    assert [d["date"] for d in sum_neg["daily"]] == ["2026-11-24"]
    rendered_tokyo = _run_chart_jobs(
        [{"id": "tokyo", "locale": "zh-CN", "metric": "tokens", "summary": sum_neg}],
        node_tz="Asia/Tokyo",
    )["tokyo"]
    assert [b["date"] for b in rendered_tokyo["bars"]] == [
        "2026-11-18",
        "2026-11-19",
        "2026-11-20",
        "2026-11-21",
        "2026-11-22",
        "2026-11-23",
        "2026-11-24",
    ]
    assert rendered_tokyo["bars"][-1]["empty"] == "0"
