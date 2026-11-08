# Memory Curation：記憶檔案自動化重量管理

本文件定義記憶檔案的自動化 context 重量治理機制。目標：**磁碟上永不刪除任何記憶**（陪伴型零損失），但 prompt 內的重量由系統自動控制，不依賴人工整理。

## 問題背景

- `temp-memory.md` 曾達 141k tokens（佔 400k 預算 35%），舊條目以全文形式滯留
- `memory/people/yufeng/health.md` 139 行卻 81KB——`max_lines` 警告度量錯誤，長行繞過檢查
- 系統中沒有任何元件把「檔案 token 重量」當成自己的職責

## 設計原則

1. **零記憶損失**：任何改寫前，原文必先成功落地 `memory/archive/`；LLM 失敗時不動原檔，下次 maintenance 重試（fail = no change）
2. **距離越遠、解析度越低**：今天的現況在場（`temp-memory.md`）、長期規則在場（`long-term.md`、pinned）、其餘靠 recall 子代理從每日快照與對話 transcript 檢索（見 `memory-recall.md`）
3. **度量用字元數**：遵循 `token-only-context-policy.md`，不做 tokenizer 預估、不呼叫 count API；字元數對治理觸發已足夠（中文約 1–1.5 token/字，預算保守抓）
4. **自動閉環**：超標 → 排入佇列 → 每日 maintenance 蒸餾，全程無人工介入

## 元件

### 1. 字元預算警告（memory_edit 寫入端）

- `MemoryEditWarningsConfig` 的 `max_lines` 改為 `max_chars`（預設 10000）
- 支援 per-pattern 覆蓋（`budgets`），`ignore` 清單語意不變（temp-memory.md 的長度由現況檔驗證 `temp_memory_max_chars` 管，仍在 ignore 內）
- 掛點：`src/lincy/memory/editor/service.py` 的 `_check_file_warnings`，每次寫入成功後量測

### 2. 超標佇列（結構化事件）

- 超過預算時除了回傳警告給模型，同時寫入 `state/memory-curation-queue.json`
- 條目：`{path, chars, budget, first_seen, last_seen}`，以 path 去重
- 純確定性邏輯，無 LLM

### 2b. 定期全掃（補寫入觸發的漏洞）

佇列只在 `memory_edit` 寫入時填入，**存量超標檔案永遠進不了佇列**。
實測：`long-term.md`（19,285 字，近兩倍預算、每輪注入的 boot file）從未被治理，
`health.md` 被處理純粹因為當天剛好被寫到。

因此 maintenance 需在消化佇列前先全掃 `memory/` 下所有檔案（套用同一套預算與 ignore 規則）補入佇列。

### 3. 檔案治理執行者：worker（不是專屬 curator）

原設計的 `memory_curator` agent **已廢除**。理由：

- 治理的價值 80% 在確定性骨架（預算、佇列、先快照再改寫、三層時間窗），LLM 層可替換
- 既有 `worker` + `kernel/builtin-skills/memory-maintenance` 已能做按主題拆檔，
  實測品質優於 curator 的原地壓縮（`health.md` 被拆成 health / history / mood / sleep / sexual 五個主題檔）
- 兩套並存造成同一檔案被雙重改寫，且舊路徑沒有快照保證

改為：**maintenance 驅動 worker 執行檔案治理**，保留 worker 的拆檔能力，同時繼承零損失不變量。

**brain 不得再自行派工做檔案瘦身**：`file_too_long` 類警告不對 brain 曝光
（或明示「已排入 curation，不要處理」），避免 brain 在對話中臨時起意派 worker 繞過快照保證。
`possible_duplicates` 的 skill 用途不受影響。

**載入檔只准原地濃縮，不准拆分**：`context.boot_files`、`context.boot_files_as_tool` 與 `state/pinned_context.json`
裡的檔案每輪都進 prompt，規則一旦被拆到未載入的分檔就等於消失。實例：2026-09-14 `long-term.md` 的約定被拆成六個分檔，
其後十一個 session 中 brain 只有一次讀過 `long-term-companion.md`，同型的「你都不理我」事件累計到第九次、每次都再往分檔補一條規則。
`curate_queue_via_worker(loaded_paths=...)` 由 `AgentCore._loaded_context_paths()` 提供這組路徑，
命中時 worker prompt 改為只准原地濃縮（合併同主題條目、去掉案例敘述、保留所有生效規則與指標），不得建新檔、不得改 index.md；
規則同步寫在 `memory-maintenance/references/rules.md`。

執行時機：maintenance 的現況檔快照之後、`context_refresh` 之前（讓 boot file reload 吃到整理結果）：

**a. 現況檔每日快照（取代 digest）**

`temp-memory.md` 是整份改寫的今日現況檔，沒有舊條目可歸檔。維護開頭 `snapshot_stale_state_file()` 依檔案最後寫入的本地日期，
早於今天就把整檔原樣寫到 `memory/archive/temp-memory/<該日>.md`（已存在就附加、不改寫），並重建該目錄 `index.md`。
沒有 digest、沒有 LLM：過去的事由 recall 子代理直接 grep 快照與 `transcript.jsonl`，不再把摘要寫回現況檔。
同一個函式也在 brain 跨日後第一次改寫現況檔前執行（`memory/editor/service.py` `_snapshot_state_file_before_rewrite`），
所以 00:00 到 03:00 之間的改寫不會讓前一天的最後版本消失，也不會混進前一天的分區。

