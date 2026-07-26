# LLM Provider 自治架構準則

本文件定義本專案在 LLM provider 整合上的設計邊界，避免再把 provider 差異硬塞進共用層。

配套文件：
- API 事實盤點：`docs/dev/provider-api-spec.md`
- Copilot premium request 省費機制：`docs/dev/copilot-agent-hint.md`

## 核心原則

### 1. 先 API 事實，後抽象

先確認各 provider 的官方 API 格式、限制、模型/版本差異，再決定 YAML schema 與程式抽象。

禁止做法：
- 用現有程式碼或舊 YAML 反推 API 規格
- 為了統一格式而扭曲 provider 真實 API

### 2. 統一 client 介面，不統一 provider config 格式

本專案追求的是一致的呼叫介面（例如 `chat`、`chat_with_tools`），不是讓所有 provider 的 YAML 與 payload 長得一樣。

代表：
- 各 provider 可使用不同的 reasoning/thinking 設定欄位
- 共用層不應定義假的「通用 reasoning 值集合」來限制所有 provider
- 同一個 concrete client 只對應一種 provider API format；若 provider 同時有 native 與 compat 路徑，必須拆成不同 client/config，不在同一個 class 內混用

### 3. provider-specific 邏輯回到 provider 自己

應放在各 provider config/client 的內容：
- reasoning/thinking 驗證規則
- payload 映射與欄位命名
- provider-specific override/escape hatch
- provider 能力差異（vision、thinking 限制等）

不應放在共用層（例如 factory 或中央 reasoning 模組）的內容：
- `isinstance` chain 判斷各 provider 規則
- provider payload 格式轉換
- provider-specific precedence（如 OpenRouter effort vs max_tokens）

### 4. factory 只做共通流程

`factory` 的責任：
- request timeout override
- retry wrapper
- 建立共通 client 包裝

`factory` 不做的事：
- import 各 provider client/config 後分支判斷
- 解讀 provider-specific kwargs
- provider-specific feature routing

provider kwargs 應由組裝層決定是否傳入，並由對應 provider 的 `create_client()` 明確接受。

### 5. runtime feature 放在組裝層，不放進 provider YAML

像 Copilot 的 initiator routing 屬於 runtime policy（計費/分類優化），不是靜態模型設定。

規則：
- runtime feature routing 放在 `app.py`（composition root）
- 只有需要的 provider 才收到該 runtime kwarg
- 不把 runtime feature 塞進 `cfgs/llm/*` provider profile
- 若需要可調 policy，放 app-level config，由組裝層讀取後決定 runtime routing

## 分層責任（目前實作）

### `src/lincy/core/schema.py`
- 定義 provider-specific config 類型（含各自 reasoning/thinking config）
- 提供每個 config 的 `validate_reasoning()` / `get_vision()` / `create_client()`
- 不提供跨 provider 的中央 reasoning 分發

### `src/lincy/llm/providers/*.py`
- 各 provider 的 API payload 映射
- provider-specific request/response 差異處理
- provider-specific adapter 規則與已知限制註解

### `src/lincy/llm/factory.py`
- provider-agnostic 建立流程
- timeout / retry 等共通包裝
- 不做 provider-specific 特判

### `src/lincy/cli/app.py`
- app-level policy 與 runtime hints 的路由（例如 Copilot initiator routing）
- 可做最小限度 provider-aware 判斷（組裝層例外）
- 不直接組 provider payload

### Agent-level LLM fallback

- `llm_fallbacks` 屬於 agent 組裝層能力，不屬於 provider config schema 本身的 API 事實
- fallback chain 應包在 agent client composition，不能塞進單一 provider client 或 `factory`
- failover 條件應聚焦在 quota / rate-limit / availability 類錯誤，不要拿來掩蓋 request-format 或 context 問題
- 模型 entitlement / subscription 不足（例如 provider 回 `403` 且訊息要求 subscription / upgrade 才能使用該模型）視為 availability 類錯誤，可切換到下一個 fallback；一般 auth / payload 錯誤仍不得用 fallback 掩蓋
- `use_own_vision_ability: true` 時只要求 primary `llm` 具 vision；fallback 可混用。對**當前 prefer 的 candidate**：有 vision 比照 own-vision，無 vision 比照 `use_own_vision_ability: false`（走 vision sub-agent）。prompt 已含 image parts 時，failover 會跳過無 vision 的 candidate

## 文件同步規則

當 provider 行為或 adapter 規則變更時：
1. 先更新 `docs/dev/provider-api-spec.md`
   - 區分「官方 API 事實 / 本專案 adapter 規則 / 實測逆向資訊」
2. 再修改程式碼
3. 若是架構邊界調整，更新本文件

## 常見反模式（避免）

- 在共用層建立 `ReasoningConfig` 後硬套所有 provider
- 在 `factory` 增加 provider-specific `if/elif` 或 `isinstance` 分支
- 把 runtime feature 直接塞進 provider YAML profile
- 對不支援的 provider kwargs 做 silent ignore
- 用「順手重構」帶過架構邊界變更
