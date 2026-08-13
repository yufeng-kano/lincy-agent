# LLM Provider API 規格盤點

本文件記錄各 LLM provider 的 API 事實、本專案 adapter 規則、實測/逆向資訊。
作為 LLM config/client 設計的依據。

架構邊界與設計準則見 `docs/dev/provider-architecture.md`。

每個 provider 分三段：
1. **官方 API 事實**（有來源連結、可信度標註）
2. **本專案 adapter 規則**（非 API 事實，是本專案的映射/驗證邏輯）
3. **實測/逆向資訊**（無官方保證的內容）

---

## Copilot (GitHub Copilot API)

### 1. 上游歷史/逆向資訊

> **重要標示**：GitHub 目前沒有穩定、完整公開的「原生 Copilot 聊天 API」文件可直接對應本專案的使用方式。以下內容以歷史頁面、實測、以及 VS Code/Copilot proxy 逆向為主，不可視為官方穩定契約。

| 項目 | 事實 | 來源類型 | 可信度 | 備註 |
|------|------|---------|--------|------|
| 上游 endpoint | `POST {endpoint}/chat/completions` | 歷史官方文件 + 實測 | 中 | 本專案 proxy 封裝此格式，不直接對外暴露 |
| 上游 request 形狀 | 接近 OpenAI Chat Completions（`messages`, `model`, `tools`） | 歷史官方文件 + 實測 | 中 | 僅代表 proxy 對 GitHub 上游的轉換目標 |
| Token 交換 | `GET https://api.github.com/copilot_internal/v2/token` with `Authorization: token {github_token}` | 逆向（VS Code/Copilot proxy 行為） | 中 | 非穩定契約 |
| Device Flow | `POST /login/device/code` + 輪詢 `POST /login/oauth/access_token` | GitHub 官方 OAuth device flow 文件 | 高 | GitHub OAuth app 需啟用 device flow |
| Copilot login client_id | `Iv1.b507a08c87ecfe98`, scope `read:user` | 逆向 | 中 | 本專案目前作為預設值；隨時可能變更 |
| VS Code / Chat 版本節奏 | GitHub Copilot Chat extension release 與最新 stable / 最新版 VS Code lockstep | Visual Studio Marketplace（官方） | 高 | `editor-version` / `editor-plugin-version` 不宜長期固定舊值 |
| Telemetry | Copilot Chat extension 會蒐集 usage / error 資料，並遵守 VS Code telemetry 設定 | Visual Studio Marketplace（官方） | 高 | 本專案 proxy **未**複製這層行為 |
| IDE Headers | `copilot-integration-id: vscode-chat`, `editor-version`, `editor-plugin-version`, `x-vscode-user-agent-library-version` | 逆向 | 低 | 偽裝 VS Code，版本號需維護 |
| 過舊版本風險 | 社群回報 gateway 可能因 `editor-version` / `editor-plugin-version` 過舊回 `410 Gone` | GitHub Community 討論 | 中 | 非官方契約，但代表版本漂移有實際風險 |
| `x-initiator` | `"user"` 視為 premium request；`"agent"` 視為 agent request | 逆向 | 中 | 計費語意非官方穩定文件 |
| `x-interaction-id` / `x-interaction-type` | 上游接受互動追蹤欄位 | 逆向 | 中 | 本專案直接顯式送出 |
| `reasoning_effort` | `/chat/completions` 使用頂層 `reasoning_effort` | 逆向 + 實測 | 中 | GitHub 未文件化此欄位 |
| Vision header | 需 `copilot-vision-request: true` | 逆向 | 中 | — |
| 非串流 max output | 16K tokens | 實測 | 中 | 可能隨 API 更新變動 |
| 串流 max output | 64K tokens | 實測 | 中 | 同上 |

### 2. 本專案 adapter 規則

| 項目 | 規則 | 程式碼位置 |
|------|------|-----------|
| 對內 API | `CopilotClient` 只打本專案 native proxy `POST /chat` | `src/lincy/llm/providers/copilot.py` |
| 對內 request 格式 | `CopilotNativeRequest` 顯式攜帶 `initiator`, `interaction_id`, `interaction_type`, `request_id` | `src/lincy/llm/schema.py` |
| initiator 路由 | 由 `CopilotRuntime` 依 inbound 分類與同-turn request 次數決定；不再靠 message history 猜測 | `src/lincy/llm/providers/copilot_runtime.py` + `src/lincy/agent/core.py` |
| 本地登入 | `proxy copilot login` 走 device flow，保存 GitHub token 到使用者設定目錄 | `src/copilot_proxy/auth.py` + `src/copilot_proxy/__main__.py` |
| serve token 來源 | 先讀 env，再 fallback 到 token store | `src/copilot_proxy/settings.py` |
| `reasoning` payload | proxy 轉上游時送頂層 `reasoning_effort` string（非 `reasoning` object） | `src/copilot_proxy/service.py` |
| Vision header 自動偵測 | request 含 image parts 時 proxy 自動加 `copilot-vision-request: true` | `src/copilot_proxy/service.py` |
| Proxy 不注入 reasoning 預設值 | 未提供 `reasoning_effort` 時不補值 | `src/copilot_proxy/service.py` |
| tools + reasoning 表現差異（觀測） | Copilot gateway 上 `reasoning_effort + tools` 可能有模型別差異；**本專案不做 adapter 自動特判** | `src/copilot_proxy/service.py`（無特判） |

---

## Claude Code

### 1. 官方 API 事實 / 逆向資訊

> **重要標示**：Claude Code 並沒有公開、穩定文件化的「訂閱憑證直接打 Anthropic API」契約。以下需區分：
> - `POST /v1/messages` 等 payload 形狀：以 **Anthropic Messages API 官方文件** 為準
> - Claude Code bearer token、必要 system prompt、beta headers：屬於 **逆向 / 實測資訊**

