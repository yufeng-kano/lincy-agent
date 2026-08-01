# Web Dashboard（chat_web_api + chat_web_ui）

監控 dashboard，即時顯示 token 用量、成本、read cache rate，並提供本機遠端 TUI 介面（`/chat` Agent 頁）。

## 架構

```
Browser → uvicorn (:9002) → FastAPI (chat_web_api)
                             ├── /health
                             ├── /api/*        REST endpoints
                             ├── /ws           WebSocket 即時推送
                             └── /*            Vue dist/ 靜態檔 + SPA fallback
```

資料流：JSONL append → watchfiles 偵測 → incremental read → cache 更新 → WebSocket push → Vue reactive 更新

送訊資料流（遠端 TUI）：

```
Browser（Agent 頁 composer，選定 send channel，預設 cli）
        → chat_web_api /api/chat/messages {content, channel}
        → chat-cli control API → AgentCore queue（該 channel 的 inbound）
        → runtime UI event → agent 活動流 → /ws agent_event → 前端時間軸
```

送出回應只是 `{"status": "accepted", "channel": "..."}`（HTTP 202），**不回傳訊息物件**；使用者送出的內容要等 `inbound_message` 事件從活動流回來才會出現在畫面上。

Agent 活動流資料流（見「Agent 活動流」章節）：

```
runtime UI event → FanoutUiSink（cli/app.py 組裝層）
        → state/ui_events/events.jsonl（每次啟動輪替）
        → watch_ui_events → /ws agent_event + GET /api/agent/events
```

## 後端 (`src/chat_web_api/`)

| 檔案 | 職責 |
|------|------|
| `settings.py` | 從 `cfgs/agent.yaml` 讀取 `agent_os_dir`、`soft_max_prompt_tokens` |
| `pricing.py` | 從 LiteLLM GitHub JSON 抓取 model pricing，本地 cache 24h |
| `session_reader.py` | 增量 JSONL 讀取器（byte offset seek，只讀新行） |
| `cache.py` | In-memory metrics cache：sessions、turns、responses 聚合 |
| `watcher.py` | `watchfiles.awatch()` 監控 session 目錄、Web Chat 事件檔、Agent 活動事件檔 |
| `app.py` | FastAPI factory：REST + WebSocket + 靜態檔 serving |

Web Chat 事件模型與 JSONL store 位於 `src/lincy/agent/web_chat.py`，adapter 位於 `src/lincy/agent/adapters/web.py`。事件檔固定在 `agent_os_dir/state/web_chat/events.jsonl`。

Agent 活動事件模型與 JSONL store 位於 `src/lincy/agent/ui_event_stream.py`，事件檔固定在 `agent_os_dir/state/ui_events/events.jsonl`。

### API Endpoints

| Method | Path | 說明 |
|--------|------|------|
| GET | `/api/dashboard?from=&to=` | 總覽：cost、turns、read cache rate、daily 聚合 |
| GET | `/api/sessions?from=&to=&limit=&offset=` | Session 列表 |
| GET | `/api/sessions/{id}` | Session 細節：turns + per-request breakdown |
| GET | `/api/requests?from=&to=&limit=&offset=` | 跨 session 的全域 request log |

### 日期篩選語意

`from` / `to` 以 **turn 開始時間** 與 **response 時間戳** 判斷是否落在區間內，**不是** session `created_at`：

