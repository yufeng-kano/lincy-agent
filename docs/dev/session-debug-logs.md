# Session Debug Logs

本文件說明 brain session 目錄下新增的 debug-first 診斷檔案。

## 目標

- 保留既有 `messages.jsonl` transcript resume 路徑，不一次重寫 session 系統
- 讓日常 debug 可以直接回答：
  - 這一輪模型到底看到了什麼 prompt？
  - 為什麼 agent 這樣回答？
  - 這輪 cache read / write token 是多少？
  - 這輪是第幾個 LLM round、用了哪些 tool？

## 檔案

每個 session 目錄現在除了既有的 `meta.json`、`messages.jsonl` 外，還會新增：

- `events.jsonl`
  - 小型時間軸索引
  - 目前包含 `turn_start`、`llm_request`、`llm_response`、`llm_error`、`compaction`、`turn_end`、`checkpoint`
- `requests.jsonl`
  - 寫入 normalized LLM client interface 的完整 request
  - 包含 `messages`、`tools`、`temperature`、`response_schema`
- `responses.jsonl`
  - 寫入 normalized response
  - 對 `chat_with_tools` 保留完整 `LLMResponse`
  - 對 plain `chat` 保留 `response_text`
  - 若 request 丟出 exception，會寫 `error`
- `turns.jsonl`
  - 每個 turn 一行摘要
  - 方便直接看最近 20 輪
  - 包含 inbound kind、input、final content、llm rounds、cache read/write、tool names
  - 若本輪有 compact，另外會記 `compaction_source`、`compaction_trigger`、`compacted_messages_removed`、`compaction_fallback`
- `checkpoints/latest.json`
  - 目前 conversation 的完整 snapshot
- `checkpoints/render_cache.jsonl`
  - 保存已渲染的 conversation prefix，讓 resume 後 prompt cache 前綴較穩定
  - resume 時會比對目前 `messages.jsonl` 的對應訊息；若角色、tool call id/name/arguments、tool result id/name 或原文內容對不上，會丟棄這份 render cache，避免舊 cache 污染下一輪 prompt

## 邊界

- `requests.jsonl` / `responses.jsonl` 記的是**本專案 normalized LLM 介面**，不是 provider HTTP payload dump
- 目前 resume 仍以 `messages.jsonl` 為主；`checkpoints/latest.json` 先作為 debug 與之後遷移用
- 硬中斷（kill / crash）可能讓 `messages.jsonl` 留下沒有 tool result 的 assistant tool call；resume 載入與每個 turn 開始時會呼叫 `Conversation.remove_dangling_tool_calls()` 直接移除這類記錄（含反向的孤兒 tool result），並 `rewrite_messages` 回寫磁碟——否則 provider API 會拒收這種歷史。修復後 render cache 比對不上會自動作廢，屬預期行為
- retry / failover 若發生在 client wrapper 內部，目前只保留最外層 request/response 或 error，不逐次展開每個底層 transport attempt

## 目前掛點

- brain client
- memory sync client
- skill check client

因此一個 turn 內若有 stage2 planning、tool loop、memory sync side-channel，都會落到同一個 session 目錄裡，並共用同一個 turn id。
