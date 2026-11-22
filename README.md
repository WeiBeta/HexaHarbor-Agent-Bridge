# HexaHarbor · 六库仙贼

**English** · [简体中文](README.zh-CN.md)　|　[Setup](SETUP.md) · [Releases](https://github.com/WeiBeta/HexaHarbor-Agent-Bridge/releases) · [AGPL-3.0-only](LICENSE)

![At night, two pilot boats pass through the entrance of a six-sided harbor.](assets/hexaharbor-night-harbor.jpg)

**Give another coding agent a job without leaving yours.** From a Codex chat, for example, you can ask Antigravity to review a change. HexaHarbor hands the task to its native CLI and brings the result back into the same conversation. You keep working in the host you chose; the worker keeps its own tools, login, and permissions.

## Your first ticket

You need [uv](https://docs.astral.sh/uv/), a coordinator such as Codex, and at least one worker CLI already installed and signed in. This example uses **Windows PowerShell + Codex + Antigravity**:

```powershell
uv tool install git+https://github.com/WeiBeta/HexaHarbor-Agent-Bridge.git
$bridge = Join-Path (uv tool dir --bin) 'agent-bridge.exe'
codex mcp add agent_bridge -- $bridge
```

Restart Codex and open a Git project with a small change to review. Try asking:

> List the available Bridge workers. If Antigravity is available, ask it to review the current Git diff without editing files. Bring back one concrete finding, or say it found none.

First check `list_agents`: Antigravity should appear as available and `coordinator.dispatch_enabled` should be `true`. Then use `dispatch_ticket` with the project directory as `cwd`. The worker runs in that directory; once it finishes, you can inspect its receipt and check `git diff` yourself. If the wait times out, keep the returned `task_id` — **continue with `wait_task` and `get_result` on that ticket; do not submit a second one**.

Not using Codex? [Setup by host](SETUP.md) includes registration examples for Cursor, Kimi Code, ZCode, Grok Build, and Claude Code, plus how Devin Desktop reads Cursor's configuration.

## Which side does what?

| Role | What it does |
| --- | --- |
| **Coordinator** | The chat where you give instructions and verify the result. Codex, Cursor, Kimi Code, ZCode, Grok Build, Claude Code, and Devin can take this role. |
| **Bridge** | A local MCP server. It starts or resumes a worker turn, tracks the task, and returns results. No GUI automation or model API proxy. |
| **Worker** | A separate CLI process. Bundled configurations cover Antigravity, Claude Code, Codex CLI, Cursor, DeepSeek Harness, Devin CLI, Grok Build, Kimi Code, MiniMax Code, OpenCode, and ZCode. Run `list_agents` to see what is available on *your* machine. |

"Local" means the **bridge** runs on your machine, not that prompts never leave it. Each worker still talks to its own provider with its own account. The task `cwd` is a working directory, not a sandbox. Results, transcripts, and receipts under `$AGENT_BRIDGE_HOME` (default `~/.agent-bridge`) are **plaintext** local files — keep them outside Git and review them before sharing.

## Working with tickets

| When you want to… | Use |
| --- | --- |
| Discover workers or save a routing preference | `list_agents` · `set_preferences` |
| Send one ticket and collect its receipt | `dispatch_ticket` (default `agent="antigravity"`, default wait 480 s; keep it below your host's MCP timeout) |
| Run independent tasks in parallel or resume a session | `dispatch_task`; keep each `task_id`, then `wait_task` (default 180 s) and `get_result` |
| Check on a quiet worker or inspect what it did | `check_task` · `get_transcript` |
| Stop a turn, inspect sessions, or shut down a worker | `cancel_task` · `list_sessions` · `end_session` |

A wait timeout is not a worker failure. A separate `stall_timeout_sec` watchdog (default 1800 s) detects a worker that stops producing output; `check_task` reports `silent_for_sec`. To read long results, follow `next_cursor` while `has_more` is `true`. Bridge never automatically replays a failed prompt. See [work tickets and local records](SETUP.md#work-tickets-and-local-records), [failure diagnostics](SETUP.md#acp-failure-diagnostics), and [quota reporting](SETUP.md#remaining-quota-in-list_agents).

**Who decides when to delegate?** The default, `auto`, leaves that judgment to the coordinator. First connect does not ask you to pick a mode. Choose `manual` to require explicit requests (enforced server-side unless `user_requested=true`), or `eager` to prefer delegating multi-step work. Configure `[coordinator] mode` in `~/.agent-bridge/agents.toml`, or override a single host with `AGENT_BRIDGE_MODE`. The [coordinator rulebook](ORCHESTRATION.md) is optional; `[server] auto_install_skill` is off by default.

## Using the dashboard

HexaHarbor includes an optional local usage & billing dashboard that stays completely out of the MCP tool list (zero extra tool context for coordinators). It binds strictly to IPv4 loopback (`127.0.0.1` or `localhost`, default `127.0.0.1:8765`, or the host/port saved in `service.json`) and is accessible to local processes on the machine.

```powershell
agent-bridge dashboard                 # foreground; binds 127.0.0.1:8765 (or service.json) and opens browser
agent-bridge dashboard --port 9000 --no-open
agent-bridge dashboard start           # detached background service (add --open or --tray as needed)
agent-bridge dashboard stop            # stop the background service
agent-bridge dashboard restart         # restart the background service
agent-bridge dashboard status          # print service status JSON only
agent-bridge dashboard tray            # Windows system tray (--stop to close)
```

- **Foreground vs. background mode**: `agent-bridge dashboard` (alias `dashboard serve`, with `--host`, `--port`, `--no-open`) runs interactively in your terminal until you press `Ctrl+C`, opening the browser automatically unless `--no-open` is passed. `agent-bridge dashboard start` (with `--host`, `--port`, `--open`, `--tray`) runs as a detached background service (without a console window on Windows, writing logs to `~/.agent-bridge/billing/dashboard.log`, and only opening the browser if `--open` is passed). When Windows sign-in autostart is enabled, Windows launches `dashboard start` silently after you sign in so the dashboard stays running in the background.
- **Windows sign-in and tray settings**: On the first explicit dashboard start for a Bridge home with no saved settings, both sign-in startup and the tray icon are enabled. If another Bridge home already owns the Windows sign-in entry, it keeps it. To change either switch, use the dashboard’s **Management (管理)** tab and save. Later starts respect your choices.
- **Chinese / English UI switch**: Use the **CN / EN** switch in the top-right corner of the dashboard header (Simplified Chinese by default). The choice is saved in `~/.agent-bridge/billing/preferences.json` (independent of `service.json` first-use defaults) and syncs with the Windows system tray for the same Bridge home.
- **Status queries are read-only**: `agent-bridge dashboard status` only prints the service status JSON. It does **not** start the background service, initialize first-use settings, register autostart, or launch the tray.
- **Local statistics & estimates only**: Metering reads task receipts and saved state JSON (which may include a result preview) and writes only task metadata and token counters into `~/.agent-bridge/billing/usage_ledger.jsonl` (prompts and output bodies are never stored in the ledger or exported). Token totals, cache hit rates, USD API list-price conversions (from built-in public reference snapshots or your `pricing.json` overrides), and harness discount rules are **local reference statistics and estimates only — not an official provider bill and not a billing basis**. Models without a matched price or tasks without token breakdowns are explicitly excluded rather than counted as `$0`.
- **Update notice is informational only**: The update notice in the web UI (as well as the Windows tray menu and `agent-bridge dashboard update`) is only a notice and does **not** automatically check for releases or upgrade Bridge. When you want to upgrade, follow [Updates](SETUP.md#update) manually.

For storage retention (`100 MiB` / `180 days` defaults), pricing overrides, and file formats, see [Usage and billing dashboard](SETUP.md#usage-and-billing-dashboard).

## Extend or contribute

- [Add a worker](skills/add-worker/SKILL.md) or [connect another coordinator](skills/add-coordinator/SKILL.md). A standard ACP CLI can often be added through configuration alone.
- [Development guide](CONTRIBUTING.md): run `uv run --extra dev pytest` from a clone. `tests/` is for pytest; live drills use `lab/`, generated by `scripts/setup_lab.py` and not in git.
- [Updates](SETUP.md#update): close coordinators using Bridge (and stop the background dashboard service if running), run `agent-bridge upgrade`, then restart them. Install this fork from Git; the command is `agent-bridge` and the Codex MCP server name in the example is `agent_bridge`.

Derived from **Agent-Bridge** by FeiZhuLulu. This fork is [AGPL-3.0-only](LICENSE); the [upstream MIT notice](LICENSES/Agent-Bridge-MIT.txt) is preserved. See [NOTICE](NOTICE.md) for full licensing and third-party attributions.
