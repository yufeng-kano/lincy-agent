# Host Runtime：單一 `lincy` 程序的分層、啟動階段、控制 API 與升級流程

本文件是 `host-runtime-refactor` 分支的設計契約。實作、code review、測試都對著這份文件看。
文件描述的是**重構後**的狀態；重構前的 `chat-supervisor` / `chat-cli` / `chat-web-api` 三程序架構只在「刪除與搬移清單」中出現。

## 目標與非目標

### 目標

- 一個程序、一個名字：`lincy`。沒有 supervisor、沒有 TUI、沒有第二個 HTTP server。
- 內部三層單向依賴：`host` → `agent` → `channels`。host 只透過 `AgentHandle` 碰 agent。
- 啟動分四個階段 `validate → build → run → web`，前兩階段失敗就 exit 非零，不留副作用。
- 升級是手動觸發（CLI 或 API），流程是 pull → sync → build → 子程序 `check` → 等 idle → `os.execv`。check 不過就 rollback 磁碟，繼續跑舊版。
- crash 重啟交給 launchd（LaunchAgent，`KeepAlive.SuccessfulExit=false`）。
- Web UI 的 Agent 頁補上 new session / compact / clear / reload / cancel 操作。

### 非目標（這輪不做）

- `cli` channel 改名。brain prompt 與持久化 session 檔都用這個字串，改名獨立成下一輪。
- Shell handoff 的 Web UI。這輪只提供 API，見「Shell handoff」。
- 任何相容層、fallback、feature flag。舊的 console script、舊的 yaml 欄位、舊的 API 路徑一律刪除，不留 alias。
- Log rotation。

## 分層

```
host      進入點、CLI、四個階段、HTTP server、control API、upgrade、launchd
  |  只透過 AgentHandle 往下
agent     AgentCore、turn loop、context、session、memory、tools、skills、LLM、build_agent
  |  只透過 InboundMessage / UiSink 往下
channels  console（原 cli）、gmail、discord、scheduler
```

- `ui/` 是 agent 層的輸出 port（typed UI event 與 sink），被 agent 與 web 使用。
- `web/` 是 dashboard 的 server side（metrics cache、pricing、watcher、routes）。它只讀檔案與 `UiEventStore`，不 import host。
- **Import 方向規則**：沒有任何模組 import `lincy.host`。`lincy.ui` 不 import `lincy.agent`。`textual` 不再出現在任何地方。由 `tests/test_import_direction.py` 守。

單一程序內的分層換不到 crash 隔離。隔離靠三個程序邊界：launchd（重啟）、`check` 子程序（用新程式碼驗證）、以及 `git reset --hard`（磁碟 rollback）。

## 套件結構

### 重構後

```
src/lincy/
  __init__.py
  __main__.py               python -m lincy -> host.cli.main
  host/
    __init__.py
    cli.py                  argparse 進入點（console script `lincy`）
    stages.py               validate() / build() / run()，web 初始化掛在 server lifespan
    runtime.py              HostRuntime：持有 BuiltAgent、server thread、狀態、signal、exit/exec
    app.py                  create_app(handle, event_store, info, upgrade, config, web=True)、include_web()
    control_api.py          APIRouter：/api/agent/*；RuntimeInfo(started_at, git_sha)；install_error_handlers()
    errors.py               HostError 與子類別（見「錯誤處理與 logging」）
    upgrade.py              UpgradeManager：git / uv sync / bun build / check / rollback / 狀態
    upgrade_notice.py       kernel 升級摘要在 check 與正式 start 之間的暫存檔
    service.py              launchd plist 產生、launchctl 包裝
    check.py                環境檢查：binary、PATH 補齊、port、web_ui dist、AX binary
    init.py                 `lincy init`（原 cli/init.py）
  agent/
    build.py                build_agent(...) -> BuiltAgent（原 cli/app.py main() 的組裝邏輯）
    handle.py               AgentHandle Protocol、AgentState、例外類別
    turn_cancel.py          TurnCancelController（原 tui/controller.py）
    ui_event_stream.py      UiEventExportSink、UiEventStore（位置不變；export sink 永遠開啟）
    adapters/console.py     ConsoleAdapter，channel_name 仍為 "cli"（原 adapters/cli.py）
    ...（其餘不動）
  ui/
    events.py               原 tui/events.py，事件型別全部保留（web 事件流的 wire schema 依賴它們）；InterruptPhase 也定義在這裡
    sink.py                 原 tui/sink.py，只剩 UiSink、FanoutUiSink
    formatting.py           原 tui/formatting.py
    formatter.py            原 cli/formatter.py
    claude_code_stream_json.py  原 cli/claude_code_stream_json.py
  web/
    api.py                  APIRouter：/api/dashboard、/api/sessions、/api/requests、/api/live、
                            /api/context/composition、/ws；static SPA mount
    lifespan.py             web_lifespan：背景載入 pricing、MetricsCache、watchers
    settings.py             WebSettings.from_config(config)
    state.py                WebState：MetricsCache 與 WebSocket 連線，router 與 lifespan 共用
    cache.py / pricing.py / session_reader.py / watcher.py / context_composition.py（原 chat_web_api）
src/web_ui/                 原 src/chat_web_ui（Vue 專案，bun build 輸出 dist/）
```

### 刪除

