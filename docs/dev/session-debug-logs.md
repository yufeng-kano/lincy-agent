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
  - 另含 `served_provider` / `served_model` / `served_candidate_index`：**真正回應這一筆的 failover candidate**（見「Failover 實際服務者」）
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

## Failover 實際服務者

`provider` / `model` 記的是**設定上的主 profile**（`agents.<name>.llm`），不是實際回應者。主 provider 進入 failover cooldown 時，請求會靜默走 `llm_fallbacks`，只看這兩個欄位會把 fallback 的流量誤判成主 provider 的流量。

因此 `responses.jsonl` 與 `events.jsonl` 的 `llm_response` / `llm_error` 另外記：

| 欄位 | 說明 |
|------|------|
| `served_provider` | 實際回應（或丟出錯誤）的 candidate provider |
| `served_model` | 該 candidate 的 model |
| `served_candidate_index` | 在**設定順序**中的位置，`0` 為主 profile、`>0` 為 fallback（不是嘗試順序：cooldown 會改變嘗試順序，但這裡永遠是設定位置） |

- `provider` / `model` 語意不變，既有消費者（pricing、cache 可測量性判斷）照舊
- 串接方式：`FailoverLLMClient` 在勝出（或丟出錯誤）時把 candidate 寫進 `lincy/llm/failover.py` 的 ContextVar，`DebugLoggingLLMClient` 用 `observe_served_candidate()` 包住呼叫再讀回來——debug wrapper 在 failover client 之外，回傳值裡拿不到這個資訊
- **缺值 = 未知**：舊 session 檔案沒有這些欄位，單一 candidate（沒有 `llm_fallbacks`）也不會產生 failover wrapper，兩者都寫 `None`，讀取端一律當「未知／假定為主 profile」處理，不可自行補值
- `requests.jsonl` 沒有這些欄位：request 在呼叫前就落地，當下還不知道誰會接手

## 邊界

- `requests.jsonl` / `responses.jsonl` 記的是**本專案 normalized LLM 介面**，不是 provider HTTP payload dump
- 目前 resume 仍以 `messages.jsonl` 為主；`checkpoints/latest.json` 先作為 debug 與之後遷移用
- 硬中斷（kill / crash）可能讓 `messages.jsonl` 留下沒有 tool result 的 assistant tool call；resume 載入與每個 turn 開始時會呼叫 `Conversation.remove_dangling_tool_calls()` 直接移除這類記錄（含反向的孤兒 tool result），並 `rewrite_messages` 回寫磁碟——否則 provider API 會拒收這種歷史。修復後 render cache 比對不上會自動作廢，屬預期行為
- retry / failover 若發生在 client wrapper 內部，目前只保留最外層 request/response 或 error，不逐次展開每個底層 transport attempt

## 目前掛點

- brain client
- memory sync client
- skill check client
- compactor client（對話 compaction 第 2 層摘要子代理，`client_label="compactor"`；見 `memory-curation.md` 的「對話 compaction」一節）
- worker subagent（`WorkerRunner` 內部用 `client_label` 標記每次呼叫，例如
  `worker-3`、`maintenance-digest`、`maintenance-curation`；maintenance
  驅動的記憶檔案治理沒有專屬 client，直接沿用同一個 worker runner，因此
  也落在這裡，不再是「零次請求」看起來像沒執行）

因此一個 turn 內若有 stage2 planning、tool loop、memory sync side-channel，都會落到同一個 session 目錄裡，並共用同一個 turn id。maintenance 的 worker 派工不屬於任何使用者 turn，但沿用同一個 session sink，一樣可在 `requests.jsonl` / `responses.jsonl` 用 `client_label` 過濾查找。
