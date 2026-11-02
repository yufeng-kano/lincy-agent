# Discord 頻道收聽

Discord adapter 怎麼決定哪些 guild 頻道的訊息會進來、怎麼送進 brain，以及 agent 用 `discord_listen` 自己調整收聽範圍的規則。

## 資料位置

全部在 `<agent_os_dir>/state/discord/`（`DiscordHistoryStore`，`src/lincy/agent/discord_history.py`）：

| 檔案 | 內容 |
|------|------|
| `channel_registry.json` | 每個已登記頻道一筆：`channel_id`、`guild_id`、`alias`（`#name @ guild`）、`filter`、`source`、`review_interval_seconds`、時間欄位 |
| `cursors.json` | 每個頻道的 `next_event_seq`、`last_reviewed_seq`、`last_review_at`、`last_seen_message_id` |
| `history/<channel_id>.jsonl` | append-only 事件（`message_create` / `message_edit`），含 agent 自己送出的訊息 |

store 只在啟動時讀一次檔，之後每個事件都用記憶體內容覆寫整個檔案。程式執行中手改 JSON 會被蓋掉；要改收聽範圍用 `discord_listen`。

## 收聽模式

| `filter` | 存什麼 | 怎麼送進 brain |
|----------|--------|----------------|
| `all` | 這個頻道每則訊息 | 定時巡看：有新訊息時，最多每 `review_interval_seconds` 整批送一次。每批都是一次 brain 呼叫，沉默也算 |
| `mention_only` | 只有 tag agent 的訊息，加上每次被 tag 時補抓的前文 | 被 tag 時，debounce 後送一批 |
| `mute` | 不存，tag 也不收 | 不送 |

判斷在 `_filter_allows()`：`mute` 一律擋；有 tag 就收；其餘只有 `all` 收。registry 裡不認得的值（例如舊版的 `from_contacts`）照 `mention_only` 處理。

DM 不走這張表：`listen_dms` 開著時每則 DM 都收，DM 不在 `discord_listen` 的範圍內。

## 頻道怎麼進名單

- **第一次被 tag**：沒登記過的 guild 頻道裡有人直接 tag agent（`message.mentions` 含 agent 的 user id，有 ping 的回覆也算；role、`@everyone` 不算），登記為 `mention_only`，`source: auto_mention`。
- **`channels.discord.listen_channels`**：非空時是硬白名單，清單外的 guild 頻道全部擋掉，tag 也一樣。清單內的 `filter`（`all` 或 `mention_only`）只在該頻道第一次登記時當初始值，之後以 registry 為準。清單外但 registry 裡還留著的頻道，`discord_listen list` 會標 `blocked_by_config: true`，`set` 回錯誤。
- 名單不會過期，也沒有刪除路徑；不想收就設 `mute`。

Thread 與論壇貼文有自己的 channel id，各自登記，不繼承上層頻道的模式。

## 被 tag 時補抓前文

`mention_only` 頻道平常不存沒 tag 的訊息，所以被 tag 時 adapter 先用 REST 抓 tag 之前的 `mention_context_messages` 則（預設 20，`0` 關閉）存進 history，再存 tag 本身：

- 已存過的 message id 跳過；`ignore_users` 的訊息跳過；agent 自己的舊訊息保留，當作上下文。
- 正在處理中的 message id（`_handling_ids`，例如還在下載附件）也跳過，由它自己的 handler 存。`_handle_message` 同一個 id 同時只跑一次，gateway、backfill 與補抓前文撞到同一則時不會重複存。
- 附件照常下載（有 `local_path`），但不加自動判讀提示，要不要看由 brain 決定。
- 抓取失敗只記 debug log，tag 照常送出。
- `all` 頻道不補抓，因為前文本來就存著。

補抓的訊息與 tag 落在同一批（`last_reviewed_seq` 之後），依訊息時間排序送進 brain。

## 巡看細節

