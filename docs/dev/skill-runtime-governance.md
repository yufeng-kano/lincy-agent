# Skill Runtime Governance

本文件定義 runtime 如何把 skill 從「提示建議」提升為「工具執行前置條件」。

## 概覽

- Skill 是純知識單元，格式對齊跨平台 Agent Skills 標準（`SKILL.md` + YAML frontmatter）
- Governance 是 runtime 層的策略，定義在 `cfgs/agent.yaml`，與 skill 分離
- 三層 skill root：builtin > personal > external

## Skill 格式

### SKILL.md

每個 skill 是獨立目錄，包含 `SKILL.md` 作為唯一入口：

```
skill-name/
├── SKILL.md              # 必要：frontmatter + 指示
├── scripts/              # 可選：可執行腳本
├── references/           # 可選：按需載入的參考文件
└── assets/               # 可選：模板、檔案
```

### Frontmatter

```yaml
---
name: skill-name          # 必填，max 64 chars，lowercase + hyphens
description: "..."        # 必填，max 1024 chars，第三人稱
---
```

## 三層 Skill Root

| 優先順序 | Root | 位置 | 用途 |
|---------|------|------|------|
| 1 | builtin | `{agent_os_dir}/kernel/builtin-skills/` | 系統內建，隨 kernel 升級 |
| 2 | personal | `{agent_os_dir}/personal-skills/` | agent 自己學的 |
| 3 | external | `~/.agents/skills/` | 生態系安裝（`npx skills add`） |

同名 skill：高優先順序贏，低優先順序跳過並 log warning。

## 索引維護

- `kernel/builtin-skills/index.md`：repo / kernel template 管理
- `personal-skills/index.md`：runtime 根據各 skill 的 `SKILL.md` frontmatter 自動重建
- external skills：不維護 `index.md`，直接掃描安裝目錄

`personal-skills/index.md` 的重建時機：

- workspace 初始化
- kernel migration 完成後
- CLI 啟動、boot files 載入前
- skill root hot reload / rescan 後

因此 agent 不應手動編輯 `personal-skills/index.md`，只需維護各 skill package 內的 `SKILL.md` 與補充檔案。

## Governance 規則

Governance 定義在 `cfgs/agent.yaml`，不在 skill 內：

```yaml
tools:
  skill_governance:
    external_skills_dir: "~/.agents/skills"
    rules:
      - skill: discord-messaging
        tool: send_message
        when:
          channel: discord
        enforcement: require_context
```

### 欄位語意

- `skill`：引用的 skill name
- `tool`：要治理的工具名
- `when`：工具參數精確比對條件
- `enforcement=require_context`：執行前模型本輪必須已看到 skill guide

### Startup 驗證

Registry 載入後，檢查每個 governance rule 引用的 skill 是否存在。不存在 → log warning（不 crash，skill 可能稍後安裝）。

## Preflight 機制

掛點在 `responder.py` 的 tool loop，是唯一的 guide 注入路徑（不分 channel）。
模型第一次要求受管工具時才注入 guide；沒有事前的 skill 預測步驟。

### 缺 prerequisite 時的行為

1. 先不執行該輪任何 tool side effect
2. 對該輪 tool calls 回傳 deferral tool result
3. 將 guide 內容以 synthetic assistant/tool pair 注入 conversation
4. 讓既有 while-loop 自然再呼叫模型一次

### Loaded guide 真相來源

掃描 `Conversation`，判斷規則：

- 若有 `_load_skill_prerequisite` 記錄（`skill_name` 或 `skill_id` 欄位）→ 已載入
- 若模型透過 `read_file` 讀過 guide path → 已載入
- Conversation compact 掉後 → 下次受管工具出現時重新注入

## Hot Reload

- 每輪 turn 開始前，檢查所有 root 下的 `SKILL.md` 檔案 mtime
- 有變動（新增、刪除、修改）→ 重新載入 registry
- 不使用 filesystem watcher

## 向後相容

### meta.yaml 退場

- 若目錄有 `SKILL.md` → 使用 SKILL.md，忽略 meta.yaml
- 若目錄只有 `meta.yaml`（無 SKILL.md）→ fallback 使用，emit deprecation warning
- 若 `SKILL.md` 存在但 frontmatter 無效 → hard fail（跳過 skill），不 fallback meta.yaml

### Conversation 向後相容

既有 conversation 中的 synthetic `_load_skill_prerequisite` 訊息可能使用 `skill_id` 欄位。Runtime 同時檢查 `skill_name` 和 `skill_id`，確保舊 session 不中斷。

## 邊界

- 這套機制保證的是「執行前，本輪上下文已看過 guide」
- 不保證每輪都真的重新呼叫 `read_file`
- Governance 是系統策略，skill 本身不知道自己被 govern
