# Memory Curation：記憶檔案自動化重量管理

本文件定義記憶檔案的自動化 context 重量治理機制。目標：**磁碟上永不刪除任何記憶**（陪伴型零損失），但 prompt 內的重量由系統自動控制，不依賴人工整理。

## 問題背景

- `temp-memory.md` 曾達 141k tokens（佔 400k 預算 35%），舊條目以全文形式滯留
- `memory/people/yufeng/health.md` 139 行卻 81KB——`max_lines` 警告度量錯誤，長行繞過檢查
- 系統中沒有任何元件把「檔案 token 重量」當成自己的職責

## 設計原則

1. **零記憶損失**：任何改寫前，原文必先成功落地 `memory/archive/`；LLM 失敗時不動原檔，下次 maintenance 重試（fail = no change）
2. **距離越遠、解析度越低**：近期全文在場、中期蒸餾為 digest 在場、遠期靠 BM25 檢索 archive
3. **度量用字元數**：遵循 `token-only-context-policy.md`，不做 tokenizer 預估、不呼叫 count API；字元數對治理觸發已足夠（中文約 1–1.5 token/字，預算保守抓）
4. **自動閉環**：超標 → 排入佇列 → 每日 maintenance 蒸餾，全程無人工介入

## 元件

### 1. 字元預算警告（memory_edit 寫入端）

- `MemoryEditWarningsConfig` 的 `max_lines` 改為 `max_chars`（預設 10000）
- 支援 per-pattern 覆蓋（`budgets`），`ignore` 清單語意不變（temp-memory.md 有自己的蒸餾機制，仍在 ignore 內）
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

執行時機：maintenance 的 `archive` 之後、`context_refresh` 之前（讓 boot file reload 吃到蒸餾結果）：

**a. temp-memory 蒸餾（取代原「歸檔即消失」）**

三層時間窗：

| 窗口 | 內容 | 依據設定 |
|------|------|----------|
| 近 `retain_days` 天 | 全文留在 temp-memory | `maintenance.archive.retain_days`（現 3） |
| 之後 `digest_retain_days` 天 | 原文歸檔 + 每日 digest 寫回 temp-memory | `maintenance.curate.digest_retain_days` |
| 更早 | digest 移除（原文永在 archive，BM25 可搜） | 同上 |

- digest 格式：`- [digest YYYY-MM-DD] ...（全文：memory/archive/temp-memory/YYYY-MM-DD.md）`
- digest 行不得匹配歸檔 parser 的日期正則（`[digest ` 前綴天然不匹配 `\[\d{4}-`），避免被重複歸檔；到期由 curate 步驟明確移除
- digest 由 worker 產生，目標長度 `digest_max_chars`；須保留教訓、約定、情感脈絡與待追事項，不只是流水帳壓縮
- **marker 去重**：實測 LLM 會自行輸出 `- [digest YYYY-MM-DD] ` 前綴導致重複
  （實際寫出 `- [digest 2026-08-08] - [digest 2026-08-08] ...`）。
  程式端必須剝除輸出開頭既有的 marker 再組裝，不可只靠 prompt 交代

**b. 超標檔案整理（消化佇列）**

對佇列中每個檔案：

1. 原文快照到 `memory/archive/curation/<relpath>/<YYYY-MM-DD>.md`（寫入成功才進行下一步）
2. worker 改寫原檔：可原地濃縮（current-state + 濃縮歷史 + archive 指標），
   也可按主題拆檔（拆出的新檔需在同目錄 `index.md` 登錄）
3. 成功則移出佇列；失敗則原檔不動、留在佇列明日重試

## 不變量

1. 改寫任何檔案前，原文必先成功寫入 archive
2. 治理流程永不修改或刪除 `memory/archive/` 下任何檔案
3. 每個 digest 必附全文的 archive 路徑
4. LLM 失敗的唯一後果是「今天沒瘦身」，絕不是「記憶消失」

## 可觀測性

curator/治理流程過去未掛 session debug log，導致「memory_curator 零次請求」看起來像沒執行，
實際上它有跑（靠 `memory/archive/curation/` 快照才確認）。
檔案治理的每次 LLM 呼叫都必須落到 session debug log（掛點見 `session-debug-logs.md`），可稽核。

## 設定

