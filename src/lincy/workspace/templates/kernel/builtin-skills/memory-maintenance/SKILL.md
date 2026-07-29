---
name: memory-maintenance
description: "記憶檔案維護：重複移除、格式規範、檔案拆分。收到 memory_edit warning 或用戶要求整理記憶檔案時使用。"
---

# 記憶維護指南

## 用途

收到 `memory_edit` warning（`possible_duplicates`、`file_too_long`）時，或用戶要求整理記憶檔案時使用。

## 重複條目處理（possible_duplicates）

直接用 `memory_edit` 處理，不需要開 subprocess。

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

- **instruction 中的行內容必須從 `read_file` 結果原文複製**，不可改寫或省略任何字元
- 每條要刪除的行必須是完整的一行（含 checkbox prefix `- [ ]`、日期、全部文字）
- 一次 `memory_edit` 處理一個檔案的所有重複
- 不確定是否重複時，保留兩者，不要誤刪

## 大規模維護（file_too_long / 用戶要求）

檔案過長、結構重整、跨檔拆分等複雜任務，委派 `worker` 呼叫 Claude Sonnet（你自己沒有 shell 工具）：

1. `context_files` 帶入本 skill 的 `kernel/builtin-skills/memory-maintenance/references/rules.md`
2. `prompt` 中寫明目標檔案路徑、要做的整理，以及以下必須照做的執行方式：

```bash
cd {agent_os_dir} && claude -p --output-format stream-json --verbose "$(cat kernel/builtin-skills/memory-maintenance/references/rules.md)

任務：[具體任務描述，包含目標檔案路徑]" --model sonnet --max-turns 25 --allowedTools "Read,Write,Edit"
```

### 注意（任務單中要明確要求 worker 遵守）

- **必須使用 `--model sonnet`**，維護任務不需要 opus 等級
- **必須使用 `--output-format stream-json --verbose`**
- **必須使用 `--allowedTools "Read,Write,Edit"`**
- **嚴禁使用 `--dangerously-skip-permissions`**
- 工作目錄必須在 `{agent_os_dir}`
- 執行完畢後檢查結果，確認沒有內容遺失；worker 要在回報中說明改了哪些檔案
