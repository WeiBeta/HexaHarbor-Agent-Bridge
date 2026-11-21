# HexaHarbor · 六库仙贼

**English** · [简体中文](README.zh-CN.md)　|　[Setup](SETUP.md) · [Releases](https://github.com/WeiBeta/HexaHarbor-Agent-Bridge/releases) · [AGPL-3.0-only](LICENSE)

![At night, two pilot boats pass through the entrance of a six-sided harbor.](assets/hexaharbor-night-harbor.jpg)

**Give another coding agent a job without leaving yours.** From a Codex chat, for example, you can ask Antigravity to review a change. HexaHarbor hands the task to its native CLI and brings the result back into the same conversation. You keep working in the host you chose; the worker keeps its own tools, login and permissions.

## Your first ticket

You need [uv](https://docs.astral.sh/uv/), a coordinator such as Codex, and at least one worker CLI already installed and signed in. This example uses **Windows PowerShell + Codex + Antigravity**:

```powershell
uv tool install git+https://github.com/WeiBeta/HexaHarbor-Agent-Bridge.git
$bridge = Join-Path (uv tool dir --bin) 'agent-bridge.exe'
codex mcp add agent_bridge -- $bridge
```

Restart Codex and open a project that has a small change to review. Try asking:

> List the available Bridge workers. If Antigravity is available, ask it to review the current Git diff without editing files. Bring back one concrete finding, or say it found none.

First check: `list_agents` should show Antigravity as available and `coordinator.dispatch_enabled` should be true. Then use `dispatch_ticket` with the project directory as `cwd`. The worker runs there; you can inspect its receipt and check `git diff` yourself. If the wait expires, keep the returned `task_id` — **continue waiting on that ticket; don't submit a second one**.

Not using Codex? [Setup by host](SETUP.md) includes registration examples for Cursor, Kimi Code, ZCode, Grok Build and Claude Code, plus how Devin reads Cursor's configuration.

## Which side does what?

| | |
| --- | --- |
| **Coordinator** | The chat where you give instructions and accept the result. Codex, Cursor, Kimi Code, ZCode, Grok Build, Claude Code and Devin can take this role. |
| **Bridge** | A local MCP server. It starts or resumes a worker turn, tracks the task and returns results. No GUI automation or model API proxy. |
| **Worker** | A separate CLI process. Bundled configurations cover Antigravity, Claude Code, Codex CLI, Cursor, DeepSeek Harness, Devin CLI, Grok Build, Kimi Code, MiniMax Code, OpenCode and ZCode. Use `list_agents` to see what's available on *your* machine. |

Local means the **bridge** runs on your machine, not that prompts never leave it. Each worker uses its own provider and account. The project directory is a working directory, not a sandbox. Results, transcripts and receipts under `~/.agent-bridge` are **plaintext**; keep them outside Git and review them before sharing.

## Working with tickets

| When you want to… | Use |
| --- | --- |
| Discover workers or remember a routing preference | `list_agents` · `set_preferences` |
| Send one ticket and collect its receipt | `dispatch_ticket` (default wait: 480 s; keep it below your host's MCP timeout) |
| Start several independent tasks or resume an existing session | `dispatch_task`; keep each task ID, then `wait_task` and `get_result` |
| Check on a quiet worker or read what it did | `check_task` · `get_transcript` |
| Stop a turn, inspect sessions or shut down a worker | `cancel_task` · `list_sessions` · `end_session` |

A wait timeout is not a worker failure. A separate `stall_timeout_sec` watchdog (default 1800 s) detects a worker that stays silent; `check_task` reports `silent_for_sec`. To read long results, follow `next_cursor` while `has_more` is true. Bridge doesn't automatically replay a failed prompt. See [work tickets and local records](SETUP.md#work-tickets-and-local-records), [failure diagnostics](SETUP.md#acp-failure-diagnostics) and [quota reporting](SETUP.md#remaining-quota-in-list_agents).

**Who decides when to delegate?** The default, `auto`, leaves that to the coordinator. First connect does not ask you to pick a mode. Choose `manual` for explicit requests only, or `eager` to prefer delegation on multi-step tasks. Set `[coordinator] mode` in `~/.agent-bridge/agents.toml` or use `AGENT_BRIDGE_MODE` for one host. The [coordinator rulebook](ORCHESTRATION.md) is optional; skill auto-install is off by default.

## Extend or contribute

- **Local usage & billing dashboard**: run `agent-bridge dashboard` (`--port 8765`, loopback `127.0.0.1` only; no extra MCP tools added). It tracks token usage, cache hit rates, USD API list-price estimates, harness discounts, task details, and ledger disk retention under `~/.agent-bridge/billing/` without reading prompt or output bodies. See [Usage & billing dashboard](SETUP.md#usage-and-billing-dashboard).
- [Add a worker](skills/add-worker/SKILL.md) or [connect another coordinator](skills/add-coordinator/SKILL.md). A standard ACP CLI can often be added through configuration alone.
- [Development guide](CONTRIBUTING.md): run `uv run --extra dev pytest` from a clone. `tests/` is for pytest; live drills use `lab/`, generated by `scripts/setup_lab.py` and not in git.
- [Updates](SETUP.md#update): close coordinators using Bridge, run `agent-bridge upgrade`, then restart them. Install this fork from Git; the command is `agent-bridge` and the Codex MCP name in the example is `agent_bridge`.

Derived from **Agent-Bridge** by FeiZhuLulu. This fork is [AGPL-3.0-only](LICENSE); the [upstream MIT notice](LICENSES/Agent-Bridge-MIT.txt) is preserved. See [NOTICE](NOTICE.md).