- 跨午夜仍在跑的 session（例如 7/10 建立、7/11 還在用 Grok）在「今天 / 7 天」會出現
- `/api/requests` 依 response `ts` 過濾，並 **最新優先**（前端首頁 limit 500 才看得到當前 model）
- dashboard 的 daily cost / token 也依 response / turn 當日聚合
| GET | `/api/live` | 當前 active session 的 token 位置 |
| GET | `/api/context/composition` | 即時分析最新一筆 brain request 的 prompt 組成（segments + token 估計），每次請求都重新解析 `requests.jsonl`、不進快取；session/brain request 不存在時回 `available: false`，見「Context 頁」 |
| GET | `/api/claude-accounts` | 轉發 claude-code-proxy `/usage`：帳號、5h/週用量、model list；proxy 不可用時回 `available: false` |
| POST | `/api/claude-accounts/login` | 轉發 proxy `POST /login`：開始 browser OAuth，回 `login_id` + `authorization_url` |
| POST | `/api/claude-accounts/login/{login_id}/complete` | 轉發 proxy 完成登入：body `{"code": "code#state"}`，token 寫入 proxy store |
| POST | `/api/claude-accounts/{token_id}/promote` | 轉發 proxy `POST /tokens/{id}/promote`：設為最高優先 |
| DELETE | `/api/claude-accounts/{token_id}` | 轉發 proxy `DELETE /tokens/{id}`：移除 token |
| GET | `/api/codex-accounts` | 轉發 codex-proxy `/usage`：帳號、usage windows；proxy 不可用時回 `available: false` |
| POST | `/api/codex-accounts/login` | 轉發 proxy 開始 browser OAuth，回 `login_id` + `authorization_url`（可能附 `listener_error`） |
| GET | `/api/codex-accounts/login/{login_id}` | 輪詢登入狀態，回 `status`：`pending` / `completed` / `expired`（前端每 2 秒輪詢一次） |
| POST | `/api/codex-accounts/login/{login_id}/complete` | 完成登入：body `{"value": "<callback URL 或 code#state>"}`，token 寫入 proxy store |
| POST | `/api/codex-accounts/{token_id}/promote` | 設為最高優先 |
| DELETE | `/api/codex-accounts/{token_id}` | 移除 token |
| GET | `/api/chat/events?limit=` | Web Chat 最近事件（舊介面遺留；Agent 頁已不使用） |
| GET | `/api/chat/channels` | 可選的送出 channel 清單（轉發 control API），回 `{"channels": ["cli", "discord", ...]}`；**永遠不含 `web` / `system`** |
| POST | `/api/chat/messages` | 轉送訊息到 chat-cli control API，body `{"content": "...", "channel": "cli"}`（`channel` 預設 `cli`）；成功回 202 `{"status": "accepted", "channel": "..."}`，正在處理上一輪時回 409 |
| GET | `/api/agent/events?limit=` | Agent 活動事件（預設 500，範圍 1..2000；只有當次 chat-cli 執行的資料） |
| WS | `/ws` | 即時推送：`session_updated`、`live_token_update`、`session_created` |

WebSocket 另會推送 `chat_event` 與 `agent_event`：

```json
{"type": "chat_event", "event": {"id": "...", "kind": "message", "role": "assistant"}}
{"type": "agent_event", "event": {"id": "...", "seq": 42, "type": "tool_call", "agent": "worker-3"}}
```

### Token 計費邏輯

Anthropic provider 的 `prompt_tokens` 已包含 cache tokens（見 `src/lincy/llm/providers/anthropic.py:241`）：

```
prompt_tokens = base_input + cache_read + cache_write
base_input = prompt_tokens - cache_read_tokens - cache_write_tokens
cost = base_input × input_rate + cache_read × cr_rate + cache_write × cw_rate + completion × output_rate
```

Pricing 來源：`https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json`

本專案可在 `src/chat_web_api/pricing.py` 維護本地 override，處理 LiteLLM 尚未更新或價格不符合本專案口徑的模型。DeepSeek V4 目前使用官方原價計算，不使用 DeepSeek 官網列出的 75% 折扣價；override 會帶 `pricing_source=local_override`、`pricing_source_url` 與 stale 狀態，前端會在 Total Cost 與 request breakdown 顯示。

### 增量讀取

JSONL 是 append-only，每個檔案追蹤 `byte_offset`：
- `seek(offset)` → 讀到 EOF → 更新 offset
- 不重讀舊資料，新 session 出現時建立新 entry

## Agent 活動流

把 chat-cli 的 typed UI event（tool call/result、assistant text、inbound/outbound、warning/error 等）鏡射到 Web，讓 `/chat` 頁能像 Claude Code 一樣看 brain 主時間軸與各子代理分頁。

### 架構

