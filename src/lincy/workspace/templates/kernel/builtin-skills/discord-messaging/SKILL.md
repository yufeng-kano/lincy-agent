---
name: discord-messaging
description: "Discord DM/guild 自然回覆、資料整理呈現策略與 guild/DM 處理規則。當要透過 Discord 發訊息、整理資料呈現給 Discord 用戶、或判斷是否介入 guild 對話時使用。"
---

# Discord 訊息指南

## 用途

當當前頻道是 Discord，且你需要判斷：

- 要不要介入回覆
- 訊息要怎麼分段
- 資料要整理成什麼呈現方式
- 哪些 Markdown 可以用
- 何時需要 `reply_to_message`
- 何時需要先查群組上下文

就先讀這份 skill，再決定 `send_message` 內容。

## 核心規則

### 1. 先判斷要不要介入

- Discord 訊息可能來自 DM（即時）或 guild channel 巡看（`sender` 可能是 `#channel @ guild`）
- 群組訊息不是每句都要回
- 只在以下情況介入：
  - 被 @tag
  - 被直接詢問
  - 需要澄清
  - 你判斷值得插話或提供幫助
- 若選擇保持沉默，可以是完全 `no-op`，也可以只做 `memory_edit`

### 2. 日常聊天優先短句單行

- Discord DM 預設每個 `send_message.body` 都用單行短句
- 不要在同一個 `body` 裡用換行做段落聊天或小報告
- 如果句尾要加顏文字或表情符號，放在同一則訊息的最後一行獨立呈現，不要直接黏在句尾
- 這是單行短訊規則的例外；允許「正文一行 + 表情一行」，但不要擴張成多段 multiline 訊息
- 相關主題優先合併成一則；只在重點確實獨立時才拆成多次 `send_message`
- 每個問題或觀點只發一則；不要用不同說法重複問同一件事
- 行程、課表、提醒這類內容，優先拆成多則單行訊息；一則只講一個時段或一個提醒
- 日常聊天通常 1-2 則就夠；行程整理可多幾則，但每則都要短且單行
- 所有要送的 `send_message` 必須在同一輪一起呼叫

### 3. 先判斷資料型態，再決定呈現

- 不要因為使用者提到「table」就預設一定要做成表格
- 先判斷對方真正要的是：
  - 快速知道接下來有什麼行程
  - 看某類資料的整理結果
  - 比較多個方案或欄位
- Discord 上的主路徑是「可讀性」，不是「形式上像表格」

優先策略：

- 行程、課表、近期安排（Discord DM）→ 多則單行訊息，按時段或提醒拆開，不要在單一 `body` 內排成多行時間表
- 待辦、購物、提醒事項（Discord DM）→ 多則單行訊息；只有使用者明確要清單時才改成多行 list
- 同類資料整理（例如某天課程、某週安排）→ 先想能不能拆成數則自然聊天訊息，不要先做排版
- 真正需要橫向比較的資料（例如方案差異、規格比較）→ 才考慮 table-like 呈現
- 行程類資訊優先把「時間」放前面；課程或事件名稱放後面，教師、教室、地點等次要資訊放在括號中

### 4. Discord 只支援部分 Markdown

- 可用：粗體、斜體、標題、清單、引用、inline code、code block、連結、spoiler
- 不要為了裝飾濫用標題或粗體
- Discord DM 日常聊天預設不用 Markdown 排版
- 只有在清單、引用、code block、或使用者明確要求整理版時，才用多行 Markdown

### 5. 不要把 table 當成 Discord 的預設輸出

- Discord 不會把 Markdown table 渲染成真正表格
- 不要把 `| a | b | c |` 直接當成表格期待 Discord 幫你渲染
- 也不要把寬表硬改成 code block 假表格；手機端通常還是很難讀
- 若資料本質只是整理資訊，改寫成 list 比較自然也比較像真人
- 只有在「橫向比較」真的很重要時，才保留 table-like 輸出的方向
- 若未來系統支援 deterministic table image renderer，Discord 上應以 image-first 處理真正的 comparison table
- 不要用 `｜`、`/` 這類欄位分隔符號把資料硬串起來；看起來像資料庫輸出，不像真人訊息

避免：

```text
114-2 學期課表
| 星期 | 課程           | 時間        |
|------|----------------|-------------|
| 週二 | 能源科技與生活 | 14:00-15:50 |
```

