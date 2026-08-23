# Codex Prompt Cache 存活觀察

## 三行摘要

- 目前**不能**把 `cfgs/agent.yaml` 的 `cache.ttl` 當成 Codex upstream 的真實 TTL。對 `codex` 而言，它目前只是**本地 `prompt_cache_key` 輪換週期**。
- repo 與官方開源 CLI 能證明的事實是：會送 `prompt_cache_key`、`session_id`，同一 turn 內會重送 `x-codex-turn-state`；**沒有**看到可由 client 指定 `1h` 或 `24h` upstream TTL 的公開欄位。
- **Codex 專屬** session 實測顯示：近期 full-hit 樣本大多落在 `10m` 內，`13m~35m` 常見 cold miss，甚至有 `2.3m` 就 miss 的例子。先前 archive 裡接近 `55m` 的樣本，回頭核對後是 `claude_code`，不是 `codex`。所以目前結論是：**對 Codex upstream 沒有接近 1h 的直接證據，也沒有 24h 證據。**

## 名詞先分清楚

- **本地 `cache.ttl`**
  - 目前只用來決定本地 `prompt_cache_key` 何時換 bucket。
  - `ephemeral` = 5 分鐘換 key，`1h` = 1 小時換 key，`24h` = 1 天換 key。
- **upstream cache 存活時間**
  - 指 ChatGPT Codex backend 實際還願不願意把前一輪 prefix 當成 cache hit。
  - 這不是本專案可以直接保證的值。

這兩件事不要混在一起。

## repo / 官方 CLI 能直接證明的事

### 1. 本專案對 `codex` 的 `cache.ttl` 是本地 key 旋轉，不是 upstream TTL

- [`README.md`](../../README.md) 已寫明：`codex` 的 `cache.ttl` 是本地 cache key 輪換週期，不是 upstream 公開 TTL 參數。
- [`src/lincy/cli/app.py`](../../src/lincy/cli/app.py) 會把 key 組成：

```text
{session_id}:{namespace}:{ttl_bucket}
```

- [`src/codex_proxy/service.py`](../../src/codex_proxy/service.py) 往 upstream 只會轉送 `prompt_cache_key`，沒有另外送 TTL 欄位。

### 2. 官方開源 Codex CLI 目前可確認的 cache 相關欄位