- 定時巡看每秒 tick，只看 `filter: all` 的 guild 頻道；cursor 顯示沒有新事件時不讀 history 檔。
- 等 `on_ready` 拿到 agent 自己的 user id 後才開始巡看。
- 一批裡只有 agent 自己送出的訊息時，標記已看、不送 brain，也不重設巡看間隔。
- 巡看與被 tag 的批次都帶 `evict_if_noop`：brain 沉默的那一輪會從對話裡移除。成功的 `discord_listen set` 算有效果（`turn_effects.had_listen_mutation`），那一輪會保留，agent 之後記得自己改過。

## 編輯

`_handle_message_edit` 套用和新訊息相同的過濾：`ignore_users`、硬白名單、`_filter_allows()`。編輯不會登記新頻道。

- `all` 頻道：編輯寫進 history 後，下一次巡看會送出，只有編輯也算一批。
- `mention_only` 頻道：只存提到 agent 的編輯，不觸發 brain，跟著下一次被 tag 的批次送出。

## 重啟補抓（backfill）

啟動時對每個已登記頻道，從 `last_seen_message_id` 之後抓最多 100 則，再走一次 `_handle_message`。`last_seen_message_id` 只往較新的 snowflake 移動；已存成 `message_create` 的 id 一律跳過。

- 被過濾掉的訊息（`mute`、`mention_only` 裡沒 tag 的）不存，但仍用 `advance_seen_cursor()` 推進 `last_seen_message_id`。否則重啟時會重抓舊閒聊，100 則的窗口也可能擠掉較新的 tag。
- backfill 之前先跑 `_catchup_unreviewed_mentions()`：`mention_only` 頻道裡有已存、但還沒送 brain 的 tag（例如 debounce 期間程式中斷），立即送一批。`all` 頻道交給定時巡看，`mute` 不送。

## `discord_listen` 工具

收聽範圍是 agent 自己的決定。程式不檢查是誰要求的；任何人（包括 owner）的要求都只是 agent 判斷時的參考，agent 可以答應，也可以拒絕。

```
discord_listen(action="list")
discord_listen(action="set", filter="all" | "mention_only" | "mute", to="#channel @ guild" | channel_id="..." )
```

- `list` 回傳所有已登記 guild 頻道：`{"channels": [{channel_id, alias, filter, source, last_seen_at, blocked_by_config}]}`，依 alias 排序。`filter` 是實際生效的值，舊的 `from_contacts` 顯示為 `mention_only`。
- `set` 的目標依序取 `channel_id`、`to`（與 `get_channel_history` 相同的 alias 解析）、目前所在的 Discord guild 頻道。只能改已登記的 guild 頻道；沒登記的頻道要先在那裡被 tag 一次。回傳 `{channel_id, alias, previous_filter, filter}`。
- 錯誤回 `Error: ...` 字串：DM、硬白名單外、未登記、alias 找不到（附最多 20 個已登記 guild 頻道名）。
- 改完立即生效：adapter 每則訊息、巡看每個 tick 都重讀 registry。

只在 `channels.discord.enabled` 且有 token 時註冊，因此列在 `_CONDITIONAL_TOOLS`。只給 brain：worker 的 `excluded_tools` 排除它，因為收聽範圍是 brain 自己的決定，worker 拿不到 brain 這一輪的 Discord 頻道。

## 升級

- 既有 registry 不改。升級前因為一次 tag 而登記成 `all` 的頻道維持 `all`，由 agent 用 `list` 看過後自己決定要不要調整。
- `listen_channels[].filter` 不再接受 `from_contacts`，config 寫了會在啟動驗證時報錯。
- brain prompt 與 `discord-messaging` skill 由 migration `m0185` 更新。

## 相關檔案

- `src/lincy/agent/adapters/discord.py`：收訊、過濾、補抓前文、巡看、backfill
- `src/lincy/agent/discord_history.py`：registry、cursor、history
- `src/lincy/tools/builtin/discord_listen.py`：工具
- `src/lincy/core/schema.py`：`DiscordChannelConfig`、`DiscordListenChannel`
