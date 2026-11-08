# Memory Recall：今日現況檔 + recall 子代理

## 結論

brain 只知道載入 context 的東西，其他記憶等於不存在；而 `temp-memory.md` 以前是 append-only 日誌，54% 是內部回合的流水帳。這版把兩件事拆開：

- `temp-memory.md` 變成**今日現況檔**，整份改寫、固定四段、6000 字上限，只描述「現在」。
- 過去的事交給 **recall 子代理**：Haiku 在私有工具迴圈裡 grep `memory/**/*.md`、每日現況快照與 `session/brain/*/transcript.jsonl`，回傳帶時間與來源的事實。每則用戶訊息系統先預抓一次附成 `[Recall]`，brain 也可以主動呼叫 `recall`。

研究與實測依據：原始 transcript 的 grep 檢索勝過整理式記憶（Raw-RAG、Is Grep All You Need、Anthropic 的 agent memory 元件文）；Haiku 5.5 + grep 在 12 題已知答案上 12/12、約 7 秒、每題約 0.005 美元。

## 現況檔

```
# 今日現況

## 最後互動
## 今日時間線
## 未閉環
## 下一步
```

| 規則 | 落點 |
|------|------|
| 第一行 `# 今日現況`，四段順序固定，整檔 ≤ `tools.memory_edit.temp_memory_max_chars`（6000） | `memory/editor/service.py` `_validate_state_file`：寫入後驗證，不合就整檔還原，回 `temp_memory_structure_invalid` / `temp_memory_too_large` |
| 整份改寫（`overwrite` / `replace_block`），禁止 `append_entry` | `kernel/agents/memory_editor/prompts/system.md` Rule 7；planner 現在拿到全文 |
| turn 內 rollback 還原完整內容 | `agent/turn_runtime.py` `_TurnMemorySnapshot` 對所有檔案存完整 bytes |
| brain 何時寫、寫什麼、不寫什麼 | `kernel/agents/brain/prompts/system.md`「每輪檢查」與「temp-memory.md 寫入規範」 |

### 狀態債（取代每 N 回合強制 rollup）

`AgentCore._turns_since_memory_sync` 的語意改為「沒有寫現況檔的**用戶**回合數」（`_track_state_file_debt`）。heartbeat、scheduled、task due、worker 回報等內部回合不計，因為以前就是把它們算進去才逼出 `SCHEDULED silent`、`worker SUCCESS` 這類條目。

債只在三個點清償，都走原本的 side-channel（`_run_memory_sync_side_channel`），提醒文改為 `[MEMORY SYNC - STATE REFRESH]`（`turn_runtime._build_memory_sync_reminder`）：

| 時機 | 位置 |
|------|------|
| soft limit 觸發 compaction 前 | `_maybe_run_pre_compaction_memory_sync` |
| quiet hours 前的 pre-sleep sync | `_handle_pre_sleep_sync` |
| 每日維護開頭 | `_perform_maintenance` |

`tools.memory_sync.every_n_turns` 已移除，`MemorySyncConfig` 只剩 `max_retries`。

### 每日快照（取代 digest）

維護時 `memory/hooks.py` `snapshot_state_file(day=昨天)` 把現況檔整檔寫到 `memory/archive/temp-memory/YYYY-MM-DD.md`（已存在則附加），重建 `index.md`。沒有 digest、不呼叫 LLM。`maintenance.archive`、`curate.digest_*` 與 `digest_day_via_worker` 全部移除。

## recall 子代理

