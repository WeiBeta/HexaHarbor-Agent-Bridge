# Attribution and licensing

HexaHarbor-Agent-Bridge (六库仙贼) is a modified distribution of
[Agent-Bridge](https://github.com/Fei2411/Agent-Bridge), based on commit
`5560e065f5aa7d7d5155cd17a28baa231208b7ed`.

Upstream copyright: Copyright (c) 2026 FeiZhuLulu.
The original MIT notice and permission terms are retained in
[LICENSES/Agent-Bridge-MIT.txt](LICENSES/Agent-Bridge-MIT.txt).
This attribution does not imply upstream endorsement of this fork.

The HexaHarbor distribution and its project-specific modifications are
provided under the GNU Affero General Public License, version 3 only
(`AGPL-3.0-only`); see [LICENSE](LICENSE). The upstream code remains
available under its original MIT terms. Third-party dependencies retain
their own licenses.

This fork's changes include configurable Antigravity execution flags,
Windows executable discovery, structured work-ticket receipts, optional
skill installation, worker filtering, receipt-finalization fixes, an optional
local usage/billing ledger and localhost dashboard, and updated documentation.
These changes began in November 2026.

## Third-party design and reference data attribution

The local billing ledger retention model, dashboard statistics/request-detail
layout, Windows WinForms system tray pattern, and built-in reference USD API
pricing table draw inspiration and verified public model price snapshots from
[opencodex](https://www.npmjs.com/package/@bitkyc08/opencodex)
(`@bitkyc08/opencodex` v2.100.6, MIT License).

Copyright (c) 2026 opencodex contributors.
The MIT notice and permission terms are retained in
[LICENSES/opencodex-MIT.txt](LICENSES/opencodex-MIT.txt).
This attribution does not imply endorsement by the opencodex project.

The Python distribution/module, CLI command, MCP configuration name, and
runtime-home names retain their existing `agent-bridge` / `agent_bridge`
forms for compatibility. They do not identify a separately published PyPI
release of HexaHarbor.
