# Token-only 上下文策略

本文件定義目前 runtime 的 token 管理策略。  
目標是以 provider 回傳的 usage 作為唯一真實來源，避免字元估算造成誤差。

## 核心原則

1. 不做送出前 tokenizer 預估
2. 不做額外 token count API 呼叫
3. 只用 non-streaming 回應中的 usage 欄位
4. soft limit 是回合後處理，不阻擋當前回合完成

## Prompt Cache 不可破壞的前綴

- `ContextBuilder` 的 BP1 / BP2 屬於 system-tier cache prefix：system prompt + boot files
- 任何 per-turn reminder / hint / overlay 都**不可**改寫 BP1 / BP2，也**不可**額外插入新的 system message 來做近端提醒
- 任何會隨本輪變動的插入資訊（例如 `current_local_time`、`[Timing Notice]`、message-time common ground）都必須待在 **latest turn**
- 原因：
  - 會破壞快取前綴穩定性，降低 cache hit
  - Anthropic / Gemini adapter 都只保留最後一個 system 欄位，多個 system message 可能互相覆蓋
- 若需要本輪近端提醒，只能放在 conversation tier；做法是附加在**最新 user message**
- 目前落地方式：`[Runtime Context]` / `[Timing Notice]` / `[Decision Reminder]` / `[Agent Notes]` 由 `agent/turn_overlay.py` 組字串，`agent/responder.py:_build_dynamic_turn_overlay()` 包成 overlay，接在 common-ground overlay 之前，一起疊加到 latest user message（`core.py:_prepare_turn_attempt` 每個 turn attempt 只組裝、快照一次）。這些 block **只存在於送給 LLM 的 request**，不寫回 `Conversation`，也不進 `ContextBuilder` 的 render cache——`ContextBuilder` 本身不再依賴 `NoteStore` 或這幾個 block 的組字邏輯，所以歷史訊息不會凍結到任何一份 block 的舊副本（詳見 `docs/dev/agent-task-system.md` 的 Agent Note System 一節）

## Prompt Cache 操作目標

- 對啟用 `cache_ttl: 1h` 的正常 brain request，當同一輪 prompt 在 tool loop / rebuild 中重送時，prompt cache hit 應維持在 **90% 以上**
- 若實測長 prompt 常落到明顯低於 90%（例如 0% / 14% / 50%），視為 runtime regression，不是可接受噪音
- conversation-tier 的兩個 breakpoint 固定用「上一個 user turn」與「本輪最新 user turn」；不要在跨 turn 時直接跳到上一輪 final assistant，否則上一輪長工具回合沒有既有 cache endpoint，下一輪第一個 request 會大幅 miss
- 例外只限於：
  - 第一次冷 cache
  - 本輪最新 user turn 本身被刻意改寫
  - boot files / system prompt / tool schema 確實發生變更

## NG 行為

- 在 `ContextBuilder.build()` 內直接讀 `tz_now()` 或其他 wall-clock 值來組 prompt
- 把 per-turn dynamic note 做成獨立 `system` message
- 把 dynamic overlay 插在 latest turn 之前（例如 pre-latest-user synthetic pair）
- 在同一輪重建 prompt 時重新計算會變的提示，而不是使用 turn-start metadata snapshot
- 讓不同路徑各自決定哪些 dynamic note 可以進 cache prefix，缺乏單一規則

## 軟上限（soft limit）

- 設定欄位：`context.soft_max_prompt_tokens`
- 設計定位：把它當成**保守安全值**，不是逼近 provider context window 的調參目標
- 配置原則：數值應保守到足以容納一個完整正常 turn，包含 prompt 成長、tool loop、必要的 synthetic context（如 common ground / skill guide）與最終回覆
- 文件口徑：可把它視為操作上的「絕對安全預算」；也就是說，一旦配置正確，正常 turn 應在此預算內完成，而不是把它拿來賭最後一點窗口
- 判定欄位：brain 回應中的 `prompt_tokens`
- 行為：
  - 若 `prompt_tokens <= soft_max_prompt_tokens`：不動作
  - 若超過：回合結束後才做 compact，影響下一輪
- 注意：runtime 目前仍是**回合後**依 usage 做 compact，不是送出前 hard stop；真正溢出時仍由 `ContextLengthExceededError` fallback 接手
- compact 策略：保留最新 `context.preserve_turns` 輪 user-turn
- **Pre-compaction sync**：compact 前檢查 `_turns_since_memory_sync > 0`，若有累積未同步內容且該輪沒有 memory_edit，先執行一次 memory sync side-channel 再 compact（best-effort，sync 失敗仍會 compact）

## Copilot usage 缺值

Copilot 若沒有回傳 usage，不做估算，也不報錯中止。  
狀態列固定顯示：

`tok unavailable/<soft_limit> (copilot no usage)`

## 硬超限 fallback

若 provider 回傳 `ContextLengthExceededError`：

1. 回滾本輪變更
2. 執行 memory archive + reload boot files
3. 以 `context.preserve_turns` compact
4. 單次 retry
5. retry 仍超限則本輪失敗

## 顯示口徑

狀態列只顯示 brain usage，不加總 memory/vision/gui 子代理。

- 主值仍為 brain prompt token
- 若同一筆 brain usage 有 cache usage，狀態列可附帶 `cache read/write` breakdown