| 路徑 | 說明 |
|---|---|
| `src/chat_supervisor/` | 整個 package |
| `src/chat_web_api/` | 搬進 `lincy/web/`，`app.py` 與 `__main__.py` 刪除；`settings.py` 改為 `WebSettings.from_config(config)`，不再自己讀環境 |
| `src/lincy/cli/` | `app.py` 拆成 `agent/build.py`；`commands.py` 刪除（slash command 不再存在）；`init.py`、`formatter.py`、`claude_code_stream_json.py` 搬走 |
| `src/lincy/tui/` | `app.py`、`state.py` 刪除；`controller.py` 的 `TextualController` 刪除、`TurnCancelController` 搬到 `agent/turn_cancel.py`；其餘搬到 `ui/` |
| `src/lincy/control.py` | 由 `host/control_api.py` 取代 |
| `src/lincy/agent/web_chat.py`、`src/lincy/agent/adapters/web.py` | Web Chat channel 已被 Agent 頁取代，連同 `/api/chat/events` 一起刪除 |
| `src/lincy/session/picker.py` | 互動式 session picker，`--resume` 改為必須帶 id（Phase B） |
| `cfgs/supervisor.yaml` | 不再有 supervisor |
| `docs/dev/cli-ui/` | TUI 文件 |

### pyproject.toml

```toml
[project.scripts]
lincy = "lincy.host.cli:main"
permissions-warmup = "lincy.macos_permissions_warmup:main"

[tool.hatch.build.targets.wheel]
packages = ["src/lincy"]
```

`textual` 從 dependencies 移除。`rich` 保留給 `lincy init`。

## CLI

| 指令 | 做什麼 |
|---|---|
| `lincy start [--new \| --resume ID]` | 前景跑四個階段。預設接續最近一個 session |
| `lincy check` | 跑 validate（不探測 port、不載入 session）+ build 後退出。upgrade 流程用子程序跑它當 gate |
| `lincy init` | 初始化 workspace（原 `python -m lincy init`） |
| `lincy status` | `GET /api/agent/health`，印出 state、pid、session、git sha、upgrade 狀態 |
| `lincy stop` | `POST /api/agent/shutdown`，graceful，exit 0 |
| `lincy upgrade` | `POST /api/agent/upgrade` 後輪詢 health 直到結束，印出結果 |
| `lincy service install` | 寫 LaunchAgent plist 並 `launchctl bootstrap` |
| `lincy service uninstall` | `launchctl bootout` 並刪 plist |
| `lincy service start` | `launchctl kickstart -k` |
| `lincy service status` | `launchctl print` 摘要 |

- `--user` 刪除。使用者只從 `CHAT_AGENT_USER` 來（`.env` 優先，其次環境變數），缺就是 `ConfigInvalid`，exit 1。
- `--continue` 刪除（是預設行為）。`--resume` 必須帶 session id，互動式 picker 刪除。
- `status` / `stop` / `upgrade` 的連線位址讀 `cfgs/agent.yaml` 的 `app.server`，沒有 `--host` / `--port`。連不上時印 `Error: lincy is not running at ...`，exit 1。
- 只有 `start` / `check` 設定 logging；其他指令只印結果。
- `lincy` 不接受裸跑，沒有 subcommand 就印 usage 並 exit 2。
- 人類日常使用：`uv run lincy <cmd>`。launchd 與 execv 直接用 `.venv/bin/python -m lincy start`，不經過 `uv run`（`uv run` 預設每次會先 sync）。

## 啟動階段

| 階段 | 做什麼 | 失敗 |
|---|---|---|
| `validate` | 設定與 workspace 就緒 | exit 1 |
| `build` | 組出 agent，不寫檔（例外見 build 節）、不連網、不開 thread | exit 1 |
| `run` | HTTP server 起來、agent 開始跑 | 之後是 runtime 錯誤 |
| `web` | dashboard 資料初始化 | 非致命，health 回報 |

`lincy check` = `validate` + `build`。因為 build 沒有副作用，check 可以安全地把整個 agent 組一遍再丟掉，所以它驗的不只是 yaml，還有 import、tool 接線、prompt 檔存在與否。

check 呼叫 validate 時跳過 port 探測，並固定 `new_session=True`（不挑、不載入任何 session）。原因是 upgrade 在線上程序仍在跑時執行 check：server 佔著 port，探測必然失敗；agent 也正在往最新的 session 追加內容，check 不該去讀它。代價是 check 不驗 resume 路徑（session 檔內容），這部分由真正啟動時的 build 負責。

### validate

輸入：argv。輸出：`ValidatedEnv`（config、agent_os_dir、user_id、display_name、timezone、ax_binary、session 選擇）。