- 組裝層（`src/lincy/cli/app.py` 的 `main()`）保留原本的 `QueueUiSink` 給 Textual app（它需要 `drain()` / `set_on_emit()`），其餘所有元件改吃 `FanoutUiSink((ui_sink, UiEventExportSink(store)))`
- 只有 `config.channels.web.enabled` 時才掛 export sink；關掉時 `sink_for_agents` 就是原本的 `ui_sink`
- fanout 在 workspace 檢查與 resume replay 之前就建立，所以 `console.print_resume_history()` 重播的歷史事件也會進 export
- `UiEventExportSink.emit` 全程包 try/except：export 失敗只 warn 一次就靜默，不得影響 TUI 或 agent；`FanoutUiSink` 也逐個 sink 隔離例外
- **`src/lincy/tui/` 與 `src/lincy/agent/ui_event_console.py` 完全沒動**，TUI 行為與改動前一致
- 事件檔 `agent_os_dir/state/ui_events/events.jsonl` 每次 chat-cli 啟動先 `rotate_on_start()`（舊檔改名 `events.prev.jsonl`，覆蓋更舊的），`seq` 每次執行從 1 重新計數；因此 Web 上只看得到「當次執行」的活動
- 單一字串欄位上限 16000 字元，超過截斷並附 `... [truncated]`，避免單筆 JSONL 爆掉

### Wire schema

```json
{"id": "<uuid4 hex>", "seq": 42, "ts": "<ISO datetime with tz>", "type": "tool_call", "agent": "worker-3", "data": {"name": "execute_shell", "summary": "..."}}
```

`type` 與 `data` 一對一對應 `src/lincy/tui/events.py` 的 dataclass 欄位（`timestamp` 改名為 `ts`）：

| type | data 欄位 |
|------|-----------|
| `inbound_message` | `channel`, `sender`, `content` |
| `processing_started` | `channel`, `sender`, `label` |
| `processing_finished` | `channel`, `sender`, `interrupted` |
| `assistant_text` | `content` |
| `tool_call` | `name`, `summary` |
| `tool_result` | `name`, `summary`, `failed`, `warning` |
| `tool_stream` | `line` |
| `warning` / `error` | `message` |
| `debug` | `label`, `message` |
| `ctx_status` | `text` |
| `resume_history` | `summary` |
| `outbound_message` | `channel`, `recipient`, `content` |
| `interrupt_state` | `phase`, `message` |

### 子代理歸屬規則

`agent` 欄位只在 `tool_call` / `tool_result` 上判定：

- `name` 符合 `^(worker-\d+)\s+(.+)$` → `agent` 取 group 1，`data.name` 去掉前綴
- `name == "gui_task"` → `agent = "gui_task"`
- 其餘事件一律 `agent = null`（brain 主軌）

這是在還原 `UiEventConsole.print_subagent_tool_call` / `print_subagent_tool_result`（`f"{label} {tool_name}"`）與 `print_gui_step`（固定 `gui_task`）的命名摺疊慣例。**改動那兩處命名時必須同步改 `ui_event_stream.py` 的 `_SUBAGENT_NAME_RE`**，否則子代理分頁會失效。

### Agent 頁（遠端 TUI）

`pages/ChatPage.vue` 是 chat-cli TUI 的遠端鏡像，路由仍是 `/chat`，sidebar 名稱為「Agent」。**它不是獨立的「Web Chat」頻道**：composer 只是把訊息「以某個 channel 的身分」丟進 agent queue。時間軸本體是 agent 活動事件（等同 TUI 的 log），但 `inbound_message` / `outbound_message` 以聊天泡泡呈現（右 = 人類、左 = agent），一眼就能分辨誰在說話；其餘事件型別（tool_call/tool_result/assistant_text/warning/error/tool_stream/interrupt/debug）維持置中卡片列的 system-row 樣式，不套用泡泡。