- 官方 CLI 會送 `prompt_cache_key`，值預設是 `conversation_id`
  - 來源：[`openai/codex` `codex-rs/core/src/client.rs`](https://raw.githubusercontent.com/openai/codex/main/codex-rs/core/src/client.rs)
- 官方 CLI 也會送 `session_id` header，值同樣是 `conversation_id`
  - 來源：[`openai/codex` `codex-rs/codex-api/src/requests/headers.rs`](https://raw.githubusercontent.com/openai/codex/main/codex-rs/codex-api/src/requests/headers.rs)
- 官方 CLI 只保證 **同一個 turn 內** 會重送 `x-codex-turn-state`
  - 來源：[`openai/codex` `codex-rs/core/tests/suite/turn_state.rs`](https://raw.githubusercontent.com/openai/codex/main/codex-rs/core/tests/suite/turn_state.rs)

目前從 repo 與官方開源程式碼，**看不到**可由 client 明確指定「upstream 請保 1h / 24h」的公開欄位。

## session 實測方法

- 範圍：`~/Library/Mobile Documents/com~apple~CloudDocs/Lincy/session/brain/*`
- 指標：每個 turn 的**第一個有 usage 的 brain request**
  - 舊 session 的 `round=1` 可能是子代理呼叫，沒有 prompt usage，要略過
- gap 定義：`本 turn ts_started - 前一個 turn ts_finished`
- 觀察目標：`cache_read_tokens / prompt_tokens`

這個觀察方式看的是 **cross-turn 首次進 brain 時 upstream 還剩多少 cache**，不是看同一 turn 內 tool loop 的自然 reuse。

## provider 範圍先切清楚

- 2026-04-09 到 2026-04-11 早上的一些 archive session，brain provider 還是 `claude_code`
- 例如：
  - `20260409_030020_579fc1`
  - `20260410_030024_9eae69`
  - `20260411_030034_f74a5b`
- 這些 session 裡接近 `55m` 的高 hit 樣本，回頭核對 `responses.jsonl` 都是 `provider: "claude_code"`，不是 `provider: "codex"`
- 目前可直接當成 **Codex provider** 觀察樣本的，主要是：
  - `20260411_133813_70b723`
  - `20260412_030026_196029`

所以這份文件後面談「Codex upstream 存活時間」時，若沒有特別註明，就是以這兩個 `codex` session 為主。

## 彙整結果

### Codex provider 樣本

- session：
  - `20260411_133813_70b723`
  - `20260412_030026_196029`
- 觀察對象：cross-turn 的第一個有 usage 的 brain request

### Codex provider 的目前觀察

- full-hit 比較穩的 gap，大多在 `10m` 內
- `13m~35m` 已有多個 cold miss
- 目前**沒有**看到 `codex` 在 `>10m` 還有接近滿 hit 的直接樣本
- 目前**沒有**看到 `codex` 接近 `1h` 的直接樣本
- 目前**沒有**任何能支持 `24h` upstream 存活的樣本

### 先前被誤認成 Codex 的樣本

| 觀察值 | session / turn | 實際 provider |
|--------|----------------|---------------|
| `55.4m` 仍高 hit | `20260409_030020_579fc1 / turn_000018` | `claude_code` |
| `53.6m` 仍高 hit | `20260411_030034_f74a5b / turn_000002` | `claude_code` |

這些樣本能證明「**session archive 裡有接近 1h 的 cache hit**」，但**不能**拿來證明「**Codex upstream** 有接近 1h 的 cache 存活」。

## 近期 session 的關鍵樣本

### session `20260412_030026_196029`（`provider=codex`）

| turn | gap | 首個有 usage 的 brain request | 解讀 |
|------|-----|-------------------------------|------|
| `turn_000019` | `9.7m` | `141,440 / 141,972` | 幾乎全 hit |
| `turn_000020` | `13.9m` | `0 / 141,884` | 直接 cold miss |
| `turn_000022` | `8.7m` | `146,688 / 147,199` | 又回到幾乎全 hit |
| `turn_000025` | `34.6m` | `0 / 148,684` | cold miss |
| `turn_000026` | `9.6m` | `150,016 / 150,438` | 幾乎全 hit |
| `turn_000024` | `2.3m` | `0 / 147,804` | 反例：很短也可能 miss |

### session `20260411_133813_70b723`（`provider=codex`）

| turn | gap | 首個有 usage 的 brain request | 解讀 |
|------|-----|-------------------------------|------|
| `turn_000029` | `0.5m` | `96,768 / 97,254` | 幾乎全 hit |
| `turn_000035` | `4.8m` | `15,872 / 106,645` | 只有局部 hit |
| `turn_000036` | `13.8m` | `0 / 106,679` | cold miss |
| `turn_000045` | `25.1m` | `0 / 112,991` | cold miss |
| `turn_000046` | `0.9m` | `112,896 / 113,538` | 又回到幾乎全 hit |

## 該怎麼解讀

### 可以確定的事

- 同一 turn 內的 tool loop cache reuse 通常很好，後續 round 常接近 `99%` hit。
- cross-turn 首次 brain request 的 hit 與 miss **不是只由 TTL 決定**。
- `cache.ttl: "24h"` 在 `codex` 上**不能**解讀成 upstream 真的會保 `24h`。
- 目前 archive 裡接近 `1h` 的高 hit 樣本，回頭核對後是 `claude_code`，**不是** `codex`。

### 目前最合理的判斷

- `codex` upstream 確實會吃 `prompt_cache_key`。
- 但以目前 `codex` 專屬樣本來看，cross-turn 存活非常不穩，full-hit 多半落在 `10m` 內，`13m~35m` 已常見 cold miss。
- 又因為有 `2.3m` 就 miss 的反例，所以這不只是「TTL 短」，也可能混了：
  - prefix 不完全相同
  - backend/shard 行為差異
  - server 端 cache eviction
  - 與 `session_id` / conversation 狀態有關的其他條件

因此目前最安全的說法是：

- **對 Codex 沒有接近 1h 的直接證據**
- **對 Codex 沒有 >10m 穩定證據**
- **沒有 24h 證據**
- **近期 Codex 行為更像短窗口，而且不穩**

## 對目前設定的實務建議

- `codex` 的 `cache.ttl` 先繼續當成**本地 key bucket** 看待。
- 若目標是避免本地自己太早換 key，`24h` 仍然有意義。
- 但若目標是預估 upstream 成本，**不要**把 `24h` 寫成保證。
- heartbeat / pre-sleep sync 文件裡提到的 `1h`，應視為**歷史設計假設或上限估計**，不是目前 Codex upstream 的可靠契約。

## 相關檔案

- [`README.md`](../../README.md)
- [`docs/dev/provider-api-spec.md`](./provider-api-spec.md)
- [`docs/dev/heartbeat.md`](./heartbeat.md)
- [`src/lincy/cli/app.py`](../../src/lincy/cli/app.py)
- [`src/codex_proxy/service.py`](../../src/codex_proxy/service.py)
