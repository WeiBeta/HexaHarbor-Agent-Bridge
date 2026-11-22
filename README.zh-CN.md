# 六库仙贼 · HexaHarbor

[English](README.md) · **简体中文**　|　[接入指南（英文）](SETUP.md) · [版本发布](https://github.com/WeiBeta/HexaHarbor-Agent-Bridge/releases) · [AGPL-3.0-only](LICENSE)

![夜色中的六边形港湾，两艘引航船在港口一出一进。](assets/hexaharbor-night-harbor.jpg)

**不必换掉手上的编程助手，也能把活交给另一位。** 比如你在 Codex 里写代码，想让 Antigravity 帮忙审一眼改动：六库仙贼把工单交给它原生的 CLI，做完再把结果带回当前对话。你继续留在原本的宿主里推进工作；接单的执行端则保持自己的工具、登录态和权限。

## 派出第一单

先准备好 [uv](https://docs.astral.sh/uv/)、一个主控（如 Codex），以及至少一个已安装并登录的执行端 CLI。以下以 **Windows PowerShell + Codex + Antigravity** 为例：

```powershell
uv tool install git+https://github.com/WeiBeta/HexaHarbor-Agent-Bridge.git
$bridge = Join-Path (uv tool dir --bin) 'agent-bridge.exe'
codex mcp add agent_bridge -- $bridge
```

重启 Codex，打开一个带有少量改动的 Git 项目，在对话里试试：

> 列出 Bridge 能用的执行端。如果 Antigravity 可用，请它审一下当前 Git diff，不修改文件。带回一条具体发现；没发现问题就直说。

先看 `list_agents`：Antigravity 应显示为可用，且 `coordinator.dispatch_enabled` 为 `true`。接着用 `dispatch_ticket` 派单，`cwd` 指向项目目录。执行端会在该目录运行；完成后你可以查看回执，并自己用 `git diff` 核对。如果只是等待超时，**保留返回的 `task_id`，用 `wait_task` 和 `get_result` 继续等这一单，不要重复再派一单**。

主控不是 Codex？[按宿主接入](SETUP.md)里提供了 Cursor、Kimi Code、ZCode、Grok Build 和 Claude Code 的接入示例，也说明了 Devin Desktop 如何复用 Cursor 的配置。

## 谁负责哪一段？

| 角色 | 职责 |
| --- | --- |
| **主控** | 你下达需求、验收结果的对话。可以是 Codex、Cursor、Kimi Code、ZCode、Grok Build、Claude Code 或 Devin。 |
| **通讯桥** | 运行在本机的 MCP 服务，负责拉起或续接执行端、跟踪任务并取回结果。不做 GUI 自动化，也不反代模型 API。 |
| **执行端** | 以独立进程运行的 CLI。内置配置覆盖 Antigravity、Claude Code、Codex CLI、Cursor、DeepSeek Harness、Devin CLI、Grok Build、Kimi Code、MiniMax Code、OpenCode 和 ZCode；你机器上实际能用哪些，以 `list_agents` 为准。 |

这里的“本地”指的是**通讯桥运行在本机**，并不意味着提示词不出本机——各执行端仍旧连接各自的服务商与账号。任务 `cwd` 只是工作目录，不是安全沙箱。`$AGENT_BRIDGE_HOME`（默认 `~/.agent-bridge`）下的结果、转录和回执均为**明文**本地文件，请放在 Git 之外，分享前先检查内容。

## 工单怎么用？

| 你要做什么 | 用什么 |
| --- | --- |
| 查看可用执行端，或保存路由偏好 | `list_agents` · `set_preferences` |
| 派出一单并直接等待回执 | `dispatch_ticket`（默认 `agent="antigravity"`，默认等 480 秒；应短于宿主的 MCP 工具超时） |
| 并行派发多项独立工作，或续接已有会话 | `dispatch_task`；分别保留 `task_id`，再配合 `wait_task`（默认 180 秒）和 `get_result` |
| 查看静默执行端状态，或回看会话过程 | `check_task` · `get_transcript` |
| 取消当前轮次、查看会话或关闭执行端进程 | `cancel_task` · `list_sessions` · `end_session` |

**等待超时不等于任务失败。** 如果执行端长时间没有任何输出，会由独立的 `stall_timeout_sec` 静默看门狗处理（默认 1800 秒），可通过 `check_task` 返回的 `silent_for_sec` 区分。长结果按 `next_cursor` 翻页，直到 `has_more` 为 `false`。Bridge 不会自动重发失败的提示词。详见[工单与本地记录](SETUP.md#work-tickets-and-local-records)、[失败诊断](SETUP.md#acp-failure-diagnostics)和[额度读数](SETUP.md#remaining-quota-in-list_agents)。

**谁来决定要不要派工？** 默认模式 `auto` 交由主控自行权衡，首次连接不会询问你选择模式。希望只在点名时派工，设为 `manual`（服务端强制校验，需传入 `user_requested=true`）；希望多步任务优先外派，设为 `eager`。可在 `~/.agent-bridge/agents.toml` 里设置 `[coordinator] mode`，或通过 `AGENT_BRIDGE_MODE` 单独覆盖某个宿主。[中文协调规则书](ORCHESTRATION.zh-CN.md)是可选项；`[server] auto_install_skill` 默认关闭，不会在启动时自动安装 Skill。

## 使用本地看板

六库仙贼自带可选的本地用量与计费看板，且不注册进 MCP 工具列表（不会给主控增加工具上下文开销）。看板仅绑定 IPv4 回环地址（`127.0.0.1` 或 `localhost`，默认 `127.0.0.1:8765`，或沿用 `service.json` 中保存的地址与端口），本机进程可以访问。

```powershell
agent-bridge dashboard                 # 前台运行；绑定 127.0.0.1:8765（或 service.json 配置）并打开浏览器
agent-bridge dashboard --port 9000 --no-open
agent-bridge dashboard start           # 后台常驻运行（可按需加 --open 或 --tray）
agent-bridge dashboard stop            # 停止后台服务
agent-bridge dashboard restart         # 重启后台服务
agent-bridge dashboard status          # 仅打印服务状态 JSON
agent-bridge dashboard tray            # 启动 Windows 系统托盘（加 --stop 关闭）
```

- **前台交互与后台常驻的区别**：`agent-bridge dashboard`（即 `dashboard serve`，支持 `--host`、`--port`、`--no-open`）在前台终端中运行，按 `Ctrl+C` 停止，未加 `--no-open` 时会自动打开浏览器。`agent-bridge dashboard start`（支持 `--host`、`--port`、`--open`、`--tray`）则以脱离终端的后台服务方式运行（Windows 下不弹出控制台窗口，日志写入 `~/.agent-bridge/billing/dashboard.log`，仅在指定 `--open` 时打开浏览器）。开启 Windows 登录自启后，登录系统时会自动在后台静默拉起 `dashboard start`，进入登录后的后台常驻运行。
- **Windows 登录自启和托盘**：当前 Bridge 目录没有已保存设置时，第一次明确启动看板默认开启登录自启和托盘图标。如果登录项已被另一个 Bridge 目录占用，就保留原项。要更改设置，请在看板的**管理**页分别勾选或取消，再点「保存服务设置」。之后启动时会沿用你的选择。
- **查询状态不会顺带启用服务**：`agent-bridge dashboard status` 是纯只读查询，只会输出当前服务状态 JSON，**不会**启动后台服务、初始化首次配置、写入自启项或拉起托盘。
- **计量与费用仅为本地统计与估算**：计量模块会解析任务回执与状态 JSON（其中可能带有结果摘要），但只提取任务元数据与 Token 计数写入独立账本 `~/.agent-bridge/billing/usage_ledger.jsonl`（账本与导出文件均不含提示词和输出正文）。看板中的 Token 统计、缓存命中率、USD API 标价折算（基于内置公开参考价或你在 `pricing.json` 中的手动覆盖）以及 Harness 折扣规则，**都只是本地统计与估算，仅供对照参考，不是供应商真实账单，也不是计费依据**。未匹配到单价的模型或缺少明细计数的任务会被明确排除，不会按 `$0` 计入。
- **页面里的更新提示不会自动升级**：网页界面中的更新提示（以及 Windows 托盘菜单项和 `agent-bridge dashboard update`）只是提示，**不会**自动检测远程版本或执行升级。需要升级时，请按[更新方法](SETUP.md#update)自行操作。

更多滚动留存策略（默认 `100 MiB` / `180 天`）、单价覆盖和文件格式说明，见 [Usage and billing dashboard](SETUP.md#usage-and-billing-dashboard)。

## 扩展与开发

- [新增执行端](skills/add-worker/SKILL.md)或[接入另一种主控](skills/add-coordinator/SKILL.md)。遵循标准 ACP 的 CLI 通常只需补配置，不必写适配代码。
- [中文贡献指南](CONTRIBUTING.zh-CN.md)：在源码目录运行 `uv run --extra dev pytest`。`tests/` 用于 pytest 单元测试；真实联调用 `scripts/setup_lab.py` 生成的本机 `lab/`，不进 Git。
- [更新方法](SETUP.md#update)：先关闭正在使用 Bridge 的主控（如运行了后台看板也一并停止），执行 `agent-bridge upgrade`，再重新启动。本分支从 Git 安装，命令名为 `agent-bridge`，示例中的 Codex MCP 服务名为 `agent_bridge`。

本项目基于 FeiZhuLulu 的 **Agent-Bridge** 二次开发，采用 [AGPL-3.0-only](LICENSE)，保留[上游 MIT 许可与署名](LICENSES/Agent-Bridge-MIT.txt)。完整的许可与第三方署名见 [NOTICE](NOTICE.md)。
