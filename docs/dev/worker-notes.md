# Worker 共用筆記

Worker 是沒有記憶的獨立子代理，每次任務都從零開始，同一個網站被擋、同一個工具的坑會一再重踩。Worker 共用筆記是一份自由格式的檔案，讓 worker 自己留下「什麼方法有效、什麼失敗、為什麼」，下一個 worker 任務開頭就看得到。

## 檔案位置

- 預設 `<agent_os_dir>/worker-notes/notes.md`，由 `agents.worker.notes.path` 設定（相對 `agent_os_dir`）
- 刻意放在 `memory/` 之外：這不是 brain 的記憶，不受記憶治理、curation 或 backup 規則管轄
- 設定見 `cfgs/agent.yaml` 的 `agents.worker.notes`，schema 為 `WorkerNotesConfig`（`src/lincy/core/schema.py`）

## 運作

實作在 `src/lincy/worker/notes.py`，接線在 `src/lincy/cli/app.py`。

- **注入**：`WorkerRunner._build_user_message` 在每個任務的 user message 最前面放整份筆記（`[Worker notes]...[/Worker notes]`），在 `context_files` 之前；檔案空或不存在就不放。maintenance 直接派工（記憶治理）走同一個 runner，也會看到
- **補記**：worker-only 工具 `worker_note(text)`，經 `WorkerRunner(extra_tools=...)` 註冊（`tool_overrides` 只能替換共用 registry 已有的名稱，`worker_note` 不在共用 registry，brain 看不到）。每筆寫成 `- [YYYY-MM-DD] text`，在 lock 內 append
- **壓縮觸發**：`WorkerRunner.run()` 每次結束（成功、截斷、例外都算）呼叫 `WorkerNotes.compress`，檔案超過 `compress_threshold_chars` 才動作；失敗只記 warning，不影響任務結果
- **Snapshot-and-replace**：lock 內取 snapshot → 放開 lock 呼叫 compactor LLM（`compress_worker_notes`，用 `agents.compactor` 的 client）→ 重新取 lock 讀檔，若目前內容以 snapshot 開頭，寫入「摘要 + snapshot 之後新增的內容」（同目錄 temp file + `os.replace`）。若檔案已被另一個 worker 壓縮或手動改過（不以 snapshot 開頭），本輪放棄，等下一次任務結束再試
- **硬上限**：壓縮結果超過 `max_chars` 直接截斷；`max_chars` 必須小於 `compress_threshold_chars`（config 驗證）
- compactor 停用時不壓縮（啟動時記 info log），筆記會持續成長

## 它不是什麼

- 不是 brain 的記憶：brain 不讀、不維護它
- 沒有 schema 也不驗證內容：worker 可能記錯，後面的筆記會覆蓋前面的，壓縮時以較新的為準
- 不放任務資料、個人資料、帳密；規則寫在 worker system prompt 的 `Worker notes:` 段落，但程式不檢查

## 已知取捨

- 壓縮在 `run()` 結尾同步執行，worker 在 compactor 呼叫期間仍佔著 `task_max_concurrency` 的 slot
- `notes.enabled: false` 時 worker prompt 仍提到 `worker_note`，但工具不註冊；模型呼叫會得到 unknown tool 錯誤