- Header：標題、狀態點、最新 `ctx_status` chip、Debug 開關（預設關；關閉時 `debug`、`processing_started`、`processing_finished`、`resume_history` 都不進時間軸）
- 狀態點來源是 agent 事件而非 Web Chat 事件：最新的 `processing_*` 事件是 `processing_started` → Processing（黑點 pulse），否則 Ready（綠點）；只有送出失敗才顯示 Error（紅點），與 TUI 的 `busy` 判定一致
- Tab bar：`Brain` + 每個子代理一個分頁（worker-N / gui_task），有未完成 tool call 時分頁點會 pulse
- Brain 分頁時間軸只有 `buildAgentRows(brain 軌事件)` 一個來源，不再合併任何 Web Chat 泡泡
- 渲染規則：
  - `tool_call` 一列（mono 工具名 + 單行摘要），可展開看完整摘要與配對到的 result；result 未到前顯示 pulse 點，`failed` 紅、`warning` 琥珀
  - result 配對規則：同一 `(agent, name)` 依 seq FIFO；沒有對應 call 的 result（`tui.show_tool_use` 關閉時只會送 failed/warning result）自成一列
  - `tool_stream` 併入該軌目前開著的 tool_call，否則自成 mono 一列
  - `assistant_text` 為 inner monologue 灰塊，超過 3 行折疊
  - `inbound_message`（人類）與 `outbound_message`（agent）都渲染成聊天泡泡，顯示完整內容（Markdown → HTML，經 DOMPurify 消毒後 `v-html`；不截斷）。tool / debug / monologue 仍維持純文字：
    - `inbound_message` 靠右、深色泡泡（`bg-[#111827] text-white`），代表人類/我方；連結與 code 用可讀的淺色
    - `outbound_message` 靠左、淺色描邊泡泡（`border border-[#E5E7EB] bg-white text-[#111827]`），代表 agent
    - 泡泡上方小字顯示 channel badge（mono）、peer（sender/recipient）、時間（`HH:MM` tabular-nums）；**所有 channel 一視同仁**（含舊資料裡殘留的 `web`）
    - 泡泡寬度上限約 72-86%（`max-w-[86%] sm:max-w-[72%]`），確保左右對齊清楚可辨
    - 元件：`components/agent/MarkdownContent.vue`（`marked` + `isomorphic-dompurify`）
  - `processing_started` / `processing_finished`（turn start/end 分隔線）與 `resume_history`（分隔線，「processing [channel]」的來源）**只在 Debug 開啟時顯示**；Debug 關閉時整段隱藏，不只是視覺淡化
  - `warning` / `error` 為琥珀 / 紅色列；`interrupt_state` 只渲染非 idle 階段；`ctx_status` 只出現在 header chip
  - 除聊天泡泡外，其餘事件型別（`tool_call`/`tool_result`/`assistant_text`/`warning`/`error`/`tool_stream`/`interrupt_state`/`debug`）維持現有卡片/列表樣式（置中或滿版），不套用左右泡泡
- Composer 上方有 live 子代理列（`worker-3 running - execute_shell`），點擊跳到該分頁；沒有 live 時隱藏
- 捲動：使用者在距底部 80px 內才自動貼底，否則顯示「Jump to latest」；切分頁會重置
- 空狀態且 WebSocket 斷線時提示 chat-cli 沒有在跑

Composer（送出 channel 選擇）：

- 左側 mono `<select>` 選送出 channel，清單來自掛載時的 `GET /api/chat/channels`；請求失敗時至少提供 `["cli"]`
- 選擇存在 `localStorage` 的 `lincy.agent.send-channel`；缺值或不在清單內就退回 `cli`
- Enter 送出、Shift+Enter 換行、送出中禁用；成功只清空輸入框，訊息本身要等 `inbound_message` 事件回來才出現
- 送出失敗（含 409「Still processing the previous turn.」）顯示紅色錯誤橫幅，狀態點同時轉為 Error

前端檔案：`stores/agentEvents.ts`（Pinia store：events 依 seq 排序、以 `id` 去重、上限 3000 筆，另輸出 `busy`；同檔另外輸出配對與時間軸組裝的純函式，因為 `src/lib/` 被 `.gitignore` 排除）、`stores/chat.ts`（只負責 composer：channel 清單、選定 channel、送出狀態與錯誤）、`components/agent/AgentTimeline.vue`（渲染）。

## 前端 (`src/chat_web_ui/`)

Tech stack：Vue 3 + Vite + Bun + shadcn-vue + Tailwind CSS + Chart.js

### 頁面結構

