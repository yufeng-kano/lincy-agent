# 維護機制

## 每日維護順序（實作，`AgentCore._perform_maintenance`）

1. **現況檔狀態債**：若 `_turns_since_memory_sync > 0`（有用戶回合沒寫 `temp-memory.md`），先跑一次 `[MEMORY SYNC - STATE REFRESH]` side-channel 讓 brain 整份重寫現況檔；只有 memory_edit 真的執行成功才歸零，否則債留到下一個清償點（`_settle_state_file_debt`）
2. **現況檔快照**：`snapshot_state_file()` 把 `temp-memory.md` 整檔寫到 `memory/archive/temp-memory/<昨天>.md`（已存在則附加），重建該目錄 `index.md`
3. **curation**：全掃超標檔案補入佇列，再由 worker 依 `memory-maintenance` skill 原地濃縮或拆檔（見 `../memory-curation.md`）
4. **context refresh**：清空對話、重載 system prompt 與 boot files、開新 session
5. **backup**：kernel 備份
6. **session cleanup**：`retention_days`（30）到期刪整個 session 目錄；`debug_retention_days`（7）到期先刪 `requests.jsonl`；`responses.jsonl`（dashboard 用量圖來源）、`messages.jsonl` 與 `transcript.jsonl` 留到 session 到期

現況檔與 recall 的設計見 `../memory-recall.md`。以下為早期設計文件。

## 歸檔觸發條件

### 知識歸檔 (knowledge/)

| 條件 | 操作 |
|------|------|
| 檔案超過 500 行 | 按主題拆分，舊檔移至 archive/ |
| 內容不再常用 | 移至 archive/knowledge/ |

### 思考歸檔 (thoughts/)

| 條件 | 操作 |
|------|------|
| 月份結束 | 建立新月份檔案 |
| 檔案過大 | 按週拆分，舊檔移至 archive/ |

### 經歷歸檔 (experiences/)

| 條件 | 操作 |
|------|------|
| 超過 30 天無互動 | 移至 archive/{user_id}/ |

### 用戶記憶歸檔 (people/)

| 條件 | 操作 |
|------|------|
| 對話結束 | 記錄至 archive/{user_id}/{date}.md |
| 超過 90 天無互動 | user-{user_id}.md 移至 archive/{user_id}/ |

## 載入規則

### 啟動載入流程

```
啟動
  ↓
memory/agent/temp-memory.md（今日現況檔）
  ↓
memory/agent/index.md
  ↓
├─ persona.md (必須)
├─ knowledge/index.md → 載入相關 knowledge
├─ thoughts/index.md → 載入當前月份 thoughts
├─ experiences/index.md → 載入最近接觸的 experiences
└─ personal-skills/index.md → 載入個人 skills（由 skill runtime 維護）
      ↓
根據當前對話人 → people/user-{user_id}.md
```

### 動態載入

| 情境 | 載入內容 |
|------|---------|
| 問及特定主題 | knowledge/{topic}.md |
| 問及特定時間 | thoughts/{month}.md |
| 問及特定人物 | experiences/{user_id}.md 或 people/user-{user_id}.md |

## BM25 檢索流程

### 檢索流程

```python
def retrieve_memory(query: str, context: dict) -> list[str]:
    """
    使用 BM25 檢索相關記憶
    """
    # 1. 確定檢索範圍
    search_paths = determine_search_scope(context)

    # 2. 載入 markdown 文件與 index.md 描述
    docs = load_markdown_documents(search_paths)
    docs = inject_index_descriptions(docs)

    # 3. 對 query 做 tokenize / 日期正規化後建立 BM25 查詢
    ranked = bm25_search(query, docs, top_k=8)

    # 4. 回傳片段而不是整檔
    return render_snippets(ranked, snippet_lines=3, max_chars=2000)
```

補充：BM25 模式可在 `tools.memory_search.bm25.exclude` 明確排除已經由 boot context 載入的檔案（例如 `memory/agent/temp-memory.md`），避免搜尋結果重複消耗片段預算。

### 檢索範圍決定

```python
def determine_search_scope(context: dict) -> list[str]:
    """
    根據對話情境決定檢索範圍
    """
    scope = []

    # 基礎範圍
    scope.append("memory/agent/knowledge/")
    # 根據情境擴充
    if "user_id" in context:
        scope.append(f"memory/people/user-{context['user_id']}.md")
        scope.append(f"memory/agent/experiences/{context['user_id']}.md")

    if "time_period" in context:
        scope.append(f"memory/agent/thoughts/{context['time_period']}.md")

    return scope
```

### 索引描述輔助召回

```python
def inject_index_descriptions(docs: list[Document]) -> list[Document]:
    """
    把 index.md 中的描述併入文件 token，提升概念型查詢召回
    """
    for doc in docs:
        desc = lookup_index_description(doc.path)
        if desc:
            doc.tokens.extend(tokenize(desc))
    return docs
```

## Agent 自我維護流程

### 定期維護

```python
def periodic_maintenance():
    """
    定期維護任務
    """
    # 1. 檢查檔案大小
    oversized_files = find_oversized_files("memory/", max_lines=500)
    for file in oversized_files:
        split_and_archive(file)

    # 2. 檢查過期內容
    stale_content = find_stale_content("memory/", days=30)
    for content in stale_content:
        archive_content(content)

    # 3. 更新索引
    update_all_indices()

    # 4. 壓縮近期記憶
    compact_recent_memory()
```

### 對話後維護

```python
def post_conversation_maintenance(conversation: dict):
    """
    對話結束後的維護
    """
    person = conversation["person"]

    # 1. 歸檔對話
    archive_conversation(conversation)

    # 2. 更新用戶記憶
    update_user_memory(person, conversation)

    # 3. 更新 Agent 經歷
    update_agent_experience(person, conversation)

    # 4. 更新索引
    update_indices()
```

## 檔案大小控制策略

| 類型 | 建議行數 | 拆分策略 |
|------|---------|---------|
| knowledge/ | 200-400 行 | 按子主題拆分 |
| thoughts/ | 300-500 行 | 按週拆分 |
| experiences/ | 200-300 行 | 按事件拆分 |
| user-{user_id}.md | 100-200 行 | 只保留摘要 |
| temp-memory.md | < 6000 字 | 整份改寫的今日現況；系統每日快照到 archive/temp-memory/ |

## 索引更新

### 更新觸發條件

- 新增或刪除檔案
- 歸檔操作後
- 對話結束後

### 更新內容

- 檔案列表
- 行數統計
- 最後更新時間