| 項目 | 事實 | 來源類型 | 可信度 | 備註 |
|------|------|---------|--------|------|
| Upstream endpoint | `POST /v1/messages` | Anthropic 官方文件 | 高 | payload 形狀與一般 Anthropic Messages API 相同 |
| Tools format | `{name, description, input_schema}` | Anthropic 官方文件 | 高 | 與 Anthropic provider 相同 |
| Vision format | `image` block + `source.base64` | Anthropic 官方文件 | 高 | 與 Anthropic provider 相同 |
| Prompt caching | `cache_control` 標記 block，provider 依 prefix 命中 | Anthropic 官方文件 | 高 | breakpoint 位置必須穩定 |
| Auth header | `Authorization: Bearer <oauth access token>` | 逆向 / 實測 | 中 | 非 Anthropic API key，通常來自 Claude Code OAuth 憑證 |
| Required system prompt | 第一個 system text block 需為 `You are Claude Code, Anthropic's official CLI for Claude.` | 逆向 / 實測 | 中 | 社群 proxy 與實測都依賴此行為 |
| Beta headers | `claude-code-*`, `oauth-*`, `interleaved-thinking-*`, `fine-grained-tool-streaming-*` 等 beta header | 逆向 / 實測 | 低 | 非穩定契約，可能隨上游變動 |
| OAuth refresh endpoint | `POST https://console.anthropic.com/v1/oauth/token` | 逆向 / 實測 | 中 | 用 refresh token 換新 access token |
| OAuth usage endpoint | `GET https://api.anthropic.com/api/oauth/usage` with Bearer + `anthropic-beta: oauth-2025-04-20`；回 `five_hour` / `seven_day` 的 `utilization`（百分比）與 `resets_at`，另有 `limits[]`（session / weekly_all / weekly_scoped）與 overage 資訊 | 逆向 / 實測 | 中 | Claude Code CLI `/usage` 顯示來源；2026-07 實測；本專案 proxy 現會解析 `limits[]` 中 `kind=weekly_scoped` 的 model-scoped weekly 配額（如 Fable、Opus），以 `scope.model.display_name` 當 label，並在 `/usage` snapshot 以 `seven_day_scoped` 欄位曝露 |
| OAuth profile endpoint | `GET https://api.anthropic.com/api/oauth/profile`；回 `account`（email、display_name）與 `organization`（organization_type、rate_limit_tier） | 逆向 / 實測 | 中 | 2026-07 實測 |
| Rate limit headers | `/v1/messages` 回應帶 `anthropic-ratelimit-unified-5h-*` / `-7d-*`（utilization、reset epoch、status）| 逆向 / 實測 | 中 | proxy 會把 `anthropic-ratelimit-*` 原樣轉回 client（見 adapter 規則） |
| Beta-gated body 欄位 | `context_management`、1M context（`[1M]` 模型）等 body 欄位需搭配對應 `anthropic-beta` entry（如 `context-management-2025-06-27`、`context-1m-*`），缺 header 時上游回 `400 Extra inputs are not permitted`；server tool `advisor_20260301`（CLI `advisorModel` 設定）需 `advisor-tool-2026-03-01` beta，缺時上游回 `400 does not match any of the expected tags` | 逆向 / 實測 | 中 | 2026-07 實測（advisor beta 為抓包 Claude Code CLI 所得）；Claude Code CLI 依賴此機制 |
| Models endpoint | `GET /v1/models` 用 OAuth Bearer + `anthropic-version` 可查帳號可用模型（官方 API 形狀） | Anthropic 官方文件 + 實測 | 高 | OAuth token 可用性為實測 |
| Sonnet 5 thinking 預設 | Claude Sonnet 5（`claude-sonnet-5`）adaptive thinking 預設開啟，省略 `thinking` 等同 `{"type": "adaptive"}`；手動 `{"type": "enabled", "budget_tokens": N}` 回 400；需顯式送 `{"type": "disabled"}` 才能關閉。與 Opus 4.7/4.8（預設關閉、需顯式送 `adaptive` 才開啟）相反 | Anthropic 官方文件 | 高 | 2026-07 查證，[Adaptive Thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking) |
| Sonnet 5 effort 支援值 | 官方支援 `low/medium/high(預設)/xhigh/max` | Anthropic 官方文件 | 高 | [Effort](https://platform.claude.com/docs/en/build-with-claude/effort)；本專案 schema 現況見下方 adapter 規則 |
| Opus 5 thinking 預設 | Claude Opus 5（`claude-opus-5`）thinking 預設開啟，省略 `thinking` 等同 `{"type": "adaptive"}`（與 Sonnet 5 同、與 Opus 4.7/4.8 相反）；`{"type": "enabled", "budget_tokens": N}` 回 400 | Anthropic 官方文件 | 高 | 2026-07 查證，[Adaptive Thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking) |
| Opus 5 disabled thinking 的 effort 上限 | `{"type": "disabled"}` 只在 effort `high` 以下被接受，配 `xhigh`/`max` 回 400。每個 request 各自驗證，不是 conversation 層設定 | Anthropic 官方文件 | 高 | 本專案 `claude-opus-5/no-thinking.yaml` 因此固定 `effort: low` |
| Opus 5 effort 支援值 | 官方支援 `low/medium/high(預設)/xhigh/max`；`temperature`/`top_p`/`top_k` 非預設值回 400 | Anthropic 官方文件 | 高 | [Effort](https://platform.claude.com/docs/en/build-with-claude/effort)；本專案 schema 現況見下方 adapter 規則 |
| Opus 5 context / 輸出上限 | 1M context（預設即最大）、max output 128K；prompt cache 最小可快取 prefix 降到 512 tokens（Opus 4.8 為 1024） | Anthropic 官方文件 | 高 | 2026-07 查證 |

### 2. 本專案 adapter 規則

| 項目 | 規則 | 程式碼位置 |
|------|------|-----------|
| 對內 API | `ClaudeCodeClient` 只打本專案 native proxy `POST /v1/messages` | `src/lincy/llm/providers/claude_code.py` |
| Payload 保真 | client 保留 `system` block array、message block array、`cache_control`；**不壓平成單一 system string** | `src/lincy/llm/providers/claude_code.py` |
| Tool result 合併 | 同一 assistant tool-use turn 的連續 `role: "tool"` 結果合併為一個 user message 的多個 `tool_result` block；與 Anthropic/Heyroute 共用 Messages mapper | `src/lincy/llm/providers/anthropic_messages.py` | 符合 Messages API 平行 tool use 形狀，且 heyroute 明確拒絕拆分結果 |
| Required prompt 注入 | proxy 在 upstream request 的第一個 system block 固定注入 Claude Code 必要 prompt；若已存在同內容首 block，則不重複注入 | `src/claude_code_proxy/service.py` |
| Browser OAuth login | `uv run proxy claude-code login` 走 browser OAuth + 手動貼 `code#state`；成功後 append 到本專案多顆 token store（`tokens.json`），每顆自動配 id。可重複登入多顆帳號 | `src/claude_code_proxy/__main__.py` + `src/claude_code_proxy/auth.py` |
| Multi-token failover | serve 平時只用優先級最高（預設最新登入）的一顆；upstream 回 401/403/429 才把該顆 bench，由下一次 `acquire()` 決定是否有下一顆可切（避免 lock-free 全域計數 race）。bench 帶冷卻（`FAILURE_COOLDOWN_SECONDS`，預設 300s）而非永久，冷卻後自動歸隊。**bench 語義限定為「upstream auth/quota 失敗」**：transport timeout 不 bench（見下方 Timeout 一列），過期又沒有 refresh token 也不 bench（每次重評成本為零，直接當成錯誤回報）。本輪所有顆都回 HTTP failover status 時回傳**上游原始錯誤**；所有顆都 timeout 時回 `504`；完全沒有可試的 token 才回 `503`（`ClaudeCodeTokenUnavailableError`） | `src/claude_code_proxy/service.py` + `src/claude_code_proxy/app.py` |
| Timeout 政策 | 兩條路徑的上游 read timeout 都是 None：streaming 見下方 keepalive 一列，非 streaming 的 `forward_json` 同樣用 `httpx.Timeout(request_timeout, read=None)`，只有 connect/write/pool 受 `request_timeout` 約束。理由：大 prompt 的非串流生成可以靜默數分鐘（實測 brain 267k prompt 要 100-280s），proxy 不該比 client 自己的 timeout 更緊。若真的發生 transport timeout，該顆**只在本次 request 內**被排除（`acquire(exclude=...)`）換下一顆重試，不寫進 bench 狀態 | `src/claude_code_proxy/service.py`（`forward_json`） |
| 空 pool 的 503 | pool 空掉時錯誤訊息要講真正原因：全部只是 bench 冷卻中 → 訊息說明 benched + 剩餘秒數，並帶 `Retry-After` header，不提 `--access-token`；真的沒憑證（沒有 token / refresh 失敗 / 過期無 refresh）才回原本的 `Claude Code token is required...` | `src/claude_code_proxy/service.py`（`_select_usable_token`）+ `src/claude_code_proxy/app.py`（`_token_unavailable_response`） |
| Token 優先級管理 | `tokens list` 列出（新者在前）、`tokens promote <id>` 把某顆提到最前、`tokens remove <id>` 刪除 | `src/claude_code_proxy/__main__.py` + `src/claude_code_proxy/auth.py` |
| Token 來源 | 先讀 env / `--access-token`（bypass store、不 failover），否則讀多顆 token store 依優先級選一顆，過期則用 refresh token 換新並寫回 store。已移除 Claude Code credentials / Keychain 匯入 | `src/claude_code_proxy/service.py` + `src/claude_code_proxy/auth.py` |
| 舊檔遷移 | 首次讀取時若偵測到舊版單顆 `token.json`，自動併入 `tokens.json` 並刪舊檔 | `src/claude_code_proxy/auth.py` |
| 進站認證 | proxy 預設綁 `127.0.0.1:4142`。`/v1/messages` 對 loopback 來源（`127.0.0.0/8`、`::1`、IPv4-mapped）一律免驗；非 loopback 來源必須帶進站 API key（`x-api-key` 或 `Authorization: Bearer`），比對 `--api-key` / `CLAUDE_CODE_PROXY_API_KEY`。未設定 key 時非 loopback 一律 `401`（fail closed），因此綁 `0.0.0.0` 不會裸奔。只信 socket peer address，不吃 `X-Forwarded-For`；`/health` 不設限；CORS middleware 已移除（避免瀏覽器網頁沾 localhost 免驗待遇）。注意與 `--access-token`（上游 Anthropic 憑證）是兩回事。由 supervisor 啟動時，repo 根目錄 `.env` 會自動注入（設定範例見 `.env.example`） | `src/claude_code_proxy/app.py` + `src/claude_code_proxy/settings.py` |
| Usage snapshot | `GET /usage`（過進站閘門）回 token pool 每顆的帳號身分（email / plan / rate_limit_tier）、5h 與 7d utilization / resets_at、狀態（active / standby / benched / unusable，active 為 acquire 會選中的第一顆），加 active 帳號的 `models[]`。快取 60s（`USAGE_CACHE_TTL_SECONDS`），`?refresh=true` 可繞過快取讀取（結果仍寫回快取）；過期 token 會先 refresh，單一帳號**暫時性**失敗不影響整體 snapshot；抓取失敗（如 OAuth endpoint 429）時退回該帳號上次成功資料並標 `stale: true`。Auth 失效（401/403）視為「沒有可用帳號」而非錯誤：env override（`__env_access_token__`）直接省略；store 帳號標 `unusable` 且 `error: null`（仍可 remove / 再登入） | `src/claude_code_proxy/service.py`（`usage_snapshot`）+ `src/claude_code_proxy/app.py` |
| Streaming keepalive | streaming 上游 read timeout 設為 None（大 prompt 處理期間 SSE 靜默可達數分鐘），並在上游第一個 byte 前由 proxy 每 30s 送 SSE 註解行 `: keepalive`（解析器會忽略、只插在 event 邊界前），避免 Cloudflare tunnel 約 100-120s 的 origin read timeout 切線（524）。副作用：streaming 不再有等 headers 的 ReadTimeout token failover（401/403/429 failover 不受影響） | `src/claude_code_proxy/service.py`（`open_stream`）+ `src/claude_code_proxy/app.py`（`_stream_with_keepalive`） |
| Models / OAuth 路徑 timeout | `GET /v1/models`、OAuth refresh、usage 抓取仍用完整 `request_timeout`（含 read）。這些都是短請求，沒有長靜默問題，維持有界 | `src/claude_code_proxy/service.py` |
| Models passthrough | `GET /v1/models`（過進站閘門）以官方 API 形狀原樣轉發，query 參數透傳，與 `/v1/messages` 相同的 401/403/429 token failover；web dashboard 的 model list 亦重用此路徑 | `src/claude_code_proxy/service.py`（`forward_models`）+ `src/claude_code_proxy/app.py` |
| Client beta 合併轉發 | client 送來的 `anthropic-beta` 會與 proxy 必要清單合併（去重、保序）後送上游，讓 `context_management`、`[1M]` 等 beta-gated 欄位可經 proxy 使用；body 端本來就以 `extra="allow"` 透傳 | `src/claude_code_proxy/service.py`（`_beta_headers`）+ `src/claude_code_proxy/app.py` |
| Tools 原樣透傳 | `tools[]` 不做型別驗證（raw dict）：server tool（如 `advisor_20260301`、web_search，只有 `type`/`name` 等欄位、無 `description`/`input_schema`）與 custom tool 的完整 JSON schema（`$schema`、`additionalProperties`...）都逐字轉發，schema 驗證交給上游。曾因強型別驗證對 advisor tool 回 `422 Field required`（Claude Code CLI 開 advisorModel 時） | `src/lincy/llm/schema.py`（`ClaudeCodeRequest.tools`） |
| Ratelimit headers 轉發 | `/v1/messages` 成功回應（含 streaming）把上游 `anthropic-ratelimit-*` headers 原樣轉回 client，讓 Claude Code CLI 等工具經 proxy 仍能顯示 5h/週用量警告 | `src/claude_code_proxy/service.py`（`passthrough_headers`）+ `src/claude_code_proxy/app.py` |
| Thinking payload | YAML 直接用 Claude Code `thinking` 物件：`type=adaptive|enabled|disabled`；`enabled` 時可選 `budget_tokens` | `src/lincy/core/schema.py` + `src/lincy/llm/providers/claude_code.py` |
| Effort payload | YAML 直接用 `output_config.effort`；client passthrough 成 upstream `output_config` | `src/lincy/core/schema.py` + `src/lincy/llm/providers/claude_code.py` |
| Effort 值集合 | `ClaudeCodeOutputConfig.effort` 允許 `low/medium/high/xhigh/max`，client 原樣 passthrough，不做任何改寫或降級 | `src/lincy/core/schema.py`（`ClaudeCodeOutputConfig`）+ `src/lincy/llm/providers/claude_code.py`（`_map_output_config`） |
| Effort × model 相容性 | 在 config 載入時（`validate_reasoning`）早停驗證，不留到 runtime 吃 400。表 `_CLAUDE_CODE_MODEL_EFFORTS` 只列「已知有限制」的 family：Haiku 4.5 / Sonnet 4.5 完全不吃 effort、Opus 4.5 只到 `high`、Opus 4.6 / Sonnet 4.6 沒有 `xhigh`；未列出的 model id（含未來新模型）一律放行 | `src/lincy/core/schema.py`（`_CLAUDE_CODE_MODEL_EFFORTS`、`ClaudeCodeConfig.validate_reasoning`） |
| Opus 5 disabled thinking 驗證 | `thinking.type=disabled` + `effort` `xhigh`/`max` 在載入時直接報錯（上游會回 400）。此規則只套在 Opus 5，Opus 4.7/4.8 的 disabled + `max` 仍合法 | `src/lincy/core/schema.py`（`_CLAUDE_CODE_DISABLED_THINKING_EFFORT_CAPPED`） |
| Effort beta header | proxy 依 request model / `output_config.effort` 動態補 `effort-2025-11-24`，不再只靠固定 header 清單 | `src/claude_code_proxy/service.py` |
| Prompt caching 開關 | app 層將 `claude_code` 列入 cache provider 白名單，讓 `ContextBuilder` 可下 BP1/BP2/BP3 | `src/lincy/cli/app.py` + `src/lincy/context/builder.py` |
| Availability 錯誤處理 | `HTTP 429` 與 `HTTP 529 overloaded` 都視為 availability/transient failure，走 retry / failover；不歸類成 request-format | `src/lincy/llm/retry.py` + `src/lincy/llm/failover.py` + `src/lincy/agent/core.py` |
| Structured outputs | `response_schema` 目前不支援；client 早停報錯，不做 silent ignore | `src/lincy/llm/providers/claude_code.py` |

### 3. 實測 / 社群維護風險

| 項目 | 事實 | 來源類型 | 可信度 | 備註 |
|------|------|---------|--------|------|
| 社群 proxy bug：quota hit 卡住 | `horselock/claude-code-proxy` open issue #7 回報 hit limit 後 client 可能卡住 | GitHub issue | 中 | 代表上游錯誤型態要保守 passthrough |
| 社群 proxy schema bug | open PR #12 修 `system` 為 string 時包裝成無效 array 的問題 | GitHub PR | 中 | 代表不能依賴社群 main branch 當規格 |
| 社群 proxy client-specific patch 持續增加 | open PR #10/#11 持續新增 Haiku / TypingMind 特判 | GitHub PR | 中 | 本專案目前不跟進 client-specific 功能旗標 |

---

## Codex

### 1. 官方 API 事實 / 逆向資訊

> **重要標示**：這裡說的 `codex` provider，不是 OpenAI Platform API key 直接打 `api.openai.com`。本專案走的是 **ChatGPT / Codex CLI OAuth**，再由本地 proxy 轉送到 ChatGPT Codex backend。`chatgpt.com/backend-api/codex/responses` 與必要 headers 屬於逆向 / 實測資訊，不是官方穩定契約。

| 項目 | 事實 | 來源類型 | 可信度 | 備註 |
|------|------|---------|--------|------|
| 官方登入入口 | `codex login` 支援 ChatGPT 帳號登入 | OpenAI 官方文件 | 高 | 本專案另外補自己的 browser OAuth flow |
| 官方 CLI auth 檔 | `~/.codex/auth.json` 會保存 ChatGPT OAuth 狀態 | 官方 CLI 行為 + 本機實測 | 高 | 本專案只讀，不直接改這個檔 |
| ChatGPT OAuth token 不能直打 Platform model API | `GET https://api.openai.com/v1/models/gpt-5.2-codex` 會回 `403` 缺 `api.model.read` | 本機實測 | 高 | 代表這不是 Platform API token |
| ChatGPT Codex backend endpoint | `POST https://chatgpt.com/backend-api/codex/responses` | 逆向 + 本機實測 | 中 | 非官方穩定契約 |
| 必要 headers | `Authorization`, `chatgpt-account-id`, `OpenAI-Beta: responses=experimental`, `originator: codex_cli_rs` | 逆向 + 本機實測 | 中 | 缺少時可能驗證失敗 |
| OAuth backend 已驗證可用模型 | `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex`, `gpt-5.3-codex-spark`, `gpt-5.2` 都能透過本專案 OAuth proxy 正常回應 | 本機實測 | 中 | 2026-04-11 測試；是否放行仍可能依帳號變動 |
| prompt cache key | 官方開源 CLI 會送 `prompt_cache_key`，值預設是 conversation id | 官方開源程式碼 | 高 | `openai/codex` `codex-rs/core/src/client.rs` |
| conversation header | 官方開源 CLI 會送 `session_id` header，值是 conversation id | 官方開源程式碼 | 高 | `openai/codex` `codex-rs/codex-api/src/requests/headers.rs` |
| account id 來源 | access token JWT claim `https://api.openai.com/auth.chatgpt_account_id` | 本機實測 | 高 | 本專案從 JWT 解出來 |
| refresh endpoint | `POST https://auth.openai.com/oauth/token` with `grant_type=refresh_token` | 社群 proxy + 本機實測 | 中 | 目前可用，但非正式文件化 |
| tool calling 事件 | SSE 會發 `response.output_item.done`、`response.function_call_arguments.delta/done` | 本機實測 | 中 | `response.completed.response.output` 可能是空陣列 |
| usage endpoint | `GET https://chatgpt.com/backend-api/codex/usage` with headers `Authorization: Bearer <access>`, `chatgpt-account-id: <account_id>`, `OpenAI-Beta: responses=experimental`, `originator: codex_cli_rs`, `Accept: application/json`；回 200，body 含 `email`、`plan_type`、`user_id`、`account_id`、`rate_limit: {allowed, limit_reached, primary_window, secondary_window}`（各 window 含 `used_percent`、`limit_window_seconds`、`reset_after_seconds`、`reset_at`（unix ts）；`secondary_window` 同 shape，帳號沒有次要視窗時為 `null`），另有 `credits`、`spend_control`、`rate_limit_reset_credits` | 逆向 + 本機實測（真實帳號） | 中 | 2026-07-18 實測；`/backend-api/wham/usage` 是同一 payload 的別名 endpoint。注意：chatgpt.com edge 的 bot 規則會擋 httpx/httpcore 客戶端（不論 headers，含 http2、自帶 stdlib SSLContext 都是 403 HTML challenge），stdlib `urllib` 與 `urllib3`/`requests` 可通過；`/codex/responses` 不受影響。故 proxy 的 usage fetch 走 stdlib urllib（thread 執行），並帶 `User-Agent: codex_cli_rs/...`（無 UA 或 python 預設 UA 也會被擋） |

### 2. 本專案 adapter 規則

| 項目 | 規則 | 程式碼位置 |
|------|------|-----------|
| 對內 API | `CodexClient` 只打本專案 native proxy `POST /chat` | `src/lincy/llm/providers/codex.py` |
| 對內 request 格式 | `CodexNativeRequest` 顯式攜帶 `messages`, `tools`, `response_schema`, `reasoning_effort` | `src/lincy/llm/schema.py` |
| 本地登入 | `uv run proxy codex login` 走 browser OAuth：開瀏覽器 + 本地 `localhost:1455/auth/callback` 監聽器自動完成；監聽器綁不到 port（例如官方 `codex login` 占用）時退回手動貼上（完整 callback URL 或 `code#state`）。重複執行可登入多顆帳號做 failover。`login --from-codex` 可把目前官方 `~/.codex/auth.json` 匯入成 store 裡一顆獨立、可 promote/remove 的帳號 | `src/codex_proxy/__main__.py` + `src/codex_proxy/auth.py` + `src/codex_proxy/service.py` |
| Auth 檔路徑 | `codex_auth_path` 固定讀官方預設 `~/.codex/auth.json`，不提供 env override（`CODEX_PROXY_CODEX_AUTH_PATH` 已移除）；本專案自己的多顆 token store 另外存在 `token_path`（env `CODEX_PROXY_TOKEN_PATH`，預設 `~/Library/Application Support/chat-agent/codex-proxy/tokens.json`） | `src/codex_proxy/settings.py` + `src/codex_proxy/auth.py` |
| Token pool 優先級與 dedup | serve 依序嘗試：(1) 本專案 token store，依 `created_at` 新到舊；(2) 官方 `~/.codex/auth.json` 當作隱式、優先級最低的 fallback（固定 id `__codex_auth__`）。store 裡任一顆的 `account_id` 與官方檔相同時，官方檔那顆會被跳過（避免同帳號重複出現在 pool / usage） | `src/codex_proxy/service.py`（`CodexTokenManager._load_candidates`） |
| Token 來源與 refresh 寫回差異 | store 裡的顆過期時 refresh 並寫回 store（`source` 變 `oauth_refresh`，保留原 `id`/`created_at`）；官方 auth 檔那顆過期時**只在記憶體 refresh，不改寫 `~/.codex/auth.json`**（該檔案仍由官方 `codex login` 管理） | `src/codex_proxy/service.py`（`CodexTokenManager._refresh`） |
| Multi-token failover | `/chat`、`/compact` 上游回 401/403/429，或等待 response headers 時 `ReadTimeout`，會把該顆 bench（`FAILURE_COOLDOWN_SECONDS`，預設 300 秒冷卻，非永久）並切下一顆重試。本輪所有可用顆都回 failover 狀態時回傳**上游原始錯誤**；都 timeout 時回 504；完全沒有可用 token 時回 503（`CodexTokenUnavailableError`） | `src/codex_proxy/service.py` + `src/codex_proxy/app.py` |
| Token 優先級管理 | `tokens list` 列出（新者在前，含 `account_id`）、`tokens promote <id>` 把某顆提到最前、`tokens remove <id>` 刪除；官方 auth fallback（`__codex_auth__`）不可 promote/remove，走這兩個操作會回 404 並提示改用官方 `codex login` | `src/codex_proxy/__main__.py` + `src/codex_proxy/auth.py` + `src/codex_proxy/app.py` |
| 進站認證 | proxy 預設綁 `127.0.0.1:4143`。`/chat`、`/compact`、`/health` 沿用舊行為維持免驗（本地 provider client 不送 key）；新增的管理面（`/usage`、`/login*`、`/tokens*`）採跟 claude-code-proxy 相同的閘門：loopback 一律免驗，非 loopback 須帶 `CODEX_PROXY_API_KEY`（`x-api-key` 或 `Authorization: Bearer`），未設定 key 時非 loopback 一律 401 | `src/codex_proxy/app.py` + `src/codex_proxy/settings.py` |
| Usage snapshot | `GET /usage`（過進站閘門）回 token pool 每顆的帳號身分（email / plan_type）、rate-limit windows（`utilization`、ISO8601 UTC `resets_at`、`label`）、狀態（active / standby / benched / unusable）。`label` 規則：`limit_window_seconds` 為 3600 倍數且 < 24 小時 → `"{h}h"`（如 18000 → `"5h"`）；`604800` → `"Week"`；其餘依天數 → `"{d}d"`。快取 60 秒（`USAGE_CACHE_TTL_SECONDS`），`?refresh=true` 可繞過快取；單一帳號**暫時性**抓取失敗時退回上次成功資料並標 `stale: true`，不影響其他帳號。Auth 失效（401/403）視為「沒有可用帳號」而非錯誤：官方 `__codex_auth__` fallback 直接省略（空列表）；store 帳號標 `unusable` 且 `error: null`（仍可 remove / 再登入）。Codex 沒有 models passthrough，`models` 固定回 `[]`（維持與 claude-code-proxy `/usage` 的 shape 對稱） | `src/codex_proxy/service.py`（`usage_snapshot`）+ `src/codex_proxy/app.py` |
| Login 流程與背景 callback listener | `begin_login()` 開始新的 PKCE + state，並確保背景 asyncio TCP listener 跑在 `callback_bind_host:callback_bind_port`（預設 `127.0.0.1:1455`，對齊固定的 `redirect_uri`）；listener 收到 `GET /auth/callback?code=..&state=..` 會比對 pending login 的 state、換 token、存進 store，並標記該 login 完成。listener 綁 port 失敗（如官方 `codex login` 占用）時不擋 `begin_login()`，改在回應裡帶 `listener_error`，manual paste（`POST /login/{id}/complete`）仍可用。`GET /login/{id}` 可輪詢狀態（`pending` / `completed` / `expired`），完成後的 login 保留約 5 分鐘供輪詢 | `src/codex_proxy/service.py`（`begin_login`、`login_status`、`complete_login`、`_CodexCallbackListener`）+ `src/codex_proxy/app.py` |
| 上游 endpoint | proxy 固定轉送到 `/codex/responses` | `src/codex_proxy/service.py` |
| `max_output_tokens` 處理 | native request 保留欄位，但 proxy 不會轉送到 ChatGPT backend，因為實測會回 `400 Unsupported parameter: max_output_tokens` | `src/codex_proxy/service.py` |
| Prompt cache key | app 組裝層對 `codex` 產生 request-level `prompt_cache_key`；key 基底跟官方 CLI 一樣用 session / conversation 概念，再額外加 agent namespace 與本地 TTL bucket，讓 `agent.yaml` 的 `cache.ttl` 不會被 silent ignore | `src/lincy/cli/app.py` + `src/lincy/llm/providers/codex.py` + `src/codex_proxy/service.py` |
| Conversation identity | app 組裝層會另外帶 `session_id`；proxy 轉成 upstream `session_id` header，盡量貼近官方 CLI conversation header 行為 | `src/lincy/cli/app.py` + `src/lincy/llm/providers/codex.py` + `src/codex_proxy/service.py` |
| Turn sticky routing | app 組裝層對 `codex` 另外帶本地 `turn_id`；proxy 會保存並重送 `x-codex-turn-state`，盡量貼近官方 CLI 同 turn sticky routing 行為，避免 tool loop 後續 round 打到不同 shard | `src/lincy/session/manager.py` + `src/lincy/cli/app.py` + `src/lincy/llm/providers/codex.py` + `src/codex_proxy/service.py` |
| Remote compact 開關 | app-level `features.codex_remote_compaction.enabled` 開啟且 brain provider 為 `codex` 時，agent 的手動 `/compact`、soft-limit compact、overflow retry、context refresh 都優先走 proxy `POST /compact`，而不是直接裁切最近幾輪 | `cfgs/agent.yaml` + `src/lincy/cli/app.py` + `src/lincy/agent/core.py` |
| 對內 compact API | `CodexClient.compact_messages()` 打本專案 native proxy `POST /compact` | `src/lincy/llm/providers/codex.py` |
| 上游 compact endpoint | proxy 轉送到 `/codex/responses/compact` | `src/codex_proxy/service.py` |
| Remote compact fallback | 若官方 compact 失敗，agent 會記 warning 並 fallback 回既有的本地 `conversation.compact(preserve_turns)`，避免 turn 直接失敗 | `src/lincy/agent/core.py` |
| Compact 可觀測性 | CLI `/compact`、soft-limit warning、context refresh 訊息都會顯示 `via codex remote` / `via local fallback`；session debug 另外在 `events.jsonl` 寫 `compaction` event，並在 `turns.jsonl` 記 `compaction_source` 等欄位 | `src/lincy/agent/adapters/cli.py` + `src/lincy/agent/core.py` + `src/lincy/session/debug_store.py` |
| `cache.ttl` 語意 | 對 `codex` 而言，`cache.ttl` 目前代表**本地 prompt cache key 旋轉週期**，不是 upstream 明文 TTL 參數：`ephemeral`=5 分鐘、`1h`=1 小時、`24h`=1 天 | `src/lincy/cli/app.py` | upstream request 目前只看到 `prompt_cache_key`，沒看到公開 TTL 欄位 |
| System prompt 處理 | 所有 `system` message 先合併成 `instructions`，不送進 `input[]` | `src/codex_proxy/service.py` |
| Tool history 映射 | assistant `tool_calls[]` -> `function_call`；tool result -> `function_call_output` | `src/codex_proxy/service.py` |
| Compact item 映射 | remote compact 回傳的 `compaction_summary` 會在本地 session 存成帶 `codex_compaction_encrypted_content` 的 synthetic message；之後 proxy 會再映回 upstream `compaction_summary` item | `src/lincy/llm/schema.py` + `src/lincy/context/builder.py` + `src/codex_proxy/service.py` |
| 圖片 tool result 映射 | tool result 裡的 image parts 會改成緊接著的 user `input_image` message | `src/codex_proxy/service.py` |
| Reasoning payload | proxy 送 Responses-style `reasoning: {"effort": "...", "summary": "auto"}` | `src/codex_proxy/service.py` |
| curated profile efforts | Codex curated profiles 目前列 low/medium/high/xhigh；`thinking.yaml` 預設用 `xhigh`，`no-thinking.yaml` 維持 `effort: null`；不列 `max`，避免把未實測值當成支援能力 | `cfgs/llm/codex/` | profile 清單是本專案選擇，不是官方 Codex backend 契約 |
| Structured outputs | `response_schema` 映射到 `text.format = {type: "json_schema", ...}` | `src/codex_proxy/service.py` |
| 回應解析 | 以 SSE `response.output_item.done` 為主還原 content / tool_calls；不依賴 `response.completed.response.output` 一定有值 | `src/codex_proxy/service.py` |

### 3. 社群參考來源

| 項目 | 事實 | 來源類型 | 可信度 | 備註 |
|------|------|---------|--------|------|
| OAuth / backend headers 參考 | `insightflo/chatgpt-codex-proxy` | GitHub repo | 中 | `src/auth.ts`, `src/codex/client.ts` |
| OpenAI / Anthropic / Codex 格式轉換參考 | `icebear0828/codex-proxy` | GitHub repo | 中 | `src/translation/*` |
| 官方 prompt cache 行為參考 | `openai/codex` | GitHub repo | 高 | `codex-rs/core/src/client.rs`, `codex-rs/core/tests/suite/prompt_caching.rs` |
| 本專案程式註解 | reverse-engineered 常數與 header 附上原 repo 註解 | 本專案規則 | 高 | 方便日後追查來源 |

### 4. 目前實測狀態

| 項目 | 事實 | 來源類型 | 可信度 | 備註 |
|------|------|---------|--------|------|
| `prompt_cache_key` transport | 本專案已實際送出 `prompt_cache_key`，上游正常接受，不報 request-format error | 本機實測 | 高 | 2026-04-11 |
| cache hit 觀測 | 實際 session 已觀測到 `cached_tokens > 0`；例如 `gpt-5.4` 某輪 `prompt_tokens=63,233`、`cached_tokens=61,824` | 遠端實測 | 高 | 2026-04-11 `lincy` session `20260411_133813_70b723` |
| 同 turn cache 不穩 | 同一 turn 內 round 3 可 hit、round 4 又掉成 `cached_tokens=0`；官方 CLI 有 `x-codex-turn-state` sticky routing，而本專案已補上同等 header 保存/重送 | 遠端實測 + 官方開源測試 | 高 | 先前 miss 很可能來自缺少同 turn sticky routing |
| cross-turn 存活觀測 | 回頭核對後，archive 裡接近 `53m~55m` 的高 hit 樣本其實是 `claude_code`，不是 `codex`；目前 `codex` 專屬樣本只看到 `10m` 內較常 full-hit，`13m~35m` 常見 cold miss，且有 `2.3m` miss 的反例；沒有 `24h` 證據 | 遠端實測 | 高 | 2026-04-12 彙整見 `docs/dev/codex-cache-survival.md` |

---

## Grok (SuperGrok OAuth via local proxy)

### 1. 官方 API 事實 / 逆向資訊

> **重要標示**：本專案 `provider: grok` 走 **SuperGrok / X Premium+ 訂閱 OAuth**，不是 `XAI_API_KEY` 計費路徑。OAuth 由 `grok-proxy` 處理；client 只打本地 proxy 的 OpenAI-compatible Chat Completions。

| 項目 | 事實 | 來源類型 | 來源連結 / 備註 | 可信度 |
|------|------|---------|----------------|--------|
| 官方 Chat Completions | `POST https://api.x.ai/v1/chat/completions` | 官方文件 | [xAI API](https://docs.x.ai/) | 高 |
| 官方 Responses API | `POST https://api.x.ai/v1/responses` | 官方文件 | 同上；proxy 亦 pass-through，但本 adapter 不用 | 高 |
| Auth（API key 路徑） | `Authorization: Bearer $XAI_API_KEY` | 官方文件 | 本專案 OAuth 路徑**不**使用此 key | 高 |
| Auth（訂閱 OAuth） | device-code against `auth.x.ai`；access token 短命，需 refresh | 社群逆向 + OIDC discovery | Hermes / OpenClaw 共用 public client_id；見 `src/grok_proxy/auth.py` | 中 |
| OAuth client_id | `b1a00492-073a-47ea-816f-4c329264a828` | 逆向 | xAI 共享 public client；consent 可能顯示 Grok Build | 中 |
| OAuth scope | `openid profile email offline_access grok-cli:access api:access` | 逆向 | 同上 | 中 |
| Chat Completions reasoning | 頂層 `reasoning_effort` string（OpenAI SDK / xAI SDK 皆如此） | 官方文件 | [Reasoning](https://docs.x.ai/developers/model-capabilities/text/reasoning) | 高 |
| Responses reasoning（對照） | `reasoning: {"effort": "..."}` nested object | 官方文件 | 與 Chat Completions 格式不同；本 adapter 不用 | 高 |
| grok-4.5 effort | `low` / `medium` / `high`（預設 high）；**不能完全關閉 reasoning** | 官方文件 | 同上 | 高 |
| grok-4.3 effort | 社群/相容層常見 `none`/`low`/`medium`/`high` | 社群 + 目錄慣例 | 以 profile `supported_efforts` 為準；上游拒絕則改 profile | 中 |
| 訂閱 OAuth 常見模型 | `grok-4.5`, `grok-4.3`, `grok-build-0.1` 等 | 社群（OpenClaw/Hermes） | 帳號 entitlement 可能不同 | 中 |
| Tier gate | OAuth 登入成功但 inference 403 可能是 allowlist | 社群實測 | 可改走 `XAI_API_KEY` / OpenRouter | 中 |

### 2. 本專案 adapter 規則

| 項目 | 規則 | 程式碼位置 |
|------|------|-----------|
| 對內 API | `GrokClient` 打本地 proxy `POST {base_url}/chat/completions` | `src/lincy/llm/providers/grok.py` |
| 預設 base_url | `http://localhost:4144/v1` | `src/lincy/core/schema.py`（`GrokConfig`） |
| Auth | client 送 sentinel `Bearer local-proxy`；**真實 token 由 grok-proxy 注入** | `src/lincy/llm/providers/grok.py` + `src/grok_proxy/service.py` |
| 本地登入 | `uv run proxy grok login`（device-code） | `src/grok_proxy/__main__.py` |
| `reasoning_effort` | YAML `reasoning.effort` / `enabled=false`→`none` 映射為頂層 `reasoning_effort` | `src/lincy/llm/providers/grok.py` |
| System messages | 連續 leading system 合併成一則（穩定 prefix 利於 automatic cache） | `src/lincy/llm/providers/grok.py` |
| Prompt cache sticky | Chat Completions 送 header `x-grok-conv-id`；值 = `session_id:agent_namespace[:ttl_bucket]`；proxy **原樣轉發**到 xAI | `src/lincy/cli/app.py` + `src/lincy/llm/providers/grok.py` + `src/grok_proxy/` |
| `cache.ttl` 語意 | 啟用 cache 時 bucket 與 codex 相同（`ephemeral`=5m / `1h` / `24h`），控制 sticky key 旋轉；關閉 cache 時仍 sticky 在 `session:namespace`（官方建議永遠帶 conv id） | `src/lincy/cli/app.py` |
| Responses pass-through | 若 client 只帶 `x-grok-conv-id` 且 body 無 `prompt_cache_key`，proxy 注入 `prompt_cache_key` | `src/grok_proxy/app.py` |
| Supervisor | `grok-proxy` `enabled: auto` when any agent uses `provider: grok` | `cfgs/supervisor.yaml` |
| Profiles | `cfgs/llm/grok/<model>/{thinking,no-thinking,low-thinking}.yaml` | `cfgs/llm/grok/` |

### 3. 逆向/實測資訊

| 項目 | 事實 | 來源類型 | 可信度 | 備註 |
|------|------|---------|--------|------|
| OAuth transport 社群偏好 | Hermes/OpenClaw 多用 Responses；本專案第一版 adapter 先走 Chat Completions pass-through | 社群文件 | 中 | 若 OAuth 僅放行 Responses，再加 Responses client |
| Access token 壽命 | ~6h；proxy refresh skew 1h | 社群實測 | 中 | `src/grok_proxy/auth.py` |

---

## OpenAI

### 1. 官方 API 事實

| 項目 | 事實 | 來源類型 | 來源連結 | 可信度 | 模型/版本相關 |
|------|------|---------|---------|--------|-------------|
| Endpoint | `POST /v1/chat/completions` | 官方文件 | [OpenAI Chat API Reference](https://platform.openai.com/docs/api-reference/chat) | 高 | 否 |
| Auth | `Authorization: Bearer {api_key}` | 官方文件 | 同上 | 高 | 否 |
| **Chat Completions reasoning** | `reasoning_effort: "low"\|"medium"\|"high"` — **頂層 string 欄位** | 官方文件 | [GPT-5.2 Guide](https://developers.openai.com/api/docs/guides/latest-model/) 原文："Chat Completions API uses: `reasoning_effort: 'none'`" | 高 | 是（reasoning models） |
| **Responses API reasoning（對照）** | `reasoning: {"effort": "..."}` — nested object。**與 Chat Completions 格式不同** | 官方文件 | [OpenAI Reasoning Guide](https://developers.openai.com/api/docs/guides/reasoning/) + [GPT-5.2 Guide](https://developers.openai.com/api/docs/guides/latest-model/) | 高 | — |
| Effort 值（GPT-5.2+） | `"none"`, `"low"`, `"medium"`, `"high"`, `"xhigh"` | 官方文件 | [GPT-5.2 Guide](https://developers.openai.com/api/docs/guides/latest-model/) | 高 | 是（xhigh + none 僅 GPT-5.2+） |
| Reasoning summary | `reasoning: {"summary": "auto"\|"detailed"}`（Responses API） | 官方文件 | [OpenAI Reasoning Guide](https://developers.openai.com/api/docs/guides/reasoning/) | 高 | 是 |
| Vision | `image_url` content parts | 官方文件 | [OpenAI Chat API Reference](https://platform.openai.com/docs/api-reference/chat) | 高 | 是（vision models） |
| Tools | OpenAI function calling format（`type: "function"`, `function: {name, description, parameters}`） | 官方文件 | 同上 | 高 | 否 |
| max_tokens | 可選；GPT-5+ 需改用 `max_completion_tokens` | 官方文件 | 同上 | 高 | 是（GPT-5+ 拒絕 `max_tokens`） |
| **max_completion_tokens** | GPT-5+ 必用（替代 `max_tokens`） | 官方文件 | 同上 | 高 | 是（GPT-5+） |
| temperature | 可選 | 官方文件 | 同上 | 高 | 否 |
| **Prompt caching** | 自動 prefix-based，≥1024 tokens，128-token 遞增 | 官方文件 | [Prompt Caching](https://developers.openai.com/api/docs/guides/prompt-caching) | 高 | 否 |
| **prompt_cache_retention** | `"in_memory"`（預設，≤1h）或 `"24h"`（extended） | 官方文件 | 同上 | 高 | 是（24h 僅 GPT-5+, GPT-4.1） |
| Cache 計費 | write 免費；read 50% off input（LiteLLM: 10% input rate） | 官方文件 | [Pricing](https://openai.com/api/pricing/) | 高 | 否 |
| Cache usage 回傳 | `prompt_tokens_details.cached_tokens`；`cache_write_tokens` 不回傳（永遠 0） | 官方文件 + 實測 | 同上 | 高 | 否 |
| `cache_control` 容忍 | content block 上的 Anthropic 式 `cache_control` 被 silent ignore，不報錯 | 實測 | `scripts/verify_openai_cache.py` | 中 | 否 |

### 2. 本專案 adapter 規則

| 項目 | 規則 | 程式碼位置 | 備註 |
|------|------|-----------|------|
| 送 `reasoning_effort` 頂層欄位 | `OpenAICompatibleClient` 送 `reasoning_effort` | `src/lincy/llm/providers/openai_compat.py` | 符合 Chat Completions API 官方格式 |
| `enabled=false` 需要 override | 驗證要求有 `provider_overrides.openai_reasoning_effort` | `src/lincy/core/schema.py`（`OpenAIConfig.validate_reasoning()`） | 本專案規則，非 API 限制 |
| `reasoning.effort` 值 | config 接受 low/medium/high/xhigh/max 並原樣送成 `reasoning_effort`；不代表每個 OpenAI 模型都支援完整集合；`max` 屬 passthrough，OpenAI 官方文件目前未列為 Chat Completions effort | `src/lincy/core/schema.py` + `src/lincy/llm/providers/openai.py` | 上游若不支援會回 request error；profile 內 `supported_efforts` 只保留已知/文件化提示，不作 hard gate |
| `max_tokens` 在 reasoning 裡擋掉 | OpenAI provider schema 不提供 reasoning.max_tokens 欄位 | `src/lincy/core/schema.py`（`OpenAIReasoningConfig`） | 本專案規則 |
| `max_completion_tokens` 切換 | `OpenAIConfig.use_max_completion_tokens=true` 時，client 送 `max_completion_tokens` 並 null 掉 `max_tokens` | `src/lincy/llm/providers/openai.py` + `src/lincy/core/schema.py` | GPT-5+ 必要 |
| `prompt_cache_retention` passthrough | agent cache config `ttl: "24h"` 時，組裝層傳入 `prompt_cache_retention="24h"` 給 `OpenAIClient` | `src/lincy/cli/app.py` + `src/lincy/llm/providers/openai.py` | 不走 breakpoint path |
| Cache TTL clamp | 組裝層依 provider 最大支援 TTL 做 clamp：OpenRouter `1h`、Anthropic/ClaudeCode `ephemeral`、OpenAI `24h` | `src/lincy/cli/app.py` | 避免 provider 切換時 silent misconfiguration |

### 3. 逆向/實測資訊

| 項目 | 事實 | 來源類型 | 可信度 | 備註 |
|------|------|---------|--------|------|
| GPT-5.1 cache hit rate | 同 prefix 第二次呼叫 99.2% hit；跨 turn prefix 穩定 98.5%+ | 實測 | 高 | `scripts/verify_openai_cache.py` |
| `cache_write_tokens` 永遠 0 | OpenAI 不回傳 write tokens | 實測 | 高 | 成本公式 `base = prompt - read - write` 對 OpenAI 成立（write=0） |

---

## DeepSeek

### 1. 官方 API 事實

| 項目 | 事實 | 來源類型 | 來源連結 | 可信度 | 備註 |
|------|------|---------|---------|--------|------|
| Endpoint | `POST /chat/completions` | 官方文件 | [Create Chat Completion](https://api-docs.deepseek.com/api/create-chat-completion) | 高 | OpenAI-format base URL 為 `https://api.deepseek.com` |
| Auth | `Authorization: Bearer {api_key}` | 官方文件 | [Your First API Call](https://api-docs.deepseek.com/) | 高 | API key 由 DeepSeek Platform 申請 |
| 現行模型 | `deepseek-v4-flash`, `deepseek-v4-pro` | 官方文件 | [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing) | 高 | `deepseek-chat` / `deepseek-reasoner` 是相容別名 |
| 舊模型別名棄用 | `deepseek-chat` 與 `deepseek-reasoner` 將於 2026-07-24 棄用 | 官方文件 | [Your First API Call](https://api-docs.deepseek.com/) | 高 | 不新增為本專案 profile |
| Thinking toggle | OpenAI format 使用 `thinking: {"type": "enabled"|"disabled"}` | 官方文件 | [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode) | 高 | 預設 enabled |
| Thinking effort | OpenAI format 使用頂層 `reasoning_effort: "high"\|"max"` | 官方文件 | [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode) | 高 | `low`/`medium` 會映射到 `high`，`xhigh` 會映射到 `max` |
| Thinking 輸出 | 回應 message 會有 `reasoning_content`，與 `content` 同層 | 官方文件 | [Create Chat Completion](https://api-docs.deepseek.com/api/create-chat-completion) | 高 | tool-call thinking 回合需回放 |
| Thinking 不支援取樣參數 | thinking mode 不支援 `temperature`, `top_p`, `presence_penalty`, `frequency_penalty` | 官方文件 | [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode) | 高 | 相容上可能不報錯，但不生效 |
| Tool calls | OpenAI function calling format，支援 `strict` beta | 官方文件 | [Function Calling](https://api-docs.deepseek.com/guides/function_calling/) | 高 | strict 需 beta base URL |
| JSON Output | `response_format: {"type": "json_object"}` | 官方文件 | [JSON Output](https://api-docs.deepseek.com/guides/json_mode/) | 高 | 需在 prompt 明確要求 JSON |
| Context cache | 自動啟用，無需修改 request | 官方文件 | [Context Caching](https://api-docs.deepseek.com/guides/kv_cache) | 高 | usage 回傳 hit/miss tokens |
| Cache usage | `usage.prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` | 官方文件 | [Context Caching](https://api-docs.deepseek.com/guides/kv_cache) | 高 | `prompt_tokens = hit + miss` |

### 2. 本專案 adapter 規則

| 項目 | 規則 | 程式碼位置 | 備註 |
|------|------|-----------|------|
| Provider 名稱 | 使用獨立 `provider: deepseek`，不共用 `openai` config | `src/lincy/core/schema.py` + `src/lincy/llm/providers/deepseek.py` | DeepSeek 有專屬 thinking/cache 規則 |
| Base URL | profile 使用 `https://api.deepseek.com`，client 自行附加 `/chat/completions` | `src/lincy/llm/providers/deepseek.py` | 拒絕 `/v1` 或 `/chat/completions` 結尾 |
| Thinking config | YAML 使用 `thinking.enabled` 與 `thinking.effort`；enabled 時只允許 `high` / `max` | `src/lincy/core/schema.py` | 不接受官方會自動映射的 effort 值 |
| Thinking payload | enabled 時送 `thinking.type=enabled` 與 `reasoning_effort`；disabled 時只送 `thinking.type=disabled` | `src/lincy/llm/providers/deepseek.py` | disabled 不送 `reasoning_effort` |
| Temperature 驗證 | thinking enabled 時若設定 `temperature` 則早停報錯 | `src/lincy/core/schema.py` | 避免 silent no-op |
| Reasoning 回放 | assistant tool-call history 使用 `reasoning_content`，不使用 OpenAI-compatible base client 的 `reasoning` 欄位 | `src/lincy/llm/providers/deepseek.py` | 避免 DeepSeek thinking tool 回合 400 |
| 合成 tool call 回放 | thinking enabled 時，assistant tool-call history 若沒有可回放的 `reasoning_content`，adapter 送空字串欄位 | `src/lincy/llm/providers/deepseek.py` | boot / pinned context / skill prerequisite 這類系統合成 tool call 沒有模型 reasoning；實測最後一則訊息為 tool result 時缺欄位會 400 |
| Structured outputs | `response_schema` 目前不支援；client 早停報錯 | `src/lincy/llm/providers/deepseek.py` | DeepSeek JSON Output 不是本專案目前的 JSON Schema 介面 |
| Cache metrics | `prompt_cache_hit_tokens` 映射為 `LLMResponse.cache_read_tokens`，`cache_write_tokens=0` | `src/lincy/llm/providers/deepseek.py` | DeepSeek 不回傳 write tokens |

### 3. 實測資訊

| 項目 | 事實 | 來源類型 | 可信度 | 備註 |
|------|------|---------|--------|------|
| `deepseek-v4-flash` no-thinking | `thinking.type=disabled` + JSON Output 實際回 `200` | 本機實測 | 高 | 2026-05-12，回傳 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` |
| `deepseek-v4-pro` thinking/max | `thinking.type=enabled` + `reasoning_effort=max` 實際回 `200` 且含 `reasoning_content` | 本機實測 | 高 | 2026-05-12 |
| thinking tool-result continuation | request 最後一則為 tool result，且前一則 assistant tool call 缺 `reasoning_content` 時回 `400`；送 `reasoning_content: ""` 時回 `200` | lincy 實測 | 高 | 2026-05-12，錯誤訊息為 `The reasoning_content in the thinking mode must be passed back to the API.` |

---

## Anthropic

### 1. 官方 API 事實

| 項目 | 事實 | 來源類型 | 來源連結 | 可信度 | 模型/版本相關 |
|------|------|---------|---------|--------|-------------|
| Endpoint | `POST /v1/messages` | 官方文件 | [Messages API](https://platform.claude.com/docs/en/api/messages) | 高 | 否 |
| Auth | `Authorization: Bearer {api_key}` 或 `x-api-key: {api_key}`，加 `anthropic-version: 2023-06-01` | 官方文件 | 同上 | 高 | 否 |
| max_tokens | **必填** | 官方文件 | 同上 | 高 | 否 |
| temperature | 可選，default 1.0 | 官方文件 | 同上 | 高 | 否 |
| Tools | `{name, description, input_schema: {type, properties, required}}`，**非** OpenAI function calling | 官方文件 | 同上 | 高 | 否 |
| Vision image source | `base64` 和 `url` 兩種 source type | 官方範例 | [Messages Examples](https://platform.claude.com/docs/en/api/messages-examples)，Vision 段落 Option 1 (base64) + Option 2 (url) | 高 | 否 |
| Vision media types | `image/jpeg`, `image/png`, `image/gif`, `image/webp` | 官方文件 | [Messages API](https://platform.claude.com/docs/en/api/messages) | 高 | 否 |
| **Extended thinking（手動）** | `thinking: {"type": "enabled", "budget_tokens": N}`，budget_tokens >= 1024 | 官方文件 | [Extended Thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking) | 高 | 是（見下方） |
| **Adaptive thinking** | `thinking: {"type": "adaptive"}` | 官方文件 | [Adaptive Thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking) | 高 | 是（僅 Opus 4.6, Sonnet 4.6） |
| **Effort 參數** | `output_config: {"effort": "low"\|"medium"\|"high"\|"max"}`，**獨立於 thinking**，影響所有 token；Sonnet 5 / Opus 5 另支援 `xhigh` | 官方文件 | [Effort](https://platform.claude.com/docs/en/build-with-claude/effort) | 高 | 是 |
| Effort `max` | 在 4.x 世代僅 Opus 4.6；Sonnet 5 / Opus 5 亦支援 | Anthropic 官方文件 | [Effort](https://platform.claude.com/docs/en/build-with-claude/effort) | 高 | 是 |
| **Opus 4.6 deprecation** | `thinking: {"type": "enabled", "budget_tokens": N}` 在 Opus 4.6 和 Sonnet 4.6 上 deprecated | 官方文件 | [Adaptive Thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking) Warning box 原文："`thinking.type: 'enabled'` and `budget_tokens` are **deprecated** on Opus 4.6 and Sonnet 4.6" | 高 | 是 |
| 舊模型 | Sonnet 4.5, Opus 4.5, Sonnet 4, Haiku 4.5 等僅支援 `thinking: {"type": "enabled", "budget_tokens": N}` | 官方文件 | [Extended Thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking) | 高 | 是 |
| Sonnet 5 thinking 預設 | Claude Sonnet 5（`claude-sonnet-5`）adaptive thinking 預設開啟；顯式送 `thinking.type=adaptive` 可保留同一語意，`thinking.type=enabled` + `budget_tokens` 回 400；需顯式送 `thinking.type=disabled` 才能關閉 | Anthropic 官方文件 | [Adaptive Thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking) | 高 | — |
| Sonnet 5 effort 支援值 | 官方支援 `low/medium/high/xhigh/max` | Anthropic 官方文件 | [Effort](https://platform.claude.com/docs/en/build-with-claude/effort) | 高 | — |
| Opus 5 thinking 預設 | Claude Opus 5（`claude-opus-5`）adaptive thinking 預設開啟；`thinking.type=enabled` + `budget_tokens` 回 400；需顯式送 `thinking.type=disabled` 才能關閉 | Anthropic 官方文件 | [Adaptive Thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking) | 高 | — |
| Opus 5 disabled thinking 的 effort 上限 | `thinking.type=disabled` 只在 effort `high` 以下被接受，配 `xhigh`/`max` 回 400 | Anthropic 官方文件 | [Effort](https://platform.claude.com/docs/en/build-with-claude/effort) | 高 | — |
| Opus 5 effort 支援值 | 官方支援 `low/medium/high/xhigh/max`；非預設 `temperature`/`top_p`/`top_k` 可能回 400 | Anthropic 官方文件 | [Effort](https://platform.claude.com/docs/en/build-with-claude/effort) | 高 | — |

### 2. 本專案 adapter 規則

| 項目 | 規則 | 程式碼位置 | 備註 |
|------|------|-----------|------|
| Thinking payload | YAML 直接使用 Anthropic `thinking` 物件：`type=adaptive|enabled|disabled`；`enabled` 時可選 `budget_tokens`，且最低 1024 | `src/lincy/core/schema.py` + `src/lincy/llm/providers/anthropic.py` | 本專案 adapter 遵循 Messages API native shape |
| Effort payload | YAML 直接使用 `output_config.effort`；client 原樣 passthrough 成 upstream `output_config` | `src/lincy/core/schema.py` + `src/lincy/llm/providers/anthropic.py` | `low/medium/high/xhigh/max` |
| Effort beta header | request 有 `output_config.effort` 時送 `anthropic-beta: effort-2025-11-24` | `src/lincy/llm/providers/anthropic.py` | 與 Claude Code proxy 的動態 beta 規則一致 |
| Effort x model 相容性 | config 載入時早停驗證；只列文件已知有限制的 family，未知/未來 model id 放行 | `src/lincy/core/schema.py` | 不在 client runtime 降級 |
| Opus 5 disabled thinking 驗證 | `thinking.type=disabled` + `effort` `xhigh`/`max` 在載入時直接報錯 | `src/lincy/core/schema.py` | 對齊官方 400 限制 |
| Vision | YAML 使用 plain `vision: true` | `src/lincy/core/schema.py` | 與 Claude Code provider 一致 |
| `provider_overrides` | Anthropic config 不提供 override escape hatch；native fields 已可直接表達 payload | `src/lincy/core/schema.py` + `src/lincy/llm/providers/anthropic.py` | 舊 `anthropic_thinking*` override 已移除 |

### 3. 逆向/實測資訊

無。

---

## Heyroute

> **重要標示**：heyroute.ai 的實際 API 文件目前不可得。本節只把使用者提供的「Anthropic Messages API compatible gateway」建模為**假設相容、尚未驗證**，不把 gateway 的模型、限制或額外欄位寫成已知事實。

### 1. 假設的 Anthropic-compatible 介面（尚未驗證）

| 項目 | 事實 | 來源類型 | 可信度 | 備註 |
|------|------|---------|--------|------|
| Endpoint | `POST /v1/messages` | 假設 Anthropic-compatible / 未驗證 | 低 | base URL 由使用者指定為 `https://heyroute.ai/`；client 依既有 Anthropic joining convention 附加 `/v1/messages` |
| Auth | `x-api-key: {api_key}` + `anthropic-version: 2023-06-01` | 假設 Anthropic-compatible / 未驗證 | 低 | 未宣稱 heyroute.ai 已公開保證此 header 行為 |
| Request / response shape | Anthropic Messages API native shape | 假設 Anthropic-compatible / 未驗證 | 低 | 不新增 gateway-specific 欄位 |
| Thinking | `thinking: {"type": "adaptive"|"enabled"|"disabled"}`；enabled 可帶 `budget_tokens` | 假設 Anthropic-compatible / 未驗證 | 低 | 完全沿用本專案 Anthropic adapter shape |
| Effort | `output_config: {"effort": "low"|"medium"|"high"|"xhigh"|"max"}` | 假設 Anthropic-compatible / 未驗證 | 低 | 不推測 heyroute 的額外 effort 值或模型能力 |
| Effort beta header | `anthropic-beta: effort-2025-11-24` | 假設 Anthropic-compatible / 未驗證 | 低 | 只在 request 有 output_config.effort 時送出 |

### 2. 本專案 adapter 規則

| 項目 | 規則 | 程式碼位置 | 備註 |
|------|------|---------|------|
| Provider 名稱 | 使用獨立 `provider: heyroute` 與 `HeyrouteConfig` / `HeyrouteClient` | `src/lincy/core/schema.py` + `src/lincy/llm/providers/heyroute.py` | Heyroute 是獨立 gateway 路徑；不把 `anthropic` config 改成多個 API 形狀 |
| Base URL | 預設 `https://heyroute.ai/`，config validator 會去除尾端 `/`，client 再附加 `/v1/messages` | `src/lincy/core/schema.py` + `src/lincy/llm/providers/anthropic.py` | 實際 request URL 為 `https://heyroute.ai/v1/messages`，不會有 double slash |
| API key env | `HEYROUTE_API_KEY` | `src/lincy/core/schema.py` + `src/lincy/core/config.py` | 依既有 provider 的 `api_key_env` 解析規則 |
| Payload / response | Heyroute client 重用 Anthropic Messages adapter，不複製 payload 與 response mapping | `src/lincy/llm/providers/heyroute.py` | 只重用已確認的本專案 Anthropic-compatible shape |
| Tool result 合併 | 連續的 `role: "tool"` message 會合併成單一 user message 的多個 `tool_result` block；遇到 assistant / user message 才開新的 | `src/lincy/llm/providers/anthropic.py` | 這是 Anthropic 平行 tool use 的正規形狀，同時是 heyroute 的硬性要求 |
| Temperature | thinking 為 active 時省略；disabled 或未設定 thinking 時照既有 Anthropic 規則送出 | `src/lincy/llm/providers/anthropic.py` | gateway 行為未獨立驗證 |
| Prompt cache breakpoints | 視為 Anthropic-style breakpoint provider | `src/lincy/context/cache_breakpoints.py` | 僅因 request shape 已明確與 Anthropic adapter 相同而納入 |

### 3. 實測 / 逆向資訊

以 2026-08-09 實際 request 對 `https://heyroute.ai/v1/messages` 逐項 bisect（重放真實失敗的 brain request，Claude-Max20x key）：

| 項目 | 實測結果 | 備註 |
|------|---------|------|
| 平行 tool_use 的 tool_result 擺放 | **必須把同一個 assistant turn 的所有 `tool_result` 放在同一個 user message**；拆成多個 user message 一律 `HTTP 400 invalid_request` | gateway 比 api.anthropic.com 嚴格：官方端點會自行合併連續 user turn，所以拆開也能過 |
| 連續同 role message（純 text） | 可接受，不會 400 | 400 只由拆開的 tool_result 觸發 |
| 錯誤格式 | `{"error": {"type": "invalid_request", "message": "请求无效，请检查请求参数。 (code: invalid_request, request id: ...)"}, "type": "error"}` | 訊息不指出違規欄位，只能靠 bisect 定位 |
| Endpoint / auth / thinking / effort / cache_control | 皆與 Anthropic native shape 相容（`thinking: adaptive`、`output_config.effort: xhigh`、`cache_control` 含 `ttl: 1h` 均回 200，且 usage 有 `ephemeral_1h_input_tokens`） | 原「假設未驗證」欄位已實測通過 |
| max_tokens | 128000 可接受 | |
| 模型 entitlement | key 逐一白名單；`claude-opus-5[1M]` / `claude-sonnet-5[1M]` 回 `HTTP 403 token_model_forbidden`，允許清單為 `claude-fable-5, claude-haiku-4-5, claude-opus-5, claude-sonnet-5` | `[1M]` 後綴視為獨立模型名，非 context 旗標 |
| Temperature | `temperature != 1` 在 claude-5 系列（fable-5 / sonnet-5）回 400；`claude-sonnet-4-6` 不受限 | 與 Anthropic「thinking active 時 temperature 必須為 1」一致；本專案 adapter 在 thinking active 時已省略 temperature，故不受影響 |

---

## Kano Proxy

> **重要標示**：kano-proxy 沒有獨立公開 API 文件。本節只把使用者指定的「Anthropic Messages API compatible gateway」建模為**假設相容、尚未驗證**。不把 gateway 的模型對應、限制或額外欄位寫成已知事實。

### 1. 假設的 Anthropic-compatible 介面（尚未驗證）

| 項目 | 事實 | 來源類型 | 可信度 | 備註 |
|------|------|---------|--------|------|
| Endpoint | `POST /v1/messages` | 假設 Anthropic-compatible / 未驗證 | 低 | 使用者指定 gateway root 為 `https://kano-proxy.yuufeng.com/anthropic`；client 依既有 Anthropic joining convention 附加 `/v1/messages` |
| Auth | `x-api-key: {api_key}` + `anthropic-version: 2023-06-01` | 假設 Anthropic-compatible / 未驗證 | 低 | 與 Anthropic/Heyroute adapter 相同；key 必須走獨立 env，不可共用 `ANTHROPIC_API_KEY` |
| Request / response shape | Anthropic Messages API native shape | 假設 Anthropic-compatible / 未驗證 | 低 | 不新增 gateway-specific 欄位 |
| Thinking | `thinking: {"type": "adaptive"|"enabled"|"disabled"}`；enabled 可帶 `budget_tokens` | 假設 Anthropic-compatible / 未驗證 | 低 | 完全沿用本專案 Anthropic adapter shape |
| Effort | `output_config: {"effort": "low"|"medium"|"high"|"xhigh"|"max"}` | 假設 Anthropic-compatible / 未驗證 | 低 | 不推測 kano-proxy 的額外 effort 值或模型能力 |

### 2. 本專案 adapter 規則

| 項目 | 規則 | 程式碼位置 | 備註 |
|------|------|---------|------|
| Provider 名稱 | 使用獨立 `provider: kano_proxy` 與 `KanoProxyConfig` / `KanoProxyClient` | `src/lincy/core/schema.py` + `src/lincy/llm/providers/kano_proxy.py` | 獨立 gateway 路徑；不把 `anthropic` config 改成多個 API 形狀 |
| Client 實作 | `KanoProxyClient` 繼承 `AnthropicClient`，不複製 payload / response mapping | `src/lincy/llm/providers/kano_proxy.py` | 與 Heyroute 同一模式 |
| Base URL | 預設 `https://kano-proxy.yuufeng.com/anthropic`，config validator 去除尾端 `/`，client 再附加 `/v1/messages` | `src/lincy/core/schema.py` + `src/lincy/llm/providers/anthropic.py` | 實際 request URL 為 `https://kano-proxy.yuufeng.com/anthropic/v1/messages` |
| API key env | 預設 `KANO_PROXY_API_KEY` | `src/lincy/core/schema.py` + `src/lincy/core/config.py` | 依既有 `api_key_env` 解析規則；不可 fallback 到 `ANTHROPIC_API_KEY` |
| Prompt cache breakpoints | 視為 Anthropic-style breakpoint provider | `src/lincy/context/cache_breakpoints.py` | 僅因 request shape 與 Anthropic adapter 相同而納入 |

### 3. 實測 / 逆向資訊

無。repo 內 curated profiles 使用 gateway 自訂 model id（`lincy-brain-agent`、`lincy-worker-agent`、`lincy-gui-manager`），不是 Anthropic 官方 model id。

---

## Gemini

### 1. 官方 API 事實

| 項目 | 事實 | 來源類型 | 來源連結 | 可信度 | 模型/版本相關 |
|------|------|---------|---------|--------|-------------|
| Endpoint | `generateContent`（REST） | 官方文件 | [Gemini Thinking](https://ai.google.dev/gemini-api/docs/thinking) | 高 | 否 |
| Auth | `x-goog-api-key` header 或 `key=` query parameter | 官方文件 | 同上（範例中兩種都有） | 高 | 否 |
| Tools | `functionDeclarations` format：`{name, description, parameters: {type, properties, required}}`。Response function call 用 `args`（非 OpenAI 的 `arguments` JSON string）。Result 用 `functionResponse: {name, response: {...}}`（非 OpenAI 的 `role: "tool"`） | 官方文件 | [Gemini Function Calling](https://ai.google.dev/gemini-api/docs/function-calling)，declaration 結構 + response `args` 欄位 + result `functionResponse` 欄位 | 高 | 否 |
| Vision | `inlineData` parts（base64） | 官方文件 | 同上 | 高 | 否 |
| max_tokens | `generationConfig.maxOutputTokens` | 官方文件 | 同上 | 高 | 否 |
| **thinkingLevel（Gemini 3）** | `thinkingConfig: {"thinkingLevel": "minimal"\|"low"\|"medium"\|"high"}` | 官方文件 | [Gemini Thinking](https://ai.google.dev/gemini-api/docs/thinking) | 高 | 是 |
| thinkingLevel 支援矩陣 | 3.1 Pro: low/medium/high；3 Pro: low/high（無 medium/minimal）；3 Flash: minimal/low/medium/high | 官方文件 | 同上，ThinkingLevel 表格 | 高 | 是 |
| Gemini 3 Pro 不能關 thinking | 原文："You cannot disable thinking for Gemini 3 Pro." | 官方文件 | 同上 | 高 | 是 |
| Default thinkingLevel | `high`（所有 Gemini 3） | 官方文件 | 同上 | 高 | 是 |
| **thinkingBudget（Gemini 2.5）** | `thinkingConfig: {"thinkingBudget": N}`，0=關閉，-1=動態 | 官方文件 | 同上 | 高 | 是 |
| thinkingBudget 範圍 | 2.5 Pro: 128-32768；2.5 Flash: 0-24576；2.5 Flash Lite: 512-24576 | 官方文件 | 同上 | 高 | 是 |
| thinkingBudget 在 Gemini 3 | 向後相容接受，官方警告 "may result in unexpected performance" | 官方文件 | 同上 | 高 | 是 |

### 2. 本專案 adapter 規則

| 項目 | 規則 | 程式碼位置 | 備註 |
|------|------|-----------|------|
| `_EFFORT_TO_LEVEL` | `low->LOW`, `medium->MEDIUM`, `high->HIGH` | `src/lincy/llm/providers/gemini.py` | 官方 API 值是小寫，SDK 用大寫 |
| `thinkingBudget` 設定 | `reasoning.max_tokens` -> `thinkingBudget` | `src/lincy/llm/providers/gemini.py` | 直接映射 |
| `enabled=True` 無 budget | 預設 `thinkingBudget: 1024` | `src/lincy/llm/providers/gemini.py` | 本專案 fallback |
| `enabled=False` | 設 `thinkingBudget: 0` | `src/lincy/llm/providers/gemini.py` | `GeminiConfig` 會在 config 載入時拒絕 Gemini 3 Pro，避免 runtime request error |
| 不支援 `minimal` | mapping 只有 low/medium/high | `src/lincy/llm/providers/gemini.py` | `GeminiConfig` 會在 config 載入時拒絕 minimal，避免 runtime KeyError |
| `provider_overrides` | `gemini_thinking_config` 整體覆蓋 | `src/lincy/llm/providers/gemini.py` | 本專案 escape hatch |

### 3. 逆向/實測資訊

無。

---

## OpenRouter

### 1. 官方 API 事實

| 項目 | 事實 | 來源類型 | 來源連結 | 可信度 | 模型/版本相關 |
|------|------|---------|---------|--------|-------------|
| Endpoint | `POST https://openrouter.ai/api/v1/chat/completions` | 官方文件 | [API Overview](https://openrouter.ai/docs/api/reference/overview) | 高 | 否 |
| Auth | `Authorization: Bearer {api_key}` | 官方文件 | 同上 | 高 | 否 |
| Optional headers | `HTTP-Referer`，`X-OpenRouter-Title`（alias `X-Title`） | 官方文件 | 同上 | 高 | 否 |
| Request 格式 | OpenAI Chat Completions 相容 | 官方文件 | 同上 | 高 | 否 |
| **Reasoning effort** | `reasoning: {"effort": "none"\|"minimal"\|"low"\|"medium"\|"high"\|"xhigh"}` | 官方文件 | [Reasoning Tokens](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)，effort levels 表格 | 高 | 否 |
| Reasoning max_tokens | `reasoning: {"max_tokens": N}`，最小 1024 | 官方文件 | 同上 | 高 | 依底層 provider |
| Reasoning exclude | `reasoning: {"exclude": true}` | 官方文件 | 同上 | 高 | 否 |
| Reasoning enabled | `reasoning: {"enabled": true}` — medium effort | 官方文件 | 同上 | 高 | 否 |
| **Verbosity** | `verbosity: "low"\|"medium"\|"high"\|"max"`；Anthropic 路由會映射到 `output_config.effort` | 官方文件 | [API Parameters](https://openrouter.ai/docs/api/reference/parameters) | 高 | `max` 支援度依模型而異 |
| Precedence | effort + max_tokens 互斥（"One of the following, not both"） | 官方文件 | 同上 | 高 | — |
| Provider routing | `provider: {"order": [...], "allow_fallbacks": bool}` | 官方文件 | [Provider Routing](https://openrouter.ai/docs/guides/routing/provider-selection) | 高 | 依模型可用 endpoint |
| Tools | OpenAI function calling format | 官方文件 | [API Overview](https://openrouter.ai/docs/api/reference/overview) | 高 | 否 |
| Prompt caching | `cache_control: {"type": "ephemeral", "ttl": "1h"}` on content parts | 官方文件 | [Prompt Caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching) | 高 | Claude 專用 TTL |
| Provider sticky routing | Cache hit 後自動路由到相同 provider endpoint | 官方文件 | 同上 | 高 | 否 |
| Claude 4.6 adaptive thinking | `reasoning: {"enabled": true}` 會走 adaptive thinking；`reasoning.max_tokens` 才切回 budget-based thinking | 官方文件 | [Claude 4.6 Migration Guide](https://openrouter.ai/docs/guides/guides/model-migrations/claude-4-6) | 高 | 僅 Claude Opus 4.6 / Sonnet 4.6 |

### 2. 本專案 adapter 規則

| 項目 | 規則 | 程式碼位置 | 備註 |
|------|------|-----------|------|
| effort / max_tokens 互斥 | config 層驗證，同時設定 → ValueError | `src/lincy/core/schema.py`（`OpenRouterConfig.validate_reasoning()`） | 符合官方 API 限制 |
| `enabled=False` -> `{"effort": "none"}` | 映射 | `src/lincy/llm/providers/openrouter.py` | 符合官方語意 |
| `enabled=True` 單獨保留 | 只設 `enabled=true` 時送 `{"enabled": true}` | `src/lincy/llm/providers/openrouter.py` | 讓 Claude 4.6 可顯式走 adaptive thinking |
| `verbosity` passthrough | YAML `verbosity` 由 `OpenRouterClient` 在 provider 層補到 OpenRouter 頂層 `verbosity` | `src/lincy/core/schema.py` + `src/lincy/llm/providers/openrouter.py` | Anthropic 路由會再映射到 `output_config.effort` |
| `provider_routing` payload | YAML `provider_routing` 映射到 request `provider` object；`null` 時不送 `provider`（走 OpenRouter 預設路由） | `src/lincy/core/schema.py` + `src/lincy/llm/providers/openrouter.py` + `src/lincy/llm/providers/openai_compat.py` | 允許各 profile 個別固定 endpoint 或回到預設 |
| Header 名稱 | 同時送 `X-OpenRouter-Title` + `X-Title` | `openrouter.py` | 官方 header + alias 相容 |
| 連線參數 self-contained | `api_key_env`/`base_url`/`site_url` 在每個 LLM YAML；`site_name` null 時 fallback 到 agent name；`site_url` 在 `load_config()` 自動附加 `/{agent_name}`（可用 `agents.*.openrouter.site_url` 覆蓋） | `src/lincy/core/config.py`（`load_config()`） | YAML 可獨立使用（validate_llm.py 等） |
| Cache breakpoint 注入 | `ContextBuilder` BP1 (system prompt) + BP2 (boot files)，`cache_control` passthrough via `_convert_content_parts()`；所有 per-turn dynamic note（`current_local_time` / `[Timing Notice]` / message-time common ground）必須留在 latest turn，不得新增 system-tier message；僅 OpenRouter provider 啟用 | `src/lincy/context/builder.py` + `src/lincy/agent/responder.py` + `openai_compat.py` + `cli/app.py` | 成本最佳化：1h TTL for heartbeat；重建同一輪長 prompt 時，cache hit 應維持 >90% |

### 3. 逆向/實測資訊

無。

---

## Ollama

### 1. 官方 API 事實

| 項目 | 事實 | 來源類型 | 來源連結 | 可信度 | 模型/版本相關 |
|------|------|---------|---------|--------|-------------|
| Endpoint | `POST /api/chat`（native API） | 官方文件 | [Chat API](https://docs.ollama.com/api/chat) | 高 | 否 |
| Auth（本機 daemon） | 無 | 官方文件 | [Authentication](https://docs.ollama.com/api/authentication) | 高 | 否 |
| Cloud 模型經本機 daemon | 同一個本機 API 可直接 offload 到 Ollama cloud；官方範例使用 `:cloud` model tags | 官方文件 | [Cloud](https://docs.ollama.com/cloud) | 高 | 是 |
| Native tools | native `/api/chat` 支援 `tools` | 官方文件 | [Tool Calling](https://docs.ollama.com/capabilities/tool-calling) | 高 | 否 |
| Native structured outputs | native `/api/chat` 支援 `format`（JSON schema） | 官方文件 | [Structured Outputs](https://docs.ollama.com/capabilities/structured-outputs) | 高 | 否 |
| Native vision | native `/api/chat` 的 message 支援 `images` | 官方文件 | [Vision](https://docs.ollama.com/capabilities/vision) | 高 | 是（vision models） |
| Native runtime options | native `/api/chat` 支援 `options`；`temperature`、`num_predict` 為官方 runtime parameters | 官方文件 | [Chat API](https://docs.ollama.com/api/chat) + [Modelfile](https://docs.ollama.com/modelfile) | 高 | 否 |
| **Native thinking** | `think` 參數（boolean 或 level string），在 native Ollama API | 官方文件 | [Thinking](https://docs.ollama.com/capabilities/thinking) + [Chat API](https://docs.ollama.com/api/chat) | 高 | 是 |
| `think` 值 | boolean `true`/`false`；本專案允許 passthrough `"low"`/`"medium"`/`"high"`/`"xhigh"`/`"max"`；公開文件明列 low/medium/high，DeepSeek V4 Flash Cloud 實測另接受 `"max"` | 官方文件 + 本機實測 | 同上 | 高（low/medium/high）/ 中（max）/ 低（xhigh） | 是 |
| Thinking 預設 | 支援 thinking 的模型預設啟用 | 官方文件 | 同上 | 高 | 是 |
| Thinking response | `message.thinking`（reasoning）+ `message.content`（answer） | 官方文件 | 同上 | 高 | 否 |
| Native usage 欄位 | non-streaming response 會回 `prompt_eval_count` / `eval_count` | 官方文件 | [Chat API](https://docs.ollama.com/api/chat) | 高 | 否 |
| DeepSeek V4 thinking modes | DeepSeek 官方模型卡列出 Non-think / Think High / Think Max；Think Max 依官方 encoding 需要特殊 prefix 與 384K context | 官方模型卡 / encoding | [DeepSeek V4 README](https://huggingface.co/deepseek-ai/DeepSeek-V4-Pro/blob/main/README.md) + [encoding_dsv4.py](https://huggingface.co/deepseek-ai/DeepSeek-V4-Pro/blob/main/encoding/encoding_dsv4.py) | 高 | 是 |

### 2. 本專案 adapter 規則

| 項目 | 規則 | 程式碼位置 | 備註 |
|------|------|-----------|------|
| 單一路徑 | 本專案 `ollama` provider 只走 native `/api/chat`，不混用 OpenAI-compat | `src/lincy/llm/providers/ollama_native.py` | 單一 concrete client 對應單一 API format |
| thinking YAML | 使用 `thinking.mode=toggle|effort`；toggle 映射到 `think: true/false`，effort 映射到 `think: "low"|"medium"|"high"|"xhigh"|"max"` | `src/lincy/core/schema.py` + `src/lincy/llm/providers/ollama_native.py` | provider-specific config，不做假統一 |
| level 驗證 | `thinking.mode=effort` 的 effort 值允許 low/medium/high/xhigh/max 並原樣送出；`gpt-oss:*` 仍要求使用 effort mode，不用 toggle | `src/lincy/core/schema.py` | 值集合依本專案設定需求放寬；上游若不支援會回 request error |
| `max_tokens` 映射 | YAML `max_tokens` -> native `options.num_predict` | `src/lincy/llm/providers/ollama_native.py` | 本專案統一輸出 token cap 口徑 |
| `temperature` 映射 | YAML `temperature` -> native `options.temperature` | `src/lincy/llm/providers/ollama_native.py` | native 欄位名與 OpenAI compat 不同 |
| `response_schema` 映射 | `chat(..., response_schema=...)` -> native `format` JSON schema | `src/lincy/llm/providers/ollama_native.py` | 對齊 Ollama structured outputs |
| real tool-loop metadata round-trip | **真實 provider 回傳**的 assistant tool history 必須保留原始 `tool_calls[]` metadata；本專案在 unified `ToolCall` 上保存 `provider_roundtrip`，並 round-trip 回送至少 `id`、`function.index`、`thoughtSignature` 與其他未知欄位 | `src/lincy/llm/providers/ollama_native.py` + `src/lincy/llm/schema.py` | 維持 native `/api/chat` round-trip fidelity；不要再手動挑少數欄位 |
| synthetic tool history textification | `read_startup_context`、`_stage1_gather`、`_load_skill_prerequisite`、`_load_common_ground_at_message_time` 等 runtime synthetic tool pair 在 Ollama adapter 出口改寫成普通 `system` 文字訊息，不再以 native function-call history 送出 | `src/lincy/llm/providers/ollama_native.py` | 這些訊息是內部上下文注入，不是真實 provider tool loop；Gemini-backed Ollama 會對其做更嚴格驗證而回 `400` |
| real tool-loop thinking replay guard | 若**真實** assistant tool history 含 `thinking`，但對應 `tool_calls[]` 缺 `thoughtSignature`，adapter 會在 replay 時省略 `thinking`、只保留 tool_calls；否則 Gemini-backed Ollama 會回 `400` `"Function call is missing a thought_signature..."` | `src/lincy/llm/providers/ollama_native.py` | 實測確認上游可能回傳無 `thoughtSignature` 的真 tool call；此 guard 僅在 metadata 已壞掉時啟用 |
| token usage 回收 | `prompt_eval_count` / `eval_count` -> `LLMResponse.prompt_tokens` / `completion_tokens` / `total_tokens` | `src/lincy/llm/providers/ollama_native.py` | 供 soft limit / status bar 使用 |
| cloud profile 命名 | repo 內 curated profiles 一律使用 cloud-only 目錄命名與 cloud model ids | `cfgs/llm/ollama/` | 減少本地模型與 cloud 模型語意混淆 |
| API key 支援（cloud direct） | `api_key` / `api_key_env` 設定時，adapter 送 `Authorization: Bearer <key>`；未設定時不送 auth header（本機 daemon 預設） | `src/lincy/core/schema.py` + `src/lincy/core/config.py` + `src/lincy/llm/providers/ollama_native.py` | 讓同一個 provider 可接本機 daemon 或 `https://ollama.com` |

### 3. 逆向/實測資訊

| 項目 | 事實 | 來源類型 | 可信度 | 備註 |
|------|------|---------|--------|------|
| `ollama show` cloud capabilities（本專案當前 profile 集） | `kimi-k2.5:cloud`、`gemini-3-flash-preview` 具 vision；`glm-4.7:cloud`、`glm-5:cloud`、`gpt-oss:20b-cloud`、`minimax-m2.5:cloud` 不具 vision | 本機 `ollama show` 實測 | 中 | 用於 curated YAML 註解，不是通用 API 保證 |
| `deepseek-v4-flash:cloud` `think=max` | `POST https://ollama.com/api/chat` 實測 `think: "max"` 回 200；相同短 prompt 下 `high` 為 `prompt_eval_count=13`、`thinking_chars=74`，`max` 為 `prompt_eval_count=92`、`thinking_chars=220` | 本機實測 | 中 | 2026-04-24；表示 Ollama Cloud 有處理 `max`，但 public docs 尚未正式列入通用 level |

---

## 差異總結表

| 項目 | Copilot | Claude Code | Grok (OAuth proxy) | OpenAI | Anthropic | Heyroute | Gemini | OpenRouter | Ollama |
|------|---------|-------------|-------------------|--------|-----------|----------|--------|------------|--------|
| Endpoint | OpenAI compat（歷史/實測） | `/v1/messages`（Anthropic schema） | OpenAI compat via local proxy | Chat Completions | `/v1/messages` | `/v1/messages`（假設相容/未驗證） | `generateContent` | OpenAI compat | native `/api/chat` |
| Reasoning 參數 | `reasoning_effort`（頂層，逆向/實測） | `thinking.type` + `output_config.effort` | `reasoning_effort`（頂層） | `reasoning_effort`（頂層） | `thinking.type` + `output_config.effort` | `thinking.type` + `output_config.effort`（假設相容/未驗證） | `thinkingConfig` | `reasoning: {"effort":...}` | `think`（native） |
| Effort 值 | low/medium/high/xhigh（curated profiles；backend 逆向） | low/medium/high/max（`output_config.effort`） | none/low/medium/high/xhigh（model-dependent） | low/medium/high/xhigh/max（adapter passthrough；官方另列 none） | low/medium/high/max（output_config） | low/medium/high/xhigh/max（假設相容/未驗證） | minimal/low/medium/high（依模型） | none/minimal/low/medium/high/xhigh | low/medium/high/xhigh/max（adapter passthrough） |
| Token budget | 無 | `thinking.budget_tokens` | 無 | 無 | `thinking.budget_tokens` | `thinking.budget_tokens`（假設相容/未驗證） | `thinkingBudget` | `reasoning.max_tokens` | 無 |
| Vision | `image_url`（實測） | `image` block（base64） | `image_url` | `image_url` | `image` block（base64/url） | `image` block（假設相容/未驗證） | `inlineData`（base64） | `image_url` | 依模型 |
| Tools | OpenAI function（實測） | Anthropic `input_schema` | OpenAI function | OpenAI function | Anthropic `input_schema` | Anthropic `input_schema`（假設相容/未驗證） | Gemini `functionDeclarations` | OpenAI function | native `tools` |
| Auth | proxy 處理（逆向） | proxy 處理（Claude Code OAuth bearer，逆向） | proxy 處理（SuperGrok OAuth） | Bearer token | Bearer/x-api-key + version | x-api-key + version（假設相容/未驗證） | API key (header/query) | Bearer token | 本機 daemon 無 |
| max_tokens | 不需要（實測） | **必填** | 可選 | 可選（GPT-5+ 用 `max_completion_tokens`） | **必填** | **必填**（假設相容/未驗證） | 可選（maxOutputTokens） | 可選 | `options.num_predict` |
| Prompt cache | 無 | Anthropic breakpoint | 自動 prefix + `x-grok-conv-id` sticky | 自動 prefix（`prompt_cache_retention: "24h"`） | Anthropic breakpoint | Anthropic breakpoint（假設相容/未驗證） | 無 | `cache_control` breakpoint | 無 |

Kano Proxy 不另開欄：獨立 `provider: kano_proxy`，payload / auth header / cache breakpoint 與 Anthropic、Heyroute 同一 adapter；差異只有 gateway URL 與 `KANO_PROXY_API_KEY`。

---

## Usage Token 回收（non-streaming）

本節描述本專案 runtime 對「回應 usage 欄位」的統一回收規則。

| Provider | API 是否可能回 usage | Adapter 是否回收 prompt/completion/total | Adapter 是否回收 cache read/write | 缺值策略 |
|---|---|---|---|---|
| OpenAI / OpenRouter / Copilot / Grok（OpenAI-compatible） | 是（視 gateway/模型） | 是 | 是（若有 prompt_tokens_details） | `usage=None` 時標記 unavailable |
| Ollama（native `/api/chat`） | 是（`prompt_eval_count` / `eval_count`） | 是 | 否 | 欄位缺失時標記 unavailable |
| Anthropic / Heyroute / Kano Proxy | 是 | 是（prompt = input + cache_read + cache_creation；completion = output） | 是（cache_read_input_tokens / cache_creation_input_tokens） | `usage` 缺失時標記 unavailable |
| Gemini | 是（usageMetadata） | 是（promptTokenCount / candidatesTokenCount / totalTokenCount） | 否 | `usageMetadata` 缺失時標記 unavailable |

補充：
- 本專案目前只看 non-streaming 回應，不使用 streaming usage。
- Copilot 在某些情況可能不回 usage；runtime 顯示 unavailable，不做估算。

---

## 修正清單（共 7 點有效 + 1 點撤回）

對照初版 A 表的修正紀錄。

| # | 原版敘述 | 修正 | 依據 | 狀態 |
|---|---------|------|------|------|
| 1 | Anthropic API 不認 effort | Anthropic 有 `output_config.effort`（low/medium/high/max），Opus 4.6 上 budget_tokens deprecated | [Effort](https://platform.claude.com/docs/en/build-with-claude/effort) + [Adaptive Thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking) | 有效 |
| 2 | Anthropic vision 只有 base64 | 支援 base64 和 url 兩種 source type | [Messages Examples](https://platform.claude.com/docs/en/api/messages-examples) Option 1 + Option 2 | 有效 |
| 3 | Gemini effort 只支援 low/high | 依模型：3 Pro 是 low/high；3 Flash 是 minimal/low/medium/high；3.1 Pro 是 low/medium/high | [Gemini Thinking](https://ai.google.dev/gemini-api/docs/thinking) ThinkingLevel 表格 | 有效 |
| 4 | Ollama 用 reasoning_effort | 無官方依據。Thinking 是 native `think` 參數 | [OpenAI Compatibility](https://docs.ollama.com/api/openai-compatibility) + [Thinking](https://docs.ollama.com/capabilities/thinking) | 有效 |
| 5 | OpenAI reasoning_effort 是頂層欄位（曾修正為「改成 reasoning object」） | **撤回修正**。Chat Completions API 仍用 `reasoning_effort` 頂層欄位。`reasoning` object 是 Responses API 格式。本專案用 Chat Completions，現行做法正確 | [GPT-5.2 Guide](https://developers.openai.com/api/docs/guides/latest-model/) 原文："Chat Completions API uses: `reasoning_effort`" | 撤回 |
| 6 | OpenAI enabled=false 需要 override | 非 API 事實，是本專案 `OpenAIConfig.validate_reasoning()` 規則 | `src/lincy/core/schema.py` | 有效 |
| 7 | Gemini auth 只有 URL parameter | 也支援 `x-goog-api-key` header | [Gemini Thinking](https://ai.google.dev/gemini-api/docs/thinking) 範例 | 有效 |
| 8 | OpenRouter effort + max_tokens 時 effort 優先 | 非官方保證，本專案自定 precedence | [Reasoning Tokens](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens) | 有效 |