```yaml
tools:
  memory_edit:
    warnings:
      max_chars: 10000        # 取代 max_lines
      budgets: []             # 選配 per-pattern 覆蓋：[{pattern, max_chars}]
      ignore: [temp-memory.md, index.md, archive/]

maintenance:
  curate:
    enabled: true
    digest_retain_days: 14
    digest_max_chars: 1200
```

`agents.memory_curator` 已移除；檔案治理改用既有 `agents.worker`。

## 對話 compaction（同一套蒸餾紀律，另一條路徑）

檔案端做零損失的同時，對話端一直在「整段丟訊息」：
`compact_local` 只做 `conversation.compact(preserve_turns)`，無 LLM、無摘要，
而 codex remote 失敗時就 fallback 到它。這是實際存在的記憶損失。

改為三層，依 provider 能力分流：

| 順位 | 條件 | 行為 |
|------|------|------|
| 1 | brain client 是 codex（有 `compact_messages`） | codex remote compaction（維持現行） |
| 2 | 非 codex，或第 1 層失敗 | `compactor` agent 摘要式壓縮（保留教訓、約定、情感脈絡、待追事項） |
| 3 | 第 2 層失敗 | `compact_local` 原始行為（丟訊息）作為最後保底 |

- 第 1 層的判定沿用現行 `cli/app.py` 的 wiring（`features.codex_remote_compaction.enabled` + client 具備 `compact_messages`），行為不變
- 第 2 層由新的 `agents.compactor`（`CompactorAgent`）執行：保留最新 `context.preserve_turns` 輪不動，
  將更早的訊息轉成單一摘要文字訊息（`role=assistant`、標記 `rendered_static`）插在最前面，
  取代 `compact_local` 直接丟棄的部分；prompt 沿用「保留教訓、約定、情感脈絡、待追事項」的蒸餾紀律，並要求輸出語言與對話一致
- 第 2 層在 turn 關鍵路徑上（soft limit 觸發），模型選型需考慮延遲，與檔案治理的 03:00 批次不同；
  預設沿用 `agents.memory_editor` 同款快速模型（`deepseek-v4-flash`），不開額外 timeout/retry
- 三層皆以 exception 判定失敗並逐層 fallback；`fallback` flag 標記「因上一層失敗才落到此層」，
  三層皆失敗不得讓 turn 崩潰（`compact_local` 本身是純確定性操作，不會拋錯，是保證不崩潰的最終防線）
- 第 2 層的 LLM 呼叫會走 `session_debug_label="compactor"`，落在 session debug log 的
  `requests.jsonl`/`responses.jsonl`（見 `session-debug-logs.md`），可稽核

## 相關檔案

- `src/lincy/memory/editor/service.py`：`_check_file_warnings`（元件 1、2 掛點）
- `src/lincy/memory/hooks.py`：`check_and_archive_buffers`、`_format_digest`（元件 3a 擴充點 + marker 去重）
- `src/lincy/memory/curation/`：確定性骨架 + worker 派工（無專屬 curator agent）
  - `queue.py`：佇列讀寫去重（元件 2）
  - `budget.py`：字元預算 + ignore 規則，`_check_file_warnings` 與定期全掃共用
  - `scan.py`：`scan_over_budget_files`（元件 2b 定期全掃）
  - `snapshot.py`：`resolve_curation_target` / `write_verified_snapshot`（零損失快照保證，archive 路徑拒絕）
  - `worker_dispatch.py`：`digest_day_via_worker` / `curate_queue_via_worker`（驅動既有 `agents.worker`）
- `src/lincy/agent/core.py`：`_perform_maintenance`（curate 步驟插入點，持有 `worker_runner`）
- `src/lincy/core/schema.py`：`MemoryEditWarningsConfig`、`MaintenanceConfig`
- `src/lincy/agent/compaction.py`：`ContextCompactor.compact`（三層路由）、`compact_via_compactor_agent`（第 2 層）
- `src/lincy/agent/compactor_agent.py`：`CompactorAgent`（第 2 層摘要子代理）
- `src/lincy/cli/app.py`：`agents.compactor` wiring（含 session debug label）
- `src/lincy/workspace/templates/kernel/agents/compactor/prompts/system.md`：第 2 層 prompt template
- `src/lincy/workspace/migrations/m0172_compactor_agent.py`：既有 workspace 補齊 `agents.compactor`
