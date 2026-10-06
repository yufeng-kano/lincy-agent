# Supervisor + Control API

**狀態**：完成

## 一句話目標

提供單一 `chat-supervisor` CLI 與對應 Control API，負責常駐服務管理、升級與手動切新 session。

## 目前介面

### CLI

`chat-supervisor` 必須帶 subcommand，不支援裸跑。

- `uv run chat-supervisor start`
- `uv run chat-supervisor status`
- `uv run chat-supervisor stop`
- `uv run chat-supervisor restart`
- `uv run chat-supervisor restart chat-cli`
- `uv run chat-supervisor upgrade`
- `uv run chat-supervisor reload`
- `uv run chat-supervisor new-session`

語意：
- `restart`：整套 managed stack restart
- `restart <name>`：只重啟單一 service
- `reload`：不重啟 process，只要求 `chat-cli` 重讀 system prompt 與 boot files
- `new-session`：不重啟 process，只要求 `chat-cli` archive 後切到新 session

### chat-cli Control API（port 9001）

| Method | Path | 說明 |
|--------|------|------|
| GET | `/health` | 存活檢查 |
| POST | `/shutdown` | Graceful shutdown |
| POST | `/reload` | reload system prompt + boot files |
| POST | `/session/new` | archive + fresh session rotation |

### Supervisor API（port 9000）

| Method | Path | 說明 |
|--------|------|------|
| GET | `/status` | 各進程狀態 + pid |
| POST | `/restart` | 重啟整套 managed stack |
| POST | `/restart/{name}` | 重啟指定進程 |
| POST | `/upgrade` | git pull + post_pull + restart cycle |
| POST | `/reload` | 轉發到 `chat-cli` 的 `/reload` |
| POST | `/new-session` | 轉發到 `chat-cli` 的 `/session/new` |
| POST | `/shutdown` | 停止所有進程 + 退出 |

## 設計重點

- 使用者介面收斂成單一 CLI：不再保留 `chat-supervisorctl`
- `reload` 是 context resource lifecycle，不是 process lifecycle
- `new-session` 是 session lifecycle，不是 process lifecycle
- supervisor 預設 `chat-cli` 啟動命令不帶 `--user`
- `chat-cli` 使用 `.env` / `CHAT_AGENT_USER` 解析 user
- spawn 子程序時將 repo 根目錄 `.env` 注入所有 managed process（`process.py` 的 `_dotenv_overlay`）；優先序為 per-process `env:` > `.env` > 繼承的 shell 環境，與 `lincy/core/config.py` 的「`.env` 優先」慣例一致。每次 spawn/restart 重讀，改 `.env` 後重啟該 process 即生效，不需重啟 supervisor
- self-restart 需重新執行 `python -m chat_supervisor start`
- auto-upgrade 在 `git pull` 後會重新載入有效 supervisor config；若 `cfgs/supervisor.yaml` 或 `cfgs/agent.yaml` 造成 process graph / auto-enabled 結果改變，會直接 self-restart，不再只跑舊 process 集合的 restart cycle
- 互動式 TUI process（目前是 `chat-cli`）不可用 `start_new_session=True` 啟動；否則它會離開前景 terminal process group，常見症狀是收不到 `SIGWINCH`，導致在 `ssh -> tmux` 下視窗 resize 後 UI 不重排

## 檔案結構

```
src/chat_supervisor/
├── __init__.py
├── __main__.py      # unified CLI: start/status/stop/restart/upgrade/reload/new-session
├── schema.py
├── config.py
├── process.py
├── server.py
└── scheduler.py

src/lincy/
├── control.py       # ControlServer: /health, /shutdown, /reload, /session/new
└── core/schema.py

cfgs/
├── agent.yaml
└── supervisor.yaml
```
