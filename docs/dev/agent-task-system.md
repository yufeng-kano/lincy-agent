# Agent Task System + Note System

> 狀態：已實作

## 概述

結構化的 agent 待辦系統，結合 todo list 與日曆排程。讓 agent 能夠：

1. 追蹤持續性任務（不再依賴 memory 自由文字猜測）
2. 按排程自動喚醒處理到期任務
3. Heartbeat 時看到完整任務清單，主動提前備料

## 動機

現有架構缺口：

- Heartbeat 醒來後「不知道該做什麼」——只能翻 memory 猜
- `schedule_action` 是一次性鬧鐘，沒有持續追蹤
- 無法表達「每週一三五 9:00 做 X」這類重複工作
- 無法區分「到期才做」和「提前備料」——agent 需要看到完整清單才能自行判斷優先序

## 與現有系統的關係

| 系統 | 定位 | 保留 |
|------|------|------|
| `schedule_action` | 輕量一次性鬧鐘（「3pm 叫我」） | 保留，不變 |
| `agent_task` | 結構化持續任務（「每週一三五查信」） | **新增** |
| heartbeat | 自主喚醒 | 保留，新增 task list 注入 |

語意區分：`schedule_action` 是 wake-up 本身就是目的；`agent_task` 是「有事要做」，wake-up 只是手段。

## Task Schema

```json
{
  "id": "t_0001",
  "title": "查火車時刻表",
  "description": null,
  "status": "pending",
  "due": "2026-03-30T06:00",
  "recurrence": "daily@06:00",
  "source_app": "calendar",
  "source_id": "event-uid",
  "source_label": "prep",
  "created_at": "2026-03-29T22:00",
  "completed_at": null
}
```

| 欄位 | 型別 | 說明 |
|------|------|------|
| `id` | `str` | 自動產生，格式 `t_NNNN` |
| `title` | `str` | 簡短任務描述（高層意圖，非具體指令） |
| `description` | `str \| null` | 可選的補充說明 |
| `status` | `str` | `pending` / `completed` / `cancelled` |
| `due` | `ISO datetime \| null` | 下次到期時間（local time）；`null` = 無期限 |
| `recurrence` | `str \| null` | 重複規則（見下節）；`null` = 一次性 |
| `source_app` | `str \| null` | 外部來源 app，例如 `calendar`、`reminders` |
| `source_id` | `str \| null` | 外部來源 item id，例如 event uid / reminder id |
| `source_label` | `str \| null` | 同一來源下的補充標籤，例如 `prep`、`follow_up` |
| `created_at` | `ISO datetime` | 建立時間 |
| `completed_at` | `ISO datetime \| null` | 最近一次完成時間 |

### Task 描述原則

Task title/description 應該是**高層意圖**，不是具體指令：

```
✗ "查 08:00-10:00 台北到新竹的班次，存到 temp-memory"
✓ "查火車時刻表"
```

Agent 根據自己對用戶的理解（memory、對話上下文）動態決定細節。結果如何處理也由 agent 自行判斷。

## Recurrence 格式

Google Calendar 級別的彈性，字串格式讓 LLM 容易產生：

| 格式 | 說明 | 範例 |
|------|------|------|
| `daily@HH:MM` | 每天指定時間 | `daily@06:00` |
| `weekdays@HH:MM` | 週一到五 | `weekdays@09:00` |
| `weekly:D[,D...]@HH:MM` | 指定星期幾（ISO: 1=Mon…7=Sun） | `weekly:1,3,5@09:00` |
| `monthly:D@HH:MM` | 每月指定日期 | `monthly:1@10:00` |
| `every:Nh` / `every:Nm` | 固定間隔（從上次完成算起） | `every:2h` |

### Recurrence 運算

Complete 一個 recurring task 時：

1. 記錄 `completed_at`
2. 根據 recurrence 計算下一個 `due`：
   - `daily/weekdays/weekly/monthly`：推進到下一個符合規則的時間點
   - `every:Nh`：`completed_at + N hours`
3. `status` 重設為 `pending`
4. 塞新的 queue wake-up message（`not_before = 新 due`）

一次性 task（`recurrence=null`）complete 後 status 設為 `completed`，不再排程。

