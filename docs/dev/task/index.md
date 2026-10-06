# 任務索引

本資料夾存放待辦任務。Agent 開始工作前應先讀取此檔了解目前任務狀態。

## 操作規範

| 文件 | 說明 |
|------|------|
| [template.md](skills/template.md) | 任務文件格式模板 |
| [task-create.md](skills/task-create.md) | 建立任務的流程 |
| [task-archive.md](skills/task-archive.md) | 歸檔任務的流程 |

## 子資料夾

| 資料夾 | 說明 |
|--------|------|
| [archive/](archive/index.md) | 已完成並歸檔的任務 |

## 任務列表

| 文件 | 說明 | 狀態 | 依賴 |
|------|------|------|------|
| [remove-reviewer-shutdown.md](remove-reviewer-shutdown.md) | 移除 Reviewer + Shutdown 系統 | 完成 | - |
| [memory-import.md](memory-import.md) | Memory 匯入（ChatGPT/Claude/Line） | 草稿 | conversation-with-memory |
| [message-queue.md](message-queue.md) | Message Queue + Channel Adapter（設計文件） | 完成規劃 | - |
| [mq-phase1-agent-core.md](mq-phase1-agent-core.md) | 抽出 Agent Core | 完成 | message-queue |
| [mq-phase2-queue-protocol.md](mq-phase2-queue-protocol.md) | Message Queue + Channel Protocol | 完成 | remove-reviewer-shutdown, mq-phase1 |
| [mq-phase-gmail-adapter.md](mq-phase-gmail-adapter.md) | Gmail Adapter + Contact Map | 完成 | mq-phase2 |
| [mq-phase3-line-adapter.md](mq-phase3-line-adapter.md) | LINE Adapter（GUI-based） | 草稿 | mq-phase2 |
| [mq-phase4-system-adapter.md](mq-phase4-system-adapter.md) | System Adapter（排程提醒） | 草稿 | mq-phase2 |
| [mq-phase5-autonomous-exploration.md](mq-phase5-autonomous-exploration.md) | 自主探索（上網查資料、主動分享） | 草稿 | mq-phase4 |
| [fix-response-packaging.md](fix-response-packaging.md) | 修復 Response Section 缺失問題 | 完成 | mq-phase2 |
| [message-timestamps.md](message-timestamps.md) | 訊息時間戳完整注入 + 移除 get_current_time | 完成 | mq-phase2 |
| [sender-aware-messages.md](sender-aware-messages.md) | Sender-Aware 訊息標籤 + 移除靜態 current_user | 完成 | message-timestamps |
| [common-ground-time-anchor.md](common-ground-time-anchor.md) | Common Ground 時間錨點（避免時間旅行解讀） | 完成 | sender-aware-messages, mq-phase1 |
| [gui-native-input-backend.md](gui-native-input-backend.md) | GUI 後端改為自寫真實輸入（取代 OpenComputerUse） | 進行中（待真機回歸） | - |

## 狀態說明

```
草稿 → 完成規劃 → 進行中 → 完成 → (歸檔)
```

| 狀態 | 說明 | 可執行 |
|------|------|--------|
| 草稿 | 快速記錄想法，部分區塊可留空 | ❌ |
| 完成規劃 | 所有區塊填寫完整，可開始執行 | ✅ |
| 進行中 | 工作進行中 | - |
| 完成 | 已完成，等待歸檔 | - |

## Agent 行為

- **草稿任務不可執行**：遇到草稿狀態的任務，需先完成規劃才能執行
- 完成任務後，讀取 [task-archive.md](skills/task-archive.md) 執行歸檔流程
- 建立新任務時，依 [task-create.md](skills/task-create.md) 流程操作
