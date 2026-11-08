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
| 整份改寫：只接受 `overwrite` / `replace_block` / `create_if_missing`，其他 operation（`append_entry`、`delete_file`、checkbox 類）回 `temp_memory_operation_invalid` | prompt 端 `kernel/agents/memory_editor/prompts/system.md` Rule 7；service 端 `_check_state_file_operations` 在套用前擋下；planner 現在拿到全文 |
| 跨日後第一次改寫前，先把前一天的最後版本快照到 `archive/temp-memory/<該日>.md` | `service.py` `_snapshot_state_file_before_rewrite`：以檔案 mtime 的本地日期判斷，早於今天才快照；補上 00:00 到 03:00 之間改寫造成的空窗 |
| turn 內 rollback 還原完整內容 | `agent/turn_runtime.py` `_TurnMemorySnapshot` 對所有檔案存完整 bytes |
| brain 何時寫、寫什麼、不寫什麼 | `kernel/agents/brain/prompts/system.md`「每輪檢查」與「temp-memory.md 寫入規範」 |

### 狀態債（取代每 N 回合強制 rollup）

`AgentCore._turns_since_memory_sync` 的語意改為「沒有寫現況檔的**用戶**回合數」（`_track_state_file_debt`）。`_classify_inbound_kind` 把 `system` 頻道的 heartbeat / scheduled / task due，以及 `worker`、`shell_task` 頻道的子代理回報都歸為內部回合，不計債；以前就是把它們算進去才逼出 `SCHEDULED silent`、`worker SUCCESS` 這類條目。

債在五個點清償，都走原本的 side-channel（`_run_memory_sync_side_channel`），提醒文改為 `[MEMORY SYNC - STATE REFRESH]`（`turn_runtime._build_memory_sync_reminder`，標題與四段名稱取自 `memory/editor/service.py` 的 `STATE_FILE_TITLE` / `STATE_FILE_SECTIONS`）：

| 時機 | 位置 |
|------|------|
| soft limit 觸發 compaction 前 | `_maybe_run_pre_compaction_memory_sync` |
| quiet hours 前的 pre-sleep sync | `_handle_pre_sleep_sync` |
| 每日維護開頭 | `_perform_maintenance` |
| 結束或重啟 | `graceful_exit`（計數只在記憶體，不先清就會隨重啟消失） |
| 開新 session（維護的 context refresh、手動 new session） | `_perform_new_session`，在清空對話前 |

side-channel 回傳 bool：只有 memory_edit 真的執行且沒出錯才算清償（`_settle_state_file_debt` 歸零）；模型沒呼叫工具、寫入失敗或丟例外都把債留到下一個點，不會靜默歸零。

`tools.memory_sync.every_n_turns` 已移除，`MemorySyncConfig` 只剩 `max_retries`。

### 每日快照（取代 digest）

維護時 `memory/hooks.py` `snapshot_state_file(day=昨天)` 把現況檔整檔寫到 `memory/archive/temp-memory/YYYY-MM-DD.md`（已存在則附加），重建 `index.md`。沒有 digest、不呼叫 LLM。`maintenance.archive`、`curate.digest_*` 與 `digest_day_via_worker` 全部移除。

## recall 子代理