1. `load_config()`：agent.yaml + agent.override.yaml 合併、pydantic 嚴格驗證。`load_config` 的 `SystemExit`（它用來回報部分設定錯誤）、pydantic `ValidationError`、`yaml.YAMLError`、`FileNotFoundError` 一律轉成 `ConfigInvalid`。
2. `configure_runtime_timezone(config.app.timezone)`。
3. 解析使用者：`CHAT_AGENT_USER`（`.env` 優先，其次環境變數）。缺 → `ConfigInvalid`。
4. workspace 已初始化，否則 `WorkspaceNotReady`（訊息帶 `Run: uv run lincy init`），exit 1。
5. kernel migration：`WorkspaceInitializer.upgrade_kernel()`。這是 validate 裡唯一會寫入 workspace 的步驟，理由是 build 可能依賴 migration 帶進來的 prompt 檔。migration 是版本化、冪等、有 backup 的，允許在這裡執行。
6. `rebuild_personal_skills_index()`。
7. 使用者 selector 解析（失敗 → `WorkspaceNotReady`），並確保使用者 memory 檔存在（`ensure_user_memory_file`）。
8. 環境檢查（`host/check.py`），失敗 → `EnvironmentCheckFailed`：
   - `git`、`uv`、`bun`、`node` 在補齊後的 PATH 上找得到（`check.py` 的 `REQUIRED_BINARIES`）。node 是 `vue-tsc` 的 shebang 需要；launchd 的 PATH 是空的，少了它 `lincy upgrade` 會在 bun build 失敗後 rollback。
   - `app.server` port 沒被佔用（`lincy check` 跳過）。
   - `src/web_ui/dist/index.html` 存在，否則印 `cd src/web_ui && bun run build` 後 exit 1。
   - `agents.gui_manager.enabled` 時 `ensure_binary()`，沒 cache 就 build（這是 cache 建置，允許）。
9. session 選擇：`--new` → `None`；`--resume ID` → 該 id；預設 → `session_mgr.list_recent(user_id, limit=1)`，沒有就等同 `--new`。只讀。`--resume` 的 id 是否存在不在這裡檢查，由 build 的 `session_mgr.load()` 判定。

### build

輸入：`BuildInputs`（`config`、`agent_os_dir`、`user_id`、`display_name`、`resume_id`、`ax_binary`、`upgrade_message`，由 host 從 `ValidatedEnv` 組出）。輸出：`BuiltAgent`。

`build_agent(inputs)` 是原 `cli/app.py` `main()` 的組裝邏輯，純粹建物件。操作者可修正的問題 raise `BuildError`（`agent/build.py`），由 host 翻成 exit 1：

- `--resume ID` 找不到 → `BuildError("Session not found: ...")`
- brain / memory_editor / worker prompt 缺檔、`agents.memory_editor` 缺或未啟用

`agents.gui_manager.enabled` 但 `ax_binary` 是 `None`（validate 沒拿到 AX binary）不是 build 錯誤：GUI 工具不註冊，log 一行 error，其餘照常組裝。

下表是原本混在組裝裡的副作用，以及它們的新歸宿：

| 原本的副作用 | 新歸宿 |
|---|---|
| `PersistentPriorityQueue.__init__` 內呼叫 `_recover()` | 建構子不再 recover；新增 `recover()` 方法，`BuiltAgent.start()` 呼叫 |
| `session_mgr.create()` / resume 時 `rewrite_messages()` 寫回修補後的歷史 | `BuiltAgent.start()`；`remove_dangling_tool_calls()` 本身留在 build（只改記憶體），因為 render cache 匯入要對著修補後的歷史比對 |
| `session_mgr.load()` 把 `meta.json` 的 `status` 改回 `active` 並寫回 | 拆成 `SessionManager.mark_active()`，`BuiltAgent.start()` 在 resume 路徑呼叫 |
| `SessionDebugStore.__init__` 建 `checkpoints/` | 延到第一次寫 checkpoint / render cache 時才 `mkdir` |
| shared_state cache 的 rebuild 與 `save()` | `BuiltAgent.start()` |
| `UiEventStore.rotate_on_start()` | `BuiltAgent.start()` |
| `console.print_resume_history()` | `BuiltAgent.start()` |
| `initializer.upgrade_kernel()`、`rebuild_personal_skills_index()` | validate |
| `ensure_binary()` | validate，結果以 `ax_binary` 傳入 build |

build 期間可以讀檔（session 內容、memory、boot files、prompt 模板）。`session_mgr.load(resume_id)` 在 build 做（見下方邊界）；dangling tool call 修補的寫回在 start。

「build 不寫檔」的實際邊界，照實寫：

- 新 session 路徑不寫任何檔案。`tests/agent/build/test_build_side_effects.py`（`resume_id=None`）斷言 build 前後 workspace 的檔案集合不變，並確認 queue recover、ui_events 輪替、session 建立都等到 `start()` 才發生。
- 建構子仍會 `mkdir`：`SessionManager`、queue 的 `pending/` 與 `active/`、`MemoryBackupManager`、各 session store。測試只比對檔案，不比對目錄。
- resume 路徑也不寫檔：`session_mgr.load()` 只讀 `messages.jsonl`，並在記憶體裡把該 session 設為 current；`meta.json` 的 status 寫回在 `start()` 的 `mark_active()`。`test_build_resume_reads_session_without_writing` 建一個 `status=exited`、含 dangling tool call 的 session，斷言 build 前後所有檔案內容逐位元組不變，`start()` 之後 status 為 `active`、修補後的歷史已寫回。

建構子已確認不連網：LLM provider client 只在呼叫時發 request；`GmailAdapter` 只建 `httpx.Client`；`DiscordAdapter` 在 `start()` 才連線；`BM25MemorySearch` 只讀檔。

`BuiltAgent`：

```python
@dataclass
class BuiltAgent:
    core: AgentCore
    handle: AgentHandle
    ui_event_store: UiEventStore
    shell_task_manager: ShellTaskManager
    _startup: _Startup          # build 準備好、留給 start() 的狀態
    def start(self) -> None     # 上表列的副作用，依序執行
    def run(self) -> ExitReason # 先 handle.mark_ready()，再阻塞在 core.run()；回傳 SHUTDOWN 或 RESTART
    def close(self) -> None     # shell_task_manager.shutdown()
```

