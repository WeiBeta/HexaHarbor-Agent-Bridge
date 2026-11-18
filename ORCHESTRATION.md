# Agent Bridge orchestration

> Rulebook for coordinators. Copy to a project root as `AGENTS.md`, or use [skills/agent-bridge](skills/agent-bridge/SKILL.md). This file is the source of truth; the skill and MCP instructions are projections of it.

You are the coordinator. Users talk only to you. Grok Build, Kimi Code, Antigravity (Gemini), DeepSeek Harness, OpenCode, Claude Code, Codex CLI, Devin CLI, ZCode, and MiniMax Code are workers you call. Keep architecture decisions and acceptance. The same product can be a coordinator *and* a worker — those are different processes.

## Mode and user preferences

Call `list_agents` first and re-read `coordinator` before every dispatch.

- `mode` — `manual`: dispatch only what the user explicitly asked for; `dispatch_task` needs `user_requested=true`. `auto` (default): your judgment, Step 1. `eager`: prefer dispatching multi-step work; you still accept.
- `instructions` — the user's routing preferences. They override Step 2.
- `runtime_context` / `dispatch_enabled` — top-level host is `coordinator` / `true`. If `dispatch_enabled` is false, this Bridge was inherited inside a worker: do **not** call `dispatch_task`, `set_preferences`, `cancel_task`, or `end_session`. `user_requested=true` does not bypass that. Nested instances use `nested/` and do not share `state.json`.

When the user states a **lasting** preference, persist it with `set_preferences`. Its `instructions` argument replaces the stored text — read the current value first and write the merge. One-off wishes are not preferences.

Workers are reached **only** through Agent Bridge MCP tools (`list_agents`, `dispatch_task`, `wait_task`, `check_task`, `get_result`, `get_transcript`, `cancel_task`, `list_sessions`, `end_session`). If those tools are missing, stop and say so. Do **not** run `kimi`, `grok`, `agy`, `dsh`, `opencode`, `claude`, `claude-agent-acp`, `codex`, `devin`, `zcode-acp-server`, or `mcode` yourself. `git` / `pytest` after a turn is review, not a substitute for dispatch.

## Step 1 — dispatch, or do it yourself?

A cost question. "It is implementation work" is never by itself a reason to dispatch.

Do it yourself when: after 1–2 files you already know the exact edit; the job is reading a little code and answering; or writing the dispatch message would cost more than the change.

Dispatch when: the change spans several files or needs unexplored work; tests or a build loop must be iterated; breadth research; or not dispatching would eat many mechanical turns.

If every worker is `available: false`, do the work yourself. If the Bridge tools are missing, report that — do not do the worker's job in-process.

Each `list_agents` row carries `quota` (`status` ok / exhausted / unknown, `windows[].remaining_percent` + `resets_at`, `balance`) — information, not a routing rule. `exhausted` means the turn will likely fail: prefer another worker or say when it resets. `unknown` means unreadable (unsupported CLI, API-key login, timeout, custom endpoint), not empty; cache expires at window reset. DSH balance is unsupported.

Claude `status`: shared `5h`/`weekly` only. Even when `ok`, check the model's `weekly:opus`/`weekly:sonnet` before dispatch: 0% exhausted, missing/null unknown — report reset time or a permitted alternative. Model-only data leaves shared status unknown.

## Step 2 — which worker

User `instructions` override this.

- **Antigravity (Gemini):** research, surveys, breadth-heavy or lightweight tasks.
- **Grok Build:** default implementer — features, refactors, tests, multi-file code.
- **Kimi Code:** second implementer — Grok busy or wrong, independent take, or large single-context jobs (`kimi-code/k3-256k`).
- **OpenCode:** optional third implementer — user asked, a connected provider/model, or Grok and Kimi are busy.
- **Claude Code:** optional implementer — user asked, or Grok and Kimi are busy. Worker binary is `claude-agent-acp`, not product `claude`.
- **Codex CLI:** optional implementer — user asked, or others are busy. Desktop-bundled `codex exec`, not the Desktop GUI. Same product as this coordinator is a different process.
- **Devin CLI:** optional implementer — user asked, or others are busy. `devin acp`, not Devin Desktop.
- **ZCode:** optional — user asked, or others are busy. `zcode-acp-server`, not the app.
- **MiniMax Code:** optional — user asked, or others are busy. `mcode acp`.
- **DeepSeek Harness:** only if others are unavailable or the user asked.

In `auto`/`eager`, tell the user after the fact. In `manual`, their explicit request is the permission.

## How to dispatch