| 元件 | 位置 | 說明 |
|------|------|------|
| `RecallAgent` | `src/lincy/tools/builtin/recall.py` | `recall(question, context_tail, exclude_paths, max_tool_calls, deadline_seconds) -> str \| None`；私有 `ToolRegistry` + 自己的 capped tool loop（`run_simple_tool_loop` 沒有上限所以不用）；達上限後多出來的呼叫回「limit reached」，再送一次 finalization prompt 拿最終文字；超過 `deadline_seconds` 時改送「time budget reached」要模型直接作答（`RecallRun.timed_out`）；assistant 回合連同 `reasoning_content` / `reasoning_details` / `served_by` 一起重放，與 brain 的 side-channel 相同；`NONE` 或空字串回 None；結果截到 `max_result_chars`；request 內寫明工具呼叫與字數預算 |
| 私有工具 `grep` | 同上 | 不分大小寫 regex；`.md` 回 `path:line: text` 加前後文；`.jsonl` 逐行 parse `SessionEntry` 渲染成 `[YYYY-MM-DD (Ddd) HH:MM channel/sender role] 文字`（`send_message` 顯示 body，tool result 截 200 字），regex 只跑在渲染後的整行上，所以 pattern 可以同時限定日期、sender 與內文；`count_only` 回每檔命中數；總輸出 6000 字、`max_matches` 上限 200。結果順序：live 檔（`people/`、`agent/`）先、`archive/temp-memory/` 新到舊、其他 archive 新到舊、session 新到舊，所以截斷時先留現行事實 |
| 私有工具 `list_dir` / `read_file` | 同上 | `read_file` 對 `.jsonl` 逐條渲染帶行號；`.md` 走 `create_read_file` |
| 路徑守門 | `_allowed_relative` / `_expand_glob` | glob 必須以 `memory/` 或 `session/brain/` 開頭、不可含 `..`、resolve 後須在 agent_os_dir 內；`memory/` 下只讀 `.md`；session 下只讀 `transcript.jsonl` / `messages.jsonl`。`requests.jsonl`、`responses.jsonl`、`events.jsonl`、`turns.jsonl`、`checkpoints/`、`state/` 一律不可見。session 路徑新到舊排序，截斷時先留最近的 |
| 搜尋文字 | `_grep_transcript` | 只搜渲染後的文字，**不含 metadata 與 JSON 欄位名**：否則 `turn_overlay` 裡的 `[Recall]` 會被下一次 recall 搜到，形成迴圈 |
| prompt | `kernel/agents/recall/prompts/system.md` | 英文指令、繁中輸出；語料地圖、先 `count_only` 再縮日期 glob、3-6 次呼叫內收手；輸出 `- [YYYY-MM-DD HH:MM][來源路徑] 事實`、衝突列「不確定」、沒有超出已載入檔案的內容就只回 `NONE` |
| 設定 | `cfgs/agent.yaml` `agents.recall` + `RecallAgentConfig`（`core/schema.py`） | `prefetch`、`prefetch_min_chars`（4）、`prefetch_max_tool_calls`（6）、`prefetch_deadline_seconds`（20）、`max_tool_calls`（12）、`max_result_chars`（1500）；LLM profile `cfgs/llm/kano-proxy/recall.yaml`（model `recall-agent`，30 秒 timeout，0 transient retry） |
| 組裝 | `agent/build.py` + `tool_setup.py` | `agents.recall` enabled 且 prompt 存在才建；`session_debug_label="recall"`；同一個實例給 `AgentCore.recall_agent`（預抓）與 `recall` 工具 |

### 預抓與 `[Recall]`

`AgentCore._add_turn_user_entry` → `_prefetch_recall`：

1. 只對 `user_message` 回合（`_classify_inbound_kind`；heartbeat / scheduled / task due 與 `worker`、`shell_task` 回報都不是）、`recall.prefetch` 開啟、訊息長度 ≥ `prefetch_min_chars` 才跑。
2. 呼叫 `recall_agent.recall(user_input, context_tail=最近 3 則 user/assistant 文字各 300 字, exclude_paths=self._loaded_context_paths(), max_tool_calls=prefetch_max_tool_calls, deadline_seconds=prefetch_deadline_seconds)`。deadline 到了之後下一輪只准作答，所以預抓最長約 20 秒加最後一輪的作答時間，不會卡住回合。
3. 結果（含空字串）存進 `turn_metadata["recall_prefetch"]`：overflow retry 的 `_prepare_retry_turn_attempt` 直接重用，不再搜一次。例外只記 warning，回合照常進行。
4. `turn_overlay.build_turn_overlay_metadata(recall=...)` 把 `[Recall]` 放在 `[Agent Notes]` 之後、common ground 之前。overlay 凍結在該回合的 user entry 上，之後原樣保留，符合 prompt cache 規則。

preempt 併入進行中 turn 的訊息沒有 overlay，所以也沒有預抓；brain 需要時自己呼叫 `recall`。

### `recall` 工具

`RECALL_DEFINITION` / `create_recall_tool`：`question` 必須自包含（人、事、大約日期），子代理看不到對話。沒結果回 `No relevant memory found.`，失敗回 `Error: recall failed: ...`。brain prompt 的分工：`memory_search` 定位記憶檔與規則，`recall` 找「實際發生過什麼」。

## transcript.jsonl 與 debug dump 保留期