`start()` 的順序：

1. `ui_event_store.rotate_on_start()`：最先做，之後發出的事件才會落在本次執行的檔案。
2. `queue.recover()`。
3. shared_state cache 需要重建時 rebuild + `save()`。
4. 新 session → `session_mgr.create()`；resume → `session_mgr.mark_active()`，有修補時 `rewrite_messages()` 並印 info，再印 `Resumed session ...`。
5. resume 時 `console.print_resume_history()`。

### run

1. `RuntimeInfo(started_at, git_sha)`：`git_sha` 取 `git rev-parse --short HEAD`，失敗為 `unknown`。
2. 建 FastAPI app：`host/app.py` 的 `create_app(handle=, event_store=, info=, upgrade=, config=, web=True)` 依序 `install_error_handlers()`、include control router、`web=True` 時 `include_web()`（web router、web lifespan、最後掛 static SPA，因為 SPA fallback 會吃掉所有沒匹配的路徑）。`include_web()` 在函式內 import `lincy.web`，讓測試能只組 control app；這是 host 唯一的延遲 import，它只在啟動時跑一次，不會落在 upgrade 的 pull 之後。
3. 一個 uvicorn 在 daemon thread 跑，bind `app.server`。`log_config=None`：不用 uvicorn 自己的 logging 設定，uvicorn logger 往 root 的 stderr handler 傳。
4. 等 server `started`（上限 5 秒）。uvicorn bind 失敗時是結束 thread 而不是 raise，所以等待迴圈一看到 thread 死掉就立刻 `HostError`（exit 1），不等滿 5 秒。此時 `/api/agent/health` 的 `state` 是 `starting`。
5. `built.start()`。
6. 註冊 SIGTERM / SIGINT handler → `handle.request_shutdown(graceful=True)`；`run()` 結束後（含例外）還原原本的 handler。
7. 主執行緒呼叫 `built.run()` 阻塞；`run()` 自己先 `mark_ready()`，state 從此不再是 `starting`。
8. `run()` 回傳（`finally` 內 `built.close()`、通知 server 結束並 join 最多 5 秒）：
   - `SHUTDOWN` → exit 0。
   - `RESTART` → 先 flush stdout / stderr（`execv` 不會 flush Python 的 buffer），再 `os.execv(sys.executable, [sys.executable, "-m", "lincy", "start"])`。cwd 與 env 不變。

agent loop 在主執行緒而不是 daemon thread，signal 才能直接進 queue。HTTP server 在 thread 是因為它只是旁路，agent 死了它也該跟著結束。

### web

掛在 server lifespan（`include_web()` 設定 `app.router.lifespan_context`）。`web_lifespan` 把初始化丟進背景 task 後立刻 yield，server 與 `/api/agent/health` 不必等 pricing 的網路請求。背景 task 第一件事是啟動 ui_events watcher（agent 一 ready 就會產生事件，watcher 從檔案當下的結尾開始讀，晚啟動就會漏事件），然後才載入 pricing、建 `MetricsCache` 並在 thread 內 `refresh_all()`、啟動 session watcher。pricing 失敗只影響 monitor 那一半，Agent 頁的事件流照常。

- `app.state.web_status`（health 的 `web` 欄位）：初始化完成前 `loading`，成功 `ready`；任何例外只 log 並設為 `unavailable`，不影響 agent。理由：它是觀察者，pricing 來源是外網，不該讓離線時 agent 起不來。`create_app(web=False)` 不掛 web，health 回 `disabled`（只用於測試）。
- cache 還沒就緒（`loading` 或 `unavailable`）時，需要 `MetricsCache` 的 routes（`/api/dashboard`、`/api/sessions`、`/api/sessions/{id}`、`/api/requests`、`/api/live`）回 503 `{"detail": "dashboard data is not available"}`。`/api/context/composition` 直接讀檔，不受影響。
- server 關閉時 lifespan 設 stop event 並 cancel 初始化 task 與 watchers。

`lincy.web` 對 host 的介面（`host/app.py` 只用這些）：

| 介面 | 位置 | 說明 |
|---|---|---|
| `WebSettings.from_config(config)` | `web/settings.py` | 路徑與參數都由已載入的 `AppConfig` 推出，不讀環境 |
| `WebState()` | `web/state.py` | router 與 lifespan 共用：`cache`（就緒前為 `None`）、WebSocket 連線 |
| `create_router(settings, state)` | `web/api.py` | dashboard 的 `APIRouter` 與 `/ws` |
| `web_lifespan(app, settings, state)` | `web/lifespan.py` | async context manager，設定 `app.state.web_status`，初始化失敗不 raise |
| `mount_static(app, settings)` | `web/api.py` | `/assets` 與 SPA fallback，必須最後掛 |

## AgentHandle

`src/lincy/agent/handle.py`。host 對 agent 的全部認知。

