# 多人記憶系統

## 結構總覽

```
people/
├── index.md              # 總索引（所有用戶列表）
├── alice/
│   ├── index.md          # 導航（連結到 basic-info + 子檔案）
│   ├── basic-info.md     # 基本資料（Boot Context 載入）
│   ├── health.md         # 詳細主題資料
│   └── schedule.md
└── bob/
    ├── index.md          # 導航 stub
    └── basic-info.md     # 基本資料
```

## 記憶策略（所有人都有資料夾；重要的人才寫詳細）

`people/` 的目的，是讓 Agent 在「多人」情境下能正確區分人、且維持關係連續性；但不代表必須對每個 user 都建立完整長期記憶。

- `{user_id}/basic-info.md` 是「該人的基本資料與長期記憶摘要」，**每個人都應該有資料夾**：
  - 重要的人：維護較完整摘要 + 子檔案（偏好、健康、通勤等詳細主題）
  - 不重要或一次性互動：資料夾仍存在，但 `basic-info.md` 只保留極簡基本資訊
- `{user_id}/index.md` 是導航檔案，列出 `basic-info.md` 和所有子檔案的連結
- `user_id` 應視為穩定識別字（例如 `.env` 的 `CHAT_AGENT_USER`），不是顯示名稱
- 若 `{user_id}/` 不存在：視為新的人；啟動或首次互動時建立

## 歸屬判斷

- **用戶個人資料**（健康、偏好、行程、通勤、飲食）→ `people/{user_id}/{topic}.md`
- **通用知識**（非特定於某用戶的事實）→ `agent/knowledge/{topic}.md`

## 總索引 (people/index.md)

記錄所有用戶的概況和快速索引。

> 類型：`registry index`（名錄索引，table 格式）
>
> 維護責任：
> - `workspace.people` 負責新增/更新/移除列（`user_id`、`display_name`、`aliases`、`last_seen`）
> - `memory_edit` 的通用 index 自動維護**不直接寫這個檔案**
> - 執行中若 `memory_edit` 動到 `memory/people/{user_id}/...`，由 people registry 同步 hook 更新本檔

```markdown
# People Index

## People

| user_id | display_name | aliases | last_seen |
|---------|--------------|---------|-----------|
| alice | Alice |  | 2025-01-28 |
| bob | Bob |  | 2025-01-15 |
```

## 用戶資料夾 ({user_id}/)

### index.md — 導航

列出該用戶所有記憶檔案的連結，供搜尋引擎索引用。

> 類型：`nav index`（導覽索引，list 格式）
>
> 維護責任：`memory_edit` 通用 index 自動維護（新增/刪除子檔案時更新連結）

```markdown
# Alice

- [basic-info](basic-info.md)
- [health](health.md)
- [commute](commute.md)
```

### basic-info.md — 基本資料

Boot Context 載入的檔案。包含高層概況、基本資料、關鍵里程碑，以及子檔案連結。

```markdown
# Alice 記憶

## 基本

- 名稱: Alice
- 身份: 軟體工程師
- 首次接觸: 2025-01-20

## 特徵

- 偏好簡潔回答
- 關注架構設計

## 關鍵決定

- 選用 .md 檔案系統而非資料庫
- 使用 Grep 檢索而非 RAG
```

### 子檔案 — 詳細主題資料

當某個主題累積足夠細節時，拆分為獨立子檔案。新增子檔案時必須同步更新 `index.md` 的連結區段。

拆分門檻（經驗值，非硬限制）：
- 單一主題在 `basic-info.md` 內累積超過約 6-8 條細節
- 同一主題開始跨多日反覆更新（例如健康、作息、通勤）
- `basic-info.md` 接近約 120 行，已不利於快速掃描摘要

拆分後原則：
- `basic-info.md` 保留高層摘要與關鍵結論
- 細節移到子檔案（如 `health.md`、`schedule.md`）
- `index.md` 更新導覽連結，讓 `memory_search` 能發現

常見子檔案：

| 檔名 | 內容 |
|------|------|
| `health.md` | 健康狀況、用藥紀錄 |
| `schedule.md` | 行程與作息 |
| `commute.md` | 交通偏好 |
| `diet.md` | 飲食偏好與禁忌 |
| `habits.md` | 生活習慣 |

子檔案透過 `memory_search` 發現（搜尋引擎讀取 `index.md` 和 sibling 檔案清單）。
