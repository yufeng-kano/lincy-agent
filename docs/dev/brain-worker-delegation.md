# Brain 指令委派 worker

Brain 不再擁有 shell 工具，所有指令執行都必須委派 `worker` 子代理。

## Maintenance 直接派工（非同步 worker tool 的例外）

`cli/app.py` 建立 `WorkerRunner` 的時機提前到 `AgentCore` 建構之前，同一個
runner 實例同時給兩個呼叫者用：

- brain 的 async `worker` tool（本文件其餘部分描述的路徑）
- `AgentCore._perform_maintenance` 內的記憶檔案治理
  （`src/lincy/memory/curation/worker_dispatch.py`），在 maintenance 執行緒
  上同步呼叫 `WorkerRunner.run(...)`，不經過 tool call / 佇列 / 背景 thread

兩者共用同一個 runner，所以：

- fail-closed 記憶寫入規則、`tool_overrides`（unguarded file tools）、
  `excluded_tools` 完全相同，維護派工同樣要在 `prompt` 中明確指派記憶維護
  權限並用 `context_files` 帶入 `memory-maintenance` skill 的 `rules.md`
- session debug log 掛點也共用（見 `session-debug-logs.md`），用
  `worker_label`（`maintenance-digest`、`maintenance-curation`）在
  `requests.jsonl` / `responses.jsonl` 區分

## 非同步派工

`worker` tool 為非同步，實作範本對齊 `gui_task`（`src/lincy/gui/tool_adapter.py`）：

- Dispatch 立即回傳 `[WORKER DISPATCHED] worker-N (...)`，背景 daemon thread 跑 `WorkerRunner.run`
- 完成後把 `format_worker_result` 的內容包成 `InboundMessage(channel="worker", sender="system")` 塞回 queue；brain 於後續 turn 收到 `[worker, from system]` 訊息再接續處理
- 併發上限 `agents.worker.task_max_concurrency`（`BoundedSemaphore`；超過回傳 `[WORKER BUSY]`，不排隊）
- `create_worker_tool(queue=None)` 時退回同步執行（測試 / 直接呼叫相容）
- Registry 的 concurrency-safe 標記已移除：dispatch 是即時的，不需要 tool-loop 層的 ThreadPool 平行執行

## 記憶寫入政策

- 共用 registry 的 `write_file` / `edit_file` 是 guarded 版：`memory/` 路徑一律回 `Use memory_edit` 錯誤（`tool_setup.py` 的 `_is_memory_path` guard）
- Worker 透過 `WorkerRunner(tool_overrides=build_worker_file_tools(...))` 拿到**未加 memory guard** 的檔案工具：worker 是指定的記憶維護執行者，維護任務由 skill 的 rules.md 治理；若維持 guard，維護任務會被 memory_edit 限制卡死
- Brain 的日常記憶修改仍必須走 `memory_edit`（planner 契約、warnings、editor session log）；brain prompt 明訂唯一例外是 memory-maintenance 委派
- Worker prompt 有 fail-closed 規則：任務單沒有明確指派記憶維護（附規則）時，`memory/` 視為唯讀，需要寫入就停下回報
- Shell 層的 memory 寫入（`>>`、`tee`、`sed -i`、`rm`、`mv`）對 brain 與 worker 都維持封鎖

## CLI 顯示

- `WorkerRunner(ui_console=...)` 接主 console；worker 每個內部 tool call / result 以 `worker-N tool_name` 為名即時顯示在 TUI（`UiEventConsole.print_subagent_tool_call/result`）
- 顯示遵守 `tui.show_tool_use` 設定；失敗與帶 warning 的 result 一律顯示
- UI 發送包在 try/except：顯示層故障不得中斷 worker 執行

## Prompt cache

Worker 與 brain 共用 `src/lincy/context/cache_breakpoints.py`：

- `agents.worker.cache.enabled/ttl` 經 `resolve_breakpoint_cache_ttl` 夾到 provider 上限（claude_code 等為 `1h`）
- system message 掛 `cache_control`
- 每次 tool-loop request（compact 之後）呼叫 `advance_cache_breakpoint`，conversation-tier breakpoint 推到最新合格 message
- 不走 `ContextBuilder`（無 boot files / reminder）；只對齊 breakpoint TTL 與 tool-loop 推進行為

## 機制：per-agent `excluded_tools`

- `agents.{name}.excluded_tools`（`cfgs/agent.yaml`）是通用欄位，不再只給 worker 用
- Brain：`AgentCore(excluded_tools=...)` 會把共用 registry 包成 `FilteredToolRegistry`（`src/lincy/tools/registry.py`）
  - `get_definitions()` 濾掉被排除的工具（模型看不到）
  - `execute()` / `has_tool()` 對被排除的名稱回傳「unknown tool」錯誤（模型幻覺出工具名也執行不到）
  - 是 **live view** 不是快照：startup 之後才註冊的工具（`send_message`、`worker` 等）也看得到
- Worker：`WorkerRunner` 仍然拿 **raw registry** 自行 clone（`_build_filtered_registry`），兩邊的排除清單互相獨立
- 啟動驗證：所有 `registry.register()` 跑完後呼叫 `validate_excluded_tools()`（`src/lincy/agent/tool_setup.py`），排除清單裡有未註冊的工具名就 `SystemExit`
  - 注意 `gui_task` / `screenshot` / `screenshot_by_subagent` 是條件式註冊；若關掉 GUI 或改 vision 設定，worker 的排除清單要跟著調整

目前設定：brain 排除 `execute_shell` + `shell_task`；worker 排除 `gui_task`、`screenshot`、`shell_task`（保留 `execute_shell`）。

`screenshot_by_subagent` 不在排除清單內：brain 目前的 LLM chain 全為 vision 模型，`adaptive_own_vision` 不成立，該工具不會被註冊。若日後把 non-vision fallback 加回 brain chain，需同步補回排除項。

## 任務單規則（brain 端）

- `worker` 沒有本輪對話上下文，`prompt` 必須自包含：目標、完成條件、關鍵資訊原文
- 相關 `SKILL.md`（含它引用的參考檔）與需要的 `memory/` 檔案用 `context_files` 帶入
- 對外可見的提交（表單、寄信、下單）：關鍵欄位值必須逐項寫在 `prompt`，並要求 worker 只用任務單裡的值
- worker 回報缺資訊時補齊後重新委派，不可讓它猜

## Fail-closed（worker 端）

Worker system prompt 要求：缺必要資訊時停下來回報缺什麼，不猜、不編；對外提交前尤其如此；回報要寫出實際送出的值與未完成項目。

## shell_task 現況

`shell_task` 目前對 brain 與 worker 都排除，等於休眠；程式碼保留以便回滾。

## 回滾

刪掉 `cfgs/agent.yaml` 中對應的 `excluded_tools` 條目即可恢復，程式碼不需改動。