`agent_task(action="complete", ...)` 只代表 agent 自己的工作完成，例如已送提醒、已查資料、已整理資訊。它不代表使用者目標已閉環。若任務內容涉及用藥、健康、安全、行程、承諾，或仍需要確認使用者是否真的完成，必須另外用 `schedule_action` 排一次性追蹤；`agent_note` 只能保存狀態，不會喚醒 agent。

## Tool 定義：`agent_task`

```
agent_task(action, ...)
```

| Action | 參數 | 說明 |
|--------|------|------|
| `create` | `title`, `description?`, `due?`, `recurrence?` | 建立 task；有 `due` 自動排 wake-up |
| `complete` | `task_id` | 標記完成；recurring 自動排下一次 |
| `list` | — | 列出所有 pending tasks |
| `update` | `task_id`, `title?`, `description?`, `due?`, `recurrence?` | 修改 task；若 `due` 變更，重排 wake-up |
| `remove` | `task_id` | 刪除 task，移除對應 wake-up |

補充欄位：

- `source_app`
- `source_id`
- `source_label`

用途：

- task 若是從 Calendar / Reminders / Notes / Photos 衍生出來的 follow-up，應把來源帶進來
- 這樣 heartbeat 時看 task list，還知道它原本是從哪個外部 item 長出來的

## 觸發機制：雙層保障

### 1. Task Wake-up（精準觸發）

建立/完成有 `due` 的 task 時，自動在 queue 塞一個 `InboundMessage`：

```
channel: "system"
priority: 3          # 高於 heartbeat(5)，低於 schedule_action(2)
not_before: <due>
metadata: {"task_id": "t_0001", "task_due": true}

[TASK DUE]
Task: [t_0001] 查火車時刻表
Recurrence: daily@06:00

Process this task. When done, call agent_task(action="complete", task_id="t_0001").
```

Agent 收到後執行任務，完成後 call `complete`，系統自動排下一次。

### 2. Heartbeat 注入（全局視野）

每次 heartbeat 時，在訊息內容中注入完整 pending task list：

```
[HEARTBEAT]
Time: 2026-03-30 06:30

## Tasks (3)
- [t_0001] 查火車時刻表 (daily@06:00, overdue 30m)
- [t_0002] 查天氣 (daily@06:00, overdue 30m)
- [t_0003] 回覆 pending emails (weekly:1,3,5@09:00, due in 2h30m)

You have woken up spontaneously.
Check your memory for pending tasks, reminders, or anything
you want to tell the user. If nothing to do, do nothing.
```

用途：

- **安全網**：若 task wake-up 被錯過（agent 忙），heartbeat 補上
- **提前備料**：agent 看到 t_0003 還有 2.5h，可以決定提前開始準備
- **全局判斷**：agent 看到所有任務，自行安排優先序

### 觸發優先級

```
priority 0 : user direct messages (Discord, Gmail, CLI)
priority 2 : schedule_action (ad-hoc reminders)
priority 3 : task wake-up (structured tasks)
priority 5 : heartbeat (autonomous wake-up)
priority 999: maintenance
```

## Turn Eviction

Task due turn 遵循與 scheduled turn 相同的 eviction 邏輯：

- 有 `send_message` / `memory_edit` applied / `agent_task complete` → 保留
- 無任何 durable effect → 從 in-memory conversation 清除

在 `turn_effects.py` 中新增 `had_task_mutation` 判斷。

## 儲存

`state/tasks.json` — 與 queue 同層級的 runtime state。

```json
{
  "tasks": [
    {
      "id": "t_0001",
      "title": "查火車時刻表",
      "description": null,
      "status": "pending",
      "due": "2026-03-30T06:00",
      "recurrence": "daily@06:00",
      "created_at": "2026-03-29T22:00",
      "completed_at": null
    }
  ],
  "next_id": 2
}
```

啟動時載入；每次 mutation 後寫回。簡單 file lock 防並行寫入。

## Queue Wake-up 管理

每個有 `due` 的 task 同時有一個 queue message。需要管理一致性：

| 事件 | Queue 操作 |
|------|-----------|
| `create`（有 due） | 塞 wake-up message |
| `complete`（recurring） | 移除舊 wake-up，塞新 wake-up |
| `complete`（one-time） | 移除 wake-up |
| `update`（due 變更） | 移除舊 wake-up，塞新 wake-up |
| `remove` | 移除 wake-up |

辨識方式：queue message 的 `metadata.task_id` 對應 task id。

## Heartbeat 整合點

