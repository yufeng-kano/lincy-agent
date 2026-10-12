# Audio Read：語音訊息交給 audio 子代理

## 結論

brain 走的是 Anthropic Messages 格式（含 kano-proxy `/anthropic`），這個 wire format 沒有 audio content block，所以 brain 永遠聽不到聲音。語音一律委派給 **audio 子代理**：一個可吃音訊的多模態模型，回逐字稿加上語氣、情緒、態度、語速等線索。沒有 `read_image` 那種 own-ability 分支。

## 元件

| 元件 | 位置 | 說明 |
|------|------|------|
| `ContentPart(type="audio")` | `src/lincy/llm/schema.py` | 沿用 `data`（base64）與 `media_type` |
| provider 映射 | `src/lincy/llm/providers/` | `openai_compat` 轉 `input_audio`（`format` 由 mime 對照表決定，未知 mime 直接 raise）；`gemini` 走 `inline_data`；`anthropic_messages`、`ollama_native`、`deepseek` 一律 raise，不 silent drop |
| 能力旗標 | `src/lincy/core/provider_schema.py` | `LLMProviderConfig.get_audio()` 預設 False；OpenAI / Gemini 用 `capabilities.audio`，OpenRouter 用 plain `audio: true`。Anthropic 類 config 沒有此欄位，寫了會在載入時被 strict schema 拒絕 |
| failover 閘 | `src/lincy/llm/failover.py` | `FailoverCandidate.supports_audio`；prompt 含 audio part 時跳過不支援的 candidate，全部不支援則報錯 |
| `MediaDescribeAgent` | `src/lincy/tools/builtin/media_agent.py` | vision 與 audio 共用：parts 進、文字出，依請求 digest 做磁碟快取。`VisionAgent` / `AudioAgent` 只固定 session label |
| `read_audio_by_subagent` | `src/lincy/tools/builtin/audio.py` | `path` + `context` 必填；副檔名白名單與 `openai_compat` 的 format 表一致；檔案上限 14 MB（Gemini inline 20 MB 扣掉 base64 膨脹） |
| 組裝 | `src/lincy/agent/build.py` + `tool_setup.py` | `agents.audio` enabled 且 prompt 存在才建 agent 並註冊工具 |
| 啟動檢查 | `src/lincy/core/config.py` | `agents.audio` 的 primary 與每個 fallback 都必須 `get_audio()`，否則早停 |
| prompt | `kernel/agents/audio/prompts/system.md` | 逐字、不改寫、繁體、不認人、不確定標 `[inaudible]`；依序回逐字稿、語氣、環境線索、指定問題 |

## kano-proxy 路由

- 音訊只在 kano-proxy 的 `/g/<group>/openai/v1` 接受；`/anthropic` 明確不收。profile `cfgs/llm/kano-proxy/audio.yaml` 用 `provider: kano_proxy_openai`，model `audio-agent`。
- group 內 `audio-agent` 的**第一個** target 決定一切：kano-proxy 只看最高優先 target 判定能否吃音訊，不會 fallthrough。第一個放 `antigravity/gemini-3.8-flash-low`，後面可掛 `openrouter/google/gemini-3.8-flash`。
- `vision-agent` 的第一個 target 是 OpenRouter 的 gpt-5.6 系列，不吃音訊，所以不能共用。

## Discord 自動提示

對齊圖片流程：`content_type` 為 `audio/*` 的附件下載後，依 `channels.discord.auto_read_audio*` 旗標在 `[Attachments]` 下加

```
audio_summary: [pending] Use read_audio_by_subagent on <local_path>
```

並在訊息尾端加 `[Discord audio hint]`。`auto_read_audio_max_mb` 預設 14，必須低於工具的檔案上限。附件只列檔名時 brain 仍可自行決定要不要聽。

## 實測（2026-10-12）

- Discord 語音是 Ogg/Opus（`audio/ogg`）。從 lincy 主機送 `input_audio` `format: ogg` 到 kano-proxy `/g/lincy/openai/v1`，antigravity Gemini 3.8 Flash 回 200，6 秒，逐字稿正確，附語言、人數、語氣。4 秒音訊約 132 prompt tokens（符合 Gemini 文件的每秒 32 tokens）。
- 回傳預設簡體，所以 prompt 明寫臺灣用字。
- `-tiered` 等級對 4 秒音訊花了 116 reasoning tokens，profile 用 `-low`。
- kano-proxy 容忍 `OpenAIClient` 固定送出的 `prompt_cache_retention` 欄位（null 與 `"24h"` 都回 200）。
- `vision-agent` 送音訊回 404 `No endpoints found that support input audio`。

## 取捨

- 自動提示會讓每則語音多一次 6 秒左右的子代理呼叫與 reasoning tokens；關掉 `auto_read_audio` 即退回純工具。
- 語音經 antigravity 出去，外流等級與圖片走 vision-agent 相同。
- 超過 14 MB 的語音不會自動提示，工具也會拒收；長音訊目前不切段。
