from __future__ import annotations

import http.client
import json
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_bridge.billing_store import append_receipt_record
from agent_bridge.dashboard_server import DashboardHTTPServer, create_dashboard_server


def _request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, hdrs, data
    finally:
        conn.close()


def _json_req(
    port: int,
    method: str,
    path: str,
    *,
    csrf: str | None = None,
    payload: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    hdrs: dict[str, str] = {}
    if csrf is not None:
        hdrs["X-Bridge-CSRF"] = csrf
    body: bytes | None = None
    if payload is not None:
        hdrs["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    if extra_headers:
        hdrs.update(extra_headers)
    status, _resp_headers, raw = _request(port, method, path, headers=hdrs, body=body)
    return status, json.loads(raw.decode("utf-8"))


def test_dashboard_refuses_non_loopback_bind(tmp_path: Path):
    with pytest.raises(ValueError, match="loopback"):
        DashboardHTTPServer(("0.0.0.0", 0), home=tmp_path)


def test_dashboard_end_to_end_and_security_guards(tmp_path: Path):
    append_receipt_record(
        tmp_path,
        {
            "task_id": "task_agy_1",
            "session_id": "sess_agy_1",
            "agent": "antigravity",
            "cwd": "C:/Dev/Demo",
            "title": "Audit PR",
            "status": "completed",
            "model": "gemini-4-hydrogen-xhigh",
            "message": "SECRET USER MESSAGE",
            "result_preview": "SECRET OUTPUT",
            "usage": {
                "input_tokens": 100_000,
                "cache_read_tokens": 900_000,
                "output_tokens": 50_000,
                "thinking_tokens": 20_000,
                "total_tokens": 150_000,
                "scope": "turn",
            },
            "created_at": "2026-11-20T10:00:00+00:00",
            "started_at": "2026-11-20T10:00:00+00:00",
            "finished_at": "2026-11-20T10:01:40+00:00",
        },
    )
    append_receipt_record(
        tmp_path,
        {
            "task_id": "task_zcode_1",
            "session_id": "sess_zc_1",
            "agent": "zcode",
            "cwd": "C:/Dev/Demo",
            "title": "GLM run",
            "status": "completed",
            "model": r"zhipu\GLM-5.3",
            "observed_model": r"zhipu\GLM-5.3",
            "usage": {"input_tokens": 1_000_000, "output_tokens": 500_000},
            "created_at": "2026-11-20T11:00:00+00:00",
            "started_at": "2026-11-20T11:00:00+00:00",
            "finished_at": "2026-11-20T11:00:25+00:00",
        },
    )

    server = create_dashboard_server(home=tmp_path, host="127.0.0.1", port=0, backfill=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.bound_port
    try:
        # 1. Index HTML injects CSRF token and security headers.
        status, headers, raw_html = _request(port, "GET", "/")
        assert status == 200
        assert "access-control-allow-origin" not in headers
        assert headers["x-frame-options"] == "DENY"
        assert headers["cache-control"] == "no-store"
        html = raw_html.decode("utf-8")
        m = re.search(r'<meta name="ab-csrf" content="([^"]+)"', html)
        assert m is not None
        csrf = m.group(1)
        assert csrf == server.csrf_token

        # Static assets & traversal block
        assert _request(port, "GET", "/assets/app.css")[0] == 200
        assert _request(port, "GET", "/assets/app.js")[0] == 200
        assert _request(port, "GET", "/assets/../cli.py")[0] == 404

        # 2. DNS Rebinding (bad Host) rejected
        bad_host_status, _, _ = _request(port, "GET", "/api/meta", headers={"Host": "evil.example"})
        assert bad_host_status == 403

        # 3. Meta defaults to antigravity and lists all built-in agents
        st, meta = _json_req(port, "GET", "/api/meta")
        assert st == 200 and meta["ok"] is True
        assert meta["default_agents"] == ["antigravity"]
        agent_names = [a["name"] for a in meta["agents"]]
        assert {"antigravity", "codex", "cursor", "claude", "zcode"}.issubset(set(agent_names))

        # 4. Summary for AGY initially marks gemini-4-hydrogen-xhigh unpriced
        st, summary = _json_req(port, "GET", "/api/summary?agents_filter=1&agents=antigravity")
        assert st == 200
        assert summary["overview"]["tasks"] == 1
        assert summary["overview"]["unpriced_tasks"] == 1
        assert summary["overview"]["effective_cost_usd"] is None
        assert summary["overview"]["cache_hit_rate"] == pytest.approx(0.9, rel=1e-6)

        # Explicit empty agent selection returns 0 records (not all records)
        st, empty_sum = _json_req(port, "GET", "/api/summary?agents_filter=1&agents=")
        assert st == 200 and empty_sum["overview"]["tasks"] == 0

        # 5. Mutations require CSRF + loopback Origin
        st, _ = _json_req(port, "PUT", "/api/pricing", payload={"upsert_model": {"key": "x", "rate": {"input": 1, "output": 2}}})
        assert st == 403
        st, _ = _json_req(
            port,
            "PUT",
            "/api/pricing",
            csrf=csrf,
            payload={"upsert_model": {"key": "x", "rate": {"input": 1, "output": 2}}},
            extra_headers={"Origin": "http://evil.example"},
        )
        assert st == 403

        # 6. Manual pricing updates summary cost immediately
        st, _ = _json_req(
            port,
            "PUT",
            "/api/pricing",
            csrf=csrf,
            payload={
                "upsert_model": {
                    "key": "gemini-4-hydrogen-xhigh",
                    "rate": {"input": 4.0, "output": 20.0, "cache_read": 0.4, "cache_write": 0.0},
                }
            },
        )
        assert st == 200
        st, priced_sum = _json_req(port, "GET", "/api/summary?agents_filter=1&agents=antigravity")
        assert st == 200
        assert priced_sum["overview"]["priced_tasks"] == 1
        # (100k*4 + 900k*0.4 + 50k*20)/1e6 = 0.4 + 0.36 + 1.0 = 1.76
        assert priced_sum["overview"]["effective_cost_usd"] == pytest.approx(1.76, rel=1e-6)

        # 7. Records & detail drawer never expose prompt/output bodies
        st, recs = _json_req(port, "GET", "/api/records")
        assert st == 200 and recs["total"] == 2
        assert "SECRET USER MESSAGE" not in json.dumps(recs)
        assert "SECRET OUTPUT" not in json.dumps(recs)

        st, detail = _json_req(port, "GET", "/api/records/task_zcode_1")
        assert st == 200
        assert detail["record"]["cost"]["discount_quota_boost_pct"] == 150.0
        assert detail["record"]["cost"]["priced"] is True

        # 8. Storage policy, export, and double-confirmed clear
        st, stor = _json_req(port, "PUT", "/api/storage", csrf=csrf, payload={"max_mb": 50, "max_age_days": 90})
        assert st == 200 and stor["storage"]["policy"]["max_age_days"] == 90

        exp_status, exp_hdrs, exp_body = _request(port, "GET", "/api/storage/export")
        assert exp_status == 200
        assert "attachment" in exp_hdrs.get("content-disposition", "")
        assert b"task_agy_1" in exp_body

        st, _ = _json_req(port, "POST", "/api/storage/clear", csrf=csrf, payload={"confirm": "NOPE"})
        assert st == 400
        st, cleared = _json_req(port, "POST", "/api/storage/clear", csrf=csrf, payload={"confirm": "CLEAR"})
        assert st == 200 and cleared["result"]["cleared_records"] == 2
        assert cleared["storage"]["record_count"] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_cli_dashboard_subcommand_parses_flags(monkeypatch):
    from agent_bridge.cli import main

    captured: dict[str, Any] = {}

    def fake_run_dashboard(*, host: str, port: int, open_browser: bool) -> None:
        captured["host"] = host
        captured["port"] = port
        captured["open_browser"] = open_browser

    monkeypatch.setattr("agent_bridge.dashboard_server.run_dashboard", fake_run_dashboard)
    main(["dashboard", "--host", "localhost", "--port", "9123", "--no-open"])
    assert captured == {"host": "localhost", "port": 9123, "open_browser": False}


@pytest.mark.asyncio
async def test_registry_terminal_receipt_persists_to_independent_billing_ledger(tmp_path: Path):
    from agent_bridge.billing_store import load_ledger
    from agent_bridge.config import AgentConfig, AppConfig
    from agent_bridge.models import Session, Task, TaskStatus
    from agent_bridge.registry import Registry

    cfg = AppConfig(agents={"fake": AgentConfig(name="fake", protocol="fake", command=["fake"])})
    reg = Registry(tmp_path, cfg)
    sess = Session(session_id="sess_reg", agent="antigravity", cwd=str(tmp_path), title="Ledger hook test")
    task = Task(
        task_id="task_reg_1",
        session_id=sess.session_id,
        agent="antigravity",
        message="PRIVATE PROMPT",
        cwd=str(tmp_path),
        model="gemini-4-argon",
        status=TaskStatus.completed,
        result_text="PRIVATE RESULT",
        usage={"input_tokens": 1200, "cache_read_tokens": 3800, "output_tokens": 400, "scope": "turn"},
    )
    reg.sessions[sess.session_id] = sess
    reg.tasks[task.task_id] = task

    receipt_file = await reg._persist_receipt(task, sess)
    assert receipt_file is not None and receipt_file.is_file()

    # Pruning the task deletes the receipt, but the independent billing ledger keeps it.
    reg._drop_task(task.task_id)
    assert not receipt_file.exists()
    records, corrupt = load_ledger(tmp_path)
    assert corrupt == 0
    assert len(records) == 1
    assert records[0].task_id == "task_reg_1"
    assert records[0].tokens.uncached_input_tokens == 1200
    assert records[0].tokens.cache_read_tokens == 3800
    assert "PRIVATE PROMPT" not in records[0].model_dump_json()
    assert "PRIVATE RESULT" not in records[0].model_dump_json()