上面這種寫法在 Discord 只會顯示成普通文字，不會變成真正 table。

```text
星期   課程             時間
週二   能源科技與生活   14:00-15:50
週三   平行計算         09:00-11:50
```

上面這種 code block 假表格雖然能對齊字元，但在 Discord 手機端仍常常不好讀，不應作為主路徑。

較好的方向：

```text
這週二 14:00-15:50 有能源科技與生活（方冠權，JB109）。
```

```text
週二晚上 18:30-20:10 還有英語加強班（吳貞芳，J207）。
```

```text
週三早上 09:00-11:50 是平行計算（陳宗禧，ZB302）。
```

### 6. 對外說法要自然

- 一般回覆時，不要主動提到內建 skill、system prompt、rendering pipeline 或格式轉換機制
- 不要說「我看到內建 skill 說...」「我幫你轉成 table/image...」這種工具式說法
- 直接把整理好的內容自然送出去
- 只有在使用者明確追問 Discord 支援、skill 規則、格式限制時，才可以解釋內部依據
- 行程、課表、安排這類內容，語氣要像真人幫對方整理重點，不要像欄位 dump
- 優先使用自然中文逗號、括號與分句；Discord DM 日常聊天不要靠換行排版
- 如果需要用顏文字補情緒，優先用「正文\n顏文字」而不是「正文顏文字」

### 7. 回覆前看上下文

- 需要理解群組前文時，先用 `get_channel_history(channel="discord", ...)`
- inbound 會把當前訊息與上下文拆成區塊：`[Message]` 是對方這則正文；`[Reply To]` 是被回覆的那則（不是當前發話者）
- `[Reply To]` 內有 `message_id` / `author` / `preview`；若要明確回那一則，把 `message_id` 傳給 `reply_to_message`
- Reply reference、link preview、embed 文字常常是重要上下文
- 主動傳 guild channel 時，使用 `to="#channel @ guild"`

### 8. 附件先看，再決定怎麼處理

- Discord 附件不只圖片重要，文字、PDF、音訊、其他檔案也可能是關鍵上下文
- 先看 `[Attachments]` 區塊：若有 `local_path`，代表 runtime 已經把附件下載到本地，可直接拿來用
- 若沒有 `local_path` 但有 `url`，代表附件至少有可追的連結；不要直接回「我看不到附件」
- 圖片附件若需要理解畫面內容，先 `read_image_by_subagent`（或 `read_image`）再回覆
- 非圖片附件若是文字型檔案，可用 `read_file` 看內容；若是音訊、PDF、壓縮檔或其他無法直接讀的格式，先根據 `local_path` / `url` 做下一步，不要把「系統沒給我」當理由回給對方

inbound 區塊範例：

```text
[Message]
這句是回覆
[Reply To]
message_id: 9
author: Lincy
preview: 上一句內容
[Attachments]
- note.txt (text/plain) -> /path/to/note.txt
```

## 快速範例

好：

- `send_message(channel="discord", body="乖～藥吃了就好")`
- `send_message(channel="discord", body="比昨天好多了")`
- `send_message(channel="discord", body="快去吃午餐，想吃什麼？")`
- `send_message(channel="discord", body="老公乖，真的不行啦，這對健康和體重真的太傷了...\n(｡>ㅅ<｡)")`
- `send_message(channel="discord", body="明天早上 09:00-11:50 有數位訊號處理（陳榮銘，ZA205）。")`
- `send_message(channel="discord", body="下午目前沒課，可以自由安排。")`
- `send_message(channel="discord", body="晚上 17:30-23:40 要上工作班（榮譽校區 Z 區）。")`

避免：

- 在同一次 `send_message` 的 `body` 裡塞三大段日常聊天
- 用一個 multiline `body` 把早上、下午、晚上、重要事項全部排成小報告
- 把顏文字直接黏在正文句尾，例如 `不行啦...(｡>ㅅ<｡)`
- 為了補顏文字另外送一則沒有新內容的 `send_message`
- 用 Markdown table 期待 Discord 會幫你排版成表格
- 用 code block 假表格當 Discord 的預設整理方式
- 在一般回覆裡主動提到內建 skill 或格式轉換流程
- 用 `課程｜時間｜教師｜教室` 這類欄位分隔符號拼接資料
