# 六库仙贼 · HexaHarbor

[English](README.md) · **简体中文**　|　[接入指南（英文）](SETUP.md) · [版本发布](https://github.com/WeiBeta/HexaHarbor-Agent-Bridge/releases) · [AGPL-3.0-only](LICENSE)

![夜色中的六边形港湾，两艘引航船在港口一出一进。](assets/hexaharbor-night-harbor.jpg)

**不必换掉手上的编程助手，也能把活交给另一位。** 比如你在 Codex 里写代码，想让 Antigravity 帮忙审一眼改动：六库仙贼把工单交给它自己的 CLI，做完再把结果带回当前对话。你不用在两个窗口之间搬提示词；接单的一方仍然用自己的工具、账号和权限。

## 派出第一单

先准备好 [uv](https://docs.astral.sh/uv/)、一个主控（如 Codex），以及至少一个已安装并登录的执行端 CLI。这里以 **Windows PowerShell + Codex + Antigravity** 为例：

```powershell
uv tool install git+https://github.com/WeiBeta/HexaHarbor-Agent-Bridge.git
$bridge = Join-Path (uv tool dir --bin) 'agent-bridge.exe'
codex mcp add agent_bridge -- $bridge
```

重启 Codex，打开一个有少量改动的 Git 项目，在对话里试试：

> 列出 Bridge 能用的执行端。如果 Antigravity 可用，请它审一下当前 Git diff，不修改文件。带回一条具体发现；没发现问题就直说。

先看 `list_agents`：Antigravity 应显示为可用，`coordinator.dispatch_enabled` 应为 true。然后用 `dispatch_ticket` 派单，`cwd` 指向项目目录。拿到回执后，可以自己看 `git diff` 核对。如果只是等待超时，**拿原来的 `task_id` 继续等，不要再派一单**。

主控不是 Codex？[按宿主接入](SETUP.md)里有 Cursor、Kimi Code、ZCode、Grok Build、Claude Code 各自的配置示例，也说明了 Devin 怎样复用 Cursor 配置。

## 谁负责哪一段？

| 角色 | 职责 |
| --- | --- |
| **主控** | 你发需求、收结果、做验收的对话。可以是 Codex、Cursor、Kimi Code、ZCode、Grok Build、Claude Code 或 Devin。 |
| **通讯桥** | 运行在本机的 MCP 服务，负责启动或续接执行端、跟踪工单、取回结果。不操作 GUI，也不反代模型 API。 |
| **执行端** | 用独立进程干活的 CLI。项目内置了 Antigravity、Claude Code、Codex CLI、Cursor、DeepSeek Harness、Devin CLI、Grok Build、Kimi Code、MiniMax Code、OpenCode 和 ZCode 的配置；本机到底能用哪些，以 `list_agents` 为准。 |

“本地”说的是**桥在本机运行**，不是承诺提示词不出机器。执行端照常连接自己的服务商、使用自己的账号。`cwd` 是工作目录，不是安全沙箱。`~/.agent-bridge` 下的结果、转录和回执是**明文**；不要提交到 Git，分享前先检查内容。

## 工单怎么用？

| 你要做什么 | 用什么 |
| --- | --- |
| 看哪些执行端可用，或记住路由偏好 | `list_agents` · `set_preferences` |
| 派一单，等待并取回回执 | `dispatch_ticket`（默认等 480 秒；应短于宿主的 MCP 工具超时） |
| 并行派几项独立工作，或续接已有会话 | `dispatch_task`；分别保留任务 ID，再用 `wait_task` 等待、`get_result` 取结果 |
| 看执行端是否仍有动静，或回看过程 | `check_task` · `get_transcript` |
| 取消本轮、查看会话、关闭执行端进程 | `cancel_task` · `list_sessions` · `end_session` |

**等超时不等于活干砸了。** 执行端长期没有输出才由另一道 `stall_timeout_sec` 静默检测处理（默认 1800 秒）；`check_task` 的 `silent_for_sec` 可以区分两者。长结果按 `next_cursor` 翻页，直到 `has_more` 为 false。Bridge 不自动重发失败的提示词。详见[工单与本地记录](SETUP.md#work-tickets-and-local-records)、[失败诊断](SETUP.md#acp-failure-diagnostics)和[额度读数](SETUP.md#remaining-quota-in-list_agents)。

**谁来决定要不要派工？** 默认 `auto` 由主控判断，首次连接不会询问。只想点名才派，用 `manual`；多步任务优先派，用 `eager`。可在 `~/.agent-bridge/agents.toml` 中设置 `[coordinator] mode`，也可以用 `AGENT_BRIDGE_MODE` 只覆盖某个宿主。[中文协调规则书](ORCHESTRATION.zh-CN.md)是可选项；默认不会自动安装 Skill。

## 扩展与开发

- **本地计量与计费看板**：运行 `agent-bridge dashboard`，可用 `start|stop|restart|status|tray` 管理后台服务与 Windows 托盘。看板监听 IPv4 回环地址，本机进程可以访问。看板显示 Token 用量、缓存命中率、USD 标价折算和 Harness 折扣，支持自动刷新和磁盘留存。计量从任务回执和保存的状态中提取元数据，写入 `~/.agent-bridge/billing/` 下的独立账本。配置与文件格式见 [Usage and billing dashboard](SETUP.md#usage-and-billing-dashboard)。
- [新增执行端](skills/add-worker/SKILL.md)或[接入另一种主控](skills/add-coordinator/SKILL.md)。遵循标准 ACP 的 CLI 往往只需补配置，不必写适配代码。
- [中文贡献指南](CONTRIBUTING.zh-CN.md)：在源码目录运行 `uv run --extra dev pytest`。`tests/` 只放 pytest 测试；真实联调用 `scripts/setup_lab.py` 生成的本地 `lab/`，不进 Git。
- [更新方法](SETUP.md#update)：先关掉正在使用 Bridge 的主控，运行 `agent-bridge upgrade`，再重启。本分支从 Git 安装，命令仍是 `agent-bridge`，示例中的 Codex MCP 名称为 `agent_bridge`。

本项目基于 FeiZhuLulu 的 **Agent-Bridge** 二次开发，采用 [AGPL-3.0-only](LICENSE)，保留[上游 MIT 许可与署名](LICENSES/Agent-Bridge-MIT.txt)。详见 [NOTICE](NOTICE.md)。