- `SessionManager.append_message()` 同一行寫 `messages.jsonl` 與 `transcript.jsonl`；compaction 的 `rewrite_messages()` 只動前者。`_rotate_session` 重放保留歷史時 `transcript=False`。compaction 摘要不進 transcript。
- overflow retry 會把同一則用戶訊息重新加進對話（overlay 重算）：`run_turn` 一開始就固定 inbound 時間戳，`append_message` 以 (channel, sender, message) 比對最近 16 筆 transcript，相同就不重寫。換 session 時清空比對清單。
- `cleanup_sessions(..., debug_retention_days=7)`：session 目錄未到 `retention_days` 但超過 7 天者，只刪 `requests.jsonl`（正式機 16GB 的來源）；`responses.jsonl` 是 dashboard 用量圖的資料來源，留到 session 到期。
- 升級前的舊 session 用 `scripts/backfill_transcripts.py <agent_os_dir> [--dry-run] [--force]` 還原：串流 `requests.jsonl`，只取 `client_label == "brain"`，略過 system、synthetic boot（`read_startup_context` / `read_pinned_context`）、side-channel 提醒、compaction 摘要；用戶與 assistant 文字去掉 `[時間]` 前綴，用戶訊息再去掉 `[channel, from sender]` 與 overlay 區塊並還原 channel/sender/timestamp；tool call / result 以 id 去重，純文字以 (role, 文字, 分鐘) 去重，所以同一句話在不同時間說兩次會保留兩條；tool 條目沒有自己的時間戳，取同一筆 request 內前一則有時間戳的訊息；與 `messages.jsonl` 重疊的條目以後者為準（有精確時間與 channel），最後依分鐘穩定排序。request 來源的條目帶 `metadata.backfilled_from`。2026-11-08 在三個 session 的副本上實測：265MB + 476MB + 13MB 的 dump 約 2 秒，產出 621 / 597 / 123 條。

## kano-proxy

group `lincy` 新增 group model `recall-agent`，targets 依序 `claude-code/claude-haiku-5-5` → `ant-official/claude-haiku-5-5` → `antigravity/gemini-3.8-flash-low`。profile 走 `/g/lincy/anthropic`。

## 升級（m0186，0.86.0）

- 複製 brain / memory_editor / recall prompt 與 `memory-maintenance/references/rules.md`。
- live `temp-memory.md`：不是新格式就轉檔。去掉 `- [digest ...]` 行（原文已在 archive），行首 `- [YYYY-MM-DD` 開頭的條目（含續行）按日期寫進 `archive/temp-memory/{date}.md`（既有檔附加），日期只認行首，條目內文提到的日期不會另開一天；沒有日期的備註行歸到 `<今天>.md` 的 `## Undated notes (migrated)`，舊模板的兩行說明文字略過。重建 index，再寫入新骨架；`## 最後互動` 放一條升級說明指向最新一個有日期的快照。
- `memory/agent/index.md` 只把 temp-memory 那行描述換成「今日現況檔（最後互動、時間線、未閉環、下一步）」，其餘 35 條自訂內容不碰。
- workspace `agent.yaml` / `cfgs/agent.yaml` 若存在：刪 `every_n_turns`、`maintenance.archive`、`digest_*`；補 `temp_memory_max_chars`、`debug_retention_days`（取 7 與 `retention_days` 的較小值，schema 不允許超過）、`agents.recall`（含 `prefetch_deadline_seconds`）、bm25 exclude `memory/archive/curation/`。

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

- 每則用戶訊息多一次 Haiku 子代理呼叫，目前平均約 10 秒，疊在 brain 本身的回應時間上；`prefetch_deadline_seconds`（20）是上限，超過就逼模型用手上的結果作答。可調：`prefetch_max_tool_calls`（降到 4 約省 2 到 4 秒、漏題會增加）、`output_config.effort: low`（省約 1.5 秒、12 題少中 1 題）、關 `recall.prefetch` 退回純工具。
- `[Recall]` 是子代理的搜尋結果，可信度中等；prompt 要求 brain 對關鍵事實先用 `recall` 或 `read_file` 確認。
- grep 是 Python regex 掃檔，15MB 語料約 0.03 秒；ripgrep / `find` 暫不引入，日期 glob 已覆蓋 find 的用途。
- curation 的「對照 transcript 驗證再刪」（dreaming）與退役 `memory_search` 不在本次範圍。
