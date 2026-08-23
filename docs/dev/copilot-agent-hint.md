# GitHub Copilot Native Proxy

## 背景

GitHub Copilot 的請求會區分兩種 initiator：

- `user`: 視為使用者主動發起，會消耗 premium request
- `agent`: 視為 agent 在同一個工作流中的後續行為，不應重複計費

## 現行架構

### 1. initiator 由 dispatch mode 靜態決定

原本的 turn-scoped `CopilotRuntime`（inbound 分類 + `features.copilot.initiator_policy`
allowlist）已移除。現在 initiator 在組裝層由 client 的 dispatch mode 靜態決定：

- brain（`first_user_then_agent`）: `initiator=user`、`interaction_type=conversation-agent`
- sub-agent / one-shot client（`always_agent`，如 memory editor、vision、GUI worker）:
  `initiator=agent`、`interaction_type=conversation-subagent`

`interaction_id` 與 `request_id` 每發請求各自產生新的 UUID。

### 2. Native proxy 接受明確欄位

本專案不再對外暴露 OpenAI-compatible route；內部只打自家的 native proxy API：

```json
POST /chat
{
  "model": "gpt-5",
  "messages": [...],
  "tools": [...],
  "response_schema": {...},
  "max_tokens": 4096,
  "temperature": 0.2,
  "reasoning_effort": "medium",
  "initiator": "user",
  "interaction_id": "turn-uuid",
  "interaction_type": "conversation-agent",
  "request_id": "req-uuid"
}
```

proxy 再把這些欄位轉成 GitHub Copilot 上游需要的 headers，例如：

- `x-initiator`
- `x-interaction-id`
- `x-interaction-type`
- `x-request-id`

### 3. proxy 是獨立 executable

本地 proxy 由本專案自己的 `copilot-proxy` 執行檔提供，掛在 `cfgs/supervisor.yaml`，不再依賴外部 fork 的 Node.js `copilot-api`。

proxy 支援：

- `uv run proxy copilot` 或 `uv run proxy copilot serve`
- `uv run proxy copilot login`

`login` 會走 GitHub device flow，拿到 GitHub access token 後存到使用者自己的設定目錄，而不是 repo：

- macOS: `~/Library/Application Support/chat-agent/copilot-proxy/github-token.json`
- Linux: `~/.config/chat-agent/copilot-proxy/github-token.json`
- Windows: `%APPDATA%/chat-agent/copilot-proxy/github-token.json`

可用 `COPILOT_PROXY_TOKEN_PATH` 覆蓋路徑。runtime 讀 token 的優先序是：

1. `COPILOT_PROXY_GITHUB_TOKEN`
2. `GH_TOKEN`
3. `GITHUB_TOKEN`
4. token store 檔案

## 相關程式碼

| 檔案 | 說明 |
|------|------|
| `src/lincy/cli/app.py` | 在組裝層把 brain/sub-agent 對應到不同 dispatch mode |
| `src/lincy/llm/providers/copilot.py` | native proxy client，直接送 `/chat`；由 dispatch mode 決定 initiator |
| `src/copilot_proxy/service.py` | native request -> GitHub Copilot upstream payload / headers |
| `src/copilot_proxy/__main__.py` | `copilot-proxy` executable |
| `src/lincy/core/provider_schema.py` | Copilot proxy config schema |
| `cfgs/llm/copilot/*` | Copilot model profiles，`base_url` 指向 proxy root |
| `cfgs/supervisor.yaml` | 啟動 `copilot-proxy` process |