```python
class AgentState(StrEnum):
    STARTING = "starting"
    READY = "ready"        # 沒有 turn 在跑
    BUSY = "busy"          # turn 進行中
    STOPPING = "stopping"

class ExitReason(StrEnum):
    SHUTDOWN = "shutdown"
    RESTART = "restart"

class AgentError(Exception):
    status_code: int = 500

class AgentBusy(AgentError):            # 409
class InvalidRequest(AgentError):       # 400：content 空白、shell text/key 不合法（reload target 經 HTTP 時先被 FastAPI 以 422 擋下）
class UnsupportedChannel(AgentError):   # 400
class ShellSessionNotFound(AgentError): # 404

class AgentHandle(Protocol):
    def state(self) -> AgentState: ...
    def session_id(self) -> str | None: ...
    def channels(self) -> list[str]: ...
    def submit(self, content: str, channel: str = "cli") -> None: ...
    def cancel_turn(self) -> None: ...
    def request_new_session(self) -> None: ...
    def request_compact(self) -> None: ...
    def request_clear(self) -> None: ...
    def request_reload(self, target: Literal["all", "system-prompt"]) -> None: ...
    def request_shutdown(self, *, graceful: bool = True) -> None: ...
    def request_restart(self) -> None: ...
    def token_status(self) -> str: ...
    def shell_sessions(self) -> list[dict]: ...
    def shell_send(self, session_id: str, *, text: str | None, key: str | None) -> str: ...
    def shell_cancel(self, session_id: str) -> str: ...
```

- `submit()` 的 channel 規則（預設 `cli`、`system` 不可送、必須是已註冊 adapter）從 `cli/app.py` 的 closure 搬進 handle 實作。channel 比對是精確比對，不做大小寫正規化。content strip 後為空 → `InvalidRequest`。`cli` 走 `ConsoleAdapter.submit()`，其他 channel 直接 `enqueue(InboundMessage(...))`，`metadata={"source": "web_console"}`。
- `_Handle.submit()` 在 `mark_ready()` 之前對**所有** channel raise `AgentBusy("Agent is still starting.")`。理由：HTTP server 先於 `start()` 起來，`queue.recover()` 還沒跑時 `put()` 的序號從 0 開始，會覆寫磁碟上既有的 pending 檔。`ConsoleAdapter.submit()` 自己也有同樣的檢查；上一個 `cli` turn 還沒結束時 raise `AgentBusy("Still processing the previous turn.")`。
- `state()` 的優先序：`STOPPING` > `STARTING` > `BUSY` > `READY`。`request_shutdown()` / `request_restart()` 一呼叫就進 `STOPPING`；`mark_ready()` 之前是 `STARTING`。
- `cancel_turn()` 只呼叫 `TurnCancelController.request()`，不丟 sentinel。
- `token_status()` 回傳 `AgentCore.get_token_status_text()`。`CtxStatusEvent`（wire `ctx_status`）原本由 Textual 輪詢後發出，現在由 `AgentCore._finalize_turn_token_status()` 在 brain turn 的 responder 結束、token 統計定案時，經 `UiEventConsole.print_ctx_status()` 發出，`text` 同 `get_token_status_text()`。Web Agent 頁 header 的 chip 靠它更新。
- `request_*` 全部是對 queue 丟 sentinel，由 agent thread 在 turn 邊界處理。新增 `CompactSentinel`、`ClearSentinel`（priority 0，與 `NewSessionSentinel` 相同）和 `RestartSentinel`（priority 999，與 `MaintenanceSentinel` 相同）。
- `RestartSentinel` 被 pop 出來時，依 priority queue 的定義，當下沒有任何 ready 的 inbound，這就是 idle。`AgentCore.run()` 執行 `graceful_exit()` 後回傳 `ExitReason.RESTART`。pop 之後才進來的訊息已持久化在 `queue/pending`，新程序 `recover()` 會撿回來（`cli` channel 除外，沿用現有 `discard_channels` 規則）。
- `_perform_compact()` 與 `_perform_clear()` 從 `adapters/cli.py` 的 `_handle_command` 搬進 `AgentCore`，連同原本印給使用者的訊息。
- Slash command 全部刪除。沒有 `/compact` 文字解析，只有 API。
- `AgentCore` 不直接實作 `AgentHandle`；`build.py` 內有一個 `_Handle` 類別把 `AgentCore`、`ConsoleAdapter`、`TurnCancelController`、`ShellTaskManager` 包起來。host 看不到這些型別。

## HTTP API

一個 FastAPI app，bind `app.server`（預設 `127.0.0.1:9002`）。

### Control（`host/control_api.py`，prefix `/api/agent`）

| Method | Path | 說明 | 回應 |
|---|---|---|---|
| GET | `/health` | 永遠可用 | `{status, state, pid, session_id, started_at, git_sha, upgrade, web}` |
| POST | `/shutdown` | graceful shutdown | 202 `{status: "shutting_down"}` |
| POST | `/upgrade` | 啟動升級，見「升級流程」 | 202 `{status: "started", from_sha}`；進行中 409 |
| GET | `/channels` | 可送出的 channel，`cli` 在前 | `{channels: [...]}` |
| POST | `/messages` | body `{content, channel?}` | 202 `{status: "accepted", channel}`；busy 409；channel 錯 400 |
| POST | `/turn/cancel` | 請求中止目前 turn | 202 |
| POST | `/session/new` | archive + 新 session | 202 |
| POST | `/session/compact` | 手動 compact | 202 |
| POST | `/session/clear` | 清空對話 | 202 |
| POST | `/reload` | body `{target: "all" \| "system-prompt"}`，預設 `all`；型別是 `Literal`，target 不合法由 FastAPI 回 422 | 202 `{status: "accepted", target}`；target 錯 422 |
| GET | `/events?limit=` | 當次執行的 UI event（原 `/api/agent/events`）。`limit` 預設 500，範圍 1..2000，超出範圍 422 | `{events: [...]}` |
| GET | `/shell/sessions` | shell_task 互動 session 清單 | `{sessions: [...]}` |
| POST | `/shell/sessions/{id}/input` | body `{text}` 或 `{key}`，key 為 `enter/up/down/left/right/tab/esc` | 200 `{result}` |
| POST | `/shell/sessions/{id}/cancel` | | 200 `{result}` |

