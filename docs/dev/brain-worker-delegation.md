# Brain 指令委派 worker

Brain 不再擁有 shell 工具，所有指令執行都必須委派 `worker` 子代理。

## 非同步派工

`worker` tool 為非同步，實作範本對齊 `gui_task`（`src/lincy/gui/tool_adapter.py`）：

- Dispatch 立即回傳 `[WORKER DISPATCHED] worker-N (...)`，背景 daemon thread 跑 `WorkerRunner.run`
- 完成後把 `format_worker_result` 的內容包成 `InboundMessage(channel="worker", sender="system")` 塞回 queue；brain 於後續 turn 收到 `[worker, from system]` 訊息再接續處理
- 併發上限 `agents.worker.task_max_concurrency`（`BoundedSemaphore`；超過回傳 `[WORKER BUSY]`，不排隊）
- `create_worker_tool(queue=None)` 時退回同步執行（測試 / 直接呼叫相容）
- Registry 的 concurrency-safe 標記已移除：dispatch 是即時的，不需要 tool-loop 層的 ThreadPool 平行執行

## 機制：per-agent `excluded_tools`

- `agents.{name}.excluded_tools`（`cfgs/agent.yaml`）是通用欄位，不再只給 worker 用
- Brain：`AgentCore(excluded_tools=...)` 會把共用 registry 包成 `FilteredToolRegistry`（`src/lincy/tools/registry.py`）
  - `get_definitions()` 濾掉被排除的工具（模型看不到）
  - `execute()` / `has_tool()` 對被排除的名稱回傳「unknown tool」錯誤（模型幻覺出工具名也執行不到）
  - 是 **live view** 不是快照：startup 之後才註冊的工具（`send_message`、`worker` 等）也看得到
- Worker：`WorkerRunner` 仍然拿 **raw registry** 自行 clone（`_build_filtered_registry`），兩邊的排除清單互相獨立
- 啟動驗證：所有 `registry.register()` 跑完後呼叫 `validate_excluded_tools()`（`src/lincy/agent/tool_setup.py`），排除清單裡有未註冊的工具名就 `SystemExit`
  - 注意 `gui_task` / `screenshot` / `screenshot_by_subagent` 是條件式註冊；若關掉 GUI 或改 vision 設定，worker 的排除清單要跟著調整

目前設定：brain 排除 `execute_shell` + `shell_task`；worker 排除 `gui_task`、`screenshot`、`screenshot_by_subagent`、`shell_task`（保留 `execute_shell`）。

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