| 路由 | 頁面 | 說明 |
|------|------|------|
| `/monitor` | MonitorDashboard | 總覽：summary cards + 圖表 + sessions 表格 |
| `/monitor/requests` | MonitorRequests | 跨 session request log，按 session 分組 |
| `/monitor/context` | MonitorContext | Brain agent 最新一輪 prompt 組成視覺化：donut + sequence bar + files + breakdown table |
| `/monitor/:id` | MonitorSession | 單一 session：turn timeline + expandable responses |
| `/proxy` | ProxyPage | Proxy usage 獨立區塊：Claude / Codex 帳號用量 + 帳號管理（add/promote/remove） |
| `/chat` | ChatPage | 遠端 TUI：Brain 時間軸 + 子代理分頁 + 帶 channel 選擇的 composer |
| `/settings` | SettingsPlaceholder | 預留 |

Overview、Requests、Context 之間用 tab bar 切換（`MonitorTabs.vue`）。

### Context 頁

`/monitor/context` 即時視覺化 brain agent 最新一輪 prompt 的組成，用來檢查 prompt cache 前綴大小與各段落佔比。

- 後端分析模組 `src/chat_web_api/context_composition.py`：純函式，不 import FastAPI；輸入 `sessions_dir` + `soft_max_prompt_tokens`，輸出 JSON-safe dict，由 `GET /api/context/composition` 透過 `run_in_threadpool` 呼叫（見上方 API 表）
- **不進快取、每次請求都重新 parse**：streaming 讀 `requests.jsonl` 找最新一筆 `client_label == "brain"` 的 request（`requests.jsonl` 含完整 message payload，可能 10MB+，依專案慣例不得存進 `cache.py` 或被 `watcher.py` 監控，見「注意事項」）
- Token 數為估計值：ASCII 固定 3.6 chars/token；CJK 比率從該 request 對應 turn 在 `turns.jsonl` 的 `max_prompt_tokens`（provider 回報值）反推校準，並 clamp 在 `[0.5, 3.0]` tok/char 之間；找不到對應 turn 記錄、或反推值超出 clamp 範圍時，退回固定 1.5 tok/char 並標記 `calibrated: false`
- **Segment 拆分直接對應 brain request 送給 LLM 時的實際順序**（system prompt → `[Core Rules]` boot files → `read_startup_context` 工具結果 → `read_pinned_context` 工具結果 → 對話歷史 → 當前輪的 user message + 動態注入區塊 + tool loop）：`context_composition.py` 讀的是 `requests.jsonl`（wire-level payload），不是重新呼叫 `ContextBuilder.build()`。system prompt / boot files / 對話歷史的組裝仍在 `src/lincy/context/builder.py` 的 `ContextBuilder.build()`；當前輪的動態注入區塊（`[Runtime Context]` / `[Timing Notice]` / `[Decision Reminder]` / `[Agent Notes]` / common ground）改由 `agent/turn_overlay.py` + `agent/responder.py` 在 `build()` 之後疊加到 latest user message，但落在 wire 上的位置與順序不變，所以 `_LATEST_TURN_MARKERS` 的 substring 掃描邏輯不受影響：**修改這兩邊任一處的組裝步驟或注入順序時，必須在同一個改動內同步更新 `context_composition.py` 的分類邏輯**，否則兩邊會失準
- 前端元件 `components/dashboard/ContextDonut.vue`（手刻 SVG donut，geometry 純函式留在元件內，不用 Chart.js）+ `stores/contextComposition.ts`（Pinia store 負責抓取/refresh，並輸出 `staticPrefixTokens`/`largestItem`/`cacheBreakpoints`/`segmentColor` 等純函式給頁面與 donut 元件共用；放在 stores 而非 `src/lib/`，理由同下方「注意事項」）
- 六色分類色盤（`stores/contextComposition.ts` 的 `CONTEXT_PALETTE`，**非**灰階 ramp）：依 prompt 順序 tool_definitions `#2a78d6`（藍）、system_prompt `#eb6834`（橘）、boot_core_rules `#1baf7a`（青綠）、boot_tool_files `#eda100`（黃）、pinned_context `#e87ba4`（洋紅）、conversation（history + current_turn 共用同一色）`#008300`（綠）；此色盤已做 CVD 驗證，含 donut 首尾相鄰的綠/藍 wrap-around pair。aqua / yellow / magenta 三色在白底上對比度都低於 3:1（約 2.2-2.8:1），**不能只靠色塊辨識**——donut 的 in-slice 百分比標籤、legend 文字與 breakdown table 都要保留，作為顏色以外的 relief。in-slice 標籤文字顏色也因此改用該色的 relative luminance 動態決定黑 `#111827` 或白（`ContextDonut.vue` 的 `labelColorFor`，threshold 依這六色實測校準為 0.3），不是固定索引規則
- Files bars 與 breakdown table 的色塊都呼叫同一個 `segmentColor()`，顏色不會與 donut 分岔
- Refresh 時機：mount 時、手動按鈕、`session_updated` WebSocket 事件（debounce 2 秒，避免 tool loop 密集觸發時反覆重新解析大檔）