- `health.upgrade`：`{state, from_sha, to_sha, error, started_at}`，`state` 見升級流程。
- `health.web`：`loading` / `ready` / `unavailable`。
- `install_error_handlers(app)` 在 app 層註冊 exception handler，涵蓋 `AgentError` 與 `HostError`（例如 `UpgradeInProgress` → 409），翻成 `{error: message}` 加該類別的 `status_code`。endpoint 內沒有 try/except，沒有 None 判斷。body / query 驗證錯誤是 FastAPI 預設的 422 `{detail: [...]}`。
- 所有 `request_*` 類 endpoint 回 202，因為動作是排進 queue 等 turn 邊界執行，不是同步完成。

### Web（`web/api.py`）

`/api/dashboard`、`/api/sessions`、`/api/sessions/{id}`、`/api/requests`、`/api/live`、`/api/context/composition`、`/ws`、static SPA fallback。行為與 `docs/dev/web-dashboard.md` 現有描述相同，差別只有：

- SPA fallback 只回傳 resolve 後仍在 `static_dir` 內的檔案；路徑是 URL decode 後才進 route，`/%2e%2e/...` 這類路徑會跳出 `static_dir`，一律改回 `index.html`。

- `/api/chat/*` 全部刪除。channels 與 messages 改用 `/api/agent/channels`、`/api/agent/messages`，不再經過 HTTP 轉發。
- `/api/agent/events` 移到 control router。
- `/health` 刪除，用 `/api/agent/health`。

### 前端（`src/web_ui`）

- `api/client.ts`：`/api/chat/channels` → `/api/agent/channels`，`/api/chat/messages` → `/api/agent/messages`。
- Agent 頁 header 加五個操作：New session、Compact、Clear、Reload、Cancel。Cancel 只在 `processing` 時可按。全部是 `POST /api/agent/...`，成功後不做樂觀更新，等事件流回來。沿用零彩色、mono、`#111827` 主色的既有風格。
- 文案中的 `chat-cli` 改成 `lincy`（`ChatPage.vue`、`MonitorContext.vue`、`agentEvents.ts` 註解）。
- `vite.config.ts` proxy 維持 `9002`。
- 驗證方式只有 `bun run build`，不開 dev server、不開瀏覽器。

## agent.yaml 欄位變更

| 原本 | 之後 | 說明 |
|---|---|---|
| `app.control: {enabled, host, port}` | `app.server: {host, port}` | 一個 server，永遠開，預設 `127.0.0.1:9002` |
| `tui: {...}` | `ui: {...}` | 欄位不變：`debug`、`show_tool_use`、`replay_turns`、`show_tool_calls` |
| `channels.web` | 刪除 | UI event export sink 永遠開啟，不再由它 gate |
| `ChannelsConfig._drop_removed_channels` | 刪除 | 相容 shim，`line_crack` 出現在 yaml 就報錯 |

pydantic 是 `extra="forbid"`，舊欄位留在 yaml 或 override 裡會在 validate 直接報錯，這是預期行為。

## 升級流程

`host/upgrade.py` 的 `UpgradeManager`。只有一份實作，CLI 與 Web 都打 `POST /api/agent/upgrade`。

狀態：`idle → fetching → pulling → syncing → building → checking → restart_pending`；失敗分支 `rolling_back → failed`；沒更新 `up_to_date`。

每個子程序都用同一份 env：目前的 `os.environ`，`PATH` 換成 `check.enriched_path()` 補齊後的值（launchd 給的 PATH 幾乎是空的，uv / bun 在使用者目錄）。

1. `start()` 在 lock 內執行：狀態不是 `idle` / `failed` / `up_to_date` → `UpgradeInProgress`（409）。`from_sha` 在這裡同步取 `git rev-parse HEAD`（202 回應要帶它），狀態設為 `fetching`，其餘步驟在 daemon thread 跑。
2. `git status --porcelain` 非空 → `failed`，error 說明 working tree dirty。rollback 會 `reset --hard`，髒的 tree 不能碰。
3. branch 取 `git rev-parse --abbrev-ref HEAD`，不寫死；結果是 `HEAD`（detached）→ `failed`。`git fetch origin <branch>`；`origin/<branch> == from_sha` → `up_to_date`。
4. `git pull --ff-only`。失敗 → `failed`，**不 rollback**：ff-only 失敗不會動到磁碟。步驟 2 到 4 的失敗都是直接 `failed`。
5. `uv sync`。
6. `bun run build`（cwd `src/web_ui`）。
7. `<sys.executable> -m lincy check`。步驟 5 到 7 任一 exit 非零 → rollback。
8. rollback：狀態 `rolling_back`，`git reset --hard <from_sha>`、`uv sync`、`bun run build`，最後 `failed`，error 帶失敗步驟的 stderr 尾段（最多 2000 字元）。rollback 自己某一步失敗時，在 error 後面附加 `rollback: ...` 一行，狀態仍是 `failed`（此時磁碟可能不一致，需要人工處理）。
9. pull 之前的任何例外（含 runner 本身拋出的）→ `failed`，磁碟沒動過所以不 rollback。pull 之後的任何例外一律走 rollback，不只 exit 非零：`subprocess.run` 找不到 `uv` / `bun` 時拋的是 `FileNotFoundError`，不是回傳碼。thread 最外層再接一次，狀態不會卡在進行中，否則之後每次升級請求都會 409。
10. check 通過：`handle.request_restart()`，狀態 `restart_pending`。agent 在 idle 時 `graceful_exit()`、回傳 `RESTART`，host `execv`。新程序起來後 `health.git_sha` 是新的、`upgrade.state` 是 `idle`。