修改 `scheduler.py` 的 `make_heartbeat_message()`，接受 task list 參數：

```python
def make_heartbeat_message(
    *,
    not_before=None,
    interval_spec: str = "2h-5h",
    is_startup: bool = False,
    pending_tasks: list[Task] | None = None,  # NEW
) -> InboundMessage:
```

呼叫端（`AgentCore._schedule_next_heartbeat`）在建立 heartbeat 時注入 tasks。

或者更簡單：heartbeat 從 `not_before` 延遲觸發，tasks 在實際 promote 時才讀取（避免 task state 過期）。考慮到 heartbeat 可能幾小時後才觸發，**應在 heartbeat 被 promote / 實際處理時動態注入**，而不是建立時。

實作方案：在 `_process_inbound()` 中，偵測到 heartbeat message 時，動態附加 task list 到 content。

## 檔案清單（預估）

| 檔案 | 說明 |
|------|------|
| `src/lincy/tools/builtin/agent_task.py` | tool 實作 |
| `src/lincy/agent/task_store.py` | TaskStore：CRUD + 持久化 + recurrence 計算 |
| `src/lincy/agent/adapters/scheduler.py` | 修改：heartbeat 注入 task list |
| `src/lincy/agent/core.py` | 修改：task wake-up eviction、TaskStore 初始化 |
| `src/lincy/agent/turn_effects.py` | 修改：`had_task_mutation` |
| `src/lincy/agent/tool_setup.py` | 修改：註冊 `agent_task` tool |
| `src/lincy/tools/builtin/agent_note.py` | agent_note tool 實作 + guardrail enforcement |
| `src/lincy/agent/note_store.py` | NoteStore：CRUD + trigger matching + 持久化 |
| `src/lincy/agent/turn_overlay.py` | `[Runtime Context]` / `[Decision Reminder]` 文字組裝（純函式） |
| `src/lincy/agent/responder.py` | `_build_dynamic_turn_overlay`：組出 notes block 等並疊加到 outgoing request |
| `src/lincy/tools/registry.py` | 修改：`add_side_effect_tools` method |
| `src/lincy/cli/app.py` | 修改：初始化 stores、late tool registration |

---

## Agent Note System

### 概述

結構化 key-value 狀態追蹤。Agent 建立 notes 來追蹤用戶即時狀態（位置、行程等），每個 turn 都注入 context 讓 agent 隨時可用。

### 與其他系統的定位

| 層級 | 用途 | 更新速度 |
|------|------|---------|
| Memory | 長期知識（偏好、關係、歷史） | 慢，agent 主動寫 |
| **Note** | **即時狀態（位置、行程、當前活動）** | **快，trigger 自動提醒更新** |
| Conversation | 當前對話 | 即時，但 ephemeral |

### Note Schema

```json
{
  "key": "location",
  "value": "新竹",
  "triggers": ["到了", "回家", "出門", "抵達"],
  "description": "使用者目前位置",
  "source_app": null,
  "source_id": null,
  "source_label": null,
  "updated_at": "2026-03-29T14:00"
}
```

### Tool 定義：`agent_note`

| Action | 參數 | 說明 |
|--------|------|------|
| `create` | `key`, `value`, `triggers?`, `description?`, `source_app?`, `source_id?`, `source_label?` | 建立 note |
| `batch_update` | `updates[]`，每筆含 `key`, `value?`, `triggers?`, `description?`, `source_app?`, `source_id?`, `source_label?` | 更新既有 note；單筆更新也使用此 action |
| `list` | — | 列出所有 notes（含 triggers） |
| `remove` | `key` | 刪除 note |

Runtime 規則：`agent_note` 寫入是狀態提交工具，同一 turn 最多成功呼叫一次；若同輪需要改一個或多個 note，都必須用 `batch_update`。`list` 是唯讀，不占提交額度；但同一 turn 連續重複相同 `list` 會被 responder 擋下並結束 tool loop，以避免無意義的 API 花費與延遲。第二次成功後的重複寫入也會被擋下。

### Guardrails（`tools.agent_note`）

設定於 `cfgs/agent.yaml`（`core/schema.py:AgentNoteToolConfig`，載入時驗證為正整數）：

| 欄位 | 預設 | 說明 |
|------|------|------|
| `max_value_chars` | 80 | 單一 note value 上限（字元數） |
| `max_notes` | 12 | store 內 note 總數上限 |

行為：

