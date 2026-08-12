---
name: memory-maintenance
description: "記憶檔案維護：重複移除、格式規範、檔案拆分。收到 memory_edit possible_duplicates warning 或用戶明確要求整理記憶檔案時使用；file_too_long 已由每日 maintenance 自動排入佇列處理，不需要手動介入。"
---

# 記憶維護指南

## 用途

收到 `memory_edit` 的 `possible_duplicates` warning 時，或用戶明確要求整理記憶檔案時使用。

`file_too_long` warning 不適用本 skill 的自動觸發：超標檔案由每日 maintenance
的定期全掃 + 佇列機制自動處理（見 `docs/dev/memory-curation.md`），brain 看到
這個 warning 不需要、也不應該派工處理，避免和 maintenance 對同一檔案重複改寫。

## 重複條目處理（possible_duplicates）

規模小，直接用 `memory_edit` 處理，不需要委派 worker。

### 步驟

1. 用 `read_file` 讀取 warning 指出的檔案
2. 根據 warning 提示的行號，找出語義相似的條目組
3. 每組保留較完整或較新的版本，決定要刪除哪些行
4. 發一次 `memory_edit`，instruction 中**逐條列出要刪除的完整行內容**

### instruction 格式範例

```
Remove duplicate entries in memory/agent/long-term.md:
1. In ## 約定: remove '- [ ] [2026-03-15] Yu-Feng: 不要在訊息中使用顏文字' (keep the 03-20 entry which is more recent)
2. In ## 清單: remove '- [2026-03-10] Yu-Feng 常用的開發工具: VS Code, Claude Code, uv' (keep the 03-15 entry which is more complete)
```

### 重要事項

- **instruction 中的行內容必須從 `read_file` 結果原文複製**，不含行號前綴，不可改寫或省略任何字元
- 每條要刪除的行必須是完整的一行（含 checkbox prefix `- [ ]`、日期、全部文字）
- 一次 `memory_edit` 處理一個檔案的所有重複
- 不確定是否重複時，保留兩者，不要誤刪

## 大規模維護（用戶明確要求時）

用戶明確要求整理特定檔案（結構重整、跨檔拆分等複雜任務）時，委派 `worker` 直接整理。worker 有 `read_file` / `edit_file` / `write_file`，足以完成所有維護動作，不需要 shell 指令或 subprocess。

（超標檔案的自動整理不走這條路徑——那是 maintenance 直接派工 worker，不經過 brain。）

### 任務單寫法

1. `context_files` 帶入本 skill 的維護規則：`kernel/builtin-skills/memory-maintenance/references/rules.md`
2. `prompt` 必須自包含，寫明：
   - 目標檔案的**絕對路徑**，以及要做的整理（重整結構、拆分目標、搬移範圍）
   - 完成條件：內容零遺失、格式符合隨附的維護規則、相關 index.md 連結已更新
3. `prompt` 中明確要求 worker：
   - 只動任務單指定的檔案；`kernel/` 與 `persona.md` 嚴禁修改
   - 用 `read_file` / `edit_file` / `write_file` 編輯，不用 shell 指令改檔案
   - 回報中逐項列出：修改的檔案、每個檔案的改動摘要、未能完成的項目

### 收尾檢查

worker 為非同步：派工後照常回覆使用者，結果以 `[worker, from system]` 訊息送達。收到結果訊息後抽查：`read_file` 目標檔案與新建檔案，確認內容沒有遺失、對應 index.md 連結正確。結果不符時，把缺漏寫明重新委派修正。