### Proxy 頁（Claude Accounts 卡片）

`components/proxy/ClaudeAccountsCard.vue`（獨立 `/proxy` 頁，sidebar「Proxy」）顯示並管理 claude-code-proxy token pool：

- 每帳號一列：狀態點（active 綠 / standby 灰 / benched 琥珀 / unusable 紅）、email、plan 標籤。狀態文字改為 dot 的 `title` tooltip，不再另外顯示 ACTIVE/STANDBY 等文字；plan 標籤縮短（`rate_limit_tier`/`plan_type` 去掉 `default_`、`claude_` 前綴並 title-case，例：`default_claude_max_5x` → `Max 5x`、`claude_pro` → `Pro`、`default_claude_ai` → `AI`）
- 用量改為對齊的單欄 meter rows（每帳號一個 grid，欄位對齊）：一列一個時間窗，依序 label、bar（<70% 黑、70-90% 琥珀、≥90% 紅）、% 與重置時間，涵蓋 5h、Week，以及 model-scoped weekly（如 Fable）；model-scoped 列來源是 proxy 解析 OAuth usage `limits[]` 中 `kind=weekly_scoped` 的項目（以 `scope.model.display_name` 當 label），在 `/api/claude-accounts` 回應以 `usage.seven_day_scoped` 欄位輸出
- 底部列出 active 帳號可用的 model id（來源 proxy `/v1/models` passthrough），呈現為 mono 文字列表（`·` 分隔）；預設收合，點「Models (N)」disclosure 展開
- 資料來源 `/api/claude-accounts`，3 分鐘輪詢；卡片右上有手動 Refresh 按鈕，帶 `?refresh=true` 繞過 proxy 端 60s snapshot 快取強制重抓
- 前端另用 `sessionStorage`（`lincy.proxy-accounts.claude`）快取上次成功回應：F5 / remount 先立刻畫出舊資料，背景再打 API；同 tab 關閉後清掉
- 帳號用量抓取失敗（如 OAuth endpoint 429）時顯示上次成功資料，錯誤降為灰字 `stale — ...` 註記；完全沒有資料才顯示紅字錯誤

編輯操作（等價於 `proxy claude-code login` / `tokens promote` / `tokens remove`）：

- **Add account**：`POST /api/claude-accounts/login` 取得授權連結，使用者在新分頁授權後把 `code#state` 貼回卡片，`POST .../login/{login_id}/complete` 完成；pending login 狀態存在 proxy 記憶體，15 分鐘過期
- **Promote**：非最高優先帳號顯示 icon 按鈕（`ArrowUp`），設為最高優先
- **Remove**：icon 按鈕（`X`），`window.confirm` 確認後移除 token
- 任何編輯成功後 proxy 會失效 usage snapshot 快取，卡片跟著 `?refresh=true` 重抓
- proxy 端管理端點與 `/usage` 同一道 inbound gate：loopback 直接信任，遠端需 `CLAUDE_CODE_PROXY_API_KEY`

單帳號列的呈現（狀態點、email/id、plan 標籤、promote/remove 按鈕、meter grid、error/stale 行）抽成共用元件 `components/proxy/ProxyAccountRow.vue`，Claude 與 Codex 卡片都用它渲染，只餵不同的 props（rows 陣列、plan 文字、canPromote/canRemove 等），視覺上完全一致。

