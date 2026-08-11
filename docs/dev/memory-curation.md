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

### 3. Curator（maintenance 治理端）

新增 `memory_curator` agent（比照 `memory_editor`，用便宜模型），在 maintenance 的
`archive` 之後、`context_refresh` 之前執行（讓 boot file reload 吃到蒸餾結果）：

**a. temp-memory 蒸餾（取代原「歸檔即消失」）**

三層時間窗：

| 窗口 | 內容 | 依據設定 |
|------|------|----------|
| 近 `retain_days` 天 | 全文留在 temp-memory | `maintenance.archive.retain_days`（現 3） |
| 之後 `digest_retain_days` 天 | 原文歸檔 + 每日 digest 寫回 temp-memory | `maintenance.curate.digest_retain_days` |
| 更早 | digest 移除（原文永在 archive，BM25 可搜） | 同上 |

- digest 格式：`- [digest YYYY-MM-DD] ...（全文：memory/archive/temp-memory/YYYY-MM-DD.md）`
- digest 行不得匹配歸檔 parser 的日期正則（`[digest ` 前綴天然不匹配 `\[\d{4}-`），避免被重複歸檔；到期由 curate 步驟明確移除
- digest 由 curator LLM 產生，目標長度 `digest_max_chars`；須保留教訓、約定、情感脈絡與待追事項，不只是流水帳壓縮

**b. 超標檔案整理（消化佇列）**

對佇列中每個檔案：

1. 原文快照到 `memory/archive/curation/<relpath>/<YYYY-MM-DD>.md`（寫入成功才進行下一步）
2. curator LLM 改寫原檔為「頂部 current-state + 濃縮歷史 + archive 全文指標」
3. 成功則移出佇列；失敗則原檔不動、留在佇列明日重試

## 不變量

1. 改寫任何檔案前，原文必先成功寫入 archive
2. curator 永不修改或刪除 `memory/archive/` 下任何檔案
3. 每個 digest 必附全文的 archive 路徑
4. LLM 失敗的唯一後果是「今天沒瘦身」，絕不是「記憶消失」

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

agents:
  memory_curator:
    enabled: true
    llm: cfgs/llm/deepseek/deepseek-v4-flash/no-thinking.yaml
```

## 相關檔案

- `src/lincy/memory/editor/service.py`：`_check_file_warnings`（元件 1、2 掛點）
- `src/lincy/memory/hooks.py`：`check_and_archive_buffers`（元件 3a 擴充點）
- `src/lincy/agent/core.py`：`_perform_maintenance`（curate 步驟插入點）
- `src/lincy/core/schema.py`：`MemoryEditWarningsConfig`、`MaintenanceConfig`