步驟 4 到 7 之間，程序繼續接訊息。pull 之後若執行到還沒 import 過的模組會讀到新版程式碼，這個窗口以秒計，host 層本身不得有 lazy import。

為什麼 check 失敗一定要 rollback 磁碟：不 rollback 的話，磁碟是沒驗過的新版、記憶體是舊版，任何原因讓 launchd 重啟都會起到沒驗過的程式碼。

kernel migration 在 check 的 validate 階段已經套用，rollback 不會還原它。舊程式碼配新 kernel 與使用者手動降版是同一種情況，接受。

migration 的升級摘要（`format_startup_message()`）由 check 子程序產生，但它的 agent 隨即被丟掉，execv 後的正式 start 看到的 kernel 已是新版、`needs_upgrade()` 為 false。為了讓 heartbeat 的升級通知仍會發出，validate 會把摘要寫到 `state/pending_upgrade_notice.txt`（`host/upgrade_notice.py`），每次 validate 都從這個檔讀 `upgrade_message`，`HostRuntime.run()` 在 `built.start()` 之後刪掉它。

`lincy upgrade` CLI：先 GET health 記下 `git_sha`，POST 後每秒 GET health。任何一次 poll 看到 `git_sha` 與起始值不同就算完成（execv 後的新程序，exit 0）；`upgrade.state` 是 `up_to_date` → exit 0，`failed` → 印 error、exit 1。poll 期間的連線錯誤（execv 重啟中）一律忽略、繼續等。上限 15 分鐘，逾時 exit 1。

## launchd

`host/service.py`。`lincy service install` 寫入 `~/Library/LaunchAgents/com.lincy.agent.plist`：

```xml
Label                 com.lincy.agent
ProgramArguments      <repo>/.venv/bin/python -m lincy start
WorkingDirectory      <repo>
RunAtLoad             true
KeepAlive             { SuccessfulExit = false }
ThrottleInterval      10
StandardOutPath       <repo>/logs/lincy.log
StandardErrorPath     <repo>/logs/lincy.log
EnvironmentVariables  { PATH = <check.py 補齊後的 PATH> }
ProcessType           Interactive
```

- `<repo>` 用 `Path(__file__)` 推出的絕對路徑。
- 一定是 LaunchAgent（gui domain），GUI computer use 的 AX 權限需要使用者 session。
- `SuccessfulExit=false`：`lincy stop` exit 0 不會被拉起來；crash（非零）會，間隔 10 秒。
- execv 不換 PID，launchd 不會察覺升級重啟。
- install 之後執行 `launchctl bootstrap gui/<uid> <plist>`；uninstall 執行 `launchctl bootout gui/<uid>/com.lincy.agent` 再刪檔。
- `.env` 由 lincy 自己讀（`load_config` 與 `_resolve_user` 都走 dotenv），plist 不放 secret。

## 從舊架構切換到 host runtime 的部署順序

部署機器上若還有舊的 `chat-supervisor` 在跑，它每分鐘會自動 pull `main`。這個分支合併進 `main` 後被它拉下來，`uv sync` 會移除 `chat-cli` / `chat-supervisor` 腳本，restart cycle 失敗，self-restart 執行的 `python -m chat_supervisor start` 也不存在，服務會直接死掉且無法自救。順序必須是：

1. 在部署機器上先停掉舊 supervisor（或先把它的 `upgrade.auto_check` 關掉）。
2. 合併分支、`git pull`。
3. `uv sync`，`cd src/web_ui && bun install && bun run build`。
4. 檢查 `cfgs/agent.override.yaml`：含 `app.control`、`tui`、`channels.web` 的話會在 validate 直接報錯，這是預期的，改成新欄位。
5. `uv run lincy check` 通過後 `uv run lincy service install`。

## Shell handoff

`shell_task` 在背景 PTY 需要使用者接手時會發 `WarningEvent`。原本提示使用者打 `/shell-input` 等 slash command，這些已刪除。這輪：

- 提供 `/api/agent/shell/*` 三個 endpoint（見 API 表）。
- `shell_task.py` 的 handoff 文字改為描述 API：`Use POST /api/agent/shell/sessions/<id>/input to respond.`
- Web UI 的 shell 接手介面是後續工作，不在這輪。

## 錯誤處理與 logging

- `host/errors.py` 定義 `HostError(Exception)`（`status_code = 500`）與子類別：
  - `ConfigInvalid`：設定檔錯誤（`load_config` 的各種失敗）、缺 `CHAT_AGENT_USER`。
  - `WorkspaceNotReady`：workspace 未初始化、使用者 selector 解析失敗。
  - `EnvironmentCheckFailed`：port 被佔用、`web_ui/dist` 沒 build、AX binary 拿不到。
  - `BuildFailed`：包住 `agent/build.py` 的 `BuildError`。
  - `UpgradeInProgress`：`status_code = 409`。