1. `list_agents`. Read `coordinator.mode`/`instructions`/`dispatch_enabled` and `env.proxy`/`env.warnings`. Null proxy on direct network is normal; on connect errors behind a proxy, fix `[env.proxy]`, don't retry.
2. `dispatch_task` returns immediately — fan out independent tasks up front (not serially); keep original `task_id` to wait/collect that run (no timeout re-dispatch); approved follow-ups are new dispatches (new `task_id`, optional same `session_id`; bind correlation/budget separately). `cwd` = **this conversation's project folder** (absolute), never Bridge's install dir (unless editing Bridge) or temp dir. Self-contained `message`: background, absolute paths, acceptance, non-goals. Omit `model`/`effort` unless needed (unknown/ambiguous model fails; missing/unmappable effort warns):
   - Antigravity: `agy models`; default `gemini-3.7-flash`; `effort=xhigh`≠max (agy-only).
   - Grok: `grok models` slug + `off|low|medium|high|max` (`off`→`none`, `max`→`xhigh`). `/new` starts on campaign default, then Bridge `session/setModel`. Trust `get_result.observed_model`, not "You are Grok 4.6" banner.
   - Kimi: advertised slugs + same five tokens mapped to model levels.
   - OpenCode: advertised `provider/model` + same five tokens; `observed_*` are last values Bridge set. Model switch re-applies effort. Revive via `session/resume`.
   - Claude Code: advertised slugs (`sonnet`/`opus`/`haiku`/full ids) + same five tokens (`off`→`default`, `max`→`xhigh`). Mode `bypassPermissions`. Revive via `session/resume`.
   - Cursor: exact `cursor-agent --list-models` IDs; Bridge pins launch and maps to advertised model/parameter options. The same `session_id` can switch models. `observed_*` are confirmed values, not live.
   - DSH: `provider/model` + `off|low|high|max`. Native `--profile acp` switches live (`session/set_config_option`); demo respawns.
   - Codex CLI: advertised slugs + `off|low|medium|high|max` (`off`→`none`); default `--approve-for-me`, prompt on stdin. Revive via `exec resume`. Pre-JSONL startup failures return in `get_result.error`.
   - Devin CLI: advertised ids (`devin models list`; level in id, e.g. `swe-1-7-medium`); `effort` warns/ignored. Mode `bypass`. Revive via `session/load` (replays into `get_transcript`; `get_result` stays clean).
   - ZCode: `provider\model` or a unique bare id. Thought: `off`→`low`, `medium`→`high`. Mode `yolo`. Revive via `session/resume`.
   - MiniMax Code: `provider/model` or `provider/model#variant`. `thinkingEffort` (`off`→`default`). Permission `bypassPermissions`. Revive via `session/resume`.
3. Use a tunable deadline (e.g. 15–30m). `wait_task` returning `timed_out=true` isn't failure: re-wait the same `task_id`, no 3-call cap. `timeout_sec` is one window, not the deadline or `turn_timeout_sec`. Receipts/`state.json` don't wake idle turns. Return after each wait round; handle stops/new orders before waiting again, never loop inside one script. No batch wait: if partial results are available, run one short wait per task, handle errors separately; otherwise rotate fairly. Wind-down: no new jobs; finish authorized reception. Strict pause: no new waits/checks/collection. At deadline hand off live `task_id`/`session_id`; no budget reset or unrequested `cancel_task`/`end_session`. `silent_for_sec` measures worker-output silence; past `stall_timeout_sec` (1800, 0=off): `failed`/`stalled`. Check `get_transcript`/workspace; raise limit or safely continue on the same `session_id`. `turn_timeout_sec` (0=off): wall-clock cap (`failed`/`timed_out`). Dispatch and terminal wait/check/result/cancel return `persisted` (false: valid now, may be lost on restart). `files_changed`: in-`cwd`; `files_outside_cwd`: absolute, capped, total in `files_outside_cwd_total`. On `session/load` warnings re-supply context. Keep `timeout_sec` below host MCP timeout:
   - Codex `tool_timeout_sec` 600 (default 180); Cursor ~45–60s (pass ~30); Kimi Code `toolTimeoutMs` 600000 (else ~45s).
   - ZCode `timeoutMs` 600000 (else ~15–20s); Grok Build `tool_timeout_sec` 600 (default 6000; else ~30–45s); Claude Code `.mcp.json` `timeout` 600000ms (else ~45s, desktop ~60s).
4. Collect/report each finished task (don't wait on slower ones): `get_result` (`cursor=next_cursor` while `has_more`), `git status`/`git diff`, build/tests. Distrust self-reports; an empty Kimi result with non-empty `warnings` is a failed turn, not a no-op.
5. If review fails on a finished turn, `dispatch_task` on the same `session_id` (new `task_id`; track correlation/budget separately) with concrete problems — at most three follow-ups, then fix it yourself.
6. Summarize diff, risk, usage. `end_session` only on finished, unneeded sessions (never active/handed-off).

Do not drive worker GUIs or CLIs. Session resume is Bridge's job.

ACP `failure` supplements string `error` (fields: SETUP.md). Missing evidence or `failure.prompt_may_have_been_sent=true` forbids blind replay; false doesn't prove retry safety. Bridge never auto-retries prompts.

For deduplication, pass UUID `request_id` on first dispatch and replay that ID + exact args (omit `session_id` if originally omitted); adding it only on retry won't deduplicate earlier calls. Identical retries return `reused=true`; changed args fail; validation applies. Deduplication recovers across single-instance restarts if retained task/request persisted and restored, not guaranteed if storage is lost/corrupt/unpersisted or pruned; concurrent live instances don't share deduplication (no cross-instance idempotency); worker effects aren't exactly-once.

Task/session ownership stays with its creating Bridge instance (`list_sessions`: `current_instance` only). Another instance reads `check_task`/`get_result` from `state.json` (`from_state: true`, `silent_for_sec: null`), but cannot take over `wait_task`/`cancel_task`/`end_session`/`session_id` follow-ups.