| 元件 | 位置 | 說明 |
|------|------|------|
| `RecallAgent` | `src/lincy/tools/builtin/recall.py` | `recall(question, context_tail, exclude_paths, max_tool_calls) -> str \| None`；私有 `ToolRegistry` + 自己的 capped tool loop（`run_simple_tool_loop` 沒有上限所以不用）；達上限後多出來的呼叫回「limit reached」，再送一次 finalization prompt 拿最終文字；`NONE` 或空字串回 None；結果截到 `max_result_chars` |
| 私有工具 `grep` | 同上 | 不分大小寫 regex；`.md` 回 `path:line: text` 加前後文；`.jsonl` 先用原始行預篩，再 parse `SessionEntry` 渲染成 `[本地時間 channel/sender role] 文字`（`send_message` 顯示 body，tool result 截 200 字）；`count_only` 回每檔命中數；總輸出 6000 字、`max_matches` 上限 200。結果順序：live 檔（`people/`、`agent/`）先、`archive/temp-memory/` 新到舊、其他 archive 新到舊、session 新到舊，所以截斷時先留現行事實 |
| 私有工具 `list_dir` / `read_file` | 同上 | `read_file` 對 `.jsonl` 逐條渲染帶行號；`.md` 走 `create_read_file` |
| 路徑守門 | `_allowed_relative` / `_expand_glob` | glob 必須以 `memory/` 或 `session/brain/` 開頭、不可含 `..`、resolve 後須在 agent_os_dir 內；`memory/` 下只讀 `.md`；session 下只讀 `transcript.jsonl` / `messages.jsonl`。`requests.jsonl`、`responses.jsonl`、`events.jsonl`、`turns.jsonl`、`checkpoints/`、`state/` 一律不可見。session 路徑新到舊排序，截斷時先留最近的 |
| 搜尋文字 | `_searchable_text` | 只用渲染後的文字，**不含 metadata**：否則 `turn_overlay` 裡的 `[Recall]` 會被下一次 recall 搜到，形成迴圈 |
| prompt | `kernel/agents/recall/prompts/system.md` | 英文指令、繁中輸出；語料地圖、先 `count_only` 再縮日期 glob、3-6 次呼叫內收手；輸出 `- [YYYY-MM-DD HH:MM][來源路徑] 事實`、衝突列「不確定」、沒有超出已載入檔案的內容就只回 `NONE` |
| 設定 | `cfgs/agent.yaml` `agents.recall` + `RecallAgentConfig`（`core/schema.py`） | `prefetch`、`prefetch_min_chars`（4）、`prefetch_max_tool_calls`（6）、`max_tool_calls`（12）、`max_result_chars`（1500）；LLM profile `cfgs/llm/kano-proxy/recall.yaml`（model `recall-agent`，30 秒 timeout，0 transient retry） |
| 組裝 | `agent/build.py` + `tool_setup.py` | `agents.recall` enabled 且 prompt 存在才建；`session_debug_label="recall"`；同一個實例給 `AgentCore.recall_agent`（預抓）與 `recall` 工具 |

### 預抓與 `[Recall]`

`AgentCore._add_turn_user_entry` → `_prefetch_recall`：

1. 只對 `user_message` 回合（`_classify_inbound_kind`）、`recall.prefetch` 開啟、訊息長度 ≥ `prefetch_min_chars` 才跑。
2. 呼叫 `recall_agent.recall(user_input, context_tail=最近 3 則 user/assistant 文字各 300 字, exclude_paths=self._loaded_context_paths(), max_tool_calls=prefetch_max_tool_calls)`。
3. 結果（含空字串）存進 `turn_metadata["recall_prefetch"]`：overflow retry 的 `_prepare_retry_turn_attempt` 直接重用，不再搜一次。例外只記 warning，回合照常進行。
4. `turn_overlay.build_turn_overlay_metadata(recall=...)` 把 `[Recall]` 放在 `[Agent Notes]` 之後、common ground 之前。overlay 凍結在該回合的 user entry 上，之後原樣保留，符合 prompt cache 規則。

preempt 併入進行中 turn 的訊息沒有 overlay，所以也沒有預抓；brain 需要時自己呼叫 `recall`。

### `recall` 工具

`RECALL_DEFINITION` / `create_recall_tool`：`question` 必須自包含（人、事、大約日期），子代理看不到對話。沒結果回 `No relevant memory found.`，失敗回 `Error: recall failed: ...`。brain prompt 的分工：`memory_search` 定位記憶檔與規則，`recall` 找「實際發生過什麼」。

## transcript.jsonl 與 debug dump 保留期

