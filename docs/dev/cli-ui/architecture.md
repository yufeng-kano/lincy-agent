# CLI UI 架構（Textual）

## 背景

原本 `chat-cli` 互動 UI 混用：

- `prompt_toolkit`（輸入 prompt / toolbar）
- `Rich` / `Rich Live`（spinner 與輸出）

這在背景通道（例如 Gmail）插入訊息時，容易出現 terminal redraw race，造成：

- `ctx` 顯示跑版
- 殘影
- 需要手動按 Enter 才恢復畫面

## 核心原則

### 1. 單一 Renderer

- 互動式 `chat-cli` UI 由 Textual 作為唯一 terminal renderer
- 其他執行緒不可直接操作 terminal（包含直接 `print()` 或 `Rich Console.print()`）

### 2. 型別化 UI Event

- Runtime 僅透過 `UiSink.emit(UiEvent)` 發送事件
- UI 僅消費事件並更新 `UiState`

目前已建立：

- `src/lincy/tui/events.py`
- `src/lincy/tui/sink.py`
- `src/lincy/tui/state.py`
- `src/lincy/tui/controller.py`
- `src/lincy/tui/app.py`

### `shell_task` Handoff

- `shell_task` 若在背景 PTY session 中偵測到需要使用者接手（例如 OAuth URL、貼回 code），runtime 會發送 `WarningEvent`
- 這類 handoff 提示只進本地 UI，不進 agent queue；避免每次等待輸入都觸發新的 agent turn
- 使用者透過本地 slash commands 接手：
  - `/shell-status`
  - `/shell-input`
  - `/shell-enter`
  - `/shell-up`
  - `/shell-down`
  - `/shell-left`
  - `/shell-right`
  - `/shell-tab`
  - `/shell-esc`
  - `/shell-cancel`
- 這些手動輸入不進 conversation/session log，避免把 code/token 寫入持久化紀錄

## 分層責任

### Runtime / Agent

- 產生 typed UI event
- 不直接觸碰 Textual widget

### Controller

- 接收 UI action（submit / interrupt / exit）
- 管理取消狀態（`TurnCancelController`）
- 推送狀態事件（例如 `CtxStatusEvent` / `InterruptStateEvent`）

### Textual App

- 唯一畫面 owner
- 消費 event 更新 log/status/input
- 綁定快捷鍵與輸入體驗
- 時間顯示需使用 app 設定時區（`cfgs/agent.yaml`），不可在 TUI 層直接用隱式本機時區 `.astimezone()`
- resize 問題要先區分「遠端 PTY 尺寸有沒有真的變」與「Textual 有沒有收到/處理 resize」：
  - 若 `ssh` 外層或 `tmux` 仍卡在舊尺寸，app 無法自行修正
  - 只有在 PTY 尺寸已更新、但 UI 漏掉 resize 傳遞或重排時，才屬於 app 層問題

## 中斷語意（目前階段）

- `Esc` 走 `TurnCancelController` 狀態機
- 目前先完成 UI 層可觀察狀態（`requested/pending/...`）
- Agent / tool 邊界的取消接線仍需後續 phase 完成

## 非 TTY 行為

- `chat-cli` 主模式在 `__main__` 已加入 fail-fast 檢查
- 若 `stdin/stdout` 非 TTY，直接以錯誤訊息退出

## 現況限制（請先理解）

- `AgentCore` 對外建構介面已收斂為 `UiSink`，內部仍使用 event-emitter UI port 封裝常用顯示方法（避免在 core 散落 event 組裝細節）
- `Esc` 中斷已改為 turn-level cancel request，但屬於「邊界安全中止」：
  - LLM 呼叫中無法瞬間硬中止 HTTP request
  - `execute_shell` 已支援 subprocess kill hook
  - `gui_task` manager loop 與 `wait` tool 已支援 cancel hook
  - GUI worker (`ask_worker`) 與其內部 LLM 呼叫仍未支援 in-flight 硬中止

## SSH / tmux Resize 排障

當使用者回報「terminal 視窗改變大小，但 TUI 不會跟著變」時，先不要直接改 Textual layout。先用下面順序判斷：

1. 在 `tmux` 外執行 `stty size`
2. 改變本機 terminal 視窗大小，再執行一次 `stty size`
3. 若數字不變，問題在 terminal app / SSH，與 app 無關
4. 若數字有變，再進 `tmux` 執行：

   `tmux display -p 'client=#{client_width}x#{client_height} window=#{window_width}x#{window_height} pane=#{pane_width}x#{pane_height}'`

5. 若 `client/window/pane` 尺寸不變，問題在 `tmux` session sizing，優先檢查：

   - `set -g window-size latest`
   - `setw -g aggressive-resize on`

6. 只有在 `tmux` 尺寸已正確更新後，`chat-cli` 仍不重排，才繼續往 `src/lincy/tui/app.py` 查

## 後續待完成

- GUI worker / provider in-flight 硬中止（更深層取消）
