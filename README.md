# Lincy

像是真人的 AI 夥伴。

## Platform

macOS only. 本專案依賴 macOS system API（如 CoreImage `CIDetector`、FSEvents），不支援 Linux/Windows。

## Quick Start

```bash
# Install dependencies
uv sync

# Copy env template and set CHAT_AGENT_USER
cp .env.example .env

# Initialize workspace (first time only)
uv run lincy init

# Build the web dashboard (first time, and after frontend changes)
(cd src/web_ui && bun install && bun run build)

# Start the agent and the web dashboard in the foreground
uv run lincy start
```

`lincy` 是單一程序：agent 與 web dashboard（預設 `http://127.0.0.1:9002`，位址在 `cfgs/agent.yaml` 的 `app.server`）一起跑。使用者只從 `.env` 的 `CHAT_AGENT_USER` 讀取，缺少時直接報錯退出。

要開機自動啟動、crash 自動重啟，交給 launchd：

```bash
uv run lincy service install
```

其他常用指令：

```bash
uv run lincy status    # state、pid、session、git sha、升級狀態
uv run lincy stop      # graceful shutdown
uv run lincy upgrade   # git pull → uv sync → bun build → check，通過才重啟，失敗自動 rollback
uv run lincy check     # 只驗證設定與組裝，不啟動
```

設計細節見 [docs/dev/host-runtime.md](docs/dev/host-runtime.md)。

如果要使用 macOS 原生 app tools（Calendar、Reminders、Notes、Photos、Mail），第一次啟動前可以先觸發系統權限：

```bash
uv run permissions-warmup
```

這個指令只讀取少量 metadata，讓 macOS 連續跳出授權視窗；不會建立、更新、刪除資料，也不會寄信。若只想看會觸發哪些 app：

```bash
uv run permissions-warmup --list
```

## Secret 掃描

第一次 clone 後，先安裝本地 pre-commit hook：

```bash
uv run pre-commit install
```

手動做一輪全檔 secret 掃描時，執行：

```bash
uv run pre-commit run --all-files detect-secrets
```

repo 內的 `.secrets.baseline` 已關閉噪音很高的 `KeywordDetector`，避免一般 `api_key_env` 類型欄位造成誤報；高熵字串與常見 token detector 仍會照常檢查。

## Configuration

- Agent runtime: `cfgs/agent.yaml`（本機覆蓋用 `cfgs/agent.override.yaml`，見 [docs/dev/local-config-override.md](docs/dev/local-config-override.md)）
- LLM model profiles: `cfgs/llm/`（anthropic / kano-proxy / ollama / openai / openrouter）