- `SessionManager.append_message()` 同一行寫 `messages.jsonl` 與 `transcript.jsonl`；compaction 的 `rewrite_messages()` 只動前者。`_rotate_session` 重放保留歷史時 `transcript=False`。compaction 摘要不進 transcript。
- `cleanup_sessions(..., debug_retention_days=7)`：session 目錄未到 `retention_days` 但超過 7 天者，刪 `requests.jsonl` / `responses.jsonl`（正式機 16GB 的來源）。
- 升級前的舊 session 用 `scripts/backfill_transcripts.py <agent_os_dir> [--dry-run] [--force]` 還原：串流 `requests.jsonl`，只取 `client_label == "brain"`，略過 system、synthetic boot（`read_startup_context` / `read_pinned_context`）、side-channel 提醒、compaction 摘要；用戶訊息去掉 `[時間] [channel, from sender]` 前綴與 overlay 區塊並還原 channel/sender/timestamp；以 (role, 文字, tool ids, tool_call_id) 去重、與 `messages.jsonl` 聯集後依時間排序；request 來源的條目帶 `metadata.backfilled_from`。2026-11-08 在三個 session 的副本上實測：265MB + 476MB + 13MB 的 dump 約 2 秒，產出 621 / 597 / 123 條。

## kano-proxy

group `lincy` 新增 group model `recall-agent`，targets 依序 `claude-code/claude-haiku-5-5` → `ant-official/claude-haiku-5-5` → `antigravity/gemini-3.8-flash-low`。profile 走 `/g/lincy/anthropic`。

## 升級（m0186，0.86.0）

- 複製 brain / memory_editor / recall prompt 與 `memory-maintenance/references/rules.md`。
- live `temp-memory.md`：偵測舊格式（任一行 `- [YYYY-MM-DD`），去掉 `- [digest ...]` 行（原文已在 archive），其餘按日期寫進 `archive/temp-memory/{date}.md`（既有檔附加），重建 index，再寫入新骨架；`## 最後互動` 放一條升級說明指向最新一天的快照。已是新格式就不動。
- `memory/agent/index.md` 只把 temp-memory 那行描述換成「今日現況檔（最後互動、時間線、未閉環、下一步）」，其餘 35 條自訂內容不碰。
- workspace `agent.yaml` / `cfgs/agent.yaml` 若存在：刪 `every_n_turns`、`maintenance.archive`、`digest_*`；補 `temp_memory_max_chars`、`debug_retention_days`、`agents.recall`、bm25 exclude `memory/archive/curation/`。

正式機上線順序：kano-proxy 加 `recall-agent` → PR 合併 → `ssh lincy` 跑回填腳本（先 `--dry-run`）→ `lincy upgrade` → 看 `logs/lincy.log` 的 migration 摘要與第一輪 `recall-prefetch` debug。

## 實測（2026-11-08，`ant-official/claude-haiku-5-5`，語料為正式機 memory/ 副本 + 三個 session 的回填 transcript）

12 題已知答案、預抓模式（6 次工具呼叫上限）：

| effort | 命中 | 平均耗時 | 用滿 6 次的題數 |
|--------|------|----------|-----------------|
| medium（採用） | 11/12 | 10.6 秒（5 到 14 秒） | 7 |
| low | 10/12 | 9.0 秒 | 7 |

- 每次 LLM 往返約 2 秒，最後一輪產出答案約 4 到 5 秒；工具本身每次 0.1 秒以下。耗時幾乎全是往返次數，Haiku 多半一輪只發一個工具呼叫。
- 漏的一題（11/5 午餐）在 37KB 的日快照裡，常見字 `元` 的命中超過 `max_matches`，答案被截掉；模型沒有照提示縮小 pattern。
- 修正前（archive 副本排在 live 檔前面）6 題只中 4 題、1 題 NONE、1 題引用過期地址；排序改成 live 優先後這兩題都正確。
- 成本每題約 0.005 美元（舊 harness 實測，新 prompt 的輸入量相近）。

## 取捨與限制

- 每則用戶訊息多一次 Haiku 子代理呼叫，目前平均約 10 秒，疊在 brain 本身的回應時間上。可調：`prefetch_max_tool_calls`（降到 4 約省 2 到 4 秒、漏題會增加）、`output_config.effort: low`（省約 1.5 秒、12 題少中 1 題）、關 `recall.prefetch` 退回純工具。
- `[Recall]` 是子代理的搜尋結果，可信度中等；prompt 要求 brain 對關鍵事實先用 `recall` 或 `read_file` 確認。
- grep 是 Python regex 掃檔，15MB 語料約 0.03 秒；ripgrep / `find` 暫不引入，日期 glob 已覆蓋 find 的用途。
- curation 的「對照 transcript 驗證再刪」（dreaming）與退役 `memory_search` 不在本次範圍。