- `stages` 與 CLI client 指令的失敗都 raise 這些，`cli.main()` 只在最外層接一次、印 `Error: ...`、exit 1；經 HTTP 時由 `install_error_handlers` 依 `status_code` 回應。
- logging 走 stderr，格式 `%(asctime)s [%(name)s] %(levelname)s: %(message)s`。launchd 把 stderr 導到 `logs/lincy.log`。沒有 fd 2 dup2，沒有 `chat-cli.stderr.log`。
- `AgentCore.run()` 內 per-turn 的例外處理不變。

## 測試

| 動作 | 路徑 |
|---|---|
| 刪除 | `tests/supervisor/`、`tests/tui/test_app.py`、`tests/tui/test_state.py`、`tests/lincy/test_control.py`、`tests/lincy/test_main_tty.py`、`tests/cli/test_commands.py`、`tests/web_api/test_chat.py` |
| 搬移並改指向 | `tests/cli/test_*wiring*.py`、`test_app_memory_editor_schema.py`、`test_app_startup_diagnostics.py` → `tests/agent/build/`，對象改為 `build_agent` |
| 搬移 | `tests/cli/test_formatter.py` → `tests/ui/`；`tests/tui/test_controller.py` 的 `TurnCancelController` 部分 → `tests/agent/test_turn_cancel.py`；`tests/web_api/*` → `tests/web/` |
| 新增 | `tests/host/test_stages.py`、`test_runtime.py`、`test_check.py`、`test_control_api.py`、`test_upgrade.py`（subprocess 以 monkeypatch 取代）、`test_service.py`（plist 內容）、`test_cli.py`；`tests/test_import_direction.py` |

`tests/agent/build/` 這個目錄名撞到兩個預設排除：pytest 預設 `norecursedirs` 含 `build`，`.gitignore` 也忽略 `build/`。因此 `pyproject.toml` 的 `[tool.pytest.ini_options]` 自訂了 `norecursedirs`（pytest 預設值去掉 `build`），`.gitignore` 加了 `!tests/agent/build/`。

`tests/test_import_direction.py` 取代 `tests/tui/test_structure_rules.py`：掃 `src/lincy` 所有 `.py`，斷言沒有 `textual` / `prompt_toolkit`、`lincy.host` 只被 `lincy/__main__.py` 與 host 自己 import、`lincy/ui/` 不 import `lincy.agent`。

## 文件與 README

- 新增本文件，`docs/dev/index.md` 加一列。
- 刪除 `docs/dev/cli-ui/`。
- `docs/dev/web-dashboard.md`：架構圖改成單一程序、刪除 Supervisor 整合章節、API 表依本文件更新、`chat_web_api` / `chat_web_ui` 路徑改名、`cli/app.py` 的組裝描述改指 `agent/build.py`。
- `docs/dev/local-config-override.md`：`chat_supervisor check` 與 `enabled: auto` 段落刪除，改寫為 `lincy check`。
- `docs/dev/gui-computer-use.md`：`ax-server-build` oneshot 改為 validate 階段的 `ensure_binary()`。
- `docs/dev/gmail-oauth-setup.md`：`uv run chat-cli --user` 改為 `.env` 設 `CHAT_AGENT_USER` 後 `uv run lincy start`。
- `docs/dev/task/supervisor.md`：狀態改為「已被 host-runtime 取代」，移到 `task/archive/`。
- `README.md`：Quick Start 改為 `uv sync` → `cp .env.example .env` → `uv run lincy init` → `cd src/web_ui && bun install && bun run build`（新 clone 沒有 `node_modules`）→ `uv run lincy start` 或 `uv run lincy service install`，並列出 `status` / `stop` / `upgrade` / `check`。刪除 tmux / TUI resize 疑難排解整段。Configuration 段刪 supervisor。

## Migration

無。這輪不改 kernel 檔案（prompt、builtin skills、info.yaml），`cfgs/agent.yaml` 在 git 內不走 migration。brain prompt 對 `cli` channel 的描述是「操作員」，不需要改。

## 實作順序

| 階段 | 內容 | 相依 | 狀態 |
|---|---|---|---|
| A | `build_agent` 從 `cli/app.py` 抽出；`tui/` → `ui/`；`AgentHandle`、sentinel、`ConsoleAdapter`；刪 Textual 與 slash command；`tests/cli` 搬移改指向 | 無，獨佔 | 完成 |
| B | `host/`：cli、stages、runtime、control_api、upgrade、service、check、init 搬移 | A | 完成 |
| C | `chat_web_api` → `lincy/web/`；`chat_web_ui` → `web_ui`；前端路徑與 Agent 頁操作；`bun run build` 過 | A | 完成 |
| D | 文件與 README | A | 完成 |
| E | 刪 `chat_supervisor`、`cfgs/supervisor.yaml`、pyproject scripts 與 packages、`tests/test_import_direction.py`、全套 `uv run pytest` | B、C、D | 完成 |

每個階段一個 commit，分支 `host-runtime-refactor`，不 push。E 另外收掉各階段 review 找到的問題：resume 的 build 不再寫 `meta.json`（`mark_active()`）、`ctx_status` 事件來源、SPA fallback 的 path traversal，並把 B 的實作細節併回本文件。
