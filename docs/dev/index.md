# 開發文件索引

本資料夾存放開發相關文件。

## 啟動指示

1. 若用戶有明確任務，根據任務載入對應文件
2. 若用戶無明確任務，遞歸讀取子資料夾的 index.md，尋找待處理事項
3. 建立或修改 skill 時，須先讀取 [skills-guide.md](skills-guide.md)

## Skills 機制

本專案的文件採用類似 Claude Skills 的動態載入機制：
- 每份 `.md` 文件可視為一個 skill
- 當任務需要建立新的 skill 時，應建立對應的 `.md` 文件
- 新增文件後須更新相關的 `index.md`

## 文件列表

| 文件 | 說明 |
|------|------|
| [skills-guide.md](skills-guide.md) | Skills 建立與修改指南（必讀） |
| [skill-runtime-governance.md](skill-runtime-governance.md) | Skill prerequisite runtime 治理：`meta.yaml`、tool preflight、synthetic guide 注入 |
| [copilot-agent-hint.md](copilot-agent-hint.md) | Copilot premium request 省費機制 |
| [copilot-staged-planning.md](copilot-staged-planning.md) | Copilot brain 三階段（看/想/做）流程與上下文邊界 |
| [heartbeat.md](heartbeat.md) | 自主喚醒系統（Heartbeat + Scheduled Actions） |
| [provider-api-spec.md](provider-api-spec.md) | LLM Provider API 規格盤點（官方事實 / adapter 規則 / 實測逆向） |
| [provider-architecture.md](provider-architecture.md) | LLM Provider 自治架構準則（config/client/factory 邊界） |
| [session-debug-logs.md](session-debug-logs.md) | Brain session debug-first 診斷檔案格式與目前 resume 邊界 |
| [codex-cache-survival.md](codex-cache-survival.md) | Codex prompt cache 存活觀察：本地 key 旋轉、官方 CLI 行為、session 實測 |
| [token-only-context-policy.md](token-only-context-policy.md) | Token-only 上下文策略（soft limit、Copilot usage 缺值、overflow fallback） |
| [agent-task-system.md](agent-task-system.md) | Agent Task System：結構化待辦 + 排程重複 + heartbeat 注入 |
| [macos-app-tools.md](macos-app-tools.md) | macOS 原生個人資料工具：Calendar / Reminders / Notes / Photos 的 tool 設計與使用規則 |
| [web-dashboard.md](web-dashboard.md) | Web Dashboard：chat_web_api + chat_web_ui 架構、API、前端設計、注意事項 |
| [gui-computer-use.md](gui-computer-use.md) | GUI Computer Use：AX-first 架構、MCP server vendor 規範、context 管理策略 |
| [brain-worker-delegation.md](brain-worker-delegation.md) | Brain 無 shell 工具：per-agent `excluded_tools` 機制、任務單規則、worker fail-closed 協定 |
| [memory-curation.md](memory-curation.md) | 記憶檔案自動化重量管理（字元預算警告、超標佇列、curator 蒸餾）與對話 compaction 三層架構（codex remote / compactor agent / local） |
| [local-config-override.md](local-config-override.md) | `cfgs/agent.override.yaml` 本機設定覆蓋：合併規則、統一讀取路徑 |

## 子資料夾

| 資料夾 | 說明 |
|--------|------|
| [goals/](goals/index.md) | 專案目標與願景 |
| [eval/](eval/index.md) | 技術評估與測試文件 |
| [project-setup/](project-setup/index.md) | 專案環境設置文件 |
| [task/](task/index.md) | 待辦任務 |
| [memory-system/](memory-system/index.md) | 記憶系統設計 |
| [cli-ui/](cli-ui/index.md) | CLI UI（Textual）架構與擴充指南 |
