# LLM Provider API 規格盤點

本文件記錄各 LLM provider 的 API 事實、本專案 adapter 規則、實測/逆向資訊。
作為 LLM config/client 設計的依據。

架構邊界與設計準則見 `docs/dev/provider-architecture.md`。

每個 provider 分三段：
1. **官方 API 事實**（有來源連結、可信度標註）
2. **本專案 adapter 規則**（非 API 事實，是本專案的映射/驗證邏輯）
3. **實測/逆向資訊**（無官方保證的內容）

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
| Effort beta header | request 有 `output_config.effort` 時送 `anthropic-beta: effort-2025-11-24` | `src/lincy/llm/providers/anthropic.py` | 依 effort 動態附掛 beta header |
| Effort x model 相容性 | config 載入時早停驗證；只列文件已知有限制的 family，未知/未來 model id 放行 | `src/lincy/core/schema.py` | 不在 client runtime 降級 |
| Opus 5 disabled thinking 驗證 | `thinking.type=disabled` + `effort` `xhigh`/`max` 在載入時直接報錯 | `src/lincy/core/schema.py` | 對齊官方 400 限制 |
| Vision | YAML 使用 plain `vision: true` | `src/lincy/core/schema.py` | 統一為 plain boolean |
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
| synthetic tool history textification | `read_startup_context`、`_load_skill_prerequisite`、`_load_common_ground_at_message_time`（以及舊 session 才有的 `_stage1_gather`）等 runtime synthetic tool pair 在 Ollama adapter 出口改寫成普通 `system` 文字訊息，不再以 native function-call history 送出 | `src/lincy/llm/providers/ollama_native.py` | 這些訊息是內部上下文注入，不是真實 provider tool loop；Gemini-backed Ollama 會對其做更嚴格驗證而回 `400` |
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

| 項目 | OpenAI | Anthropic | Heyroute | Gemini | OpenRouter | Ollama |
|------|--------|-----------|----------|--------|------------|--------|
| Endpoint | Chat Completions | `/v1/messages` | `/v1/messages`（假設相容/未驗證） | `generateContent` | OpenAI compat | native `/api/chat` |
| Reasoning 參數 | `reasoning_effort`（頂層） | `thinking.type` + `output_config.effort` | `thinking.type` + `output_config.effort`（假設相容/未驗證） | `thinkingConfig` | `reasoning: {"effort":...}` | `think`（native） |
| Effort 值 | low/medium/high/xhigh/max（adapter passthrough；官方另列 none） | low/medium/high/max（output_config） | low/medium/high/xhigh/max（假設相容/未驗證） | minimal/low/medium/high（依模型） | none/minimal/low/medium/high/xhigh | low/medium/high/xhigh/max（adapter passthrough） |
| Token budget | 無 | `thinking.budget_tokens` | `thinking.budget_tokens`（假設相容/未驗證） | `thinkingBudget` | `reasoning.max_tokens` | 無 |
| Vision | `image_url` | `image` block（base64/url） | `image` block（假設相容/未驗證） | `inlineData`（base64） | `image_url` | 依模型 |
| Tools | OpenAI function | Anthropic `input_schema` | Anthropic `input_schema`（假設相容/未驗證） | Gemini `functionDeclarations` | OpenAI function | native `tools` |
| Auth | Bearer token | Bearer/x-api-key + version | x-api-key + version（假設相容/未驗證） | API key (header/query) | Bearer token | 本機 daemon 無 |
| max_tokens | 可選（GPT-5+ 用 `max_completion_tokens`） | **必填** | **必填**（假設相容/未驗證） | 可選（maxOutputTokens） | 可選 | `options.num_predict` |
| Prompt cache | 自動 prefix（`prompt_cache_retention: "24h"`） | Anthropic breakpoint | Anthropic breakpoint（假設相容/未驗證） | 無 | `cache_control` breakpoint | 無 |

Kano Proxy 不另開欄：獨立 `provider: kano_proxy`，payload / auth header / cache breakpoint 與 Anthropic、Heyroute 同一 adapter；差異只有 gateway URL 與 `KANO_PROXY_API_KEY`。

---

## Usage Token 回收（non-streaming）

本節描述本專案 runtime 對「回應 usage 欄位」的統一回收規則。

| Provider | API 是否可能回 usage | Adapter 是否回收 prompt/completion/total | Adapter 是否回收 cache read/write | 缺值策略 |
|---|---|---|---|---|
| OpenAI / OpenRouter（OpenAI-compatible） | 是（視 gateway/模型） | 是 | 是（若有 prompt_tokens_details） | `usage=None` 時標記 unavailable |
| Ollama（native `/api/chat`） | 是（`prompt_eval_count` / `eval_count`） | 是 | 否 | 欄位缺失時標記 unavailable |
| Anthropic / Heyroute / Kano Proxy | 是 | 是（prompt = input + cache_read + cache_creation；completion = output） | 是（cache_read_input_tokens / cache_creation_input_tokens） | `usage` 缺失時標記 unavailable |
| Gemini | 是（usageMetadata） | 是（promptTokenCount / candidatesTokenCount / totalTokenCount） | 否 | `usageMetadata` 缺失時標記 unavailable |

補充：
- 本專案目前只看 non-streaming 回應，不使用 streaming usage。

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
