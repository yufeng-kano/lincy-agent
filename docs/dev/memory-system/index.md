# 記憶系統設計

本資料夾存放 Agent 記憶系統的設計文件。Agent 遇到記憶相關任務時應先讀取此檔。

**實作狀態**：已完成基礎架構（見 `conversation-with-memory` 任務）

## 核心概念

工作目錄 (`agent_os_dir`) 的核心資料分層：

- **kernel/** - 可升級的系統核心（system prompts、版本資訊）
- **memory/** - 可回憶的 live memory / people data（升級時不覆蓋）
- **personal-skills/** - 個人 skill packages（獨立於 memory）
- 其餘如 `artifacts/`、`state/` 為 runtime supporting data

```yaml
# agent.yaml（AppConfig 層級）
agent_os_dir: ~/.agent
```

## 初始化

```bash
uv run lincy init
```

## 文件列表

### 啟動機制

| 文件 | 說明 |
|------|------|
| [bootstrap.md](bootstrap.md) | Bootloader 啟動架構（kernel/system-prompts/brain.md） |
| [system-prompt.md](system-prompt.md) | System Prompt 設計與維護指南（v0.3.0） |

### 存儲層

| 文件 | 說明 |
|------|------|
| [architecture.md](architecture.md) | 記憶系統架構設計（檔案結構、目錄樹） |
| [agent-memory.md](agent-memory.md) | Agent 記憶系統詳述（live memory 與 skill subsystem 邊界） |
| [people-memory.md](people-memory.md) | 多人記憶系統詳述（用戶記憶、對話歸檔） |
| [maintenance.md](maintenance.md) | 維護機制（歸檔、載入、檢索、Grep 檢索流程） |
| [trigger-review.md](trigger-review.md) | Trigger Review 雙 LLM 架構（Pre-fetch + Post-review） |

### 行為層（心理驅動）

| 文件 | 說明 |
|------|------|
| [recent.md](inner-state.md) | 近期記憶（合併原 inner-state.md 與 short-term.md；內心狀態 + 短期工作記憶） |
| [pending-thoughts.md](pending-thoughts.md) | 念頭系統（待分享的念頭、靈感） |
| [interests.md](interests.md) | 興趣系統（真心感興趣、好奇想探索） |
| [journal.md](journal.md) | 日記系統（每日記錄、晚間反思） |

## 關鍵原則

1. **Memory 子樹每層都有索引** - `memory/` 內的資料夾以 `index.md` 導覽；skills 由獨立 skill subsystem 維護
2. **檔案大小可控** - 按主題/時間/人拆分，單檔約 200-500 行
3. **動態載入** - 根據需求載入，不是一次全部載入
4. **Agent 自我維護** - Agent 自己負責記憶的歸檔和更新
5. **使用 BM25 檢索** - 不用 RAG，靠索引描述 + 內容片段做確定性搜尋