### Proxy 頁（Codex Accounts 卡片）

`components/proxy/CodexAccountsCard.vue`（`/proxy` 頁，Claude 卡片下方）顯示並管理 codex-proxy token pool，版面與互動邏輯比照 Claude 卡片，皆透過 `ProxyAccountRow` 渲染：

- 用量列直接對應 `/api/codex-accounts` 回應的 `usage.windows[]`：每列 `label`（例：`5h`、`Week`，由 proxy 端 `limit_window_seconds` 推導）、`utilization`、`resets_at`；label 不是 `\d+h` 形式時重置時間帶日期（`MM/DD HH:MM`），否則只顯示 `HH:MM`
- plan 標籤：`account.plan_type` title-case（例：`plus` → `Plus`）；`source === "codex_auth"` 的帳號（讀自官方 Codex CLI 的 `~/.codex/auth.json`，不在本專案 token store 裡）在標籤後綴 ` · codex cli`，並隱藏 promote/remove 按鈕，因為這類帳號無法透過 proxy store API 操作
- `models` 目前固定回空陣列，沿用 Claude 卡片同一顆 `v-if`，故不顯示 Models disclosure
- 資料來源 `/api/codex-accounts`，3 分鐘輪詢；同 Claude 卡片提供手動 Refresh（`?refresh=true`）；同樣用 `sessionStorage`（`lincy.proxy-accounts.codex`）做 F5 hydrate

登入流程與 Claude 卡片的手動貼 `code#state` 不同，改成「自動完成為主、手動貼網址為備援」：

- **Add account**：`POST /api/codex-accounts/login` 取得 `authorization_url` 後在新分頁開啟；ChatGPT 授權完成後會導回 proxy 監聽的 `http://localhost:1455/auth/callback`，proxy 端 listener 自動完成登入（僅當瀏覽器與 proxy 在同一台機器時才連得到 localhost）
- 面板開啟後卡片以 2 秒間隔輪詢 `GET /api/codex-accounts/login/{login_id}`：`completed` 時關閉面板並 `?refresh=true` 重抓；`expired` 時顯示錯誤並清空面板，需重新點 Add account
- 手動 fallback：遠端瀏覽器打不開 callback 頁面時，把網址列上失敗的 `localhost:1455/...` 網址複製貼到卡片輸入框，按 Complete 呼叫 `POST .../login/{login_id}/complete`（body `{"value": "..."}`）
- `beginCodexLogin()` 回應若帶 `listener_error`，卡片顯示提示：自動完成不可用，需改用手動貼網址
- Cancel 按鈕清除輪詢 timer；元件 unmount 時一併清掉，避免背景持續打 API

### 視覺風格

- 背景 `#FFFFFF`，卡片邊框 `1px #E5E7EB`，陰影 `0 1px 2px rgba(0,0,0,0.04)`
- 主色 `#111827`（近黑），次色 `#6B7280`（灰），禁用 `#D1D5DB`
- 數字一律 `tabular-nums`
- **零漸層、零彩色背景**
- 唯一彩色：Live 綠點 `#22C55E`、token bar 警告色（amber `#F59E0B`、red `#EF4444`）
- Context 頁的 donut / sequence bar / files bar 色塊是零彩色規則的唯一受認可例外：那是 segment 的 data encoding（六色分類色盤，見「Context 頁」），不是 UI chrome，不得比照拿來用在其他頁面的裝飾或狀態色

### Token Bar

位於 top bar 右上，顯示當前 active session 的 context 用量：
- 單一 bar：0 → hard limit（model max_input_tokens），soft limit 位置有細線標記
- 顏色：`<70% soft` 灰、`70-85%` 黑、`85-100%` 琥珀、`>100%` 紅
- Hover tooltip 顯示完整數字
- 只在有 active session 時顯示

### 圖表

- **Daily Cost**（bar chart）：每日花費，黑色柱狀
- **Read Cache Rate**（line chart）：
  - Today → per-request 粒度，x 軸 `HH:MM`，標題 "Request Read Cache Rate"
  - 7D/30D/Month → per-day 粒度，x 軸 `MM/DD`，標題 "Daily Read Cache Rate"

### 時間範圍

