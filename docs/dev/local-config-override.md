# 本機設定覆蓋（agent.override.yaml）

`cfgs/agent.override.yaml` 讓你在本機調整設定（換 brain / fallback model、關掉某個 channel 等），
不必修改受 git 控制的 `cfgs/agent.yaml`。檔案已列入 `.gitignore`。

## 使用

```bash
cp cfgs/agent.override.yaml.example cfgs/agent.override.yaml
```

檔案不存在時完全不影響行為。

```yaml
# cfgs/agent.override.yaml
agents:
  brain:
    llm: cfgs/llm/codex/gpt-5.5/thinking.yaml
    llm_fallbacks:
      - cfgs/llm/deepseek/deepseek-v4-pro/thinking.yaml
```

## 合併規則

| 型別 | 行為 |
|------|------|
| dict | 遞迴合併；未提及的 key 保留 `agent.yaml` 的值 |
| list | 整份取代（要清空就寫 `[]`） |
| scalar | 取代 |

list 不做元素級合併：`llm_fallbacks`、`excluded_tools`、`boot_files` 這類欄位若做 append
語義無法預期，一律要求寫出完整清單。

合併發生在 `load_config()` 解析 LLM 路徑**之前**（`src/lincy/core/config.py`），
此時兩邊的 `llm` 都還是字串路徑，不會出現 dict 蓋字串的髒合併。

## 邊界

- schema 仍是 `extra="forbid"`：override 打錯 key 會在啟動時報錯，不會 silent ignore。
- 不支援「刪除 key」語義（沒有 null sentinel）。
- **不覆蓋 `cfgs/llm/**`**。本機要用不同的 provider profile，就在 `cfgs/llm/` 下自建檔案再由
  override 指過去；多一層 provider config override 只會讓實際生效值難以追查。
- **不放 API key**，key 一律走 `.env`。

## 讀取路徑統一

所有 `agent.yaml` 讀取者都走 `lincy.core.config.load_raw_agent_config()`，
避免 agent 進程與周邊服務對同一個值（如 `app.agent_os_dir`、`app.timezone`）看法不一致：

- `load_config()` / `load_app_timezone()`（`src/lincy/core/config.py`）
- `WebApiSettings.from_env()`（`src/chat_web_api/settings.py`）
- `chat_supervisor check`（`src/chat_supervisor/check.py`，同時會印出 override 是否套用）

supervisor 的 `enabled: auto`（依 agent 實際使用的 provider 決定要不要啟動對應 proxy）
經由 `load_config()` 取值，因此會自動跟著 override 走。

套用時會在啟動 log 印出被覆蓋的路徑，例如：

```
INFO: Applied agent.override.yaml: agents.brain.llm, agents.brain.llm_fallbacks
```

## 測試

需要斷言「repo 預設值」的測試改用 `load_config("agent.yaml", apply_override=False)`，
避免本機 override 汙染測試結果。