**b. 超標檔案整理（消化佇列）**

對佇列中每個檔案：

1. 原文快照到 `memory/archive/curation/<relpath>/<YYYY-MM-DD>.md`（寫入成功才進行下一步）
2. worker 改寫原檔：可原地濃縮（current-state + 濃縮歷史 + archive 指標），
   也可按主題拆檔（拆出的新檔需在同目錄 `index.md` 登錄）
3. 成功則移出佇列；失敗則原檔不動、留在佇列明日重試

## 不變量

1. 改寫任何檔案前，原文必先成功寫入 archive
2. 治理流程永不修改或刪除 `memory/archive/` 下任何檔案
3. 現況檔快照只追加，不改寫既有快照
4. LLM 失敗的唯一後果是「今天沒瘦身」，絕不是「記憶消失」

## 可觀測性

curator/治理流程過去未掛 session debug log，導致「memory_curator 零次請求」看起來像沒執行，
實際上它有跑（靠 `memory/archive/curation/` 快照才確認）。
檔案治理的每次 LLM 呼叫都必須落到 session debug log（掛點見 `session-debug-logs.md`），可稽核。

## 設定

```yaml
tools:
  memory_edit:
    temp_memory_max_chars: 6000   # 現況檔整檔上限，超過即拒絕並還原
    warnings:
      max_chars: 10000        # 取代 max_lines
      budgets: []             # 選配 per-pattern 覆蓋：[{pattern, max_chars}]
      ignore: [temp-memory.md, index.md, archive/]

maintenance:
  curate:
    enabled: true
  session_file_cleanup:
    retention_days: 30
    debug_retention_days: 7   # 只提前刪 requests.jsonl；responses.jsonl 留給 dashboard
```

`maintenance.archive`、`curate.digest_*` 與 `tools.memory_sync.every_n_turns` 已移除（m0186）。

`agents.memory_curator` 已移除；檔案治理改用既有 `agents.worker`。

## 對話 compaction（同一套蒸餾紀律，另一條路徑）

檔案端做零損失的同時，對話端一直在「整段丟訊息」：
`compact_local` 只做 `conversation.compact(preserve_turns)`，無 LLM、無摘要。
這是實際存在的記憶損失。

改為兩層：

| 順位 | 條件 | 行為 |
|------|------|------|
| 1 | `agents.compactor` 已啟用 | `compactor` agent 摘要式壓縮（保留教訓、約定、情感脈絡、待追事項） |
| 2 | 第 1 層失敗或未啟用 | `compact_local` 原始行為（丟訊息）作為最後保底 |

- 第 1 層由 `agents.compactor`（`CompactorAgent`）執行：保留最新 `context.preserve_turns` 輪不動，
  將更早的訊息轉成單一摘要文字訊息（`role=assistant`、標記 `rendered_static`）插在最前面，
  取代 `compact_local` 直接丟棄的部分；prompt 沿用「保留教訓、約定、情感脈絡、待追事項」的蒸餾紀律，並要求輸出語言與對話一致
- 第 1 層在 turn 關鍵路徑上（soft limit 觸發），模型選型需考慮延遲，與檔案治理的 03:00 批次不同；
  但摘要失真的代價高於延遲，故 `cfgs/llm/kano-proxy/compactor.yaml` 採深度 tier，慢的情況交給 fallback，不開額外 timeout/retry
- 各層以 exception 判定失敗並逐層 fallback；`fallback` flag 標記「因上一層失敗才落到此層」，
  各層皆失敗不得讓 turn 崩潰（`compact_local` 本身是純確定性操作，不會拋錯，是保證不崩潰的最終防線）
- 第 1 層的 LLM 呼叫會走 `session_debug_label="compactor"`，落在 session debug log 的
  `requests.jsonl`/`responses.jsonl`（見 `session-debug-logs.md`），可稽核

## 相關檔案

- `src/lincy/memory/editor/service.py`：`_check_file_warnings`（元件 1、2 掛點）
- `src/lincy/memory/hooks.py`：`snapshot_state_file`（現況檔每日快照）
- `src/lincy/memory/curation/`：確定性骨架 + worker 派工（無專屬 curator agent）
  - `queue.py`：佇列讀寫去重（元件 2）
  - `budget.py`：字元預算 + ignore 規則，`_check_file_warnings` 與定期全掃共用
  - `scan.py`：`scan_over_budget_files`（元件 2b 定期全掃）
  - `snapshot.py`：`resolve_curation_target` / `write_verified_snapshot`（零損失快照保證，archive 路徑拒絕）
  - `worker_dispatch.py`：`curate_queue_via_worker`（驅動既有 `agents.worker`）
- `src/lincy/agent/core.py`：`_perform_maintenance`（curate 步驟插入點，持有 `worker_runner`）
- `src/lincy/core/schema.py`：`MemoryEditWarningsConfig`、`MaintenanceConfig`
- `src/lincy/agent/compaction.py`：`ContextCompactor.compact`（兩層路由）、`compact_via_compactor_agent`（第 1 層）
- `src/lincy/agent/compactor_agent.py`：`CompactorAgent`（第 1 層摘要子代理）
- `src/lincy/agent/build.py`：`agents.compactor` wiring（含 session debug label）
- `src/lincy/workspace/templates/kernel/agents/compactor/prompts/system.md`：第 1 層 prompt template
- `src/lincy/workspace/migrations/m0172_compactor_agent.py`：既有 workspace 補齊 `agents.compactor`