- `create` / `batch_update` 寫入的 value 超過 `max_value_chars` → 直接 REJECT（error tool result，不改動 state），錯誤訊息會引導改寫到 `memory/agent/temp-memory.md`（過程細節）或 `memory/agent/long-term.md`（長期結論），例如 `Error: Note value too long (N > 80 chars). ...`
- `create` 時若 store 已達 `max_notes` → REJECT，訊息要求先合併或刪除既有 note
- 限制只套用在**新寫入**；既有已超長的 value 不會被拒絕或截斷，仍正常運作——每次成功呼叫（含 `list`）後，若 store 內仍有超過 `max_value_chars` 的既有 note，會在 tool result 後面追加一行 warning（例如 `warning: 3 notes exceed 80 chars: expenses_today(902), bedtime_med(850), noon_med(712) — compress them`），依長度遞減排序；這個 warning **不會**出現在注入 prompt 的 `[Agent Notes]` block 裡，只在 tool result 給 agent 看
- 要調整上限，改 `cfgs/agent.yaml` 的 `tools.agent_note.max_value_chars` / `max_notes` 即可，不需要改程式碼

### Context 注入

每輪 outgoing request 的 latest user message 都附加 notes block：

```
[Agent Notes]
location: "新竹" | updated_at 03-29 14:00
schedule_today: "14:00 開會" | updated_at 03-29 09:00
```

注入位置：**responder overlay**，不是 `ContextBuilder.build()`。實際組裝在 `agent/turn_overlay.py:build_dynamic_turn_overlay_text()`（純函式，組出 `[Runtime Context]` + `[Timing Notice]` + `[Decision Reminder]` + `[Agent Notes]`），再由 `agent/responder.py:_build_dynamic_turn_overlay()` 包成 overlay callable，接在 common-ground overlay 之前一起疊加到 outgoing request 的 latest user message（`core.py:_prepare_turn_attempt` 每個 turn attempt 只組裝一次並快取；同一 turn 內的每次 LLM call、包含 tool loop 重建與 overflow retry，都重複套用同一份快取文字，確保 prompt cache 的 BP4 仍然命中）。

`Conversation`、`ContextBuilder` 的 render cache、以及 session 的 `messages.jsonl` **永遠不會**含有這個 block——只有送給 LLM 的 request（`requests.jsonl` 記錄的內容）才看得到。

**Turn-snapshot 語意**：overlay 文字在 turn 開始時就快照定案；若 agent 在同一 turn 的 tool loop 中途改了某個 note，本輪剩餘的 LLM call 仍會看到 turn 開始時的舊值，直到**下一輪**才會看到新值。這是刻意設計，換取 prompt cache 穩定，不是 bug。

**既有 session 不會回溯**：這個改動生效前，舊 session 已經把 `[Agent Notes]` 等 block 凍結進 render cache（見 `docs/dev/token-only-context-policy.md`）。這些 session 沒有 migration，凍結副本會保留到下次 `context_refresh` 或 compaction 自然清掉為止。

注意：context 注入必須使用穩定字串（例如絕對時間），不可用 `2h ago` 這種相對時間；否則 prompt rebuild 時會因 wall clock 漂移破壞 prompt cache 前綴。

### Trigger 機制

1. 每個 note 可設定多個 trigger phrases
2. Inbound 非系統訊息到達時，substring match 所有 notes 的 triggers
3. 命中時注入 `[NOTE UPDATE]` 提示到 turn content：

```
[NOTE UPDATE] The following notes may need updating:
- location (current: "新竹")
Review and update these notes if the message indicates a change.
```

4. Agent 自行判斷是否更新——trigger 只是提醒，不是自動更新
5. False positive 成本極低（agent 無視即可）

### 儲存

`state/notes.json`：

```json
{
  "notes": {
    "location": {
      "value": "新竹",
      "triggers": ["到了", "回家", "出門"],
      "description": "使用者目前位置",
      "source_app": null,
      "source_id": null,
      "source_label": null,
      "updated_at": "2026-03-29T14:00:00+08:00"
    }
  }
}
```

## Calendar / Reminders 當作 user input

`calendar_tool` / `reminders_tool` 是外部真實資料來源。

現在不會再自動同步成 `agent_note`，也不會自動轉成排程。
要不要查、查完要不要建立 `agent_task` 或 `agent_note`，都由 LLM 當輪判斷。