`TimeRangeSelector.vue` 提供：
- Today / 7D / 30D 快速按鈕
- Month 按鈕：彈出下拉面板，年份左右箭頭 + 3×4 月份網格，未來月份禁用

### 即時更新

- `stores/websocket.ts`：singleton WebSocket，3 秒自動重連
- `stores/live.ts`：active session token 位置，WebSocket `live_token_update` 更新
- `stores/dashboard.ts`：收到 `session_updated` / `session_created` 時自動 refresh
- `stores/chat.ts`：載入 `/api/chat/channels`，送出 `/api/chat/messages`；不再訂閱 `chat_event`
- `stores/agentEvents.ts`：載入 `/api/agent/events`，收到 `agent_event` 時依 `seq` 插入（以 `id` dedupe）

## Supervisor 整合

```yaml
# cfgs/supervisor.yaml
chat-web-ui-build:       # oneshot：bun run build → dist/
  auto_restart: false
  
chat-web-api:            # daemon：uvicorn :9002
  depends_on: [chat-web-ui-build]
  
chat-cli:
  depends_on: [..., chat-web-api]
```

啟動順序：build frontend → start API（health check）→ start chat-cli

### 環境檢查

```bash
chat-supervisor check    # 檢查 bun/uv/node 是否在 PATH、sessions 目錄是否存在、dist/ 是否已 build
```

Supervisor 在子 process 環境自動補充 `~/.local/bin`、`~/.bun/bin`、`/opt/homebrew/bin`、`/usr/local/bin` 等路徑。

手動 `cd src/chat_web_ui && bun run build` 時，PATH 也必須找得到 `node`：`vue-tsc` 的 shebang 是 `#!/usr/bin/env node`。若 `node` 不在 PATH，bun 會改用自己的 runtime 執行 `vue-tsc`，Volar 解析失效，表面上看起來像「找不到所有 `.vue` 檔」（`TS2307`），其實檔案都在。lincy 上 node 通常在 `/usr/local/bin/node`。

## 開發模式

```bash
# Terminal 1：後端
uv run chat-web-api serve          # :9002

# Terminal 2：前端（HMR）
cd src/chat_web_ui && bun run dev  # :5173，proxy /api → :9002
```

Production 模式由 `chat-web-api` 直接 serve `dist/` 靜態檔。

## 注意事項

- Agent 頁是本機單使用者介面，信任 loopback service，不做登入、附件與 token streaming。
- Agent 頁只是 TUI 的鏡像：它自己沒有「回覆」概念，模型的可見回覆會走使用者選定的那個 channel（例如選 `discord` 送出，回覆就送到 Discord）。
- `web` / `system` 不在可選 channel 清單裡；舊 `web` channel 的歷史事件若還在活動流，就當成一般 channel badge 列渲染。
- `channels.web.enabled` 仍控制是否註冊 WebAdapter、以及是否輸出 Agent 活動事件（export sink）。Agent 頁送訊不再走 `web` channel；`history_limit` 只影響殘留的 web_chat JSONL 讀取，前端已不依賴它。
- Agent 活動事件是**當次執行**的快照：chat-cli 重啟就輪替檔案，Web 端讀不到上一輪歷史（`events.prev.jsonl` 只留給人工檢查）。
- Agent 活動 export 是唯讀旁路，不得反向影響 TUI；新增事件型別時要同步更新 `ui_event_stream.py` 的 `_EVENT_SPECS` 與前端 `AgentUiEventType`。
- `requests.jsonl` 含完整 message payload，**不要全部載入 cache**；`context_composition.py`（Context 頁）示範了 on-demand、每次請求重新 parse 的做法，之後若有新功能要讀 `requests.jsonl` 應比照此模式
- `read_cache_rate = cache_read_tokens / prompt_tokens`
- `write_cache` 和 `read_cache_rate` 分開顯示
- provider 不支援 write cache 度量時，前端直接顯示「無法測量」
- `watchfiles` 使用 OS 原生通知（macOS FSEvents），不是 polling
- 前端 `node_modules/` 和 `dist/` 已加入 `.gitignore`
- 新機器部署需先 `cd src/chat_web_ui && bun install` 安裝 node 依賴
